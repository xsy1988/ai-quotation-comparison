"""AI 综合建议（LLM）+ 数字回检。

generate_ai_summary：
  1. 取机械对比结果（get_comparison），task 不存在抛 ValueError；
  2. 构造中文 prompt（system：资深采购比价顾问），只喂结构化对比结果，不让 LLM 碰原始文件；
  3. chat_json 调用（LLMError/LLMUnavailable 直接上抛，不降级）；
  4. 数字回检：从 markdown 抽金额，与机械结果候选集对账（容差 max(0.01, |值|×1%)）；
  5. 有可疑数则把清单反馈给 LLM 重生成（最多 max_regen 次）；仍对不上保存 mismatch，全对上 pass；
  6. 落库 ai_summary + parse_log（stage='ai_summary'）。

数字抽取规则：
  - 带货币上下文的钱数：数字紧邻 元/块/¥/￥（前后均可）；
  - 裸数：含小数点且绝对值 ≥ 0.01。
候选集：hierarchy 各值、suppliers 的 final/tax/discount、tooling 各项金额、
  drawers 各桶金额、fingerprint_groups 行金额、各供应商两两差值。
"""

import json
import re
import sqlite3

from app.compare.compare_engine import get_comparison
from app.llm import client as llm_client
from app.llm.client import LLMError

# 数字：支持千分位逗号与小数；货币单位可在数字前（¥100）或后（100元）
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_RE_MONEY_AFTER = re.compile(rf"(?<![\d.])({_NUMBER})\s*(?:元|块)(?![\w%])")
_RE_MONEY_BEFORE = re.compile(rf"(?:¥|￥)\s*({_NUMBER})(?![\d.])")
_RE_BARE_DECIMAL = re.compile(r"(?<![\d.])(\d+\.\d+)(?![\d.%])")  # 排除百分比

_PROMPT_SIZE_LIMIT = 50_000

_SYSTEM_PROMPT = (
    "你是一名资深采购比价顾问，擅长解读多家供应商的零件报价对比结果。"
    "你只依据输入的结构化对比数据作答，绝不臆造数字。"
    "所有引用的金额必须直接来自输入数据，禁止自行计算或编造。"
    "用中文输出，以 markdown 段落组织（可用标题、列表、表格）。"
    "输出必须是 JSON：{\"markdown\": \"<综合建议全文>\"}。"
)

_USER_TEMPLATE = """以下是同一零件多家供应商报价的【结构化机械对比结果】（JSON）。
请输出综合采购建议（markdown 正文放入 markdown 字段），必须覆盖：
1. 推荐排序及理由（结合最终含税单价、各模块结构）；
2. 主要差异解读（模块/抽屉维度的金额差距）；
3. 异常提醒（引用 warnings 中的 flags 与行计数，如低置信、未匹配、新工艺候选、勾稽异常）；
4. 议价抓手（哪家在哪项偏高、可压价点）；
5. 数据质量声明：建议基于解析后的结构化对比结果生成，未核对原始报价文件，金额引用须与数据一致。

【机械对比结果 JSON】
{comparison}
"""


def _trim_comparison(comparison: dict) -> dict:
    """裁剪过大的明细：保留 suppliers/hierarchy/tooling/warnings/fingerprint_groups 与 drawers 汇总，
    去掉 processing_details（逐条明细，量最大）。超限时再退掉 fingerprint_groups。"""
    payload = {
        "task_id": comparison["task_id"],
        "suppliers": comparison["suppliers"],
        "hierarchy": comparison["hierarchy"],
        "drawers": comparison["drawers"],
        "fingerprint_groups": comparison["fingerprint_groups"],
        "tooling": comparison["tooling"],
        "warnings": comparison["warnings"],
    }
    if len(json.dumps(payload, ensure_ascii=False)) > _PROMPT_SIZE_LIMIT:
        payload["fingerprint_groups"] = []
    return payload


def _candidates(comparison: dict) -> list[float]:
    """机械结果候选金额集（含各供应商两两差值）。"""
    values: list[float] = []

    def add(v) -> None:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            values.append(round(float(v), 6))

    for row in comparison["hierarchy"]:
        vals = [v for v in row["values"].values() if v is not None]
        for v in vals:
            add(v)
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                add(abs(vals[i] - vals[j]))
    for s in comparison["suppliers"]:
        add(s.get("final_unit_price_taxed"))
    for block in comparison["tooling"]:
        for item in block["items"]:
            add(item.get("amount"))
    for drawer in comparison["drawers"]:
        for group in drawer["groups"]:
            for v in group["values"].values():
                add(v)
    for fp in comparison["fingerprint_groups"]:
        for r in fp["rows"]:
            add(r.get("amount"))
    return values


def _extract_amounts(markdown: str) -> list[float]:
    """从建议文本抽金额：带 元/块/¥/￥ 上下文的钱数 + 含小数点且绝对值≥0.01 的裸数。
    markdown 标题行（如 "## 2.1 费用模块对比"）里的节号不是金额，先把标题行、
    表格分隔行剥掉再抽，避免误报。"""
    lines = [
        line
        for line in markdown.splitlines()
        if not line.lstrip().startswith("#") and not re.match(r"^\s*\|?[\s:|-]+\|?\s*$", line)
    ]
    body = "\n".join(lines)
    found: list[float] = []
    for regex in (_RE_MONEY_AFTER, _RE_MONEY_BEFORE, _RE_BARE_DECIMAL):
        for m in regex.finditer(body):
            text = m.group(1).replace(",", "")
            value = float(text)
            if abs(value) >= 0.01:
                found.append(round(value, 6))
    return list(dict.fromkeys(found))  # 同一金额可能被多条正则命中，去重


def _tolerance(value: float) -> float:
    return max(0.01, abs(value) * 0.01)


def _check_numbers(markdown: str, candidates: list[float]) -> list[dict]:
    """每个文本金额须在候选集中找到容差内匹配，否则记为可疑数。"""
    suspicious = []
    for value in _extract_amounts(markdown):
        if any(abs(value - c) <= _tolerance(c) for c in candidates):
            continue
        if any(abs(s["value"] - value) < 1e-9 for s in suspicious):
            continue
        suspicious.append({"value": value})
    return suspicious


def _log(conn: sqlite3.Connection, task_id: int, action: str, detail: dict,
         is_llm_call: int = 0) -> None:
    with conn:
        conn.execute(
            "INSERT INTO parse_log (task_id, stage, action, detail, is_llm_call)"
            " VALUES (?, 'ai_summary', ?, ?, ?)",
            (task_id, action, json.dumps(detail, ensure_ascii=False), is_llm_call),
        )


def _log_llm_call(conn, task_id, action, usage, round_no) -> None:
    _log(
        conn, task_id, action,
        {"round": round_no, "model": usage.get("model"),
         "tokens": usage.get("total_tokens"), "elapsed_ms": usage.get("elapsed_ms")},
        is_llm_call=1,
    )


def generate_ai_summary(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    chat_fn=None,
    max_regen: int = 1,
) -> dict:
    task = conn.execute(
        "SELECT id FROM comparison_task WHERE id = ?", (task_id,)
    ).fetchone()
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")

    comparison = get_comparison(conn, task_id)
    payload = _trim_comparison(comparison)
    candidates = _candidates(comparison)
    chat = chat_fn or llm_client.chat_json

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_TEMPLATE.format(
            comparison=json.dumps(payload, ensure_ascii=False))},
    ]

    markdown = None
    suspicious: list[dict] = []
    rounds = 0
    total_tokens = 0
    elapsed_ms = 0.0
    model = None
    for round_no in range(max_regen + 1):
        rounds = round_no + 1
        parsed, usage = chat(messages)
        model = usage.get("model")
        total_tokens += usage.get("total_tokens") or 0
        elapsed_ms += usage.get("elapsed_ms") or 0
        _log_llm_call(conn, task_id, "generated" if round_no == 0 else "regenerated",
                      usage, round_no)
        if not isinstance(parsed.get("markdown"), str) or not parsed["markdown"].strip():
            raise LLMError(
                f"LLM 输出缺少 markdown 字段：{json.dumps(parsed, ensure_ascii=False)[:200]}"
            )
        markdown = parsed["markdown"].strip()
        suspicious = _check_numbers(markdown, candidates)
        if not suspicious:
            break
        if round_no < max_regen:
            messages.append({
                "role": "user",
                "content": (
                    "数字回检发现以下金额与机械对比数据对不上，请修正后重新输出完整建议"
                    "（所有金额必须来自给定数据，不得臆造；无法核实的数字请删除）：\n"
                    + json.dumps(suspicious, ensure_ascii=False)
                ),
            })

    check_status = "pass" if not suspicious else "mismatch"
    check_detail = {
        "rounds": rounds,
        "checked": len(_extract_amounts(markdown)),
        "suspicious": suspicious,
    }
    _log(conn, task_id, "numeric_check",
         {"check_status": check_status, **check_detail})

    with conn:
        cur = conn.execute(
            "INSERT INTO ai_summary (task_id, content, check_status, check_detail,"
            " model, tokens, elapsed_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, markdown, check_status, json.dumps(check_detail, ensure_ascii=False),
             model, total_tokens or None, int(elapsed_ms) if elapsed_ms else None),
        )
    return _row_to_summary(
        conn.execute("SELECT * FROM ai_summary WHERE id = ?", (cur.lastrowid,)).fetchone()
    )


def _row_to_summary(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "content": row["content"],
        "check_status": row["check_status"],
        "check_detail": json.loads(row["check_detail"] or "null"),
        "created_at": row["created_at"],
    }


def get_ai_summary(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ai_summary WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return _row_to_summary(row) if row else None

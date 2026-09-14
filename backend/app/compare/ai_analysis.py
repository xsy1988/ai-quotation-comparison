"""AI 分析（LLM 生成的结构化对比表格）+ 数字回检 + 输入指纹。

与旧版「AI 建议」（markdown 段落）不同，本模块产出**结构化表格数据**，直接驱动
比价页面顶部的「AI 分析」模块：一行 = 一个维度（含税单价/优势/劣势/风险/建议），
一列 = 一家供应商；优势/劣势/风险各带 3 个固定子维度。

关键设计
- 只喂结构化数据：输入由 get_comparison 结果 + quote_line 明细 + quote.other_info 组装，
  LLM 全程接触不到原始文件；
- 含税单价/供应商名/排名**不取 LLM 输出**，一律以机械对比结果为准（唯一可信来源），
  LLM 只负责文字判断，按 quote_id 对齐；
- 数字回检：LLM 文本里出现的每个金额都必须能在机械结果候选集中找到容差内匹配，
  对不上则反馈重生成（最多 max_regen 次），仍对不上记 mismatch（前端提示核对）；
- 输入指纹 input_signature = hash(结构化输入 + prompt 版本)：
  指纹变了才需要重新分析（新增供应商/金额变化/解析信息更新都会改指纹），
  因此"进入比价页面自动触发一次、刷新不重复触发"由指纹天然保证。
"""

import hashlib
import json
import re
import sqlite3

from app import prompts
from app.compare.compare_engine import get_comparison
from app.llm import client as llm_client
from app.llm.client import LLMError

PROMPT_TASK = "ai_analysis"
LOG_STAGE = "ai_analysis"
PROMPT_SIZE_LIMIT = 60_000
RUNNING_STALE_MINUTES = 10  # running 超过该时长视为僵尸行，可重新触发

UNIT = "元/pcs"
EMPTY_CELL = "—"

ADVANTAGE_DIMS = ("成本/材料", "工艺/质量", "商务/交付")
WEAKNESS_DIMS = ("成本/费用", "材料/工艺", "商务/交付")
RISK_DIMS = ("质量/合规风险", "成本/商务风险", "资金/交付风险")

MODULE_LABELS = {
    "materials": "材料费",
    "processing": "加工费",
    "inspection": "检验费",
    "packaging_transport": "包装运输费",
    "sga_tax": "损管利税",
    "other": "其它费用",
}
MODULES = tuple(MODULE_LABELS)

CELL_MAX = 40
HEADLINE_MAX = 25

# 数字：支持千分位逗号与小数；货币单位可在数字前（¥100）或后（100元）
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_RE_MONEY_AFTER = re.compile(rf"(?<![\d.])({_NUMBER})\s*(?:元|块)(?![\w%])")
_RE_MONEY_BEFORE = re.compile(rf"(?:¥|￥)\s*({_NUMBER})(?![\d.])")
# 裸数只认小数：整数多为天数/穴数/模次等非金额。紧跟计量单位的小数（重量/尺寸/时间）不算金额，
# 否则「材料0.045kg」「阳极0.5mm」「保温1.5h」会被误判成编造金额。单位后可直接接中文
# （「2.8mm壁厚」「1.5h烘烤」），故单位只要求后面不是 ASCII 字母数字（避免 m 在 ml/cmos 里误命中）。
_BARE_DECIMAL_UNIT = (
    "kg|Kg|KG|g|G|mg|mm|Mm|MM|cm|CM|m|M|ml|ML|L|l|pcs|PCS|Pcs|次|天|月|年|周|小时|分钟|工作日|模次|万次"
    "|h|H|hr|穴|℃|度|张|套|副|支|台|档|个|项|件|片|米|克|吨|丝|寸"
)
_RE_BARE_DECIMAL = re.compile(
    rf"(?<![\d.])(\d+\.\d+)(?![\d.%]|\s*(?:{_BARE_DECIMAL_UNIT})(?![A-Za-z0-9]))"
)


# ========== 机械结果侧：输入组装 + 候选金额集 ==========


def _lines(conn: sqlite3.Connection, quote_ids: list[int]) -> list[list]:
    if not quote_ids:
        return []
    placeholders = ",".join("?" for _ in quote_ids)
    rows = conn.execute(
        f"""SELECT quote_id, module, item_type, item_name, amount, atom_code FROM quote_line
            WHERE quote_id IN ({placeholders}) ORDER BY quote_id, module, id""",
        quote_ids,
    )
    return [
        [r["quote_id"], r["module"], r["item_type"], r["item_name"], r["amount"], r["atom_code"]]
        for r in rows
    ]


def _category_name(conn: sqlite3.Connection, code: str | None) -> str | None:
    if not code:
        return None
    row = conn.execute("SELECT name FROM category WHERE code = ?", (code,)).fetchone()
    return row["name"] if row else None


def _supplier_entries(comparison: dict, lines: list[list]) -> list[dict]:
    """每供应商：机械指标 + 六模块合计 + 明细 + 模治具 + 其它信息；附带价格排名。

    输出顺序 = 比价表格的供应商列顺序（comparison["suppliers"]，即报价单 id 升序），
    让「AI 分析」与「报价单对比」两张表的供应商前后顺序完全一致（价格次序由 price_rank 表达）。
    """
    ranked = sorted(
        comparison["suppliers"],
        key=lambda s: (s["final_unit_price_taxed"] is None, s["final_unit_price_taxed"] or 0),
    )
    rank_of = {s["quote_id"]: i + 1 for i, s in enumerate(ranked)}
    module_totals: dict[int, dict[str, float]] = {}
    for qid, module, _t, _n, amount, _a in lines:
        if amount is None:
            continue
        bucket = module_totals.setdefault(qid, {})
        bucket[module] = round(bucket.get(module, 0.0) + amount, 6)

    entries = []
    for s in comparison["suppliers"]:
        qid = s["quote_id"]
        entries.append(
            {
                "quote_id": qid,
                "supplier_name": s["supplier_name"],
                "part_name": s["part_name"],
                "scheme": s["scheme"],
                "category_code": s["category_code"],
                "final_unit_price_taxed": s["final_unit_price_taxed"],
                "price_rank": rank_of[qid],
                "is_lowest": rank_of[qid] == 1 and s["final_unit_price_taxed"] is not None,
                "calc_check": s["calc_check"],
                "flags": s["flags"],
                "modules": module_totals.get(qid, {}),
                "other_info": s.get("other_info"),
            }
        )
    return entries


def build_input(conn: sqlite3.Connection, comparison: dict) -> dict:
    """组装喂给 LLM 的结构化输入（不含原始文件、不含 processing 逐条以外的冗余）。"""
    suppliers = comparison["suppliers"]
    quote_ids = [s["quote_id"] for s in suppliers]
    lines = _lines(conn, quote_ids)
    entries = _supplier_entries(comparison, lines)
    by_id = {e["quote_id"]: e for e in entries}
    base = next(
        (
            b
            for b in comparison["basic"]
            if any(b.get(f) for f in ("project_name", "part_name", "material_spec", "moq"))
        ),
        comparison["basic"][0] if comparison["basic"] else {},
    )
    category = _first_category(suppliers)

    tooling = [
        {
            "type": block["type"],
            "items": [
                {
                    "quote_id": item["quote_id"],
                    "supplier_name": by_id[item["quote_id"]]["supplier_name"],
                    "item_name": item["item_name"],
                    "amount": item["amount"],
                    "cavity_count": item["cavity_count"],
                    "lifespan": item["lifespan"],
                }
                for item in block["items"]
            ],
        }
        for block in comparison["tooling"]
        if block["items"]
    ]

    payload = {
        "task_id": comparison["task_id"],
        "unit": UNIT,
        "category": category,
        "category_name": _category_name(conn, category),
        "part": {
            "project_name": base.get("project_name"),
            "part_name": base.get("part_name"),
            "material_spec": base.get("material_spec"),
            "quote_date": base.get("quote_date"),
            "currency": base.get("currency"),
            "moq": base.get("moq"),
        },
        "suppliers": entries,
        "summary_rows": [
            {
                "label": row["label"],
                "values": {str(q): v for q, v in row["values"].items()},
            }
            for row in comparison["hierarchy"]
        ],
        "lines": lines,
        "drawers": [
            {
                "drawer_name": drawer.get("name") or drawer["scope"],
                "scope": drawer["scope"],
                "groups": [
                    {"group_name": g["group_name"], "values": {str(k): v for k, v in g["values"].items()}}
                    for g in drawer["groups"]
                ],
            }
            for drawer in comparison["drawers"]
        ],
        "tooling": tooling,
        "warnings": comparison["warnings"],
    }
    return _trim(payload)


def _first_category(suppliers: list[dict]) -> str | None:
    for s in suppliers:
        if s["category_code"]:
            return s["category_code"]
    return None


def _trim(payload: dict) -> dict:
    """超限时按信息密度从低到高裁剪：明细 → 抽屉 → 模治具明细。"""
    if len(json.dumps(payload, ensure_ascii=False)) <= PROMPT_SIZE_LIMIT:
        return payload
    payload = {**payload, "lines": []}
    if len(json.dumps(payload, ensure_ascii=False)) <= PROMPT_SIZE_LIMIT:
        return payload
    payload = {**payload, "drawers": []}
    if len(json.dumps(payload, ensure_ascii=False)) <= PROMPT_SIZE_LIMIT:
        return payload
    return {
        **payload,
        "tooling": [
            {"type": b["type"], "items": [{k: v for k, v in i.items() if k != "item_name"} for i in b["items"]]}
            for b in payload["tooling"]
        ],
    }


def candidates(payload: dict) -> list[float]:
    """机械结果候选金额集：层级行值/模块合计/逐条明细金额/勾稽差值/模治具/抽屉桶。

    不只是"出现过的数字"——分项加总与模块合计的差值、未税合计与模块之和的差值
    也进候选集，因为专家结论里常引用"分项加总差 0.36"这类勾稽差。
    逐条明细金额必须进：结论里"CNC1 4.5""管理费 1.89"引用的就是单行金额。
    """
    values: list[float] = []

    def add(v) -> None:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            values.append(round(float(v), 6))

    for row in payload["summary_rows"]:
        vals = []
        for raw in row["values"].values():
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                add(raw)
                vals.append(float(raw))
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                add(abs(vals[i] - vals[j]))

    for supplier in payload["suppliers"]:
        add(supplier.get("final_unit_price_taxed"))
        module_sum = sum(v for v in supplier["modules"].values() if v is not None)
        if supplier["modules"]:
            add(module_sum)

    # 逐条明细金额 + 逐模块勾稽差：|Σ明细 − 模块合计| 与 Σ明细
    line_sum: dict[tuple[int, str], float] = {}
    for qid, module, _t, _n, amount, _a in payload["lines"]:
        if amount is None:
            continue
        add(amount)
        key = (qid, module)
        line_sum[key] = line_sum.get(key, 0.0) + amount
    for (qid, module), total in line_sum.items():
        add(total)
        supplier = next((s for s in payload["suppliers"] if s["quote_id"] == qid), None)
        module_total = (supplier or {}).get("modules", {}).get(module)
        if module_total is not None:
            add(abs(total - module_total))
            add(total + module_total)

    # 未税合计 − Σ模块（前端"分项加总差"口径）
    for row in payload["summary_rows"]:
        if row["label"] == "未税合计":
            for qid_str, untaxed in row["values"].items():
                if untaxed is None:
                    continue
                supplier = next(
                    (s for s in payload["suppliers"] if str(s["quote_id"]) == qid_str), None
                )
                if not supplier or not supplier["modules"]:
                    continue
                diff = abs(untaxed - sum(v for v in supplier["modules"].values() if v is not None))
                add(diff)

    for block in payload["tooling"]:
        for item in block["items"]:
            add(item.get("amount"))
    for drawer in payload["drawers"]:
        for group in drawer["groups"]:
            for v in group["values"].values():
                add(v)
    return values


# ========== 数字回检 ==========

_TEXT_KEYS = ("advantage", "weakness", "risk", "suggestion")


def extract_amounts(text: str) -> list[float]:
    """抽金额：带 元/块/¥/￥ 上下文 + 含小数点且绝对值 ≥ 0.01 的裸数（排除计量单位后缀）。"""
    lines = [
        line
        for line in text.splitlines()
        if not line.lstrip().startswith("#") and not re.match(r"^\s*\|?[\s:|-]+\|?\s*$", line)
    ]
    found: list[float] = []
    for regex in (_RE_MONEY_AFTER, _RE_MONEY_BEFORE, _RE_BARE_DECIMAL):
        for m in regex.finditer("\n".join(lines)):
            value = float(m.group(1).replace(",", ""))
            if abs(value) >= 0.01:
                found.append(round(value, 6))
    return list(dict.fromkeys(found))  # 同一金额可能被多条正则命中，去重


def _tolerance(value: float) -> float:
    return max(0.01, abs(value) * 0.01)


def _check_numbers(text: str, candidate_values: list[float]) -> list[dict]:
    suspicious = []
    for value in extract_amounts(text):
        if any(abs(value - c) <= _tolerance(c) for c in candidate_values):
            continue
        if any(abs(s["value"] - value) < 1e-9 for s in suspicious):
            continue
        suspicious.append({"value": value})
    return suspicious


# ========== LLM 输出结构校验与归一化 ==========


def _cell(value, fallback: str = EMPTY_CELL) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _detail_block(raw, dims: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {d: EMPTY_CELL for d in dims}
    return {d: _cell(raw.get(d)) for d in dims}


def _structure_errors(parsed, quote_ids: list[int]) -> list[str]:
    errors: list[str] = []
    if not isinstance(parsed, dict):
        return ["顶层必须是 JSON 对象"]
    if not isinstance(parsed.get("overall"), str) or not parsed["overall"].strip():
        errors.append("缺少 overall（专家总评）")
    items = parsed.get("suppliers")
    if not isinstance(items, list) or not items:
        return errors + ["缺少 suppliers 数组"]
    seen: list = []
    for item in items:
        if not isinstance(item, dict):
            errors.append("suppliers 元素必须是对象")
            continue
        try:
            qid = int(item.get("quote_id"))
        except (TypeError, ValueError):
            errors.append(f"quote_id 非法：{item.get('quote_id')!r}")
            continue
        seen.append(qid)
    expected = set(quote_ids)
    got = set(seen)
    if len(seen) != len(got):
        errors.append(f"quote_id 重复：{sorted(seen)}")
    unknown = sorted(got - expected)
    if unknown:
        errors.append(f"quote_id 不在输入中：{unknown}（可用 {sorted(expected)}）")
    return errors


def normalize(parsed: dict, payload: dict) -> tuple[dict, dict]:
    """LLM 输出 → 前端渲染用结构：文字取 LLM，名称/价格/排名取机械结果。"""
    entries = {e["quote_id"]: e for e in payload["suppliers"]}
    raw_items = {
        int(item["quote_id"]): item
        for item in parsed.get("suppliers") or []
        if isinstance(item, dict) and str(item.get("quote_id", "")).strip() != ""
        and _as_int(item.get("quote_id")) is not None
    }
    suppliers: list[dict] = []
    missing: list[int] = []
    for entry in payload["suppliers"]:
        qid = entry["quote_id"]
        item = raw_items.get(qid)
        if item is None:
            missing.append(qid)
        item = item or {}
        suppliers.append(
            {
                "quote_id": qid,
                "name": entry["supplier_name"],
                "part_name": entry["part_name"],
                "scheme": entry["scheme"],
                "price_rank": entry["price_rank"],
                "is_lowest": entry["is_lowest"],
                "final_unit_price_taxed": entry["final_unit_price_taxed"],
                "advantage": _cell(item.get("advantage")),
                "weakness": _cell(item.get("weakness")),
                "risk": _cell(item.get("risk")),
                "suggestion": _cell(item.get("suggestion")),
                "advantage_detail": _detail_block(item.get("advantage_detail"), ADVANTAGE_DIMS),
                "weakness_detail": _detail_block(item.get("weakness_detail"), WEAKNESS_DIMS),
                "risk_detail": _detail_block(item.get("risk_detail"), RISK_DIMS),
            }
        )
    content = {
        "unit": UNIT,
        "category": payload["category"],
        "category_name": payload["category_name"],
        "part": payload["part"],
        "overall": _cell(parsed.get("overall")),
        "suppliers": suppliers,
        "dimensions": {
            "advantage": list(ADVANTAGE_DIMS),
            "weakness": list(WEAKNESS_DIMS),
            "risk": list(RISK_DIMS),
        },
    }
    return content, {"missing_quote_ids": missing, "unknown_quote_ids": sorted(set(raw_items) - set(entries))}


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _content_text(content: dict) -> str:
    parts = [content["overall"]]
    for s in content["suppliers"]:
        parts.extend(s[k] for k in _TEXT_KEYS)
        for block in ("advantage_detail", "weakness_detail", "risk_detail"):
            parts.extend(s[block].values())
    return "\n".join(parts)


def _over_length(content: dict) -> list[str]:
    issues: list[str] = []
    for s in content["suppliers"]:
        name = s["name"]
        for key in _TEXT_KEYS:
            if len(s[key]) > HEADLINE_MAX:
                issues.append(f"{name}.{key} > {HEADLINE_MAX} 字")
    for s in content["suppliers"]:
        for block in ("advantage_detail", "weakness_detail", "risk_detail"):
            for dim, cell in s[block].items():
                if len(cell) > CELL_MAX:
                    issues.append(f"{s['name']}.{block}.{dim} > {CELL_MAX} 字")
    return issues


# ========== 输入指纹 ==========

def input_signature(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    version = prompts.get_prompt_version(PROMPT_TASK)
    return hashlib.sha256(f"{version}\n{blob}".encode("utf-8")).hexdigest()


# ========== 落库 ==========


def _log(conn: sqlite3.Connection, task_id: int, action: str, detail: dict,
         is_llm_call: int = 0) -> None:
    with conn:
        conn.execute(
            "INSERT INTO parse_log (task_id, stage, action, detail, is_llm_call)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, LOG_STAGE, action, json.dumps(detail, ensure_ascii=False), is_llm_call),
        )


def _log_llm_call(conn, task_id, action, usage, round_no) -> None:
    _log(
        conn, task_id, action,
        {"round": round_no, "model": usage.get("model"),
         "tokens": usage.get("total_tokens"), "elapsed_ms": usage.get("elapsed_ms")},
        is_llm_call=1,
    )


def _row_to_analysis(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "task_id": row["task_id"],
        "signature": row["input_signature"],
        "status": row["status"],
        "content": json.loads(row["content"]) if row["content"] else None,
        "check_status": row["check_status"],
        "check_detail": json.loads(row["check_detail"] or "null"),
        "error": row["error"],
        "model": row["model"],
        "tokens": row["tokens"],
        "elapsed_ms": row["elapsed_ms"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _running_is_stale(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    age = conn.execute(
        "SELECT (julianday('now', 'localtime') - julianday(?)) * 24 * 60 AS minutes",
        (row["updated_at"],),
    ).fetchone()[0]
    return age is None or age > RUNNING_STALE_MINUTES


def _signature_row(conn: sqlite3.Connection, task_id: int, signature: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM ai_analysis WHERE task_id = ? AND input_signature = ?"
        " ORDER BY id DESC LIMIT 1",
        (task_id, signature),
    ).fetchone()


def get_ai_analysis(conn: sqlite3.Connection, task_id: int, *, signature: str | None = None) -> dict:
    """查询态：返回当前输入指纹对应的分析行 + 是否需要自动触发。"""
    comparison = get_comparison(conn, task_id)
    if not comparison["suppliers"]:
        return {
            "signature": None,
            "analysis": None,
            "stale": False,
            "auto_run": False,
            "in_progress": False,
        }
    payload = build_input(conn, comparison)
    sig = signature or input_signature(payload)
    row = _signature_row(conn, task_id, sig)
    stale_row = conn.execute(
        "SELECT * FROM ai_analysis WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    in_progress = bool(row and row["status"] == "running" and not _running_is_stale(conn, row))
    auto_run = row is None
    return {
        "signature": sig,
        "analysis": _row_to_analysis(row) if row else None,
        "stale": bool(stale_row and stale_row["input_signature"] != sig),
        "auto_run": auto_run,
        "in_progress": in_progress,
    }


def run_ai_analysis(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    chat_fn=None,
    max_regen: int = 1,
) -> dict:
    """生成（或复用进行中的）AI 分析。LLM 故障不降级：落 failed 行后上抛。"""
    task = conn.execute("SELECT id FROM comparison_task WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise ValueError(f"任务不存在: {task_id}")

    comparison = get_comparison(conn, task_id)
    if not comparison["suppliers"]:
        raise ValueError(f"任务 {task_id} 下没有可分析的报价单")

    payload = build_input(conn, comparison)
    candidate_values = candidates(payload)
    signature = input_signature(payload)

    existing = _signature_row(conn, task_id, signature)
    if existing is not None:
        if existing["status"] == "completed":
            return _row_to_analysis(existing)
        if existing["status"] == "running" and not _running_is_stale(conn, existing):
            return _row_to_analysis(existing)

    with conn:
        cur = conn.execute(
            "INSERT INTO ai_analysis (task_id, input_signature, status) VALUES (?, ?, 'running')",
            (task_id, signature),
        )
    row_id = cur.lastrowid

    chat = chat_fn or llm_client.chat_json
    messages = prompts.build_messages(
        PROMPT_TASK, {"comparison": json.dumps(payload, ensure_ascii=False)}
    )

    quote_ids = [s["quote_id"] for s in payload["suppliers"]]
    best: dict | None = None  # 保留最干净的一轮，重生成更差时不回退质量
    errors: list[str] = []
    rounds = 0
    total_tokens = 0
    elapsed_ms = 0.0
    model = None
    try:
        for round_no in range(max_regen + 1):
            rounds = round_no + 1
            parsed, usage = chat(messages)
            model = usage.get("model")
            total_tokens += usage.get("total_tokens") or 0
            elapsed_ms += usage.get("elapsed_ms") or 0
            _log_llm_call(
                conn, task_id, "generated" if round_no == 0 else "regenerated", usage, round_no
            )

            errors = _structure_errors(parsed, quote_ids)
            if errors:
                attempt = {
                    "score": (len(errors), 0, 0), "content": None, "errors": errors,
                    "suspicious": [], "over_length": [], "extra": {},
                }
            else:
                attempt_content, attempt_extra = normalize(parsed, payload)
                attempt_suspicious = _check_numbers(_content_text(attempt_content), candidate_values)
                attempt_over = _over_length(attempt_content)
                attempt = {
                    "score": (0, len(attempt_suspicious), len(attempt_over)),
                    "content": attempt_content, "errors": [],
                    "suspicious": attempt_suspicious,
                    "over_length": attempt_over, "extra": attempt_extra,
                }
            if best is None or attempt["score"] < best["score"]:
                best = attempt
            if best["score"][:2] == (0, 0):
                break
            if round_no < max_regen:
                messages.append(
                    {"role": "user", "content": _feedback(attempt["errors"], attempt["suspicious"], attempt["over_length"])}
                )

        content = best["content"] if best else None
        if content is None:
            # 结构始终不合法：无内容可展示，直接失败（不落半成品）
            raise LLMError("LLM 输出结构不合法：" + "；".join(errors[:5]))
        errors = best["errors"]
        suspicious = best["suspicious"]
        extra = best["extra"]
    except Exception as exc:
        with conn:
            conn.execute(
                "UPDATE ai_analysis SET status = 'failed', error = ?,"
                " updated_at = datetime('now', 'localtime') WHERE id = ?",
                (str(exc)[:500], row_id),
            )
        raise

    check_status = "pass" if not suspicious and not errors else "mismatch"
    checked = extract_amounts(_content_text(content))
    check_detail = {
        "rounds": rounds,
        "checked": len(checked),
        "suspicious": suspicious,
        "structure_errors": errors,
        "missing_quote_ids": extra.get("missing_quote_ids", []),
        "unknown_quote_ids": extra.get("unknown_quote_ids", []),
        "over_length": best["over_length"],
    }
    _log(conn, task_id, "numeric_check", {"check_status": check_status, **check_detail})

    with conn:
        conn.execute(
            """UPDATE ai_analysis SET status = 'completed', content = ?, check_status = ?,
                   check_detail = ?, error = NULL, model = ?, tokens = ?, elapsed_ms = ?,
                   updated_at = datetime('now', 'localtime') WHERE id = ?""",
            (
                json.dumps(content, ensure_ascii=False),
                check_status,
                json.dumps(check_detail, ensure_ascii=False),
                model,
                total_tokens or None,
                int(elapsed_ms) if elapsed_ms else None,
                row_id,
            ),
        )
    row = conn.execute("SELECT * FROM ai_analysis WHERE id = ?", (row_id,)).fetchone()
    return _row_to_analysis(row)


def _feedback(errors: list[str], suspicious: list[dict], over_length: list[str] | None = None) -> str:
    parts = ["上次输出有以下问题，请修正后重新输出完整 JSON（结构不要变）："]
    if errors:
        parts.append("结构问题：\n" + "\n".join(f"- {e}" for e in errors))
    if suspicious:
        parts.append(
            "数字回检不通过（这些数值在给定数据里找不到，属编造或自行计算出错，"
            "请删除或改为输入数据中的原始数值）：\n"
            + json.dumps(suspicious, ensure_ascii=False)
        )
    if over_length:
        parts.append("超长单元格（请压缩到限制字数内）：\n" + "\n".join(f"- {o}" for o in over_length))
    return "\n".join(parts)

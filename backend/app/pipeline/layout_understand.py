"""LLM 版面理解：IR → 信封 JSON {"offers": [...]} + 品类判定 + 脚本交叉验证。

主入口 parse_ir_with_llm（返回信封 JSON）与 parse_ir_with_llm_traced
（额外返回 LLM 调用尝试记录与 cross_check 结果，pipeline 记 parse_log 用）。
offers 按"产品 × 报价方案"拆分：同产品多方案、多产品报价单各为一个 offer；
普通报价单只有 1 个元素（兼容旧格式：LLM 输出无 offers 层时自动包一层）。

流程：IR 紧凑序列化 → prompts 注册表组装 prompt（constitution 全局宪法 + parse_quote
模板含品类清单与负例）→ chat_json → 摘取 offer._self_check 存档 _derived →
信封结构 + 逐 offer Draft7 校验，失败用 retry_feedback 模板把错误列表 append 进对话重试，
上限 3 轮（schema 类按 schema_invalid 反馈、JSON 解析失败按 json_unparseable 反馈）；
L0 通过后跑 L1 溯源校验（金额必须在自身 evidence.raw_text 中、坐标必须落在 IR 范围内，
traceability 反馈），第 3 轮仍失败抛 LLMValidateError。L1 通过（或仅 info 级）时 L2 勾稽
问题（blame=B）写入 attempts 末条 l2_issues 只记录不打断。
每个 LLM 轮次都会把原始输出与校验结果落盘到 {data_dir}/llm_trace/{file_hash}_r{n}.json
（LLM_TRACE=0 关闭）——失败轮此前什么都不留，事后无法复盘；L1 无论 L0 是否通过都算一遍并
入档（schema 报错会掩盖真正的首因）；两轮输出完全一致（模型未做任何修改）时提前终止，
不再空烧剩余轮次。

成功后跑 cross_check：独立脚本侧解析（自带简化关键词分类器，不复用规则解析器）
与 LLM 条目按 evidence.location 行号对账，差异写 _cross_check 供 persist 落库。
"""

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from app import prompts
from app.ir import IR
from app.llm import client as llm_client
from app.llm.client import LLMError, LLMUnavailable
from app.normalize import normalize_amount
from app.validate.validate import MODULES, iter_offers, load_envelope_schema, load_schema
from app.validate.validators import (
    parse_location,
    validate_l1_evidence_consistency,
    validate_l1_traceability,
    validate_l2_reconcile,
)

MAX_ATTEMPTS = 3
"""LLM 版面理解最大尝试轮数（含首轮）：schema/溯源/解析失败均按此上限重试。"""

LLM_TRACE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "llm_trace"
"""每轮 LLM 原始输出落盘目录（数据目录，非源码）；LLM_TRACE=0 时不写。"""

# conn 不可用时兜底品类清单（与 scripts/import_master_data.py 保持一致）
DEFAULT_CATEGORIES = [
    ("CAT-WJWK", "五金外壳"),
    ("CAT-CMF", "CMF"),
    ("CAT-WJNZ", "五金内置"),
    ("CAT-SJ", "塑胶"),
    ("CAT-PCBA", "PCBA"),
    ("CAT-GJ", "硅胶"),
    ("CAT-BC", "包材"),
]


class LLMValidateError(LLMUnavailable):
    """LLM 输出反复不符合 schema（重试用尽）。"""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


# ---------------------------------------------------------------------------
# IR 序列化与 prompt
# ---------------------------------------------------------------------------

def _serialize_ir(ir: IR) -> str:
    """IR → 紧凑文本：每行 sheet|row|col:value|col:value...，附文本块。"""
    lines: list[str] = []
    for t in ir.tables:
        cells = [c for c in t.cells if c.value is not None and c.value != ""]
        if not cells:
            continue
        parts = [f"{c.col}:{c.value}" for c in cells]
        lines.append(f"{t.sheet}|{t.row_number}|" + "|".join(parts))
    for b in ir.blocks:
        lines.append(f"BLOCK {b.sheet}!R{b.row}C{b.col} {b.text}")
    for note in getattr(ir, "notes", None) or []:
        lines.append(note)
    return "\n".join(lines)


def _load_categories(conn: sqlite3.Connection | None) -> list[tuple[str, str]]:
    if conn is not None:
        try:
            rows = [(r[0], r[1]) for r in conn.execute("SELECT code, name FROM category ORDER BY code")]
            if rows:
                return rows
        except sqlite3.Error:
            pass
    return DEFAULT_CATEGORIES


def _build_messages(ir: IR, categories: list[tuple[str, str]]) -> list[dict]:
    """组装 LLM 消息：prompt 文本统一由 app.prompts 注册表维护
    （constitution 宪法 + parse_quote 模板 + 创锋负例 fewshot 自动注入）。"""
    cat_lines = " / ".join(f"{code} {name}" for code, name in categories)
    return prompts.build_messages(
        "parse_quote",
        {"categories": cat_lines, "ir_serialized": _serialize_ir(ir)},
    )


def _archive_self_check(parsed: Any) -> None:
    """把每个 offer 顶层的 _self_check 摘下入档 offer["_derived"]["llm_self_check"]（不进 schema 校验）；
    LLM 未输出 _self_check 时不报错，存档 null。"""
    if not isinstance(parsed, dict):
        return
    for offer in parsed.get("offers") or []:
        if isinstance(offer, dict):
            offer.setdefault("_derived", {})["llm_self_check"] = offer.pop("_self_check", None)


def _validate(data: Any) -> list[str]:
    """先校验信封结构，再对每个 offer 用内层 schema 校验；错误带 offers[i] 路径前缀。"""
    lines: list[str] = []
    for e in sorted(
        Draft7Validator(load_envelope_schema()).iter_errors(data),
        key=lambda e: list(e.absolute_path),
    ):
        lines.append(f"$.{'.'.join(str(p) for p in e.absolute_path)}: {e.message}")
    if not lines and isinstance(data, dict):
        for i, offer in enumerate(data.get("offers") or []):
            for e in sorted(
                Draft7Validator(load_schema()).iter_errors(offer),
                key=lambda e: list(e.absolute_path),
            ):
                lines.append(
                    f"$.offers[{i}].{'.'.join(str(p) for p in e.absolute_path)}: {e.message}"
                )
    return lines[:10]


def _strip_disallowed_nulls(node: Any, schema_node: dict | None) -> None:
    """就地修复 LLM 常见笔误：值为 null 但 schema 不允许 null 的非 required 属性，直接删键。"""
    if not isinstance(node, dict) or not isinstance(schema_node, dict):
        return
    props = schema_node.get("properties") or {}
    required = set(schema_node.get("required") or [])
    for key in list(node.keys()):
        sub = props.get(key)
        if node[key] is None and key not in required and isinstance(sub, dict):
            types = sub.get("type")
            allowed = types if isinstance(types, list) else [types]
            if "null" not in allowed:
                del node[key]
                continue
        if isinstance(node[key], dict):
            _strip_disallowed_nulls(node[key], props.get(key))
        elif isinstance(node[key], list) and isinstance(props.get(key), dict):
            item_schema = props[key].get("items")
            for el in node[key]:
                _strip_disallowed_nulls(el, item_schema)


# ---------------------------------------------------------------------------
# 脚本交叉验证（独立实现：自带简化分类器，不复用 simple_excel_parse）
# ---------------------------------------------------------------------------

# 简化独立版分区关键词（与 simple_excel_parse._SECTION_RULES 刻意不同，保证交叉验证独立性）
_SCRIPT_SECTION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("materials", ("材料费", "原材料", "铝材", "钢材", "塑胶粒", "五金件")),
    ("processing", ("加工费", "工艺", "CNC", "cnc", "冲压", "注塑", "压铸", "打磨")),
    ("inspection", ("检验费", "检测费", "品检", "测试费")),
    ("packaging_transport", ("包装运输", "运费", "物流费", "包材费")),
    ("sga_tax", ("损管利税", "增值税", "管理费", "利润", "损耗")),
    ("tooling", ("模治具", "模具费", "治具", "检具", "钢网")),
]
_SCRIPT_SUMMARY_LABELS = {
    "未税合计": "untaxed_total",
    "税额": "tax_amount",
    "含税合计": "taxed_total",
    "折扣": "discount",
    "最终含税单价": "final_unit_price_taxed",
}


def _tol(value: float) -> float:
    return max(0.01, abs(value) * 0.01)


def _script_classify(label: str, text: str) -> str | None:
    """先看分区标签（行首单元格），再看整行文本。"""
    for source in (label, text):
        for module, keywords in _SCRIPT_SECTION_RULES:
            if any(k in source for k in keywords):
                return module
    return None


def _script_parse(ir: IR) -> tuple[dict, dict, dict, dict]:
    """脚本侧独立解析：返回 (行号→该行金额列表, 模块→明细和, 汇总标签→金额, 行→(模块, 首个金额))。
    行级归属供多 offer 场景按各 offer 自己的行范围对账模块合计。"""
    row_amounts: dict[tuple[str, int], list[float]] = {}
    module_sums: dict[str, float] = {m: 0.0 for m in (*MODULES, "tooling")}
    summary_seen: dict[str, float] = {}
    row_module: dict[tuple[str, int], tuple[str, float]] = {}
    for t in ir.tables:
        cells = [c for c in t.cells if c.value is not None and c.value != ""]
        if not cells:
            continue
        amounts = [a for a in (normalize_amount(c.value) for c in cells) if a is not None]
        row_amounts[(t.sheet, t.row_number)] = amounts
        texts = " ".join(str(c.value) for c in cells)
        first_text = str(cells[0].value)
        amount = amounts[0] if amounts else None
        if amount is None:
            continue
        if first_text in _SCRIPT_SUMMARY_LABELS:
            summary_seen[_SCRIPT_SUMMARY_LABELS[first_text]] = amount
            continue
        section = _script_classify(first_text, texts)
        if section is None:
            continue
        if first_text.endswith("合计"):
            continue  # 合计行单独由模块 total 对账，不进明细和
        module_sums[section] = round(module_sums[section] + amount, 6)
        row_module[(t.sheet, t.row_number)] = (section, amount)
    return row_amounts, module_sums, summary_seen, row_module


def _iter_amount_items(offer: dict):
    """产出 (层级路径, 金额字段名, item dict)，供行级对账。入参为单个 offer。"""
    up = offer.get("unit_price") or {}
    for module in MODULES:
        for item in (up.get(module) or {}).get("items") or []:
            yield module, "amount_per_pc", item
    tooling = offer.get("tooling")
    if tooling:
        for key in ("molds", "fixtures", "stencils"):
            for item in (tooling.get(key) or {}).get("items") or []:
                yield f"tooling.{key}", "amount", item


def _parse_location(location: str | None) -> tuple[str | None, int, int] | None:
    """evidence.location → (sheet, 起始行, 结束行)；解析失败返回 None。

    复用 validate/validators.parse_location（同时支持 `sheet!R9C3` RC 式与 `sheet!B7` A1 式）。
    这里此前自带的正则解不出 RC 式：`CNC5分钟 (2)!R9C4:R9C6` 被解成 ('CNC', 5)，行号错位后
    又叠加"按行号跨 sheet 回退"，于是第 5 行（联系电话 18003008878）被当成金额出处，
    凭空造出一批 _cross_check 冲突。
    """
    parsed = parse_location(location)
    if parsed is None:
        return None
    sheet, row_min, row_max, _col_min, _col_max = parsed
    return sheet, row_min, row_max


def _row_amounts_of(
    row_amounts: dict[tuple[str, int], list[float]], sheet: str | None, row_min: int, row_max: int
) -> list[float]:
    """取该位置的脚本金额。

    location 未带 sheet 时按行号在任意 sheet 中找（单表单据常见）；带了 sheet 却对不上时
    不再跨 sheet 猜测——猜出来的金额比没有金额更容易误导。
    """
    if sheet is not None:
        return [
            amount for row in range(row_min, row_max + 1) for amount in row_amounts.get((sheet, row), [])
        ]
    return [
        amount
        for (_sheet, row), amounts in row_amounts.items()
        if row_min <= row <= row_max
        for amount in amounts
    ]


def cross_check(ir: IR | dict, data: dict) -> dict:
    """脚本侧独立对账：LLM 条目金额 vs IR 原文行金额；模块合计/summary 层级差异计入 total_conflicts。
    data 为信封 {"offers": [...]}（兼容旧格式：无 offers 层的单个 quote 对象）。

    行级差异 > max(0.01, |值|×1%) 的条目加 _cross_check={"script_value":x,"llm_value":y}。
    返回 {"item_conflicts": n, "total_conflicts": [...]}。
    """
    if isinstance(ir, dict):
        ir = IR.from_dict(ir)
    row_amounts, module_sums, summary_seen, row_module = _script_parse(ir)
    offers = iter_offers(data)

    item_conflicts = 0
    for offer in offers:
        for module, amount_key, item in _iter_amount_items(offer):
            value = item.get(amount_key)
            if value is None:
                continue
            loc = _parse_location((item.get("evidence") or {}).get("location"))
            if loc is None:
                continue
            script_amounts = _row_amounts_of(row_amounts, loc[0], loc[1], loc[2])
            if not script_amounts:
                continue
            if any(abs(value - a) <= _tol(value) for a in script_amounts):
                continue
            script_value = min(script_amounts, key=lambda a: abs(a - value))
            item["_cross_check"] = {"script_value": script_value, "llm_value": value}
            item_conflicts += 1

    total_conflicts: list[dict] = []
    if len(offers) > 1:
        # 多 offer：每个 offer 的模块合计只与自己条目所在行的脚本金额对账
        #（全表聚合会把各方案/各产品行重复计入而误报）；summary 层级跳过，条目级对账已覆盖
        for idx, offer in enumerate(offers):
            up = offer.get("unit_price") or {}
            for module in MODULES:
                mod = up.get(module) or {}
                total = mod.get("total")
                if total is None:
                    continue
                rows: set[tuple[str, int]] = set()
                for item in mod.get("items") or []:
                    loc = _parse_location((item.get("evidence") or {}).get("location"))
                    if loc is None:
                        continue
                    sheet_name, row_min, row_max = loc
                    if sheet_name is None:
                        rows.update(k for k in row_module if row_min <= k[1] <= row_max)
                    else:
                        rows.update((sheet_name, row) for row in range(row_min, row_max + 1))
                script_sum = round(
                    sum(a for r, (m, a) in row_module.items() if r in rows and m == module), 6
                )
                if script_sum > 0 and script_sum > float(total) + _tol(float(total)):
                    total_conflicts.append(
                        {"level": "module_total", "offer": idx, "module": module,
                         "script_value": script_sum, "llm_value": total}
                    )
        return {"item_conflicts": item_conflicts, "total_conflicts": total_conflicts}

    up = offers[0].get("unit_price") or {}
    for module in MODULES:
        mod = up.get(module) or {}
        total = mod.get("total")
        script_sum = module_sums.get(module, 0.0)
        if total is not None and script_sum > 0 and script_sum > float(total) + _tol(float(total)):
            total_conflicts.append(
                {"level": "module_total", "module": module,
                 "script_value": script_sum, "llm_value": total}
            )

    summary = up.get("summary") or {}
    script_sga = module_sums.get("sga_tax", 0.0)
    script_tax = summary_seen.get("tax_amount")
    script_untaxed = round(
        sum(v for k, v in module_sums.items() if k not in ("sga_tax", "tooling"))
        + script_sga - (script_tax or 0), 6
    )
    if summary.get("untaxed_total") is not None and abs(summary["untaxed_total"] - script_untaxed) > _tol(script_untaxed):
        total_conflicts.append(
            {"level": "summary", "field": "untaxed_total",
             "script_value": script_untaxed, "llm_value": summary["untaxed_total"]}
        )
    if script_tax is not None and summary.get("tax_amount") is not None:
        if abs(summary["tax_amount"] - script_tax) > _tol(script_tax):
            total_conflicts.append(
                {"level": "summary", "field": "tax_amount",
                 "script_value": script_tax, "llm_value": summary["tax_amount"]}
            )
    return {"item_conflicts": item_conflicts, "total_conflicts": total_conflicts}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _trace_attempt(ir: IR, attempt: dict, output: Any) -> None:
    """把该轮原始输出 + 校验结果落盘：{data_dir}/llm_trace/{file_hash}_r{n}.json。

    失败轮此前不留任何痕迹，事后只能靠猜复现（本次回归的教训）。写盘失败不影响主流程，
    设 LLM_TRACE=0 可完全关闭。
    """
    if os.environ.get("LLM_TRACE", "1") == "0":
        return
    try:
        LLM_TRACE_DIR.mkdir(parents=True, exist_ok=True)
        path = LLM_TRACE_DIR / f"{ir.file_hash[:8]}_r{attempt['round']}.json"
        path.write_text(
            json.dumps(
                {"source_file": ir.source_file, "file_hash": ir.file_hash,
                 "attempt": attempt, "output": output},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        pass


def _safe_l1(envelope: Any, ir: IR) -> list:
    """L1 溯源校验（容错）：L0 都没过的 payload 可能缺字段，校验器不允许拖垮主流程。"""
    if not isinstance(envelope, dict):
        return []
    try:
        return validate_l1_traceability(envelope, ir)
    except Exception:
        return []


def _safe_consistency(envelope: Any, ir: IR) -> list:
    """evidence 一致性诊断（容错）：只入档诊断，不参与重试路由。"""
    if not isinstance(envelope, dict):
        return []
    try:
        return validate_l1_evidence_consistency(envelope, ir)
    except Exception:
        return []


def _safe_diagnostics(run, fallback):
    """后置诊断（L2 勾稽 / 脚本侧对账）容错：产出只供记录与落库标记，不该让已通过
    L0+L1 的解析整体失败（曾因 IR 噪声触发的 ValueError 把一个文件判成 failed）。
    返回 (结果, 异常描述)；异常描述写入 attempts 与 parse_log 便于排查。"""
    try:
        return run(), None
    except Exception as e:
        return fallback, f"{type(e).__name__}: {e}"


def _attempt_kind(errors: list[str] | None, l1_issues: list) -> str:
    """该轮失败类型：schema_invalid / traceability / ok（写入 attempts 便于事后复盘）。"""
    if errors:
        return "schema_invalid"
    if l1_issues:
        return "traceability"
    return "ok"


def parse_ir_with_llm_traced(
    ir: IR | dict, *, conn: sqlite3.Connection | None = None, chat_fn=None, progress_cb=None
) -> tuple[dict, list[dict], dict]:
    """LLM 版面理解，返回 (信封 JSON {"offers": [...]}, LLM 调用尝试记录, cross_check 结果)。

    每轮：调 LLM → 信封兼容处理 → _self_check 存档 → L0 Draft7 校验（失败先剥除
    非法 null 重验一次）→ L0 通过再跑 L1 溯源校验（金额/坐标必须能在 evidence 与 IR
    中找到出处）。重试上限 3 轮：schema 类错误按 schema_invalid 反馈、JSON 解析失败按
    json_unparseable 反馈、L1 抽取错误（blame=A 且非 info 级）按 traceability 反馈；
    第 3 轮仍失败抛 LLMValidateError（带 attempts 与最终问题清单）。
    每轮的原始输出与校验结果都会落盘 llm_trace/{file_hash}_r{n}.json（LLM_TRACE=0 关闭）；
    若某轮输出与上一轮完全一致（模型对 retry 反馈零响应），立即终止，不再空烧剩余轮次。
    L1 通过（或仅 info 级）时返回，L2 勾稽问题（blame=B，只记录）写入 attempts
    末条记录的 l2_issues 字段，供 parse_log 追溯、不打断流程。

    progress_cb：可选进度回调，每次发起新一轮重试前调用一次，入参
    {"round": 即将开始的轮次, "reason": 触发重试的原因摘要}（pipeline 用它写
    layout/retry_round 进度日志）；回调异常被吞掉，绝不影响解析主流程。
    """
    def _emit_retry(round_no: int, kind: str, errors: list[str]) -> None:
        if progress_cb is None:
            return
        try:
            reason = f"{kind}: {(errors[0] if errors else '')[:160]}"
            progress_cb({"round": round_no + 1, "reason": reason})
        except Exception:
            pass

    if isinstance(ir, dict):
        ir = IR.from_dict(ir)
    fn = chat_fn or llm_client.chat_json
    categories = _load_categories(conn)
    messages = _build_messages(ir, categories)
    prompt_version = prompts.get_prompt_version("parse_quote")

    attempts: list[dict] = []
    last_errors: list[str] = []
    prev_signature: str | None = None  # 上一轮成功解析出的输出指纹（探测"零进展"重试）
    stalled = False
    for round_no in range(1, MAX_ATTEMPTS + 1):
        try:
            parsed, usage = fn(messages)
        except LLMUnavailable:
            raise
        except LLMError as e:
            last_errors = [str(e)]
            attempt = {"round": round_no, "usage": None, "kind": "json_unparseable",
                       "validation_errors": last_errors, "l1_issues": None,
                       "prompt_version": prompt_version}
            attempts.append(attempt)
            _trace_attempt(ir, attempt, None)
            if round_no < MAX_ATTEMPTS:
                _emit_retry(round_no, "json_unparseable", last_errors)
                messages = messages + prompts.build_retry_messages("json_unparseable", last_errors)
            continue
        if isinstance(parsed, dict) and "offers" not in parsed and "unit_price" in parsed:
            parsed = {"offers": [parsed]}  # 兼容旧格式：无信封层的单个 quote 对象
        _archive_self_check(parsed)
        errors = _validate(parsed)
        if errors:
            for offer in parsed.get("offers") or []:
                _strip_disallowed_nulls(offer, load_schema())
            errors = _validate(parsed)
        # L1 无论 L0 是否通过都算：schema 报错会掩盖真正的首因（如"填 0 占位"背后的编造金额），
        # 只有把溯源问题一并入档，事后复盘才看得到真正原因；路由逻辑不变（仍按 blame=A 过滤）。
        l1_all = _safe_l1(parsed, ir)
        l1_issues = [i for i in l1_all if i.blame == "A" and i.level != "info"]
        l1_warnings = [
            i.to_dict() for i in l1_all if i.level != "error"
        ] + [
            i.to_dict() for i in _safe_consistency(parsed, ir)
        ]
        attempt = {"round": round_no, "usage": usage,
                   "kind": _attempt_kind(errors, l1_issues),
                   "validation_errors": errors or None,
                   "l1_issues": [issue.to_dict() for issue in l1_issues] or None,
                   "l1_warnings": l1_warnings or None,
                   "prompt_version": prompt_version}
        attempts.append(attempt)
        _trace_attempt(ir, attempt, parsed)
        if not errors and not l1_issues:
            l2_issues, l2_error = _safe_diagnostics(
                lambda: validate_l2_reconcile(parsed), []
            )
            cross, cross_error = _safe_diagnostics(
                lambda: cross_check(ir, parsed), {"item_conflicts": 0, "total_conflicts": []}
            )
            diag_error = l2_error or cross_error
            attempts[-1]["l2_issues"] = [issue.to_dict() for issue in l2_issues] or None
            attempts[-1]["diagnostic_error"] = diag_error
            if diag_error:  # 诊断失败只记录：解析结果本身已通过 L0+L1
                cross = dict(cross or {})
                cross["diagnostic_error"] = diag_error
            return parsed, attempts, cross
        if errors:
            last_errors = errors
            kind = "schema_invalid"
        else:
            last_errors = [f"{issue.path}: {issue.issue}，{issue.detail}" for issue in l1_issues]
            kind = "traceability"
        if round_no == MAX_ATTEMPTS:
            break
        signature = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        if signature == prev_signature:
            # 模型原样吐回上一轮结果：retry 反馈没起作用，再烧轮次也只是重复同样的输出
            stalled = True
            break
        prev_signature = signature
        _emit_retry(round_no, kind, last_errors)
        messages = messages + prompts.build_retry_messages(kind, last_errors)

    summary = "；".join(
        f"第{a['round']}轮 {a.get('kind', '?')}"
        f"(schema {len(a['validation_errors'] or [])} 项，溯源 {len(a['l1_issues'] or [])} 项)"
        for a in attempts
    )
    raise LLMValidateError(
        f"LLM 版面理解输出 {MAX_ATTEMPTS} 次均不符合 schema/溯源校验，最后错误：\n"
        + "\n".join(last_errors[:10])
        + (f"\n{len(attempts)} 轮输出与上一轮完全一致（模型无进展），已提前终止。" if stalled else "")
        + f"\n各轮情况：{summary}"
        + f"\n逐轮原始输出见 {LLM_TRACE_DIR}（LLM_TRACE=0 可关闭）。",
        attempts,
    )


def parse_ir_with_llm(ir: IR | dict, *, conn: sqlite3.Connection | None = None, chat_fn=None) -> dict:
    """LLM 版面理解 → 信封 JSON {"offers": [...]}，单个 offer 与 simple_excel_parse.parse_ir 输出同构。"""
    data, _attempts, _cross = parse_ir_with_llm_traced(ir, conn=conn, chat_fn=chat_fn)
    return data

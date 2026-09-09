"""LLM 版面理解：IR → quote_schema v1.1 JSON + 品类判定 + 脚本交叉验证。

主入口 parse_ir_with_llm（返回纯 quote JSON）与 parse_ir_with_llm_traced
（额外返回 LLM 调用尝试记录与 cross_check 结果，pipeline 记 parse_log 用）。

流程：IR 紧凑序列化 → 压缩版 schema prompt（含品类清单）→ chat_json →
Draft7 校验，失败把错误列表 append 进对话重试 1 次，仍失败抛 LLMUnavailable。
成功后跑 cross_check：独立脚本侧解析（自带简化关键词分类器，不复用规则解析器）
与 LLM 条目按 evidence.location 行号对账，差异写 _cross_check 供 persist 落库。
"""

import re
import sqlite3
from typing import Any

from jsonschema import Draft7Validator

from app.ir import IR
from app.llm import client as llm_client
from app.llm.client import LLMError, LLMUnavailable
from app.normalize import normalize_amount
from app.validate.validate import MODULES, load_schema

FALLBACK_ATOM = "AT-QT-001"

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

_SCHEMA_BRIEF = """把报价单解析为 JSON，严格符合 quote_schema v1.1（压缩版字段说明）：

顶层（required 标*）：
- schema_version: "1.1"
- supplier*: {supplier_name*: 字符串, supplier_code: null}
- basic*: {project_name: 字符串|null, part_name*: 字符串, material_spec: 字符串|null,
  quote_date: "YYYY-MM-DD"|null, currency*: 枚举(CNY/USD/EUR/JPY/HKD/TWD/KRW/OTHER，判断不了填CNY),
  moq: 数字|null, category: 品类编码|null（见下方品类清单）, quote_no: 字符串|null,
  source_file: 字符串|null, parse_status: "parsed"}
- unit_price*: {materials*, processing*, inspection*, packaging_transport*, sga_tax*, other*, summary*}
- tooling: 对象|null（无模治具费用时填 null）

金额单位：unit_price 下所有金额 = 元/pcs；tooling 下 = 元/项（一次性费用）。
六费用模块结构均为 {total: 数字|null, items: [...]}。供应商只报模块总价时 total 填数、items 为空数组。

各模块 items 字段（required 标*）：
- materials: {name*, amount_per_pc*, spec: 字符串|null, note: 字符串|null, evidence}
- processing: {name*, amount_per_pc*, atom_code: null, is_new_process: false, bundle_flag: false,
  confidence: "low", match_path: null, confirm_status: "unconfirmed", note: 字符串|null, evidence}
  （工艺原子映射是后续阶段的事：atom_code 一律 null、confidence 一律 "low"、match_path 一律 null；
  bundle_members/bundle_fingerprint/split_method 本次不要输出该字段）
- inspection: {name*, amount_per_pc*, note: 字符串|null, evidence}
- packaging_transport: {name*, amount_per_pc*, item_type*: "包装"|"运输", note: 字符串|null, evidence}
- sga_tax: {name*, amount_per_pc*, item_type*: "损耗"|"管理费"|"利润"|"税费"|"其他",
  rate: 数字|null（费率小数，0.13=13%；税费条目必填）, note: 字符串|null, evidence}
- other: {name*, amount_per_pc*, note*: 字符串（必填，说明这是什么费用）, evidence}

evidence（每个条目必填）: {"file": 源文件名, "location": "sheet名!单元格范围"（如 "报价单!B9:C9"）, "raw_text": 原文片段}

summary*: {untaxed_total: 数字|null, tax_amount: 数字|null, taxed_total: 数字|null,
  discount: 数字|null, final_unit_price_taxed*: 数字, calc_check: "unchecked"}
勾稽规则：未税合计 = Σ各模块合计（排除税费）；含税合计 = 未税合计 + 税额；最终含税单价 = 含税合计 − 折扣。
按此规则计算并填 summary；算不准时 final_unit_price_taxed 必须给最优估计值。

tooling: {total: 数字|null, molds/fixtures/stencils: {total: 数字|null, items: [
  {name*, amount*, cavities: 整数|null（穴数）, lifespan: 数字|null（寿命模次）, note: 字符串|null, evidence}]}}
模具→molds，治具/检具/夹具→fixtures，钢网/网板→stencils。"""

_JSON_EXAMPLE = """输出示例（结构示意，字段以实际内容为准）：
{"schema_version":"1.1","supplier":{"supplier_name":"XX公司","supplier_code":null},
"basic":{"project_name":null,"part_name":"某零件","material_spec":null,"quote_date":"2026-09-01",
"currency":"CNY","moq":null,"category":"CAT-WJWK","quote_no":null,"source_file":"报价单.xlsx","parse_status":"parsed"},
"unit_price":{"materials":{"total":1.0,"items":[{"name":"铝材","amount_per_pc":1.0,"spec":null,"note":null,
"evidence":{"file":"报价单.xlsx","location":"Sheet1!B2:C2","raw_text":"铝材 1.0"}}]},
"processing":{"total":null,"items":[]},"inspection":{"total":null,"items":[]},
"packaging_transport":{"total":null,"items":[]},"sga_tax":{"total":null,"items":[]},
"other":{"total":null,"items":[]},
"summary":{"untaxed_total":null,"tax_amount":null,"taxed_total":null,"discount":null,
"final_unit_price_taxed":1.0,"calc_check":"unchecked"}},"tooling":null}"""


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
    cat_lines = " / ".join(f"{code} {name}" for code, name in categories)
    user = f"""{_SCHEMA_BRIEF}

品类判定：根据 part_name / material_spec / 报价内容判断零件所属品类，basic.category 填下方清单中的编码，都拿不准填 null：
{cat_lines}

报价单原文（IR 格式：sheet|行号|列号:值|列号:值...）：
{_serialize_ir(ir)}

只输出一个 JSON 对象，不要输出任何其他文字，不要用 markdown 代码块包裹。

{_JSON_EXAMPLE}"""
    return [
        {"role": "system", "content": "你是采购报价单结构化解析助手，只输出 JSON。"},
        {"role": "user", "content": user},
    ]


def _validate(data: Any) -> list[str]:
    validator = Draft7Validator(load_schema())
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    return [
        f"$.{'.'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors[:10]
    ]


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

_LOCATION_RE = re.compile(r"([A-Za-z0-9_\u4e00-\u9fff]+)!?[A-Z]{0,3}(\d+)(?::[A-Z]{0,3}(\d+))?")

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


def _script_parse(ir: IR) -> tuple[dict, dict, dict]:
    """脚本侧独立解析：返回 (行号→该行金额列表, 模块→明细和, 汇总标签→金额)。"""
    row_amounts: dict[tuple[str, int], list[float]] = {}
    module_sums: dict[str, float] = {m: 0.0 for m in (*MODULES, "tooling")}
    summary_seen: dict[str, float] = {}
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
    return row_amounts, module_sums, summary_seen


def _iter_amount_items(data: dict):
    """产出 (层级路径, 金额字段名, item dict)，供行级对账。"""
    up = data.get("unit_price") or {}
    for module in MODULES:
        for item in (up.get(module) or {}).get("items") or []:
            yield module, "amount_per_pc", item
    tooling = data.get("tooling")
    if tooling:
        for key in ("molds", "fixtures", "stencils"):
            for item in (tooling.get(key) or {}).get("items") or []:
                yield f"tooling.{key}", "amount", item


def _parse_location(location: str | None) -> tuple[str, int] | None:
    if not location:
        return None
    m = _LOCATION_RE.search(location)
    if not m:
        return None
    row = int(m.group(2))
    return m.group(1), row


def cross_check(ir: IR | dict, data: dict) -> dict:
    """脚本侧独立对账：LLM 条目金额 vs IR 原文行金额；模块合计/summary 层级差异计入 total_conflicts。

    行级差异 > max(0.01, |值|×1%) 的条目加 _cross_check={"script_value":x,"llm_value":y}。
    返回 {"item_conflicts": n, "total_conflicts": [...]}。
    """
    if isinstance(ir, dict):
        ir = IR.from_dict(ir)
    row_amounts, module_sums, summary_seen = _script_parse(ir)
    up = data.get("unit_price") or {}

    item_conflicts = 0
    for module, amount_key, item in _iter_amount_items(data):
        value = item.get(amount_key)
        if value is None:
            continue
        loc = _parse_location((item.get("evidence") or {}).get("location"))
        if loc is None:
            continue
        script_amounts = row_amounts.get(loc[0], []) if loc else []
        # location 只带单行时允许跨 sheet 回退：按行号在所有 sheet 中找
        if not script_amounts:
            script_amounts = next(
                (v for (sheet, row), v in row_amounts.items() if row == loc[1]), []
            )
        if not script_amounts:
            continue
        if any(abs(value - a) <= _tol(value) for a in script_amounts):
            continue
        script_value = min(script_amounts, key=lambda a: abs(a - value))
        item["_cross_check"] = {"script_value": script_value, "llm_value": value}
        item_conflicts += 1

    total_conflicts: list[dict] = []
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

def parse_ir_with_llm_traced(
    ir: IR | dict, *, conn: sqlite3.Connection | None = None, chat_fn=None
) -> tuple[dict, list[dict], dict]:
    """LLM 版面理解，返回 (quote JSON, LLM 调用尝试记录, cross_check 结果)。

    校验失败重试 1 次；仍失败抛 LLMValidateError（带 attempts 详情）。
    """
    if isinstance(ir, dict):
        ir = IR.from_dict(ir)
    fn = chat_fn or llm_client.chat_json
    categories = _load_categories(conn)
    messages = _build_messages(ir, categories)

    attempts: list[dict] = []
    last_errors: list[str] = []
    for round_no in (1, 2):
        try:
            parsed, usage = fn(messages)
        except LLMUnavailable:
            raise
        except LLMError as e:
            last_errors = [str(e)]
            attempts.append({"round": round_no, "usage": None, "validation_errors": last_errors})
            if round_no == 2:
                break
            messages = messages + [
                {"role": "assistant", "content": "（上一次输出无法解析，见下方错误）"},
                {"role": "user", "content": "上次输出有误，请修正后重新输出完整 JSON：\n" + "\n".join(last_errors)},
            ]
            continue
        errors = _validate(parsed)
        if errors:
            _strip_disallowed_nulls(parsed, load_schema())
            errors = _validate(parsed)
        attempts.append({"round": round_no, "usage": usage, "validation_errors": errors or None})
        if not errors:
            cross = cross_check(ir, parsed)
            return parsed, attempts, cross
        last_errors = errors
        if round_no == 1:
            messages = messages + [
                {"role": "assistant", "content": "（见下方校验错误）"},
                {"role": "user", "content":
                    "你的输出不符合 quote_schema v1.1，校验错误如下。请修正后重新输出完整 JSON（只输出 JSON）：\n"
                    + "\n".join(errors)},
            ]

    raise LLMValidateError(
        f"LLM 版面理解输出 2 次均不符合 quote_schema v1.1，最后错误：\n" + "\n".join(last_errors[:10]),
        attempts,
    )


def parse_ir_with_llm(ir: IR | dict, *, conn: sqlite3.Connection | None = None, chat_fn=None) -> dict:
    """LLM 版面理解 → quote_schema v1.1 JSON（与 simple_excel_parse.parse_ir 输出同构）。"""
    data, _attempts, _cross = parse_ir_with_llm_traced(ir, conn=conn, chat_fn=chat_fn)
    return data

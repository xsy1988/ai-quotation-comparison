"""分层校验器框架：统一 Issue 问题对象 + L0 结构 / L1 溯源 / L2 勾稽 三层校验。

层级与 blame 路由：
- L0 结构（validate_l0_schema）：Draft7 schema 校验包装，结构错误 blame=A；
  信封结构有任一问题时短路（深层校验无意义）。
- L1 溯源（validate_l1_traceability，ir 必传）：金额必须能在条目自身 evidence.raw_text
  中找到（词边界口径，"0" 不会误中 "13.0"）、坐标必须落在 IR 单元格范围内；抽取错误
  blame=A，可反馈 LLM 重抽取。金额溯源另有 IR 兜底：raw_text 不是原文（坐标 / IR 序列化
  片段 → raw_text_not_verbatim）、或金额在单据任何单元格中都不存在（amount_not_in_ir）
  时告警，用于抓住「raw_text 与金额一起被编造」的循环论证。例外：税费金额无出处但 rate
  非空时脚本可兜底派生，金额强制置 null 并降级 warning（blame=B，不触发重试）；非税费 0
  金额豁免出处校验。evidence_location_mismatch 是独立函数 validate_l1_evidence_consistency
  的 warning（只诊断，不进重试清单）。
- L2 勾稽（validate_l2_reconcile）：模块 total≠Σitems、summary 不闭合、税费≠税率×未税、
  疑似同值重复；计算/勾稽类 blame=B，本期只记录不触发重试（P4 接 LLM-B）。

L1/L2 金额口径与 derive 一致：Σitems 按共享单元格去重后计算，避免对去重场景误报。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from jsonschema import Draft7Validator

from app.derive import _has_provenance_in_ir, _ir_numeric_values, _shared_group_decision, _shared_group_keeper
from app.ir import IR
from app.normalize import amount_in_text
from app.validate.validate import (
    MODULES,
    iter_offers,
    load_envelope_schema,
    load_schema,
)

TAX_ITEM_TYPE = "税费"
"""sga_tax 中税费条目的 item_type（与 derive.TAX_ITEM_TYPE 同义）。"""

LEVEL_ERROR = "error"
"""会触发重试的问题级别。missing_evidence 为 info、疑似同值重复为 warning，均不触发。"""


@dataclass
class Issue:
    """统一问题对象（JSON 可序列化，to_dict 后即 plain dict）。

    blame：A=抽取错误（可反馈重抽取）；B=计算/勾稽（P4 接 LLM-B，本期只记录）。
    level：error 参与重试触发；info/warning 只记录。
    """

    path: str
    issue: str
    detail: str
    expected: Any = None
    actual: Any = None
    evidence: dict[str, Any] = field(default_factory=dict)
    blame: str = "A"
    level: str = LEVEL_ERROR

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# L0 结构校验
# ---------------------------------------------------------------------------

def validate_l0_schema(envelope: Any) -> list[Issue]:
    """L0 结构校验：信封 Draft7 + 逐 offer Draft7，错误转 Issue（blame=A）。

    行为与 layout_understand._validate 一致：信封层有错即跳过逐 offer 校验，最多报 10 条。
    """
    issues: list[Issue] = []
    for e in sorted(
        Draft7Validator(load_envelope_schema()).iter_errors(envelope),
        key=lambda e: list(e.absolute_path),
    ):
        issues.append(Issue(
            path="$." + ".".join(str(p) for p in e.absolute_path),
            issue="schema_invalid",
            detail=e.message,
            expected="符合 quote_schema v1.1",
            blame="A",
        ))
    if not issues and isinstance(envelope, dict):
        for i, offer in enumerate(envelope.get("offers") or []):
            for e in sorted(
                Draft7Validator(load_schema()).iter_errors(offer),
                key=lambda e: list(e.absolute_path),
            ):
                issues.append(Issue(
                    path=f"$.offers[{i}]." + ".".join(str(p) for p in e.absolute_path),
                    issue="schema_invalid",
                    detail=e.message,
                    expected="符合 quote_schema v1.1",
                    blame="A",
                ))
    return issues[:10]


# ---------------------------------------------------------------------------
# evidence.location 解析与坐标存在性
# ---------------------------------------------------------------------------

_RC_LOCATION_RE = re.compile(r"^R(\d+)C(\d+)(?::R(\d+)C(\d+))?$", re.IGNORECASE)
_A1_LOCATION_RE = re.compile(r"^([A-Za-z]{1,3})(\d+)(?::([A-Za-z]{1,3})(\d+))?$")


def _col_letters_to_index(letters: str) -> int:
    """Excel 列字母 → 列号（A→1，B→2，AA→27）。"""
    index = 0
    for ch in letters.upper():
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index


def parse_location(location: str | None) -> tuple[str | None, int, int, int, int] | None:
    """解析 evidence.location → (sheet, 起始行, 结束行, 起始列, 结束列)。

    支持 'sheet!R4C24' 与 'sheet!R9C3:R9C7'（RC 式单格/区间，sheet 名可含空格与括号，
    如 'CNC5分钟 (2)!R9C3:R9C7'）与 'sheet!B7:C7' / 'B7'（A1 式，可带单元格范围）；
    sheet 缺省返回 None 的 sheet 位。解析失败返回 None。
    """
    if not location:
        return None
    sheet: str | None = None
    cell = location.strip()
    if "!" in cell:
        sheet, cell = cell.split("!", 1)
        sheet = sheet or None
    m = _RC_LOCATION_RE.match(cell)
    if m:
        row1, col1 = int(m.group(1)), int(m.group(2))
        if m.group(3):
            row2, col2 = int(m.group(3)), int(m.group(4))
            return sheet, min(row1, row2), max(row1, row2), min(col1, col2), max(col1, col2)
        return sheet, row1, row1, col1, col1
    m = _A1_LOCATION_RE.match(cell)
    if m:
        col1 = _col_letters_to_index(m.group(1))
        row1 = int(m.group(2))
        if m.group(3):
            col2, row2 = _col_letters_to_index(m.group(3)), int(m.group(4))
            return sheet, min(row1, row2), max(row1, row2), min(col1, col2), max(col1, col2)
        return sheet, row1, row1, col1, col1
    return None


_IR_SERIAL_TOKEN_RE = re.compile(r"(?:^|\|)\s*\d+\s*:")
_COORD_ONLY_RES = (
    re.compile(r"(?:[^!]{0,40}!)?R\d+C\d+(?:\s*[::\-~]\s*(?:[^!]{0,40}!)?R\d+C\d+)?", re.IGNORECASE),
    re.compile(r"\d+\s*:\s*\d+"),
)


def _looks_like_ir_serialization(text: str) -> bool:
    """是否是 IR 序列化片段（如 "8:1|9:28|10:0.96"）：≥2 个 "数字:" 片段且以 | 分隔。"""
    if "|" not in text:
        return False
    return len(_IR_SERIAL_TOKEN_RE.findall(text)) >= 2


def _raw_text_not_verbatim(raw_text: str, location: Any) -> str | None:
    """raw_text 是否明显不是所引单元格的原文（而是坐标 / IR 序列化片段）。

    返回原因文本，看不出问题返回 None。用于堵住「raw_text 由模型自己编造」的循环论证漏洞：
    只要 raw_text 不是原文，后面拿它做金额溯源就没有意义。
    """
    text = raw_text.strip()
    if isinstance(location, str) and text and text == location.strip():
        return "raw_text 与 evidence.location 完全相同，填的是坐标而不是单元格原文"
    if _looks_like_ir_serialization(text):
        return "raw_text 是 IR 序列化片段（列号:值 用 | 拼接），不是单元格原文"
    if any(pattern.fullmatch(text) for pattern in _COORD_ONLY_RES):
        return "raw_text 只有行列坐标，不是单元格原文"
    return None


def _ir_texts(ir: IR) -> str:
    """IR 全部单元格文本 + 文本块拼接：模型无法伪造的证据来源（用于「数字印在长文本里」的场景）。"""
    parts: list[str] = []
    for table in ir.tables:
        parts.extend(str(c.value) for c in table.cells if c.value is not None and c.value != "")
    parts.extend(block.text for block in ir.blocks if block.text)
    return "\n".join(parts)


def _amount_in_document(ir: IR, value: float) -> bool:
    """金额是否真实存在于单据中（IR 是模型无法伪造的证据源）。

    IR 无任何可比对内容（如纯图片、无文本层）时返回 True：无法判定就不判定，避免误杀。
    """
    text = _ir_texts(ir)
    if not text.strip():
        return True
    if _has_provenance_in_ir(_ir_numeric_values(ir), value):
        return True
    return amount_in_text(text, value)


def _sheet_ranges(ir: IR) -> dict[str, tuple[int, int]]:
    """IR → {sheet: (最大行号, 最大列号)}（以实际出现过的单元格为准）。"""
    ranges: dict[str, list[int]] = {}
    for table in ir.tables:
        cols = [c.col for c in table.cells]
        if not cols:
            continue
        current = ranges.setdefault(table.sheet, [0, 0])
        current[0] = max(current[0], table.row_number)
        current[1] = max(current[1], max(cols))
    return {sheet: (rows, cols) for sheet, (rows, cols) in ranges.items()}


def _location_issues(
    path: str, location: str, ranges: dict[str, tuple[int, int]], evidence: dict[str, Any]
) -> list[Issue]:
    """坐标存在性：sheet 必须在 IR 中、行/列必须在单元格范围内 → invalid_location（blame=A）。"""
    parsed = parse_location(location)
    if parsed is None:
        return [Issue(
            path=path, issue="invalid_location",
            detail=f"坐标『{location}』格式无法解析", evidence=evidence, blame="A",
        )]
    sheet, row_min, row_max, col_min, col_max = parsed
    if sheet is None:
        if len(ranges) != 1:
            return [Issue(
                path=path, issue="invalid_location",
                detail=f"坐标『{location}』未指明 sheet，IR 含多个 sheet 无法定位（IR 中 sheet：{'、'.join(ranges)}）",
                evidence=evidence, blame="A",
            )]
        sheet = next(iter(ranges))
    if sheet not in ranges:
        known = "、".join(ranges)
        if len(ranges) == 1:
            # 单 sheet 单据：sheet 名写错（如占位词 "sheet"）不构成歧义，脚本就地修正为
            # IR 真实 sheet 名并降级提示，不触发 LLM 重试
            actual = next(iter(ranges))
            evidence["location"] = f"{actual}!{location.split('!', 1)[1]}"
            return [Issue(
                path=path, issue="invalid_location",
                detail=f"坐标『{location}』的 sheet 名写错，已按唯一 sheet『{actual}』修正",
                evidence=evidence, blame="B", level="warning",
            )]
        return [Issue(
            path=path, issue="invalid_location",
            detail=f"坐标『{location}』的 sheet『{sheet}』不存在于 IR（IR 中 sheet：{known}）",
            evidence=evidence, blame="A",
        )]
    max_row, max_col = ranges[sheet]
    if row_min < 1 or row_max > max_row or col_min < 1 or col_max > max_col:
        return [Issue(
            path=path, issue="invalid_location",
            detail=f"坐标『{location}』超出 sheet『{sheet}』单元格范围（行 1~{max_row}，列 1~{max_col}）",
            evidence=evidence, blame="A",
        )]
    return []


# ---------------------------------------------------------------------------
# L1 溯源校验
# ---------------------------------------------------------------------------

def _iter_amount_items(offer: dict) -> Iterator[tuple[str, int, str, dict]]:
    """产出 (模块路径, 条目索引, 金额字段名, 条目 dict)：unit_price 六模块 + tooling 三类。"""
    up = offer.get("unit_price") or {}
    for module in MODULES:
        items = (up.get(module) or {}).get("items") or []
        for index, item in enumerate(items):
            yield f"unit_price.{module}", index, "amount_per_pc", item
    tooling = offer.get("tooling") or {}
    for key in ("molds", "fixtures", "stencils"):
        items = (tooling.get(key) or {}).get("items") or []
        for index, item in enumerate(items):
            yield f"tooling.{key}", index, "amount", item


def validate_l1_traceability(envelope: dict, ir: IR | dict) -> list[Issue]:
    """L1 溯源校验（ir 必传，IR 对象或 dict）：

    1. 金额溯源：amount_per_pc/amount 非 null 的金额写法（2.01/2.1/2.10 变体，词边界口径
       复用 normalize.amount_in_text，避免 "0" 误中 "13.0"）必须出现在条目自身
       evidence.raw_text 中；找不到 → amount_not_in_evidence（blame=A）；非税费条目
       金额为 0 时豁免（0 在单据里到处出现，出处比对必然误报）。税费条目特例 →
       tax_amount_not_in_evidence：rate 非空时脚本可按 税率×未税 兜底派生，不再硬失败——
       amount 强制置 null 并降级 warning（blame=B，继续流程）；rate 也为空时才硬失败。
    1b. IR 兜底（raw_text 由模型自己写，单看它是循环论证）：raw_text 明显不是单元格原文
       （坐标 / IR 序列化片段）→ raw_text_not_verbatim；金额在单据任何单元格中都不存在
       → amount_not_in_ir（编造值，含自作算术）；金额存在但在 raw_text 里没写对时，
       amount_not_in_evidence 的 detail 会说明「疑似抄错单元格」还是「单据中不存在」。
    2. 坐标存在性：evidence.location 的 sheet 必须存在于 IR、行/列在单元格范围内
       → invalid_location（blame=A）。
    3. evidence 缺失：无 evidence 或 raw_text 为空 → missing_evidence（info 级，只记录）。
    """
    if isinstance(ir, dict):
        ir = IR.from_dict(ir)
    ranges = _sheet_ranges(ir)
    issues: list[Issue] = []
    for offer_index, offer in enumerate(iter_offers(envelope)):
        for module_path, item_index, amount_key, item in _iter_amount_items(offer):
            base_path = f"offers[{offer_index}].{module_path}.items[{item_index}]"
            evidence = item.get("evidence")
            raw_text = (evidence or {}).get("raw_text")
            location = (evidence or {}).get("location")
            issue_evidence = {"location": location, "raw_text": raw_text}
            if not evidence or not raw_text:
                issues.append(Issue(
                    path=base_path, issue="missing_evidence", level="info",
                    detail="条目缺少 evidence 或 raw_text 为空，无法溯源",
                    evidence=issue_evidence, blame="A",
                ))
            elif location:
                issues.extend(_location_issues(base_path, location, ranges, evidence))
            amount = item.get(amount_key)
            if amount is None or isinstance(amount, bool) or not raw_text:
                continue
            value = float(amount)
            is_tax = module_path == "unit_price.sga_tax" and item.get("item_type") == TAX_ITEM_TYPE
            verbatim_problem = _raw_text_not_verbatim(raw_text, location)
            if verbatim_problem:
                # raw_text 不是原文 → 金额溯源本身失去意义，先报原文问题（可反馈重抽取）
                issues.append(Issue(
                    path=f"{base_path}.evidence.raw_text", issue="raw_text_not_verbatim",
                    detail=f"{verbatim_problem}：『{raw_text}』",
                    expected="所引用单元格的原文文本（照抄内容，不要写坐标或 IR 序列化片段）",
                    actual=raw_text, evidence=issue_evidence, blame="A",
                ))
                if not is_tax and value != 0 and not _amount_in_document(ir, value):
                    # 原文不可信 + 全部单元格里都没有这个数 → 编造值（含自行算术推导）
                    issues.append(Issue(
                        path=f"{base_path}.{amount_key}", issue="amount_not_in_ir",
                        detail=f"金额 {value} 不在单据任何单元格中（单据未印出的金额应填 null，"
                               f"禁止自行相乘/相加）",
                        expected="单据上印出的金额，找不到则填 null", actual=value,
                        evidence=issue_evidence, blame="A",
                    ))
                continue
            if amount_in_text(raw_text, value):
                if not is_tax and value != 0 and not _amount_in_document(ir, value):
                    # 数字在 raw_text 里、却不在单据里：raw_text 连同金额一起被编造
                    issues.append(Issue(
                        path=f"{base_path}.{amount_key}", issue="amount_not_in_ir",
                        detail=f"金额 {value} 虽出现在 evidence.raw_text『{raw_text}』中，"
                               f"但不在单据任何单元格中，raw_text 与金额均不可信",
                        expected="单据上印出的金额，找不到则填 null", actual=value,
                        evidence=issue_evidence, blame="A",
                    ))
                continue
            if not is_tax and value == 0:
                continue  # 0 在单据里到处出现，豁免金额出处校验（双保险，词边界已过滤大半）
            if is_tax and item.get("rate") is not None:
                # 脚本能按 税率×未税 兜底派生：不再硬失败，amount 强制置 null，降级 warning
                # （blame=B 只记录不触发重试，流程继续交给 derive 派生）
                item[amount_key] = None
                issues.append(Issue(
                    path=f"{base_path}.{amount_key}", issue="tax_amount_not_in_evidence",
                    detail=f"税费金额 {value} 未出现在 evidence.raw_text『{raw_text}』中，"
                           f"已置 null，由脚本按税率×未税派生",
                    expected="null（由脚本按税率×未税派生）", actual=value,
                    evidence=issue_evidence, blame="B", level="warning",
                ))
            elif is_tax:
                issues.append(Issue(
                    path=f"{base_path}.{amount_key}", issue="tax_amount_not_in_evidence",
                    detail=f"税费金额 {value} 未出现在 evidence.raw_text『{raw_text}』中，"
                           f"该格只有税率时应填 null，由脚本按税率×未税派生",
                    expected="null（由脚本按税率×未税派生）", actual=value,
                    evidence=issue_evidence, blame="A",
                ))
            else:
                elsewhere = _amount_in_document(ir, value)
                issues.append(Issue(
                    path=f"{base_path}.{amount_key}", issue="amount_not_in_evidence",
                    detail=f"金额 {value} 未出现在 evidence.raw_text『{raw_text}』中"
                           + ("（该数字在单据其它单元格中存在，疑似抄错单元格）" if elsewhere else
                              "（该数字在单据任何单元格中都不存在，属编造值：单据未印出的金额应填 null，"
                              "禁止自行相乘/相加）"),
                    expected="raw_text 中出现的金额，找不到则填 null",
                    actual=value, evidence=issue_evidence, blame="A",
                ))
    return issues


def validate_l1_evidence_consistency(envelope: dict, ir: IR | dict) -> list[Issue]:
    """evidence 一致性（诊断用，warning/blame=B，不影响流程、不触发重试）：

    raw_text 与所引 location 的单元格原文不符时提示。单独成函数是为了不污染
    validate_l1_traceability 的错误清单（该清单参与重试路由，语义必须保持稳定）。
    """
    if isinstance(ir, dict):
        ir = IR.from_dict(ir)
    cell_text: dict[tuple[str, int, int], str] = {}
    for table in ir.tables:
        for cell in table.cells:
            if cell.value is not None and cell.value != "":
                cell_text[(table.sheet, table.row_number, cell.col)] = str(cell.value)
    issues: list[Issue] = []
    for offer_index, offer in enumerate(iter_offers(envelope)):
        for module_path, item_index, amount_key, item in _iter_amount_items(offer):
            evidence = item.get("evidence") or {}
            raw_text = evidence.get("raw_text")
            location = evidence.get("location")
            if not raw_text or not location:
                continue
            parsed = parse_location(location)
            if parsed is None:
                continue
            sheet, row1, row2, col1, col2 = parsed
            sheets = [sheet] if sheet in ir.sheets else (ir.sheets if sheet is None else [])
            if len(sheets) != 1:
                continue
            text = raw_text.strip()
            cited = [
                cell_text[(sheets[0], row, col)]
                for row in range(row1, row2 + 1)
                for col in range(col1, col2 + 1)
                if (sheets[0], row, col) in cell_text
            ]
            if not cited or any(text == cell or text in cell or cell in text for cell in cited):
                continue
            if any(text in value or value in text for value in cell_text.values()):
                issues.append(Issue(
                    path=f"offers[{offer_index}].{module_path}.items[{item_index}]",
                    issue="evidence_location_mismatch", level="warning",
                    detail=f"evidence.raw_text『{raw_text}』与 location『{location}』单元格原文"
                           f"（{' / '.join(cited)}）不符，该原文出现在其它单元格",
                    expected="raw_text 照抄 location 所指单元格的原文",
                    actual=raw_text, evidence={"location": location, "raw_text": raw_text}, blame="B",
                ))
    return issues


# ---------------------------------------------------------------------------
# L2 勾稽校验（只记录，blame=B）
# ---------------------------------------------------------------------------

def _tolerance(expected: float) -> float:
    return max(0.01, abs(expected) * 0.01)


def _shared_cell_groups(offer: dict) -> dict[str, list[tuple[int, int, dict]]]:
    """跨模块按 evidence.location 分组（derive 规则 A 的只读版，不修改条目）。"""
    groups: dict[str, list[tuple[int, int, dict]]] = {}
    up = offer.get("unit_price") or {}
    for module_index, name in enumerate(MODULES):
        for item_index, item in enumerate((up.get(name) or {}).get("items") or []):
            location = (item.get("evidence") or {}).get("location")
            if location:
                groups.setdefault(location, []).append((module_index, item_index, item))
    return groups


def _shared_cell_zeroed_ids(offer: dict) -> set[int]:
    """共享单元格去重判定（derive 规则 A 同口径）→ 会被置零的条目 id 集合。"""
    zeroed: set[int] = set()
    for members in _shared_cell_groups(offer).values():
        if _shared_group_decision(members) != "dedupe":
            continue
        keeper = _shared_group_keeper(members)
        zeroed.update(id(m[2]) for m in members if m[2] is not keeper[2])
    return zeroed


def validate_l2_reconcile(envelope: dict, ir: Any = None) -> list[Issue]:
    """L2 勾稽校验（只记录不阻断，blame=B，结果供 P4 消费）。ir 本期仅作占位（P4 扩展用）。

    检查（金额口径同 derive：Σitems 按共享单元格去重后计算，模块无明细时回退 total）：
    - module_total_mismatch：模块 total 与去重后 Σitems 不符；
    - summary_not_closed：untaxed_total / taxed_total / final_unit_price_taxed 与重算值不符；
    - tax_rate_mismatch：税费金额与 税率×未税 不符；
    - possible_shared_cell：同 location 同金额但未满足去重判据（非恰好出现一次，或
      科目名出处/表头碎片判不准），疑似同值重复（warning）。
    """
    issues: list[Issue] = []
    untaxed_modules = tuple(m for m in MODULES if m != "sga_tax")
    for offer_index, offer in enumerate(iter_offers(envelope)):
        up = offer.get("unit_price")
        if not isinstance(up, dict):
            continue
        zeroed = _shared_cell_zeroed_ids(offer)
        items_by_module = {name: ((up.get(name) or {}).get("items") or []) for name in MODULES}
        prefix = f"offers[{offer_index}]"

        def _sum_items(name: str, tax_only: bool | None = None) -> float:
            total = 0.0
            for item in items_by_module[name]:
                if id(item) in zeroed:
                    continue
                if tax_only is True and item.get("item_type") != TAX_ITEM_TYPE:
                    continue
                if tax_only is False and item.get("item_type") == TAX_ITEM_TYPE:
                    continue
                amount = item.get("amount_per_pc")
                if amount is not None:
                    total += float(amount)
            return round(total, 6)

        def _module_sum(name: str) -> float:
            if items_by_module[name]:
                return _sum_items(name)
            total = (up.get(name) or {}).get("total")
            return round(float(total), 6) if total is not None else 0.0

        untaxed = round(
            sum(_module_sum(name) for name in untaxed_modules)
            + _sum_items("sga_tax", tax_only=False),
            6,
        )

        for name in MODULES:
            module = up.get(name) or {}
            total = module.get("total")
            items_sum = _sum_items(name)
            if (
                total is not None
                and items_by_module[name]
                and abs(float(total) - items_sum) > _tolerance(items_sum)
            ):
                issues.append(Issue(
                    path=f"{prefix}.unit_price.{name}.total", issue="module_total_mismatch",
                    detail=f"模块 total {total} 与去重后明细和 {items_sum} 不符",
                    expected=items_sum, actual=float(total), blame="B",
                ))

        tax_items = [
            item for item in items_by_module["sga_tax"]
            if item.get("item_type") == TAX_ITEM_TYPE
            and item.get("amount_per_pc") is not None
            and id(item) not in zeroed
        ]
        tax_amount = float(tax_items[0]["amount_per_pc"]) if tax_items else None
        if tax_amount is not None and tax_items[0].get("rate") is not None:
            expected_tax = round(float(tax_items[0]["rate"]) * untaxed, 6)
            if abs(tax_amount - expected_tax) > _tolerance(expected_tax):
                issues.append(Issue(
                    path=f"{prefix}.unit_price.sga_tax", issue="tax_rate_mismatch",
                    detail=f"税费金额 {tax_amount} 与 税率×未税 {expected_tax} 不符",
                    expected=expected_tax, actual=tax_amount, blame="B",
                ))

        summary = up.get("summary")
        if isinstance(summary, dict) and summary:
            def _field_issue(field: str, expected: float) -> None:
                actual = summary.get(field)
                if actual is not None and abs(float(actual) - expected) > _tolerance(expected):
                    issues.append(Issue(
                        path=f"{prefix}.unit_price.summary.{field}", issue="summary_not_closed",
                        detail=f"summary.{field} {actual} 与重算值 {round(expected, 6)} 不符",
                        expected=round(expected, 6), actual=float(actual), blame="B",
                    ))

            _field_issue("untaxed_total", untaxed)
            if tax_amount is not None:
                _field_issue("taxed_total", untaxed + tax_amount)
            taxed = summary.get("taxed_total")
            if taxed is not None:
                taxed_used = float(taxed)
            elif tax_amount is not None:
                taxed_used = untaxed + tax_amount
            else:
                taxed_used = untaxed
            _field_issue("final_unit_price_taxed", taxed_used - float(summary.get("discount") or 0))

        for location, members in _shared_cell_groups(offer).items():
            if len(members) < 2:
                continue
            amounts = [m[2].get("amount_per_pc") for m in members]
            if any(a is None for a in amounts) or len({float(a) for a in amounts}) != 1:
                continue
            if _shared_group_decision(members) == "dedupe":
                continue  # 判为真共享，derive 规则 A 会处理
            names = "、".join(str(m[2].get("name")) for m in members)
            issues.append(Issue(
                path=f"{prefix}.unit_price", issue="possible_shared_cell", level="warning",
                detail=f"条目 {names} 同引坐标『{location}』且金额相同 {amounts[0]}，"
                       f"但未满足去重判据（金额非恰好出现一次，或科目名出处/表头碎片判不准），"
                       f"疑似同值重复",
                actual=float(amounts[0]), evidence={"location": location}, blame="B",
            ))
    return issues


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def run_validators(
    envelope: dict,
    ir: IR | dict | None = None,
    levels: tuple[str, ...] = ("L0", "L1"),
) -> list[Issue]:
    """按序跑指定层级校验；L0 有任一 error 即短路返回（结构坏了深层校验无意义）。

    levels 含 L1/L2 时 ir 必传（L2 本期不用 ir，签名预留 P4 扩展）。
    """
    issues: list[Issue] = []
    for level in levels:
        if level == "L0":
            got = validate_l0_schema(envelope)
        elif level == "L1":
            got = validate_l1_traceability(envelope, ir)  # type: ignore[arg-type]
        elif level == "L2":
            got = validate_l2_reconcile(envelope, ir)
        else:
            raise ValueError(f"未知校验层级: {level}（可选：L0/L1/L2）")
        issues.extend(got)
        if level == "L0" and any(i.level == LEVEL_ERROR for i in got):
            break
    return issues

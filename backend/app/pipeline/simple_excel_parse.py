"""[INTERIM] 临时规则版式解析器：IR → quote_schema JSON。

仅识别 sample_quote.xlsx 一类版式（A 列分区/B 列条目名/C 列金额/D 列备注，关键词分区）。
第 5 步由 LLM 版面理解取代本模块。识别不了的行进 other（note 必填）或抛 ParseError，不硬猜。
"""

import re
from typing import Any

from app.ir import IR
from app.match.fee_classify import classify_item_type
from app.normalize import normalize_amount, normalize_currency, normalize_date
from app.validate.validate import MODULES, items_sum, module_total

SUMMARY_LABELS = {
    "未税合计": "untaxed_total",
    "税额": "tax_amount",
    "含税合计": "taxed_total",
    "折扣": "discount",
    "最终含税单价": "final_unit_price_taxed",
}

BASIC_LABELS = {
    "供应商": "supplier_name",
    "零件名称": "part_name",
    "材料规格": "material_spec",
    "报价日期": "quote_date",
    "币种": "currency",
    "项目名称": "project_name",
    "报价单号": "quote_no",
}

_SECTION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("materials", ("材料", "铝材", "钢材")),
    ("processing", ("加工", "工艺")),
    ("inspection", ("检验", "检测", "品检")),
    ("packaging_transport", ("包装", "运输", "包运", "物流")),
    ("sga_tax", ("损管利税", "损耗", "管理费", "利润", "税")),
    ("tooling", ("模治具", "模具", "治具", "检具", "钢网")),
]

_CAVITY_RE = re.compile(r"穴\s*数?\s*[:：]?\s*(\d+)")
_LIFESPAN_RE = re.compile(r"寿\s*命\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(万)?")


class ParseError(Exception):
    pass


def _cell(row: dict[int, Any], col: int) -> Any:
    return row.get(col)


def _row_cells(table_row) -> dict[int, Any]:
    return {c.col: c.value for c in table_row.cells if c.value is not None and c.value != ""}


def _classify_section(text: str) -> str | None:
    for module, keywords in _SECTION_RULES:
        if any(k in text for k in keywords):
            return module
    return None


def _tooling_type(name: str) -> str:
    if "钢网" in name or "网板" in name:
        return "stencil"
    if any(k in name for k in ("治具", "检具", "夹具")):
        return "fixture"
    return "mold"


def _evidence(ir: IR, sheet: str, row_number: int, name: str, amount: Any) -> dict:
    return {
        "file": ir.source_file,
        "location": f"{sheet}!B{row_number}:C{row_number}",
        "raw_text": f"{name} {amount}",
    }


def _new_module() -> dict:
    return {"total": None, "items": []}


def _parse_cavities_lifespan(note: str | None) -> tuple[int | None, int | None]:
    cavities: int | None = None
    lifespan: int | None = None
    if not note:
        return None, None
    m = _CAVITY_RE.search(note)
    if m:
        cavities = int(m.group(1))
    m = _LIFESPAN_RE.search(note)
    if m:
        lifespan = int(float(m.group(1)) * (10000 if m.group(2) else 1))
    return cavities, lifespan


def parse_ir(ir: IR | dict, supplier_name: str | None = None) -> dict:
    """IR → quote_schema v1.1 JSON。supplier_name 参数优先于表格内的供应商单元格。"""
    if isinstance(ir, dict):
        ir = IR.from_dict(ir)

    basic: dict[str, Any] = {
        "project_name": None,
        "part_name": None,
        "material_spec": None,
        "quote_date": None,
        "currency": None,
        "moq": None,
        "category": None,
        "quote_no": None,
        "source_file": ir.source_file,
        "parse_status": "parsed",
    }
    sheet_supplier: str | None = None
    modules: dict[str, dict] = {name: _new_module() for name in MODULES}
    other_total: float | None = None
    tooling: dict[str, Any] | None = None
    summary_seen: dict[str, float] = {}

    def ensure_tooling() -> dict:
        nonlocal tooling
        if tooling is None:
            tooling = {
                "total": None,
                "molds": _new_module(),
                "fixtures": _new_module(),
                "stencils": _new_module(),
            }
        return tooling

    for table_row in ir.tables:
        cells = _row_cells(table_row)
        if not cells:
            continue
        sheet, row_no = table_row.sheet, table_row.row_number
        a = _cell(cells, 1)
        b = _cell(cells, 2)
        c = _cell(cells, 3)
        d = _cell(cells, 4)
        a_text = str(a).strip() if a is not None else ""
        b_text = str(b).strip() if b is not None else ""
        amount = normalize_amount(c)

        # 表头行
        if a_text == "费用项目":
            continue

        # 基本信息行：A/B 与 C/D 各一组 标签+值
        handled_basic = False
        for label_col, value_col in ((a_text, b), (str(c).strip() if c is not None else "", d)):
            if label_col in BASIC_LABELS:
                field = BASIC_LABELS[label_col]
                if field == "supplier_name" and value_col is not None:
                    sheet_supplier = str(value_col).strip()
                    handled_basic = True
                elif field in basic and value_col is not None:
                    basic[field] = str(value_col).strip()
                    handled_basic = True
        if handled_basic:
            continue

        # 汇总行（仅 A 列有值）
        if not b_text and a_text in SUMMARY_LABELS:
            if amount is None:
                raise ParseError(f"第 {row_no} 行：汇总项「{a_text}」缺少金额")
            summary_seen[SUMMARY_LABELS[a_text]] = amount
            continue

        # 模块合计行（仅 A 列有值，以"合计"结尾）
        if not b_text and a_text.endswith("合计"):
            section = _classify_section(a_text)
            if section is None:
                raise ParseError(f"第 {row_no} 行：合计行「{a_text}」无法识别所属模块")
            if amount is None:
                raise ParseError(f"第 {row_no} 行：合计行「{a_text}」缺少金额")
            if section == "tooling":
                ensure_tooling()["total"] = amount
            else:
                modules[section]["total"] = amount
            continue

        # 条目行（B 列有值）
        if b_text:
            if amount is None:
                raise ParseError(f"第 {row_no} 行：条目「{b_text}」缺少金额，无法解析")
            section = _classify_section(a_text)
            note = str(d).strip() if d is not None else None
            evidence = _evidence(ir, sheet, row_no, b_text, c)
            if section == "materials":
                modules["materials"]["items"].append(
                    {"name": b_text, "amount_per_pc": amount, "spec": note, "note": None, "evidence": evidence}
                )
            elif section == "processing":
                modules["processing"]["items"].append(
                    {
                        "name": b_text,
                        "amount_per_pc": amount,
                        "atom_code": None,
                        "is_new_process": False,
                        "bundle_flag": False,
                        "bundle_fingerprint": None,
                        "split_method": "none",
                        "confidence": "low",
                        "match_path": None,
                        "confirm_status": "unconfirmed",
                        "note": None,
                        "evidence": evidence,
                    }
                )
            elif section == "inspection":
                modules["inspection"]["items"].append(
                    {"name": b_text, "amount_per_pc": amount, "note": note, "evidence": evidence}
                )
            elif section == "packaging_transport":
                item = {"name": b_text, "amount_per_pc": amount, "item_type": None, "note": note, "evidence": evidence}
                classify_item_type("packaging_transport", item)
                modules["packaging_transport"]["items"].append(item)
            elif section == "sga_tax":
                item = {"name": b_text, "amount_per_pc": amount, "item_type": None, "rate": None, "note": note, "evidence": evidence}
                classify_item_type("sga_tax", item)
                modules["sga_tax"]["items"].append(item)
            elif section == "tooling":
                t = ensure_tooling()
                t_type = _tooling_type(b_text)
                cavities, lifespan = _parse_cavities_lifespan(note)
                key = {"mold": "molds", "fixture": "fixtures", "stencil": "stencils"}[t_type]
                t[key]["items"].append(
                    {
                        "name": b_text,
                        "amount": amount,
                        "cavities": cavities,
                        "lifespan": lifespan,
                        "note": note,
                        "evidence": evidence,
                    }
                )
            else:
                modules["other"]["items"].append(
                    {
                        "name": b_text,
                        "amount_per_pc": amount,
                        "note": note or f"分区未识别，原文：{a_text}",
                        "evidence": evidence,
                    }
                )
            continue

        # 仅 A 列文本 + 金额：进 other（说明必填）
        if a_text and amount is not None:
            modules["other"]["items"].append(
                {
                    "name": a_text,
                    "amount_per_pc": amount,
                    "note": f"分区未识别，原文：{a_text}",
                    "evidence": _evidence(ir, sheet, row_no, a_text, c),
                }
            )
            continue

        # 其余（标题等无金额说明行）跳过

    if basic["part_name"] is None:
        raise ParseError("未找到零件名称，版式不支持")

    # 模块 total 缺省时按明细汇总回填
    for name in MODULES:
        module = modules[name]
        if module["total"] is None and module["items"]:
            module["total"] = items_sum(module)

    # 勾稽字段按规则计算：untaxed=Σ模块合计（排除税费），taxed=untaxed+税额，final=taxed−折扣
    tax_amount = round(
        sum(
            item["amount_per_pc"]
            for item in modules["sga_tax"]["items"]
            if item.get("item_type") == "税费"
        ),
        6,
    )
    untaxed = 0.0
    for name in MODULES:
        if name == "sga_tax":
            continue
        total = module_total(modules[name])
        if total is not None:
            untaxed += total
    sga_total = module_total(modules["sga_tax"])
    if sga_total is not None:
        untaxed += sga_total - tax_amount
    untaxed = round(untaxed, 6)
    discount = summary_seen.get("discount")
    taxed = round(untaxed + tax_amount, 6)
    final = round(taxed - (discount or 0), 6)

    if tooling is not None:
        for key in ("molds", "fixtures", "stencils"):
            sub = tooling[key]
            if sub["total"] is None and sub["items"]:
                sub["total"] = round(sum(i["amount"] for i in sub["items"]), 6)

    return {
        "schema_version": "1.1",
        "supplier": {
            "supplier_name": supplier_name or sheet_supplier or "未知供应商",
            "supplier_code": None,
        },
        "basic": {
            **basic,
            "quote_date": normalize_date(basic["quote_date"]),
            "currency": normalize_currency(basic["currency"]) or "CNY",
        },
        "unit_price": {
            **modules,
            "summary": {
                "untaxed_total": untaxed,
                "tax_amount": tax_amount if (tax_amount or modules["sga_tax"]["items"]) else None,
                "taxed_total": taxed,
                "discount": discount,
                "final_unit_price_taxed": final,
                "calc_check": "unchecked",
            },
        },
        "tooling": tooling,
    }

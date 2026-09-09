"""simple_excel_parse（INTERIM 规则解析器）：sample xlsx 全链路、supplier 覆盖、other 兜底、ParseError。"""

from pathlib import Path

import openpyxl
import pytest

from app.ingest import excel_to_ir, sha256_of
from app.ir import IR
from app.pipeline.simple_excel_parse import ParseError, parse_ir
from app.validate.validate import validate_quote_full, calc_check

FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote.xlsx"


def load_ir() -> IR:
    return excel_to_ir(FIXTURE, sha256_of(FIXTURE))


def test_parse_sample_xlsx_calc_check_pass():
    data = parse_ir(load_ir())
    assert data["schema_version"] == "1.1"
    assert data["supplier"]["supplier_name"] == "深圳市样例五金有限公司"
    assert data["basic"]["part_name"] == "铝合金外壳"
    assert data["basic"]["currency"] == "CNY"
    assert data["basic"]["quote_date"] == "2026-09-01"
    assert calc_check(data) == "pass"

    up = data["unit_price"]
    assert up["materials"]["total"] == 4.5
    assert up["processing"]["total"] == 3.7
    assert [i["name"] for i in up["processing"]["items"]] == ["CNC加工", "阳极氧化", "喷砂"]
    assert up["inspection"]["total"] == 0.3
    assert up["packaging_transport"]["total"] == 0.15
    assert up["sga_tax"]["total"] == 1.86
    assert up["summary"] == {
        "untaxed_total": 10.0,
        "tax_amount": 0.51,
        "taxed_total": 10.51,
        "discount": None,
        "final_unit_price_taxed": 10.51,
        "calc_check": "unchecked",
    }


def test_parse_sample_full_validation_pass():
    data = parse_ir(load_ir())
    check, flags = validate_quote_full(None, data)
    assert check == "pass"
    assert flags == []


def test_parse_evidence_complete():
    data = parse_ir(load_ir())
    item = data["unit_price"]["materials"]["items"][0]
    assert item["evidence"]["file"] == "sample_quote.xlsx"
    assert item["evidence"]["location"] == "报价单!B7:C7"
    assert "铝材" in item["evidence"]["raw_text"]
    mold = data["tooling"]["molds"]["items"][0]
    assert mold["cavities"] == 1
    assert mold["lifespan"] == 500000


def test_parse_supplier_name_override():
    data = parse_ir(load_ir(), supplier_name="覆盖供应商")
    assert data["supplier"]["supplier_name"] == "覆盖供应商"


def test_parse_sga_item_types_and_rate():
    data = parse_ir(load_ir())
    sga = {i["name"]: i for i in data["unit_price"]["sga_tax"]["items"]}
    assert sga["增值税"]["item_type"] == "税费"
    assert sga["增值税"]["rate"] == 0.13
    assert sga["损耗"]["item_type"] == "损耗"
    pack = {i["name"]: i for i in data["unit_price"]["packaging_transport"]["items"]}
    assert pack["运输"]["item_type"] == "运输"


def _write_xlsx(tmp_path: Path, rows: list[list]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "报价单"
    for row in rows:
        ws.append(row)
    path = tmp_path / "custom.xlsx"
    wb.save(path)
    return path


def test_unknown_section_goes_other_with_note(tmp_path):
    rows = [
        ["零件名称", "测试件", None, None],
        ["材料费", "铝材", 1.0, None],
        ["神秘费用", "清洗", 0.5, None],
    ]
    path = _write_xlsx(tmp_path, rows)
    data = parse_ir(excel_to_ir(path, "h"))
    others = data["unit_price"]["other"]["items"]
    assert len(others) == 1
    assert others[0]["name"] == "清洗"
    assert others[0]["note"]
    # 材料费照常，神秘费用金额进 other 后仍参与勾稽
    assert data["unit_price"]["materials"]["total"] == 1.0


def test_missing_amount_raises_parse_error(tmp_path):
    rows = [
        ["零件名称", "测试件", None, None],
        ["加工费", "CNC加工", None, None],
    ]
    path = _write_xlsx(tmp_path, rows)
    with pytest.raises(ParseError, match="第 2 行"):
        parse_ir(excel_to_ir(path, "h"))


def test_missing_part_name_raises(tmp_path):
    rows = [["材料费", "铝材", 1.0, None]]
    path = _write_xlsx(tmp_path, rows)
    with pytest.raises(ParseError, match="零件名称"):
        parse_ir(excel_to_ir(path, "h"))


def test_summary_computed_by_rule_not_raw_rows(tmp_path):
    """无合计行的最小版式：summary 按规则由明细算出。"""
    rows = [
        ["零件名称", "测试件", None, None],
        ["材料费", "铝材", 2.0, None],
        ["加工费", "CNC", 3.0, None],
        ["损管利税", "增值税", 0.65, "税率13%"],
    ]
    path = _write_xlsx(tmp_path, rows)
    data = parse_ir(excel_to_ir(path, "h"))
    s = data["unit_price"]["summary"]
    assert s["untaxed_total"] == 5.0
    assert s["tax_amount"] == 0.65
    assert s["taxed_total"] == 5.65
    assert s["final_unit_price_taxed"] == 5.65
    assert calc_check(data) == "pass"

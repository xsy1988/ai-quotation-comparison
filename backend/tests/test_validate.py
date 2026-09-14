"""validate 模块：枚举打标与 validate_quote_full 汇总。"""

import pytest

from app.db import get_connection, init_db
from app.validate.validate import (
    ValidateError,
    module_total,
    validate_enums,
    validate_quote_full,
)


def make_quote() -> dict:
    return {
        "schema_version": "1.1",
        "supplier": {"supplier_name": "测试供应商", "supplier_code": None},
        "basic": {"part_name": "测试零件", "currency": "CNY", "category": None},
        "unit_price": {
            "materials": {"total": 4.0, "items": [{"name": "铝材", "amount_per_pc": 4.0}]},
            "processing": {
                "total": 3.0,
                "items": [
                    {
                        "name": "CNC",
                        "amount_per_pc": 3.0,
                        "atom_code": "AT-TEST-001",
                        "confidence": "high",
                        "confirm_status": "unconfirmed",
                    }
                ],
            },
            "inspection": {"total": 0.5, "items": []},
            "packaging_transport": {
                "total": 0.5,
                "items": [{"name": "运输", "amount_per_pc": 0.5, "item_type": "运输"}],
            },
            "sga_tax": {
                "total": 0.91,
                "items": [{"name": "税费", "amount_per_pc": 0.91, "item_type": "税费", "rate": 0.13}],
            },
            "other": {"total": 0.1, "items": []},
            "summary": {
                "untaxed_total": 8.1,
                "tax_amount": 0.91,
                "taxed_total": 9.01,
                "discount": 0.4,
                "final_unit_price_taxed": 8.61,
            },
        },
    }


@pytest.fixture
def conn_with_atom():
    init_db()
    conn = get_connection()
    with conn:
        conn.execute("INSERT INTO process_domain (code, name) VALUES ('TEST', '测试域')")
        conn.execute("INSERT INTO process_stage (name) VALUES ('测试阶段')")
        conn.execute("INSERT INTO process_class (name) VALUES ('测试类别')")
        conn.execute(
            "INSERT INTO atom (code, name, domain_code, stage_name, class_name) VALUES ('AT-TEST-001', '测试工艺', 'TEST', '测试阶段', '测试类别')"
        )
    return conn


def test_validate_enums_clean(conn_with_atom):
    assert validate_enums(conn_with_atom, make_quote()) == []


def test_validate_enums_flags_all_issues(conn_with_atom):
    data = make_quote()
    data["basic"]["currency"] = "XXX"
    data["unit_price"]["processing"]["items"][0]["atom_code"] = "AT-NOPE-999"
    data["unit_price"]["packaging_transport"]["items"][0]["item_type"] = "保险"
    data["unit_price"]["sga_tax"]["items"][0]["item_type"] = "回扣"
    flags = validate_enums(conn_with_atom, data)
    assert "invalid_currency" in flags
    assert "invalid_atom_code" in flags
    assert "invalid_item_type" in flags
    conn_with_atom.close()


def test_validate_enums_atom_code_null_not_flagged(conn_with_atom):
    data = make_quote()
    data["unit_price"]["processing"]["items"][0]["atom_code"] = None
    assert validate_enums(conn_with_atom, data) == []
    conn_with_atom.close()


def test_validate_quote_full_pass(conn_with_atom):
    check, flags = validate_quote_full(conn_with_atom, make_quote())
    assert check == "pass"
    assert flags == []
    conn_with_atom.close()


def test_validate_quote_full_calc_fail_flagged(conn_with_atom):
    data = make_quote()
    data["unit_price"]["summary"]["final_unit_price_taxed"] = 99.0
    check, flags = validate_quote_full(conn_with_atom, data)
    assert check == "fail"
    assert "calc_abnormal" in flags
    conn_with_atom.close()


def test_validate_quote_full_schema_error_raises(conn_with_atom):
    data = make_quote()
    del data["unit_price"]["summary"]
    with pytest.raises(ValidateError):
        validate_quote_full(conn_with_atom, data)
    conn_with_atom.close()


def test_module_total_all_null_amounts_returns_none():
    """明细金额全部未印出（null）时无可加项 → 返回 None（不得以 0.0 占位）。"""
    assert module_total({"total": None, "items": [{"amount_per_pc": None}]}) is None
    assert module_total({"total": None, "items": []}) is None
    assert module_total({"total": None, "items": [{"amount_per_pc": 2.0}]}) == 2.0
    assert module_total({"total": 3.0, "items": [{"amount_per_pc": None}]}) == 3.0

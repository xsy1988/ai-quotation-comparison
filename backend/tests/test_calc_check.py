"""勾稽 calc_check 与徽标 collect_flags 测试。"""

import copy

from app.persist import calc_check, collect_flags


def make_quote() -> dict:
    """勾稽平衡的样例报价单。"""
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
                        "atom_code": "AT-JG-001",
                        "confidence": "high",
                        "confirm_status": "unconfirmed",
                    }
                ],
            },
            "inspection": {"total": 0.5, "items": []},
            "packaging_transport": {"total": 0.5, "items": []},
            "sga_tax": {
                "total": 0.91,
                "items": [{"name": "税费", "amount_per_pc": 0.91, "item_type": "税费", "rate": 0.13}],
            },
            "other": {"total": 0.1, "items": [{"name": "杂费", "amount_per_pc": 0.1, "note": "兜底"}]},
            "summary": {
                "untaxed_total": 8.1,
                "tax_amount": 0.91,
                "taxed_total": 9.01,
                "discount": 0.5,
                "final_unit_price_taxed": 8.51,
            },
        },
    }


def test_calc_check_pass():
    assert calc_check(make_quote()) == "pass"


def test_calc_check_final_mismatch_fails():
    data = make_quote()
    data["unit_price"]["summary"]["final_unit_price_taxed"] = 9.99
    assert calc_check(data) == "fail"


def test_calc_check_module_total_less_than_items_fails():
    data = make_quote()
    data["unit_price"]["materials"]["total"] = 3.0  # < Σitems 4.0
    assert calc_check(data) == "fail"


def test_calc_check_discount_chain():
    data = make_quote()
    data["unit_price"]["summary"]["discount"] = None
    data["unit_price"]["summary"]["final_unit_price_taxed"] = 9.01  # 无折扣时最终=含税合计
    assert calc_check(data) == "pass"


def test_collect_flags_clean():
    assert collect_flags(make_quote(), "pass") == []


def test_collect_flags_calc_abnormal():
    assert collect_flags(make_quote(), "fail") == ["calc_abnormal"]


def test_collect_flags_low_confidence_and_unmatched():
    data = make_quote()
    item = copy.deepcopy(data["unit_price"]["processing"]["items"][0])
    item.update({"name": "神秘工艺", "atom_code": None, "confidence": "low"})
    data["unit_price"]["processing"]["items"].append(item)
    assert set(collect_flags(data, "pass")) == {"low_confidence", "unmatched"}


def test_collect_flags_new_process():
    data = make_quote()
    item = copy.deepcopy(data["unit_price"]["processing"]["items"][0])
    item.update({"name": "新工艺", "atom_code": None, "is_new_process": True})
    data["unit_price"]["processing"]["items"].append(item)
    assert "new_process" in collect_flags(data, "pass")

"""fee_classify：非加工费 item_type 推断测试。"""

from app.match.fee_classify import classify_item_type, classify_packaging, classify_sga


def test_sga_keywords():
    assert classify_sga("增值税") == "税费"
    assert classify_sga("损耗率2%") == "损耗"
    assert classify_sga("工厂管理费") == "管理费"
    assert classify_sga("毛利") == "利润"
    assert classify_sga("莫名其妙的费用") is None


def test_packaging_keywords():
    assert classify_packaging("顺丰运费") == "运输"
    assert classify_packaging("吸塑包装") == "包装"
    assert classify_packaging("杂项") is None


def test_sga_fallback_to_other():
    item = {"name": "赞助费", "amount_per_pc": 0.1, "item_type": None, "note": None}
    out = classify_item_type("sga_tax", item)
    assert out["item_type"] == "其他"
    assert "赞助费" in out["note"]


def test_tax_rate_extracted_from_note():
    item = {"name": "增值税", "amount_per_pc": 0.51, "item_type": "税费", "rate": None,
            "note": "税率13%"}
    out = classify_item_type("sga_tax", item)
    assert out["rate"] == 0.13


def test_wrong_packaging_type_corrected():
    item = {"name": "顺丰运费", "amount_per_pc": 0.05, "item_type": "包装", "note": None}
    out = classify_item_type("packaging_transport", item)
    assert out["item_type"] == "运输"


def test_valid_type_kept_when_no_keyword():
    item = {"name": "利润", "amount_per_pc": 0.9, "item_type": "利润", "rate": None, "note": None}
    out = classify_item_type("sga_tax", item)
    assert out["item_type"] == "利润"

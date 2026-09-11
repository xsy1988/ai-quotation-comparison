from app.normalize import (
    amount_in_text,
    amount_occurrences,
    normalize_amount,
    normalize_currency,
    normalize_date,
    normalize_rate,
)


def test_pure_amount_with_currency():
    assert normalize_amount("¥1,234.50") == 1234.50
    assert normalize_amount("￥１２３４.５") == 1234.5  # 全角
    assert normalize_amount("12.5元") == 12.5
    assert normalize_amount("(100)") == -100.0
    assert normalize_amount("-50") == -50.0


def test_amount_with_unit_suffix():
    assert normalize_amount("12.5元/pcs") == 12.5


def test_non_amount_text_returns_none():
    assert normalize_amount("1/2") is None
    assert normalize_amount("10-20") is None
    assert normalize_amount("10~20") is None
    assert normalize_amount("13%") is None
    assert normalize_amount("abc") is None
    assert normalize_amount(None) is None
    assert normalize_amount("") is None


def test_numeric_passthrough():
    assert normalize_amount(12) == 12.0
    assert normalize_amount(0.8) == 0.8


def test_normalize_rate():
    assert normalize_rate("13%") == 0.13
    assert normalize_rate("5.5％") == 0.055
    assert normalize_rate("0.13") == 0.13
    assert normalize_rate(0.05) == 0.05
    assert normalize_rate(None) is None
    assert normalize_rate("abc") is None


def test_normalize_date():
    assert normalize_date("2026年9月8日") == "2026-09-08"
    assert normalize_date("2026/09/08") == "2026-09-08"
    assert normalize_date("不是日期") is None


def test_normalize_currency():
    assert normalize_currency("人民币") == "CNY"
    assert normalize_currency("US$") == "USD"
    assert normalize_currency("未知币") == "OTHER"


def test_amount_in_text_word_boundary():
    """词边界出处比对：候选前后不得再跟数字/小数点。"""
    assert amount_in_text("全检 0.30", 0.3)  # 0.3 命中两位小数写法
    assert amount_in_text("损管利税 2.01", 2.01)
    assert not amount_in_text("合计 10.30", 0.3)  # "0.30" 是 "10.30" 的子串，不命中
    assert not amount_in_text("税率 13.0%", 0.0)  # "0" 是 "13.0" 的子串，不命中
    assert not amount_in_text("不良率 20% 2.01", 0.2)  # "0.2" 是 "20" 的数字邻居场景
    assert not amount_in_text(None, 0.3)
    assert not amount_in_text("", 0.3)


def test_amount_occurrences_counts_boundary_hits():
    assert amount_occurrences("全检 0.30", 0.3) == 1
    assert amount_occurrences("0.30 0.30", 0.3) == 2  # 恰好出现一次判定的反例
    assert amount_occurrences("10.30", 0.3) == 0

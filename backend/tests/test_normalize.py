from app.normalize import normalize_amount, normalize_currency, normalize_date, normalize_rate


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

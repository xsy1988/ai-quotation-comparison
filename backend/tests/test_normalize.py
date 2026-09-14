from app.normalize import (
    amount_in_text,
    amount_occurrences,
    display_number,
    extract_moq,
    extract_moq_options,
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


def test_separator_only_text_returns_none():
    """版面提取噪声：只剩千分位分隔符/单位，清洗后为空，必须返回 None 而不是抛 ValueError。"""
    for noise in ("，", ",", "，，", "元", "￥", "¥", "／"):
        assert normalize_amount(noise) is None, noise


def test_amount_with_leading_separators():
    assert normalize_amount("，123") == 123.0
    assert normalize_amount(",1,234") == 1234.0


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


def test_amount_in_text_tolerates_float_noise():
    """逐字符口径落空时退化为数值比对：Excel 公式求值带的尾数噪声不再误判为编造。"""
    assert amount_in_text("3#铝合金 / 0.02 / 0.021 / 35 / 0.7000000000000001", 0.7)
    assert amount_in_text("0.8654999999999999", 0.8655)
    assert amount_in_text("未税合计 13.66 / 增值税 13.0", 13.0)  # "13" 被 "13.0" 挡住数字边界
    assert not amount_in_text("合计 10.30", 0.3)  # 数值口径同样不允许跨数字匹配
    assert not amount_in_text("不良率 20% 2.01", 0.2)


def test_display_number_strips_binary_noise():
    """0.02×35 的浮点噪声按 Excel 显示精度抹平。"""
    assert display_number(0.02 * 35) == 0.7  # 0.7000000000000001 → 0.7
    assert display_number(0.87 - 0.0045) == 0.8655
    assert display_number(21.883015) == 21.883015
    assert display_number(13) == 13.0


def test_amount_occurrences_counts_boundary_hits():
    assert amount_occurrences("全检 0.30", 0.3) == 1
    assert amount_occurrences("0.30 0.30", 0.3) == 2  # 恰好出现一次判定的反例
    assert amount_occurrences("10.30", 0.3) == 0


def test_extract_moq_declared_forms():
    """声明式写法：量级缩写（K/千/万）、千分位、英文关键词、单双色分别标注取通用值。"""
    assert extract_moq("皮革现货单色 MOQ：3K") == (3000, "MOQ:3K")
    assert extract_moq("MOQ,单色5K") == (5000, "MOQ,单色5K")
    assert extract_moq("最小起订量 2000PCS") == (2000, "最小起订量 2000")
    assert extract_moq("起订量：3,000") == (3000, "起订量:3,000")
    assert extract_moq("起订量2千个") == (2000, "起订量2千")
    assert extract_moq("起订量：单色3K，双色5K") == (3000, "起订量:单色3K")  # 取最先出现的通用值
    assert extract_moq("minimum order quantity 500") == (500, "minimum order quantity 500")
    assert extract_moq("起订量不足500") == (500, "起订量不足500")


def test_extract_moq_threshold_forms():
    """阈值式写法（订单量下限即起订量）：豪泽 2000PCS / 美格 3000PCS。"""
    assert extract_moq("订单量少于2000PCS加收开机费1000元。") == (2000, "订单量少于2000")
    assert extract_moq("订单量少于3000PCS加收开机费800元。") == (3000, "订单量少于3000")
    assert extract_moq("低于起订量2000PCS的另议") == (2000, "起订量2000")


def test_extract_moq_ignores_non_moq_text():
    """非起订量文本：报价数量列、穴数/寿命、无数字表述、跨越无关词的金额。"""
    assert extract_moq("数量(PCS): 1") is None
    assert extract_moq("穴数 1*1，模具寿命 30万模次") is None
    assert extract_moq("无起订量要求") is None
    assert extract_moq("MOQ另议，模具费20000元") is None
    assert extract_moq("MOQ：0") is None
    assert extract_moq(None) is None
    assert extract_moq("") is None


def test_extract_moq_ignores_fields_in_other_info_markdown():
    """「其它信息」Markdown 整体送入：命中商务条款里的起订量，不受同段落其它数字干扰。"""
    text = (
        "## 商务条款\n"
        "- 报价有效期 15 天\n"
        "- 订单量少于2000PCS加收开机费1000元。\n"
        "- 运费：珠三角供方承担\n"
    )
    assert extract_moq(text) == (2000, "订单量少于2000")


def _pairs(text):
    return [(o["condition"], o["value"]) for o in extract_moq_options(text)]


def test_extract_moq_options_multiple_conditions():
    """同一产品按条件分档：每档各成一条（这是 moq_options 的立案场景）。"""
    assert _pairs("金属管需要提供3%损耗；皮革现货单色 MOQ：3K；定制皮革单色 MOQ：40K") == [
        ("皮革现货单色", 3000),
        ("定制皮革单色", 40000),
    ]
    assert _pairs("MOQ：皮革现货单色 MOQ：3K；定制皮革单色 MOQ：40K") == [
        ("皮革现货单色", 3000),
        ("定制皮革单色", 40000),
    ]
    # 并列档位（同一「起订量：」后跟多档，只有首档带关键词）
    assert _pairs("起订量：单色3K，双色5K") == [("单色", 3000), ("双色", 5000)]
    assert _pairs("起订量：常规3K、加急1K、大货2K") == [
        ("常规", 3000),
        ("加急", 1000),
        ("大货", 2000),
    ]


def test_extract_moq_options_single_and_empty():
    """单一无条件起订量只有一条（调用方据此判定"无分档"）；无起订量文本返回空表。"""
    assert _pairs("起订量：3,000") == [(None, 3000)]
    assert _pairs("订单量少于2000PCS加收开机费1000元。") == [(None, 2000)]
    assert _pairs("MOQ 5000PCS，月结60天") == [(None, 5000)]
    assert extract_moq_options("穴数 1*1，模具寿命 30万模次") == []
    assert extract_moq_options(None) == []
    assert extract_moq_options("") == []


def test_extract_moq_options_chain_guards():
    """并列档位只在带条件的声明之后顺延，且不吞掉模具费/交期等其它条款。"""
    assert _pairs("起订量：3K，模具费1万") == [(None, 3000)]  # 无条件声明 → 不顺延
    assert _pairs("起订量：单色3K，交期15天。双色5K") == [("单色", 3000)]  # 句号终结 + 非档位词
    assert _pairs("MOQ 5000PCS，月结60天，起订5000") == [(None, 5000)]  # 条件+数值相同 → 去重


def test_extract_moq_options_dedupes_across_patterns():
    """同一条写法可能同时命中声明式与阈值式：按「条件 + 数值」去重。"""
    options = extract_moq_options("低于起订量2000PCS的另议")
    assert [(o["condition"], o["value"]) for o in options] == [(None, 2000)]
    assert options[0]["snippet"]

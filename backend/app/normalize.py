"""规范化（流水线段④配套）：数字/日期/币种纯函数，只规范化不做映射。"""

import re
import unicodedata
from datetime import date, datetime
from typing import Any

CURRENCY_MAP = {
    "￥": "CNY", "¥": "CNY", "RMB": "CNY", "CNY": "CNY", "人民币": "CNY", "元": "CNY",
    "$": "USD", "US$": "USD", "USD": "USD", "美元": "USD",
    "€": "EUR", "EUR": "EUR", "欧元": "EUR",
    "JP¥": "JPY", "JPY": "JPY", "日元": "JPY",
    "HK$": "HKD", "HKD": "HKD", "港币": "HKD", "港元": "HKD",
    "NT$": "TWD", "TWD": "TWD", "新台币": "TWD",
    "₩": "KRW", "KRW": "KRW", "韩元": "KRW",
}

DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d",
    "%Y年%m月%d日", "%d-%m-%Y", "%m/%d/%Y",
)

_AMOUNT_NOISE_RE = re.compile(r"[\s,，￥¥$€元/／]")
# 至少含一位数字：单独的千分位分隔符（"，"、","）是版面提取噪声，不能被当成金额
_PURE_AMOUNT_RE = re.compile(r"^\(?-?[\d,，]*\d[\d,，]*(\.\d+)?\)?\s*(元|人民币|块)?$")
_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")
_FRACTION_RE = re.compile(r"^\d+\s*/\s*\d+$")
_RANGE_RE = re.compile(r"^\d+(\.\d+)?\s*[-~～—]\s*\d+(\.\d+)?$")


def normalize_amount(value: Any) -> float | None:
    """金额文本 → float。支持全角字符、千分位、货币符号与单位；无法解析返回 None。
    分数（"1/2"）、区间（"10-20"）、费率（"13%"）不是金额，返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return None
    if text.endswith("%") or _FRACTION_RE.match(text) or _RANGE_RE.match(text):
        return None
    negative = (text.startswith("(") and text.endswith(")")) or text.startswith("-")
    if _PURE_AMOUNT_RE.match(text):
        cleaned = _AMOUNT_NOISE_RE.sub("", text).strip("()+-")
        if not cleaned:  # 纯噪声（如 "，"/"元"）清洗后为空，不是金额
            return None
        amount = float(cleaned)
        return -amount if negative else amount
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    tail = text[match.end():].strip()
    # 数字后面紧跟 % 的是费率；紧跟 / 或 - 的是分数/区间的一部分
    if tail.startswith(("%", "/")):
        return None
    amount = float(match.group(0).replace(",", ""))
    return -amount if negative else amount


def amount_strings(amount: float) -> set[str]:
    """金额的可能文本写法（0.3 → {"0.3", "0.30"}），用于 evidence.raw_text 出处比对。"""
    value = float(amount)
    return {str(round(value, 6)), f"{value:.2f}", f"{value:g}"}


def amount_occurrences(text: str | None, amount: float) -> int:
    """金额写法在文本中的出现次数（词边界口径：命中位置前后不得再跟数字或小数点，
    避免 "0" 误中 "13.0"、"0.30" 误中 "10.30" 这类子串误判）。"""
    if not text:
        return 0
    count = 0
    for candidate in amount_strings(amount):
        start = 0
        while True:
            index = text.find(candidate, start)
            if index < 0:
                break
            before = text[index - 1] if index > 0 else ""
            after_index = index + len(candidate)
            after = text[after_index] if after_index < len(text) else ""
            if not (before.isdigit() or before == ".") and not (after.isdigit() or after == "."):
                count += 1
            start = index + 1
    return count


def amount_in_text(text: str | None, amount: float) -> bool:
    """金额写法是否以词边界出现在文本中（出处比对口径，同 amount_occurrences）。"""
    return amount_occurrences(text, amount) > 0


def normalize_rate(value: Any) -> float | None:
    """费率文本 → 小数（"13%"→0.13，"0.13"→0.13）；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = unicodedata.normalize("NFKC", str(value)).strip().rstrip("％")
    if not text:
        return None
    try:
        rate = float(text.rstrip("%").strip())
    except ValueError:
        match = _NUMBER_RE.search(text)
        if not match:
            return None
        rate = float(match.group(0).replace(",", ""))
    return rate / 100 if text.endswith("%") else rate


def normalize_date(value: Any) -> str | None:
    """日期文本 → ISO（YYYY-MM-DD）；无法解析返回 None。"""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = unicodedata.normalize("NFKC", str(value)).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_currency(value: Any) -> str | None:
    """币种符号/缩写/中文 → quote_schema 枚举（CNY/USD/EUR/JPY/HKD/TWD/KRW/OTHER）。"""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text in CURRENCY_MAP:
        return CURRENCY_MAP[text]
    return "OTHER" if text else None

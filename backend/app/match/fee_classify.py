"""非加工费条目的 item_type 推断（语义映射层配套：同义词表 + 规则）。

只补全/纠正 item_type 与缺失费率，不改金额。归不进的类型进"其他"并保留原文语义。
"""

import re
from typing import Any

from app.normalize import normalize_rate

# 顺序即优先级：税费含"税"字必须最先判
_SGA_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("税费", ("增值税", "税率", "税金", "税额", "开票", "税收", "税")),
    ("损耗", ("损耗", "报废", "不良", "次品")),
    ("管理费", ("管理", "厂管", "overhead")),
    ("利润", ("利润", "毛利", "净利", "盈利")),
]
_PACKAGING_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("运输", ("运输", "运费", "物流", "快递", "货运", "送货", "装卸")),
    ("包装", ("包装", "包材", "纸箱", "吸塑", "珍珠棉", "气泡", "贴纸", "外箱")),
]

_RATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[％%]")


def classify_sga(name: str) -> str | None:
    for item_type, keywords in _SGA_KEYWORDS:
        if any(k in name for k in keywords):
            return item_type
    return None


def classify_packaging(name: str) -> str | None:
    for item_type, keywords in _PACKAGING_KEYWORDS:
        if any(k in name.casefold() for k in keywords):
            return item_type
    return None


def classify_item_type(module: str, item: dict[str, Any]) -> dict[str, Any]:
    """按模块与名称推断 item_type；sga_tax 归不进置"其他"（note 保留原文）。"""
    name = str(item.get("name") or "")
    current = item.get("item_type")
    inferred: str | None = None
    if module == "sga_tax":
        inferred = classify_sga(name)
    elif module == "packaging_transport":
        inferred = classify_packaging(name)

    if inferred:
        item["item_type"] = inferred
    elif module == "sga_tax" and current not in {"损耗", "管理费", "利润", "税费", "其他"}:
        item["item_type"] = "其他"
        if not item.get("note"):
            item["note"] = f"类型未识别，原文：{name}"

    # 税费条目 rate 缺失时从名称/note 提取
    if item.get("item_type") == "税费" and item.get("rate") is None:
        for text in (name, str(item.get("note") or "")):
            match = _RATE_RE.search(text)
            if match:
                item["rate"] = normalize_rate(match.group(0))
                break
    return item

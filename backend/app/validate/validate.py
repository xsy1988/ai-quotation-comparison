"""校验（流水线段④）：schema 校验 + 勾稽校验 + 枚举校验。所有校验只打标不阻断（schema 错误除外）。"""

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "quote_schema.json"

MODULES = ("materials", "processing", "inspection", "packaging_transport", "sga_tax", "other")

CURRENCY_ENUM = ("CNY", "USD", "EUR", "JPY", "HKD", "TWD", "KRW", "OTHER")
PACKAGING_ITEM_TYPES = ("包装", "运输")
SGA_ITEM_TYPES = ("损耗", "管理费", "利润", "税费", "其他")


class ValidateError(Exception):
    pass


@lru_cache(maxsize=1)
def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_envelope_schema() -> dict:
    """信封 schema：{"offers": [单 offer schema...]}。单文件多产品/多方案时 LLM 的输出结构，
    由单 offer schema 程序化包装，避免维护两份 280 行定义。
    definitions 提升到信封根部：offer 内的 $ref '#/definitions/...' 以信封文档为根解析。"""
    offer = load_schema()
    return {
        "type": "object",
        "required": ["offers"],
        "properties": {
            "offers": {"type": "array", "minItems": 1, "items": offer}
        },
        "definitions": offer.get("definitions", {}),
    }


def iter_offers(data: dict) -> list[dict]:
    """从 LLM 输出取 offers 列表；兼容旧格式（无信封层的单个 quote 对象）。"""
    offers = data.get("offers")
    return offers if isinstance(offers, list) and offers else [data]


def validate_quote(data: dict, schema: dict | None = None) -> None:
    schema = schema or load_schema()
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        lines = [
            f"  $.{'.'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors[:10]
        ]
        raise ValidateError(f"quote JSON 不符合 quote_schema v1.1，共 {len(errors)} 处错误：\n" + "\n".join(lines))


def items_sum(module: dict) -> float:
    return round(sum(item.get("amount_per_pc") or 0 for item in module.get("items") or []), 6)


def module_total(module: dict) -> float | None:
    """模块合计取值优先级：total 字段 > Σitems；两者皆空返回 None。

    明细金额全部未印出（null）时没有任何已知项可累加，返回 None 而非 0.0——null 不得以 0 占位。
    """
    if module.get("total") is not None:
        return round(float(module["total"]), 6)
    items = module.get("items") or []
    if not items or all(item.get("amount_per_pc") is None for item in items):
        return None
    return items_sum(module)


def _within_tolerance(actual: float | None, expected: float) -> bool:
    if actual is None:
        return True
    tol = max(0.01, abs(expected) * 0.01)
    return abs(actual - expected) <= tol


def calc_check(data: dict) -> str:
    """勾稽校验（报价结构说明 §五）：模块 total ≥ Σitems；未税=六模块合计（排除税费），
    含税=未税+税额，最终=含税−折扣，允差 ±0.01 或 ±1%。全部只判 pass/fail，不阻断。"""
    from app.derive import sga_untaxed_contribution, untaxed_total_candidates, untaxed_total_of

    up = data["unit_price"]

    for name in MODULES:
        module = up[name]
        if module.get("total") is None:
            continue
        printed = round(float(module["total"]) + 0.01, 6)
        if name == "sga_tax":
            # sga_tax.total 口径不统一（可能不含单列的税费）：Σitems 或"非税费部分"任一不超 total 即一致
            if items_sum(module) > printed and sga_untaxed_contribution(module) > printed:
                return "fail"
            continue
        if items_sum(module) > printed:
            return "fail"

    untaxed = untaxed_total_of(up)  # 与 derive 重算 summary 同口径
    candidates = untaxed_total_candidates(up)  # 人工修正按模块 total 口径，两种口径都算合法

    summary = up["summary"]
    if summary.get("untaxed_total") is not None and not any(
        _within_tolerance(summary["untaxed_total"], expected) for expected in candidates
    ):
        return "fail"

    untaxed_used = summary["untaxed_total"] if summary.get("untaxed_total") is not None else untaxed
    if summary.get("tax_amount") is not None:
        taxed_expected = untaxed_used + summary["tax_amount"]
        if not _within_tolerance(summary.get("taxed_total"), taxed_expected):
            return "fail"
    taxed_used = summary.get("taxed_total")
    if taxed_used is None and summary.get("tax_amount") is not None:
        taxed_used = untaxed_used + summary["tax_amount"]
    if taxed_used is None:
        taxed_used = untaxed_used
    final_expected = taxed_used - (summary.get("discount") or 0)
    if not _within_tolerance(summary.get("final_unit_price_taxed"), final_expected):
        return "fail"
    return "pass"


def validate_enums(conn: sqlite3.Connection | None, data: dict) -> list[str]:
    """枚举校验（只打标不阻断）：币种枚举、atom_code 存在性、item_type 合法值。"""
    flags: set[str] = set()
    basic = data.get("basic") or {}
    if basic.get("currency") is not None and basic["currency"] not in CURRENCY_ENUM:
        flags.add("invalid_currency")

    if conn is not None:
        known_atoms = {
            row[0]
            for row in conn.execute("SELECT code FROM atom")
        }
    else:
        known_atoms = None

    up = data.get("unit_price") or {}
    for item in (up.get("processing") or {}).get("items") or []:
        code = item.get("atom_code")
        if code and known_atoms is not None and code not in known_atoms:
            flags.add("invalid_atom_code")
    for item in (up.get("packaging_transport") or {}).get("items") or []:
        if item.get("item_type") is not None and item["item_type"] not in PACKAGING_ITEM_TYPES:
            flags.add("invalid_item_type")
    for item in (up.get("sga_tax") or {}).get("items") or []:
        if item.get("item_type") is not None and item["item_type"] not in SGA_ITEM_TYPES:
            flags.add("invalid_item_type")
    return sorted(flags)


def validate_quote_full(
    conn: sqlite3.Connection | None, data: dict
) -> tuple[str, list[str]]:
    """完整校验：schema（不通过抛 ValidateError）+ 勾稽 + 枚举。
    返回 (calc_check, flags 增量列表)。"""
    validate_quote(data)
    check = calc_check(data)
    flags: list[str] = []
    if check == "fail":
        flags.append("calc_abnormal")
    flags.extend(validate_enums(conn, data))
    return check, flags

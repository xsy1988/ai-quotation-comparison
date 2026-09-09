"""就地编辑（第 7 步）：人工修正 quote / quote_line，快照与关系表同步更新。

- 金额/模块合计修正后重推导汇总（未税/税额/含税/最终单价）并回写快照与 quote 表；
- 原子人工修正置 match_path=manual，原文写法回流 atom_alias（source=manual_feedback），
  对应 new_atom_suggestion 置 merged；
- 品类变更重置非 manual 的映射结果并重跑 L1+L2（LLM 失败上抛，不降级）。

快照 item 定位约定与 mapping_runner 一致：同 (quote_id, module) 下按 id 排序的
quote_line 行序 = 快照 unit_price[module].items 数组序（断言防错位）。
"""

import json
import sqlite3

from app.match.atom_match import make_fingerprint
from app.persist import (
    MODULES,
    calc_check,
    collect_flags,
    module_total,
)
from app.pipeline.mapping_runner import run_mapping

QUOTE_BASIC_FIELDS = (
    "part_name",
    "material_spec",
    "quote_date",
    "currency",
    "moq",
    "quote_no",
)

CONFIRM_STATUS_ENUM = ("confirmed", "corrected")


class CorrectionError(Exception):
    """修正请求非法（400）：枚举非法、atom/category 不存在等。"""

    status_code = 400


class CorrectionNotFound(Exception):
    """quote / quote_line 不存在（404）。"""

    status_code = 404


def _load_snapshot(conn: sqlite3.Connection, quote_id: int) -> tuple[dict, str]:
    row = conn.execute(
        "SELECT raw_json_path FROM quote WHERE id = ?", (quote_id,)
    ).fetchone()
    if row is None:
        raise CorrectionNotFound("报价单不存在")
    path = row["raw_json_path"]
    data = json.loads(open(path, encoding="utf-8").read())
    return data, path


def _save_snapshot(conn: sqlite3.Connection, quote_id: int, data: dict, path: str) -> None:
    json.dumps(data, ensure_ascii=False)  # 防御：序列化失败则不写盘
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _line_rows(conn: sqlite3.Connection, quote_id: int, module: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id FROM quote_line WHERE quote_id = ? AND module = ? ORDER BY id",
            (quote_id, module),
        )
    )


def _snapshot_item(
    conn: sqlite3.Connection, data: dict, quote_id: int, module: str, line_id: int
) -> tuple[dict, int]:
    """按行序 zip 定位快照 item，返回 (item, 行下标)。"""
    rows = _line_rows(conn, quote_id, module)
    ids = [row["id"] for row in rows]
    if line_id not in ids:
        raise CorrectionNotFound("明细条目不存在")
    idx = ids.index(line_id)
    items = data["unit_price"][module].get("items") or []
    assert len(rows) == len(items), f"{module} 行数与 JSON items 数不一致"
    return items[idx], idx


def _sync_quote_derived(conn: sqlite3.Connection, quote_id: int, data: dict) -> None:
    """改金额/模块合计后重推导：回写快照 summary、quote 汇总列与模块 total 列，
    重算 calc_check 与 flags。"""
    up = data["unit_price"]
    module_totals = {name: module_total(up[name]) for name in MODULES}
    tax = round(
        sum(
            item.get("amount_per_pc") or 0
            for item in up["sga_tax"].get("items") or []
            if item.get("item_type") == "税费"
        ),
        6,
    )
    untaxed = round(
        sum(v or 0 for k, v in module_totals.items() if k != "sga_tax")
        + (module_totals["sga_tax"] or 0)
        - tax,
        6,
    )
    taxed = round(untaxed + tax, 6)
    discount = up["summary"].get("discount") or 0
    final = round(taxed - discount, 6)

    summary = up["summary"]
    summary["untaxed_total"] = untaxed
    summary["tax_amount"] = tax
    summary["taxed_total"] = taxed
    summary["final_unit_price_taxed"] = final

    check = calc_check(data)
    flags = collect_flags(data, check)
    with conn:
        conn.execute(
            f"""UPDATE quote SET untaxed_total = ?, tax_amount = ?,
                discount = ?, final_unit_price_taxed = ?,
                {", ".join(f"{name}_total = ?" for name in MODULES)},
                calc_check = ?, flags = ?
                WHERE id = ?""",
            (
                untaxed,
                tax,
                summary.get("discount"),
                final,
                *(module_totals[name] for name in MODULES),
                check,
                json.dumps(flags, ensure_ascii=False),
                quote_id,
            ),
        )


def _mark_reviewed(conn: sqlite3.Connection, quote_id: int, data: dict) -> None:
    data.setdefault("basic", {})["parse_status"] = "reviewed"
    with conn:
        conn.execute(
            "UPDATE quote SET parse_status = 'reviewed' WHERE id = ?", (quote_id,)
        )


def _quote_summary(conn: sqlite3.Connection, quote_id: int, data: dict) -> dict:
    row = conn.execute(
        """SELECT id, supplier_name, category_code, parse_status, calc_check, flags,
                  untaxed_total, tax_amount, discount, final_unit_price_taxed
           FROM quote WHERE id = ?""",
        (quote_id,),
    ).fetchone()
    return {
        "quote_id": quote_id,
        "supplier_name": row["supplier_name"],
        "category_code": row["category_code"],
        "parse_status": row["parse_status"],
        "calc_check": row["calc_check"],
        "flags": json.loads(row["flags"] or "[]"),
        "summary": data["unit_price"]["summary"],
    }


def _reset_non_manual_items(data: dict) -> None:
    """品类变更：快照中 match_path 非 manual 的加工条目重置为未匹配状态。"""
    for item in data["unit_price"]["processing"].get("items") or []:
        if item.get("match_path") == "manual":
            continue
        for key in (
            "atom_code",
            "confidence",
            "match_path",
            "bundle_members",
            "bundle_fingerprint",
            "split_method",
        ):
            item[key] = None
        item["bundle_flag"] = False
        item["is_new_process"] = False


def patch_quote(conn: sqlite3.Connection, quote_id: int, patch: dict) -> dict:
    data, path = _load_snapshot(conn, quote_id)
    basic = data.setdefault("basic", {})
    supplier = data.setdefault("supplier", {})

    changed_derived = False

    for field in QUOTE_BASIC_FIELDS:
        if field in patch:
            basic[field] = patch[field]

    if "supplier_name" in patch:
        supplier["supplier_name"] = patch["supplier_name"]
        with conn:
            conn.execute(
                "UPDATE quote SET supplier_name = ? WHERE id = ?",
                (patch["supplier_name"], quote_id),
            )

    if "module_totals" in patch:
        totals = patch["module_totals"] or {}
        for name in MODULES:
            if name in totals:
                data["unit_price"][name]["total"] = totals[name]
        changed_derived = True

    if "discount" in patch:
        data["unit_price"]["summary"]["discount"] = patch["discount"]
        changed_derived = True

    category_code = patch.get("category_code")
    if category_code is not None:
        exists = conn.execute(
            "SELECT 1 FROM category WHERE code = ?", (category_code,)
        ).fetchone()
        if exists is None:
            raise CorrectionError(f"品类不存在：{category_code}")
        basic["category"] = category_code
        _reset_non_manual_items(data)
        _save_snapshot(conn, quote_id, data, path)
        with conn:
            conn.execute(
                "UPDATE quote SET category_code = ? WHERE id = ?", (category_code, quote_id)
            )
            conn.execute(
                "UPDATE comparison_task SET category_code = ? WHERE id = ?",
                (category_code, conn.execute(
                    "SELECT task_id FROM quote WHERE id = ?", (quote_id,)
                ).fetchone()[0]),
            )
        # 重跑 L1+L2；LLMError 上抛由 API 层转 502（不降级）
        run_mapping(quote_id, conn)
        data, path = _load_snapshot(conn, quote_id)

    if changed_derived:
        _sync_quote_derived(conn, quote_id, data)

    _mark_reviewed(conn, quote_id, data)
    _save_snapshot(conn, quote_id, data, path)
    return _quote_summary(conn, quote_id, data)


def _line_dict(conn: sqlite3.Connection, line_id: int) -> dict:
    row = conn.execute(
        """SELECT id, quote_id, module, item_name, amount, atom_code, confidence,
                  match_path, confirm_status, note, is_new_process, bundle_flag,
                  candidate_atoms, fingerprint
           FROM quote_line WHERE id = ?""",
        (line_id,),
    ).fetchone()
    if row is None:
        raise CorrectionNotFound("明细条目不存在")
    return dict(row)


def patch_quote_line(conn: sqlite3.Connection, line_id: int, patch: dict) -> dict:
    line = _line_dict(conn, line_id)
    quote_id = line["quote_id"]
    module = line["module"]
    data, path = _load_snapshot(conn, quote_id)
    item, _idx = _snapshot_item(conn, data, quote_id, module, line_id)

    changed_derived = False

    if "amount" in patch:
        amount = patch["amount"]
        with conn:
            conn.execute(
                "UPDATE quote_line SET amount = ? WHERE id = ?", (amount, line_id)
            )
        item["amount_per_pc"] = amount
        changed_derived = True

    if "atom_code" in patch:
        code = patch["atom_code"]
        exists = conn.execute("SELECT 1 FROM atom WHERE code = ?", (code,)).fetchone()
        if exists is None:
            raise CorrectionError(f"原子不存在：{code}")
        fingerprint = make_fingerprint([code])
        with conn:
            conn.execute(
                """UPDATE quote_line SET atom_code = ?, match_path = 'manual',
                   confidence = 'high', confirm_status = 'corrected',
                   candidate_atoms = ?, fingerprint = ?, bundle_flag = 0,
                   is_new_process = 0 WHERE id = ?""",
                (code, json.dumps([code], ensure_ascii=False), fingerprint, line_id),
            )
        # 别名回流：原文写法进入词库（UNIQUE 约束，重复静默跳过）
        try:
            with conn:
                conn.execute(
                    """INSERT INTO atom_alias (atom_code, alias_text, source)
                       VALUES (?, ?, 'manual_feedback')""",
                    (code, line["item_name"]),
                )
        except sqlite3.IntegrityError:
            pass
        # 该条目挂起的新工艺建议视为已被人工修正合并
        with conn:
            conn.execute(
                """UPDATE new_atom_suggestion SET status = 'merged'
                   WHERE quote_line_id = ? AND status = 'pending'""",
                (line_id,),
            )
        item.update(
            {
                "atom_code": code,
                "match_path": "manual",
                "confidence": "high",
                "confirm_status": "corrected",
                "bundle_members": [code],
                "bundle_fingerprint": fingerprint,
                "bundle_flag": False,
                "split_method": "none",
                "is_new_process": False,
            }
        )

    if "note" in patch:
        with conn:
            conn.execute(
                "UPDATE quote_line SET note = ? WHERE id = ?", (patch["note"], line_id)
            )
        item["note"] = patch["note"]

    if "confirm_status" in patch:
        status = patch["confirm_status"]
        if status not in CONFIRM_STATUS_ENUM:
            raise CorrectionError(
                f"confirm_status 非法：{status}（可选 {CONFIRM_STATUS_ENUM}）"
            )
        with conn:
            conn.execute(
                "UPDATE quote_line SET confirm_status = ? WHERE id = ?", (status, line_id)
            )
        item["confirm_status"] = status

    if changed_derived:
        _sync_quote_derived(conn, quote_id, data)
    else:
        # 仅映射/状态修正：重算 flags（如 unmatched 消失）
        check = conn.execute(
            "SELECT calc_check FROM quote WHERE id = ?", (quote_id,)
        ).fetchone()["calc_check"]
        flags = collect_flags(data, check or "unchecked")
        with conn:
            conn.execute(
                "UPDATE quote SET flags = ? WHERE id = ?",
                (json.dumps(flags, ensure_ascii=False), quote_id),
            )

    _mark_reviewed(conn, quote_id, data)
    _save_snapshot(conn, quote_id, data, path)
    return _line_dict(conn, line_id)

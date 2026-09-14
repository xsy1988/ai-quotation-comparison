"""机械对比引擎（纯脚本，SQL 聚合）：层级金额对比 + 加工费维度抽屉 + 指纹对齐 + 模治具 + 警示汇总。

空值语义：供应商未报某模块/抽屉时值为 null（前端显示"/"），绝不当作 0。
"""

import json
import re
import sqlite3
from typing import Any

HIERARCHY_ROWS: list[tuple[str, str, str]] = [
    ("materials", "材料费", "module"),
    ("processing", "加工费", "module"),
    ("inspection", "检验费", "module"),
    ("packaging_transport", "包装运输费", "module"),
    ("sga_tax", "损管利税", "module"),
    ("other", "其他费用", "module"),
    ("untaxed_total", "未税合计", "summary"),
    ("tax_amount", "税额", "summary"),
    ("taxed_total", "含税合计", "summary"),
    ("discount", "折扣", "summary"),
    ("final_unit_price_taxed", "最终含税单价", "summary"),
]

UNMATCHED_BUCKET_CODE = "unmatched"


def _is_shared_item(note: str | None, amount) -> bool:
    """共享单元格去重的置零副本（derive 规则 A）：note 带共享标记且金额已置 0。
    前端据此把金额显示为"/"，备注在 tooltip 展示。"""
    return amount == 0 and bool(note) and "共享单元格" in note


def _task_quotes(conn: sqlite3.Connection, task_id: int) -> list[sqlite3.Row]:
    """任务内参与比价的报价单，按「同一供应商相邻」排序。

    排序键 = 供应商首次出现的序号，组内按 quote id 升序：同一供应商的多份报价（多产品/
    多方案）在比价表里必须挨在一起，否则用户要跨列比对同一家供应商的报价（历史缺陷）。
    供应商身份一律按 supplier_name 分组：新供应商的 supplier_code 为空，按 code 会把
    同名供应商拆散。此顺序是全量下游顺序（含 AI 分析的供应商列顺序）的唯一来源。
    """
    rows = list(
        conn.execute(
            "SELECT id, supplier_name, supplier_code, project_code, flags, calc_check,"
            " final_unit_price_taxed, category_code, basic_info, other_info"
            " FROM quote WHERE task_id = ? AND parse_status IN ('parsed', 'reviewed') ORDER BY id",
            (task_id,),
        )
    )
    group_order: dict[str, int] = {}
    for row in rows:
        key = _supplier_key(row)
        group_order.setdefault(key, len(group_order))
    rows.sort(key=lambda row: (group_order[_supplier_key(row)], row["id"]))
    return rows


def _supplier_key(row: sqlite3.Row) -> str:
    """供应商分组键：以展示名称为准（表格列头就是名称），名称缺失时退回 supplier_code。"""
    name = (row["supplier_name"] or "").strip()
    if name:
        return f"name:{name}"
    return f"code:{(row['supplier_code'] or '').strip()}"


def _suppliers(quotes: list[sqlite3.Row]) -> list[dict]:
    suppliers = []
    for row in quotes:
        basic = json.loads(row["basic_info"] or "{}")
        suppliers.append(
            {
                "quote_id": row["id"],
                "supplier_name": row["supplier_name"],
                "supplier_code": row["supplier_code"],
                "project_code": row["project_code"],
                "part_name": basic.get("part_name"),
                "scheme": basic.get("scheme"),
                "moq": basic.get("moq"),
                "moq_options": basic.get("moq_options"),
                "flags": json.loads(row["flags"] or "[]"),
                "calc_check": row["calc_check"],
                "final_unit_price_taxed": row["final_unit_price_taxed"],
                "category_code": row["category_code"],
                "other_info": row["other_info"],
            }
        )
    return suppliers


def _hierarchy(conn: sqlite3.Connection, quote_ids: list[int]) -> list[dict]:
    rows: list[dict] = []
    for key, label, source in HIERARCHY_ROWS:
        if source == "module":
            sql = f"SELECT id, {key}_total AS amount FROM quote WHERE id = ?"
        else:
            sql = f"SELECT id, {key} AS amount FROM quote WHERE id = ?"
        values: dict[int, float | None] = {}
        for qid in quote_ids:
            if source == "summary" and key == "taxed_total":
                values[qid] = None  # quote 表无此列，下方统一推导
                continue
            row = conn.execute(sql, (qid,)).fetchone()
            amount = row["amount"] if row else None
            values[qid] = round(amount, 6) if amount is not None else None
        rows.append({"key": key, "label": label, "values": values})
    # quote 表无 taxed_total 列：含税合计 = 未税 + 税额（缺则按 最终+折扣 回推）
    taxed_row = next(r for r in rows if r["key"] == "taxed_total")
    untaxed_row = next(r for r in rows if r["key"] == "untaxed_total")
    tax_row = next(r for r in rows if r["key"] == "tax_amount")
    final_row = next(r for r in rows if r["key"] == "final_unit_price_taxed")
    discount_row = next(r for r in rows if r["key"] == "discount")
    for qid in quote_ids:
        untaxed, tax = untaxed_row["values"][qid], tax_row["values"][qid]
        final, discount = final_row["values"][qid], discount_row["values"][qid]
        if untaxed is not None and tax is not None:
            taxed_row["values"][qid] = round(untaxed + tax, 6)
        elif final is not None:
            taxed_row["values"][qid] = round(final + (discount or 0), 6)
        else:
            taxed_row["values"][qid] = None
    return rows


def _processing_details(conn: sqlite3.Connection, quote_ids: list[int]) -> list[dict]:
    result = []
    for qid in quote_ids:
        items = [
            {
                "id": row["id"],
                "name": row["item_name"],
                "amount": row["amount"],
                "atom_code": row["atom_code"],
                "confidence": row["confidence"],
                "fingerprint": row["fingerprint"],
                "bundle_flag": bool(row["bundle_flag"]),
                "is_new_process": bool(row["is_new_process"]),
                "match_path": row["match_path"],
                "note": row["note"],
                "is_shared": _is_shared_item(row["note"], row["amount"]),
            }
            for row in conn.execute(
                "SELECT id, item_name, amount, atom_code, confidence, fingerprint,"
                " bundle_flag, is_new_process, match_path, note"
                " FROM quote_line WHERE quote_id = ? AND module = 'processing' ORDER BY id",
                (qid,),
            )
        ]
        result.append({"quote_id": qid, "module": "processing", "items": items})
    return result


def _drawer_bucket(conn: sqlite3.Connection, quote_ids: list[int], members: list[str]) -> dict[int, float | None]:
    values: dict[int, float | None] = {}
    if not members:
        return {qid: None for qid in quote_ids}
    if members == [None]:
        for qid in quote_ids:
            total = conn.execute(
                "SELECT SUM(amount) FROM quote_line"
                " WHERE quote_id = ? AND module = 'processing'"
                " AND atom_code IS NULL",
                (qid,),
            ).fetchone()[0]
            values[qid] = round(total, 6) if total is not None else None
        return values
    placeholders = ",".join("?" for _ in members)
    for qid in quote_ids:
        total = conn.execute(
            f"SELECT SUM(amount) FROM quote_line"
            f" WHERE quote_id = ? AND module = 'processing' AND atom_code IN ({placeholders})",
            (qid, *members),
        ).fetchone()[0]
        values[qid] = round(total, 6) if total is not None else None
    return values


def _drawers(conn: sqlite3.Connection, quote_ids: list[int]) -> list[dict]:
    drawers = list(conn.execute("SELECT code, name FROM drawer ORDER BY sort_order, code"))
    if not drawers:
        return []
    codes = [d["code"] for d in drawers]
    groups = list(
        conn.execute(
            f"SELECT group_code, group_name, scope, member_atoms FROM dim_group"
            f" WHERE scope IN ({','.join('?' for _ in codes)}) ORDER BY scope, group_code",
            codes,
        )
    )
    by_scope: dict[str, list[sqlite3.Row]] = {code: [] for code in codes}
    for row in groups:
        if row["scope"] in by_scope:
            by_scope[row["scope"]].append(row)

    result: list[dict] = []
    for drawer in drawers:
        scope = drawer["code"]
        scope_groups: list[dict] = []
        for row in by_scope[scope]:
            # 空成员组不下发（避免与未匹配桶重复计数）
            members = json.loads(row["member_atoms"] or "[]")
            if not members:
                continue
            scope_groups.append(
                {
                    "group_code": row["group_code"],
                    "group_name": row["group_name"],
                    "values": _drawer_bucket(conn, quote_ids, members),
                    "is_fallback_bucket": False,
                }
            )
        scope_groups.append(
            {
                "group_code": UNMATCHED_BUCKET_CODE,
                "group_name": "未匹配",
                "values": _drawer_bucket(conn, quote_ids, [None]),
                "is_fallback_bucket": True,
            }
        )
        result.append({"scope": scope, "name": drawer["name"], "groups": scope_groups})
    return result


def _fingerprint_groups(conn: sqlite3.Connection, quote_ids: list[int]) -> list[dict]:
    if not quote_ids:
        return []
    placeholders = ",".join("?" for _ in quote_ids)
    rows = conn.execute(
        f"SELECT fingerprint, quote_id, item_name, amount FROM quote_line"
        f" WHERE module = 'processing' AND fingerprint IS NOT NULL AND fingerprint != ''"
        f" AND quote_id IN ({placeholders}) ORDER BY fingerprint, quote_id",
        quote_ids,
    ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["fingerprint"], []).append(
            {"quote_id": row["quote_id"], "item_name": row["item_name"], "amount": row["amount"]}
        )
    # 指纹是「|」连接的原子编码，前端只有代号没法读；一次性查表补上原子名称
    names = {
        row["code"]: row["name"]
        for row in conn.execute("SELECT code, name FROM atom").fetchall()
    }
    return [
        {
            "fingerprint": fp,
            "atoms": [
                {"code": code, "name": names.get(code)} for code in fp.split("|") if code
            ],
            "rows": group_rows,
        }
        for fp, group_rows in sorted(grouped.items())
    ]


def _tooling(conn: sqlite3.Connection, quote_ids: list[int]) -> list[dict]:
    result = []
    for tooling_type in ("mold", "fixture", "stencil"):
        items = [
            {
                "quote_id": row["quote_id"],
                "item_name": row["item_name"],
                "amount": row["amount"],
                "cavity_count": row["cavity_count"],
                "lifespan": row["lifespan"],
            }
            for row in conn.execute(
                "SELECT quote_id, item_name, amount, cavity_count, lifespan FROM tooling_line"
                " WHERE tooling_type = ? ORDER BY quote_id, id",
                (tooling_type,),
            )
            if row["quote_id"] in set(quote_ids)
        ]
        result.append({"type": tooling_type, "items": items})
    return result


def _warnings(conn: sqlite3.Connection, quotes: list[sqlite3.Row]) -> list[dict]:
    result = []
    for row in quotes:
        counts = conn.execute(
            "SELECT"
            " SUM(CASE WHEN confidence = 'low' THEN 1 ELSE 0 END) AS low_confidence,"
            " SUM(CASE WHEN atom_code IS NULL THEN 1 ELSE 0 END) AS unmatched,"
            " SUM(CASE WHEN is_new_process = 1 THEN 1 ELSE 0 END) AS new_process"
            " FROM quote_line WHERE module = 'processing' AND quote_id = ?",
            (row["id"],),
        ).fetchone()
        result.append(
            {
                "quote_id": row["id"],
                "flags": json.loads(row["flags"] or "[]"),
                "line_counts": {
                    "low_confidence": counts["low_confidence"] or 0,
                    "unmatched": counts["unmatched"] or 0,
                    "new_process": counts["new_process"] or 0,
                },
            }
        )
    return result


def _basic(quotes: list[sqlite3.Row]) -> list[dict]:
    """每供应商基本信息：quote.basic_info JSON 解析，字段缺失给 None。"""
    fields = (
        "project_name",
        "part_name",
        "material_spec",
        "quote_date",
        "currency",
        "moq",
        "moq_options",
    )
    result = []
    for row in quotes:
        info = json.loads(row["basic_info"] or "{}") or {}
        result.append({"quote_id": row["id"], **{f: info.get(f) for f in fields}})
    return result


def moq_option_text(options: Any) -> str | None:
    """起订量分档的单元格文本：每档一行「条件 3,000」，不限条件的档写「不限条件」；无分档返回 None。"""
    if not isinstance(options, list) or not options:
        return None
    lines: list[str] = []
    for item in options:
        if not isinstance(item, dict) or item.get("value") is None:
            continue
        condition = item.get("condition") or "不限条件"
        note = item.get("note")
        suffix = f"（{note}）" if note else ""
        lines.append(f"{condition} {int(item['value']):,}{suffix}")
    return "\n".join(lines) if lines else None


SGA_NON_TAX_TYPES = ("损耗", "管理费", "利润", "其他")

_BASIC_TREE_ROWS: list[tuple[str, str]] = [
    ("project_name", "项目名称"),
    ("part_name", "零件名称"),
    ("material_spec", "材料规格"),
    ("quote_date", "报价时间"),
    ("currency", "币种"),
    ("moq", "最小起订量"),
]

_TOOLING_TREE_TYPES: list[tuple[str, str]] = [
    ("mold", "模具费"),
    ("fixture", "治具费"),
    ("stencil", "钢网费"),
]


def _module_totals(conn: sqlite3.Connection, quote_ids: list[int]) -> dict[str, dict[int, float | None]]:
    """六个模块合计（quote 表 total 列），取值与 hierarchy 模块行一致。"""
    totals: dict[str, dict[int, float | None]] = {}
    for module in ("materials", "processing", "inspection", "packaging_transport", "sga_tax", "other"):
        col = f"{module}_total"
        values: dict[int, float | None] = {}
        for qid in quote_ids:
            row = conn.execute(f"SELECT {col} AS amount FROM quote WHERE id = ?", (qid,)).fetchone()
            amount = row["amount"] if row else None
            values[qid] = round(amount, 6) if amount is not None else None
        totals[module] = values
    return totals


def _module_detail_rows(
    conn: sqlite3.Connection, quote_ids: list[int], module: str
) -> list[dict]:
    """明细行按 (quote_id, item_name) 聚合：同名列多条合并求和；item_type/rate/note 取其一。"""
    if not quote_ids:
        return []
    placeholders = ",".join("?" for _ in quote_ids)
    rows = conn.execute(
        f"SELECT quote_id, item_name, MIN(item_type) AS item_type, MIN(rate) AS rate,"
        f" MIN(note) AS note, SUM(amount) AS amount"
        f" FROM quote_line WHERE module = ? AND quote_id IN ({placeholders})"
        f" GROUP BY quote_id, item_name ORDER BY item_name, quote_id",
        (module, *quote_ids),
    )
    return [dict(r) for r in rows]


def _typed_detail_children(
    rows: list[dict], quote_ids: list[int], prefix: str,
    order: tuple[str, ...] = (), labels: dict[str, str] | None = None,
) -> list[dict]:
    """损管利/包装运输/税费等固定结构模块的明细 children：按 item_type 跨供应商对齐
    （不良率/损耗→损耗一行，管理/管理费用→管理费一行），无 type 的条目回退按名称；
    标签取规范名（labels 映射），行序按给定结构顺序，其余排尾。meta 保留 rate/note/原文 name。"""
    labels = labels or {}

    def key_of(r: dict) -> str:
        return r["item_type"] or r["item_name"]

    keys: list[str] = [t for t in order if any(key_of(r) == t for r in rows)]
    for r in sorted(rows, key=lambda r: (str(key_of(r)), r["quote_id"])):
        if key_of(r) not in keys:
            keys.append(key_of(r))

    children = []
    for key in keys:
        values: dict[int, float | None] = {}
        meta: dict[int, dict] = {}
        for qid in quote_ids:
            matches = [r for r in rows if r["quote_id"] == qid and key_of(r) == key]
            if not matches:
                values[qid] = None
                continue
            amounts = [m["amount"] for m in matches if m["amount"] is not None]
            values[qid] = round(sum(amounts), 6) if amounts else None
            first = matches[0]
            entry = {"item_type": first["item_type"], "name": first["item_name"]}
            for f in ("rate", "note"):
                if first.get(f) is not None:
                    entry[f] = first[f]
            meta[qid] = entry
        children.append(
            {
                "key": f"{prefix}::{key}",
                "label": labels.get(key, key),
                "kind": "amount",
                "values": values,
                "meta": meta,
            }
        )
    return children


def _processing_children(
    quote_ids: list[int], processing_details: list[dict],
    atom_names: dict[str, str], atom_scope: dict[str, dict],
) -> list[dict]:
    """加工费明细 children：按原子码跨供应商对齐（同名同事序一行对比，如 CNC/cnc 合并），
    未匹配条目按归一化名称（忽略大小写）对齐；行序取路线出现顺序。
    meta 带 quote_line id/atom/confidence/match_path/原文 name 等供就地编辑；
    is_shared 标记共享单元格去重的置零副本（金额显示"/"）；
    scope_meta 带原子所属工艺域/阶段/类别，供前端切换分组展示。"""
    by_quote = {d["quote_id"]: d["items"] for d in processing_details}

    def group_key(it: dict) -> str:
        return f"atom:{it['atom_code']}" if it.get("atom_code") else f"name:{it['name'].casefold()}"

    order: list[str] = []
    for qid in quote_ids:
        for it in by_quote.get(qid, []):
            key = group_key(it)
            if key not in order:
                order.append(key)

    children = []
    for key in order:
        values: dict[int, float | None] = {}
        meta: dict[int, dict] = {}
        for qid in quote_ids:
            matches = [it for it in by_quote.get(qid, []) if group_key(it) == key]
            if not matches:
                values[qid] = None
                continue
            amounts = [it["amount"] for it in matches if it["amount"] is not None]
            values[qid] = round(sum(amounts), 6) if amounts else None
            first = matches[0]
            meta[qid] = {
                "id": first["id"],
                "atom_code": first["atom_code"],
                "confidence": first["confidence"],
                "match_path": first.get("match_path"),
                "is_new_process": first["is_new_process"],
                "bundle_flag": first["bundle_flag"],
                "fingerprint": first["fingerprint"],
                "note": first["note"],
                "name": first["name"],
                "is_shared": first["is_shared"],
            }
        if key.startswith("atom:"):
            code = key[len("atom:"):]
            label = atom_names.get(code, code)
            scope_meta = atom_scope.get(code)
        else:
            label = next(iter(meta.values()))["name"]
            scope_meta = None
        children.append(
            {
                "key": f"processing::{key}",
                "label": label,
                "kind": "amount",
                "values": values,
                "meta": meta,
                "scope_meta": scope_meta,
            }
        )
    return children


_GENERIC_MATERIAL_BASES = ("材料费", "材料费用", "原材料", "材料", "原料", "材质", "金属材料")
_GENERIC_MATERIAL_RE = re.compile(r"^(?:材料费|材料费用|原材料|材料|原料|材质|金属材料)\s*([（(].*[)）])$")


def _material_display_name(item_name: str | None, spec: str | None) -> str | None:
    """材料条目标题回退：LLM 把条目命名为栏目名（原材料/材料费等）时，
    用 basic.material_spec 展示真实材料（牌号），括号后缀保留。"""
    if not item_name or not spec:
        return item_name
    stripped = item_name.strip()
    if stripped in _GENERIC_MATERIAL_BASES:
        return spec
    m = _GENERIC_MATERIAL_RE.match(stripped)
    if m:
        return f"{spec}{m.group(1)}"
    return item_name


def _material_children(
    conn: sqlite3.Connection, quote_ids: list[int], material_spec: dict[int, str | None],
) -> list[dict]:
    """材料费明细 children：材料名称为自由文本，无法按名对齐；
    按各家报价内出现位置跨供应商对齐（第 N 种材料同一行），
    单元格 meta 带各家自己的材料名称与备注，前端名称+金额同格展示。
    条目名是栏目名（原材料/材料费等）时回退用 material_spec 展示。"""
    if not quote_ids:
        return []
    placeholders = ",".join("?" for _ in quote_ids)
    rows = [
        dict(r)
        for r in conn.execute(
            f"SELECT quote_id, item_name, MIN(note) AS note, SUM(amount) AS amount"
            f" FROM quote_line WHERE module = 'materials' AND quote_id IN ({placeholders})"
            f" GROUP BY quote_id, item_name ORDER BY quote_id, MIN(id)",
            quote_ids,
        )
    ]
    by_quote: dict[int, list[dict]] = {qid: [] for qid in quote_ids}
    for r in rows:
        by_quote[r["quote_id"]].append(r)
    max_count = max((len(v) for v in by_quote.values()), default=0)
    children = []
    for idx in range(max_count):
        values: dict[int, float | None] = {}
        meta: dict[int, dict] = {}
        for qid in quote_ids:
            items = by_quote[qid]
            if idx >= len(items):
                values[qid] = None
                continue
            item = items[idx]
            amount = item["amount"]
            values[qid] = round(amount, 6) if amount is not None else None
            entry = {"name": _material_display_name(item["item_name"], material_spec.get(qid))}
            if item.get("note") is not None:
                entry["note"] = item["note"]
            meta[qid] = entry
        children.append(
            {
                "key": f"materials::{idx}",
                "label": f"材料 {idx + 1}",
                "kind": "amount",
                "values": values,
                "meta": meta,
            }
        )
    return children


def _sga_values(
    totals: dict[int, float | None], tax_rows: list[dict], non_tax_rows: list[dict],
    quote_ids: list[int],
) -> dict[int, float | None]:
    """损管利展示值 = sga_tax 模块合计 − 税费明细合计。
    取舍：若差为 0 且原合计全部来自税费，回退按非税费明细合计取值
    （报价单 total 只含税费但损管利条目有金额时，如惠州豪泽单）；
    两者皆无（供应商只报了税费）→ None；未报整个模块（合计为 None）→ None，均不按 0 处理。"""
    tax_sum_by_q: dict[int, float] = {}
    for row in tax_rows:
        if row["amount"] is not None:
            tax_sum_by_q[row["quote_id"]] = round(
                tax_sum_by_q.get(row["quote_id"], 0) + row["amount"], 6
            )
    non_tax_sum_by_q: dict[int, float] = {}
    for row in non_tax_rows:
        if row["amount"] is not None:
            non_tax_sum_by_q[row["quote_id"]] = round(
                non_tax_sum_by_q.get(row["quote_id"], 0) + row["amount"], 6
            )
    values: dict[int, float | None] = {}
    for qid in quote_ids:
        total = totals.get(qid)
        if total is None:
            values[qid] = None
            continue
        tax_sum = tax_sum_by_q.get(qid)
        if tax_sum is None:
            values[qid] = total
            continue
        diff = round(total - tax_sum, 6)
        if diff != 0:
            values[qid] = diff
            continue
        # 合计与税费相等：优先取非税费明细合计，确实没有损管利条目才视为未报
        non_tax_sum = non_tax_sum_by_q.get(qid)
        values[qid] = non_tax_sum if non_tax_sum else None
    return values


def _tooling_tree(conn: sqlite3.Connection, quote_ids: list[int]) -> list[dict]:
    children = []
    quote_id_set = set(quote_ids)
    for tooling_type, label in _TOOLING_TREE_TYPES:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, quote_id, item_name, amount, cavity_count, lifespan, note"
                " FROM tooling_line WHERE tooling_type = ? ORDER BY quote_id, id",
                (tooling_type,),
            )
            if r["quote_id"] in quote_id_set
        ]
        values: dict[int, float | None] = {}
        for qid in quote_ids:
            amounts = [r["amount"] for r in rows if r["quote_id"] == qid and r["amount"] is not None]
            values[qid] = round(sum(amounts), 6) if amounts else None
        node: dict = {"key": f"tooling_{tooling_type}", "label": label, "kind": "group", "values": values}
        if rows:
            # 明细按名称跨供应商聚合：同名一行，金额按 (名称, quote) 求和，meta 取该供应商首条
            order: list[str] = []
            for r in rows:
                if r["item_name"] not in order:
                    order.append(r["item_name"])
            detail_nodes = []
            for name in order:
                detail_values: dict[int, float | None] = {}
                meta: dict[int, dict] = {}
                for qid in quote_ids:
                    qrows = [r for r in rows if r["item_name"] == name and r["quote_id"] == qid]
                    amounts = [r["amount"] for r in qrows if r["amount"] is not None]
                    detail_values[qid] = round(sum(amounts), 6) if amounts else None
                    if qrows:
                        meta[qid] = {
                            f: qrows[0][f]
                            for f in ("cavity_count", "lifespan", "note")
                            if qrows[0][f] is not None
                        }
                detail_nodes.append(
                    {
                        "key": f"tooling_{tooling_type}::{name}",
                        "label": name,
                        "kind": "amount",
                        "values": detail_values,
                        "meta": meta,
                    }
                )
            node["children"] = detail_nodes
        children.append(node)
    return children


def _price_tree(
    conn: sqlite3.Connection, quote_ids: list[int], basic: list[dict], processing_details: list[dict]
) -> list[dict]:
    """报价单固有结构的嵌套对比树（基本信息 / 产品单价（含税） / 模治具费用）。"""
    totals = _module_totals(conn, quote_ids)
    atom_names = {
        r["code"]: r["name"]
        for r in conn.execute("SELECT code, name FROM atom")
    }
    atom_scope = {
        r["code"]: {
            "domain": {"code": r["domain_code"], "name": r["domain_name"]},
            "stage": {"code": r["stage_name"], "name": r["stage_name"]},
            "class": {"code": r["class_name"], "name": r["class_name"]},
        }
        for r in conn.execute(
            "SELECT a.code, a.domain_code, a.stage_name, a.class_name, d.name AS domain_name"
            " FROM atom a LEFT JOIN process_domain d ON d.code = a.domain_code"
        )
    }

    def summary_values(col: str) -> dict[int, float | None]:
        values: dict[int, float | None] = {}
        for qid in quote_ids:
            row = conn.execute(f"SELECT {col} AS amount FROM quote WHERE id = ?", (qid,)).fetchone()
            amount = row["amount"] if row else None
            values[qid] = round(amount, 6) if amount is not None else None
        return values

    basic_by_q = {b["quote_id"]: b for b in basic}
    basic_children = [
        {
            "key": f"basic_{field}",
            "label": label,
            "kind": "text",
            "values": {qid: basic_by_q.get(qid, {}).get(field) for qid in quote_ids},
        }
        for field, label in _BASIC_TREE_ROWS
    ]
    # 多条件起订量（现货/定制…分档）只有真的分档时才多出一行，避免常态多一行空白
    moq_options_cells = {
        qid: moq_option_text(basic_by_q.get(qid, {}).get("moq_options")) for qid in quote_ids
    }
    if any(value is not None for value in moq_options_cells.values()):
        index = next(
            (i + 1 for i, (field, _) in enumerate(_BASIC_TREE_ROWS) if field == "moq"),
            len(basic_children),
        )
        basic_children.insert(
            index,
            {
                "key": "basic_moq_options",
                "label": "起订量分档",
                "kind": "text",
                "values": moq_options_cells,
            },
        )

    sga_rows = _module_detail_rows(conn, quote_ids, "sga_tax")
    sga_tax_rows = [r for r in sga_rows if r["item_type"] == "税费"]
    sga_non_tax_rows = [r for r in sga_rows if r["item_type"] in SGA_NON_TAX_TYPES]
    other_rows = _module_detail_rows(conn, quote_ids, "other")
    has_other = any(v is not None for v in totals["other"].values()) or bool(other_rows)

    def group(key: str, label: str, values: dict[int, float | None], children: list[dict]) -> dict:
        node: dict = {"key": key, "label": label, "kind": "group", "values": values}
        if children:
            node["children"] = children
        return node

    unit_children: list[dict] = [
        {"key": "final", "label": "计算总价（含税）", "kind": "amount",
         "values": summary_values("final_unit_price_taxed")},
        {"key": "untaxed", "label": "计算总价（未税）", "kind": "amount",
         "values": summary_values("untaxed_total")},
        {"key": "discount", "label": "折扣（含税）", "kind": "amount",
         "values": summary_values("discount")},
        group("materials", "材料费", totals["materials"],
              _material_children(conn, quote_ids,
                                 {b["quote_id"]: b["material_spec"] for b in basic})),
        group("processing", "生产加工费", totals["processing"],
              _processing_children(quote_ids, processing_details, atom_names, atom_scope)),
        group("inspection", "检验费", totals["inspection"],
              _typed_detail_children(_module_detail_rows(conn, quote_ids, "inspection"),
                                     quote_ids, "inspection")),
        group("packaging_transport", "包装运输费", totals["packaging_transport"],
              _typed_detail_children(_module_detail_rows(conn, quote_ids, "packaging_transport"),
                                     quote_ids, "packaging_transport",
                                     order=("包装", "运输"),
                                     labels={"包装": "包装费", "运输": "运输费"})),
        group("sga", "损管利", _sga_values(totals["sga_tax"], sga_tax_rows, sga_non_tax_rows, quote_ids),
              _typed_detail_children(sga_non_tax_rows, quote_ids, "sga",
                                     order=SGA_NON_TAX_TYPES)),
        group("tax", "税费",
              {qid: (round(sum(r["amount"] for r in sga_tax_rows
                               if r["quote_id"] == qid and r["amount"] is not None), 6)
                     if any(r["quote_id"] == qid and r["amount"] is not None for r in sga_tax_rows)
                     else None)
               for qid in quote_ids},
              _typed_detail_children(sga_tax_rows, quote_ids, "tax", order=("税费",))),
    ]
    if has_other:
        unit_children.append(
            group("other", "其他费用", totals["other"],
                  _typed_detail_children(other_rows, quote_ids, "other"))
        )

    return [
        {"key": "basic", "label": "基本信息", "kind": "group",
         "values": {}, "children": basic_children},
        {"key": "unit_price", "label": "产品单价（含税）", "kind": "group",
         "values": summary_values("final_unit_price_taxed"), "children": unit_children},
        {"key": "tooling", "label": "模/治具费用", "kind": "group",
         "values": summary_values("tooling_total"), "children": _tooling_tree(conn, quote_ids)},
    ]


def get_comparison(conn: sqlite3.Connection, task_id: int) -> dict:
    quotes = _task_quotes(conn, task_id)
    quote_ids = [row["id"] for row in quotes]
    basic = _basic(quotes)
    processing_details = _processing_details(conn, quote_ids)
    return {
        "task_id": task_id,
        "suppliers": _suppliers(quotes),
        "hierarchy": _hierarchy(conn, quote_ids),
        "processing_details": processing_details,
        "drawers": _drawers(conn, quote_ids),
        "fingerprint_groups": _fingerprint_groups(conn, quote_ids),
        "tooling": _tooling(conn, quote_ids),
        "warnings": _warnings(conn, quotes),
        "basic": basic,
        "price_tree": _price_tree(conn, quote_ids, basic, processing_details),
    }

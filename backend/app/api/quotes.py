"""报价单数据 API（第 3 步产物）：已解析报价单列表 + 单份解析结果详情。

列表页只给轻量摘要（供应商/归属任务/品类/含税单价/解析状态），
详情页给「解析结果」全貌：六模块明细、工艺原子匹配情况、模治具、其它信息。
"""

import json

from fastapi import APIRouter, HTTPException, Query

from app.db import get_connection, init_db

router = APIRouter(prefix="/api/quotes", tags=["quotes"])

_MODULES = (
    ("materials", "材料费"),
    ("processing", "加工费"),
    ("inspection", "检验费"),
    ("packaging_transport", "包装运输费"),
    ("sga_tax", "损管利税"),
    ("other", "其它费用"),
)
_TOOLING_TYPES = (("mold", "模具"), ("fixture", "治具"), ("stencil", "钢网"))


def _flags(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _quote_or_404(conn, quote_id: int):
    row = conn.execute(
        """SELECT q.*, t.project_name FROM quote q
           LEFT JOIN comparison_task t ON t.id = q.task_id WHERE q.id = ?""",
        (quote_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="报价单不存在")
    return row


@router.get("")
def list_quotes(
    q: str | None = Query(None, description="供应商名称/零件名称模糊搜索"),
    parse_status: str | None = Query(None, description="解析状态过滤"),
    task_id: int | None = Query(None, description="按归属任务过滤"),
) -> dict:
    init_db()
    conn = get_connection()
    try:
        where: list[str] = []
        params: list = []
        if parse_status:
            where.append("q.parse_status = ?")
            params.append(parse_status)
        if task_id is not None:
            where.append("q.task_id = ?")
            params.append(task_id)
        if q:
            where.append(
                "(q.supplier_name LIKE ? OR json_extract(q.basic_info, '$.part_name') LIKE ?)"
            )
            params.extend([f"%{q}%", f"%{q}%"])

        rows = conn.execute(
            f"""SELECT q.id, q.task_id, t.project_name, q.supplier_name, q.category_code,
                       c.name AS category_name, q.basic_info, q.final_unit_price_taxed,
                       q.tooling_total, q.flags, q.calc_check, q.parse_status, q.created_at,
                       (q.other_info IS NOT NULL AND q.other_info <> '') AS has_other_info,
                       (SELECT COUNT(*) FROM quote_line l WHERE l.quote_id = q.id) AS line_count
                FROM quote q
                LEFT JOIN comparison_task t ON t.id = q.task_id
                LEFT JOIN category c ON c.code = q.category_code
                {'WHERE ' + ' AND '.join(where) if where else ''}
                ORDER BY q.id DESC""",
            params,
        )
        items = []
        for row in rows:
            basic = json.loads(row["basic_info"] or "{}") or {}
            items.append(
                {
                    "quote_id": row["id"],
                    "task_id": row["task_id"],
                    "project_name": row["project_name"],
                    "supplier_name": row["supplier_name"],
                    "part_name": basic.get("part_name"),
                    "category_code": row["category_code"],
                    "category_name": row["category_name"],
                    "final_unit_price_taxed": row["final_unit_price_taxed"],
                    "tooling_total": row["tooling_total"],
                    "calc_check": row["calc_check"],
                    "flags": _flags(row["flags"]),
                    "parse_status": row["parse_status"],
                    "line_count": row["line_count"],
                    "has_other_info": bool(row["has_other_info"]),
                    "created_at": row["created_at"],
                }
            )
        return {"items": items}
    finally:
        conn.close()


@router.get("/{quote_id}")
def quote_detail(quote_id: int) -> dict:
    """单份报价单解析结果：基本信息 + 六模块明细 + 模治具 + 其它信息。"""
    init_db()
    conn = get_connection()
    try:
        row = _quote_or_404(conn, quote_id)
        basic = json.loads(row["basic_info"] or "{}") or {}

        lines = conn.execute(
            """SELECT id, module, item_name, item_type, amount, unit, rate, atom_code,
                      is_new_process, bundle_flag, candidate_atoms, fingerprint, confidence,
                      match_path, cross_check, confirm_status, note, evidence
               FROM quote_line WHERE quote_id = ? ORDER BY module, id""",
            (quote_id,),
        ).fetchall()
        atom_names = {
            r["code"]: r["name"]
            for r in conn.execute(
                "SELECT code, name FROM atom WHERE code IN (SELECT DISTINCT atom_code"
                " FROM quote_line WHERE quote_id = ? AND atom_code IS NOT NULL)",
                (quote_id,),
            )
        }
        items = []
        for line in lines:
            items.append(
                {
                    "id": line["id"],
                    "module": line["module"],
                    "item_name": line["item_name"],
                    "item_type": line["item_type"],
                    "amount": line["amount"],
                    "unit": line["unit"],
                    "rate": line["rate"],
                    "atom_code": line["atom_code"],
                    "atom_name": atom_names.get(line["atom_code"]),
                    "is_new_process": bool(line["is_new_process"]),
                    "bundle_flag": bool(line["bundle_flag"]),
                    "candidate_atoms": json.loads(line["candidate_atoms"] or "[]"),
                    "fingerprint": line["fingerprint"],
                    "confidence": line["confidence"],
                    "match_path": line["match_path"],
                    "cross_check": json.loads(line["cross_check"] or "null"),
                    "confirm_status": line["confirm_status"],
                    "note": line["note"],
                    "evidence": json.loads(line["evidence"] or "null"),
                }
            )

        tooling_rows = conn.execute(
            "SELECT tooling_type, item_name, amount, cavity_count, lifespan, note"
            " FROM tooling_line WHERE quote_id = ?"
            " ORDER BY CASE tooling_type WHEN 'mold' THEN 1 WHEN 'fixture' THEN 2 ELSE 3 END, id",
            (quote_id,),
        ).fetchall()

        return {
            "quote_id": row["id"],
            "task_id": row["task_id"],
            "project_name": row["project_name"],
            "supplier_name": row["supplier_name"],
            "supplier_code": row["supplier_code"],
            "category_code": row["category_code"],
            "basic": basic,
            "parse_status": row["parse_status"],
            "calc_check": row["calc_check"],
            "flags": _flags(row["flags"]),
            "modules": [
                {"module": key, "name": label, "total": row[f"{key}_total"]}
                for key, label in _MODULES
            ],
            "summary": {
                "untaxed_total": row["untaxed_total"],
                "tax_amount": row["tax_amount"],
                "discount": row["discount"],
                "final_unit_price_taxed": row["final_unit_price_taxed"],
                "tooling_total": row["tooling_total"],
            },
            "lines": items,
            "tooling": [
                {
                    "tooling_type": t["tooling_type"],
                    "type_name": dict(_TOOLING_TYPES).get(t["tooling_type"], t["tooling_type"]),
                    "item_name": t["item_name"],
                    "amount": t["amount"],
                    "cavity_count": t["cavity_count"],
                    "lifespan": t["lifespan"],
                    "note": t["note"],
                }
                for t in tooling_rows
            ],
            "other_info": row["other_info"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    finally:
        conn.close()

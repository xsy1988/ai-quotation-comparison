"""供应商历史报价 API（迭代：供应商历史报价曲线）。

数据源是已解析报价单的六模块合计 / 税费 / 含税单价 / 模治具费合计，
按品类、时间区间、指标过滤，可选叠加另一家供应商做同期对比。

口径说明：金额均为「元/pcs」（模/治具费为整单金额，量纲与单价不同，前端给出提示）；
未识别到的指标返回 null，曲线断点而不补 0。
"""

import json

from fastapi import APIRouter, HTTPException, Query

from app.db import get_connection, init_db

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])

# 指标白名单：key -> (列名, 中文名)。顺序即前端展示顺序
METRIC_COLUMNS: dict[str, tuple[str, str]] = {
    "final_unit_price_taxed": ("final_unit_price_taxed", "含税单价"),
    "materials_total": ("materials_total", "材料费"),
    "processing_total": ("processing_total", "生产加工费"),
    "inspection_total": ("inspection_total", "检验费"),
    "packaging_transport_total": ("packaging_transport_total", "包装运输费"),
    "sga_tax_total": ("sga_tax_total", "损管利税"),
    "tax_amount": ("tax_amount", "税费"),
    "other_total": ("other_total", "其它费用"),
    "tooling_total": ("tooling_total", "模/治具费"),
}
DEFAULT_METRICS = ("final_unit_price_taxed",)

_PARSED_STATUSES = ("parsed", "reviewed")


def _supplier_or_404(conn, code: str):
    row = conn.execute(
        "SELECT code, name, alias FROM supplier WHERE code = ?", (code,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"供应商不存在：{code}")
    return row


def _metric_keys(metrics: str | None) -> list[str]:
    keys = [m.strip() for m in (metrics or "").split(",") if m.strip()]
    if not keys:
        return list(DEFAULT_METRICS)
    invalid = [k for k in keys if k not in METRIC_COLUMNS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"指标非法：{', '.join(invalid)}（可选 {', '.join(METRIC_COLUMNS)}）",
        )
    # 去重并按白名单顺序返回，保证曲线图例顺序稳定
    return [k for k in METRIC_COLUMNS if k in keys]


def _date_of(basic: dict, created_at: str | None) -> tuple[str, str]:
    """报价日期：优先 basic_info.quote_date，缺失时按录入日期兜底（date_source 标注来源）。"""
    raw = str(basic.get("quote_date") or "").strip()
    if raw:
        return raw[:10], "quote"
    return (created_at or "")[:10], "created"


def _quote_points(
    conn,
    codes: list[str],
    metric_keys: list[str],
    category_code: str | None,
    date_from: str | None,
    date_to: str | None,
) -> list[dict]:
    """按供应商编码集合取报价点（多供应商时用于叠加对比曲线）。"""
    if not codes:
        return []
    names = {row["code"]: row["name"] for row in conn.execute("SELECT code, name FROM supplier")}
    selected = [
        "q.id",
        "q.supplier_code",
        "q.task_id",
        "q.category_code",
        "q.basic_info",
        "q.created_at",
        "c.name AS category_name",
    ]
    selected += [f"q.{METRIC_COLUMNS[k][0]} AS {k}" for k in metric_keys]
    placeholders = ", ".join("?" for _ in codes)
    rows = conn.execute(
        f"""SELECT {', '.join(selected)}
            FROM quote q
            LEFT JOIN category c ON c.code = q.category_code
            WHERE q.supplier_code IN ({placeholders})
              AND q.parse_status IN (?, ?)
            ORDER BY q.id""",
        (*codes, *_PARSED_STATUSES),
    ).fetchall()

    points = []
    for row in rows:
        if category_code and row["category_code"] != category_code:
            continue
        try:
            basic = json.loads(row["basic_info"] or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            basic = {}
        date, date_source = _date_of(basic, row["created_at"])
        if date_from and date < date_from:
            continue
        if date_to and date > date_to:
            continue
        points.append(
            {
                "supplier_code": row["supplier_code"],
                "supplier_name": names.get(row["supplier_code"]),
                "quote_id": row["id"],
                "task_id": row["task_id"],
                "date": date,
                "date_source": date_source,
                "category_code": row["category_code"],
                "category_name": row["category_name"],
                "project_name": basic.get("project_name"),
                "part_name": basic.get("part_name"),
                "scheme": basic.get("scheme"),
                "metrics": {k: row[k] for k in metric_keys},
            }
        )
    points.sort(key=lambda p: (p["date"], p["quote_id"]))
    return points


@router.get("/{code}/history")
def supplier_history(
    code: str,
    metrics: str | None = Query(default=None, description="逗号分隔的指标 key，可多选"),
    category_code: str | None = Query(default=None),
    date_from: str | None = Query(default=None, description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD"),
    compare_code: str | None = Query(default=None, description="叠加对比的供应商编码"),
) -> dict:
    """某供应商的历史报价点 + 可选另加一家供应商同期对比。"""
    init_db()
    conn = get_connection()
    try:
        supplier = _supplier_or_404(conn, code)
        metric_keys = _metric_keys(metrics)
        compare = None
        if compare_code and compare_code != code:
            compare = _supplier_or_404(conn, compare_code)
        codes = [code] + ([compare_code] if compare else [])
        points = _quote_points(
            conn, codes, metric_keys, category_code or None, date_from or None, date_to or None
        )
        # 品类下拉：该供应商出现过的品类
        categories = [
            {"code": row["code"], "name": row["name"]}
            for row in conn.execute(
                """SELECT DISTINCT c.code, c.name FROM quote q
                   JOIN category c ON c.code = q.category_code
                   WHERE q.supplier_code = ? AND q.parse_status IN (?, ?)
                   ORDER BY c.code""",
                (code, *_PARSED_STATUSES),
            )
        ]
        total_quotes = conn.execute(
            f"""SELECT COUNT(*) FROM quote
                WHERE supplier_code = ? AND parse_status IN ({', '.join('?' for _ in _PARSED_STATUSES)})""",
            (code, *_PARSED_STATUSES),
        ).fetchone()[0]
        return {
            "supplier": {"code": supplier["code"], "name": supplier["name"]},
            "compare_supplier": (
                {"code": compare["code"], "name": compare["name"]} if compare else None
            ),
            "metrics": [{"key": k, "label": METRIC_COLUMNS[k][1]} for k in metric_keys],
            "metric_options": [
                {"key": k, "label": label} for k, (_col, label) in METRIC_COLUMNS.items()
            ],
            "categories": categories,
            "points": points,
            # quote_count 是该供应商的报价单总数（不受筛选影响）；filtered_count 为当前筛选命中数
            "quote_count": total_quotes,
            "filtered_count": sum(1 for p in points if p["supplier_code"] == code),
        }
    finally:
        conn.close()


@router.get("/{code}/quotes")
def supplier_quotes(code: str, limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    """供应商历史报价明细（曲线页表格用）。"""
    init_db()
    conn = get_connection()
    try:
        supplier = _supplier_or_404(conn, code)
        points = _quote_points(conn, [code], list(METRIC_COLUMNS), None, None, None)
        return {
            "supplier": {"code": supplier["code"], "name": supplier["name"]},
            "points": points[-limit:],
        }
    finally:
        conn.close()

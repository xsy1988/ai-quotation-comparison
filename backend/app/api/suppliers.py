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


def _split_codes(raw: str | None) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def _process_clause(
    domain_codes: list[str], atom_codes: list[str]
) -> tuple[str, list[str]]:
    """工艺域/原子工艺筛选 → 报价级 EXISTS 子句（只保留命中工艺的报价单）。

    语义：不改变指标口径（纵轴仍是整单费用细项），只是把没做这些工艺的报价单排除在外。
    """
    if not domain_codes and not atom_codes:
        return "", []
    conds: list[str] = []
    params: list[str] = []
    if domain_codes:
        conds.append(f"a.domain_code IN ({', '.join('?' for _ in domain_codes)})")
        params += domain_codes
    if atom_codes:
        conds.append(f"l.atom_code IN ({', '.join('?' for _ in atom_codes)})")
        params += atom_codes
    clause = (
        " AND EXISTS (SELECT 1 FROM quote_line l JOIN atom a ON a.code = l.atom_code"
        f" WHERE l.quote_id = q.id AND {' AND '.join(conds)})"
    )
    return clause, params


def _matched_process(
    conn,
    quote_ids: list[int],
    domain_codes: list[str],
    atom_codes: list[str],
) -> dict[int, dict]:
    """每份报价单命中的工艺（工艺域 + 原子工艺名称）。

    未选工艺筛选时返回该报价单的全部已匹配原子；选了筛选时只返回命中的部分。
    """
    if not quote_ids:
        return {}
    conds: list[str] = []
    params: list = list(quote_ids)
    if domain_codes:
        conds.append(f"a.domain_code IN ({', '.join('?' for _ in domain_codes)})")
        params += domain_codes
    if atom_codes:
        conds.append(f"l.atom_code IN ({', '.join('?' for _ in atom_codes)})")
        params += atom_codes
    where = " AND " + " AND ".join(conds) if conds else ""
    rows = conn.execute(
        f"""SELECT l.quote_id, a.code, a.name, a.domain_code, d.name AS domain_name
            FROM quote_line l
            JOIN atom a ON a.code = l.atom_code
            LEFT JOIN process_domain d ON d.code = a.domain_code
            WHERE l.quote_id IN ({', '.join('?' for _ in quote_ids)}){where}
            GROUP BY l.quote_id, a.code
            ORDER BY a.domain_code, a.code""",
        params,
    ).fetchall()

    matched: dict[int, dict] = {}
    for row in rows:
        slot = matched.setdefault(row["quote_id"], {"atoms": [], "domains": []})
        slot["atoms"].append({"code": row["code"], "name": row["name"]})
        domain = {"code": row["domain_code"], "name": row["domain_name"]}
        if domain not in slot["domains"]:
            slot["domains"].append(domain)
    return matched


def _process_options(conn, codes: list[str]) -> tuple[list[dict], list[dict]]:
    """可选项：该供应商（含对比供应商）历史报价中出现过的工艺域 / 原子工艺。

    只给出「选了能有结果」的选项，避免几百条主数据原子里绝大多数点了是空图。
    """
    if not codes:
        return [], []
    placeholders = ", ".join("?" for _ in codes)
    base_sql = f"""FROM quote_line l
            JOIN quote q ON q.id = l.quote_id
            JOIN atom a ON a.code = l.atom_code
            LEFT JOIN process_domain d ON d.code = a.domain_code
            WHERE q.supplier_code IN ({placeholders})
              AND q.parse_status IN (?, ?)"""
    params = (*codes, *_PARSED_STATUSES)
    atom_rows = conn.execute(
        f"""SELECT a.code, a.name, a.domain_code, COUNT(DISTINCT l.quote_id) AS quote_count
            {base_sql}
            GROUP BY a.code
            ORDER BY a.domain_code, a.code""",
        params,
    ).fetchall()
    # 域的去重报价单数不能由原子的计数相加得到（同一份报价单可命中同域多个原子）
    domain_rows = conn.execute(
        f"""SELECT a.domain_code AS code, d.name AS name, COUNT(DISTINCT l.quote_id) AS quote_count
            {base_sql}
            GROUP BY a.domain_code
            ORDER BY a.domain_code""",
        params,
    ).fetchall()
    atoms = [
        {
            "code": row["code"],
            "name": row["name"],
            "domain_code": row["domain_code"],
            "quote_count": row["quote_count"],
        }
        for row in atom_rows
    ]
    domains = [
        {
            "code": row["code"],
            "name": row["name"] or row["code"],
            "quote_count": row["quote_count"],
        }
        for row in domain_rows
    ]
    return domains, atoms


def _quote_points(
    conn,
    codes: list[str],
    metric_keys: list[str],
    category_code: str | None,
    date_from: str | None,
    date_to: str | None,
    domain_codes: list[str] | None = None,
    atom_codes: list[str] | None = None,
) -> list[dict]:
    """按供应商编码集合取报价点（多供应商时用于叠加对比曲线）。"""
    if not codes:
        return []
    process_clause, process_params = _process_clause(domain_codes or [], atom_codes or [])
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
              AND q.parse_status IN (?, ?){process_clause}
            ORDER BY q.id""",
        (*codes, *_PARSED_STATUSES, *process_params),
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
    matched = _matched_process(
        conn, [p["quote_id"] for p in points], domain_codes or [], atom_codes or []
    )
    for point in points:
        hit = matched.get(point["quote_id"]) or {"atoms": [], "domains": []}
        point["matched_atoms"] = hit["atoms"]
        point["matched_domains"] = hit["domains"]
    return points


@router.get("/{code}/history")
def supplier_history(
    code: str,
    metrics: str | None = Query(default=None, description="逗号分隔的指标 key，可多选"),
    category_code: str | None = Query(default=None),
    date_from: str | None = Query(default=None, description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD"),
    domain_codes: str | None = Query(default=None, description="逗号分隔的工艺域 code，可多选"),
    atom_codes: str | None = Query(default=None, description="逗号分隔的原子工艺 code，可多选"),
    compare_code: str | None = Query(default=None, description="叠加对比的供应商编码"),
) -> dict:
    """某供应商的历史报价点 + 可选另加一家供应商同期对比（工艺域/原子工艺按报价单级过滤）。"""
    init_db()
    conn = get_connection()
    try:
        supplier = _supplier_or_404(conn, code)
        metric_keys = _metric_keys(metrics)
        compare = None
        if compare_code and compare_code != code:
            compare = _supplier_or_404(conn, compare_code)
        selected_domains = _split_codes(domain_codes)
        selected_atoms = _split_codes(atom_codes)
        codes = [code] + ([compare_code] if compare else [])
        points = _quote_points(
            conn,
            codes,
            metric_keys,
            category_code or None,
            date_from or None,
            date_to or None,
            selected_domains,
            selected_atoms,
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
        domain_options, atom_options = _process_options(conn, codes)
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
            "domain_options": domain_options,
            "atom_options": atom_options,
            "process_filter": {"domain_codes": selected_domains, "atom_codes": selected_atoms},
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

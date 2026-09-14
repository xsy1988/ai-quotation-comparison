"""供应商 / 项目的识别结果绑定（迭代：供应商管理深入 + 新增业务数据项目）。

背景：报价单解析只落 quote.supplier_name（自由文本）与 basic_info.project_name，
历史实现不维护 supplier 主数据，导致「供应商管理」永远是空的。本模块负责：

- 名称归一化匹配：全半角、大小写、空格差异视为同一家供应商；
- 供应商注册：自动分配 SUP-xxx 编码，并把全部同名报价单挂上 supplier_code；
- 项目注册 / 绑定 / 解绑：项目 = 公司内部的一个 SKU，按识别名或显式报价单 id 绑定；
- 任务级匹配视图：比价页面「供应商 / 项目管理」模块的数据源（列出未管理项）。

绑定一律「显式报价单 id 优先、识别名归一化兜底」：用户可以把识别名改成规范名后新建，
此时仍靠显式 id 完成绑定。
"""

import json
import re
import sqlite3
import unicodedata

SUPPLIER_CODE_RE = re.compile(r"^SUP-(\d+)$")
PROJECT_CODE_RE = re.compile(r"^PRJ-(\d+)$")
_ALIAS_SEPARATOR_RE = re.compile(r"[、,，;；/|\n\r]+")
# 参与匹配的报价单状态：解析失败/未完成的报价单不参与绑定
_MATCHABLE_STATUSES = ("parsed", "reviewed")


class MasterBindingError(Exception):
    """绑定类业务错误（名称为空、编码冲突等），API 层转 400。"""


class MasterBindingConflict(MasterBindingError):
    """唯一性冲突（项目名/编码重复），API 层转 409。"""


def normalize_name(text: str | None) -> str:
    """名称归一化：NFKC（全角→半角）+ 去空白 + 统一小写，用于同名判定。"""
    if not text:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text))).casefold()


def _alias_list(alias: str | None) -> list[str]:
    if not alias:
        return []
    return [part.strip() for part in _ALIAS_SEPARATOR_RE.split(alias) if part.strip()]


def _quote_rows(conn: sqlite3.Connection, task_id: int | None = None) -> list[sqlite3.Row]:
    """可匹配的报价单：带任务名（项目名缺失时作为兜底识别名）。"""
    sql = """SELECT q.id, q.supplier_name, q.supplier_code, q.project_code, q.category_code, q.basic_info,
                    t.project_name AS task_project_name
             FROM quote q
             JOIN comparison_task t ON t.id = q.task_id
             WHERE q.parse_status IN (?, ?)"""
    params: list = list(_MATCHABLE_STATUSES)
    if task_id is not None:
        sql += " AND q.task_id = ?"
        params.append(task_id)
    return list(conn.execute(sql + " ORDER BY q.id", params))


def quote_project_name(row: sqlite3.Row) -> str:
    """报价单识别出的项目名：basic_info.project_name；缺失时退回所属任务名。"""
    try:
        basic = json.loads(row["basic_info"] or "{}") or {}
    except (json.JSONDecodeError, TypeError):
        basic = {}
    name = str(basic.get("project_name") or "").strip()
    return name or str(row["task_project_name"] or "").strip()


# ========== 编码生成 ==========


def _next_code(conn: sqlite3.Connection, table: str, pattern: re.Pattern, prefix: str) -> str:
    max_seq = 0
    for row in conn.execute(f"SELECT code FROM {table}"):
        match = pattern.match(row["code"] or "")
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    return f"{prefix}{max_seq + 1:03d}"


def _insert_with_retry(
    conn: sqlite3.Connection,
    table: str,
    pattern: re.Pattern,
    prefix: str,
    columns: tuple[str, ...],
    values: tuple,
) -> str:
    """生成顺序编码并插入；并发撞号（IntegrityError）时重新取号再试。"""
    sql = (
        f"INSERT INTO {table} (code, {', '.join(columns)})"
        f" VALUES ({', '.join(['?'] * (len(columns) + 1))})"
    )
    last_error: Exception | None = None
    for _attempt in range(3):
        code = _next_code(conn, table, pattern, prefix)
        try:
            with conn:
                conn.execute(sql, (code, *values))
            return code
        except sqlite3.IntegrityError as e:
            last_error = e
    raise MasterBindingError(f"{table} 编码生成冲突，请重试：{last_error}")


def _bind_quotes(
    conn: sqlite3.Connection, column: str, value: str | None, quote_ids: list[int]
) -> int:
    """批量挂载/解绑（value=None 表示解绑）；只更新真正发生变化的行。"""
    if column not in ("supplier_code", "project_code"):
        raise MasterBindingError(f"非法绑定列：{column}")
    changed = 0
    with conn:
        for quote_id in quote_ids:
            cursor = conn.execute(
                f"""UPDATE quote SET {column} = ?, updated_at = datetime('now', 'localtime')
                    WHERE id = ? AND IFNULL({column}, '') <> IFNULL(?, '')""",
                (value, quote_id, value),
            )
            changed += cursor.rowcount
    return changed


# ========== 供应商 ==========


def find_supplier_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """按归一化名称/别名查供应商（别名可用、，;/ 等分隔维护多个）。"""
    target = normalize_name(name)
    if not target:
        return None
    for row in conn.execute("SELECT * FROM supplier ORDER BY code"):
        if any(
            normalize_name(c) == target for c in (row["name"], *_alias_list(row["alias"]))
        ):
            return row
    return None


def bind_supplier_quotes(conn: sqlite3.Connection, code: str) -> int:
    """把该供应商全部同名报价单挂上 supplier_code（跨任务），返回新绑定条数。"""
    row = conn.execute("SELECT * FROM supplier WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise MasterBindingError(f"供应商不存在：{code}")
    names = {normalize_name(n) for n in (row["name"], *_alias_list(row["alias"]))}
    names.discard("")
    pending = [
        q["id"]
        for q in _quote_rows(conn)
        if normalize_name(q["supplier_name"]) in names and q["supplier_code"] != code
    ]
    return _bind_quotes(conn, "supplier_code", code, pending)


def register_supplier(
    conn: sqlite3.Connection,
    name: str,
    alias: str | None = None,
    quote_ids: list[int] | None = None,
) -> dict:
    """注册供应商：同名（含别名）已存在则复用，否则自动编码新建；随后绑定报价单。"""
    name = (name or "").strip()
    if not name:
        raise MasterBindingError("供应商名称不能为空")
    existing = find_supplier_by_name(conn, name)
    code = existing["code"] if existing is not None else None
    created = False
    if code is None:
        code = _insert_with_retry(
            conn, "supplier", SUPPLIER_CODE_RE, "SUP-", ("name", "alias"), (name, alias)
        )
        created = True
    row = conn.execute("SELECT * FROM supplier WHERE code = ?", (code,)).fetchone()
    bound = bind_supplier_quotes(conn, code) + bind_supplier_codes(conn, code, quote_ids or [])
    return {
        "code": row["code"],
        "name": row["name"],
        "alias": row["alias"],
        "created": created,
        "bound_quotes": bound,
    }


def bind_supplier_codes(conn: sqlite3.Connection, code: str, quote_ids: list[int]) -> int:
    """显式绑定（识别名与主数据名不一致时使用，例如关联到已有供应商）。"""
    if conn.execute("SELECT 1 FROM supplier WHERE code = ?", (code,)).fetchone() is None:
        raise MasterBindingError(f"供应商不存在：{code}")
    return _bind_quotes(conn, "supplier_code", code, quote_ids)


# ========== 项目 ==========


def project_to_dict(row: sqlite3.Row, quote_count: int | None = None) -> dict:
    data = {
        "code": row["code"],
        "name": row["name"],
        "category_code": row["category_code"],
        "remark": row["remark"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if quote_count is not None:
        data["quote_count"] = quote_count
    return data


def project_quote_count(conn: sqlite3.Connection, code: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM quote WHERE project_code = ?", (code,)
    ).fetchone()[0]


def find_project_by_name(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    target = normalize_name(name)
    if not target:
        return None
    for row in conn.execute("SELECT * FROM project ORDER BY code"):
        if normalize_name(row["name"]) == target:
            return row
    return None


def bind_project_quotes(conn: sqlite3.Connection, code: str) -> int:
    """把识别名与项目名一致的报价单全部绑定（跨任务），返回新绑定条数。"""
    row = conn.execute("SELECT * FROM project WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise MasterBindingError(f"项目不存在：{code}")
    target = normalize_name(row["name"])
    pending = [
        q["id"]
        for q in _quote_rows(conn)
        if normalize_name(quote_project_name(q)) == target and q["project_code"] != code
    ]
    return _bind_quotes(conn, "project_code", code, pending)


def create_project(
    conn: sqlite3.Connection,
    name: str,
    category_code: str | None = None,
    remark: str | None = None,
    quote_ids: list[int] | None = None,
    code: str | None = None,
) -> dict:
    """新建项目并按识别名 + 显式 id 绑定报价单。同名项目提示复用（409）。"""
    name = (name or "").strip()
    if not name:
        raise MasterBindingError("项目名称不能为空")
    if find_project_by_name(conn, name) is not None:
        raise MasterBindingConflict(f"项目名称已存在：{name}")
    if code:
        if conn.execute("SELECT 1 FROM project WHERE code = ?", (code,)).fetchone() is not None:
            raise MasterBindingConflict(f"项目编码已存在：{code}")
        with conn:
            conn.execute(
                "INSERT INTO project (code, name, category_code, remark) VALUES (?, ?, ?, ?)",
                (code, name, category_code, remark),
            )
    else:
        code = _insert_with_retry(
            conn,
            "project",
            PROJECT_CODE_RE,
            "PRJ-",
            ("name", "category_code", "remark"),
            (name, category_code, remark),
        )
    bound = bind_project_quotes(conn, code) + bind_project_codes(conn, code, quote_ids or [])
    row = conn.execute("SELECT * FROM project WHERE code = ?", (code,)).fetchone()
    return {**project_to_dict(row, project_quote_count(conn, code)), "bound_quotes": bound}


def bind_project_codes(conn: sqlite3.Connection, code: str, quote_ids: list[int]) -> int:
    if conn.execute("SELECT 1 FROM project WHERE code = ?", (code,)).fetchone() is None:
        raise MasterBindingError(f"项目不存在：{code}")
    return _bind_quotes(conn, "project_code", code, quote_ids)


def unbind_project_codes(conn: sqlite3.Connection, code: str, quote_ids: list[int]) -> int:
    if conn.execute("SELECT 1 FROM project WHERE code = ?", (code,)).fetchone() is None:
        raise MasterBindingError(f"项目不存在：{code}")
    return _bind_quotes(conn, "project_code", None, quote_ids)


# ========== 任务级匹配视图 ==========


def _quote_extra(conn: sqlite3.Connection, quote_id: int, column: str) -> str | None:
    row = conn.execute(f"SELECT {column} FROM quote WHERE id = ?", (quote_id,)).fetchone()
    return row[column] if row else None


def _resolved_code(conn: sqlite3.Connection, quote_ids: list[int], column: str) -> str | None:
    """组内报价单的绑定编码；全部一致且非空才算绑定完成，部分绑定视为未绑定。"""
    codes = [_quote_extra(conn, qid, column) for qid in quote_ids]
    if not codes or not codes[0]:
        return None
    return codes[0] if all(code == codes[0] for code in codes) else None


def task_master_match(conn: sqlite3.Connection, task_id: int) -> dict:
    """比价页面「供应商 / 项目管理」模块的数据源（未管理/未绑定的排前面）。"""
    rows = _quote_rows(conn, task_id)
    suppliers = _group_suppliers(conn, rows)
    projects = _group_projects(conn, rows)
    return {
        "task_id": task_id,
        "suppliers": suppliers,
        "projects": projects,
        "unmanaged_supplier_count": sum(1 for s in suppliers if not s["managed"]),
        "unbound_project_count": sum(1 for p in projects if not p["bound"]),
    }


def _group_suppliers(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for row in rows:
        name = (row["supplier_name"] or "").strip() or "未知供应商"
        bucket = buckets.setdefault(
            normalize_name(name),
            {"name": name, "quote_ids": [], "part_names": set(), "categories": set()},
        )
        bucket["quote_ids"].append(row["id"])
        if row["category_code"]:
            bucket["categories"].add(row["category_code"])
        try:
            basic = json.loads(row["basic_info"] or "{}") or {}
        except (json.JSONDecodeError, TypeError):
            basic = {}
        if basic.get("part_name"):
            bucket["part_names"].add(str(basic["part_name"]))

    result = []
    for normalized, bucket in buckets.items():
        code = _resolved_code(conn, bucket["quote_ids"], "supplier_code")
        master = (
            conn.execute("SELECT name FROM supplier WHERE code = ?", (code,)).fetchone()
            if code
            else None
        )
        result.append(
            {
                "name": bucket["name"],
                "normalized": normalized,
                "quote_ids": bucket["quote_ids"],
                "quote_count": len(bucket["quote_ids"]),
                "part_names": sorted(bucket["part_names"]),
                "categories": sorted(bucket["categories"]),
                "managed": code is not None,
                "supplier_code": code,
                "supplier_name": master["name"] if master else None,
            }
        )
    result.sort(key=lambda item: (item["managed"], item["name"]))
    return result


def _group_projects(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for row in rows:
        name = quote_project_name(row)
        if not name:
            continue
        bucket = buckets.setdefault(normalize_name(name), {"name": name, "quote_ids": []})
        bucket["quote_ids"].append(row["id"])

    result = []
    for normalized, bucket in buckets.items():
        code = _resolved_code(conn, bucket["quote_ids"], "project_code")
        project = (
            conn.execute("SELECT name FROM project WHERE code = ?", (code,)).fetchone()
            if code
            else None
        )
        result.append(
            {
                "name": bucket["name"],
                "normalized": normalized,
                "quote_ids": bucket["quote_ids"],
                "quote_count": len(bucket["quote_ids"]),
                "bound": code is not None,
                "project_code": code,
                "project_name": project["name"] if project else None,
            }
        )
    result.sort(key=lambda item: (item["bound"], item["name"]))
    return result

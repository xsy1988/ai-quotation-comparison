"""主数据管理 API（第 9 步）：原子 / 别名 / 品类 / 抽屉分组 / 供应商 / 项目的 CRUD。

- 独立命名空间 /api/master/*，不动前端在用的 /api/atoms、/api/categories 等只读接口；
- 原子编码自动生成：复用 new_atom_service._next_atom_code（域内 max+1，撞号重试）；
- 供应商/项目编码自动生成（SUP-xxx / PRJ-xxx），注册时按名称归一化去重并绑定报价单；
- 删除保护：被业务数据（quote_line / atom_category / atom_alias / comparison_task / quote 等）
  引用的主数据禁止删除，返回 409 + 中文提示；DB 层 IntegrityError 统一转 409。
"""

import json
import re
import sqlite3

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.services.master_binding import (
    MasterBindingConflict,
    MasterBindingError,
    bind_project_codes,
    bind_supplier_codes,
    create_project,
    project_quote_count,
    project_to_dict,
    register_supplier,
    unbind_project_codes,
)
from app.services.new_atom_service import _next_atom_code

router = APIRouter(prefix="/api/master", tags=["master"])

CATEGORY_CODE_RE = re.compile(r"^CAT-[A-Z0-9]+$")
DRAWER_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
# 不参与对比抽屉的历史 scope（自定义分组只用于原子归档）；抽屉编码由 drawer 表提供
NON_DRAWER_SCOPE = "custom"


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _not_found(label: str, key: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{label}不存在：{key}")


def _ref_count(conn: sqlite3.Connection, sql: str, key: str) -> int:
    return conn.execute(sql, (key,)).fetchone()[0]


def _check_atom_members(conn: sqlite3.Connection, member_atoms: list[str]) -> None:
    for code in member_atoms:
        if conn.execute("SELECT 1 FROM atom WHERE code = ?", (code,)).fetchone() is None:
            raise _bad(f"原子不存在：{code}")


def _drawer_codes(conn: sqlite3.Connection) -> set[str]:
    return {row["code"] for row in conn.execute("SELECT code FROM drawer")}


def _check_dim_scope(conn: sqlite3.Connection, scope: str) -> None:
    """分组 scope 必须是某个抽屉编码（进对比抽屉）或 non-drawer 的 custom。"""
    codes = _drawer_codes(conn)
    if scope != NON_DRAWER_SCOPE and scope not in codes:
        options = ", ".join([NON_DRAWER_SCOPE, *sorted(codes)])
        raise _bad(f"scope 非法：{scope}（可选 {options}）")


# ========== 原子 ==========


def _atom_categories(conn: sqlite3.Connection, atom_code: str) -> list[str]:
    return [
        row["name"]
        for row in conn.execute(
            """SELECT c.name FROM atom_category ac JOIN category c ON c.code = ac.category_code
               WHERE ac.atom_code = ? ORDER BY c.code""",
            (atom_code,),
        )
    ]


def _atom_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    return {
        "code": row["code"],
        "name": row["name"],
        "domain_code": row["domain_code"],
        "domain_name": row["domain_name"],
        "stage_name": row["stage_name"],
        "class_name": row["class_name"],
        "remark": row["remark"],
        "is_fallback": bool(row["is_fallback"]),
        "categories": _atom_categories(conn, row["code"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _fetch_atom(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute(
        """SELECT a.*, d.name AS domain_name FROM atom a
           JOIN process_domain d ON d.code = a.domain_code WHERE a.code = ?""",
        (code,),
    ).fetchone()
    if row is None:
        raise _not_found("原子", code)
    return row


@router.get("/atoms")
def list_atoms(q: str = Query(default="")) -> dict:
    init_db()
    conn = get_connection()
    try:
        like = f"%{q}%"
        rows = list(
            conn.execute(
                """SELECT a.*, d.name AS domain_name FROM atom a
                   JOIN process_domain d ON d.code = a.domain_code
                   WHERE ? = '' OR a.code LIKE ? OR a.name LIKE ?
                   ORDER BY a.code""",
                (q, like, like),
            )
        )
        return {"atoms": [_atom_to_dict(conn, row) for row in rows]}
    finally:
        conn.close()


class AtomCreate(BaseModel):
    name: str
    domain_code: str
    stage_name: str
    class_name: str
    remark: str | None = None
    category_codes: list[str] | None = None


class AtomPatch(BaseModel):
    name: str | None = None
    stage_name: str | None = None
    class_name: str | None = None
    remark: str | None = None

    def to_patch(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


def _check_atom_refs(conn: sqlite3.Connection, field: str, value: str, label: str) -> None:
    if conn.execute(f"SELECT 1 FROM {field} WHERE code = ?", (value,)).fetchone() is None:
        raise _bad(f"{label}不存在：{value}")


def _check_atom_enums(conn: sqlite3.Connection, body: dict) -> None:
    if conn.execute(
        "SELECT 1 FROM process_domain WHERE code = ?", (body["domain_code"],)
    ).fetchone() is None:
        raise _bad(f"工艺域不存在：{body['domain_code']}")
    if conn.execute(
        "SELECT 1 FROM process_stage WHERE name = ?", (body["stage_name"],)
    ).fetchone() is None:
        raise _bad(f"工艺阶段不存在：{body['stage_name']}")
    if conn.execute(
        "SELECT 1 FROM process_class WHERE name = ?", (body["class_name"],)
    ).fetchone() is None:
        raise _bad(f"工艺类别不存在：{body['class_name']}")


@router.post("/atoms", status_code=201)
def create_atom(body: AtomCreate) -> dict:
    init_db()
    conn = get_connection()
    try:
        payload = body.model_dump()
        if not payload["name"].strip():
            raise _bad("字段 name 不能为空")
        _check_atom_enums(conn, payload)
        for category_code in payload.get("category_codes") or []:
            _check_atom_refs(conn, "category", category_code, "品类")

        # 编码自动生成（复用 _next_atom_code，撞号重试）
        code: str | None = None
        for _attempt in range(3):
            candidate = _next_atom_code(conn, payload["domain_code"])
            try:
                with conn:
                    conn.execute(
                        """INSERT INTO atom (code, name, domain_code, stage_name, class_name, remark)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            candidate,
                            payload["name"].strip(),
                            payload["domain_code"],
                            payload["stage_name"],
                            payload["class_name"],
                            payload.get("remark"),
                        ),
                    )
                code = candidate
                break
            except sqlite3.IntegrityError:
                continue
        if code is None:
            raise _conflict(f"原子编码生成冲突，请重试：{payload['domain_code']}")

        for category_code in payload.get("category_codes") or []:
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO atom_category (atom_code, category_code) VALUES (?, ?)",
                        (code, category_code),
                    )
            except sqlite3.IntegrityError:
                pass
        return _atom_to_dict(conn, _fetch_atom(conn, code))
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.patch("/atoms/{code}")
def patch_atom(code: str, body: AtomPatch) -> dict:
    init_db()
    conn = get_connection()
    try:
        _fetch_atom(conn, code)
        patch = body.to_patch()
        if not patch:
            return _atom_to_dict(conn, _fetch_atom(conn, code))
        if "stage_name" in patch and conn.execute(
            "SELECT 1 FROM process_stage WHERE name = ?", (patch["stage_name"],)
        ).fetchone() is None:
            raise _bad(f"工艺阶段不存在：{patch['stage_name']}")
        if "class_name" in patch and conn.execute(
            "SELECT 1 FROM process_class WHERE name = ?", (patch["class_name"],)
        ).fetchone() is None:
            raise _bad(f"工艺类别不存在：{patch['class_name']}")
        assignments = ", ".join(f"{k} = ?" for k in patch)
        with conn:
            conn.execute(
                f"UPDATE atom SET {assignments}, updated_at = datetime('now', 'localtime') WHERE code = ?",
                (*patch.values(), code),
            )
        return _atom_to_dict(conn, _fetch_atom(conn, code))
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.delete("/atoms/{code}")
def delete_atom(code: str) -> dict:
    init_db()
    conn = get_connection()
    try:
        _fetch_atom(conn, code)
        refs = {
            "别名": _ref_count(conn, "SELECT COUNT(*) FROM atom_alias WHERE atom_code = ?", code),
            "品类关联": _ref_count(conn, "SELECT COUNT(*) FROM atom_category WHERE atom_code = ?", code),
            "报价行": _ref_count(conn, "SELECT COUNT(*) FROM quote_line WHERE atom_code = ?", code),
        }
        used = {k: v for k, v in refs.items() if v > 0}
        if used:
            detail = "、".join(f"{k} {v} 条" for k, v in used.items())
            raise _conflict(f"原子已被引用，禁止删除（{detail}）")
        with conn:
            conn.execute("DELETE FROM atom WHERE code = ?", (code,))
        return {"code": code, "deleted": True}
    except sqlite3.IntegrityError as e:
        raise _conflict(f"原子已被引用，禁止删除：{e}")
    finally:
        conn.close()


# ========== 别名 ==========


def _alias_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "atom_code": row["atom_code"],
        "alias_text": row["alias_text"],
        "atom_name": row["atom_name"],
        "source": row["source"],
        "hit_count": row["hit_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/aliases")
def list_aliases(
    q: str = Query(default=""),
    sort: str = Query(default="id"),
) -> dict:
    init_db()
    conn = get_connection()
    try:
        like = f"%{q}%"
        order = "aa.hit_count DESC, aa.id" if sort == "hit" else "aa.id"
        rows = list(
            conn.execute(
                f"""SELECT aa.*, a.name AS atom_name FROM atom_alias aa
                   JOIN atom a ON a.code = aa.atom_code
                   WHERE ? = '' OR aa.alias_text LIKE ?
                   ORDER BY {order}""",
                (q, like),
            )
        )
        return {"aliases": [_alias_to_dict(row) for row in rows]}
    finally:
        conn.close()


class AliasCreate(BaseModel):
    atom_code: str
    alias_text: str


@router.post("/aliases", status_code=201)
def create_alias(body: AliasCreate) -> dict:
    init_db()
    conn = get_connection()
    try:
        if not body.alias_text.strip():
            raise _bad("字段 alias_text 不能为空")
        if conn.execute("SELECT 1 FROM atom WHERE code = ?", (body.atom_code,)).fetchone() is None:
            raise _not_found("原子", body.atom_code)
        try:
            with conn:
                cur = conn.execute(
                    """INSERT INTO atom_alias (atom_code, alias_text, source)
                       VALUES (?, ?, 'manual_feedback')""",
                    (body.atom_code, body.alias_text.strip()),
                )
        except sqlite3.IntegrityError:
            raise _bad("该原子下已存在相同别名")
        row = conn.execute(
            """SELECT aa.*, a.name AS atom_name FROM atom_alias aa
               JOIN atom a ON a.code = aa.atom_code WHERE aa.id = ?""",
            (cur.lastrowid,),
        ).fetchone()
        return _alias_to_dict(row)
    finally:
        conn.close()


@router.delete("/aliases/{alias_id}")
def delete_alias(alias_id: int) -> dict:
    init_db()
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM atom_alias WHERE id = ?", (alias_id,)).fetchone()
        if row is None:
            raise _not_found("别名", str(alias_id))
        with conn:
            conn.execute("DELETE FROM atom_alias WHERE id = ?", (alias_id,))
        return {"id": alias_id, "deleted": True}
    except sqlite3.IntegrityError as e:
        raise _conflict(f"别名已被引用，禁止删除：{e}")
    finally:
        conn.close()


# ========== 品类 ==========


def _category_to_dict(row: sqlite3.Row) -> dict:
    return {
        "code": row["code"],
        "name": row["name"],
        "atom_count": row["atom_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/categories")
def list_categories() -> dict:
    init_db()
    conn = get_connection()
    try:
        rows = list(
            conn.execute(
                """SELECT c.*, (SELECT COUNT(*) FROM atom_category ac
                                 WHERE ac.category_code = c.code) AS atom_count
                   FROM category c ORDER BY c.code"""
            )
        )
        return {"categories": [_category_to_dict(row) for row in rows]}
    finally:
        conn.close()


class CategoryCreate(BaseModel):
    code: str
    name: str


@router.post("/categories", status_code=201)
def create_category(body: CategoryCreate) -> dict:
    init_db()
    conn = get_connection()
    try:
        code = body.code.strip()
        name = body.name.strip()
        if not CATEGORY_CODE_RE.match(code):
            raise _bad("品类编码格式不正确，应为 CAT-XXX（大写字母或数字）")
        if not name:
            raise _bad("字段 name 不能为空")
        if conn.execute("SELECT 1 FROM category WHERE code = ?", (code,)).fetchone():
            raise _bad(f"品类编码已存在：{code}")
        if conn.execute("SELECT 1 FROM category WHERE name = ?", (name,)).fetchone():
            raise _bad(f"品类名称已存在：{name}")
        with conn:
            conn.execute("INSERT INTO category (code, name) VALUES (?, ?)", (code, name))
        row = conn.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM atom_category ac
                             WHERE ac.category_code = c.code) AS atom_count
               FROM category c WHERE c.code = ?""",
            (code,),
        ).fetchone()
        return _category_to_dict(row)
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


class CategoryPatch(BaseModel):
    name: str


@router.patch("/categories/{code}")
def patch_category(code: str, body: CategoryPatch) -> dict:
    init_db()
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM category WHERE code = ?", (code,)).fetchone() is None:
            raise _not_found("品类", code)
        name = body.name.strip()
        if not name:
            raise _bad("字段 name 不能为空")
        dup = conn.execute(
            "SELECT code FROM category WHERE name = ? AND code <> ?", (name, code)
        ).fetchone()
        if dup:
            raise _bad(f"品类名称已存在：{name}")
        with conn:
            conn.execute(
                "UPDATE category SET name = ?, updated_at = datetime('now', 'localtime') WHERE code = ?",
                (name, code),
            )
        row = conn.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM atom_category ac
                             WHERE ac.category_code = c.code) AS atom_count
               FROM category c WHERE c.code = ?""",
            (code,),
        ).fetchone()
        return _category_to_dict(row)
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.delete("/categories/{code}")
def delete_category(code: str) -> dict:
    init_db()
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM category WHERE code = ?", (code,)).fetchone() is None:
            raise _not_found("品类", code)
        refs = {
            "原子关联": _ref_count(conn, "SELECT COUNT(*) FROM atom_category WHERE category_code = ?", code),
            "对比任务": _ref_count(conn, "SELECT COUNT(*) FROM comparison_task WHERE category_code = ?", code),
            "报价单": _ref_count(conn, "SELECT COUNT(*) FROM quote WHERE category_code = ?", code),
        }
        used = {k: v for k, v in refs.items() if v > 0}
        if used:
            detail = "、".join(f"{k} {v} 条" for k, v in used.items())
            raise _conflict(f"品类已被引用，禁止删除（{detail}）")
        with conn:
            conn.execute("DELETE FROM category WHERE code = ?", (code,))
        return {"code": code, "deleted": True}
    except sqlite3.IntegrityError as e:
        raise _conflict(f"品类已被引用，禁止删除：{e}")
    finally:
        conn.close()


# ========== 对比抽屉 ==========


def _drawer_to_dict(row: sqlite3.Row, group_count: int = 0) -> dict:
    return {
        "code": row["code"],
        "name": row["name"],
        "is_builtin": bool(row["is_builtin"]),
        "sort_order": row["sort_order"],
        "group_count": group_count,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _drawer_group_count(conn: sqlite3.Connection, code: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM dim_group WHERE scope = ?", (code,)).fetchone()[0]


@router.get("/drawers")
def list_drawers() -> dict:
    init_db()
    conn = get_connection()
    try:
        rows = list(conn.execute("SELECT * FROM drawer ORDER BY sort_order, code"))
        return {
            "drawers": [_drawer_to_dict(row, _drawer_group_count(conn, row["code"])) for row in rows]
        }
    finally:
        conn.close()


class DrawerCreate(BaseModel):
    code: str
    name: str
    sort_order: int | None = None


class DrawerPatch(BaseModel):
    name: str | None = None
    sort_order: int | None = None


@router.post("/drawers", status_code=201)
def create_drawer(body: DrawerCreate) -> dict:
    init_db()
    conn = get_connection()
    try:
        code = body.code.strip()
        name = body.name.strip()
        if not DRAWER_CODE_RE.match(code):
            raise _bad("抽屉编码需为小写字母开头的小写字母/数字/下划线组合（2-32 位）")
        if code == NON_DRAWER_SCOPE:
            raise _bad(f"抽屉编码 {NON_DRAWER_SCOPE} 为保留字，请换一个")
        if not name:
            raise _bad("抽屉名称不能为空")
        if conn.execute("SELECT 1 FROM drawer WHERE code = ?", (code,)).fetchone():
            raise _bad(f"抽屉编码已存在：{code}")
        if conn.execute("SELECT 1 FROM drawer WHERE name = ?", (name,)).fetchone():
            raise _bad(f"抽屉名称已存在：{name}")
        order = body.sort_order
        if order is None:
            row = conn.execute("SELECT MAX(sort_order) AS m FROM drawer").fetchone()
            order = (row["m"] or 0) + 10
        with conn:
            conn.execute(
                "INSERT INTO drawer (code, name, sort_order) VALUES (?, ?, ?)", (code, name, order)
            )
        row = conn.execute("SELECT * FROM drawer WHERE code = ?", (code,)).fetchone()
        return _drawer_to_dict(row)
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.patch("/drawers/{code}")
def patch_drawer(code: str, body: DrawerPatch) -> dict:
    init_db()
    conn = get_connection()
    try:
        existing = conn.execute("SELECT * FROM drawer WHERE code = ?", (code,)).fetchone()
        if existing is None:
            raise _not_found("抽屉", code)
        if existing["is_builtin"]:
            raise _bad("内置抽屉不可修改")
        patch: dict = {}
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise _bad("抽屉名称不能为空")
            if conn.execute(
                "SELECT 1 FROM drawer WHERE name = ? AND code != ?", (name, code)
            ).fetchone():
                raise _bad(f"抽屉名称已存在：{name}")
            patch["name"] = name
        if body.sort_order is not None:
            patch["sort_order"] = body.sort_order
        if patch:
            assignments = ", ".join(f"{k} = ?" for k in patch)
            with conn:
                conn.execute(
                    f"""UPDATE drawer SET {assignments},
                        updated_at = datetime('now', 'localtime') WHERE code = ?""",
                    (*patch.values(), code),
                )
        row = conn.execute("SELECT * FROM drawer WHERE code = ?", (code,)).fetchone()
        return _drawer_to_dict(row, _drawer_group_count(conn, code))
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.delete("/drawers/{code}")
def delete_drawer(code: str) -> dict:
    init_db()
    conn = get_connection()
    try:
        existing = conn.execute("SELECT * FROM drawer WHERE code = ?", (code,)).fetchone()
        if existing is None:
            raise _not_found("抽屉", code)
        if existing["is_builtin"]:
            raise _conflict("内置抽屉不可删除")
        count = _drawer_group_count(conn, code)
        if count:
            raise _conflict(f"抽屉下已有分组 {count} 个，禁止删除")
        with conn:
            conn.execute("DELETE FROM drawer WHERE code = ?", (code,))
        return {"code": code, "deleted": True}
    except sqlite3.IntegrityError as e:
        raise _conflict(f"抽屉已被引用，禁止删除：{e}")
    finally:
        conn.close()


# ========== 抽屉分组 ==========


def _group_to_dict(row: sqlite3.Row) -> dict:
    member_atoms = json.loads(row["member_atoms"] or "[]")
    return {
        "group_code": row["group_code"],
        "group_name": row["group_name"],
        "scope": row["scope"],
        "parent_code": row["parent_code"],
        "parent_name": row["parent_name"],
        "member_atoms": member_atoms,
        "member_count": len(member_atoms),
        "is_builtin": bool(row["is_builtin"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/dim_groups")
def list_dim_groups() -> dict:
    init_db()
    conn = get_connection()
    try:
        rows = list(
            conn.execute(
                """SELECT g.*, p.group_name AS parent_name FROM dim_group g
                   LEFT JOIN dim_group p ON p.group_code = g.parent_code
                   ORDER BY g.is_builtin DESC, g.group_code"""
            )
        )
        return {"dim_groups": [_group_to_dict(row) for row in rows]}
    finally:
        conn.close()


class DimGroupCreate(BaseModel):
    group_code: str
    group_name: str
    scope: str
    parent_code: str | None = None
    member_atoms: list[str] | None = None


class DimGroupPatch(BaseModel):
    group_name: str | None = None
    scope: str | None = None
    parent_code: str | None = None
    member_atoms: list[str] | None = None

    def to_patch(self) -> dict:
        patch = {}
        if self.group_name is not None:
            patch["group_name"] = self.group_name
        if self.scope is not None:
            patch["scope"] = self.scope
        if self.parent_code is not None:
            patch["parent_code"] = self.parent_code
        if self.member_atoms is not None:
            patch["member_atoms"] = self.member_atoms
        return patch


def _check_group_parent(conn: sqlite3.Connection, group_code: str, parent_code: str | None) -> None:
    if parent_code is None:
        return
    if parent_code == group_code:
        raise _bad("父分组不能是自身")
    if conn.execute("SELECT 1 FROM dim_group WHERE group_code = ?", (parent_code,)).fetchone() is None:
        raise _not_found("父分组", parent_code)
    # 防成环：沿父链向上走，回到自身则拒绝
    seen = {group_code}
    current = parent_code
    while current is not None:
        if current in seen:
            raise _bad("父分组设置会形成循环层级")
        seen.add(current)
        row = conn.execute(
            "SELECT parent_code FROM dim_group WHERE group_code = ?", (current,)
        ).fetchone()
        current = row["parent_code"] if row else None


@router.post("/dim_groups", status_code=201)
def create_dim_group(body: DimGroupCreate) -> dict:
    init_db()
    conn = get_connection()
    try:
        group_code = body.group_code.strip()
        group_name = body.group_name.strip()
        if not group_code:
            raise _bad("字段 group_code 不能为空")
        if not group_name:
            raise _bad("字段 group_name 不能为空")
        _check_dim_scope(conn, body.scope)
        if conn.execute("SELECT 1 FROM dim_group WHERE group_code = ?", (group_code,)).fetchone():
            raise _bad(f"分组编码已存在：{group_code}")
        _check_group_parent(conn, group_code, body.parent_code)
        members = body.member_atoms or []
        _check_atom_members(conn, members)
        with conn:
            conn.execute(
                """INSERT INTO dim_group (group_code, group_name, scope, parent_code, member_atoms)
                   VALUES (?, ?, ?, ?, ?)""",
                (group_code, group_name, body.scope, body.parent_code,
                 json.dumps(members, ensure_ascii=False)),
            )
        row = conn.execute(
            """SELECT g.*, p.group_name AS parent_name FROM dim_group g
               LEFT JOIN dim_group p ON p.group_code = g.parent_code
               WHERE g.group_code = ?""",
            (group_code,),
        ).fetchone()
        return _group_to_dict(row)
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.patch("/dim_groups/{group_code}")
def patch_dim_group(group_code: str, body: DimGroupPatch) -> dict:
    init_db()
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT * FROM dim_group WHERE group_code = ?", (group_code,)
        ).fetchone()
        if existing is None:
            raise _not_found("抽屉分组", group_code)
        patch = body.to_patch()
        if existing["is_builtin"] and ("group_name" in patch or "parent_code" in patch):
            raise _bad("内置分组仅支持修改成员")
        if "scope" in patch:
            _check_dim_scope(conn, patch["scope"])
        if "group_name" in patch:
            if not patch["group_name"].strip():
                raise _bad("字段 group_name 不能为空")
            patch["group_name"] = patch["group_name"].strip()
        if "parent_code" in patch:
            _check_group_parent(conn, group_code, patch["parent_code"])
        if "member_atoms" in patch:
            _check_atom_members(conn, patch["member_atoms"])
            patch["member_atoms"] = json.dumps(patch["member_atoms"], ensure_ascii=False)
        if patch:
            assignments = ", ".join(f"{k} = ?" for k in patch)
            with conn:
                conn.execute(
                    f"""UPDATE dim_group SET {assignments},
                        updated_at = datetime('now', 'localtime') WHERE group_code = ?""",
                    (*patch.values(), group_code),
                )
        row = conn.execute(
            """SELECT g.*, p.group_name AS parent_name FROM dim_group g
               LEFT JOIN dim_group p ON p.group_code = g.parent_code
               WHERE g.group_code = ?""",
            (group_code,),
        ).fetchone()
        return _group_to_dict(row)
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.delete("/dim_groups/{group_code}")
def delete_dim_group(group_code: str) -> dict:
    init_db()
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT is_builtin FROM dim_group WHERE group_code = ?", (group_code,)
        ).fetchone()
        if existing is None:
            raise _not_found("抽屉分组", group_code)
        if existing["is_builtin"]:
            raise _conflict("内置分组禁止删除")
        children = _ref_count(
            conn, "SELECT COUNT(*) FROM dim_group WHERE parent_code = ?", group_code
        )
        if children > 0:
            raise _conflict(f"分组下存在 {children} 个子分组，禁止删除")
        with conn:
            conn.execute("DELETE FROM dim_group WHERE group_code = ?", (group_code,))
        return {"group_code": group_code, "deleted": True}
    except sqlite3.IntegrityError as e:
        raise _conflict(f"分组已被引用，禁止删除：{e}")
    finally:
        conn.close()


# ========== 供应商 ==========


def _supplier_to_dict(row: sqlite3.Row, quote_count: int | None = None) -> dict:
    data = {
        "code": row["code"],
        "name": row["name"],
        "alias": row["alias"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if quote_count is not None:
        data["quote_count"] = quote_count
    return data


@router.get("/suppliers")
def list_suppliers(q: str = Query(default="")) -> dict:
    init_db()
    conn = get_connection()
    try:
        like = f"%{q}%"
        rows = list(
            conn.execute(
                """SELECT * FROM supplier
                   WHERE ? = '' OR code LIKE ? OR name LIKE ? OR alias LIKE ?
                   ORDER BY code""",
                (q, like, like, like),
            )
        )
        counts = {
            row["supplier_code"]: row["n"]
            for row in conn.execute(
                """SELECT supplier_code, COUNT(*) AS n FROM quote
                   WHERE supplier_code IS NOT NULL AND parse_status IN ('parsed', 'reviewed')
                   GROUP BY supplier_code"""
            )
        }
        return {"suppliers": [_supplier_to_dict(row, counts.get(row["code"], 0)) for row in rows]}
    finally:
        conn.close()


class SupplierCreate(BaseModel):
    code: str
    name: str
    alias: str | None = None


@router.post("/suppliers", status_code=201)
def create_supplier(body: SupplierCreate) -> dict:
    init_db()
    conn = get_connection()
    try:
        code = body.code.strip()
        name = body.name.strip()
        if not code:
            raise _bad("字段 code 不能为空")
        if not name:
            raise _bad("字段 name 不能为空")
        if conn.execute("SELECT 1 FROM supplier WHERE code = ?", (code,)).fetchone():
            raise _bad(f"供应商编码已存在：{code}")
        with conn:
            conn.execute(
                "INSERT INTO supplier (code, name, alias) VALUES (?, ?, ?)",
                (code, name, body.alias),
            )
        return _supplier_to_dict(
            conn.execute("SELECT * FROM supplier WHERE code = ?", (code,)).fetchone()
        )
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


class SupplierPatch(BaseModel):
    name: str | None = None
    alias: str | None = None

    def to_patch(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


@router.patch("/suppliers/{code}")
def patch_supplier(code: str, body: SupplierPatch) -> dict:
    init_db()
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM supplier WHERE code = ?", (code,)).fetchone() is None:
            raise _not_found("供应商", code)
        patch = body.to_patch()
        if "name" in patch and not patch["name"].strip():
            raise _bad("字段 name 不能为空")
        if patch:
            assignments = ", ".join(f"{k} = ?" for k in patch)
            with conn:
                conn.execute(
                    f"""UPDATE supplier SET {assignments},
                        updated_at = datetime('now', 'localtime') WHERE code = ?""",
                    (*patch.values(), code),
                )
        return _supplier_to_dict(
            conn.execute("SELECT * FROM supplier WHERE code = ?", (code,)).fetchone()
        )
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.delete("/suppliers/{code}")
def delete_supplier(code: str) -> dict:
    init_db()
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM supplier WHERE code = ?", (code,)).fetchone() is None:
            raise _not_found("供应商", code)
        quotes = _ref_count(conn, "SELECT COUNT(*) FROM quote WHERE supplier_code = ?", code)
        if quotes > 0:
            raise _conflict(f"供应商已被 {quotes} 张报价单引用，禁止删除")
        with conn:
            conn.execute("DELETE FROM supplier WHERE code = ?", (code,))
        return {"code": code, "deleted": True}
    except sqlite3.IntegrityError as e:
        raise _conflict(f"供应商已被引用，禁止删除：{e}")
    finally:
        conn.close()


class SupplierRegister(BaseModel):
    """比价页面「加入管理」：按识别名注册（同名复用），并绑定这些报价单。"""

    name: str
    alias: str | None = None
    quote_ids: list[int] = []


@router.post("/suppliers/register")
def register_supplier_endpoint(body: SupplierRegister) -> dict:
    init_db()
    conn = get_connection()
    try:
        return register_supplier(conn, body.name, body.alias, body.quote_ids)
    except MasterBindingError as e:
        raise _bad(str(e))
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


class QuoteIds(BaseModel):
    quote_ids: list[int]


@router.post("/suppliers/{code}/bind")
def bind_supplier_endpoint(code: str, body: QuoteIds) -> dict:
    """把已有主数据供应商关联到识别名不一致的报价单。"""
    init_db()
    conn = get_connection()
    try:
        if conn.execute("SELECT 1 FROM supplier WHERE code = ?", (code,)).fetchone() is None:
            raise _not_found("供应商", code)
        return {"code": code, "bound_quotes": bind_supplier_codes(conn, code, body.quote_ids)}
    except MasterBindingError as e:
        raise _bad(str(e))
    finally:
        conn.close()


# ========== 项目（公司内部的具体 SKU） ==========


def _project_row(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM project WHERE code = ?", (code,)).fetchone()


def _check_category(conn: sqlite3.Connection, category_code: str | None) -> None:
    if category_code and conn.execute(
        "SELECT 1 FROM category WHERE code = ?", (category_code,)
    ).fetchone() is None:
        raise _bad(f"品类不存在：{category_code}")


@router.get("/projects")
def list_projects(q: str = Query(default="")) -> dict:
    init_db()
    conn = get_connection()
    try:
        like = f"%{q}%"
        rows = conn.execute(
            """SELECT * FROM project
               WHERE ? = '' OR code LIKE ? OR name LIKE ? OR IFNULL(remark, '') LIKE ?
               ORDER BY code""",
            (q, like, like, like),
        )
        return {
            "projects": [
                project_to_dict(row, project_quote_count(conn, row["code"])) for row in rows
            ]
        }
    finally:
        conn.close()


class ProjectCreate(BaseModel):
    name: str
    code: str | None = None
    category_code: str | None = None
    remark: str | None = None
    quote_ids: list[int] = []


@router.post("/projects", status_code=201)
def create_project_endpoint(body: ProjectCreate) -> dict:
    init_db()
    conn = get_connection()
    try:
        _check_category(conn, body.category_code)
        return create_project(
            conn,
            body.name,
            body.category_code,
            body.remark,
            body.quote_ids,
            code=(body.code or "").strip() or None,
        )
    except MasterBindingConflict as e:
        raise _conflict(str(e))
    except MasterBindingError as e:
        raise _bad(str(e))
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


class ProjectPatch(BaseModel):
    name: str | None = None
    category_code: str | None = None
    remark: str | None = None

    def to_patch(self) -> dict:
        """只取显式传入的字段；空字符串一律视为「清空」（转 NULL），便于解除品类/备注。"""
        patch = {}
        for key in self.model_fields_set:
            value = getattr(self, key)
            if isinstance(value, str):
                value = value.strip() or None
            patch[key] = value
        return patch


@router.patch("/projects/{code}")
def patch_project(code: str, body: ProjectPatch) -> dict:
    init_db()
    conn = get_connection()
    try:
        if _project_row(conn, code) is None:
            raise _not_found("项目", code)
        patch = body.to_patch()
        if "name" in patch:
            name = (patch["name"] or "").strip()
            if not name:
                raise _bad("字段 name 不能为空")
            patch["name"] = name
            dup = conn.execute(
                "SELECT code FROM project WHERE name = ? AND code <> ?", (name, code)
            ).fetchone()
            if dup:
                raise _bad(f"项目名称已存在：{name}")
        if "category_code" in patch:
            _check_category(conn, patch["category_code"])
        if patch:
            assignments = ", ".join(f"{k} = ?" for k in patch)
            with conn:
                conn.execute(
                    f"""UPDATE project SET {assignments},
                        updated_at = datetime('now', 'localtime') WHERE code = ?""",
                    (*patch.values(), code),
                )
        return project_to_dict(_project_row(conn, code), project_quote_count(conn, code))
    except sqlite3.IntegrityError as e:
        raise _conflict(f"数据冲突：{e}")
    finally:
        conn.close()


@router.delete("/projects/{code}")
def delete_project(code: str) -> dict:
    init_db()
    conn = get_connection()
    try:
        if _project_row(conn, code) is None:
            raise _not_found("项目", code)
        quotes = project_quote_count(conn, code)
        if quotes > 0:
            raise _conflict(f"项目已被 {quotes} 张报价单引用，请先解绑")
        with conn:
            conn.execute("DELETE FROM project WHERE code = ?", (code,))
        return {"code": code, "deleted": True}
    except sqlite3.IntegrityError as e:
        raise _conflict(f"项目已被引用，禁止删除：{e}")
    finally:
        conn.close()


@router.post("/projects/{code}/bind")
def bind_project_endpoint(code: str, body: QuoteIds) -> dict:
    init_db()
    conn = get_connection()
    try:
        if _project_row(conn, code) is None:
            raise _not_found("项目", code)
        return {"code": code, "bound_quotes": bind_project_codes(conn, code, body.quote_ids)}
    except MasterBindingError as e:
        raise _bad(str(e))
    finally:
        conn.close()


@router.post("/projects/{code}/unbind")
def unbind_project_endpoint(code: str, body: QuoteIds) -> dict:
    init_db()
    conn = get_connection()
    try:
        if _project_row(conn, code) is None:
            raise _not_found("项目", code)
        return {"code": code, "unbound_quotes": unbind_project_codes(conn, code, body.quote_ids)}
    except MasterBindingError as e:
        raise _bad(str(e))
    finally:
        conn.close()


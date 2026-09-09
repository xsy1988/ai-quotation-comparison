"""主数据管理 API（第 9 步）：五资源 CRUD、删除保护、编码自动生成、格式校验。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app

client = TestClient(app)


@pytest.fixture
def seeded():
    """最小主数据：2 域 / 1 阶段 / 1 类别 / 2 原子 / 1 品类 / 1 内置分组 / 1 供应商。"""
    init_db()
    conn = get_connection()
    with conn:
        conn.execute("INSERT INTO process_domain (code, name) VALUES ('TJ', '特种加工')")
        conn.execute("INSERT INTO process_domain (code, name) VALUES ('QX', '切削')")
        conn.execute("INSERT INTO process_stage (name) VALUES ('机加')")
        conn.execute("INSERT INTO process_class (name) VALUES ('主制程')")
        conn.execute("INSERT INTO category (code, name) VALUES ('CAT-WJWK', '五金外壳')")
        conn.execute(
            """INSERT INTO atom (code, name, domain_code, stage_name, class_name)
               VALUES ('AT-TJ-001', '电火花', 'TJ', '机加', '主制程')"""
        )
        conn.execute(
            """INSERT INTO atom (code, name, domain_code, stage_name, class_name)
               VALUES ('AT-TJ-002', '线切割', 'TJ', '机加', '主制程')"""
        )
        conn.execute(
            """INSERT INTO atom (code, name, domain_code, stage_name, class_name)
               VALUES ('AT-QX-001', '车削', 'QX', '机加', '主制程')"""
        )
        conn.execute(
            """INSERT INTO dim_group (group_code, group_name, scope, member_atoms, is_builtin)
               VALUES ('builtin:stage:机加', '机加', 'process_stage', '["AT-TJ-001"]', 1)"""
        )
        conn.execute("INSERT INTO supplier (code, name) VALUES ('SUP-001', '华强精密')")
    conn.close()
    return True


# ---------- 原子 ----------

def test_atom_crud_and_auto_code(seeded):
    resp = client.post(
        "/api/master/atoms",
        json={
            "name": "电火花加工",
            "domain_code": "TJ",
            "stage_name": "机加",
            "class_name": "主制程",
            "remark": "新工序",
            "category_codes": ["CAT-WJWK"],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    # 域内 max(002) + 1
    assert body["code"] == "AT-TJ-003"
    assert body["domain_name"] == "特种加工"
    assert body["categories"] == ["五金外壳"]
    assert body["is_fallback"] is False

    # GET 列表 + q 过滤
    resp = client.get("/api/master/atoms")
    assert resp.status_code == 200
    assert len(resp.json()["atoms"]) == 4
    resp = client.get("/api/master/atoms", params={"q": "线切割"})
    assert [a["code"] for a in resp.json()["atoms"]] == ["AT-TJ-002"]
    resp = client.get("/api/master/atoms", params={"q": "AT-QX"})
    assert [a["code"] for a in resp.json()["atoms"]] == ["AT-QX-001"]

    # PATCH
    resp = client.patch("/api/master/atoms/AT-TJ-003", json={"name": "电火花精加工", "remark": "r2"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "电火花精加工"
    assert resp.json()["remark"] == "r2"

    # 空域编码从 001 开始
    resp = client.post(
        "/api/master/atoms",
        json={"name": "铣削", "domain_code": "QX", "stage_name": "机加", "class_name": "主制程"},
    )
    assert resp.json()["code"] == "AT-QX-002"

    # DELETE（先解除品类关联，无引用后可删）
    resp = client.delete("/api/master/atoms/AT-TJ-003")
    assert resp.status_code == 409
    assert "品类关联 1 条" in resp.json()["detail"]
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM atom_category WHERE atom_code = 'AT-TJ-003'")
    conn.close()
    resp = client.delete("/api/master/atoms/AT-TJ-003")
    assert resp.status_code == 200
    assert resp.json() == {"code": "AT-TJ-003", "deleted": True}
    resp = client.get("/api/master/atoms", params={"q": "AT-TJ-003"})
    assert resp.json()["atoms"] == []


def test_atom_validation_errors(seeded):
    resp = client.post(
        "/api/master/atoms",
        json={"name": "x", "domain_code": "NOPE", "stage_name": "机加", "class_name": "主制程"},
    )
    assert resp.status_code == 400
    assert "工艺域不存在" in resp.json()["detail"]

    resp = client.post(
        "/api/master/atoms",
        json={"name": "x", "domain_code": "TJ", "stage_name": "机加", "class_name": "主制程",
              "category_codes": ["CAT-NOPE"]},
    )
    assert resp.status_code == 400
    assert "品类不存在" in resp.json()["detail"]

    resp = client.patch("/api/master/atoms/AT-XX-999", json={"name": "x"})
    assert resp.status_code == 404

    resp = client.patch("/api/master/atoms/AT-TJ-001", json={"stage_name": "不存在阶段"})
    assert resp.status_code == 400
    assert "工艺阶段不存在" in resp.json()["detail"]


def test_atom_delete_protection(seeded):
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO atom_alias (atom_code, alias_text) VALUES ('AT-TJ-002', '走丝')"
        )
        conn.execute(
            "INSERT INTO atom_category (atom_code, category_code) VALUES ('AT-TJ-002', 'CAT-WJWK')"
        )
        conn.execute(
            "INSERT INTO comparison_task (project_name) VALUES ('保护测试')"
        )
        conn.execute(
            """INSERT INTO quote (task_id) VALUES ((SELECT MAX(id) FROM comparison_task))"""
        )
        conn.execute(
            """INSERT INTO quote_line (quote_id, module, item_name, atom_code)
               VALUES ((SELECT MAX(id) FROM quote), 'processing', '走丝', 'AT-TJ-002')"""
        )
    conn.close()

    resp = client.delete("/api/master/atoms/AT-TJ-002")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "禁止删除" in detail
    assert "别名 1 条" in detail
    assert "品类关联 1 条" in detail
    assert "报价行 1 条" in detail


# ---------- 别名 ----------

def test_alias_crud_and_hit_count(seeded):
    resp = client.post(
        "/api/master/aliases", json={"atom_code": "AT-TJ-001", "alias_text": "EDM"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["source"] == "manual_feedback"
    assert body["hit_count"] == 0
    assert body["atom_name"] == "电火花"

    # hit_count 在列表中返回
    conn = get_connection()
    with conn:
        conn.execute("UPDATE atom_alias SET hit_count = 7 WHERE id = ?", (body["id"],))
        conn.execute(
            "INSERT INTO atom_alias (atom_code, alias_text, source) VALUES ('AT-TJ-001', 'initial别名', 'initial')"
        )
    conn.close()

    resp = client.get("/api/master/aliases", params={"sort": "hit"})
    assert resp.status_code == 200
    aliases = resp.json()["aliases"]
    assert aliases[0]["alias_text"] == "EDM"
    assert aliases[0]["hit_count"] == 7
    assert aliases[1]["hit_count"] == 0

    # q 过滤
    resp = client.get("/api/master/aliases", params={"q": "initial"})
    assert [a["alias_text"] for a in resp.json()["aliases"]] == ["initial别名"]

    # 重复别名 400
    resp = client.post(
        "/api/master/aliases", json={"atom_code": "AT-TJ-001", "alias_text": "EDM"}
    )
    assert resp.status_code == 400

    # 原子不存在 404
    resp = client.post(
        "/api/master/aliases", json={"atom_code": "AT-XX-999", "alias_text": "x"}
    )
    assert resp.status_code == 404

    # DELETE + 404
    resp = client.delete(f"/api/master/aliases/{body['id']}")
    assert resp.status_code == 200
    resp = client.delete(f"/api/master/aliases/{body['id']}")
    assert resp.status_code == 404


# ---------- 品类 ----------

def test_category_crud(seeded):
    resp = client.post("/api/master/categories", json={"code": "CAT-SJ", "name": "塑胶"})
    assert resp.status_code == 201
    assert resp.json()["atom_count"] == 0

    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO atom_category (atom_code, category_code) VALUES ('AT-TJ-001', 'CAT-SJ')"
        )
    conn.close()

    resp = client.get("/api/master/categories")
    cats = {c["code"]: c for c in resp.json()["categories"]}
    assert cats["CAT-SJ"]["atom_count"] == 1
    assert cats["CAT-WJWK"]["atom_count"] == 0

    resp = client.patch("/api/master/categories/CAT-SJ", json={"name": "塑胶件"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "塑胶件"

    resp = client.delete("/api/master/categories/CAT-SJ")
    assert resp.status_code == 409
    assert "原子关联 1 条" in resp.json()["detail"]

    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM atom_category WHERE category_code = 'CAT-SJ'")
    conn.close()
    resp = client.delete("/api/master/categories/CAT-SJ")
    assert resp.status_code == 200


def test_category_validation(seeded):
    resp = client.post("/api/master/categories", json={"code": "SJ", "name": "塑胶"})
    assert resp.status_code == 400
    assert "格式" in resp.json()["detail"]

    resp = client.post("/api/master/categories", json={"code": "CAT-WJWK", "name": "别的名字"})
    assert resp.status_code == 400
    assert "已存在" in resp.json()["detail"]

    resp = client.patch("/api/master/categories/CAT-NOPE", json={"name": "x"})
    assert resp.status_code == 404

    client.post("/api/master/categories", json={"code": "CAT-SJ", "name": "塑胶"})
    resp = client.patch("/api/master/categories/CAT-SJ", json={"name": "五金外壳"})
    assert resp.status_code == 400
    assert "品类名称已存在" in resp.json()["detail"]


def test_category_delete_protection_by_task_and_quote(seeded):
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT INTO comparison_task (project_name, category_code) VALUES ('t', 'CAT-WJWK')"
        )
    conn.close()
    resp = client.delete("/api/master/categories/CAT-WJWK")
    assert resp.status_code == 409
    assert "对比任务 1 条" in resp.json()["detail"]


# ---------- 抽屉分组 ----------

def test_dim_group_crud(seeded):
    resp = client.post(
        "/api/master/dim_groups",
        json={
            "group_code": "custom:高精密组",
            "group_name": "高精密组",
            "scope": "custom",
            "member_atoms": ["AT-TJ-001", "AT-TJ-002"],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["member_count"] == 2
    assert body["is_builtin"] is False

    # PATCH：挂父级 + 改成员
    resp = client.patch(
        "/api/master/dim_groups/custom:高精密组",
        json={"parent_code": "builtin:stage:机加", "member_atoms": ["AT-TJ-001"]},
    )
    assert resp.status_code == 200
    assert resp.json()["parent_name"] == "机加"
    assert resp.json()["member_count"] == 1

    # GET 列表联展
    resp = client.get("/api/master/dim_groups")
    groups = {g["group_code"]: g for g in resp.json()["dim_groups"]}
    assert groups["builtin:stage:机加"]["is_builtin"] is True
    assert groups["custom:高精密组"]["parent_name"] == "机加"

    # DELETE（带子分组仍先删子）
    resp = client.delete("/api/master/dim_groups/builtin:stage:机加")
    assert resp.status_code == 409
    assert "内置分组禁止删除" in resp.json()["detail"]

    resp = client.delete("/api/master/dim_groups/custom:高精密组")
    assert resp.status_code == 200


def test_dim_group_validation(seeded):
    resp = client.post(
        "/api/master/dim_groups",
        json={"group_code": "g1", "group_name": "g", "scope": "bogus"},
    )
    assert resp.status_code == 400
    assert "scope 非法" in resp.json()["detail"]

    resp = client.post(
        "/api/master/dim_groups",
        json={"group_code": "g1", "group_name": "g", "scope": "custom",
              "member_atoms": ["AT-XX-999"]},
    )
    assert resp.status_code == 400
    assert "原子不存在" in resp.json()["detail"]

    resp = client.post(
        "/api/master/dim_groups",
        json={"group_code": "g1", "group_name": "g", "scope": "custom",
              "parent_code": "builtin:stage:机加"},
    )
    assert resp.status_code == 201

    # 父分组不存在
    resp = client.patch("/api/master/dim_groups/g1", json={"parent_code": "NOPE"})
    assert resp.status_code == 404

    # 自身成环
    resp = client.patch("/api/master/dim_groups/g1", json={"parent_code": "g1"})
    assert resp.status_code == 400

    # 两节点成环：g1 -> g2 -> g1
    client.post(
        "/api/master/dim_groups",
        json={"group_code": "g2", "group_name": "g2", "scope": "custom", "parent_code": "g1"},
    )
    resp = client.patch("/api/master/dim_groups/g1", json={"parent_code": "g2"})
    assert resp.status_code == 400
    assert "循环" in resp.json()["detail"]

    # 子分组保护
    resp = client.delete("/api/master/dim_groups/g1")
    assert resp.status_code == 409
    assert "子分组" in resp.json()["detail"]

    # 内置组仅可改成员
    resp = client.patch("/api/master/dim_groups/builtin:stage:机加", json={"group_name": "改名"})
    assert resp.status_code == 400
    resp = client.patch(
        "/api/master/dim_groups/builtin:stage:机加",
        json={"member_atoms": ["AT-TJ-001", "AT-QX-001"]},
    )
    assert resp.status_code == 200
    assert resp.json()["member_count"] == 2

    resp = client.patch("/api/master/dim_groups/NOPE", json={"group_name": "x"})
    assert resp.status_code == 404


# ---------- 供应商 ----------

def test_supplier_crud_and_protection(seeded):
    resp = client.post(
        "/api/master/suppliers", json={"code": "SUP-002", "name": "宏图五金", "alias": "宏图"}
    )
    assert resp.status_code == 201
    assert resp.json()["alias"] == "宏图"

    resp = client.get("/api/master/suppliers", params={"q": "宏图"})
    assert [s["code"] for s in resp.json()["suppliers"]] == ["SUP-002"]

    resp = client.patch("/api/master/suppliers/SUP-002", json={"name": "宏图精密五金"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "宏图精密五金"

    # 引用保护：quote.supplier_code
    conn = get_connection()
    with conn:
        conn.execute("INSERT INTO comparison_task (project_name) VALUES ('s')")
        conn.execute(
            """INSERT INTO quote (task_id, supplier_code) VALUES
               ((SELECT MAX(id) FROM comparison_task), 'SUP-002')"""
        )
    conn.close()
    resp = client.delete("/api/master/suppliers/SUP-002")
    assert resp.status_code == 409
    assert "已被 1 张报价单引用" in resp.json()["detail"]

    # 无引用可删
    resp = client.delete("/api/master/suppliers/SUP-001")
    assert resp.status_code == 200


def test_supplier_validation(seeded):
    resp = client.post("/api/master/suppliers", json={"code": "SUP-001", "name": "重复"})
    assert resp.status_code == 400

    resp = client.post("/api/master/suppliers", json={"code": " ", "name": "x"})
    assert resp.status_code == 400
    assert "code" in resp.json()["detail"]

    resp = client.patch("/api/master/suppliers/NOPE", json={"name": "x"})
    assert resp.status_code == 404

    resp = client.delete("/api/master/suppliers/NOPE")
    assert resp.status_code == 404

"""供应商 / 项目绑定（迭代）：名称归一化、注册幂等、项目 CRUD、任务匹配视图、历史曲线。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.services.master_binding import normalize_name

client = TestClient(app)


def _add_quote(conn, task_id, supplier_name, basic=None, category="CAT-WJWK", **totals) -> int:
    columns = ["task_id", "supplier_name", "category_code", "basic_info", "parse_status"]
    values = [task_id, supplier_name, category, json.dumps(basic or {}, ensure_ascii=False), "parsed"]
    for key, value in totals.items():
        columns.append(key)
        values.append(value)
    sql = f"INSERT INTO quote ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})"
    with conn:
        cursor = conn.execute(sql, values)
    return cursor.lastrowid


@pytest.fixture
def seeded():
    """2 个任务 / 5 张报价单：同名供应商出现全半角与空格差异，项目名一份缺失。"""
    init_db()
    conn = get_connection()
    with conn:
        conn.execute("INSERT INTO category (code, name) VALUES ('CAT-WJWK', '五金外壳')")
        conn.execute("INSERT INTO category (code, name) VALUES ('CAT-SJ', '塑胶件')")
        conn.execute(
            "INSERT INTO comparison_task (id, project_name, category_code, status)"
            " VALUES (1, '主壳', 'CAT-WJWK', 'parsed')"
        )
        conn.execute(
            "INSERT INTO comparison_task (id, project_name, category_code, status)"
            " VALUES (2, '雾化芯', 'CAT-SJ', 'parsed')"
        )
    ids = {
        "a1": _add_quote(
            conn,
            1,
            "东莞市鸿图精密压铸有限公司",
            {"project_name": "主壳", "quote_date": "2024-03-01"},
            final_unit_price_taxed=15.44,
            materials_total=3.2,
            tooling_total=43000,
        ),
        "a2": _add_quote(
            conn,
            2,
            "东莞市鸿图精密压铸 有限公司",  # 空格差异，应归为同一家
            {"project_name": "雾化芯", "quote_date": "2024-06-01"},
            category="CAT-SJ",
            final_unit_price_taxed=14.1,
            materials_total=2.9,
        ),
        "b1": _add_quote(
            conn,
            1,
            "中山美格",
            {"project_name": "主壳", "quote_date": "2024-04-01"},
            final_unit_price_taxed=17.37,
            materials_total=4.0,
        ),
        "c1": _add_quote(
            conn,
            1,
            "深圳锐进",
            basic={},  # 项目名缺失：应回退到任务名「主壳」
            final_unit_price_taxed=17.74,
        ),
        "fail": _add_quote(conn, 1, "未完成供应商", {"project_name": "主壳"}),
    }
    with conn:
        conn.execute("UPDATE quote SET parse_status = 'failed' WHERE id = ?", (ids["fail"],))
    conn.close()
    return ids


# ---------- 名称归一化 ----------

@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("东莞鸿图", "东莞鸿图"),
        ("东莞鸿图 ", " 东莞鸿图"),
        ("ＡＢＣ 精密", "abc精密"),
        ("中山美格\n", "中山美格"),
        (None, ""),
    ],
)
def test_normalize_name_equivalent(left, right):
    assert normalize_name(left) == normalize_name(right)


def test_normalize_name_distinguishes():
    assert normalize_name("深圳锐进") != normalize_name("深圳联诚")


# ---------- 供应商注册 / 绑定 ----------

def test_register_supplier_creates_and_binds(seeded):
    resp = client.post(
        "/api/master/suppliers/register",
        json={"name": "东莞市鸿图精密压铸有限公司", "quote_ids": [seeded["a1"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == "SUP-001"
    assert body["created"] is True
    # 同名（含空格差异）跨任务一并绑定
    assert body["bound_quotes"] == 2

    conn = get_connection()
    codes = {
        row["supplier_code"]
        for row in conn.execute("SELECT supplier_code FROM quote WHERE id IN (?, ?)", (seeded["a1"], seeded["a2"]))
    }
    conn.close()
    assert codes == {"SUP-001"}


def test_register_supplier_idempotent(seeded):
    first = client.post("/api/master/suppliers/register", json={"name": "中山美格"}).json()
    second = client.post("/api/master/suppliers/register", json={"name": "中山美格"}).json()
    assert first["code"] == second["code"]
    assert second["created"] is False
    assert second["bound_quotes"] == 0  # 第二次无新增绑定


def test_register_supplier_matches_alias(seeded):
    client.post(
        "/api/master/suppliers/register",
        json={"name": "中山市美格金属科技有限公司", "alias": "美格、中山美格"},
    )
    resp = client.post("/api/master/suppliers/register", json={"name": "中山美格"})
    assert resp.json()["code"] == "SUP-001"
    assert resp.json()["created"] is False


def test_register_supplier_empty_name(seeded):
    resp = client.post("/api/master/suppliers/register", json={"name": "   "})
    assert resp.status_code == 400


def test_bind_supplier_codes_explicit(seeded):
    client.post("/api/master/suppliers/register", json={"name": "深圳锐进"})
    resp = client.post("/api/master/suppliers/SUP-001/bind", json={"quote_ids": [seeded["c1"]]})
    assert resp.status_code == 200
    assert resp.json()["bound_quotes"] == 0  # 已是 SUP-001，无需变更
    resp = client.post("/api/master/suppliers/SUP-999/bind", json={"quote_ids": [seeded["c1"]]})
    assert resp.status_code == 404


def test_supplier_code_increments(seeded):
    assert client.post("/api/master/suppliers/register", json={"name": "中山美格"}).json()["code"] == "SUP-001"
    assert client.post("/api/master/suppliers/register", json={"name": "深圳锐进"}).json()["code"] == "SUP-002"


# ---------- 任务匹配视图 ----------

def test_master_match_lists_unmanaged_first(seeded):
    resp = client.get("/api/tasks/1/master-match")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unmanaged_supplier_count"] == 3
    assert [s["name"] for s in body["suppliers"]] == [
        "东莞市鸿图精密压铸有限公司",
        "中山美格",
        "深圳锐进",
    ]
    assert all(s["managed"] is False and s["supplier_code"] is None for s in body["suppliers"])
    # 解析失败的报价单不参与
    assert "未完成供应商" not in [s["name"] for s in body["suppliers"]]
    hongtu = body["suppliers"][0]
    assert hongtu["quote_ids"] == [seeded["a1"]]
    assert hongtu["part_names"] == [] and hongtu["quote_count"] == 1
    # 品类编码供前端在「历史报价」入口做单品类预选
    assert hongtu["categories"] == ["CAT-WJWK"]


def test_master_match_after_binding(seeded):
    client.post("/api/master/suppliers/register", json={"name": "中山美格"})
    body = client.get("/api/tasks/1/master-match").json()
    assert body["unmanaged_supplier_count"] == 2
    assert body["suppliers"][-1]["name"] == "中山美格"  # 已管理的排最后
    assert body["suppliers"][-1]["managed"] is True
    assert body["suppliers"][-1]["supplier_name"] == "中山美格"


def test_master_match_partial_binding_counts_unmanaged(seeded):
    """任务内同名供应商只有部分报价单被绑定时仍视为待管理，方便用户补齐。"""
    conn = get_connection()
    second = _add_quote(
        conn,
        1,
        "东莞市鸿图精密压铸有限公司",
        {"project_name": "主壳"},
        final_unit_price_taxed=15.9,
    )
    with conn:
        conn.execute("INSERT INTO supplier (code, name) VALUES ('SUP-009', '东莞市鸿图精密压铸有限公司')")
        conn.execute("UPDATE quote SET supplier_code = 'SUP-009' WHERE id = ?", (seeded["a1"],))
    conn.close()
    body = client.get("/api/tasks/1/master-match").json()
    hongtu = next(s for s in body["suppliers"] if s["name"].startswith("东莞市鸿图"))
    assert hongtu["quote_ids"] == [seeded["a1"], second]
    assert hongtu["managed"] is False
    assert hongtu["supplier_code"] is None


def test_master_match_projects_fallback_task_name(seeded):
    body = client.get("/api/tasks/1/master-match").json()
    names = [p["name"] for p in body["projects"]]
    assert names == ["主壳"]  # 项目名缺失的报价单回退到任务名
    assert body["unbound_project_count"] == 1
    project = body["projects"][0]
    assert project["quote_count"] == 3
    assert project["quote_ids"] == [seeded["a1"], seeded["b1"], seeded["c1"]]
    assert project["bound"] is False


def test_master_match_task_not_found(seeded):
    assert client.get("/api/tasks/999/master-match").status_code == 404


# ---------- 项目 CRUD ----------

def test_project_crud_and_auto_code(seeded):
    resp = client.post(
        "/api/master/projects",
        json={"name": "主壳", "category_code": "CAT-WJWK", "remark": "主力 SKU", "quote_ids": [seeded["a1"]]},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == "PRJ-001"
    assert body["quote_count"] == 3  # 名称匹配跨全部任务
    assert body["bound_quotes"] == 3

    listed = client.get("/api/master/projects").json()["projects"]
    assert [p["code"] for p in listed] == ["PRJ-001"]
    assert listed[0]["quote_count"] == 3

    patched = client.patch("/api/master/projects/PRJ-001", json={"name": "主壳-A", "remark": "改"})
    assert patched.status_code == 200
    assert patched.json()["name"] == "主壳-A"

    resp = client.delete("/api/master/projects/PRJ-001")
    assert resp.status_code == 409  # 仍被报价单引用

    conn = get_connection()
    with conn:
        conn.execute("UPDATE quote SET project_code = NULL")
    conn.close()
    assert client.delete("/api/master/projects/PRJ-001").status_code == 200
    assert client.get("/api/master/projects").json()["projects"] == []


def test_project_patch_clears_optional_fields(seeded):
    client.post("/api/master/projects", json={"name": "主壳", "category_code": "CAT-WJWK", "remark": "r"})
    resp = client.patch("/api/master/projects/PRJ-001", json={"category_code": "", "remark": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["category_code"] is None
    assert resp.json()["remark"] is None
    # 未传的字段不动
    assert resp.json()["name"] == "主壳"
    assert client.patch("/api/master/projects/PRJ-001", json={"name": "  "}).status_code == 400


def test_project_name_conflict(seeded):
    client.post("/api/master/projects", json={"name": "主壳"})
    resp = client.post("/api/master/projects", json={"name": "主壳"})
    assert resp.status_code == 409
    resp = client.post("/api/master/projects", json={"name": "PRJ-001", "code": "PRJ-001"})
    assert resp.status_code == 409


def test_project_validation(seeded):
    assert client.post("/api/master/projects", json={"name": "  "}).status_code == 400
    assert client.post("/api/master/projects", json={"name": "X", "category_code": "CAT-NOPE"}).status_code == 400
    assert client.patch("/api/master/projects/PRJ-404", json={"name": "x"}).status_code == 404
    assert client.post(
        "/api/master/projects", json={"name": "X", "code": "SKU/1"}
    ).status_code in (201, 400)  # 自定义编码允许自由格式，但不允许重复


def test_project_bound_to_unknown_quote_id(seeded):
    resp = client.post("/api/master/projects", json={"name": "主壳", "quote_ids": [9999]})
    assert resp.status_code == 201


def test_project_bind_and_unbind(seeded):
    client.post("/api/master/projects", json={"name": "雾化芯"})
    code = "PRJ-001"
    resp = client.post(f"/api/master/projects/{code}/bind", json={"quote_ids": [seeded["b1"]]})
    assert resp.status_code == 200
    assert resp.json()["bound_quotes"] == 1
    resp = client.post(f"/api/master/projects/{code}/unbind", json={"quote_ids": [seeded["b1"]]})
    assert resp.json()["unbound_quotes"] == 1
    assert client.post("/api/master/projects/PRJ-404/unbind", json={"quote_ids": [1]}).status_code == 404


def test_project_search(seeded):
    client.post("/api/master/projects", json={"name": "主壳"})
    client.post("/api/master/projects", json={"name": "雾化芯", "remark": "陶瓷"})
    assert len(client.get("/api/master/projects", params={"q": "雾化"}).json()["projects"]) == 1
    assert len(client.get("/api/master/projects", params={"q": "PRJ-002"}).json()["projects"]) == 1
    assert len(client.get("/api/master/projects", params={"q": "陶瓷"}).json()["projects"]) == 1


# ---------- 供应商历史曲线 ----------

def test_supplier_history_metrics_and_compare(seeded):
    client.post("/api/master/suppliers/register", json={"name": "东莞市鸿图精密压铸有限公司"})
    client.post("/api/master/suppliers/register", json={"name": "中山美格"})

    resp = client.get(
        "/api/suppliers/SUP-001/history",
        params={"metrics": "final_unit_price_taxed,materials_total", "compare_code": "SUP-002"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["supplier"]["code"] == "SUP-001"
    assert body["compare_supplier"]["code"] == "SUP-002"
    assert [m["key"] for m in body["metrics"]] == ["final_unit_price_taxed", "materials_total"]
    assert len(body["metric_options"]) == 9
    assert body["quote_count"] == 2
    assert [p["date"] for p in body["points"]] == ["2024-03-01", "2024-04-01", "2024-06-01"]
    assert body["points"][0]["date_source"] == "quote"
    assert body["points"][0]["metrics"] == {"final_unit_price_taxed": 15.44, "materials_total": 3.2}
    assert body["categories"] == [
        {"code": "CAT-SJ", "name": "塑胶件"},
        {"code": "CAT-WJWK", "name": "五金外壳"},
    ]


def test_supplier_history_filters(seeded):
    client.post("/api/master/suppliers/register", json={"name": "东莞市鸿图精密压铸有限公司"})
    only_first = client.get(
        "/api/suppliers/SUP-001/history", params={"date_from": "2024-01-01", "date_to": "2024-05-01"}
    ).json()
    assert [p["date"] for p in only_first["points"]] == ["2024-03-01"]

    by_category = client.get("/api/suppliers/SUP-001/history", params={"category_code": "CAT-SJ"}).json()
    assert [p["quote_id"] for p in by_category["points"]] == [seeded["a2"]]
    # quote_count 是供应商报价单总数（不吃筛选），filtered_count 才是当前筛选命中数
    assert by_category["quote_count"] == 2
    assert by_category["filtered_count"] == 1


def test_supplier_history_invalid_params(seeded):
    client.post("/api/master/suppliers/register", json={"name": "深圳锐进"})
    assert client.get("/api/suppliers/SUP-404/history").status_code == 404
    assert client.get("/api/suppliers/SUP-001/history", params={"metrics": "nope"}).status_code == 400
    assert client.get("/api/suppliers/SUP-001/history", params={"compare_code": "SUP-404"}).status_code == 404


def test_supplier_quotes_detail(seeded):
    client.post("/api/master/suppliers/register", json={"name": "深圳锐进"})
    body = client.get("/api/suppliers/SUP-001/quotes").json()
    assert [p["quote_id"] for p in body["points"]] == [seeded["c1"]]
    assert body["points"][0]["metrics"]["tooling_total"] is None


def test_supplier_history_falls_back_to_created_at(seeded):
    client.post("/api/master/suppliers/register", json={"name": "深圳锐进"})
    body = client.get("/api/suppliers/SUP-001/history").json()
    assert body["points"][0]["date_source"] == "created"
    assert body["categories"] == [{"code": "CAT-WJWK", "name": "五金外壳"}]

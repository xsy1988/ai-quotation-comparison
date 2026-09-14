"""报价单数据 API：列表摘要、搜索/过滤、详情（六模块明细 + 模治具 + 其它信息）、404。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.persist import persist_quote
from test_compare_engine import make_quote

BACKEND_DIR = Path(__file__).resolve().parent.parent
client = TestClient(app)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """隔离库 + 主数据 + 一个任务下的两份报价单（含其它信息）。"""
    import app.persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    db_path = tmp_path / "quotes.db"
    monkeypatch.setenv("QUOTES_DB_PATH", str(db_path))
    env = {**os.environ, "QUOTES_DB_PATH": str(db_path)}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    conn = get_connection()
    with conn:
        cur = conn.execute(
            "INSERT INTO comparison_task (project_name, status) VALUES ('报价单数据测试', 'parsed')"
        )
        task_id = cur.lastrowid
    a = persist_quote(make_quote("供应商A", True, True), task_id=task_id, file_hash="ha")["quote_id"]
    b = persist_quote(make_quote("供应商B", False, False), task_id=task_id, file_hash="hb")["quote_id"]
    with conn:
        conn.execute(
            "UPDATE quote SET other_info = ? WHERE id = ?",
            ("## 商务条款\n- 月结60天\n- 报价有效期30天", a),
        )
    yield conn, task_id, a, b
    conn.close()


def test_list_quotes_summary(seeded):
    conn, task_id, a, b = seeded
    body = client.get("/api/quotes").json()
    ids = [item["quote_id"] for item in body["items"]]
    assert ids == [b, a]  # 倒序：最新在前
    item = next(i for i in body["items"] if i["quote_id"] == a)
    assert item["supplier_name"] == "供应商A"
    assert item["task_id"] == task_id
    assert item["project_name"] == "报价单数据测试"
    assert item["part_name"] == "对比零件"
    assert item["final_unit_price_taxed"] == 10.61
    assert item["tooling_total"] == 28000
    assert item["parse_status"] == "parsed"
    assert item["line_count"] > 0
    assert item["has_other_info"] is True
    assert next(i for i in body["items"] if i["quote_id"] == b)["has_other_info"] is False


def test_list_quotes_filters(seeded):
    conn, task_id, a, b = seeded
    assert [i["quote_id"] for i in client.get("/api/quotes?q=供应商B").json()["items"]] == [b]
    assert [i["quote_id"] for i in client.get("/api/quotes?q=对比零件").json()["items"]] == [b, a]
    assert client.get("/api/quotes?q=不存在的供应商").json()["items"] == []
    assert client.get("/api/quotes?parse_status=reviewed").json()["items"] == []
    assert len(client.get(f"/api/quotes?task_id={task_id}").json()["items"]) == 2
    assert client.get("/api/quotes?task_id=999999").json()["items"] == []


def test_quote_detail(seeded):
    conn, task_id, a, b = seeded
    data = client.get(f"/api/quotes/{a}").json()
    assert data["quote_id"] == a
    assert data["supplier_name"] == "供应商A"
    assert data["project_name"] == "报价单数据测试"
    assert data["basic"]["part_name"] == "对比零件"
    assert data["summary"]["final_unit_price_taxed"] == 10.61
    assert data["summary"]["tooling_total"] == 28000

    modules = {m["module"]: m for m in data["modules"]}
    assert modules["materials"]["name"] == "材料费"
    assert modules["materials"]["total"] == 5.0
    assert modules["processing"]["total"] == 4.6
    assert modules["inspection"]["total"] == 0.3

    # 明细：工艺行带原子名与匹配信息
    processing = [line for line in data["lines"] if line["module"] == "processing"]
    assert [line["item_name"] for line in processing][:2] == ["CNC加工", "阳极氧化"]
    cnc = processing[0]
    assert cnc["atom_code"] == "AT-QX-001"
    assert cnc["atom_name"]  # 已 JOIN 出原子名称
    assert cnc["match_path"]
    assert cnc["candidate_atoms"] == []
    assert cnc["confirm_status"] == "unconfirmed"
    assert any(line["item_name"] == "损耗" for line in data["lines"] if line["module"] == "sga_tax")

    assert [t["tooling_type"] for t in data["tooling"]] == ["mold", "fixture"]
    assert data["tooling"][0]["amount"] == 25000
    assert data["tooling"][0]["cavity_count"] == 1
    assert data["tooling"][1]["type_name"] == "治具"

    assert data["other_info"].startswith("## 商务条款")
    assert data["created_at"]


def test_quote_detail_without_other_info(seeded):
    conn, task_id, a, b = seeded
    assert client.get(f"/api/quotes/{b}").json()["other_info"] is None


def test_quote_detail_404():
    assert client.get("/api/quotes/999999").status_code == 404

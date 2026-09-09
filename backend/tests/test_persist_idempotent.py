"""persist 幂等改造：task_id 复用、file_hash 查重、旧行为兼容。"""

import pytest

from app.db import get_connection, init_db
from app.persist import find_quote_by_hash, persist_quote


def make_quote(supplier: str = "测试供应商") -> dict:
    return {
        "schema_version": "1.1",
        "supplier": {"supplier_name": supplier, "supplier_code": None},
        "basic": {"part_name": "测试零件", "currency": "CNY", "category": None},
        "unit_price": {
            "materials": {"total": 4.0, "items": [{"name": "铝材", "amount_per_pc": 4.0}]},
            "processing": {
                "total": 3.0,
                "items": [
                    {
                        "name": "CNC",
                        "amount_per_pc": 3.0,
                        "atom_code": None,
                        "confidence": "high",
                        "confirm_status": "unconfirmed",
                    }
                ],
            },
            "inspection": {"total": 0.5, "items": []},
            "packaging_transport": {"total": 0.5, "items": []},
            "sga_tax": {"total": 0.91, "items": []},
            "other": {"total": 0.1, "items": []},
            "summary": {
                "untaxed_total": 8.1,
                "tax_amount": 0.91,
                "taxed_total": 9.01,
                "discount": 0.4,
                "final_unit_price_taxed": 8.61,
            },
        },
    }


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    import app.persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    init_db()
    conn = get_connection()
    with conn:
        cur = conn.execute("INSERT INTO comparison_task (project_name, status) VALUES ('既有任务', 'parsing')")
    return conn, cur.lastrowid


def test_persist_with_task_id_does_not_create_task(prepared):
    conn, task_id = prepared
    result = persist_quote(make_quote(), project_name="不应使用", task_id=task_id, file_hash="h1")
    assert result["task_id"] == task_id
    count = conn.execute("SELECT COUNT(*) FROM comparison_task").fetchone()[0]
    assert count == 1
    row = conn.execute("SELECT file_hash, parse_status FROM quote WHERE id = ?", (result["quote_id"],)).fetchone()
    assert row["file_hash"] == "h1"
    assert row["parse_status"] == "parsed"
    conn.close()


def test_find_quote_by_hash(prepared):
    conn, task_id = prepared
    assert find_quote_by_hash(conn, "hash-x") is None
    result = persist_quote(make_quote(), task_id=task_id, file_hash="hash-x")
    assert find_quote_by_hash(conn, "hash-x") == result["quote_id"]
    # 同 hash 再落一条（模拟查重后由 pipeline 决定是否复用，persist 本身照写）
    data = make_quote("另一家")
    result2 = persist_quote(data, task_id=task_id, file_hash="hash-x")
    assert find_quote_by_hash(conn, "hash-x") == result["quote_id"]
    assert result2["quote_id"] != result["quote_id"]
    conn.close()


def test_legacy_persist_creates_task(prepared):
    conn, _ = prepared
    before = conn.execute("SELECT COUNT(*) FROM comparison_task").fetchone()[0]
    result = persist_quote(make_quote(), project_name="旧式调用")
    after = conn.execute("SELECT COUNT(*) FROM comparison_task").fetchone()[0]
    assert after == before + 1
    task = conn.execute("SELECT project_name, status FROM comparison_task WHERE id = ?", (result["task_id"],)).fetchone()
    assert task["project_name"] == "旧式调用"
    conn.close()


def test_persist_without_hash_leaves_null(prepared):
    conn, task_id = prepared
    result = persist_quote(make_quote(), task_id=task_id)
    row = conn.execute("SELECT file_hash FROM quote WHERE id = ?", (result["quote_id"],)).fetchone()
    assert row["file_hash"] is None
    conn.close()

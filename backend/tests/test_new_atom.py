"""新工艺决策（new_atom_service + suggestions API）：建议聚合、三类决策、别名回流、快照/flags 同步。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.persist import collect_flags, persist_quote
from app.pipeline.mapping_runner import FALLBACK_ATOM
from app.services.new_atom_service import list_suggestions, sync_suggestions

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote_unmatched.json"

client = TestClient(app)

NEW_ITEM = "EDM"


def _conn():
    return get_connection()


def _snapshot_path(quote_id):
    conn = _conn()
    path = conn.execute("SELECT raw_json_path FROM quote WHERE id = ?", (quote_id,)).fetchone()[0]
    conn.close()
    return path


def _snapshot(quote_id):
    return json.loads(open(_snapshot_path(quote_id), encoding="utf-8").read())


def _write_snapshot(quote_id, data):
    Path(_snapshot_path(quote_id)).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _lines(quote_id, item_name=NEW_ITEM):
    conn = _conn()
    rows = list(
        conn.execute(
            "SELECT * FROM quote_line WHERE quote_id = ? AND item_name = ?",
            (quote_id, item_name),
        )
    )
    conn.close()
    return rows


@pytest.fixture
def prepared_task(tmp_path, monkeypatch):
    """灌主数据 + 同一任务下两张报价单，EDM 条目标记为兜底新工艺（DB 与快照一致）。"""
    import app.persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "newatom.db"))
    env = {**os.environ, "QUOTES_DB_PATH": str(tmp_path / "newatom.db")}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    first = persist_quote(data, project_name="新工艺测试")
    second = persist_quote(data, task_id=first["task_id"])

    quote_ids = [first["quote_id"], second["quote_id"]]
    conn = _conn()
    for quote_id in quote_ids:
        line = _lines(quote_id)[0]
        with conn:
            conn.execute(
                """UPDATE quote_line SET atom_code = ?, is_new_process = 1,
                   confidence = 'low', match_path = 'L2_llm',
                   candidate_atoms = ?, fingerprint = ?
                   WHERE id = ?""",
                (
                    FALLBACK_ATOM,
                    json.dumps([FALLBACK_ATOM], ensure_ascii=False),
                    FALLBACK_ATOM,
                    line["id"],
                ),
            )
        snap = _snapshot(quote_id)
        item = next(i for i in snap["unit_price"]["processing"]["items"] if i["name"] == NEW_ITEM)
        item.update(
            {
                "atom_code": FALLBACK_ATOM,
                "is_new_process": True,
                "confidence": "low",
                "match_path": "llm_semantic",
                "bundle_members": [FALLBACK_ATOM],
                "bundle_fingerprint": FALLBACK_ATOM,
                "bundle_flag": False,
            }
        )
        _write_snapshot(quote_id, snap)
        check = conn.execute(
            "SELECT calc_check FROM quote WHERE id = ?", (quote_id,)
        ).fetchone()["calc_check"]
        flags = collect_flags(snap, check or "unchecked")
        assert "new_process" in flags
        with conn:
            conn.execute(
                "UPDATE quote SET flags = ? WHERE id = ?",
                (json.dumps(flags, ensure_ascii=False), quote_id),
            )
    conn.close()
    return first["task_id"], quote_ids


def _suggestion_id(task_id):
    conn = _conn()
    sync_suggestions(conn, task_id)
    row = conn.execute(
        "SELECT id FROM new_atom_suggestion WHERE source_text = ?", (NEW_ITEM,)
    ).fetchone()
    conn.close()
    assert row is not None
    return row["id"]


def _max_seq(domain_code):
    import re

    conn = _conn()
    codes = [r["code"] for r in conn.execute(
        "SELECT code FROM atom WHERE domain_code = ?", (domain_code,)
    )]
    conn.close()
    seqs = [int(m.group(1)) for c in codes for m in [re.match(rf"^AT-{domain_code}-(\d+)$", c)] if m]
    return max(seqs, default=0)


# ---------- sync 聚合 ----------

def test_sync_aggregates_across_quotes_and_is_idempotent(prepared_task):
    task_id, quote_ids = prepared_task

    conn = _conn()
    pending = sync_suggestions(conn, task_id)
    conn.close()
    assert pending == 1

    conn = _conn()
    suggestions = list_suggestions(conn, task_id)
    conn.close()
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s["source_text"] == NEW_ITEM
    assert s["occurrence_count"] == 2
    assert s["quote_id"] in quote_ids
    assert s["amount"] is not None

    # 幂等：重复 sync / list 不翻倍
    conn = _conn()
    assert sync_suggestions(conn, task_id) == 1
    assert len(list_suggestions(conn, task_id)) == 1
    conn.close()

    resp = client.get(f"/api/tasks/{task_id}/suggestions")
    assert resp.status_code == 200
    body = resp.json()["suggestions"]
    assert len(body) == 1
    assert body[0]["occurrence_count"] == 2


def test_sync_keeps_resolved_status(prepared_task):
    """已决策的 source_text 不被 sync 重新翻回 pending。"""
    task_id, _ = prepared_task
    sid = _suggestion_id(task_id)
    conn = _conn()
    with conn:
        conn.execute(
            "UPDATE new_atom_suggestion SET status = 'ignored' WHERE id = ?", (sid,)
        )
    assert sync_suggestions(conn, task_id) == 0
    row = conn.execute(
        "SELECT status, occurrence_count FROM new_atom_suggestion WHERE id = ?", (sid,)
    ).fetchone()
    conn.close()
    assert row["status"] == "ignored"
    assert row["occurrence_count"] == 2  # 非 pending 不动计数


# ---------- create ----------

def test_create_new_atom(prepared_task):
    task_id, quote_ids = prepared_task
    sid = _suggestion_id(task_id)
    expected_code = f"AT-TJ-{_max_seq('TJ') + 1:03d}"

    resp = client.post(
        f"/api/suggestions/{sid}/resolve",
        json={
            "action": "create",
            "name": "电火花加工",
            "domain_code": "TJ",
            "stage_name": "机加",
            "class_name": "主制程",
            "category_code": "CAT-WJWK",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"suggestion_id": sid, "action": "create", "atom_code": expected_code}

    conn = _conn()
    atom = conn.execute(
        "SELECT * FROM atom WHERE code = ?", (expected_code,)
    ).fetchone()
    alias = conn.execute(
        """SELECT source FROM atom_alias WHERE atom_code = ? AND alias_text = ?""",
        (expected_code, NEW_ITEM),
    ).fetchone()
    cat = conn.execute(
        "SELECT 1 FROM atom_category WHERE atom_code = ? AND category_code = 'CAT-WJWK'",
        (expected_code,),
    ).fetchone()
    suggestion = conn.execute(
        "SELECT status FROM new_atom_suggestion WHERE id = ?", (sid,)
    ).fetchone()
    flags = {
        r["id"]: json.loads(r["flags"] or "[]")
        for r in conn.execute(
            "SELECT id, flags FROM quote WHERE id IN (?, ?)", tuple(quote_ids)
        )
    }
    conn.close()

    assert atom["name"] == "电火花加工"
    assert atom["domain_code"] == "TJ"
    assert atom["stage_name"] == "机加"
    assert atom["class_name"] == "主制程"
    assert atom["is_fallback"] == 0
    assert alias["source"] == "new_process"
    assert cat is not None
    assert suggestion["status"] == "created"

    for quote_id in quote_ids:
        line = _lines(quote_id)[0]
        assert line["atom_code"] == expected_code
        assert line["match_path"] == "manual"
        assert line["confidence"] == "high"
        assert line["is_new_process"] == 0
        assert line["confirm_status"] == "corrected"
        assert line["fingerprint"] == expected_code
        assert json.loads(line["candidate_atoms"]) == [expected_code]

        item = next(
            i for i in _snapshot(quote_id)["unit_price"]["processing"]["items"]
            if i["name"] == NEW_ITEM
        )
        assert item["atom_code"] == expected_code
        assert item["match_path"] == "manual"
        assert item["is_new_process"] is False
        assert item["bundle_members"] == [expected_code]

        assert "new_process" not in flags[quote_id]
        assert _snapshot(quote_id)["basic"]["parse_status"] == "reviewed"


def test_create_collision_retries_next_code(prepared_task):
    """并发撞号：预占下一个编码后 resolve，服务应重新取号成功。"""
    task_id, _ = prepared_task
    sid = _suggestion_id(task_id)
    stolen = f"AT-TJ-{_max_seq('TJ') + 1:03d}"
    conn = _conn()
    with conn:
        conn.execute(
            """INSERT INTO atom (code, name, domain_code, stage_name, class_name)
               VALUES (?, '占位', 'TJ', '机加', '主制程')""",
            (stolen,),
        )
    conn.close()
    expected_code = f"AT-TJ-{_max_seq('TJ') + 1:03d}"
    assert expected_code != stolen

    resp = client.post(
        f"/api/suggestions/{sid}/resolve",
        json={
            "action": "create",
            "name": "电火花加工",
            "domain_code": "TJ",
            "stage_name": "机加",
            "class_name": "主制程",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["atom_code"] == expected_code


# ---------- merge ----------

def test_merge_existing_atom(prepared_task):
    task_id, quote_ids = prepared_task
    sid = _suggestion_id(task_id)

    resp = client.post(
        f"/api/suggestions/{sid}/resolve",
        json={"action": "merge", "atom_code": "AT-QX-001"},
    )
    assert resp.status_code == 200
    assert resp.json()["atom_code"] == "AT-QX-001"

    conn = _conn()
    alias = conn.execute(
        """SELECT source FROM atom_alias WHERE atom_code = 'AT-QX-001'
           AND alias_text = ?""",
        (NEW_ITEM,),
    ).fetchone()
    suggestion = conn.execute(
        "SELECT status FROM new_atom_suggestion WHERE id = ?", (sid,)
    ).fetchone()
    conn.close()
    assert alias["source"] == "new_process"
    assert suggestion["status"] == "merged"

    for quote_id in quote_ids:
        line = _lines(quote_id)[0]
        assert line["atom_code"] == "AT-QX-001"
        assert line["match_path"] == "manual"
        assert line["is_new_process"] == 0
        item = next(
            i for i in _snapshot(quote_id)["unit_price"]["processing"]["items"]
            if i["name"] == NEW_ITEM
        )
        assert item["atom_code"] == "AT-QX-001"
        assert item["is_new_process"] is False


# ---------- ignore ----------

def test_ignore_keeps_fallback_atom(prepared_task):
    task_id, quote_ids = prepared_task
    sid = _suggestion_id(task_id)

    resp = client.post(
        f"/api/suggestions/{sid}/resolve", json={"action": "ignore"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"suggestion_id": sid, "action": "ignore"}

    conn = _conn()
    suggestion = conn.execute(
        "SELECT status FROM new_atom_suggestion WHERE id = ?", (sid,)
    ).fetchone()
    alias = conn.execute(
        "SELECT 1 FROM atom_alias WHERE alias_text = ? AND source = 'new_process'",
        (NEW_ITEM,),
    ).fetchone()
    conn.close()
    assert suggestion["status"] == "ignored"
    assert alias is None  # ignore 不产生 new_process 别名回流

    for quote_id in quote_ids:
        line = _lines(quote_id)[0]
        assert line["atom_code"] == FALLBACK_ATOM  # 保留兜底原子
        assert line["match_path"] == "L2_llm"  # 匹配路径不变
        assert line["is_new_process"] == 0
        item = next(
            i for i in _snapshot(quote_id)["unit_price"]["processing"]["items"]
            if i["name"] == NEW_ITEM
        )
        assert item["atom_code"] == FALLBACK_ATOM
        assert item["is_new_process"] is False


# ---------- 错误分支 ----------

def test_not_found_and_invalid_actions(prepared_task):
    task_id, _ = prepared_task
    sid = _suggestion_id(task_id)

    resp = client.post("/api/suggestions/999999/resolve", json={"action": "ignore"})
    assert resp.status_code == 404

    resp = client.post(f"/api/suggestions/{sid}/resolve", json={"action": "bogus"})
    assert resp.status_code == 400
    assert "action 非法" in resp.json()["detail"]

    resp = client.post(
        f"/api/suggestions/{sid}/resolve",
        json={"action": "create", "name": "x", "domain_code": "NOPE",
              "stage_name": "机加", "class_name": "主制程"},
    )
    assert resp.status_code == 400
    assert "工艺域不存在" in resp.json()["detail"]

    resp = client.post(
        f"/api/suggestions/{sid}/resolve",
        json={"action": "create", "name": "x", "domain_code": "TJ",
              "stage_name": "不存在阶段", "class_name": "主制程"},
    )
    assert resp.status_code == 400
    assert "工艺阶段不存在" in resp.json()["detail"]

    resp = client.post(
        f"/api/suggestions/{sid}/resolve",
        json={"action": "create", "name": "x", "domain_code": "TJ",
              "stage_name": "机加", "class_name": "主制程", "category_code": "CAT-NOPE"},
    )
    assert resp.status_code == 400
    assert "品类不存在" in resp.json()["detail"]

    resp = client.post(f"/api/suggestions/{sid}/resolve", json={"action": "merge"})
    assert resp.status_code == 400

    resp = client.post(
        f"/api/suggestions/{sid}/resolve",
        json={"action": "merge", "atom_code": "AT-XX-999"},
    )
    assert resp.status_code == 400
    assert "原子不存在" in resp.json()["detail"]

    resp = client.get("/api/tasks/999999/suggestions")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "任务不存在"}


# ---------- 枚举接口 ----------

def test_enum_apis(prepared_task):
    resp = client.get("/api/domains")
    assert resp.status_code == 200
    domains = resp.json()
    assert {"code": "TJ", "name": "特种加工"} in domains
    assert {"code": "QX", "name": "切削"} in domains

    resp = client.get("/api/stages")
    assert resp.status_code == 200
    assert "机加" in resp.json()

    resp = client.get("/api/classes")
    assert resp.status_code == 200
    assert set(resp.json()) == {"主制程", "后工序", "成型加工"}

"""mapping_runner 端到端：persist → run_mapping → quote_line 与快照一致。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.db import get_connection, init_db
from app.persist import persist_quote
from app.pipeline.mapping_runner import run_mapping

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote_unmatched.json"


@pytest.fixture
def quote_id(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "t.db"))
    import app.persist as persist_module
    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    # 临时库先灌入主数据（词库依赖）
    env = {**os.environ, "QUOTES_DB_PATH": str(tmp_path / "t.db")}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    _mock_l2_empty(monkeypatch)  # L2 批处理返回空映射 → L1 未命中条目全部走兜底新工艺
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = persist_quote(data, project_name="mapping测试")
    return result["quote_id"]


def _mock_l2_empty(monkeypatch):
    """chat_json 替身：L2 语义匹配返回空映射（所有条目判为清单外），不触网。"""
    from app.llm import client as llm_client

    def _fake_chat(messages, **kwargs):
        return {"matches": {}}, {"model": "fake", "total_tokens": 10, "elapsed_ms": 1}

    monkeypatch.setattr(llm_client, "chat_json", _fake_chat)


def _lines(quote_id, module):
    conn = get_connection()
    rows = conn.execute(
        "SELECT item_name, atom_code, confidence, match_path, fingerprint, item_type, note, rate"
        " FROM quote_line WHERE quote_id=? AND module=? ORDER BY id",
        (quote_id, module),
    ).fetchall()
    conn.close()
    return {r["item_name"]: dict(r) for r in rows}


def test_l1_matches_expected_atoms(quote_id):
    run_mapping(quote_id)
    lines = _lines(quote_id, "processing")
    assert lines["CNC加工"]["atom_code"] == "AT-QX-001"
    assert lines["阳极氧化"]["atom_code"] == "AT-ZH-013"
    assert lines["喷砂"]["atom_code"] == "AT-ZP-005"
    assert lines["CNC加工"]["confidence"] == "high"
    assert lines["CNC加工"]["match_path"] == "L1_alias"
    assert lines["CNC加工"]["fingerprint"] == "AT-QX-001"


def test_ambiguous_and_unknown_go_to_l2_fallback(quote_id):
    """L1 未命中（歧义/未知）→ L2 判清单外 → 兜底原子 AT-QT-001 + 新工艺标记。"""
    stats = run_mapping(quote_id)
    lines = _lines(quote_id, "processing")
    assert lines["EDM"]["atom_code"] == "AT-QT-001"
    assert lines["EDM"]["match_path"] == "L2_llm"
    assert lines["激光熔覆"]["atom_code"] == "AT-QT-001"
    assert lines["激光熔覆"]["match_path"] == "L2_llm"
    assert stats["ambiguous"] == 1  # L1 统计口径不变
    assert stats["unmatched"] == 2
    assert stats["l2_new_process"] == 2

    conn = get_connection()
    terms = {r[0] for r in conn.execute("SELECT term_text FROM unmatched_term")}
    conn.close()
    assert "EDM" in terms and "激光熔覆" in terms


def test_hit_count_incremented(quote_id):
    run_mapping(quote_id)
    conn = get_connection()
    hits = dict(conn.execute(
        "SELECT alias_text, hit_count FROM atom_alias WHERE alias_text IN ('阳极氧化','喷砂','CNC加工')"
    ).fetchall())
    conn.close()
    # 原子名命中不计 hit_count；这三条均按原子名命中（别名表中本就没有这些写法）
    assert hits.get("阳极氧化", 0) == 0


def test_fee_classify_applied(quote_id):
    run_mapping(quote_id)
    lines = _lines(quote_id, "packaging_transport")
    assert lines["顺丰运费"]["item_type"] == "运输"


def test_flags_updated(quote_id):
    run_mapping(quote_id)
    conn = get_connection()
    flags = json.loads(conn.execute("SELECT flags FROM quote WHERE id=?", (quote_id,)).fetchone()[0])
    conn.close()
    assert "new_process" in flags  # L2 兜底后不再有未匹配条目
    assert "low_confidence" in flags
    assert "unmatched" not in flags


def test_snapshot_synced(quote_id):
    run_mapping(quote_id)
    conn = get_connection()
    path = conn.execute("SELECT raw_json_path FROM quote WHERE id=?", (quote_id,)).fetchone()[0]
    conn.close()
    data = json.loads(open(path, encoding="utf-8").read())
    by_name = {i["name"]: i for i in data["unit_price"]["processing"]["items"]}
    assert by_name["CNC加工"]["atom_code"] == "AT-QX-001"
    assert by_name["EDM"]["atom_code"] == "AT-QT-001"  # L2 兜底新工艺
    assert by_name["EDM"]["is_new_process"] is True

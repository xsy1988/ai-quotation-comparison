"""pipeline 编排：create_task 落盘建任务、run_task 全链路、查重复用、单文件失败隔离。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.ingest as ingest_module
import app.persist as persist_module
import app.pipeline.pipeline as pipeline_module
from app.db import get_connection, init_db
from app.pipeline.pipeline import UPLOAD_DIR, create_task, run_task

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote.xlsx"


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """隔离目录 + 灌主数据（映射词库依赖）+ mock LLM 版面理解，返回 (conn, tmp_path)。"""
    monkeypatch.setattr(pipeline_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(ingest_module, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(ingest_module, "IR_DIR", tmp_path / "ir")
    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    db_path = tmp_path / "pipeline.db"
    monkeypatch.setenv("QUOTES_DB_PATH", str(db_path))
    env = {**os.environ, "QUOTES_DB_PATH": str(db_path)}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    _mock_llm_layout(monkeypatch)
    conn = get_connection()
    return conn, tmp_path


def _mock_llm_layout(monkeypatch):
    """chat_json 替身：对样例文件返回规则解析结果（等价于 LLM 完美输出），不触网。"""
    from app.ingest import excel_to_ir, sha256_of
    from app.llm import client as llm_client
    from app.pipeline.simple_excel_parse import parse_ir as rule_parse_ir

    def _fake_chat(messages, **kwargs):
        data = rule_parse_ir(excel_to_ir(FIXTURE, sha256_of(FIXTURE)))
        data["basic"]["category"] = "CAT-WJWK"
        return data, {"model": "fake", "total_tokens": 10, "elapsed_ms": 1}

    monkeypatch.setattr(llm_client, "chat_json", _fake_chat)


def _new_task(conn, project_name, files):
    return create_task(conn, project_name, files)


def test_create_task_writes_uploads(prepared):
    conn, tmp_path = prepared
    task_id = _new_task(conn, "落盘测试", [("sample_quote.xlsx", FIXTURE.read_bytes())])
    task = conn.execute("SELECT project_name, status FROM comparison_task WHERE id = ?", (task_id,)).fetchone()
    assert task["status"] == "parsing"
    assert (tmp_path / "uploads" / str(task_id) / "sample_quote.xlsx").exists()
    logs = conn.execute("SELECT COUNT(*) FROM parse_log WHERE task_id = ? AND stage='task'", (task_id,)).fetchone()[0]
    assert logs == 1
    # 预建 quote 占位行：进度页在上传后立即可见该文件（流水线未跑也是 pending）
    quotes = conn.execute(
        "SELECT supplier_name, parse_status FROM quote WHERE task_id = ?", (task_id,)
    ).fetchall()
    assert [(q["supplier_name"], q["parse_status"]) for q in quotes] == [("sample_quote.xlsx", "pending")]
    conn.close()


def test_run_task_full_chain(prepared):
    conn, tmp_path = prepared
    task_id = _new_task(conn, "全链路", [("sample_quote.xlsx", FIXTURE.read_bytes())])
    summary = run_task(task_id, conn)
    assert summary["task_status"] == "parsed"

    task = conn.execute("SELECT status FROM comparison_task WHERE id = ?", (task_id,)).fetchone()
    assert task["status"] == "parsed"
    quote = conn.execute("SELECT * FROM quote WHERE task_id = ?", (task_id,)).fetchone()
    assert quote["parse_status"] == "parsed"
    assert quote["calc_check"] == "pass"
    assert quote["supplier_name"] == "深圳市样例五金有限公司"
    assert quote["file_hash"]

    # 映射已跑：CNC 命中 L1 原子
    lines = conn.execute(
        "SELECT atom_code, match_path FROM quote_line WHERE quote_id = ? AND module='processing'",
        (quote["id"],),
    ).fetchall()
    cnc = next(r for r in lines if r["atom_code"] == "AT-QX-001")
    assert cnc["match_path"] == "L1_alias"

    # parse_log 覆盖各段
    stages = {r[0] for r in conn.execute("SELECT DISTINCT stage FROM parse_log WHERE task_id = ?", (task_id,))}
    assert {"task", "dedup", "layout", "validate", "persist", "match"} <= stages
    conn.close()


def test_run_task_dedup_reuse(prepared):
    conn, tmp_path = prepared
    content = FIXTURE.read_bytes()

    task1 = _new_task(conn, "首次", [("sample_quote.xlsx", content)])
    run_task(task1, conn)
    quote1 = conn.execute("SELECT id FROM quote WHERE task_id = ?", (task1,)).fetchone()[0]

    task2 = _new_task(conn, "二次", [("sample_quote.xlsx", content)])
    summary = run_task(task2, conn)
    assert summary["results"][0]["status"] == "reused"
    quote2 = conn.execute("SELECT id FROM quote WHERE task_id = ?", (task2,)).fetchone()[0]
    assert quote2 != quote1

    # 复用而非重新解析：快照内容一致、勾稽仍 pass、新任务有自己的 quote 行
    snap1 = json.loads(Path(conn.execute("SELECT raw_json_path FROM quote WHERE id=?", (quote1,)).fetchone()[0]).read_text(encoding="utf-8"))
    snap2 = json.loads(Path(conn.execute("SELECT raw_json_path FROM quote WHERE id=?", (quote2,)).fetchone()[0]).read_text(encoding="utf-8"))
    assert snap2["supplier"] == snap1["supplier"]
    q2 = conn.execute("SELECT calc_check, parse_status FROM quote WHERE id = ?", (quote2,)).fetchone()
    assert q2["calc_check"] == "pass"
    assert q2["parse_status"] == "parsed"
    conn.close()


def test_run_task_failure_isolated(prepared):
    conn, tmp_path = prepared
    task_id = _new_task(
        conn,
        "部分失败",
        [("good.xlsx", FIXTURE.read_bytes()), ("bad.txt", b"not an excel")],
    )
    summary = run_task(task_id, conn)
    statuses = sorted(r["status"] for r in summary["results"])
    assert statuses == ["failed", "parsed"]

    task = conn.execute("SELECT status FROM comparison_task WHERE id = ?", (task_id,)).fetchone()
    assert task["status"] == "parsed"  # 部分失败也收尾为 parsed

    failed = conn.execute(
        "SELECT id, supplier_name, parse_status FROM quote WHERE task_id = ? AND parse_status='failed'",
        (task_id,),
    ).fetchone()
    assert failed["supplier_name"] == "bad.txt"
    basic = json.loads(conn.execute("SELECT basic_info FROM quote WHERE id = ?", (failed["id"],)).fetchone()[0])
    assert "不支持的文件格式" in basic["error"]
    conn.close()

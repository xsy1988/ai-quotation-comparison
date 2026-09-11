"""文件级并发解析：ThreadPoolExecutor 并发执行、单文件失败隔离、结果按上传顺序、终态判定。

_process_file 用 monkeypatch 替身（不触网、不落库），只验证 run_task 的线程/收口模型；
失败路径走真实 _mark_failed（worker 线程自建连接写 tmp 库），同时覆盖并发写日志不锁库。
"""

import json
import threading
import time

import pytest

import app.pipeline.pipeline as pipeline_module
from app.db import get_connection, init_db
from app.pipeline.pipeline import create_task, run_task
from app.pipeline.simple_excel_parse import ParseError

FILES = [("a.xlsx", b"a"), ("bad.xlsx", b"b"), ("c.xlsx", b"c")]


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setenv("PARSE_CONCURRENCY", "3")
    init_db()
    conn = get_connection()
    yield conn
    conn.close()


def _task(conn, files=FILES):
    return create_task(conn, "并发解析", files)


def test_run_task_concurrent_and_failure_isolated(prepared, monkeypatch):
    """3 文件并发跑（观测最大并发 3）；单文件 ParseError 只影响自己；终态 parsed。"""
    conn = prepared
    lock = threading.Lock()
    active = 0
    max_active = 0
    seen_threads: set[str] = set()

    def _fake(conn, task_id, project_name, path, quote_id=None):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            seen_threads.add(threading.current_thread().name)
        time.sleep(0.2)  # 模拟 LLM 解析耗时，制造重叠窗口
        with lock:
            active -= 1
        if path.name == "bad.xlsx":
            raise ParseError("模拟解析失败")
        return {"quote_ids": [quote_id], "quote_id": quote_id, "status": "parsed", "offers": 1}

    monkeypatch.setattr(pipeline_module, "_process_file", _fake)
    task_id = _task(conn)
    summary = run_task(task_id, conn)

    assert max_active == 3  # PARSE_CONCURRENCY=3，3 个文件同时进入解析
    assert len(seen_threads) >= 2
    # 结果按上传顺序回填（与完成顺序无关）
    assert [r["status"] for r in summary["results"]] == ["parsed", "failed", "parsed"]
    assert summary["task_status"] == "parsed"

    # 失败文件落库标 failed（worker 线程自建连接写入），其余不受影响
    rows = conn.execute(
        "SELECT supplier_name, parse_status FROM quote WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    assert [(r["supplier_name"], r["parse_status"]) for r in rows] == [
        ("a.xlsx", "pending"),  # _process_file 是替身，不回填占位行
        ("bad.xlsx", "failed"),
        ("c.xlsx", "pending"),
    ]
    basic = json.loads(conn.execute(
        "SELECT basic_info FROM quote WHERE task_id = ? AND parse_status='failed'", (task_id,)
    ).fetchone()[0])
    assert "模拟解析失败" in basic["error"]
    conn.close()


def test_run_task_results_keep_upload_order(prepared, monkeypatch):
    """完成顺序与上传顺序相反时，results 仍按上传顺序排列。"""
    conn = prepared

    def _fake(conn, task_id, project_name, path, quote_id=None):
        if path.name == "a.xlsx":
            time.sleep(0.3)  # 最先提交、最后完成
        return {"quote_ids": [quote_id], "quote_id": quote_id, "status": "parsed", "file": path.name}

    monkeypatch.setattr(pipeline_module, "_process_file", _fake)
    task_id = _task(conn)
    summary = run_task(task_id, conn)
    assert [r["file"] for r in summary["results"]] == ["a.xlsx", "bad.xlsx", "c.xlsx"]
    assert summary["task_status"] == "parsed"
    conn.close()


def test_run_task_all_failed_marks_task_failed(prepared, monkeypatch):
    """全部文件失败 → 任务终态 failed。"""
    conn = prepared

    def _always_fail(conn, task_id, project_name, path, quote_id=None):
        raise ValueError("全灭")

    monkeypatch.setattr(pipeline_module, "_process_file", _always_fail)
    task_id = _task(conn)
    summary = run_task(task_id, conn)
    assert [r["status"] for r in summary["results"]] == ["failed"] * 3
    assert summary["task_status"] == "failed"
    status = conn.execute(
        "SELECT status FROM comparison_task WHERE id = ?", (task_id,)
    ).fetchone()[0]
    assert status == "failed"
    failed = conn.execute(
        "SELECT COUNT(*) FROM quote WHERE task_id = ? AND parse_status='failed'", (task_id,)
    ).fetchone()[0]
    assert failed == 3
    conn.close()

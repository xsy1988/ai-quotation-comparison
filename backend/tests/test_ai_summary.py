"""AI 综合建议 + 数字回检：正常生成、编造金额触发重生成、mismatch/pass、LLM 故障 502。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.compare.ai_summary import generate_ai_summary, get_ai_summary
from app.db import get_connection, init_db
from app.llm.client import LLMUnavailable
from app.main import app
from app.persist import persist_quote
from test_compare_engine import make_quote

BACKEND_DIR = Path(__file__).resolve().parent.parent
client = TestClient(app)


@pytest.fixture
def task(tmp_path, monkeypatch):
    """隔离库 + 灌主数据 + 两个供应商报价单，返回 (conn, task_id)。"""
    import app.persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    db_path = tmp_path / "ai.db"
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
            "INSERT INTO comparison_task (project_name, status) VALUES ('AI建议测试', 'parsed')"
        )
        task_id = cur.lastrowid
    persist_quote(make_quote("供应商A", True, True), task_id=task_id, file_hash="ha")
    persist_quote(make_quote("供应商B", False, False), task_id=task_id, file_hash="hb")
    yield conn, task_id
    conn.close()


def _fake_chat(markdowns):
    """按序返回 markdown 的 chat_json 替身，记录调用次数。"""
    calls = []

    def _chat(messages, **kwargs):
        calls.append(list(messages))
        md = markdowns[min(len(calls) - 1, len(markdowns) - 1)]
        return {"markdown": md}, {"model": "fake", "total_tokens": 50, "elapsed_ms": 3}

    _chat.calls = calls
    return _chat


_GOOD_MD = (
    "# 综合建议\n"
    "推荐供应商B，最终含税单价 8.5 元，低于供应商A 的 10.61 元，差 2.11 元。\n"
    "供应商A 加工费 4.6 元偏高；模治具 A 25000 元 / B 15000 元，供应商A 另有检具 3000 元。\n"
    "注意：供应商A 存在未匹配与低置信条目，折扣 0.5 元后含税合计 11.11 元，"
    "议价时可要求其拆分报价明细。"
)


def test_extract_amounts_ignores_markdown_headings():
    """数字回检：markdown 标题节号（## 2.1 xxx）不是金额，不得误报。"""
    from app.compare.ai_summary import _extract_amounts

    md = "## 2.1 费用模块对比\n## 2.2 工艺对比\n| 列A | 列B |\n| :--- | ---: |\n| 材料费 | 10.51 元 |\n"
    assert _extract_amounts(md) == [10.51]


def test_generate_pass(task):
    conn, task_id = task
    summary = generate_ai_summary(conn, task_id, chat_fn=_fake_chat([_GOOD_MD]))

    assert summary["task_id"] == task_id
    assert summary["content"] == _GOOD_MD
    assert summary["check_status"] == "pass"
    assert summary["check_detail"]["suspicious"] == []
    assert summary["id"] is not None
    assert summary["created_at"]

    # 落库与 get_ai_summary 一致
    assert get_ai_summary(conn, task_id)["id"] == summary["id"]

    # parse_log：一次 LLM 调用 + 数字回检
    logs = conn.execute(
        "SELECT action, is_llm_call, detail FROM parse_log"
        " WHERE task_id=? AND stage='ai_summary' ORDER BY id",
        (task_id,),
    ).fetchall()
    assert [l["action"] for l in logs] == ["generated", "numeric_check"]
    assert logs[0]["is_llm_call"] == 1
    assert json.loads(logs[0]["detail"])["tokens"] == 50
    assert json.loads(logs[1]["detail"])["check_status"] == "pass"


def test_fabricated_amount_regenerates_then_mismatch(task):
    conn, task_id = task
    bad_md = "推荐供应商A，其含税总价仅 999.99 元，极具优势，CNC 2.0 元也便宜。"
    chat = _fake_chat([bad_md, bad_md])  # 重生成仍含编造金额
    summary = generate_ai_summary(conn, task_id, chat_fn=chat)

    assert len(chat.calls) == 2  # 触发 1 次重生成
    assert summary["check_status"] == "mismatch"
    suspicious = summary["check_detail"]["suspicious"]
    assert any(s["value"] == 999.99 for s in suspicious)
    assert summary["check_detail"]["rounds"] == 2

    logs = conn.execute(
        "SELECT action FROM parse_log WHERE task_id=? AND stage='ai_summary' ORDER BY id",
        (task_id,),
    ).fetchall()
    assert [l["action"] for l in logs] == ["generated", "regenerated", "numeric_check"]

    row = conn.execute(
        "SELECT check_status, check_detail FROM ai_summary WHERE task_id=?", (task_id,)
    ).fetchone()
    assert row["check_status"] == "mismatch"
    assert any(s["value"] == 999.99 for s in json.loads(row["check_detail"])["suspicious"])


def test_regenerated_amounts_pass(task):
    conn, task_id = task
    bad_md = "推荐供应商A，其含税总价仅 999.99 元，极具优势。"
    chat = _fake_chat([bad_md, _GOOD_MD])  # 重生成后数字合规
    summary = generate_ai_summary(conn, task_id, chat_fn=chat)

    assert len(chat.calls) == 2
    assert summary["check_status"] == "pass"
    assert summary["check_detail"]["rounds"] == 2


def test_llm_unavailable_propagates_and_api_502(task):
    conn, task_id = task
    # conftest 禁网替身：直接上抛 LLMUnavailable，不允许降级
    with pytest.raises(LLMUnavailable):
        generate_ai_summary(conn, task_id)

    resp = client.post(f"/api/tasks/{task_id}/ai-summary")
    assert resp.status_code == 502
    assert "禁用真实 LLM" in resp.json()["detail"]

    # 失败不落库
    assert get_ai_summary(conn, task_id) is None


def test_get_ai_summary_null_and_404(task):
    conn, task_id = task
    resp = client.get(f"/api/tasks/{task_id}/ai-summary")
    assert resp.status_code == 200
    assert resp.json() == {"summary": None}

    resp = client.get("/api/tasks/999999/ai-summary")
    assert resp.status_code == 404

    resp = client.post("/api/tasks/999999/ai-summary")
    assert resp.status_code == 404


def test_generate_task_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "none.db"))
    init_db()
    conn = get_connection()
    with pytest.raises(ValueError):
        generate_ai_summary(conn, 12345, chat_fn=_fake_chat([_GOOD_MD]))
    conn.close()

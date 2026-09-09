"""LLM 版面理解：正常解析+品类判定、schema 重试、持续失败中止任务、cross_check 对账与落库。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.db import get_connection, init_db
from app.ingest import excel_to_ir, sha256_of
from app.llm.client import LLMUnavailable
import app.persist as persist_module
from app.persist import persist_quote
from app.pipeline.layout_understand import (
    cross_check,
    parse_ir_with_llm,
    parse_ir_with_llm_traced,
)
from app.pipeline.simple_excel_parse import parse_ir as rule_parse_ir
from app.validate.validate import validate_quote

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote.xlsx"


def load_ir():
    return excel_to_ir(FIXTURE, sha256_of(FIXTURE))


class FakeChat:
    """chat_fn 替身：按队列返回 (dict, usage) 或抛异常，并记录收到的 messages。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append(messages)
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp, {"model": "fake", "total_tokens": 100, "elapsed_ms": 5}


def valid_quote(category="CAT-WJWK") -> dict:
    data = rule_parse_ir(load_ir())
    data["basic"]["category"] = category
    return data


def invalid_quote() -> dict:
    """currency 违反枚举，必触发 Draft7 校验错误。"""
    data = valid_quote()
    data["basic"]["currency"] = "RMB"
    return data


def test_parse_llm_normal_and_category():
    fake = FakeChat(valid_quote())
    data, attempts, cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    validate_quote(data)  # LLM 输出本身过 Draft7
    assert data["basic"]["category"] == "CAT-WJWK"
    assert attempts[0]["validation_errors"] is None
    assert cross["item_conflicts"] == 0
    # 品类清单进了 prompt
    prompt = fake.calls[0][-1]["content"]
    for code in ("CAT-WJWK", "CAT-PCBA", "CAT-BC"):
        assert code in prompt
    # processing 映射阶段字段的规约
    assert '"low"' in prompt and "match_path" in prompt


def test_parse_llm_schema_retry_once_then_success():
    fake = FakeChat(invalid_quote(), valid_quote())
    data, attempts, _cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 2
    assert attempts[0]["validation_errors"]  # 第一轮有校验错误
    assert attempts[1]["validation_errors"] is None
    # 第二轮对话里带上了校验错误反馈
    retry_text = fake.calls[1][-1]["content"]
    assert "currency" in retry_text
    assert data["basic"]["currency"] == "CNY"


def test_parse_llm_persistent_invalid_raises_unavailable():
    fake = FakeChat(invalid_quote(), invalid_quote())
    with pytest.raises(LLMUnavailable) as exc_info:
        parse_ir_with_llm(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 2  # 只重试 1 次
    attempts = exc_info.value.attempts
    assert len(attempts) == 2
    assert attempts[1]["validation_errors"]


def test_parse_llm_gateway_unavailable_no_retry():
    fake = FakeChat(LLMUnavailable("网关挂了"))
    with pytest.raises(LLMUnavailable):
        parse_ir_with_llm(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 1  # 网关类错误立即上抛，不烧重试


def test_cross_check_clean_on_sample():
    data = valid_quote()
    result = cross_check(load_ir(), data)
    assert result["item_conflicts"] == 0
    assert result["total_conflicts"] == []
    assert all("_cross_check" not in i for i in data["unit_price"]["processing"]["items"])


def test_cross_check_conflict_written_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    data = valid_quote(category=None)  # 本测试未灌主数据，category 有 FK 约束
    tampered = data["unit_price"]["processing"]["items"][0]
    tampered["amount_per_pc"] = 9.99  # 原文行金额为 2.0

    result = cross_check(load_ir(), data)
    assert result["item_conflicts"] == 1
    assert tampered["_cross_check"] == {"script_value": 2.0, "llm_value": 9.99}

    out = persist_quote(data, project_name="交叉验证测试")
    assert "cross_validation_conflict" in out["flags"]

    conn = get_connection()
    row = conn.execute(
        "SELECT cross_check FROM quote_line WHERE module='processing' AND item_name='CNC加工'",
        (),
    ).fetchone()
    conn.close()
    assert json.loads(row["cross_check"]) == {"script_value": 2.0, "llm_value": 9.99}


# ---------------------------------------------------------------------------
# pipeline 编排：LLM 优先；LLM 故障中止任务（不降级）
# ---------------------------------------------------------------------------

@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """隔离目录 + 灌主数据，返回 (conn, tmp_path)。"""
    import app.ingest as ingest_module
    import app.pipeline.pipeline as pipeline_module

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
    return get_connection(), tmp_path


def _upload_and_run(conn, tmp_path):
    from app.pipeline.pipeline import create_task, run_task

    task_id = create_task(conn, "LLM编排测试", [("sample_quote.xlsx", FIXTURE.read_bytes())])
    return run_task(task_id, conn)


def test_pipeline_llm_unavailable_aborts_task(prepared):
    conn, tmp_path = prepared
    summary = _upload_and_run(conn, tmp_path)  # conftest 禁真实网关 → 任务中止
    assert summary["task_status"] == "failed"
    assert summary["results"][0]["status"] == "failed"
    assert "LLM" in summary["error"]

    task = conn.execute("SELECT status FROM comparison_task WHERE id = ?", (summary["task_id"],)).fetchone()
    assert task["status"] == "failed"

    quote = conn.execute("SELECT parse_status, basic_info FROM quote").fetchone()
    assert quote["parse_status"] == "failed"
    assert "LLM" in json.loads(quote["basic_info"])["error"]

    actions = [r[0] for r in conn.execute("SELECT action FROM parse_log WHERE stage='layout'")]
    assert "llm_error" in actions
    assert "parsed" not in actions  # 未降级到规则解析
    conn.close()


def test_pipeline_llm_success_path(prepared, monkeypatch):
    conn, tmp_path = prepared
    from app.llm import client as llm_client

    fake = FakeChat(valid_quote("CAT-WJWK"))
    monkeypatch.setattr(llm_client, "chat_json", fake)
    summary = _upload_and_run(conn, tmp_path)
    assert summary["results"][0]["status"] == "parsed"

    logs = {
        r["action"]: json.loads(r["detail"])
        for r in conn.execute("SELECT action, detail FROM parse_log WHERE stage='layout'")
    }
    assert logs["parsed"]["engine"] == "llm"
    assert logs["cross_check"]["item_conflicts"] == 0

    quote = conn.execute("SELECT category_code, calc_check, flags FROM quote").fetchone()
    assert quote["category_code"] == "CAT-WJWK"  # LLM 品类判定落库
    assert quote["calc_check"] == "pass"
    assert "cross_validation_conflict" not in json.loads(quote["flags"])
    conn.close()

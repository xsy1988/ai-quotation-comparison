"""LLM-B 核算复核（影子模式）：run_verify_shadow 结论解析与降级、只读保证、
创锋金标对账、parse_log 日志结构与 pipeline 集成开关（LLM_B_VERIFY）。

金标：惠州市创锋科技双 offer（envelope_chuangfeng_two_offers.json + ir_chuangfeng.json），
derive_offer 修正后税费按 0.13×未税 派生（offer1 未税 13.93 → 税 1.81）。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.pipeline.pipeline as pipeline_module
from app.db import get_connection, init_db
from app.derive import derive_offer
from app.llm.client import LLMError, LLMUnavailable
from app.pipeline.verify_runner import run_verify_shadow

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).parent / "fixtures"
CHUANGFENG_ENVELOPE = FIXTURE_DIR / "envelope_chuangfeng_two_offers.json"
CHUANGFENG_IR = FIXTURE_DIR / "ir_chuangfeng.json"
SAMPLE_XLSX = FIXTURE_DIR / "sample_quote.xlsx"


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
        return resp, {"model": "fake-b", "total_tokens": 50, "elapsed_ms": 7}


def _minimal_offer() -> dict:
    return json.loads(CHUANGFENG_ENVELOPE.read_text(encoding="utf-8"))["offers"][0]


def _ir() -> dict:
    return json.loads(CHUANGFENG_IR.read_text(encoding="utf-8"))


def _derived_offer(index: int = 0) -> tuple[dict, dict]:
    """创锋 fixture：derive_offer(offer, ir) 修正后的 offer + IR。"""
    offer = json.loads(CHUANGFENG_ENVELOPE.read_text(encoding="utf-8"))["offers"][index]
    ir = _ir()
    derive_offer(offer, ir=ir)
    return offer, ir


# ---------------------------------------------------------------------------
# run_verify_shadow：结论解析、缺字段宽容、异常降级 skipped
# ---------------------------------------------------------------------------

def test_verify_pass_verdict():
    fake = FakeChat({"verdict": "pass", "checks": [
        {"target": "unit_price.summary.taxed_total", "ok": True,
         "expected": 15.74, "actual": 15.74, "formula": "13.93+1.81=15.74",
         "source": "核对一致", "comment": ""},
    ], "suspects": []})
    result = run_verify_shadow(_minimal_offer(), _ir(), chat_fn=fake)
    assert result["verdict"] == "pass"
    assert result["checks"][0]["formula"] == "13.93+1.81=15.74"
    assert result["suspects"] == []
    assert result["usage"]["model"] == "fake-b"
    assert result["prompt_version"] == "v1.0.0"
    # prompt 组装：offer JSON 与 IR 序列化都进了 user 消息，且带 system 角色短句
    assert fake.calls[0][0]["role"] == "system"
    user = fake.calls[0][-1]["content"]
    assert "报价核算复核员" in user
    assert "不良率损耗" in user  # offer 序列化
    assert "page_1|4|" in user  # IR 序列化（sheet|行号|列:值）


def test_verify_issues_verdict_with_formula():
    fake = FakeChat({"verdict": "issues", "checks": [
        {"target": "unit_price.sga_tax.items[3].amount_per_pc", "ok": False,
         "expected": 1.81, "actual": 2.01, "formula": "13.93×0.13=1.81",
         "source": "派生", "comment": "税费 2.01 系抄袭相邻『不良率损耗』单元格"},
    ], "suspects": [
        {"path": "unit_price.sga_tax.items[3]", "reason": "raw_text 只有税率 13%，金额抄相邻单元格"},
    ]})
    result = run_verify_shadow(_minimal_offer(), _ir(), chat_fn=fake)
    assert result["verdict"] == "issues"
    check = result["checks"][0]
    assert check["ok"] is False and check["formula"] == "13.93×0.13=1.81"
    assert result["suspects"][0]["path"] == "unit_price.sga_tax.items[3]"


def test_verify_missing_fields_tolerated():
    """checks/suspects 缺失宽容为 []；数组里的非 dict 条目丢弃。"""
    fake = FakeChat({"verdict": "issues"})
    result = run_verify_shadow(_minimal_offer(), _ir(), chat_fn=fake)
    assert result["verdict"] == "issues"
    assert result["checks"] == [] and result["suspects"] == []

    fake = FakeChat({"verdict": "pass", "checks": ["not-a-dict", {"ok": True}]})
    result = run_verify_shadow(_minimal_offer(), _ir(), chat_fn=fake)
    assert result["verdict"] == "pass"
    assert result["checks"] == [{"ok": True}]


def test_verify_bad_verdict_skipped_with_reason():
    for bad in ({"verdict": "unknown"}, {"no_verdict": True}, ["not-a-dict"],
                {"verdict": "pass", "checks": "not-a-list"}):
        result = run_verify_shadow(_minimal_offer(), _ir(), chat_fn=FakeChat(bad))
        assert result["verdict"] == "skipped"
        assert result["reason"]


def test_verify_llm_error_and_unavailable_skipped_with_reason():
    for exc in (LLMError("输出不是合法 JSON"), LLMUnavailable("网关挂了")):
        result = run_verify_shadow(_minimal_offer(), _ir(), chat_fn=FakeChat(exc))
        assert result["verdict"] == "skipped"
        assert "网关挂了" in result["reason"] or "输出不是合法 JSON" in result["reason"]


def test_verify_never_raises_on_unexpected_error():
    def _boom(messages, **kwargs):
        raise RuntimeError("完全没预料到的错")

    result = run_verify_shadow(_minimal_offer(), _ir(), chat_fn=_boom)
    assert result["verdict"] == "skipped"
    assert "RuntimeError" in result["reason"]


# ---------------------------------------------------------------------------
# 创锋金标：derive 修正后复核——只读不改 offer
# ---------------------------------------------------------------------------

def test_chuangfeng_shadow_verify_readonly():
    """影子模式幂等只读：复核前后 offer 逐字节一致；mock B 报税费抄袭 issues。"""
    offer, ir = _derived_offer(0)
    before = json.dumps(offer, sort_keys=True, ensure_ascii=False)
    assert offer["unit_price"]["sga_tax"]["items"][3]["amount_per_pc"] == pytest.approx(1.81, abs=0.01)

    llm_b = {"verdict": "issues", "checks": [
        {"target": "unit_price.sga_tax.items[3].amount_per_pc", "ok": False,
         "expected": 1.81, "actual": 2.01, "formula": "13.93×0.13=1.81",
         "source": "派生",
         "comment": "税费 2.01 系相邻单元格抄袭，应为 null 由脚本按税率×未税派生 1.81"},
    ], "suspects": [
        {"path": "unit_price.sga_tax.items[3].amount_per_pc",
         "reason": "raw_text 只有税率 13%，无税额出处"},
    ]}
    result = run_verify_shadow(offer, ir, chat_fn=FakeChat(llm_b))
    assert result["verdict"] == "issues"
    assert result["checks"][0]["actual"] == 2.01
    assert result["checks"][0]["expected"] == pytest.approx(1.81)
    # 复核不改任何数值
    assert json.dumps(offer, sort_keys=True, ensure_ascii=False) == before


def test_chuangfeng_shadow_log_detail_structure():
    """parse_log 日志结构：stage=verify/action=shadow，含 blame_b_issues 与 llm_b 并排。"""
    init_db()
    conn = get_connection()
    with conn:
        cur = conn.execute(
            "INSERT INTO comparison_task (project_name, status) VALUES ('影子测试', 'parsing')"
        )
    task_id = cur.lastrowid

    offer, ir = _derived_offer(0)
    fake = FakeChat({"verdict": "issues", "checks": [
        {"target": "unit_price.sga_tax.items[3].amount_per_pc", "ok": False,
         "expected": 1.81, "actual": 2.01, "formula": "13.93×0.13=1.81",
         "source": "派生", "comment": "税费 2.01 系相邻单元格抄袭，应为 null 派生 1.81"},
    ], "suspects": []})
    pipeline_module._run_verify_shadow_all(conn, task_id, [offer], ir, chat_fn=fake)

    rows = conn.execute(
        "SELECT stage, action, detail FROM parse_log WHERE task_id = ? AND stage = 'verify'",
        (task_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["action"] == "shadow"
    detail = json.loads(rows[0]["detail"])
    assert detail["quote_offer_index"] == 0
    assert isinstance(detail["blame_b_issues"], list)
    assert all(i["blame"] == "B" for i in detail["blame_b_issues"])
    assert detail["llm_b"]["verdict"] == "issues"
    assert detail["llm_b"]["checks"][0]["formula"] == "13.93×0.13=1.81"
    assert detail["model"] == "fake-b"
    assert detail["elapsed_ms"] == 7
    conn.close()


# ---------------------------------------------------------------------------
# pipeline 集成：LLM_B_VERIFY 开关
# ---------------------------------------------------------------------------

@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """隔离目录 + 灌主数据，返回 (conn, tmp_path)。"""
    import app.ingest as ingest_module
    import app.persist as persist_module

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


def _mock_llm_layout(monkeypatch, verify_response):
    """chat_json 替身：第 1 次调用（版面理解）返回规则解析结果，第 2 次（LLM-B 复核）
    返回 verify_response；与 test_pipeline/test_llm_layout 的模式一致。"""
    from app.ingest import excel_to_ir, sha256_of
    from app.llm import client as llm_client
    from app.pipeline.simple_excel_parse import parse_ir as rule_parse_ir

    fake = FakeChat(verify_response)  # 复核用

    def _fake_chat(messages, **kwargs):
        data = rule_parse_ir(excel_to_ir(SAMPLE_XLSX, sha256_of(SAMPLE_XLSX)))
        data["basic"]["category"] = "CAT-WJWK"
        return {"offers": [data]}, {"model": "fake", "total_tokens": 10, "elapsed_ms": 1}

    def _chat(messages, **kwargs):
        # 版面理解的 prompt 含 offers/schema 契约；复核 prompt 含"核算复核员"
        if "核算复核员" in messages[0]["content"] or "核算复核员" in messages[-1]["content"]:
            return fake(messages, **kwargs)
        return _fake_chat(messages, **kwargs)

    monkeypatch.setattr(llm_client, "chat_json", _chat)


def _upload_and_run(conn):
    task_id = pipeline_module.create_task(
        conn, "影子集成测试", [("sample_quote.xlsx", SAMPLE_XLSX.read_bytes())]
    )
    return pipeline_module.run_task(task_id, conn)


def test_pipeline_verify_shadow_enabled(prepared, monkeypatch):
    """LLM_B_VERIFY=1（默认）：parse_log 有 verify/shadow 记录，llm_b 结论入档。"""
    conn, tmp_path = prepared
    monkeypatch.delenv("LLM_B_VERIFY", raising=False)
    _mock_llm_layout(monkeypatch, {"verdict": "pass", "checks": [], "suspects": []})
    summary = _upload_and_run(conn)
    assert summary["results"][0]["status"] == "parsed"

    rows = conn.execute(
        "SELECT action, detail FROM parse_log WHERE stage = 'verify' AND action = 'shadow'"
    ).fetchall()
    # 新增的 verify/started 进度事件另占一行，shadow 判例仍只此一条
    assert len(rows) == 1 and rows[0]["action"] == "shadow"
    detail = json.loads(rows[0]["detail"])
    assert detail["quote_offer_index"] == 0
    assert detail["llm_b"]["verdict"] == "pass"
    assert detail["model"] == "fake-b"
    assert detail["elapsed_ms"] == 7
    conn.close()


def test_pipeline_verify_shadow_disabled(prepared, monkeypatch):
    """LLM_B_VERIFY=0：跳过复核，parse_log 无 verify 记录。"""
    conn, tmp_path = prepared
    monkeypatch.setenv("LLM_B_VERIFY", "0")
    _mock_llm_layout(monkeypatch, {"verdict": "pass", "checks": [], "suspects": []})
    summary = _upload_and_run(conn)
    assert summary["results"][0]["status"] == "parsed"

    count = conn.execute("SELECT COUNT(*) FROM parse_log WHERE stage = 'verify'").fetchone()[0]
    assert count == 0
    conn.close()

"""LLM 版面理解：正常解析+品类判定、schema 重试、持续失败中止任务、cross_check 对账与落库。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.db import get_connection, init_db
from app.ingest import excel_to_ir, sha256_of
from app.ir import CellValue
from app.llm.client import LLMError, LLMUnavailable
import app.persist as persist_module
import app.pipeline.layout_understand as layout_module
from app.persist import persist_quote
from app.pipeline.layout_understand import (
    LLMValidateError,
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


def valid_offer(category="CAT-WJWK") -> dict:
    data = rule_parse_ir(load_ir())
    data["basic"]["category"] = category
    return data


def envelope(*offers) -> dict:
    return {"offers": list(offers)}


def valid_quote(category="CAT-WJWK") -> dict:
    """信封格式的合法 LLM 输出。"""
    return envelope(valid_offer(category))


def invalid_quote(bad_currency="RMB") -> dict:
    """currency 违反枚举，必触发 Draft7 校验错误。"""
    data = valid_offer()
    data["basic"]["currency"] = bad_currency
    return envelope(data)


def test_parse_llm_normal_and_category():
    fake = FakeChat(valid_quote())
    data, attempts, cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    validate_quote(data["offers"][0])  # LLM 输出本身过 Draft7（单 offer）
    assert data["offers"][0]["basic"]["category"] == "CAT-WJWK"
    assert attempts[0]["validation_errors"] is None
    assert cross["item_conflicts"] == 0
    # 品类清单进了 prompt
    prompt = fake.calls[0][-1]["content"]
    for code in ("CAT-WJWK", "CAT-PCBA", "CAT-BC"):
        assert code in prompt
    # 信封结构与 processing 映射阶段字段的规约
    assert '"offers"' in prompt and '"low"' in prompt and "match_path" in prompt


def test_parse_llm_schema_retry_once_then_success():
    fake = FakeChat(invalid_quote(), valid_quote())
    data, attempts, _cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 2
    assert attempts[0]["validation_errors"]  # 第一轮有校验错误
    assert attempts[1]["validation_errors"] is None
    # 第二轮对话里带上了校验错误反馈
    retry_text = fake.calls[1][-1]["content"]
    assert "currency" in retry_text
    assert data["offers"][0]["basic"]["currency"] == "CNY"


def test_self_check_archived_to_derived():
    """带 _self_check 的 LLM 输出：schema 校验通过，_self_check 摘下入档 _derived.llm_self_check。"""
    quote = valid_quote()
    self_check = {
        "amounts_traceable": True,
        "no_invented_values": True,
        "null_fields": ["unit_price.sga_tax.items[3].amount_per_pc"],
        "uncertain_cells": [{"location": "page_1!R4C22", "reason": "税率13%只有费率，税额缺失"}],
    }
    quote["offers"][0]["_self_check"] = self_check
    fake = FakeChat(quote)
    data, attempts, _cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    offer = data["offers"][0]
    assert "_self_check" not in offer  # 已从 offer 顶层摘除，不进 schema 校验
    assert offer["_derived"]["llm_self_check"] == self_check
    validate_quote(offer)  # schema 校验不受 _derived 附加字段影响
    assert attempts[0]["prompt_version"]  # attempts 带 prompt 版本号
    assert "v" in attempts[0]["prompt_version"]
    # prompt 契约要求 LLM 输出 _self_check
    assert "_self_check" in fake.calls[0][-1]["content"]


def test_self_check_absent_archived_null():
    """LLM 未输出 _self_check 不报错，存档 null。"""
    fake = FakeChat(valid_quote())
    data, attempts, _cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    assert data["offers"][0]["_derived"]["llm_self_check"] is None
    assert attempts[0]["prompt_version"]


def test_parse_llm_persistent_invalid_raises_unavailable():
    # 3 轮各不相同（避免触发"输出与上一轮一致"的提前终止），但都 schema 非法
    fake = FakeChat(invalid_quote("RMB"), invalid_quote("RMBX"), invalid_quote("RMBXX"))
    with pytest.raises(LLMUnavailable) as exc_info:
        parse_ir_with_llm(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 3  # 上限 3 轮（含首轮）
    attempts = exc_info.value.attempts
    assert len(attempts) == 3
    assert attempts[2]["validation_errors"]
    assert all(a["kind"] == "schema_invalid" for a in attempts)  # 每轮失败类型入档
    assert "各轮情况" in str(exc_info.value)  # 异常消息带逐轮摘要，便于排查


def test_parse_llm_gateway_unavailable_no_retry():
    fake = FakeChat(LLMUnavailable("网关挂了"))
    with pytest.raises(LLMUnavailable):
        parse_ir_with_llm(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 1  # 网关类错误立即上抛，不烧重试


# ---------------------------------------------------------------------------
# 3 轮重试闭环：L1 溯源反馈（traceability）、L2 只记录不打断
# ---------------------------------------------------------------------------

def _hallucinated_amount_quote(amount: float = 9.99) -> dict:
    """金额被改成 raw_text 中不存在的值（schema 合法的内容幻觉）。"""
    quote = valid_quote()
    quote["offers"][0]["unit_price"]["processing"]["items"][0]["amount_per_pc"] = amount  # 原文 'CNC加工 2'
    return quote


def test_parse_llm_l1_hallucination_retry_then_success():
    """第 1 轮幻觉金额 → L1 报 amount_not_in_evidence 并按 traceability 反馈 → 第 2 轮修正成功。"""
    fake = FakeChat(_hallucinated_amount_quote(), valid_quote())
    data, attempts, _cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 2
    assert attempts[0]["l1_issues"]  # 第一轮 L1 问题入档
    assert attempts[0]["l1_issues"][0]["issue"] == "amount_not_in_evidence"
    assert attempts[1]["l1_issues"] is None
    # 第二轮对话带上了 traceability 反馈（按 path 逐条列出问题）
    retry_text = fake.calls[1][-1]["content"]
    assert "amount_not_in_evidence" in retry_text
    assert "_self_check.null_fields" in retry_text
    assert "9.99" in retry_text
    assert data["offers"][0]["unit_price"]["processing"]["items"][0]["amount_per_pc"] == 2.0


def test_parse_llm_l1_persistent_hallucination_raises_after_3_rounds():
    """3 轮改成各不相同的幻觉金额 → 用满 3 轮，attempts 长度 3，异常带最终问题清单。"""
    fake = FakeChat(
        _hallucinated_amount_quote(9.99),
        _hallucinated_amount_quote(8.88),
        _hallucinated_amount_quote(7.77),
    )
    with pytest.raises(LLMValidateError) as exc_info:
        parse_ir_with_llm(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 3
    attempts = exc_info.value.attempts
    assert len(attempts) == 3
    assert all(a["l1_issues"] for a in attempts)  # 每轮 L1 都报幻觉
    assert all(a["kind"] == "traceability" for a in attempts)
    assert "7.77" in str(exc_info.value)  # 异常消息带最终问题清单


def test_parse_llm_identical_output_fails_fast():
    """连续两轮输出完全一致（模型对 retry 反馈零响应）→ 提前终止，不再空烧第 3 轮。"""
    fake = FakeChat(_hallucinated_amount_quote(), _hallucinated_amount_quote())
    with pytest.raises(LLMValidateError) as exc_info:
        parse_ir_with_llm(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 2  # 第 3 轮被跳过
    assert len(exc_info.value.attempts) == 2
    assert "无进展" in str(exc_info.value)  # 终止原因写进异常消息


def test_l1_issues_recorded_even_when_schema_invalid():
    """L0 报错时 L1 仍要算并入档：schema 错误会掩盖编造金额这一真正的首因。"""
    quote = _hallucinated_amount_quote()
    quote["offers"][0]["basic"]["currency"] = "RMB"  # 同时 schema 非法
    fake = FakeChat(quote, valid_quote())
    _data, attempts, _cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    assert attempts[0]["validation_errors"]  # L0 问题仍在
    assert attempts[0]["l1_issues"][0]["issue"] == "amount_not_in_evidence"  # L1 也入档
    assert attempts[0]["kind"] == "schema_invalid"  # 路由优先级不变
    assert "currency" in fake.calls[1][-1]["content"]


def test_llm_trace_archived_per_round(tmp_path, monkeypatch):
    """每轮原始输出 + 校验结果落盘 llm_trace/{file_hash}_r{n}.json，失败轮也留痕。"""
    monkeypatch.setattr(layout_module, "LLM_TRACE_DIR", tmp_path / "trace")
    fake = FakeChat(_hallucinated_amount_quote(), valid_quote())
    parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    files = sorted(p.name for p in (tmp_path / "trace").iterdir())
    assert len(files) == 2  # 失败轮与成功轮都落盘
    first = json.loads((tmp_path / "trace" / files[0]).read_text(encoding="utf-8"))
    assert first["attempt"]["round"] == 1
    assert first["attempt"]["l1_issues"][0]["issue"] == "amount_not_in_evidence"
    # 落盘的是模型原始输出（未剥离 _derived 的解析结果）
    assert first["output"]["offers"][0]["unit_price"]["processing"]["items"][0]["amount_per_pc"] == 9.99
    # LLM_TRACE=0 时完全不写盘
    monkeypatch.setenv("LLM_TRACE", "0")
    parse_ir_with_llm(load_ir(), chat_fn=FakeChat(valid_quote()))
    assert len(list((tmp_path / "trace").iterdir())) == 2


def test_parse_llm_json_unparseable_retries_up_to_3():
    """JSON 解析失败路径保持原有行为，上限变为 3 轮。"""
    fake = FakeChat(LLMError("bad json"), LLMError("bad json"), LLMError("bad json"))
    with pytest.raises(LLMUnavailable) as exc_info:
        parse_ir_with_llm(load_ir(), chat_fn=fake)
    assert len(fake.calls) == 3
    assert len(exc_info.value.attempts) == 3
    assert all(a["validation_errors"] == ["bad json"] for a in exc_info.value.attempts)
    # 每轮反馈均为 json_unparseable 模板
    for call in fake.calls[1:]:
        assert "上次输出无法解析" in call[-1]["content"]


def test_parse_llm_l2_issues_recorded_not_blocking():
    """summary 不闭合属勾稽问题（blame=B）：只写入 attempts 末条 l2_issues，不打断返回。"""
    quote = valid_quote()
    quote["offers"][0]["unit_price"]["summary"]["untaxed_total"] = 999.0  # 与重算值 10.0 不符
    fake = FakeChat(quote)
    data, attempts, _cross = parse_ir_with_llm_traced(load_ir(), chat_fn=fake)
    assert data["offers"][0]["unit_price"]["summary"]["untaxed_total"] == 999.0
    assert len(attempts) == 1  # 一轮通过，L2 不触发重试
    l2_issues = attempts[-1]["l2_issues"]
    assert l2_issues
    assert any(i["issue"] == "summary_not_closed" for i in l2_issues)
    assert all(i["blame"] == "B" for i in l2_issues)
    assert attempts[-1]["validation_errors"] is None
    assert attempts[-1]["l1_issues"] is None


def test_cross_check_clean_on_sample():
    data = valid_offer()  # 旧格式单 quote：兼容路径
    result = cross_check(load_ir(), data)
    assert result["item_conflicts"] == 0
    assert result["total_conflicts"] == []
    assert all("_cross_check" not in i for i in data["unit_price"]["processing"]["items"])


def test_cross_check_envelope_multi_offers():
    """信封多 offer：条目级对账逐 offer 覆盖，模块合计按各 offer 行 scope 对账不误报。"""
    offer_a = valid_offer()
    offer_b = valid_offer()
    offer_b["basic"]["scheme"] = "方案2"
    data = envelope(offer_a, offer_b)
    result = cross_check(load_ir(), data)
    assert result["item_conflicts"] == 0
    assert result["total_conflicts"] == []


def test_cross_check_conflict_written_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    data = valid_offer(category=None)  # 本测试未灌主数据，category 有 FK 约束
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
# pipeline 编排：LLM 优先；LLM 故障按单文件失败隔离（不再中止任务）
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


def test_pipeline_llm_unavailable_marks_file_failed(prepared):
    """LLM 故障改为单文件级隔离：唯一文件失败 → 该文件 failed、任务 failed（全败才 failed）。"""
    conn, tmp_path = prepared
    summary = _upload_and_run(conn, tmp_path)  # conftest 禁真实网关 → 该文件标失败
    assert summary["task_status"] == "failed"
    assert summary["results"][0]["status"] == "failed"

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
    # 样例报价单税额 0.51 与 13%×未税(1.30) 不符：单据值保留，derive 打标（新规则，单据优先不改值）
    assert json.loads(quote["flags"]) == ["cross_validation_conflict"]
    conn.close()


def test_cross_check_tolerates_separator_only_cell():
    """IR 里的纯分隔符噪声单元格（如 "，"）不能被当成金额：曾抛
    ValueError: could not convert string to float: '' 让整个文件解析失败。"""
    ir = load_ir()
    ir.tables[0].cells.append(CellValue(row=0, col=99, value="，"))
    ir.tables[0].cells.append(CellValue(row=0, col=98, value="￥"))
    cross = cross_check(ir, valid_quote())  # 不再抛异常
    assert cross["item_conflicts"] == 0


def test_parse_llm_succeeds_when_diagnostics_crash(monkeypatch):
    """后置诊断（脚本侧对账 / L2 勾稽）异常只记录、不让已通过 L0+L1 的解析失败。"""
    def boom(*_args, **_kwargs):
        raise ValueError("could not convert string to float: ''")

    monkeypatch.setattr(layout_module, "cross_check", boom)
    data, attempts, cross = parse_ir_with_llm_traced(
        load_ir(), chat_fn=FakeChat(valid_quote("CAT-WJWK"))
    )
    assert data["offers"]  # 解析结果照常返回
    assert "could not convert" in attempts[-1]["diagnostic_error"]
    assert "could not convert" in cross["diagnostic_error"]


def test_parse_llm_records_diagnostic_error_for_l2(monkeypatch):
    monkeypatch.setattr(
        layout_module, "validate_l2_reconcile",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("l2 boom")),
    )
    _data, attempts, cross = parse_ir_with_llm_traced(
        load_ir(), chat_fn=FakeChat(valid_quote("CAT-WJWK"))
    )
    assert attempts[-1]["diagnostic_error"] == "RuntimeError: l2 boom"
    assert cross["diagnostic_error"] == "RuntimeError: l2 boom"

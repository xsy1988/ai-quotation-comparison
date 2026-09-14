"""AI 分析：结构化输出归一化、数字回检、结构重生成、输入指纹与自动触发、LLM 故障 502。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.compare.ai_analysis import (
    ADVANTAGE_DIMS,
    RISK_DIMS,
    WEAKNESS_DIMS,
    build_input,
    candidates,
    extract_amounts,
    get_ai_analysis,
    input_signature,
    run_ai_analysis,
)
from app.compare.compare_engine import get_comparison
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
            "INSERT INTO comparison_task (project_name, status) VALUES ('AI分析测试', 'parsed')"
        )
        task_id = cur.lastrowid
    persist_quote(make_quote("供应商A", True, True), task_id=task_id, file_hash="ha")
    persist_quote(make_quote("供应商B", False, False), task_id=task_id, file_hash="hb")
    yield conn, task_id
    conn.close()


# 供应商A：全量 + 折扣 → 最终含税 10.61；供应商B：缺检验 → 最终含税 8.5
_A_QUOTE_ID = 1


def _item(quote_id: int, advantage: str, weakness: str, risk: str, suggestion: str) -> dict:
    def block(dims, seed):
        return {d: f"{seed}{d}" for d in dims}

    return {
        "quote_id": quote_id,
        "advantage": advantage,
        "weakness": weakness,
        "risk": risk,
        "suggestion": suggestion,
        "advantage_detail": block(ADVANTAGE_DIMS, "优"),
        "weakness_detail": block(WEAKNESS_DIMS, "劣"),
        "risk_detail": block(RISK_DIMS, "险"),
    }


def _good_payload(quote_ids: tuple[int, int]) -> dict:
    """数字全部来自机械结果候选集（10.61/8.5/1.06/4.6/15000/0.5）。"""
    a, b = quote_ids
    return {
        "overall": "供应商B 价格低 2.11 元，供应商A 加工费 4.6 元偏高，先谈价再定点。",
        "suppliers": [
            _item(a, "含税 10.61 元最高", "加工费 4.6 元偏高", "折后 0.5 元需复核", "按 8.5 元对标议价"),
            _item(b, "含税 8.5 元最低", "缺检验费 0.3 元", "模具 15000 元一次性", "主供候选"),
        ],
    }


def _fake_chat(payloads):
    """按序返回 JSON 的 chat_json 替身，记录调用次数。"""
    calls = []

    def _chat(messages, **kwargs):
        calls.append(list(messages))
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        return payload, {"model": "fake", "total_tokens": 50, "elapsed_ms": 3}

    _chat.calls = calls
    return _chat


def test_extract_amounts_skips_units_and_headings():
    """数字回检：计量单位小数与 markdown 标题/表格分隔行不算金额。"""
    md = "## 2.1 费用模块对比\n| 列A | 列B |\n| :--- | ---: |\n材料0.045kg；阳极0.5mm；保温1.5h；费1.25 元；¥999.99\n"
    assert extract_amounts(md) == [1.25, 999.99]


def test_extract_amounts_skips_units_followed_by_chinese():
    """单位后直接接中文（2.8mm壁厚 / 0.045kg损耗）同样按尺寸处理，不算金额——
    否则模型照抄原文的规格数字会被误判为编造金额，白白多跑一轮重生成。"""
    assert extract_amounts("表面处理仅标注氧化，2.8mm壁厚对五金外壳偏厚") == []
    assert extract_amounts("材料0.045kg损耗6%，管理费1.89") == [1.89]
    # 金额仍要能取到：元/¥ 形式与无单位的小数
    assert extract_amounts("损管利税9.5453最高；CNC1 4.5；单价24.63") == [9.5453, 4.5, 24.63]


def test_build_input_structured_only(task):
    """喂给 LLM 的输入只来自结构化数据：不含文件名/路径/原始文本。"""
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))

    assert payload["unit"] == "元/pcs"
    # 顺序与「报价对比」表的供应商列一致（报价单 id 升序）：A(10.61) 在前，B(8.5) 在后；
    # 价格高低只由 price_rank/is_lowest 表达，不改变列顺序
    assert [s["quote_id"] for s in payload["suppliers"]] == [1, 2]
    assert [s["supplier_name"] for s in payload["suppliers"]] == ["供应商A", "供应商B"]
    assert payload["suppliers"][0]["price_rank"] == 2
    assert payload["suppliers"][0]["is_lowest"] is False
    assert payload["suppliers"][0]["final_unit_price_taxed"] == 10.61
    assert payload["suppliers"][1]["is_lowest"] is True
    assert payload["suppliers"][1]["price_rank"] == 1
    # 六模块合计 + 明细 + 抽屉 + 模治具
    assert payload["suppliers"][0]["modules"]["processing"] == 4.6
    assert any(row["label"] == "未税合计" for row in payload["summary_rows"])
    assert [line[1] for line in payload["lines"]]
    assert payload["drawers"] and all("drawer_name" in d for d in payload["drawers"])
    assert payload["tooling"]
    text = json.dumps(payload, ensure_ascii=False)
    assert ".xlsx" not in text and "raw_json_path" not in text


def test_run_pass_and_reuse(task):
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    chat = _fake_chat([good])
    analysis = run_ai_analysis(conn, task_id, chat_fn=chat)

    assert analysis["status"] == "completed"
    assert analysis["check_status"] == "pass"
    assert analysis["check_detail"]["suspicious"] == []
    assert analysis["check_detail"]["missing_quote_ids"] == []
    assert analysis["content"]["unit"] == "元/pcs"
    assert len(analysis["content"]["suppliers"]) == 2
    # 顺序 = 比价表格的供应商列顺序（quote id 升序），与「报价对比」模块逐列对齐
    comparison = get_comparison(conn, task_id)
    assert [s["quote_id"] for s in analysis["content"]["suppliers"]] == [
        s["quote_id"] for s in comparison["suppliers"]
    ]
    # 名称/价格/排名以机械结果为准，不由 LLM 决定
    names = [s["name"] for s in analysis["content"]["suppliers"]]
    assert names == ["供应商A", "供应商B"]
    assert analysis["content"]["suppliers"][0]["final_unit_price_taxed"] == 10.61
    assert analysis["content"]["suppliers"][1]["final_unit_price_taxed"] == 8.5
    assert list(analysis["content"]["dimensions"]["advantage"]) == list(ADVANTAGE_DIMS)

    logs = conn.execute(
        "SELECT action, is_llm_call, detail FROM parse_log"
        " WHERE task_id=? AND stage='ai_analysis' ORDER BY id",
        (task_id,),
    ).fetchall()
    assert [l["action"] for l in logs] == ["generated", "numeric_check"]
    assert logs[0]["is_llm_call"] == 1
    assert json.loads(logs[0]["detail"])["tokens"] == 50
    assert json.loads(logs[1]["detail"])["check_status"] == "pass"

    # 同指纹已完成 → 直接复用，不再调 LLM
    again = run_ai_analysis(conn, task_id, chat_fn=chat)
    assert again["id"] == analysis["id"]
    assert len(chat.calls) == 1


def test_fabricated_amount_regenerates_then_mismatch(task):
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    bad = json.loads(json.dumps(good))
    bad["suppliers"][0]["advantage"] = "含税 999.99 元，极具优势"
    bad["suppliers"][0]["advantage_detail"][ADVANTAGE_DIMS[0]] = "损耗 7.77 元"

    chat = _fake_chat([bad, bad])  # 重生成仍编造
    analysis = run_ai_analysis(conn, task_id, chat_fn=chat)

    assert len(chat.calls) == 2
    assert analysis["check_status"] == "mismatch"
    assert [s["value"] for s in analysis["check_detail"]["suspicious"]] == [999.99, 7.77]
    assert analysis["check_detail"]["rounds"] == 2
    # 反馈消息带上了编造值与重试指令
    assert "999.99" in chat.calls[1][-1]["content"]
    assert "数字回检不通过" in chat.calls[1][-1]["content"]

    logs = conn.execute(
        "SELECT action FROM parse_log WHERE task_id=? AND stage='ai_analysis' ORDER BY id",
        (task_id,),
    ).fetchall()
    assert [l["action"] for l in logs] == ["generated", "regenerated", "numeric_check"]


def test_regenerated_amounts_pass(task):
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    bad = json.loads(json.dumps(good))
    bad["suppliers"][0]["advantage"] = "含税 999.99 元，极具优势"

    chat = _fake_chat([bad, good])
    analysis = run_ai_analysis(conn, task_id, chat_fn=chat)

    assert len(chat.calls) == 2
    assert analysis["check_status"] == "pass"
    assert analysis["check_detail"]["rounds"] == 2


def test_candidates_cover_every_line_amount(task):
    """数字回检候选集必须含逐条明细金额：结论引用"CNC1 4.5""管理费 1.89"是原始数据，不是编造。"""
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    values = candidates(payload)

    amounts = [line[4] for line in payload["lines"] if line[4] is not None]
    assert amounts
    for amount in amounts:
        assert any(abs(amount - c) <= 0.01 for c in values), amount


def test_line_amount_in_conclusion_is_not_suspicious(task):
    """逐条明细金额写进结论 → 不回检失败、不重生成。"""
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    line_amount = next(line[4] for line in payload["lines"] if line[4] is not None)
    good["suppliers"][0]["advantage_detail"][ADVANTAGE_DIMS[1]] = f"单条工序费 {line_amount} 最低"

    chat = _fake_chat([good])
    analysis = run_ai_analysis(conn, task_id, chat_fn=chat)

    assert len(chat.calls) == 1
    assert analysis["check_status"] == "pass"
    assert analysis["check_detail"]["suspicious"] == []


def test_worse_regeneration_keeps_first_attempt(task):
    """重生成更差时保留首轮：不因第二轮多编造数字而丢质量。"""
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    worse = json.loads(json.dumps(good))
    worse["suppliers"][0]["advantage"] = "含税 999.99 元"
    worse["suppliers"][0]["weakness"] = "损耗 888.88 元"
    better = json.loads(json.dumps(good))
    better["suppliers"][0]["advantage"] = "含税 999.99 元"

    chat = _fake_chat([better, worse])
    analysis = run_ai_analysis(conn, task_id, chat_fn=chat)

    assert len(chat.calls) == 2
    assert [s["value"] for s in analysis["check_detail"]["suspicious"]] == [999.99]
    assert analysis["content"]["suppliers"][0]["weakness"] == good["suppliers"][0]["weakness"]


def test_over_length_reported_but_does_not_retry_alone(task):
    """超长单元格只记录不改判定：单独超长不触发重生成。"""
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    good["suppliers"][0]["suggestion"] = "确认工艺后锁价" + "再压价" * 20
    good["suppliers"][0]["advantage_detail"][ADVANTAGE_DIMS[0]] = "材料费最低" + "；工艺完整" * 12

    chat = _fake_chat([good])
    analysis = run_ai_analysis(conn, task_id, chat_fn=chat)

    assert len(chat.calls) == 1
    assert analysis["check_status"] == "pass"
    assert analysis["check_detail"]["over_length"]


def test_over_length_rides_along_with_regenerate_feedback(task):
    """因数字/结构问题重生成时，超长单元格一并反馈。"""
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    long_cell = "确认工艺后锁价" + "再压价" * 20
    bad = json.loads(json.dumps(good))
    bad["suppliers"][0]["advantage"] = "含税 999.99 元"
    bad["suppliers"][0]["suggestion"] = long_cell
    fixed = json.loads(json.dumps(good))
    fixed["suppliers"][0]["suggestion"] = long_cell

    chat = _fake_chat([bad, fixed])
    analysis = run_ai_analysis(conn, task_id, chat_fn=chat)

    assert len(chat.calls) == 2
    assert "超长单元格" in chat.calls[1][-1]["content"]
    assert analysis["check_status"] == "pass"  # 超长不进 mismatch


def test_structure_error_regenerates(task):
    """quote_id 不在输入中 → 反馈重生成，重生成合法则 pass。"""
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    bad = json.loads(json.dumps(good))
    bad["suppliers"][0]["quote_id"] = 987654

    chat = _fake_chat([bad, good])
    analysis = run_ai_analysis(conn, task_id, chat_fn=chat)
    assert analysis["check_status"] == "pass"
    assert "quote_id 不在输入中" in chat.calls[1][-1]["content"]


def test_structure_error_persists_failed_row(task):
    """结构始终不合法 → 不落半成品，落 failed 行并上抛。"""
    from app.llm.client import LLMError

    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    bad = json.loads(json.dumps(good))
    bad["suppliers"][0]["quote_id"] = 987654

    chat = _fake_chat([bad, bad])
    with pytest.raises(LLMError):
        run_ai_analysis(conn, task_id, chat_fn=chat, max_regen=1)
    row = conn.execute(
        "SELECT status, error, content FROM ai_analysis WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["content"] is None
    assert "结构不合法" in row["error"]


def test_signature_auto_run_and_new_supplier(task):
    """指纹驱动触发：无记录才自动跑；新增供应商后指纹变化 → 重新自动跑；刷新不变。"""
    conn, task_id = task
    first = get_ai_analysis(conn, task_id)
    assert first["analysis"] is None
    assert first["auto_run"] is True
    assert first["stale"] is False

    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    analysis = run_ai_analysis(conn, task_id, chat_fn=_fake_chat([good]))
    assert analysis["signature"] == payload_signature(payload)

    after = get_ai_analysis(conn, task_id)
    assert after["auto_run"] is False  # 页面刷新不会再触发
    assert after["analysis"]["id"] == analysis["id"]
    assert after["stale"] is False

    # 新增第三家供应商 → 指纹变化，需要重新分析
    persist_quote(make_quote("供应商C", True, False), task_id=task_id, file_hash="hc")
    changed = get_ai_analysis(conn, task_id)
    assert changed["auto_run"] is True
    assert changed["stale"] is True
    assert changed["analysis"] is None


def payload_signature(payload: dict) -> str:
    return input_signature(payload)


def test_llm_unavailable_marks_failed_and_api_502(task):
    conn, task_id = task
    # conftest 禁网替身：直接上抛 LLMUnavailable，不允许降级
    with pytest.raises(LLMUnavailable):
        run_ai_analysis(conn, task_id)

    row = conn.execute(
        "SELECT status, error FROM ai_analysis WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert "禁用真实 LLM" in row["error"]

    resp = client.post(f"/api/tasks/{task_id}/ai-analysis")
    assert resp.status_code == 502
    assert "禁用真实 LLM" in resp.json()["detail"]

    # 失败行可查询但不阻塞后续重试
    body = client.get(f"/api/tasks/{task_id}/ai-analysis").json()
    assert body["analysis"]["status"] == "failed"
    assert body["auto_run"] is False


def test_api_get_and_404(task):
    conn, task_id = task
    resp = client.get(f"/api/tasks/{task_id}/ai-analysis")
    assert resp.status_code == 200
    assert resp.json()["analysis"] is None

    assert client.get("/api/tasks/999999/ai-analysis").status_code == 404
    assert client.post("/api/tasks/999999/ai-analysis").status_code == 404


def test_missing_supplier_text_falls_back_to_placeholder(task):
    """LLM 漏了某家供应商 → 单元格回落占位符，并记录 missing。"""
    conn, task_id = task
    payload = build_input(conn, get_comparison(conn, task_id))
    good = _good_payload(tuple(s["quote_id"] for s in payload["suppliers"]))
    only_first = json.loads(json.dumps(good))
    only_first["suppliers"] = only_first["suppliers"][:1]

    # 输入第一家的 id（= 比价表格第一列），另一家即被 LLM 漏掉的那家
    first_qid = payload["suppliers"][0]["quote_id"]
    dropped_qid = payload["suppliers"][1]["quote_id"]

    analysis = run_ai_analysis(conn, task_id, chat_fn=_fake_chat([only_first, only_first]))
    assert analysis["status"] == "completed"
    assert analysis["check_detail"]["missing_quote_ids"] == [dropped_qid]
    missing = next(s for s in analysis["content"]["suppliers"] if s["quote_id"] == dropped_qid)
    assert missing["advantage"] == "—"
    assert missing["risk_detail"][RISK_DIMS[0]] == "—"
    assert analysis["content"]["suppliers"][0]["quote_id"] == first_qid


def test_run_without_suppliers_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "empty.db"))
    init_db()
    conn = get_connection()
    with conn:
        cur = conn.execute(
            "INSERT INTO comparison_task (project_name, status) VALUES ('空任务', 'parsed')"
        )
        task_id = cur.lastrowid
    assert get_ai_analysis(conn, task_id)["auto_run"] is False
    with pytest.raises(ValueError):
        run_ai_analysis(conn, task_id, chat_fn=_fake_chat([{}]))
    with pytest.raises(ValueError):
        run_ai_analysis(conn, 12345, chat_fn=_fake_chat([{}]))
    conn.close()


def test_build_input_carries_per_supplier_moq(task):
    """起订量随供应商走（同产品各供应商档位不同），所以放在 suppliers 里而不是 base。"""
    conn, task_id = task
    row = conn.execute("SELECT basic_info FROM quote WHERE id = ?", (_A_QUOTE_ID,)).fetchone()
    info = json.loads(row["basic_info"] or "{}") or {}
    info["moq"] = 3000
    info["moq_options"] = [
        {"condition": "皮革现货单色", "value": 3000, "note": None},
        {"condition": "定制皮革单色", "value": 40000, "note": "金属管需提供3%损耗"},
    ]
    with conn:
        conn.execute(
            "UPDATE quote SET basic_info = ? WHERE id = ?",
            (json.dumps(info, ensure_ascii=False), _A_QUOTE_ID),
        )

    payload = build_input(conn, get_comparison(conn, task_id))
    first = payload["suppliers"][0]
    assert first["moq"] == 3000
    assert first["moq_options"][1] == {
        "condition": "定制皮革单色",
        "value": 40000,
        "note": "金属管需提供3%损耗",
    }
    assert payload["suppliers"][1]["moq_options"] is None
    assert "moq" not in payload["part"]  # 供应商维度字段不再挂在零件上

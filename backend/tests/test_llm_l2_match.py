"""L2 LLM 语义匹配：唯一命中 / 编造编码兜底 / 多编码 bundle / 网关不可达上抛。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.db import get_connection, init_db
from app.llm.client import LLMUnavailable
from app.persist import persist_quote
import app.persist as persist_module
from app.pipeline.mapping_runner import run_mapping

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote_unmatched.json"


@pytest.fixture
def quote_id(tmp_path, monkeypatch):
    """隔离库 + 灌主数据 + persist 未匹配样例，返回 quote_id。"""
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    env = {**os.environ, "QUOTES_DB_PATH": str(tmp_path / "t.db")}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return persist_quote(data, project_name="L2测试")["quote_id"]


def _fake_llm(matches: dict):
    calls = []

    def _chat(messages, **kwargs):
        calls.append(messages)
        return {"matches": matches}, {"model": "fake", "total_tokens": 50, "elapsed_ms": 3}

    _chat.calls = calls
    return _chat


def _category_candidates() -> list[str]:
    conn = get_connection()
    codes = [
        r[0]
        for r in conn.execute(
            """SELECT a.code FROM atom a JOIN atom_category ac ON a.code = ac.atom_code
               WHERE ac.category_code = 'CAT-WJWK' AND a.is_fallback = 0 ORDER BY a.code"""
        )
    ]
    conn.close()
    return codes


def _line(quote_id, item_name):
    conn = get_connection()
    row = conn.execute(
        "SELECT atom_code, confidence, match_path, is_new_process, bundle_flag, fingerprint, note"
        " FROM quote_line WHERE quote_id=? AND module='processing' AND item_name=?",
        (quote_id, item_name),
    ).fetchone()
    conn.close()
    return dict(row)


def _snapshot(quote_id) -> dict:
    conn = get_connection()
    path = conn.execute("SELECT raw_json_path FROM quote WHERE id=?", (quote_id,)).fetchone()[0]
    conn.close()
    return json.loads(open(path, encoding="utf-8").read())


def test_l2_unique_match_and_fallback_new_process(quote_id, monkeypatch):
    from app.llm import client as llm_client

    code = _category_candidates()[0]
    # l2_pending = [EDM(歧义), 激光熔覆(未命中)]，索引 0/1
    chat = _fake_llm({"0": code, "1": None})
    monkeypatch.setattr(llm_client, "chat_json", chat)

    stats = run_mapping(quote_id)
    assert stats["l2_matched"] == 1
    assert stats["l2_new_process"] == 1
    assert len(chat.calls) == 1  # 一次调用批处理所有未匹配条目

    edm = _line(quote_id, "EDM")
    assert edm["atom_code"] == code
    assert edm["confidence"] == "medium"  # DB 词汇
    assert edm["match_path"] == "L2_llm"
    assert edm["fingerprint"] == code
    assert edm["is_new_process"] == 0

    laser = _line(quote_id, "激光熔覆")
    assert laser["atom_code"] == "AT-QT-001"
    assert laser["is_new_process"] == 1
    assert laser["confidence"] == "low"
    assert laser["match_path"] == "L2_llm"

    # 快照与 parse_log（is_llm_call=1）
    snap = _snapshot(quote_id)
    items = {i["name"]: i for i in snap["unit_price"]["processing"]["items"]}
    assert items["EDM"]["atom_code"] == code
    assert items["激光熔覆"]["atom_code"] == "AT-QT-001"
    assert items["激光熔覆"]["is_new_process"] is True

    conn = get_connection()
    log = conn.execute(
        "SELECT detail FROM parse_log WHERE quote_id=? AND action='l2_llm_match'", (quote_id,)
    ).fetchone()
    conn.close()
    assert json.loads(log["detail"])["items"] == 2


def test_l2_fabricated_code_goes_to_fallback(quote_id, monkeypatch):
    from app.llm import client as llm_client

    chat = _fake_llm({"0": "AT-FAKE-999", "1": "AT-XX-000"})
    monkeypatch.setattr(llm_client, "chat_json", chat)
    stats = run_mapping(quote_id)

    assert stats["l2_matched"] == 0
    assert stats["l2_new_process"] == 2
    for name in ("EDM", "激光熔覆"):
        line = _line(quote_id, name)
        assert line["atom_code"] == "AT-QT-001"
        assert line["is_new_process"] == 1


def test_l2_multi_code_bundle(quote_id, monkeypatch):
    from app.llm import client as llm_client

    cands = _category_candidates()
    pair = sorted(cands[:2])
    chat = _fake_llm({"0": pair, "1": None})
    monkeypatch.setattr(llm_client, "chat_json", chat)
    stats = run_mapping(quote_id)

    assert stats["l2_matched"] == 1
    edm = _line(quote_id, "EDM")
    assert edm["bundle_flag"] == 1
    assert edm["fingerprint"] == "|".join(pair)
    assert edm["confidence"] == "medium"
    assert edm["match_path"] == "L2_llm"

    items = {i["name"]: i for i in _snapshot(quote_id)["unit_price"]["processing"]["items"]}
    assert items["EDM"]["bundle_members"] == pair
    assert items["EDM"]["split_method"] == "estimated"
    assert items["EDM"]["bundle_flag"] is True


def test_l2_gateway_down_raises(quote_id):
    """LLM 故障不降级：conftest 把 chat_json 替换为抛 LLMUnavailable，run_mapping 直接上抛。"""
    from app.llm import client as llm_client

    with pytest.raises(LLMUnavailable):
        run_mapping(quote_id)

    edm = _line(quote_id, "EDM")
    assert edm["atom_code"] is None  # 未写入任何 L2 结果
    assert edm["match_path"] is None

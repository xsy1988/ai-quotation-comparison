"""就地编辑（correction_service + corrections API）：金额/原子/品类修正、别名回流、快照同步。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from app.persist import persist_quote

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote_unmatched.json"

client = TestClient(app)


def _mock_l2_empty(monkeypatch):
    """chat_json 替身：L2 语义匹配返回空映射（全部走兜底新工艺），不触网。"""
    from app.llm import client as llm_client

    def _fake_chat(messages, **kwargs):
        return {"matches": {}}, {"model": "fake", "total_tokens": 10, "elapsed_ms": 1}

    monkeypatch.setattr(llm_client, "chat_json", _fake_chat)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """灌主数据 + 持久化样例报价单（L1 未匹配版本），返回 quote_id。"""
    import app.persist as persist_module

    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "corr.db"))
    env = {**os.environ, "QUOTES_DB_PATH": str(tmp_path / "corr.db")}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    init_db()
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    result = persist_quote(data, project_name="修正测试")
    return result["quote_id"]


def _line_id(quote_id, item_name):
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM quote_line WHERE quote_id = ? AND item_name = ?",
        (quote_id, item_name),
    ).fetchone()
    conn.close()
    return row["id"]


def _snapshot(quote_id):
    conn = get_connection()
    path = conn.execute("SELECT raw_json_path FROM quote WHERE id = ?", (quote_id,)).fetchone()[0]
    conn.close()
    return json.loads(open(path, encoding="utf-8").read())


def _quote_row(quote_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM quote WHERE id = ?", (quote_id,)).fetchone()
    conn.close()
    return row


# ---------- 金额修正 ----------

def test_amount_correction_resyncs_and_rederives(prepared):
    quote_id = prepared
    line_id = _line_id(quote_id, "CNC加工")

    # 改前构造 fail：条目金额 2.0 → 5.0（Σitems 4.2 → 7.2 > total 4.2）
    conn = get_connection()
    with conn:
        conn.execute("UPDATE quote_line SET amount = 5.0 WHERE id = ?", (line_id,))
    conn.close()
    data = _snapshot(quote_id)
    data["unit_price"]["processing"]["items"][0]["amount_per_pc"] = 5.0
    path = _quote_row(quote_id)["raw_json_path"]
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    resp = client.patch(f"/api/quote_lines/{line_id}", json={"amount": 2.0})
    assert resp.status_code == 200

    # quote_line 与快照同步
    conn = get_connection()
    amount = conn.execute("SELECT amount FROM quote_line WHERE id = ?", (line_id,)).fetchone()[0]
    conn.close()
    assert amount == 2.0
    item = _snapshot(quote_id)["unit_price"]["processing"]["items"][0]
    assert item["amount_per_pc"] == 2.0

    # 汇总重推导：fixture 勾稽自洽，改回后 untaxed=10.5 tax=0.51 final=11.01
    quote = _quote_row(quote_id)
    assert quote["calc_check"] == "pass"
    assert quote["untaxed_total"] == pytest.approx(10.5)
    assert quote["tax_amount"] == pytest.approx(0.51)
    assert quote["final_unit_price_taxed"] == pytest.approx(11.01)
    assert quote["processing_total"] == pytest.approx(4.2)
    assert "calc_abnormal" not in json.loads(quote["flags"] or "[]")
    assert quote["parse_status"] == "reviewed"
    assert _snapshot(quote_id)["basic"]["parse_status"] == "reviewed"


def test_module_totals_and_discount_rederive(prepared):
    quote_id = prepared
    resp = client.patch(
        f"/api/quotes/{quote_id}",
        json={"module_totals": {"processing": 5.0}, "discount": 1.0},
    )
    assert resp.status_code == 200

    quote = _quote_row(quote_id)
    assert quote["processing_total"] == pytest.approx(5.0)
    assert quote["discount"] == pytest.approx(1.0)
    # untaxed = 4.5+5.0+0.3+0.15+(1.86−0.51) = 11.3；final = 11.3 + 0.51 − 1.0 = 10.81
    assert quote["untaxed_total"] == pytest.approx(11.3)
    assert quote["tax_amount"] == pytest.approx(0.51)
    assert quote["final_unit_price_taxed"] == pytest.approx(10.81)
    assert quote["calc_check"] == "pass"

    snapshot = _snapshot(quote_id)
    assert snapshot["unit_price"]["processing"]["total"] == 5.0
    assert snapshot["unit_price"]["summary"]["untaxed_total"] == pytest.approx(11.3)
    assert snapshot["unit_price"]["summary"]["final_unit_price_taxed"] == pytest.approx(10.81)
    assert snapshot["basic"]["parse_status"] == "reviewed"


def test_basic_info_and_supplier_name(prepared):
    quote_id = prepared
    resp = client.patch(
        f"/api/quotes/{quote_id}",
        json={"part_name": "新零件名", "supplier_name": "新供应商名"},
    )
    assert resp.status_code == 200
    quote = _quote_row(quote_id)
    assert quote["supplier_name"] == "新供应商名"
    assert quote["parse_status"] == "reviewed"
    snapshot = _snapshot(quote_id)
    assert snapshot["basic"]["part_name"] == "新零件名"
    assert snapshot["supplier"]["supplier_name"] == "新供应商名"


# ---------- 原子人工修正 ----------

def test_atom_manual_correction_alias_and_suggestion(prepared):
    quote_id = prepared
    line_id = _line_id(quote_id, "CNC加工")

    conn = get_connection()
    with conn:
        conn.execute(
            """INSERT INTO new_atom_suggestion (quote_line_id, source_text, status)
               VALUES (?, 'CNC加工', 'pending')""",
            (line_id,),
        )
        # 该条目先标为新工艺候选，验证人工修正后清零
        conn.execute("UPDATE quote_line SET is_new_process = 1 WHERE id = ?", (line_id,))
    conn.close()

    resp = client.patch(f"/api/quote_lines/{line_id}", json={"atom_code": "AT-ZH-013"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["atom_code"] == "AT-ZH-013"
    assert body["match_path"] == "manual"
    assert body["confidence"] == "high"
    assert body["confirm_status"] == "corrected"
    assert body["fingerprint"] == "AT-ZH-013"
    assert body["is_new_process"] == 0
    assert body["bundle_flag"] == 0

    # 别名回流：原文写法 → atom_alias(source=manual_feedback)
    conn = get_connection()
    alias = conn.execute(
        """SELECT source FROM atom_alias WHERE atom_code = 'AT-ZH-013'
           AND alias_text = 'CNC加工'"""
    ).fetchone()
    suggestion = conn.execute(
        "SELECT status FROM new_atom_suggestion WHERE quote_line_id = ?", (line_id,)
    ).fetchone()
    conn.close()
    assert alias["source"] == "manual_feedback"
    assert suggestion["status"] == "merged"

    # 快照条目同步
    item = _snapshot(quote_id)["unit_price"]["processing"]["items"][0]
    assert item["atom_code"] == "AT-ZH-013"
    assert item["match_path"] == "manual"
    assert item["is_new_process"] is False
    assert item["bundle_members"] == ["AT-ZH-013"]

    # 重复修正：UNIQUE(atom_code, alias_text) 冲突静默跳过，不报错
    resp = client.patch(f"/api/quote_lines/{line_id}", json={"atom_code": "AT-ZH-013"})
    assert resp.status_code == 200


def test_confirm_status_enum_and_note(prepared):
    quote_id = prepared
    line_id = _line_id(quote_id, "CNC加工")
    resp = client.patch(f"/api/quote_lines/{line_id}", json={"confirm_status": "confirmed", "note": "已复核"})
    assert resp.status_code == 200
    assert resp.json()["confirm_status"] == "confirmed"
    assert resp.json()["note"] == "已复核"

    resp = client.patch(f"/api/quote_lines/{line_id}", json={"confirm_status": "bogus"})
    assert resp.status_code == 400


# ---------- 错误分支 ----------

def test_not_found_and_invalid_references(prepared):
    resp = client.patch("/api/quotes/999999", json={"part_name": "x"})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "报价单不存在"}

    resp = client.patch("/api/quote_lines/999999", json={"amount": 1.0})
    assert resp.status_code == 404

    quote_id = prepared
    line_id = _line_id(quote_id, "CNC加工")
    resp = client.patch(f"/api/quote_lines/{line_id}", json={"atom_code": "AT-XX-999"})
    assert resp.status_code == 400
    assert "原子不存在" in resp.json()["detail"]

    resp = client.patch(f"/api/quotes/{quote_id}", json={"category_code": "CAT-NOPE"})
    assert resp.status_code == 400
    assert "品类不存在" in resp.json()["detail"]


# ---------- 改品类：重置 + 重跑 ----------

def test_category_change_resets_and_reruns(prepared, monkeypatch):
    quote_id = prepared
    manual_line_id = _line_id(quote_id, "激光熔覆")

    # 先把激光熔覆人工修正为 manual，验证重跑不被覆盖
    resp = client.patch(f"/api/quote_lines/{manual_line_id}", json={"atom_code": "AT-ZP-005"})
    assert resp.status_code == 200

    _mock_l2_empty(monkeypatch)
    resp = client.patch(f"/api/quotes/{quote_id}", json={"category_code": "CAT-CMF"})
    assert resp.status_code == 200
    assert resp.json()["category_code"] == "CAT-CMF"

    # comparison_task / quote / 快照三处同步
    quote = _quote_row(quote_id)
    assert quote["category_code"] == "CAT-CMF"
    conn = get_connection()
    task_category = conn.execute(
        "SELECT category_code FROM comparison_task WHERE id = ?", (quote["task_id"],)
    ).fetchone()["category_code"]
    conn.close()
    assert task_category == "CAT-CMF"
    assert _snapshot(quote_id)["basic"]["category"] == "CAT-CMF"

    # 非 manual 条目重跑：L1 命中照旧，L1 未命中（EDM）走 L2 兜底新工艺
    conn = get_connection()
    lines = {
        r["item_name"]: dict(r)
        for r in conn.execute(
            "SELECT item_name, atom_code, match_path, is_new_process FROM quote_line"
            " WHERE quote_id = ? AND module = 'processing'",
            (quote_id,),
        )
    }
    conn.close()
    assert lines["CNC加工"]["atom_code"] == "AT-QX-001"
    assert lines["CNC加工"]["match_path"] == "L1_alias"
    assert lines["EDM"]["atom_code"] == "AT-QT-001"  # L2 空映射 → 兜底
    assert lines["EDM"]["match_path"] == "L2_llm"
    assert lines["EDM"]["is_new_process"] == 1
    # manual 条目不被覆盖
    assert lines["激光熔覆"]["atom_code"] == "AT-ZP-005"
    assert lines["激光熔覆"]["match_path"] == "manual"


def test_category_change_llm_unavailable_returns_502(prepared):
    """LLM 不可用（conftest 禁网替身）：不降级，API 返回 502 + 原始错误。"""
    quote_id = prepared
    resp = client.patch(f"/api/quotes/{quote_id}", json={"category_code": "CAT-CMF"})
    assert resp.status_code == 502
    assert "LLM" in resp.json()["detail"]


# ---------- 只读前置 API ----------

def test_atoms_and_categories_search(prepared):
    resp = client.get("/api/atoms", params={"q": "CNC"})
    assert resp.status_code == 200
    atoms = resp.json()
    assert any(a["code"] == "AT-QX-001" for a in atoms)

    resp = client.get("/api/atoms")
    assert resp.status_code == 200
    assert len(resp.json()) <= 50

    resp = client.get("/api/categories")
    assert resp.status_code == 200
    categories = resp.json()
    assert {"code": "CAT-WJWK", "name": "五金外壳"} in categories

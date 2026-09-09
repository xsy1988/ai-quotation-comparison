"""API 端到端：创建任务、comparison 结构、tasks 列表、SSE 首帧、404。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.ingest as ingest_module
import app.persist as persist_module
import app.pipeline.pipeline as pipeline_module
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote.xlsx"

client = TestClient(app)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(ingest_module, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(ingest_module, "IR_DIR", tmp_path / "ir")
    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    db_path = tmp_path / "api.db"
    monkeypatch.setenv("QUOTES_DB_PATH", str(db_path))
    env = {**os.environ, "QUOTES_DB_PATH": str(db_path)}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    _mock_llm_layout(monkeypatch)


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


def _upload(project="API测试"):
    return client.post(
        "/api/tasks",
        data={"project_name": project},
        files=[("files", ("sample_quote.xlsx", FIXTURE.read_bytes(),
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
    )


def test_create_task_and_comparison(prepared):
    resp = _upload()
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    # TestClient 会同步执行 BackgroundTasks，此处任务已 parsed
    resp = client.get(f"/api/tasks/{task_id}/comparison")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == task_id
    assert len(data["suppliers"]) == 1
    supplier = data["suppliers"][0]
    assert supplier["supplier_name"] == "深圳市样例五金有限公司"
    assert supplier["calc_check"] == "pass"
    assert supplier["final_unit_price_taxed"] == 10.51

    hierarchy = {row["key"]: row for row in data["hierarchy"]}
    assert hierarchy["materials"]["label"] == "材料费"
    assert hierarchy["materials"]["values"][str(supplier["quote_id"])] == 4.5
    assert hierarchy["final_unit_price_taxed"]["values"][str(supplier["quote_id"])] == 10.51

    assert len(data["drawers"]) == 3
    assert [t["type"] for t in data["tooling"]] == ["mold", "fixture", "stencil"]
    assert len(data["warnings"]) == 1


def test_tasks_list(prepared):
    _upload("列表项目")
    _upload("列表项目2")
    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 2
    assert tasks[0]["project_name"] == "列表项目2"  # 倒序
    assert tasks[0]["quote_count"] == 1
    assert tasks[0]["status"] == "parsed"


def test_progress_sse(prepared):
    task_id = _upload().json()["task_id"]
    frames = []
    with client.stream("GET", f"/api/tasks/{task_id}/progress") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            frames.append(line)
            if line == "event: done":
                break
    text = "\n".join(frames)
    assert "event: progress" in text
    assert "event: done" in text
    assert '"task_status": "parsed"' in text


def test_comparison_404(prepared):
    resp = client.get("/api/tasks/999999/comparison")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "任务不存在"}


def test_snapshot_endpoint(prepared):
    task_id = _upload().json()["task_id"]
    comparison = client.get(f"/api/tasks/{task_id}/comparison").json()
    quote_id = comparison["suppliers"][0]["quote_id"]
    resp = client.get(f"/api/tasks/{task_id}/quotes/{quote_id}/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["schema_version"] == "1.1"
    assert data["supplier"]["supplier_name"] == "深圳市样例五金有限公司"

    resp = client.get(f"/api/tasks/{task_id}/quotes/999999/snapshot")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "报价单不存在"}


def test_llm_failure_aborts_task_and_surfaces_error(tmp_path, monkeypatch):
    """LLM 故障（conftest 禁网）：任务中止为 failed，进度接口透出错误信息。"""
    monkeypatch.setattr(pipeline_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(ingest_module, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(ingest_module, "IR_DIR", tmp_path / "ir")
    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "api_fail.db"))

    task_id = _upload().json()["task_id"]
    task = client.get("/api/tasks").json()[0]
    assert task["status"] == "failed"

    frames = []
    with client.stream("GET", f"/api/tasks/{task_id}/progress") as resp:
        for line in resp.iter_lines():
            frames.append(line)
            if line == "event: done":
                break
    text = "\n".join(frames)
    assert '"task_status": "failed"' in text
    assert '"error"' in text  # 失败报价带错误信息


def test_create_task_without_files_400(prepared):
    resp = client.post("/api/tasks", data={"project_name": "空上传"})
    assert resp.status_code == 400
    assert resp.json() == {"detail": "未上传文件"}


def test_patch_quote_and_line_via_api(prepared):
    """就地编辑 API：PATCH quote 基本信息 + PATCH quote_line 金额，快照同步、状态转 reviewed。"""
    task_id = _upload().json()["task_id"]
    comparison = client.get(f"/api/tasks/{task_id}/comparison").json()
    quote_id = comparison["suppliers"][0]["quote_id"]
    assert comparison["suppliers"][0]["category_code"] == "CAT-WJWK"

    resp = client.patch(f"/api/quotes/{quote_id}", json={"moq": 5000})
    assert resp.status_code == 200
    assert resp.json()["parse_status"] == "reviewed"

    resp = client.patch(f"/api/quote_lines/999999", json={"amount": 1.5})
    assert resp.status_code == 404

    detail = next(d for d in comparison["processing_details"] if d["quote_id"] == quote_id)
    line = next(i for i in detail["items"] if i["name"] == "CNC加工")

    resp = client.patch(f"/api/quote_lines/{line['id']}", json={"amount": 1.5})
    assert resp.status_code == 200
    assert resp.json()["amount"] == 1.5

    resp = client.get(f"/api/tasks/{task_id}/quotes/{quote_id}/snapshot")
    data = resp.json()
    cnc = next(i for i in data["unit_price"]["processing"]["items"] if i["name"] == "CNC加工")
    assert cnc["amount_per_pc"] == 1.5
    assert data["basic"]["moq"] == 5000
    assert data["basic"]["parse_status"] == "reviewed"

    # comparison 重新计算：模块无 total 字段时合计 = Σitems（2.0→1.5 后 3.7）
    hierarchy = {
        row["key"]: row for row in client.get(f"/api/tasks/{task_id}/comparison").json()["hierarchy"]
    }
    assert hierarchy["processing"]["values"][str(quote_id)] == 3.7

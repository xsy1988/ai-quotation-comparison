"""对象存储层与报价单源文件接口：内容寻址去重、登记、上传即落库、下载/预览、回退与 404。"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.ingest as ingest_module
import app.persist as persist_module
import app.pipeline.pipeline as pipeline_module
from app.config import settings
from app.db import get_connection, init_db
from app.main import app
from app.storage import (
    LocalObjectStore,
    ObjectStoreError,
    S3ObjectStore,
    content_type_of,
    find_object_by_sha,
    find_object_for_quote,
    get_store,
    is_previewable,
    set_store,
    store_and_record,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote.xlsx"

client = TestClient(app)


def _new_task(conn, name: str) -> int:
    conn.execute(
        "INSERT INTO comparison_task (project_name, status) VALUES (?, 'parsed')", (name,)
    )
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def _seed_task_quote(conn, task_name: str) -> tuple[int, int]:
    """建一个任务 + 一份报价单占位行（stored_object 的外键需要真实归属）。"""
    task_id = _new_task(conn, task_name)
    conn.execute(
        "INSERT INTO quote (task_id, supplier_name, parse_status) VALUES (?, '甲供应商', 'parsed')",
        (task_id,),
    )
    quote_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit()
    return task_id, quote_id


def _store(tmp_path) -> LocalObjectStore:
    store = LocalObjectStore(tmp_path / "objects")
    set_store(store)
    return store


def test_key_is_content_addressed(tmp_path):
    store = _store(tmp_path)
    first = store.put_bytes(b"hello", "报价单.xlsx")
    second = store.put_bytes(b"hello", "另一个名字.xlsx")
    assert first.key == second.key  # 同内容同 key，天然去重
    assert first.key == f"objects/{first.sha256[:2]}/{first.sha256}.xlsx"
    assert store.local_path(first.key).read_bytes() == b"hello"


def test_put_file_links_and_reports_metadata(tmp_path):
    store = _store(tmp_path)
    stored = store.put_file(FIXTURE, original_name="供应商甲报价.xlsx")
    assert stored.original_name == "供应商甲报价.xlsx"
    assert stored.backend == "local"
    assert stored.size_bytes == FIXTURE.stat().st_size
    assert stored.content_type.startswith("application/vnd.openxmlformats")
    assert store.exists(stored.key)
    assert store.local_path(stored.key).read_bytes() == FIXTURE.read_bytes()
    # 二次 put 不重复写字节（硬链接/file 均已存在）
    again = store.put_file(FIXTURE, original_name="供应商甲报价.xlsx")
    assert again.key == stored.key


def test_register_and_lookup(tmp_path):
    init_db()
    conn = get_connection()
    try:
        store = _store(tmp_path)
        task_id, quote_id = _seed_task_quote(conn, "登记测试")
        stored = store_and_record(
            conn, FIXTURE, original_name="样品.xlsx", task_id=task_id, quote_id=quote_id
        )
        conn.commit()
        row = find_object_for_quote(conn, quote_id)
        assert row["object_key"] == stored.key
        assert row["original_name"] == "样品.xlsx"
        assert row["task_id"] == task_id
        assert row["size_bytes"] == FIXTURE.stat().st_size
        assert find_object_for_quote(conn, quote_id + 1000) is None
        assert find_object_by_sha(conn, stored.sha256)["quote_id"] == quote_id
        assert find_object_by_sha(conn, "0" * 64) is None
    finally:
        conn.close()


def test_same_content_uploaded_twice_keeps_both_names(tmp_path):
    """同一文件被两次上传：字节只存一份，但每次上传各留一条登记（原始文件名按次留存）。"""
    init_db()
    conn = get_connection()
    try:
        store = _store(tmp_path)
        first = _seed_task_quote(conn, "甲家上传")
        second = _seed_task_quote(conn, "乙家上传")
        store_and_record(conn, FIXTURE, original_name="甲.xlsx", task_id=first[0], quote_id=first[1])
        store_and_record(conn, FIXTURE, original_name="乙.xlsx", task_id=second[0], quote_id=second[1])
        conn.commit()
        names = [r["original_name"] for r in conn.execute("SELECT original_name FROM stored_object")]
        assert names == ["甲.xlsx", "乙.xlsx"]
        assert find_object_for_quote(conn, first[1])["original_name"] == "甲.xlsx"
        assert find_object_for_quote(conn, second[1])["original_name"] == "乙.xlsx"
        objects = list((tmp_path / "objects").rglob("*"))
        assert len([p for p in objects if p.is_file()]) == 1
    finally:
        conn.close()


def test_s3_backend_is_interface_only(monkeypatch):
    monkeypatch.setenv("OBJECT_STORE_BACKEND", "s3")
    set_store(None)
    try:
        assert isinstance(get_store(), S3ObjectStore)
        with pytest.raises(ObjectStoreError, match="尚未接入"):
            get_store().put_file(FIXTURE, original_name="x.xlsx")
        assert get_store().local_path("objects/xx/yy.xlsx") is None
    finally:
        set_store(None)


def test_helpers():
    assert content_type_of("a.pdf") == "application/pdf"
    assert content_type_of("a.bin") == "application/octet-stream"
    assert is_previewable("a.PNG") is True
    assert is_previewable("a.xlsx") is False


def test_ingest_stores_source_once(tmp_path, monkeypatch):
    """直接接入（脚本路径）也落对象存储；重复接入同一内容不重复登记。"""
    from app.ingest import ensure_stored

    monkeypatch.setattr(ingest_module, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(ingest_module, "IR_DIR", tmp_path / "ir")
    _store(tmp_path)
    init_db()
    digest = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    conn = get_connection()
    try:
        ensure_stored(FIXTURE, digest)
        ensure_stored(FIXTURE, digest)
        rows = conn.execute("SELECT original_name FROM stored_object").fetchall()
        assert [r["original_name"] for r in rows] == [FIXTURE.name]
        # 上传钩子已登记过同内容（按 sha 命中）则跳过
        assert find_object_by_sha(conn, digest) is not None
    finally:
        conn.close()


# ---------- 源文件接口 ----------


def _mock_llm_layout(monkeypatch):
    from app.ingest import excel_to_ir, sha256_of
    from app.llm import client as llm_client
    from app.pipeline.simple_excel_parse import parse_ir as rule_parse_ir

    def _fake_chat(messages, **kwargs):
        data = rule_parse_ir(excel_to_ir(FIXTURE, sha256_of(FIXTURE)))
        data["basic"]["category"] = "CAT-WJWK"
        return data, {"model": "fake", "total_tokens": 10, "elapsed_ms": 1}

    monkeypatch.setattr(llm_client, "chat_json", _fake_chat)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(ingest_module, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(ingest_module, "IR_DIR", tmp_path / "ir")
    monkeypatch.setattr(persist_module, "SNAPSHOT_DIR", tmp_path / "snapshots")
    db_path = tmp_path / "storage.db"
    monkeypatch.setenv("QUOTES_DB_PATH", str(db_path))
    env = {**os.environ, "QUOTES_DB_PATH": str(db_path)}
    subprocess.run(
        [sys.executable, "scripts/import_master_data.py"],
        cwd=BACKEND_DIR, env=env, check=True, capture_output=True,
    )
    set_store(LocalObjectStore(tmp_path / "object_store"))
    init_db()
    _mock_llm_layout(monkeypatch)
    yield tmp_path
    set_store(None)


def _upload(name="供应商甲报价单.xlsx"):
    return client.post(
        "/api/tasks",
        data={"project_name": "源文件测试"},
        files=[("files", (name, FIXTURE.read_bytes(), "application/octet-stream"))],
    )


def test_upload_persists_source_file(prepared):
    task_id = _upload().json()["task_id"]
    detail = client.get("/api/quotes/1").json()
    assert detail["quote_id"] == 1
    source = detail["source_file"]
    assert source["name"] == "供应商甲报价单.xlsx"
    assert source["content_type"].startswith("application/vnd.openxmlformats")
    assert source["size_bytes"] == FIXTURE.stat().st_size
    assert source["previewable"] is False  # xlsx 不能在线预览，只能下载
    assert (settings.object_store_dir).is_dir()
    assert client.get(f"/api/tasks/{task_id}/comparison").status_code == 200


def test_source_download_and_preview(prepared):
    _upload()
    resp = client.get("/api/quotes/1/source")
    assert resp.status_code == 200
    assert resp.content == FIXTURE.read_bytes()
    assert "attachment" in resp.headers["content-disposition"]
    # 中文文件名按 RFC 5987 编码，避免 latin-1 报错
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]

    resp = client.get("/api/quotes/1/source/preview")
    assert resp.status_code == 415  # xlsx 不能内联预览
    assert "预览" in resp.json()["detail"]


def test_source_preview_allows_pdf_and_images(prepared):
    """pdf/图片可内联预览：直接放一份对象与登记，不依赖解析流程。"""
    conn = get_connection()
    try:
        task_id = _new_task(conn, "源文件预览")
        conn.execute(
            "INSERT INTO quote (task_id, supplier_name, parse_status) VALUES (?, 'PDF供应商', 'parsed')",
            (task_id,),
        )
        quote_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        pdf = FIXTURE.parent / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        store_and_record(conn, pdf, original_name="某供应商报价.pdf", task_id=None, quote_id=quote_id)
        conn.commit()

        resp = client.get(f"/api/quotes/{quote_id}/source/preview")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert "inline" in resp.headers["content-disposition"]
        assert client.get(f"/api/quotes/{quote_id}/source").content == b"%PDF-1.4\n"
    finally:
        conn.close()
        (FIXTURE.parent / "fake.pdf").unlink(missing_ok=True)


def test_source_missing_returns_404(prepared):
    conn = get_connection()
    try:
        task_id = _new_task(conn, "源文件缺失")
        conn.execute(
            "INSERT INTO quote (task_id, supplier_name, parse_status) VALUES (?, '无源文件', 'parsed')",
            (task_id,),
        )
        quote_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    assert client.get(f"/api/quotes/{quote_id}/source").status_code == 404
    assert client.get(f"/api/quotes/{quote_id}/source/preview").status_code == 404
    assert client.get("/api/quotes/999999/source").status_code == 404


def test_source_falls_back_to_archive(prepared):
    """存储不可用/老数据：回退 source_file 归档副本仍能下载。"""
    archived = prepared / "archive" / "abc12345_老数据.xlsx"
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_bytes(b"old-bytes")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO source_file (sha256, original_name, file_type, archived_path, ir_path)"
            " VALUES ('deadbeef', '老数据.xlsx', 'xlsx', ?, 'ir.json')",
            (str(archived),),
        )
        task_id = _new_task(conn, "老数据回退")
        conn.execute(
            "INSERT INTO quote (task_id, supplier_name, parse_status, file_hash)"
            " VALUES (?, '老供应商', 'parsed', 'deadbeef')",
            (task_id,),
        )
        quote_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    resp = client.get(f"/api/quotes/{quote_id}/source")
    assert resp.status_code == 200
    assert resp.content == b"old-bytes"


def test_list_quotes_exposes_source_file_name(prepared):
    _upload()
    items = client.get("/api/quotes").json()["items"]
    assert items[0]["source_file_name"] == "供应商甲报价单.xlsx"

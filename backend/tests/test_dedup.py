"""查重门禁测试：同一文件第二次接入必须复用历史结果。"""

from pathlib import Path

import pytest

import app.ingest as ingest_module
from app.ingest import ingest_file

FIXTURE = Path(__file__).parent / "fixtures" / "sample_quote.xlsx"


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest_module, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(ingest_module, "IR_DIR", tmp_path / "ir")
    return tmp_path


def test_ingest_then_reuse(isolated_dirs):
    first = ingest_file(FIXTURE)
    assert first["status"] == "ingested"
    assert Path(first["archived_path"]).exists()
    assert Path(first["ir_path"]).exists()

    second = ingest_file(FIXTURE)
    assert second["status"] == "reused"
    assert second["ir_path"] == first["ir_path"]


def test_ingest_force_reingests(isolated_dirs):
    first = ingest_file(FIXTURE)
    forced = ingest_file(FIXTURE, force=True)
    assert forced["status"] == "ingested"
    assert forced["sha256"] == first["sha256"]

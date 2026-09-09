"""全格式接入测试：docx/pdf/image → IR、ingest_file 扩展名分派、查重门禁、OCR 路径。"""

import json
import sys
from pathlib import Path

import pytest

from app import ingest, ingest_formats
from app.ingest import ParseError, excel_to_ir, ingest_file, sha256_of
from app.llm.client import LLMError, LLMUnavailable

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """ingest 归档/IR 目录重定向到临时目录，不污染 backend/data。"""
    monkeypatch.setattr(ingest, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(ingest, "IR_DIR", tmp_path / "ir")
    return tmp_path


# ---------------------------------------------------------------------------
# docx
# ---------------------------------------------------------------------------

def _make_docx(path: Path) -> Path:
    from docx import Document

    doc = Document()
    doc.add_paragraph("供应商报价单")
    doc.add_paragraph("供应商：深圳市样例五金有限公司")
    table = doc.add_table(rows=3, cols=3)
    table.cell(0, 0).text = "费用项目"
    table.cell(0, 1).text = "明细"
    table.cell(0, 2).text = "金额"
    table.cell(1, 0).text = "加工费"
    table.cell(1, 1).text = "CNC加工"
    table.cell(1, 2).text = "2.00"
    table.cell(2, 0).text = ""
    table.cell(2, 1).text = ""
    table.cell(2, 2).text = ""
    doc.save(path)
    return path


def test_docx_to_ir(sandbox):
    path = _make_docx(sandbox / "quote.docx")
    ir = ingest_formats.docx_to_ir(path, "hash")

    assert ir.file_type == "docx"
    assert [b.text for b in ir.blocks] == ["供应商报价单", "供应商：深圳市样例五金有限公司"]
    assert all(b.sheet == "doc" and b.col == 1 for b in ir.blocks)
    assert [t.sheet for t in ir.tables] == ["table_1", "table_1"]
    header, row = ir.tables
    assert header.row_number == 1
    assert row.row_number == 2  # 空行（第 3 行）被跳过
    assert [(c.col, c.value) for c in row.cells] == [(1, "加工费"), (2, "CNC加工"), (3, "2.00")]


# ---------------------------------------------------------------------------
# pdf
# ---------------------------------------------------------------------------

def _make_pdf(path: Path, with_text: bool) -> Path:
    reportlab = pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    if with_text:
        c.drawString(72, 720, "Material Fee 4.50")
        c.drawString(72, 700, "Processing CNC 2.00")
    else:
        c.rect(72, 700, 100, 20)  # 只画图无文字 → 无文字层
    c.showPage()
    c.save()
    return path


def test_pdf_to_ir_with_text_layer(sandbox):
    path = _make_pdf(sandbox / "quote.pdf", with_text=True)
    ir = ingest_formats.pdf_to_ir(path, "hash")

    assert ir.file_type == "pdf"
    assert ir.sheets == ["page_1"]
    assert any("Material Fee" in b.text for b in ir.blocks)


def test_pdf_to_ir_no_text_layer_raises_chinese_error(sandbox):
    path = _make_pdf(sandbox / "scan.pdf", with_text=False)
    with pytest.raises(ParseError, match="PDF 无文字层，暂不支持扫描版 PDF，请转为图片上传"):
        ingest_formats.pdf_to_ir(path, "hash")


# ---------------------------------------------------------------------------
# image（vision OCR，mock chat_json）
# ---------------------------------------------------------------------------

def _fake_png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
    return path


def test_image_to_ir_prompt_and_multimodal(sandbox, monkeypatch):
    calls = {}

    def fake_chat_json(messages, *, model=None, client=None):
        calls["messages"] = messages
        calls["model"] = model
        lines = "ocr|1|1:供应商报价单\nocr|2|1:供应商|2:深圳样例公司\nocr|3|1:加工费|2:CNC加工|3:2.00"
        return {"lines": lines}, {"model": model}

    monkeypatch.setattr("app.llm.client.chat_json", fake_chat_json)
    path = _fake_png(sandbox / "scan.png")

    ir = ingest_formats.image_to_ir(path, "hash")

    assert calls["model"] == "qwen3.5-ocr"
    content = calls["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "IR 行格式" in content[0]["text"] and '"lines"' in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    assert ir.file_type == "image" and ir.sheets == ["ocr"]
    assert [(t.row_number, [(c.col, c.value) for c in t.cells]) for t in ir.tables] == [
        (1, [(1, "供应商报价单")]),
        (2, [(1, "供应商"), (2, "深圳样例公司")]),
        (3, [(1, "加工费"), (2, "CNC加工"), (3, "2.00")]),
    ]
    assert ir.blocks == []


def test_image_to_ir_unparseable_line_becomes_textblock(sandbox, monkeypatch):
    def fake_chat_json(messages, *, model=None, client=None):
        return {"lines": "ocr|1|1:正常行\n这是一行无法解析的内容"}, {}

    monkeypatch.setattr("app.llm.client.chat_json", fake_chat_json)
    ir = ingest_formats.image_to_ir(_fake_png(sandbox / "scan.jpg"), "hash")

    assert len(ir.tables) == 1 and ir.tables[0].row_number == 1
    assert len(ir.blocks) == 1 and ir.blocks[0].text == "这是一行无法解析的内容"


def test_image_to_ir_plain_text_compat(sandbox, monkeypatch):
    """网关忽略 response_format 时：dict 里没有 lines，取第一个字符串值按裸文本行处理。"""
    def fake_chat_json(messages, *, model=None, client=None):
        return {"text": "ocr|1|1:裸文本行"}, {}

    monkeypatch.setattr("app.llm.client.chat_json", fake_chat_json)
    ir = ingest_formats.image_to_ir(_fake_png(sandbox / "scan.png"), "hash")

    assert [(t.sheet, t.row_number, t.cells[0].value) for t in ir.tables] == [("ocr", 1, "裸文本行")]


def test_image_to_ir_llm_error_propagates(sandbox, monkeypatch):
    def failing(messages, *, model=None, client=None):
        raise LLMError("OCR 输出不是合法 JSON")

    monkeypatch.setattr("app.llm.client.chat_json", failing)
    with pytest.raises(LLMError):
        ingest_formats.image_to_ir(_fake_png(sandbox / "scan.png"), "hash")


def test_image_to_ir_llm_unavailable_propagates_no_fallback(sandbox, monkeypatch):
    """LLM 不可用：不降级，原样上抛（用户政策）。"""
    def failing(messages, *, model=None, client=None):
        raise LLMUnavailable("LLM 网关不可达或超时")

    monkeypatch.setattr("app.llm.client.chat_json", failing)
    with pytest.raises(LLMUnavailable):
        ingest_formats.image_to_ir(_fake_png(sandbox / "scan.png"), "hash")


# ---------------------------------------------------------------------------
# ingest_file 分派与查重门禁
# ---------------------------------------------------------------------------

def test_ingest_file_dispatches_by_suffix(sandbox, monkeypatch):
    calls = []
    fake_ir = ingest.excel_to_ir(FIXTURES / "sample_quote.xlsx", "fake")

    def fake_to_ir(path, file_hash, **kwargs):
        calls.append(path.suffix)
        return fake_ir

    for fn_name in ("docx_to_ir", "pdf_to_ir", "image_to_ir"):
        monkeypatch.setattr(ingest_formats, fn_name, fake_to_ir)

    src = FIXTURES / "sample_quote.xlsx"
    for i, suffix in enumerate((".docx", ".pdf", ".png")):
        path = sandbox / f"quote{i}{suffix}"  # 内容不同，避开 sha256 查重门禁
        path.write_bytes(src.read_bytes() + str(i).encode())
        result = ingest_file(path)
        assert result["status"] == "ingested"

    assert calls == [".docx", ".pdf", ".png"]


def test_ingest_file_unsupported_suffix(sandbox):
    path = sandbox / "quote.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持的文件格式"):
        ingest_file(path)


def test_ingest_file_dedup_gate_still_applies(sandbox):
    src = FIXTURES / "sample_quote.xlsx"
    first = ingest_file(src)
    assert first["status"] == "ingested"

    second = ingest_file(src)
    assert second["status"] == "reused"
    assert second["ir_path"] == first["ir_path"]

    forced = ingest_file(src, force=True)
    assert forced["status"] == "ingested"


def test_ingest_file_no_text_pdf_raises_chinese_error(sandbox):
    path = _make_pdf(sandbox / "scan.pdf", with_text=False)
    with pytest.raises(ParseError, match="PDF 无文字层"):
        ingest_file(path)


def test_excel_baseline_unchanged(sandbox):
    result = ingest_file(FIXTURES / "sample_quote.xlsx")
    assert result["status"] == "ingested"
    ir_data = json.loads(Path(result["ir_path"]).read_text(encoding="utf-8"))
    assert ir_data["file_type"] == "xlsx"
    assert ir_data["source_file"] == "sample_quote.xlsx"
    assert sha256_of(FIXTURES / "sample_quote.xlsx") == result["sha256"]

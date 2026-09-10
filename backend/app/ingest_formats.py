"""全格式 IR 构造：docx / pdf（文字层 + 扫描版渲染 OCR）/ 图片（vision OCR）→ 与 excel_to_ir 同构的 IR。

OCR 走 LLM 网关 vision 模型（qwen3.5-ocr），失败上抛不降级（用户政策：不降级，中止并反馈）。
扫描版 PDF 由 pypdfium2 按页渲染为 PNG（200 DPI）后逐页 OCR，sheet 记为 page_N。
OCR 输出要求与版面理解的 IR 行格式一致（sheet|行号|列号:值|...），sheet 固定 "ocr"，
因此 layout_understand 的 _serialize_ir / prompt 无需任何改动。
"""

import base64
import io
import json
import re
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from docx import Document

from .ir import CellValue, IR, TableRow, TextBlock

OCR_MODEL = "qwen3.5-ocr"

_OCR_PROMPT = """你是报价单 OCR 转写助手。请只转写图片中的报价单内容，按以下 IR 行格式输出，每行一条记录：

sheet|行号|列号:值|列号:值...

规则：
1. sheet 固定为 "ocr"；行号从 1 开始按视觉行从上到下递增；列号按视觉列从左到右从 1 开始编号。
2. 只转写报价单中的文字与数字，金额保持数字原文（如 4.50、25000），不要换算、不要补全看不见的内容。
3. 一行视觉记录对应一行输出；该行没有值的列直接跳过。
4. 不要输出任何其他文字、解释或 markdown。

把全部 IR 行放进 JSON 的 lines 字段（字符串，行间用 \\n 分隔），只输出这个 JSON 对象：
{"lines": "ocr|1|1:供应商报价单\\nocr|2|1:供应商|2:XX公司"}"""

# 兼容网关忽略 response_format 时返回的裸 IR 行文本
_IR_LINE_RE = re.compile(
    r"^(?P<sheet>[A-Za-z0-9_\u4e00-\u9fff]+)\|(?P<row>\d+)\|(?P<cells>.*)$"
)
_CELL_RE = re.compile(r"^(?P<col>\d+):(?P<value>.*)$")


def docx_to_ir(path: Path, file_hash: str) -> IR:
    """python-docx：段落 → TextBlock(sheet="doc", 顺序号)；表格 → TableRow(sheet="table_N")。"""
    doc = Document(path)
    ir = IR(source_file=path.name, file_hash=file_hash, file_type="docx", sheets=["doc"], tables=[])
    seq = 0
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            seq += 1
            ir.blocks.append(TextBlock(text=text, sheet="doc", row=seq, col=1))
    for t_idx, table in enumerate(doc.tables, start=1):
        sheet = f"table_{t_idx}"
        if sheet not in ir.sheets:
            ir.sheets.append(sheet)
        for r_idx, row in enumerate(table.rows, start=1):
            cells = [
                CellValue(row=r_idx, col=c_idx, value=str(cell.text).strip())
                for c_idx, cell in enumerate(row.cells, start=1)
                if str(cell.text).strip()
            ]
            table_row = TableRow(sheet=sheet, row_number=r_idx, cells=cells)
            if not table_row.is_empty():
                ir.tables.append(table_row)
    return ir


def pdf_to_ir(path: Path, file_hash: str, chat_fn=None) -> IR:
    """pdfplumber 逐页：extract_tables → TableRow(sheet=page_N)；页面纯文本 → TextBlock(sheet=page_N)。

    某页文字层与表格都抽不到内容（扫描页）→ pypdfium2 渲染该页为 PNG（200 DPI）→ vision OCR，
    结果归入同一 page_N sheet。LLM 错误上抛不降级；渲染失败抛 ParseError。
    """
    from .ingest import ParseError

    ir = IR(source_file=path.name, file_hash=file_hash, file_type="pdf", sheets=[], tables=[])
    pdf = pdfium.PdfDocument(str(path))
    try:
        page_count = len(pdf)
        with pdfplumber.open(path) as pl:
            for page_no in range(1, page_count + 1):
                sheet = f"page_{page_no}"
                page = pl.pages[page_no - 1]
                page_has_content = False

                for table in page.extract_tables() or []:
                    rows_added = 0
                    for r_idx, row in enumerate(table, start=1):
                        cells = [
                            CellValue(row=r_idx, col=c_idx, value=str(v).strip())
                            for c_idx, v in enumerate(row, start=1)
                            if v is not None and str(v).strip()
                        ]
                        table_row = TableRow(sheet=sheet, row_number=r_idx, cells=cells)
                        if not table_row.is_empty():
                            ir.tables.append(table_row)
                            rows_added += 1
                    if rows_added:
                        page_has_content = True

                text = (page.extract_text() or "").strip()
                if text:
                    ir.blocks.append(TextBlock(text=text, sheet=sheet, row=1, col=1))
                    page_has_content = True

                if not page_has_content:
                    b64 = _render_pdf_page_b64(pdf, page_no - 1, ParseError)
                    tables, blocks = _ocr_image_b64(b64, "png", sheet, chat_fn)
                    ir.tables.extend(tables)
                    ir.blocks.extend(blocks)
                    page_has_content = bool(tables or blocks)

                if page_has_content:
                    ir.sheets.append(sheet)
    finally:
        pdf.close()
    return ir


def _render_pdf_page_b64(pdf: "pdfium.PdfDocument", index: int, error_cls) -> str:
    """pypdfium2 渲染单页为 200 DPI PNG → base64。渲染失败抛中文 ParseError。"""
    page = pdf[index]
    try:
        try:
            bitmap = page.render(scale=200 / 72)
            pil_image = bitmap.to_pil()
        except Exception as exc:
            raise error_cls(f"扫描版 PDF 第 {index + 1} 页渲染失败：{exc}") from exc
    finally:
        page.close()
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _decode_ocr_lines(parsed: dict) -> str:
    """从 chat_json 返回的 JSON dict 取 lines；兼容网关忽略 response_format 的退化形态。

    优先 parsed["lines"]；其次把 dict 里第一个字符串值当 lines（嵌套 JSON 字符串再解一次）。
    """
    lines = parsed.get("lines")
    if isinstance(lines, str):
        return lines
    for value in parsed.values():
        if isinstance(value, str):
            try:
                inner = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
            if isinstance(inner, dict) and isinstance(inner.get("lines"), str):
                return inner["lines"]
            return value
    raise ValueError("OCR 输出缺少 lines 字段，无法解析 IR 行")


def _parse_ir_lines(lines_text: str) -> tuple[list[TableRow], list]:
    """OCR 文本逐行解析为 TableRow；解析不了的行退化为 TextBlock（顺序保留）。"""
    tables: list[TableRow] = []
    blocks: list[TextBlock] = []
    seq = 0
    for raw in lines_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        seq += 1
        m = _IR_LINE_RE.match(line)
        cells: list[CellValue] = []
        if m:
            for part in m.group("cells").split("|"):
                cm = _CELL_RE.match(part)
                if cm:
                    cells.append(CellValue(row=int(m.group("row")), col=int(cm.group("col")), value=cm.group("value")))
        if cells:
            tables.append(
                TableRow(sheet=m.group("sheet"), row_number=int(m.group("row")), cells=cells)
            )
        else:
            blocks.append(TextBlock(text=line, sheet="ocr", row=seq, col=1))
    return tables, blocks


def _ocr_image_b64(
    b64: str, mime: str, sheet: str, chat_fn=None
) -> tuple[list[TableRow], list[TextBlock]]:
    """base64 图片 → vision OCR（chat_json, model=qwen3.5-ocr）→ IR 行，sheet 归一到调用方指定值。

    LLMError/LLMUnavailable 上抛不捕获（不降级）。OCR 行格式与版面理解 IR 序列化一致。
    """
    from .llm import client as llm_client

    fn = chat_fn or llm_client.chat_json
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}},
            ],
        }
    ]
    parsed, _usage = fn(messages, model=OCR_MODEL)
    tables, blocks = _parse_ir_lines(_decode_ocr_lines(parsed))
    # 提示词固定输出 sheet "ocr"，这里替换为调用方指定的 sheet（page_N / ocr）
    for t in tables:
        t.sheet = sheet
    for b in blocks:
        b.sheet = sheet
    return tables, blocks


def image_to_ir(path: Path, file_hash: str, chat_fn=None) -> IR:
    """图片 → base64 → vision OCR（sheet="ocr"）→ IR。"""
    suffix = path.suffix.lstrip(".").lower()
    mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    tables, blocks = _ocr_image_b64(b64, mime, "ocr", chat_fn)
    return IR(
        source_file=path.name,
        file_hash=file_hash,
        file_type="image",
        sheets=["ocr"],
        blocks=blocks,
        tables=tables,
    )

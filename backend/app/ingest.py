"""文件接入（流水线段⓪①）：sha256 查重门禁 + 全格式 → IR + 原件归档 + IR 快照。

格式分派：.xlsx/.xlsm → excel_to_ir；.docx/.pdf → app.ingest_formats（结构化抽取，
PDF 扫描页自动渲染为图片走 vision OCR）；.png/.jpg/.jpeg → app.ingest_formats.image_to_ir（vision OCR，失败上抛不降级）。
查重门禁与归档/登记逻辑全格式共用。
"""

import hashlib
import json
import shutil
from pathlib import Path

import openpyxl

from .db import get_connection, init_db
from .ir import CellValue, IR, TableRow

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ARCHIVE_DIR = DATA_DIR / "archive"
IR_DIR = DATA_DIR / "ir"

SUPPORTED_SUFFIXES = {".xlsx", ".xlsm", ".docx", ".pdf", ".png", ".jpg", ".jpeg"}


class ParseError(Exception):
    """文件解析失败（如无文字层 PDF、格式损坏），中文错误信息反馈给用户。"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_source_file(file_hash: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM source_file WHERE sha256 = ?", (file_hash,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def excel_to_ir(path: Path, file_hash: str) -> IR:
    wb = openpyxl.load_workbook(path, data_only=True)
    ir = IR(
        source_file=path.name,
        file_hash=file_hash,
        file_type=path.suffix.lstrip(".").lower(),
        sheets=wb.sheetnames,
        tables=[],
    )
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        for row in ws.iter_rows():
            cells = [
                CellValue(row=cell.row, col=cell.column, value=cell.value)
                for cell in row
            ]
            table_row = TableRow(sheet=sheet_name, row_number=row[0].row if row else 0, cells=cells)
            if not table_row.is_empty():
                ir.tables.append(table_row)
    wb.close()
    return ir


def _to_ir(path: Path, file_hash: str) -> IR:
    """按扩展名分派到对应 IR 构造器。"""
    from . import ingest_formats

    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return excel_to_ir(path, file_hash)
    if suffix == ".docx":
        return ingest_formats.docx_to_ir(path, file_hash)
    if suffix == ".pdf":
        return ingest_formats.pdf_to_ir(path, file_hash)
    if suffix in (".png", ".jpg", ".jpeg"):
        return ingest_formats.image_to_ir(path, file_hash)
    raise ValueError(f"不支持的文件格式：{path.suffix}（支持 xlsx/xlsm/docx/pdf/png/jpg/jpeg）")


def ingest_file(path: Path, force: bool = False) -> dict:
    """查重 → 接入 → 归档 + IR 快照。返回处理结果描述（命中历史则复用）。"""
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    init_db()
    file_hash = sha256_of(path)
    existing = find_source_file(file_hash)
    if existing and not force:
        return {
            "status": "reused",
            "sha256": file_hash,
            "message": "文件已解析过，复用历史结果",
            "ir_path": existing["ir_path"],
            "archived_path": existing["archived_path"],
        }

    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"不支持的文件格式：{path.suffix}（支持 xlsx/xlsm/docx/pdf/png/jpg/jpeg）")

    ir = _to_ir(path, file_hash)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    IR_DIR.mkdir(parents=True, exist_ok=True)
    archived_path = ARCHIVE_DIR / f"{file_hash[:8]}_{path.name}"
    if not archived_path.exists():
        shutil.copy2(path, archived_path)
    ir_path = IR_DIR / f"{file_hash[:8]}.json"
    ir_path.write_text(json.dumps(ir.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    conn = get_connection()
    try:
        with conn:
            if existing:
                conn.execute(
                    "UPDATE source_file SET archived_path=?, ir_path=?, original_name=? WHERE sha256=?",
                    (str(archived_path), str(ir_path), path.name, file_hash),
                )
                source_id = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO source_file (sha256, original_name, file_type, archived_path, ir_path)
                       VALUES (?, ?, ?, ?, ?)""",
                    (file_hash, path.name, ir.file_type, str(archived_path), str(ir_path)),
                )
                source_id = cur.lastrowid
    finally:
        conn.close()

    return {
        "status": "ingested",
        "sha256": file_hash,
        "source_id": source_id,
        "sheets": ir.sheets,
        "rows": len(ir.tables),
        "archived_path": str(archived_path),
        "ir_path": str(ir_path),
    }

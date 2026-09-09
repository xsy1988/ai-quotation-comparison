"""就地编辑 API（第 7 步）：PATCH quote / quote_line、原子搜索、品类列表。

LLM 故障不降级：改品类触发重跑映射时 LLM 报错 → 502 透出原始错误。
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.llm.client import LLMError
from app.services.correction_service import (
    CorrectionError,
    CorrectionNotFound,
    patch_quote,
    patch_quote_line,
)

router = APIRouter(prefix="/api", tags=["corrections"])


class QuotePatch(BaseModel):
    part_name: str | None = None
    material_spec: str | None = None
    quote_date: str | None = None
    currency: str | None = None
    moq: int | None = None
    quote_no: str | None = None
    supplier_name: str | None = None
    category_code: str | None = None
    module_totals: dict[str, float | None] | None = None
    discount: float | None = None

    def to_patch(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class QuoteLinePatch(BaseModel):
    amount: float | None = None
    atom_code: str | None = None
    note: str | None = None
    confirm_status: str | None = None

    def to_patch(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


@router.patch("/quotes/{quote_id}")
def patch_quote_endpoint(quote_id: int, body: QuotePatch) -> dict:
    init_db()
    conn = get_connection()
    try:
        try:
            return patch_quote(conn, quote_id, body.to_patch())
        except CorrectionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except CorrectionError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except LLMError as e:
            # 用户政策：LLM 不可用/报错时不降级，透出原始错误供前端 Alert 展示
            raise HTTPException(status_code=502, detail=str(e))
    finally:
        conn.close()


@router.patch("/quote_lines/{line_id}")
def patch_quote_line_endpoint(line_id: int, body: QuoteLinePatch) -> dict:
    init_db()
    conn = get_connection()
    try:
        try:
            return patch_quote_line(conn, line_id, body.to_patch())
        except CorrectionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except CorrectionError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@router.get("/atoms")
def search_atoms(q: str = Query(default="")) -> list[dict]:
    init_db()
    conn = get_connection()
    try:
        like = f"%{q}%"
        return [
            {"code": row["code"], "name": row["name"]}
            for row in conn.execute(
                """SELECT code, name FROM atom
                   WHERE code LIKE ? OR name LIKE ?
                   ORDER BY code LIMIT 50""",
                (like, like),
            )
        ]
    finally:
        conn.close()


@router.get("/categories")
def list_categories() -> list[dict]:
    init_db()
    conn = get_connection()
    try:
        return [
            {"code": row["code"], "name": row["name"]}
            for row in conn.execute("SELECT code, name FROM category ORDER BY code")
        ]
    finally:
        conn.close()

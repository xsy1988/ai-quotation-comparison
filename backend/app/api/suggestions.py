"""新工艺决策 API（第 8 步）：建议队列、决策回执、决策用枚举接口。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import get_connection, init_db
from app.services.correction_service import CorrectionError, CorrectionNotFound
from app.services.new_atom_service import list_suggestions, resolve_suggestion

router = APIRouter(prefix="/api", tags=["suggestions"])


class ResolveBody(BaseModel):
    action: str
    name: str | None = None
    domain_code: str | None = None
    stage_name: str | None = None
    class_name: str | None = None
    category_code: str | None = None
    atom_code: str | None = None

    def to_payload(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if v is not None}


@router.get("/tasks/{task_id}/suggestions")
def task_suggestions(task_id: int) -> dict:
    init_db()
    conn = get_connection()
    try:
        task = conn.execute(
            "SELECT id FROM comparison_task WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"suggestions": list_suggestions(conn, task_id)}
    finally:
        conn.close()


@router.post("/suggestions/{suggestion_id}/resolve")
def resolve_endpoint(suggestion_id: int, body: ResolveBody) -> dict:
    init_db()
    conn = get_connection()
    try:
        try:
            return resolve_suggestion(conn, suggestion_id, body.action, body.to_payload())
        except CorrectionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except CorrectionError as e:
            raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@router.get("/domains")
def list_domains() -> list[dict]:
    init_db()
    conn = get_connection()
    try:
        return [
            {"code": row["code"], "name": row["name"]}
            for row in conn.execute(
                "SELECT code, name FROM process_domain ORDER BY code"
            )
        ]
    finally:
        conn.close()


@router.get("/stages")
def list_stages() -> list[str]:
    init_db()
    conn = get_connection()
    try:
        return [row["name"] for row in conn.execute("SELECT name FROM process_stage ORDER BY name")]
    finally:
        conn.close()


@router.get("/classes")
def list_classes() -> list[str]:
    init_db()
    conn = get_connection()
    try:
        return [row["name"] for row in conn.execute("SELECT name FROM process_class ORDER BY name")]
    finally:
        conn.close()

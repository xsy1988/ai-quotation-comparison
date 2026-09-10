import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.compare.ai_summary import generate_ai_summary, get_ai_summary
from app.compare.compare_engine import get_comparison
from app.db import get_connection, init_db
from app.llm.client import LLMError
from app.pipeline.pipeline import create_task, run_task

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_TASK_DONE_STATUSES = ("parsed", "failed", "reviewed")
_MAX_POLLS = 3600  # 最长 1 小时，防任务卡死时连接无限挂起


def _task_or_404(conn, task_id: int):
    task = conn.execute(
        "SELECT id, project_name, status, created_at FROM comparison_task WHERE id = ?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def _quote_progress(conn, task_id: int) -> list[dict]:
    return [
        {
            "quote_id": row["id"],
            "supplier_name": row["supplier_name"],
            "parse_status": row["parse_status"],
            "stage": row["stage"],
            "error": _quote_error(row),
        }
        for row in conn.execute(
            """SELECT q.id, q.supplier_name, q.parse_status, q.basic_info,
                      (SELECT pl.stage FROM parse_log pl
                        WHERE pl.quote_id = q.id ORDER BY pl.id DESC LIMIT 1) AS stage
               FROM quote q WHERE q.task_id = ? ORDER BY q.id""",
            (task_id,),
        )
    ]


def _quote_error(row) -> str | None:
    """失败报价的错误信息（_mark_failed 写入 basic_info.error），供前端展示。"""
    if row["parse_status"] != "failed":
        return None
    try:
        return (json.loads(row["basic_info"] or "{}") or {}).get("error")
    except (json.JSONDecodeError, AttributeError):
        return None


@router.post("")
def post_task(
    background_tasks: BackgroundTasks,
    project_name: str = Form(""),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="未上传文件")
    init_db()
    conn = get_connection()
    try:
        uploads = [(f.filename or "未命名文件", f.file.read()) for f in files]
        task_id = create_task(conn, project_name, uploads)
    finally:
        conn.close()
    background_tasks.add_task(run_task, task_id)
    return {"task_id": task_id}


@router.get("")
def list_tasks() -> list[dict]:
    init_db()
    conn = get_connection()
    try:
        return [
            {
                "id": row["id"],
                "project_name": row["project_name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "quote_count": row["quote_count"],
            }
            for row in conn.execute(
                """SELECT t.id, t.project_name, t.status, t.created_at,
                          (SELECT COUNT(*) FROM quote q WHERE q.task_id = t.id) AS quote_count
                   FROM comparison_task t ORDER BY t.id DESC"""
            )
        ]
    finally:
        conn.close()


@router.get("/{task_id}/progress")
def task_progress(task_id: int) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        last_key: str | None = None
        for poll in range(_MAX_POLLS):
            conn = get_connection()
            try:
                task = conn.execute(
                    "SELECT id, status FROM comparison_task WHERE id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    yield f"event: error\ndata: {json.dumps({'detail': '任务不存在'}, ensure_ascii=False)}\n\n"
                    return
                payload = {
                    "task_status": task["status"],
                    "quotes": _quote_progress(conn, task_id),
                }
            finally:
                conn.close()
            key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
            if key != last_key:
                last_key = key
                yield f"event: progress\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if task["status"] in _TASK_DONE_STATUSES:
                yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            if poll > 0 and poll % 30 == 0:
                yield ": heartbeat\n\n"  # 30s 心跳注释行，防代理断连
            await asyncio.sleep(1)
        yield f"event: error\ndata: {json.dumps({'detail': '等待超时'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/{task_id}/comparison")
def task_comparison(task_id: int) -> dict:
    init_db()
    conn = get_connection()
    try:
        _task_or_404(conn, task_id)
        return get_comparison(conn, task_id)
    finally:
        conn.close()


@router.post("/{task_id}/ai-summary")
def generate_task_ai_summary(task_id: int) -> dict:
    init_db()
    conn = get_connection()
    try:
        _task_or_404(conn, task_id)
        try:
            return generate_ai_summary(conn, task_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except LLMError as e:
            # LLM 故障不降级：502 透出原始错误信息，前端 Alert 展示
            raise HTTPException(status_code=502, detail=str(e))
    finally:
        conn.close()


@router.get("/{task_id}/ai-summary")
def task_ai_summary(task_id: int) -> dict:
    init_db()
    conn = get_connection()
    try:
        _task_or_404(conn, task_id)
        return {"summary": get_ai_summary(conn, task_id)}
    finally:
        conn.close()


@router.get("/{task_id}/quotes/{quote_id}/snapshot")
def quote_snapshot(task_id: int, quote_id: int) -> dict:
    init_db()
    conn = get_connection()
    try:
        _task_or_404(conn, task_id)
        row = conn.execute(
            "SELECT raw_json_path FROM quote WHERE id = ? AND task_id = ?",
            (quote_id, task_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="报价单不存在")
        if not row["raw_json_path"]:
            raise HTTPException(status_code=404, detail="该报价单无解析快照")
        return json.loads(open(row["raw_json_path"], encoding="utf-8").read())
    finally:
        conn.close()

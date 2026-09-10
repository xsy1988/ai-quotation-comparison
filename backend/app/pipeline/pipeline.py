"""任务编排：上传文件落盘 → 建任务 → 逐文件跑 查重→接入→解析→校验→落库→映射。

单文件失败不拖垮整任务：该 quote 标 failed 并写 parse_log，全部结束后 task.status='parsed'。
LLM 故障例外：网关不可用/报错时不降级，标记失败、task.status='failed' 并中止剩余文件。
ingest/persist 自建连接提交；本模块的日志/状态更新走传入 conn 的短事务，避免跨连接写锁竞争。
"""

import json
import sqlite3
import traceback
from pathlib import Path

from app.db import get_connection, init_db
from app.ingest import ParseError as IngestParseError
from app.ingest import ingest_file
from app.ir import IR
from app.llm.client import LLMError
from app.persist import find_quote_by_hash, persist_quote
from app.pipeline.layout_understand import parse_ir_with_llm_traced
from app.pipeline.mapping_runner import run_mapping
from app.pipeline.simple_excel_parse import ParseError
from app.validate.validate import ValidateError, validate_quote_full

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"

FINAL_TASK_STATUS = "parsed"


def create_task(
    conn: sqlite3.Connection, project_name: str, files: list[tuple[str, bytes]]
) -> int:
    """上传文件落盘 data/uploads/<task_id>/，建 task 行（status='parsing'），返回 task_id。

    每个文件同步预建 quote 占位行（parse_status='pending'，supplier_name 记原文件名），
    进度接口从任务创建起即可逐文件展示；quote_id 记入 upload 日志供流水线回填。
    """
    init_db()
    with conn:
        cur = conn.execute(
            "INSERT INTO comparison_task (project_name, status) VALUES (?, 'parsing')",
            (project_name,),
        )
        task_id = cur.lastrowid

    upload_dir = UPLOAD_DIR / str(task_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in files:
        safe_name = Path(filename).name
        path = upload_dir / safe_name
        path.write_bytes(content)
        with conn:
            cur = conn.execute(
                "INSERT INTO quote (task_id, supplier_name, parse_status) VALUES (?, ?, 'pending')",
                (task_id, safe_name),
            )
            conn.execute(
                "INSERT INTO parse_log (task_id, stage, action, detail) VALUES (?, 'task', 'upload', ?)",
                (task_id, json.dumps(
                    {"original_name": safe_name, "path": str(path), "quote_id": cur.lastrowid},
                    ensure_ascii=False,
                )),
            )
    return task_id


def _log(
    conn: sqlite3.Connection,
    task_id: int,
    stage: str,
    action: str,
    detail: dict,
    quote_id: int | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO parse_log (quote_id, task_id, stage, action, detail) VALUES (?, ?, ?, ?, ?)",
            (quote_id, task_id, stage, action, json.dumps(detail, ensure_ascii=False)),
        )


def _mark_failed(
    conn: sqlite3.Connection,
    task_id: int,
    original_name: str,
    stage: str,
    error: Exception,
    quote_id: int | None = None,
) -> int:
    """失败 quote：有占位行则回填（parse_status='failed'、basic_info 记错误），无则新建失败行。"""
    with conn:
        if quote_id is not None:
            conn.execute(
                """UPDATE quote SET parse_status='failed', basic_info=?,
                          updated_at=datetime('now', 'localtime') WHERE id=?""",
                (json.dumps({"error": str(error)}, ensure_ascii=False), quote_id),
            )
        else:
            cur = conn.execute(
                """INSERT INTO quote (task_id, supplier_name, basic_info, parse_status)
                   VALUES (?, ?, ?, 'failed')""",
                (task_id, original_name, json.dumps({"error": str(error)}, ensure_ascii=False)),
            )
            quote_id = cur.lastrowid
        conn.execute(
            "INSERT INTO parse_log (quote_id, task_id, stage, action, detail) VALUES (?, ?, ?, 'failed', ?)",
            (quote_id, task_id, stage, json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False)),
        )
    return quote_id


def _process_file(
    conn: sqlite3.Connection, task_id: int, project_name: str, path: Path, quote_id: int | None = None
) -> dict:
    # ⓪ 查重 + ① 接入（ingest 自建连接提交）
    ingest_result = ingest_file(path)
    file_hash = ingest_result["sha256"]
    _log(conn, task_id, "dedup", ingest_result["status"], ingest_result, quote_id)

    # 同 hash 已有 quote：复用历史解析结果（快照重新派生落库，不重复解析/映射）
    existing_quote_id = find_quote_by_hash(conn, file_hash)
    if existing_quote_id is not None:
        row = conn.execute("SELECT raw_json_path FROM quote WHERE id = ?", (existing_quote_id,)).fetchone()
        data = json.loads(Path(row["raw_json_path"]).read_text(encoding="utf-8"))
        result = persist_quote(
            data, project_name=project_name, task_id=task_id, file_hash=file_hash, quote_id=quote_id
        )
        _log(
            conn, task_id, "dedup", "quote_reused",
            {"reused_from": existing_quote_id}, quote_id or result["quote_id"],
        )
        return {"quote_id": result["quote_id"], "status": "reused", "calc_check": result["calc_check"]}

    # ② 版面解析：LLM 版面理解。网关不可用/报错直接抛 LLMError，由 run_task 中止任务（不降级）
    ir = IR.from_dict(json.loads(Path(ingest_result["ir_path"]).read_text(encoding="utf-8")))
    data, llm_attempts, cross = parse_ir_with_llm_traced(ir, conn=conn)
    _log(
        conn, task_id, "layout", "parsed",
        {"rows": len(ir.tables), "supplier": data["supplier"]["supplier_name"],
         "engine": "llm", "llm_attempts": llm_attempts},
        quote_id,
    )
    _log(conn, task_id, "layout", "cross_check", cross, quote_id)

    # ④ 校验（schema + 勾稽 + 枚举）
    check, flags = validate_quote_full(conn, data)
    _log(conn, task_id, "validate", check, {"flags": flags}, quote_id)

    # ⑤ 落库（persist 自建连接提交；回填本文件占位行）
    result = persist_quote(data, project_name=project_name, task_id=task_id, file_hash=file_hash, quote_id=quote_id)
    qid = quote_id or result["quote_id"]
    _log(
        conn, task_id, "persist", "stored",
        {"quote_id": qid, "calc_check": check, "flags": flags}, qid,
    )

    # ③ 语义映射
    stats = run_mapping(qid, conn)
    _log(conn, task_id, "match", "mapped", stats, qid)
    return {"quote_id": qid, "status": "parsed", "calc_check": check, "flags": flags}


def _set_task_status(conn: sqlite3.Connection, task_id: int, status: str) -> None:
    with conn:
        conn.execute(
            "UPDATE comparison_task SET status = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (status, task_id),
        )


def run_task(task_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """逐文件跑流水线；单文件失败标 failed 不中断；全部结束 task.status='parsed'。
    LLM 故障（网关不可用/报错）不降级：该文件标 failed、task.status='failed'、中止剩余文件。"""
    own = conn is None
    if own:
        init_db()
        conn = get_connection()
    results: list[dict] = []
    try:
        task = conn.execute(
            "SELECT id, project_name FROM comparison_task WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise ValueError(f"任务 {task_id} 不存在")

        uploads = [
            json.loads(row["detail"])
            for row in conn.execute(
                "SELECT detail FROM parse_log WHERE task_id = ? AND stage = 'task' AND action = 'upload' ORDER BY id",
                (task_id,),
            )
        ]
        for upload in uploads:
            path = Path(upload["path"])
            placeholder_id = upload.get("quote_id")  # create_task 预建占位行；旧日志无此字段则为 None
            try:
                results.append(_process_file(conn, task["id"], task["project_name"], path, placeholder_id))
            except LLMError as e:  # LLM 故障：不降级，中止整个任务并反馈错误
                quote_id = _mark_failed(conn, task_id, upload["original_name"], "layout", e, placeholder_id)
                _log(
                    conn, task_id, "layout", "llm_error",
                    {"error": str(e), "type": type(e).__name__,
                     "attempts": getattr(e, "attempts", None)}, quote_id,
                )
                results.append({"quote_id": quote_id, "status": "failed", "error": str(e)})
                _set_task_status(conn, task_id, "failed")
                return {"task_id": task_id, "task_status": "failed", "results": results,
                        "error": f"LLM 服务不可用，任务已中止：{e}"}
            except (ParseError, IngestParseError, ValidateError, ValueError, KeyError, json.JSONDecodeError) as e:
                quote_id = _mark_failed(conn, task_id, upload["original_name"], "parse", e, placeholder_id)
                results.append({"quote_id": quote_id, "status": "failed", "error": str(e)})
            except Exception as e:  # 兜底：单文件异常不拖垮整任务
                quote_id = _mark_failed(conn, task_id, upload["original_name"], "parse", e, placeholder_id)
                _log(
                    conn, task_id, "parse", "unexpected_error",
                    {"traceback": traceback.format_exc()}, quote_id,
                )
                results.append({"quote_id": quote_id, "status": "failed", "error": str(e)})

        _set_task_status(conn, task_id, FINAL_TASK_STATUS)
        return {"task_id": task_id, "task_status": FINAL_TASK_STATUS, "results": results}
    finally:
        if own:
            conn.close()

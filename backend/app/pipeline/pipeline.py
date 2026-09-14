"""任务编排：上传文件落盘 → 建任务 → 逐文件跑 查重→接入→解析→派生→校验→落库→映射。

文件级并发：run_task 用 ThreadPoolExecutor（max_workers=PARSE_CONCURRENCY）并发处理各文件，
每个 worker 线程在 _process_one 内自建 sqlite 连接（finally 关闭），不跨线程共享连接
（sqlite3 默认 check_same_thread=True）；主线程连接只用于读任务/上传清单与写终态。
并发写锁由 WAL + busy_timeout 兜底（db.get_connection）。

单文件失败不拖垮整任务：该 quote 标 failed 并写 parse_log，继续剩余文件；LLM 故障
（网关不可用/解析失败）按同等级隔离处理，不中止任务。全部结束后：任一文件成功
task.status='parsed'，全部失败 'failed'。
ingest/persist 自建连接提交；本模块的日志/状态更新走所在线程连接的短事务，避免跨连接写锁竞争。
"""

import json
import os
import re
import sqlite3
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import settings
from app.db import get_connection, init_db
from app.derive import derive_offer
from app.ingest import ParseError as IngestParseError
from app.ingest import ingest_file
from app.ir import IR
from app.llm.client import LLMError
from app.persist import load_envelope, persist_quote, save_envelope
from app.pipeline.layout_understand import parse_ir_with_llm_traced
from app.pipeline.mapping_runner import run_mapping
from app.pipeline.simple_excel_parse import ParseError
from app.pipeline.verify_runner import run_verify_shadow
from app.validate.validate import ValidateError, validate_quote_full
from app.validate.validators import validate_l2_reconcile

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"


def _auto_project_name(filenames: list[str]) -> str:
    """任务自动命名：取报价文件名（去扩展名）的公共前缀（通常是零件名）；
    无公共前缀时用首个文件名 + 家数。用户再结合创建时间/任务 ID 区分历史任务。"""
    stems = [re.sub(r"\.(xlsx|pdf)$", "", f, flags=re.IGNORECASE).strip() for f in filenames]
    stems = [s for s in stems if s] or ["未命名报价"]
    prefix = os.path.commonprefix(stems).strip("-_—–·. ")
    if prefix:
        return prefix
    return f"{stems[0]} 等{len(stems)}家" if len(stems) > 1 else stems[0]


def create_task(
    conn: sqlite3.Connection, project_name: str, files: list[tuple[str, bytes]]
) -> int:
    """上传文件落盘 data/uploads/<task_id>/，建 task 行（status='parsing'），返回 task_id。

    project_name 为空时按文件名自动命名。每个文件同步预建 quote 占位行
    （parse_status='pending'，supplier_name 记原文件名），进度接口从任务创建起即可逐文件展示；
    quote_id 记入 upload 日志供流水线回填。
    """
    init_db()
    project_name = project_name.strip() or _auto_project_name([name for name, _ in files])
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


def _verify_enabled() -> bool:
    """LLM-B 影子复核开关：配置项 LLM_B_VERIFY，默认开，"0" 关闭。"""
    return settings.llm_b_verify


def _run_verify_shadow_all(
    conn: sqlite3.Connection,
    task_id: int,
    offers: list[dict],
    ir,
    quote_id: int | None = None,
    chat_fn=None,
) -> None:
    """逐 offer 跑 LLM-B 影子复核并写 parse_log（stage="verify", action="shadow"）。

    与脚本侧 L2 勾稽问题（blame=B，derive 修正后口径）并排记录，积累"脚本 vs LLM-B"
    判例；verify 自身承诺不抛异常，这里再套一层防御，保证单文件异常隔离不破。
    """
    for i, offer in enumerate(offers):
        try:
            result = run_verify_shadow(offer, ir, conn=conn, chat_fn=chat_fn)
        except Exception as e:  # 防御：影子模式绝不拖垮流水线
            result = {"verdict": "skipped", "reason": f"unexpected {type(e).__name__}: {e}"}
        if result is None:
            continue
        usage = result.get("usage") or {}
        blame_b = validate_l2_reconcile({"offers": [offer]})
        _log(
            conn, task_id, "verify", "shadow",
            {
                "quote_offer_index": i,
                "blame_b_issues": [issue.to_dict() for issue in blame_b],
                "llm_b": result,
                "model": usage.get("model"),
                "elapsed_ms": usage.get("elapsed_ms"),
            },
            quote_id,
        )


def _process_file(
    conn: sqlite3.Connection, task_id: int, project_name: str, path: Path, quote_id: int | None = None
) -> dict:
    # ⓪ 查重 + ① 接入（ingest 自建连接提交）
    ingest_result = ingest_file(path)
    file_hash = ingest_result["sha256"]
    _log(conn, task_id, "dedup", ingest_result["status"], ingest_result, quote_id)

    # 同 hash 已有信封存档：复用历史解析结果（不重复 LLM 解析/校验），
    # 但映射结果不存信封（映射依赖库内原子主数据，会随主数据更新变化），故仍需逐 offer 重新映射；
    # 派生重算同样不能省：信封是 derive 之前的原始 LLM 输出（total 可能是 LLM 算错的值），
    # 不带 IR 的保守重算会保留这个错值，必须带 IR 复算（与正常路径同一口径）
    envelope = load_envelope(file_hash)
    if envelope is not None:
        ir = IR.from_dict(json.loads(Path(ingest_result["ir_path"]).read_text(encoding="utf-8")))
        offers = envelope.get("offers") or []
        for offer in offers:
            derive_offer(offer, ir=ir)
        quote_ids: list[int] = []
        for i, offer in enumerate(offers):
            result = persist_quote(
                offer, project_name=project_name, task_id=task_id, file_hash=file_hash,
                quote_id=quote_id if i == 0 else None,
            )
            quote_ids.append(result["quote_id"])
        _log(
            conn, task_id, "dedup", "quote_reused",
            {"reused_from_hash": file_hash, "offers": len(offers)}, quote_ids[0] if quote_ids else quote_id,
        )
        stats = _map_quotes(conn, task_id, quote_ids, quote_id)
        return {
            "quote_ids": quote_ids, "quote_id": quote_ids[0] if quote_ids else quote_id,
            "status": "reused", "calc_check": result["calc_check"], "offers": len(offers),
        }

    # ② 版面解析：LLM 版面理解。网关不可用/报错抛 LLMError，由 _process_one 按单文件失败隔离（继续剩余文件）
    ir = IR.from_dict(json.loads(Path(ingest_result["ir_path"]).read_text(encoding="utf-8")))
    # 进度事件：LLM 解析耗时长（40s×N 轮），进入即写 layout/started，每轮重试写 layout/retry_round，
    # 否则前端进度在"接入"节点长时间无动静
    _log(conn, task_id, "layout", "started", {"file": path.name, "rows": len(ir.tables)}, quote_id)

    def _on_layout_retry(info: dict) -> None:
        _log(conn, task_id, "layout", "retry_round", info, quote_id)

    envelope, llm_attempts, cross = parse_ir_with_llm_traced(ir, conn=conn, progress_cb=_on_layout_retry)
    offers = envelope.get("offers") or []
    save_envelope(file_hash, envelope)
    _log(
        conn, task_id, "layout", "parsed",
        {"rows": len(ir.tables), "offers": len(offers),
         "supplier": offers[0]["supplier"]["supplier_name"] if offers else None,
         "engine": "llm", "llm_attempts": llm_attempts},
        quote_id,
    )
    _log(conn, task_id, "layout", "cross_check", cross, quote_id)

    # ③ 派生重算（脚本确定性规则修正 LLM 算术：共享单元格去重/模块 total/税费/summary），
    # 幂等，persist 落库前会再跑一次（保守路径，不带 IR）
    for offer in offers:
        derive_offer(offer, ir=ir)

    # ③½ LLM-B 核算复核（影子模式，只记录不生效）：derive 之后、validate 之前；
    # LLM_B_VERIFY=0 时跳过
    if _verify_enabled():
        _log(conn, task_id, "verify", "started", {"offers": len(offers)}, quote_id)
        _run_verify_shadow_all(conn, task_id, offers, ir, quote_id)

    # ④ 校验（schema + 勾稽 + 枚举），逐 offer 独立校验
    checks: list[str] = []
    all_flags: list[str] = []
    for offer in offers:
        check, flags = validate_quote_full(conn, offer)
        checks.append(check)
        all_flags.extend(flags)
    _log(conn, task_id, "validate", "/".join(checks) or "unchecked", {"flags": all_flags}, quote_id)

    # ⑤ 落库（persist 自建连接提交；第 1 个 offer 回填本文件占位行，其余新增 quote 行）
    quote_ids = []
    calc_check = checks[0] if checks else "unchecked"
    flags: list[str] = []
    for i, offer in enumerate(offers):
        result = persist_quote(
            offer, project_name=project_name, task_id=task_id, file_hash=file_hash,
            quote_id=quote_id if i == 0 else None,
        )
        quote_ids.append(result["quote_id"])
        calc_check = result["calc_check"]
        flags = result["flags"]
    qid = quote_ids[0] if quote_ids else quote_id
    _log(
        conn, task_id, "persist", "stored",
        {"quote_ids": quote_ids, "calc_check": calc_check, "flags": flags}, qid,
    )

    # ③ 语义映射（逐 offer 独立映射）
    stats = _map_quotes(conn, task_id, quote_ids, qid)
    return {
        "quote_ids": quote_ids, "quote_id": qid, "status": "parsed",
        "calc_check": calc_check, "flags": flags, "offers": len(offers),
    }


def _map_quotes(conn: sqlite3.Connection, task_id: int, quote_ids: list[int], qid: int | None) -> dict:
    """语义映射阶段：逐 offer 跑 L1/L2 匹配并把原子归属写回 quote_line。"""
    _log(conn, task_id, "mapping", "started", {"offers": len(quote_ids)}, qid)
    stats: dict = {}
    for offer_qid in quote_ids:
        stats = run_mapping(offer_qid, conn)
    _log(conn, task_id, "match", "mapped", stats, qid)
    return stats


def _set_task_status(conn: sqlite3.Connection, task_id: int, status: str) -> None:
    with conn:
        conn.execute(
            "UPDATE comparison_task SET status = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            (status, task_id),
        )


def _process_one(task_id: int, project_name: str, upload: dict) -> dict:
    """单文件流水线 worker（ThreadPoolExecutor 线程内执行）：自建 sqlite 连接（finally 关闭），
    单文件失败（含 LLM 故障）隔离为 failed 结果并落库，不向线程外抛异常。"""
    conn = get_connection()
    try:
        path = Path(upload["path"])
        placeholder_id = upload.get("quote_id")  # create_task 预建占位行；旧日志无此字段则为 None
        try:
            return _process_file(conn, task_id, project_name, path, placeholder_id)
        except LLMError as e:  # LLM 故障：与单文件异常同等隔离，标失败并继续剩余文件
            quote_id = _mark_failed(conn, task_id, upload["original_name"], "layout", e, placeholder_id)
            _log(
                conn, task_id, "layout", "llm_error",
                {"error": str(e), "type": type(e).__name__,
                 "attempts": getattr(e, "attempts", None)}, quote_id,
            )
            return {"quote_id": quote_id, "status": "failed", "error": str(e)}
        except (ParseError, IngestParseError, ValidateError, ValueError, KeyError, json.JSONDecodeError) as e:
            quote_id = _mark_failed(conn, task_id, upload["original_name"], "parse", e, placeholder_id)
            # 栈也入档：这类错误此前只留一行 str(e)，事后无法定位（如 "could not convert string to float: ''"）
            _log(
                conn, task_id, "parse", f"{type(e).__name__}", {"traceback": traceback.format_exc()},
                quote_id,
            )
            return {"quote_id": quote_id, "status": "failed", "error": str(e)}
        except Exception as e:  # 兜底：单文件异常不拖垮整任务
            quote_id = _mark_failed(conn, task_id, upload["original_name"], "parse", e, placeholder_id)
            _log(
                conn, task_id, "parse", "unexpected_error",
                {"traceback": traceback.format_exc()}, quote_id,
            )
            return {"quote_id": quote_id, "status": "failed", "error": str(e)}
    finally:
        conn.close()


def run_task(task_id: int, conn: sqlite3.Connection | None = None) -> dict:
    """并发跑各文件流水线（max_workers=PARSE_CONCURRENCY）；单文件失败（含 LLM 故障）
    标 failed 不中断；全部 future 完成后收尾：任一文件成功 task.status='parsed'，全部失败 'failed'。

    主线程 conn 只读任务/上传清单、写任务终态；worker 线程在 _process_one 内自建连接。
    results 按上传顺序回填（与并发完成顺序无关），保持结果可对应原文件。"""
    init_db()  # 主线程先初始化（建表/迁移/WAL），worker 线程随后各自建连
    own = conn is None
    if own:
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
        results = [{}] * len(uploads)
        with ThreadPoolExecutor(
            max_workers=settings.parse_concurrency, thread_name_prefix="parse"
        ) as pool:
            futures = {
                pool.submit(_process_one, task["id"], task["project_name"], upload): i
                for i, upload in enumerate(uploads)
            }
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:  # 防御：_process_one 已兜底，此处理论不可达
                    results[i] = {
                        "quote_id": uploads[i].get("quote_id"),
                        "status": "failed", "error": str(e),
                    }

        # 收尾：任一文件成功（parsed/reused）→ parsed；全部失败 → failed
        final_status = "parsed" if any(r.get("status") in ("parsed", "reused") for r in results) else "failed"
        _set_task_status(conn, task_id, final_status)
        return {"task_id": task_id, "task_status": final_status, "results": results}
    finally:
        if own:
            conn.close()

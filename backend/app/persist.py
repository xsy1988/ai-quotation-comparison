"""落库（流水线段⑤）：quote_schema JSON → 派生重算 → 校验 → 快照存档 → 拍平写关系表。

校验函数（calc_check/items_sum/module_total/validate_quote）已迁至 app.validate.validate，
此处 re-export 保持旧引用兼容。所有落库路径（流水线解析/查重复用/人工修正）均先经
app.derive 确定性重算（幂等），派生发现的冲突合并进 quote.flags。
"""

import json
from pathlib import Path
import sqlite3

from .db import get_connection, init_db
from .derive import derive_offer
from .validate.validate import (  # noqa: F401  (re-export)
    calc_check,
    items_sum,
    load_envelope_schema,
    load_schema,
    module_total,
    validate_quote,
)

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "data" / "snapshots"

MODULES = ("materials", "processing", "inspection", "packaging_transport", "sga_tax", "other")

CONFIDENCE_MAP = {"high": "high", "mid": "medium", "low": "low"}
MATCH_PATH_MAP = {"alias_exact": "L1_alias", "llm_semantic": "L2_llm", "manual": "manual"}


class PersistError(Exception):
    pass


def find_quote_by_hash(conn: sqlite3.Connection, file_hash: str) -> int | None:
    """查重：同 hash 已有 quote 则返回其 id（幂等复用），否则 None。"""
    row = conn.execute(
        "SELECT id FROM quote WHERE file_hash = ? ORDER BY id LIMIT 1", (file_hash,)
    ).fetchone()
    return row[0] if row else None


def envelope_path(file_hash: str) -> Path:
    return SNAPSHOT_DIR / f"envelope_{file_hash}.json"


def save_envelope(file_hash: str, envelope: dict) -> str:
    """信封（offers 列表）存档：同 hash 文件重复上传时直接复用，不重复解析。"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = envelope_path(file_hash)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def load_envelope(file_hash: str) -> dict | None:
    """查重复用：信封存在则返回（含 offers 列表），否则 None。"""
    path = envelope_path(file_hash)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _insert_quote_lines(conn, quote_id: int, module: str, items: list[dict]) -> int:
    count = 0
    for item in items or []:
        conn.execute(
            """INSERT INTO quote_line
               (quote_id, module, item_name, item_type, amount, rate,
                atom_code, is_new_process, bundle_flag, candidate_atoms, fingerprint,
                confidence, match_path, confirm_status, note, evidence, cross_check)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                quote_id,
                module,
                item.get("name"),
                item.get("item_type"),
                item.get("amount_per_pc"),
                item.get("rate"),
                item.get("atom_code"),
                int(bool(item.get("is_new_process"))),
                int(bool(item.get("bundle_flag"))),
                json.dumps(item.get("bundle_members"), ensure_ascii=False) if item.get("bundle_members") else None,
                item.get("bundle_fingerprint"),
                CONFIDENCE_MAP.get(item.get("confidence", ""), item.get("confidence")),
                MATCH_PATH_MAP.get(item.get("match_path") or "", item.get("match_path")),
                item.get("confirm_status", "unconfirmed"),
                item.get("note"),
                json.dumps(item.get("evidence"), ensure_ascii=False) if item.get("evidence") else None,
                json.dumps(item.get("_cross_check"), ensure_ascii=False) if item.get("_cross_check") else None,
            ),
        )
        count += 1
    return count


def _insert_tooling_lines(conn, quote_id: int, tooling: dict | None) -> int:
    if not tooling:
        return 0
    count = 0
    for tooling_type, key in (("mold", "molds"), ("fixture", "fixtures"), ("stencil", "stencils")):
        for item in (tooling.get(key) or {}).get("items") or []:
            conn.execute(
                """INSERT INTO tooling_line (quote_id, tooling_type, item_name, amount, cavity_count, lifespan, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    quote_id,
                    tooling_type,
                    item.get("name"),
                    item.get("amount"),
                    item.get("cavities"),
                    item.get("lifespan"),
                    item.get("note"),
                ),
            )
            count += 1
    return count


def collect_flags(data: dict, check: str) -> list[str]:
    """校验徽标（只打标不阻断）：勾稽异常 / 低置信度 / 未匹配 / 新工艺候选 / 派生冲突。"""
    flags: list[str] = []
    if check == "fail":
        flags.append("calc_abnormal")
    processing_items = data["unit_price"]["processing"].get("items") or []
    if any(item.get("confidence") == "low" for item in processing_items):
        flags.append("low_confidence")
    if any(item.get("atom_code") is None for item in processing_items):
        flags.append("unmatched")
    if any(item.get("is_new_process") for item in processing_items):
        flags.append("new_process")
    if any(item.get("_cross_check") for item in processing_items):
        flags.append("cross_validation_conflict")
    # derive 重算发现的冲突（模块 total/税费/final 与派生值不符，单据值保留）
    derived = data.get("_derived") or {}
    if derived.get("conflicts"):
        flags.append("cross_validation_conflict")
    if derived.get("shared_cells"):
        flags.append("shared_cell")
    # 共享单元格判不准（未置零，待人工核对）
    if any(c.get("kind") == "shared_cell_ambiguous" for c in derived.get("conflicts") or []):
        flags.append("calc_abnormal")
    # 去重保持顺序稳定
    return list(dict.fromkeys(flags))


def persist_quote(
    data: dict,
    project_name: str | None = None,
    task_id: int | None = None,
    file_hash: str | None = None,
    quote_id: int | None = None,
) -> dict:
    """落库。传 task_id 则归属既有任务（不新建 comparison_task，任务状态由流水线更新）；
    传 file_hash 写入 quote.file_hash 供查重复用；
    传 quote_id 则回填流水线预建的占位行（UPDATE 而非 INSERT，行内明细先清后填）。"""
    # ⓪ 派生重算（脚本确定性规则：模块 total/税费/summary；幂等，双跑无碍），冲突并入 flags
    derive_offer(data)
    validate_quote(data)
    check = calc_check(data)
    flags = collect_flags(data, check)
    up = data["unit_price"]
    basic = data["basic"]
    tooling = data.get("tooling")

    init_db()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        with conn:
            if task_id is None:
                cur = conn.execute(
                    "INSERT INTO comparison_task (project_name, category_code, status) VALUES (?, ?, 'parsed')",
                    (project_name or basic.get("project_name") or basic.get("part_name"), basic.get("category")),
                )
                task_id = cur.lastrowid

            column_values = (
                data["supplier"].get("supplier_code"),
                data["supplier"].get("supplier_name"),
                basic.get("category"),
                json.dumps(basic, ensure_ascii=False),
                up["summary"].get("final_unit_price_taxed"),
                up["summary"].get("untaxed_total"),
                up["summary"].get("tax_amount"),
                up["summary"].get("discount"),
                module_total(up["materials"]),
                module_total(up["processing"]),
                module_total(up["inspection"]),
                module_total(up["packaging_transport"]),
                module_total(up["sga_tax"]),
                module_total(up["other"]),
                module_total(tooling) if tooling and tooling.get("total") is not None
                else (sum(module_total(tooling[k]) or 0 for k in ("molds", "fixtures", "stencils")) if tooling else None),
                file_hash,
                json.dumps(flags, ensure_ascii=False),
                check,
            )
            if quote_id is not None:
                conn.execute(
                    """UPDATE quote SET supplier_code=?, supplier_name=?, category_code=?, basic_info=?,
                               final_unit_price_taxed=?, untaxed_total=?, tax_amount=?, discount=?,
                               materials_total=?, processing_total=?, inspection_total=?,
                               packaging_transport_total=?, sga_tax_total=?, other_total=?, tooling_total=?,
                               file_hash=?, flags=?, parse_status='parsed', calc_check=?,
                               updated_at=datetime('now', 'localtime') WHERE id=?""",
                    (*column_values, quote_id),
                )
                # 占位行重填：清掉旧明细（若有）再写入，保证幂等
                conn.execute("DELETE FROM quote_line WHERE quote_id = ?", (quote_id,))
                conn.execute("DELETE FROM tooling_line WHERE quote_id = ?", (quote_id,))
            else:
                cur = conn.execute(
                    """INSERT INTO quote
                       (task_id, supplier_code, supplier_name, category_code, basic_info,
                        final_unit_price_taxed, untaxed_total, tax_amount, discount,
                        materials_total, processing_total, inspection_total,
                        packaging_transport_total, sga_tax_total, other_total, tooling_total,
                        raw_json_path, file_hash, flags, parse_status, calc_check)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed', ?)""",
                    (task_id, *column_values[:15], None, *column_values[15:]),
                )
                quote_id = cur.lastrowid

            line_count = 0
            for name in MODULES:
                line_count += _insert_quote_lines(conn, quote_id, name, up[name].get("items"))
            tooling_count = _insert_tooling_lines(conn, quote_id, tooling)

            snapshot_path = SNAPSHOT_DIR / f"quote_{quote_id}.json"
            snapshot_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            conn.execute(
                "UPDATE quote SET raw_json_path = ? WHERE id = ?",
                (str(snapshot_path), quote_id),
            )
        return {
            "task_id": task_id,
            "quote_id": quote_id,
            "quote_lines": line_count,
            "tooling_lines": tooling_count,
            "calc_check": check,
            "flags": flags,
            "source_file": basic.get("source_file"),
            "snapshot_path": str(snapshot_path),
        }
    finally:
        conn.close()

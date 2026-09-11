"""新工艺决策（第 8 步）：扫描清单外新工艺条目 → 建议队列 → 用户决策（新增原子/归并/忽略）。

- sync_suggestions：按 item_name 聚合任务内 is_new_process=1 且未挂原子的加工条目，
  upsert new_atom_suggestion（已有非 pending 状态不动，pending 更新计数），幂等；
- resolve_suggestion：三类决策统一改挂 quote_line（快照按行序 zip 定位同步）、
  别名回流 atom_alias(source='new_process')、flags 重算、建议状态推进。

快照定位与 flags 重算约定复用 correction_service / persist 的既有 helper，不复制实现。
"""

import json
import re
import sqlite3

from app.match.atom_match import make_fingerprint
from app.persist import collect_flags
from app.services.correction_service import (
    CorrectionError,
    CorrectionNotFound,
    _load_snapshot,
    _mark_reviewed,
    _save_snapshot,
    _snapshot_item,
)

ACTIONS = ("create", "merge", "ignore")
RESOLVED_STATUS = {"create": "created", "merge": "merged", "ignore": "ignored"}

CODE_RE = re.compile(r"^AT-[A-Z]+-(\d+)$")


def sync_suggestions(conn: sqlite3.Connection, task_id: int) -> int:
    """聚合任务内新工艺条目并 upsert 建议队列，返回 pending 建议数。幂等。"""
    groups = list(
        conn.execute(
            """SELECT ql.item_name AS item_name, MIN(ql.id) AS line_id, COUNT(*) AS cnt
               FROM quote_line ql
               JOIN quote q ON ql.quote_id = q.id
               WHERE q.task_id = ? AND ql.module = 'processing'
                 AND ql.is_new_process = 1 AND ql.atom_code IS NULL
               GROUP BY ql.item_name""",
            (task_id,),
        )
    )
    for group in groups:
        existing = conn.execute(
            """SELECT ns.id, ns.status FROM new_atom_suggestion ns
               JOIN quote_line ql ON ns.quote_line_id = ql.id
               JOIN quote q ON ql.quote_id = q.id
               WHERE q.task_id = ? AND ns.source_text = ?""",
            (task_id, group["item_name"]),
        ).fetchone()
        if existing is None:
            with conn:
                conn.execute(
                    """INSERT INTO new_atom_suggestion
                       (quote_line_id, source_text, occurrence_count, status)
                       VALUES (?, ?, ?, 'pending')""",
                    (group["line_id"], group["item_name"], group["cnt"]),
                )
        elif existing["status"] == "pending":
            with conn:
                conn.execute(
                    """UPDATE new_atom_suggestion
                       SET quote_line_id = ?, occurrence_count = ? WHERE id = ?""",
                    (group["line_id"], group["cnt"], existing["id"]),
                )
        # 已有非 pending 建议：保持原决策不动
    return conn.execute(
        """SELECT COUNT(*) AS n FROM new_atom_suggestion ns
           JOIN quote_line ql ON ns.quote_line_id = ql.id
           JOIN quote q ON ql.quote_id = q.id
           WHERE q.task_id = ? AND ns.status = 'pending'""",
        (task_id,),
    ).fetchone()["n"]


def list_suggestions(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    """先同步队列，再返回该任务 pending 建议（含最早条目所在报价与金额）。"""
    sync_suggestions(conn, task_id)
    return [
        {
            "id": row["id"],
            "source_text": row["source_text"],
            "suggested_name": row["suggested_name"],
            "suggested_domain_code": row["suggested_domain_code"],
            "suggested_stage_name": row["suggested_stage_name"],
            "occurrence_count": row["occurrence_count"],
            "quote_line_id": row["quote_line_id"],
            "quote_id": row["quote_id"],
            "amount": row["amount"],
            "supplier_name": row["supplier_name"],
        }
        for row in conn.execute(
            """SELECT ns.id, ns.source_text, ns.suggested_name, ns.suggested_domain_code,
                      ns.suggested_stage_name, ns.occurrence_count, ns.quote_line_id,
                      ql.quote_id, ql.amount, q.supplier_name
               FROM new_atom_suggestion ns
               JOIN quote_line ql ON ns.quote_line_id = ql.id
               JOIN quote q ON ql.quote_id = q.id
               WHERE q.task_id = ? AND ns.status = 'pending'
               ORDER BY ns.id""",
            (task_id,),
        )
    ]


def _check_ref(conn: sqlite3.Connection, table: str, key_col: str, value: str, label: str) -> None:
    row = conn.execute(
        f"SELECT 1 FROM {table} WHERE {key_col} = ?", (value,)
    ).fetchone()
    if row is None:
        raise CorrectionError(f"{label}不存在：{value}")


def _next_atom_code(conn: sqlite3.Connection, domain_code: str) -> str:
    """域内顺序号 = 现有最大 + 1，3 位补零（AT-{域码}-{序号})。"""
    max_seq = 0
    for row in conn.execute(
        "SELECT code FROM atom WHERE domain_code = ?", (domain_code,)
    ):
        match = CODE_RE.match(row["code"])
        if match:
            max_seq = max(max_seq, int(match.group(1)))
    return f"AT-{domain_code}-{max_seq + 1:03d}"


def _insert_atom_with_retry(conn: sqlite3.Connection, domain_code: str, payload: dict) -> str:
    """生成域内新编码并插入原子；并发撞号（IntegrityError）时重新取号再试一次。"""
    for _attempt in range(2):
        code = _next_atom_code(conn, domain_code)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO atom (code, name, domain_code, stage_name, class_name)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        code,
                        payload["name"],
                        domain_code,
                        payload["stage_name"],
                        payload["class_name"],
                    ),
                )
            return code
        except sqlite3.IntegrityError:
            continue
    raise CorrectionError(f"原子编码生成冲突，请重试：{domain_code}")


def _insert_alias(conn: sqlite3.Connection, atom_code: str, source_text: str) -> None:
    """别名回流（UNIQUE(atom_code, alias_text) 冲突静默跳过）。"""
    try:
        with conn:
            conn.execute(
                """INSERT INTO atom_alias (atom_code, alias_text, source)
                   VALUES (?, ?, 'new_process')""",
                (atom_code, source_text),
            )
    except sqlite3.IntegrityError:
        pass


def _matching_lines(conn: sqlite3.Connection, source_text: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT id, quote_id, module FROM quote_line
               WHERE module = 'processing' AND is_new_process = 1 AND item_name = ?
               ORDER BY quote_id, id""",
            (source_text,),
        )
    )


def _relink_lines(conn: sqlite3.Connection, lines: list[sqlite3.Row], atom_code: str | None) -> None:
    """create/merge：改挂新原子；ignore：仅清 is_new_process，原子保持空。"""
    if atom_code is not None:
        fingerprint = make_fingerprint([atom_code])
        members = json.dumps([atom_code], ensure_ascii=False)
        for line in lines:
            with conn:
                conn.execute(
                    """UPDATE quote_line SET atom_code = ?, match_path = 'manual',
                       confidence = 'high', is_new_process = 0, bundle_flag = 0,
                       candidate_atoms = ?, fingerprint = ?, confirm_status = 'corrected'
                       WHERE id = ?""",
                    (atom_code, members, fingerprint, line["id"]),
                )
    else:
        for line in lines:
            with conn:
                conn.execute(
                    "UPDATE quote_line SET is_new_process = 0 WHERE id = ?", (line["id"],)
                )


def _sync_snapshots(conn: sqlite3.Connection, lines: list[sqlite3.Row], atom_code: str | None) -> None:
    """受影响报价的快照按行序 zip 定位同步条目并重算 flags。"""
    by_quote: dict[int, list[sqlite3.Row]] = {}
    for line in lines:
        by_quote.setdefault(line["quote_id"], []).append(line)

    for quote_id, quote_lines in by_quote.items():
        data, path = _load_snapshot(conn, quote_id)
        for line in quote_lines:
            item, _idx = _snapshot_item(conn, data, quote_id, line["module"], line["id"])
            if atom_code is not None:
                fingerprint = make_fingerprint([atom_code])
                item.update(
                    {
                        "atom_code": atom_code,
                        "match_path": "manual",
                        "confidence": "high",
                        "confirm_status": "corrected",
                        "bundle_members": [atom_code],
                        "bundle_fingerprint": fingerprint,
                        "bundle_flag": False,
                        "split_method": "none",
                        "is_new_process": False,
                    }
                )
            else:
                item["is_new_process"] = False
        check = conn.execute(
            "SELECT calc_check FROM quote WHERE id = ?", (quote_id,)
        ).fetchone()["calc_check"]
        flags = collect_flags(data, check or "unchecked")
        with conn:
            conn.execute(
                "UPDATE quote SET flags = ? WHERE id = ?",
                (json.dumps(flags, ensure_ascii=False), quote_id),
            )
        _mark_reviewed(conn, quote_id, data)
        _save_snapshot(conn, quote_id, data, path)


def resolve_suggestion(
    conn: sqlite3.Connection, suggestion_id: int, action: str, payload: dict
) -> dict:
    """新工艺建议决策：create 新增原子 / merge 归并现有 / ignore 忽略。"""
    suggestion = conn.execute(
        "SELECT id, source_text, status FROM new_atom_suggestion WHERE id = ?",
        (suggestion_id,),
    ).fetchone()
    if suggestion is None:
        raise CorrectionNotFound("新工艺建议不存在")
    if action not in ACTIONS:
        raise CorrectionError(f"action 非法：{action}（可选 {ACTIONS}）")

    source_text = suggestion["source_text"]
    atom_code: str | None = None

    if action == "create":
        for field in ("name", "domain_code", "stage_name", "class_name"):
            if not payload.get(field):
                raise CorrectionError(f"缺少必填字段：{field}")
        domain_code = payload["domain_code"]
        _check_ref(conn, "process_domain", "code", domain_code, "工艺域")
        _check_ref(conn, "process_stage", "name", payload["stage_name"], "工艺阶段")
        _check_ref(conn, "process_class", "name", payload["class_name"], "工艺类别")
        category_code = payload.get("category_code")
        if category_code:
            _check_ref(conn, "category", "code", category_code, "品类")

        atom_code = _insert_atom_with_retry(conn, domain_code, payload)
        _insert_alias(conn, atom_code, source_text)
        if category_code:
            with conn:
                conn.execute(
                    """INSERT INTO atom_category (atom_code, category_code)
                       VALUES (?, ?)""",
                    (atom_code, category_code),
                )
    elif action == "merge":
        atom_code = payload.get("atom_code")
        if not atom_code:
            raise CorrectionError("缺少必填字段：atom_code")
        _check_ref(conn, "atom", "code", atom_code, "原子")
        _insert_alias(conn, atom_code, source_text)

    lines = _matching_lines(conn, source_text)
    _relink_lines(conn, lines, atom_code)
    if lines:
        _sync_snapshots(conn, lines, atom_code)

    with conn:
        conn.execute(
            "UPDATE new_atom_suggestion SET status = ? WHERE source_text = ?",
            (RESOLVED_STATUS[action], source_text),
        )

    result: dict = {"suggestion_id": suggestion_id, "action": action}
    if atom_code is not None:
        result["atom_code"] = atom_code
    return result

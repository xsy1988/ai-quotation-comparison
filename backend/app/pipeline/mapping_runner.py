"""映射回写编排：读快照 JSON → 加工费跑 L1、L2（LLM 语义）兜底、包运/损管跑 fee_classify → 回写 quote_line 与快照。

定位约定：persist 按 MODULES 顺序、模块内按 items 数组顺序插入 quote_line，
因此同 (quote_id, module) 下按 id 排序的行序与 JSON items 序一致（代码内断言防错位）。
"""

import json
import sqlite3
from typing import Any

from app import prompts
from app.db import get_connection, init_db
from app.llm import client as llm_client
from app.match.atom_match import load_lexicon, make_fingerprint, match_l1
from app.match.fee_classify import classify_item_type
from app.persist import CONFIDENCE_MAP, MATCH_PATH_MAP, MODULES, collect_flags

FEE_CLASSIFY_MODULES = ("packaging_transport", "sga_tax")


def _load_snapshot(conn: sqlite3.Connection, quote_id: int) -> tuple[dict, str, str | None, str | None]:
    row = conn.execute(
        "SELECT raw_json_path, category_code, calc_check FROM quote WHERE id = ?", (quote_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"quote {quote_id} 不存在")
    path = row["raw_json_path"]
    data = json.loads(open(path, encoding="utf-8").read())
    return data, path, row["category_code"], row["calc_check"]


def _line_rows(conn: sqlite3.Connection, quote_id: int, module: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id FROM quote_line WHERE quote_id = ? AND module = ? ORDER BY id",
            (quote_id, module),
        )
    )


def _quote_task_id(conn: sqlite3.Connection, quote_id: int) -> int | None:
    row = conn.execute("SELECT task_id FROM quote WHERE id = ?", (quote_id,)).fetchone()
    return row["task_id"] if row else None


def _l2_candidates(conn: sqlite3.Connection, category_code: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT a.code, a.name FROM atom a
               JOIN atom_category ac ON a.code = ac.atom_code
               WHERE ac.category_code = ? ORDER BY a.code""",
            (category_code,),
        )
    )


def _l2_llm_match(
    conn: sqlite3.Connection,
    quote_id: int,
    task_id: int | None,
    category_code: str,
    items: list[tuple[int, dict]],
) -> dict[int, Any]:
    """一次 LLM 调用批量匹配，返回 {条目在 items 中的下标: 编码/编码列表/null}。"""
    candidates = _l2_candidates(conn, category_code)
    cand_lines = "\n".join(f"[{i}] {r['code']} {r['name']}" for i, r in enumerate(candidates))
    item_lines = "\n".join(
        f"[{i}] 名称：{item.get('name') or ''}"
        + (f"；备注：{item['note']}" if item.get("note") else "")
        for i, (_idx, item) in enumerate(items)
    )
    messages = prompts.build_messages(
        "match_atoms",
        {"category": category_code, "candidates": cand_lines, "items": item_lines},
    )
    parsed, usage = llm_client.chat_json(messages)
    with conn:
        conn.execute(
            "INSERT INTO parse_log (quote_id, task_id, stage, action, detail, is_llm_call) VALUES (?, ?, 'match', 'l2_llm_match', ?, 1)",
            (
                quote_id,
                task_id,
                json.dumps(
                    {"category": category_code, "items": len(items),
                     "tokens": usage.get("total_tokens"), "elapsed_ms": usage.get("elapsed_ms"),
                     "model": usage.get("model")},
                    ensure_ascii=False,
                ),
            ),
        )
    raw_matches = parsed.get("matches") if isinstance(parsed, dict) else None
    if not isinstance(raw_matches, dict):
        return {}
    out: dict[int, Any] = {}
    for key, value in raw_matches.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(items):
            out[idx] = value
    return out


def _apply_l2_result(item: dict, value: Any, cand_codes: set[str], stats: dict[str, Any]) -> None:
    """按 LLM 匹配结果回填条目：唯一命中 / 多编码 bundle / 清单外新工艺三分支。"""
    if isinstance(value, str):
        codes = [value]
    elif isinstance(value, list):
        codes = [c for c in value if isinstance(c, str)]
    else:
        codes = []
    valid = [c for c in codes if c in cand_codes]
    if len(valid) == 1 and isinstance(value, str):
        item["atom_code"] = valid[0]
        item["confidence"] = "mid"
        item["match_path"] = "llm_semantic"
        item["bundle_members"] = [valid[0]]
        item["bundle_fingerprint"] = make_fingerprint([valid[0]])
        stats["l2_matched"] += 1
    elif len(valid) >= 2:
        item["bundle_flag"] = True
        item["bundle_members"] = sorted(valid)
        item["bundle_fingerprint"] = make_fingerprint(sorted(valid))
        item["split_method"] = "none"  # 打包行不拆金额：整行金额随 bundle 计入合计
        item["confidence"] = "mid"
        item["match_path"] = "llm_semantic"
        stats["l2_matched"] += 1
    else:
        # LLM 返回 null 或清单外编码：atom_code 留空 + 新工艺上报
        item["atom_code"] = None
        item["is_new_process"] = True
        item["confidence"] = "low"
        item["match_path"] = "llm_semantic"
        stats["l2_new_process"] += 1


def run_mapping(quote_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    if own:
        init_db()
        conn = get_connection()
    stats: dict[str, Any] = {"matched": 0, "ambiguous": 0, "unmatched": 0, "downgraded": 0,
                             "l2_matched": 0, "l2_new_process": 0}
    try:
        data, snapshot_path, category_code, calc_check = _load_snapshot(conn, quote_id)
        lexicon = load_lexicon(conn)
        up = data["unit_price"]

        l2_pending: list[tuple[int, dict]] = []
        for i, item in enumerate(up["processing"].get("items") or []):
            # 已有人工/L2 结果的条目不覆盖
            if item.get("atom_code") or item.get("match_path") in ("manual", "llm_semantic"):
                continue
            result = match_l1(item.get("name") or "", lexicon, category_code, conn)
            item["atom_code"] = result.atom_code
            item["confidence"] = result.confidence
            item["match_path"] = result.match_path if result.atom_code else None
            if result.note:
                item["note"] = result.note
            if result.atom_code:
                members = item.get("bundle_members") or [result.atom_code]
                item["bundle_members"] = members
                item["bundle_fingerprint"] = make_fingerprint(members)
                stats["matched"] += 1
                if result.confidence == "mid":
                    stats["downgraded"] += 1
            else:
                stats["unmatched"] += 1
                if result.note and result.note.startswith("歧义"):
                    stats["ambiguous"] += 1
                l2_pending.append((i, item))

        # L2：LLM 语义匹配兜底（品类未知则跳过，留待人工；LLM 不可达/报错上抛，由 pipeline 按单文件失败隔离）
        if category_code and l2_pending:
            matches = _l2_llm_match(conn, quote_id, _quote_task_id(conn, quote_id), category_code, l2_pending)
            cand_codes = {r["code"] for r in _l2_candidates(conn, category_code)}
            for j, (_i, item) in enumerate(l2_pending):
                _apply_l2_result(item, matches.get(j), cand_codes, stats)

        # L2 已成功匹配（含 bundle）的条目：L1 失败时插入的 unmatched_term 置 resolved；未匹配上的保持 pending
        resolved_terms = [
            item.get("name")
            for _i, item in l2_pending
            if item.get("match_path") == "llm_semantic"
            and not item.get("is_new_process")
            and (item.get("atom_code") or item.get("bundle_flag"))
        ]

        for module in FEE_CLASSIFY_MODULES:
            for item in up[module].get("items") or []:
                classify_item_type(module, item)

        # 回写 quote_line（按插入序 zip）
        with conn:
            for term in resolved_terms:
                conn.execute(
                    """UPDATE unmatched_term SET status = 'resolved',
                          updated_at = datetime('now', 'localtime')
                       WHERE term_text = ? AND status = 'pending'""",
                    (term,),
                )
            for module in MODULES:
                items = up[module].get("items") or []
                rows = _line_rows(conn, quote_id, module)
                assert len(rows) == len(items), f"{module} 行数与 JSON items 数不一致"
                for row, item in zip(rows, items):
                    # DB 存 L1_alias/L2_llm/L3_none 语义，JSON 快照保留 schema 词汇
                    db_match_path = MATCH_PATH_MAP.get(item.get("match_path") or "", item.get("match_path"))
                    conn.execute(
                        """UPDATE quote_line SET item_type = ?, amount = ?, rate = ?,
                           atom_code = ?, confidence = ?, match_path = ?, note = ?,
                           fingerprint = ?, candidate_atoms = ?,
                           is_new_process = ?, bundle_flag = ?
                           WHERE id = ?""",
                        (
                            item.get("item_type"),
                            item.get("amount_per_pc"),
                            item.get("rate"),
                            item.get("atom_code"),
                            CONFIDENCE_MAP.get(item.get("confidence") or "", item.get("confidence")),
                            db_match_path,
                            item.get("note"),
                            item.get("bundle_fingerprint"),
                            json.dumps(item.get("bundle_members"), ensure_ascii=False)
                            if item.get("bundle_members")
                            else None,
                            int(bool(item.get("is_new_process"))),
                            int(bool(item.get("bundle_flag"))),
                            row["id"],
                        ),
                    )
            flags = collect_flags(data, calc_check or "unchecked")
            conn.execute(
                "UPDATE quote SET flags = ? WHERE id = ?",
                (json.dumps(flags, ensure_ascii=False), quote_id),
            )
        json.dumps(data, ensure_ascii=False)  # 防御：序列化失败则不写盘
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        stats["flags"] = flags
        return stats
    finally:
        if own:
            conn.close()

"""一次性迁移：移除兜底原子 AT-QT-001（业务决定：清单外工艺 = atom_code 空 + is_new_process）。

- quote_line.atom_code = 'AT-QT-001' → NULL（is_new_process 保留）；
- 快照 JSON 中 atom_code = 'AT-QT-001' 的条目同步置空（bundle_members/fingerprint 一并清）；
- atom_alias / atom_category / atom 中 AT-QT-001 行删除（先清子表再删主表行）；
- dim_group 成员剔除 AT-QT-001，剔除后无成员的组（builtin:domain:QT 等）整组删除；
- 不再被任何原子引用的 process_domain / process_stage 值一并删除；
- atom_alias 补充 "镭雕破氧白" → AT-YS-027（与导入脚本 EXTRA_ALIASES 同步）。

幂等，可重复执行。用法：.venv/bin/python scripts/migrate_remove_fallback_atom.py [db_path]
"""

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from app.db import get_connection  # noqa: E402
from import_master_data import EXTRA_ALIASES, REMOVED_CODES  # noqa: E402


def _migrate_snapshots(conn) -> int:
    rows = conn.execute(
        "SELECT id, raw_json_path FROM quote WHERE raw_json_path IS NOT NULL"
    ).fetchall()
    touched = 0
    for row in rows:
        path = Path(row["raw_json_path"])
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for item in (data.get("unit_price", {}).get("processing") or {}).get("items") or []:
            if item.get("atom_code") in REMOVED_CODES:
                item["atom_code"] = None
                item.pop("bundle_members", None)
                item.pop("bundle_fingerprint", None)
                changed = True
        if changed:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            touched += 1
    return touched


def main() -> None:
    db_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    conn = get_connection(db_path)
    try:
        with conn:
            placeholders = ",".join("?" for _ in REMOVED_CODES)
            cur = conn.execute(
                "UPDATE quote_line SET atom_code = NULL, updated_at = datetime('now', 'localtime')"
                f" WHERE atom_code IN ({placeholders})",
                tuple(REMOVED_CODES),
            )
            print(f"quote_line 置空：{cur.rowcount}")

            for code in REMOVED_CODES:
                conn.execute("DELETE FROM atom_alias WHERE atom_code = ?", (code,))
                conn.execute("DELETE FROM atom_category WHERE atom_code = ?", (code,))

            removed_groups = 0
            for row in conn.execute("SELECT group_code, member_atoms FROM dim_group").fetchall():
                members = [m for m in json.loads(row["member_atoms"] or "[]") if m not in REMOVED_CODES]
                if len(members) == len(json.loads(row["member_atoms"] or "[]")):
                    continue
                if not members:
                    conn.execute("DELETE FROM dim_group WHERE group_code = ?", (row["group_code"],))
                    removed_groups += 1
                else:
                    conn.execute(
                        "UPDATE dim_group SET member_atoms = ?,"
                        " updated_at = datetime('now', 'localtime') WHERE group_code = ?",
                        (json.dumps(members, ensure_ascii=False), row["group_code"]),
                    )
            print(f"dim_group 剔除成员，空组删除：{removed_groups}")

            for code in REMOVED_CODES:
                conn.execute("DELETE FROM atom WHERE code = ?", (code,))

            conn.execute(
                "DELETE FROM process_domain WHERE NOT EXISTS"
                " (SELECT 1 FROM atom WHERE atom.domain_code = process_domain.code)"
            )
            conn.execute(
                "DELETE FROM process_stage WHERE NOT EXISTS"
                " (SELECT 1 FROM atom WHERE atom.stage_name = process_stage.name)"
            )

            for atom_code, alias in EXTRA_ALIASES:
                exists = conn.execute("SELECT 1 FROM atom WHERE code = ?", (atom_code,)).fetchone()
                if exists:
                    conn.execute(
                        "INSERT OR IGNORE INTO atom_alias (atom_code, alias_text, source)"
                        " VALUES (?, ?, 'initial')",
                        (atom_code, alias),
                    )

        touched = _migrate_snapshots(conn)
        print(f"快照同步：{touched}")
        left = conn.execute(
            f"SELECT COUNT(*) FROM atom WHERE code IN ({placeholders})", tuple(REMOVED_CODES)
        ).fetchone()[0]
        print(f"atoms 表残留：{left}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

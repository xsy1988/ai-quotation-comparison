import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "quotes.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_db_path() -> Path:
    override = os.environ.get("QUOTES_DB_PATH")
    return Path(override) if override else DEFAULT_DB_PATH


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# 存量库缺列迁移：新列在 schema.sql 演进后补上（SQLite 无法修改 CHECK，重建除外）
COLUMN_MIGRATIONS: dict[str, list[str]] = {
    "quote": [
        "ALTER TABLE quote ADD COLUMN supplier_name TEXT",
        "ALTER TABLE quote ADD COLUMN flags TEXT",
    ],
    "quote_line": [
        "ALTER TABLE quote_line ADD COLUMN cross_check TEXT",
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, statements in COLUMN_MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for stmt in statements:
            column = stmt.split("ADD COLUMN")[1].strip().split()[0]
            if column not in existing:
                conn.execute(stmt)


def init_db(db_path: Path | None = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()

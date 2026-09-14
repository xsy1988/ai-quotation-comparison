import sqlite3
from functools import lru_cache
from pathlib import Path

from app.config import settings

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "quotes.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_db_path() -> Path:
    override = settings.quotes_db_path
    return Path(override) if override else DEFAULT_DB_PATH


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """每调用新建连接（并发解析时各 worker 线程一连）。WAL + busy_timeout：
    读写可并发，多线程写冲突时最多等 30s 而非立刻 SQLITE_BUSY。"""
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# 存量库补列：目标结构以 schema.sql 在内存库中跑出的结果为准（手写清单容易漏，
# 例如 schema 里新增列 + 新增引用该列的索引后，旧库会直接启动失败）。
@lru_cache(maxsize=1)
def _schema_columns() -> dict[str, list[tuple[str, str, int, str | None]]]:
    scratch = sqlite3.connect(":memory:")
    try:
        scratch.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        tables = [
            row[0]
            for row in scratch.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not row[0].startswith("sqlite_")
        ]
        return {
            table: [
                (col[1], col[2], col[3], col[4]) for col in scratch.execute(f"PRAGMA table_info({table})")
            ]
            for table in sorted(tables)
        }
    finally:
        scratch.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """把旧库缺的列补齐。必须在 schema.sql 之前执行：schema 里的 CREATE INDEX 会引用这些列，
    而 CREATE TABLE IF NOT EXISTS 对已存在的旧表是空操作，先跑 schema 会 "no such column" 启动失败。"""
    for table, columns in _schema_columns().items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # 新库：表尚不存在，由 schema.sql 一次建全
        for name, ctype, _notnull, default in columns:
            if name in existing:
                continue
            # 旧库补列不继承 NOT NULL：表内已有数据无法回填默认值
            spec = f"{name} {ctype}".strip()
            # SQLite 的 ADD COLUMN 只接受常量默认值，datetime('now') 这类表达式只能放弃
            if default is not None and "(" not in default:
                spec += f" DEFAULT {default}"
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {spec}")
            existing.add(name)


def init_db(db_path: Path | None = None) -> None:
    conn = get_connection(db_path)
    try:
        _migrate(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

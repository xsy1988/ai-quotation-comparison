"""存量库启动迁移：schema 里的新索引引用了新列，补列必须发生在 schema 之前。"""

import sqlite3

from app.db import _schema_columns, init_db


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def test_init_db_on_fresh_path_creates_schema(tmp_path):
    db_path = tmp_path / "fresh.db"
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        quote_columns = _columns(conn, "quote")
        assert {"supplier_code", "supplier_name", "project_code", "other_info"} <= quote_columns
        assert {"idx_quote_supplier", "idx_quote_project"} <= _indexes(conn, "quote")
    finally:
        conn.close()


def test_init_db_migrates_legacy_quote_table(tmp_path):
    """旧库（quote 只有早期几个列）也必须能启动，而不是 no such column。"""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "CREATE TABLE quote (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, category_code TEXT,"
            " basic_info TEXT, parse_status TEXT)"
        )
    conn.close()

    init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        expected = {name for name, *_ in _schema_columns()["quote"]}
        assert expected <= _columns(conn, "quote")
        assert {"idx_quote_supplier", "idx_quote_project"} <= _indexes(conn, "quote")
        with conn:
            conn.execute(
                "INSERT INTO quote (task_id, supplier_name, project_code, parse_status)"
                " VALUES (1, '甲', NULL, 'parsed')"
            )
        assert conn.execute("SELECT COUNT(*) FROM quote").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "twice.db"
    init_db(db_path)
    init_db(db_path)  # 二次启动不应报错（表/索引均为 IF NOT EXISTS）

    conn = sqlite3.connect(db_path)
    try:
        assert "idx_quote_project" in _indexes(conn, "quote")
    finally:
        conn.close()

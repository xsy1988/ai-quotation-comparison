"""L1 别名匹配测试：唯一命中 / 歧义 / 无命中 / 品类降级 / 归一化。"""

import sqlite3

import pytest

from app.match.atom_match import (
    Lexicon,
    load_lexicon,
    make_fingerprint,
    match_l1,
    normalize_term,
)


@pytest.fixture
def lex():
    return Lexicon(
        entries={
            "cnc加工": [(None, "AT-QX-001", "CNC加工")],
            "cnc切割": [(1, "AT-QX-001", "CNC 切割")],
            "edm": [(2, "AT-JG-001", "EDM"), (3, "AT-JG-002", "EDM")],
        }
    )


def test_normalize_term():
    assert normalize_term("ＣＮＣ 切割") == "cnc切割"
    assert normalize_term("CNC  Cutting") == "cnccutting"
    assert normalize_term("阳极氧化") == "阳极氧化"


def test_unique_hit_by_atom_name(lex):
    result = match_l1("CNC加工", lex)
    assert result.atom_code == "AT-QX-001"
    assert result.confidence == "high"
    assert result.match_path == "alias_exact"


def test_unique_hit_by_alias_normalized(lex):
    # "CNC 切割" 归一化后与词库 "cnc切割" 命中
    result = match_l1("ＣＮＣ 切割", lex)
    assert result.atom_code == "AT-QX-001"


def test_ambiguous_no_auto_decision(lex):
    result = match_l1("EDM", lex)
    assert result.atom_code is None
    assert result.confidence == "low"
    assert result.note and "歧义" in result.note


def test_no_hit(lex):
    result = match_l1("等离子喷涂", lex)
    assert result.atom_code is None
    assert result.note is None


def test_hit_count_bumped_via_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "t.db"))
    from app.db import init_db, get_connection
    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO process_domain (code, name) VALUES ('CX', '成型')")
    conn.execute("INSERT INTO process_stage (name) VALUES ('毛坯')")
    conn.execute("INSERT INTO process_class (name) VALUES ('成型加工')")
    conn.execute("INSERT INTO atom (code, name, domain_code, stage_name, class_name) VALUES ('AT-QX-001', 'CNC切割', 'CX', '毛坯', '成型加工')")
    conn.execute("INSERT INTO atom_alias (atom_code, alias_text) VALUES ('AT-QX-001', 'CNC 切割')")
    conn.commit()
    lexicon = load_lexicon(conn)
    match_l1("CNC切割", lexicon, conn=conn)  # 原子名命中，不计 hit_count
    match_l1("CNC 切割", lexicon, conn=conn)  # 别名命中，计 hit_count
    hits = conn.execute("SELECT hit_count FROM atom_alias").fetchone()[0]
    assert hits == 1


def test_category_downgrade(lex, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "t.db"))
    from app.db import init_db, get_connection
    init_db()
    conn = get_connection()
    conn.execute("INSERT INTO category (code, name) VALUES ('CAT-X', '测试品类')")
    conn.execute("INSERT INTO process_domain (code, name) VALUES ('CX', '成型')")
    conn.execute("INSERT INTO process_stage (name) VALUES ('毛坯')")
    conn.execute("INSERT INTO process_class (name) VALUES ('成型加工')")
    conn.execute("INSERT INTO atom (code, name, domain_code, stage_name, class_name) VALUES ('AT-QX-001', 'CNC加工', 'CX', '毛坯', '成型加工')")
    conn.commit()
    result = match_l1("CNC加工", lex, category_code="CAT-X", conn=conn)
    assert result.atom_code == "AT-QX-001"
    assert result.confidence == "mid"
    assert "跨品类" in (result.note or "")


def test_unmatched_recorded_once(lex, tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTES_DB_PATH", str(tmp_path / "t.db"))
    from app.db import init_db, get_connection
    init_db()
    conn = get_connection()
    match_l1("等离子喷涂", lex, conn=conn)
    match_l1("等离子喷涂", lex, conn=conn)
    row = conn.execute("SELECT occurrence_count FROM unmatched_term WHERE term_text='等离子喷涂'").fetchone()
    assert row[0] == 2


def test_make_fingerprint():
    assert make_fingerprint(["AT-B", "AT-A", "AT-C"]) == "AT-A|AT-B|AT-C"
    assert make_fingerprint(["AT-A"]) == "AT-A"

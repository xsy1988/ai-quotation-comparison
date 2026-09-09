"""L1 别名精确匹配：原子名 ∪ 别名 词库；唯一命中自动判，歧义/未命中进词池。

词库构建时把原子名与别名走同一归一化（NFKC + casefold + 去空白），
因此供应商写原子本名（"阳极氧化"）或别名写法（"CNC 切割"）都能命中。
"""

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_term(text: str) -> str:
    """条目名归一化：NFKC（全角→半角）+ casefold + 去全部空白。"""
    text = unicodedata.normalize("NFKC", str(text))
    return _WHITESPACE_RE.sub("", text).casefold()


@dataclass
class Lexicon:
    # normalized_text → list[(alias_id 或 None, atom_code, 原文)]；None 表示来自原子名
    entries: dict[str, list[tuple[int | None, str, str]]] = field(default_factory=dict)

    def codes(self, text: str) -> set[str]:
        return {code for _, code, _ in self.entries.get(normalize_term(text), [])}

    def alias_ids(self, text: str, code: str) -> list[int]:
        """仅当供应商原文与别名原文一致（去首尾空白）时才计命中，原子名写法不计。"""
        raw = str(text).strip()
        return [
            aid
            for aid, c, alias_raw in self.entries.get(normalize_term(text), [])
            if aid and c == code and alias_raw.strip() == raw
        ]


@dataclass
class MatchResult:
    atom_code: str | None
    confidence: str
    match_path: str
    note: str | None = None


def load_lexicon(conn: sqlite3.Connection) -> Lexicon:
    lex = Lexicon()
    for row in conn.execute("SELECT code, name FROM atom"):
        lex.entries.setdefault(normalize_term(row["name"]), []).append((None, row["code"], row["name"]))
    for row in conn.execute("SELECT id, atom_code, alias_text FROM atom_alias"):
        lex.entries.setdefault(normalize_term(row["alias_text"]), []).append(
            (row["id"], row["atom_code"], row["alias_text"])
        )
    return lex


def record_unmatched(conn: sqlite3.Connection | None, term: str) -> None:
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO unmatched_term (term_text) VALUES (?)", (term,)
            )
    except sqlite3.IntegrityError:
        with conn:
            conn.execute(
                "UPDATE unmatched_term SET occurrence_count = occurrence_count + 1 WHERE term_text = ?",
                (term,),
            )


def bump_hit_count(conn: sqlite3.Connection | None, alias_ids: list[int]) -> None:
    if conn is None or not alias_ids:
        return
    with conn:
        conn.executemany(
            "UPDATE atom_alias SET hit_count = hit_count + 1 WHERE id = ?",
            [(aid,) for aid in alias_ids],
        )


def match_l1(
    term: str,
    lexicon: Lexicon,
    category_code: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> MatchResult:
    """L1 精确匹配四态：唯一命中 / 歧义 / 无命中 / 跨品类降级。"""
    candidates = lexicon.codes(term)
    if not candidates:
        record_unmatched(conn, term)
        return MatchResult(None, "low", "L3_none")
    if len(candidates) > 1:
        record_unmatched(conn, term)
        return MatchResult(None, "low", "L3_none", note=f"歧义别名：{sorted(candidates)}")

    code = next(iter(candidates))
    bump_hit_count(conn, lexicon.alias_ids(term, code))

    if category_code and conn is not None:
        in_category = conn.execute(
            "SELECT 1 FROM atom_category WHERE atom_code = ? AND category_code = ?",
            (code, category_code),
        ).fetchone()
        if not in_category:
            return MatchResult(code, "mid", "alias_exact", note="跨品类命中")
    return MatchResult(code, "high", "alias_exact")


def make_fingerprint(members: list[str]) -> str:
    """组合指纹：成员编码排序后拼接，跨供应商对齐用。"""
    return "|".join(sorted(members))

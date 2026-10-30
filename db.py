"""
TextCheck database (SQLite, built into Python, no install needed).
Everything is saved to textcheck.db next to this file.

Tables:
  checks     - every message you checked (original, fixed, changes, tone warning, where it came from)
  mistakes   - each word you typo'd and how many times ("promble" -> "problem", 3 times)
  my_words   - names/slang you never want flagged
"""

import json
import os
import re
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "textcheck.db")
LEARN_AFTER = 2  # after you make the same typo this many times, it's fixed instantly (no AI needed)


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, source TEXT, original TEXT, fixed TEXT,
        changes TEXT, tone_warning TEXT, seconds REAL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mistakes (
        wrong TEXT, right TEXT, count INTEGER DEFAULT 0, last_seen TEXT,
        PRIMARY KEY (wrong, right))""")
    conn.execute("CREATE TABLE IF NOT EXISTS my_words (word TEXT PRIMARY KEY)")
    return conn


def parse_change(change: str):
    """'"promble" -> "problem" (spelling)'  ->  ("promble", "problem")"""
    m = re.match(r'\s*["“]([^"”]+)["”]\s*->\s*["“]([^"”]+)["”]', change)
    return (m.group(1), m.group(2)) if m else None


def log_check(original, fixed, changes, tone_warning, source, seconds=0.0):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO checks (created_at, source, original, fixed, changes, tone_warning, seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, source, original, fixed, json.dumps(changes), tone_warning, round(seconds, 1)),
        )
        for c in changes:
            pair = parse_change(c)
            # only learn single-word typos, like "promble" -> "problem"
            if pair and " " not in pair[0] and " " not in pair[1] and pair[0].lower() != pair[1].lower():
                conn.execute(
                    "INSERT INTO mistakes (wrong, right, count, last_seen) VALUES (?, ?, 1, ?) "
                    "ON CONFLICT(wrong, right) DO UPDATE SET count = count + 1, last_seen = excluded.last_seen",
                    (pair[0].lower(), pair[1].lower(), now),
                )


def learned_typos() -> dict:
    """Typos you've made LEARN_AFTER+ times: {"promble": "problem"}"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT wrong, right FROM mistakes WHERE count >= ? ORDER BY count DESC", (LEARN_AFTER,)
        ).fetchall()
    learned = {}
    for wrong, right in rows:
        learned.setdefault(wrong, right)  # keep the most common fix
    for w in my_words():
        learned.pop(w, None)
    return learned


def fix_learned(text: str, changes: list) -> str:
    """Instantly fix typos you've made before."""
    learned = learned_typos()
    if not learned:
        return text

    def repl(m):
        word = m.group(0)
        right = learned.get(word.lower())
        if not right:
            return word
        if word[0].isupper():
            right = right[0].upper() + right[1:]
        changes.append(f'"{word}" -> "{right}" (you make this typo a lot)')
        return right

    return re.sub(r"(?<![\w'])[A-Za-z]+(?![\w'])", repl, text)


def add_word(word: str):
    word = word.strip().lower()
    if word:
        with _conn() as conn:
            conn.execute("INSERT OR IGNORE INTO my_words (word) VALUES (?)", (word,))


def remove_word(word: str):
    with _conn() as conn:
        conn.execute("DELETE FROM my_words WHERE word = ?", (word.strip().lower(),))


def my_words() -> set:
    with _conn() as conn:
        return {r[0] for r in conn.execute("SELECT word FROM my_words")}


def recent_checks(limit=20) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT created_at, source, original, fixed, tone_warning FROM checks ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [list(r) for r in rows]


def top_mistakes(limit=10) -> list:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT wrong, right, count FROM mistakes ORDER BY count DESC, last_seen DESC LIMIT ?", (limit,)
        ).fetchall()
    return [list(r) for r in rows]


def stats() -> dict:
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
        fixed = conn.execute("SELECT COUNT(*) FROM checks WHERE original != fixed").fetchone()[0]
        tone = conn.execute("SELECT COUNT(*) FROM checks WHERE tone_warning != ''").fetchone()[0]
        n_mistakes = conn.execute("SELECT COALESCE(SUM(count), 0) FROM mistakes").fetchone()[0]
        avg = conn.execute("SELECT COALESCE(AVG(seconds), 0) FROM checks").fetchone()[0]
    return {"checks": total, "messages_fixed": fixed, "tone_warnings": tone,
            "words_fixed": n_mistakes, "learned": len(learned_typos()), "avg_seconds": round(avg, 1)}

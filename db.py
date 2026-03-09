"""
db.py — SQLite persistence for Clima-Gold
"""
import sqlite3
import json
import os

DB_PATH = os.environ.get("DB_PATH", "clima_gold.db")


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS open_positions (
                pos_id TEXT PRIMARY KEY,
                data   TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS closed_positions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                pos_id    TEXT,
                data      TEXT NOT NULL,
                closed_at TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS capital_history (
                ts      TEXT DEFAULT (datetime('now')),
                capital REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.commit()


def upsert_open(pos_id: str, data: dict):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO open_positions (pos_id, data) VALUES (?, ?)",
            (pos_id, json.dumps(data))
        )
        c.commit()


def delete_open(pos_id: str):
    with _conn() as c:
        c.execute("DELETE FROM open_positions WHERE pos_id = ?", (pos_id,))
        c.commit()


def load_open_positions() -> dict:
    with _conn() as c:
        rows = c.execute("SELECT pos_id, data FROM open_positions").fetchall()
        return {r["pos_id"]: json.loads(r["data"]) for r in rows}


def insert_closed(pos_id: str, data: dict):
    with _conn() as c:
        c.execute(
            "INSERT INTO closed_positions (pos_id, data) VALUES (?, ?)",
            (pos_id, json.dumps(data))
        )
        c.commit()


def load_closed_positions(limit=50) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT data FROM closed_positions ORDER BY closed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]


def append_capital(capital: float):
    with _conn() as c:
        c.execute("INSERT INTO capital_history (capital) VALUES (?)", (capital,))
        c.commit()


def load_capital_history(limit=200) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, capital FROM capital_history ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"ts": r["ts"], "capital": r["capital"]} for r in reversed(rows)]


def set_state(key: str, value: str):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
        c.commit()


def get_state(key: str, default=None) -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

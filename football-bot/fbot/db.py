"""SQLite storage: settings, fixtures, bot signals (tips) and the user's bets."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dc_cache (
    key TEXT PRIMARY KEY, div TEXT,
    dc_h REAL, dc_d REAL, dc_a REAL, dc_over REAL, dc_lh REAL, dc_la REAL
);
CREATE TABLE IF NOT EXISTS fixtures (
    key TEXT PRIMARY KEY, div TEXT, kickoff TEXT, home TEXT, away TEXT,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL, div TEXT, kickoff TEXT, home TEXT, away TEXT,
    market TEXT NOT NULL, selection TEXT NOT NULL,
    model_prob REAL, market_prob REAL, price REAL, price_source TEXT, ev REAL, kelly REAL,
    first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1,
    status TEXT DEFAULT 'open', fthg INTEGER, ftag INTEGER, profit REAL,
    close_prob REAL, clv REAL,
    UNIQUE(key, market, selection)
);
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT, tip_id INTEGER, by_tip INTEGER DEFAULT 0,
    key TEXT NOT NULL, div TEXT, kickoff TEXT, home TEXT, away TEXT,
    market TEXT NOT NULL, selection TEXT NOT NULL,
    odds REAL NOT NULL, stake REAL NOT NULL,
    model_prob REAL, market_prob REAL, ev REAL,
    bookmaker TEXT, note TEXT,
    status TEXT DEFAULT 'open', fthg INTEGER, ftag INTEGER, profit REAL,
    settled_at TEXT, manual INTEGER DEFAULT 0,
    close_odds REAL, close_prob REAL, clv REAL
);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_tips_kickoff ON tips(kickoff);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)


def get_settings(conn: sqlite3.Connection) -> dict:
    values = dict(config.DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in values:
            values[row["key"]] = json.loads(row["value"])
    return values


def save_settings(conn: sqlite3.Connection, values: dict) -> None:
    for k, v in values.items():
        if k in config.DEFAULT_SETTINGS:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v, ensure_ascii=False)),
            )


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False, default=str)),
    )


def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]

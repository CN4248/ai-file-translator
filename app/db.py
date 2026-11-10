from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    source_lang TEXT NOT NULL DEFAULT 'auto',
    source_filter TEXT NOT NULL DEFAULT '[]',
    mode TEXT NOT NULL DEFAULT 'offline_first',
    input_dir TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    output_name TEXT,
    name_suffix TEXT NOT NULL DEFAULT '',
    preserve_tree INTEGER NOT NULL DEFAULT 1,
    total_files INTEGER NOT NULL DEFAULT 0,
    done_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    skipped_files INTEGER NOT NULL DEFAULT 0,
    total_segments INTEGER NOT NULL DEFAULT 0,
    done_segments INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL DEFAULT 0,
    rel_path TEXT NOT NULL,
    output_rel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    size INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    source_lang TEXT,
    segments_total INTEGER NOT NULL DEFAULT 0,
    segments_done INTEGER NOT NULL DEFAULT 0,
    translated_segments INTEGER NOT NULL DEFAULT 0,
    skipped_segments INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_items_task ON items(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    task_id TEXT,
    item_id INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id, id);

CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    engine TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    translated TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created_at);

CREATE TABLE IF NOT EXISTS download_jobs (
    id TEXT PRIMARY KEY,
    from_code TEXT NOT NULL,
    to_code TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or 0

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            if row is None:
                return default
            return row[0]

    # Settings helpers -------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,strftime('%s','now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def get_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.query("SELECT key,value FROM settings")}

    def prune_logs(self, keep: int) -> None:
        max_id = self.scalar("SELECT MAX(id) FROM logs", default=0) or 0
        cutoff = max_id - keep
        if cutoff > 0:
            self.execute("DELETE FROM logs WHERE id <= ?", (cutoff,))

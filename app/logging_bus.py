from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from typing import Any

from .db import Database

LEVELS = ("debug", "info", "success", "warn", "error")


class LogBus:
    """Append-only application log persisted in SQLite and fan-out to SSE readers."""

    def __init__(self, db: Database, max_rows: int = 30000):
        self.db = db
        self.max_rows = max_rows
        self._cond = threading.Condition()
        self._writes = 0

    def log(
        self,
        level: str,
        message: str,
        task_id: str | None = None,
        item_id: int | None = None,
        *,
        echo: bool = True,
    ) -> int:
        level = level if level in LEVELS else "info"
        ts = time.time()
        row_id = self.db.execute(
            "INSERT INTO logs(ts,level,task_id,item_id,message) VALUES(?,?,?,?,?)",
            (ts, level, task_id, item_id, str(message)[:8000]),
        )
        if echo:
            stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            prefix = f"[{stamp}] {level.upper():7s}"
            if task_id:
                prefix += f" [{task_id[:8]}]"
            print(f"{prefix} {message}", flush=True)
        with self._cond:
            self._writes += 1
            self._cond.notify_all()
        # Trim occasionally, not on every line.
        if row_id % 500 == 0:
            try:
                self.db.prune_logs(self.max_rows)
            except Exception:
                pass
        return row_id

    def recent(
        self,
        after_id: int = 0,
        limit: int = 300,
        task_id: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["id > ?"]
        params: list[Any] = [after_id]
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if level and level != "all":
            clauses.append("level = ?")
            params.append(level)
        params.append(max(1, min(limit, 1000)))
        # Initial page: show the newest lines, not the oldest ones. Incremental
        # reads (after_id > 0) stay ascending so the UI can append them.
        order = "DESC" if after_id <= 0 else "ASC"
        sql = (
            "SELECT id,ts,level,task_id,item_id,message FROM logs WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY id {order} LIMIT ?"
        )
        rows = self.db.query(sql, params)
        return list(reversed(rows)) if order == "DESC" else rows

    def wait_for_change(self, seen_id: int, timeout: float = 1.0) -> bool:
        """Wait briefly for new lines; returns True when at least one was seen."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while time.monotonic() < deadline:
                latest = self.db.scalar("SELECT MAX(id) FROM logs", default=0) or 0
                if latest > seen_id:
                    return True
                self._cond.wait(timeout=min(0.35, max(0.05, deadline - time.monotonic())))
        return False

    def clear_for_task(self, task_id: str) -> None:
        self.db.execute("DELETE FROM logs WHERE task_id=?", (task_id,))
        with self._cond:
            self._writes += 1
            self._cond.notify_all()

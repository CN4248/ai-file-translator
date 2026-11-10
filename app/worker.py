from __future__ import annotations

import os
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from .config import Config
from .db import Database
from .engines.base import AIConfigError, ControlInterrupted, SkipFile, TranslationError
from .formats.base import TranslationContext
from .formats.registry import FormatRegistry
from .logging_bus import LogBus
from .settings import SettingsService
from .translator import TranslationManager


class TaskStateError(RuntimeError):
    pass


class TaskManager:
    def __init__(
        self,
        db: Database,
        cfg: Config,
        bus: LogBus,
        registry: FormatRegistry,
        translator: TranslationManager,
        settings: SettingsService,
    ):
        self.db = db
        self.cfg = cfg
        self.bus = bus
        self.registry = registry
        self.translator = translator
        self.settings = settings
        self._lock = threading.RLock()
        self._active: set[str] = set()
        self._shutting_down = False
        self._threads: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------
    def recover(self, auto_resume: bool = True) -> None:
        self.db.execute(
            "UPDATE items SET status='pending', started_at=NULL WHERE status='running'"
        )
        self.db.execute(
            "UPDATE tasks SET status='queued', pause_requested=0, cancel_requested=0, "
            "updated_at=? WHERE status IN ('running','pausing','cancelling')",
            (time.time(),),
        )
        # Remove half-written outputs from a previous crash.
        for task in self.db.query("SELECT output_dir FROM tasks"):
            try:
                for path in Path(task["output_dir"]).glob(".*.lf-part-*"):
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        if auto_resume:
            queued = self.db.scalar(
                "SELECT COUNT(*) FROM tasks WHERE status='queued'", default=0
            ) or 0
            if queued:
                self.bus.log("info", f"检测到 {queued} 个待处理任务，自动恢复执行")
                self._schedule()

    # ------------------------------------------------------------------
    def _schedule(self) -> None:
        with self._lock:
            if self._shutting_down:
                return
            while len(self._active) < self.cfg.max_task_workers:
                rows = self.db.query(
                    "SELECT id FROM tasks WHERE status='queued' ORDER BY created_at ASC LIMIT 50"
                )
                candidate = next((r["id"] for r in rows if r["id"] not in self._active), None)
                if candidate is None:
                    return
                self._active.add(candidate)
                thread = threading.Thread(
                    target=self._run_task, args=(candidate,), name=f"task-{candidate[:8]}", daemon=True
                )
                self._threads[candidate] = thread
                thread.start()

    def _run_task(self, task_id: str) -> None:
        try:
            self._process(task_id)
        except Exception as exc:  # last-resort guard; never leave a task "running"
            self.bus.log("error", f"任务线程异常：{exc}", task_id)
            self.db.execute(
                "UPDATE tasks SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=?",
                (str(exc)[:2000], time.time(), time.time(), task_id),
            )
        finally:
            with self._lock:
                self._active.discard(task_id)
                self._threads.pop(task_id, None)
            self._schedule()

    # ------------------------------------------------------------------
    def _process(self, task_id: str) -> None:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return
        if task["status"] != "queued":
            return
        now = time.time()
        self.db.execute(
            "UPDATE tasks SET status='running', started_at=COALESCE(started_at,?), finished_at=NULL, "
            "updated_at=?, error=NULL WHERE id=?",
            (now, now, task_id),
        )
        self.bus.log("info", f"开始执行任务「{task['name']}」", task_id)
        while True:
            try:
                self._check_control(task_id)
            except ControlInterrupted as interrupt:
                self._finish_interrupted(task_id, interrupt.action)
                return
            item = self.db.one(
                "SELECT * FROM items WHERE task_id=? AND status IN ('pending','running') "
                "ORDER BY seq ASC LIMIT 1",
                (task_id,),
            )
            if item is None:
                self._finalize(task_id)
                return
            try:
                self._process_item(task_id, item)
            except ControlInterrupted as interrupt:
                self.db.execute(
                    "UPDATE items SET status='pending', started_at=NULL WHERE id=?",
                    (item["id"],),
                )
                self._refresh_counts(task_id)
                self._finish_interrupted(task_id, interrupt.action)
                return
            except Exception as exc:
                # _process_item normally handles its own errors; this is a safety net.
                self.bus.log("error", f"处理文件失败：{exc}", task_id, item["id"])
                self.db.execute(
                    "UPDATE items SET status='failed', error=?, finished_at=? WHERE id=?",
                    (str(exc)[:2000], time.time(), item["id"]),
                )
                self._refresh_counts(task_id)

    def _process_item(self, task_id: str, item: dict) -> None:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise ControlInterrupted("cancel")
        input_root = Path(task["input_dir"])
        src = input_root if input_root.is_file() else input_root / item["rel_path"]
        output_root = Path(task["output_dir"])
        dst = (output_root / item["output_rel"]).resolve()
        if not _is_within(dst, output_root.resolve()):
            raise TranslationError("输出路径越界，已阻止写入")
        if not src.exists():
            raise TranslationError(f"源文件不存在：{src}")
        handler = self.registry.get(src)
        if handler is None:
            raise TranslationError(f"不支持的文件类型：{src.suffix}")

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.parent / f".{dst.name}.lf-part-{uuid.uuid4().hex[:10]}"
        now = time.time()
        self.db.execute(
            "UPDATE items SET status='running', started_at=?, attempts=attempts+1, "
            "segments_done=0, segments_total=0 WHERE id=?",
            (now, item["id"]),
        )
        self.bus.log(
            "info",
            f"翻译 {item['rel_path']}（{item['size']/1024:.1f} KB）→ {item['output_rel']}",
            task_id,
            item["id"],
        )
        ctx = TranslationContext(
            db=self.db,
            manager=self.translator,
            task=task,
            item=item,
            check_control=lambda: self._check_control(task_id),
            target_lang=task["target_lang"],
            source_lang=task["source_lang"],
            source_filter=_json_list(task["source_filter"]),
            mode=task["mode"],
            log=self.bus.log,
        )
        try:
            result = handler.translate(src, tmp, ctx)
            self._check_control(task_id)
            if not tmp.exists():
                raise TranslationError("翻译器没有生成输出文件")
            try:
                shutil.copystat(src, tmp)
            except OSError:
                pass
            os.replace(tmp, dst)
            self.db.execute(
                "UPDATE items SET status='completed', error=NULL, finished_at=?, "
                "translated_segments=?, skipped_segments=?, source_lang=COALESCE(source_lang, ?) WHERE id=?",
                (
                    time.time(), result.translated_segments, result.skipped_segments,
                    ctx.source_lang or None, item["id"],
                ),
            )
            self.bus.log(
                "success",
                f"完成 {item['rel_path']}：{result.translated_segments} 段 → {item['output_rel']}",
                task_id,
                item["id"],
            )
        except ControlInterrupted:
            tmp.unlink(missing_ok=True)
            raise
        except SkipFile as exc:
            tmp.unlink(missing_ok=True)
            self.db.execute(
                "UPDATE items SET status='skipped', error=?, finished_at=? WHERE id=?",
                (str(exc)[:1000], time.time(), item["id"]),
            )
            self.bus.log("warn", f"跳过 {item['rel_path']}：{exc}", task_id, item["id"])
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            message = str(exc)[:2000] or exc.__class__.__name__
            self.db.execute(
                "UPDATE items SET status='failed', error=?, finished_at=? WHERE id=?",
                (message, time.time(), item["id"]),
            )
            level = "error" if not isinstance(exc, (AIConfigError, TranslationError)) else "error"
            self.bus.log(level, f"失败 {item['rel_path']}：{message}", task_id, item["id"])
            if not isinstance(exc, (AIConfigError, TranslationError, OSError)):
                self.bus.log(
                    "debug", traceback.format_exc()[:3000], task_id, item["id"]
                )
        finally:
            self._refresh_counts(task_id)

    # ------------------------------------------------------------------
    def _check_control(self, task_id: str) -> None:
        row = self.db.one(
            "SELECT status, pause_requested, cancel_requested FROM tasks WHERE id=?", (task_id,)
        )
        if row is None or row["status"] in ("cancelling", "cancelled") or row["cancel_requested"]:
            raise ControlInterrupted("cancel")
        if row["status"] == "pausing" or row["pause_requested"]:
            raise ControlInterrupted("pause")

    def _finish_interrupted(self, task_id: str, action: str) -> None:
        now = time.time()
        if action == "cancel":
            self.db.execute(
                "UPDATE tasks SET status='cancelled', cancel_requested=0, pause_requested=0, "
                "finished_at=?, updated_at=? WHERE id=?",
                (now, now, task_id),
            )
            self.bus.log("warn", "任务已取消，未完成的文件保留在队列中", task_id)
        else:
            self.db.execute(
                "UPDATE tasks SET status='paused', pause_requested=0, cancel_requested=0, "
                "updated_at=? WHERE id=?",
                (now, task_id),
            )
            self.bus.log("info", "任务已暂停，进度已保存", task_id)
        self._refresh_counts(task_id)

    def _finalize(self, task_id: str) -> None:
        self._refresh_counts(task_id)
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            return
        failed = task["failed_files"]
        done = task["done_files"] + task["skipped_files"]
        if failed and done:
            status = "partial"
        elif failed:
            status = "failed"
        else:
            status = "completed"
        now = time.time()
        self.db.execute(
            "UPDATE tasks SET status=?, finished_at=?, updated_at=? WHERE id=?",
            (status, now, now, task_id),
        )
        level = "success" if status == "completed" else ("warn" if status == "partial" else "error")
        label = {"completed": "任务完成", "partial": "任务部分完成", "failed": "任务失败"}[status]
        self.bus.log(
            level,
            f"{label}：成功 {task['done_files']}，跳过 {task['skipped_files']}，失败 {failed}",
            task_id,
        )

    def _refresh_counts(self, task_id: str) -> None:
        counts = {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) AS n FROM items WHERE task_id=? GROUP BY status", (task_id,)
        )}
        seg = self.db.one(
            "SELECT COALESCE(SUM(segments_total),0) AS total, COALESCE(SUM(translated_segments),0) AS done "
            "FROM items WHERE task_id=?",
            (task_id,),
        )
        now = time.time()
        self.db.execute(
            "UPDATE tasks SET done_files=?, failed_files=?, skipped_files=?, total_segments=?, "
            "done_segments=?, updated_at=? WHERE id=?",
            (
                counts.get("completed", 0), counts.get("failed", 0), counts.get("skipped", 0),
                int(seg["total"] or 0), int(seg["done"] or 0), now, task_id,
            ),
        )

    # ------------------------------------------------------------------
    def start_task(self, task_id: str, retry_failed: bool = False) -> dict:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError(task_id)
        with self._lock:
            if task_id in self._active or task["status"] in ("running", "pausing", "cancelling"):
                raise TaskStateError("任务正在运行")
            if task["status"] == "completed" and not retry_failed:
                raise TaskStateError("任务已完成")
            if retry_failed or task["status"] in ("failed", "partial"):
                self.db.execute(
                    "UPDATE items SET status='pending', error=NULL, started_at=NULL, finished_at=NULL "
                    "WHERE task_id=? AND status='failed'",
                    (task_id,),
                )
            self.db.execute(
                "UPDATE tasks SET status='queued', pause_requested=0, cancel_requested=0, "
                "error=NULL, finished_at=NULL, updated_at=? WHERE id=?",
                (time.time(), task_id),
            )
            self.bus.log("info", "任务已加入执行队列", task_id)
            self._schedule()
        return self._task_brief(task_id)

    def pause_task(self, task_id: str) -> dict:
        task = self.db.one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError(task_id)
        if task["status"] in ("completed", "failed", "partial"):
            raise TaskStateError("任务已结束")
        if task["status"] in ("queued", "held"):
            self.db.execute(
                "UPDATE tasks SET status='paused', pause_requested=0, updated_at=? WHERE id=?",
                (time.time(), task_id),
            )
            self.bus.log("info", "排队任务已暂停", task_id)
        else:
            self.db.execute(
                "UPDATE tasks SET status='pausing', pause_requested=1, updated_at=? WHERE id=?",
                (time.time(), task_id),
            )
            self.bus.log("info", "正在暂停任务，等待当前段落结束…", task_id)
        return self._task_brief(task_id)

    def cancel_task(self, task_id: str) -> dict:
        task = self.db.one("SELECT status FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError(task_id)
        if task["status"] in ("completed", "failed", "partial", "cancelled"):
            return self._task_brief(task_id)
        if task["status"] in ("queued", "paused", "held"):
            self.db.execute(
                "UPDATE tasks SET status='cancelled', cancel_requested=0, pause_requested=0, "
                "finished_at=?, updated_at=? WHERE id=?",
                (time.time(), time.time(), task_id),
            )
            self.bus.log("warn", "任务已取消", task_id)
        else:
            self.db.execute(
                "UPDATE tasks SET status='cancelling', cancel_requested=1, updated_at=? WHERE id=?",
                (time.time(), task_id),
            )
            self.bus.log("warn", "正在取消任务…", task_id)
        return self._task_brief(task_id)

    def retry_task(self, task_id: str) -> dict:
        return self.start_task(task_id, retry_failed=True)

    def batch(self, action: str, ids: list[str]) -> dict:
        results = []
        for task_id in ids:
            try:
                if action == "start":
                    self.start_task(task_id)
                elif action == "pause":
                    self.pause_task(task_id)
                elif action == "cancel":
                    self.cancel_task(task_id)
                elif action == "retry":
                    self.retry_task(task_id)
                else:
                    raise TaskStateError(f"未知批量操作：{action}")
                results.append({"id": task_id, "ok": True})
            except Exception as exc:
                results.append({"id": task_id, "ok": False, "error": str(exc)})
        return {"results": results}

    def retry_all_failed(self) -> dict:
        rows = self.db.query(
            "SELECT DISTINCT task_id FROM items WHERE status='failed'"
        )
        started = 0
        for row in rows:
            task_id = row["task_id"]
            try:
                task = self.db.one("SELECT status FROM tasks WHERE id=?", (task_id,))
                if not task or task["status"] in ("running", "pausing", "cancelling"):
                    continue
                self.start_task(task_id, retry_failed=True)
                started += 1
            except Exception as exc:
                self.bus.log("warn", f"重试任务失败：{exc}", task_id)
        self.bus.log("info", f"已重新排队 {started} 个含失败文件的任务")
        return {"started": started}

    def clear_completed(self) -> dict:
        rows = self.db.query("SELECT id,name FROM tasks WHERE status='completed'")
        for row in rows:
            self.db.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
            self.db.execute("DELETE FROM logs WHERE task_id=?", (row["id"],))
        if rows:
            self.bus.log("info", f"已清除 {len(rows)} 条已完成任务记录")
        return {"cleared": len(rows)}

    def _task_brief(self, task_id: str) -> dict:
        return self.db.one(
            "SELECT id,status,done_files,failed_files,skipped_files,total_files FROM tasks WHERE id=?",
            (task_id,),
        ) or {}

    # ------------------------------------------------------------------
    def shutdown(self, timeout: float = 6.0) -> None:
        with self._lock:
            self._shutting_down = True
            self.db.execute(
                "UPDATE tasks SET pause_requested=1 WHERE status IN ('running','pausing')"
            )
            threads = list(self._threads.values())
        self.bus.log("info", "容器正在停止，任务进度已保存；下次启动可继续")
        for thread in threads:
            thread.join(timeout=max(0.5, timeout / max(1, len(threads))))


def _json_list(value) -> list[str]:
    import json

    try:
        data = json.loads(value or "[]")
        return [str(x) for x in data if x]
    except Exception:
        return []


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False

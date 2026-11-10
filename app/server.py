from __future__ import annotations

import json
import mimetypes
import os
import platform
import re
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config
from .db import Database
from .engines.base import AIConfigError, TranslationError
from .engines.models import ModelStore
from .engines.offline import OfflineEngine
from .formats.registry import FormatRegistry
from .languages import language_options
from .logging_bus import LogBus
from .ocr import available as ocr_available, installed_languages as ocr_languages
from .settings import SettingsService
from .tasks import TaskService, TaskValidationError
from .translator import TranslationManager
from .worker import TaskManager, TaskStateError

STATIC_DIR = Path(__file__).parent / "static"
TASK_RE = re.compile(r"^/api/tasks/([a-zA-Z0-9]+)$")
TASK_ACTION_RE = re.compile(r"^/api/tasks/([a-zA-Z0-9]+)/(start|pause|cancel|retry)$")
MODEL_JOB_RE = re.compile(r"^/api/models/jobs/([a-zA-Z0-9]+)$")
MODEL_PAIR_RE = re.compile(r"^/api/models/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class App:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        bus: LogBus,
        offline: OfflineEngine,
        models: ModelStore,
        registry: FormatRegistry,
        translator: TranslationManager,
        settings: SettingsService,
    ):
        self.cfg = cfg
        self.db = db
        self.bus = bus
        self.offline = offline
        self.models = models
        self.registry = registry
        self.translator = translator
        self.settings = settings
        self.tasks = TaskService(db, cfg, bus, registry)
        self.manager = TaskManager(db, cfg, bus, registry, translator, settings)
        self.started_at = time.time()
        self._bg: list[threading.Thread] = []

    def start(self) -> None:
        self.bus.log("info", f"LinguaFlow {self.cfg.version} 启动，端口 {self.cfg.port}")
        self.db.execute(
            "UPDATE download_jobs SET status='error', error='容器重启，下载中断', updated_at=? "
            "WHERE status IN ('pending','running')",
            (time.time(),),
        )
        self.models.load_cached_catalog()
        # Install preloaded packages before recovering queued tasks, otherwise a
        # task that needs a newly mounted model can fail on its first item.
        if self.cfg.preload_dir.exists() and any(self.cfg.preload_dir.glob("*.argosmodel")):
            self._import_preload()
        auto_resume = bool(self.settings.get().get("auto_resume", True))
        self.manager.recover(auto_resume=auto_resume)

    def _import_preload(self) -> None:
        try:
            installed = self.models.import_dir(self.cfg.preload_dir, log=self.bus.log)
            if installed:
                self.offline.refresh()
                self.bus.log("success", f"预加载目录导入完成：{len(installed)} 个语言包")
        except Exception as exc:
            self.bus.log("warn", f"预加载语言包失败：{exc}")

    def shutdown(self) -> None:
        self.manager.shutdown()
        self.db.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LinguaFlow/1.0"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._api(method, path, query)
            elif method == "GET":
                self._static(path)
            else:
                raise ApiError(405, "不支持的请求方法")
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except BrokenPipeError:
            self.close_connection = True
        except Exception as exc:
            self.app.bus.log("error", f"HTTP 处理异常：{exc}")
            try:
                self._json({"error": f"服务器错误：{exc}"}, 500)
            except Exception:
                self.close_connection = True

    # ------------------------------------------------------------------
    def _api(self, method: str, path: str, query: dict) -> None:
        app = self.app
        if path == "/api/health" and method == "GET":
            return self._json({"ok": True, "version": app.cfg.version, "time": time.time()})

        if path == "/api/bootstrap" and method == "GET":
            return self._json(
                {
                    "config": app.cfg.as_public_dict(),
                    "languages": language_options(),
                    "settings": app.settings.public(),
                    "supported_extensions": app.registry.supported(),
                    "stats": app.tasks.stats(),
                    "offline": app.translator.status(),
                    "uptime": time.time() - app.started_at,
                }
            )

        if path == "/api/scan" and method == "POST":
            body = self._body()
            raw = str(body.get("path") or app.cfg.input_dir).strip()
            root = Path(raw).expanduser()
            try:
                root = root.resolve(strict=True)
            except OSError as exc:
                raise ApiError(400, f"扫描路径不可访问：{raw}") from exc
            input_root = app.cfg.input_dir.resolve()
            if root != input_root and not _within(root, input_root):
                raise ApiError(400, "只能扫描输入目录内的文件")
            if not root.is_dir():
                raise ApiError(400, "扫描路径必须是文件夹")
            supported = set(app.registry.supported())
            files = []
            truncated = False
            try:
                children = sorted(root.rglob("*"), key=lambda p: p.as_posix().lower())
                for child in children:
                    if len(files) >= 5000:
                        truncated = True
                        break
                    try:
                        if child.is_symlink() or not child.is_file():
                            continue
                        if child.name.startswith("~$") or child.name.startswith("."):
                            continue
                        if ".part-" in child.name:
                            continue
                        stat = child.stat()
                    except OSError:
                        continue
                    ext = child.suffix.lower()
                    files.append(
                        {
                            "path": str(child),
                            "rel": child.relative_to(root).as_posix(),
                            "name": child.name,
                            "ext": ext,
                            "size": stat.st_size,
                            "modified": stat.st_mtime,
                            "supported": ext in supported,
                        }
                    )
            except OSError as exc:
                raise ApiError(400, f"扫描失败：{exc}") from exc
            return self._json(
                {
                    "root": str(root),
                    "files": files,
                    "total": len(files),
                    "supported": sum(1 for f in files if f["supported"]),
                    "truncated": truncated,
                }
            )

        if path == "/api/tasks" and method == "GET":
            status = _first(query, "status")
            limit = _int(query, "limit", 200)
            tasks = app.tasks.list_tasks(status=status, limit=limit)
            return self._json({"tasks": tasks, "stats": app.tasks.stats()})

        if path == "/api/tasks" and method == "POST":
            body = self._body()
            try:
                task = app.tasks.create(body)
            except TaskValidationError as exc:
                raise ApiError(400, str(exc))
            if bool(body.get("auto_start", True)):
                try:
                    app.manager.start_task(task["id"])
                except TaskStateError as exc:
                    app.bus.log("warn", f"任务创建后自动启动失败：{exc}", task["id"])
            return self._json(task, 201)

        if path == "/api/tasks/batch" and method == "POST":
            body = self._body()
            action = str(body.get("action") or "")
            ids = [str(x) for x in (body.get("ids") or []) if x]
            if action not in ("start", "pause", "cancel", "retry"):
                raise ApiError(400, "未知批量操作")
            if not ids:
                raise ApiError(400, "请选择任务")
            return self._json(app.manager.batch(action, ids))

        if path == "/api/tasks/retry-failed" and method == "POST":
            return self._json(app.manager.retry_all_failed())

        if path == "/api/tasks/clear-completed" and method == "POST":
            return self._json(app.manager.clear_completed())

        match = TASK_ACTION_RE.match(path)
        if match and method == "POST":
            task_id, action = match.group(1), match.group(2)
            try:
                if action == "start":
                    result = app.manager.start_task(task_id)
                elif action == "pause":
                    result = app.manager.pause_task(task_id)
                elif action == "cancel":
                    result = app.manager.cancel_task(task_id)
                else:
                    result = app.manager.retry_task(task_id)
            except KeyError:
                raise ApiError(404, "任务不存在")
            except TaskStateError as exc:
                raise ApiError(409, str(exc))
            return self._json(result)

        match = TASK_RE.match(path)
        if match:
            task_id = match.group(1)
            if method == "GET":
                try:
                    return self._json(app.tasks.task_view(task_id, include_items=True))
                except KeyError:
                    raise ApiError(404, "任务不存在")
            if method == "DELETE":
                try:
                    app.tasks.delete(task_id)
                except KeyError:
                    raise ApiError(404, "任务不存在")
                except TaskValidationError as exc:
                    raise ApiError(409, str(exc))
                return self._json({"ok": True})
            raise ApiError(405, "不支持的请求方法")

        if path == "/api/logs" and method == "GET":
            after = _int(query, "after", 0)
            limit = _int(query, "limit", 300)
            task_id = _first(query, "task_id")
            level = _first(query, "level")
            rows = app.bus.recent(after_id=after, limit=limit, task_id=task_id, level=level)
            return self._json({"logs": rows})

        if path == "/api/logs/stream" and method == "GET":
            return self._stream_logs(query)

        if path == "/api/models" and method == "GET":
            installed = app.offline.installed()
            pairs = {(p["from"], p["to"]) for p in installed}
            return self._json(
                {
                    "installed": installed,
                    "catalog": app.models.catalog_view(pairs),
                    "index_cached": app.cfg.model_index_path.exists(),
                }
            )

        if path == "/api/models/refresh" and method == "POST":
            try:
                catalog = app.models.fetch_catalog(force=True)
            except TranslationError as exc:
                raise ApiError(502, str(exc))
            installed = app.offline.installed()
            pairs = {(p["from"], p["to"]) for p in installed}
            return self._json({"catalog": app.models.catalog_view(pairs), "count": len(catalog)})

        if path == "/api/models/download" and method == "POST":
            body = self._body()
            src = str(body.get("from") or "").strip()
            tgt = str(body.get("to") or "").strip()
            if not src or not tgt:
                raise ApiError(400, "缺少语言包方向")
            return self._json(self._start_download(src, tgt), 202)

        if path == "/api/models/import" and method == "POST":
            if not app.cfg.preload_dir.exists():
                raise ApiError(400, "预加载目录不存在")
            installed = app.models.import_dir(app.cfg.preload_dir, log=app.bus.log)
            app.offline.refresh()
            return self._json({"installed": installed})

        match = MODEL_JOB_RE.match(path)
        if match and method == "GET":
            row = app.db.one("SELECT * FROM download_jobs WHERE id=?", (match.group(1),))
            if not row:
                raise ApiError(404, "下载任务不存在")
            return self._json(row)

        match = MODEL_PAIR_RE.match(path)
        if match and method == "DELETE":
            src, tgt = match.group(1), match.group(2)
            removed = app.models.remove(src, tgt)
            app.offline.refresh()
            return self._json({"removed": removed})

        if path == "/api/settings" and method == "GET":
            return self._json(app.settings.public())

        if path == "/api/settings" and method == "PUT":
            body = self._body()
            try:
                app.settings.update(body)
            except Exception as exc:
                raise ApiError(400, f"设置无效：{exc}")
            app.bus.log("info", "设置已保存")
            return self._json(app.settings.public())

        if path == "/api/ai/test" and method == "POST":
            body = self._body()
            target = str(body.get("target") or app.settings.get().get("default_target", "zh"))
            try:
                sample = app.translator.test_ai(target)
            except AIConfigError as exc:
                raise ApiError(400, str(exc))
            except TranslationError as exc:
                raise ApiError(502, str(exc))
            return self._json({"ok": True, "sample": sample})

        if path == "/api/system" and method == "GET":
            try:
                usage = shutil.disk_usage(str(app.cfg.data_dir))
                disk = {"total": usage.total, "used": usage.used, "free": usage.free}
            except OSError:
                disk = {}
            soffice = shutil.which("soffice") or ""
            return self._json(
                {
                    "version": app.cfg.version,
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "disk": disk,
                    "soffice": soffice,
                    "ocr": ocr_available(),
                    "ocr_languages": list(ocr_languages()),
                    "offline": app.translator.status(),
                    "data_dir": str(app.cfg.data_dir),
                    "uptime": time.time() - app.started_at,
                    "supported_extensions": app.registry.supported(),
                }
            )

        raise ApiError(404, "接口不存在")

    # ------------------------------------------------------------------
    def _start_download(self, src: str, tgt: str) -> dict:
        app = self.app
        if app.offline.pair_installed(src, tgt):
            # Re-downloading is allowed to repair a package.
            pass
        job_id = uuid.uuid4().hex[:16]
        now = time.time()
        app.db.execute(
            "INSERT INTO download_jobs(id,from_code,to_code,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (job_id, src, tgt, "pending", now, now),
        )

        def run():
            app.db.execute(
                "UPDATE download_jobs SET status='running', updated_at=? WHERE id=?",
                (time.time(), job_id),
            )
            try:
                app.models.download(
                    src, tgt,
                    log=lambda level, msg: app.bus.log(level, msg),
                )
                app.offline.refresh()
                app.db.execute(
                    "UPDATE download_jobs SET status='success', updated_at=? WHERE id=?",
                    (time.time(), job_id),
                )
            except Exception as exc:
                app.db.execute(
                    "UPDATE download_jobs SET status='error', error=?, updated_at=? WHERE id=?",
                    (str(exc)[:1500], time.time(), job_id),
                )
                app.bus.log("error", f"语言包下载失败 {src}→{tgt}：{exc}")

        threading.Thread(target=run, daemon=True, name=f"download-{src}-{tgt}").start()
        return {"id": job_id, "status": "pending", "from": src, "to": tgt}

    # ------------------------------------------------------------------
    def _stream_logs(self, query: dict) -> None:
        app = self.app
        last = _int(query, "after", 0)
        header_id = self.headers.get("Last-Event-ID")
        if header_id:
            try:
                last = max(last, int(header_id))
            except ValueError:
                pass
        task_id = _first(query, "task_id")
        level = _first(query, "level")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"event: hello\ndata: {\"ok\":true}\n\n")
            self.wfile.flush()
            while True:
                rows = app.bus.recent(after_id=last, limit=200, task_id=task_id, level=level)
                if rows:
                    for row in rows:
                        last = max(last, int(row["id"]))
                        data = json.dumps(row, ensure_ascii=False)
                        self.wfile.write(
                            f"id: {row['id']}\nevent: log\ndata: {data}\n\n".encode("utf-8")
                        )
                    self.wfile.flush()
                    continue
                changed = app.bus.wait_for_change(last, timeout=12.0)
                if not changed:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    # ------------------------------------------------------------------
    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            raise ApiError(404, "文件不存在")
        if not target.is_file():
            raise ApiError(404, "文件不存在")
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            raise ApiError(400, "Content-Length 无效")
        if length > 2 * 1024 * 1024:
            raise ApiError(413, "请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "请求 JSON 格式错误")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _json(self, data, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        # Keep the console for application logs; HTTP access logs are noisy.
        return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)

    def __init__(self, addr, handler_cls, app: App):
        super().__init__(addr, handler_cls)
        self.app = app


def create_server(app: App) -> Server:
    return Server((app.cfg.host, app.cfg.port), Handler, app)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _first(query: dict, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _int(query: dict, key: str, default: int) -> int:
    try:
        return int((query.get(key) or [str(default)])[0])
    except (TypeError, ValueError):
        return default

"""LinguaFlow entry point: ``python -m app``."""
from __future__ import annotations

import os
import signal
import sys
import threading

from .config import Config
from .db import Database
from .engines.models import ModelStore
from .engines.offline import OfflineEngine
from .formats.registry import build_registry
from .logging_bus import LogBus
from .server import App, create_server
from .settings import SettingsService
from .translator import TranslationManager


def build_app(cfg: Config | None = None) -> App:
    cfg = cfg or Config.from_env()
    cfg.ensure_dirs()
    # Keep LibreOffice and font caches inside the persistent data volume.
    os.environ.setdefault("HOME", str(cfg.home_dir))
    os.environ.setdefault("TMPDIR", str(cfg.temp_dir))
    os.environ.setdefault("SAL_USE_VCLPLUGIN", "svp")

    db = Database(cfg.db_path)
    bus = LogBus(db, max_rows=cfg.max_log_rows)
    settings = SettingsService(db)
    offline = OfflineEngine(cfg.models_dir, logger=bus.log)
    models = ModelStore(cfg.models_dir, cfg.model_index_path, logger=bus.log)
    registry = build_registry(os.environ.get("SOFFICE_PATH", "soffice"))
    translator = TranslationManager(db, offline, bus.log, settings.get)
    app = App(cfg, db, bus, offline, models, registry, translator, settings)
    app.start()
    return app


def main() -> int:
    app = build_app()
    server = create_server(app)
    stop = threading.Event()

    def _stop(signum, _frame):
        if stop.is_set():
            return
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    url = f"http://127.0.0.1:{app.cfg.port}"
    print(f"\n  LinguaFlow {app.cfg.version}  →  {url}\n", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        app.shutdown()
        server.server_close()
        print("LinguaFlow stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

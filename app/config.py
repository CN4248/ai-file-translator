from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser().resolve()


@dataclass
class Config:
    """Runtime configuration. All paths are inside the container by default."""

    data_dir: Path = field(default_factory=lambda: _env_path("DATA_DIR", "/data"))
    input_dir: Path = field(default_factory=lambda: _env_path("INPUT_DIR", "/input"))
    output_dir: Path = field(default_factory=lambda: _env_path("OUTPUT_DIR", "/output"))
    preload_dir: Path = field(default_factory=lambda: _env_path("ARGOS_PRELOAD_DIR", "/opt/argos-preload"))
    host: str = field(default_factory=lambda: os.environ.get("WEB_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("WEB_PORT", "5463")))
    version: str = field(default_factory=lambda: os.environ.get("APP_VERSION", "1.0.0"))
    max_task_workers: int = field(default_factory=lambda: max(1, int(os.environ.get("MAX_TASK_WORKERS", "2"))))
    max_log_rows: int = field(default_factory=lambda: max(1000, int(os.environ.get("MAX_LOG_ROWS", "30000"))))
    args_max_batch_chars: int = field(default_factory=lambda: int(os.environ.get("OFFLINE_MAX_BATCH_CHARS", "900")))
    use_lo_profile: bool = True

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        cfg.ensure_dirs()
        return cfg

    @property
    def db_path(self) -> Path:
        return self.data_dir / "translator.db"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models" / "argos"

    @property
    def model_index_path(self) -> Path:
        return self.data_dir / "models" / "index.json"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def lo_profile_dir(self) -> Path:
        return self.data_dir / "libreoffice"

    @property
    def home_dir(self) -> Path:
        return self.data_dir / "home"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.models_dir,
            self.temp_dir,
            self.lo_profile_dir,
            self.home_dir,
            self.output_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        # /input is intentionally not created/written by the application when mounted read-only.
        if not self.input_dir.exists():
            self.input_dir.mkdir(parents=True, exist_ok=True)

    def as_public_dict(self) -> dict:
        return {
            "version": self.version,
            "input_dir": str(self.input_dir),
            "output_dir": str(self.output_dir),
            "data_dir": str(self.data_dir),
            "port": self.port,
            "max_task_workers": self.max_task_workers,
        }

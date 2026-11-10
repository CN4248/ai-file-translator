from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .base import TranslationError

INDEX_URLS = [
    "https://cdn.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
    "https://fastly.jsdelivr.net/gh/argosopentech/argospm-index@main/index.json",
    "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json",
]


def _download(url: str, dest: Path, progress=None, timeout: int = 60) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "LinguaFlow/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        last = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress and (done - last >= 8 * 1024 * 1024 or done == total):
                progress(done, total)
                last = done


class ModelStore:
    def __init__(self, models_dir: Path, index_path: Path, logger=None):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = Path(index_path)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self._lock = threading.RLock()
        self._catalog: list[dict] | None = None
        self._downloading: set[tuple[str, str]] = set()
        self._jobs: dict[str, dict] = {}

    # ------------------------------------------------------------------
    def load_cached_catalog(self) -> list[dict] | None:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self._catalog = [p for p in data if p.get("links")]
                return self._catalog
        except Exception:
            pass
        return None

    def fetch_catalog(self, force: bool = False) -> list[dict]:
        with self._lock:
            if self._catalog is not None and not force:
                return self._catalog
        if not force:
            cached = self.load_cached_catalog()
            if cached:
                return cached
        errors = []
        for url in INDEX_URLS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "LinguaFlow/1.0"})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, list):
                    raise ValueError("index is not a list")
                catalog = [p for p in data if p.get("links")]
                if not catalog:
                    raise ValueError("index is empty")
                self.index_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
                self._catalog = catalog
                if self.logger:
                    self.logger("info", f"离线语言包目录已更新（{len(catalog)} 个包）")
                return catalog
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        raise TranslationError("无法获取离线语言包目录，请检查网络或手动导入模型。" + " | ".join(errors[:2]))

    def catalog_view(self, installed: set[tuple[str, str]] | None = None) -> list[dict]:
        catalog = self.load_cached_catalog() or []
        installed = installed or set()
        out = []
        for p in catalog:
            out.append(
                {
                    "from": p.get("from_code"),
                    "to": p.get("to_code"),
                    "from_name": p.get("from_name"),
                    "to_name": p.get("to_name"),
                    "version": p.get("package_version"),
                    "link": (p.get("links") or [""])[0],
                    "installed": (p.get("from_code"), p.get("to_code")) in installed,
                }
            )
        return out

    # ------------------------------------------------------------------
    def _find_package_dir(self, root: Path) -> Path | None:
        if (root / "metadata.json").exists():
            return root
        for child in root.iterdir():
            if child.is_dir() and (child / "metadata.json").exists():
                return child
        for meta in root.rglob("metadata.json"):
            return meta.parent
        return None

    def install_archive(self, archive: Path, expected: tuple[str, str] | None = None) -> dict:
        archive = Path(archive)
        tmp = self.models_dir / f".extract-{os.getpid()}-{int(time.time()*1000)}"
        try:
            with zipfile.ZipFile(archive) as zf:
                root = tmp.resolve()
                total = 0
                for info in zf.infolist():
                    target = (tmp / info.filename).resolve()
                    if not target.is_relative_to(root):
                        raise TranslationError("语言包包含非法路径")
                    total += max(0, info.file_size)
                    if total > 4 * 1024**3:
                        raise TranslationError("语言包解压后体积异常")
                if len(zf.infolist()) > 20000:
                    raise TranslationError("语言包文件数量异常")
                zf.extractall(tmp)
            pkg = self._find_package_dir(tmp)
            if pkg is None:
                raise TranslationError("语言包缺少 metadata.json")
            meta = json.loads((pkg / "metadata.json").read_text(encoding="utf-8"))
            src, tgt = str(meta["from_code"]), str(meta["to_code"])
            if expected and (src, tgt) != expected:
                raise TranslationError(f"语言包方向不匹配：期望 {expected[0]}→{expected[1]}，实际 {src}→{tgt}")
            if not (pkg / "sentencepiece.model").exists() or not (pkg / "model").exists():
                raise TranslationError("语言包缺少 sentencepiece.model 或 model/")
            dest = self.models_dir / f"{src}_{tgt}"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(pkg, dest)
            return {"from": src, "to": tgt, "path": str(dest)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def download(self, src: str, tgt: str, log=None) -> dict:
        tmp: Path | None = None
        key = (src, tgt)
        with self._lock:
            if key in self._downloading:
                raise TranslationError("该语言包正在下载中")
            self._downloading.add(key)
        try:
            catalog = self.fetch_catalog()
            match = next((p for p in catalog if p.get("from_code") == src and p.get("to_code") == tgt), None)
            if not match:
                raise TranslationError(f"语言包目录中没有 {src}→{tgt}")
            link = (match.get("links") or [None])[0]
            if not link:
                raise TranslationError("语言包缺少下载地址")
            tmp = self.models_dir / f".download-{src}_{tgt}-{int(time.time())}.zip"
            if log:
                log("info", f"开始下载离线语言包 {src}→{tgt}（{link}）")
            _download(link, tmp, progress=(lambda d, t: log("info", f"下载 {src}→{tgt}: {d/1048576:.1f}/{t/1048576:.1f} MB") if log and t else None))
            size_mb = tmp.stat().st_size / 1048576
            result = self.install_archive(tmp, expected=(src, tgt))
            tmp.unlink(missing_ok=True)
            if log:
                log("success", f"离线语言包 {src}→{tgt} 安装完成（{size_mb:.1f} MB）")
            return result
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            with self._lock:
                self._downloading.discard(key)

    def remove(self, src: str, tgt: str) -> bool:
        dest = self.models_dir / f"{src}_{tgt}"
        if dest.exists():
            shutil.rmtree(dest)
            return True
        return False

    def import_dir(self, source: Path, log=None) -> list[dict]:
        """Install .argosmodel files from a directory (for fully offline setups)."""
        installed = []
        source = Path(source)
        if not source.exists():
            return installed
        for archive in sorted(source.glob("*.argosmodel")):
            try:
                # Avoid re-extracting a large model on every container start.
                with zipfile.ZipFile(archive) as zf:
                    meta_name = next((n for n in zf.namelist() if n.endswith("metadata.json")), None)
                    if meta_name is None:
                        raise TranslationError("语言包缺少 metadata.json")
                    meta = json.loads(zf.read(meta_name).decode("utf-8"))
                dest = self.models_dir / f"{meta.get('from_code')}_{meta.get('to_code')}"
                if dest.exists():
                    continue
                result = self.install_archive(archive)
                installed.append(result)
                if log:
                    log("success", f"已导入离线语言包 {result['from']}→{result['to']}")
            except Exception as exc:
                if log:
                    log("warn", f"导入语言包 {archive.name} 失败：{exc}")
        return installed

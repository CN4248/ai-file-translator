from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import FileResult, FormatHandler, TranslationContext


class OfficeConversionError(RuntimeError):
    pass


def soffice_available(path: str) -> bool:
    return bool(shutil.which(path) or Path(path).exists())


def convert_with_soffice(
    src: Path,
    target_ext: str,
    outdir: Path,
    *,
    soffice_path: str = "soffice",
    export_filter: str | None = None,
    timeout: int = 420,
) -> Path:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix=".lo-profile-", dir=str(outdir)))
    spec = target_ext.lstrip(".")
    if export_filter:
        spec = f"{spec}:{export_filter}"
    cmd = [
        soffice_path,
        "--headless",
        "--norestore",
        "--invisible",
        "--nolockcheck",
        "--nodefault",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to",
        spec,
        "--outdir",
        str(outdir),
        str(src),
    ]
    env = dict(os.environ)
    env["HOME"] = str(profile)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise OfficeConversionError(f"未找到 LibreOffice（{soffice_path}），无法处理旧版 Office 文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise OfficeConversionError(f"LibreOffice 转换超时（>{timeout}s）") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:600]
        raise OfficeConversionError(f"LibreOffice 转换失败：{detail or '未知错误'}")
    candidates = sorted(outdir.glob(f"{src.stem}.*"))
    wanted = [p for p in candidates if p.suffix.lower() == f".{target_ext.lower().lstrip('.')}"]
    if not wanted:
        detail = (proc.stderr or proc.stdout or "").strip()[:600]
        raise OfficeConversionError(f"LibreOffice 未生成 {target_ext} 文件：{detail}")
    return wanted[0]


class LegacyOfficeHandler(FormatHandler):
    """Convert legacy Office formats to OOXML, translate, then convert back."""

    def __init__(self, extension: str, modern_ext: str, inner: FormatHandler, back_filter: str, label: str, soffice_path: str):
        self.extensions = (extension,)
        self.modern_ext = modern_ext
        self.inner = inner
        self.back_filter = back_filter
        self.label = label
        self.soffice_path = soffice_path

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        work = Path(tempfile.mkdtemp(prefix="legacy-", dir=str(Path(dst).parent)))
        try:
            forward_dir = work / "forward"
            back_dir = work / "back"
            source_copy = forward_dir / f"source{Path(src).suffix.lower()}"
            forward_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, source_copy)
            modern = convert_with_soffice(
                source_copy, self.modern_ext, forward_dir, soffice_path=self.soffice_path
            )
            modern_out = work / f"translated{self.modern_ext}"
            self.inner.translate(modern, modern_out, ctx)
            converted_back = convert_with_soffice(
                modern_out,
                self.extensions[0],
                back_dir,
                soffice_path=self.soffice_path,
                export_filter=self.back_filter,
            )
            shutil.copyfile(converted_back, dst)
            return ctx.result()
        finally:
            shutil.rmtree(work, ignore_errors=True)

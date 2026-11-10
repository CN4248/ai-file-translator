from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path, PurePosixPath

from .config import Config
from .db import Database
from .formats.registry import FormatRegistry
from .languages import ensure_supported, filter_codes, language_name
from .logging_bus import LogBus

PLACEHOLDERS = ("{name}", "{stem}", "{ext}", "{lang}")


class TaskValidationError(ValueError):
    pass


def _safe_filename(name: str) -> str:
    name = str(name or "").replace("\\", "/").split("/")[-1].strip()
    name = name.replace("\x00", "")
    if name in ("", ".", ".."):
        raise TaskValidationError("输出文件名无效")
    if len(name) > 180:
        raise TaskValidationError("输出文件名过长（最多 180 字符）")
    return name


def _safe_suffix(suffix: str) -> str:
    suffix = str(suffix or "")
    if "/" in suffix or "\\" in suffix or "\x00" in suffix or ".." in suffix:
        raise TaskValidationError("文件名后缀不能包含路径分隔符")
    return suffix[:60]


class TaskService:
    def __init__(self, db: Database, cfg: Config, bus: LogBus, registry: FormatRegistry):
        self.db = db
        self.cfg = cfg
        self.bus = bus
        self.registry = registry
        self.supported = registry.supported()

    # ------------------------------------------------------------------
    def create(self, payload: dict) -> dict:
        target = ensure_supported(str(payload.get("target_lang") or "zh"))
        source = str(payload.get("source_lang") or "auto")
        if source != "auto":
            source = ensure_supported(source)
        mode = str(payload.get("mode") or "offline_first")
        if mode not in ("offline_first", "ai_first", "offline_only"):
            raise TaskValidationError("未知翻译模式")
        filters = filter_codes(payload.get("source_filter") or [])
        if target:
            filters = [c for c in filters if c != target] if source == "auto" else filters

        input_path = Path(str(payload.get("input_dir") or self.cfg.input_dir)).expanduser()
        output_path = Path(str(payload.get("output_dir") or self.cfg.output_dir)).expanduser()
        try:
            input_path = input_path.resolve(strict=True)
            output_path = output_path.resolve()
        except OSError as exc:
            raise TaskValidationError(f"输入路径不可访问：{input_path}") from exc
        if not input_path.exists():
            raise TaskValidationError(f"输入路径不存在：{input_path}")
        # Never allow an output location that can overwrite (or be mistaken for)
        # the source tree. Overlapping input/output directories are rejected
        # in both directions, including a single file's own parent directory.
        if input_path.is_dir():
            if (
                output_path == input_path
                or _is_within(output_path, input_path)
                or _is_within(input_path, output_path)
            ):
                raise TaskValidationError("输入目录与输出目录不能相同或互相包含（保护源文件）")
        elif output_path == input_path.parent:
            raise TaskValidationError("单文件任务不能输出到源文件所在目录（保护源文件）")
        try:
            output_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TaskValidationError(f"无法创建输出目录：{exc}") from exc
        if not output_path.is_dir():
            raise TaskValidationError("输出路径必须是目录")

        auto_start = bool(payload.get("auto_start", True))
        output_name = str(payload.get("output_name") or "").strip()
        suffix = _safe_suffix(str(payload.get("name_suffix") or ""))
        preserve_tree = bool(payload.get("preserve_tree", True))
        selected = payload.get("files")
        if selected is not None:
            files = self._selected_files(input_path, selected)
        else:
            files = self._scan(input_path)
        if not files:
            raise TaskValidationError(
                "输入路径中没有可翻译的文件（支持：" + "、".join(self.supported[:18]) + " …）"
            )
        if output_name and len(files) > 1 and not any(p in output_name for p in PLACEHOLDERS):
            raise TaskValidationError(
                "批量任务的输出文件名必须包含占位符 {name}/{stem}/{ext}/{lang}，或留空以使用原文件名"
            )

        task_id = uuid.uuid4().hex
        now = time.time()
        name = str(payload.get("name") or "").strip() or (input_path.name if input_path.name else "翻译任务")
        rows = []
        used_outputs: set[str] = set()
        for seq, src_file in enumerate(files):
            root = input_path if input_path.is_dir() else input_path.parent
            rel = src_file.relative_to(root)
            out_rel = self._output_rel(
                rel, src_file, output_name, suffix, preserve_tree, target, len(files)
            )
            out_str = out_rel.as_posix()
            if out_str in used_outputs:
                out_rel = _dedupe(out_rel, used_outputs)
                out_str = out_rel.as_posix()
            used_outputs.add(out_str)
            rows.append(
                (
                    task_id, seq, rel.as_posix(), out_str,
                    src_file.stat().st_size if src_file.exists() else 0,
                )
            )

        self.db.execute(
            "INSERT INTO tasks(id,name,status,target_lang,source_lang,source_filter,mode,input_dir,"
            "output_dir,output_name,name_suffix,preserve_tree,total_files,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, name[:200], "queued" if auto_start else "held", target, source,
                json.dumps(filters, ensure_ascii=False), mode,
                str(input_path), str(output_path), output_name, suffix,
                1 if preserve_tree else 0, len(rows), now, now,
            ),
        )
        self.db.executemany(
            "INSERT INTO items(task_id,seq,rel_path,output_rel,status,size) VALUES(?,?,?,?, 'pending', ?)",
            rows,
        )
        self.bus.log(
            "success",
            f"任务「{name}」已创建：{len(rows)} 个文件，{language_name(source) if source != 'auto' else '自动识别'} → {language_name(target)}",
            task_id,
        )
        return self.task_view(task_id)

    # ------------------------------------------------------------------
    def _scan(self, path: Path) -> list[Path]:
        files: list[Path] = []
        if path.is_file():
            if path.suffix.lower() in self.supported:
                return [path]
            raise TaskValidationError(f"不支持的文件类型：{path.suffix or '无扩展名'}")
        for child in sorted(path.rglob("*")):
            try:
                if child.is_symlink() or not child.is_file():
                    continue
                if child.name.startswith("~$") or child.name.startswith("."):
                    continue
                if ".part-" in child.name:
                    continue
                if child.suffix.lower() in self.supported:
                    files.append(child)
            except OSError:
                continue
        return files

    def _selected_files(self, root: Path, selected) -> list[Path]:
        """Validate an explicit multi-selection made from the /input scan."""
        if not isinstance(selected, list) or not selected:
            raise TaskValidationError("请至少选择一个文件")
        if not root.is_dir():
            raise TaskValidationError("批量选择文件时，输入路径必须是文件夹")
        chosen: dict[str, Path] = {}
        for raw in selected:
            name = str(raw or "").strip()
            if not name:
                continue
            candidate = Path(name).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                candidate = candidate.resolve(strict=True)
            except OSError as exc:
                raise TaskValidationError(f"选中的文件不存在：{name}") from exc
            if candidate.is_symlink() or not candidate.is_file():
                raise TaskValidationError(f"选中的路径不是普通文件：{name}")
            if not _is_within(candidate, root):
                raise TaskValidationError(f"选中的文件不在输入目录内：{name}")
            if candidate.suffix.lower() not in self.supported:
                raise TaskValidationError(f"不支持的文件类型：{candidate.suffix or '无扩展名'}")
            chosen[str(candidate)] = candidate
        if not chosen:
            raise TaskValidationError("请至少选择一个文件")
        return sorted(chosen.values(), key=lambda p: p.relative_to(root).as_posix().lower())

    def _output_rel(
        self,
        rel: Path,
        src: Path,
        output_name: str,
        suffix: str,
        preserve_tree: bool,
        target: str,
        total: int,
    ) -> Path:
        parent = rel.parent if preserve_tree and str(rel.parent) != "." else Path()
        stem = src.stem
        ext = src.suffix
        if output_name:
            filename = output_name
            for key, value in (("{name}", stem), ("{stem}", stem), ("{ext}", ext), ("{lang}", target)):
                filename = filename.replace(key, value)
            filename = _safe_filename(filename)
            # Never let a renamed output claim a different container format:
            # translation preserves the source format, so append the original
            # extension unless the custom name already ends with it.
            if ext and not filename.lower().endswith(ext.lower()):
                filename += ext
        else:
            filename = f"{stem}{suffix}{ext}"
            filename = _safe_filename(filename)
        return PurePosixPath(parent.as_posix(), filename)

    # ------------------------------------------------------------------
    def task_view(self, task_id: str, include_items: bool = False) -> dict:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError(task_id)
        task = _decode_task(task)
        task["progress"] = (
            (task["done_files"] + task["skipped_files"]) / task["total_files"]
            if task["total_files"] else 0.0
        )
        if include_items:
            task["items"] = [
                _decode_item(i)
                for i in self.db.query("SELECT * FROM items WHERE task_id=? ORDER BY seq", (task_id,))
            ]
        return task

    def list_tasks(self, status: str | None = None, limit: int = 200) -> list[dict]:
        if status and status != "all":
            rows = self.db.query(
                "SELECT * FROM tasks WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, max(1, min(limit, 500))),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            )
        return [_decode_task(r) for r in rows]

    def items(self, task_id: str) -> list[dict]:
        return [_decode_item(r) for r in self.db.query(
            "SELECT * FROM items WHERE task_id=? ORDER BY seq", (task_id,)
        )]

    def stats(self) -> dict:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        by_status = {r["status"]: r["n"] for r in rows}
        items = self.db.query("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        by_item = {r["status"]: r["n"] for r in items}
        return {
            "tasks": sum(by_status.values()),
            "by_status": by_status,
            "items": sum(by_item.values()),
            "items_by_status": by_item,
        }

    def delete(self, task_id: str, *, only_completed: bool = False) -> None:
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task:
            raise KeyError(task_id)
        if only_completed and task["status"] != "completed":
            raise TaskValidationError("只能清除已完成任务记录")
        if task["status"] in ("running", "pausing", "cancelling", "queued"):
            raise TaskValidationError("任务仍在运行或排队，请先取消")
        self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.db.execute("DELETE FROM logs WHERE task_id=?", (task_id,))
        self.bus.log("info", f"已清除任务记录「{task['name']}」", echo=False)


def _decode_task(row: dict) -> dict:
    out = dict(row)
    try:
        out["source_filter"] = json.loads(out.get("source_filter") or "[]")
    except (TypeError, ValueError):
        out["source_filter"] = []
    for key in ("pause_requested", "cancel_requested", "preserve_tree"):
        out[key] = bool(out.get(key))
    return out


def _decode_item(row: dict) -> dict:
    out = dict(row)
    out["progress"] = (out["segments_done"] / out["segments_total"]) if out["segments_total"] else 0.0
    return out


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _dedupe(rel: Path, used: set[str]) -> Path:
    stem, ext = rel.stem, rel.suffix
    parent = rel.parent
    i = 2
    while True:
        candidate = PurePosixPath(parent.as_posix(), f"{stem} ({i}){ext}")
        if candidate.as_posix() not in used:
            return candidate
        i += 1

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..db import Database
from ..engines.base import ControlInterrupted, SkipFile
from ..languages import detect_language, language_name, sample_text
from ..translator import TranslationManager


@dataclass
class FileResult:
    total_segments: int = 0
    translated_segments: int = 0
    skipped_segments: int = 0
    warnings: list[str] = field(default_factory=list)


class TranslationContext:
    """Per-file translation context shared by all format handlers."""

    def __init__(
        self,
        *,
        db: Database,
        manager: TranslationManager,
        task: dict,
        item: dict,
        check_control: Callable[[], None],
        target_lang: str,
        source_lang: str,
        source_filter: list[str],
        mode: str,
        log: Callable[..., None],
    ):
        self.db = db
        self.manager = manager
        self.task = task
        self.item = item
        self.task_id = task["id"]
        self.item_id = item["id"]
        self.target_lang = target_lang
        self.requested_source = source_lang
        self.source_lang = "" if source_lang == "auto" else source_lang
        self.source_filter = source_filter
        self.mode = mode
        self._check_control = check_control
        self._log = log
        self.total = 0
        self.done = 0
        self.translated = 0
        self.skipped = 0
        self.warnings: list[str] = []
        self._last_db_update = 0.0
        self._source_resolved = bool(self.source_lang)
        self._source_filter_checked = False

    # ------------------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        self._log(level, message, self.task_id, self.item_id)

    def control(self) -> None:
        self._check_control()

    def begin(self, total: int) -> None:
        self.total = max(0, total)
        self.done = 0
        self.db.execute(
            "UPDATE items SET segments_total=?, segments_done=0 WHERE id=?",
            (self.total, self.item_id),
        )

    def _report(self, done: int, total: int | None = None) -> None:
        self.done = max(self.done, min(done, self.total or done))
        now = time.monotonic()
        if now - self._last_db_update > 0.45 or self.done >= (self.total or 0):
            self.db.execute(
                "UPDATE items SET segments_done=? WHERE id=?",
                (self.done, self.item_id),
            )
            self._last_db_update = now

    def translate_many(self, texts: list[str], *, label: str = "") -> list[str]:
        """Translate a group of segments, updating progress and honouring pause/cancel."""
        self.control()
        count = sum(1 for t in texts if t and t.strip())
        if count == 0:
            return list(texts)
        base = self.done
        try:
            out = self.manager.translate_many(
                texts,
                self.source_lang or "en",
                self.target_lang,
                self.mode,
                task_id=self.task_id,
                item_id=self.item_id,
                control=self.control,
                progress=lambda d, t: self._report(base + int(count * d / max(1, t))),
            )
        except ControlInterrupted:
            raise
        self.done = base + count
        self.translated += count
        self._report(self.done)
        if label:
            self.log("debug", f"{label}: {count} 段")
        return out

    def advance(self, count: int) -> None:
        self.done += max(0, count)
        self._report(self.done)

    # ------------------------------------------------------------------
    def resolve_source(self, sample: str) -> str:
        # A fixed source language forces the translation direction, but the
        # "only translate these languages" filter still applies to the file's
        # actual content.
        if self.source_lang and self.source_filter and not self._source_filter_checked:
            self._source_filter_checked = True
            detected, _ = detect_language(sample_text(sample))
            if detected and detected not in self.source_filter:
                raise SkipFile(
                    f"源语言 {language_name(detected)} 不在筛选范围（仅翻译："
                    + "、".join(language_name(x) for x in self.source_filter)
                    + "）"
                )
        if self._source_resolved:
            code = self.source_lang
        else:
            cleaned = sample_text(sample)
            code, score = detect_language(cleaned)
            if not code:
                code = "en"
                score = 0.0
                self.warnings.append("无法可靠识别源语言，按英语处理")
                self.log("warn", "无法可靠识别源语言，按英语处理")
            self.source_lang = code
            self._source_resolved = True
            self.db.execute("UPDATE items SET source_lang=? WHERE id=?", (code, self.item_id))
            confidence = f"，置信度 {score:.2f}" if 0.0 <= score <= 1.0 else ""
            self.log(
                "info",
                f"检测到源语言：{language_name(code)} ({code}){confidence}",
            )
        if self.source_filter and code not in self.source_filter:
            raise SkipFile(
                f"源语言 {language_name(code)} 不在筛选范围（仅翻译："
                + "、".join(language_name(x) for x in self.source_filter)
                + "）"
            )
        if code == self.target_lang:
            raise SkipFile(f"文件已经是目标语言 {language_name(code)}，无需翻译")
        return code

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.log("warn", message)

    def result(self) -> FileResult:
        return FileResult(
            total_segments=self.total or self.translated,
            translated_segments=self.translated,
            skipped_segments=max(0, (self.total or 0) - self.translated),
            warnings=self.warnings,
        )


class FormatHandler:
    extensions: tuple[str, ...] = ()
    label = "文件"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        raise NotImplementedError

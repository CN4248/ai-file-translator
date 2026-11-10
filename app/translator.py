from __future__ import annotations

import hashlib
import re
import time
from typing import Callable

from .db import Database
from .engines.ai import AIClient, AIConfig
from .engines.base import AIConfigError, ControlInterrupted, TranslationError, UnsupportedPair
from .engines.offline import OfflineEngine

DEFAULT_AI = {
    "enabled": False,
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.2,
    "timeout": 120,
    "polish": True,
}

MODES = ("offline_first", "ai_first", "offline_only")


class TranslationManager:
    def __init__(self, db: Database, offline: OfflineEngine, log, settings_getter):
        self.db = db
        self.offline = offline
        self.log = log
        self.settings_getter = settings_getter

    # ------------------------------------------------------------------
    def ai_config(self) -> AIConfig:
        settings = self.settings_getter() or {}
        ai = dict(DEFAULT_AI)
        ai.update(settings.get("ai") or {})
        return AIConfig(
            base_url=str(ai.get("base_url") or ""),
            api_key=str(ai.get("api_key") or ""),
            model=str(ai.get("model") or ""),
            temperature=float(ai.get("temperature", 0.2)),
            timeout=int(ai.get("timeout", 120)),
        )

    def ai_enabled(self) -> bool:
        settings = self.settings_getter() or {}
        ai = settings.get("ai") or {}
        cfg = self.ai_config()
        return bool(ai.get("enabled")) and bool(cfg.configured)

    def _cache_get(self, engine: str, src: str, tgt: str, text: str) -> str | None:
        key = hashlib.sha256(f"{engine}|{src}|{tgt}|{text}".encode("utf-8")).hexdigest()
        row = self.db.one("SELECT translated FROM cache WHERE key=?", (key,))
        return row["translated"] if row else None

    def _cache_put(self, engine: str, src: str, tgt: str, text: str, translated: str) -> None:
        key = hashlib.sha256(f"{engine}|{src}|{tgt}|{text}".encode("utf-8")).hexdigest()
        self.db.execute(
            "INSERT OR REPLACE INTO cache(key,source,target,engine,text_hash,translated,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (key, src, tgt, engine, hashlib.sha256(text.encode()).hexdigest(), translated, time.time()),
        )

    def purge_cache(self, keep: int = 80000) -> None:
        count = self.db.scalar("SELECT COUNT(*) FROM cache", default=0) or 0
        if count > keep:
            self.db.execute(
                "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY created_at ASC LIMIT ?)",
                (count - keep,),
            )

    def polish_enabled(self, mode: str) -> bool:
        if mode == "offline_only":
            # "offline_only" must never touch the network.
            return False
        settings = self.settings_getter() or {}
        ai = settings.get("ai") or {}
        return bool(ai.get("polish", True)) and self.ai_enabled()

    # ------------------------------------------------------------------
    def translate_many(
        self,
        texts: list[str],
        source: str,
        target: str,
        mode: str = "offline_first",
        *,
        task_id: str | None = None,
        item_id: int | None = None,
        control: Callable[[], None] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[str]:
        if source == target:
            return list(texts)
        if mode not in MODES:
            mode = "offline_first"
        result = list(texts)
        unique: list[str] = []
        seen: set[str] = set()
        for text in texts:
            if text and text.strip() and text not in seen:
                seen.add(text)
                unique.append(text)
        if not unique:
            return result

        ai_cfg = self.ai_config()
        ai_model_key = f"ai:{ai_cfg.base_url}|{ai_cfg.model}"
        offline_key = f"offline:v2:{source}:{target}"
        primary = offline_key if mode in ("offline_first", "offline_only") else ai_model_key

        pending: list[str] = []
        for text in unique:
            # Only consult the cache of the engine that actually runs first.
            # Otherwise an old AI-cache row could silently turn "local first"
            # into "AI first" without any network call or log.
            cached = self._cache_get(primary, source, target, text)
            if cached is not None:
                result = [cached if t == text else t for t in result]
            else:
                pending.append(text)

        total = len(unique)
        done = total - len(pending)
        if progress:
            progress(done, total)

        if pending:
            if mode == "ai_first":
                if self.ai_enabled():
                    translated_map, untranslated = self._run_ai(
                        pending, source, target, ai_cfg, task_id, item_id, control, progress, done, total
                    )
                else:
                    translated_map, untranslated = {}, list(pending)
                for text, value in translated_map.items():
                    result = [value if t == text else t for t in result]
                if untranslated:
                    # AI failed or was unavailable; fall back to the local engine in
                    # short chunks so pause/cancel stays responsive.
                    still: list[str] = []
                    for start_idx in range(0, len(untranslated), 32):
                        if control:
                            control()
                        chunk = untranslated[start_idx:start_idx + 32]
                        try:
                            values = self.offline.translate_batch(chunk, source, target)
                        except UnsupportedPair:
                            values = [""] * len(chunk)
                        except TranslationError:
                            values = [""] * len(chunk)
                        for text, value in zip(chunk, values):
                            if value and value.strip():
                                self._cache_put(offline_key, source, target, text, value)
                                result = [value if t == text else t for t in result]
                            else:
                                still.append(text)
                    if still:
                        raise TranslationError(
                            f"AI 与离线引擎均无法翻译 {len(still)} 段；"
                            "请安装对应离线语言包或检查 AI API 配置"
                        )
            else:
                failed: list[str] = []
                success = 0
                # Keep each inference call short so pause/cancel is responsive and
                # memory use stays bounded on large subtitle/document batches.
                for start in range(0, len(pending), 32):
                    if control:
                        control()
                    chunk = pending[start:start + 32]
                    try:
                        values = self.offline.translate_batch(chunk, source, target)
                    except UnsupportedPair:
                        values = [""] * len(chunk)
                    except TranslationError:
                        values = [""] * len(chunk)
                    for text, value in zip(chunk, values):
                        if value and value.strip():
                            self._cache_put(offline_key, source, target, text, value)
                            result = [value if t == text else t for t in result]
                            success += 1
                        else:
                            failed.append(text)
                    if progress:
                        progress(done + success, total)
                if failed:
                    if mode == "offline_only":
                        raise TranslationError(
                            f"离线引擎无法翻译 {len(failed)} 段，且当前为仅离线模式"
                        )
                    if not self.ai_enabled():
                        raise TranslationError(
                            f"离线引擎无法翻译 {len(failed)} 段；请安装对应离线语言包或配置 AI API"
                        )
                    translated_map, leftover = self._run_ai(
                        failed, source, target, ai_cfg, task_id, item_id, control, progress, done + success, total
                    )
                    for text, value in translated_map.items():
                        result = [value if t == text else t for t in result]
                    if leftover:
                        raise TranslationError(f"{len(leftover)} 段无法翻译（AI 返回为空）")
            if progress:
                progress(total, total)
        if self.polish_enabled(mode):
            result = self._polish_result(
                result, texts, unique, source, target, ai_cfg, task_id, item_id, control
            )
        return result

    @staticmethod
    def _polish_safe(draft: str, polished: str) -> bool:
        """Reject a proofread that drops placeholders, numbers, URLs or tags."""
        if not polished or not polished.strip():
            return False
        patterns = (
            r"\{[^{}]*\}",
            r"\$\{[^}]+\}",
            r"%[0-9]*[sdif]",
            r"\\[Nnh]",
            r"<[^>]+>",
            r"https?://[^\s]+",
            r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
            r"\d+(?:[.,]\d+)*",
        )
        for pattern in patterns:
            for token in re.findall(pattern, draft):
                if token not in polished:
                    return False
        return True

    def _polish_result(
        self,
        result: list[str],
        texts: list[str],
        unique: list[str],
        source: str,
        target: str,
        cfg: AIConfig,
        task_id,
        item_id,
        control,
    ) -> list[str]:
        """Proofread draft translations with AI, preserving a usable fallback.

        A proofread failure never discards the draft; it logs a warning and keeps
        the already translated text. Successful edits are cached by source+draft.
        """
        first: dict[str, str] = {}
        positions: dict[str, list[int]] = {}
        unique_set = set(unique)
        for idx, text in enumerate(texts):
            if text in unique_set:
                positions.setdefault(text, []).append(idx)
        for text, indexes in positions.items():
            value = result[indexes[0]]
            if value and value.strip():
                first[text] = value
        if not first:
            return result

        client = AIClient(cfg)
        polish_key = f"polish:v1:{cfg.base_url}|{cfg.model}"
        pending: list[tuple[str, str]] = []
        for text in unique:
            draft = first.get(text)
            if not draft:
                continue
            cached = self._cache_get(polish_key, source, target, text + "\x00" + draft)
            if cached is not None and cached.strip():
                for idx in positions.get(text, []):
                    result[idx] = cached
            else:
                pending.append((text, draft))
        if not pending:
            return result

        self.log("info", f"AI 校对润色：{len(pending)} 段", task_id, item_id)
        chunks: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        chars = 0
        for pair in pending:
            size = len(pair[0]) + len(pair[1])
            if current and (chars + size > 2600 or len(current) >= 16):
                chunks.append(current)
                current, chars = [], 0
            current.append(pair)
            chars += size
        if current:
            chunks.append(current)

        failures = 0
        polished_count = 0
        rejected = 0
        for chunk in chunks:
            if control:
                control()
            try:
                values = client.polish_batch(chunk, source, target)
            except AIConfigError:
                self.log("warn", "AI 校对不可用，已保留原译文", task_id, item_id)
                break
            except Exception as exc:
                failures += 1
                self.log("warn", f"AI 校对失败，已保留原译文：{str(exc)[:200]}", task_id, item_id)
                if failures >= 2:
                    break
                continue
            for (text, draft), value in zip(chunk, values):
                if not self._polish_safe(draft, value):
                    rejected += 1
                    continue
                self._cache_put(polish_key, source, target, text + "\x00" + draft, value)
                for idx in positions.get(text, []):
                    result[idx] = value
                polished_count += 1
        if rejected:
            self.log(
                "warn",
                f"AI 校对结果缺少占位符/数字/链接，已保留 {rejected} 段原译文",
                task_id,
                item_id,
            )
        if polished_count:
            self.log("success", f"AI 校对完成：{polished_count} 段已润色", task_id, item_id)
        return result

    def _run_ai(
        self,
        texts: list[str],
        source: str,
        target: str,
        cfg: AIConfig,
        task_id,
        item_id,
        control,
        progress,
        done_base,
        total,
    ) -> tuple[dict[str, str], list[str]]:
        """Translate texts with AI.

        Returns ``(text -> translation, failed_texts)`` after per-item retries.
        """
        if not cfg.configured:
            return {}, list(texts)
        client = AIClient(cfg)
        # Chunk by approximate character budget and count.
        chunks: list[list[str]] = []
        current: list[str] = []
        chars = 0
        for text in texts:
            if current and (chars + len(text) > 2200 or len(current) >= 24):
                chunks.append(current)
                current, chars = [], 0
            current.append(text)
            chars += len(text)
        if current:
            chunks.append(current)

        failed: list[str] = []
        translated_map: dict[str, str] = {}
        ai_key = f"ai:{cfg.base_url}|{cfg.model}"
        for chunk in chunks:
            if control:
                control()
            try:
                values = client.translate_batch(chunk, source, target)
            except AIConfigError:
                raise
            except Exception as exc:
                self.log("warn", f"AI 批量翻译失败，尝试逐段重试：{str(exc)[:240]}", task_id, item_id)
                values = []
                for text in chunk:
                    if control:
                        control()
                    try:
                        values.append(client.translate_batch([text], source, target)[0])
                    except Exception as one_exc:
                        self.log("warn", f"AI 单段翻译失败：{str(one_exc)[:200]}", task_id, item_id)
                        values.append("")
            for text, value in zip(chunk, values):
                if value and value.strip():
                    self._cache_put(ai_key, source, target, text, value)
                    translated_map[text] = value
                else:
                    failed.append(text)
        if progress:
            progress(min(total, done_base + len(texts) - len(failed)), total)
        return translated_map, failed

    def test_ai(self, target: str = "zh") -> str:
        cfg = self.ai_config()
        if not cfg.configured:
            raise AIConfigError("请先填写 AI API 地址和模型")
        client = AIClient(cfg)
        return client.test(target)

    def status(self) -> dict:
        installed = self.offline.installed()
        return {
            "offline_available": self.offline.available,
            "offline_packages": len(installed),
            "offline_pairs": installed,
            "ai_configured": self.ai_enabled(),
            "ai_polish": bool((self.settings_getter() or {}).get("ai", {}).get("polish", True)),
            "ai_model": self.ai_config().model,
        }

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .base import AIConfigError, TranslationError

USER_AGENT = "LinguaFlow/1.0"


@dataclass
class AIConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.2
    timeout: int = 120

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())

    def endpoint(self) -> str:
        url = self.base_url.strip().rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/v1"):
            return url + "/chat/completions"
        return url + "/v1/chat/completions"


SYSTEM_PROMPT = """You are a professional document translator.
Translate every input segment into the requested target language.
Rules:
- Return ONLY valid JSON, with the same number of items and the same ids.
- Translate meaning naturally and accurately; keep the original tone.
- Preserve placeholders like {0}, <tag>, URLs, e-mail addresses, numbers, names and line breaks exactly.
- Do not add explanations, Markdown fences, or commentary.
- If a segment is already in the target language, return it unchanged.
"""

POLISH_PROMPT = """You are a meticulous bilingual editor doing a final proofread.
You receive source segments and their draft translations.
Improve the draft's grammar, punctuation, fluency, terminology and natural tone.
Rules:
- Keep the original meaning and do not add or remove information.
- Keep placeholders, numbers, names, URLs, e-mail addresses, tags and line breaks exactly.
- Preserve the target language and formatting; do not translate the source again.
- If the draft is already good, return it unchanged.
- Return ONLY valid JSON with the same number of items and the same ids:
  {"segments": [{"id": 0, "text": "polished translation"}]}
- Do not add explanations, Markdown fences, or commentary.
"""


class AIClient:
    def __init__(self, config: AIConfig):
        self.config = config

    def _post(self, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        cfg = self.config
        if not cfg.configured:
            raise AIConfigError("AI API 未配置 base_url 或 model")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        req = urllib.request.Request(cfg.endpoint(), data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or cfg.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = exc.read().decode("utf-8", "replace")
                parsed = json.loads(body)
                detail = (
                    parsed.get("error", {}).get("message")
                    if isinstance(parsed.get("error"), dict)
                    else parsed.get("error") or parsed.get("message")
                ) or body
            except Exception:
                pass
            detail = str(detail)[:500]
            if exc.code in (401, 403):
                raise AIConfigError(f"AI API 鉴权失败（HTTP {exc.code}）：{detail}") from exc
            raise TranslationError(f"AI API 请求失败（HTTP {exc.code}）：{detail}") from exc
        except urllib.error.URLError as exc:
            raise TranslationError(f"无法连接 AI API：{exc.reason}") from exc
        except TimeoutError as exc:
            raise TranslationError("AI API 请求超时") from exc

    def _complete(self, system: str, user: str, temperature: float | None = None) -> str:
        cfg = self.config
        base_payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": cfg.temperature if temperature is None else temperature,
            "stream": False,
        }
        payload = dict(base_payload, response_format={"type": "json_object"})
        try:
            data = self._post(payload)
        except TranslationError as exc:
            # Some providers (e.g. older Ollama) reject response_format.
            if "response_format" in str(exc) or "json" in str(exc).lower():
                data = self._post(base_payload)
            else:
                raise
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError(f"AI API 返回格式异常：{str(data)[:300]}") from exc

    def _chat(self, items: list[str], source: str, target: str) -> list[str]:
        numbered = [{"id": i, "text": text} for i, text in enumerate(items)]
        user = (
            f"Source language: {source or 'auto-detect'}\n"
            f"Target language: {target}\n"
            f"Translate these {len(items)} segment(s). Return JSON only:\n"
            + json.dumps({"segments": numbered}, ensure_ascii=False)
        )
        content = self._complete(SYSTEM_PROMPT, user)
        return self._parse(content, len(items))

    def polish_batch(self, pairs: list[tuple[str, str]], source: str, target: str) -> list[str]:
        """Proofread (source, draft) pairs and return improved target texts."""
        if not pairs:
            return []
        numbered = [
            {"id": i, "source": source_text, "draft": draft}
            for i, (source_text, draft) in enumerate(pairs)
        ]
        user = (
            f"Source language: {source or 'auto-detect'}\n"
            f"Target language: {target}\n"
            f"Proofread these {len(pairs)} draft translation(s). Return JSON only:\n"
            + json.dumps({"segments": numbered}, ensure_ascii=False)
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                content = self._complete(POLISH_PROMPT, user, temperature=0.1)
                return self._parse(content, len(pairs))
            except AIConfigError:
                raise
            except Exception as exc:  # transient network / malformed response
                last_error = exc
                if attempt == 0:
                    time.sleep(0.8)
        raise TranslationError(f"AI 校对失败：{last_error}")

    @staticmethod
    def _parse(content: str, expected: int) -> list[str]:
        text = (content or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise TranslationError("AI 返回内容不是有效 JSON")
            try:
                obj = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise TranslationError("AI 返回 JSON 解析失败") from exc
        if isinstance(obj, dict):
            if "translations" in obj:
                rows = obj["translations"]
            elif "segments" in obj:
                rows = obj["segments"]
            else:
                rows = obj
        else:
            rows = obj
        if isinstance(rows, dict) and "segments" in rows:
            rows = rows["segments"]
        if isinstance(rows, dict):
            # Also accept {"0": "译文", "1": "译文"} mappings.
            rows = [{"id": key, "text": value} for key, value in rows.items()]
        if not isinstance(rows, list):
            raise TranslationError("AI 返回 JSON 缺少 translations/segments 数组")
        by_id: dict[int, str] = {}
        for idx, row in enumerate(rows):
            if isinstance(row, str):
                by_id[idx] = row
            elif isinstance(row, dict):
                rid = row.get("id", idx)
                value = row.get("text", row.get("translation", row.get("t")))
                if value is None:
                    continue
                try:
                    by_id[int(rid)] = str(value)
                except (TypeError, ValueError):
                    by_id[idx] = str(value)
        out: list[str] = []
        for i in range(expected):
            if i not in by_id:
                raise TranslationError(f"AI 返回缺少第 {i + 1} 段译文")
            out.append(by_id[i])
        return out

    def translate_batch(self, texts: list[str], source: str, target: str) -> list[str]:
        if not texts:
            return []
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return self._chat(texts, source, target)
            except AIConfigError:
                raise
            except Exception as exc:  # network / parse / transient API
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise TranslationError(f"AI 翻译失败：{last_error}")

    def test(self, target: str = "zh") -> str:
        result = self.translate_batch(["Hello, world! This is a connection test."], "en", target)
        return result[0]

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .base import TranslationError, UnsupportedPair

try:
    import ctranslate2
except Exception:  # pragma: no cover
    ctranslate2 = None
try:
    import sentencepiece as spm
except Exception:  # pragma: no cover
    spm = None


@dataclass
class ModelInfo:
    from_code: str
    to_code: str
    path: Path
    name: str = ""


_INSTALLED_RE = re.compile(r"^([a-z]{2,3})_([a-z]{2,3})$")


def _split_long_line(text: str, max_chars: int = 700) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    # Split at sentence punctuation first.
    parts = re.split(r"(?<=[.!?。！？；;])\s+", text)
    out: list[str] = []
    buf = ""
    for part in parts:
        if not part:
            continue
        if len(buf) + len(part) + 1 <= max_chars:
            buf = (buf + " " + part).strip() if buf else part
        else:
            if buf:
                out.append(buf)
            if len(part) <= max_chars:
                buf = part
            else:
                # Hard-boundary split on spaces.
                words = part.split(" ")
                tmp = ""
                for word in words:
                    if len(tmp) + len(word) + 1 <= max_chars:
                        tmp = (tmp + " " + word).strip() if tmp else word
                    else:
                        if tmp:
                            out.append(tmp)
                        tmp = word[:max_chars]
                buf = tmp
    if buf:
        out.append(buf)
    return out or [text]


class OfflineEngine:
    """Direct Argos Translate model runner.

    We use the model package format (sentencepiece + CTranslate2) without the
    heavy argostranslate/stanza/spacy runtime. Inference is fully offline.
    """

    def __init__(self, models_dir: Path, max_loaded: int = 2, cpu_threads: int = 0, logger=None):
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.max_loaded = max_loaded
        self.cpu_threads = cpu_threads
        self.logger = logger
        self._lock = threading.RLock()
        self._cache: OrderedDict[tuple[str, str], tuple[object, object]] = OrderedDict()
        self._pairs: dict[tuple[str, str], ModelInfo] = {}
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> dict[tuple[str, str], ModelInfo]:
        pairs: dict[tuple[str, str], ModelInfo] = {}
        for child in sorted(self.models_dir.iterdir() if self.models_dir.exists() else []):
            if not child.is_dir():
                continue
            meta_path = child / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                src = str(meta.get("from_code", "")).strip()
                tgt = str(meta.get("to_code", "")).strip()
                if not src or not tgt:
                    m = _INSTALLED_RE.match(child.name)
                    if not m:
                        continue
                    src, tgt = m.group(1), m.group(2)
                if not (child / "sentencepiece.model").exists() or not (child / "model").exists():
                    continue
                pairs[(src, tgt)] = ModelInfo(
                    src,
                    tgt,
                    child,
                    f"{meta.get('from_name', src)} → {meta.get('to_name', tgt)}",
                )
            except Exception:
                continue
        with self._lock:
            self._pairs = pairs
            self._cache.clear()
        return pairs

    @property
    def available(self) -> bool:
        return ctranslate2 is not None and spm is not None

    def installed(self) -> list[dict]:
        with self._lock:
            pairs = list(self._pairs.values())
        return sorted(
            [
                {
                    "from": p.from_code,
                    "to": p.to_code,
                    "name": p.name,
                    "path": str(p.path),
                    "size": sum(f.stat().st_size for f in p.path.rglob("*") if f.is_file()),
                }
                for p in pairs
            ],
            key=lambda x: (x["from"], x["to"]),
        )

    def pair_installed(self, src: str, tgt: str) -> bool:
        with self._lock:
            return (src, tgt) in self._pairs

    def route(self, src: str, tgt: str) -> list[tuple[str, str]]:
        if not src or src == tgt:
            return []
        with self._lock:
            pairs = set(self._pairs)
        if (src, tgt) in pairs:
            return [(src, tgt)]
        if (src, "en") in pairs and ("en", tgt) in pairs:
            return [(src, "en"), ("en", tgt)]
        raise UnsupportedPair(
            f"未安装离线语言包 {src}→{tgt}（也没有 {src}→en→{tgt} 中转路径）"
        )

    def can_translate(self, src: str, tgt: str) -> bool:
        try:
            self.route(src, tgt)
            return True
        except UnsupportedPair:
            return False

    def available_targets(self) -> list[str]:
        with self._lock:
            return sorted({b for _a, b in self._pairs})

    # ------------------------------------------------------------------
    def _load(self, src: str, tgt: str):
        if not self.available:
            raise TranslationError("离线引擎未安装（缺少 ctranslate2 / sentencepiece）")
        key = (src, tgt)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            try:
                info = self._pairs[key]
            except KeyError as exc:
                raise UnsupportedPair(f"未安装离线语言包 {src}→{tgt}") from exc
            try:
                sp = spm.SentencePieceProcessor(model_file=str(info.path / "sentencepiece.model"))
                kwargs = {
                    "device": "cpu",
                    "compute_type": "int8",
                    "inter_threads": 1,
                    "intra_threads": self.cpu_threads or 0,
                }
                translator = ctranslate2.Translator(str(info.path / "model"), **kwargs)
            except Exception as exc:
                raise TranslationError(f"加载离线模型失败 {src}→{tgt}: {exc}") from exc
            self._cache[key] = (sp, translator)
            while len(self._cache) > self.max_loaded:
                self._cache.popitem(last=False)
            return sp, translator

    def _translate_one_route(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []
        sp, translator = self._load(src, tgt)
        with self._lock:
            tokenized = [sp.encode(t, out_type=str) for t in texts]
            try:
                results = translator.translate_batch(
                    tokenized,
                    beam_size=3,
                    max_batch_size=24,
                    batch_type="examples",
                    max_decoding_length=768,
                )
            except Exception as exc:
                raise TranslationError(f"离线翻译推理失败: {exc}") from exc
            out = []
            for result in results:
                try:
                    tokens = result.hypotheses[0]
                    # Argos packages share a vocabulary between source and target;
                    # sentencepiece.decode() leaves the metaspace marker in place for
                    # tokens that belong to the target side. Detokenize explicitly.
                    text = "".join(tokens).replace("\u2581", " ").strip()
                    if not text:
                        text = sp.decode(tokens).replace("\u2581", " ").strip()
                    out.append(text)
                except Exception:
                    out.append("")
            return out

    def translate_batch(self, texts: list[str], src: str, tgt: str) -> list[str]:
        if not texts:
            return []
        if src == tgt:
            return list(texts)
        route = self.route(src, tgt)
        # Preserve blank segments and newlines by translating line-by-line.
        units: list[tuple[int, str]] = []
        for idx, text in enumerate(texts):
            if not text or not text.strip():
                continue
            for line in text.split("\n"):
                if line.strip():
                    for chunk in _split_long_line(line):
                        units.append((idx, chunk))
        if not units:
            return list(texts)
        current = [u[1] for u in units]
        for a, b in route:
            current = self._translate_one_route(current, a, b)
        grouped: dict[int, list[str]] = {}
        for (idx, _chunk), translated in zip(units, current):
            grouped.setdefault(idx, []).append(translated)
        out = list(texts)
        for idx, pieces in grouped.items():
            if len(pieces) == 1:
                out[idx] = pieces[0]
            else:
                # Reconstruct approximate line structure.
                original_lines = [l for l in texts[idx].split("\n") if l.strip()]
                if len(pieces) == len(original_lines):
                    out[idx] = "\n".join(pieces)
                else:
                    out[idx] = " ".join(pieces)
        return out

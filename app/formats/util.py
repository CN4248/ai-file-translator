from __future__ import annotations

import re
from pathlib import Path

try:
    from charset_normalizer import from_bytes as _cn_from_bytes
except Exception:  # pragma: no cover
    _cn_from_bytes = None


def read_text(path: Path) -> tuple[str, str]:
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", "replace"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", "replace"), "utf-16"
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", "replace"), "utf-16"
    for enc in ("utf-8", "gb18030", "big5", "shift_jis"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    if _cn_from_bytes is not None:
        best = _cn_from_bytes(raw).best()
        if best is not None:
            return str(best), best.encoding or "utf-8"
    return raw.decode("utf-8", "replace"), "utf-8"


def write_text(path: Path, text: str, source_encoding: str = "utf-8") -> None:
    # Modern players/editors expect UTF-8. Preserve a UTF-8 BOM if the source had one.
    encoding = "utf-8-sig" if source_encoding == "utf-8-sig" else "utf-8"
    Path(path).write_text(text, encoding=encoding, newline="")


def has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


_INLINE_TAG = re.compile(r"(<[^>]*>|\{[^}]*\})")


def translate_inline_markup(text: str, translate_parts) -> str:
    """Translate text outside inline tags while preserving tag order/positions.

    ``translate_parts`` accepts a list of plain strings and returns translations.
    """
    parts = _INLINE_TAG.split(text)
    plain_indexes = [i for i, part in enumerate(parts) if part and not _INLINE_TAG.fullmatch(part)]
    values = translate_parts([parts[i] for i in plain_indexes]) if plain_indexes else []
    for idx, value in zip(plain_indexes, values):
        parts[idx] = value
    return "".join(parts)


def translate_ass_text(text: str, translate_parts) -> str:
    """Translate ASS dialogue text around override tags and hard line breaks."""
    # Split on {\\...} tags, literal \\N / \\n, and \\h.
    tokens = re.split(r"(\{[^}]*\}|\\[Nnh])", text)
    plain = [i for i, tok in enumerate(tokens) if tok and not re.fullmatch(r"(\{[^}]*\}|\\[Nnh])", tok)]
    if not plain:
        return text
    values = translate_parts([tokens[i] for i in plain])
    for idx, value in zip(plain, values):
        tokens[idx] = value
    return "".join(tokens)

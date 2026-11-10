"""OCR helpers for image translation.

Tesseract is used only to obtain text and word boxes.  The translation itself
still goes through the normal offline/AI pipeline, so OCR never sends pixels
anywhere.
"""
from __future__ import annotations

from functools import lru_cache

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

from .languages import normalize_code

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

_CJK_RANGES = (
    (0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF), (0x1100, 0x11FF),
)
_RTL_RANGES = ((0x0590, 0x05FF), (0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
_THAI_RANGE = (0x0E00, 0x0E7F)

try:
    import pytesseract  # type: ignore
    from pytesseract import Output  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None
    Output = None

# Tesseract language codes.  Argos language codes are on the left.
OCR_LANGS: dict[str, str] = {
    "en": "eng",
    "zh": "chi_sim",
    "zt": "chi_tra",
    "ja": "jpn",
    "ko": "kor",
    "fr": "fra",
    "de": "deu",
    "es": "spa",
    "it": "ita",
    "pt": "por",
    "pb": "por",
    "ru": "rus",
    "ar": "ara",
    "he": "heb",
    "th": "tha",
    "vi": "vie",
    "nl": "nld",
    "pl": "pol",
    "tr": "tur",
    "uk": "ukr",
    "fa": "fas",
    "hi": "hin",
    "el": "ell",
    "sv": "swe",
    "da": "dan",
    "fi": "fin",
    "cs": "ces",
    "ro": "ron",
    "hu": "hun",
    "id": "ind",
    "ms": "msa",
    "ca": "cat",
}

# Languages that are useful for mixed-script screenshots.  ``auto`` OCR tries
# all installed packs from this list, avoiding a second OCR pass just to detect
# the source language.
AUTO_ORDER = [
    "eng", "chi_sim", "chi_tra", "jpn", "kor", "tha", "deu", "spa", "fra",
    "ita", "ara", "heb", "por", "rus", "vie", "nld", "pol", "tur", "ukr",
    "fas", "hin", "ell", "swe", "dan", "fin", "ces", "ron", "hun", "ind",
]

_RTL = {"ar", "he", "fa", "ur"}


class OcrUnavailable(RuntimeError):
    pass


@dataclass
class OcrWord:
    text: str
    box: tuple[int, int, int, int]  # left, top, width, height
    confidence: float = 0.0
    line_key: tuple[int, int, int] = (0, 0, 0)


@dataclass
class OcrLine:
    text: str
    box: tuple[int, int, int, int]
    confidence: float
    words: list[OcrWord] = field(default_factory=list)
    # "horizontal" (left-to-right or right-to-left) or "vertical" (columns
    # read top-to-bottom; column order is right-to-left for CJK).
    direction: str = "horizontal"
    column: int = 0


@lru_cache(maxsize=1)
def installed_languages() -> tuple[str, ...]:
    if pytesseract is None:
        return ()
    try:
        return tuple(x for x in pytesseract.get_languages(config="") if x != "osd")
    except Exception:
        return ()


def available() -> bool:
    return pytesseract is not None and "eng" in installed_languages()


def languages_for(source: str = "auto") -> str:
    """Return a ``+`` joined Tesseract language list."""
    installed = set(installed_languages())
    source = normalize_code(source)
    if source and source != "auto":
        lang = OCR_LANGS.get(source)
        if lang and lang in installed:
            return lang
    picked = [x for x in AUTO_ORDER if x in installed]
    if "eng" not in picked and "eng" in installed:
        picked.insert(0, "eng")
    return "+".join(picked or ["eng"])


def _in_range(ch: str, ranges) -> bool:
    cp = ord(ch)
    return any(a <= cp <= b for a, b in ranges)


def _merge_words(words: list[OcrWord], source: str = "auto") -> tuple[str, tuple[int, int, int, int], float]:
    left = min(w.box[0] for w in words)
    top = min(w.box[1] for w in words)
    right = max(w.box[0] + w.box[2] for w in words)
    bottom = max(w.box[1] + w.box[3] for w in words)
    code = normalize_code(source)
    raw = [w.text for w in words]
    joined = " ".join(raw)
    cjk = sum(1 for ch in joined if _in_range(ch, _CJK_RANGES))
    thai = sum(1 for ch in joined if _in_range(ch, (_THAI_RANGE,)))
    rtl = sum(1 for ch in joined if _in_range(ch, _RTL_RANGES))
    latin = sum(1 for ch in joined if ch.isascii() and ch.isalpha())
    if code in {"zh", "zt", "ja", "ko", "th"} or (cjk + thai) > latin:
        text = "".join(raw)
    elif code in {"ar", "he", "fa", "ur"} or rtl > max(1, latin):
        # Tesseract returns Arabic/Hebrew words in visual (left-to-right) order.
        text = " ".join(reversed(raw))
    else:
        text = joined
    conf = sum(w.confidence for w in words) / len(words)
    return text, (left, top, right - left, bottom - top), conf


_PUNCTUATION = {"。", "、", "，", "！", "？", "；", "：", ".", ",", "!", "?", ";", ":", "…"}


def _word_text(text: str) -> bool:
    return any(ch.isalnum() or unicodedata.category(ch).startswith("M") for ch in text) or text in _PUNCTUATION


def _ocr_words(image, lang: str, scale: int, psm: int, min_conf: float) -> list[OcrWord]:
    data = pytesseract.image_to_data(
        image,
        lang=lang,
        config=f"--oem 1 --psm {psm} -c preserve_interword_spaces=1",
        output_type=Output.DICT,
    )
    words: list[OcrWord] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text or not _word_text(text):
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if text in _PUNCTUATION and conf < 60:
            continue
        if conf < min_conf:
            continue
        words.append(
            OcrWord(
                text=text,
                box=(
                    int(data["left"][i]) // scale,
                    int(data["top"][i]) // scale,
                    int(data["width"][i]) // scale,
                    int(data["height"][i]) // scale,
                ),
                confidence=conf,
                line_key=(
                    int(data["block_num"][i]),
                    int(data["par_num"][i]),
                    int(data["line_num"][i]),
                ),
            )
        )
    return words


VERTICAL_LANGS = {
    "ja": "jpn_vert",
    "zh": "chi_sim_vert",
    "zt": "chi_tra_vert",
    "ko": "kor_vert",
}


def vertical_languages_for(source: str) -> str:
    installed = set(installed_languages())
    code = normalize_code(source)
    if code in VERTICAL_LANGS:
        cand = VERTICAL_LANGS[code]
        if cand in installed:
            return cand
        return ""
    # Auto: try every installed vertical model.  Tesseract keeps the first
    # model's charset in practice, but the script-specific models cooperate
    # well enough for CJK screenshots.
    picked = [x for x in VERTICAL_LANGS.values() if x in installed]
    return "+".join(picked)


def _vertical_columns_hocr(image, lang: str, scale: int, source: str) -> list[OcrLine]:
    """Read vertical CJK with Tesseract's dedicated ``*_vert`` models.

    These models return one hOCR line per column, with a tall line bbox.  That
    is far more reliable than reconstructing columns from single-glyph boxes.
    """
    try:
        raw = pytesseract.image_to_pdf_or_hocr(
            image,
            lang=lang,
            config="--oem 1 --psm 3",
            extension="hocr",
        )
    except Exception:
        return []
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return []
    try:
        soup = BeautifulSoup(raw, "html.parser")
    except Exception:
        return []
    built: list[OcrLine] = []
    for idx, node in enumerate(soup.select(".ocr_line")):
        title = node.get("title", "")
        m = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", title)
        if not m:
            continue
        x0, y0, x1, y1 = (int(v) // scale for v in m.groups())
        words: list[OcrWord] = []
        for wn in node.select(".ocrx_word"):
            wt = wn.get("title", "")
            wm = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", wt)
            if not wm:
                continue
            wx0, wy0, wx1, wy1 = (int(v) // scale for v in wm.groups())
            text = wn.get_text(" ", strip=True)
            if not text:
                continue
            cm = re.search(r"x_wconf (\d+)", wt)
            conf = float(cm.group(1)) if cm else 0.0
            words.append(OcrWord(text, (wx0, wy0, wx1 - wx0, wy1 - wy0), conf, (0, 0, idx)))
        if len(words) < 2:
            continue
        joined = " ".join(w.text for w in words)
        cjk = sum(1 for ch in joined if _in_range(ch, _CJK_RANGES))
        latin = sum(1 for ch in joined if ch.isascii() and ch.isalpha())
        text = "".join(w.text for w in words) if cjk >= latin else joined
        text = re.sub(r"\s+", "", text) if cjk >= latin else re.sub(r"\s+", " ", text).strip()
        if len(text) < 2:
            continue
        built.append(
            OcrLine(
                text=text,
                box=(x0, y0, x1 - x0, y1 - y0),
                confidence=sum(w.confidence for w in words) / len(words),
                words=words,
                direction="vertical",
                column=0,
            )
        )
    built.sort(key=lambda ln: -(ln.box[0] + ln.box[2]))
    for i, line in enumerate(built):
        line.column = i
    return built


def _korean_columns(image, scale: int) -> list[OcrLine]:
    """Read Korean vertical columns.

    ``kor_vert`` is trained on pages rotated 90 degrees clockwise, where a
    vertical column becomes one horizontal line.  Rotate a copy, OCR with
    psm 6, then map the line boxes back to the original column geometry.
    """
    if "kor_vert" not in installed_languages():
        return []
    try:
        rotated = image.rotate(90, expand=True)
        raw = pytesseract.image_to_pdf_or_hocr(
            rotated,
            lang="kor_vert",
            config="--oem 1 --psm 6",
            extension="hocr",
        )
    except Exception:
        return []
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return []
    try:
        soup = BeautifulSoup(raw, "html.parser")
    except Exception:
        return []
    scaled_w = rotated.size[1]  # original image width at this scale
    built: list[OcrLine] = []
    for idx, node in enumerate(soup.select(".ocr_line")):
        m = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", node.get("title", ""))
        if not m:
            continue
        rx0, ry0, rx1, ry1 = (int(v) for v in m.groups())
        words: list[OcrWord] = []
        for wn in node.select(".ocrx_word"):
            wm = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", wn.get("title", ""))
            if not wm:
                continue
            wx0, wy0, wx1, wy1 = (int(v) for v in wm.groups())
            ox = scaled_w - 1 - (wy0 + (wy1 - wy0))
            oy = wx0
            ow = wy1 - wy0
            oh = wx1 - wx0
            text = wn.get_text(" ", strip=True)
            if not text:
                continue
            cm = re.search(r"x_wconf (\d+)", wn.get("title", ""))
            conf = float(cm.group(1)) if cm else 0.0
            words.append(
                OcrWord(
                    text,
                    (ox // scale, oy // scale, ow // scale, oh // scale),
                    conf,
                    (0, 0, idx),
                )
            )
        if not words:
            continue
        text = "".join(w.text for w in words)
        text = re.sub(r"\s+", "", text)
        if len(text) < 2:
            continue
        left = min(w.box[0] for w in words)
        top = min(w.box[1] for w in words)
        right = max(w.box[0] + w.box[2] for w in words)
        bottom = max(w.box[1] + w.box[3] for w in words)
        built.append(
            OcrLine(
                text=text,
                box=(left, top, right - left, bottom - top),
                confidence=sum(w.confidence for w in words) / len(words),
                words=words,
                direction="vertical",
                column=0,
            )
        )
    built.sort(key=lambda ln: -(ln.box[0] + ln.box[2]))
    for i, line in enumerate(built):
        line.column = i
    return built


def _vertical_quality(lines: list[OcrLine]) -> float:
    if not lines:
        return 0.0
    text = "".join(ln.text for ln in lines)
    cjk = sum(1 for ch in text if _in_range(ch, _CJK_RANGES))
    if cjk < max(3, len(text) * 0.5):
        return 0.0
    tall = sum(1 for ln in lines if ln.box[3] >= ln.box[2] * 1.6)
    if tall < max(1, len(lines) * 0.6):
        return 0.0
    return sum(len(ln.text) * ln.confidence for ln in lines) / 100.0 + cjk * 0.8


def _vertical_columns(image, lang: str, scale: int, source: str) -> list[OcrLine]:
    code = normalize_code(source)
    if code in VERTICAL_LANGS:
        if code == "ko":
            korean = _korean_columns(image, scale)
            if korean:
                return korean
        vlang = vertical_languages_for(code)
        if vlang:
            return _vertical_columns_hocr(image, vlang, scale, source)
    else:
        # Auto: each vertical model has its own script; running them together
        # makes the first charset win.  Score each separately and pick the
        # strongest tall-column result.
        best: list[OcrLine] = []
        best_score = 0.0
        installed = set(installed_languages())
        for vlang in VERTICAL_LANGS.values():
            if vlang not in installed:
                continue
            cand = _vertical_columns_hocr(image, vlang, scale, source)
            score = _vertical_quality(cand)
            if score > best_score:
                best, best_score = cand, score
        korean = _korean_columns(image, scale)
        kscore = _vertical_quality(korean)
        if kscore > best_score:
            best, best_score = korean, kscore
        if best:
            return best
    return _vertical_columns_fallback(image, lang, scale, source)


def _vertical_columns_fallback(image, lang: str, scale: int, source: str) -> list[OcrLine]:
    """Fallback column recovery when no ``*_vert`` model is installed.

    With ``--psm 6`` Tesseract returns individual glyphs/words with boxes, so
    columns can be rebuilt geometrically: characters sharing an x range and
    stacked downwards form a column, and CJK columns are read right-to-left.
    """
    words = _ocr_words(image, lang, scale, 6, 0.0)
    if len(words) < 4:
        return []
    # Group words by horizontal overlap, not by a fixed x centre: fonts with
    # wide punctuation can shift the centre enough to split a real column.
    columns: list[dict] = []
    for word in sorted(words, key=lambda w: (w.box[0], w.box[1])):
        x, y, w, h = word.box
        placed = False
        for col in columns:
            cx, cy, cw, ch = col["box"]
            overlap = max(0, min(x + w, cx + cw) - max(x, cx))
            if overlap >= 0.42 * max(1, min(w, cw)):
                col["words"].append(word)
                nx, ny = min(x, cx), min(y, cy)
                nr = max(x + w, cx + cw)
                nb = max(y + h, cy + ch)
                col["box"] = (nx, ny, nr - nx, nb - ny)
                placed = True
                break
        if not placed:
            columns.append({"words": [word], "box": (x, y, w, h)})
    cjk = sum(1 for word in words for ch in word.text if _in_range(ch, _CJK_RANGES))
    latin = sum(1 for word in words for ch in word.text if ch.isascii() and ch.isalpha())
    cjk_mode = cjk >= max(3, latin)
    built: list[OcrLine] = []
    for col in columns:
        items = sorted(col["words"], key=lambda w: (w.box[1], w.box[0]))
        if len(items) < 3:
            continue
        left = min(w.box[0] for w in items)
        top = min(w.box[1] for w in items)
        right = max(w.box[0] + w.box[2] for w in items)
        bottom = max(w.box[1] + w.box[3] for w in items)
        widths = sorted(w.box[2] for w in items)
        heights = sorted(w.box[3] for w in items)
        med_w = widths[len(widths) // 2]
        med_h = heights[len(heights) // 2]
        if bottom - top < med_h * 2.0 or right - left > med_w * 1.55:
            continue
        raw = [w.text for w in items]
        text = "".join(raw) if cjk_mode or normalize_code(source) in {"zh", "zt", "ja", "ko", "th"} else " ".join(raw)
        if len(text) < 3:
            continue
        built.append(
            OcrLine(
                text=text,
                box=(left, top, right - left, bottom - top),
                confidence=sum(w.confidence for w in items) / len(items),
                words=items,
                direction="vertical",
                column=0,
            )
        )
    # CJK vertical writing runs right-to-left; assign column numbers after sort.
    built.sort(key=lambda ln: -(ln.box[0] + ln.box[2]))
    for i, line in enumerate(built):
        line.column = i
    return built


def _prefer_vertical(normal: list[OcrLine], vertical: list[OcrLine], source: str) -> bool:
    if not vertical:
        return False
    vchars = sum(len(ln.text) for ln in vertical)
    nchars = sum(len(ln.text) for ln in normal)
    if vchars < 4:
        return False
    if not normal:
        return True
    # Two or more clear columns are a strong signal.  A single long column is
    # accepted only when normal OCR found little usable text.
    if len(vertical) >= 2:
        vscore = sum(len(ln.text) * ln.confidence for ln in vertical)
        nscore = sum(len(ln.text) * ln.confidence for ln in normal)
        if normalize_code(source) in {"zh", "zt", "ja", "ko", "auto", ""}:
            vcjk = sum(1 for ln in vertical for ch in ln.text if _in_range(ch, _CJK_RANGES))
            ncjk = sum(1 for ln in normal for ch in ln.text if _in_range(ch, _CJK_RANGES))
            if vcjk >= max(4, vchars * 0.7) and ncjk < nchars * 0.5:
                return True
        return vscore >= nscore * 0.9
    if len(vertical) == 1:
        line = vertical[0]
        vcjk = sum(1 for ch in line.text if _in_range(ch, _CJK_RANGES))
        ncjk = sum(1 for ln in normal for ch in ln.text if _in_range(ch, _CJK_RANGES))
        tall = line.box[3] >= line.box[2] * 1.8
        if (
            tall
            and vchars >= 4
            and line.confidence >= 70
            and vcjk >= vchars * 0.7
            and ncjk < nchars * 0.5
        ):
            return True
    return nchars <= 2 and vchars >= 4


def _reading_order(lines: list[OcrLine], source: str) -> list[OcrLine]:
    if not lines:
        return lines
    if any(ln.direction == "vertical" for ln in lines):
        return lines
    rtl = any(_in_range(ch, _RTL_RANGES) for ln in lines for ch in ln.text)
    if rtl or normalize_code(source) in {"ar", "he", "fa", "ur"}:
        return sorted(lines, key=lambda ln: (ln.box[1], -(ln.box[0] + ln.box[2])))
    return sorted(lines, key=lambda ln: (ln.box[1], ln.box[0]))


def _normal_lines(image, lang: str, scale: int, source: str, min_confidence: float) -> list[OcrLine]:
    words = _ocr_words(image, lang, scale, 3, min_confidence)
    groups: dict[tuple[int, int, int], list[OcrWord]] = {}
    order: list[tuple[int, int, int]] = []
    for word in words:
        key = word.line_key
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(word)
    # Tesseract can emit a trailing CJK full stop as its own line.  Attach
    # punctuation-only groups to the nearest real line so the period is
    # cleared together with the source text instead of remaining as a stray
    # glyph.
    orphans = [k for k, ws in groups.items() if all(w.text in _PUNCTUATION for w in ws)]
    for key in orphans:
        pwords = groups[key]
        px = min(w.box[0] for w in pwords)
        py = min(w.box[1] for w in pwords)
        ph = max(w.box[3] for w in pwords)
        best = None
        best_dist = None
        for other, cwords in groups.items():
            if other == key or all(w.text in _PUNCTUATION for w in cwords):
                continue
            ox = min(w.box[0] for w in cwords)
            oy = min(w.box[1] for w in cwords)
            oh = max(w.box[3] for w in cwords)
            ow = max(w.box[0] + w.box[2] for w in cwords) - ox
            dx = max(0, max(ox, px) - min(ox + ow, px))
            dy = abs((py + ph / 2) - (oy + oh / 2))
            if dy <= max(oh, ph) * 0.9 and dx <= max(oh, ph) * 2.5:
                dist = dx + dy * 2
                if best is None or dist < best_dist:
                    best, best_dist = other, dist
        if best is not None:
            groups[best].extend(pwords)
            order.remove(key)
            del groups[key]
    order.sort(key=lambda k: (min(w.box[1] for w in groups[k]), min(w.box[0] for w in groups[k])))
    lines: list[OcrLine] = []
    seen: set[str] = set()
    for key in order:
        group = sorted(groups[key], key=lambda w: w.box[0])
        text, box, conf = _merge_words(group, source)
        folded = text.casefold().strip()
        if not folded or folded in seen:
            continue
        seen.add(folded)
        lines.append(OcrLine(text=text, box=box, confidence=conf, words=group))
    return lines


def _score(lines: list[OcrLine]) -> float:
    if not lines:
        return 0.0
    text = sum(len(ln.text) for ln in lines)
    return sum(len(ln.text) * max(0.0, ln.confidence) for ln in lines) / 100.0 + text * 0.15


def extract_lines(image, source: str = "auto", *, min_confidence: float = 20.0) -> list[OcrLine]:
    """OCR an image and return visual lines with word boxes.

    Horizontal lines are grouped by Tesseract's (block, paragraph, line) ids
    and reading order.  For CJK images the dedicated ``*_vert`` models recover
    vertical columns; Korean is OCR'd from a rotated copy.  Vertical candidates
    win only when the geometry is convincing.  Small/low-confidence images are
    retried at 2x.
    """
    if not available():
        raise OcrUnavailable("未安装 Tesseract OCR 或缺少 eng 语言包")
    lang = languages_for(source)
    width, height = image.size

    def analyze(im, scale: int):
        normal = _normal_lines(im, lang, scale, source, min_confidence)
        vertical: list[OcrLine] = []
        if normalize_code(source) in {"zh", "zt", "ja", "ko"} or source in {"auto", ""}:
            vertical = _vertical_columns(im, lang, scale, source)
        return normal, vertical

    lines, vertical = analyze(image, 1)
    chars = sum(len(ln.text) for ln in lines)
    conf = sum(ln.confidence for ln in lines) / len(lines) if lines else 0.0
    if (
        (chars < 3 or conf < 55)
        and max(width, height) < 1600
        and not (vertical and _vertical_quality(vertical) >= 10)
    ):
        up = image.resize((width * 2, height * 2), Image.Resampling.LANCZOS)
        lines2, vertical2 = analyze(up, 2)
        if _score(lines2) > _score(lines):
            lines, vertical = lines2, vertical2
    if _prefer_vertical(lines, vertical, source):
        return vertical
    return _reading_order(lines, source)


def is_rtl(code: str) -> bool:
    return normalize_code(code) in _RTL

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

try:  # optional; the app still works with Unicode heuristics if unavailable
    import langid  # type: ignore
except Exception:  # pragma: no cover
    langid = None

# Canonical codes follow the Argos / LibreTranslate convention.
LANGUAGES: dict[str, tuple[str, str]] = {
    "en": ("English", "English"),
    "zh": ("Chinese (Simplified)", "简体中文"),
    "zt": ("Chinese (Traditional)", "繁體中文"),
    "ja": ("Japanese", "日本語"),
    "ko": ("Korean", "한국어"),
    "fr": ("French", "Français"),
    "de": ("German", "Deutsch"),
    "es": ("Spanish", "Español"),
    "pt": ("Portuguese", "Português"),
    "pb": ("Portuguese (Brazil)", "Português (Brasil)"),
    "it": ("Italian", "Italiano"),
    "ru": ("Russian", "Русский"),
    "uk": ("Ukrainian", "Українська"),
    "pl": ("Polish", "Polski"),
    "nl": ("Dutch", "Nederlands"),
    "sv": ("Swedish", "Svenska"),
    "da": ("Danish", "Dansk"),
    "nb": ("Norwegian", "Norsk"),
    "fi": ("Finnish", "Suomi"),
    "cs": ("Czech", "Čeština"),
    "sk": ("Slovak", "Slovenčina"),
    "sl": ("Slovenian", "Slovenščina"),
    "hu": ("Hungarian", "Magyar"),
    "ro": ("Romanian", "Română"),
    "bg": ("Bulgarian", "Български"),
    "el": ("Greek", "Ελληνικά"),
    "tr": ("Turkish", "Türkçe"),
    "ar": ("Arabic", "العربية"),
    "he": ("Hebrew", "עברית"),
    "fa": ("Persian", "فارسی"),
    "hi": ("Hindi", "हिन्दी"),
    "bn": ("Bengali", "বাংলা"),
    "th": ("Thai", "ไทย"),
    "vi": ("Vietnamese", "Tiếng Việt"),
    "id": ("Indonesian", "Bahasa Indonesia"),
    "ms": ("Malay", "Bahasa Melayu"),
    "tl": ("Tagalog", "Tagalog"),
    "sw": ("Swahili", "Kiswahili"),
    "ca": ("Catalan", "Català"),
    "gl": ("Galician", "Galego"),
    "eu": ("Basque", "Euskara"),
    "et": ("Estonian", "Eesti"),
    "lv": ("Latvian", "Latviešu"),
    "lt": ("Lithuanian", "Lietuvių"),
    "sq": ("Albanian", "Shqip"),
    "az": ("Azerbaijani", "Azərbaycan"),
    "ky": ("Kyrgyz", "Кыргызча"),
    "eo": ("Esperanto", "Esperanto"),
    "ga": ("Irish", "Gaeilge"),
    "ur": ("Urdu", "اردو"),
}

# langid / ISO codes that differ from Argos codes.
ALIASES = {
    "zh-cn": "zh",
    "zh-tw": "zt",
    "zh-hans": "zh",
    "zh-hant": "zt",
    "zh-hk": "zt",
    "zh-sg": "zh",
    "jp": "ja",
    "kr": "ko",
    "cmn": "zh",
    "yue": "zh",
    "pt-br": "pb",
    "no": "nb",
    "nn": "nb",
    "iw": "he",
    "in": "id",
    "ji": "yi",
}

LABEL_TO_CODE = {}
for _code, (_en, _native) in LANGUAGES.items():
    LABEL_TO_CODE[_code.lower()] = _code
    LABEL_TO_CODE[_en.lower()] = _code
    LABEL_TO_CODE[_native.lower()] = _code


def normalize_code(code: str | None) -> str:
    if not code:
        return ""
    c = code.strip().lower().replace("_", "-")
    c = ALIASES.get(c, c)
    if c in LANGUAGES:
        return c
    base = c.split("-")[0]
    return ALIASES.get(base, base)


def language_name(code: str) -> str:
    code = normalize_code(code)
    return LANGUAGES.get(code, (code.upper() or "未知", code))[0]


def language_native(code: str) -> str:
    code = normalize_code(code)
    return LANGUAGES.get(code, (code, code))[1]


def language_options() -> list[dict]:
    return [
        {"code": code, "name": name, "native": native}
        for code, (name, native) in sorted(LANGUAGES.items(), key=lambda kv: (kv[1][0], kv[0]))
    ]


_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")
_ARABIC = re.compile(r"[\u0600-\u06ff]")
_HEBREW = re.compile(r"[\u0590-\u05ff]")
_THAI = re.compile(r"[\u0e00-\u0e7f]")
_DEVANAGARI = re.compile(r"[\u0900-\u097f]")
_LATIN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]")


def _letter_count(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


def detect_language(text: str) -> tuple[str, float]:
    """Best-effort offline language detection.

    Unicode script heuristics win over statistical detection for CJK, since
    short subtitle lines are notoriously hard for n-gram detectors.
    """
    cleaned = re.sub(r"<[^>]+>|\{[^}]*\}", " ", text or "")
    cleaned = cleaned.strip()
    if _letter_count(cleaned) < 2:
        return "", 0.0

    kana = len(_KANA.findall(cleaned))
    han = len(_CJK.findall(cleaned))
    hangul = len(_HANGUL.findall(cleaned))
    if kana >= 1 and (kana + han) >= 2:
        return "ja", min(0.99, 0.6 + kana / max(1, kana + han) * 0.39)
    if hangul >= 2 and hangul >= han * 0.3:
        return "ko", 0.95
    if han >= 2 and kana == 0:
        # Traditional characters that are common only in Traditional Chinese.
        trad = set("這個們來時說會後學國東車書見點麼為與")
        simp = set("这个们来时说会后学国东车书见点么为与")
        t = sum(1 for ch in cleaned if ch in trad)
        s = sum(1 for ch in cleaned if ch in simp)
        return ("zt" if t > s * 1.5 and t >= 2 else "zh"), 0.9
    if len(_CYRILLIC.findall(cleaned)) >= 3:
        if any(ch in cleaned.lower() for ch in "іїєґ"):
            return "uk", 0.9
        return "ru", 0.85
    if len(_ARABIC.findall(cleaned)) >= 3:
        return "ar", 0.9
    if len(_HEBREW.findall(cleaned)) >= 3:
        return "he", 0.9
    if len(_THAI.findall(cleaned)) >= 3:
        return "th", 0.9
    if len(_DEVANAGARI.findall(cleaned)) >= 3:
        return "hi", 0.85

    # Short Latin phrases are frequently misclassified by n-gram detectors
    # ("Hello, world!" was identified as Dutch).  A few unambiguous English
    # function words are a stronger signal for short lines.
    words = set(re.findall(r"[a-z']+", cleaned.lower()))
    en_hits = words & {
        "the", "and", "this", "that", "these", "those", "is", "are", "was",
        "were", "have", "has", "will", "would", "should", "with", "from",
        "hello", "world", "test", "offline", "translation", "translate",
    }
    if en_hits and len(_LATIN.findall(cleaned)) >= 0.9 * max(1, _letter_count(cleaned)):
        return "en", 0.55

    if langid is not None:
        try:
            code, score = langid.classify(cleaned[:6000])
            return normalize_code(code), float(score)
        except Exception:
            pass

    if _LATIN.search(cleaned):
        return "en", 0.2
    return "", 0.0


def sample_text(text: str, limit: int = 8000) -> str:
    """Return text with boilerplate/URLs removed for language detection."""
    text = re.sub(r"https?://\S+", " ", text or "")
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", " ", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    # Prefer prose lines and drop pure numbers/timestamps.
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if _letter_count(line) >= 2:
            lines.append(line)
        if sum(len(x) for x in lines) >= limit:
            break
    return "\n".join(lines)[:limit]


def ensure_supported(code: str) -> str:
    c = normalize_code(code)
    if c not in LANGUAGES:
        raise ValueError(f"不支持的语言代码: {code}")
    return c


def filter_codes(codes: Iterable[str]) -> list[str]:
    out = []
    for c in codes or []:
        n = normalize_code(c)
        if n in LANGUAGES and n not in out:
            out.append(n)
    return out

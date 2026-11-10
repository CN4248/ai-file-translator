from __future__ import annotations

import re
from pathlib import Path

from .base import FileResult, FormatHandler, TranslationContext
from .util import has_letters

_CJK_TARGETS = {"zh", "zh-Hant", "ja", "ko"}
_FONT_CANDIDATES = {
    "cjk": [
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ],
    "latin": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
}
_URL = re.compile(r"^(https?://|www\.)\S+$", re.I)


class PdfHandler(FormatHandler):
    extensions = (".pdf",)
    label = "PDF 文档"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        import pymupdf

        try:
            doc = pymupdf.open(str(src))
        except Exception as exc:
            raise RuntimeError(f"无法打开 PDF：{exc}") from exc
        if doc.needs_pass:
            raise RuntimeError("PDF 已加密，暂不支持翻译加密文档")
        font_path = _pick_font(ctx.target_lang)
        if not font_path:
            raise RuntimeError("找不到可用的 PDF 输出字体（需要 fonts-wqy-microhei 或 DejaVuSans）")

        pages: list[tuple[object, list[dict]]] = []
        sample_parts: list[str] = []
        total = 0
        for page in doc:
            items = _page_blocks(page)
            if not items:
                continue
            pages.append((page, items))
            total += len(items)
            if len(sample_parts) < 60:
                sample_parts.extend(item["text"] for item in items[:10])
        if total == 0:
            from ..engines.base import SkipFile

            doc.close()
            raise SkipFile("PDF 没有可提取的文本层（可能是扫描件，需要先 OCR）")
        ctx.resolve_source("\n".join(sample_parts))
        ctx.begin(total)
        for page, items in pages:
            translated = ctx.translate_many([item["text"] for item in items], label=f"第 {page.number + 1} 页")
            _rewrite_page(page, items, translated, font_path, ctx)
        try:
            doc.subset_fonts()
        except Exception:
            pass
        doc.save(str(dst), garbage=4, deflate=True)
        doc.close()
        return ctx.result()


def _pick_font(target: str) -> str | None:
    kind = "cjk" if target in _CJK_TARGETS else "latin"
    for path in _FONT_CANDIDATES[kind] + _FONT_CANDIDATES["latin"]:
        if Path(path).exists():
            return path
    return None


def _page_blocks(page) -> list[dict]:
    try:
        data = page.get_text("dict", sort=True)
    except TypeError:
        data = page.get_text("dict")
    items = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines = []
        size = 0.0
        color = (0, 0, 0)
        for line in block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                lines.append(text)
            for span in line.get("spans", []):
                if span.get("text", "").strip():
                    if span.get("size", 0) > size:
                        size = float(span.get("size", 0))
                    raw = span.get("color", 0)
                    try:
                        color = tuple(c / 255.0 for c in _int_to_rgb(raw))
                    except Exception:
                        pass
        text = " ".join(line.strip() for line in lines if line.strip())
        text = text.replace("- ", "").strip()
        if not text or not has_letters(text) or _URL.match(text):
            continue
        rect = block.get("bbox")
        if not rect:
            continue
        items.append(
            {
                "text": text,
                "bbox": tuple(rect),
                "size": max(4.0, min(size or 10.0, 48.0)),
                "color": color,
            }
        )
    return items


def _int_to_rgb(value) -> tuple[int, int, int]:
    import pymupdf

    return pymupdf.sRGB_to_rgb(int(value))


def _rewrite_page(page, items: list[dict], translated: list[str], font_path: str, ctx: TranslationContext) -> None:
    import pymupdf

    for item in items:
        page.add_redact_annot(pymupdf.Rect(item["bbox"]), fill=None)
    try:
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    except Exception:
        page.apply_redactions()
    for item, value in zip(items, translated):
        # Never redact a block and then insert nothing: if a backend returned an
        # empty string, preserve the source text so no content is silently lost.
        if not value:
            value = item["text"]
        rect = pymupdf.Rect(item["bbox"])
        if rect.width < 1 or rect.height < 1:
            continue
        size = item["size"]
        text = value
        if ctx.target_lang in {"ar", "fa", "ur", "he"}:
            text = _rtl_text(text)
        inserted = False
        while size >= 1.0:
            try:
                result = page.insert_textbox(
                    rect,
                    text,
                    fontname="LinguaFlow",
                    fontfile=font_path,
                    fontsize=size,
                    color=item["color"],
                    align=0,
                    lineheight=1.0,
                )
            except Exception:
                result = -1
            if result >= 0:
                inserted = True
                break
            size -= 0.5 if size > 4.0 else 0.25
        if not inserted:
            ctx.warn(f"第 {page.number + 1} 页有一段译文区域过小，已用极小字号写入")
            try:
                page.insert_text(
                    (rect.x0, rect.y0 + max(1.0, min(rect.height, 4.0))),
                    text,
                    fontname="LinguaFlow",
                    fontfile=font_path,
                    fontsize=max(1.0, min(3.0, rect.height / 1.4)),
                    color=item["color"],
                )
            except Exception:
                pass


def _rtl_text(text: str) -> str:
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        if any("\u0600" <= ch <= "\u06ff" for ch in text):
            text = arabic_reshaper.reshape(text)
        return get_display(text)
    except Exception:
        return text

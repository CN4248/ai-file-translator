from __future__ import annotations

import io
import math
import re
import tempfile
from pathlib import Path

from ..engines.base import SkipFile
from ..ocr import OcrLine, OcrUnavailable, available as ocr_available, extract_lines, is_rtl
from .base import FileResult, FormatHandler, TranslationContext

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps, features as pil_features
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFont = ImageOps = None
    pil_features = None

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:  # pragma: no cover
    arabic_reshaper = None
    get_display = None

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".gif")
FORMAT_BY_EXT = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".avif": "AVIF",
    ".bmp": "BMP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".gif": "GIF",
}
CJK = {"zh", "zt", "ja", "ko"}
THAI = {"th"}
RTL = {"ar", "he", "fa", "ur"}

FONT_CANDIDATES = {
    "zh": ["/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "zt": ["/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "ja": ["/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "ko": ["/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "ar": ["/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "he": ["/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "fa": ["/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "ur": ["/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "th": ["/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "hi": ["/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "default": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
}


def _font_path(target: str) -> str:
    for path in FONT_CANDIDATES.get(target, []) + FONT_CANDIDATES["default"]:
        if Path(path).exists():
            return path
    return ""


def _raqm() -> bool:
    return bool(pil_features is not None and pil_features.check("raqm"))


def _shape(text: str, target: str) -> str:
    """Fallback shaping for Pillow builds without libraqm."""
    if target in RTL and not _raqm():
        if target in {"ar", "fa", "ur"} and arabic_reshaper is not None:
            try:
                text = arabic_reshaper.reshape(text)
            except Exception:
                pass
        if get_display is not None:
            try:
                text = get_display(text)
            except Exception:
                pass
    return text


def _text_kwargs(target: str) -> dict:
    if target in RTL and _raqm():
        return {"direction": "rtl", "language": "ar" if target in {"ar", "fa", "ur"} else "he"}
    return {}


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1100 <= code <= 0x11FF
        or 0x2E80 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
        or 0x1F300 <= code <= 0x1FAFF
    )


def _wrap_text(text: str, font, draw, max_width: int, target: str, tkw: dict) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    if target in CJK or target in THAI:
        # CJK/Thai can break between characters when a word is too long.
        tokens = list(text.replace(" ", ""))
    else:
        tokens = text.split(" ")
    lines: list[str] = []
    current = ""
    for token in tokens:
        sep = "" if (target in CJK or target in THAI) else " "
        candidate = token if not current else current + sep + token
        if draw.textlength(candidate, font=font, **tkw) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = token
        else:
            # A single token wider than the box: split it by characters.
            chunk = ""
            for ch in token:
                if draw.textlength(chunk + ch, font=font, **tkw) <= max_width:
                    chunk += ch
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            current = chunk
    if current:
        lines.append(current)
    return lines


def _fit_text(draw, text: str, target: str, box_w: int, box_h: int, max_size: int, tkw: dict):
    path = _font_path(target)
    if not path:
        return None, [], 0
    best = None
    for size in range(max(6, max_size), 4, -1):
        font = ImageFont.truetype(path, size)
        lines = _wrap_text(text, font, draw, max(4, box_w), target, tkw)
        if not lines:
            continue
        heights = [draw.textbbox((0, 0), ln, font=font, **tkw)[3] for ln in lines]
        spacing = max(1, int(size * 0.12))
        total_h = sum(heights) + spacing * (len(lines) - 1)
        widths = [draw.textlength(ln, font=font, **tkw) for ln in lines]
        if total_h <= box_h + 2 and max(widths) <= box_w + 2:
            return font, lines, total_h
        if best is None:
            best = (font, lines, total_h)
    return best if best else (None, [], 0)


def _draw_vertical(draw, text: str, box, target: str, fg, max_size: int = 0) -> bool:
    """Draw a translation down a vertical CJK column.

    The original column is treated as a text cell: glyphs stay upright and are
    stacked top-to-bottom, which is what a reader expects for vertical CJK.
    """
    if target not in CJK:
        return False
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return False
    x, y, w, h = box
    if w < 6 or h < 8:
        return False
    # Start with a size that fits width and the per-glyph share of height.
    own = min(int(w * 0.95), max(6, int(h / (len(chars) * 1.08))))
    if max_size:
        own = min(own, max_size)
    max_size = own
    for size in range(max_size, 4, -1):
        font = ImageFont.truetype(_font_path(target), size)
        spacing = max(1, int(size * 0.08))
        heights = []
        widths = []
        for ch in chars:
            bbox = draw.textbbox((0, 0), ch, font=font, anchor="lt")
            widths.append(bbox[2] - bbox[0])
            heights.append(max(1, bbox[3] - bbox[1]))
        total = sum(heights) + spacing * (len(chars) - 1)
        if total > h + 2 or max(widths) > w + 2:
            continue
        cy = y + 2
        for ch, cw, chh in zip(chars, widths, heights):
            draw.text((x + (w - cw) / 2, cy), ch, font=font, fill=fg, anchor="lt")
            cy += chh + spacing
        return True
    return False


def _draw_rotated(canvas, text: str, box, target: str, fg) -> bool:
    """Render a non-CJK translation as a rotated label inside a column."""
    x, y, w, h = box
    if w < 6 or h < 8:
        return False
    path = _font_path(target)
    if not path:
        return False
    tkw = _text_kwargs(target)
    for size in range(max(6, int(w * 0.9)), 4, -1):
        font = ImageFont.truetype(path, size)
        layer = Image.new("RGBA", (max(1, h), max(1, w)), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        bbox = d.textbbox((0, 0), text, font=font, **tkw)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw > h - 4 or th > w - 2:
            continue
        d.text((2 - bbox[0], (w - th) / 2 - bbox[1]), text, font=font, fill=fg, **tkw)
        rotated = layer.rotate(-90, expand=True)
        canvas.paste(rotated, (x, y), rotated)
        return True
    return False


def _estimate_colors(arr, box):
    x, y, w, h = box
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(arr.shape[1], x + w), min(arr.shape[0], y + h)
    if x1 <= x0 or y1 <= y0:
        return (255, 255, 255), (0, 0, 0)
    region = arr[y0:y1, x0:x1]
    borders = []
    if region.shape[0] > 2 and region.shape[1] > 2:
        borders.extend(region[0, :, :].reshape(-1, 3))
        borders.extend(region[-1, :, :].reshape(-1, 3))
        borders.extend(region[:, 0, :].reshape(-1, 3))
        borders.extend(region[:, -1, :].reshape(-1, 3))
    if borders:
        bg = tuple(int(v) for v in np.median(np.stack(borders), axis=0))
    else:
        bg = (255, 255, 255)
    diff = np.abs(region.astype("int16") - np.array(bg, dtype="int16")).sum(axis=2)
    text_pixels = region[diff > 70]
    if len(text_pixels) >= max(3, region.shape[0] * region.shape[1] // 200):
        fg = tuple(int(v) for v in np.median(text_pixels, axis=0))
    else:
        lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        fg = (20, 20, 20) if lum > 128 else (245, 245, 245)
    if abs(fg[0] - bg[0]) + abs(fg[1] - bg[1]) + abs(fg[2] - bg[2]) < 60:
        lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
        fg = (20, 20, 20) if lum > 128 else (245, 245, 245)
    return bg, fg


def _inpaint(arr, line: OcrLine):
    if np is None:
        return arr
    bg, _ = _estimate_colors(arr, line.box)
    x, y, w, h = line.box
    region = arr[max(0, y): min(arr.shape[0], y + h), max(0, x): min(arr.shape[1], x + w)]
    flat = False
    if region.size:
        flat_px = region.reshape(-1, 3)
        med = np.median(flat_px, axis=0)
        share = float(np.mean(np.abs(flat_px.astype("int16") - med).sum(axis=1) < 45))
        if share > 0.55:
            flat = True
            bg = tuple(int(v) for v in med)
    mask = np.zeros(arr.shape[:2], dtype="uint8")
    for word in line.words:
        wx, wy, ww, wh = word.box
        x0, y0 = max(0, wx - 4), max(0, wy - 4)
        x1, y1 = min(arr.shape[1], wx + ww + 4), min(arr.shape[0], wy + wh + 4)
        mask[y0:y1, x0:x1] = 255
    if flat:
        # A solid background is best restored by clearing the whole line box;
        # clearing only glyph boxes leaves descenders/JPEG halos behind.
        x0, y0 = max(0, x - 4), max(0, y - 6)
        x1, y1 = min(arr.shape[1], x + w + 4), min(arr.shape[0], y + h + 6)
        clean_bg = tuple(255 if v >= 250 else v for v in bg)
        arr[y0:y1, x0:x1] = np.array(clean_bg, dtype="uint8")
        return arr
    # Non-flat backgrounds: reconstruct from the rows immediately above and
    # below the line.  On gradients/photos this avoids the visible rectangular
    # patch that cv2.inpaint leaves over a wide text run.
    x0, y0 = max(0, x - 4), max(0, y - 6)
    x1, y1 = min(arr.shape[1], x + w + 4), min(arr.shape[0], y + h + 6)
    if y1 > y0 and x1 > x0:
        above = arr[max(0, y0 - 3):y0]
        below = arr[y1:min(arr.shape[0], y1 + 3)]
        if len(above) and len(below):
            top = np.median(above[:, x0:x1], axis=0).astype("float32")
            bottom = np.median(below[:, x0:x1], axis=0).astype("float32")
            rows = y1 - y0
            t = np.linspace(0.0, 1.0, rows, dtype="float32")[:, None, None]
            patch = top[None, :, :] * (1.0 - t) + bottom[None, :, :] * t
            arr[y0:y1, x0:x1] = np.clip(patch, 0, 255).astype("uint8")
            return arr
    if cv2 is not None and mask.any():
        return cv2.inpaint(arr, mask, 6, cv2.INPAINT_TELEA)
    # Pillow-only fallback: fill each word box with the line's border colour.
    fill = np.array(bg, dtype="uint8")
    for word in line.words:
        wx, wy, ww, wh = word.box
        arr[max(0, wy): wy + wh, max(0, wx): wx + ww] = fill
    return arr


class ImageHandler(FormatHandler):
    extensions = IMAGE_EXTENSIONS
    label = "图片 OCR"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        if Image is None or np is None:
            raise SkipFile("未安装 Pillow/numpy，无法处理图片")
        if not ocr_available():
            raise SkipFile("未安装 Tesseract OCR 或缺少语言包，无法识别图片文字")
        try:
            img = Image.open(src)
        except Exception as exc:
            raise SkipFile(f"无法读取图片：{exc}") from exc
        if getattr(img, "n_frames", 1) > 1:
            raise SkipFile("暂不支持动态 GIF/WebP，请先导出为静态图片")
        # Honour phone/camera EXIF orientation before OCR and drawing.
        img = ImageOps.exif_transpose(img)

        rgb = img.convert("RGB")
        alpha = img.convert("RGBA").getchannel("A") if img.mode in ("RGBA", "LA") else None
        arr = np.array(rgb)
        try:
            lines = extract_lines(rgb, ctx.source_lang or "auto")
        except OcrUnavailable as exc:
            raise SkipFile(str(exc)) from exc
        lines = [ln for ln in lines if any(ch.isalpha() for ch in ln.text)]
        if not lines:
            raise SkipFile("图片中没有识别到可翻译的文字")

        vertical = sum(1 for ln in lines if ln.direction == "vertical")
        if vertical:
            ctx.log("info", f"图片版面：竖排 {vertical} 列 / 横排 {len(lines) - vertical} 行（自动识别）")
        sample = "\n".join(ln.text for ln in lines[:80])
        ctx.resolve_source(sample)
        ctx.begin(len(lines))
        vertical_cap = 0
        vlines = [ln for ln in lines if ln.direction == "vertical"]
        if vlines:
            caps = [
                min(int(ln.box[2] * 0.95), max(6, ln.box[3] // max(1, len(ln.text))))
                for ln in vlines
            ]
            vertical_cap = min(caps)
        translated = ctx.translate_many([ln.text for ln in lines], label="图片文字")

        canvas = Image.fromarray(arr)
        draw = ImageDraw.Draw(canvas)
        changed = 0
        for line, text in zip(lines, translated):
            if not text or not text.strip() or text.strip() == line.text.strip():
                continue
            _, fg = _estimate_colors(arr, line.box)
            arr = _inpaint(arr, line)
            canvas = Image.fromarray(arr)
            draw = ImageDraw.Draw(canvas)
            visual = _shape(text, ctx.target_lang)
            x, y, w, h = line.box
            if line.direction == "vertical":
                drawn = (
                    _draw_vertical(draw, visual, line.box, ctx.target_lang, fg, vertical_cap)
                    if ctx.target_lang in CJK
                    else _draw_rotated(canvas, visual, line.box, ctx.target_lang, fg)
                )
                if drawn:
                    arr = np.array(canvas)
                    changed += 1
                    continue
                ctx.warn(f"竖排文字无法按原方向排版，改为横排：{text[:30]}")
            max_size = max(6, int(h * 0.95))
            tkw = _text_kwargs(ctx.target_lang)
            font, rows, total_h = _fit_text(draw, visual, ctx.target_lang, w, h, max_size, tkw)
            if font is None or not rows:
                ctx.warn(f"图片文字无法排版：{text[:30]}")
                continue
            x0 = max(0, x)
            y0 = max(0, y)
            max_w = max(draw.textlength(r, font=font, **tkw) for r in rows)
            ty = y0 + max(0, (h - total_h) / 2)
            for row in rows:
                if tkw.get("direction") == "rtl":
                    draw.text((x0 + w, ty), row, font=font, fill=fg, anchor="ra", **tkw)
                else:
                    draw.text((x0, ty), row, font=font, fill=fg, **tkw)
                ty += draw.textbbox((0, 0), row, font=font, **tkw)[3] + max(1, int(font.size * 0.12))
            arr = np.array(canvas)
            changed += 1

        out = canvas
        if alpha is not None:
            out = out.convert("RGBA")
            out.putalpha(alpha)
        elif img.mode == "P":
            out = out.convert("P", palette=Image.ADAPTIVE)
        # `dst` is the worker's temporary path and has no meaningful extension,
        # so the source extension decides the output format.
        ext = Path(src).suffix.lower()
        fmt = FORMAT_BY_EXT.get(ext, "PNG")
        if fmt == "JPEG":
            out = out.convert("RGB")
        kwargs = {}
        if fmt == "JPEG":
            kwargs.update(quality=95, subsampling=0, optimize=True)
        elif fmt == "WEBP":
            kwargs.update(quality=95, method=6)
        elif fmt == "AVIF":
            kwargs.update(quality=90)
        elif fmt == "PNG":
            kwargs.update(optimize=True)
        if img.info.get("icc_profile"):
            kwargs["icc_profile"] = img.info["icc_profile"]
        if fmt == "JPEG" and img.info.get("exif"):
            kwargs["exif"] = img.info["exif"]
        try:
            out.save(dst, format=fmt, **kwargs)
        except Exception as exc:
            raise SkipFile(f"无法写入图片 {ext}：{exc}") from exc
        ctx.log("debug", f"图片文字：{changed}/{len(lines)} 行已替换")
        return ctx.result()

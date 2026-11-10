from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from .base import FileResult, FormatHandler, TranslationContext
from .util import has_letters, read_text, translate_ass_text, write_text

_LRC_TAG = re.compile(r"^((?:\s*\[[^\]]+\])*)(.*)$")
_LRC_META = re.compile(r"^\s*\[(ti|ar|al|by|offset|re|ve|length):.*\]\s*$", re.I)
_TIME_LINE = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{1,3}\s*"
    r"(?:-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{1,3}|,\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[,.]\d{1,3})"
)


class TxtHandler(FormatHandler):
    extensions = (".txt", ".text", ".log")
    label = "纯文本"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        text, encoding = read_text(src)
        ctx.resolve_source(text[:4000])
        lines = text.splitlines(keepends=True)
        slots: list[tuple[int, str, str]] = []  # index, body, ending
        for idx, raw in enumerate(lines):
            body = raw.rstrip("\r\n")
            ending = raw[len(body):]
            if body.strip() and has_letters(body):
                slots.append((idx, body, ending))
        if not slots:
            raise_from_no_text()
        ctx.begin(len(slots))
        translated = ctx.translate_many([s[1] for s in slots], label="文本行")
        out_lines = list(lines)
        for (idx, body, ending), value in zip(slots, translated):
            out_lines[idx] = value + ending
        write_text(dst, "".join(out_lines), encoding)
        return ctx.result()


class LrcHandler(FormatHandler):
    extensions = (".lrc",)
    label = "LRC 歌词"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        text, encoding = read_text(src)
        ctx.resolve_source(text[:4000])
        lines = text.splitlines(keepends=True)
        slots = []
        for idx, raw in enumerate(lines):
            body = raw.rstrip("\r\n")
            ending = raw[len(body):]
            if not body.strip() or _LRC_META.match(body):
                continue
            match = _LRC_TAG.match(body)
            prefix, content = (match.group(1), match.group(2)) if match else ("", body)
            if content.strip() and has_letters(content):
                slots.append((idx, prefix, content, ending))
        if not slots:
            raise_from_no_text()
        ctx.begin(len(slots))
        translated = ctx.translate_many([s[2] for s in slots], label="歌词行")
        out_lines = list(lines)
        for (idx, prefix, content, ending), value in zip(slots, translated):
            out_lines[idx] = prefix + value + ending
        write_text(dst, "".join(out_lines), encoding)
        return ctx.result()


class SrtHandler(FormatHandler):
    extensions = (".srt", ".vtt", ".sub", ".sbv")
    label = "字幕"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        text, encoding = read_text(src)
        ctx.resolve_source(text[:4000])
        # Keep block separators intact. Each block is a cue with an optional index,
        # an optional timing line, then one or more text lines.
        blocks = re.split(r"(\r?\n[ \t]*\r?\n)", text)
        line_slots: list[tuple[int, int, str, str]] = []
        for bi in range(0, len(blocks), 2):
            block = blocks[bi]
            raw_lines = block.splitlines(keepends=True)
            timing = -1
            for i, line in enumerate(raw_lines):
                if _TIME_LINE.match(line):
                    timing = i
                    break
            if timing < 0:
                continue  # header/metadata block
            for i in range(timing + 1, len(raw_lines)):
                raw = raw_lines[i]
                body = raw.rstrip("\r\n")
                ending = raw[len(body):]
                if body.strip() and has_letters(body):
                    line_slots.append((bi, i, body, ending))
        if not line_slots:
            raise_from_no_text()
        translated = _translate_markup_batch([s[2] for s in line_slots], ctx, tag_split=True)
        out_blocks = list(blocks)
        for bi in sorted({s[0] for s in line_slots}):
            raw_lines = out_blocks[bi].splitlines(keepends=True)
            for (b2, i, body, ending), value in zip(line_slots, translated):
                if b2 == bi:
                    raw_lines[i] = value + ending
            out_blocks[bi] = "".join(raw_lines)
        write_text(dst, "".join(out_blocks), encoding)
        return ctx.result()


class AssHandler(FormatHandler):
    extensions = (".ass", ".ssa")
    label = "ASS 字幕"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        text, encoding = read_text(src)
        ctx.resolve_source(text[:4000])
        lines = text.splitlines(keepends=True)
        section = ""
        format_fields: list[str] = []
        slots: list[tuple[int, str, str]] = []  # line idx, prefix, text/ending later
        for idx, raw in enumerate(lines):
            body = raw.rstrip("\r\n")
            ending = raw[len(body):]
            stripped = body.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped.lower()
                continue
            if section != "[events]":
                continue
            low = body.lower()
            if low.startswith("format:"):
                format_fields = [f.strip().lower() for f in body.split(":", 1)[1].split(",")]
                continue
            if not (low.startswith("dialogue:")):
                continue
            head, after = body.split(":", 1)
            count = len(format_fields) if format_fields else 10
            parts = after.split(",", count - 1)
            if len(parts) < count:
                continue
            text_index = format_fields.index("text") if "text" in format_fields else len(parts) - 1
            content = parts[text_index]
            if not content.strip() or not has_letters(content):
                continue
            prefix = head + ":" + (",".join(parts[:text_index]) + "," if text_index else "")
            suffix = ",".join(parts[text_index + 1:])
            slots.append((idx, prefix, content, suffix + ending))
        if not slots:
            raise_from_no_text()
        translated = _translate_markup_batch([s[2] for s in slots], ctx, ass=True)
        out_lines = list(lines)
        for (idx, prefix, content, suffix), value in zip(slots, translated):
            out_lines[idx] = prefix + value + suffix
        write_text(dst, "".join(out_lines), encoding)
        return ctx.result()


class CsvHandler(FormatHandler):
    extensions = (".csv", ".tsv")
    label = "CSV 表格"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        text, encoding = read_text(src)
        ctx.resolve_source(text[:4000])
        delimiter = "\t" if src.suffix.lower() == ".tsv" else ","
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        slots: list[tuple[int, int, str]] = []
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                if value and value.strip() and has_letters(value):
                    slots.append((r, c, value))
        if not slots:
            raise_from_no_text()
        ctx.begin(len(slots))
        translated = ctx.translate_many([s[2] for s in slots], label="单元格")
        for (r, c, _), value in zip(slots, translated):
            rows[r][c] = value
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\n")
        writer.writerows(rows)
        Path(dst).write_text(buffer.getvalue(), encoding="utf-8", newline="")
        return ctx.result()


class HtmlHandler(FormatHandler):
    extensions = (".html", ".htm", ".xhtml")
    label = "网页 HTML"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        from bs4 import BeautifulSoup, NavigableString, Doctype, Comment, CData, ProcessingInstruction

        text, _encoding = read_text(src)
        ctx.resolve_source(text[:4000])
        soup = BeautifulSoup(text, "html.parser")
        skip_tags = {"script", "style", "code", "pre", "kbd", "samp", "svg", "math"}
        nodes = []
        for node in soup.find_all(string=True):
            if not isinstance(node, NavigableString):
                continue
            if isinstance(node, (Doctype, Comment, CData, ProcessingInstruction)):
                continue
            parent = node.parent
            if parent is not None and parent.name in skip_tags:
                continue
            raw = str(node)
            stripped = raw.strip()
            if not stripped or not has_letters(stripped):
                continue
            leading = raw[: len(raw) - len(raw.lstrip())]
            trailing = raw[len(raw.rstrip()):]
            nodes.append((node, leading, trailing, stripped))
        if not nodes:
            raise_from_no_text()
        ctx.begin(len(nodes))
        translated = ctx.translate_many([n[3] for n in nodes], label="文本节点")
        for (node, leading, trailing, _), value in zip(nodes, translated):
            node.replace_with(leading + value + trailing)
        Path(dst).write_text(str(soup), encoding="utf-8")
        return ctx.result()


def _translate_markup_batch(lines: list[str], ctx: TranslationContext, tag_split: bool = False, ass: bool = False) -> list[str]:
    """Translate plain fragments across all lines in one batch, preserving markup."""
    if ass:
        splitter = re.compile(r"(\{[^}]*\}|\\[Nnh])")
        tag_re = re.compile(r"(\{[^}]*\}|\\[Nnh])")
    elif tag_split:
        splitter = re.compile(r"(<[^>]*>|\{[^}]*\})")
        tag_re = re.compile(r"(<[^>]*>|\{[^}]*\})")
    else:
        splitter = re.compile(r"(<[^>]*>|\{[^}]*\})")
        tag_re = re.compile(r"(<[^>]*>|\{[^}]*\})")
    split_lines = [splitter.split(line) for line in lines]
    plain_refs: list[tuple[int, int]] = []
    plain_texts: list[str] = []
    for li, parts in enumerate(split_lines):
        for pi, part in enumerate(parts):
            if part and has_letters(part) and not tag_re.fullmatch(part):
                plain_refs.append((li, pi))
                plain_texts.append(part)
    ctx.begin(len(plain_texts))
    translated = ctx.translate_many(plain_texts, label="字幕文本")
    for (li, pi), value in zip(plain_refs, translated):
        split_lines[li][pi] = value
    return ["".join(parts) for parts in split_lines]


def raise_from_no_text():
    from ..engines.base import SkipFile

    raise SkipFile("文件中没有可翻译的文本")

from __future__ import annotations

from pathlib import Path

from .base import FileResult, FormatHandler, TranslationContext
from .util import has_letters


class DocxHandler(FormatHandler):
    extensions = (".docx", ".docm", ".dotx", ".dotm")
    label = "Word 文档"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        from docx import Document
        from docx.oxml.ns import qn

        doc = Document(str(src))
        paragraphs = []
        seen: set[int] = set()

        def add_paragraph(p):
            key = id(p._p)
            if key not in seen and p.text.strip() and has_letters(p.text):
                seen.add(key)
                paragraphs.append(p)

        def walk_tables(tables):
            for table in tables:
                for row in table.rows:
                    for cell in row.cells:
                        for p in cell.paragraphs:
                            add_paragraph(p)
                        walk_tables(cell.tables)

        for p in doc.paragraphs:
            add_paragraph(p)
        walk_tables(doc.tables)
        for section in doc.sections:
            for part in (section.header, section.footer, section.first_page_header,
                         section.first_page_footer, section.even_page_header, section.even_page_footer):
                try:
                    for p in part.paragraphs:
                        add_paragraph(p)
                    walk_tables(part.tables)
                except Exception:
                    continue

        # Text boxes are not exposed as paragraphs by python-docx.
        text_nodes = []
        roots = [doc.element.body]
        for section in doc.sections:
            for part in (section.header, section.footer):
                try:
                    roots.append(part._element)
                except Exception:
                    pass
        for root in roots:
            for txbx in root.iter(qn("w:txbxContent")):
                for node in txbx.iter(qn("w:t")):
                    if node.text and node.text.strip() and has_letters(node.text):
                        text_nodes.append(node)

        sample = "\n".join([p.text for p in paragraphs[:80]] + [n.text for n in text_nodes[:20]])
        if not paragraphs and not text_nodes:
            from ..engines.base import SkipFile

            raise SkipFile("文档中没有可翻译的文本")
        ctx.resolve_source(sample)
        ctx.begin(len(paragraphs) + len(text_nodes))

        values = ctx.translate_many([p.text for p in paragraphs], label="段落")
        for paragraph, value in zip(paragraphs, values):
            _set_paragraph_text(paragraph, value)

        if text_nodes:
            node_values = ctx.translate_many([n.text for n in text_nodes], label="文本框")
            for node, value in zip(text_nodes, node_values):
                raw = node.text or ""
                leading = raw[: len(raw) - len(raw.lstrip())]
                trailing = raw[len(raw.rstrip()):]
                node.text = leading + value + trailing
                node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

        doc.save(str(dst))
        return ctx.result()


def _set_paragraph_text(paragraph, value: str) -> None:
    runs = paragraph.runs
    if runs:
        runs[0].text = value
        for run in runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(value)

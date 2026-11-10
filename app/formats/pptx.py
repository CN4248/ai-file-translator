from __future__ import annotations

from pathlib import Path

from .base import FileResult, FormatHandler, TranslationContext
from .util import has_letters


class PptxHandler(FormatHandler):
    extensions = (".pptx", ".pptm", ".potx", ".potm")
    label = "PowerPoint 幻灯片"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        prs = Presentation(str(src))
        paragraphs = []
        seen: set[int] = set()

        def add_para(p):
            key = id(p._p)
            if key not in seen and p.text.strip() and has_letters(p.text):
                seen.add(key)
                paragraphs.append(p)

        def walk(shapes):
            for shape in shapes:
                try:
                    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                        walk(shape.shapes)
                        continue
                except Exception:
                    pass
                if getattr(shape, "has_text_frame", False):
                    for p in shape.text_frame.paragraphs:
                        add_para(p)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        for cell in row.cells:
                            for p in cell.text_frame.paragraphs:
                                add_para(p)
                if getattr(shape, "has_chart", False):
                    try:
                        title = shape.chart.chart_title
                        if title.has_text_frame:
                            for p in title.text_frame.paragraphs:
                                add_para(p)
                    except Exception:
                        pass

        for slide in prs.slides:
            walk(slide.shapes)
            try:
                for p in slide.notes_slide.notes_text_frame.paragraphs:
                    add_para(p)
            except Exception:
                pass

        if not paragraphs:
            from ..engines.base import SkipFile

            raise SkipFile("演示文稿中没有可翻译的文本")
        ctx.resolve_source("\n".join(p.text for p in paragraphs[:150]))
        ctx.begin(len(paragraphs))
        values = ctx.translate_many([p.text for p in paragraphs], label="幻灯片文本")
        for p, value in zip(paragraphs, values):
            _set_paragraph_text(p, value)
        prs.save(str(dst))
        return ctx.result()


def _set_paragraph_text(paragraph, value: str) -> None:
    runs = paragraph.runs
    if runs:
        runs[0].text = value
        for run in runs[1:]:
            run.text = ""
    else:
        paragraph.add_run()
        paragraph.runs[-1].text = value

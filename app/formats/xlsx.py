from __future__ import annotations

from pathlib import Path

from .base import FileResult, FormatHandler, TranslationContext
from .util import has_letters


class XlsxHandler(FormatHandler):
    extensions = (".xlsx", ".xlsm", ".xltx", ".xltm")
    label = "Excel 工作簿"

    def translate(self, src: Path, dst: Path, ctx: TranslationContext) -> FileResult:
        import openpyxl
        import zipfile

        keep_vba = False
        if src.suffix.lower() in {".xlsm", ".xltm"}:
            try:
                with zipfile.ZipFile(src) as archive:
                    keep_vba = "xl/vbaProject.bin" in archive.namelist()
            except (OSError, zipfile.BadZipFile):
                keep_vba = False
        wb = openpyxl.load_workbook(str(src), data_only=False, keep_vba=keep_vba)
        cells = []
        sample: list[str] = []
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    value = cell.value
                    if not isinstance(value, str) or not value.strip() or not has_letters(value):
                        continue
                    if value.startswith("="):
                        continue
                    cells.append(cell)
                    if len(sample) < 100:
                        sample.append(value)
        if not cells:
            from ..engines.base import SkipFile

            raise SkipFile("工作簿中没有可翻译的文本单元格")
        # Avoid enormous single calls but keep progress meaningful.
        ctx.resolve_source("\n".join(sample))
        ctx.begin(len(cells))
        values = ctx.translate_many([c.value for c in cells], label="单元格")
        for cell, value in zip(cells, values):
            if value:
                cell.value = value
        wb.save(str(dst))
        wb.close()
        return ctx.result()

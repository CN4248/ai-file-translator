from __future__ import annotations

from pathlib import Path

from .base import FormatHandler, TranslationContext
from .docx import DocxHandler
from .image import IMAGE_EXTENSIONS, ImageHandler
from .office import LegacyOfficeHandler
from .pdf import PdfHandler
from .pptx import PptxHandler
from .text import AssHandler, CsvHandler, HtmlHandler, LrcHandler, SrtHandler, TxtHandler
from .xlsx import XlsxHandler

LEGACY = {
    ".doc": (".docx", "docx", "MS Word 97", "Word 97-2003 文档"),
    ".xls": (".xlsx", "xlsx", "MS Excel 97", "Excel 97-2003 工作簿"),
    ".ppt": (".pptx", "pptx", "MS PowerPoint 97", "PowerPoint 97-2003 演示文稿"),
    ".odt": (".docx", "docx", "writer8", "OpenDocument 文本"),
    ".ods": (".xlsx", "xlsx", "calc8", "OpenDocument 表格"),
    ".odp": (".pptx", "pptx", "impress8", "OpenDocument 演示文稿"),
    ".rtf": (".docx", "docx", "Rich Text Format", "RTF 富文本"),
}

SUPPORTED_EXTENSIONS = [
    ".txt", ".text", ".log", ".lrc", ".ass", ".ssa", ".srt", ".vtt", ".sub", ".sbv", ".csv", ".tsv",
    ".html", ".htm", ".xhtml",
    ".docx", ".docm", ".dotx", ".dotm", ".doc",
    ".pdf", ".xlsx", ".xlsm", ".xltx", ".xltm", ".xls",
    ".pptx", ".pptm", ".potx", ".potm", ".ppt",
    ".odt", ".ods", ".odp", ".rtf",
    *IMAGE_EXTENSIONS,
]


class FormatRegistry:
    def __init__(self, handlers: dict[str, FormatHandler]):
        self.handlers = handlers

    def get(self, path: Path | str) -> FormatHandler | None:
        return self.handlers.get(Path(path).suffix.lower())

    def supported(self) -> list[str]:
        return sorted(self.handlers)

    def label_for(self, path: Path | str) -> str:
        handler = self.get(path)
        return handler.label if handler else "未知格式"


def build_registry(soffice_path: str = "soffice") -> FormatRegistry:
    handlers: dict[str, FormatHandler] = {}

    def register(handler: FormatHandler):
        for ext in handler.extensions:
            handlers[ext] = handler

    register(TxtHandler())
    register(LrcHandler())
    register(SrtHandler())
    register(AssHandler())
    register(CsvHandler())
    register(HtmlHandler())
    register(PdfHandler())
    register(ImageHandler())
    docx = DocxHandler()
    xlsx = XlsxHandler()
    pptx = PptxHandler()
    register(docx)
    register(xlsx)
    register(pptx)
    modern = {".docx": docx, ".xlsx": xlsx, ".pptx": pptx}
    for ext, (modern_ext, _kind, back_filter, label) in LEGACY.items():
        register(
            LegacyOfficeHandler(
                ext, modern_ext, modern[modern_ext], back_filter, label, soffice_path
            )
        )
    return FormatRegistry(handlers)

#!/usr/bin/env python3
"""Offline smoke test for format handlers and output naming.

Run with the project dependencies installed:
    PYTHONPATH=. python tests/smoke.py
No Docker, network, LibreOffice or AI endpoint is required; the image OCR
case needs the system Tesseract binary and eng language pack.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import time
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches

from app.config import Config
from app.db import Database
from app.formats.base import TranslationContext
from app.formats.registry import build_registry
from app.logging_bus import LogBus
from app.ocr import available as ocr_available, extract_lines, installed_languages
from app.tasks import TaskService
from app.translator import TranslationManager


class FakeManager:
    def __init__(self):
        self.calls = 0

    def translate_many(self, texts, source, target, mode, **kwargs):
        self.calls += len(texts)
        return [("译:" + t) if t.strip() else t for t in texts]


def make_fixtures(root: Path):
    (root / "note.txt").write_text("Hello world.\nSecond line.", encoding="utf-8")
    (root / "sub.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello subtitle.\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nGoodbye.\n", encoding="utf-8")
    (root / "sub.sbv").write_text(
        "0:00:01.000,0:00:02.000\nHello SBV subtitle.\n\n"
        "0:00:03.000,0:00:04.000\nGoodbye SBV.\n", encoding="utf-8")
    (root / "song.lrc").write_text("[00:01.00]Hello song\n[00:02.00]Second line\n", encoding="utf-8")
    (root / "sub.ass").write_text(
        "[Script Info]\nTitle: test\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello ASS\n",
        encoding="utf-8")
    (root / "data.csv").write_text("name,note\nA,Hello table\n", encoding="utf-8")
    (root / "page.html").write_text(
        "<!doctype html><!-- keep me --><html><body><h1>Hello page</h1><script>alert(1)</script></body></html>",
        encoding="utf-8")

    doc = Document()
    doc.add_heading("Hello heading", 0)
    doc.add_paragraph("Hello paragraph.")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Cell A"
    table.cell(0, 1).text = "Cell B"
    doc.save(root / "manual.docx")

    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Hello excel"
    ws["B1"] = 125000
    wb.save(root / "report.xlsx")

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Hello deck"
    slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text = "Body text"
    prs.save(root / "deck.pptx")

    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 100), "Hello PDF page", fontsize=18)
    pdf.save(root / "guide.pdf")
    pdf.close()

    if ocr_available():
        img = Image.new("RGB", (760, 200), (248, 246, 240))
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
        draw.text((48, 72), "Hello image", font=font, fill=(25, 32, 48))
        img.save(root / "cover.png")


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lf-smoke-") as tmp:
        tmp = Path(tmp)
        src_dir, out_dir = tmp / "in", tmp / "out"
        src_dir.mkdir(); out_dir.mkdir()
        make_fixtures(src_dir)

        db = Database(tmp / "test.db")
        db.execute(
            "INSERT INTO tasks(id,name,status,target_lang,input_dir,output_dir,created_at,updated_at)"
            " VALUES('t','test','running','zh',?,?,?,?)",
            (str(src_dir), str(out_dir), time.time(), time.time()))
        db.execute(
            "INSERT INTO items(task_id,seq,rel_path,output_rel,status) VALUES('t',0,'x','x','running')")
        item_id = db.scalar("SELECT id FROM items LIMIT 1")
        manager = FakeManager()
        registry = build_registry()

        expected = {
            "note.txt": "译:Hello world.", "sub.srt": "译:Hello subtitle.",
            "sub.sbv": "译:Hello SBV subtitle.",
            "song.lrc": "译:Hello song", "sub.ass": "译:Hello ASS",
            "data.csv": "译:Hello table", "page.html": "译:Hello page",
            "manual.docx": "译:Hello paragraph.", "report.xlsx": "译:Hello excel",
            "deck.pptx": "译:Hello deck", "guide.pdf": "译:Hello PDF page",
        }
        if ocr_available():
            expected["cover.png"] = ""
        for name, needle in expected.items():
            src = src_dir / name
            before = hashlib.sha256(src.read_bytes()).hexdigest()
            dst = out_dir / name
            ctx = TranslationContext(
                db=db, manager=manager, task={"id": "t"}, item={"id": item_id},
                check_control=lambda: None, target_lang="zh", source_lang="en",
                source_filter=[], mode="offline_only", log=lambda *a, **k: None)
            try:
                registry.get(src).translate(src, dst, ctx)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            after = hashlib.sha256(src.read_bytes()).hexdigest()
            if before != after:
                failures.append(f"{name}: source changed")
            if not dst.exists():
                failures.append(f"{name}: no output")
                continue
            if name == "cover.png":
                out_img = Image.open(dst)
                if out_img.format != "PNG" or out_img.size != (760, 200):
                    failures.append("cover.png: wrong format/size")
                if dst.read_bytes() == src.read_bytes():
                    failures.append("cover.png: output identical to source")
                continue
            if name.endswith(".pdf"):
                text = "".join(p.get_text() for p in fitz.open(dst))
            elif name.endswith(".docx"):
                text = "\n".join(p.text for p in Document(dst).paragraphs)
            elif name.endswith(".xlsx"):
                text = "\n".join(str(c.value) for row in __import__("openpyxl").load_workbook(dst).active.iter_rows() for c in row if c.value)
            elif name.endswith(".pptx"):
                text = "\n".join(sh.text for s in Presentation(dst).slides for sh in s.shapes if hasattr(sh, "text"))
            else:
                text = dst.read_text(encoding="utf-8", errors="replace")
            if needle not in text:
                failures.append(f"{name}: missing {needle!r} in output")
            if name == "page.html":
                if not text.lstrip().lower().startswith("<!doctype html>"):
                    failures.append("page.html: doctype was not preserved")
                if "<!-- keep me -->" not in text:
                    failures.append("page.html: comment was not preserved")

        # Output name must keep the source extension even if the custom name has none.
        cfg = Config(data_dir=tmp / "cfg", input_dir=src_dir, output_dir=out_dir)
        cfg.ensure_dirs()
        svc = TaskService(db, cfg, LogBus(db), build_registry())
        task = svc.create({
            "name": "naming", "input_dir": str(src_dir / "note.txt"),
            "output_dir": str(out_dir), "target_lang": "zh", "source_lang": "en",
            "mode": "offline_only", "output_name": "renamed", "auto_start": False,
        })
        items = svc.items(task["id"])
        if not items or items[0]["output_rel"] != "renamed.txt":
            failures.append(f"naming: {items[0]['output_rel'] if items else 'no items'!r}")

        # Explicit scan selection: only the chosen files, relative paths preserved.
        selected = svc.create({
            "name": "selected", "input_dir": str(src_dir),
            "output_dir": str(tmp / "selected_out"), "target_lang": "zh",
            "source_lang": "en", "mode": "offline_only", "auto_start": False,
            "files": [str(src_dir / "note.txt"), str(src_dir / "sub.srt")],
        })
        selected_items = svc.items(selected["id"])
        rels = [i["rel_path"] for i in selected_items]
        if selected["total_files"] != 2 or rels != ["note.txt", "sub.srt"]:
            failures.append(f"selection: {selected['total_files']} files, rels={rels!r}")
        if any(i["output_rel"] != i["rel_path"] for i in selected_items):
            failures.append("selection: output tree was not preserved")

        # Vertical CJK layout detection: two columns must be recognized as
        # vertical and read right-to-left.  This is unit-level because not all
        # test machines have the Japanese Tesseract pack.
        if ocr_available() and "jpn" in installed_languages():
            font_path = "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"
            if Path(font_path).exists():
                vertical_img = Image.new("RGB", (420, 520), "white")
                vd = ImageDraw.Draw(vertical_img)
                vf = ImageFont.truetype(font_path, 44)
                for ci, chars in enumerate(("こんにちは世界", "おはようございます")):
                    vx = 300 - ci * 100
                    for k, ch in enumerate(chars):
                        vd.text((vx, 50 + k * 48), ch, font=vf, fill=(20, 20, 20))
                vlines = extract_lines(vertical_img, "ja")
                if len(vlines) < 2 or any(ln.direction != "vertical" for ln in vlines):
                    failures.append(f"vertical OCR: {[(ln.direction, ln.text) for ln in vlines]}")
                elif "世界" not in vlines[0].text or "おはよう" not in vlines[1].text:
                    failures.append(f"vertical OCR text: {[ln.text for ln in vlines]}")

                if "kor_vert" in installed_languages():
                    ko_img = Image.new("RGB", (420, 400), "white")
                    kd = ImageDraw.Draw(ko_img)
                    for k, ch in enumerate("안녕하세요세계"):
                        kd.text((300, 50 + k * 48), ch, font=vf, fill=(20, 20, 20))
                    klines = extract_lines(ko_img, "ko")
                    if len(klines) != 1 or klines[0].direction != "vertical" or "안녕" not in klines[0].text:
                        failures.append(f"korean vertical OCR: {[(ln.direction, ln.text) for ln in klines]}")

        safe = TranslationManager._polish_safe
        if not safe("Total 42 at https://example.test {name}", "总计 42，见 https://example.test {name}"):
            failures.append("polish guard: rejected a valid edit")
        if safe("Total 42", "总计 42"):  # no number dropped -> valid
            pass
        else:
            failures.append("polish guard: rejected an unchanged number")
        if safe("Total 42", "总计"):
            failures.append("polish guard: accepted a dropped number")
        if safe("Use {name}", "使用名称"):
            failures.append("polish guard: accepted a dropped placeholder")

    if failures:
        print("FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("OK: 12 handlers, read-only sources, PDF/docx/xlsx/pptx output, SBV, doctype, image OCR/vertical, selected files, naming, polish guard")
    return 0


if __name__ == "__main__":
    sys.exit(main())

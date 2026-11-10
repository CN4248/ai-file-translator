# 开发自测

无 Docker 依赖的格式处理器冒烟测试：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# 图片 OCR 用例还需要系统 Tesseract：
#   apt-get install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
#     tesseract-ocr-jpn-vert tesseract-ocr-chi-sim-vert \
#     tesseract-ocr-chi-tra-vert tesseract-ocr-kor-vert
PYTHONPATH=. python tests/smoke.py
```

覆盖 TXT/SRT/SBV/LRC/ASS/CSV/HTML/DOCX/XLSX/PPTX/PDF/PNG 十二种格式、源文件哈希不变、输出格式、PDF 中文重排、SBV 时间轴、HTML doctype 保留、图片 OCR 原位重绘、竖排日文/中文/韩文方向识别、自定义输出名保留原扩展名，扫描多选后的显式文件列表（只处理选中文件、保留相对路径），以及 AI 校对的安全校验（数字/占位符/URL 不得丢失）。

2026-11-10 另做过一次全量 35 扩展名矩阵回归（含 `.doc .xls .ppt .odt .ods .odp .rtf`，使用 LibreOffice 25.2.3），结果 35/35 completed、0 failed、0 skipped；9 种图片格式另做矩阵，详见 `TEST_REPORT.md` 第 9、10 节。旧版 Office 转换仍建议在容器首次构建后再跑一次。

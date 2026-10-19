"""附件 PDF → Word（再由 Google 转成 Google 文档）：文字页放文字，图页（平面图、流程图等）放整页图片。

**无锡（shengqiu/wuxi-leads 的 wuxi/attach2doc.py）和湖州（shengqiu/EIA_information 的 tools/attach2doc.py）共用
这一份文件，两边必须逐字节相同**（wuxi-leads 第 3+4 步 workflow 每天比对，不一致会警告）。只依赖 pymupdf、python-docx，
不 import 任何项目内模块；上传 / OCR 调 Google 的部分各自实现（wuxi/leaddoc.py、EIA 的 tools/to_gdoc.py）。

- 文字版 PDF：文字直接从 PDF 文字层读（没有错字）。
- 扫描版 PDF：文字来自 Google OCR（分段识别，见 leaddoc.google_ocr_parts），按段放；每页先用本地 tesseract 粗数字数判断是不是图页；
  含原辅材料 / 设备 / 工程组成 / 燃料等关键表格的页再插一张原页图，第 2 步只读 Drive 时用来核对 OCR。
- 图页判断：字很少，或带「平面图 / 流程图 / 示意图 / 附图」等图题而字不多，或（文字版）图片 / 矢量线条占了大半页。
选 Word 做中间格式，是因为 Google 转换 Word 时图片保留得最稳。
"""
import io
import re
import shutil
import subprocess

import pymupdf
from docx import Document
from docx.shared import Cm, Pt

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
# 关键表格页：扫描件里这些页除了 OCR 文字再插一张原页图，第 2 步只读 Drive 时能对照原图核对数字
# （OCR 常把氩/氧、氮/氢认错）
KEY_WORDS = re.compile(r"原辅材料|原辅料|原料及|主要设备|设备清单|设备一览|工程组成|燃料|能源消耗|能耗|储存设施|储罐")
FIG_WORDS = re.compile(r"平面(布置)?图|布置图|流程图|示意图|位置图|四至图|周边(环境|关系)|附图|监测点位|厂区平面|照片|图\s*\d")
FEW_CHARS = 250          # 一页少于这么多字 → 图页
CAPTION_MAX = 600        # 有图题、且全页不超过这么多字 → 图页


def _local_ocr_chars(page):
    """本地 tesseract 粗识别（只为数字数、找图题，不用作正文）。没装 tesseract 返回 None。"""
    if not shutil.which("tesseract"):
        return None
    png = page.get_pixmap(dpi=90, colorspace=pymupdf.csGRAY).tobytes("png")
    p = subprocess.run(["tesseract", "stdin", "stdout", "-l", "chi_sim", "--psm", "3"],
                       input=png, capture_output=True, timeout=120)
    return re.sub(r"\s+", "", p.stdout.decode("utf-8", "ignore"))


def classify(data, scanned):
    """逐页返回 [{"no": 页码, "text": 文字（文字版才有）, "figure": 是否图页}]。"""
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = []
    for i, page in enumerate(doc):
        area = page.rect.width * page.rect.height or 1
        if not scanned:
            text = page.get_text().strip()
            img_area = sum(r.width * r.height for img in page.get_images(full=True)
                           for r in page.get_image_rects(img[0]))
            dense = len(re.sub(r"\s+", "", text))
            figure = dense < FEW_CHARS or img_area / area > 0.5 or len(page.get_drawings()) > 300 \
                or (dense < CAPTION_MAX and bool(FIG_WORDS.search(text)))
        else:
            text = ""
            rough = _local_ocr_chars(page)
            if rough is None:      # 没有 tesseract：保守起见，按「是图页」处理，至少图看得见
                figure = True
            else:
                figure = len(rough) < FEW_CHARS or (len(rough) < CAPTION_MAX and bool(FIG_WORDS.search(rough)))
        key = scanned and not figure and bool(rough and KEY_WORDS.search(rough))
        pages.append({"no": i + 1, "text": text, "figure": figure, "key": key})
    return pages


def _page_jpeg(doc, no, dpi=130):
    return doc[no - 1].get_pixmap(dpi=dpi).tobytes("jpeg", jpg_quality=75)


def build_docx(title, data, pages, ocr_parts=None, note=""):
    """生成 Word。ocr_parts=[(起页, 止页, 文字)] 时（扫描版）按段放 OCR 文字，再放该段里的图页；
    否则（文字版）逐页放文字，图页在该页文字后放整页图片。返回 bytes。"""
    src = pymupdf.open(stream=data, filetype="pdf")
    d = Document()
    d.styles["Normal"].font.size = Pt(10.5)
    d.add_heading(title, level=1)
    if note:
        d.add_paragraph(note).runs[0].italic = True

    def add_text(text):
        # PDF 文字层里偶尔夹着控制字符，Word（XML）不接受，先去掉（2026-10-19 青岑生物医药的报告表遇到）
        text = _CTRL.sub("", text)
        for line in text.splitlines():
            if line.strip():
                d.add_paragraph(line.strip())

    def add_figure(no):
        d.add_paragraph(f"【第 {no} 页 · 图】")
        d.add_picture(io.BytesIO(_page_jpeg(src, no)), width=Cm(16))

    if ocr_parts is None:
        for p in pages:
            d.add_paragraph(f"—— 第 {p['no']} 页 ——").runs[0].bold = True
            add_text(p["text"])
            if p["figure"]:
                add_figure(p["no"])
    else:
        figs = {p["no"] for p in pages if p["figure"]}
        keys = {p["no"] for p in pages if p.get("key")}
        for a, b, text in ocr_parts:
            d.add_heading(f"第 {a}–{b} 页", level=2)
            add_text(text)
            for no in range(a, b + 1):
                if no in figs:
                    add_figure(no)
                elif no in keys:
                    d.add_paragraph(f"【第 {no} 页 · 关键表格原图（核对 OCR 用）】")
                    d.add_picture(io.BytesIO(src[no - 1].get_pixmap(dpi=150).tobytes("jpeg", jpg_quality=80)),
                                  width=Cm(16))
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def is_scanned_pdf(data, pages=10, min_chars=50):
    """抽前几页文字层，平均每页不到 min_chars 个字 → 扫描件（图片）。打不开的也按扫描件处理（存原文件）。"""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        n = min(len(doc), pages)
        chars = sum(len(doc[i].get_text().strip()) for i in range(n))
        return n == 0 or chars / n < min_chars
    except Exception:  # noqa: BLE001
        return True


OCR_PAGES, OCR_MAX_BYTES = 10, 1_900_000   # Google 转换 PDF 时的 OCR 限制：约 10 页、2MB 以内


def ocr_chunks(data):
    """扫描版 PDF → 一段段小 PDF（每段 ≤10 页、≤1.9MB，页面压成灰度 JPEG），返回 [(起页, 止页, bytes)]。"""
    src = pymupdf.open(stream=data, filetype="pdf")
    out, start = [], 0
    while start < len(src):
        n = min(OCR_PAGES, len(src) - start)
        for dpi, quality in ((150, 70), (120, 60), (100, 50), (80, 45)):
            doc = pymupdf.open()
            for i in range(start, start + n):
                page = src[i]
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
                new = doc.new_page(width=page.rect.width, height=page.rect.height)
                new.insert_image(new.rect, stream=pix.tobytes("jpeg", jpg_quality=quality))
            buf = doc.tobytes(garbage=3, deflate=True)
            if len(buf) <= OCR_MAX_BYTES:
                break
        if len(buf) > OCR_MAX_BYTES and n > 1:   # 最低画质还超，就减页数重来
            n = max(1, n // 2)
            continue
        out.append((start + 1, start + n, buf))
        start += n
    return out


OCR_NOTE = ("本文档由扫描件自动生成：文字是 Google OCR 识别结果，仅供检索和快速浏览，中文 OCR 会有错字"
            "（如「氩/氧」「氮/氢」），数字以原件为准；平面图、流程图等图页按原样插入。")

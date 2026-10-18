#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EIA 线索同步的共享层：解压、文件名还原、数据加载、正文渲染。

tools/to_drive.py（Google Drive）和 tools/to_confluence.py（Confluence）
共用本模块；两个工具各自只负责「上传到哪」和 state 记账。
这里只放纯函数和数据装配，不做任何上传动作。
"""
import csv
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "data", "oss_links.csv")
ITEMS_PATH = os.path.join(ROOT, "data", "items.json")
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
ARCHIVE_EXT = (".zip", ".rar", ".7z")
TEXT_NAME = "网页内容.txt"
CST = timezone(timedelta(hours=8))


def log(msg):
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] {msg}", flush=True)


def sanitize(name, maxlen=80):
    """Drive/Confluence 文件夹/文件名安全化：去掉非法字符、压缩空白、限长。"""
    s = ILLEGAL.sub("_", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:maxlen].rstrip(" .")) or "未命名"


def parse_size(text):
    """'11.7MB' / '349KB' / '1.2GB' → 字节数；失败返回 0。"""
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?B)\s*$", (text or "").upper())
    if not m:
        return 0
    # 1024 进制，和 data/oss_links.csv 既有写法（monitor/download.py 的 human）保持一致
    unit = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}[m.group(2)]
    return int(float(m.group(1)) * unit)


def human(n):
    n = n or 0
    for u in ("B", "KB", "MB", "GB"):
        if n < 1000 or u == "GB":
            return f"{n:.1f}{u}" if u != "B" else f"{n}B"
        n /= 1000


def is_real_oss(url):
    """真 OSS 直链的判定：http 开头，且不是网关形态。
    网关 URL 一定带 fileUrl= 密文参数；政务外网专用域名（cms.zjzwfw.gov.cn）
    也是网关形态，公网不可达，同样排除。"""
    u = (url or "").strip()
    return u.startswith("http") and "fileUrl=" not in u and "/api-gateway/" not in u


# ---------------------------------------------------------------- 数据加载

def load_links():
    """读映射表，返回 (可同步列表, 缺直链列表)。按 OSS URL 去重。"""
    ok, missing, seen = [], [], set()
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            oss = (row.get("OSS直链") or "").strip()
            item = {
                "date": (row.get("公告日期") or "").strip(),
                "unit": (row.get("发布单位") or "").strip(),
                "project": (row.get("项目名称") or "").strip(),
                "org": (row.get("建设单位") or "").strip(),
                "filename": (row.get("文件名") or "").strip(),
                "size": parse_size(row.get("大小") or ""),
                "oss": oss,
                "gateway": (row.get("网关地址") or "").strip(),
            }
            if not is_real_oss(oss):
                missing.append(item)
                continue
            if oss in seen:
                continue
            seen.add(oss)
            ok.append(item)
    return ok, missing


def load_announcements():
    """items.json → {项目名: [公告, ...]}，公告按发布日期正序。

    公告里带项目明细（地点/单位/机构/备注）和正文摘要，是页面正文
    的内容来源；没有明细的公告（项目: []）不进映射。
    """
    if not os.path.exists(ITEMS_PATH):
        log(f"⚠ 找不到 {ITEMS_PATH}，正文将只有附件清单")
        return {}
    with open(ITEMS_PATH, encoding="utf-8") as f:
        items = json.load(f)
    out = {}
    for it in items:
        ann = {
            "title": (it.get("title") or "").strip(),
            "type": (it.get("类型") or "").strip(),
            "date": (it.get("发布日期") or it.get("列表日期") or "").strip(),
            "unit": (it.get("unit") or "").strip(),
            "url": (it.get("url") or "").strip(),
            "abstract": (it.get("摘要") or "").strip(),
            "details": None,
        }
        for p in it.get("项目", []):
            name = (p.get("项目名称") or "").strip()
            if not name:
                continue
            if ann["details"] is None:            # 同公告多项目时各自一份
                ann["details"] = {}
            ann["details"][name] = {
                "loc": (p.get("地点") or "").strip(),
                "org": (p.get("单位") or "").strip(),
                "inst": (p.get("机构") or "").strip(),
                "note": (p.get("备注") or "").strip(),
            }
            out.setdefault(name, []).append(ann)
    for v in out.values():
        v.sort(key=lambda a: a["date"])
    return out


def build_projects(rows, anns):
    """CSV 行 + 公告映射 → {项目名: {unit, org, files, anns, latest}}。"""
    projects = {}
    for r in rows:
        name = r["project"] or r["filename"]
        p = projects.setdefault(name, {
            "unit": r["unit"], "org": r["org"], "files": [], "anns": [],
            "latest": r["date"],
        })
        p["files"].append(r)
        p["latest"] = max(p["latest"], r["date"])
    for name, p in projects.items():
        p["anns"] = anns.get(name, [])
        if p["anns"]:
            p["latest"] = max([p["latest"]] + [a["date"] for a in p["anns"]])
    return projects


def render_text(name, p):
    """生成「网页内容.txt」的正文（Drive 用的纯文本版）。"""
    L = []
    L.append(f"项目：{name}")
    L.append("")
    if p["anns"]:
        L.append(f"（共 {len(p['anns'])} 条相关公告，按时间正序）")
        L.append("")
        for i, a in enumerate(p["anns"], 1):
            head = f"【公告 {i}/{len(p['anns'])}】"
            meta = " · ".join(x for x in (a["type"], a["date"], a["unit"]) if x)
            L.append("─" * 56)
            L.append(f"{head}{meta}" if meta else head)
            if a["title"]:
                L.append(a["title"])
            if a["url"]:
                L.append(f"链接：{a['url']}")
            d = (a["details"] or {}).get(name)
            if d:
                L.append("")
                if d["loc"]:
                    L.append(f"建设地点：{d['loc']}")
                if d["org"]:
                    L.append(f"建设单位：{d['org']}")
                if d["inst"]:
                    L.append(f"环评机构：{d['inst']}")
            if a["abstract"]:
                L.append("")
                L.append("公示内容：")
                L.append(a["abstract"])
            if d and d["note"]:
                L.append("")
                L.append(f"备注：{d['note']}")
            L.append("")
    else:
        L.append("（未在 data/items.json 中找到对应公告正文，以下为附件清单）")
        L.append("")
    L.append("─" * 56)
    L.append("本文件夹附件：")
    L.append(f"- {TEXT_NAME}（本文件）")
    for f in p["files"]:
        tag = "（解压后上传）" if f["filename"].lower().endswith(ARCHIVE_EXT) else ""
        L.append(f"- {f['filename']}{tag}"
                 + (f"（{f['size'] and human(f['size'])}）" if f["size"] else ""))
    L.append("")
    L.append(f"生成时间：{datetime.now(CST).strftime('%Y-%m-%d %H:%M')}（北京时间）")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 解压

def clip_bytes(name, max_bytes=200):
    """按 UTF-8 字节数截断，防超 Linux 单文件名 255 字节上限。

    2026-10-16 实测：一个解压文件名 255 字节整，open() 直接
    Errno 36（GitHub runner 同症状）。中文 3 字节/字符，按字符数
    截断（旧代码 sanitize(...,120)）根本拦不住。
    """
    b = name.encode("utf-8")
    if len(b) <= max_bytes:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not ext:
        stem, ext = name, ""
    ext_part = ("." + ext) if ext else ""
    room = max_bytes - len(ext_part.encode("utf-8"))
    if room <= 0:
        return b[:max_bytes].decode("utf-8", "ignore")
    head = stem.encode("utf-8")[:room].decode("utf-8", "ignore")
    return (head + ext_part) or "未命名"


def safe_inner(name):
    """压缩包内文件名 → 扁平安全名：只留最后一段，防目录穿越。"""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return clip_bytes(sanitize(base, 120))


def dedupe(name, used):
    """同一压缩包内解出同名文件时加 _2/_3 后缀。"""
    if name not in used:
        return name
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        mk = lambda n: f"{stem}_{n}.{ext}"
    else:
        mk = lambda n: f"{name}_{n}"
    n = 2
    while mk(n) in used:
        n += 1
    return mk(n)


def decode_zip_name(info):
    """zip 内文件名 → 正确中文名。

    政务网压缩包来源五花八门：多数是 Windows 打包的 GBK 且没打
    UTF-8 标志位（flag_bits & 0x800），Python 默认按 cp437 解出乱码，
    要 cp437→gbk 转回来；但也有的明明是 UTF-8 却忘了打标志位——
    2026-10-16 实测金洁的 zip 就是这种：gbk 还原报 illegal
    multibyte，按 UTF-8 解才对。打了标志位的 zipfile 自己会按
    utf-8 解，直接用。依次试 gbk → gb18030 → utf-8。
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")     # cp437 字符还原成原始字节
    except UnicodeEncodeError:
        return name
    for enc in ("gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def extract_zip(path, dest):
    """zipfile 解压 + 中文文件名还原。返回 [(安全名, 字节数), ...]。

    政务网压缩包是 Windows/macOS 打包的：没打 UTF-8 标志位时文件名
    是 GBK，Python 默认按 cp437 解，出来是 ║■╓▌ 这类乱码，
    按 flag_bits 判断还原（见 decode_zip_name，已用真实附件实测）。
    macOS 打包的 __MACOSX/._* AppleDouble 元数据不是文档，跳过。
    """
    out, used = [], set()
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            if info.filename.startswith("__MACOSX/") \
                    or os.path.basename(info.filename).startswith("._"):
                continue
            name = dedupe(safe_inner(decode_zip_name(info)), used)
            used.add(name)
            target = os.path.join(dest, name)
            os.makedirs(dest, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append((name, info.file_size))
    return out


def extract_bsdtar(path, dest):
    """rar/7z 等交给 bsdtar（libarchive）。返回扁平化后的 [(安全名, 字节数), ...]。

    2026-10-16 实测坑：有的 rar 把文件裹在同名子目录里（X/X.pdf），
    bsdtar 原样落盘；旧代码只报告 basename、不把文件搬到 dest 根下，
    于是 do_archive 拿 work/<basename> 当 rclone 源——文件不存在，
    rclone 把不存在的源当目录，报 `Failed to copy: directory not found`。
    所以解压完统一拍平到 dest 根下（顺带治子目录名超长）。
    """
    def snapshot():
        return {os.path.relpath(os.path.join(r, f), dest)
                for r, _, fs in os.walk(dest) for f in fs}
    before = snapshot()
    try:
        p = subprocess.run(["bsdtar", "-xf", path, "-C", dest],
                           capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        raise RuntimeError("bsdtar 不存在：runner 上需 sudo apt-get install -y libarchive-tools")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        raise RuntimeError((tail[-1] if tail else f"bsdtar exit {p.returncode}")[:200])
    out, used = [], set()
    for rel in sorted(snapshot() - before):
        full = os.path.join(dest, rel)
        if not os.path.isfile(full):
            continue
        name = dedupe(safe_inner(rel), used)
        used.add(name)
        flat = os.path.join(dest, name)
        if os.path.abspath(full) != os.path.abspath(flat):
            shutil.move(full, flat)      # 拍平：子目录里的文件提到 dest 根下
        out.append((name, os.path.getsize(flat)))
    for r, dirs, _ in os.walk(dest, topdown=False):   # 清掉空目录层级
        if r != dest and not os.listdir(r):
            os.rmdir(r)
    return out


def download(url, path, tries=3):
    """OSS 直链 → 本地临时文件。返回字节数。"""
    last = ""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=600) as r, \
                    open(path, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return os.path.getsize(path)
        except Exception as e:
            last = repr(e)[:200]
            time.sleep(2 * (i + 1))
    raise RuntimeError(last)


def sniff_archive_kind(path):
    """按文件头判断真实格式，不信扩展名。

    2026-10-16 实测：安吉嘉好转椅配件厂的附件文件名是 .zip，
    实际内容是 RAR5（file 命令确认），按扩展名路由给 zipfile
    直接 "File is not a zip file"。PK 开头才是 zip，其余
    （rar/7z/tar/gz…）统交 bsdtar。
    """
    with open(path, "rb") as f:
        head = f.read(8)
    return "zip" if head.startswith(b"PK") else "other"

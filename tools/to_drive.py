#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把环评项目同步到 Google Drive：湖州线索/<项目>/网页内容.txt + 解压后的附件。

目录结构（2026-10-16 起按用户要求重做——原来是 <base>/<分局>/<项目>/，
现在改成扁平一层，不再按分局组织）：

    gdrive:湖州线索/
      ├── 年产纸制品20万平方米、纸箱50万平方米搬迁项目/
      │     ├── 网页内容.txt              ← 公告标题/类型/日期/链接/项目明细/公示正文
      │     ├── 公示-年产纸制品….pdf      ← 从 .rar/.zip 里解压出来
      │     └── …
      └── 善含中格局灌区续建配套与现代化改造项目/
            ├── 网页内容.txt
            └── 善含中格局灌区….pdf

每个项目一个文件夹：第一个文件是网页文字内容，其余是解压后的环评附件。

要点：
- .pdf 附件直接流式上传（rclone copyurl，OSS 直链境外可达）；
- .zip 用 Python zipfile 解压。政务网是 Windows 打包的，压缩包内文件名
  十有八九是 GBK 且没打 UTF-8 标志位，Python 会按 cp437 解出乱码——
  按 flag_bits 判断，没有 0x800 就 cp437→gbk 还原（已实测）；
- .rar 交给 bsdtar（libarchive，能正确处理中文文件名），runner 上装
  libarchive-tools；.7z 同理；
- 文本文件走 rclone rcat（stdin → Drive，不落临时文件）；
- 断点续跑：data/drive_state.json 按「目标文件」记账（text|/url|/file| 键），
  已传的跳过；每传完一个就落盘，中途挂了下次接着跑；
- 压缩包下载后即删，runner 磁盘峰值 ≈ 最大附件 × 2。

用法：
    python3 tools/to_drive.py                  # 正式同步
    python3 tools/to_drive.py --dry-run        # 只列计划，不调 rclone
    python3 tools/to_drive.py --limit 5        # 只传前 5 个任务
    python3 tools/to_drive.py --project 化工    # 只同步项目名含关键词的
    python3 tools/to_drive.py --base 湖州线索   # Drive 上的母文件夹名（默认）
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "data", "oss_links.csv")
ITEMS_PATH = os.path.join(ROOT, "data", "items.json")
STATE_PATH = os.path.join(ROOT, "data", "drive_state.json")
REMOTE = "gdrive:"                      # rclone 远端名，与 workflow 里的环境变量对应
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
ARCHIVE_EXT = (".zip", ".rar", ".7z")
TEXT_NAME = "网页内容.txt"
CST = timezone(__import__("datetime").timedelta(hours=8))


def log(msg):
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] {msg}", flush=True)


def sanitize(name, maxlen=80):
    """Drive 文件夹/文件名安全化：去掉非法字符、压缩空白、限长。"""
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

    公告里带项目明细（地点/单位/机构/备注）和正文摘要，是「网页内容.txt」
    的内容来源；没有明细的公告（项目: []）不进映射。
    """
    if not os.path.exists(ITEMS_PATH):
        log(f"⚠ 找不到 {ITEMS_PATH}，网页内容.txt 将只有附件清单")
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
    """生成「网页内容.txt」的正文。"""
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


# ---------------------------------------------------------------- 任务清单

def plan_tasks(projects, base, only_project=None):
    """把项目展开成上传任务列表。

    任务三种：
      text —— 网页内容.txt（rcat 上传）
      url  —— .pdf 等非压缩附件（copyurl 流式上传）
      arc  —— .zip/.rar/.7z（下载解压后逐个上传解出文件）
    同一项目内 text 排最前（用户要求「第一个文件是网页的文字内容」）；
    项目之间按最近公告日期倒序，新线索排在前面。
    """
    tasks = []
    names = sorted(projects, key=lambda n: (projects[n]["latest"] or "", n),
                   reverse=True)
    for name in names:
        if only_project and only_project not in name:
            continue
        p = projects[name]
        folder = f"{REMOTE}{sanitize(base)}/{sanitize(name)}"
        tasks.append({"kind": "text", "key": f"text|{name}",
                      "project": name, "dest": f"{folder}/{TEXT_NAME}",
                      "content": render_text(name, p)})
        for f in sorted(p["files"], key=lambda x: (x["filename"], x["date"])):
            if f["filename"].lower().endswith(ARCHIVE_EXT):
                tasks.append({"kind": "arc", "key": f"arc|{f['oss']}",
                              "project": name, "folder": folder,
                              "oss": f["oss"], "size": f["size"],
                              "fname": f["filename"]})
            else:
                tasks.append({"kind": "url", "key": f"url|{f['oss']}",
                              "project": name,
                              "dest": f"{folder}/{sanitize(f['filename'], 120)}",
                              "oss": f["oss"], "size": f["size"],
                              "fname": f["filename"]})
    return tasks


# ---------------------------------------------------------------- state

def load_state():
    """读续跑状态。旧格式（键是不带 | 的裸 OSS URL、dest 是
    <base>/<分局>/<项目>/ 老布局）整体隔离进 legacy，不参与跳过判断——
    2026-10-16 目录结构重做，老记录的目标路径已失效。"""
    st = {"uploaded": {}, "failed": {}, "legacy": {}}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                old = json.load(f)
            for bucket in ("uploaded", "failed"):
                for k, v in (old.get(bucket) or {}).items():
                    if "|" in k:
                        st[bucket][k] = v
                    else:
                        st["legacy"][k] = v
        except Exception:
            pass
    return st


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------------------------------------------------------------- 解压

def safe_inner(name):
    """压缩包内文件名 → 扁平安全名：只留最后一段，防目录穿越。"""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return sanitize(base, 120)


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


def extract_zip(path, dest):
    """zipfile 解压 + GBK 文件名还原。返回 [(安全名, 字节数), ...]。

    政务网压缩包是 Windows 打包的：没打 UTF-8 标志位（flag_bits & 0x800）
    时文件名是 GBK，Python 默认按 cp437 解，出来是 ║■╓▌ 这类乱码，
    要 cp437→gbk 转回来（已用真实附件实测）。
    """
    out, used = [], set()
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if not (info.flag_bits & 0x800):
                try:
                    name = name.encode("cp437").decode("gbk")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            name = dedupe(safe_inner(name), used)
            used.add(name)
            target = os.path.join(dest, name)
            os.makedirs(dest, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append((name, info.file_size))
    return out


def extract_bsdtar(path, dest):
    """rar/7z 等交给 bsdtar（libarchive）。返回解出的安全名列表。"""
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
        out.append((name, os.path.getsize(full)))
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


# ---------------------------------------------------------------- rclone

def rclone_copyurl(url, dest):
    """流式上传：OSS 直链 → Drive。返回 (ok, 错误信息)。"""
    cmd = [
        "rclone", "copyurl", url, dest,
        "--retries", "3",
        "--low-level-retries", "10",
        "--transfers", "1",
        "--stats", "15s",
        "--stats-one-line",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return False, "超时（>1h）"
    if p.returncode == 0:
        return True, ""
    tail = (p.stderr or p.stdout or "").strip().splitlines()
    return False, (tail[-1] if tail else f"rclone exit {p.returncode}")[:200]


def rclone_copy(local, folder):
    """本地文件 → Drive 文件夹（落在 folder/< basename >）。"""
    p = subprocess.run(["rclone", "copy", local, folder,
                        "--retries", "3", "--stats", "15s", "--stats-one-line"],
                       capture_output=True, text=True, timeout=3600)
    if p.returncode == 0:
        return True, ""
    tail = (p.stderr or p.stdout or "").strip().splitlines()
    return False, (tail[-1] if tail else f"rclone exit {p.returncode}")[:200]


def rclone_rcat(content, dest):
    """stdin → Drive 文件（网页内容.txt）。"""
    p = subprocess.run(["rclone", "rcat", dest],
                       input=content, capture_output=True, text=True,
                       encoding="utf-8", timeout=300)
    if p.returncode == 0:
        return True, ""
    tail = (p.stderr or p.stdout or "").strip().splitlines()
    return False, (tail[-1] if tail else f"rclone exit {p.returncode}")[:200]


def rclone_delete(path):
    subprocess.run(["rclone", "deletefile", path],
                   capture_output=True, text=True, timeout=120)


def probe_drive(base, rows):
    """同步前探活：确认凭证看得见、写得进共享文件夹，且有存储配额。

    两次实测踩坑（都在 2026-10-15）：
    1. 101 个文件全部死在「failed to make directory: 404」——SA 看不到文件夹；
    2. 分享修好后 101 个文件全部死在「Service Accounts do not have storage
       quota」403——SA 配额为 0，往「我的云端硬盘」传文件一律被拒。
    mkdir 只建文件夹、不占存储，两个坑都探不出来；所以最后用最小的待传文件
    真传一次。两种失败都不再让 101 个文件挨个重演。
    """
    log("检查 Drive 访问（about + mkdir 探活）…")
    p = subprocess.run(["rclone", "about", REMOTE], capture_output=True, text=True, timeout=120)
    if p.returncode == 0:
        for line in (p.stdout or "").splitlines():
            if ":" in line:
                log(f"  {line.strip()}")
    else:
        log("  （about 查不到账号信息，忽略，继续 mkdir 探活）")
    q = subprocess.run(["rclone", "mkdir", f"{REMOTE}{sanitize(base)}"],
                       capture_output=True, text=True, timeout=120)
    if q.returncode != 0:
        tail = ((q.stderr or "") + (q.stdout or "")).strip().splitlines()
        errs = [l for l in tail if " ERROR " in l or " CRITICAL " in l or "Failed" in l]
        log("✗ 在共享文件夹里建目录失败——凭证访问不到它：")
        for line in (errs or tail)[-3:]:
            log(f"    {line}")
        log("排查（按命中率排序）：")
        log("  1. 文件夹没分享给 SA：Drive 网页打开该文件夹 → 分享 → 添加 SA 邮箱")
        log("     → 权限选「编辑者」。注意：只分享父文件夹不够，要分享到用的这一层。")
        log("  2. 分享错了邮箱：SA 邮箱以 workflow 日志里「SA 邮箱」那行为准，")
        log("     逐字符对比分享时填的地址。")
        log("  3. 文件夹在「共享云端硬盘」里：必须把 SA 加为该磁盘成员，")
        log("     只分享其中的子文件夹，SA 访问照样是 404。")
        log("  4. DRIVE_FOLDER_ID 填错：secrets 里的 ID 应与文件夹 URL 末段一致。")
        return False
    log(f"✓ 母文件夹可写（{base} 已就绪）")

    if not rows:
        return True
    r = min(rows, key=lambda x: x["size"] or (1 << 40))
    probe = f"{REMOTE}{sanitize(base)}/.probe/{sanitize(r['filename'], 120)}"
    log(f"试传最小的附件（{human(r['size'])}）验证存储配额…")
    good, err = rclone_copyurl(r["oss"], probe)
    if good:
        rclone_delete(probe)
        subprocess.run(["rclone", "rmdir", f"{REMOTE}{sanitize(base)}/.probe"],
                       capture_output=True, text=True, timeout=60)
        log("✓ 试传成功，配额正常")
        return True
    if "storage quota" in err or "shared drives" in err.lower():
        log("✗ 试传被拒：Service Account 没有存储配额")
        log(f"    {err}")
        log("原因：SA 的存储配额是 0，往「我的云端硬盘」里的文件夹传文件会被")
        log("      Google 一律拒绝（403）；建文件夹不占存储，所以上面那步是过的。")
        log("出路二选一：")
        log("  A. 共享云端硬盘（需 Google Workspace 付费账号）：")
        log("     新建共享云端硬盘 → 把 SA 邮箱加为成员（内容管理员）→")
        log("     用磁盘里的文件夹 ID 更新 secrets.DRIVE_FOLDER_ID → 重跑。")
        log("  B. OAuth 用户授权（免费个人账号可用，文件占用户本人 15GB 配额）：")
        log("     自己电脑跑 rclone authorize \"drive\"，浏览器登录后把输出的")
        log("     token JSON 存为 secrets.GDRIVE_TOKEN，workflow 会自动切模式。")
        return False
    log("⚠ 试传失败但不像配额问题（可能只是这条 OSS 直链失效），继续正式同步：")
    log(f"    {err}")
    return True


# ---------------------------------------------------------------- 主流程

def do_archive(task, st, tmpdir, keep_temp=False):
    """下载压缩包 → 解压 → 逐个上传解出文件。返回 (成功数, 失败数, 字节数)。"""
    ok_n = fail_n = bytes_n = 0
    work = os.path.join(tmpdir, sanitize(task["fname"], 100) or "archive")
    os.makedirs(work, exist_ok=True)
    arc = os.path.join(work, sanitize(task["fname"], 120))
    try:
        got = download(task["oss"], arc)
    except Exception as e:
        st["failed"][task["key"]] = {"file": task["fname"], "error": str(e),
                                     "at": datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ")}
        log(f"        ✗ 下载失败：{e}")
        return 0, 1, 0
    log(f"        下载 {human(got)}，解压中…")
    try:
        if task["fname"].lower().endswith(".zip"):
            inner = extract_zip(arc, work)
        else:
            inner = extract_bsdtar(arc, work)
    except Exception as e:
        st["failed"][task["key"]] = {"file": task["fname"], "error": str(e),
                                     "at": datetime.now(timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ")}
        log(f"        ✗ 解压失败：{e}")
        if not keep_temp:
            shutil.rmtree(work, ignore_errors=True)
        return 0, 1, 0

    if not inner:
        log("        ⚠ 压缩包里没有文件（空包），标记完成避免重复下载")
        st["uploaded"][task["key"]] = {"file": task["fname"], "inner": [],
                                       "at": datetime.now(timezone.utc)
                                       .strftime("%Y-%m-%dT%H:%M:%SZ")}
        if not keep_temp:
            shutil.rmtree(work, ignore_errors=True)
        return 0, 0, 0

    for name, size in inner:
        fkey = f"file|{task['oss']}#{name}"
        if fkey in st["uploaded"]:
            log(f"        · 已传过 {name[:50]}")
            continue
        dest = f"{task['folder']}/{sanitize(name, 120)}"
        t0 = time.time()
        good, err = rclone_copy(os.path.join(work, name), task["folder"])
        dt = time.time() - t0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if good:
            ok_n += 1
            bytes_n += size
            st["uploaded"][fkey] = {"file": name, "dest": dest,
                                    "size": size, "at": now}
            st["failed"].pop(fkey, None)
            speed = f"{size/dt/1024:.0f}KB/s" if dt > 0 and size else "-"
            log(f"        ✓ {name[:50]}（{human(size)}，{dt:.0f}s {speed}）")
        else:
            fail_n += 1
            st["failed"][fkey] = {"file": name, "error": err, "at": now}
            log(f"        ✗ {name[:50]}：{err}")
        save_state(st)   # 每个文件后落盘，中途挂掉下次续跑

    if fail_n == 0:
        # 全部解出文件都已上传，标记整个压缩包任务完成，下次不再重复下载
        st["uploaded"][task["key"]] = {"file": task["fname"],
                                       "inner": [n for n, _ in inner],
                                       "at": datetime.now(timezone.utc)
                                       .strftime("%Y-%m-%dT%H:%M:%SZ")}
        save_state(st)
    if not keep_temp:
        shutil.rmtree(work, ignore_errors=True)
    return ok_n, fail_n, bytes_n


def main():
    ap = argparse.ArgumentParser(
        description="把环评项目按「湖州线索/<项目>/」扁平结构同步到 Google Drive")
    ap.add_argument("--base", default="湖州线索", help="Drive 上的母文件夹名")
    ap.add_argument("--limit", type=int, default=0,
                    help="本次最多处理几个任务（0=全部）")
    ap.add_argument("--project", default=None,
                    help="只同步项目名含该关键词的（子串匹配）")
    ap.add_argument("--dry-run", action="store_true", help="只列计划，不调 rclone")
    ap.add_argument("--keep-temp", action="store_true",
                    help="保留下载的压缩包和解压目录（调试用）")
    args = ap.parse_args()

    if not os.path.exists(CSV_PATH):
        sys.exit(f"找不到 {CSV_PATH}（先在境内跑 download.py --resolve 生成映射）")

    rows, missing = load_links()
    anns = load_announcements()
    projects = build_projects(rows, anns)
    st = load_state()
    if st["legacy"]:
        log(f"旧版目录结构（按分局组织）的 {len(st['legacy'])} 条记录已隔离，"
            f"本次按新结构重新上传；Drive 上的旧「环评附件/」目录可手动删除")

    tasks = plan_tasks(projects, args.base, args.project)
    pending = [t for t in tasks if t["key"] not in st["uploaded"]]
    retry = [t for t in pending if t["key"] in st["failed"]]

    log(f"映射表 {len(rows)} 条可用直链（另 {len(missing)} 条缺直链跳过）→ "
        f"{len(projects)} 个项目、{len(tasks)} 个上传任务；"
        f"已传 {len(st['uploaded'])}，本次待传 {len(pending)}"
        + (f"（其中 {len(retry)} 个是重试）" if retry else ""))
    if missing:
        log(f"缺直链的 {len(missing)} 个附件（政务外网专用域名或未解析，境内补映射后再同步）：")
        for m in missing:
            log(f"  - [{m['unit']}] {m['project'][:40]} → {m['filename'][:40]}")

    if args.limit:
        pending = pending[:args.limit]
    if not pending:
        log("没有待传的任务，收工")
        return 0

    if args.dry_run:
        log("计划（--dry-run，不调 rclone）：")
        cur = None
        for i, t in enumerate(pending, 1):
            if t["project"] != cur:
                cur = t["project"]
                log(f"  ◆ {cur}")
            if t["kind"] == "text":
                log(f"    [{i}] 网页内容.txt（公告 "
                    f"{len(projects[t['project']]['anns'])} 条）")
            elif t["kind"] == "url":
                log(f"    [{i}] {t['fname'][:50]}（{human(t['size'])}，直接上传）")
            else:
                log(f"    [{i}] {t['fname'][:50]}（{human(t['size'])}，解压后上传）")
        log(f"合计 {len(pending)} 个任务")
        return 0

    if subprocess.run(["rclone", "version"], capture_output=True).returncode != 0:
        sys.exit("rclone 不可用。Actions 里先装：curl https://rclone.org/install.sh | sudo bash")

    if not probe_drive(args.base, rows):
        return 1

    ok_n = fail_n = bytes_n = 0
    t_all = time.time()
    tmpdir = tempfile.mkdtemp(prefix="huzhou_drive_")
    try:
        for i, t in enumerate(pending, 1):
            log(f"[{i}/{len(pending)}] {t['project'][:36]}"
                + (f" › {t['fname'][:40]}" if t["kind"] != "text" else " › 网页内容.txt"))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if t["kind"] == "text":
                t0 = time.time()
                good, err = rclone_rcat(t["content"], t["dest"])
                dt = time.time() - t0
                if good:
                    ok_n += 1
                    st["uploaded"][t["key"]] = {"file": TEXT_NAME, "dest": t["dest"],
                                                "size": len(t["content"].encode()),
                                                "at": now}
                    st["failed"].pop(t["key"], None)
                    log(f"        ✓ {dt:.1f}s → {t['dest']}")
                else:
                    fail_n += 1
                    st["failed"][t["key"]] = {"file": TEXT_NAME, "error": err,
                                              "at": now}
                    log(f"        ✗ {err}")
                save_state(st)
            elif t["kind"] == "url":
                t0 = time.time()
                good, err = rclone_copyurl(t["oss"], t["dest"])
                dt = time.time() - t0
                if good:
                    ok_n += 1
                    bytes_n += t["size"]
                    st["uploaded"][t["key"]] = {"file": t["fname"], "dest": t["dest"],
                                                "size": t["size"], "at": now}
                    st["failed"].pop(t["key"], None)
                    speed = f"{t['size']/dt/1024:.0f}KB/s" if dt > 0 and t["size"] else "-"
                    log(f"        ✓ {human(t['size'])} {dt:.0f}s（{speed}）→ {t['dest']}")
                else:
                    fail_n += 1
                    st["failed"][t["key"]] = {"file": t["fname"], "error": err,
                                              "at": now}
                    log(f"        ✗ {err}")
                save_state(st)
            else:
                a_ok, a_fail, a_bytes = do_archive(t, st, tmpdir, args.keep_temp)
                ok_n += a_ok
                fail_n += a_fail
                bytes_n += a_bytes
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)

    done = sum(1 for t in tasks if t["key"] in st["uploaded"])
    log(f"本次完成：成功 {ok_n}、失败 {fail_n}，传输 {human(bytes_n)}，"
        f"总耗时 {time.time()-t_all:.0f}s；累计完成 {done}/{len(tasks)} 个任务")
    return 1 if fail_n else 0


if __name__ == "__main__":
    sys.exit(main())

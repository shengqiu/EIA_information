#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把环评项目同步到 Google Drive：湖州线索/<项目>/网页内容.txt + 解压后的附件。

目录结构（2026-10-16 起按用户要求重做——原来是 <base>/<分局>/<项目>/，
现在改成扁平一层，不再按分局组织）：

    gdrive:湖州线索/
      ├── 年产纸制品20万平方米、纸箱50万平方米搬迁项目/
      │     ├── 网页内容.txt              ← 公告标题/类型/日期/链接/项目明细/正文
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
  按 flag_bits 判断还原（已实测）；
- .rar 交给 bsdtar（libarchive，能正确处理中文文件名），runner 上装
  libarchive-tools；.7z 同理；
- 文本文件走 rclone rcat（stdin → Drive，不落临时文件）；
- 断点续跑：data/drive_state.json 按「目标文件」记账（text|/url|/file| 键），
  已传的跳过；每传完一个就落盘，中途挂了下次接着跑；
- 压缩包下载后即删，runner 磁盘峰值 ≈ 最大附件 × 2。

共享层：数据加载、正文渲染、解压/文件名还原/大小解析等纯函数在
tools/eia_common.py（与 tools/to_confluence.py 共用）。本文件只保留
Drive 特有的 rclone 封装、探活、state 和主流程。

用法：
    python3 tools/to_drive.py                  # 正式同步
    python3 tools/to_drive.py --dry-run        # 只列计划，不调 rclone
    python3 tools/to_drive.py --limit 5        # 只传前 5 个任务
    python3 tools/to_drive.py --project 化工    # 只同步项目名含关键词的
    python3 tools/to_drive.py --base 湖州线索   # Drive 上的母文件夹名（默认）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

from eia_common import (ARCHIVE_EXT, CSV_PATH, TEXT_NAME, build_projects,
                        clip_bytes, download, extract_bsdtar, extract_zip,
                        human, load_announcements, load_links, log,
                        render_text, sanitize, sniff_archive_kind)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "drive_state.json")
REMOTE = "gdrive:"                      # rclone 远端名，与 workflow 里的环境变量对应


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


# ---------------------------------------------------------------- rclone

def _run_rclone(cmd, timeout, tries=2, backoff=5, input_text=None):
    """跑 rclone；瞬时失败自动重试一次。

    2026-10-16 实测：Google 上传 API 会抖（品善.pdf 的 copyurl
    "Post .../upload/drive/v3/files" 连错 2 次），Drive 目录元数据
    也有短暂滞后。rclone 自带 --retries/--low-level-retries 之外，
    这里再包一层进程级重试，两次都挂才记失败。
    """
    last = ""
    for i in range(tries):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               input=input_text, encoding="utf-8",
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"超时（>{timeout // 60}min）"
        else:
            if p.returncode == 0:
                return True, ""
            tail = (p.stderr or p.stdout or "").strip().splitlines()
            last = (tail[-1] if tail else f"rclone exit {p.returncode}")[:200]
        if i + 1 < tries:
            time.sleep(backoff * (i + 1))
    return False, last


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
    return _run_rclone(cmd, 3600)


def rclone_copy(local, folder):
    """本地文件 → Drive 文件夹（落在 folder/< basename >）。"""
    cmd = ["rclone", "copy", local, folder,
           "--retries", "3", "--low-level-retries", "10",
           "--stats", "15s", "--stats-one-line"]
    return _run_rclone(cmd, 3600)


def rclone_rcat(content, dest):
    """stdin → Drive 文件（网页内容.txt）。"""
    cmd = ["rclone", "rcat", dest]
    return _run_rclone(cmd, 300, input_text=content)


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
    work = os.path.join(tmpdir,
                        clip_bytes(sanitize(task["fname"], 100) or "archive", 150))
    os.makedirs(work, exist_ok=True)
    arc = os.path.join(work, clip_bytes(sanitize(task["fname"], 120), 150))
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
        if sniff_archive_kind(arc) == "zip":
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
    ap.add_argument("--base", default="湖州", help="Drive 上的母文件夹名")
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

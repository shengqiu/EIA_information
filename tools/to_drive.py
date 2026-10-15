#!/usr/bin/env python3
"""把 data/oss_links.csv 里的附件同步到 Google Drive（在 GitHub Actions 上跑）。

背景约束（见 CODEBUDDY.md）：
- OSS 直链境外可达（已实测 206、250~324 KB/s），所以「下载 + 传 Drive」两步
  都能在 Actions 闭环；被挡的政务网网关（hbj.huzhou.gov.cn / cms.zjzwfw.gov.cn）
  全程不碰。
- 传输交给 rclone：copyurl 流式（URL → Drive，不落本地磁盘），分块断点续传，
  单个 137.5 MB 的文件也没压力。
- 认证：Service Account + 用户 Drive 里的共享文件夹。rclone 用环境变量配置，
  不落配置文件：
      RCLONE_CONFIG_GDRIVE_TYPE=drive
      RCLONE_CONFIG_GDRIVE_SERVICE_ACCOUNT_CREDENTIALS=<sa.json 路径>
      RCLONE_CONFIG_GDRIVE_ROOT_FOLDER_ID=<共享文件夹 ID>
- 断点续跑：data/drive_state.json 记录已传/失败，跨运行跳过已传项；
  每传完一个文件就落盘，中途挂了下次接着跑。

用法：
    python3 tools/to_drive.py                 # 正式同步
    python3 tools/to_drive.py --dry-run       # 只列计划，不调 rclone
    python3 tools/to_drive.py --limit 5       # 只传前 5 个
    python3 tools/to_drive.py --base 环评附件  # Drive 上的顶层文件夹名
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "data", "oss_links.csv")
STATE_PATH = os.path.join(ROOT, "data", "drive_state.json")
REMOTE = "gdrive:"                      # rclone 远端名，与 workflow 里的环境变量对应
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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


def load_rows():
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


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                st = json.load(f)
            st.setdefault("uploaded", {})
            st.setdefault("failed", {})
            return st
        except Exception:
            pass
    return {"uploaded": {}, "failed": {}}


def save_state(st):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def dest_path(item, base):
    """Drive 内的目标路径：<base>/<单位>/<项目名称>/<文件名>。"""
    folder = f"{REMOTE}{sanitize(base)}/{sanitize(item['unit'])}/{sanitize(item['project'])}"
    return f"{folder}/{sanitize(item['filename'], 120)}"


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
    log(f"✓ 共享文件夹可写（顶层目录 {base} 已就绪）")

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


def main():
    ap = argparse.ArgumentParser(description="把 oss_links.csv 里的附件同步到 Google Drive")
    ap.add_argument("--base", default="环评附件", help="Drive 上的顶层文件夹名")
    ap.add_argument("--limit", type=int, default=0, help="本次最多传几个（0=全部）")
    ap.add_argument("--dry-run", action="store_true", help="只列计划，不调 rclone")
    args = ap.parse_args()

    if not os.path.exists(CSV_PATH):
        sys.exit(f"找不到 {CSV_PATH}（先在境内跑 download.py --resolve 生成映射）")

    rows, missing = load_rows()
    st = load_state()
    pending = [r for r in rows if r["oss"] not in st["uploaded"]]
    retry = [r for r in pending if r["oss"] in st["failed"]]

    log(f"映射表 {len(rows)} 条可用直链（另 {len(missing)} 条缺直链跳过），"
        f"已传 {len(st['uploaded'])}，本次待传 {len(pending)}"
        + (f"（其中 {len(retry)} 条是重试）" if retry else ""))
    if missing:
        log(f"缺直链的 {len(missing)} 个附件（政务外网专用域名或未解析，境内补映射后再同步）：")
        for m in missing:
            log(f"  - [{m['unit']}] {m['project'][:40]} → {m['filename'][:40]}")

    if args.limit:
        pending = pending[:args.limit]
    if not pending:
        log("没有待传的附件，收工")
        return 0

    if args.dry_run:
        log("计划（--dry-run，不调 rclone）：")
        for i, r in enumerate(pending, 1):
            log(f"  [{i}] {human(r['size']):>8}  {r['unit']}/{r['project'][:30]}/{r['filename'][:40]}")
            log(f"        → {dest_path(r, args.base)}")
        log(f"合计 {len(pending)} 个，{human(sum(r['size'] for r in pending))}")
        return 0

    if subprocess.run(["rclone", "version"], capture_output=True).returncode != 0:
        sys.exit("rclone 不可用。Actions 里先装：curl https://rclone.org/install.sh | sudo bash")

    if not probe_drive(args.base, pending):
        return 1

    ok_n = fail_n = 0
    bytes_n = 0
    t_all = time.time()
    for i, r in enumerate(pending, 1):
        dest = dest_path(r, args.base)
        log(f"[{i}/{len(pending)}] {human(r['size']):>8}  {r['filename'][:50]}")
        t0 = time.time()
        good, err = rclone_copyurl(r["oss"], dest)
        dt = time.time() - t0
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if good:
            ok_n += 1
            bytes_n += r["size"]
            st["uploaded"][r["oss"]] = {"file": r["filename"], "dest": dest,
                                        "size": r["size"], "at": now}
            st["failed"].pop(r["oss"], None)
            speed = f"{r['size']/dt/1024:.0f}KB/s" if dt > 0 and r["size"] else "-"
            log(f"        ✓ {dt:.0f}s（{speed}）→ {dest}")
        else:
            fail_n += 1
            st["failed"][r["oss"]] = {"file": r["filename"], "error": err, "at": now}
            log(f"        ✗ {err}")
        save_state(st)   # 每个文件后落盘，中途挂掉下次续跑

    log(f"本次完成：成功 {ok_n}、失败 {fail_n}，传输 {human(bytes_n)}，"
        f"总耗时 {time.time()-t_all:.0f}s；累计已传 {len(st['uploaded'])}/{len(rows)}")
    return 1 if fail_n else 0


if __name__ == "__main__":
    sys.exit(main())

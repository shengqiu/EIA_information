#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载环评公告里挂的附件（环境影响报告书/报告表全本、公众参与说明等）。

前提：先跑过 python3 monitor.py（附件链接存在 data/items.json 里）。

常见用法 ------------------------------------------------------------
python3 tools/download.py                      # 全量下载（可能好几个 GB，慎用）
python3 tools/download.py --dry-run            # 只统计不下载，先看清楚有多大
python3 tools/download.py --unit 南浔           # 只下某个单位的
python3 tools/download.py --keyword 化工        # 只下项目名/单位含关键词的
python3 tools/download.py --since 2026-09-01   # 只下某天之后的
python3 tools/download.py --max-size 50        # 跳过大于 50MB 的文件
python3 tools/download.py --limit 5            # 只下 5 个，试试水

文件落在 files/<日期>_<单位>/<项目名>/<文件名>，
同时生成 manifest.csv（含项目名称、单位、原文链接、本地路径、大小）。
已存在且大小一致的文件会跳过，中断后重跑可续传。
"""

import argparse
import csv
import os
import re
import shutil
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ITEMS = os.path.join(ROOT, "data", "items.json")
DEST = os.path.join(ROOT, "files")
MANIFEST = os.path.join(ROOT, "files", "manifest.csv")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')
CST = timezone(timedelta(hours=8))


def log(msg):
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] {msg}", flush=True)


def safe_name(s, maxlen=90):
    """去掉文件系统不接受的字符，顺便砍到合理长度"""
    s = ILLEGAL.sub("_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:maxlen] if len(s) > maxlen else s


def parse_filename(url):
    """优先取 query 里的 fileName（URL 编码过的中文），兜底用路径最后一段"""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if q.get("fileName"):
        return urllib.parse.unquote(q["fileName"][0])
    return urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]) or "attachment"


def collect(args):
    """从 items.json 里筛出要下载的附件"""
    if not os.path.exists(ITEMS):
        sys.exit(f"找不到 {ITEMS}，请先运行 python3 monitor.py")

    with open(ITEMS, encoding="utf-8") as f:
        items = json = __import__("json").load(f)

    out = []
    for it in items:
        if args.unit and args.unit not in it.get("unit", ""):
            continue
        if args.since and (it.get("发布日期") or "") < args.since:
            continue
        for p in it.get("项目", []):
            name = p.get("项目名称") or ""
            org = p.get("单位") or ""
            hay = name + org + (p.get("地点") or "")
            if args.keyword and args.keyword not in hay:
                continue
            for url in p.get("附件链接", []):
                filename = parse_filename(url)
                hay = name + org + (p.get("地点") or "") + filename
                if args.keyword and args.keyword not in hay:
                    continue
                out.append({
                    "date": it.get("发布日期", ""),
                    "unit": it.get("unit", ""),
                    "type": it.get("类型", ""),
                    "title": it.get("title", ""),
                    "art_url": it.get("url", ""),
                    "project": name,
                    "org": org,
                    "loc": p.get("地点", ""),
                    "url": url,
                    "filename": filename,
                })
    return out


def size_from_headers(r):
    """从 Content-Range 或 Content-Length 里问出文件总大小"""
    cr = r.headers.get("Content-Range") or ""
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except ValueError:
            pass
    n = r.headers.get("Content-Length")
    return int(n) if n and n.isdigit() else None


def resolve_direct(url, session, timeout=40):
    """
    拿到 OSS 直链 + 文件大小，不下内容。

    官网给的是网关地址，真实文件在浙江政务云的 OSS 上，中间要跳两次：
        /api-gateway/jpaas-web-server/front/document/download?fileUrl=<密文>
          └ 302 → hbj.huzhou.gov.cn/cms_files/filemanager/.../xxx.rar
                └ 301 → zjjcmspublicnew.oss-cn-hangzhou-zwynet-d01-a.../xxx.rar
    fileUrl 是服务端加密的（base64 解开是 128 字节密文），没法离线推算，
    只能靠跟随重定向拿到直链。直链是公开读的，不带签名、不需要 Referer，
    可以直接丢给 wget / 迅雷 / IDM 批量下载。
    """
    try:
        r = session.get(url, headers={"User-Agent": UA, "Range": "bytes=0-0"},
                        timeout=timeout, stream=True, allow_redirects=True)
        final, size = r.url, size_from_headers(r)
        hops = len(r.history)
        r.close()
        return final, size, hops
    except Exception:
        return url, None, 0


def probe_size(url, session):
    """只问大小不下载：先试 Range，不行退回 HEAD"""
    final, size, _ = resolve_direct(url, session)
    return size


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n is None:
            return "未知"
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def download(entry, session, dry=False):
    """探测文件大小；dry-run 时同样要探测，才能把清单和体积列出来给用户看"""
    return entry, probe_size(entry["url"], session)


def main():
    ap = argparse.ArgumentParser(description="下载湖州环评公告里的附件")
    ap.add_argument("--unit", help="只下某个发布单位（支持子串，如 南浔）")
    ap.add_argument("--keyword", help="按项目名/建设单位过滤")
    ap.add_argument("--since", help="只下该日期之后的公告，如 2026-09-01")
    ap.add_argument("--max-size", type=float, default=None, help="跳过大于该 MB 的文件")
    ap.add_argument("--limit", type=int, default=None, help="最多下载几个")
    ap.add_argument("--workers", type=int, default=3, help="并发数")
    ap.add_argument("--dry-run", action="store_true", help="只统计不下载")
    ap.add_argument("--resolve", action="store_true",
                    help="只解析 OSS 直链不下文件，输出到 files/direct_links.csv")
    args = ap.parse_args()

    cand = collect(args)
    if args.limit:
        cand = cand[:args.limit]
    if not cand:
        log("没有符合条件的附件")
        return 0

    sess = requests.Session()

    if args.resolve:
        log(f"解析 {len(cand)} 个附件的 OSS 直链…")
        os.makedirs(DEST, exist_ok=True)
        out = os.path.join(DEST, "direct_links.csv")
        rows, ok = [], 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(resolve_direct, e["url"], sess): e for e in cand}
            for fu in as_completed(futs):
                e = futs[fu]
                final, size, hops = fu.result()
                if "oss-" in final or hops:
                    ok += 1
                rows.append([e["date"], e["unit"], e["project"], e["org"],
                             e["filename"], "" if size is None else human(size),
                             final, e["url"]])
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        with open(out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["公告日期", "发布单位", "项目名称", "建设单位", "文件名", "大小", "OSS直链", "网关地址"])
            w.writerows(rows)
        log(f"解析完成 {ok}/{len(rows)}，清单 {out}")
        log("直链公开可读、无签名，可直接 wget / 迅雷批量下载")
        # 同步一份进仓库（data/ 默认被 git 跟踪），让 GitHub Actions 上的
        # mailer.py 也能拿到 网关URL → OSS直链 映射，境外直接下附件
        repo_copy = os.path.join(os.path.dirname(DEST), "data", "oss_links.csv")
        try:
            shutil.copyfile(out, repo_copy)
            log(f"已同步仓库副本 {os.path.normpath(repo_copy)}")
        except Exception as e:
            log(f"⚠ 仓库副本同步失败（{e}），不影响 files/ 清单")
        return 0

    log(f"匹配到 {len(cand)} 个附件，探测大小中…")
    sized = []
    with ThreadPoolExecutor(max_workers=min(6, len(cand))) as ex:
        futs = [ex.submit(download, c, sess, args.dry_run) for c in cand]
        for fu in as_completed(futs):
            try:
                sized.append(fu.result())
            except Exception as e:
                log(f"  探测失败：{e}")

    if args.max_size:
        before = len(sized)
        sized = [(e, s) for e, s in sized if s is None or s <= args.max_size * 1024 * 1024]
        log(f"按 --max-size {args.max_size}MB 过滤掉 {before - len(sized)} 个")

    known = [s for _, s in sized if s is not None]
    total = sum(known)
    unknown = len(sized) - len(known)
    log(f"待下载 {len(sized)} 个文件"
        + (f"，已知部分合计 {human(total)}" if known else "")
        + (f"，另有 {unknown} 个大小未知" if unknown else ""))

    if args.dry_run:
        print()
        print(f"{'大小':>10}  {'日期':<12} {'单位':<10} 文件名")
        print("-" * 96)
        for e, s in sorted(sized, key=lambda x: (x[1] if x[1] is not None else 10 ** 13), reverse=True)[:25]:
            print(f"{human(s):>10}  {e['date']:<12} {e['unit']:<10} {e['filename'][:52]}")
        print()
        log("（--dry-run 模式，没有真的下载。大小未知的文件不影响下载，只是没法在下载前预估）")
        return 0

    print(f"\n预估下载量 {human(total)}" + (f"（另有 {unknown} 个大小未知）" if unknown else ""))
    confirm = input("确认下载？[y/N] ").strip().lower()
    if confirm != "y":
        log("已取消")
        return 0

    os.makedirs(DEST, exist_ok=True)
    rows = []
    ok = fail = skip = 0
    t0 = time.time()

    def work(item):
        e, size = item
        folder = os.path.join(DEST, safe_name(f"{e['date'] or '无日期'}_{e['unit']}"), safe_name(e["project"] or e["org"] or "未命名项目"))
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, safe_name(e["filename"], 120))

        if os.path.exists(path) and size and abs(os.path.getsize(path) - size) < 1024:
            return ("skip", e, path, os.path.getsize(path))

        # 断点续传
        resume = os.path.getsize(path) if os.path.exists(path) else 0
        headers = {"User-Agent": UA, "Referer": e["art_url"]}
        if resume:
            headers["Range"] = f"bytes={resume}-"

        for attempt in range(3):
            try:
                r = requests.get(e["url"], headers=headers, timeout=120, stream=True)
                if not r.ok:
                    r.close()
                    raise RuntimeError(f"HTTP {r.status_code}")
                mode = "ab" if resume and r.status_code == 206 else "wb"
                if mode == "wb":
                    resume = 0
                with open(path, mode) as f:
                    for chunk in r.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
                r.close()
                return ("ok", e, path, os.path.getsize(path))
            except Exception as err:
                if attempt == 2:
                    return ("fail", e, str(err), 0)
                time.sleep(2 * (attempt + 1))
        return ("fail", e, "未知错误", 0)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, s) for s in sized]
        for fu in as_completed(futs):
            try:
                state, e, path, got = fu.result()
            except Exception as err:
                state, e, path, got = "fail", {"filename": "?"}, str(err), 0

            if state == "ok":
                ok += 1
                log(f"  ✓ {human(got):>9}  {e['unit']}  {e['filename'][:44]}")
                rows.append([e["date"], e["unit"], e["type"], e["project"], e["org"],
                             e["filename"], human(got), os.path.relpath(path, DEST), e["art_url"]])
            elif state == "skip":
                skip += 1
                log(f"  · 已存在 {human(got):>9}  {e['filename'][:44]}")
                rows.append([e["date"], e["unit"], e["type"], e["project"], e["org"],
                             e["filename"], human(got), os.path.relpath(path, DEST), e["art_url"]])
            else:
                fail += 1
                log(f"  ✗ 失败 {e.get('filename','?')[:44]} → {path}")

    with open(MANIFEST, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["公告日期", "发布单位", "公告类型", "项目名称", "建设单位", "文件名", "大小", "本地路径", "原文链接"])
        w.writerows(sorted(rows, key=lambda r: (r[0], r[1]), reverse=True))

    log(f"完成：成功 {ok} / 跳过 {skip} / 失败 {fail}，用时 {time.time() - t0:.0f}s")
    log(f"文件目录：{DEST}")
    log(f"清单：{MANIFEST}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("已中断（已下载的文件会保留，重跑可续传）")

# -*- coding: utf-8 -*-
"""
从境内云函数拉新公告，合并进本地 data/items.json。

云函数只返回「本次新解析的公告」，已处理的 id 名单（seen）由本脚本带上再带回来，
所以云函数本身不用存任何状态。

用法：
    export SCRAPE_URL="https://service-xxxx.gz.apigw.tencentcs.com/release/scrape"
    export SCRAPE_TOKEN="你自己设的口令"
    python3 tools/fetch_remote.py

    python3 tools/fetch_remote.py --limit 20 --max-new 100   # 抓得更狠一点
    python3 tools/fetch_remote.py --dry-run                  # 只探活，不落盘
    python3 tools/fetch_remote.py --resolve-missing          # 重试之前没解析出直链的附件
```

# 云函数现在已经「采集 + 解析附件直链」一次做完：
#   网关 download?fileUrl=<密文> 只有境内才肯 302/301 到政务云 OSS，
#   所以解析必须在云函数里做；解析出来的 OSS 直链无签名、境外可达，
#   境外 Actions 沿着直链就能直接把附件传上 Google Drive（tools/to_drive.py）。
# 拿到的直链按「网关地址」合并进 data/oss_links.csv —— 这份 CSV 是邮件
# 和 Drive 同步的唯一数据来源。
"""

import os
import sys
import csv
import json
import argparse
import datetime

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
ITEMS_PATH = os.path.join(DATA_DIR, "items.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
CSV_PATH = os.path.join(DATA_DIR, "oss_links.csv")
CSV_FIELDS = ["公告日期", "发布单位", "项目名称", "建设单位",
              "文件名", "大小", "OSS直链", "网关地址"]
KEEP_DAYS = 550          # 历史库只留 18 个月，和 monitor.py 保持一致


def real_oss(url):
    """真 OSS 直链判定（和 mailer / to_drive 保持一致）。
    网关形态（带 fileUrl= 密文参数）不算，政务外网专用域名也是网关形态。"""
    u = (url or "").strip()
    return bool(u.startswith("http") and "fileUrl=" not in u
                and "/api-gateway/" not in u)


def load_csv_rows():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        return [{k: (r.get(k) or "").strip() for k in CSV_FIELDS}
                for r in csv.DictReader(f)]


def upsert_links(rows, links):
    """把云函数的解析结果按「网关地址」合并进 CSV。只有拿到真直链的才写。"""
    idx = {r.get("网关地址", ""): r for r in rows}
    added = updated = 0
    for l in links:
        gw = (l.get("网关地址") or "").strip()
        oss = (l.get("OSS直链") or "").strip()
        if not gw or not real_oss(oss):
            continue
        cur = idx.get(gw)
        if cur is None:
            new = {k: (l.get(k) or "").strip() for k in CSV_FIELDS}
            rows.append(new)
            idx[gw] = new
            added += 1
            continue
        changed = False
        for k in ("OSS直链", "大小", "文件名"):     # 这些以云函数的最新结果为准
            v = (l.get(k) or "").strip()
            if v and cur.get(k) != v:
                cur[k] = v
                changed = True
        for k in ("公告日期", "发布单位", "项目名称", "建设单位"):   # 缺了才补
            v = (l.get(k) or "").strip()
            if v and not cur.get(k):
                cur[k] = v
                changed = True
        if changed:
            updated += 1
    return added, updated


def write_csv(rows):
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] {path} 读不出来（{e!r}），按空处理")
        return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--max-new", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true", help="只探活，不写文件")
    ap.add_argument("--resolve-missing", action="store_true",
                    help="不采集，只让云函数重试 CSV 里还没解析出直链的附件")
    ap.add_argument("--max-resolve", type=int, default=40,
                    help="单次最多解析几个附件（云函数串行解析，防超时）")
    args = ap.parse_args()

    url = os.environ.get("SCRAPE_URL", "").strip()
    token = os.environ.get("SCRAPE_TOKEN", "").strip()
    if not url:
        sys.exit("缺少环境变量 SCRAPE_URL（境内云函数的 HTTP 地址）")

    q = f"?token={token}" if token else ""
    scrape_url = url + q
    resolve_url = url.rstrip("/") + "/resolve" + q

    if args.resolve_missing:
        return do_resolve_missing(resolve_url, args)

    state = load_json(STATE_PATH, {"initialized": True, "seen": {}})
    seen = dict(state.get("seen") or {})
    items = load_json(ITEMS_PATH, [])
    print(f"本地在库 {len(items)} 条，已处理标记 {len(seen)} 个")

    print(f"→ 调用云函数（limit={args.limit}, max_new={args.max_new}, "
          f"max_resolve={args.max_resolve}）")
    r = requests.post(scrape_url,
                      json={"seen": seen, "limit": args.limit, "max_new": args.max_new,
                            "resolve": True, "max_resolve": args.max_resolve},
                      timeout=args.timeout)
    print(f"← HTTP {r.status_code}, {len(r.content)} 字节")
    if r.status_code != 200:
        sys.exit(f"云函数返回 {r.status_code}：{r.text[:500]}")
    try:
        data = r.json()
    except Exception:
        sys.exit(f"响应不是 JSON：{r.text[:300]}")
    if not data.get("ok"):
        sys.exit(f"云函数报错：{data.get('error')}")

    print(f"   扫描 {data.get('scanned')} 条 / 新解析 {data.get('parsed')} 条 / "
          f"失败 {data.get('failed')} 条 / 耗时 {data.get('elapsed_sec')}s / "
          f"栏目 {data.get('columns_ok')} 成功 {data.get('columns_failed')} 失败")
    if data.get("attachments"):
        print(f"   附件 {data.get('attachments')} 个 / 解析出直链 {data.get('resolved')} 个")

    fresh = data.get("new") or []
    if args.dry_run:
        print("--dry-run：不写文件")
        for i in fresh[:10]:
            print(f"   {i.get('发布日期')} [{i.get('unit')}] {i.get('title')[:40]}")
        return 0

    known = {i["art_id"] for i in items}
    added = 0
    for it in fresh:
        if it.get("art_id") in known:
            continue
        items.append(it)
        known.add(it["art_id"])
        added += 1

    # 清理过期 + 去重，和 monitor.py 的规则一致
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    dedup, seen_keys = [], set()
    for i in sorted(items, key=lambda x: x.get("发布日期", ""), reverse=True):
        if i.get("发布日期", "") < cutoff or i["art_id"] in seen_keys:
            continue
        seen_keys.add(i["art_id"])
        dedup.append(i)
    dropped = len(items) - len(dedup)
    items = dedup

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ITEMS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"initialized": True, "seen": data.get("seen") or seen},
                  f, ensure_ascii=False, indent=1)

    # 云函数已经把新附件的网关地址解析成 OSS 直链了（境内才能跟随 302/301），
    # 这里按「网关地址」合并进 CSV —— 邮件和 Google Drive 同步都读这份表
    links = data.get("links") or []
    if links:
        rows = load_csv_rows()
        csv_added, csv_updated = upsert_links(rows, links)
        if csv_added or csv_updated:
            write_csv(rows)
        print(f"✅ 公告在库 {len(items)} 条（新增 {added}，清理 {dropped}）/ "
              f"直链表 {len(rows)} 条（新增 {csv_added}，更新 {csv_updated}）")
        print(f"::notice::公告 {len(items)} 条 / 新增 {added} / "
              f"直链表 {len(rows)} 条（+{csv_added} ~{csv_updated}）")
        return 0

    att = sum(len(p.get("附件链接", [])) for i in items for p in i.get("项目", []))
    print(f"✅ 在库 {len(items)} 条（新增 {added}，清理 {dropped}） / 附件 {att} 个")
    print(f"::notice::公告 {len(items)} 条 / 新增 {added} / 附件 {att} 个")
    return 0


def do_resolve_missing(resolve_url, args):
    """把缺直链的附件丢给云函数重试。补两类漏网：
    1) CSV 里有行但没直链的（比如指向政务外网专用域名的，重试也是失败，至少留个交代）
    2) items.json 里有附件、但压根没进 CSV 的 —— 半路升级新功能前的历史遗留，
       这类不主动捞一次就永远进不了 Google Drive。
    """
    rows = load_csv_rows()
    have = {r.get("网关地址", "").strip() for r in rows if r.get("网关地址")}
    missing = [r["网关地址"] for r in rows
               if r.get("网关地址") and not real_oss(r.get("OSS直链", ""))]

    extra = {}          # 网关地址 → items.json 里的项目信息
    items = load_json(ITEMS_PATH, [])
    for it in items:
        for p in it.get("项目") or []:
            for u in p.get("附件链接") or []:
                if u and u not in have and u not in extra:
                    extra[u] = {"公告日期": it.get("发布日期") or "",
                                "发布单位": it.get("unit") or "",
                                "项目名称": p.get("项目名称") or "",
                                "建设单位": p.get("建设单位") or ""}

    urls = missing + list(extra)
    if not urls:
        print("没有缺直链的条目，跳过")
        return 0
    print(f"待补 {len(urls)} 条（CSV 缺直链 {len(missing)} + 新发现未入库 {len(extra)}）"
          f" → {resolve_url.split('?')[0]}")
    try:
        r = requests.post(resolve_url, json={"urls": urls}, timeout=args.timeout)
    except Exception as e:
        print(f"[warn] 调用失败：{e!r}")
        return 1
    print(f"← HTTP {r.status_code}, {len(r.content)} 字节")
    if r.status_code != 200:
        print(f"[warn] 云函数返回 {r.status_code}：{r.text[:300]}")
        return 1
    try:
        out = r.json()
    except Exception:
        print(f"[warn] 响应不是 JSON：{r.text[:200]}")
        return 1

    # 新入库的那些（原本没有 CSV 行的）要把项目信息从 items.json 补上
    links = out.get("links") or []
    for l in links:
        meta = extra.get((l.get("网关地址") or "").strip())
        if meta:
            for k, v in meta.items():
                if v and not (l.get(k) or "").strip():
                    l[k] = v

    added, updated = upsert_links(rows, links)
    if added or updated:
        write_csv(rows)
    left = len(urls) - len(links)
    print(f"✅ 补回 {len(links)} 条（新增 {added} 更新 {updated}），"
          f"仍缺 {left} 条；耗时 {out.get('elapsed_sec')}s")
    print(f"::notice::补回直链 {len(links)} 条 / 仍缺 {left} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())

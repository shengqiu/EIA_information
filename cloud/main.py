# -*- coding: utf-8 -*-
"""
湖州环评公示采集 —— 云函数入口（腾讯云 SCF / 阿里云 FC 通用）

为什么要有这层：hbj.huzhou.gov.cn 拒绝境外 IP，GitHub Actions 和 Cloudflare
都抓不到。把抓取放到境内云函数里，Actions 只负责调度、合并和发布。

对外暴露用「函数 URL」：腾讯云 API 网关已于 2025-06-30 停服、API 网关触发器下线，
官方指定的替代就是函数 URL——不收费、链路纯透传（没有网关那一层 15s 超时）、
且兼容 apigw 响应格式。注意它的 event 里 query 在 `queryString` 字段，
且去掉了 isBase64Encoded / requestContext，本文件已做兼容。

设计要点：
  * 云函数不保存状态。已处理的公告 id 由调用方（Actions）通过请求体带过来，
    处理完再带回去 —— 所以函数可以随便冷启动、随便扩缩容，不用接 COS/OSS。
  * 只返回「本次新解析的公告」，不是全量 —— 日常增量只有几十 KB。

请求体 (POST JSON)：
    {"seen": {"<art_id>": "2026-09-29 17:19", ...},   # 已处理过的公告
     "limit": 12,        # 每栏目取最新几条，默认 12
     "max_new": 60}      # 单次最多解析几条新公告，默认 60

    # 直链解析（附件自动同步 Google Drive 用）：同一份 body 里加
    "resolve": true,     # 是否顺带解析新公告附件的 OSS 直链，默认 true
    "max_resolve": 40    # 单次最多解析几个附件（串行，防超时/限流）

POST 到 `.../resolve` 是单独的批量解析端点（重试失败条目用）：
    {"urls": ["https://hbj.huzhou.gov.cn/api-gateway/...download?fileUrl=...", ...]}
    → {"ok": true, "links": [{"网关地址": ..., "OSS直链": ..., "大小": "11.7MB"}],
       "done": 3, "failed": 1}

为什么直链必须在境内解析：网关 download?fileUrl=<密文> 只有在境内才肯 302/301 到
浙江政务云 OSS；OSS 直链本身无签名、境外可达，所以境外只用、境内只发。
带上直链后，境外 Actions 就能直接沿着直链把附件传上 Google Drive。

响应 (JSON)：
    {"ok": true, "new": [<公告对象>...], "seen": {...更新后的...},
     "scanned": 192, "parsed": 3, "failed": 0, "elapsed_sec": 21.4,
     "columns_ok": 16, "columns_failed": 0}

鉴权：环境变量 SCRAPE_TOKEN；调用方在 query `?token=xxx` 或头 `X-Token` 里带上。
没设 SCRAPE_TOKEN 时不鉴权（调试用，别在生产这么干）。
"""

import os
import json
import time
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, parse_qs, unquote

from monitor import COLUMNS, fetch_list, parse_detail, now_cst

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
RESOLVE_GAP = 0.4          # 两次解析之间的间隔：网关会限流，必须串行且留间隔


# ------------------------------------------------------------------ 业务

def do_scrape(body):
    t0 = time.time()
    seen = dict(body.get("seen") or {})
    limit = int(body.get("limit") or 12)
    max_new = int(body.get("max_new") or 60)

    sess = _session()
    candidates, col_ok, col_fail = [], 0, 0
    for col_id in COLUMNS:
        try:
            candidates.extend(fetch_list(col_id, limit, session=sess))
            col_ok += 1
        except Exception as e:
            col_fail += 1
            print(f"[skip] col{col_id}: {e!r}")

    uniq = {}
    for c in candidates:
        uniq.setdefault(c["art_id"], c)

    targets = [v for k, v in uniq.items() if k not in seen][:max_new]

    new_items, failed = [], 0
    if targets:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(parse_detail, t): t for t in targets}
            for fu in as_completed(futs):
                t = futs[fu]
                try:
                    detail = fu.result()
                except Exception as e:
                    failed += 1
                    print(f"[fail] {t['title'][:30]}: {e!r}")
                    continue
                new_items.append(detail)
                seen[t["art_id"]] = now_cst().strftime("%Y-%m-%d %H:%M")

    # ---- 附件直链解析：只解析、不下载内容（Range 只要 1 字节）----
    links = []
    if body.get("resolve", True):
        todo = [(it, p, u)
                for it in new_items
                for p in (it.get("项目") or [])
                for u in (p.get("附件链接") or [])]
        cap = int(body.get("max_resolve") or 40)
        rs = _session()
        for it, p, u in todo[:cap]:
            rows = resolve_one(u, rs)
            rows.update({
                "公告日期": it.get("发布日期") or "",
                "发布单位": it.get("unit") or "",
                "项目名称": p.get("项目名称") or "",
                "建设单位": p.get("建设单位") or "",
            })
            links.append(rows)
            time.sleep(RESOLVE_GAP)
        if len(todo) > cap:
            print(f"[resolve] 本次有 {len(todo)} 个附件，只解析了前 {cap} 个（防超时）")

    new_items.sort(key=lambda x: (x.get("发布日期") or ""), reverse=True)
    return {
        "ok": True,
        "new": new_items,
        "links": links,                       # CSV 行格式，可直接合并进 data/oss_links.csv
        "attachments": sum(len(p.get("附件链接") or [])
                           for it in new_items for p in (it.get("项目") or [])),
        "resolved": sum(1 for l in links if l["OSS直链"]),
        "seen": seen,
        "scanned": len(uniq),
        "parsed": len(new_items),
        "failed": failed,
        "columns_ok": col_ok,
        "columns_failed": col_fail,
        "elapsed_sec": round(time.time() - t0, 1),
    }


def do_resolve(body):
    """批量解析网关地址 → OSS 直链。给 Actions 重试之前失败的条目用。

    请求 {"urls": [<网关地址>, ...]}，返回能解析出的部分。
    解析不了的不返回（比如指向 cms.zjzwfw.gov.cn 政务外网专用域名的，
    公网和境外都不可达，重试一万次也没用）。
    """
    t0 = time.time()
    urls = [u for u in (body.get("urls") or []) if u]
    cap = int(body.get("max") or 60)
    out, failed = [], 0
    sess = _session()
    for u in urls[:cap]:
        r = resolve_one(u, sess)
        if r["OSS直链"]:
            out.append(r)
        else:
            failed += 1
        time.sleep(RESOLVE_GAP)
    return {"ok": True, "links": out, "done": len(out), "failed": failed,
            "elapsed_sec": round(time.time() - t0, 1)}


# ------------------------------------------------------------------ 直链解析

def filename_of(url):
    """附件名：优先 query 里的 fileName（URL 编码过的中文），兜底路径最后一段。"""
    try:
        q = parse_qs(urlsplit(url).query)
        if q.get("fileName"):
            return unquote(q["fileName"][0])
        if q.get("filename"):
            return unquote(q["filename"][0])
        return unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "未知"
    except Exception:
        return "未知"


def size_from_headers(r):
    """从 Content-Range / Content-Length 问出文件总大小（Range 只要了 1 字节）"""
    cr = r.headers.get("Content-Range") or ""
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except ValueError:
            pass
    n = r.headers.get("Content-Length")
    return int(n) if n and n.isdigit() else None


def is_real_oss(url):
    """真 OSS 直链判定。网关 URL 一定带 fileUrl= 密文参数或 /api-gateway/ 路径，
    政务外网专用域名（cms.zjzwfw.gov.cn）也是这个形态，公网不可达。"""
    u = (url or "").strip()
    return bool(u.startswith("http") and "fileUrl=" not in u
                and "/api-gateway/" not in u)


def size_text(n):
    """形式对齐 data/oss_links.csv 既有写法：30.6MB / 735.0KB。
    和 monitor/download.py 一致用 1024 进制（既有 97 行都是这个口径）。"""
    if not n:
        return ""
    return f"{n/1024:.1f}KB" if n < 1024 ** 2 else f"{n/1024**2:.1f}MB"


def resolve_one(url, session, timeout=25):
    """网关 → OSS 直链（跟随 302/301）。失败时重试一次。
    返回 {"网关地址":..., "OSS直链":..., "大小":...}，解析不了则 OSS直链 为空串。"""
    final, size = "", None
    for _ in (1, 2):
        try:
            r = session.get(url, headers={"User-Agent": UA, "Range": "bytes=0-0"},
                            timeout=timeout, stream=True, allow_redirects=True)
            final, size = r.url, size_from_headers(r)
            r.close()
            if final != url:
                break
        except Exception as e:
            print(f"[resolve-fail] {filename_of(url)[:30]}: {e!r}")
        time.sleep(RESOLVE_GAP)
    return {"网关地址": url,
            "OSS直链": final if is_real_oss(final) else "",
            "大小": size_text(size),
            "文件名": filename_of(url)}


def _session():
    import requests
    s = requests.Session()
    s.keep_alive = False
    return s


# ------------------------------------------------------------------ HTTP 层

def _check_auth(query, headers):
    token = os.environ.get("SCRAPE_TOKEN", "").strip()
    if not token:
        return True
    got = ""
    if query:
        got = query.get("token") or query.get("Token") or ""
    if not got and headers:
        got = (headers.get("x-token") or headers.get("X-Token")
               or headers.get("X_TOKEN") or "")
    return got == token


def _parse_body(event):
    raw = event.get("body")
    if raw is None:
        return {}
    # 函数 URL 已经去掉了 isBase64Encoded 字段，所以不能只看标志位，
    # 直接试 JSON，不行再试 base64
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8", "replace"))
    except Exception:
        return {}


def _parse_query(event):
    """函数 URL 把 query 放在 event['queryString']，
    API 网关（已下线）放在 event['queryStringParameters']，两者都兼容。"""
    for key in ("queryStringParameters", "queryString", "query"):
        v = event.get(key)
        if not v:
            continue
        if isinstance(v, dict):
            return v
        if isinstance(v, str):
            try:
                d = json.loads(v)
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
            from urllib.parse import parse_qs
            return {k: val[0] for k, val in parse_qs(v).items()}
    return {}


def _resp(code, obj):
    # 腾讯云 API 网关「集成响应」要求的四件套（从云函数控制台建触发器时默认开启）
    return {
        "isBase64Encoded": False,
        "statusCode": code,
        "headers": {"Content-Type": "application/json; charset=utf-8",
                    "Cache-Control": "no-store"},
        "body": json.dumps(obj, ensure_ascii=False),
    }


# ---- 腾讯云 SCF（API 网关触发器 / HTTP 触发） ----

def main_handler(event, context):
    try:
        query = _parse_query(event)
        if not _check_auth(query, event.get("headers") or {}):
            return _resp(403, {"ok": False, "error": "bad token"})

        path = event.get("path") or ""
        body = _parse_body(event)
        method = (event.get("httpMethod")
                  or (event.get("requestContext") or {}).get("httpMethod")
                  or "").upper()

        if path.rstrip("/").endswith("/health") or method == "GET":
            return _resp(200, {"ok": True, "service": "huzhou-epi-scrape",
                               "columns": len(COLUMNS),
                               "auth": bool(os.environ.get("SCRAPE_TOKEN"))})
        if path.rstrip("/").endswith("/resolve"):
            return _resp(200, do_resolve(body))
        return _resp(200, do_scrape(body))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _resp(500, {"ok": False, "error": repr(e)})


# ---- 阿里云函数计算（HTTP 函数，WSGI 风格） ----

def handler(environ, start_response):
    from urllib.parse import parse_qs, urlparse
    try:
        qs = parse_qs(urlparse(environ.get("PATH_INFO", "") or environ.get("RAW_URI", "")).query)
        query = {k: v[0] for k, v in qs.items()}
        headers = {}
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                headers[k[5:].lower().replace("_", "-")] = v
        if not _check_auth(query, headers):
            start_response("403 Forbidden", [("Content-Type", "application/json")])
            return [json.dumps({"ok": False, "error": "bad token"}).encode()]

        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length) if length else b""
        body = json.loads(raw.decode("utf-8", "replace")) if raw else {}

        path = environ.get("PATH_INFO", "") or environ.get("RAW_URI", "")
        if (environ.get("REQUEST_METHOD") or "").upper() == "GET":
            out = {"ok": True, "service": "huzhou-epi-scrape", "columns": len(COLUMNS)}
        elif path.rstrip("/").endswith("/resolve"):
            out = do_resolve(body)
        else:
            out = do_scrape(body)

        payload = json.dumps(out, ensure_ascii=False).encode()
        start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                                  ("Content-Length", str(len(payload)))])
        return [payload]
    except Exception as e:
        import traceback
        traceback.print_exc()
        payload = json.dumps({"ok": False, "error": repr(e)}, ensure_ascii=False).encode()
        start_response("500 Internal Server Error", [("Content-Type", "application/json")])
        return [payload]


# 本地试跑：python3 cloud/main.py
if __name__ == "__main__":
    print(json.dumps(do_scrape({"limit": 3, "max_new": 3}),
                     ensure_ascii=False)[:800])

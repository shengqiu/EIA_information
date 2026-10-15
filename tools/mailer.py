#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按「项目」粒度把环评公示发邮件，附件打包随信发送。

一条公告里通常有好几个项目，这里**一个项目一封邮件**：
  - 默认只发链接：带附件的项目正文给政务网直链（境内可打开），**不下载附件本体**
  - 显式 --attach 才下载并打包小附件（≤ --max-attach，默认 30MB）进邮件
  - 没有附件的项目也发，正文只放项目信息

为什么是 30MB 而不是 50MB：邮件附件走 base64，体积膨胀 4/3，
再加上 QQ 服务器会追加 Received / X-QQ-* 头，留 20% 余量才不会在最后一步被拒。

配置全部走环境变量（跟 fetch_remote.py 的 SCRAPE_* 一个风格）：

    SMTP_HOST  默认 smtp.qq.com
    SMTP_PORT  默认 465（SSL）
    SMTP_USER  发件邮箱地址
    SMTP_PASS  16 位 SMTP 授权码（不是登录密码）
    MAIL_FROM  发件人，默认取 SMTP_USER
    MAIL_TO    收件人，默认 frankqiu2020@foxmail.com，多个用逗号分隔

    COS_SECRET_ID / COS_SECRET_KEY / COS_REGION / COS_BUCKET / COS_APPID
        可选。配了才做 COS 备份；没配或没权限就只发政务网直链，不影响发信。

用法：
    python3 tools/mailer.py --dry-run                 # 只看清单，不发信
    python3 tools/mailer.py --limit 3                 # 试发 3 封
    python3 tools/mailer.py --since 30 --yes          # 补发最近 30 天
    python3 tools/mailer.py --unit 长兴分局 --keyword 纺织
    python3 tools/mailer.py --no-cos --max-attach 20  # 不做 COS 备份，附件上限 20MB
    python3 tools/mailer.py --link-only --yes         # 境外/Actions：只发网关直链，不下载附件
"""

import os
import re
import io
import sys
import csv
import json
import time
import hmac
import argparse
import zipfile
import hashlib
import smtplib
import mimetypes
import http.client
import urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from email import encoders
from email.header import Header
from email.utils import formataddr, formatdate, make_msgid
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ITEMS = os.path.join(ROOT, "data", "items.json")
STATE = os.path.join(ROOT, "data", "mail_state.json")
# 直链映射：优先仓库内 data/oss_links.csv（跟着 git 走，Actions 也能读）；
# files/direct_links.csv 是境内 download.py --resolve 的本地产出，作为补充合并。
LINK_CACHE = os.path.join(ROOT, "data", "oss_links.csv")
LINK_CACHE_LOCAL = os.path.join(ROOT, "files", "direct_links.csv")
WORKDIR = os.path.join(ROOT, "files", "mail")

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MSG_LIMIT = 45 * 1024 * 1024        # 编码后 MIME 硬上限（QQ 按约 50MB 算，留余量）
DEFAULT_TO = "frankqiu2020@foxmail.com"
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


# ---------------------------------------------------------------- 基础工具
def log(msg):
    print(f"[{datetime.now(CST).strftime('%H:%M:%S')}] {msg}", flush=True)


def safe_name(s, maxlen=90):
    s = ILLEGAL.sub("_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:maxlen] if len(s) > maxlen else s


def load_env_file():
    """
    懒得每次 export 的话，把配置写进项目根的 .env（已在 .gitignore 里，不会入库）。
    命令行里显式设过的环境变量优先，这里只做兜底。
    """
    for p in (os.path.join(ROOT, ".env"), os.path.join(ROOT, "tools", ".env")):
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
        except Exception:
            pass


def human(n):
    n = n or 0
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f}{u}" if u != "B" else f"{int(n)}B"
        n /= 1024.0


def parse_size(s):
    """'43.2MB' / '735.0KB' / '144157795' → 字节"""
    s = (s or "").strip()
    m = re.match(r"^([\d.]+)\s*(B|KB|K|MB|M|GB|G)?$", s, re.I)
    if not m:
        return 0
    v = float(m.group(1))
    u = (m.group(2) or "B").upper()
    if u in ("B", ""):
        return int(v)
    if u.startswith("K"):
        return int(v * 1024)
    if u.startswith("M"):
        return int(v * 1024 * 1024)
    return int(v * 1024 * 1024 * 1024)


def parse_filename(url):
    """优先取 query 里的 fileName，兜底用路径最后一段"""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if q.get("fileName"):
        return urllib.parse.unquote(q["fileName"][0])
    tail = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    return urllib.parse.unquote(tail) or "attachment"


def project_key(it, p):
    """同一公告内项目不重名，但"受理→审批"两阶段会跨公告重名，所以必须带上 art_id"""
    raw = "|".join([it.get("art_id", ""), p.get("项目名称", ""), p.get("单位", "")])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 状态
def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
    else:
        st = {}
    st.setdefault("version", 1)
    st.setdefault("sent", {})
    st.setdefault("failed", {})
    st.setdefault("dead", {})
    return st


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


# ---------------------------------------------------------------- 读数据
def load_projects(args):
    """展平成「一项目一条」；没有附件的项目也要，正文只放信息"""
    if not os.path.exists(ITEMS):
        sys.exit(f"找不到 {ITEMS}，先跑 python3 monitor.py")

    with open(ITEMS, encoding="utf-8") as f:
        items = json.load(f)

    cutoff = None
    if args.since is not None:
        cutoff = (datetime.now(CST).date() - timedelta(days=args.since)).isoformat()

    out = []
    for it in items:
        date = it.get("发布日期") or it.get("列表日期") or ""
        if args.date_from and date < args.date_from:
            continue
        if cutoff and date < cutoff:
            continue
        if args.unit and args.unit not in it.get("unit", ""):
            continue

        for p in it.get("项目") or []:
            hay = " ".join([p.get("项目名称", ""), p.get("单位", ""), p.get("地点", "")])
            if args.keyword and args.keyword not in hay:
                continue
            atts = [u for u in (p.get("附件链接") or []) if u]
            out.append({
                "key": project_key(it, p),
                "date": date,
                "unit": it.get("unit", ""),
                "type": it.get("类型", ""),
                "title": it.get("title", ""),
                "art_url": it.get("url", ""),
                "name": p.get("项目名称") or "(未命名项目)",
                "org": p.get("单位", ""),
                "loc": p.get("地点", ""),
                "agency": p.get("机构", ""),
                "accept": p.get("受理日期", ""),
                "note": p.get("备注", ""),
                "atts": [{"url": u, "filename": parse_filename(u)} for u in atts],
            })
    out.sort(key=lambda r: (r["date"], r["unit"]), reverse=True)
    return out


def load_cache():
    """网关URL → (OSS直链, 大小) 的映射。两个来源都合并：
    data/oss_links.csv 入库随 git 走（Actions 可读），
    files/direct_links.csv 是境内 resolve 的本地产出（后者覆盖前者）"""
    cache = {}
    for path in (LINK_CACHE, LINK_CACHE_LOCAL):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    g = (row.get("网关地址") or "").strip()
                    oss = (row.get("OSS直链") or "").strip()
                    # 只收真直链：解析失败的行 OSS直链 列会退回网关 URL，不能当缓存。
                    # 政务外网专用域名（cms.zjzwfw.gov.cn，A 记录是 10.x 私网地址）也会
                    # 以 http 开头且不含 hbj.huzhou.gov.cn，必须按 fileUrl= 特征排除。
                    if (g and oss.startswith("http")
                            and "hbj.huzhou.gov.cn" not in oss
                            and "fileUrl=" not in oss
                            and "/api-gateway/" not in oss):
                        cache[g] = (oss, parse_size(row.get("大小") or ""))
        except Exception:
            pass
    return cache


# ---------------------------------------------------------------- 解析 / 下载
def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def resolve(session, url, cache):
    """网关地址 → (OSS直链, 字节数)。先查缓存，没有就跟随重定向问一次"""
    if url in cache:
        return cache[url]
    try:
        r = session.get(url, headers={"Range": "bytes=0-0"},
                        timeout=40, stream=True, allow_redirects=True)
        cr = r.headers.get("Content-Range") or ""
        size = None
        if "/" in cr:
            try:
                size = int(cr.rsplit("/", 1)[1])
            except ValueError:
                size = None
        if size is None and (r.headers.get("Content-Length") or "").isdigit():
            size = int(r.headers["Content-Length"])
        final = r.url
        r.close()
        return final, size
    except Exception:
        return url, None


def resolve_all(records, cache, workers=6):
    """并发把每条记录的附件直链和体积问出来"""
    todo = [(r, a) for r in records for a in r["atts"]]
    if not todo:
        return

    def work(pair):
        _, a = pair
        sess = make_session()
        try:
            a["oss"], a["size"] = resolve(sess, a["url"], cache)
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as ex:
        list(ex.map(work, todo))

    for r in records:
        r["total"] = sum(a.get("size") or 0 for a in r["atts"])


def download(url, dest, tries=3):
    """流式下载，失败退避重试"""
    sess = make_session()
    try:
        for attempt in range(tries):
            try:
                with sess.get(url, timeout=120, stream=True) as r:
                    r.raise_for_status()
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            if chunk:
                                f.write(chunk)
                if os.path.getsize(dest) > 0:
                    return dest
            except Exception as e:
                if attempt == tries - 1:
                    raise
                time.sleep(2 * (attempt + 1))
        return None
    finally:
        sess.close()


def zipup(files, zippath):
    """源文件已经是 zip/rar/pdf，用 STORED 打包，别白烧 CPU 做 deflate"""
    os.makedirs(os.path.dirname(zippath), exist_ok=True)
    with zipfile.ZipFile(zippath, "w", zipfile.ZIP_STORED) as zf:
        for f in files:
            zf.write(f, os.path.basename(f))
    return zippath


# ---------------------------------------------------------------- COS 备份（可选）
class COS:
    """纯标准库手写腾讯云 COS XML 签名。任何一步没权限都安静降级，绝不打断发信"""

    def __init__(self):
        self.ok = False
        self.warned = False
        self.sid = os.environ.get("COS_SECRET_ID", "").strip()
        self.skey = os.environ.get("COS_SECRET_KEY", "").strip()
        self.region = os.environ.get("COS_REGION", "ap-shanghai").strip()
        bucket = os.environ.get("COS_BUCKET", "").strip()
        appid = os.environ.get("COS_APPID", "").strip()
        if bucket and appid and not bucket.endswith("-" + appid):
            bucket = f"{bucket}-{appid}"
        self.bucket = bucket
        if not (self.sid and self.skey and self.bucket):
            return
        self.host = f"{self.bucket}.cos.{self.region}.myqcloud.com"
        self.ok = True

    def _warn(self, msg):
        if not self.warned:
            log(f"  ⚠ COS 不可用（{msg}），只发政务网直链")
            self.warned = True
        self.ok = False

    def _auth(self, method, key):
        now = int(time.time())
        kt = f"{now};{now + 3600}"
        sign_key = hmac.new(self.skey.encode(), kt.encode(), hashlib.sha1).hexdigest()
        headers = {"host": self.host}
        hl = "host"
        hh = urllib.parse.urlencode(headers)          # 只签 host，绕开转义坑
        http_string = (f"{method.lower()}\n"
                       f"{urllib.parse.quote(key, safe='/')}\n\n{hh}\n")
        sts = ("sha1\n" + kt + "\n"
               + hashlib.sha1(http_string.encode()).hexdigest() + "\n")
        sig = hmac.new(sign_key.encode(), sts.encode(), hashlib.sha1).hexdigest()
        return (f"q-sign-algorithm=sha1&q-ak={self.sid}&q-sign-time={kt}"
                f"&q-key-time={kt}&q-header-list={hl}&q-url-param-list=&q-signature={sig}")

    def _request(self, method, key, body=None, extra=None):
        path = "/" + urllib.parse.quote(key.lstrip("/"), safe="/")
        headers = {"Host": self.host, "Authorization": self._auth(method, "/" + key.lstrip("/"))}
        if extra:
            headers.update(extra)
        conn = http.client.HTTPSConnection(self.host, 443, timeout=120)
        try:
            conn.putrequest(method, path)
            for k, v in headers.items():
                conn.putheader(k, v)
            if body is None:
                conn.putheader("Content-Length", "0")
                conn.endheaders()
            else:
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders()
                conn.send(body)
            resp = conn.getresponse()
            resp.read()                      # 丢弃响应体，只为拿状态码
            return resp.status
        finally:
            conn.close()

    def send_body(self, method, key, filepath, size):
        """137MB 的文件不能整个读进内存，分块 send"""
        path = "/" + urllib.parse.quote(key.lstrip("/"), safe="/")
        headers = {"Host": self.host,
                   "Authorization": self._auth(method, "/" + key.lstrip("/")),
                   "Content-Length": str(size)}
        conn = http.client.HTTPSConnection(self.host, 443, timeout=300)
        try:
            conn.putrequest(method, path)
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders()
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    conn.send(chunk)
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    def ensure_bucket(self):
        try:
            st = self._request("PUT", "/",
                               body=b"<CreateBucketConfiguration>"
                                    b"<LocationConstraint>" + self.region.encode() +
                                    b"</LocationConstraint></CreateBucketConfiguration>",
                               extra={"Content-Type": "application/xml"})
            if st not in (200, 409):        # 409 = 已存在（且是你的）
                self._warn(f"建桶返回 {st}")
        except Exception as e:
            self._warn(f"建桶异常 {type(e).__name__}")

    def put(self, filepath, key):
        """上传并返回公开读直链；失败返回 None"""
        if not self.ok:
            return None
        try:
            size = os.path.getsize(filepath)
            if self._request("HEAD", key) == 200:
                pass                                     # 已存在，跳过重传
            else:
                st = self.send_body("PUT", key, filepath, size)
                if st != 200:
                    self._warn(f"上传返回 {st}")
                    return None
            return (f"https://{self.host}/"
                    + urllib.parse.quote(key.lstrip("/"), safe="/"))
        except Exception as e:
            self._warn(f"上传异常 {type(e).__name__}")
            return None


# ---------------------------------------------------------------- 邮件构造
def _h(s):
    return Header(s or "", "utf-8").encode()


def build_mail(rec, cfg, zippath=None, backup=None):
    """
    一条项目一封邮件。
    zippath 有值 → 附件模式；否则 → 链接模式（正文给政务网直链，可选 COS 备份链）
    """
    rows = [
        ("项目名称", rec["name"]),
        ("建设地点", rec["loc"]),
        ("建设单位", rec["org"]),
        ("环评机构", rec["agency"]),
        ("受理日期", rec["accept"]),
        ("公告类型", rec["type"]),
        ("发布单位", rec["unit"]),
        ("发布日期", rec["date"]),
    ]
    tr = "".join(
        f'<tr><th style="text-align:left;padding:6px 10px;color:#6b7280;'
        f'white-space:nowrap;vertical-align:top">{k}</th>'
        f'<td style="padding:6px 10px">{v or "—"}</td></tr>'
        for k, v in rows if v or k in ("项目名称", "建设单位")
    )

    if zippath:
        att_html = (f'<p style="margin:14px 0 4px"><b>附件</b>（{human(rec.get("zip_bytes"))}，'
                    f'已打包为本邮件附件）：</p><ul>'
                    + "".join(f'<li>{a["filename"]}'
                              + (f' · {human(a.get("size"))}' if a.get("size") else "")
                              + "</li>" for a in rec["atts"])
                    + "</ul>")
    elif rec["atts"]:
        lis = []
        for a in rec["atts"]:
            oss, gw = a.get("oss"), a["url"]
            size = f' · {human(a.get("size"))}' if a.get("size") else ""
            links = f'<a href="{gw}">政务网网关</a>'
            if oss:
                links += f'　｜　<a href="{oss}">OSS 直链（境外可下）</a>'
            extra = ""
            if backup and backup.get(a["url"]):
                extra = f'　｜　<a href="{backup[a["url"]]}">COS 备份</a>'
            lis.append(f'<li>{a["filename"]}{size}　→　{links}{extra}</li>')
        att_html = ('<p style="margin:14px 0 4px"><b>附件</b>（请从下方链接下载）：</p>'
                    '<ul>' + "".join(lis) + "</ul>")
    else:
        att_html = '<p style="margin:14px 0 4px">该公告未提供附件下载。</p>'

    html = f"""<div style="font:14px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;color:#1f2430">
<h3 style="margin:0 0 12px;color:#1f7a4d">[环评公示] 湖州市生态环境局 · 项目推送</h3>
<table style="border-collapse:collapse">{tr}</table>
<p style="margin:14px 0 4px"><b>所属公告</b>：{rec["title"]}</p>
<p style="margin:0"><a href="{rec["art_url"]}">{rec["art_url"]}</a></p>
{att_html}
{f'<p style="color:#6b7280">{rec["note"]}</p>' if rec.get("note") else ""}
<hr style="border:0;border-top:1px solid #e6e9ef;margin:18px 0">
<p style="color:#9ca3af;font-size:12px">本邮件由 huzhou 监测脚本自动发送 · {rec["key"]}</p>
</div>"""

    plain = "\n".join([
        "[环评公示] 湖州市生态环境局 · 项目推送",
        f"项目名称：{rec['name']}",
        f"建设地点：{rec['loc'] or '—'}",
        f"建设单位：{rec['org'] or '—'}",
        f"环评机构：{rec['agency'] or '—'}",
        f"受理日期：{rec['accept'] or '—'}",
        f"发布单位：{rec['unit']}　发布日期：{rec['date']}",
        f"所属公告：{rec['title']}",
        f"公告链接：{rec['art_url']}",
    ])
    if zippath:
        plain += "\n附件：" + "、".join(a["filename"] for a in rec["atts"]) + "（见邮件附件）"
    else:
        for a in rec["atts"]:
            plain += f"\n附件：{a['filename']} {a['url']}"
            if a.get("oss"):
                plain += f"\n　OSS 直链：{a['oss']}"
            if backup and backup.get(a["url"]):
                plain += f"\n　备份：{backup[a['url']]}"

    mixed = MIMEMultipart("mixed")
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    mixed.attach(alt)

    # [环评公示] 固定在最前；单位放项目名前面，手机上不用点开就知道是哪个分局
    name_part = rec["name"][:40]
    subj = (f"[环评公示] {rec['unit']} · {name_part}" if rec["unit"]
            else f"[环评公示] {name_part}")
    mixed["Subject"] = _h(subj)
    mixed["From"] = formataddr(("湖州环评公示", cfg["from"]))
    mixed["To"] = ", ".join(cfg["to"])
    mixed["Date"] = formatdate(localtime=True)
    mixed["Message-ID"] = make_msgid("huzhou")
    return mixed


def attach_file(msg, path, name):
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    main, sub = ctype.split("/", 1)
    part = MIMEBase(main, sub)
    with open(path, "rb") as f:
        part.set_payload(f.read())
    encoders.encode_base64(part)
    # 老客户端不认 RFC2231，给个纯 ASCII 兜底名；连续非 ASCII 折叠成一个下划线。
    # 全中文名会被折没了，这时退化成 attachment + 原扩展名
    ascii_fb = re.sub(r"[^\x20-\x7e]+", "_", name).strip("_")
    if not ascii_fb or ascii_fb.startswith("."):
        ascii_fb = "attachment" + (os.path.splitext(name)[1] or ".zip")
    # RFC 5987/2231：filename 给 ASCII 兜底，filename* 给 UTF-8 原文
    part["Content-Disposition"] = (
        f'attachment; filename="{ascii_fb}"; '
        f"filename*=UTF-8''{urllib.parse.quote(name)}"
    )
    msg.attach(part)


def mail_size(msg):
    """编码后的真实体积——唯一可靠的判断口径"""
    return len(msg.as_bytes())


# ---------------------------------------------------------------- 发送
def connect(cfg):
    s = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=60)
    s.login(cfg["user"], cfg["pass"])
    return s


def send_one(msg, smtp, cfg):
    return smtp.sendmail(cfg["from"], cfg["to"], msg.as_bytes())


def is_temp_err(e):
    """只对瞬时错误重试；5xx 拒收重试也没用"""
    code = getattr(e, "smtp_code", None)
    if code and str(code).startswith("5"):
        return False
    return True


def wait_interval(st, interval):
    """
    保证两封之间至少隔 interval 秒。

    单次只发一封时，脚本内部的间隔逻辑用不上（最后一封后面不 sleep），
    所以改成跨运行记账：state 里存上次发信时刻，这次不够钟就先等。
    这样用定时任务每 10 分钟跑一次、或者手动连着跑，节奏都一样。
    """
    last = st.get("last_sent_at")
    if not last or interval <= 0:
        return
    try:
        # last_sent_at 是按 CST 写的，得补回时区，否则和 now(CST) 相减会炸
        t0 = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
    except ValueError:
        return
    elapsed = (datetime.now(CST) - t0).total_seconds()
    if elapsed < interval:
        wait = int(interval - elapsed) + 1
        log(f"距上次发信才 {int(elapsed)} 秒，按间隔要求等待 {wait} 秒"
            f"（{wait // 60} 分 {wait % 60} 秒）…")
        time.sleep(wait)


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="按项目发送湖州环评公示邮件")
    ap.add_argument("--since", type=int, default=30, help="只处理最近 N 天的项目（默认 30）")
    ap.add_argument("--date-from", default="", help="起始日期 YYYY-MM-DD，优先级高于 --since")
    ap.add_argument("--limit", type=int, default=None,
                    help="本次最多处理多少封（默认取 MAIL_LIMIT，再默认 1）")
    ap.add_argument("--unit", default="", help="只发某个发布单位（子串匹配）")
    ap.add_argument("--keyword", default="", help="项目名/单位/地点包含该关键词")
    ap.add_argument("--max-attach", type=int, default=30,
                    help="附件打包上限（MB，默认 30；超过就改发链接）")
    ap.add_argument("--link-only", action="store_true",
                    help="强制链接模式：带附件项目只发政务网直链，不下载（现在也是默认行为）")
    ap.add_argument("--attach", action="store_true",
                    help="允许把小附件（≤ --max-attach MB）下载并打包进邮件本体；默认不发附件，只发链接")
    ap.add_argument("--interval", type=float, default=None,
                    help="两封之间至少间隔多少秒（默认取 MAIL_INTERVAL，再默认 5）")
    ap.add_argument("--to", default="", help="收件人，逗号分隔，默认取 MAIL_TO 环境变量")
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不发信")
    ap.add_argument("--check", action="store_true",
                    help="只测 SMTP 能否连通并登录，不发任何邮件")
    ap.add_argument("--no-cos", action="store_true", help="不做 COS 备份")
    ap.add_argument("--force", action="store_true", help="忽略已发记录，重发")
    ap.add_argument("--keep", action="store_true", help="发完保留打包的 zip")
    ap.add_argument("--yes", action="store_true", help="数量多时不问确认")
    args = ap.parse_args()
    load_env_file()

    # 节奏参数允许写进 .env，免得每次敲长命令
    if args.limit is None:
        args.limit = int(os.environ.get("MAIL_LIMIT", "1") or 1)
    if args.interval is None:
        args.interval = float(os.environ.get("MAIL_INTERVAL", "5") or 5)

    cfg = {
        "host": os.environ.get("SMTP_HOST", "smtp.qq.com").strip(),
        "port": int(os.environ.get("SMTP_PORT", "465")),
        "user": os.environ.get("SMTP_USER", "").strip(),
        "pass": os.environ.get("SMTP_PASS", "").strip(),
        "from": os.environ.get("MAIL_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip(),
    }
    to_raw = args.to or os.environ.get("MAIL_TO", "").strip() or DEFAULT_TO
    cfg["to"] = [t.strip() for t in to_raw.split(",") if t.strip()]

    if not args.dry_run and not (cfg["user"] and cfg["pass"]):
        sys.exit("缺少 SMTP_USER / SMTP_PASS。QQ 邮箱请填发件地址和 16 位授权码（不是登录密码）；"
                 "只想看清单就加 --dry-run")

    # 只验连通性：连上、登录、握手，然后立刻退出，不投递任何邮件
    if args.check:
        log(f"测试 {cfg['host']}:{cfg['port']} …")
        try:
            s = connect(cfg)
            code, _ = s.noop()
            s.quit()
            log(f"✓ SMTP 连通并登录成功（noop 返回 {code}）")
            log(f"  发件人 {cfg['from']}")
            log(f"  收件人 {', '.join(cfg['to'])}")
            log("配置没问题，可以去掉 --check 正式发送了（建议先 --limit 3 试发）")
            return 0
        except Exception as e:
            log(f"✗ 失败：{type(e).__name__}: {e}")
            log("  常见原因：① 填的是登录密码而不是 16 位授权码；"
                "② 邮箱没开启 IMAP/SMTP 服务；③ 端口/加密方式不对")
            return 1

    records = load_projects(args)
    st = load_state()
    if not args.force:
        before = len(records)
        records = [r for r in records
                   if r["key"] not in st["sent"] and r["key"] not in st["dead"]]
        skipped = before - len(records)
    else:
        skipped = 0
    if args.limit:
        records = records[:args.limit]

    log(f"待处理 {len(records)} 个项目（已发/已放弃跳过 {skipped}）→ 收件人 {', '.join(cfg['to'])}")
    if not records:
        return 0

    # 默认只发链接（带附件项目正文给政务网直链），不下载附件本体；
    # 只有显式 --attach 才允许把小附件打包进邮件。--link-only 是默认值别名，保留兼容。
    if args.link_only or not args.attach:
        # 链接模式不下载附件，但用仓库里的直链映射（data/oss_links.csv）查表，
        # 把 OSS 直链和体积填进 atts——纯本地查表，境外 Actions 上也安全。
        cache = load_cache()
        n_oss = n_att_all = 0
        for r in records:
            for a in r["atts"]:
                n_att_all += 1
                hit = cache.get(a["url"])
                if hit:
                    a["oss"], a["size"] = hit
                    n_oss += 1
            r["total"] = sum(a.get("size") or 0 for a in r["atts"])
            r["mode"] = "info" if not r["atts"] else "link"
        log(f"链接模式：跳过附件下载；{n_oss}/{n_att_all} 个附件查到 OSS 直链"
            f"（其余只发网关链接）")
    else:
        log("解析附件直链与体积…")
        resolve_all(records, load_cache())
        raw_limit = args.max_attach * 1024 * 1024
        for r in records:
            if not r["atts"]:
                r["mode"] = "info"                   # 无附件，正文只放项目信息
            elif (r.get("total") or 0) <= raw_limit:
                r["mode"] = "attach"
            else:
                r["mode"] = "link"

    # ---- dry-run：打印清单就收工
    if args.dry_run:
        n_att = sum(1 for r in records if r["mode"] == "attach")
        n_link = sum(1 for r in records if r["mode"] == "link")
        log(f"{'模式':<7} {'体积':>9}  {'单位':<10} 项目")
        for r in records:
            tot = r.get("total") or 0
            log(f"{r['mode']:<7} {human(tot):>9}  {r['unit'][:10]:<10} {r['name'][:44]}"
                + (f"　附件×{len(r['atts'])}" if r["atts"] else "　（无附件）"))
        log(f"合计 {len(records)} 封：带附件发送 {n_att}、大附件发链接 {n_link}、"
            f"仅项目信息 {len(records)-n_att-n_link}"
            f"，附件总计 {human(sum(r.get('total') or 0 for r in records))}")
        log("--dry-run：未发信")
        return 0

    if len(records) > 50 and not args.yes:
        ans = input(f"确认向 {', '.join(cfg['to'])} 发送 {len(records)} 封邮件？[y/N] ")
        if ans.strip().lower() != "y":
            log("已取消")
            return 1

    cos = None if args.no_cos else COS()
    if cos and cos.ok:
        log("COS 备份已启用，先确认存储桶")
        cos.ensure_bucket()

    smtp = connect(cfg)
    log(f"SMTP {cfg['host']}:{cfg['port']} 已连接")
    os.makedirs(WORKDIR, exist_ok=True)

    ok = fail = 0
    for idx, r in enumerate(records, 1):
        tag = f"[{idx}/{len(records)}] {r['name'][:34]}"
        zippath = None
        backup = {}
        try:
            if r["mode"] == "attach":
                files = []
                for j, a in enumerate(r["atts"]):
                    dest = os.path.join(WORKDIR, r["key"], safe_name(a["filename"], 120))
                    log(f"{tag} ↓ {a['filename'][:40]} {human(a.get('size'))}")
                    download(a["url"], dest)
                    files.append(dest)
                if len(files) == 1 and files[0].lower().endswith(".zip"):
                    zippath = files[0]                      # 本来就一个 zip，不用再包一层
                else:
                    zipname = safe_name(
                        f"{r['date']}_{r['unit']}_{r['name'][:50]}_{r['key'][:8]}.zip", 120)
                    zippath = zipup(files, os.path.join(WORKDIR, r["key"], zipname))
                r["zip_bytes"] = os.path.getsize(zippath)
            elif r["atts"] and cos and cos.ok:
                for a in r["atts"]:
                    src = os.path.join(WORKDIR, r["key"], safe_name(a["filename"], 120))
                    if r.get("total", 0) <= 200 * 1024 * 1024:      # 太大就不费劲备份了
                        log(f"{tag} ↑ COS 备份 {a['filename'][:36]}")
                        download(a["url"], src)
                        u = cos.put(src, f"huzhou/{r['date']}/{r['key']}/{a['filename']}")
                        if u:
                            backup[a["url"]] = u

            msg = build_mail(r, cfg, zippath, backup)
            if zippath:
                attach_file(msg, zippath, os.path.basename(zippath))
                if mail_size(msg) > MSG_LIMIT:                 # 精判：超了就降级成链接版
                    log(f"{tag} 打包后 {human(mail_size(msg))} 超限，改为链接模式")
                    msg = build_mail(r, cfg, None, backup)
                    r["mode"] = "link"

            wait_interval(st, args.interval)          # 不够钟就先等，防被判 spam
            tries, sent = 0, False
            while tries < 3 and not sent:
                tries += 1
                try:
                    smtp.noop()
                    send_one(msg, smtp, cfg)
                    sent = True
                except Exception as e:
                    if tries >= 3 or not is_temp_err(e):
                        raise
                    log(f"{tag} 第 {tries} 次失败（{type(e).__name__}），退避重试")
                    time.sleep(5 * tries)
                    try:
                        smtp = connect(cfg)
                    except Exception:
                        pass

            if sent:
                ok += 1
                now = datetime.now(CST)
                st["sent"][r["key"]] = {
                    "at": now.strftime("%Y-%m-%d %H:%M"),
                    "project": r["name"], "unit": r["unit"], "date": r["date"],
                    "mode": r["mode"], "bytes": mail_size(msg),
                }
                st["last_sent_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                st["failed"].pop(r["key"], None)
                log(f"{tag} ✓ 已发送（{r['mode']}，{human(mail_size(msg))}）")
            save_state(st)
        except Exception as e:
            fail += 1
            rec = st["failed"].get(r["key"], {"tries": 0})
            rec.update({"tries": rec.get("tries", 0) + 1,
                        "last": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
                        "err": f"{type(e).__name__}: {str(e)[:120]}"})
            st["failed"][r["key"]] = rec
            if rec["tries"] >= 3:
                st["dead"][r["key"]] = rec
                st["failed"].pop(r["key"])
            log(f"{tag} ✗ 失败：{rec['err']}")
        finally:
            if not args.keep:
                d = os.path.join(WORKDIR, r["key"])
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        try:
                            os.remove(os.path.join(d, f))
                        except Exception:
                            pass
                    try:
                        os.rmdir(d)
                    except Exception:
                        pass
            # 节奏统一由 wait_interval() 管（它按 last_sent_at 记账，跨运行也有效），
            # 这里不能再 sleep 一次，否则两处叠加会变成双倍间隔

    try:
        smtp.quit()
    except Exception:
        pass
    save_state(st)

    log(f"完成：成功 {ok} 封，失败 {fail} 封")
    if st["failed"]:
        log(f"待重试 {len(st['failed'])} 个（下次运行会自动重试）")
    if st["dead"]:
        log(f"已放弃 {len(st['dead'])} 个（连续失败 3 次）")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

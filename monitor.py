#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
环评审批数据导入
================
定时采集湖州市生态环境局「行政审批（许可）公示」下所有环评相关栏目的公示公告，
解析公告内的项目明细（项目名称 / 建设地点 / 建设单位 / 环评机构 / 受理日期 / 附件），
去重后生成 HTML 看板。

栏目清单 ------------------------------------------------------------
非辐环评审批：市局 吴兴 南太湖新区 南浔 德清 长兴 安吉 长合（分局）
辐射项目审批：市局 吴兴 南浔 德清 长兴 安吉 南太湖新区
建设项目环境影响评价信息公示

列表页数据接口（无需浏览器）
----------------------------------------------------------------
栏目列表由 JS 动态渲染，但底层是一个返回 JSON 的 HTTP 接口，可直接在
Cloudflare Worker / 任意无浏览器环境里 fetch：

    GET https://hbj.huzhou.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit
        ?parseType=bulidstatic&webId=3630&tplSetId=5HrZiFGOR8vHklhTYqM4B
        &pageType=column&tagId=zw&editType=null&pageId=<colId>

返回 { data: { html: "..." } }，html 里就是带日期的列表。
注意：个别栏目（如建设项目环评信息公示 1229858680）用的是 tagId=list，
代码里做成 zw → list 依次回退。

用法 ----------------------------------------------------------------
python3 monitor.py            # 采集 + 生成看板（默认每栏目抓最新 12 条）
python3 monitor.py --limit 20 # 调整每栏目抓取条数
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

BASE = "https://hbj.huzhou.gov.cn"
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
REPORT_DIR = os.path.join(ROOT, "reports")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
ITEMS_PATH = os.path.join(DATA_DIR, "items.json")
HTML_PATH = os.path.join(REPORT_DIR, "index.html")

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# colId -> (发布单位, 大类)
COLUMNS = {
    "1229208550": ("市局", "非辐环评审批"),
    "1229208551": ("吴兴分局", "非辐环评审批"),
    "1229208552": ("南太湖新区分局", "非辐环评审批"),
    "1229208553": ("南浔分局", "非辐环评审批"),
    "1229208554": ("德清分局", "非辐环评审批"),
    "1229208555": ("长兴分局", "非辐环评审批"),
    "1229208556": ("安吉分局", "非辐环评审批"),
    "1229691710": ("长合分局", "非辐环评审批"),
    "1229208558": ("市局", "辐射项目审批"),
    "1229208559": ("吴兴分局", "辐射项目审批"),
    "1229208560": ("南浔分局", "辐射项目审批"),
    "1229208561": ("德清分局", "辐射项目审批"),
    "1229208562": ("长兴分局", "辐射项目审批"),
    "1229208563": ("安吉分局", "辐射项目审批"),
    "1229208564": ("南太湖新区分局", "辐射项目审批"),
    "1229858680": ("全市", "建设项目环评信息公示"),
}

# 标题关键词 -> 公示类型
TYPE_RULES = [
    ("拟对", "拟审批公示"),
    ("拟作出", "拟审批公示"),
    ("作出审批决定", "审批决定公告"),
    ("审批决定", "审批决定公告"),
    ("受理", "受理公告"),
   ("信息公示", "环评信息公示"),
]

ATTACH_EXT = (".pdf", ".doc", ".docx", ".zip", ".rar", ".xls", ".xlsx", ".7z")


def looks_like_file(url):
    """判断一个链接是不是附件。

    官网站的下载链接把文件名藏在 query 里：
        /api-gateway/jpaas-web-server/front/document/download
            ?fileUrl=xxxxx&fileName=%E5%85%AC%E7%A4%BA%E7%A8%BF.zip
    所以不能只看 split('?')[0] 的路径后缀，得连 query 里的 fileName 一起判断。
    """
    if not url:
        return False
    low = url.lower()
    if any(low.split("?")[0].endswith(e) for e in ATTACH_EXT):
        return True
    m = re.search(r"[?&]filename=([^&]+)", low)
    return bool(m and any(m.group(1).endswith(e) for e in ATTACH_EXT))


# ---------------------------------------------------------------- 工具

def now_cst():
    return datetime.now(CST)


def log(msg):
    stamp = now_cst().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def clean_text(s):
    """压缩政府网站常见的乱空格（如 '20 2 6 年 9 月' 这种被切割的字符）。"""
    if not s:
        return ""
    s = s.replace("\u3000", " ")
    # 清掉政务站常见的断字/排版空格，顺带提高看板里的搜索命中率：
    #   "20 2 6 年"    → "2026年"
    #   "2026 年 9 月" → "2026年9月"
    #   "1700 台"      → "1700台"
    s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)
    s = re.sub(r"(?<=\d)\s+(?=[\u4e00-\u9fa5])", "", s)
    s = re.sub(r"(?<=[\u4e00-\u9fa5])\s+(?=\d)", "", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def guess_type(title):
    for kw, label in TYPE_RULES:
        if kw in title:
            return label
    return "其他公告"


def guess_area(unit, projects):
    """优先用公告里的建设地点判断区县，兜底用发布单位。"""
    areas = []
    for p in projects:
        loc = p.get("地点", "")
        for a in ("吴兴", "南浔", "德清", "长兴", "安吉", "南太湖新区"):
            if a in loc or a in p.get("项目名称", ""):
                if a not in areas:
                    areas.append(a)
    if areas:
        return "/".join(areas)
    return unit.replace("分局", "")


# ---------------------------------------------------------------- 列表页

LIST_API = (BASE + "/api-gateway/jpaas-publish-server/front/page/build/unit"
            "?parseType=bulidstatic&webId=3630&tplSetId=5HrZiFGOR8vHklhTYqM4B"
            "&pageType=column&tagId={tag}&editType=null&pageId={col}")
ALT_TAGS = ("zw", "list")

ART_RE = re.compile(r'href="(/col/col\d+/art/(\d+)/art_([0-9a-zA-Z]+)\.html)"[^>]*>\s*([^<]+)</a>')
DATE_RE = re.compile(r'class="time"[^>]*>\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})')


def norm_date(d):
    return d.replace("/", "-") if d else ""


def fetch_list(col_id, limit, session=None):
    """列表页：直接打底层 JSON 接口（无需浏览器）。"""
    unit, category = COLUMNS.get(col_id, ("未知", "未知"))
    s = session or requests.Session()
    html = ""
    for tag in ALT_TAGS:
        r = s.get(LIST_API.format(tag=tag, col=col_id), headers={"User-Agent": UA}, timeout=30)
        r.encoding = "utf-8"
        j = r.json()
        chunk = (j.get("data") or {}).get("html") or ""
        if len(chunk) > len(html):
            html = chunk
        if "art_" in html:
            break

    arts = ART_RE.findall(html)
    dates = DATE_RE.findall(html)

    out, seen = [], set()
    for href, year, art_id, title in arts:
        if art_id in seen:
            continue
        seen.add(art_id)
        idx = arts.index((href, year, art_id, title))
        out.append({
            "art_id": art_id,
            "year": year,
            "col_id": col_id,
            "unit": unit,
            "category": category,
            "title": clean_text(title),
            "列表日期": norm_date(dates[idx]) if idx < len(dates) else "",
            "url": BASE + href,
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 详情页

def http_get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            r.encoding = r.apparent_encoding or "utf-8"
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"抓取失败 {url}: {last}")


HEADER_MAP = [
    ("项目名称", "项目名称"),
    ("建设地点", "地点"),
    ("建设单位", "单位"),
    ("环评机构", "机构"),
    ("环评编制单位", "机构"),
    ("受理日期", "受理日期"),
    ("公示日期", "受理日期"),
    ("环评文件", "附件"),
    ("建设内容", "备注"),
    ("项目概况", "备注"),
]


def direct_rows(table):
    """只取直接属于该 table（或 tbody/thead）的 tr，避免拿到嵌套子表的行。"""
    rows = []

    def walk(node):
        for child in node.children:
            if getattr(child, "name", None) is None:
                continue
            if child.name == "tr":
                rows.append(child)
            elif child.name in ("tbody", "thead", "tfoot"):
                walk(child)

    walk(table)
    return rows


def row_cells(row):
    """返回按 colspan 展开后的单元格文本。"""
    cells = []
    for c in row.find_all(["td", "th"], recursive=False):
        if c.find("table"):
            continue
        txt = clean_text(c.get_text(" "))
        try:
            span = max(1, int(c.get("colspan", "1")))
        except ValueError:
            span = 1
        cells.extend([txt] * span)
    return cells


def pick_table(soup):
    """在层层嵌套的政府站模板里，找出真正的项目明细表。"""
    hits = [t for t in soup.find_all("table")
            if "项目名称" in clean_text(t.get_text(" "))]
    if not hits:
        return None
    inner = [t for t in hits
             if not any(c is not t and c in hits for c in t.find_all("table"))]
    return min(inner or hits, key=lambda x: len(direct_rows(x)) * 100 + abs(len(x.find_all("tr")) - 4))


def parse_text_blocks(txt):
    """表格解析不到时的兜底：抓取正文里『序号. 项目名称（建设单位）』形式的列举。"""
    projects = []
    body = clean_text(txt)
    pat = re.compile(r"(?:^|\s)(\d{1,2})[\.、]\s*([^\n，,；;]{6,80}?(?:项目|工程|线|站|厂|园|中心|基地))")
    for m in pat.finditer(body):
        name = clean_text(m.group(2))
        if name and name not in [p["项目名称"] for p in projects]:
            projects.append({"项目名称": name})
    return projects[:30]


def parse_detail(item):
    html = http_get(item["url"])
    soup = BeautifulSoup(html, "html.parser")

    # 发布时间
    pub = ""
    txt = clean_text(soup.get_text(" "))
    m = re.search(r"时间[：:]\s*(\d{4}-\d{1,2}-\d{1,2})", txt)
    if m:
        pub = m.group(1)
    if not pub:
        m2 = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", item["title"])
        if m2:
            pub = f"{m2.group(1)}-{int(m2.group(2)):02d}-{int(m2.group(3)):02d}"
    if not pub:
        pub = item.get("列表日期", "")

    # 正文容器：取最大的那个 table / div
    projects = []
    table = pick_table(soup)
    if table:
        rows = direct_rows(table)
        keys, header_idx = [], None
        for ridx, row in enumerate(rows):
            cells = row_cells(row)
            if any("项目名称" in c for c in cells) and any(
                    ("建设单位" in c or "建设地点" in c or "环评机构" in c) for c in cells):
                header_idx = ridx
                keys = []
                for c in cells:
                    key = next((v for k, v in HEADER_MAP if k in c), None)
                    if not key and re.fullmatch(r"序\s*号", c or ""):
                        key = "序号"
                    keys.append(key)
                break
        if header_idx is not None and sum(1 for k in keys if k) >= 2:
            ncol = len(keys)
            for row in rows[header_idx + 1:]:
                cells = row_cells(row)
                if len(cells) != ncol:          # 列数对不上说明是页脚/说明行，跳过
                    continue
                rec = {}
                for k, v in zip(keys, cells):
                    if k and k not in ("序号", "附件") and v:
                        rec[k] = v
                link_cells = row.find_all("a", href=True)
                files = [a.get("href") for a in link_cells if looks_like_file(a.get("href", ""))]
                if files:
                    rec["附件链接"] = [BASE + f if f.startswith("/") else f for f in files]
                name = rec.get("项目名称", "")
                if not name or name == "项目名称" or re.fullmatch(r"\d{1,3}", name):
                    continue
                projects.append(rec)

    if not projects:
        projects = parse_text_blocks(txt)

    summary = ""
    m = re.search(r"(根据.{0,400})", txt.replace("\n", " "))
    if m:
        summary = clean_text(m.group(1))[:300]

    item = dict(item)
    item.update({
        "发布日期": pub,
        "类型": guess_type(item["title"]),
        "项目": projects,
        "摘要": summary,
        "抓取时间": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
    })
    item["区域"] = guess_area(item["unit"], projects)
    return item


# ---------------------------------------------------------------- 状态

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"initialized": False, "seen": {}}


def load_items():
    if os.path.exists(ITEMS_PATH):
        with open(ITEMS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


# ---------------------------------------------------------------- HTML

def build_html(items, new_ids, stat):
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    newest = {}
    for it in items:
        key = it["art_id"]
        if key not in newest:
            newest[key] = it

    ordered = sorted(newest.values(), key=lambda x: (x.get("发布日期", "") or "0000"), reverse=True)
    payload = []
    for it in ordered:
        payload.append({
            "id": it["art_id"],
            "title": it["title"],
            "url": it["url"],
            "date": it.get("发布日期", ""),
            "unit": it["unit"],
            "cat": it["category"],
            "type": it["类型"],
            "area": it["区域"],
            "projects": it["项目"],
            "summary": it.get("摘要", ""),
            "isNew": it["art_id"] in new_ids,
        })

    units = sorted({it["unit"] for it in ordered})
    types = sorted({it["类型"] for it in ordered})
    generated = now_cst().strftime("%Y-%m-%d %H:%M")
    new_count = len(new_ids)
    total_projects = sum(len(it["项目"]) for it in ordered)

    data_json = json.dumps(payload, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>环评审批数据导入</title>
<style>
:root {{
  --bg:#f5f7fa; --card:#fff; --line:#e6e9ef; --txt:#1f2430; --sub:#6b7280;
  --brand:#1f7a4d; --brand2:#e8f5ee; --warn:#d4380d; --warn-bg:#fff2e8;
  --shadow:0 1px 3px rgba(16,24,40,.06),0 1px 2px rgba(16,24,40,.04);
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--txt);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }}
header {{ background:linear-gradient(135deg,#0f5132,#1f7a4d); color:#fff; padding:22px 28px; }}
header h1 {{ margin:0; font-size:20px; letter-spacing:.5px; }}
header p {{ margin:6px 0 0; opacity:.85; font-size:13px; }}
.wrap {{ max-width:1200px; margin:0 auto; padding:18px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:16px; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; box-shadow:var(--shadow); }}
.stat b {{ display:block; font-size:24px; color:var(--brand); }}
.stat span {{ color:var(--sub); font-size:12px; }}
.bar {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:14px; }}
input,select {{ padding:8px 12px; border:1px solid var(--line); border-radius:8px; background:#fff; font-size:13px; }}
input {{ flex:1; min-width:220px; }}
.chip {{ padding:6px 12px; border:1px solid var(--line); background:#fff; border-radius:999px;
  cursor:pointer; font-size:12px; color:var(--sub); }}
.chip.on {{ background:var(--brand); border-color:var(--brand); color:#fff; }}
.group {{ margin-bottom:22px; }}
.group h2 {{ font-size:15px; margin:0 0 10px; padding-left:9px; border-left:3px solid var(--brand); }}
.item {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px;
  margin-bottom:8px; box-shadow:var(--shadow); }}
.item.new {{ border-color:#ffa39e; background:var(--warn-bg); }}
.row1 {{ display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; }}
.tag {{ font-size:11px; padding:2px 8px; border-radius:6px; background:var(--brand2); color:var(--brand); }}
.tag.unit {{ background:#eef2ff; color:#3949ab; }}
.tag.date {{ background:#f1f3f5; color:#495057; }}
.badge-new {{ font-size:11px; color:#fff; background:var(--warn); padding:2px 8px; border-radius:6px; }}
.item a {{ color:var(--txt); text-decoration:none; font-weight:500; }}
.item a:hover {{ color:var(--brand); text-decoration:underline; }}
table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:12.5px; }}
th,td {{ border:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }}
th {{ background:#fafbfc; font-weight:600; white-space:nowrap; }}
.summary {{ color:var(--sub); font-size:12.5px; margin-top:6px; }}
.empty {{ text-align:center; color:var(--sub); padding:40px 0; }}
footer {{ text-align:center; color:var(--sub); font-size:12px; padding:20px 0 34px; }}
</style>
</head>
<body>
<header>
  <h1>湖州市生态环境局 · 环评审批数据导入</h1>
  <p>数据来源：湖州市生态环境局官网「行政审批（许可）公示」| 更新于 {generated}</p>
</header>
<div class="wrap">
  <div class="stats">
    <div class="stat"><b>{new_count}</b><span>本次新增公告</span></div>
    <div class="stat"><b>{stat['total']}</b><span>在库公告总数</span></div>
    <div class="stat"><b>{total_projects}</b><span>涉及建设项目数</span></div>
    <div class="stat"><b>{len(units)}</b><span>覆盖发布单位</span></div>
  </div>
  <div class="bar">
    <input id="q" placeholder="搜索项目名称、建设单位、地点、关键字…">
    <select id="unitSel"><option value="">全部单位</option>{''.join(f'<option>{esc(u)}</option>' for u in units)}</select>
    <select id="typeSel"><option value="">全部类型</option>{''.join(f'<option>{esc(t)}</option>' for t in types)}</select>
    <span class="chip on" data-new="1">仅看新增</span>
    <span class="chip" data-new="0">全部</span>
  </div>
  <div id="list"></div>
</div>
<footer>由 WorkBuddy 自动化任务每日生成 · 数据以官网原文为准</footer>
<script>
const DATA = {data_json};
let onlyNew = true;
const q = document.getElementById('q');
const unitSel = document.getElementById('unitSel');
const typeSel = document.getElementById('typeSel');
const list = document.getElementById('list');

document.querySelectorAll('.chip[data-new]').forEach(c => {{
  c.onclick = () => {{
    onlyNew = c.dataset.new === '1';
    document.querySelectorAll('.chip[data-new]').forEach(x => x.classList.remove('on'));
    c.classList.add('on');
    render();
  }};
}});
q.oninput = unitSel.onchange = typeSel.onchange = render;

function projTable(ps) {{
  if (!ps || !ps.length) return '';
  let h = '<table><tr><th>#</th><th>项目名称</th><th>建设地点</th><th>建设单位</th><th>环评机构</th><th>受理日期</th><th>附件</th></tr>';
  ps.forEach((p, i) => {{
    const files = (p['附件链接'] || []).map(u => `<a href="${{u}}" target="_blank" rel="noopener">下载</a>`).join(' ') || '-';
    h += `<tr><td>${{i+1}}</td><td>${{p['项目名称']||'-'}}</td><td>${{p['地点']||'-'}}</td><td>${{p['单位']||'-'}}</td><td>${{p['机构']||'-'}}</td><td>${{p['受理日期']||'-'}}</td><td>${{files}}</td></tr>`;
  }});
  return h + '</table>';
}}

function render() {{
  const kw = q.value.trim().toLowerCase();
  const unit = unitSel.value, type = typeSel.value;
  const rows = DATA.filter(d => {{
    if (onlyNew && !d.isNew) return false;
    if (unit && d.unit !== unit) return false;
    if (type && d.type !== type) return false;
    if (kw) {{
      const hay = (d.title + ' ' + d.date + ' ' + d.unit + ' ' + d.area + ' ' +
        (d.projects || []).map(p => Object.values(p).join(' ')).join(' ')).toLowerCase();
      if (!hay.includes(kw)) return false;
    }}
    return true;
  }});
  if (!rows.length) {{
    list.innerHTML = '<div class="empty">没有符合条件的公示</div>';
    return;
  }}
  const groups = {{}};
  rows.forEach(d => (groups[d.unit] = groups[d.unit] || []).push(d));
  let html = '';
  Object.keys(groups).sort().forEach(u => {{
    html += `<div class="group"><h2>${{u}}（${{groups[u].length}}）</h2>`;
    groups[u].forEach(d => {{
      html += `<div class="item${{d.isNew ? ' new' : ''}}">
        <div class="row1">
          <span class="tag date">${{d.date || '未标注'}}</span>
          <span class="tag">${{d.type}}</span>
          <span class="tag unit">${{d.area}}</span>
          ${{d.isNew ? '<span class="badge-new">NEW</span>' : ''}}
          <a href="${{d.url}}" target="_blank" rel="noopener">${{d.title}}</a>
        </div>
        ${{projTable(d.projects)}}
        ${{d.summary ? `<div class="summary">${{d.summary}}</div>` : ''}}
      </div>`;
    }});
    html += '</div>';
  }});
  list.innerHTML = html;
}}
render();
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12, help="每个栏目抓取的最新条数")
    ap.add_argument("--max-new", type=int, default=60, help="单次最多解析的新公告数")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

    state = load_state()
    seen = state.get("seen", {})
    first_run = not state.get("initialized")

    log(f"开始采集 {len(COLUMNS)} 个栏目（每栏目取最新 {args.limit} 条）")
    candidates = []
    sess = requests.Session()
    for col_id in COLUMNS:
        try:
            got = fetch_list(col_id, args.limit, session=sess)
            log(f"  {COLUMNS[col_id][0]:<8} {COLUMNS[col_id][1]:<14} → {len(got)} 条")
            candidates.extend(got)
        except Exception as e:
            log(f"  [跳过] col{col_id}: {e}")

    uniq = {}
    for c in candidates:
        uniq.setdefault(c["art_id"], c)
    log(f"列表汇总 {len(uniq)} 条不重复公告，其中 {sum(1 for k in uniq if k not in seen)} 条未处理")

    if first_run:
        log("首次运行：建立历史基线（本次结果不计为新增）")
        max_new = 9999
    else:
        max_new = args.max_new

    targets = [v for k, v in uniq.items() if k not in seen][:max_new]
    items = load_items()
    new_ids = set()
    ok = fail = 0
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(parse_detail, t): t for t in targets}
        for fu in as_completed(futures):
            t = futures[fu]
            try:
                detail = fu.result()
            except Exception as e:
                fail += 1
                log(f"  ✗ {t['title'][:30]}: {e}")
                continue
            items.append(detail)
            seen[t["art_id"]] = now_cst().strftime("%Y-%m-%d %H:%M")
            if not first_run:
                new_ids.add(t["art_id"])
            ok += 1
            log(f"  ✓ {detail['发布日期'] or '?'} [{detail['unit']}] "
                f"{detail['title'][:38]}… ({len(detail['项目'])} 个项目)")

    # 历史库只保留近 18 个月，避免文件无限膨胀；
    # seen 不清理，否则旧公告会被当成「新增」反复刷出来。
    cutoff = (now_cst() - timedelta(days=550)).strftime("%Y-%m-%d")
    before = len(items)
    dedup, seen_keys = [], set()
    for i in sorted(items, key=lambda x: x.get("发布日期", ""), reverse=True):
        if i.get("发布日期", "") < cutoff or i["art_id"] in seen_keys:
            continue
        seen_keys.add(i["art_id"])
        dedup.append(i)
    items = dedup

    with open(ITEMS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"initialized": True, "seen": seen}, f, ensure_ascii=False, indent=1)

    html = build_html(items, new_ids, {"total": len({i["art_id"] for i in items})})
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    log(f"完成：解析成功 {ok} 条，失败 {fail} 条；在库 {len(items)} 条（清理 {before - len(items)} 条过期）；新增 {len(new_ids)} 条")
    log(f"看板已生成：{HTML_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

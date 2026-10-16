# 湖州环评公示监测

定时抓**湖州市生态环境局** 16 个栏目的环评公示公告，把公告里藏的**项目明细表格**
（项目名称 / 建设地点 / 建设单位 / 环评机构 / 受理日期）+ **附件链接**挖出来，
做成可搜索的看板，并把附件（环评报告书/报告表全本）送到 Google Drive。

- 线上看板：**https://shengqiu.github.io/huzhou/**
- 附件去向：Google Drive `湖州线索/<项目名称>/`（网页内容.txt + 解压后的附件）

## ⚠️ 一条约束决定整个架构：境外 IP 采不了政务站

`hbj.huzhou.gov.cn` 拒绝境外访问（实测 GitHub Actions 16 个栏目全部超时，504 秒 0 条）。
但附件真实存放在浙江政务云 OSS，**OSS 直链境外可达**。所以分工是：

> **采集在境内跑（云函数），Actions 在境外负责调度、同步和发布。**

## Action 目录

仓库共有 5 个 GitHub Actions，按依赖顺序：

| # | Action | 文件 | 触发条件 | 一句话职责 |
|---|---|---|---|---|
| 1 | 部署采集云函数 | `.github/workflows/deploy-scf.yml` | push `cloud/**`、手动 | 把境内采集器打包部署到腾讯云 SCF 并冒烟测试 |
| 2 | 每日采集并发布 | `.github/workflows/daily.yml` | 每天 09:00（CST）、手动 | 调云函数采新公告 → 渲染看板 → 提交 → 发布 Pages |
| 3 | 发布看板到 Pages | `.github/workflows/pages.yml` | push `reports/**`、`data/**`、手动 | 只发布不采集；境内手动推数据后的自动发布通道 |
| 4 | 附件同步 Google Drive | `.github/workflows/drive.yml` | Action 2 成功后、每天 10:30、push 脚本、手动 | 把附件按「项目」解压送进 Google Drive |
| 5 | 按项目发邮件 | `.github/workflows/mail.yml` | 仅手动 | 一个项目一封邮件，默认只发链接 |

---

## 每个 Action 做什么

### 1. 部署采集云函数（deploy-scf.yml）

维护 Action 2 路线 A 依赖的境内采集端点。`cloud/` 或 `monitor.py` 有改动推上 main 就自动跑：

1. `cloud/build.sh` 打 zip（把依赖按 SCF 的 Python 3.10 交叉打包）
2. `cloud/deploy_tc.py` 调腾讯云 API：更新代码 → 更新配置 → 开函数 URL
3. 冒烟测试：GET 函数 URL，断言 `ok=true`、`auth=true`、`columns=16`

缺 `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY` 两个 secret 时明确报错退出。
也可在 Actions 页手动 Run workflow。**不用再去控制台传 zip。**

### 2. 每日采集并发布（daily.yml）——数据链的源头

北京时间每天 09:00（UTC 01:00）自动跑，也可手动触发。两条采集路线二选一：

| | 路线 A（默认） | 路线 B |
|---|---|---|
| 条件 | 配了 secret `SCRAPE_URL` | 变量 `SCRAPE_MODE=local` |
| 采集在哪 | 境内云函数（腾讯云 SCF） | 本机 `monitor.py`，需自托管 runner（`RUNNER_LABEL=self-hosted`） |
| 怎么跑 | `tools/fetch_remote.py` 把已处理公告 id 名单 POST 给云函数，拿回新公告合并 | `python3 monitor.py` 直接抓 16 栏目 |

跑完紧接着：重试没解析出直链的附件 → `tools/render.py` 渲染看板 → **保险丝校验
（公告 0 条就整体退出，不提交不发布，线上看板不被清空）** → 提交 `data/` + `reports/`
→ 配置 Pages → 上传 → 部署。

### 3. 发布看板到 GitHub Pages（pages.yml）

纯发布通道，不采集。任何 push 到 main 且动了 `reports/` 或 `data/` 都会触发
（典型场景：境内跑完 `tools/publish.sh` 把看板推上来）。同样有保险丝：
看板数据 0 条拒绝发布。

> Action 2 内部已经自带发布；本 Action 是给**境内手动推送**准备的自动发布兜底。

### 4. 附件同步 Google Drive（drive.yml）

把 `data/oss_links.csv` 里的附件送进 Google Drive，目录结构**按项目扁平一层**：

```
湖州线索/
  └── <项目名称>/
        ├── 网页内容.txt      ← 公告标题/类型/日期/链接/项目明细/公示正文
        └── 解压后的附件       ← .pdf 直接传；.zip/.rar/.7z 下载解压后传
```

流程：装 rclone + bsdtar → 写 Drive 凭证（OAuth / Service Account 双模式自动识别）
→ `tools/to_drive.py`（先探活再同步）→ 把 `data/drive_state.json` 回写仓库断点续跑。
触发器有四类：Action 2 成功后紧接着、每天 10:30 CST 兜底、改了同步脚本立即验证、手动。

### 5. 按项目发邮件（mail.yml）

一个项目一封邮件，`tools/mailer.py --link-only --yes`：正文给政务网网关直链
（境内可打开）+ OSS 直链（境外可下），**不下载附件本体**。已发记录在
`data/mail_state.json`，跨运行续传。

> 定时发送已于 2026-10-15 停用（附件改走 Drive 后邮件不再是必经路径），
> 只在手动 Run workflow 时补发。恢复定时把 workflow 里注释的两行加回来。

---

## Action 之间的关系

```
  push cloud/** ──► ┌──────────────────────────────┐
                     │ 1. deploy-scf.yml            │  维护云函数
                     │   打包 → 更新 SCF → 冒烟测试  │
                     └──────────────┬───────────────┘
                                    │ 维护
                                    ▼
  每天 09:00 CST ──► ┌──────────────────────────────┐        ┌──────────────┐
                     │ 2. daily.yml                 │  POST  │ 境内云函数 SCF │
                     │   路线A: fetch_remote.py ────┼───────►│ 抓16栏目+解析  │
                     │   合并 → 渲染 → 提交 → 自己发布│◄───────┤ +OSS直链解析  │
                     └───────┬──────────────────────┘ 新公告  └──────────────┘
                             │ 成功完成
                             │（workflow_run；它的提交用 GITHUB_TOKEN，
                             │  不触发别的 workflow，所以发布放在自己流程里）
                             ▼
                     ┌──────────────────┐         ┌──────────────────┐
                     │ 4. drive.yml     │         │ 5. mail.yml      │
                     │ 读 oss_links.csv │         │ 读 items.json    │
                     │ + items.json     │         │ + oss_links.csv  │
                     │ → Google Drive   │         │ 独立，仅手动触发  │
                     └──────────────────┘         └──────────────────┘

  境内 ./tools/publish.sh 推送（PAT）──► push 事件 ──► ┌──────────────────┐
                                                      │ 3. pages.yml     │
                                                      │ 手动推送后的      │
                                                      │ 自动发布兜底      │
                                                      └──────────────────┘
```

三个关键点：

1. **deploy-scf 是基础设施**：它部署的云函数是 daily.yml 路线 A 的采集端点，
   改了采集代码（`cloud/**`）必须经过它才能生效。
2. **daily 是全链源头**：它写 `data/items.json`、`data/oss_links.csv`、
   `reports/index.html`，drive 和 mail 都读这两份数据。它成功后立即触发 drive。
3. **状态文件各自回写**：`drive_state.json` / `mail_state.json` 由对应 Action
   在跑完后提交回仓库（先 `pull --rebase` 再推，避免并发提交互踩），
   实现跨运行的断点续跑。`pages`、`drive`、`mail` 各自有 concurrency group 防并发。

数据流一览：

| 文件 | 谁写 | 谁读 |
|---|---|---|
| `data/items.json` | `monitor.py` / `fetch_remote.py` | `render.py`、`to_drive.py`、`mailer.py` |
| `data/oss_links.csv` | 云函数解析直链 → `fetch_remote.py` 合并 | `to_drive.py`、`mailer.py` |
| `reports/index.html` | `render.py` | `pages.yml` / `daily.yml` 发布 |
| `data/drive_state.json` | `to_drive.py`（Actions 回写） | `to_drive.py` |
| `data/mail_state.json` | `mailer.py`（Actions 回写） | `mailer.py` |

---

## 每个 Action 的代码实现

### 1. deploy-scf.yml → `cloud/`

| 文件 | 实现要点 |
|---|---|
| `cloud/build.sh` | **manylinux 交叉打包**：SCF 运行环境是 Python 3.10，Actions 是 3.12，必须 `pip install --platform manylinux2014_x86_64 --python-version 3.10 --only-binary=:all:`，否则装入 cp312 的 C 扩展，函数 import 直接失败 |
| `cloud/deploy_tc.py` | 纯标准库，**手写 TC3-HMAC-SHA256 签名**（不装 SDK）。token 决策树：`--token` > 环境变量 `SCRAPE_TOKEN` > 函数里现有值 > 新生成。**`wait_active()`**：`UpdateFunctionCode` 后函数进入 Updating 态，立刻改配置会被 `FailedOperation` 拒，必须轮询 `GetFunction` 等回 Active。`CreateTrigger` 重试 5 次兼容「已存在」，最后 `ListTriggers` 从 `TriggerDesc.NetConfig` 里挖函数 URL |
| `cloud/main.py` | 采集端点本体，见 daily.yml 的实现说明 |
| `cloud/test_wait_active.py` | Updating→Active 等待路径的 mock 单测（三场景） |

### 2. daily.yml → `monitor.py` / `cloud/main.py` / `tools/fetch_remote.py` / `tools/render.py`

| 文件 | 实现要点 |
|---|---|
| `monitor.py` | 采集器，**纯 HTTP 不用浏览器**：栏目列表虽是 JS 渲染，但底层有返回 JSON 的接口（`page/build/unit`，个别栏目 `tagId=list` 需回退）。解析公告详情页，抽出项目表格和 `项目[].附件链接` |
| `cloud/main.py` | 云函数端跑同一套抓取解析，**顺带把新附件的网关地址 302/301 解析成 OSS 直链**（`resolve: true` / `max_resolve: 40`，串行 + 0.4s 间隔防网关限流）。函数**不存状态**：已处理公告 id 名单（`seen`）随请求带来带去，纯函数、免数据库 |
| `tools/fetch_remote.py` | 把 `seen` POST 给云函数、拿回新公告合并进 `items.json`（幂等）。`--resolve-missing` 每天重试漏网直链，分三类：缺直链 / 解析失败 / **已失效直链**（从 `drive_state.json` 的 failed 记录反查网关地址重解析） |
| `tools/render.py` | 渲染**自包含单文件**看板（数据内联，Pages 直接发） |

### 3. pages.yml → 官方 actions + 保险丝

无自有代码。`actions/configure-pages` → `upload-pages-artifact` → `deploy-pages`。
唯一逻辑是保险丝：解析 `reports/index.html` 里 `"unit":` 的出现次数，0 条拒绝发布。

### 4. drive.yml → `tools/to_drive.py`

按「项目名称」把 CSV 行分组，逐项目上传。三个上传通道：

- `.pdf` → `rclone copyurl` 流式上传（OSS 直链 → Drive，不落本地磁盘）
- `.zip` → Python `zipfile` 解压。**政务包是 Windows 打包的，内文件名是 GBK 且没打
  UTF-8 标志位**，Python 默认按 cp437 解出乱码——按 `flag_bits` 判断，没有 `0x800`
  就 `cp437→gbk` 还原（已用真实附件实测）
- `.rar` / `.7z` → `bsdtar`（libarchive）解压，中文文件名处理正确
- 文本文件 `网页内容.txt` → `rclone rcat`（stdin 直写 Drive，不落临时文件）

配套机制：

- **`probe_drive` 探活**：`rclone about` + `mkdir` + 真传一个最小附件。
  访问不通（404）或没有存储配额（SA 的 403）10 秒内带排查清单退出，
  不让上百个文件挨个重演
- **断点续跑**：`data/drive_state.json` 按目标文件记账，键分四种
  （`text|` / `url|` / `file|` / `arc|`），每传完一个就落盘；压缩包的解出文件
  全部传完才标记 `arc|` 任务完成，下次不再重复下载
- 压缩包下载后即删，runner 磁盘峰值 ≈ 最大附件（137.5MB）× 2

### 5. mail.yml → `tools/mailer.py`

按「项目」逐封发信（一条公告含多个项目就拆多封）。两种模式：

- `--link-only`（默认，Actions 必带）：查 `oss_links.csv` 给网关直链 + OSS 直链，
  不下载附件——境外下不动政务网网关
- `--attach`（仅境内手动机）：把小附件（≤ 30MB）下载打包成 zip 随信

去重靠 `data/mail_state.json`。SMTP 凭证全走 Secrets，仓库只留 `.env.example`。

---

## 本地手动跑法（境内机器）

```bash
python3 monitor.py                        # 直接采集（会建历史基线）
./tools/publish.sh                        # 一键：采集 → 渲染 → 提交 → 推送 → Pages
./tools/publish.sh --no-push              # 只采集不推送
python3 tools/download.py --dry-run       # 看附件清单和体积
python3 tools/download.py --resolve       # 解析全部附件的 OSS 直链 → oss_links.csv
python3 tools/to_drive.py --dry-run       # 看 Drive 同步计划
python3 tools/to_drive.py --project 化工   # 只同步单个项目
python3 tools/mailer.py --dry-run         # 看待发邮件清单
```

## 目录结构

```
monitor.py            采集器（纯 HTTP）
cloud/                境内云函数采集端点 + SCF 自动部署脚本
tools/publish.sh      一键采集 + 推送
tools/fetch_remote.py 调云函数取新公告并合并
tools/render.py       渲染看板
tools/download.py     附件下载 + OSS 直链解析
tools/mailer.py       按项目发邮件
tools/to_drive.py     附件同步 Google Drive（按项目扁平一层）
data/ reports/        已入库：采集结果与看板（Pages 发布源）
.github/workflows/    5 个 Action，见上方目录
docs/                 01 会话全记录 / 02 技术决策 / 03 数据源清单 / 04 附件下载 / 05 邮件推送
```

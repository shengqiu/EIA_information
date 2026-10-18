# WorkBuddy Project · 环评审批数据导入

> 给未来的会话看：先读这个文件，再决定动哪里的代码。
> 本目录（`/workspace`）已被设为 **WorkBuddy 项目工作区根目录**——新建任务时在输入框左下角「选择工作空间」选这个目录，就能接着这个项目继续。

## 这个项目在干什么

自动盯 **湖州市生态环境局**（`hbj.huzhou.gov.cn`）的环评公示栏目，把 **16 个栏目**（市局 + 吴兴/南太湖新区/南浔/德清/长兴/安吉/长合分局 × 非辐环评审批、辐射项目审批，外加全市建设项目环评信息公示）的新公告抓下来，提取出**项目表格**（项目名称、建设地点、建设单位、环评机构、受理日期）+ **附件链接**（环评报告书 / 报告表 / 公参说明），存起来并出成一个可搜索的 HTML 看板，同时支持把附件本体下载到本地。

用户原始需求：**工作日每天早上**看一次 **湖州全市、全类型**的环评公示，产出 **HTML 看板**；下载页面里的附件是「非常重要」的一环。**必须免费**。

## 当前状态（2026-09-30 凌晨 · 全自动已打通）

| 项 | 值 |
|---|---|
| 工作空间根目录 | `/workspace`（**本目录本身就是项目根目录**，不是子目录） |
| GitHub | https://github.com/shengqiu/EIA_information（**public**，Pages 要求免费计划必须公开） |
| 线上看板 | **https://shengqiu.github.io/EIA_information/** ✅ 249 条 |
| 采集结果 | 249 条公告 / 166 个项目，共 87 个附件 ≈ 1 GB |
| **境内云函数** | ✅ **已上线**：腾讯云 SCF `huzhou-epi-scrape`（ap-shanghai，函数 URL），16 栏目 2.8–5 秒跑完 |
| 每日 09:00 | ✅ `daily.yml` 全绿实测：境外 runner → 调境内云函数 → 渲染 → 发布 |
| SCF 部署 | ✅ **全自动**：`cloud/**` 改动推 main → `deploy-scf.yml` 打包+更新函数+冒烟测试（2026-10-16 实测通过，修掉了 Updating 态竞态） |
| Drive 同步 | ✅ `drive.yml`：按项目扁平一层（`湖州线索/<项目>/网页内容.txt + 解压后附件`），OAuth 模式，改脚本即自动跑 |
| 共享层 | ✅ `tools/eia_common.py`（2026-10-18）：数据加载/正文渲染/解压/文件名还原抽成共享模块，`to_drive.py` 改用，`--dry-run` 回归输出逐字节一致 |
| Confluence 迁移 | ❌ **已放弃（2026-10-18）**：评估后实现过半，因 Free 计划仅 2GB 存储（附件月增 ≈442MB，只能撑 ~5 个月）而放弃；附件存储继续走 Google Drive。决策记录见待办区 |

## ⚠️ 头号约束：境外 IP 访问不了 hbj.huzhou.gov.cn

这是整个架构的决定性事实，**别再试图把采集搬到境外**：

| 环境 | 结果 |
|---|---|
| 本地 / 本沙箱 / 境内云函数（国内） | ✅ 16 栏目 3~5 秒跑完，249 条 |
| GitHub Actions（美国机房）→ `hbj.huzhou.gov.cn` | ❌ 16 栏目全部超时，**504 秒拿到 0 条**，还把线上看板刷成了空数组 |
| GitHub Actions → **政务网 OSS 直链** | ✅ **能下载**（实测 `206`，1MB 用时 3–4 秒，约 250–324 KB/s） |

**关键区分：被挡的是 `hbj.huzhou.gov.cn` 这个域名，不是文件本身。**
附件真实存放在 `zjjcmspublicnew.oss-cn-hangzhou-zwynet-d01-a.internet.cloud.zj.gov.cn`（浙江政务云 OSS），
这个域名境外可达，且直链不带签名参数。实测（Actions 出口 IP `4.246.86.197`）：

```
① OSS 直链（小文件）  code=206 下载=1048576B 耗时=4.18s 速度=250947B/s
② OSS 直链（137MB）   code=206 下载=1048576B 耗时=3.23s 速度=324176B/s
③ 网关 hbj.huzhou.gov.cn  curl(28) 超时 30s   ← 只有这个被挡
④ example.com         code=200 耗时=0.04s     ← 出口本身正常
```

推论：**只要手里有 OSS 直链，境外就能下附件**。直链靠跟随网关的 302/301 拿到（必须境内），
已落地为 `data/oss_links.csv` 映射入库（境内 `download.py --resolve` 更新，注意 `--workers 1`
防网关限流），mailer 的 link 模式查表后邮件里同时给网关链接 + OSS 直链。

所以现在的分工是：**采集在国内跑 → 把结果推到 GitHub → GitHub Pages 只负责发布**。
`pages.yml` 里有一道保险丝：看板数据为 0 条时**拒绝发布**，防止再次被空数据覆盖。

## 两条运行路线

**自动路线（定时在 GitHub，执行在境内）**
`.github/workflows/daily.yml` 每天 **UTC 01:00 = 北京 09:00** 触发，两条子路线：

1. **云函数（✅ 已上线，当前在跑这条）**：secret `SCRAPE_URL` + `SCRAPE_TOKEN` 已配好 →
   `tools/fetch_remote.py` 把 `seen` 名单 POST 给境内云函数，拿回新公告合并。
   云函数不存状态，见 `cloud/README.md`（腾讯云 SCF，费用约 0.05 元/月）。
   函数地址与 token 只写在 GitHub Secrets 里，**不要写进仓库**。
2. **自托管 runner**：设变量 `RUNNER_LABEL=self-hosted` + `SCRAPE_MODE=local` →
   在国内机器上直接跑 `monitor.py`。没配就跑在境外托管 runner 上，抓不到，
   但保险丝会拦住（0 条不提交不发布，已实测线上 229 条毫发无损）。

**国内采集 + 发布（主力，无需任何 runner）**
```bash
./tools/publish.sh               # 采集 → 生成看板 → 提交 → 推送 → Pages 自动发布
./tools/publish.sh --no-push     # 只采集不推送
./tools/publish.sh --force       # 无新增也提交
./tools/publish.sh --mail        # 顺便按项目发邮件（需 SMTP_* 环境变量）
```

**单独跑采集 / 下附件**
```bash
python3 monitor.py                 # ~30 秒，写 data/items.json + data/state.json + reports/index.html
python3 tools/download.py --dry-run          # 看看有哪些附件、多大
python3 tools/download.py --unit 南浔分局     # 按单位下载
python3 tools/download.py --resolve          # 解析全部附件的 OSS 直链 → files/ + data/oss_links.csv
```

## 目录

```
monitor.py           采集器，纯 HTTP（不用浏览器）
cloud/               境内云函数采集端点（腾讯云 SCF / 阿里云 FC）+ 打包脚本
tools/publish.sh     一键采集 + 推送（国内跑）
tools/fetch_remote.py  调云函数取新公告并合并进 data/items.json
tools/render.py      只渲染看板不采集（境外 runner 用）
tools/download.py    附件下载 + OSS 直链解析
tools/mailer.py      按「项目」逐封发邮件 + 附件打包（需 SMTP_* 环境变量）
tools/to_drive.py    附件同步 Google Drive：湖州线索/<项目>/网页内容.txt + 解压后附件（跑在 Actions）
tools/eia_common.py  同步工具共享层：数据加载、正文渲染、解压、中文文件名还原（to_drive.py 在用）
.github/workflows/pages.yml   只发布 reports/ 到 Pages（不采集）
.github/workflows/drive.yml   附件每日同步 Google Drive，按项目扁平一层（10:30 CST + 手动触发）
.github/workflows/deploy-scf.yml   cloud/ 或 monitor.py 改动推 main → 自动打包+部署 SCF+冒烟测试
data/  reports/      ← 已入库：Pages 的发布源就是仓库里的看板
files/               ← gitignore（下载的附件本体）
```

## 读过再动手：三个不能破的约束

1. **采集只能在境内跑；发邮件已上 Actions（仅链接模式）**。`monitor.py` 绝不能塞回 GitHub Actions——境外下不动 `hbj.huzhou.gov.cn`，采集必失败；采集的全自动靠 **境内云函数**（已上线，`daily.yml` 每天 09:00 调它）。`tools/mailer.py` 现在跑在 Actions 上（`.github/workflows/mail.yml`，每 2 小时 1 封），但**必须带 `--link-only`**：境外解析不了附件直链、也下不动网关，带附件项目一律正文发**政务网网关直链**（境内可打开），**绝不下载附件本体**。SMTP 凭证全走 GitHub Secrets（`SMTP_*` / `MAIL_*`），仓库里只有 `.env.example`。要发**真正带附件**的邮件，仍在境内跑（本沙箱/用户机器，`./tools/publish.sh --mail` 或定时任务），或等云函数返回 OSS 直链后改 mailer 从 OSS 下载（境外实测可下）。
2. **`fileUrl` 是服务端加密的**。下载链接长这样 `download?fileUrl=<密文>&fileName=<文件名>.zip`，base64 解开是 128 字节密文，**算不出真实路径**，只能跟随 302 → 301 重定向到 OSS 直链。
3. **附件总规模约 1 GB**，单个最大 137.5 MB。任何 Serverless 环境都碰不得（单个文件就超过多数云函数的内存/响应体上限），本体下载永远交给本地 `tools/download.py`。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/01-会话全记录.md`](docs/01-会话全记录.md) | 整个对话的演进过程：需求、反复、踩坑、结论 |
| [`docs/02-技术决策.md`](docs/02-技术决策.md) | 为什么不用浏览器、为什么拆单元任务、KV 为什么按月分片 |
| [`docs/03-数据源清单.md`](docs/03-数据源清单.md) | 16 个栏目的 colId、隐藏 JSON 接口、TagId 坑 |
| [`docs/04-附件下载.md`](docs/04-附件下载.md) | 81 个附件的统计、OSS 直链机制、命令行用法 |
| [`docs/05-邮件推送.md`](docs/05-邮件推送.md) | 按项目发邮件：SMTP 配置、三种模式、去重、COS 备份 |

## 待办 / 下一步

- [x] GitHub Pages 上线并恢复 229 条数据
- [x] 每日 09:00 CST 定时（`daily.yml`），境外 runner 已验证会安全 fail-fast
- [x] **部署境内云函数**（腾讯云 SCF `huzhou-epi-scrape` / ap-shanghai / 函数 URL）
      + 配好 `SCRAPE_URL`/`SCRAPE_TOKEN` 两个 secret，`daily.yml` 全链路实测通过
- [ ] **删除腾讯云子用户 `workbuddy` 并吊销其 SecretId/SecretKey**（部署已完成，密钥不必留）
- [x] 备选：装 self-hosted runner（境内机器）+ 设 `RUNNER_LABEL=self-hosted`
      + `SCRAPE_MODE=local` —— 云函数通了，这条不必做了
- [x] **按项目发邮件（链接模式）**：`tools/mailer.py` + `.github/workflows/mail.yml`。
      **定时任务已停用**（2026-10-15）——附件改走 Drive 后邮件不再是必经路径，
      只在手动 Run workflow 时补发/看历史项目。恢复定时：把 mail.yml 里注释掉的两行加回来。
      `--link-only` 必带（境外下载附件），SMTP 凭证在 GitHub Secrets，
      `data/mail_state.json` 跨运行回写续传。端到端实测通过（已发 5 封）
- [x] **邮件附件区带 OSS 直链**：`data/oss_links.csv`（`网关URL → OSS直链` 映射，81/87 覆盖）
      已入库，mailer link 模式纯查表填充，Actions 上也能给出境外可下的直链。
      更新方式：**全自动**——每日 `daily.yml` 调境内云函数时顺带把新附件的网关地址
      解析成直链（`resolve`/`max_resolve` 控制），`fetch_remote.py` 合并进 CSV；
      失败条目由 `--resolve-missing` 每天重试。手动重跑应急：
      `python3 tools/download.py --resolve --workers 1`（并发高会触发网关限流）
- [ ] **发真正带附件的邮件**：仍需境内执行（`--attach`，本沙箱/用户机器）。
      理论上也可让 mailer 从 OSS 直链下载后 attach（境外实测可下），未实现
- [ ] 补齐 `data/oss_links.csv` 剩余 6 个未覆盖附件（网关链接过期或解析失败）
- [ ] 决定 Cloudflare Worker 的去留（现在是个只能读 KV 的空壳）
- [x] `data/oss_links.csv` 已纳入仓库（81 条 OSS 直链映射，mailer 直接用；
      配合 `aria2` 可按直链整包拉附件）
- [x] ~~换 fine-grained token~~（2026-10-16 关闭）：原待办针对的是**旧沙箱工作区**的
      git remote——当时 `/workspace` clone 过本仓库，classic PAT 明文写在 remote URL 里。
      **用户本机从未 clone 过本仓库，无需任何操作**；且旧 classic PAT 已于当日吊销重建，
      remote 里残留的明文随即失效，风险清零。若将来确需在境内机器手动跑
      `publish.sh`：clone 后执行
      `git config --global credential.helper 'cache --timeout=28800'`
      托管凭据（token 只在内存，不落盘），**严禁把 token 写进 remote URL**；
      新 token 用 fine-grained PAT，Only select repositories 只勾本仓库，
      权限 `Contents: Read and write` 即可（如需 API 触发 workflow 再加 `Actions: write`）。
- [x] **附件同步 Google Drive 已上 Actions**：`tools/to_drive.py` + `.github/workflows/drive.yml`
      （每日 10:30 CST + 手动触发）。OSS 直链境外可达，`data/drive_state.json` 断点续跑。
      凭证只走 Secrets（`GDRIVE_TOKEN` / `DRIVE_FOLDER_ID`，OAuth 模式；SA 模式兼容但无配额）
      - [x] **卡在分享配置上（2026-10-15 首跑 101 个全挂）**：症状是全部文件
            `failed to make directory: 404 File not found`。404（不是 401）说明 SA 密钥
            没问题，是共享文件夹对它不可见。代码侧已加
            `probe_drive` 探活：访问不通就 10 秒内带排查清单退出，不再空跑 10 分钟；
            回写步骤也改成先 `pull --rebase` 再推（首跑因并发提交被拒丢过状态）。
            之后又撞第二堵墙：`Service Accounts do not have storage
            quota` 403——SA 配额为 0，只能传共享云端硬盘。已切
            OAuth 用户授权（`GDRIVE_TOKEN` secret，workflow 自动切模式），
            详见 docs/04。run #3 以 OAuth 模式传了 6 个后被手动取消，state 已记账
- [x] **直链解析自动化（不用再手动 resolve）**：`cloud/main.py` 在采集时顺带把新附件的
      网关地址 302/301 解析成 OSS 直链（`resolve: true` / `max_resolve: 40`，串行+0.4s 间隔防限流），
      随 `links[]` 返回；新增 `POST /resolve` 端点给 Actions 重试失败条目。
      `tools/fetch_remote.py` 按「网关地址」合并进 `data/oss_links.csv`（幂等，已测重跑 0 变更）。
      **部署已全自动**：`cloud/**` 改动推 main 触发 `.github/workflows/deploy-scf.yml`
      （打包 manylinux py3.10 依赖 → 调腾讯云 API 更新函数 → GET /health 冒烟测试），
      无需再去控制台传 zip。需要 `TENCENTCLOUD_SECRET_ID` / `TENCENTCLOUD_SECRET_KEY`
      两个 secret（建议只有 SCF 权限的子用户）
- [x] **附件直达 Google Drive，不再经过邮件**：`daily.yml`（含文件解析）→ `workflow_run`
      触发 `drive.yml` → `to_drive.py` 沿 OSS 直链上传。附件本体一条邮件都不用发
- [x] **Drive 目录改为按项目扁平一层（2026-10-16）**：`湖州线索/<项目名称>/` 下第一个
      文件是 `网页内容.txt`（公告标题/类型/日期/链接/项目明细/正文，取自 items.json），
      其余是**解压后**的附件（.pdf 直接 copyurl；.zip 走 Python zipfile，GBK 内名
      cp437→gbk 还原；.rar/.7z 走 bsdtar）。不再按分局组织、不再原样传压缩包。
      旧「环评附件/」目录需在 Drive 网页手动删；state 换 `text|/url|/file|/arc|` 键，
      旧记录自动隔离、全量重传
- [x] ~~附件存储搬到 Confluence（每项目一页 + 页面附件）~~（2026-10-18 放弃）：
      用户想替换掉 Google Drive。实现完了共享层 `tools/eia_common.py` 和
      `tools/to_confluence.py`（REST 客户端、find-or-create 页面、正文指纹跳过、
      附件 multipart 上传、断点续跑、增量种子），本地用假 Confluence 服务器
      + 真实 OSS 附件跑通全链路（含 409 重试、state 丢失恢复、死链记账）。
      **放弃原因：Confluence Cloud Free 只有 2GB 存储**，而附件月增 ≈442MB
      （2026-08/09/10 均值），只能撑 ~5 个月；升级 Standard 要 $7.15/用户/月，
      违背项目「必须免费」的初衷。附件存储继续走 Google Drive（15GB 免费）。
      代码保留在本地未提交；`eia_common.py` 共享层因回归无损被保留。
      若将来存储诉求再变（如自建 Confluence/其他 Wiki），to_confluence.py
      的 REST 打法可直接复用
- [x] **`to_drive.py` 改用共享层 `eia_common.py`（2026-10-18）**：数据加载、
      正文渲染、解压、中文文件名还原、大小解析全部抽到 `tools/eia_common.py`，
      `to_drive.py` 只留 Drive 特有的 rclone 封装/探活/state/主流程。
      `--dry-run` 输出与重构前逐字节一致（仅时间戳不同）；解压链路用真实附件
      （金洁 zip / 安吉嘉好 rar-in-zip / 海杰 rar）在 Confluence 集成测试中
      连带验证过。drive.yml 的 push paths 已加 `tools/eia_common.py`

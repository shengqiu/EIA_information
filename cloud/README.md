# 境内云函数采集端点

> **状态：已上线（2026-09-30）**，腾讯云 SCF · 上海 · 函数 URL。
> 实测：16 栏目扫描 184 条 / 2.8–5.1 秒 / 0 失败；`daily.yml` 每天 09:00 全链路通过。
> 地址和 token **只存在 GitHub Secrets**（`SCRAPE_URL` / `SCRAPE_TOKEN`）里，别写进仓库。
> **部署全自动**：`cloud/` 或 `monitor.py` 有改动推上 main → `.github/workflows/deploy-scf.yml`
> 自动打包（manylinux py3.10 依赖）+ 调腾讯云 API 更新函数 + GET /health 冒烟测试，
> 不用再去控制台传 zip。也可在 Actions 页手动 Run workflow。

GitHub Actions 的托管 runner 在境外，访问不了 `hbj.huzhou.gov.cn`。
把抓取放进**中国大陆的云函数**里，境外只负责调度和发布：

```
GitHub Actions（每天 09:00，境外也无所谓）
   │  POST {seen: {...已处理的公告id...}}
   ▼
腾讯云 SCF / 阿里云 FC（境内）
   │  抓 16 个栏目 → 解析新公告详情
   │  返回 {new: [...], seen: {...更新后...}}
   ▼
Actions 合并进 data/items.json → 渲染看板 → 提交 → Pages 发布
```

## 为什么云函数不存状态

`seen`（已处理公告 id 名单，当前 13 KB）由调用方带过来、带回去。
所以函数随便冷启动、随便扩缩容，**不用接 COS/OSS/数据库**，一个纯函数就够。

只返回「本次新解析的公告」，日常增量只有几十 KB。

## 1. 打包

```bash
./cloud/build.sh          # → cloud/dist/huzhou-scrape.zip（约 3 MB）
```

脚本会自动处理依赖平台问题（Mac/Windows 上打包 Linux 二进制）。

## 先说钱：不是永久免费 ⚠️

腾讯云 SCF 的免费额度是**新用户前三个月**（含开通当月），
之后转按量计费。这不是我猜的，官方文档原话：

> 开通使用云函数三个月后的用户每月不再享受免费试用额度，采用按量计费模式。

第四个月起的实际开销（按本项目的用量算）：

| 计费项 | 单价 | 本项目月用量 | 月费用 |
|---|---|---|---|
| 资源使用量 | 0.00011108 元/GBs | 512MB × 30s × 30 次 ≈ 450 GBs | **0.050 元** |
| 调用次数 | 0.0133 元/万次 | 0.003 万次 | 0.00004 元 |
| 外网出流量 | 0.80 元/GB | ≈ 0.5 MB | 0.0004 元 |

**合计约 0.05 元/月，一年 6 毛钱。**

阿里云 FC 现在是 CU 计费，且有「每小时不足 0.01 元按 0.01 元计」的兜底规则，
按本项目用量大约 **0.3 元/月**——比腾讯云贵 6 倍，就因为这个最低计费。

所以：**前三个月 0 元，之后每月 5 分钱**。如果你要的是严格意义的永久 0 元，
那正解是定时任务在 WorkBuddy 沙箱里跑 `./tools/publish.sh`（沙箱在国内能访问政务站）。

## 2. 腾讯云 SCF —— 逐步操作

### 2.1 创建函数

<https://console.cloud.tencent.com/scf/list> → **新建** → **自定义创建**

| 字段 | 填什么 |
|---|---|
| 函数类型 | **事件函数**（不要选 Web 函数） |
| 函数名称 | `huzhou-epi-scrape` |
| 地域 | 国内任选，**上海 / 广州** 到湖州延迟低 |
| 运行环境 | **Python 3.10** |
| 创建方式 | 本地上传 zip 包 |
| 执行方法 | **`main.main_handler`** ← 两段式「文件名.函数名」，文件是 `main.py` |

> ⚠️ 执行方法是最容易填错的地方。腾讯云文档原话：
> "使用 Python 开发语言时，执行方法类似 `index.main_handler`，此处 index 表示
> 执行的入口文件为 index.py"。我们入口文件叫 `main.py`，所以填 `main.main_handler`。

### 2.2 函数配置

- **内存**：512 MB
- **执行超时**：**300 秒**（默认 3 秒，必超时）
- **公网访问**：保持「启用」（要访问政务站）

### 2.3 环境变量

函数配置 → 环境变量 → 新增：

| Key | Value |
|---|---|
| `SCRAPE_TOKEN` | 一个长随机串，比如 `openssl rand -hex 24` 的输出 |

### 2.3.1 ⚠️ 踩过的坑：CLS / 子账号权限

第一次用子账号部署时函数直接 `CreateFailed`，报：

```
OperationDenied.AccountNotExists — account is abnormal（CLS service is unregistered）
```

别被文案骗了，不一定是主账号没开 CLS。SCF 强制把运行日志投递到 CLS，
建函数时后台会用**你这个密钥的身份**去建日志集。用子账号实测：

```
cls:DescribeLogsets  → 200 OK（空列表）
cls:CreateLogset     → AuthFailure.UnauthorizedOperation（logset/* has no permission）
```

查得到、建不了 → SCF 把它包装成了那句 "CLS service is unregistered"。
**解决办法**：给子账号（或自己用的密钥）挂 `QcloudCLSFullAccess`；
如果控制台提示 CLS 未开通，先去 https://console.cloud.tencent.com/cls 点「立即开通」。

另外一句：`CreateFailed` 的函数是颗死子（CodeSize=0，建不了触发器），
`deploy_tc.py` 会**自动先删再重建**，不用手动清理。

### 2.4 开「函数 URL」对外暴露

腾讯云 **API 网关产品已于 2025-06-30 停服，API 网关触发器同步下线**
（2024-07-01 起就不再支持新建）。官方指定的替代是 **函数 URL**，
而且对我们更合适：

| | API 网关（已下线） | 函数 URL |
|---|---|---|
| 费用 | 调用费 + 流量费 | **不收费** |
| 链路 | 经过网关，有独立超时 | **纯透传，没有那一层超时** |
| 响应格式 | 集成响应 | **兼容 apigw 响应，无需改造** |

函数详情页 → 左侧 **函数 URL** → **新建函数 URL**：

| 配置项 | 选什么 |
|---|---|
| 别名/版本 | `$LATEST`（URL 跟版本一对一绑定） |
| 公网访问 | 开启 |
| 授权类型 | **开放**（我们在函数里自己校验 token；选 CAM 鉴权的话调用方要做签名） |
| CORS | 随便，用不到 |

建好后得到 URL，形如：

```
https://1251234567-abcdefgh.ap-shanghai.tencentscf.com
```

> ⚠️ 函数 URL **默认是关闭的**，必须手动建。而且它跟版本/别名一对一绑定，
> 以后发新版本要记得给新版本再开一次，或者用别名。

### 2.5 event 结构变了，代码已适配

函数 URL 给事件函数的 event **兼容 apigw 协议，但去掉了几个字段**：

```jsonc
{
  "body": "{\"test\":\"hello\"}",
  "headers": { "content-type": "application/json" },
  "httpMethod": "POST",
  "path": "/",
  "queryString": { "token": "xxx" }        // ← 注意：不是 queryStringParameters
}
```

去掉了 `isBase64Encoded`、`requestContext`、`queryStringParameters`、
`pathParameters`、`headerParameters`。

`cloud/main.py` 已经同时兼容两种结构（函数 URL 的 `queryString` 和
老 API 网关的 `queryStringParameters`），body 先按 JSON 解析、
失败再试 base64，所以两种都能跑。已本地验证：

```
函数URL POST → 200 ok | scanned 16
函数URL GET  → 200          （探活）
无 token     → 403
旧 apigw 结构 → 200          （向后兼容）
```

### 2.6 测试

```bash
# 本地 mock 状态机单测（可直接独立运行）
python3 cloud/test_wait_active.py

# 探活
curl 'https://1251234567-abcdefgh.ap-shanghai.tencentscf.com?token=你的TOKEN'
# → {"ok": true, "service": "huzhou-epi-scrape", "columns": 16, "auth": true}

# 真跑一次（不写文件）
SCRAPE_URL='https://1251234567-abcdefgh.ap-shanghai.tencentscf.com' \
SCRAPE_TOKEN='你的TOKEN' \
python3 tools/fetch_remote.py --dry-run
```

看到 `栏目 16 成功 0 失败` 就成了。

## 3. 阿里云函数计算

1. 控制台 → **创建函数 → HTTP 函数**，运行环境 **Python 3.10**
2. 上传 zip，入口 `handler`（本仓库已提供 WSGI 适配）
3. 执行超时 **300 秒**、内存 **512 MB**
4. 环境变量 `SCRAPE_TOKEN`
5. 触发器 → HTTP 触发器，记下公网 URL

## 4. 配到 GitHub

Settings → Secrets and variables → Actions → **New repository secret**：

| Secret | 值 | 用途 |
|---|---|---|
| `SCRAPE_URL` | 函数 URL | daily.yml 采集、部署后冒烟测试 |
| `SCRAPE_TOKEN` | 函数口令 | daily.yml 鉴权；部署时对齐函数里的 token |
| `TENCENTCLOUD_SECRET_ID` | 腾讯云 API SecretId | deploy-scf.yml 部署用 |
| `TENCENTCLOUD_SECRET_KEY` | 腾讯云 API SecretKey | 同上 |

> 腾讯云那两个建议建一个**只有 SCF 权限的子用户**（别用主账号密钥），
> 权限给 `QcloudSCFFullAccess` 就够。

之后 `daily.yml` 每天 09:00 会自动走云函数路线。
没配 `SCRAPE_URL` 时该步骤自动跳过（不会失败）。
改代码推 main 即自动部署；手动部署：Actions → 「部署采集云函数」→ Run workflow。

## 5. 先在本地验一遍

部署前用内置服务器模拟一遍，不花钱：

```bash
# 起服务
SCRAPE_TOKEN=test python3 - <<'PY'
import sys, os
sys.path.insert(0, '/workspace'); sys.path.insert(0, '/workspace/cloud')
from flask import Flask, request
from main import main_handler
app = Flask(__name__)
@app.route('/scrape', methods=['GET','POST'])
def s():
    r = main_handler({'body': request.get_data(as_text=True),
                      'httpMethod': request.method,
                      'headers': dict(request.headers),
                      'queryStringParameters': request.args.to_dict()}, None)
    return r['body'], r['statusCode'], {'Content-Type': 'application/json'}
app.run(port=8899, threaded=True)
PY

# 另开一个终端
curl 'http://127.0.0.1:8899/scrape?token=test'      # 探活
SCRAPE_URL=http://127.0.0.1:8899/scrape SCRAPE_TOKEN=test \
  python3 tools/fetch_remote.py --dry-run           # 看有没有新公告
```

部署后把 URL 换成真地址再跑一次同样的命令即可。

## 用量 vs 额度

腾讯云前三个月的免费额度（每月重置）：

| 项 | 免费额度 | 本项目月用量 |
|---|---|---|
| 调用次数 | 100 万次 | 30 次 |
| 资源使用量 | 100 万 GBs | ~450 GBs |
| 外网出流量 | 2 GB | ~0.5 MB |

用量是免费额度的零头。额度每月 1 号重置，不累积。

## 接口

**POST** `?token=<SCRAPE_TOKEN>` → 采集 +（默认）顺带解析新附件的 OSS 直链

```jsonc
// 请求
{"seen": {"<art_id>": "2026-09-29 17:19"}, "limit": 12, "max_new": 60,
 "resolve": true,      // 是否解析新公告附件的 OSS 直链，默认 true
 "max_resolve": 40}    // 单次最多解析几个附件（串行，防超时）

// 响应
{"ok": true,
 "new": [ /* 本次新解析的公告对象 */ ],
 "links": [           // 附件直链，CSV 行格式，可直接合并进 data/oss_links.csv
   {"公告日期": "2026-10-15", "发布单位": "南浔分局", "项目名称": "...",
    "建设单位": "...", "文件名": "...全本.zip", "大小": "32.1MB",
    "OSS直链": "https://zjjcmspublicnew.oss-cn-hangzhou-zwynet-d01-a.../xxx.zip?fileName=...",
    "网关地址": "https://hbj.huzhou.gov.cn/api-gateway/...download?fileUrl=..."}
 ],
 "attachments": 12,     // 本次新公告带了多少个附件
 "resolved": 11,        // 其中解析出真直链几个
 "seen": { /* 更新后的 id 名单，原样存回 data/state.json */ },
 "scanned": 192, "parsed": 3, "failed": 0,
 "columns_ok": 16, "columns_failed": 0,
 "elapsed_sec": 21.4}
```

**GET** → 探活，返回 `{"ok":true,"columns":16,"auth":true}`

**POST** `/resolve` → 单独批量解析网关地址，用来重试之前失败的条目

```jsonc
// 请求
{"urls": ["https://hbj.huzhou.gov.cn/api-gateway/...download?fileUrl=...", "..."]}

// 响应：只返回解析成功的；解析不了的不返回
{"ok": true,
 "links": [{"网关地址": "...", "OSS直链": "...", "大小": "32.1MB", "文件名": "..."}],
 "done": 3, "failed": 1, "elapsed_sec": 4.2}
```

### 为什么直链解析必须放在云函数

网关 `download?fileUrl=<密文>` **只有境内 IP 才肯** 302 → 301 跳到浙江政务云 OSS；
而 OSS 直链本身无签名、境外可达。所以分工是：**境内解析（云函数），境外使用（Actions）**。
解析走 Range `bytes=0-0`，只要 1 字节头信息，不下内容；串行 + 间隔 0.4 秒，
否则网关会限流（本地实测并发 6 → 成功率掉到 10%，串行 → 70%+）。

有些附件的网关链指向 `cms.zjzwfw.gov.cn`（浙江政务外网专用域名，公共 DNS 返回
10.x 私网地址），公网无解 —— 这类 `OSS直链` 会留空，下游脚本自动跳过。

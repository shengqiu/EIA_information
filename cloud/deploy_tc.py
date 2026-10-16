# -*- coding: utf-8 -*-
"""
一条命令把采集函数部署到腾讯云 SCF（含开函数 URL）。

只依赖标准库，手写 TC3 签名，不用装 SDK。

用法：
    export TENCENTCLOUD_SECRET_ID=AKIDxxxx
    export TENCENTCLOUD_SECRET_KEY=xxxx
    python3 cloud/deploy_tc.py \
        --region ap-shanghai \
        --token "$(openssl rand -hex 24)"

    python3 cloud/deploy_tc.py --zip cloud/dist/huzhou-scrape.zip --function huzhou-epi-scrape
"""

import os
import sys
import json
import time
import base64
import hashlib
import hmac
import argparse
import urllib.request
import urllib.error

SERVICE = "scf"
HOST = "scf.tencentcloudapi.com"
ENDPOINT = "https://" + HOST
VERSION = "2018-04-16"
ALGO = "TC3-HMAC-SHA256"
CT = "application/json; charset=utf-8"


def _sha256(b):
    return hashlib.sha256(b if isinstance(b, bytes) else b.encode("utf-8")).hexdigest()


def _hmac(key, msg):
    return hmac.new(key if isinstance(key, bytes) else key.encode("utf-8"),
                    msg.encode("utf-8"), hashlib.sha256).digest()


def call(action, payload, sid, skey, region):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    now = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(now))

    # 签名里写的 content-type 必须和实际发送的完全一致，否则 AuthFailure
    canon_headers = f"content-type:{CT}\nhost:{HOST}\n"
    signed_headers = "content-type;host"
    canon = ("POST\n/\n\n" + canon_headers + "\n" + signed_headers + "\n" + _sha256(body))

    scope = f"{date}/{SERVICE}/tc3_request"
    to_sign = f"{ALGO}\n{now}\n{scope}\n{_sha256(canon)}"

    k = _hmac(("TC3" + skey).encode("utf-8"), date)
    k = _hmac(k, SERVICE)
    k = _hmac(k, "tc3_request")
    sig = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth = (f"{ALGO} Credential={sid}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}")

    req = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={
            "Authorization": auth,
            "Content-Type": CT,
            "Host": HOST,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(now),
            "X-TC-Version": VERSION,
            "X-TC-Region": region,
        })

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"Response": {"Error": {"Code": str(e.code), "Message": raw[:500]}}}
    except Exception as e:
        return {"Response": {"Error": {"Code": "ClientError", "Message": repr(e)}}}


def unwrap(resp, what):
    r = resp.get("Response", {})
    if "Error" in r:
        err = r["Error"]
        print(f"✗ {what} 失败：{err.get('Code')} - {err.get('Message')}")
        if "CLS" in (err.get("Message") or ""):
            print()
            print("  ⚠ 这是账号没开通 CLS（日志服务）导致的，SCF 强制依赖它投递运行日志。")
            print("    去 https://console.cloud.tencent.com/cls 点「立即开通」（免费额度够用），")
            print("    开通后重跑本脚本即可，其它都不用改。")
        return None
    return r


def wait_active(function, sid, skey, region, tries=40, interval=3):
    """等函数离开 Creating/Updating 态，返回最终状态。

    UpdateFunctionCode 之后函数会进入 Updating 态，这时立刻调
    UpdateFunctionConfiguration 会被拒（FailedOperation：当前函数处于
    Updating状态）——必须等它回到 Active。2026-10-16 Actions 首跑实测踩到。
    """
    for _ in range(tries):
        r0 = call("GetFunction", {"FunctionName": function}, sid, skey, region)
        st = ((r0.get("Response") or {}).get("Status") or "")
        if st and st != "Creating" and st != "Updating":
            print(f"  状态 {st}")
            return st
        time.sleep(interval)
    print(f"  （等了 {tries * interval}s 仍未就绪，继续尝试后续操作）")
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="ap-shanghai")
    ap.add_argument("--function", default="huzhou-epi-scrape")
    ap.add_argument("--zip", default="cloud/dist/huzhou-scrape.zip")
    ap.add_argument("--token", default=None, help="SCRAPE_TOKEN，不给就自动生成")
    ap.add_argument("--memory", type=int, default=512)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--runtime", default="Python3.10")
    ap.add_argument("--url-only", action="store_true",
                    help="代码已上传过，只补建函数 URL")
    args = ap.parse_args()

    sid = os.environ.get("TENCENTCLOUD_SECRET_ID", "").strip()
    skey = os.environ.get("TENCENTCLOUD_SECRET_KEY", "").strip()
    if not sid or not skey:
        sys.exit("请先设置环境变量 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY")

    # 1. 函数是否已存在（不存在时静默，不打印 ResourceNotFound）
    r0 = call("GetFunction", {"FunctionName": args.function},
              sid, skey, args.region)
    exists = None if "Error" in r0.get("Response", {}) else r0.get("Response")

    # token 决策（顺序：--token > 环境变量 SCRAPE_TOKEN > 函数里现有的 > 新生成）。
    # 环境变量那份是 GitHub Actions 传来的 SCRAPE_TOKEN secret——daily.yml 用的
    # 就是它，两边不一致时采集会 403，所以以 secret 为准并大声告警。
    old_token = ""
    if exists:
        for kv in ((exists.get("Environment") or {}).get("Variables") or []):
            if kv.get("Key") == "SCRAPE_TOKEN":
                old_token = (kv.get("Value") or "").strip()
    if args.token:
        token = args.token
    elif os.environ.get("SCRAPE_TOKEN", "").strip():
        token = os.environ["SCRAPE_TOKEN"].strip()
        if old_token and old_token != token:
            print("⚠ 函数里的 SCRAPE_TOKEN 与 GitHub secret 不一致，已按 secret 覆盖")
            print("  （daily.yml 用的是 secret 的值，不一致时每日采集会 403）")
    elif old_token:
        token = old_token
        print("▶ 沿用函数里现有的 SCRAPE_TOKEN")
    else:
        token = __import__("secrets").token_hex(24)
        print("▶ 生成了新的 SCRAPE_TOKEN，记得同步进 GitHub Secrets")
    env = {"Variables": [{"Key": "SCRAPE_TOKEN", "Value": token}]}

    # 1b. 上次创建失败的函数是颗死子（CodeSize=0、建不了触发器），先删掉再重建
    if exists and (exists.get("Status") or "") == "CreateFailed":
        print(f"▶ 函数存在但状态 CreateFailed（多半是 CLS 未开通），删除后重建")
        unwrap(call("DeleteFunction", {"FunctionName": args.function},
                    sid, skey, args.region), "删除失败的函数")
        time.sleep(5)
        exists = None

    if not args.url_only:
        zp = args.zip if os.path.isabs(args.zip) else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.zip)
        if not os.path.exists(zp):
            sys.exit(f"找不到 {zp}，先跑 ./cloud/build.sh")
        zdata = base64.b64encode(open(zp, "rb").read()).decode()
        print(f"▶ 代码包 {zp}（{os.path.getsize(zp)//1024} KB）")
    else:
        zdata = None
        print("▶ --url-only：跳过代码上传")

    if exists and not args.url_only:
        print(f"▶ 函数已存在，更新代码和配置")
        r = unwrap(call("UpdateFunctionCode",
                        {"FunctionName": args.function, "ZipFile": zdata,
                         "Handler": "main.main_handler"},
                        sid, skey, args.region), "更新代码")
        if r is None:
            return 1
        # 代码更新后函数进入 Updating 态，立刻改配置会被拒，先等它回 Active
        print("▶ 等待代码更新完成")
        wait_active(args.function, sid, skey, args.region)
        r = unwrap(call("UpdateFunctionConfiguration",
                        {"FunctionName": args.function,
                         "MemorySize": args.memory, "Timeout": args.timeout,
                         "Environment": env},
                        sid, skey, args.region), "更新配置")
        if r is None:
            return 1
    elif exists is None:
        print(f"▶ 创建函数 {args.function}（{args.runtime} / {args.memory}MB / {args.timeout}s）")
        r = unwrap(call("CreateFunction", {
            "FunctionName": args.function,
            "Code": {"ZipFile": zdata},
            "Handler": "main.main_handler",
            "Runtime": args.runtime,
            "MemorySize": args.memory,
            "Timeout": args.timeout,
            "Type": "Event",
            "Environment": env,
            "Description": "huzhou epi scrape (境内抓取，境外调度)",
        }, sid, skey, args.region), "创建函数")
        if r is None:
            return 1

    # 2. 开函数 URL（API 网关已停服，这是官方替代）
    #    刚建好/刚更新完的函数可能是 Creating/Updating 态，这会儿建触发器
    #    会被拒，统一走 wait_active 等它回 Active
    print("▶ 等待函数状态就绪")
    wait_active(args.function, sid, skey, args.region)

    desc = {
        "AuthType": "NONE",                       # 开放，函数内部自己校验 token
        "NetConfig": {"EnableExtranet": True, "EnableIntranet": False},
        "ApiGwCompatible": True,                  # 兼容 apigw 响应格式
        "CorsConfig": {"Enable": False, "Credentials": False, "MaxAge": 0},
    }
    r = None
    for attempt in range(1, 6):
        print(f"▶ 开启函数 URL（第 {attempt} 次）")
        r = unwrap(call("CreateTrigger", {
            "FunctionName": args.function,
            "Type": "http",
            "TriggerName": "url",
            "TriggerDesc": json.dumps(desc),
            "Qualifier": "$LATEST",
        }, sid, skey, args.region), "创建函数 URL")
        if r is not None:
            break
        time.sleep(5)
    if r is None:
        print("  （可能已经建过了，继续读现有触发器）")

    # 3. 读回触发器，找 URL
    url = None
    r = unwrap(call("ListTriggers", {"FunctionName": args.function},
                    sid, skey, args.region), "读取触发器")
    if r:
        for t in r.get("Triggers", []) or []:
            if t.get("Type") == "http":
                try:
                    d = json.loads(t.get("TriggerDesc") or "{}")
                except Exception:
                    d = {}
                # 函数 URL 藏在 TriggerDesc.NetConfig 里，HTTP/HTTPS 两条
                net = d.get("NetConfig") or {}
                url = (d.get("Url") or d.get("url") or t.get("Url")
                       or net.get("ExtranetUrl") or net.get("ExtranetHTTPUrl")
                       or d.get("SubnetId") or None)
                if not url:
                    print(f"  触发器原文：{json.dumps(t, ensure_ascii=False)[:600]}")
                break

    print()
    print("=" * 62)
    print(f"函数名     {args.function}")
    print(f"地域       {args.region}")
    print(f"SCRAPE_TOKEN  {token}")
    if url:
        print(f"函数 URL   {url}")
    else:
        print("函数 URL   已创建，但 API 没返回地址；")
        print("           去控制台 函数详情 → 函数 URL 里复制一下")
    print("=" * 62)
    print()
    if args.token or os.environ.get("SCRAPE_TOKEN", "").strip():
        print("token 来自命令行/环境变量，GitHub Secrets 无需改动")
    elif not old_token:
        print("首次创建函数：把上面两项填进 GitHub 仓库的 Secrets")
        print("  SCRAPE_URL   = 函数 URL")
        print(f"  SCRAPE_TOKEN = {token}")
    else:
        print("token 沿用函数里的现值，GitHub Secrets 无需改动")
    return 0


if __name__ == "__main__":
    sys.exit(main())

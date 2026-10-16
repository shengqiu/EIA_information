# -*- coding: utf-8 -*-
"""mock 单测：UpdateFunctionCode 后函数处于 Updating 态时，
deploy_tc.py 必须等它回 Active 才调 UpdateFunctionConfiguration。"""

import sys
import os
import time
import importlib.util

_here = os.path.dirname(os.path.abspath(__file__))
os.chdir(os.path.dirname(_here))

_spec = importlib.util.spec_from_file_location(
    "deploy_tc", os.path.join(_here, "deploy_tc.py"))
dt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dt)

os.environ["TENCENTCLOUD_SECRET_ID"] = "AKIDtest"
os.environ["TENCENTCLOUD_SECRET_KEY"] = "testkey"

calls = []
state = {"status": "Active"}


def fake_call(action, payload, sid, skey, region):
    calls.append(action)
    if action == "GetFunction":
        st = state["status"]
        # 模拟状态机：Updating 两次后回 Active
        if st == "Updating":
            state["status"] = "Active"
        return {"Response": {"FunctionName": payload["FunctionName"],
                             "Status": st}}
    if action == "UpdateFunctionCode":
        state["status"] = "Updating"   # 更新代码 → 进入 Updating 态
        return {"Response": {}}
    if action == "UpdateFunctionConfiguration":
        # 关键断言：收到这个调用时函数必须是 Active
        assert state["status"] == "Active", \
            f"UpdateFunctionConfiguration 在 {state['status']} 态被调用，会被 FailedOperation 拒！"
        return {"Response": {}}
    if action == "ListTriggers":
        return {"Response": {"Triggers": []}}
    if action == "CreateTrigger":
        return {"Response": {}}
    return {"Response": {}}


dt.call = fake_call
dt.time.sleep = lambda s: None  # 别真等

sys.argv = ["deploy_tc.py", "--token", "testtoken"]
rc = dt.main()

# 验证调用顺序
seq = [a for a in calls if a != "GetFunction"]
assert seq == ["UpdateFunctionCode", "UpdateFunctionConfiguration",
               "CreateTrigger", "ListTriggers"], f"调用顺序不对: {seq}"
assert rc == 0
print("✓ Updating→Active 等待路径正确：配置更新确实发生在 Active 态")
print(f"  调用顺序: {' → '.join(seq)}")
print(f"  GetFunction 轮询次数: {calls.count('GetFunction')}")

# 场景 2：函数一开始就 Active（新建后已就绪）也应正常
calls.clear()
state["status"] = "Active"
rc = dt.main()
assert rc == 0
seq = [a for a in calls if a != "GetFunction"]
assert seq == ["UpdateFunctionCode", "UpdateFunctionConfiguration",
               "CreateTrigger", "ListTriggers"], f"调用顺序不对: {seq}"
print("✓ 直接就绪路径正常")

# 场景 3：wait_active 超时后不死等（tries 用 1 快速验证）
calls.clear()
state["status"] = "Updating"   # 永远 Updating
st = dt.wait_active("huzhou-epi-scrape", "sid", "skey", "ap-shanghai",
                    tries=1, interval=0)
assert st == "", f"超时应返回空串, got {st!r}"
print("✓ wait_active 超时降级正常（不无限等）")

print("\n全部通过")

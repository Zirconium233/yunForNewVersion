# -*- coding: utf-8 -*-
"""实机验收 L1 准入探测：登录 + run/getRlStatus，不创建跑步记录。

除登录内部的公共学校目录查询外，只发以下两类请求：
  1) /login/<school_login_url>（tools.Login 既有实现，凭据取自 config.ini
     [Login]，登录失败可能触发服务端锁定/验证码——首次失败即停，勿重试）；
  2) /run/getRlStatus  {"raRunArea": <命令行参数>}（APK 证据：
     NewRunningFragment.java:929-932 构造，:640-698 消费 runFaceStudentStatus）。
不发 start/split/finish/isStandard/face。登录会更新会话及本地配置，可能使
手机端会话失效，因此此脚本并非零状态变更。

用法：在 config.ini 填好 [Login] username/password 与 Yun 段
（先用 tools/getUrl_Id.py 查询学校地址与 ID；school_login_url 需对应学校确认）。

  python live_probe.py <raRunArea>
"""
import json
import sys

import main as M
from tools.Login import Login
from yun_http import redact


def interpret(status):
    # NewRunningFragment.java:646-697 分支语义
    return {
        "Y": "GO：人脸准入正常，正版 APP 会直接开跑（可进 L2/L3）",
        "N": "需采集：APP 会先强制人脸采集（runFaceInfo），本脚本不实现采集链；"
             "无人脸任务不受影响",
        "N0": "认证失败需重新采集（同上，需真人 APP 操作）",
        "N1": "审核中：APP 自身拒绝开跑——当前账号一切跑步验收都不可行",
    }.get(status, "未知状态：如实呈报，不猜测")


def run(ra_run_area, conf_path=None):
    cfg = M.resolve_cli_path(conf_path) if conf_path else M.project_resource("config.ini")
    M.set_args(cfg)
    if not M.my_host:
        print("[L1] school_host 未配置：请先用 tools/getUrl_Id.py 查询并确认学校地址")
        return 2
    result = Login.main(cfg)
    if result is None:
        print("[L1] 登录失败/取消：停止（勿盲目重试，留意账号锁定）")
        return 3
    M.apply_login_result(*result, conf_path=cfg)
    client = M.default_client()
    obj = client.post_json("/run/getRlStatus",
                           json.dumps({"raRunArea": ra_run_area}),
                           raise_on_business_code=False)
    print("[L1] getRlStatus 原样响应（脱敏）：")
    print("  " + json.dumps(redact(obj), ensure_ascii=False))
    if not isinstance(obj, dict) or obj.get("code") != 200:
        print("[L1] 业务请求失败：不解释准入状态，停止")
        return 4
    data = obj.get("data")
    if not isinstance(data, dict):
        print("[L1] 准入响应缺少有效 data：停止")
        return 4
    status = data.get("runFaceStudentStatus")
    print(f"[L1] runFaceStudentStatus={status!r}")
    print("[L1] " + interpret(status))
    return 0 if status == "Y" else 5


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit("缺少参数：<raRunArea>（任务列表中的跑步区域名，只读输入）")
    raise SystemExit(run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))



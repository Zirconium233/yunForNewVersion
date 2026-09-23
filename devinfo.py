# -*- coding: utf-8 -*-
"""云运动真机助手：设备信息 / token 提取 / 明文抓包（依赖 adb，不依赖本项目其他代码）

用法：python devinfo.py   （或双击，需已关联 Python）
"""
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = "com.yunzhi.tiyu"
PLAT_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
TOKEN_RE = re.compile(r'"token"\s*:\s*"(.*?)"')
LABEL_RE = re.compile(r"D snow\s*:\s*(\S+?):(.*)")

C_RESET = "\033[0m"
C_CYAN, C_YELLOW, C_GREEN, C_MAGENTA, C_RED = "\033[36m", "\033[33m", "\033[32m", "\033[35m", "\033[31m"


def ask(prompt: str = "", default: str = "") -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return default


def find_adb() -> str:
    p = shutil.which("adb")
    if p:
        return p
    local = ROOT / "platform-tools" / "adb.exe"
    if local.is_file():
        return str(local)
    print("未检测到 adb（Android 调试桥）")
    if ask("是否现在下载安装 platform-tools（约15MB，Google官方）? (Y/N)").upper() != "Y":
        sys.exit(1)
    zip_path = Path(os.environ.get("TEMP", ".")) / "platform-tools.zip"
    print(f"下载中: {PLAT_URL}")
    urllib.request.urlretrieve(PLAT_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(ROOT)
    zip_path.unlink(missing_ok=True)
    print(f"已安装: {local}")
    return str(local)


def run(args, capture=True):
    r = subprocess.run(args, capture_output=capture, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip(), r.returncode


ADB = find_adb()


def adb_shell(cmd: str) -> str:
    out, _ = run([ADB, "shell"] + cmd.split())
    return out


def logcat_dump():
    out, code = run([ADB, "logcat", "-d", "-s", "snow:D"])
    return [] if code else out.splitlines()


def report_tokens(lines) -> bool:
    found = False
    for line in lines:
        m = TOKEN_RE.search(line)
        if m:
            print(C_RESET + line)
            print(f"{C_GREEN}token: {m.group(1)}{C_RESET}")
            found = True
    return found


def mode_token():
    if report_tokens(logcat_dump()):
        return
    run([ADB, "logcat", "-c"])
    print("现有日志中无 token，请在手机上登录云运动…（最长3分钟）")
    end = time.time() + 180
    while time.time() < end:
        time.sleep(1)
        if report_tokens(logcat_dump()):
            return
    print(f"{C_RED}超时未抓到 token{C_RESET}")


def mode_capture():
    run([ADB, "logcat", "-c"])
    print("开始抓包，请在手机上操作云运动…（Ctrl+C 停止）")
    labels = {"url": ("地址    ", C_CYAN), "params": ("请求明文", C_YELLOW),
              "response": ("响应明文", C_GREEN), "unzip": ("响应解压", C_MAGENTA)}
    proc = subprocess.Popen([ADB, "logcat", "-s", "snow:D"],
                            stdout=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    try:
        for line in proc.stdout:
            m = LABEL_RE.search(line)
            if not m:
                continue
            kind, body = m.group(1), m.group(2).strip()
            if kind not in labels:      # backSource 等直接跳过
                continue
            ts = " ".join(line.split()[:2])
            name, color = labels[kind]
            if not body:
                body = "(无参数)"
            print(f"{C_RESET}{ts} {color}[{name}] {body}{C_RESET}")
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


def main():
    if os.name == "nt":
        os.system("chcp 65001 >nul")
        for s in (sys.stdout, sys.stderr):
            if s.encoding and s.encoding.lower().replace("-", "") != "utf8":
                s.reconfigure(encoding="utf-8", errors="replace")
    _, code = run([ADB, "get-state"])
    if code:
        print(f"{C_RED}[错误] adb 未连接手机（USB调试未开/未插线）{C_RESET}")
        sys.exit(1)

    mfr = adb_shell("getprop ro.product.manufacturer")
    model = adb_shell("getprop ro.product.model")
    rel = adb_shell("getprop ro.build.version.release")
    dev = adb_shell(f"cat /sdcard/Android/data/{PKG}/files/.sys_device_id")

    print("-" * 46)
    print(f"deviceName : {mfr}({model})")
    print(f"sysEdition : Android_{rel}")
    print(f"deviceId   : {dev or '(未读到:云运动可能未安装或未启动过)'}")
    print("-" * 46)

    print("[1] 取值：提取登录 token（先查现有日志，没有再等你登录）")
    print("[2] 抓包：实时滚动 请求/响应 明文")
    sel = ask("选择功能: ", "1")
    if sel == "2":
        mode_capture()
    else:
        mode_token()
    ask("按回车退出")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""云运动真机助手：设备信息 / token 提取 / 客户端调试日志查看（依赖 adb）

重要说明：本工具不是抓包，也没有解密 HTTPS——它读取的是云运动 App 自行打印到
Android 调试日志(logcat)中的明文请求与响应（加密前 / 解密后的本机内存数据）。
若官方在后续版本关闭这些日志打印，本工具将失效。

支持真机（USB 或无线 adb）与模拟器（虚拟机+虚拟定位环境同样适用，只要 adb 能连上）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = "com.yunzhi.tiyu"
PLAT_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
LOG_ROOT = ROOT / "devinfo_logs"

TOKEN_RE = re.compile(r'"token"\s*:\s*"(.*?)"')
# logcat -v threadtime: "MM-DD HH:MM:SS.mmm  PID  TID LEVEL TAG: msg"
TT_RE = re.compile(r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+[VDIWEAF]\s+snow\s*:\s?(.*)$")

LABELS = {  # 标签 -> (中文名, 颜色)
    "params":   ("请求明文", "\033[33m"),
    "url":      ("地址", "\033[36m"),
    "@@@@":     ("地址(备用封装)", "\033[36m"),
    "response": ("响应明文", "\033[32m"),
    "decry":    ("解密响应(备用封装)", "\033[32m"),
    "unzip":    ("响应解压", "\033[35m"),
    "backSource": ("原始响应(未确认明/密文)", "\033[90m"),
}

C_RESET = "\033[0m"


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


ADB = find_adb()


def run(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", "", -1
    return (r.stdout or "").strip(), (r.stderr or "").strip(), r.returncode


def adb_shell(cmd: str) -> str:
    out, _, _ = run([ADB, "shell"] + cmd.split())
    return out


def app_pid() -> str:
    """云运动进程号，用于过滤同名标签；取不到则返回空串（不过滤并提示）。"""
    out, _, code = run([ADB, "shell", "pidof", PKG])
    return out.split()[0] if code == 0 and out.split() else ""


def classify_read_error(out: str, err: str, code: int) -> str:
    text = (out + err).lower()
    if "offline" in text or "unauthorized" in text or "no device" in text or code == -1:
        return "(设备连接异常：offline/unauthorized/超时)"
    if "permission denied" in text:
        return "(读取被拒：该文件属 app 私有外部目录，部分系统版本需 root)"
    if "no such file" in text or "not found" in text or "does not exist" in text:
        return "(文件不存在：云运动可能未安装或未启动过)"
    return f"(读取失败: {(out + err).strip()[:80] or '未知错误'})"


def parse_tt(line: str):
    """解析 threadtime 行 → (时间戳, pid, tid, 正文)；失败返回 None。"""
    m = TT_RE.match(line.strip())
    return m.groups() if m else None


def split_label(msg: str):
    """返回 (label, body)；备用封装用 '@@@@<url>' 形式（无冒号）。"""
    if msg.startswith("@@@@"):
        return "@@@@", msg[4:].strip()
    m = re.match(r"^(\S+?):(.*)$", msg)
    return (m.group(1), m.group(2).strip()) if m else (None, msg)


def trunc_note(body: str) -> str:
    if body[:1] in "{[" and body[-1:] not in "}]":
        return "  ⚠ JSON 可能被日志截断或含多行，勿当完整响应"
    return ""


def ts_age_warning(ts: str) -> str:
    """token 时间戳距今超过10分钟时提示可能是历史会话。"""
    try:
        now = datetime.now()
        t = datetime.strptime(f"{now.year}-{ts.split('.')[0]}", "%Y-%m-%d %H:%M:%S")
        if t.month > now.month + 1:
            t = t.replace(year=now.year - 1)
        if now - t > timedelta(minutes=10):
            return f"  ⚠ 该记录产生于 {ts.split('.')[0]}，可能是历史会话的旧 token"
    except ValueError:
        pass
    return ""


class LogSaver:
    """先把收到的每一行原样落盘，再做解析展示。"""

    def __init__(self):
        LOG_ROOT.mkdir(exist_ok=True)
        self.dir = LOG_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir()
        self.raw = (self.dir / "raw.log").open("w", encoding="utf-8", errors="replace")
        self.events = (self.dir / "events.jsonl").open("w", encoding="utf-8", errors="replace")

    def put(self, line: str):
        self.raw.write(line + "\n")
        self.raw.flush()

    def put_event(self, ts, pid, tid, label, body):
        self.events.write(json.dumps({"ts": ts, "pid": pid, "tid": tid,
                                      "label": label, "body": body},
                                     ensure_ascii=False) + "\n")
        self.events.flush()

    def close(self):
        for f in (self.raw, self.events):
            try:
                f.flush()
                f.close()
            except Exception:
                pass


def start_logcat(pid: str):
    args = [ADB, "logcat", "-v", "threadtime"]
    if pid:
        args += ["--pid", pid]
    return subprocess.Popen(args + ["-s", "snow:D"],
                            stdout=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")


def dump_logcat(pid: str):
    args = [ADB, "logcat", "-d", "-v", "threadtime"]
    if pid:
        args += ["--pid", pid]
    out, _, code = run(args + ["-s", "snow:D"])
    return [] if code else out.splitlines()


def render(ts, pid, tid, label, body):
    name, color = LABELS[label]
    return f"{C_RESET}{ts} pid={pid} tid={tid} {color}[{name}]{C_RESET} {body}{trunc_note(body)}"


def mode_token(pid: str):
    if ask("先清空设备日志缓冲？历史 token 也会被清掉 (y/N)").lower() == "y":
        run([ADB, "logcat", "-c"])
    found = _token_scan(dump_logcat(pid))
    if found:
        return
    print("现有日志中暂无 token，请在手机上登录云运动…（最长3分钟，Ctrl+C 停止）")
    end = time.time() + 180
    try:
        while time.time() < end:
            time.sleep(2)
            if _token_scan(dump_logcat(pid)):
                return
    except KeyboardInterrupt:
        pass
    print(f"\033[31m未抓到 token\033[0m")


def _token_scan(lines) -> bool:
    found = False
    for line in lines:
        m = TOKEN_RE.search(line)
        if not m:
            continue
        parsed = parse_tt(line)
        ts = parsed[0] if parsed else "?"
        print(C_RESET + line)
        print(f"\033[32mtoken: {m.group(1)}\033[0m  (日志时间 {ts}){ts_age_warning(ts)}")
        found = True
    return found


def mode_view(pid: str):
    saver = LogSaver()
    if ask("开始记录前清空设备日志缓冲？(y/N)").lower() == "y":
        run([ADB, "logcat", "-c"])
    print(f"开始读取调试日志（原始行同时存档到 {saver.dir}\\raw.log），Ctrl+C 停止并收尾")
    proc = start_logcat(pid)
    try:
        for line in proc.stdout:
            saver.put(line.rstrip("\n"))
            parsed = parse_tt(line)
            if not parsed:
                continue
            ts, p, t, msg = parsed
            label, body = split_label(msg)
            if label is None:
                continue
            if label in LABELS:
                saver.put_event(ts, p, t, label, body)
                print(render(ts, p, t, label, body))
            else:
                print(f"{C_RESET}{ts} pid={p} tid={t} \033[90m[未识别标签:{label}]（已存原始日志）\033[0m {body[:120]}")
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在收尾…")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        saver.close()
        print(f"存档完成: {saver.dir}")


# ─────────────────────────── 脱敏打包 ───────────────────────────
MASKS = [
    (re.compile(r'("password"\s*:\s*")[^"]*(")'), r"\1***\2"),
    (re.compile(r'("token"\s*:\s*")[^"]{4}[^"]*(")'), r"\1****\2"),
    (re.compile(r'("faceBaseData"\s*:\s*")[^"]+(")'), r"\1<BASE64_STRIPPED>\2"),
    (re.compile(r'("(userName|realName|nickName|phonenumber|studentId|account)"\s*:\s*")[^"]*(")'), r"\1***\3"),
    (re.compile(r"\b[0-9a-f]{64}\b"), lambda m: m.group(0)[:8] + "0" * 56),
    (re.compile(r'"(deviceId|device_id)"\s*:\s*"([^"]{8})[^"]*"'), r'"\1":"\2****"'),
]


def sanitize_text(text: str) -> str:
    for pat, rep in MASKS:
        text = pat.sub(rep, text)
    return text


def mode_zip():
    if not LOG_ROOT.is_dir():
        print("还没有任何存档日志（先用功能[2]记录一次）")
        return
    folders = sorted([d for d in LOG_ROOT.iterdir() if d.is_dir()])
    if not folders:
        print("devinfo_logs 为空")
        return
    print("可打包的存档：")
    for d in folders:
        print(" -", d.name)
    pick = ask(f"输入要打包的存档名（回车默认最新 {folders[-1].name}）: ") or folders[-1].name
    src = LOG_ROOT / pick
    out_zip = LOG_ROOT / f"{pick}_sanitized.zip"
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.iterdir()):
            if f.is_file():
                z.writestr(f"{pick}/{f.name}", sanitize_text(f.read_text(encoding="utf-8", errors="replace")))
    print(f"已生成脱敏包（已抹除 密码/token/姓名/学号/人脸base64/设备号）: {out_zip}")


def main():
    if os.name == "nt":
        os.system("chcp 65001 >nul")
        for s in (sys.stdout, sys.stderr):
            if s.encoding and s.encoding.lower().replace("-", "") != "utf8":
                s.reconfigure(encoding="utf-8", errors="replace")
    _, err, code = run([ADB, "get-state"])
    if code:
        print(f"\033[31m[错误] adb 设备不可用: {(err or '未连接')}\033[0m")
        sys.exit(1)

    mfr = adb_shell("getprop ro.product.manufacturer")
    model = adb_shell("getprop ro.product.model")
    rel = adb_shell("getprop ro.build.version.release")
    out, err, rcode = run([ADB, "shell", "cat", f"/sdcard/Android/data/{PKG}/files/.sys_device_id"])
    dev = out.strip() or classify_read_error(out, err, rcode)

    print("-" * 52)
    print(f"deviceName : {mfr}({model})")
    print(f"sysEdition : Android_{rel}")
    print(f"deviceId   : {dev}")
    pid = app_pid()
    print(f"进程过滤   : {'pid=' + pid if pid else '未找到云运动进程（不过滤，可能混入同名标签）'}")
    print("-" * 52)

    print("[1] 提取登录 token（读调试日志，不请求业务服务器）")
    print("[2] 查看调试日志（实时滚动 + 原始行存档，可脱敏打包分享）")
    print("[3] 将已存档日志脱敏打包 zip")
    sel = ask("选择功能: ", "0")
    if sel == "1":
        mode_token(pid)
    elif sel == "2":
        mode_view(pid)
    elif sel == "3":
        mode_zip()
    ask("按回车退出")


if __name__ == "__main__":
    main()

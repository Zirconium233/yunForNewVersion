# -*- coding: utf-8 -*-
import argparse
import configparser
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import yun_route  # 轨迹合成引擎（deform）；只 import，不修改

CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
ROUTES_DIR = os.path.join(BASE_DIR, "examples", "routes")
MASK = "__MASKED__"          # 返回前端的敏感值占位
KEEP = "__KEEP__"            # 前端未改动敏感值的回传哨兵
SENSITIVE_KEYS = {"password", "token", "cipherkey", "cipherkeyencrypted"}

# 面板自产的"发送素材"：work_dir 已被 .gitignore 忽略，不污染仓库。
SEND_DIR = os.path.join(BASE_DIR, "work_dir", "web_send")
DRILL_FIXTURE = os.path.join(SEND_DIR, "dry_home_drill.json")

RUN = {"proc": None, "buf": [], "lock": threading.Lock()}  # 执行日志占位实现
# 选项卡②最近一次成功预览对应的发送计划：/api/run 只跑它，保证"看到什么就发什么"，
# 也避免前端（或任何本地页面）能凭任意 argv 调起 main.py。
PLAN = {"argv": None, "dry": [], "detail": "", "cwd": BASE_DIR}


# ------------------------------------------------------------------ helpers
def list_campuses():
    out = {}
    for d in sorted(os.listdir(BASE_DIR)):
        if d.startswith("tasks_") and os.path.isdir(os.path.join(BASE_DIR, d)):
            files = sorted(f for f in os.listdir(os.path.join(BASE_DIR, d))
                           if f.endswith(".json"))
            if files:
                out[d] = files
    return out


def list_geojsons():
    """examples/routes/ 下可作形变来源的 GeoJSON（排除 fence* 围栏等非轨迹文件）。"""
    if not os.path.isdir(ROUTES_DIR):
        return []
    return sorted(f for f in os.listdir(ROUTES_DIR)
                  if f.endswith(".geojson") and not f.lower().startswith("fence"))


def list_sources():
    """形变方案的"任选底图 json"清单：任务表 + 任意 GeoJSON（仓库相对路径）。

    与 yun_route.extract_points 的能力对应：tasklist（data.pointsList）、
    GeoJSON LineString/FeatureCollection、裸坐标列表都能直接当形变来源。
    """
    out = []
    for campus, files in list_campuses().items():
        out.extend(f"{campus}/{f}" for f in files)
    out.extend(f"examples/routes/{f}" for f in list_geojsons())
    return out


def _source_path(rel):
    """把清单里的仓库相对路径解析为绝对路径；不在清单内一律拒绝（防目录穿越）。"""
    rel = (rel or "").replace("\\", "/")
    if rel not in list_sources():
        return None
    return os.path.join(BASE_DIR, *rel.split("/"))


def _task_file_points(path):
    """tasklist 原始点列 -> ([[lon,lat],...], km)。'point' 字段格式 'lon,lat'。"""
    j = json.load(open(path, encoding="utf-8-sig"))
    d = j.get("data") or j
    rows = d.get("pointsList") or []
    pts = [[float(x) for x in r["point"].split(",")] for r in rows]
    # 注意：里程字段实测可能是字符串（'2214' 米 / '3' 公里，格式跨表不统一），
    # 防御性处理：>100 视为米，否则视为公里（审计点：单位启发式有歧义风险）
    raw_km = float(rows[-1].get("runMileage") or 0) if rows else 0.0
    km = raw_km / 1000.0 if raw_km > 100 else raw_km
    return pts, km


# ------------------------------------------- 发送计划（②预览 ↔ ③命令，同一份事实）
def _rel(path):
    """绝对路径 → 仓库相对路径（正斜杠）：命令可读，main.py 按调用者 cwd(仓库根)解析。"""
    return os.path.relpath(path, BASE_DIR).replace("\\", "/")


def _dump_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return path


def _drill_fixture():
    """离线演练夹具（面板自产，放宽步频/里程区间）。

    判据来自假服务端回包：仓库自带 dry_run_home.json 是打表夹具（raDislikes=3、
    6~7 km），拿它演练合成轨迹必被 validate_generated_route 拒；这份只压链路，
    不冒充真实任务区间（真跑的区间由 getHomeRunInfo 下发，离线无从得知）。
    """
    return _dump_json(DRILL_FIXTURE, {
        "code": 200, "msg": "web.py offline drill fixture（放宽区间，仅演练链路）",
        "data": {"cralist": [{"id": 900001, "schoolId": "100", "raType": "T1",
                              "raRunArea": "A", "raDislikes": 0,
                              "raSingleMileageMin": 1.0, "raSingleMileageMax": 20.0,
                              "raCadenceMin": 1, "raCadenceMax": 1000,
                              "runFaceStatus": "N", "points": ""}]}})


def _plan_base(campus, task):
    """打表回放：把所选任务表放进"独占目录"，让 -a 不可能随机选到别的表。

    main.py 的 -t/--task_path 交给 do_by_points_map(random_choose=True)，内部是
    random.choice(os.listdir(目录))：直接把 tasks_fch 传进去，预览的 tasklist_0
    和真正发送的可能是任意一张表。故把所选表复制到只含该表的目录。
    """
    src = os.path.join(BASE_DIR, campus, task)
    pick_dir = os.path.join(SEND_DIR, "task_pick")
    os.makedirs(pick_dir, exist_ok=True)
    for old in os.listdir(pick_dir):
        os.remove(os.path.join(pick_dir, old))
    dst = os.path.join(pick_dir, task)
    shutil.copyfile(src, dst)
    with open(src, "rb") as a, open(dst, "rb") as b:
        if a.read() != b.read():
            raise ValueError("任务表复制校验失败：" + _rel(dst))
    return {"argv": ["-a", "-t", _rel(pick_dir)], "dry": ["--dry-run"], "cwd": BASE_DIR,
            "detail": f"{campus}/{task} → {_rel(pick_dir)}/（单表目录，-a 不会随机选表）"}


def _plan_deform(cfg, preview_task):
    """合成轨迹：把预览用的 cfg 原样落盘，③只引用这一份 cfg。

    自检：用 CLI 的同一入口（yun_route.generate，base_dir=cfg 所在目录）复算一遍，
    与预览逐点比对；不一致就报错，而不是给出一条"看起来对"的命令。
    """
    cfg_path = _dump_json(os.path.join(SEND_DIR, "route_cfg.json"), cfg)
    again = yun_route.generate(cfg_path)
    a, b = preview_task["data"]["pointsList"], again["data"]["pointsList"]
    same = (len(a) == len(b) and a[0]["point"] == b[0]["point"]
            and a[-1]["point"] == b[-1]["point"]
            and (preview_task["data"]["recordMileage"], preview_task["data"]["duration"])
            == (again["data"]["recordMileage"], again["data"]["duration"]))
    if not same:
        raise ValueError("预览与命令不一致（cfg 落盘后复算结果不同）：已拒绝生成命令")
    md = again["metadata"]
    return {"argv": ["-a", "--route-config", _rel(cfg_path)],
            "dry": ["--dry-run", "--dry-home", _rel(_drill_fixture())], "cwd": BASE_DIR,
            "detail": f"{_rel(cfg_path)} ｜ {md['source']} seed={md['seed']} "
                      f"第 {md['attempt']} 次抽中 ｜ {len(b)} 点 "
                      f"{again['data']['recordMileage']:.4f} km"}


def _cadence_gate_warn(cadence):
    """按本地 config.ini 的步频偏移，提前算出"真跑会不会被区间拒"。

    validate_generated_route 判据：raCadenceMin+偏移 ≤ 合成步频 ≤ raCadenceMax+偏移，
    偏移取自 config.ini（本仓库 +30/−150）。区间被压窄到需要 ≥180 spm 宽度时，
    典型任务区间（如 120~190）必然判负——真跑会在 start 之前中止，dry-run 不受影响。
    """
    if cadence is None:
        return ""
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    try:
        lo = int(cp.get("Run", "cadence_min_offset"))
        hi = int(cp.get("Run", "cadence_max_offset"))
    except Exception:
        return ""
    need_min, need_max = cadence - lo, cadence - hi
    if need_max - need_min <= 70:          # 70 ≈ 常见任务区间宽度（120~190），不算异常
        return ""
    return (f"步频闸门提示：本次合成步频 {cadence:.1f} spm；按本地 config.ini 偏移 "
            f"({lo:+d}/{hi:+d})，服务端必须 raCadenceMin ≤ {need_min:.0f} 且 "
            f"raCadenceMax ≥ {need_max:.0f}（区间宽 ≥ {need_max - need_min:.0f} spm）才放行。"
            f"若学校下发 120~190，真跑会被 validate_generated_route 拒（步频不在允许范围），"
            f"需调小 cadence_max_offset 的收缩量")


def _set_plan(plan):
    """把②的预览结果登记为③可执行（且唯一可执行）的计划。"""
    PLAN["argv"] = list(plan.get("argv") or []) or None
    PLAN["dry"] = list(plan.get("dry") or [])
    PLAN["detail"] = plan.get("detail", "")
    PLAN["cwd"] = plan.get("cwd", BASE_DIR)


def build_preview(qs):
    scheme = (qs.get("scheme") or ["base"])[0]
    if scheme == "base":
        campus = (qs.get("campus") or ["tasks_fch"])[0]
        task = (qs.get("task") or [""])[0]
        meta = list_campuses()
        if campus not in meta:
            return {"error": "未知校区目录: " + campus}
        if task not in meta[campus]:
            task = meta[campus][0]
        ref, km = _task_file_points(os.path.join(BASE_DIR, campus, task))
        return {"ref": ref, "send": ref,
                "ref_name": f"参考=即将发送：{campus}/{task}（打表原样上传）",
                "send_name": "打表原轨迹", "send_km": round(km, 3), "note": "",
                "command": _plan_base(campus, task), "warn": ""}

    if scheme == "deform":
        # 轨迹合成（deform）：5.5m 弧长重采样 + 法向 OU 游走 + 二维相关漂移 +
        # 稀疏衰减跳变，再按 ±10% 抽签配速/步频/总里程并做一致性采样。
        # 与 main.py --route-config 共用 yun_route.generate_cfg 同一实现。
        src = (qs.get("src") or [""])[0]
        src_path = _source_path(src)
        if src_path is None:
            return {"error": "未知底图：" + (src or "(空)")}
        try:
            seed = int((qs.get("seed") or ["31337"])[0])
            interval = int((qs.get("interval") or ["2"])[0])
        except ValueError:
            return {"error": "seed / 采样间隔必须是整数"}
        cfg = {"source_json": src_path, "coordinate_system": "GCJ-02",
               "seed": seed, "sample_seconds": interval}
        for key, qk, label in (("min_distance_m", "min", "最短里程"), ("max_distance_m", "max", "最长里程")):
            raw = (qs.get(qk) or [""])[0].strip()
            if raw:
                try:
                    cfg[key] = float(raw)
                except ValueError:
                    return {"error": f"{label}必须是数字（留空=不限制）"}
        try:
            task = yun_route.generate_cfg(cfg, BASE_DIR)
        except ValueError as exc:
            return {"error": str(exc), "window_hint": "可放宽最短/最长里程，或改 seed 重抽"}
        md, data = task["metadata"], task["data"]
        points, ref_metrics = yun_route.load_source(src_path)
        rows = data["pointsList"]
        try:
            command = _plan_deform(cfg, task)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"ref": [[p[0], p[1]] for p in points],
                "send": [[float(x) for x in r["point"].split(",")] for r in rows],
                "ref_name": f"参考原轨迹：{src}",
                "send_name": f"合成任务（seed={md['seed']}，第 {md['attempt']} 次抽中）",
                "send_km": data["recordMileage"],
                "metrics": {
                    "ref_len_km": round(ref_metrics["length_m"]/1000.0, 3),
                    "ref_pace": None if ref_metrics["pace_min_km"] is None else round(ref_metrics["pace_min_km"], 2),
                    "ref_cadence": None if ref_metrics["cadence_spm"] is None else round(ref_metrics["cadence_spm"], 1),
                    "send_len_km": round(data["recordMileage"], 3),
                    "send_pace": round(data["recodePace"], 2),
                    "send_cadence": round(data["recodeCadence"], 1),
                    "chord_km": round(md["geometry_chord_m"]/1000.0, 3),
                    "interval_s": md["interval_s"], "step_m": md["step_m"],
                    "points": len(rows), "attempt": md["attempt"],
                    "window": md["distance_window_m"],
                },
                "note": f"输入 {len(points)} 点 → 输出 {len(rows)} 点",
                "command": command, "warn": _cadence_gate_warn(data["recodeCadence"])}

    return {"error": "未知 scheme: " + scheme}


def read_config_masked():
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")
    out = {}
    for sec in cp.sections():
        out[sec] = {k: (MASK if k.lower() in SENSITIVE_KEYS and cp[sec][k]
                        else cp[sec][k]) for k in cp[sec]}
    return out


def save_config(payload):
    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH, encoding="utf-8")   # 先读原值，__KEEP__ 直接跳过
    for sec, kv in (payload or {}).items():
        sec = str(sec).strip()
        if not sec or sec.lower() == "DEFAULT":
            continue
        if not cp.has_section(sec):
            cp.add_section(sec)
        for k, v in kv.items():
            if v in (KEEP, MASK):   # 纵深防御：掩码占位串永远不许落盘
                continue
            cp[sec][str(k).strip()] = "" if v is None else str(v)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)   # 已验证 config.ini 无注释，重写无损（见文件头 TODO）


def run_main(args_list):
    with RUN["lock"]:
        if RUN["proc"] is not None and RUN["proc"].poll() is None:
            return {"error": "已有 main.py 在运行，先点停止"}
        RUN["buf"] = []
        cmd = [sys.executable, os.path.join(BASE_DIR, "main.py")] + list(args_list)
        # 子进程 stdout 是管道 → Python 默认按本地代码页(GBK)写，而这里按 utf-8 读，
        # 日志会整片乱码。只改子进程的 stdio 编码（PYTHONIOENCODING 不影响 open() 默认值）。
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        RUN["buf"].append("$ " + " ".join(cmd) + "\n")
        RUN["proc"] = subprocess.Popen(
            cmd, cwd=BASE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")
        t = threading.Thread(target=_pump, args=(RUN["proc"],), daemon=True)
        t.start()
    return {"ok": True}


def _pump(proc):
    for line in proc.stdout:
        with RUN["lock"]:
            RUN["buf"].append(line)
            if len(RUN["buf"]) > 20000:          # 缓冲上限，防长跑内存膨胀
                del RUN["buf"][:10000]
            RUN["buf"][:] = RUN["buf"]           # offset 语义简单化：截断后前端全量重拉
    proc.wait()
    with RUN["lock"]:
        RUN["buf"].append(f"\n[进程退出 code={proc.returncode} {time.strftime('%H:%M:%S')}]\n")


# ------------------------------------------------------------------ frontend
PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>云运动 · 本地工作台</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <style>
    :root {
      --nav:#111c31;--nav-deep:#0c1425;--nav-sub:#a2b2cc;
      --ink:#162138;--ink-2:#344157;--muted:#738198;--hint:#9ca8b9;
      --blue:#3668e9;--blue-deep:#2456d9;--cyan:#12b9b1;
      --bg:#f4f7fb;--surface:#ffffff;--border:#e2e9f2;
      --soft:#f8fafc;--shadow:0 10px 35px rgba(29,52,95,.05);
      --radius:18px;
    }
    *{box-sizing:border-box}
    html{scroll-behavior:smooth}
    body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,"Segoe UI",-apple-system,BlinkMacSystemFont,"Microsoft YaHei","PingFang SC",sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
    button,input,select,textarea{font:inherit}
    button{cursor:pointer}
    a{color:inherit;text-decoration:none}
    code{font-family:"Cascadia Code",Consolas,"SFMono-Regular",monospace}
    ::selection{background:#dbeafe;color:#173c82}
    .app-shell{min-height:100vh;display:grid;grid-template-columns:264px minmax(0,1fr)}
    .sidebar{background:linear-gradient(168deg,#14233a 0%,#111b2e 56%,#0b1424 100%);color:#fff;min-height:100vh;position:sticky;top:0;height:100vh;padding:30px 16px 22px;display:flex;flex-direction:column;overflow:hidden;isolation:isolate}
    .sidebar:before{content:"";position:absolute;inset:0;pointer-events:none;z-index:-1;background:radial-gradient(circle at -45% -15%,rgba(26,190,178,.26),transparent 40%),radial-gradient(circle at 120% 65%,rgba(68,104,238,.18),transparent 45%)}
    .brand{display:flex;align-items:center;gap:13px;padding:0 14px 36px}
    .brand-mark{width:42px;height:42px;border:1px solid rgba(167,230,235,.28);border-radius:14px;display:grid;place-items:center;color:#e7ffff;background:linear-gradient(140deg,rgba(27,200,182,.35),rgba(32,107,224,.27));box-shadow:0 5px 20px #05102150}
    .brand-mark svg{width:25px;height:25px}
    .brand-title{font-size:18px;line-height:1.3;letter-spacing:.07em;font-weight:760}
    .brand-caption{display:block;font-size:10px;color:#91a7c5;letter-spacing:.18em;font-weight:670;margin-top:3px}
    .nav-caption{font-size:10px;letter-spacing:.22em;color:#6f89ad;font-weight:800;padding:8px 16px 12px}
    #tabs{display:flex;flex-direction:column;gap:7px}
    .tab{min-height:49px;display:flex;align-items:center;gap:13px;cursor:pointer;color:#a8b7d0;font-size:13.5px;font-weight:630;letter-spacing:.01em;border:1px solid transparent;border-radius:11px;padding:0 14px;position:relative;transition:background .18s ease,color .18s ease,transform .18s ease}
    .tab svg{width:19px;height:19px;flex:none;stroke-width:1.8}
    .tab:hover{color:#f0f8ff;background:rgba(255,255,255,.06)}
    .tab.on{color:white;background:linear-gradient(95deg,rgba(60,118,222,.32),rgba(27,151,178,.10));border-color:rgba(145,192,255,.12);box-shadow:inset 3px 0 0 #56d7d0}
    .tab.on svg{color:#70e1da}
    .sidebar-bottom{margin-top:auto;padding:20px 13px 0;border-top:1px solid rgba(160,192,233,.12)}
    .sidebar-visual{height:83px;position:relative;margin:0 0 16px;border:1px solid rgba(157,205,246,.12);border-radius:12px;overflow:hidden;background:radial-gradient(ellipse at 65% 65%,#28648639,transparent 55%),repeating-radial-gradient(ellipse at 60% 95%,transparent 0 16px,#5db6d32e 17px 18px,transparent 19px 27px)}
    .sidebar-visual span{position:absolute;left:14px;top:12px;font-size:10px;letter-spacing:.16em;color:#9ab9d1}
    .sidebar-visual i{position:absolute;width:6px;height:6px;border-radius:50%;background:#41e1c8;right:43px;bottom:25px;box-shadow:0 0 0 7px #4bd7bd20,0 0 22px #26e5c96b}
    .local-label{display:flex;align-items:center;gap:9px;font-size:12px;color:#c7d9e9;font-weight:650}
    .local-label:before{content:"";width:7px;height:7px;border-radius:50%;background:#39d7aa;box-shadow:0 0 0 4px #2ac79c1b}
    #bar{margin-top:9px;word-break:break-word;font-size:11px;color:#839cbb;line-height:1.75}
    #bar a{color:#b6d1f2;text-decoration:underline;text-decoration-color:#6283a9;text-underline-offset:3px}
    .workspace{min-width:0;min-height:100vh}
    .topbar{height:78px;background:rgba(255,255,255,.91);border-bottom:1px solid var(--border);padding:0 clamp(20px,3.5vw,58px);display:flex;justify-content:space-between;align-items:center;gap:12px}
    .breadcrumbs{display:flex;align-items:center;gap:9px;font-size:12px;font-weight:650;color:#8b99ac}
    .breadcrumbs strong{font-weight:760;color:#435373}
    .top-right{display:flex;align-items:center;gap:13px}
    .chip{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border-radius:30px;border:1px solid #dde9e9;background:#f4fbf9;color:#328272;font-size:11px;font-weight:760;letter-spacing:.03em}
    .chip i{width:6px;height:6px;border-radius:50%;background:#2ac49a}
    .top-clock{display:inline-flex;align-items:center;color:#9aa7b6;font-size:11px;letter-spacing:.1em;font-weight:750}
    .content{padding:38px clamp(20px,3.5vw,58px) 72px;max-width:1640px;margin:0 auto}
    .pane{display:none;animation:reveal .22s ease}
    .pane.on{display:block}
    @keyframes reveal{from{opacity:.5;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
    .page-titleline{display:flex;flex-wrap:wrap;gap:14px;justify-content:space-between;align-items:center;margin:0 0 28px}
    .eyebrow{font-size:10px;font-weight:830;letter-spacing:.22em;color:#477de2;text-transform:uppercase;margin:0 0 7px}
    h1{font-weight:800;letter-spacing:-.05em;font-size:clamp(27px,2.7vw,35px);line-height:1.28;margin:0 0 6px}
    .page-description{font-size:13px;color:var(--muted);margin:0;max-width:720px}
    .top-number{font-size:11px;color:#a7b2c1;border:1px solid var(--border);padding:8px 11px;border-radius:10px;background:#fff;letter-spacing:.1em;font-weight:750}
    .surface{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);min-width:0}
    .section-header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:17px}
    .section-heading{font-size:15px;font-weight:790;margin:0;color:#24334d;letter-spacing:-.02em}
    .section-subtitle{color:#8795aa;font-size:11px}
    .section-badge{font-size:10px;letter-spacing:.07em;color:#5b7baa;font-weight:740;padding:5px 9px;border-radius:7px;background:#f0f5fd}
    .info-banner{display:flex;gap:13px;align-items:flex-start;background:linear-gradient(95deg,#eef5ff,#f5f8ff);padding:17px 20px;border:1px solid #dce9ff;border-radius:14px;margin-bottom:24px}
    .info-banner .banner-icon{color:#4275e9;background:#dceafe;width:31px;height:31px;border-radius:9px;display:grid;place-items:center;flex:none}
    .banner-icon svg{width:17px;height:17px}
    .info-banner b{font-size:12.5px;font-weight:790;color:#2d4f91;display:block;margin-bottom:3px}
    .info-banner p{font-size:12px;color:#6580a5;margin:0}
    .info-banner code{color:#3762ae}
    .settings-surface{padding:26px;margin-bottom:18px}
    #cfg2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start;gap:16px}
    #cfg2 .cfg-anchor{display:none}
    fieldset{border:1px solid #e4eaf2;background:#fff;border-radius:13px;margin:0;padding:14px 18px 20px;min-width:0;display:grid;grid-template-columns:minmax(96px,.70fr) minmax(0,1.35fr);align-items:center;column-gap:14px;row-gap:11px}
    fieldset legend{font-weight:800;font-size:13px;color:#223855;padding:0 10px;letter-spacing:.01em}
    fieldset label{font-size:11.5px;font-weight:620;color:#69788f;text-align:left;overflow-wrap:anywhere;min-width:0}
    fieldset br{display:none}
    .f,select{background:#fff;border:1px solid #dce4ee;color:#26384f;border-radius:9px;outline:0;min-height:40px;padding:9px 12px;font-size:12px;transition:box-shadow .15s,border-color .15s,background .15s}
    .f:hover,select:hover{border-color:#aebfdb}
    .f:focus,select:focus{border-color:#6e98ed;box-shadow:0 0 0 3px #d8e8ff99}
    .f::placeholder{color:#a6b0bf}
    fieldset input.f{width:100%;min-width:0}
    button{border:0;transition:background .16s ease,box-shadow .16s ease,transform .16s ease}
    button:hover{transform:translateY(-1px)}
    button:active{transform:translateY(0)}
    .btn{min-height:40px;display:inline-flex;gap:8px;align-items:center;justify-content:center;border-radius:9px;padding:9px 17px;background:linear-gradient(130deg,#4276ec,#305fdc);color:white;font-size:12px;font-weight:750;box-shadow:0 5px 12px #3668e72b;white-space:nowrap}
    .btn:hover{background:linear-gradient(130deg,#4e85ff,#2b59d0);box-shadow:0 7px 17px #3567e63a}
    .btn.gray,.btn-secondary{background:#f2f5fa;color:#50627d;box-shadow:none;border:1px solid #e0e8f1}
    .btn.gray:hover,.btn-secondary:hover{background:#e9f0f9}
    .btn-danger{background:#fff2f3;color:#d4495f;box-shadow:none;border:1px solid #f8d9de}
    .btn-danger:hover{background:#ffe8ed}
    .btn svg{width:15px;height:15px}
    .actionbar{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:18px 22px;border-top:1px solid #edf1f6}
    #cfgmsg{font-size:11px;color:#338873;font-weight:650;margin-left:auto}
    .config-hint{font-size:11px;color:#95a3b5;margin-top:10px}
    .control-card{padding:24px 24px 20px;margin-bottom:18px}
    .control-grid{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:end;gap:15px 16px}
    .control-field{display:flex;flex-direction:column;gap:7px;min-width:0}
    .control-field>label,.field-caption{font-size:11px;font-weight:760;color:#65768d}
    .control-field select{max-width:100%;width:100%}
    .control-field.scheme{grid-column:1/-1;width:100%;max-width:500px;min-width:0}
    #pickBase,#pickDeform{grid-column:1;grid-row:2;display:flex;flex-wrap:wrap;align-items:end;gap:11px;min-width:0}
    #pickBase .mini-field,#pickDeform .mini-field{display:flex;flex-direction:column;gap:7px;min-width:0;flex:1 1 120px}
    #pickBase .mini-field:first-child{flex:1 1 145px}
    #pickDeform .mini-field.source{flex:3 1 245px}
    #pickDeform .mini-field.tiny{flex:0 1 80px}
    #pickBase select,#pickDeform select,#pickDeform input.f{width:100%!important;min-width:0}
    #pickDeform .mini-field label,#pickBase .mini-field label{font-size:11px;font-weight:760;color:#65768d;white-space:nowrap}
    .preview-button{grid-column:2;grid-row:2;align-self:end;min-width:135px}
    .metrics-surface{padding:20px 24px;margin-bottom:18px;min-height:82px}
    #pvinfo{font-size:12px;color:#617186;overflow-x:auto;margin:0;line-height:1.8}
    #pvinfo:empty:after{content:"请选择上方方案并点击「刷新预览」，轨迹详情会显示在这里。";color:#9ba8b8;font-size:12px}
    #pvinfo table{border-collapse:separate!important;border-spacing:0;width:100%;min-width:410px;margin:5px 0 10px;font-size:12px!important}
    #pvinfo th,#pvinfo td{padding:12px 15px!important;text-align:left;border-bottom:1px solid #e7edf5}
    #pvinfo th{background:#f2f6fc!important;font-size:11px;color:#647895}
    #pvinfo th:first-child{border-radius:8px 0 0 0}
    #pvinfo th:last-child{border-radius:0 8px 0 0}
    #pvinfo b{color:#275bd2}
    .map-surface{overflow:hidden;margin-bottom:18px}
    .map-head{padding:19px 24px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
    .map-legend{display:flex;align-items:center;flex-wrap:wrap;gap:15px;font-size:11px;font-weight:730;color:#71839b}
    .map-legend span{display:flex;align-items:center;gap:7px}
    .line-sample{display:inline-block;width:23px;height:0;border-top:3px solid #2974f2}
    .line-sample.reference{border-top:3px dashed #9da7b3}
    #map{height:clamp(390px,61vh,630px);background:linear-gradient(125deg,#e8f1ed,#e2eaf1);border-top:1px solid var(--border);border-bottom:1px solid var(--border);position:relative;z-index:1}
    .map-foot{padding:13px 20px;font-size:11px;color:#718399;line-height:1.75}
    .map-foot code{font-size:10.5px;color:#4165ad}
    .run-control{padding:23px;margin-bottom:18px}
    .run-inline{display:flex;flex-wrap:wrap;align-items:end;gap:10px}
    .run-input{display:flex;flex-direction:column;gap:7px;flex:1 1 360px;min-width:180px}
    .run-input label{font-size:11px;font-weight:760;color:#65768d}
    #runCmd{width:100%!important;min-width:0;font-family:"Cascadia Code",Consolas,monospace;font-size:11.5px;min-height:42px;background:var(--soft);color:#33445e}
    #runCmd[readonly]{cursor:default}
    #runCmd[readonly]:focus{border-color:#dce4ee;box-shadow:none}
    .dry-toggle{display:inline-flex;align-items:center;gap:7px;min-height:40px;padding:0 13px;border:1px solid #dce4ee;border-radius:9px;background:#fff;font-size:12px;font-weight:700;color:#4a5c76;cursor:pointer;user-select:none;white-space:nowrap}
    .dry-toggle:hover{border-color:#aebfdb}
    .dry-toggle input{width:15px;height:15px;accent-color:#3668e9;cursor:pointer;margin:0}
    .gate-warn{display:block;margin-top:6px;color:#a8620f;font-size:11.5px;line-height:1.75}
    .note{font-size:11px;color:#75869e;line-height:1.8;margin:14px 0 0}
    .terminal{background:#121d30;border-radius:var(--radius);overflow:hidden;box-shadow:0 18px 36px #12253c12;border:1px solid #23354e}
    .terminal-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:16px 20px;border-bottom:1px solid #253753;background:#162339}
    .terminal-title{display:flex;align-items:center;gap:9px;color:#dbe6f5;font-weight:720;font-size:12px}
    .terminal-dots{display:flex;gap:6px}
    .terminal-dots i{display:block;width:8px;height:8px;border-radius:50%;background:#ff7469}
    .terminal-dots i:nth-child(2){background:#e7c26c}
    .terminal-dots i:nth-child(3){background:#60c89d}
    .terminal-hint{font:10px Consolas,monospace;letter-spacing:.04em;color:#8499b5}
    #log{display:block;width:100%;min-height:465px;height:min(62vh,680px);padding:22px 24px;resize:vertical;background:#101b2e;border:0;outline:none;color:#a7e9ca;font:12px/1.78 "Cascadia Code","SFMono-Regular",Consolas,monospace;white-space:pre;overflow:auto;tab-size:2}
    #log::placeholder{color:#637997}
    .footer-note{font-size:10px;color:#a0adbc;margin-top:28px;text-align:center;letter-spacing:.02em}
    .svg-icon{width:16px;height:16px;vertical-align:-3px;flex:none}
    @media(max-width:1250px){#cfg2{grid-template-columns:1fr}}
    @media(max-width:850px){.app-shell{display:block}.sidebar{position:relative;min-height:unset;height:auto;padding:14px 18px}.sidebar:before{display:none}.brand{padding:0 0 12px}.brand-mark{width:36px;height:36px}.brand-title{font-size:15px}.nav-caption,.sidebar-bottom{display:none}#tabs{flex-direction:row;gap:5px;overflow:auto}.tab{flex:1 0 auto;min-height:43px;justify-content:center;padding:0 13px}.tab.on{box-shadow:inset 0 -3px 0 #56d7d0}.tab svg{width:17px;height:17px}.topbar{height:57px;padding:0 20px}.content{padding:25px 20px 60px}}
    @media(max-width:600px){.page-titleline{margin-bottom:22px}h1{font-size:27px}.content{padding:22px 13px 45px}.topbar{padding:0 14px}.top-clock{display:none}.settings-surface,.control-card,.metrics-surface,.run-control{padding:17px}.info-banner{padding:13px}.control-grid{grid-template-columns:minmax(0,1fr);align-items:stretch}#pickBase,#pickDeform{grid-column:1;grid-row:auto;width:100%;gap:9px}.preview-button{grid-column:1;grid-row:auto;width:100%}.control-field.scheme{grid-column:1}.map-head{padding:15px}.map-legend{gap:11px}.map-foot{padding:13px 15px}#map{height:440px}.actionbar{padding:15px;flex-wrap:wrap}.actionbar .btn{flex:1 1 175px}#cfgmsg{margin:0;width:100%}fieldset{grid-template-columns:1fr;row-gap:6px}fieldset>input.f{margin-bottom:7px}.terminal-head{padding:12px 15px}#log{padding:14px;height:420px}}
    @media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
  </style>
</head>
<body>
<div class="app-shell">
  <aside class="sidebar" aria-label="工作台侧边导航">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true">
        <svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 22l7-13 5 9 5-6 5 10"/><path d="M8 25h16"/><circle cx="12" cy="9" r="2" fill="currentColor" stroke="none"/></svg>
      </span>
      <div><div class="brand-title">云运动工作台</div><span class="brand-caption">LOCAL DASHBOARD</span></div>
    </div>
    <div class="nav-caption">WORKSPACE / 工作区</div>
    <nav id="tabs">
      <div class="tab on" data-p="p1">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M4 12h16M4 17h16"/><circle cx="8" cy="7" r="2" fill="#14233a"/><circle cx="15" cy="12" r="2" fill="#14233a"/></svg>
        <span>信息配置</span>
      </div>
      <div class="tab" data-p="p2">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15m6-12v15"/><path d="m11 11 2 2 2-3"/></svg>
        <span>路线预览</span>
      </div>
      <div class="tab" data-p="p3">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/></svg>
        <span>执行日志</span>
      </div>
    </nav>
    <div class="sidebar-bottom">
      <div class="sidebar-visual" aria-hidden="true"><span>ROUTE / VISUALIZATION</span><i></i></div>
      <div class="local-label">本地工作模式</div>
      <div id="bar">当前控制台地址：<a id="selflink" href="#">__URL__</a><br>敏感字段未编辑时保持原值</div>
    </div>
  </aside>
  <div class="workspace">
    <header class="topbar">
      <div class="breadcrumbs"><span>YUN</span><span>/</span><strong>CONTROL CENTER</strong></div>
      <div class="top-right"><span class="top-clock">WEB CONSOLE</span><span class="chip"><i></i>127.0.0.1 · LOCAL</span></div>
    </header>
    <main class="content">
      <section class="pane on" id="p1">
        <div class="page-titleline">
          <div><p class="eyebrow">01 / CONFIGURATION</p><h1>信息配置</h1><p class="page-description">集中管理本地配置文件。字段与现有 config.ini 保持一致，修改后可以随时重新读取。</p></div>
          <span class="top-number">SETTINGS · 01</span>
        </div>
        <div class="info-banner wip">
          <span class="banner-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 10v6m0-9v.5"/></svg></span>
          <div><b>快捷登录正在开发中</b><p>当前请继续使用 <code>python main.py</code> 的登录流程。本页只负责查看和修改配置；已有敏感信息默认隐藏，留空则保留原值。</p></div>
        </div>
        <div class="surface settings-surface">
          <div class="section-header"><div><h2 class="section-heading">配置参数</h2><div class="section-subtitle">CONFIGURATION FIELDS · 读取自 config.ini</div></div><span class="section-badge">本地文件</span></div>
          <div id="cfg"></div>
          <div id="cfg2"><div class="cfg-anchor" aria-hidden="true"></div></div>
          <p class="config-hint">带有隐藏提示的字段不会在未编辑时覆盖现有值。</p>
        </div>
        <div class="surface actionbar">
          <button class="btn" onclick="saveCfg()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4h12l3 3v13H4V4h1zM7 4v6h10V4M7 20v-7h10v7"/></svg>保存配置</button>
          <button class="btn gray" onclick="loadCfg()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v5h-5"/></svg>重新读取 INI</button>
          <span id="cfgmsg" role="status" aria-live="polite"></span>
        </div>
      </section>

      <section class="pane" id="p2">
        <div class="page-titleline">
          <div><p class="eyebrow">02 / ROUTE VISUALIZATION</p><h1>路线预览</h1><p class="page-description">在地图上对照参考轨迹和当前方案的轨迹点列，检查几何位置与路线信息。</p></div>
          <span class="top-number">PREVIEW · 02</span>
        </div>
        <div class="surface control-card">
          <div class="section-header"><div><h2 class="section-heading">预览参数</h2><div class="section-subtitle">ROUTE CONFIGURATION</div></div><span class="section-badge">地图可视化</span></div>
          <div class="control-grid">
            <div class="control-field scheme"><label for="scheme">路线方案</label>
              <select id="scheme" onchange="schemeUI()"><option value="base">方案 1 · 打表回放（原始任务表）</option><option value="deform">方案 2 · 轨迹合成（Deform）</option></select>
            </div>
            <span id="pickBase">
              <span class="mini-field"><label for="campus">校区目录</label><select id="campus"></select></span>
              <span class="mini-field"><label for="task">任务表</label><select id="task"></select></span>
            </span>
            <span id="pickDeform" style="display:none">
              <span class="mini-field source"><label for="srcSel">参考底图 JSON</label><select id="srcSel"></select></span>
              <span class="mini-field tiny"><label for="deformSeed">随机种子</label><input id="deformSeed" class="f" style="width:80px" value="31337"></span>
              <span class="mini-field tiny"><label for="deformInterval">采样间隔 / s</label><input id="deformInterval" class="f" style="width:56px" value="2"></span>
              <span class="mini-field tiny"><label for="deformMin">最短 / m</label><input id="deformMin" class="f" style="width:76px" placeholder="不限"></span>
              <span class="mini-field tiny"><label for="deformMax">最长 / m</label><input id="deformMax" class="f" style="width:76px" placeholder="不限"></span>
            </span>
            <button class="btn preview-button" onclick="preview()"><svg class="svg-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h4l3-7 4 14 3-7h2"/></svg>刷新预览</button>
          </div>
        </div>
        <div class="surface metrics-surface">
          <div class="section-header" style="margin-bottom:8px"><h2 class="section-heading">轨迹数据</h2><span class="section-badge">METRICS</span></div>
          <div id="pvinfo" aria-live="polite"></div>
        </div>
        <div class="surface map-surface">
          <div class="map-head"><div><h2 class="section-heading">交互地图</h2><div class="section-subtitle">INTERACTIVE MAP · 地图自动定位参考轨迹</div></div>
            <div class="map-legend"><span><i class="line-sample reference"></i>参考路线</span><span><i class="line-sample"></i>当前路线</span></div>
          </div>
          <div id="map" aria-label="轨迹地图"></div>
          <div class="map-foot note">灰色虚线为参考轨迹；蓝色实线为预览方案生成的点列——③「执行日志」发送的就是这条线。方案 2 与 <code>python main.py --route-config &lt;cfg&gt;</code> 使用同一实现；打表模式的旧式微量平移不会单独显示。</div>
        </div>
      </section>

      <section class="pane" id="p3">
        <div class="page-titleline">
          <div><p class="eyebrow">03 / EXECUTION LOGS</p><h1>执行日志</h1><p class="page-description">命令由「路线预览」的当前轨迹自动确定（只读）。勾选 dry run 走本地假传输演练，取消勾选即真实发送这条轨迹。</p></div>
          <span class="top-number">TERMINAL · 03</span>
        </div>
        <div class="surface run-control">
          <div class="section-header"><div><h2 class="section-heading">运行控制</h2><div class="section-subtitle">PROCESS CONTROL · 命令由「路线预览」当前轨迹自动确定</div></div><span class="section-badge">MAIN.PY</span></div>
          <div class="run-inline">
            <div class="run-input"><label for="runCmd">将要执行的命令（只读 · 跟随选项卡②的预览结果）</label><input class="f" id="runCmd" readonly placeholder="先在「路线预览」点一次「刷新预览」，命令会自动生成"></div>
            <button class="btn" onclick="runMain()"><svg class="svg-icon" viewBox="0 0 24 24" fill="currentColor"><path d="m8 5 12 7-12 7V5z"/></svg>执行</button>
            <label class="dry-toggle" title="勾选=追加 --dry-run（本地假传输：不登录、不探测学校、不 sleep、不发真实请求）"><input type="checkbox" id="dryRun" checked onchange="renderCmd()">dry run</label>
            <button class="btn btn-danger" onclick="killMain()"><svg class="svg-icon" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>停止</button>
            <button class="btn gray" onclick="offset=0;log.value=''">清屏</button>
          </div>
          <p class="note" id="cmdnote">命令与②里那条蓝线同源（同底图 / 同随机种子 / 同采样间隔与里程区间）。<code>-a</code> 自动确认：面板无法代答控制台的 y/n。</p>
        </div>
        <div class="terminal">
          <div class="terminal-head"><div class="terminal-title"><span class="terminal-dots"><i></i><i></i><i></i></span>&nbsp; 运行输出 / stdout</div><span class="terminal-hint">PYTHON PROCESS · LIVE LOG</span></div>
          <textarea id="log" readonly placeholder="等待进程输出…"></textarea>
        </div>
      </section>
      <div class="footer-note">YUN LOCAL DASHBOARD &nbsp;·&nbsp; WEB UI &nbsp;·&nbsp; 仅本地访问</div>
    </main>
  </div>
</div>
<script>
let offset=0, map=null, refLayer=null, sendLayer=null, meta=null, leafletOK=(typeof L!=='undefined');
let lastPlan=null, lastWarn='';   // ②最近一次成功预览：③的命令唯一来源
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab,.pane').forEach(x=>x.classList.remove('on'));
  t.classList.add('on'); document.getElementById(t.dataset.p).classList.add('on');
  if(t.dataset.p==='p2'){ if(!map&&leafletOK){ map=L.map('map');
      L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
        {subdomains:['1','2','3','4'],maxNativeZoom:18,maxZoom:20,attribution:'© 高德'}).addTo(map);
      refLayer=L.polyline([],{color:'#999',dashArray:'6,4',weight:3}).addTo(map);
      sendLayer=L.polyline([],{color:'#2379f6',weight:4}).addTo(map); }
    if(map) setTimeout(()=>map.invalidateSize(),50); }
});
async function j(u,opt){ const r=await fetch(u,opt); return r.json(); }

async function boot(){ meta=await j('/api/meta');
  const c=document.getElementById('campus'); c.innerHTML='';
  for(const k of Object.keys(meta.campuses)){ const o=document.createElement('option');o.value=k;o.textContent=k;c.appendChild(o);} 
  const ss=document.getElementById('srcSel'); ss.innerHTML='';
  for(const k of meta.sources){ const o=document.createElement('option');o.value=k;o.textContent=k;ss.appendChild(o);} 
  taskFill(); c.onchange=taskFill; loadCfg();
  document.getElementById('selflink').href=location.href;
  document.getElementById('selflink').textContent=location.href;
  renderCmd(); preview(); }
function taskFill(){ const t=document.getElementById('task'); t.innerHTML='';
  for(const k of (meta.campuses[document.getElementById('campus').value]||[])){
    const o=document.createElement('option');o.value=k;o.textContent=k;t.appendChild(o);} }
function schemeUI(){ const s=document.getElementById('scheme').value;
  document.getElementById('pickBase').style.display=(s==='base')?'':'none';
  document.getElementById('pickDeform').style.display=(s==='base')?'none':''; }
function renderCmd(){ const box=document.getElementById('runCmd'), cb=document.getElementById('dryRun');
  const note=document.getElementById('cmdnote');
  if(!lastPlan){ box.value='';
    note.innerHTML='命令与②里那条蓝线同源（同底图 / 同随机种子 / 同采样间隔与里程区间）。'+
      '先在「路线预览」点一次「刷新预览」，这里会自动生成对应的发送命令。'; return; }
  box.value='python main.py '+lastPlan.argv.concat(cb.checked?(lastPlan.dry||[]):[]).join(' ');
  note.innerHTML='工作目录 <code>'+lastPlan.cwd+'</code> ｜ '+lastPlan.detail+
    (cb.checked?' ｜ 已追加 <code>--dry-run</code>：本地假传输，不登录、不探测、不发真实请求'
               :' ｜ <b style="color:#c0392b">未勾选 dry run：将真实登录并发送这条轨迹</b>')+
    (lastWarn?'<span class="gate-warn">'+lastWarn+'</span>':''); }

async function loadCfg(){ const d=await j('/api/config'); const host=document.getElementById('cfg2')||document.getElementById('p1');
  host.querySelectorAll('fieldset').forEach(x=>x.remove());
  for(const [sec,kv] of Object.entries(d)){ const fs=document.createElement('fieldset');
    fs.innerHTML='<legend>['+sec+']</legend>';
    for(const [k,v] of Object.entries(kv)){ const lab=document.createElement('label'); lab.textContent=k;
      const inp=document.createElement('input'); inp.className='f'; inp.dataset.sec=sec; inp.dataset.key=k;
      if(v==='__MASKED__'){ inp.dataset.keep='1'; inp.placeholder='•••••• 已隐藏；不输入则保存时原样保留'; }
      else inp.value=v;
      fs.appendChild(lab); fs.appendChild(inp);
      const br=document.createElement('br'); fs.appendChild(br);} 
    host.insertBefore(fs, host.lastElementChild); }
  document.getElementById('cfgmsg').textContent='已读取 '+new Date().toLocaleTimeString(); }
async function saveCfg(){ const body={};
  document.querySelectorAll('#p1 input').forEach(i=>{ const sec=i.dataset.sec,k=i.dataset.key;
    body[sec]=body[sec]||{}; body[sec][k]=(i.dataset.keep&&i.value==='')?'__KEEP__':i.value; });
  const r=await j('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  document.getElementById('cfgmsg').textContent=r.ok?('保存成功 '+new Date().toLocaleTimeString()):('失败: '+(r.error||'?')); }

async function preview(){ const s=document.getElementById('scheme').value;
  const q=new URLSearchParams({scheme:(s==='base'?'base':'deform')});
  if(s==='base'){ q.set('campus',document.getElementById('campus').value);
                  q.set('task',document.getElementById('task').value); }
  else { q.set('src',document.getElementById('srcSel').value);
         q.set('seed',document.getElementById('deformSeed').value||'31337');
         q.set('interval',document.getElementById('deformInterval').value||'2');
         const mn=document.getElementById('deformMin').value.trim();
         const mx=document.getElementById('deformMax').value.trim();
         if(mn) q.set('min',mn);
         if(mx) q.set('max',mx); }
  const d=await j('/api/routes?'+q); const info=document.getElementById('pvinfo');
  if(d.error){ lastPlan=null; lastWarn=''; renderCmd();
    info.innerHTML='<b style=color:#c00>预览失败：'+d.error+'</b>'+
    (d.window_hint?'<br><span style=color:#a60>'+d.window_hint+'</span>':'');
    if(leafletOK){ refLayer.setLatLngs([]); sendLayer.setLatLngs([]); }   /* 失败即清图，③同步失效 */
    return; }
  lastPlan=d.command||null; lastWarn=d.warn||''; renderCmd();
  if(d.metrics){ const m=d.metrics; const f=(v,u,s)=>v===null||v===undefined?'—':(v+(u||''));
    info.innerHTML='<table style="border-collapse:collapse;font-size:13px">'+
      '<tr style="background:#eef3f8"><th style="padding:3px 10px">参数</th><th style="padding:3px 10px">参考原轨迹</th>'+
      '<th style="padding:3px 10px">即将发送</th></tr>'+
      '<tr><td style="padding:3px 10px">总里程</td><td style="padding:3px 10px">'+f(m.ref_len_km,' km')+
      '</td><td style="padding:3px 10px"><b>'+f(m.send_len_km,' km')+'</b></td></tr>'+
      '<tr><td style="padding:3px 10px">配速</td><td style="padding:3px 10px">'+f(m.ref_pace,' min/km')+
      '</td><td style="padding:3px 10px"><b>'+f(m.send_pace,' min/km')+'</b></td></tr>'+
      '<tr><td style="padding:3px 10px">步频</td><td style="padding:3px 10px">'+f(m.ref_cadence,' spm')+
      '</td><td style="padding:3px 10px"><b>'+f(m.send_cadence,' spm')+'</b></td></tr>'+
      '</table>'+
      '<div style="font-size:12px;color:#567;margin-top:4px">'+m.points+' 点 ｜ 间隔 '+m.interval_s+
      's ｜ 每点 '+m.step_m+' m ｜ 几何弦长 '+m.chord_km+' km ｜ '+d.send_name+
      (m.window?' ｜ 里程区间 ['+m.window[0]+', '+m.window[1]+']':'')+'</div>'+
      (d.warn?'<div class="gate-warn">'+d.warn+'</div>':'');
  } else {
    info.innerHTML='<b>参考</b> '+d.ref_name+'　→　<b style=color:#2379f6>即将发送</b> '+d.send_name+
                   '　≈'+d.send_km+' km，'+d.send.length+' 点'+
                   (d.note?'　<b style=color:#a60>［'+d.note+'］</b>':'')+
                   (d.warn?'<div class="gate-warn">'+d.warn+'</div>':'');
  }
  if(!leafletOK){ info.innerHTML+='<br><b>Leaflet CDN 未能加载（离线？），地图不可用</b>'; return; }
  refLayer.setLatLngs(d.ref.map(p=>[p[1],p[0]])); sendLayer.setLatLngs(d.send.map(p=>[p[1],p[0]]));
  if(d.ref.length){ map.fitBounds(L.latLngBounds(d.ref.map(p=>[p[1],p[0]])).pad(0.12)); }
  sendLayer.bindTooltip(d.send_name+' · '+d.send_km+' km',{sticky:true}); }

async function runMain(){ const r=await j('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({dry_run:document.getElementById('dryRun').checked})});
  if(r.error) alert(r.error); }
async function killMain(){ await j('/api/run/kill',{method:'POST'}); }
async function tick(){ try{ const d=await j('/api/log?offset='+offset);
    if(d.text){ log.value+=d.text; offset=d.offset; log.scrollTop=1e9; }
    if(d.reset&&d.text){ log.value=d.text; offset=d.offset; } }catch(e){}
  setTimeout(tick,1200); }
boot(); tick();
</script>
</body>
</html>"""


# ------------------------------------------------------------------ http glue
class H(BaseHTTPRequestHandler):
    server_version = "YunPanel/0.1"

    def _send(self, code, ctype, body):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ";charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            self._send(200, "text/html", PAGE.replace("__URL__", f"http://127.0.0.1:{PORT}/"))
        elif u.path == "/api/meta":
            self._json({"campuses": list_campuses(), "sources": list_sources()})
        elif u.path == "/api/config":
            try:
                self._json(read_config_masked())
            except Exception as e:
                self._json({"error": str(e)})
        elif u.path == "/api/routes":
            try:
                out = build_preview(q)
            except Exception as e:
                out = {"error": f"{type(e).__name__}: {e}"}
            if out.get("error"):
                _set_plan({})            # 预览失败即撤销计划：③不许再发上一次的轨迹
            else:
                _set_plan(out.get("command") or {})
            self._json(out)
        elif u.path == "/api/log":
            want = int((q.get("offset") or ["0"])[0])
            with RUN["lock"]:
                full = "".join(RUN["buf"])
                if want > len(full):          # 后端截断过 → 全量重发
                    self._json({"text": full, "offset": len(full), "reset": True})
                else:
                    self._json({"text": full[want:], "offset": len(full)})
        elif u.path == "/api/run/status":
            p = RUN["proc"]
            self._json({"running": p is not None and p.poll() is None})
        else:
            self._send(404, "text/plain", "not found")

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._json({"error": "bad json: " + str(e)})
            return
        dry = bool((body or {}).get("dry_run", True))   # 缺省按演练处理（安全侧）
        if u.path == "/api/config":
            try:
                save_config(body)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})
        elif u.path == "/api/run":
            # 只跑②登记过的计划：argv 由服务端持有，前端只能决定是否追加 --dry-run，
            # 保证"地图上看到的那条轨迹"就是被发送的那条。
            if not PLAN["argv"]:
                self._json({"error": "还没有发送计划：请先在「路线预览」点一次「刷新预览」。"})
                return
            argv = list(PLAN["argv"])
            if dry:
                argv += list(PLAN["dry"])
            self._json(run_main(argv))
        elif u.path == "/api/run/kill":
            p = RUN["proc"]
            if p is not None and p.poll() is None:
                p.kill()
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "没有正在运行的进程"})
        else:
            self._send(404, "text/plain", "not found")

    def log_message(self, fmt, *args):   # 安静模式，避免污染控制台
        pass


PORT = 8080


def main():
    global PORT
    # 说明文字写死：__doc__ 会随文件头整理而消失（本文件已无模块 docstring），
    # 用 __doc__.splitlines()[0] 会直接 AttributeError 导致面板起不来。
    ap = argparse.ArgumentParser(description="云运动脚本本地网页面板（信息配置 / 路线预览 / 执行日志）")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    port = args.port
    for _ in range(12):                     # 端口占用则顺延，最多试 12 个
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), H)
            break
        except OSError:
            port += 1
    else:
        sys.exit("没有可用端口")
    PORT = port
    url = f"http://127.0.0.1:{port}/"
    print(f"[web.py] 云运动本地面板已启动：{url}   （Ctrl+C 退出）")
    print(f"[web.py] 再次打开浏览器可 Win+R 重跑本命令，或直接点上面的地址")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()

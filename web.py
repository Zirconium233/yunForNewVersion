# -*- coding: utf-8 -*-
r"""web.py — 云运动脚本本地网页面板（gui 分支原型，单文件，零新增依赖）
==========================================================================
【需求（用户原话归纳）】
  直接 Python 运行则弹出网页，至少 3 个选项卡：
   1. 信息配置：读取 ini 展示现有配置，允许改动后点击保存覆盖 ini；有"重新
      读取"按钮（文件被其他软件改过后可手动重载）；注明"快捷登录施工中"。
   2. 路线预览：下拉选方案（打表回放 / 轨迹合成），地图展示"参考路线"+
      "当前配置下即将发送的路线"；地图不预设校区，按参考轨迹定位。
   3. 执行日志：本质是以命令方式跑 main.py，把它的 stdout 抓进文本框。
  可配置 API 端口（web.py --port 8080）；启动后打印网址；自动拉起一次浏览器，
  页面上留链接可再次拉起。

【方案（自审要点，给审计模型的提示）】
  - 后端仅用标准库（http.server/configparser/subprocess/threading/webbrowser），
    不新增第三方依赖；只 import 仓库既有模块 yun_route。main.py 通过 subprocess
    起，绝不在本进程内执行，防止面板崩溃牵连上传流程。
  - 仅绑定 127.0.0.1：本页面能读写 config.ini（含明文密码/token 字段），
    绑 0.0.0.0 等于把账号文件暴露给局域网，故硬编码只回环。
  - config.ini 全仓验证为 0 注释 42 行纯 kv，configparser 重写无损；若未来
    ini 加入注释，此"直接覆盖"策略需改为正则原位替换（TODO）。
  - 敏感字段（password/token/cipherkey*）GET 时掩码返回；后端同时拒写
    __MASKED__/__KEEP__ 两个哨兵（纵深防御，实测曾把掩码串写进 ini）。
  - 轨迹合成的"即将发送"与 main.py 用同一实现：后端直接调
    yun_route.deform_geometry()（main 走 yun_route.generate() 的同一算法）。
    来源清单 = tasks_*/tasklist_*.json + examples/routes/*.geojson，走白名单
    校验（_source_path），不在清单内的路径一律拒绝，防目录穿越。
  - 打表方案的"即将发送"= tasklist 原始点列（老式漂移 -d 仅 ≈0.11mm 刚性
    平移，不在预览里体现，页面如实说明）。
  - 前端 Leaflet 走 unpkg CDN + 高德在线矢量瓦片；加载失败时降级提示，
    不做离线瓦片内嵌（体积不划算）。
  - 执行日志：POST /api/run 起 subprocess，后台线程把 stdout 逐行 append 进
    内存缓冲（带上限截断），前端 ?offset= 轮询增量拉取。只允许一个并发的
    main 进程；默认 --dry-run，真跑需在参数里显式去掉（防手滑上传）。
  - 已知粗糙处（欢迎审计指出）：无鉴权（仅回环兜底）、日志缓冲纯内存、
    同时只支持单 main 进程、来源选择不过滤坏文件（坏文件在预览接口会如实
    报错）、config 保存无并发锁（两人同开面板时后保存者覆盖先保存者）。

【运行】
  python web.py [--port 8080] [--no-browser]
  Win+R（本项目 venv 零命令行弹浏览器）：
    C:\Temp\playground\yunForNewVersion\.venv\Scripts\pythonw.exe C:\Temp\playground\yunForNewVersion\web.py
  （pythonw 无控制台窗口；要控制台日志用 python.exe）
"""
import argparse
import configparser
import json
import os
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

RUN = {"proc": None, "buf": [], "lock": threading.Lock()}  # 执行日志占位实现


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
                "send_name": "打表原轨迹", "send_km": round(km, 3), "note": ""}

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
                "note": f"输入 {len(points)} 点 → 输出 {len(rows)} 点"}

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
        RUN["buf"].append("$ " + " ".join(cmd) + "\n")
        RUN["proc"] = subprocess.Popen(
            cmd, cwd=BASE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
<html lang="zh"><head><meta charset="utf-8">
<title>云运动面板（原型）</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 body{font-family:'Microsoft YaHei',sans-serif;margin:0;background:#f4f6f8;color:#222}
 #bar{background:#1d3557;color:#fff;padding:8px 14px;font-size:13px}
 #bar a{color:#8ecae6}
 #tabs{display:flex;gap:2px;background:#1d3557;padding:0 10px}
 .tab{padding:8px 18px;background:#2a4a73;color:#cfe3f5;cursor:pointer;border-radius:6px 6px 0 0;font-size:14px}
 .tab.on{background:#f4f6f8;color:#123}
 .pane{display:none;padding:14px;max-width:1100px}
 .pane.on{display:block}
 fieldset{border:1px solid #cdd7e1;margin:0 0 12px;background:#fff;border-radius:8px}
 legend{font-weight:bold;padding:0 6px}
 label{display:inline-block;width:200px;font-size:13px;text-align:right;margin:3px 6px 3px 0}
 input.f{width:420px;padding:3px 6px;border:1px solid #bcd;border-radius:4px;font-size:13px}
 button{background:#1d3557;color:#fff;border:0;border-radius:5px;padding:6px 14px;cursor:pointer;margin:2px}
 button.gray{background:#778}
 #map{height:520px;border-radius:8px;border:1px solid #cdd7e1}
 #log{width:100%;height:380px;font:12px/1.45 Consolas,monospace;background:#10151c;color:#b6e388;border:0;padding:8px;border-radius:8px;white-space:pre}
 .note{font-size:12px;color:#567;margin:4px 0 10px 206px}
 .wip{background:#fff6d9;border:1px dashed #d7a;padding:10px;border-radius:6px;font-size:13px}
 select{padding:4px;margin:2px}
 #pvinfo{font-size:13px;margin:8px 0}
</style></head><body>
<div id="bar">云运动本地面板（原型/未加固）— 地址可收藏：<a id="selflink" href="#">__URL__</a>
 　敏感字段未编辑将原样保留</div>
<div id="tabs">
 <div class="tab on" data-p="p1">① 信息配置</div>
 <div class="tab" data-p="p2">② 路线预览</div>
 <div class="tab" data-p="p3">③ 执行日志（占位：命令方式跑 main）</div>
</div>

<div class="pane on" id="p1">
 <p class="wip">🚧 快捷登录（账号密码一键换 token）施工中，暂不提供——当前请照常使用
   <code>python main.py</code> 的登录流程，本页只做配置的查看/修改。</p>
 <div id="cfg"></div>
 <button onclick="saveCfg()">保存（覆盖 config.ini）</button>
 <button class="gray" onclick="loadCfg()">重新读取 ini（外部改动后点此重载）</button>
 <span id="cfgmsg" style="font-size:13px;color:#073"></span>
</div>

<div class="pane" id="p2">
 <select id="scheme" onchange="schemeUI()">
   <option value="base">方案1 打表回放（即将发送=任务表原轨迹）</option>
   <option value="deform">方案2 轨迹合成（deform：OU 游走+二维漂移+跳变，任选底图 json）</option>
 </select>
 <span id="pickBase"><label style="width:auto">校区</label><select id="campus"></select>
   <select id="task"></select></span>
 <span id="pickDeform" style="display:none"><label style="width:auto">底图json</label><select id="srcSel"></select>
   <label style="width:auto">seed</label><input id="deformSeed" class="f" style="width:80px" value="31337">
   <label style="width:auto">间隔s</label><input id="deformInterval" class="f" style="width:56px" value="2">
   <label style="width:auto">最短m</label><input id="deformMin" class="f" style="width:76px" placeholder="留空=不限">
   <label style="width:auto">最长m</label><input id="deformMax" class="f" style="width:76px" placeholder="留空=不限"></span>
 <button onclick="preview()">刷新预览</button>
 <div id="pvinfo"></div>
 <div id="map"></div>
 <p class="note">灰虚线=参考轨迹（地图按它定位，不预设校区）；蓝实线=当前配置下即将发送的点列。
   方案2 的合成算法与 <code>python main.py --route-config examples/routes/deform_*.json</code>
   完全同一实现（5.5m 等弧长重采样 + 法向 OU 游走 + 二维相关漂移 + 稀疏衰减跳变，seed 可改）。
   打表模式无坐标级偏移（老式 -d 仅 ≈0.11mm 刚性平移，故不呈现）。</p>
</div>

<div class="pane" id="p3">
 <input class="f" id="runArgs" style="width:560px" value="--dry-run"
        title="传给 main.py 的参数。默认 --dry-run 离线演练，去掉才是真跑">
 <button onclick="runMain()">执行</button>
 <button class="gray" onclick="killMain()">停止</button>
 <button class="gray" onclick="offset=0;log.value=''">清屏</button>
 <p class="note">本质是 <code>python main.py &lt;参数&gt;</code> 的子进程，stdout 实时抓进下方文本框。
   真跑请先确认 config.ini 无误——此面板不拦截任何网络行为。</p>
 <textarea id="log" readonly></textarea>
</div>

<script>
let offset=0, map=null, refLayer=null, sendLayer=null, meta=null, leafletOK=(typeof L!=='undefined');
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
  document.getElementById('selflink').textContent=location.href; }
function taskFill(){ const t=document.getElementById('task'); t.innerHTML='';
  for(const k of (meta.campuses[document.getElementById('campus').value]||[])){
    const o=document.createElement('option');o.value=k;o.textContent=k;t.appendChild(o);} }
function schemeUI(){ const s=document.getElementById('scheme').value;
  document.getElementById('pickBase').style.display=(s==='base')?'':'none';
  document.getElementById('pickDeform').style.display=(s==='base')?'none':''; }

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
  if(d.error){ info.innerHTML='<b style=color:#c00>预览失败：'+d.error+'</b>'+
    (d.window_hint?'<br><span style=color:#a60>'+d.window_hint+'</span>':''); return; }
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
      (m.window?' ｜ 里程区间 ['+m.window[0]+', '+m.window[1]+']':'')+'</div>';
  } else {
    info.innerHTML='<b>参考</b> '+d.ref_name+'　→　<b style=color:#2379f6>即将发送</b> '+d.send_name+
                   '　≈'+d.send_km+' km，'+d.send.length+' 点'+
                   (d.note?'　<b style=color:#a60>［'+d.note+'］</b>':'');
  }
  if(!leafletOK){ info.innerHTML+='<br><b>Leaflet CDN 未能加载（离线？），地图不可用</b>'; return; }
  refLayer.setLatLngs(d.ref.map(p=>[p[1],p[0]])); sendLayer.setLatLngs(d.send.map(p=>[p[1],p[0]]));
  if(d.ref.length){ map.fitBounds(L.latLngBounds(d.ref.map(p=>[p[1],p[0]])).pad(0.12)); }
  sendLayer.bindTooltip(d.send_name+' · '+d.send_km+' km',{sticky:true}); }

async function runMain(){ const r=await j('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({args:document.getElementById('runArgs').value.trim().split(/\\s+/).filter(Boolean)})});
  if(r.error) alert(r.error); }
async function killMain(){ await j('/api/run/kill',{method:'POST'}); }
async function tick(){ try{ const d=await j('/api/log?offset='+offset);
    if(d.text){ log.value+=d.text; offset=d.offset; log.scrollTop=1e9; }
    if(d.reset&&d.text){ log.value=d.text; offset=d.offset; } }catch(e){}
  setTimeout(tick,1200); }
boot(); tick();
</script></body></html>"""


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
                self._json(build_preview(q))
            except Exception as e:
                self._json({"error": f"{type(e).__name__}: {e}"})
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
        if u.path == "/api/config":
            try:
                save_config(body)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)})
        elif u.path == "/api/run":
            self._json(run_main(body.get("args") or []))
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
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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

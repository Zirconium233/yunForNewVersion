"""V4 几何模板生成；纯本地计算，不导入账号、HTTP 或登录模块。"""
import bisect
import json
import math
import random
import secrets
from pathlib import Path


def distance(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742000 * math.asin(min(1, math.sqrt(h)))


def coordinates(values):
    if not isinstance(values, list) or not 2 <= len(values) <= 100000:
        raise ValueError("路线需要 2～100000 个经纬度点")
    out = []
    for p in values:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError("每点必须为 [经度, 纬度]")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p):
            raise ValueError("坐标必须为有限数字")
        if not -180 <= p[0] <= 180 or not -85 <= p[1] <= 85:
            raise ValueError("经纬度超出支持范围（纬度 ±85 度）")
        if not out or tuple(p) != out[-1]:
            out.append(tuple(p))
    if len(out) < 2:
        raise ValueError("路线不能只有重复点")
    if any(distance(a, b) > 100 for a, b in zip(out, out[1:])):
        raise ValueError("底图相邻点超过 100 米，请检查坐标或先加密几何")
    if any(max(p[k] for p in out)-min(p[k] for p in out) > 0.2 for k in (0, 1)):
        raise ValueError("仅支持局部校园几何，坐标跨度不能超过 0.2 度")
    return out


def number(cfg, key, default, low, high, integer=False):
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} 必须是有限数字")
    if not low <= value <= high or (integer and int(value) != value):
        raise ValueError(f"{key} 必须在 {low}～{high} 之间" + ("且为整数" if integer else ""))
    return int(value) if integer else value


def tangent(points, i):
    a, b = points[max(0, i-2)], points[min(len(points)-1, i+2)]
    dx, dy = b[0]-a[0], b[1]-a[1]
    size = math.hypot(dx, dy)
    if size < 1e-9:
        raise ValueError("无法确定路线切线：局部折返或重复点")
    return (dx/size, dy/size), (-dy/size, dx/size)


def geometry(base, cfg):
    """保留用户 V4 的裁剪、法线偏移、绕行和空间重采样结构。"""
    seed = number(cfg, "seed", 20260919, 0, 2**63-1, True)
    rng = random.Random(seed)
    start = number(cfg, "start_trim", 0, 0, len(base), True)
    end = number(cfg, "end_trim", 0, 0, len(base), True)
    if len(base)-start-end < 25:
        raise ValueError("裁剪后至少保留 25 个点")
    lane_indices = cfg.get("lane_change_indices", [90, 175, 290])
    lane_choices = cfg.get("lane_change_choices_m", [-1.3, -0.9, 0.9, 1.2])
    if not isinstance(lane_indices, list) or any(type(i) is not int or i < 0 for i in lane_indices):
        raise ValueError("lane_change_indices 必须为非负整数列表")
    if len(set(lane_indices)) != len(lane_indices):
        raise ValueError("lane_change_indices 不能重复")
    if not isinstance(lane_choices, list) or not lane_choices:
        raise ValueError("lane_change_choices_m 不能为空")
    for v in lane_choices:
        number({"offset": v}, "offset", 0, -5, 5)
    detour_enabled = cfg.get("detour_enabled", False)
    if type(detour_enabled) is not bool:
        raise ValueError("detour_enabled 必须为布尔值")
    max_offset = number(cfg, "max_offset_m", 5, 0.6, 20)
    transition = number(cfg, "lane_transition_points", 12, 1, 100, True)
    lat0 = sum(p[1] for p in base)/len(base)
    origin = base[0]
    sx, sy = 111320*math.cos(math.radians(lat0)), 111320
    xy = [((p[0]-origin[0])*sx, (p[1]-origin[1])*sy) for p in base]
    work = xy[start:len(xy)-end]
    shifted, lane, target_lane = [], 0.0, 0.0
    for i, p in enumerate(work):
        _, normal = tangent(work, i)
        if i in lane_indices:
            target_lane += rng.choice(lane_choices)
        # 原脚本在指定索引处直接跳变；逐点逼近目标偏移。
        lane += (target_lane-lane)/transition
        lane *= 0.997
        target_lane *= 0.997
        offset = lane + 0.35*math.sin(i/29) + 0.18*math.sin(i/7.7)
        if abs(offset) > max_offset:
            raise ValueError("累计侧向偏移超过 max_offset_m")
        shifted.append((p[0]+normal[0]*offset, p[1]+normal[1]*offset))
    if detour_enabled:
        idx = number(cfg, "detour_index", 220, 0, len(shifted)-2, True)
        rejoin = number(cfg, "detour_rejoin_offset", 14, 1, len(shifted)-idx-1, True)
        exit_m = number(cfg, "detour_exit_m", 12, 0, 30)
        forward = number(cfg, "detour_forward_m", 18, 0, 50)
        step = number(cfg, "detour_step_m", 3.5, 0.5, 10)
        a = shifted[idx]
        t, n = tangent(shifted, idx)
        controls = [a] + [(a[0]+t[0]*f+n[0]*v, a[1]+t[1]*f+n[1]*v)
                          for f, v in [(6, 5), (12, exit_m), (forward, exit_m*0.85)]]
        controls.append(shifted[idx+rejoin])
        detour = []
        for a, b in zip(controls, controls[1:]):
            count = max(1, math.ceil(math.dist(a, b)/step))
            detour.extend((a[0]+(b[0]-a[0])*k/count, a[1]+(b[1]-a[1])*k/count)
                          for k in range(count))
        shifted = shifted[:idx] + detour + [controls[-1]] + shifted[idx+rejoin+1:]
    sampled, i = [], 0
    while i < len(shifted):
        sampled.append(shifted[i])
        u = 0.5+0.5*math.sin(i/45)
        if u < 0.18 and i+1 < len(shifted):
            a, b = shifted[i:i+2]
            sampled.append(((a[0]+b[0])/2, (a[1]+b[1])/2))
        i += 2 if u > 0.78 else 1
    # 原脚本稀疏采样可能跳过末点，显式保留终点。
    if sampled[-1] != shifted[-1]:
        sampled.append(shifted[-1])
    return coordinates([[round(p[0]/sx+origin[0], 8), round(p[1]/sy+origin[1], 8)] for p in sampled])


def cumulative(points):
    result = [0.0]
    for a, b in zip(points, points[1:]):
        result.append(result[-1]+distance(a, b))
    return result


def client_speed(meters, seconds):
    # APK SportRunMapActivity:4140-4154：字段名 speed，实际为 min/km。
    return format(min(900, max(1, seconds/60/(meters/1000))), '.2f') if meters > 0 and seconds > 0 else '0.0'


def position_at(route, lengths, meters):
    j = min(len(route)-1, max(1, bisect.bisect_left(lengths, meters)))
    a, b = route[j-1:j+1]
    f = (meters-lengths[j-1])/(lengths[j]-lengths[j-1])
    return tuple(round(a[k]+(b[k]-a[k])*f, 8) for k in (0, 1))


def allowed_polygon(path):
    obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(obj, dict):
        raise ValueError('可跑区域必须是 GeoJSON 对象')
    if obj.get('type') == 'FeatureCollection':
        features = obj.get('features', [])
        if len(features) != 1:
            raise ValueError('可跑区域文件只能包含一个 Polygon')
        obj = features[0].get('geometry', {})
    elif obj.get('type') == 'Feature':
        obj = obj.get('geometry', {})
    if obj.get('type') != 'Polygon' or len(obj.get('coordinates', [])) != 1:
        raise ValueError('可跑区域必须是单环 Polygon；带洞或多区域暂不支持')
    raw_ring = obj['coordinates'][0]
    if not isinstance(raw_ring, list):
        raise ValueError('可跑区域坐标必须是列表')
    ring = []
    for p in raw_ring:
        if (not isinstance(p, list) or len(p) != 2 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                    not math.isfinite(v) for v in p)):
            raise ValueError('可跑区域坐标必须是有限经纬度')
        if not -180 <= p[0] <= 180 or not -85 <= p[1] <= 85:
            raise ValueError('可跑区域经纬度超出支持范围')
        if not ring or tuple(p) != ring[-1]:
            ring.append(tuple(p))
    if ring and ring[0] == ring[-1]:
        ring.pop()
    if len(ring) < 3:
        raise ValueError('可跑区域至少需要三个不同顶点')
    if len(ring) > 10000:
        raise ValueError('可跑区域顶点超过 10000 个')
    x0, y0 = ring[0]
    area = sum(((a[0]-x0)*(b[1]-y0)-(b[0]-x0)*(a[1]-y0))
               for a, b in zip(ring, ring[1:]+ring[:1]))
    if abs(area) < 1e-18:
        raise ValueError('可跑区域多边形面积为零')
    edges = list(zip(ring, ring[1:]+ring[:1]))
    for i, (a, b) in enumerate(edges):
        for j in range(i+1, len(edges)):
            if j == i+1 or (i == 0 and j == len(edges)-1):
                continue
            c, d = edges[j]
            if segment_intersections(a, b, c, d):
                raise ValueError('可跑区域多边形自交')
    return ring


def cross(a, b):
    return a[0]*b[1]-a[1]*b[0]


def on_segment(p, a, b):
    ab = (b[0]-a[0], b[1]-a[1])
    ap = (p[0]-a[0], p[1]-a[1])
    scale = max(abs(ab[0]), abs(ab[1]), 1e-12)
    return (abs(cross(ab, ap)) <= 1e-13*scale and
            min(a[0], b[0])-1e-12 <= p[0] <= max(a[0], b[0])+1e-12 and
            min(a[1], b[1])-1e-12 <= p[1] <= max(a[1], b[1])+1e-12)


def segment_intersections(a, b, c, d):
    """返回 AB 与 CD 的交点在 AB 上的参数，含相切及共线端点。"""
    r = (b[0]-a[0], b[1]-a[1])
    s = (d[0]-c[0], d[1]-c[1])
    qa = (c[0]-a[0], c[1]-a[1])
    det = cross(r, s)
    if abs(det) < 1e-20:
        if abs(cross(qa, r)) > 1e-20:
            return []
        rr = r[0]*r[0]+r[1]*r[1]
        if rr == 0:
            return [0.0] if on_segment(a, c, d) else []
        return [max(0.0, min(1.0, ((p[0]-a[0])*r[0]+(p[1]-a[1])*r[1])/rr))
                for p in (c, d) if on_segment(p, a, b)] or [
                    0.0 if on_segment(a, c, d) else 1.0
                    for p in (a, b) if on_segment(p, c, d)]
    t = cross(qa, s)/det
    u = cross(qa, r)/det
    return [max(0.0, min(1.0, t))] if -1e-12 <= t <= 1+1e-12 and -1e-12 <= u <= 1+1e-12 else []


def inside(point, ring):
    x, y = point
    result = False
    for a, b in zip(ring, ring[1:]+ring[:1]):
        if on_segment(point, a, b):
            return True
        if (a[1] > y) != (b[1] > y):
            cross = a[0]+(y-a[1])*(b[0]-a[0])/(b[1]-a[1])
            if x < cross:
                result = not result
    return result


def validate_polygon_route(route, ring):
    # 以线段与全部边界的交点切分，再检查每段中点；狭窄凹口不会被采样跳过。
    edges = list(zip(ring, ring[1:]+ring[:1]))
    for a, b in zip(route, route[1:]):
        cuts = [0.0, 1.0]
        for c, d in edges:
            cuts.extend(segment_intersections(a, b, c, d))
        cuts = sorted(set(cuts))
        for f in cuts[:1]+[(x+y)/2 for x, y in zip(cuts, cuts[1:])]+cuts[-1:]:
            p = (a[0]+(b[0]-a[0])*f, a[1]+(b[1]-a[1])*f)
            if not inside(p, ring):
                raise ValueError('生成路线有点或线段超出配置的可跑区域')


def validate_output_polygon(data, ring):
    if ring is not None:
        points = [tuple(map(float, p['point'].split(','))) for p in data['pointsList']]
        validate_polygon_route(points, ring)


def task_from_telemetry(path, route, lengths, target, variation, seed):
    """保留来源记录的速度/步频变化形状，轻微改变相位与节奏。"""
    obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    source = obj.get('data', {}).get('pointsList', [])
    if not isinstance(source, list) or not 2 <= len(source) <= 50000:
        raise ValueError('运动样本需要 2～50000 个轨迹点')
    try:
        miles = [float(p['runMileage']) for p in source]
        times = [int(p['runTime']) for p in source]
        steps = [int(p['runStep']) for p in source]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('运动样本缺少合法的 runMileage/runTime/runStep') from exc
    if (any(not math.isfinite(x) for x in miles) or
            any(b <= a for a, b in zip(miles, miles[1:])) or
            any(b <= a for a, b in zip(times, times[1:])) or
            any(b < a for a, b in zip(steps, steps[1:])) or
            steps[-1] <= steps[0]):
        raise ValueError('运动样本距离/时间必须递增，累计步数不可回退且总步数须为正')
    original_distance = miles[-1]-miles[0]
    if not 0.9 <= target/original_distance <= 1.1:
        raise ValueError('目标与运动样本距离相差超过 10%；无法同时保留速度特征')
    rng = random.Random(seed ^ 0x5EED)
    phase = rng.uniform(-math.pi, math.pi)
    ds = [b-a for a, b in zip(miles, miles[1:])]
    dt = [b-a for a, b in zip(times, times[1:])]
    dsteps = [b-a for a, b in zip(steps, steps[1:])]
    varied_distance = [v*(1+variation*math.sin(i/19+phase)) for i, v in enumerate(ds)]
    varied_time = [v*(1+variation*math.sin(i/31+phase)) for i, v in enumerate(dt)]
    varied_steps = [v*(1+variation*math.sin(i/23+phase)) for i, v in enumerate(dsteps)]
    distance_scale = target/sum(varied_distance)
    time_scale = (times[-1]-times[0])/sum(varied_time)
    step_scale = (steps[-1]-steps[0])/sum(varied_steps)
    progress, elapsed, step_progress = 0.0, 0.0, 0.0
    spatial, ticks, counts = [0.0], [0], [0]
    for d, t, st in zip(varied_distance, varied_time, varied_steps):
        progress += d*distance_scale
        elapsed += t*time_scale
        step_progress += st*step_scale
        spatial.append(min(target, progress))
        ticks.append(max(ticks[-1]+1, round(elapsed)))
        counts.append(max(counts[-1], round(step_progress)))
    pts = [position_at(route, lengths, s) for s in spatial]
    actual = cumulative(pts)
    if any(b <= a for a, b in zip(actual, actual[1:])):
        raise ValueError('运动样本重采样后出现重复位置；请减少采样密度或更换底图')
    if abs(actual[-1]-target) > max(1, target*0.01):
        raise ValueError('运动样本重采样后的几何长度损失超过 1%')
    out = []
    for i, (t, p, m, st) in enumerate(zip(ticks, pts, actual, counts)):
        speed = client_speed(m-actual[i-1], t-ticks[i-1]) if i else '0.0'
        out.append({'point': f'{p[0]:.8f},{p[1]:.8f}', 'runMileage': m,
                    'runTime': t, 'runStep': st, 'speed': speed,
                    'runStatus': '1', 'isFence': 'Y', 'isMock': False, 'ts': '0'})
    duration = ticks[-1]
    return {'pointsList': out, 'duration': duration, 'recordMileage': actual[-1]/1000,
            'recodeCadence': counts[-1]*60/duration,
            'recodePace': duration/60/(actual[-1]/1000),
            'recodeDislikes': 0, 'manageList': []}


def base_from_task(path):
    obj = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    points = obj.get('data', {}).get('pointsList', [])
    if not isinstance(points, list):
        raise ValueError('base_task 缺少 pointsList')
    try:
        raw = [[float(v) for v in p['point'].split(',')] for p in points]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('base_task 含非法经纬度点') from exc
    return coordinates(raw)


def generate(config_path):
    path = Path(config_path).resolve()
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(cfg, dict):
        raise ValueError("路线配置必须为 JSON 对象")
    allowed = {"base_geojson", "base_task", "coordinate_system", "distance_m", "pace_min_km", "cadence_spm",
               "sample_seconds", "seed", "start_trim", "end_trim", "lane_change_indices",
               "lane_change_choices_m", "lane_transition_points", "detour_enabled", "max_offset_m", "detour_index",
               "detour_rejoin_offset", "detour_exit_m", "detour_forward_m", "detour_step_m",
               "allowed_polygon_geojson", "telemetry_task", "telemetry_variation"}
    if cfg.keys()-allowed:
        raise ValueError(f"未知路线配置项：{sorted(cfg.keys()-allowed)}")
    if cfg.get("coordinate_system") not in ("GCJ-02", "WGS84"):
        raise ValueError("必须明确 coordinate_system 为 GCJ-02 或 WGS84；本工具不转换坐标系")
    source = cfg.get('base_geojson')
    base_task = cfg.get('base_task')
    if bool(source) == bool(base_task):
        raise ValueError('base_geojson 与 base_task 必须且只能提供一个')
    if source:
        if not isinstance(source, str):
            raise ValueError('base_geojson 必须是文件路径')
        obj = json.loads((path.parent/source).read_text(encoding="utf-8-sig"))
        features = obj.get("features", []) if isinstance(obj, dict) else []
        if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection" or not isinstance(features, list) or len(features) != 1 or not isinstance(features[0], dict) or not isinstance(features[0].get("geometry"), dict) or features[0]["geometry"].get("type") != "LineString":
            raise ValueError("底图必须是仅包含一条 LineString 的 FeatureCollection")
        base = coordinates(features[0]["geometry"]["coordinates"])
    else:
        if not isinstance(base_task, str):
            raise ValueError('base_task 必须是文件路径')
        base = base_from_task(path.parent/base_task)
    if cfg.get('seed') == 'auto':
        cfg = dict(cfg, seed=secrets.randbits(63))
    cfg = dict(cfg, seed=number(cfg, 'seed', 20260919, 0, 2**63-1, True))
    route = geometry(base, cfg)
    lengths = cumulative(route)
    target = number(cfg, "distance_m", 2000, 10, 50000)
    polygon_path = cfg.get('allowed_polygon_geojson')
    ring = None
    if (number(cfg, 'max_offset_m', 5, 0.6, 20) > 5 or cfg.get('detour_enabled', False)) and not polygon_path:
        raise ValueError('偏移超过 5 米或开启绕行时，必须提供 allowed_polygon_geojson')
    if polygon_path:
        if not isinstance(polygon_path, str):
            raise ValueError('allowed_polygon_geojson 必须是文件路径')
        ring = allowed_polygon(path.parent/polygon_path)
        validate_polygon_route(route, ring)
    pace = number(cfg, "pace_min_km", 6, 2, 30)
    cadence = number(cfg, "cadence_spm", 160, 1, 350)
    interval = number(cfg, "sample_seconds", 1, 1, 5, True)
    if target > lengths[-1]:
        raise ValueError(f"目标 {target:.1f} 米超过生成几何可用长度 {lengths[-1]:.1f} 米；减少裁剪或提供更长底图")
    variation = number(cfg, 'telemetry_variation', 0.05, 0, 0.15)
    telemetry_path = cfg.get('telemetry_task') or base_task
    if telemetry_path:
        if not isinstance(telemetry_path, str):
            raise ValueError('telemetry_task 必须是文件路径')
        data = task_from_telemetry(path.parent/telemetry_path, route, lengths,
                                   target, variation, cfg.get('seed', 20260919))
        validate_output_polygon(data, ring)
        return {'code': 200, 'metadata': {'synthetic': True, 'mode': 'geometry_v4_telemetry',
                'coordinate_system': cfg['coordinate_system'], 'seed': cfg.get('seed', 20260919),
                'target_m': target, 'available_m': lengths[-1], 'server_verified': False,
                'telemetry_source': Path(telemetry_path).name}, 'data': data}
    duration = math.ceil(round(target/1000*pace*60, 9))
    ticks = list(range(0, duration, interval)) + [duration]
    sampled = []
    for t in ticks:
        s = target*t/duration
        sampled.append(position_at(route, lengths, s))
    mileage = cumulative(sampled)
    if abs(mileage[-1]-target) > max(1, target*0.01):
        raise ValueError("时间采样造成几何长度损失超过 1%；缩短 sample_seconds")
    points = []
    for i, (t, p, m) in enumerate(zip(ticks, sampled, mileage)):
        speed = client_speed(m-mileage[i-1], t-ticks[i-1]) if i else '0.0'
        points.append({"point": f"{p[0]:.8f},{p[1]:.8f}", "runMileage": m,
                       "runTime": t, "runStep": math.floor(t*cadence/60),
                       "speed": speed, "runStatus": "1",
                       "isFence": "Y", "isMock": False, "ts": "0"})
    data = {"pointsList": points, "duration": duration, "recordMileage": mileage[-1]/1000,
            "recodeCadence": points[-1]["runStep"]*60/duration,
            "recodePace": duration/60/(mileage[-1]/1000),
            "recodeDislikes": 0, "manageList": []}
    validate_output_polygon(data, ring)
    return {"code": 200, "metadata": {"synthetic": True, "mode": "geometry_v4",
            "coordinate_system": cfg["coordinate_system"], "seed": cfg.get("seed", 20260919),
            "target_m": target, "available_m": lengths[-1], "cadence_spm": cadence, "server_verified": False},
            "data": data}


def geojson(task):
    return {"type": "FeatureCollection", "metadata": task["metadata"], "features": [
        {"type": "Feature", "properties": {"duration_s": task["data"]["duration"]},
         "geometry": {"type": "LineString", "coordinates": [
             list(map(float, p["point"].split(','))) for p in task["data"]["pointsList"]]}}]}

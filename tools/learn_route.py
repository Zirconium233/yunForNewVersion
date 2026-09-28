"""从已有 tasklist 学习跑步点位逻辑，并生成一份全新且不同的路径文件。

与 `tools/generate_route.py` 的区别：本工具不需要底图 GeoJSON，也不复制任何单条
来源轨迹。它把 `tasks_*` 目录（按校区区分）中的多条记录当作同一场地的重复观测，
学习出该场地的“点位逻辑”，再用学到的模型合成一条新的任务文件：

1. 闭环几何 —— 环形跑道中心线（弧长均匀化后做环形高斯平滑，保留真实拐角）；
2. 分圈结构 —— 单圈周长与圈数，总里程按几何累加；
3. 走廊约束 —— 各弧长位置上的侧向偏移分布，生成点必须落在观测到的可跑带内；
4. 点位逻辑 —— 围栏/踩点坐标（manageList）、标记顺序、index 的取值规律；
5. 字段协议 —— 逐点键序、JSON 类型与数值文本精度（各校区不同）；
6. 节奏模型 —— 采样间隔、配速、步频、步幅、步数累计与增量分配。

离线运行：不读取 `config.ini`、不导入 main、不发起任何网络请求。
生成结果只保证几何与协议自洽，不代表服务端接受或成绩有效。

每个 `tasks_*` 目录是一个校区的重复观测，一次只学习一个校区：把多个校区的文件
混在一起会因为场地不同而无法定标单圈长度。命令行会直接拒绝这种组合。

用法：
    # 第一步：学习校区模型（可复用，不必每次重新学习）
    python tools/learn_route.py learn --tasks tasks_fch --out work_dir/fch.model.json
    # 第二步：按模型生成新路径；--tasks 也可以直接替代 --model，顺带给出新颖度报告
    python tools/learn_route.py generate --model work_dir/fch.model.json \\
        --out work_dir/tasklist_new.json --seed auto
"""
import argparse
import json
import math
import random
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yun_route  # noqa: E402

# 协议常量：三个校区全部 7851+4527+7850 个点一致，属于本工具接受的唯一在线形态。
POINT_KEY_ORDER = ('id', 'point', 'speed', 'runStatus', 'runRecordId',
                   'runTime', 'isFence', 'runStep', 'runMileage')
TOP_KEY_ORDER = ('msg', 'code', 'data')
DATA_KEY_ORDER = ('recordMileage', 'recodePace', 'recodeCadence', 'recodeDislikes',
                  'duration', 'pointsList', 'schoolId', 'manageList')
# 逐点里程的文本精度不能低于此值：来源里 tasks_xc 的 runMileage 全为整数文本
# （5 米栅格），照抄会让逐点里程失去分辨率，speed 字段与里程差不再自洽。
MIN_MILEAGE_TEXT_PRECISION = 6
SPEED_LOW, SPEED_HIGH = 1.0, 900.0
MAX_STEP_M = 100.0         # 与 yun_route.coordinates 的上限一致，留作稀疏采样判据
MAX_SPIKE_M = 25.0         # 正常点距上限约 15 米；超过此值的孤立点是跳变，丢弃
MAX_SPAN_DEG = 0.2
FENCE_REACH_M = 20.0       # 实测：marked=Y 的踩点最大 19.5 米，marked=N 最小 20.1 米
RATIO_LOW = 0.85           # 上报里程 / 轨迹弧长的自洽区间
RATIO_HIGH = 1.15
CAMPUS_SPREAD_M = 500.0    # 同一校区各文件轨迹中心的最大间距
MODEL_VERSION = 1


class LearnError(ValueError):
    """输入数据不满足学习前提；信息面向使用者，可直接阅读。"""


# --------------------------------------------------------------------------- #
# 基础几何
# --------------------------------------------------------------------------- #

def project_factory(origin):
    """返回经纬度 -> 本地米制平面的投影函数（等距圆柱，校园尺度足够）。"""
    lon0, lat0 = origin
    scale_x = 111320 * math.cos(math.radians(lat0))
    return lambda p: ((p[0] - lon0) * scale_x, (p[1] - lat0) * 111320)


def unproject_factory(origin):
    lon0, lat0 = origin
    scale_x = 111320 * math.cos(math.radians(lat0))
    return lambda p: (p[0] / scale_x + lon0, p[1] / 111320 + lat0)


def arc_lengths(points, closed=False):
    seq = list(points) + [points[0]] if closed else list(points)
    out = [0.0]
    for a, b in zip(seq, seq[1:]):
        out.append(out[-1] + math.dist(a, b))
    return out


def resample_closed(points, count):
    """按弧长把闭环重采成 count 个等距点；返回 (点列, 周长)。"""
    if len(points) < 3:
        raise LearnError('闭环至少需要三个点')
    ring = list(points) + [points[0]]
    lens = arc_lengths(list(points), closed=True)
    total = lens[-1]
    if total <= 0:
        raise LearnError('闭环周长为零')
    out, j = [], 0
    for k in range(count):
        target = total * k / count
        while j < len(lens) - 2 and lens[j + 1] < target:
            j += 1
        span = lens[j + 1] - lens[j]
        frac = 0.0 if span == 0 else (target - lens[j]) / span
        a, b = ring[j], ring[j + 1]
        out.append((a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac))
    return out, total


def smooth_closed(ring, sigma):
    """环形高斯平滑；sigma 以采样点为单位。1 附近只压高频抖动，不动拐角。"""
    n = len(ring)
    if sigma <= 0 or n < 5:
        return list(ring)
    half = max(1, int(math.ceil(3 * sigma)))
    weights = [math.exp(-(k * k) / (2 * sigma * sigma)) for k in range(-half, half + 1)]
    norm = sum(weights)
    out = []
    for i in range(n):
        sx = sy = 0.0
        for k, w in zip(range(-half, half + 1), weights):
            p = ring[(i + k) % n]
            sx += p[0] * w
            sy += p[1] * w
        out.append((sx / norm, sy / norm))
    return out


def vertex_normals(ring):
    """闭环逐点左法线（单位向量）。"""
    n = len(ring)
    out = []
    for i in range(n):
        a, b = ring[(i - 2) % n], ring[(i + 2) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        size = math.hypot(dx, dy)
        if size < 1e-9:
            raise LearnError('中心线存在折返或重复点，无法确定切线')
        out.append((-dy / size, dx / size))
    return out


def jitter_of(points, window=2):
    """高频抖动强度：各点到 5 点滑动平均的位置偏差。"""
    n = len(points)
    if n < 2 * window + 1:
        return 0.0
    devs = []
    for i in range(window, n - window):
        avg = (sum(p[0] for p in points[i - window:i + window + 1]) / (2 * window + 1),
               sum(p[1] for p in points[i - window:i + window + 1]) / (2 * window + 1))
        devs.append(math.dist(points[i], avg))
    devs.sort()
    return devs[len(devs) // 2]


class Grid:
    """均匀网格最近点索引；广场尺度下比逐点扫描快几个数量级。"""

    def __init__(self, points, cell=5.0):
        self.cell = cell
        self.points = points
        self.buckets = {}
        for i, (x, y) in enumerate(points):
            self.buckets.setdefault((int(x // cell), int(y // cell)), []).append(i)

    def nearest(self, x, y):
        cx, cy = int(x // self.cell), int(y // self.cell)
        best, best_d, r = -1, float('inf'), 0
        while r < 500:
            for gx in range(cx - r, cx + r + 1):
                for gy in range(cy - r, cy + r + 1):
                    if r and max(abs(gx - cx), abs(gy - cy)) != r:
                        continue
                    for i in self.buckets.get((gx, gy), ()):
                        px, py = self.points[i]
                        d = (px - x) ** 2 + (py - y) ** 2
                        if d < best_d:
                            best, best_d = i, d
            if best >= 0 and (r * self.cell) ** 2 > best_d:
                break
            r += 1
        if best < 0:
            raise LearnError('网格索引未命中任何点')
        return best, math.sqrt(best_d)


def quantile(values, q):
    if not values:
        raise LearnError('空序列无法取分位数')
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(math.floor(pos))
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


# --------------------------------------------------------------------------- #
# 读取与校验来源任务
# --------------------------------------------------------------------------- #

def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError as exc:
        raise LearnError(f'找不到文件：{path}') from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LearnError(f'不是合法 JSON：{path}（{exc}）') from exc


def numeric(value, where):
    """把 str/int/float 统一成有限数字；bool 与非法文本一律拒绝。"""
    if isinstance(value, bool):
        raise LearnError(f'{where} 不能是布尔值')
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str):
        try:
            out = float(value.strip())
        except ValueError as exc:
            raise LearnError(f'{where} 不是数字：{value!r}') from exc
    else:
        raise LearnError(f'{where} 类型不支持：{type(value).__name__}')
    if not math.isfinite(out):
        raise LearnError(f'{where} 不是有限数字：{value!r}')
    return out


def integer(value, where):
    out = numeric(value, where)
    if int(out) != out:
        raise LearnError(f'{where} 必须是整数：{value!r}')
    return int(out)


def parse_source(path):
    """解析一个 tasklist 文件，抽出几何、时序、围栏与字段格式。

    来源文件是打表数据，实测含三类瑕疵，全部在读取阶段处理，不做插值：
      1. 孤立跳点：单点跳变 80～190 米，前后都恢复正常点距；
      2. 采集接缝：时间与步数一起退到较早的值（两个片段的拼接痕迹）；
      3. 重叠片段：拼接后出现两段各自单调的记录，只保留最长的一段。
    """
    obj = read_json(path)
    if not isinstance(obj, dict) or obj.get('code') != 200:
        raise LearnError(f'{path} 不是 code=200 的任务文件')
    data = obj.get('data')
    if not isinstance(data, dict):
        raise LearnError(f'{path} 缺少 data 对象')
    raw_points = data.get('pointsList')
    if not isinstance(raw_points, list) or len(raw_points) < 50:
        raise LearnError(f'{path} 的 pointsList 少于 50 点，不足以学习点位逻辑')

    key_order = tuple(raw_points[0].keys())
    if tuple(k for k in key_order if k != 'ts') != POINT_KEY_ORDER:
        raise LearnError(f'{path} 的逐点键序与协议不符：{list(key_order)}')
    has_ts = 'ts' in key_order
    if has_ts and key_order[-1] != 'ts':
        raise LearnError(f'{path} 的 ts 不在点字段末尾：{list(key_order)}')

    # 第一遍：逐点做格式与类型校验，并记录坐标、时间、步数；跳点用原始序列的
    # 前一个点判定（用"最后一个保留点"判定会让一次跳变清空后面整段轨迹）。
    records, status_seen, fence_seen = [], {}, {}
    lon_text, lat_text, mileage_text = [], [], []
    dropped_spike = 0
    previous_raw = None
    for index, point in enumerate(raw_points):
        if not isinstance(point, dict) or tuple(k for k in point.keys() if k != 'ts') != POINT_KEY_ORDER:
            raise LearnError(f'{path} 第 {index} 点的键序与其他点不一致')
        text = point.get('point')
        if not isinstance(text, str) or text.count(',') != 1:
            raise LearnError(f'{path} 第 {index} 点的 point 不是 "经度,纬度"')
        lon_s, lat_s = text.split(',')
        lon, lat = numeric(lon_s, f'{path} 第 {index} 点经度'), numeric(lat_s, f'{path} 第 {index} 点纬度')
        if not -180 <= lon <= 180 or not -85 <= lat <= 85:
            raise LearnError(f'{path} 第 {index} 点经纬度超出范围')
        # isFence 是逐点状态（Y=在围栏内活动），来源里确有少量 N，必须按分布学习。
        status = str(point.get('runStatus'))
        fence = str(point.get('isFence'))
        status_seen[status] = status_seen.get(status, 0) + 1
        fence_seen[fence] = fence_seen.get(fence, 0) + 1

        candidate = (lon, lat)
        if previous_raw is not None and yun_route.distance(previous_raw, candidate) > MAX_SPIKE_M:
            dropped_spike += 1
            previous_raw = candidate
            continue
        previous_raw = candidate
        records.append({
            'index': index,
            'coord': candidate,
            'time': integer(point.get('runTime'), f'{path} 第 {index} 点 runTime'),
            'step': integer(point.get('runStep'), f'{path} 第 {index} 点 runStep'),
            'mile': numeric(point.get('runMileage'), f'{path} 第 {index} 点 runMileage'),
            'speed': numeric(point.get('speed'), f'{path} 第 {index} 点 speed'),
            'lon_text': len(lon_s.split('.')[1]) if '.' in lon_s else 0,
            'lat_text': len(lat_s.split('.')[1]) if '.' in lat_s else 0,
            'mile_text': len(str(point.get('runMileage')).split('.')[1])
            if '.' in str(point.get('runMileage')) else 0,
        })

    # 第二遍：按"时间与步数同时前进"切分单调段，只保留最长的一段。
    segments, current = [], []
    for record in records:
        if current and (record['time'] < current[-1]['time'] or
                        record['step'] < current[-1]['step']):
            segments.append(current)
            current = []
        current.append(record)
    if current:
        segments.append(current)
    if not segments:
        raise LearnError(f'{path} 过滤后没有可用点')
    kept = max(segments, key=len)
    dropped_split = len(records) - len(kept)
    if len(kept) < 50:
        raise LearnError(f'{path} 过滤后只剩 {len(kept)} 个点，不足以学习点位逻辑')

    coords = [r['coord'] for r in kept]
    times = [r['time'] for r in kept]
    steps = [r['step'] for r in kept]
    miles = [r['mile'] for r in kept]
    speeds = [r['speed'] for r in kept]
    lon_text = [r['lon_text'] for r in kept]
    lat_text = [r['lat_text'] for r in kept]
    mileage_text = [r['mile_text'] for r in kept]

    if any(b < a for a, b in zip(miles, miles[1:])):
        raise LearnError(f'{path} 的 runMileage 出现回退')
    if times[-1] <= times[0] or miles[-1] <= miles[0]:
        raise LearnError(f'{path} 的总时长或总里程没有前进')
    if '1' not in status_seen:
        raise LearnError(f'{path} 的 runStatus 没有正常值 1')
    if 'Y' not in fence_seen:
        raise LearnError(f'{path} 的 isFence 没有围栏内的点')

    manage = data.get('manageList')
    if not isinstance(manage, list) or len(manage) < 3:
        raise LearnError(f'{path} 的 manageList 少于 3 个踩点，无法学习点位逻辑')

    return {
        'name': Path(path).name,
        'coords': coords,
        'times': times,
        'miles': miles,
        'steps': steps,
        'speeds': speeds,
        'status_seen': status_seen,
        'fence_seen': fence_seen,
        'manage': manage,
        'key_order': key_order,
        'has_ts': has_ts,
        'text_precision': {
            'lon': max(set(lon_text), key=lon_text.count),
            'lat': max(set(lat_text), key=lat_text.count),
            'runMileage': max(set(mileage_text), key=mileage_text.count),
        },
        'summary': {
            'duration': integer(data.get('duration'), f'{path} duration'),
            'recordMileage_km': numeric(data.get('recordMileage'), f'{path} recordMileage'),
            'recodePace': numeric(data.get('recodePace'), f'{path} recodePace'),
            'recodeCadence': numeric(data.get('recodeCadence'), f'{path} recodeCadence'),
            'recodeDislikes': integer(data.get('recodeDislikes', 0), f'{path} recodeDislikes'),
            'schoolId': data.get('schoolId'),
        },
        'dropped_points': dropped_split + dropped_spike,
        'dropped_spikes': dropped_spike,
        'dropped_split': dropped_split,
        'kept_segment': [kept[0]['index'], kept[-1]['index']],
        'input_points': len(raw_points),
    }


def cluster_points(points, threshold):
    """把相近坐标合并成同一个点位（跨文件的同一踩点会有几米抖动）。"""
    clusters = []
    for point in points:
        for group in clusters:
            if yun_route.distance(point, group[0]) <= threshold:
                group.append(point)
                break
        else:
            clusters.append([point])
    out = []
    for group in clusters:
        out.append((sum(p[0] for p in group) / len(group),
                    sum(p[1] for p in group) / len(group), len(group)))
    return out


def learn_fences(sources, project, grid, tolerance):
    """学习踩点池与可达规则。

    实测结论：`marked=Y` 的踩点全部落在轨迹 20 米以内（最大 19.5 米），
    `marked=N` 的除极少数边界情况外都在 20.1 米以上。也就是说 `manageList`
    里同时包含本次跑到的点和本次没跑到的点，`marked` 记录的是“是否跑到”。
    生成时只把中心线确实经过的踩点标成 `Y` 并编号，其余保留为 `N`。
    """
    pool = []
    for src in sources:
        points, marked = parse_manage(src['manage'], src['name'])
        for point, flag in zip(points, marked):
            pool.append((point, flag))
    if not pool:
        raise LearnError('来源没有任何踩点')
    clusters = cluster_points([p for p, _ in pool], 25.0)
    if len(clusters) < 3:
        raise LearnError('合并后的踩点少于 3 个，无法确认围栏结构')

    reachable, unreachable = [], []
    for lon, lat, seen in clusters:
        point = (lon, lat)
        gap = grid.nearest(*project(point))[1]
        # 多数文件都出现过，且中心线确实经过 —— 才是稳定的必打点。
        if gap <= tolerance and seen >= max(2, len(sources) // 3):
            reached_votes = 0
            total_votes = 0
            for src in sources:
                points, marked = parse_manage(src['manage'], src['name'])
                near = min((yun_route.distance(point, q), m)
                           for q, m in zip(points, marked))
                if near[0] <= 25:
                    total_votes += 1
                    reached_votes += 1 if near[1] == 'Y' else 0
            reachable.append({'lon': lon, 'lat': lat, 'clearance_m': gap,
                              'files_seen': seen, 'marked_Y_votes': reached_votes,
                              'votes': total_votes})
        else:
            unreachable.append({'lon': lon, 'lat': lat, 'clearance_m': gap,
                                'files_seen': seen})
    reachable.sort(key=lambda row: row['clearance_m'])
    unreachable.sort(key=lambda row: row['clearance_m'])
    if not reachable:
        raise LearnError('没有学到一个可达踩点；中心线与 manageList 不匹配')
    return reachable, unreachable


def parse_manage(manage, where):
    """manageList 项按 {point, marked, index} 解析；index 可能是 int 或 str。"""
    points, marked = [], []
    for pos, item in enumerate(manage):
        if not isinstance(item, dict) or 'point' not in item:
            raise LearnError(f'{where} manageList 第 {pos} 项缺少 point')
        text = item['point']
        if not isinstance(text, str) or text.count(',') != 1:
            raise LearnError(f'{where} manageList 第 {pos} 项的 point 非法')
        lon, lat = (numeric(v, f'{where} manageList 第 {pos} 项') for v in text.split(','))
        points.append((lon, lat))
        marked.append(item.get('marked'))
    if len(set(points)) != len(points):
        raise LearnError(f'{where} manageList 存在重复踩点坐标')
    return points, marked


# --------------------------------------------------------------------------- #
# 学习
# --------------------------------------------------------------------------- #

def learn(tasks_dirs, name=None, smooth_sigma=1.0):
    """从若干 tasklist 目录学习一个校区的点位逻辑，返回可序列化模型。"""
    files = []
    for directory in tasks_dirs:
        path = Path(directory)
        if not path.is_dir():
            raise LearnError(f'任务目录不存在：{directory}')
        found = sorted(path.glob('*.json'))
        if not found:
            raise LearnError(f'任务目录里没有 JSON：{directory}')
        files.extend(found)
    if len(files) < 2:
        raise LearnError('至少需要两个 tasklist 文件才能学习重复观测（当前只有 1 个）')

    sources = [parse_source(path) for path in files]
    base_orders = {tuple(k for k in s['key_order'] if k != 'ts') for s in sources}
    if len(base_orders) != 1:
        raise LearnError('同一校区的逐点键序不一致，无法确定输出协议')
    key_order = tuple(POINT_KEY_ORDER) + (('ts',) if any(s['has_ts'] for s in sources) else ())

    # 多校区混在一起会让单圈长度无法定标：先确认所有文件确实在同一场地。
    centroids = [(sum(p[0] for p in s['coords']) / len(s['coords']),
                  sum(p[1] for p in s['coords']) / len(s['coords'])) for s in sources]
    spread = max(yun_route.distance(a, b) for a in centroids for b in centroids)
    if spread > CAMPUS_SPREAD_M:
        raise LearnError(f'输入文件的轨迹中心相距 {spread:.0f} 米，超过 {CAMPUS_SPREAD_M:.0f} 米；'
                         '这些文件不像同一个校区，请按校区分别学习')

    # 共同坐标框：第一个文件的首点，精度足够覆盖校园尺度（跨度已限 0.2 度）。
    origin = sources[0]['coords'][0]
    project = project_factory(origin)
    traces = []
    for src in sources:
        xy = [project(p) for p in src['coords']]
        length = sum(math.dist(a, b) for a, b in zip(xy, xy[1:]))
        traces.append({'name': src['name'], 'xy': xy, 'length': length, 'src': src})
    all_points = [p for t in traces for p in t['xy']]

    # 参考轨迹取最长的一条：观测点最多，抖动被平滑后最接近真实中心线。
    ref = max(traces, key=lambda t: t['length'])
    if any(math.dist(a, b) > MAX_STEP_M for a, b in zip(ref['xy'], ref['xy'][1:])):
        raise LearnError(f'{ref["name"]} 过滤后仍有超过 {MAX_STEP_M:.0f} 米的相邻点，'
                         '采样过稀，无法确定中心线')
    resampled, _raw_perimeter = resample_closed(ref['xy'], max(360, min(1200, len(ref['xy']))))
    centerline = smooth_closed(resampled, smooth_sigma)
    centerline, loop_m = resample_closed(centerline, len(centerline))
    if loop_m < 50:
        raise LearnError(f'学习到的一圈周长只有 {loop_m:.1f} 米，几何不可信')

    grid = Grid(centerline, max(2.0, loop_m / len(centerline)))
    deviations = sorted(grid.nearest(*p)[1] for p in all_points)
    normals = vertex_normals(centerline)
    signed = [[grid.nearest(*p)[0] for p in t['xy']] for t in traces]
    profile = []
    for index in range(len(centerline)):
        values = []
        for t, idxs in zip(traces, signed):
            for pos, hit in enumerate(idxs):
                if hit == index:
                    px, py = t['xy'][pos]
                    nx, ny = normals[index]
                    values.append((px - centerline[index][0]) * nx + (py - centerline[index][1]) * ny)
        profile.append({'lo': quantile(values, 0.05) if values else 0.0,
                        'hi': quantile(values, 0.95) if values else 0.0,
                        'n': len(values)})
    envelope = sorted(max(abs(row['lo']), abs(row['hi'])) for row in profile)
    corridor_m = quantile(envelope, 0.95)

    # 踩点：跨文件聚类成点位池，再按“中心线是否经过”判定可达性。
    reachable, unreachable = learn_fences(sources, project, grid, FENCE_REACH_M)

    # 单圈长度定标。两件事必须分开：
    #   * 环形平滑去掉了高频抖动，平滑后的弧长会比原始轨迹短；
    #   * 原始轨迹的弧长又被 GPS 抖动抬高，高于 App 上报里程。
    # 逐个文件给出一个单圈估计 = 平滑弧长 × (该文件上报里程 / 该文件地理里程)。
    # 报告里程与轨迹弧长本应接近（实测比值中位约 1.00）；比值明显偏离 1 的文件说明
    # 拼接片段没有被完整保留（例如两段记录重叠比例很大），这类文件不参与定标。
    reported_km, geo_km = [], []
    for t in traces:
        reported_km.append(t['src']['summary']['recordMileage_km'])
        geo_km.append(t['length'] / 1000)
    if any(km <= 0 for km in geo_km + reported_km):
        raise LearnError('来源里程必须为正')
    loop_geo_m = loop_m
    coherent, excluded = [], []
    for t, rep, geo in zip(traces, reported_km, geo_km):
        ratio = rep / geo
        if RATIO_LOW <= ratio <= RATIO_HIGH:
            coherent.append(loop_geo_m * ratio)
        else:
            excluded.append({'file': t['name'], 'reported_over_geo': round(ratio, 4)})
    if len(coherent) < 2:
        raise LearnError(f'只有 {len(coherent)} 个文件的里程口径自洽，无法定标单圈长度')
    loop_m = quantile(coherent, 0.5)
    spread = (max(coherent) - min(coherent)) / loop_m if len(coherent) > 1 else 0.0
    if not 0.5 <= loop_m / loop_geo_m <= 2.0:
        raise LearnError(f'定标后的单圈长度 {loop_m:.1f} 米相对平滑弧长 '
                         f'{loop_geo_m:.1f} 米偏差过大，里程口径不一致')
    if spread > 0.35:
        raise LearnError(f'自洽文件给出的单圈长度差异过大（{spread:.2f}），'
                         '这些记录可能不是同一条环线')
    laps = [km / (loop_m / 1000) for km in reported_km]
    if min(laps) < 0.2 or max(laps) > 60:
        raise LearnError(f'按上报里程算出的圈数范围 {min(laps):.2f}～{max(laps):.2f} 不合理，'
                         '单圈长度估计不可信')
    # 生成阶段按严格递增的整数秒推进，这里只看正增量。
    time_deltas = [b - a for src in sources for a, b in zip(src['times'], src['times'][1:]) if b > a]
    step_deltas = [b - a for src in sources for a, b in zip(src['steps'], src['steps'][1:]) if b > a]
    jitter = [jitter_of(t['xy']) for t in traces]
    stride = []
    for src in sources:
        if src['steps'][-1] - src['steps'][0] > 0:
            stride.append((src['miles'][-1] - src['miles'][0]) / (src['steps'][-1] - src['steps'][0]))
    fence_ratio = []
    for src in sources:
        total = sum(src['fence_seen'].values())
        fence_ratio.append(src['fence_seen'].get('N', 0) / total)
    status_seen = {}
    for src in sources:
        for key, count in src['status_seen'].items():
            status_seen[key] = status_seen.get(key, 0) + count

    model = {
        'model_version': MODEL_VERSION,
        'name': name or Path(tasks_dirs[0]).name,
        'created_from': {
            'files': [t['name'] for t in traces],
            'dirs': [str(d) for d in tasks_dirs],
            'file_count': len(traces),
            'smooth_sigma': smooth_sigma,
            'reference': ref['name'],
            'reference_geo_m': round(ref['length'], 3),
        },
        'frame': {
            'origin': [round(origin[0], 9), round(origin[1], 9)],
            'latitude_scale_m': round(111320.0, 3),
            'longitude_scale_m': round(111320.0 * math.cos(math.radians(origin[1])), 6),
            'coordinate_system': 'GCJ-02',
        },
        'loop': {
            'centerline': [[round(x, 3), round(y, 3)] for x, y in centerline],
            'perimeter_m': round(loop_m, 3),
            'perimeter_smoothed_m': round(loop_geo_m, 3),
            'perimeter_estimates_m': [round(v, 3) for v in coherent],
            'estimate_spread': round(spread, 5),
            'excluded_files': excluded,
            'reported_km_median': round(quantile(reported_km, 0.5), 5),
            'geometry_km_median': round(quantile(geo_km, 0.5), 5),
            'distortion_ratio': round(quantile(reported_km, 0.5) / quantile(geo_km, 0.5), 5),
        },
        'corridor': {
            'envelope_p95_m': round(corridor_m, 3),
            'deviation_median_m': round(quantile(deviations, 0.5), 3),
            'deviation_p95_m': round(quantile(deviations, 0.95), 3),
            'deviation_p99_m': round(quantile(deviations, 0.99), 3),
            'deviation_max_m': round(deviations[-1], 3),
            'profile': [{'lo': round(r['lo'], 3), 'hi': round(r['hi'], 3), 'n': r['n']}
                        for r in profile],
        },
        'points_of_interest': {
            'fence_reach_tolerance_m': FENCE_REACH_M,
            'reachable': [{'point': [round(r['lon'], 7), round(r['lat'], 7)],
                           'clearance_m': round(r['clearance_m'], 3),
                           'files_seen': r['files_seen'],
                           'marked_Y_votes': r['marked_Y_votes']} for r in reachable],
            'unreachable': [{'point': [round(r['lon'], 7), round(r['lat'], 7)],
                             'clearance_m': round(r['clearance_m'], 3),
                             'files_seen': r['files_seen']} for r in unreachable],
            'count': len(reachable) + len(unreachable),
            'reachable_count': len(reachable),
        },
        'schema': {
            'point_key_order': list(key_order),
            'top_key_order': list(TOP_KEY_ORDER),
            'data_key_order': list(DATA_KEY_ORDER),
            'types': {'id': 'int', 'point': 'str', 'speed': 'float', 'runStatus': 'int',
                      'runRecordId': 'int', 'runTime': 'str', 'isFence': 'str',
                      'runStep': 'str', 'runMileage': 'str'},
            'has_ts_key': any(s['has_ts'] for s in sources),
            'text_precision': sources[0]['text_precision'],
            'runStatus_seen': {k: status_seen[k] for k in sorted(status_seen)},
            'isFence_seen': {k: sum(s['fence_seen'].get(k, 0) for s in sources)
                             for k in sorted({k for s in sources for k in s['fence_seen']})},
            'outside_fence_ratio_median': round(quantile(fence_ratio, 0.5), 4),
            'code': 200,
            'msg': '请求成功',
            'schoolId': sources[0]['summary']['schoolId'],
        },
        'telemetry': {
            'duration_s': [s['summary']['duration'] for s in sources],
            'duration_median_s': round(quantile([s['summary']['duration'] for s in sources], 0.5), 3),
            'pace_min_per_km': [round(s['summary']['recodePace'], 3) for s in sources],
            'pace_median_min_per_km': round(quantile([s['summary']['recodePace'] for s in sources], 0.5), 3),
            'cadence_spm': [round(s['summary']['recodeCadence'], 3) for s in sources],
            'cadence_median_spm': round(quantile([s['summary']['recodeCadence'] for s in sources], 0.5), 3),
            'stride_median_m': round(quantile(stride, 0.5), 4) if stride else None,
            'sample_interval_s': {str(v): time_deltas.count(v) for v in sorted(set(time_deltas))},
            'step_delta_median': quantile(step_deltas, 0.5) if step_deltas else 0,
            'step_delta_p95': quantile(step_deltas, 0.95) if step_deltas else 0,
            'speed_field_median': round(quantile([v for s in sources for v in s['speeds']], 0.5), 3),
            'speed_field_min': round(min(v for s in sources for v in s['speeds']), 3),
            'speed_field_max': round(max(v for s in sources for v in s['speeds']), 3),
            'recodeDislikes': sources[0]['summary']['recodeDislikes'],
        },
        'observations': {
            'lap_count': [round(v, 4) for v in laps],
            'lap_count_median': round(quantile(laps, 0.5), 4),
            'lap_count_min': round(min(laps), 4),
            'lap_count_max': round(max(laps), 4),
            'point_count': [len(t['xy']) for t in traces],
            'point_count_median': quantile([len(t['xy']) for t in traces], 0.5),
            'median_step_m': round(quantile([math.dist(a, b) for t in traces
                                             for a, b in zip(t['xy'], t['xy'][1:])], 0.5), 4),
            'jitter_median_m': round(quantile(jitter, 0.5), 4),
            'jitter_p95_m': round(quantile(jitter, 0.95), 4),
            'reported_km': [round(v, 4) for v in reported_km],
            'geo_km': [round(v, 4) for v in geo_km],
        },
    }
    check_model(model)
    return model


def check_model(model):
    """模型自检：形状、范围与必需字段；学习结果不可信时直接失败。"""
    if not isinstance(model, dict) or model.get('model_version') != MODEL_VERSION:
        raise LearnError('学习模型版本不支持，请重新运行 learn')
    loop = model['loop']
    ring = [tuple(p) for p in loop['centerline']]
    if len(ring) < 64:
        raise LearnError('模型中心线点太少')
    perimeter = sum(math.dist(a, b) for a, b in zip(ring, ring[1:] + ring[:1]))
    if abs(perimeter - loop['perimeter_smoothed_m']) / loop['perimeter_smoothed_m'] > 0.02:
        raise LearnError('模型记录的单圈长度与中心线不一致，文件可能被改写')
    if not 0 < model['corridor']['envelope_p95_m'] < 40:
        raise LearnError('走廊宽度不在合理范围（0～40 米）')
    if model['points_of_interest']['reachable_count'] < 2:
        raise LearnError('可达踩点少于 2 个，无法确定必打点位')
    if model['schema']['isFence_seen'].get('Y', 0) <= 0:
        raise LearnError('来源没有围栏内的点，协议假设不成立')
    if '1' not in model['schema']['runStatus_seen']:
        raise LearnError('来源没有 runStatus=1 的正常点')
    return True


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

def build_placement(model, laps, offset_m, jitter_m, seed, start_fraction, novelty=0.0):
    """在中心线的走廊内合成一条新路径，返回 (点列, 单圈米数, 车道剖面, 抖动幅度)。

    点列元素为 (x, y, 从起点起算的米数)。横向位置由三部分叠加：中心线本身、一个
    低频车道剖面、按来源抖动量级生成的高频噪声。低频剖面保证相邻点之间连续变化
    （不会跨车道跳变），噪声复现真实轨迹的微观质感；相位旋转把起点挪到环上另一处。
    所有点都会回查与中心线的距离，越界立即失败而不是悄悄放过。

    `novelty` > 0 时把车道偏移按比例推向该处实测走廊的边缘：偏移越大，与来源轨迹
    重合的部分越少，但落点也越靠近观测带的外沿（那里实测点最少）。
    """
    rng = random.Random(seed)
    ring = [tuple(p) for p in model['loop']['centerline']]
    profile = model['corridor']['profile']
    n = len(ring)
    if len(profile) != n:
        raise LearnError('走廊剖面长度与中心线不一致，模型已损坏')
    normals = vertex_normals(ring)
    corridor = model['corridor']['envelope_p95_m']
    perimeter = model['loop']['perimeter_m']
    if corridor <= 0 or perimeter <= 0:
        raise LearnError('学习的走廊宽度或周长为零，无法放置新路径')
    if not 0.0 <= novelty <= 1.0:
        raise LearnError('novelty 必须在 0～1 之间')

    # 该顶点允许的横向区间：用实测分位区间与本顶点的安全半径取交集。
    def corridor_at(i):
        row = profile[i]
        low = min(row['lo'], row['hi'], 0.0)
        high = max(row['lo'], row['hi'], 0.0)
        if high - low < 0.4:
            low, high = -0.2, 0.2
        safe = min(corridor * 0.85, max(abs(low), abs(high)))
        return -safe, safe

    # 低频车道剖面：四个互不整除的谐波叠加，相位由种子决定。
    harmonics = (1, 2, 3, 5)
    amplitudes = [offset_m * rng.uniform(0.45, 1.0) / len(harmonics) for _ in harmonics]
    phases = [rng.uniform(-math.pi, math.pi) for _ in harmonics]
    lane = [sum(a * math.sin(k * (2 * math.pi * i / n) + p)
                for a, k, p in zip(amplitudes, harmonics, phases)) for i in range(n)]
    peak = max(abs(v) for v in lane) or 1.0
    limit = min(offset_m, max(0.2, corridor * 0.85))
    lane = [v * limit / peak for v in lane]
    # novelty 只放大偏移的幅度，不改变形状；随后逐点夹回走廊，保证仍然落在实测带内。
    lane = [v * (1.0 + novelty * 1.5) for v in lane]
    lane = [clamp(v, *corridor_at(i)) for i, v in enumerate(lane)]

    # 高频抖动：两个互不整除的谐波，闭环连续且量级对齐来源观测。
    noise_amp = min(jitter_m, max(0.0, corridor - limit) * 0.9)
    k1, k2 = rng.randrange(37, 60), rng.randrange(61, 97)
    phase1, phase2 = rng.uniform(-math.pi, math.pi), rng.uniform(-math.pi, math.pi)

    step = model['observations']['median_step_m']
    samples = max(32, int(round(perimeter / max(0.5, step))))
    # 顶点数刻意不等于任何单一来源的点数，避免与来源逐点重合。
    samples += 3 + (seed % 7)
    start_index = int(round(start_fraction * samples)) % n

    grid = Grid(ring, max(2.0, perimeter / n))
    tolerance = max(0.5, corridor * 1.35)
    placed, total, previous = [], 0.0, None
    for lap in range(laps):
        for j in range(samples):
            base = (start_index + lap * samples + j) % n
            theta = 2 * math.pi * base / n
            low, high = corridor_at(base)
            lateral = clamp(lane[base]
                            + noise_amp * math.sin(k1 * theta + phase1)
                            + noise_amp * 0.45 * math.sin(k2 * theta + phase2), low, high)
            x = ring[base][0] + normals[base][0] * lateral
            y = ring[base][1] + normals[base][1] * lateral
            _, dist = grid.nearest(x, y)
            if dist > tolerance:
                raise LearnError(f'生成点偏离中心线 {dist:.2f} 米，超过走廊容差；'
                                 '请降低 --lane-offset-m 或 --jitter-m')
            if previous is not None:
                total += math.dist((x, y), previous)
            placed.append((x, y, total))
            previous = (x, y)
    if len(placed) < 2:
        raise LearnError('生成点不足两个')
    if placed[-1][2] < 10:
        raise LearnError(f'生成路径只有 {placed[-1][2]:.1f} 米，不足以构成任务')
    return placed, perimeter, lane, noise_amp


def clamp(value, low, high):
    return low if value < low else (high if value > high else value)


def format_point(model, *, lon, lat, speed, run_time, step_count, mileage, ts=None):
    """严格按来源键序与类型组装一个点；文本精度沿用该校区来源的格式。

    `isFence`/`runStatus` 取围栏内正常值：生成路径整体落在学习到的走廊里，
    走廊由实测点在围栏内的轨迹构成，因此逐点声明为围栏内是唯一自洽的取值。
    """
    schema = model['schema']
    precision = schema['text_precision']
    mileage_precision = max(MIN_MILEAGE_TEXT_PRECISION, precision['runMileage'])
    record = {
        'id': 0,
        'point': f"{lon:.{precision['lon']}f},{lat:.{precision['lat']}f}",
        'speed': round(float(speed), 2),
        'runStatus': 1,
        'runRecordId': 0,
        'runTime': str(int(run_time)),
        'isFence': 'Y',
        'runStep': str(int(step_count)),
        'runMileage': f"{mileage:.{mileage_precision}f}",
    }
    if schema['has_ts_key']:
        record['ts'] = str(int(ts if ts is not None else 0))
    return {key: record[key] for key in schema['point_key_order']}


def build_manage(model, planned_points):
    """围栏点位：只把生成路径确实经过的踩点标成 Y 并编号，其余保留为 N。

    planned_points 是本地米制平面坐标，踩点要先投影到同一平面再比对；
    直接把经纬度丢进米制索引会得到无意义的距离。
    """
    poi = model['points_of_interest']
    tolerance = poi['fence_reach_tolerance_m']
    project = project_factory(tuple(model['frame']['origin']))
    grid = Grid([(p[0], p[1]) for p in planned_points])
    items = []
    # 可达点按环上的到达顺序（而不是学习时的净距顺序）编号。
    order = []
    for row in poi['reachable']:
        lon, lat = row['point']
        x, y = project((lon, lat))
        hit, dist = grid.nearest(x, y)
        if dist > tolerance:
            continue
        order.append((hit, lon, lat))
    order.sort(key=lambda row: row[0])
    for index, (_hit, lon, lat) in enumerate(order):
        items.append({'point': f'{lon:.6f},{lat:.6f}', 'marked': 'Y', 'index': index})
    for row in poi['unreachable']:
        lon, lat = row['point']
        items.append({'point': f'{lon:.6f},{lat:.6f}', 'marked': 'N',
                      'index': len(order)})
    if not items:
        raise LearnError('manageList 为空，无法体现踩点逻辑')
    if not any(item['marked'] == 'Y' for item in items):
        raise LearnError('没有任何可达踩点落在生成路径上，请调整车道偏移或圈数')
    return items


def generate(model, *, seed=20260919, laps=None, target_m=None, lane_offset_m=None,
             jitter_m=None, start_fraction=None, pace=None, cadence=None,
             sample_seconds=1, novelty=0.0, ts_base=0):
    """按学习模型合成一份全新的任务文件（dict，可直接 json.dumps）。"""
    check_model(model)
    loop_m = model['loop']['perimeter_m']
    tele = model['telemetry']
    obs = model['observations']

    if laps is None:
        if target_m is not None:
            laps = max(1, int(round(float(target_m) / loop_m)))
        else:
            laps = max(1, int(round(obs['lap_count_median'])))
    laps = int(laps)
    if not 1 <= laps <= 200:
        raise LearnError(f'圈数必须在 1～200 之间，收到 {laps}')

    corridor = model['corridor']['envelope_p95_m']
    if lane_offset_m is None:
        lane_offset_m = min(3.0, corridor * 0.55)
    if jitter_m is None:
        jitter_m = min(obs['jitter_median_m'], max(0.0, corridor - lane_offset_m))
    if start_fraction is None:
        start_fraction = 0.0
    if seed == 'auto':
        seed = secrets.randbits(63)
    seed = int(seed)
    if not 0.0 <= float(start_fraction) < 1.0:
        raise LearnError('start_fraction 必须在 [0, 1) 之间')
    if pace is None:
        pace = tele['pace_median_min_per_km']
    if cadence is None:
        cadence = tele['cadence_median_spm']
    pace = float(pace)
    cadence = float(cadence)
    sample_seconds = int(sample_seconds)
    if not 2 <= pace <= 30:
        raise LearnError('配速必须在 2～30 分钟/公里之间')
    if not 1 <= cadence <= 350:
        raise LearnError('步频必须在 1～350 步/分钟之间')
    if not 1 <= sample_seconds <= 5:
        raise LearnError('采样间隔必须是 1～5 秒的整数')
    ts_base = integer(ts_base, 'ts_base')
    if ts_base < 0:
        raise LearnError('ts_base 必须为非负整数（Unix 秒）')
    has_ts = model['schema']['has_ts_key']
    if has_ts and ts_base == 0:
        ts_base = 1_700_000_000   # 占位起点：保证 ts 自洽且非零；真实值请用 --ts-base
        ts_placeholder = True
    else:
        ts_placeholder = False

    placed, perimeter, lane, noise_amp = build_placement(
        model, laps, float(lane_offset_m), float(jitter_m), seed, float(start_fraction),
        novelty=float(novelty))
    unproject = unproject_factory(tuple(model['frame']['origin']))

    # 时间轴：按几何里程与目标配速线性推进，末点落在整数秒总时长上。
    geometry_m = placed[-1][2]
    duration = max(sample_seconds, int(round(geometry_m / 1000 * pace * 60)))
    duration -= duration % sample_seconds
    if duration < sample_seconds:
        duration = sample_seconds

    def position_at_mileage(target):
        """在生成点列上按弧长插值，返回 (x, y, 实际里程)。"""
        if target <= 0:
            return placed[0][0], placed[0][1], 0.0
        if target >= placed[-1][2]:
            return placed[-1][0], placed[-1][1], placed[-1][2]
        low, high = 0, len(placed) - 1
        while low < high - 1:
            mid = (low + high) // 2
            if placed[mid][2] <= target:
                low = mid
            else:
                high = mid
        span = placed[high][2] - placed[low][2]
        frac = 0.0 if span == 0 else (target - placed[low][2]) / span
        return (placed[low][0] + (placed[high][0] - placed[low][0]) * frac,
                placed[low][1] + (placed[high][1] - placed[low][1]) * frac,
                target)

    ticks = list(range(0, duration, sample_seconds)) + [duration]
    point_count = len(ticks)
    total_steps = int(round(cadence * duration / 60))
    if total_steps <= 0:
        raise LearnError(f'总步数为零（步频 {cadence}，时长 {duration} 秒）')

    # 步数增量：来源的逐点增量并非恒定，按其 p95 与中位之差做有界抖动分配，
    # 并用“剩余步数 / 剩余点数”回补，保证末点累计恰好等于总步数。
    step_rng = random.Random(seed ^ 0x5EED)
    spread = max(0.0, tele['step_delta_p95'] - tele['step_delta_median'])
    upper = 12 * (tele['step_delta_p95'] + 1)

    points, previous_mileage, previous_time, used_steps = [], 0.0, 0, 0
    for k, run_time in enumerate(ticks):
        progress = k / (point_count - 1) if point_count > 1 else 1.0
        x, y, mileage = position_at_mileage(geometry_m * progress)
        lon, lat = unproject((x, y))
        delta_m = mileage - previous_mileage
        delta_s = run_time - previous_time
        pace_text = yun_route.client_speed(delta_m, delta_s) if k else '0.0'
        speed_value = float(pace_text)
        if k and not SPEED_LOW - 0.01 <= speed_value <= SPEED_HIGH + 0.01:
            raise LearnError(f'第 {k} 点配速 {speed_value} 超出协议范围，请调整 --pace')

        if k == 0:
            step_count = 0
        elif k == point_count - 1:
            step_count = total_steps
        else:
            remaining_points = point_count - k
            average = (total_steps - used_steps) / remaining_points
            delta = average + rng_normal(step_rng, spread)
            delta = max(0.0, min(upper, delta))
            take = int(round(delta))
            take = max(0, min(take, (total_steps - used_steps) - (remaining_points - 1)))
            used_steps += take
            step_count = used_steps

        points.append(format_point(
            model, lon=lon, lat=lat, speed=speed_value, run_time=run_time,
            step_count=step_count, mileage=mileage, ts=ts_base + run_time))
        previous_mileage, previous_time = mileage, run_time

    points[-1]['runMileage'] = (
        f"{geometry_m:.{max(MIN_MILEAGE_TEXT_PRECISION, model['schema']['text_precision']['runMileage'])}f}")
    points[-1]['runStep'] = str(total_steps)
    manage = build_manage(model, placed)
    data = {
        'recordMileage': round(geometry_m / 1000, 6),
        'recodePace': round(duration / 60 / (geometry_m / 1000), 4),
        'recodeCadence': int(points[-1]['runStep']) * 60 / duration if duration else 0,
        'recodeDislikes': tele['recodeDislikes'],
        'duration': duration,
        'pointsList': points,
        'schoolId': model['schema']['schoolId'],
        'manageList': manage,
    }
    task = {'msg': model['schema']['msg'], 'code': model['schema']['code'],
            'data': {key: data[key] for key in DATA_KEY_ORDER}}
    report = validate_generated(task, model, placed, lane, noise_amp, seed)
    if ts_placeholder:
        report['ts_placeholder'] = ts_base
    return task, report


def rng_normal(rng, spread):
    """有界抖动：三次均匀分布叠加近似正态，避免极端值把步数差拉爆。"""
    if spread <= 0:
        return 0.0
    return (rng.random() + rng.random() + rng.random() - 1.5) * spread * 2.0


def validate_generated(task, model, placed, lane, noise_amp, seed):
    """对生成结果做独立复算：几何、协议、走廊、踩点与汇总一致性。"""
    data = task['data']
    points = data['pointsList']
    if len(points) < 2:
        raise LearnError('生成任务少于两个点')
    schema = model['schema']
    for index, point in enumerate(points):
        if tuple(point.keys()) != tuple(schema['point_key_order']):
            raise LearnError(f'第 {index} 点键序与学习结果不一致')
    coords = []
    for point in points:
        lon_s, lat_s = point['point'].split(',')
        coords.append((float(lon_s), float(lat_s)))
    if any(b <= a for a, b in zip((float(p['runTime']) for p in points),
                                  (float(p['runTime']) for p in points[1:]))):
        raise LearnError('runTime 未严格递增')
    if any(b <= a for a, b in zip((float(p['runMileage']) for p in points),
                                  (float(p['runMileage']) for p in points[1:]))):
        raise LearnError('runMileage 未严格递增')
    if any(b < a for a, b in zip((int(p['runStep']) for p in points),
                                 (int(p['runStep']) for p in points[1:]))):
        raise LearnError('runStep 回退')
    if any(math.dist(a, b) > MAX_STEP_M for a, b in zip(coords, coords[1:])):
        raise LearnError('生成路径存在超过 90 米的相邻点')
    for axis in (0, 1):
        span = max(p[axis] for p in coords) - min(p[axis] for p in coords)
        if span > MAX_SPAN_DEG:
            raise LearnError('生成路径经纬度跨度超过 0.2 度')
    # yun_route.coordinates 是主流程使用的同一套几何校验，这里直接复用。
    yun_route.coordinates([list(p) for p in coords])

    geometry_m = placed[-1][2]
    if abs(float(points[-1]['runMileage']) - geometry_m) > 0.5:
        raise LearnError('末点里程与生成几何长度不符')
    if abs(data['recordMileage'] - geometry_m / 1000) > 0.001:
        raise LearnError('recordMileage 与几何长度不符')
    # 来源里程与几何里程的比值必须仍在观测范围内。
    ratio = data['recordMileage'] / (geometry_m / 1000)
    observed = model['observations']
    ratios = [observed['reported_km'][i] / observed['geo_km'][i]
              for i in range(len(observed['geo_km']))]
    if not 0.5 <= ratio <= 2.0:
        raise LearnError(f'上报/几何里程比 {ratio:.3f} 超出观测范围 '
                         f'({min(ratios):.3f}～{max(ratios):.3f})')
    cadence_out = data['recodeCadence']
    if not 20 <= cadence_out <= 400:
        raise LearnError(f'生成步频 {cadence_out:.1f} 明显异常')

    # 围栏可达性：marked=Y 的踩点必须在学习到的容差内贴近生成路径。
    project = project_factory(tuple(model['frame']['origin']))
    projected = [project(p) for p in coords]
    tolerance = model['points_of_interest']['fence_reach_tolerance_m']
    fence_gaps = []
    for index, item in enumerate(data['manageList']):
        lon, lat = (float(v) for v in item['point'].split(','))
        x, y = project((lon, lat))
        best = min(math.dist((x, y), q) for q in projected)
        if item['marked'] == 'Y':
            fence_gaps.append(best)
            if best > tolerance:
                raise LearnError(f'第 {index + 1} 个踩点距生成路径 {best:.1f} 米，'
                                 f'超过 {tolerance:.0f} 米容差')
    if not any(item['marked'] == 'Y' for item in data['manageList']):
        raise LearnError('manageList 没有任何 marked=Y 的点位')
    marked_order = [item['index'] for item in data['manageList'] if item['marked'] == 'Y']
    if marked_order != list(range(len(marked_order))):
        raise LearnError(f'marked=Y 的 index 应为 0..n-1，实际 {marked_order}')

    # 新颖度：生成点到来源点云的横向间距分布。走廊由实测点构成，所以"离来源最近
    # 一点的距离"不可能很大——有意义的是有多少点真正离开了所有已有轨迹。
    # 来源点云只在直接给 --tasks 时附带，且与生成点一样先投影到模型的本地平面。
    novelty = None
    source_cloud = model.get('_source_points')
    if isinstance(source_cloud, list) and len(source_cloud) > 100:
        cloud = Grid([project(tuple(p)) for p in source_cloud], 6.0)
        gaps = sorted(cloud.nearest(*q)[1] for q in projected)
        novelty = {
            'generated_to_source_median_m': round(quantile(gaps, 0.5), 3),
            'generated_to_source_max_m': round(gaps[-1], 3),
            'fraction_beyond_1m': round(sum(1 for g in gaps if g > 1) / len(gaps), 4),
            'fraction_beyond_2m': round(sum(1 for g in gaps if g > 2) / len(gaps), 4),
            'fraction_beyond_3m': round(sum(1 for g in gaps if g > 3) / len(gaps), 4),
        }
    return {
        'seed': seed,
        'points': len(points),
        'laps_equivalent': round(geometry_m / model['loop']['perimeter_m'], 3),
        'geometry_m': round(geometry_m, 3),
        'loop_perimeter_m': model['loop']['perimeter_m'],
        'duration_s': data['duration'],
        'pace_min_per_km': round(data['recodePace'], 3),
        'cadence_spm': round(data['recodeCadence'], 3),
        'record_mileage_km': round(data['recordMileage'], 4),
        'reported_over_geo_ratio': round(ratio, 4),
        'fence_gap_m': [round(v, 3) for v in fence_gaps],
        'lane_peak_m': round(max(abs(v) for v in lane), 3),
        'jitter_amp_m': round(noise_amp, 3),
        'novelty': novelty,
    }


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #

def write_json(path, payload):
    out = Path(path)
    if out.exists():
        raise LearnError(f'输出已存在，请换文件名：{out}')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return out


def print_report(model, report=None):
    loop = model['loop']
    cor = model['corridor']
    obs = model['observations']
    poi = model['points_of_interest']
    print(f"校区模型：{model['name']}  来源 {model['created_from']['file_count']} 个文件"
          f"（参考 {model['created_from']['reference']}）")
    print(f"  单圈 {loop['perimeter_m']:.1f} 米（平滑弧长 {loop['perimeter_smoothed_m']:.1f} 米，"
          f"各文件估计离散 {loop['estimate_spread']:.3f}）")
    print(f"  圈数 {obs['lap_count_min']:.2f}～{obs['lap_count_max']:.2f}"
          f"（中位 {obs['lap_count_median']:.2f}），"
          f"上报里程 {min(obs['reported_km']):.2f}～{max(obs['reported_km']):.2f} 公里")
    print(f"  走廊 ±{cor['envelope_p95_m']:.2f} 米（偏离中位 {cor['deviation_median_m']:.2f} / "
          f"p95 {cor['deviation_p95_m']:.2f} / p99 {cor['deviation_p99_m']:.2f} 米）")
    print(f"  抖动中位 {obs['jitter_median_m']:.2f} 米，中位点距 {obs['median_step_m']:.2f} 米")
    clearances = ', '.join('{:.1f}m'.format(r['clearance_m']) for r in poi['reachable'])
    print(f"  踩点池 {poi['count']} 个：可达 {poi['reachable_count']} 个（净距 {clearances}），"
          f"不可达 {len(poi['unreachable'])} 个")
    print(f"  配速中位 {model['telemetry']['pace_median_min_per_km']:.2f} 分/公里，"
          f"步频中位 {model['telemetry']['cadence_median_spm']:.1f} 步/分，"
          f"采样间隔 {sorted(model['telemetry']['sample_interval_s'])} 秒")
    if report:
        print(f"  新路径：{report['points']} 点 / {report['geometry_m']:.1f} 米 / "
              f"{report['duration_s']} 秒 / 等效 {report['laps_equivalent']:.2f} 圈"
              f" / seed={report['seed']}")
        print(f"    配速 {report['pace_min_per_km']:.2f} 分/公里，步频 {report['cadence_spm']:.1f} 步/分，"
              f"上报/几何 = {report['reported_over_geo_ratio']:.3f}")
        print(f"    车道峰值 {report['lane_peak_m']:.2f} 米，抖动幅度 {report['jitter_amp_m']:.2f} 米，"
              f"踩点净距 {', '.join(f'{v:.1f}m' for v in report['fence_gap_m'])}")
        if report['novelty']:
            nov = report['novelty']
            print(f"    与来源点云：中位 {nov['generated_to_source_median_m']:.2f} 米 / "
                  f"最大 {nov['generated_to_source_max_m']:.2f} 米；"
                  f"偏离 1 米以上 {nov['fraction_beyond_1m'] * 100:.0f}%，"
                  f"2 米以上 {nov['fraction_beyond_2m'] * 100:.0f}%，"
                  f"3 米以上 {nov['fraction_beyond_3m'] * 100:.0f}%")
        if report.get('ts_placeholder'):
            print(f"    注意：ts 用了占位起点 {report['ts_placeholder']}；"
                  '需要真实时间戳请加 --ts-base')


def cmd_learn(args):
    model = learn(args.tasks, name=args.name, smooth_sigma=args.smooth_sigma)
    path = write_json(args.out, model)
    print_report(model)
    print(f"模型已写入：{path}")
    print('模型只记录观测到的几何与协议统计；不代表服务端围栏或成绩判定。')
    return 0


def cmd_generate(args):
    if args.tasks:
        # 先学习再生成；来源点云只用于新颖度报告，不写入输出文件。
        model = learn(args.tasks, name=args.name, smooth_sigma=args.smooth_sigma)
        model['_source_points'] = collect_source_points(args.tasks)
    elif args.model:
        model = read_json(args.model)
    else:
        raise LearnError('需要 --model 或 --tasks')
    task, report = generate(
        model, seed=args.seed, laps=args.laps, target_m=args.distance,
        lane_offset_m=args.lane_offset_m, jitter_m=args.jitter_m,
        start_fraction=args.start_fraction, pace=args.pace, cadence=args.cadence,
        sample_seconds=args.sample_seconds, novelty=args.novelty, ts_base=args.ts_base)
    path = write_json(args.out, task)
    print_report(model, report)
    print(f"新路径已写入：{path}")
    print('仅完成离线生成；未验证服务端围栏、人脸准入或成绩有效性。')
    return 0


def collect_source_points(tasks_dirs):
    points = []
    for directory in tasks_dirs:
        for path in sorted(Path(directory).glob('*.json')):
            for coord in parse_source(path)['coords']:
                points.append(list(coord))
    return points


def build_parser():
    parser = argparse.ArgumentParser(
        description='从已有 tasklist 学习跑步点位逻辑，并生成全新路径文件（全离线）')
    sub = parser.add_subparsers(dest='command', required=True)

    learn_p = sub.add_parser('learn', help='学习一个校区的点位逻辑，输出模型 JSON')
    learn_p.add_argument('--tasks', nargs='+', required=True,
                         help='任务目录，按校区区分；可给多个')
    learn_p.add_argument('--out', required=True, help='模型输出路径')
    learn_p.add_argument('--name', default=None, help='模型名称，默认取第一个目录名')
    learn_p.add_argument('--smooth-sigma', type=float, default=1.0,
                         help='中心线环形平滑强度（采样点为单位，默认 1.0）')
    learn_p.set_defaults(func=cmd_learn)

    gen_p = sub.add_parser('generate', help='按模型生成新的 tasklist 文件')
    gen_p.add_argument('--model', help='learn 产出的模型 JSON')
    gen_p.add_argument('--tasks', nargs='+', default=None,
                       help='也可以直接给任务目录，先学习再生成')
    gen_p.add_argument('--out', required=True, help='新路径输出 JSON')
    gen_p.add_argument('--name', default=None, help='配合 --tasks 时的模型名')
    gen_p.add_argument('--smooth-sigma', type=float, default=1.0)
    gen_p.add_argument('--seed', default='20260919',
                       help='随机种子；填 auto 每次取新种子，实值会打印')
    gen_p.add_argument('--laps', type=int, default=None, help='圈数；默认用学习到的中位圈数')
    gen_p.add_argument('--distance', type=float, default=None, help='目标米数；换算成整数圈')
    gen_p.add_argument('--lane-offset-m', type=float, default=None,
                       help='车道偏移幅度；默认 0.55×走廊')
    gen_p.add_argument('--jitter-m', type=float, default=None, help='高频抖动幅度；默认取来源中位')
    gen_p.add_argument('--start-fraction', type=float, default=None,
                       help='起点在环上的相位，0～1；默认 0')
    gen_p.add_argument('--pace', type=float, default=None, help='分/公里；默认取来源中位')
    gen_p.add_argument('--cadence', type=float, default=None, help='步/分；默认取来源中位')
    gen_p.add_argument('--sample-seconds', type=int, default=1, help='采样间隔 1～5 秒')
    gen_p.add_argument('--novelty', type=float, default=0.0,
                       help='0～1；>0 时把车道偏移推向该处实测走廊的边缘，'
                            '与来源轨迹重合的部分更少（落点也更靠近观测带外沿）')
    gen_p.add_argument('--ts-base', type=int, default=0,
                       help='ts 字段的 Unix 秒起点（仅带 ts 的校区需要）；'
                            '省略时用占位起点，ts 仍严格递增')
    gen_p.set_defaults(func=cmd_generate)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'generate' and args.model and args.tasks:
            parser.error('--model 与 --tasks 只能给一个')
        return args.func(args)
    except LearnError as exc:
        print(f'失败：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

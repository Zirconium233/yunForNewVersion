#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云运动 (yunForNewVersion - Develop 分支) 操场边缘提取与拟真跑道形状 Tasks 生成器
================================================================================

功能概述：
1. 操场边缘提取 (extract_playground_boundary)：
   - 读取已有 tasks 表目录（如 tasks_fch / tasks_txl / tasks_xc）下的全部 tasklist_*.json；
   - 基于多任务空间栅格占据密度与必经打卡点 (manageList marked='Y') 自动滤除单次 GPS 漂移尖峰或偶发场外绕行（支持 core / full 两种模式）；
   - 提取贴合操场外围的非自交简单多边形边缘（支持凹包内缩精修 + 外扩安全缓冲），导出符合 yun_route.allowed_polygon 规范的 GeoJSON Polygon。

2. 基于操场边缘与『拟真操场跑道形状 + 随机起终点』的视觉差异化 Tasks 生成：
   - 操场跑道形状建模 (TrackShapeModel)：
     从提取出的操场边缘多边形自动拟合平滑闭合的操场跑道基准环（直道 + 半圆弯道平滑过渡，并提供外道、中道、内道及分场子跑道环），符合真实人类主要沿操场跑道形状跑步的习惯；
   - 随机起点 (S) 与随机终点 (E) + 4 种拟真跑道视觉变化模式（既保持跑道形状，又在人眼视觉上与原有 tasks 截然不同）：
     * track_layered_lanes : 随机起终点 + 跑道内外道分层递进/偏心环跑（外道圈 -> 中道圈 -> 内道圈平滑换道，形成明显的多层跑道环带）
     * track_chord_cut     : 随机起终点 + 沿跑道绕圈结合 1~3 次随机起终点跨场切线跑（模拟跑完整圈后斜穿草坪踩打卡点或换向继续沿跑道跑）
     * track_d_loops       : 随机起终点 + 标准大圈跑道与半场 D 形跑道环组合（沿跑道跑整圈后，在随机直道位置横切中线跑半场环）
     * track_dual_subloops : 随机起终点 + 双子跑道环/错位偏心跑道切换跑（在操场内两个错位跑道环或双操场东西跑道环之间切换绕圈）
   - 严格几何边缘约束：内置与 yun_route.validate_polygon_route 完全一致的线段-多边形求交与中点射线检测，确保所有轨迹点与连接线段 100% 位于操场边缘内部；
   - 打卡点 (manageList) 真实途经：在沿跑道跑步过程中，通过平滑切出/切入弧线自然经过每个 marked='Y' 的打卡点；
   - 协议与字段严格对齐：点项严格遵循 main.py UpPointModel 的 9 字段契约 (_POINT_FIELD_ORDER)，距离、时间、累计步数单调自洽，配速字段按 client_speed (min/km) 精确派生。
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import secrets
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# 对齐 main.py UpPointModel 9 字段声明顺序
POINT_FIELD_ORDER = (
    "isFence",
    "isMock",
    "point",
    "runMileage",
    "runStatus",
    "runStep",
    "runTime",
    "speed",
    "ts",
)

VISUAL_STYLES = (
    "track_layered_lanes",
    "track_chord_cut",
    "track_d_loops",
    "track_dual_subloops",
)


# ===========================================================================
# 1. 球面距离与局部平面投影工具（对齐 yun_route.py）
# ===========================================================================

def distance_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """计算两个经纬度点 (lon, lat) 之间的球面距离（单位：米，与 yun_route.distance 完全一致）。"""
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2.0) ** 2
    )
    return 12742000.0 * math.asin(min(1.0, math.sqrt(h)))


def client_speed(meters: float, seconds: float) -> str:
    """
    计算客户端点项 speed 字段（APK SportRunMapActivity:4140-4154：实为配速 min/km，范围 1~900）。
    与 yun_route.client_speed 完全对齐。
    """
    if meters > 0.0 and seconds > 0.0:
        pace = seconds / 60.0 / (meters / 1000.0)
        return format(min(900.0, max(1.0, pace)), ".2f")
    return "0.0"


class LocalProjector:
    """局部切平面米制坐标 (x_m, y_m) 与经纬度 (lon, lat) 互转器。"""

    def __init__(self, origin: Tuple[float, float], lat0: Optional[float] = None):
        self.origin = (float(origin[0]), float(origin[1]))
        ref_lat = self.origin[1] if lat0 is None else float(lat0)
        self.sx = 111320.0 * math.cos(math.radians(ref_lat))
        self.sy = 111320.0

    def to_xy(self, lonlat: Tuple[float, float]) -> Tuple[float, float]:
        return (
            (lonlat[0] - self.origin[0]) * self.sx,
            (lonlat[1] - self.origin[1]) * self.sy,
        )

    def to_lonlat(self, xy: Tuple[float, float]) -> Tuple[float, float]:
        return (
            round(xy[0] / self.sx + self.origin[0], 8),
            round(xy[1] / self.sy + self.origin[1], 8),
        )


# ===========================================================================
# 2. 多边形几何与边缘约束校验（对齐 yun_route.py）
# ===========================================================================

def cross(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return a[0] * b[1] - a[1] * b[0]


def on_segment(p: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> bool:
    ab = (b[0] - a[0], b[1] - a[1])
    ap = (p[0] - a[0], p[1] - a[1])
    scale = max(abs(ab[0]), abs(ab[1]), 1e-12)
    return (
        abs(cross(ab, ap)) <= 1e-13 * scale
        and min(a[0], b[0]) - 1e-12 <= p[0] <= max(a[0], b[0]) + 1e-12
        and min(a[1], b[1]) - 1e-12 <= p[1] <= max(a[1], b[1]) + 1e-12
    )


def segment_intersections(
    a: Tuple[float, float],
    b: Tuple[float, float],
    c: Tuple[float, float],
    d: Tuple[float, float],
) -> List[float]:
    """返回线段 AB 与 CD 的交点在 AB 上的参数 t ∈ [0, 1]，与 yun_route.segment_intersections 一致。"""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    qa = (c[0] - a[0], c[1] - a[1])
    det = cross(r, s)
    if abs(det) < 1e-20:
        if abs(cross(qa, r)) > 1e-20:
            return []
        rr = r[0] * r[0] + r[1] * r[1]
        if rr == 0:
            return [0.0] if on_segment(a, c, d) else []
        return [
            max(0.0, min(1.0, ((p[0] - a[0]) * r[0] + (p[1] - a[1]) * r[1]) / rr))
            for p in (c, d)
            if on_segment(p, a, b)
        ] or [
            0.0 if on_segment(a, c, d) else 1.0
            for p in (a, b)
            if on_segment(p, c, d)
        ]
    t = cross(qa, s) / det
    u = cross(qa, r) / det
    return (
        [max(0.0, min(1.0, t))]
        if -1e-12 <= t <= 1.0 + 1e-12 and -1e-12 <= u <= 1.0 + 1e-12
        else []
    )


def inside_polygon(point: Tuple[float, float], ring: Sequence[Tuple[float, float]]) -> bool:
    """射线法判定点是否在多边形内（含边界），与 yun_route.inside 一致。"""
    x, y = point
    result = False
    ring_list = list(ring)
    for a, b in zip(ring_list, ring_list[1:] + ring_list[:1]):
        if on_segment(point, a, b):
            return True
        if (a[1] > y) != (b[1] > y):
            x_int = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x < x_int:
                result = not result
    return result


def segment_inside_polygon(
    a: Tuple[float, float],
    b: Tuple[float, float],
    ring: Sequence[Tuple[float, float]],
) -> bool:
    """检查线段 AB 整段（含所有边界交点切分后的中点）是否完全位于多边形内部。"""
    if not inside_polygon(a, ring) or not inside_polygon(b, ring):
        return False
    ring_list = list(ring)
    edges = list(zip(ring_list, ring_list[1:] + ring_list[:1]))
    cuts = [0.0, 1.0]
    for c, d in edges:
        cuts.extend(segment_intersections(a, b, c, d))
    cuts = sorted(set(cuts))
    test_params = cuts[:1] + [(x + y) / 2.0 for x, y in zip(cuts, cuts[1:])] + cuts[-1:]
    for f in test_params:
        p = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        if not inside_polygon(p, ring_list):
            return False
    return True


def validate_polygon_route(
    route: Sequence[Tuple[float, float]],
    ring: Sequence[Tuple[float, float]],
) -> None:
    """严格校验整条轨迹的所有点与线段均在多边形 ring 内，超出则抛 ValueError。"""
    ring_list = list(ring)
    edges = list(zip(ring_list, ring_list[1:] + ring_list[:1]))
    for a, b in zip(route, route[1:]):
        cuts = [0.0, 1.0]
        for c, d in edges:
            cuts.extend(segment_intersections(a, b, c, d))
        cuts = sorted(set(cuts))
        for f in cuts[:1] + [(x + y) / 2.0 for x, y in zip(cuts, cuts[1:])] + cuts[-1:]:
            p = (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            if not inside_polygon(p, ring_list):
                raise ValueError("生成路线有点或线段超出操场边缘区域")


def point_to_segment_dist(
    p: Tuple[float, float],
    a: Tuple[float, float],
    b: Tuple[float, float],
) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    proj = (a[0] + t * dx, a[1] + t * dy)
    return math.hypot(p[0] - proj[0], p[1] - proj[1])


def dist_to_polygon_boundary(
    p: Tuple[float, float],
    ring: Sequence[Tuple[float, float]],
) -> float:
    ring_list = list(ring)
    return min(
        point_to_segment_dist(p, a, b)
        for a, b in zip(ring_list, ring_list[1:] + ring_list[:1])
    )


def polygon_signed_area(ring: Sequence[Tuple[float, float]]) -> float:
    if len(ring) < 3:
        return 0.0
    ring_list = list(ring)
    x0, y0 = ring_list[0]
    return 0.5 * sum(
        ((a[0] - x0) * (b[1] - y0) - (b[0] - x0) * (a[1] - y0))
        for a, b in zip(ring_list, ring_list[1:] + ring_list[:1])
    )


def is_simple_polygon(ring: Sequence[Tuple[float, float]]) -> bool:
    """验证多边形是否为非退化、无自交的简单单环多边形（对齐 yun_route.allowed_polygon）。"""
    ring_list = list(ring)
    if len(ring_list) < 3 or abs(polygon_signed_area(ring_list)) < 1e-18:
        return False
    edges = list(zip(ring_list, ring_list[1:] + ring_list[:1]))
    for i, (a, b) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            c, d = edges[j]
            if segment_intersections(a, b, c, d):
                return False
    return True


# ===========================================================================
# 3. 从已有 tasks 表产生操场边缘 (Playground Boundary Extraction)
# ===========================================================================

def convex_hull_xy(points: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Monotone Chain 凸包算法（逆时针序）。"""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return list(pts)

    def ccw(o: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: List[Tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and ccw(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper: List[Tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and ccw(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def _filter_core_playground_xy(
    trajectories: Sequence[Sequence[Tuple[float, float]]],
    checkpoints_y: Sequence[Tuple[float, float]],
    proj: LocalProjector,
    cell_size_m: float = 7.5,
    min_file_ratio: float = 0.45,
) -> List[Tuple[float, float]]:
    """
    基于多任务文件栅格共现率 + 必经打卡点锚定提取核心操场点云：
    滤除仅在个别任务中出现的 GPS 漂移尖峰或场外偶发绕行（如 tasks_xc 中少数任务的场外马路延伸）。
    """
    n_files = len(trajectories)
    min_files = max(2, int(math.ceil(n_files * min_file_ratio))) if n_files >= 3 else 1
    cell_files: Dict[Tuple[int, int], set] = {}
    all_xy: List[Tuple[float, float, int]] = []

    for f_idx, traj in enumerate(trajectories):
        for lon, lat in traj:
            x, y = proj.to_xy((lon, lat))
            all_xy.append((x, y, f_idx))
            cell = (int(math.floor(x / cell_size_m)), int(math.floor(y / cell_size_m)))
            cell_files.setdefault(cell, set()).add(f_idx)

    core_cells = {c for c, fset in cell_files.items() if len(fset) >= min_files}
    cp_xy = [proj.to_xy(p) for p in checkpoints_y]
    for px, py in cp_xy:
        c = (int(math.floor(px / cell_size_m)), int(math.floor(py / cell_size_m)))
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if (c[0] + dx, c[1] + dy) in cell_files:
                    core_cells.add((c[0] + dx, c[1] + dy))

    if not core_cells:
        return [(x, y) for x, y, _ in all_xy]

    unvisited = set(core_cells)
    clusters: List[set] = []
    while unvisited:
        seed = next(iter(unvisited))
        unvisited.remove(seed)
        comp = {seed}
        queue = [seed]
        while queue:
            cx, cy = queue.pop()
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    nb = (cx + dx, cy + dy)
                    if nb in unvisited:
                        unvisited.remove(nb)
                        comp.add(nb)
                        queue.append(nb)
        clusters.append(comp)

    best_cluster = max(
        clusters,
        key=lambda comp: (
            sum(
                1
                for px, py in cp_xy
                if any(
                    abs(int(math.floor(px / cell_size_m)) - cx) <= 2
                    and abs(int(math.floor(py / cell_size_m)) - cy) <= 2
                    for cx, cy in comp
                )
            ),
            len(comp),
        ),
    )

    filtered = [
        (x, y)
        for x, y, _ in all_xy
        if (int(math.floor(x / cell_size_m)), int(math.floor(y / cell_size_m))) in best_cluster
    ]
    for px, py in cp_xy:
        c = (int(math.floor(px / cell_size_m)), int(math.floor(py / cell_size_m)))
        if any(abs(c[0] - cx) <= 3 and abs(c[1] - cy) <= 3 for cx, cy in best_cluster):
            filtered.append((px, py))

    return filtered if len(filtered) >= 20 else [(x, y) for x, y, _ in all_xy]


def _build_buffered_boundary_xy(
    xy_pts: Sequence[Tuple[float, float]],
    buffer_m: float = 2.5,
    concave_refine: bool = True,
    max_edge_m: float = 28.0,
) -> List[Tuple[float, float]]:
    """
    由操场点云构建贴合外边缘（可选凹包内缩精修 + 顶点法线角平分线外扩缓冲），
    确保输出多边形无自交且包含操场点云。
    """
    hull = convex_hull_xy(xy_pts)
    if polygon_signed_area(hull) < 0:
        hull.reverse()

    ring = list(hull)
    if concave_refine and len(xy_pts) >= 30:
        sample_pts = list(xy_pts)[:: max(1, len(xy_pts) // 350)]
        changed = True
        iters = 0
        while changed and iters < 25:
            changed = False
            iters += 1
            n = len(ring)
            for i in range(n):
                a = ring[i]
                b = ring[(i + 1) % n]
                edge_len = math.hypot(b[0] - a[0], b[1] - a[1])
                if edge_len <= max_edge_m:
                    continue
                dx, dy = (b[0] - a[0]) / edge_len, (b[1] - a[1]) / edge_len
                nx, ny = -dy, dx  # CCW 多边形的内法线方向
                candidates: List[Tuple[float, Tuple[float, float]]] = []
                for p in xy_pts:
                    proj_t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / edge_len
                    if 0.22 <= proj_t <= 0.78:
                        inward_d = (p[0] - a[0]) * nx + (p[1] - a[1]) * ny
                        if 2.0 <= inward_d <= edge_len * 0.32:
                            candidates.append((inward_d, p))
                if candidates:
                    candidates.sort(key=lambda item: item[0])
                    cand_p = candidates[0][1]
                    test_ring = ring[: i + 1] + [cand_p] + ring[i + 1 :]
                    if is_simple_polygon(test_ring) and all(
                        inside_polygon(sp, test_ring) for sp in sample_pts
                    ):
                        ring = test_ring
                        changed = True
                        break

    n = len(ring)
    buffered: List[Tuple[float, float]] = []
    for i in range(n):
        prev_p = ring[(i - 1) % n]
        curr_p = ring[i]
        next_p = ring[(i + 1) % n]
        e1x, e1y = curr_p[0] - prev_p[0], curr_p[1] - prev_p[1]
        l1 = math.hypot(e1x, e1y) or 1.0
        n1x, n1y = e1y / l1, -e1x / l1
        e2x, e2y = next_p[0] - curr_p[0], next_p[1] - curr_p[1]
        l2 = math.hypot(e2x, e2y) or 1.0
        n2x, n2y = e2y / l2, -e2x / l2
        bx, by = n1x + n2x, n1y + n2y
        bl = math.hypot(bx, by)
        if bl < 1e-6:
            bx, by = n1x, n1y
        else:
            bx, by = bx / bl, by / bl
        scale = min(1.8, 1.0 / max(0.4, (bx * n1x + by * n1y)))
        buffered.append((curr_p[0] + bx * buffer_m * scale, curr_p[1] + by * buffer_m * scale))

    if not is_simple_polygon(buffered):
        cx = sum(p[0] for p in hull) / len(hull)
        cy = sum(p[1] for p in hull) / len(hull)
        buffered = []
        for vx, vy in hull:
            dx, dy = vx - cx, vy - cy
            norm = math.hypot(dx, dy) or 1.0
            buffered.append((vx + dx / norm * buffer_m, vy + dy / norm * buffer_m))

    return buffered


def extract_playground_boundary(
    tasks_dir: str | Path,
    mode: str = "core",
    buffer_m: float = 2.5,
) -> Dict:
    """
    从指定的 tasks 目录（如 tasks_fch / tasks_txl / tasks_xc）学习并提取操场边缘多边形与统计特征。
    """
    folder = Path(tasks_dir).resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"tasks 目录不存在: {folder}")

    json_files = sorted(folder.glob("*.json"))
    if not json_files:
        raise ValueError(f"在目录 {folder} 中未找到任何 JSON 任务文件")

    raw_lonlat: List[Tuple[float, float]] = []
    trajectories: List[List[Tuple[float, float]]] = []
    checkpoints_y: List[Tuple[float, float]] = []
    checkpoints_pool: List[Tuple[float, float]] = []
    mileages: List[float] = []
    paces: List[float] = []
    cadences: List[float] = []
    dislikes_list: List[int] = []
    manage_pool_sizes: List[int] = []

    for fpath in json_files:
        obj = json.loads(fpath.read_text(encoding="utf-8-sig"))
        data = obj.get("data") or {}
        pts_list = data.get("pointsList") or []
        if len(pts_list) < 2:
            continue
        traj: List[Tuple[float, float]] = []
        for pt in pts_list:
            lon, lat = map(float, str(pt["point"]).split(","))
            traj.append((lon, lat))
            raw_lonlat.append((lon, lat))
        trajectories.append(traj)

        mlist = data.get("manageList") or []
        if mlist:
            manage_pool_sizes.append(len(mlist))
        for item in mlist:
            p_str = str(item.get("point", "")).strip()
            if not p_str:
                continue
            lon, lat = map(float, p_str.split(","))
            checkpoints_pool.append((lon, lat))
            if str(item.get("marked", "N")).strip().upper() == "Y":
                checkpoints_y.append((lon, lat))

        if data.get("recordMileage") is not None:
            mileages.append(float(data["recordMileage"]))
        if data.get("recodePace") is not None:
            paces.append(float(data["recodePace"]))
        if data.get("recodeCadence") is not None:
            cadences.append(float(data["recodeCadence"]))
        if data.get("recodeDislikes") is not None:
            dislikes_list.append(int(data["recodeDislikes"]))

    if not trajectories:
        raise ValueError(f"目录 {folder} 中没有包含有效 pointsList 的任务文件")

    lat0 = sum(p[1] for p in raw_lonlat) / len(raw_lonlat)
    lon0 = sum(p[0] for p in raw_lonlat) / len(raw_lonlat)
    proj = LocalProjector((lon0, lat0), lat0=lat0)

    unique_cp_y = sorted(set(checkpoints_y))
    unique_cp_pool = sorted(set(checkpoints_pool))

    if mode == "core":
        xy_used = _filter_core_playground_xy(trajectories, unique_cp_y, proj)
    elif mode == "full":
        xy_used = [proj.to_xy(p) for p in raw_lonlat]
    else:
        raise ValueError(f"未知的 boundary mode: {mode!r}（仅支持 'core' 或 'full'）")

    ring_xy = _build_buffered_boundary_xy(xy_used, buffer_m=buffer_m, concave_refine=True)
    ring_lonlat = [proj.to_lonlat(p) for p in ring_xy]
    if not is_simple_polygon(ring_lonlat):
        ring_xy = _build_buffered_boundary_xy(xy_used, buffer_m=buffer_m, concave_refine=False)
        ring_lonlat = [proj.to_lonlat(p) for p in ring_xy]

    inside_cp_y = [cp for cp in unique_cp_y if inside_polygon(cp, ring_lonlat)]
    inside_cp_pool = [cp for cp in unique_cp_pool if inside_polygon(cp, ring_lonlat)]

    closed_geojson_ring = [[p[0], p[1]] for p in ring_lonlat] + [[ring_lonlat[0][0], ring_lonlat[0][1]]]
    geojson_obj = {
        "type": "FeatureCollection",
        "metadata": {
            "source_tasks_dir": folder.name,
            "mode": mode,
            "buffer_m": buffer_m,
            "vertex_count": len(ring_lonlat),
            "area_m2": round(abs(polygon_signed_area(ring_xy)), 2),
            "coordinate_system": "GCJ-02",
        },
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": f"{folder.name}_playground_boundary",
                    "mode": mode,
                    "area_m2": round(abs(polygon_signed_area(ring_xy)), 2),
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [closed_geojson_ring],
                },
            }
        ],
    }

    return {
        "tasks_dir": str(folder),
        "projector": proj,
        "ring_xy": ring_xy,
        "ring_lonlat": ring_lonlat,
        "geojson": geojson_obj,
        "trajectories": trajectories,
        "checkpoints_y": inside_cp_y,
        "checkpoints_pool": inside_cp_pool or unique_cp_pool,
        "stats": {
            "mileage_min_km": min(mileages) if mileages else 2.20,
            "mileage_max_km": max(mileages) if mileages else 3.80,
            "mileage_median_km": sorted(mileages)[len(mileages) // 2] if mileages else 2.65,
            "pace_min": max(3.5, min(paces)) if paces else 4.50,
            "pace_max": min(7.5, max(paces)) if paces else 5.50,
            "cadence_min": max(130, int(min(cadences))) if cadences else 150,
            "cadence_max": min(200, int(max(cadences))) if cadences else 180,
            "dislikes_mode": max(set(dislikes_list), key=dislikes_list.count) if dislikes_list else 3,
            "manage_list_size": max(set(manage_pool_sizes), key=manage_pool_sizes.count) if manage_pool_sizes else 5,
        },
    }


# ===========================================================================
# 4. 视觉相似度计算（2D 栅格 IoU）
# ===========================================================================

def rasterize_trajectory_cells(
    traj_xy: Sequence[Tuple[float, float]],
    cell_size_m: float = 3.0,
) -> set:
    """将平面轨迹线段栅格化为 2D 网格集合，用于计算视觉空间重合度 (IoU)。"""
    cells = set()
    for a, b in zip(traj_xy, traj_xy[1:]):
        dist = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = max(1, int(math.ceil(dist / (cell_size_m * 0.5))))
        for k in range(steps + 1):
            t = k / steps
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            cells.add((int(math.floor(x / cell_size_m)), int(math.floor(y / cell_size_m))))
    return cells


def compute_visual_iou(cells_a: set, cells_b: set) -> float:
    if not cells_a or not cells_b:
        return 0.0
    inter = len(cells_a & cells_b)
    union = len(cells_a | cells_b)
    return inter / union if union > 0 else 0.0


# ===========================================================================
# 5. 操场跑道形状建模与拟真跑道轨迹生成器 (TrackShapeModel)
# ===========================================================================

def _clamp_point_inside_ring(
    pt: Tuple[float, float],
    center: Tuple[float, float],
    ring_xy: Sequence[Tuple[float, float]],
    margin_m: float = 2.2,
) -> Tuple[float, float]:
    """确保点 pt 严格位于操场边缘 ring_xy 内部且距离边缘不少于 margin_m，必要时向 center 收缩。"""
    if inside_polygon(pt, ring_xy) and dist_to_polygon_boundary(pt, ring_xy) >= margin_m:
        return pt
    for alpha in (0.94, 0.88, 0.82, 0.75, 0.65, 0.50, 0.35, 0.20, 0.0):
        cand = (
            center[0] + (pt[0] - center[0]) * alpha,
            center[1] + (pt[1] - center[1]) * alpha,
        )
        if inside_polygon(cand, ring_xy) and dist_to_polygon_boundary(cand, ring_xy) >= margin_m * 0.7:
            return cand
    return center


class StadiumTrackLoop:
    """
    由操场边缘多边形或子区域构建的平滑闭合『操场跑道形状环』：
    - 具备标准操场跑道的直道 + 半圆弯道平滑几何特征；
    - 支持任意跑道相位 u ∈ [0, 1)、向内道宽偏移 lane_depth_m、偏心调制与顺/逆时针行进。
    """

    def __init__(
        self,
        ring_xy: Sequence[Tuple[float, float]],
        center: Tuple[float, float],
        rx: float,
        ry: float,
        superellipse_p: float = 2.35,
        n_samples: int = 240,
        margin_m: float = 3.5,
    ):
        self.ring_xy = list(ring_xy)
        self.center = center
        self.rx = rx
        self.ry = ry
        self.n_samples = n_samples
        self.margin_m = margin_m

        # 构造贴合操场边缘的平滑跑道外圈基准线（先通过射线探测边界距离，再做周期平滑）
        raw_pts: List[Tuple[float, float]] = []
        for i in range(n_samples):
            theta = 2.0 * math.pi * i / n_samples
            ct, st = math.cos(theta), math.sin(theta)
            # 超椭圆跑道基底（直道+圆弧弯道形态）
            denom = (abs(ct) ** superellipse_p + abs(st) ** superellipse_p) ** (1.0 / superellipse_p)
            ux = ct / denom if denom > 1e-9 else ct
            uy = st / denom if denom > 1e-9 else st
            ideal = (center[0] + ux * rx, center[1] + uy * ry)
            clamped = _clamp_point_inside_ring(ideal, center, self.ring_xy, margin_m=margin_m)
            raw_pts.append(clamped)

        # 周期高斯滤波平滑，使直道与弯道过渡完全光滑自然
        smoothed = list(raw_pts)
        for _ in range(10):
            nxt: List[Tuple[float, float]] = []
            for i in range(n_samples):
                p_prev2 = smoothed[(i - 2) % n_samples]
                p_prev1 = smoothed[(i - 1) % n_samples]
                p_curr = smoothed[i]
                p_next1 = smoothed[(i + 1) % n_samples]
                p_next2 = smoothed[(i + 2) % n_samples]
                sx = 0.1 * p_prev2[0] + 0.2 * p_prev1[0] + 0.4 * p_curr[0] + 0.2 * p_next1[0] + 0.1 * p_next2[0]
                sy = 0.1 * p_prev2[1] + 0.2 * p_prev1[1] + 0.4 * p_curr[1] + 0.2 * p_next1[1] + 0.1 * p_next2[1]
                nxt.append(_clamp_point_inside_ring((sx, sy), center, self.ring_xy, margin_m=margin_m * 0.9))
            smoothed = nxt

        self.base_points = smoothed
        self.inward_normals: List[Tuple[float, float]] = []
        for i in range(n_samples):
            p_prev = smoothed[(i - 1) % n_samples]
            p_next = smoothed[(i + 1) % n_samples]
            tx, ty = p_next[0] - p_prev[0], p_next[1] - p_prev[1]
            tl = math.hypot(tx, ty) or 1.0
            nx, ny = -ty / tl, tx / tl  # CCW 内法线
            # 确保法线指向中心侧
            if nx * (center[0] - smoothed[i][0]) + ny * (center[1] - smoothed[i][1]) < 0:
                nx, ny = -nx, -ny
            self.inward_normals.append((nx, ny))

        self.perimeter_m = sum(
            math.hypot(
                smoothed[(i + 1) % n_samples][0] - smoothed[i][0],
                smoothed[(i + 1) % n_samples][1] - smoothed[i][1],
            )
            for i in range(n_samples)
        )

    def point_at(
        self,
        phase_u: float,
        lane_depth_m: float = 2.0,
        shift_xy: Tuple[float, float] = (0.0, 0.0),
    ) -> Tuple[float, float]:
        """获取跑道相位 phase_u（可大于 1 跨多圈）及内道偏移 lane_depth_m 处的安全坐标。"""
        u = phase_u % 1.0
        f_idx = u * self.n_samples
        i0 = int(math.floor(f_idx)) % self.n_samples
        i1 = (i0 + 1) % self.n_samples
        frac = f_idx - math.floor(f_idx)

        bx = self.base_points[i0][0] * (1.0 - frac) + self.base_points[i1][0] * frac
        by = self.base_points[i0][1] * (1.0 - frac) + self.base_points[i1][1] * frac
        nx = self.inward_normals[i0][0] * (1.0 - frac) + self.inward_normals[i1][0] * frac
        ny = self.inward_normals[i0][1] * (1.0 - frac) + self.inward_normals[i1][1] * frac
        nl = math.hypot(nx, ny) or 1.0
        nx, ny = nx / nl, ny / nl

        max_depth = min(lane_depth_m, min(self.rx, self.ry) * 0.55)
        px = bx + nx * max_depth + shift_xy[0]
        py = by + ny * max_depth + shift_xy[1]
        return _clamp_point_inside_ring((px, py), self.center, self.ring_xy, margin_m=2.0)

    def nearest_phase(self, pt: Tuple[float, float]) -> float:
        """求任意平面点 pt 在当前跑道环上的最近相位 u ∈ [0, 1)。"""
        best_i = min(
            range(self.n_samples),
            key=lambda i: math.hypot(pt[0] - self.base_points[i][0], pt[1] - self.base_points[i][1]),
        )
        return best_i / self.n_samples


def build_playground_track_loops(ring_xy: Sequence[Tuple[float, float]]) -> Dict[str, StadiumTrackLoop]:
    """
    根据操场边缘多边形构建：
    - 'main': 贴合操场整体边缘的主跑道环
    - 'sub_a' / 'sub_b': 两个错位/分场子跑道环（对于双操场如 tasks_txl 为左右两个标准跑道，对于单操场为南北/内外错位跑道环）
    """
    xs = [p[0] for p in ring_xy]
    ys = [p[1] for p in ring_xy]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    w = max_x - min_x
    h = max_y - min_y
    cx = 0.5 * (min_x + max_x)
    cy = 0.5 * (min_y + max_y)

    main_loop = StadiumTrackLoop(
        ring_xy=ring_xy,
        center=(cx, cy),
        rx=w * 0.45,
        ry=h * 0.45,
        superellipse_p=2.4,
        margin_m=4.0,
    )

    if w > h * 1.12:
        # 横向双操场（如 tasks_txl 东西并排两个运动场）：构建左右两个独立标准跑道环
        c_left = _clamp_point_inside_ring((min_x + w * 0.27, cy), (cx, cy), ring_xy, margin_m=15.0)
        c_right = _clamp_point_inside_ring((min_x + w * 0.73, cy), (cx, cy), ring_xy, margin_m=15.0)
        sub_a = StadiumTrackLoop(
            ring_xy=ring_xy,
            center=c_left,
            rx=w * 0.21,
            ry=h * 0.41,
            superellipse_p=2.3,
            margin_m=4.0,
        )
        sub_b = StadiumTrackLoop(
            ring_xy=ring_xy,
            center=c_right,
            rx=w * 0.21,
            ry=h * 0.41,
            superellipse_p=2.3,
            margin_m=4.0,
        )
    else:
        # 纵向标准单操场（如 tasks_fch / tasks_xc）：构建偏外圈与偏内/南北错位的两个跑道环
        c_north = _clamp_point_inside_ring((cx, cy + h * 0.08), (cx, cy), ring_xy, margin_m=15.0)
        c_south = _clamp_point_inside_ring((cx, cy - h * 0.08), (cx, cy), ring_xy, margin_m=15.0)
        sub_a = StadiumTrackLoop(
            ring_xy=ring_xy,
            center=c_north,
            rx=w * 0.42,
            ry=h * 0.36,
            superellipse_p=2.3,
            margin_m=4.0,
        )
        sub_b = StadiumTrackLoop(
            ring_xy=ring_xy,
            center=c_south,
            rx=w * 0.35,
            ry=h * 0.33,
            superellipse_p=2.2,
            margin_m=5.5,
        )

    return {"main": main_loop, "sub_a": sub_a, "sub_b": sub_b}


def sample_safe_point_xy(
    ring_xy: Sequence[Tuple[float, float]],
    rng: random.Random,
    margin_m: float = 3.5,
    boundary_band: Optional[Tuple[float, float]] = None,
) -> Tuple[float, float]:
    """在操场边缘多边形内部随机采样安全点（距离边界至少 margin_m 米）。"""
    xs = [p[0] for p in ring_xy]
    ys = [p[1] for p in ring_xy]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    for _ in range(2500):
        x = rng.uniform(min_x, max_x)
        y = rng.uniform(min_y, max_y)
        p = (x, y)
        if inside_polygon(p, ring_xy):
            d = dist_to_polygon_boundary(p, ring_xy)
            if d >= margin_m:
                if boundary_band is not None:
                    lo, hi = boundary_band
                    if not (lo <= d <= hi):
                        continue
                return p

    for fallback_margin in (margin_m * 0.5, 1.0, 0.2):
        for _ in range(1000):
            p = (rng.uniform(min_x, max_x), rng.uniform(min_y, max_y))
            if inside_polygon(p, ring_xy) and dist_to_polygon_boundary(p, ring_xy) >= fallback_margin:
                return p
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def _connect_smooth_curve_xy(
    a: Tuple[float, float],
    b: Tuple[float, float],
    ring_xy: Sequence[Tuple[float, float]],
    rng: random.Random,
    margin_m: float = 2.0,
    curvature_scale: float = 0.16,
    s_curve: bool = False,
) -> List[Tuple[float, float]]:
    """在点 a 与点 b 之间生成平滑过渡弧线（用于入场/出场/切场/打卡点过渡），严格限制在操场边缘内。"""
    chord = math.hypot(b[0] - a[0], b[1] - a[1])
    if chord < 0.8:
        return [a, b]
    dx, dy = (b[0] - a[0]) / chord, (b[1] - a[1]) / chord
    nx, ny = -dy, dx

    n_steps = max(6, int(math.ceil(chord / 3.0)))
    for attempt in range(12):
        scale = curvature_scale * (0.72 ** attempt)
        if attempt == 11:
            scale = 0.0
        sign1 = rng.choice([-1.0, 1.0])
        sign2 = -sign1 if s_curve else sign1
        off1 = chord * scale * sign1 * rng.uniform(0.6, 1.1)
        off2 = chord * scale * sign2 * rng.uniform(0.6, 1.1)
        c1 = (a[0] + dx * chord * 0.33 + nx * off1, a[1] + dy * chord * 0.33 + ny * off1)
        c2 = (a[0] + dx * chord * 0.67 + nx * off2, a[1] + dy * chord * 0.67 + ny * off2)

        pts: List[Tuple[float, float]] = []
        valid = True
        for k in range(n_steps + 1):
            t = k / n_steps
            mt = 1.0 - t
            px = (mt ** 3) * a[0] + 3 * (mt ** 2) * t * c1[0] + 3 * mt * (t ** 2) * c2[0] + (t ** 3) * b[0]
            py = (mt ** 3) * a[1] + 3 * (mt ** 2) * t * c1[1] + 3 * mt * (t ** 2) * c2[1] + (t ** 3) * b[1]
            p = (px, py)
            if not inside_polygon(p, ring_xy) or dist_to_polygon_boundary(p, ring_xy) < margin_m:
                valid = False
                break
            if pts and not segment_inside_polygon(pts[-1], p, ring_xy):
                valid = False
                break
            pts.append(p)
        if valid and len(pts) >= 2:
            return pts

    return [
        (a[0] + (b[0] - a[0]) * k / n_steps, a[1] + (b[1] - a[1]) * k / n_steps)
        for k in range(n_steps + 1)
    ]


def _trace_track_arc_xy(
    track: StadiumTrackLoop,
    u_start: float,
    delta_u: float,
    lane_start_m: float,
    lane_end_m: float,
    ring_xy: Sequence[Tuple[float, float]],
    rng: random.Random,
    eccentric_amp_m: float = 1.5,
    checkpoints_to_visit: Optional[List[Tuple[float, float]]] = None,
) -> List[Tuple[float, float]]:
    """
    沿操场跑道形状 track 从相位 u_start 行进 delta_u 圈（正为逆时针，负为顺时针），
    期间道宽从 lane_start_m 平滑过渡到 lane_end_m，叠加真实人类跑道微幅摆动，
    并在跑道相位接近必经打卡点时平滑切入打卡后再切回跑道。
    """
    arc_len_est = abs(delta_u) * track.perimeter_m
    n_steps = max(16, int(math.ceil(arc_len_est / 3.2)))
    phase_ecc = rng.uniform(0.0, 2.0 * math.pi)
    weave_phase = rng.uniform(0.0, 2.0 * math.pi)

    pending_cps = list(checkpoints_to_visit or [])
    pts: List[Tuple[float, float]] = []

    for k in range(n_steps + 1):
        frac = k / n_steps
        u = u_start + delta_u * frac
        base_lane = lane_start_m + (lane_end_m - lane_start_m) * frac
        # 跑道偏心与人体自然晃动（弯道与直道微幅呼吸变化）
        ecc = eccentric_amp_m * math.sin(2.0 * math.pi * u + phase_ecc)
        weave = 0.38 * math.sin(2.0 * math.pi * 5.0 * u + weave_phase)
        lane_d = max(0.5, base_lane + ecc + weave)
        p = track.point_at(u, lane_depth_m=lane_d)
        if not pts or segment_inside_polygon(pts[-1], p, ring_xy):
            pts.append(p)

        # 检查是否有待访问打卡点恰好位于当前跑道相位附近
        if pending_cps and pts:
            curr = pts[-1]
            for cp_idx, cp in enumerate(pending_cps):
                cp_u = track.nearest_phase(cp)
                phase_diff = abs(((u % 1.0) - cp_u + 0.5) % 1.0 - 0.5)
                if phase_diff < 0.045 and segment_inside_polygon(curr, cp, ring_xy):
                    pending_cps.pop(cp_idx)
                    # 从跑道平滑切向打卡点，再平滑切回前方跑道位置
                    leg_in = _connect_smooth_curve_xy(curr, cp, ring_xy, rng, margin_m=1.5, curvature_scale=0.10)
                    for q in leg_in[1:]:
                        if segment_inside_polygon(pts[-1], q, ring_xy):
                            pts.append(q)
                    rejoin_u = u + math.copysign(min(0.08, abs(delta_u) * (1.0 - frac)), delta_u)
                    rejoin_pt = track.point_at(rejoin_u, lane_depth_m=lane_d)
                    if segment_inside_polygon(pts[-1], rejoin_pt, ring_xy):
                        leg_out = _connect_smooth_curve_xy(
                            pts[-1], rejoin_pt, ring_xy, rng, margin_m=1.5, curvature_scale=0.10
                        )
                        for q in leg_out[1:]:
                            if segment_inside_polygon(pts[-1], q, ring_xy):
                                pts.append(q)
                    break

    if checkpoints_to_visit is not None:
        checkpoints_to_visit[:] = pending_cps
    return pts


def _append_safe_leg(
    path_xy: List[Tuple[float, float]],
    leg_pts: Sequence[Tuple[float, float]],
    ring_xy: Sequence[Tuple[float, float]],
) -> float:
    """将子路径安全追加到主路径末尾，返回新增的几何长度（米）。"""
    added = 0.0
    for p in leg_pts:
        if not path_xy:
            path_xy.append(p)
            continue
        prev = path_xy[-1]
        d = math.hypot(p[0] - prev[0], p[1] - prev[1])
        if d < 0.15:
            continue
        if segment_inside_polygon(prev, p, ring_xy):
            path_xy.append(p)
            added += d
    return added


def synthesize_distinct_path_xy(
    ring_xy: Sequence[Tuple[float, float]],
    target_distance_m: float,
    checkpoints_xy: Sequence[Tuple[float, float]],
    existing_start_ends: Sequence[Tuple[Tuple[float, float], Tuple[float, float]]],
    rng: random.Random,
    style: str = "track_layered_lanes",
    margin_m: float = 3.0,
) -> Tuple[List[Tuple[float, float]], Tuple[float, float], Tuple[float, float]]:
    """
    在操场边缘 ring_xy 限制下，结合『人类主要沿操场跑道形状跑步』与『随机选择起点和终点』，
    生成一条总长度 >= target_distance_m 且与原有 tasks 在人眼视觉上完全不同的轨迹。
    """
    if style not in VISUAL_STYLES:
        style = rng.choice(VISUAL_STYLES)

    loops = build_playground_track_loops(ring_xy)
    main_track = loops["main"]
    sub_a = loops["sub_a"]
    sub_b = loops["sub_b"]

    # 1. 随机选择起点 S 与终点 E（远离原 tasks 的西侧起终点扎堆区，且 S 与 E 位于不同跑道方位/内场出入口）
    best_s: Optional[Tuple[float, float]] = None
    best_e: Optional[Tuple[float, float]] = None
    best_u_start = 0.0
    best_score = -1.0

    for _ in range(120):
        u_s = rng.uniform(0.0, 1.0)
        u_e = (u_s + rng.uniform(0.22, 0.78)) % 1.0
        # 起点与终点选在跑道不同内外道或跑道旁出入口/热身区（距离跑道 0~14m）
        cand_s = main_track.point_at(u_s, lane_depth_m=rng.uniform(0.5, 14.0))
        cand_e = main_track.point_at(u_e, lane_depth_m=rng.uniform(0.5, 14.0))
        se_dist = math.hypot(cand_e[0] - cand_s[0], cand_e[1] - cand_s[1])
        if se_dist < 45.0:
            continue
        min_pair_dist = (
            min(
                math.hypot(cand_s[0] - es[0], cand_s[1] - es[1])
                + math.hypot(cand_e[0] - ee[0], cand_e[1] - ee[1])
                for es, ee in existing_start_ends
            )
            if existing_start_ends
            else 120.0
        )
        score = min_pair_dist + 0.5 * se_dist + rng.uniform(0.0, 15.0)
        if score > best_score:
            best_score = score
            best_s, best_e = cand_s, cand_e
            best_u_start = u_s

    if best_s is None or best_e is None:
        best_u_start = rng.uniform(0.0, 1.0)
        best_s = main_track.point_at(best_u_start, lane_depth_m=2.0)
        best_e = main_track.point_at((best_u_start + 0.45) % 1.0, lane_depth_m=8.0)

    remaining_cps = list(checkpoints_xy)
    rng.shuffle(remaining_cps)

    path_xy: List[Tuple[float, float]] = [best_s]
    accum_len = 0.0

    # 随机选择主跑步方向（+1 逆时针，-1 顺时针）
    direction = rng.choice([1.0, -1.0])
    curr_u = best_u_start

    # 2. 按不同拟真跑道风格组织『沿跑道跑步 + 随机起终点换道/切场/子环过渡』
    if style == "track_layered_lanes":
        # 风格 1：随机起终点 + 跑道外道/中道/内道分层递进环跑（每 1.2~1.8 圈显著切换一次跑道半径与偏心度）
        lane_layers = [1.0, 7.5, 14.5, 4.0, 11.0]
        if rng.random() < 0.5:
            lane_layers.reverse()
        layer_idx = 0
        while accum_len < target_distance_m - 120.0 and layer_idx < 25:
            d_curr = lane_layers[layer_idx % len(lane_layers)]
            d_next = lane_layers[(layer_idx + 1) % len(lane_layers)]
            delta_u = direction * rng.uniform(1.15, 1.75)
            entry_pt = main_track.point_at(curr_u, lane_depth_m=d_curr)
            accum_len += _append_safe_leg(
                path_xy,
                _connect_smooth_curve_xy(path_xy[-1], entry_pt, ring_xy, rng, margin_m=1.8, curvature_scale=0.12),
                ring_xy,
            )
            arc_pts = _trace_track_arc_xy(
                main_track,
                u_start=curr_u,
                delta_u=delta_u,
                lane_start_m=d_curr,
                lane_end_m=d_next,
                ring_xy=ring_xy,
                rng=rng,
                eccentric_amp_m=2.2,
                checkpoints_to_visit=remaining_cps,
            )
            accum_len += _append_safe_leg(path_xy, arc_pts, ring_xy)
            curr_u = (curr_u + delta_u) % 1.0
            layer_idx += 1

    elif style == "track_chord_cut":
        # 风格 2：随机起终点 + 沿操场跑道绕圈穿插 2~3 次随机起终点斜穿切场（模拟绕圈中斜切草坪踩点/换向）
        stage = 0
        while accum_len < target_distance_m - 120.0 and stage < 20:
            lane_d = rng.uniform(1.5, 11.0)
            delta_u = direction * rng.uniform(1.10, 1.85)
            entry_pt = main_track.point_at(curr_u, lane_depth_m=lane_d)
            accum_len += _append_safe_leg(
                path_xy,
                _connect_smooth_curve_xy(path_xy[-1], entry_pt, ring_xy, rng, margin_m=1.8, curvature_scale=0.12),
                ring_xy,
            )
            arc_pts = _trace_track_arc_xy(
                main_track,
                u_start=curr_u,
                delta_u=delta_u,
                lane_start_m=lane_d,
                lane_end_m=rng.uniform(2.0, 12.0),
                ring_xy=ring_xy,
                rng=rng,
                eccentric_amp_m=1.8,
                checkpoints_to_visit=remaining_cps,
            )
            accum_len += _append_safe_leg(path_xy, arc_pts, ring_xy)
            curr_u = (curr_u + delta_u) % 1.0

            # 在跑道当前随机终点处，斜切穿过内场（优先顺路经过一个必经打卡点）到达跑道另一侧随机起点
            target_u = (curr_u + rng.uniform(0.36, 0.64)) % 1.0
            target_lane = rng.uniform(1.5, 11.0)
            chord_end = main_track.point_at(target_u, lane_depth_m=target_lane)
            if remaining_cps:
                cp = remaining_cps.pop(0)
                if segment_inside_polygon(path_xy[-1], cp, ring_xy) and segment_inside_polygon(cp, chord_end, ring_xy):
                    leg1 = _connect_smooth_curve_xy(path_xy[-1], cp, ring_xy, rng, margin_m=1.8, curvature_scale=0.14)
                    accum_len += _append_safe_leg(path_xy, leg1, ring_xy)
            if segment_inside_polygon(path_xy[-1], chord_end, ring_xy):
                leg2 = _connect_smooth_curve_xy(
                    path_xy[-1], chord_end, ring_xy, rng, margin_m=2.0, curvature_scale=0.18, s_curve=True
                )
                accum_len += _append_safe_leg(path_xy, leg2, ring_xy)
                curr_u = target_u
                # 切场后有一定概率自然切换顺/逆时针跑向
                if rng.random() < 0.5:
                    direction = -direction
            stage += 1

    elif style == "track_d_loops":
        # 风格 3：随机起终点 + 整圈标准跑道与半场 D 形圈组合（沿跑道跑整圈后，在直道中段横切中线跑半场环）
        stage = 0
        while accum_len < target_distance_m - 120.0 and stage < 22:
            lane_d = rng.uniform(1.2, 10.5)
            # 交替执行整圈跑 (1.0~1.4 圈) 与半场跑 (0.48~0.55 圈 + 横切中场弦线)
            is_half_loop = (stage % 2 == 1)
            delta_u = direction * (rng.uniform(0.46, 0.54) if is_half_loop else rng.uniform(1.05, 1.45))
            entry_pt = main_track.point_at(curr_u, lane_depth_m=lane_d)
            accum_len += _append_safe_leg(
                path_xy,
                _connect_smooth_curve_xy(path_xy[-1], entry_pt, ring_xy, rng, margin_m=1.8, curvature_scale=0.10),
                ring_xy,
            )
            arc_pts = _trace_track_arc_xy(
                main_track,
                u_start=curr_u,
                delta_u=delta_u,
                lane_start_m=lane_d,
                lane_end_m=lane_d + rng.uniform(-2.0, 3.0),
                ring_xy=ring_xy,
                rng=rng,
                eccentric_amp_m=1.6,
                checkpoints_to_visit=remaining_cps,
            )
            accum_len += _append_safe_leg(path_xy, arc_pts, ring_xy)
            curr_u = (curr_u + delta_u) % 1.0

            if is_half_loop:
                # 从跑道一侧直道横切回对称侧直道，形成鲜明的半场 D 形跑道轨迹
                across_u = (1.0 - curr_u + rng.uniform(-0.04, 0.04)) % 1.0
                across_pt = main_track.point_at(across_u, lane_depth_m=rng.uniform(2.0, 9.0))
                if segment_inside_polygon(path_xy[-1], across_pt, ring_xy):
                    bridge = _connect_smooth_curve_xy(
                        path_xy[-1], across_pt, ring_xy, rng, margin_m=2.0, curvature_scale=0.10
                    )
                    accum_len += _append_safe_leg(path_xy, bridge, ring_xy)
                    curr_u = across_u
            stage += 1

    else:  # track_dual_subloops
        # 风格 4：随机起终点 + 双子跑道环 / 错位偏心跑道切换跑（在 sub_a、sub_b 与 main 跑道环之间平滑切换绕圈）
        track_seq = [sub_a, sub_b, main_track]
        rng.shuffle(track_seq)
        stage = 0
        while accum_len < target_distance_m - 120.0 and stage < 20:
            active_track = track_seq[stage % len(track_seq)]
            u0 = active_track.nearest_phase(path_xy[-1])
            lane_d = rng.uniform(1.0, 8.5)
            entry_pt = active_track.point_at(u0, lane_depth_m=lane_d)
            if segment_inside_polygon(path_xy[-1], entry_pt, ring_xy):
                accum_len += _append_safe_leg(
                    path_xy,
                    _connect_smooth_curve_xy(path_xy[-1], entry_pt, ring_xy, rng, margin_m=1.8, curvature_scale=0.14),
                    ring_xy,
                )
            delta_u = direction * rng.uniform(1.25, 1.95)
            arc_pts = _trace_track_arc_xy(
                active_track,
                u_start=u0,
                delta_u=delta_u,
                lane_start_m=lane_d,
                lane_end_m=rng.uniform(1.5, 9.5),
                ring_xy=ring_xy,
                rng=rng,
                eccentric_amp_m=1.8,
                checkpoints_to_visit=remaining_cps,
            )
            accum_len += _append_safe_leg(path_xy, arc_pts, ring_xy)
            stage += 1

    # 3. 若仍有未顺路经过的必经打卡点，从跑道平滑切入访问后再回到跑道
    for cp in remaining_cps:
        if segment_inside_polygon(path_xy[-1], cp, ring_xy):
            leg = _connect_smooth_curve_xy(path_xy[-1], cp, ring_xy, rng, margin_m=1.5, curvature_scale=0.12)
            accum_len += _append_safe_leg(path_xy, leg, ring_xy)

    # 4. 收尾阶段：沿主跑道形状补足剩余里程，最终平滑切出并精确抵达随机终点 best_e
    curr_u = main_track.nearest_phase(path_xy[-1])
    end_u = main_track.nearest_phase(best_e)
    rem_m = max(0.0, target_distance_m - accum_len)
    exit_chord_m = math.hypot(best_e[0] - path_xy[-1][0], best_e[1] - path_xy[-1][1])

    if rem_m > exit_chord_m + 35.0:
        track_rem_u = direction * ((rem_m - 25.0) / max(100.0, main_track.perimeter_m))
        final_lane = rng.uniform(2.0, 10.0)
        rejoin_pt = main_track.point_at(curr_u, lane_depth_m=final_lane)
        if segment_inside_polygon(path_xy[-1], rejoin_pt, ring_xy):
            accum_len += _append_safe_leg(
                path_xy,
                _connect_smooth_curve_xy(path_xy[-1], rejoin_pt, ring_xy, rng, margin_m=1.8, curvature_scale=0.12),
                ring_xy,
            )
        tail_arc = _trace_track_arc_xy(
            main_track,
            u_start=curr_u,
            delta_u=track_rem_u,
            lane_start_m=final_lane,
            lane_end_m=final_lane,
            ring_xy=ring_xy,
            rng=rng,
            eccentric_amp_m=1.2,
        )
        accum_len += _append_safe_leg(path_xy, tail_arc, ring_xy)

    # 最终平滑连接至随机终点 best_e（若直接连线距離较短，确保总长严格 >= target_distance_m）
    if segment_inside_polygon(path_xy[-1], best_e, ring_xy):
        final_leg = _connect_smooth_curve_xy(path_xy[-1], best_e, ring_xy, rng, margin_m=1.8, curvature_scale=0.14)
        accum_len += _append_safe_leg(path_xy, final_leg, ring_xy)

    return path_xy, path_xy[0], path_xy[-1]


# ===========================================================================
# 6. 运动学采样与 Task JSON 组装（100% 兼容 main.py / yun_route.py / route_viewer.html）
# ===========================================================================

def _sample_trajectory_to_task_dict(
    route_lonlat: Sequence[Tuple[float, float]],
    ring_lonlat: Sequence[Tuple[float, float]],
    target_distance_m: float,
    pace_min_km: float,
    cadence_spm: int,
    sample_seconds: int,
    manage_list: List[Dict],
    dislikes_count: int,
    rng: random.Random,
    seed_val: int,
    style: str,
) -> Dict:
    """
    将经纬度几何路径按人体变速节奏采样为完整的 tasklist JSON 结构，
    并执行操场边缘与单调性校验。
    """
    cum_lengths = [0.0]
    for a, b in zip(route_lonlat, route_lonlat[1:]):
        cum_lengths.append(cum_lengths[-1] + distance_m(a, b))

    total_avail_m = cum_lengths[-1]
    actual_target_m = min(target_distance_m, total_avail_m)
    if actual_target_m < 50.0:
        raise ValueError(f"生成几何可用长度过短: {actual_target_m:.1f} m")

    nominal_duration = max(10, int(math.ceil((actual_target_m / 1000.0) * pace_min_km * 60.0)))
    ticks = list(range(0, nominal_duration, sample_seconds))
    if ticks[-1] != nominal_duration:
        ticks.append(nominal_duration)

    n_intervals = len(ticks) - 1
    phase1 = rng.uniform(0.0, 2.0 * math.pi)
    phase2 = rng.uniform(0.0, 2.0 * math.pi)
    weights: List[float] = []
    for i in range(n_intervals):
        dt = ticks[i + 1] - ticks[i]
        u = i / max(1, n_intervals)
        mod = (
            1.0
            + 0.065 * math.sin(2.0 * math.pi * 2.5 * u + phase1)
            + 0.035 * math.cos(2.0 * math.pi * 6.0 * u + phase2)
            + rng.uniform(-0.015, 0.015)
        )
        weights.append(max(0.2, dt * mod))

    weight_sum = sum(weights)
    spatial_progress = [0.0]
    acc = 0.0
    for w in weights:
        acc += (w / weight_sum) * actual_target_m
        spatial_progress.append(min(actual_target_m, acc))
    spatial_progress[-1] = actual_target_m

    def interp_at(s_m: float) -> Tuple[float, float]:
        idx = min(len(route_lonlat) - 1, max(1, bisect.bisect_left(cum_lengths, s_m)))
        p0, p1 = route_lonlat[idx - 1], route_lonlat[idx]
        seg_len = cum_lengths[idx] - cum_lengths[idx - 1]
        f = (s_m - cum_lengths[idx - 1]) / seg_len if seg_len > 1e-9 else 0.0
        f = max(0.0, min(1.0, f))
        return (
            round(p0[0] + (p1[0] - p0[0]) * f, 8),
            round(p0[1] + (p1[1] - p0[1]) * f, 8),
        )

    sampled_pts: List[Tuple[float, float]] = [interp_at(0.0)]
    valid_ticks: List[int] = [ticks[0]]

    for t_val, s_m in zip(ticks[1:], spatial_progress[1:]):
        cand = interp_at(s_m)
        prev = sampled_pts[-1]
        if not segment_inside_polygon(prev, cand, ring_lonlat):
            idx = min(len(route_lonlat) - 1, max(1, bisect.bisect_left(cum_lengths, s_m)))
            cand = route_lonlat[idx]
        if distance_m(prev, cand) < 0.05:
            continue
        sampled_pts.append(cand)
        valid_ticks.append(t_val)

    # 严格执行多边形边界验证
    validate_polygon_route(sampled_pts, ring_lonlat)

    # 计算真实累积球面距离、步数与配速（保持与存储值 100% 一致）
    actual_miles = [0.0]
    for a, b in zip(sampled_pts, sampled_pts[1:]):
        actual_miles.append(actual_miles[-1] + distance_m(a, b))

    points_list: List[Dict] = []
    cum_steps = 0
    for i, (t_sec, pt, mile_m) in enumerate(zip(valid_ticks, sampled_pts, actual_miles)):
        if i == 0:
            spd_str = "0.0"
            cum_steps = 0
        else:
            dm = mile_m - points_list[-1]["runMileage"]
            dt = t_sec - points_list[-1]["runTime"]
            spd_str = client_speed(dm, dt)
            step_inc = max(1, int(round((dt / 60.0) * cadence_spm * rng.uniform(0.95, 1.05))))
            cum_steps += step_inc

        pt_item = {
            "isFence": "Y",
            "isMock": False,
            "point": f"{pt[0]:.8f},{pt[1]:.8f}",
            "runMileage": mile_m,
            "runStatus": "1",
            "runStep": int(cum_steps),
            "runTime": int(t_sec),
            "speed": spd_str,
            "ts": "0",
        }
        assert tuple(pt_item.keys()) == POINT_FIELD_ORDER
        points_list.append(pt_item)

    total_duration = int(valid_ticks[-1])
    total_km = actual_miles[-1] / 1000.0
    final_pace = round((total_duration / 60.0) / total_km, 2) if total_km > 0 else round(pace_min_km, 2)
    final_cadence = (
        int(round(points_list[-1]["runStep"] * 60.0 / total_duration)) if total_duration > 0 else cadence_spm
    )

    return {
        "msg": "操作成功",
        "code": 200,
        "metadata": {
            "synthetic": True,
            "generator": "playground_track_shape_random_start_end",
            "visual_style": style,
            "coordinate_system": "GCJ-02",
            "seed": seed_val,
            "start_point": f"{sampled_pts[0][0]:.8f},{sampled_pts[0][1]:.8f}",
            "end_point": f"{sampled_pts[-1][0]:.8f},{sampled_pts[-1][1]:.8f}",
            "start_end_distance_m": round(distance_m(sampled_pts[0], sampled_pts[-1]), 2),
            "cadence_spm": final_cadence,
            "server_verified": False,
        },
        "data": {
            "recordMileage": round(total_km, 2),
            "recodePace": final_pace,
            "recodeCadence": final_cadence,
            "recodeDislikes": int(dislikes_count),
            "duration": total_duration,
            "manageList": manage_list,
            "pointsList": points_list,
        },
    }


def _build_manage_list(
    chosen_y: Sequence[Tuple[float, float]],
    pool_all: Sequence[Tuple[float, float]],
    total_size: int,
    rng: random.Random,
) -> List[Dict]:
    """构建与原 tasks 表结构一致的 manageList（含 marked='Y' 与 marked='N' 打卡点）。"""
    chosen_set = set(chosen_y)
    other_pts = [p for p in pool_all if p not in chosen_set]
    rng.shuffle(other_pts)

    manage_items: List[Dict] = []
    for idx, pt in enumerate(chosen_y):
        manage_items.append(
            {
                "point": f"{pt[0]:.6f},{pt[1]:.6f}".rstrip("0").rstrip("."),
                "marked": "Y",
                "index": idx,
            }
        )
    needed_n = max(0, total_size - len(manage_items))
    for idx, pt in enumerate(other_pts[:needed_n]):
        manage_items.append(
            {
                "point": f"{pt[0]:.6f},{pt[1]:.6f}".rstrip("0").rstrip("."),
                "marked": "N",
                "index": len(chosen_y) + idx,
            }
        )
    rng.shuffle(manage_items)
    return manage_items


def generate_task_suite(
    boundary_info: Dict,
    count: int = 9,
    min_km: Optional[float] = None,
    max_km: Optional[float] = None,
    min_pace: Optional[float] = None,
    max_pace: Optional[float] = None,
    min_cadence: Optional[int] = None,
    max_cadence: Optional[int] = None,
    dislikes: Optional[int] = None,
    sample_seconds: int = 2,
    style: str = "cycle",
    seed: Optional[int] = None,
    max_visual_iou: float = 0.45,
) -> List[Dict]:
    """
    根据提取的操场边缘信息 boundary_info 批量生成 count 个既符合操场跑道形状、
    又在人眼视觉上与原有 tasks 截然不同的新 Tasks。
    """
    base_seed = seed if seed is not None else secrets.randbits(32)
    rng = random.Random(base_seed)

    proj: LocalProjector = boundary_info["projector"]
    ring_xy: List[Tuple[float, float]] = boundary_info["ring_xy"]
    ring_lonlat: List[Tuple[float, float]] = boundary_info["ring_lonlat"]
    stats = boundary_info["stats"]

    km_lo = min_km if min_km is not None else max(2.15, stats["mileage_min_km"])
    km_hi = max_km if max_km is not None else max(km_lo + 0.4, stats["mileage_max_km"])
    pace_lo = min_pace if min_pace is not None else max(4.20, stats["pace_min"])
    pace_hi = max_pace if max_pace is not None else max(pace_lo + 0.4, min(6.20, stats["pace_max"]))
    cad_lo = min_cadence if min_cadence is not None else max(145, stats["cadence_min"])
    cad_hi = max_cadence if max_cadence is not None else max(cad_lo + 15, min(190, stats["cadence_max"]))
    target_dislikes = dislikes if dislikes is not None else stats["dislikes_mode"]

    orig_trajs_xy = [[proj.to_xy(p) for p in t] for t in boundary_info["trajectories"]]
    orig_cells_list = [rasterize_trajectory_cells(t_xy, cell_size_m=3.0) for t_xy in orig_trajs_xy]
    used_start_ends: List[Tuple[Tuple[float, float], Tuple[float, float]]] = [
        (t_xy[0], t_xy[-1]) for t_xy in orig_trajs_xy if len(t_xy) >= 2
    ]

    cp_y_lonlat: List[Tuple[float, float]] = boundary_info["checkpoints_y"]
    cp_pool_lonlat: List[Tuple[float, float]] = boundary_info["checkpoints_pool"]

    generated_tasks: List[Dict] = []
    for idx in range(count):
        chosen_style = (
            VISUAL_STYLES[idx % len(VISUAL_STYLES)]
            if style == "cycle"
            else (rng.choice(VISUAL_STYLES) if style == "random" else style)
        )
        target_m = rng.uniform(km_lo, km_hi) * 1000.0
        pace_val = rng.uniform(pace_lo, pace_hi)
        cadence_val = rng.randint(cad_lo, cad_hi)

        if target_dislikes > 0 and cp_y_lonlat:
            k_cp = min(target_dislikes, len(cp_y_lonlat))
            chosen_cps_lonlat = rng.sample(cp_y_lonlat, k_cp)
            chosen_cps_xy = [proj.to_xy(p) for p in chosen_cps_lonlat]
            manage_list = _build_manage_list(
                chosen_cps_lonlat, cp_pool_lonlat, max(stats["manage_list_size"], k_cp), rng
            )
            actual_dislikes = k_cp
        else:
            chosen_cps_xy = []
            manage_list = []
            actual_dislikes = 0

        best_candidate = None
        best_iou = 999.0
        for _ in range(8):
            task_seed = rng.getrandbits(32)
            sub_rng = random.Random(task_seed)
            path_xy, s_xy, e_xy = synthesize_distinct_path_xy(
                ring_xy=ring_xy,
                target_distance_m=target_m * 1.04,
                checkpoints_xy=chosen_cps_xy,
                existing_start_ends=used_start_ends,
                rng=sub_rng,
                style=chosen_style,
                margin_m=3.0,
            )
            cand_cells = rasterize_trajectory_cells(path_xy, cell_size_m=3.0)
            max_orig_iou = (
                max(compute_visual_iou(cand_cells, oc) for oc in orig_cells_list)
                if orig_cells_list
                else 0.0
            )
            if max_orig_iou < best_iou:
                best_iou = max_orig_iou
                best_candidate = (path_xy, s_xy, e_xy, task_seed, max_orig_iou)
            if max_orig_iou <= max_visual_iou:
                break

        assert best_candidate is not None
        path_xy, s_xy, e_xy, task_seed, max_orig_iou = best_candidate
        used_start_ends.append((s_xy, e_xy))
        orig_cells_list.append(rasterize_trajectory_cells(path_xy, cell_size_m=3.0))

        route_lonlat = [proj.to_lonlat(p) for p in path_xy]
        task_dict = _sample_trajectory_to_task_dict(
            route_lonlat=route_lonlat,
            ring_lonlat=ring_lonlat,
            target_distance_m=target_m,
            pace_min_km=pace_val,
            cadence_spm=cadence_val,
            sample_seconds=sample_seconds,
            manage_list=manage_list,
            dislikes_count=actual_dislikes,
            rng=random.Random(task_seed ^ 0xA5A5),
            seed_val=task_seed,
            style=chosen_style,
        )
        task_dict["metadata"]["max_visual_iou_vs_original"] = round(max_orig_iou, 4)
        generated_tasks.append(task_dict)

    return generated_tasks


# ===========================================================================
# 7. 可视化对比图生成与命令行入口
# ===========================================================================

def plot_comparison(
    boundary_info: Dict,
    generated_tasks: Sequence[Dict],
    output_png: str | Path,
) -> None:
    """绘制『原有 Tasks + 提取的操场边缘』与『单条新生成拟真跑道 Tasks 独立展示』对比图。"""
    import matplotlib.pyplot as plt

    show_tasks = list(generated_tasks)[:4]
    ncols = 1 + len(show_tasks)
    fig, axes = plt.subplots(1, ncols, figsize=(4.2 * ncols, 5.2))
    if ncols == 1:
        axes = [axes]

    ring_lonlat = boundary_info["ring_lonlat"]
    bx = [p[0] for p in ring_lonlat] + [ring_lonlat[0][0]]
    by = [p[1] for p in ring_lonlat] + [ring_lonlat[0][1]]

    ax0 = axes[0]
    for idx, traj in enumerate(boundary_info["trajectories"]):
        lons = [p[0] for p in traj]
        lats = [p[1] for p in traj]
        ax0.plot(lons, lats, alpha=0.50, linewidth=1.0, label=f"orig_{idx}" if idx < 5 else None)
        ax0.scatter([lons[0]], [lats[0]], c="green", s=22, zorder=5)
        ax0.scatter([lons[-1]], [lats[-1]], c="red", s=22, zorder=5)
    ax0.plot(bx, by, "k--", linewidth=2.0, label="Boundary")
    for cp in boundary_info["checkpoints_y"]:
        ax0.scatter([cp[0]], [cp[1]], c="purple", marker="*", s=80, zorder=8)
    ax0.set_title(f"Original ({Path(boundary_info['tasks_dir']).name})\n+ Extracted Boundary", fontsize=10)
    ax0.set_aspect("equal")
    ax0.tick_params(labelsize=7)

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]
    for idx, task in enumerate(show_tasks):
        ax = axes[idx + 1]
        ax.plot(bx, by, "k--", linewidth=1.8, label="Boundary")
        pts = [tuple(map(float, p["point"].split(","))) for p in task["data"]["pointsList"]]
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        meta = task.get("metadata", {})
        st = meta.get("visual_style", "")
        iou = meta.get("max_visual_iou_vs_original", 0.0)
        se_d = meta.get("start_end_distance_m", 0.0)
        ax.plot(lons, lats, color=colors[idx % len(colors)], alpha=0.82, linewidth=1.25)
        ax.scatter([lons[0]], [lats[0]], c="green", s=55, zorder=7, label="Start (S)")
        ax.scatter([lons[-1]], [lats[-1]], c="red", marker="X", s=60, zorder=7, label="End (E)")
        for m in task["data"].get("manageList", []):
            if m.get("marked") == "Y":
                mc = tuple(map(float, m["point"].split(",")))
                ax.scatter([mc[0]], [mc[1]], c="purple", marker="*", s=85, zorder=8)
        ax.set_title(
            f"New tasklist_{idx} ({st})\n{task['data']['recordMileage']}km | S-E={se_d:.0f}m | IoU={iou:.2f}",
            fontsize=9.5,
        )
        ax.set_aspect("equal")
        ax.tick_params(labelsize=7)
        ax.legend(fontsize=7, loc="upper right")

    plt.tight_layout()
    plt.savefig(output_png, dpi=150)
    plt.close(fig)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="根据已有 tasks 表提取操场边缘，并在边缘限制内按照操场跑道形状与随机起终点生成视觉不同的新 Tasks"
    )
    parser.add_argument(
        "-i",
        "--tasks-dir",
        type=str,
        default="tasks_fch",
        help="输入已有 tasks 表目录路径（如 tasks_fch / tasks_txl / tasks_xc，默认 tasks_fch）",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=str,
        default="tasks_generated",
        help="输出新生成 tasklist_*.json 的目录路径（默认 tasks_generated）",
    )
    parser.add_argument(
        "-b",
        "--boundary-out",
        type=str,
        default=None,
        help="可选：导出的操场边缘 GeoJSON 文件路径（默认保存在 output-dir/playground_boundary.geojson）",
    )
    parser.add_argument(
        "--boundary-mode",
        choices=("core", "full"),
        default="core",
        help="操场边缘提取模式：core（默认，过滤漂移尖峰与场外延伸）或 full（包含全部历史轨迹点）",
    )
    parser.add_argument(
        "--buffer-m",
        type=float,
        default=2.5,
        help="操场边缘外扩安全缓冲距离（米，默认 2.5）",
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=9,
        help="生成新 tasklist_*.json 的文件数量（默认 9）",
    )
    parser.add_argument(
        "--style",
        choices=("cycle", "random", *VISUAL_STYLES),
        default="cycle",
        help="跑道轨迹视觉生成风格：cycle（轮换 4 种跑道风格）、random 或指定单一风格",
    )
    parser.add_argument("--min-km", type=float, default=None, help="生成任务最小里程（公里，默认自动学习原表）")
    parser.add_argument("--max-km", type=float, default=None, help="生成任务最大里程（公里，默认自动学习原表）")
    parser.add_argument("--min-pace", type=float, default=None, help="最小配速 min/km（默认自动学习原表）")
    parser.add_argument("--max-pace", type=float, default=None, help="最大配速 min/km（默认自动学习原表）")
    parser.add_argument("--dislikes", type=int, default=None, help="必经打卡点数量（填 0 可生成无踩点要求任务）")
    parser.add_argument("--sample-seconds", type=int, default=2, help="轨迹点采样时间间隔（秒，1~5，默认 2）")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（默认每次随机生成）")
    parser.add_argument("--plot", type=str, default=None, help="可选：保存新旧轨迹与操场边缘视觉对比图 PNG 路径")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    boundary_info = extract_playground_boundary(
        tasks_dir=args.tasks_dir,
        mode=args.boundary_mode,
        buffer_m=args.buffer_m,
    )

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    boundary_path = (
        Path(args.boundary_out).resolve()
        if args.boundary_out
        else (out_dir / "playground_boundary.geojson")
    )
    boundary_path.parent.mkdir(parents=True, exist_ok=True)
    boundary_path.write_text(
        json.dumps(boundary_info["geojson"], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    tasks = generate_task_suite(
        boundary_info=boundary_info,
        count=args.count,
        min_km=args.min_km,
        max_km=args.max_km,
        min_pace=args.min_pace,
        max_pace=args.max_pace,
        dislikes=args.dislikes,
        sample_seconds=args.sample_seconds,
        style=args.style,
        seed=args.seed,
    )

    for idx, task in enumerate(tasks):
        task_path = out_dir / f"tasklist_{idx}.json"
        task_path.write_text(
            json.dumps(task, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    if args.plot:
        plot_comparison(boundary_info, tasks, args.plot)

    print(
        f"[操场边缘] 已从 {boundary_info['tasks_dir']} 提取 {len(boundary_info['ring_lonlat'])} 个边界顶点 "
        f"(面积 {boundary_info['geojson']['metadata']['area_m2']} m²) -> {boundary_path}"
    )
    print(f"[任务生成] 已在操场边缘限制内按操场跑道形状生成 {len(tasks)} 个视觉差异化任务 -> {out_dir}")
    for idx, t in enumerate(tasks):
        d = t["data"]
        m = t["metadata"]
        print(
            f"  tasklist_{idx}.json: style={m['visual_style']}, "
            f"mileage={d['recordMileage']:.2f}km, duration={d['duration']}s, "
            f"pace={d['recodePace']:.2f}, cadence={d['recodeCadence']}, "
            f"start_end_dist={m['start_end_distance_m']}m, "
            f"max_visual_IoU={m['max_visual_iou_vs_original']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

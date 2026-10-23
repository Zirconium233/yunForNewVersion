#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云运动 (yunForNewVersion - Develop 分支) 操场边缘提取与防穿墙随机拟真跑道合成器
================================================================================

设计目标（针对四大核心要求）：
1. 随机性（Randomness）：
   - 彻底摒弃固定几何模板或确定性正弦/余弦函数曲线；
   - 采用『真实跑道走廊图随机游走重组 + 全局随机起终点 + 泊松跳变目标道宽 + Ornstein-Uhlenbeck (AR1) 横向随机漫游 + 真实轨迹微观 GPS 残差块自举 (Block Bootstrap)』；
   - 不仅与原 tasks 轨迹不相似，同一底图下使用不同随机种子生成的任意多条轨迹彼此之间也完全不同。

2. 合理性与细节拟真（Realism & Micro-Detail）：
   - 宏观上严格保持『操场跑道绕圈』形态（直接基于历史真实跑道走廊网络进行多圈随机同向巡游与交叉换道，而非理想数学超椭圆），绝不产生杂乱无章的混沌折线；
   - 微观上完全移除周期性三角函数调制，由非均匀步长、AR(1) 横向随机漫游与实测 GPS 抖动残差驱动，步间航向角变化分布（|Δθ| 中位数 ~5°、P90 ~15°）与真机 GPS 轨迹高度一致。

3. 零穿墙保证（Zero Wall-Clipping / Corridor Shield）：
   - 双重几何屏障：
     a) 外层操场边界多边形（ring_lonlat / ring_xy）：通过 yun_route.validate_polygon_route 严格校验每个点与每条线段；
     b) 内层真实可通行走廊距离场（RunnableCorridorShield）：以已有 tasks 中去重加密后的核心轨迹线段构建空间网格索引，强制所有合成点与连接线段必须落在真实历史可通行跑道走廊半径（默认 ≤ 4.5m）内。对于 tasks_txl 等双操场中间隔墙、看台或禁行区，因无历史走廊覆盖，算法物理上绝不可能穿墙。

4. 协议与线上载荷对齐：
   - 点项严格遵循 main.py UpPointModel 的 9 字段顺序 (_POINT_FIELD_ORDER)，runMileage、runTime、runStep 单调自洽，speed 严格对齐 yun_route.client_speed。
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


def point_to_segment_projection(
    p: Tuple[float, float],
    a: Tuple[float, float],
    b: Tuple[float, float],
) -> Tuple[float, Tuple[float, float]]:
    """返回点 p 到线段 ab 的最短距离及最近投影点。"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1]), a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    proj = (a[0] + t * dx, a[1] + t * dy)
    return math.hypot(p[0] - proj[0], p[1] - proj[1]), proj


def point_to_segment_dist(
    p: Tuple[float, float],
    a: Tuple[float, float],
    b: Tuple[float, float],
) -> float:
    return point_to_segment_projection(p, a, b)[0]


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
# 3. 高速防穿墙真实可通行走廊屏障 (RunnableCorridorShield)
# ===========================================================================

class RunnableCorridorShield:
    """
    基于已有 tasks 核心轨迹去重锚点构建的高速『真实可通行跑道走廊屏障』：
    - 将历史可通行走廊离散化为 1.2m 空间去重锚点并建立 6m 网格索引，单次查询仅比对 5~15 个锚点（O(1) 极速响应）；
    - 强制所有合成点与连接线段必须落在真实历史可通行跑道走廊半径（≤ max_corridor_radius_m）内且位于 ring_xy 内部；
    - 彻底杜绝跨越双操场中间隔墙（如 tasks_txl）、冲出看台围栏或切入未通行建筑区（穿墙）。
    """

    def __init__(
        self,
        corridor_points_xy: Sequence[Tuple[float, float]],
        ring_xy: Sequence[Tuple[float, float]],
        max_corridor_radius_m: float = 4.6,
        boundary_margin_m: float = 0.8,
        cell_size_m: float = 6.0,
    ):
        self.ring_xy = list(ring_xy)
        self.max_corridor_radius_m = max_corridor_radius_m
        self.boundary_margin_m = boundary_margin_m
        self.cell_size_m = cell_size_m

        # 对走廊点按 1.2m 网格去重，大幅提升距离场查询速度
        dedup: Dict[Tuple[int, int], Tuple[float, float]] = {}
        for x, y in corridor_points_xy:
            key = (int(math.floor(x / 1.2)), int(math.floor(y / 1.2)))
            if key not in dedup:
                dedup[key] = (x, y)
        self.anchors = list(dedup.values())

        self.grid: Dict[Tuple[int, int], List[Tuple[float, float]]] = {}
        for ax, ay in self.anchors:
            cx = int(math.floor(ax / cell_size_m))
            cy = int(math.floor(ay / cell_size_m))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    self.grid.setdefault((cx + dx, cy + dy), []).append((ax, ay))

    def nearest_corridor_projection(
        self, p: Tuple[float, float]
    ) -> Tuple[float, Tuple[float, float]]:
        """O(1) 求点 p 到最近真实可通行走廊锚点的距离与最近点。"""
        cx = int(math.floor(p[0] / self.cell_size_m))
        cy = int(math.floor(p[1] / self.cell_size_m))
        candidates = self.grid.get((cx, cy))
        if not candidates:
            candidates = self.anchors

        best_d2 = 1e18
        best_pt = p
        px, py = p
        for ax, ay in candidates:
            d2 = (px - ax) * (px - ax) + (py - ay) * (py - ay)
            if d2 < best_d2:
                best_d2 = d2
                best_pt = (ax, ay)
        return math.sqrt(best_d2), best_pt

    def is_point_safe(
        self, p: Tuple[float, float], radius_override: Optional[float] = None
    ) -> bool:
        r_lim = self.max_corridor_radius_m if radius_override is None else radius_override
        d, _ = self.nearest_corridor_projection(p)
        if d > r_lim:
            return False
        if not inside_polygon(p, self.ring_xy):
            return False
        if dist_to_polygon_boundary(p, self.ring_xy) < self.boundary_margin_m:
            return False
        return True

    def is_segment_safe(
        self,
        a: Tuple[float, float],
        b: Tuple[float, float],
        radius_override: Optional[float] = None,
    ) -> bool:
        """检查线段 ab 全程是否既不超出外多边形，也不离开真实可通行走廊（防穿墙）。"""
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        n_checks = max(2, int(math.ceil(length / 1.8)))
        r_lim = self.max_corridor_radius_m if radius_override is None else radius_override
        for k in range(n_checks + 1):
            t = k / n_checks
            p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            d, _ = self.nearest_corridor_projection(p)
            if d > r_lim:
                return False
        # 若线段端点靠近外边界或跨度较大，执行精确多边形边求交检查
        if (
            length > 4.5
            or dist_to_polygon_boundary(a, self.ring_xy) < 4.5
            or dist_to_polygon_boundary(b, self.ring_xy) < 4.5
        ):
            return segment_inside_polygon(a, b, self.ring_xy)
        return inside_polygon(b, self.ring_xy)

    def clamp_point(
        self, p: Tuple[float, float], target_radius: Optional[float] = None
    ) -> Tuple[float, float]:
        """若点 p 偏离可通行走廊或过于靠近外边界，将其平滑拉回最近真实走廊锚点。"""
        r_lim = (self.max_corridor_radius_m * 0.85) if target_radius is None else target_radius
        d, proj = self.nearest_corridor_projection(p)
        cand = p
        if d > r_lim and d > 1e-6:
            ratio = r_lim / d
            cand = (proj[0] + (p[0] - proj[0]) * ratio, proj[1] + (p[1] - proj[1]) * ratio)
        if inside_polygon(cand, self.ring_xy) and dist_to_polygon_boundary(cand, self.ring_xy) >= self.boundary_margin_m:
            return cand
        for alpha in (0.7, 0.4, 0.0):
            test_p = (proj[0] + (cand[0] - proj[0]) * alpha, proj[1] + (cand[1] - proj[1]) * alpha)
            if inside_polygon(test_p, self.ring_xy):
                return test_p
        return proj


# ===========================================================================
# 4. 从已有 tasks 表提取操场边缘、真实走廊连通轨迹与微观残差库
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


def _resample_polyline_uniform(
    pts: Sequence[Tuple[float, float]], step_m: float = 3.0
) -> List[Tuple[float, float]]:
    """将折线按 step_m 均匀加密重采样，保证相邻点间距恒定在 ~3m。"""
    if len(pts) < 2:
        return list(pts)
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = cum[-1]
    if total < step_m:
        return list(pts)
    n = max(2, int(round(total / step_m)))
    out: List[Tuple[float, float]] = []
    for k in range(n + 1):
        s = total * k / n
        idx = min(len(pts) - 1, max(1, bisect.bisect_left(cum, s)))
        p0, p1 = pts[idx - 1], pts[idx]
        seg = cum[idx] - cum[idx - 1]
        f = (s - cum[idx - 1]) / seg if seg > 1e-9 else 0.0
        out.append((p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f))
    return out


def _filter_core_trajectories_xy(
    trajectories_xy: Sequence[Sequence[Tuple[float, float]]],
    checkpoints_xy: Sequence[Tuple[float, float]],
    cell_size_m: float = 8.0,
    min_file_ratio: float = 0.40,
) -> Tuple[List[Tuple[float, float]], List[List[Tuple[float, float]]]]:
    """
    先将每条原始轨迹加密重采样至 ~3m 步长（消除因采样间隔长导致的栅格断连），
    再基于多文件栅格共现率 + 必经打卡点锚定滤除少数任务的场外马路延伸（如 tasks_xc 的东南侧延伸）。
    """
    n_files = len(trajectories_xy)
    min_files = max(2, int(math.ceil(n_files * min_file_ratio))) if n_files >= 3 else 1

    dense_trajs: List[List[Tuple[float, float]]] = []
    cell_files: Dict[Tuple[int, int], set] = {}
    all_xy: List[Tuple[float, float]] = []

    for f_idx, raw_t in enumerate(trajectories_xy):
        clean_t = [raw_t[0]]
        for p in raw_t[1:]:
            if math.hypot(p[0] - clean_t[-1][0], p[1] - clean_t[-1][1]) <= 95.0:
                clean_t.append(p)
        dense_t = _resample_polyline_uniform(clean_t, step_m=3.0)
        dense_trajs.append(dense_t)
        for x, y in dense_t:
            all_xy.append((x, y))
            cell = (int(math.floor(x / cell_size_m)), int(math.floor(y / cell_size_m)))
            cell_files.setdefault(cell, set()).add(f_idx)

    core_cells = {c for c, fset in cell_files.items() if len(fset) >= min_files}
    for px, py in checkpoints_xy:
        c = (int(math.floor(px / cell_size_m)), int(math.floor(py / cell_size_m)))
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                if (c[0] + dx, c[1] + dy) in cell_files:
                    core_cells.add((c[0] + dx, c[1] + dy))

    if not core_cells:
        return all_xy, dense_trajs

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
                for px, py in checkpoints_xy
                if any(
                    abs(int(math.floor(px / cell_size_m)) - cx) <= 2
                    and abs(int(math.floor(py / cell_size_m)) - cy) <= 2
                    for cx, cy in comp
                )
            ),
            len(comp),
        ),
    )

    expanded_core = set()
    for cx, cy in best_cluster:
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                expanded_core.add((cx + dx, cy + dy))

    def in_core(pt: Tuple[float, float]) -> bool:
        c = (int(math.floor(pt[0] / cell_size_m)), int(math.floor(pt[1] / cell_size_m)))
        return c in expanded_core

    filtered_pts = [p for p in all_xy if in_core(p)]
    core_sub_trajs: List[List[Tuple[float, float]]] = []
    for dense_t in dense_trajs:
        curr_sub: List[Tuple[float, float]] = []
        for p in dense_t:
            if in_core(p):
                curr_sub.append(p)
            else:
                if len(curr_sub) >= 40:
                    core_sub_trajs.append(curr_sub)
                curr_sub = []
        if len(curr_sub) >= 40:
            core_sub_trajs.append(curr_sub)

    return (filtered_pts if len(filtered_pts) >= 20 else all_xy), (
        core_sub_trajs if core_sub_trajs else dense_trajs
    )


def _build_buffered_boundary_xy(
    xy_pts: Sequence[Tuple[float, float]],
    buffer_m: float = 2.0,
    concave_refine: bool = True,
    max_edge_m: float = 26.0,
) -> List[Tuple[float, float]]:
    """由核心点云构建贴合操场边缘的简单多边形（含凹包内缩精修与法线外扩缓冲）。"""
    hull = convex_hull_xy(xy_pts)
    if polygon_signed_area(hull) < 0:
        hull.reverse()

    ring = list(hull)
    if concave_refine and len(xy_pts) >= 30:
        sample_pts = list(xy_pts)[:: max(1, len(xy_pts) // 350)]
        changed = True
        iters = 0
        while changed and iters < 28:
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
                nx, ny = -dy, dx
                candidates: List[Tuple[float, Tuple[float, float]]] = []
                for p in xy_pts:
                    proj_t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / edge_len
                    if 0.20 <= proj_t <= 0.80:
                        inward_d = (p[0] - a[0]) * nx + (p[1] - a[1]) * ny
                        if 1.8 <= inward_d <= edge_len * 0.34:
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
        scale = min(1.7, 1.0 / max(0.42, (bx * n1x + by * n1y)))
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


def _extract_corridor_tracks_and_residuals(
    raw_trajs_xy: Sequence[Sequence[Tuple[float, float]]],
    core_sub_trajs: Sequence[Sequence[Tuple[float, float]]],
    ring_xy: Sequence[Tuple[float, float]],
) -> Tuple[List[List[Tuple[float, float]]], List[Tuple[float, float]]]:
    """
    提取：
    1) 平滑去噪后的长跑道走廊载体序列 (corridor_tracks)；
    2) 实测微观 GPS 抖动与步态残差库 (empirical_residuals)：用于 Block Bootstrap 注入新轨迹。
    """
    residuals: List[Tuple[float, float]] = []
    for raw_t in raw_trajs_xy:
        if len(raw_t) < 15:
            continue
        n = len(raw_t)
        for i in range(2, n - 2):
            sx = sum(raw_t[k][0] for k in range(i - 2, i + 3)) / 5.0
            sy = sum(raw_t[k][1] for k in range(i - 2, i + 3)) / 5.0
            tx = raw_t[i + 1][0] - raw_t[i - 1][0]
            ty = raw_t[i + 1][1] - raw_t[i - 1][1]
            tl = math.hypot(tx, ty)
            if 0.5 <= tl <= 30.0:
                ux, uy = tx / tl, ty / tl
                nx, ny = -uy, ux
                dx, dy = raw_t[i][0] - sx, raw_t[i][1] - sy
                lon_err = dx * ux + dy * uy
                lat_err = dx * nx + dy * ny
                if abs(lat_err) <= 2.2 and abs(lon_err) <= 2.2:
                    residuals.append((lon_err, lat_err))

    corridor_tracks: List[List[Tuple[float, float]]] = []
    for sub in core_sub_trajs:
        inside_sub = [p for p in sub if inside_polygon(p, ring_xy)]
        if len(inside_sub) < 35:
            continue
        smoothed: List[Tuple[float, float]] = []
        n = len(inside_sub)
        for i in range(n):
            lo = max(0, i - 2)
            hi = min(n, i + 3)
            w = inside_sub[lo:hi]
            p = (sum(q[0] for q in w) / len(w), sum(q[1] for q in w) / len(w))
            if inside_polygon(p, ring_xy):
                smoothed.append(p)
        resampled = _resample_polyline_uniform(smoothed, step_m=3.0)
        if len(resampled) >= 40:
            corridor_tracks.append(resampled)

    if not residuals:
        residuals = [(0.0, 0.25), (0.0, -0.25), (0.15, 0.35), (-0.15, -0.35)]
    return corridor_tracks, residuals


def extract_playground_boundary(
    tasks_dir: str | Path,
    mode: str = "core",
    buffer_m: float = 2.0,
) -> Dict:
    """
    从指定的 tasks 目录（如 tasks_fch / tasks_txl / tasks_xc）学习并提取：
    - 操场外边缘多边形 (ring_lonlat / ring_xy / geojson)
    - 防穿墙真实可通行走廊屏障 (corridor_shield)
    - 真实长跑道走廊载体库 (corridor_tracks) 与实测 GPS 微观残差库 (empirical_residuals)
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
    dt_samples: List[int] = []

    for fpath in json_files:
        obj = json.loads(fpath.read_text(encoding="utf-8-sig"))
        data = obj.get("data") or {}
        pts_list = data.get("pointsList") or []
        if len(pts_list) < 2:
            continue
        traj: List[Tuple[float, float]] = []
        prev_t: Optional[int] = None
        for pt in pts_list:
            lon, lat = map(float, str(pt["point"]).split(","))
            traj.append((lon, lat))
            raw_lonlat.append((lon, lat))
            try:
                t_val = int(float(pt.get("runTime", 0)))
                if prev_t is not None and 1 <= t_val - prev_t <= 4:
                    dt_samples.append(t_val - prev_t)
                prev_t = t_val
            except (TypeError, ValueError):
                pass
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
    trajs_xy = [[proj.to_xy(p) for p in t] for t in trajectories]
    cp_y_xy = [proj.to_xy(p) for p in unique_cp_y]

    if mode == "core":
        xy_used, core_sub_trajs = _filter_core_trajectories_xy(trajs_xy, cp_y_xy)
    elif mode == "full":
        xy_used = [proj.to_xy(p) for p in raw_lonlat]
        core_sub_trajs = [_resample_polyline_uniform(t, step_m=3.0) for t in trajs_xy]
    else:
        raise ValueError(f"未知的 boundary mode: {mode!r}（仅支持 'core' 或 'full'）")

    ring_xy = _build_buffered_boundary_xy(xy_used, buffer_m=buffer_m, concave_refine=True)
    ring_lonlat = [proj.to_lonlat(p) for p in ring_xy]
    if not is_simple_polygon(ring_lonlat):
        ring_xy = _build_buffered_boundary_xy(xy_used, buffer_m=buffer_m, concave_refine=False)
        ring_lonlat = [proj.to_lonlat(p) for p in ring_xy]

    corridor_tracks, empirical_residuals = _extract_corridor_tracks_and_residuals(
        trajs_xy, core_sub_trajs, ring_xy
    )

    corridor_pts: List[Tuple[float, float]] = []
    for ct in corridor_tracks:
        corridor_pts.extend(ct)

    corridor_shield = RunnableCorridorShield(
        corridor_points_xy=corridor_pts,
        ring_xy=ring_xy,
        max_corridor_radius_m=4.6,
        boundary_margin_m=0.8,
    )

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
        "corridor_shield": corridor_shield,
        "corridor_tracks": corridor_tracks,
        "empirical_residuals": empirical_residuals,
        "dt_samples": dt_samples or [1, 2, 2, 2],
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
# 5. 视觉相似度计算（2D 栅格 IoU）
# ===========================================================================

def rasterize_trajectory_cells(
    traj_xy: Sequence[Tuple[float, float]],
    cell_size_m: float = 2.5,
) -> set:
    """将平面轨迹线段栅格化为 2D 网格集合，用于评估轨迹间的空间重合度 (IoU)。"""
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
# 6. 防穿墙随机过程拟真绕圈合成算法 (Corridor Graph Walk + AR1 + Block Bootstrap)
# ===========================================================================

def synthesize_stochastic_track_path_xy(
    boundary_info: Dict,
    target_distance_m: float,
    checkpoints_xy: Sequence[Tuple[float, float]],
    existing_start_ends: Sequence[Tuple[Tuple[float, float], Tuple[float, float]]],
    rng: random.Random,
) -> Tuple[List[Tuple[float, float]], Tuple[float, float], Tuple[float, float]]:
    """
    核心轨迹合成引擎：
    1) 在加密重采样后的真实跑道走廊图 (corridor_tracks) 上执行带惯性的随机游走；
    2) 在跑道走廊基底上施加非周期的『泊松跳变目标道宽 + Ornstein-Uhlenbeck (AR1) 横向随机漫游 + 实测 GPS 残差块自举』，
       彻底消除函数曲线的机械规律感；
    3) 每一步均由 RunnableCorridorShield 与多边形边界双重钳制，保证 0 穿墙。
    """
    shield: RunnableCorridorShield = boundary_info["corridor_shield"]
    tracks: List[List[Tuple[float, float]]] = boundary_info["corridor_tracks"]
    residuals_pool: List[Tuple[float, float]] = boundary_info["empirical_residuals"]

    node_grid: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    grid_sz = 6.0
    for t_idx, trk in enumerate(tracks):
        for p_idx, (x, y) in enumerate(trk):
            cell = (int(math.floor(x / grid_sz)), int(math.floor(y / grid_sz)))
            node_grid.setdefault(cell, []).append((t_idx, p_idx))

    # 1. 随机挑选远离已有起终点的起始节点 (cur_t, cur_i, cur_dir)
    best_t, best_i, best_dir = 0, 0, 1
    best_s = tracks[0][0]
    best_score = -1.0

    for _ in range(80):
        t_idx = rng.randrange(len(tracks))
        trk = tracks[t_idx]
        p_idx = rng.randrange(len(trk))
        d_sign = rng.choice([1, -1])
        base_p = trk[p_idx]
        cand_s = shield.clamp_point(
            (base_p[0] + rng.uniform(-3.2, 3.2), base_p[1] + rng.uniform(-3.2, 3.2)),
            target_radius=3.8,
        )
        min_d = (
            min(math.hypot(cand_s[0] - es[0], cand_s[1] - es[1]) for es, _ in existing_start_ends)
            if existing_start_ends
            else 100.0
        )
        score = min_d + rng.uniform(0.0, 15.0)
        if score > best_score:
            best_score = score
            best_t, best_i, best_dir = t_idx, p_idx, d_sign
            best_s = cand_s

    # 2. 在跑道走廊图上生成连续无穿墙的载体序列 carrier_pts
    carrier_pts: List[Tuple[float, float]] = [tracks[best_t][best_i]]
    carrier_len = 0.0
    cur_t, cur_i, cur_dir = best_t, best_i, best_dir
    steps_since_switch = 0
    cooldown_switch = rng.randint(25, 75)

    max_carrier_steps = int(target_distance_m / 2.2) + 800
    for _ in range(max_carrier_steps):
        if carrier_len >= target_distance_m + 180.0:
            break

        trk = tracks[cur_t]
        next_i = cur_i + cur_dir
        steps_since_switch += 1

        need_endpoint_switch = not (0 <= next_i < len(trk))
        want_random_switch = steps_since_switch >= cooldown_switch and rng.random() < 0.35

        if need_endpoint_switch or want_random_switch:
            curr_p = carrier_pts[-1]
            prev_p = carrier_pts[-2] if len(carrier_pts) >= 2 else curr_p
            curr_heading = math.atan2(curr_p[1] - prev_p[1], curr_p[0] - prev_p[0])

            cx = int(math.floor(curr_p[0] / grid_sz))
            cy = int(math.floor(curr_p[1] / grid_sz))
            neighbor_nodes = list(node_grid.get((cx, cy), []))
            if len(neighbor_nodes) < 6:
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx != 0 or dy != 0:
                            neighbor_nodes.extend(node_grid.get((cx + dx, cy + dy), []))

            if len(neighbor_nodes) > 24:
                neighbor_nodes = rng.sample(neighbor_nodes, 24)
            else:
                rng.shuffle(neighbor_nodes)

            switched = False
            for cand_t, cand_i in neighbor_nodes:
                if cand_t == cur_t and abs(cand_i - cur_i) < 15:
                    continue
                cand_trk = tracks[cand_t]
                cand_p = cand_trk[cand_i]
                dist_jump = math.hypot(cand_p[0] - curr_p[0], cand_p[1] - curr_p[1])
                if dist_jump > 5.5 or not shield.is_segment_safe(curr_p, cand_p):
                    continue

                for cand_dir in (cur_dir, -cur_dir):
                    probe_i = cand_i + cand_dir * 3
                    if not (0 <= probe_i < len(cand_trk)):
                        continue
                    probe_p = cand_trk[probe_i]
                    new_heading = math.atan2(probe_p[1] - cand_p[1], probe_p[0] - cand_p[0])
                    ang_diff = abs((new_heading - curr_heading + math.pi) % (2.0 * math.pi) - math.pi)
                    if ang_diff <= math.radians(60.0) or (need_endpoint_switch and ang_diff <= math.radians(120.0)):
                        cur_t, cur_i, cur_dir = cand_t, cand_i, cand_dir
                        steps_since_switch = 0
                        cooldown_switch = rng.randint(25, 80)
                        switched = True
                        break
                if switched:
                    break

            if not switched and need_endpoint_switch:
                cur_dir = -cur_dir
                next_i = cur_i + cur_dir
                steps_since_switch = 0
                cooldown_switch = rng.randint(30, 80)
            else:
                next_i = cur_i + cur_dir

        if not (0 <= next_i < len(tracks[cur_t])):
            cur_dir = -cur_dir
            next_i = max(0, min(len(tracks[cur_t]) - 1, cur_i + cur_dir))

        next_p = tracks[cur_t][next_i]
        cur_i = next_i
        step_d = math.hypot(next_p[0] - carrier_pts[-1][0], next_p[1] - carrier_pts[-1][1])
        if step_d >= 0.5 and shield.is_segment_safe(carrier_pts[-1], next_p):
            carrier_pts.append(next_p)
            carrier_len += step_d

    # 3. 在走廊载体序列上施加『泊松随机变道 + AR(1) 横向漫游 + 实测 GPS 残差块自举』
    synthesized_xy: List[Tuple[float, float]] = [
        best_s if shield.is_segment_safe(best_s, carrier_pts[0]) else carrier_pts[0]
    ]
    accum_len = 0.0

    lat_offset = rng.uniform(-2.6, 2.6)
    target_lane = rng.uniform(-3.6, 3.6)
    steps_until_lane_change = rng.randint(10, 42)
    lon_jitter = 0.0

    res_idx = rng.randrange(len(residuals_pool))
    res_block_rem = rng.randint(6, 18)
    res_sign = rng.choice([-1.0, 1.0])

    n_carrier = len(carrier_pts)
    for i in range(1, n_carrier):
        p_prev = carrier_pts[i - 1]
        p_curr = carrier_pts[i]
        p_next = carrier_pts[min(n_carrier - 1, i + 1)]

        tx, ty = p_next[0] - p_prev[0], p_next[1] - p_prev[1]
        tl = math.hypot(tx, ty)
        if tl < 1e-6:
            continue
        ux, uy = tx / tl, ty / tl
        nx, ny = -uy, ux

        steps_until_lane_change -= 1
        if steps_until_lane_change <= 0:
            target_lane = rng.uniform(-3.8, 3.8)
            steps_until_lane_change = rng.randint(10, 48)

        reversion_rate = rng.uniform(0.09, 0.24)
        lat_offset += reversion_rate * (target_lane - lat_offset) + rng.gauss(0.0, 0.38)
        lat_offset = max(-4.0, min(4.0, lat_offset))

        res_block_rem -= 1
        if res_block_rem <= 0:
            res_idx = rng.randrange(len(residuals_pool))
            res_block_rem = rng.randint(6, 18)
            res_sign = rng.choice([-1.0, 1.0])
        else:
            res_idx = (res_idx + 1) % len(residuals_pool)
        emp_lon, emp_lat = residuals_pool[res_idx]

        lon_jitter = 0.50 * lon_jitter + rng.gauss(0.0, 0.20) + emp_lon * 0.70
        lon_jitter = max(-1.1, min(1.1, lon_jitter))
        micro_lat = emp_lat * res_sign * rng.uniform(0.80, 1.25)

        total_lat = lat_offset + micro_lat
        raw_cand = (
            p_curr[0] + nx * total_lat + ux * lon_jitter,
            p_curr[1] + ny * total_lat + uy * lon_jitter,
        )

        prev_out = synthesized_xy[-1]
        accepted_pt: Optional[Tuple[float, float]] = None
        for shrink in (1.0, 0.5, 0.0):
            blend = (
                p_curr[0] + (raw_cand[0] - p_curr[0]) * shrink,
                p_curr[1] + (raw_cand[1] - p_curr[1]) * shrink,
            )
            clamped = shield.clamp_point(blend, target_radius=4.2 * shrink + 0.5)
            if shield.is_segment_safe(prev_out, clamped):
                accepted_pt = clamped
                break

        if accepted_pt is None:
            if segment_inside_polygon(prev_out, p_curr, shield.ring_xy):
                accepted_pt = p_curr
            else:
                continue

        step_d = math.hypot(accepted_pt[0] - prev_out[0], accepted_pt[1] - prev_out[1])
        if step_d < 0.35:
            continue
        synthesized_xy.append(accepted_pt)
        accum_len += step_d

        if accum_len >= target_distance_m:
            se_dist = math.hypot(
                synthesized_xy[-1][0] - synthesized_xy[0][0],
                synthesized_xy[-1][1] - synthesized_xy[0][1],
            )
            if se_dist >= 45.0 or accum_len >= target_distance_m + 140.0:
                break

    return synthesized_xy, synthesized_xy[0], synthesized_xy[-1]


# ===========================================================================
# 7. 运动学采样与 Task JSON 组装（100% 兼容 main.py / yun_route.py / route_viewer.html）
# ===========================================================================

def _sample_trajectory_to_task_dict(
    route_lonlat: Sequence[Tuple[float, float]],
    ring_lonlat: Sequence[Tuple[float, float]],
    pace_min_km: float,
    cadence_spm: int,
    dt_pool: Sequence[int],
    manage_list: List[Dict],
    dislikes_count: int,
    rng: random.Random,
    seed_val: int,
) -> Dict:
    """
    将经纬度轨迹按非周期随机步频与变速过程封装为 tasklist JSON，
    并通过 validate_polygon_route 与字段单调性检验。
    """
    cleaned_pts: List[Tuple[float, float]] = [route_lonlat[0]]
    for p in route_lonlat[1:]:
        if distance_m(cleaned_pts[-1], p) >= 0.25 and segment_inside_polygon(cleaned_pts[-1], p, ring_lonlat):
            cleaned_pts.append(p)

    validate_polygon_route(cleaned_pts, ring_lonlat)

    actual_miles = [0.0]
    for a, b in zip(cleaned_pts, cleaned_pts[1:]):
        actual_miles.append(actual_miles[-1] + distance_m(a, b))

    total_m = actual_miles[-1]
    if total_m < 50.0:
        raise ValueError(f"生成几何可用长度过短: {total_m:.1f} m")

    target_total_s = (total_m / 1000.0) * pace_min_km * 60.0
    raw_dt_floats: List[float] = []
    pace_state = 1.0
    for i in range(1, len(cleaned_pts)):
        dm = actual_miles[i] - actual_miles[i - 1]
        turn_slowdown = 1.0
        if 1 <= i < len(cleaned_pts) - 1:
            p0, p1, p2 = cleaned_pts[i - 1], cleaned_pts[i], cleaned_pts[i + 1]
            a1 = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
            a2 = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
            da = abs((a2 - a1 + math.pi) % (2.0 * math.pi) - math.pi)
            turn_slowdown = 1.0 + 0.18 * min(1.0, da / (math.pi * 0.5))

        pace_state = 0.88 * pace_state + 0.12 * 1.0 + rng.gauss(0.0, 0.045)
        pace_state = max(0.78, min(1.28, pace_state))
        raw_dt_floats.append(dm * pace_state * turn_slowdown)

    scale_t = target_total_s / max(1e-6, sum(raw_dt_floats))
    valid_ticks: List[int] = [0]
    acc_t = 0.0
    for f_dt in raw_dt_floats:
        acc_t += f_dt * scale_t
        next_t = max(valid_ticks[-1] + 1, int(round(acc_t)))
        valid_ticks.append(next_t)

    points_list: List[Dict] = []
    cum_steps = 0
    cadence_state = float(cadence_spm)

    for i, (t_sec, pt, mile_m) in enumerate(zip(valid_ticks, cleaned_pts, actual_miles)):
        if i == 0:
            spd_str = "0.0"
            cum_steps = 0
        else:
            dm = mile_m - points_list[-1]["runMileage"]
            dt = t_sec - points_list[-1]["runTime"]
            spd_str = client_speed(dm, dt)
            cadence_state = 0.85 * cadence_state + 0.15 * cadence_spm + rng.gauss(0.0, 2.5)
            cadence_state = max(125.0, min(205.0, cadence_state))
            step_inc = max(1, int(round((dt / 60.0) * cadence_state)))
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
            "generator": "corridor_stochastic_empirical_track",
            "visual_style": "stochastic_track_ar1",
            "coordinate_system": "GCJ-02",
            "seed": seed_val,
            "start_point": f"{cleaned_pts[0][0]:.8f},{cleaned_pts[0][1]:.8f}",
            "end_point": f"{cleaned_pts[-1][0]:.8f},{cleaned_pts[-1][1]:.8f}",
            "start_end_distance_m": round(distance_m(cleaned_pts[0], cleaned_pts[-1]), 2),
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
    style: str = "stochastic",
    seed: Optional[int] = None,
    max_visual_iou: float = 0.42,
) -> List[Dict]:
    """
    根据提取的操场边缘与可通行走廊信息 boundary_info 批量生成 count 个新 Tasks：
    - 保证与原轨迹不相似，且不同随机种子生成的轨迹彼此也不相似。
    """
    base_seed = seed if seed is not None else secrets.randbits(32)
    rng = random.Random(base_seed)

    proj: LocalProjector = boundary_info["projector"]
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
    orig_cells_list = [rasterize_trajectory_cells(t_xy, cell_size_m=2.5) for t_xy in orig_trajs_xy]
    synth_cells_list: List[set] = []
    used_start_ends: List[Tuple[Tuple[float, float], Tuple[float, float]]] = [
        (t_xy[0], t_xy[-1]) for t_xy in orig_trajs_xy if len(t_xy) >= 2
    ]

    cp_y_lonlat: List[Tuple[float, float]] = boundary_info["checkpoints_y"]
    cp_pool_lonlat: List[Tuple[float, float]] = boundary_info["checkpoints_pool"]

    generated_tasks: List[Dict] = []
    for idx in range(count):
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
        best_score = 999.0
        for _ in range(3):
            task_seed = rng.getrandbits(32)
            sub_rng = random.Random(task_seed)
            path_xy, s_xy, e_xy = synthesize_stochastic_track_path_xy(
                boundary_info=boundary_info,
                target_distance_m=target_m,
                checkpoints_xy=chosen_cps_xy,
                existing_start_ends=used_start_ends,
                rng=sub_rng,
            )
            cand_cells = rasterize_trajectory_cells(path_xy, cell_size_m=2.5)
            max_orig_iou = (
                max(compute_visual_iou(cand_cells, oc) for oc in orig_cells_list)
                if orig_cells_list
                else 0.0
            )
            max_synth_iou = (
                max(compute_visual_iou(cand_cells, sc) for sc in synth_cells_list)
                if synth_cells_list
                else 0.0
            )
            combined_iou = max(max_orig_iou, max_synth_iou)
            if combined_iou < best_score:
                best_score = combined_iou
                best_candidate = (path_xy, s_xy, e_xy, task_seed, max_orig_iou, max_synth_iou, cand_cells)
            if combined_iou <= max_visual_iou:
                break

        assert best_candidate is not None
        path_xy, s_xy, e_xy, task_seed, max_orig_iou, max_synth_iou, cand_cells = best_candidate
        used_start_ends.append((s_xy, e_xy))
        synth_cells_list.append(cand_cells)

        route_lonlat = [proj.to_lonlat(p) for p in path_xy]
        task_dict = _sample_trajectory_to_task_dict(
            route_lonlat=route_lonlat,
            ring_lonlat=ring_lonlat,
            pace_min_km=pace_val,
            cadence_spm=cadence_val,
            dt_pool=boundary_info["dt_samples"],
            manage_list=manage_list,
            dislikes_count=actual_dislikes,
            rng=random.Random(task_seed ^ 0xA5A5),
            seed_val=task_seed,
        )
        task_dict["metadata"]["max_visual_iou_vs_original"] = round(max_orig_iou, 4)
        task_dict["metadata"]["max_visual_iou_vs_siblings"] = round(max_synth_iou, 4)
        generated_tasks.append(task_dict)

    return generated_tasks


# ===========================================================================
# 8. 可视化对比图生成与命令行入口
# ===========================================================================

def plot_comparison(
    boundary_info: Dict,
    generated_tasks: Sequence[Dict],
    output_png: str | Path,
) -> None:
    """绘制『单条原轨迹实测细节』与『多个不同随机种子生成的拟真跑道轨迹』对比图。"""
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
        if idx == 0:
            ax0.plot(lons, lats, color="#333333", alpha=0.88, linewidth=1.15, label="Orig #0")
            ax0.scatter([lons[0]], [lats[0]], c="green", s=50, zorder=7)
            ax0.scatter([lons[-1]], [lats[-1]], c="red", marker="X", s=55, zorder=7)
        else:
            ax0.plot(lons, lats, color="#999999", alpha=0.22, linewidth=0.8)
    ax0.plot(bx, by, "k--", linewidth=1.9, label="Boundary")
    for cp in boundary_info["checkpoints_y"]:
        ax0.scatter([cp[0]], [cp[1]], c="purple", marker="*", s=80, zorder=8)
    ax0.set_title(f"Original #0 + Corridor ({Path(boundary_info['tasks_dir']).name})", fontsize=9.5)
    ax0.set_aspect("equal")
    ax0.tick_params(labelsize=7)
    ax0.legend(fontsize=7, loc="upper right")

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]
    for idx, task in enumerate(show_tasks):
        ax = axes[idx + 1]
        ax.plot(bx, by, "k--", linewidth=1.8, label="Boundary")
        pts = [tuple(map(float, p["point"].split(","))) for p in task["data"]["pointsList"]]
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        meta = task.get("metadata", {})
        seed_v = meta.get("seed", 0)
        iou_o = meta.get("max_visual_iou_vs_original", 0.0)
        iou_s = meta.get("max_visual_iou_vs_siblings", 0.0)
        se_d = meta.get("start_end_distance_m", 0.0)
        ax.plot(lons, lats, color=colors[idx % len(colors)], alpha=0.82, linewidth=1.15)
        ax.scatter([lons[0]], [lats[0]], c="green", s=55, zorder=7, label="Start (S)")
        ax.scatter([lons[-1]], [lats[-1]], c="red", marker="X", s=60, zorder=7, label="End (E)")
        for m in task["data"].get("manageList", []):
            if m.get("marked") == "Y":
                mc = tuple(map(float, m["point"].split(",")))
                ax.scatter([mc[0]], [mc[1]], c="purple", marker="*", s=85, zorder=8)
        ax.set_title(
            f"Seed #{idx+1} ({seed_v % 100000})\n{task['data']['recordMileage']}km | S-E={se_d:.0f}m | IoU={iou_o:.2f}/{iou_s:.2f}",
            fontsize=9.2,
        )
        ax.set_aspect("equal")
        ax.tick_params(labelsize=7)
        ax.legend(fontsize=7, loc="upper right")

    plt.tight_layout()
    plt.savefig(output_png, dpi=150)
    plt.close(fig)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="基于已有 tasks 表提取操场边缘与防穿墙走廊，通过非周期随机游走与经验微观残差生成拟真绕圈 Tasks"
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
        default=2.0,
        help="操场边缘外扩安全缓冲距离（米，默认 2.0）",
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
        type=str,
        default="stochastic",
        help="保留兼容参数（默认 stochastic：全随机经验走廊绕圈合成）",
    )
    parser.add_argument("--min-km", type=float, default=None, help="生成任务最小里程（公里，默认自动学习原表）")
    parser.add_argument("--max-km", type=float, default=None, help="生成任务最大里程（公里，默认自动学习原表）")
    parser.add_argument("--min-pace", type=float, default=None, help="最小配速 min/km（默认自动学习原表）")
    parser.add_argument("--max-pace", type=float, default=None, help="最大配速 min/km（默认自动学习原表）")
    parser.add_argument("--dislikes", type=int, default=None, help="必经打卡点数量（填 0 可生成无踩点要求任务）")
    parser.add_argument("--sample-seconds", type=int, default=2, help="基准采样时间间隔（秒，默认 2）")
    parser.add_argument("--seed", type=int, default=None, help="主随机种子（默认每次随机生成，子任务自动派生独立种子）")
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
        f"[操场边缘与走廊] 已从 {boundary_info['tasks_dir']} 提取 {len(boundary_info['ring_lonlat'])} 个边界顶点 "
        f"(面积 {boundary_info['geojson']['metadata']['area_m2']} m²，走廊轨迹 {len(boundary_info['corridor_tracks'])} 条，"
        f"实测微观残差样本 {len(boundary_info['empirical_residuals'])} 个) -> {boundary_path}"
    )
    print(f"[任务生成] 已在防穿墙走廊限制内生成 {len(tasks)} 个随机拟真跑道任务 -> {out_dir}")
    for idx, t in enumerate(tasks):
        d = t["data"]
        m = t["metadata"]
        print(
            f"  tasklist_{idx}.json: seed={m['seed']}, "
            f"mileage={d['recordMileage']:.2f}km, duration={d['duration']}s, "
            f"pace={d['recodePace']:.2f}, cadence={d['recodeCadence']}, "
            f"start_end_dist={m['start_end_distance_m']}m, "
            f"IoU_orig={m['max_visual_iou_vs_original']:.3f}, IoU_sibling={m['max_visual_iou_vs_siblings']:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

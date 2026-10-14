# 轨迹合成模式（deform）

此模式把一条**已有轨迹**形变成一条新的二维轨迹，并补齐上传所需的时间、累计距离、步数与配速。它不识别地图、不做路径规划，也不判断道路是否可通行：输出形状完全由来源轨迹 + 形变参数 + 种子决定。底图和参数不能证明实际运动发生，也不能保证服务端接受或成绩长期有效。

算法（`yun_route.py`）：按弧长把来源重采样成 5.5m 等间距折线 → 沿法线做 OU 低频游走（跨道摆动）→ 叠加 x/y 二维相关漂移（定位噪声尺度）→ 叠加稀疏、指数衰减的偶发大偏移（约 1.2 次/km）→ 可选把几何长度校准到来源的 +3%。该实现与历史演示包同源，回归基准钉在 `tests/test_yun_route.py::test_deform_matches_demo_scale`（fch 来源 686 点 / 3.877 km）。

## 离线生成与配置

仅生成文件，无须账号，也不会读取 `config.ini`：

```powershell
python tools/generate_route.py --config examples/routes/deform_xc.json --output work_dir/route_preview.geojson
```

输出 GeoJSON 和同名 `route_preview.task.json`（逐点与汇总检查用，`ts=0` 是尚未运行的占位符）。输出文件已存在时会拒绝覆盖。可用支持 GeoJSON 的本地查看器预览，不要把调试文件放入 `tasks_*` 后通过旧打表入口运行。

`examples/routes/deform_*.json` 是配置模板。`source_json` 等相对**路线配置文件所在目录**解析；命令行 `--route-config` 相对**启动命令时所在目录**解析。

| 配置项 | 示例/默认 | 含义 |
|---|---|---|
| `source_json` | `../../tasks_xc/tasklist_4.json` | 形变来源，**任选**：tasklist（`data.pointsList[*].point`）、GeoJSON LineString/FeatureCollection、裸坐标列表 `[[lon,lat],…]` |
| `coordinate_system` | `GCJ-02` | 必须明确声明；不执行坐标转换。离线工具也支持 WGS84，当前主流程要求 GCJ-02 |
| `distance_m` | 不设置 | **精确**总里程（米）：给了就钉住该值、不再抽签；不得超过形变后可用长度 |
| `min_distance_m` / `max_distance_m` | 不设置 | 里程区间：不给 `distance_m` 时总里程 = 来源里程 ×(1±`length_offset_pct`) 抽签，必须落在区间内，否则换种子重抽，连续 5 次不中直接报错 |
| `pace_offset_pct` / `cadence_offset_pct` / `length_offset_pct` | 0.10 | 三个参数各自的抽签幅度（0~0.5）；设 0 关闭抽签 |
| `pace_min_km` | 来源实测值 | 配速基准：**写了以 cfg 为准**，没写则取来源轨迹实测总体配速；来源无节奏字段时用 6.0 |
| `cadence_spm` | 来源实测值 | 步频基准：同上；来源步频越界（合理区间 110~220 步/分）或字段缺失时退回 160 |
| `sample_seconds` | 1 | 发送间隔，整数 1～5 秒；与里程、配速严格自洽（每点里程 = 间隔 ÷ 速度） |
| `seed` | 31337 | 固定种子可复现；填 `"auto"` 每次新种子，实值写入 `metadata.seed`；重抽时按 `seed + 1000003×n` 递进 |
| `allowed_polygon_geojson` | 不设置 | 可选的单环 GeoJSON Polygon 围栏：给了就逐段校验，不给则不校验 |
| `deform_profile` | 不设置 | 覆盖形变参数（下表），未知键与越界值都会报错 |

### 三个参数的一致性规则

总里程、配速、步频各自**独立抽签**（默认各 ±10%），但生成出的点列必须自洽，不能逐点乱偏：

- 采样采用「等时间间隔 + 等里程增量」：`n = round(总里程 / (间隔 × 速度))`、`Δm = 总里程 / n`、
  `Δt = sample_seconds`、`duration = n × 间隔`。因此**每一点的配速（间隔/Δm）恒等于总体配速
  （总时长 / 总里程）**，`runMileage` 与 `runTime` 都严格等差，发送间隔与里程、配速三者
  互相算得出来（舍入误差 < 1 个采样间隔）。
- 步频同理：`runStep = round(t × 步频 / 60)`，末点步数 × 60 / 总时长 ≈ 名义步频。
- 地图上量到的折线弦长比总里程短 1~3%（形变的折返尖峰所致），在 `metadata.geometry_chord_m`
  如实给出；它不参与里程判定（判定用 `recordMileage`，即等差里程序列的末值 = 抽到的总里程）。

`deform_profile` 可覆盖的参数（默认值 = 演示包尺度 / 下界 / 上界）：

| 参数 | 默认 | 范围 | 含义 |
|---|---|---|---|
| `spacing_m` | 5.5 | 1–50 | 输出等弧长重采样间距 |
| `lane_sigma_m` / `lane_tau_m` | 2.8 / 280 | 0–20 / 10–5000 | 法向 OU 游走强度 / 相关长度（米） |
| `max_lane_m` | 5 | 0–20 | 法向游走硬限幅 |
| `drift_sigma_m` / `drift_tau_m` | 4.2 / 200 | 0–20 / 10–5000 | 二维定位漂移强度 / 相关长度 |
| `jump_sigma_m` / `jump_rate_per_km` / `jump_decay` | 14 / 1.2 / 0.90 | 0–60 / 0–20 / 0.5–1.0 | 偶发大偏移幅值 / 频次 / 逐点衰减 |
| `length_gain_pct` | 0.03 | 0–0.5 | 几何长度校准（相对来源；设 0 关闭） |
| `start_trim_m` / `end_trim_m` | 0 / 0 | 0–20000 | 按弧长从头/尾裁剪来源 |

注意：**2026-10 起 V4 引擎（`base_geojson`/`base_task`/`max_offset_m`/`lane_change_*`/`detour_*`/`telemetry_task` 等）已移除**。旧配置会得到明确的迁移报错（`V4 合成参数已移除：…`），不会静默按新语义执行。原 V4 的参数语义、复现基准与历史说明见 git 历史中的本文件旧版本。

`allowed_polygon_geojson` 需要由使用者按真实可通行范围提供，不能把简单矩形外包框当作跑道边界；它只证明"生成点都在给定多边形内"，不证明学校服务端围栏与该文件一致。当前只支持一个无洞多边形，不支持多区域与跨围栏桥接。

## 接入主流程

先用本地测试账号配置和本地 `getHomeRunInfo` fixture 做假传输演练：

```powershell
python main.py --dry-run -f tests/fixtures/test_config.ini --dry-home examples/routes/dry_home.json --route-config examples/routes/deform_xc.json
```

这条命令不联网、不登录、不等待真实跑步时长。fixture 仅用于演练，不代表学校实际要求。正式入口：

```powershell
python main.py -f config.ini --route-config examples/routes/deform_xc.json
```

交互模式（`python main.py` 登录后选"轨迹来源 → 合成轨迹"）等价于上面的 `--route-config`，菜单直接列出 `examples/routes/*.json`。`--route-config` 不能与 `-t` 或 `-d` 混用。路线文件不会自动写回账号配置。

生成与几何输入校验在账号配置读取、登录和建记录之前完成。取得学校任务后、`start` 前检查里程、步频和坐标系。**要求踩点的任务（`raDislikes > 0`）暂不支持，会停止；不会虚构踩点列表。** 人脸准入、拒绝停止以及结束时的 `isStandard → 尾批 → finish` 链沿用现有实现。任务下发的 `raSingleMileageMax` 会在到达上限时截断末点并进入结束链。

## 一致性与限制

- 时间采样按 `sample_seconds` 均匀推进，里程为等增量序列（`Δm = 总里程/n`），`speed` 字段实际是配速 min/km（APK `SportRunMapActivity.java:4140` 公式，1～900 两位小数）；汇总里程按公里、逐点里程按米。因为里程取的是等增量而非弦长累加，不再存在"时间采样导致几何长度损失"的问题。
- 形变的偶发大偏移会形成折返尖峰，地图上量到的折线弦长会比 `recordMileage` 短 1~3%（加密顶点无法改变，折返是路径本身的性质），故 `metadata.geometry_chord_m` 单独给出，供人工核对。
- 里程抽签失败（连续 5 次不落在给定区间）会直接报错，并在消息里给出"来源里程 ± 抽签幅度"的可达范围与区间交集占比，以及"可用 `distance_m` 精确指定"的提示；不会静默放宽区间。
- 几何不足（目标超过形变后可用长度）直接报错，不缩放场地、不首尾瞬移拼接；围栏不给就不校验，给了则逐段检查（含狭窄凹口）。
- 逐点等待、批次上传；网络与人脸处理延时纳入后续点的时间与配速，不做突发追赶。批次大小在服务端返回 `passPointNum` 时按 APK 规则确定（小于 10 回退 60，缓存严格超过阈值再发）；否则用 `[Run] split_count`。
- 同一种子 + 同一来源 + 同一 profile = 完全相同的结果；换种子或换来源才会改变形状与三个参数。**重复使用同一来源与种子仍可能保留可识别的相似性**，不代表规避服务端复审。
- 离线验证覆盖：演示基准复现、三参数抽签幅度与三向自洽、里程区间重抽与失败、来源步频闸门、配置校验（未知键/越界 profile/区间倒挂）、三种来源等价、围栏校验、种子可复现、网络延时、学校约束、生成失败不读取账号、尾批结束链。它不能证明实机人脸通过、记录有效或不会追溯取消。

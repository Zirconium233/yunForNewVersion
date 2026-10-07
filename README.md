# 云运动自动跑步脚本（develop 使用手册）

**文档导航**：本文是主使用文档；协议构造细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，
逐条 CLI 与失败分支表见 [docs/USAGE.md](docs/USAGE.md)，合成配置完整键表见
[docs/ROUTE_GENERATION.md](docs/ROUTE_GENERATION.md)。技术细节与历史档案在本文第 4 部分。

## 1. 速览

develop：云运动（3.6.6）自动跑步脚本，解决 master 两个老大难问题：

1. **人脸验证**——master 停在 3.6.4/3.6.6 核验上无法开工；develop 重构请求链并实现人脸窗口状态机，已基本解决（少量实测失败待复核）。
2. **轨迹重复**——master 的 `-d` 漂移只是整条轨迹约 0.1 毫米刚性平移（几何不变，易被近重复检测命中）；develop 用 `--route-config` 基于已有轨迹偏移出合成轨迹，算法仍有缺陷（换校区需换底图等），但已远好于老漂移。

其余改动：每请求新鲜签名、splitPoint 载荷保真、结束前状态检查、dry-run 离线演练。

```
├── main.py                 入口 + 会话编排 + 交互式菜单
├── yun_http.py             协议层（SM2/SM4 信封、签名、解码）
├── yun_face.py             人脸子系统（窗口调度、压缩、状态机）
├── yun_route.py            轨迹合成引擎（--route-config 后端）
├── live_probe.py           跑前准入探测（只读，不建记录）
├── history.py              历史记录查看器
├── tools/                  登录 / 学校地址查询 / 老漂移(弃用) / 抓包 / 路由生成 CLI
├── examples/routes/        合成配置样例（v4.json + 底图 + dry-run 夹具）
├── tasks_fch|txl|xc/       三个校区的打表任务表
├── tests/                  离线测试（211 项，禁网可跑）
├── docs/                   USAGE / ARCHITECTURE / ROUTE_GENERATION / 学校目录
├── config.ini              唯一配置文件
└── dry_run_home.json       dry-run 离线夹具
```

## 2. 更新日志

- 2026-10-08：
  1. develop 分支 README 优化为使用文档（速览 / 更新日志 / 使用文档 / 技术细节后置四部分）。
  2. `main.py` 交互式模式升级：轨迹来源三选一菜单（打表回放 / 合成轨迹 / 老式漂移），
     老式漂移标注弃用并警告近重复风险，提示勿重复上传同一轨迹，合成配置以菜单选择并可反馈补齐指引。命令行参数行为不变。

- 2026/9/23：
   1. 改进路线生成与打表播放模式，支持基于已有轨迹保留节奏变化、自动种子和可跑区域校验（不能再假定说什么服务器信什么了）
   2. 按任务里程上限截断，并依据客户端规则确定上传批次（解决2km提交问题，弱化固定`splitCount`特征）

- 2026/9/14：
   1. 更新合成路径方案

- 2026/9/12：
   1. 对云运动的人脸问题安排了对策，但未经过实际测试。
   2. 用Astra重构了代码，我承认我在24年手搓的代码质量，一坨。。

- 2025/12/9：
   1. 感谢 10punny 解决gmssl和hutool的验签问题，加密函数加上04头就可以被后端正确解密。现在我们可以使用随机密钥了(注意是随机加密，不是解密)详见[PR](https://github.com/Zirconium233/yunForNewVersion/pull/75)
   2. headers的user-agent被顺手更新成了4.9.1，虽然服务器一直都是忽略这个的。

- 2025/3/16: 封装抓历史记录功能。

- 2025/2/25: 修复3.4.7版本公钥密钥变换问题，脚本基本功能已经恢复。

- 2024/12/3:
   1. 合并xiaocheng4097代码，提供登录功能支持，可以不抓包直接登录。
   2. 增加自动版本检查，现在会自动检查`config.ini`里面的`app_edition`版本信息，如果小于3.4.5会自动更新最低可运行版本3.4.5，高版本不会更改(截至12/3日，最新版本为3.4.5)。过低的版本会导致服务返回错误信息，详见[issue#35](https://github.com/Zirconium233/yunForNewVersion/issues/35)。

- 2024/10/28: 合并laizhangtu代码，现在代理工具可以批量抓取config了。

- 2024/10/18:
   1. 修改并合并xiaochen4097代码，提供随机偏移添加功能(路线改变效果并不明显，所以也不会鬼畜)。**（后记：此"偏移"实为整条轨迹 ≈0.1mm 刚性平移，2026-10 起标注弃用。）**
   2. 有人测试发现ios版本也可以直接抓包token和deviceId，uuid使用当前代码，虽然很逆天但是真的过了。(还是不建议使用iOS登录信息跑本脚本)

- 2024/10/12:
   1. 合并ANormalDD代码，提供屯溪路校区地图和自动抓包(配置教程见proxy.md)支持。
   2. 允许传递参数执行`main.py`，提供`./tools/EasyAutoRunServer/run.sh`批量并行运行多个config的任务，配合crontab即可定时批量运行跑步任务(挂一个云服务器上就可以全自动)。

- 2024/9/21：其实我什么都没干，然后它自己又能过了，实锤了是学校服务器问题。

  <img src="./image/pass.png" alt="image" style="zoom:50%;" />

  注意事项：
  1. 新版本**无需填写config里面的utc和sign参数**(留空就行，直接把那2行删了会报错)，脚本会自动生成utc，然后和uuid计算得到sign。详见 [issue#1](https://github.com/Zirconium233/yunForNewVerison/issues/1)
  2. finish包500的问题自己好了，不知道是学校服务器是草台班子还是采用即时生成utc方法解决的。现在finish返回的是code 200。

## 3. 使用文档

### 3.1 安装

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt   # 参与开发再加 -r requirements-dev.txt
pytest tests -q                   # 211 项离线测试，可在禁网环境运行
python main.py --dry-run          # 全离线演练：确认本地构造链路正常
python live_probe.py <跑步区域名> # 实机第一步：查人脸准入（不创建跑步记录）
```

### 3.2 config.ini 配置教学

所有配置在 `config.ini`。**实机期间切勿把填了密码的 config.ini 提交进 git。**

- **[Login]（实机必填）**：`username`/`password` 学号密码，留空则运行时询问；
  登录响应中的 token 自动写回 `[User]`，**不再需要抓包填 token**。
  登录失败可能触发服务端锁定/验证码：首次失败即停，勿盲目重试。
- **[Yun]（多数保持默认）**：`school_host`/`school_id`/`school_login_url` 学校服务端
  三要素（查询方法见 §3.6）；`app_edition` 保持 3.6.6；`md5key/publickey/privatekey/cipherkey*`
  为仓库内公共协议密钥，勿改。
- **[User]（全部留空）**：token/device_id/device_name/uuid/utc/sign 由登录与每请求逻辑
  自动维护；历史"手填 4 件套"仍兼容，仅 `legacy_uuid=1` 回退旧协议时有意义。
- **[Run]（打表参数，按需微调）**：`split_count` 每批点数；`min_distance`/
  `allow_overflow_distance` 里程约束；`cadence_*_offset`、`strides` 步频步幅扰动
  （只影响配速类字段，**不改坐标**）；`exclude_points` 围栏排除点。默认值可用。

### 3.3 运行方式

命令行（行为固定，适合自动化）：

| 命令 | 模式 |
|---|---|
| `python main.py -a -t tasks_fch` | 打表自动回放（默认翡翠湖；`-d` 加老式漂移，**已不推荐**，见下） |
| `python main.py -a --route-config examples/routes/v4.json` | 合成轨迹自动执行 |
| `python main.py --dry-run [-f …] [--dry-home …] [--route-config …]` | 离线假传输演练，不登录不发请求 |
| `python main.py --face-photo … --face-detection …` | 带人脸窗口任务的输入（见 §3.5） |

交互模式（首次使用推荐，`python main.py` 直接回车）：

1. 登录 → 确认账号信息（脱敏展示）；
2. **轨迹来源三选一**：
   - `[1] 打表原轨迹回放`（默认）：选校区后回放 tasklist 固定轨迹。
     **请勿反复上传同一份任务表**——服务端有近似重复检测，轮换不同 tasklist
     文件或换用合成轨迹；
   - `[2] 合成轨迹`（基于已有轨迹偏移，**仍在测试**）：从 `examples/routes/`
     菜单选择一份配置文件离线生成新轨迹后提交；配置不全时会打印缺失项与补齐
     指引，修改后重新选择即可，全程不发起网络请求；
   - `[3] 打表 + 老式漂移`（**弃用**）：即原 `-d`，仅 ≈0.1mm 刚性平移，
     几何与原轨迹完全重合，近重复检测下基本无效，仅作历史兼容保留。
3. 确认后自动执行：start → 逐批上传 → 状态检查 → 尾批 → finish。

### 3.4 合成轨迹配置（--route-config）

样例：[examples/routes/v4.json](examples/routes/v4.json)。**配置内所有相对路径按该
配置文件所在目录解析**（所以 `v4.json` 里的 `base_v3.geojson` 就是同目录文件）。

核心键：

| 键 | 含义 |
|---|---|
| `base_geojson` 或 `base_task` | 底图二选一：GeoJSON（单条 LineString FeatureCollection）或已有打表任务 JSON |
| `coordinate_system` | 声明底图坐标系（本项目用 `"GCJ-02"`；底图坐标系与声明不符会在生成时报错） |
| `distance_m` | 目标里程，不得超过本校任务里程上限（`raSingleMileageMax`） |
| `pace_min_km` / `cadence_spm` / `sample_seconds` | 配速 / 步频 / 采样间隔 |
| `seed` | 随机种子（固定可复现；`"auto"` 每次新种子） |
| `max_offset_m` / `detour_enabled` / `lane_change_*` | 偏移幅度 / 绕行开关 / 变道点 |
| `allowed_polygon_geojson` | 可跑区域 Polygon。**`max_offset_m>5` 或开启绕行时必填**，所有点（含底图）都必须在框内 |
| `telemetry_task` / `telemetry_variation` | 用真实任务的分段速度/步频做遥测回放（可选） |

先离线预览再生成：

```bash
python tools/generate_route.py --config examples/routes/v4.json --output route_preview.geojson
python main.py --dry-run -f tests/fixtures/test_config.ini --dry-home examples/routes/dry_home.json --route-config examples/routes/v4.json
python main.py -f config.ini --route-config examples/routes/v4.json
```

**跨校区通用性**：配置结构与字段跨校区通用，但**底图与围栏必须换成目标校区**：
把 `base_geojson` 指向目标校区的轨迹/手绘线、`allowed_polygon_geojson` 换成目标
校区的可跑范围、`distance_m` 对齐该校任务上限；`pace/cadence/seed/偏移` 等参数
可原样沿用。用真实轨迹当底图最省事——从任一 tasklist 导出 GeoJSON：

```python
import json, sys
j = json.load(open(sys.argv[1], encoding='utf-8-sig')); d = j.get('data') or j
pts = [[float(x) for x in r['point'].split(',')] for r in d['pointsList']]
json.dump({"type":"FeatureCollection","features":[{"type":"Feature","properties":{},
  "geometry":{"type":"LineString","coordinates":pts}}]},
  open(sys.argv[2],'w',encoding='utf-8'), ensure_ascii=False)
# 用法: python export_base.py tasks_fch/tasklist_0.json my_base.geojson
```

注意：底图若是"将来要提交"的轨迹本身，请只把它当几何模板——合成结果会围绕它
偏移；同一底图反复合成仍可能相似，建议换种子或换底图。完整键表、误差与限制声明
见 [docs/ROUTE_GENERATION.md](docs/ROUTE_GENERATION.md)。

### 3.5 人脸输入（跑带人脸窗口任务时）

标注 JSON 与照片/视频放一起，`--face-detection` 指向它：

```json
{
  "box": [100, 120, 260, 360],
  "points": [[130,180],[230,180],[180,240],[150,260],[210,260]],
  "score": 0.9, "space": "image",
  "source_sha256": "<照片或视频文件的SHA-256>",
  "bind_apply": {"after_exif": true, "mirrored": false}
}
```

- 照片：整帧正面自拍，人脸需过取景门（哈希用 `certutil -hashfile <文件> SHA256`）。
- 视频：改用 `"frames": {"<帧号>": {…}}` 逐帧标注，`source_sha256` 绑定视频文件。
- 任何绑定不符/预检不过 = start 之前报错停止，不会创建服务端记录。
- 失败分支表见 docs/USAGE.md §4。

### 3.6 常见问题

- **连接失败先核对学校地址，不要猜端口**。官方目录实时接口为 POST
  `https://sports.aiyyd.com:9011/api/app/lisshtcool`（需 `version: 3.6.6` 请求头，
  缺头返回"版本过低"是业务错误不是网络不通）：

  ```bash
  python tools/getUrl_Id.py --list
  python tools/getUrl_Id.py --school "你的学校全称" --write --config config.ini
  ```

  已抓好的 99 条对照表见 [docs/SCHOOL_DIRECTORY_20260913.md](docs/SCHOOL_DIRECTORY_20260913.md)。
  合肥工业大学为 `schoolId=100`、`http://210.45.246.53:8080/`；外校不能照搬。
  多数学校 `https://sports.aiyyd.com:8000/` 但 schoolId 各不相同；带 `/m-api/`
  或私网 IP 的必须保留完整 URL。
- **被"人脸完整性守卫"拦截 finish**：有窗口未弹或比对未确认，脚本有意不静默收尾；
  服务端会留一条未完成记录，可在云运动 APP 内删除。
- **BusinessException** = 服务端明确拒绝（修数据/修时机），后续 split/finish 一律不发；
  传输结果未知 ≠ 失败，先 `python history.py` 查记录，勿自动重发。
- 登录不可用时抓包兜底：手机装 PCAPdroid → VPN 放行 → 抓 `token`/`deviceId`
  抄进 config.ini（图示见下方历史档案与 [proxy.md](proxy.md)）。
- 更多问答见 [questions.md](questions.md)。

---

## 4. 技术细节（为合并评审保留）

### 4.1 develop 实测反馈与结束流程修正（2026-09-12）

当前分支保留用于受控验证，**等待更多可复现成功反馈后再考虑合并 master**。
维护者的自动测试均为离线测试，不能替代账号实测。Issue #78 已有一次人脸
`data.status=Y` 的日志片段及用户报告的结束成功，但缺少完整版本、改动、回包
和最终成绩记录，尚不能证明稳定可复现。

修正了结束检查对 `url/list` 的误拦截。客户端 `CheckRunStateDialog` 将 `url`
作为状态图片、`list` 作为条件明细展示；字段非空不代表需要补拍或复核。
本分支继续遵守"状态检查 → 必要尾批确认 → finish"，不会无条件强制提交：

| 状态检查结果 | 自动处理 |
|---|---|
| HTTP、解码或业务 code 失败；data 缺失或类型错误 | 停止，不发尾批/finish |
| isCheat=Y（即使 isStandard=Y） | 显示服务端标记并停止 |
| isStandard=Y，且没有明确 isCheat=Y | 发送尾批，确认成功后发送 finish |
| isStandard 非 Y、缺失或未知 | 停止自动结束，保留现场 |
| url/list 非空 | 记录展示字段，不因此阻断 |

反馈请附提交版本、修改差异、命令参数、脱敏请求时间线、业务回包及最终成绩/限制
提示；不要上传密码、token、人脸 Base64 或完整账号配置。不要通过反复登录或强制
提交来猜测限制阈值。

### 4.2 相对 master 的功能性改动（服务器可见 / 行为可见）

| 功能点 | master | develop |
|---|---|---|
| 请求身份 | uuid 固定读配置；utc/sign 整会话算一次复用 | 每请求随机大写 UUID + 新鲜 utc + 重算 sign（3.6.6 真机行为）；`legacy_uuid=1` 可回退旧协议 |
| 业务 code | 只看 HTTP 200，响应仅打印 | `code≠200 → BusinessException` 传播即停，后续 split/finish 一律不发，报告 recordId 与最后确认位置；HTTP 层错误与业务拒绝严格区分，不自动重放 |
| splitPoint 载荷 | StepNumber=里程差÷步幅（自造）；null 字段丢失 | Gson serializeNulls 字段序、null 保留；StepNumber=表格真实 runStep 差值；体级 gzip 仅该端点白名单 |
| 结束链 | 直接 finish | `/run/isStandard` 状态检查先行 → 必要尾批 → finish；检查失败、未达标或 isCheat=Y → 后两者不发 |
| 自动人脸 | 无（[issue#78](https://github.com/Zirconium233/yunForNewVersion/issues/78)） | 窗口调度、比对上传、重试/等待状态机、faceTime+4s 预算、成功守卫 |
| getRlStatus/采集 | 无 | `live_probe.py` 准入探测；采集端点 `runFaceInfo` **有意不自动调用**（脚本代发=伪造身份材料） |
| 响应解码 | 单一 SM4 | 明文 / SM4 / SM4+gzip 三形态统一 |
| 其它 | — | 登录后同步内存客户端、输出脱敏、`--dry-run`、history.py |

### 4.3 自动人脸现状

真机 APK 发送 `POST /run/appFace/runFaceInfoComparison`：
`{ "faceBaseData": "Base64_NO_WRAP(压缩后JPEG整帧)", "recordId": "<record>" }`；
图像是取景质量门通过后的整帧（不裁剪），EXIF 摆正→宽>720 才缩→质量阶梯 80..20
压至 ≤150KB。比对基准在服务端注册照（`runFaceInfo` 采集 + 审核状态机：
`runFaceStudentStatus` Y=可跑 / N、N0=需重新采集 / N1=审核中禁跑）。

我们发送与上面请求字段结构对应（图片编码及 Luban 处理存在已记录的近似差异）；
**限制如实声明**：检测模型（RetinaFace）未移植，标注必须人工提供；比对阈值在
服务端，离线不可测。实测成功率尚无可靠统计（此前的百分比估计缺少样本依据，已撤回）。

### 4.4 V4 几何生成实现要点

`yun_route.generate(cfg)` 纯离线（读文件+计算，无网络）。`geometry_v4`：车道级
OU 游走 + 变道事件 + 采样，`max_offset_m≤5` 时自由偏移，超出或开 `detour_enabled`
则要求 `allowed_polygon_geojson` 且**每个点（含底图）必须在多边形内**。
`geometry_v4_telemetry`：以真实任务为 `base_task`/`telemetry_task` 保留分段速度与
步频形状，要求里程/时间字段单调累计，时间采样弦切损失 >1% 时报错（缩短
`sample_seconds`）。任务下发的里程上限和 `passPointNum` 上传阈值在运行时生效；
"2 km 强制提交"按学校任务 `raSingleMileageMax` 处理，无全局写死。
2026-09-23 离线验收：阻断 socket 环境下 211 项测试通过（样本轨迹完整执行、里程
上限、上传批量、时间戳、旧表步数兼容、区域越界）。运动样本模式仍可能保留轨迹与
节奏的相似性，当前实现不代表已解决次日复核不合格的问题。

### 4.5 代码结构与文件职能

```
├── main.py            CLI 入口 + Yun_For_New 会话编排 + 交互式轨迹来源菜单 + dry-run
├── yun_http.py        协议边界：DeviceProfile、SM2/SM4 信封、每请求 sign/uuid、gzip 白名单、
│                      三形态解码、异常体系、YunClient
├── yun_face.py        人脸子系统：照片/视频源、sha256 内容绑定、取景门、APK 压缩链、
│                      窗口触发(W1)、FaceRunner、FaceVerifier(预算状态机)
├── yun_route.py       轨迹合成：geometry_v4 / geometry_v4_telemetry，GCJ-02 声明校验
├── live_probe.py      实机 L1 准入探测（退出码 0=Y/2=缺配置/3=登录未完成/4=业务失败/5=非Y）
├── history.py         历史记录查看器
├── tools/             Login.py / getUrl_Id.py / drift.py(弃用) / pace_changer.py /
│                      proxy.py / generate_route.py / EasyAutoRunServer(批量并行)
├── examples/routes/   v4.json 样例、base_v3.geojson 合成底图、dry_home.json 夹具
├── tests/             离线测试：yun_http / yun_face / yun_route / main_phase_a /
│                      wire_alignment / rework_final / live_probe / phase_a_fixes …
├── docs/USAGE.md      逐条 CLI 与失败分支表
├── docs/ARCHITECTURE.md 分层 + 线上载荷投影 + 偏差清单 §7(10 条)
├── docs/ROUTE_GENERATION.md 合成配置完整键表与限制声明
└── config.ini         唯一配置（实机期间禁提交含密码的副本！）
```

### 4.6 历史档案

- 加密史：3.4.7 起 SM2 包 SM4 信封（`04` 头 hex → Base64 才能被 hutool 验签，
  [PR#75](https://github.com/Zirconium233/yunForNewVersion/pull/75)），现收敛在
  `yun_http.py`，密钥每请求随机；cipherKey 只加密不解密，"解密别人流量"走不通，
  读自己的记录用 `history.py`（api 细节见 [history.md](history.md)）。
- 踩点/围栏：服务端对关键点"踩点数"要求改过（2→3），任务表按 `isFence=Y` 覆盖
  踩点即可；围栏点在 config `[Run] exclude_points` 可调。
- 主要历史节点：2025-12 随机 SM4 key；2024-12 登录合并；2024-10 屯溪路地图 +
  proxy.py 批量抓配置 + EasyAutoRunServer；2024-09 起打表模式。项目曾于 3.6.4
  人脸验证上线后停摆（[issue#78](https://github.com/Zirconium233/yunForNewVersion/issues/78)），
  develop 即该问题的完整解决方案。
- 抓包图示（原版保留）：
  <img src="./image/googleplay.jpg" alt="image" style="zoom:50%;" />
  <img src="./image/VPN.jpg" alt="image" style="zoom:50%;" />
  <img src="./image/package.png" alt="image" style="zoom:50%;" />
  <img src="./image/header.jpg" alt="image" style="zoom:50%;" />
- 效果展示（历史截图，3.4.x 时代）：
  <img src="./image/goodMap.jpg" alt="image" style="zoom:50%;" />
  <img src="./image/processBar.png" alt="image" style="zoom:50%;" />

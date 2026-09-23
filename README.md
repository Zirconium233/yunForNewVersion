# 云运动自动跑步脚本（develop：3.6.6 协议层重构，已通过两轮评审返修）

**文档导航**：当前实际行为与操作方式见 [docs/USAGE.md](docs/USAGE.md)；
协议构造、人脸子系统与偏差清单见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
本 README 顶部为 develop 现状汇报与最新配置教学；底部保留仍有价值的历史档案。

## 连接失败先核对学校地址（2026-09-13）

已实时查询官方目录（HTTP 200 / code=200 / 99 条）：
[全部学校 → schoolId → schoolUrl 对照表](docs/SCHOOL_DIRECTORY_20260913.md)。
请按自己的**学校全称**查找，不要复制合工大地址到外校，也不要根据目录域名拼接 8080。

- 合肥工业大学：`schoolId=100`，`http://210.45.246.53:8080/`。
- 多数学校：`https://sports.aiyyd.com:8000/`，但 schoolId 仍各不相同。
- 本次目录没有 `http://sports.aiyyd.com:8080/`；此前反馈更应先核对目标地址，
  不能凭“ping 通但 TCP 失败”认定学校封了宿舍 8080。
- 有的学校 URL 带 `/m-api/` 或使用私网 IP，必须保留完整 URL；私网入口需要
  对应校园网络或官方提供的入口，热点/普通公网代理不保证可达。
- 目录快照不代表所有学校业务服务都可用。若学校目录记录异常，以官方客户端
  实际选校后的地址或学校确认结果为准；不要猜端口、降级 HTTPS 或套用其他学校。

新版目录接口是 **POST `https://sports.aiyyd.com:9011/api/app/lisshtcool`**，
Android 3.6.6 也使用该路径。它只负责返回学校地址，9011 不等于跑步服务端口。
旧查询工具的 `9001/api/app/schoolList` 已替换；新版查询不需要账号配置、token 或加密信封。

```powershell
# 只查询，不登录、不读写账号配置
python tools/getUrl_Id.py --list
python tools/getUrl_Id.py --school "合肥工业大学"
# 核对全称后显式更新 school_host / school_id（保留其他配置值）
python tools/getUrl_Id.py --school "你的学校全称" --write --config config.ini
```

也可独立通过 PowerShell 查询（不需要本项目或账号）：

```powershell
$schoolDirectory = Invoke-RestMethod -Method Post `
  -Uri 'https://sports.aiyyd.com:9011/api/app/lisshtcool' `
  -Headers @{version='3.6.6'; platform='android'; isApp='app'} `
  -ContentType 'application/json' -Body ''
if ($schoolDirectory.code -ne 200) { throw $schoolDirectory.msg }
$schoolDirectory.data | Select-Object schoolName, schoolId, schoolUrl | Format-Table -AutoSize
```

缺少 version 请求头时本次返回“版本过低”，不能把该业务错误当成 TCP 不通。
取到 schoolUrl 后，将其完整填入 `[Yun] school_host`，并使用同一条记录的 schoolId；
查询工具不推测或更改 `school_login_url`。TCP 不通时 token 尚未参与 HTTP 业务判断。
本次仅查询公共目录，没有登录或调用任何学校的跑步、人脸接口。

## develop 实测反馈与结束流程修正（2026-09-12）

当前分支保留用于受控验证，**等待更多可复现成功反馈后再考虑合并 master**。
维护者的自动测试均为离线测试，不能替代账号实测。Issue #78 已有一次人脸
`data.status=Y` 的日志片段及用户报告的结束成功，但缺少完整版本、改动、回包
和最终成绩记录，尚不能证明稳定可复现，也无法据此确定后续封禁的原因。

修正了结束检查对 `url/list` 的误拦截。客户端 `CheckRunStateDialog` 将 `url`
作为状态图片、`list` 作为条件明细展示；字段非空不代表需要补拍或复核。
本分支继续遵守“状态检查 → 必要尾批确认 → finish”，不会无条件强制提交：

| 状态检查结果 | 自动处理 |
|---|---|
| HTTP、解码或业务 code 失败；data 缺失或类型错误 | 停止，不发尾批/finish |
| isCheat=Y（即使 isStandard=Y） | 显示服务端标记并停止 |
| isStandard=Y，且没有明确 isCheat=Y | 发送尾批，确认成功后发送 finish |
| isStandard 非 Y、缺失或未知 | 停止自动结束，保留现场 |
| url/list 非空 | 记录展示字段，不因此阻断 |

非 Y 时停止是脚本的保守自动化策略，不代表客户端所有人工结束分支。
`isCheat` 缺失不是“确认无作弊”；人脸 status=Y、结束前达标、finish 受理、
最终成绩有效是不同结果，不能相互替代。日志新增脱敏后的状态展示字段。

反馈请附每次运行的提交版本、修改差异、命令参数、脱敏请求时间线、业务回包
及最终成绩/限制提示；不要上传密码、token、人脸 Base64 或完整账号配置。
不要通过反复登录或强制提交来猜测限制阈值。登录会改变会话及本地配置，
实测应由账号持有人明确授权；本次修复验证不执行登录或线上跑步请求。

输入仍为照片/选定视频帧加人工标注，自动检测器、完整恢复及部分客户端
时序尚未实现；详细范围见 [docs/USAGE.md](docs/USAGE.md)。

## 1. 相对 master 的功能性改动（服务器可见 / 行为可见）

| 功能点 | master | develop |
|---|---|---|
| 请求身份 | uuid 固定读配置；utc/sign 整会话算一次复用 | 每请求随机大写 UUID + 新鲜 utc + 重算 sign（3.6.6 真机行为）；`legacy_uuid=1` 可回退旧协议 |
| 业务 code | 只看 HTTP 200，响应仅打印 | `code≠200 → BusinessException` 传播即停，后续 split/finish 一律不发，报告 recordId 与最后确认位置；HTTP 层错误（结果未知）与业务拒绝严格区分，不自动重放 |
| splitPoint 载荷 | StepNumber=里程差÷步幅（自造）；null 字段丢失 | Gson serializeNulls 字段序、null 保留；StepNumber=表格真实 runStep 差值（loader 不再丢 runStep/ts）；体级 gzip 仅该端点白名单 |
| 结束链 | 直接 finish | `/run/isStandard`（同一 P1 体）状态检查先行 → 必要尾批（改为暂存至此补发）→ finish；检查失败、未达标或 isCheat=Y → 后两者不发；url/list 仅展示 |
| 自动人脸 | 无（当年正倒在 3.6.4/3.6.6 人脸验证上，见 [issue#78](https://github.com/Zirconium233/yunForNewVersion/issues/78)） | 新增整套：窗口调度、比对上传、重试/等待状态机、faceTime+4s 预算、成功守卫（未确认窗口拒绝 finish） |
| getRlStatus/采集 | 无 | `live_probe.py` 准入探测 getRlStatus（不创建跑步记录）；采集端点 `runFaceInfo` **有意不自动调用**（真人审核流，脚本代发=伪造身份材料） |
| 响应解码 | 单一 SM4 | 明文 JSON / SM4 / SM4+gzip 三形态统一，异常即 DecodeException |
| 登录后置 | 首个请求仍带旧 base_url/空 token | 登录后同步内存客户端（含学校地址探测结果）；输出脱敏 |
| 其它 | — | `--dry-run` 全离线演练、config/task 路径 CLI 贯穿、history.py 记录查看器 |

## 2. 自动人脸现状

**真机 APK 发送什么**（JTFaceCompareActivity.java:724-740）：

```json
POST /run/appFace/runFaceInfoComparison
{ "faceBaseData": "Base64_NO_WRAP(压缩后JPEG整帧)", "recordId": "<start响应的record>" }
```

图像是取景质量门（人脸尺寸/俯仰/偏航/滚转）通过后的**整帧**（不裁剪），经
FaceImageCompressor：EXIF 摆正→宽>720 才缩→质量阶梯 80..20 压至 ≤150KB。
比对基准在**服务端注册照**（`runFaceInfo` 采集 + 审核状态机：
`getRlStatus.runFaceStudentStatus` Y=可跑 / N、N0=需（重新）采集 / N1=审核中禁跑）。

**我们发送什么**：与上面的请求字段结构对应（图片编码及 Luban 处理存在已记录的近似差异）（两键、同 b64 形态、同压缩链，含
"限宽不限长边、150KB 硬目标"等 WIRE_AUDIT 修正）；图片来源为
`--face-photo` / `--face-video` + 人工标注 JSON（内容哈希绑定 + 取景门复算 +
start 前全量预检）。**限制如实声明**：检测模型（RetinaFace）未移植，标注必须
人工提供；等待期复用同一张图、语音引导 4s、重试形态对齐 APK，但比对阈值在
服务端，离线不可测。

**实测成功率尚无可靠统计**：此前的百分比估计缺少样本依据，已撤回。
需要按版本收集完整成功/失败记录，才能评估可复现性。

## 3. 代码结构与文件职能

```
├── main.py            CLI 入口 + Yun_For_New 会话编排：start/split/尾批暂存/
│                      结束链(isStandard→尾批→finish)、人脸窗口接线与守卫、
│                      build_face_runner 全量预检、dry-run
├── yun_http.py        协议边界：DeviceProfile、SM2/SM4 信封、每请求 sign/uuid、
│                      gzip 白名单、三形态解码、异常体系、YunClient
├── yun_face.py        人脸子系统：照片/视频源、sha256 内容绑定、标注 Bundle、
│                      取景质量门、APK 压缩链、窗口触发(W1)、FaceRunner、
│                      FaceVerifier(预算状态机+双段超时裁剪)、compare_once
├── live_probe.py      实机 L1 准入探测：login + getRlStatus，不创建跑步记录（登录非零状态变更；退出码 0=Y/2=缺配置/3=登录未完成/4=业务失败/5=非Y）
├── history.py         历史记录查看器（抓轨迹做打表数据 / 事后核验）
├── tools/Login.py     登录（凭据来自 ini；token 脱敏；地址探测失败保留配置）
├── tools/getUrl_Id.py 学校地址/ID 发现（当前网络环境不可达时可预填绕过）
├── tools/drift.py / pace_changer.py / proxy.py   漂移 / 配速 / 抓包配置工具
├── tools/EasyAutoRunServer/run.sh                多 config 批量并行（crontab 可用）
├── tests/             离线测试：yun_http(信封/字段序/序列化)、yun_face(窗口/压缩/
│                      verifier/绑定)、main_phase_a(会话流程/dry-run)、
│                      wire_alignment(10 项服务器视角护栏)、
│                      rework_final(R1-R6+S1-S3)、live_probe(探测只读性与退出码)、phase_a_fixes
├── docs/USAGE.md      用户文档（配置/CLI/人脸输入/失败分支表）
├── docs/ARCHITECTURE.md 分层 + 线上载荷投影 + 偏差清单 §7(10 条) + 测试地图
├── config.ini         唯一配置（见下方教学；实机期间禁提交含密码的副本！）
├── dry_run_home.json  dry-run 离线夹具（数据已脱敏）
└── tasks_fch|txl|xc/  打表任务表（runStep 保真）
```

## 4. 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt    # 测试再加 -r requirements-dev.txt
pytest tests -q                    # 可在禁网环境运行
python main.py --dry-run           # 全离线演练（不登录、不发任何真实请求）
python live_probe.py <跑步区域名>  # 实机第一步：查人脸准入（不建跑步记录；登录会更新会话/本地配置，可能使手机APP会话失效）
python main.py                     # 正式跑（先小步验证，见 docs/USAGE.md §6）
```

常用参数：`-f/-t` 指定 config/task 目录；`-a` 自动模式（缺省打表；指定 `--route-config` 时生成路线）；`-d` 轨迹漂移；`--face-photo/--face-video/--face-detection/--face-mirror`
人脸源（见 §人脸输入）。

## V4 几何模板生成（develop）

新增 `--route-config`，使用本地几何底图和参数生成点列，重新计算时间、距离、步数和配速，替代读取旧打表数据。仍需要底图定义跑道形状；默认不裁剪、不绕出跑道，随机种子固定以便复现。

现在也可在配置中用 `base_task` 读取已有轨迹、用 `telemetry_task` 保留分段速度与步频变化，或将 `seed` 设为 `"auto"` 每次选择新种子。偏移超过 5 米须给出可跑区域 Polygon。任务下发的里程上限和 `passPointNum` 上传阈值会在运行时生效；打表入口的时间戳按逐点 `runTime` 推进。详见下方配置说明。

打表及 `--route-config` 模式中的“2 km 强制提交”按学校任务的 `raSingleMileageMax` 处理，没有全局写死 2 km；达到上限后进入既有的状态检查 → 尾批 → finish 链。上传批量按客户端正常分支的 `passPointNum` 阈值确定，不额外随机化。运动样本模式仍可能保留轨迹与节奏的相似性，当前实现不代表已解决次日复核不合格的问题。

2026-09-23 离线验收：在阻断 socket 连接的环境下，211 项测试通过，覆盖样本轨迹完整执行、里程上限、上传批量、时间戳、旧表步数兼容及区域越界。当前仅发布至 develop，后续仍需实机与延迟复核反馈。

```powershell
# 只生成文件，不读取账号、不联网；输出已存在时请换文件名
python tools/generate_route.py --config examples/routes/v4.json --output work_dir/route_preview.geojson
# 完整离线假传输演练
python main.py --dry-run -f tests/fixtures/test_config.ini --dry-home examples/routes/dry_home.json --route-config examples/routes/v4.json
# 使用账号运行时的入口
python main.py -f config.ini --route-config examples/routes/v4.json
```

参数、路径规则与限制见 [路径生成配置说明](docs/ROUTE_GENERATION.md)，配置模板为 [v4.json](examples/routes/v4.json)。示例底图仅适用于其对应场地，其他学校需更换；不能与 `-t/-d` 混用。暂不支持需要踩点的任务，发现不匹配会在建记录前停止。

此前记录被追溯取消的原因尚不明确。几何合理和离线测试通过均不代表成绩有效，当前保持 develop 测试状态。

## 5. 配置教学（2026 develop 版）

所有配置在 `config.ini`。**开发/实机期间切勿把填了密码的 config.ini 提交进 git。**

### [Login]（实机必填）
- `username` / `password`：学号与密码。留空则运行时交互询问。登录响应里的
  token 自动写回 `[User]`，**不再需要抓包填 token**。
- 登录失败可能触发服务端锁定/验证码：首次失败即停，勿盲目重试。

### [Yun]（多数保持默认；实机核对三项）
- `school_host` / `school_id` / `school_login_url`：学校服务端三要素。地址与 ID
  通过本文顶部的新版目录查询；登录路由仍需对应学校确认。合工大为
  `http://210.45.246.53:8080` / `100` / `appLoginHGD`，外校不能照搬。
  目录查询失败不会证明既有配置正确，先单独查询或对照官方客户端；当前登录
  流程遇到目录请求异常会停止，勿将其理解为保证自动回退。
- `app_edition`：3.6.6 行为基准（脚本按此生成 3.6.6 形态请求）。
- `legacy_uuid=1`（可选，[User] 段）：回退"固定 uuid + 会话级 sign"旧协议，
  仅当服务端按新版本拒绝请求时排查用。
- `md5key/publickey/privatekey/cipherkey*`：协议密钥，仓库内公共值，勿改。

### [User]（全部留空）
- token/device_id/device_name/uuid/utc/sign 由登录与每请求逻辑自动维护。
  历史教程里"手填 4 件套"的方式仍兼容，但 3.6.6 起 uuid 每请求随机，
  固定值只在 legacy 模式有意义。

### [Run]（打表参数，按需微调）
- `split_count` 每批点数（默认 10）；`min_distance`/`allow_overflow_distance`
  里程约束；`cadence_min_offset`/`max_offset`、`strides` 步频步幅扰动；
  `exclude_points` 围栏排除点。默认值可用，异常时先别动。

### 人脸输入（跑带人脸窗口任务时）
标注 JSON 与照片/视频放一起，`--face-detection` 指向它：

```json
{
  "box": [100, 120, 260, 360],                    // 人脸框（原图像素坐标）
  "points": [[130,180],[230,180],[180,240],[150,260],[210,260]],  // 五官点
  "score": 0.9, "space": "image",
  "source_sha256": "<照片或视频文件的SHA-256>",    // 内容绑定，必需
  "bind_apply": {"after_exif": true, "mirrored": false}            // 坐标声明
}
```

- 照片：整帧正面自拍，人脸占比/角度过取景门即可（可用图像查看器量坐标；
  哈希用 `certutil -hashfile <文件> SHA256` 或 `python -c "import
  hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" <文件>`）。
- 视频：改用 `"frames": {"<帧号>": {box/points/score/space...}}` 逐帧标注，
  `source_sha256` 绑定的是**视频文件**内容。
- 任何绑定不符/预检不过 = start 之前就报错停止，不会创建服务端记录。
- 完整语义（含失败分支表）见 docs/USAGE.md §4。

### 抓包兜底（登录不可用时）
新版无需 root：手机装 PCAPdroid（Google Play 搜索）→ 配 VPN 放行 → 云运动
随便翻页 → 抓 `210.x.x.x:8080` 的请求 → headers 里 token/deviceId 抄进
config.ini 对应项。图示版教程保留在下方历史档案与 [proxy.md](proxy.md)。

## 6. 常见问题

- 常见问题清单见 [questions.md](questions.md)；失败分支的机器可读含义见
  docs/USAGE.md §7 表格（BusinessException=服务端明确拒绝：修数据/修时机；
  传输结果未知：勿自动重发，先 history.py 查记录）。
- finish 被"人脸完整性守卫"拦截：说明有窗口未弹或比对未确认——脚本有意
  为之（不静默收尾）；服务端会留一条未完成记录，可在云运动 APP 内删除
  （协议存在 `run/deleteCrsRunRecordById`，脚本未实现该操作）。

---

## 历史档案（仍有参考价值的内容）

### 加密史一句话
3.4.7 起云运动改用 SM2 包 SM4 信封（客户端只加密不签名回验，`04` 头 hex →
Base64 才能被 hutool 验签，见 [PR#75](https://github.com/Zirconium233/yunForNewVersion/pull/75)）；
现在该逻辑全部收敛在 `yun_http.py`，密钥每请求随机。客户端对 cipherKey 只有
加密能力没有解密能力（服务端公钥设计如此）——所以"解密别人流量"走不通，
读自己的记录用 `history.py`（api 细节见 [history.md](history.md)）。

### 踩点/围栏一句话
服务器对关键点 ManageList 的"踩点数"要求改过（2→3），任务表按
`isFence=Y` 覆盖踩点即可——服务端对轨迹细节的信任度比想象中高，围栏点
列表在 config `[Run] exclude_points` 可调。

### 主要历史节点（完整记录 `git log --all`）
- 2025-12 随机 SM4 key 通讯（10punny）；2024-12 登录功能合并（可不抓包）；
  2024-10 屯溪路地图 + proxy.py 批量抓配置、EasyAutoRunServer 批量并行；
  2024-09 起打表模式 / 时间戳 / 多图随机。
- 项目曾于 3.6.4 人脸验证上线后停摆（[issue#78](https://github.com/Zirconium233/yunForNewVersion/issues/78)）；
  develop 分支即该问题的完整解决方案（人脸链路重构 + 3.6.6 对齐）。

### 抓包图示（原版保留）
<img src="./image/googleplay.jpg" alt="image" style="zoom:50%;" />
<img src="./image/VPN.jpg" alt="image" style="zoom:50%;" />
<img src="./image/package.png" alt="image" style="zoom:50%;" />
<img src="./image/header.jpg" alt="image" style="zoom:50%;" />

效果展示（历史截图，3.4.x 时代）：肉眼难辨的轨迹与进度条
<img src="./image/goodMap.jpg" alt="image" style="zoom:50%;" />
<img src="./image/processBar.png" alt="image" style="zoom:50%;" />

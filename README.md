# 云运动自动跑步脚本（develop：3.6.6 协议层重构，已通过两轮评审返修）

**文档导航**：当前实际行为与操作方式见 [docs/USAGE.md](docs/USAGE.md)；
协议构造、人脸子系统与偏差清单见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
本 README 顶部为 develop 现状汇报与最新配置教学；底部保留仍有价值的历史档案。

状态：按 APK 3.6.6 反编译逐字段对齐；155 项离线测试全绿（禁网守卫下复证）；
两轮评审返修（R1-R6 + S1-S3）通过。**未做账号实机验证**——离线口径为
`offline_client_compatibility`，服务端接受性待实机（受控实测范围见 docs/USAGE.md §8）。

## 1. 相对 master 的功能性改动（服务器可见 / 行为可见）

| 功能点 | master | develop |
|---|---|---|
| 请求身份 | uuid 固定读配置；utc/sign 整会话算一次复用 | 每请求随机大写 UUID + 新鲜 utc + 重算 sign（3.6.6 真机行为）；`legacy_uuid=1` 可回退旧协议 |
| 业务 code | 只看 HTTP 200，响应仅打印 | `code≠200 → BusinessException` 传播即停，后续 split/finish 一律不发，报告 recordId 与最后确认位置；HTTP 层错误（结果未知）与业务拒绝严格区分，不自动重放 |
| splitPoint 载荷 | StepNumber=里程差÷步幅（自造）；null 字段丢失 | Gson serializeNulls 字段序、null 保留；StepNumber=表格真实 runStep 差值（loader 不再丢 runStep/ts）；体级 gzip 仅该端点白名单 |
| 结束链 | 直接 finish | `/run/isStandard`（同一 P1 体）状态检查先行 → 必要尾批（改为暂存至此补发）→ finish；检查失败/未知/返回 url,list → 后两者不发 |
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

**我们发送什么**：与上面逐字节同构（两键、同 b64 形态、同压缩链，含
"限宽不限长边、150KB 硬目标"等 WIRE_AUDIT 修正）；图片来源为
`--face-photo` / `--face-video` + 人工标注 JSON（内容哈希绑定 + 取景门复算 +
start 前全量预检）。**限制如实声明**：检测模型（RetinaFace）未移植，标注必须
人工提供；等待期复用同一张图、语音引导 4s、重试形态对齐 APK，但比对阈值在
服务端，离线不可测。

**第一次实机通过概率（分层估计，非单一数字）**：

| 层 | 依据 | 首验估计 |
|---|---|---|
| 传输/信封/加密 | 与已可用端点同通道 + 155 测试/10 项字段护栏 | ~90% |
| 业务受理（recordId 时机、学校人脸开关、两键体） | 键面逐字对照 APK | ~75% |
| 比对本体 status=Y | 取决于账号人脸注册状态 | 状态 Y + 本人真照：~50-70%；未注册：≈0（先真机 APP 采集，N1 期间连 APP 都禁跑） |

综合：L1 探测为 Y 且提供本人合格照片时，端到端首验约 **4-6 成**；未注册账号
人脸任务现在不可能过——这与脚本代码质量无关，先用 `live_probe.py` 探状态。

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
├── tests/             155 项：yun_http(信封/字段序/序列化)、yun_face(窗口/压缩/
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
pytest tests -q                    # 155 项，全程可禁网跑（conftest 断网守卫）
python main.py --dry-run           # 全离线演练（不登录、不发任何真实请求）
python live_probe.py <跑步区域名>  # 实机第一步：查人脸准入（不建跑步记录；登录会更新会话/本地配置，可能使手机APP会话失效）
python main.py                     # 正式跑（先小步验证，见 docs/USAGE.md §6）
```

常用参数：`-f/-t` 指定 config/task 目录；`-a` 自动模式（map.json 路线，旧行为
保留）；`-d` 轨迹漂移；`--face-photo/--face-video/--face-detection/--face-mirror`
人脸源（见 §人脸输入）。

## 5. 配置教学（2026 develop 版）

所有配置在 `config.ini`。**开发/实机期间切勿把填了密码的 config.ini 提交进 git。**

### [Login]（实机必填）
- `username` / `password`：学号与密码。留空则运行时交互询问。登录响应里的
  token 自动写回 `[User]`，**不再需要抓包填 token**。
- 登录失败可能触发服务端锁定/验证码：首次失败即停，勿盲目重试。

### [Yun]（多数保持默认；实机核对三项）
- `school_host` / `school_id` / `school_login_url`：学校服务端三要素。地址
  发现接口（`yun_host:8085`）在部分网络环境不可达——探测失败时脚本保留既有
  配置，直接手填即可（合工大：`http://210.45.246.53:8080` / `100` /
  `appLoginHGD`；其他学校抓包 `210.x.x.x:8080` 类 URL 照抄）。
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

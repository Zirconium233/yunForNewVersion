# 使用文档

本文档描述当前 develop 分支的实际行为（人脸管线与线上字段对齐均已并入）。
项目框架与协议取证见 [ARCHITECTURE.md](ARCHITECTURE.md)。

> 验收边界：本项目所有自动化验证均为**离线**（产物名 `offline_client_compatibility`），
> 只证明"构造的包与 3.6.6 反编译证据一致"，**不证明服务器接受、不证明能过人脸、
> 不承诺任何成功率**。服务端接受性需要真实账号线上验证，当前未验证。

## 1. 环境准备

- Python 3.9+（开发验证于 3.11，仓库自带 `.venv` 不入库）
- 安装依赖：

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -r requirements.txt # 必装：requests、gmssl==3.2.2、pillow、tqdm、pycryptodome 等
pip install opencv-python       # 可选：仅当使用 --face-video 视频源时需要
```

- 网络代理：脚本走系统环境变量（`HTTP_PROXY`/`HTTPS_PROXY`），无特殊处理。
- gmssl 固定 3.2.2（3.2.1 缺少 `mode/asn1` 参数，加密布局不同，不可替换）。

## 2. 配置 `config.ini`

用 `-f 路径` 可指定其他配置文件。所有字段含义：

### `[User]` —— 身份（登录后由脚本自动写回，也可手工填写）

| 键 | 说明 |
|---|---|
| `token` | 登录令牌。留空时 `main.py` 会询问是否账号密码登录（`tools/Login.py`） |
| `device_id` | 请求头 deviceId。真机取值为 android_id 的 SHA-256（64 位小写 hex）；Login 未配置时生成 16 位随机数——建议手工填成 64 位 hex 更接近真机 |
| `device_name` | 请求头 deviceName / finish 体 deviceName。**真机格式为 `厂商(型号)`**，如 `Xiaomi(M2011K2C)`（RetrofitService.java:67） |
| `sys_edition` | 系统版本名裸值，如 `14`（请求头 `sysVersion` 用它；finish 体 `sysEdition` 会自动加 `Android_` 前缀，见 §6） |
| `sys_version` | 可选，优先于 `sys_edition` 用于 `sysVersion` 头 |
| `uuid` | 仅在 `[User] legacy_uuid = true` 时作为固定 uuid 使用；默认不启用 |
| `legacy_uuid` | 兼容开关：`true` 时沿用配置固定 uuid（旧脚本路线）；默认（不配置）每请求随机大写 UUID，与 3.6.6 APK 一致 |
| `map_key` / `utc` / `sign` | 旧路线遗留字段，常规流程不再需要 |

### `[Yun]` —— 主机与密钥

| 键 | 说明 |
|---|---|
| `yun_host` / `school_host` | 接口基址；登录流程会自动探测并写回 `school_host` |
| `PublicKey` / `PrivateKey` | SM2 密钥对（base64）。仓库默认值是公开示例且**公私不配对**（"3.4.7 私钥失效"），生产加密只需要公钥，不受影响；私钥仅离线工具（FakeTransport 解信封）需要配对值 |
| `cipherkey` / `cipherkeyencrypted` | 登录/学校目录旧路线的固定 SM4 key 与其 SM2 密文；主接口默认每请求随机 SM4 key |
| `md5key` | sign 计算用的 appsecret |
| `platform` | 固定 `android` |
| `app_edition` | 请求头 version，按当前 APK 版本填写（分析基线为 `3.6.6`，仓库示例值可能偏旧） |
| `school_login_url` / `school_id` / `school_name` | 学校目录/登录探测参数 |

### `[Run]` —— 跑步行为

`min_distance`、`allow_overflow_distance`、`split_count`（每组 splitPoint 的点数）、
`single_mileage_min/max_offset`、`cadence_min/max_offset`、`strides`（步幅，米）、
`min_consume/max_consume`（自动生成任务时的配速区间）等，均有注释示例。

## 3. 运行

```bash
python main.py                 # 交互式：选配置→(可选)登录→选任务表→执行
python main.py -f cfg.ini -t tasks_xc -a          # 指定配置/任务目录，全自动
python main.py -d              # 轨迹漂移（tools/drift.py）
```

- 旧版 `EasyAutoRunServer`（`tools/EasyAutoRunServer/run.sh`）与
  `python history.py`（拉历史记录，见 `history.md`）保持可用。
- **路径契约**：显式传入的相对路径（`-f/-t/--face-*` 等）按**调用时的工作目录**
  解析；未显式传入的缺省资源按项目根目录解析。
- 流程：`getHomeRunInfo` → `start`（先保存 recordId 再判 `canSport`；
  `canSport=N` 抛 `RunNotPermittedError` 并带出 recordId，**不会**继续发送）→
  按任务表分组发送 `splitPointCheating` → `finish`。

## 4. 人脸核验任务（runFaceStatus=Y）

任务要求人脸时的行为取决于是否提供人脸源：

- **未提供源**：立即抛 `FaceRequiredError` 停止（不默认放行）。请使用官方 App，
  或按下面提供离线源。
- **提供 `--face-photo 图片`**（方案 1）：按 APK 图像链处理照片
  （EXIF 定向→JPEG→宽度>720 才缩图→>150KB 走质量阶梯 80..20，缩放与阶梯
  作用于同一处理阶段图像）；标注 JSON **必须绑定该图片文件**：提供
  `source_sha256`（文件哈希）或 `source_path`，并用 `bind_apply` 声明坐标系
  （`{"after_exif":bool,"mirrored":bool}`，管线是 EXIF 摆正→可选镜像）。
  未绑定/哈希不符 = 预检失败（不能拿无主或过期标注宣称“这张图检测到了”）。
- **提供 `--face-video 视频`**（方案 2）：逐帧（可配 `frame_step`）用同一质量门
  选第一帧合格画面；需要 `opencv-python`。**检测标注必须逐帧**：JSON 提供
  `{"frames":{"<帧号>":{box,points,...}}}`，未标注帧一律视为无检测（不挪用
  其他帧标注凑数）。**没有逐帧标注能力时在 run/start 之前的预检即失败**
  （发生在 `build_face_runner`，早于任何真实网络请求），不会先建任务再报错。
- 两种源都必须配合 **`--face-detection 检测标注.json`** 提供人脸框/五点，
  或自行注入检测器。检测模型（RetinaFace）**未随仓库移植**，且
  **没有检测结果时不放行**（不默认通过质量门）。
- `--face-mirror`：照片/视频源默认**不**镜像（与 APK 一致，仅在源本身是镜像预览时开启）；开启时标注 `bind_apply.mirrored` 必须同为 true，坐标系矛盾即拒绝。
- `runFaceStatus=Y` 且 start 响应缺少 `randomList` 或 `faceTime`：必要参数
  不完整，显式停止（不按“无限预算/默认放行”继续）。

窗口调度语义与 APK W1 对齐，事件源为**轨迹点**（与网络批次解耦，首批跨
窗不漏）：起点基线 0、`int(window_km*1000)` 跨越判定（点恰在边界算跨越）、
非单调事件忽略、每窗口一次、在途互斥。**与 APK 相同**：会话在途期间跨过的
窗口不会自动补触发。在此之上本脚本加了一道**更严格的策略**：finish 前
检查实际经过范围内所有窗口，存在“未弹（含在途漏跨）或比对未确认成功”
的窗口即拒绝发送 finish（不得静默收尾）。

窗口预算与重试：语音引导 4s、比对失败后 ≤3 次立即重试（间隔 1s）+ 等待期
每 3s 复用同一张图重发——这些形态与 APK 一致；但等待按**单调时钟经过
时间**计（网络请求耗时计入预算；返修 R2：修复前按 sleep 计数，慢请求会
把窗口拖出预算），整个窗口共用 `faceTime+4s` 截止（时钟双轨：utc/sign 用
epoch 秒，预算用 client.mono）。预算耗尽 → `expired`：该窗口
`compare_success` 一律不记 Y，且**后续 split 与 finish 全部被守卫拦截**（不
补发、不静默收尾）。`code=200 且 data.status!=Y` 仍是终端
compare_failed（不重试）。

**已知策略偏差**：APK 在人脸失败路径会自动提交本次跑步数据（checkRunState），
本脚本**不自动提交**，改为停止并保留现场——提交与否由人工决定。

**支持范围声明**：人脸**注册/预登记链路不支持**——`run/getRlStatus`（登记
状态预查询）与注册接口在本脚本中无调用点；遇到需要注册流程的任务应使用
官方 App。**窗口断点持久化未实现**：进程中断不会恢复未完成窗口；完整性
检查只覆盖当前进程观察到的里程。`recover_unfinished` 是纯函数，**未接入
运行时**。

人脸数据（faceBaseData）默认脱敏，不落日志明文；本脚本**不会调用注册接口**
（`runFaceInfo` 注册链路存在但禁用，不做自动录入）。

## 5. 离线演练 `--dry-run`

```bash
python main.py --dry-run [--dry-home dry_run_home.json]
```

全部请求走假传输（零网络），逐请求解开信封与业务体并报告：
`可用持有私钥直接验证的信封 n/总数`。仓库默认密钥对公私不配对时 n=0 是**正常且如实**
的（生产方向只需要公钥）。产物名固定为 `offline_client_compatibility`，
禁止与任何"线上通过"表述混用。

## 6. 线上字段对齐要点（脚本已自动处理，列出供排错）

- 请求头 11 项与 APK 拦截器同集合同序（`Content-Type: application/json` 无
  charset；不发 `Accept`/`Connection`；`User-Agent: okhttp/4.9.1`、
  `Accept-Encoding: gzip` 由共享 Session 注入）。
- `uuid` 每请求随机大写；`utc` 秒级；`sign = MD5(platform&utc&uuid&appsecret)`。
- 仅 `splitPointCheating` 的 content 前置 gzip（JDK 容器头字节对齐）；人脸接口
  **不** gzip。
- finish 体 `sysEdition = "Android_" + 版本名`；`remake = "0|{}"`（APK 设备自检
  在干净设备上的输出形态）。
- Cookie：共享 Session 持久保存服务端 Set-Cookie（对齐 APK 的 CookieJar；
  内容取决于服务端，不本地伪造）。

## 7. 常见异常与处置

| 异常 | 含义 | 处置 |
|---|---|---|
| `TransportOutcomeUnknown` | 连接失败/读超时，**请求结果未知** | 勿自动重发（服务器可能已入账），先查历史记录 |
| `HttpStatusException` | 非 200 响应（消息已脱敏） | 同上按"结果未知"对待 |
| `RunNotPermittedError` | 服务端拒绝开始（canSport=N），异常带 recordId | 按提示处理已开始的记录 |
| `FaceRequiredError` | 任务要求人脸 / 状态缺失未知 | 用官方 App 或提供人脸源 |
| `BusinessException` | 服务器明确拒绝（HTTP 200 业务 code!=200）：上传/finish 即时停止 | 按提示查 recordId 与最后确认位置，勿盲目重发 |
| `FaceRunStopError` | 人脸窗口未确认通过 | 按提示"勿盲目重发"，人工核对 |
| `DecodeException` | 响应无法按 明文/SM4/SM4+gzip 解码 | 保留现场报告，不要当成业务失败 |

## 8. 安全与合规提醒

本工具仅用于理解与自查接口行为；请遵守所在学校的规定。任何字段对齐都不构成
"绕过"承诺——服务端还有行为/数据层面的分析（配速分布、请求节奏、TLS/HTTP
指纹等，见 `work_dir/develop_docs/WIRE_AUDIT.md`，不随仓库分发）。

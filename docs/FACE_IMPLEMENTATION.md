# 人脸方案实施与 A 阶段整改（docs/REVIEW_FACE_PLAN.md 执行记录）

日期：2026-09-11。基线：评审提交 94c522f 的验收文档 `docs/REVIEW_FACE_PLAN.md`。
本轮无账号、无网络请求；全部断言为“本地校验”或“静态证据吻合”，服务端相关均未验证。

## 一、阻断问题整改（§一 表格逐项）

| 项 | 修复 | 回归测试 |
|---|---|---|
| P1-1 `set_args` 未加载 DeviceName | main.py 加载 `User.device_name`；入口测试直接调用 `main(run=False)` | `tests/test_phase_a_fixes.py::test_set_args_loads_device_name`、`test_main_entry_run_false_with_token_config`；复现脚本 `configured_device_name_loaded=true`、`cli_print=ok` |
| P1-2 登录只更新全局 | 新增 `apply_login_result(...)`：token/设备身份/sysVersion 写入 `_CLIENT.profile`，并以实际配置文件为准同步 Login 探测出的 `school_host` → `_CLIENT.base_url` 与 `my_host`（无论是否写盘 token） | `test_login_updates_client_and_first_request`（登录后首个请求头携带新 token + stdout 无明文 token）、`test_apply_login_result_syncs_school_host`；复现脚本 `login_client_token_updated=true` |
| P1-3 `canSport is False` | 按 APK 字符串语义 `TextUtils.equals("N")`（:790）判定；recordId/开始时间先保存再判定（:783-786）；业务拒绝抛 `RunNotPermittedError`（携带 `record_id`），与人脸要求分离 | `test_cansport_string_N_rejected_with_record_id`、`test_cansport_not_boolean_true`；复现脚本 `canSport_N=blocked` |
| P2-4 登录身份连续性 | Login 头部 deviceId 用实际 `DeviceId`（不再拿旧配置 uuid 顶替）、sysVersion 用本次实际使用的 `sys_edition`（config `sys_version` 优先） | `test_login_header_identity_continuity`（解开登录请求头部断言三字段连续） |
| P2-5 faceTime 当开关 | 启用开关=任务 `runFaceStatus`（:4314）；faceTime 仅窗口参数（N 任务下 faceTime>0 不拦截、打印说明；Y 任务下缺 randomList 不放行）；faceTime 下限 10s（:787-788） | `test_face_time_positive_on_N_task_does_not_stop`、`test_face_time_floor_10`、`test_Y_task_requires_random_list_not_default_pass` |
| P2-6 配置 uuid 优先 | `YunClient` 默认每请求随机大写 UUID（对齐 3.6.6，RetrofitService.java:68）；沿用配置 uuid 需显式 `legacy_uuid=True`（config `User.legacy_uuid` 开关） | `test_default_uuid_is_random_per_request`（含 sign-uuid-utc 绑定断言）、`test_set_args_default_client_random_uuid` |

### 质量问题
- **密码实现处置（评审 §一质量问题 1）**：生产加密恢复 gmssl 3.2.2 成熟实现为主路径
  （`b64encode(0x04 || gmssl.encrypt(...))`，与仓库原始 `encrypt_sm2` 同构——
  注意 gmssl 输出**不含** 04 未压缩点前缀，此前仿射直出是等价替换而非原样保留）。
  gmssl 上游缺陷（偶发 `_add_point(None)` TypeError）以“同进程内换新随机数重试”消化，
  连续 3 次失败才退回仿射参考实现并记 WARNING + `fallback_count` 计数。
  交叉验证升级为多 k：`test_sm2_wire_matches_gmssl_for_many_k` 对 24 个固定 k 断言
  gmssl≡仿射逐位一致，并用独立参考实现解回生产路径密文（外部验证，非自回环）。
  仿射实现留在 `SM2Box` 兜底位与 `tests/sm2_ref.py` 作验证尺，不再宣称“已替换”。
- **FakeTransport 解开请求（质量问题 2）**：记录新增 `business`（用该请求自己的 SM4 key
  解开的业务体 JSON/原始文本）与 `envelope_verified`（key 是否由信封私钥/fixed_pair 真实解出，
  兜底路线为 False）；`require_verified_envelope=True` 时不可验证信封直接断言失败。
  dry-run 输出报告 `verified n/total` 与“兜底等效假设”声明，产物名
  `offline_client_compatibility`（非 `face_passed_live`）。
- **错误消息泄漏（质量问题 3）**：`HttpStatusException` 不再嵌入响应片段原文；
  JSON 体输出 `redact` 后结构、非 JSON 体只报字节数+sha8。客户端自造文案改用
  `TransportOutcomeUnknown`（不经脱敏，保留“结果未知，勿自动重发”字样）。
  `post_json` 的“响应不是 JSON”同样只输出脱敏摘要。
- **default_post headers 静默丢弃（质量问题 4）**：`gen_sign=True` 且传入 headers →
  `ValueError` 显式报错（`test_default_post_headers_rejected_on_signed_path`）。
- **全局变量/默认构造联网（质量问题 5）**：仍属未完成的 B/C 范围，本文档不宣称完成。

## 二、人脸实施（方案 1 + 方案 2 source，模块 `yun_face.py`）

### 处理链（静态行号内嵌源码注释）
- 端点：比对 `/run/appFace/runFaceInfoComparison`，体仅 `{faceBaseData, recordId}`；
  注册端点存在但**本仓库不提供自动注册调用**（约束延续）。人脸端点不在 gzip 白名单。
- 三分支图像处理：正常比对（EXIF 3/6/8→180/90/270 →镜像仅显式约定→ q100 →
  仅宽>720 等比缩 720/q90，宽≤720 不重采样→ >153600 走 Luban(近似)→ 质量阶梯
  80,75,…,20 首个 ≤153600，全超即失败）；注册分支（q100+ignoreBy(100) 阈值）与
  超时抓拍分支（q90+ignoreBy(100)）独立实现、独立测试。
- Base64：无换行、无 `data:` 前缀、保留 padding（Android flag=2 语义）。
- **明示不成立的**：Pillow≠Android Bitmap 字节一致；Luban 内部算法未证实，
  以“近似透传到阶梯”处理并在 steps 里打 `APPROX` 标记（差分基准属方案 3，未做）。

### 取景质量门
- `check_framing` 按 K()（:531-602）逐条同序同阈值：包含判定、宽占比 40%~65%、
  top/bottom/left/right、偏航 0.15、俯仰 0.3~0.8、翻滚 15°；手动拍摄分数门 score≥0.6。
- 坐标空间三分：原图（image）、检测模型 640 letterbox（model640+xOff/yOff）、
  预览控件（preview）；`map_point_model640_to_preview` 原样移植 DrawResult.java:35
  （包括两轴分母同用 640 的写法，不“顺手修正”）。照片源约定“摆正后原图=预览面”，
  作为输入适配层新增约定标注。
- 除零按 Java float 语义（`java_div`）：0/0→NaN 不触发拒绝、x/0→±Inf 触发；
  零分母/无检测/多人脸选脸策略均有显式失败路径（无检测器=失败，绝不说“图中有人=通过”）。
- 检测器为注入点（`FaceDetection`/callable）；仓库不内置模型权重（RetinaFace ONNX
  复现属客户端完整一致性的后续项，见评审 §五）。人工标注 JSON
  （`--face-detection`，`load_detection_json`）标记为“人工指定，非模型输出”。

### 状态机（评审 §二.4“专用状态机”逐条）
- `FaceVerifier`：首传失败 → 立即重试 ≤3 次（间隔 1s，L() :606-624）→ 30s 等待态
  （Z() :696-707），每秒 tick、每 3s 且非在途时**重用同一份最终文件**再传（f :345-373），
  耗尽 → `识别超时(3004)`。`code=200 & status!=Y` 为终端性比对失败，不重试。
  会话终止标志先行检查，迟到回调丢弃；重试只存在于人脸状态机，通用 HTTP 仍零自动重试。
  全失败路径断言尝试数 1+3+9 与 sleep 序列；同文件重用以字节同一性断言。
- `WindowTrigger`（W1 :2854-2900）：距离(米、int 截断)跨越判定 `m>prev && m<=cur`，
  首轮只建基线；isShow 置位不重复；单窗口在途期间仅推进基线；
  idStr=`str(recordId)+i`（a2() :2995）。
- `recover_unfinished`（o2 :3337-3368）：isShow=Y 未上传成功按 (faceTime+4)−elapsed
  分流“可重开/已过期”，上传成功但比对未成功判失败。**未做断点持久化**（D 阶段范围）。

### 与主流程集成
- 停止门升级：`runFaceStatus='N'` 放行；`'Y'` 且无 `--face-photo/--face-video` 仍停止
  （A 阶段行为保持，且新增“缺 randomList 不放行”）；未知/缺失照旧停止。
- `do_by_points_map` 每批 splitPoint 后以累计 runMileage 推进 `WindowTrigger`；
  触发→执行窗口（4s 语音引导经注入 sleep）→成功继续、比对失败/结果未知停止并保留现场。
- **与 APK 的有意偏差（记录在案）**：APK 人脸失败会 `T1("faceFail")→checkRunState`
  自动提交成绩（:2798-2810）；本脚本**不自动提交**，交人工决定。
- CLI 新增：`--face-photo`、`--face-video`（可选依赖 opencv-python，注入
  frame_provider 可离线测）、`--face-detection`、`--face-mirror`（照片源默认不镜像）。
- dry-run：`--dry-run --face-photo ... --dry-home <Y-fixture>` 走完整
  窗口→比对→继续→finish 假链路；比对回包按 `code+data.status` 分层。

## 三、验收分层（§四 口径）

| 层 | 状态 |
|---|---|
| A 整改回归 | Python 已验证（47+42+ 项断言；评审复现脚本四项全翻转） |
| 输入适配/取景门/图像处理/调度/重试 | Python 已验证（规则层一致；fixture 覆盖阈值两侧、零分母、NaN/Inf、边界截断） |
| 编码传输 | Python 已验证（FakeTransport 解开信封+业务体断言键集/recordId/Base64 无换行/不 gzip） |
| 结果处理 | Python 已验证（六态：status Y/N、空 data、code≠200、HTTP 错误、会话终止迟到回调） |
| 图像字节一致（Pillow vs Android） | **未验证**（需方案 3 Android 参照程序，本轮未做） |
| Luban 实际算法、检测模型输出、超时/重试真机全分支 | 静态部分确认 / **未验证** |
| 服务端接受、活体/实时性、成绩有效性 | **未验证**（无账号；禁用网络） |

产物命名：`offline_client_compatibility`。本文档不包含任何“必过/成功率”表述。

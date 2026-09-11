# 项目框架文档

适用分支：develop。面向读代码/改协议的维护者；使用方式见 [USAGE.md](USAGE.md)。
反编译基线：云运动 APK 3.6.6（jadx 产物 `work_dir/src`，不随仓库分发）。

## 1. 模块地图

```
main.py            入口 + 旧全局快照兼容层 + 跑步会话(Yun_For_New)
                   + 线上载荷投影(_build_split_body/_build_finish_body/_project_card_point)
yun_http.py        协议边界：请求构造/信封加密/响应解码/脱敏/异常分类/假传输
yun_face.py        人脸管线：图像处理/质量门(APK K() 等价)/比对状态机/窗口调度/输入源
tools/Login.py     账号密码登录（学校目录探测 + 写回 config）
tools/drift.py     轨迹漂移；tools/pace_changer.py 配速工具；tools/proxy.py 抓包辅助
tools/getUrl_Id.py 学校地址/id 探测；history.py 历史记录拉取（显式注入客户端）
tools/EasyAutoRunServer/  旧定时批量启动壳（run.sh 仍走 main.py）
tests/             99 项离线测试（无任何真实网络）
config.ini         设备/密钥/跑步参数；tasks_*/tasklist_*.json 任务表（抓包模板）
dry_run_home.json  --dry-run 的 getHomeRunInfo 假响应
```

依赖方向：`main.py → yun_http.py`；`main.py → yun_face.py`（yun_face 不 import main）；
`tools/Login.py → yun_http`。

## 2. 单请求生命周期（与 APK RetrofitService 拦截器等价）

```
调用方 (Yun_For_New / FaceVerifier / history)
  └─ YunClient.post(router, json_text|raw_bytes)
       ├─ new_context(): uuid(默认每请求随机大写) + utc(秒) + sign(MD5) + SM4 key
       │    [legacy_uuid=True 时改用配置固定 uuid]
       ├─ content = SM4_ECB(b64key, json)；白名单接口先 gzip_apk(json) 再 SM4
       ├─ cipherKey = SM2(base64(SM4key))
       │    gmssl 3.2.2 主路径，wire = base64(0x04 || gmssl字节)；
       │    上游偶发 TypeError 时换随机数重试≤3 次，才退仿射兜底并计数 fallback_count
       ├─ headers = RequestContext.headers()（11 项，见 USAGE §6）
       └─ transport(url, data, headers, timeout)
            默认 _real_transport：进程共享 requests.Session
            （清默认头 + okhttp UA/Accept-Encoding + 持久 CookieJar）
            测试/dry-run：FakeTransport（解信封、解业务体、按请求 SM4 key 回包）
  响应: decode_response() 三形态统一（明文 JSON / SM4 / SM4+gzip）
        HTTP!=200 → HttpStatusException；传输异常 → TransportOutcomeUnknown
```

**不重试原则**：状态改变类请求（start/split/finish/人脸比对）任何失败都不自动重放，
由调用方按"结果未知"报告。

## 3. 线上载荷投影（服务器可见字段对齐层）

APK 用 Gson（`serializeNulls`）序列化 `UpPointsModel`，用 `org.json` 手拼 finish 体；
脚本在 `main.py` 投影函数里复刻字段集合、声明序与类型：

| 位置 | APK 证据 | 脚本处理 |
|---|---|---|
| split 体键 | UpPointsModel 字段声明序；时间键是 `times`(long) | `_build_split_body`，断言键序；无 `time` 键 |
| StepNumber/speeds/strides/runSteps | int/double 且互为派生（Y1:2945-2959） | 同一组派生公式（步数由里程/步幅估算，数值属已知偏差，格式对齐） |
| cardPointList 点项 | UpPointModel 仅 9 字段、类型严格 | `_project_card_point` 裁剪 bean 外键并提示一次 |
| b/c 为 null | GsonUtils `serializeNulls` | 显式 null |
| finish 体 | P1():2535-2600 插入序、全字符串 | `_build_finish_body`；`sysEdition="Android_"+版本`；`remake="0|{}"`（BaseCheckUtil 干净设备形态）；空 manageList 不写键 |
| start 体 | HashMap<String,String> | 三字段全字符串 |
| gzip 容器 | Java GZIPOutputStream 头（实测 MTIME=0/XFL=0/OS=0xff） | `yun_http.gzip_apk` 手工容器 |

回归护栏：`tests/test_wire_alignment.py`。

## 4. 人脸子系统（yun_face.py）

```
WindowTrigger.on_distance(km)     W1 等价：int(win*1000) 跨越判定、首轮仅建基线、
                                  每窗口一次、在途互斥
FaceRunner.run_window             语音提前 4s → 取源帧 → 检测框 → 质量门 →
                                  图像处理 → runFaceInfoComparison → 状态机
PhotoSource / VideoFrameSource    方案1 照片 / 方案2 视频选帧（frame_provider 可注入，
                                  不硬依赖 opencv）
process_face_image(branch)        compare/register/timeout 三分支独立：
                                  EXIF 仅 3/6/8 旋转；仅宽>720 缩 720/q90；
                                  >153600B 走质量阶梯 80..20；
                                  Luban.ignoreBy 未证实 → APPROX 透传标记
quality_gate / check_framing      K()(:531-602) 逐条同序同阈值；
                                  DrawResult 映射原样移植（两轴分母同 640 的字面写法）；
                                  零分母按 Java float 的 NaN/Inf 语义
FaceVerifier.run                  首次上传 → ≤3 次立即重试(1s) → 30s 等待期每 3s
                                  复用同一文件重发 → 3004；
                                  code=200 且 status!=Y 为终端 compare_failed（不重试）
windows_from_random_list          窗口构造（idStr = recordId+序号）
recover_unfinished                o2(:3337-3368) 断点恢复分类（纯函数）
```

边界：检测模型（RetinaFace）未移植——检测结果是注入点，**缺检测=不放行**；
注册接口存在但永不自动调用；比对失败不自动提交跑步数据（APK 会提交，
此偏差是刻意的，见 `_face_on_mileage` 注释与 USAGE §4）；窗口断点持久化未实现。

## 5. 配置与全局变量契约

- `set_args(conf_path)` 加载配置 → 模块级全局快照 + 构建 `_CLIENT`（构造不发网络）。
- 登录成功走 `apply_login_result()`：无论是否写盘，内存态
  （token/设备身份/sysVersion/base_url）立即同步到 `_CLIENT.profile`。
- 时钟/随机源/sleep/transport 全部可注入（`YunClient(now,rng,sleep,transport)`），
  测试与 dry-run 零网络副作用。
- 旧函数签名（`default_post/getsign/encrypt_sm4/...`）保留为薄委托；
  `default_post(headers=..., gen_sign=True)` 显式报错（不再静默丢头部）。

## 6. 测试分层（99 项，全部离线）

| 文件 | 层 |
|---|---|
| test_yun_http.py | 密码 golden 向量、SM2 24 样本多 k 交叉验证、信封/解码/脱敏 |
| test_wire_alignment.py | 服务器可见字段集合/类型/顺序、HTTP 头、gzip 容器头字节 |
| test_main_phase_a.py / test_phase_a_fixes.py | 会话流程、登录连续性、canSport/faceTime、CLI 契约 |
| test_yun_face.py | 质量门几何、图像链、比对状态机、窗口调度、照片→线上格式 E2E |

命名约束：自动验收产物只叫 `offline_client_compatibility`；测试反向锁死
"线上已验证"类文案不得出现。

## 7. 已知偏差 / 未验证清单（勿当 bug 顺手"修复"）

1. 人脸失败后不自动提交跑步数据（APK 会）——策略性偏差。
2. StepNumber/runSteps 由里程/步幅估算；simulateNum 恒 0；remake 恒 "0|{}"——
   无传感器/无设备自检环境下的格式对齐近似。
3. Luban 压缩库行为未证实：>150KB 路径直接进质量阶梯（APPROX）。
4. 人脸挂钩只接在 do_by_points_map 路径（高德 do() 路径未接）。
5. 断点续跑（窗口持久化）未实现；恢复逻辑为纯函数。
6. 服务端一切（接受性、字段依赖、人脸判定阈值）未线上验证。
7. TLS/HTTP 栈指纹（requests vs OkHttp/Conscrypt）、HTTP 版本协商、请求节奏等
   字段之外的可观测面不在本项目控制范围（详见开发期审查记录 WIRE_AUDIT，
   不随仓库分发）。

## 8. 维护提示

- 反编译重跑：`work_dir\tools\jadx\bin\jadx.bat -d work_dir\src --show-bad-code work_dir\云运动.apk`
- 开发文档（评审基线、实施方案、线上审查记录）放 `work_dir/develop_docs/`
  （gitignored）；`docs/` 只放面向使用者的文档。
- 密码实现处置：生产加密只允许 gmssl 主路径（换 k 重试 + 仿射兜底），
  修改前必读 `SM2Box` docstring 与 `tests/test_yun_http.py` 的交叉验证。

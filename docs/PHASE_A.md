# A 阶段交付说明（依据 docs/review_and_revised_plan.md §3-A）

日期：2026-09-11。范围：小范围兼容基础，不宣称完整跑步链路已适配；未使用账号，未执行任何线上请求。

## 交付内容

| 评审条目 | 落地位置 |
|---|---|
| A.1 最小纯请求构造/响应解码边界；保留 requests 与既有加密；每请求绑定 uuid/utc/key | 新模块 `yun_http.py`：`DeviceProfile` / `RequestContext` / `YunClient`；`main.py` 的 `default_post`、`getsign`、`encrypt_sm*` 等旧签名保留并委托 |
| A.2 统一主请求/登录/学校查询的头部与 URL 规则；设备版本从配置读取；旧 CLI 保持并传递实际配置路径 | `YunClient.post` 单点构造头（含 3.6.6 新增 `sysVersion`）；`join_url` 统一 URL 边界；`[User] sys_version` 缺省沿用 `sys_edition`；`Login.main(conf_path)`、`getschool_Url_Id(..., conf_path)` 贯穿路径；CLI 参数名一字未改，`--dry-run` 等仅增量别名 |
| A.3 历史详情解码收敛；删除二次解密 | `yun_http.decode_response` 统一三形态（明文 JSON / SM4 / SM4+gzip）；`history.py` 移除 `key_ctx`+gzip 二次解码，`from main import *` 改为显式注入 |
| A.4 超时无自动重试；日志/dry-run 脱敏 | `DEFAULT_TIMEOUT=(10,30)`；状态改变请求失败抛 `HttpStatusException`（文案标注“结果未知，勿自动重发”）；`mask_secret`/`redact` 默认掩掉 token、password、密钥、content、人脸字段 |
| A.5 dry-run 本地 fixture + 假传输 | `--dry-run`：`FakeTransport` 全程离线；不登录、不探测学校、不调高德、不 sleep（注入 no-op）、不写配置；home fixture 缺失直接报错而非回退真实请求 |
| A.6 识别人脸要求即停止 | `Yun_For_New.__init__` 停止门：`runFaceStatus=='N'` 才放行；`'Y'`→提示走官方 App；缺失/未知按未通过停止；`/run/start` 响应 `canSport=false` 或 `faceTime>0` 同样停止 |

## 评审条目对应修复（§2）

- 2.1.3 旧 CLI：`--config_path/--task_path/--auto_run/--drift` 与短参保持；`history.py` 的 `--conf_path/--history_path` 保持。
- 2.1.4 `history.py` 改为显式客户端注入；旧全局保留为 `set_args` 时快照，不承诺动态同步。
- 2.1.5 `tools/Login.py` 不再硬编码 `./config.ini`；`tools/getUrl_Id.py` 导入时不再读配置（副作用移除）。
- 2.1.6 路径契约：显式相对 CLI 路径按调用者工作目录解析（`INITIAL_CWD` 在 `os.chdir` 之前捕获），缺省资源按项目根解析；`tools/EasyAutoRunServer/run.sh` 的 `python ../../main.py -f=./configs/*.ini -t=../../tasks_fch` 调用方式在契约下成立（旧版 chdir 会使这类相对路径解析错位）。
- 2.1.7 未做任务文件物理迁移、未建软链接，`tasks_fch/tasks_txl/tasks_xc` 原样。
- 2.2 表“loader 丢 runStep”行：`do_by_points_map` 现在以原点为基底覆盖可控字段，真机 `runStep`/`ts` 等进入上传体（测试钉住）。
- 2.2 表“正常 finish 必加 recordEndTime”“finish/isStandard 同体”“time 改名足够”“b/c 恒 null”等行：属 C 阶段字段/状态机工作，本阶段刻意未动（`remake:'1'`、`time` 键等原样保留，避免混入未验证协议改动）。
- 2.3：人脸重放、底照注册、assist 均未实现，只实现停止门。

## 本轮新发现（静态与离线验证）

1. **gmssl 3.2.2 SM2 缺陷**：`CryptSM2.encrypt` 存在偶发 `_add_point(None)` 崩溃；`CryptSM2.decrypt` 自回环输出乱码。`yun_http.SM2Box` 两侧改用模块内仿射点运算实现，并在固定随机数 k 下与 `gmssl.encrypt` 输出逐位一致（wire 格式 `04||C1||C3||C2` + base64 不变，即服务端实测接受的格式），见 `tests/test_yun_http.py::test_sm2_wire_matches_gmssl_for_fixed_k`。
2. **仓库默认 SM2 密钥对不配对**：`config.ini` 的 privatekey 与 publickey 不满足 `d*G==PUB`（注释自证“私钥失效”）。生产请求封装只用公钥，不受影响；但依赖私钥解密的旧教程路径（decrypt_task_tutorial.ipynb / `decrypt_sm2`）在当前密钥对下不可用。测试改用独立生成的匹配密钥对（`tests/fixtures/test_config.ini`）。
3. `tools/Login.py` 旧版 `from getUrl_Id import ...` 在仓库根入口下直接 ModuleNotFoundError（登录路径此前是坏的），已修。
4. requirements 钉住 `gmssl==3.2.2`（3.2.1 的 `CryptSM2` 无 `mode/asn1` 参数，与本仓库用法不兼容）。

## 验收状态区分（评审 §5 要求）

- **本地校验通过**：`pytest tests`（31 项）：SM4 GB/T 32907-2016 黄金向量、sign 字段序黄金向量、SM2 随机密文回环+与 gmssl 逐位一致、解码三形态与错误三分类、每请求上下文绑定（随机/固定两路线）、人脸停止门三态、loader 保真、dry-run 全假传输且不落敏感字段、旧 CLI 解析、路径契约、缺失配置显式报错。
- **协议证据吻合**：`sysVersion` 头、每请求随机大写 uuid、固定 cipherKey 路线保留、`crsReocordInfo` SM4→gzip 解码链——均以反编译代码行号标注在源码注释。
- **线上验收未执行**：服务端是否仍接受固定 cipherKey/旧密钥值、`runFaceStatus` 实际取值面、登录/开始/分段/结束的接收与成绩有效性，全部未验证（无账号）。A 阶段不宣称线上可通过。

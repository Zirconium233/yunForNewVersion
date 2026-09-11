### 寄了，但是没完全寄

~没想到这套AI时代之前的代码，能在无维护的情况撑一年，最后倒在了3.6.4的人脸上面，详见[issue](https://github.com/Zirconium233/yunForNewVersion/issues/78)~
~这个项目大概率是死了，我会尝试去收集一下新版本云运动的人脸相关信息，看看能否用比较合适的方式解决。~
~- 如果搞不定，项目会转为archived，不推荐fork，参考解密的实现方式即可，fork反而拉低AI的代码质量。（要是搞定了一定要开源啊，别闭源拿去卖钱了！）~
~- 如果能搞定，我会更新项目的。（欢迎勇士提供有跑步任务的账号，作为人脸过验证的实验田）~

学长已经大四了，云运动里面没有任何跑步任务，连包的抓不了，已经失去维护项目的条件了 T_T 不过静态分析还是可行的。

以下关于人脸的分析全部基于反编译（3.6.6版本apk），由GPT-6 Astra逐条核实。

#### 1. 触发时间：谁决定什么

**服务端决定全部要素，客户端只做"距离到达→弹窗"的执行**，且服务端从不推送"现在弹"的指令。决策链分四层：

| 层 | 包 | 决定 |
|---|---|---|
| 启用与否 | `run/getHomeRunInfo`（任务列表）响应 | 该跑步区域本次任务是否启用人脸（`runFaceStatus`） |
| 跑点与预算 | `/run/start` 响应 | 本次会话每个人脸校验点的**距离位置**（`randomList`，服务端随机下发）和单窗倒计时（`faceTime`） |
| 账号准入 | `run/getRlStatus` 响应 | 账号人脸注册状态（N1 时连正版 APP 都拒绝开跑） |
| 结果判定 | `run/appFace/runFaceInfoComparison` 响应 | 单次比对通过与否（`data.status=="Y"`） |

客户端侧的"触发时刻"= 累计里程跨过 `randomList` 某项（弹窗前有 4s 语音引导；弹窗后的总预算 = `faceTime+4` 秒）。服务端事后对整条记录做核验，其核验算法离线不可见，只能尽可能模仿客户端的行为。

#### 2. 相关配置参数（包 → 参数 → 功能）

**决定人脸行为的全部字段**

| 端点/方向 | 字段 | 语义 | APK 消费点 | 我方实现 |
|---|---|---|---|---|
| `run/getHomeRunInfo` 响应 `data.cralist[]` | `runFaceStatus` "Y"/"N" | **人脸验证唯一启用开关**（区域任务级） | NewRunningFragment:1079 启动前分支；SportRunMapActivity:4314 `B1="Y".equals(...)`、:3056 `N0.setNeedFace` | main.py:796-797 明示"faceTime 不是开关"；N=完全不执行 |
| 同上 | `raRunArea`/`id`/`raDislikes`/`raSingleMileageMin/Max`/`raCadenceMin/Max`/`points` | 任务基准（区域、踩点数、里程/步频约束、围栏点）；randomList 取值范围落在里程区间内 | 任务卡片→start | 打表与守卫上下文用 |
| `run/getRlStatus` 请求 `{raRunArea}` 响应 `data.runFaceStudentStatus` | Y=注册通过可跑；N=未注册（APP 强制先去 `runFaceInfo` 采集）；N0=认证失败重采；**N1=审核中，禁止跑步** | NewRunningFragment:929-932 构造、:640-698 四分支 | 仅 `live_probe.py` L1 只读探测（退出码 0/5 区分 Y/非Y）；正式跑流程不调用 |
| `/run/start` 响应 `data.id` | crsRunRecordId（字符串）——比对包 `recordId` 唯一来源 | 结束链/比对共用 | `build_compare_body` 强转 str |
| 同响应 `faceTime`（int，秒） | 单窗口倒计时；**APK 对 <10 的值夹到 10**（:787-788 `if(K1<10)K1=10`）；窗口总预算 `faceTime+4`（:1500、:2172 `(K1+4)*1000`、:3344） | start 回调 f0；断点续跑从本地 RunTaskModel.FaceTime 恢复（:3126） | `yun_face.FACE_TIME_FLOOR=10`（main.py:803-804 同下限）；**Y 任务 faceTime 缺失/非法 → 拒绝执行**（:812-815，不默认放行） |
| 同响应 `randomList`（List\<Double\>，km） | 本次人脸校验点距离列表；每项生成一个窗口：`FaceRunWindowBean{idStr=recordId+序号, window=值, isShow="N", ...}`（a2() :2989-3012，落 GreenDao） | MAP:786、a2() | `windows_from_random_list`（main.py:818）；**Y 任务 randomList 缺失 → 停止**（:808-810）；int(km×1000) 米制跨越判定 |
| `/run/appFace/runFaceInfoComparison` 响应 `data.status`/`msg` | Y=该窗口通过（唯一记成功值）；非 Y=终端失败不重试；code≠200/HTTP/解码=可重试的传输失败 | JTFaceCompareActivity:737/845、f:370、L():607-611、3004 | `compare_once`/`FaceVerifier` 逐态对齐 |
| `/run/isStandard` 响应 `data.isStandard/isCheat/msg/url/list` | 结束前有效性预检；**`url/list` 非空 = 服务端要求补拍/复核**（人脸关联分支） | b0 回调 :481-514 | `_finish_state_check`：非200/解码失败/超时→尾批+finish 不发；url/list→明确停止 |

**客户端本地字段（不上行，但是"完成度"的依据）**：`FaceRunWindowBean` 的 `voiceSecond/voiceTime`（弹窗时刻）、`uploadSuccess/compareSuccess/reason`——正版靠它+GreenDao 做断点续跑；我们可以用同构跟踪（`WindowTrigger` + 窗口记账）支撑守卫。config.ini `[Run]` 全部与人脸无关；`[User].legacy_uuid` 仅协议回退。

#### 3. 当前的核心对策

- **参数严格性**：开关只认 `runFaceStatus`；Y 任务缺 `faceTime`/`randomList` 直接拒绝；faceTime 下限 10 与 APK 一致；N 任务带窗口参数只提示不执行。
- **W1 事件语义等价**：起点基线 0（首批跨窗不漏）、`int(window_m)` 边界算跨越、非单调忽略、每窗一次、在途互斥（在途漏跨与 APK 相同不补偿）；其上叠加自加护栏——finish 前完整性检查（范围内任何窗口未弹/未确认 → 拒绝 finish）+ expired 窗口拦截后续 split/finish。
- **时钟与预算**：双轨（utc/sign 走 epoch，一切预算走 client.mono）；重试状态机对齐 a0（会话终止丢弃）、f:370（3 次即时间隔 1s + 等待期每 3s 复用同图、30s 耗尽报 3004）；`faceTime+4` 与 `pending_seconds` 两道截止，**恰好压线或越界的成功一律丢弃**（reviewer 小修后连请求的 connect/read 裁剪都按阶段剩余预算，<0.05s 直接停发）。
- **图像链**：EXIF 摆正→镜像声明→限宽 720（不限长边）→质量阶梯 80..20 压至 ≤150KB，与 FaceImageCompressor 逐字节对齐；取景质量门（人脸占比/俯仰/偏航/滚转）复刻相机 UI 提交前门；内容哈希（sha256）绑定 + 全量预检（解码→标注匹配→质量门→最终压缩形态）在 **start 之前**完成并缓存（照片与视频预检帧均锁死为已验证内容，运行中换源文件不影响上传）。
- **比对上传**：两键体 `{faceBaseData, recordId}` 逐字对齐；只有 `data.status=="Y"` 记成功；成功以外的终端失败不重试。
- **结束链**：isStandard 前置门——失败/未知/`url/list`（补拍分支）时尾批与 finish 都不发，不猜测服务端后续流程。
- **边界与诚实**：人脸采集链（`runFaceInfo`）有意不实现不自动调用（真人审核材料，代发=伪造）；准入状态用只读 `live_probe.py` 先行探测；服务端复核语义未线上验证（概率分层表见 README §2）；全部离线可证部分由 155 项测试覆盖（含禁网守卫复跑）。

#### 4. 如何使用最新的实现

**注意：最新的实现只经过和客户端逻辑的严格比对，未经过实际测试（建议等我借到账号跑完验证）**

**目前只推荐作为开发者和你的harness一起入场尝试，因为很可能跑出来的记录还是不合格**

使用步骤：
1. 切到develop分支，那里包含了当前的最新实现，详细的说明在对应分支的`README.d`和`docs/`文件夹下。
2. 准备一张**人脸证件照**，或者准备你**跑步时候的自拍**，以及config.ini里面抓包获取到的参数（Astra说已经重构了登录功能，如果你愿意可以试试看）
3. 按照develop分支的说明部署，或者让你的harness帮你部署。你也可以直接下载后就让它帮你部署，它要什么就给什么，期间唯一需要自己动手的是抓包，看README底下之前的教学即可。

建议：
1. 如果你成功完成了跑步，欢迎反馈到issues中，develop分支将和master合并，后续会开发GUI版本，降低使用门槛。（所以我就不用借号测试了）
2. 如果出现了问题，请用你的harness debug一下，很可能根据反馈小调几个参数就解决了，欢迎把你验证通过后的代码PR进来，并在README里面留下说明，成为本项目贡献者之一；
3. 如果出现了问题，并且你的额度归零了、API欠费了、模型太笨了、服务器反馈到信息太少等等总之无法解决问题，欢迎在issues里面反馈你的日志。**有效的信息越多，这个项目能维护下去的希望越大。**
4. 不过请注意核对不合格原因，别把跑步时间不在要求的时间范围内这种问题当成了代码问题猛干一晚上发现早上自己好了。。。

### 简介：

这是(3.4.8)云运动代跑脚本，可以进行云运动全自动代跑。

### 更新记录：

- 2026/9/12：
   1. 对云运动的人脸问题安排了对策，但未经过实际测试。
 
   2. 用Astra重构了代码，我承认我在24年手搓的代码质量，一坨。。

- 2025/12/9：
   1. 感谢 10punny 解决gmssl和hutool的验签问题，加密函数加上04头就可以被后端正确解密。现在我们可以使用随机密钥了(注意是随机加密，不是解密)详见[PR](https://github.com/Zirconium233/yunForNewVersion/pull/75)
      
   2. headers的user-agent被顺手更新成了4.9.1，虽然服务器一直都是忽略这个的。

- 2025/3/16:
   1. 封装抓历史记录功能。


- 2025/2/25:
   1. 修复3.4.7版本公钥密钥变换问题，脚本基本功能已经恢复。


- 2024/12/3: 
   1. 合并xiaocheng4097代码，提供登录功能支持，可以不抓包直接登录。

   2. 增加自动版本检查，现在会自动检查`config.ini`里面的`app_edition`版本信息，如果小于3.4.5会自动更新最低可运行版本3.4.5，高版本不会更改(截至12/3日，最新版本为3.4.5)。过低的版本会导致服务返回错误信息，详见[issue#35](https://github.com/Zirconium233/yunForNewVersion/issues/35)。

- 2024/10/28:
   1. 合并laizhangtu代码，现在代理工具可以批量抓取config了。

- 2024/10/18:
   1. 修改并合并xiaochen4097代码，提供随机偏移添加功能(路线改变效果并不明显，所以也不会鬼畜)。

   2. 有人测试发现ios版本也可以直接抓包token和deviceId，uuid使用当前代码，虽然很逆天但是真的过了。(还是不建议使用iOS登录信息跑本脚本)

- 2024/10/12: 
   1. 合并ANormalDD代码，提供屯溪路校区地图和自动抓包(配置教程见proxy.md)支持。

   2. 允许传递参数执行`main.py`，提供`./tools/EasyAutoRunServer/run.sh`批量并行运行多个config的任务，配合crontab即可定时批量运行跑步任务(挂一个云服务器上就可以全自动)。

- 2024/9/21：其实我什么都没干，然后它自己又能过了，实锤了是学校服务器问题。

   <img src="./image/pass.png" alt="image" style="zoom:50%;" />

   注意事项：
   1. 新版本**无需填写config里面的utc和sign参数**(留空就行，直接把那2行删了会报错)，脚本会自动生成utc，然后和uuid计算得到sign。详见 [issue#1](https://github.com/Zirconium233/yunForNewVerison/issues/1)
   2. finish包500的问题自己好了，不知道是学校服务器是草台班子还是采用即时生成utc方法解决的。现在finish返回的是code 200。


- 2024/9/10：给points添加了时间戳数据，目前已经通过splitPointCheating接口测试，finish包还没过测试(神tm要求大二在2月到7月跑步，穿越时空是吧，看上去学校忘记调时间了)

- 2024/5/3：更新随机提速脚本，用python复现了[Ma-minghao/Yunyundong (github.com)](https://github.com/Ma-minghao/Yunyundong)，因为原作者说python不熟...

   <img src="./image/paceChanger.png" alt="image" style="zoom:50%;" />

- ~~2024/5/2：更新了一个小工具，用java实现了端到端的解密，[Source code](https://github.com/Zirconium233/JavaSmDecryptToy)，各位再也不用麻烦费事的找在线解密网页了。~~（3.4.7失效，私钥不对，当然如果有人能拿到正确的私钥还是能用的）

   <img src="./image/javaTool.png" alt="image" style="zoom:50%;" />

   

- 2024/4/3：更新多图随机打表模式，现在可以随机选择多套图中的一套来跑步了，同时也更新了定时系统，现在可以直接输入时间，自动随机选图打表。

- 2024/3/31：更新main.py，现在打表模式再也不需要高德地图key了。顺便给了一个计数器工具，帮助各位自动7:30晨跑(但是你还是要支付电脑放一夜的电费)

- 2024/3/14：添加打表模式，修复路径问题。(可惜finish返回500的问题还是没解决，不过不影响用，就是看着难受)

   <img src="./image/goodMap.jpg" alt="image" style="zoom:50%;" />



### 使用方法：

**概览：**

1. `pip install -r requirements.txt`
2. 配置`config.ini`文件(自己抓包或者详见(proxy.md)，只填uuid, token, device_id, device_name 4个就行)
3. `python history.py` 拿历史记录(有预置的可以直接跑，外校区需要配置)
4. `pyhton main.py`(可以附带参数)
5. 按照提示操作即可

**细节：**

1. 
   - headers: 3.0.0新版本补充了utc，uuid，sign等参数(*3.3.1只需要uuid了，utc和sign可以自动生成，不建议写死*)，同token和deviceId一样需要获取，建议抓包获取，登录功能未经过测试，不保证功能。
   
   - 快速模式：无需等待直接通过，不过没有轨迹，但是程序算你过(不是实在没时间别用，被人工干了别找我.jpg)
   
2. 配置`config.ini`的具体事项：
   - 必填: token,device_name,device_id,uuid，抓包获得，不建议更改。token决定了你可以访问你的账户，device_name和id是检测多机的，uuid和一个固定的随机数一样，应该也是检测多机的。
   - `utc`, `sign`，`utc`是时间生成的随机数，`sign`是`utc`和`uuid`2者的md5值，具体怎么算的看代码就行了，服务器只会验证`sign`是不是前二者的md5，所以可以一套用到死，3.3.1以后脚本会自动生成这些参数，这2个不用填了。
   - 可选：填写map_key，现在，打表模式再也不需要map_key了。
   - 备注：只需要填写user部分就行，其他地方我已经设置默认值，如果你不知道那是什么，请不要更改。
3. 关于`config.ini` 与 `tasklist.json`，可以使用`proxy.py` 快速配置。详细教程请参考[说明](./proxy.md)

**打表模式：**

- **简介：**

1. 使用`task`文件夹的json文件控制轨迹，json文件来源是历史记录的抓包。
2. 合工大翡翠湖和屯溪路校区有默认提供的表格，无需配置表格即可直接使用。


~~更新--java解密小工具，专门用于解包：**(Dead，别看了，现在没有人可以解密)~~

~~- 简介：如果你有java，双击打开`./tools/decrypt_java.jar`就行了*(jdk-17.0.9)*。(后续更新提供了命令行版本)~~

**其他校区或者学校要额外配置：**

1. 确保你config里面的school_host改对了
2. 使用`history.py`获取跑步数据
3. 打表

6. **效果展示: **

  - 肉眼无法分辨真假的轨迹：

    <img src="./image/goodMap.jpg" alt="image" style="zoom:50%;" />

  - 进度条显示(只支持打表模式)

    <img src="./image/processBar.png" alt="image" style="zoom:50%;" />


**补充抓包教学：**

**新版本可以考虑直接使用ADNormalDD提供的`proxy.py`，教程见`./proxy.md`**

发现很多老哥卡在抓包上了，其实这个云运动是学校架设服务端，还用的http，所以基本上随便抓包，不用什么群里说的fiddler远程、CA证书、ss代理等，甚至还有群友kali都整上了...

其实没这么复杂，我这里介绍一个最简单的方法，**不用root，有一部手机就可以**：

1. Google Play上随便搜一个抓包软件(搞不定Google？你都能上github还搞不定Google？【笑)

   <img src="./image/googleplay.jpg" alt="image" style="zoom:50%;" />

2. 安装它，配置VPN给它过(演示用的群友给的`PCAPdroid`，你用哪个都差不多)

   <img src="./image/VPN.jpg" alt="image" style="zoom:50%;" />

3. 进云运动，随便翻一翻

4. 如果是对于合工大的，找到`ip`是`210.xxx.xxx.xxx:8080`的包就行

   <img src="./image/package.png" alt="image" style="zoom:50%;" />

5. copy里面headers的一切，照着填上去就行了

   <img src="./image/header.jpg" alt="image" style="zoom:50%;" />

6. 还有老哥问sys_edit这个参数，这个是安卓大版本，随便填一个就行，我一般填12，当然我手机是安卓13，服务器不会检查这个

**3.0.0导航模式的残留代码(新版本不推荐使用这种方式，很久没维护了)：**

1. `map.json`的点，你自己post一下getHomeInfo那个，对着地图选几个好看的点填上去就行。我把原作者的随机选点方法弃用改成了手动选点的方法，因为学校很贴心的把位置限定在了一个操场，选的点太乱会导致轨迹魔怔(虽然现在轨迹也很魔怔，不过起码不会抽搐了)

   **补充：**issue里面有老哥提到了轨迹问题，我详细介绍一下`map.json`的作用：

    	1. 这个`map.json`记录的是跑步的控制点，控制着给高德导航目的地的顺序，导航会从里面的上一个点走向下一个点。
    	2. config.ini里面有一个参数是初始点，这是导航最开始的点，别只改map忘记改这个了。
    	3. getHomeInfo的点是关键点，就是你跑步要踩点的几个点。
    	4. 把getHomeInfo的点copy过来相当于作者原本的导航直冲关键点方法，好处是方便，缺点是可能路径直来直去会魔怔。
    	5. 你可以自己对着地图选优质的点，按顺序填入，从而准确的控制脚本走的路径。
    	6. 当点距离足够近的时候还是推荐用导航，因为导航会返回距离，当然如果你有用经纬度精确计算里程的把握，你可以设计算法手动跑，这样就不需要高德的map_key了。
    	7. 经过测试，服务器的关键点也是以你给的点为准，你说什么点是关键点，有没有踩点，服务器就信什么。

   **代码实现细节：**

   普通模式，脚本默认会把`map.json`里面的点当成控制点上传给服务器(回跑的时候不会重复上传关键点)。这是一个偷懒的方式，如果你直接用getHomeInfo的点就不会有问题，当然如果你微操每一个点，打上100个，可能出现一次跑步100个关键点的逆天情况，这时候你可以改代码，比如每20个点add_task后才给manageList.append()一次关键点。*(你可以自己实现，我反正现在都是打表了)*

3. 如果是其他学校要改主机，这个很简单，替换一下就行，当然接口如果用的不一样那没办法，自己抓包研究吧(无慈悲)。

### 相关REPO：

之前的工作：感谢yun大佬的初代脚本[kontori/yun: 云运动一键跑步脚本，理论上适用于一切使用云运动的学校的健跑任务，包括但不限于合肥工业大学 (github.com)](https://github.com/kontori/yun)，为整个脚本提供逻辑框架，可惜作者停更了。

远古的最新消息：仓库公开前已经有人完成了相关工作，[StarYuhen/Yun: 云运动，协议一键刷路程脚本 (github.com)](https://github.com/StarYuhen/Yun)，不过用的接口和合工大的不同，已经测试了合工大用的是`/splitPointCheating`接口，`headers`也大改加了检测，所以合工大学生不能直接使用那个版本。那个项目issue里面提到的更新版本也是这个原因。(随口一提：其实我猜项目作者学校使用的才是老接口，合工大其实是新接口，那位打的其实是简单局，虽然难度也没差多少就是了)（错了当我没说...）

### 加密相关细节：

#### 云运动新版本的加密模式：

1. 随机生成sm4密钥，通过sm2加密sm4密钥。对应"cipherKey"参数
2. 用sm4密钥加密数据，对应"content"参数
3. 3.4.7换了公钥，私钥是乱给的，也就是现在我们不能解密，只能加密，详见开头。

#### 云运动防止修改的小细节：

1. sm2密钥存放于`crs-sdk.so`中，包括公钥和私钥。
2. 这个c++ 库会检查dex文件，如果文件被篡改，会返回错误的密钥(服务器可能因此判定软件作弊)。
3. apk安装包文件被360壳保护，需要脱壳才能反编译。
4. 服务器使用`/run/splitPointsCheating`域名，加入了检测，路径不能再魔幻了。

#### 本次实现的细节(偷懒部分)：

~~1. utc是随时间自动改变的，但服务器不会验证，所以可以不管，保证一套sign和utc、uuid对应上就行，具体表现是一次抓包获取一切。~~（已经修了）
2. cipherKey你给服务器什么服务器就用什么，所以我是直接默认给了一个cipherKey，用到死(偷懒，逃)，当然如果你愿意随机密钥，我提供了sm2加密解密函数和公钥私钥，你可以自己改代码实现。(其实原本不打算偷懒的，但java实现用的hutool和python的gmssl验签过不了，不知道什么原因，看上面那个实现也是一套cipherKey用到死，就不管了，逃~)
3. finish你说什么服务器信什么，你就是刚刚start原地没动下一秒finish说跑了2公里，服务器都信。路径点都不用上传的，我已经用这个方法干好几天了，系统算的是通过，所以就做了一个快速模式，还是不要用为好。

#### 加密破解方法(面向开发者)：

0. 这个加密的破解搞的头大，大一下课排满了，晚自习搞破解，要不然上周末应该就出来了(周六工程课进厂打工一天我"爱你"合工大)。
1. 反编译是通过Frida把真正的dex文件hook出来的，使用安卓虚拟机+adb。这里抓到的大多数是系统和依赖的dex，5秒延迟开深度大概率可以得到云运动的dex文件。
2. 使用dex2jar项目把dump出来的dex文件变成jar文件，然后使用jd-gui反编译找的加密代码。jadx的反编译不太行，用dex2jar的。
3. so库使用IDA分析的，IDA不是Pro居然还不给我分析ARM文件，逆天。
4. 2025/12/9: gmssl和hutool过验签要加一个04头，见 10punny 的 [PR](https://github.com/Zirconium233/yunForNewVersion/pull/75)

### 最后：

**这玩意随缘更新，有能力的推荐自己修改使用，把这个当成一个demo就好...**

*免责声明：一切内容只能用于交流学习，24h内自觉卸载，否则后果自负。*

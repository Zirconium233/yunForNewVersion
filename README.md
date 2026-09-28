### Master分支为3.4.8版本对策，对3.6.6无效。如有需要请切换到develop使用。

**注意：develop实现只经过和客户端逻辑的严格比对，未经过充分测试**

9月28：

1. 基本上确定人脸问题已解决（目前没有任何反馈关于这个），详见[issue78](https://github.com/Zirconium233/yunForNewVersion/issues/78) 

2. 有很大把握认为云运动会人工审查，目前主要针对重复的轨迹，不排除审查轨迹合理性的可能。因此后面不要使用相同轨迹多次跑步。详见 [issue82](https://github.com/Zirconium233/yunForNewVersion/issues/82)

3. 注意，打表模式的偏移方案是不可靠的！drift.py为24年PR提供，偏移质量很拉。现在只建议一天一张表，或者用现有的合成轨迹方案。
      

**如果你知道develop分支是什么、怎么用，欢迎参与讨论和后续开发；如果你不知道我下文那些内容在叽里咕噜说什么，请保持观望，避免造成损失**

### 简介：

这是(3.4.8)云运动代跑脚本，可以进行云运动全自动代跑。

### 更新记录：

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

**其他校区或者学校要额外配置：**

1. 确保你config里面的school_host改对了
2. 使用`history.py`获取跑步数据
3. 打表

6. **效果展示: **

  - 肉眼无法分辨真假的轨迹：

    <img src="./image/goodMap.jpg" alt="image" style="zoom:50%;" />

  - 进度条显示(只支持打表模式)

    <img src="./image/processBar.png" alt="image" style="zoom:50%;" />


**抓包教学：**

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


### 相关REPO：

之前的工作：感谢yun大佬的初代脚本[kontori/yun: 云运动一键跑步脚本，理论上适用于一切使用云运动的学校的健跑任务，包括但不限于合肥工业大学 (github.com)](https://github.com/kontori/yun)

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
~~2. cipherKey你给服务器什么服务器就用什么，所以我是直接默认给了一个cipherKey，用到死(偷懒，逃)，当然如果你愿意随机密钥，我提供了sm2加密解密函数和公钥私钥，你可以自己改代码实现。(其实原本不打算偷懒的，但java实现用的hutool和python的gmssl验签过不了，不知道什么原因，看上面那个实现也是一套cipherKey用到死，就不管了，逃)~~
~~3. finish你说什么服务器信什么，你就是刚刚start原地没动下一秒finish说跑了2公里，服务器都信。路径点都不用上传的，我已经用这个方法干好几天了，系统算的是通过，所以就做了一个快速模式，还是不要用为好。~~

#### 加密破解方法(面向开发者)：

0. 这个加密的破解搞的头大，大一下课排满了，晚自习搞破解，要不然上周末应该就出来了(周六工程课进厂打工一天我"爱你"合工大)。
1. 反编译是通过Frida把真正的dex文件hook出来的，使用安卓虚拟机+adb。这里抓到的大多数是系统和依赖的dex，5秒延迟开深度大概率可以得到云运动的dex文件。
2. 使用dex2jar项目把dump出来的dex文件变成jar文件，然后使用jd-gui反编译找的加密代码。jadx的反编译不太行，用dex2jar的。
3. so库使用IDA分析的，IDA不是Pro居然还不给我分析ARM文件，逆天。
4. 2025/12/9: gmssl和hutool过验签要加一个04头，见 10punny 的 [PR](https://github.com/Zirconium233/yunForNewVersion/pull/75)

*免责声明：一切内容只能用于交流学习，24h内自觉卸载，否则后果自负。*

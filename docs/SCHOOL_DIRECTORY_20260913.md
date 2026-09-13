# 学校与业务地址目录快照（2026-09-13）

来源：`POST https://sports.aiyyd.com:9011/api/app/lisshtcool`；请求头
`version: 3.6.6`、`platform: android`、`isApp: app`、`Content-Type: application/json`，空请求体。
本次请求返回 HTTP 200、业务 code=200，共 99 条。未登录、未发送 token，未探测各学校业务端口。
不带 version 的首次请求返回业务 code=500（版本过低），因此浏览器直接打开不等于正确查询。

这是目录返回值，不是逐校可用性证明；可能存在过期/测试/私网记录。
按学校全称查找，保留协议、端口和路径，不能只复制 IP 或按 schoolId 唯一识别学校。
本次目录 schoolId=233 对应两条不同学校记录。域名不得自行改成 DNS 解析 IP，HTTPS 证书和路由可能依赖域名。

关键结果：合肥工业大学为 `http://210.45.246.53:8080/`；多数学校为
`https://sports.aiyyd.com:8000/`。本次目录没有 `http://sports.aiyyd.com:8080/`。
9011 是公共学校目录服务端口，不能直接替换学校业务服务端口。

私网地址（192.168.* / 172.16.*）不能从普通公网直接路由；须由对应学校确认可用入口或网络。
地址查询正确仍连接失败时，再核对官方客户端在同网使用的地址，不能仅凭 ping/TCP 结果归因于宿舍封端口。

| 学校全称 | schoolId | schoolUrl（服务端原值） | 地址类型 |
|---|---|---|---|
| 安徽电气工程职业技术学院 | 159 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 亳州职业技术学院 | 191 | `http://218.22.250.221:8000/` | 公网 IP |
| 周口文理职业学院 | 221 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥幼儿师范高等专科学校 | 136 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥滨湖职业技术学院 | 174 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 南阳科技职业学院 | 207 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 河南医学高等专科学校 | 161 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽中澳科技职业学院 | 193 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 郑州工程技术学院 | 222 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 阜阳师范大学 | 137 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽林业职业技术学院 | 175 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽文达信息工程学院 | 209 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽城市管理职业学院 | 120 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 河南科技学院 | 162 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽外国语学院 | 194 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽电子信息职业技术学院 | 223 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽中医药大学 | 176 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 郑州升达经贸管理学院 | 210 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 焦作大学 | 163 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽邮电职业技术学院 | 195 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 石河子大学 | 226 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽建筑大学 | 140 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽工业经济职业技术学院 | 177 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 辽河石油职业技术学院 | 211 | `http://39.152.115.32:8088/` | 公网 IP |
| 安徽国防科技职业学院 | 164 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽工贸职业技术学院 | 196 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽体育运动职业技术学院 | 228 | `http://60.173.254.108:2444/` | 公网 IP |
| 安徽云知大学 | 141 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥经济技术职业学院 | 178 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 中国农业大学 | 233 | `http://192.168.5.113:8080/` | 私网 IP（需学校确认） |
| 安徽三联学院 | 125 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 黄淮学院 | 165 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 洛阳理工学院 | 197 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 宿州学院（教职工） | 229 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 宿州学院 | 142 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 河南工业和信息化职业学院 | 179 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽广播影视职业技术学院 | 213 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽师范大学 | 127 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 宣城职业技术学院 | 166 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 蚌埠医科大学 | 198 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽汽车职业技术学院 | 230 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 芜湖职业技术大学 | 101 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽工程技术学校 | 145 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 芜湖职业技术学院白马校区 | 180 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 马鞍山师范高等专科学校 | 214 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥经济学院 | 128 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 河南工学院 | 167 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 宿州航空职业学院 | 199 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽省第一轻工业学校 | 231 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥财经职业学院 | 146 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 万博科技职业学院 | 181 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽大学 | 215 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽艺术学院 | 130 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 池州学院 | 168 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 郑州澍青医学高等专科学校 | 200 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 临沂大学 | 232 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 徽商职业学院 | 148 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 阜阳职业技术学院 | 185 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 湖州健康职业学院 | 216 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽工商职业学院 | 131 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥城市学院 | 169 | `http://60.174.215.2:8000/m-api/` | 公网 IP |
| 宿州职业技术学院 | 201 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 池州职业技术学院 | 233 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥通用职业技术学院 | 149 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽国际商务职业学院 | 186 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 皖西经济技术学校 | 217 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥信息技术职业学院 | 132 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 三明学院 | 170 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 河南师范大学 | 202 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽新闻出版职业技术学院 | 156 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 阜阳理工学院 | 188 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 南阳医学高等专科学校 | 218 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽医科大学临床医学院 | 133 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 焦作工贸职业学院 | 171 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 铜陵职业技术学院 | 203 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽交通职业技术学院 | 157 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 皖江工学院 | 189 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 新乡学院 | 219 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 淮南职业技术学院 | 134 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 黄河交通学院 | 172 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 河南科技职业大学 | 205 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽新华学院 | 113 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 蚌埠学院 | 158 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 亳州学院 | 190 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽工业大学 | 220 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽审计职业学院 | 135 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽理工大学 | 173 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽锦瑞科技大学 | 206 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽应用技术职业大学 | 116 | `https://yyd.ahcme.edu.cn/` | 域名（保留域名） |
| 合肥工业大学 | 100 | `http://210.45.246.53:8080/` | 公网 IP |
| 阜阳幼儿师范高等专科学校 | 138 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 合肥大学 | 112 | `http://210.45.88.131:9008/m-api/` | 公网 IP |
| 安徽农业大学 | 110 | `http://192.168.5.48:8080/` | 私网 IP（需学校确认） |
| 淮南师范学院 | 105 | `http://192.168.5.95:8080/` | 私网 IP（需学校确认） |
| 滁州学院 | 106 | `https://tiyu.chzu.edu.cn:8010/m-api/` | 域名（保留域名） |
| 安徽涉外经济职业学院 | 108 | `http://1.13.251.61:8088/` | 公网 IP |
| 辽宁科技学院 | 117 | `http://172.16.10.190:8080/m-api/` | 私网 IP（需学校确认） |
| 合肥职业技术学院 | 122 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |
| 安徽工业职业技术学院 | 123 | `https://sports.aiyyd.com:8000/` | 域名（保留域名） |

# -*- coding: utf-8 -*-
"""A 阶段：最小“请求构造 / 响应解码”边界（work_dir/develop_docs/review_and_revised_plan.md §3-A）。

设计约束（来自评审基线，优先于 work_dir 下原方案）：
- 保留 requests 与既有 gmssl 加密实现；不引入自动重试。
  状态改变类请求超时或非 200 时抛出 HttpStatusException，保留现场由调用方报告“状态未知”。
- 每个请求用 RequestContext 绑定 uuid / utc / sign / SM4 key，并发下不串钥。
  本样本 APK（http/SMUtils.java、RetrofitService.java）显示共享 SM4 对象、每请求重新封装密钥：
  固定 cipherKey 路线保留（Login/schoolList 旧路线），随机封装能力沿用 main.py 既有逻辑。
- 响应解码统一三形态：明文 JSON、SM4 密文、SM4 密文+gzip（crsReocordInfo）。
  HTTP 状态错误、解密错误、业务错误分别以异常/返回值区分，禁止 except 后返回密文假装成功。
- 日志与 dry-run 默认脱敏 token、password、密钥、人脸数据（mask_secret / redact）。
- 时钟、随机源、sleep、transport 均可注入，测试与 dry-run 不产生任何网络副作用。

注意：本模块只证明“本地实现与已知静态证据一致”，不证明服务器接受任何请求。
"""
from __future__ import annotations

import configparser
import gzip
import hashlib
import json
import logging
import random as _random_module
import time
import uuid as _uuid
from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from gmssl import func, sm2
from gmssl.sm4 import SM4_DECRYPT, SM4_ENCRYPT, CryptSM4

logger = logging.getLogger("yun.http")

# (connect, read)。刻意不配 Retry adapter：状态改变请求不自动重放。
DEFAULT_TIMEOUT: Tuple[float, float] = (10, 30)

_DEFAULT_UA = "okhttp/4.9.1"

# 需要脱敏的字段名（小写比较）
SENSITIVE_KEYS = {
    "token", "password", "appsecret", "cipherkey", "cipherkeyencrypted",
    "content", "facebasedata", "map_key", "md5key", "privatekey", "sign",
    "cipherKey", "recordFaceData",
}


# ---------------------------------------------------------------- 异常分类
class YunError(RuntimeError):
    """本模块所有异常的基类。"""


def safe_snippet(text: Any, limit: int = 200) -> str:
    """错误消息里不直接携带服务器响应片段（可能回显 token 等），统一脱敏。

    JSON 体只保留“脱敏后”的结构；非 JSON 体仅报告字节数与校验前缀。
    """
    s = "" if text is None else str(text)
    if not s:
        return "<empty body>"
    t = s.strip()
    if t[:1] in "{[":
        try:
            return json.dumps(redact(json.loads(t)), ensure_ascii=False)[:limit]
        except (json.JSONDecodeError, ValueError):
            pass
    import hashlib
    digest = hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:8]
    return f"<非 JSON 响应 {len(s.encode('utf-8', 'replace'))}B sha256:{digest}>"


class HttpStatusException(YunError):
    """HTTP 层错误（非 200）。对状态改变请求而言这是“结果未知”，不可自动重放。

    评审质量问题：消息不再直接嵌入响应片段；body 仅以脱敏摘要呈现。
    """

    def __init__(self, status: int, url: str, snippet: str = ""):
        super().__init__(f"HTTP {status} @ {url}: {safe_snippet(snippet)}")
        self.status = status
        self.url = url
        self.snippet = safe_snippet(snippet)


class TransportOutcomeUnknown(HttpStatusException):
    """客户端侧传输失败（读超时/连接失败）。

    文案是本脚本自己组织的，不是服务器回显，因此不经过响应脱敏；
    语义仍是“结果未知”，禁止自动重发。
    """

    def __init__(self, url: str, detail: str):
        YunError.__init__(self, f"HTTP -1 @ {url}: {detail}")
        self.status = -1
        self.url = url
        self.snippet = detail


class DecodeException(YunError):
    """响应无法按明文 JSON / SM4 / SM4+gzip 任一形态解码。"""


class BusinessException(YunError):
    """响应已解码为 JSON，但业务 code != 200。"""

    def __init__(self, code: Any, msg: str, payload: Optional[dict] = None):
        super().__init__(f"业务错误 code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.payload = payload


class FaceRequiredError(YunError):
    """识别到人脸核验要求：策略是停止自动流程（无照片源时提示走官方 App）。"""


class RunNotPermittedError(YunError):
    """服务端业务性拒绝开始/继续跑步（如 canSport="N"），与“需要人脸”是不同状态。

    评审 P1：拒绝发生时 recordId 可能已经下发（APK 先保存 id 再判定，
    SportRunMapActivity.java:783-799），必须随异常带出以便处理“已开始但不可继续”。
    """

    def __init__(self, message: str, record_id: Any = None, raw: Any = None):
        super().__init__(message)
        self.record_id = record_id
        self.raw = raw


# ---------------------------------------------------------------- 脱敏
def mask_secret(value: Any, keep_head: int = 4) -> str:
    s = "" if value is None else str(value)
    if not s:
        return "<empty>"
    if len(s) <= keep_head + 2:
        return "***"
    return s[:keep_head] + "***" + s[-2:]


def redact(obj: Any) -> Any:
    """递归复制并掩掉敏感字段，用于日志与 dry-run 输出。"""
    if isinstance(obj, dict):
        return {
            k: (mask_secret(v) if str(k).lower() in SENSITIVE_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


# ---------------------------------------------------------------- 密码原语（沿用原实现）
def _bytes_to_hex(raw: bytes) -> str:
    return hex(int.from_bytes(raw, "big"))[2:].upper()


def encrypt_sm4(value, key: bytes, is_bytes: bool = False) -> str:
    crypt_sm4 = CryptSM4()
    crypt_sm4.set_key(key, SM4_ENCRYPT)
    data = value if is_bytes else value.encode("utf-8")
    return b64encode(crypt_sm4.crypt_ecb(data)).decode()


def decrypt_sm4(value_b64: str, key: bytes) -> bytes:
    crypt_sm4 = CryptSM4()
    crypt_sm4.set_key(key, SM4_DECRYPT)
    return crypt_sm4.crypt_ecb(b64decode(value_b64))


def generate_sm4(rng: Optional[_random_module.Random] = None) -> str:
    """随机 16 字节 SM4 key，返回 base64（沿用 main.py 原逻辑，rng 可注入）。"""
    if rng is None:
        key_hex = func.random_hex(32)
    else:
        key_hex = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    return b64encode(bytes.fromhex(key_hex)).decode("utf-8")


def md5_sign(platform: str, utc: str, uuid_value: str, appsecret: str) -> str:
    """sign = MD5("platform=..&utc=..&uuid=..&appsecret=..")，字段序固定（TreeMap 顺序）。"""
    sb = f"platform={platform}&utc={utc}&uuid={uuid_value}&appsecret={appsecret}"
    return hashlib.md5(sb.encode("utf-8")).hexdigest()


# ---------------- SM2 解密参考实现（仿射点运算；加密仍走 gmssl 生产路径） ----------------
_SM2_P = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF
_SM2_A = _SM2_P - 3


def _ec_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _SM2_P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1 + _SM2_A) * pow(2 * y1, _SM2_P - 2, _SM2_P) % _SM2_P
    else:
        lam = (y2 - y1) * pow(x2 - x1, _SM2_P - 2, _SM2_P) % _SM2_P
    x3 = (lam * lam - x1 - x2) % _SM2_P
    y3 = (lam * (x1 - x3) - y1) % _SM2_P
    return (x3, y3)


def _ec_mul(k: int, pt):
    acc, addend, k = None, pt, k % (
        0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123)
    while k:
        if k & 1:
            acc = _ec_add(acc, addend)
        addend = _ec_add(addend, addend)
        k >>= 1
    return acc


def _sm2_decrypt_b64(text_b64: str, private_key_hex: str) -> bytes:
    """解密 wire 格式 base64(04 || C1 || C3 || C2)（mode=1 布局，与仓库生产加密一致）。"""
    from gmssl.sm3 import sm3_hash, sm3_kdf

    raw = b64decode(text_b64)
    if not raw or raw[0] != 0x04:
        raise DecodeException("SM2 密文缺少 04 未压缩点前缀")
    body = raw[1:].hex()
    if len(body) < 192:
        raise DecodeException("SM2 密文过短")
    c1 = (int(body[0:64], 16), int(body[64:128], 16))
    c3, c2 = body[128:192], body[192:]
    xy_pt = _ec_mul(int(private_key_hex, 16), c1)
    xy = "%064x%064x" % xy_pt
    t = sm3_kdf(xy.encode("utf-8"), (len(c2) + 1) // 2)
    m = "%0*x" % (len(c2), int(c2, 16) ^ int(t, 16))
    u = sm3_hash([i for i in bytes.fromhex("%s%s%s" % (xy[:64], m, xy[64:]))])
    if u != c3:
        raise DecodeException("SM2 C3 校验失败：key/密文不匹配")
    return bytes.fromhex(m)


def _sm2_encrypt_b64(plain: str, public_key_hex: str, k: int) -> str:
    """SM2 加密（mode=1 布局 04||C1||C3||C2），与 gmssl 3.2.2 输出逐位一致（tests/sm2_ref 交叉验证）。"""
    from gmssl.sm3 import sm3_hash, sm3_kdf

    msg = plain.encode("utf-8").hex()
    gx = int("32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7", 16)
    gy = int("BC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0", 16)
    c1 = _ec_mul(k, (gx, gy))
    pub = (int(public_key_hex[:64], 16), int(public_key_hex[64:], 16))
    xy_pt = _ec_mul(k, pub)
    xy = "%064x%064x" % xy_pt
    t = sm3_kdf(xy.encode("utf-8"), (len(msg) + 1) // 2)
    c2 = "%0*x" % (len(msg), int(msg, 16) ^ int(t, 16))
    c3 = sm3_hash([i for i in bytes.fromhex("%s%s%s" % (xy[:64], msg, xy[64:]))])
    return b64encode(bytes.fromhex("04" + "%064x%064x" % c1 + c3 + c2)).decode()


class SM2Box:
    """SM2 信封封装器。

    加密生产路径 = gmssl 成熟实现（评审：密码实现处置）。gmssl 3.2.2 的
    CryptSM2.encrypt 存在已实测的上游缺陷（偶发 _add_point(None) TypeError，
    与密钥配对无关、固定 k 可复现），因此每次尝试都是“gmssl 的一次完整加密”，
    内部崩溃时换新随机数重试，连续失败达到上限才退回本模块仿射实现并记 WARNING。
    退回实现与 gmssl 输出在固定 k 下逐位一致（tests 多样本交叉验证），wire 不变。
    解密：gmssl 3.2.2 自回环输出乱码（上游缺陷），本模块只有离线工具用到解密，
    使用仿射参考实现；线上协议只要求加密方向。
    """

    _GMSSTL_ATTEMPTS = 3

    def __init__(self, public_key: bytes, private_key: Optional[bytes] = None, rng=None):
        self._pub_hex = _bytes_to_hex(public_key[1:])
        self._crypt = sm2.CryptSM2(  # 生产加密走这里（保留旧属性名以兼容外部引用）
            public_key=self._pub_hex,
            private_key=_bytes_to_hex(private_key) if private_key else "",
            mode=1,
            asn1=True,
        )
        self._private_hex = _bytes_to_hex(private_key) if private_key else None
        self._rng = rng
        self.fallback_count = 0  # 观测用：仿射兜底被触发的次数

    def _random_k(self) -> int:
        n = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
        if self._rng is not None:
            return self._rng.randrange(1, n)
        import secrets
        return secrets.randbelow(n - 1) + 1

    def encrypt_b64(self, text: str) -> str:
        # 1) 成熟实现优先：gmssl 每次内部自行取随机 k
        last_exc: Optional[Exception] = None
        for _ in range(self._GMSSTL_ATTEMPTS):
            try:
                # 与仓库原始 encrypt_sm2 同构：gmssl 输出不含 04 未压缩点前缀，
                # 线上 wire = base64(0x04 || C1||C3||C2)（mode=1 布局）。
                ct = self._crypt.encrypt(text.encode("utf-8"))
                return b64encode(b"\x04" + ct).decode()
            except TypeError as exc:  # gmssl 上游缺陷：_add_point(None)
                last_exc = exc
                logger.warning("gmssl SM2 encrypt 内部错误（换新随机数重试）: %s", exc)
        # 2) 兜底：仿射实现（与 gmssl 逐位一致，见模块说明与 tests）
        self.fallback_count += 1
        logger.warning("gmssl SM2 encrypt 连续 %d 次失败，退回仿射参考实现（结果 wire 格式相同）；最后错误: %s",
                       self._GMSSTL_ATTEMPTS, last_exc)
        return _sm2_encrypt_b64(text, self._pub_hex, self._random_k())

    def decrypt_b64(self, text_b64: str) -> bytes:
        if not self._private_hex:
            raise YunError("SM2Box 未持有私钥，无法解密")
        return _sm2_decrypt_b64(text_b64, self._private_hex)


# ---------------------------------------------------------------- 统一响应解码
def decode_response(text: str, sm4_key: bytes) -> str:
    """三形态统一解码：明文 JSON / SM4 / SM4+gzip。失败抛 DecodeException。"""
    t = (text or "").strip()
    if not t:
        raise DecodeException("空响应")
    if t[0] in "{[":  # 明文 JSON（部分学校/部分接口不加密）
        return t
    try:
        raw = decrypt_sm4(t, sm4_key)
    except Exception as exc:  # gmssl 内部异常类型不稳定，统一收敛
        raise DecodeException(f"SM4 解密失败: {exc}") from exc
    if raw[:2] == b"\x1f\x8b":  # gzip（run/crsReocordInfo 响应为 SM4->gzip->JSON）
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise DecodeException(f"gzip 解压失败: {exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecodeException("解密结果不是 UTF-8 JSON 文本") from exc


# ---------------------------------------------------------------- 请求上下文与客户端
@dataclass
class DeviceProfile:
    """静态设备/密钥画像。sys_version 来自统一设备配置，不允许把示例 Android 14 当强制值。"""

    platform: str = "android"
    md5key: str = ""
    token: str = ""
    device_id: str = ""
    device_name: str = ""
    app_edition: str = ""
    sys_version: str = ""
    uuid: str = ""  # 为空 => 每请求随机大写 UUID（RetrofitService.java:68 证据）
    cipherkey: str = ""  # 固定 SM4 key(base64)；为空 => 每请求随机 key（main.py 既有能力）
    cipherkey_encrypted: str = ""  # 登录/学校目录旧路线使用的固定 SM2 密文
    public_key: bytes = b""
    private_key: bytes = b""

    def sm2box(self, need_private: bool = False) -> SM2Box:
        if not self.public_key:
            raise YunError("缺少 SM2 publickey")
        priv = self.private_key if (need_private and self.private_key) else None
        return SM2Box(self.public_key, priv)


@dataclass
class RequestContext:
    """单请求绑定：uuid/utc/sign/SM4 key 一起生成、一起使用，避免并发串钥。"""

    uuid: str
    utc: str
    sign: str
    sm4_key_b64: str

    def headers(self, profile: DeviceProfile) -> Dict[str, str]:
        """线上对齐（3.6.6 抓包审查）：与 APK 头部集合/取值逐一对应。

        - 顺序与集合 = RetrofitService.java:108-110 应用拦截器按固定次序添加的
          11 个自定义头（Content-Type 最先；uuid 在 utc 之前）；
        - Content-Type 精确为 "application/json"（APK 就是该字符串，无 charset 后缀）；
        - User-Agent/Accept-Encoding 是 OkHttp 网络层默认头
          （okhttp3/internal/Util.java:102，userAgent="okhttp/4.9.1"），
          由 _get_session() 统一注入，不混入业务头；
        - Accept 与 Connection 均不发送：App 从不发 Accept（requests 默认头的
          "Accept: */*" 是脚本可识别差异），OkHttp 在 HTTP/1.1 也不发 Connection
          头（requests 默认的 "Connection: keep-alive" 同理），由共享 Session
          清除默认头实现。
        """
        return {
            "Content-Type": "application/json",
            "token": profile.token,
            "isApp": "app",
            "deviceId": profile.device_id,
            "deviceName": profile.device_name,
            "version": profile.app_edition,
            "sysVersion": profile.sys_version,  # 3.6.6 新增头（RetrofitService.java:108）
            "platform": profile.platform,
            "uuid": self.uuid,
            "utc": self.utc,
            "sign": self.sign,
        }


def join_url(base: str, router: str) -> str:
    """URL 边界统一：不重复、不丢失路径分隔；base 以 /api 结尾而 router 以 /api/ 开头时去重。"""
    b = (base or "").rstrip("/")
    r = router if router.startswith("/") else "/" + router
    if b.endswith("/api") and r.startswith("/api/"):
        r = r[len("/api"):]
    return b + r


def profile_from_conf(conf: configparser.ConfigParser) -> DeviceProfile:
    """从已加载的 ConfigParser 构造统一设备画像（主请求/登录/学校目录共用）。

    sys_version 缺省沿用 sys_edition：取值来自统一设备配置，不把示例 Android 14 当强制值。
    """
    from base64 import b64decode as _b64
    sys_edition = conf.get("User", "sys_edition", fallback="")
    return DeviceProfile(
        platform=conf.get("Yun", "platform", fallback="android"),
        md5key=conf.get("Yun", "md5key", fallback=""),
        token=conf.get("User", "token", fallback=""),
        device_id=conf.get("User", "device_id", fallback=""),
        device_name=conf.get("User", "device_name", fallback=""),
        app_edition=conf.get("Yun", "app_edition", fallback=""),
        sys_version=conf.get("User", "sys_version", fallback="") or sys_edition,
        uuid=conf.get("User", "uuid", fallback=""),
        cipherkey=conf.get("Yun", "cipherkey", fallback=""),
        cipherkey_encrypted=conf.get("Yun", "cipherkeyencrypted", fallback=""),
        public_key=_b64(conf.get("Yun", "PublicKey", fallback="")),
        private_key=_b64(conf.get("Yun", "PrivateKey", fallback="")),
    )


class FakeTransport:
    """离线假传输：用已知私钥解出本请求的 SM4 key，再按该 key 加密回包。

    responder(router_path, envelope_dict) -> dict 为响应明文。
    绝不发起网络；所有调用记录在 self.calls（保存原始记录，输出前请 redact）。
    """

    def __init__(self,
                 responder: Callable[[str, dict], dict],
                 sm2box: Optional[SM2Box] = None,
                 fixed_pair: Optional[Tuple[str, str]] = None,
                 fallback_key_b64: Optional[str] = None,
                 require_verified_envelope: bool = False):
        # fixed_pair = (cipherkey_encrypted, cipherkey_b64)：固定信封路线的回包加密依据
        # fallback_key_b64：配置了固定 SM4 key 但信封仍是随机 SM2 封装（仓库默认密钥对公私钥不
        # 匹配、本地无法解出）时的离线兜底；生产上服务端持有配对私钥。
        self._responder = responder
        self._box = sm2box
        self._fixed = {fixed_pair[0]: fixed_pair[1]} if fixed_pair else {}
        self._fallback = fallback_key_b64
        # 评审：FakeTransport 不能只“凭 fallback key 造回包”。开启后，
        # 凡是不能用真实私钥/fixed_pair 解开 cipherKey 的请求直接断言失败。
        self.require_verified_envelope = require_verified_envelope
        self.calls: list = []

    def __call__(self, url: str, data: str, headers: dict, timeout) -> Any:
        envelope = json.loads(data)
        cipher_key = envelope.get("cipherKey", "")
        verified = True  # SM4 key 是否由请求信封本身解出（而非离线兜底假设）
        if cipher_key in self._fixed:
            sm4_key_b64 = self._fixed[cipher_key]
        else:
            try:
                if self._box is None:
                    raise YunError("缺少 sm2box")
                sm4_key_b64 = self._box.decrypt_b64(cipher_key).decode()
            except Exception:
                if self.require_verified_envelope:
                    raise AssertionError("信封不可用持有的私钥验证（require_verified_envelope）")
                if not self._fallback:
                    raise AssertionError("FakeTransport 无法解出 SM4 key：缺少可解私钥/fixed_pair/fallback")
                sm4_key_b64 = self._fallback
                verified = False
        router = urlparse(url).path or url
        # 解开业务体：证明“请求内容”可见并可断言（评审：假回包成功≠信封正确）
        try:
            plain = decode_response(envelope.get("content", ""), b64decode(sm4_key_b64))
            try:
                business = json.loads(plain)
            except (json.JSONDecodeError, ValueError):
                business = {"_raw": plain}
        except DecodeException as exc:
            business = {"_undecodable": str(exc)}
        obj = self._responder(router, envelope)
        body = encrypt_sm4(json.dumps(obj, ensure_ascii=False), b64decode(sm4_key_b64))
        self.calls.append({"router": router, "url": url, "headers": headers,
                           "envelope": envelope, "sm4_key_b64": sm4_key_b64,
                           "envelope_verified": verified, "business": business})
        return _FakeResponse(200, body)


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _real_transport(url: str, data: str, headers: dict, timeout) -> Any:
    return _get_session().post(url=url, data=data, headers=headers, timeout=timeout)


_SESSION: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    """进程共享 Session（可注入替换）：

    - 头部：清除 requests 默认头（Accept: */*、Connection: keep-alive、
      python-requests UA 均为 App 不会出现的可观测差异），只保留 OkHttp 网络层
      等价的 User-Agent/Accept-Encoding；业务头仍按请求由 RequestContext 注入。
    - Cookie：APK 的 OkHttpClient 配了 CookiesManager（RetrofitService.java:137），
      服务端一旦下发 Cookie，真机后续请求都会携带；无 CookieJar 的裸请求是
      可观测差异，因此这里用持久 CookieJar 对齐（Cookie 的具体内容取决于
      服务端 Set-Cookie，无法也不应本地伪造）。
    """
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.clear()
        s.headers.update({"User-Agent": _DEFAULT_UA, "Accept-Encoding": "gzip"})
        _SESSION = s
    return _SESSION


# APK（Java GZIPOutputStream，hutool ZipUtil.gzip 的底层）gzip 容器头实测字节：
# 1f 8b 08 00 | MTIME=0(4B) | XFL=00 OS=ff（桌面 JDK21 实测；Android libcore 与
# OpenJDK 同源 ojluni，未在真机复测——静态推断）。Python gzip.compress 默认写
# 当前 mtime、OS=0x0a，SM4 解密后这 5 个明文字节服务器可直接与真机样本比对。
_GZIP_APK_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"


def gzip_apk(raw: bytes) -> bytes:
    """构造与 APK 头部字节一致的 gzip 容器（白名单接口的 content 前置压缩）。

    deflate 本体仍由 zlib 产生（不同压缩器实现本就不同，解压后不可见）；
    容器头/CRC/长度完全按 RFC 1952 + JDK 取值。解压语义与 gzip.compress 等价。
    """
    import struct
    import zlib
    co = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
    body = co.compress(raw) + co.flush()
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    return _GZIP_APK_HEADER + body + struct.pack("<II", crc, len(raw) & 0xFFFFFFFF)


class YunClient:
    """统一请求构造 + 响应解码的最小客户端。构造函数不发起任何网络。"""

    def __init__(self,
                 profile: DeviceProfile,
                 base_url: str = "",
                 transport: Optional[Callable[..., Any]] = None,
                 rng: Optional[_random_module.Random] = None,
                 now: Optional[Callable[[], int]] = None,
                 mono: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
                 legacy_uuid: bool = False):
        self.profile = profile
        self.base_url = base_url
        self.transport = transport or _real_transport
        self.rng = rng
        self._now = now or (lambda: int(time.time()))
        self.now = self._now  # 公开别名：人脸窗口计时等调用方读取注入时钟
        # Rework R2 dual clock: utc/sign must use epoch seconds (now);
        # window/request budgets must use the monotonic clock (mono).
        self.mono = mono or time.monotonic
        self.sleep = sleep or time.sleep
        self.timeout = timeout
        # 评审 P2：3.6.6 对齐模式默认每请求随机大写 UUID
        # （RetrofitService.java:68）；沿用配置固定 uuid 的旧路线需
        # 显式 legacy_uuid=True，不再是隐式默认。
        self.legacy_uuid = legacy_uuid
        self.last_ctx: Optional[RequestContext] = None
        self.sent: list = []  # 每次请求的 (router/url, ctx)；输出前请 redact headers

    # ---- 上下文：每请求新 uuid(未固定时)/utc/sign/key 三者一致绑定 ----
    def new_context(self, fixed_envelope: bool = False) -> RequestContext:
        p = self.profile
        rid = p.uuid if (self.legacy_uuid and p.uuid) else str(_uuid.uuid4()).upper()
        utc = str(self._now())
        sign = md5_sign(p.platform, utc, rid, p.md5key)
        if fixed_envelope:
            if not p.cipherkey:
                raise YunError("固定信封要求配置 cipherkey（登录/学校目录旧路线）")
            key = p.cipherkey  # 登录/学校目录：SM4 必须用固定 key（cipherKey 是它的密文）
        else:
            key = p.cipherkey or generate_sm4(self.rng)  # 主接口：保留随机 key 能力
        return RequestContext(uuid=rid, utc=utc, sign=sign, sm4_key_b64=key)

    def build_envelope(self, ctx: RequestContext, content_b64: str,
                       fixed_envelope: bool = False) -> dict:
        p = self.profile
        if fixed_envelope:
            # 固定信封只属于登录/学校目录旧路线；主接口默认随机封装（沿用原行为）
            if not p.cipherkey_encrypted:
                raise YunError("profile 未配置 cipherkey_encrypted，无法使用固定信封")
            cipher_key = p.cipherkey_encrypted
        else:
            cipher_key = p.sm2box().encrypt_b64(ctx.sm4_key_b64)
        return {"cipherKey": cipher_key, "content": content_b64}

    # ---- POST：默认返回解码后的文本；post_json 返回 dict ----
    def post(self, router: str, json_text: str = "", raw_bytes: Optional[bytes] = None,
             absolute_url: Optional[str] = None, fixed_envelope: bool = False) -> str:
        ctx = self.new_context(fixed_envelope=fixed_envelope)
        key_bytes = b64decode(ctx.sm4_key_b64)
        if raw_bytes is not None:
            content = encrypt_sm4(raw_bytes, key_bytes, is_bytes=True)
        else:
            content = encrypt_sm4(json_text, key_bytes)
        envelope = self.build_envelope(ctx, content, fixed_envelope=fixed_envelope)
        url = absolute_url or join_url(self.base_url, router)
        headers = ctx.headers(self.profile)
        try:
            resp = self.transport(url=url, data=json.dumps(envelope),
                                  headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            # 只报告，不重试：读超时可能发生在服务器已提交之后。
            raise TransportOutcomeUnknown(url, f"传输失败(结果未知，勿自动重发): {exc}") from exc
        self.last_ctx = ctx
        self.sent.append({"router": router, "url": url, "uuid": ctx.uuid, "utc": ctx.utc})
        if resp.status_code != 200:
            raise HttpStatusException(resp.status_code, url, getattr(resp, "text", ""))
        return decode_response(resp.text, key_bytes)

    def post_json(self, router: str, json_text: str = "", raw_bytes: Optional[bytes] = None,
                  absolute_url: Optional[str] = None, fixed_envelope: bool = False,
                  raise_on_business_code: bool = False) -> dict:
        text = self.post(router, json_text=json_text, raw_bytes=raw_bytes,
                         absolute_url=absolute_url, fixed_envelope=fixed_envelope)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DecodeException(f"响应不是 JSON: {safe_snippet(text)}") from exc
        if raise_on_business_code and obj.get("code") != 200:
            raise BusinessException(obj.get("code"), obj.get("msg", ""), obj)
        return obj

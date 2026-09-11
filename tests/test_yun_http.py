# -*- coding: utf-8 -*-
"""yun_http 单元测试：只证明本地实现与静态证据/标准向量一致，不触碰网络。"""
import base64
import gzip
import json
import random

import pytest

import yun_http as yh

# 本地测试专用 SM2 密钥对（公私钥匹配，仅用于离线回环；不代表线上密钥）
PUB = base64.b64decode(
    "BEWZDlEKyx8HIeGxA6Sow/Eo7E6ob7wGiDA37Qljlk18jebAF5RpOZygu+dV9ibuWOXXsxwqtLEWZdPR0ClntxU==")
PRIV = base64.b64decode("sOJdr4SC3D+uHV1MCBdVCyb19TuHX6t/otKnXQr+uUE=")
SM4_KEY_B64 = "JXhWGZjmhhXN+nt8nLpNxA=="
CIPHERKEY_ENCRYPTED = (
    "BGfbsG9EkXz5KeCva8E0MisBeS6bhBEDId3VXeIuBoiBMZU0Mosv7PqKsvqxZ3PjkUlsjzh09Se629SWW45XP4"
    "TIUeXoLpYzgk5fAMbg0VNVnXuLH9xVzdHAeM+1qJrgvwwkwio85/DnrP1aArvVQrw3N4xd5tugqQ==")


def _profile(**over):
    p = yh.DeviceProfile(platform="android", md5key="TEST_MD5_KEY_LOCAL_ONLY",
                         token="TESTTOKEN0123456789abcdef", device_id="1234567890123456",
                         device_name="TestDevice", app_edition="3.6.6", sys_version="13",
                         uuid="", cipherkey="", cipherkey_encrypted="",
                         public_key=PUB, private_key=PRIV)
    for k, v in over.items():
        setattr(p, k, v)
    return p


# ---------------------------------------------------------------- 密码原语
def test_sm4_golden_vector_gb_t_32907():
    """GB/T 32907-2016 示例 1：单块 + PKCS7 整块填充，确定性密文，可作黄金向量。"""
    key = bytes.fromhex("0123456789abcdeffedcba9876543210")
    pt = bytes.fromhex("0123456789abcdeffedcba9876543210")
    ct = base64.b64decode(yh.encrypt_sm4(pt, key, is_bytes=True))
    assert ct.hex() == "681edf34d206965e86b3e94f536e4246" "002a8a4efa863ccad024ac0300bb40d2"
    assert yh.decrypt_sm4(base64.b64encode(ct).decode(), key) == pt


def test_sm4_roundtrip_text():
    key = base64.b64decode(SM4_KEY_B64)
    enc = yh.encrypt_sm4('{"code":200}', key)
    assert yh.decrypt_sm4(enc, key).decode() == '{"code":200}'


def test_md5_sign_golden_pins_field_order():
    """固定输入 -> 固定输出；黄金向量用途是钉住 TreeMap 字段顺序与拼接格式。"""
    got = yh.md5_sign("android", "1757500000",
                      "0F9E8D7C-6B5A-4321-FEDC-BA9876543210",
                      "pie0hDSfMRINRXc7s1UIXfkE")
    assert got == "52a52d749b3b2fac47ffa376f4d7e7ed"


def test_sm2_roundtrip_and_randomness():
    """SM2 加密随机化：只允许回环断言，不允许把密文快照写死。"""
    box = yh.SM2Box(PUB, PRIV)
    c1 = box.encrypt_b64(SM4_KEY_B64)
    c2 = box.encrypt_b64(SM4_KEY_B64)
    assert c1 != c2
    assert box.decrypt_b64(c1).decode() == SM4_KEY_B64
    assert box.decrypt_b64(c2).decode() == SM4_KEY_B64


def test_sm2_wire_matches_gmssl_for_fixed_k(monkeypatch):
    """固定随机数 k 下，本实现与 gmssl 3.2.2.encrypt 输出逐位一致（wire 格式回归钉）。"""
    import gmssl.sm2 as gm_sm2
    import tests.sm2_ref as ref  # noqa: F401  参考实现独立存在，便于对照

    def h(b):
        return hex(int.from_bytes(b, "big"))[2:].upper()

    k_hex = "0" * 56 + "deadbeef"
    monkeypatch.setattr(gm_sm2.func, "random_hex", lambda n: "0" * (n - 8) + "deadbeef")
    c = gm_sm2.CryptSM2(public_key=h(PUB[1:]), private_key="", mode=1, asn1=True)
    msg = "KEY0123456789abcdefghij=="
    e = c.encrypt(msg.encode("utf-8"))
    gmssl_wire = base64.b64encode(bytes.fromhex("04" + e.hex().upper())).decode()
    mine = yh._sm2_encrypt_b64(msg, h(PUB[1:]), int(k_hex, 16))
    assert mine == gmssl_wire
    # 且独立参考实现也能解回
    assert ref.decrypt_b64(mine, h(PRIV)).decode() == msg


# ---------------------------------------------------------------- 响应解码
def test_decode_response_three_forms():
    key = base64.b64decode(SM4_KEY_B64)
    payload = '{"code":200,"data":{}}'
    assert yh.decode_response(payload, key) == payload  # 明文 JSON
    assert yh.decode_response(yh.encrypt_sm4(payload, key), key) == payload  # SM4
    gz = yh.encrypt_sm4(gzip.compress(payload.encode()), key, is_bytes=True)
    assert yh.decode_response(gz, key) == payload  # SM4 -> gzip -> JSON


def test_decode_response_errors_are_typed():
    key = base64.b64decode(SM4_KEY_B64)
    with pytest.raises(yh.DecodeException):
        yh.decode_response("!!!not-base64!!!", key)
    with pytest.raises(yh.DecodeException):
        yh.decode_response("", key)
    # 合法 base64 但不是 16 字节倍数，SM4 分组解密应失败并被收敛为 DecodeException
    with pytest.raises(yh.DecodeException):
        yh.decode_response(base64.b64encode(b"\x01\x02\x03").decode(), key)


# ---------------------------------------------------------------- URL / 脱敏
@pytest.mark.parametrize("base,router,want", [
    ("http://h:8080/", "/run/x", "http://h:8080/run/x"),
    ("http://h:8080", "run/x", "http://h:8080/run/x"),
    ("https://a/api", "/api/app/schoolList", "https://a/api/app/schoolList"),
    ("https://a", "/login/appLoginHGD", "https://a/login/appLoginHGD"),
    ("http://h:8080/", "login/appLoginHGD", "http://h:8080/login/appLoginHGD"),
])
def test_join_url(base, router, want):
    assert yh.join_url(base, router) == want


def test_mask_and_redact():
    assert yh.mask_secret("ABCDEFGHIJKLMNOP") == "ABCD***OP"
    assert yh.mask_secret("short") == "***"
    assert yh.mask_secret("") == "<empty>"
    red = yh.redact({"token": "ABCDEFGHIJKLMNOP",
                     "content": "XYZXYZXYZXYZXYZXYZ",
                     "faceBaseData": "AAAAAAAABBBBBBBB",
                     "inner": {"md5key": "KEYKEYKEYKEYKEY"},
                     "visible": "hello"})
    assert red["token"] == "ABCD***OP"
    assert "*" in red["content"] and "*" in red["faceBaseData"]
    assert "*" in red["inner"]["md5key"]
    assert red["visible"] == "hello"


# ---------------------------------------------------------------- 请求上下文绑定
def _fake(responder=None, fixed_pair=None):
    return yh.FakeTransport(responder or (lambda router, env: {"code": 200, "msg": "ok", "data": None}),
                            sm2box=yh.SM2Box(PUB, PRIV), fixed_pair=fixed_pair)


def test_random_key_profile_binds_per_request():
    fake = _fake()
    client = yh.YunClient(_profile(), base_url="http://school.invalid:8080",
                          transport=fake, rng=random.Random(7), now=lambda: 1757500000)
    client.post("/run/a", "{}")
    client.post("/run/b", "{}")
    c1, c2 = fake.calls
    # 每请求 uuid 随机大写（RetrofitService.java:68 形态）
    assert c1["headers"]["uuid"] != c2["headers"]["uuid"]
    assert c1["headers"]["uuid"] == c1["headers"]["uuid"].upper()
    # sign 与同一请求头里的 uuid/utc 绑定
    for c in (c1, c2):
        h = c["headers"]
        assert h["sign"] == yh.md5_sign("android", h["utc"], h["uuid"],
                                        "TEST_MD5_KEY_LOCAL_ONLY")
        assert h["sysVersion"] == "13"  # sysVersion 头存在且来自设备配置
    # 每请求重新封装密钥：随机 key 路线两次 key 不同，且各自能解开自己的回包
    assert c1["sm4_key_b64"] != c2["sm4_key_b64"]
    assert {u["uuid"] for u in client.sent} == {c1["headers"]["uuid"], c2["headers"]["uuid"]}


def test_fixed_cipherkey_profile_shares_key_like_apk():
    """本样本 APK 是共享 SM4 对象、每请求重新封装：固定 key 路线 key 恒定。"""
    profile = _profile(cipherkey=SM4_KEY_B64, uuid="FIXED-UUID-1")
    fake = _fake()
    client = yh.YunClient(profile, base_url="http://school.invalid:8080", transport=fake)
    client.post("/run/a", "{}")
    client.post("/run/b", "{}")
    c1, c2 = fake.calls
    assert c1["sm4_key_b64"] == c2["sm4_key_b64"] == SM4_KEY_B64
    assert c1["headers"]["uuid"] == "FIXED-UUID-1"


def test_fixed_envelope_uses_precomputed_cipherkey():
    fake = _fake(fixed_pair=(CIPHERKEY_ENCRYPTED, SM4_KEY_B64))
    profile = _profile(cipherkey=SM4_KEY_B64, cipherkey_encrypted=CIPHERKEY_ENCRYPTED)
    client = yh.YunClient(profile, base_url="http://school.invalid:8080", transport=fake)
    obj = client.post_json("/login/appLoginHGD", '{"a":1}', fixed_envelope=True)
    assert obj["code"] == 200
    assert fake.calls[0]["envelope"]["cipherKey"] == CIPHERKEY_ENCRYPTED


# ---------------------------------------------------------------- 错误分类 / 不重试
def test_http_status_error_is_classified_and_not_retried():
    calls = []

    def transport(url, data, headers, timeout):
        calls.append(url)

        class R:
            status_code, text = 500, "server error"
        return R()

    client = yh.YunClient(_profile(), base_url="http://school.invalid:8080", transport=transport)
    with pytest.raises(yh.HttpStatusException) as ei:
        client.post("/run/start", "{}")
    assert ei.value.status == 500
    assert len(calls) == 1  # 不自动重试


def test_transport_exception_reports_unknown_state():
    import requests

    def transport(url, data, headers, timeout):
        raise requests.ReadTimeout("timeout")

    client = yh.YunClient(_profile(), base_url="http://school.invalid:8080", transport=transport)
    with pytest.raises(yh.HttpStatusException) as ei:
        client.post("/run/finish", "{}")
    assert "结果未知" in str(ei.value)


def test_business_error_is_separate_from_decode_error():
    fake = _fake(responder=lambda router, env: {"code": 500, "msg": "距离不足"})
    client = yh.YunClient(_profile(), base_url="http://school.invalid:8080", transport=fake)
    obj = client.post_json("/run/splitPointCheating", "{}")  # 默认不抛，交调用方处理
    assert obj["code"] == 500
    client2 = yh.YunClient(_profile(), base_url="http://school.invalid:8080",
                           transport=_fake(responder=lambda router, env: {"code": 500, "msg": "x"}))
    with pytest.raises(yh.BusinessException):
        client2.post_json("/run/start", "{}", raise_on_business_code=True)

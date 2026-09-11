# -*- coding: utf-8 -*-
"""独立 SM2 C1C3C2 参考实现（仅供测试/FakeTransport 使用）。

背景：gmssl 3.2.2 的 CryptSM2.encrypt 是仓库生产路径（服务端实测接受其输出），
但其 CryptSM2.decrypt 在本环境下自回环失败（已知上游缺陷）。为了让离线测试能
“解出客户端每次请求封装的 SM4 key”，这里按 GB/T 32918.5 用最小仿射点运算重写解密，
并用 gmssl 的 sm3_kdf/sm3_hash 保持与生产加密同一套 KDF/哈希。
"""
import base64

from gmssl.sm3 import sm3_hash, sm3_kdf

P = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF00000000FFFFFFFFFFFFFFFF
A = P - 3
B = 0x28E9FA9E9D9F5E344D5A9E4BCF6509A7F39789F515AB8F92DDBCBD414D940E93
N = 0xFFFFFFFEFFFFFFFFFFFFFFFFFFFFFFFF7203DF6B21C6052B53BBF40939D54123
GX = 0x32C4AE2C1F1981195F9904466A39C9948FE30BBFF2660BE1715A4589334C74C7
GY = 0xBC3736A2F4F6779C59BDCEE36B692153D0A9877CC62A474002DF32E52139F0A0
INF = None


def _add(p1, p2):
    if p1 is INF:
        return p2
    if p2 is INF:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return INF
    if p1 == p2:
        lam = (3 * x1 * x1 + A) * pow(2 * y1, P - 2, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, P - 2, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def _mul(k, pt):
    acc, addend = INF, pt
    k %= N
    while k:
        if k & 1:
            acc = _add(acc, addend)
        addend = _add(addend, addend)
        k >>= 1
    return acc


def _xy(pt):
    return "%064x%064x" % (pt[0], pt[1])


def decrypt_b64(cipher_b64: str, private_key_hex: str) -> bytes:
    """解密 wire 格式：base64(04 || C1 || C3 || C2)（mode=1 布局，仓库生产格式）。"""
    raw = base64.b64decode(cipher_b64)
    assert raw[0] == 0x04, "缺少 04 未压缩点前缀"
    body = raw[1:].hex()
    c1x, c1y = int(body[0:64], 16), int(body[64:128], 16)
    c3 = body[128:192]
    c2 = body[192:]
    xy = _xy(_mul(int(private_key_hex, 16), (c1x, c1y)))
    t = sm3_kdf(xy.encode("utf-8"), (len(c2) + 1) // 2)
    m = "%0*x" % (len(c2), int(c2, 16) ^ int(t, 16))
    plain = bytes.fromhex(m)
    # C3 校验：hash(x2 || M || y2)
    u = sm3_hash([i for i in bytes.fromhex("%s%s%s" % (xy[:64], m, xy[64:]))])
    if u != c3:
        raise ValueError("SM2 C3 校验失败：key/密文不匹配")
    return plain


def encrypt_b64(plain: str, public_key_hex: str, k_hex: str) -> str:
    """测试用 SM2 加密（与 gmssl 生产路径同布局），k 显式传入保证确定性可选。"""
    msg = plain.encode("utf-8").hex()
    k = int(k_hex, 16)
    pub = (int(public_key_hex[:64], 16), int(public_key_hex[64:], 16))
    c1 = _mul(k, (GX, GY))
    xy = _mul(k, pub)
    t = sm3_kdf(xy.encode("utf-8"), (len(msg) + 1) // 2)
    c2 = "%0*x" % (len(msg), int(msg, 16) ^ int(t, 16))
    c3 = sm3_hash([i for i in bytes.fromhex("%s%s%s" % (xy[:64], msg, xy[64:]))])
    return base64.b64encode(bytes.fromhex("04" + _xy(c1) + c3 + c2)).decode()

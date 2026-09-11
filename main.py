# -*- coding: utf-8 -*-
"""云运动脚本主入口（A 阶段改造，见 docs/review_and_revised_plan.md §3-A）。

兼容承诺（旧调用方）：
- CLI 参数保持 --config_path/-f、--task_path/-t、--auto_run/-a、--drift/-d；
  新参数（--dry-run 等）只作为增量别名。
- set_args/default_post/getsign/encrypt_sm4/decrypt_sm4/encrypt_sm2/decrypt_sm2/
  generate_sm4 等旧函数签名保留，内部统一委托给 yun_http。
- 模块级全局变量保留为 set_args 时的“快照”，不承诺 from main import * 后
  跨模块动态同步（评审 2.1.4）；仓库内 history.py 已改为显式客户端注入。

路径契约（评审 2.1.6）：显式传入的相对 CLI 路径按调用者工作目录解析；
未显式传入的缺省资源按项目根目录解析。os.chdir 副作用保留以兼容旧习惯。
"""
import os

# 先记录调用者工作目录，再做传统 chdir。
INITIAL_CWD = os.getcwd()
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_ROOT)  # 有些人不喜欢cd到这个目录，帮他cd一下（保留：部分旧脚本依赖）

import base64
import math
import random
import time
import requests
import json
import configparser
import hashlib
from typing import List, Dict
from gmssl.sm4 import CryptSM4, SM4_ENCRYPT, SM4_DECRYPT
import sys
import gmssl.sm2 as sm2
from base64 import b64encode, b64decode
import traceback
import gzip
from tqdm import tqdm
import argparse
from tools.drift import add_drift
from gmssl import sm4, func
from Crypto.Util.Padding import pad, unpad
from tools.Login import Login

import yun_http
import yun_face
from yun_face import FaceRunStopError  # noqa: F401  (main 的 except 分支使用)
from yun_http import (
    BusinessException,
    RunNotPermittedError,
    DecodeException,
    DeviceProfile,
    FakeTransport,
    FaceRequiredError,
    HttpStatusException,
    RequestContext,
    SM2Box,
    YunClient,
    decode_response,
    join_url,
    mask_secret,
    md5_sign,
    redact,
)

"""
加密模式：sm2非对称加密sm4密钥
"""
# 偏移量
# default_iv = '\1\2\3\4\5\6\7\x08' 失效

# PublicKey = 'BL7JvEAV7Wci0h5YAysN0BPNVdcUhuyJszJLRwnurav0CGftcrVcvrWeCPBIjIIBF371teRbrCS9V1Wyq7i3Arc=' # 旧公钥
PublicKey = 'BDdKFsuBf51UObke1pEgfER17biBg/5r8slqE4s8oOa8lVesWgIUxsRc+AmZ72GcuJ56f7avnyJe3CJY4n00LU4='  # 3.4.7
PrivateKey = 'P3s0+rMuY4Nt5cUWuOCjMhDzVNdom+W0RvdV6ngM+/E='  # 3.4.7 私钥失效（是否被服务端接受未经线上验证）
PUBLIC_KEY = b64decode(PublicKey)
PRIVATE_KEY = b64decode(PrivateKey)

my_host = None
default_key = None
CipherKeyEncrypted = None
my_app_edition = None
my_token = None
my_device_id = None
my_key = None
my_device_name = None
my_sys_edition = None
my_sys_version = None  # A 阶段新增：统一设备版本（sysVersion 头/sysEdition 体）
my_utc = None
my_uuid = None
my_sign = None
min_distance = None
allow_overflow_distance = None
single_mileage_min_offset = None
single_mileage_max_offset = None
cadence_min_offset = None
cadence_max_offset = None
split_count = None
exclude_points = None
min_consume = None
max_consume = None
strides = None
md5key = None
platform = None

_CONF = configparser.ConfigParser()  # set_args 加载后的配置快照
_CLIENT: YunClient | None = None     # set_args 构建的统一客户端（构造不发网络）
_SM2_BOX: SM2Box | None = None


def resolve_cli_path(path: str) -> str:
    """显式 CLI 相对路径 => 调用者工作目录；绝对路径原样。"""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(INITIAL_CWD, path))


def project_resource(rel: str) -> str:
    """缺省资源 => 项目根目录。"""
    return os.path.normpath(os.path.join(PROJECT_ROOT, rel))


def set_args(conf_path: str):
    """加载配置：填充旧全局快照 + 构建 DeviceProfile/YunClient。不发起网络。"""
    global my_host, default_key, CipherKeyEncrypted, my_app_edition, my_token, my_device_id
    global my_key, my_device_name, my_sys_edition, my_sys_version, my_utc, my_uuid, my_sign
    global min_distance, allow_overflow_distance, single_mileage_min_offset, single_mileage_max_offset
    global cadence_min_offset, cadence_max_offset, split_count, exclude_points, min_consume, max_consume
    global strides, PUBLIC_KEY, PRIVATE_KEY, md5key, platform
    global _CONF, _CLIENT, _SM2_BOX, sm2_crypt

    resolved = resolve_cli_path(conf_path)
    if not os.path.exists(resolved):
        raise FileNotFoundError(f"配置文件不存在: {resolved}")

    conf = configparser.ConfigParser()
    conf.read(resolved, encoding="utf-8")
    _CONF = conf

    # 学校、keys和版本信息
    my_host = conf.get("Yun", "school_host")  # 学校的host
    default_key = conf.get("Yun", "cipherkey")  # 加密密钥
    CipherKeyEncrypted = conf.get("Yun", "cipherkeyencrypted")  # 加密密钥的sm2加密版本
    my_app_edition = conf.get("Yun", "app_edition")  # app版本

    # 用户信息，包括设备信息
    my_token = conf.get("User", 'token')
    my_device_id = conf.get("User", "device_id")
    # 评审 P1：device_name 必须真正加载（旧版声明了全局却没有赋值，入口打印 TypeError）
    my_device_name = conf.get("User", "device_name", fallback="")
    my_key = conf.get("User", "map_key")  # 高德地图开发者密钥
    my_sys_edition = conf.get("User", "sys_edition")  # 安卓大版本（body sysEdition 旧来源）
    # sysVersion 头来自 3.6.6 证据；取值统一走设备配置，缺省沿用 sys_edition，不强制示例值
    my_sys_version = conf.get("User", "sys_version", fallback="") or my_sys_edition
    my_utc = conf.get('User', 'utc') or str(int(time.time()))
    my_uuid = conf.get("User", "uuid")
    my_sign = conf.get("User", "sign")

    # 跑步相关的信息
    min_distance = float(conf.get("Run", "min_distance"))
    allow_overflow_distance = float(conf.get("Run", "allow_overflow_distance"))
    single_mileage_min_offset = float(conf.get("Run", "single_mileage_min_offset"))
    single_mileage_max_offset = float(conf.get("Run", "single_mileage_max_offset"))
    cadence_min_offset = int(conf.get("Run", "cadence_min_offset"))
    cadence_max_offset = int(conf.get("Run", "cadence_max_offset"))
    split_count = int(conf.get("Run", "split_count"))
    exclude_points = json.loads(conf.get("Run", "exclude_points"))
    min_consume = float(conf.get("Run", "min_consume"))
    max_consume = float(conf.get("Run", "max_consume"))
    strides = float(conf.get("Run", "strides"))

    PUBLIC_KEY = b64decode(conf.get("Yun", "PublicKey"))
    PRIVATE_KEY = b64decode(conf.get("Yun", "PrivateKey"))
    _SM2_BOX = SM2Box(PUBLIC_KEY, PRIVATE_KEY)
    sm2_crypt = _SM2_BOX._crypt  # 兼容旧全局名（快照），新代码请勿依赖

    md5key = conf.get("Yun", "md5key")
    platform = conf.get("Yun", "platform")

    # 评审 P2：默认按 3.6.6 行为“每请求随机 UUID”；旧协议兼容开关显式配置
    legacy_uuid = str(conf.get("User", "legacy_uuid", fallback="")).strip().lower() in ("1", "true", "yes", "y")
    _CLIENT = YunClient(build_profile(conf), base_url=my_host, legacy_uuid=legacy_uuid)

    return {
        "my_token": my_token,
        "my_device_id": my_device_id,
        "my_device_name": my_device_name,
        "my_utc": my_utc,
        "my_uuid": my_uuid,
        "my_sign": my_sign,
        "my_key": my_key,
        "my_sys_version": my_sys_version,
    }


def build_profile(conf: configparser.ConfigParser) -> DeviceProfile:
    """从已加载的 ConfigParser 构造统一设备画像（统一实现位于 yun_http，此处保留旧名）。"""
    return yun_http.profile_from_conf(conf)


def default_client() -> YunClient:
    """全局客户端；外部只改了模块全局没走 set_args 时按当前全局兜底构造。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = YunClient(
            DeviceProfile(
                platform=platform or "android", md5key=md5key or "",
                token=my_token or "", device_id=my_device_id or "",
                device_name=my_device_name or "", app_edition=my_app_edition or "",
                sys_version=my_sys_version or my_sys_edition or "", uuid=my_uuid or "",
                cipherkey=default_key or "", cipherkey_encrypted=CipherKeyEncrypted or "",
                public_key=PUBLIC_KEY, private_key=PRIVATE_KEY,
            ),
            base_url=my_host or "",
        )
    return _CLIENT


def parse_args():
    parser = argparse.ArgumentParser(description='云运动自动跑步脚本')
    # 旧参数名一字不改；缺省 None 以便按“缺省资源=项目根”契约解析
    parser.add_argument('-f', '--config_path', type=str, default=None,
                        help='配置文件路径（相对路径按调用者工作目录解析；缺省为项目根 config.ini）')
    parser.add_argument('-t', '--task_path', type=str, default=None,
                        help='任务文件路径（相对路径按调用者工作目录解析；缺省为项目根 tasks_fch）')
    parser.add_argument('-a', '--auto_run', action='store_true', help='自动跑步，默认打表')
    parser.add_argument('-d', '--drift', action='store_true', help='是否添加漂移')
    parser.add_argument('--dry-run', '--dry_run', dest='dry_run', action='store_true',
                        help='离线演练：本地 fixture + 假传输，不登录/不探测学校/不调高德/不 sleep/不写配置')
    parser.add_argument('--dry-home', dest='dry_home', type=str, default=None,
                        help='dry-run 用 getHomeRunInfo 响应 fixture（JSON 文件路径）')
    # 人脸输入适配（docs/REVIEW_FACE_PLAN.md §三 方案1/2）：仅提供源时 Y 任务才启用离线管线
    parser.add_argument('--face-photo', dest='face_photo', type=str, default=None,
                        help='人脸输入：本人照片路径（人像照，非证件扫描）')
    parser.add_argument('--face-video', dest='face_video', type=str, default=None,
                        help='人脸输入：自拍视频路径，确定性抽帧（需可选依赖 opencv-python）')
    parser.add_argument('--face-detection', dest='face_detection', type=str, default=None,
                        help='人工标注检测结果 JSON（box+5 点+score）；缺省时取景质量门无法执行并按失败处理')
    parser.add_argument('--face-mirror', dest='face_mirror', action='store_true',
                        help='声明输入已是镜像翻转（照片源默认不镜像，无前置相机标志）')
    return parser.parse_args()


def string_to_hex(input_string):
    # 将字符串转换为十六进制表示，然后去除前缀和分隔符
    hex_string = hex(int.from_bytes(input_string.encode(), 'big'))[2:].upper()
    return hex_string


def bytes_to_hex(input_string):
    # 将字符串转换为十六进制表示，然后去除前缀和分隔符
    hex_string = hex(int.from_bytes(input_string, 'big'))[2:].upper()
    return hex_string


sm2_crypt = sm2.CryptSM2(public_key=bytes_to_hex(PUBLIC_KEY[1:]), private_key=bytes_to_hex(PRIVATE_KEY), mode=1, asn1=True)


# ---------------- 加密/签名旧接口：统一委托 yun_http，签名保持不变 ----------------
def encrypt_sm4(value, SM_KEY, isBytes=False):
    return yun_http.encrypt_sm4(value, SM_KEY, is_bytes=isBytes)


def decrypt_sm4(value, SM_KEY):
    return yun_http.decrypt_sm4(value, SM_KEY)


def encrypt_sm2(info):
    box = _SM2_BOX or SM2Box(PUBLIC_KEY)
    return box.encrypt_b64(info)


def decrypt_sm2(info):
    box = _SM2_BOX or SM2Box(PUBLIC_KEY, PRIVATE_KEY)
    return box.decrypt_b64(info)


def generate_sm4():
    return yun_http.generate_sm4()


def getsign(utc, uuid):
    return md5_sign(platform, utc, uuid, md5key)


def default_post(router, data, headers=None, m_host=None, isBytes=False, gen_sign=True, key_ctx=None):
    """旧接口薄包装：返回解码后的响应文本。

    与旧行为的差异（评审要求）：HTTP/解码错误抛异常而不是 except 后返回密文；
    每请求上下文（uuid/utc/sign/SM4 key）在 yun_http.RequestContext 内绑定。
    """
    client = default_client()
    if headers is not None and gen_sign:
        # 评审：headers 参数在常规签名路径会被忽略——显式报错，不再静默丢弃
        raise ValueError("default_post(headers=...) 仅在 gen_sign=False 兼容路径生效；"
                         "gen_sign=True 时头部由 RequestContext 统一构造")
    if not gen_sign:
        # 旧路线：使用配置文件里预生成的 utc/sign（登录前探测等场景）
        ctx = RequestContext(uuid=my_uuid or "", utc=my_utc, sign=my_sign,
                             sm4_key_b64=default_key or yun_http.generate_sm4())
        key_bytes = base64.b64decode(ctx.sm4_key_b64)
        content = (yun_http.encrypt_sm4(data, key_bytes, is_bytes=True) if isBytes
                   else yun_http.encrypt_sm4(data, key_bytes))
        envelope = {"cipherKey": CipherKeyEncrypted or client.build_envelope(ctx, "")["cipherKey"],
                    "content": content}
        url = join_url(m_host or my_host, router)
        hd = headers or ctx.headers(client.profile)
        resp = client.transport(url=url, data=json.dumps(envelope), headers=hd,
                                timeout=client.timeout)
        if isinstance(key_ctx, dict):
            key_ctx["key"] = ctx.sm4_key_b64
        if resp.status_code != 200:
            raise HttpStatusException(resp.status_code, url, getattr(resp, "text", ""))
        return decode_response(resp.text, key_bytes)
    text = client.post(router, json_text=data if not isBytes else "",
                       raw_bytes=data if isBytes else None,
                       absolute_url=join_url(m_host, router) if m_host else None)
    if isinstance(key_ctx, dict) and client.last_ctx is not None:
        key_ctx["key"] = client.last_ctx.sm4_key_b64
    return text


def noTokenLogin(conf_path: str = None):
    conf_path = conf_path or project_resource("config.ini")
    print("config中token为空，是否尝试使用账号密码登录？(y/n)")
    LoginChoice = input()
    if LoginChoice == 'y':
        login_result = Login.main(conf_path)
        if login_result is None:
            print("登录失败，请再试一次")
            return None

        token, DeviceId, DeviceName, uuid, sys_edition = login_result
        print("是否保存本次登录产生的token和uuid？(y/n)")
        TokenWrite = input()
        if TokenWrite == 'y':
            # 评审 2.1.5：写回必须落到本次实际使用的配置路径，不再硬编码 ./config.ini
            config = configparser.ConfigParser()
            config.read(conf_path, encoding='utf-8')
            config.set('User', 'token', token)
            config.set('User', 'uuid', uuid)
            config.set('User', 'device_id', DeviceId)
            config.set('User', 'device_name', DeviceName)
            config.set('User', 'sys_edition', sys_edition)
            with open(conf_path, 'w+', encoding='utf-8') as f:
                config.write(f)
        return token, DeviceId, DeviceName, uuid, sys_edition
    elif LoginChoice == 'n':
        print("由于缺少token退出")
        exit()


def apply_login_result(token: str, device_id: str, device_name: str,
                       uuid_value: str, sys_edition: str, conf_path: str = None):
    """评审 P1：登录成功后，无论用户是否选择写盘，都必须立即更新内存态。

    更新旧全局快照 + 已构建的 _CLIENT（token/设备身份/版本），并同步 Login
    阶段发现的新学校地址（Login 无条件写回 school_host，这里以实际配置文件为准
    重读），否则“登录后首个请求”仍会携带空 token 和旧 base_url。
    """
    global my_token, my_device_id, my_device_name, my_uuid, my_sys_edition
    global my_host, _CLIENT
    my_token = token
    my_device_id = device_id
    my_device_name = device_name
    my_uuid = uuid_value
    my_sys_edition = sys_edition

    # 学校地址同步：Login 内部已把探测结果写入实际配置文件，这里以文件为准重读
    conf_path = conf_path or project_resource("config.ini")
    try:
        conf = configparser.ConfigParser()
        conf.read(conf_path, encoding="utf-8")
        new_host = conf.get("Yun", "school_host", fallback=my_host or "")
        sys_version = conf.get("User", "sys_version", fallback="") or sys_edition
    except Exception:
        new_host = my_host or ""
        sys_version = sys_edition

    if _CLIENT is None:
        _CLIENT = default_client()
    from dataclasses import replace as _dc_replace
    _CLIENT.profile = _dc_replace(
        _CLIENT.profile,
        token=token, device_id=device_id, device_name=device_name,
        uuid=uuid_value, sys_version=sys_version,
    )
    if new_host:
        my_host = new_host
        _CLIENT.base_url = new_host


class Yun_For_New:

    def __init__(self, auto_generate_task=False, client: YunClient = None, home_info=None,
                 face_runner=None):
        """home_info: getHomeRunInfo 响应 dict（离线注入用）。不传才发真实请求。

        face_runner: yun_face.FaceRunner。提供时 runFaceStatus=Y 不再直接停止，
        而是由距离窗口触发人脸执行；不提供时保持 A 阶段停止门行为。
        """
        self.client = client or default_client()
        if home_info is not None:
            obj = home_info if isinstance(home_info, dict) else json.loads(home_info)
            if obj.get("code") not in (None, 200):
                raise BusinessException(obj.get("code"), obj.get("msg", ""), obj)
            data = obj['data']['cralist'][0]
        else:
            obj = self.client.post_json('/run/getHomeRunInfo', '',
                                        raise_on_business_code=True)
            cralist = (obj.get('data') or {}).get('cralist') or []
            if not cralist:
                raise BusinessException(obj.get('code'), 'cralist 为空，没有可用跑步任务', obj)
            data = cralist[0]

        # 人脸开关（评审 P2 + §二.4）：启用开关是任务 runFaceStatus
        # （SportRunMapActivity.java:4314），faceTime 只是窗口参数。
        # 'N' 放行；'Y' 且无照片源 → 停止（A 阶段行为保持）；缺失/未知不假设通过。
        face_status = str(data.get('runFaceStatus', '')).strip().upper()
        self.face_runner = face_runner
        self._face_trigger = None
        if face_status == 'Y':
            if face_runner is None:
                raise FaceRequiredError(
                    "该跑步任务要求人脸核验（runFaceStatus=Y）。自动流程已停止；"
                    "可使用官方 App，或提供 --face-photo/--face-video 启用离线人脸管线。")
        elif face_status != 'N':
            raise FaceRequiredError(
                f"人脸核验状态缺失或未知（runFaceStatus={data.get('runFaceStatus')!r}），"
                "按未通过处理，自动流程已停止。")
        self.runFaceStatus = face_status

        self.raType = data['raType']
        self.raId = data['id']
        self.strides = strides
        self.schoolId = data['schoolId']
        self.raRunArea = data['raRunArea']
        self.raDislikes = data['raDislikes']
        self.raMinDislikes = data['raDislikes']
        self.raSingleMileageMin = data['raSingleMileageMin'] + single_mileage_min_offset
        self.raSingleMileageMax = data['raSingleMileageMax'] + single_mileage_max_offset
        self.raCadenceMin = data['raCadenceMin'] + cadence_min_offset
        self.raCadenceMax = data['raCadenceMax'] + cadence_max_offset
        points = data['points'].split('|')
        if auto_generate_task:
            # 如果只要打表，完全可以不执行下面初始化代码
            self.my_select_points = ""
            with open("./map.json") as f:
                my_s = f.read()
                tmp = json.loads(my_s)
                self.my_select_points = tmp["mypoints"]
                self.my_point = tmp["origin_point"]
            for my_select_point in self.my_select_points:  # 手动取点
                if my_select_point in points:
                    print(my_select_point + " 存在")
                else:
                    print(my_select_point + " 不存在")
                    raise ValueError
            print('开始标记打卡点...')
            self.now_dist = 0
            i = 0
            while (self.now_dist / 1000 > min_distance + allow_overflow_distance) or self.now_dist == 0:
                i += 1
                print('第' + str(i) + '次尝试...')
                self.manageList: List[Dict] = []  # 列表的每一个元素都是字典
                self.now_dist = 0
                self.now_time = 0
                self.task_list = []
                self.task_count = 0
                self.myLikes = 0
                self.generate_task(self.my_select_points)
            self.now_time = int(random.uniform(min_consume, max_consume) * 60 * (self.now_dist / 1000))
            print('打卡点标记完成！本次将打卡' + str(self.myLikes) + '个点，处理' + str(len(self.task_list)) + '个点，总计'
                  + format(self.now_dist / 1000, '.2f')
                  + '公里，将耗时' + str(self.now_time // 60) + '分' + str(self.now_time % 60) + '秒')
            # 这三个只是初始化，并非最终值
            self.recordStartTime = ''
            self.crsRunRecordId = 0
            self.userName = ''

    def generate_task(self, points):
        for point_index, point in enumerate(points):
            if self.now_dist / 1000 < min_distance or self.myLikes < self.raMinDislikes:  # 里程不足或者点不够
                self.manageList.append({
                    'point': point,
                    'marked': 'Y',
                    'index': str(point_index)
                })
                self.add_task(point)
                self.myLikes += 1
                # 必须的任务
            else:
                self.manageList.append({
                    'point': point,
                    'marked': 'N',
                    'index': ''
                })
                # 多余的点
        # 如果跑完了表都不够
        if self.now_dist / 1000 < min_distance:
            print("跑完了一圈关键点，长度仍然不够，会自动回跑绕圈圈")
            print('公里数不足' + str(min_distance) + '公里，将自动回跑...')
            index = 0
            while self.now_dist / 1000 < min_distance:
                self.add_task(self.manageList[index]['point'])
                index = (index + 1) % self.raDislikes

    # 每10个路径点作为一组splitPoint;
    # 若最后一组不满10个且多于1个，则将最后一组中每两个点位分取10点（含终点而不含起点），作为一组splitPoint
    # 若最后一组只有1个（这种情况只会发生在len(splitPoints) > 0），则将已插入的最后一组splitPoint的最后一个点替换为最后一组的点
    def add_task(self, point):  # add_task 传一个点，开始跑
        if not self.task_list:
            origin = self.my_point
        else:
            origin = self.task_list[-1]['originPoint']  # 列表的-1项当起始点
        data = {
            'key': my_key,
            'origin': origin,  # 起始点
            'destination': point  # 传入的点
        }
        resp = requests.get("https://restapi.amap.com/v4/direction/bicycling", params=data,
                            timeout=yun_http.DEFAULT_TIMEOUT)  # A.4：外部调用同样加超时
        # 规划的点
        j = json.loads(resp.text)
        split_points = []
        split_point = []
        for path in j['data']['paths']:
            self.now_dist += path['distance']  # 路径长度
            path['steps'][-1]['polyline'] += ';' + point  # 补上了一个起始点
            for step in path['steps']:
                polyline = step['polyline']
                points = polyline.split(';')
                for p in points:
                    i = len(split_point)
                    distForthis = self.now_dist - path['distance'] * (split_count - i) / split_count
                    timeForthis = int(((min_consume + max_consume) / 2) * 60 * (self.now_dist - path['distance'] * (split_count - i)) / 1000)
                    split_point.append({
                        'point': p,
                        'runStatus': '1',
                        'speed': format((min_consume + max_consume) / 2, '.2f'),
                        # 最小和最大速度之间的随机
                        'isFence': 'Y',
                        'isMock': False,
                        "runMileage": distForthis,
                        "runTime": timeForthis
                    })
                    if len(split_point) == split_count:
                        # 到了10个，加入列表组中
                        split_points.append(split_point)
                        # 任务数量加一
                        self.task_count = self.task_count + 1
                        # 清空组
                        split_point = []

        if len(split_point) > 1:  # 不满10个且多于一个
            b = split_point[0]['point']
            # 上一个点坐标
            for i in range(1, len(split_point)):
                # 建立一个分割列表
                new_split_point = []
                # 保存上一个点的信息
                a = b
                b = split_point[i]['point']
                # 对a和b求坐标
                a_split = a.split(',')
                b_split = b.split(',')
                a_x = float(a_split[0])
                a_y = float(a_split[1])
                b_x = float(b_split[0])
                b_y = float(b_split[1])
                # 真就均匀等分啊
                d_x = (b_x - a_x) / split_count
                d_y = (b_y - a_y) / split_count
                # 补上10个点
                for j in range(0, split_count):
                    distForthis = self.now_dist - (path['distance'] / len(split_point)) * (split_count - j) / split_count
                    timeForthis = int(((min_consume + max_consume) / 2) * 60 * (self.now_dist - (path['distance'] / len(split_point)) * (split_count - j) / split_count) / 1000)
                    new_split_point.append({
                        'point': str(a_x + (j + 1) * d_x) + ',' + str(a_y + (j + 1) * d_y),
                        'runStatus': '1',
                        'speed': format((min_consume + max_consume) / 2, '.2f'),
                        # 最小和最大速度之间的随机
                        'isFence': 'Y',
                        'isMock': False,
                        "runMileage": distForthis,
                        "runTime": timeForthis
                    })
                split_points.append(new_split_point)
                # 最后一组被分成了 2 ~ 9 组
                self.task_count = self.task_count + 1
        elif len(split_point) == 1:  # 直接把最后一个点扔进去
            split_points[-1][-1] = split_point[0]  # 最后的最后点直接替换
        # 把任务列表加入
        self.task_list.append({
            'originPoint': point,
            'points': split_points
        })

    def start(self):
        data = {
            'raRunArea': self.raRunArea,
            'raType': self.raType,
            'raId': self.raId
        }
        j = self.client.post_json('/run/start', json.dumps(data),
                                  raise_on_business_code=True)
        d = j.get('data') or {}
        # 评审 P1：APK 先保存 recordId/开始时间/人脸窗口参数，再判 canSport
        # （SportRunMapActivity.java:783-786）。即便后续拒绝/要求人脸，
        # “已经下发的 recordId”也必须留存在异常里，供处理已开始但不可继续的状态。
        self.crsRunRecordId = d.get('id')
        self.recordStartTime = d.get('recordStartTime')
        self.userName = d.get('studentId')

        # canSport 是客户端字符串状态："N"=拒绝（TextUtils.equals("N",…) :790）。
        # 业务拒绝与人脸要求是两类状态（评审 P1），分开抛出。
        can = str(d.get('canSport', '') or '').strip().upper()
        if can == 'N':
            raise RunNotPermittedError(
                "服务端拒绝开始跑步（canSport=N）：" + str(d.get('warnContent', '')),
                record_id=self.crsRunRecordId, raw=d)

        # 评审 P2：faceTime 是窗口参数不是启用开关；开关只在 runFaceStatus。
        # APK 对 faceTime 有 10s 下界（:787-788）；Y 任务缺窗口参数不得默认通过。
        ft_raw = d.get('faceTime')
        try:
            self.faceTime = int(ft_raw) if ft_raw not in (None, '') else None
        except (TypeError, ValueError):
            self.faceTime = None
        if self.faceTime is not None and self.faceTime < yun_face.FACE_TIME_FLOOR:
            self.faceTime = yun_face.FACE_TIME_FLOOR
        self.randomList = d.get('randomList')

        if self.runFaceStatus == 'Y':
            if not self.randomList:
                raise FaceRequiredError(
                    "runFaceStatus=Y 但 start 响应缺少 randomList 窗口参数，"
                    "不默认放行，自动流程已停止。")
            self._face_trigger = yun_face.WindowTrigger(
                yun_face.windows_from_random_list(self.crsRunRecordId, self.randomList))
            print(f"云运动任务创建成功！（runFaceStatus=Y，"
                  f"待触发人脸窗口 {len(self._face_trigger.windows)} 个）\n")
        else:
            if self.faceTime not in (None, 0):
                print(f"提示：响应 faceTime={self.faceTime}，但任务 runFaceStatus=N；"
                      "APK 逻辑人脸窗口不启用，本流程不会发送人脸请求")
            if self.faceTime is None:
                print("提示：响应未含 faceTime（任务开关为 N，按无人脸窗口处理；"
                      "服务端行为未经线上验证）")
            print("云运动任务创建成功！\n")

    def _face_on_mileage(self, meters: float):
        """W1 等价挂钩：每次 splitPoint 成功后用累计里程(米)推进距离窗口。

        触发即执行整次人脸（语音引导→图像管线→上传状态机）。失败策略是停止
        并保留现场；APK 在此处会走 checkRunState 自动提交（T1 :2798），本脚本
        不自动提交——该偏差在 docs 中显式记录。
        """
        trigger = getattr(self, "_face_trigger", None)
        if trigger is None:
            return
        window = trigger.on_distance(meters / 1000.0)
        if window is None:
            return
        runner = self.face_runner
        if runner is None:
            raise FaceRequiredError(
                f"人脸核验窗口 {window.id_str} 已触发（runFaceStatus=Y），"
                "但未提供照片/视频源；自动流程停止，请使用官方 App 完成核验。")
        window.voice_second = self.client.now()
        print(f"[face] 窗口 {window.id_str} 触发 @ {meters / 1000.0:.3f} km")
        outcome = runner.run_window(window, self.client, self.crsRunRecordId)
        trigger.in_flight = False
        if outcome.state == "success":
            print("[face] 比对通过（data.status=Y），跑步继续")
        elif outcome.state == "compare_failed":
            raise FaceRunStopError(
                f"人脸比对未通过（{outcome.msg or outcome.detail}）。"
                "APK 行为会自动提交本次跑步数据（T1/checkRunState），"
                "本脚本策略：不自动提交，保留现场由人工决定。")
        elif outcome.state == "session_terminated":
            print("[face] 会话已终止，本次比对结果按迟到回调丢弃")
        else:
            raise FaceRunStopError(
                f"人脸上传未确认成功（{outcome.msg or outcome.detail}）；"
                "结果未知，不要盲目重发，请先查询服务端记录。")

    def split(self, points):
        data = {
            "StepNumber": int(points[9]['runMileage'] - points[0]['runMileage']) / self.strides,
            'a': 0,
            'b': None,
            'c': None,
            "mileage": points[9]['runMileage'] - points[0]['runMileage'],
            "orientationNum": 0,
            "runSteps": random.uniform(self.raCadenceMin, self.raCadenceMax),
            'cardPointList': points,
            "simulateNum": 0,
            "time": points[9]['runTime'] - points[0]['runTime'],
            'crsRunRecordId': self.crsRunRecordId,
            "speeds": format((min_consume + max_consume) / 2, '.2f'),
            'schoolId': self.schoolId,
            "strides": self.strides,
            'userName': self.userName
        }
        # 特殊接口：gzip 后再 SM4（合工大抓包验证过；其他学校未知）
        resp = self.client.post("/run/splitPointCheating",
                                raw_bytes=gzip.compress(json.dumps(data).encode("utf-8")))
        print('  ' + resp)

    def do(self):
        sleep_time = self.now_time / (self.task_count + 1)
        print('等待' + format(sleep_time, '.2f') + '秒...')
        self.client.sleep(sleep_time)  # 隔一段时间
        for task_index, task in enumerate(self.task_list):
            print('开始处理第' + str(task_index + 1) + '个点...')  # 打卡点组
            for split_index, split in enumerate(task['points']):  # 一组splitpoints （高德点10个一组）
                self.split(split)  # 发送一组splitpoint （发送的高德点）
                print('  第' + str(split_index + 1) + '次splitPoint发送成功！等待' + format(sleep_time, '.2f') + '秒...')
                self.client.sleep(sleep_time)
            print('第' + str(task_index + 1) + '个点处理完毕！')

    def do_by_points_map(self, path='./tasks', random_choose=False, isDrift=False):
        files = os.listdir(path)
        files.sort()
        if not random_choose:
            print("检测到可用表格：[输入-1随机选择，输入序号选择对应task]")
            print(files)
            choice = int(input("选择："))
            if choice == -1:
                file = os.path.join(path, random.choice(files))
                print("随机选择：" + file)
            else:
                file = os.path.join(path, files[choice])
        else:
            file = os.path.join(path, random.choice(files))
            print("随机选择：" + file)
        with open(file, 'r', encoding='utf-8') as f:
            self.task_map = json.loads(f.read())
        if isDrift:
            self.task_map = add_drift(self.task_map)
        points = []
        count = 0
        for point in tqdm(self.task_map['data']['pointsList'], leave=True):
            # 评审 2.2：loader 不得静默丢弃真机字段（runStep/ts 等），
            # 在保留原字段的基础上覆盖脚本可控项。
            point_changed = dict(point)
            point_changed.update({
                'runStatus': '1',
                'speed': point['speed'],
                # 打表，为了防止格式意外，来一个格式化
                'isFence': 'Y',
                'isMock': False,
                "runMileage": point['runMileage'],
                "runTime": point['runTime'],
                "ts": str(int(time.time()))
            })
            points.append(point_changed)
            count += 1
            if count == split_count:
                self.split_by_points_map(points)
                self._face_on_mileage(float(points[-1]['runMileage']))
                sleep_time = self.task_map['data']['duration'] / len(self.task_map['data']['pointsList']) * split_count
                print(f" 等待{sleep_time:.2f}秒.")
                self.client.sleep(sleep_time)
                count = 0
                points = []
        if count != 0:
            self.split_by_points_map(points)
            self._face_on_mileage(float(points[-1]['runMileage']))
            count = 0
            points = []

    def split_by_points_map(self, points):
        data = {
            "StepNumber": int(float(points[-1]['runMileage']) - float(points[0]['runMileage'])) / self.strides,
            'a': 0,
            'b': None,
            'c': None,
            "mileage": float(points[-1]['runMileage']) - float(points[0]['runMileage']),
            "orientationNum": 0,
            "runSteps": random.uniform(self.raCadenceMin, self.raCadenceMax),
            'cardPointList': points,
            "simulateNum": 0,
            "time": float(points[-1]['runTime']) - float(points[0]['runTime']),
            'crsRunRecordId': self.crsRunRecordId,
            "speeds": self.task_map['data']['recodePace'],
            'schoolId': self.schoolId,
            "strides": self.strides,
            'userName': self.userName
        }
        resp = self.client.post("/run/splitPointCheating",
                                raw_bytes=gzip.compress(json.dumps(data).encode("utf-8")))
        print('  ' + resp)

    def finish_by_points_map(self):
        print('发送结束信号...')
        data = {
            'recordMileage': self.task_map['data']['recordMileage'],
            'recodeCadence': self.task_map['data']['recodeCadence'],
            'recodePace': self.task_map['data']['recodePace'],
            'deviceName': my_device_name,
            'sysEdition': my_sys_version or my_sys_edition,
            'appEdition': my_app_edition,
            'raIsStartPoint': 'Y',
            'raIsEndPoint': 'Y',
            'raRunArea': self.raRunArea,
            'recodeDislikes': str(self.task_map['data']['recodeDislikes']),
            'raId': str(self.raId),
            'raType': self.raType,
            'id': str(self.crsRunRecordId),
            'duration': self.task_map['data']['duration'],
            'recordStartTime': self.recordStartTime,
            'manageList': self.task_map['data']['manageList'],
            'remake': '1'
        }
        resp = self.client.post("/run/finish", json.dumps(data))
        print(resp)

    def finish(self):
        print('发送结束信号...')
        data = {
            'recordMileage': format(self.now_dist / 1000, '.2f'),
            'recodeCadence': str(random.randint(self.raCadenceMin, self.raCadenceMax)),
            'recodePace': format(self.now_time / 60 / (self.now_dist / 1000), '.2f'),
            'deviceName': my_device_name,
            'sysEdition': my_sys_version or my_sys_edition,
            'appEdition': my_app_edition,
            'raIsStartPoint': 'Y',
            'raIsEndPoint': 'Y',
            'raRunArea': self.raRunArea,
            'recodeDislikes': str(self.myLikes),
            'raId': str(self.raId),
            'raType': self.raType,
            'id': str(self.crsRunRecordId),
            'duration': str(self.now_time),
            'recordStartTime': self.recordStartTime,
            'manageList': self.manageList,
            'remake': '1'
        }
        resp = self.client.post("/run/finish", json.dumps(data))
        print(resp)


def dry_run_responder(home_fixture: dict, face_status: str = "Y",
                      face_windows=(), face_compare_status: str = "Y"):
    """dry-run 假传输回包：只覆盖打表链路的只读/状态接口形态，绝不联网。"""
    def responder(router: str, envelope: dict) -> dict:
        if router.endswith("/run/getHomeRunInfo"):
            return home_fixture
        if router.endswith("/run/start"):
            return {"code": 200, "msg": "dry-run", "data": {
                "recordStartTime": "2000-01-01 00:00:00", "id": 900001,
                "studentId": "DRYRUN001",
                # APK 形态：canSport 为字符串（"N"=拒绝）；faceTime 为窗口参数秒
                "faceTime": 20 if face_windows else 0,
                "randomList": list(face_windows),
                "canSport": "Y"}}
        if router.endswith("/run/appFace/runFaceInfoComparison"):
            # 比对结果分层：外层 code + data.status（§二.3）
            return {"code": 200, "msg": "dry-run",
                    "data": {"status": face_compare_status, "msg": "dry-run"}}
        if router.endswith("/run/finish"):
            return {"code": 200, "msg": "dry-run", "data": None}
        # splitPointCheating 等默认成功
        return {"code": 200, "msg": "dry-run", "data": None}
    return responder


def build_face_runner(args, sleep=None):
    """按 CLI 输入构建 yun_face.FaceRunner；未提供源返回 None。

    用 getattr 读取 face_* 参数，兼容旧调用方自造的 argparse.Namespace。
    """
    face_photo = getattr(args, "face_photo", None)
    face_video = getattr(args, "face_video", None)
    face_mirror = bool(getattr(args, "face_mirror", False))
    if not (face_photo or face_video):
        if face_mirror:
            raise ValueError("--face-mirror 需要配合 --face-photo/--face-video 使用")
        return None
    if face_photo and face_video:
        raise ValueError("--face-photo 与 --face-video 二选一（同一时刻只有一个输入源）")
    detection = None
    face_detection = getattr(args, "face_detection", None)
    if face_detection:
        detection = yun_face.load_detection_json(resolve_cli_path(face_detection))
    else:
        print("[face] 未提供 --face-detection 标注：取景质量门将因无检测结果按失败处理"
              "（不以“图里有人脸”替代客户端姿态门）。")
    if face_video:
        source = yun_face.VideoFrameSource(resolve_cli_path(face_video),
                                           mirror=face_mirror)
    else:
        source = yun_face.PhotoSource(resolve_cli_path(face_photo),
                                      mirror=face_mirror)
    return yun_face.FaceRunner(source, detection=detection,
                               sleep=sleep or time.sleep)


def run_dry(cfg_path: str, task_path: str, args):
    """离线演练：本地 fixture + FakeTransport；不登录、不探测学校、不调高德、不 sleep、不写配置。"""
    home_path = args.dry_home or (_CONF.get("Run", "dry_home", fallback="") or None)
    home_path = resolve_cli_path(home_path) if home_path else project_resource("dry_run_home.json")
    if not os.path.exists(home_path):
        raise FileNotFoundError(
            f"dry-run 需要本地 getHomeRunInfo fixture：{home_path}"
            "（可用 --dry-home 指定）。dry-run 禁止构造时联网，故不回退真实请求。")
    with open(home_path, 'r', encoding='utf-8') as f:
        home_fixture = json.load(f)

    # 人脸演练参数从 fixture 读取（脱敏离线样本自有字段）
    hdata = (home_fixture.get('data') or {})
    hstatus = ""
    try:
        hstatus = str(hdata['cralist'][0].get('runFaceStatus', '')).strip().upper()
    except (KeyError, IndexError, TypeError):
        pass
    face_windows = list(hdata.get('dry_face_windows') or []) if hstatus == 'Y' else []
    face_compare = str(hdata.get('dry_face_compare_status', 'Y'))

    profile = build_profile(_CONF)
    fake = FakeTransport(
        dry_run_responder(home_fixture, face_windows=face_windows,
                          face_compare_status=face_compare),
        sm2box=SM2Box(PUBLIC_KEY, PRIVATE_KEY),
        fixed_pair=((profile.cipherkey_encrypted, profile.cipherkey)
                    if profile.cipherkey_encrypted else None),
        # 仓库默认密钥对公私密钥不配对（config 注明“私钥失效”），本地解不开随机封装；
        # 配置了固定 cipherkey 时以其兜底构造回包，与线上“服务端持配对私钥”等效。
        fallback_key_b64=profile.cipherkey or None,
    )
    client = YunClient(profile, base_url=my_host, transport=fake,
                       rng=random.Random(20260911), sleep=lambda s: None)
    face_runner = build_face_runner(args, sleep=lambda s: None)
    try:
        yun = Yun_For_New(auto_generate_task=False, client=client, home_info=None,
                          face_runner=face_runner)
        yun.start()
        yun.do_by_points_map(path=task_path, random_choose=True, isDrift=args.drift)
        yun.finish_by_points_map()
    except FaceRequiredError as e:
        print("[dry-run 停止门] " + str(e))
        raise
    routers = [c["router"] for c in fake.calls]
    verified = sum(1 for c in fake.calls if c.get("envelope_verified"))
    undec = sum(1 for c in fake.calls if "_undecodable" in (c.get("business") or {}))
    print(f"[dry-run 完成] 共构造 {len(fake.calls)} 个请求，全部走假传输（无真实网络）：{routers}")
    print(f"[dry-run 信封] 可用持有私钥直接验证的信封 {verified}/{len(fake.calls)}；"
          f"业务体不可解码 {undec} 个（兜底 key 路线下按等效假设构造回包，不宣称信封已验证）")
    print("[dry-run 声明] 产物名称：offline_client_compatibility。"
          "以上仅证明本地构造与静态证据一致（非 face_passed_live）；"
          "服务端是否接受、成绩是否有效、人脸是否通过均未验证。")
    return client  # 供测试/审阅者检查 fake.calls


def main(run=True):
    args = parse_args()
    cfg_path = resolve_cli_path(args.config_path) if args.config_path else project_resource("config.ini")
    task_path = resolve_cli_path(args.task_path) if args.task_path else project_resource("tasks_fch")

    # 设置全局变量（快照）并构建统一客户端
    user_info = set_args(cfg_path)
    global my_token, my_device_id, my_device_name, my_uuid, my_sys_edition

    if args.dry_run:
        # 评审 §3-A.5：dry-run 不登录、不探测、不 sleep、不写配置
        run_dry(cfg_path, task_path, args)
        return

    if not args.auto_run:
        if len(my_token) == 0:
            result = noTokenLogin(cfg_path)
            if result is None:
                return
            # 评审 P1：登录后同步内存客户端（含新学校地址），不只更新全局值
            apply_login_result(*result, conf_path=cfg_path)

        print("确定数据无误：")
    # A.4：默认输出脱敏（token/uuid/sign/map_key 掩码）
    print("Token: ".ljust(15) + mask_secret(my_token))
    print('deviceId: '.ljust(15) + my_device_id)
    print('deviceName: '.ljust(15) + my_device_name)
    print('utc: '.ljust(15) + my_utc)
    print('uuid: '.ljust(15) + mask_secret(my_uuid))
    print('sign: '.ljust(15) + mask_secret(my_sign))
    print('map_key: '.ljust(15) + mask_secret(my_key))

    if not run:
        return

    if args.auto_run:
        sure = 'y'
    else:
        sure = input("确认：[y/n]")
    try:
        if sure == 'y':
            if args.auto_run:
                print_table = 'y'
            else:
                print_table = input("打表模式(固定路线，无需高德地图key)：[y/n]")
            if print_table == 'y':
                if not args.auto_run:
                    print("warning:\n打表模式下\n跑步的步频、配速等信息受tasklist.json控制，不会读取map.json，config.ini的跑步信息失效")
                    choice = input("请选择校区（1.翡翠湖校区,2.屯溪路校区,3.宣城校区,4.自定义(文件夹tasks_else)）")
                    if (choice == '1'):
                        path = "./tasks_fch"
                    elif (choice == '2'):
                        path = "./tasks_txl"
                    elif (choice == '3'):
                        path = "./tasks_xc"
                    else:
                        path = "./tasks_else"
                    isDrift = input("是否为数据添加漂移：[y/n]")
                    if isDrift == 'y':
                        driftChoice = True
                    else:
                        driftChoice = False
                    Yun = Yun_For_New(auto_generate_task=False,
                                      face_runner=build_face_runner(args))
                    Yun.start()
                    Yun.do_by_points_map(path=path, isDrift=driftChoice)
                    Yun.finish_by_points_map()
                else:
                    path = task_path
                    Yun = Yun_For_New(auto_generate_task=False,
                                      face_runner=build_face_runner(args))
                    Yun.start()
                    Yun.do_by_points_map(path=path, random_choose=True, isDrift=args.drift)
                    Yun.finish_by_points_map()
            else:
                quick_model = input("快速模式(瞬间跑完)：[y/n]")
                if quick_model == 'y':
                    Yun = Yun_For_New()
                    Yun.start()
                    Yun.finish()
                else:
                    Yun = Yun_For_New()
                    print("起始点：[" + Yun.my_point + ']')
                    Yun.start()
                    Yun.do()
                    Yun.finish()
        else:
            print("退出。")
    except RunNotPermittedError as e:
        # 评审 P1：业务拒绝 ≠ 人脸要求；recordId 已下发时明示，便于查服务端记录
        rid = f"（已下发 recordId={e.record_id}）" if e.record_id else "（无 recordId）"
        print(f"[业务拒绝] {e} {rid}")
        print("[业务拒绝] 请先在服务端确认该记录状态，不要盲目重发开始请求。")
    except FaceRunStopError as e:
        print("[人脸停止] " + str(e))
    except FaceRequiredError as e:
        print("[停止] " + str(e))
        print("[停止] A 阶段策略：识别人脸要求后不模拟、不猜测，请使用官方 App 完成核验。")
    except (HttpStatusException, DecodeException) as e:
        # 评审 §3-A.1：状态未知时保留现场、报告，而不是继续 finish 或自动重发
        print("[失败] " + str(e))
        print("[失败] 请求结果可能未定（状态未知），不要盲目重发；请先查询服务端记录再决定。")
    except Exception as e:
        print("跑步失败了，错误信息：")
        print(e)
        input()


if __name__ == '__main__':
    main()

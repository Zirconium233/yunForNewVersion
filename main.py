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
from yun_http import (
    BusinessException,
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

    _CLIENT = YunClient(build_profile(conf), base_url=my_host)

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


class Yun_For_New:

    def __init__(self, auto_generate_task=False, client: YunClient = None, home_info=None):
        """home_info: getHomeRunInfo 响应 dict（离线注入用）。不传才发真实请求。"""
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

        # A 阶段人脸停止门（评审 2.3 / §3-A.6）：只识别、不模拟。
        # 仅 runFaceStatus == 'N' 视为无核验要求；'Y' 停止并提示走官方 App；
        # 缺失/未知值不假设已通过，同样停止。
        face_status = str(data.get('runFaceStatus', '')).strip().upper()
        if face_status == 'Y':
            raise FaceRequiredError(
                "该跑步任务要求人脸核验（runFaceStatus=Y）。自动流程已停止，"
                "请使用官方 App 完成人脸注册/核验。")
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
        # A.6 停止门：服务端明示不可跑或带人脸要求时，不再继续
        d = j['data']
        if d.get('canSport') is False:
            raise FaceRequiredError(
                "服务端拒绝开始跑步（canSport=false）：" + str(d.get('warnContent', '')))
        face_time = d.get('faceTime')
        if face_time not in (None, 0, '0', ''):
            raise FaceRequiredError(
                f"开始响应 faceTime={face_time}，跑步中会要求人脸核验；A 阶段不模拟人脸，自动流程停止。")
        if face_time is None:
            print("提示：响应未含 faceTime（按无人脸处理，服务端行为未经线上验证）")
        self.recordStartTime = d['recordStartTime']
        self.crsRunRecordId = d['id']
        self.userName = d['studentId']
        print("云运动任务创建成功！\n")

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
                sleep_time = self.task_map['data']['duration'] / len(self.task_map['data']['pointsList']) * split_count
                print(f" 等待{sleep_time:.2f}秒.")
                self.client.sleep(sleep_time)
                count = 0
                points = []
        if count != 0:
            self.split_by_points_map(points)
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


def dry_run_responder(home_fixture: dict):
    """dry-run 假传输回包：只覆盖打表链路的只读/状态接口形态，绝不联网。"""
    def responder(router: str, envelope: dict) -> dict:
        if router.endswith("/run/getHomeRunInfo"):
            return home_fixture
        if router.endswith("/run/start"):
            return {"code": 200, "msg": "dry-run", "data": {
                "recordStartTime": "2000-01-01 00:00:00", "id": 900001,
                "studentId": "DRYRUN001", "faceTime": 0, "canSport": True}}
        if router.endswith("/run/finish"):
            return {"code": 200, "msg": "dry-run", "data": None}
        # splitPointCheating 等默认成功
        return {"code": 200, "msg": "dry-run", "data": None}
    return responder


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

    profile = build_profile(_CONF)
    fake = FakeTransport(
        dry_run_responder(home_fixture),
        sm2box=SM2Box(PUBLIC_KEY, PRIVATE_KEY),
        fixed_pair=((profile.cipherkey_encrypted, profile.cipherkey)
                    if profile.cipherkey_encrypted else None),
        # 仓库默认密钥对公私密钥不配对（config 注明“私钥失效”），本地解不开随机封装；
        # 配置了固定 cipherkey 时以其兜底构造回包，与线上“服务端持配对私钥”等效。
        fallback_key_b64=profile.cipherkey or None,
    )
    client = YunClient(profile, base_url=my_host, transport=fake,
                       rng=random.Random(20260911), sleep=lambda s: None)
    try:
        yun = Yun_For_New(auto_generate_task=False, client=client, home_info=None)
        yun.start()
        yun.do_by_points_map(path=task_path, random_choose=True, isDrift=args.drift)
        yun.finish_by_points_map()
    except FaceRequiredError as e:
        print("[dry-run 停止门] " + str(e))
        raise
    routers = [c["router"] for c in fake.calls]
    print(f"[dry-run 完成] 共构造 {len(fake.calls)} 个请求，全部走假传输（无真实网络）：{routers}")
    print("[dry-run 声明] 以上仅证明本地构造与静态证据一致；服务端是否接受、成绩是否有效均未验证。")
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
            my_token, my_device_id, my_device_name, my_uuid, my_sys_edition = result

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
                    Yun = Yun_For_New(auto_generate_task=False)
                    Yun.start()
                    Yun.do_by_points_map(path=path, isDrift=driftChoice)
                    Yun.finish_by_points_map()
                else:
                    path = task_path
                    Yun = Yun_For_New(auto_generate_task=False)
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

import base64
import math
import random
import time
import requests
import json
import configparser
import hashlib
import os
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
from gmssl import sm4
from Crypto.Util.Padding import pad, unpad
import time
from tools.Login import Login

"""
加密模式：sm2非对称加密sm4密钥
"""
# 偏移量
# default_iv = '\1\2\3\4\5\6\7\x08' 失效

PublicKey = 'BL7JvEAV7Wci0h5YAysN0BPNVdcUhuyJszJLRwnurav0CGftcrVcvrWeCPBIjIIBF371teRbrCS9V1Wyq7i3Arc='
PrivateKey = 'P3s0+rMuY4Nt5cUWuOCjMhDzVNdom+W0RvdV6ngM+/E='
PUBLIC_KEY = b64decode(PublicKey)
PRIVATE_KEY = b64decode(PrivateKey)

def parse_args():
    parser = argparse.ArgumentParser(description='云运动自动跑步脚本')
    parser.add_argument('-f', '--config_path', type=str, default='./config.ini', help='配置文件路径')
    parser.add_argument('-t', '--task_path', type=str, default='./tasks_fch', help='任务文件路径')
    parser.add_argument('-a', '--auto_run', action='store_true', help='自动跑步，默认打表')
    parser.add_argument('-d', '--drift', action='store_true', help='是否添加漂移')
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

def encrypt_sm4(value, SM_KEY, isBytes = False):
    crypt_sm4 = CryptSM4()
    crypt_sm4.set_key(SM_KEY, SM4_ENCRYPT)
    if not isBytes:
        encrypt_value = b64encode(crypt_sm4.crypt_ecb(value.encode("utf-8")))
    else:
        encrypt_value = b64encode(crypt_sm4.crypt_ecb(value))
    return encrypt_value.decode()

def decrypt_sm4(value, SM_KEY):
    crypt_sm4 = CryptSM4()
    crypt_sm4.set_key(SM_KEY, SM4_DECRYPT)
    decrypt_value = crypt_sm4.crypt_ecb(b64decode(value))
    return decrypt_value

# warning：实测gmssl的sm2加密给Java Hutool解密结果不对，所以下面的2函数暂不使用
def encrypt_sm2(info):
    encode_info = sm2_crypt.encrypt(info.encode("utf-8"))
    encode_info = b64encode(encode_info).decode()  # 将二进制bytes通过base64编码
    return encode_info

def decrypt_sm2(info):
    decode_info = b64decode(info)  # 通过base64解码成二进制bytes
    decode_info = sm2_crypt.decrypt(decode_info)
    return decode_info


def getsign(utc, uuid):
    sb = (
        "platform="
        + platform
        + "&utc="
        + str(utc)
        + "&uuid="
        + str(uuid)
        + "&appsecret="
        + md5key
    )
    m = hashlib.md5()
    m.update(sb.encode("utf-8"))
    return m.hexdigest()

def default_post(router, data, headers=None, m_host=None, isBytes=False, gen_sign=True):
    if m_host is None:
        m_host = my_host
    url = m_host + router
    if gen_sign:
        my_utc = str(int(time.time()))
    sign = getsign(my_utc, my_uuid) if gen_sign else my_sign
    if headers is None:
        headers = {
            'token': my_token,
            'isApp': 'app',
            'deviceId': my_device_id,
            'deviceName': my_device_name,
            'version': my_app_edition,
            'platform': 'android',
            'Content-Type': 'application/json; charset=utf-8',
            'Connection': 'Keep-Alive',
            'Accept-Encoding': 'gzip',
            'User-Agent': 'okhttp/3.12.0',
            'utc': my_utc,
            'uuid': my_uuid,
            'sign': sign
        }
    data_json = {
        "cipherKey":CipherKeyEncrypted,
        "content":encrypt_sm4(data, b64decode(default_key),isBytes=isBytes)
    }
    req = requests.post(url=url, data=json.dumps(data_json), headers=headers) # data进行了加密
    try:
        return decrypt_sm4(req.text, b64decode(default_key)).decode()
    except:
        return req.text

def noTokenLogin():
    print("config中token为空，是否尝试使用账号密码登录？(y/n)")
    LoginChoice = input()
    if LoginChoice == 'y':
        token,DeviceId,DeviceName,uuid,sys_edition = Login.main()
        #TEST CONTENT
        print("是否保存本次登录产生的token和uuid？(y/n)")
        TokenWrite = input()
        if TokenWrite == 'y':
            config = configparser.ConfigParser()
            config.read('config.ini', encoding='utf-8')
            config.set('User', 'token', token)
            config.set('User', 'uuid', uuid)
            config.set('User', 'device_id', DeviceId)
            config.set('User', 'device_name', DeviceName)
            config.set('User', 'sys_edition', sys_edition)
            with open('config.ini', 'w+', encoding='utf-8') as f:
                config.write(f)
        return token,DeviceId,DeviceName,uuid,sys_edition
    elif LoginChoice == 'n':
        print("由于缺少token退出")
        exit()

class Yun_For_New:

    def __init__(self, auto_generate_task = False):
        data = json.loads(default_post("/run/getHomeRunInfo", ""))['data']['cralist'][0]
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
            for my_select_point in self.my_select_points:# 手动取点
                if my_select_point in points:
                    print(my_select_point + " 存在")
                else:
                    print(my_select_point + " 不存在")
                    raise ValueError
            print('开始标记打卡点...')
            # for exclude_point in exclude_points:
            #     try:
            #         points.remove(exclude_point)
            #         print("成功删除打卡点", exclude_point)
            #     except ValueError:
            #         print("打卡点", exclude_point, "不存在")
            #         # 删除容易跑到学校外面的打卡点
            # # 采取手动选择点的方式，上面的放出圈方法弃用
            self.now_dist = 0
            i = 0
            while (self.now_dist / 1000 > min_distance + allow_overflow_distance) or self.now_dist == 0:
                i += 1
                print('第' + str(i) + '次尝试...')
                self.manageList: List[Dict] = [] # 列表的每一个元素都是字典
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
        # random_points = random.sample(points, self.raDislikes) # 在打卡点随机选raDislike个点
        
        for point_index, point in enumerate(points):
            if self.now_dist / 1000 < min_distance or self.myLikes < self.raMinDislikes: # 里程不足或者点不够
                self.manageList.append({
                    'point': point,
                    'marked': 'Y',
                    'index': str(point_index)
                })
                self.add_task(point)
                self.myLikes += 1
                #必须的任务
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
    def add_task(self, point): # add_ task 传一个点，开始跑
        if not self.task_list:
            origin = self.my_point
        else:
            origin = self.task_list[-1]['originPoint'] # 列表的-1项当起始点
        data = {
            'key': my_key,
            'origin': origin, # 起始点
            'destination': point # 传入的点
        }
        resp = requests.get("https://restapi.amap.com/v4/direction/bicycling", params=data)
        # 规划的点
        j = json.loads(resp.text)
        split_points = []
        split_point = []
        for path in j['data']['paths']:
            self.now_dist += path['distance'] # 路径长度
            path['steps'][-1]['polyline'] += ';' + point # 补上了一个起始点
            for step in path['steps']:
                polyline = step['polyline']
                points = polyline.split(';')
                for p in points:
                    i = len(split_point)
                    distForthis = self.now_dist - path['distance']*(split_count-i)/split_count
                    timeForthis = int(((min_consume + max_consume) / 2) * 60 * (self.now_dist - path['distance']*(split_count-i)) / 1000)
                    split_point.append({
                        'point': p,
                        'runStatus': '1',
                        'speed': format((min_consume + max_consume)/2, '.2f'),
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

        if len(split_point) > 1: # 不满10个且多于一个
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
                    distForthis = self.now_dist - (path['distance']/len(split_point))*(split_count-j)/split_count
                    timeForthis = int(((min_consume + max_consume) / 2) * 60 * (self.now_dist - (path['distance']/len(split_point))*(split_count-j)/split_count) / 1000)
                    new_split_point.append({
                        'point': str(a_x + (j + 1) * d_x) + ',' + str(a_y + (j + 1) * d_y),
                        'runStatus': '1',
                        'speed': format((min_consume + max_consume)/2, '.2f'),
                        # 最小和最大速度之间的随机
                        'isFence': 'Y',
                        'isMock': False,
                        "runMileage": distForthis,
                        "runTime": timeForthis
                    })
                split_points.append(new_split_point)
                # 最后一组被分成了 2 ~ 9 组
                self.task_count = self.task_count + 1
        elif len(split_point) == 1: # 直接把最后一个点扔进去
            split_points[-1][-1] = split_point[0] # 最后的最后点直接替换
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
        j = json.loads(default_post('/run/start', json.dumps(data)))
        # 发送开始请求
        if j['code'] == 200:
            self.recordStartTime = j['data']['recordStartTime']
            self.crsRunRecordId = j['data']['id']
            self.userName = j['data']['studentId']
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
            "speeds": format((min_consume + max_consume)/2, '.2f'),
            'schoolId': self.schoolId,
            "strides": self.strides,
            'userName': self.userName
        }
        resp = default_post("/run/splitPointCheating", gzip.compress(data=json.dumps(data).encode("utf-8")), isBytes=True) # 这里是特殊的接口，不清楚其他学校，但合工大的完全OK。
        # 发送一组点
        print('  ' + resp)

    def do(self):
        sleep_time = self.now_time / (self.task_count + 1)
        print('等待' + format(sleep_time, '.2f') + '秒...')
        time.sleep(sleep_time) # 隔一段时间
        for task_index, task in enumerate(self.task_list):
            print('开始处理第' + str(task_index + 1) + '个点...') # 打卡点组
            for split_index, split in enumerate(task['points']): # 一组splitpoints （高德点10个一组）
                self.split(split) # 发送一组splitpoint （发送的高德点）
                print('  第' + str(split_index + 1) + '次splitPoint发送成功！等待' + format(sleep_time, '.2f') + '秒...')
                time.sleep(sleep_time)
            print('第' + str(task_index + 1) + '个点处理完毕！')

    def do_by_points_map(self, path = './tasks', random_choose = False, isDrift = False):
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
            point_changed = {
                'point': point['point'],
                'runStatus': '1',
                'speed': point['speed'],
                # 打表，为了防止格式意外，来一个格式化
                'isFence': 'Y',
                'isMock': False,
                "runMileage": point['runMileage'],
                "runTime": point['runTime'],
                "ts": str(int(time.time()))
            }
            points.append(point_changed)
            count += 1
            if count == split_count:
                self.split_by_points_map(points)
                sleep_time = self.task_map['data']['duration'] / len(self.task_map['data']['pointsList']) * split_count
                print(f" 等待{sleep_time:.2f}秒.")
                time.sleep(sleep_time)
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
        resp = default_post("/run/splitPointCheating", gzip.compress(data=json.dumps(data).encode("utf-8")), isBytes=True) # 这里是特殊的接口，不清楚其他学校，但合工大的完全OK。
        # 发送一组点
        print('  ' + resp)

    def finish_by_points_map(self):
        print('发送结束信号...')
        data = {
            'recordMileage': self.task_map['data']['recordMileage'],
            'recodeCadence': self.task_map['data']['recodeCadence'],
            'recodePace': self.task_map['data']['recodePace'],
            'deviceName': my_device_name,
            'sysEdition': my_sys_edition,
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
        resp = default_post("/run/finish", json.dumps(data))
        print(resp)

    def finish(self):
        print('发送结束信号...')
        data = {
            'recordMileage': format(self.now_dist / 1000, '.2f'),
            'recodeCadence': str(random.randint(self.raCadenceMin, self.raCadenceMax)),
            'recodePace': format(self.now_time / 60 / (self.now_dist / 1000), '.2f'),
            'deviceName': my_device_name,
            'sysEdition': my_sys_edition,
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
        resp = default_post("/run/finish", json.dumps(data))
        print(resp)

if __name__ == '__main__':
    args = parse_args()
    cfg_path = args.config_path
    # 加载配置文件
    # cfg_path = "./config.ini"
    conf = configparser.ConfigParser()
    conf.read(cfg_path, encoding="utf-8")

    # 学校、keys和版本信息
    my_host = conf.get("Yun", "school_host") # 学校的host
    default_key = conf.get("Yun", "cipherkey") # 加密密钥
    CipherKeyEncrypted = conf.get("Yun", "cipherkeyencrypted") # 加密密钥的sm2加密版本
    my_app_edition = conf.get("Yun", "app_edition") # app版本（我手机上是3.0.0）

    # 用户信息，包括设备信息
    my_token = conf.get("User", 'token') # 用户token 
    my_device_id = conf.get("User", "device_id") # 设备id （据说很随机，抓包搞几次试试看）
    my_key = conf.get("User", "map_key") # map_key是高德地图的开发者密钥
    my_device_name = conf.get("User", "device_name") # 手机名称
    my_sys_edition = conf.get("User", "sys_edition") # 安卓版本（大版本）
    my_utc = conf.get('User', 'utc') or str(int(time.time()))
    my_uuid = conf.get("User", "uuid")
    my_sign = conf.get("User", "sign")

    # 跑步相关的信息
    # my_point = conf.get("Run", "point") # 当前位置，取消，改到map.json
    min_distance = float(conf.get("Run", "min_distance")) # 2公里
    allow_overflow_distance = float(conf.get("Run", "allow_overflow_distance")) # 允许偏移超出的公里数
    single_mileage_min_offset = float(conf.get("Run", "single_mileage_min_offset")) # 单次配速偏移最小
    single_mileage_max_offset = float(conf.get("Run", "single_mileage_max_offset")) # 单次配速偏移最大
    cadence_min_offset = int(conf.get("Run", "cadence_min_offset")) # 最小步频偏移
    cadence_max_offset = int(conf.get("Run", "cadence_max_offset")) # 最大步频偏移
    split_count = int(conf.get("Run", "split_count")) 
    exclude_points = json.loads(conf.get("Run", "exclude_points")) # 排除点
    min_consume = float(conf.get("Run", "min_consume")) # 配速最小和最大
    max_consume = float(conf.get("Run", "max_consume"))
    strides = float(conf.get("Run", "strides"))

    PUBLIC_KEY = b64decode(conf.get("Yun", "PublicKey"))
    PRIVATE_KEY = b64decode(conf.get("Yun", "PrivateKey"))

    md5key = conf.get("Yun", "md5key")
    platform = conf.get("Yun", "platform")
    if not args.auto_run:
        if len(conf.get('User', 'token')) == 0:
            my_token,my_device_id,my_device_name,my_uuid,my_sys_edition = noTokenLogin()
        # 检查版本是否小于3.4.5
        if conf.get('Yun', 'app_edition') < '3.4.5':
            conf.set('Yun', 'app_edition', '3.4.5')
            with open(cfg_path, 'w+', encoding='utf-8') as f:
                conf.write(f)
        #config app版本检查 当前可用3.4.5
        
        print("确定数据无误：")
    print("Token: ".ljust(15) + my_token)
    print('deviceId: '.ljust(15) + my_device_id)
    print('deviceName: '.ljust(15) +  my_device_name)
    print('utc: '.ljust(15) + my_utc)
    print('uuid: '.ljust(15) + my_uuid)
    print('sign: '.ljust(15) + my_sign)
    print('map_key: '.ljust(15) + my_key)

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
                    if(choice == '1'): path = "./tasks_fch"
                    elif(choice == '2'): path = "./tasks_txl"
                    elif(choice == '3') : path = "./tasks_xc"
                    else: path = "./tasks_else"
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
                    path = args.task_path
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
    except Exception as e:
        print("跑步失败了，错误信息：")
        print(e)
        input()


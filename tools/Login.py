# -*- coding: utf-8 -*-
"""账号密码登录（学校业务服务的 /login/<route> 路线）。

A 阶段修订（评审 2.1.3/2.1.5、§3-A.2/A.4）：
- Login.main(conf_path) 贯穿调用方实际使用的配置路径：读取、写回都用它，
  不再硬编码 './config.ini'；缺省（独立运行）按项目根解析。
- 头部/签名/信封/解码统一走 yun_http（固定 cipherKey 信封为登录路线既有形态，保留）。
- 失败返回 None（不再进程内 exit()），由调用方决定；输出对 token 等脱敏。
- 静态样本只证明客户端形态，不证明服务端仍接受固定 cipherKey 或本仓库密钥值（未线上验证）。
"""
import configparser
import json
import os
import random
import sys
import time
from dataclasses import replace

# 允许 `python tools/Login.py`（历史用法）与 `from tools.Login import Login` 两种入口
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import yun_http
from yun_http import (
    DecodeException,
    HttpStatusException,
    YunClient,
    join_url,
    mask_secret,
)

try:  # 作为包导入时
    from .getUrl_Id import getschool_Url_Id, DEFAULT_CONF
except ImportError:  # 在 tools/ 目录下直接运行时
    from getUrl_Id import getschool_Url_Id, DEFAULT_CONF


def _write_conf(conf, path):
    with open(path, 'w', encoding='utf-8') as f:
        conf.write(f)


class Login():

    def main(conf_path=None, transport=None):
        path = conf_path or DEFAULT_CONF
        if not os.path.exists(path):
            print(f"配置文件不存在: {path}")
            return None

        conf = configparser.ConfigParser()
        conf.read(path, encoding='utf-8')

        if 'Login' not in conf.sections():
            conf.add_section('Login')
            conf.set('Login', 'username', '')
            conf.set('Login', 'password', '')
            _write_conf(conf, path)

        if 'school_id' not in conf['Yun']:
            conf.set('Yun', 'school_id', '100')
            _write_conf(conf, path)

        # 读取ini配置（密码只在内存中使用，不打印）
        username = conf.get('Login', 'username') or input('未找到用户名，请输入用户名：')
        password = conf.get('Login', 'password') or input('未找到密码，请输入密码：')
        iniDeviceId = conf.get('User', 'device_id')
        iniDeviceName = conf.get('User', 'device_name')
        iniuuid = conf.get('User', 'uuid')
        iniSysedition = conf.get('User', 'sys_edition')
        schoolName = conf.get('Yun', 'school_name') or input("未找到学校名称，请输入学校名称：")
        conf.set('Yun', 'school_Name', schoolName)
        url, scId = getschool_Url_Id(schoolName, conf_path=path, transport=transport)
        if url and scId:
            conf.set('Yun', 'school_host', url)
            conf.set('Yun', 'school_id', str(scId))
            _write_conf(conf, path)
        schoolid = conf.get('Yun', 'school_id')
        schoolHost = conf.get('Yun', 'school_host')

        # 不同学校不同，例如 appLoginHGD appLoginCHZU appLogin
        school_login_url = conf.get('Yun', "school_login_url")
        login_url = join_url(schoolHost, '/login/' + school_login_url)

        if username != conf.get('Login', 'username'):
            conf.set('Login', 'username', username)
            _write_conf(conf, path)
        if password != conf.get('Login', 'password'):
            conf.set('Login', 'password', password)
            _write_conf(conf, path)

        # 如果部分配置为空则随机生成
        if iniDeviceId != '':
            DeviceId = iniDeviceId
        else:
            DeviceId = str(random.randint(1000000000000000, 9999999999999999))
            conf.set('User', 'device_id', DeviceId)
            _write_conf(conf, path)
        uuid = iniuuid if iniuuid != '' else DeviceId

        if iniDeviceName != '':
            DeviceName = iniDeviceName
        else:
            print('DeviceName为空 请输入希望使用的设备名\n留空则使用默认名')
            DeviceName = input() or 'Xiaomi'

        if iniSysedition != '':
            sys_edition = iniSysedition
        else:
            print('Sys_edition为空 请输入希望使用的系统版本\n留空则使用14')
            sys_edition = input() or '14'

        body = json.dumps({
            "password": password,
            "schoolId": schoolid,
            "userName": username,
            "type": "1",
        }, ensure_ascii=False)

        profile = yun_http.profile_from_conf(conf)
        profile = replace(profile, token="", device_id=uuid, device_name=DeviceName)
        client = YunClient(profile, base_url=schoolHost, transport=transport)
        try:
            result = client.post_json("/login/" + school_login_url, body,
                                      absolute_url=login_url, fixed_envelope=True)
        except HttpStatusException as e:
            # 登录失败不跨服务器重放密码（评审 §3-C），报告后交由调用方决定
            print(f"登录请求失败: {e}")
            return None
        except DecodeException as e:
            print(f"登录响应无法解码: {e}")
            return None

        if result.get('code') != 200 or 'data' not in result:
            print("登录失败:", result.get('msg', result))
            return None
        token = (result.get('data') or {}).get('token')
        if not token:
            print("登录响应缺少 token")
            return None

        print("登录成功，本次登录获得的token为：" + mask_secret(token)
              + "  本次使用的uuid为：" + mask_secret(uuid))
        print("!请注意! 使用脚本登录后会导致手机客户端登录失效\n请尽量减少手机登录次数，避免被识别为多设备登录代跑")
        return token, DeviceId, DeviceName, uuid, sys_edition

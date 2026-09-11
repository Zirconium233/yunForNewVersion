# -*- coding: utf-8 -*-
"""学校目录查询（旧路线 http://sports.aiyyd.com:9001/api/app/schoolList）。

A 阶段修订（评审 2.1.5 / 2.2 表 BASE_URL 行）：
- 模块导入时不再读取 config.ini（去除导入副作用）；缺省配置按项目根解析，可用 conf_path 覆盖。
- 头部构造、签名、信封与响应解码统一走 yun_http；不再各处复制实现。
- uuid 不再硬编码抓包值：配置 [User] uuid 非空则沿用，为空则每请求随机大写（与 3.6.6 证据一致）。
- 该端点属于“公共目录”，与学校业务服务、登录路由分开；A 阶段不改默认端点值。
"""
import configparser
import hashlib
import json
import os
import sys
from base64 import b64decode, b64encode

# 允许 `python tools/getUrl_Id.py` 直接从 tools 目录运行
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import yun_http
from yun_http import YunClient, join_url
from yun_http import encrypt_sm4, decrypt_sm4  # 兼容旧导出名

DEFAULT_CONF = os.path.join(_REPO_ROOT, "config.ini")
SCHOOL_LIST_BASE = "http://sports.aiyyd.com:9001/api"


def md5_encryption(data):
    md5 = hashlib.md5()
    md5.update(data.encode('utf-8'))
    return md5.hexdigest()


def load_conf(conf_path=None):
    path = conf_path or DEFAULT_CONF
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")
    conf = configparser.ConfigParser()
    conf.read(path, encoding="utf-8")
    return conf


def getschool_Url_Id(schoolName, conf_path=None, transport=None, rng=None):
    """按学校名查 (schoolUrl, schoolId)。transport 可注入假传输（测试/dry-run）。"""
    conf = load_conf(conf_path)
    profile = yun_http.profile_from_conf(conf)
    client = YunClient(profile, base_url=SCHOOL_LIST_BASE, transport=transport, rng=rng)
    infojson = client.post_json("/app/schoolList", "", fixed_envelope=True)
    if infojson.get('code') != 200:
        print("请求失败，请检查输入或网络。", infojson.get('msg', ''))
        return None, None
    for school in infojson.get('data', []):
        if school.get('schoolName') == schoolName:
            schoolUrl = school.get('schoolUrl').rstrip('/')
            schoolId = school.get('schoolId')
            return schoolUrl, schoolId
    print("未找到匹配的学校名称")
    return None, None


def writeUrlToConfig(schoolUrl, schoolId, conf_path=None):
    path = conf_path or DEFAULT_CONF
    config = load_conf(path)
    current_school_host = config.get("Yun", "school_host", fallback="")
    current_school_id = config.get("Yun", "school_id", fallback="")
    if schoolUrl != current_school_host or str(schoolId) != current_school_id:
        print("schoolUrl:", schoolUrl)
        print("schoolId:", schoolId)
        config.set("Yun", "school_host", schoolUrl or "")
        config.set("Yun", "school_id", str(schoolId))
        with open(path, 'w', encoding='utf-8') as configfile:
            config.write(configfile)
    else:
        print("当前学校URL和ID与配置文件中的一致，无需更新。")


if __name__ == '__main__':
    schoolName = input("请输入学校名称：")
    url, schoolId = getschool_Url_Id(schoolName)
    if url is not None:
        writeUrlToConfig(url, schoolId)

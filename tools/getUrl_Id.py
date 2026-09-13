# -*- coding: utf-8 -*-
"""学校目录查询：默认只查询公共目录，不读账号配置、不登录、不写配置。

2026-09-13 核实 Android 3.6.6 使用 HTTPS 9011 /api/app/lisshtcool。
该接口返回普通 JSON，与学校业务服务的加密请求不同；实测空 POST 加
version/platform/isApp 即可查询。显式 --write 才更新学校配置。
"""
import argparse
import requests
from urllib.parse import urlsplit
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
SCHOOL_LIST_BASE = "https://sports.aiyyd.com:9011/api"
SCHOOL_LIST_URL = SCHOOL_LIST_BASE + "/app/lisshtcool"


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


def fetch_school_directory(transport=None):
    """一次公共目录请求；无 token/cookie/账号配置，HTTPS 校验保持开启。"""
    send = transport or requests.post
    response = send(url=SCHOOL_LIST_URL, data="", headers={
        "version": "3.6.6", "platform": "android", "isApp": "app",
        "Content-Type": "application/json",
    }, timeout=(8, 15))
    if response.status_code != 200:
        raise yun_http.HttpStatusException(response.status_code, SCHOOL_LIST_URL,
                                            response.text[:200])
    try:
        obj = json.loads(response.text)
    except (TypeError, ValueError) as exc:
        raise yun_http.DecodeException("学校目录不是有效 JSON") from exc
    if not isinstance(obj, dict):
        raise yun_http.DecodeException("学校目录响应不是对象")
    if obj.get("code") != 200:
        raise yun_http.BusinessException(obj.get("code"), str(obj.get("msg", "")), obj)
    rows = obj.get("data")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise yun_http.DecodeException("学校目录 data 不是学校列表")
    return rows


def getschool_Url_Id(schoolName, conf_path=None, transport=None, rng=None):
    """按全称精确匹配；兼容旧调用参数，但查询不读取 conf_path。"""
    matches = [row for row in fetch_school_directory(transport)
               if row.get("schoolName") == schoolName.strip()]
    if not matches:
        print("未找到匹配的学校全称；请 --list 查询，勿套用其他学校地址")
        return None, None
    if len(matches) != 1:
        raise yun_http.DecodeException("学校名称重复，需官方客户端确认，不自动选择")
    row = matches[0]
    url, sid = row.get("schoolUrl"), row.get("schoolId")
    if not isinstance(url, str) or not url.strip() or sid is None or not str(sid).strip():
        raise yun_http.DecodeException("所选学校缺少有效 schoolUrl/schoolId")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise yun_http.DecodeException("所选学校 schoolUrl 不是 HTTP(S) 地址")
    return url.rstrip('/'), sid


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="输出完整学校目录 JSON")
    mode.add_argument("--school", help="学校全称，精确匹配")
    parser.add_argument("--write", action="store_true", help="显式更新配置中的学校地址和 ID")
    parser.add_argument("--config", default=DEFAULT_CONF)
    args = parser.parse_args(argv)
    if args.list:
        if args.write:
            parser.error("--write 必须选择一所学校，不能与 --list 同用")
        print(json.dumps(fetch_school_directory(), ensure_ascii=False, indent=2))
        return 0
    name = args.school or input("请输入学校全称（默认只查询）：")
    url, sid = getschool_Url_Id(name)
    if url is None:
        return 1
    print(json.dumps({"schoolName": name, "schoolId": sid, "schoolUrl": url},
                     ensure_ascii=False, indent=2))
    if args.write:
        writeUrlToConfig(url, sid, conf_path=args.config)
    else:
        print("仅查询：未读取或修改账号配置；确认学校无误后用 --write 更新学校字段。")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

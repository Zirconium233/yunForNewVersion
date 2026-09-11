# -*- coding: utf-8 -*-
"""docs/REVIEW_FACE_PLAN.md §一 阻断问题的回归测试（P1-1/P1-2/P1-3、P2、质量问题）。"""
import argparse
import base64
import configparser
import io
import json
import os
from contextlib import redirect_stdout
from unittest import mock

import pytest

import main as M
import yun_http as yh
from conftest import FIXTURES

CFG = os.path.join(FIXTURES, "test_config.ini")


def _conf():
    c = configparser.ConfigParser()
    c.read(CFG, encoding="utf-8")
    return c


# ---------------------------------------------------------------- P1-1 set_args 加载 DeviceName
def test_set_args_loads_device_name():
    M.set_args(CFG)
    assert M.my_device_name == _conf().get("User", "device_name")
    assert M.my_device_name != ""


def test_main_entry_run_false_with_token_config(tmp_path, monkeypatch):
    """评审 P1-1：必须有调用 main(run=False) 的入口测试，不能只测参数解析。"""
    cfg = _conf()
    cfg.set("User", "token", "VALID-TOKEN-ABCDEF0123456789")
    path = tmp_path / "cfg.ini"
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)
    args = argparse.Namespace(config_path=str(path), task_path=None, dry_run=False,
                              auto_run=True, drift=False, dry_home=None,
                              face_photo=None, face_video=None,
                              face_detection=None, face_mirror=False)
    with mock.patch.object(M, "parse_args", return_value=args):
        M.main(run=False)  # 旧版在这里 TypeError: NoneType 拼接


# ---------------------------------------------------------------- P1-2 登录后同步内存客户端
def test_login_updates_client_and_first_request(capsys):
    M.set_args(CFG)
    assert M._CLIENT.profile.token == _conf().get("User", "token")  # fixture token
    login_result = ("NEW-TOKEN-XYZ", "9000000000000002", "AfterLoginPhone",
                    "9000000000000002", "13")
    with mock.patch.object(M, "noTokenLogin", return_value=login_result):
        result = M.noTokenLogin(CFG)
        M.apply_login_result(*result, conf_path=CFG)
    prof = M.default_client().profile
    assert prof.token == "NEW-TOKEN-XYZ"
    assert prof.device_id == "9000000000000002"
    assert prof.device_name == "AfterLoginPhone"
    assert M.my_token == "NEW-TOKEN-XYZ"

    # 登录后首个请求：头部必须携带新 token；输出不得含明文 token（脱敏断言）
    fake = yh.FakeTransport(lambda router, env: {"code": 200, "msg": "ok", "data": None},
                            sm2box=yh.SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY),
                            fallback_key_b64=yh.generate_sm4())
    M._CLIENT.transport = fake
    M._CLIENT.base_url = M.my_host
    with redirect_stdout(io.StringIO()):
        M.default_post("/run/getHomeRunInfo", "{}")
    assert fake.calls, "登录后必须真的发出了首个请求"
    assert fake.calls[0]["headers"]["token"] == "NEW-TOKEN-XYZ"
    out = capsys.readouterr().out
    assert "NEW-TOKEN-XYZ" not in out


def test_apply_login_result_syncs_school_host(tmp_path):
    M.set_args(CFG)
    cfg = _conf()
    cfg.set("Yun", "school_host", "http://new-school.invalid:9000/api")
    p = tmp_path / "cfg.ini"
    with open(p, "w", encoding="utf-8") as f:
        cfg.write(f)
    old_base = M._CLIENT.base_url
    M.apply_login_result("T", "D", "N", "U", "14", conf_path=str(p))
    assert M._CLIENT.base_url == "http://new-school.invalid:9000/api"
    assert M._CLIENT.base_url != old_base
    assert M.my_host == "http://new-school.invalid:9000/api"


# ---------------------------------------------------------------- P1-3 canSport 字符串 + recordId
def _yun_with_start(data, status="N", face_runner=None):
    M.set_args(CFG)
    responder = {"/run/start": {"code": 200, "msg": "ok", "data": data}}

    def r(router, env):
        for k, v in responder.items():
            if router.endswith(k):
                return v
        return {"code": 200, "msg": "ok", "data": None}
    fake = yh.FakeTransport(r, sm2box=yh.SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY),
                            fallback_key_b64=yh.generate_sm4())
    client = yh.YunClient(M.build_profile(M._CONF), base_url=M.my_host, transport=fake,
                          sleep=lambda s: None)
    home = {"code": 200, "data": {"cralist": [{
        "id": 1, "schoolId": "100", "raType": "T1", "raRunArea": "A",
        "raDislikes": 1, "raSingleMileageMin": 6.0, "raSingleMileageMax": 7.0,
        "raCadenceMin": 120, "raCadenceMax": 190, "runFaceStatus": status,
        "points": "117.2,31.7|117.3,31.8"}]}}
    return M.Yun_For_New(auto_generate_task=False, client=client, home_info=home,
                         face_runner=face_runner)


def test_cansport_string_N_rejected_with_record_id():
    yun = _yun_with_start({"recordStartTime": "2000-01-01 00:00:00", "id": 900001,
                           "studentId": "U", "faceTime": 0, "canSport": "N",
                           "warnContent": "配速异常"})
    with pytest.raises(yh.RunNotPermittedError) as ei:
        yun.start()
    # recordId 已下发也必须留存在异常上（“已开始但不可继续”）
    assert ei.value.record_id == 900001
    assert yun.crsRunRecordId == 900001
    assert "配速异常" in str(ei.value)


def test_cansport_not_boolean_true():
    """canSport="Y" 与缺失都不等于拒绝；"N" 才是拒绝（TextUtils.equals("N") 语义）。"""
    for can in ("Y", "", None):
        yun = _yun_with_start({"recordStartTime": "s", "id": 7, "studentId": "U",
                               "faceTime": 0, "canSport": can})
        yun.start()  # 不抛
        assert yun.crsRunRecordId == 7


# ---------------------------------------------------------------- P2-5 faceTime 不是启用开关
def test_face_time_positive_on_N_task_does_not_stop(capsys):
    yun = _yun_with_start({"recordStartTime": "s", "id": 8, "studentId": "U",
                           "faceTime": 120, "canSport": "Y"})
    yun.start()  # runFaceStatus=N：faceTime 只是窗口参数，APK 不启用人脸
    assert yun.faceTime == 120
    assert "不启用" in capsys.readouterr().out


def test_face_time_floor_10():
    yun = _yun_with_start({"recordStartTime": "s", "id": 8, "studentId": "U",
                           "faceTime": 5, "canSport": "Y"})
    yun.start()
    assert yun.faceTime == 10  # SportRunMapActivity.java:787-788


def test_Y_task_requires_random_list_not_default_pass():
    yun = _yun_with_start({"recordStartTime": "s", "id": 9, "studentId": "U",
                           "faceTime": 30, "canSport": "Y"},
                          status="Y", face_runner=object())
    with pytest.raises(yh.FaceRequiredError):
        yun.start()


# ---------------------------------------------------------------- P2-4 登录身份连续性
def test_login_header_identity_continuity(tmp_path, monkeypatch):
    import tools.Login as L
    cfg = _conf()
    cfg.set("User", "uuid", "OLD-UUID-VALUE")
    cfg.set("User", "device_id", "7777000011112222")
    cfg.set("User", "device_name", "ContinuityPhone")
    cfg.set("User", "sys_edition", "13")
    cfg.set("User", "sys_version", "")
    cfg_path = tmp_path / "login_cfg.ini"
    with open(cfg_path, "w", encoding="utf-8") as f:
        cfg.write(f)

    def responder(router, envelope):
        return {"code": 200, "msg": "ok", "data": {"token": "LK-TOKEN"}}
    fake = yh.FakeTransport(
        responder,
        fixed_pair=(cfg.get("Yun", "cipherkeyencrypted"), cfg.get("Yun", "cipherkey")))
    recorded = []

    def transport(url, data, headers, timeout):
        recorded.append(dict(headers))
        return fake(url, data, headers, timeout)

    def fake_school(schoolName, conf_path=None, transport=None, rng=None):
        return cfg.get("Yun", "school_host"), cfg.get("Yun", "school_id")
    monkeypatch.setattr(L, "getschool_Url_Id", fake_school)
    out = io.StringIO()
    with redirect_stdout(out):
        result = L.Login.main(conf_path=str(cfg_path), transport=transport)
    assert result is not None, out.getvalue()
    token, device_id, device_name, uuid_value, sys_edition = result
    assert recorded, "登录必须发出请求"
    hd = recorded[0]
    # 头部 deviceId 与返回并写回的 DeviceId 一致；sysVersion 反映实际使用的系统版本
    assert hd["deviceId"] == device_id == "7777000011112222"
    assert hd["deviceName"] == device_name == "ContinuityPhone"
    assert hd["sysVersion"] == sys_edition == "13"
    assert hd["token"] == ""  # 登录前不携带旧 token


# ---------------------------------------------------------------- P2-6 默认随机 UUID（在 test_yun_http 里断言细节）
def test_set_args_default_client_random_uuid(loaded_cfg=None):
    M.set_args(CFG)
    assert M._CLIENT.legacy_uuid is False
    c1 = M._CLIENT.new_context()
    c2 = M._CLIENT.new_context()
    assert c1.uuid != c2.uuid


# ---------------------------------------------------------------- default_post headers 语义
def test_default_post_headers_rejected_on_signed_path(loaded_cfg=None):
    M.set_args(CFG)
    with pytest.raises(ValueError):
        M.default_post("/run/x", "{}", headers={"X": "1"}, gen_sign=True)


# ---------------------------------------------------------------- 错误消息不泄漏响应片段
def test_http_error_message_redacted():
    def transport(url, data, headers, timeout):
        class R:
            status_code = 502
            text = '{"msg":"internal","token":"SECRETECHO123456789"}'
        return R()
    client = yh.YunClient(yh.DeviceProfile(md5key="k", public_key=M.PUBLIC_KEY),
                          base_url="http://x.invalid", transport=transport)
    with pytest.raises(yh.HttpStatusException) as ei:
        client.post("/run/finish", "{}")
    assert "SECRETECHO123456789" not in str(ei.value)
    assert "502" in str(ei.value)


def test_transport_error_keeps_unknown_state_wording():
    import requests

    def transport(url, data, headers, timeout):
        raise requests.ReadTimeout("read timeout")
    client = yh.YunClient(yh.DeviceProfile(md5key="k", public_key=M.PUBLIC_KEY),
                          base_url="http://x.invalid", transport=transport)
    with pytest.raises(yh.HttpStatusException) as ei:
        client.post("/run/finish", "{}")
    assert "结果未知" in str(ei.value)
    assert isinstance(ei.value, yh.TransportOutcomeUnknown)

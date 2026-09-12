# -*- coding: utf-8 -*-
"""线上可观测字段对齐测试（服务器视角审查的回归护栏，全部离线）。

依据 3.6.6 反编译取证：RetrofitService.java:96-110（头部/gzip）、
UpPointsModel/UpPointModel（Gson serializeNulls、声明序、类型）、
SportRunMapActivity.P1():2535-2600（finish 插入序）、startRun():4820（start）。
只证明“字段集合/类型/取值格式与 APK 一致”，不证明服务器接受。
"""
import gzip
import json
import os
from urllib.parse import urlparse

import pytest

import main as M
import yun_http as yh
from conftest import FIXTURES

CFG = os.path.join(FIXTURES, "test_config.ini")
TASKS = os.path.join(FIXTURES, "tasks")


@pytest.fixture()
def loaded():
    M.set_args(CFG)
    return M


def _home(**over):
    entry = {
        "id": 900001, "schoolId": "100", "raType": "T1", "raRunArea": "A",
        "raDislikes": 2, "raSingleMileageMin": 6.0, "raSingleMileageMax": 7.0,
        "raCadenceMin": 120, "raCadenceMax": 190, "runFaceStatus": "N",
        "points": "117.200001,31.770001|117.200101,31.770101",
    }
    entry.update(over)
    return {"msg": "ok", "code": 200, "data": {"cralist": [entry]}}


def _responder(router, envelope):
    if router.endswith("/run/start"):
        return {"code": 200, "msg": "ok",
                "data": {"recordStartTime": "2000-01-01 00:00:00",
                         "id": 42, "studentId": "U1", "faceTime": 0,
                         "canSport": "Y"}}
    if router.endswith("/run/isStandard"):
        return {"code": 200, "data": {"isStandard": "Y", "isCheat": "N"}}
    return {"code": 200, "msg": "ok", "data": None}


def _client():
    fake = yh.FakeTransport(_responder,
                            sm2box=yh.SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY))
    client = yh.YunClient(M.build_profile(M._CONF), base_url=M.my_host,
                          transport=fake, sleep=lambda s: None)
    return fake, client


def _calls(fake, suffix):
    return [c for c in fake.calls if urlparse(c["url"]).path.endswith(suffix)]


# ------------------------------------------------------------ HTTP 头部
def test_request_headers_exact_set_and_order():
    p = yh.DeviceProfile(token="T0", device_id="d" * 64,
                         device_name="Xiaomi(M2011K2C)", app_edition="3.6.6",
                         sys_version="14", platform="android")
    ctx = yh.RequestContext(uuid="U", utc="1700000000", sign="s", sm4_key_b64="k")
    h = ctx.headers(p)
    assert list(h) == ["Content-Type", "token", "isApp", "deviceId", "deviceName",
                       "version", "sysVersion", "platform", "uuid", "utc", "sign"]
    assert h["Content-Type"] == "application/json"  # APK 字面量，无 charset 后缀
    assert h["isApp"] == "app" and h["platform"] == "android"
    assert "Accept" not in h and "Connection" not in h and "User-Agent" not in h


def test_session_headers_okhttp_equivalent(monkeypatch):
    monkeypatch.setattr(yh, "_SESSION", None)
    s = yh._get_session()
    assert s.headers.get("User-Agent") == "okhttp/4.9.1"  # okhttp3/internal/Util.java:102
    assert s.headers.get("Accept-Encoding") == "gzip"
    assert "Accept" not in s.headers      # OkHttp 不发 Accept
    assert "Connection" not in s.headers  # OkHttp HTTP/1.1 不发 Connection
    assert s.cookies is not None          # 持久 CookieJar（对齐 CookiesManager）


# ------------------------------------------------------------ gzip 容器
def test_gzip_apk_header_bytes_match_jdk():
    raw = json.dumps({"x": 1}).encode()
    out = yh.gzip_apk(raw)
    assert out[:10] == bytes.fromhex("1f8b08000000000000ff")
    assert gzip.decompress(out) == raw
    default = gzip.compress(raw)
    assert (default[4:8] != b"\x00\x00\x00\x00") or (default[9] != 0xFF)


# ------------------------------------------------------------ split 体
def test_split_body_matches_uppointsmodel(loaded):
    fake, client = _client()
    yun = M.Yun_For_New(auto_generate_task=False, client=client, home_info=_home())
    yun.start()
    yun.do_by_points_map(path=TASKS, random_choose=True, isDrift=False)
    body = _calls(fake, "/run/splitPointCheating")[0]["business"]
    assert "_raw" not in body and "_undecodable" not in body
    assert list(body) == list(M._SPLIT_BODY_ORDER)
    assert "time" not in body  # 旧脚本的 "time" 收敛为 APK 的 "times"
    assert isinstance(body["times"], int) and not isinstance(body["times"], bool)
    assert isinstance(body["StepNumber"], int)
    for key in ("mileage", "runSteps", "speeds", "strides"):
        assert isinstance(body[key], float), key
    for key in ("crsRunRecordId", "schoolId", "userName"):
        assert isinstance(body[key], str), key
    assert body["b"] is None and body["c"] is None  # serializeNulls 显式 null
    assert body["a"] == 0 and body["simulateNum"] == 0
    assert body["orientationNum"] == 0
    km = body["mileage"] / 1000.0
    if body["times"] > 0 and body["mileage"] > 10.0:
        assert body["speeds"] == pytest.approx((body["times"] / 60.0) / km)
    if body["StepNumber"]:
        assert body["strides"] == pytest.approx(body["mileage"] / body["StepNumber"])
    p0 = body["cardPointList"][0]
    assert list(p0) == list(M._POINT_FIELD_ORDER)
    assert isinstance(p0["runMileage"], float) and isinstance(p0["runStep"], int)
    assert isinstance(p0["runTime"], int) and isinstance(p0["speed"], str)
    assert isinstance(p0["isMock"], bool) and isinstance(p0["ts"], str)
    assert "id" not in p0 and "runRecordId" not in p0


def test_projection_crops_and_warns_once(capsys):
    M._dropped_point_keys.clear()
    pt = {"id": 3, "runRecordId": 9, "point": "1,2", "runMileage": "12.5",
          "runTime": "7", "runStep": "10", "speed": 0.0, "ts": "1",
          "isFence": "Y", "runStatus": 1}
    out = M._project_card_point(pt)
    assert out == M._project_card_point(pt)
    # 第一次投影：id、runRecordId 两个新键各提示一次；第二次同一批键不再提示
    assert capsys.readouterr().out.count("[wire]") == 2
    assert out["runMileage"] == 12.5 and out["runStep"] == 10
    assert out["speed"] == "0.00"


def test_build_split_body_derived_numbers():
    pts = [{"point": "1,2", "runMileage": 0.0, "runTime": 0, "runStep": 0,
            "speed": "0.00", "ts": "1", "isFence": "Y", "isMock": False,
            "runStatus": "1"},
           {"point": "1,2", "runMileage": 1000.0, "runTime": 600, "runStep": 0,
            "speed": "1.67", "ts": "2", "isFence": "Y", "isMock": False,
            "runStatus": "1"}]
    b = M._build_split_body(42, "U1", 100, pts, 1.0)
    assert b["crsRunRecordId"] == "42" and b["schoolId"] == "100"
    assert b["mileage"] == 1000.0 and b["times"] == 600
    assert b["StepNumber"] == 1000
    assert b["speeds"] == pytest.approx(10.0)    # (600/60)/(1km)
    assert b["runSteps"] == pytest.approx(100.0)  # 1000 步/10min
    assert b["strides"] == pytest.approx(1.0)
    # 返修 R4：该用例走"点列步数全零→按里程/步幅合成"契约——cardPointList 的
    # 累计步数必须与 StepNumber 同包自洽（差值），而不是只冻结汇总快照。
    assert [p["runStep"] for p in b["cardPointList"]] == [0, 1000]
    assert b["StepNumber"] == b["cardPointList"][-1]["runStep"] \
        - b["cardPointList"][0]["runStep"]


# ------------------------------------------------------------ start / finish 体
def test_start_body_all_strings(loaded):
    fake, client = _client()
    M.Yun_For_New(auto_generate_task=False, client=client,
                  home_info=_home()).start()
    body = _calls(fake, "/run/start")[0]["business"]
    assert body == {"raRunArea": "A", "raType": "T1", "raId": "900001"}


def test_finish_body_p1_order_and_types(loaded):
    fake, client = _client()
    yun = M.Yun_For_New(auto_generate_task=False, client=client, home_info=_home())
    yun.start()
    yun.do_by_points_map(path=TASKS, random_choose=True, isDrift=False)
    yun.finish_by_points_map()
    body = _calls(fake, "/run/finish")[0]["business"]
    assert list(body) == ["manageList", "recordMileage", "recodeCadence",
                          "recodePace", "deviceName", "sysEdition", "appEdition",
                          "raIsStartPoint", "raIsEndPoint", "raRunArea",
                          "recodeDislikes", "raId", "raType", "id", "duration",
                          "recordStartTime", "remake"]
    assert all(isinstance(v, str) for k, v in body.items() if k != "manageList")
    assert isinstance(body["manageList"], list)  # 唯一非字符串值（P1 里是 JSONArray）
    assert body["remake"] == "0|{}"  # BaseCheckUtil 干净设备的 "score|evidence"
    assert body["duration"].isdigit()
    assert body["id"] == "42"


def test_sys_edition_prefixed_once():
    old_v, old_e = M.my_sys_version, M.my_sys_edition
    try:
        M.my_sys_version, M.my_sys_edition = "14", ""
        assert M._sys_edition_field() == "Android_14"
        M.my_sys_version, M.my_sys_edition = "Android_13", ""
        assert M._sys_edition_field() == "Android_13"  # 已带前缀不重复
        M.my_sys_version, M.my_sys_edition = "", ""
        assert M._sys_edition_field() == ""
    finally:
        M.my_sys_version, M.my_sys_edition = old_v, old_e


def test_manage_list_empty_omits_key():
    body = M._build_finish_body("2.20", "150", "5.50", 2, [], "A", "1", "T1",
                                "42", 480, "t")
    assert "manageList" not in body  # P1:2560 空则不写键
    body2 = M._build_finish_body("2.20", "150", "5.50", 2,
                                 [{"point": "1,2", "marked": "Y", "index": "0"}],
                                 "A", "1", "T1", "42", 480, "t")
    assert body2["manageList"] == [{"point": "1,2", "marked": "Y", "index": 0}]
    assert isinstance(body2["recodeCadence"], str)




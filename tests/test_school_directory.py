import json
from types import SimpleNamespace

import pytest
from tools import getUrl_Id as directory
from yun_http import BusinessException, DecodeException


def transport_for(obj):
    return lambda **kw: SimpleNamespace(status_code=200, text=json.dumps(obj))


def test_public_directory_never_reads_config_or_sends_credentials(monkeypatch):
    monkeypatch.setattr(directory, "load_conf", lambda *a: pytest.fail("config read"))
    calls = []
    def send(**kw):
        calls.append(kw)
        return transport_for({"code": 200, "data": [
            {"schoolName": "学校甲", "schoolId": "1",
             "schoolUrl": "https://example.invalid:8010/m-api/"}]})(**kw)
    assert directory.getschool_Url_Id("学校甲", conf_path="does-not-exist",
                                      transport=send) == (
        "https://example.invalid:8010/m-api", "1")
    assert len(calls) == 1
    assert calls[0]["url"] == "https://sports.aiyyd.com:9011/api/app/lisshtcool"
    assert calls[0]["data"] == ""
    assert calls[0]["headers"] == {"version": "3.6.6", "platform": "android",
                                     "isApp": "app", "Content-Type": "application/json"}


@pytest.mark.parametrize("obj,error", [
    ({"code": 500, "msg": "版本过低"}, BusinessException),
    ({"code": 200, "data": None}, DecodeException),
    ({"code": 200, "data": ["bad"]}, DecodeException),
    ([], DecodeException),
])
def test_directory_rejects_invalid_responses(obj, error):
    with pytest.raises(error):
        directory.fetch_school_directory(transport_for(obj))


def test_selection_uses_school_name_not_duplicate_id():
    rows = [{"schoolName": "甲", "schoolId": "233", "schoolUrl": "http://192.168.1.1:8080/"},
            {"schoolName": "乙", "schoolId": "233", "schoolUrl": "https://example.invalid:8000/"}]
    send = transport_for({"code": 200, "data": rows})
    assert directory.getschool_Url_Id("乙", transport=send)[0] == "https://example.invalid:8000"
    assert directory.getschool_Url_Id("不存在", transport=send) == (None, None)
    with pytest.raises(DecodeException):
        directory.getschool_Url_Id("甲", transport=transport_for({"code": 200, "data": [rows[0]] * 2}))


def test_cli_query_never_writes(monkeypatch):
    monkeypatch.setattr(directory, "getschool_Url_Id", lambda *a: ("https://example.invalid", "1"))
    monkeypatch.setattr(directory, "writeUrlToConfig", lambda *a, **kw: pytest.fail("config write"))
    assert directory.main(["--school", "甲"]) == 0


def test_explicit_write_preserves_other_config_fields(tmp_path, monkeypatch):
    path = tmp_path / "test.ini"
    path.write_text("[Yun]\nschool_host=http://old.invalid\nschool_id=2\nschool_login_url=appLogin\n[Login]\nusername=fixture\n", encoding="utf-8")
    monkeypatch.setattr(directory, "getschool_Url_Id", lambda *a: ("https://example.invalid/m-api", "1"))
    assert directory.main(["--school", "甲", "--write", "--config", str(path)]) == 0
    cfg = directory.load_conf(path)
    assert cfg.get("Yun", "school_host") == "https://example.invalid/m-api"
    assert cfg.get("Yun", "school_id") == "1"
    assert cfg.get("Yun", "school_login_url") == "appLogin"
    assert cfg.get("Login", "username") == "fixture"

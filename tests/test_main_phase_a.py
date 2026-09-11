# -*- coding: utf-8 -*-
"""A 阶段 main/history 行为测试：人脸停止门、loader 字段保真、dry-run 假传输、CLI 与路径契约。"""
import argparse
import base64
import gzip
import json
import os
import sys
from urllib.parse import urlparse

import pytest

import main as M
import yun_http as yh
from conftest import FIXTURES

CFG = os.path.join(FIXTURES, "test_config.ini")
TASKS = os.path.join(FIXTURES, "tasks")


def _home(**over):
    entry = {
        "id": 900001, "schoolId": "100", "raType": "T1", "raRunArea": "A",
        "raDislikes": 2, "raSingleMileageMin": 6.0, "raSingleMileageMax": 7.0,
        "raCadenceMin": 120, "raCadenceMax": 190, "runFaceStatus": "N",
        "points": "117.200001,31.770001|117.200101,31.770101",
    }
    if "runFaceStatus" in over and over["runFaceStatus"] is None:
        over = dict(over)
        del entry["runFaceStatus"]
        over.pop("runFaceStatus")
    entry.update(over)
    return {"msg": "ok", "code": 200, "data": {"cralist": [entry]}}


def _responder(router, envelope):
    if router.endswith("/run/start"):
        return {"code": 200, "msg": "ok", "data": {"recordStartTime": "2000-01-01 00:00:00",
                                                   "id": 42, "studentId": "U1", "faceTime": 0,
                                                   "canSport": True}}
    return {"code": 200, "msg": "ok", "data": None}


@pytest.fixture()
def loaded():
    M.set_args(CFG)
    return M


def make_client(profile=None, responder=_responder):
    fake = yh.FakeTransport(responder, sm2box=yh.SM2Box(M.PUBLIC_KEY, M.PRIVATE_KEY))
    return fake, yh.YunClient(profile or M.build_profile(M._CONF),
                              base_url=M.my_host, transport=fake,
                              sleep=lambda s: None)


# ---------------------------------------------------------------- 人脸停止门
def test_face_gate_Y_stops(loaded):
    _, client = make_client()
    with pytest.raises(yh.FaceRequiredError):
        M.Yun_For_New(auto_generate_task=False, client=client, home_info=_home(runFaceStatus="Y"))


def test_face_gate_missing_or_unknown_stops(loaded):
    _, client = make_client()
    with pytest.raises(yh.FaceRequiredError):  # 缺失 => 不假设已通过
        M.Yun_For_New(auto_generate_task=False, client=client, home_info=_home(runFaceStatus=None))
    with pytest.raises(yh.FaceRequiredError):  # 未知值
        M.Yun_For_New(auto_generate_task=False, client=client, home_info=_home(runFaceStatus="W"))


def test_face_gate_N_passes_without_network(loaded):
    fake, client = make_client()
    yun = M.Yun_For_New(auto_generate_task=False, client=client, home_info=_home())
    assert yun.runFaceStatus == "N"
    assert fake.calls == [] and client.sent == []  # 构造函数未发任何请求


# ---------------------------------------------------------------- loader 字段保真（评审 2.2：不得丢 runStep）
def test_loader_preserves_run_step(loaded):
    fake, client = make_client()
    yun = M.Yun_For_New(auto_generate_task=False, client=client, home_info=_home())
    yun.start()
    yun.do_by_points_map(path=TASKS, random_choose=True, isDrift=False)
    splits = [c for c in fake.calls
              if urlparse(c["url"]).path.endswith("/run/splitPointCheating")]
    # 二返修 S1：尾批（2 点）暂存，批末不再无条件发送——do 后只有 1 个 split
    assert len(splits) == 1
    body = json.loads(gzip.decompress(
        yh.decrypt_sm4(splits[0]["envelope"]["content"],
                       base64.b64decode(splits[0]["sm4_key_b64"]))).decode())
    p0 = body["cardPointList"][0]
    assert p0["runStep"] == 0
    assert p0["runMileage"] == 0.0
    assert "ts" in p0
    # 结束链：状态检查 → 尾批补发 → finish（完整顺序断言）
    yun.finish_by_points_map()
    paths = [urlparse(c["url"]).path for c in fake.calls]
    idx_split = [i for i, p in enumerate(paths)
                 if p.endswith("/run/splitPointCheating")]
    idx_std = [i for i, p in enumerate(paths) if p.endswith("/run/isStandard")]
    idx_fin = [i for i, p in enumerate(paths) if p.endswith("/run/finish")]
    assert len(idx_split) == 2 and len(idx_std) == 1 and len(idx_fin) == 1
    assert idx_split[0] < idx_std[0] < idx_split[1] < idx_fin[0]
    tail = json.loads(gzip.decompress(
        yh.decrypt_sm4(fake.calls[idx_split[1]]["envelope"]["content"],
                       base64.b64decode(
                           fake.calls[idx_split[1]]["sm4_key_b64"]))).decode())
    assert len(tail["cardPointList"]) == 2   # 12 点 / split_count 10 => 10 + 2


# ---------------------------------------------------------------- dry-run（§3-A.5）
def test_dry_run_uses_fake_transport_only(loaded, capsys):
    args = argparse.Namespace(dry_run=True, drift=False,
                              dry_home=os.path.join(M.PROJECT_ROOT, "dry_run_home.json"))
    client = M.run_dry(CFG, TASKS, args)
    fake = client.transport
    paths = [urlparse(c["url"]).path for c in fake.calls]
    assert paths[0].endswith("/run/getHomeRunInfo")
    assert any(p.endswith("/run/start") for p in paths)
    assert sum(1 for p in paths if p.endswith("/run/splitPointCheating")) == 2
    assert any(p.endswith("/run/finish") for p in paths)
    assert all(c["url"].startswith("http://school.invalid:8080") for c in fake.calls)
    out = capsys.readouterr().out
    assert "[dry-run 完成]" in out
    # A.4：dry-run 输出不落敏感字段
    for secret in ("TESTTOKEN0123456789abcdef", "JXhWGZjmhhXN+nt8nLpNxA==",
                   "TEST_PASSWORD_LOCAL_ONLY", "BGfbsG9EkXz5KeCva8E0MisB"):
        assert secret not in out


def test_dry_run_face_gate_stops_loudly(loaded, tmp_path, capsys):
    home = _home(runFaceStatus="Y")
    p = tmp_path / "home.json"
    p.write_text(json.dumps(home), encoding="utf-8")
    args = argparse.Namespace(dry_run=True, drift=False, dry_home=str(p))
    with pytest.raises(yh.FaceRequiredError):
        M.run_dry(CFG, TASKS, args)


# ---------------------------------------------------------------- CLI 兼容（评审 2.1.3）
@pytest.mark.parametrize("argv,expect", [
    (["prog", "-f", "a.ini", "-t", "t_dir", "-a", "-d"],
     {"config_path": "a.ini", "task_path": "t_dir", "auto_run": True, "drift": True}),
    (["prog", "--config_path", "a.ini", "--task_path", "t", "--auto_run", "--drift"],
     {"config_path": "a.ini", "task_path": "t", "auto_run": True, "drift": True}),
])
def test_cli_old_flags_parse(monkeypatch, argv, expect):
    monkeypatch.setattr(sys, "argv", argv)
    ns = vars(M.parse_args())
    for k, v in expect.items():
        assert ns[k] == v


def test_cli_new_aliases_only(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run"])
    assert M.parse_args().dry_run is True
    monkeypatch.setattr(sys, "argv", ["prog", "--dry_run"])
    assert M.parse_args().dry_run is True


def test_history_cli_flags_parse():
    import history
    ns = history.build_arg_parser().parse_args(["--conf_path", "c.ini", "--history_path", "h"])
    assert ns.conf_path == "c.ini" and ns.history_path == "h"


# ---------------------------------------------------------------- 路径契约（评审 2.1.6）
def test_resolve_cli_path_is_caller_relative(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "INITIAL_CWD", str(tmp_path))
    assert M.resolve_cli_path("cfg/x.ini") == os.path.normpath(str(tmp_path / "cfg" / "x.ini"))
    assert M.resolve_cli_path(str(tmp_path / "abs.ini")) == str(tmp_path / "abs.ini")
    assert M.project_resource("config.ini") == os.path.normpath(
        os.path.join(M.PROJECT_ROOT, "config.ini"))


def test_set_args_missing_config_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "INITIAL_CWD", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        M.set_args("nope.ini")  # 旧行为：configparser 静默空读；现在显式报错

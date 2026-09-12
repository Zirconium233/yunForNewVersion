# -*- coding: utf-8 -*-
"""FINAL_REVIEW_REWORK_16462a8 返修回归（R1–R6）。

每个用例把复现脚本里的一个反例翻成对新正确行为的断言；全部离线（不建 socket、
不登录、不写配置）。原则：只更新测试去断言"新正确行为"，不迁就旧快照。
"""
import argparse
import base64
import configparser
import io
import json
import os
import time

import pytest
from PIL import Image

import main as M
import yun_face as yf
import yun_http as yh

CFG = os.path.join(os.path.dirname(__file__), "..", "config.ini")
BOX_PASS = (100.0, 120.0, 260.0, 360.0)
PTS_PASS = [(130.0, 180.0), (230.0, 180.0), (180.0, 240.0),
            (140.0, 300.0), (220.0, 300.0)]
PW, PH = 400, 400


def home_fixture(run_face="Y"):
    return {"code": 200, "msg": "fixture", "data": {"cralist": [{
        "id": 900001, "schoolId": "100", "raType": "T1", "raRunArea": "A",
        "raDislikes": 1, "raSingleMileageMin": 6.0, "raSingleMileageMax": 7.0,
        "raCadenceMin": 120, "raCadenceMax": 190, "runFaceStatus": run_face,
        "points": "117.2,31.7|117.3,31.8",
    }]}}


def task_dict(pts, duration=480):
    return {"code": 200, "msg": "ok", "data": {
        "duration": duration, "recordMileage": "2.20", "recodeCadence": "150",
        "recodePace": "5.50", "recodeDislikes": 2,
        "manageList": [{"point": "117.2,31.7", "marked": "Y", "index": "0"}],
        "pointsList": pts}}


def make_pts(n=12, step_m=200.0):
    return [{"point": f"117.{200000 + i:06d},31.77", "speed": "5.50",
             "runMileage": i * step_m, "runTime": i * 40, "runStep": 0}
            for i in range(n)]


def write_task(tdir, data):
    tdir.mkdir(exist_ok=True)
    p = tdir / "tasklist_0.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class StubClient:
    """最小 YunClient 替身：记录调用序列，可注入路由级拒绝/耗时行为。"""

    def __init__(self, face_time=10, random_list=(), behavior=None, mono_step=0.0):
        self.calls = []                    # [(router, kwargs)]
        self.face_time = face_time
        self.random_list = list(random_list)
        self.behavior = behavior or {}     # router 后缀 → callable(self, router, kw)→dict|raise
        self._t = [1000.0]
        self.mono_step = mono_step
        self.timeout = (5.0, 15.0)

    def now(self):
        return int(self._t[0])

    def mono(self):
        return self._t[0]

    def advance(self, s):
        self._t[0] += s

    def sleep(self, s):                        # no-op（打表等待不占真实时间）
        self.calls.append(("__sleep__", {"s": s}))

    def post(self, router, json_text="", **kw):   # 兼容鸭子路线
        return json.dumps(self.post_json(router, json_text, **kw))

    def post_json(self, router, json_text="", raw_bytes=None,
                  raise_on_business_code=False, **kw):
        self.calls.append((router, dict(kw, raw_bytes=raw_bytes)))
        if self._t and self.mono_step:
            self.advance(self.mono_step)
        for suf, fn in self.behavior.items():
            if router.endswith(suf):
                out = fn(self, router, kw)
                if out is not None:
                    break
        else:
            out = self._default(router)
        if raise_on_business_code and out.get("code") != 200:
            raise yh.BusinessException(out.get("code"), str(out.get("msg", "")), out)
        return out

    def _default(self, router):
        if router.endswith("/run/start"):
            return {"code": 200, "msg": "ok", "data": {
                "recordStartTime": "2026-01-01 00:00:00", "id": 900001,
                "studentId": "S1", "faceTime": self.face_time,
                "randomList": self.random_list, "canSport": "Y"}}
        if router.endswith("runFaceInfoComparison"):
            return {"code": 200, "msg": "ok",
                    "data": {"status": "Y", "msg": "stub"}}
        if router.endswith("/run/isStandard"):
            return {"code": 200, "data": {"isStandard": "Y", "isCheat": "N"}}
        return {"code": 200, "msg": "ok", "data": None}

    def routers(self):
        return [r for r, _ in self.calls if r != "__sleep__"]


def make_yun(client, face_runner=None, home=None):
    M.set_args(CFG)
    return M.Yun_For_New(auto_generate_task=False, client=client,
                         home_info=home or home_fixture(),
                         face_runner=face_runner)


def photo_pair(tmp_path):
    """绑定标注的照片对（R6 语义下的合法输入）。"""
    import hashlib
    img = Image.new("RGB", (400, 500), "gray")
    photo = tmp_path / "selfie.jpg"
    b = io.BytesIO(); img.save(b, "JPEG", quality=95)
    photo_bytes = b.getvalue()
    photo.write_bytes(photo_bytes)
    det_json = tmp_path / "det.json"
    det_json.write_text(json.dumps({
        "box": list(BOX_PASS), "points": [list(p) for p in PTS_PASS],
        "score": 0.9, "space": "image",
        "source_sha256": hashlib.sha256(photo_bytes).hexdigest(),
        "bind_apply": {"after_exif": True, "mirrored": False},
    }), encoding="utf-8")
    return photo, det_json


# ================================================================ R1
class TestR1WindowEvents:
    def test_first_batch_crossing_triggers(self):
        # 反例翻转：首批批末 900m 也必须触发 500m 窗口（起点基线规则）
        t = yf.WindowTrigger([yf.FaceWindow(id_str="500", window_m=500)])
        w = t.on_distance(0.9)
        assert w is not None and w.is_show

    def test_boundary_exact_and_multiwindow(self):
        t = yf.WindowTrigger([yf.FaceWindow(id_str="500", window_m=500),
                              yf.FaceWindow(id_str="1000", window_m=1000)])
        assert t.on_distance(0.0) is None                 # 起点=基线，无跨越
        w = t.on_distance(0.5)                            # 点恰在边界：算跨越
        assert w is not None and w.id_str == "500"
        t.in_flight = False
        w2 = t.on_distance(1.2)                           # 跨 1000
        assert w2 is not None and w2.id_str == "1000"

    def test_inflight_crossing_not_auto_replayed_but_blocker_flags(self):
        t = yf.WindowTrigger([yf.FaceWindow(id_str="500", window_m=500),
                              yf.FaceWindow(id_str="1000", window_m=1000)])
        t.on_distance(0.0)
        w = t.on_distance(0.6)                            # 触发 500，进入在途
        assert w.id_str == "500" and t.in_flight
        assert t.on_distance(1.4) is None                 # 在途跨越多窗口：APK 同样丢
        t.in_flight = False
        assert t.on_distance(1.4) is None                 # 不回退补触发（与 APK 一致）
        bad = t.incomplete_within(1400)                   # 但结束前完整性检查必须拦截
        assert [w.id_str for w in bad] == ["500", "1000"]  # 500 未确认 + 1000 漏跨

    def test_nonmonotonic_ignored_negative_raises(self):
        t = yf.WindowTrigger([yf.FaceWindow(id_str="1500", window_m=1500)])
        t.on_distance(1.0)
        assert t.on_distance(0.4) is None                 # 忽略，不回退基线
        assert t.on_distance(1.2) is None                 # 1500 仍不该触发
        assert t.on_distance(1.6) is not None
        with pytest.raises(yf.FaceInputError):
            t.on_distance(-0.1)

    def test_real_flow_per_point_advance_two_windows(self, tmp_path):
        # 真实 do_by_points_map：每批 10 点（200m 间隔），500/1500 两窗口都必须
        # 按轨迹点触发（旧实现只喂批末里程；R1 反例的整链版本）
        client = StubClient(face_time=10, random_list=(0.5, 1.5))
        photo, det = photo_pair(tmp_path)
        runner = yf.FaceRunner(yf.PhotoSource(str(photo)),
                               detection=yf.load_detection_json(str(det)),
                               sleep=lambda s: None)
        yun = make_yun(client, face_runner=runner)
        yun.start()
        write_task(tmp_path / "tasks", task_dict(make_pts(12)))
        yun.do_by_points_map(path=str(tmp_path / "tasks"), random_choose=True)
        yun.finish_by_points_map()
        routers = client.routers()
        assert routers.count("/run/splitPointCheating") == 2
        cmp_n = sum(1 for r in routers if r.endswith("runFaceInfoComparison"))
        assert cmp_n == 2                                   # 两窗口各一次比对
        assert sum(1 for r in routers if r.endswith("/run/finish")) == 1
        assert sum(1 for r in routers if r.endswith("/run/isStandard")) == 1
        trig = yun._face_trigger
        assert all(w.compare_success == "Y" for w in trig.windows)

    def test_single_point_batch_still_triggers(self, tmp_path):
        # 一批只有一个点（尾批形态）也必须推进事件
        client = StubClient(face_time=10, random_list=(0.5,))
        photo, det = photo_pair(tmp_path)
        runner = yf.FaceRunner(yf.PhotoSource(str(photo)),
                               detection=yf.load_detection_json(str(det)),
                               sleep=lambda s: None)
        yun = make_yun(client, face_runner=runner)
        yun.start()
        pts = make_pts(10) + [{"point": "117.300000,31.77", "speed": "5.50",
                               "runMileage": 3000.0, "runTime": 999, "runStep": 0}]
        write_task(tmp_path / "tasks", task_dict(pts))
        yun.do_by_points_map(path=str(tmp_path / "tasks"), random_choose=True)
        yun.finish_by_points_map()
        assert any(r.endswith("runFaceInfoComparison") for r in client.routers())

    def test_finish_refused_when_window_unfinished(self, tmp_path):
        # 弹出但比对未成功 → finish 拒绝发送（不得静默 finish）
        # 构造时用 runFaceStatus=N（本用例手动布置窗口状态，不依赖人脸源）
        client = StubClient(face_time=10, random_list=(0.5,))
        yun = make_yun(client, home=home_fixture("N"))
        w = yf.FaceWindow(id_str="500", window_m=500, is_show=True,
                          compare_success="N", reason="比对失败")
        yun._face_trigger = yf.WindowTrigger([w])
        yun._max_mileage_m = 900
        yun.task_map = task_dict(make_pts(1))
        with pytest.raises(M.FaceRunStopError, match="拒绝发送 finish"):
            yun.finish_by_points_map()
        assert not any(r.endswith("/run/finish") for r in client.routers())


    def test_tail_batch_smaller_than_split_count(self, tmp_path):
        # 尾批 < split_count（11 点：尾批 1 点）也要推进并允许 finish
        client = StubClient(face_time=10, random_list=(1.0,))
        photo, det = photo_pair(tmp_path)
        runner = yf.FaceRunner(yf.PhotoSource(str(photo)),
                               detection=yf.load_detection_json(str(det)),
                               sleep=lambda s: None)
        yun = make_yun(client, face_runner=runner)
        yun.start()
        write_task(tmp_path / "tasks", task_dict(make_pts(11)))
        yun.do_by_points_map(path=str(tmp_path / "tasks"), random_choose=True)
        yun.finish_by_points_map()
        routers = client.routers()
        assert routers.count("/run/splitPointCheating") == 2
        assert any(r.endswith("runFaceInfoComparison") for r in routers)


# ================================================================ R2
class TestR2VerifierBudget:
    def _vclock(self, start=0.0):
        t = [start]
        return (lambda: t[0]), t

    def test_late_success_after_deadline_is_discarded(self):
        # 反例翻转：请求耗时 44s 越过 14s 窗口预算 → 不得算成功
        clock, t = self._vclock()

        def attempt(data):
            t[0] += 44.0
            return yf.FaceOutcome("success")

        cfg = yf.VerifierConfig(attempt_fn=attempt)
        v = yf.FaceVerifier(None, 42, cfg=cfg,
                            sleep=lambda s: t.__setitem__(0, t[0] + s),
                            clock=clock, deadline=14.0)
        o = v.run(b"F")
        assert o.state == "expired"
        assert any("late success dropped" in s for s in v.trace)

    def test_network_time_counts_in_pending(self):
        # 等待态每次请求自身耗时计入预算
        clock, t = self._vclock()
        n = {"k": 0}

        def attempt(data):
            n["k"] += 1
            t[0] += 6.0
            return yf.FaceOutcome("transport_failed")

        cfg = yf.VerifierConfig(attempt_fn=attempt, immediate_retries=0,
                                pending_seconds=15)
        v = yf.FaceVerifier(None, 1, cfg=cfg,
                            sleep=lambda s: t.__setitem__(0, t[0] + s),
                            clock=clock)
        o = v.run(b"F")
        assert o.state == "transport_failed" and o.msg == "识别超时(3004)"
        assert n["k"] <= 4          # 网络耗时使等待态提前归零（旧实现 tick 计数会拖长）

    def test_sleep_clamped_to_remaining(self):
        clock, t = self._vclock()
        n = {"k": 0}

        def counting_attempt(data):
            n["k"] += 1
            return yf.FaceOutcome("transport_failed")

        cfg = yf.VerifierConfig(attempt_fn=counting_attempt)
        sleeps = []
        v = yf.FaceVerifier(None, 1, cfg=cfg, sleep=sleeps.append,
                            clock=clock, deadline=0.5)      # 剩余预算 0.5s
        o = v.run(b"F")
        assert o.state == "expired"                          # 首传失败后即截止
        assert max(sleeps) <= 0.5                            # 1s 间隔被裁剪进预算

    def test_session_terminated_midway_stops_all(self):
        clock, t = self._vclock()
        flag = {"t": False}

        def attempt(data):
            flag["t"] = True
            return yf.FaceOutcome("transport_failed")

        cfg = yf.VerifierConfig(attempt_fn=attempt)
        v = yf.FaceVerifier(None, 1, cfg=cfg, sleep=lambda s: None,
                            session_terminated=lambda: flag["t"],
                            clock=clock, deadline=60.0)
        o = v.run(b"F")
        assert o.state == "session_terminated"



class TestR2Flow:
    def test_expired_window_stops_split_and_finish(self, tmp_path):
        # 反例翻转（整链版）：比对请求耗时 40s 超预算 → 该窗口不得记 Y，
        # 后续 split 被守卫拦截，finish 永不调用。
        client = StubClient(face_time=10, random_list=(0.5,))

        def slow_compare(cli_self, router, kw):
            cli_self.advance(40.0)
            return {"code": 200, "msg": "ok",
                    "data": {"status": "Y", "msg": "太迟了"}}

        client.behavior = {"runFaceInfoComparison": slow_compare}
        photo, det = photo_pair(tmp_path)
        runner = yf.FaceRunner(yf.PhotoSource(str(photo)),
                               detection=yf.load_detection_json(str(det)),
                               sleep=lambda s: client.advance(s))
        yun = make_yun(client, face_runner=runner)
        yun.start()
        write_task(tmp_path / "tasks", task_dict(make_pts(12)))
        with pytest.raises(M.FaceRunStopError, match="人脸"):
            yun.do_by_points_map(path=str(tmp_path / "tasks"), random_choose=True)
            yun.finish_by_points_map()
        routers = client.routers()
        assert routers.count("/run/splitPointCheating") == 1     # 超时后未再发下一批
        cmp_seen = [r for r in routers if r.endswith("runFaceInfoComparison")]
        assert len(cmp_seen) == 1                                # 只一次，不轰炸
        assert sum(1 for r in routers if r.endswith("/run/finish")) == 0
        for w in yun._face_trigger.windows:
            assert w.compare_success != "Y"

    def test_start_missing_face_time_stops(self):
        client = StubClient(face_time=None, random_list=(0.5,))
        yun = make_yun(client, face_runner=object())
        with pytest.raises(yh.FaceRequiredError, match="faceTime"):
            yun.start()

    def test_session_terminated_flag_blocks_finish(self, tmp_path):
        yun = make_yun(StubClient(), home=home_fixture("N"))
        yun._face_block = "人脸会话已终止（迟到回调丢弃，自动流程终止）"
        yun.task_map = task_dict(make_pts(1))
        with pytest.raises(M.FaceRunStopError):
            yun.finish_by_points_map()
        with pytest.raises(M.FaceRunStopError):
            yun.split_by_points_map(make_pts(1))



# ================================================================ R3
class TestR3BusinessCode:
    def test_split_rejected_stops_entire_flow(self, tmp_path):
        # 反例翻转：第 2 批被 code=500 拒绝 → 异常停止；无第 3 批、无 finish
        client = StubClient(face_time=10)
        n = {"k": 0}

        def reject(cli_self, router, kw):
            n["k"] += 1
            if n["k"] == 2:
                return {"code": 500, "msg": "服务器拒绝", "data": None}
            return None

        client.behavior = {"/run/splitPointCheating": reject}
        yun = make_yun(client, home=home_fixture("N"))
        yun.start()
        write_task(tmp_path / "tasks", task_dict(make_pts(25)))
        with pytest.raises(yh.BusinessException, match="code=500"):
            yun.do_by_points_map(path=str(tmp_path / "tasks"), random_choose=True)
            yun.finish_by_points_map()
        routers = client.routers()
        assert routers.count("/run/splitPointCheating") == 2   # 被拒后无第 3 批
        assert sum(1 for r in routers if r.endswith("/run/finish")) == 0
        assert yun.crsRunRecordId == 900001                     # recordId 保留
        assert yun._last_confirmed_mileage_m == 1800            # 最后确认位置保留

    def test_duck_client_rejection_raises_same_exception(self):
        # 评审复现形态：只提供 post(文本) 的鸭子客户端 → 同样抛 BusinessException
        class RejectClient:
            def post(self, *a, **kw):
                return '{"code":500,"msg":"no","data":null}'

        yun = make_yun(RejectClient(), home=home_fixture("N"))
        yun.crsRunRecordId, yun.userName, yun.schoolId, yun.strides = 1, "u", "100", 0.8
        with pytest.raises(yh.BusinessException, match="code=500"):
            yun.split_by_points_map(make_pts(2))

    def test_finish_rejected_still_never_reported_success(self, capsys):
        # 二返修 S1：isStandard 先查（200 通过）→ finish 被拒 → 不得出现"受理"；
        # finish 是序列最后一个请求（其后什么都不发）。
        client = StubClient()
        client.behavior = {"/run/finish": lambda c, r, kw: {
            "code": 500, "msg": "记录异常", "data": None}}
        yun = make_yun(client, home=home_fixture("N"))
        yun.crsRunRecordId, yun.task_map = 5, task_dict(make_pts(1))
        yun.recordStartTime = "2026-01-01 00:00:00"
        with pytest.raises(yh.BusinessException):
            yun.finish_by_points_map()
        out = capsys.readouterr().out
        assert "受理" not in out
        rs = client.routers()
        assert sum(1 for r in rs if r.endswith("/run/isStandard")) == 1
        assert sum(1 for r in rs if r.endswith("/run/finish")) == 1
        assert rs.index(next(r for r in rs if r.endswith("/run/isStandard"))) \
            < rs.index(next(r for r in rs if r.endswith("/run/finish")))
        assert rs[-1].endswith("/run/finish")

    def test_isstandard_result_parsed_and_reported(self, capsys):
        # 二返修 S1：isStandard 是结束链前置门。code!=200 → 停止（重抛），
        # 尾批与 finish 一律不发；呈报失败/未知。
        client = StubClient()
        client.behavior = {"/run/isStandard": lambda c, r, kw: {
            "code": 500, "msg": "查询拒绝"}}
        yun = make_yun(client, home=home_fixture("N"))
        yun.crsRunRecordId, yun.task_map = 5, task_dict(make_pts(1))
        yun.recordStartTime = "2026-01-01 00:00:00"
        with pytest.raises(yh.BusinessException):
            yun.finish_by_points_map()
        out = capsys.readouterr().out
        assert "[isStandard] 状态检查失败/未知" in out
        assert "不发送尾批、不发送 finish" in out
        assert not any(r.endswith("/run/finish") for r in client.routers())


# ============================================================ 二返修 S1
class TestS1FinishChainOrder:
    """结束链 = 状态检查 → 尾批 → finish（不再"先 finish 后查询"）。"""

    def _flow(self, behavior=None):
        import main as _m
        client = StubClient()
        if behavior:
            client.behavior = behavior
        yun = make_yun(client, home=home_fixture("N"))
        yun.start()
        yun.task_map = task_dict(make_pts(12))
        pts = list(yun.task_map["data"]["pointsList"])
        sc = _m.split_count
        yun.split_by_points_map(pts[:sc])
        yun._pending_tail_points = pts[sc:]
        return client, yun

    def test_isstandard_http_error_blocks_tail_and_finish(self):
        def boom(c, r, kw):
            raise yh.HttpStatusException(502, "http://x/run/isStandard", "bad gw")
        client, yun = self._flow({"/run/isStandard": boom})
        with pytest.raises(yh.HttpStatusException):
            yun.finish_by_points_map()
        rs = client.routers()
        assert not any(r.endswith("/run/finish") for r in rs)
        assert sum(1 for r in rs if r.endswith("splitPointCheating")) == 1

    def test_isstandard_decode_error_blocks(self):
        def boom(c, r, kw):
            raise yh.DecodeException("响应无法解码")
        client, yun = self._flow({"/run/isStandard": boom})
        with pytest.raises(yh.DecodeException):
            yun.finish_by_points_map()
        rs = client.routers()
        assert not any(r.endswith("/run/finish") for r in rs)
        assert sum(1 for r in rs if r.endswith("splitPointCheating")) == 1

    def test_isstandard_cheat_stops(self, capsys):
        client, yun = self._flow({"/run/isStandard": lambda c, r, kw: {
            "code": 200, "msg": "ok",
            "data": {"isStandard": "N", "isCheat": "Y",
                     "msg": "需复核", "url": "http://x/recheck",
                     "list": [{"a": 1}]}}})
        with pytest.raises(M.FaceRunStopError, match="isCheat=Y"):
            yun.finish_by_points_map()
        rs = client.routers()
        assert not any(r.endswith("/run/finish") for r in rs)
        assert sum(1 for r in rs if r.endswith("splitPointCheating")) == 1
        assert "[isStandard 预检]" in capsys.readouterr().out

    @pytest.mark.parametrize("cheat", [None, "N"])
    def test_standard_display_fields_allow_tail_and_finish(self, cheat):
        data = {"isStandard": "Y", "isCheat": cheat,
                "msg": "本次跑步已满足要求", "url": "https://example.invalid/status.png",
                "list": [{"msg": "至少跑1.5公里", "isStandard": "Y",
                          "url": "https://example.invalid/item.png"}]}
        client, yun = self._flow({"/run/isStandard": lambda c, r, kw:
                                 {"code": 200, "data": data}})
        yun.finish_by_points_map()
        assert [r.rsplit("/", 1)[-1] for r in client.routers()][-3:] == [
            "isStandard", "splitPointCheating", "finish"]

    @pytest.mark.parametrize("data,error", [
        (None, yh.DecodeException), ([], yh.DecodeException),
        ("Y", yh.DecodeException), ({}, M.FaceRunStopError),
        ({"isStandard": "N"}, M.FaceRunStopError),
        ({"isStandard": "unknown"}, M.FaceRunStopError),
        ({"isStandard": True}, M.FaceRunStopError),
        ({"isStandard": "Y", "isCheat": "Y"}, M.FaceRunStopError),
    ])
    def test_unconfirmed_or_cheat_state_never_sends_tail_or_finish(self, data, error):
        client, yun = self._flow({"/run/isStandard": lambda c, r, kw:
                                 {"code": 200, "data": data}})
        tail = list(yun._pending_tail_points)
        with pytest.raises(error):
            yun.finish_by_points_map()
        assert client.routers()[-1].endswith("/run/isStandard")
        assert yun._pending_tail_points == tail

    def test_tail_rejected_means_no_finish(self):
        cnt = {"n": 0}

        def reject2nd(c, r, kw):
            cnt["n"] += 1
            if cnt["n"] >= 2:
                return {"code": 500, "msg": "尾批拒绝", "data": None}
            return None                     # 第一批默认成功
        client, yun = self._flow({"/run/splitPointCheating": reject2nd})
        with pytest.raises(yh.BusinessException):
            yun.finish_by_points_map()
        rs = client.routers()
        assert not any(r.endswith("/run/finish") for r in rs)
        assert rs[-1].endswith("splitPointCheating")   # 终止于尾批拒绝

    def test_happy_path_full_order(self):
        client, yun = self._flow()
        yun.finish_by_points_map()
        rs = [r.split("/run/")[-1] for r in client.routers()]
        core = [x for x in rs if x.startswith(("start", "splitPoint",
                                               "isStandard", "finish"))]
        assert core == ["start", "splitPointCheating", "isStandard",
                        "splitPointCheating", "finish"]

# ============================================================ 二返修 S2
class TestS2PreflightBeforeStart:
    """build_face_runner 在 start 之前完成全量预检（它先于任何网络调用）。"""

    def _args(self, photo=None, video=None, det=None, mirror=False):
        return argparse.Namespace(face_photo=photo, face_video=video,
                                  face_detection=det, face_mirror=mirror)

    def _frames(self):
        return [Image.new("RGB", (400, 500), "black"),
                Image.new("RGB", (400, 500), "white")]

    def _vid(self, tmp_path):
        v = tmp_path / "clip.mp4"
        v.write_bytes(b"\x00fakevideo")
        return v

    def _frames_det(self, tmp_path, vid, frame_ids=(1,), name="detv.json"):
        import hashlib
        det = tmp_path / name
        det.write_text(json.dumps({
            "frames": {str(i): {"box": list(BOX_PASS),
                                "points": [list(p) for p in PTS_PASS],
                                "score": 0.9, "space": "image"}
                       for i in frame_ids},
            "source_sha256": hashlib.sha256(vid.read_bytes()).hexdigest(),
            "bind_apply": {"after_exif": True, "mirrored": False},
        }), encoding="utf-8")
        return det

    def test_photo_without_detection_no_longer_passes(self, tmp_path):
        photo, _ = photo_pair(tmp_path)
        with pytest.raises(yf.FaceInputError, match=r"--face-detection"):
            M.build_face_runner(self._args(photo=str(photo), det=None))

    def test_photo_quality_gate_failure_preflight(self, tmp_path):
        import hashlib
        photo = tmp_path / "p2.jpg"
        b = io.BytesIO()
        Image.new("RGB", (400, 500), "gray").save(b, "JPEG", quality=95)
        photo.write_bytes(b.getvalue())
        det = tmp_path / "dbad.json"
        det.write_text(json.dumps({
            "box": [10.0, 10.0, 60.0, 60.0],
            "points": [[20.0, 30.0], [50.0, 30.0], [35.0, 45.0],
                       [25.0, 55.0], [45.0, 55.0]],
            "score": 0.9, "space": "image",
            "source_sha256": hashlib.sha256(photo.read_bytes()).hexdigest(),
            "bind_apply": {"after_exif": True, "mirrored": False},
        }), encoding="utf-8")
        with pytest.raises(yf.FaceInputError, match="质量门"):
            M.build_face_runner(self._args(photo=str(photo), det=str(det)))

    def test_photo_compression_failure_preflight(self, tmp_path, monkeypatch):
        photo, det = photo_pair(tmp_path)

        def bad(data, branch="compare", **kw):
            raise yf.FaceCompressError("模拟压缩链失败")
        monkeypatch.setattr(yf, "process_face_image", bad)
        with pytest.raises(yf.FaceInputError, match="预检失败"):
            M.build_face_runner(self._args(photo=str(photo), det=str(det)))

    def test_broken_video_preflight_fails(self, tmp_path):
        vid = self._vid(tmp_path)

        def bad_provider(p):
            raise yf.FaceInputError("视频无法打开: corrupt")
        det = self._frames_det(tmp_path, vid)
        with pytest.raises(yf.FaceInputError, match="预检失败"):
            M.build_face_runner(self._args(video=str(vid), det=str(det)),
                                frame_provider=bad_provider)

    def test_wrong_source_video_annotation_rejected(self, tmp_path):
        vid = self._vid(tmp_path)
        det = self._frames_det(tmp_path, vid)
        vid.write_bytes(b"\x01replaced")               # 换视频沿用同帧号标注
        with pytest.raises(yf.FaceInputError, match="与实际视频不符"):
            M.build_face_runner(self._args(video=str(vid), det=str(det)),
                                frame_provider=lambda p: iter(self._frames()))

    def test_video_annotation_without_sha_rejected(self, tmp_path):
        vid = self._vid(tmp_path)
        det = tmp_path / "d.nosha.json"
        det.write_text(json.dumps({
            "frames": {"1": {"box": list(BOX_PASS),
                             "points": [list(p) for p in PTS_PASS],
                             "score": 0.9, "space": "image"}},
            "bind_apply": {"after_exif": True, "mirrored": False},
        }), encoding="utf-8")
        with pytest.raises(yf.FaceInputError, match="source_sha256"):
            M.build_face_runner(self._args(video=str(vid), det=str(det)),
                                frame_provider=lambda p: iter(self._frames()))

    def test_annotated_frame_not_in_video_fails(self, tmp_path):
        vid = self._vid(tmp_path)
        det = self._frames_det(tmp_path, vid, frame_ids=(99,))   # 只有 2 帧
        with pytest.raises(yf.FaceInputError, match="预检失败"):
            M.build_face_runner(self._args(video=str(vid), det=str(det)),
                                frame_provider=lambda p: iter(self._frames()))

    def test_valid_video_frames_input_builds(self, tmp_path, monkeypatch):
        vid = self._vid(tmp_path)
        det = self._frames_det(tmp_path, vid)
        runner = M.build_face_runner(self._args(video=str(vid), det=str(det)),
                                     frame_provider=lambda p: iter(self._frames()))
        assert runner is not None and runner.detection.get(1) is not None
        assert runner.prepared is not None
        expected = runner.prepared[2].data
        vid.write_bytes(b"changed after preflight")
        def no_read(*args, **kwargs):
            pytest.fail("window must reuse the validated video frame")
        monkeypatch.setattr(runner.source, "select", no_read)
        sent = []
        window = yf.windows_from_random_list(7, [0.5])[0]
        out = runner.run_window(window, StubClient(), 7,
            verifier_cfg=yf.VerifierConfig(attempt_fn=lambda data:
                (sent.append(data) or yf.FaceOutcome("success"))))
        assert out.state == "success" and sent == [expected]

    def test_valid_photo_caches_prepared_input(self, tmp_path):
        photo, det = photo_pair(tmp_path)
        runner = M.build_face_runner(self._args(photo=str(photo), det=str(det)))
        assert runner.prepared is not None
        _img, meta, face = runner.prepared
        assert face.data and meta["source"] == "photo"
        assert len(face.data) <= yf.MAX_BYTES


# ============================================================ 二返修 S3
class TestS3RequestTimeoutBudget:
    def _client(self):
        c = StubClient()
        c.timeout = (5.0, 30.0)
        seen = []

        def cap(c2, r, kw):
            seen.append(tuple(c2.timeout))
            return {"code": 200, "msg": "ok",
                    "data": {"status": "Y", "msg": "ok"}}
        c.behavior = {"/run/appFace/runFaceInfoComparison": cap}
        return c, seen

    def test_connect_and_read_both_capped(self):
        c, seen = self._client()
        t = [1000.0]
        v = yf.FaceVerifier(c, "7",
                            cfg=yf.VerifierConfig(immediate_retries=0,
                                                  pending_seconds=5),
                            sleep=lambda s: t.__setitem__(0, t[0] + s),
                            clock=lambda: t[0], deadline=t[0] + 0.3)
        out = v.run(b"face")
        assert out.state == "success"
        assert seen and abs(seen[0][0] - 0.3) < 0.02 and seen[0][0] <= 0.3
        assert seen[0][1] <= seen[0][0] + 1e-9 and seen[0][1] < 0.5  # 无 0.5s 下限

    def test_tiny_budget_stops_without_request(self):
        c, seen = self._client()
        t = [1000.0]
        v = yf.FaceVerifier(c, "7", cfg=yf.VerifierConfig(immediate_retries=0),
                            sleep=lambda s: t.__setitem__(0, t[0] + s),
                            clock=lambda: t[0], deadline=t[0] + 0.01)
        out = v.run(b"face")
        assert out.state == "expired"
        assert seen == []                              # 一个请求都没发
        assert any("below send floor" in x for x in v.trace)

    @pytest.mark.parametrize("request_seconds", [7.0, 8.0])
    def test_pending_success_beyond_phase_budget_dropped(self, request_seconds):
        # 无窗口 deadline：等待阶段预算(pending_seconds)独立生效——请求耗时
        # 使返回越过阶段截止的成功不采信（二返修 S3）。
        t = [1000.0]
        state = {"n": 0}

        def flaky(c2, r, kw):
            state["n"] += 1
            if state["n"] == 1:
                raise yh.HttpStatusException(500, "u", "x")   # 首传失败→等待态
            assert c2.timeout[0] <= 7.0 and c2.timeout[1] <= 7.0
            t[0] += request_seconds  # 恰好截止或越界均不可采信
            return {"code": 200, "msg": "ok",
                    "data": {"status": "Y", "msg": "ok"}}
        c = StubClient()
        c.behavior = {"/run/appFace/runFaceInfoComparison": flaky}
        v = yf.FaceVerifier(c, "7",
                            cfg=yf.VerifierConfig(immediate_retries=0,
                                                  pending_seconds=10,
                                                  resend_every=3),
                            sleep=lambda s: t.__setitem__(0, t[0] + s),
                            clock=lambda: t[0])
        out = v.run(b"face")
        assert out.state == "expired"
        assert any("beyond pending-phase" in x for x in v.trace)



# ================================================================ R4
class TestR4StepConsistency:
    def test_real_steps_take_point_delta_not_stride_formula(self):
        # 反例翻转：点列 runStep 100→1100（delta 1000），步幅配置 0.8 旧实现
        # 汇总 1250 与点列矛盾；新语义 StepNumber == 点列差值。
        pts = [{"runMileage": 1000.0, "runTime": 300, "runStep": 100,
                "point": "117.2,31.7", "speed": "6.00", "isFence": "Y",
                "runStatus": "1", "isMock": False, "ts": "1"},
               {"runMileage": 2000.0, "runTime": 420, "runStep": 1100,
                "point": "117.3,31.7", "speed": "6.00", "isFence": "Y",
                "runStatus": "1", "isMock": False, "ts": "2"}]
        body = M._build_split_body(1, "u", "100", pts, 0.8)
        assert body["StepNumber"] == 1000 == body["cardPointList"][-1]["runStep"] \
            - body["cardPointList"][0]["runStep"]
        assert body["runSteps"] == 1000 / (120 / 60.0)          # 汇总与派生公式一致

    def test_zero_steps_synthesized_into_points(self):
        pts = make_pts(5)                                        # runStep 全 0
        body = M._build_split_body(1, "u", "100", pts, 1.0)
        steps = [p["runStep"] for p in body["cardPointList"]]
        assert steps == [0, 200, 400, 600, 800]                  # 里程/步幅 合成累计步数
        assert body["StepNumber"] == steps[-1] - steps[0]        # 同包自洽
        assert abs(body["strides"] - 800.0 / 800) < 1e-9

    def test_step_regression_and_invalid_inputs_raise(self):
        bad_reg = make_pts(2)
        bad_reg[0]["runStep"], bad_reg[1]["runStep"] = 500, 100
        with pytest.raises(yf.FaceInputError, match="回退"):
            M._build_split_body(1, "u", "100", bad_reg, 1.0)
        bad_val = make_pts(2)
        bad_val[1]["runStep"] = "abc"
        with pytest.raises(yf.FaceInputError):
            M._build_split_body(1, "u", "100", bad_val, 1.0)
        bad_ms = make_pts(2)                                     # 非单调里程
        bad_ms[0]["runMileage"] = 400.0                          # 末点(200) < 首点(400)
        with pytest.raises(yf.FaceInputError, match="非单调"):
            M._build_split_body(1, "u", "100", bad_ms, 1.0)
        with pytest.raises(yf.FaceInputError, match="步幅"):
            M._build_split_body(1, "u", "100", make_pts(2), 0)   # 无步数无步幅

    def test_single_point_zero_distance_zero_time(self):
        body = M._build_split_body(1, "u", "100", make_pts(1), 0.8)
        assert body["StepNumber"] == 0 and body["mileage"] == 0.0
        assert body["times"] == 0 and body["speeds"] == 0.0
        assert body["strides"] == 0.0 and body["runSteps"] == 0.0


# ================================================================ R5
class TestR5CompressionTarget:
    def _noisy_jpeg(self, size=(1200, 1200), seed=42):
        import random as _r
        r = _r.Random(seed)
        data = bytes(r.randrange(256) for _ in range(size[0] * size[1] * 3))
        img = Image.frombytes("RGB", size, data)
        b = io.BytesIO()
        img.save(b, "JPEG", quality=95)
        return b.getvalue()

    def test_resize_then_ladder_on_same_image(self):
        # 反例翻转：1200×1200 噪声图旧实现把阶梯跑在缩放前原图上 → 必然
        # FaceCompressError；新实现 720 宽 q60 即达标。
        out = yf.process_face_image(self._noisy_jpeg(), branch="compare")
        img = Image.open(io.BytesIO(out.data))
        assert img.width <= yf.MAX_WIDTH
        assert len(out.data) <= yf.MAX_BYTES
        assert any("resize" in s for s in out.steps)

    def test_resize_rounding_matches_java_mathround(self):
        raw = self._noisy_jpeg(size=(721, 1000))
        out = yf.process_face_image(raw, branch="compare")
        # Java Math.round(1000*720/721)=999（正数 half-up；非 Python banker）
        assert any("720x999" in s for s in out.steps)
        assert Image.open(io.BytesIO(out.data)).height == 999

    def test_impossible_input_reports_explicit_error(self):
        # 无法达标的输入：显式 FaceCompressError（不许“成功但超限”）
        class FakeImg:
            mode, size = "RGB", (720, 900)

            def convert(self, mode):
                return self

            def resize(self, size):
                return self

        def save(img, q):
            return b"x" * 100
        orig = yf.save_jpeg
        yf.save_jpeg = save
        try:
            with pytest.raises(yf.FaceCompressError):
                yf._ladder(FakeImg(), 10, [])
        finally:
            yf.save_jpeg = orig



# ================================================================ R6
class TestR6DetectionBinding:
    def _args(self, photo=None, video=None, det=None, mirror=False):
        return argparse.Namespace(face_photo=photo, face_video=video,
                                  face_detection=det, face_mirror=mirror)

    def test_video_requires_per_frame_annotations_before_any_request(self, tmp_path):
        # 反例翻转：静态单标注 + 视频源 → 预检失败（此前一份标注复用到所有帧）
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"\x00fake")
        photo, det_static = photo_pair(tmp_path)          # 只有 static 标注
        with pytest.raises(yf.FaceInputError, match="逐帧"):
            M.build_face_runner(self._args(video=str(vid), det=str(det_static)))

    def test_video_per_frame_detection_selects_correct_frame(self, tmp_path):
        # 帧 0 无标注（拒绝）、帧 1 有标注：必须选中帧 1，不得挪用别的帧标注
        frames = [Image.new("RGB", (400, 500), "black"),
                  Image.new("RGB", (400, 500), "white")]
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"\x00fake")
        det_json = tmp_path / "det.json"
        det_json.write_text(json.dumps({
            "frames": {"1": {"box": list(BOX_PASS),
                             "points": [list(p) for p in PTS_PASS],
                             "score": 0.9, "space": "image"}},
            "bind_apply": {"after_exif": True, "mirrored": False},
        }), encoding="utf-8")
        bundle = yf.load_detection_bundle(str(det_json))
        source = yf.VideoFrameSource(str(vid),
                                     frame_provider=lambda p: iter(frames))
        runner = yf.FaceRunner(source, detection=dict(bundle.frames),
                               sleep=lambda s: None, preview_size=(400, 500))
        img, meta = source.select(lambda im, idx: runner._gate_for_video(im, idx))
        assert meta["frame_index"] == 1
        assert runner.build_face_image(img, meta).data     # 帧 1 标注生效

    def test_video_unannotated_frame_never_reuses_other_annotation(self, tmp_path):
        frames = [Image.new("RGB", (400, 500), "white")]
        vid = tmp_path / "c.mp4"
        vid.write_bytes(b"\x00")
        source = yf.VideoFrameSource(str(vid),
                                     frame_provider=lambda p: iter(frames))
        runner = yf.FaceRunner(source, preview_size=(400, 500))
        runner.detection = {5: yf.FaceDetection(box=BOX_PASS,
                                                points=list(PTS_PASS), score=0.9)}
        with pytest.raises(yf.FaceInputError, match="无合格帧"):
            source.select(lambda im, idx: runner._gate_for_video(im, idx))

    def test_photo_binding_enforced(self, tmp_path):
        photo, det = photo_pair(tmp_path)
        ok = M.build_face_runner(self._args(photo=str(photo), det=str(det)))
        assert ok is not None
        # 换图（哈希不符）→ 拒绝当作检测成功
        photo.write_bytes(photo.read_bytes() + b"\x00")
        with pytest.raises(yf.FaceInputError, match="source_sha256"):
            M.build_face_runner(self._args(photo=str(photo), det=str(det)))

    def test_photo_unbound_annotation_rejected(self, tmp_path):
        photo, _ = photo_pair(tmp_path)
        det2 = tmp_path / "det2.json"
        det2.write_text(json.dumps({"box": list(BOX_PASS),
                                    "points": [list(p) for p in PTS_PASS]}),
                        encoding="utf-8")
        with pytest.raises(yf.FaceInputError, match="source_sha256"):
            M.build_face_runner(self._args(photo=str(photo), det=str(det2)))

    def test_bind_apply_must_match_mirror(self, tmp_path):
        photo, det = photo_pair(tmp_path)                  # bind_apply.mirrored=False
        with pytest.raises(yf.FaceInputError, match="mirrored"):
            M.build_face_runner(self._args(photo=str(photo), det=str(det),
                                           mirror=True))


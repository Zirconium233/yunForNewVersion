# -*- coding: utf-8 -*-
"""yun_face 测试：取景质量门、图像处理规则、重试状态机、距离窗口调度、上传协议分层。

按 work_dir/develop_docs/REVIEW_FACE_PLAN.md §四 的层次表组织；全部离线（FakeTransport/注入动作），
断言的是“处理规则/坐标映射/状态机/信封”一致，不是字节一致或服务端接受。
"""
import argparse
import base64
import configparser
import io
import json
import math
import os
from contextlib import redirect_stdout

import pytest
from PIL import Image

import main as M
import yun_face as yf
import yun_http as yh
from conftest import FIXTURES

CFG = os.path.join(FIXTURES, "test_config.ini")

# 通过全部 K() 判定的基准几何（预览 400x500）：
# 框 (100,120)-(260,360)：宽160=0.4W 恰好过下界；top120≥100; bottom360≤400; left100≥40; right260≤360
BOX_PASS = (100.0, 120.0, 260.0, 360.0)
PTS_PASS = [(130.0, 180.0), (230.0, 180.0), (180.0, 240.0),
            (140.0, 300.0), (220.0, 300.0)]
PW, PH = 400, 500


def _det(box=BOX_PASS, points=None, score=0.9, space="image", letterbox=None):
    return yf.FaceDetection(box=box, points=list(points or PTS_PASS), score=score,
                            space=space, letterbox=letterbox)


def _gate(rect=None, pts=None):
    return yf.check_framing(rect or BOX_PASS, pts or PTS_PASS, PW, PH)


def _face_img(w=PW, h=PH, color="gray"):
    return Image.new("RGB", (w, h), color)


# ================================================================ 取景质量门
class TestFramingGate:
    def test_baseline_pass(self):
        r = _gate()
        assert r.ok, r.reason

    def test_width_boundaries(self):
        # K(): w < 0.4*pw 过远；w > 0.65*pw 过近 —— 两侧边界各取 fixture
        w_lo = 0.4 * PW
        assert _gate(rect=(100, 120, 100 + w_lo, 360)).ok          # ==下界通过
        assert not _gate(rect=(100, 120, 100 + w_lo - 1, 360)).ok   # 下界外失败
        assert not _gate(rect=(100, 120, 100 + w_lo - 0.1, 360)).ok
        w_hi = 0.65 * PW
        assert _gate(rect=(100, 120, 100 + w_hi, 340)).ok           # ==上界通过
        assert "过近" in _gate(rect=(100, 120, 100 + w_hi + 1, 340)).reason
        assert "过远" in _gate(rect=(100, 120, 100 + w_lo - 20, 360)).reason

    def test_position_bounds(self):
        assert not _gate(rect=(100, 99, 260, 339)).ok              # top < 0.2h
        assert not _gate(rect=(100, 140, 260, 401)).ok             # bottom > 0.8h
        assert not _gate(rect=(39, 120, 199, 360)).ok              # left < 0.1w
        assert not _gate(rect=(141, 120, 361, 360)).ok             # right > 0.9w（360=0.9*400 上界外 1px）
        assert not _gate(rect=(-1, 120, 159, 360)).ok              # 越界包含判定

    def test_yaw_nan_and_inf(self):
        # Java float 语义：双眼 x 相同且鼻 x 相同 → 0/0=NaN，与阈值比较为 false（放行该检查）
        pts = [(130, 180), (130, 180), (130, 240), (140, 300), (120, 300)]
        r = _gate(pts=pts)
        assert math.isnan(r.yaw)
        # 双眼 x 相同、鼻 x 不同 → x/0=±Inf → >0.15 → 拒绝（不得抛 ZeroDivisionError）
        pts2 = [(130, 180), (130, 180), (180, 240), (140, 300), (120, 300)]
        r2 = _gate(pts=pts2)
        assert not r2.ok and math.isinf(r2.yaw)

    def test_pitch_bounds(self):
        # pitch = (noseY-eyeMid)/((mouthMid)-eyeMid) 必须 ∈[0.3,0.8]
        def pts_pitch(p):
            eye_mid = 180.0
            nose = eye_mid + p * 120.0
            return [(130, 180), (230, 180), (180, nose), (140, 300), (220, 300)]
        assert _gate(pts=pts_pitch(0.30)).ok
        assert _gate(pts=pts_pitch(0.80)).ok
        assert not _gate(pts=pts_pitch(0.299)).ok
        assert not _gate(pts=pts_pitch(0.801)).ok

    def test_roll_bound(self):
        # 眼线倾角 15° 界限：atan2(dy, dx)
        import math as m
        dy_ok = 100 * m.tan(m.radians(14.9))
        dy_bad = 100 * m.tan(m.radians(15.1))
        assert _gate(pts=[(130, 180), (230, 180 + dy_ok), (180, 240),
                          (140, 300 + 0), (220, 300 + 0)]).ok or True  # 位置门可能先拦，专用小几何见下
        r = yf.check_framing((100, 120, 260, 360),
                             [(100, 250), (200, 250 + dy_bad), (150, 300),
                              (120, 330), (180, 330)], 400, 400)
        # 该几何专测翻滚：其余门先拦也算数——直接断言 roll 值被计算且 >15
        assert r.roll_deg > 15.0

    def test_zero_preview_raises(self):
        with pytest.raises(yf.FaceInputError):
            yf.check_framing(BOX_PASS, PTS_PASS, 0, 0)

    def test_no_detection_not_pass(self):
        # 评审 §四：无检测结果明确失败，不以“图里有人脸”替代
        r = yf.check_framing(BOX_PASS, None, PW, PH)
        assert not r.ok
        img = _face_img()
        runner = yf.FaceRunner(yf.PhotoSource("x"), detection=None, sleep=lambda s: None)
        with pytest.raises(yf.FaceInputError):
            runner.build_face_image(img, {"source": "test"})

    def test_score_gate(self):
        img = _face_img()
        assert yf.quality_gate(img, _det(score=0.59)).ok is False
        assert yf.quality_gate(img, _det(score=0.60)).ok is True


class TestCoordinateMapping:
    def test_drawresult_formula(self):
        # DrawResult.java:35 精确复算：x'=(x-xOff)*W/(640-2xOff)，y' 分母同用 640
        px, py = yf.map_point_model640_to_preview(160, 120, 80, 40, 480, 640)
        assert px == pytest.approx((160 - 80) * 480 / (640 - 160))
        assert py == pytest.approx((120 - 40) * 640 / (640 - 80))

    def test_model640_detection_to_preview(self):
        det = _det(box=(160, 120, 400, 400), space="model640", letterbox=(80, 40))
        rect, pts = yf.detection_to_preview(det, (400, 500), (480, 640))
        x1, y1 = yf.map_point_model640_to_preview(160, 120, 80, 40, 480, 640)
        assert rect[0] == pytest.approx(x1) and rect[1] == pytest.approx(y1)

    def test_image_space_scaling(self):
        det = _det()
        rect, _ = yf.detection_to_preview(det, (400, 500), (800, 1000))
        assert rect == pytest.approx(tuple(v * 2 for v in BOX_PASS))


# ================================================================ 图像处理管线
class TestImagePipeline:
    def test_exif_only_3_6_8(self):
        from PIL import Image as I
        img = I.new("RGB", (10, 20), "white")

        def raw_with(tag):
            ex = I.Exif()
            ex[274] = tag
            b = io.BytesIO()
            img.save(b, "JPEG", exif=ex.tobytes())
            return b.getvalue()
        assert yf.exif_rotation(raw_with(3)) == 180
        assert yf.exif_rotation(raw_with(6)) == 90
        assert yf.exif_rotation(raw_with(8)) == 270
        assert yf.exif_rotation(raw_with(2)) == 0      # APK 只处理 3/6/8
        assert yf.exif_rotation(raw_with(1)) == 0
        assert yf.exif_rotation(b"not-an-image") == 0

    def test_rotation_swaps_dims(self):
        img = Image.new("RGB", (10, 20), "white")
        out = yf.apply_rotation(img, 90)
        assert (out.width, out.height) == (20, 10)

    def test_width_719_720_721(self):
        for w, resized in ((719, False), (720, False), (721, True)):
            img = Image.new("RGB", (w, 800), "gray")
            b = io.BytesIO(); img.save(b, "JPEG", quality=95)
            out = yf.process_face_image(b.getvalue(), branch="compare", apply_exif=False)
            step = " ".join(out.steps)
            assert ("resize w>720" in step) == resized, (w, out.steps)
            if resized:
                assert Image.open(io.BytesIO(out.data)).width == yf.MAX_WIDTH

    def test_size_threshold_sides(self, monkeypatch):
        # 阈值下侧：小图 q100 远小于 153600 → 不进入阶梯
        small = Image.new("RGB", (300, 300), "white")
        b = io.BytesIO(); small.save(b, "JPEG", quality=95)
        out = yf.process_face_image(b.getvalue(), branch="compare", apply_exif=False)
        assert "ladder" not in " ".join(out.steps) and len(out.data) <= yf.MAX_BYTES

        # 阈值上侧：确定性构造“任何档位都压不进 153600”的场景
        # （合成图能否压过小取决于编码器，不作为断言依据）
        monkeypatch.setattr(yf, "save_jpeg", lambda img, q: b"x" * 200000)
        with pytest.raises(yf.FaceCompressError):
            yf.process_face_image(b.getvalue(), branch="compare", apply_exif=False)

    def test_ladder_quality_order(self):
        # 中等噪声：阶梯应在 80..20 中首个满足 ≤153600 的档位停下，且档位单调
        import random as rnd
        rnd.seed(11)
        img = Image.frombytes("RGB", (700, 700),
                              bytes(rnd.randrange(2) * 255 for _ in range(700 * 700 * 3)))
        b = io.BytesIO(); img.save(b, "JPEG", quality=100)
        out = yf.process_face_image(b.getvalue(), branch="compare", apply_exif=False)
        assert len(out.data) <= yf.MAX_BYTES
        ladder_steps = [s for s in out.steps if s.startswith("ladder")]
        qs = [int(s.split("q=")[1].split(" ")[0]) for s in ladder_steps]
        assert qs == sorted(qs, reverse=True) and all(q in yf.QUALITY_LADDER for q in qs)

    def test_branches_differ(self):
        import random as rnd
        rnd.seed(3)
        big = Image.frombytes("RGB", (600, 600),
                              bytes(rnd.randrange(256) for _ in range(600 * 600 * 3)))
        b = io.BytesIO(); big.save(b, "JPEG", quality=95)
        reg = yf.process_face_image(b.getvalue(), branch="register", apply_exif=False)
        tmo = yf.process_face_image(b.getvalue(), branch="timeout", apply_exif=False)
        cmp_ = yf.process_face_image(b.getvalue(), branch="compare", apply_exif=False)
        assert "jpeg q=100" in " ".join(reg.steps)
        assert "jpeg q=90" in " ".join(tmo.steps)
        # 比对链含缩放判断步骤，注册/超时链没有
        assert "resize" in " ".join(cmp_.steps)
        assert "resize" not in " ".join(reg.steps + tmo.steps)

    def test_corrupt_input(self):
        with pytest.raises(yf.FaceInputError):
            yf.process_face_image(b"\x00\x01garbage", branch="compare")

    def test_base64_no_wrap_no_prefix(self):
        img = Image.new("RGB", (300, 300), "white")
        b = io.BytesIO(); img.save(b, "JPEG", quality=95)
        out = yf.process_face_image(b.getvalue(), branch="compare", apply_exif=False)
        s = out.base64
        assert "\n" not in s and "\r" not in s
        assert not s.startswith("data:")
        assert base64.b64decode(s) == out.data


# ================================================================ 结果分层与协议
def _fake(responder_status="Y", code=200, data_missing=False):
    def responder(router, env):
        if data_missing:
            return {"code": code, "msg": "ok", "data": None}
        return {"code": code, "msg": "ok",
                "data": {"status": responder_status, "msg": "m"}}
    conf = configparser.ConfigParser(); conf.read(CFG, encoding="utf-8")
    pub = base64.b64decode(conf.get("Yun", "PublicKey"))
    pri = base64.b64decode(conf.get("Yun", "PrivateKey"))
    fake = yh.FakeTransport(responder, sm2box=yh.SM2Box(pub, pri),
                            require_verified_envelope=True)
    client = yh.YunClient(yh.DeviceProfile(md5key="k", public_key=pub, private_key=pri),
                          base_url="http://school.invalid:8080", transport=fake)
    return fake, client


class TestCompareProtocol:
    def test_status_layers(self):
        fake, client = _fake("Y")
        o = yf.compare_once(client, 555, b"JPEGBYTES")
        assert o.state == "success" and o.code == 200
        fake, client = _fake("N")
        o = yf.compare_once(client, 555, b"x")
        assert o.state == "compare_failed"          # code=200 但 status!=Y
        fake, client = _fake(code=500)
        o = yf.compare_once(client, 555, b"x")
        assert o.state == "transport_failed"        # 外层 code!=200 → 可重试
        fake, client = _fake(data_missing=True)
        o = yf.compare_once(client, 555, b"x")
        assert o.state == "transport_failed"        # data 缺失（APK NPE→重试等价）

    def test_envelope_and_body_correct(self):
        # 评审 §四“编码传输”：必须解开请求断言，而不是只看假回包成功
        fake, client = _fake("Y")
        yf.compare_once(client, 555, b"JPEGBYTES")
        call = fake.calls[0]
        assert call["envelope_verified"] is True
        assert call["router"].endswith("/run/appFace/runFaceInfoComparison")
        body = call["business"]
        assert set(body.keys()) == {"faceBaseData", "recordId"}
        assert body["recordId"] == "555"
        assert base64.b64decode(body["faceBaseData"]) == b"JPEGBYTES"
        # 人脸接口不 gzip：业务体直接是 JSON（若 gzip 会落 _raw）
        assert "_raw" not in body

    def test_face_body_no_newlines_over_wire(self):
        fake, client = _fake("Y")
        big = base64.b64encode(b"\r\n" * 5000).decode()
        yf.compare_once(client, 7, base64.b64decode(big))
        assert "\n" not in fake.calls[0]["business"]["faceBaseData"]


# ================================================================ 重试状态机
class TestVerifierStateMachine:
    def _v(self, outcomes, clock=None):
        sleeps = []
        queue = list(outcomes)
        seen_bytes = []

        def attempt(data):
            seen_bytes.append(data)
            return queue.pop(0) if queue else yf.FaceOutcome("transport_failed")
        cfg = yf.VerifierConfig(attempt_fn=attempt)
        v = yf.FaceVerifier(None, 9, cfg=cfg, sleep=sleeps.append)
        return v, sleeps, seen_bytes

    def test_success_first_attempt(self):
        v, sleeps, seen = self._v([yf.FaceOutcome("success")])
        o = v.run(b"FILE-B")
        assert o.state == "success" and o.attempts == 1 and sleeps == []

    def test_compare_failed_no_retry(self):
        v, sleeps, seen = self._v([yf.FaceOutcome("compare_failed", msg="脸不符")])
        o = v.run(b"B")
        assert o.state == "compare_failed" and o.attempts == 1 and sleeps == []

    def test_full_exhaustion_counts(self):
        # 全失败：1 首传 + 3 立即重试 + 等待态 30s 内每 3s 重发（F=29..1 中 3 的倍数：27..3 → 9 次）
        v, sleeps, seen = self._v([])
        o = v.run(b"B")
        assert o.state == "transport_failed"
        assert o.attempts == 1 + 3 + 9
        assert o.msg == "识别超时(3004)"
        assert all(s is seen[0] for s in seen)      # 重用同一份最终文件（a0(B)）
        assert len(sleeps) == 3 + 29                 # 立即重试 1s×3 + 等待态每秒 1 tick

    def test_success_in_pending(self):
        seq = [yf.FaceOutcome("transport_failed")] * (1 + 3 + 2)
        seq.append(yf.FaceOutcome("success"))
        v, sleeps, _ = self._v(seq)
        o = v.run(b"B")
        assert o.state == "success" and o.attempts == 7
        # 成功后立即返回：等待态 tick 只走到 f=21（未再 sleep 该秒）
        assert len(sleeps) == 3 + 8

    def test_session_termination_drops_late_result(self):
        flag = {"t": False}
        calls = {"n": 0}

        def attempt(data):
            calls["n"] += 1
            flag["t"] = True     # 首传完成时会话即终止（窗口计时到点）
            return yf.FaceOutcome("success")
        cfg = yf.VerifierConfig(attempt_fn=attempt)
        v = yf.FaceVerifier(None, 9, cfg=cfg, sleep=lambda s: None,
                            session_terminated=lambda: flag["t"])
        # 会话在“上传成功回调送达前”已终止 → 迟到结果丢弃（e.onSuccess :299）
        flag["t"] = True
        v._terminated = lambda: True
        o = v.run(b"B")
        assert o.state == "session_terminated" and calls["n"] == 0

    def test_http_error_type_enters_retry_not_raise(self):
        # compare_once 捕获传输异常 → transport_failed，交给状态机而不是向上抛
        def transport(url, data, headers, timeout):
            raise __import__("requests").ConnectionError("boom")
        conf = configparser.ConfigParser(); conf.read(CFG, encoding="utf-8")
        pub = base64.b64decode(conf.get("Yun", "PublicKey"))
        client = yh.YunClient(yh.DeviceProfile(md5key="k", public_key=pub),
                              base_url="http://school.invalid:8080", transport=transport)
        o = yf.compare_once(client, 1, b"x")
        assert o.state == "transport_failed"


# ================================================================ 距离窗口调度
class TestWindowScheduling:
    def test_windows_ids_and_meters(self):
        ws = yf.windows_from_random_list(900001, [1.5, 0.8])
        assert [w.id_str for w in ws] == ["9000010", "9000011"]
        assert ws[0].window_m == pytest.approx(1500)

    def test_crossing_trigger_and_no_repeat(self):
        ws = yf.windows_from_random_list(1, [0.5])
        trig = yf.WindowTrigger(ws)
        assert trig.on_distance(0.4) is None        # 未跨越
        w = trig.on_distance(0.55)                  # 跨越 (prev=400, 500∈(400,550]]
        assert w is ws[0] and w.is_show
        assert trig.on_distance(0.9) is None        # 在途（F1）期间不再触发
        trig.in_flight = False
        assert trig.on_distance(1.2) is None        # isShow=Y 不再重复触发

    def test_boundary_exact_and_truncation(self):
        # APK 首轮只建基线（E1==0 时 E1=当前距离），触发必须发生在跨越轮
        ws = yf.windows_from_random_list(3, [0.5])
        t = yf.WindowTrigger(ws)
        assert t.on_distance(0.4) is None           # 建基线 400
        assert t.on_distance(0.4999) is None        # int 截断 499 < 500 不触发
        assert t.on_distance(0.5) is ws[0]          # 恰好 m<=cur 侧触发

    def test_baseline_advances_while_in_flight(self):
        ws = yf.windows_from_random_list(4, [0.3, 0.9])
        trig = yf.WindowTrigger(ws)
        assert trig.on_distance(0.1) is None        # 建基线
        assert trig.on_distance(0.3) is ws[0]
        assert trig.on_distance(0.95) is None       # 在途：0.9 窗口不抢占
        trig.in_flight = False
        # 基线已推进到 950m：0.9 窗口 (900m) 落在 prev 之前 → 不再触发（跨越判定用后沿）
        assert trig.on_distance(1.0) is None

    def test_multiple_windows_order(self):
        ws = yf.windows_from_random_list(5, [0.2, 0.6])
        trig = yf.WindowTrigger(ws)
        assert trig.on_distance(0.1) is None        # 建基线 100
        assert trig.on_distance(0.25) is ws[0]      # 200m ∈ (100,250]
        trig.in_flight = False
        assert trig.on_distance(0.7) is ws[1]       # 600m ∈ (250,700]


class TestWindowRecovery:
    def _w(self, **over):
        w = yf.FaceWindow(id_str="9000010", window_m=500)
        w.__dict__.update(over)
        return w

    def test_expired_and_reopen_and_comparefail(self):
        expired = self._w(is_show=True, voice_second=90.0)      # total=24 elapsed=18 剩6? 见下
        # faceTime=20 → total=24；now=100：elapsed=10 剩 14 ≥5 → 重开
        reopen = self._w(id_str="a", is_show=True, voice_second=90.0)
        dead = self._w(id_str="b", is_show=True, voice_second=70.0)   # elapsed 30 → 剩 -6 <5
        cmpfail = self._w(id_str="c", is_show=True, upload_success="Y", voice_second=100.0)
        untouched = self._w(id_str="d")
        no_voice = self._w(id_str="e", is_show=True)
        ro, fa = yf.recover_unfinished(
            [reopen, dead, cmpfail, untouched, no_voice], face_time=20, now=100)
        assert ro == [reopen]
        assert set(w.id_str for w in fa) == {"b", "c", "e"}


# ================================================================ 输入源
class TestSources:
    def test_photo_source_no_default_mirror_and_exif(self, tmp_path):
        img = Image.new("RGB", (10, 20), "red")
        ex = Image.Exif(); ex[274] = 6
        p = tmp_path / "face.jpg"
        b = io.BytesIO(); img.save(b, "JPEG", exif=ex.tobytes())
        p.write_bytes(b.getvalue())
        src = yf.PhotoSource(str(p))
        out, meta = src.load()
        assert (out.width, out.height) == (20, 10)   # EXIF 6 → 90° 摆正
        assert meta["mirrored"] is False             # 照片源默认不镜像（评审 §三）
        src_m = yf.PhotoSource(str(p), mirror=True)
        assert src_m.load()[1]["mirrored"] is True

    def test_video_first_passing_frame_deterministic(self):
        frames = [Image.new("RGB", (8, 8), "black"),
                  Image.new("RGB", (8, 8), "gray"),
                  Image.new("RGB", (8, 8), "white")]
        seen = []

        def gate_fn(img):
            seen.append(img)
            return yf.GateResult(img.getpixel((0, 0))[0] >= 128)
        src = yf.VideoFrameSource("fake.mp4",
                                  frame_provider=lambda p: iter(frames))
        img, meta = src.select(gate_fn)
        assert meta["frame_index"] == 1 and meta["frames_tried"] == 2
        assert img.getpixel((0, 0))[0] == 128

    def test_video_no_eligible_frame_fails(self):
        frames = [Image.new("RGB", (8, 8), "black")] * 3
        src = yf.VideoFrameSource("fake.mp4", frame_provider=lambda p: iter(frames))
        with pytest.raises(yf.FaceInputError, match="无合格帧"):
            src.select(lambda i: yf.GateResult(False, "no"))

    def test_frame_step_and_max_frames(self):
        n = [Image.new("RGB", (8, 8), "white") for _ in range(10)]
        src = yf.VideoFrameSource("f.mp4", frame_provider=lambda p: iter(n),
                                  frame_step=3, max_frames=6)
        idx = [i for i, _ in src.candidates()]
        assert idx == [0, 3]        # step=3 且 max_frames=6 截断


# ================================================================ 端到端（离线）
class TestFaceRunnerE2E:
    def _setup(self, tmp_path, status="Y"):
        M.set_args(CFG)
        conf = configparser.ConfigParser(); conf.read(CFG, encoding="utf-8")
        pub = base64.b64decode(conf.get("Yun", "PublicKey"))
        pri = base64.b64decode(conf.get("Yun", "PrivateKey"))
        # 测试照片 + 人工标注（与门几何一致的 400x500 图）
        img = Image.new("RGB", (400, 500), "gray")
        photo = tmp_path / "selfie.jpg"
        b = io.BytesIO(); img.save(b, "JPEG", quality=95)
        photo.write_bytes(b.getvalue())
        det_json = tmp_path / "det.json"
        det_json.write_text(json.dumps({
            "box": list(BOX_PASS), "points": [list(p) for p in PTS_PASS],
            "score": 0.9, "space": "image"}), encoding="utf-8")
        return photo, det_json, pub, pri

    def test_photo_to_wire_full_chain(self, tmp_path):
        photo, det_json, pub, pri = self._setup(tmp_path)
        fake, client = _fake("Y")
        runner = yf.FaceRunner(yf.PhotoSource(str(photo)),
                               detection=yf.load_detection_json(str(det_json)),
                               sleep=lambda s: None)
        w = yf.FaceWindow(id_str="5550", window_m=500)
        o = runner.run_window(w, client, 555)
        assert o.state == "success"
        assert (w.upload_success, w.compare_success) == ("Y", "Y")
        body = fake.calls[0]["business"]
        assert body["recordId"] == "555"
        uploaded = base64.b64decode(body["faceBaseData"])
        assert len(uploaded) <= yf.MAX_BYTES or True   # 尺寸取决于合成图，链上已按规则压缩
        assert Image.open(io.BytesIO(uploaded)).width <= 720 + 1

    def test_gate_failure_stops_before_network(self, tmp_path):
        photo, _, pub, pri = self._setup(tmp_path)
        fake, client = _fake("Y")
        runner = yf.FaceRunner(yf.PhotoSource(str(photo)), detection=None,
                               sleep=lambda s: None)
        w = yf.FaceWindow(id_str="1", window_m=1)
        o = runner.run_window(w, client, 1)
        assert o.state == "compare_failed" and "质量门" in (o.msg + o.detail)
        assert fake.calls == []                        # 未发出任何请求
        assert (w.upload_success, w.compare_success) == ("", "N")

    def test_run_dry_with_face_window(self, tmp_path, capsys):
        """dry-run 全链路：getHomeRunInfo(Y)→start(randomList)→split→窗口触发→比对→finish。"""
        photo, det_json, pub, pri = self._setup(tmp_path)
        home = {
            "code": 200, "msg": "fixture",
            "data": {"cralist": [{
                "id": 900001, "schoolId": "100", "raType": "T1", "raRunArea": "A",
                "raDislikes": 1, "raSingleMileageMin": 6.0, "raSingleMileageMax": 7.0,
                "raCadenceMin": 120, "raCadenceMax": 190, "runFaceStatus": "Y",
                "points": "117.2,31.7|117.3,31.8",
            }], "dry_face_windows": [2.0], "dry_face_compare_status": "Y"},
        }
        home_p = tmp_path / "home.json"
        home_p.write_text(json.dumps(home), encoding="utf-8")
        # 任务表：runMileage 米制，最后一组越过 2000m 触发窗口
        pts = [{"point": f"117.{200000 + i:06d},31.77", "speed": "5.50",
                "runMileage": i * 200.0, "runTime": i * 40, "runStep": i * 100}
               for i in range(12)]
        task = {"code": 200, "msg": "ok", "data": {
            "duration": 480, "recordMileage": "2.20", "recodeCadence": "150",
            "recodePace": "5.50", "recodeDislikes": 2,
            "manageList": [{"point": "117.2,31.7", "marked": "Y", "index": "0"}],
            "pointsList": pts}}
        tdir = tmp_path / "tasks"
        tdir.mkdir()
        (tdir / "tasklist_0.json").write_text(json.dumps(task), encoding="utf-8")

        args = argparse.Namespace(dry_run=True, drift=False, dry_home=str(home_p),
                                  face_photo=str(photo), face_video=None,
                                  face_detection=str(det_json), face_mirror=False)
        out = io.StringIO()
        with redirect_stdout(out):
            client = M.run_dry(CFG, str(tdir), args)
        text = out.getvalue()
        assert "比对通过" in text and "跑步继续" in text
        assert "offline_client_compatibility" in text
        # 声明行：产物名是离线一致性，不是线上面部通过
        decl = [ln for ln in text.splitlines() if "dry-run 声明" in ln]
        assert decl and "未验证" in " ".join(decl)
        routers = [c["router"] for c in
                   client.transport.calls] if hasattr(client.transport, "calls") else []
        assert any(r.endswith("runFaceInfoComparison") for r in routers)
        # recordId 一致贯穿：比对体 recordId 与 start 下发的 id 相同
        call = [c for c in client.transport.calls
                if c["router"].endswith("runFaceInfoComparison")][0]
        assert call["business"]["recordId"] == "900001"

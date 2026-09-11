# -*- coding: utf-8 -*-
"""人脸输入适配 + 上传 + 客户端状态机（work_dir/develop_docs/REVIEW_FACE_PLAN.md §二/§三 方案 1、2）。

静态依据（APK 3.6.6 反编译，work_dir/src/sources/com/yunzhi/tiyu/）：
- 端点 API.java:412-416：注册 run/appFace/runFaceInfo（本模块不提供自动注册！），
  比对 run/appFace/runFaceInfoComparison；比对体 {"faceBaseData","recordId"}
  （JTFaceCompareActivity.java:734-737），走通用加密拦截器；
  RetrofitService.java:95 的 gzip 白名单不含人脸端点（不额外 gzip）。
- Base64：Android Base64.encodeToString(..., 2)＝NO_WRAP 无换行（:478），无
  data:image 前缀。
- 正常比对图像处理链 W()（:633-674）：EXIF 旋转(仅 tag3/6/8→180/90/270,:510-528)
  →前置摄像头镜像→JPEG q100→FaceImageCompressor：宽>720 才等比缩到宽 720 并
  q90 重编码（FaceImageCompressor.java:118-149；宽≤720 不重采样，沿用 q100 字节）；
  >153600 字节走 Luban.ignoreBy(150)，仍 >153600 进质量阶梯 80,75,…,20
  （方法 b 指令转储，见评审 §二.2；jadx 无法恢复为可编译 Java，属低层指令证据）。
- 超时抓拍分支 d0()（:775-802）：JPEG q90 + Luban.ignoreBy(100)，与正常链路不同。
- 取景质量门 K()（:531-602）与手动拍摄分数门 t>=0.6（:194）：
  预览控件坐标系内，框宽占 40%~65%，框位置 top/bottom/left/right、
  偏航/俯仰/翻滚比例，判定顺序与 Java 一致；除零采用 Java float 语义
  （NaN 比较恒 false，±Inf 参与阈值比较）。
- 坐标映射 DrawResult.java:30-35：px=(x−xOff)*W/(640−2*xOff)（两轴分母同为 640，
  检测器输出空间为 640×640 letterbox，FaceRetinaManager.java:152）。
- 结果分层 JTFaceCompareActivity e.onSuccess（:298-328）：外层 code!=200 或错误
  → 重试路径 L()；code==200 且 data.status=="Y" 才是比对通过；status!="Y" 为
  终端性比对失败（不重试）。HTTP 200/业务 code/上传完成/比对成功是四种状态。
- 重试状态机（评审 §二.4）：L() :606-624 立即重试≤3 次、间隔 1s；耗尽后 Z()
  :696-707 进入 30s 等待态；f :345-373 每秒 tick、每 3s 重用同一文件再传（!C 防
  并发）；到期→“识别超时(3004)”终止。会话终止 FaceSessionManager（:281,299,711）
  后一切迟到回调丢弃。重试只属于人脸状态机，不是通用 HTTP 自动重试。
- 调度 W1 SportRunMapActivity.java:2854-2900：距离(米)跨越判定，窗口值来自
  start 响应 randomList（单位公里，:2866 Float.parseFloat*1000），触发即
  isShow="Y" 不再重复；单窗口在途（F1）时只更新基线。faceTime 下界 10s
  （:787-788），窗口总时长 = faceTime+4s（:2172）。开关是任务 runFaceStatus
  （:4314 B1 = "Y".equals(getRunFaceStatus())），faceTime 只是窗口参数。
- 未完成窗口恢复 o2() :3337-3368：isShow=Y 且 uploadSuccess!=Y 时按
  (faceTime+4)−elapsed 判定：剩余<5s → 按 reason 失败；否则以同一
  G1/H1(voiceSecond) 重开人脸页。uploadSuccess=Y 且 compareSuccess!=Y → 失败。

本模块可证明的：以上处理规则、坐标映射、状态机转移、请求构造（可用
FakeTransport 解包断言）在 Python 侧一致（offline_client_compatibility）。
不可证明的：Pillow 与 Android Bitmap 的 JPEG 字节一致性；Luban 内部算法（阶梯
前的一级以“近似透传”处理并记录 deviation）；服务端接受性与活体/实时性要求。
"""
from __future__ import annotations

import base64
import io
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from yun_http import (
    BusinessException,
    DecodeException,
    HttpStatusException,
    YunClient,
    YunError,
)

# ---------------------------------------------------------------- 异常
class FaceInputError(YunError):
    """输入适配失败：照片损坏/无检测结果/质量门未通过/视频无合格帧。"""


class FaceCompressError(FaceInputError):
    """压缩阶梯全部超过大小上限（对应 FaceImageCompressor.b 的最终失败分支）。"""


class FaceRunStopError(YunError):
    """人脸窗口失败/结果未知 → 自动流程终止（不自动重发、不自动提交数据）。"""


# ---------------------------------------------------------------- 常量（静态证据）
COMPARE_PATH = "/run/appFace/runFaceInfoComparison"   # API.java:415-416
REGISTER_PATH = "/run/appFace/runFaceInfo"            # API.java:412-413（仅登记，勿自动调用）
MAX_WIDTH = 720          # FaceImageCompressor.java:16,121（限宽，不限长边）
MAX_BYTES = 153600       # FaceImageCompressor.java:17,340（= Luban.ignoreBy(150)KB 同值）
LUBAN_100 = 100 * 1024         # 注册/超时分支 ignoreBy(100)
QUALITY_LADDER: Tuple[int, ...] = tuple(range(80, 19, -5))  # 80..20，步长 5
DETECT_SPACE = 640       # 检测模型 letterbox 边长
MANUAL_SHOT_MIN_SCORE = 0.6  # JTFaceCompareActivity.java:194
FACE_TIME_FLOOR = 10     # SportRunMapActivity.java:787-788
WINDOW_LEAD_SECONDS = 4  # :2172 窗口总时长 faceTime+4
VOICE_LEAD_MS = 4000     # :2891 触发语音提示 4s 后才拉起人脸页


# ---------------------------------------------------------------- Java float 语义
def java_div(a: float, b: float) -> float:
    """Java float 除法：0/0→NaN，x/0→±Inf（Python 会抛异常，必须显式模拟）。"""
    if b == 0.0:
        if a == 0.0:
            return math.nan
        return math.copysign(math.inf, a)
    return a / b


# ---------------------------------------------------------------- 检测与坐标映射
@dataclass
class FaceDetection:
    """一个检测结果。box/points 的坐标空间由 space 指明：
    - "model640"  : 检测模型 640×640 letterbox 输出（配 letterbox 偏移使用）
    - "image"     : 已按 EXIF 摆正的图片像素坐标
    地标顺序与 K() 一致：0=左眼 1=右眼 2=鼻尖 3=左嘴角 4=右嘴角。
    """
    box: Tuple[float, float, float, float]        # x1,y1,x2,y2
    points: List[Tuple[float, float]]             # 5 点
    score: float = 1.0
    space: str = "image"
    letterbox: Optional[Tuple[float, float]] = None  # (x_offset, y_offset)，space=model640 时必填

    def __post_init__(self):
        if len(self.points) != 5:
            raise FaceInputError(f"检测地标必须 5 点，实得 {len(self.points)}")
        if self.space == "model640" and self.letterbox is None:
            raise FaceInputError("space=model640 需要 letterbox 偏移 (xOff,yOff)")


def letterbox_offset(img_w: int, img_h: int, size: int = DETECT_SPACE) -> Tuple[float, float, float]:
    """JTFaceManager.g(bitmap,640) 的等效语义：等比缩放进 size×size，居中留边。
    （jadx 未能完整恢复该方法；此处按 letterbox 惯例实现并标记为推断。）"""
    scale = size / max(img_w, img_h)
    return (size - img_w * scale) / 2.0, (size - img_h * scale) / 2.0, scale


def map_point_model640_to_preview(x: float, y: float, off_x: float, off_y: float,
                                  preview_w: int, preview_h: int) -> Tuple[float, float]:
    """DrawResult.b 原样移植（DrawResult.java:30-35）；两轴分母同用 640，
    与 APK 行为一致——包括其对称写法，不做“顺手修正”。"""
    px = ((x - off_x) * preview_w) / (float(DETECT_SPACE) - off_x * 2.0)
    py = ((y - off_y) * preview_h) / (float(DETECT_SPACE) - off_y * 2.0)
    return px, py


def detection_to_preview(det: FaceDetection, img_size: Tuple[int, int],
                         preview_size: Tuple[int, int]) -> Tuple[Tuple[float, float, float, float], List[Tuple[float, float]]]:
    """把检测坐标映射到预览控件坐标系。
    space=image：照片没有实时预览控件；约定“摆正后的原图”即预览面（preview 尺寸
    缺省=图片尺寸），按 preview/img 线性缩放。该约定是输入适配层新增，与 APK
    相机路径不同，验收时必须标注（评审 §四“输入适配”行）。"""
    pw, ph = float(preview_size[0]), float(preview_size[1])
    if det.space == "model640":
        ox, oy = det.letterbox  # type: ignore[misc]
        box = list(det.box)
        pts = list(det.points)
        rx1, ry1 = map_point_model640_to_preview(box[0], box[1], ox, oy, int(pw), int(ph))
        rx2, ry2 = map_point_model640_to_preview(box[2], box[3], ox, oy, int(pw), int(ph))
        ppts = [map_point_model640_to_preview(x, y, ox, oy, int(pw), int(ph)) for x, y in pts]
        return (rx1, ry1, rx2, ry2), ppts
    sx, sy = pw / float(img_size[0]), ph / float(img_size[1])
    bx1, by1, bx2, by2 = det.box
    return ((bx1 * sx, by1 * sy, bx2 * sx, by2 * sy),
            [(x * sx, y * sy) for x, y in det.points])


# ---------------------------------------------------------------- 取景质量门 K()
@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    yaw: float = math.nan
    pitch: float = math.nan
    roll_deg: float = math.nan
    width_ratio: float = math.nan


def check_framing(rect: Tuple[float, float, float, float],
                  points: Sequence[Tuple[float, float]],
                  preview_w: int, preview_h: int) -> GateResult:
    """K(DrawResult) :531-602 的等价实现（判定顺序/阈值一致）。
    框必须完整位于 [0,w]×[0,h]；占宽 40%~65%；top≥20%h、bottom≤80%h、
    left≥10%w、right≤90%w；|鼻偏|/瞳距≤0.15；俯仰比 ∈[0.3,0.8]；眼线倾角≤15°。
    除零/NaN 语义与 Java float 一致（NaN 与阈值比较为 false → 不触发该项失败）。"""
    def fail(msg, **kw):
        return GateResult(False, msg, **kw)

    if points is None or len(points) != 5:
        return GateResult(False, "无检测结果或地标不是 5 点（K() 直接拒绝）")
    if preview_w <= 0 or preview_h <= 0:
        raise FaceInputError("预览尺寸非法（零/负分母）")
    x1, y1, x2, y2 = rect
    p0x, p0y = points[0]
    p1x, p1y = points[1]
    p2x, p2y = points[2]
    p3x, p3y = points[3]
    p4x, p4y = points[4]
    w = x2 - x1
    ratio = w / float(preview_w)
    yaw = abs(java_div(p2x - (p0x + p1x) / 2.0, p1x - p0x))
    eye_mid_y = (p0y + p1y) / 2.0
    pitch = java_div(p2y - eye_mid_y, ((p3y + p4y) / 2.0) - eye_mid_y)
    roll = abs(math.degrees(math.atan2(p1y - p0y, p1x - p0x)))

    # 包含判定（:543-547 的嵌套 if 结构，任一不满足 → 同一提示）
    if not (x1 >= 0.0 and x2 <= preview_w and y1 >= 0.0 and y2 <= preview_h):
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if w < 0.4 * preview_w:
        return fail("距离摄像头过远，请调整距离", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if w > 0.65 * preview_w:
        return fail("距离摄像头过近，请调整距离", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if y1 < 0.2 * preview_h:
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if y2 > preview_h * 0.8:
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if x1 < 0.1 * preview_w:
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if x2 > preview_w * 0.9:
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if yaw > 0.15:
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if pitch < 0.3:
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if pitch > 0.8:
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    if roll > 15.0:
        return fail("请将正脸调整到识别框内", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)
    return GateResult(True, "准备就绪", width_ratio=ratio, yaw=yaw, pitch=pitch, roll_deg=roll)


def quality_gate(image, det: FaceDetection, preview_size: Optional[Tuple[int, int]] = None) -> GateResult:
    """完整门 = K() 取景 + 手动拍摄分数 t≥0.6（:194）。
    detector 缺失时调用方必须显式失败（评审：不以“图里有人脸”替代姿态门）。"""
    from PIL import Image  # 延迟导入，测试可注入假 image
    img = image
    size = (img.width, img.height)
    pw, ph = preview_size or size
    rect, pts = detection_to_preview(det, size, (pw, ph))
    res = check_framing(rect, pts, pw, ph)
    if not res.ok:
        return res
    if not (det.score >= MANUAL_SHOT_MIN_SCORE):
        return GateResult(False, f"检测分数 {det.score} < {MANUAL_SHOT_MIN_SCORE}（手动拍摄分数门）")
    return res


# ---------------------------------------------------------------- 图像处理管线
def exif_rotation(data: bytes) -> int:
    """J()/s()（:510-528）：仅 EXIF Orientation 3→180, 6→90, 8→270，其余→0。
    与 Pillow exif_transpose 的全量语义不同，这里按 APK 分支精确实现。"""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as img:
            tag = img.getexif().get(274, 1)
    except Exception:
        return 0
    return {3: 180, 6: 90, 8: 270}.get(tag, 0)


def apply_rotation(img, deg: int):
    from PIL import Image
    if deg == 0:
        return img
    return img.transpose({90: Image.Transpose.ROTATE_270,      # Android postRotate(90)＝顺时针
                          180: Image.Transpose.ROTATE_180,
                          270: Image.Transpose.ROTATE_90}[deg])


def mirror_image(img):
    from PIL import ImageOps
    return ImageOps.mirror(img)


def save_jpeg(img, quality: int) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


@dataclass
class FaceImageOutput:
    data: bytes
    steps: List[str] = field(default_factory=list)

    @property
    def base64(self) -> str:
        """Android Base64 NO_WRAP（flag=2）：无换行、保留 padding、无 data: 前缀。"""
        return base64.b64encode(self.data).decode("ascii")


def _ladder(img, limit: int, steps: List[str]) -> bytes:
    """FaceImageCompressor.b 指令转储语义：80,75,…,20 首个 ≤limit 者胜，否则失败。"""
    for q in QUALITY_LADDER:
        data = save_jpeg(img, q)
        if len(data) <= limit:
            steps.append(f"ladder q={q} -> {len(data)}B")
            return data
        steps.append(f"ladder q={q} -> {len(data)}B (> {limit})")
    raise FaceCompressError(f"质量阶梯 {QUALITY_LADDER[0]}..{QUALITY_LADDER[-1]} 全部超过 {limit}B")


def process_face_image(data: bytes, branch: str = "compare",
                       mirror_input: bool = False, apply_exif: bool = True) -> FaceImageOutput:
    """三分支处理链的 Python 等价实现（规则一致；不承诺与 Android Bitmap 字节一致）。

    branch:
      compare  : 正常比对 W() + compressFaceImage（q100→宽>720 缩 720/q90→阶梯）
      register : 注册 B()（q100；>ignoreBy(100) 才压缩）——仅用于离线研究/差分，
                 本仓库策略不自动注册。
      timeout  : 超时抓拍 d0()（q90；>ignoreBy(100) 才压缩）
    未证实环节：Luban 内部算法（ignoreBy 超限后的第一步）。离线基线将其近似为
    “透传到质量阶梯”，并在 steps 里记录 approx 标记——不得据此声称字节一致。
    """
    from PIL import Image
    steps: List[str] = []
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise FaceInputError(f"图片解码失败（对应 :637 提示）: {exc}") from exc

    if apply_exif:
        deg = exif_rotation(data)
        if deg:
            img = apply_rotation(img, deg)
            steps.append(f"exif rotate {deg}")
    if mirror_input:
        img = mirror_image(img)
        steps.append(f"mirror={mirror_input}")

    if branch == "compare":
        q0 = 100
    elif branch == "register":
        q0 = 100
    elif branch == "timeout":
        q0 = 90
    else:
        raise ValueError(f"未知分支: {branch}")
    out = save_jpeg(img, q0)
    steps.append(f"jpeg q={q0} -> {len(out)}B")

    if branch == "compare":
        # FaceImageCompressor.d：仅宽度超 720 才重采样（等比，q90）；≤720 原样返回
        w, h = img.size
        if w > MAX_WIDTH:
            new_h = int(round(h * (MAX_WIDTH / float(w))))
            img2 = img.convert("RGB").resize((MAX_WIDTH, new_h))
            out = save_jpeg(img2, 90)
            steps.append(f"resize w>720 -> 720x{new_h}, jpeg q=90 -> {len(out)}B")
        else:
            steps.append("resize skipped (width<=720)")
        if len(out) > MAX_BYTES:
            steps.append("luban ignoreBy(150)=APPROX(passthrough to ladder)")
            out = _ladder(img.convert("RGB"), MAX_BYTES, steps)
    else:
        limit = LUBAN_100
        if len(out) > limit:
            steps.append(f"luban ignoreBy(100)=APPROX(passthrough to ladder, limit={limit})")
            out = _ladder(img.convert("RGB"), limit, steps)
    return FaceImageOutput(out, steps)


# ---------------------------------------------------------------- 上传（协议层）
def build_compare_body(record_id: Any, face_b64: str) -> str:
    """JTFaceCompareActivity.java:734-737：仅 faceBaseData + recordId 两个键，
    recordId 为字符串（与 start 响应一致），值必须是同一个 record。"""
    return json.dumps({"faceBaseData": face_b64, "recordId": str(record_id)},
                      ensure_ascii=False)


@dataclass
class FaceOutcome:
    """结果分层（评审 §二.3：HTTP200 / 业务 code / 上传完成 / 比对成功是四种状态）。"""
    state: str                 # success | compare_failed | transport_failed | session_terminated
    code: Optional[Any] = None
    msg: str = ""
    detail: str = ""
    attempts: int = 0

    @property
    def retryable(self) -> bool:
        return self.state == "transport_failed"

    @property
    def success(self) -> bool:
        return self.state == "success"


def compare_once(client: YunClient, record_id: Any, face_data: bytes) -> FaceOutcome:
    """一次比对上传。绝不重试（重试属于 FaceVerifier 状态机）。"""
    body = build_compare_body(record_id, base64.b64encode(face_data).decode("ascii"))
    try:
        obj = client.post_json(COMPARE_PATH, body)
    except (HttpStatusException, DecodeException, BusinessException, YunError) as exc:
        # HTTP/解码错误 = “传输失败”；不打印响应片段（safe_snippet 已在异常内脱敏）
        return FaceOutcome("transport_failed", detail=f"{type(exc).__name__}: {exc}")
    code = obj.get("code")
    if code != 200:
        return FaceOutcome("transport_failed", code=code, msg=str(obj.get("msg", "")),
                           detail="外层业务 code!=200（APK 走重试路径 L()）")
    data = obj.get("data")
    if not isinstance(data, dict):
        # APK 此处直接 getData().getStatus()，data 为 null 会抛 NPE 落入 onError→重试；
        # Python 等价：视作可重试的协议异常，并记录偏差说明。
        return FaceOutcome("transport_failed", code=code,
                           detail="code=200 但 data 缺失（APK 将 NPE 进重试，等价处理）")
    status = data.get("status")
    msg = str(data.get("msg", ""))
    if status == "Y":
        return FaceOutcome("success", code=code, msg=msg)
    return FaceOutcome("compare_failed", code=code, msg=msg,
                       detail=f"data.status={status!r}（仅此值以外为失败，不重试）")


# ---------------------------------------------------------------- 重试状态机
@dataclass
class VerifierConfig:
    immediate_retries: int = 3        # L() :607-611 E>=3 上限
    immediate_delay: float = 1.0      # :623 postDelayed 1000ms
    pending_seconds: int = 30         # Z() :701 F=30
    resend_every: int = 3             # f :356 F%3==0
    attempt_fn: Optional[Callable[[bytes], FaceOutcome]] = None  # 注入点（测试）


class FaceVerifier:
    """上传+重试状态机。同一次上传流程重用同一份最终文件字节（f:358 a0(B)）。

    时钟/sleep/上传动作全部注入；通用 HTTP 层依旧无自动重试。
    """

    def __init__(self, client: YunClient, record_id: Any,
                 cfg: Optional[VerifierConfig] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 session_terminated: Optional[Callable[[], bool]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.client = client
        self.record_id = record_id
        self.cfg = cfg or VerifierConfig()
        self._sleep = sleep or time.sleep
        self._terminated = session_terminated or (lambda: False)
        self._log = log or (lambda s: None)
        self.trace: List[str] = []

    def _attempt(self, face_data: bytes) -> Optional[FaceOutcome]:
        """a0()：先查会话终止（:711-714），终止则丢弃本次上传。"""
        if self._terminated():
            self.trace.append("attempt skipped: session terminated")
            return FaceOutcome("session_terminated")
        if self.cfg.attempt_fn is not None:
            return self.cfg.attempt_fn(face_data)
        return compare_once(self.client, self.record_id, face_data)

    def _apply(self, out: Optional[FaceOutcome]) -> Optional[FaceOutcome]:
        """e.onSuccess 的会话终止优先检查（:299-301）：迟到回调丢弃。"""
        if out is None or out.state == "session_terminated":
            return out
        if self._terminated():
            self.trace.append(f"late callback dropped ({out.state})")
            return FaceOutcome("session_terminated")
        return out

    def run(self, face_data: bytes) -> FaceOutcome:
        attempts = 0

        def wrap(o: Optional[FaceOutcome]) -> Optional[FaceOutcome]:
            nonlocal attempts
            if o is not None:
                attempts += 1
                o.attempts = attempts
            return self._apply(o)

        # 首传
        outcome = wrap(self._attempt(face_data))
        if outcome is None or outcome.state in ("success", "session_terminated"):
            return outcome
        if outcome.state == "compare_failed":
            return outcome  # 终端性比对失败：status!=Y 不重试（e.onSuccess :314）

        # L()：立即重试 ≤ immediate_retries 次，间隔 1s
        for i in range(1, self.cfg.immediate_retries + 1):
            self.trace.append(f"immediate retry {i}/{self.cfg.immediate_retries}")
            self._sleep(self.cfg.immediate_delay)
            outcome = wrap(self._attempt(face_data))
            if outcome is None or outcome.state != "transport_failed":
                return outcome or FaceOutcome("session_terminated")

        # Z()+f：30s 等待态，每秒 tick，每 3s 重用同一文件再传（同步模型必不并发）
        f = self.cfg.pending_seconds - 1
        while f > 0:
            if f % self.cfg.resend_every == 0:
                self.trace.append(f"pending resend F={f}")
                outcome = wrap(self._attempt(face_data))
                if outcome is None or outcome.state != "transport_failed":
                    return outcome or FaceOutcome("session_terminated")
            self._sleep(1.0)
            f -= 1

        # 倒计时归零：识别超时(3004)，同 f:370
        return FaceOutcome("transport_failed", msg="识别超时(3004)",
                           detail="30s 等待态耗尽", attempts=attempts)


# ---------------------------------------------------------------- 距离窗口调度
@dataclass
class FaceWindow:
    """FaceRunWindowBean 精简等价（本地状态，不承诺 SQLite 持久化）。"""
    id_str: str
    window_m: float                 # 触发距离（米）；randomList 值(公里)*1000
    is_show: bool = False           # "Y"/"N" → bool
    distance: Optional[float] = None
    voice_second: Optional[float] = None
    upload_success: str = ""
    compare_success: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "FaceWindow":
        return cls(**d)


def windows_from_random_list(record_id: Any, random_list: Sequence[float]) -> List[FaceWindow]:
    """a2() :2989-3013：idStr = str(recordId)+i，window = randomList[i]（公里）→米。"""
    wins = []
    for i, km in enumerate(random_list):
        wins.append(FaceWindow(id_str=f"{record_id}{i}", window_m=float(km) * 1000.0))
    return wins


class WindowTrigger:
    """W1(SportRunMapActivity.java:2854-2900) 等价：距离跨越触发，单窗口在途，防重复。

    on_distance(km) 用 int(km*1000) 米制（Java (int) 截断语义一致）。
    """

    def __init__(self, windows: List[FaceWindow]):
        self.windows = windows
        self.prev_m: Optional[int] = None    # E1
        self.in_flight = False               # F1
        self.current: Optional[FaceWindow] = None  # G1

    def on_distance(self, km: float) -> Optional[FaceWindow]:
        cur_m = int(km * 1000.0)
        if self.prev_m is None:
            self.prev_m = cur_m
        if self.in_flight:                    # :2860-2863 只更新基线
            self.prev_m = cur_m
            return None
        triggered = None
        for w in self.windows:                # :2865-2873
            wm = int(w.window_m)
            if not w.is_show and wm > self.prev_m and wm <= cur_m:
                w.is_show = True
                w.distance = km
                self.current = w
                triggered = w
                break
        self.prev_m = cur_m                   # :2898（含触发轮）
        if triggered is not None:
            self.in_flight = True
        return triggered


def recover_unfinished(windows: List[FaceWindow], face_time: float, now: float) -> Tuple[List[FaceWindow], List[FaceWindow]]:
    """o2() :3337-3368 等价：返回 (可重开的未完成窗口, 应判失败的窗口)。
    now/voice_second 单位秒（elapsedRealtime 语义由注入时钟提供）。
    窗口总时长 = faceTime+4（下限 10 已在装载处处理）。"""
    reopen: List[FaceWindow] = []
    failed: List[FaceWindow] = []
    total = max(float(face_time), FACE_TIME_FLOOR) + WINDOW_LEAD_SECONDS
    for w in windows:
        if not w.is_show:
            continue
        if w.upload_success != "Y":
            if w.voice_second is None:
                failed.append(w)  # 弹出后无后续流程（W1 :2887 reason 语义）
                continue
            remaining = total - (now - float(w.voice_second))
            if remaining < 5:
                failed.append(w)
            else:
                reopen.append(w)
        elif w.compare_success != "Y":
            failed.append(w)
    return reopen, failed


# ---------------------------------------------------------------- 输入源（方案 1/2）
class PhotoSource:
    """方案 1：单张照片输入适配器。

    照片源没有前置相机标志，因此默认不镜像；是否镜像必须由 mirror 显式约定
    （评审 §三 方案 1）。EXIF 按 J() 规则摆正。原始文件不动，全部在内存/临时对象。
    """

    def __init__(self, path: str, mirror: bool = False):
        self.path = path
        self.mirror = mirror

    def load(self) -> Tuple[Any, dict]:
        from PIL import Image
        with open(self.path, "rb") as f:
            raw = f.read()
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:
            raise FaceInputError(f"照片损坏或不是图片: {self.path}: {exc}") from exc
        deg = exif_rotation(raw)
        img = apply_rotation(img, deg)
        if self.mirror:
            img = mirror_image(img)
        meta = {"source": "photo", "path": self.path, "exif_rotation": deg,
                "mirrored": self.mirror, "size": [img.width, img.height]}
        return img, meta

    def raw_bytes(self) -> bytes:
        with open(self.path, "rb") as f:
            return f.read()


def _default_cv2_frame_provider(path: str):
    """视频抽帧默认 provider（需可选依赖 opencv-python；缺失时明确报错，不假装支持）。"""
    try:
        import cv2  # type: ignore
    except ImportError as exc:
        raise FaceInputError(
            "视频抽帧需要可选依赖 opencv-python（pip install opencv-python）；"
            "或注入 frame_provider。") from exc
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise FaceInputError(f"视频无法打开: {path}")
    return cap


class VideoFrameSource:
    """方案 2：自拍视频确定性抽帧，复用与照片相同的下游管线。

    选择规则（输入适配层新增，非客户端行为）：按帧序号升序、每 frame_step 帧
    取样，取第一个通过质量门的清晰帧；没有合格帧 → FaceInputError，不宣称通过。
    不做随机噪声/亮度抖动/EXIF 伪造/“防检测”变换（评审明令禁止）。
    """

    def __init__(self, path: str, frame_provider: Optional[Callable[[str], Any]] = None,
                 frame_step: int = 1, max_frames: int = 2000, mirror: bool = False):
        self.path = path
        self._provider = frame_provider or _default_cv2_frame_provider
        self.frame_step = max(1, int(frame_step))
        self.max_frames = int(max_frames)
        self.mirror = mirror

    def candidates(self):
        cap = self._provider(self.path)
        idx = 0
        try:
            read = getattr(cap, "read", None)
            if callable(read):  # cv2.VideoCapture 风格：(ok, BGR ndarray)
                while idx < self.max_frames:
                    ok, frame = read()
                    if not ok or frame is None:
                        break
                    if idx % self.frame_step == 0:
                        yield idx, self._to_image(frame)
                    idx += 1
            else:               # 注入型 provider：可迭代 ndarray/PIL.Image
                for seq, frame in enumerate(cap):        # type: ignore[arg-type]
                    if seq >= self.max_frames:
                        break
                    if seq % self.frame_step == 0:
                        yield seq, self._to_image(frame)
        finally:
            release = getattr(cap, "release", None)
            if callable(release):
                release()

    @staticmethod
    def _to_image(frame):
        from PIL import Image
        if isinstance(frame, Image.Image):
            return frame
        import numpy as np
        return Image.fromarray(np.asarray(frame)[:, :, ::-1])  # BGR->RGB

    def select(self, gate_fn: Callable[[Any], GateResult]) -> Tuple[Any, dict]:
        """取第一个通过 gate_fn 的帧；记录媒体序号与结果，保证可复现。"""
        tried = 0
        for idx, img in self.candidates():
            tried += 1
            if self.mirror:
                img = mirror_image(img)
            res = gate_fn(img)
            if res.ok:
                meta = {"source": "video_frame", "path": self.path, "frame_index": idx,
                        "frames_tried": tried, "mirrored": self.mirror,
                        "gate": "passed", "note": "输入适配层新增选择规则，非客户端行为"}
                return img, meta
        raise FaceInputError(
            f"视频 {self.path} 无合格帧（尝试 {tried} 帧）。按输入不合格处理，不宣称通过。")


# ---------------------------------------------------------------- 集成：窗口执行器
class FaceRunner:
    """把 输入源→质量门→图像处理→上传状态机 串成一次窗口执行。

    detection: FaceDetection 或 callable(image)->FaceDetection|None。
    无检测能力时（detection=None 且无注入检测器）→ FaceInputError：
    不以“图里有人”替代客户端姿态门（评审 §四 取景质量行）。
    """

    def __init__(self, source: Any, detection: Any = None,
                 preview_size: Optional[Tuple[int, int]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 voice_lead: float = VOICE_LEAD_MS / 1000.0,
                 log: Optional[Callable[[str], None]] = None):
        self.source = source
        self.detection = detection
        self.preview_size = preview_size
        self._sleep = sleep or time.sleep
        self.voice_lead = voice_lead
        self._log = log or (lambda s: None)

    def _detect(self, image) -> Optional[FaceDetection]:
        if self.detection is None:
            return None
        if callable(self.detection):
            return self.detection(image)
        return self.detection

    def build_face_image(self, image, meta: dict) -> FaceImageOutput:
        """图片→最终上传字节。质量门失败在这里抛 FaceInputError。"""
        det = self._detect(image)
        if det is None:
            raise FaceInputError(
                "没有可用检测结果（客户端取景质量门无法执行）。"
                "请提供 --face-detection 标注或注入检测器；不默认放行。")
        img_bytes = save_jpeg(image, 95)  # 中间态；下游链会按分支重编码
        gate = quality_gate(image, det, self.preview_size)
        if not gate.ok:
            raise FaceInputError(f"取景质量门未通过: {gate.reason}")
        # 处理规则从 q100 起点开始（对图片对象直接执行，与 W() 输入摄像头文件等效）
        out = process_face_image(img_bytes, branch="compare", mirror_input=False,
                                 apply_exif=False)
        out.steps.insert(0, f"source={meta.get('source')} gate=pitch={gate.pitch:.3f} "
                            f"yaw={gate.yaw:.3f} roll={gate.roll_deg:.2f}")
        return out

    def run_window(self, window: FaceWindow, client: YunClient, record_id: Any,
                   session_terminated: Optional[Callable[[], bool]] = None,
                   verifier_cfg: Optional[VerifierConfig] = None) -> FaceOutcome:
        """一个窗口的完整执行；语音引导 4s（voice_lead）经注入 sleep，离线可 no-op。"""
        self._sleep(self.voice_lead)
        image, meta = (self.source.select(self._gate_for_video)
                       if isinstance(self.source, VideoFrameSource)
                       else self.source.load())
        self._last_meta = meta
        try:
            face = self.build_face_image(image, meta)
        except FaceInputError as exc:
            window.upload_success = ""
            window.compare_success = "N"
            window.reason = str(exc)
            return FaceOutcome("compare_failed", msg=str(exc))
        verifier = FaceVerifier(client, record_id, cfg=verifier_cfg,
                                sleep=self._sleep, session_terminated=session_terminated,
                                log=self._log)
        outcome = verifier.run(face.data)
        if outcome.state == "success":
            window.upload_success, window.compare_success = "Y", "Y"
        elif outcome.state == "compare_failed":
            window.upload_success, window.compare_success = "Y", "N"
        else:
            window.upload_success, window.compare_success = "N", ""
        window.reason = outcome.msg or outcome.detail
        return outcome

    def _gate_for_video(self, image) -> GateResult:
        from PIL import Image
        size = (image.width, image.height)
        pw, ph = self.preview_size or size
        det = self._detect(image)
        if det is None:
            return GateResult(False, "无检测结果")
        rect, pts = detection_to_preview(det, size, (pw, ph))
        res = check_framing(rect, pts, pw, ph)
        if res.ok and det.score < MANUAL_SHOT_MIN_SCORE:
            return GateResult(False, f"score {det.score} 低于手动拍摄门")
        return res


def load_detection_json(path: str) -> FaceDetection:
    """人工标注检测结果的稳定交换格式（离线可复现输入）：
    {"box":[x1,y1,x2,y2], "points":[[x,y]×5], "score":0.9,
     "space":"image"|"model640", "letterbox":[ox,oy]?}"""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return FaceDetection(box=tuple(d["box"]), points=[tuple(p) for p in d["points"]],
                         score=float(d.get("score", 1.0)), space=d.get("space", "image"),
                         letterbox=(tuple(d["letterbox"]) if d.get("letterbox") else None))

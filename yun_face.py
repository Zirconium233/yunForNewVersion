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
import hashlib
import io
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
        # FaceImageCompressor.d：仅宽度超 720 才重采样（等比，q90）；≤720 原样返回。
        # 阶梯必须作用在"当前处理阶段"的图像上（返修 R5：曾因传回缩放前原图，
        # 导致可能上传超宽图或可压缩输入被误判失败）。
        # 高度取整改 Java Math.round 语义（正数 half-up，floor(x+0.5)），
        # 不用 Python round 的银行家取整。
        w, h = img.size
        cur = img.convert("RGB")
        if w > MAX_WIDTH:
            new_h = max(1, math.floor(h * (MAX_WIDTH / float(w)) + 0.5))
            cur = cur.resize((MAX_WIDTH, new_h))
            out = save_jpeg(cur, 90)
            steps.append(f"resize w>720 -> 720x{new_h}, jpeg q=90 -> {len(out)}B")
        else:
            steps.append("resize skipped (width<=720)")
        if len(out) > MAX_BYTES:
            steps.append("luban ignoreBy(150)=APPROX(passthrough to ladder)")
            out = _ladder(cur, MAX_BYTES, steps)
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
    """结果分层（评审 §二.3：HTTP200 / 业务 code / 上传完成 / 比对成功是四种状态）。

    state ∈ success | compare_failed | transport_failed | session_terminated | expired
    （expired=返修 R2 新增：单调时钟预算内未确认，结果一律不采信。）
    """
    state: str
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

    返修 R2：等待时长改为按经过时间（elapsed）而非 sleep 计数——
    - clock: 单调时钟（可注入；main 路线注入 client.mono/client.now）。缺省为
      “虚拟时钟”：只随注入的 sleep 推进，用于无网络上下文的单测；它无法感知
      attempt_fn 自身耗时，因此有真实预算约束时必须注入外部时钟。
    - deadline: 该绝对时刻后一切结果作废（expired），每次发起请求前与收到回调
      后都检查；网络请求耗时天然计入。
    - 有 deadline 时，请求的读超时被压缩到剩余预算内（client.timeout 临时调整，
      结束恢复）。
    """

    def __init__(self, client: YunClient, record_id: Any,
                 cfg: Optional[VerifierConfig] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 session_terminated: Optional[Callable[[], bool]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 deadline: Optional[float] = None):
        self.client = client
        self.record_id = record_id
        self.cfg = cfg or VerifierConfig()
        self._ext_sleep = sleep or time.sleep
        self._terminated = session_terminated or (lambda: False)
        self._log = log or (lambda s: None)
        self._vnow = 0.0
        self._own_clock = clock is None
        self._ext_clock = clock
        self._deadline = deadline
        self.trace: List[str] = []

    # ---- 预算原语 ----
    def _now(self) -> float:
        """有效时刻 = max(外部时钟, 内部睡眠下界)。

        睡眠声明的时间必然被消耗：外部时钟若未随之推进（静态测试时钟），
        内部下界兜底推进，保证等待循环有界、预算不被“冻结时钟”绕过。
        """
        if self._ext_clock is None:
            return self._vnow
        return max(self._ext_clock(), self._vnow)

    def remaining(self) -> Optional[float]:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - self._now())

    def expired(self) -> bool:
        return self._deadline is not None and self._now() >= self._deadline

    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._vnow += seconds
        self._ext_sleep(seconds)

    # ---- 发起与回调 ----
    def _attempt(self, face_data: bytes) -> Optional[FaceOutcome]:
        """a0()：先查会话终止（:711-714），终止则丢弃本次上传。"""
        if self._terminated():
            self.trace.append("attempt skipped: session terminated")
            return FaceOutcome("session_terminated")
        if self.expired():
            self.trace.append("attempt skipped: window deadline exceeded")
            return self._expired_outcome()
        restore = None
        rem = self.remaining()
        if rem is not None:
            # 二返修 S3：极小预算直接停发，不用固定下限把预算撑大；
            # 连接与读取两段都按剩余预算裁剪（timeout 参数≠整体截止，
            # 回调截止检查仍保留在 _apply 与等待阶段核对中）。
            if rem < REQ_SEND_MIN_S:
                self.trace.append(f"attempt skipped: remaining {rem:.3f}s "
                                  "below send floor")
                return self._expired_outcome()
            if self.client is not None and \
                    isinstance(getattr(self.client, "timeout", None), tuple):
                orig = self.client.timeout
                capped = (min(float(orig[0]), rem), min(float(orig[1]), rem))
                if capped != tuple(orig):
                    restore = orig
                    self.client.timeout = capped
        try:
            if self.cfg.attempt_fn is not None:
                return self.cfg.attempt_fn(face_data)
            return compare_once(self.client, self.record_id, face_data)
        finally:
            if restore is not None:
                self.client.timeout = restore

    def _expired_outcome(self) -> FaceOutcome:
        return FaceOutcome("expired", msg="人脸窗口截止时间已过（单调时钟预算耗尽）",
                           detail="expired：结果一律不采信（返修 R2）")

    def _apply(self, out: Optional[FaceOutcome]) -> Optional[FaceOutcome]:
        """e.onSuccess 的会话终止优先检查（:299-301）：迟到回调丢弃。

        返修 R2：成功回调迟到（返回时已越过 deadline）同样丢弃并作废。
        """
        if out is None or out.state == "session_terminated":
            return out
        if self._terminated():
            self.trace.append(f"late callback dropped ({out.state})")
            return FaceOutcome("session_terminated")
        if out.state == "success" and self.expired():
            self.trace.append("late success dropped: deadline passed during request")
            return self._expired_outcome()
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
        if outcome is None or outcome.state in ("success", "session_terminated", "expired"):
            return outcome
        if outcome.state == "compare_failed":
            return outcome  # 终端性比对失败：status!=Y 不重试（e.onSuccess :314）

        # L()：立即重试 ≤ immediate_retries 次，间隔 1s（有预算时 sleep 被裁剪，
        # 且发起前逐次检查截止）
        for i in range(1, self.cfg.immediate_retries + 1):
            if self.expired():
                return self._expired_outcome()
            self.trace.append(f"immediate retry {i}/{self.cfg.immediate_retries}")
            rem = self.remaining()
            delay = self.cfg.immediate_delay if rem is None else \
                min(self.cfg.immediate_delay, rem)
            self._pause(delay)
            outcome = wrap(self._attempt(face_data))
            if outcome is None or outcome.state != "transport_failed":
                return outcome or FaceOutcome("session_terminated")

        # Z()+f：等待态。以“经过时间”驱动的每 3s 重发调度（网络耗时计入），
        # 上界 = min(外部 deadline, 进入等待态 + pending_seconds)。
        start = self._now()
        end = start + self.cfg.pending_seconds
        if self._deadline is not None:
            end = min(end, self._deadline)
        next_fire = start + self.cfg.resend_every
        while True:
            now = self._now()
            if now >= end:
                break
            wait = min(max(0.0, next_fire - now), max(0.0, end - now))
            self._pause(wait)
            if self.expired() or self._now() >= end:
                break
            self.trace.append(f"pending resend elapsed={self._now() - start:.1f}s")
            outcome = wrap(self._attempt(face_data))
            if outcome is not None and outcome.state == "success" and \
                    self._now() > end:
                # 二返修 S3：请求返回早于窗口总截止也不够——越过
                # 等待阶段预算（pending_seconds）的成功不采信。
                self.trace.append("success beyond pending-phase budget dropped")
                return self._expired_outcome()
            if outcome is None or outcome.state != "transport_failed":
                return outcome or FaceOutcome("session_terminated")
            next_fire += self.cfg.resend_every

        if self.expired():
            return self._expired_outcome()
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

    距离事件与网络批次分离：调用方必须按轨迹点（事件）逐个推进，而不是只喂
    批末里程（返修 R1）。事件语义定义：
    - 首个事件：以跑步起点 0 为基线，直接评估 (0, cur] 内的跨越——
      因此首批就越过窗口不会漏（APK 首次回调里程≈0，两条规则对 APK 等价）；
    - 后续事件：评估 (prev, cur]；点恰在窗口值上算跨越（wm<=cur）；
    - 非单调（cur < prev）：忽略该事件、基线不回退（累计里程本应单调）；
    - 负值：输入错误，直接抛异常（不允许静默吞掉）；
    - 同一事件跨越多个窗口：触发第一个未弹窗口（与 APK 单次回调一致）；
      其余窗口不会自动补触发——由结束前的完整性检查兜底拒绝静默 finish。
    """

    def __init__(self, windows: List[FaceWindow]):
        self.windows = windows
        self.prev_m: Optional[int] = None    # E1
        self.in_flight = False               # F1
        self.current: Optional[FaceWindow] = None  # G1

    def on_distance(self, km: float) -> Optional[FaceWindow]:
        if km < 0:
            raise FaceInputError(f"距离事件非法（负里程）: {km}")
        cur_m = int(km * 1000.0)
        if self.prev_m is not None and cur_m < self.prev_m:
            # 非单调事件：忽略（基线不回退）
            return None
        first_event = self.prev_m is None
        base = 0 if first_event else self.prev_m  # 起点基线 0
        if self.in_flight:                    # :2860-2863 只更新基线
            self.prev_m = cur_m
            return None
        triggered = None
        for w in self.windows:                # :2865-2873
            wm = int(w.window_m)
            if not w.is_show and wm > base and wm <= cur_m:
                w.is_show = True
                w.distance = km
                self.current = w
                triggered = w
                break
        self.prev_m = cur_m                   # :2898（含触发轮）
        if triggered is not None:
            self.in_flight = True
        return triggered

    def required_within(self, max_m: int) -> List[FaceWindow]:
        """实际经过范围 (0, max_m] 内所有本应触发的窗口（含未弹的）。"""
        return [w for w in self.windows if int(w.window_m) <= int(max_m)]

    def incomplete_within(self, max_m: int) -> List[FaceWindow]:
        """经过范围内未完成的窗口：未弹（漏触发）或弹出但比对未成功。"""
        bad = []
        for w in self.required_within(max_m):
            if not w.is_show or w.compare_success != "Y":
                bad.append(w)
        return bad


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

    def select(self, gate_fn: Callable[..., GateResult]) -> Tuple[Any, dict]:
        """取第一个通过 gate_fn 的帧；记录媒体序号与结果，保证可复现。

        Rework R6：gate 支持 (image, frame_index) 两参回调（逐帧检测）；
        兼容单参回调（纯帧内判据，如测试桩）。
        """
        tried = 0
        for idx, img in self.candidates():
            tried += 1
            if self.mirror:
                img = mirror_image(img)
            res = _call_gate(gate_fn, img, idx)
            if res.ok:
                meta = {"source": "video_frame", "path": self.path, "frame_index": idx,
                        "frames_tried": tried, "mirrored": self.mirror,
                        "gate": "passed", "note": "输入适配层新增选择规则，非客户端行为"}
                return img, meta
        raise FaceInputError(
            f"视频 {self.path} 无合格帧（尝试 {tried} 帧）。按输入不合格处理，不宣称通过。")


def _call_gate(gate_fn, img, idx):
    """按回调 arity 兼容调用：2 参（含逐帧检测）或 1 参（帧内判据）。"""
    import inspect
    try:
        params = inspect.signature(gate_fn).parameters
        n_pos = sum(1 for p in params.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
        var = any(p.kind == p.VAR_POSITIONAL for p in params.values())
        if var or n_pos >= 2:
            return gate_fn(img, idx)
    except (TypeError, ValueError):
        pass
    return gate_fn(img)


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
        # S2：预检完成的有效输入缓存 (image, meta, FaceImageOutput)
        self.prepared = None

    def _detect(self, image, frame_index: Optional[int] = None) -> Optional[FaceDetection]:
        """检测解析：FaceDetection（静态）、callable(image)、dict{帧号: FaceDetection}。

        Rework R6：dict 为逐帧静态标注——该帧无标注即无检测（不放行），
        禁止把单帧标注复用到其他帧。
        """
        if self.detection is None:
            return None
        if isinstance(self.detection, dict):
            return self.detection.get(frame_index)
        if callable(self.detection):
            return self.detection(image)
        return self.detection

    def build_face_image(self, image, meta: dict) -> FaceImageOutput:
        """图片→最终上传字节。质量门失败在这里抛 FaceInputError。"""
        det = self._detect(image, (meta or {}).get("frame_index"))
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
                   verifier_cfg: Optional[VerifierConfig] = None,
                   clock: Optional[Callable[[], float]] = None,
                   window_seconds: Optional[float] = None) -> FaceOutcome:
        """一个窗口的完整执行；语音引导 4s（voice_lead）经注入 sleep，离线可 no-op。

        Rework R2：caller 注入 clock（单调预算时钟，勿用 epoch-utc 顶替）与
        window_seconds（=faceTime+4s）。语音引导、取源/预处理、上传状态机共用
        同一 deadline；每段边界与每次请求回调后检查截止；越界一律 expired——
        绝不把 compare_success 置 Y，外层必须停止后续流程。
        """
        clock = clock or time.monotonic
        t0 = clock()
        deadline = None if window_seconds is None else t0 + float(window_seconds)

        def _expired() -> bool:
            return deadline is not None and clock() >= deadline

        def _out_expired(stage: str) -> FaceOutcome:
            out = FaceOutcome("expired", msg=f"窗口 {window.id_str} {stage}时截止已到")
            window.upload_success = window.upload_success or "N"
            window.compare_success = ""       # 未确认：不得标成功
            window.reason = out.msg
            return out

        lead = self.voice_lead
        if deadline is not None:
            lead = min(lead, max(0.0, deadline - clock()))
        if lead > 0:
            self._sleep(lead)
        if _expired():
            return _out_expired("语音引导阶段")
        prepared = getattr(self, "prepared", None)
        if prepared is not None:
            # 二返修 S2：预检阶段已把照片走完"解码→检测匹配→取景门→最终压缩"，
            # 窗口执行直接复用缓存输入，不再中途撞上必然失败的预处理。
            image, meta, face = prepared
            self._last_meta = meta
            self._log("[face] 使用预检完成的有效输入（照片缓存复用）")
        else:
            image, meta = (self.source.select(lambda img, idx: self._gate_for_video(img, idx))
                           if isinstance(self.source, VideoFrameSource)
                           else self.source.load())
            self._last_meta = meta
            if _expired():
                return _out_expired("取源阶段")
            try:
                face = self.build_face_image(image, meta)
            except FaceInputError as exc:
                if _expired():
                    return _out_expired("预处理阶段")
                window.upload_success = ""
                window.compare_success = "N"
                window.reason = str(exc)
                return FaceOutcome("compare_failed", msg=str(exc))
        if _expired():
            return _out_expired("预处理阶段")
        verifier = FaceVerifier(client, record_id, cfg=verifier_cfg,
                                sleep=self._sleep, session_terminated=session_terminated,
                                log=self._log, clock=clock, deadline=deadline)
        outcome = verifier.run(face.data)
        if outcome.state == "success":
            window.upload_success, window.compare_success = "Y", "Y"
        elif outcome.state == "compare_failed":
            window.upload_success, window.compare_success = "Y", "N"
        elif outcome.state == "expired":
            window.upload_success = window.upload_success or "N"
            window.compare_success = ""
            window.reason = outcome.msg or outcome.detail
        else:
            window.upload_success, window.compare_success = "N", ""
        window.reason = outcome.msg or outcome.detail
        return outcome

    def _gate_for_video(self, image, frame_index: Optional[int] = None) -> GateResult:
        from PIL import Image
        size = (image.width, image.height)
        pw, ph = self.preview_size or size
        det = self._detect(image, frame_index)
        if det is None:
            return GateResult(False, "无检测结果")
        rect, pts = detection_to_preview(det, size, (pw, ph))
        res = check_framing(rect, pts, pw, ph)
        if res.ok and det.score < MANUAL_SHOT_MIN_SCORE:
            return GateResult(False, f"score {det.score} 低于手动拍摄门")
        return res


# 二返修 S3：剩余预算低于该下限则不再发起请求（固定 0.5s 下限会把预算撑大，已废弃）
REQ_SEND_MIN_S = 0.05


def load_detection_json(path: str) -> FaceDetection:
    """人工标注检测结果的稳定交换格式（离线可复现输入）：
    {"box":[x1,y1,x2,y2], "points":[[x,y]×5], "score":0.9,
     "space":"image"|"model640", "letterbox":[ox,oy]?}"""
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    return _detection_from_dict(d)


def _detection_from_dict(d: dict) -> FaceDetection:
    return FaceDetection(box=tuple(d["box"]), points=[tuple(p) for p in d["points"]],
                         score=float(d.get("score", 1.0)), space=d.get("space", "image"),
                         letterbox=(tuple(d["letterbox"]) if d.get("letterbox") else None))


# ---------------------------------------------------------------- R6：检测标注包
@dataclass
class DetectionBundle:
    """检测标注的装载结果（Rework R6：区分离线标注模式与可实测输入模式）。

    - static: 单一标注。只允许配合照片源使用，且必须绑定来源（见
      verify_photo_binding）——同一份标注不能宣称代表别的图。
    - frames: 帧号 → 检测。视频源用它做"逐帧有效"的取景判定；未标注帧
      即无检测，质量门不放行。
    - bind_apply: 标注坐标系约定，如 {"after_exif": true, "mirrored": false}
      ——坐标是相对"摆正/镜像之后"的图像声明。
    本轮不移植检测模型：bundle 覆盖不了"真实任意视频每帧都有检测"，
    所以视频只支持带 frames 标注的人工选段（明确声明，不冒充自动检测）。
    """
    static: Optional[FaceDetection] = None
    frames: Dict[int, FaceDetection] = field(default_factory=dict)
    source_sha256: Optional[str] = None
    source_path: Optional[str] = None
    bind_apply: Optional[dict] = None


def load_detection_bundle(path: str) -> DetectionBundle:
    """扩展标注格式（兼容旧版单标注）：
    旧：{"box":..., "points":..., ...}
    新：{"static":{...}, "source_sha256":"...", "source_path":"...",
         "bind_apply":{"after_exif":true,"mirrored":false},
         "frames":{"0":{...},"37":{...}}}
    """
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    frames = {int(k): _detection_from_dict(v) for k, v in (d.get("frames") or {}).items()}
    static_raw = d.get("static")
    if static_raw is None and "box" in d:
        static_raw = d          # 旧版单标注
    static = _detection_from_dict(static_raw) if static_raw else None
    return DetectionBundle(static=static, frames=frames,
                           source_sha256=d.get("source_sha256"),
                           source_path=d.get("source_path"),
                           bind_apply=d.get("bind_apply"))


def sha256_file(path: str) -> str:
    """大文件流式哈希（二返修 S2：内容身份校验，路径字符串不算）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_content_binding(bundle: DetectionBundle, raw: bytes, path: str,
                           kind: str = "photo") -> None:
    """二返修 S2：绑定必须是内容哈希（source_sha256）。

    source_path 只能核对"文件名相同"，无法识别同路径文件被替换，不是有效的
    内容身份；保留为辅助一致性检查，但单独存在即拒绝。
    """
    if not bundle.source_sha256:
        raise FaceInputError(
            f"{kind}标注缺少 source_sha256（内容哈希）绑定：路径字符串不是内容"
            "身份（同路径文件可能被替换），拒绝宣称标注对应当前文件内容")
    got = hashlib.sha256(raw).hexdigest()
    if got != bundle.source_sha256:
        raise FaceInputError(
            f"{kind}标注 source_sha256 与实际文件不符（{got[:12]}… != "
            f"{bundle.source_sha256[:12]}…）：过期/错源标注，拒绝当作检测成功")
    if bundle.source_path and os.path.abspath(bundle.source_path) != os.path.abspath(path):
        raise FaceInputError(
            f"{kind}标注 source_path 与当前输入不一致：{bundle.source_path} != {path}")


def verify_photo_binding(bundle: DetectionBundle, raw: bytes, path: str) -> None:
    """照片标注必须绑定具体来源文件（Rework R6）：sha256 或规范化路径一致。

    未绑定 = 拒绝（不能拿一份无主标注宣称"这张图有人脸且检测到了"）；
    哈希不匹配 = 过期/错图标注，拒绝。坐标系的摆正/镜像约定由 bind_apply
    声明，供标注工具与人工核对使用。
    """
    if bundle.static is None:
        raise FaceInputError("照片源需要静态检测标注（static 或旧版单标注格式）")
    verify_content_binding(bundle, raw, path, kind="photo")
    if bundle.source_path and os.path.abspath(bundle.source_path) != os.path.abspath(path):
        raise FaceInputError(
            f"照片标注 source_path 与当前输入不一致：{bundle.source_path} != {path}")

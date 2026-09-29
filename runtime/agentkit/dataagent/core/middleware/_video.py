# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
# 本文件中的模型可见文案原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""把视频字节抽成带时间戳的 JPEG 内容块。

``av`` 和 Pillow 都是可选依赖。导入本模块不会加载它们。
``video_dependencies_available`` 只问这两个名字能不能被找到；
真正抽帧时才导入。找不到、打不开、解不出窗口里的帧，都变成
``VideoExtractionError``。采样率由调用方传入，本模块不设默认值。
"""

import base64
import functools
import importlib.util as import_util
import io, logging
import math, time

logger = logging.getLogger(__name__)

# 运行时没有 langchain 的 ContentBlock 类型；标注落到 dict 上。
ContentBlock = dict

MISSING_VIDEO_HINT = "Reading video files requires the optional video dependencies. Install them with `uv add 'deepagents[video]'`."

_PROBE_FAILED = "Video dependency probe failed; treating the [video] extra as unavailable."

_NO_TIME_BASE = "Video stream has no time_base; cannot determine frame timestamps"

_ZERO_TIME_BASE = "Video stream time_base is zero; cannot determine frame timestamps"

_NO_VIDEO_STREAM = "Video payload contains no video stream"

MAX_VIDEO_SAMPLED_FRAMES = 64
MAX_VIDEO_FRAME_PIXELS = 1920 * 1080
MAX_VIDEO_FRAME_SIDE = 4096
MAX_VIDEO_OUTPUT_WIDTH = 1920
MAX_VIDEO_OUTPUT_HEIGHT = 1080
MAX_VIDEO_EMITTED_BYTES = 4 * 1024 * 1024
MAX_VIDEO_DECODE_SECONDS = 10.0

_JPEG_ENCODE_QUALITY = 85

_HOUR_MS = 3_600_000
_MINUTE_MS = 60_000
_SECOND_MS = 1000
_EMIT_SLACK = 1e-6


class VideoExtractionError(RuntimeError):
    """抽帧失败。``str(异常)`` 会进模型看见的工具错误。"""


@functools.lru_cache(maxsize=1)
def video_dependencies_available() -> bool:
    """``av`` 与 ``PIL.Image`` 是否都能被找到。

    只查规格，不真正导入。``av`` 找不到就不再查 Pillow。
    探针抛出 ``ImportError`` 或 ``ValueError`` 时记一条警告并返回假。
    结果缓存在无参的 ``lru_cache`` 里，``maxsize`` 为 1。
    """
    try:
        av_spec = import_util.find_spec("av")
        if av_spec is None:
            return False
        pillow_spec = import_util.find_spec("PIL.Image")
    except (ImportError, ValueError):
        logger.warning(_PROBE_FAILED, exc_info=True)
        return False
    return pillow_spec is not None


def _reject_bad_window(offset_seconds, duration_seconds, sampling_rate) -> None:
    """窗口非法时抛 ``ValueError``。调用方再包成 ``VideoExtractionError``。"""
    origin_negative = offset_seconds < 0
    if origin_negative:
        complaint = f"offset_seconds must be >= 0, got {offset_seconds!r}"
        raise ValueError(complaint)
    rate_not_positive = sampling_rate <= 0
    if rate_not_positive:
        complaint = f"sampling_rate must be > 0, got {sampling_rate!r}"
        raise ValueError(complaint)
    span_not_positive = duration_seconds <= 0
    if span_not_positive:
        complaint = f"duration_seconds must be > 0, got {duration_seconds!r}"
        raise ValueError(complaint)


def _load_pyav():
    """懒加载顶层模块 ``av``。失败时带上安装说明。"""
    try:
        import av as pyav
    except ImportError as failure:
        detail = f"{MISSING_VIDEO_HINT} (underlying error: {failure})"
        raise VideoExtractionError(detail) from failure
    return pyav


def _backend_failure_types(pyav):
    """解码器失败里，哪些类型要收成 ``VideoExtractionError``。"""
    found: list[type[BaseException]] = [OSError]
    namespace = getattr(pyav, "error", None)
    for label in ("FFmpegError", "InvalidDataError"):
        candidate = getattr(namespace, label, None)
        if not isinstance(candidate, type):
            continue
        if not issubclass(candidate, BaseException):
            continue
        already_covered = any(issubclass(candidate, earlier) for earlier in found)
        if already_covered:
            continue
        found.append(candidate)
    return tuple(found)


def _open_container(pyav, content):
    """用内存字节打开容器。后端错误换成固定前缀。"""
    try:
        payload = io.BytesIO(content)
        return pyav.open(payload)
    except _backend_failure_types(pyav) as failure:
        detail = f"Failed to open video payload: {failure}"
        raise VideoExtractionError(detail) from failure


def _first_video_stream(container):
    """第一条 ``type == "video"`` 的流。后面的流不再取。"""
    chosen = None
    for stream in container.streams:
        if stream.type == "video":
            chosen = stream
            break
    if chosen is None:
        raise VideoExtractionError(_NO_VIDEO_STREAM)
    return chosen


def _start_units(video_stream) -> int:
    """流起点，时间基单位。缺省或 ``None`` 都是 0。"""
    raw = getattr(video_stream, "start_time", None)
    if raw is None:
        return 0
    return int(raw)


def _time_base_seconds(video_stream) -> float:
    """把 ``time_base`` 收成秒。``None`` 和 0 是两句不同的错误。"""
    raw = video_stream.time_base
    if raw is None:
        raise VideoExtractionError(_NO_TIME_BASE)
    seconds = float(raw)
    if seconds == 0.0:
        raise VideoExtractionError(_ZERO_TIME_BASE)
    return seconds


def _clock_text(seconds: float) -> str:
    """秒数格式化成 ``HH:MM:SS.mmm``。负数先夹到 0。"""
    if seconds < 0:
        seconds = 0.0
    millis_per_second = 1000
    total_ms = round(seconds * millis_per_second)
    hours, remainder = divmod(total_ms, _HOUR_MS)
    minutes, remainder = divmod(remainder, _MINUTE_MS)
    secs, millis = divmod(remainder, _SECOND_MS)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _seconds_from_start(frame, *, time_base, stream_start_seconds):
    """帧相对视频起点的秒。有 ``pts`` 就不再读 ``time``。"""
    pts = getattr(frame, "pts", None)
    if pts is not None:
        raw = float(pts) * time_base - stream_start_seconds
        return max(0.0, raw)
    frame_time = getattr(frame, "time", None)
    if frame_time is None:
        return None
    raw = float(frame_time) - stream_start_seconds
    return max(0.0, raw)


def _measured_sides(frame):
    """宽和高都有才返回整数对。两个属性都会读。"""
    width = getattr(frame, "width", None)
    height = getattr(frame, "height", None)
    missing_side = width is None or height is None
    if missing_side:
        return None
    return int(width), int(height)


def _reject_huge_side(width: int, height: int) -> None:
    """边长超过上限就拒绝。非正边长留给后面的图像。"""
    if width <= 0 or height <= 0:
        return
    if width > MAX_VIDEO_FRAME_SIDE or height > MAX_VIDEO_FRAME_SIDE:
        detail = f"Video frame dimensions {width}x{height} exceed the maximum {MAX_VIDEO_FRAME_SIDE}px side"
        raise VideoExtractionError(detail)


def _jpeg_bytes(frame) -> bytes:
    """把一帧收成 JPEG。Pillow 只在这里导入。"""
    try:
        from PIL import Image
    except ImportError as failure:
        detail = f"{MISSING_VIDEO_HINT} (underlying error: {failure})"
        raise VideoExtractionError(detail) from failure

    pair = _measured_sides(frame)
    if pair is not None:
        _reject_huge_side(*pair)

    if hasattr(frame, "to_image"):
        image = frame.to_image()
    else:
        pixels = frame.to_ndarray(format="rgb24")
        image = Image.fromarray(pixels)

    _reject_huge_side(*image.size)
    width, height = image.size
    if (
        width * height > MAX_VIDEO_FRAME_PIXELS
        or width > MAX_VIDEO_OUTPUT_WIDTH
        or height > MAX_VIDEO_OUTPUT_HEIGHT
    ):
        image = image.copy()
        image.thumbnail(
            (MAX_VIDEO_OUTPUT_WIDTH, MAX_VIDEO_OUTPUT_HEIGHT),
            Image.Resampling.LANCZOS,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=_JPEG_ENCODE_QUALITY)
    return buffer.getvalue()


def _ensure_within_budget(deadline_seconds) -> None:
    """墙钟严格超过截止点才停。等于截止点还继续。"""
    if deadline_seconds is None:
        return
    if time.monotonic() > deadline_seconds:
        detail = f"Video decoding exceeded the {MAX_VIDEO_DECODE_SECONDS:.1f}s safety budget"
        raise VideoExtractionError(detail)


def _append_truncation(blocks, last_emitted_seconds) -> None:
    """帧数或字节到顶时，补一条给模型看的续读说明。"""
    last_ts = _clock_text(last_emitted_seconds)
    head = f"Coverage truncated at t={last_ts}: the output or frame cap was reached "
    middle = f"before the full window was decoded. Continue from "
    tail = f"offset={last_emitted_seconds:.3f} to see the remaining frames."
    blocks.append({"type": "text", "text": head + middle + tail})


def _blocks_for_window(
    decoded_frames,
    *,
    offset_seconds,
    duration,
    rate,
    time_base,
    stream_start_seconds,
    deadline_seconds,
    failure_types,
):
    """在半开窗口里按采样率挑帧，并守住帧数和字节上限。"""
    frame_interval_seconds = 1.0 / rate
    end_seconds = offset_seconds + float(duration)
    next_emit_seconds = offset_seconds
    blocks: list[ContentBlock] = []
    emitted_frames = 0
    emitted_bytes = 0
    last_emitted_seconds = None
    truncated = False
    try:
        for frame in decoded_frames:
            _ensure_within_budget(deadline_seconds)
            frame_seconds = _seconds_from_start(
                frame,
                time_base=time_base,
                stream_start_seconds=stream_start_seconds,
            )
            if frame_seconds is None:
                continue
            reached_end = frame_seconds >= end_seconds
            if reached_end:
                break
            too_early = frame_seconds + _EMIT_SLACK < next_emit_seconds
            if too_early:
                continue
            if emitted_frames >= MAX_VIDEO_SAMPLED_FRAMES:
                truncated = True
                break

            jpeg_bytes = _jpeg_bytes(frame)
            encoded = base64.b64encode(jpeg_bytes)
            stamp = _clock_text(frame_seconds)
            caption = f"Frame at t={stamp}"
            next_block_bytes = len(caption.encode()) + len(encoded)
            over_budget = emitted_bytes + next_block_bytes > MAX_VIDEO_EMITTED_BYTES
            if over_budget and emitted_frames == 0:
                detail = f"Video frame output exceeded the {MAX_VIDEO_EMITTED_BYTES} byte safety budget before emitting a frame"
                raise VideoExtractionError(detail)
            if over_budget:
                truncated = True
                break

            text_block = {"type": "text", "text": caption}
            image_block = {"type": "image", "base64": encoded.decode("ascii"), "mime_type": "image/jpeg"}
            blocks.extend((text_block, image_block))
            emitted_frames += 1
            emitted_bytes += next_block_bytes
            last_emitted_seconds = frame_seconds
            slot = math.floor((frame_seconds - offset_seconds) / frame_interval_seconds)
            emitted_index = slot + 1
            stepped = next_emit_seconds + frame_interval_seconds
            indexed = offset_seconds + frame_interval_seconds * emitted_index
            next_emit_seconds = max(stepped, indexed)
    except failure_types as problem:
        detail = f"Failed to decode video frames: {problem}"
        raise VideoExtractionError(detail) from problem

    if truncated and last_emitted_seconds is not None:
        _append_truncation(blocks, last_emitted_seconds)
    return blocks


def _pull_window(container, *, offset_seconds, duration, rate, failure_types):
    """定位视频流、按起点 seek，再把解码器交给抽帧循环。"""
    video_stream = _first_video_stream(container)
    time_base = _time_base_seconds(video_stream)
    stream_start_seconds = _start_units(video_stream) * time_base
    if offset_seconds > 0:
        start_pts = _start_units(video_stream) + int(offset_seconds / time_base)
        container.seek(
            start_pts,
            any_frame=False,
            backward=True,
            stream=video_stream,
        )
    decoded = container.decode(video_stream)
    deadline_seconds = time.monotonic() + MAX_VIDEO_DECODE_SECONDS
    return _blocks_for_window(
        decoded,
        offset_seconds=offset_seconds,
        duration=duration,
        rate=rate,
        time_base=time_base,
        stream_start_seconds=stream_start_seconds,
        deadline_seconds=deadline_seconds,
        failure_types=failure_types,
    )


def extract_video_frames(content: bytes, *, offset_seconds: float, duration_seconds: float, sampling_rate: float) -> list[ContentBlock]:
    """从 ``content`` 抽出 ``[起点, 起点+时长)`` 里的 JPEG 块。

    文本块和图像块交替。原始视频字节不会返回。
    窗口不合法、依赖缺失、容器或解码失败，都是 ``VideoExtractionError``。
    """
    try:
        _reject_bad_window(offset_seconds, duration_seconds, sampling_rate)
    except ValueError as problem:
        raise VideoExtractionError(str(problem)) from problem

    rate = float(sampling_rate)
    duration = float(duration_seconds)
    pyav = _load_pyav()
    container = _open_container(pyav, content)
    failure_types = _backend_failure_types(pyav)
    try:
        try:
            blocks = _pull_window(
                container,
                offset_seconds=offset_seconds,
                duration=duration,
                rate=rate,
                failure_types=failure_types,
            )
        except failure_types as problem:
            detail = f"Failed to decode video frames: {problem}"
            raise VideoExtractionError(detail) from problem
    finally:
        container.close()

    window_empty = not blocks
    if window_empty:
        end_seconds = offset_seconds + duration
        detail = f"No frames decoded for window [{offset_seconds:.3f}s, {end_seconds:.3f}s)"
        raise VideoExtractionError(detail)
    return list(blocks)

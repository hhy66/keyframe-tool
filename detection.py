"""检测算法：逐帧颜色差分、切换帧筛选、截取策略与截图相似度。不涉及 HTTP 和工作区。"""

from __future__ import annotations

import math
from array import array
from pathlib import Path

import cv2
import numpy as np

SCAN_WIDTH = 160  # 检测小图最长边；横竖屏均有界


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------------- 基础工具


def _small_color(frame):
    """保留颜色差异，先缩小再比较；不放大小视频。"""
    h, w = frame.shape[:2]
    scale = min(1.0, SCAN_WIDTH / max(h, w))
    return cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)


def _mad(a, b):
    """小图所有颜色通道的平均绝对差（0~255）。"""
    return float(np.mean(cv2.absdiff(a, b)))


def _fmt_time(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _grab_frame_at(cap, target_frame):
    """读取指定的零基帧编号，不增加时间偏移，不用末帧掩盖读取失败。"""
    target_frame = int(target_frame)
    if not cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame):
        return None
    # POS_FRAMES 表示下一次 read 的帧号；某些后端可能定位到更早的帧。
    position = cap.get(cv2.CAP_PROP_POS_FRAMES)
    if not math.isfinite(position) or position > target_frame + 0.5:
        return None
    for _ in range(max(0, target_frame - round(position))):
        if not cap.grab():
            return None
    ok, frame = cap.read()
    return frame if ok else None


PICKS = ("first", "settle", "sharp")
SETTLE_SECONDS = 0.5  # “切换后 0.5 秒”：避开切换瞬间的残影和压缩块
SHARP_WINDOW = 1.5  # “最清晰”：只在镜头开头这段时间内挑选


def _sharpness(frame):
    """拉普拉斯方差：数值越大画面越清晰。缩到最长边 320 以控制耗时。"""
    h, w = frame.shape[:2]
    scale = min(1.0, 320 / max(h, w))
    small = cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def _pick_frame(cap, start, stop, pick, fps):
    """按截取策略选出镜头 [start, stop] 中的一帧，返回 (帧号, 图像)；读取失败时图像为 None。"""
    if pick == "settle":
        target = max(start, min(stop, start + round(SETTLE_SECONDS * fps)))
        return target, _grab_frame_at(cap, target)
    first = _grab_frame_at(cap, start)
    if pick != "sharp" or first is None:
        return start, first
    best_score, best_index, best = _sharpness(first), start, first
    for index in range(start + 1, min(stop, start + max(1, round(SHARP_WINDOW * fps))) + 1):
        ok, frame = cap.read()
        if not ok:
            break
        score = _sharpness(frame)
        # 需要明显更清晰才换，避免在噪点上来回跳，也尽量靠近切换点。
        if score > best_score * 1.05:
            best_score, best_index, best = score, index, frame
    return best_index, best


def _write_jpeg(path, frame, quality):
    # imencode + Python 文件 I/O 同时支持 Windows 中文路径，并检查编码/写入错误。
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("图片编码失败")
    path.write_bytes(encoded.tobytes())


def _fps(cap):
    value = cap.get(cv2.CAP_PROP_FPS)
    return float(value) if math.isfinite(value) and 0 < value <= 1000 else 30.0


def probe(path: Path):
    """读取视频基本信息；无法解码时返回 None。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    fps = _fps(cap)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    ok, _ = cap.read()
    cap.release()
    if not ok or w <= 0 or h <= 0:
        return None
    return {"fps": float(fps), "frames": total, "width": w, "height": h, "duration": (total / fps) if total else 0.0}


# ---------------------------------------------------------------- 检测算法


def _scan_pass1(video_path: str, job: dict, cache: dict):
    """边解码边计算差异，只保留两张小图和紧凑的每帧数值数组。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("无法解码该视频（编码可能不支持）")
    fps = _fps(cap)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    times, diffs, bridges = array("d"), array("f"), array("f")
    prev = before_prev = None
    estimated_time = False
    n = 0
    try:
        while True:
            if job.get("cancel"):
                raise _Cancelled()
            ok, frame = cap.read()
            if not ok:
                break
            timestamp = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if not math.isfinite(timestamp) or timestamp < 0 or (times and timestamp <= times[-1]):
                timestamp = times[-1] + 1 / fps if times else 0.0
                estimated_time = True
            times.append(timestamp)
            small = _small_color(frame)
            diffs.append(_mad(prev, small) if prev is not None else 0.0)
            bridges.append(_mad(before_prev, small) if before_prev is not None else 255.0)
            before_prev, prev = prev, small
            n += 1
            if n % 150 == 0:
                pct = 5 + 62 * (n / max(1, total))
                job["pct"] = min(pct, 66.0)
                job["stage"] = f"扫描画面差异… {n:,}/{total:,} 帧"
    finally:
        cap.release()

    if n == 0:
        raise RuntimeError("视频没有任何可解码的帧")
    # 容器帧数可能有少量估算误差，但明显缺帧不能当作完整视频发布。
    if total > 0 and total - n > max(2, math.ceil(total * 0.01)):
        raise RuntimeError(f"视频解码提前结束（预计 {total} 帧，实际 {n} 帧），文件可能损坏；已保留上次结果")
    cache.update(
        {
            "fps": float(fps),
            "frames": n,
            "times": np.frombuffer(times, dtype=np.float64),
            "diffs": np.frombuffer(diffs, dtype=np.float32),
            "bridges": np.frombuffer(bridges, dtype=np.float32),
            "estimated_time": estimated_time,
        }
    )
    return True


def _select_entries(video_path: str, job: dict, ses: dict):
    """使用缓存挑选切换帧，返回 [(kind, frame_index)]，无需再次解码。"""
    cache = ses["cache"]
    diffs = cache["diffs"]
    fps = cache["fps"]
    sens = job["sensitivity"]
    mult = 8.0 - sens / 100.0 * 6.0  # 0=保守(阈值高) 100=敏感(阈值低)
    med = float(np.median(diffs[1:])) if len(diffs) > 1 else 0.0
    base = med
    if base < 0.5:  # 画面基本静止的视频，基准抬高一点
        base = max(float(np.percentile(diffs, 65)), 0.5)
    thr = max(base * mult, 2.0)

    candidates = diffs > thr
    candidates[0] = False
    if job.get("suppress_flash", True) and len(diffs) > 2:
        # A -> 闪光 -> A：相邻两次突变很强，但跨过闪光后与原画面接近。
        flashes = (
            np.flatnonzero(
                candidates[1:-1]
                & candidates[2:]
                & (cache["bridges"][2:] < np.maximum(2.0, np.minimum(diffs[1:-1], diffs[2:]) * 0.25))
            )
            + 1
        )
        candidates[flashes] = False
        candidates[flashes + 1] = False
    times = cache["times"]
    exact = []
    gap = job.get("min_scene_seconds", 0.1)
    # 按时间线性筛选，避免原来的两两比较开销；同一邻近峰群保留更强的一帧。
    for index in np.flatnonzero(candidates):
        i = int(index)
        if job.get("cancel"):
            raise _Cancelled()
        if exact and times[i] - times[exact[-1]] < gap - 1e-8:
            if diffs[i] > diffs[exact[-1]]:
                exact[-1] = i
        else:
            exact.append(i)

    dur = float(times[-1] + 1 / fps)
    entries = []
    if job.get("include_ends"):
        entries.append(("head", 0))
    entries.extend(("cut", i) for i in exact)
    if job.get("include_ends"):
        tail = cache["frames"] - 1
        if not entries or entries[-1][1] != tail:
            entries.append(("tail", tail))
    # 手动改动优先；重新检测只改变自动候选，不丢弃用户选定的画面。
    adjustments = ses.get("manual_adjustments", {})
    merged = {i: kind for kind, i in entries if i not in adjustments}
    merged.update({i: "adjusted" for i in adjustments.values()})
    merged.update({i: "manual" for i in ses.get("manual_additions", set())})
    return [(kind, i) for i, kind in sorted(merged.items())], dur


def _signature(image):
    """32×18 的缩略颜色图：轻微运动和压缩噪点影响很小，换了画面则差异明显。"""
    return cv2.resize(image, (32, 18), interpolation=cv2.INTER_AREA)


def _similarity(a, b):
    """0～100：两张缩略图的平均色差换算成百分比，100 表示完全相同。"""
    return round(max(0.0, 100.0 - float(np.mean(cv2.absdiff(a, b))) * 100.0 / 48.0), 1)

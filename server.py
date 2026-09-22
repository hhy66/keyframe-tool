# -*- coding: utf-8 -*-
"""
视频 Cut 关键帧截取工具 —— 本地服务
====================================
对上传的视频做「镜头切换检测」(cut / scene change)：
  顺序扫描：逐帧计算缩小画面的颜色差，只缓存分数和时间戳
  筛选：抑制单帧闪光，按灵敏度和最小镜头间隔选择切换帧
  输出：每个新镜头第一帧（即 cut 后的首帧）作为关键帧参考图，可打包 zip 下载

全部处理在本地完成，不上传任何数据。无需安装 ffmpeg（OpenCV 自带解码）。
运行方式：双击「启动工具.bat」，或 .venv\\Scripts\\python.exe server.py
"""
from __future__ import annotations

from array import array
import math
import os
import shutil
import sys
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

if getattr(sys, "frozen", False):
    # 打包成 exe：代码与静态资源在 PyInstaller 临时解包目录，工作数据放 exe 旁边
    APP_DIR = Path(sys.executable).resolve().parent
    BASE = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent
    BASE = APP_DIR
WORK = APP_DIR / "work"
STATIC = BASE / "static"
WORK.mkdir(exist_ok=True)

ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
               ".ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".ogv"}

SCAN_WIDTH = 160          # 检测小图最长边；横竖屏均有界
MAX_EXPORT = 500          # 单次打包上限

app = FastAPI(title="视频关键帧截取工具")

_lock = threading.Lock()
_sessions: dict[str, dict] = {}   # session_id -> {video_path, video_name, meta, cache}
_jobs: dict[str, dict] = {}       # session_id -> job


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------------- 基础工具

def _small_color(frame):
    """保留颜色差异，先缩小再比较；不放大小视频。"""
    h, w = frame.shape[:2]
    scale = min(1.0, SCAN_WIDTH / max(h, w))
    return cv2.resize(frame, (max(1, round(w * scale)), max(1, round(h * scale))),
                      interpolation=cv2.INTER_AREA)


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
    return {"fps": float(fps), "frames": total, "width": w, "height": h,
            "duration": (total / fps) if total else 0.0}


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
    cache.update({
        "fps": float(fps),
        "frames": n,
        "times": np.frombuffer(times, dtype=np.float64),
        "diffs": np.frombuffer(diffs, dtype=np.float32),
        "bridges": np.frombuffer(bridges, dtype=np.float32),
        "estimated_time": estimated_time,
    })
    return True


def _select_entries(video_path: str, job: dict, ses: dict):
    """使用缓存挑选切换帧，返回 [(kind, frame_index)]，无需再次解码。"""
    cache = ses["cache"]
    diffs = cache["diffs"]
    fps = cache["fps"]
    sens = job["sensitivity"]
    mult = 8.0 - sens / 100.0 * 6.0          # 0=保守(阈值高) 100=敏感(阈值低)
    med = float(np.median(diffs[1:])) if len(diffs) > 1 else 0.0
    base = med
    if base < 0.5:                            # 画面基本静止的视频，基准抬高一点
        base = max(float(np.percentile(diffs, 65)), 0.5)
    thr = max(base * mult, 2.0)

    candidates = diffs > thr
    candidates[0] = False
    if job.get("suppress_flash", True) and len(diffs) > 2:
        # A -> 闪光 -> A：相邻两次突变很强，但跨过闪光后与原画面接近。
        flashes = np.flatnonzero(candidates[1:-1] & candidates[2:] &
                                 (cache["bridges"][2:] < np.maximum(2.0, np.minimum(diffs[1:-1], diffs[2:]) * 0.25))) + 1
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


def _run_job(sid: str, job: dict, ses: dict) -> None:
    # 同一视频只允许一个工作线程持有解码器与缓存，旧线程只操作自己的 job。
    with ses["worker_lock"]:
        _process_job(sid, job, ses)


def _process_job(sid: str, job: dict, ses: dict) -> None:
    fdir = tdir = None
    try:
        if job.get("cancel"):
            raise _Cancelled()
        sdir = WORK / sid
        if "diffs" not in ses.setdefault("cache", {}):
            job["stage"] = "逐帧扫描颜色差异（大文件可能需要几分钟）"
            job["pct"] = 4.0
            _scan_pass1(str(ses["video_path"]), job, ses["cache"])
        if job.get("cancel"):
            raise _Cancelled()

        job["stage"] = "筛选切换帧并抑制闪光…"
        job["pct"] = 68.0
        entries, dur = _select_entries(str(ses["video_path"]), job, ses)
        if job.get("cancel"):
            raise _Cancelled()

        cache = ses["cache"]
        run = job["run"]
        fdir = sdir / "frames" / run          # 原分辨率原图
        tdir = sdir / "thumbs" / run          # 页面预览小图
        # 结果完成前不覆盖或删除已发布的图片，旧页面和下载仍可使用。
        fdir.mkdir(parents=True, exist_ok=True)
        tdir.mkdir(parents=True, exist_ok=True)
        job["stage"] = "截取关键帧原图…"
        job["pct"] = 76.0
        cap = cv2.VideoCapture(str(ses["video_path"]))
        cuts: list[dict] = []
        total_n = len(entries)
        try:
            for i, (kind, frame_index) in enumerate(entries):
                if job.get("cancel"):
                    raise _Cancelled()
                job["pct"] = 78 + 16 * (i / max(1, total_n))
                job["stage"] = f"截取关键帧原图… {i + 1}/{total_n}"
                frame = _grab_frame_at(cap, frame_index)
                if frame is None:
                    raise RuntimeError(f"第 {frame_index + 1} 帧读取失败，已保留上次完整结果")
                _write_jpeg(fdir / f"{i}.jpg", frame, 95)
                # 页面预览小图（最长边 720）
                hh, ww = frame.shape[:2]
                if max(hh, ww) > 720:
                    scale = 720 / max(hh, ww)
                    prev = cv2.resize(frame, (int(ww * scale), int(hh * scale)))
                else:
                    prev = frame
                _write_jpeg(tdir / f"{i}.jpg", prev, 85)
                t = float(cache["times"][frame_index])
                cuts.append({"kind": kind, "frame_index": frame_index, "time": round(t, 6),
                             "label": _fmt_time(t)})
        finally:
            cap.release()

        if job.get("cancel"):
            raise _Cancelled()
        job["pct"] = 97.0
        job["stage"] = "完成"
        fps_r = cache["fps"]
        result = {
            "run": run,
            "video_name": ses["video_name"],
            "params": {"sensitivity": job["sensitivity"],
                       "include_ends": bool(job.get("include_ends")),
                       "min_scene_seconds": job["min_scene_seconds"],
                       "suppress_flash": job["suppress_flash"]},
            "meta": {
                "width": ses["meta"].get("width"),
                "height": ses["meta"].get("height"),
                "fps": round(fps_r, 3),
                "frames": int(cache["frames"]),
                "duration": round(dur, 6),
                "estimated_time": cache["estimated_time"],
                "size_mb": ses["meta"].get("size_mb"),
            },
            "cuts": cuts,
            "thumbs": [f"/api/thumb/{sid}/{job['run']}/{i}" for i in range(len(cuts))],
            "frames": [f"/api/frame/{sid}/{job['run']}/{i}" for i in range(len(cuts))],
        }
        with _lock:
            if job.get("cancel") or _jobs.get(sid) is not job:
                raise _Cancelled()
            ses.setdefault("results", {})[run] = result
            job["result"] = result
            job["pct"] = 100.0
            job["status"] = "done"
    except _Cancelled:
        job["status"] = "cancelled"
        job["stage"] = "已取消"
    except Exception as exc:                  # noqa: BLE001
        job["status"] = "error"
        job["stage"] = "失败"
        job["error"] = str(exc)
    finally:
        if job["status"] != "done":
            for directory in (fdir, tdir):
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------- HTTP API

def _session(sid):
    ses = _sessions.get(sid)
    if ses is None:
        raise HTTPException(404, "会话不存在或已过期，请重新上传视频")
    return ses


def _preview_ready(sid, ses):
    if (_jobs.get(sid) or {}).get("status") == "running":
        raise HTTPException(409, "正在分析，请完成后再预览或编辑")
    if "times" not in ses.get("cache", {}):
        raise HTTPException(409, "请先完成视频分析")


def _integer(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(400, "帧号与条目索引必须是整数")
    return value


@app.get("/api/video/{sid}")
def video(sid: str):
    return FileResponse(_session(sid)["video_path"])


@app.get("/api/preview/{sid}")
def preview(sid: str, frame_index: int | None = None, time: float | None = None):
    ses = _session(sid)
    if not ses["worker_lock"].acquire(blocking=False):
        raise HTTPException(409, "视频正忙，请稍后再试")
    try:
        _preview_ready(sid, ses)
        times = ses["cache"]["times"]
        if (frame_index is None) == (time is None):
            raise HTTPException(400, "请只提供帧号或时间其中之一")
        if frame_index is not None:
            index = _integer(frame_index)
        else:
            try:
                timestamp = float(time)
                if not math.isfinite(timestamp):
                    raise ValueError()
            except (TypeError, ValueError):
                raise HTTPException(400, "时间必须为有限数值")
            index = int(np.searchsorted(times, timestamp, side="right")) - 1
        index = min(len(times) - 1, max(0, index))
        timestamp = float(times[index])
        return {"frame_index": index, "time": timestamp, "label": _fmt_time(timestamp),
                "image_url": f"/api/preview-image/{sid}/{index}", "frames": len(times)}
    finally:
        ses["worker_lock"].release()


@app.get("/api/preview-image/{sid}/{frame_index}")
def preview_image(sid: str, frame_index: int):
    ses = _session(sid)
    if not ses["worker_lock"].acquire(blocking=False):
        raise HTTPException(409, "视频正忙，请稍后再试")
    try:
        _preview_ready(sid, ses)
        if not 0 <= _integer(frame_index) < ses["cache"]["frames"]:
            raise HTTPException(400, "帧号超出视频范围")
        cap = cv2.VideoCapture(str(ses["video_path"]))
        try:
            image = _grab_frame_at(cap, frame_index)
        finally:
            cap.release()
        if image is None:
            raise HTTPException(422, "该帧无法解码，请选择相邻画面")
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise HTTPException(500, "预览图片编码失败")
        return Response(encoded.tobytes(), media_type="image/jpeg")
    finally:
        ses["worker_lock"].release()


@app.post("/api/edit/{sid}")
def edit(sid: str, payload: dict):
    ses = _session(sid)
    if not ses["worker_lock"].acquire(blocking=False):
        raise HTTPException(409, "视频正忙，请稍后再试")
    directories = []
    published = False
    try:
        with _lock:
            _preview_ready(sid, ses)
            job = _jobs.get(sid)
            result = (job or {}).get("result")
            if not result or payload.get("base_run") != result["run"]:
                raise HTTPException(409, "结果已更新，请刷新后再编辑")
        target = _integer(payload.get("frame_index"))
        if not 0 <= target < ses["cache"]["frames"]:
            raise HTTPException(400, "帧号超出视频范围")
        action = payload.get("action")
        if action not in ("add", "move"):
            raise HTTPException(400, "未知编辑操作")
        cuts = [dict(cut) for cut in result["cuts"]]
        source = None
        old_cut = None
        if action == "move":
            index = _integer(payload.get("index"))
            if not 0 <= index < len(cuts):
                raise HTTPException(400, "关键帧条目不存在")
            old_cut = cuts.pop(index)
            source = old_cut["frame_index"]
        if any(cut["frame_index"] == target for cut in cuts) or source == target:
            raise HTTPException(409, "该帧已在结果中，请选择其他画面")
        additions = set(ses.get("manual_additions", set()))
        adjustments = dict(ses.get("manual_adjustments", {}))
        if action == "add" or (old_cut and old_cut["kind"] == "manual"):
            if source is not None:
                additions.discard(source)
            additions.add(target)
            kind = "manual"
        else:
            original = next((key for key, value in adjustments.items() if value == source), source)
            adjustments[original] = target
            kind = "adjusted"
        timestamp = float(ses["cache"]["times"][target])
        cuts.append({"kind": kind, "frame_index": target, "time": round(timestamp, 6),
                     "label": _fmt_time(timestamp)})
        cuts.sort(key=lambda cut: cut["frame_index"])
        run = uuid.uuid4().hex[:8]
        fdir, tdir = (WORK / sid / name / run for name in ("frames", "thumbs"))
        for directory in (fdir, tdir):
            directory.mkdir(parents=True)
            directories.append(directory)
        cap = cv2.VideoCapture(str(ses["video_path"]))
        try:
            image = _grab_frame_at(cap, target)
        finally:
            cap.release()
        if image is None:
            raise HTTPException(422, "该帧无法解码，已保留原结果")
        hh, ww = image.shape[:2]
        scale = min(1.0, 720 / max(hh, ww))
        thumbnail = cv2.resize(image, (max(1, int(ww * scale)), max(1, int(hh * scale))))
        previous = {cut["frame_index"]: i for i, cut in enumerate(result["cuts"])}
        for i, cut in enumerate(cuts):
            if cut["frame_index"] == target:
                _write_jpeg(fdir / f"{i}.jpg", image, 95)
                _write_jpeg(tdir / f"{i}.jpg", thumbnail, 85)
            else:
                old_index = previous[cut["frame_index"]]
                for name, directory in (("frames", fdir), ("thumbs", tdir)):
                    shutil.copyfile(WORK / sid / name / result["run"] / f"{old_index}.jpg",
                                    directory / f"{i}.jpg")
        updated = {**result, "run": run, "cuts": cuts,
                   "thumbs": [f"/api/thumb/{sid}/{run}/{i}" for i in range(len(cuts))],
                   "frames": [f"/api/frame/{sid}/{run}/{i}" for i in range(len(cuts))]}
        with _lock:
            if _jobs.get(sid) is not job or job.get("result") is not result or job["status"] == "running":
                raise HTTPException(409, "分析任务或结果已更新，请重试编辑")
            ses["manual_additions"] = additions
            ses["manual_adjustments"] = adjustments
            ses.setdefault("results", {})[run] = updated
            job.update(result=updated, run=run, status="done", pct=100.0, stage="完成", error=None)
            published = True
        return {"result": updated, "from_frame": source, "to_frame": target}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "保存关键帧失败，已保留原结果") from exc
    finally:
        if not published:
            for directory in directories:
                shutil.rmtree(directory, ignore_errors=True)
        ses["worker_lock"].release()


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = (file.filename or "video.mp4").replace("\\", "/").split("/")[-1]
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型「{ext}」。支持："
                                 + " ".join(sorted(ALLOWED_EXT)))
    sid = uuid.uuid4().hex[:10]
    sdir = WORK / sid
    sdir.mkdir(parents=True, exist_ok=True)
    dest = sdir / ("video" + ext)
    size = 0
    try:
        with open(dest, "wb") as f:
            while chunk := await file.read(1 << 20):
                f.write(chunk)
                size += len(chunk)
    except Exception:
        shutil.rmtree(sdir, ignore_errors=True)
        raise HTTPException(400, "视频接收失败，请重试")
    meta = probe(dest)
    if meta is None:
        shutil.rmtree(sdir, ignore_errors=True)
        raise HTTPException(400, "无法解析该视频：文件可能损坏，或编码不受支持")
    meta["size_mb"] = round(size / 1e6, 1)
    with _lock:
        _sessions[sid] = {"video_path": dest, "video_name": name,
                          "meta": meta, "cache": {}, "worker_lock": threading.Lock(),
                          "results": {}}
    return {"session_id": sid, "video_name": name, "meta": meta}


@app.post("/api/analyze")
def analyze(payload: dict):
    sid = str(payload.get("session_id", ""))
    ses = _sessions.get(sid)
    if ses is None:
        raise HTTPException(404, "会话不存在或已过期，请重新上传视频")
    try:
        sens = float(payload.get("sensitivity", 50))
        minimum = float(payload.get("min_scene_seconds", 0.1))
        if not math.isfinite(sens) or not math.isfinite(minimum) or not 0 <= minimum <= 10:
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(400, "灵敏度须为有限数值，最小镜头间隔须为 0～10 秒")
    sens = min(100.0, max(0.0, sens))
    ends = bool(payload.get("include_ends", False))
    with _lock:
        old = _jobs.get(sid)
        if old:
            old["cancel"] = True
        job = _jobs[sid] = {
            "status": "running", "pct": 0.0, "stage": "准备中…",
            "error": None, "cancel": False, "result": None,
            "sensitivity": sens, "include_ends": ends,
            "min_scene_seconds": minimum,
            "suppress_flash": bool(payload.get("suppress_flash", True)),
            "run": uuid.uuid4().hex[:8],
        }
        job["result"] = old.get("result") if old else None
    threading.Thread(target=_run_job, args=(sid, job, ses), daemon=True).start()
    return {"ok": True, "run": job["run"]}


@app.post("/api/cancel/{sid}")
def cancel(sid: str, payload: dict):
    with _lock:
        job = _jobs.get(sid)
        if job is None:
            raise HTTPException(404, "任务不存在")
        if payload.get("run") != job["run"]:
            raise HTTPException(409, "任务已更新，请刷新任务状态后再取消")
        if job["status"] == "running":
            job["cancel"] = True
            job["stage"] = "正在取消…"
    return {"ok": True}


@app.get("/api/status/{sid}")
def status(sid: str):
    job = _jobs.get(sid)
    if job is None:
        raise HTTPException(404, "没有进行中的任务")
    return {
        "run": job["run"],
        "video_name": _sessions[sid]["video_name"],
        "meta": _sessions[sid]["meta"],
        "params": {key: job[key] for key in ("sensitivity", "include_ends", "min_scene_seconds", "suppress_flash")},
        "max_export": MAX_EXPORT,
        "status": job["status"],
        "pct": round(job["pct"], 1),
        "stage": job["stage"],
        "error": job["error"],
        "result": job["result"],
    }


@app.get("/api/thumb/{sid}/{run}/{i}")
def thumb(sid: str, run: str, i: int):
    path = WORK / sid / "thumbs" / run / f"{i}.jpg"
    if not path.exists():
        raise HTTPException(404, "预览图不存在")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/frame/{sid}/{run}/{i}")
def frame(sid: str, run: str, i: int):
    """原分辨率原图（页面内查看用，inline）。"""
    path = WORK / sid / "frames" / run / f"{i}.jpg"
    if not path.exists():
        raise HTTPException(404, "原图不存在")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/frame-dl/{sid}/{run}/{i}")
def frame_dl(sid: str, run: str, i: int):
    """单张原图下载（attachment）。"""
    path = WORK / sid / "frames" / run / f"{i}.jpg"
    if not path.exists():
        raise HTTPException(404, "原图不存在")
    result = _sessions.get(sid, {}).get("results", {}).get(run)
    name = "frame.jpg"
    if result and 0 <= i < len(result["cuts"]):
        c = result["cuts"][i]
        name = f"{i + 1:03d}_{c['label'].replace(':', '-')}.jpg"
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _export_archive(sid: str, ids: str = "", run: str = ""):
    ses = _sessions.get(sid)
    if ses is None:
        raise HTTPException(404, "会话不存在或已过期，请重新上传视频")
    job = _jobs.get(sid)
    result = ses.get("results", {}).get(run) if run else (job or {}).get("result")
    if not result:
        raise HTTPException(400, "还没有可导出的结果，请先完成分析")
    cuts = result["cuts"]
    if not cuts:
        raise HTTPException(400, "没有检测到任何关键帧")
    if ids.strip():
        try:
            wanted = sorted({int(part.strip()) for part in ids.split(",")})
        except ValueError:
            raise HTTPException(400, "无效的关键帧选择")
        if not wanted or any(i < 0 or i >= len(cuts) for i in wanted):
            raise HTTPException(400, "选中的关键帧不存在")
    else:
        wanted = list(range(len(cuts)))
    if len(wanted) > MAX_EXPORT:
        raise HTTPException(400, f"一次最多导出 {MAX_EXPORT} 张，请先减少选择")
    fdir = WORK / sid / "frames" / result["run"]
    if any(not (fdir / f"{i}.jpg").is_file() for i in wanted):
        raise HTTPException(409, "选中的原图文件缺失，请重新分析；未导出不完整的结果")
    export_dir = WORK / sid / "exports"
    export_dir.mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".zip", dir=export_dir)
    os.close(fd)
    path = Path(name)
    lines = ["# 关键帧列表（零基帧号及实际时间码）", f"# 视频: {result['video_name']}"]
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            for i in wanted:
                stem = f"{i + 1:03d}_{cuts[i]['label'].replace(':', '-')}"
                archive.write(fdir / f"{i}.jpg", f"{stem}.jpg")
                lines.append(f"{stem}.jpg\t{cuts[i]['label']}\t{cuts[i]['kind']}\t{cuts[i]['frame_index']}")
            archive.writestr("cuts.txt", "\n".join(lines))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path, len(wanted)


def _zip_response(sid, path):
    stem = re_safe(Path(_sessions[sid]["video_name"]).stem or "video")
    return FileResponse(path, media_type="application/zip", filename=f"关键帧_{stem}.zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.get("/api/export/{sid}")
def export(sid: str, ids: str = "", run: str = ""):
    path, _ = _export_archive(sid, ids, run)
    return _zip_response(sid, path)


@app.post("/api/export/{sid}")
def prepare_export(sid: str, payload: dict):
    # 先返回可检查的 JSON，随后由浏览器原生下载，避免在 JS 中构造整个 ZIP Blob。
    path, count = _export_archive(sid, str(payload.get("ids", "")), str(payload.get("run", "")))
    return {"url": f"/api/download/{sid}/{path.name}", "count": count}


@app.get("/api/download/{sid}/{name}")
def download_archive(sid: str, name: str):
    if sid not in _sessions or Path(name).name != name or not name.endswith(".zip") or "\\" in name:
        raise HTTPException(404, "下载不存在")
    path = WORK / sid / "exports" / name
    if not path.is_file():
        raise HTTPException(404, "下载已完成或文件不存在，请重新打包")
    return _zip_response(sid, path)


def re_safe(name: str) -> str:
    return re_sub(name)


def re_sub(name: str) -> str:
    import re
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)[:80]


# ---------------------------------------------------------------- 静态页面

app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"关键帧工具已启动： http://127.0.0.1:{port}   （Ctrl+C 退出）")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

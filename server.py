# -*- coding: utf-8 -*-
"""
视频 Cut 关键帧截取工具 —— 本地服务
====================================
对上传的视频做「镜头切换检测」(cut / scene change)：
  第一遍：按 ~8fps 采样整段视频，计算相邻帧灰度差，找出突变候选点
  第二遍：在每个候选点附近 ±0.4s 逐帧精扫，得到精确的切换时间
  输出：每个新镜头第一帧（即 cut 后的首帧）作为关键帧参考图，可打包 zip 下载

全部处理在本地完成，不上传任何数据。无需安装 ffmpeg（OpenCV 自带解码）。
运行方式：双击「启动工具.bat」，或 .venv\\Scripts\\python.exe server.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

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

SAMPLE_FPS = 8.0          # 粗扫采样帧率
SCAN_WIDTH = 160          # 粗扫/精扫用的灰度图宽度
MAX_EXPORT = 500          # 单次打包上限

app = FastAPI(title="视频关键帧截取工具")

_lock = threading.Lock()
_sessions: dict[str, dict] = {}   # session_id -> {video_path, video_name, meta, cache}
_jobs: dict[str, dict] = {}       # session_id -> job


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------------- 基础工具

def _small_gray(frame):
    """把一帧缩成小灰度图，用于快速比较。"""
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = g.shape[:2]
    return cv2.resize(g, (SCAN_WIDTH, max(1, int(SCAN_WIDTH * h / max(1, w)))))


def _mad(a, b):
    """两帧小灰度图的平均绝对差（0~255 尺度）。"""
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))


def _fmt_time(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _grab_frame_at(cap, target_sec):
    """取时间点 >= target_sec 的第一帧（target 已在调用处加了保险偏移）。

    定位方式：从 target-2s 开始顺序解码（内部会从最近关键帧解起），
    返回第一帧时间戳 >= target 的画面；到结尾还没到就返回最后一帧。
    """
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, (target_sec - 2.0)) * 1000.0)
    last = None
    while True:
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        ok, frame = cap.read()
        if not ok:
            return last
        last = frame
        if t >= target_sec:
            return frame


def probe(path: Path):
    """读取视频基本信息；无法解码时返回 None。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0 or fps > 240:
        fps = 30.0
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
    """第一遍：顺序解码整段视频，每隔 N 帧取小灰度图，算相邻差。"""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("无法解码该视频（编码可能不支持）")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if not fps or fps <= 0 or fps > 240:
        fps = 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    interval = max(1, int(round(fps / SAMPLE_FPS)))
    times: list[float] = []
    grays: list[np.ndarray] = []
    n = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if n % interval == 0:
                times.append(n / fps)
                grays.append(_small_gray(frame))
            n += 1
            if n % 150 == 0:
                if job.get("cancel"):
                    raise _Cancelled()
                pct = 5 + 62 * (n / max(1, total))
                job["pct"] = min(pct, 66.0)
                job["stage"] = f"扫描画面差异… {n:,}/{total:,} 帧"
    finally:
        cap.release()

    if n == 0:
        raise RuntimeError("视频没有任何可解码的帧")
    if len(grays) < 2:
        raise RuntimeError("视频太短，无法检测镜头切换")

    diffs = np.array([_mad(grays[i], grays[i + 1]) for i in range(len(grays) - 1)],
                     dtype=np.float64)
    cache.update({
        "fps": float(fps),
        "frames": n,
        "interval": interval,
        "times": times,     # 采样帧的时间
        "diffs": diffs,     # 相邻采样帧的差
    })
    return True


def _refine_cuts(video_path: str, job: dict, cache: dict, chosen: list[int]):
    """第二遍：在每个候选点附近 ±0.4s 逐帧精扫，返回精确 cut 时间列表。"""
    fps = cache["fps"]
    times = cache["times"]
    cap = cv2.VideoCapture(video_path)
    exact: list[float] = []
    try:
        for i in chosen:
            if job.get("cancel"):
                raise _Cancelled()
            t_center = (times[i] + times[i + 1]) / 2.0
            lo = max(0.0, t_center - 0.4)
            hi = min(times[-1] + 1.0 / fps, t_center + 0.4)
            cap.set(cv2.CAP_PROP_POS_MSEC, lo * 1000.0)
            prev = None
            best_t = None
            best_d = -1.0
            idx = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                tf = lo + idx / fps
                if tf > hi + 1e-6:
                    break
                g = _small_gray(frame)
                if prev is not None:
                    dd = _mad(prev, g)
                    if dd > best_d:
                        best_d = dd
                        best_t = tf       # 突变后这一帧即新镜头的第一帧
                prev = g
                idx += 1
            if best_t is not None:
                exact.append(best_t)
    finally:
        cap.release()
    exact.sort()
    ded: list[float] = []
    for t in exact:
        if not ded or t - ded[-1] > 0.3:
            ded.append(t)
    return ded


def _select_entries(video_path: str, job: dict, ses: dict):
    """根据灵敏度挑选 cut 点并组成最终条目 [(kind, time)]。kind: cut/head/tail"""
    cache = ses["cache"]
    diffs = cache["diffs"]
    fps = cache["fps"]
    sens = job["sensitivity"]
    mult = 8.0 - sens / 100.0 * 6.0          # 0=保守(阈值高) 100=敏感(阈值低)
    med = float(np.median(diffs))
    base = med
    if base < 0.5:                            # 画面基本静止的视频，基准抬高一点
        base = max(float(np.percentile(diffs, 65)), 0.5)
    thr = max(base * mult, 2.0)

    # 差超过阈值者为候选；按差值从大到小贪心挑选，保证最小间隔 ~0.4s
    gap = max(2, int(round(0.4 * fps / cache["interval"])))
    cand = [i for i in range(len(diffs)) if diffs[i] > thr]
    chosen: list[int] = []
    for i in sorted(cand, key=lambda i: -diffs[i]):
        if all(abs(i - j) >= gap for j in chosen):
            chosen.append(i)
    chosen.sort()

    exact = _refine_cuts(video_path, job, cache, chosen)

    dur = cache["frames"] / fps
    entries: list[tuple[str, float]] = []
    if job.get("include_ends"):
        if not exact or exact[0] > 0.25:
            entries.append(("head", 0.0))     # 视频首帧
    entries.extend(("cut", t) for t in exact)
    if job.get("include_ends"):
        tail = max(0.0, dur - 1.0 / fps)
        if not entries or entries[-1][1] < dur - 0.5:
            entries.append(("tail", tail))    # 视频末帧
    return entries, dur


def _run_job(sid: str) -> None:
    job = _jobs.get(sid)
    ses = _sessions.get(sid)
    try:
        if job is None or ses is None:
            return
        sdir = WORK / sid
        if "diffs" not in ses.setdefault("cache", {}):
            job["stage"] = "第一遍：解码并扫描画面差异（大文件可能需要几分钟）"
            job["pct"] = 4.0
            _scan_pass1(str(ses["video_path"]), job, ses["cache"])
        if job.get("cancel"):
            return

        job["stage"] = "按灵敏度筛选并精确定位镜头切换点…"
        job["pct"] = 68.0
        entries, dur = _select_entries(str(ses["video_path"]), job, ses)
        if job.get("cancel"):
            return

        cache = ses["cache"]
        fps = cache["fps"]
        run = job["run"]
        fdir = sdir / "frames" / run          # 原分辨率原图
        tdir = sdir / "thumbs" / run          # 页面预览小图
        # 清理上一次（其它 run）的产物，避免残留
        for base in (sdir / "frames", sdir / "thumbs"):
            if base.is_dir():
                for d in base.iterdir():
                    if d.name != run:
                        shutil.rmtree(d, ignore_errors=True)
        fdir.mkdir(parents=True, exist_ok=True)
        tdir.mkdir(parents=True, exist_ok=True)
        job["stage"] = "截取关键帧原图…"
        job["pct"] = 76.0
        cap = cv2.VideoCapture(str(ses["video_path"]))
        cuts: list[dict] = []
        total_n = len(entries)
        try:
            for i, (kind, t) in enumerate(entries):
                if job.get("cancel"):
                    return
                job["pct"] = 78 + 16 * (i / max(1, total_n))
                job["stage"] = f"截取关键帧原图… {i + 1}/{total_n}"
                target = t + 1.5 / fps        # 取切换点之后一点，确保属于新镜头
                frame = _grab_frame_at(cap, target)
                if frame is None:
                    continue
                # 原分辨率原图（JPEG 95，近无损）
                cv2.imwrite(str(fdir / f"{i}.jpg"), frame,
                            [cv2.IMWRITE_JPEG_QUALITY, 95])
                # 页面预览小图（最长边 720）
                hh, ww = frame.shape[:2]
                if max(hh, ww) > 720:
                    scale = 720 / max(hh, ww)
                    prev = cv2.resize(frame, (int(ww * scale), int(hh * scale)))
                else:
                    prev = frame
                cv2.imwrite(str(tdir / f"{i}.jpg"), prev,
                            [cv2.IMWRITE_JPEG_QUALITY, 85])
                cuts.append({"kind": kind, "time": round(t, 4),
                             "label": _fmt_time(t)})
        finally:
            cap.release()

        if job.get("cancel"):
            return
        job["pct"] = 97.0
        job["stage"] = "完成"
        fps_r = cache["fps"]
        job["result"] = {
            "video_name": ses["video_name"],
            "params": {"sensitivity": job["sensitivity"],
                       "include_ends": bool(job.get("include_ends"))},
            "meta": {
                "width": ses["meta"].get("width"),
                "height": ses["meta"].get("height"),
                "fps": round(fps_r, 3),
                "frames": int(cache["frames"]),
                "duration": round(cache["frames"] / fps_r, 3),
                "size_mb": ses["meta"].get("size_mb"),
            },
            "cuts": cuts,
            "thumbs": [f"/api/thumb/{sid}/{job['run']}/{i}" for i in range(len(cuts))],
            "frames": [f"/api/frame/{sid}/{job['run']}/{i}" for i in range(len(cuts))],
        }
        job["pct"] = 100.0
        job["status"] = "done"
    except _Cancelled:
        return
    except Exception as exc:                  # noqa: BLE001
        job["status"] = "error"
        job["stage"] = "失败"
        job["error"] = str(exc)


# ---------------------------------------------------------------- HTTP API

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
    with _lock:
        for old in list(_sessions):
            if old != sid:
                old_job = _jobs.get(old)
                if old_job:
                    old_job["cancel"] = True
                _jobs.pop(old, None)
                shutil.rmtree(WORK / old, ignore_errors=True)
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
    _sessions[sid] = {"video_path": dest, "video_name": name,
                      "meta": meta, "cache": {}}
    return {"session_id": sid, "video_name": name, "meta": meta}


@app.post("/api/analyze")
def analyze(payload: dict):
    sid = str(payload.get("session_id", ""))
    ses = _sessions.get(sid)
    if ses is None:
        raise HTTPException(404, "会话不存在或已过期，请重新上传视频")
    sens = float(payload.get("sensitivity", 50))
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
            "run": uuid.uuid4().hex[:8],
        }
    threading.Thread(target=_run_job, args=(sid,), daemon=True).start()
    return {"ok": True}


@app.get("/api/status/{sid}")
def status(sid: str):
    job = _jobs.get(sid)
    if job is None:
        raise HTTPException(404, "没有进行中的任务")
    return {
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
    job = _jobs.get(sid)
    name = "frame.jpg"
    if job and job.get("result") and 0 <= i < len(job["result"]["cuts"]):
        c = job["result"]["cuts"][i]
        name = f"{i + 1:03d}_{c['label'].replace(':', '-')}.jpg"
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/export/{sid}")
def export(sid: str, ids: str = ""):
    """把分析时已截好的原图打包成 zip（不做任何视频解码，秒级完成）。"""
    ses = _sessions.get(sid)
    job = _jobs.get(sid)
    if ses is None:
        raise HTTPException(404, "会话不存在或已过期，请重新上传视频")
    if job is None or job["status"] != "done" or not job["result"]:
        raise HTTPException(400, "还没有可导出的结果，请先完成分析")
    result = job["result"]
    cuts = result["cuts"]
    if not cuts:
        raise HTTPException(400, "没有检测到任何关键帧")
    wanted = list(range(len(cuts)))
    if ids.strip():
        parsed: list[int] = []
        for part in ids.split(","):
            part = part.strip()
            if not part.isdigit():
                continue
            v = int(part)
            if 0 <= v < len(cuts):
                parsed.append(v)
        if parsed:
            wanted = parsed
    if len(wanted) > MAX_EXPORT:
        raise HTTPException(400, f"一次最多导出 {MAX_EXPORT} 张，请先减少选择")

    fdir = WORK / sid / "frames" / job["run"]
    if not fdir.is_dir():
        raise HTTPException(500, "原图文件缺失，请重新点「重新分析」")

    buf = io.BytesIO()
    lines = ["# 关键帧列表（顺序: 时间码）", f"# 视频: {result['video_name']}"]
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # JPEG 不再压缩，秒级打包
        for i in wanted:
            p = fdir / f"{i}.jpg"
            if not p.exists():
                continue
            stem = f"{i + 1:03d}_{cuts[i]['label'].replace(':', '-')}"
            z.writestr(f"{stem}.jpg", p.read_bytes())
            lines.append(f"{stem}.jpg\t{cuts[i]['label']}\t{cuts[i]['kind']}")
            n += 1
        if not n:
            raise HTTPException(400, "选中的帧没有可用文件，请重新分析")
        z.writestr("cuts.txt", "\n".join(lines))

    stem = re_safe(Path(ses["video_name"]).stem or "video")
    zh_name = f"关键帧_{stem}.zip"
    cd = (f"attachment; filename=\"keyframes.zip\"; "
          f"filename*=UTF-8''{quote(zh_name)}")
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": cd})


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

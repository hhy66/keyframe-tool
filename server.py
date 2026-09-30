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

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

import corrections
import cv2
import frame_analysis
import motion
import numpy as np
import shots
import uvicorn
import workspace_store
import storage_manager
from functools import wraps
from runtime_compat import lifespan
from detection import (  # noqa: F401  检测算法独立成模块，这里保留原名供接口与测试使用
    PICKS,
    SCAN_WIDTH,
    SETTLE_SECONDS,
    SHARP_WINDOW,
    _Cancelled,
    _fmt_time,
    _fps,
    _grab_frame_at,
    _mad,
    _pick_frame,
    _scan_pass1,
    _select_entries,
    _sharpness,
    _signature,
    _similarity,
    _small_color,
    _write_jpeg,
    probe,
)
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
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

ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg", ".3gp", ".ogv"}

MAX_EXPORT = 500  # 单次打包上限

app = FastAPI(title="视频关键帧截取工具", lifespan=lifespan)


@app.middleware("http")
async def _no_stale_pages(request, call_next):
    """页面与脚本每次都向本机服务确认是否更新（未变时只回 304），升级后不会混用旧脚本。"""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


_lock = threading.RLock()
_sessions: dict[str, dict] = {}  # session_id -> {video_path, video_name, meta, cache}
_jobs: dict[str, dict] = {}  # session_id -> job


def _storage_guard(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with _lock:
            _storage.recover()
            for sid in list(_sessions):
                _collect_garbage(sid)
            return fn(*args, **kwargs)

    return guarded


def _file_response(sid, path, **kwargs):
    return _storage.lease(sid, FileResponse(path, **kwargs))


def _collect_garbage(sid):
    ses = _sessions.get(sid)
    if not ses or not ses.get('pending_delete') or _storage.active.get(sid, 0):
        return
    try:
        workspace_store.collect_retired(WORK, sid, ses)
        ses['cleanup_warning'] = '旧图被占用，稍后自动清理' if ses.get('pending_delete') else ''
    except (OSError, ValueError):
        ses['cleanup_warning'] = '旧图暂未清理，将在下次访问时重试'


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
        workspace_store.begin_result(WORK, sid, run)
        fdir = sdir / "frames" / run  # 原分辨率原图
        tdir = sdir / "thumbs" / run  # 页面预览小图
        # 结果完成前不覆盖或删除已发布的图片，旧页面和下载仍可使用。
        fdir.mkdir(parents=True, exist_ok=True)
        tdir.mkdir(parents=True, exist_ok=True)
        job["stage"] = "截取关键帧原图…"
        job["pct"] = 76.0
        cap = cv2.VideoCapture(str(ses["video_path"]))
        cuts: list[dict] = []
        total_n = len(entries)
        pick = job.get("pick", "first")
        try:
            for i, (kind, source_frame) in enumerate(entries):
                if job.get("cancel"):
                    raise _Cancelled()
                job["pct"] = 78 + 16 * (i / max(1, total_n))
                job["stage"] = f"截取关键帧原图… {i + 1}/{total_n}"
                # 截取策略只作用于自动检测的镜头；手动补帧和微调保持用户选定的帧。
                stop = (entries[i + 1][1] - 1) if i + 1 < total_n else int(cache["frames"]) - 1
                if kind in ("cut", "head"):
                    frame_index, frame = _pick_frame(cap, source_frame, stop, pick, cache["fps"])
                else:
                    frame_index, frame = source_frame, _grab_frame_at(cap, source_frame)
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
                cut = {
                    "kind": kind,
                    "frame_index": frame_index,
                    "time": round(t, 6),
                    "label": _fmt_time(t),
                    "asset_run": run,
                    "file_index": i,
                }
                if kind in ("cut", "head"):
                    cut["source_frame"] = source_frame  # 检测到的切换帧：跳过状态和微调按它对应
                cuts.append(cut)
        finally:
            cap.release()

        if job.get("cancel"):
            raise _Cancelled()
        job["pct"] = 97.0
        job["stage"] = "完成"
        fps_r = cache["fps"]
        result = {
            "run": run,
            "created_at": storage_manager.now(),
            "action": "analysis",
            "video_name": ses["video_name"],
            "params": {
                "sensitivity": job["sensitivity"],
                "include_ends": bool(job.get("include_ends")),
                "min_scene_seconds": job["min_scene_seconds"],
                "suppress_flash": job["suppress_flash"],
                "pick": job.get("pick", "first"),
            },
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
            staged = workspace_store.current_update(ses, result)
            _persist_session(sid, staged, result)
            ses.update(
                {key: staged[key] for key in ("cache_file", "latest_run", "updated_at", "results", "pending_delete")}
            )
            job["result"] = result
            job["pct"] = 100.0
            job["status"] = "done"
            _collect_garbage(sid)
    except _Cancelled:
        job["status"] = "cancelled"
        job["stage"] = "已取消"
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["stage"] = "失败"
        job["error"] = str(exc)
    finally:
        if job["status"] != "done":
            for directory in (fdir, tdir):
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)
        try:
            workspace_store.finish_result(WORK, sid, job['run'])
        except OSError:
            pass


# ---------------------------------------------------------------- HTTP API


def _persist_session(sid, ses, result=None):
    workspace_store.save(WORK, sid, ses, result)


def _safe_path(sid, *parts):
    try:
        return workspace_store.safe_path(WORK, sid, *parts)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _session(sid):
    ses = _sessions.get(sid)
    if ses is not None:
        return ses
    try:
        restored = workspace_store.load(WORK, sid)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(422, str(exc)) from exc
    restored["worker_lock"] = threading.Lock()
    workspace_store.recover_result(WORK, sid, restored)
    result = restored["results"].get(restored.get("latest_run"))
    params = result["params"] if result else dict(workspace_store.DEFAULT_PARAMS)
    with _lock:
        if sid not in _sessions:
            _sessions[sid] = restored
            _jobs[sid] = dict(
                status="done" if result else "idle",
                pct=100.0 if result else 0.0,
                stage="已恢复",
                error=None,
                result=result,
                run=result["run"] if result else "",
                cancel=False,
                **params,
            )
        _collect_garbage(sid)
        return _sessions[sid]


@app.get("/api/workspace")
@_storage_guard
def workspace():
    entries = []
    for directory in WORK.iterdir():
        if directory.name == storage_manager.MANAGEMENT or not directory.is_dir():
            continue
        sid = directory.name
        try:
            ses = workspace_store.load(WORK, sid, load_cache=False)
            result = ses["results"].get(ses.get("latest_run"))
            entries.append(
                dict(
                    session_id=sid,
                    video_name=ses["video_name"],
                    updated_at=ses["updated_at"],
                    frame_count=len(result["cuts"]) if result else 0,
                    legacy=ses["legacy"],
                    can_restore=True,
                    note=ses.get("workspace_note", ""),
                )
            )
        except (ValueError, OSError) as exc:
            entries.append(
                dict(
                    session_id=sid,
                    video_name=sid,
                    updated_at="",
                    frame_count=0,
                    legacy=False,
                    can_restore=False,
                    note=str(exc),
                )
            )
    entries.sort(key=lambda entry: entry["updated_at"] or "", reverse=True)
    return {"entries": entries, "path": str(WORK)}


@app.post("/api/workspace/{sid}/restore")
@_storage_guard
def restore_workspace(sid: str):
    _session(sid)
    return status(sid)


@app.put("/api/workspace/{sid}/selection")
@_storage_guard
def save_selection(sid: str, payload: dict):
    ses = _session(sid)
    excluded = payload.get("excluded")
    if not isinstance(excluded, list) or any(isinstance(i, bool) or not isinstance(i, (int, str)) for i in excluded):
        raise HTTPException(400, "选择记录格式无效")
    with _lock:
        result = (_jobs.get(sid) or {}).get("result")
        if not result or payload.get("run") != result["run"]:
            raise HTTPException(409, "结果已更新，请刷新后再保存选择")
        allowed = {
            c.get("source_frame", c["frame_index"])
            if c["frame_index"] is not None
            else f"legacy:{result['run']}:{c.get('file_index', i)}"
            for i, c in enumerate(result["cuts"])
        }
        if any(i not in allowed for i in excluded):
            raise HTTPException(400, "选择记录包含不存在的关键帧")
        staged = {**ses, "excluded": list(dict.fromkeys(excluded))}
        try:
            _persist_session(sid, staged, result)
        except Exception as exc:
            raise HTTPException(500, "保存选择失败，已保留之前记录") from exc
        ses.update({key: staged[key] for key in ("excluded", "cache_file", "latest_run", "updated_at")})
    return {"ok": True, "excluded": ses["excluded"]}


@app.get("/api/workspace/{sid}/collage")
@_storage_guard
def load_collage_draft(sid: str):
    import collage_engine

    ses = _session(sid)
    try:
        draft = collage_engine.clean_draft(ses.get("collage") or {})
    except ValueError:
        draft = {"ids": [], "crops": {}, "notes": {}}  # 损坏的草稿不阻止使用，重新开始即可
    return {"draft": draft, "saved": bool(ses.get("collage"))}


@app.put("/api/workspace/{sid}/collage")
@_storage_guard
def save_collage_draft(sid: str, payload: dict):
    """拼图草稿（顺序、裁剪、备注）随工作区保存，换浏览器、刷新或重启后都能恢复。"""
    import collage_engine

    ses = _session(sid)
    try:
        draft = collage_engine.clean_draft(payload.get("draft"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with _lock:
        staged = {**ses, "collage": draft}
        try:
            _persist_session(sid, staged)
        except Exception as exc:
            raise HTTPException(500, "保存拼图草稿失败，已保留之前记录") from exc
        ses.update({key: staged[key] for key in ("collage", "cache_file", "latest_run", "updated_at")})
    return {"ok": True}


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
@_storage_guard
def video(sid: str):
    path = _session(sid)["video_path"]
    if path is None or not Path(path).is_file():
        raise HTTPException(404, "原视频不存在，仅可查看已有截图")
    return _file_response(sid, path)


@app.get("/api/preview/{sid}")
@_storage_guard
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
        return {
            "frame_index": index,
            "time": timestamp,
            "label": _fmt_time(timestamp),
            "image_url": f"/api/preview-image/{sid}/{index}",
            "frames": len(times),
        }
    finally:
        ses["worker_lock"].release()


@app.get("/api/preview-image/{sid}/{frame_index}")
@_storage_guard
def preview_image(sid: str, frame_index: int, download: bool = False):
    """任意一帧的原分辨率画面；download=true 时作为文件下载（镜头首帧、尾帧原图用）。"""
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
        headers = {}
        if download:
            label = _fmt_time(float(ses["cache"]["times"][frame_index])).replace(":", "-")
            headers["Content-Disposition"] = f'attachment; filename="frame{frame_index + 1:06d}_{label}.jpg"'
        return Response(encoded.tobytes(), media_type="image/jpeg", headers=headers)
    finally:
        ses["worker_lock"].release()


@app.post("/api/edit/{sid}")
@_storage_guard
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
        cuts = [
            {
                **cut,
                'asset_run': workspace_store.asset_ref(result, i)[0],
                'file_index': workspace_store.asset_ref(result, i)[1],
            }
            for i, cut in enumerate(result["cuts"])
        ]
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
            original = next(
                (key for key, value in adjustments.items() if value == source), old_cut.get("source_frame", source)
            )
            adjustments[original] = target
            kind = "adjusted"
        timestamp = float(ses["cache"]["times"][target])
        cuts.append({"kind": kind, "frame_index": target, "time": round(timestamp, 6), "label": _fmt_time(timestamp)})
        cuts.sort(key=lambda cut: cut["frame_index"])
        run = uuid.uuid4().hex[:8]
        for cut in cuts:
            if cut['frame_index'] == target:
                cut.update(asset_run=run, file_index=0)
        workspace_store.begin_result(WORK, sid, run)
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
        _write_jpeg(fdir / '0.jpg', image, 95)
        _write_jpeg(tdir / '0.jpg', thumbnail, 85)
        updated = {
            **result,
            "run": run,
            "cuts": cuts,
            "created_at": storage_manager.now(),
            "action": "manual",
            "pinned": False,
            "thumbs": [f"/api/thumb/{sid}/{run}/{i}" for i in range(len(cuts))],
            "frames": [f"/api/frame/{sid}/{run}/{i}" for i in range(len(cuts))],
        }
        workspace_store.result_urls(sid, updated)
        with _lock:
            if _jobs.get(sid) is not job or job.get("result") is not result or job["status"] == "running":
                raise HTTPException(409, "分析任务或结果已更新，请重试编辑")
            old_keys = {source, (old_cut or {}).get("source_frame", source)}
            excluded = [target if value in old_keys else value for value in ses.get("excluded", [])]
            staged = {
                **workspace_store.current_update(ses, updated),
                "manual_additions": additions,
                "manual_adjustments": adjustments,
                "excluded": excluded,
            }
            _persist_session(sid, staged, updated)
            ses.update(
                {
                    key: staged[key]
                    for key in (
                        "manual_additions",
                        "manual_adjustments",
                        "excluded",
                        "cache_file",
                        "latest_run",
                        "updated_at",
                        "results",
                        "pending_delete",
                    )
                }
            )
            job.update(result=updated, run=run, status="done", pct=100.0, stage="完成", error=None)
            published = True
            _collect_garbage(sid)
        return {"result": updated, "from_frame": source, "to_frame": target}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "保存关键帧失败，已保留原结果") from exc
    finally:
        if not published:
            for directory in directories:
                shutil.rmtree(directory, ignore_errors=True)
        if directories:
            try:
                workspace_store.finish_result(WORK, sid, run)
            except OSError:
                pass
        ses["worker_lock"].release()


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = (file.filename or "video.mp4").replace("\\", "/").split("/")[-1]
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型「{ext}」。支持：" + " ".join(sorted(ALLOWED_EXT)))
    sid = uuid.uuid4().hex[:10]
    with _lock:
        _storage.active[sid] = _storage.active.get(sid, 0) + 1
    try:
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
        ses = {
            "video_path": dest,
            "video_name": name,
            "meta": meta,
            "cache": {},
            "worker_lock": threading.Lock(),
            "results": {},
        }
        try:
            _persist_session(sid, ses)
        except Exception as exc:
            raise HTTPException(500, "工作区记录保存失败，请检查磁盘空间") from exc
        with _lock:
            _sessions[sid] = ses
        return {"session_id": sid, "video_name": name, "meta": meta}
    finally:
        with _lock:
            _storage.active[sid] = max(0, _storage.active.get(sid, 0) - 1)


@app.post("/api/analyze")
@_storage_guard
def analyze(payload: dict):
    sid = str(payload.get("session_id", ""))
    ses = _session(sid)
    if ses.get("video_path") is None:
        raise HTTPException(409, "原视频缺失，无法重新分析")
    if not ses.get("meta"):
        ses["meta"] = probe(ses["video_path"]) or {}
    try:
        sens = float(payload.get("sensitivity", 50))
        minimum = float(payload.get("min_scene_seconds", 0.1))
        if not math.isfinite(sens) or not math.isfinite(minimum) or not 0 <= minimum <= 10:
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(400, "灵敏度须为有限数值，最小镜头间隔须为 0～10 秒")
    sens = min(100.0, max(0.0, sens))
    ends = bool(payload.get("include_ends", False))
    pick = payload.get("pick", "first")
    if pick not in PICKS:
        raise HTTPException(400, "截取策略无效")
    with _lock:
        old = _jobs.get(sid)
        if old:
            old["cancel"] = True
        job = _jobs[sid] = {
            "status": "running",
            "pct": 0.0,
            "stage": "准备中…",
            "error": None,
            "cancel": False,
            "result": None,
            "sensitivity": sens,
            "include_ends": ends,
            "min_scene_seconds": minimum,
            "suppress_flash": bool(payload.get("suppress_flash", True)),
            "pick": pick,
            "run": uuid.uuid4().hex[:8],
        }
        job["result"] = old.get("result") if old else None
    threading.Thread(target=_run_job, args=(sid, job, ses), daemon=True).start()
    return {"ok": True, "run": job["run"]}


@app.post("/api/cancel/{sid}")
@_storage_guard
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
@_storage_guard
def status(sid: str):
    ses = _session(sid)
    job = _jobs.get(sid)
    if job is None:
        return dict(
            status="idle",
            run="",
            video_name=ses["video_name"],
            meta=ses["meta"],
            params=dict(workspace_store.DEFAULT_PARAMS),
            max_export=MAX_EXPORT,
            excluded=ses.get("excluded", []),
            pct=0.0,
            stage="等待分析",
            error=None,
            result=None,
        )
    result = job["result"]
    if result is not None:
        result = {
            **result,
            "meta": {
                **result["meta"],
                "can_edit": bool(ses.get("cache", {}).get("frames"))
                and ses.get("video_path") is not None
                and not result["meta"].get("gallery_only", False),
            },
        }
    return {
        "run": job["run"],
        "video_name": _sessions[sid]["video_name"],
        "meta": _sessions[sid]["meta"],
        "params": {
            **{key: job[key] for key in ("sensitivity", "include_ends", "min_scene_seconds", "suppress_flash")},
            "pick": job.get("pick", "first"),
        },
        "max_export": MAX_EXPORT,
        "excluded": ses.get("excluded", []),
        "workspace_note": ses.get("workspace_note", "") + ses.get("cleanup_warning", ""),
        "status": job["status"],
        "pct": round(job["pct"], 1),
        "stage": job["stage"],
        "error": job["error"],
        "result": result,
    }


SIMILAR_THRESHOLD = 80.0  # 相似度 ≥ 80% 视为“与上一张几乎相同”
_similar_cache: dict[tuple, dict] = {}


@app.get("/api/similar/{sid}")
@_storage_guard
def similar(sid: str, run: str):
    """每张截图与前一张的相似度，用于提示并一键跳过重复画面。只读，不修改结果。"""
    ses = _session(sid)
    result = ses.get("results", {}).get(run)
    if not result:
        raise HTTPException(404, "结果已更新，请刷新")
    key = (sid, run)
    if key not in _similar_cache:
        signatures = []
        for i in range(len(result["cuts"])):
            try:
                path = workspace_store.asset_path(WORK, sid, result, i, "thumbs")
                if not path.is_file():
                    path = workspace_store.asset_path(WORK, sid, result, i)
                # np.fromfile + imdecode 支持 Windows 中文路径
                image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR) if path.is_file() else None
            except (ValueError, OSError):
                image = None
            signatures.append(None if image is None else _signature(image))
        scores = [
            None if i == 0 or a is None or signatures[i - 1] is None else _similarity(signatures[i - 1], a)
            for i, a in enumerate(signatures)
        ]
        if len(_similar_cache) > 64:
            _similar_cache.clear()
        _similar_cache[key] = {"run": run, "scores": scores, "threshold": SIMILAR_THRESHOLD}
    return _similar_cache[key]


# ---------------------------------------------------------------- 画面分析与镜头
# 按钮触发，后台先截每个镜头的首 / 中 / 尾帧小图（strip/，按帧号存放），再逐张分析截图；
# 分析结果按图片文件记在工作目录的 analysis.json，微调过的图只重算那一张。
ANALYSIS_FILE = "analysis.json"
STRIP_DIR = "strip"
_analysis_items: dict[str, dict] = {}  # session_id -> {"run/编号": 分析结果}
_motion_items: dict[str, dict] = {}  # session_id -> {"起始帧-结束帧": 运镜}
_analysis_jobs: dict[str, dict] = {}  # session_id -> {run, status, done, total, failed, error}
_analysis_write = threading.Lock()


def _asset_key(result, i):
    run, number = workspace_store.asset_ref(result, i)
    return f"{run}/{number}"


def _load_analysis(sid):
    """读 analysis.json：截图分析按图片文件，运镜按镜头起止帧；算法版本不同的部分直接作废。"""
    items, moves = {}, {}
    try:
        data = json.loads(workspace_store.safe_path(WORK, sid, ANALYSIS_FILE).read_text(encoding="utf-8"))
        if data.get("version") == frame_analysis.ANALYSIS_VERSION and isinstance(data.get("items"), dict):
            items = data["items"]
        if data.get("motion_version") == motion.MOTION_VERSION and isinstance(data.get("motion"), dict):
            moves = data["motion"]
    except (OSError, ValueError, AttributeError):
        pass
    _analysis_items[sid], _motion_items[sid] = items, moves


def _analysis_cache(sid):
    """已算好的截图分析结果。"""
    if sid not in _analysis_items:
        _load_analysis(sid)
    return _analysis_items[sid]


def _motion_cache(sid):
    """已算好的各镜头运镜。"""
    if sid not in _motion_items:
        _load_analysis(sid)
    return _motion_items[sid]


def _motion_key(span):
    return f"{span['start_frame']}-{span['end_frame']}"


def _save_analysis(sid, items):
    """只保留仍被某个结果引用的图片和镜头，删掉的历史版本不会一直留在缓存里。"""
    moves = _motion_cache(sid)
    with _lock:
        ses = _sessions.get(sid)
        if ses is None:
            return
        live = {_asset_key(res, i) for res in ses.get("results", {}).values() for i in range(len(res["cuts"]))}
        spans = {_motion_key(span) for res in ses.get("results", {}).values() for span in _spans(ses, res) if span}
    kept = {key: value for key, value in list(items.items()) if key in live}
    kept_moves = {key: value for key, value in list(moves.items()) if key in spans}
    with _analysis_write:
        workspace_store.atomic_json(
            workspace_store.safe_path(WORK, sid, ANALYSIS_FILE),
            {
                "version": frame_analysis.ANALYSIS_VERSION,
                "items": kept,
                "motion_version": motion.MOTION_VERSION,
                "motion": kept_moves,
            },
        )


def _spans(ses, result):
    return shots.spans(result, ses.get("cache", {}).get("times"))


def _spans_for_export(sid, ses, result):
    """导出用：镜头起止、时长，以及已算好的运镜描述。"""
    moves = _motion_cache(sid)
    return [
        None if span is None else {**span, "motion": (moves.get(_motion_key(span)) or {}).get("text", "")}
        for span in _spans(ses, result)
    ]


def _has_video(ses):
    path = ses.get("video_path")
    return bool(path) and Path(path).is_file()


def _strip_frames(ses, result):
    return [frame for span in _spans(ses, result) if span for frame in span["strip"]]


def _live_strip_frames(ses):
    return {frame for res in ses.get("results", {}).values() for frame in _strip_frames(ses, res)}


def _run_analysis(sid, job, todo, items, video_work=None):
    status, error = "done", ""
    try:
        offset = 0
        if video_work:
            # 一次顺序读视频：截镜头小图，同时测运镜
            root = workspace_store.safe_path(WORK, sid, STRIP_DIR)

            def progress(n, total):
                job["done"] = n

            failed, moves = shots.scan(
                video_work["video"],
                video_work["spans"],
                root,
                video_work["strips"],
                video_work["times"],
                video_work["fps"],
                progress,
            )
            job["strip_failed"] = len(failed)
            _motion_cache(sid).update(moves)
            offset = job["done"]
            with _lock:
                ses = _sessions.get(sid)
                if ses is not None:
                    shots.prune(root, _live_strip_frames(ses))
        for n, (key, path) in enumerate(todo, 1):
            try:
                # np.fromfile + imdecode 支持 Windows 中文路径
                image = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
                items[key] = frame_analysis.analyze(image)
            except (OSError, ValueError, cv2.error):
                job["failed"] += 1
            job["done"] = offset + n
            if n % 25 == 0:
                _save_analysis(sid, items)
        _save_analysis(sid, items)
    except Exception as exc:  # 保存失败等：告诉页面，已算好的仍在内存里可用
        status, error = "error", str(exc) or "画面分析失败"
    finally:
        with _lock:
            _storage.active[sid] = max(0, _storage.active.get(sid, 0) - 1)
            _collect_garbage(sid)
            job.update(status=status, error=error)


def _analysis_state(sid, run, items=True):
    ses = _session(sid)
    result = ses.get("results", {}).get(run)
    if not result:
        raise HTTPException(404, "结果已更新，请刷新")
    cache = _analysis_cache(sid)
    found = [cache.get(_asset_key(result, i)) for i in range(len(result["cuts"]))]
    ready = sum(item is not None for item in found)
    spans = _spans(ses, result)
    root = workspace_store.safe_path(WORK, sid, STRIP_DIR)
    moves = _motion_cache(sid)
    # 原视频被清理后截不了镜头小图、测不了运镜：不算未完成，页面会说明
    video = _has_video(ses)
    strip_missing = len(shots.missing(root, _strip_frames(ses, result))) if video else 0
    motion_missing = sum(1 for span in spans if span and _motion_key(span) not in moves) if video else 0
    job = _analysis_jobs.get(sid)
    running = bool(job and job["status"] == "running")
    state = {
        "run": run,
        "version": frame_analysis.ANALYSIS_VERSION,
        "status": "running"
        if running
        else "done"
        if ready == len(found) and not strip_missing and not motion_missing
        else "idle",
        "ready": ready,
        "total": len(found),
        "strip_missing": strip_missing,
        "motion_missing": motion_missing,
        "video": video,
        "job": {key: job.get(key) for key in ("run", "done", "total", "failed", "strip_failed", "error", "status")}
        if job
        else None,
    }
    if items:
        state["items"] = found
        state["shots"] = [
            None
            if span is None
            else {
                **span,
                "strip_ready": [shots.strip_path(root, frame).is_file() for frame in span["strip"]],
                "motion": moves.get(_motion_key(span)),
            }
            for span in spans
        ]
        fixes = _correction_cache(sid)
        state["corrections"] = [(fixes.get(_asset_key(result, i)) or {}).get("fix") for i in range(len(result["cuts"]))]
        state["corrections_total"] = len(fixes)
    return state


@app.get("/api/analysis/{sid}")
@_storage_guard
def analysis(sid: str, run: str, items: bool = True):
    """画面分析进度与结果（按当前结果的顺序，未分析的为 null）。只读。"""
    return _analysis_state(sid, run, items)


@app.post("/api/analysis/{sid}")
@_storage_guard
def start_analysis(sid: str, payload: dict):
    """开始分析这组结果里还没分析过的图片；已经在算时直接返回进度。"""
    run = payload.get("run")
    ses = _session(sid)
    result = ses.get("results", {}).get(run) if isinstance(run, str) else None
    if not result:
        raise HTTPException(404, "结果已更新，请刷新")
    job = _analysis_jobs.get(sid)
    if job and job["status"] == "running":
        if job["run"] != run:
            raise HTTPException(409, "另一组结果正在分析画面，请稍候")
        return _analysis_state(sid, run, items=False)
    items = _analysis_cache(sid)
    todo = {}
    for i in range(len(result["cuts"])):
        key = _asset_key(result, i)
        if key not in items and key not in todo:
            todo[key] = workspace_store.asset_path(WORK, sid, result, i)
    todo = list(todo.items())
    video_work, reads = None, 0
    if _has_video(ses):
        frames = shots.missing(workspace_store.safe_path(WORK, sid, STRIP_DIR), _strip_frames(ses, result))
        moves = _motion_cache(sid)
        pending = {}
        for span in _spans(ses, result):
            if span and _motion_key(span) not in moves:
                pending[_motion_key(span)] = span
        if frames or pending:
            samples = {
                f for span in pending.values() for f in motion.sample_frames(span["start_frame"], span["end_frame"])
            }
            reads = len(set(frames) | samples)
            cache = ses.get("cache", {})
            video_work = {
                "video": ses["video_path"],
                "spans": list(pending.items()),
                "strips": frames,
                "times": cache.get("times"),
                "fps": float((result.get("meta") or {}).get("fps") or cache.get("fps") or 25.0),
            }
    work = len(todo) + reads
    job = {
        "run": run,
        "status": "running" if work else "done",
        "done": 0,
        "total": work,
        "failed": 0,
        "strip_failed": 0,
        "error": "",
    }
    _analysis_jobs[sid] = job
    if work:
        # 计算期间占用这条记录：旧图不会被回收，空间管理也不会清理它。
        _storage.active[sid] = _storage.active.get(sid, 0) + 1
        threading.Thread(target=_run_analysis, args=(sid, job, todo, items, video_work), daemon=True).start()
    return _analysis_state(sid, run, items=False)


@app.get("/api/strip/{sid}/{frame}")
@_storage_guard
def strip_image(sid: str, frame: int):
    """镜头的首 / 中 / 尾帧小图（按视频帧号）。"""
    _session(sid)
    path = shots.strip_path(workspace_store.safe_path(WORK, sid, STRIP_DIR), _integer(frame))
    if frame < 0 or not path.is_file():
        raise HTTPException(404, "镜头小图不存在，请点「分析画面」生成")
    return _file_response(sid, path, media_type="image/jpeg")


# ---------------------------------------------------------------- 纠错
# 用户觉得分析不对时在大图里纠正；连同当时的测量值存进 corrections.json，导出后用于校准。
# 不算缓存：清理缓存、重新分析都不会删除它。
_corrections: dict[str, dict] = {}  # session_id -> {"run/编号": 记录}
_corrections_write = threading.Lock()


def _cut_signal(ses, span):
    """这个镜头附近的画面变化强度：开头那一帧，以及镜头里最强的几次变化（校准漏切 / 多切用）。"""
    diffs = ses.get("cache", {}).get("diffs")
    if diffs is None or not span or len(diffs) < 2:
        return None
    start, end = span["start_frame"], min(span["end_frame"], len(diffs) - 1)
    inside = np.asarray(diffs[start + 1 : end + 1], dtype=np.float32)
    top = np.argsort(inside)[::-1][:5] if len(inside) else []
    return {
        "median": round(float(np.median(diffs[1:])), 3),
        "p65": round(float(np.percentile(diffs, 65)), 3),
        "at_start": round(float(diffs[start]), 3) if start < len(diffs) else None,
        "peaks": [[int(start + 1 + k), round(float(inside[k]), 3)] for k in top],
    }


def _correction_cache(sid):
    if sid not in _corrections:
        _corrections[sid] = corrections.load(workspace_store.safe_path(WORK, sid, corrections.FILE))
    return _corrections[sid]


@app.put("/api/corrections/{sid}")
@_storage_guard
def save_correction(sid: str, payload: dict):
    """保存（或清除）一张截图的纠错。index 是这张图在结果 run 里的位置。"""
    ses = _session(sid)
    run, index = payload.get("run"), payload.get("index")
    result = ses.get("results", {}).get(run) if isinstance(run, str) else None
    if not result:
        raise HTTPException(404, "结果已更新，请刷新")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(result["cuts"]):
        raise HTTPException(400, "截图编号无效")
    try:
        fix = corrections.clean(payload.get("fix"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    key = _asset_key(result, index)
    with _corrections_write:
        items = _correction_cache(sid)
        if fix is None:
            items.pop(key, None)
        else:
            cut = result["cuts"][index]
            span = _spans(ses, result)[index]
            old = items.get(key, {})
            items[key] = {
                "fix": fix,
                "updated_at": storage_manager.now(),
                "index": index,
                "time": cut.get("time"),
                "label": cut.get("label"),
                "kind": cut.get("kind"),
                "frame_index": cut.get("frame_index"),
                "source_frame": cut.get("source_frame"),
                "shot_span": span,
                # 纠错时看到的测量值：之后算法更新重算，也知道当时错在哪
                "analysis": old.get("analysis") or _analysis_cache(sid).get(key),
                "motion": old.get("motion") or (_motion_cache(sid).get(_motion_key(span)) if span else None),
                "cut_signal": _cut_signal(ses, span),
                "params": result.get("params"),
                "meta": result.get("meta"),
            }
        workspace_store.atomic_json(workspace_store.safe_path(WORK, sid, corrections.FILE), corrections.document(items))
    return {"fix": fix, "total": len(items)}


@app.get("/api/corrections/{sid}/export")
@_storage_guard
def export_corrections(sid: str):
    """全部纠错记录（JSON，只有数值和文字）。"""
    from urllib.parse import quote

    ses = _session(sid)
    data = corrections.export(
        _correction_cache(sid),
        ses.get("video_name", ""),
        _analysis_cache(sid),
        _motion_cache(sid),
        {"analysis": frame_analysis.ANALYSIS_VERSION, "motion": motion.MOTION_VERSION},
    )
    name = quote(f"纠错记录_{_collage_video_name(sid)}.json")
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        # 旧浏览器不认 filename*，退回英文名
        headers={"Content-Disposition": f"attachment; filename=\"corrections.json\"; filename*=UTF-8''{name}"},
    )


@app.get("/api/thumb/{sid}/{run}/{i}")
@_storage_guard
def thumb(sid: str, run: str, i: int):
    path = _safe_path(sid, "thumbs", run, f"{i}.jpg")
    if not path.exists():
        raise HTTPException(404, "预览图不存在")
    return _file_response(sid, path, media_type="image/jpeg")


@app.get("/api/frame/{sid}/{run}/{i}")
@_storage_guard
def frame(sid: str, run: str, i: int):
    """原分辨率原图（页面内查看用，inline）。"""
    path = _safe_path(sid, "frames", run, f"{i}.jpg")
    if not path.exists():
        raise HTTPException(404, "原图不存在")
    return _file_response(sid, path, media_type="image/jpeg")


@app.get("/api/frame-dl/{sid}/{run}/{i}")
@_storage_guard
def frame_dl(sid: str, run: str, i: int):
    """单张原图下载（attachment）。"""
    path = _safe_path(sid, "frames", run, f"{i}.jpg")
    if not path.exists():
        raise HTTPException(404, "原图不存在")
    results = _session(sid).get("results", {})
    name = "frame.jpg"
    for result in results.values():
        match = next(
            ((n, c) for n, c in enumerate(result['cuts']) if workspace_store.asset_ref(result, n) == (run, i)), None
        )
        if match:
            number, c = match
            label = "unknown" if c["frame_index"] is None else c["label"].replace(":", "-")
            name = f"{number + 1:03d}_{label}.jpg"
            break
    return _file_response(
        sid, path, media_type="image/jpeg", headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


def _export_archive(sid: str, ids: str = "", run: str = ""):
    ses = _session(sid)
    job = _jobs.get(sid)
    result = ses.get("results", {}).get(run) if run else (job or {}).get("result")
    if not result:
        raise HTTPException(
            409 if run else 400, "结果已更新，请刷新后重新选择" if run else "还没有可导出的结果，请先完成分析"
        )
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
    fdir = _safe_path(sid, "frames", result["run"])
    paths = {i: workspace_store.asset_path(WORK, sid, result, i) for i in wanted}
    if any(not paths[i].is_file() for i in wanted):
        raise HTTPException(409, "选中的原图文件缺失，请重新分析；未导出不完整的结果")
    export_dir = _safe_path(sid, "exports")
    export_dir.mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".zip", dir=export_dir)
    os.close(fd)
    path = Path(name)
    lines = [
        "# 关键帧列表（零基帧号及实际时间码）；镜头起止为该截图所在镜头",
        f"# 视频: {result['video_name']}",
        "# 文件\t时间码\t类型\t帧号\t镜头开始\t镜头结束\t镜头时长(秒)\t运镜",
    ]
    spans = _spans_for_export(sid, _session(sid), result)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            for i in wanted:
                label = "unknown" if cuts[i]["frame_index"] is None else cuts[i]["label"].replace(":", "-")
                stem = f"{i + 1:03d}_{label}"
                archive.write(paths[i], f"{stem}.jpg")
                index = cuts[i]["frame_index"] if cuts[i]["frame_index"] is not None else "unknown"
                span = spans[i]
                timing = (
                    f"\t{_fmt_time(span['start'])}\t{_fmt_time(span['end'])}\t{span['duration']:.2f}\t{span['motion']}"
                    if span
                    else ""
                )
                lines.append(f"{stem}.jpg\t{cuts[i]['label']}\t{cuts[i]['kind']}\t{index}{timing}")
            archive.writestr("cuts.txt", "\n".join(lines))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path, len(wanted)


def _zip_response(sid, path):
    stem = re_safe(Path(_sessions[sid]["video_name"]).stem or "video")
    return _file_response(
        sid,
        path,
        media_type="application/zip",
        filename=f"关键帧_{stem}.zip",
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.get("/api/export/{sid}")
@_storage_guard
def export(sid: str, ids: str = "", run: str = ""):
    path, _ = _export_archive(sid, ids, run)
    return _zip_response(sid, path)


@app.post("/api/export/{sid}")
@_storage_guard
def prepare_export(sid: str, payload: dict):
    # 先返回可检查的 JSON，随后由浏览器原生下载，避免在 JS 中构造整个 ZIP Blob。
    path, count = _export_archive(sid, str(payload.get("ids", "")), str(payload.get("run", "")))
    _storage.prepared[(sid, path.name)] = __import__("time").monotonic() + 60
    return {"url": f"/api/download/{sid}/{path.name}", "count": count}


@app.get("/api/download/{sid}/{name}")
@_storage_guard
def download_archive(sid: str, name: str):
    _session(sid)
    if Path(name).name != name or not name.endswith(".zip") or "\\" in name:
        raise HTTPException(404, "下载不存在")
    path = _safe_path(sid, "exports", name)
    if not path.is_file():
        raise HTTPException(404, "下载已完成或文件不存在，请重新打包")
    _storage.prepared.pop((sid, name), None)
    return _zip_response(sid, path)


def re_safe(name: str) -> str:
    return re_sub(name)


def re_sub(name: str) -> str:
    import re

    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name)[:80]


# ---------------------------------------------------------------- Pillow reference collages


def _collage_result(sid, payload):
    ses = _session(sid)
    result = ses.get('results', {}).get(ses.get('latest_run'))
    if not result or payload.get('run') != result['run']:
        raise HTTPException(409, '结果已更新，请刷新后重新制作拼图')
    return result


def _collage_ids(result, payload, limit):
    ids = payload.get('ids')
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= limit
        or any(type(i) is not int or not 0 <= i < len(result['cuts']) for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise HTTPException(400, f'请选择1至{limit}张不重复的有效关键帧')
    return ids


def _collage_options(payload):
    import collage_engine

    try:
        return collage_engine.validate_options(payload, payload.get('ids', []))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _collage_sources(sid, payload, limit=16):
    result = _collage_result(sid, payload)
    ids = _collage_ids(result, payload, limit)
    sources = []
    for i in ids:
        try:
            path = workspace_store.asset_path(WORK, sid, result, i)
            run, number = workspace_store.asset_ref(result, i)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(409, '选中的原图文件缺失，请恢复工作区或重新分析')
        src = f'/api/frame/{sid}/{run}/{number}'
        thumb = workspace_store.asset_path(WORK, sid, result, i, 'thumbs')
        sources.append(
            dict(
                id=i,
                path=path,
                label=result['cuts'][i].get('label', '未知'),
                src=src,
                thumb=f'/api/thumb/{sid}/{run}/{number}' if thumb.is_file() else src,
            )
        )
    return sources


def _collage_plan(payload, sources):
    import collage_engine
    from PIL.Image import DecompressionBombError

    try:
        return collage_engine.plan(payload, sources)
    except (ValueError, OSError, DecompressionBombError) as exc:
        raise HTTPException(400, str(exc)) from exc


def _expire_collages(sid, keep=3):
    import collage_engine, time

    if _storage.active.get(sid, 0):
        return
    directory = _safe_path(sid, 'exports')
    if not directory.is_dir():
        return
    groups = {}
    for path in directory.iterdir():
        if collage_engine.managed_file(path.name):
            path = _safe_path(sid, 'exports', path.name)
            token = path.name[8:40]
            groups.setdefault(token, []).append(path)
    ordered = sorted(groups.values(), key=lambda paths: max(p.stat().st_mtime for p in paths), reverse=True)
    for n, paths in enumerate(ordered):
        if any(_storage.prepared.get((sid, p.name), 0) > time.monotonic() for p in paths):
            continue
        if n >= keep or time.time() - max(p.stat().st_mtime for p in paths) > 3600:
            for p in paths:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass


@app.post('/api/collage/{sid}/plan')
@_storage_guard
def plan_collage(sid: str, payload: dict):
    _expire_collages(sid)
    return _collage_plan(payload, _collage_sources(sid, payload))


@app.post('/api/collage/{sid}/render')
@_storage_guard
def render_collage(sid: str, payload: dict):
    import collage_engine, time

    sources = _collage_sources(sid, payload)
    plan = _collage_plan(payload, sources)
    if not plan['can_render']:
        raise HTTPException(400, plan['warning'])
    # The existing storage RLock serializes renders and protects all source files.
    _expire_collages(sid, keep=2)
    if (
        sum(
            1
            for (record, name), expiry in _storage.prepared.items()
            if record == sid and name.startswith('collage-') and expiry > time.monotonic()
        )
        >= 8
    ):
        raise HTTPException(429, '待查看拼图过多，请关闭旧成品或稍后再生成')
    token = uuid.uuid4().hex
    fmt = payload.get('format', 'png')
    ext = 'png' if fmt == 'png' else 'jpg'
    directory = _safe_path(sid, 'exports')
    directory.mkdir(exist_ok=True)
    name = f'collage-{token}'
    path = directory / f'{name}.{ext}'
    preview = directory / f'{name}-preview.jpg'
    metadata = directory / f'{name}.json'
    try:
        collage_engine.render(plan, sources, payload, path, preview)
        video_name = _collage_video_name(sid)
        layout = '智能排版' if plan['layout'] == 'justified' else f"{plan['columns']}列网格"
        filename = f"参考拼图_{video_name}_{layout}_{payload.get('page', 1):02d}.{ext}"
        workspace_store.atomic_json(
            metadata,
            dict(
                token=token,
                format=fmt,
                filename=filename,
                run=payload['run'],
                created_at=time.time(),
                width=plan['width'],
                height=plan['height'],
            ),
        )
    except Exception as exc:
        for p in (path, preview, metadata):
            p.unlink(missing_ok=True)
        raise HTTPException(400, '拼图生成失败：' + str(exc)) from exc
    _storage.prepared[(sid, metadata.name)] = time.monotonic() + 60
    base = f'/api/collage/{sid}/{token}'
    return dict(
        token=token,
        width=plan['width'],
        height=plan['height'],
        format=fmt,
        bytes=path.stat().st_size,
        preview_url=base + '/preview',
        image_url=base + '/image',
        download_url=base + '/download',
        plan=plan,
    )


def _collage_video_name(sid):
    return re_safe(Path(_session(sid).get('video_name', 'video')).stem or 'video')


@app.post('/api/collage/{sid}/render-all')
@_storage_guard
def render_all_collages(sid: str, payload: dict):
    """Every page of the selection, rendered one at a time into a zip with the storyboard CSV."""
    import collage_engine, time

    result = _collage_result(sid, payload)
    sources = _collage_sources(sid, payload, collage_engine.MAX_BATCH)
    capacity, _ = _collage_options(payload)
    ids = payload['ids']
    pages = [ids[i : i + capacity] for i in range(0, len(ids), capacity)]
    by_id = {s['id']: s for s in sources}
    plans = []
    for n, page_ids in enumerate(pages, 1):
        page = collage_engine.page_payload(payload, page_ids, n)
        plan = _collage_plan(page, [by_id[i] for i in page_ids])
        if not plan['can_render']:
            raise HTTPException(400, f'第 {n} 张拼图：{plan["warning"]}')
        plans.append((page, plan))
    _expire_collages(sid, keep=2)
    token = uuid.uuid4().hex
    fmt = payload.get('format', 'png')
    ext = 'png' if fmt == 'png' else 'jpg'
    directory = _safe_path(sid, 'exports')
    directory.mkdir(exist_ok=True)
    archive_path = directory / f'collage-{token}.zip'
    scratch = directory / f'collage-{token}.{ext}'
    metadata = directory / f'collage-{token}.json'
    video_name = _collage_video_name(sid)
    try:
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_STORED) as archive:
            for page, plan in plans:
                collage_engine.render(plan, [by_id[i] for i in page['ids']], page, scratch, None)
                archive.write(scratch, f"参考拼图_{video_name}_{page['page']:02d}.{ext}")
                scratch.unlink()
            archive.writestr(
                '分镜表.csv',
                collage_engine.storyboard_csv(payload, result['cuts'], _spans_for_export(sid, _session(sid), result)),
            )
        filename = f'参考拼图_{video_name}_共{len(pages)}张.zip'
        workspace_store.atomic_json(
            metadata,
            dict(
                token=token,
                format='zip',
                filename=filename,
                run=payload['run'],
                created_at=time.time(),
                pages=len(pages),
            ),
        )
    except Exception as exc:
        for p in (archive_path, scratch, metadata):
            p.unlink(missing_ok=True)
        raise HTTPException(400, '拼图打包失败：' + str(exc)) from exc
    _storage.prepared[(sid, metadata.name)] = time.monotonic() + 60
    return dict(
        token=token,
        pages=len(pages),
        bytes=archive_path.stat().st_size,
        download_url=f'/api/collage/{sid}/{token}/download',
    )


@app.post('/api/collage/{sid}/storyboard')
@_storage_guard
def collage_storyboard(sid: str, payload: dict):
    import collage_engine
    from urllib.parse import quote

    result = _collage_result(sid, payload)
    _collage_ids(result, payload, collage_engine.MAX_BATCH)
    _collage_options(payload)
    name = quote(f'分镜表_{_collage_video_name(sid)}.csv')
    return Response(
        collage_engine.storyboard_csv(payload, result['cuts'], _spans_for_export(sid, _session(sid), result)),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f"attachment; filename*=UTF-8''{name}"},
    )


@app.get('/api/collage/{sid}/{token}/{view}')
@_storage_guard
def collage_file(sid: str, token: str, view: str):
    import collage_engine, json

    _session(sid)
    if not collage_engine.TOKEN.fullmatch(token) or view not in ('preview', 'image', 'download'):
        raise HTTPException(404, '拼图不存在')
    metadata = _safe_path(sid, 'exports', f'collage-{token}.json')
    try:
        info = json.loads(metadata.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise HTTPException(404, '拼图已清理，请重新生成')
    if info.get('format') not in ('png', 'jpeg', 'zip'):
        raise HTTPException(404, '拼图记录无效')
    if info['format'] == 'zip':
        path = _safe_path(sid, 'exports', f'collage-{token}.zip')
        if view != 'download' or not path.is_file():
            raise HTTPException(404, '拼图已清理，请重新生成')
        return _file_response(
            sid, path, media_type='application/zip', filename=re_safe(info.get('filename', f'参考拼图_{token[:8]}.zip'))
        )
    ext = 'png' if info['format'] == 'png' else 'jpg'
    path = _safe_path(sid, 'exports', f'collage-{token}-preview.jpg' if view == 'preview' else f'collage-{token}.{ext}')
    if not path.is_file():
        raise HTTPException(404, '拼图已清理，请重新生成')
    kwargs = {'media_type': 'image/jpeg' if view == 'preview' or ext == 'jpg' else 'image/png'}
    if view == 'download':
        kwargs['filename'] = re_safe(info.get('filename', f'参考拼图_{token[:8]}.{ext}'))
    return _file_response(sid, path, **kwargs)


@app.delete('/api/collage/{sid}/{token}')
@_storage_guard
def delete_collage(sid: str, token: str):
    import collage_engine

    _session(sid)
    if not collage_engine.TOKEN.fullmatch(token):
        raise HTTPException(400, '拼图标识无效')
    if _storage.active.get(sid, 0):
        raise HTTPException(409, '文件正在发送，请稍后清理')
    for suffix in ('.png', '.jpg', '.zip', '-preview.jpg', '.json'):
        path = _safe_path(sid, 'exports', f'collage-{token}{suffix}')
        path.unlink(missing_ok=True)
        _storage.prepared.pop((sid, path.name), None)
    return {'status': 'done'}


# ---------------------------------------------------------------- 静态页面

_storage = storage_manager.install(sys.modules[__name__])

_asset_versions: dict[tuple, str] = {}


def _asset_version(name):
    """页面引用的脚本 / 样式的内容指纹；文件没变时复用，变了就换新号。"""
    try:
        path = (STATIC / name).resolve()
        path.relative_to(STATIC.resolve())
        stat = path.stat()
    except (OSError, ValueError):
        return None
    key = (name, stat.st_mtime_ns, stat.st_size)
    if key not in _asset_versions:
        import hashlib

        _asset_versions[key] = hashlib.sha1(path.read_bytes()).hexdigest()[:10]
    return _asset_versions[key]


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page():
    """给页面里的本地脚本和样式加上内容版本号（/app.js?v=…），
    更新后浏览器必定取新文件，不会把缓存的旧脚本和新页面混在一起。"""
    import re

    html = (STATIC / "index.html").read_text(encoding="utf-8")

    def stamp(match):
        version = _asset_version(match.group(2).lstrip("/"))
        return f'{match.group(1)}="{match.group(2)}?v={version}"' if version else match.group(0)

    html = re.sub(r'(src|href)="(/[^"?#:]+\.(?:js|css))"', stamp, html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print(f"关键帧工具已启动： http://127.0.0.1:{port}   （Ctrl+C 退出）")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

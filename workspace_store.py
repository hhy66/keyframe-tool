"""Versioned, atomic workspace manifests. Never follows links outside a workspace."""
from __future__ import annotations
import json
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import numpy as np

DEFAULT_PARAMS = dict(sensitivity=50, include_ends=False, min_scene_seconds=0.1, suppress_flash=True)


def safe_path(root, *parts):
    root = Path(root)
    if root.is_symlink() or getattr(root, 'is_junction', lambda: False)():
        raise ValueError('工作区不能是符号链接或目录联接')
    path = root
    for part in parts:
        if not isinstance(part, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', part) or part in ('.', '..'):
            raise ValueError('非法工作区路径')
        path = path / part
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('工作区包含符号链接或目录联接')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('工作区路径越界')
    return path


def atomic_json(path, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    fd, name = tempfile.mkstemp(prefix='.manifest-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save(root, sid, ses, result=None):
    directory = safe_path(root, sid)
    directory.mkdir(parents=True, exist_ok=True)
    video = ses.get('video_path')
    video_name = None
    if video is not None:
        video = Path(video)
        video_name = video.name
        safe_path(root, sid, video_name)
        if video.resolve().parent != directory.resolve():
            # In-memory callers may register an input outside the session folder.
            import shutil
            video_name = 'video' + video.suffix.lower()
            target = safe_path(root, sid, video_name)
            if not target.exists(): shutil.copyfile(video, target)
    cache = ses.get('cache', {})
    cache_name = ses.get('cache_file')
    if 'times' in cache and not cache_name:
        import uuid
        cache_name = 'scan-' + uuid.uuid4().hex + '.npz'
        cache_path = safe_path(root, sid, cache_name)
        with cache_path.open('xb') as output:
            np.savez_compressed(output, **{key:cache[key] for key in
                                ('times','diffs','bridges','fps','frames','estimated_time')})
            output.flush(); os.fsync(output.fileno())
    results = dict(ses.get('results', {}))
    if result is not None: results[result['run']] = result
    latest = result['run'] if result is not None else ses.get('latest_run')
    manifest = dict(schema=1, video_file=video_name, video_name=ses['video_name'],
                    meta=ses['meta'], updated_at=datetime.now(timezone.utc).isoformat(),
                    latest_run=latest, results=results, cache_file=cache_name,
                    manual_additions=sorted(ses.get('manual_additions', set())),
                    manual_adjustments={str(k):v for k,v in ses.get('manual_adjustments', {}).items()},
                    excluded=list(ses.get('excluded', [])))
    atomic_json(safe_path(root, sid, 'manifest.json'), manifest)
    ses['cache_file'] = cache_name
    ses['latest_run'] = latest
    ses['updated_at'] = manifest['updated_at']


def load(root, sid, load_cache=True):
    directory = safe_path(root, sid)
    if not directory.is_dir(): raise FileNotFoundError('工作区不存在')
    path = safe_path(root, sid, 'manifest.json')
    if not path.exists(): return legacy(root, sid)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['schema'] != 1: raise ValueError('不支持的工作区版本')
        results = data['results']
        if not isinstance(results, dict): raise ValueError('结果索引损坏')
        missing = 0
        for run, result in results.items():
            safe_path(root, sid, 'frames', run)
            if result['run'] != run or not isinstance(result['cuts'], list): raise ValueError('结果索引损坏')
            frames, thumbs = [], []
            for i, cut in enumerate(result['cuts']):
                number = cut.get('file_index', i)
                if isinstance(number, bool) or not isinstance(number, int) or number < 0: raise ValueError('图片编号无效')
                f = safe_path(root, sid, 'frames', run, f'{number}.jpg')
                t = safe_path(root, sid, 'thumbs', run, f'{number}.jpg')
                cut['available'] = f.is_file()
                if not cut['available']: missing += 1
                frames.append(f'/api/frame/{sid}/{run}/{number}')
                thumbs.append(f'/api/thumb/{sid}/{run}/{number}' if t.is_file() else frames[-1])
            result['frames'], result['thumbs'] = frames, thumbs
        latest = data.get('latest_run')
        if latest is not None and latest not in results: raise ValueError('最新结果索引损坏')
        video = safe_path(root, sid, data['video_file']) if data.get('video_file') else None
        if video is not None and not video.is_file(): video = None
        cache = {}
        cache_name = data.get('cache_file')
        if cache_name and not safe_path(root, sid, cache_name).is_file():
            cache_name = None
        if cache_name and load_cache:
            with np.load(safe_path(root, sid, cache_name), allow_pickle=False) as stored:
                for key in ('times','diffs','bridges'):
                    a = stored[key]
                    if a.ndim != 1 or a.dtype.kind not in 'fi' or not np.isfinite(a).all(): raise ValueError('扫描缓存损坏')
                    cache[key] = a
                if not len(cache['times']) or any(len(cache[k]) != len(cache['times']) for k in ('diffs','bridges')): raise ValueError('扫描缓存损坏')
                if np.any(np.diff(cache['times']) <= 0) or cache['times'][0] < 0: raise ValueError('扫描时间戳损坏')
                cache.update(fps=float(stored['fps']), frames=int(stored['frames']), estimated_time=bool(stored['estimated_time']))
                if not np.isfinite(cache['fps']) or cache['fps'] <= 0 or cache['frames'] != len(cache['times']): raise ValueError('扫描缓存损坏')
        elif cache_name:
            safe_path(root, sid, cache_name)
        ses = dict(video_path=video, video_name=data['video_name'], meta=data['meta'],
                   results=results, latest_run=latest, cache=cache, cache_file=cache_name,
                   manual_additions=set(data.get('manual_additions', [])),
                   manual_adjustments={int(k):v for k,v in data.get('manual_adjustments', {}).items()},
                   excluded=data.get('excluded', []), updated_at=data.get('updated_at'), legacy=False,
                   workspace_note=f'{missing} 张原图缺失，不能导出缺失图片' if missing else '')
        if video is None:
            ses['workspace_note'] += ' 原视频缺失，仅可查看和导出已有截图'
            for result in results.values(): result['meta']['gallery_only'] = True
        return ses
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ValueError(f'工作区记录损坏：{exc}') from exc


def legacy(root, sid):
    directory = safe_path(root, sid)
    results = {}
    video = None
    for item in directory.glob('video.*'):
        item = safe_path(root, sid, item.name)
        if item.is_file() and item.suffix.lower() != '.json': video = item; break
    frames = safe_path(root, sid, 'frames')
    run_dirs = sorted(frames.iterdir(), key=lambda p:p.stat().st_mtime) if frames.is_dir() else []
    for item in run_dirs:
        run = item.name
        path = safe_path(root, sid, 'frames', run)
        if not path.is_dir(): continue
        numbers = sorted(int(p.stem) for p in path.glob('*.jpg') if p.stem.isdigit() and p.stem == str(int(p.stem)))
        if not numbers: continue
        urls = [f'/api/frame/{sid}/{run}/{n}' for n in numbers]
        for n in numbers: safe_path(root, sid, 'frames', run, f'{n}.jpg')
        results[run] = dict(run=run, video_name=video.name if video else sid,
                           params=dict(DEFAULT_PARAMS), meta={'gallery_only':True},
                           cuts=[dict(kind='archived', frame_index=None, time=None, label='时间未知', file_index=n, available=True) for n in numbers],
                           frames=urls, thumbs=[f'/api/thumb/{sid}/{run}/{n}' if safe_path(root,sid,'thumbs',run,f'{n}.jpg').is_file() else urls[i] for i,n in enumerate(numbers)])
    if not results and video is None: raise ValueError('未找到可恢复的视频或截图')
    return dict(video_path=video, video_name=video.name if video else sid, meta={}, cache={}, results=results,
                latest_run=next(reversed(results), None), manual_additions=set(), manual_adjustments={}, excluded=[],
                updated_at=datetime.fromtimestamp(directory.stat().st_mtime, timezone.utc).isoformat(), legacy=True,
                workspace_note='旧工作区无时间元数据，截图按原文件编号恢复；可重新分析原视频')

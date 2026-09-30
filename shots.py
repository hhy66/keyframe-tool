"""镜头：把每张关键帧还原成它所在的整个镜头——起止帧、时长，以及首帧 / 中间帧 / 尾帧三张小图。

镜头从一个切换点开始，到下一个切换点的前一帧结束；手动补帧也算作一个新镜头的开始。
三张小图按视频帧号存放（`strip/{帧号}.jpg`），重新分析、微调后同一帧直接复用。
"""

from __future__ import annotations

from pathlib import Path

import cv2

from detection import _grab_frame_at, _write_jpeg

STRIP_SIZE = 480  # 镜头小图最长边
SEEK_GAP = 120  # 两个目标帧相隔超过这么多帧时跳转，否则顺序往后读（跳转要从关键帧重新解码，近处反而慢）
STRIP_NAMES = ('首帧', '中间', '尾帧')


def boundary(cut):
    """镜头开始的帧：检测到的切换帧；手动补帧、微调没有切换帧，按截图所在帧。"""
    return int(cut.get('source_frame', cut['frame_index']))


def strip_frames(start, end):
    """首帧、中间帧、尾帧的帧号。"""
    return [start, (start + end) // 2, end]


def spans(result, times=None):
    """每张截图所在镜头的起止帧、时间和时长（与 result['cuts'] 同序）。旧版没有帧号的记录返回 None。"""
    cuts = result.get('cuts') or []
    meta = result.get('meta') or {}
    total = int(meta.get('frames') or 0)
    fps = float(meta.get('fps') or 0)
    if total <= 0 or fps <= 0 or any(cut.get('frame_index') is None for cut in cuts):
        return [None] * len(cuts)

    def at(frame):
        return float(times[frame]) if times is not None and 0 <= frame < len(times) else frame / fps

    order = sorted(range(len(cuts)), key=lambda i: boundary(cuts[i]))
    found = [None] * len(cuts)
    for k, i in enumerate(order):
        start = min(max(0, boundary(cuts[i])), total - 1)
        end = boundary(cuts[order[k + 1]]) - 1 if k + 1 < len(order) else total - 1
        end = max(start, min(end, total - 1))
        begin, finish = at(start), at(end) + 1 / fps
        found[i] = {
            'start_frame': start,
            'end_frame': end,
            'frames': end - start + 1,
            'start': round(begin, 3),
            'end': round(finish, 3),
            'duration': round(finish - begin, 3),
            'strip': strip_frames(start, end),
        }
    return found


def strip_path(root: Path, frame):
    return root / f'{int(frame)}.jpg'


def missing(root: Path, frames):
    return sorted({int(f) for f in frames if not strip_path(root, f).is_file()})


def extract(video_path, frames, root: Path, progress=None):
    """按帧号从小到大读出需要的帧，存成小图。相近的帧顺序往后读，远的才跳转。返回读取失败的帧号。"""
    root.mkdir(parents=True, exist_ok=True)
    targets = missing(root, frames)
    failed = []
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return targets
        position = None  # 下一次 read 会读到的帧号
        for n, target in enumerate(targets, 1):
            if position is None or target < position or target - position > SEEK_GAP:
                image = _grab_frame_at(cap, target)
            else:
                image = None
                for _ in range(target - position):
                    if not cap.grab():
                        break
                else:
                    ok, image = cap.read()
                    image = image if ok else None
            position = target + 1
            if image is None:
                failed.append(target)
                position = None  # 读取出错后重新定位
            else:
                h, w = image.shape[:2]
                scale = min(1.0, STRIP_SIZE / max(h, w))
                if scale < 1:
                    image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
                _write_jpeg(strip_path(root, target), image, 82)
            if progress:
                progress(n, len(targets))
    finally:
        cap.release()
    return failed


def prune(root: Path, keep):
    """删掉不再被任何结果用到的镜头小图。"""
    if not root.is_dir():
        return
    keep = {int(f) for f in keep}
    for path in root.glob('*.jpg'):
        if path.stem.isdigit() and int(path.stem) not in keep:
            path.unlink(missing_ok=True)

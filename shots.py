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


def _read_in_order(cap, targets, visit, progress=None):
    """按帧号从小到大读出 targets，每读到一帧调用 visit(帧号, 图像或 None)。相近的帧顺序往后读，远的才跳转。"""
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
        position = None if image is None else target + 1  # 读取出错后重新定位
        visit(target, image)
        if progress:
            progress(n, len(targets))


def _save_strip(root, frame, image):
    h, w = image.shape[:2]
    scale = min(1.0, STRIP_SIZE / max(h, w))
    if scale < 1:
        image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    _write_jpeg(strip_path(root, frame), image, 82)


def extract(video_path, frames, root: Path, progress=None):
    """只截镜头小图。返回读取失败的帧号。"""
    failed, _ = scan(video_path, [], root, frames, progress=progress)
    return failed


def scan(video_path, spans, root: Path, strip_frames=(), times=None, fps=25.0, progress=None):
    """一次顺序读视频：截出缺少的镜头小图，并为 spans 里的每个镜头测运镜。
    spans：[(key, span)]，返回 (读取失败的帧号, {key: 运镜})。"""
    import motion  # 运镜分析只在需要时加载

    root.mkdir(parents=True, exist_ok=True)
    strips = set(missing(root, strip_frames))
    samples = {}  # 帧号 -> 属于哪个镜头
    plans = []
    for key, span in sorted(spans, key=lambda item: item[1]['start_frame']):
        frames = motion.sample_frames(span['start_frame'], span['end_frame'])
        plans.append({'key': key, 'frames': frames, 'grays': [], 'times': [], 'ends': {}})
        for frame in frames:
            samples[frame] = plans[-1]
    targets = sorted(strips | set(samples))
    failed, found = [], {}

    def when(frame):
        return float(times[frame]) if times is not None and 0 <= frame < len(times) else frame / fps

    def finish(plan):
        people = _people(plan['ends'].get('first'), plan['ends'].get('last'))
        found[plan['key']] = motion.analyze(plan['grays'], plan['times'], people)

    def visit(frame, image):
        if image is None:
            failed.append(frame)
        elif frame in strips:
            _save_strip(root, frame, image)
        plan = samples.get(frame)
        if plan is None:
            return
        if image is not None:
            plan['grays'].append(motion.small_gray(image))
            plan['times'].append(when(frame))
            if frame == plan['frames'][0]:
                plan['ends']['first'] = image
            if frame == plan['frames'][-1]:
                plan['ends']['last'] = image
        if frame == plan['frames'][-1]:
            finish(plan)
            plan['grays'] = plan['ends'] = None  # 释放内存

    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return targets, {}
        _read_in_order(cap, targets, visit, progress)
    finally:
        cap.release()
    return failed, found


def _people(first, last):
    """镜头首尾帧里最大的人物框，用来判断跟拍；模型不可用时返回 None。"""
    if first is None or last is None:
        return None
    try:
        import shot_analysis

        if not shot_analysis.PERSON_MODEL.is_file():
            return None
        boxes = []
        for image in (first, last):
            h, w = image.shape[:2]
            scale = min(1.0, 640 / max(h, w))
            small = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
            people = shot_analysis.find_people(small)
            boxes.append(people[0]['box'] if people else None)
        return tuple(boxes)
    except cv2.error:
        return None


def prune(root: Path, keep):
    """删掉不再被任何结果用到的镜头小图。"""
    if not root.is_dir():
        return
    keep = {int(f) for f in keep}
    for path in root.glob('*.jpg'):
        if path.stem.isdigit() and int(path.stem) not in keep:
            path.unlink(missing_ok=True)

"""运镜：在一个镜头里按时间取一串画面，逐对估计整幅画面的平移、缩放、旋转，累加后换算成推、拉、摇 / 移、
升降、旋转、手持晃动、固定机位和跟拍，并给出一句中文描述和对应的英文说法。纯计算，不涉及 HTTP 和工作区。

画面整体向左移动说明镜头向右运动；整体放大说明推近。镜头原地转动（摇）和整机平移（移）的差别在于前后景是否错位，
本机难以可靠区分，所以统一写作"摇（或横移）"。阈值集中在文件顶部，便于用真实镜头校准。
"""

from __future__ import annotations

import math

import cv2
import numpy as np

MOTION_VERSION = 1
SAMPLE_WIDTH = 320  # 估计运动用的小图宽度
MAX_SAMPLES = 48  # 每个镜头最多取多少帧
MIN_SHOT_SECONDS = 0.3

PAN_MIN = 0.08  # 整个镜头累计平移达到画面宽（高）的 8% 才算在运动
ZOOM_MIN = 0.06  # 累计缩放 6% 以上才算推 / 拉
ROLL_MIN = 3.0  # 累计旋转（度）
SPEED_SLOW = 0.1  # 每秒移动画面的比例：低于此为缓慢
SPEED_FAST = 0.4  # 高于此为快速
ZOOM_SLOW = 0.05  # 每秒缩放的对数：低于此为缓慢
ZOOM_FAST = 0.2
SHAKE_SLIGHT = 0.002  # 相邻两步速度变化的标准差（画面宽度比例）
SHAKE_STRONG = 0.007
FOLLOW_STILL = 0.12  # 跟拍：人物在画面里的位置变化不超过此比例
FOLLOW_SIZE = 0.2  # 跟拍：人物至少占画面高度的比例


def sample_frames(start, end):
    """镜头 [start, end] 里要读的帧：均匀分布，最多 48 帧；首尾必取。"""
    count = end - start + 1
    if count <= 1:
        return [start]
    step = max(1, math.ceil(count / MAX_SAMPLES))
    frames = list(range(start, end + 1, step))
    if frames[-1] != end:
        frames.append(end)
    return frames


def small_gray(bgr):
    h, w = bgr.shape[:2]
    scale = SAMPLE_WIDTH / w
    small = cv2.resize(bgr, (SAMPLE_WIDTH, max(8, round(h * scale))), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


_orb = None


def _features(gray, mask=None):
    global _orb
    if _orb is None:
        _orb = cv2.ORB_create(600, fastThreshold=12)
    return _orb.detectAndCompute(gray, mask)


def background_mask(shape, people):
    """估计镜头运动时遮住人物，只看背景：跟拍时人物在画面里基本不动，按人物算会误以为镜头没动。
    people：首尾帧的人物框（0～1 比例），各向外扩 10%。遮完剩下太少时不遮。"""
    boxes = [box for box in (people or ()) if box]
    if not boxes:
        return None
    h, w = shape
    mask = np.full((h, w), 255, np.uint8)
    for x, y, bw, bh in boxes:
        x0, y0 = int(max(0, (x - bw * 0.1) * w)), int(max(0, (y - bh * 0.1) * h))
        x1, y1 = int(min(w, (x + bw * 1.1) * w)), int(min(h, (y + bh * 1.1) * h))
        mask[y0:y1, x0:x1] = 0
    return mask if mask.mean() > 255 * 0.35 else None


def step_motion(a, b, cache=None, mask=None, fallback=True):
    """两帧之间整幅画面的变化：中心点位移（dx, dy，像素）、缩放 s、旋转（度）。对不上时返回 None。
    先用特征点 + RANSAC 拟合相似变换，特征点太少（大片纯色、暗场）时退回相位相关，只估平移；
    fallback=False 时不退回。mask 为 0 的地方（人物）不参与。"""
    h, w = a.shape
    first = cache.get(id(a)) if cache is not None else None
    if first is None:
        first = _features(a, mask)
        if cache is not None:
            cache[id(a)] = first
    second = cache.get(id(b)) if cache is not None else None
    if second is None:
        second = _features(b, mask)
    if cache is not None:
        cache[id(b)] = second
    (k1, d1), (k2, d2) = first, second
    if d1 is not None and d2 is not None and len(k1) >= 12 and len(k2) >= 12:
        matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d1, d2)
        matches = sorted(matches, key=lambda m: m.distance)[:300]
        if len(matches) >= 12:
            src = np.float32([k1[m.queryIdx].pt for m in matches])
            dst = np.float32([k2[m.trainIdx].pt for m in matches])
            matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=2.5)
            if matrix is not None and inliers is not None and int(inliers.sum()) >= 10:
                scale = math.hypot(matrix[0, 0], matrix[1, 0])
                angle = math.degrees(math.atan2(matrix[1, 0], matrix[0, 0]))
                center = np.array([w / 2, h / 2, 1.0])
                moved = matrix @ center
                if 0.7 < scale < 1.4:
                    return float(moved[0] - w / 2), float(moved[1] - h / 2), float(scale), float(angle)
    if not fallback:
        return None
    # 相位相关：只看平移，适合纹理少的画面
    window = cv2.createHanningWindow((w, h), cv2.CV_32F)
    if mask is not None:
        window = window * (mask.astype(np.float32) / 255.0)
    (dx, dy), response = cv2.phaseCorrelate(a.astype(np.float32), b.astype(np.float32), window)
    if response < 0.08:
        return None
    return float(dx), float(dy), 1.0, 0.0


def _travel(grays, cache, mask=None):
    """整个镜头累计的平移（画面宽 / 高的比例）、缩放（对数）和旋转（度）。
    一直拿同一张参考帧和后面的帧比，对不上了才换参考帧：比逐帧累加准，小角度的旋转、缓慢的推拉不会被吞掉。"""
    h, w = grays[0].shape
    pan = tilt = zoom = roll = 0.0
    anchor, last = 0, None
    for j in range(1, len(grays)):
        # 隔得远的两帧只信特征点匹配；对不上就换参考帧，不用只估平移的相位相关顶替
        found = step_motion(grays[anchor], grays[j], cache, mask, fallback=j - anchor == 1)
        far = found is not None and (abs(found[0]) > 0.35 * w or abs(found[1]) > 0.35 * h)
        if found is not None and not far:
            last = found
            continue
        if last is None:  # 连相邻帧都对不上：跳过这一步
            anchor = j
            continue
        pan, tilt = pan + last[0] / w, tilt + last[1] / h
        zoom, roll = zoom + math.log(last[2]), roll + last[3]
        anchor, last = j - 1, step_motion(grays[j - 1], grays[j], cache, mask)
    if last is not None:
        pan, tilt = pan + last[0] / w, tilt + last[1] / h
        zoom, roll = zoom + math.log(last[2]), roll + last[3]
    return pan, tilt, zoom, roll


def _speed(rate, slow, fast):
    return '缓慢' if rate < slow else '快速' if rate > fast else '中速'


def _english_speed(word):
    return {'缓慢': 'slow', '中速': 'steady', '快速': 'fast'}[word]


def analyze(grays, times, people=None):
    """一个镜头的运镜。grays：按时间排列的小灰度图；times：对应秒数；
    people：(首帧人物框, 尾帧人物框) 或 None，用来判断跟拍。"""
    duration = float(times[-1] - times[0]) if len(times) > 1 else 0.0
    if len(grays) < 3 or duration < MIN_SHOT_SECONDS:
        return {'label': '', 'text': '镜头太短，无法判断运镜', 'terms': [], 'confidence': '', 'moves': {}}
    h, w = grays[0].shape
    if float(np.median([g.std() for g in grays])) < 4:
        return {'label': '', 'text': '无法判断运镜（画面几乎没有细节）', 'terms': [], 'confidence': '', 'moves': {}}
    cache = {}
    mask = background_mask((h, w), people)
    steps = [step_motion(grays[i], grays[i + 1], cache, mask) for i in range(len(grays) - 1)]
    known = [s for s in steps if s is not None]
    if len(known) < max(2, 0.5 * len(steps)):
        return {
            'label': '',
            'text': '无法判断运镜（画面缺少纹理，或变化太剧烈）',
            'terms': [],
            'confidence': '',
            'moves': {},
        }
    pan, tilt, zoom, roll = _travel(grays, cache, mask)
    # 手持晃动：相邻两步之间速度的变化。匀速的推拉摇几乎为 0，手持忽快忽慢
    dx = np.array([s[0] for s in known]) / w
    dy = np.array([s[1] for s in known]) / h
    jitter = float(np.hypot(np.diff(dx), np.diff(dy) * h / w).std()) if len(known) > 2 else 0.0

    parts, terms, card = [], [], []
    if abs(zoom) >= math.log(1 + ZOOM_MIN):
        speed = _speed(abs(zoom) / duration, ZOOM_SLOW, ZOOM_FAST)
        word = '推近' if zoom > 0 else '拉远'
        parts.append(f'{speed}{word}')
        card.append(word)
        terms.append(f'{_english_speed(speed)} {"push in" if zoom > 0 else "pull out"}')
    across, down = abs(pan) >= PAN_MIN, abs(tilt) >= PAN_MIN
    follow = False
    if across or down:
        rate = max(abs(pan), abs(tilt)) / duration
        speed = _speed(rate, SPEED_SLOW, SPEED_FAST)
        # 画面内容向左移动 = 镜头向右运动；画面内容向上移动 = 镜头向下
        horizontal = '向右摇（或横移）' if pan < 0 else '向左摇（或横移）'
        vertical = '向下（下摇或降）' if tilt < 0 else '向上（上摇或升）'
        if across and (not down or abs(pan) >= 2 * abs(tilt)):
            direction, short = horizontal, ('右摇/移' if pan < 0 else '左摇/移')
            english = 'pan right' if pan < 0 else 'pan left'
        elif down and (not across or abs(tilt) >= 2 * abs(pan)):
            direction, short = vertical, ('下摇/降' if tilt < 0 else '上摇/升')
            english = 'tilt down' if tilt < 0 else 'tilt up'
        else:
            direction = f'斜向运动：{horizontal[:2]}并{vertical[:2]}'
            short = f'{horizontal[1]}{vertical[1]}斜移'
            english = f'diagonal move {"right" if pan < 0 else "left"} and {"down" if tilt < 0 else "up"}'
        if people:
            first, last = people
            if first and last and first[3] >= FOLLOW_SIZE and last[3] >= FOLLOW_SIZE:
                shift = math.hypot(
                    (first[0] + first[2] / 2) - (last[0] + last[2] / 2),
                    (first[1] + first[3] / 2) - (last[1] + last[3] / 2),
                )
                follow = shift <= FOLLOW_STILL
        if follow:
            parts.append(f'{speed}跟拍，镜头{direction}')
            card.append('跟拍')
            terms.append(f'{_english_speed(speed)} tracking shot following the subject, {english}')
        else:
            parts.append(f'{speed}{direction}')
            card.append(short)
            terms.append(f'{_english_speed(speed)} {english}')
    if abs(roll) >= ROLL_MIN:
        # 图像坐标 y 向下：角度为负是画面逆时针转
        parts.append(f'画面{"逆" if roll < 0 else "顺"}时针旋转约 {abs(roll):.0f}°')
        card.append('旋转')
        terms.append('camera roll')
    shake = '明显晃动' if jitter >= SHAKE_STRONG else '轻微晃动' if jitter >= SHAKE_SLIGHT else ''
    if not parts:
        text = f'手持，{shake}' if shake else '固定机位'
        card = ['手持'] if shake else ['固定']
        terms = ['handheld camera, shaky' if shake == '明显晃动' else 'handheld camera'] if shake else ['static shot']
    else:
        text = '，'.join(parts) + (f'；手持{shake}' if shake else '')
        if shake:
            terms.append('handheld')
    return {
        'label': ' · '.join(card),
        'text': text,
        'terms': terms,
        'confidence': '较准' if len(known) == len(steps) else '一般',
        'moves': {
            'pan': round(pan, 3),
            'tilt': round(tilt, 3),
            'zoom': round(math.exp(zoom), 3),
            'roll': round(roll, 1),
            'shake': round(jitter, 4),
            'follow': follow,
            'duration': round(duration, 2),
        },
    }

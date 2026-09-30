"""画面分析：从一张关键帧测出画幅、色彩、影调、明暗分布，以及景别、构图、水平与景深（shot_analysis.py）。
纯计算，不涉及 HTTP 和工作区。

所有结果都是可复现的测量值：同一张图每次得到同样的结果。阈值集中在本文件顶部，便于用真实镜头校准。
"""

from __future__ import annotations

import math

import cv2
import numpy as np

import shot_analysis

ANALYSIS_VERSION = 4  # 算法或阈值改变时加一，旧缓存会自动重算
WORK_SIZE = 640  # 分析用图的最长边
DETAIL_SIZE = 1280  # 找人脸用图的最长边：远处的小人脸需要更多像素
DARK_LIGHTNESS = 15  # Lab 亮度低于此值的像素不计入饱和度
PALETTE_SIZE = 160  # 主色聚类用图的最长边

# 画幅：黑边行的亮度上限（取该行 90% 分位，零星字幕不影响）；黑边最多占一侧的比例
BAR_LEVEL = 24
BAR_MAX_SHARE = 0.3
BAR_FLAT = 2.0  # 黑边内部亮度起伏（四分位差）的上限；暗角通常在 4 以上
ASPECTS = [
    ('9:16', 9 / 16),
    ('3:4', 3 / 4),
    ('1:1', 1.0),
    ('4:3', 4 / 3),
    ('3:2', 3 / 2),
    ('16:9', 16 / 9),
    ('1.85:1', 1.85),
    ('2:1', 2.0),
    ('2.39:1', 2.39),
]

# 色彩：Lab 色度（0 附近为灰）
CHROMA_GRAY = 10  # 低于此值的像素算作无彩色
SATURATION_LOW = 12  # 平均色度
SATURATION_HIGH = 28
TEMPERATURE_SHIFT = 0.25  # 暖色与冷色权重之差占比

# 影调：亮度 0～1
KEY_LOW = 0.3
KEY_HIGH = 0.66
CONTRAST_LOW = 0.45  # 5%～95% 分位亮度差
CONTRAST_HIGH = 0.78
CLIP_SHARE = 0.02  # 过曝 / 欠曝像素占比达到此值才提示
LIGHT_SHIFT = 0.1  # 画面两侧平均亮度差
GLARE_LEVEL = 0.85  # 低调画面里，亮度高于此值的像素算强光
GLARE_SHARE = 0.004  # 强光占画面 0.4% 以上时，算作局部强光
ACCENT_CHROMA = 15  # 点缀色：足够鲜艳（Lab 色度）
ACCENT_SHARE = 0.01  # 至少占画面 1%
ACCENT_GAP = 90  # 与主色调色相至少相差 90°


# ---------------------------------------------------------------- 画幅与黑边


def _bar(lines):
    """从边缘开始连续的黑色行数。"""
    count = 0
    for level in lines:
        if level >= BAR_LEVEL:
            break
        count += 1
    return count


def _flat_black(region):
    """一块边缘区域是不是真正的黑边：几乎纯黑、每行（列）内部均匀、各行（列）亮度一致。
    暗角、暗场景的边缘虽然也很暗，但内部有明暗起伏，不算黑边。字幕只占少数像素，不影响四分位。"""
    if not region.size:
        return False
    spread = np.percentile(region, 75, axis=1) - np.percentile(region, 25, axis=1)
    level = np.percentile(region, 50, axis=1)
    return bool(
        np.median(spread) <= BAR_FLAT and level.max() <= BAR_LEVEL and level.max() - level.min() <= BAR_FLAT * 2
    )


def find_bars(gray):
    """上下（遮幅）或左右（柱状）黑边的像素数。只有两侧都有、宽度接近、而且是均匀纯黑时才算黑边，
    避免把夜景、暗角误判。黑边里压了字幕时，那几行只要多数像素仍是黑的，也算黑边。"""
    h, w = gray.shape
    bars = {'top': 0, 'bottom': 0, 'left': 0, 'right': 0}
    for first, second, axis, size in (('top', 'bottom', 1, h), ('left', 'right', 0, w)):
        lines = gray if axis == 1 else gray.T  # 每一行是一条从画面一侧到另一侧的线
        strict = np.percentile(lines, 90, axis=1)
        loose = np.percentile(lines, 50, axis=1)
        a, b = _bar(strict), _bar(strict[::-1])
        if a >= size or not a or not b:
            continue  # 全黑画面或只有一侧暗
        widest = max(a, b)
        if _bar(loose[:widest]) == widest and _bar(loose[::-1][:widest]) == widest:
            a = b = widest  # 较窄一侧只是被字幕打断
        if a > size * BAR_MAX_SHARE or b > size * BAR_MAX_SHARE:
            continue
        if abs(a - b) > max(4, 0.25 * widest):
            continue
        if not (_flat_black(lines[:a]) and _flat_black(lines[::-1][:b])):
            continue
        bars[first], bars[second] = a, b
    return bars


def aspect_name(ratio):
    """最接近的常见画幅；相差 4% 以上时直接写比值。"""
    name, value = min(ASPECTS, key=lambda item: abs(math.log(ratio / item[1])))
    return name if abs(math.log(ratio / value)) < 0.04 else f'{ratio:.2f}:1'


def orientation(ratio):
    return '竖屏' if ratio < 0.9 else '方形' if ratio <= 1.1 else '横屏'


# ---------------------------------------------------------------- 色彩


def _hue_name(hue, lightness, chroma):
    """Lab 色相角转常用中文颜色名。"""
    if chroma < CHROMA_GRAY:
        return (
            '黑'
            if lightness < 18
            else '深灰'
            if lightness < 40
            else '灰'
            if lightness < 65
            else '浅灰'
            if lightness < 88
            else '白'
        )
    # Lab 色相角：纯红约 40°、黄约 100°、绿约 135°、青约 196°、纯蓝约 306°、品红约 328°、粉约 350°
    names = [
        (20, '品红'),
        (50, '红'),
        (85, '橙'),
        (110, '黄'),
        (130, '黄绿'),
        (160, '绿'),
        (185, '青绿'),
        (235, '青'),
        (315, '蓝'),
        (340, '紫'),
        (360, '品红'),
    ]
    name = next(label for limit, label in names if hue < limit)
    if name in ('橙', '红') and lightness < 45:
        return '棕' if name == '橙' else '暗红'
    if lightness < 35:
        return '深' + name
    if lightness > 78:
        return '浅' + name
    return name


def _lab(bgr):
    """8 位 BGR 转真实单位的 Lab：L 0～100，a/b 约 -128～127。"""
    return cv2.cvtColor(bgr.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)


def _hex(lab_color):
    pixel = cv2.cvtColor(np.array([[lab_color]], np.float32), cv2.COLOR_LAB2BGR)[0, 0]
    b, g, r = (int(round(min(1.0, max(0.0, v)) * 255)) for v in pixel)
    return f'#{r:02x}{g:02x}{b:02x}'


def palette(bgr, count=5):
    """主色卡：按 Lab 聚类，合并几乎相同的颜色，按面积从大到小，只保留占比不少于 3% 的颜色。"""
    h, w = bgr.shape[:2]
    scale = min(1.0, PALETTE_SIZE / max(h, w))
    small = cv2.resize(bgr, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    samples = _lab(small).reshape(-1, 3)
    k = min(6, len(samples))
    cv2.setRNGSeed(7)  # 同一张图每次得到同样的色卡
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    _, labels, centers = cv2.kmeans(samples, k, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
    shares = np.bincount(labels.ravel(), minlength=k) / len(samples)
    groups = []
    for index in np.argsort(-shares):
        center, share = centers[index], float(shares[index])
        for group in groups:
            if np.linalg.norm(group['center'] - center) < 8:  # 肉眼难以区分
                total = group['share'] + share
                group['center'] = (group['center'] * group['share'] + center * share) / total
                group['share'] = total
                break
        else:
            groups.append({'center': center, 'share': share})
    groups.sort(key=lambda g: -g['share'])
    colors = []
    for group in groups:
        if group['share'] < 0.03 or len(colors) == count:
            break
        lightness, a, b = (float(v) for v in group['center'])
        chroma = math.hypot(a, b)
        hue = math.degrees(math.atan2(b, a)) % 360
        colors.append(
            {
                'hex': _hex(group['center']),
                'share': round(group['share'], 3),
                'name': _hue_name(hue, lightness, chroma),
                'hue': round(hue, 1),
                'chroma': round(chroma, 1),
                'lightness': round(lightness, 1),
            }
        )
    return colors


def _warmth(hue):
    """色相角的冷暖：暖 1，冷 -1，绿与品红算中性 0。"""
    if hue >= 345 or hue < 105:
        return 1
    if 160 <= hue < 320:
        return -1
    return 0


def temperature(lab):
    """按有彩色像素的色相与色度加权，看暖色和冷色谁占上风。"""
    a, b = lab[..., 1].ravel(), lab[..., 2].ravel()
    chroma = np.hypot(a, b)
    colored = chroma >= CHROMA_GRAY
    if colored.mean() < 0.05:
        return {'label': '中性', 'score': 0.0, 'note': '画面接近无彩色'}
    hue = np.degrees(np.arctan2(b[colored], a[colored])) % 360
    warmth = np.where((hue >= 345) | (hue < 105), 1.0, np.where((hue >= 160) & (hue < 320), -1.0, 0.0))
    weight = chroma[colored]
    score = float((warmth * weight).sum() / weight.sum())
    label = '暖调' if score > TEMPERATURE_SHIFT else '冷调' if score < -TEMPERATURE_SHIFT else '中性'
    return {'label': label, 'score': round(score, 2), 'note': ''}


def saturation(lab):
    """不算接近纯黑的像素：夜景里大片黑色会把霓虹、火光这些鲜艳颜色的饱和度"平均"掉。"""
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    lit = lab[..., 0] > DARK_LIGHTNESS
    mean = float(chroma[lit].mean() if lit.mean() >= 0.05 else chroma.mean())
    label = (
        '接近黑白'
        if mean < 4
        else '低饱和'
        if mean < SATURATION_LOW
        else '高饱和'
        if mean > SATURATION_HIGH
        else '中等饱和'
    )
    return {'label': label, 'value': round(mean, 1)}


def _hue_gap(a, b):
    gap = abs(a - b) % 360
    return min(gap, 360 - gap)


def accent(lab, colors):
    """点缀色：面积小、但鲜艳且与主色调反差大的颜色（比如暖棕房间里的绿色牌桌）。主色卡里已有的不算。"""
    a, b = lab[..., 1].ravel(), lab[..., 2].ravel()
    chroma = np.hypot(a, b)
    vivid = chroma >= ACCENT_CHROMA
    if vivid.mean() < ACCENT_SHARE:
        return None
    hue = np.degrees(np.arctan2(b[vivid], a[vivid])) % 360
    bins = np.bincount((hue // 30).astype(int) % 12, weights=chroma[vivid], minlength=12)
    main = (int(np.argmax(bins)) + 0.5) * 30
    counts = np.bincount((hue // 30).astype(int) % 12, minlength=12) / chroma.size
    for index in np.argsort(-counts):
        centre = (index + 0.5) * 30
        if counts[index] < ACCENT_SHARE:
            break
        if _hue_gap(centre, main) < ACCENT_GAP:
            continue
        if any(c['chroma'] >= 15 and _hue_gap(c['hue'], centre) < 30 and c['share'] >= 0.08 for c in colors):
            continue  # 已经是主色之一
        picked = (hue // 30).astype(int) % 12 == index
        pixels = lab.reshape(-1, 3)[vivid][picked]
        center = pixels.mean(axis=0)
        lightness, ca, cb = (float(v) for v in center)
        spot_hue = math.degrees(math.atan2(cb, ca)) % 360
        return {
            'name': _hue_name(spot_hue, lightness, math.hypot(ca, cb)),
            'hex': _hex(center),
            'share': round(float(counts[index]), 3),
            'hue': round(spot_hue, 1),
        }
    return None


def harmony(colors, spot=None, warmth=''):
    """主色之间的色相关系。暗场景里有彩色只占一小块，所以占比按"有彩色部分"来算，不按整幅画面。
    主色只有一种冷暖倾向、点缀色又正好相反时，算冷暖对比（如青色背景里的暖色皮肤）。"""
    colored = [c for c in colors if c['chroma'] >= 12]
    total = sum(c['share'] for c in colored)
    vivid = [c for c in colored if c['chroma'] >= 15 and c['share'] >= 0.02 and c['share'] >= 0.2 * total]
    contrast = None
    if spot and warmth in ('暖调', '冷调'):
        spot_warmth = _warmth(spot['hue'])
        if spot_warmth and spot_warmth != (1 if warmth == '暖调' else -1):
            contrast = {'label': '冷暖对比', 'note': f'{warmth[0]}色为主，{spot["name"]}点缀'}
    if not vivid:
        return contrast or {'label': '低饱和 / 无明显主色', 'note': ''}
    same = len({_warmth(c['hue']) for c in vivid}) == 1
    if contrast and same:
        return contrast  # 主色都是同一冷暖，靠点缀色形成对比，这才是画面的配色重点
    if len(vivid) == 1:
        return {'label': '单色主导', 'note': f'以{vivid[0]["name"]}为主'}
    pairs = [(x, y) for i, x in enumerate(vivid) for y in vivid[i + 1 :]]
    widest = max(pairs, key=lambda p: _hue_gap(p[0]['hue'], p[1]['hue']))
    gap = _hue_gap(widest[0]['hue'], widest[1]['hue'])
    if gap >= 140:
        names = {_warmth(widest[0]['hue']), _warmth(widest[1]['hue'])}
        teal_orange = names == {1, -1} and any(40 <= c['hue'] < 105 for c in widest)
        label = '青橙对比' if teal_orange else '互补色对比'
        return {'label': label, 'note': f'{widest[0]["name"]}与{widest[1]["name"]}'}
    if gap <= 35:
        return {'label': '单色系', 'note': '主色色相相近'}
    if gap <= 75:
        return {'label': '邻近色', 'note': '主色在色环上相邻'}
    return {'label': '多色', 'note': '主色分布较散'}


# ---------------------------------------------------------------- 影调与明暗


def tone(lightness):
    """lightness：0～1 的亮度图。"""
    values = lightness.ravel()
    mean = float(values.mean())
    low, high = (float(v) for v in np.percentile(values, [5, 95]))
    spread = high - low
    histogram = np.histogram(values, bins=32, range=(0.0, 1.0))[0] / len(values)
    key = '低调' if mean < KEY_LOW else '高调' if mean > KEY_HIGH else '中间调'
    contrast = '高对比' if spread > CONTRAST_HIGH else '低对比' if spread < CONTRAST_LOW else '中等对比'
    note = ''
    # 暗部为主、只有灯具或窗户很亮：面积小，拉不开分位差，但摄影上属于"低调高反差"
    glare = float((values > GLARE_LEVEL).mean())
    if key == '低调' and contrast != '高对比' and glare >= GLARE_SHARE:
        contrast = '高对比'
        note = '暗部为主，有小面积强光（如灯具、窗户）'
    return {
        'key': key,
        'contrast': contrast,
        'note': note,
        'brightness': round(mean, 3),
        'spread': round(spread, 3),
        'highlights': round(float((values > 0.98).mean()), 3),
        'shadows': round(float((values < 0.02).mean()), 3),
        'histogram': [round(float(v), 4) for v in histogram],
    }


def light_layout(lightness):
    """3×3 分区平均亮度，以及最明显的明暗分布。只描述亮暗位置，不推断光源。"""
    h, w = lightness.shape
    grid = [
        [
            round(float(lightness[h * r // 3 : h * (r + 1) // 3, w * c // 3 : w * (c + 1) // 3].mean()), 3)
            for c in range(3)
        ]
        for r in range(3)
    ]
    left = np.mean([row[0] for row in grid])
    right = np.mean([row[2] for row in grid])
    top, bottom = np.mean(grid[0]), np.mean(grid[2])
    center = grid[1][1]
    edge = np.mean([grid[r][c] for r in range(3) for c in range(3) if (r, c) != (1, 1)])
    candidates = [
        (left - right, '左亮右暗'),
        (right - left, '右亮左暗'),
        (top - bottom, '上亮下暗'),
        (bottom - top, '下亮上暗'),
        (center - edge, '中心亮、四周暗'),
        (edge - center, '四周亮、中心暗'),
    ]
    shift, label = max(candidates)
    return {'label': label if shift >= LIGHT_SHIFT else '明暗均匀', 'shift': round(float(shift), 3), 'grid': grid}


# ---------------------------------------------------------------- 汇总


def tags(result):
    """卡片上显示的精简标签，最多 4 个，按拍法优先：景别、构图，再是画幅、冷暖、影调。"""
    frame = result['frame']
    aspect = ''
    if frame['letterbox'] or frame['aspect'] not in ('16:9', '9:16'):
        aspect = ('宽银幕 ' if frame['content_ratio'] >= 2.2 else '') + frame['aspect']
    elif frame['orientation'] == '竖屏':
        aspect = '竖屏'
    tone = result['tone']
    candidates = [
        result['shot']['label'],
        result['composition']['label'],
        aspect,
        result['color']['temperature']['label'],
        tone['key'],
        tone['contrast'] if tone['contrast'] != '中等对比' else '',
    ]
    return [text for text in candidates if text][:4]


def analyze(bgr):
    """一张 8 位 BGR 图的完整分析。先去掉黑边，其余各项只看画面内容。"""
    if bgr is None or bgr.ndim != 3 or bgr.shape[0] < 8 or bgr.shape[1] < 8:
        raise ValueError('图片无效')
    height, width = bgr.shape[:2]
    scale = min(1.0, WORK_SIZE / max(height, width))
    small = cv2.resize(bgr, (max(8, round(width * scale)), max(8, round(height * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    bars = find_bars(gray)
    h, w = gray.shape
    content = small[bars['top'] : h - bars['bottom'], bars['left'] : w - bars['right']]
    ratio = width / height
    content_ratio = content.shape[1] / content.shape[0] * (small.shape[0] / height) / (small.shape[1] / width)
    lab = _lab(content)
    lightness = lab[..., 0] / 100.0
    colors = palette(content)
    spot = accent(lab, colors)
    warmth = temperature(lab)
    # 人脸检测用更清楚的图：远处的小人脸在分析图上只有几个像素
    top, bottom = round(bars['top'] / scale), round(bars['bottom'] / scale)
    left, right = round(bars['left'] / scale), round(bars['right'] / scale)
    detail = bgr[top : height - bottom, left : width - right]
    detail_scale = min(1.0, DETAIL_SIZE / max(detail.shape[:2]))
    if detail_scale < 1:
        size = (max(8, round(detail.shape[1] * detail_scale)), max(8, round(detail.shape[0] * detail_scale)))
        detail = cv2.resize(detail, size, interpolation=cv2.INTER_AREA)
    result = {
        'version': ANALYSIS_VERSION,
        'size': [width, height],
        'frame': {
            'aspect': aspect_name(content_ratio),
            'ratio': round(ratio, 3),
            'content_ratio': round(content_ratio, 3),
            'orientation': orientation(content_ratio),
            'letterbox': any(bars.values()),
            'bars': {side: round(value / scale) for side, value in bars.items()},
            # 画面内容在整张图里的位置（0～1），前端据此把辅助线画在黑边以内
            'content': [
                round(bars['left'] / w, 4),
                round(bars['top'] / h, 4),
                round(content.shape[1] / w, 4),
                round(content.shape[0] / h, 4),
            ],
        },
        'color': {
            'palette': colors,
            'temperature': warmth,
            'saturation': saturation(lab),
            'harmony': harmony(colors, spot, warmth['label']),
            'accent': spot,
        },
        'tone': tone(lightness),
        'light': light_layout(lightness),
        **shot_analysis.analyze(content, detail),
    }
    result['tags'] = tags(result)
    return result

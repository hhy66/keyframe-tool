"""镜头语言分析：人物检测、景别、构图、水平与景深。纯计算，不涉及 HTTP 和工作区。

人脸用 YuNet，人体用 NanoDet（均来自 OpenCV 官方模型库，随工具附带在 models/，本机运行）。
没检测到人时不猜景别；构图改用视觉显著性找画面主体，并标明依据，便于用户判断可信程度。
阈值集中在本文件顶部，便于用真实镜头校准。所有坐标都是相对画面内容（去掉黑边后）的 0～1 比例。
"""

from __future__ import annotations

import math
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

MODELS = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'models'
FACE_MODEL = MODELS / 'face_detection_yunet_2023mar.onnx'
PERSON_MODEL = MODELS / 'object_detection_nanodet_2022nov.onnx'
FACE_SCORE = 0.75
PERSON_SCORE = 0.5

# 景别：主体人脸框高度占画面高度的比例（人脸框约为额头到下巴）。
# 国内常用分法：远景（人很小、环境为主）/ 全景（全身）/ 中景（膝盖以上）/ 近景（胸部以上）/ 特写（肩部以上）/ 大特写（脸的局部）。
SHOT_BY_FACE = [(0.72, '大特写'), (0.40, '特写'), (0.19, '近景'), (0.11, '中景'), (0.05, '全景'), (0.0, '远景')]
PERSON_FULL = 0.35  # 没有正脸时：完整的人体框高于此比例算全景，否则远景

# 构图：主体中心离三分线 / 中线多近才算落在线上
THIRD_BAND = 0.08
CENTER_BAND = 0.07
SYMMETRY_MIN = 0.9  # 左右镜像相似度
SPACE_SUBJECT = 0.12  # 主体面积小于此比例且偏在一侧时，提示留白

# 水平：参与判断的直线总长至少为画面宽度的比例，以及倾斜档位（度）
LEVEL_MIN_LENGTH = 0.6
LEVEL_OK = 1.5
LEVEL_DUTCH = 5.0

# 景深：主体区域与背景区域清晰度之比
DEPTH_SHALLOW = 2.5
DEPTH_DEEP = 1.5
BLURRY = 4.0  # 最清晰的几块（拉普拉斯均值的 98% 分位）也低于此值时，算整体偏糊

_local = threading.local()


# ---------------------------------------------------------------- 人物检测


def _face_detector(width, height):
    detector = getattr(_local, 'face', None)
    if detector is None:
        detector = cv2.FaceDetectorYN.create(str(FACE_MODEL), '', (width, height), FACE_SCORE, 0.3, 50)
        _local.face = detector
    detector.setInputSize((width, height))
    return detector


def find_faces(bgr):
    """人脸框（按面积从大到小）。`height` 是未裁切的人脸框高度占比，脸超出画面时可以大于 1。"""
    h, w = bgr.shape[:2]
    _, faces = _face_detector(w, h).detect(bgr)
    found = []
    for face in faces if faces is not None else []:
        x, y, fw, fh = (float(v) for v in face[:4])
        x0, y0, x1, y1 = max(0.0, x), max(0.0, y), min(w, x + fw), min(h, y + fh)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        found.append(
            {
                'box': [round(x0 / w, 4), round(y0 / h, 4), round((x1 - x0) / w, 4), round((y1 - y0) / h, 4)],
                'height': round(fh / h, 3),
                'score': round(float(face[-1]), 2),
            }
        )
    found.sort(key=lambda f: -f['box'][2] * f['box'][3])
    return found


PERSON_SIZE = 416
PERSON_MEAN = np.array([103.53, 116.28, 123.675], np.float32)
PERSON_STD = np.array([57.375, 57.12, 58.395], np.float32)


def _person_net():
    net = getattr(_local, 'person', None)
    if net is None:
        net = cv2.dnn.readNet(str(PERSON_MODEL))
        _local.person = net
    return net


def find_people(bgr):
    """人体框（NanoDet 的 person 类，按面积从大到小）。背影、侧身和远处的人也能找到。"""
    h, w = bgr.shape[:2]
    size = PERSON_SIZE
    scale = size / max(h, w)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    top, left = (size - nh) // 2, (size - nw) // 2
    canvas = np.zeros((size, size, 3), np.float32)
    resized = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA).astype(np.float32)
    canvas[top : top + nh, left : left + nw] = (resized - PERSON_MEAN) / PERSON_STD
    net = _person_net()
    net.setInput(cv2.dnn.blobFromImage(canvas))
    outputs = [o.reshape(o.shape[-2], o.shape[-1]) for o in net.forward(net.getUnconnectedOutLayersNames())]
    # 输出按形状配对，不依赖顺序（不同 OpenCV 版本的输出顺序不同）：80 列是类别分数，32 列是边框分布。
    scores_by_level = {o.shape[0]: o for o in outputs if o.shape[1] == 80}
    boxes_by_level = {o.shape[0]: o for o in outputs if o.shape[1] == 32}
    boxes, scores = [], []
    for cells, classes in scores_by_level.items():
        side = int(round(math.sqrt(cells)))
        stride = size / side
        person = classes[:, 0]
        keep = person >= PERSON_SCORE
        if not keep.any() or cells not in boxes_by_level:
            continue
        spread = boxes_by_level[cells][keep].reshape(-1, 4, 8)
        spread = np.exp(spread - spread.max(axis=2, keepdims=True))
        spread /= spread.sum(axis=2, keepdims=True)
        distance = (spread * np.arange(8)).sum(axis=2) * stride
        index = np.nonzero(keep)[0]
        cx = (index % side) * stride + 0.5 * (stride - 1)
        cy = (index // side) * stride + 0.5 * (stride - 1)
        for (d0, d1, d2, d3), x, y, score in zip(distance, cx, cy, person[keep]):
            boxes.append([float(x - d0), float(y - d1), float(d0 + d2), float(d1 + d3)])
            scores.append(float(score))
    found = []
    for i in cv2.dnn.NMSBoxes(boxes, scores, PERSON_SCORE, 0.5) if boxes else []:
        i = int(np.asarray(i).ravel()[0])
        x, y, bw, bh = boxes[i]
        x0, y0 = max(0.0, (x - left) / scale), max(0.0, (y - top) / scale)
        x1, y1 = min(w, (x + bw - left) / scale), min(h, (y + bh - top) / scale)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        found.append(
            {
                'box': [round(x0 / w, 4), round(y0 / h, 4), round((x1 - x0) / w, 4), round((y1 - y0) / h, 4)],
                'score': round(scores[i], 2),
            }
        )
    found.sort(key=lambda p: -p['box'][2] * p['box'][3])
    return found


def detect(bgr):
    """人脸与人体；模型文件缺失或无法加载时返回 None，其余分析照常进行。"""
    try:
        faces = find_faces(bgr) if FACE_MODEL.is_file() else None
        people = find_people(bgr) if PERSON_MODEL.is_file() else None
    except cv2.error:
        return None
    if faces is None and people is None:
        return None
    return {'faces': faces or [], 'people': people or []}


# ---------------------------------------------------------------- 景别


def shot_from_face(height):
    return next(label for limit, label in SHOT_BY_FACE if height >= limit)


def shot_size(found):
    """按最大的人脸判断景别；没有正脸时按人体框粗判；都没有时不猜。"""
    if found is None:
        return {'label': '', 'basis': '人物检测模型不可用', 'confidence': '', 'people': 0}
    faces, people = found['faces'], found['people']
    count = max(len(faces), len(people))
    if faces:
        height = faces[0]['height']
        return {
            'label': shot_from_face(height),
            'basis': f'主体人脸高度约占画面 {round(height * 100)}%',
            'confidence': '较准',
            'people': count,
        }
    if people:
        x, y, w, h = people[0]['box']
        if y + h < 0.97:  # 脚在画面内：完整的人
            label = '全景' if h >= PERSON_FULL else '远景'
            basis = f'未见正脸，人物全身约占画面高度 {round(h * 100)}%'
        else:
            label = '中景'
            basis = '未见正脸，人物下半身被画面裁掉（背影或侧身时只能粗判）'
        return {'label': label, 'basis': basis, 'confidence': '一般', 'people': count}
    return {'label': '', 'basis': '未检测到人物（可能是空镜，或人物太小、被遮挡）', 'confidence': '', 'people': 0}


# ---------------------------------------------------------------- 画面主体与构图


def saliency(bgr):
    """颜色对比显著性：与画面四周（多为背景）的平均颜色差得越多越"跳"，0～1，64×64。
    以四周为参照而不是整幅平均，大块的主体（如白底上的黑马）也能被找出来。"""
    small = cv2.resize(bgr, (64, 64), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(cv2.GaussianBlur(small, (5, 5), 0).astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    border = np.concatenate(
        [lab[:6].reshape(-1, 3), lab[-6:].reshape(-1, 3), lab[6:-6, :6].reshape(-1, 3), lab[6:-6, -6:].reshape(-1, 3)]
    )
    result = np.linalg.norm(lab - np.median(border, axis=0), axis=2)
    peak = result.max()
    return result / peak if peak > 8.0 else np.zeros_like(result)  # 与四周色差不到 8 个单位：没有突出的东西


def salient_subject(bgr):
    """显著性最高的一块区域的外框与中心。"""
    heat = saliency(bgr)
    mask = (heat >= max(0.5, float(heat.mean()) * 2)).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    if count <= 1:
        return None
    # 取总显著性最大的一块
    best = max(range(1, count), key=lambda k: float(heat[labels == k].sum()))
    x, y, w, h, _ = stats[best]
    ys, xs = np.nonzero(labels == best)
    weights = heat[ys, xs]
    # 占了大半个画面的是背景而不是主体；主体也要明显比其余部分"跳"
    if stats[best][4] > 0.45 * 64 * 64 or stats[best][4] < 0.004 * 64 * 64:
        return None
    # 主体要比其余部分明显"跳"：很突出的小主体可以，略突出的必须够大；纹理里零碎的亮点不算
    rest = heat[labels != best]
    standout = (float(weights.mean()) - float(rest.mean())) / (float(rest.std()) + 1e-6) if rest.size else 0.0
    if not (standout >= 4 or (standout >= 2.2 and stats[best][4] >= 60)):
        return None
    return {
        'box': [round(float(v) / 64, 4) for v in (x, y, w, h)],
        'point': [
            round(float((xs * weights).sum() / weights.sum() + 0.5) / 64, 4),
            round(float((ys * weights).sum() / weights.sum() + 0.5) / 64, 4),
        ],
    }


def subject(found, bgr):
    """构图看的主体：优先人脸，其次人体，最后显著区域。"""
    if found and found['faces']:
        x, y, w, h = found['faces'][0]['box']
        return {'source': '人脸', 'box': [x, y, w, h], 'point': [x + w / 2, y + h * 0.45]}
    if found and found['people']:
        x, y, w, h = found['people'][0]['box']
        return {'source': '人物', 'box': [x, y, w, h], 'point': [x + w / 2, y + min(h, w * 1.2) * 0.3]}
    salient = salient_subject(bgr)
    if salient:
        return {'source': '显著区域', **salient}
    return None


def _place(value, first, second, low, high):
    if abs(value - 0.5) <= CENTER_BAND:
        return '居中'
    if abs(value - 1 / 3) <= THIRD_BAND:
        return first
    if abs(value - 2 / 3) <= THIRD_BAND:
        return second
    return low if value < 0.5 else high


def symmetry(gray):
    """左右镜像相似度 0～1：画面与自己的镜像越像越接近 1。"""
    small = cv2.GaussianBlur(cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA), (5, 5), 0).astype(np.float32)
    difference = float(np.abs(small - small[:, ::-1]).mean())
    return round(max(0.0, 1.0 - difference / 64.0), 3)


def composition(main, gray):
    score = symmetry(gray)
    if main is None:
        structured = float(gray.std()) >= 12  # 一片纯色谈不上对称
        return {
            'label': '对称构图' if score >= SYMMETRY_MIN and structured else '',
            'position': '',
            'space': '',
            'symmetry': score,
            'basis': '画面没有明显主体',
        }
    x, y = main['point']
    across = _place(x, '左三分线', '右三分线', '偏左', '偏右')
    down = _place(y, '上三分线', '下三分线', '偏上', '偏下')
    if across == '居中' and score >= SYMMETRY_MIN:
        label = '对称构图'
    elif across == '居中':
        label = '中心构图'
    elif '三分线' in across:
        label = '三分法构图'
    else:
        label = '偏侧构图'
    bw, bh = main['box'][2], main['box'][3]
    space = ''
    if bw * bh < SPACE_SUBJECT and across != '居中':
        space = '右侧留白' if x < 0.5 else '左侧留白'
    return {
        'label': label,
        'position': f'水平{across} · 垂直{down}',
        'space': space,
        'symmetry': score,
        'basis': f'按{main["source"]}位置判断',
    }


# ---------------------------------------------------------------- 水平与景深


def level(gray):
    """画面里接近水平的长直线的倾斜角。直线太少时不下结论。"""
    h, w = gray.shape
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 60, 160)
    edges[:3], edges[-3:], edges[:, :3], edges[:, -3:] = 0, 0, 0, 0  # 裁掉黑边后的边缘不算
    lines = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=60, minLineLength=w * 0.2, maxLineGap=8)
    angles, lengths, best = [], [], None
    for x0, y0, x1, y1 in lines.reshape(-1, 4) if lines is not None else []:
        angle = math.degrees(math.atan2(y1 - y0, x1 - x0))
        angle = (angle + 90) % 180 - 90  # -90～90，向右下为正
        if abs(angle) > 20:
            continue
        length = math.hypot(x1 - x0, y1 - y0)
        angles.append(angle)
        lengths.append(length)
        if best is None or length > best[0]:
            best = (length, [x0 / w, y0 / h, x1 / w, y1 / h])
    if sum(lengths) < w * LEVEL_MIN_LENGTH:
        return {'label': '无法判断', 'angle': None, 'line': None, 'basis': '画面里缺少明显的水平线条'}
    order = np.argsort(angles)
    cumulative = np.cumsum(np.array(lengths)[order])
    angle = float(np.array(angles)[order][np.searchsorted(cumulative, cumulative[-1] / 2)])  # 按长度加权的中位数
    tilt = abs(angle)
    side = '右低左高' if angle > 0 else '左低右高'
    if tilt < LEVEL_OK:
        label = '水平'
    elif tilt < LEVEL_DUTCH:
        label = f'轻微倾斜 {tilt:.1f}°（{side}）'
    else:
        label = f'明显倾斜 {tilt:.1f}°（{side}，可能是荷兰角）'
    return {
        'label': label,
        'angle': round(angle, 1),
        'line': [round(v, 4) for v in best[1]],
        'basis': '按画面里的水平线条判断',
    }


def depth_of_field(gray, main):
    """比较主体区域和背景的清晰度。背景本身平整（天空、白墙）时也会显得"柔和"，所以说明里会提醒。"""
    sharp = np.abs(cv2.Laplacian(gray.astype(np.float32), cv2.CV_32F, ksize=3))
    h, w = gray.shape
    rows, cols = 9, 16
    blocks = np.array(
        [
            [
                float(sharp[h * r // rows : h * (r + 1) // rows, w * c // cols : w * (c + 1) // cols].mean())
                for c in range(cols)
            ]
            for r in range(rows)
        ]
    )
    overall = float(np.percentile(blocks, 98))  # 最清晰的几块：大片天空、白墙不会被当成偏糊
    if overall < BLURRY:
        return {'label': '整体偏糊', 'ratio': None, 'basis': '全画面都缺少清晰细节（可能是运动模糊、失焦或刻意的柔焦）'}
    if main is None:
        return {'label': '无法判断', 'ratio': None, 'basis': '画面没有明显主体'}
    x, y, bw, bh = main['box']
    # 人脸框只包住脸：向四周扩展到头肩，避免把头发、肩膀算成背景
    grow = 0.6 if main['source'] == '人脸' else 0.1
    x0, x1 = x - bw * grow, x + bw * (1 + grow)
    y0, y1 = y - bh * grow, y + bh * (1 + grow * 2)
    inside = np.zeros_like(blocks, bool)
    for r in range(rows):
        for c in range(cols):
            cx, cy = (c + 0.5) / cols, (r + 0.5) / rows
            inside[r, c] = x0 <= cx <= x1 and y0 <= cy <= y1
    if inside.sum() < 1 or (~inside).sum() < 12:
        return {'label': '无法判断', 'ratio': None, 'basis': '主体占满画面，看不到背景'}
    ratio = float(np.percentile(blocks[inside], 75) / max(np.median(blocks[~inside]), 0.5))
    if ratio >= DEPTH_SHALLOW:
        label, basis = '浅景深', '主体清晰、背景明显柔和：多为背景虚化（背景本身平整时也会这样）'
    elif ratio <= DEPTH_DEEP:
        label, basis = '深景深', '主体和背景一样清晰'
    else:
        label, basis = '中等景深', '背景略柔和'
    return {'label': label, 'ratio': round(ratio, 2), 'basis': basis}


def analyze(content):
    """一张已去掉黑边、长边不超过分析尺寸的 BGR 图。"""
    gray = cv2.cvtColor(content, cv2.COLOR_BGR2GRAY)
    found = detect(content)
    main = subject(found, content)
    return {
        'people': found,
        'shot': shot_size(found),
        'subject': main,
        'composition': composition(main, gray),
        'level': level(gray),
        'depth': depth_of_field(gray, main),
    }

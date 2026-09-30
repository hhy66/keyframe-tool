"""纠错记录：用户觉得分析不对时，在大图里选出正确的类别，或者自己写一段描述。

每条记录连同当时的测量值（画面分析、运镜、镜头起止）一起保存在工作目录的 corrections.json，
按截图文件对应，重新分析不会丢。导出的文件只有数值和文字，不含任何图片，用来校准各项阈值。
"""

from __future__ import annotations

import json
from pathlib import Path

FILE = 'corrections.json'
VERSION = 1
CHOICES = ('shot', 'composition', 'depth', 'cut', 'steady', 'speed')  # 单选的项目
MOVES = 'moves'  # 运镜可以多选
NOTE_MAX = 2000
VALUE_MAX = 40
MOVES_MAX = 12


def clean(fix):
    """只留下认识的项目和合理长度的文字；什么都没填时返回 None（表示删除这条纠错）。"""
    if not isinstance(fix, dict):
        raise ValueError('纠错内容无效')
    found = {}
    for key in CHOICES:
        value = fix.get(key)
        if isinstance(value, str) and value.strip():
            found[key] = value.strip()[:VALUE_MAX]
    moves = fix.get(MOVES)
    if isinstance(moves, list):
        picked = []
        for value in moves:
            if isinstance(value, str) and value.strip() and value.strip()[:VALUE_MAX] not in picked:
                picked.append(value.strip()[:VALUE_MAX])
        if picked:
            found[MOVES] = picked[:MOVES_MAX]
    note = fix.get('note')
    if isinstance(note, str) and note.strip():
        found['note'] = note.strip()[:NOTE_MAX]
    return found or None


def load(path: Path):
    """{截图文件: 记录}；文件不存在或损坏时为空。"""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    items = data.get('items') if isinstance(data, dict) else None
    return {key: value for key, value in items.items() if isinstance(value, dict)} if isinstance(items, dict) else {}


def document(items):
    return {'version': VERSION, 'items': items}


def export(items, video_name, analysis=None, moves=None, versions=None):
    """导出给校准用：按视频里的时间排好，没有测量值的记录用现在已算好的补上。"""
    analysis, moves = analysis or {}, moves or {}
    records = []
    for key, record in items.items():
        record = {**record, 'key': key}
        if record.get('analysis') is None and key in analysis:
            record['analysis'] = analysis[key]
        span = record.get('shot_span')
        if record.get('motion') is None and span:
            record['motion'] = moves.get(f'{span["start_frame"]}-{span["end_frame"]}')
        records.append(record)
    records.sort(key=lambda r: (r.get('time') is None, r.get('time') or 0))
    return {
        'kind': 'keyframe-tool corrections',
        'version': VERSION,
        'video': video_name,
        'versions': versions or {},
        'count': len(records),
        'note': '只含测量数值和你的纠错，不含任何图片。',
        'records': records,
    }

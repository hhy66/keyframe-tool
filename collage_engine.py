"""Pixel-exact source grids, justified reference boards and bounded, serial Pillow exports."""

import csv
import io
import math
import os
import re
import sys
from pathlib import Path
from statistics import median

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import justified_layout

MAX_PIXELS = 40_000_000
MAX_MEMORY = 512 * 1024 * 1024
MAX_BATCH = 200
TOKEN = re.compile(r'^[0-9a-f]{32}$')
TEMP_NAME = re.compile(r'^collage-[0-9a-f]{32}(?:-preview\.jpg|\.png|\.jpg|\.zip|\.json)$')
BACKGROUNDS = {'white': (255, 255, 255), 'light': (241, 243, 246), 'dark': (17, 24, 39), 'black': (0, 0, 0)}
# Fractions of the whole collage width, so a style looks the same at 1500 or 6000 pixels.
GAPS = {'none': 0, 'small': 0.004, 'medium': 0.01, 'large': 0.02}
RADII = {'none': 0, 'small': 0.004, 'medium': 0.008, 'large': 0.016}
CANVAS = {'landscape': 16 / 9, 'square': 1, 'portrait': 3 / 4}
CELL = {'landscape': 16 / 9, 'square': 1, 'portrait': 9 / 16}
# Photos per page and the number of grid columns used for it.
PER_PAGE = {4: 2, 9: 3, 12: 4, 16: 4}
LABEL_POSITIONS = ('tl', 'tr', 'bl', 'br', 'below')
NOTE_LIMIT = 60
BASE = Path(__file__).resolve().parent
# In the portable build, fonts/ sits next to the executable rather than the unpacked code.
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else BASE
_font_path = None


def managed_file(name):
    return bool(TEMP_NAME.fullmatch(name))


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(name + '无效')
    return value


def choice(payload, key, default, allowed, message):
    value = payload.get(key, default)
    if value not in allowed:
        raise ValueError(message)
    return value


def font_path():
    """First CJK capable font: KEYFRAME_FONT, fonts/ next to the tool, then common system fonts."""
    global _font_path
    if _font_path is not None:
        return _font_path or None
    windows = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    candidates = [os.environ.get('KEYFRAME_FONT', '')]
    for folder in dict.fromkeys((APP_DIR / 'fonts', BASE / 'fonts')):
        candidates += sorted(str(p) for p in folder.glob('*') if p.suffix.lower() in ('.ttf', '.otf', '.ttc'))
    candidates += [str(windows / n) for n in ('msyh.ttc', 'msyhbd.ttc', 'simhei.ttf', 'Deng.ttf', 'simsun.ttc')]
    candidates += [
        '/System/Library/Fonts/PingFang.ttc',
        '/System/Library/Fonts/STHeiti Medium.ttc',
        '/System/Library/Fonts/Hiragino Sans GB.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            ImageFont.truetype(candidate, 12)
        except OSError:
            continue
        _font_path = candidate
        return candidate
    _font_path = ''
    return None


def font(size):
    path = font_path()
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def text_width(text, size):
    left, _, right, _ = font(size).getbbox(text) if text else (0, 0, 0, 0)
    return right - left


def ellipsize(text, size, max_width):
    """Trim text with an ellipsis so it fits max_width pixels."""
    if text_width(text, size) <= max_width:
        return text
    while text and text_width(text + '…', size) > max_width:
        text = text[:-1]
    return text + '…' if text else ''


def _crop_box(crop, sw, sh):
    """Normalised user crop to source pixels; None keeps the whole frame."""
    if crop is None:
        return 0.0, 0.0, float(sw), float(sh)
    return (
        crop['x'] * sw,
        crop['y'] * sh,
        min(crop['width'], 1 - crop['x']) * sw,
        min(crop['height'], 1 - crop['y']) * sh,
    )


def _cover(box, ratio):
    """Centre-trim a source box to the target ratio (for pixel rounding or cropped grid cells)."""
    sx, sy, rw, rh = box
    if rw / rh > ratio:
        new = rh * ratio
        sx += (rw - new) / 2
        rw = new
    else:
        new = rw / ratio
        sy += (rh - new) / 2
        rh = new
    return sx, sy, rw, rh


def per_page(payload):
    """Photos per page. Old clients only send grid (2 or 3), meaning grid x grid photos."""
    if 'per_page' in payload:
        value = payload['per_page']
        if type(value) is not int or value not in PER_PAGE:
            raise ValueError('每张张数无效')
        return value
    return integer(payload.get('grid', 3), 2, 3, '布局') ** 2


def _check_text(value, limit, message):
    if not isinstance(value, str) or len(value.strip()) > limit or any(ord(ch) < 32 for ch in value):
        raise ValueError(message)
    return value.strip()


def _check_crop(c):
    if not isinstance(c, dict) or set(c) != {'x', 'y', 'width', 'height'}:
        raise ValueError('裁剪参数无效')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in c.values()):
        raise ValueError('裁剪参数无效')
    if (
        c['x'] < 0
        or c['y'] < 0
        or c['width'] <= 0
        or c['height'] <= 0
        or c['x'] + c['width'] > 1.000001
        or c['y'] + c['height'] > 1.000001
    ):
        raise ValueError('裁剪区域超出原图')
    return c


DRAFT_LIMIT = 2000


def _draft_key(value):
    """Frames are keyed like the skip state: detected frame number, or a legacy file key."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError('拼图草稿无效')
    key = str(value)
    if not key or len(key) > 80 or (isinstance(value, int) and value < 0):
        raise ValueError('拼图草稿无效')
    return key


def clean_draft(draft):
    """Validated collage draft: photo order, crops and notes keyed by stable frame keys."""
    if not isinstance(draft, dict):
        raise ValueError('拼图草稿无效')
    ids, crops, notes = draft.get('ids', []), draft.get('crops', {}), draft.get('notes', {})
    if (
        not isinstance(ids, list)
        or not isinstance(crops, dict)
        or not isinstance(notes, dict)
        or max(len(ids), len(crops), len(notes)) > DRAFT_LIMIT
    ):
        raise ValueError('拼图草稿无效')
    keys = [_draft_key(k) for k in ids]
    if len(set(keys)) != len(keys):
        raise ValueError('拼图草稿无效')
    clean_notes = {}
    for key, text in notes.items():
        text = _check_text(text, NOTE_LIMIT, f'备注无效（每张最多 {NOTE_LIMIT} 个字）')
        if text:
            clean_notes[_draft_key(key)] = text
    return {'ids': keys, 'crops': {_draft_key(k): _check_crop(c) for k, c in crops.items()}, 'notes': clean_notes}


def validate_options(payload, ids):
    """Checks every option that does not depend on the photos themselves; ids are all selected ids."""
    capacity = per_page(payload)
    mode = choice(payload, 'mode', 'original', ('original', 'share', 'custom'), '拼图模式无效')
    choice(payload, 'layout', 'grid', ('grid', 'justified'), '拼图模式无效')
    choice(payload, 'shape', 'source', ('source',) + tuple(CELL), '拼图模式无效')
    choice(payload, 'canvas', 'auto', ('auto',) + tuple(CANVAS), '拼图模式无效')
    choice(payload, 'fit', 'contain', ('contain', 'cover'), '拼图模式无效')
    choice(payload, 'background', 'white', tuple(BACKGROUNDS), '底色无效')
    choice(payload, 'gap', 'none', tuple(GAPS), '间距无效')
    choice(payload, 'radius', 'none', tuple(RADII), '圆角无效')
    choice(payload, 'format', 'png', ('png', 'jpeg'), '图片格式无效')
    choice(payload, 'label_position', 'bl', LABEL_POSITIONS, '标注位置无效')
    for key in ('index', 'time', 'order', 'shadow'):
        if type(payload.get(key, False)) is not bool:
            raise ValueError('标注参数无效')
    _check_text(payload.get('title', ''), 40, '标题无效（最多 40 个字）')
    integer(payload.get('page', 1), 1, 100000, '页码')
    if 'width' in payload:
        integer(payload['width'], 64, 12000, '输出宽度')
    known = {str(i) for i in ids}
    crops = payload.get('crops', {})
    if not isinstance(crops, dict) or any(k not in known for k in crops):
        raise ValueError('裁剪参数无效')
    for c in crops.values():
        _check_crop(c)
    notes = payload.get('notes', {})
    if not isinstance(notes, dict) or any(k not in known for k in notes):
        raise ValueError('备注参数无效')
    for note in notes.values():
        _check_text(note, NOTE_LIMIT, f'备注无效（每张最多 {NOTE_LIMIT} 个字）')
    return capacity, mode


def page_payload(payload, page_ids, page):
    """The single page request for one page of a multi-page batch."""
    keep = {str(i) for i in page_ids}
    return {
        **payload,
        'ids': page_ids,
        'page': page,
        'crops': {k: v for k, v in payload.get('crops', {}).items() if k in keep},
        'notes': {k: v for k, v in payload.get('notes', {}).items() if k in keep},
    }


def _label(payload, source, position):
    labels = []
    if payload.get('order'):
        labels.append(f'{position:02d}')
    if payload.get('index'):
        labels.append(f"#{source['id'] + 1:03d}")
    if payload.get('time'):
        labels.append(source.get('label') or '未知')
    return '  '.join(labels)


def _caption_fit(text, size, tile_width):
    """Shrink a caption that would not fit a narrow (cropped) tile."""
    while size > 10 and text_width(text, size) + size * 1.9 > tile_width:
        size -= 1
    return size


def plan(payload, sources):
    capacity, mode = validate_options(payload, [s['id'] for s in sources])
    if not 1 <= len(sources) <= capacity:
        raise ValueError('所选图片超过布局容量')
    columns = PER_PAGE[capacity]
    crops = payload.get('crops', {})
    dimensions = []
    for s in sources:
        with Image.open(s['path']) as im:
            dimensions.append(im.size)
    w, h = dimensions[0]
    original = mode == 'original'
    if original and any(size != (w, h) for size in dimensions):
        raise ValueError('原尺寸无缝模式要求图片尺寸一致，请选择分享模式')
    layout = 'grid' if original else payload.get('layout', 'grid')
    shape = 'source' if original else payload.get('shape', 'source')
    fit = 'contain' if original or layout == 'justified' else payload.get('fit', 'contain')
    width = w * columns if original else integer(payload.get('width', 3000), 64, 12000, '输出宽度')
    gap = 0 if original else round(width * GAPS[payload.get('gap', 'none')])
    # Rounded corners only make sense with a gap: touching tiles would show notches of background.
    radius = 0 if original or not gap else round(width * RADII[payload.get('radius', 'none')])
    shadow = not original and payload.get('shadow', False)
    boxes = [
        (0.0, 0.0, float(sw), float(sh)) if original else _crop_box(crops.get(str(s['id'])), sw, sh)
        for s, (sw, sh) in zip(sources, dimensions)
    ]
    background = BACKGROUNDS[payload.get('background', 'white')]
    light = sum(background) > 384
    text_color = '#1f2937' if light else '#f3f4f6'
    muted_color = '#6b7280' if light else '#9ca3af'

    # Labels either sit on the photo or, with notes, in a text band below every photo.
    position = payload.get('label_position', 'bl')
    first_number = (payload.get('page', 1) - 1) * capacity + 1
    labels = ['' if original else _label(payload, s, first_number + n) for n, s in enumerate(sources)]
    notes = ['' if original else payload.get('notes', {}).get(str(s['id']), '').strip() for s in sources]
    band_size = max(12, round(width * 0.011))
    has_band = any(notes) or (position == 'below' and any(labels))
    band = round(band_size * 2.1) if has_band else 0

    title_text = '' if original else payload.get('title', '').strip()
    title = None
    top = gap
    if title_text:
        size = max(16, round(width * 0.024))
        pad = max(gap, round(size * 0.6))
        line = round(size * 1.6)
        title = dict(text=title_text, x=max(gap, round(size * 0.6)), y=pad + line // 2, size=size)
        top = pad + line + max(gap, round(size * 0.5))

    inner = width - 2 * gap
    cells = []
    if layout == 'justified':
        ratios = [rw / rh for _, _, rw, rh in boxes]
        target = CANVAS.get(payload.get('canvas', 'auto')) or median(sw / sh for sw, sh in dimensions)
        groups = justified_layout.best_rows(ratios, inner, gap, target, band)
        heights = justified_layout.row_heights(groups, ratios, inner, gap)
        y = top
        for group, row_height in zip(groups, heights):
            row = max(1, round(row_height))
            left = float(gap)
            for n, i in enumerate(group):
                x0 = round(left)
                left += ratios[i] * row_height
                x1 = width - gap if n == len(group) - 1 else round(left)
                left += gap
                cells.append((i, x0, y, max(1, x1 - x0), row))
            y += row + band + gap
        rows = len(groups)
        height = y  # the gap after the last row is the bottom margin
    else:
        rows = math.ceil(len(sources) / columns)
        if original:
            for n in range(len(sources)):
                cells.append((n, (n % columns) * w, (n // columns) * h, w, h))
            height = h * rows
        else:
            ratio = w / h if shape == 'source' else CELL[shape]
            cell_w = (inner - gap * (columns - 1)) / columns
            if cell_w < 8:
                raise ValueError('输出宽度太小，请增大宽度或减小间距')
            cell_h = max(1, round(cell_w / ratio))
            for n in range(len(sources)):
                col, row = n % columns, n // columns
                x0 = round(gap + col * (cell_w + gap))
                x1 = round(gap + col * (cell_w + gap) + cell_w)
                cells.append((n, x0, top + row * (cell_h + band + gap), x1 - x0, cell_h))
            height = top + rows * (cell_h + band) + (rows - 1) * gap + gap

    items = []
    for n, x, y0, cw, ch in cells:
        s, (sw, sh) = sources[n], dimensions[n]
        box = boxes[n]
        if original:
            crop, target = box, (x, y0, sw, sh)
        elif layout == 'justified' or fit == 'cover':
            crop, target = _cover(box, cw / ch), (x, y0, cw, ch)
        else:
            scale = min(cw / box[2], ch / box[3])
            tw, th = max(1, round(box[2] * scale)), max(1, round(box[3] * scale))
            crop, target = box, (x + (cw - tw) // 2, y0 + (ch - th) // 2, tw, th)
        tx, ty, tw, th = target
        below = None
        if band:
            text_label = labels[n] if position == 'below' else ''
            room = cw - round(band_size * 0.2)
            text_label = ellipsize(text_label, band_size, room)
            used = text_width(text_label, band_size) + (round(band_size * 0.8) if text_label else 0)
            below = dict(
                x=x,
                y=y0 + ch,
                width=cw,
                height=band,
                size=band_size,
                label=text_label,
                note=ellipsize(notes[n], band_size, max(0, room - used)),
                note_x=x + used,
            )
        overlay = '' if position == 'below' else labels[n]
        items.append(
            {k: s[k] for k in ('id', 'src', 'thumb')}
            | dict(
                source_width=sw,
                source_height=sh,
                crop=dict(x=crop[0], y=crop[1], width=crop[2], height=crop[3]),
                target=dict(x=tx, y=ty, width=tw, height=th),
                cell=dict(x=x, y=y0, width=cw, height=ch),
                radius=min(radius, tw // 2, th // 2),
                caption=overlay,
                caption_size=0,
                caption_position=position,
                below=below,
            )
        )
    # One caption size for the whole board, from the typical tile rather than each tile.
    if any(i['caption'] for i in items):
        size = max(12, round(median(min(i['target']['width'], i['target']['height'] * 16 / 9) for i in items) * 0.04))
        for i in items:
            if i['caption']:
                i['caption_size'] = _caption_fit(i['caption'], size, i['target']['width'])

    memory = width * height * 8 + max(sw * sh for sw, sh in dimensions) * 12
    safe = width * height <= MAX_PIXELS and memory <= MAX_MEMORY
    enlarged = any(
        i['target']['width'] > i['crop']['width'] + 1 or i['target']['height'] > i['crop']['height'] + 1 for i in items
    )
    warning = '部分画面将被放大，放大不会增加原图细节。' if enlarged else ''
    if (title or any(i['caption'] or i['below'] for i in items)) and not font_path():
        warning += '未找到中文字体，中文文字可能显示为方框；可把字体文件放入 fonts 文件夹。'
    first = cells[0] if cells else (0, 0, 0, w, h)
    return dict(
        width=width,
        height=height,
        layout=layout,
        rows=rows,
        per_page=capacity,
        columns=columns,
        cell_width=first[3],
        image_height=first[4],
        caption_height=band,
        gap=gap,
        radius=radius,
        radius_disabled=not original and not gap and payload.get('radius', 'none') != 'none',
        shadow=shadow,
        shadow_blur=max(4, round(width * 0.006)) if shadow else 0,
        background='#%02x%02x%02x' % background,
        text_color=text_color,
        muted_color=muted_color,
        title=title,
        items=items,
        estimated_memory_bytes=memory,
        can_render=safe,
        warning=warning if safe else '图片过大，超过4000万像素或512MB内存预算，请降低输出宽度或减少每张张数',
    )


def _rounded_mask(size, radius):
    """Anti-aliased rounded rectangle: drawn at 4x and reduced."""
    w, h = size
    if radius <= 0:
        return None
    big = Image.new('L', (w * 4, h * 4), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w * 4 - 1, h * 4 - 1), radius * 4, fill=255)
    return big.resize(size, Image.Resampling.LANCZOS)


def _draw_shadows(canvas, plan):
    blur = plan['shadow_blur']
    alpha = Image.new('L', canvas.size, 0)
    draw = ImageDraw.Draw(alpha)
    offset = blur // 2
    for item in plan['items']:
        t = item['target']
        draw.rounded_rectangle(
            (t['x'], t['y'] + offset, t['x'] + t['width'] - 1, t['y'] + t['height'] - 1 + offset),
            item['radius'],
            fill=90,
        )
    alpha = alpha.filter(ImageFilter.GaussianBlur(blur))
    canvas.paste((0, 0, 0), mask=alpha)


def caption_box(item):
    """Where the on-photo label pill goes; shared with the browser preview through the plan."""
    size = item['caption_size']
    t = item['target']
    pad_x, pad_y = round(size * 0.5), round(size * 0.3)
    box_w = min(t['width'], text_width(item['caption'], size) + pad_x * 2)
    box_h = size + pad_y * 2
    margin = round(size * 0.45)
    right = item['caption_position'] in ('tr', 'br')
    bottom = item['caption_position'] in ('bl', 'br')
    x = t['x'] + t['width'] - margin - box_w if right else t['x'] + margin
    y = t['y'] + t['height'] - margin - box_h if bottom else t['y'] + margin
    return x, y, box_w, box_h, pad_x


def _draw_caption(canvas, item):
    size = item['caption_size']
    t = item['target']
    x, y, box_w, box_h, pad_x = caption_box(item)
    if box_w + round(size * 0.45) > t['width'] or box_h + round(size * 0.45) > t['height']:
        return
    region = canvas.crop((x, y, x + box_w, y + box_h)).convert('RGBA')
    layer = Image.new('RGBA', region.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, box_w - 1, box_h - 1), round(box_h / 3), fill=(15, 23, 42, 150))
    region = Image.alpha_composite(region, layer)
    ImageDraw.Draw(region).text(
        (pad_x, box_h / 2), item['caption'], font=font(size), fill=(255, 255, 255, 255), anchor='lm'
    )
    canvas.paste(region.convert('RGB'), (x, y))


def _draw_below(canvas, item, plan):
    b = item['below']
    draw = ImageDraw.Draw(canvas)
    face = font(b['size'])
    middle = b['y'] + b['height'] / 2
    if b['label']:
        draw.text((b['x'], middle), b['label'], font=face, fill=plan['muted_color'], anchor='lm')
    if b['note']:
        draw.text((b['note_x'], middle), b['note'], font=face, fill=plan['text_color'], anchor='lm')


def render(plan, sources, payload, path, preview):
    if not plan['can_render']:
        raise ValueError(plan['warning'])
    with Image.new('RGB', (plan['width'], plan['height']), plan['background']) as canvas:
        if plan['shadow']:
            _draw_shadows(canvas, plan)
        for item, s in zip(plan['items'], sources):
            with Image.open(s['path']) as image:
                rgb = image.convert('RGB')
                try:
                    t = item['target']
                    c = item['crop']
                    size = (t['width'], t['height'])
                    if payload.get('mode', 'original') == 'original':
                        canvas.paste(rgb, (t['x'], t['y']))
                    else:
                        box = (c['x'], c['y'], c['x'] + c['width'], c['y'] + c['height'])
                        with rgb.resize(size, Image.Resampling.LANCZOS, box=box) as fitted:
                            canvas.paste(fitted, (t['x'], t['y']), _rounded_mask(size, item['radius']))
                finally:
                    rgb.close()
            if item['caption']:
                _draw_caption(canvas, item)
            if item['below']:
                _draw_below(canvas, item, plan)
        if plan['title']:
            t = plan['title']
            ImageDraw.Draw(canvas).text(
                (t['x'], t['y']), t['text'], font=font(t['size']), fill=plan['text_color'], anchor='lm'
            )
        fmt = payload.get('format', 'png')
        options = {} if fmt == 'png' else {'quality': 95, 'subsampling': 0}
        canvas.save(path, format='PNG' if fmt == 'png' else 'JPEG', **options)
    if preview is None:
        return
    # Decode the actual exported file, including JPEG loss, so the preview matches it.
    with Image.open(path) as final:
        final.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
        final.convert('RGB').save(preview, format='JPEG', quality=90)


def _clock(seconds):
    minutes, rest = divmod(max(0.0, seconds), 60)
    hours, minutes = divmod(int(minutes), 60)
    return f'{hours:02d}:{minutes:02d}:{rest:06.3f}'


def storyboard_csv(payload, cuts, spans=None):
    """A spreadsheet-friendly shot list for the selected photos, in collage order (UTF-8 with BOM for Excel).
    `spans` (shots.spans) adds each shot's start, end and length."""
    ids = payload.get('ids', [])
    notes = payload.get('notes', {})
    crops = payload.get('crops', {})
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ['顺序', '原截图编号', '时间码', '帧编号', '镜头开始', '镜头结束', '镜头时长（秒）', '备注', '裁剪']
    )
    for n, i in enumerate(ids, 1):
        cut = cuts[i]
        crop = crops.get(str(i))
        crop_text = (
            ''
            if not crop or payload.get('mode') == 'original'
            else '左{:.0%} 上{:.0%} 宽{:.0%} 高{:.0%}'.format(crop['x'], crop['y'], crop['width'], crop['height'])
        )
        note = notes.get(str(i), '').strip()
        if note[:1] in ('=', '+', '-', '@'):
            note = "'" + note  # keep spreadsheets from treating a note as a formula
        span = spans[i] if spans else None
        timing = [_clock(span['start']), _clock(span['end']), f"{span['duration']:.2f}"] if span else ['', '', '']
        writer.writerow(
            [n, f'#{i + 1:03d}', cut.get('label', ''), cut.get('frame_index', ''), *timing, note, crop_text]
        )
    return '\ufeff' + out.getvalue()

"""Pixel-exact source grids, justified reference boards and bounded, serial Pillow exports."""
import math
import os
import re
from pathlib import Path
from statistics import median
from PIL import Image, ImageDraw, ImageFilter, ImageFont
import justified_layout

MAX_PIXELS = 40_000_000
MAX_MEMORY = 512 * 1024 * 1024
TOKEN = re.compile(r'^[0-9a-f]{32}$')
TEMP_NAME = re.compile(r'^collage-[0-9a-f]{32}(?:-preview\.jpg|\.png|\.jpg|\.json)$')
BACKGROUNDS = {'white': (255, 255, 255), 'light': (241, 243, 246), 'dark': (17, 24, 39), 'black': (0, 0, 0)}
# Fractions of the whole collage width, so a style looks the same at 1500 or 6000 pixels.
GAPS = {'none': 0, 'small': .004, 'medium': .01, 'large': .02}
RADII = {'none': 0, 'small': .004, 'medium': .008, 'large': .016}
CANVAS = {'landscape': 16 / 9, 'square': 1, 'portrait': 3 / 4}
CELL = {'landscape': 16 / 9, 'square': 1, 'portrait': 9 / 16}
BASE = Path(__file__).resolve().parent
_font_path = None


def managed_file(name):
    return bool(TEMP_NAME.fullmatch(name))


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high: raise ValueError(name+'无效')
    return value


def choice(payload, key, default, allowed, message):
    value = payload.get(key, default)
    if value not in allowed: raise ValueError(message)
    return value


def font_path():
    """First CJK capable font: KEYFRAME_FONT, fonts/ next to the tool, then common system fonts."""
    global _font_path
    if _font_path is not None: return _font_path or None
    windows = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    candidates = [os.environ.get('KEYFRAME_FONT', '')]
    candidates += sorted(str(p) for p in (BASE / 'fonts').glob('*') if p.suffix.lower() in ('.ttf', '.otf', '.ttc'))
    candidates += [str(windows / n) for n in ('msyh.ttc', 'msyhbd.ttc', 'simhei.ttf', 'Deng.ttf', 'simsun.ttc')]
    candidates += ['/System/Library/Fonts/PingFang.ttc', '/System/Library/Fonts/STHeiti Medium.ttc', '/System/Library/Fonts/Hiragino Sans GB.ttc',
                   '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
                   '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc', '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc']
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file(): continue
        try: ImageFont.truetype(candidate, 12)
        except OSError: continue
        _font_path = candidate
        return candidate
    _font_path = ''
    return None


def font(size):
    path = font_path()
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def _crop_box(crop, sw, sh):
    """Normalised user crop to source pixels; None keeps the whole frame."""
    if crop is None: return 0.0, 0.0, float(sw), float(sh)
    return crop['x']*sw, crop['y']*sh, min(crop['width'], 1-crop['x'])*sw, min(crop['height'], 1-crop['y'])*sh


def _cover(box, ratio):
    """Centre-trim a source box to the target ratio (for pixel rounding or cropped grid cells)."""
    sx, sy, rw, rh = box
    if rw/rh > ratio:
        new = rh*ratio; sx += (rw-new)/2; rw = new
    else:
        new = rw/ratio; sy += (rh-new)/2; rh = new
    return sx, sy, rw, rh


def _validate(payload, sources):
    grid = integer(payload.get('grid', 3), 2, 3, '布局')
    mode = choice(payload, 'mode', 'original', ('original', 'share', 'custom'), '拼图模式无效')
    choice(payload, 'layout', 'grid', ('grid', 'justified'), '拼图模式无效')
    choice(payload, 'shape', 'source', ('source',) + tuple(CELL), '拼图模式无效')
    choice(payload, 'canvas', 'auto', ('auto',) + tuple(CANVAS), '拼图模式无效')
    choice(payload, 'fit', 'contain', ('contain', 'cover'), '拼图模式无效')
    choice(payload, 'background', 'white', tuple(BACKGROUNDS), '底色无效')
    choice(payload, 'gap', 'none', tuple(GAPS), '间距无效')
    choice(payload, 'radius', 'none', tuple(RADII), '圆角无效')
    choice(payload, 'format', 'png', ('png', 'jpeg'), '图片格式无效')
    for key in ('index', 'time', 'shadow'):
        if type(payload.get(key, False)) is not bool: raise ValueError('标注参数无效')
    title = payload.get('title', '')
    if not isinstance(title, str) or len(title.strip()) > 40 or any(ord(ch) < 32 for ch in title): raise ValueError('标题无效（最多 40 个字）')
    integer(payload.get('page', 1), 1, 100000, '页码')
    if 'width' in payload: integer(payload['width'], 64, 12000, '输出宽度')
    if not 1 <= len(sources) <= grid*grid: raise ValueError('所选图片超过布局容量')
    crops = payload.get('crops', {})
    ids = {str(s['id']) for s in sources}
    if not isinstance(crops, dict) or any(k not in ids for k in crops): raise ValueError('裁剪参数无效')
    for c in crops.values():
        if not isinstance(c, dict) or set(c) != {'x', 'y', 'width', 'height'}: raise ValueError('裁剪参数无效')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in c.values()): raise ValueError('裁剪参数无效')
        if c['x'] < 0 or c['y'] < 0 or c['width'] <= 0 or c['height'] <= 0 or c['x']+c['width'] > 1.000001 or c['y']+c['height'] > 1.000001:
            raise ValueError('裁剪区域超出原图')
    return grid, mode, crops


def _caption(payload, source, enabled):
    labels = []
    if enabled and payload.get('index'): labels.append(f"#{source['id']+1:03d}")
    if enabled and payload.get('time'): labels.append(source.get('label') or '未知')
    return '  '.join(labels)


def _caption_fit(text, size, tile_width):
    """Shrink a caption that would not fit a narrow (cropped) tile."""
    while size > 10:
        left, _, right, _ = font(size).getbbox(text)
        if right-left+size*1.9 <= tile_width: break
        size -= 1
    return size


def plan(payload, sources):
    grid, mode, crops = _validate(payload, sources)
    dimensions = []
    for s in sources:
        with Image.open(s['path']) as im: dimensions.append(im.size)
    w, h = dimensions[0]
    original = mode == 'original'
    if original and any(size != (w, h) for size in dimensions): raise ValueError('原尺寸无缝模式要求图片尺寸一致，请选择分享模式')
    layout = 'grid' if original else payload.get('layout', 'grid')
    shape = 'source' if original else payload.get('shape', 'source')
    fit = 'contain' if original or layout == 'justified' else payload.get('fit', 'contain')
    width = w*grid if original else integer(payload.get('width', 3000), 64, 12000, '输出宽度')
    gap = 0 if original else round(width*GAPS[payload.get('gap', 'none')])
    radius = 0 if original else round(width*RADII[payload.get('radius', 'none')])
    shadow = not original and payload.get('shadow', False)
    boxes = [(0.0, 0.0, float(sw), float(sh)) if original else _crop_box(crops.get(str(s['id'])), sw, sh) for s, (sw, sh) in zip(sources, dimensions)]

    # Optional title band above the photos.
    title_text = '' if original else payload.get('title', '').strip()
    title = None
    top = gap
    if title_text:
        size = max(16, round(width*.024))
        pad = max(gap, round(size*.6))
        line = round(size*1.6)
        title = dict(text=title_text, x=max(gap, round(size*.6)), y=pad+line//2, size=size)
        top = pad+line+max(gap, round(size*.5))

    inner = width-2*gap
    cells = []
    rows = 0
    if layout == 'justified':
        ratios = [rw/rh for _, _, rw, rh in boxes]
        target = CANVAS.get(payload.get('canvas', 'auto')) or median(sw/sh for sw, sh in dimensions)
        groups = justified_layout.best_rows(ratios, inner, gap, target)
        heights = justified_layout.row_heights(groups, ratios, inner, gap)
        y = top
        for group, row_height in zip(groups, heights):
            height = max(1, round(row_height))
            left = float(gap)
            for n, i in enumerate(group):
                x0 = round(left)
                left += ratios[i]*row_height
                x1 = width-gap if n == len(group)-1 else round(left)
                left += gap
                cells.append((i, x0, y, max(1, x1-x0), height))
            y += height+gap
        rows = len(groups)
        height = y  # the gap after the last row is the bottom margin
    else:
        rows = math.ceil(len(sources)/grid)
        if original:
            cell_w, cell_h = w, h
            for n in range(len(sources)): cells.append((n, (n % grid)*w, (n//grid)*h, w, h))
            height = h*rows
        else:
            ratio = w/h if shape == 'source' else CELL[shape]
            cell_w = (inner-gap*(grid-1))/grid
            if cell_w < 8: raise ValueError('输出宽度太小，请增大宽度或减小间距')
            cell_h = max(1, round(cell_w/ratio))
            for n in range(len(sources)):
                col, row = n % grid, n//grid
                x0 = round(gap+col*(cell_w+gap)); x1 = round(gap+col*(cell_w+gap)+cell_w)
                cells.append((n, x0, top+row*(cell_h+gap), x1-x0, cell_h))
            height = top+rows*cell_h+(rows-1)*gap+gap

    items = []
    for n, x, y0, cw, ch in cells:
        s, (sw, sh) = sources[n], dimensions[n]
        box = boxes[n]
        if original:
            crop, target = box, (x, y0, sw, sh)
        elif layout == 'justified' or fit == 'cover':
            crop, target = _cover(box, cw/ch), (x, y0, cw, ch)
        else:
            scale = min(cw/box[2], ch/box[3])
            tw, th = max(1, round(box[2]*scale)), max(1, round(box[3]*scale))
            crop, target = box, (x+(cw-tw)//2, y0+(ch-th)//2, tw, th)
        tx, ty, tw, th = target
        caption = _caption(payload, s, not original)
        items.append({k: s[k] for k in ('id', 'src', 'thumb')} | dict(
            source_width=sw, source_height=sh,
            crop=dict(x=crop[0], y=crop[1], width=crop[2], height=crop[3]),
            target=dict(x=tx, y=ty, width=tw, height=th),
            cell=dict(x=x, y=y0, width=cw, height=ch),
            radius=min(radius, tw//2, th//2),
            caption=caption, caption_size=0))
    # One caption size for the whole board, from the typical tile rather than each tile.
    if any(i['caption'] for i in items):
        size = max(12, round(median(min(i['target']['width'], i['target']['height']*16/9) for i in items)*.04))
        for i in items:
            if i['caption']: i['caption_size'] = _caption_fit(i['caption'], size, i['target']['width'])

    memory = width*height*8+max(sw*sh for sw, sh in dimensions)*12
    safe = width*height <= MAX_PIXELS and memory <= MAX_MEMORY
    enlarged = any(i['target']['width'] > i['crop']['width']+1 or i['target']['height'] > i['crop']['height']+1 for i in items)
    warning = '部分画面将被放大，放大不会增加原图细节。' if enlarged else ''
    if (title or any(i['caption'] for i in items)) and not font_path():
        warning += '未找到中文字体，中文文字可能显示为方框；可把字体文件放入 fonts 文件夹。'
    background = BACKGROUNDS[payload.get('background', 'white')]
    first = cells[0] if cells else (0, 0, 0, w, h)
    return dict(width=width, height=height, layout=layout, rows=rows,
                cell_width=first[3], image_height=first[4], caption_height=0, gap=gap, radius=radius,
                shadow=shadow, shadow_blur=max(4, round(width*.006)) if shadow else 0,
                background='#%02x%02x%02x' % background, text_color='#1f2937' if sum(background) > 384 else '#f3f4f6',
                title=title, items=items, estimated_memory_bytes=memory, can_render=safe,
                warning=warning if safe else '图片过大，超过4000万像素或512MB内存预算，请降低输出宽度或减少布局格数')


def _rounded_mask(size, radius):
    """Anti-aliased rounded rectangle: drawn at 4x and reduced."""
    w, h = size
    if radius <= 0: return None
    big = Image.new('L', (w*4, h*4), 0)
    ImageDraw.Draw(big).rounded_rectangle((0, 0, w*4-1, h*4-1), radius*4, fill=255)
    return big.resize(size, Image.Resampling.LANCZOS)


def _draw_shadows(canvas, plan):
    blur = plan['shadow_blur']
    alpha = Image.new('L', canvas.size, 0)
    draw = ImageDraw.Draw(alpha)
    for item in plan['items']:
        t = item['target']; offset = blur//2
        draw.rounded_rectangle((t['x'], t['y']+offset, t['x']+t['width']-1, t['y']+t['height']-1+offset), item['radius'], fill=90)
    alpha = alpha.filter(ImageFilter.GaussianBlur(blur))
    canvas.paste((0, 0, 0), mask=alpha)


def _draw_caption(canvas, item):
    size = item['caption_size']; t = item['target']
    face = font(size); left, top, right, bottom = face.getbbox(item['caption'])
    pad_x, pad_y = round(size*.5), round(size*.3)
    box_w = min(t['width'], right-left+pad_x*2); box_h = size+pad_y*2
    margin = round(size*.45)
    x = t['x']+margin; y = t['y']+t['height']-margin-box_h
    if box_w+margin > t['width'] or box_h+margin > t['height']: return
    region = canvas.crop((x, y, x+box_w, y+box_h)).convert('RGBA')
    layer = Image.new('RGBA', region.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, box_w-1, box_h-1), round(box_h/3), fill=(15, 23, 42, 150))
    region = Image.alpha_composite(region, layer)
    ImageDraw.Draw(region).text((pad_x, box_h/2), item['caption'], font=face, fill=(255, 255, 255, 255), anchor='lm')
    canvas.paste(region.convert('RGB'), (x, y))


def render(plan, sources, payload, path, preview):
    if not plan['can_render']: raise ValueError(plan['warning'])
    with Image.new('RGB', (plan['width'], plan['height']), plan['background']) as canvas:
        if plan['shadow']: _draw_shadows(canvas, plan)
        for item, s in zip(plan['items'], sources):
            with Image.open(s['path']) as image:
                rgb = image.convert('RGB')
                try:
                    t = item['target']; c = item['crop']; size = (t['width'], t['height'])
                    if payload.get('mode', 'original') == 'original': canvas.paste(rgb, (t['x'], t['y']))
                    else:
                        with rgb.resize(size, Image.Resampling.LANCZOS, box=(c['x'], c['y'], c['x']+c['width'], c['y']+c['height'])) as fitted:
                            canvas.paste(fitted, (t['x'], t['y']), _rounded_mask(size, item['radius']))
                finally: rgb.close()
            if item['caption']: _draw_caption(canvas, item)
        if plan['title']:
            t = plan['title']
            ImageDraw.Draw(canvas).text((t['x'], t['y']), t['text'], font=font(t['size']), fill=plan['text_color'], anchor='lm')
        fmt = payload.get('format', 'png'); canvas.save(path, format='PNG' if fmt == 'png' else 'JPEG', **({} if fmt == 'png' else {'quality': 95, 'subsampling': 0}))
    # Decode the actual exported file, including JPEG loss, so the preview matches it.
    with Image.open(path) as final:
        final.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
        final.convert('RGB').save(preview, format='JPEG', quality=90)

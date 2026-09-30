"""Pixel-exact source grids and bounded, serial Pillow exports."""
import math
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

MAX_PIXELS = 40_000_000
MAX_MEMORY = 512 * 1024 * 1024
TOKEN = re.compile(r'^[0-9a-f]{32}$')
TEMP_NAME = re.compile(r'^collage-[0-9a-f]{32}(?:-preview\.jpg|\.png|\.jpg|\.json)$')

def managed_file(name):
    return bool(TEMP_NAME.fullmatch(name))

def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high: raise ValueError(name+'无效')
    return value

def plan(payload, sources):
    grid=integer(payload.get('grid',3),2,3,'布局')
    mode=payload.get('mode','original');shape=payload.get('shape','source');fit=payload.get('fit','contain')
    if mode not in ('original','share','custom') or shape not in ('source','landscape','square','portrait') or fit not in ('contain','cover'):raise ValueError('拼图模式无效')
    if payload.get('background','white') not in ('white','dark'):raise ValueError('底色无效')
    if payload.get('format','png') not in ('png','jpeg'):raise ValueError('图片格式无效')
    for key in ('index','time'):
        if type(payload.get(key,False)) is not bool:raise ValueError('标注参数无效')
    integer(payload.get('page',1),1,100000,'页码')
    if 'width' in payload:integer(payload['width'],64,12000,'输出宽度')
    if not 1 <= len(sources) <= grid*grid:raise ValueError('所选图片超过布局容量')
    crops=payload.get('crops',{})
    if not isinstance(crops,dict) or any(k not in {str(s['id']) for s in sources} for k in crops):raise ValueError('裁剪参数无效')
    dimensions=[]
    for s in sources:
        with Image.open(s['path']) as im:dimensions.append(im.size)
    w,h=dimensions[0]
    if mode=='original' and any(size!=(w,h) for size in dimensions):raise ValueError('原尺寸无缝模式要求图片尺寸一致，请选择分享模式')
    width=w*grid if mode=='original' else integer(payload.get('width',3000),64,12000,'输出宽度')
    ratio=w/h if mode=='original' or shape=='source' else {'landscape':16/9,'square':1,'portrait':9/16}[shape]
    cell_width=width//grid
    image_height=max(1,round(cell_width/ratio))
    caption=0 if mode=='original' or shape=='source' else (max(20,round(cell_width*.055)) if payload.get('index') or payload.get('time') else 0)
    height=(image_height+caption)*grid
    items=[]
    for n,(s,(sw,sh)) in enumerate(zip(sources,dimensions)):
        x0=round((n%grid)*width/grid); x1=round((n%grid+1)*width/grid); y0=(n//grid)*(image_height+caption)
        cw=x1-x0; sx=sy=0; rw=sw; rh=sh
        c=crops.get(str(s['id']))
        if c is not None:
            if not isinstance(c,dict) or set(c)!=set(('x','y','width','height')):raise ValueError('裁剪参数无效')
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in c.values()):raise ValueError('裁剪参数无效')
            if c['x']<0 or c['y']<0 or c['width']<=0 or c['height']<=0 or c['x']+c['width']>1.000001 or c['y']+c['height']>1.000001:raise ValueError('裁剪区域超出原图')
        if mode!='original':
            if c and fit=='cover':sx=c['x']*sw;sy=c['y']*sh;rw=c['width']*sw;rh=c['height']*sh
            if fit=='cover':
                target_ratio=cw/image_height
                if rw/rh>target_ratio:
                    new=rh*target_ratio;sx+=(rw-new)/2;rw=new
                else:
                    new=rw/target_ratio;sy+=(rh-new)/2;rh=new
                tw,th=cw,image_height
            else:
                scale=min(cw/rw,image_height/rh);tw=max(1,round(rw*scale));th=max(1,round(rh*scale))
        else:tw,th=sw,sh
        labels=[]
        if caption and payload.get('index'):labels.append(f"#{s['id']+1:03d}")
        if caption and payload.get('time'):labels.append(s.get('label','未知'))
        items.append({k:s[k] for k in ('id','src','thumb')} | dict(source_width=sw,source_height=sh,crop=dict(x=sx,y=sy,width=rw,height=rh),target=dict(x=x0+(cw-tw)//2,y=y0+(image_height-th)//2,width=tw,height=th),cell=dict(x=x0,y=y0,width=cw,height=image_height+caption),caption='  '.join(labels)))
    memory=width*height*8+max(sw*sh for sw,sh in dimensions)*12
    safe=width*height<=MAX_PIXELS and memory<=MAX_MEMORY
    enlarged=any(i['target']['width']>i['crop']['width']+1 or i['target']['height']>i['crop']['height']+1 for i in items)
    warning='部分画面将被放大，放大不会增加原图细节。' if enlarged else ''
    return dict(width=width,height=height,cell_width=cell_width,image_height=image_height,caption_height=caption,gap=0,items=items,estimated_memory_bytes=memory,can_render=safe,warning=warning if safe else '图片过大，超过4000万像素或512MB内存预算，请降低输出宽度或减少布局格数')

def render(plan, sources, payload, path, preview):
    if not plan['can_render']:raise ValueError(plan['warning'])
    color=(255,255,255) if payload.get('background','white')=='white' else (17,24,39)
    with Image.new('RGB',(plan['width'],plan['height']),color) as canvas:
        for item,s in zip(plan['items'],sources):
            with Image.open(s['path']) as image:
                rgb=image.convert('RGB')
                try:
                    t=item['target'];c=item['crop'];size=(t['width'],t['height'])
                    if payload.get('mode','original')=='original':canvas.paste(rgb,(t['x'],t['y']))
                    else:
                        with rgb.resize(size,Image.Resampling.LANCZOS,box=(c['x'],c['y'],c['x']+c['width'],c['y']+c['height'])) as fitted:canvas.paste(fitted,(t['x'],t['y']))
                finally:rgb.close()
            if item['caption']:
                d=ImageDraw.Draw(canvas);cell=item['cell']
                font=ImageFont.load_default(size=max(12,plan['caption_height']//2))
                d.text((cell['x']+4,cell['y']+plan['image_height']+2),item['caption'],fill=(30,41,59) if color==(255,255,255) else 'white',font=font)
        fmt=payload.get('format','png');canvas.save(path,format='PNG' if fmt=='png' else 'JPEG',**({} if fmt=='png' else {'quality':95,'subsampling':0}))
    # Decode the actual exported file, including JPEG loss, so the preview matches it.
    with Image.open(path) as final:
        final.thumbnail((1200,1200),Image.Resampling.LANCZOS)
        final.convert('RGB').save(preview,format='JPEG',quality=90)

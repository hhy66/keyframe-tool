/* Konva draws a bounded, editable preview of the server plan; Pillow owns the final image geometry and pixels. */
const COLLAGE_FONT = '"Microsoft YaHei","PingFang SC","Noto Sans CJK SC","WenQuanYi Zen Hei",sans-serif';

class CollageStage {
  constructor(container, {onSwap, onSelect, onEdit}) {
    this.container = container;
    this.onSwap = onSwap;
    this.onSelect = onSelect;
    this.onEdit = onEdit;
    this.stage = null;
    this.generation = 0;
    this.nodes = new Map();
    this.focus = null;
  }

  destroy() {
    this.generation++;
    if (this.stage) this.stage.destroy();
    this.stage = null;
    this.nodes.clear();
  }

  static roundedPath(ctx, w, h, r) {
    ctx.beginPath();
    if (r <= 0) {
      ctx.rect(0, 0, w, h);
      return;
    }
    ctx.moveTo(r, 0);
    ctx.arcTo(w, 0, w, h, r);
    ctx.arcTo(w, h, 0, h, r);
    ctx.arcTo(0, h, 0, 0, r);
    ctx.arcTo(0, 0, w, 0, r);
    ctx.closePath();
  }

  // The label pill in the corner chosen on the server (same geometry as collage_engine.caption_box).
  static caption(item) {
    const size = item.caption_size,
      t = item.target,
      margin = Math.round(size * 0.45);
    const label = new Konva.Label({listening: false});
    label.add(new Konva.Tag({fill: 'rgba(15,23,42,.59)', cornerRadius: size * 0.53}));
    label.add(
      new Konva.Text({
        text: item.caption,
        fontSize: size,
        fontFamily: COLLAGE_FONT,
        fill: 'white',
        padding: size * 0.3,
        lineHeight: 1,
      }),
    );
    const right = ['tr', 'br'].includes(item.caption_position),
      bottom = ['bl', 'br'].includes(item.caption_position);
    label.x(right ? t.width - margin - label.width() : margin);
    label.y(bottom ? t.height - margin - label.height() : margin);
    return label;
  }

  // Label and note written in the band under the photo.
  static below(item, plan) {
    const b = item.below,
      t = item.target,
      group = new Konva.Group({x: b.x - t.x, y: b.y - t.y, listening: false});
    const text = (value, x, fill) =>
      group.add(
        new Konva.Text({
          x,
          y: 0,
          height: b.height,
          text: value,
          fontSize: b.size,
          fontFamily: COLLAGE_FONT,
          fill,
          verticalAlign: 'middle',
          wrap: 'none',
        }),
      );
    if (b.label) text(b.label, 0, plan.muted_color);
    if (b.note) text(b.note, b.note_x - b.x, plan.text_color);
    return group;
  }

  async show(plan) {
    this.destroy();
    const generation = this.generation;
    this.plan = plan;
    const available = Math.max(240, (this.container.clientWidth || 640) - 16);
    const scale = Math.min(available / plan.width, Math.max(320, window.innerHeight - 330) / plan.height);
    this.stage = new Konva.Stage({
      container: this.container,
      width: Math.round(plan.width * scale),
      height: Math.round(plan.height * scale),
      scaleX: scale,
      scaleY: scale,
    });
    const layer = new Konva.Layer();
    this.stage.add(layer);
    layer.add(
      new Konva.Rect({x: 0, y: 0, width: plan.width, height: plan.height, fill: plan.background, listening: false}),
    );
    if (plan.title) {
      const t = plan.title;
      layer.add(
        new Konva.Text({
          x: t.x,
          y: t.y - t.size * 0.6,
          height: t.size * 1.2,
          text: t.text,
          fontSize: t.size,
          fontFamily: COLLAGE_FONT,
          fill: plan.text_color,
          verticalAlign: 'middle',
          listening: false,
        }),
      );
    }
    for (const item of plan.items) {
      const image = new Image();
      image.src = item.thumb || item.src;
      try {
        await image.decode();
      } catch {
        if (generation === this.generation) throw new Error('缩略图读取失败，请确认截图仍然存在');
        return;
      }
      if (generation !== this.generation) return;
      const t = item.target,
        c = item.crop,
        r = item.radius;
      const kx = (image.naturalWidth || item.source_width) / item.source_width;
      const ky = (image.naturalHeight || item.source_height) / item.source_height;
      const group = new Konva.Group({x: t.x, y: t.y, draggable: true});
      if (plan.shadow) {
        group.add(
          new Konva.Rect({
            width: t.width,
            height: t.height,
            cornerRadius: r,
            fill: plan.background,
            shadowColor: 'black',
            shadowBlur: plan.shadow_blur * 2,
            shadowOffsetY: plan.shadow_blur / 2,
            shadowOpacity: 0.35,
            listening: false,
          }),
        );
      }
      const clip = new Konva.Group({clipFunc: ctx => CollageStage.roundedPath(ctx, t.width, t.height, r)});
      clip.add(
        new Konva.Image({
          image,
          width: t.width,
          height: t.height,
          crop: {x: c.x * kx, y: c.y * ky, width: c.width * kx, height: c.height * ky},
        }),
      );
      group.add(clip);
      if (item.caption) group.add(CollageStage.caption(item));
      if (item.below) group.add(CollageStage.below(item, plan));
      const border = new Konva.Rect({
        width: t.width,
        height: t.height,
        cornerRadius: r,
        stroke: '#4f46e5',
        strokeWidth: 4 / scale,
        visible: false,
        listening: false,
      });
      group.add(border);
      group.on('mouseenter', () => {
        this.container.style.cursor = 'grab';
      });
      group.on('mouseleave', () => {
        this.container.style.cursor = '';
      });
      group.on('click tap', () => this.select(item.id));
      group.on('dblclick dbltap', () => {
        this.select(item.id);
        this.onEdit(item.id);
      });
      group.on('dragstart', () => {
        group.moveToTop();
        group.opacity(0.8);
        this.select(item.id);
      });
      group.on('dragend', () => {
        const x = group.x() + t.width / 2,
          y = group.y() + t.height / 2;
        const target = plan.items.reduce((best, value) => {
          const v = value.target,
            d = (v.x + v.width / 2 - x) ** 2 + (v.y + v.height / 2 - y) ** 2;
          return !best || d < best.distance ? {id: value.id, distance: d} : best;
        }, null);
        group.position({x: t.x, y: t.y});
        group.opacity(1);
        if (target && target.id !== item.id) this.onSwap(item.id, target.id);
      });
      this.nodes.set(item.id, {group, border});
      layer.add(group);
    }
    if (generation !== this.generation) return;
    if (this.focus !== null && this.nodes.has(this.focus)) this.select(this.focus, true);
    this.stage.draw();
  }

  select(id, quiet) {
    if (!this.nodes.has(id)) return;
    this.focus = id;
    for (const [key, value] of this.nodes) value.border.visible(key === id);
    this.stage?.batchDraw();
    if (!quiet) this.onSelect(id);
  }
}

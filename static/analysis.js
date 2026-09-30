/* Picture analysis of the results (frame_analysis.py, computed on this computer): started with a
   button, then tags on the result cards and a panel in the full-screen viewer.
   Uses `state`, `jsonRequest` and `toast` from app.js. */
const ANALYSIS_PANEL_KEY = 'keyframe-tool.viewer.analysis';
const ANALYSIS_GUIDES_KEY = 'keyframe-tool.viewer.guides';

// Text rows of the viewer panel for one analysed picture. Pure, so it can be tested on its own.
function analysisRows(item) {
  const frame = item.frame;
  const color = item.color;
  const tone = item.tone;
  const percent = value => `${Math.round(value * 100)}%`;
  const bars = frame.bars.top ? '上下有黑边' : frame.bars.left ? '左右有黑边' : '';
  const aspect = frame.content_ratio >= 2.2 ? `${frame.aspect} 宽银幕` : frame.aspect;
  const clipped = [
    tone.highlights >= 0.02 ? `过曝 ${percent(tone.highlights)}` : '',
    tone.shadows >= 0.02 ? `欠曝 ${percent(tone.shadows)}` : '',
  ].filter(Boolean);
  return {
    frame: [
      ['比例', [aspect, frame.orientation, bars].filter(Boolean).join(' · ')],
      ['原图', `${item.size[0]} × ${item.size[1]}`],
    ],
    color: [
      ['冷暖', [color.temperature.label, color.temperature.note].filter(Boolean).join('，')],
      ['饱和度', color.saturation.label],
      ['色彩关系', [color.harmony.label, color.harmony.note].filter(Boolean).join('：')],
      ...(color.accent
        ? [['点缀色', `${color.accent.name} ${color.accent.hex.toUpperCase()}，约占 ${percent(color.accent.share)}`]]
        : []),
    ],
    tone: [
      ['类型', `${tone.key} · ${tone.contrast}`],
      ...(tone.note ? [['说明', tone.note]] : []),
      ['平均亮度', percent(tone.brightness)],
      ...(clipped.length ? [['细节损失', clipped.join('，')]] : []),
    ],
    light: [['结论', item.light.label]],
    shot: [
      ['景别', item.shot.label || '无法判断'],
      ...(item.shot.people > 1 ? [['人数', `约 ${item.shot.people} 人`]] : []),
      ['依据', item.shot.basis],
    ],
    composition: [
      ['构图', item.composition.label || '无明显主体'],
      ['主体位置', item.composition.position],
      ['留白', item.composition.space],
      ['依据', item.composition.basis],
    ].filter(([, value]) => value),
    lens: [
      ['水平', item.level.label],
      ['景深', item.depth.label],
      ['说明', item.depth.basis],
    ],
  };
}

const Analysis = {
  run: null,
  items: [],
  status: 'idle',
  job: null,
  token: 0,
  timer: null,

  el(selector) {
    return document.querySelector(selector);
  },
  panelOpen() {
    try {
      return localStorage.getItem(ANALYSIS_PANEL_KEY) !== '0';
    } catch {
      return true;
    }
  },
  guidesOn() {
    try {
      return localStorage.getItem(ANALYSIS_GUIDES_KEY) === '1';
    } catch {
      return false;
    }
  },

  // New results on screen: show what was analysed before, without starting anything.
  async load(res) {
    const token = ++this.token;
    clearTimeout(this.timer);
    this.run = res?.run || null;
    this.items = [];
    this.status = 'idle';
    this.job = null;
    this.paint();
    if (!state.sid || !this.run) return;
    let data;
    try {
      data = await jsonRequest(`/api/analysis/${encodeURIComponent(state.sid)}?run=${encodeURIComponent(this.run)}`);
    } catch {
      return; // 分析只是辅助信息，读取失败不影响挑选和下载
    }
    if (token !== this.token) return;
    this.accept(data);
    if (this.status === 'running') this.poll(token);
  },

  accept(data) {
    this.status = data.status;
    this.job = data.job;
    if (!data.items) return this.paintButton(); // progress only
    this.items = data.items;
    this.paint();
  },

  async start() {
    if (!state.sid || !this.run || this.status === 'running') return;
    const token = this.token;
    this.status = 'running';
    this.paintButton();
    try {
      const data = await jsonRequest(`/api/analysis/${encodeURIComponent(state.sid)}`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({run: this.run}),
      });
      if (token !== this.token) return;
      this.accept(data);
      if (data.status === 'running') this.poll(token);
      else this.finish(token);
    } catch (e) {
      if (token !== this.token) return;
      this.status = 'idle';
      this.paintButton();
      toast(e.message || '画面分析启动失败', true);
    }
  },

  poll(token) {
    clearTimeout(this.timer);
    this.timer = setTimeout(async () => {
      if (token !== this.token) return;
      let data;
      try {
        data = await jsonRequest(
          `/api/analysis/${encodeURIComponent(state.sid)}?run=${encodeURIComponent(this.run)}&items=false`,
        );
      } catch {
        return this.poll(token); // 连接短暂中断：继续等
      }
      if (token !== this.token) return;
      this.accept(data);
      if (data.status === 'running') this.poll(token);
      else this.finish(token);
    }, 700);
  },

  async finish(token) {
    let data;
    try {
      data = await jsonRequest(`/api/analysis/${encodeURIComponent(state.sid)}?run=${encodeURIComponent(this.run)}`);
    } catch (e) {
      toast(e.message || '读取画面分析结果失败', true);
      return;
    }
    if (token !== this.token) return;
    this.accept(data);
    const job = data.job || {};
    if (job.status === 'error') toast(`画面分析未完成：${job.error}`, true);
    else
      toast(
        `画面分析完成：${data.ready} / ${data.total} 张` +
          (job.failed ? `，${job.failed} 张图片读取失败` : '') +
          '。单击缩略图可在大图里查看详细分析。',
      );
  },

  // ------------------------------------------------------------------ painting
  paint() {
    this.paintButton();
    this.paintCards();
    if (typeof Viewer !== 'undefined' && Viewer.isOpen()) this.paintViewer(Viewer.index);
  },

  missing() {
    return this.items.length ? this.items.filter(item => !item).length : state.cuts.length;
  },

  paintButton() {
    const button = this.el('#btnAnalyze');
    if (!button) return;
    const total = state.cuts.length;
    const missing = this.missing();
    button.hidden = !total;
    button.classList.toggle('done', this.status === 'done');
    if (this.status === 'running') {
      const job = this.job;
      button.textContent = job?.total ? `分析中 ${job.done}/${job.total}…` : '分析中…';
      button.disabled = true;
    } else if (!missing) {
      button.textContent = '✓ 画面已分析';
      button.disabled = true;
    } else {
      button.textContent = missing < total ? `分析新增的 ${missing} 张` : '分析画面';
      button.disabled = false;
    }
  },

  paintCards() {
    (state.cards || []).forEach((card, i) => {
      card.item.querySelector('.anaRow')?.remove();
      const item = this.items[i];
      if (!item) return;
      const row = document.createElement('div');
      row.className = 'anaRow';
      row.title = '画面分析（单击缩略图查看详情）';
      row.appendChild(this.swatches(item.color.palette, 'anaStrip'));
      const tags = document.createElement('div');
      tags.className = 'anaTags';
      for (const text of item.tags) {
        const tag = document.createElement('span');
        tag.className = 'anaTag';
        tag.textContent = text;
        tags.appendChild(tag);
      }
      row.appendChild(tags);
      card.cap.after(row);
    });
  },

  // A strip of the main colours, each as wide as its share of the picture.
  swatches(palette, className) {
    const strip = document.createElement('div');
    strip.className = className;
    strip.setAttribute('aria-label', '主色：' + palette.map(c => `${c.name} ${Math.round(c.share * 100)}%`).join('，'));
    for (const colour of palette) {
      const swatch = document.createElement('span');
      swatch.style.background = colour.hex;
      swatch.style.flexGrow = String(colour.share);
      swatch.title = `${colour.name} ${colour.hex} · ${Math.round(colour.share * 100)}%`;
      strip.appendChild(swatch);
    }
    return strip;
  },

  // ------------------------------------------------------------------ viewer panel
  togglePanel() {
    const open = !this.panelOpen();
    try {
      localStorage.setItem(ANALYSIS_PANEL_KEY, open ? '1' : '0');
    } catch {
      /* 仅本次有效 */
    }
    if (typeof Viewer !== 'undefined' && Viewer.isOpen()) {
      this.paintViewer(Viewer.index);
      Viewer.fit();
    }
  },

  toggleGuides() {
    try {
      localStorage.setItem(ANALYSIS_GUIDES_KEY, this.guidesOn() ? '0' : '1');
    } catch {
      /* 仅本次有效 */
    }
    if (typeof Viewer !== 'undefined' && Viewer.isOpen()) this.paintGuides(Viewer.index);
  },

  // Rule-of-thirds lines inside the picture (black bars excluded), people, the main subject and the
  // horizon found by the analysis, drawn over the picture in the viewer.
  paintGuides(i) {
    const svg = this.el('#vwGuides');
    const button = this.el('#vwGuideBtn');
    if (!svg || i == null) return;
    const item = this.items[i];
    const on = this.guidesOn();
    button.setAttribute('aria-pressed', String(on));
    button.disabled = !item;
    // An <svg> has no `hidden` property: toggle the attribute itself.
    svg.toggleAttribute('hidden', !on || !item);
    svg.innerHTML = '';
    if (!on || !item) return;
    const [cx, cy, cw, ch] = item.frame.content || [0, 0, 1, 1];
    const X = u => (cx + u * cw) * 1000;
    const Y = v => (cy + v * ch) * 1000;
    const add = (tag, attributes) => {
      const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
      node.setAttribute('vector-effect', 'non-scaling-stroke');
      svg.appendChild(node);
      return node;
    };
    const line = (x0, y0, x1, y1, className) =>
      add('line', {x1: X(x0), y1: Y(y0), x2: X(x1), y2: Y(y1), class: className});
    const box = ([x, y, w, h], className) =>
      add('rect', {x: X(x), y: Y(y), width: w * cw * 1000, height: h * ch * 1000, class: className});
    for (const t of [1 / 3, 2 / 3]) {
      line(t, 0, t, 1, 'gThird');
      line(0, t, 1, t, 'gThird');
    }
    const found = item.people || {faces: [], people: []};
    found.people.forEach(person => box(person.box, 'gPerson'));
    found.faces.forEach(face => box(face.box, 'gFace'));
    if (item.subject && item.subject.source === '显著区域') box(item.subject.box, 'gSalient');
    if (item.subject) {
      const [px, py] = item.subject.point;
      line(px - 0.025, py, px + 0.025, py, 'gPoint');
      line(px, py - 0.04, px, py + 0.04, 'gPoint');
    }
    if (item.level.line) {
      const [x0, y0, x1, y1] = item.level.line;
      line(x0, y0, x1, y1, 'gLevel');
    }
  },

  paintViewer(i) {
    this.paintGuides(i);
    const panel = this.el('#vwAnalysis');
    const toggle = this.el('#vwInfo');
    if (!panel || i == null) return;
    const open = this.panelOpen();
    panel.hidden = !open;
    toggle.setAttribute('aria-pressed', String(open));
    if (!open) return;
    panel.innerHTML = '';
    const heading = document.createElement('h3');
    heading.textContent = '画面分析';
    panel.appendChild(heading);
    const item = this.items[i];
    if (!item) {
      const note = document.createElement('p');
      note.className = 'anaEmpty';
      note.textContent =
        this.status === 'running'
          ? '正在分析，完成后显示在这里。'
          : '还没有分析这张图。分析在本机完成，不联网：色彩、影调、画幅与明暗分布。';
      panel.appendChild(note);
      if (this.status !== 'running' && state.cuts.length) {
        const start = document.createElement('button');
        start.type = 'button';
        start.className = 'vwBtn';
        start.textContent = '分析全部画面';
        start.addEventListener('click', () => this.start());
        panel.appendChild(start);
      }
      return;
    }
    const rows = analysisRows(item);
    const section = (title, lines, extra) => {
      const box = document.createElement('section');
      const name = document.createElement('h4');
      name.textContent = title;
      box.appendChild(name);
      if (extra) box.appendChild(extra);
      const list = document.createElement('dl');
      for (const [label, value] of lines) {
        const term = document.createElement('dt');
        term.textContent = label;
        const detail = document.createElement('dd');
        detail.textContent = value;
        list.append(term, detail);
      }
      box.appendChild(list);
      panel.appendChild(box);
    };
    section('景别', rows.shot);
    section('构图', rows.composition);
    section('水平与景深', rows.lens);
    section('画幅', rows.frame);
    section('色彩', rows.color, this.paletteList(item.color.palette));
    section('影调', rows.tone, this.histogram(item.tone.histogram));
    section('明暗分布', rows.light, this.lightGrid(item.light.grid));
    const foot = document.createElement('p');
    foot.className = 'anaFoot';
    foot.textContent =
      '以上为本机测量值，不联网。景别按人脸大小推算，背影或极近的特写可能判断不出；按 G 在画面上显示三分线、人物框和主体位置。';
    panel.appendChild(foot);
  },

  paletteList(palette) {
    const box = document.createElement('div');
    box.appendChild(this.swatches(palette, 'anaBigStrip'));
    const list = document.createElement('ul');
    list.className = 'anaColours';
    for (const colour of palette) {
      const row = document.createElement('li');
      const chip = document.createElement('i');
      chip.style.background = colour.hex;
      const name = document.createElement('span');
      name.textContent = colour.name;
      const code = document.createElement('code');
      code.textContent = colour.hex.toUpperCase();
      const share = document.createElement('b');
      share.textContent = `${Math.round(colour.share * 100)}%`;
      row.append(chip, name, code, share);
      list.appendChild(row);
    }
    box.appendChild(list);
    return box;
  },

  // Brightness histogram, dark on the left and bright on the right.
  histogram(values) {
    const chart = document.createElement('div');
    chart.className = 'anaHistogram';
    chart.setAttribute('aria-label', '亮度分布直方图：左暗右亮');
    const peak = Math.max(...values, 0.0001);
    for (const value of values) {
      const bar = document.createElement('span');
      bar.style.height = `${Math.max(2, Math.round((value / peak) * 100))}%`;
      chart.appendChild(bar);
    }
    return chart;
  },

  // 3×3 map of the average brightness of each part of the picture.
  lightGrid(grid) {
    const map = document.createElement('div');
    map.className = 'anaLightGrid';
    map.setAttribute('aria-hidden', 'true');
    for (const row of grid) {
      for (const value of row) {
        const cell = document.createElement('span');
        const level = Math.round(value * 255);
        cell.style.background = `rgb(${level}, ${level}, ${level})`;
        map.appendChild(cell);
      }
    }
    return map;
  },
};

if (typeof document !== 'undefined' && document.querySelector('#btnAnalyze')) {
  document.querySelector('#btnAnalyze').addEventListener('click', () => Analysis.start());
  document.querySelector('#vwInfo').addEventListener('click', () => Analysis.togglePanel());
  document.querySelector('#vwGuideBtn').addEventListener('click', () => Analysis.toggleGuides());
}

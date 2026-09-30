/* Full-screen, one-by-one review of the results: look, decide, move on.
   Uses `state`, `setKept`, `kindName` and `toast` from app.js, and `Review` (similar-shot scores). */
const Viewer = {
  index: null,
  touchX: null,

  el(selector) {
    return document.querySelector(selector);
  },
  isOpen() {
    return !this.el('#lb').hidden;
  },
  count() {
    return state.cuts.length;
  },

  open(i) {
    if (!state.frames[i]) return;
    if (!this.isOpen()) {
      this.el('#lb').hidden = false;
      document.body.classList.add('viewerOpen');
      this.buildStrip();
    }
    this.show(i);
  },

  close() {
    if (!this.isOpen()) return;
    const last = this.index;
    this.el('#lb').hidden = true;
    this.el('#vwImg').removeAttribute('src');
    document.body.classList.remove('viewerOpen');
    this.index = null;
    state.lbIndex = null;
    // Back in the grid, the card seen last is the current one.
    if (last != null && typeof Review !== 'undefined') Review.setCursor(last);
  },

  show(i) {
    const count = this.count();
    if (!count) return this.close();
    i = Math.max(0, Math.min(count - 1, i));
    this.index = i;
    state.lbIndex = i;
    const cut = state.cuts[i];
    const image = this.el('#vwImg');
    image.src = state.frames[i];
    image.alt = `#${String(i + 1).padStart(3, '0')} ${cut.label || ''}`;
    this.el('#vwPos').textContent = `${i + 1} / ${count}`;
    this.el('#vwMeta').textContent = [
      `#${String(i + 1).padStart(3, '0')}`,
      cut.label,
      kindName[cut.kind] || cut.kind,
      cut.frame_index == null ? '原帧号未知' : `第 ${cut.frame_index + 1} 帧`,
    ]
      .filter(Boolean)
      .join(' · ');
    const score = typeof Review !== 'undefined' && Review.flagged.includes(i) ? Review.scores?.[i] : null;
    this.el('#vwSimilar').hidden = score == null;
    if (score != null) this.el('#vwSimilar').textContent = `与上一张相似 ${Math.round(score)}%`;
    const download = this.el('#vwDownload');
    download.href = state.frames[i].replace('/api/frame/', '/api/frame-dl/');
    download.download = '';
    this.el('#vwPrev').disabled = i === 0;
    this.el('#vwNext').disabled = i === count - 1;
    this.el('#vwEdit').disabled = !state.cards?.[i] || state.cards[i].edit.disabled;
    if (typeof Analysis !== 'undefined') Analysis.paintViewer(i);
    this.paint();
    this.preload(i);
  },

  // Keep / skip state, counters and the film strip, without reloading the picture.
  paint() {
    const i = this.index;
    if (i == null) return;
    const keep = state.sel.has(i);
    this.el('#lb').classList.toggle('skipped', !keep);
    const badge = this.el('#vwState');
    badge.textContent = keep ? '✓ 保留' : '已跳过';
    badge.className = 'vwState ' + (keep ? 'keep' : 'skip');
    this.el('#vwKeep').setAttribute('aria-pressed', String(keep));
    this.el('#vwSkip').setAttribute('aria-pressed', String(!keep));
    this.el('#vwStats').textContent = `保留 ${state.sel.size} / ${this.count()}`;
    const strip = this.el('#vwStrip');
    [...strip.children].forEach((thumb, k) => {
      thumb.classList.toggle('current', k === i);
      thumb.classList.toggle('off', !state.sel.has(k));
    });
    const current = strip.children[i];
    // Scroll only the strip itself, never the page behind the viewer.
    if (current) strip.scrollLeft = current.offsetLeft - strip.clientWidth / 2 + current.offsetWidth / 2;
  },

  buildStrip() {
    const strip = this.el('#vwStrip');
    strip.innerHTML = '';
    state.cuts.forEach((cut, i) => {
      const thumb = document.createElement('button');
      thumb.type = 'button';
      thumb.className = 'vwThumb';
      thumb.title = `#${String(i + 1).padStart(3, '0')} ${cut.label || ''}`;
      const image = document.createElement('img');
      image.loading = 'lazy';
      image.src = state.thumbs[i];
      image.alt = '';
      thumb.appendChild(image);
      thumb.addEventListener('click', () => this.show(i));
      strip.appendChild(thumb);
    });
  },

  // Scale the picture to the free space, small sources included, keeping its shape;
  // sizing the image itself keeps the keep/skip label on the picture's corner.
  fit() {
    const image = this.el('#vwImg');
    const stage = this.el('#vwStage');
    if (!image.naturalWidth || !stage.clientWidth) return;
    const style = getComputedStyle(stage);
    const width = stage.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height = stage.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    const scale = Math.min(width / image.naturalWidth, height / image.naturalHeight);
    image.style.width = `${Math.max(1, Math.floor(image.naturalWidth * scale))}px`;
    image.style.height = `${Math.max(1, Math.floor(image.naturalHeight * scale))}px`;
  },

  preload(i) {
    for (const k of [i + 1, i - 1, i + 2, i - 2]) {
      if (state.frames[k]) new Image().src = state.frames[k];
    }
  },

  mark(keep, advance) {
    const i = this.index;
    if (i == null || state.busy || state.editBusy) return;
    if (state.sel.has(i) !== keep) setKept([i], keep);
    if (advance && i < this.count() - 1) this.show(i + 1);
    else {
      this.paint();
      if (advance) toast(`已是最后一张：保留 ${state.sel.size} / ${this.count()} 张。按 Esc 回到网格。`);
    }
  },

  toggle() {
    if (this.index != null) this.mark(!state.sel.has(this.index), false);
  },

  edit() {
    const card = state.cards?.[this.index];
    if (!card || card.edit.disabled) return;
    this.close();
    card.edit.click();
  },

  onKey(event) {
    if (!this.isOpen() || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target || {};
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) && target.type !== 'checkbox') return;
    // Letters follow the physical key so they also work with a Chinese input method on.
    const key = {KeyA: 'a', KeyE: 'e', KeyD: 'd', KeyX: 'x'}[event.code] || event.key.toLowerCase();
    const actions = {
      arrowleft: () => this.show(this.index - 1),
      arrowright: () => this.show(this.index + 1),
      arrowup: () => this.mark(true, true),
      arrowdown: () => this.mark(false, true),
      ' ': () => this.toggle(),
      x: () => this.toggle(),
      home: () => this.show(0),
      end: () => this.show(this.count() - 1),
      e: () => this.edit(),
      a: () => typeof Analysis !== 'undefined' && Analysis.togglePanel(),
      d: () => this.el('#vwDownload').click(),
      escape: () => this.close(),
    };
    const action = actions[key];
    if (!action) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    action();
  },
};

if (typeof document !== 'undefined' && document.querySelector('#vwStrip')) {
  // Capture phase: while the viewer is open it owns the keyboard, before the grid shortcuts.
  document.addEventListener('keydown', event => Viewer.onKey(event), true);
  const on = (selector, handler) => document.querySelector(selector).addEventListener('click', handler);
  document.querySelector('#vwImg').addEventListener('load', () => Viewer.fit());
  window.addEventListener('resize', () => {
    if (Viewer.isOpen()) Viewer.fit();
  });
  on('#vwPrev', () => Viewer.show(Viewer.index - 1));
  on('#vwNext', () => Viewer.show(Viewer.index + 1));
  on('#vwKeep', () => Viewer.mark(true, true));
  on('#vwSkip', () => Viewer.mark(false, true));
  on('#vwEdit', () => Viewer.edit());
  on('#lbClose', () => Viewer.close());
  if (typeof Confirm !== 'undefined')
    Confirm.guardLink(document.querySelector('#vwDownload'), () => ({
      kind: 'download',
      title: '下载这张原图？',
      message: `${document.querySelector('#vwMeta').textContent}，原始分辨率 JPEG，交给浏览器保存。`,
      ok: '下载',
    }));
  // Swipe left / right on touch screens.
  const stage = document.querySelector('#vwStage');
  stage.addEventListener('pointerdown', event => {
    Viewer.touchX = event.pointerType === 'mouse' ? null : event.clientX;
  });
  stage.addEventListener('pointerup', event => {
    if (Viewer.touchX == null) return;
    const dx = event.clientX - Viewer.touchX;
    Viewer.touchX = null;
    if (Math.abs(dx) > 60) Viewer.show(Viewer.index + (dx < 0 ? 1 : -1));
  });
}

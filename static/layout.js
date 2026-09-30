/* Function navigator, draggable dividers, collapsible workspace and empty results state for the
   two-column layout. It only reads the page (and the global `state` from app.js); it never changes
   analysis or results. */
const LAYOUT_NAV_KEY = 'keyframe-tool.nav.collapsed';
const LAYOUT_SIZES_KEY = 'keyframe-tool.layout.sizes';
const LAYOUT_WIDE = '(min-width: 1100px)';
const LAYOUT_SPLIT_GAP = 14; // width of a column divider, --splitw in layout.css
const LAYOUT_NAV_FOLD = 120; // dragging the navigator narrower than this folds it to icons
// Each divider: the navigator width, or the left column of a two-column page. `room` is what the
// right column always keeps.
const LAYOUT_SPLITS = {
  nav: {splitter: '#navSplitter', min: 160, max: 360},
  main: {splitter: '#mainSplitter', columns: '.appColumns', min: 320, room: 420},
  storage: {splitter: '#storageSplitter', columns: '#storageColumns', min: 260, room: 420},
  collage: {splitter: '#collageSplitter', columns: '#collageColumns', min: 260, room: 420},
};
// Views that cover the two main columns, keyed like their navigator entries.
const LAYOUT_VIEWS = {
  storage: {view: '#storageManager', close: '#storageClose'},
  collage: {view: '#collagePanel', close: '#collageClose'},
};

// Width a divider dragged to `x` gives, kept between `min` and `max`. Pure, so it can be tested.
function layoutSplitWidth(x, start, min, max) {
  return Math.round(Math.max(min, Math.min(Math.max(min, max), x - start)));
}

// What each navigator entry shows for a snapshot of the page. Pure, so it can be tested on its own.
function layoutNavState(page) {
  const off = reason => ({disabled: true, reason, text: '', live: false});
  const on = (text = '', live = false) => ({disabled: false, reason: '', text, live});
  return {
    workspace: on(page.workspaceCount ? `${page.workspaceCount} 条记录` : ''),
    upload: on(page.videoName || '尚未选择'),
    params: page.paramsVisible ? on(page.sens) : off('先选择视频'),
    progress: page.running
      ? on(`分析中 ${page.progress}%`, true)
      : page.progressVisible
        ? on(page.progressText)
        : off('当前没有进行中的分析'),
    editor: page.editorVisible ? on() : off('分析完成后可用'),
    results: page.resultVisible ? on(`${page.total} 张 · 选中 ${page.selected}`) : off('还没有结果'),
    download: !page.resultVisible
      ? off('还没有结果')
      : page.downloadEnabled
        ? on(`${page.selected} 张`)
        : off('请先选中要下载的关键帧'),
    collage: page.resultVisible ? on() : off('还没有结果'),
    storage: on(),
  };
}

const Layout = {
  lastSid: null,
  sizes: {},
  el(selector) {
    return document.querySelector(selector);
  },
  shown(selector) {
    const el = this.el(selector);
    return !!el && !el.hidden;
  },
  wide() {
    return window.matchMedia(LAYOUT_WIDE).matches;
  },

  snapshot() {
    const text = selector => (this.el(selector)?.textContent || '').trim();
    const width = parseFloat(this.el('#bar')?.style.width) || 0;
    return {
      videoName: typeof state !== 'undefined' && state.sid ? text('#mName') : '',
      workspaceCount:
        this.el('#workspaceList')?.hidden === false ? this.el('#workspaceList').querySelectorAll('.toolbar').length : 0,
      paramsVisible: this.shown('#cardParams'),
      sens: text('#sensVal'),
      progressVisible: this.shown('#cardProgress'),
      running: typeof state !== 'undefined' && !!state.jobRunning && this.shown('#cardProgress'),
      progress: Math.round(width),
      progressText: text('#stage'),
      editorVisible: this.shown('#cardEditor'),
      resultVisible: this.shown('#cardResult'),
      total: text('#cntAll') || '0',
      selected: text('#cntSel') || '0',
      downloadEnabled: this.shown('#cardResult') && !this.el('#btnDownload').disabled,
    };
  },

  render() {
    const page = this.snapshot();
    for (const [key, item] of Object.entries(layoutNavState(page))) {
      const button = this.el(`[data-nav="${key}"]`);
      if (!button) continue;
      button.disabled = item.disabled;
      button.classList.toggle('live', item.live);
      const label = button.querySelector('.navLabel').textContent;
      button.title = item.reason ? `${label}（${item.reason}）` : [label, item.text].filter(Boolean).join(' · ');
      this.el(`#navStatus-${key}`).textContent = item.disabled ? item.reason : item.text;
    }
    this.el('#resultsEmpty').hidden = page.resultVisible || page.progressVisible;
    this.el('#cardUpload').classList.toggle('compact', !!page.videoName);
    const file = this.el('#headerFile');
    if (file.dataset.name !== page.videoName) {
      file.dataset.name = page.videoName;
      file.hidden = !page.videoName;
      file.innerHTML = '';
      if (page.videoName) {
        const name = document.createElement('b');
        name.textContent = page.videoName;
        file.append('当前视频：', name);
        file.title = page.videoName;
      }
    }
    for (const [key, {view}] of Object.entries(LAYOUT_VIEWS)) {
      const button = this.el(`[data-nav="${key}"]`);
      const open = this.shown(view);
      button?.classList.toggle('active', open);
      if (open) button?.setAttribute('aria-current', 'page');
      else button?.removeAttribute('aria-current');
    }
    // Once a video is open the workspace list is rarely needed: fold it away, once per video.
    const sid = typeof state !== 'undefined' ? state.sid : null;
    if (sid && sid !== this.lastSid) this.setWorkspace(false);
    this.lastSid = sid;
  },

  setWorkspace(open) {
    this.el('#cardWorkspace').classList.toggle('collapsed', !open);
    this.el('#workspaceToggle').setAttribute('aria-expanded', String(open));
  },

  reveal(selector) {
    const target = this.el(selector);
    if (!target || target.hidden) return;
    target.scrollIntoView({behavior: 'smooth', block: 'start'});
    target.classList.remove('flash');
    void target.offsetWidth; // restart the highlight animation
    target.classList.add('flash');
  },

  navigate(key) {
    const sections = {
      upload: '#cardUpload',
      params: '#cardParams',
      progress: '#cardProgress',
      editor: '#cardEditor',
      results: '#cardResult',
    };
    const actions = {download: '#btnDownload', collage: '#btnCollage', storage: '#btnStorage'};
    this.closeDrawer();
    if (LAYOUT_VIEWS[key] && this.shown(LAYOUT_VIEWS[key].view)) return; // already there
    if (!this.closeViews()) return;
    if (key === 'workspace') {
      this.setWorkspace(true);
      this.reveal('#cardWorkspace');
    } else if (sections[key]) this.reveal(sections[key]);
    else if (actions[key]) this.el(actions[key]).click();
    if (key === 'upload') this.el('#drop').focus({preventScroll: true});
  },

  // Leave the storage manager or the collage for the main page; false when a view cannot close yet.
  closeViews() {
    for (const {view, close} of Object.values(LAYOUT_VIEWS)) {
      if (!this.shown(view)) continue;
      this.el(close).click();
      if (this.shown(view)) {
        if (typeof toast === 'function') toast('当前页面还有操作在进行，请稍候再切换');
        return false;
      }
    }
    return true;
  },

  // ------------------------------------------------------------------ dividers
  loadSizes() {
    try {
      const saved = JSON.parse(localStorage.getItem(LAYOUT_SIZES_KEY) || '{}');
      this.sizes = saved && typeof saved === 'object' ? saved : {};
    } catch {
      this.sizes = {};
    }
    for (const key of Object.keys(LAYOUT_SPLITS)) this.applySize(key);
  },
  saveSizes() {
    try {
      localStorage.setItem(LAYOUT_SIZES_KEY, JSON.stringify(this.sizes));
    } catch {
      /* 仅本次有效 */
    }
  },
  // The element that carries the width variable of a divider.
  sizeTarget(key) {
    return key === 'nav' ? document.documentElement : this.el(LAYOUT_SPLITS[key].columns);
  },
  applySize(key) {
    const target = this.sizeTarget(key);
    const width = this.sizes[key];
    if (!target) return;
    const name = key === 'nav' ? '--navw' : '--leftw';
    if (Number.isFinite(width)) target.style.setProperty(name, `${width}px`);
    else target.style.removeProperty(name);
  },
  // Current width of what a divider resizes.
  currentWidth(key) {
    if (key === 'nav') return document.body.classList.contains('navCollapsed') ? 60 : this.el('#appNav').offsetWidth;
    return this.el(LAYOUT_SPLITS[key].splitter).previousElementSibling.getBoundingClientRect().width;
  },
  // Resize to a pointer position (or, with `absolute`, straight to a width).
  resizeTo(key, x, absolute = false) {
    const split = LAYOUT_SPLITS[key];
    if (key === 'nav') {
      if (x < LAYOUT_NAV_FOLD) {
        this.setCollapsed(true, true);
        return;
      }
      if (document.body.classList.contains('navCollapsed')) this.setCollapsed(false, true);
      this.sizes.nav = layoutSplitWidth(x, 0, split.min, split.max);
    } else {
      const columns = this.el(split.columns);
      const box = columns.getBoundingClientRect();
      const style = getComputedStyle(columns);
      const left = box.left + parseFloat(style.paddingLeft);
      const inner = columns.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const start = absolute ? 0 : left + LAYOUT_SPLIT_GAP / 2;
      this.sizes[key] = layoutSplitWidth(x, start, split.min, inner - LAYOUT_SPLIT_GAP - split.room);
    }
    this.applySize(key);
  },
  resetSize(key) {
    delete this.sizes[key];
    this.applySize(key);
    if (key === 'nav') this.setCollapsed(false, true);
    this.resized();
  },
  // Pictures sized for the old width (collage preview, full-screen viewer) redraw on resize.
  resized() {
    this.saveSizes();
    window.dispatchEvent(new Event('resize'));
  },
  initSplitter(key) {
    const splitter = this.el(LAYOUT_SPLITS[key].splitter);
    if (!splitter) return;
    splitter.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      splitter.setPointerCapture?.(event.pointerId);
      splitter.classList.add('dragging');
      document.body.classList.add('resizing');
      const before = this.sizes[key];
      const move = moveEvent => this.resizeTo(key, moveEvent.clientX);
      const stop = () => {
        splitter.removeEventListener('pointermove', move);
        splitter.removeEventListener('pointerup', stop);
        splitter.removeEventListener('pointercancel', stop);
        splitter.classList.remove('dragging');
        document.body.classList.remove('resizing');
        // Folding the navigator keeps the width it had, for when it opens again.
        if (key === 'nav' && document.body.classList.contains('navCollapsed')) {
          if (before === undefined) delete this.sizes.nav;
          else this.sizes.nav = before;
          this.applySize('nav');
        }
        this.resized();
      };
      splitter.addEventListener('pointermove', move);
      splitter.addEventListener('pointerup', stop);
      splitter.addEventListener('pointercancel', stop);
    });
    splitter.addEventListener('dblclick', () => this.resetSize(key));
    splitter.addEventListener('keydown', event => {
      const step = event.shiftKey ? 64 : 16;
      const delta = {ArrowLeft: -step, ArrowRight: step}[event.key];
      if (event.key === 'Enter') this.resetSize(key);
      else if (delta) {
        // A folded navigator opens again with the first step to the right.
        const folded = key === 'nav' && document.body.classList.contains('navCollapsed');
        this.resizeTo(key, (folded && delta > 0 ? LAYOUT_NAV_FOLD : this.currentWidth(key)) + delta, true);
        this.resized();
      } else return;
      event.preventDefault();
      event.stopPropagation();
    });
  },

  setCollapsed(collapsed, remember) {
    document.body.classList.toggle('navCollapsed', collapsed);
    this.el('#navToggle').setAttribute('aria-expanded', String(!collapsed));
    if (remember) {
      // The page got wider or narrower: let pictures sized for it redraw once the slide is over.
      setTimeout(() => window.dispatchEvent(new Event('resize')), 220);
      try {
        localStorage.setItem(LAYOUT_NAV_KEY, collapsed ? '1' : '0');
      } catch {
        /* 仅本次有效 */
      }
    }
  },
  openDrawer() {
    document.body.classList.add('navOpen');
    this.el('#navScrim').hidden = false;
    this.el('#navFab').setAttribute('aria-expanded', 'true');
  },
  closeDrawer() {
    document.body.classList.remove('navOpen');
    this.el('#navScrim').hidden = true;
    this.el('#navFab').setAttribute('aria-expanded', 'false');
  },

  init() {
    let saved = null;
    try {
      saved = localStorage.getItem(LAYOUT_NAV_KEY);
    } catch {
      /* 默认展开 */
    }
    // First visit: show labels when there is room for them, icons only on smaller screens.
    this.setCollapsed(saved === null ? window.innerWidth < 1400 : saved === '1', false);
    this.loadSizes();
    for (const key of Object.keys(LAYOUT_SPLITS)) this.initSplitter(key);
    this.el('#navToggle').addEventListener('click', () => {
      if (this.wide()) this.setCollapsed(!document.body.classList.contains('navCollapsed'), true);
      else this.closeDrawer();
    });
    this.el('#navFab').addEventListener('click', () => this.openDrawer());
    this.el('#navScrim').addEventListener('click', () => this.closeDrawer());
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') this.closeDrawer();
    });
    for (const button of document.querySelectorAll('[data-nav]')) {
      button.addEventListener('click', () => this.navigate(button.dataset.nav));
    }
    this.el('#workspaceToggle').addEventListener('click', () =>
      this.setWorkspace(this.el('#cardWorkspace').classList.contains('collapsed')),
    );
    this.render();
    setInterval(() => {
      if (!document.hidden) this.render();
    }, 500);
  },
};

if (typeof document !== 'undefined' && document.querySelector('#appNav')) Layout.init();

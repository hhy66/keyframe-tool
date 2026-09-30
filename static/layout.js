/* Function navigator, collapsible workspace and empty results state for the two-column layout.
   It only reads the page (and the global `state` from app.js); it never changes analysis or results. */
const LAYOUT_NAV_KEY = 'keyframe-tool.nav.collapsed';
const LAYOUT_WIDE = '(min-width: 1100px)';

// What each navigator entry shows for a snapshot of the page. Pure, so it can be tested on its own.
function layoutNavState(page) {
  const off = reason => ({disabled: true, reason, text: '', live: false});
  const on = (text = '', live = false) => ({disabled: false, reason: '', text, live});
  return {
    workspace: on(page.workspaceCount ? `${page.workspaceCount} 条记录` : ''),
    upload: on(page.videoName || '尚未选择'),
    params: page.paramsVisible ? on(page.sens) : off('先选择视频'),
    progress: page.running ? on(`分析中 ${page.progress}%`, true) : page.progressVisible ? on(page.progressText) : off('当前没有进行中的分析'),
    editor: page.editorVisible ? on() : off('分析完成后可用'),
    results: page.resultVisible ? on(`${page.total} 张 · 选中 ${page.selected}`) : off('还没有结果'),
    download: !page.resultVisible ? off('还没有结果') : page.downloadEnabled ? on(`${page.selected} 张`) : off('请先选中要下载的关键帧'),
    collage: page.resultVisible ? on() : off('还没有结果'),
    storage: on(),
  };
}

const Layout = {
  lastSid: null,
  el(selector) { return document.querySelector(selector); },
  shown(selector) { const el = this.el(selector); return !!el && !el.hidden; },
  wide() { return window.matchMedia(LAYOUT_WIDE).matches; },

  snapshot() {
    const text = selector => (this.el(selector)?.textContent || '').trim();
    const width = parseFloat(this.el('#bar')?.style.width) || 0;
    return {
      videoName: typeof state !== 'undefined' && state.sid ? text('#mName') : '',
      workspaceCount: this.el('#workspaceList')?.hidden === false ? this.el('#workspaceList').querySelectorAll('.toolbar').length : 0,
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
    void target.offsetWidth;  // restart the highlight animation
    target.classList.add('flash');
  },

  navigate(key) {
    const sections = {upload: '#cardUpload', params: '#cardParams', progress: '#cardProgress',
      editor: '#cardEditor', results: '#cardResult'};
    const actions = {download: '#btnDownload', collage: '#btnCollage', storage: '#btnStorage'};
    this.closeDrawer();
    if (key === 'workspace') { this.setWorkspace(true); this.reveal('#cardWorkspace'); }
    else if (sections[key]) this.reveal(sections[key]);
    else if (actions[key]) this.el(actions[key]).click();
    if (key === 'upload') this.el('#drop').focus({preventScroll: true});
  },

  setCollapsed(collapsed, remember) {
    document.body.classList.toggle('navCollapsed', collapsed);
    this.el('#navToggle').setAttribute('aria-expanded', String(!collapsed));
    if (remember) {
      try { localStorage.setItem(LAYOUT_NAV_KEY, collapsed ? '1' : '0'); } catch { /* 仅本次有效 */ }
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
    try { saved = localStorage.getItem(LAYOUT_NAV_KEY); } catch { /* 默认展开 */ }
    // First visit: show labels when there is room for them, icons only on smaller screens.
    this.setCollapsed(saved === null ? window.innerWidth < 1400 : saved === '1', false);
    this.el('#navToggle').addEventListener('click', () => {
      if (this.wide()) this.setCollapsed(!document.body.classList.contains('navCollapsed'), true);
      else this.closeDrawer();
    });
    this.el('#navFab').addEventListener('click', () => this.openDrawer());
    this.el('#navScrim').addEventListener('click', () => this.closeDrawer());
    document.addEventListener('keydown', event => { if (event.key === 'Escape') this.closeDrawer(); });
    for (const button of document.querySelectorAll('[data-nav]')) {
      button.addEventListener('click', () => this.navigate(button.dataset.nav));
    }
    this.el('#workspaceToggle').addEventListener('click', () =>
      this.setWorkspace(this.el('#cardWorkspace').classList.contains('collapsed')));
    this.render();
    setInterval(() => { if (!document.hidden) this.render(); }, 500);
  },
};

if (typeof document !== 'undefined' && document.querySelector('#appNav')) Layout.init();

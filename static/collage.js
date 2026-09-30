/* A collage is a local, independent draft. Source screenshot files are never edited. */
const COLLAGE_PRESETS = {
  card: {gap: 'medium', radius: 'medium', background: 'light', shadow: true},
  clean: {gap: 'none', radius: 'none', background: 'white', shadow: false},
  white: {gap: 'small', radius: 'small', background: 'white', shadow: false},
  gallery: {gap: 'medium', radius: 'small', background: 'dark', shadow: false},
};
const COLLAGE_RATIOS = [['自由', NaN], ['原画面', 0], ['16:9', 16 / 9], ['4:3', 4 / 3], ['1:1', 1], ['3:4', 3 / 4], ['9:16', 9 / 16]];
const COLLAGE_PAGE_SIZES = [4, 9, 12, 16];
const COLLAGE_NOTE_LIMIT = 60;
const COLLAGE_SETTINGS_KEY = 'keyframe-tool.collage.settings.v1';
// Remembered settings: selector, property, allowed values (null = a whole number) and default.
const COLLAGE_SETTINGS = [
  ['#collageMode', 'value', ['share', 'custom', 'original'], 'share'],
  ['#collageWidth', 'value', null, '3000'],
  ['#collageLayout', 'value', ['justified', 'grid'], 'justified'],
  ['#collagePerPage', 'value', ['4', '9', '12', '16'], '9'],
  ['#collageCanvas', 'value', ['auto', 'landscape', 'square', 'portrait'], 'auto'],
  ['#collageShape', 'value', ['source', 'landscape', 'square', 'portrait'], 'source'],
  ['#collageFit', 'value', ['cover', 'contain'], 'cover'],
  ['#collageFormat', 'value', ['png', 'jpeg'], 'png'],
  ['#collageSplit', 'checked', [true, false], true],
  ['#collagePreset', 'value', ['card', 'white', 'gallery', 'clean', 'custom'], 'card'],
  ['#collageGap', 'value', ['none', 'small', 'medium', 'large'], 'medium'],
  ['#collageRadius', 'value', ['none', 'small', 'medium', 'large'], 'medium'],
  ['#collageBackground', 'value', ['white', 'light', 'dark', 'black'], 'light'],
  ['#collageShadow', 'checked', [true, false], true],
  ['#collageOrderLabel', 'checked', [true, false], false],
  ['#collageIndex', 'checked', [true, false], false],
  ['#collageTime', 'checked', [true, false], false],
  ['#collageLabelPosition', 'value', ['tr', 'tl', 'br', 'bl', 'below'], 'tr'],
];

function collageStorage(action) {
  try { return action(window.localStorage); } catch { return null; }
}

const CollageUI = {
  snapshot: null, ids: [], page: 0, token: 0, busy: false, abort: null, dragId: null, plan: null, output: null,
  crops: {}, notes: {}, noteInputs: new Map(), stageView: null, focus: null, cropper: null, cropId: null, cropToken: 0,
  draftToken: 0, draftLoaded: false, touched: false, foreignNotes: {}, saveTimer: null, legacyKey: null,

  el(selector) { return document.querySelector(selector); },
  node(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  },
  label(id) { return `#${String(id + 1).padStart(3, '0')}`; },
  isOriginal() { return this.el('#collageMode').value === 'original'; },

  // ------------------------------------------------------------------ open / close
  open() {
    if (!state.sid || !state.resultRun || !state.cuts.length) { toast('请先生成或恢复关键帧结果'); return; }
    this.invalidate();
    if (this.snapshot?.sid === state.sid && this.snapshot?.run === state.resultRun) {
      this.el('#collagePanel').hidden = false;
      this.renderLists(); this.summary(); this.queuePlan();
      return;
    }
    this.snapshot = {sid: state.sid, run: state.resultRun, name: this.el('#mName').textContent || 'video',
      cuts: state.cuts.map(c => ({...c})), thumbs: [...state.thumbs], frames: [...state.frames]};
    this.ids = [...state.sel].filter(i => this.available(i)).sort((a, b) => a - b);
    this.page = 0; this.crops = {}; this.notes = {}; this.focus = null;
    this.restoreSettings();
    this.el('#collageTitleText').value = '';
    this.el('#collageSource').textContent = `${this.snapshot.name} · 基于打开面板时的关键帧结果`;
    this.el('#collagePanel').hidden = false;
    this.renderLists(); this.summary(); this.queuePlan();
    this.loadDraft(this.snapshot);
  },
  close() {
    this.flushSave();
    this.closeCrop(); this.closeResult(); this.invalidate();
    this.el('#collagePanel').hidden = true;
  },
  available(i) {
    return Number.isInteger(i) && i >= 0 && i < (this.snapshot?.cuts.length || 0)
      && this.snapshot.cuts[i].available !== false && !!this.snapshot.frames[i];
  },

  // ------------------------------------------------------------------ remembered settings
  restoreSettings() {
    const saved = collageStorage(storage => JSON.parse(storage.getItem(COLLAGE_SETTINGS_KEY) || '{}')) || {};
    for (const [selector, property, allowed, fallback] of COLLAGE_SETTINGS) {
      const value = saved[selector];
      const valid = allowed ? allowed.includes(value) : /^\d{2,5}$/.test(String(value ?? ''));
      this.el(selector)[property] = valid ? value : fallback;
    }
  },
  saveSettings() {
    const values = Object.fromEntries(COLLAGE_SETTINGS.map(([selector, property]) => [selector, this.el(selector)[property]]));
    collageStorage(storage => storage.setItem(COLLAGE_SETTINGS_KEY, JSON.stringify(values)));
  },
  // ------------------------------------------------------------------ draft kept in the workspace
  // Order, crops and notes are saved with the video on disk, keyed like the skip state, so they
  // survive a refresh, another browser, a restart and a new analysis of the same video.
  frameKey(i) {
    const cut = this.snapshot.cuts[i];
    return String(cut.source_frame ?? cut.frame_index ?? `legacy:${this.snapshot.run}:${cut.file_index ?? i}`);
  },
  draftBody() {
    const keyed = values => Object.fromEntries(Object.entries(values)
      .filter(([id]) => this.available(Number(id))).map(([id, value]) => [this.frameKey(Number(id)), value]));
    const notes = Object.fromEntries(Object.entries(keyed(this.notes)).map(([key, text]) => [key, text.trim()]).filter(([, text]) => text));
    return {ids: this.ids.map(i => this.frameKey(i)), crops: keyed(this.crops), notes: {...this.foreignNotes, ...notes}};
  },
  legacyNotes() {
    // Notes kept only in this browser by the previous version; moved into the workspace once.
    this.legacyKey = `keyframe-tool.collage.notes.${this.snapshot.sid}.${this.snapshot.run}`;
    const saved = collageStorage(storage => JSON.parse(storage.getItem(this.legacyKey) || '{}')) || {};
    return Object.fromEntries(Object.entries(saved)
      .filter(([id, text]) => this.available(Number(id)) && typeof text === 'string' && text.trim())
      .map(([id, text]) => [id, text.slice(0, COLLAGE_NOTE_LIMIT)]));
  },
  async loadDraft(snapshot) {
    const token = ++this.draftToken;
    this.draftLoaded = false; this.touched = false; this.foreignNotes = {};
    let draft = null;
    try {
      draft = (await jsonRequest(`/api/workspace/${encodeURIComponent(snapshot.sid)}/collage`)).draft;
    } catch { /* 读取失败时从当前勾选开始，稍后的修改仍会保存 */ }
    if (token !== this.draftToken || snapshot !== this.snapshot) return;
    const index = new Map(snapshot.cuts.map((_, i) => [this.frameKey(i), i]));
    const toIndex = key => index.get(String(key));
    let applied = false;
    if (draft && typeof draft === 'object') {
      // Anything changed while the draft was loading wins over the saved copy.
      for (const [key, text] of Object.entries(draft.notes || {})) {
        const i = toIndex(key);
        if (i === undefined) this.foreignNotes[key] = text;
        else if (this.notes[i] === undefined) { this.notes[i] = text; applied = true; }
      }
      for (const [key, crop] of Object.entries(draft.crops || {})) {
        const i = toIndex(key);
        if (i !== undefined && !this.crops[i]) { this.crops[i] = crop; applied = true; }
      }
      const ids = (draft.ids || []).map(toIndex).filter(i => i !== undefined && this.available(i));
      if (!this.touched && ids.length && ids.join() !== this.ids.join()) { this.ids = ids; applied = true; }
    }
    let migrated = false;
    for (const [i, text] of Object.entries(this.legacyNotes())) {
      if (!this.notes[i]) { this.notes[i] = text; migrated = true; }
    }
    this.draftLoaded = true;
    if (this.touched || migrated) this.scheduleSave(0);
    if (!applied && !migrated) return;
    this.page = 0;
    this.invalidate(); this.renderLists(); this.summary(); this.queuePlan();
  },
  scheduleSave(delay = 600) {
    clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => this.saveDraft(), delay);
  },
  flushSave() {
    if (this.saveTimer === null) return;
    clearTimeout(this.saveTimer);
    this.saveDraft();
  },
  async saveDraft() {
    this.saveTimer = null;
    // Never overwrite the saved draft with defaults before it has been read back.
    if (!this.snapshot || !this.draftLoaded) return;
    const sid = this.snapshot.sid, legacyKey = this.legacyKey;
    try {
      await jsonRequest(`/api/workspace/${encodeURIComponent(sid)}/collage`, {method: 'PUT',
        headers: {'Content-Type': 'application/json'}, keepalive: true, body: JSON.stringify({draft: this.draftBody()})});
      if (legacyKey) collageStorage(storage => storage.removeItem(legacyKey));
    } catch (e) {
      if (this.snapshot?.sid === sid) this.el('#collageStatus').textContent = '拼图草稿暂未保存到工作区：' + (e.message || '请确认工具仍在运行');
    }
  },
  setNote(id, text, source) {
    this.notes[id] = String(text).slice(0, COLLAGE_NOTE_LIMIT);
    this.touched = true; this.scheduleSave();
    for (const input of [this.noteInputs.get(id), this.focus === id ? this.el('#collageFocusNote') : null]) {
      if (input && input !== source) input.value = this.notes[id];
    }
    this.invalidate(); this.summary(); this.queuePlan(500);
  },

  // ------------------------------------------------------------------ selection and order
  toggle(i) {
    if (!this.available(i)) return;
    const position = this.ids.indexOf(i);
    if (position < 0) this.ids.push(i); else this.ids.splice(position, 1);
    this.changed();
  },
  move(id, position) {
    const from = this.ids.indexOf(id);
    if (from < 0 || !Number.isInteger(position)) return;
    this.ids.splice(from, 1);
    this.ids.splice(Math.max(0, Math.min(this.ids.length, position)), 0, id);
    this.changed();
  },
  swap(a, b) {
    const first = this.ids.indexOf(a), second = this.ids.indexOf(b);
    if (first < 0 || second < 0 || first === second) return;
    [this.ids[first], this.ids[second]] = [this.ids[second], this.ids[first]];
    this.touched = true; this.scheduleSave();
    this.invalidate(); this.renderLists(); this.summary(); this.queuePlan();
  },
  changed() {
    this.touched = true; this.scheduleSave();
    this.page = 0; this.invalidate(); this.renderLists(); this.summary(); this.queuePlan();
  },
  select(id) {
    this.focus = id;
    for (const [key, input] of this.noteInputs) input.closest?.('.collageOrderRow')?.classList.toggle('focused', key === id);
    this.stageView?.select(id, true);
    this.summary();
  },
  applyPreset(name) {
    const preset = COLLAGE_PRESETS[name];
    if (!preset) return;
    this.el('#collageGap').value = preset.gap;
    this.el('#collageRadius').value = preset.radius;
    this.el('#collageBackground').value = preset.background;
    this.el('#collageShadow').checked = preset.shadow;
  },

  // ------------------------------------------------------------------ request payloads
  pages(ids, perPage, split) {
    if (!COLLAGE_PAGE_SIZES.includes(perPage)) throw new Error('请选择每张最多 4、9、12 或 16 张');
    if (!ids.length) throw new Error('请至少选择一张参考图');
    if (ids.length > perPage && !split) {
      throw new Error(`已选 ${ids.length} 张，超过每张 ${perPage} 张；请减少选图或开启“超出张数时自动分成多张”。`);
    }
    const pages = [];
    for (let i = 0; i < ids.length; i += perPage) pages.push(ids.slice(i, i + perPage));
    return pages;
  },
  options() {
    const original = this.isOriginal();
    const checked = selector => !original && this.el(selector).checked;
    const notes = Object.fromEntries(Object.entries(this.notes).map(([id, text]) => [id, text.trim()]).filter(([, text]) => text));
    return {
      per_page: +this.el('#collagePerPage').value, mode: this.el('#collageMode').value,
      layout: original ? 'grid' : this.el('#collageLayout').value, canvas: this.el('#collageCanvas').value,
      shape: original ? 'source' : this.el('#collageShape').value, fit: original ? 'contain' : this.el('#collageFit').value,
      width: +this.el('#collageWidth').value, background: this.el('#collageBackground').value,
      gap: original ? 'none' : this.el('#collageGap').value, radius: original ? 'none' : this.el('#collageRadius').value,
      shadow: checked('#collageShadow'), title: original ? '' : this.el('#collageTitleText').value.trim(),
      format: this.el('#collageFormat').value, order: checked('#collageOrderLabel'), index: checked('#collageIndex'),
      time: checked('#collageTime'), label_position: this.el('#collageLabelPosition').value,
      split: this.el('#collageSplit').checked, crops: original ? {} : this.crops, notes: original ? {} : notes,
    };
  },
  only(values, ids) { return Object.fromEntries(Object.entries(values).filter(([id]) => ids.includes(Number(id)))); },
  payload() {
    const options = this.options(), pages = this.pages(this.ids, options.per_page, options.split);
    this.page = Math.max(0, Math.min(this.page, pages.length - 1));
    const ids = pages[this.page];
    return {...options, ids, run: this.snapshot.run, page: this.page + 1,
      crops: this.only(options.crops, ids), notes: this.only(options.notes, ids)};
  },
  batchPayload() {
    const options = this.options();
    this.pages(this.ids, options.per_page, true);
    return {...options, ids: [...this.ids], run: this.snapshot.run, page: 1,
      crops: this.only(options.crops, this.ids), notes: this.only(options.notes, this.ids)};
  },

  // ------------------------------------------------------------------ status of the panel
  summary() {
    const original = this.isOriginal();
    const justified = !original && this.el('#collageLayout').value === 'justified';
    const noGap = this.el('#collageGap').value === 'none';
    this.el('#collageLayout').disabled = original;
    this.el('#collageCanvasField').hidden = !justified;
    this.el('#collageShapeField').hidden = justified || original;
    this.el('#collageFitField').hidden = justified || original;
    this.el('#collageWidthField').hidden = this.el('#collageMode').value !== 'custom';
    this.el('#collageWidth').disabled = this.el('#collageMode').value !== 'custom';
    for (const selector of ['#collagePreset', '#collageGap', '#collageShadow', '#collageTitleText',
      '#collageOrderLabel', '#collageIndex', '#collageTime', '#collageLabelPosition']) this.el(selector).disabled = original;
    this.el('#collageRadius').disabled = original || noGap;
    this.el('#collageRadiusNote').hidden = original || !noGap;
    this.el('#collageModeNote').textContent = original
      ? '保留原图细节：按网格原尺寸无缝拼接，不缩放、不裁剪，也不加间距、圆角、标题、标注或备注。'
      : justified ? '智能排版：按每张画面（含裁剪后）的比例自动分行，每行铺满宽度，不裁剪也不留白。'
        : '固定网格：每格同样大小。“完整显示”会在格内留白，“裁剪铺满”会居中裁掉多余部分，可双击画面调整裁剪。';

    const focused = this.focus !== null && this.ids.includes(this.focus);
    this.el('#collageFocusBar').hidden = !focused;
    if (focused) {
      const cut = this.snapshot.cuts[this.focus];
      this.el('#collageFocusName').textContent = `已选 ${this.label(this.focus)} ${cut.label || ''}`;
      this.el('#collageCropSelected').disabled = original || this.busy;
      this.el('#collageResetCrop').disabled = original || this.busy || !this.crops[this.focus];
      const note = this.el('#collageFocusNote');
      note.disabled = original;
      if (document.activeElement !== note) note.value = this.notes[this.focus] || '';
    }
    const hasIds = !!this.snapshot && this.ids.length > 0;
    this.el('#collageDownloadAll').disabled = this.busy || !hasIds;
    this.el('#collageCsv').disabled = this.busy || !hasIds;
    this.el('#collageShowResult').disabled = !this.output;
    try {
      const options = this.options(), pages = this.pages(this.ids, options.per_page, options.split);
      this.page = Math.max(0, Math.min(this.page, pages.length - 1));
      this.el('#collageSummary').className = 'collageNotice';
      this.el('#collageSummary').textContent = `共选 ${this.ids.length} 张，分成 ${pages.length} 张拼图；当前第 ${this.page + 1} 张使用 ${pages[this.page].length} 张。`
        + (pages.length > 1 ? '点右上角“下载全部拼图”可一次打包。' : '');
      this.el('#collagePage').textContent = `第 ${this.page + 1} / ${pages.length} 张`;
      this.el('#collagePrev').disabled = this.page === 0 || this.busy;
      this.el('#collageNext').disabled = this.page === pages.length - 1 || this.busy;
      this.el('#collageRender').disabled = this.busy || !this.plan || !this.plan.can_render;
    } catch (e) {
      this.el('#collageSummary').className = 'collageNotice error';
      this.el('#collageSummary').textContent = e.message;
      this.el('#collagePage').textContent = '';
      this.el('#collageDimensions').textContent = '';
      this.el('#collagePrev').disabled = this.el('#collageNext').disabled = this.el('#collageRender').disabled = true;
    }
  },

  // ------------------------------------------------------------------ plan and interactive preview
  discardOutput(output = this.output) {
    if (output?.token && this.snapshot) {
      jsonRequest(`/api/collage/${encodeURIComponent(output.sid || this.snapshot.sid)}/${encodeURIComponent(output.token)}`,
        {method: 'DELETE', keepalive: true}).catch(() => {});
    }
    if (output === this.output) this.output = null;
  },
  invalidate() {
    this.token++;
    clearTimeout(this.planTimer);
    if (this.abort) this.abort();
    this.plan = null;
    this.discardOutput();
    this.el('#collagePreview').textContent = '设置已更新，请重新生成成品。';
    this.el('#collageDownload').disabled = true;
    this.el('#collageDownload').setAttribute('aria-disabled', 'true');
    this.el('#collageActual').disabled = true;
    this.el('#collageFitView').disabled = true;
    this.el('#collageShowResult').disabled = true;
  },
  queuePlan(delay = 120) {
    clearTimeout(this.planTimer);
    this.planTimer = setTimeout(() => this.refreshPlan(), delay);
  },
  async refreshPlan() {
    if (!this.snapshot) return;
    let payload;
    try { payload = this.payload(); } catch { this.stageView?.destroy(); this.summary(); return; }
    const token = this.token, snapshot = this.snapshot;
    this.el('#collageDimensions').textContent = '正在计算排版…';
    try {
      const plan = await jsonRequest(`/api/collage/${encodeURIComponent(snapshot.sid)}/plan`,
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      if (token !== this.token) return;
      this.plan = plan;
      const layout = plan.layout === 'justified' ? `智能排版 ${plan.rows} 行` : `每格 ${plan.cell_width} × ${plan.image_height}`;
      this.el('#collageDimensions').textContent = `成品 ${plan.width} × ${plan.height} 像素 · ${layout} · 约 ${Math.ceil(plan.estimated_memory_bytes / 1048576)} MB 内存。`
        + (plan.warning || '');
      this.el('#collageStatus').textContent = plan.can_render ? '' : (plan.warning || '尺寸超过处理限制，请降低宽度或减少每张张数');
      this.summary();
      if (typeof CollageStage !== 'undefined') {
        if (!this.stageView) {
          this.stageView = new CollageStage(this.el('#collageStage'), {
            onSwap: (a, b) => this.swap(a, b), onSelect: id => this.select(id), onEdit: id => this.openCrop(id)});
        }
        this.stageView.focus = this.focus;
        await this.stageView.show(plan);
      }
    } catch (e) {
      if (token === this.token) { this.plan = null; this.el('#collageStatus').textContent = e.message || '预览失败'; this.summary(); }
    }
  },
  changePage(delta) {
    if (this.busy) return;
    this.page += delta;
    this.invalidate(); this.summary(); this.queuePlan();
  },
  settingChanged(selector) {
    if (selector === '#collagePreset') this.applyPreset(this.el('#collagePreset').value);
    if (['#collageGap', '#collageRadius', '#collageBackground', '#collageShadow'].includes(selector)) this.el('#collagePreset').value = 'custom';
    if (this.el('#collageMode').value === 'share') this.el('#collageWidth').value = '3000';
    if (selector === '#collageMode') this.renderLists();
    this.saveSettings();
    this.page = 0;
    this.invalidate(); this.summary(); this.queuePlan(selector === '#collageTitleText' ? 400 : 120);
  },

  // ------------------------------------------------------------------ cropping (Cropper.js)
  setCrop(id, crop) {
    if (crop) this.crops[id] = crop; else delete this.crops[id];
    this.touched = true; this.scheduleSave();
    this.focus = id;
    this.invalidate(); this.renderLists(); this.summary(); this.queuePlan();
  },
  cropRatio(id) {
    const options = this.options();
    if (options.layout !== 'grid' || options.fit !== 'cover') return NaN;
    const cell = this.plan?.items?.find(i => i.id === id)?.cell;
    return cell ? cell.width / cell.height : NaN;
  },
  async openCrop(id) {
    if (!this.snapshot || !this.available(id) || !this.ids.includes(id)) return;
    if (this.isOriginal()) { toast('“保留原图细节”模式不缩放也不裁剪；要裁剪请把输出改为“适合分享”或“自定义宽度”。'); return; }
    if (typeof Cropper === 'undefined') { toast('裁剪组件未加载，请刷新页面'); return; }
    this.closeCrop();
    const locked = this.cropRatio(id), image = this.el('#cropImage'), token = ++this.cropToken;
    this.cropId = id;
    this.focus = id;
    this.el('#cropTitle').textContent = `裁剪原图 ${this.label(id)}`;
    this.el('#cropInfo').textContent = '正在读取原图…';
    this.el('#cropDialog').hidden = false;
    const ratios = this.el('#cropRatios');
    ratios.innerHTML = '';
    if (Number.isFinite(locked)) {
      ratios.appendChild(this.node('span', '固定网格 + 裁剪铺满：裁剪框已锁定为格子比例', 'hint'));
    } else {
      for (const [text, ratio] of COLLAGE_RATIOS) {
        const button = this.node('button', text, 'btn ghost small');
        button.addEventListener('click', () => {
          if (!this.cropper) return;
          const data = this.cropper.getImageData();
          this.cropper.setAspectRatio(ratio === 0 ? data.naturalWidth / data.naturalHeight : ratio);
        });
        ratios.appendChild(button);
      }
    }
    image.src = this.snapshot.frames[id];
    try { await image.decode(); } catch {
      if (token === this.cropToken) this.el('#cropInfo').textContent = '原图读取失败，请确认截图仍然存在';
      return;
    }
    if (token !== this.cropToken) return;
    this.cropper = new Cropper(image, {
      viewMode: 1, dragMode: 'move', autoCropArea: 1, aspectRatio: locked, checkOrientation: false,
      rotatable: false, scalable: false, zoomOnWheel: true, wheelZoomRatio: .1, toggleDragModeOnDblclick: false, restore: false,
      ready: () => {
        const crop = this.crops[id];
        if (!crop || token !== this.cropToken) return;
        const data = this.cropper.getImageData();
        this.cropper.setData({x: crop.x * data.naturalWidth, y: crop.y * data.naturalHeight,
          width: crop.width * data.naturalWidth, height: crop.height * data.naturalHeight});
      },
      crop: event => {
        this.el('#cropInfo').textContent = `保留区域 ${Math.round(event.detail.width)} × ${Math.round(event.detail.height)} 像素。拖动画面移动位置，滚轮或按钮缩放。`;
      },
    });
  },
  closeCrop() {
    this.cropToken++;
    if (this.cropper) { this.cropper.destroy(); this.cropper = null; }
    this.cropId = null;
    this.el('#cropDialog').hidden = true;
  },
  applyCrop() {
    if (!this.cropper || this.cropId === null) return;
    const data = this.cropper.getData(), image = this.cropper.getImageData(), id = this.cropId;
    const w = image.naturalWidth, h = image.naturalHeight, clamp = v => Math.max(0, Math.min(1, v));
    const x = clamp(data.x / w), y = clamp(data.y / h);
    const crop = {x, y, width: Math.min(1 - x, data.width / w), height: Math.min(1 - y, data.height / h)};
    this.closeCrop();
    const whole = crop.x < .002 && crop.y < .002 && crop.width > .996 && crop.height > .996;
    this.setCrop(id, whole || crop.width <= 0 || crop.height <= 0 ? null : crop);
  },

  // ------------------------------------------------------------------ side lists
  renderLists() {
    const picker = this.el('#collagePicker'), order = this.el('#collageOrder');
    picker.innerHTML = ''; order.innerHTML = '';
    this.noteInputs = new Map();
    if (!this.snapshot) return;
    const original = this.isOriginal();
    this.snapshot.cuts.forEach((cut, id) => {
      const label = this.node('label', undefined, 'collageChoice' + (this.ids.includes(id) ? ' selected' : ''));
      const check = this.node('input');
      check.type = 'checkbox'; check.checked = this.ids.includes(id); check.disabled = !this.available(id);
      check.setAttribute('aria-label', `拼图选用第 ${id + 1} 张`);
      check.addEventListener('change', () => this.toggle(id));
      const img = this.node('img');
      img.src = this.snapshot.thumbs[id]; img.alt = cut.label || '时间未知'; img.loading = 'lazy';
      label.append(check, img, this.node('span', `${this.label(id)} ${cut.label || '时间未知'}`));
      picker.appendChild(label);
    });
    if (!this.ids.length) order.textContent = '尚未选择图片。';
    this.ids.forEach((id, index) => order.appendChild(this.orderRow(id, index, original)));
  },
  orderRow(id, index, original) {
    const row = this.node('div', undefined, 'collageOrderRow' + (this.focus === id ? ' focused' : ''));
    row.draggable = true;
    row.addEventListener('dragstart', e => {
      this.dragId = id; e.dataTransfer.setData('text/plain', String(id)); e.dataTransfer.effectAllowed = 'move'; row.classList.add('dragging');
    });
    row.addEventListener('dragend', () => { this.dragId = null; row.classList.remove('dragging'); });
    row.addEventListener('dragover', e => { if (this.dragId !== null) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; } });
    row.addEventListener('drop', e => {
      e.preventDefault();
      if (this.dragId !== null) { const dragged = this.dragId; this.dragId = null; this.move(dragged, index); }
    });
    const image = this.node('img');
    image.src = this.snapshot.thumbs[id]; image.alt = ''; image.draggable = false;
    image.addEventListener('click', () => this.select(id));
    const info = this.node('div', undefined, 'collageOrderInfo');
    info.append(this.node('span', `${index + 1}. 原图 ${this.label(id)}`, 'collageOrderName'),
      this.node('span', (this.snapshot.cuts[id].label || '时间未知') + (this.crops[id] ? ' · 已裁剪' : ''), 'collageOrderMeta'));
    const actions = this.node('div', undefined, 'collageOrderActions');
    const button = (text, aria, handler, disabled = false) => {
      const b = this.node('button', text, 'btn ghost small');
      b.disabled = disabled; b.setAttribute('aria-label', aria); b.addEventListener('click', handler);
      actions.appendChild(b);
    };
    button('上移', `上移拼图中的原图 ${id + 1}`, () => this.move(id, index - 1), index === 0);
    button('下移', `下移拼图中的原图 ${id + 1}`, () => this.move(id, index + 1), index === this.ids.length - 1);
    button('裁剪', `裁剪拼图中的原图 ${id + 1}`, () => this.openCrop(id), original);
    button('移除', `从拼图移除原图 ${id + 1}`, () => this.toggle(id));
    const note = this.node('input', undefined, 'collageNote');
    note.type = 'text'; note.maxLength = COLLAGE_NOTE_LIMIT; note.value = this.notes[id] || '';
    note.placeholder = original ? '“保留原图细节”模式不显示备注' : '备注（可选），如：航拍 / 推镜';
    note.disabled = original;
    note.setAttribute('aria-label', `原图 ${id + 1} 的备注`);
    // A draggable row would steal text selection from the note field.
    note.addEventListener('focus', () => { row.draggable = false; this.select(id); });
    note.addEventListener('blur', () => { row.draggable = true; });
    note.addEventListener('input', () => this.setNote(id, note.value, note));
    this.noteInputs.set(id, note);
    row.append(image, info, actions, note);
    return row;
  },

  // ------------------------------------------------------------------ rendering and downloads
  async loadImage(url) {
    const image = new Image();
    image.src = url;
    let timer, cancel;
    const interrupted = new Promise((resolve, reject) => {
      cancel = () => { image.src = ''; reject(new Error('预览已取消')); };
      timer = setTimeout(() => { image.src = ''; reject(new Error('读取原图超时，请确认工具仍在运行')); }, 20000);
    });
    this.abort = cancel;
    try { await Promise.race([image.decode(), interrupted]); return image; }
    finally { clearTimeout(timer); if (this.abort === cancel) this.abort = null; }
  },
  async assertCurrent(snapshot) {
    const current = await jsonRequest('/api/status/' + encodeURIComponent(snapshot.sid));
    if (current.result?.run !== snapshot.run) throw new Error('关键帧结果已更新。请关闭拼图面板、刷新结果后重新选择，避免混用旧图。');
  },
  async generate() {
    if (this.busy || !this.snapshot) return;
    let payload;
    try { payload = this.payload(); } catch (e) { this.el('#collageStatus').textContent = e.message; return; }
    const token = this.token, snapshot = this.snapshot;
    this.discardOutput();
    this.busy = true; this.summary();
    this.el('#collageDownload').disabled = true;
    this.el('#collageDownload').setAttribute('aria-disabled', 'true');
    this.el('#collageStatus').textContent = '正在用原图生成成品…';
    let result, published = false;
    try {
      result = await jsonRequest(`/api/collage/${encodeURIComponent(snapshot.sid)}/render`,
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)}, 0);
      result.sid = snapshot.sid;
      if (token !== this.token) return;
      const image = await this.loadImage(result.preview_url);
      if (token !== this.token) return;
      image.alt = '实际导出文件的预览'; image.className = 'collageFinalImage';
      const preview = this.el('#collagePreview');
      preview.innerHTML = ''; preview.appendChild(image);
      this.output = {...result, revision: token, page: this.page, pages: this.pages(this.ids, payload.per_page, payload.split).length};
      const link = this.el('#collageDownload');
      link.href = result.download_url; link.download = ''; link.disabled = false; link.setAttribute('aria-disabled', 'false');
      this.el('#collageActual').disabled = false;
      this.el('#collageFitView').disabled = false;
      const info = `第 ${this.output.page + 1}/${this.output.pages} 张 · ${result.width} × ${result.height} · ${result.format.toUpperCase()}`;
      this.el('#collageResultInfo').textContent = info;
      this.el('#collageStatus').textContent = `成品已生成：${info}。预览与下载对应同一个文件。`;
      this.el('#collageResult').hidden = false;
      published = true;
    } catch (e) {
      if (token === this.token) this.el('#collageStatus').textContent = e.message || '生成失败，未改变原有截图';
    } finally {
      if (result && !published) this.discardOutput(result);
      this.busy = false; this.summary();
    }
  },
  async finalView(actual) {
    if (!this.output) return;
    const output = this.output, token = this.token;
    try {
      const image = await this.loadImage(actual ? output.image_url : output.preview_url);
      if (token !== this.token) return;
      image.alt = actual ? '成品原尺寸，滚动查看细节' : '实际导出文件的预览';
      image.className = actual ? 'collageFinalImage actual' : 'collageFinalImage';
      const preview = this.el('#collagePreview');
      preview.innerHTML = ''; preview.appendChild(image);
    } catch (e) {
      if (token === this.token) this.el('#collageStatus').textContent = e.message || '读取成品失败';
    }
  },
  showResult() { if (this.output) this.el('#collageResult').hidden = false; },
  closeResult() { this.el('#collageResult').hidden = true; },
  download(event) {
    if (!this.output || this.output.revision !== this.token) { event.preventDefault(); return; }
    this.el('#collageStatus').textContent = `已交给浏览器下载第 ${this.output.page + 1}/${this.output.pages} 张成品，请查看下载列表。`;
  },
  startDownload(url, filename = '') {
    const link = this.el('#collageBatchLink');
    link.href = url; link.download = filename;
    link.click();
  },
  async downloadAll() {
    if (this.busy || !this.snapshot || !this.ids.length) return;
    let payload;
    try { payload = this.batchPayload(); } catch (e) { this.el('#collageStatus').textContent = e.message; return; }
    const snapshot = this.snapshot, pages = Math.ceil(payload.ids.length / payload.per_page);
    this.busy = true; this.summary();
    this.el('#collageStatus').textContent = `正在逐张生成 ${pages} 张拼图并打包，请稍候…`;
    try {
      const result = await jsonRequest(`/api/collage/${encodeURIComponent(snapshot.sid)}/render-all`,
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)}, 0);
      this.startDownload(result.download_url);
      this.el('#collageStatus').textContent = `已打包 ${result.pages} 张拼图和分镜表（${(result.bytes / 1048576).toFixed(1)} MB），已交给浏览器下载。`;
    } catch (e) {
      this.el('#collageStatus').textContent = e.message || '打包失败，未改变原有截图';
    } finally {
      this.busy = false; this.summary();
    }
  },
  async exportCsv() {
    if (this.busy || !this.snapshot || !this.ids.length) return;
    let payload;
    try { payload = this.batchPayload(); } catch (e) { this.el('#collageStatus').textContent = e.message; return; }
    try {
      const response = await fetch(`/api/collage/${encodeURIComponent(this.snapshot.sid)}/storyboard`,
        {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(typeof data.detail === 'string' ? data.detail : '导出失败，请重试');
      }
      const url = URL.createObjectURL(await response.blob());
      this.startDownload(url, `分镜表_${this.snapshot.name.replace(/\.[^.]+$/, '')}.csv`);
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      this.el('#collageStatus').textContent = `已导出 ${payload.ids.length} 张的分镜表，可用 Excel 打开。`;
    } catch (e) {
      this.el('#collageStatus').textContent = e.message || '导出失败，请重试';
    }
  },
};

// ------------------------------------------------------------------ wiring
const collageOn = (selector, event, handler) => document.querySelector(selector).addEventListener(event, handler);
collageOn('#btnCollage', 'click', () => CollageUI.open());
collageOn('#collageClose', 'click', () => CollageUI.close());
collageOn('#collageSelectAll', 'click', () => {
  if (!CollageUI.snapshot) return;
  CollageUI.ids = CollageUI.snapshot.cuts.flatMap((_, i) => CollageUI.available(i) ? [i] : []);
  CollageUI.changed();
});
collageOn('#collageClear', 'click', () => { CollageUI.ids = []; CollageUI.changed(); });
collageOn('#collageImportSelection', 'click', () => {
  if (CollageUI.snapshot?.sid !== state.sid || CollageUI.snapshot?.run !== state.resultRun) { toast('结果版本已变化，请关闭后重新打开拼图面板'); return; }
  CollageUI.ids = [...state.sel].filter(i => CollageUI.available(i)).sort((a, b) => a - b);
  CollageUI.changed();
});
for (const [selector] of COLLAGE_SETTINGS) collageOn(selector, 'change', () => CollageUI.settingChanged(selector));
collageOn('#collageTitleText', 'input', () => CollageUI.settingChanged('#collageTitleText'));
collageOn('#collageFocusNote', 'input', () => {
  if (CollageUI.focus !== null) CollageUI.setNote(CollageUI.focus, CollageUI.el('#collageFocusNote').value, CollageUI.el('#collageFocusNote'));
});
collageOn('#collagePrev', 'click', () => CollageUI.changePage(-1));
collageOn('#collageNext', 'click', () => CollageUI.changePage(1));
collageOn('#collageRender', 'click', () => CollageUI.generate());
collageOn('#collageShowResult', 'click', () => CollageUI.showResult());
collageOn('#collageResultClose', 'click', () => CollageUI.closeResult());
collageOn('#collageDownload', 'click', event => CollageUI.download(event));
collageOn('#collageDownloadAll', 'click', () => CollageUI.downloadAll());
collageOn('#collageCsv', 'click', () => CollageUI.exportCsv());
collageOn('#collageCropSelected', 'click', () => { if (CollageUI.focus !== null) CollageUI.openCrop(CollageUI.focus); });
collageOn('#collageResetCrop', 'click', () => { if (CollageUI.focus !== null) CollageUI.setCrop(CollageUI.focus, null); });
collageOn('#collageActual', 'click', () => CollageUI.finalView(true));
collageOn('#collageFitView', 'click', () => CollageUI.finalView(false));
collageOn('#cropZoomIn', 'click', () => CollageUI.cropper?.zoom(.1));
collageOn('#cropZoomOut', 'click', () => CollageUI.cropper?.zoom(-.1));
collageOn('#cropReset', 'click', () => CollageUI.cropper?.reset());
collageOn('#cropCancel', 'click', () => CollageUI.closeCrop());
collageOn('#cropApply', 'click', () => CollageUI.applyCrop());
if (typeof window !== 'undefined' && window.addEventListener) {
  let resizeTimer;
  // The preview is drawn for the width it had; redraw it when the window size changes.
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!CollageUI.el('#collagePanel').hidden && CollageUI.plan && CollageUI.stageView) CollageUI.stageView.show(CollageUI.plan).catch(() => {});
    }, 200);
  });
}

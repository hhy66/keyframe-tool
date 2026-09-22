const $ = s => document.querySelector(s);
const state = {
  sid: null, busy: false, pollTimer: null, pollToken: 0, requestRevision: 0,
  analyzing: false, pendingAnalyze: false, run: null, resultRun: null,
  sens: 50, ends: false, minimum: 0.1, flash: true, maxExport: 500,
  cuts: [], thumbs: [], frames: [], sel: new Set(), dlBusy: false,
  videoSid: null, totalFrames: 0, jobRunning: false, editBusy: false,
  previewFrame: null, previewTime: 0, previewReady: false, previewToken: 0,
  editIndex: null, editorBase: null,
  ignorePause: false, previewLoading: false,
};
const kindName = {cut: '镜头切换', head: '首帧', tail: '末帧', manual: '手动补帧', adjusted: '已微调'};
const sensName = v => v < 33 ? '保守' : v > 66 ? '敏感' : '适中';
let sensTimer = null;

function toast(msg, err) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'show' + (err ? ' err' : '');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.className = '', 5000);
}
function fmtDur(s) {
  if (!Number.isFinite(s)) return '?';
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(sec).padStart(2, '0');
}
function readStorage(key, fallback) {
  try { return JSON.parse(sessionStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function saveStorage(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* 存储禁用时仍可使用 */ }
}
function skippedFrames() {
  const values = readStorage('kf_skipped_' + state.sid, []);
  return new Set(Array.isArray(values) ? values : []);
}
function rememberSelection() {
  const skipped = skippedFrames();
  state.cuts.forEach((c, i) => {
    if (state.sel.has(i)) skipped.delete(c.frame_index);
    else skipped.add(c.frame_index);
  });
  saveStorage('kf_skipped_' + state.sid, [...skipped]);
}
async function jsonRequest(url, options = {}, timeout = 15000) {
  const controller = new AbortController();
  const timer = timeout ? setTimeout(() => controller.abort(), timeout) : null;
  try {
    const response = await fetch(url, {...options, signal: controller.signal});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请重试');
      error.status = response.status;
      throw error;
    }
    return data;
  } finally { clearTimeout(timer); }
}
function syncInfo(info, params = true) {
  $('#cardParams').hidden = false;
  $('#metaBox').style.display = 'block';
  $('#mName').textContent = info.video_name || '';
  const m = info.meta || {};
  $('#mSize').textContent = (m.size_mb || 0) + ' MB';
  $('#mRes').textContent = m.width + '×' + m.height;
  $('#mDur').textContent = fmtDur(m.duration) + (m.frames ? ` (${m.frames} 帧)` : '');
  if (params && info.params) {
    state.sens = info.params.sensitivity;
    state.ends = info.params.include_ends;
    state.minimum = info.params.min_scene_seconds ?? 0.1;
    state.flash = info.params.suppress_flash ?? true;
    $('#sens').value = state.sens;
    $('#sensVal').textContent = `${sensName(state.sens)} (${state.sens})`;
    $('#chkEnds').checked = state.ends;
    $('#minScene').value = state.minimum;
    $('#chkFlash').checked = state.flash;
  }
}
function setUploadBusy(busy) {
  state.busy = busy;
  ['#sens', '#chkEnds', '#minScene', '#chkFlash', '#btnAgain'].forEach(s => $(s).disabled = busy);
}

async function upload(file) {
  if (state.busy || state.editBusy || !file) return;
  if (!/\.(mp4|mov|avi|mkv|webm|m4v|ts|flv|wmv|mpg|mpeg|3gp|ogv)$/i.test(file.name)) {
    toast('不支持的文件格式', true); return;
  }
  setUploadBusy(true);
  stopPoll();
  clearTimeout(sensTimer);
  state.requestRevision++;
  state.pendingAnalyze = false;
  toast('正在复制视频到本机工作目录…');
  let uploaded = false;
  try {
    const fd = new FormData(); fd.append('file', file);
    const j = await jsonRequest('/api/upload', {method:'POST', body:fd}, 0);
    state.sid = j.session_id;
    try { sessionStorage.setItem('kf_sid', state.sid); } catch { /* 可继续处理 */ }
    state.run = state.resultRun = null;
    state.previewToken++; state.previewFrame = null; state.previewReady = false; state.previewLoading = false;
    state.editIndex = null; state.totalFrames = 0;
    $('#cardEditor').hidden = true;
    $('#exactFrame').hidden = true;
    $('#frameSeek').value = 0;
    $('#seekTime').value = 0;
    $('#previewLabel').textContent = '分析完成后可定位和补帧。';
    pauseForFrameControl();
    state.cuts = []; state.sel.clear();
    closeLb();
    $('#cardResult').hidden = true;
    syncInfo(j, false);
    uploaded = true;
  } catch (e) {
    toast((e.message || '上传失败') + '；原有结果已保留', true);
  } finally {
    setUploadBusy(false);
    if (uploaded) analyze();
    else if (state.sid) startPoll(true);
  }
}

async function analyze() {
  if (!state.sid || state.busy || state.editBusy) return;
  clearTimeout(sensTimer);
  stopPoll();
  const revision = ++state.requestRevision;
  if (state.analyzing) { state.pendingAnalyze = true; return; }
  if (!Number.isFinite(state.minimum) || state.minimum < 0 || state.minimum > 10) {
    toast('最小镜头间隔须为 0～10 秒', true); startPoll(); return;
  }
  state.analyzing = true;
  state.jobRunning = true;
  updateEditorControls();
  state.pendingAnalyze = false;
  const sid = state.sid;
  $('#cardProgress').hidden = false;
  $('#bar').style.width = '0%';
  $('#stage').textContent = '正在提交分析任务…';
  $('#btnAgain').disabled = true;
  $('#btnRetry').hidden = true;
  try {
    const j = await jsonRequest('/api/analyze', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:sid, sensitivity:state.sens, include_ends:state.ends,
        min_scene_seconds:state.minimum, suppress_flash:state.flash}),
    });
    if (revision !== state.requestRevision || sid !== state.sid) return;
    state.run = j.run;
    startPoll();
  } catch (e) {
    if (revision !== state.requestRevision || sid !== state.sid) return;
    toast(e.message || '分析启动失败', true);
    // 请求超时时服务端可能已经收到任务，查询状态再决定，不直接丢掉会话。
    startPoll(true);
  } finally {
    state.analyzing = false;
    $('#btnAgain').disabled = state.busy;
    if (state.pendingAnalyze && !state.busy) analyze();
  }
}

function stopPoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
  state.pollToken++;
}
function startPoll(restoreParams = false) {
  stopPoll();
  poll(state.pollToken, restoreParams, 0);
}
async function poll(token, restoreParams, failures) {
  const sid = state.sid;
  if (!sid) return;
  const current = () => token === state.pollToken && sid === state.sid;
  try {
    const j = await jsonRequest('/api/status/' + sid);
    if (!current()) return;
    state.run = j.run;
    state.maxExport = j.max_export || 500;
    state.jobRunning = j.status === 'running';
    syncInfo(j, restoreParams);
    $('#btnRetry').hidden = true;
    if (j.result && (restoreParams || state.resultRun !== j.result.run)) render(j.result, false);
    updateEditorControls();
    if (j.status === 'running') {
      $('#cardProgress').hidden = false;
      $('#btnCancel').hidden = false;
      $('#btnCancel').disabled = false;
      $('#bar').style.width = Math.max(1, j.pct) + '%';
      $('#stage').textContent = j.stage || '';
      state.pollTimer = setTimeout(() => poll(token, false, 0), 650);
    } else {
      $('#cardProgress').hidden = true;
      $('#btnCancel').hidden = true;
      $('#btnAgain').disabled = state.busy || state.analyzing;
      if (j.status === 'error') toast('分析失败：' + (j.error || '未知错误'), true);
      else if (j.status === 'cancelled') toast('任务已取消，已有结果仍可下载');
    }
  } catch (e) {
    if (!current()) return;
    $('#cardProgress').hidden = false;
    $('#btnCancel').hidden = true;
    $('#btnAgain').disabled = false;
    if (e.status === 404) {
      $('#stage').textContent = '会话已失效，请重新选择视频。';
      $('#btnRetry').hidden = true;
      $('#cardResult').hidden = true;
      state.sid = null; state.resultRun = null;
      state.previewToken++; state.previewReady = false;
      $('#cardEditor').hidden = true;
      pauseForFrameControl();
      try { sessionStorage.removeItem('kf_sid'); } catch { /* 忽略不可用存储 */ }
      return;
    }
    const count = failures + 1;
    $('#stage').textContent = count < 5 ? `连接暂时中断，正在重试 (${count}/5)…` : '连接失败。请确认工具仍在运行，再点击重试连接。';
    $('#btnRetry').hidden = count < 5;
    if (count < 5) state.pollTimer = setTimeout(() => poll(token, restoreParams, count), Math.min(5000, count * 1000));
  }
}

function render(res, restoreParams = true) {
  syncInfo(res, restoreParams);
  if (state.resultRun !== res.run) state.editIndex = null;
  state.resultRun = res.run;
  state.cuts = res.cuts || []; state.thumbs = res.thumbs || []; state.frames = res.frames || [];
  const skipped = skippedFrames();
  state.sel = new Set(state.cuts.flatMap((c, i) => skipped.has(c.frame_index) ? [] : [i]));
  $('#cardResult').hidden = false;
  $('#resultNote').textContent = res.meta?.estimated_time ? '此视频部分时间戳不可用，时间码按帧率估算；截图仍按帧编号定位。' : '';
  const grid = $('#grid'); grid.innerHTML = '';
  $('#empty').hidden = state.cuts.length !== 0;
  state.cuts.forEach((c, i) => {
    const item = document.createElement('div'); item.className = 'item';
    const img = document.createElement('img');
    img.loading = 'lazy'; img.src = state.thumbs[i]; img.alt = c.label;
    const eye = document.createElement('span'); eye.className = 'eye'; eye.textContent = '🔍 原图';
    const cap = document.createElement('div'); cap.className = 'cap';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox'; checkbox.checked = state.sel.has(i);
    checkbox.setAttribute('aria-label', '保留第 ' + (i + 1) + ' 张');
    const idx = document.createElement('span'); idx.className = 'idx'; idx.textContent = '#' + String(i + 1).padStart(3, '0');
    const tm = document.createElement('span'); tm.className = 'time'; tm.textContent = c.label;
    const bd = document.createElement('span'); bd.className = 'badge ' + c.kind; bd.textContent = kindName[c.kind] || c.kind;
    cap.append(checkbox, idx, tm, bd);
    const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = '已跳过';
    const paint = () => {
      const selected = state.sel.has(i);
      item.classList.toggle('off', !selected); tag.hidden = selected; checkbox.checked = selected;
    };
    const toggle = () => {
      if (state.sel.has(i)) state.sel.delete(i); else state.sel.add(i);
      paint(); rememberSelection(); updateToolbar();
    };
    const editRow = document.createElement('div'); editRow.className = 'editRow';
    const edit = document.createElement('button'); edit.className = 'btn ghost small'; edit.textContent = '逐帧微调';
    edit.setAttribute('aria-label', '微调第 ' + (i + 1) + ' 张');
    edit.addEventListener('click', ev => {ev.stopPropagation(); beginEdit(i);});
    editRow.append(edit);
    item.append(img, eye, tag, cap, editRow);
    img.addEventListener('click', ev => {ev.stopPropagation(); openLb(i);});
    checkbox.addEventListener('click', ev => {ev.stopPropagation(); toggle();});
    item.addEventListener('click', toggle);
    paint(); grid.appendChild(item);
  });
  updateToolbar();
  configureEditor(res);
}

function configureEditor(res) {
  const player = $('#videoPlayer');
  if (state.videoSid !== state.sid) {
    state.videoSid = state.sid;
    player.src = '/api/video/' + state.sid;
    player.load();
    $('#videoHint').textContent = '暂停或拖动播放进度后，右侧显示对应的精确帧。';
  }
  state.totalFrames = res.meta?.frames || 0;
  state.editorBase = res.run;
  $('#cardEditor').hidden = false;
  $('#frameSeek').max = Math.max(0, state.totalFrames - 1);
  if (state.previewFrame === null && res.cuts.length) {
    const first = res.cuts[0];
    state.previewFrame = first.frame_index; state.previewTime = first.time; state.previewReady = true;
    $('#exactFrame').src = res.frames[0]; $('#exactFrame').hidden = false;
    $('#frameSeek').value = first.frame_index;
    $('#seekTime').value = first.time;
    $('#previewLabel').textContent = `${first.label} · 第 ${first.frame_index + 1} / ${state.totalFrames} 帧`;
  }
  updateEditorControls();
  if (state.previewFrame === null && !state.jobRunning && state.totalFrames) requestPreview({frame_index:0});
}
function updateEditorControls() {
  const blocked = state.busy || state.editBusy || state.jobRunning || !state.resultRun;
  const frame = state.previewFrame ?? 0;
  $('#frameSeek').disabled = blocked;
  $('#btnSeekTime').disabled = blocked;
  $('#btnPrevFrame').disabled = blocked || state.previewLoading || frame <= 0;
  $('#btnNextFrame').disabled = blocked || state.previewLoading || frame >= state.totalFrames - 1;
  $('#btnAddFrame').disabled = blocked || !state.previewReady;
  $('#btnSaveFrame').disabled = blocked || !state.previewReady || state.editIndex === null;
  $('#btnSaveFrame').hidden = state.editIndex === null;
  $('#btnCancelEdit').hidden = state.editIndex === null;
  $('#btnCancelEdit').disabled = state.editBusy;
  $('#btnAddFrame').hidden = state.editIndex !== null;
  $('#editorMode').textContent = state.editIndex === null ? '补入新的关键帧' : `微调第 ${state.editIndex + 1} 张 · 保存后替换该截图`;
}
async function requestPreview(position) {
  if (!state.sid || !state.totalFrames || state.editBusy || state.jobRunning) return;
  const sid = state.sid, token = ++state.previewToken;
  state.previewLoading = true;
  state.previewReady = false;
  updateEditorControls();
  $('#previewLabel').textContent = '正在读取精确帧…';
  // 一个会话只有一个解码器；等待上一张读完，中间过期的定位请求直接跳过。
  const previous = state.previewChain || Promise.resolve();
  let release;
  state.previewChain = new Promise(resolve => {release = resolve;});
  await previous;
  if (token !== state.previewToken || sid !== state.sid) {release(); return;}
  const query = position.frame_index !== undefined ? 'frame_index=' + position.frame_index : 'time=' + position.time;
  try {
    const p = await jsonRequest('/api/preview/' + sid + '?' + query);
    if (token !== state.previewToken || sid !== state.sid) return;
    if (!Number.isInteger(p.frame_index)) throw new Error('无法定位该帧');
    const image = new Image(); image.src = p.image_url;
    await image.decode();
    if (token !== state.previewToken || sid !== state.sid) return;
    state.previewFrame = p.frame_index; state.previewTime = p.time; state.previewReady = true;
    $('#exactFrame').src = image.src; $('#exactFrame').hidden = false;
    $('#frameSeek').value = p.frame_index;
    $('#seekTime').value = Number(p.time.toFixed(6));
    $('#previewLabel').textContent = `${p.label} · 第 ${p.frame_index + 1} / ${state.totalFrames} 帧`;
  } catch (e) {
    if (token !== state.previewToken || sid !== state.sid) return;
    $('#previewLabel').textContent = e.message || '精确帧读取失败，请重试';
  } finally {
    release();
    if (token === state.previewToken && sid === state.sid) {
      state.previewLoading = false;
      updateEditorControls();
    }
  }
}
function pauseForFrameControl() {
  const player = $('#videoPlayer');
  if (!player.paused) {state.ignorePause = true; player.pause();}
}
function stepPreview(delta) {
  pauseForFrameControl();
  const target = Math.max(0, Math.min(state.totalFrames - 1, (state.previewFrame ?? 0) + delta));
  return requestPreview({frame_index:target});
}
function beginEdit(index) {
  if (state.jobRunning || state.editBusy || state.busy) {toast('请等当前任务完成后再微调'); return;}
  const c = state.cuts[index]; if (!c) return;
  state.editIndex = index; state.editorBase = state.resultRun;
  pauseForFrameControl();
  $('#cardEditor').scrollIntoView({behavior:'smooth', block:'start'});
  return requestPreview({frame_index:c.frame_index});
}
async function saveManual(action) {
  if (!state.previewReady || state.jobRunning || state.editBusy || state.busy || !state.resultRun) return;
  if (action === 'move' && state.editIndex === null) return;
  const sid = state.sid, base = state.editorBase;
  const target = state.previewFrame, index = state.editIndex;
  clearTimeout(sensTimer); stopPoll();
  state.editBusy = true;
  setUploadBusy(true); updateEditorControls();
  try {
    const j = await jsonRequest('/api/edit/' + sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({base_run:base,action,frame_index:target,index}),
    }, 0);
    if (sid !== state.sid) return;
    const skipped = skippedFrames();
    if (j.from_frame !== null && skipped.has(j.from_frame)) {skipped.delete(j.from_frame); skipped.add(j.to_frame);}
    if (action === 'add') skipped.delete(j.to_frame);
    saveStorage('kf_skipped_' + sid,[...skipped]);
    state.run = j.result.run;
    state.editIndex = null;
    closeLb(); render(j.result,false);
    toast(action === 'add' ? '已补入关键帧，导出时会按时间排序' : '已保存逐帧微调');
  } catch (e) {
    toast(e.message || '保存失败，原有结果已保留',true);
    if (sid === state.sid) startPoll(true);
  } finally {
    state.editBusy = false;
    setUploadBusy(false); updateEditorControls();
  }
}
function updateToolbar() {
  $('#cntAll').textContent = state.cuts.length; $('#cntSel').textContent = state.sel.size;
  $('#btnDownload').disabled = state.dlBusy || state.sel.size === 0 || state.sel.size > state.maxExport;
  $('#exportLimit').textContent = `单次最多 ${state.maxExport} 张` + (state.sel.size > state.maxExport ? '，请减少选择后下载。' : '；下载交给浏览器保存。');
}
function openLb(i) {
  if (!state.frames[i]) return;
  $('#lbImg').src = state.frames[i];
  const c = state.cuts[i];
  $('#lbCap').textContent = '#' + String(i + 1).padStart(3, '0') + '  ' + c.label + ' · ' + (kindName[c.kind] || c.kind) + ' · 第 ' + (c.frame_index + 1) + ' 帧';
  $('#lbDl').href = state.frames[i].replace('/api/frame/', '/api/frame-dl/');
  $('#lbDl').download = ''; $('#lb').hidden = false;
}
function closeLb() { $('#lb').hidden = true; $('#lbImg').src = ''; }

async function download() {
  if (!state.sid || !state.resultRun || !state.sel.size || state.dlBusy || state.sel.size > state.maxExport) return;
  state.dlBusy = true; updateToolbar();
  $('#dl').hidden = false; $('#dlBar').style.width = '15%'; $('#dlInfo').textContent = '正在生成 ZIP…';
  try {
    const j = await jsonRequest('/api/export/' + state.sid, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({run:state.resultRun, ids:[...state.sel].sort((a,b)=>a-b).join(',')}),
    }, 0);
    const a = document.createElement('a'); a.href = j.url; a.download = '';
    document.body.appendChild(a); a.click(); a.remove();
    toast(`已交给浏览器下载 ${j.count} 张图片，请在浏览器下载列表中查看保存结果`);
  } catch (e) { toast(e.message || '打包失败，请重试', true); }
  finally { state.dlBusy = false; $('#dl').hidden = true; updateToolbar(); }
}

const drop = $('#drop'), fileInput = $('#file');
drop.addEventListener('click', () => {if (!state.busy) fileInput.click();});
drop.addEventListener('keydown', e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); if (!state.busy) fileInput.click();}});
fileInput.addEventListener('change', () => {if (fileInput.files[0]) upload(fileInput.files[0]); fileInput.value = '';});
['dragover','dragenter'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.add('over');}));
['dragleave','drop'].forEach(ev => drop.addEventListener(ev, e => {e.preventDefault(); drop.classList.remove('over');}));
drop.addEventListener('drop', e => {const f = e.dataTransfer?.files?.[0]; if (f) upload(f);});
$('#sens').addEventListener('input', () => {
  state.sens = +$('#sens').value;
  $('#sensVal').textContent = `${sensName(state.sens)} (${state.sens})`;
  clearTimeout(sensTimer); sensTimer = setTimeout(analyze, 500);
});
$('#chkEnds').addEventListener('change', () => {state.ends = $('#chkEnds').checked; analyze();});
$('#chkFlash').addEventListener('change', () => {state.flash = $('#chkFlash').checked; analyze();});
$('#minScene').addEventListener('change', () => {state.minimum = +$('#minScene').value; analyze();});
$('#btnAgain').addEventListener('click', analyze);
$('#btnRetry').addEventListener('click', () => startPoll(true));
$('#btnCancel').addEventListener('click', async () => {
  const sid = state.sid;
  const run = state.run;
  clearTimeout(sensTimer);
  state.pendingAnalyze = false;
  $('#btnCancel').disabled = true;
  try {await jsonRequest('/api/cancel/' + sid, {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({run}),
  });}
  catch (e) {toast(e.message || '取消失败，请重试', true);}
  finally {if (sid === state.sid) startPoll();}
});
$('#btnAll').addEventListener('click', () => {
  state.sel = new Set(state.cuts.map((_, i) => i)); rememberSelection();
  document.querySelectorAll('#grid .item').forEach(el => {el.classList.remove('off'); el.querySelector('.tag').hidden = true; el.querySelector('input').checked = true;});
  updateToolbar();
});
$('#btnNone').addEventListener('click', () => {
  state.sel.clear(); rememberSelection();
  document.querySelectorAll('#grid .item').forEach(el => {el.classList.add('off'); el.querySelector('.tag').hidden = false; el.querySelector('input').checked = false;});
  updateToolbar();
});
$('#btnDownload').addEventListener('click', download);
$('#btnPrevFrame').addEventListener('click', () => stepPreview(-1));
$('#btnNextFrame').addEventListener('click', () => stepPreview(1));
$('#frameSeek').addEventListener('change', () => {
  pauseForFrameControl(); requestPreview({frame_index:+$('#frameSeek').value});
});
$('#btnSeekTime').addEventListener('click', () => {
  const time = +$('#seekTime').value;
  if (!Number.isFinite(time) || time < 0) {toast('请输入非负秒数',true); return;}
  pauseForFrameControl(); requestPreview({time});
});
$('#btnAddFrame').addEventListener('click', () => saveManual('add'));
$('#btnSaveFrame').addEventListener('click', () => saveManual('move'));
$('#btnCancelEdit').addEventListener('click', () => {state.editIndex=null; updateEditorControls();});
$('#videoPlayer').addEventListener('error', () => {
  $('#videoHint').textContent = '浏览器无法播放此格式，仍可输入秒数或使用逐帧时间轴定位并补帧。';
});
$('#videoPlayer').addEventListener('timeupdate', () => {
  if (!$('#videoPlayer').paused) $('#seekTime').value = $('#videoPlayer').currentTime.toFixed(3);
});
['pause','seeked'].forEach(name => $('#videoPlayer').addEventListener(name, () => {
  if (name === 'pause' && state.ignorePause) {state.ignorePause = false; return;}
  if ($('#videoPlayer').paused && state.resultRun && !state.editBusy) requestPreview({time:$('#videoPlayer').currentTime});
}));
$('#lbClose').addEventListener('click', closeLb);
$('#lb').addEventListener('click', e => {if (e.target.id === 'lb') closeLb();});
document.addEventListener('keydown', e => {if (e.key === 'Escape') closeLb();});
try { state.sid = sessionStorage.getItem('kf_sid'); } catch { /* 可在无存储模式运行 */ }
if (state.sid) startPoll(true);

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load() {
  const context = vm.createContext({document: {querySelector: () => null}, window: {}});
  vm.runInContext(fs.readFileSync('static/layout.js', 'utf8'), context);
  return context;
}
const idle = {
  videoName: '',
  workspaceCount: 0,
  paramsVisible: false,
  sens: '',
  progressVisible: false,
  running: false,
  progress: 0,
  progressText: '',
  editorVisible: false,
  resultVisible: false,
  total: '0',
  selected: '0',
  downloadEnabled: false,
};

test('before a video only the start and management entries are usable, each disabled one says why', () => {
  const nav = load().layoutNavState(idle);
  assert.deepEqual(
    Object.keys(nav).filter(k => !nav[k].disabled),
    ['workspace', 'upload', 'storage'],
  );
  assert.equal(nav.upload.text, '尚未选择');
  for (const key of ['params', 'progress', 'editor', 'results', 'download', 'collage']) assert.ok(nav[key].reason, key);
});
test('a running analysis is live and results show counts', () => {
  const nav = load().layoutNavState({
    ...idle,
    videoName: 'a.mp4',
    paramsVisible: true,
    sens: '适中 (50)',
    progressVisible: true,
    running: true,
    progress: 63,
    resultVisible: true,
    total: '45',
    selected: '40',
    downloadEnabled: true,
    editorVisible: true,
  });
  assert.equal(nav.progress.text, '分析中 63%');
  assert.equal(nav.progress.live, true);
  assert.equal(nav.results.text, '45 张 · 选中 40');
  assert.equal(nav.download.text, '40 张');
  assert.equal(nav.params.text, '适中 (50)');
  assert.equal(nav.editor.disabled, false);
});
test('download explains an empty selection', () => {
  const nav = load().layoutNavState({...idle, resultVisible: true, total: '5', selected: '0'});
  assert.equal(nav.download.disabled, true);
  assert.match(nav.download.reason, /选中/);
});
test('every navigator entry and layout hook exists in the page', () => {
  const html = fs.readFileSync('static/index.html', 'utf8');
  for (const key of Object.keys(load().layoutNavState(idle))) {
    assert.match(html, new RegExp(`data-nav="${key}"`));
    assert.match(html, new RegExp(`id="navStatus-${key}"`));
  }
  for (const id of [
    'cardWorkspace',
    'workspaceToggle',
    'workspaceBody',
    'cardUpload',
    'cardParams',
    'cardEditor',
    'cardProgress',
    'cardResult',
    'resultsEmpty',
    'colLeft',
    'colRight',
    'navToggle',
    'navFab',
    'navScrim',
    'btnDownload',
    'btnCollage',
    'btnStorage',
  ])
    assert.match(html, new RegExp(`id="${id}"`), id);
  // Results and progress live in the right column, tools in the left.
  const left = html.slice(html.indexOf('id="colLeft"'), html.indexOf('id="colRight"'));
  const right = html.slice(html.indexOf('id="colRight"'));
  for (const id of ['cardWorkspace', 'cardUpload', 'cardParams', 'cardEditor'])
    assert.match(left, new RegExp(`id="${id}"`));
  for (const id of ['cardProgress', 'cardResult', 'resultsEmpty']) assert.match(right, new RegExp(`id="${id}"`));
});
test('a dragged divider stays within its bounds', () => {
  const {layoutSplitWidth} = load();
  assert.equal(layoutSplitWidth(500, 100, 260, 800), 400);
  assert.equal(layoutSplitWidth(200, 100, 260, 800), 260, 'never narrower than the minimum');
  assert.equal(layoutSplitWidth(1500, 100, 260, 800), 800, 'the other column keeps its room');
  assert.equal(layoutSplitWidth(500, 100, 260, 100), 260, 'a small window still gets the minimum');
});
test('every divider and full view exists, and the views use the same two-column shell', () => {
  const html = fs.readFileSync('static/index.html', 'utf8');
  const context = load();
  for (const {splitter, columns} of Object.values(vm.runInContext('LAYOUT_SPLITS', context))) {
    assert.match(html, new RegExp(`id="${splitter.slice(1)}"[^>]*role="separator"`), splitter);
    if (columns?.startsWith('#')) assert.match(html, new RegExp(`id="${columns.slice(1)}"`), columns);
  }
  for (const [key, {view, close}] of Object.entries(vm.runInContext('LAYOUT_VIEWS', context))) {
    assert.match(html, new RegExp(`id="${view.slice(1)}" class="appView"`), view);
    assert.match(html, new RegExp(`id="${close.slice(1)}"`), close);
    assert.match(html, new RegExp(`data-nav="${key}"`), key);
  }
  // Each view: header, left column, divider, right column.
  for (const [view, divider] of [
    ['storageManager', 'storageSplitter'],
    ['collagePanel', 'collageSplitter'],
  ]) {
    const start = html.indexOf(`id="${view}"`);
    const part = html.slice(start, html.indexOf(`id="${divider}"`) + 200);
    assert.ok(part.indexOf('viewHead') < part.indexOf('viewLeft'), view);
    assert.ok(part.indexOf('viewLeft') < part.indexOf(divider), view);
    assert.match(part.slice(part.indexOf(divider)), /viewRight/, view);
  }
});

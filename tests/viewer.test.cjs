const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function boot({count = 5} = {}) {
  const nodes = new Map();
  const node = () => ({
    hidden: false,
    disabled: false,
    textContent: '',
    className: '',
    innerHTML: '',
    children: [],
    scrollLeft: 0,
    clientWidth: 500,
    attrs: {},
    classList: {toggle() {}, add() {}, remove() {}},
    setAttribute(k, v) {
      this.attrs[k] = v;
    },
    removeAttribute(k) {
      delete this[k];
    },
    appendChild(child) {
      this.children.push(child);
    },
    addEventListener() {},
    click() {
      this.clicked = (this.clicked || 0) + 1;
    },
  });
  const $ = s => {
    if (!nodes.has(s)) nodes.set(s, node());
    return nodes.get(s);
  };
  $('#lb').hidden = true;
  const log = [];
  const cards = Array.from({length: count}, (_, i) => ({
    edit: {disabled: i === 3, click: () => log.push('edit ' + i)},
  }));
  const state = {
    cuts: Array.from({length: count}, (_, i) => ({kind: 'cut', label: `00:00:0${i}`, frame_index: i * 10})),
    frames: Array.from({length: count}, (_, i) => `/api/frame/s/r/${i}`),
    thumbs: Array.from({length: count}, (_, i) => `/api/thumb/s/r/${i}`),
    sel: new Set(Array.from({length: count}, (_, i) => i)),
    cards,
    busy: false,
    editBusy: false,
  };
  const context = vm.createContext({
    state,
    kindName: {cut: '镜头切换'},
    Image: class {},
    toast: message => log.push('toast'),
    setKept: (ids, keep) => {
      ids.forEach(i => (keep ? state.sel.add(i) : state.sel.delete(i)));
      log.push(`setKept ${ids} ${keep}`);
    },
    Review: {flagged: [2], scores: [null, 40, 91], setCursor: i => log.push('cursor ' + i)},
    document: {querySelector: () => null, createElement: node, body: {classList: {add() {}, remove() {}}}},
  });
  vm.runInContext(fs.readFileSync('static/viewer.js', 'utf8'), context);
  const Viewer = vm.runInContext('Viewer', context);
  Viewer.el = $;
  const key = (k, extra = {}) => {
    const e = {
      key: k,
      code: '',
      target: {tagName: 'BODY'},
      preventDefault() {},
      stopImmediatePropagation() {},
      ...extra,
    };
    Viewer.onKey(e);
  };
  return {Viewer, state, log, key, $};
}

test('opens on the clicked picture with position, details and download link', () => {
  const v = boot();
  v.Viewer.open(2);
  assert.equal(v.$('#lb').hidden, false);
  assert.equal(v.$('#vwPos').textContent, '3 / 5');
  assert.match(v.$('#vwMeta').textContent, /#003 · 00:00:02 · 镜头切换 · 第 21 帧/);
  assert.equal(v.$('#vwDownload').href, '/api/frame-dl/s/r/2');
  assert.equal(v.$('#vwSimilar').hidden, false);
  assert.equal(v.$('#vwSimilar').textContent, '与上一张相似 91%');
  assert.equal(v.$('#vwStrip').children.length, 5);
  assert.equal(v.$('#vwState').textContent, '✓ 保留');
});
test('up keeps and moves on, down skips and moves on, space toggles in place', () => {
  const v = boot();
  v.Viewer.open(0);
  v.key('ArrowDown');
  assert.equal(v.state.sel.has(0), false);
  assert.equal(v.Viewer.index, 1);
  v.key('ArrowUp');
  assert.equal(v.state.sel.has(1), true);
  assert.equal(v.Viewer.index, 2);
  v.key(' ');
  assert.equal(v.state.sel.has(2), false);
  assert.equal(v.Viewer.index, 2);
  assert.equal(v.$('#vwState').textContent, '已跳过');
  assert.equal(v.$('#vwStats').textContent, '保留 3 / 5');
});
test('arrows, Home and End move without going past either end', () => {
  const v = boot();
  v.Viewer.open(1);
  v.key('ArrowLeft');
  v.key('ArrowLeft');
  assert.equal(v.Viewer.index, 0);
  assert.equal(v.$('#vwPrev').disabled, true);
  v.key('End');
  assert.equal(v.Viewer.index, 4);
  v.key('ArrowRight');
  assert.equal(v.Viewer.index, 4);
  v.key('ArrowDown');
  assert.equal(v.state.sel.has(4), false);
  assert.equal(v.Viewer.index, 4);
  assert.equal(v.log.at(-1), 'toast');
  v.key('Home');
  assert.equal(v.Viewer.index, 0);
});
test('E closes and fine-tunes, D downloads, Esc returns to the grid on the last picture', () => {
  const v = boot();
  v.Viewer.open(2);
  v.key('Process', {code: 'KeyD'});
  assert.equal(v.$('#vwDownload').clicked, 1);
  v.key('Process', {code: 'KeyE'});
  assert.equal(v.$('#lb').hidden, true);
  assert.deepEqual(v.log.slice(-2), ['cursor 2', 'edit 2']);
  v.Viewer.open(3);
  assert.equal(v.$('#vwEdit').disabled, true);
  v.key('Escape');
  assert.equal(v.$('#lb').hidden, true);
  assert.equal(v.log.at(-1), 'cursor 3');
});
test('keys are ignored while closed or while typing', () => {
  const v = boot();
  v.key('ArrowDown');
  assert.equal(v.state.sel.size, 5);
  v.Viewer.open(0);
  v.key('ArrowDown', {target: {tagName: 'INPUT', type: 'text'}});
  assert.equal(v.state.sel.size, 5);
  v.key('ArrowDown', {ctrlKey: true});
  assert.equal(v.state.sel.size, 5);
});

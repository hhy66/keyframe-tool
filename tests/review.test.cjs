const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function boot({count = 6, columns = 3} = {}) {
  const nodes = new Map();
  const node = () => ({
    hidden: false,
    disabled: false,
    textContent: '',
    open: false,
    classList: {toggle() {}},
    scrollIntoView() {},
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
  $('#collagePanel').hidden = true;
  $('#storageManager').hidden = true;
  $('#dl').hidden = true;
  const log = [];
  const cards = Array.from({length: count}, (_, i) => ({
    item: {classList: {toggle() {}}, scrollIntoView() {}},
    cap: {querySelector: () => null, appendChild() {}},
    toggle() {
      log.push('toggle ' + i);
    },
    edit: {
      disabled: false,
      click() {
        log.push('edit ' + i);
      },
    },
  }));
  const state = {
    sid: 's',
    resultRun: 'r',
    cuts: cards,
    cards,
    sel: new Set([0, 1, 2, 3, 4, 5]),
    cursor: null,
    lbIndex: null,
  };
  const context = vm.createContext({
    state,
    document: {querySelector: () => null},
    toast() {},
    log,
    getComputedStyle: () => ({gridTemplateColumns: Array(columns).fill('100px').join(' ')}),
    openLb: i => {
      state.lbIndex = i;
      $('#lb').hidden = false;
      log.push('open ' + i);
    },
    setKept: (ids, keep) => {
      ids.forEach(i => (keep ? state.sel.add(i) : state.sel.delete(i)));
      log.push('setKept ' + ids.join(',') + ' ' + keep);
    },
  });
  vm.runInContext(fs.readFileSync('static/review.js', 'utf8'), context);
  const Review = vm.runInContext('Review', context);
  Review.el = $;
  const key = (k, extra = {}) => {
    const e = {
      key: k,
      target: {tagName: 'BODY'},
      preventDefault() {
        this.prevented = true;
      },
      ...extra,
    };
    Review.onKey(e);
    return e;
  };
  return {Review, state, log, key, $};
}

test('only shots at or above the threshold are flagged', () => {
  const {Review} = boot();
  assert.equal(JSON.stringify(Review.flags([null, 95, 40, 80, 79.9, null], 80)), '[1,3]');
});
test('arrows move by one, up and down move by a row of the grid', () => {
  const r = boot();
  r.key('ArrowRight');
  assert.equal(r.state.cursor, 0);
  r.key('ArrowDown');
  assert.equal(r.state.cursor, 3);
  r.key('ArrowRight');
  r.key('ArrowRight');
  r.key('ArrowRight');
  assert.equal(r.state.cursor, 5);
  r.key('ArrowUp');
  assert.equal(r.state.cursor, 2);
});
test('space toggles, Enter opens the picture, E starts fine-tuning; the open viewer owns the keys', () => {
  const r = boot();
  r.key('ArrowRight');
  r.key('ArrowRight');
  assert.equal(r.key(' ').prevented, true);
  r.key('e');
  r.key('Enter');
  assert.equal(r.log.join('|'), 'toggle 1|edit 1|open 1');
  // With the full-screen review open, the grid shortcuts stay quiet (viewer.js handles the keys).
  r.key('ArrowRight');
  r.key(' ');
  assert.equal(r.state.cursor, 1);
  assert.equal(r.log.join('|'), 'toggle 1|edit 1|open 1');
});
test('shortcuts stay out of the way while typing or when another panel is open', () => {
  const r = boot();
  r.key('ArrowRight', {target: {tagName: 'INPUT', type: 'text'}});
  assert.equal(r.state.cursor, null);
  r.key('ArrowRight', {ctrlKey: true});
  assert.equal(r.state.cursor, null);
  r.$('#collagePanel').hidden = false;
  r.key('ArrowRight');
  assert.equal(r.state.cursor, null);
  r.$('#collagePanel').hidden = true;
  r.key('ArrowRight');
  assert.equal(r.state.cursor, 0);
});
test('skipping similar shots only touches flagged photos that are still kept', () => {
  const r = boot();
  r.Review.flagged = [1, 4];
  r.state.sel.delete(4);
  r.Review.skipSimilar();
  assert.equal(r.log.at(-1), 'setKept 1 false');
  assert.equal(r.$('#btnSkipSimilar').disabled, true);
});

test('the similar-shot button stays visible and says when nothing similar was found', () => {
  const r = boot();
  r.Review.checked = true;
  r.Review.flagged = [];
  r.Review.updateSkipButton();
  const button = r.$('#btnSkipSimilar');
  assert.equal(button.hidden, false);
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, '未发现相似画面');
  r.Review.checked = false;
  r.Review.updateSkipButton();
  assert.equal(button.hidden, true);
});
test('space and Enter keep their meaning on a focused button, arrows still move', () => {
  const r = boot();
  r.key('ArrowRight');
  const button = {tagName: 'BUTTON'};
  assert.equal(r.key(' ', {target: button}).prevented, undefined);
  assert.equal(r.key('Enter', {target: button}).prevented, undefined);
  assert.equal(r.log.length, 0);
  r.key('ArrowRight', {target: button});
  assert.equal(r.state.cursor, 1);
});
test('letter shortcuts work while a Chinese input method is on', () => {
  const r = boot();
  r.key('ArrowRight');
  r.key('Process', {code: 'KeyE'});
  r.key('Process', {code: 'KeyX'});
  assert.equal(r.log.join('|'), 'edit 0|toggle 0');
});
test('after clicking a checkbox and moving, Space acts on the blue card', () => {
  const r = boot();
  r.key('ArrowRight');
  r.key('ArrowRight');
  assert.equal(r.key(' ', {target: {tagName: 'INPUT', type: 'checkbox'}}).prevented, true);
  assert.equal(r.log.join('|'), 'toggle 1');
});

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load() {
  const context = vm.createContext({document: {querySelector: () => null}});
  vm.runInContext(fs.readFileSync('static/overview.js', 'utf8'), context);
  return context;
}
const shots = lengths => {
  let start = 0;
  return lengths.map(duration => {
    const shot = {start, end: start + duration, duration};
    start += duration;
    return shot;
  });
};

test('pace from the average shot length', () => {
  const {rhythmSummary} = load();
  assert.equal(rhythmSummary(shots([1, 1.5, 1.2])).pace, '快节奏（快切）');
  assert.equal(rhythmSummary(shots([3, 4, 2.5])).pace, '中等节奏');
  assert.equal(rhythmSummary(shots([8, 12])).pace, '慢节奏（长镜头为主）');
  assert.match(rhythmSummary(shots([3, 4, 2.5])).headline, /^共 3 个镜头 · 平均 3\.2 秒一个 · 中等节奏$/);
  assert.equal(rhythmSummary([null]), null);
});

test('speeding up, a run of quick cuts and the longest shot are called out', () => {
  const {rhythmSummary} = load();
  const summary = rhythmSummary(shots([4, 3.5, 4, 3, 0.6, 0.5, 0.7, 0.4, 0.5, 0.6, 0.5]));
  const notes = JSON.parse(JSON.stringify(summary.notes));
  assert.match(notes[0], /^越往后越快：开头平均 3\.\d 秒一个镜头，结尾 0\.\d 秒$/);
  assert.equal(notes[1], '第 5–11 个镜头连续快切：7 个镜头都不到 1 秒');
  assert.equal(notes[2], '最长的是第 1 个镜头，4.0 秒');
  const slower = rhythmSummary(shots([1, 1, 1, 1, 1, 1, 4, 5, 6, 5]));
  assert.match(slower.notes[0], /^越往后越慢/);
});

test('shots out of order are read in time order', () => {
  const {rhythmSummary} = load();
  const list = shots([2, 6, 2]);
  assert.equal(rhythmSummary([list[2], list[0], list[1]]).notes.at(-1), '最长的是第 2 个镜头，6.0 秒');
});

test('tally counts values, most common first, ignoring blanks', () => {
  const {tally} = load();
  assert.deepEqual(JSON.parse(JSON.stringify(tally(['近景', '特写', '', '近景', null]))), [
    ['近景', 2],
    ['特写', 1],
  ]);
});

test('the page has the overview and its filter banner', () => {
  const html = fs.readFileSync('static/index.html', 'utf8');
  for (const id of ['overview', 'overviewHeadline', 'overviewBody', 'overviewTip', 'overviewFilter', 'overviewClear'])
    assert.match(html, new RegExp(`id="${id}"`), id);
  assert.match(html, /<script src="\/overview\.js"><\/script>/);
});

const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load() {
  const context = vm.createContext({document: {querySelector: () => null}});
  vm.runInContext(fs.readFileSync('static/corrections.js', 'utf8'), context);
  return context;
}

test('a saved correction reads as one short line', () => {
  const {fixSummary} = load();
  assert.equal(fixSummary(null), '');
  assert.equal(fixSummary({}), '');
  assert.equal(
    fixSummary({shot: '特写', depth: 'ok', cut: 'unsure', moves: ['推近', '右摇/移'], note: '其实是逆光'}),
    '景别 → 特写 · 景深 → ✓ 对 · 镜头切分 → 说不清 · 运镜 → 推近、右摇/移 · 有描述',
  );
  assert.equal(fixSummary({moves: ['ok']}), '运镜 → ✓ 对');
});

test('the measured camera movement in the words of the steadiness and speed choices', () => {
  const {motionReading} = load();
  assert.deepEqual({...motionReading(null)}, {steady: '', speed: ''});
  assert.deepEqual({...motionReading({label: '', text: '无法判断运镜'})}, {steady: '', speed: ''});
  assert.deepEqual({...motionReading({label: '固定', text: '固定机位'})}, {steady: '很稳', speed: '没有移动'});
  assert.deepEqual({...motionReading({label: '推近', text: '缓慢推近'})}, {steady: '很稳', speed: '缓慢'});
  assert.deepEqual(
    {...motionReading({label: '右摇/移', text: '快速向右摇（或横移）；手持明显晃动'})},
    {steady: '明显晃动', speed: '快速'},
  );
  assert.deepEqual(
    {...motionReading({label: '手持', text: '手持，轻微晃动'})},
    {steady: '稳，有轻微浮动', speed: '没有移动'},
  );
});

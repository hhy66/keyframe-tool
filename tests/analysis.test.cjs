const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load() {
  const context = vm.createContext({document: {querySelector: () => null}});
  vm.runInContext(fs.readFileSync('static/analysis.js', 'utf8'), context);
  return context;
}
const item = {
  size: [1920, 1080],
  frame: {aspect: '2.39:1', content_ratio: 2.39, orientation: '横屏', bars: {top: 140, bottom: 140, left: 0, right: 0}},
  color: {
    palette: [{hex: '#e68c3c', share: 0.6, name: '橙'}],
    temperature: {label: '暖调', note: ''},
    saturation: {label: '高饱和'},
    harmony: {label: '青橙对比', note: '青与橙'},
  },
  tone: {key: '低调', contrast: '高对比', brightness: 0.21, highlights: 0, shadows: 0.05},
  light: {label: '左亮右暗', grid: []},
  shot: {label: '近景', basis: '主体人脸高度约占画面 25%', people: 2},
  composition: {
    label: '三分法构图',
    position: '水平左三分线 · 垂直上三分线',
    space: '',
    symmetry: 0.4,
    basis: '按人脸位置判断',
  },
  level: {label: '水平'},
  depth: {label: '浅景深', basis: '主体清晰、背景明显柔和'},
};

test('panel rows describe a letterboxed, low-key frame in plain words', () => {
  const rows = JSON.parse(JSON.stringify(load().analysisRows(item)));
  assert.deepEqual(rows.frame[0], ['比例', '2.39:1 宽银幕 · 横屏 · 上下有黑边']);
  assert.deepEqual(rows.color[2], ['色彩关系', '青橙对比：青与橙']);
  assert.deepEqual(rows.tone[0], ['类型', '低调 · 高对比']);
  assert.deepEqual(rows.tone[1], ['平均亮度', '21%']);
  assert.deepEqual(rows.tone[2], ['细节损失', '欠曝 5%']);
  assert.deepEqual(rows.light, [['结论', '左亮右暗']]);
  assert.deepEqual(rows.shot, [
    ['景别', '近景'],
    ['人数', '约 2 人'],
    ['依据', '主体人脸高度约占画面 25%'],
  ]);
  assert.deepEqual(
    rows.composition.map(([label]) => label),
    ['构图', '主体位置', '依据'],
    'empty rows such as 留白 are left out',
  );
  assert.deepEqual(rows.lens[1], ['景深', '浅景深']);
});

test('no person: the shot size says it cannot tell instead of guessing', () => {
  const rows = JSON.parse(
    JSON.stringify(load().analysisRows({...item, shot: {label: '', basis: '未检测到人物', people: 0}})),
  );
  assert.deepEqual(rows.shot, [
    ['景别', '无法判断'],
    ['依据', '未检测到人物'],
  ]);
});

test('no clipping row when nothing is lost', () => {
  const rows = load().analysisRows({...item, tone: {...item.tone, shadows: 0.01}});
  assert.equal(rows.tone.length, 2);
});

test('the page has the button, the viewer toggle and the panel', () => {
  const html = fs.readFileSync('static/index.html', 'utf8');
  for (const id of ['btnAnalyze', 'vwInfo', 'vwAnalysis', 'vwGuideBtn', 'vwGuides'])
    assert.match(html, new RegExp(`id="${id}"`), id);
  assert.match(html, /<script src="\/analysis\.js"><\/script>/);
});

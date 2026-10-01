const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function load() {
  const context = vm.createContext({document: {querySelector: () => null}});
  vm.runInContext(fs.readFileSync('static/clip.js', 'utf8'), context);
  return context;
}
// 50 frames at 25 fps starting at 4 s, as shots.spans() describes them.
const span = {start_frame: 100, end_frame: 149, frames: 50, start: 4, end: 6, duration: 2, strip: [100, 124, 149]};

test('seeks land inside a frame and read back as the same frame', () => {
  const {clipFrames} = load();
  const frames = clipFrames(span);
  assert.equal(frames.count, 50);
  assert.equal(frames.step, 0.04);
  for (const k of [0, 1, 24, 49]) assert.equal(frames.index(frames.at(k)), k);
  assert.ok(frames.at(0) > span.start, 'never on the edge with the previous shot');
  assert.ok(frames.at(49) < span.end, 'never on the edge with the next shot');
});

test('frames outside the shot are held at its ends', () => {
  const {clipFrames} = load();
  const frames = clipFrames(span);
  assert.equal(frames.at(-3), frames.at(0));
  assert.equal(frames.at(80), frames.at(49));
  assert.equal(frames.index(3.5), 0);
  assert.equal(frames.index(6.2), 49);
});

test('first, middle and last frames are marked on the progress bar', () => {
  const {clipFrames} = load();
  assert.deepEqual([...clipFrames(span).marks], [0.01, 0.49, 0.99]);
  const single = clipFrames({...span, end_frame: 100, frames: 1, duration: 0.04, strip: [100, 100, 100]});
  assert.equal(single.count, 1);
  assert.deepEqual([...single.marks], [0.5, 0.5, 0.5]);
});

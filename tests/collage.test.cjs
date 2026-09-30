const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const plan = {
  width: 4000,
  height: 2000,
  cell_width: 2000,
  image_height: 1000,
  caption_height: 0,
  gap: 0,
  items: [],
  estimated_memory_bytes: 64000000,
  can_render: true,
  warning: '',
};
const artifact = {
  token: 'export1',
  width: 4000,
  height: 2000,
  format: 'png',
  bytes: 1000,
  preview_url: '/export/preview',
  image_url: '/export/image',
  download_url: '/export/download',
  plan,
};
function boot(handler) {
  const nodes = new Map(),
    calls = [],
    timers = new Map();
  let timer = 0;
  const node = () => ({
    value: '',
    hidden: false,
    disabled: false,
    checked: false,
    children: [],
    style: {},
    textContent: '',
    className: '',
    clientWidth: 600,
    classList: {add() {}, remove() {}, toggle() {}},
    addEventListener() {},
    setAttribute() {},
    append(...x) {
      this.children.push(...x);
    },
    appendChild(x) {
      this.children.push(x);
    },
    pause() {},
    click() {},
  });
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, node());
    return nodes.get(key);
  };
  const state = {
    sid: 'one',
    resultRun: 'run',
    sel: new Set([2, 0]),
    cuts: [0, 1, 2].map(i => ({label: `time${i}`, frame_index: i})),
    thumbs: ['t0', 't1', 't2'],
    frames: ['f0', 'f1', 'f2'],
  };
  const context = vm.createContext({
    console,
    state,
    document: {querySelector: $, createElement: node, body: node()},
    toast() {},
    setTimeout: fn => {
      timers.set(++timer, fn);
      return timer;
    },
    clearTimeout: id => timers.delete(id),
    jsonRequest: async (url, options = {}) => {
      calls.push({url, options});
      return handler
        ? handler(url, options)
        : url.endsWith('/plan')
          ? plan
          : url.endsWith('/render')
            ? artifact
            : {status: 'done'};
    },
    Image: class {
      constructor() {
        this.naturalWidth = 200;
        this.naturalHeight = 100;
      }
      async decode() {}
    },
  });
  vm.runInContext(fs.readFileSync('static/collage.js', 'utf8'), context);
  return {state, $, calls, context, run: code => vm.runInContext(code, context)};
}
const settle = () => new Promise(r => setImmediate(r));

test('independent selection starts in justified share mode with the card preset', () => {
  const a = boot();
  a.run('CollageUI.open();CollageUI.toggle(1);CollageUI.toggle(0)');
  assert.deepEqual([...a.state.sel], [2, 0]);
  assert.equal(a.run('CollageUI.ids.join(",")'), '2,1');
  assert.equal(a.$('#collageMode').value, 'share');
  assert.equal(a.$('#collageLayout').value, 'justified');
  assert.equal(a.$('#collageWidth').disabled, true);
  assert.equal(a.$('#collageGap').value, 'medium');
  assert.equal(a.$('#collageShadow').checked, true);
  assert.equal(a.$('#collageCanvasField').hidden, false);
  assert.equal(a.$('#collageFitField').hidden, true);
});
test('overflow never silently discards photos and partial page never duplicates', () => {
  const a = boot();
  assert.throws(() => a.run('CollageUI.pages([0,1,2,3,4],4,false)'), /超过/);
  assert.equal(a.run('JSON.stringify(CollageUI.pages([0,1,2,3,4],4,true))'), '[[0,1,2,3],[4]]');
  assert.throws(() => a.run('CollageUI.pages([0],5,true)'), /4、9、12 或 16/);
});
test('moving and direct swapping retain independent order', () => {
  const a = boot();
  a.run('CollageUI.open();CollageUI.toggle(1);CollageUI.move(1,0);CollageUI.swap(1,2)');
  assert.equal(a.run('CollageUI.ids.join(",")'), '2,0,1');
  assert.deepEqual([...a.state.sel], [2, 0]);
});
test('dimensions and memory limit come from the authoritative server plan', async () => {
  const a = boot(async () => ({...plan, can_render: false, warning: 'too large'}));
  a.run('CollageUI.open()');
  await a.run('CollageUI.refreshPlan()');
  assert.match(a.$('#collageDimensions').textContent, /4000 × 2000/);
  assert.equal(a.$('#collageRender').disabled, true);
});
test('original payload disables crop and additional labels without reducing source size', () => {
  const a = boot();
  a.run('CollageUI.open();CollageUI.crops={0:{x:.1,y:.1,width:.8,height:.8}}');
  a.$('#collageMode').value = 'original';
  a.$('#collageTitleText').value = '标题';
  a.$('#collageIndex').checked = true;
  a.$('#collageTime').checked = true;
  const p = a.run('CollageUI.payload()');
  assert.equal(p.mode, 'original');
  assert.equal(p.shape, 'source');
  assert.equal(p.index, false);
  assert.equal(JSON.stringify(p.crops), '{}');
  assert.equal(JSON.stringify(p.ids), '[0,2]');
  assert.equal(p.layout, 'grid');
  assert.equal(p.gap, 'none');
  assert.equal(p.radius, 'none');
  assert.equal(p.shadow, false);
  assert.equal(p.title, '');
});
test('final preview and download refer to server rendered files, not a browser export', async () => {
  const a = boot();
  a.run('CollageUI.open()');
  await a.run('CollageUI.generate()');
  assert.equal(a.$('#collageDownload').href, '/export/download');
  assert.equal(a.$('#collageDownload').disabled, false);
  assert.equal(a.$('#collagePreview').children.at(-1).src, '/export/preview');
  await a.run('CollageUI.finalView(true)');
  assert.equal(a.$('#collagePreview').children.at(-1).src, '/export/image');
  assert.match(a.$('#collagePreview').children.at(-1).className, /actual/);
});
test('changed selection invalidates output and deletes only its temporary artifact', async () => {
  const a = boot();
  a.run('CollageUI.open()');
  await a.run('CollageUI.generate()');
  a.run('CollageUI.toggle(1)');
  await settle();
  assert.equal(a.run('CollageUI.output'), null);
  assert.equal(a.$('#collageDownload').disabled, true);
  assert.ok(a.calls.some(c => c.url === '/api/collage/one/export1' && c.options.method === 'DELETE'));
});
test('stale result or missing image returned by renderer cannot publish a partial image', async () => {
  const a = boot(async () => {
    throw Error('结果已更新');
  });
  a.run('CollageUI.open()');
  await a.run('CollageUI.generate()');
  assert.equal(a.run('CollageUI.output'), null);
  assert.equal(a.$('#collageDownload').disabled, true);
  assert.match(a.$('#collageStatus').textContent, /结果已更新/);
});
test('obsolete render response is cleaned rather than replacing current state', async () => {
  let resolve;
  const a = boot(url => (url.endsWith('/render') ? new Promise(r => (resolve = r)) : {}));
  a.run('CollageUI.open()');
  const render = a.run('CollageUI.generate()');
  a.run('CollageUI.toggle(1)');
  resolve(artifact);
  await render;
  assert.equal(a.run('CollageUI.output'), null);
  assert.ok(a.calls.some(c => c.options.method === 'DELETE'));
});
test('multi-page request sends only the final page photo and correct page number', async () => {
  const a = boot();
  a.state.cuts = Array.from({length: 9}, (_, i) => ({label: `t${i}`}));
  a.state.frames = Array(9).fill('frame');
  a.state.thumbs = Array(9).fill('thumb');
  a.state.sel = new Set([0, 1, 2, 3, 4, 5, 6, 7, 8]);
  a.run('CollageUI.open()');
  a.$('#collagePerPage').value = '4';
  a.$('#collageSplit').checked = true;
  a.run('CollageUI.page=2');
  await a.run('CollageUI.generate()');
  const call = a.calls.find(c => c.url.endsWith('/render'));
  const data = JSON.parse(call.options.body);
  assert.deepEqual(data.ids, [8]);
  assert.equal(data.page, 3);
  assert.equal(a.state.sel.size, 9);
});
test('crop state stays attached to source id when images are reordered', () => {
  const a = boot();
  a.run('CollageUI.open()');
  a.$('#collageMode').value = 'custom';
  a.$('#collageShape').value = 'square';
  a.$('#collageFit').value = 'cover';
  a.run('CollageUI.setCrop(0,{x:.25,y:0,width:.5,height:1});CollageUI.swap(0,2)');
  const data = a.run('CollageUI.payload()');
  assert.equal(data.crops['0'].x, 0.25);
  assert.equal(JSON.stringify(data.ids), '[2,0]');
});
test('style presets fill the controls and manual changes switch to custom', () => {
  const a = boot();
  a.run('CollageUI.open()');
  a.$('#collagePreset').value = 'gallery';
  a.run("CollageUI.settingChanged('#collagePreset')");
  assert.equal(a.$('#collageBackground').value, 'dark');
  assert.equal(a.$('#collageShadow').checked, false);
  a.$('#collageGap').value = 'large';
  a.run("CollageUI.settingChanged('#collageGap')");
  assert.equal(a.$('#collagePreset').value, 'custom');
  a.$('#collageTitleText').value = '  镜头参考  ';
  const p = a.run('CollageUI.payload()');
  assert.equal(p.gap, 'large');
  assert.equal(p.radius, 'small');
  assert.equal(p.background, 'dark');
  assert.equal(p.title, '镜头参考');
  assert.equal(p.canvas, 'auto');
});
test('cropper result is normalised to the source and a full frame clears the crop', async () => {
  const a = boot();
  a.run('CollageUI.open()');
  a.$('#cropImage').decode = async () => {};
  a.context.Cropper = class {
    constructor(img, options) {
      this.options = options;
      a.context.lastCropper = this;
    }
    getImageData() {
      return {naturalWidth: 200, naturalHeight: 100};
    }
    getData() {
      return this.data;
    }
    setData() {}
    destroy() {
      this.destroyed = true;
    }
  };
  await a.run('CollageUI.openCrop(0)');
  assert.equal(a.$('#cropDialog').hidden, false);
  assert.ok(Number.isNaN(a.run('lastCropper.options.aspectRatio')));
  a.run('lastCropper.data={x:50,y:10,width:100,height:80};CollageUI.applyCrop()');
  assert.equal(JSON.stringify(a.run('CollageUI.crops[0]')), JSON.stringify({x: 0.25, y: 0.1, width: 0.5, height: 0.8}));
  assert.equal(a.$('#cropDialog').hidden, true);
  assert.equal(a.run('lastCropper.destroyed'), true);
  await a.run('CollageUI.openCrop(0)');
  a.run('lastCropper.data={x:0,y:0,width:200,height:100};CollageUI.applyCrop()');
  assert.equal(a.run('CollageUI.crops[0]'), undefined);
});
test('grid cover locks the cropper to the cell ratio; original mode refuses to crop', async () => {
  const a = boot();
  a.run('CollageUI.open()');
  a.$('#cropImage').decode = async () => {};
  a.context.lastOptions = null;
  a.context.Cropper = class {
    constructor(img, options) {
      a.context.lastOptions = options;
    }
    destroy() {}
  };
  a.$('#collageLayout').value = 'grid';
  a.$('#collageFit').value = 'cover';
  a.run('CollageUI.plan={items:[{id:0,cell:{width:300,height:200}}]}');
  await a.run('CollageUI.openCrop(0)');
  assert.equal(a.run('lastOptions.aspectRatio'), 1.5);
  a.run('CollageUI.closeCrop();lastOptions=null');
  a.$('#collageMode').value = 'original';
  await a.run('CollageUI.openCrop(0)');
  assert.equal(a.run('lastOptions'), null);
  assert.equal(a.$('#cropDialog').hidden, true);
});
test('settings are remembered, invalid saved values fall back to defaults', () => {
  const store = {};
  const a = boot();
  a.context.window = {
    localStorage: {
      getItem: k => store[k] ?? null,
      setItem: (k, v) => {
        store[k] = v;
      },
    },
  };
  a.run('CollageUI.open()');
  assert.equal(a.$('#collageLabelPosition').value, 'tr');
  assert.equal(a.$('#collagePerPage').value, '9');
  a.$('#collagePerPage').value = '16';
  a.$('#collageLabelPosition').value = 'below';
  a.run("CollageUI.settingChanged('#collagePerPage')");
  const saved = JSON.parse(store['keyframe-tool.collage.settings.v1']);
  assert.equal(saved['#collagePerPage'], '16');
  saved['#collageMode'] = 'bogus';
  store['keyframe-tool.collage.settings.v1'] = JSON.stringify(saved);
  a.run('CollageUI.snapshot=null;CollageUI.open()');
  assert.equal(a.$('#collagePerPage').value, '16');
  assert.equal(a.$('#collageLabelPosition').value, 'below');
  assert.equal(a.$('#collageMode').value, 'share');
  const p = a.run('CollageUI.payload()');
  assert.equal(p.per_page, 16);
  assert.equal(p.label_position, 'below');
});
test('notes are trimmed into the payload, filtered per page and ignored in original mode', async () => {
  const a = boot();
  a.run('CollageUI.open()');
  await settle();
  a.run("CollageUI.setNote(0,'  航拍  ');CollageUI.setNote(2,'   ')");
  let p = a.run('CollageUI.payload()');
  assert.equal(JSON.stringify(p.notes), JSON.stringify({0: '航拍'}));
  a.$('#collageMode').value = 'original';
  p = a.run('CollageUI.payload()');
  assert.equal(JSON.stringify(p.notes), '{}');
});
test('the draft is saved to the workspace keyed by the detected frame and restored onto the right photos', async () => {
  const saved = {
    draft: {
      ids: ['2', '0'],
      crops: {0: {x: 0.25, y: 0, width: 0.5, height: 1}},
      notes: {2: '远景', 99: '已不在结果里'},
    },
  };
  const a = boot(url => (url.endsWith('/collage') ? saved : {}));
  a.state.cuts = [
    {label: 't0', frame_index: 10, source_frame: 0},
    {label: 't1', frame_index: 40},
    {label: 't2', frame_index: 70, source_frame: 2},
  ];
  a.run('CollageUI.open()');
  await settle();
  await settle();
  assert.equal(a.run('CollageUI.ids.join(",")'), '2,0');
  assert.equal(a.run('CollageUI.notes[2]'), '远景');
  assert.equal(a.run('CollageUI.crops[0].x'), 0.25);
  a.run("CollageUI.setNote(1,'特写')");
  await a.run('CollageUI.saveDraft()');
  const put = a.calls.filter(c => c.url === '/api/workspace/one/collage' && c.options.method === 'PUT').at(-1);
  const body = JSON.parse(put.options.body).draft;
  assert.deepEqual(body.ids, ['2', '0']);
  assert.deepEqual(body.notes, {99: '已不在结果里', 2: '远景', 40: '特写'});
  assert.deepEqual(Object.keys(body.crops), ['0']);
});
test('nothing is saved before the draft is read, and changes made meanwhile win', async () => {
  let release;
  const saved = {draft: {ids: ['2'], crops: {}, notes: {0: '旧备注'}}};
  const a = boot((url, options) =>
    url.endsWith('/collage') && !options.method ? new Promise(r => (release = () => r(saved))) : {},
  );
  a.run('CollageUI.open()');
  a.run("CollageUI.toggle(1);CollageUI.setNote(0,'新备注')");
  await a.run('CollageUI.saveDraft()');
  assert.equal(a.calls.filter(c => c.options.method === 'PUT').length, 0);
  release();
  await settle();
  await settle();
  assert.equal(a.run('CollageUI.ids.join(",")'), '0,2,1');
  assert.equal(a.run('CollageUI.notes[0]'), '新备注');
  await a.run('CollageUI.saveDraft()');
  assert.equal(a.calls.filter(c => c.options.method === 'PUT').length, 1);
});
test('notes kept in this browser by the previous version move into the workspace once', async () => {
  const store = {'keyframe-tool.collage.notes.one.run': JSON.stringify({1: '浏览器里的备注'})};
  const a = boot();
  a.context.window = {
    localStorage: {
      getItem: k => store[k] ?? null,
      setItem: (k, v) => {
        store[k] = v;
      },
      removeItem: k => {
        delete store[k];
      },
    },
  };
  a.run('CollageUI.open()');
  await settle();
  assert.equal(a.run('CollageUI.notes[1]'), '浏览器里的备注');
  await a.run('CollageUI.saveDraft()');
  const put = a.calls.find(c => c.options.method === 'PUT');
  assert.equal(JSON.parse(put.options.body).draft.notes['1'], '浏览器里的备注');
  assert.equal(store['keyframe-tool.collage.notes.one.run'], undefined);
});
test('rounded corners are disabled with no gap and the reason is shown', () => {
  const a = boot();
  a.run('CollageUI.open()');
  a.$('#collageGap').value = 'none';
  a.run("CollageUI.settingChanged('#collageGap')");
  assert.equal(a.$('#collageRadius').disabled, true);
  assert.equal(a.$('#collageRadiusNote').hidden, false);
  assert.equal(a.$('#collagePreset').value, 'custom');
});
test('download all sends every selected photo once and hands the zip to the browser', async () => {
  const a = boot(url =>
    url.endsWith('/render-all') ? {token: 'z', pages: 2, bytes: 2048, download_url: '/zip/download'} : {},
  );
  a.state.cuts = Array.from({length: 6}, (_, i) => ({label: `t${i}`}));
  a.state.frames = Array(6).fill('frame');
  a.state.thumbs = Array(6).fill('thumb');
  a.state.sel = new Set([5, 1, 3, 0, 2, 4]);
  a.run('CollageUI.open()');
  a.$('#collagePerPage').value = '4';
  a.$('#collageSplit').checked = false;
  a.run("CollageUI.setNote(4,'结尾')");
  let clicked = 0;
  a.$('#collageBatchLink').click = () => {
    clicked++;
  };
  await a.run('CollageUI.downloadAll()');
  const call = a.calls.find(c => c.url.endsWith('/render-all'));
  const body = JSON.parse(call.options.body);
  assert.deepEqual(body.ids, [0, 1, 2, 3, 4, 5]);
  assert.equal(body.per_page, 4);
  assert.equal(body.notes['4'], '结尾');
  assert.equal(a.$('#collageBatchLink').href, '/zip/download');
  assert.equal(clicked, 1);
  assert.match(a.$('#collageStatus').textContent, /2 张拼图/);
  assert.equal(a.run('CollageUI.busy'), false);
});

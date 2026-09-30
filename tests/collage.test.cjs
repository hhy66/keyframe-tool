const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const plan={width:4000,height:2000,cell_width:2000,image_height:1000,caption_height:0,gap:0,items:[],estimated_memory_bytes:64000000,can_render:true,warning:''};
const artifact={token:'export1',width:4000,height:2000,format:'png',bytes:1000,preview_url:'/export/preview',image_url:'/export/image',download_url:'/export/download',plan};
function boot(handler){
  const nodes=new Map(),calls=[],timers=new Map();let timer=0;
  const node=()=>({value:'',hidden:false,disabled:false,checked:false,children:[],style:{},textContent:'',className:'',clientWidth:600,
    classList:{add(){},remove(){},toggle(){}},addEventListener(){},setAttribute(){},append(...x){this.children.push(...x);},appendChild(x){this.children.push(x);},pause(){},click(){}});
  const $=key=>{if(!nodes.has(key))nodes.set(key,node());return nodes.get(key);};
  const state={sid:'one',resultRun:'run',sel:new Set([2,0]),cuts:[0,1,2].map(i=>({label:`time${i}`,frame_index:i})),thumbs:['t0','t1','t2'],frames:['f0','f1','f2']};
  const context=vm.createContext({console,state,document:{querySelector:$,createElement:node,body:node()},toast(){},
    setTimeout:fn=>{timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id),
    jsonRequest:async(url,options={})=>{calls.push({url,options});return handler?handler(url,options):(url.endsWith('/plan')?plan:url.endsWith('/render')?artifact:{status:'done'});},
    Image:class{constructor(){this.naturalWidth=200;this.naturalHeight=100;}async decode(){}}});
  vm.runInContext(fs.readFileSync('static/collage.js','utf8'),context);
  return {state,$,calls,context,run:code=>vm.runInContext(code,context)};
}
const settle=()=>new Promise(r=>setImmediate(r));

test('independent selection starts in unscaled original mode',()=>{
  const a=boot();a.run('CollageUI.open();CollageUI.toggle(1);CollageUI.toggle(0)');
  assert.deepEqual([...a.state.sel],[2,0]);assert.equal(a.run('CollageUI.ids.join(",")'),'2,1');
  assert.equal(a.$('#collageMode').value,'original');assert.equal(a.$('#collageWidth').disabled,true);
});
test('overflow never silently discards photos and partial page never duplicates',()=>{
  const a=boot();assert.throws(()=>a.run('CollageUI.pages([0,1,2,3,4],2,false)'),/超过/);
  assert.equal(a.run('JSON.stringify(CollageUI.pages([0,1,2,3,4],2,true))'),'[[0,1,2,3],[4]]');
});
test('moving and direct swapping retain independent order',()=>{
  const a=boot();a.run('CollageUI.open();CollageUI.toggle(1);CollageUI.move(1,0);CollageUI.swap(1,2)');
  assert.equal(a.run('CollageUI.ids.join(",")'),'2,0,1');assert.deepEqual([...a.state.sel],[2,0]);
});
test('dimensions and memory limit come from the authoritative server plan',async()=>{
  const a=boot(async()=>({...plan,can_render:false,warning:'too large'}));a.run('CollageUI.open()');await a.run('CollageUI.refreshPlan()');
  assert.match(a.$('#collageDimensions').textContent,/4000 × 2000/);assert.equal(a.$('#collageRender').disabled,true);
});
test('original payload disables crop and additional labels without reducing source size',()=>{
  const a=boot();a.run('CollageUI.open();CollageUI.crops={0:{x:.1,y:.1,width:.8,height:.8}}');
  a.$('#collageIndex').checked=true;a.$('#collageTime').checked=true;
  const p=a.run('CollageUI.payload()');assert.equal(p.mode,'original');assert.equal(p.shape,'source');assert.equal(p.index,false);
  assert.equal(JSON.stringify(p.crops),'{}');assert.equal(JSON.stringify(p.ids),'[0,2]');
});
test('final preview and download refer to server rendered files, not a browser export',async()=>{
  const a=boot();a.run('CollageUI.open()');await a.run('CollageUI.generate()');
  assert.equal(a.$('#collageDownload').href,'/export/download');assert.equal(a.$('#collageDownload').disabled,false);
  assert.equal(a.$('#collagePreview').children.at(-1).src,'/export/preview');
  await a.run('CollageUI.finalView(true)');assert.equal(a.$('#collagePreview').children.at(-1).src,'/export/image');
  assert.match(a.$('#collagePreview').children.at(-1).className,/actual/);
});
test('changed selection invalidates output and deletes only its temporary artifact',async()=>{
  const a=boot();a.run('CollageUI.open()');await a.run('CollageUI.generate()');a.run('CollageUI.toggle(1)');await settle();
  assert.equal(a.run('CollageUI.output'),null);assert.equal(a.$('#collageDownload').disabled,true);
  assert.ok(a.calls.some(c=>c.url==='/api/collage/one/export1'&&c.options.method==='DELETE'));
});
test('stale result or missing image returned by renderer cannot publish a partial image',async()=>{
  const a=boot(async()=>{throw Error('结果已更新');});a.run('CollageUI.open()');await a.run('CollageUI.generate()');
  assert.equal(a.run('CollageUI.output'),null);assert.equal(a.$('#collageDownload').disabled,true);assert.match(a.$('#collageStatus').textContent,/结果已更新/);
});
test('obsolete render response is cleaned rather than replacing current state',async()=>{
  let resolve;const a=boot((url)=>url.endsWith('/render')?new Promise(r=>resolve=r):{});
  a.run('CollageUI.open()');const render=a.run('CollageUI.generate()');a.run('CollageUI.toggle(1)');resolve(artifact);await render;
  assert.equal(a.run('CollageUI.output'),null);assert.ok(a.calls.some(c=>c.options.method==='DELETE'));
});
test('multi-page request sends only the final page photo and correct page number',async()=>{
  const a=boot();a.state.cuts=Array.from({length:9},(_,i)=>({label:`t${i}`}));a.state.frames=Array(9).fill('frame');a.state.thumbs=Array(9).fill('thumb');a.state.sel=new Set([0,1,2,3,4,5,6,7,8]);
  a.run('CollageUI.open()');a.$('#collageSplit').checked=true;a.run('CollageUI.page=2');await a.run('CollageUI.generate()');
  const call=a.calls.find(c=>c.url.endsWith('/render'));const data=JSON.parse(call.options.body);
  assert.deepEqual(data.ids,[8]);assert.equal(data.page,3);assert.equal(a.state.sel.size,9);
});
test('crop state stays attached to source id when images are reordered',()=>{
  const a=boot();a.run('CollageUI.open()');a.$('#collageMode').value='custom';a.$('#collageShape').value='square';a.$('#collageFit').value='cover';
  a.run('CollageUI.setCrop(0,{x:.25,y:0,width:.5,height:1});CollageUI.swap(0,2)');
  const data=a.run('CollageUI.payload()');assert.equal(data.crops['0'].x,.25);assert.equal(JSON.stringify(data.ids),'[2,0]');
});

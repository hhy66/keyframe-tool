const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('static/index.html', 'utf8');
const script = fs.existsSync('static/app.js') ? fs.readFileSync('static/app.js', 'utf8') : html.match(/<script>([\s\S]*?)<\/script>/)[1];

function boot({saved = {}, response, fetchImpl} = {}) {
  const elements = new Map();
  function element() {
    return {hidden: true, disabled: false, style: {}, textContent: '', value: '', checked: false,
      children: [], events: {}, className: '', classList: {add(){}, remove(){}, toggle(){}},
      append(...els){this.children.push(...els);}, appendChild(el){this.children.push(el);},
      addEventListener(name, fn){this.events[name] = fn;}, querySelector(){return element();},
      click(){this.events.click?.({stopPropagation(){}});}, remove(){}, setAttribute(){},
      pause(){this.paused=true;}, load(){}, scrollIntoView(){}, paused:true, currentTime:0};
  }
  const $ = selector => {if(!elements.has(selector)) elements.set(selector, element()); return elements.get(selector);};
  const store = new Map(Object.entries(saved));
  const timers = new Map(); let timerId = 0;
  const context = vm.createContext({console, Set, Map, URL, AbortController, FormData,
    Image:class {async decode(){}},
    document: {querySelector:$,querySelectorAll:()=>[],createElement:element,addEventListener(){},body:element()},
    sessionStorage: {getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)},
    setTimeout:fn=>{timers.set(++timerId,fn);return timerId;}, clearTimeout:id=>timers.delete(id),
    setInterval:fn=>{timers.set(++timerId,fn);return timerId;}, clearInterval:id=>timers.delete(id),
    fetch:fetchImpl || (async()=>({ok:true,status:200,json:async()=>response})),
  });
  vm.runInContext(script,context);
  return {$, context, store, timers, run:source=>vm.runInContext(source,context)};
}

const result = {run:'r1',video_name:'example.avi',params:{sensitivity:75,include_ends:true,min_scene_seconds:0.2,suppress_flash:false},
  meta:{width:90,height:160,duration:2,frames:60,size_mb:1},
  cuts:[{kind:'head',frame_index:0,time:0,label:'00:00:00.000'}, {kind:'cut',frame_index:30,time:1,label:'00:00:01.000'}],
  thumbs:['/thumb/0','/thumb/1'],frames:['/frame/0','/frame/1']};
const done = {status:'done',run:'r1',result,params:result.params,meta:result.meta,video_name:result.video_name,max_export:500};
const settle = () => new Promise(resolve => setImmediate(resolve));

test('refresh restores metadata, controls and excluded frames',async()=>{
  const app = boot({saved:{kf_sid:'s1',kf_skipped_s1:'[30]'},response:done});
  await settle();
  assert.equal(app.$('#cardParams').hidden,false);
  assert.equal(app.$('#metaBox').style.display,'block');
  assert.equal(app.$('#mName').textContent,'example.avi');
  assert.equal(Number(app.$('#sens').value),75);
  assert.equal(app.$('#chkEnds').checked,true);
  assert.equal(app.run('state.sel.size'),1);
});

test('refresh of a running job shows progress and parameters',async()=>{
  const app=boot({saved:{kf_sid:'s1'},response:{...done,status:'running',pct:42,result:null,stage:'scanning'}});
  await settle();
  assert.equal(app.$('#cardProgress').hidden,false);
  assert.equal(app.$('#cardParams').hidden,false);
  assert.equal(app.$('#bar').style.width,'42%');
});

test('transient polling failure schedules retry and displays explanation',async()=>{
  const app=boot({saved:{kf_sid:'s1'},fetchImpl:async()=>{throw Error('offline');}});
  await settle();
  assert.ok(app.timers.size>0);
  assert.match(app.$('#stage').textContent,/重试|连接/);
});

test('a stale status response cannot replace the current video',async()=>{
  let resolve;
  const app=boot({fetchImpl:()=>new Promise(r=>{resolve=r;})});
  app.run("state.sid='old'; startPoll();");
  app.run("stopPoll(); state.sid='new';");
  resolve({ok:true,status:200,json:async()=>done});
  await settle();
  assert.equal(app.run('state.cuts.length'),0);
});

test('selecting no frames is preserved when rendering again',()=>{
  const app=boot();
  app.context.fixture=result;
  app.run("state.sid='s1'; render(fixture);");
  app.$('#btnNone').click();
  app.run('render(fixture)');
  assert.equal(app.run('state.sel.size'),0);
  assert.equal(app.$('#btnDownload').disabled,true);
});

test('more than 500 selections are rejected before download',()=>{
  const app=boot();
  app.run('state.sel=new Set(Array.from({length:501},(_,i)=>i)); updateToolbar();');
  assert.equal(app.$('#btnDownload').disabled,true);
});

test('precise frame stepping clamps at both video boundaries',async()=>{
  const requested=[];
  const app=boot({fetchImpl:async url=>{
    requested.push(url);
    const index=Number(new URL(url,'http://local').searchParams.get('frame_index'));
    return {ok:true,json:async()=>({frame_index:index,time:index/30,label:'frame',image_url:'/image/'+index,frames:60})};
  }});
  app.run("state.sid='s1'; state.resultRun='r1'; state.totalFrames=60;");
  await app.run('requestPreview({frame_index:0})');
  await app.run('stepPreview(-1)');
  assert.equal(app.run('state.previewFrame'),0);
  await app.run('requestPreview({frame_index:59})');
  await app.run('stepPreview(1)');
  assert.equal(app.run('state.previewFrame'),59);
  assert.ok(requested.every(url=>!url.includes('frame_index=-1')&&!url.includes('frame_index=60')));
});

test('preview requests are serialized and the newest frame wins',async()=>{
  const requests=[];
  const app=boot({fetchImpl:url=>new Promise(resolve=>requests.push({url,resolve}))});
  app.run("state.sid='s1'; state.totalFrames=60;");
  const first=app.run('requestPreview({frame_index:10})');
  await settle();
  const second=app.run('requestPreview({frame_index:11})');
  await settle();
  assert.equal(requests.length,1,'must not compete with an active decoder');
  requests[0].resolve({ok:true,json:async()=>({frame_index:10,time:10/30,label:'10',image_url:'/10',frames:60})});
  await first;
  await settle();
  requests[1].resolve({ok:true,json:async()=>({frame_index:11,time:11/30,label:'11',image_url:'/11',frames:60})});
  await second;
  assert.equal(app.run('state.previewFrame'),11);
});

test('manual move preserves excluded selection at the new frame',async()=>{
  let submitted;
  const moved={...result,run:'r2',cuts:[result.cuts[0],{...result.cuts[1],kind:'adjusted',frame_index:31,time:31/30}]};
  const app=boot({saved:{kf_skipped_s1:'[30]'},fetchImpl:async(url,options)=>{
    if (url.includes('/selection')) return {ok:true,json:async()=>({ok:true})};
    submitted=JSON.parse(options.body);
    return {ok:true,json:async()=>({result:moved,from_frame:30,to_frame:31})};
  }});
  app.context.fixture=result;
  app.run("state.sid='s1'; render(fixture); state.previewFrame=31; state.previewReady=true; state.editIndex=1; state.editorBase='r1';");
  await app.run("saveManual('move')");
  assert.equal(submitted.base_run,'r1');
  assert.equal(submitted.frame_index,31);
  assert.equal(app.run('state.sel.has(1)'),false);
  assert.deepEqual(JSON.parse(app.store.get('kf_skipped_s1')),[31]);
});

test('programmatic pause does not overwrite a requested exact frame',async()=>{
  const requested=[];
  const app=boot({fetchImpl:async url=>{
    requested.push(url);
    const q=new URL(url,'http://local').searchParams;
    const frame=q.has('frame_index')?Number(q.get('frame_index')):0;
    return {ok:true,json:async()=>({frame_index:frame,time:frame/30,label:'frame',image_url:'/image/'+frame,frames:60})};
  }});
  app.run("state.sid='s1';state.resultRun='r1';state.totalFrames=60;state.previewFrame=30;");
  app.$('#videoPlayer').paused=false;
  await app.run('stepPreview(1)');
  app.$('#videoPlayer').events.pause();
  await settle();
  assert.equal(app.run('state.previewFrame'),31);
  assert.equal(requested.length,1);
});

test('no automatic cuts still initializes a precise frame for manual addition',async()=>{
  const app=boot({fetchImpl:async()=>({ok:true,json:async()=>({frame_index:0,time:0,label:'00:00:00.000',image_url:'/image/0',frames:60})})});
  app.context.empty={...result,cuts:[],thumbs:[],frames:[]};
  app.run("state.sid='empty';render(empty);");
  await settle();
  assert.equal(app.run('state.previewFrame'),0);
  assert.equal(app.$('#btnAddFrame').disabled,false);
  assert.equal(app.$('#exactFrame').hidden,false);
});

test('workspace restore works without an old browser session',async()=>{
  const app=boot({response:{...done,excluded:[30]}});
  await app.run("restoreWorkspace('saved-session')");
  assert.equal(app.run('state.sid'),'saved-session');
  assert.equal(app.run('state.sel.size'),1);
  assert.equal(app.$('#mName').textContent,'example.avi');
  assert.equal(app.$('#cardResult').hidden,false);
});

test('archived screenshots can be individually selected without invented frame numbers',()=>{
  const app=boot();
  app.context.archived={...result,meta:{...result.meta,gallery_only:true},cuts:[
    {kind:'archived',frame_index:null,file_index:0,label:'时间未知'},
    {kind:'archived',frame_index:null,file_index:7,label:'时间未知'}]};
  app.run("state.sid='archive';render(archived);state.sel.delete(0);rememberSelection();render(archived);");
  assert.equal(app.run('state.sel.has(0)'),false);
  assert.equal(app.run('state.sel.has(1)'),true);
  assert.equal(app.$('#cardEditor').hidden,true);
});

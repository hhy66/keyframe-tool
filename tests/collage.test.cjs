const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function boot() {
  const elements=new Map(),draws=[];
  const node=()=>({value:'',hidden:false,disabled:false,checked:false,children:[],style:{},textContent:'',
    classList:{add(){},remove(){},toggle(){}},addEventListener(){},setAttribute(){},append(...x){this.children.push(...x);},appendChild(x){this.children.push(x);},
    pause(){},focus(){},click(){},remove(){},toBlob(callback,type){callback({type});},getContext(){return {fillRect(){},strokeRect(){},drawImage(...args){draws.push(args.slice(1));},fillText(){},measureText(){return {width:1};}};}});
  const $=key=>{if(!elements.has(key))elements.set(key,node());return elements.get(key);};
  const state={sid:'one',resultRun:'run',sel:new Set([2,0]),cuts:[0,1,2].map(i=>({label:`time${i}`,frame_index:i})),thumbs:['t0','t1','t2'],frames:['f0','f1','f2']};
  const context=vm.createContext({console,state,document:{querySelector:$,createElement:node,body:node()},toast(){},setTimeout,clearTimeout,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},
    Image:class{constructor(){this.naturalWidth=200;this.naturalHeight=100;}async decode(){}}});
  if(fs.existsSync('static/collage.js'))vm.runInContext(fs.readFileSync('static/collage.js','utf8'),context);
  return {state,$,context,draws,run:code=>vm.runInContext(code,context)};
}

test('collage starts with an independent ordered copy of download selections',()=>{
  const app=boot();app.run('CollageUI.open()');
  assert.equal(app.run('CollageUI.ids.join(",")'),'0,2');
  app.run('CollageUI.toggle(1);CollageUI.toggle(0)');
  assert.deepEqual([...app.state.sel],[2,0]);
  assert.equal(app.run('CollageUI.ids.join(",")'),'2,1');
});

test('overflow never silently drops selected images',()=>{
  const app=boot();
  assert.throws(()=>app.run('CollageUI.pages([0,1,2,3,4],2,false)'),/超出|超过/);
  assert.equal(app.run('JSON.stringify(CollageUI.pages([0,1,2,3,4],2,true))'),'[[0,1,2,3],[4]]');
});

test('incomplete layout keeps empty positions without duplicate photos',()=>{
  const app=boot();
  assert.equal(app.run('JSON.stringify(CollageUI.pages([0,2],3,false))'),'[[0,2]]');
});

test('fit geometry preserves whole image or deliberately crops it',()=>{
  const app=boot();
  const contain=app.run('CollageUI.geometry(200,100,100,100,"contain")');
  assert.equal(contain.dw,100);assert.equal(contain.dh,50);assert.equal(contain.dy,25);
  const cover=app.run('CollageUI.geometry(200,100,100,100,"cover")');
  assert.equal(cover.sw,100);assert.equal(cover.sx,50);assert.equal(cover.dh,100);
});

test('drag and keyboard ordering do not change source or download selection',()=>{
  const app=boot();app.run('CollageUI.open();CollageUI.toggle(1);CollageUI.move(1,0)');
  assert.equal(app.run('CollageUI.ids.join(",")'),'1,0,2');
  assert.equal(app.state.cuts[0].label,'time0');
  assert.deepEqual([...app.state.sel],[2,0]);
});

test('grid size and output dimensions are bounded',()=>{
  const app=boot();
  assert.throws(()=>app.run('CollageUI.layout({grid:100,width:3000,shape:"square",index:false,time:false})'));
  assert.throws(()=>app.run('CollageUI.layout({grid:3,width:90000,shape:"square",index:false,time:false})'));
  const layout=app.run('CollageUI.layout({grid:3,width:2400,shape:"square",index:false,time:false})');
  assert.equal(layout.width,2400);assert.ok(layout.cellWidth<800);assert.ok(layout.height<=2400);
});

test('missing source image prevents exporting a silently incomplete collage',async()=>{
  const app=boot();
  app.context.Image=class{async decode(){throw Error('missing');}};
  app.run('CollageUI.open()');
  await app.run('CollageUI.generate()');
  assert.equal(app.run('CollageUI.canvas'),null);
  assert.equal(app.$('#collageDownload').disabled,true);
  assert.match(app.$('#collageStatus').textContent,/未生成不完整拼图/);
});

test('generation uses full image sources instead of thumbnails',async()=>{
  const app=boot(),sources=[];
  app.context.Image=class{constructor(){this.naturalWidth=2000;this.naturalHeight=1000;}async decode(){sources.push(this.src);}};
  app.run('CollageUI.open()');
  await app.run('CollageUI.generate()');
  assert.deepEqual(sources,['f0','f2']);
  assert.equal(app.run('CollageUI.canvas.width'),2400);
  assert.equal(app.$('#collageDownload').disabled,false);
});

test('changing settings invalidates old output and closing preserves draft selection',async()=>{
  const app=boot();app.run('CollageUI.open();CollageUI.toggle(1)');
  await app.run('CollageUI.generate()');
  app.run('CollageUI.close();CollageUI.open()');
  assert.equal(app.run('CollageUI.ids.join(",")'),'0,2,1');
  assert.equal(app.run('CollageUI.canvas'),null);
  assert.equal(app.$('#collageDownload').disabled,true);
});

test('last page uses only its remaining photo and prepares the selected file format',async()=>{
  const app=boot(),sources=[];
  app.state.cuts=Array.from({length:9},(_,i)=>({label:`time${i}`,frame_index:i}));
  app.state.frames=Array.from({length:9},(_,i)=>`full${i}`);
  app.state.thumbs=Array.from({length:9},(_,i)=>`thumb${i}`);
  app.state.sel=new Set([0,1,2,3,4,5,6,7,8]);
  app.context.Image=class{constructor(){this.naturalWidth=2000;this.naturalHeight=1000;}async decode(){sources.push(this.src);}};
  app.run('CollageUI.open()');
  app.$('#collageSplit').checked=true;
  app.$('#collageFormat').value='jpeg';
  app.run('CollageUI.page=2');
  await app.run('CollageUI.generate()');
  assert.deepEqual(sources,['full8']);
  assert.match(app.$('#collageSummary').textContent,/3 个空格/);
  assert.match(app.$('#collageDownload').download,/_03\.jpg$/);
  assert.equal(app.state.sel.size,9);
});

test('same-video collage defaults to edge-to-edge original ratio without captions',async()=>{
  const app=boot();
  app.state.cuts=Array.from({length:9},(_,i)=>({label:`time${i}`,frame_index:i}));
  app.state.frames=Array.from({length:9},(_,i)=>`full${i}`);
  app.state.thumbs=Array.from({length:9},(_,i)=>`thumb${i}`);
  app.state.sel=new Set([0,1,2,3,4,5,6,7,8]);
  app.context.Image=class{constructor(){this.naturalWidth=1600;this.naturalHeight=1200;}async decode(){}};
  app.run('CollageUI.open()');
  assert.equal(app.$('#collageShape').value,'source');
  app.$('#collageGrid').value='3';app.$('#collageWidth').value='3000';
  app.$('#collageIndex').checked=true;app.$('#collageTime').checked=true;
  await app.run('CollageUI.generate()');
  assert.equal(app.run('CollageUI.canvas.width'),3000);
  assert.equal(app.run('CollageUI.canvas.height'),2250);
  assert.equal(app.draws.length,9);
  for(let i=0;i<9;i++) {
    assert.deepEqual(app.draws[i],[0,0,1600,1200,(i%3)*1000,Math.floor(i/3)*750,1000,750]);
  }
  assert.equal(app.$('#collageIndex').disabled,true);
});

test('seamless mode rejects mismatched source ratios instead of stretching or cropping',async()=>{
  const app=boot();let index=0;
  app.context.Image=class{constructor(){this.naturalWidth=1600;this.naturalHeight=index++===0?1200:900;}async decode(){}};
  app.run('CollageUI.open()');await app.run('CollageUI.generate()');
  assert.equal(app.run('CollageUI.canvas'),null);
  assert.match(app.$('#collageStatus').textContent,/比例不一致/);
});

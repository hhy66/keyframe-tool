const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');

function boot(responder=async()=>({})) {
  const nodes=new Map(),calls=[],messages=[];
  function element(){return {hidden:true,disabled:false,value:'',textContent:'',children:[],dataset:{},style:{},checked:false,
    classList:{add(){},remove(){},toggle(){}},addEventListener(){},setAttribute(){},
    append(...items){this.children.push(...items);},appendChild(item){this.children.push(item);},scrollIntoView(){}};}
  const $=key=>{if(!nodes.has(key))nodes.set(key,element());return nodes.get(key);};
  const context=vm.createContext({console,document:{querySelector:$,createElement:element,body:element()},
    jsonRequest:async(url,options)=>{calls.push({url,options});return responder(url,options);},
    toast:(...args)=>messages.push(args),state:{sid:null},loadWorkspace(){},startPoll(){},
    navigator:{clipboard:{writeText:async()=>{}}},setTimeout,clearTimeout});
  const path='static/storage.js';
  if(fs.existsSync(path))vm.runInContext(fs.readFileSync(path,'utf8'),context);
  return {$,calls,messages,context,run:code=>vm.runInContext(code,context)};
}

test('recommendations never select current, pinned, or manually edited versions',()=>{
  const app=boot();
  app.context.versions=[
    {run:'current',current:true},{run:'pin',pinned:true},{run:'manual',manual:true},{run:'ordinary'}];
  assert.equal(app.run("StorageUI.candidates(versions, 'current').join(',')"),'ordinary');
  assert.equal(app.run("StorageUI.candidates(versions, 'three').join(',')"),'ordinary');
});

test('whole record deletion cannot execute without the exact confirmation phrase',async()=>{
  const app=boot();
  app.run("StorageUI.plan={token:'t',kind:'session',require_phrase:'删除整条记录',bytes:1024};");
  app.$('#cleanupPhrase').value='删除';
  await app.run('StorageUI.execute()');
  assert.equal(app.calls.length,0);
});

test('trash confirmation explicitly says it does not release disk space',()=>{
  const app=boot();
  app.run("StorageUI.plan={token:'t',kind:'versions',require_phrase:'',bytes:1024};StorageUI.mode='trash';StorageUI.updateConfirm();");
  assert.match(app.$('#cleanupImpact').textContent,/不释放/);
  assert.match(app.$('#cleanupExecute').textContent,/回收区/);
});

test('permanent cleanup sends the reviewed token and mode',async()=>{
  const app=boot(async url=>url.endsWith('/execute')?{status:'done',message:'已完成',released_bytes:1024,processed_bytes:1024,failures:[]}:
    {sessions:[],trash:[],total_bytes:0,trash_bytes:0,free_bytes:100});
  app.run("StorageUI.plan={token:'reviewed',kind:'versions',require_phrase:'',bytes:1024};StorageUI.mode='permanent';");
  await app.run('StorageUI.execute()');
  const call=app.calls.find(c=>c.url.endsWith('/execute'));
  assert.deepEqual(JSON.parse(call.options.body),{token:'reviewed',mode:'permanent',confirmation:''});
});

test('partial failure remains visible and cannot be reported as full success',async()=>{
  const app=boot(async url=>url.endsWith('/execute')?{status:'partial',message:'部分完成',released_bytes:0,processed_bytes:100,failures:['文件被占用']}:
    {sessions:[],trash:[],total_bytes:0,trash_bytes:0,free_bytes:100});
  app.run("StorageUI.plan={token:'t',kind:'versions',require_phrase:'',bytes:100};");
  await app.run('StorageUI.execute()');
  assert.match(app.$('#storageOutcome').textContent,/部分完成/);
  assert.match(app.$('#storageOutcome').textContent,/文件被占用/);
});

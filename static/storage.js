/* Workspace cleanup always uses a server-reviewed plan; never send disk paths. */
const StorageUI = {
  data:null, busy:false, plan:null, mode:'trash', tab:'records', selections:new Map(),
  bytes(value) {
    let size=Math.max(0,Number(value)||0), unit=0;
    const units=['B','KB','MB','GB','TB'];
    while(size>=1024 && unit<units.length-1){size/=1024;unit++;}
    return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  },
  date(value) {
    if(!value)return '时间未知';
    const parsed=new Date(typeof value==='number'?value*1000:value);
    return Number.isNaN(parsed.getTime())?'时间未知':parsed.toLocaleString();
  },
  node(tag,text,className) {
    const node=document.createElement(tag);
    if(text!==undefined)node.textContent=text;
    if(className)node.className=className;
    return node;
  },
  button(text,action,danger=false) {
    const button=this.node('button',text,'btn ghost small'+(danger?' storageDanger':''));
    button.addEventListener('click',action);return button;
  },
  candidates(versions,rule) {
    const ordered=[...versions].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')));
    const retained=new Set(rule==='three'?ordered.slice(0,3).map(v=>v.run):[]);
    return ordered.filter(v=>!v.current&&!v.pinned&&!v.manual&&!retained.has(v.run)).map(v=>v.run);
  },
  async open() {
    document.querySelector('#storageManager').hidden=false;
    await this.load();
  },
  async load() {
    if(this.busy)return;
    this.busy=true;
    document.querySelector('#storageRefresh').disabled=true;
    document.querySelector('#storageStatus').textContent='正在统计文件占用，不会删除任何文件…';
    try {
      this.data=await jsonRequest('/api/storage',{},0);
      document.querySelector('#storagePath').value=this.data.path||'';
      const summary=document.querySelector('#storageSummary');summary.innerHTML='';
      const sessions=this.data.sessions||[];
      const history=sessions.reduce((total,s)=>total+(s.versions||[]).filter(v=>!v.current).reduce((n,v)=>n+(v.bytes||0),0),0);
      const metrics=[['工作区总占用',this.data.total_bytes],['历史结果占用',history],['回收区占用',this.data.trash_bytes],['磁盘剩余空间',this.data.free_bytes]];
      metrics.forEach(([label,value])=>{
        const card=this.node('div',undefined,'storageMetric');
        card.append(this.node('span',label),this.node('strong',this.bytes(value)));summary.appendChild(card);
      });
      document.querySelector('#storageStatus').textContent=`${sessions.length} 条工作记录。容量按文件大小统计；实际磁盘可用空间以系统为准。`;
      this.render();
    } catch(e) {
      document.querySelector('#storageStatus').textContent='读取占用失败：'+(e.message||'请确认工具已运行');
    } finally {this.busy=false;document.querySelector('#storageRefresh').disabled=false;}
  },
  render() {
    if(!this.data)return;
    const isTrash=this.tab==='trash';
    document.querySelector('#storageRecords').hidden=isTrash;
    document.querySelector('#storageTrash').hidden=!isTrash;
    document.querySelector('#storageSearch').hidden=isTrash;
    document.querySelector('#storageSort').hidden=isTrash;
    document.querySelector('#storageRecordsTab').className='btn small'+(isTrash?' ghost':'');
    document.querySelector('#storageTrashTab').className='btn small'+(isTrash?'':' ghost');
    this.renderRecords();this.renderTrash();
  },
  renderRecords() {
    const list=document.querySelector('#storageRecords');list.innerHTML='';
    const search=(document.querySelector('#storageSearch').value||'').toLowerCase();
    const sort=document.querySelector('#storageSort').value||'size';
    const sessions=(this.data.sessions||[]).filter(s=>(String(s.video_name||'')+' '+s.session_id).toLowerCase().includes(search));
    sessions.sort((a,b)=>sort==='name'?String(a.video_name).localeCompare(String(b.video_name)):sort==='recent'?String(b.updated_at||'').localeCompare(String(a.updated_at||'')):(b.bytes||0)-(a.bytes||0));
    if(!sessions.length){list.textContent='没有匹配的工作记录。';return;}
    sessions.forEach(session=>list.appendChild(this.record(session)));
  },
  record(session) {
    const sid=session.session_id, versions=session.versions||[];
    const selected=this.selections.get(sid)||new Set();this.selections.set(sid,selected);
    for(const run of selected){if(!versions.some(v=>v.run===run&&!v.current&&!v.pinned))selected.delete(run);}
    const card=this.node('details',undefined,'storageRecord');
    const summary=this.node('summary');
    if(session.thumbnail_url){const img=this.node('img',undefined,'storageThumb');img.src=session.thumbnail_url;img.loading='lazy';img.alt='记录缩略图';summary.appendChild(img);}
    const name=this.node('div',undefined,'storageName');
    name.append(this.node('b',session.video_name||sid),this.node('small',`${versions.length} 个版本 · ${this.date(session.updated_at)} · 记录 ${sid}`));
    summary.append(name,this.node('span',this.bytes(session.bytes),'storageBytes'));
    if(session.busy)summary.appendChild(this.node('span','正在使用，暂不能清理','storageLabel protected'));
    card.appendChild(summary);
    const detail=this.node('div',undefined,'storageDetail');
    const breakdown=this.node('div',undefined,'storageBreakdown');
    [['视频副本',session.video_bytes],['扫描缓存',session.cache_bytes],['临时压缩包',session.temp_bytes],['未知文件',session.unknown_bytes]].forEach(([key,value])=>breakdown.appendChild(this.node('span',`${key}：${this.bytes(value)}`)));
    detail.appendChild(breakdown);
    if(session.note)detail.appendChild(this.node('p',session.note,'storageNotice'));
    if(session.unknown_bytes)detail.appendChild(this.node('p','未知文件不纳入版本或临时文件清理；只有明确删除整条记录时才会一并列入确认清单。','hint'));
    const presets=this.node('div',undefined,'storageActions');
    const choices=[];
    const applyRule=rule=>{
      selected.clear();
      if(!session.busy)this.candidates(versions,rule).forEach(run=>selected.add(run));
      choices.forEach(({check,version})=>{check.checked=selected.has(version.run);});
      update();
    };
    const keep3=this.button('保留最近 3 个版本',()=>applyRule('three'));
    const keepCurrent=this.button('仅保留当前版本',()=>applyRule('current'));
    keep3.disabled=keepCurrent.disabled=!!session.busy;
    presets.append(keep3,keepCurrent,this.button('取消选择',()=>{selected.clear();choices.forEach(({check})=>check.checked=false);update();}));
    detail.append(presets,this.node('p','规则仅勾选候选，不直接删除。当前、标记保留、含手动修改的版本不会被规则自动勾选。','hint'));
    const selectedLabel=this.node('p','', 'hint');
    const previewButton=this.button('查看所选版本清理清单',()=>this.preview({session_id:sid,kind:'versions',runs:[...selected]}));
    const update=()=>{
      const bytes=versions.filter(v=>selected.has(v.run)).reduce((sum,v)=>sum+(v.bytes||0),0);
      selectedLabel.textContent=`已选 ${selected.size} 个历史版本，共 ${this.bytes(bytes)}；确认前不会删除。`;
      previewButton.disabled=!!session.busy||selected.size===0;
    };
    [...versions].sort((a,b)=>Number(b.current)-Number(a.current)||String(b.created_at||'').localeCompare(String(a.created_at||''))).forEach(version=>{
      const row=this.node('div',undefined,'storageVersion');
      const check=this.node('input');check.type='checkbox';check.checked=selected.has(version.run);
      check.disabled=!!session.busy||!!version.current||!!version.pinned;
      check.setAttribute('aria-label','清理版本 '+version.run);
      check.addEventListener('change',()=>{if(check.checked)selected.add(version.run);else selected.delete(version.run);update();});
      choices.push({check,version});row.appendChild(check);
      const image=this.node('img');image.alt='版本缩略图';image.loading='lazy';if(version.thumbnail_url)image.src=version.thumbnail_url;row.appendChild(image);
      const info=this.node('div');info.append(this.node('b',`${version.count||0} 张 · ${this.bytes(version.bytes)}`),this.node('small',`${this.date(version.created_at)} · 版本 ${version.run}`));
      if(version.current)info.appendChild(this.node('span','当前结果','storageLabel protected'));
      if(version.pinned)info.appendChild(this.node('span','已标记保留','storageLabel protected'));
      if(version.manual)info.appendChild(this.node('span','含手动修改','storageLabel manual'));
      row.appendChild(info);
      const actions=this.node('div',undefined,'storageVersionActions');
      const gallery=this.node('div',undefined,'storagePreview');gallery.hidden=true;
      actions.appendChild(this.button('查看截图',()=>this.showVersion(sid,version.run,gallery)));
      const pin=this.button(version.pinned?'取消保留标记':'标记保留',()=>this.pin(sid,version.run,!version.pinned));pin.disabled=!!session.busy;actions.appendChild(pin);
      row.appendChild(actions);detail.append(row,gallery);
    });
    detail.append(selectedLabel,previewButton);update();
    const advanced=this.node('details',undefined,'storageAdvanced');advanced.appendChild(this.node('summary','高级清理：缓存、视频副本及整条记录'));
    const options=[
      ['清理临时 ZIP','temporary','只清理不再使用的工作区压缩包；需要时可重新打包。',session.temp_bytes],
      ['清理扫描缓存','cache','保留截图；后续补帧或微调需要重新分析视频。',session.cache_bytes],
      ['删除工作区视频副本','video','保留截图；无法继续分析、补帧或微调。外部原始视频不受影响。',session.video_bytes],
      ['删除整条工作记录','session','包含视频副本、所有结果版本、缓存及未知文件。将失去这条记录的全部本地恢复内容。',session.bytes],
    ];
    options.forEach(([label,kind,warning,bytes])=>{
      const button=this.button(`${label} · ${this.bytes(bytes)}`,()=>this.preview({session_id:sid,kind}),kind==='session');
      button.disabled=!!session.busy||!bytes;advanced.append(button,this.node('p',warning));
    });
    detail.appendChild(advanced);card.appendChild(detail);return card;
  },
  async showVersion(sid,run,gallery) {
    if(!gallery.hidden){gallery.hidden=true;return;}
    gallery.hidden=false;gallery.textContent='正在读取版本截图…';
    try {
      const result=await jsonRequest(`/api/storage/version/${encodeURIComponent(sid)}/${encodeURIComponent(run)}`);
      gallery.innerHTML='';
      (result.cuts||[]).forEach((cut,index)=>{
        const link=this.node('a');link.href=result.frames[index];link.target='_blank';link.rel='noopener';
        const image=this.node('img');image.src=result.thumbs[index];image.loading='lazy';image.alt=cut.label||'时间未知';
        link.append(image,this.node('span',`#${index+1} ${cut.label||'时间未知'}`));gallery.appendChild(link);
      });
      if(!result.cuts?.length)gallery.textContent='此版本没有截图。';
    } catch(e){gallery.textContent=e.message||'此版本已被清理或暂时不可用，请刷新列表。';}
  },
  async pin(sid,run,pinned) {
    if(this.busy)return;
    try {
      await jsonRequest('/api/storage/pin',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,run,pinned})});
      await this.load();
    }catch(e){toast(e.message||'修改保留标记失败',true);}
  },
  renderTrash() {
    const list=document.querySelector('#storageTrash');list.innerHTML='';
    list.appendChild(this.node('p','回收区中的文件仍占用磁盘空间。恢复不会覆盖现有记录；永久删除后不能撤销。','storageNotice'));
    const items=this.data.trash||[];
    if(!items.length){list.appendChild(this.node('p','回收区为空。'));return;}
    items.forEach(item=>{
      const card=this.node('div',undefined,'storageRecord');const content=this.node('div',undefined,'storageDetail');
      content.append(this.node('b',`${item.name||item.session_id||item.id} · ${this.bytes(item.bytes)}`),this.node('p',`${this.date(item.created_at)} · ${item.note||item.status||'已移入回收区'}`));
      const actions=this.node('div',undefined,'storageActions');
      const restore=this.button('恢复这批文件',()=>this.restoreTrash(item.id));restore.disabled=item.restorable===false;
      actions.append(restore,this.button('永久删除这批文件',()=>this.preview({trash_id:item.id,kind:'purge'}),true));
      content.appendChild(actions);card.appendChild(content);list.appendChild(card);
    });
  },
  async preview(payload) {
    if(this.busy)return;
    this.busy=true;this.plan=null;
    document.querySelector('#storageStatus').textContent='正在核对文件、保护状态和清理范围…';
    try {
      this.plan=await jsonRequest('/api/storage/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)},0);
      this.mode=payload.kind==='purge'?'permanent':'trash';
      document.querySelector('#cleanupName').textContent=this.plan.name||'';
      document.querySelector('#cleanupTitle').textContent=payload.kind==='purge'?'永久删除回收区文件':'确认清理内容';
      const items=document.querySelector('#cleanupItems');items.innerHTML='';
      const table=this.node('table');
      const heading=this.node('tr');['将处理的内容','数量','占用'].forEach(s=>heading.appendChild(this.node('th',s)));table.appendChild(heading);
      (this.plan.items||[]).forEach(item=>{const row=this.node('tr');row.append(this.node('td',item.label),this.node('td',String(item.count??'—')),this.node('td',this.bytes(item.bytes)));table.appendChild(row);});
      items.append(table,this.node('p',`共 ${this.plan.file_count||0} 个文件，预计 ${this.bytes(this.plan.bytes)}。仅操作工作目录内列出的内容。`));
      [['#cleanupRetained',this.plan.retained],['#cleanupConsequences',this.plan.consequences]].forEach(([selector,lines])=>{
        const list=document.querySelector(selector);list.innerHTML='';(lines||[]).forEach(line=>list.appendChild(this.node('li',line)));
      });
      document.querySelector('#cleanupTrashMode').checked=this.mode==='trash';
      document.querySelector('#cleanupPermanentMode').checked=this.mode==='permanent';
      document.querySelector('#cleanupMode').hidden=payload.kind==='purge';
      document.querySelector('#cleanupPhrase').value='';
      document.querySelector('#cleanupPhraseLabel').textContent=`请输入“${this.plan.require_phrase||''}”以确认：`;
      document.querySelector('#cleanupPhraseWrap').hidden=!this.plan.require_phrase;
      document.querySelector('#cleanupError').textContent='';
      document.querySelector('#cleanupCancel').disabled=false;
      document.querySelector('#cleanupConfirm').hidden=false;
    } catch(e){document.querySelector('#storageStatus').textContent=e.message||'无法生成清理清单，请刷新后重试。';}
    finally{this.busy=false;this.updateConfirm();}
  },
  updateConfirm() {
    const button=document.querySelector('#cleanupExecute');
    const valid=this.plan&&(!this.plan.require_phrase||document.querySelector('#cleanupPhrase').value===this.plan.require_phrase);
    button.disabled=this.busy||!valid;
    button.textContent=this.mode==='permanent'?'永久删除，无法撤销':'移入回收区';
    button.className='btn'+(this.mode==='permanent'?' storageDanger':'');
    document.querySelector('#cleanupImpact').textContent=this.mode==='permanent'?
      `预计释放 ${this.bytes(this.plan?.bytes)}。文件将永久删除，工具无法撤销此操作。`:
      `将 ${this.bytes(this.plan?.bytes)} 移入回收区，可在无冲突时恢复。此步骤不释放磁盘空间。`;
  },
  outcome(result) {
    const output=document.querySelector('#storageOutcome');output.hidden=false;
    output.className='storageOutcome'+(result.status==='done'?'':' warning');
    const status=result.status==='done'?'处理完成':result.status==='partial'?'部分完成':'处理失败';
    output.textContent=`${status}：${result.message||''}\n已处理 ${this.bytes(result.processed_bytes)}；已永久删除文件容量 ${this.bytes(result.released_bytes)}。`+
      (result.failures?.length?'\n未完成项目：\n'+result.failures.join('\n'):'');
  },
  async execute() {
    if(this.busy||!this.plan)return;
    const confirmation=document.querySelector('#cleanupPhrase').value||'';
    if(this.plan.require_phrase&&confirmation!==this.plan.require_phrase){document.querySelector('#cleanupError').textContent='确认文字不匹配，尚未删除任何文件。';return;}
    this.busy=true;this.updateConfirm();document.querySelector('#cleanupCancel').disabled=true;
    const plan=this.plan;
    try {
      const result=await jsonRequest('/api/storage/execute',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({token:plan.token,mode:this.mode,confirmation})},0);
      this.outcome(result);this.plan=null;document.querySelector('#cleanupConfirm').hidden=true;
      this.selections.clear();
      if(state.sid===plan.session_id)startPoll(true);
    } catch(e) {
      this.plan=null;
      document.querySelector('#cleanupError').textContent=e.status?`${e.message}。请取消此窗口、刷新列表后重新预览清单。`:
        '连接中断，执行结果暂未确认。请取消此窗口并刷新空间信息核对，不要重复执行旧清单。';
    } finally {
      this.busy=false;document.querySelector('#cleanupCancel').disabled=false;this.updateConfirm();
      await this.load();
    }
  },
  async restoreTrash(id) {
    if(this.busy)return;this.busy=true;
    try {
      const result=await jsonRequest(`/api/storage/trash/${encodeURIComponent(id)}/restore`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'},0);
      this.outcome({...result,processed_bytes:0,released_bytes:0});
      if(state.sid)startPoll(true);
    }catch(e){this.outcome({status:'failed',message:e.message||'恢复失败，回收区内容已保留',failures:[]});}
    finally{this.busy=false;await this.load();}
  },
};

document.querySelector('#btnStorage').addEventListener('click',()=>StorageUI.open());
document.querySelector('#storageClose').addEventListener('click',()=>{if(!StorageUI.busy)document.querySelector('#storageManager').hidden=true;});
document.querySelector('#storageRefresh').addEventListener('click',()=>StorageUI.load());
document.querySelector('#storageRecordsTab').addEventListener('click',()=>{StorageUI.tab='records';StorageUI.render();});
document.querySelector('#storageTrashTab').addEventListener('click',()=>{StorageUI.tab='trash';StorageUI.render();});
document.querySelector('#storageSearch').addEventListener('input',()=>{if(StorageUI.data)StorageUI.renderRecords();});
document.querySelector('#storageSort').addEventListener('change',()=>{if(StorageUI.data)StorageUI.renderRecords();});
document.querySelector('#storageCopyPath').addEventListener('click',async()=>{
  try{await navigator.clipboard.writeText(document.querySelector('#storagePath').value);toast('已复制工作目录路径');}
  catch{toast('无法自动复制，请选中路径文字后手动复制');}
});
document.querySelector('#cleanupTrashMode').addEventListener('change',()=>{StorageUI.mode='trash';StorageUI.updateConfirm();});
document.querySelector('#cleanupPermanentMode').addEventListener('change',()=>{StorageUI.mode='permanent';StorageUI.updateConfirm();});
document.querySelector('#cleanupPhrase').addEventListener('input',()=>StorageUI.updateConfirm());
document.querySelector('#cleanupCancel').addEventListener('click',()=>{if(!StorageUI.busy){StorageUI.plan=null;document.querySelector('#cleanupConfirm').hidden=true;}});
document.querySelector('#cleanupExecute').addEventListener('click',()=>StorageUI.execute());

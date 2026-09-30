/* A collage is a local, independent draft. Source screenshot files are never edited. */
const CollageUI = {
  snapshot:null, ids:[], page:0, token:0, busy:false, abort:null, dragId:null, plan:null, output:null, crops:{}, stageView:null, cropMode:false, focus:null,
  el(selector){return document.querySelector(selector);},
  node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;},
  open() {
    if(!state.sid||!state.resultRun||!state.cuts.length){toast('请先生成或恢复关键帧结果');return;}
    this.invalidate();
    if(this.snapshot?.sid===state.sid&&this.snapshot?.run===state.resultRun){
      this.el('#collagePanel').hidden=false;this.renderLists();this.summary();this.queuePlan();return;
    }
    this.snapshot={sid:state.sid,run:state.resultRun,name:this.el('#mName').textContent||'video',
      cuts:state.cuts.map(c=>({...c})),thumbs:[...state.thumbs],frames:[...state.frames]};
    this.ids=[...state.sel].filter(i=>this.available(i)).sort((a,b)=>a-b);
    this.page=0;this.crops={};this.focus=null;this.cropMode=false;
    this.el('#collageMode').value='original';
    this.el('#collageGrid').value='2';this.el('#collageShape').value='source';
    this.el('#collageFit').value='contain';this.el('#collageWidth').value='3000';
    this.el('#collageBackground').value='white';this.el('#collageFormat').value='png';
    for(const selector of ['#collageIndex','#collageTime','#collageSplit'])this.el(selector).checked=false;
    this.el('#collageSource').textContent=`${this.snapshot.name} · 本次基于打开面板时的结果制作`;
    this.el('#collagePanel').hidden=false;
    this.renderLists();this.summary();this.queuePlan();
  },
  close(){this.invalidate();this.el('#collagePanel').hidden=true;},
  available(i){return Number.isInteger(i)&&i>=0&&i<(this.snapshot?.cuts.length||0)&&this.snapshot.cuts[i].available!==false&&!!this.snapshot.frames[i];},
  toggle(i){if(!this.available(i))return;const pos=this.ids.indexOf(i);if(pos<0)this.ids.push(i);else this.ids.splice(pos,1);this.changed();},
  move(id,position){
    const from=this.ids.indexOf(id);if(from<0||!Number.isInteger(position))return;
    this.ids.splice(from,1);this.ids.splice(Math.max(0,Math.min(this.ids.length,position)),0,id);this.changed();
  },
  changed(){this.page=0;this.invalidate();this.renderLists();this.summary();this.queuePlan();},
  discardOutput(output=this.output){
    if(output?.token&&this.snapshot){
      jsonRequest(`/api/collage/${encodeURIComponent(output.sid||this.snapshot.sid)}/${encodeURIComponent(output.token)}`,{method:'DELETE',keepalive:true}).catch(()=>{});
    }
    if(output===this.output)this.output=null;
  },
  invalidate(){
    this.token++;clearTimeout(this.planTimer);
    if(this.abort)this.abort();
    this.stageView?.destroy();this.plan=null;this.discardOutput();
    this.el('#collagePreview').textContent='设置已更新。点击“生成成品”查看实际输出。';
    this.el('#collageDownload').disabled=true;this.el('#collageDownload').setAttribute('aria-disabled','true');
    this.el('#collageActual').disabled=true;this.el('#collageFitView').disabled=true;
  },
  pages(ids,grid,split){
    if(![2,3].includes(grid))throw new Error('请选择 2×2 或 3×3 布局');
    if(!ids.length)throw new Error('请至少选择一张参考图');
    const capacity=grid*grid;
    if(ids.length>capacity&&!split)throw new Error(`已选 ${ids.length} 张，超过 ${capacity} 格；请减少选图或开启“分成多张拼图”。`);
    const pages=[];for(let i=0;i<ids.length;i+=capacity)pages.push(ids.slice(i,i+capacity));return pages;
  },
  options(){
    const original=this.el('#collageMode').value==='original';
    return {grid:+this.el('#collageGrid').value,mode:this.el('#collageMode').value,
      shape:original?'source':this.el('#collageShape').value,fit:original?'contain':this.el('#collageFit').value,
      width:+this.el('#collageWidth').value,background:this.el('#collageBackground').value,
      format:this.el('#collageFormat').value,index:!original&&this.el('#collageIndex').checked,
      time:!original&&this.el('#collageTime').checked,split:this.el('#collageSplit').checked,crops:original?{}:this.crops};
  },
  payload(){
    const options=this.options(),pages=this.pages(this.ids,options.grid,options.split);
    this.page=Math.max(0,Math.min(this.page,pages.length-1));
    const ids=pages[this.page];
    return {...options,ids,run:this.snapshot.run,page:this.page+1,
      crops:Object.fromEntries(Object.entries(options.crops).filter(([id])=>ids.includes(Number(id))))};
  },
  summary(){
    const original=this.el('#collageMode').value==='original';
    for(const selector of ['#collageShape','#collageFit','#collageIndex','#collageTime'])this.el(selector).disabled=original;
    this.el('#collageWidth').disabled=this.el('#collageMode').value!=='custom';
    const cropAllowed=!original&&this.el('#collageShape').value!=='source'&&this.el('#collageFit').value==='cover';
    if(!cropAllowed)this.cropMode=false;
    this.el('#collageCropMode').disabled=!cropAllowed||this.busy;
    this.el('#collageCropMode').textContent=this.cropMode?'结束裁剪编辑':'编辑单图裁剪';
    for(const selector of ['#collageZoomIn','#collageZoomOut','#collageResetCrop'])this.el(selector).disabled=!this.cropMode||this.focus===null||this.busy;
    this.el('#collageModeNote').textContent=original?'原尺寸无缝：不缩放、不裁剪、没有边距或标注栏。每格保留原图像素。':'分享或自定义：原图经高质量缩放，可选择留白或主动裁剪；最终以实际成品为准。';
    try{
      const options=this.options(),pages=this.pages(this.ids,options.grid,options.split);
      this.page=Math.max(0,Math.min(this.page,pages.length-1));
      const empty=options.grid*options.grid-pages[this.page].length;
      this.el('#collageSummary').className='collageNotice';
      this.el('#collageSummary').textContent=`共选 ${this.ids.length} 张，将生成 ${pages.length} 张拼图；当前使用 ${pages[this.page].length} 张。`+(empty?`剩余 ${empty} 个空格，不重复填图。`:'没有空格。');
      this.el('#collagePage').textContent=`第 ${this.page+1} / ${pages.length} 张拼图`;
      this.el('#collagePrev').disabled=this.page===0||this.busy;
      this.el('#collageNext').disabled=this.page===pages.length-1||this.busy;
      this.el('#collageRender').disabled=this.busy||!this.plan||!this.plan.can_render;
    }catch(e){
      this.el('#collageSummary').className='collageNotice error';this.el('#collageSummary').textContent=e.message;
      this.el('#collagePage').textContent='';this.el('#collageDimensions').textContent='';
      this.el('#collagePrev').disabled=this.el('#collageNext').disabled=this.el('#collageRender').disabled=true;
    }
  },
  queuePlan(){
    clearTimeout(this.planTimer);
    this.planTimer=setTimeout(()=>this.refreshPlan(),120);
  },
  async refreshPlan(){
    if(!this.snapshot)return;
    let payload;try{payload=this.payload();}catch{this.summary();return;}
    const token=this.token,snapshot=this.snapshot;
    this.el('#collageDimensions').textContent='正在读取原图尺寸…';
    try{
      const plan=await jsonRequest(`/api/collage/${encodeURIComponent(snapshot.sid)}/plan`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if(token!==this.token)return;
      this.plan=plan;
      this.el('#collageDimensions').textContent=`整图 ${plan.width} × ${plan.height} 像素；每格 ${plan.cell_width} × ${plan.image_height} 像素；预计处理内存约 ${Math.ceil(plan.estimated_memory_bytes/1048576)} MB。`+(plan.warning||'');
      this.el('#collageStatus').textContent=plan.can_render?'可直接拖动画面交换位置，或生成实际成品。':(plan.warning||'尺寸超过处理限制，请改用分享或自定义尺寸');
      this.summary();
      if(typeof CollageStage!=='undefined'){
        if(!this.stageView)this.stageView=new CollageStage(this.el('#collageStage'),(a,b)=>this.swap(a,b),(id,crop)=>this.setCrop(id,crop),id=>{this.focus=id;this.summary();});
        await this.stageView.show({...plan,background:payload.background},this.cropMode);
      }
    }catch(e){if(token===this.token){this.plan=null;this.el('#collageStatus').textContent=e.message||'预览失败';this.summary();}}
  },
  swap(a,b){
    const first=this.ids.indexOf(a),second=this.ids.indexOf(b);if(first<0||second<0||first===second)return;
    [this.ids[first],this.ids[second]]=[this.ids[second],this.ids[first]];
    this.invalidate();this.renderLists();this.summary();this.queuePlan();
  },
  setCrop(id,crop){
    this.crops[id]=crop;this.focus=id;this.invalidate();this.summary();this.queuePlan();
  },
  renderLists(){
    const picker=this.el('#collagePicker'),order=this.el('#collageOrder');picker.innerHTML='';order.innerHTML='';
    if(!this.snapshot)return;
    this.snapshot.cuts.forEach((cut,id)=>{
      const label=this.node('label',undefined,'collageChoice'+(this.ids.includes(id)?' selected':''));
      const check=this.node('input');check.type='checkbox';check.checked=this.ids.includes(id);check.disabled=!this.available(id);
      check.setAttribute('aria-label',`拼图选用第 ${id+1} 张`);check.addEventListener('change',()=>this.toggle(id));
      const img=this.node('img');img.src=this.snapshot.thumbs[id];img.alt=cut.label||'时间未知';img.loading='lazy';
      label.append(check,img,this.node('span',`#${String(id+1).padStart(3,'0')} ${cut.label||'时间未知'}`));picker.appendChild(label);
    });
    if(!this.ids.length)order.textContent='尚未选择图片。';
    this.ids.forEach((id,index)=>{
      const row=this.node('div',undefined,'collageOrderRow');row.draggable=true;
      row.addEventListener('dragstart',e=>{this.dragId=id;e.dataTransfer.setData('text/plain',String(id));e.dataTransfer.effectAllowed='move';row.classList.add('dragging');});
      row.addEventListener('dragend',()=>{this.dragId=null;row.classList.remove('dragging');});
      row.addEventListener('dragover',e=>{if(this.dragId!==null){e.preventDefault();e.dataTransfer.dropEffect='move';}});
      row.addEventListener('drop',e=>{e.preventDefault();if(this.dragId!==null){const dragged=this.dragId;this.dragId=null;this.move(dragged,index);}});
      const image=this.node('img');image.src=this.snapshot.thumbs[id];image.alt='';image.draggable=false;
      row.append(image,this.node('span',`${index+1}. 原图 #${String(id+1).padStart(3,'0')}`,'collageOrderName'));
      [['上移',index-1],['下移',index+1]].forEach(([label,position])=>{
        const button=this.node('button',label,'btn ghost small');button.disabled=position<0||position>=this.ids.length;
        button.setAttribute('aria-label',`${label}拼图中的原图 ${id+1}`);button.addEventListener('click',()=>this.move(id,position));row.appendChild(button);
      });
      const remove=this.node('button','移除','btn ghost small');remove.setAttribute('aria-label',`从拼图移除原图 ${id+1}`);remove.addEventListener('click',()=>this.toggle(id));row.appendChild(remove);
      order.appendChild(row);
    });
  },
  async loadImage(url){
    const image=new Image();image.src=url;
    let timer,cancel;
    const interrupted=new Promise((resolve,reject)=>{
      cancel=()=>{image.src='';reject(new Error('预览已取消'));};
      timer=setTimeout(()=>{image.src='';reject(new Error('读取原图超时，请确认工具仍在运行'));},20000);
    });
    this.abort=cancel;
    try{await Promise.race([image.decode(),interrupted]);return image;}
    finally{clearTimeout(timer);if(this.abort===cancel)this.abort=null;}
  },
  async assertCurrent(snapshot){
    const current=await jsonRequest('/api/status/'+encodeURIComponent(snapshot.sid));
    if(current.result?.run!==snapshot.run)throw new Error('关键帧结果已更新。请关闭拼图面板、刷新结果后重新选择，避免混用旧图。');
  },
  async generate(){
    if(this.busy||!this.snapshot)return;
    let payload;try{payload=this.payload();}catch(e){this.el('#collageStatus').textContent=e.message;return;}
    const token=this.token,snapshot=this.snapshot;
    this.discardOutput();this.busy=true;this.summary();
    this.el('#collageDownload').disabled=true;this.el('#collageDownload').setAttribute('aria-disabled','true');
    this.el('#collageStatus').textContent='正在用原图生成实际成品…';
    let result,published=false;
    try{
      result=await jsonRequest(`/api/collage/${encodeURIComponent(snapshot.sid)}/render`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)},0);
      result.sid=snapshot.sid;
      if(token!==this.token)return;
      const image=await this.loadImage(result.preview_url);
      if(token!==this.token)return;
      image.alt='实际导出文件的预览';image.className='collageFinalImage';
      const preview=this.el('#collagePreview');preview.innerHTML='';preview.appendChild(image);
      this.output={...result,revision:token,page:this.page,pages:this.pages(this.ids,payload.grid,payload.split).length};
      const link=this.el('#collageDownload');link.href=result.download_url;link.download='';link.disabled=false;link.setAttribute('aria-disabled','false');
      this.el('#collageActual').disabled=false;this.el('#collageFitView').disabled=false;
      this.el('#collageStatus').textContent=`成品已生成：${result.width} × ${result.height}，${result.format.toUpperCase()}。预览与下载对应同一个文件。`;
      published=true;
    }catch(e){if(token===this.token)this.el('#collageStatus').textContent=e.message||'生成失败，未改变原有截图';}
    finally{if(result&&!published)this.discardOutput(result);this.busy=false;this.summary();}
  },
  async finalView(actual){
    if(!this.output)return;
    const output=this.output,token=this.token;
    try{
      const image=await this.loadImage(actual?output.image_url:output.preview_url);
      if(token!==this.token)return;
      image.alt=actual?'成品原尺寸，滚动查看细节':'实际导出文件的预览';image.className=actual?'collageFinalImage actual':'collageFinalImage';
      const preview=this.el('#collagePreview');preview.innerHTML='';preview.appendChild(image);
    }catch(e){if(token===this.token)this.el('#collageStatus').textContent=e.message||'读取成品失败';}
  },
  changePage(delta){if(this.busy)return;this.page+=delta;this.invalidate();this.summary();this.queuePlan();},
  download(event){
    if(!this.output||this.output.revision!==this.token){event.preventDefault();return;}
    this.el('#collageStatus').textContent=`已交给浏览器下载第 ${this.output.page+1}/${this.output.pages} 张成品，请查看下载列表。`;
  },

};
document.querySelector('#btnCollage').addEventListener('click',()=>CollageUI.open());
document.querySelector('#collageClose').addEventListener('click',()=>CollageUI.close());
document.querySelector('#collageSelectAll').addEventListener('click',()=>{
  if(CollageUI.snapshot){CollageUI.ids=CollageUI.snapshot.cuts.flatMap((_,i)=>CollageUI.available(i)?[i]:[]);CollageUI.changed();}
});
document.querySelector('#collageClear').addEventListener('click',()=>{CollageUI.ids=[];CollageUI.changed();});
document.querySelector('#collageImportSelection').addEventListener('click',()=>{
  if(CollageUI.snapshot?.sid!==state.sid||CollageUI.snapshot?.run!==state.resultRun){toast('结果版本已变化，请关闭后重新打开拼图面板');return;}
  CollageUI.ids=[...state.sel].filter(i=>CollageUI.available(i)).sort((a,b)=>a-b);CollageUI.changed();
});
for(const selector of ['#collageMode','#collageGrid','#collageShape','#collageFit','#collageWidth','#collageBackground','#collageFormat','#collageIndex','#collageTime','#collageSplit']){
  document.querySelector(selector).addEventListener('change',()=>{
    if(['#collageMode','#collageShape','#collageFit'].includes(selector)){CollageUI.crops={};CollageUI.focus=null;}
    if(CollageUI.el('#collageMode').value==='share')CollageUI.el('#collageWidth').value='3000';
    CollageUI.page=0;CollageUI.invalidate();CollageUI.summary();CollageUI.queuePlan();
  });
}
document.querySelector('#collagePrev').addEventListener('click',()=>CollageUI.changePage(-1));
document.querySelector('#collageNext').addEventListener('click',()=>CollageUI.changePage(1));
document.querySelector('#collageRender').addEventListener('click',()=>CollageUI.generate());
document.querySelector('#collageDownload').addEventListener('click',event=>CollageUI.download(event));

document.querySelector('#collageCropMode').addEventListener('click',()=>{CollageUI.cropMode=!CollageUI.cropMode;CollageUI.summary();CollageUI.refreshPlan();});
document.querySelector('#collageZoomIn').addEventListener('click',()=>CollageUI.stageView?.zoom(1.15));
document.querySelector('#collageZoomOut').addEventListener('click',()=>CollageUI.stageView?.zoom(1/1.15));
document.querySelector('#collageResetCrop').addEventListener('click',()=>{if(CollageUI.focus!==null){delete CollageUI.crops[CollageUI.focus];CollageUI.invalidate();CollageUI.summary();CollageUI.queuePlan();}});
document.querySelector('#collageActual').addEventListener('click',()=>CollageUI.finalView(true));
document.querySelector('#collageFitView').addEventListener('click',()=>CollageUI.finalView(false));

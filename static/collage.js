/* A collage is a local, independent draft. Source screenshot files are never edited. */
const CollageUI = {
  snapshot:null, ids:[], page:0, token:0, busy:false, canvas:null, abort:null, dragId:null,
  el(selector){return document.querySelector(selector);},
  node(tag,text,cls){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;},
  open() {
    if(!state.sid||!state.resultRun||!state.cuts.length){toast('请先生成或恢复关键帧结果');return;}
    this.invalidate();
    if(this.snapshot?.sid===state.sid&&this.snapshot?.run===state.resultRun){
      this.el('#collagePanel').hidden=false;this.renderLists();this.summary();return;
    }
    this.snapshot={sid:state.sid,run:state.resultRun,name:this.el('#mName').textContent||'video',
      cuts:state.cuts.map(c=>({...c})),thumbs:[...state.thumbs],frames:[...state.frames]};
    this.ids=[...state.sel].filter(i=>this.available(i)).sort((a,b)=>a-b);
    this.page=0;
    this.el('#collageGrid').value='2';this.el('#collageShape').value='source';
    this.el('#collageFit').value='contain';this.el('#collageWidth').value='2400';
    this.el('#collageBackground').value='white';this.el('#collageFormat').value='png';
    for(const selector of ['#collageIndex','#collageTime','#collageSplit'])this.el(selector).checked=false;
    this.el('#collageSource').textContent=`${this.snapshot.name} · 本次基于打开面板时的结果制作`;
    this.el('#collagePanel').hidden=false;
    this.renderLists();this.summary();
  },
  close(){this.invalidate();this.el('#collagePanel').hidden=true;},
  available(i){return Number.isInteger(i)&&i>=0&&i<(this.snapshot?.cuts.length||0)&&this.snapshot.cuts[i].available!==false&&!!this.snapshot.frames[i];},
  toggle(i){if(!this.available(i))return;const pos=this.ids.indexOf(i);if(pos<0)this.ids.push(i);else this.ids.splice(pos,1);this.changed();},
  move(id,position){
    const from=this.ids.indexOf(id);if(from<0||!Number.isInteger(position))return;
    this.ids.splice(from,1);this.ids.splice(Math.max(0,Math.min(this.ids.length,position)),0,id);this.changed();
  },
  changed(){this.page=0;this.invalidate();this.renderLists();this.summary();},
  invalidate(){
    this.token++;
    if(this.abort)this.abort();
    if(this.canvas){this.canvas.width=1;this.canvas.height=1;this.canvas=null;}
    if(this.downloadUrl){URL.revokeObjectURL(this.downloadUrl);this.downloadUrl=null;}
    this.el('#collagePreview').textContent='选图或设置已更新，请生成预览。';
    this.el('#collageDownload').disabled=true;
    this.el('#collageDownload').setAttribute('aria-disabled','true');
  },
  pages(ids,grid,split){
    if(![2,3].includes(grid))throw new Error('请选择 2×2 或 3×3 布局');
    if(!ids.length)throw new Error('请至少选择一张参考图');
    const capacity=grid*grid;
    if(ids.length>capacity&&!split)throw new Error(`已选 ${ids.length} 张，超过 ${capacity} 格；请减少选图或开启“分成多张拼图”。`);
    const pages=[];for(let i=0;i<ids.length;i+=capacity)pages.push(ids.slice(i,i+capacity));return pages;
  },
  options(){return {grid:+this.el('#collageGrid').value,shape:this.el('#collageShape').value,sourceRatio:this.snapshot?.sourceRatio,
    fit:this.el('#collageFit').value,width:+this.el('#collageWidth').value,
    background:this.el('#collageBackground').value,format:this.el('#collageFormat').value,
    index:this.el('#collageIndex').checked,time:this.el('#collageTime').checked,split:this.el('#collageSplit').checked};},
  layout(options){
    const {grid,width,shape}=options,ratios={source:options.sourceRatio??16/9,landscape:16/9,square:1,portrait:9/16};
    if(![2,3].includes(grid)||![1500,2400,3000].includes(width)||!Number.isFinite(ratios[shape])||ratios[shape]<=0)throw new Error('拼图尺寸设置无效');
    const gap=shape==='source'?0:Math.max(8,Math.round(width/150));
    const cellWidth=Math.floor((width-(grid+1)*gap)/grid);
    const imageHeight=Math.round(cellWidth/ratios[shape]);
    const captionHeight=shape!=='source'&&(options.index||options.time)?Math.max(24,Math.round(width/75)):0;
    const height=grid*(imageHeight+captionHeight)+(grid+1)*gap;
    if(width*height>20000000)throw new Error('拼图尺寸过大，请降低输出宽度');
    return {width,height,gap,cellWidth,imageHeight,captionHeight};
  },
  geometry(sw,sh,w,h,fit){
    if(![sw,sh,w,h].every(n=>Number.isFinite(n)&&n>0))throw new Error('图片尺寸无效');
    if(fit==='contain'){
      const scale=Math.min(w/sw,h/sh),dw=sw*scale,dh=sh*scale;
      return {sx:0,sy:0,sw,sh,dx:(w-dw)/2,dy:(h-dh)/2,dw,dh};
    }
    if(fit!=='cover')throw new Error('画面填充设置无效');
    const scale=Math.max(w/sw,h/sh),cropW=w/scale,cropH=h/scale;
    return {sx:(sw-cropW)/2,sy:(sh-cropH)/2,sw:cropW,sh:cropH,dx:0,dy:0,dw:w,dh:h};
  },
  summary(){
    const notice=this.el('#collageSummary');
    const seamless=this.el('#collageShape').value==='source';
    for(const selector of ['#collageFit','#collageIndex','#collageTime'])this.el(selector).disabled=seamless;
    this.el('#collageModeNote').textContent=seamless?'无缝拼接：自动跟随原图比例，零外边距、零间距，不裁剪、不添加标注栏。图片不会互相覆盖，原视频自带字幕保留。':'留白排版：可自定义格子比例、裁剪方式及标注栏。';
    try {
      const options=this.options(),layout=this.layout(options),pages=this.pages(this.ids,options.grid,options.split);
      this.page=Math.max(0,Math.min(this.page,pages.length-1));
      const empty=options.grid*options.grid-pages[this.page].length;
      notice.className='collageNotice';
      notice.textContent=`共选 ${this.ids.length} 张，将生成 ${pages.length} 张拼图。当前第 ${this.page+1} 张使用 ${pages[this.page].length} 张图片`+
        (empty?`，有 ${empty} 个空格，将保留底色，不重复填图。`:'，没有空格。');
      this.el('#collagePage').textContent=`第 ${this.page+1} / ${pages.length} 张拼图`;
      this.el('#collageDimensions').textContent=`整图 ${layout.width} × ${layout.height} 像素；每格画面 ${layout.cellWidth} × ${layout.imageHeight} 像素`+
        (layout.captionHeight?'，另有标注栏。':'。');
      if(seamless&&!options.sourceRatio)this.el('#collageDimensions').textContent=`整图宽 ${layout.width} 像素；高度会在读取原图后按原比例确定。`;
      this.el('#collagePrev').disabled=this.page===0||this.busy;
      this.el('#collageNext').disabled=this.page===pages.length-1||this.busy;
      this.el('#collageRender').disabled=this.busy;
    }catch(e){
      notice.className='collageNotice error';notice.textContent=e.message;
      this.el('#collagePage').textContent='';this.el('#collageDimensions').textContent='';
      this.el('#collagePrev').disabled=this.el('#collageNext').disabled=this.el('#collageRender').disabled=true;
    }
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
  async generate(){
    if(this.busy||!this.snapshot)return;
    let options,layout,pages;
    try{options=this.options();layout=this.layout(options);pages=this.pages(this.ids,options.grid,options.split);}
    catch(e){this.el('#collageStatus').textContent=e.message;return;}
    this.invalidate();const token=this.token,page=this.page,indices=[...pages[page]],snapshot=this.snapshot;
    this.busy=true;this.summary();
    const canvas=document.createElement('canvas');canvas.width=layout.width;canvas.height=layout.height;
    canvas.setAttribute('aria-label',`参考拼图第 ${page+1} 张预览`);
    const context=canvas.getContext('2d');
    if(!context){this.busy=false;this.el('#collageStatus').textContent='浏览器无法创建拼图画布';this.summary();return;}
    context.fillStyle=options.background==='dark'?'#202632':'#ffffff';context.fillRect(0,0,canvas.width,canvas.height);
    context.imageSmoothingEnabled=true;context.imageSmoothingQuality='high';
    let completed=false,upscaled=0;
    try{
      for(let position=0;position<indices.length;position++){
        if(token!==this.token)return;
        const id=indices[position];
        this.el('#collageStatus').textContent=`正在读取原图 ${position+1}/${indices.length}…`;
        let image;
        try{image=await this.loadImage(snapshot.frames[id]);}
        catch(e){throw new Error(`原图 #${id+1} 无法读取：${e.message}。未生成不完整拼图，请恢复文件或移除该图片。`);}
        if(token!==this.token){image.src='';return;}
        if(options.shape==='source'&&position===0){
          options.sourceRatio=image.naturalWidth/image.naturalHeight;
          snapshot.sourceRatio=options.sourceRatio;
          layout=this.layout(options);
          canvas.width=layout.width;canvas.height=layout.height;
          context.fillStyle=options.background==='dark'?'#202632':'#ffffff';context.fillRect(0,0,canvas.width,canvas.height);
          context.imageSmoothingEnabled=true;context.imageSmoothingQuality='high';
        }
        if(options.shape==='source'&&Math.abs(image.naturalWidth/image.naturalHeight-options.sourceRatio)>1e-6){
          image.src='';throw new Error('所选图片比例不一致，无法同时无缝且完整显示。请选择相同比例的图片，或切换留白排版。');
        }
        // Integer tile rectangles share exactly one boundary: no gutters or overlaps.
        const box=options.shape==='source'?{sx:0,sy:0,sw:image.naturalWidth,sh:image.naturalHeight,dx:0,dy:0,dw:layout.cellWidth,dh:layout.imageHeight}:
          this.geometry(image.naturalWidth,image.naturalHeight,layout.cellWidth,layout.imageHeight,options.fit);
        if(box.dw>box.sw+1||box.dh>box.sh+1)upscaled++;
        const x=layout.gap+(position%options.grid)*(layout.cellWidth+layout.gap);
        const y=layout.gap+Math.floor(position/options.grid)*(layout.imageHeight+layout.captionHeight+layout.gap);
        context.drawImage(image,box.sx,box.sy,box.sw,box.sh,x+box.dx,y+box.dy,box.dw,box.dh);
        image.src='';
        if(layout.captionHeight){
          const pieces=[];
          if(options.index)pieces.push('#'+String(id+1).padStart(3,'0'));
          if(options.time)pieces.push(snapshot.cuts[id].label||'时间未知');
          context.fillStyle=options.background==='dark'?'#f2f4f8':'#26354b';
          context.font=`${Math.round(layout.captionHeight*0.5)}px "Microsoft YaHei", sans-serif`;
          context.textBaseline='middle';context.fillText(pieces.join('  ·  '),x+4,y+layout.imageHeight+layout.captionHeight/2,layout.cellWidth-8);
        }
      }
      if(token!==this.token)return;
      const mime=options.format==='jpeg'?'image/jpeg':'image/png';
      this.el('#collageStatus').textContent='正在准备下载图片…';
      const blob=await new Promise((resolve,reject)=>canvas.toBlob(value=>value?resolve(value):reject(new Error('图片编码失败，请降低尺寸后重试')),mime,0.95));
      if(token!==this.token)return;
      if(blob.type!==mime)throw new Error('浏览器不支持此格式，请选择 PNG');
      this.downloadUrl=URL.createObjectURL(blob);
      const name=snapshot.name.replace(/\.[^.]+$/,'').replace(/[\\/:*?"<>|\r\n]/g,'_').slice(0,60)||'video';
      const link=this.el('#collageDownload');link.href=this.downloadUrl;
      link.download=`参考拼图_${name}_${options.grid}x${options.grid}_${String(page+1).padStart(2,'0')}.${options.format==='jpeg'?'jpg':'png'}`;
      const preview=this.el('#collagePreview');preview.innerHTML='';preview.appendChild(canvas);
      this.canvas=canvas;this.output={page,pages:pages.length,options,name:snapshot.name,token};completed=true;
      this.el('#collageDownload').disabled=false;
      this.el('#collageDownload').setAttribute('aria-disabled','false');
      this.el('#collageStatus').textContent=`第 ${page+1} 张预览已生成，可下载 ${options.format.toUpperCase()}。`+
        (upscaled?`其中 ${upscaled} 张原图小于目标格子，放大不会增加真实细节。`:'');
    }catch(e){if(token===this.token)this.el('#collageStatus').textContent=e.message||'拼图生成失败，请重试';}
    finally{if(!completed){canvas.width=1;canvas.height=1;}this.busy=false;this.summary();}
  },
  changePage(delta){
    if(this.busy)return;this.page+=delta;this.invalidate();this.summary();
  },
  download(event){
    if(!this.canvas||!this.downloadUrl||!this.output||this.output.token!==this.token){event.preventDefault();return;}
    const output=this.output;
    this.el('#collageStatus').textContent=`已交给浏览器下载第 ${output.page+1}/${output.pages} 张拼图，请在下载列表确认保存结果。`;
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
for(const selector of ['#collageGrid','#collageShape','#collageFit','#collageWidth','#collageBackground','#collageFormat','#collageIndex','#collageTime','#collageSplit']){
  document.querySelector(selector).addEventListener('change',()=>{CollageUI.page=0;CollageUI.invalidate();CollageUI.summary();});
}
document.querySelector('#collagePrev').addEventListener('click',()=>CollageUI.changePage(-1));
document.querySelector('#collageNext').addEventListener('click',()=>CollageUI.changePage(1));
document.querySelector('#collageRender').addEventListener('click',()=>CollageUI.generate());
document.querySelector('#collageDownload').addEventListener('click',event=>CollageUI.download(event));

/* Konva only edits a bounded preview; Pillow owns the final image geometry and pixels. */
class CollageStage {
  constructor(container,onSwap,onCrop,onFocus){
    this.container=container;this.onSwap=onSwap;this.onCrop=onCrop;this.onFocus=onFocus;
    this.stage=null;this.generation=0;this.nodes=new Map();this.focus=null;
  }
  destroy(){this.generation++;if(this.stage)this.stage.destroy();this.stage=null;this.nodes.clear();}
  static normalizedCrop(image,item){
    return {x:Math.max(0,-image.x()/image.width()),y:Math.max(0,-image.y()/image.height()),
      width:Math.min(1,item.cell.width/image.width()),height:Math.min(1,item.target.height/image.height())};
  }
  async show(plan,cropMode){
    this.destroy();const generation=this.generation;this.plan=plan;this.cropMode=cropMode;
    const available=Math.max(240,this.container.clientWidth||640);
    const scale=Math.min(available/plan.width,520/plan.height);
    this.stage=new Konva.Stage({container:this.container,width:Math.round(plan.width*scale),height:Math.round(plan.height*scale),scaleX:scale,scaleY:scale});
    const layer=new Konva.Layer();this.stage.add(layer);
    layer.add(new Konva.Rect({x:0,y:0,width:plan.width,height:plan.height,fill:plan.background==='dark'?'#202632':'#fff',listening:false}));
    for(const item of plan.items){
      const image=new Image();image.src=item.thumb||item.src;
      try{await image.decode();}catch{if(generation===this.generation)throw new Error('缩略图读取失败，请确认截图仍然存在');else return;}
      if(generation!==this.generation)return;
      const cell=item.cell,group=new Konva.Group({x:cell.x,y:cell.y,draggable:!cropMode});
      const clip=new Konva.Group({clipX:0,clipY:0,clipWidth:cell.width,clipHeight:plan.image_height});
      const factor=item.target.width/item.crop.width;
      const picture=new Konva.Image({image,x:item.target.x-cell.x-item.crop.x*factor,y:item.target.y-cell.y-item.crop.y*factor,
        width:item.source_width*factor,height:item.source_height*factor,draggable:cropMode});
      const border=new Konva.Rect({x:0,y:0,width:cell.width,height:plan.image_height,stroke:'#6b72ea',strokeWidth:3/scale,visible:false,listening:false});
      clip.add(picture);group.add(clip);group.add(border);
      if(item.caption)group.add(new Konva.Text({x:4,y:plan.image_height,width:cell.width-8,height:plan.caption_height,text:item.caption,fontSize:Math.max(12,plan.caption_height/2),verticalAlign:'middle',fill:plan.background==='dark'?'white':'#26354b',listening:false}));
      const clamp=()=>{picture.x(Math.max(cell.width-picture.width(),Math.min(0,picture.x())));picture.y(Math.max(plan.image_height-picture.height(),Math.min(0,picture.y())));};
      group.on('click tap',()=>this.select(item.id));
      group.on('dragstart',()=>{if(!cropMode){group.moveToTop();group.opacity(.8);}});
      group.on('dragend',event=>{
        if(cropMode||event.target!==group)return;
        const x=group.x()+cell.width/2,y=group.y()+cell.height/2;
        const target=plan.items.reduce((best,value)=>{
          const d=(value.cell.x+value.cell.width/2-x)**2+(value.cell.y+value.cell.height/2-y)**2;
          return !best||d<best.distance?{id:value.id,distance:d}:best;
        },null);
        group.position({x:cell.x,y:cell.y});group.opacity(1);
        if(target&&target.id!==item.id)this.onSwap(item.id,target.id);
      });
      picture.on('dragstart',()=>this.select(item.id));
      picture.on('dragmove',()=>{if(cropMode)clamp();});
      picture.on('dragend',event=>{
        if(!cropMode)return;event.cancelBubble=true;clamp();
        this.onCrop(item.id,CollageStage.normalizedCrop(picture,item));
      });
      this.nodes.set(item.id,{picture,item,border});layer.add(group);
    }
    if(generation!==this.generation)return;
    if(this.focus!==null)this.select(this.focus);
    this.stage.draw();
  }
  select(id){
    if(!this.nodes.has(id))return;
    this.focus=id;for(const [key,value] of this.nodes)value.border.visible(key===id&&this.cropMode);
    this.stage?.batchDraw();this.onFocus(id);
  }
  zoom(factor){
    const node=this.nodes.get(this.focus);if(!node||!this.cropMode)return;
    const {picture,item}=node,cell=item.cell;
    const minimum=Math.max(cell.width/item.source_width,this.plan.image_height/item.source_height);
    const oldScale=picture.width()/item.source_width;
    const newScale=Math.max(minimum,Math.min(minimum*8,oldScale*factor));
    const ratio=newScale/oldScale;
    const x=cell.width/2-(cell.width/2-picture.x())*ratio;
    const y=this.plan.image_height/2-(this.plan.image_height/2-picture.y())*ratio;
    picture.size({width:item.source_width*newScale,height:item.source_height*newScale});
    picture.position({x:Math.max(cell.width-picture.width(),Math.min(0,x)),y:Math.max(this.plan.image_height-picture.height(),Math.min(0,y))});
    this.onCrop(item.id,CollageStage.normalizedCrop(picture,item));
  }
}

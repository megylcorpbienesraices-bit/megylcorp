/* ITM QUANT v1.26.2 · Native WebGPU quantitative terrain.
 * WebGPU is preferred. WebGL2/Canvas remain deterministic fallbacks.
 * This renderer intentionally keeps the hot visual path outside SolidJS/DOM state.
 */
(()=>{'use strict';
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const matMul=(a,b)=>{const o=new Float32Array(16);for(let c=0;c<4;c++)for(let r=0;r<4;r++){let v=0;for(let k=0;k<4;k++)v+=a[k*4+r]*b[c*4+k];o[c*4+r]=v}return o};
const persp=(fovy,aspect,n=.05,f=50)=>{const q=1/Math.tan(fovy/2),o=new Float32Array(16);o[0]=q/aspect;o[5]=q;o[10]=(f+n)/(n-f);o[11]=-1;o[14]=2*f*n/(n-f);return o};
const trans=(x,y,z)=>new Float32Array([1,0,0,0,0,1,0,0,0,0,1,0,x,y,z,1]);
const rx=a=>{const c=Math.cos(a),s=Math.sin(a);return new Float32Array([1,0,0,0,0,c,s,0,0,-s,c,0,0,0,0,1])};
const ry=a=>{const c=Math.cos(a),s=Math.sin(a);return new Float32Array([c,0,-s,0,0,1,0,0,s,0,c,0,0,0,0,1])};
const VERTEX_STRIDE=28;
const UNIFORM_FLOATS=24;
const UNIFORM_BYTES=UNIFORM_FLOATS*4; // WGSL U = mat4(64) + f32(4) + 12-byte alignment gap + vec3(12) => 96 bytes.

class WebGPUSurface{
 constructor(host,canvas,adapter,device,context,format){
  this.host=host;this.canvas=canvas;this.adapter=adapter;this.device=device;this.context=context;this.format=format;
  this.payload=null;this.field='Gamma';this.yaw=-.62;this.pitch=.70;this.zoom=3.55;this.drag=null;
  this.vertexCount=0;this.wireCount=0;this.contourCount=0;this.dpr=Math.min(devicePixelRatio||1,2);
  this.frameOk=false;this.failed=false;this.validationPending=false;this.lastError='';
  this.attachDeviceGuards();this.initGPU();this.bind();this.themeListener=()=>this.draw();window.addEventListener('itmq:theme-change',this.themeListener);this.ro=new ResizeObserver(()=>this.resize());this.ro.observe(host);this.resize();
 }
 attachDeviceGuards(){
  const d=this.device;
  d?.lost?.then?.(info=>this.fail(new Error(`WebGPU device lost: ${info?.message||info?.reason||'unknown'}`),'DEVICE_LOST')).catch(()=>{});
  d?.addEventListener?.('uncapturederror',e=>{try{e.preventDefault?.()}catch(_){ }this.fail(e?.error||new Error('WebGPU uncaptured validation error'),'UNCAUGHT_GPU_ERROR');});
 }
 status(label,backend){
  const gpu=document.getElementById('surfaceGpuState'),bs=document.getElementById('surfaceBackendStatus');
  if(gpu&&label)gpu.textContent=label;if(bs&&backend)bs.textContent=backend;
 }
 fail(error,code='WEBGPU_FAILED'){
  if(this.failed)return;this.failed=true;this.lastError=String(error?.message||error||code);
  console.error('[ITM QUANT WEBGPU]',code,error);
  this.status('GPU · WEBGPU FAILED',`WEBGPU ERROR · ${code} · FALLBACK WEBGL2`);
  try{this.host?.classList?.add('renderer-degraded')}catch(_){ }
  window.dispatchEvent(new CustomEvent('itmq:webgpu-failed',{detail:{code,message:this.lastError}}));
 }
 markFrameOk(){
  if(this.failed||this.frameOk)return;this.frameOk=true;
  this.status('GPU · WEBGPU ACTIVE · FRAME OK','WEBGPU · WGSL SHADER · FRAME OK');
  window.ITMQChartRuntime?.mark?.('surfaceChart','PRESENT','FRAME_OK','BINARY_SURFACE','WebGPU first frame validated');
  this.updateStatus();
 }
 dispose(){
  try{this.ro?.disconnect?.()}catch(_){ }
  try{window.removeEventListener('itmq:theme-change',this.themeListener)}catch(_){ }
  try{this.depth?.destroy?.()}catch(_){ }
  for(const k of ['uniform','vbuf','wireBuf','contourBuf'])try{this[k]?.destroy?.()}catch(_){ }
 }
 initGPU(){
  const d=this.device;
  const shader=d.createShaderModule({code:`
struct U { mvp: mat4x4<f32>, time: f32, pad: vec3<f32> };
@group(0) @binding(0) var<uniform> u: U;
struct In { @location(0) pos: vec3<f32>, @location(1) col: vec3<f32>, @location(2) conf: f32 };
struct Out { @builtin(position) pos: vec4<f32>, @location(0) col: vec3<f32>, @location(1) conf: f32, @location(2) h: f32 };
@vertex fn vs(i:In)->Out { var o:Out; o.pos=u.mvp*vec4<f32>(i.pos,1.0);o.col=i.col;o.conf=i.conf;o.h=i.pos.z;return o; }
@fragment fn fs(i:Out)->@location(0) vec4<f32>{
  let pulse=0.985+0.015*sin(u.time*1.4+i.h*9.0);
  let uncertainty=clamp(0.28+(1.0-i.conf)*0.62,0.0,0.82);
  let fog=select(vec3<f32>(0.025,0.045,0.060),vec3<f32>(0.965,0.975,0.985),u.pad.x>0.5);
  let c=mix(i.col*pulse,fog,uncertainty*0.50);
  return vec4<f32>(c,0.98);
}`});
  this.bindGroupLayout=d.createBindGroupLayout({entries:[{binding:0,visibility:GPUShaderStage.VERTEX|GPUShaderStage.FRAGMENT,buffer:{type:'uniform'}}]});
  this.pipelineLayout=d.createPipelineLayout({bindGroupLayouts:[this.bindGroupLayout]});
  const vertex={module:shader,entryPoint:'vs',buffers:[{arrayStride:VERTEX_STRIDE,attributes:[
   {shaderLocation:0,offset:0,format:'float32x3'},
   {shaderLocation:1,offset:12,format:'float32x3'},
   {shaderLocation:2,offset:24,format:'float32'}
  ]}]};
  const fragment={module:shader,entryPoint:'fs',targets:[{format:this.format,blend:{color:{srcFactor:'src-alpha',dstFactor:'one-minus-src-alpha',operation:'add'},alpha:{srcFactor:'one',dstFactor:'one-minus-src-alpha',operation:'add'}}}]};
  this.pipeline=d.createRenderPipeline({layout:this.pipelineLayout,vertex,fragment,primitive:{topology:'triangle-list',cullMode:'back'},depthStencil:{format:'depth24plus',depthWriteEnabled:true,depthCompare:'less'}});
  this.linePipeline=d.createRenderPipeline({layout:this.pipelineLayout,vertex,fragment,primitive:{topology:'line-list'},depthStencil:{format:'depth24plus',depthWriteEnabled:false,depthCompare:'less-equal'}});
  this.uniform=d.createBuffer({size:UNIFORM_BYTES,usage:GPUBufferUsage.UNIFORM|GPUBufferUsage.COPY_DST});
  this.bindGroup=d.createBindGroup({layout:this.bindGroupLayout,entries:[{binding:0,resource:{buffer:this.uniform}}]});
  this.vbuf=this.makeBuffer(VERTEX_STRIDE*6*100000);
  this.wireBuf=this.makeBuffer(VERTEX_STRIDE*2*100000);
  this.contourBuf=this.makeBuffer(VERTEX_STRIDE*2*100000);
 }
 makeBuffer(size){return this.device.createBuffer({size:Math.max(256,Math.ceil(size/256)*256),usage:GPUBufferUsage.VERTEX|GPUBufferUsage.COPY_DST});}
 ensureBuffer(name,byteLength){const b=this[name];if(b&&b.size>=byteLength)return;b?.destroy?.();this[name]=this.makeBuffer(byteLength);}
 bind(){
  this.canvas.addEventListener('pointerdown',e=>{this.canvas.setPointerCapture(e.pointerId);this.drag={x:e.clientX,y:e.clientY,yaw:this.yaw,pitch:this.pitch}});
  this.canvas.addEventListener('pointermove',e=>{if(!this.drag)return;this.yaw=this.drag.yaw+(e.clientX-this.drag.x)*.008;this.pitch=clamp(this.drag.pitch+(e.clientY-this.drag.y)*.008,.15,1.35);this.draw()});
  this.canvas.addEventListener('pointerup',()=>this.drag=null);this.canvas.addEventListener('pointercancel',()=>this.drag=null);
  this.canvas.addEventListener('wheel',e=>{e.preventDefault();this.zoom=clamp(this.zoom*(e.deltaY>0?1.08:.92),2.1,8);this.draw()},{passive:false});
  this.canvas.addEventListener('dblclick',()=>this.resetView());
 }
 resize(){
  const r=this.host.getBoundingClientRect(),w=Math.max(360,Math.round(r.width)),h=Math.max(360,Math.round(r.height)),q=window.ITMQPerformanceCore?.runtime?.quality?.get?.(),scale=clamp(Number(q?.surfaceResolution)||1,.55,1);
  this.dpr=Math.min(devicePixelRatio||1,2)*scale;this.canvas.width=Math.round(w*this.dpr);this.canvas.height=Math.round(h*this.dpr);this.canvas.style.width=w+'px';this.canvas.style.height=h+'px';
  this.context.configure({device:this.device,format:this.format,alphaMode:'opaque'});
  this.depth?.destroy?.();this.depth=this.device.createTexture({size:[this.canvas.width,this.canvas.height],format:'depth24plus',usage:GPUTextureUsage.RENDER_ATTACHMENT});this.draw();
 }
 resetView(){this.yaw=-.62;this.pitch=.70;this.zoom=3.55;this.draw()}
 setPayload(p,field=null){if(!p?.ready){window.ITMQChartRuntime?.mark?.('surfaceChart','WAITING','WAITING','BINARY_SURFACE','surface payload not ready');return;}if(field)this.field=field;this.payload=p;window.ITMQChartRuntime?.mark?.('surfaceChart','PRESENT','BUILDING','BINARY_SURFACE',`field=${this.field}`);this.rebuild();this.draw();this.updateStatus()}
 setField(f){const next=f||'Gamma';if(next===this.field){this.updateStatus();return;}this.field=next;this.rebuild();this.draw();this.updateStatus()}
 updateStatus(){
  const m=document.getElementById('surfaceNativeMetric'),st=document.getElementById('surfaceNativeStatus'),bs=document.getElementById('surfaceBackendStatus'),gpu=document.getElementById('surfaceGpuState');
  if(m)m.textContent=String(this.field).toUpperCase();
  if(st&&this.payload){const a=this.payload.fields?.[this.field]||[];st.textContent=`${a.length||0} strikes × ${(a[0]||[]).length||0} expiries · WebGPU terrain + wireframe + contours · confidence fog · ${this.payload.weights_source||''}`;}
  if(bs)bs.textContent=this.frameOk?'WEBGPU · WGSL SHADER · FRAME OK':'WEBGPU · WGSL SHADER · READY · AWAITING FRAME';
  if(gpu)gpu.textContent=this.frameOk?'GPU · WEBGPU ACTIVE · FRAME OK':'GPU · WEBGPU READY';
 }
 rebuild(){
  const p=this.payload,a=p?.fields?.[this.field];
  if(!Array.isArray(a)||!a.length||!Array.isArray(a[0])){this.vertexCount=this.wireCount=this.contourCount=0;return}
  const ny=a.length,nx=a[0].length,cf=p.confidence||[],vals=a.flat().map(Number).filter(Number.isFinite),max=Math.max(1e-12,...vals.map(Math.abs));
  const at=(y,x)=>Number(a[clamp(y,0,ny-1)]?.[clamp(x,0,nx-1)]||0),conf=(y,x)=>clamp(Number(cf[clamp(y,0,ny-1)]?.[clamp(x,0,nx-1)]??.5),.05,1);
  const cache=[];
  for(let y=0;y<ny;y++){
   cache[y]=[];
   for(let x=0;x<nx;x++){
    const v=at(y,x),m=clamp(Math.abs(v)/max,0,1),sg=v>=0?1:-1,xx=nx===1?0:(x/(nx-1)-.5)*2.45,yy=ny===1?0:(.5-y/(ny-1))*2.0,zz=sg*Math.pow(m,.72)*.72;
    let col=this.field==='IV'?[.20+.20*m,.46+.27*m,.72+.20*m]:(v>=0?[.08+.09*(1-m),.34+.53*m,.46+.20*m]:[.52+.38*m,.12+.08*(1-m),.30+.18*m]);
    cache[y][x]={v,n:clamp(v/max,-1,1),p:[xx,yy,zz,...col,conf(y,x)]};
   }
  }
  const verts=[],wire=[],cont=[];
  const wireColor=[.17,.56,.72],wireStrideY=Math.max(1,Math.floor(ny/20)),wireStrideX=Math.max(1,Math.floor(nx/20));
  const addLine=(arr,a,b,col,confv=.90,zoff=.006)=>arr.push(a[0],a[1],a[2]+zoff,...col,confv,b[0],b[1],b[2]+zoff,...col,confv);
  for(let y=0;y<ny-1;y++)for(let x=0;x<nx-1;x++){
   const a0=cache[y][x].p,a1=cache[y][x+1].p,a2=cache[y+1][x].p,a3=cache[y+1][x+1].p;
   // Keep triangle winding CCW for WebGPU back-face culling with top->bottom rows.
   verts.push(...a0,...a2,...a1,...a1,...a2,...a3);
   if(y%wireStrideY===0)addLine(wire,a0,a1,wireColor,.80,.009);
   if(x%wireStrideX===0)addLine(wire,a0,a2,wireColor,.80,.009);
  }
  for(let x=0;x<nx-1;x++)addLine(wire,cache[ny-1][x].p,cache[ny-1][x+1].p,wireColor,.80,.009);
  for(let y=0;y<ny-1;y++)addLine(wire,cache[y][nx-1].p,cache[y+1][nx-1].p,wireColor,.80,.009);

  // Ground-plane isolines via marching-squares edge intersections. These are
  // analytical contours, not decorative geometry, and use normalized field levels.
  const levels=[-.60,-.30,0,.30,.60],ground=-.82;
  const xy=(y,x)=>{const q=cache[y][x].p;return[q[0],q[1],ground]};
  const interp=(p1,p2,v1,v2,l)=>{const t=Math.abs(v2-v1)<1e-9?.5:clamp((l-v1)/(v2-v1),0,1);return[p1[0]+(p2[0]-p1[0])*t,p1[1]+(p2[1]-p1[1])*t,ground]};
  for(const lev of levels){
   const col=lev>0?[.16,.78,.56]:lev<0?[.95,.23,.38]:[.98,.72,.24];
   for(let y=0;y<ny-1;y++)for(let x=0;x<nx-1;x++){
    const ps=[xy(y,x),xy(y,x+1),xy(y+1,x+1),xy(y+1,x)],vs=[cache[y][x].n,cache[y][x+1].n,cache[y+1][x+1].n,cache[y+1][x].n],cuts=[];
    for(let e=0;e<4;e++){const j=(e+1)%4,v1=vs[e],v2=vs[j];if(((v1<=lev&&v2>=lev)||(v1>=lev&&v2<=lev))&&Math.abs(v1-v2)>1e-8)cuts.push(interp(ps[e],ps[j],v1,v2,lev));}
    if(cuts.length===2)addLine(cont,cuts[0],cuts[1],col,.92,0);
    else if(cuts.length>=4){addLine(cont,cuts[0],cuts[1],col,.92,0);addLine(cont,cuts[2],cuts[3],col,.92,0);}
   }
  }
  const vf=new Float32Array(verts),wf=new Float32Array(wire),cf32=new Float32Array(cont);
  this.ensureBuffer('vbuf',vf.byteLength);this.ensureBuffer('wireBuf',wf.byteLength);this.ensureBuffer('contourBuf',cf32.byteLength);
  if(vf.byteLength)this.device.queue.writeBuffer(this.vbuf,0,vf);if(wf.byteLength)this.device.queue.writeBuffer(this.wireBuf,0,wf);if(cf32.byteLength)this.device.queue.writeBuffer(this.contourBuf,0,cf32);
  this.vertexCount=vf.length/7;this.wireCount=wf.length/7;this.contourCount=cf32.length/7;
 }
 draw(){
  if(this.failed||!this.device||!this.vertexCount||!this.depth)return;
  try{
   const aspect=this.canvas.width/Math.max(1,this.canvas.height),mvp=matMul(persp(.70,aspect),matMul(trans(0,0,-this.zoom),matMul(rx(this.pitch),ry(this.yaw))));
   const ub=new Float32Array(UNIFORM_FLOATS);ub.set(mvp,0);ub[16]=performance.now()/1000;ub[17]=document.documentElement.dataset.theme==='light'?1:0;
   // ub[18..23] remain zero and satisfy WGSL struct alignment/padding.
   this.device.queue.writeBuffer(this.uniform,0,ub);
   const validate=!this.frameOk&&!this.validationPending;
   if(validate){this.validationPending=true;this.device.pushErrorScope('validation');}
   const enc=this.device.createCommandEncoder(),view=this.context.getCurrentTexture().createView(),pass=enc.beginRenderPass({colorAttachments:[{view,clearValue:document.documentElement.dataset.theme==='light'?{r:.965,g:.975,b:.985,a:1}:{r:.008,g:.018,b:.028,a:1},loadOp:'clear',storeOp:'store'}],depthStencilAttachment:{view:this.depth.createView(),depthClearValue:1,depthLoadOp:'clear',depthStoreOp:'store'}});
   pass.setBindGroup(0,this.bindGroup);
   pass.setPipeline(this.pipeline);pass.setVertexBuffer(0,this.vbuf);pass.draw(this.vertexCount);
   pass.setPipeline(this.linePipeline);
   if(this.wireCount){pass.setVertexBuffer(0,this.wireBuf);pass.draw(this.wireCount)}
   if(this.contourCount){pass.setVertexBuffer(0,this.contourBuf);pass.draw(this.contourCount)}
   pass.end();this.device.queue.submit([enc.finish()]);
   if(validate){
    Promise.resolve(this.device.popErrorScope()).then(err=>{
     if(err)throw err;return this.device.queue.onSubmittedWorkDone();
    }).then(()=>this.markFrameOk()).catch(err=>this.fail(err,'FIRST_FRAME_VALIDATION')).finally(()=>{this.validationPending=false;});
   }
  }catch(e){this.fail(e,'RENDER_EXCEPTION');}
 }
 async enterXR(){return false}
}
async function create(hostId,canvasId){if(!navigator.gpu)return null;try{const host=document.getElementById(hostId),canvas=document.getElementById(canvasId);if(!host||!canvas)return null;const adapter=await navigator.gpu.requestAdapter();if(!adapter)return null;const device=await adapter.requestDevice();const context=canvas.getContext('webgpu');if(!context)return null;const format=navigator.gpu.getPreferredCanvasFormat();device.pushErrorScope('validation');let surface=null;try{surface=new WebGPUSurface(host,canvas,adapter,device,context,format);}catch(e){await device.popErrorScope().catch(()=>null);throw e;}const initErr=await device.popErrorScope();if(initErr){surface?.dispose?.();console.warn('ITM WebGPU init validation failed; WebGL fallback',initErr);return null;}surface.status('GPU · WEBGPU READY','WEBGPU · WGSL SHADER · READY · AWAITING FRAME');return surface}catch(e){console.warn('ITM WebGPU unavailable; WebGL fallback',e);return null}}
window.ITMQWebGPU={create,supported:()=>!!navigator.gpu};
})();

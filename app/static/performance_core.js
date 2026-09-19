/* ITM QUANT v1.27.2 · PERFORMANCE & INTERACTION CORE
 * PRESENTATION-ONLY authority. This file never changes Scanner, risk, signal,
 * execution, calibration, replay, or quantitative math. It only schedules,
 * coalesces and diagnoses browser-side display work after the Quant Core.
 */
(()=>{
'use strict';

const MODE_ORDER={MAX:0,SMOOTH:1,BALANCED:2,PROTECT:3};
const now=()=>performance.now();
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));

class RingQueue{
  constructor(capacity=1024){
    let c=1;while(c<Math.max(16,capacity))c<<=1;
    this.buf=new Array(c);this.mask=c-1;this.head=0;this.tail=0;this.length=0;
  }
  _grow(){
    const old=this.buf,next=new Array(old.length<<1);
    for(let i=0;i<this.length;i++)next[i]=old[(this.head+i)&this.mask];
    this.buf=next;this.mask=next.length-1;this.head=0;this.tail=this.length;
  }
  push(v){if(this.length>=this.buf.length-1)this._grow();this.buf[this.tail]=v;this.tail=(this.tail+1)&this.mask;this.length++;}
  shift(){if(!this.length)return undefined;const v=this.buf[this.head];this.buf[this.head]=undefined;this.head=(this.head+1)&this.mask;this.length--;return v;}
  peek(){return this.length?this.buf[this.head]:undefined;}
  clear(){this.buf.fill(undefined);this.head=this.tail=this.length=0;}
}

class PerformanceGovernor{
  constructor(opts={}){
    this.maxTargetFPS=Number(opts.targetFPS)||120;
    this.maxQueue=Number(opts.maxQueue)||6000;
    this.mode='MAX';this.visualThrottle=1;
    this.frameTimes=[];this.lastFrame=0;this.displayHz=60;this.targetFPS=60;
    this.pendingMode=null;this.pendingSince=0;this.longTaskUntil=0;
    this.listeners=new Set();
    this.metrics={fps:60,targetFPS:60,displayHz:60,frameMs:16.67,queue:0,drainMs:0,renderMs:0,longTasks:0,lastLongTaskMs:0,droppedFrames:0,memoryMB:null,visibility:document.hidden?'HIDDEN':'VISIBLE',authority:'PRESENTATION_ONLY'};
  }
  onChange(cb){this.listeners.add(cb);return()=>this.listeners.delete(cb);}
  emit(){const s=this.snapshot();for(const cb of this.listeners){try{cb(s);}catch(_){}}window.dispatchEvent(new CustomEvent('itmq:performance-mode',{detail:s}));}
  snapshot(){return{...this.metrics,mode:this.mode,visualThrottle:this.visualThrottle};}
  noteLongTask(ms){const t=now();this.metrics.longTasks++;this.metrics.lastLongTaskMs=Number((Number(ms)||0).toFixed(1));this.longTaskUntil=Math.max(this.longTaskUntil,t+2500);this.recalculate(t);}
  reportQueueSize(n){this.metrics.queue=Math.max(0,Number(n)||0);this.recalculate(now());}
  reportDrainTime(ms){this.metrics.drainMs=Number((Number(ms)||0).toFixed(2));}
  reportRenderTime(ms){this.metrics.renderMs=Number((Number(ms)||0).toFixed(2));}
  reportVisibility(){this.metrics.visibility=document.hidden?'HIDDEN':'VISIBLE';this.recalculate(now());}
  _detectDisplay(){
    if(this.frameTimes.length<30)return;
    const xs=this.frameTimes.slice().sort((a,b)=>a-b),idx=Math.min(xs.length-1,Math.floor(xs.length*.20));
    const dt=xs[idx];if(!(dt>3&&dt<50))return;
    const raw=1000/dt,common=[30,50,60,72,75,90,100,120,144,165,180,200,240];
    let hz=common.reduce((best,x)=>Math.abs(x-raw)<Math.abs(best-raw)?x:best,60);
    if(Math.abs(hz-raw)>12)hz=Math.round(raw);
    this.displayHz=clamp(hz,30,240);this.targetFPS=Math.min(this.maxTargetFPS,this.displayHz);
    this.metrics.displayHz=this.displayHz;this.metrics.targetFPS=this.targetFPS;
  }
  tickFrame(ts=now()){
    if(!this.lastFrame){this.lastFrame=ts;return;}
    const dt=ts-this.lastFrame;this.lastFrame=ts;if(!(dt>0&&dt<1000))return;
    if(dt<100){this.frameTimes.push(dt);if(this.frameTimes.length>120)this.frameTimes.shift();}
    this._detectDisplay();
    const recent=this.frameTimes.slice(-45);const avg=recent.length?recent.reduce((a,b)=>a+b,0)/recent.length:dt;
    const fps=1000/Math.max(1,avg);this.metrics.fps=Math.round(fps);this.metrics.frameMs=Number(dt.toFixed(2));
    const expected=1000/Math.max(1,this.targetFPS);this.metrics.droppedFrames+=Math.max(0,Math.floor(dt/expected)-1);
    const mem=performance?.memory?.usedJSHeapSize;if(Number.isFinite(mem))this.metrics.memoryMB=Math.round(mem/1048576);
    this.recalculate(ts);
  }
  _desired(t){
    if(document.hidden)return'PROTECT';
    const q=this.metrics.queue,ratio=this.metrics.fps/Math.max(1,this.targetFPS),long=t<this.longTaskUntil;
    if(q>this.maxQueue||ratio<.55||(long&&ratio<.72))return'PROTECT';
    if(q>this.maxQueue*.50||ratio<.72||long)return'BALANCED';
    if(q>this.maxQueue*.18||ratio<.90)return'SMOOTH';
    return'MAX';
  }
  recalculate(t=now()){
    const desired=this._desired(t);if(desired===this.mode){this.pendingMode=null;this.pendingSince=0;return;}
    if(this.pendingMode!==desired){this.pendingMode=desired;this.pendingSince=t;}
    const moreSevere=MODE_ORDER[desired]>MODE_ORDER[this.mode];
    const dwell=moreSevere?(desired==='PROTECT'?0:220):3000;
    if(t-this.pendingSince<dwell)return;
    this.mode=desired;this.pendingMode=null;this.pendingSince=0;
    this.visualThrottle=desired==='MAX'?1:desired==='SMOOTH'?.82:desired==='BALANCED'?.62:.38;
    this.emit();
  }
  allowVisual(priority=2){
    if(priority<=1)return true;
    if(this.mode==='MAX')return true;
    if(this.mode==='SMOOTH')return priority<=2;
    if(this.mode==='BALANCED')return priority<=2;
    return priority<=1;
  }
}

class HotDisplayState{
  constructor(){this.map=new Map();this.versions=new Map();}
  ensure(symbol='GLOBAL'){
    symbol=String(symbol||'GLOBAL').toUpperCase();
    if(!this.map.has(symbol))this.map.set(symbol,{price:null,bid:null,ask:null,last:null,lastTickSeq:0,tickCount:0,surface:{},updatedAt:0});
    return this.map.get(symbol);
  }
  patch(symbol,patch){const s=this.ensure(symbol);Object.assign(s,patch);s.updatedAt=now();this.versions.set(String(symbol).toUpperCase(),(this.versions.get(String(symbol).toUpperCase())||0)+1);return s;}
  noteTicks(payload){const symbol=String(payload?.symbol||'GLOBAL').toUpperCase(),ticks=Array.isArray(payload?.ticks)?payload.ticks:[],last=ticks[ticks.length-1];const s=this.ensure(symbol);if(last){s.price=last.price;s.last=last.price;}s.lastTickSeq=Math.max(Number(s.lastTickSeq)||0,Number(payload?.last_seq)||0);s.tickCount=(Number(s.tickCount)||0)+ticks.length;s.updatedAt=now();return s;}
  noteSurface(payload){const s=this.ensure('GLOBAL');if(payload?.field)s.surface[payload.field]={sequence:payload.sequence,rows:payload.rows,cols:payload.cols,updatedAt:now()};s.updatedAt=now();return s;}
  get(symbol='GLOBAL'){return this.ensure(symbol);}
}

class DirtyRegistry{
  constructor(){this.keys=new Set();this.listeners=new Map();}
  mark(key){if(key)this.keys.add(String(key));}
  subscribe(prefix,cb){prefix=String(prefix);if(!this.listeners.has(prefix))this.listeners.set(prefix,new Set());this.listeners.get(prefix).add(cb);return()=>this.listeners.get(prefix)?.delete(cb);}
  flush(){if(!this.keys.size)return[];const keys=[...this.keys];this.keys.clear();for(const key of keys)for(const [prefix,cbs] of this.listeners)if(key.startsWith(prefix))for(const cb of cbs){try{cb(key);}catch(e){console.warn('[ITM DIRTY]',e);}}return keys;}
}

class DisplayEventBus{
  constructor({governor,hotState,dirty}){this.governor=governor;this.hotState=hotState;this.dirty=dirty;this.queue=new RingQueue(2048);this.scheduled=false;this.processing=false;}
  enqueue(event){this.queue.push(event);this.governor.reportQueueSize(this.queue.length);this.schedule();}
  ingestTicks(payload){this.hotState.noteTicks(payload);this.dirty.mark(`${payload?.symbol||'GLOBAL'}:PRICE`);window.dispatchEvent(new CustomEvent('itmq:binary-ticks',{detail:payload}));}
  ingestSurface(payload){this.enqueue({kind:'BINARY_SURFACE',payload});}
  schedule(){if(this.scheduled)return;this.scheduled=true;setTimeout(()=>{this.scheduled=false;this.drainSlice();},0);}
  _dispatchTicks(first){
    const merged={symbol:first.payload?.symbol||'',last_seq:Number(first.payload?.last_seq)||0,ticks:[]};
    const append=p=>{if(!p)return;merged.symbol=merged.symbol||p.symbol||'';merged.last_seq=Math.max(merged.last_seq,Number(p.last_seq)||0);if(Array.isArray(p.ticks)&&p.ticks.length)merged.ticks.push(...p.ticks);};
    append(first.payload);
    while(this.queue.length){const n=this.queue.peek();if(n?.kind!=='BINARY_TICKS'||(n.payload?.symbol&&merged.symbol&&String(n.payload.symbol)!==String(merged.symbol)))break;append(this.queue.shift().payload);}
    this.hotState.noteTicks(merged);this.dirty.mark(`${merged.symbol||'GLOBAL'}:PRICE`);this.dirty.mark(`${merged.symbol||'GLOBAL'}:TRACE`);
    window.dispatchEvent(new CustomEvent('itmq:binary-ticks',{detail:merged}));
  }
  _dispatch(event){
    if(event.kind==='BINARY_TICKS'){this._dispatchTicks(event);return;}
    if(event.kind==='BINARY_SURFACE'){this.hotState.noteSurface(event.payload);this.dirty.mark(`GLOBAL:SURFACE:${event.payload?.field||''}`);window.dispatchEvent(new CustomEvent('itmq:binary-surface',{detail:event.payload}));return;}
    if(event.name){this.dirty.mark(event.dirty||event.name);window.dispatchEvent(new CustomEvent(event.name,{detail:event.payload}));}
  }
  drainSlice(budgetMs=null){
    if(this.processing)return;this.processing=true;const start=now(),budget=Number(budgetMs)||(this.governor.mode==='PROTECT'?3.2:2.0),deadline=start+budget;
    try{while(this.queue.length&&now()<deadline){const ev=this.queue.shift();if(ev)this._dispatch(ev);}}
    finally{this.processing=false;this.governor.reportQueueSize(this.queue.length);this.governor.reportDrainTime(now()-start);if(this.queue.length)this.schedule();}
  }
}

class Float32BufferPool{
  constructor(){this.pool=new Map();}
  bucket(size){let n=256;while(n<Math.max(1,size))n<<=1;return n;}
  acquire(size){const b=this.bucket(size),list=this.pool.get(b)||[];this.pool.set(b,list);return list.pop()||new Float32Array(b);}
  release(buf){if(!(buf instanceof Float32Array))return;const b=buf.length,list=this.pool.get(b)||[];if(list.length<12)list.push(buf);this.pool.set(b,list);}
}

class FrameScheduler{
  constructor(governor,dirty,bus){this.governor=governor;this.dirty=dirty;this.bus=bus;this.pending=new Map();this.running=false;this.raf=0;}
  schedule(key,fn,priority=2){const k=String(key);const cur=this.pending.get(k);if(cur){cur.fn=fn;cur.priority=Math.min(cur.priority,priority);return;}this.pending.set(k,{fn,priority});}
  _runPending(){if(!this.pending.size)return;const rows=[...this.pending.entries()].sort((a,b)=>a[1].priority-b[1].priority);for(const [key,task] of rows){if(!this.governor.allowVisual(task.priority))continue;this.pending.delete(key);try{task.fn();}catch(e){console.warn(`[ITM FRAME] ${key}`,e);}}}
  start(){if(this.running)return;this.running=true;const loop=ts=>{if(!this.running)return;const st=now();this.governor.tickFrame(ts);if(this.bus.queue.length)this.bus.drainSlice(.7);this.dirty.flush();this._runPending();this.governor.reportRenderTime(now()-st);this.raf=requestAnimationFrame(loop);};this.raf=requestAnimationFrame(loop);}
  stop(){this.running=false;if(this.raf)cancelAnimationFrame(this.raf);this.raf=0;}
}

class AdaptiveVisualQuality{
  constructor(governor){this.governor=governor;this.state={labelDensity:1,surfaceResolution:1,decorativeEffects:true};governor.onChange(()=>this.apply(governor.mode));this.apply(governor.mode);}
  apply(mode){
    this.state=mode==='MAX'?{labelDensity:1,surfaceResolution:1,decorativeEffects:true}:mode==='SMOOTH'?{labelDensity:.88,surfaceResolution:.92,decorativeEffects:true}:mode==='BALANCED'?{labelDensity:.68,surfaceResolution:.78,decorativeEffects:false}:{labelDensity:.50,surfaceResolution:.62,decorativeEffects:false};
    document.documentElement.dataset.itmPerfMode=mode;
  }
  get(){return this.state;}
}

class VirtualListRecycler{
  constructor({container,rowHeight=28,overscan=8,getItems,renderRow}){this.container=container;this.rowHeight=rowHeight;this.overscan=overscan;this.getItems=getItems;this.renderRow=renderRow;this.pool=[];this.spacer=document.createElement('div');this.spacer.style.position='relative';container.replaceChildren(this.spacer);this.onScroll=()=>this.render();container.addEventListener('scroll',this.onScroll,{passive:true});this.ro=new ResizeObserver(()=>this.render());this.ro.observe(container);this.render();}
  _row(){const r=this.pool.pop()||document.createElement('div');r.style.position='absolute';r.style.left='0';r.style.right='0';return r;}
  render(){const items=this.getItems?.()||[],h=this.container.clientHeight,top=this.container.scrollTop,start=Math.max(0,Math.floor(top/this.rowHeight)-this.overscan),count=Math.ceil(h/this.rowHeight)+this.overscan*2,end=Math.min(items.length,start+count);while(this.spacer.firstChild){const n=this.spacer.firstChild;this.spacer.removeChild(n);this.pool.push(n);}this.spacer.style.height=`${items.length*this.rowHeight}px`;for(let i=start;i<end;i++){const row=this._row();row.style.top=`${i*this.rowHeight}px`;row.style.height=`${this.rowHeight}px`;this.renderRow(row,items[i],i);this.spacer.appendChild(row);}}
  refresh(){this.render();}
  destroy(){this.ro.disconnect();this.container.removeEventListener('scroll',this.onScroll);}
}

function createPanel(runtime){
  let panel=document.getElementById('itmPerformancePanel');if(!panel){panel=document.createElement('aside');panel.id='itmPerformancePanel';panel.className='itm-performance-panel';panel.hidden=true;document.body.appendChild(panel);}
  const draw=()=>{const m=runtime.governor.snapshot(),mem=m.memoryMB==null?'—':`${m.memoryMB} MB`,lat=window.ITMQLiveLatency?.snapshot?.()||{},paint=lat.browser_to_paint_p95_ms==null?'—':`${lat.browser_to_paint_p95_ms} ms`;panel.innerHTML=`<div class="itm-perf-head"><b>ITM PERFORMANCE</b><span>${m.mode}</span></div><div class="itm-perf-grid"><span>AUTHORITY</span><b>PRESENTATION ONLY</b><span>FPS</span><b>${m.fps} / ${m.targetFPS}</b><span>DISPLAY</span><b>${m.displayHz} Hz</b><span>FRAME</span><b>${m.frameMs} ms</b><span>PRICE PAINT P95</span><b>${paint}</b><span>QUEUE</span><b>${m.queue}</b><span>DRAIN</span><b>${m.drainMs} ms</b><span>LONG TASKS</span><b>${m.longTasks}</b><span>JS HEAP</span><b>${mem}</b><span>VISIBILITY</span><b>${m.visibility}</b></div><small>FPS governor never throttles Scanner, math, risk, signal, causal logs or execution.</small>`;const t=document.getElementById('performanceToggle');if(t)t.textContent=`PERF · ${m.mode}`;};
  runtime.governor.onChange(draw);setInterval(draw,500);draw();
  document.addEventListener('click',e=>{if(e.target?.id==='performanceToggle')panel.hidden=!panel.hidden;});
  return panel;
}

function boot(config={}){
  if(window.ITMQPerformanceCore?.runtime)return window.ITMQPerformanceCore.runtime;
  const governor=new PerformanceGovernor(config),hotState=new HotDisplayState(),dirty=new DirtyRegistry(),bus=new DisplayEventBus({governor,hotState,dirty}),bufferPool=new Float32BufferPool(),scheduler=new FrameScheduler(governor,dirty,bus),quality=new AdaptiveVisualQuality(governor);
  const runtime={governor,hotState,dirty,bus,bufferPool,scheduler,quality,authority:'PRESENTATION_ONLY',version:(window.ITMQ_VERSION||'unknown'),coalesce:(key,fn,priority=2)=>scheduler.schedule(key,fn,priority),ingestTicks:p=>bus.ingestTicks(p),ingestSurface:p=>bus.ingestSurface(p),snapshot:()=>governor.snapshot()};
  try{if('PerformanceObserver'in window){const po=new PerformanceObserver(list=>{for(const e of list.getEntries())governor.noteLongTask(e.duration||0);});po.observe({entryTypes:['longtask']});runtime.longTaskObserver=po;}}catch(_){ }
  document.addEventListener('visibilitychange',()=>governor.reportVisibility());
  scheduler.start();window.ITMQPerformanceCore={runtime,RingQueue,PerformanceGovernor,HotDisplayState,DirtyRegistry,DisplayEventBus,Float32BufferPool,FrameScheduler,AdaptiveVisualQuality,VirtualListRecycler,boot};
  createPanel(runtime);console.log(`[ITM] Performance Core ${window.ITMQ_VERSION||'unknown'} ready · PRESENTATION_ONLY`);return runtime;
}

window.ITMQPerformanceCore={boot,RingQueue,PerformanceGovernor,HotDisplayState,DirtyRegistry,DisplayEventBus,Float32BufferPool,FrameScheduler,AdaptiveVisualQuality,VirtualListRecycler};
document.addEventListener('DOMContentLoaded',()=>boot({targetFPS:120,maxQueue:6000}));
})();

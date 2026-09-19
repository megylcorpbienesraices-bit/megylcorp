/* ITM QUANT v1.40.0 · TOOL WORKER
 * Pure presentation/data-shaping jobs. No Scanner authority, no provider access.
 */
'use strict';
function finite(v){v=Number(v);return Number.isFinite(v)?v:NaN;}
function cumulative(values){const out=new Float64Array(values.length);let acc=0;for(let i=0;i<values.length;i++){const v=finite(values[i]);if(Number.isFinite(v))acc+=v;out[i]=acc;}return out;}
function derivative(values){const out=new Float64Array(values.length);out[0]=0;for(let i=1;i<values.length;i++){const a=finite(values[i-1]),b=finite(values[i]);out[i]=Number.isFinite(a)&&Number.isFinite(b)?b-a:0;}return out;}
function robustScale(values,q=.95){const xs=Array.from(values,finite).filter(Number.isFinite).map(Math.abs).sort((a,b)=>a-b);if(!xs.length)return{cap:1,mvc:null};const idx=Math.min(xs.length-1,Math.max(0,Math.floor((xs.length-1)*q)));const cap=Math.max(xs[idx],1e-12);const mvc=xs[xs.length-1];return{cap,mvc};}
function migrationDifference(current,previous){const n=Math.min(current.length,previous.length),out=new Float64Array(n);for(let i=0;i<n;i++){const a=finite(current[i]),b=finite(previous[i]);out[i]=Number.isFinite(a)&&Number.isFinite(b)?a-b:NaN;}return out;}
function bubbleScale(values){const {cap}=robustScale(values,.95),out=new Float32Array(values.length);for(let i=0;i<values.length;i++){const v=Math.abs(finite(values[i]));out[i]=Number.isFinite(v)?1+19*Math.min(1,v/cap):0;}return{values:out,cap};}
function normalizeSurface(values){const {cap,mvc}=robustScale(values,.97),out=new Float32Array(values.length);for(let i=0;i<values.length;i++){const v=finite(values[i]);out[i]=Number.isFinite(v)?Math.max(-1,Math.min(1,v/cap)):NaN;}return{values:out,cap,mvc};}
function visibleRange(times,values,start,end){const ts=[],vs=[];const a=Number(start),b=Number(end);for(let i=0;i<Math.min(times.length,values.length);i++){const t=finite(times[i]);if(Number.isFinite(t)&&(!Number.isFinite(a)||t>=a)&&(!Number.isFinite(b)||t<=b)){ts.push(t);vs.push(finite(values[i]));}}return{times:new Float64Array(ts),values:new Float64Array(vs)};}
self.onmessage=e=>{const m=e.data||{},id=m.id,job=String(m.job||'').toUpperCase(),p=m.payload||{};try{let result=null,transfer=[];
 if(job==='CUMULATIVE_SERIES'){const a=cumulative(p.values||[]);result={values:a};transfer=[a.buffer];}
 else if(job==='DERIVATIVE_SERIES'){const a=derivative(p.values||[]);result={values:a};transfer=[a.buffer];}
 else if(job==='ROBUST_SCALE'){result=robustScale(p.values||[],Number(p.quantile)||.95);}
 else if(job==='MIGRATION_DIFFERENCE'){const a=migrationDifference(p.current||[],p.previous||[]);result={values:a};transfer=[a.buffer];}
 else if(job==='VISIBLE_BUBBLE_SCALE'){const r=bubbleScale(p.values||[]);result=r;transfer=[r.values.buffer];}
 else if(job==='SURFACE_NORMALIZE'){const r=normalizeSurface(p.values||[]);result=r;transfer=[r.values.buffer];}
 else if(job==='TRACE_VISIBLE_RANGE'){const r=visibleRange(p.times||[],p.values||[],p.start,p.end);result=r;transfer=[r.times.buffer,r.values.buffer];}
 else throw new Error(`UNKNOWN_JOB:${job}`);
 self.postMessage({id,ok:true,result},transfer);
 }catch(err){self.postMessage({id,ok:false,error:String(err?.message||err)});}};

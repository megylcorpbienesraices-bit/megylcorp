/* ITM QUANT v1.40.0 · unified institutional chart system.
 * Native Canvas 2D for high-value diagnostic charts, linked strike hover,
 * deep-tech readiness, stochastic scenario lab, QPU readiness and WebXR hooks.
 * This layer has ZERO Scanner authority.
 */
(()=>{
'use strict';
const $=id=>document.getElementById(id);
const arr=(v,field='series')=>window.ITMQTraceContract?.array?window.ITMQTraceContract.array(v,field,'InstitutionalChart'):(Array.isArray(v)?v:ArrayBuffer.isView(v)?Array.from(v):[]);
const finite=v=>Number.isFinite(Number(v));
const num=(v,d=0)=>finite(v)?Number(v):d;
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const fmt=(v,n=2)=>finite(v)?Number(v).toLocaleString(undefined,{maximumFractionDigits:n,minimumFractionDigits:n}):'—';
const compact=v=>{v=num(v,NaN);if(!Number.isFinite(v))return'—';const a=Math.abs(v),s=v<0?'-':'';if(a>=1e9)return`${s}${(a/1e9).toFixed(2)}B`;if(a>=1e6)return`${s}${(a/1e6).toFixed(2)}M`;if(a>=1e3)return`${s}${(a/1e3).toFixed(1)}K`;return`${s}${a.toFixed(1)}`;};

const BUS=new EventTarget();
window.ITMQBus=BUS;
const NATIVE_IDS=new Set(['exposureChart','netPositioningChart','volumeChart']);
const UNIFIED_IDS=new Set(['scannerChart','flowChart','flowProChart','traceFlowProChart','netDriftChart','chainChart','exposureChart','gexMatrixChart','netPositioningChart','volumeChart','volChart','skewChart','macroChart','printsChart','equityHubGammaModel','equityHubMonteCarlo','surfaceAltChart','surfaceSliceChart']);

function colorFor(v,idx=0){
 if(finite(v))return Number(v)>=0?'#3bbd84':'#e15b6b';
 return ['#5ab2df','#a78be8','#d9b85f','#66c5a4','#d17ca2'][idx%5];
}
function css(elm,k){return getComputedStyle(elm).getPropertyValue(k).trim();}

class Native2D{
 constructor(host,id){this.host=host;this.id=id;this.canvas=document.createElement('canvas');this.canvas.className='institutional-native-canvas';host.innerHTML='';host.appendChild(this.canvas);this.ctx=this.canvas.getContext('2d');this.tip=document.createElement('div');this.tip.className='native-chart-tip';host.appendChild(this.tip);this.spec=null;this.hover=null;this.dpr=1;this.ro=new ResizeObserver(()=>this.resize());this.ro.observe(host);this.canvas.addEventListener('pointermove',e=>this.onMove(e));this.canvas.addEventListener('pointerleave',()=>{this.hover=null;this.tip.classList.remove('show');this.draw();BUS.dispatchEvent(new CustomEvent('itmq:strike-hover',{detail:{strike:null,source:this.id}}));});this.resize();}
 resize(){const r=this.host.getBoundingClientRect();this.w=Math.max(260,Math.round(r.width));this.h=Math.max(240,Math.round(r.height));this.dpr=Math.min(window.devicePixelRatio||1,2);this.canvas.width=this.w*this.dpr;this.canvas.height=this.h*this.dpr;this.canvas.style.width=this.w+'px';this.canvas.style.height=this.h+'px';this.ctx.setTransform(this.dpr,0,0,this.dpr,0,0);this.draw();}
 setSpec(s){this.spec=s;this.draw();}
 traces(){return arr(this.spec?.data,'spec.data').filter(t=>t&&['bar','scatter','heatmap'].includes(String(t.type||'scatter').toLowerCase()));}
 bounds(){const ts=this.traces();let xs=[],ys=[],zeroX=false,zeroY=false;for(const t of ts){if(t.type==='heatmap'){const x=arr(t.x,'trace.x');const y=arr(t.y,'trace.y');xs.push(...x.filter(finite).map(Number));ys.push(...y.filter(finite).map(Number));}else{xs.push(...arr(t.x,'trace.x').filter(finite).map(Number));ys.push(...arr(t.y,'trace.y').filter(finite).map(Number));if(t.type==='bar'){if(String(t.orientation||'v')==='h')zeroX=true;else zeroY=true;}}}if(!xs.length||!ys.length)return null;if(zeroX)xs.push(0);if(zeroY)ys.push(0);let xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);if(xmin===xmax){xmin-=1;xmax+=1}if(ymin===ymax){ymin-=1;ymax+=1}return{xmin,xmax,ymin,ymax};}
 frame(){return{l:64,r:this.w-22,t:22,b:this.h-42};}
 mapx(x,b,f){return f.l+(x-b.xmin)/(b.xmax-b.xmin)*(f.r-f.l)}
 mapy(y,b,f){return f.b-(y-b.ymin)/(b.ymax-b.ymin)*(f.b-f.t)}
 draw(){const c=this.ctx;if(!c)return;c.fillStyle='#05090e';c.fillRect(0,0,this.w,this.h);const b=this.bounds(),f=this.frame();c.strokeStyle='#172638';c.lineWidth=1;for(let i=0;i<=5;i++){const x=f.l+i*(f.r-f.l)/5;c.beginPath();c.moveTo(x,f.t);c.lineTo(x,f.b);c.stroke();const y=f.t+i*(f.b-f.t)/5;c.beginPath();c.moveTo(f.l,y);c.lineTo(f.r,y);c.stroke();}if(!b){c.fillStyle='#7c8b9a';c.font='12px ui-monospace';c.fillText('Esperando datos cuantitativos…',f.l+10,f.t+30);return;}c.font='10px ui-monospace';c.fillStyle='#7e8c9b';for(let i=0;i<=4;i++){const xv=b.xmin+i*(b.xmax-b.xmin)/4;c.fillText(compact(xv),f.l+i*(f.r-f.l)/4-12,f.b+19);const yv=b.ymax-i*(b.ymax-b.ymin)/4;c.fillText(compact(yv),5,f.t+i*(f.b-f.t)/4+4);}const ts=this.traces();ts.forEach((t,ti)=>{const type=String(t.type||'scatter').toLowerCase();if(type==='heatmap')this.drawHeatmap(c,t,b,f);else if(type==='bar')this.drawBars(c,t,b,f,ti);else this.drawScatter(c,t,b,f,ti);});if(this.hover){c.strokeStyle='rgba(210,230,242,.72)';c.setLineDash([3,4]);c.beginPath();c.moveTo(this.hover.x,f.t);c.lineTo(this.hover.x,f.b);c.stroke();c.setLineDash([]);}}
 drawScatter(c,t,b,f,ti){const x=arr(t.x,'trace.x'),y=arr(t.y,'trace.y');if(!x.length||!y.length)return;c.strokeStyle=colorFor(null,ti);c.lineWidth=1.8;c.beginPath();let started=false;for(let i=0;i<Math.min(x.length,y.length);i++){if(!finite(x[i])||!finite(y[i]))continue;const px=this.mapx(Number(x[i]),b,f),py=this.mapy(Number(y[i]),b,f);if(!started){c.moveTo(px,py);started=true}else c.lineTo(px,py);}c.stroke();}
 drawBars(c,t,b,f,ti){const x=arr(t.x,'trace.x'),y=arr(t.y,'trace.y');const orient=String(t.orientation||'v');if(orient==='h'){const barH=Math.max(2,(f.b-f.t)/Math.max(1,y.length)*.66);for(let i=0;i<Math.min(x.length,y.length);i++){if(!finite(x[i])||!finite(y[i]))continue;const zero=this.mapx(0,b,f),px=this.mapx(Number(x[i]),b,f),py=this.mapy(Number(y[i]),b,f);c.fillStyle=colorFor(x[i],ti);c.fillRect(Math.min(zero,px),py-barH/2,Math.abs(px-zero),barH);}}else{const bw=Math.max(2,(f.r-f.l)/Math.max(1,x.length)*.62);for(let i=0;i<Math.min(x.length,y.length);i++){if(!finite(x[i])||!finite(y[i]))continue;const zero=this.mapy(0,b,f),px=this.mapx(Number(x[i]),b,f),py=this.mapy(Number(y[i]),b,f);c.fillStyle=colorFor(y[i],ti);c.fillRect(px-bw/2,Math.min(zero,py),bw,Math.abs(py-zero));}}}
 drawHeatmap(c,t,b,f){const z=arr(t.z,'trace.z'),xs=arr(t.x,'trace.x').map(Number),ys=arr(t.y,'trace.y').map(Number);if(!z.length||!xs.length||!ys.length)return;let vals=z.flat().map(Number).filter(Number.isFinite),ma=Math.max(1e-9,...vals.map(v=>Math.abs(v)));for(let iy=0;iy<ys.length;iy++)for(let ix=0;ix<xs.length;ix++){const v=num(z[iy]?.[ix],0),p=clamp(v/ma,-1,1),x0=this.mapx(xs[ix],b,f),x1=ix+1<xs.length?this.mapx(xs[ix+1],b,f):x0+Math.max(3,(f.r-f.l)/xs.length),y0=this.mapy(ys[iy],b,f),y1=iy+1<ys.length?this.mapy(ys[iy+1],b,f):y0-Math.max(3,(f.b-f.t)/ys.length);const a=.16+.78*Math.abs(p);c.fillStyle=p>=0?`rgba(55,176,126,${a})`:`rgba(220,80,101,${a})`;c.fillRect(Math.min(x0,x1),Math.min(y0,y1),Math.abs(x1-x0)+1,Math.abs(y1-y0)+1);}}
 onMove(e){const r=this.canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top,b=this.bounds(),f=this.frame();if(!b||x<f.l||x>f.r||y<f.t||y>f.b){this.tip.classList.remove('show');return;}const xv=b.xmin+(x-f.l)/(f.r-f.l)*(b.xmax-b.xmin),yv=b.ymax-(y-f.t)/(f.b-f.t)*(b.ymax-b.ymin);this.hover={x,y,xv,yv};let strike=null;const traces=this.traces();for(const t of traces){const vals=arr(t.orientation==='h'?t.y:t.x,'trace.axis');const nums=vals.filter(finite).map(Number);if(nums.length){const n=nums.reduce((a,v)=>Math.abs(v-xv)<Math.abs(a-xv)?v:a,nums[0]);if(!finite(strike)||Math.abs(n-xv)<Math.abs(strike-xv))strike=n;}}this.tip.innerHTML=`<b>${this.id.replace('Chart','')}</b><span>x ${fmt(xv,2)}</span><span>y ${compact(yv)}</span>${finite(strike)?`<em>strike ${fmt(strike,2)}</em>`:''}`;this.tip.style.left=Math.min(this.w-170,x+12)+'px';this.tip.style.top=Math.max(8,y-46)+'px';this.tip.classList.add('show');this.draw();if(finite(strike))BUS.dispatchEvent(new CustomEvent('itmq:strike-hover',{detail:{strike:Number(strike),source:this.id}}));}
}

function normalizeSpec(id,spec){
 if(!spec||!UNIFIED_IDS.has(id))return spec;
 spec.layout=spec.layout||{};const l=spec.layout;
 l.paper_bgcolor='#05090e';l.plot_bgcolor='#071018';l.font={...(l.font||{}),family:'Inter, ui-sans-serif, system-ui',color:'#b8c9d6',size:l.font?.size||11};
 l.margin={l:56,r:32,t:Math.max(44,Number(l.margin?.t||0)),b:Math.max(42,Number(l.margin?.b||0)),...(l.margin||{})};
 l.hoverlabel={...(l.hoverlabel||{}),bgcolor:'#08131d',bordercolor:'rgba(82,184,255,.42)',font:{color:'#eaf6ff',size:11}};
 l.legend={orientation:'h',y:1.06,x:0,...(l.legend||{}),font:{color:'#9db1c0',size:10,...(l.legend?.font||{})}};
 l.uirevision=l.uirevision||`itmq-v139-${id}`;
 const axisStyle={gridcolor:'rgba(122,158,185,.10)',zerolinecolor:'rgba(122,158,185,.18)',linecolor:'rgba(122,158,185,.18)',tickfont:{color:'#7890a2',size:10},titlefont:{color:'#9bb0c0',size:11},showspikes:true,spikecolor:'rgba(140,236,255,.38)',spikethickness:1};
 for(const k of Object.keys(l)){if(/^xaxis\d*$/.test(k)||/^yaxis\d*$/.test(k))l[k]={...axisStyle,...(l[k]||{})};}
 if(!l.xaxis)l.xaxis={...axisStyle};if(!l.yaxis)l.yaxis={...axisStyle};
 const host=$(id);if(host){host.classList.add('itmq-unified-chart');host.dataset.chartFamily='INSTITUTIONAL_V139';}
 return spec;
}
const charts=new Map();
function supports(id,spec){const data=arr(spec?.data,'spec.data');if(!NATIVE_IDS.has(id)||!data.length)return false;return data.every(t=>{const type=String(t.type||'scatter').toLowerCase();if(!['bar','scatter','heatmap'].includes(type))return false;const xs=arr(t.x,'trace.x').filter(finite),ys=arr(t.y,'trace.y').filter(finite);return type==='heatmap'?xs.length>0&&ys.length>0:xs.length>0&&ys.length>0;});}
function render(id,spec){if(!supports(id,spec))return false;const host=$(id);if(!host)return false;try{let c=charts.get(id);if(!c){host.classList.add('native-chart-host');c=new Native2D(host,id);charts.set(id,c);}c.setSpec(spec);return true;}catch(e){console.warn('[ITM QUANT CHART SAFE MODE]',id,e);window.ITMQTraceContract?.array?.({error:String(e?.message||e)},`${id}.renderer`,'InstitutionalChart');host.innerHTML=`<div class="native-chart-contract-fallback"><b>DATA CONTRACT WARN</b><span>${String(e?.message||e)}</span></div>`;return true;}}

// Renombrada desde `api` en v1.42.4. Se declaraba a nivel global con el mismo
// nombre que app.js pero con UNA sola firma (sin `opts`), así que la que quedaba
// activa dependía del orden de los <script>. Con el orden invertido, todos los
// POST (refresh, premarket, replay, board) habrían perdido sus opciones y se
// habrían convertido en GET silenciosos, sin un solo error en consola.
async function itGetJson(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(`${r.status}`);return await r.json();}
function capClass(s){s=String(s||'').toUpperCase();return s==='ACTIVE'?'ok':s==='READY'||s==='BROWSER CHECK'?'ready':s==='UNAVAILABLE'||s==='DISABLED'?'off':'warn';}
function setText(id,v){const x=$(id);if(x)x.textContent=v??'—';}
function renderReadiness(p){const caps=p?.capabilities||{};const put=(id,key)=>{const c=caps[key]||{};const x=$(id);if(x){x.textContent=c.status||'—';x.className=`tech-state ${capClass(c.status)}`;x.title=c.detail||'';}};put('techHft','hft_telemetry');put('techFpga','fpga_feed_bridge');put('techColo','co_location');put('techQpu','quantum_optimization');put('techAi','generative_scenarios');put('techFutures','direct_futures');const l=p?.latency||{};setText('techLatency',l.p95_ms==null?'NO DATA':`${fmt(l.p95_ms,1)}ms p95`);const grid=$('deepTechGrid');if(grid){grid.innerHTML=Object.values(caps).map(c=>`<div class="deep-tech-row"><span>${String(c.key||'').replaceAll('_',' ')}</span><b class="tech-state ${capClass(c.status)}">${c.status||'—'}</b><small>${c.detail||''}</small></div>`).join('');}}
function drawScenario(p){const canvas=$('scenarioCanvas');if(!canvas||!p?.ready)return;const c=canvas.getContext('2d'),host=canvas.parentElement,r=host.getBoundingClientRect(),d=Math.min(devicePixelRatio||1,2),w=Math.max(300,Math.round(r.width)),h=190;canvas.width=w*d;canvas.height=h*d;canvas.style.width=w+'px';canvas.style.height=h+'px';c.setTransform(d,0,0,d,0,0);c.fillStyle='#05090e';c.fillRect(0,0,w,h);const paths=p.representative_paths||[];const vals=paths.flat().filter(finite).map(Number);if(!vals.length)return;const lo=Math.min(...vals),hi=Math.max(...vals),span=Math.max(1e-9,hi-lo);paths.forEach((row,i)=>{c.strokeStyle=i<3?'rgba(106,190,225,.35)':'rgba(120,140,158,.12)';c.lineWidth=i<3?1.2:.7;c.beginPath();row.forEach((v,j)=>{const x=8+j/(row.length-1)*(w-16),y=h-10-(Number(v)-lo)/span*(h-20);j?c.lineTo(x,y):c.moveTo(x,y)});c.stroke();});const q=p.quantiles||{};setText('scenarioRange',`${fmt(q.p16)} — ${fmt(q.p84)}`);setText('scenarioTail',`${fmt(q.p05)} / ${fmt(q.p95)}`);setText('scenarioEngine',p.engine||'SHADOW');}
function renderQuantum(p){setText('quantumBackend',p?.backend||'UNAVAILABLE');setText('quantumSelected',(p?.selected||[]).join(' · ')||'COLLECTING');const x=$('quantumState');if(x){x.textContent=p?.qpu_active?'QPU ACTIVE':'QUANTUM READY · CLASSICAL FALLBACK';x.className=`tech-state ${p?.qpu_active?'ok':'ready'}`;}}
async function refreshAdvanced(){try{const [r,s,q]=await Promise.all([itGetJson('/api/nextgen/readiness'),itGetJson('/api/nextgen/scenario'),itGetJson('/api/nextgen/quantum')]);renderReadiness(r);drawScenario(s);renderQuantum(q);}catch(e){console.warn('ITMQ advanced readiness',e);}}

async function initXR(){const b=$('surfaceImmersiveBtn'),state=$('techXr');if(!b||!state)return;if(!navigator.xr){state.textContent='UNAVAILABLE';state.className='tech-state off';b.disabled=true;return;}state.textContent='BROWSER CHECK';state.className='tech-state ready';b.disabled=false;b.title='WebXR se comprobará solo al solicitar modo inmersivo';b.addEventListener('click',async()=>{try{const vr=await navigator.xr.isSessionSupported('immersive-vr'),ar=await navigator.xr.isSessionSupported('immersive-ar');if(!vr&&!ar){state.textContent='UNAVAILABLE';state.className='tech-state off';return;}const mode=vr?'immersive-vr':'immersive-ar',session=await navigator.xr.requestSession(mode,{optionalFeatures:['local-floor']});state.textContent='ACTIVE';state.className='tech-state ok';session.addEventListener('end',()=>{state.textContent='READY';state.className='tech-state ready';});window.ITMQNextGen?.enterXR?.(session);}catch(err){state.textContent='UNAVAILABLE';state.className='tech-state off';console.info('[ITM XR] runtime unavailable');}});}

BUS.addEventListener('itmq:strike-hover',e=>{const strike=e.detail?.strike;if(window.ITMQNextGen?.highlightStrike)window.ITMQNextGen.highlightStrike(strike);});

document.addEventListener('DOMContentLoaded',()=>{refreshAdvanced();setInterval(refreshAdvanced,15000);initXR();});
window.ITMQInstitutionalCharts={supports,render,normalizeSpec,refreshAdvanced,state:{charts}};
})();

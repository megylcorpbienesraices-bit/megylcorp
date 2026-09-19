/* ITM QUANT v1.40.0 · UNIFIED TOOL RUNTIME
 * Snapshot -> incremental event -> worker -> digest. Presentation/runtime only.
 */
(()=>{'use strict';
const state={registry:null,active:null,ws:null,seq:0,lastEvent:null,digests:new Map(),pending:new Map(),jobSeq:0,worker:null,coalesceTimer:0,eventQueue:[]};
function section(){return document.querySelector('.page-section.active')?.id||'';}
const sectionMap={
 'section-command':'command','section-scanner':'scanner','section-trace':'trace','section-flow':'flow','section-netdrift':'netdrift','section-chain':'chain','section-exposure':'exposure','section-gexmatrix':'gexmatrix','section-positioning':'positioning','section-vol':'volatility','section-surface':'surface','section-macro':'macro','section-prints':'prints','section-equityhub':'equity_hub'
};
function worker(){if(state.worker)return state.worker;try{state.worker=new Worker('/static/tool_worker.js');state.worker.onmessage=e=>{const m=e.data||{},p=state.pending.get(m.id);if(!p)return;state.pending.delete(m.id);m.ok?p.resolve(m.result):p.reject(new Error(m.error||'WORKER_ERROR'));};return state.worker;}catch(e){console.warn('[ITM TOOL WORKER]',e);return null;}}
function run(job,payload){const w=worker();if(!w)return Promise.reject(new Error('WORKER_UNAVAILABLE'));const id=++state.jobSeq;return new Promise((resolve,reject)=>{state.pending.set(id,{resolve,reject});w.postMessage({id,job,payload});});}
async function loadRegistry(){try{const r=await fetch('/api/tools/registry',{cache:'no-store'});if(!r.ok)throw new Error(`HTTP ${r.status}`);state.registry=await r.json();window.dispatchEvent(new CustomEvent('itmq:tool-registry',{detail:state.registry}));return state.registry;}catch(e){console.warn('[ITM TOOL REGISTRY]',e);return null;}}
function wsUrl(tool){const proto=location.protocol==='https:'?'wss:':'ws:';return `${proto}//${location.host}/ws/tools/${encodeURIComponent(tool)}`;}
function scheduleEvent(ev){state.eventQueue.push(ev);if(state.coalesceTimer)return;state.coalesceTimer=setTimeout(()=>{state.coalesceTimer=0;const q=state.eventQueue.splice(0,state.eventQueue.length);if(!q.length)return;const latest=q[q.length-1];state.lastEvent=latest;state.seq=Math.max(state.seq,Number(latest.seq)||0);if(latest.digest)state.digests.set(latest.tool_id,latest.digest);window.dispatchEvent(new CustomEvent('itmq:tool-update',{detail:{...latest,coalesced_count:q.length}}));},200);}
function disconnect(){try{state.ws?.close?.();}catch(_){}state.ws=null;}
async function activate(tool){tool=String(tool||'');if(!tool||tool===state.active&&state.ws?.readyState===1)return;disconnect();state.active=tool;try{const r=await fetch(`/api/tools/${encodeURIComponent(tool)}/digest`,{cache:'no-store'});if(r.ok){const snap=await r.json();if(state.active===tool&&snap?.digest)scheduleEvent({type:'TOOL_SNAPSHOT',tool_id:tool,seq:0,signature:snap.signature,symbol_epoch:snap.symbol_epoch,digest:snap.digest});}}catch(_){}if(state.active!==tool)return;try{const ws=new WebSocket(wsUrl(tool));state.ws=ws;ws.onmessage=e=>{try{scheduleEvent(JSON.parse(e.data));}catch(_){}};ws.onerror=()=>{};ws.onclose=()=>{if(state.active===tool)setTimeout(()=>activate(tool),1500);};}catch(e){console.debug('[ITM TOOL WS]',e?.message||e);}}
function sync(){const tool=sectionMap[section()]||null;if(tool&&tool!==state.active)activate(tool);}
const mo=new MutationObserver(sync);document.addEventListener('DOMContentLoaded',()=>{loadRegistry();sync();document.querySelectorAll('.page-section').forEach(n=>mo.observe(n,{attributes:true,attributeFilter:['class']}));});
window.ITMQToolRuntime={state,loadRegistry,activate,disconnect,run,digest:(id)=>state.digests.get(id)||null,sectionMap};
})();

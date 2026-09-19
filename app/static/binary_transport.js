/* ITM QUANT v1.40.2 · BINARY BROWSER TRANSPORT
 * Existing single binary WebSocket transport. No duplicate market socket is created.
 * Hot path: binary -> TypedArray/DataView -> PRESENTATION event bus -> Canvas/WebGPU.
 * Quantitative authority remains in Rust/Python Quant Core; this file only transports display frames.
 */
(()=>{
'use strict';
const state={ticks:null,surface:null,tickState:'OFF',surfaceState:'OFF',lastTickSeq:0,wasm:'READY',surfaceField:'Gamma',wasmBridge:null,wasmMemory:null,surfaceWanted:false,surfaceGeneration:0};
const wsUrl=path=>`${location.protocol==='https:'?'wss':'ws'}://${location.host}${path}`;
const LITTLE_ENDIAN=(()=>{const b=new ArrayBuffer(4);new DataView(b).setUint32(0,0x01020304,true);return new Uint8Array(b)[0]===4;})();
function nsToIso(ns){const ms=Number(BigInt(ns)/1000000n);return new Date(ms).toISOString();}
function decodeTicks(buffer){
 const v=new DataView(buffer);if(buffer.byteLength<19)return null;
 const magic=String.fromCharCode(v.getUint8(0),v.getUint8(1),v.getUint8(2),v.getUint8(3));if(magic!=='ITMT'||v.getUint8(4)!==1)return null;
 const slen=v.getUint16(5,true),count=v.getUint32(7,true),lastSeq=Number(v.getBigUint64(11,true));let off=19;
 const symbol=new TextDecoder().decode(new Uint8Array(buffer,off,slen));off+=slen;const rows=[],rec=32;
 for(let i=0;i<count;i++){if(off+rec>buffer.byteLength)break;const ns=v.getBigUint64(off,true),price=v.getFloat64(off+8,true),size=v.getFloat32(off+16,true),signed=v.getFloat32(off+20,true),seq=Number(v.getBigUint64(off+24,true));off+=rec;rows.push({timestamp:nsToIso(ns),price,size,signed_volume:signed,seq});}
 return{symbol,last_seq:lastSeq,ticks:rows};
}
function decodeSurfaceDataView(buffer,off,count){const dv=new DataView(buffer);const values=new Float32Array(count);for(let i=0;i<count;i++)values[i]=dv.getFloat32(off+i*4,true);return values;}
function decodeSurfaceValues(buffer,off,count){
 // Minimal-copy path: when alignment + host endianness permit, expose a view over the WebSocket ArrayBuffer.
 if(LITTLE_ENDIAN&&(off&3)===0)return{values:new Float32Array(buffer,off,count),decoder:'TYPEDARRAY_VIEW'};
 return{values:decodeSurfaceDataView(buffer,off,count),decoder:'DATAVIEW_COPY'};
}
function decodeSurface(buffer){
 const v=new DataView(buffer);if(buffer.byteLength<26)return null;
 const magic=String.fromCharCode(v.getUint8(0),v.getUint8(1),v.getUint8(2),v.getUint8(3));if(magic!=='ITMS'||v.getUint8(4)!==1)return null;
 const nlen=v.getUint8(5),rows=v.getUint16(6,true),cols=v.getUint16(8,true),seq=Number(v.getBigUint64(10,true)),min=v.getFloat32(18,true),max=v.getFloat32(22,true);let off=26;
 const field=new TextDecoder().decode(new Uint8Array(buffer,off,nlen));off+=nlen;const count=rows*cols;if(off+count*4>buffer.byteLength)return null;
 let values,decoder='DATAVIEW_COPY';
 if(state.wasm==='ACTIVE'&&state.wasmBridge&&state.wasmMemory){try{const n=state.wasmBridge.process_surface_frame(new Uint8Array(buffer));const ptr=state.wasmBridge.pointer();values=new Float32Array(new Float32Array(state.wasmMemory.buffer,ptr,n));decoder='WASM_COPY_SAFE';}catch(_){const r=decodeSurfaceValues(buffer,off,count);values=r.values;decoder=r.decoder;}}
 else{const r=decodeSurfaceValues(buffer,off,count);values=r.values;decoder=r.decoder;}
 return{field,rows,cols,sequence:seq,min,max,values,decoder};
}
async function initWasm(){try{const status=await fetch('/api/nextgen/low-latency',{cache:'no-store'}).then(r=>r.ok?r.json():null).catch(()=>null);if(!status?.wasm_built){state.wasm='FALLBACK';return;}const mod=await import('/static/wasm/itmq_wasm_bridge.js');if(typeof mod.default==='function')await mod.default();if(mod.QuantBridge&&typeof mod.wasm_memory==='function'){state.wasmBridge=new mod.QuantBridge(262144);state.wasmMemory=mod.wasm_memory();state.wasm='ACTIVE';return;}state.wasm='FALLBACK';}catch(_){state.wasm='FALLBACK';}}
function emitTicks(p){if(!p)return;const rx=performance.now(),last=p.ticks?.[p.ticks.length-1],evt=last?.timestamp?new Date(last.timestamp).getTime():NaN;p.browser_received_perf=rx;p.event_to_browser_ms=Number.isFinite(evt)?Math.max(0,Date.now()-evt):null;const rt=window.ITMQPerformanceCore?.runtime;if(rt?.ingestTicks)rt.ingestTicks(p);else window.dispatchEvent(new CustomEvent('itmq:binary-ticks',{detail:p}));}
function emitSurface(p){const rt=window.ITMQPerformanceCore?.runtime;if(rt?.ingestSurface)rt.ingestSurface(p);else window.dispatchEvent(new CustomEvent('itmq:binary-surface',{detail:p}));}
function connectTicks(){
 if(state.ticks&&state.ticks.readyState<=1)return;
 try{const ws=new WebSocket(wsUrl('/ws/nextgen/ticks-bin'));state.ticks=ws;ws.binaryType='arraybuffer';ws.onopen=()=>{state.tickState='ACTIVE'};ws.onclose=()=>{state.tickState='RECONNECTING';setTimeout(connectTicks,1200)};ws.onerror=()=>{state.tickState='DEGRADED'};ws.onmessage=e=>{if(!(e.data instanceof ArrayBuffer))return;const p=decodeTicks(e.data);if(!p)return;state.lastTickSeq=p.last_seq;emitTicks(p);};}catch(_){state.tickState='UNAVAILABLE';}
}
function connectSurface(field='Gamma'){
 state.surfaceField=field||state.surfaceField||'Gamma';state.surfaceWanted=true;
 const gen=++state.surfaceGeneration,current=state.surface;
 if(current&&current.readyState===WebSocket.OPEN){state.surfaceState='ACTIVE';try{current.send(state.surfaceField)}catch(_){}return;}
 if(current&&current.readyState===WebSocket.CONNECTING){state.surfaceGeneration--;return;}
 try{
  const ws=new WebSocket(wsUrl(`/ws/nextgen/surface-bin?field=${encodeURIComponent(state.surfaceField)}`));state.surface=ws;ws.binaryType='arraybuffer';state.surfaceState='CONNECTING';
  ws.onopen=()=>{if(gen!==state.surfaceGeneration||!state.surfaceWanted){try{ws.close(1000,'surface inactive')}catch(_){}return;}state.surfaceState='ACTIVE';try{ws.send(state.surfaceField)}catch(_){}};
  ws.onclose=()=>{if(state.surface===ws)state.surface=null;if(gen!==state.surfaceGeneration||!state.surfaceWanted){state.surfaceState='OFF';return;}state.surfaceState='RECONNECTING';setTimeout(()=>{if(state.surfaceWanted&&gen===state.surfaceGeneration)connectSurface(state.surfaceField)},1500)};
  ws.onerror=()=>{if(gen===state.surfaceGeneration)state.surfaceState='DEGRADED'};
  ws.onmessage=e=>{if(gen!==state.surfaceGeneration||!state.surfaceWanted||!(e.data instanceof ArrayBuffer))return;const p=decodeSurface(e.data);if(!p)return;emitSurface(p);};
 }catch(_){state.surfaceState='UNAVAILABLE';}
}
function disconnectSurface(){state.surfaceWanted=false;const gen=++state.surfaceGeneration,ws=state.surface;if(!ws){state.surfaceState='OFF';return;}if(ws.readyState===WebSocket.OPEN){try{ws.close(1000,'surface inactive')}catch(_){ }return;}if(ws.readyState===WebSocket.CONNECTING){const prev=ws.onopen;ws.onopen=e=>{try{prev?.(e)}catch(_){ }if(gen===state.surfaceGeneration&&!state.surfaceWanted){try{ws.close(1000,'surface inactive')}catch(_){ }}};return;}state.surface=null;state.surfaceState='OFF';}
function setSurfaceField(field){state.surfaceField=field||'Gamma';state.surfaceWanted=true;if(state.surface?.readyState===WebSocket.OPEN){try{state.surface.send(state.surfaceField)}catch(_){}}else connectSurface(state.surfaceField);}
document.addEventListener('DOMContentLoaded',async()=>{await initWasm();connectTicks();});
window.addEventListener('itmq:section-change',e=>{const sec=String(e?.detail?.section||'');if(sec==='surface')connectSurface(state.surfaceField||'Gamma');else disconnectSurface();});
window.ITMQBinaryTransport={state,initWasm,connectTicks,connectSurface,disconnectSurface,setSurfaceField,decodeTicks,decodeSurface,decodeSurfaceValues,single_transport:true,authority:'TRANSPORT_ONLY'};
})();

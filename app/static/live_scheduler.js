/* ITM QUANT v1.26.2 · ADAPTIVE LIVE CLIENT SCHEDULER
 * Event-driven data stays event-driven. This module only coalesces browser work and
 * adapts polling cadence so quiet markets do not burn CPU and active markets stay responsive.
 */
(()=>{
'use strict';
const loops=new Map(),frames=new Map();
const s={activeUntil:0,normalUntil:0,lastTickAt:0,lastFlowAt:0,tickTimes:[],tickRate:0,lastMode:'QUIET'};
const now=()=>performance.now();
function prune(t){const cutoff=t-2000;s.tickTimes=s.tickTimes.filter(x=>x>=cutoff);s.tickRate=s.tickTimes.length/2;}
function mode(){const t=now();if(document.hidden)return'HIDDEN';if(t<s.activeUntil)return'ACTIVE';if(t<s.normalUntil)return'NORMAL';return'QUIET';}
function noteTicks(ticks){const n=Array.isArray(ticks)?ticks.length:Number(ticks)||0;if(n<=0)return;const t=now();s.lastTickAt=t;const cap=Math.min(n,240);for(let i=0;i<cap;i++)s.tickTimes.push(t);prune(t);if(n>=12||s.tickRate>=8){s.activeUntil=Math.max(s.activeUntil,t+6000);s.normalUntil=Math.max(s.normalUntil,t+16000);}else{s.normalUntil=Math.max(s.normalUntil,t+12000);}emit();}
function noteFlow(delta=1){delta=Math.abs(Number(delta)||0);if(delta<=0)return;const t=now();s.lastFlowAt=t;s.activeUntil=Math.max(s.activeUntil,t+8000);s.normalUntil=Math.max(s.normalUntil,t+20000);emit();}
function cadence(kind='trace'){const m=mode(),table={
 pulse:{ACTIVE:650,NORMAL:1000,QUIET:1800,HIDDEN:5000},
 trace:{ACTIVE:1200,NORMAL:2200,QUIET:4500,HIDDEN:8000},
 surface:{ACTIVE:2500,NORMAL:4000,QUIET:8000,HIDDEN:12000},
 mini:{ACTIVE:180,NORMAL:300,QUIET:650,HIDDEN:1800},
 charts:{ACTIVE:12000,NORMAL:18000,QUIET:30000,HIDDEN:60000}
 };return(table[kind]||table.trace)[m]||2200;}
function snapshot(){prune(now());return{mode:mode(),tick_rate_eps:Number(s.tickRate.toFixed(1)),pulse_ms:cadence('pulse'),trace_ms:cadence('trace'),surface_ms:cadence('surface'),mini_ms:cadence('mini'),policy:'ADAPTIVE + COALESCED'}}
function emit(){const m=mode();if(m!==s.lastMode){s.lastMode=m;window.dispatchEvent(new CustomEvent('itmq:live-cadence',{detail:snapshot()}));}}
function loop(key,fn,kind='trace',shouldRun=null){stop(key);const rec={timer:0,busy:false,stopped:false};loops.set(key,rec);const run=async()=>{if(rec.stopped)return;try{if((!shouldRun||shouldRun())&&!rec.busy){rec.busy=true;await fn();}}catch(e){console.warn(`[ITM QUANT LIVE SCHEDULER] ${key}`,e);}finally{rec.busy=false;if(!rec.stopped)rec.timer=setTimeout(run,cadence(kind));}};rec.timer=setTimeout(run,cadence(kind));return()=>stop(key);}
function stop(key){const rec=loops.get(key);if(rec){rec.stopped=true;if(rec.timer)clearTimeout(rec.timer);loops.delete(key);}}
function frame(key,fn,priority=2){const rt=window.ITMQPerformanceCore?.runtime;if(rt?.coalesce){rt.coalesce(`live:${key}`,fn,priority);return key;}const old=frames.get(key);if(old)return old;const id=requestAnimationFrame(()=>{frames.delete(key);try{fn();}catch(e){console.warn(`[ITM QUANT COALESCED FRAME] ${key}`,e);}});frames.set(key,id);return id;}
document.addEventListener('visibilitychange',emit);
setInterval(()=>{prune(now());emit();},1000);
window.ITMQLiveScheduler={noteTicks,noteFlow,cadence,snapshot,mode,loop,stop,frame};
})();

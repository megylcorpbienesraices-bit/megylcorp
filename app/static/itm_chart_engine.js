/* ITM QUANT v1.26.2 — shared chart engine for CHART TW / TRACE data.
   Presentation-only: Scanner/Quant math remains server-side.
   v1.26.2 visual policy: dark from frame 0, render-only interpolation,
   bounded observed-event ripples, visual-only LOD clustering and canvas crosshair. */
(function(){
'use strict';
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const num=(v,d=0)=>Number.isFinite(Number(v))?Number(v):d;
const lerp=(a,b,t)=>a+(b-a)*t;
const reducedMotion=()=>{try{return window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches}catch(_){return false}};
const _moneyCompact=v=>{const n=Number(v);if(!Number.isFinite(n))return'—';const a=Math.abs(n),s=n<0?'-':n>0?'+':'';if(a>=1e9)return`${s}${(a/1e9).toFixed(2)}B`;if(a>=1e6)return`${s}${(a/1e6).toFixed(2)}M`;if(a>=1e3)return`${s}${(a/1e3).toFixed(1)}K`;return`${s}${a.toFixed(a<100?2:0)}`};

class ITMChartDataCache{
  constructor(){this.bySymbol=new Map();}
  put(payload){if(!payload||!payload.symbol)return null;const sym=String(payload.symbol).toUpperCase();const prev=this.bySymbol.get(sym)||{};const hm=this.mergeHeatmap(prev.heatmap_history,payload.heatmap_history);const row={...prev,...payload,heatmap_history:hm,_cached_at:performance.now()};this.bySymbol.set(sym,row);return row;}
  get(symbol){return this.bySymbol.get(String(symbol||'').toUpperCase())||null;}
  mergeHeatmap(prev,next){
    if(!next||!Array.isArray(next.times))return next||prev||null;
    if(!prev||!Array.isArray(prev.times))return next;
    const strikes=(next.strikes||[]).map(Number),prevStrikes=(prev.strikes||[]).map(Number);
    if(strikes.join('|')!==prevStrikes.join('|'))return next;
    const fields=['gamma_m','delta_m','gamma_intensity','delta_intensity','joint_coherence'];
    const allTimes=[...(prev.times||[])];const pos=new Map(allTimes.map((t,i)=>[String(t),i]));
    const mats={};for(const f of fields){mats[f]=strikes.map((_,si)=>Array.isArray(prev[f]?.[si])?prev[f][si].slice():[]);}
    for(let tj=0;tj<(next.times||[]).length;tj++){
      const key=String(next.times[tj]);let col=pos.get(key);
      if(col==null){col=allTimes.length;pos.set(key,col);allTimes.push(next.times[tj]);for(const f of fields)for(let si=0;si<strikes.length;si++)mats[f][si].push(num(next[f]?.[si]?.[tj],0));}
      else{for(const f of fields)for(let si=0;si<strikes.length;si++)mats[f][si][col]=num(next[f]?.[si]?.[tj],mats[f][si][col]??0);}
    }
    const max=180;if(allTimes.length>max){const cut=allTimes.length-max;allTimes.splice(0,cut);for(const f of fields)for(let si=0;si<strikes.length;si++)mats[f][si].splice(0,cut);}
    return {...next,times:allTimes,...mats};
  }
}

class ITMChartEngine{
  constructor(canvas,opts={}){
    this.canvas=typeof canvas==='string'?document.getElementById(canvas):canvas;if(!this.canvas)throw new Error('canvas missing');
    this.ctx=this.canvas.getContext('2d',{alpha:false,desynchronized:true})||this.canvas.getContext('2d',{alpha:false});
    this.payload=null;this.heatmapMode='joint';this.heatmapOpacity=.42;this.profileMetric='Gamma+Delta';this.zoom=1;this.pan=0;this.drag=false;this.dragX=0;
    this.cross=null;this.crossTarget=null;this.crossRender=null;this.onCrosshair=opts.onCrosshair||null;this.dpr=1;
    this.ripples=[];this.seenPrints=new Set();this.raf=0;this.lastFrame=0;this.visualSpot=null;this.targetSpot=null;this.lodActive=false;this.motionReduced=reducedMotion();
    this._bind();this.resize();
  }
  _bind(){
    const c=this.canvas;
    c.addEventListener('wheel',e=>{e.preventDefault();this.zoom=clamp(this.zoom*(e.deltaY>0?.9:1.1),1,12);this.draw();},{passive:false});
    c.addEventListener('pointerdown',e=>{if(e.button!==0)return;this.drag=true;this.dragX=e.clientX;c.setPointerCapture?.(e.pointerId)});
    c.addEventListener('pointerup',()=>this.drag=false);
    c.addEventListener('pointercancel',()=>this.drag=false);
    c.addEventListener('pointerleave',()=>{this.drag=false;this.cross=null;this.crossTarget=null;this.crossRender=null;this.draw();});
    c.addEventListener('pointermove',e=>{const r=c.getBoundingClientRect();if(this.drag){this.pan+=e.clientX-this.dragX;this.dragX=e.clientX;}this.crossTarget={x:e.clientX-r.left,y:e.clientY-r.top};if(this.motionReduced){this.crossRender={...this.crossTarget};this.cross={...this.crossTarget};this.draw();this._emitCrosshair();}else this._scheduleAnimation();});
    c.addEventListener('dblclick',()=>{this.zoom=1;this.pan=0;this.draw();});
    window.addEventListener('resize',()=>this.resize(),{passive:true});
  }
  resize(){const r=this.canvas.getBoundingClientRect();this.dpr=Math.min(2,window.devicePixelRatio||1);const w=Math.max(320,Math.floor(r.width*this.dpr)),h=Math.max(240,Math.floor(r.height*this.dpr));if(this.canvas.width!==w||this.canvas.height!==h){this.canvas.width=w;this.canvas.height=h;}this.draw();}
  setPayload(p){this.payload=p;const s=num(p?.profiles?.spot??p?.spot,NaN);if(Number.isFinite(s)){this.targetSpot=s;if(this.visualSpot==null||!Number.isFinite(this.visualSpot))this.visualSpot=s;}this._ingestObservedRipples(p);if(this.motionReduced)this.draw();else this._scheduleAnimation();}
  setHeatmapMode(m){this.heatmapMode=String(m||'joint').toLowerCase();this.draw();}
  setHeatmapOpacity(v){this.heatmapOpacity=clamp(num(v,.42),0,1);this.draw();}
  setProfileMetric(v){this.profileMetric=String(v||'Gamma+Delta');this.draw();}
  focusTime(ts){
    const all=this._candles();if(!all.length)return false;const target=new Date(ts).getTime();if(!Number.isFinite(target))return false;
    let idx=0,best=Infinity;for(let i=0;i<all.length;i++){const t=new Date(all[i].t??all[i].time??all[i].timestamp).getTime(),d=Math.abs(t-target);if(Number.isFinite(d)&&d<best){best=d;idx=i;}}
    const n=Math.max(12,Math.floor(all.length/this.zoom)),desiredEnd=clamp(Math.round(idx+n/2),n,all.length);this.pan=(all.length-desiredEnd)*8;
    const vis=this._visibleCandles(),local=Math.max(0,vis.findIndex(x=>x===all[idx])),r=this.canvas.getBoundingClientRect(),usable=Math.max(1,r.width-156);this.crossTarget={x:78+((local+.5)/Math.max(1,vis.length))*usable,y:r.height*.45};this.crossRender={...this.crossTarget};this.cross={...this.crossTarget};this.draw();this._emitCrosshair();return true;
  }
  _candles(){return Array.isArray(this.payload?.candles)?this.payload.candles:[];}
  _visibleCandles(){const all=this._candles();if(!all.length)return [];const n=Math.max(12,Math.floor(all.length/this.zoom));let end=all.length-Math.round(this.pan/8);end=clamp(end,n,all.length);return all.slice(Math.max(0,end-n),end);}
  _priceRange(cs){let lo=Infinity,hi=-Infinity;for(const c of cs){lo=Math.min(lo,num(c.l??c.low,Infinity));hi=Math.max(hi,num(c.h??c.high,-Infinity));}const spot=num(this.payload?.profiles?.spot??this.payload?.spot,0);if(!Number.isFinite(lo)||!Number.isFinite(hi)||lo===hi){lo=spot-2;hi=spot+2;}const pad=(hi-lo)*.08||1;return[lo-pad,hi+pad];}
  _xy(c,i,n,lo,hi,W,H,margin){const x=margin.l+(i+.5)*(W-margin.l-margin.r)/Math.max(1,n),map=p=>margin.t+(hi-p)/(hi-lo)*(H-margin.t-margin.b);return{x,o:map(num(c.o??c.open)),h:map(num(c.h??c.high)),l:map(num(c.l??c.low)),cl:map(num(c.c??c.close))};}
  _scheduleAnimation(){if(this.raf)return;this.raf=requestAnimationFrame(ts=>this._animate(ts));}
  _animate(ts){this.raf=0;const dt=Math.min(50,Math.max(8,ts-(this.lastFrame||ts-16)));this.lastFrame=ts;let keep=false;
    if(this.crossTarget){if(!this.crossRender)this.crossRender={...this.crossTarget};const f=1-Math.exp(-dt/42);this.crossRender.x=lerp(this.crossRender.x,this.crossTarget.x,f);this.crossRender.y=lerp(this.crossRender.y,this.crossTarget.y,f);this.cross={...this.crossRender};if(Math.abs(this.crossRender.x-this.crossTarget.x)>.15||Math.abs(this.crossRender.y-this.crossTarget.y)>.15)keep=true;this._emitCrosshair();}
    if(Number.isFinite(this.targetSpot)){if(!Number.isFinite(this.visualSpot))this.visualSpot=this.targetSpot;const f=1-Math.exp(-dt/95);this.visualSpot=lerp(this.visualSpot,this.targetSpot,f);if(Math.abs(this.visualSpot-this.targetSpot)>1e-5)keep=true;}
    const now=performance.now();this.ripples=this.ripples.filter(r=>now-r.born<r.duration);if(this.ripples.length)keep=true;
    if(!this._candles().length)keep=true;
    this.draw(ts);if(keep&&!this.motionReduced)this._scheduleAnimation();
  }
  _ingestObservedRipples(p){const rows=Array.isArray(p?.option_prints)?p.option_prints:[];if(!rows.length||this.motionReduced)return;const prem=rows.map(x=>Math.abs(num(x.premium??x.notional,0))).filter(x=>x>0).sort((a,b)=>a-b);if(!prem.length)return;const q=prem[Math.min(prem.length-1,Math.floor((prem.length-1)*.88))]||Infinity;let added=0;
    for(const x of rows.slice(-40)){const value=Math.abs(num(x.premium??x.notional,0));if(value<q||value<=0)continue;const t=String(x.timestamp??x.time??x.t??''),price=num(x.underlying_price??x.price??x.spot,NaN);if(!t||!Number.isFinite(price))continue;const id=String(x.id??x.trade_id??x.seq??`${t}|${x.symbol??x.contract??''}|${price}|${value}`);if(this.seenPrints.has(id))continue;this.seenPrints.add(id);if(this.seenPrints.size>1200){const first=this.seenPrints.values().next().value;if(first!=null)this.seenPrints.delete(first);}const dir=num(x.direction,0),side=String(x.option_type??x.side??x.right??'').toUpperCase();this.ripples.push({id,t,price,value,positive:dir>0||(dir===0&&side.includes('C')),born:performance.now(),duration:900+Math.min(550,Math.log10(Math.max(10,value))*80)});added++;if(added>=6)break;}
    if(this.ripples.length>24)this.ripples.splice(0,this.ripples.length-24);if(added)this._scheduleAnimation();
  }
  _drawLoading(W,H){const c=this.ctx,phase=(performance.now()%1400)/1400;c.fillStyle='#0b0e14';c.fillRect(0,0,W,H);c.save();const cols=8,usable=W-96*this.dpr;for(let i=0;i<cols;i++){const x=48*this.dpr+i*usable/(cols-1),wave=.25+.55*Math.max(0,1-Math.abs(((phase*cols)-i)%cols)/2);c.fillStyle=`rgba(34,211,238,${.04+wave*.10})`;c.fillRect(x-1*this.dpr,70*this.dpr,2*this.dpr,H-140*this.dpr);}for(let i=0;i<5;i++){const y=(100+i*70)*this.dpr,cw=(W-130*this.dpr)*(0.35+0.5*((i+1)/5));const g=c.createLinearGradient(48*this.dpr,0,48*this.dpr+cw,0);g.addColorStop(0,'rgba(73,184,223,.05)');g.addColorStop(clamp(phase+.1,0,1),'rgba(73,184,223,.20)');g.addColorStop(1,'rgba(73,184,223,.04)');c.fillStyle=g;c.fillRect(48*this.dpr,y,cw,7*this.dpr);}c.fillStyle='rgba(140,236,255,.72)';c.font=`700 ${11*this.dpr}px ui-monospace,SFMono-Regular,Consolas,monospace`;c.fillText('HIDRATANDO HISTÓRICO · LIVE CONTINÚA',48*this.dpr,48*this.dpr);c.restore();}
  _drawHeatmap(W,H,m,lo,hi){const hm=this.payload?.heatmap_history;if(!hm||this.heatmapMode==='off'||!Array.isArray(hm.times)||!hm.times.length)return;const strikes=hm.strikes||[];if(!strikes.length)return;let field='joint_coherence';if(this.heatmapMode==='gamma')field='gamma_intensity';else if(this.heatmapMode==='delta')field='delta_intensity';const matrix=hm[field]||[],cols=Math.min(hm.times.length,120),start=Math.max(0,hm.times.length-cols),pw=(W-m.l-m.r)/Math.max(1,cols);this.ctx.save();this.ctx.globalAlpha=this.heatmapOpacity;
    for(let ti=start;ti<hm.times.length;ti++)for(let si=0;si<strikes.length;si++){const s=num(strikes[si],NaN);if(!Number.isFinite(s)||s<lo||s>hi)continue;const v=num(matrix?.[si]?.[ti],0),a=clamp(Math.abs(v),0,1);if(a<.04)continue;const y=m.t+(hi-s)/(hi-lo)*(H-m.t-m.b),rh=Math.max(2,(H-m.t-m.b)/Math.max(18,strikes.length)),x=m.l+(ti-start)*pw;this.ctx.fillStyle=v>=0?`rgba(20,145,95,${.12+.68*a})`:`rgba(210,55,75,${.12+.68*a})`;this.ctx.fillRect(x,y-rh/2,pw+1,rh+1);}
    this.ctx.restore();
  }
  _clusterProfileRows(rows,mapY){const valid=(rows||[]).filter(r=>Number.isFinite(num(r.strike,NaN))&&Number.isFinite(num(r.value,NaN)));if(valid.length<90)return valid;const bucketPx=Math.max(8,10*this.dpr),bins=new Map();for(const r of valid){const y=mapY(num(r.strike)),k=Math.floor(y/bucketPx),v=num(r.value),w=Math.max(1e-12,Math.abs(v)),b=bins.get(k)||{value:0,weightedStrike:0,weight:0,count:0};b.value+=v;b.weightedStrike+=num(r.strike)*w;b.weight+=w;b.count++;bins.set(k,b);}const out=[...bins.values()].map(b=>({strike:b.weight?b.weightedStrike/b.weight:0,value:b.value,_lod_count:b.count}));this.lodActive=out.length<valid.length;return out;}
  _drawProfiles(W,H,m,lo,hi){
    const mapY=s=>m.t+(hi-s)/(hi-lo)*(H-m.t-m.b),bundle=this.payload?.profile_bundle?.profiles||{};this.lodActive=false;
    const drawRows=(rows,side,label)=>{if(!Array.isArray(rows)||!rows.length)return;const vis=this._clusterProfileRows(rows.filter(r=>{const s=num(r.strike,NaN);return Number.isFinite(s)&&s>=lo&&s<=hi;}),mapY);let mx=0;for(const r of vis)mx=Math.max(mx,Math.abs(num(r.value)));if(!mx)return;this.ctx.save();
      for(const r of vis){const strike=num(r.strike,NaN),value=num(r.value),ratio=clamp(Math.abs(value)/mx,0,1),w=60*this.dpr*ratio,y=mapY(strike);const pos=value>=0,col=side==='left'?(pos?'rgba(33,230,165,.72)':'rgba(255,79,104,.72)'):(pos?'rgba(57,166,255,.72)':'rgba(255,137,82,.72)');this.ctx.fillStyle=col;if(ratio>.66&&!this.motionReduced){this.ctx.shadowColor=col;this.ctx.shadowBlur=(2+6*ratio)*this.dpr;}if(side==='left')this.ctx.fillRect(m.l-w,y-2*this.dpr,w,4*this.dpr);else this.ctx.fillRect(W-m.r,y-2*this.dpr,w,4*this.dpr);this.ctx.shadowBlur=0;}
      this.ctx.fillStyle='#718399';this.ctx.font=`700 ${9*this.dpr}px ui-monospace,Consolas,monospace`;this.ctx.fillText(label,side==='left'?Math.max(2,m.l-58*this.dpr):W-m.r+4*this.dpr,m.t+10*this.dpr);this.ctx.restore();};
    const metric=String(this.profileMetric||'Gamma+Delta');
    if(metric==='Gamma+Delta'){
      if(bundle.Gamma?.rows?.length||bundle.Delta?.rows?.length){drawRows(bundle.Gamma?.rows||[],'left','Γ');drawRows(bundle.Delta?.rows||[],'right','Δ');return;}
      const rows=this.payload?.profiles?.rows||[];if(!Array.isArray(rows))return;const g=rows.map(r=>({strike:r.strike,value:num(r.gamma_net??r.gamma??r.gex??r.net_gamma)})),d=rows.map(r=>({strike:r.strike,value:num(r.delta_net??r.delta??r.dex??r.net_delta)}));drawRows(g,'left','Γ');drawRows(d,'right','Δ');return;
    }
    const profile=bundle[metric];if(profile?.rows?.length)drawRows(profile.rows,metric==='Delta'||metric.includes('Volume')||metric.includes('OI')?'right':'left',metric.toUpperCase());
  }
  _drawLevels(W,H,m,lo,hi){const levels=this.payload?.levels||[];this.ctx.save();this.ctx.font=`${11*this.dpr}px ui-monospace,Consolas,monospace`;for(const lv of levels){const p=num(lv.price??lv.level??lv.value,NaN);if(!Number.isFinite(p)||p<lo||p>hi)continue;const y=m.t+(hi-p)/(hi-lo)*(H-m.t-m.b);this.ctx.strokeStyle='rgba(92,116,139,.25)';this.ctx.setLineDash([5*this.dpr,5*this.dpr]);this.ctx.beginPath();this.ctx.moveTo(m.l,y);this.ctx.lineTo(W-m.r,y);this.ctx.stroke();this.ctx.setLineDash([]);this.ctx.fillStyle='#74869a';this.ctx.fillText(String(lv.label??lv.name??lv.type??'LEVEL').slice(0,22),m.l+5*this.dpr,y-3*this.dpr);}this.ctx.restore();}
  _drawFlowMarkers(W,H,m,lo,hi,cs){const prints=this.payload?.option_prints||[];if(!Array.isArray(prints)||!prints.length||!cs.length)return;const times=cs.map(c=>new Date(c.t??c.time??c.timestamp).getTime()),t0=times[0],t1=times[times.length-1]||t0+1;this.ctx.save();for(const p of prints.slice(-120)){const ts=new Date(p.timestamp??p.time??p.t).getTime(),price=num(p.underlying_price??p.price??p.spot,NaN);if(!Number.isFinite(ts)||!Number.isFinite(price)||price<lo||price>hi||ts<t0||ts>t1)continue;const x=m.l+(ts-t0)/Math.max(1,t1-t0)*(W-m.l-m.r),y=m.t+(hi-price)/(hi-lo)*(H-m.t-m.b),call=String(p.option_type??p.side??p.right??'').toUpperCase().includes('C');this.ctx.fillStyle=call?'rgba(33,230,165,.88)':'rgba(255,79,104,.88)';this.ctx.beginPath();this.ctx.arc(x,y,3.5*this.dpr,0,Math.PI*2);this.ctx.fill();}this.ctx.restore();}
  _drawRipples(W,H,m,lo,hi,cs){if(!this.ripples.length||this.motionReduced||!cs.length)return;const times=cs.map(c=>new Date(c.t??c.time??c.timestamp).getTime()),t0=times[0],t1=times[times.length-1]||t0+1,now=performance.now();this.ctx.save();for(const r of this.ripples){const ts=new Date(r.t).getTime();if(!Number.isFinite(ts)||ts<t0||ts>t1||r.price<lo||r.price>hi)continue;const age=clamp((now-r.born)/r.duration,0,1),alpha=(1-age)*.72,radius=(5+32*age)*this.dpr,x=m.l+(ts-t0)/Math.max(1,t1-t0)*(W-m.l-m.r),y=m.t+(hi-r.price)/(hi-lo)*(H-m.t-m.b),col=r.positive?'33,230,165':'255,79,104';this.ctx.strokeStyle=`rgba(${col},${alpha})`;this.ctx.lineWidth=Math.max(1,2.1*this.dpr*(1-age*.5));this.ctx.shadowColor=`rgba(${col},${alpha})`;this.ctx.shadowBlur=8*this.dpr*(1-age);this.ctx.beginPath();this.ctx.arc(x,y,radius,0,Math.PI*2);this.ctx.stroke();this.ctx.shadowBlur=0;}this.ctx.restore();}
  _drawLiveMarker(W,H,m,lo,hi){if(!Number.isFinite(this.visualSpot)||this.visualSpot<lo||this.visualSpot>hi)return;const y=m.t+(hi-this.visualSpot)/(hi-lo)*(H-m.t-m.b),c=this.ctx;c.save();c.strokeStyle='rgba(34,211,238,.38)';c.lineWidth=1*this.dpr;c.setLineDash([3*this.dpr,5*this.dpr]);c.beginPath();c.moveTo(m.l,y);c.lineTo(W-m.r,y);c.stroke();c.setLineDash([]);const text=`LIVE ${this.visualSpot.toFixed(this.visualSpot<100?2:1)}`,pad=6*this.dpr;c.font=`700 ${10*this.dpr}px ui-monospace,Consolas,monospace`;const tw=c.measureText(text).width;c.fillStyle='rgba(5,14,22,.94)';c.fillRect(W-m.r-tw-2*pad,y-10*this.dpr,tw+2*pad,20*this.dpr);c.strokeStyle='rgba(34,211,238,.45)';c.strokeRect(W-m.r-tw-2*pad,y-10*this.dpr,tw+2*pad,20*this.dpr);c.fillStyle='#8cecff';c.fillText(text,W-m.r-tw-pad,y+3.5*this.dpr);c.restore();}
  _drawCrosshair(W,H,m,lo,hi,cs){if(!this.cross)return;const c=this.ctx,x=clamp(this.cross.x*this.dpr,m.l,W-m.r),y=clamp(this.cross.y*this.dpr,m.t,H-m.b);c.save();c.strokeStyle='rgba(162,184,202,.58)';c.lineWidth=1*this.dpr;c.setLineDash([4*this.dpr,4*this.dpr]);c.beginPath();c.moveTo(x,m.t);c.lineTo(x,H-m.b);c.moveTo(m.l,y);c.lineTo(W-m.r,y);c.stroke();c.setLineDash([]);const price=hi-((y-m.t)/Math.max(1,H-m.t-m.b))*(hi-lo),ptext=price.toFixed(price<100?2:1);c.font=`700 ${10*this.dpr}px ui-monospace,Consolas,monospace`;const ph=20*this.dpr,pw=Math.max(52*this.dpr,c.measureText(ptext).width+14*this.dpr);c.fillStyle='rgba(5,14,22,.97)';c.fillRect(W-m.r,y-ph/2,pw,ph);c.strokeStyle='rgba(140,236,255,.38)';c.strokeRect(W-m.r,y-ph/2,pw,ph);c.fillStyle='#8cecff';c.textAlign='center';c.textBaseline='middle';c.fillText(ptext,W-m.r+pw/2,y);
    if(cs.length){const usable=Math.max(1,W-m.l-m.r),i=clamp(Math.round((x-m.l)/usable*(cs.length-1)),0,cs.length-1),t=cs[i]?.t??cs[i]?.time??cs[i]?.timestamp;if(t){const d=new Date(t),tt=Number.isFinite(d.getTime())?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):String(t).slice(11,19),tw=c.measureText(tt).width+14*this.dpr,tx=clamp(x-tw/2,m.l,W-m.r-tw);c.fillStyle='rgba(5,14,22,.97)';c.fillRect(tx,H-m.b,tw,ph);c.strokeStyle='rgba(140,236,255,.30)';c.strokeRect(tx,H-m.b,tw,ph);c.fillStyle='#a8bdca';c.fillText(tt,tx+tw/2,H-m.b+ph/2);}}
    c.textAlign='start';c.textBaseline='alphabetic';c.restore();
  }
  draw(){const ctx=this.ctx,W=this.canvas.width,H=this.canvas.height,m={l:78*this.dpr,r:78*this.dpr,t:22*this.dpr,b:34*this.dpr};ctx.fillStyle='#0b0e14';ctx.fillRect(0,0,W,H);const cs=this._visibleCandles();if(!cs.length){this._drawLoading(W,H);if(!this.motionReduced)this._scheduleAnimation();return;}const[lo,hi]=this._priceRange(cs);this._drawHeatmap(W,H,m,lo,hi);ctx.strokeStyle='rgba(80,112,138,.11)';ctx.lineWidth=1;for(let i=0;i<6;i++){const y=m.t+i*(H-m.t-m.b)/5;ctx.beginPath();ctx.moveTo(m.l,y);ctx.lineTo(W-m.r,y);ctx.stroke();}
    this._drawLevels(W,H,m,lo,hi);const bw=Math.max(1.5*this.dpr,(W-m.l-m.r)/Math.max(1,cs.length)*.62);cs.forEach((c,i)=>{const q=this._xy(c,i,cs.length,lo,hi,W,H,m),up=q.cl>=q.o;ctx.strokeStyle=up?'#1689ff':'#ff2aa3';ctx.fillStyle=ctx.strokeStyle;ctx.globalAlpha=.90;ctx.beginPath();ctx.moveTo(q.x,q.h);ctx.lineTo(q.x,q.l);ctx.stroke();ctx.fillRect(q.x-bw/2,Math.min(q.o,q.cl),bw,Math.max(1.5*this.dpr,Math.abs(q.cl-q.o)));ctx.globalAlpha=1;});this._drawProfiles(W,H,m,lo,hi);this._drawFlowMarkers(W,H,m,lo,hi,cs);this._drawRipples(W,H,m,lo,hi,cs);this._drawLiveMarker(W,H,m,lo,hi);
    ctx.fillStyle='#8da1b2';ctx.font=`${11*this.dpr}px ui-monospace,Consolas,monospace`;for(let i=0;i<6;i++){const v=hi-(hi-lo)*i/5,y=m.t+i*(H-m.t-m.b)/5;ctx.fillText(v.toFixed(v<100?2:1),5*this.dpr,y+4*this.dpr);}if(this.lodActive){ctx.fillStyle='rgba(34,211,238,.65)';ctx.font=`700 ${9*this.dpr}px ui-monospace,Consolas,monospace`;ctx.fillText('LOD VISUAL · DATOS CRUDOS INTACTOS',m.l,m.t+11*this.dpr);}this._drawCrosshair(W,H,m,lo,hi,cs);
  }
  _emitCrosshair(){if(!this.cross||!this.onCrosshair)return;const cs=this._visibleCandles();if(!cs.length)return;const r=this.canvas.getBoundingClientRect(),m=78,usable=Math.max(1,r.width-156),i=clamp(Math.round((this.cross.x-m)/usable*(cs.length-1)),0,cs.length-1);this.onCrosshair(cs[i],this.cross);}
}
window.ITMQChartDataCache=window.ITMQChartDataCache||new ITMChartDataCache();window.ITMChartEngine=ITMChartEngine;
})();

(()=>{
  'use strict';
  const lab=document.getElementById('returnAnomalyLab');
  if(!lab) return;

  const $=id=>document.getElementById(id);
  const el={
    summary:$('ramSummaryState'), state:$('ramState'), stateDetail:$('ramStateDetail'),
    score:$('ramScore'), pressure:$('ramPressure'), baseline:$('ramBaseline'), baselineN:$('ramBaselineN'),
    episodes:$('ramEpisodes'), flow:$('ramFlow'), flowDetail:$('ramFlowDetail'), updated:$('ramUpdated'),
    canvas:$('returnAnomalyCanvas'), empty:$('ramEmpty')
  };
  const buttons=[...lab.querySelectorAll('.ram-tf')];
  let tf='1m', last=null, busy=false, timer=0;

  const safeText=(node,value,fallback='—')=>{if(node) node.textContent=(value===null||value===undefined||value==='')?fallback:String(value)};
  const fmt=(v,d=1)=>Number.isFinite(Number(v))?Number(v).toFixed(d):'—';
  const stateLabel=s=>({
    TRIGGERED_RETURN_ANOMALY:'TRIGGERED', BUILDING:'BUILDING', WATCH:'WATCH', NORMAL:'NORMAL', COLLECTING:'COLLECTING'
  }[String(s||'').toUpperCase()]||String(s||'—'));

  function setWaiting(msg){
    safeText(el.summary,'ACTIVE · COLLECTING'); safeText(el.state,'COLLECTING'); safeText(el.score,'—'); safeText(el.pressure,'—');
    safeText(el.baseline,'—'); safeText(el.baselineN,'solo pasado'); safeText(el.episodes,'—'); safeText(el.flow,'—');
    safeText(el.flowDetail,'SHADOW / absorción');
    if(el.empty){el.empty.classList.remove('hidden'); el.empty.textContent=msg||'Esperando historial causal…'}
    draw([]);
  }

  function render(data){
    last=data;
    if(!data){setWaiting('Esperando historial causal…');return}
    const r=data.timeframes?.[tf]||{};
    const cand=data.candidate||{};
    const flow=cand.contexto_flow||{};
    safeText(el.summary, `${stateLabel(r.estado)} · ${tf}`);
    safeText(el.state, stateLabel(r.estado));
    safeText(el.stateDetail, r.regimen_vol?`RÉGIMEN VOL · ${r.regimen_vol}`:'CURRENT protegido');
    safeText(el.score, Number.isFinite(Number(r.precursor_score))?`${fmt(r.precursor_score,1)}/100`:'—');
    safeText(el.pressure, r.presion_observada||'—');
    safeText(el.baseline, r.baseline_scope||'—');
    safeText(el.baselineN, `${Number(r.baseline_n||0)} obs · solo pasado`);
    safeText(el.episodes, String(r.episodios_totales??'—'));
    safeText(el.flow, flow.estado||'—');
    const pv=Number.isFinite(Number(flow.p_value))?`p=${fmt(flow.p_value,3)}`:'';
    const ab=Number.isFinite(Number(flow.absorcion))?`abs ${fmt(flow.absorcion,0)}`:'';
    safeText(el.flowDetail,[flow.alineacion,pv,ab].filter(Boolean).join(' · ')||'SHADOW / absorción');
    safeText(el.updated,new Date().toLocaleTimeString());
    if(el.empty) el.empty.classList.add('hidden');
    draw(Array.isArray(r.series)?r.series:[],Array.isArray(r.episodios)?r.episodios:[]);
  }

  function resizeCanvas(){
    const c=el.canvas;if(!c)return null;
    const rect=c.getBoundingClientRect(), dpr=Math.min(window.devicePixelRatio||1,2);
    const w=Math.max(320,Math.floor(rect.width)), h=Math.max(220,Math.floor(rect.height||300));
    const pw=Math.floor(w*dpr),ph=Math.floor(h*dpr);
    if(c.width!==pw||c.height!==ph){c.width=pw;c.height=ph}
    const ctx=c.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);return {ctx,w,h};
  }

  function draw(series,episodes=[]){
    const out=resizeCanvas();if(!out)return;const {ctx,w,h}=out;
    ctx.clearRect(0,0,w,h);ctx.fillStyle='#070d15';ctx.fillRect(0,0,w,h);
    const pad={l:54,r:18,t:20,b:30}, iw=w-pad.l-pad.r, ih=h-pad.t-pad.b;
    ctx.strokeStyle='rgba(126,164,194,.10)';ctx.lineWidth=1;
    for(let i=0;i<=4;i++){const y=pad.t+ih*i/4;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke()}
    if(!series||series.length<2){ctx.fillStyle='#63798c';ctx.font='12px system-ui';ctx.fillText('Esperando historial causal…',pad.l,pad.t+30);return}
    const data=series.slice(-240);
    const vals=[];
    data.forEach(p=>{[p.log_return,p.banda_superior,p.banda_inferior].forEach(v=>{if(Number.isFinite(Number(v)))vals.push(Number(v))})});
    let max=Math.max(...vals.map(v=>Math.abs(v)),1e-6);max*=1.12;
    const x=i=>pad.l+(data.length<=1?0:(iw*i/(data.length-1)));
    const y=v=>pad.t+ih/2-(Number(v)/max)*(ih*.46);
    const y0=y(0);
    ctx.strokeStyle='rgba(160,182,201,.34)';ctx.beginPath();ctx.moveTo(pad.l,y0);ctx.lineTo(w-pad.r,y0);ctx.stroke();

    // Bandas causales: segmentos; no se conectan huecos largos entre sesiones.
    for(const key of ['banda_superior','banda_inferior']){
      ctx.strokeStyle=key==='banda_superior'?'rgba(255,106,170,.50)':'rgba(97,203,255,.45)';
      ctx.setLineDash([5,5]);ctx.beginPath();let open=false, prevTs=null;
      data.forEach((p,i)=>{const v=Number(p[key]);const ts=Date.parse(p.timestamp);if(!Number.isFinite(v)) {open=false;prevTs=null;return}
        const gap=prevTs===null?0:(ts-prevTs)/60000;const tfm=parseInt(tf,10)||1;
        if(!open||gap>Math.max(45,tfm*3)){ctx.moveTo(x(i),y(v));open=true}else ctx.lineTo(x(i),y(v));
        prevTs=ts;
      });ctx.stroke();ctx.setLineDash([]);
    }

    // Retornos como stems/barras, no línea continua entre sesiones.
    const bw=Math.max(1,Math.min(5,iw/Math.max(data.length,1)*.62));
    data.forEach((p,i)=>{const v=Number(p.log_return);if(!Number.isFinite(v))return;const yy=y(v);const xx=x(i);
      ctx.fillStyle=v>=0?'rgba(75,215,255,.62)':'rgba(255,78,153,.62)';ctx.fillRect(xx-bw/2,Math.min(y0,yy),bw,Math.max(1,Math.abs(yy-y0)));
      if(p.anomalia){ctx.beginPath();ctx.arc(xx,yy,4.2,0,Math.PI*2);ctx.fillStyle='#ffd36a';ctx.fill();ctx.strokeStyle='rgba(255,211,106,.55)';ctx.stroke()}
    });

    // Episodios activos/cerrados como etiquetas discretas, sin unir shocks.
    const byTs=new Map(data.map((p,i)=>[p.timestamp,i]));
    episodes.slice(-8).forEach(ep=>{const i=byTs.get(ep.fin);if(i===undefined)return;const xx=x(i);ctx.fillStyle='rgba(221,235,248,.72)';ctx.font='10px system-ui';ctx.fillText(`#${ep.id}`,Math.min(xx+5,w-36),pad.t+12)});

    ctx.fillStyle='#7e94a7';ctx.font='10px system-ui';ctx.fillText(`+${(max*100).toFixed(3)}%`,4,pad.t+5);ctx.fillText('0',32,y0+3);ctx.fillText(`-${(max*100).toFixed(3)}%`,4,pad.t+ih);
    const a=new Date(data[0].timestamp),b=new Date(data[data.length-1].timestamp);ctx.fillText(a.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}),pad.l,h-9);const txt=b.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});ctx.fillText(txt,w-pad.r-38,h-9);
  }

  async function load(){
    if(busy||!lab.open||document.hidden)return;busy=true;
    try{
      const r=await fetch('/api/seccion/anomalias-rendimientos',{cache:'no-store',headers:{'Accept':'application/json'}});
      if(!r.ok) throw new Error(`HTTP ${r.status}`);render(await r.json());
    }catch(e){setWaiting('Esperando historial causal…')}finally{busy=false}
  }

  buttons.forEach(btn=>btn.addEventListener('click',()=>{buttons.forEach(x=>x.classList.toggle('active',x===btn));tf=btn.dataset.ramTf||'1m';if(last)render(last)}));
  // El identificador `timer` se asignaba y no se volvía a leer nunca: el intervalo
  // quedaba vivo el resto de la sesión sin forma de pararlo. `load()` ya se protege
  // con `!lab.open || document.hidden`, así que no consumía red, pero seguía
  // despertando cada 3 s para no hacer nada. Ahora late sólo con el panel abierto.
  timer = null;
  const stopPolling = () => { if (timer !== null) { window.clearInterval(timer); timer = null; } };
  const startPolling = () => { if (timer === null) timer = window.setInterval(load, 3000); };
  lab.addEventListener('toggle',()=>{if(lab.open){load();startPolling();}else stopPolling();});
  window.addEventListener('resize',()=>{if(last&&lab.open)render(last)});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stopPolling();else if(lab.open){load();startPolling();}});
  window.addEventListener('pagehide',stopPolling);
  load();
  if (lab.open) startPolling();
})();

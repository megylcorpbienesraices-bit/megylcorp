const fmt = (v,d=2)=> (v===null||v===undefined||Number.isNaN(Number(v)))?'—':Number(v).toFixed(d);
const compact = v => { v=Number(v||0); const a=Math.abs(v); if(a>=1e9)return `${(v/1e9).toFixed(2)}B`; if(a>=1e6)return `${(v/1e6).toFixed(2)}M`; if(a>=1e3)return `${(v/1e3).toFixed(1)}K`; return v.toFixed(0); };
const money = v => { v=Number(v||0); const s=v>=0?'+':'-'; return `${s}$${compact(Math.abs(v))}`; };
const intfmt = v => (v===null||v===undefined||Number.isNaN(Number(v)))?'—':Math.round(Number(v)).toLocaleString('en-US');
const exposureExact = v => (v===null||v===undefined||Number.isNaN(Number(v)))?'—':`${Number(v)>=0?'+':'-'}$${Math.abs(Math.round(Number(v))).toLocaleString('en-US')}`;
const el=id=>document.getElementById(id);
let lastState=null;
let publicationBlocked=false;
let publicationCritical=false;
let activeSymbol="DIA";
let activeSymbolEpoch=0;
let assetSwitchSeq=0;
let stateRequestSeq=0;
let assetCatalog=[];
let uiWorkspace="trader";
let easyMode=false;
const INTERNAL_MODE = new URLSearchParams(window.location.search).get('internal') === '1';
function applyInternalMode(){document.body.classList.toggle('internal-mode',INTERNAL_MODE);document.querySelectorAll('[data-internal-only]').forEach(x=>{x.hidden=!INTERNAL_MODE;});}
let liveTickCursor=0;
let liveTickBusy=false;
let traceFollow=true;
let tracePulseBusy=false;
let tracePulseLast=null;
let nodeInspectorLock=null;
let chartRequestSeq=0;
let chartAppliedSeq=0;
let surfaceSliceRequestSeq=0;
let surfaceMainRequestSeq=0;
let assetSwitchInProgress=false;
let assetQuantWarmup=false;
let assetHydrationSeq=0;
let chartFetchController=null;
let surfaceMainFetchController=null;
let surfaceSliceFetchController=null;
let pendingCharts=false;
let pendingSurfaceMain=false;
let pendingSurfaceSlice=false;
let lastPremarketAnalysis=null;
let traceResizeBound=false;
let preferReadyChartCache=true;
let calendarPrepToken=0;
let replayClockMarks=[];
let replayClockIndex=0;
let replayPlaybackTimer=null;
let replayPlaybackBusy=false;
let traceRecommendedConfig={preset:'pro',candle:'1m',window:'60',yframe:'auto',temporalHeatmap:'joint',why:'Configuración institucional equilibrada.'};
function setText(id,val){const x=el(id);if(x)x.textContent=val??'—'}
const SECTION_GROUPS={flow:'flow',netdrift:'flow',prints:'flow',chain:'structure',exposure:'structure',gexmatrix:'structure',positioning:'structure',surface:'structure'};
const MERGED_SECTIONS={infrastructure:{parent:'auditor',workspace:'trader'},netdrift:{parent:'flow'},prints:{parent:'flow'},exposure:{parent:'chain'},gexmatrix:{parent:'chain'},positioning:{parent:'chain'},surface:{parent:'chain'}};
const CHART_RUNTIME_MAP={
 command:['operativaTraceChart'],trace:['traceChart','traceFlowProChart'],
 scanner:['scannerChart'],chain:['chainChart'],flow:['flowProChart'],netdrift:['netDriftChart'],exposure:['exposureChart'],gexmatrix:['gexMatrixChart','gammaMigrationChart'],vol:['volChart'],positioning:['netPositioningChart','volumeChart'],prints:['printsChart'],surface:['surfaceChart','surfaceSliceChart','surfaceAltChart'],macro:['macroChart']
};
function markChartRuntime(id,data='UNKNOWN',render='WAITING',stream='N/A',detail=''){const h=el(id);if(!h)return;h.dataset.itmqData=String(data);h.dataset.itmqRender=String(render);h.dataset.itmqStream=String(stream);h.dataset.itmqDataState=String(data);h.dataset.itmqRenderState=String(render);h.dataset.itmqStreamState=String(stream);h.dataset.itmqRuntime=`DATA:${data}|RENDER:${render}|STREAM:${stream}`;if(detail)h.dataset.itmqRuntimeDetail=String(detail).slice(0,180);}
function rehydrateActiveCharts(section){const ids=CHART_RUNTIME_MAP[section]||[];requestAnimationFrame(()=>requestAnimationFrame(()=>{window.dispatchEvent(new Event('resize'));window.ITMQUltraCharts?.redrawAll?.();window.ITMQNextGen?.redrawTrace?.();if(section==='surface')window.ITMQNextGen?.refreshSurface?.({force:true});for(const id of ids){const h=el(id);if(!h)continue;const r=h.getBoundingClientRect();markChartRuntime(id,h.childNodes.length?'PRESENT':'WAITING',r.width>1&&r.height>1?'VISIBLE':'ZERO_SIZE',id.includes('trace')?'LIVE_PATH':'SNAPSHOT',`${Math.round(r.width)}x${Math.round(r.height)}`);try{if(window.Plotly&&h._fullLayout)Plotly.Plots.resize(h);}catch(_){}}}),0);}
window.ITMQChartRuntime={mark:markChartRuntime,rehydrate:rehydrateActiveCharts,map:CHART_RUNTIME_MAP,contract:'DATA_RENDER_STREAM_V1'};

function navigateSection(section){
 const merged=MERGED_SECTIONS[section]||null;const navSection=merged?.parent||section;
 const btn=[...document.querySelectorAll('.nav-btn')].find(x=>x.dataset.section===navSection);
 const workspace=merged?.workspace||btn?.dataset.workspace;
 if(workspace&&workspace!==uiWorkspace)applyWorkspace(workspace,false);
 document.querySelectorAll('.nav-btn').forEach(x=>x.classList.toggle('active',x.dataset.section===navSection));
 document.querySelectorAll('.page-section').forEach(x=>x.classList.toggle('active',x.id===`section-${section}`));
 document.querySelectorAll('.guide-step').forEach(x=>x.classList.toggle('active',x.dataset.go===section));
 const sectionGroup=SECTION_GROUPS[section]||'';
 document.querySelectorAll('.group-subnav').forEach(x=>x.classList.toggle('hidden',x.dataset.group!==sectionGroup));
 document.querySelectorAll('.group-subnav [data-lean-go]').forEach(x=>x.classList.toggle('active',x.dataset.leanGo===section));
 document.body.dataset.section=section;
 document.body.classList.toggle('trace-focus',section==='trace');
 window.dispatchEvent(new CustomEvent('itmq:section-change',{detail:{section}}));
 setTimeout(()=>{rehydrateActiveCharts(section);if(section==='infrastructure')refreshProviderFlowHealth(true);const view=chartViewForSection();if(view&&!assetQuantWarmup)loadCharts();if(tablesNeeded())loadTablesIfNeeded();setTimeout(()=>rehydrateActiveCharts(section),180);},70);
}
window.ITMQNavigateSection=navigateSection;
function applyWorkspace(name,navigate=true){
 uiWorkspace=['trader','quant','research'].includes(name)?name:'trader';
 document.body.dataset.workspace=uiWorkspace;
 document.querySelectorAll('.workspace-btn').forEach(x=>x.classList.toggle('active',x.dataset.workspace===uiWorkspace));
 document.querySelectorAll('.nav-btn').forEach(x=>x.classList.toggle('workspace-hidden',x.dataset.workspace!==uiWorkspace));
 document.querySelectorAll('.nav-group').forEach(x=>x.classList.toggle('workspace-hidden',x.dataset.workspace!==uiWorkspace));
 if(navigate){const first=document.querySelector(`.nav-btn[data-workspace="${uiWorkspace}"]`);if(first)navigateSection(first.dataset.section);}
}
function applyEasyMode(on){
 easyMode=Boolean(on);document.body.classList.add('v117-ui','v118-ui');document.body.classList.toggle('easy-ui',easyMode);document.body.classList.toggle('full-ui',!easyMode);
 const b=el('easyModeBtn');if(b){b.textContent=easyMode?'MODO FÁCIL · ON':'MODO FÁCIL · OFF';b.classList.toggle('active',easyMode);}
}

function applyInstitutionalTheme(theme, persist=true){
 const next=String(theme||'dark').toLowerCase()==='light'?'light':'dark';
 document.documentElement.dataset.theme=next;
 document.body?.setAttribute('data-theme',next);
 if(persist){try{localStorage.setItem('itmq-theme',next);}catch(_){}}
 const b=el('themeToggle');if(b){b.textContent=next==='light'?'TEMA · CLARO':'TEMA · OSCURO';b.classList.toggle('active',next==='light');}
 window.dispatchEvent(new CustomEvent('itmq:theme-change',{detail:{theme:next}}));
 window.ITMQNextGen?.redrawTrace?.();window.ITMQUltraCharts?.redrawAll?.();
 return next;
}

function localDateISO(){const d=new Date(),y=d.getFullYear(),m=String(d.getMonth()+1).padStart(2,'0'),q=String(d.getDate()).padStart(2,'0');return `${y}-${m}-${q}`;}
const sleepMs=ms=>new Promise(r=>setTimeout(r,ms));
function replayDisplayMark(iso){
 const x=String(iso||'');if(!x)return '—';const d=new Date(x);if(Number.isNaN(d.getTime()))return x.replace('T',' ');
 return d.toLocaleString('es-EC',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}
function stopReplayPlayback(){if(replayPlaybackTimer){clearInterval(replayPlaybackTimer);replayPlaybackTimer=null;}const b=el('replayPlayPause');if(b)b.textContent='▶ PLAY';}
function configureReplayClock(clock,asof=null){
 const marks=Array.isArray(clock?.marks)?clock.marks.filter(Boolean):[];replayClockMarks=marks;
 const slider=el('replayTimeline');
 if(!marks.length){replayClockIndex=0;if(slider){slider.min='0';slider.max='0';slider.value='0';slider.disabled=true;}setText('replayTimelineLabel',lastState?.replay?.mode&&lastState.replay.mode!=='LIVE'?'HISTÓRICO · SIN RELOJ':'LIVE');stopReplayPlayback();return;}
 let idx=0;if(asof){const t=Date.parse(asof);let best=Infinity;marks.forEach((m,i)=>{const q=Math.abs(Date.parse(m)-t);if(Number.isFinite(q)&&q<best){best=q;idx=i;}});}
 replayClockIndex=idx;if(slider){slider.min='0';slider.max=String(marks.length-1);slider.value=String(idx);slider.disabled=false;}
 setText('replayTimelineLabel',replayDisplayMark(marks[idx]));
 const tm=el('replayTime');if(tm){const mm=String(marks[idx]).match(/T(\d{2}:\d{2}:\d{2})/);if(mm)tm.value=mm[1];}
}
async function applyReplayDateInstant(dateValue){const d=String(dateValue||'').slice(0,10);if(!d)return;return applyReplaySession(null,d);}
function bindReplayCalendar(){const cal=el('replayDateCalendar');if(!cal)return;const today=localDateISO();cal.max=today;if(!cal.value)cal.value=today;cal.addEventListener('change',()=>{const d=cal.value;if(d)applyReplayDateInstant(d);});}
function initInstitutionalTheme(){let saved='dark';try{saved=localStorage.getItem('itmq-theme')||'dark';}catch(_){}return applyInstitutionalTheme(saved,false);}

function renderTraceOrderflow(m){
 m=m||{};const c=m.confirmation||{},a=m.aggression||{},cd=m.cadence||{},mt=m.multiframe||{},z=m.scanner_zone||{};
 const state=String(c.state||'WAITING').toUpperCase();
 const labels={WAITING:'ESPERANDO ZONA',ARMED:'ARMADO · LEYENDO FLUJO',CONFIRMED:'CONFIRMADO',REJECTED:'RECHAZADO',ABSORBED:'ABSORCIÓN · NO ENTRAR',CHURN:'CHURN · NIVEL DISPUTADO',EXPIRED:'EXPIRÓ · SIN PARTICIPACIÓN'};
 setText('tapeConfirmState',labels[state]||state);
 setText('tapeConfirmZone',(z.low!=null&&z.high!=null)?`${fmt(z.low)} – ${fmt(z.high)}${c.direction?' · '+c.direction:''}`:'—');
 const p=Number(c.progress_pct);setText('tapeConfirmProgress',Number.isFinite(p)?`${p>=0?'+':''}${fmt(p,0)}%`:'0%');
 const bar=el('tapeConfirmBar');if(bar){const w=Math.min(100,Math.abs(Number.isFinite(p)?p:0));bar.style.width=`${w}%`;bar.dataset.negative=(p<0)?'1':'0';}
 setText('tapeConfirmSeconds',c.seconds_remaining==null?'—':`${fmt(c.seconds_remaining,0)}s`);
 setText('tapeConfirmSigned',c.signed_volume==null?'—':`${Number(c.signed_volume)>=0?'+':''}${intfmt(c.signed_volume)}`);
 setText('tapeConfirmBuyPct',c.buy_pct==null?'BUY —':`BUY ${fmt(c.buy_pct,0)}% · ${c.trades??0} trades`);
 setText('tapeCadence',cd.regime||'SIN DATOS');
 setText('tapeCadenceDetail',cd.bars_per_minute==null?'Esperando velas':`${fmt(cd.bars_per_minute,2)} vel/min · mediana ${fmt(cd.median_seconds,0)}s · timeout ${fmt(cd.timeout_pct,0)}%`);
 setText('tapeAggressionScore',`${fmt(a.score??50,0)}/100 · ${a.control||'MIXED'}`);
 const contested=a.contested?'NIVEL DISPUTADO':'nivel no disputado';setText('tapeContested',`${contested} · CHURN ${fmt(a.churn_pct??0,0)}% · ABS ${fmt(a.absorption_pct??0,0)}%`);
 let note=c.note||'La confirmación usa flujo tick a tick dentro de la zona.';
 if(state==='CONFIRMED')note='El flujo acompaña la dirección del Scanner y el precio responde. Confirmación de entrada, no señal aislada.';
 else if(state==='ABSORBED')note='Tu lado está golpeando, pero el precio casi no avanza: tamaño pasivo está defendiendo el nivel. No persigas la entrada.';
 else if(state==='REJECTED')note='El flujo dominante va contra la dirección del Scanner. La entrada queda rechazada.';
 else if(state==='CHURN')note='Mucho volumen, compradores y vendedores equilibrados. El nivel está disputado; todavía no hay ganador.';
 else if(state==='EXPIRED')note='Se agotó el tiempo sin participación suficiente. No hubo confirmación en esta visita a la zona.';
 setText('tapeConfirmNote',note);
 const frameText=(k)=>{const f=mt?.frames?.[k]||{};if(!f.ready)return 'COLLECTING';const z=Number(f.delta_z);return `${Number.isFinite(z)?(z>=0?'+':'')+fmt(z,2):'—'} · ${f.control||'MIXED'}`;};
 setText('tapeMtf1m',frameText('1m'));setText('tapeMtf3m',frameText('3m'));setText('tapeMtf5m',frameText('5m'));
 setText('tapeMtfState',mt.status||'COLLECTING');setText('tapeMtfFastSlow',mt.fast_vs_slow||'—');
 setText('tapeMtfNote',`${mt.family||'MULTITIMEFRAME_TAPE_CONTEXT'} · banda ${fmt(mt.dead_band??1.6,2)} ${mt.dead_band_status||'SHADOW'} · contexto solamente`);

 const panel=el('traceTapePanel');if(panel)panel.dataset.state=state;
}

function renderDecisionCockpit(s){
 const q=s?.scanner||{},syn=s?.quant_synthesis||{},cmd=s?.command||{},z=syn.zone||cmd.zone||q.zone||{},mt=s?.trace_orderflow?.multiframe||{};
 const ready=Boolean(syn.ready??q.ready),dir=syn.direction||cmd.bias||'WAITING',edge=syn.action_code||cmd.actionability_state||q.edge_state||'WAIT';
 const d=el('opDirection');if(d){d.textContent=ready?(syn.direction_text||dir):'ESPERANDO';d.className=dir==='BUY'?'buy':dir==='SELL'?'sell':'wait';}
 const action=syn.action||'ESPERAR';setText('opActionability',action);const ag=el('opActionability');if(ag){ag.className=['ENTER'].includes(edge)?'actionable':['ARMED','CAUTION','WAIT_TAPE','WAIT_ZONE'].includes(edge)?'caution':'none';}
 const strength=Number(syn.strength);setText('quantStrength',Number.isFinite(strength)?`${fmt(strength,0)}/100`:'—');const cc=Number(syn.context_confluence);setText('quantStrengthNote',syn.strength_is_probability?'probabilidad calibrada':`fuerza estructural · contexto ${Number.isFinite(cc)?fmt(cc,0)+'/100':'—'} · no es probabilidad`);
 setText('opDirectionDetail',ready?`${syn.scenario||q.scenario_type||'SCANNER'} · ${syn.market_phase||'ESTADO CUANT'} · ${syn.verdict||action}`:'Esperando estructura cuantitativa');
 setText('opZone',(z.low!=null&&z.high!=null)?`${fmt(z.low)} – ${fmt(z.high)}`:'—');
 const loc=syn.location||{};const locMap={IN_ZONE:'PRECIO DENTRO DE ZONA',NEAR_ZONE:'PRECIO CERCA DE ZONA',AWAY_FROM_ZONE:'PRECIO LEJOS · NO PERSEGUIR',UNKNOWN:'UBICACIÓN PENDIENTE'};let ltxt=locMap[loc.state]||loc.state||'—';if(loc.distance!=null&&loc.state!=='IN_ZONE')ltxt+=` · ${fmt(loc.distance,2)} pts`;setText('quantLocation',ltxt);
 setText('opT1',syn.target1==null?(q.target1==null?'—':fmt(q.target1)):fmt(syn.target1));setText('opT2',syn.target2==null?(q.target2==null?'—':fmt(q.target2)):fmt(syn.target2));
 setText('opInvalidation',syn.invalidation==null?(q.invalidation==null?'—':fmt(q.invalidation)):fmt(syn.invalidation));
 const sm=q.stop_model||{};setText('opInvalidationDetail',sm.sigma_h==null?'estructura + volatilidad explícita':`σ ${fmt(sm.sigma_h,2)} · k ${fmt(sm.k_live,2)} · ${sm.source||'VOL+STRUCT'}`);
 const why=el('quantWhy');if(why){why.innerHTML='';(syn.why||[]).slice(0,3).forEach(t=>{const row=document.createElement('span');row.textContent=t;why.appendChild(row)});if(!why.children.length){const row=document.createElement('span');row.textContent='Esperando suficiente información cuantitativa.';why.appendChild(row);}}
 let modelNote=syn.calibration_status==='CALIBRATED'?'CALIBRATION · READY':'CALIBRATION · COLLECTING';if(syn.probability_t1_first!=null)modelNote+=` · P(T1 primero) ${fmt(Number(syn.probability_t1_first)*100,1)}%`;else modelNote+=' · sin probabilidad publicada';setText('opActionabilityMode',modelNote);
 const ts=String(syn.tape_state||cmd.tape_state||'WAITING').toUpperCase();setText('opTapeState',`TIMING · ${ts}`);const tb=el('opTapeState');if(tb)tb.className=ts.toLowerCase();
 const prog=Math.max(0,Math.min(100,Math.abs(Number(syn.tape_progress_pct??cmd.tape_progress_pct)||0)));const fill=el('opTapeProgressFill');if(fill){fill.style.width=`${prog}%`;fill.classList.toggle('neg',['REJECTED','ABSORBED'].includes(ts));}
 let td='Tape temporiza; no cambia Scanner';if((syn.tape_progress_pct??cmd.tape_progress_pct)!=null)td=`${fmt(syn.tape_progress_pct??cmd.tape_progress_pct,0)}% · ${action}`;setText('opTapeDetail',td);
 // Detailed context stays collapsed; it is still populated for audit/inspection.
 setText('opGammaContext',cmd.gamma_regime||'—');setText('opGammaDetail',`${cmd.flip_context||'FLIP —'} · contexto interno`);
 const v=s?.volatility||{};setText('opVolContext',cmd.vol_regime||v.regime||'—');setText('opVolDetail',v.expected_move==null?'Expected Move —':`EM ${fmt(v.expected_move,2)} · ATM IV ${v.atm_iv==null?'—':fmt(v.atm_iv,1)+'%'}`);
 setText('opMtfContext',mt.status||'COLLECTING');const fr=mt.frames||{};const ftxt=k=>fr[k]?.ready?`${k} ${fr[k].control||'MIXED'} ${fr[k].delta_z==null?'':fmt(fr[k].delta_z,2)}`:`${k} COLLECTING`;setText('opMtfDetail',`${ftxt('1m')} · ${ftxt('3m')} · ${ftxt('5m')}`);
}

function renderExpiryWindow(info,z0){
 info=info||{};const mode=info.mode||'AUTO';const sel=el('expiryWindow');if(sel&&sel.value!==mode)sel.value=mode;
 setText('expiryWindowLabel',info.label||mode);const loading=Boolean(info.loading||info.count===null||info.status==='VERIFYING_CHAIN');
 setText('expiryCount',loading?'CARGANDO…':(info.count??0));setText('zeroDteToday',loading||z0?.loading||z0?.known===false?'VERIFICANDO…':(z0?.available?`SÍ · ${z0.contracts_today??0} contratos`:(z0?.label||'NO')));
 const rec=el('expiryRecommended');if(rec)rec.classList.toggle('hidden',mode!=='AUTO');
 const ex=info.expirations||[];const used=el('expiryUsed');if(used)used.innerHTML=loading?'<b>Cadena:</b> hidratando vencimientos y Greeks en segundo plano…':(ex.length?`<b>Expiraciones utilizadas:</b> ${ex.join(' · ')}`:'<b>Sin vencimientos</b> para esta ventana.');
 const wr=el('expiryWeights');if(wr){const w=info.weights||{};wr.innerHTML='';if(!loading&&mode==='AUTO'&&Object.keys(w).length){wr.classList.remove('hidden');Object.entries(w).forEach(([d,v])=>{const x=document.createElement('span');x.innerHTML=`${d} <strong>${Math.round(Number(v)*100)}%</strong>`;wr.appendChild(x)})}else wr.classList.add('hidden');}
 const ts=el('traceExpiryWindow');if(ts&&ts.value!==mode)ts.value=mode; setText('traceExpiryDetail',loading?`${info.label||mode} · VERIFICANDO CADENA · precio/TRACE independiente`:`${info.label||mode} · ${info.count??0} vencimientos · sincronizado globalmente`);
}
async function selectExpiryWindow(mode){
 const sel=el('expiryWindow'), tsel=el('traceExpiryWindow');if(sel)sel.disabled=true;if(tsel)tsel.disabled=true;clearError();
 try{const r=await api(`/api/expiry/select?window=${encodeURIComponent(mode)}`,{method:'POST'});renderState(r.state);await Promise.all([loadCharts(),loadTables()]);}
 catch(e){showError(e.message);await refreshState();}
 finally{if(sel)sel.disabled=false;if(tsel)tsel.disabled=false;}
}
function badgeState(s){return s||'—'}

async function api(url,opts={}){
 const r=await fetch(url,opts); if(r.status===401){location='/login';return;}
 const raw=await r.text(); let data=null; try{data=raw?JSON.parse(raw):null}catch{}
 if(!r.ok){const t=data?.detail||data?.message||raw||`HTTP ${r.status}`;throw new Error(t)}
 return data;
}
function showError(msg){console.error('[ITM INTERNAL]',msg);const x=el('globalError');if(!x)return;x.textContent=INTERNAL_MODE?String(msg||'ERROR INTERNO'):'ANÁLISIS TEMPORALMENTE NO DISPONIBLE · los datos visibles quedan como contexto hasta recuperar el ciclo.';x.classList.remove('hidden')}
function clearError(){el('globalError').classList.add('hidden')}
function setAssetLoading(on,symbol=''){const x=el('assetLoading');if(!x)return;if(on){setText('assetLoadingTitle',`CARGANDO ${symbol||'ACTIVO'}…`);x.classList.remove('hidden')}else{x.classList.add('hidden')}}

function updateAssetLabels(symbol){
 activeSymbol=String(symbol||activeSymbol||"ASSET").toUpperCase();
 document.querySelectorAll('.asset-symbol').forEach(x=>x.textContent=activeSymbol);
 setText('activeAssetSymbol',activeSymbol);
}
function normalizeSymbolCategory(asset){
 const raw=String(asset?.category||asset?.kind||'').toLowerCase();
 if(raw.includes('índice')||raw.includes('indice'))return 'Índices';
 if(raw.includes('futuro'))return 'Futuros';
 if(raw.includes('volatil'))return 'Volatilidad';
 if(raw.includes('acción')||raw.includes('accion')||raw.includes('stock'))return 'Acciones';
 if(raw.includes('etf')||raw.includes('etn'))return 'ETFs';
 return asset?.category||'Otros';
}
let symbolSearchCategory='Todos';
function renderSymbolSearchResults(){
 const root=el('symbolSearchResults');if(!root)return;
 const q=String(el('symbolSearchInput')?.value||'').trim().toLowerCase();
 const cat=String(symbolSearchCategory||'Todos');
 let rows=(assetCatalog||[]).filter(a=>{
   if(cat!=='Todos'&&normalizeSymbolCategory(a)!==cat)return false;
   if(!q)return true;
   return [a.symbol,a.name,a.description,a.exchange,a.family,a.kind,(a.provider_catalog?.alpaca?'alpaca':''),(a.provider_catalog?.quantdata?'quant data':'')].some(v=>String(v||'').toLowerCase().includes(q));
 }).sort((a,b)=>String(a.symbol).localeCompare(String(b.symbol)));
 // The provider catalog can contain hundreds/thousands of ETFs. Keep every asset
 // searchable, but do not create thousands of DOM nodes before the user types.
 const total=rows.length,limit=q?320:160;rows=rows.slice(0,limit);
 root.innerHTML='';
 if(!rows.length){root.innerHTML='<div class="symbol-search-empty">No hay símbolos coincidentes en el catálogo activo.</div>';return;}
 rows.forEach(a=>{
   const b=document.createElement('button');b.type='button';b.className=`symbol-result ${a.full?'':'capability-gated'} ${a.symbol===activeSymbol?'active':''}`;b.dataset.symbol=a.symbol;
   const category=normalizeSymbolCategory(a),avatar=String(a.symbol||'?').slice(0,1),ready=Boolean(a.full);
   const providers=[a.provider_catalog?.alpaca?'ALPACA':'',a.provider_catalog?.quantdata?'QUANT DATA':''].filter(Boolean).join(' + ');
   b.innerHTML=`<span class="symbol-result-main"><span class="symbol-avatar">${avatar}</span><span><b class="symbol-result-symbol">${a.symbol}</b><small class="symbol-result-desc">${a.description||a.name||a.symbol}</small><em class="symbol-result-capability">${ready?'ANÁLISIS COMPLETO':'MERCADO DIRECTO · DERIVADOS PARCIALES'}${providers?` · ${providers}`:''}</em></span></span><span class="symbol-result-market"><b>${a.exchange||'—'}</b><small>${category.toLowerCase()}</small></span>`;
   b.title=ready?'Precio + cadena propia + análisis cuantitativo completo':(a.reason||'Precio/mercado directo; derivados permanecen capability-gated.');
   b.addEventListener('click',()=>{closeSymbolSearch();selectAsset(a);});root.appendChild(b);
 });
 if(total>rows.length){const more=document.createElement('div');more.className='symbol-search-empty';more.textContent=`${total-rows.length} símbolos adicionales · escribe para filtrar`;root.appendChild(more);}
}

async function loadAssetCatalog(force=false){
 const now=Date.now();if(!force&&assetCatalog.length>8&&now-Number(window.ITMQ_ASSET_CATALOG_AT||0)<300000)return assetCatalog;
 try{const r=await api('/api/assets/catalog');if(Array.isArray(r?.assets)&&r.assets.length){assetCatalog=r.assets;window.ITMQ_ASSET_CATALOG_AT=now;renderSymbolSearchResults();}}catch(e){console.warn('[ITM ASSET CATALOG]',e);}
 return assetCatalog;
}
function openSymbolSearch(){const m=el('symbolSearchModal');if(!m)return;m.classList.remove('hidden');renderSymbolSearchResults();loadAssetCatalog(true);requestAnimationFrame(()=>el('symbolSearchInput')?.focus());}
function closeSymbolSearch(){el('symbolSearchModal')?.classList.add('hidden');}
function renderAssetDock(s){
 const a=s?.asset||{};updateAssetLabels(s?.active_symbol||'DIA');const stateAssets=s.assets||[];if(!assetCatalog.length)assetCatalog=stateAssets;else{const merged=new Map(assetCatalog.map(x=>[x.symbol,x]));stateAssets.forEach(x=>merged.set(x.symbol,{...(merged.get(x.symbol)||{}),...x}));assetCatalog=[...merged.values()];}
 setText('activeAssetName',a.name||'—');setText('activeAssetKind',`${a.kind||'—'} · ${a.family||'—'}`);
 setText('symbolSearchCurrent',activeSymbol);setText('symbolSearchCurrentName',a.description||a.name||'Buscar símbolo');
 const dockAssets=(assetCatalog||[]).filter(x=>!x.dynamic||x.symbol===activeSymbol);
 const qs=el('quickAssetSelect');if(qs){const cur=activeSymbol;qs.innerHTML='';dockAssets.forEach(x=>{const o=document.createElement('option');o.value=x.symbol;o.textContent=`${x.symbol} · ${x.description||x.name||x.kind||''}`;qs.appendChild(o)});if([...qs.options].some(o=>o.value===cur))qs.value=cur;}
 const grid=el('assetGrid');if(grid){grid.innerHTML='';dockAssets.forEach(x=>{const b=document.createElement('button');b.className=`asset-chip ${x.symbol===activeSymbol?'active':''} ${x.full?'':'pending'}`;b.dataset.symbol=x.symbol;b.innerHTML=`<b>${x.symbol}</b><span>${x.kind||''}</span><small>${x.full?'ANÁLISIS LISTO':'MERCADO DIRECTO'}</small>`;b.title=x.reason||x.name||x.symbol;b.addEventListener('click',()=>selectAsset(x));grid.appendChild(b);});}
 const badge=el('assetDataStatus');if(badge)badge.textContent=a.full?'ANÁLISIS LISTO':'ANÁLISIS PARCIAL';
 // Ecosystem relations remain engine-only confluence. Legacy DOM ids are kept hidden for compatibility.
 setText('ecoEquities','CONFLUENCIA INTERNA');setText('ecoIndices','CONFLUENCIA INTERNA');setText('ecoFutures','CONFLUENCIA INTERNA');setText('ecoDerivatives','CONFLUENCIA INTERNA');setText('ecoFusionState','ENGINE ONLY');
 renderSymbolSearchResults();
}
const delay=ms=>new Promise(r=>setTimeout(r,ms));
function flushPendingQuantUI(){
 if(assetSwitchInProgress||assetQuantWarmup||activeSymbolEpoch<0)return;
 const main=pendingSurfaceMain,slice=pendingSurfaceSlice,charts=pendingCharts;
 pendingSurfaceMain=false;pendingSurfaceSlice=false;pendingCharts=false;
 if(main)loadSurfaceSliceFast('main');
 if(slice)loadSurfaceSliceFast('slice');
 if(charts)loadCharts();
}
async function hydrateSelectedAsset(target,epoch,switchSeq){
 const hydration=++assetHydrationSeq;
 const waits=[100,150,220,320,450,650,850,1100,1400,1800,2300,3000,4000,5000];
 for(const ms of waits){
   await delay(ms);
   if(hydration!==assetHydrationSeq||switchSeq!==assetSwitchSeq||target!==activeSymbol||Number(epoch)!==Number(activeSymbolEpoch))return false;
   try{
     const st=await api('/api/state');
     if(hydration!==assetHydrationSeq||switchSeq!==assetSwitchSeq)return false;
     if(String(st?.active_symbol||'').toUpperCase()!==target||Number(st?.symbol_epoch)!==Number(epoch))continue;
     renderState(st);
     if(st?.ready){
       assetQuantWarmup=false;
       setAssetLoading(false);
       // Heavy charts/tables are hydrated after Quant is ready and never block the click path.
       pendingCharts=true;flushPendingQuantUI();
       Promise.allSettled([loadTraceDates(),loadTablesIfNeeded()]);
       window.ITMQNextGen?.refresh?.();
       if(document.querySelector('.page-section.active')?.id==='section-surface')window.ITMQNextGen?.syncSurfaceControls?.();
       return true;
     }
     if(st?.warmup?.status==='DEGRADED'){
       assetQuantWarmup=false;setAssetLoading(false);
       showError(`${target}: motor cuantitativo en modo degradado. ${st?.warmup?.error||st?.error||'Reintenta Actualizar ahora.'}`);
       return false;
     }
   }catch(e){
     // Network polling must never freeze the UI; the next poll retries automatically.
     console.debug('[ITM ASSET HYDRATION]',target,e?.message||e);
   }
 }
 assetQuantWarmup=false;setAssetLoading(false);
 const b=el('assetDataStatus');if(b)b.textContent='PRECIO LIVE · ANÁLISIS EN PROCESO';
 return false;
}

async function selectAsset(asset){
 if(asset?.selectable===false){showError(`${asset.symbol}: ${asset.reason||'activo no seleccionable'}.`);return;}
 const target=String(asset?.symbol||'').toUpperCase();if(!target||target===activeSymbol)return;
 const switchSeq=++assetSwitchSeq;const previous=activeSymbol;const requiresQuant=Boolean(asset?.full);
 assetSwitchInProgress=true;assetQuantWarmup=requiresQuant;assetHydrationSeq++;
 chartRequestSeq++;surfaceMainRequestSeq++;surfaceSliceRequestSeq++;try{chartFetchController?.abort?.();}catch(_){}try{surfaceMainFetchController?.abort?.();}catch(_){}try{surfaceSliceFetchController?.abort?.();}catch(_){}chartFetchController=null;surfaceMainFetchController=null;surfaceSliceFetchController=null;pendingCharts=requiresQuant;pendingSurfaceMain=requiresQuant;pendingSurfaceSlice=requiresQuant;
 const btns=document.querySelectorAll('.asset-chip,.symbol-result');btns.forEach(b=>b.disabled=true);setAssetLoading(true,target);clearError();
 updateAssetLabels(target);activeSymbolEpoch=-1;window.ITMQ_ACTIVE_EPOCH=-1;liveTickCursor=0;tracePulseLast=null;
 window.ITMQNextGen?.beginSymbolSwitch?.(target);
 const tf=el('traceCandle')?.value||'1m';
 const bootstrapPromise=Promise.resolve(window.ITMQNextGen?.ensureTraceBootstrap?.(tf,target,'full_day'))
   .then(b=>{if(switchSeq!==assetSwitchSeq||!b||String(b?.symbol||'').toUpperCase()!==target)return null;window.ITMQNextGen?.prepareSymbolBootstrap?.(target,b);setText('assetLoadingTitle',b?.ready?`CARGANDO ${target}… precio listo · preparando análisis`:`CARGANDO ${target}… preparando histórico`);return b;})
   .catch(e=>{console.warn('[ITM MARKET BOOTSTRAP]',target,e);return null;});
 try{
   const r=await api(`/api/asset/select?symbol=${encodeURIComponent(target)}`,{method:'POST'});
   if(switchSeq!==assetSwitchSeq)return;
   if(String(r?.result?.symbol||'').toUpperCase()!==target)throw new Error(`SYMBOL COMMIT MISMATCH ${r?.result?.symbol||'—'} != ${target}`);
   activeSymbolEpoch=Number(r?.result?.symbol_epoch??r?.state?.symbol_epoch??0);window.ITMQ_ACTIVE_EPOCH=activeSymbolEpoch;
   assetSwitchInProgress=false;assetQuantWarmup=Boolean(r?.result?.progressive);
   renderState(r.state||{ready:false,loading:assetQuantWarmup,progressive_switch:assetQuantWarmup,active_symbol:target,symbol_epoch:activeSymbolEpoch,asset,assets:assetCatalog});
   btns.forEach(b=>b.disabled=false);setAssetLoading(false);
   bootstrapPromise.then(b=>{if(b&&switchSeq===assetSwitchSeq)window.ITMQNextGen?.prepareSymbolBootstrap?.(target,b,{historyOnly:true});});
   if(r?.result?.price_only){
     assetQuantWarmup=false;pendingCharts=false;pendingSurfaceMain=false;pendingSurfaceSlice=false;
     const badge=el('assetDataStatus');if(badge)badge.textContent='ANÁLISIS PARCIAL';
     // Price/history are allowed immediately. Quant option surfaces intentionally remain unavailable.
     setTimeout(()=>window.ITMQNextGen?.refresh?.(),0);
   }else{
     hydrateSelectedAsset(target,activeSymbolEpoch,switchSeq);
   }
 }catch(e){
   if(switchSeq!==assetSwitchSeq)return;assetSwitchInProgress=false;assetQuantWarmup=false;showError(`No pude cargar ${target}. ${e.message}`);updateAssetLabels(previous);await refreshState();
 }finally{
   if(switchSeq===assetSwitchSeq){assetSwitchInProgress=false;btns.forEach(b=>b.disabled=false);setAssetLoading(false);}
 }
}



function renderQuantWaitingState(s){
 const w=s?.warmup||{},ib=s?.instant_boot||{},pct=Math.max(0,Math.min(99,Number(w.progress_pct??ib.progress_pct??0)||0));
 const phase=String(w.phase||ib.stage||'ESPERANDO CADENA').replaceAll('_',' ');
 const err=String(w.error||s?.error||'').trim(),label=err?`AVISO · ${err.slice(0,78)}`:`CARGANDO · ${phase} · ${pct}%`;
 setText('dealerDataStatus',label);setText('positioningDataStatus',label);
 setText('dealerState',err?'WAITING / FAIL-SOFT':'WAITING');setText('dealerConfidence',err?'Precio/TRACE continúan; Dealer espera datos cuantitativos.':`${phase} · ${pct}% · esperando OPRA/OI/Greeks propios`);
 const wait=err?'FAIL-SOFT · PRECIO ACTIVO':`WARMING ${pct}%`;
 setText('dealerGex',wait);setText('dealerHedge15',wait);setText('dealerHedgeAccel',wait);setText('dealerSyntheticOi',wait);setText('dealerScenarioRange',wait);
 for(const id of ['callOi','putOi','pcOi2','topCallOi','topPutOi','netGex','grossGex','netDex'])setText(id,wait);
 const dh=el('dealerHedgeWindows');if(dh&&!dh.dataset.ready)dh.innerHTML=`<div class="muted">${err?'Quant temporalmente degradado;':'Cargando '+phase+';'} precio y TRACE no quedan bloqueados.</div>`;
}

function renderDealer(di){
 di=di||{};setText('dealerDataStatus',di?.ready===false?'ESPERANDO DATOS':'ACTUALIZADO · DATOS CUANTITATIVOS');const hp=di.hedge_pressure||{},shift=di.inventory_shift||{},fc=di.flow_confirmation||{},mq=di.microstructure_quality||{},pk=di.packages||{};
 const field=di.dealer_field||di.state||'WAITING';setText('dealerState',field);setText('dealerConfidence',`${di.confidence_label||'LOW'} · quality ${fmt(di.confidence,0)}/100 · ${di.inventory_contracts||0} contracts sintéticos`);
 setText('dealerGex',money(di.estimated_dealer_gex??di.synthetic_dealer_gex??0));setText('dealerHedge15',`${hp.direction||'NEUTRAL'} ${money(hp.net_15m||0)}`);setText('dealerHedgeConfidence',`model support ${fmt(hp.confidence,0)}/100 · counterparty ${fmt(hp.counterparty_share_assumption_pct,0)}%`);
 setText('dealerHedgeAccel',shift.state==='ACTIVE'?`${shift.score>=0?'+':''}${fmt(shift.score,0)}`:'WAITING');setText('dealerInventoryShiftDetail',shift.state==='ACTIVE'?`${compact(shift.hedge_equivalent_shares||0)} hedge-eq shares · ${shift.events||0} events`:'cambio sintético 15m');
 const soi=di.synthetic_oi||{};setText('dealerSyntheticOi',soi.synthetic_delta==null?'—':`${soi.synthetic_delta>=0?'+':''}${compact(soi.synthetic_delta)}`);setText('dealerSyntheticOiDetail',`official ${compact(soi.official_oi||0)} → synthetic ${compact(soi.synthetic_oi||0)}`);
 setText('dealerScenarioRange',fc.state||'UNAVAILABLE');setText('dealerFlowConfirmDetail',fc.state?`${fc.estimated_hedge_direction||'NEUTRAL'} hedge · alignment ${fmt(fc.alignment,0)}/100 · futures ${fc.futures_available?'LIVE':'NO FEED'}`:'underlying/futures when available');
 setText('dealerNbboSync',`${fmt(mq.quote_sync_coverage_pct,0)}% / ${fmt(mq.underlying_sync_coverage_pct,0)}%`);setText('dealerAggressorQuality',`${fmt(mq.high_conf_aggressor_pct,0)}%`);const counts=pk.counts||{};setText('dealerPackages',`${counts.SWEEP_CANDIDATE||0} SW · ${counts.BLOCK_CANDIDATE||0} BL · ${counts.MULTI_LEG_CANDIDATE||0} ML`);setText('dealerStateScore',di.dealer_state_score==null?'—':`${Number(di.dealer_state_score)>=0?'+':''}${fmt(di.dealer_state_score,0)}/100`);
 setText('cmdHedgePressure',`${hp.direction||'NEUTRAL'} ${money(hp.net_15m||0)}`);setText('cmdHedgeDetail',`dealer ${field} · quality ${fmt(di.confidence,0)}/100`);setText('traceDealerGex',money(di.estimated_dealer_gex??di.synthetic_dealer_gex??0));setText('traceHedge1',`${hp.windows?.['1']?.direction||'NEUTRAL'} ${money(hp.windows?.['1']?.net_notional||0)}`);setText('traceHedge5',`${hp.windows?.['5']?.direction||'NEUTRAL'} ${money(hp.windows?.['5']?.net_notional||0)}`);setText('traceHedge15',`${hp.windows?.['15']?.direction||'NEUTRAL'} ${money(hp.windows?.['15']?.net_notional||0)}`);
 const om=di.opening_model||{},gr=di.gex_reconciliation||{};setText('dealerGreeksValuation',di.greeks_valuation||'—');setText('dealerGreeksRepriced',`repreciado ${fmt(di.greeks_repriced_pct,0)}% del inventario usable`);setText('dealerOpeningModel',om.source||'HEURISTIC PRIOR');const sf=om.shadow_fit||{};setText('dealerOpeningDetail',sf.status==='FITTED'?`SHADOW · R² ${sf.test_r2==null?'—':fmt(sf.test_r2,2)} · MAE ${sf.test_mae==null?'—':fmt(sf.test_mae,3)} vs ${sf.baseline_mae==null?'—':fmt(sf.baseline_mae,3)}`:`SHADOW · ${sf.reason||sf.status||'recolectando sesiones'}`);setText('dealerGexAgreement',gr.agreement||'—');setText('dealerGexAgreementDetail',`Structural ${gr.structural_gex==null?'—':money(gr.structural_gex)} · Dealer ${gr.estimated_dealer_gex==null?'—':money(gr.estimated_dealer_gex)} · cobertura ${fmt(gr.flow_inventory_coverage_pct,0)}%`);
 const w=el('dealerHedgeWindows');if(w){w.innerHTML='';Object.values(hp.windows||{}).sort((a,b)=>a.minutes-b.minutes).forEach(r=>{const d=document.createElement('div');d.className='change-item';d.innerHTML=`<span>${r.minutes} MIN</span><b>${r.direction} ${money(r.net_notional||0)}</b><small>${r.events||0} eventos · gross ${money(r.gross_notional||0)} · conf ${fmt(r.confidence,0)}</small>`;w.appendChild(d)});if(!w.children.length)w.innerHTML='<div class="muted">Esperando OPRA LIVE.</div>';}
 const ts=el('dealerTopStrikes');if(ts){ts.innerHTML='';(hp.top_strikes||[]).forEach(r=>{const d=document.createElement('div');d.className='change-item';d.innerHTML=`<span>${fmt(r.strike)}</span><b>${r.direction} ${money(r.net_notional||0)}</b><small>${r.events||0} eventos · conf ${fmt(r.confidence,0)}</small>`;ts.appendChild(d)});if(!ts.children.length)ts.innerHTML='<div class="muted">Sin presión suficiente.</div>';}
 const tb=el('dealerInventoryTable');if(tb){tb.innerHTML='';(di.inventory||[]).forEach(r=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${fmt(r.strike)}</td><td>${fmt(r.dealer_contracts,1)}</td><td>${compact(r.dealer_delta_shares)}</td><td>${money(r.dealer_gex)}</td><td>${compact(r.dealer_vanna_exposure)}</td><td>${compact(r.dealer_charm_exposure)}</td><td>${money(r.hedge_to_neutral_notional)}</td><td>${fmt(r.confidence,0)}</td>`;tb.appendChild(tr)});if(!tb.children.length)tb.innerHTML='<tr><td colspan="8">Inventario sintético aún sin suficientes eventos LIVE.</td></tr>';}
}

function renderInfrastructure(s){
 const sh=s.source_health||{}, rs=s.research_storage||{},pr=s.provider_runtime||{};const fs=sh.fusion_sources||[];const allSrc=[...(sh.sources||[]),...fs];const sip=(sh.sources||[]).find(x=>x.name==='ALPACA SIP')||{};const opra=(sh.sources||[]).find(x=>x.name==='ALPACA OPRA REST')||{};const qd=fs.find(x=>x.name==='QUANTDATA')||pr.quantdata||{};const tt=pr.tastytrade||{};setText('infraFutures',sip.status||'WAITING');setText('infraFuturesDetail',sip.detail||'SIP market-data provider');setText('infraIndex',opra.status||'WAITING');setText('infraIndexDetail',opra.detail||'OPRA options-data provider');setText('infraSecondary',qd.status||((qd.configured||qd.running)?'READY':'WAITING'));setText('infraSecondaryDetail',qd.detail||'Quant Data · options intelligence · corroboración');const ttLive=String(tt.market_data||'').toUpperCase()==='LIVE'||String(tt.dxlink||'').toUpperCase()==='CONNECTED';setText('infraTasty',ttLive?'LIVE':(tt.configured?'WARMING':'NOT CONFIGURED'));setText('infraTastyDetail',tt.configured?`DXLink ${tt.dxlink||'—'} · Market ${tt.market_data||'—'} · contratos ${tt.derivative_contracts_selected??tt.subscriptions??0}`:'Configura OAuth read-only');setText('infraResearch',rs.backend||'—');setText('infraResearchDetail',`${rs.cycles||0} cycles · ${rs.sessions||0} sessions`);
 const runtimeRows=[{name:'TASTYTRADE DXLINK',status:ttLive?'LIVE':(tt.configured?'WARMING':'NOT CONFIGURED'),detail:tt.configured?`OAuth ${tt.oauth||tt.auth||'—'} · DXLink ${tt.dxlink||'—'} · market ${tt.market_data||'—'} · dropped ${tt.dropped_events??0}`:'Sin credenciales activas'}];const statusRows=[...allSrc,...runtimeRows];const bad=statusRows.filter(x=>['DEGRADED','WAITING','DISAGREEMENT','ERROR','STALE','INVALID','WARMING'].includes(String(x.status).toUpperCase())&&!['EXPECTED_IDLE','STRUCTURAL'].includes(String(x.status).toUpperCase())).length;setText('cmdSourceHealth',bad?`${bad} AVISOS`:'OK');setText('cmdSourceHealthDetail',`${statusRows.length} fuentes/canales observados · política sin rango fijo`);
 const root=el('sourceHealthGrid');if(root){root.innerHTML='';[...statusRows,...(sh.components||[]).map(x=>({name:x.component,status:x.status,detail:x.detail}))].forEach(r=>{const d=document.createElement('div');d.className='change-item';d.innerHTML=`<span>${r.name||r.key||'FUENTE'}</span><b>${r.status||'—'}</b><small>${r.detail||''}</small>`;root.appendChild(d)});if(!root.children.length)root.innerHTML='<div class="muted">Esperando matriz de fuentes.</div>';}
}

let providerFlowBusy=false,providerFlowLastAt=0;
async function refreshProviderFlowHealth(force=false){
 if(providerFlowBusy)return;const now=Date.now();if(!force&&now-providerFlowLastAt<4000)return;
 providerFlowBusy=true;providerFlowLastAt=now;
 try{
  const r=await fetch(`/api/providers/flow-health?symbol=${encodeURIComponent(activeSymbol)}`,{cache:'no-store'});
  const x=r.ok?await r.json():null;if(!x)throw new Error(`HTTP ${r.status}`);
  const price=x.price||{},opt=x.options||{},diag=x.runtime?.option_diagnostics||{},qd=x.runtime?.quantdata||{};
  const issues=Array.isArray(x.issues)?x.issues:[],advisories=Array.isArray(x.advisories)?x.advisories:[],clock=x.session_expectation||price.session_expectation||{};
  const systemState=x.state||'CLEAR',systemCard=el('providerFlowBottleneck')?.closest('.hero-card');if(systemCard)systemCard.dataset.health=systemState;
  setText('providerFlowBottleneck',systemState==='CLEAR'?'CLEAR':systemState==='STRUCTURAL'?'STRUCTURAL':systemState==='EXPECTED_IDLE'?'EXPECTED IDLE':`${issues.length} ERROR${issues.length===1?'':'ES'}`);
  setText('providerFlowBottleneckDetail',issues.length?issues.slice(0,3).join(' · '):`${String(clock.session_phase||'SESSION').replaceAll('_',' ')} · ${clock.detail||x.redundancy_state||'—'}${advisories.length?` · ${advisories.length} advisory`:''}`);
  const pp=price.providers_live||[];setText('providerFlowPrice',price.canonical_source||price.selected_source||(price.state==='STRUCTURAL'?'STRUCTURAL':price.state==='EXPECTED_IDLE'?'EXPECTED IDLE':'WAITING'));setText('providerFlowPriceDetail',pp.length?`${pp.length} fuente${pp.length===1?'':'s'} fresca${pp.length===1?'':'s'} · ${pp.join(' + ')}`:`${String(price.state||'WAITING').replaceAll('_',' ')} · ${clock.detail||'sin precio fresco requerido'}`);
  const op=opt.live_trade_sources||[];setText('providerFlowOptions',opt.selected_source||opt.state||'WAITING');setText('providerFlowOptionsDetail',`${op.length} tape${op.length===1?'':'s'} LIVE · ${op.join(' + ')||opt.bottleneck||'esperando'}`);
  setText('providerFlowPrints',diag.prints_5m??0);setText('providerFlowPrintsDetail',`${diag.selected_source||opt.selected_source||'—'} · ${diag.status||'WAITING'}`);
  setText('providerFlowContracts',diag.contracts??0);setText('providerFlowContractsDetail',`${(opt.providers_with_universe||[]).join(' + ')||'sin universo'}`);
  const qdActive=!!qd.running||!!qd.last_success;
  setText('providerFlowQuantData',qdActive?'ACTIVE':(qd.configured?'WAITING':'OFF'));
  setText('providerFlowQuantDataDetail',qdActive?`FEATURE_BUS · ${qd.available_components??'—'} componentes`:'options intelligence · corroboración');
 }catch(e){setText('providerFlowBottleneck','HEALTH ERROR');setText('providerFlowBottleneckDetail',String(e?.message||e).slice(0,140));}
 finally{providerFlowBusy=false;}
}
window.ITMQProviderFlowHealth={refresh:refreshProviderFlowHealth};

function renderNextgenIntelligence(s){
 const mt=s.market_truth||s.provider_runtime?.market_truth||{},fi=s.feature_intelligence||{},di=s.decision_intelligence||{},rv=s.research_validation||{},ca=mt.causality||s.provider_runtime?.causality||{},tt=s.temporal_truth||mt.temporal_truth||{},ei=s.expiry_intelligence||{},si=s.structural_intelligence||{},dx=s.derivatives_intelligence||{};
 setText('opTruthNext',mt.truth_confidence==null?'—':`${fmt(mt.truth_confidence,0)}/100`);setText('opTruthDetail',mt.ready?`${mt.provider_diversity||0} proveedores utilizables · desacuerdo ${fmt(mt.max_price_disagreement_pct,3)}%`:'Esperando observaciones comparables');
 const late=Number(ca.late_after_watermark||0),ordered=Number(ca.ordered_emitted||0);setText('opCausalityNext',ca.ready?(late?'QUARANTINE':'ORDERED'):'WAITING');setText('opCausalityDetail',ca.ready?`${ordered} ordenados · ${late} late · ventana ${fmt(ca.max_lateness_ms,0)} ms`:'Esperando event-time');
 setText('opFeatureNext',fi.scanner_alignment==null?'—':`${fmt(fi.scanner_alignment,0)}/100`);setText('opFeatureDetail',fi.ready?`${fi.context_direction||'NEUTRAL'} · ${fi.mode||'SHADOW'}`:'Sin canales contextuales suficientes');
 const sp=di.scenario_probability||{},p=Number(sp.p_target_before_invalidation);setText('opProbNext',Number.isFinite(p)?`${fmt(p*100,1)}%`:'—');setText('opProbDetail',Number.isFinite(p)?`${sp.status||'CALIBRATED'} · T1 antes de invalidación`:`${sp.status||rv.status||'COLLECTING'} · no se inventa probabilidad`);
 const ev=Number(di.expected_value_r);setText('opEvNext',Number.isFinite(ev)?`${ev>=0?'+':''}${fmt(ev,2)}R`:'—');setText('opEvDetail',Number.isFinite(ev)?`Scanner ${di.direction||'—'} · ${di.edge_state||'—'}`:'Gate EV esperando calibración OOS');
 const ch=Array.isArray(di.what_changed)?di.what_changed:[];setText('opChangeNext',ch.length?`${ch.length} CAMBIO${ch.length===1?'':'S'}`:'ESTABLE');setText('opChangeDetail',ch.length?ch.slice(0,2).join(' · '):'Sin cambio material medible entre ciclos');
 const ph=tt.provider_health||[],warnings=ph.filter(x=>String(x.status||'').toUpperCase()!=='LIVE').length;setText('opDataHealthNext',tt.ready?(warnings?`${warnings} AVISOS`:'OK'):'WAITING');setText('opDataHealthDetail',tt.ready?`${tt.synchronized_market_observations||0} sync · ${tt.critical_stale_channels?.length||0} canales stale`:'salud por proveedor + canal');
 const exps=ei.actual_expirations||[];const tg=ei.top_gamma_expiration||{};setText('opExpiryNext',ei.ready?`${exps.length} EXP`:'WAITING');setText('opExpiryDetail',ei.ready?`Top Γ ${tg.expiration||'—'} · ${fmt(tg.gamma_concentration_pct,1)}%`:'fechas reales · no alias zero/one');
 const px=si.proximity||{};setText('opStructuralNext',si.ready?`${fmt(px.structural_density,0)}/100`:'WAITING');setText('opStructuralDetail',si.ready?`${px.confluence_count||0} niveles dentro de 1%`:'proximidad estructural');
 const dd=dx.directional||{};setText('opDerivativesNext',dx.ready?`${dd.direction||'NEUTRAL'} ${fmt(dd.confidence,0)}`:'WAITING');const div=dx.divergence||{},divs=Object.values(div).filter(x=>x?.sign_agreement===false).length;setText('opDerivativesDetail',dx.ready?`MODEL/OBS · ${divs} divergencia${divs===1?'':'s'}`:'GEX/DEX/Convexity/Vanna/Charm');
}


let operativaView='board';
function setOperativaView(mode){
 operativaView=mode==='asset'?'asset':'board';
 const bv=el('opBoardView'),av=el('opAssetView'),bb=el('opViewBoard'),ba=el('opViewAsset');
 if(bv)bv.classList.toggle('hidden',operativaView!=='board'); if(av)av.classList.toggle('hidden',operativaView!=='asset');
 if(bb){bb.classList.toggle('btn-primary',operativaView==='board');bb.classList.toggle('btn-ghost',operativaView!=='board');}
 if(ba){ba.classList.toggle('btn-primary',operativaView==='asset');ba.classList.toggle('btn-ghost',operativaView!=='asset');}
}
function boardAge(r){if(r?.age_seconds==null)return 'SIN DATO';return `${Math.round(Number(r.age_seconds))}s · ${r.freshness||'—'}`;}
function renderBoard(b){
 if(!b)return; setText('boardActionable',b.counts?.ACTIONABLE??0);setText('boardCalibrated',`${b.calibrated_models??0} / ${b.assets??0}`);setText('boardEvActive',`${b.ev_gate_active??0} / ${b.assets??0}`);setText('boardStale',b.stale_rows??0);setText('boardNote',b.note||'');setText('boardRefreshState',`${b.live_rows??0}/${b.assets??0} comparables · escalonado`);
 const body=el('boardBody');if(!body)return;body.innerHTML=''; let staleHeader=false;
 (b.rows||[]).forEach(r=>{if(r.freshness==='OBSOLETO'&&!staleHeader){const h=document.createElement('tr');h.className='board-separator';h.innerHTML='<td colspan="8">DATOS OBSOLETOS · FUERA DEL RANKING OPERATIVO</td>';body.appendChild(h);staleHeader=true;}
  const tr=document.createElement('tr');tr.className=`board-row ${r.freshness==='OBSOLETO'?'stale':''}`;tr.dataset.symbol=r.symbol||'';
  const gate=r.ev_gate_active?'EV ACTIVE':(r.model_calibrated?'CALIBRATED · OFF':'LEGACY'); const edge=r.ev_gate_active&&r.expected_value_r!=null?`${Number(r.expected_value_r)>=0?'+':''}${fmt(r.expected_value_r,2)}R`:`Evid ${fmt(r.evidence_score,0)}`;
  const zone=(r.zone_low==null||r.zone_high==null)?'—':`${fmt(r.zone_low)}–${fmt(r.zone_high)}`;
  tr.innerHTML=`<td><b>${r.symbol||'—'}</b></td><td>${r.direction||'—'}${r.scenario_type?` · ${r.scenario_type}`:''}</td><td>${r.edge_state||'SIN SEÑAL'}</td><td>${gate}</td><td>${edge}</td><td>${zone}</td><td>${fmt(r.contender_gap,1)}</td><td>${boardAge(r)}</td>`;
  tr.addEventListener('click',()=>{const a=assetCatalog.find(x=>x.symbol===r.symbol);if(a&&a.full){setOperativaView('asset');selectAsset(a);}});body.appendChild(tr);
 }); if(!body.children.length)body.innerHTML='<tr><td colspan="8" class="muted">El Board se poblará de forma escalonada.</td></tr>';
}
async function refreshBoard(){const sec=activeSectionId();if(sec!=='section-command'&&sec!=='section-scanner')return;if(lastState?.replay?.mode&&lastState.replay.mode!=='LIVE')return;try{const b=await api('/api/board/refresh',{method:'POST'});renderBoard(b);}catch(e){setText('boardRefreshState','Board: '+e.message)}}
function renderReplay(s){
 const r=s?.replay||{mode:'LIVE',banner:'EN VIVO'};const mode=String(r.mode||'LIVE').toUpperCase();const active=mode!=='LIVE';
 document.body.classList.toggle('replay-active',active);const bar=el('globalReplayBar');if(bar)bar.dataset.mode=mode;
 setText('replayBanner',active?`◉ ${r.banner||mode}`:'● EN VIVO');setText('replayContextDetail',r.note||'HOY usa LIVE. Una fecha histórica cambia todos los módulos al mismo reloj causal.');
 const bb=el('opViewBoard');if(bb){bb.disabled=active;bb.title=active?'Board desactivado en Replay: no compara activos sin reconstruirlos al mismo reloj.':'';}
 if(active&&operativaView==='board')setOperativaView('asset');
 const cal=el('replayDateCalendar');if(cal){cal.value=r.date||localDateISO();cal.max=localDateISO();}
 if(r.date&&el('traceDate'))el('traceDate').value=r.date;
 if(!active){stopReplayPlayback();configureReplayClock(null);setText('replayTimelineLabel','LIVE');}
 else if(replayClockMarks.length&&r.asof){configureReplayClock({marks:replayClockMarks},r.asof);}
}
async function applyReplaySession(asofISO=null,dateOverride=null){
 const d=String(dateOverride||el('replayDateCalendar')?.value||el('traceDate')?.value||'').slice(0,10);if(!d){showError('Selecciona una fecha histórica.');return;}
 if(d===localDateISO()){await exitReplay();return;}
 preferReadyChartCache=true;const q=`/api/replay/set?date=${encodeURIComponent(d)}${asofISO?`&asof=${encodeURIComponent(asofISO)}`:''}`;
 try{const r=await api(q,{method:'POST'});configureReplayClock(r?.clock,r?.context?.asof||asofISO);liveTickCursor=0;await fullRefresh();setText('instantCalendarStatus',`${d} · HISTÓRICO`);return r;}catch(e){stopReplayPlayback();showError(e.message);throw e;}
}
async function applyReplayClockIndex(idx){
 if(!replayClockMarks.length||replayPlaybackBusy)return;const next=Math.max(0,Math.min(replayClockMarks.length-1,Number(idx)||0));replayClockIndex=next;const slider=el('replayTimeline');if(slider)slider.value=String(next);setText('replayTimelineLabel',replayDisplayMark(replayClockMarks[next]));replayPlaybackBusy=true;try{await applyReplaySession(replayClockMarks[next]);}finally{replayPlaybackBusy=false;}
}
async function stepReplay(steps){if(!replayClockMarks.length)return;stopReplayPlayback();await applyReplayClockIndex(replayClockIndex+Number(steps||0));}
function toggleReplayPlayback(){
 if(replayPlaybackTimer){stopReplayPlayback();return;}if(!replayClockMarks.length)return;const b=el('replayPlayPause');if(b)b.textContent='⏸ PAUSE';
 replayPlaybackTimer=setInterval(async()=>{if(replayPlaybackBusy)return;const speed=Math.max(1,Number(el('replaySpeed')?.value||1));const next=Math.min(replayClockMarks.length-1,replayClockIndex+speed);if(next<=replayClockIndex){stopReplayPlayback();return;}await applyReplayClockIndex(next);if(next>=replayClockMarks.length-1)stopReplayPlayback();},900);
}
async function exitReplay(){
 stopReplayPlayback();const b=el('replayLiveBtn');if(b)b.disabled=true;preferReadyChartCache=true;try{await api('/api/replay/live',{method:'POST'});replayClockMarks=[];replayClockIndex=0;liveTickCursor=0;await fullRefresh();const d=localDateISO();if(el('replayDateCalendar'))el('replayDateCalendar').value=d;configureReplayClock(null);setText('instantCalendarStatus','HOY · LIVE');}catch(e){showError(e.message)}finally{if(b)b.disabled=false;}
}
function renderResearchRange(x){const r=x?.range||x;if(!r)return;setText('rangeSessions',`${r.sessions_usable??0} / ${r.sessions_found??0}`);const sc=r.sample_coverage||{};setText('rangeSignals',`${sc.signals_logged??0} / ${sc.signals_required??120}`);setText('rangeTape',`${r.sessions_with_tape??0} / ${r.sessions_usable??0}`);const m=r.current_calibration_model||{};setText('rangeModel',m.stage||m.status||'COLLECTING');const d=el('rangeDropped');if(d){const rows=r.dropped_detail||[];d.textContent=rows.length?`Descartadas: ${rows.map(v=>`${v.date} · ${v.coverage_class||'INSUFFICIENT'} · ${v.reason||v.coverage_pct+'% cobertura'}`).join(' | ')}`:`Sin sesiones descartadas en el rango. ${r.note||''}`;}}
async function requestResearchRange(){const a=el('researchStart')?.value||'',b=el('researchEnd')?.value||'';if(!a||!b){showError('Selecciona fecha inicial y final para evaluar el rango.');return;}const btn=el('researchRangeBtn');if(btn)btn.disabled=true;try{const r=await api(`/api/replay/sessions?start=${encodeURIComponent(a)}&end=${encodeURIComponent(b)}`);renderResearchRange(r);}catch(e){showError(e.message)}finally{if(btn)btn.disabled=false;}}
function renderEasyMode(easy){
 const panel=el('easyPanel'),blocks=el('easyBlocks'),banner=el('easyBanner');if(!panel||!blocks)return;easy=easy||{};
 if(banner){banner.hidden=!easy.banner;banner.textContent=easy.banner||'';}
 blocks.innerHTML='';(easy.blocks||[]).forEach(group=>{const section=document.createElement('section');section.className='easy-group';const h=document.createElement('h2');h.textContent=group.title||'';section.appendChild(h);const grid=document.createElement('div');grid.className='easy-card-grid';
  (group.cards||[]).forEach(card=>{const a=document.createElement('article');a.className=`easy-card tone-${card.tone||'neutral'}`;const l=document.createElement('span');l.textContent=card.label||'';const v=document.createElement('strong');v.textContent=card.value||'—';const m=document.createElement('p');m.textContent=card.meaning||'';a.append(l,v,m);if(card.caveat){const c=document.createElement('small');c.textContent=card.caveat;a.appendChild(c);}grid.appendChild(a);});section.appendChild(grid);blocks.appendChild(section);});
 setText('easyNote',easy.note||'Modo Fácil traduce el mismo motor; no crea una señal paralela.');
}
function researchItems(id,items,empty='Esperando muestra suficiente.'){const root=el(id);if(!root)return;root.innerHTML='';(items||[]).forEach(r=>{const d=document.createElement('div');d.className='change-item';const s=document.createElement('span');s.textContent=r.label||'MÉTRICA';const b=document.createElement('b');b.textContent=r.value??'—';const sm=document.createElement('small');sm.textContent=r.detail||'';d.append(s,b,sm);root.appendChild(d);});if(!root.children.length){const d=document.createElement('div');d.className='muted';d.textContent=empty;root.appendChild(d);}}
function renderInstitutionalResearch(r){r=r||{};setText('researchAuthority',r.authority||'Scanner LIVE sin cambios');
 const vf=r.vol_forecast||{},mi=r.microstructure||{},ca=r.cross_asset||{},ml=r.ml||{};setText('researchVolState',vf.state||'COLLECTING');setText('researchMicroState',mi.state||'COLLECTING');setText('researchCrossState',ca.state||'COLLECTING');setText('researchMlState',ml.state||'COLLECTING');
 const vm=(vf.models||[]).map(x=>({label:x.name,value:x.forecast_vol_pct==null?(x.status||'COLLECTING'):`${fmt(x.forecast_vol_pct,2)}%`,detail:`${x.status||'SHADOW'}${x.samples?` · ${x.samples} muestras`:''}${x.reason?` · ${x.reason}`:''}`}));if(vf.reason)vm.push({label:'COBERTURA',value:'COLLECTING',detail:vf.reason});researchItems('researchVolGrid',vm);
 const micro=[{label:'MICROPRICE L1',value:mi.microprice==null?'—':fmt(mi.microprice,4),detail:'ponderación bid/ask por tamaño L1'},{label:'IMBALANCE L1',value:mi.book_imbalance_l1==null?'—':`${fmt(Number(mi.book_imbalance_l1)*100,1)}%`,detail:'contexto de microestructura; no vota dirección'},{label:'FLUJO FIRMADO',value:mi.signed_flow_ratio==null?'—':`${fmt(Number(mi.signed_flow_ratio)*100,1)}%`,detail:`${intfmt(mi.samples||0)} eventos observados`},{label:'EXCITACIÓN',value:mi.hawkes_excitation_proxy==null?'COLLECTING':fmt(mi.hawkes_excitation_proxy,2),detail:'proxy Hawkes-style; no es un Hawkes calibrado'},{label:'PROFUNDIDAD L2',value:mi.l2_status||mi.l2_metrics?.status||'WAITING FOR L2',detail:'L5/L10, cancelaciones, colas y resiliencia requieren feed real'}];researchItems('researchMicroGrid',micro);
 const pair=ca.pair||{},pca=ca.pca||{};const cross=[];if(ca.reason)cross.push({label:'COBERTURA',value:'COLLECTING',detail:ca.reason});if(ca.common_sessions!=null)cross.push({label:'SESIONES COMUNES',value:intfmt(ca.common_sessions),detail:`familia ${ca.family||'—'}`});if(pca.first_component_explained_pct!=null)cross.push({label:'FACTOR COMÚN PCA',value:`${fmt(pca.first_component_explained_pct,1)}%`,detail:'varianza explicada por el primer componente'});if(pair.peer)cross.push({label:`RELACIÓN ${pair.peer}`,value:pair.rolling_correlation==null?'—':fmt(pair.rolling_correlation,2),detail:`beta dinámica ${pair.kalman_beta==null?'—':fmt(pair.kalman_beta,3)} · residual z ${pair.residual_z==null?'—':fmt(pair.residual_z,2)}`});if(pair.cointegration_adf_t_proxy!=null)cross.push({label:'COINTEGRACIÓN',value:fmt(pair.cointegration_adf_t_proxy,2),detail:'diagnóstico residual; no señal operativa'});researchItems('researchCrossGrid',cross);
 const mlItems=[{label:'MUESTRAS',value:`${intfmt(ml.samples||0)} / ${intfmt(ml.minimum_samples||500)}`,detail:'historial LIVE requerido antes de entrenar'},{label:'SESIONES',value:`${intfmt(ml.sessions||0)} / ${intfmt(ml.minimum_sessions||20)}`,detail:'mínimo de diversidad temporal'},{label:'CANDIDATOS',value:(ml.candidates||[]).join(' · ')||'—',detail:'solo Research/SHADOW'},{label:'PRODUCCIÓN',value:ml.production_enabled?'ACTIVA':'BLOQUEADA',detail:'Calibration/OOS debe autorizar cualquier promoción'}];researchItems('researchMlGrid',mlItems);
}
// v1.40.8 · freshness remains an INTERNAL fail-closed gate.
// Analyst UI intentionally does not render stale-feed popups, ribbons or chart badges.
function renderFreshnessGate(s){
 const gate=s?.publication_gate||s?.data_quality_report?.circuito_frescura||{};
 const replayMode=String(s?.replay?.mode||'LIVE').toUpperCase();
 const clock=gate?.session_expectation||s?.price_stream?.session_expectation||{};
 const expectedLive=clock?.expected_price_live===true;
 publicationBlocked=gate?.publicar_permitido!==true && replayMode==='LIVE';
 publicationCritical=publicationBlocked&&expectedLive;
 // Safety state is retained for execution/publication logic, but never painted as a
 // modal, red banner, pseudo-badge or overlay on analytical charts.
 document.body.classList.remove('publication-blocked','publication-structural');
 const old=el('freshnessGateBanner'); if(old) old.remove();
 return !publicationBlocked;
}
function renderState(s){
 lastState=s;renderFreshnessGate(s);if(s?.active_symbol){activeSymbolEpoch=Number(s.symbol_epoch??activeSymbolEpoch??0);window.ITMQ_ACTIVE_EPOCH=activeSymbolEpoch;}renderReplay(s);renderExpiryWindow(s?.expiry_window,s?.zero_dte_status);renderBoard(s?.board);if(s?.active_symbol)renderAssetDock(s);
 // INSTANT BOOT: publish everything that is already true before the heavy Quant state exists.
 // Price, memory and TRACE readiness must never be hidden behind Gamma/Scanner hydration.
 const ib=s?.instant_boot||{},ps=s?.price_stream||{};const priceReady=Boolean(ib.price_ready||s?.spot!=null||ps?.last_tick?.price!=null);
 setText('marketMode',s?.mode||'LIVE');setText('marketSource',`${s?.meta?.stock_feed||ps?.feed||ps?.source||'PRICE STREAM'} + ${s?.meta?.option_feed||'QUANT WARMING'} · ${s?.meta?.market_state||'LIVE'} · PRICE ${priceReady?'TICK LIVE':'CONNECTING'}`);
 setText('spot',fmt(s?.spot??ps?.last_tick?.price));setText('routeCurrent',fmt(s?.spot??ps?.last_tick?.price));
 if(s?.persistent_memory?.policy){setText('memoryState','MEMORIA · PROTEGIDA');setText('opMemoryTop','PERSISTENTE');}else if(ib.memory_ready){setText('memoryState','MEMORIA · LISTA');}
 if(!s.ready){assetQuantWarmup=Boolean(s?.progressive_switch||s?.warmup?.active);const pct=Math.max(0,Math.min(99,Number(s?.warmup?.progress_pct??ib.progress_pct??15)||15));const phase=String(s?.warmup?.phase||ib.stage||'STARTING').replaceAll('_',' ');
   if(assetQuantWarmup){clearError();setAssetLoading(false);const b=el('assetDataStatus');if(b)b.textContent=priceReady?`PRECIO LIVE · ANÁLISIS ${pct}%`:`CONECTANDO · ANÁLISIS ${pct}%`;const ex=el('expiryUsed');if(ex)ex.textContent=`${priceReady?'PRECIO/TRACE DISPONIBLE':'PRECIO CONECTANDO'} · ANÁLISIS EN SEGUNDO PLANO`;setText('lastRefresh',`ANÁLISIS · ${pct}%`);}
   else if(s?.loading){clearError();setAssetLoading(true,s.active_symbol);}else showError(s.error||'Motor no listo');renderQuantWaitingState(s);return;}assetQuantWarmup=false;clearError();renderAssetDock(s);if(s?.instant_boot?.background_enrichment){const b=el('assetDataStatus');if(b)b.textContent=`SCANNER LISTO · COMPLETANDO CONTEXTO ${Math.max(82,Number(s.instant_boot.progress_pct||82))}%`;}
 setText('marketMode',s.mode); setText('marketSource',`${s.meta?.stock_feed||'—'} + ${s.meta?.option_feed||'—'} · ${s.meta?.market_state||'—'} · PRICE ${s.price_stream?.connected?'TICK LIVE':'WAIT'}`); setText('dataAge',s.data_age_seconds==null?'—':`edad ${Math.round(s.data_age_seconds)}s`); setText('lastRefresh',s.last_refresh_ec?new Date(s.last_refresh_ec).toLocaleString():'—');
 const cmd=s.command||{},cz=cmd.zone||{};
 setText('spot',fmt(s.spot)); setText('regime',cmd.market_regime); setText('bias',cmd.bias||'WAITING');
 setText('biasScore',cmd.direction_source==='SCANNER'?`zona ${fmt(cz.low)}–${fmt(cz.high)} · SCANNER ES LA ÚNICA AUTORIDAD`:'Esperando Scanner');
 setText('cmdActionability',cmd.actionability_state||'WAITING');setText('cmdActionabilityDetail',`${cmd.actionability_source||'—'}${cmd.probability_status==='CALIBRATED'&&cmd.probability_t1_first!=null?` · P(T1) ${fmt(Number(cmd.probability_t1_first)*100,1)}%`:''}`);
 setText('cmdTapeTiming',cmd.tape_state||'WAITING');setText('cmdTapeTimingDetail',cmd.tape_progress_pct==null?'TIMING ONLY · esperando zona':`${fmt(cmd.tape_progress_pct,0)}% · ${cmd.tape_seconds_remaining==null?'—':fmt(cmd.tape_seconds_remaining,0)+'s restantes'} · TIMING ONLY`);
 setText('cmdContextRegime',`Gamma ${cmd.gamma_regime||'—'} · Vol ${cmd.vol_regime||'—'}`);setText('cmdContextDetail',`${cmd.flip_context||'FLIP —'} · CONTEXTO, NO VOTA`);
 setText('gammaCenter',fmt(s.gamma_center)); setText('gammaMigration',`${s.gamma_migration?.direction||'—'} ${fmt(s.gamma_migration?.strength,0)}/100`); setText('gammaFlip',s.gamma_flip==null?'—':`≈ ${fmt(s.gamma_flip,1)}`); setText('flipDynamics',`${s.gamma_flip_direction||'—'} · ${s.gamma_flip_velocity||'—'}`); setText('deltaCenter',fmt(s.delta_center)); setText('deltaMigration',`${s.delta_migration?.direction||'—'} ${fmt(s.delta_migration?.strength,0)}/100`);
 const dq=s.data_quality_report||{}, mh=s.model_health||{}, rg=s.regime_context||{}; setText('opRegimeTop',rg.regime||s.command?.market_regime||'—');setText('opQualityTop',`${fmt(dq.score??s.data_quality,0)}/100`);setText('opHealthTop',`${fmt(mh.score,0)}/100`);const ap=s.auditor_persistence||{};setText('opAuditTop',ap.ready?'AUDITOR · PERSISTENTE':(s.mode==='LIVE'?'AUDITOR · CHECK':'AUDITOR · DEMO'));setText('opMemoryTop',s.persistent_memory?.policy?'PERSISTENTE':'CHECK');setText('memoryState',s.persistent_memory?.policy?'MEMORIA · PROTEGIDA':'MEMORIA · CHECK'); setText('quality',`${fmt(dq.score??s.data_quality,0)}/100`);setText('qualityDetail',`${dq.status||'—'} · IV ${fmt(dq.iv_coverage_pct,0)}% · fallback ${dq.fallback_iv_count??0}`);setText('modelHealthScore',`${fmt(mh.score,0)}/100`);setText('modelHealthStatus',mh.status||'—');setText('regimeEngine',rg.regime||'—');setText('regimeEngineDetail',`${fmt(rg.confidence,0)}/100 · adaptive ${s.scanner?.adaptive_mode||'SHADOW'}`);setText('adaptiveMode',`SCANNER ${s.scanner?.adaptive_mode||'SHADOW'}`);
 setText('marketRegime',s.command?.market_regime); setText('gammaRegime',s.command?.gamma_regime); setText('gammaPressure',s.command?.gamma_pressure); setText('deltaPressure',s.command?.delta_pressure); setText('flowRegime',s.command?.flow_regime); setText('volRegime',s.command?.vol_regime); setText('alignment',`${s.gamma_delta_alignment?.label||'—'} ${fmt(s.gamma_delta_alignment?.score,0)}`);
 setText('routeCurrent',fmt(s.spot)); setText('routeActive',fmt(s.targets?.active?.strike)); setText('routeNext',fmt(s.targets?.next)); setText('routeExt',fmt(s.targets?.extension)); setText('activeState',`${s.targets?.active?.state||'—'} · confianza ${fmt(s.targets?.active?.confidence,0)}/100`);
 const lf=s.flow?.largest; setText('largestFlow',lf?`${lf.direction_sign>0?'BUY':'SELL'} ${money((lf.direction_sign||0)*(lf.premium||0))} · QF${fmt(lf.flow_score,0)}`:'Esperando mercado');
 setText('expectedMove',`± ${fmt(s.volatility?.expected_move)}`); setText('expectedRange',`${fmt(s.volatility?.expected_low)} → ${fmt(s.volatility?.expected_high)}`); setText('pcOi',fmt(s.positioning?.put_call_oi_ratio,2)); setText('oiWalls',`Call ${fmt(s.positioning?.top_call_oi_strike)} · Put ${fmt(s.positioning?.top_put_oi_strike)}`); setText('sourceNote',s.source_note);
 renderEasyMode(s.easy||{}); renderInstitutionalResearch(s.institutional_research||{}); renderSessionMemory(s.session_memory||{}); renderDealer(s.dealer_intelligence||{}); renderInfrastructure(s); renderNextgenIntelligence(s); renderLiveValidation(s.live_validation||{}); renderPremarket(s.premarket); renderPremarketAnalysis(s.premarket_analysis); renderScanner(s.scanner); renderConditionalOutcomes(s.conditional_outcomes||{}); renderTraceOrderflow(s.trace_orderflow||{}); renderDecisionCockpit(s); renderFlowCards(s.flow); renderTraceFlowCards(s); renderFlowProCards(s.flow_pro); renderFlowMicrostructure(s.flow?.microstructure||{}); renderVolCards(s.volatility); renderEquityHub(s); renderModelPrecision(s); renderExposureScenarios(s.exposure_scenarios||{}); renderGreeksDiagnostics(s.greeks_diagnostics||{}); renderPositioning(s.positioning); renderHighlights(s.chain_insights); renderMacro(s.macro); renderLargePrints(s.large_prints); renderWhatChanged(s.what_changed||[]);renderAttribution(s.trace_attribution||{});renderCalibration(s.calibration||{},s.scanner||{});updateTraceRecommendation();
 setText('auditScore',`${fmt(dq.score??s.data_quality,0)}/100`);setText('auditDataStatus',dq.status||'—');setText('auditModelHealth',`${fmt(mh.score,0)}/100`);setText('auditModelStatus',mh.status||'—');setText('auditCalibration',`${s.calibration?.sample_size??0} N`);setText('auditCalibrationStatus',s.calibration?.status||'COLLECTING'); const ar=s.auditor_persistence||{},ad=ar.daily||{},ac=ar.cumulative||{};setText('auditDailyPersistent',ar.ready?(ad.status||'PERSISTENTE'):(s.mode==='LIVE'?'CHECK':'DEMO'));setText('auditDailyPersistentDetail',ar.ready?`${ad.observations||0} observaciones · ${ad.session_date||'sesión actual'}`:'DEMO no contamina historial');setText('auditCumulativePersistent',ar.ready?`${ac.sessions_reported||0} SESIONES`:'COLLECTING');setText('auditCumulativePersistentDetail',ar.ready?`${ac.refresh_observations||0} observaciones acumuladas`:'esperando historial LIVE');setText('auditMemoryPersistent',s.persistent_memory?.policy?'PROTEGIDA':'CHECK');setText('auditMemoryPersistentDetail',s.persistent_memory?.policy?'actualizaciones no reinician memoria':'verificar persistencia'); const ns=s.native_options_structure||{}; setText('nativeStructureStatus',ns.ready?'OK':(ns.status||'WAITING')); setText('nativeStructureDetail',ns.ready?`Flip ${fmt(ns.gamma_flip)} · +OI ${fmt(ns.major_pos_oi)} · -OI ${fmt(ns.major_neg_oi)} · motor nativo`:'Estructura nativa esperando cadena válida.');
}

function setScoreBar(id,v){const x=el(id);if(!x)return;const n=Math.max(0,Math.min(100,Number(v||0)));x.style.width=`${n}%`;}
function scannerSideClass(dir){return String(dir||'').toUpperCase()==='BUY'?'scanner-buy':'scanner-sell'}
function renderScanner(q){
 const sec=el('section-scanner');if(!sec)return;
 if(!q||!q.ready){setText('scannerHeadline',q?.reason||'Esperando estructura…');setText('scannerState',q?.state||'WAITING');setText('scannerMode','—');return;}
 const ew=q.expiry_window||{};setText('scannerMode',`${ew.label||'AUTO'} · ${ew.count??0} exp.`);setText('scannerState',`${q.edge_state||'—'} · ${q.state||'PRIMARY'} · gap ${fmt(q.contender_gap,1)}`);
 setText('scannerDirection',q.direction||'—');setText('scannerType',`${q.scenario_type||'—'} · ${q.mode||''}`);
 const dcard=document.querySelector('.scanner-direction');if(dcard){dcard.classList.remove('scanner-buy','scanner-sell');dcard.classList.add(scannerSideClass(q.direction));}
 const z=q.zone||{};setText('scannerZone',`${fmt(z.low)} – ${fmt(z.high)}`);setText('scannerZoneCenter',`centro ${fmt(z.center)}`);
 setText('scannerT1',fmt(q.target1));setText('scannerT1Score',q.target1_score==null?'atracción —':`atracción ${fmt(q.target1_score,0)}/100`);
 setText('scannerT2',fmt(q.target2));setText('scannerT2Score',q.target2_score==null?'atracción —':`atracción ${fmt(q.target2_score,0)}/100`);
 setText('scannerEvidence',`${fmt(q.evidence_score,0)}/100`);setText('scannerInvalidation',fmt(q.invalidation));const sm=q.stop_model||{};setText('scannerStopDetail',sm.sigma_h==null?`${sm.status||'fallback'} · IV no disponible`:`${sm.status||'VOLATILITY'} · σ${fmt(sm.sigma_h,2)} · k ${fmt(sm.k_live,2)} · riesgo ${sm.risk_sigma==null?'—':fmt(sm.risk_sigma,2)+'σ'}`);
 const eg=q.edge_gate||{};const pp=Number(eg.probability_t1_first),ee=Number(eg.expected_value_r),bb=Number(eg.breakeven_probability);
 setText('scannerProbability',Number.isFinite(pp)?`${fmt(pp*100,1)}%`:'—');setText('scannerProbabilityDetail',Number.isFinite(pp)?`${eg.stage||'CALIBRATED'} · ${eg.calibration_expiry_mode||'—'} · ${eg.probability_source||'OOS'}`:`${eg.stage||'COLLECTING'} · ${eg.calibration_expiry_mode||'—'} · Evidence no se convierte a %`);
 setText('scannerEV',Number.isFinite(ee)?`${ee>=0?'+':''}${fmt(ee,2)}R`:'—');const instMode=(eg.instrument_mode||'UNDERLYING').toUpperCase();const rrTxt=eg.rr_used==null?'—':fmt(eg.rr_used,2);setText('scannerEVDetail',Number.isFinite(ee)?`${eg.active?'ACTIVE':'SHADOW'} · BE ${Number.isFinite(bb)?fmt(bb*100,1)+'%':'—'} · ${instMode==='OPTIONS'?'R/R prima':'R/R suby.'} ${rrTxt}`:`${eg.mode||'sin EV calibrado'} · ${instMode}${instMode==='OPTIONS'&&eg.option_economic_rr?.reason?' · '+eg.option_economic_rr.reason:''}`);
 const gate=q.quality_gate||{};setText('scannerQualityGate',gate.after||q.edge_state||'—');setText('scannerQualityGateDetail',`Data ${fmt(gate.data_quality,0)} · Model ${fmt(gate.model_health,0)}${(gate.reasons||[]).length?` · ${(gate.reasons||[]).join(' / ')}`:''}`);
 const rel=q.signal_reliability||{};const rd=rel.descriptive||{};setText('scannerReliability',rel.status||'COLLECTING');setText('scannerReliabilityDetail',rd&&rd.samples!=null?`${rd.samples} N · Exp ${rd.expectancy==null?'—':fmt(rd.expectancy,3)} · PF ${rd.profit_factor==null?'—':fmt(rd.profit_factor,2)}`:'Recolectando régimen comparable');
 const sh=q.shadow_scenario||q.regime_shadow||q.adaptive_shadow||{};setText('scannerShadow',sh.direction||sh.state||q.adaptive_mode||'SHADOW');setText('scannerShadowDetail',sh.evidence_score!=null?`${sh.kind||'scenario'} · Evidence ${fmt(sh.evidence_score,0)}/100 · solo shadow`:`${q.adaptive_mode||'SHADOW'} · no altera LIVE sin calibración`);
 const fs=q.feature_snapshot||{};setText('scannerMass',`${fmt(fs.mass_score,0)}/100`);setText('scannerGammaIntensity',`${fmt(fs.gamma_intensity_score,0)}/100`);setText('scannerTurnover',`${fmt(fs.turnover_score,0)}/100`);setText('scannerNetTilt',`${fmt(fs.net_tilt_score,0)}/100`);
 setText('scannerBounce',`${fmt(q.bounce_score,0)}/100`);setText('scannerBreak',`${fmt(q.break_score,0)}/100`);setText('scannerStructure',`${fmt(q.structural_score,0)}/100`);
 setScoreBar('scannerBounceBar',q.bounce_score);setScoreBar('scannerBreakBar',q.break_score);setScoreBar('scannerStructureBar',q.structural_score);
 setText('scannerRR1',q.rr_t1==null?'—':`${fmt(q.rr_t1,2)}R`);setText('scannerRR2',q.rr_t2==null?'—':`${fmt(q.rr_t2,2)}R`);
 const vac=q.vacuum||{};setText('scannerVacuum',vac.active?`ACTIVO ${fmt(vac.score,0)}/100`:`NO ${fmt(vac.score,0)}/100`);
 setText('scannerHeadline',`${q.edge_state||'—'} · ${q.direction} · ${q.scenario_type} desde ${fmt(z.center)} → ${q.target1==null?'—':fmt(q.target1)}${q.target2==null?'':` → ${fmt(q.target2)}`}`);
 const sp=q.secondary;setText('scannerSecondary',sp?`${sp.direction} · ${sp.kind==='BOUNCE'?'REBOTE':'RUPTURA'} zona ${fmt(sp.zone)} · trigger ${fmt(sp.trigger)} · evidencia ${fmt(sp.evidence_score,0)}/100`:'Sin Plan B relevante por ahora.');
 const reasons=el('scannerReasons');if(reasons){reasons.innerHTML='';(q.reasons||[]).forEach(r=>{const d=document.createElement('div');d.className='scanner-reason positive';d.innerHTML=`<i>✓</i><div><b>${r.label}</b><span>${r.detail||''}</span></div><strong>${fmt(r.value,0)}</strong>`;reasons.appendChild(d)});if(!(q.reasons||[]).length)reasons.innerHTML='<div class="muted">Todavía no hay razones destacadas suficientes.</div>';}
 const cons=el('scannerContradictions');if(cons){cons.innerHTML='';(q.contradictions||[]).forEach(r=>{const d=document.createElement('div');d.className='scanner-reason negative';d.innerHTML=`<i>!</i><div><b>${r.label}</b><span>${r.detail||''}</span></div><strong>${fmt(r.value,0)}</strong>`;cons.appendChild(d)});if(!(q.contradictions||[]).length)cons.innerHTML='<div class="scanner-clean">✓ Sin contradicción fuerte detectada en este ciclo.</div>';}
 const tb=el('scannerZonesTable');if(tb){tb.innerHTML='';(q.candidate_zones||[]).slice(0,12).sort((a,b)=>Number(a.strike)-Number(b.strike)).forEach(r=>{const tr=document.createElement('tr');const near=Math.abs(Number(r.strike)-Number(q.spot))<0.35;if(near)tr.classList.add('near-spot-row');const spec=(r.specials||[]).join(' · ');tr.innerHTML=`<td>${fmt(r.strike)}</td><td>${fmt(r.structural_score,0)}</td><td>${fmt(r.bounce_score,0)}</td><td>${fmt(r.break_score,0)}</td><td>${compact(r.signed_gex)}</td><td>${compact(r.delta_exposure)}</td><td>${compact(r.oi)}</td><td>${compact(r.volume)}</td><td>${fmt(r.vol_oi,2)}</td><td>${spec||'—'}</td>`;tb.appendChild(tr)});}
 setText('scannerMethod',q.method_note||'Scanner interno: sintetiza las secciones del programa.');
}

function flowSessionLabel(ts){if(!ts)return '—';const d=new Date(ts);const mins=d.getHours()*60+d.getMinutes();return mins<8*60+30?'PRE':'LIVE';}
function flowZoneLabel(s,e){
 if(!e||e.underlying_price==null)return '—'; const p=Number(e.underlying_price);
 const checks=[['DYNAMIC FLIP',s.gamma_flip],['GAMMA CENTER',s.gamma_center],['DELTA CENTER',s.delta_center],['NIVEL ACTIVO',s.targets?.active?.strike],['NEXT TARGET',s.targets?.next]];
 let best=null;checks.forEach(([n,v])=>{v=Number(v);if(Number.isFinite(v)){const d=Math.abs(p-v);if(!best||d<best.d)best={n,d,v}}});
 if(best&&best.d<=0.25)return `${best.n} · ${best.v.toFixed(2)}`;
 return 'ZONA INTERMEDIA · ver marcador TRACE';
}
function renderTraceFlowCards(s){
 const f=s?.flow||{}; const e=f.latest||f.largest;
 if(!e){setText('traceFlowActive','ESPERANDO');setText('traceFlowActiveDetail','Sin Q-Flow ≥70 confirmado');setText('traceLargestFlow','—');setText('traceLargestFlowDetail','—');setText('traceFlowSession','—');setText('traceFlowZone','—');return;}
 const buy=Number(e.direction_sign||0)>0; const side=buy?'BUY':'SELL'; const sess=flowSessionLabel(e.timestamp);
 setText('traceFlowActive',`${sess} ${side}`);setText('traceFlowActiveDetail',`${money((e.direction_sign||0)*(e.premium||0))} · QF${fmt(e.flow_score,0)} · ${activeSymbol} ${fmt(e.underlying_price)}`);
 const l=f.largest||e; const lb=Number(l.direction_sign||0)>0?'BUY':'SELL';
 setText('traceLargestFlow',`${lb} ${money((l.direction_sign||0)*(l.premium||0))}`);setText('traceLargestFlowDetail',`QF${fmt(l.flow_score,0)} · strike ${fmt(l.strike)}`);
 setText('traceFlowSession',sess);setText('traceFlowZone',flowZoneLabel(s,e));
}
function renderFlowProCards(f){
 const e=f?.latest, l=f?.largest;
 if(!e){setText('flowProLatest','ESPERANDO');setText('flowProLatestDetail','Sin evento confirmado');setText('flowProLargest','—');setText('flowProLargestDetail','—');setText('flowProNet',money(f?.net||0));setText('flowProZone','—');setText('flowProSession','PRE / LIVE');return;}
 const amt=x=>x?(x.amount>=1e6?`$${(x.amount/1e6).toFixed(1)}M`:`$${(x.amount/1e3).toFixed(0)}K`):'—';
 setText('flowProLatest',`${e.session} ${e.side} ${amt(e)}`);setText('flowProLatestDetail',`${e.source} · ${activeSymbol} ${fmt(e.price)} · Q${fmt(e.score,0)}`);
 setText('flowProLargest',l?`${l.session} ${l.side} ${amt(l)}`:'—');setText('flowProLargestDetail',l?`${l.zone} · Q${fmt(l.score,0)}`:'—');
 setText('flowProNet',money(f?.net||0));setText('flowProZone',e.zone||'—');setText('flowProSession',`${e.session} · ${e.source}`);
}
function renderDriftSummary(d){const x=d?.latest||{};setText('driftCall',x?.cum_call_premium==null?'—':money(x.cum_call_premium));setText('driftPut',x?.cum_put_premium==null?'—':money(x.cum_put_premium));setText('driftNet',x?.cum_net_premium==null?'—':money(x.cum_net_premium));setText('driftGamma',d?.buckets==null?'—':intfmt(d.buckets));}
function renderVolumeSummary(v){
 setText('volumeCalls',compact(v?.call_volume));setText('volumePuts',compact(v?.put_volume));setText('volumePCR',fmt(v?.put_call_volume_ratio,2));
 const h=v?.unusual;setText('volumeHotspot',h?`${activeSymbol} ${fmt(h.strike)}`:'—');setText('volumeHotspotDetail',h?`Vol/OI ${fmt(h.ratio,2)} · Z ${fmt(h.robust_z,1)} · accel ${v?.volume_acceleration_pct==null?'—':fmt(v.volume_acceleration_pct,0)+'%'}`:'actividad relativa');
}
function renderExposureSummary(x){
 if(!x||Object.keys(x).length===0){setText('expPositive','—');setText('expNegative','—');setText('expNearest','—');setText('expFlip','—');return;}
 setText('expPositive',x.positive_strike==null?'—':`${activeSymbol} ${fmt(x.positive_strike)}`);setText('expPositiveValue',x.positive_value==null?'—':`${fmt(x.positive_value,2)}`);
 setText('expNegative',x.negative_strike==null?'—':`${activeSymbol} ${fmt(x.negative_strike)}`);setText('expNegativeValue',x.negative_value==null?'—':`${fmt(x.negative_value,2)}`);
 setText('expNearest',x.nearest_strike==null?'—':`${activeSymbol} ${fmt(x.nearest_strike)}`);setText('expNearestValue',x.nearest_value==null?'—':`${fmt(x.nearest_value,2)}`);setText('expFlip',fmt(x.flip));
}

function renderSessionMemory(m){const g=m?.gamma_center_run||{},d=m?.delta_center_run||{};setText('sessionGammaRun',g?.direction?`${g.direction} · ${fmt(g.minutes,0)} min`:'—');setText('sessionGammaDetail',g?.from==null?'migración acumulada':`${fmt(g.from)} → ${fmt(g.to)}`);setText('sessionDeltaRun',d?.direction?`${d.direction} · ${fmt(d.minutes,0)} min`:'—');setText('sessionDeltaDetail',d?.from==null?'migración acumulada':`${fmt(d.from)} → ${fmt(d.to)}`);setText('sessionMemoryCount',m?.ready?`${m.observations||0} obs.`:'—');setText('sessionMemoryDetail',m?.ready?`${fmt(m.minutes_covered,0)} min · full snapshots en research`:(m?.note||'esperando LIVE'));}
function renderGreeksDiagnostics(g){const mix=g?.source_mix||{};const total=Object.values(mix).reduce((a,b)=>a+Number(b||0),0);const own=Object.entries(mix).filter(([k])=>String(k).toUpperCase().includes('ITM')).reduce((a,[,v])=>a+Number(v||0),0);setText('greeksSource',total?`ITM ${Math.round(100*own/total)}% · N ${total}`:'—');setText('greeksDeltaGap',g?.mean_abs_provider_delta_gap==null?'—':fmt(g.mean_abs_provider_delta_gap,4));setText('greeksGammaGap',g?.median_abs_provider_gamma_gap_pct==null?'—':`${fmt(g.median_abs_provider_gamma_gap_pct,1)}%`);const v=g?.top_vanna,c=g?.top_charm,sp=g?.top_speed;setText('greeksHigher',v?`V ${fmt(v.strike)} · C ${fmt(c?.strike)} · S ${fmt(sp?.strike)}`:'—');setText('greeksHigherDetail','strikes con mayor masa |Greek| × OI');}
function renderExposureScenarios(x){if(!x?.ready){setText('structuralGex','—');setText('flowAdjustedGex','—');setText('gexDownScenario','—');setText('gexUpScenario','—');return;}setText('structuralGex',money(x.structural_gex||0));setText('flowAdjustedGex',money(x.flow_adjusted_gex_proxy||0));const pick=(v)=> (x.sensitivity||[]).find(r=>Math.abs(Number(r.spot_shift_pct)-v)<.01);const dn=pick(-.5),up=pick(.5);setText('gexDownScenario',dn?money(dn.gex):'—');setText('gexUpScenario',up?money(up.gex):'—');}
function renderFlowMicrostructure(m){setText('flowMicroState',m?.state||m?.exhaustion||'WAITING');setText('flowPremiumAccel',m?.premium_acceleration_pct==null?'—':`${fmt(m.premium_acceleration_pct,0)}%`);setText('flowPersistence',m?.persistence_pct==null?'—':`${fmt(m.persistence_pct,0)}%`);const c=m?.multi_expiry_cluster;setText('flowMultiExpiry',c?`${activeSymbol} ${fmt(c.strike)}`:'—');setText('flowMultiExpiryDetail',c?`${c.expiries||c.expiry_count||'—'} expiries · ${c.events||c.count||'—'} eventos`:'sin cluster multi-expiry');}
function renderModelPrecision(s){const a=s?.american_model||{},mi=s?.model_inputs||{};setText('americanCheck',a?.ready?`MAX ${fmt(a.max_american_premium,3)}`:'—');setText('americanCheckDetail',a?.ready?`avg premium ${fmt(a.avg_american_premium,3)} · ${a.contracts||0} contratos`:(a?.reason||'CRR diagnóstico'));setText('rateInput',mi?.risk_free_rate==null?'—':`${fmt(100*mi.risk_free_rate,3)}%`);setText('rateSource',mi?.risk_free_source||'—');setText('dividendInput',mi?.dividend_yield==null?'—':`${fmt(100*mi.dividend_yield,3)}%`);setText('dividendSource',mi?.dividend_source||'—');}
function renderWhatChanged(rows){const root=el('whatChanged');if(!root)return;root.innerHTML='';(rows||[]).forEach(r=>{const d=document.createElement('div');d.className=`change-item ${r.severity==='WARN'?'warn':''}`;let body=r.detail||'';if(r.from!==undefined&&r.to!==undefined&&typeof r.from==='number')body=`${fmt(r.from)} → ${fmt(r.to)} · Δ ${fmt(r.change)}`;else if(r.detail)body=r.detail;d.innerHTML=`<span>${r.label||'CAMBIO'}</span><b>${r.direction||''}</b><small>${body}</small>`;root.appendChild(d)});if(!root.children.length)root.innerHTML='<div class="muted">Esperando comparación entre ciclos.</div>';}
function renderAttribution(a){if(!a?.ready){setText('attrDriver','ESPERANDO');setText('attrSpot','—');setText('attrIv','—');setText('attrTimeOi','—');return;}const e=a.effects||{},sh=a.shares_pct||{};const names={spot_repricing:'SPOT',iv_change:'IV',time_decay:'TIME',oi_structure:'OI'};setText('attrDriver',names[a.dominant_driver]||a.dominant_driver||'—');setText('attrSpot',`${fmt(e.spot_repricing,2)}`);setText('attrSpotDetail',`${fmt(sh.spot_repricing,0)}% del cambio absoluto atribuido`);setText('attrIv',`${fmt(e.iv_change,2)}`);setText('attrIvDetail',`${fmt(sh.iv_change,0)}% del cambio absoluto atribuido`);setText('attrTimeOi',`${fmt(e.time_decay,2)} / ${fmt(e.oi_structure,2)}`);}
function renderLiveValidation(v){
 setText('liveValidationStage',`${v.probability_stage||'COLLECTING'} · GATE ${v.ev_gate_active?'ON':'OFF'}`);
 const root=el('liveValidationGrid');if(root){root.innerHTML='';(v.components||[]).forEach(r=>{const d=document.createElement('div');d.className=`change-item ${r.ready?'':'warn'}`;const value=r.target==null?`${intfmt(r.value||0)}`:`${intfmt(r.value||0)} / ${intfmt(r.target)}`;d.innerHTML=`<span>${r.label||'—'}</span><b>${value}</b><small>${r.detail||''}</small>`;root.appendChild(d)});if(!root.children.length)root.innerHTML='<div class="muted">Aún no hay datos LIVE persistidos.</div>';}
 setText('liveValidationNext',`${v.instrument_mode||'UNDERLYING'} · ${v.next_step||'Recolectando datos reales.'}`);
}

function renderCalibration(c,scanner){setText('calStatus',c?.status||'COLLECTING');setText('calSamples',c?.sample_size??0);setText('calPurge',`${c?.walk_forward?.purged_sessions??0} sesión`);setText('calNetExpectancy',c?.cost_aware?.net_expectancy==null?'—':fmt(c.cost_aware.net_expectancy,3));setText('calNetPF',c?.cost_aware?.net_profit_factor==null?'—':fmt(c.cost_aware.net_profit_factor,2));setText('calCostAssumptions',`slippage ${fmt(c?.execution_assumptions?.slippage_bps,1)} bps · cost ${fmt(c?.execution_assumptions?.roundtrip_cost_bps,1)} bps · costR ${fmt(c?.execution_assumptions?.cost_r,2)}`);setText('calMinimum',`mínimo recomendado ${c?.minimum_recommended??'—'}`);setText('calSessions',c?.sessions??0);setText('calAdaptive',scanner?.adaptive_mode||'SHADOW');setText('calWalkForward',c?.walk_forward_readiness||'COLLECTING SESSIONS');setText('calibrationNote',c?.method_note||c?.reason||'Recolectando datos.');
 const pm=c?.probability_model||{};setText('calProbStage',pm.stage||pm.status||'COLLECTING');setText('calProbStageDetail',pm.ready?`${pm.expiry_mode||'—'} · ${pm.source_label||'OOS válido'} · listo para habilitación explícita`:`${pm.expiry_mode||'—'} · ${pm.reason||pm.source_label||'solo research/shadow'}`);setText('calBrierSkill',pm.brier_skill_score==null?'—':fmt(Number(pm.brier_skill_score)*100,1)+'%');setText('calBrierDetail',pm.brier_model==null?'esperando holdout':`modelo ${fmt(pm.brier_model,4)} · base ${fmt(pm.brier_base_rate,4)}`);setText('calBaseRate',pm.base_rate==null?'—':fmt(Number(pm.base_rate)*100,1)+'%');setText('calInstrumentMode',(c?.instrument_mode||'underlying').toUpperCase());
 setText('calPooling',pm.source_label||'—');const bv=pm.blend_validation||{};setText('calPoolingDetail',pm.pooling_weight_scope==null?'sin partial pooling':`scope ${fmt(Number(pm.pooling_weight_scope)*100,1)}% · global ${fmt(Number(pm.pooling_weight_global)*100,1)}% · ${pm.pooling_weight_method||'peso'}${bv.status?` · blend ${bv.status}`:''}`);setText('calLogLoss',pm.log_loss_model==null?'—':fmt(pm.log_loss_model,4));setText('calLogLossDetail',pm.log_loss_base_rate==null?'esperando holdout':`base ${fmt(pm.log_loss_base_rate,4)}`);const rt=pm.resolution_time||{},rt1=rt.t1||{},ri=rt.invalidation||{};setText('calResolutionTime',rt1.median==null?'—':`${fmt(rt1.median,1)} min`);setText('calResolutionDetail',`T1 mediana · Inv ${ri.median==null?'—':fmt(ri.median,1)+' min'} · ${rt.source||'sin fuente'}`);setText('calRiskUnit',(c?.instrument_mode||'underlying').toLowerCase()==='options'?'MODEL→INVALIDATION':'UNDERLYING→INVALIDATION');setText('calRiskUnitDetail',(c?.instrument_mode||'underlying').toLowerCase()==='options'?'Prima completa = capital máximo, no 1R':'1R = distancia entrada→invalidación');
 const evr=el('evExamples');if(evr){evr.innerHTML='';(c?.expected_value_examples||[]).forEach(r=>{const d=document.createElement('div');d.className='change-item';d.innerHTML=`<span>R/R ${fmt(r.rr,2)}</span><b>${r.ev_r==null?'—':(Number(r.ev_r)>=0?'+':'')+fmt(r.ev_r,2)+'R'}</b><small>P ${fmt(Number(r.assumed_probability)*100,1)}% · BE ${r.breakeven_probability==null?'—':fmt(Number(r.breakeven_probability)*100,1)+'%'}</small>`;evr.appendChild(d)});if(!evr.children.length)evr.innerHTML='<div class="muted">Aún no existe calibración utilizable.</div>';}
 const pr=el('probReliability');if(pr){pr.innerHTML='';(pm.reliability||[]).forEach(r=>{const d=document.createElement('div');d.className='change-item';d.innerHTML=`<span>${r.predicted_range||'—'}</span><b>${fmt(Number(r.observed_frequency)*100,1)}%</b><small>N ${r.n||0} · pred ${fmt(Number(r.mean_predicted)*100,1)}%</small>`;pr.appendChild(d)});if(!pr.children.length)pr.innerHTML='<div class="muted">Se necesita una muestra OOS suficiente.</div>';}
 const tb=el('calibrationTable');if(tb){tb.innerHTML='';Object.entries(c?.horizons||{}).forEach(([h,r])=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${h} min</td><td>${r.samples}</td><td>${fmt(r.directional_positive_pct,1)}%</td><td>${fmt(r.t1_hit_pct,1)}%</td><td>${fmt(r.t2_hit_pct,1)}%</td><td>${fmt(r.invalidation_pct,1)}%</td><td>${fmt(r.avg_mfe,3)}</td><td>${fmt(r.avg_mae,3)}</td><td>${fmt(r.expectancy,3)}</td><td>${r.profit_factor==null?'—':fmt(r.profit_factor,2)}</td><td>${r.brier_diagnostic==null?'—':fmt(r.brier_diagnostic,3)}</td><td>${r.avg_t1_minutes==null?'—':fmt(r.avg_t1_minutes,1)}</td><td>${r.risk_unit||'—'}</td>`;tb.appendChild(tr)});if(!tb.children.length)tb.innerHTML='<tr><td colspan="13">Aún no hay trayectoria posterior suficiente.</td></tr>';}const fill=(id,rows,key)=>{const root=el(id);if(!root)return;root.innerHTML='';(rows||[]).forEach(r=>{const d=document.createElement('div');d.className='change-item';const name=r[key]??r.bin??'—';d.innerHTML=`<span>${name}</span><b>N ${r.samples||0}</b><small>Exp ${fmt(r.expectancy,3)} · PF ${r.profit_factor==null?'—':fmt(r.profit_factor,2)} · T1 ${fmt(r.t1_hit_pct,1)}% · Inv ${fmt(r.invalidation_pct,1)}%</small>`;root.appendChild(d)});if(!root.children.length)root.innerHTML='<div class="muted">Muestra insuficiente todavía.</div>';};fill('evidenceBins',c?.evidence_bins||[],'bin');fill('edgeCalibration',c?.edge_breakdown||[],'edge_state');fill('regimeCalibration',c?.regime_breakdown||[],'regime');fill('expiryCalibration',c?.expiry_breakdown||[],'expiry_mode');const wf=el('walkForwardStats');if(wf){wf.innerHTML='';const w=c?.walk_forward||{};if(w?.ready){const t=w.test||{};wf.innerHTML=`<div class="change-item"><span>HOLDOUT POSTERIOR</span><b>${w.test_sessions||0} sesiones</b><small>Expectancy ${fmt(t.expectancy,3)} · PF ${t.profit_factor==null?'—':fmt(t.profit_factor,2)} · T1 ${fmt(t.t1_hit_pct,1)}% · Brier* ${t.brier_diagnostic==null?'—':fmt(t.brier_diagnostic,3)}</small></div><div class="change-item"><span>TRAIN / TEST</span><b>${w.train_sessions||0} / ${w.test_sessions||0}</b><small>${w.method||'holdout cronológico'}</small></div>`;}else wf.innerHTML='<div class="muted">Aún faltan sesiones independientes para holdout cronológico.</div>';const fd=el('featureCalibration');if(fd){fd.innerHTML='';(c?.feature_diagnostics||[]).slice(0,12).forEach(r=>{const d=document.createElement('div');d.className='change-item';const corr=Number(r.spearman_to_resolved_move??r.spearman??r.correlation??0);d.innerHTML=`<span>${r.feature||'—'}</span><b>${Number.isFinite(corr)?fmt(corr,2):'—'}</b><small>N ${r.samples||0} · + ${r.avg_when_positive==null?'—':fmt(r.avg_when_positive,3)} · no+ ${r.avg_when_nonpositive==null?'—':fmt(r.avg_when_nonpositive,3)}</small>`;fd.appendChild(d)});if(!fd.children.length)fd.innerHTML='<div class="muted">Aún no hay muestra suficiente por variable.</div>';}const wf2=el('weightFitStats');if(wf2){wf2.innerHTML='';const w=c?.weight_fit||{};if(w?.ready){const d=document.createElement('div');d.className='change-item';d.innerHTML=`<span>HOLDOUT WEIGHT FIT</span><b>${fmt(w.test_direction_accuracy,1)}%</b><small>${w.train_sessions||0} sesiones train · ${w.test_sessions||0} sesiones test · ${w.test_samples||0} muestras test · investigación SHADOW</small>`;wf2.appendChild(d);Object.entries(w.suggested_weights||{}).sort((a,b)=>Number(b[1])-Number(a[1])).slice(0,10).forEach(([k,v])=>{const x=document.createElement('div');x.className='change-item';x.innerHTML=`<span>${k}</span><b>${fmt(Number(v),1)}%</b><small>peso sugerido · no activo LIVE</small>`;wf2.appendChild(x)});}else wf2.innerHTML=`<div class="muted">${w?.reason||'Necesita ≥80 muestras, ≥8 sesiones y suficientes variables.'}</div>`;}}}
function drawProjectedRouteCanvas(report){
 lastPremarketAnalysis=report||lastPremarketAnalysis;const canvas=el('premarketRouteCanvas'),toggle=el('premarketRouteToggle');if(!canvas)return;if(toggle&&!toggle.checked){canvas.classList.add('route-off');return;}canvas.classList.remove('route-off');const r=lastPremarketAnalysis||{},m=r.main_scenario||{},z=m.zone||{},dir=String(m.direction||r?.summary?.bias||'').toUpperCase();const start=[Number(z.center),Number(z.low),Number(z.high),Number(r.spot)].find(Number.isFinite),t1=Number(m.target1),t2=Number(m.target2),inv=Number(m.invalidation);const vals=[start,t1,t2,inv].filter(Number.isFinite);const rect=canvas.getBoundingClientRect(),w=Math.max(480,Math.round(rect.width||900)),h=Math.max(190,Math.round(rect.height||230)),d=Math.min(devicePixelRatio||1,2);if(canvas.width!==Math.round(w*d)||canvas.height!==Math.round(h*d)){canvas.width=Math.round(w*d);canvas.height=Math.round(h*d)}const c=canvas.getContext('2d');c.setTransform(d,0,0,d,0,0);c.clearRect(0,0,w,h);c.fillStyle='#040a10';c.fillRect(0,0,w,h);c.strokeStyle='#172938';c.lineWidth=1;for(let i=0;i<5;i++){const y=22+(h-46)*i/4;c.beginPath();c.moveTo(52,y);c.lineTo(w-18,y);c.stroke();}
 if(!Number.isFinite(start)||!vals.length){c.fillStyle='#71879a';c.font='12px ui-monospace,Consolas';c.fillText('ANALIZA PREMARKET PARA CONSTRUIR LA RUTA DE NIVELES',60,h/2);setText('pmaRouteState','ESPERANDO ANÁLISIS');return;}
 let lo=Math.min(...vals),hi=Math.max(...vals);let span=Math.max(hi-lo,Math.abs(start)*.001,0.25);lo-=span*.18;hi+=span*.18;const py=v=>22+(hi-v)/(hi-lo)*(h-46),xs=[w*.12,w*.53,w*.86],ys=[py(start),Number.isFinite(t1)?py(t1):py(start),Number.isFinite(t2)?py(t2):(Number.isFinite(t1)?py(t1):py(start))];const isSell=dir.includes('VENTA')||dir.includes('SELL')||dir.includes('BEAR');const col=isSell?'#ff5a76':'#25d7a4';c.strokeStyle=col;c.lineWidth=2.2;c.setLineDash([7,5]);c.beginPath();c.moveTo(xs[0],ys[0]);const mx=(xs[0]+xs[1])/2,my=(ys[0]+ys[1])/2+(isSell?-8:8);c.quadraticCurveTo(mx,my,xs[1],ys[1]);const mx2=(xs[1]+xs[2])/2,my2=(ys[1]+ys[2])/2+(isSell?7:-7);c.quadraticCurveTo(mx2,my2,xs[2],ys[2]);c.stroke();c.setLineDash([]);
 const node=(x,y,label,val)=>{c.fillStyle=col;c.beginPath();c.arc(x,y,5,0,Math.PI*2);c.fill();c.fillStyle='#dceaf3';c.font='700 11px ui-monospace,Consolas';c.fillText(`${label} ${fmt(val,2)}`,x+9,y-7)};node(xs[0],ys[0],'ORIGEN',start);if(Number.isFinite(t1))node(xs[1],ys[1],'T1',t1);if(Number.isFinite(t2))node(xs[2],ys[2],'T2',t2);
 if(Number.isFinite(inv)){const ix=w*.32,iy=py(inv);c.strokeStyle='#f2c24f';c.setLineDash([3,4]);c.beginPath();c.moveTo(xs[0],ys[0]);c.lineTo(ix,iy);c.stroke();c.setLineDash([]);c.fillStyle='#f2c24f';c.beginPath();c.arc(ix,iy,4,0,Math.PI*2);c.fill();c.fillStyle='#d8c782';c.fillText(`INVALIDACIÓN ${fmt(inv,2)}`,ix+8,iy-6)}
 c.fillStyle='#71879a';c.font='10px ui-monospace,Consolas';c.fillText('CURVA = INTERPOLACIÓN VISUAL ENTRE NIVELES DEL SCANNER · NO ES PROBABILIDAD DE CAMINO',52,h-8);setText('pmaRouteState',`${dir||'—'} · ${activeSymbol} ${fmt(start)} → ${Number.isFinite(t1)?fmt(t1):'—'}${Number.isFinite(t2)?' → '+fmt(t2):''}`);
}
function scheduleProjectedRoute(report){requestAnimationFrame(()=>drawProjectedRouteCanvas(report));}

function renderPremarket(p){if(!p){el('premarketEmpty').classList.remove('hidden');el('premarketContent').classList.add('hidden');return;} el('premarketEmpty').classList.add('hidden');el('premarketContent').classList.remove('hidden');
 setText('pmLow',fmt(p.low));setText('pmPivot',fmt(p.pivot));setText('pmHigh',fmt(p.high));setText('pmDirection',p.favored_direction||'—');setText('pmSpot',fmt(p.spot));setText('pmCenter',fmt(p.gamma_center));setText('pmFlip',p.gamma_flip==null?'—':`≈ ${fmt(p.gamma_flip,1)}`);setText('pmGamma',p.gamma_regime||'—'); setText('pmCaptured',p.captured_at_ec?new Date(p.captured_at_ec).toLocaleString():'—'); const t=p.premarket_tape;setText('pmTape',t?`${t.pressure||'—'} ${fmt(t.score,0)}/100`:'Options: esperando apertura'); setText('pmEM',p.volatility?.expected_move?`± ${fmt(p.volatility.expected_move)}`:'—'); const pew=p.expiry_window||{};setText('pmExpiry',pew.label?`${pew.label} · ${pew.count??0} exp.`:'—');}
function renderPremarketAnalysis(r){
 lastPremarketAnalysis=r||lastPremarketAnalysis;const empty=el('premarketAnalysisEmpty'),content=el('premarketAnalysisContent');if(!empty||!content)return;
 if(!r||!r.ready){empty.classList.remove('hidden');content.classList.add('hidden');return;}empty.classList.add('hidden');content.classList.remove('hidden');
 const su=r.summary||{},ct=r.chain_totals||{},st=r.structure||{},co=r.conclusion||{},cs=r.confirmation_summary||{};
 setText('pmaBias',su.bias||'—');setText('pmaRegime',su.regime||su.market_state||'—');setText('pmaStrength',`${fmt(su.strength,0)}/100`);setText('pmaQuality',`Datos ${fmt(su.data_quality,0)} · Modelo ${fmt(su.model_health,0)}`);setText('pmaSpot',fmt(r.spot));setText('pmaKeyLevel',fmt(co.key_level));
 const kr=(r.key_strikes||[]).reduce((best,x)=>best==null||Math.abs(Number(x.strike)-Number(co.key_level))<Math.abs(Number(best.strike)-Number(co.key_level))?x:best,null);setText('pmaKeyReading',kr?.joint_reading||'—');
 setText('pmaCallOi',intfmt(ct.call_oi));setText('pmaPutOi',intfmt(ct.put_oi));setText('pmaTotalOi',intfmt(ct.total_oi));setText('pmaCallVol',intfmt(ct.call_volume));setText('pmaPutVol',intfmt(ct.put_volume));setText('pmaTotalVol',intfmt(ct.total_volume));setText('pmaNetGex',exposureExact(ct.net_gex));setText('pmaGrossGex',`$${intfmt(ct.gross_gex)}`);setText('pmaNetDex',exposureExact(ct.net_delta));
 const tb=el('pmaStrikes');if(tb){tb.innerHTML='';(r.key_strikes||[]).forEach(x=>{const tr=document.createElement('tr');const g=`${exposureExact(x.net_gex)}<small>C ${exposureExact(x.call_gex)} · P ${exposureExact(x.put_gex)}</small>`;const d=`${exposureExact(x.net_delta)}<small>C ${exposureExact(x.call_delta)} · P ${exposureExact(x.put_delta)}</small>`;tr.innerHTML=`<td><b>${fmt(x.strike)}</b><small>${x.distance>=0?'+':''}${fmt(x.distance,2)} vs spot</small></td><td>${intfmt(x.call_oi)}</td><td>${intfmt(x.put_oi)}</td><td><b>${intfmt(x.total_oi)}</b></td><td>${intfmt(x.call_volume)}</td><td>${intfmt(x.put_volume)}</td><td><b>${intfmt(x.total_volume)}</b></td><td>${g}</td><td>${d}</td><td>${fmt(x.avg_iv_pct,2)}%</td><td><b>${x.joint_reading||'—'}</b><small>Score ${fmt(x.importance_score,0)} · ${x.state||'—'}</small></td>`;tb.appendChild(tr)});if(!tb.children.length)tb.innerHTML='<tr><td colspan="11">Sin strikes utilizables.</td></tr>';}
 const conc=el('pmaConcentrations');if(conc){conc.innerHTML='';const c=r.concentrations||{};const defs=[['Mayor OI total',c.max_total_oi,'contratos'],['Mayor Call OI',c.max_call_oi,'contratos'],['Mayor Put OI',c.max_put_oi,'contratos'],['Mayor Gamma positiva',c.max_positive_gamma,'GEX'],['Mayor Gamma negativa',c.max_negative_gamma,'GEX'],['Mayor Delta positiva',c.max_positive_delta,'DEX'],['Mayor Delta negativa',c.max_negative_delta,'DEX'],['Mayor actividad',c.max_activity,'ratio']];defs.forEach(([name,x,unit])=>{if(!x)return;const d=document.createElement('div');d.className='change-item';let val=unit==='contratos'?intfmt(x.value):unit==='ratio'?`${fmt(x.value,2)}x · Vol ${intfmt(x.volume)} / OI ${intfmt(x.oi)}`:exposureExact(x.value);d.innerHTML=`<span>${name}</span><b>${activeSymbol} ${fmt(x.strike)} · ${val}</b><small>${unit}</small>`;conc.appendChild(d)});if(!conc.children.length)conc.innerHTML='<div class="muted">Sin concentraciones calculables.</div>';}
 setText('pmaFlip',st.gamma_flip==null?'—':`≈ ${fmt(st.gamma_flip,1)}`);setText('pmaMaxPain',fmt(st.max_pain));setText('pmaCallWall',fmt(st.call_wall));setText('pmaPutWall',fmt(st.put_wall));setText('pmaCallOiPeak',fmt(st.max_call_oi_strike));setText('pmaPutOiPeak',fmt(st.max_put_oi_strike));setText('pmaAtmIv',st.atm_iv_pct==null?'—':`${fmt(st.atm_iv_pct,2)}%`);setText('pmaExpectedMove',st.expected_move==null?'—':`± ${fmt(st.expected_move)}`);setText('pmaExpectedLow',fmt(st.expected_low));setText('pmaExpectedHigh',fmt(st.expected_high));
 const ex=r.expiry||{};setText('pmaExpiryStatus',ex.status||'—');const eg=el('pmaExpiryGrid');if(eg){eg.innerHTML='';Object.entries(ex.horizons||{}).forEach(([name,h])=>{const d=document.createElement('div');d.className='change-item';const tops=(h.top||[]).map(x=>`${activeSymbol} ${fmt(x.strike)} (${fmt(x.score,0)})`).join(' · ');d.innerHTML=`<span>${name}</span><b>${(h.expirations||[]).length} vencimientos</b><small>${tops||'Sin niveles suficientes'}</small>`;eg.appendChild(d)});if(!eg.children.length)eg.innerHTML='<div class="muted">Sin confluencia por vencimiento disponible.</div>';}
 setText('pmaConfirmSummary',cs.usable?`${cs.confirmed}/${cs.usable} confirman · ${cs.contradicted} contradicen${cs.score==null?'':` · ${fmt(cs.score,0)}/100`}`:`Sin confirmaciones externas utilizables · ${cs.pending||0} fuentes pendientes${cs.not_applicable_premarket?` · ${cs.not_applicable_premarket} se activan en LIVE`:''}`);
 const cf=el('pmaConfirmations');if(cf){cf.innerHTML='';(r.confirmations||[]).forEach(x=>{const v=x.values||{};const parts=[];if(v.price!=null)parts.push(`Precio ${fmt(v.price)}`);if(v.change_pct!=null)parts.push(`${Number(v.change_pct)>=0?'+':''}${fmt(v.change_pct,2)}%`);if(v.vwap!=null)parts.push(`VWAP ${fmt(v.vwap)}`);if(v.volume!=null)parts.push(`Vol ${intfmt(v.volume)}`);if(v.open_interest!=null)parts.push(`OI ${intfmt(v.open_interest)}`);if(v.net_flow!=null)parts.push(`Flujo ${exposureExact(v.net_flow)}`);if(v.confidence!=null)parts.push(`Conf ${fmt(v.confidence,0)}/100`);if(v.net_15m!=null)parts.push(`15m ${exposureExact(v.net_15m)}`);if(v.level!=null)parts.push(`Nivel ${fmt(v.level)}`);const tr=document.createElement('tr');tr.innerHTML=`<td>${x.name}${x.inferred?' <small>estimado</small>':''}</td><td><b>${x.status||'—'}</b></td><td>${x.score==null?'—':fmt(x.score,0)+'/100'}</td><td>${parts.join(' · ')||'—'}</td><td>${x.basis||'—'}</td>`;cf.appendChild(tr)});if(!cf.children.length)cf.innerHTML='<tr><td colspan="5">Sin capas de confirmación.</td></tr>';}
 const sit=el('pmaSituations');if(sit){sit.innerHTML='';(r.situations||[]).forEach(x=>{const d=document.createElement('div');d.className='change-item';const m=Object.entries(x.metrics||{}).map(([k,v])=>`${k}: ${typeof v==='number'?fmt(v,2):v}`).join(' · ');d.innerHTML=`<span>${x.title}</span><b>${x.severity||'—'}</b><small>${m}${m?' · ':''}${x.reading||''}</small>`;sit.appendChild(d)});if(!sit.children.length)sit.innerHTML='<div class="muted">No se detectó una anomalía cuantificada suficientemente fuerte.</div>';}
 const m=r.main_scenario||{},z=m.zone||{};setText('pmaMainScenario',`${m.direction||'—'} · zona ${activeSymbol} ${fmt(z.low)}–${fmt(z.high)} · T1 ${fmt(m.target1)} · T2 ${fmt(m.target2)} · invalidación ${fmt(m.invalidation)} · evidencia ${fmt(m.evidence,0)}/100`);
 const a=r.alternative_scenario;setText('pmaAltScenario',a?`${a.direction||'—'} · se activa si falla el principal / trigger ${fmt(a.trigger)} · zona ${fmt(a.zone)} · evidencia ${fmt(a.evidence,0)}/100`:'Sin escenario alternativo utilizable.');
 setText('pmaConclusionHeadline',`${co.bias||'—'} · nivel ${fmt(co.key_level)} · objetivo ${fmt(co.primary_target)} · invalidación ${fmt(co.invalidation)}`);setText('pmaConclusionText',co.reading||'—');setText('pmaMethod',r.method_note||'—');scheduleProjectedRoute(r);
}
function renderFlowCards(f){const ns=f?.normalized_shadow||{};setText('flowCardRegime',`${f?.regime||'WAITING'} ${fmt(f?.confidence,0)} · Norm ${ns.status||'SHADOW · COLLECTING'}`);setText('flowNet',money(f?.net||0)); const b=f?.bull_top, s=f?.bear_top;setText('flowBuy',b?`+$${compact(b.premium)} · LIVE ${fmt(b.flow_score,0)} · N ${b.flow_score_normalized==null?'—':fmt(b.flow_score_normalized,0)}`:'—');setText('flowSell',s?`-$${compact(s.premium)} · LIVE ${fmt(s.flow_score,0)} · N ${s.flow_score_normalized==null?'—':fmt(s.flow_score_normalized,0)}`:'—');}
function renderVolCards(v){setText('atmIv',v?.atm_iv==null?'—':`${fmt(v.atm_iv,2)}%`);setText('skew',v?.skew_25d==null?'—':`${fmt(v.skew_25d,2)} pp`);setText('volEM',`± ${fmt(v?.model_expected_move??v?.expected_move)}`);setText('marketEM',v?.market_implied_move==null?'—':`± ${fmt(v.market_implied_move)}`);setText('emDislocation',v?.expected_move_dislocation_pct==null?'—':`${fmt(v.expected_move_dislocation_pct,1)}%`);setText('volState',v?.regime||'—');setText('realizedVol',v?.realized_volatility_pct==null?'—':`${fmt(v.realized_volatility_pct,2)}%`);setText('realizedVolDetail',`${v?.realized_vol_method||'INSUFICIENTE'}${v?.realized_vol_rel_std_error_pct==null?'':` · error rel. ~${fmt(v.realized_vol_rel_std_error_pct,1)}%`}`);setText('ivRvSpread',v?.iv_minus_rv_pp==null?'—':`${fmt(v.iv_minus_rv_pp,2)} pp`);setText('termState',v?.term_structure_state||'—');setText('volOfVol',v?.vol_of_vol_pp==null?'—':`${fmt(v.vol_of_vol_pp,3)} pp`);setText('forwardVol',v?.forward_volatility_pct==null?'—':`${fmt(v.forward_volatility_pct,2)}%`);setText('frontForward',v?.front_vs_forward_vol_pp==null?'—':`${fmt(v.front_vs_forward_vol_pp,2)} pp`);const root=el('skewExpiryGrid');if(root){root.innerHTML='';(v?.skew_by_expiry||[]).slice(0,8).forEach(r=>{const d=document.createElement('div');d.className='change-item';d.innerHTML=`<span>${r.expiration_date}</span><b>${fmt(r.skew_25d,2)} pp</b><small>Call25 ${fmt(r.call25_iv,1)}% · Put25 ${fmt(r.put25_iv,1)}% · Δskew ${r.skew_migration_pp==null?'—':fmt(r.skew_migration_pp,2)+' pp'}</small>`;root.appendChild(d)});if(!root.children.length)root.innerHTML='<div class="muted">Sin skew comparable por vencimiento.</div>';}}

// Equity Hub (v1.36): every field here already exists elsewhere in the state
// (key_levels_report=Fase 3, gamma_squeeze=Fase 6, skew=Fase 4/observed, gamma_regime,
// scanner). This function only reads and displays; it computes nothing.
function renderEquityHub(s){
 const kl=s?.key_levels_report||{};
 setText('ehSpot',fmt(s?.spot));
 setText('ehZeroGamma',kl.zero_gamma==null?'—':fmt(kl.zero_gamma,2));
 setText('ehCallWall',kl.call_wall==null?'—':fmt(kl.call_wall,2));
 setText('ehPutWall',kl.put_wall==null?'—':fmt(kl.put_wall,2));
 setText('ehVolTrigger',kl.vol_trigger==null?'—':fmt(kl.vol_trigger,2));
 setText('ehMaxPain',kl.max_pain==null?'—':fmt(kl.max_pain,2));
 setText('ehExpectedMove',kl.expected_move==null?'—':`± ${fmt(kl.expected_move,2)}`);
 setText('ehExpectedRange',kl.expected_low==null?'—':`${fmt(kl.expected_low,2)} – ${fmt(kl.expected_high,2)}`);
 setText('ehAtmIv',kl.atm_iv_pct==null?'—':`${fmt(kl.atm_iv_pct,2)}%`);
 const v=s?.volatility||{};setText('ehSkew',v.skew_25d==null?'—':`${fmt(v.skew_25d,2)} pp`);
 setText('ehRegime',s?.gamma_regime||'—');
 const sq=s?.market_state_field?.gamma_squeeze||{};
 setText('ehSqueeze',sq.ready?`${fmt(sq.score,0)}/100 · ${sq.label}`:'—');
 const dir=String(s?.scanner?.direction||'').toUpperCase();
 setText('ehScanner',dir==='BUY'?'COMPRA':dir==='SELL'?'VENTA':(s?.scanner?.direction||'—'));
}
function renderMonteCarloLevels(mc){
 const root=el('ehMonteCarloLevels');if(!root)return;
 root.innerHTML='';
 if(!mc?.ready){root.innerHTML=`<div class="muted">Sin simulación disponible${mc?.reason?` (${mc.reason})`:''}.</div>`;return;}
 const labels={zero_gamma:'Zero Gamma',call_wall:'Call Wall',put_wall:'Put Wall',vol_trigger:'Vol Trigger',max_pain:'Max Pain'};
 Object.entries(mc.levels||{}).forEach(([key,lv])=>{
  const d=document.createElement('div');d.className='change-item';
  d.innerHTML=`<span>${labels[key]||key}</span><b>${fmt(lv.level,2)}</b><small>Tocar antes del venc.: ${fmt(lv.prob_touch_by_expiry_pct,1)}% · Cerrar más allá: ${fmt(lv.prob_finish_beyond_pct,1)}%</small>`;
  root.appendChild(d);
 });
 if(!root.children.length)root.innerHTML='<div class="muted">Sin niveles disponibles para simular.</div>';
}
function renderPositioning(p){setText('positioningDataStatus',p?'ACTUALIZADO · OI / GEX / DEX':'ESPERANDO CADENA / OI');setText('callOi',compact(p?.call_oi));setText('putOi',compact(p?.put_oi));setText('pcOi2',fmt(p?.put_call_oi_ratio,2));setText('topCallOi',fmt(p?.top_call_oi_strike));setText('topPutOi',fmt(p?.top_put_oi_strike));setText('netGex',`${compact(p?.net_gex)} GEX`);setText('grossGex',`${compact(p?.gross_gex)} GEX`);setText('netDex',`${compact(p?.net_delta)} DEX`);}
function renderMacro(m){
 const st=m?.stress||{};setText('macroStress',`${st.label||'—'} ${fmt(st.score,0)}/100`);const ac=m?.asset_context||{};setText('macroStress2',ac?.score==null?`${st.label||'—'} ${fmt(st.score,0)}/100`:`${activeSymbol} ${fmt(ac.score,0)}/100`);
 const e=st.next_high_event;setText('macroNextEvent',e?`Próximo HIGH: ${e.title} · ${new Date(e.time_ec).toLocaleString()}`:'Sin evento HIGH cercano');
 const ser=m?.series||{}; const y10=ser.DGS10||{}, curve=ser.T10Y2Y||{}, hy=ser.BAMLH0A0HYM2||{};
 setText('macro10y',y10.value==null?'—':`${fmt(y10.value,2)}%`);setText('macro10yChange',y10.change_5==null?'—':`cambio 5 obs ${fmt(y10.change_5,2)} pp`);
 setText('macroCurve',curve.value==null?'—':`${fmt(curve.value,2)} pp`);setText('macroHy',hy.value==null?'—':`${fmt(hy.value,2)}%`);
}
function renderConditionalOutcomes(r){
 const q=String(r?.statistical_evidence||r?.evidence_quality||r?.quality||'NO_CALIBRATED_VERDICT').toUpperCase();
 setText('conditionalEvidence',q);setText('conditionalEvidenceDetail',r?.reason||r?.note||'Contexto estadístico; Scanner mantiene la dirección');
 const n=Number(r?.samples??r?.sample_size);const sess=Number(r?.oos_sessions??r?.sessions);
 setText('conditionalSamples',Number.isFinite(n)?intfmt(n):'—');setText('conditionalSessions',Number.isFinite(sess)?`${intfmt(sess)} sesiones OOS`:'sesiones comparables —');
 const p=Number(r?.p_t1_before_invalidation);setText('conditionalProb',Number.isFinite(p)?`${fmt(p*100,1)}%`:'—');setText('conditionalProbDetail',`${r?.source||'CALIBRATION'} · ${r?.regime||'regime —'}`);
 const bs=Number(r?.brier_skill),br=Number(r?.brier);setText('conditionalBrier',Number.isFinite(bs)?`${fmt(bs*100,1)}% skill`:Number.isFinite(br)?fmt(br,3):'—');setText('conditionalBrierDetail',`LogLoss ${r?.log_loss==null?'—':fmt(r.log_loss,3)} · base ${r?.base_rate==null?'—':fmt(Number(r.base_rate)*100,1)+'%'}`);
}
function renderLiquidityZones(p){
 const root=el('liquidityZonesGrid');if(!root)return;root.innerHTML='';const zpack=p?.liquidity_zones||{};const zones=Array.isArray(zpack)?zpack:(Array.isArray(zpack.zones)?zpack.zones:[]);
 zones.slice(0,8).forEach(z=>{const d=document.createElement('div');d.className='change-item';d.innerHTML=`<span>${z.side||'ZONE'} · ${z.type||'CARPET'}</span><b>${activeSymbol} ${fmt(z.low??z.center)}–${fmt(z.high??z.center)} · ${money(z.notional||0)}</b><small>Centro ${fmt(z.center)} · score ${fmt(z.score,0)}/100 · ${intfmt(z.prints??z.events??z.count??0)} prints</small>`;root.appendChild(d)});
 if(!root.children.length)root.innerHTML='<div class="muted">Esperando prints suficientes para formar zonas adaptativas.</div>';
}
function renderGammaMigrationDigest(d){
 setText('gammaMigrationState',d?.ready===false?'INSUFICIENTE':`${String(d?.mode||el('gammaMigrationMode')?.value||'DIFFERENCE').toUpperCase()} · ${d?.points??d?.count??0} PTS`);
 const root=el('gammaMigrationDigest');if(!root)return;root.innerHTML='';
 const rows=[['Modo',d?.mode],['Snapshots',d?.timestamps??d?.snapshots],['Strikes',d?.strike_count??(Array.isArray(d?.strikes)?d.strikes.length:d?.strikes)],['Puntos',d?.points??d?.count],['Máx. +',d?.max_positive_m==null?null:`${fmt(d.max_positive_m,3)}M`],['Máx. −',d?.max_negative_m==null?null:`${fmt(d.max_negative_m,3)}M`],['Lectura',d?.interpretation||d?.reading]];
 rows.forEach(([k,v])=>{if(v==null||v==='')return;const x=document.createElement('div');x.className='change-item';x.innerHTML=`<span>${k}</span><b>${typeof v==='number'?Number(v).toLocaleString(undefined,{maximumFractionDigits:3}):v}</b>`;root.appendChild(x)});
 if(!root.children.length)root.innerHTML='<div class="muted">Se necesitan snapshots comparables para calcular migración.</div>';
}
async function loadGexCellInspector(strike,expiration){
 if(!Number.isFinite(Number(strike))||!expiration)return;
 try{const r=await api(`/api/analytics/gex-cell?strike=${encodeURIComponent(strike)}&expiration=${encodeURIComponent(expiration)}`);if(!r)return;setText('gexCellKey',`${activeSymbol} ${fmt(strike)} · ${expiration}`);setText('gexCellExposure',`${r.total_gex==null?'GEX —':exposureExact(r.total_gex)} · ${r.total_dex==null?'DEX —':exposureExact(r.total_dex)}`);setText('gexCellActivity',`OI ${intfmt(r.total_open_interest)} · Vol ${intfmt(r.total_volume)}`);setText('gexCellContracts',`IV ${r.avg_iv==null?'—':fmt(Number(r.avg_iv)*100,2)+'%'} · ${intfmt(r.contract_count)} contratos`);}catch(e){setText('gexCellKey','Drilldown no disponible');}
}
function bindGexMatrixDrilldown(spec){
 const g=el('gexMatrixChart');if(!g)return;g.__itmqGexSpec=spec;if(g.__itmqGexBound)return;g.__itmqGexBound=true;g.on?.('plotly_click',ev=>{const p=ev?.points?.[0];if(!p)return;const meta=g.layout?.meta||g.__itmqGexSpec?.layout?.meta||{};const exp=(meta.expiration_map||{})[String(p.x)]||String(p.x||'');loadGexCellInspector(Number(p.y),exp);});
}
function renderLargePrints(p){
 renderLiquidityZones(p);
 setText('printCount',fmt(p?.count,0));setText('offCount',fmt(p?.off_exchange_count,0));setText('printNotional',money(p?.total_notional||0));setText('offNotional',money(p?.off_exchange_notional||0));
 const x=p?.largest;setText('largestPrint',x?`${money(x.notional)} · QP${fmt(x.q_print,0)}`:'Esperando prints');const rz=(p?.repeated_zones||[])[0];setText('largestPrintVenue',x?`${x.q_print_status||'COLLECTING'} · ${x.class||''} · ${activeSymbol} ${fmt(x.price)} · ${x.pct_adv==null?'ADV —':fmt(x.pct_adv,3)+'% ADV'} · ${x.exchange_name||x.exchange||'—'}${rz?' · repeat '+fmt(rz.price_zone)+' ×'+rz.events:''}`:'—');
}
function renderHighlights(h){const root=el('chainHighlights');root.innerHTML=''; const items=[['CALL MÁS RELEVANTE',h?.top_call],['PUT MÁS RELEVANTE',h?.top_put],['GAMMA HOTSPOT',h?.gamma_hotspot],['DELTA HOTSPOT',h?.delta_hotspot],['ACTIVIDAD INUSUAL',h?.unusual]]; items.forEach(([title,x])=>{const d=document.createElement('div');d.className='highlight-card';d.innerHTML=`<span>${title}</span><b>${x?`${activeSymbol} ${fmt(x.strike)} · ${x.expiration} · Q${fmt(x.qscore,0)}`:'—'}</b>`;root.appendChild(d)});}

function setTraceFollow(on){
 traceFollow=Boolean(on);
 const b=el('traceFollowToggle');
 if(b){b.textContent=traceFollow?'● FOLLOW LIVE':'FOLLOW OFF';b.title=traceFollow?'Mantiene el extremo derecho LIVE según la ventana elegida. Mover/zoom lo apaga.':'Vista manual: TRACE respeta tu viewport.';b.classList.toggle('btn-primary',traceFollow);b.classList.toggle('btn-ghost',!traceFollow);b.classList.toggle('active',traceFollow);}
 if(window.ITMQNextGen?.setFollow)window.ITMQNextGen.setFollow(traceFollow);
}
function readChartControlState(){return{surfaceMetric:el('surfaceMetric')?.value||'Gamma',surfaceRender:el('surfaceRenderStyle')?.value||'Superficie',surfaceOptionView:el('surfaceOptionView')?.value||'Net',surfaceSliceMetric:el('surfaceSliceMetric')?.value||'Gamma',surfaceSliceRender:el('surfaceSliceRender')?.value||'Barras',surfaceView:el('surfaceView')?.value||'expiry',netDriftScope:el('netDriftScope')?.value||'Todas exp.',gammaMigrationMode:el('gammaMigrationMode')?.value||'DIFFERENCE'};}
function chartStateKey(x){return [x.surfaceMetric,x.surfaceRender,x.surfaceOptionView,x.surfaceSliceMetric,x.surfaceSliceRender,x.surfaceView,x.netDriftScope,x.gammaMigrationMode].join('|');}
function updateSurfaceMainState(state=null){const q=state||readChartControlState();setText('surfaceMainState',`${String(q.surfaceMetric).toUpperCase()} · ${String(q.surfaceOptionView).toUpperCase()} · ${String(q.surfaceRender).toUpperCase()}`);}
function updateSurfaceSliceState(){const m=el('surfaceSliceMetric')?.value||'Gamma',r=el('surfaceSliceRender')?.value||'Barras',v=el('surfaceOptionView')?.value||'Net';setText('surfaceSliceState',`${String(m).toUpperCase()} · ${String(v).toUpperCase()} · ${String(r).toUpperCase()}`);updateSurfaceMainState();}
function nodeDetailsFromCustom(cd){const a=Array.isArray(cd)?cd:[];if(a.length<6)return'';const labels=[['Call OI',a[0]],['Put OI',a[1]],['Net OI',a[2]],['Call Vol',a[3]],['Put Vol',a[4]],['Net Vol',a[5]]];return labels.map(([k,v])=>`${k}: ${Number.isFinite(Number(v))?Math.round(Number(v)).toLocaleString('en-US'):'—'}`).join('\n');}
function renderNodeInspector(detail,locked=false){if(!detail)return;if(nodeInspectorLock&&!locked)return;const strike=Number(detail.strike),metric=detail.metric||el('surfaceSliceMetric')?.value||'—',value=Number(detail.value),root=el('nodeInspector');if(locked){nodeInspectorLock={...detail,locked:true};root?.classList.add('locked');}else root?.classList.remove('locked');setText('nodeInspectorState',locked?'LOCKED':'HOVER');setText('nodeInspectorStrike',Number.isFinite(strike)?strike.toFixed(2):'—');setText('nodeInspectorMetric',`${metric}${detail.option_view?` · ${detail.option_view}`:''}`);setText('nodeInspectorValue',Number.isFinite(value)?value.toLocaleString(undefined,{maximumFractionDigits:3}):'—');const extra=nodeDetailsFromCustom(detail.customdata);const unit=detail.unit?`Unidad: ${detail.unit}\n`:'';const render=detail.render?`Render: ${detail.render}\n`:'';const src=detail.source?`Source: ${detail.source}`:'';const x=el('nodeInspectorDetails');if(x)x.textContent=`${unit}${render}${extra}${extra?'\n':''}${src}`.trim()||'Nodo sin detalle adicional.';if(Number.isFinite(strike))window.ITMQNextGen?.highlightStrike?.(strike);}
function clearNodeInspector(){nodeInspectorLock=null;el('nodeInspector')?.classList.remove('locked');setText('nodeInspectorState','WAITING');setText('nodeInspectorStrike','—');setText('nodeInspectorMetric','—');setText('nodeInspectorValue','—');const x=el('nodeInspectorDetails');if(x)x.textContent='Mueve el cursor sobre el Cross-Section o haz clic para fijar un strike y enlazarlo con TRACE.';window.ITMQNextGen?.highlightStrike?.(null);}
function bindSurfaceSlicePlotly(g,spec){if(!g||g.__itmqNodeInspectorBound)return;g.__itmqNodeInspectorBound=true;g.on?.('plotly_hover',ev=>{if(nodeInspectorLock)return;const p=ev?.points?.[0];if(!p)return;const meta=spec?.layout?.meta||{};renderNodeInspector({strike:Number(p.x),value:Number(p.y),metric:meta.metric||p.data?.name||'',option_view:meta.option_view||'',render:meta.render||'',unit:meta.unit||'',customdata:p.customdata||null,source:g.id||'surfaceSliceChart'},false);});g.on?.('plotly_click',ev=>{const p=ev?.points?.[0];if(!p)return;const meta=g.layout?.meta||spec?.layout?.meta||{};renderNodeInspector({strike:Number(p.x),value:Number(p.y),metric:meta.metric||p.data?.name||'',option_view:meta.option_view||'',render:meta.render||'',unit:meta.unit||'',customdata:p.customdata||null,source:g.id||'surfaceSliceChart'},true);});}
function plot(id,spec){
 const g=el(id);if(!g)return;if(!spec){markChartRuntime(id,'EMPTY','EMPTY_STATE','N/A','No chart spec');return;}markChartRuntime(id,'PRESENT','QUEUED','N/A');
 spec=window.ITMQInstitutionalCharts?.normalizeSpec?.(id,spec)||spec;
 if(window.ITMQMarketLineTerminals?.render?.(id,spec)){markChartRuntime(id,'PRESENT','LIGHTWEIGHT_OK','N/A');return;}
 // TRACE/surface and critical 2D analytics use owned renderers.
 if(window.ITMQNextGen&&['traceChart','operativaTraceChart','surfaceChart'].includes(id))return;
 if(window.ITMQUltraCharts?.render?.(id,spec)){markChartRuntime(id,'PRESENT','NATIVE_OK','N/A');return;}
 if(window.ITMQInstitutionalCharts?.render?.(id,spec)){markChartRuntime(id,'PRESENT','INSTITUTIONAL_OK','N/A');return;}
 spec.layout=spec.layout||{};
 Plotly.react(g,spec.data,spec.layout,{responsive:true,displaylogo:false,scrollZoom:true,doubleClick:'reset+autosize',modeBarButtonsToRemove:['lasso2d','select2d','toggleSpikelines'],modeBarButtonsToAdd:[]});markChartRuntime(id,'PRESENT','PLOTLY_OK','N/A');
 if(id==='surfaceSliceChart'||id==='surfaceAltChart')bindSurfaceSlicePlotly(g,spec);
}
function traceProfileIndex(g,needle){if(!g?.data)return -1;return g.data.findIndex(t=>String(t.name||'').includes(needle));}
function pulseSign(v){v=Number(v);return !Number.isFinite(v)?'':v>0?'positive':v<0?'negative':'neutral';}
function setPulseCell(id,value,signValue=null){const x=el(id);if(!x)return;x.textContent=value??'—';x.classList.remove('positive','negative','neutral');if(signValue!=null)x.classList.add(pulseSign(signValue));}
function renderTracePulse(p){
 p=window.ITMQTraceContract?.normalizePulse?window.ITMQTraceContract.normalizePulse(p,'app.renderTracePulse'):p;
 if(!p?.ready)return;const prev=tracePulseLast;tracePulseLast=p;
 const age=Number(p.snapshot_age_seconds||0);const ageTxt=age<60?`${Math.round(age)}s`:`${(age/60).toFixed(1)}m`;
 const pt=prev?new Date(prev.asof||prev.timestamp||0).getTime():NaN,ct=new Date(p.asof||p.timestamp||0).getTime(),dt=Number.isFinite(pt)&&Number.isFinite(ct)&&ct>pt?Math.max(.05,(ct-pt)/1000):null;
 const gPulse=dt?(Number(p.gamma_net_m||0)-Number(prev.gamma_net_m||0))/dt:null,dPulse=dt?(Number(p.delta_net_m||0)-Number(prev.delta_net_m||0))/dt:null,gcPulse=dt?(Number(p.gamma_center||0)-Number(prev.gamma_center||0))/dt:null,dcPulse=dt?(Number(p.delta_center||0)-Number(prev.delta_center||0))/dt:null;
 const opraDelta=prev?Number(p.opra_contracts_5m||0)-Number(prev.opra_contracts_5m||0):null;if(opraDelta>0)window.ITMQLiveScheduler?.noteFlow?.(opraDelta);
 setPulseCell('tracePulseSpot',fmt(p.spot),p.spot-p.snapshot_spot);setText('tracePulseSource',p.source==='LIVE_SPOT_REPRICE'?'LIVE SPOT REPRICE':'SNAPSHOT');
 setPulseCell('traceGammaNet',`${Number(p.gamma_net_m)>=0?'+':''}${fmt(p.gamma_net_m,2)}M`,p.gamma_net_m);setText('traceGammaVelocity',gPulse==null?`vs snapshot ${Number(p.gamma_net_change_m)>=0?'+':''}${fmt(p.gamma_net_change_m,2)}M`:`pulse ${gPulse>=0?'+':''}${fmt(gPulse,3)}M/s · snap ${Number(p.gamma_net_change_m)>=0?'+':''}${fmt(p.gamma_net_change_m,2)}M`);setText('traceGammaCenterLive',fmt(p.gamma_center));
 setPulseCell('traceDeltaNet',`${Number(p.delta_net_m)>=0?'+':''}${fmt(p.delta_net_m,2)}M`,p.delta_net_m);setText('traceDeltaVelocity',dPulse==null?`vs snapshot ${Number(p.delta_net_change_m)>=0?'+':''}${fmt(p.delta_net_change_m,2)}M`:`pulse ${dPulse>=0?'+':''}${fmt(dPulse,3)}M/s · snap ${Number(p.delta_net_change_m)>=0?'+':''}${fmt(p.delta_net_change_m,2)}M`);setText('traceDeltaCenterLive',fmt(p.delta_center));
 const gc=el('traceGammaCenterLive')?.parentElement?.querySelector('small'),dc=el('traceDeltaCenterLive')?.parentElement?.querySelector('small');if(gc&&gcPulse!=null)gc.textContent=`center ${gcPulse>=0?'+':''}${fmt(gcPulse,3)}/s`;if(dc&&dcPulse!=null)dc.textContent=`center ${dcPulse>=0?'+':''}${fmt(dcPulse,3)}/s`;
 setText('traceOpraActivity',intfmt(p.opra_contracts_5m));const dirPrem=Number(p.opra_directional_premium_5m||0),newTxt=opraDelta==null?'':opraDelta>0?` · NEW +${intfmt(opraDelta)}`:opraDelta<0?' · ventana rodando':'';setText('traceOpraCoverage',`${dirPrem>=0?'+':''}${money(dirPrem)} dir. · ${money(p.opra_premium_5m||0)} bruto${newTxt}`);setText('traceSnapshotAge',ageTxt);
 setPulseCell('opGammaNetLive',`${Number(p.gamma_net_m)>=0?'+':''}${fmt(p.gamma_net_m,2)}M`,p.gamma_net_m);setPulseCell('opDeltaNetLive',`${Number(p.delta_net_m)>=0?'+':''}${fmt(p.delta_net_m,2)}M`,p.delta_net_m);setText('opOpraLive',intfmt(p.opra_contracts_5m));setText('opPulseAge',`snapshot ${ageTxt}`);
 ['traceChart','operativaTraceChart'].forEach(id=>updateTraceProfiles(el(id),p));
}
function updateTraceProfiles(g,p){
 if(window.ITMQNextGen?.setProfiles){window.ITMQNextGen.setProfiles(p);return;}
 const rows=window.ITMQTraceContract?.array?window.ITMQTraceContract.array(p?.rows,'profiles.rows','app.updateTraceProfiles'):(Array.isArray(p?.rows)?p.rows:[]);if(!g?.data||!rows.length)return;const y=rows.map(r=>Number(r.strike));
 const custom=rows.map(r=>[Number(r.oi||0),Number(r.volume_snapshot||0),Number(r.opra_contracts_5m||0),Number(r.opra_premium_5m||0),Number(r.opra_net_contracts_5m||0),Number(r.opra_directional_premium_5m||0),Number(r.vanna_1vol_m||0),Number(r.charm_10m_m||0),Number(r.speed_1pct_m||0),Number(r.color_10m_m||0)]);
 const cfg=[['Γ LIVE',rows.map(r=>Number(r.gamma_m)),rows.map(r=>Number(r.gamma_m)>=0?'#22c983':'#ff5268')],['Δ LIVE',rows.map(r=>Number(r.delta_m)),rows.map(r=>Number(r.delta_m)>=0?'#22c983':'#ff5268')]];
 cfg.forEach(([name,x,colors])=>{const idx=traceProfileIndex(g,name);if(idx>=0)Plotly.restyle(g,{x:[x],y:[y],customdata:[custom],'marker.color':[colors]},[idx]);});
 const maxAct=Math.max(1,...rows.map(r=>Number(r.opra_contracts_5m||0)));const sizes=rows.map(r=>{const a=Number(r.opra_contracts_5m||0);return a<=0?0:Math.min(18,4+12*Math.sqrt(a/maxAct));});
 const actColors=rows.map(r=>Number(r.opra_directional_premium_5m||0)>0?'#22c983':Number(r.opra_directional_premium_5m||0)<0?'#ff5268':'#f5c451'); ['Γ · OPRA ACTIVITY','Δ · OPRA ACTIVITY'].forEach(name=>{const idx=traceProfileIndex(g,name);if(idx>=0)Plotly.restyle(g,{x:[rows.map(r=>name.startsWith('Γ')?Number(r.gamma_m):Number(r.delta_m))],y:[y],customdata:[custom],'marker.size':[sizes],'marker.color':[actColors]},[idx]);});
}
async function pollTracePulse(){
 const sec=activeSectionId();if(!['section-trace','section-command'].includes(sec))return;
 if(assetSwitchInProgress||activeSymbolEpoch<0)return;if(lastState?.replay?.mode&&lastState.replay.mode!=='LIVE')return;if(tracePulseBusy)return;tracePulseBusy=true;
 const sym=activeSymbol,epoch=activeSymbolEpoch;
 try{const w=Number(el('traceWindow')?.value||12);const p=await api(`/api/trace/pulse?window=${encodeURIComponent(w)}&expected_symbol=${encodeURIComponent(sym)}${epoch>=0?`&expected_epoch=${encodeURIComponent(epoch)}`:''}`);if(p?.stale)return;if(sym!==activeSymbol||epoch!==activeSymbolEpoch)return;if(p?.symbol&&String(p.symbol).toUpperCase()!==String(sym).toUpperCase())return;if(p?.symbol_epoch!=null&&epoch>=0&&Number(p.symbol_epoch)!==Number(epoch))return;renderTracePulse(p);}catch(e){if(!String(e?.message||e).includes('STALE_')){} }finally{tracePulseBusy=false;}
}
async function loadSurfaceSliceFast(kind='slice'){
 const main=kind==='main';
 if(assetSwitchInProgress||assetQuantWarmup||activeSymbolEpoch<0){if(main)pendingSurfaceMain=true;else pendingSurfaceSlice=true;updateSurfaceMainState();updateSurfaceSliceState();return;}
 const sym=activeSymbol,epoch=activeSymbolEpoch;
 const metric=main?(el('surfaceMetric')?.value||'Gamma'):(el('surfaceSliceMetric')?.value||'Gamma');
 const render=main?(el('surfaceRenderStyle')?.value||'Superficie'):(el('surfaceSliceRender')?.value||'Barras');
 const view=el('surfaceOptionView')?.value||'Net';
 if(main&&render==='Superficie'){
   pendingSurfaceMain=false;updateSurfaceMainState();window.ITMQNextGen?.setSurfacePresentation?.('Superficie');await window.ITMQNextGen?.syncSurfaceControls?.();return;
 }
 const seq=main?++surfaceMainRequestSeq:++surfaceSliceRequestSeq;
 const controller=new AbortController();
 try{
   if(main){try{surfaceMainFetchController?.abort?.();}catch(_){}surfaceMainFetchController=controller;pendingSurfaceMain=false;}
   else{try{surfaceSliceFetchController?.abort?.();}catch(_){}surfaceSliceFetchController=controller;pendingSurfaceSlice=false;}
   if(main)setText('surfaceMainState',`${String(metric).toUpperCase()} · ${String(view).toUpperCase()} · ${String(render).toUpperCase()} · ACTUALIZANDO`);
   else setText('surfaceSliceState',`${String(metric).toUpperCase()} · ${String(view).toUpperCase()} · ${String(render).toUpperCase()} · ACTUALIZANDO`);
   const url=`/api/charts/surface-slice?metric=${encodeURIComponent(metric)}&option_view=${encodeURIComponent(view)}&render_style=${encodeURIComponent(render)}&expected_symbol=${encodeURIComponent(sym)}&expected_epoch=${encodeURIComponent(epoch)}`;
   const r=await api(url,{signal:controller.signal});if(r?.stale||!r?.ready)return;
   if(sym!==activeSymbol||epoch!==activeSymbolEpoch||String(r?.symbol||'').toUpperCase()!==sym)return;
   if(main&&seq!==surfaceMainRequestSeq)return;if(!main&&seq!==surfaceSliceRequestSeq)return;
   const bs=r?.chart_state||{};
   if(String(bs.metric||'')!==String(metric)||String(bs.option_view||'')!==String(view)||String(bs.render||'')!==String(render))return;
   if(main){updateSurfaceMainState();window.ITMQNextGen?.setSurfacePresentation?.(render);plot('surfaceAltChart',r.figure);}
   else{updateSurfaceSliceState();plot('surfaceSliceChart',r.figure);}
   window.ITMQUltraCharts?.redrawAll?.();
 }catch(e){if(e?.name==='AbortError')return;if(!String(e?.message||'').startsWith('STALE_'))showError(e.message);}
 finally{if(main&&surfaceMainFetchController===controller)surfaceMainFetchController=null;if(!main&&surfaceSliceFetchController===controller)surfaceSliceFetchController=null;}
}

function activeSectionId(){return document.querySelector('.page-section.active')?.id||'section-command';}
function chartViewForSection(sec=activeSectionId()){const m={'section-command':'command','section-trace':'trace','section-scanner':'scanner','section-chain':'structure','section-flow':'flow','section-netdrift':'netdrift','section-exposure':'exposure','section-gexmatrix':'gexmatrix','section-vol':'volatility','section-positioning':'positioning','section-macro':'macro','section-prints':'prints','section-surface':'surface','section-equityhub':'equity_hub'};return m[sec]||null;}
function tablesNeeded(sec=activeSectionId()){return ['section-chain','section-flow','section-positioning','section-macro','section-prints','section-auditor'].includes(sec);}
async function loadTablesIfNeeded(){if(!tablesNeeded())return;return loadTables();}
async function loadCharts(){const activeView=chartViewForSection();if(!activeView)return;if(assetSwitchInProgress||assetQuantWarmup||activeSymbolEpoch<0){pendingCharts=true;return;}pendingCharts=false;const requestSeq=++chartRequestSeq;const symbolAtRequest=activeSymbol;const epochAtRequest=activeSymbolEpoch;const requested=readChartControlState();const requestedKey=chartStateKey(requested);try{try{chartFetchController?.abort?.();}catch(_){}const controller=new AbortController();chartFetchController=controller;
 const cm=el('chainMetric')?.value||'Q-Score';const sm=requested.surfaceMetric;const sl=el('traceLandscapeLens')?.value||'Gamma';const ss=el('traceLandscapeScale')?.value||'session';const srs=requested.surfaceRender;const sov=requested.surfaceOptionView;const ssm=requested.surfaceSliceMetric||sm;const ssr=requested.surfaceSliceRender;
 const nd=requested.netDriftScope;const gmmode=requested.gammaMigrationMode;const em=el('exposureMetric')?.value||'Gamma';const pm=el('positioningMetric')?.value||'Delta-adjusted';const vm=el('volumeMetric')?.value||'Calls vs Puts';const gm=el('gexMatrixMetric')?.value||'GEX';const gmb=el('gexMatrixBaseline')?.value||'OPEN';const gmt=el('gexMatrixThreshold')?.value||'AUTO';
 const cacheNow=preferReadyChartCache?'true':'false';const url=`/api/charts?view=${encodeURIComponent(activeView)}&chain_metric=${encodeURIComponent(cm)}&surface_metric=${encodeURIComponent(sm)}&net_drift_scope=${encodeURIComponent(nd)}&exposure_metric=${encodeURIComponent(em)}&positioning_metric=${encodeURIComponent(pm)}&volume_metric=${encodeURIComponent(vm)}&gex_matrix_metric=${encodeURIComponent(gm)}&gex_matrix_baseline=${encodeURIComponent(gmb)}&gex_matrix_threshold=${encodeURIComponent(gmt)}&gamma_migration_mode=${encodeURIComponent(gmmode)}&trace_landscape_lens=${encodeURIComponent(sl)}&trace_landscape_scale=${encodeURIComponent(ss)}&surface_render_style=${encodeURIComponent(srs)}&surface_option_view=${encodeURIComponent(sov)}&surface_slice_metric=${encodeURIComponent(ssm)}&surface_slice_render=${encodeURIComponent(ssr)}&prefer_ready_cache=${cacheNow}&expected_symbol=${encodeURIComponent(symbolAtRequest)}${epochAtRequest>=0?`&expected_epoch=${encodeURIComponent(epochAtRequest)}`:''}`;
 updateSurfaceSliceState();const c=await api(url,{signal:controller.signal});if(c?.stale)return;if(c?.publication_gate)renderFreshnessGate({publication_gate:c.publication_gate,replay:lastState?.replay});if(c?.blocked){publicationBlocked=true;renderFreshnessGate({publication_gate:c.publication_gate,replay:lastState?.replay});return;}preferReadyChartCache=false;if(requestSeq!==chartRequestSeq||symbolAtRequest!==activeSymbol||epochAtRequest!==activeSymbolEpoch||String(c?.symbol||'').toUpperCase()!==symbolAtRequest||chartStateKey(readChartControlState())!==requestedKey){console.debug('[ITM CHART STATE] stale response discarded',requestSeq,requestedKey);return;}chartAppliedSeq=requestSeq;const bs=c?.chart_state||{};const backendKey=[bs.surface_metric,bs.surface_render,bs.surface_option_view,bs.surface_slice_metric,bs.surface_slice_render,requested.surfaceView,bs.net_drift_scope,bs.gamma_migration_mode||requested.gammaMigrationMode].join('|');if(activeView==='surface'&&bs.surface_metric&&backendKey!==requestedKey){console.warn('[ITM CHART STATE] surface backend/control mismatch',{requested,backend:bs});setText('surfaceSliceState','STATE WARN · RESPONSE DESCARTADA');return;}updateSurfaceMainState(requested);
 if(c.trace_orderflow)renderTraceOrderflow(c.trace_orderflow);if(c.scanner)plot('scannerChart',c.scanner);if(c.chain)plot('chainChart',c.chain);
 if(c.flow)plot('flowChart',c.flow);if(c.flow_pro){if(activeSectionId()==='section-flow')plot('flowProChart',c.flow_pro);if(activeSectionId()==='section-trace')plot('traceFlowProChart',c.flow_pro);}
 if(c.net_drift){plot('netDriftChart',c.net_drift);window.ITMQNextGen?.setMiniSpecs?.({netDrift:c.net_drift});}if(c.exposure_strike)plot('exposureChart',c.exposure_strike);if(c.gex_matrix){plot('gexMatrixChart',c.gex_matrix);setTimeout(()=>bindGexMatrixDrilldown(c.gex_matrix),0);}if(c.gamma_migration)plot('gammaMigrationChart',c.gamma_migration);if(c.gamma_migration_digest)renderGammaMigrationDigest(c.gamma_migration_digest);if(c.net_positioning)plot('netPositioningChart',c.net_positioning);if(c.volume){plot('volumeChart',c.volume);renderVolumeSummary(c.volume_summary||{});}if(c.volatility)plot('volChart',c.volatility);if(c.skew)plot('skewChart',c.skew);
 if(c.equity_hub_gamma_model)plot('equityHubGammaModel',c.equity_hub_gamma_model);if(c.equity_hub_monte_carlo){plot('equityHubMonteCarlo',c.equity_hub_monte_carlo);renderMonteCarloLevels(c.equity_hub_monte_carlo_summary);}
 const sv=requested.surfaceView;if(c.trace_landscape||c.surface_main_slice||c.surface_slice){if(sv==='session'&&c.trace_landscape){window.ITMQNextGen?.setSurfacePresentation?.('session');plot('surfaceAltChart',c.trace_landscape);}else if(srs==='Superficie'){window.ITMQNextGen?.setSurfacePresentation?.('Superficie');}else if(c.surface_main_slice||c.surface_slice){window.ITMQNextGen?.setSurfacePresentation?.(srs);plot('surfaceAltChart',c.surface_main_slice||c.surface_slice);}if(sv==='expiry'&&c.surface_slice)plot('surfaceSliceChart',c.surface_slice);}
 if(c.macro)plot('macroChart',c.macro);if(c.large_prints)plot('printsChart',c.large_prints);if(c.net_drift_summary)renderDriftSummary(c.net_drift_summary);if(c.exposure_summary)renderExposureSummary(c.exposure_summary);if(tracePulseLast&&['command','trace'].includes(activeView))renderTracePulse(tracePulseLast);
 if(window.ITMQNextGen&&['command','trace','surface'].includes(activeView)){window.ITMQNextGen.refresh?.();if(activeView==='surface')window.ITMQNextGen.syncSurfaceControls?.();}
 }catch(e){if(e?.name==='AbortError')return;if(!String(e?.message||'').startsWith('STALE_'))showError(e.message)}finally{if(chartFetchController?.signal?.aborted||requestSeq===chartRequestSeq)chartFetchController=null}}

async function pollLiveTicks(){
 const sec=activeSectionId();if(!['section-trace','section-command'].includes(sec))return;
 if(lastState?.replay?.mode&&lastState.replay.mode!=='LIVE')return;if(window.ITMQBinaryTransport?.state?.tickState==='ACTIVE')return;if(liveTickBusy)return;liveTickBusy=true;
 try{const r=await api(`/api/live/ticks?after=${liveTickCursor}`);if(!r||r.symbol!==activeSymbol)return;liveTickCursor=Number(r.last_seq||liveTickCursor);const ticks=r.ticks||[];if(!ticks.length)return;const last=ticks[ticks.length-1];setText('spot',fmt(last.price));setText('routeCurrent',fmt(last.price));setText('tracePulseSpot',fmt(last.price));window.ITMQNextGen?.ingestTicks?.(ticks);
 }catch(e){}finally{liveTickBusy=false;}
}

function syncSurfaceViewControls(){
 const session=(el('surfaceView')?.value||'expiry')==='session';
 el('surfaceMetricWrap')?.classList.toggle('hidden',session);
 el('surfaceOptionViewWrap')?.classList.toggle('hidden',session);
 el('surfaceRenderStyleWrap')?.classList.toggle('hidden',session);
 el('traceLandscapeLensWrap')?.classList.toggle('hidden',!session);
 el('traceLandscapeScaleWrap')?.classList.toggle('hidden',!session);
 el('surfaceSlicePanel')?.classList.toggle('hidden',session);
 el('surfaceExpiryExplain')?.classList.toggle('hidden',session);
 el('surfaceTraceExplain')?.classList.toggle('hidden',!session);
}
function syncTracePriceControls(){
 const style=el('tracePriceStyle')?.value||'Velas japonesas';const wrap=el('traceCandleWrap');const candles=!style.toLowerCase().startsWith('l');if(wrap)wrap.style.display=candles?'grid':'none';const label=el('tracePriceModeLabel');if(label)label.textContent=candles?'VELAS JAPONESAS · EJECUCIÓN / TICK LIVE':'LÍNEA · ESTRUCTURA / SIP TICK STREAM';document.querySelectorAll('[data-trace-price]').forEach(b=>b.classList.toggle('active',b.dataset.tracePrice===style));
}
function syncTraceQuickControls(){
 const tf=el('traceCandle')?.value||'1m',win=String(el('traceTimeWindow')?.value||'60');document.querySelectorAll('[data-trace-tf]').forEach(b=>b.classList.toggle('active',b.dataset.traceTf===tf));document.querySelectorAll('[data-trace-window]').forEach(b=>b.classList.toggle('active',b.dataset.traceWindow===win));const shell=el('traceTerminalShell'),dockOn=Boolean(el('traceFlowExpanded')?.checked);if(shell)shell.classList.toggle('trace-flow-dock-hidden',!dockOn);
}
function updateTraceLayoutSummary(cfg={}){
 const left=String(el('traceLeftProfile')?.value||cfg.leftProfile||'DEX').toUpperCase();
 const right=String(el('traceRightProfile')?.value||cfg.rightProfile||'GEX').toUpperCase();
 const heat=String(el('traceTemporalHeatmap')?.value||cfg.temporalHeatmap||'joint').toUpperCase().replace('JOINT','Γ + Δ');
 const price=String(el('tracePriceStyle')?.value||cfg.priceStyle||'Velas japonesas').toUpperCase().startsWith('L')?'LÍNEA':'VELAS';
 const mode=String(cfg.mode||'institutional').toUpperCase();
 setText('traceLayoutSummary',`${mode} · ${left} IZQ · ${right} DER · ${price} · ${heat}`);
 const why=cfg.mode==='structure'?'Estructura primero: línea, escala Y estructural y heatmap más presente.':cfg.mode==='flow'?'Ejecución primero: velas, microestructura y flow más visibles.':'Balance institucional entre estructura y ejecución.';
 setText('traceLayoutSummaryWhy',why);
}
function applyTraceConfig(cfg,reload=true){
 cfg=cfg||{};const set=(id,v)=>{const x=el(id);if(x&&v!=null)x.value=String(v)};set('traceCandle',cfg.candle);set('traceTimeWindow',cfg.window);set('traceYFrame',cfg.yframe);set('tracePriceStyle',cfg.priceStyle);set('traceWindow',cfg.strikeWindow);set('traceTemporalHeatmap',cfg.temporalHeatmap);set('traceLeftProfile',cfg.leftProfile);set('traceRightProfile',cfg.rightProfile);set('traceValueField',cfg.valueField);set('traceUnusualThreshold',cfg.unusualThreshold);set('traceHeatmapOpacity',cfg.heatmapOpacity);
 if(el('traceKeyLevels')&&cfg.levels!=null)el('traceKeyLevels').checked=Boolean(cfg.levels);if(el('traceExpectedMove')&&cfg.expectedMove!=null)el('traceExpectedMove').checked=Boolean(cfg.expectedMove);if(el('traceFlowExpanded')&&cfg.flow!=null)el('traceFlowExpanded').checked=Boolean(cfg.flow);if(el('traceRouteProjection')&&cfg.routeProjection!=null)el('traceRouteProjection').checked=Boolean(cfg.routeProjection);
 syncTracePriceControls();syncTraceQuickControls();updateTraceLayoutSummary(cfg);setTraceFollow(true);window.ITMQNextGen?.setRouteProjection?.(Boolean(el('traceRouteProjection')?.checked));window.ITMQNextGen?.redrawTrace?.();if(reload)loadCharts();
}
const TRACE_PRESETS={
 pro:{mode:'institutional',candle:'1m',window:'60',yframe:'auto',priceStyle:'Velas japonesas',strikeWindow:'12',leftProfile:'DEX',rightProfile:'GEX',valueField:'GEX',temporalHeatmap:'joint',heatmapOpacity:'42',levels:true,expectedMove:true,flow:true,unusualThreshold:'AUTO',routeProjection:false},
 sniper:{mode:'institutional',candle:'1m',window:'30',yframe:'auto',priceStyle:'Velas japonesas',strikeWindow:'8',leftProfile:'DEX',rightProfile:'GEX',valueField:'DEX',temporalHeatmap:'joint',heatmapOpacity:'34',levels:true,expectedMove:false,flow:true,unusualThreshold:'250000',routeProjection:true},
 structure:{mode:'structure',candle:'5m',window:'120',yframe:'structure',priceStyle:'Línea',strikeWindow:'18',leftProfile:'DEX',rightProfile:'GEX',valueField:'GEX',temporalHeatmap:'gamma',heatmapOpacity:'58',levels:true,expectedMove:true,flow:false,unusualThreshold:'AUTO',routeProjection:false},
 flow:{mode:'flow',candle:'1m',window:'60',yframe:'auto',priceStyle:'Velas japonesas',strikeWindow:'8',leftProfile:'DEX',rightProfile:'GEX',valueField:'OPRA_NET',temporalHeatmap:'delta',heatmapOpacity:'28',levels:true,expectedMove:false,flow:true,unusualThreshold:'250000',routeProjection:true}
};
function applyTracePreset(name){const cfg=TRACE_PRESETS[name]||TRACE_PRESETS.pro;document.querySelectorAll('[data-trace-preset]').forEach(b=>b.classList.toggle('active',b.dataset.tracePreset===name));applyTraceConfig(cfg,true);}
function updateTraceRecommendation(){
 const s=lastState||{},q=s.scanner||{},cmd=s.command||{},spot=Number(s.spot),z=cmd.zone||q.zone||{};let dist=Infinity;const zl=Number(z.low),zh=Number(z.high);if(Number.isFinite(spot)&&Number.isFinite(zl)&&Number.isFinite(zh))dist=spot<zl?zl-spot:spot>zh?spot-zh:0;const em=Number(s.volatility?.expected_move);const near=Number.isFinite(dist)&&(dist===0||(Number.isFinite(em)&&em>0&&dist<=em*0.18));const tape=String(cmd.tape_state||s.trace_orderflow?.confirmation?.state||'').toUpperCase();const edge=String(cmd.actionability_state||q.edge_state||'WAITING').toUpperCase();const vol=String(cmd.vol_regime||s.regime_context?.vol_regime||'').toUpperCase();let preset='pro',why='Equilibrio entre precio LIVE, estructura Γ/Δ y contexto.';if(['ARMED','CONFIRMED'].includes(tape)){preset='flow';why='Precio en/near zona y Tape activo: prioriza microestructura sin perder el Scanner.';}else if(near||edge==='ACTIONABLE'){preset='sniper';why='Precio cerca de la zona operativa: 1m + 30m + AUTO para entrada precisa.';}else if(Number.isFinite(dist)&&Number.isFinite(em)&&em>0&&dist>em*0.35){preset='structure';why='Precio todavía lejos de la zona: conviene estructura 5m/2H antes de afinar la entrada.';}else if(vol.includes('HIGH')||vol.includes('EXPANS')){preset='pro';why='Volatilidad elevada: 1m/1H con escala AUTO para seguir expansión sin abrir demasiado el rango.';}const cfg={...TRACE_PRESETS[preset],preset,why};traceRecommendedConfig=cfg;const names={pro:'PRO AUTO',sniper:'SNIPER',structure:'STRUCTURE',flow:'FLOW'};setText('traceRecommended',`${names[preset]} · ${cfg.candle} · ${cfg.window==='30'?'30m':cfg.window==='60'?'1H':cfg.window==='120'?'2H':'SESIÓN'} · ${String(cfg.yframe).toUpperCase()} · HEATMAP ${String(cfg.temporalHeatmap).toUpperCase()}`);setText('traceRecommendedWhy',why);
}
function toggleTraceFullscreen(force=null){
 const shell=el('traceTerminalShell');if(!shell)return;const on=force==null?!shell.classList.contains('trace-fullscreen'):Boolean(force);shell.classList.toggle('trace-fullscreen',on);document.body.classList.toggle('trace-fullscreen-active',on);const b=el('traceFullscreen');if(b)b.textContent=on?'✕':'⛶';setTimeout(()=>window.ITMQNextGen?.redrawTrace?.(),80);
}
function bindTraceResizeObservers(){
 if(traceResizeBound||typeof ResizeObserver==='undefined')return;traceResizeBound=true;let raf=0;const ro=new ResizeObserver(()=>{cancelAnimationFrame(raf);raf=requestAnimationFrame(()=>window.ITMQNextGen?.redrawTrace?.());});['traceTerminalShell','operativaTraceChart'].forEach(id=>{const x=el(id);if(x)ro.observe(x)});window._itmTraceResizeObserver=ro;
}

function openQuant3D(metric='Gamma'){
 const m=String(metric||'Gamma');if(el('surfaceMetric'))el('surfaceMetric').value=m;if(el('surfaceView'))el('surfaceView').value='expiry';if(el('surfaceRenderStyle'))el('surfaceRenderStyle').value='Superficie';navigateSection('surface');window.ITMQNextGen?.syncSurfaceControls?.();window.ITMQNextGen?.refreshSurface?.({force:true});
}
function row(v){return v==null?'—':v}
async function loadTraceDates(){try{const r=await api('/api/trace/dates');const sel=el('traceDate');if(!sel)return;const cur=sel.value;sel.innerHTML='<option value="">Sesión histórica</option>';const dates=r.dates||[],ready=new Set(r.ready_dates||[]);dates.forEach(d=>{const o=document.createElement('option');o.value=d;o.textContent=ready.has(d)?`${d} · ARCHIVADA`:`${d} · RAW`;sel.appendChild(o)});if([...sel.options].some(o=>o.value===cur))sel.value=cur;const today=localDateISO();if(!el('replayDateCalendar')?.value)el('replayDateCalendar').value=today;}catch(e){}}
async function loadTables(){if(assetSwitchInProgress||!tablesNeeded())return;try{const t=await api('/api/tables'); const lt=el('levelsTable');if(lt){lt.innerHTML='';const order=el('levelsOrder')?.value||'strike';const rows=order==='score'?(t.levels_by_score||[]):(t.levels_by_strike||t.levels||[]);const spot=Number(lastState?.spot);rows.forEach(r=>{const tr=document.createElement('tr');if(Number.isFinite(spot)&&Math.abs(Number(r.strike)-spot)<=0.26)tr.classList.add('near-spot-row');tr.innerHTML=`<td>${fmt(r.strike)}</td><td>${compact(r.signed_gex)}</td><td>${compact(r.delta_exposure)}</td><td>${compact(r.open_interest)}</td><td>${compact(r.option_volume)}</td><td>${fmt(r.gamma_delta_level_score,1)}</td><td>${row(r.state_delta)}</td><td>${fmt(r.state_confidence_delta,1)}</td>`;lt.appendChild(tr)})};
 const ft=el('flowTable');if(ft){ft.innerHTML='';(t.flow_events||[]).filter(r=>Number(r.flow_score||0)>=70||Number(r.flow_score_normalized||0)>=70).slice(0,30).forEach(r=>{const tr=document.createElement('tr');const dt=r.timestamp?new Date(r.timestamp).toLocaleTimeString():'';const sess=flowSessionLabel(r.timestamp);const side=Number(r.direction_sign||0)>0?'BUY':Number(r.direction_sign||0)<0?'SELL':'MIXED';tr.innerHTML=`<td>${dt}</td><td>${sess}</td><td>${side}</td><td>${fmt(r.underlying_price)}</td><td>${fmt(r.strike)}</td><td>${money((r.direction_sign||0)*(r.premium||0))}</td><td>${fmt(r.flow_score,0)}</td><td>${r.flow_score_normalized==null?'—':fmt(r.flow_score_normalized,0)} ${r.flow_score_normalized_ready?'READY':'SHADOW'}</td><td>${r.aggressor||''}</td><td>${r.opening_label||'—'} ${r.opening_probability==null?'':(r.opening_probability_calibrated?fmt(r.opening_probability,0)+'%':'score '+fmt(r.opening_score??r.opening_probability,0)+'/100')}</td><td>${r.estimated_hedge_notional==null?'—':money(r.estimated_hedge_notional)}${r.hedge_confidence==null?'':` · ${fmt(r.hedge_confidence,0)}`}</td>`;ft.appendChild(tr)})}
 const pt=el('printsTable');if(pt){pt.innerHTML='';(t.large_prints||[]).slice(0,50).forEach(r=>{const tr=document.createElement('tr');const dt=r.timestamp?new Date(r.timestamp).toLocaleTimeString():'';tr.innerHTML=`<td>${dt}</td><td>${fmt(r.price)}</td><td>${compact(r.size)}</td><td>${money(r.notional||0)}</td><td>${fmt(r.q_print,0)}</td><td>${r.class||''}</td><td>${r.exchange_name||r.exchange||''}</td><td>${r.conditions||''}</td>`;pt.appendChild(tr)})}
 const mt=el('macroEventsTable');if(mt){mt.innerHTML='';(t.macro_events||[]).slice(0,30).forEach(r=>{const tr=document.createElement('tr');const dt=r.time_ec?new Date(r.time_ec).toLocaleString():'';const high=String(r.impact||'').toUpperCase()==='HIGH';tr.innerHTML=`<td>${dt}</td><td>${r.source||''}</td><td>${r.title||''}</td><td><span class="badge ${high?'badge-warn macro-impact-high':'badge-ok'}">${r.impact||''}</span></td>`;mt.appendChild(tr)})}
 const at=el('auditTable');at.innerHTML='';(t.audit||[]).forEach(r=>{const tr=document.createElement('tr');tr.innerHTML=`<td>${r.check}</td><td><span class="badge ${r.status==='OK'?'badge-ok':'badge-warn'}">${r.status}</span></td><td>${r.detail}</td>`;at.appendChild(tr)});
 }catch(e){showError(e.message)}}
async function refreshState(){if(assetSwitchInProgress)return;const seq=++stateRequestSeq;const expected=activeSymbol;const wasWarm=assetQuantWarmup;try{const s=await api('/api/state');if(seq!==stateRequestSeq)return;const sym=String(s?.active_symbol||expected||'').toUpperCase();if(assetSwitchSeq>0&&activeSymbol&&sym!==activeSymbol){console.debug('[ITM STATE] stale symbol response discarded',sym,activeSymbol);return;}renderState(s);if((wasWarm||pendingCharts||pendingSurfaceMain||pendingSurfaceSlice)&&s?.ready&&sym===activeSymbol){assetQuantWarmup=false;setAssetLoading(false);flushPendingQuantUI();Promise.allSettled([loadTraceDates(),loadTablesIfNeeded()]);window.ITMQNextGen?.refresh?.();if(document.querySelector('.page-section.active')?.id==='section-surface')window.ITMQNextGen?.syncSurfaceControls?.();}}catch(e){showError(e.message)}}
async function fullRefresh(){await refreshState();if(!assetQuantWarmup&&activeSymbolEpoch>=0)await Promise.all([loadCharts(),loadTablesIfNeeded()]);else pendingCharts=true;}

document.querySelectorAll('.nav-btn').forEach(btn=>btn.addEventListener('click',()=>navigateSection(btn.dataset.section)));
document.querySelectorAll('.workspace-btn').forEach(btn=>btn.addEventListener('click',()=>applyWorkspace(btn.dataset.workspace,true)));
document.querySelectorAll('.guide-step').forEach(btn=>btn.addEventListener('click',()=>navigateSection(btn.dataset.go)));document.querySelectorAll('[data-lean-go]').forEach(btn=>btn.addEventListener('click',()=>navigateSection(btn.dataset.leanGo)));document.querySelectorAll('[data-quant3d]').forEach(b=>b.addEventListener('click',()=>openQuant3D(b.dataset.quant3d||'Gamma')));

if(el('easyModeBtn'))el('easyModeBtn').addEventListener('click',()=>applyEasyMode(!easyMode));
if(el('easyToExpert'))el('easyToExpert').addEventListener('click',()=>{applyEasyMode(false);navigateSection('command');setOperativaView('asset');});
if(el('openScannerDetail'))el('openScannerDetail').addEventListener('click',()=>navigateSection('scanner'));
if(el('backToOperativa'))el('backToOperativa').addEventListener('click',()=>navigateSection('command'));
if(el('backToMatrix'))el('backToMatrix').addEventListener('click',()=>{if(el('matrixSurfaceView'))el('matrixSurfaceView').value='matrix';navigateSection('gexmatrix');});
if(el('backToAuditor'))el('backToAuditor').addEventListener('click',()=>{if(el('auditSourceView'))el('auditSourceView').value='auditor';navigateSection('auditor');});
if(el('providerFlowRefresh'))el('providerFlowRefresh').addEventListener('click',()=>refreshProviderFlowHealth(true));
if(el('matrixSurfaceView'))el('matrixSurfaceView').addEventListener('change',()=>{if(el('matrixSurfaceView').value==='surface')navigateSection('surface');else navigateSection('gexmatrix');});
if(el('auditSourceView'))el('auditSourceView').addEventListener('change',()=>{if(el('auditSourceView').value==='infrastructure')navigateSection('infrastructure');else navigateSection('auditor');});
if(el('quickAssetSelect'))el('quickAssetSelect').addEventListener('change',()=>{const a=assetCatalog.find(x=>x.symbol===el('quickAssetSelect').value);if(a)selectAsset(a)});
if(el('symbolSearchBtn'))el('symbolSearchBtn').addEventListener('click',openSymbolSearch);if(el('symbolSearchClose'))el('symbolSearchClose').addEventListener('click',closeSymbolSearch);el('symbolSearchModal')?.querySelector('.symbol-search-backdrop')?.addEventListener('click',closeSymbolSearch);if(el('symbolSearchInput'))el('symbolSearchInput').addEventListener('input',renderSymbolSearchResults);document.querySelectorAll('[data-symbol-category]').forEach(b=>b.addEventListener('click',()=>{symbolSearchCategory=b.dataset.symbolCategory||'Todos';document.querySelectorAll('[data-symbol-category]').forEach(x=>x.classList.toggle('active',x===b));renderSymbolSearchResults();}));document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&String(e.key).toLowerCase()==='k'){e.preventDefault();openSymbolSearch();}else if(e.key==='Escape'&&!el('symbolSearchModal')?.classList.contains('hidden'))closeSymbolSearch();});
window.addEventListener('itmq:select-asset',e=>{const symbol=String(e?.detail?.symbol||'').toUpperCase();const a=assetCatalog.find(x=>x.symbol===symbol);if(a&&a.selectable!==false)selectAsset(a);});
if(el('opViewBoard'))el('opViewBoard').addEventListener('click',()=>setOperativaView('board'));if(el('opViewAsset'))el('opViewAsset').addEventListener('click',()=>setOperativaView('asset'));
if(el('replayLiveBtn'))el('replayLiveBtn').addEventListener('click',exitReplay);if(el('researchRangeBtn'))el('researchRangeBtn').addEventListener('click',requestResearchRange);
if(el('replayTimeline')){el('replayTimeline').addEventListener('input',e=>{const i=Math.max(0,Math.min(replayClockMarks.length-1,Number(e.target.value)||0));replayClockIndex=i;setText('replayTimelineLabel',replayDisplayMark(replayClockMarks[i]));});el('replayTimeline').addEventListener('change',e=>applyReplayClockIndex(Number(e.target.value)||0));}
if(el('replayStepBack'))el('replayStepBack').addEventListener('click',()=>stepReplay(-1));if(el('replayStepForward'))el('replayStepForward').addEventListener('click',()=>stepReplay(1));if(el('replayPlayPause'))el('replayPlayPause').addEventListener('click',toggleReplayPlayback);if(el('replaySpeed'))el('replaySpeed').addEventListener('change',()=>{});
const ews=el('expiryWindow');if(ews)ews.addEventListener('change',()=>selectExpiryWindow(ews.value));
const tews=el('traceExpiryWindow');if(tews)tews.addEventListener('change',()=>selectExpiryWindow(tews.value));
el('chainMetric').addEventListener('change',loadCharts);el('surfaceMetric').addEventListener('change',()=>{updateSurfaceMainState();loadSurfaceSliceFast('main');});['surfaceView','traceLandscapeLens','traceLandscapeScale'].forEach(id=>{const x=el(id);if(x)x.addEventListener('change',()=>{syncSurfaceViewControls();updateSurfaceSliceState();loadCharts();})});const srsCtl=el('surfaceRenderStyle');if(srsCtl)srsCtl.addEventListener('change',()=>{syncSurfaceViewControls();updateSurfaceMainState();loadSurfaceSliceFast('main');});const sovCtl=el('surfaceOptionView');if(sovCtl)sovCtl.addEventListener('change',()=>{updateSurfaceSliceState();loadSurfaceSliceFast('main');loadSurfaceSliceFast('slice');});['surfaceSliceMetric','surfaceSliceRender'].forEach(id=>{const x=el(id);if(x)x.addEventListener('change',()=>{updateSurfaceSliceState();loadSurfaceSliceFast('slice');})});const nds=el('netDriftScope');if(nds)nds.addEventListener('change',loadCharts);const exm=el('exposureMetric');if(exm)exm.addEventListener('change',loadCharts);const pom=el('positioningMetric');if(pom)pom.addEventListener('change',loadCharts);const vom=el('volumeMetric');if(vom)vom.addEventListener('change',loadCharts);const gmm=el('gexMatrixMetric');if(gmm)gmm.addEventListener('change',loadCharts);const gmb=el('gexMatrixBaseline');if(gmb)gmb.addEventListener('change',loadCharts);const gmt=el('gexMatrixThreshold');if(gmt)gmt.addEventListener('change',loadCharts);const gmig=el('gammaMigrationMode');if(gmig)gmig.addEventListener('change',loadCharts);const lvo=el('levelsOrder');if(lvo)lvo.addEventListener('change',loadTables);
['traceLeftProfile','traceRightProfile','traceValueField','traceUnusualThreshold'].forEach(id=>{const x=el(id);if(x)x.addEventListener('change',()=>{updateTraceLayoutSummary();window.ITMQNextGen?.redrawTrace?.();});});
const themeBtn=el('themeToggle');if(themeBtn)themeBtn.addEventListener('click',()=>applyInstitutionalTheme(document.documentElement.dataset.theme==='light'?'dark':'light'));
['traceCandle','traceWindow','traceTimeWindow'].forEach(id=>{const x=el(id);if(x)x.addEventListener('change',()=>{syncTracePriceControls();syncTraceQuickControls();setTraceFollow(true);loadCharts();})});
['tracePriceStyle','traceYFrame','traceFlowExpanded'].forEach(id=>{const x=el(id);if(x)x.addEventListener('change',()=>{if(id==='tracePriceStyle')syncTracePriceControls();syncTraceQuickControls();updateTraceLayoutSummary();window.ITMQNextGen?.redrawTrace?.();})});
if(window.ITMQBus){window.ITMQBus.addEventListener('itmq:node-hover',e=>renderNodeInspector(e.detail,false));window.ITMQBus.addEventListener('itmq:node-inspect',e=>renderNodeInspector(e.detail,true));}
el('nodeInspectorClear')?.addEventListener('click',clearNodeInspector);
updateSurfaceSliceState();
const tfollow=el('traceFollowToggle');if(tfollow)tfollow.addEventListener('click',()=>setTraceFollow(!traceFollow));const troute=el('traceRouteProjection');if(troute)troute.addEventListener('change',()=>window.ITMQNextGen?.setRouteProjection?.(troute.checked));
const tcenter=el('traceCenterPrice');if(tcenter)tcenter.addEventListener('click',()=>{setTraceFollow(true);if(window.ITMQNextGen?.resetTrace)window.ITMQNextGen.resetTrace(true);else loadCharts();});
const tback=el('traceBackLive');if(tback)tback.addEventListener('click',async()=>{setTraceFollow(true);if(lastState?.replay?.mode&&lastState.replay.mode!=='LIVE')await exitReplay();else await loadCharts();});
document.querySelectorAll('[data-trace-preset]').forEach(b=>b.addEventListener('click',()=>applyTracePreset(b.dataset.tracePreset)));
document.querySelectorAll('[data-trace-tf]').forEach(b=>b.addEventListener('click',()=>{if(el('traceCandle'))el('traceCandle').value=b.dataset.traceTf;syncTracePriceControls();syncTraceQuickControls();setTraceFollow(true);loadCharts();}));
document.querySelectorAll('[data-trace-price]').forEach(b=>b.addEventListener('click',()=>{if(el('tracePriceStyle'))el('tracePriceStyle').value=b.dataset.tracePrice;document.querySelectorAll('[data-trace-price]').forEach(x=>x.classList.toggle('active',x===b));syncTracePriceControls();window.ITMQNextGen?.redrawTrace?.();}));
document.querySelectorAll('[data-trace-window]').forEach(b=>b.addEventListener('click',()=>{if(el('traceTimeWindow'))el('traceTimeWindow').value=b.dataset.traceWindow;syncTraceQuickControls();setTraceFollow(true);loadCharts();}));
if(el('traceApplyRecommended'))el('traceApplyRecommended').addEventListener('click',()=>{const p=traceRecommendedConfig?.preset||'pro';document.querySelectorAll('[data-trace-preset]').forEach(b=>b.classList.toggle('active',b.dataset.tracePreset===p));applyTraceConfig(traceRecommendedConfig,true);});
updateTraceLayoutSummary(TRACE_PRESETS.pro);
if(el('traceFullscreen'))el('traceFullscreen').addEventListener('click',()=>toggleTraceFullscreen());document.addEventListener('keydown',e=>{if(e.key==='Escape')toggleTraceFullscreen(false);});

el('refreshBtn').addEventListener('click',async()=>{el('refreshBtn').textContent='Actualizando…';try{await api('/api/refresh',{method:'POST'});await fullRefresh();}catch(e){showError(e.message)}finally{el('refreshBtn').textContent='Actualizar ahora'}});
if(el('premarketRouteToggle'))el('premarketRouteToggle').addEventListener('change',()=>drawProjectedRouteCanvas(lastPremarketAnalysis));
window.addEventListener('resize',()=>{if(lastPremarketAnalysis)drawProjectedRouteCanvas(lastPremarketAnalysis)});
window.addEventListener('itmq:surface-control-change',()=>{updateSurfaceMainState();loadSurfaceSliceFast('main');});
if(el('analyzePremarketBtn'))el('analyzePremarketBtn').addEventListener('click',async()=>{const b=el('analyzePremarketBtn');b.disabled=true;b.textContent='ANALIZANDO…';try{const r=await api('/api/premarket/analyze',{method:'POST'});renderPremarketAnalysis(r.premarket_analysis);await refreshState();}catch(e){showError(e.message)}finally{b.disabled=false;b.textContent='ANALIZAR PREMARKET'}});
el('freezeBtn').addEventListener('click',async()=>{try{const r=await api('/api/premarket/freeze',{method:'POST'});renderPremarket(r.premarket)}catch(e){showError(e.message)}});
window.addEventListener('itmq:binary-ticks',ev=>{try{const p=ev.detail||{},ticks=p.ticks||[];if(!ticks.length)return;liveTickCursor=Number(p.last_seq||liveTickCursor);const last=ticks[ticks.length-1];setText('spot',fmt(last.price));setText('routeCurrent',fmt(last.price));setText('tracePulseSpot',fmt(last.price));/* NextGen owns tick ingestion; this listener updates shell DOM only. */}catch(_){}});
initInstitutionalTheme();bindReplayCalendar();applyInternalMode();applyEasyMode(false);applyWorkspace('trader',false);navigateSection('command');setOperativaView('asset');syncTracePriceControls();syncTraceQuickControls();syncSurfaceViewControls();bindTraceResizeObservers();loadTraceDates();fullRefresh().then(()=>{loadAssetCatalog(false);if(lastState?.progressive_switch&&!lastState?.ready)hydrateSelectedAsset(String(lastState.active_symbol||activeSymbol).toUpperCase(),Number(lastState.symbol_epoch??activeSymbolEpoch),assetSwitchSeq);pollTracePulse();});setInterval(pollLiveTicks,350);if(window.ITMQLiveScheduler?.loop)window.ITMQLiveScheduler.loop('trace-pulse',pollTracePulse,'pulse',()=>!lastState?.replay?.mode||lastState.replay.mode==='LIVE');else setInterval(pollTracePulse,1000);setInterval(refreshState,15000);setInterval(refreshBoard,8000);setInterval(()=>{const sec=document.querySelector('.page-section.active')?.id;if(['section-trace','section-command'].includes(sec))loadCharts();},15000);setInterval(()=>{const sec=document.querySelector('.page-section.active')?.id;if(['section-scanner','section-chain','section-flow','section-netdrift','section-exposure','section-gexmatrix','section-positioning','section-vol','section-surface','section-macro','section-prints','section-equityhub'].includes(sec))loadCharts();if(['section-positioning','section-flow','section-auditor','section-macro','section-prints'].includes(sec))loadTables();},30000);

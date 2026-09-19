/* ITM QUANT v1.40.0 · MARKET LINE TERMINALS
 * TradingView/TRACE-style Lightweight Charts workspaces for:
 * - Unusual Flow
 * - Net Drift
 * - Large Prints
 * Presentation only. Scanner keeps sole directional authority.
 */
(()=>{
'use strict';
const L=()=>window.LightweightCharts;
const TARGETS=new Set(['flowProChart','traceFlowProChart','netDriftChart','printsChart']);
const charts=new Map();
const toTime=v=>{const ms=new Date(v).getTime();return Number.isFinite(ms)?Math.floor(ms/1000):null;};
const num=(v,d=null)=>{v=Number(v);return Number.isFinite(v)?v:d};
const compact=v=>{v=Number(v);if(!Number.isFinite(v))return'—';const a=Math.abs(v),s=v<0?'-':'';if(a>=1e9)return`${s}${(a/1e9).toFixed(2)}B`;if(a>=1e6)return`${s}${(a/1e6).toFixed(2)}M`;if(a>=1e3)return`${s}${(a/1e3).toFixed(1)}K`;return`${s}${a.toFixed(2)}`;};
const theme=()=>document.documentElement.dataset.theme==='light'?{
 bg:'#f8fafc',text:'#526476',grid:'rgba(15,23,42,.08)',border:'rgba(51,65,85,.18)',cross:'rgba(51,65,85,.42)',price:'#243447',cyan:'#0284c7',pink:'#c02670',green:'#059669',gold:'#d97706',white:'#334155',hist:'#0ea5e9'
}:{bg:'#050a10',text:'#8195a5',grid:'rgba(112,149,176,.08)',border:'rgba(112,149,176,.18)',cross:'rgba(178,210,232,.34)',price:'#d7e5ee',cyan:'#36c8f4',pink:'#e850c7',green:'#22c983',gold:'#e1ad3d',white:'#f0f4f8',hist:'#38bdf8'};
function seriesRole(t){return String(t?.meta?.role||'').toUpperCase();}
function traceName(t){return String(t?.name||'SERIE');}
function normalizeLine(t){
 const xs=Array.isArray(t?.x)?t.x:[],ys=Array.isArray(t?.y)?t.y:[],rows=[];let prev=null;
 const gap=Number(t?.meta?.gap_threshold_seconds||300);
 for(let i=0;i<Math.min(xs.length,ys.length);i++){
  const time=toTime(xs[i]);if(time==null)continue;const value=num(ys[i],null);
  if(prev!=null&&time-prev>gap){const wt=Math.min(time-1,prev+Math.max(1,Math.floor(gap)));if(wt>prev)rows.push({time:wt});}
  if(value==null)rows.push({time});else rows.push({time,value});prev=time;
 }
 rows.sort((a,b)=>a.time-b.time);const out=[];let last=-Infinity;for(const r of rows){if(r.time===last){out[out.length-1]=r;}else{out.push(r);last=r.time;}}return out;
}
function normalizeHistogram(t){const xs=Array.isArray(t?.x)?t.x:[],ys=Array.isArray(t?.y)?t.y:[],out=[];for(let i=0;i<Math.min(xs.length,ys.length);i++){const time=toTime(xs[i]),value=num(ys[i],null);if(time==null||value==null)continue;out.push({time,value,color:Array.isArray(t?.marker?.color)?t.marker.color[i]:undefined});}out.sort((a,b)=>a.time-b.time);return out;}
function visibleValue(data){for(let i=data.length-1;i>=0;i--)if(Number.isFinite(Number(data[i]?.value)))return Number(data[i].value);return null;}
class Pane{
 constructor(root,{height=240,timeVisible=false,label=''}={}){this.root=root;this.series=[];this.label=label;const T=theme();this.host=document.createElement('div');this.host.className='market-terminal-pane';this.host.style.height=`${height}px`;this.header=document.createElement('div');this.header.className='market-terminal-pane-header';this.header.innerHTML=`<b>${label}</b><span></span>`;this.host.appendChild(this.header);this.chartHost=document.createElement('div');this.chartHost.className='market-terminal-chart';this.host.appendChild(this.chartHost);root.appendChild(this.host);const lib=L();this.chart=lib.createChart(this.chartHost,{layout:{background:{type:'solid',color:T.bg},textColor:T.text,attributionLogo:true},grid:{vertLines:{color:T.grid},horzLines:{color:T.grid}},rightPriceScale:{visible:true,borderColor:T.border,scaleMargins:{top:.12,bottom:.12}},leftPriceScale:{visible:false},timeScale:{visible:timeVisible,borderColor:T.border,timeVisible:true,secondsVisible:false,rightOffset:6,barSpacing:8},crosshair:{mode:lib.CrosshairMode?.Normal??0,vertLine:{color:T.cross,width:1,style:2,labelVisible:timeVisible},horzLine:{color:T.cross,width:1,style:2,labelVisible:true}},handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:false},handleScale:{axisPressedMouseMove:true,mouseWheel:true,pinch:true},kineticScroll:{mouse:true,touch:true}});this.ro=new ResizeObserver(()=>this.resize());this.ro.observe(this.chartHost);this.resize();this.chart.subscribeCrosshairMove?.(p=>this.onCrosshair(p));}
 resize(){const r=this.chartHost.getBoundingClientRect();this.chart.resize(Math.max(100,Math.floor(r.width)),Math.max(80,Math.floor(r.height)));}
 addLine(name,data,color,width=2,dash='solid'){
  const lib=L();const style=dash==='dot'?(lib.LineStyle?.Dotted??1):dash==='dash'?(lib.LineStyle?.Dashed??2):(lib.LineStyle?.Solid??0);const s=this.chart.addSeries(lib.LineSeries,{color,lineWidth:width,lineStyle:style,lastValueVisible:true,priceLineVisible:false,crosshairMarkerVisible:true,crosshairMarkerRadius:3,title:name});s.setData(data);this.series.push({name,series:s,data,color});return s;
 }
 addHistogram(name,data,color){const lib=L();if(!lib.HistogramSeries)return null;const s=this.chart.addSeries(lib.HistogramSeries,{color,priceFormat:{type:'volume'},lastValueVisible:true,priceLineVisible:false,title:name});s.setData(data.map(x=>({...x,color:x.color||color})));this.series.push({name,series:s,data,color});return s;}
 addPriceLine(series,price,title,color){if(!series||!Number.isFinite(Number(price)))return null;try{return series.createPriceLine({price:Number(price),color,lineWidth:1,lineStyle:L()?.LineStyle?.Dashed??2,axisLabelVisible:true,title:String(title||'')});}catch(_){return null;}}
 onCrosshair(param){const values=[];for(const s of this.series){const v=param?.seriesData?.get?.(s.series);const val=v&&('value'in v?v.value:'close'in v?v.close:null);if(Number.isFinite(Number(val)))values.push(`${s.name} ${compact(val)}`);}const span=this.header.querySelector('span');if(span)span.textContent=values.join(' · ');}
 latestSummary(){return this.series.map(s=>{const v=visibleValue(s.data);return v==null?'':`${s.name} ${compact(v)}`;}).filter(Boolean).join(' · ');}
 dispose(){try{this.ro?.disconnect?.();}catch(_){}try{this.chart?.remove?.();}catch(_){}this.host.remove();}
}
class Terminal{
 constructor(host,id){this.host=host;this.id=id;this.panes=[];this.guard=false;this.lastSignature='';host.classList.add('market-line-terminal-host');}
 clear(){for(const p of this.panes)p.dispose();this.panes=[];this.host.innerHTML='';}
 pane(opts){const p=new Pane(this.host,opts);this.panes.push(p);return p;}
 // Las alturas estaban fijas en píxeles y su suma superaba la del contenedor
 // (.flow-pro-chart mide 900 px y los cuatro paneles sumaban 957), así que con
 // `overflow:hidden` el último quedaba recortado por abajo. Repartir por pesos usa
 // exactamente el alto disponible, en cualquier pantalla y sin recortes.
 layout(weights){
  const total=weights.reduce((a,b)=>a+b,0)||1;
  const avail=Math.max(320,Math.floor(this.host?.clientHeight||0)||0);
  const headers=weights.length*26;                 // cabecera de cada panel
  const usable=Math.max(240,avail-headers);
  const out=weights.map(w=>Math.max(64,Math.floor(usable*w/total)));
  const drift=usable-out.reduce((a,b)=>a+b,0);
  out[0]=Math.max(120,out[0]+drift);               // el residuo va al panel de precio
  return out;
 }
 sync(){for(const source of this.panes){source.chart.timeScale().subscribeVisibleTimeRangeChange?.(range=>{if(this.guard||!range)return;this.guard=true;try{for(const p of this.panes)if(p!==source)p.chart.timeScale().setVisibleRange?.(range);}catch(_){}finally{this.guard=false;}});}}
 fit(){for(const p of this.panes)try{p.chart.timeScale().fitContent();}catch(_){} }
 render(spec){const sig=JSON.stringify((spec?.data||[]).map(t=>[t.name,t.type,t.meta?.role,(t.x||[]).length,(t.x||[]).at?.(-1),(t.y||[]).at?.(-1)]));if(sig===this.lastSignature)return true;this.lastSignature=sig;this.clear();if(this.id==='flowProChart'||this.id==='traceFlowProChart')this.renderFlow(spec);else if(this.id==='netDriftChart')this.renderDrift(spec);else if(this.id==='printsChart')this.renderPrints(spec);this.sync();this.fit();return true;}
 renderFlow(spec){
  const T=theme(),data=Array.isArray(spec?.data)?spec.data:[];
  const price=data.find(t=>seriesRole(t)==='UNDERLYING_PRICE');
  const events=data.find(t=>seriesRole(t)==='FLOW_EVENT');
  const levels=data.filter(t=>seriesRole(t)==='LEVEL_LINE');
  const aggressor=data.find(t=>seriesRole(t)==='AGGRESSOR_BAR');
  const total=data.find(t=>seriesRole(t)==='TOTAL_BAR');
  const netFlow=data.find(t=>seriesRole(t)==='NET_FLOW_BAR');
  
  const H=this.layout([52,7,20,21]);
  const p0=this.pane({height:H[0],timeVisible:false,label:'PRECIO'});
  const flowPrice=(document.documentElement.dataset.theme==='light'?'#5369da':'#7d8fe8');
  const priceSeries=price?p0.addLine(traceName(price),normalizeLine(price),flowPrice,2):null;
  if(priceSeries){
   levels.forEach(t=>{const m=t?.meta||{},v=num(m.level_value,Array.isArray(t?.y)?num(t.y[0],null):null);if(v==null)return;const typ=String(m.level_type||traceName(t)).toUpperCase();const short=String(m.level_short||typ);const color=typ.includes('CALL')?T.green:typ.includes('PUT')?T.pink:'#3284a8';p0.addPriceLine(priceSeries,v,`${short} ${Number(v).toFixed(2)}`,color);});
  }
  if(events&&priceSeries&&L()?.createSeriesMarkers){
   const xs=events.x||[],ys=events.y||[],cd=events.customdata||[],markers=[];let outside=0;
   const priceVals=(price?.y||[]).map(Number).filter(Number.isFinite);const lo=priceVals.length?Math.min(...priceVals):null,hi=priceVals.length?Math.max(...priceVals):null;
   for(let i=0;i<Math.min(xs.length,ys.length);i++){
    const time=toTime(xs[i]),v=num(ys[i],null);if(time==null||v==null)continue;
    const row=Array.isArray(cd[i])?cd[i]:[],amount=String(row[0]||''),side=String(row[1]||'').toUpperCase();
    if(lo!=null&&hi!=null&&(v<lo||v>hi))outside++;
    markers.push({time,position:'aboveBar',color:T.gold,shape:'circle',text:amount});
    markers.push({time,position:'aboveBar',color:side==='BUY'?T.green:T.pink,shape:side==='BUY'?'arrowUp':'arrowDown',text:''});
   }
   try{L().createSeriesMarkers(priceSeries,markers);}catch(_){}
   const span=p0.header.querySelector('span');if(span)span.textContent=outside?`≡ · ${outside} fuera`:'≡ · 0 fuera';
  }

  const p1=this.pane({height:H[1],timeVisible:false,label:'AGRESOR'});
  if(aggressor)p1.addHistogram('AGRESOR',normalizeHistogram(aggressor),T.green);else p1.header.querySelector('span').textContent='ESPERANDO';
  const p2=this.pane({height:H[2],timeVisible:false,label:'TOTAL'});
  if(total){const td=normalizeHistogram(total),ts=p2.addHistogram('TOTAL',td,T.gold);if(ts&&L()?.createSeriesMarkers){const vals=td.map(x=>Math.abs(Number(x.value)||0)).filter(Number.isFinite).sort((a,b)=>a-b),cut=vals.length?vals[Math.max(0,Math.floor(vals.length*.80)-1)]:Infinity;const marks=td.filter(x=>Math.abs(Number(x.value)||0)>=cut&&Math.abs(Number(x.value)||0)>0).slice(-16).map(x=>({time:x.time,position:'aboveBar',color:T.gold,shape:'circle',text:`$${compact(Math.abs(Number(x.value)||0))}M`}));try{L().createSeriesMarkers(ts,marks);}catch(_){}}}else p2.header.querySelector('span').textContent='ESPERANDO';
  const p3=this.pane({height:H[3],timeVisible:true,label:'NET FLOW'});
  if(netFlow)p3.addHistogram('NET FLOW',normalizeHistogram(netFlow),T.green);else p3.header.querySelector('span').textContent='ESPERANDO';
 }
 renderDrift(spec){const T=theme(),data=Array.isArray(spec?.data)?spec.data:[];const price=data.find(t=>seriesRole(t)==='UNDERLYING_PRICE');const drift=data.filter(t=>seriesRole(t)==='DRIFT_LINE');const flow=data.filter(t=>seriesRole(t)==='FLOW_LINE');
  const D=this.layout([48,30,22]);const p0=this.pane({height:D[0],timeVisible:false,label:'PRECIO · TRACE STYLE'});if(price)p0.addLine(traceName(price),normalizeLine(price),T.price,2);
  const p1=this.pane({height:D[1],timeVisible:false,label:'NET DRIFT · ESTRUCTURA'});const cols=[T.cyan,T.pink,T.white,T.green];drift.forEach((t,i)=>p1.addLine(traceName(t),normalizeLine(t),cols[i%cols.length],i>=2?2.4:1.9,String(t?.line?.dash||'solid')));
  const p2=this.pane({height:D[2],timeVisible:true,label:'Q-FLOW · ACUMULADO'});flow.forEach((t,i)=>p2.addLine(traceName(t),normalizeLine(t),T.gold,2.2,String(t?.line?.dash||'solid')));
 }
 renderPrints(spec){const T=theme(),data=Array.isArray(spec?.data)?spec.data:[];const price=data.find(t=>seriesRole(t)==='UNDERLYING_PRICE'||(String(t.type||'scatter')==='scatter'&&String(t.mode||'').includes('lines')));const points=data.find(t=>seriesRole(t)==='PRINT_EVENT'||/prints/i.test(traceName(t)));const bars=data.find(t=>seriesRole(t)==='PRINT_NOTIONAL'||String(t.type||'').toLowerCase()==='bar');
  const P=this.layout([62,38]);const p0=this.pane({height:P[0],timeVisible:false,label:'PRECIO + LARGE PRINTS'});if(price)p0.addLine(traceName(price),normalizeLine(price),T.price,2);
  if(points&&p0.series[0]&&L()?.createSeriesMarkers){const xs=points.x||[],ys=points.y||[],cd=points.customdata||[],markers=[];for(let i=0;i<Math.min(xs.length,ys.length);i++){const time=toTime(xs[i]),v=num(ys[i],null);if(time==null||v==null)continue;const label=Array.isArray(cd[i])?String(cd[i][0]||''):'';markers.push({time,position:'aboveBar',color:T.cyan,shape:'circle',text:label.length<=12?label:''});}try{L().createSeriesMarkers(p0.series[0].series,markers);}catch(_){}}
  const p1=this.pane({height:P[1],timeVisible:true,label:'PRINT NOTIONAL · $M'});if(bars)p1.addHistogram(traceName(bars),normalizeHistogram(bars),T.hist);
 }
}

function cleanPlotlyFallback(id,spec){
 const host=document.getElementById(id),P=window.Plotly;if(!host||!P?.react)return false;
 const src=Array.isArray(spec?.data)?spec.data:[],T=theme(),copy=t=>({...t,line:t?.line?{...t.line}:undefined,marker:t?.marker?{...t.marker}:undefined});
 const price=src.find(t=>seriesRole(t)==='UNDERLYING_PRICE');
 const base={paper_bgcolor:T.bg,plot_bgcolor:T.bg,font:{color:T.text,family:'ui-monospace,Consolas,monospace',size:10},hovermode:'x unified',dragmode:'pan',showlegend:true,legend:{orientation:'h',x:0,y:1.02},margin:{l:52,r:48,t:38,b:42},uirevision:`market-terminal-${id}`,xaxis:{domain:[0,1],showgrid:true,gridcolor:T.grid,zeroline:false,rangeslider:{visible:false},fixedrange:false},yaxis:{showgrid:true,gridcolor:T.grid,zeroline:false,fixedrange:false},modebar:{orientation:'v'}};
 let traces=[],layout={...base};
 if(id==='flowProChart'||id==='traceFlowProChart'){
   const role=r=>src.find(t=>seriesRole(t)===r), priceT=role('UNDERLYING_PRICE'), ev=role('FLOW_EVENT');
   const flowAnn=[];
   if(priceT){const t=copy(priceT);t.xaxis='x';t.yaxis='y';t.line={...(t.line||{}),color:(document.documentElement.dataset.theme==='light'?'#5369da':'#7d8fe8'),width:2};traces.push(t);}
   if(ev){const t=copy(ev);t.xaxis='x';t.yaxis='y';t.marker={...(t.marker||{}),line:{color:T.gold,width:2}};traces.push(t);
     const py=(priceT?.y||[]).map(Number).filter(Number.isFinite),lo=py.length?Math.min(...py):null,hi=py.length?Math.max(...py):null;
     let outside=0;(ev.y||[]).forEach(v=>{v=Number(v);if(Number.isFinite(v)&&lo!=null&&hi!=null&&(v<lo||v>hi))outside++;});
     if(outside)flowAnn.push({text:`≡ · ${outside} fuera`,xref:'paper',yref:'paper',x:.995,y:1.025,xanchor:'right',showarrow:false,font:{color:T.text,size:10}});
   }
   src.filter(t=>seriesRole(t)==='LEVEL_LINE').forEach(q=>{const t=copy(q);t.xaxis='x';t.yaxis='y';traces.push(t);const m=q?.meta||{},v=num(m.level_value,Array.isArray(q?.y)?num(q.y[0],null):null);if(v!=null){const short=String(m.level_short||q.name||'').toUpperCase(),col=String(q?.line?.color||T.cyan);flowAnn.push({text:`<b>${short} ${Number(v).toFixed(2)}</b>`,xref:'paper',yref:'y',x:.002,y:v,xanchor:'left',yanchor:'middle',showarrow:false,bgcolor:col,bordercolor:col,font:{color:'#fff',size:9},borderpad:2});}});
   [['AGGRESSOR_BAR','x2','y2'],['TOTAL_BAR','x3','y3'],['NET_FLOW_BAR','x4','y4']].forEach(([r,x,y])=>{const q=role(r);if(q){const t=copy(q);t.xaxis=x;t.yaxis=y;traces.push(t);}});
   layout={...base,height:940,showlegend:false,margin:{l:18,r:68,t:28,b:38},
    xaxis:{...base.xaxis,anchor:'y',showticklabels:true},yaxis:{...base.yaxis,domain:[.43,1],side:'right',title:''},
    xaxis2:{...base.xaxis,anchor:'y2',matches:'x',showticklabels:false},yaxis2:{...base.yaxis,domain:[.30,.40],range:[-1.05,1.05],side:'right',title:''},
    xaxis3:{...base.xaxis,anchor:'y3',matches:'x',showticklabels:false},yaxis3:{...base.yaxis,domain:[.14,.27],side:'right',title:''},
    xaxis4:{...base.xaxis,anchor:'y4',matches:'x'},yaxis4:{...base.yaxis,domain:[0,.11],side:'right',title:'',zeroline:true,zerolinecolor:T.border},
    annotations:[{text:'PRECIO',xref:'paper',yref:'paper',x:.995,y:1.025,xanchor:'right',showarrow:false,font:{color:T.text,size:10}},{text:'AGRESOR',xref:'paper',yref:'paper',x:0,y:.415,showarrow:false,font:{color:T.text,size:10}},{text:'TOTAL',xref:'paper',yref:'paper',x:0,y:.285,showarrow:false,font:{color:T.text,size:10}},{text:'NET FLOW',xref:'paper',yref:'paper',x:0,y:.125,showarrow:false,font:{color:T.text,size:10}},...flowAnn]};
 }else if(id==='netDriftChart'){
   if(price){const t=copy(price);t.xaxis='x';t.yaxis='y';t.line={...(t.line||{}),color:T.price,width:2};traces.push(t);}
   src.filter(t=>seriesRole(t)==='DRIFT_LINE').forEach((q,i)=>{const t=copy(q);t.xaxis='x2';t.yaxis='y2';t.line={...(t.line||{}),color:[T.cyan,T.pink,T.white,T.green][i%4],width:i>=2?2.4:1.8};traces.push(t);});
   src.filter(t=>seriesRole(t)==='FLOW_LINE').forEach(q=>{const t=copy(q);t.xaxis='x3';t.yaxis='y3';t.line={...(t.line||{}),color:T.gold,width:2.2};traces.push(t);});
   layout={...base,height:760,xaxis:{...base.xaxis,anchor:'y',showticklabels:false},yaxis:{...base.yaxis,domain:[.62,1],title:'PRECIO'},xaxis2:{...base.xaxis,anchor:'y2',matches:'x',showticklabels:false},yaxis2:{...base.yaxis,domain:[.27,.55],title:'DRIFT'},xaxis3:{...base.xaxis,anchor:'y3',matches:'x'},yaxis3:{...base.yaxis,domain:[0,.18],title:'Q-FLOW'}};
 }else if(id==='printsChart'){
   if(price){const t=copy(price);t.xaxis='x';t.yaxis='y';t.line={...(t.line||{}),color:T.price,width:2};traces.push(t);}
   const ev=src.find(t=>seriesRole(t)==='PRINT_EVENT');if(ev){const t=copy(ev);t.xaxis='x';t.yaxis='y';t.mode='markers';t.text=undefined;t.marker={...(t.marker||{}),size:9};traces.push(t);}
   const bar=src.find(t=>seriesRole(t)==='PRINT_NOTIONAL'||String(t.type||'').toLowerCase()==='bar');if(bar){const t=copy(bar);t.xaxis='x2';t.yaxis='y2';traces.push(t);}
   layout={...base,height:670,xaxis:{...base.xaxis,anchor:'y',showticklabels:false},yaxis:{...base.yaxis,domain:[.37,1],title:'PRECIO'},xaxis2:{...base.xaxis,anchor:'y2',matches:'x'},yaxis2:{...base.yaxis,domain:[0,.27],title:'NOTIONAL $M'}};
 }
 if(!traces.length)return false;
 try{P.react(host,traces,layout,{responsive:true,displaylogo:false,displayModeBar:!(id==='flowProChart'||id==='traceFlowProChart'),scrollZoom:true,doubleClick:'reset+autosize',modeBarButtonsToRemove:['lasso2d','select2d']});host.dataset.renderer='ITM_MARKET_PLOTLY_FALLBACK';return true;}catch(e){console.warn('[ITM MARKET PLOTLY FALLBACK]',id,e);return false;}
}

function supports(id,spec){return TARGETS.has(id)&&Array.isArray(spec?.data)&&(!!L()?.createChart||!!window.Plotly?.react);}
function render(id,spec){if(!supports(id,spec))return false;/* Flujo Inusual usa Plotly owned renderer: permite burbujas escalables, labels $ y 4 paneles sincronizados como el contrato visual de referencia. */if((id==='flowProChart'||id==='traceFlowProChart')&&window.Plotly?.react)return cleanPlotlyFallback(id,spec);if(!L()?.createChart)return cleanPlotlyFallback(id,spec);const host=document.getElementById(id);if(!host)return false;try{if(window.Plotly?.purge)try{window.Plotly.purge(host);}catch(_){}let t=charts.get(id);if(!t){t=new Terminal(host,id);charts.set(id,t);}return t.render(spec);}catch(e){console.warn('[ITM MARKET TERMINAL FALLBACK]',id,e);try{charts.get(id)?.clear?.();}catch(_){}charts.delete(id);return false;}}
window.ITMQMarketLineTerminals={supports,render,state:{charts,targets:TARGETS}};
})();

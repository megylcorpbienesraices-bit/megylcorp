/* ITM QUANT v1.40.2 · LOW-LATENCY Lightweight Charts price bridge.
 * Historical data is bootstrapped with setData().  LIVE price uses series.update()
 * exclusively, so a market tick never rebuilds the full candle history.
 * Presentation only: Scanner, calculations, provider policy and risk are untouched.
 */
(()=>{
'use strict';
const toTime=v=>{const ms=new Date(v).getTime();return Number.isFinite(ms)?Math.floor(ms/1000):null;};
const n=v=>{v=Number(v);return Number.isFinite(v)?v:null;};
const candleRow=b=>{const time=toTime(b?.t),open=n(b?.o),high=n(b?.h),low=n(b?.l),close=n(b?.c);return time==null||[open,high,low,close].some(v=>v==null)?null:{time,open,high,low,close};};
const lineRow=r=>r?{time:r.time,value:r.close}:null;
const pct=(arr,q)=>{if(!arr.length)return 0;const a=arr.slice().sort((x,y)=>x-y);return a[Math.min(a.length-1,Math.max(0,Math.floor((a.length-1)*q)))]||0;};
function latencyState(){
 if(window.ITMQLiveLatency)return window.ITMQLiveLatency;
 const st={samples:[],paintSamples:[],eventSamples:[],last:null,note(updateMs,paintMs,eventMs){
  if(Number.isFinite(updateMs)){this.samples.push(updateMs);if(this.samples.length>240)this.samples.shift();}
  if(Number.isFinite(paintMs)){this.paintSamples.push(paintMs);if(this.paintSamples.length>240)this.paintSamples.shift();}
  if(Number.isFinite(eventMs)){this.eventSamples.push(eventMs);if(this.eventSamples.length>240)this.eventSamples.shift();}
  this.last={update_ms:Number.isFinite(updateMs)?updateMs:null,paint_ms:Number.isFinite(paintMs)?paintMs:null,event_to_browser_ms:Number.isFinite(eventMs)?eventMs:null,timestamp:new Date().toISOString()};
 },snapshot(){return{...this.last,browser_to_update_p95_ms:+pct(this.samples,.95).toFixed(2),browser_to_paint_p95_ms:+pct(this.paintSamples,.95).toFixed(2),event_to_browser_p95_ms:+pct(this.eventSamples,.95).toFixed(2),samples:this.samples.length};}};
 window.ITMQLiveLatency=st;return st;
}
class LightweightTracePrice{
 constructor(host){
  this.host=host;this.active=false;this.range=[0,1];this.lastStyle='';this.lastTheme='';this.lastRect='';this.lastScale='';this.lastViewport='';this.dataReady=false;this.syncedLength=0;this.firstTime=0;this.lastTime=0;
  const L=window.LightweightCharts;if(!host||!L?.createChart||!L?.CandlestickSeries||!L?.LineSeries)return;
  try{
   const el=document.createElement('div');el.className='trace-lwc-base';el.setAttribute('aria-hidden','true');host.insertBefore(el,host.firstChild);this.el=el;
   const common={layout:{background:{type:'solid',color:'rgba(0,0,0,0)'},textColor:'rgba(0,0,0,0)',attributionLogo:true},grid:{vertLines:{visible:false},horzLines:{visible:false}},rightPriceScale:{visible:false,borderVisible:false,autoScale:true,scaleMargins:{top:0,bottom:0}},leftPriceScale:{visible:false,borderVisible:false},timeScale:{visible:false,borderVisible:false,rightOffset:7,barSpacing:9,fixLeftEdge:false,fixRightEdge:false,timeVisible:true,secondsVisible:false},crosshair:{mode:L.CrosshairMode?.Hidden??0,vertLine:{visible:false},horzLine:{visible:false}},handleScroll:false,handleScale:false,kineticScroll:{mouse:false,touch:false}};
   this.chart=L.createChart(el,common);
   const autoscaleInfoProvider=()=>({priceRange:{minValue:this.range[0],maxValue:this.range[1]},margins:{above:0,below:0}});
   this.candles=this.chart.addSeries(L.CandlestickSeries,{upColor:'#49b8df',downColor:'#db4b93',wickUpColor:'#49b8df',wickDownColor:'#db4b93',borderVisible:false,lastValueVisible:false,priceLineVisible:false,autoscaleInfoProvider});
   this.line=this.chart.addSeries(L.LineSeries,{color:'#d7e5ee',lineWidth:2,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false,autoscaleInfoProvider});
   this.line.applyOptions({visible:false});this.active=true;latencyState();
  }catch(err){console.warn('[ITM TRACE] Lightweight Charts unavailable, native fallback active',err);this.dispose();}
 }
 dispose(){try{this.chart?.remove?.();}catch(_){}try{this.el?.remove?.();}catch(_){}this.chart=null;this.candles=null;this.line=null;this.active=false;}
 reset(){if(!this.active)return;try{this.candles.setData([]);this.line.setData([]);}catch(_){}this.dataReady=false;this.syncedLength=0;this.firstTime=0;this.lastTime=0;}
 syncHistory(candles=[]){
  if(!this.active)return false;const rows=[];for(const b of candles){const r=candleRow(b);if(r)rows.push(r);}if(!rows.length){this.reset();return true;}
  try{
   const first=rows[0].time,last=rows[rows.length-1].time;
   const appendCompatible=this.dataReady&&first===this.firstTime&&rows.length>=this.syncedLength&&last>=this.lastTime&&(rows.length-this.syncedLength)<=64;
   if(!appendCompatible){this.candles.setData(rows);this.line.setData(rows.map(lineRow));}
   else{
    // Update only the tail.  Index -1 is deliberate: the current candle may have
    // received a REST correction while all earlier bars remain immutable.
    const start=Math.max(0,this.syncedLength-1);for(let i=start;i<rows.length;i++){this.candles.update(rows[i]);this.line.update(lineRow(rows[i]));}
   }
   this.dataReady=true;this.syncedLength=rows.length;this.firstTime=first;this.lastTime=last;return true;
  }catch(err){console.warn('[ITM TRACE] Lightweight history sync failed; native fallback active',err);this.dispose();return false;}
 }
 updateBar(bar,meta={}){
  if(!this.active)return false;const row=candleRow(bar);if(!row)return false;
  try{
   if(!this.dataReady){this.candles.setData([row]);this.line.setData([lineRow(row)]);this.dataReady=true;this.syncedLength=1;this.firstTime=row.time;this.lastTime=row.time;}
   else{const isNew=row.time>this.lastTime;this.candles.update(row);this.line.update(lineRow(row));if(isNew){this.syncedLength++;this.lastTime=row.time;}}
   const rx=Number(meta?.browserReceivedPerf),eventMs=Number(meta?.eventToBrowserMs),updateAt=performance.now(),updateMs=Number.isFinite(rx)?Math.max(0,updateAt-rx):NaN;
   if(Number.isFinite(rx))requestAnimationFrame(()=>latencyState().note(updateMs,Math.max(0,performance.now()-rx),eventMs));
   return true;
  }catch(err){console.warn('[ITM TRACE] Lightweight LIVE update failed; native fallback active',err);this.dispose();return false;}
 }
 setFrame({style='candles',rect,priceRange=[0,1],barSpacing=9,rightOffset=7,xShiftPx=0,theme='dark'}={}){
  if(!this.active||!rect||rect.right<=rect.left||rect.bottom<=rect.top)return false;
  try{
   const nextRange=[Number(priceRange[0])||0,Number(priceRange[1])||1],rangeKey=`${nextRange[0].toFixed(8)}|${nextRange[1].toFixed(8)}`;this.range=nextRange;
   const width=Math.max(40,Math.round(rect.right-rect.left)),height=Math.max(40,Math.round(rect.bottom-rect.top)),rectKey=`${Math.round(rect.left)}|${Math.round(rect.top)}|${width}|${height}`;
   if(rectKey!==this.lastRect){this.lastRect=rectKey;Object.assign(this.el.style,{left:`${Math.round(rect.left)}px`,top:`${Math.round(rect.top)}px`,width:`${width}px`,height:`${height}px`});this.chart.resize(width,height);}
   const dark=theme!=='light',themeKey=dark?'dark':'light';
   if(this.lastTheme!==themeKey){this.lastTheme=themeKey;this.chart.applyOptions({layout:{background:{type:'solid',color:'rgba(0,0,0,0)'},textColor:'rgba(0,0,0,0)',attributionLogo:true}});this.candles.applyOptions({upColor:dark?'#49b8df':'#0891b2',downColor:dark?'#db4b93':'#c02670',wickUpColor:dark?'#49b8df':'#0891b2',wickDownColor:dark?'#db4b93':'#c02670'});this.line.applyOptions({color:dark?'#e4edf4':'#243447'});}
   const safeSpacing=Math.max(3,Math.min(34,Number(barSpacing)||9)),offset=Math.max(-500,Math.min(500,(Number(rightOffset)||7)-(Number(xShiftPx)||0)/safeSpacing)),viewKey=`${safeSpacing.toFixed(3)}|${offset.toFixed(3)}`;
   if(viewKey!==this.lastViewport){this.lastViewport=viewKey;this.chart.timeScale().applyOptions({barSpacing:safeSpacing,rightOffset:offset});}
   const lineMode=String(style).toLowerCase().startsWith('l');if(this.lastStyle!==String(lineMode)){this.lastStyle=String(lineMode);this.candles.applyOptions({visible:!lineMode});this.line.applyOptions({visible:lineMode});}
   if(rangeKey!==this.lastScale){this.lastScale=rangeKey;this.chart.priceScale('right').applyOptions({autoScale:true});}
   return true;
  }catch(err){console.warn('[ITM TRACE] Lightweight price frame failed; native fallback active',err);this.dispose();return false;}
 }
}
window.ITMQLightweightTrace={create:host=>new LightweightTracePrice(host),latency:latencyState()};
})();

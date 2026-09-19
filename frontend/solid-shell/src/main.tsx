import { render } from 'solid-js/web';
import { createMemo, createSignal, For, Show, onCleanup } from 'solid-js';
import './terminal.css';

type Decision={direction?:string;strength?:number;zone?:string;t1?:number;t2?:number;invalidation?:number;explanation?:string};
type MarketState={gamma?:string;delta?:string;dealer?:string;hedge?:string;dataQuality?:number;modelHealth?:number};
type NativeStructure={status?:string;zero_gamma?:number|null;gamma_flip?:number|null;major_pos_oi?:number|null;major_neg_oi?:number|null;gamma_pressure?:string;delta_pressure?:string};
type Asset={symbol:string;name?:string;family?:string;kind?:string;full?:boolean;ecosystem?:{family?:string;indices?:string[];futures?:string[]}};
type TerminalAggregate={activeSymbol?:string;assets?:Asset[];ecosystem?:unknown;decision?:Decision;market?:MarketState;native_options_structure?:NativeStructure};

function Metric(props:{label:string,value:any,tone?:'pos'|'neg'|'warn'|'neutral'}){
 return <div class={`metric ${props.tone||'neutral'}`}><span>{props.label}</span><b>{props.value??'—'}</b></div>
}

function EcosystemSelector(props:{agg:TerminalAggregate}){
 const active=()=>String(props.agg.activeSymbol||'DIA').toUpperCase();
 const rows=()=>Array.isArray(props.agg.assets)?props.agg.assets.filter(x=>x?.full):[];
 const select=(symbol:string)=>window.dispatchEvent(new CustomEvent('itmq:select-asset',{detail:{symbol}}));
 return <label class="solid-ecosystem-command"><span>ECOSISTEMA</span><select value={active()} onChange={e=>select(e.currentTarget.value)} aria-label="Seleccionar ecosistema cuantitativo"><Show when={rows().length} fallback={<option value={active()}>{active()}</option>}><For each={rows()}>{a=>{const f=a.ecosystem?.futures?.[0],i=a.ecosystem?.indices?.[0];return <option value={a.symbol}>{a.symbol} · {a.family||a.ecosystem?.family||a.kind||''}{i?` · ${i}`:''}{f?` · ${f}`:''}</option>}}</For></Show></select></label>
}

function App(){
 const [section,setSection]=createSignal<'TRACE'|'SURFACE'|'FLOW'|'RESEARCH'>('TRACE');
 const [agg,setAgg]=createSignal<TerminalAggregate>({});
 const handler=(e:Event)=>setAgg((e as CustomEvent).detail||{});
 window.addEventListener('itmq:aggregate-ui',handler as EventListener);
 onCleanup(()=>window.removeEventListener('itmq:aggregate-ui',handler as EventListener));
 const dec=createMemo(()=>agg().decision||{}), market=createMemo(()=>agg().market||{}), ns=createMemo(()=>agg().native_options_structure||{});
 const bearish=createMemo(()=>String(dec().direction||'').toUpperCase().includes('VENTA'));
 const sections=['TRACE','SURFACE','FLOW','RESEARCH'] as const;
 return <div class="v124-shell">
   <header class="top-shell"><div class="brand"><strong>ITM QUANT</strong><span>INSTITUTIONAL TRADING INTELLIGENCE</span></div><EcosystemSelector agg={agg()}/><div class="health"><Metric label="DATA" value={market().dataQuality==null?'—':`${market().dataQuality}%`}/><Metric label="MODEL" value={market().modelHealth==null?'—':`${market().modelHealth}%`}/></div></header>
   <aside class="side-shell"><For each={sections}>{x=><button classList={{active:section()===x}} onClick={()=>setSection(x)}>{x}</button>}</For><div class="gpu">GPU / WASM<br/><b>CAPABILITY</b></div></aside>
   <main class="main-shell">
     <section class={`decision-shell ${bearish()?'bearish':'bullish'}`}><div><span>SEÑAL ACTUAL</span><strong>{dec().direction||'ESPERANDO'} · {dec().strength??'—'}/100</strong></div><Metric label="ENTRADA / ZONA" value={dec().zone}/><Metric label="T1" value={dec().t1}/><Metric label="T2" value={dec().t2}/><Metric label="INVALIDACIÓN" value={dec().invalidation}/><p>{dec().explanation||'Scanner conserva autoridad; el resto de la terminal valida contexto y microestructura.'}</p></section>
     <section class="work-shell">
       <div class="gpu-stage"><div class="stage-title"><b>{section()==='SURFACE'?'SURFACE LAB 4D':'TRACE PRO 3.0'}</b><span>CANVAS / WEBGPU · BINARY STREAM</span></div><div id="solid-native-mount" data-section={section()}>Native renderer mount</div></div>
       <aside class="intel-shell"><h3>ESTADO DE MERCADO</h3><Metric label="Régimen Gamma" value={market().gamma}/><Metric label="Presión Delta" value={market().delta}/><Metric label="Dealer Field" value={market().dealer}/><Metric label="Hedge Pressure" value={market().hedge}/><h3 class="external">ESTRUCTURA NATIVA</h3><Metric label="Zero Gamma" value={ns().zero_gamma}/><Metric label="Gamma Flip" value={ns().gamma_flip}/><Metric label="Major +OI" value={ns().major_pos_oi}/><Metric label="Major -OI" value={ns().major_neg_oi}/><Metric label="Gamma Pressure" value={ns().gamma_pressure}/><Metric label="Delta Pressure" value={ns().delta_pressure}/><small>{ns().status||'WAITING'} · ITM QUANT native authority</small></aside>
     </section>
   </main>
 </div>
}
render(()=><App/>,document.getElementById('root')!);

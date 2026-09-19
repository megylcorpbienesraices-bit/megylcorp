"""Sophia: self-hosted quantitative copilot for ITM QUANT.

No paid LLM/STT/TTS provider is wired here.  Sophia answers deterministic operational
questions directly from ITM QUANT state and can optionally use a LOCAL loopback LLM
(Ollama/llama.cpp/OpenAI-compatible) for broader financial-language interpretation.
Scanner remains the only directional authority.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import RLock
from typing import Any
from urllib.parse import urlparse
import asyncio
import json
import math
import os
import re
import unicodedata
import uuid

import httpx
from .obs import note as _obs_note
from .tool_registry import TOOL_REGISTRY, build_tool_digest


def _norm(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", raw.lower()).strip()


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _side_es(side: str) -> str:
    s=str(side or "").upper()
    return "compra" if s=="BUY" else "venta" if s=="SELL" else "neutral"


# SpotGamma-style named-level watches (v1.34): the value is resolved from
# state["key_levels_report"] at EVALUATION time, never frozen at creation time --
# Call Wall/Put Wall/Vol Trigger/Zero Gamma move every session. Longest phrases
# first so "vol trigger" is not shadowed by a shorter, unrelated match.
_NAMED_LEVEL_PHRASES: list[tuple[str, str]] = [
    ("disparador de volatilidad", "VOL_TRIGGER"), ("volatility trigger", "VOL_TRIGGER"), ("vol trigger", "VOL_TRIGGER"),
    ("call wall", "CALL_WALL"), ("put wall", "PUT_WALL"),
    ("zero gamma", "ZERO_GAMMA"), ("gamma cero", "ZERO_GAMMA"), ("gamma flip", "ZERO_GAMMA"),
]
_NAMED_LEVEL_LABELS: dict[str, str] = {
    "ZERO_GAMMA": "Zero Gamma", "CALL_WALL": "Call Wall", "PUT_WALL": "Put Wall", "VOL_TRIGGER": "Vol Trigger",
}
_KEY_LEVELS_REPORT_FIELD: dict[str, str] = {
    "ZERO_GAMMA": "zero_gamma", "CALL_WALL": "call_wall", "PUT_WALL": "put_wall", "VOL_TRIGGER": "vol_trigger",
}


def _actionable_side(text: str) -> str | None:
    """Detect an affirmative trading recommendation, ignoring explicit negation."""
    t=_norm(text)
    patterns={
        "BUY":[r"\bcompraria\b",r"\bcomprar(?:ia)?(?: aqui| ahora)?\b",r"\bentraria (?:en )?(?:compra|largo)\b",r"\biria largo\b",r"\babriria (?:una )?compra\b",r"\bbuy\b",r"\blong\b"],
        "SELL":[r"\bvenderia\b",r"\bvender(?:ia)?(?: aqui| ahora)?\b",r"\bentraria (?:en )?(?:venta|corto)\b",r"\biria corto\b",r"\babriria (?:una )?venta\b",r"\bsell\b",r"\bshort\b"],
    }
    hits=[]
    for side,pats in patterns.items():
        for pat in pats:
            for m in re.finditer(pat,t):
                # Negation can be separated from the verb by a short clause
                # (e.g. "no recomiendo comprar" / "nunca iria long").
                prefix=t[max(0,m.start()-48):m.start()]
                clause=re.split(r"[.;!?]",prefix)[-1]
                if re.search(r"\b(?:no|nunca|evita|evitaria|evite|sin)\b[^.;!?]{0,32}$",clause):
                    continue
                hits.append((m.start(),side))
    if not hits:return None
    hits.sort()
    sides={x[1] for x in hits}
    return hits[-1][1] if len(sides)==1 else "MIXED"


def validate_against_scanner(response: str, state: dict[str,Any]) -> dict[str,Any]:
    """Post-LLM hard governor. Prompt instructions are not an authority boundary.

    A local model may explain the market, but it cannot create or reverse the direction
    calculated by Scanner. A contradiction is replaced, not merely annotated.
    """
    text=str(response or "").strip()
    sc=(state or {}).get("scanner") or {}
    direction=str(sc.get("direction") or "WAIT").upper()
    gate=(state or {}).get("publication_gate") or (state or {}).get("circuito_frescura") or {}
    if gate and gate.get("publicar_permitido") is not True:
        reason=str(gate.get("motivo") or gate.get("reason") or "frescura no habilitada")
        return {"text":f"Publicacion cuantitativa bloqueada por frescura: {reason}. Sophia no emitira direccion ni entrada.",
                "blocked":True,"reason":"FRESHNESS_GATE","scanner_direction":direction}
    rec=_actionable_side(text)
    opposite="SELL" if direction=="BUY" else "BUY" if direction=="SELL" else None
    if opposite and rec in {opposite,"MIXED"}:
        es="COMPRA" if direction=="BUY" else "VENTA"
        return {"text":f"Scanner manda {es}. La respuesta del modelo local fue bloqueada porque proponia una direccion incompatible. Sophia puede explicar el contexto, pero no cambiar la autoridad del Scanner.",
                "blocked":True,"reason":"SCANNER_DIRECTION_CONFLICT","scanner_direction":direction,"detected_recommendation":rec}
    return {"text":text,"blocked":False,"reason":None,"scanner_direction":direction,"detected_recommendation":rec}


def _compact(value: Any, depth: int = 0, max_items: int = 28) -> Any:
    """Bound the local-LLM context without discarding whole ITM QUANT modules."""
    if depth >= 3:
        if isinstance(value, (dict, list, tuple)):
            return "…"
        return value
    if isinstance(value, dict):
        out={}
        for i,(k,v) in enumerate(value.items()):
            if i>=max_items: break
            if str(k).lower() in {"enriched","matrix","surface","representative_paths","raw","history","rows"}:
                continue
            out[str(k)]=_compact(v,depth+1,max_items)
        return out
    if isinstance(value,(list,tuple)):
        return [_compact(v,depth+1,max_items) for v in list(value)[:10]]
    if isinstance(value,(str,int,float,bool)) or value is None:
        return value
    try:
        return str(value)[:240]
    except Exception:
        return None


@dataclass
class WatchRule:
    id: str
    kind: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    active: bool = True
    one_shot: bool = True
    session_only: bool = True
    last_signature: str | None = None
    trigger_count: int = 0


class SophiaEventHub:
    def __init__(self) -> None:
        self._lock = RLock()
        self._queues: set[asyncio.Queue] = set()
        self._history: list[dict[str, Any]] = []

    def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._queues.add(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    def publish_nowait(self, event: dict[str, Any]) -> None:
        payload={"timestamp":datetime.now(timezone.utc).isoformat(),**dict(event or {})}
        with self._lock:
            self._history.append(payload)
            self._history=self._history[-100:]
            queues=list(self._queues)
        for q in queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    _=q.get_nowait();q.task_done();q.put_nowait(payload)
                except Exception as _e:
                    _obs_note('sophia_core:113', _e)

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history)


class LocalLLM:
    def __init__(self) -> None:
        self.url=str(os.getenv("ITM_SOPHIA_LLM_URL","")).strip()
        self.model=str(os.getenv("ITM_SOPHIA_LLM_MODEL","itm-sophia-local")).strip()

    def configured(self) -> bool:
        if not self.url:return False
        try:
            host=(urlparse(self.url).hostname or "").lower()
            return host in {"127.0.0.1","localhost","::1"}
        except Exception:return False

    async def answer(self, message: str, state_digest: dict[str, Any]) -> str | None:
        if not self.configured():return None
        system=(
            "Eres Sophia, asistente trader cuantitativa profesional integrada en ITM QUANT. "
            "Solo puedes afirmar datos presentes en STATE. Scanner es la unica autoridad direccional; "
            "Aggression Trigger solo temporiza entradas. Si publication_gate.publicar_permitido no es true, no emitas direccion, entrada ni confirmacion. No inventes precios, flujo, gamma, delta ni probabilidades. "
            "Responde en espanol claro, breve y operativo. Si falta dato di que no esta disponible."
        )
        payload={"model":self.model,"messages":[{"role":"system","content":system+"\nSTATE="+json.dumps(state_digest,ensure_ascii=False)},{"role":"user","content":message}],"temperature":0.15,"stream":False}
        try:
            async with httpx.AsyncClient(timeout=18.0) as client:
                r=await client.post(self.url,json=payload)
                r.raise_for_status();data=r.json()
            if isinstance(data,dict):
                if isinstance(data.get("choices"),list) and data["choices"]:
                    return str(((data["choices"][0] or {}).get("message") or {}).get("content") or "").strip() or None
                if isinstance(data.get("message"),dict):
                    return str(data["message"].get("content") or "").strip() or None
                if data.get("response"):
                    return str(data.get("response")).strip() or None
        except Exception:
            return None
        return None


class SophiaRuntime:
    def __init__(self) -> None:
        self._lock=RLock()
        self._watches: dict[str,WatchRule]={}
        self._last_watch_id: str|None=None
        self.events=SophiaEventHub()
        self.local_llm=LocalLLM()
        self._last_session_date: str|None=None

    def status(self) -> dict[str,Any]:
        with self._lock:
            watches=[asdict(x) for x in self._watches.values() if x.active]
        return {"ready":True,"name":"Sophia","mode":"SELF_HOSTED_NO_CREDITS","scanner_authority":"UNCHANGED",
                "local_llm":{"configured":self.local_llm.configured(),"url":"LOOPBACK" if self.local_llm.configured() else None,"model":self.local_llm.model if self.local_llm.configured() else None},
                "watch_count":len(watches),"voice":"LOCAL_ADAPTER","external_paid_ai":False}

    def watches(self) -> list[dict[str,Any]]:
        with self._lock:return [asdict(x) for x in self._watches.values() if x.active]

    def clear_watches(self) -> int:
        with self._lock:
            n=len(self._watches);self._watches.clear();self._last_watch_id=None
        return n

    def cancel_watch(self, watch_id: str|None=None) -> WatchRule|None:
        with self._lock:
            wid=watch_id or self._last_watch_id
            if not wid:return None
            rule=self._watches.pop(wid,None)
            if self._last_watch_id==wid:self._last_watch_id=None
            return rule

    def set_watch_baseline(self, watch_id: str, **params: Any) -> WatchRule|None:
        """Attach causal baselines captured by the runtime at command time."""
        with self._lock:
            rule=self._watches.get(str(watch_id))
            if rule is not None:
                rule.params.update({k:v for k,v in params.items() if v is not None})
            return rule

    def _add(self, kind:str,label:str,params:dict[str,Any],*,one_shot:bool=True) -> WatchRule:
        rule=WatchRule(id=uuid.uuid4().hex[:10],kind=kind,label=label,params=params,one_shot=one_shot,session_only=True)
        with self._lock:
            self._watches[rule.id]=rule;self._last_watch_id=rule.id
        return rule

    def parse_watch(self, message: str) -> WatchRule|None:
        t=_norm(message)
        if not any(k in t for k in ("avisame","avisa me","notificame","alertame","dime cuando","cuando aparezca","cuando haya")):
            return None
        side="ANY"
        if any(x in t for x in ("compra","comprador","compradora","buy")):side="BUY"
        elif any(x in t for x in ("venta","vendedor","vendedora","sell")):side="SELL"
        if "flujo inusual" in t or "unusual flow" in t:
            label="Flujo Inusual"+(f" {_side_es(side)}" if side!="ANY" else "")
            return self._add("UNUSUAL_FLOW",label,{"side":side,"min_score":float(os.getenv("ITM_SOPHIA_UNUSUAL_SCORE","70"))},one_shot=False)
        if "agresion" in t or "delta" in t and any(x in t for x in ("vela","1m","3m","5m","15m")):
            tf="1m"
            m=re.search(r"\b(1|3|5|15)\s*(?:m|min|minuto|minutos)\b",t)
            if m:tf=f"{m.group(1)}m"
            if side=="ANY":
                side="BUY" if "cambie a compra" in t else "SELL" if "cambie a venta" in t else "ANY"
            return self._add("AGGRESSION",f"Agresion {tf}"+(f" {_side_es(side)}" if side!="ANY" else ""),{"timeframe":tf,"side":side},one_shot=True)
        if "scanner" in t:
            strength=None
            m=re.search(r"(?:fuerza|strength)[^0-9]{0,8}(\d{1,3})",t)
            if m:strength=max(0,min(100,int(m.group(1))))
            return self._add("SCANNER",f"Scanner"+(f" {_side_es(side)}" if side!="ANY" else ""),{"side":side,"min_strength":strength},one_shot=True)
        # Named structural level: "avisame cuando rompa el call wall" references the
        # CURRENT value of a level that moves every session (Zero Gamma / walls / Vol
        # Trigger), never a number frozen at the moment the watch was created. Checked
        # before the generic Gamma/Delta dominance rule below, since "zero gamma"/
        # "gamma flip" contain the word "gamma" and would otherwise be swallowed by it.
        for phrase, level_name in _NAMED_LEVEL_PHRASES:
            if phrase in t:
                relation="TOUCH"
                if any(x in t for x in ("supere","por encima","rompa arriba","rompe arriba")):relation="ABOVE"
                elif any(x in t for x in ("baje de","por debajo","rompa abajo","rompe abajo")):relation="BELOW"
                label=_NAMED_LEVEL_LABELS[level_name]
                return self._add("PRICE",f"Precio {relation.lower()} {label}",{"level_name":level_name,"relation":relation},one_shot=True)
        if "gamma" in t or "delta" in t:
            return self._add("GAMMA_DELTA","Gamma/Delta dominancia",{"side":side},one_shot=True)
        # Price level: accept strike/level/price wording and a reasonable numeric token.
        if any(x in t for x in ("nivel","strike","precio","llegue","toque","pase")):
            nums=re.findall(r"(?<![a-z])\d+(?:[.,]\d+)?",t)
            if nums:
                level=float(nums[-1].replace(",","."))
                relation="TOUCH"
                if any(x in t for x in ("supere","por encima","rompa arriba")):relation="ABOVE"
                elif any(x in t for x in ("baje de","por debajo","rompa abajo")):relation="BELOW"
                return self._add("PRICE",f"Precio {relation.lower()} {level:g}",{"level":level,"relation":relation},one_shot=True)
        return None

    @staticmethod
    def _digest(state: dict[str,Any], aggression: dict[str,Any]|None=None) -> dict[str,Any]:
        sc=state.get("scanner") or {};flowp=state.get("flow_pro") or {};gd=state.get("gamma_delta_alignment") or {};cmd=state.get("command") or {}
        symbol=state.get("active_symbol") or state.get("symbol")
        meta=state.get("meta") or {}
        dq=state.get("data_quality_report") or {}
        digest={
            "symbol":symbol,"mode":state.get("mode"),"spot":state.get("spot"),"market_state":meta.get("market_state"),
            "replay":_compact(state.get("replay") or {}),
            "scanner":_compact({"direction":sc.get("direction"),"strength":sc.get("strength",sc.get("evidence_score")),"edge_state":sc.get("edge_state"),"zone":sc.get("zone"),"entry":sc.get("entry"),"target1":sc.get("target1"),"target2":sc.get("target2"),"invalidation":sc.get("invalidation"),"reason":sc.get("reason"),"probability":sc.get("probability")}),
            "gamma":{"center":state.get("gamma_center"),"flip":state.get("gamma_flip"),"migration":_compact(state.get("gamma_migration") or {})},
            "delta":{"center":state.get("delta_center"),"migration":_compact(state.get("delta_migration") or {})},
            "gamma_delta_alignment":_compact(gd),
            "aggression":_compact((aggression or {}).get("frames",{})),
            "trace_orderflow":_compact(state.get("trace_orderflow") or {}),
            "flow":_compact(state.get("flow") or {}),
            "flow_unusual":_compact(flowp.get("latest")),"flow_pro":_compact(flowp),
            "targets_levels":_compact(state.get("targets") or {}),
            "key_levels_report":_compact(state.get("key_levels_report") or {}),
            "command_center":_compact(cmd),
            "volatility":_compact(state.get("volatility") or {}),
            "positioning":_compact(state.get("positioning") or {}),
            "chain_insights":_compact(state.get("chain_insights") or {}),
            "dealer_intelligence":_compact(state.get("dealer_intelligence") or {}),
            "market_state_field":_compact(state.get("market_state_field") or {}),
            "derivatives_intelligence":_compact(state.get("derivatives_intelligence") or {}),
            "expiry_intelligence":_compact(state.get("expiry_intelligence") or {}),
            "structural_intelligence":_compact(state.get("structural_intelligence") or {}),
            "profile_bundle":_compact(state.get("profile_bundle") or {}),
            "macro":_compact(state.get("macro") or {}),
            "large_prints":_compact(state.get("large_prints") or {}),
            "decision_intelligence":_compact(state.get("decision_intelligence") or {}),
            "feature_intelligence":_compact(state.get("feature_intelligence") or {}),
            "market_truth":_compact(state.get("market_truth") or {}),
            "data_quality":state.get("data_quality"),"publication_gate":_compact(state.get("publication_gate") or dq.get("circuito_frescura") or {}),"model_health":_compact(state.get("model_health")),
            "oi_structural_freshness":_compact(dq.get("oi_structural_freshness") or {}),
            "greeks_provenance":_compact(dq.get("greeks_provenance") or {}),
        }
        # Structured Tool Digests: Sophia reads bounded contracts instead of screenshots or
        # unbounded chart payloads. Scanner remains the sole directional authority.
        digest["tool_digests"]={}
        for _tid in ("scanner","trace","flow","netdrift","gexmatrix","gamma_migration","volatility","prints","macro","conditional_outcomes"):
            if _tid in TOOL_REGISTRY:
                try:digest["tool_digests"][_tid]=build_tool_digest(_tid,state)
                except Exception as _e:
                    _obs_note("sophia:tool_digest",_e)
                    continue
        return digest

    @staticmethod
    def read_tool_data(tool_id: str, state: dict[str,Any]) -> dict[str,Any]:
        """Read one bounded Tool contract from cached state only.

        This is intentionally read-only: it cannot alter Scanner rules, provider state,
        or dashboard configuration.  It is the local equivalent of a structured read-tool
        call for Sophia.
        """
        tid=str(tool_id or "").lower().strip()
        if tid not in TOOL_REGISTRY:
            return {"ready":False,"reason":"TOOL_NOT_REGISTERED","tool_id":tid}
        return {"ready":True,"tool_id":tid,"digest":build_tool_digest(tid,state),"authority":TOOL_REGISTRY[tid].authority}


    @staticmethod
    def _publication_blocked(state: dict[str,Any]) -> tuple[bool,str]:
        gate=(state or {}).get("publication_gate") or ((state or {}).get("data_quality_report") or {}).get("circuito_frescura") or {}
        allowed=isinstance(gate,dict) and gate.get("publicar_permitido") is True
        return (not allowed, str(gate.get("motivo") or "frescura critica no verificada") if isinstance(gate,dict) else "frescura critica no verificada")

    def _internal_answer(self,message:str,state:dict[str,Any],aggression:dict[str,Any]|None) -> str|None:
        t=_norm(message);d=self._digest(state,aggression);sc=d["scanner"] or {};sym=d.get("symbol") or "activo";spot=d.get("spot")
        if any(x in t for x in ("quien manda","quien domina","domina ahora","direccion ahora")):
            direction=str(sc.get("direction") or "WAIT").upper();strength=_f(sc.get("strength"),0) or 0
            agg=(d.get("aggression") or {}).get("1m") or {}
            return f"{sym}: Scanner {direction} ({strength:.0f}/100). Agresion 1m: {str(agg.get('direction') or 'NEUTRAL')}. Spot {spot if spot is not None else 'no disponible'}."
        if "flujo inusual" in t:
            ev=d.get("flow_unusual")
            if not ev:return f"{sym}: no tengo una senal de Flujo Inusual activa en el estado actual."
            return f"{sym}: ultimo Flujo Inusual {_side_es(ev.get('side'))}, score {float(ev.get('score') or 0):.0f}, strike {ev.get('strike') if ev.get('strike') is not None else '—'}, a las {str(ev.get('timestamp') or '')[-14:-6] or '—'}."
        if "agresion" in t or "aggression" in t:
            frames=d.get("aggression") or {}
            parts=[f"{k} {str((frames.get(k) or {}).get('direction') or 'NEUTRAL')}" for k in ("1m","3m","5m","15m")]
            return f"{sym}: agresion actual: "+", ".join(parts)+". 1m es el gatillo; 3m transicion; 5m desarrollo; 15m contexto."
        if any(x in t for x in ("entro","meto la operacion","conviene entrar","puedo comprar","puedo vender")):
            direction=str(sc.get("direction") or "WAIT").upper();edge=str(sc.get("edge_state") or "NO EDGE");agg1=((d.get("aggression") or {}).get("1m") or {}).get("direction")
            return f"{sym}: Scanner manda {direction} con estado {edge}; Aggression 1m esta {agg1 or 'NEUTRAL'}. Sophia no crea una direccion nueva: usa el nivel/Scanner y la agresion solo como confirmacion de timing."
        if "trace" in t:
            tr=d.get("trace_orderflow") or {};conf=tr.get("confirmation") or {};gate=tr.get("timing_gate") or {}
            return f"{sym}: TRACE timing {gate.get('state') or conf.get('state') or 'sin confirmacion'}; confirmacion {conf.get('note') or conf.get('state') or '—'}. Scanner sigue siendo la autoridad."
        if "volatilidad" in t or "volatility" in t:
            v=d.get("volatility") or {}
            return f"{sym}: volatilidad {v.get('state') or v.get('regime') or v.get('label') or 'disponible en HOT STATE'}; IV {v.get('iv') if v.get('iv') is not None else v.get('atm_iv','—')}; expected move {v.get('expected_move') if v.get('expected_move') is not None else '—'}."
        if any(x in t for x in ("niveles","nivel clave","soporte","resistencia","objetivo")):
            levels=d.get("targets_levels") or {}
            return f"{sym}: niveles activos: {json.dumps(levels,ensure_ascii=False)[:650] if levels else 'no disponibles en este corte'}."
        if "gamma" in t and "delta" in t:
            a=d.get("gamma_delta_alignment") or {}
            return f"{sym}: Gamma+Delta = {a.get('label') or 'sin lectura'} (score {a.get('score') if a.get('score') is not None else '—'}). Gamma center {d['gamma'].get('center')}; Delta center {d['delta'].get('center')}."
        return None

    async def handle_message(self,message:str,state:dict[str,Any],aggression:dict[str,Any]|None=None) -> dict[str,Any]:
        text=str(message or "").strip()
        t=_norm(text)
        if not text:return {"ok":False,"reply":"Dime que quieres revisar."}
        if any(x in t for x in ("que estas vigilando","que vigilas","avisos activos","watch rules")):
            ws=self.watches()
            if not ws:return {"ok":True,"reply":"No tengo avisos activos.","watches":[]}
            return {"ok":True,"reply":"Estoy vigilando: "+"; ".join(w["label"] for w in ws)+".","watches":ws}
        if any(x in t for x in ("cancela todos","borra todos los avisos","quita todos los avisos")):
            n=self.clear_watches();return {"ok":True,"reply":f"Cancele {n} avisos.","watches":[]}
        if any(x in t for x in ("cancela ese aviso","cancela el aviso","quita ese aviso","cancela ultimo")):
            w=self.cancel_watch();return {"ok":True,"reply":f"Cancele {w.label}." if w else "No habia un aviso reciente que cancelar.","watches":self.watches()}
        rule=self.parse_watch(text)
        if rule:
            blocked,why=self._publication_blocked(state)
            suffix=" El aviso queda armado, pero no disparara una lectura direccional hasta que el circuito de frescura se rearme." if blocked and rule.kind!="PRICE" else ""
            return {"ok":True,"reply":f"Estoy vigilando {rule.label}. Te aviso apenas se cumpla.{suffix}","watch":asdict(rule),"watches":self.watches(),"publication_blocked":blocked,"publication_block_reason":why if blocked else None}
        blocked,why=self._publication_blocked(state)
        if blocked:
            return {"ok":True,"blocked":True,"reply":f"Publicacion cuantitativa bloqueada por frescura: {why}. Sophia no emitira direccion, entrada ni confirmacion hasta que el circuito se rearme.","source":"FRESHNESS_CIRCUIT","watches":self.watches()}
        direct=self._internal_answer(text,state,aggression)
        if direct:return {"ok":True,"reply":direct,"source":"ITM_HOT_STATE","watches":self.watches()}
        local=await self.local_llm.answer(text,self._digest(state,aggression))
        if local:
            checked=validate_against_scanner(local,state)
            return {"ok":True,"reply":checked["text"],
                    "source":"LOCAL_SELF_HOSTED_LLM_BLOCKED" if checked.get("blocked") else "LOCAL_SELF_HOSTED_LLM_VALIDATED",
                    "llm_validator":checked,"watches":self.watches()}
        return {"ok":True,"reply":"Puedo leer el estado operativo de ITM QUANT y tus avisos ahora mismo. Para preguntas financieras abiertas, el modelo local de Sophia se activara en el VPS sin creditos por consulta.","source":"DETERMINISTIC_CORE","watches":self.watches()}

    def _trigger(self,rule:WatchRule,message:str,context:dict[str,Any],signature:str) -> dict[str,Any]|None:
        if signature and signature==rule.last_signature:return None
        rule.last_signature=signature or datetime.now(timezone.utc).isoformat();rule.trigger_count+=1
        payload={"type":"SOPHIA_ALERT","watch_id":rule.id,"kind":rule.kind,"message":message,"context":context,"voice":True}
        self.events.publish_nowait(payload)
        if rule.one_shot:
            rule.active=False
            with self._lock:self._watches.pop(rule.id,None)
        return payload

    def evaluate(self,state:dict[str,Any],aggression:dict[str,Any]|None=None) -> list[dict[str,Any]]:
        now=datetime.now(timezone.utc);session_day=datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        if self._last_session_date and self._last_session_date!=session_day:
            with self._lock:
                self._watches={k:v for k,v in self._watches.items() if not v.session_only}
        self._last_session_date=session_day
        with self._lock:rules=list(self._watches.values())
        out=[];spot=_f(state.get("spot"));sc=state.get("scanner") or {};flow=(state.get("flow_pro") or {}).get("latest") or {};frames=(aggression or {}).get("frames") or {}
        blocked,_why=self._publication_blocked(state)
        for r in rules:
            if not r.active:continue
            if blocked and r.kind!="PRICE":
                continue
            p=r.params
            if r.kind=="UNUSUAL_FLOW" and flow:
                side=str(flow.get("side") or "").upper();score=float(flow.get("score") or 0);wanted=str(p.get("side") or "ANY").upper()
                seq=int(flow.get("fabric_seq") or 0);min_seq=int(p.get("min_fabric_seq") or 0)
                if seq>min_seq and score>=float(p.get("min_score") or 70) and (wanted=="ANY" or wanted==side):
                    sig=f"{flow.get('timestamp')}|{side}|{flow.get('strike')}|{score:.1f}"
                    strike=flow.get("strike");stamp=str(flow.get("timestamp") or "")
                    try: hhmm=datetime.fromisoformat(stamp.replace("Z","+00:00")).strftime("%H:%M:%S")
                    except Exception: hhmm=stamp[-8:] if len(stamp)>=8 else ""
                    loc=f", strike {float(strike):g}" if strike is not None else ""
                    tm=f", {hhmm}" if hhmm else ""
                    msg=f"Flujo Inusual {_side_es(side)} detectado en {state.get('active_symbol')}{loc}{tm}."
                    ev=self._trigger(r,msg,{"side":side,"score":score,"strike":strike,"timestamp":flow.get("timestamp")},sig)
                    if ev:out.append(ev)
            elif r.kind=="AGGRESSION":
                tf=str(p.get("timeframe") or "1m");fr=frames.get(tf) or {};side=str(fr.get("direction") or "NEUTRAL").upper();wanted=str(p.get("side") or "ANY").upper()
                if side in {"BUY","SELL"} and (wanted=="ANY" or wanted==side):
                    sig=f"{tf}|{side}|{(aggression or {}).get('revision')}"
                    ev=self._trigger(r,f"Agresion {tf} cambio a {_side_es(side)} en {state.get('active_symbol')}.",{"timeframe":tf,"side":side,"score":fr.get("score")},sig)
                    if ev:out.append(ev)
            elif r.kind=="PRICE" and spot is not None:
                rel=str(p.get("relation") or "TOUCH")
                level_name=p.get("level_name")
                if level_name:
                    # Resolve the CURRENT value every evaluation pass -- never the value
                    # at watch-creation time, since these levels move every session.
                    klr=state.get("key_levels_report") or {}
                    level=_f(klr.get(_KEY_LEVELS_REPORT_FIELD.get(level_name,"")))
                    if level is None:continue  # level not published yet; wait for the next pass
                    label=_NAMED_LEVEL_LABELS.get(level_name,level_name)
                else:
                    level=float(p.get("level"));label=f"{level:g}"
                tol=max(0.02,abs(level)*0.0002)
                ok=(abs(spot-level)<=tol) if rel=="TOUCH" else spot>=level if rel=="ABOVE" else spot<=level
                if ok:
                    ev=self._trigger(r,f"{state.get('active_symbol')} llego a {spot:.2f} cerca de {label} ({level:g}).",{"spot":spot,"level":level,"level_name":level_name,"relation":rel},f"{rel}|{level_name or level}|{round(spot,4)}")
                    if ev:out.append(ev)
            elif r.kind=="SCANNER":
                side=str(sc.get("direction") or "").upper();wanted=str(p.get("side") or "ANY").upper();strength=float(sc.get("strength") or sc.get("evidence_score") or 0);min_s=p.get("min_strength")
                if side in {"BUY","SELL"} and (wanted=="ANY" or side==wanted) and (min_s is None or strength>=float(min_s)):
                    sig=f"{side}|{round(strength,1)}|{sc.get('edge_state')}"
                    ev=self._trigger(r,f"Scanner esta en {_side_es(side)} con fuerza {strength:.0f}/100.",{"side":side,"strength":strength,"edge_state":sc.get("edge_state")},sig)
                    if ev:out.append(ev)
            elif r.kind=="GAMMA_DELTA":
                a=state.get("gamma_delta_alignment") or {};label=str(a.get("label") or "").upper();wanted=str(p.get("side") or "ANY").upper()
                side="BUY" if any(x in label for x in ("BUY","BULL","POSITIVE","ALCISTA")) else "SELL" if any(x in label for x in ("SELL","BEAR","NEGATIVE","BAJISTA")) else "NEUTRAL"
                if side in {"BUY","SELL"} and (wanted=="ANY" or wanted==side):
                    sig=f"{label}|{a.get('score')}"
                    ev=self._trigger(r,f"Gamma y Delta se alinearon hacia {_side_es(side)}.",{"label":label,"score":a.get("score"),"side":side},sig)
                    if ev:out.append(ev)
        return out


SOPHIA = SophiaRuntime()

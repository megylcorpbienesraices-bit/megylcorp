from __future__ import annotations

import asyncio
import os
import traceback
import json
import time
import uuid
import pandas as pd
from .core.frame_guards import numeric_column
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import Request, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import (
    APP_NAME, REFRESH_SECONDS, FLOW_REFRESH_SECONDS, PERSISTENCE_REPORT,
    ADAPTIVE_REFRESH_ENABLED, ADAPTIVE_STRUCT_ACTIVE_SECONDS, ADAPTIVE_STRUCT_NORMAL_SECONDS,
    ADAPTIVE_STRUCT_QUIET_SECONDS, ADAPTIVE_FLOW_ACTIVE_SECONDS, ADAPTIVE_FLOW_NORMAL_SECONDS,
    ADAPTIVE_FLOW_QUIET_SECONDS, ADAPTIVE_WAKE_SECONDS,
)
from .persistence import category_dir
from .version import APP_VERSION
from .service import STATE, PlatformState, _jsonable, is_transient_chain_failure
from .core import alpaca_data
from .core.replay import session_clock as replay_session_clock
from .core.replay import available_sessions as replay_available_sessions
from app.persistence import routed_dir
import datetime as _dt
import logging
from .core.assets import (selectable_assets, core_selectable_assets, asset_info,
                          register_provider_assets, apply_roster_capabilities)
from .core.provider_asset_catalog import load_cached_universe, sync_provider_asset_catalog
from .core.asset_ecosystems import public_summary
from .core.live_price import PRICE_STREAM
from .core.option_stream import OPTION_STREAM
from .core import terminal_metrics
from .core.low_latency_bridge import RUST_CAUSAL_BRIDGE, RUST_CAUSAL_INGRESS
from .core.binary_protocol import pack_surface_frame, pack_tick_batch
from .core.trace_contract import normalize_trace_pulse_contract
from .core.live_scheduler import AdaptiveLiveScheduler, SchedulerCadence
from .core.provider_bus import PROVIDER_BUS, FEATURE_BUS
from .core.provider_flow_fabric import OPTION_FLOW_FABRIC, PRICE_TICK_FABRIC
from .core.flow_intelligence import flow_session_phase
from .core.provider_data_lake import DATA_LAKE
from .core.provider_library import PROVIDER_LIBRARY
from .core.ecosystem_runtime import ECOSYSTEM_RUNTIME
from .core.market_truth import MARKET_TRUTH
from .core.feature_intelligence import build_feature_intelligence
from .core.research_validation import build_research_validation
from .core.temporal_causality import CAUSAL_RUNTIME
from .core.chart_data_cache import CHART_DATA_CACHE
from .core.always_on_state import READY_STORE
from .core.aggression_delta import AGGRESSION_DELTA, TIMEFRAMES
from .core.sophia_core import SOPHIA
from .core.sophia_voice import SOPHIA_VOICE
from .core.temporal_truth import TEMPORAL_TRUTH
from .core.versioned_market_state import VERSIONED_MARKET_STATE
from .core.profile_engine import build_profile_bundle
from .core.expiry_intelligence import build_expiry_intelligence
from .core.derivatives_intelligence import build_derivatives_intelligence
from .core.transport_benchmark import benchmark_transport, transport_policy
from .providers.tastytrade import TASTYTRADE
from .providers.quantdata import QUANTDATA
from .providers.quantdata.intelligence import QUANTDATA_INTELLIGENCE
from .core.provider_parity import parity_report, parity_policy
from .terminal_api import build_terminal_bundle, build_diagnostics
from .core.replay import ReplayContext
from .core.obs import note as _obs_note, expected as _obs_expected
from .core import obs, net_guard
from .core.model_governance import governance_contract
from .core.tool_registry import TOOL_REGISTRY, registry_payload, build_tool_digest, digest_signature, quality_flags
from .core.structured_alerts import STRUCTURED_ALERTS
from .core.conditional_outcomes import conditional_outcome_report

BASE = os.path.dirname(__file__)
# Cada cuántos segundos se permite reconstruir el trace para revalorizar los
# Greeks contra el spot LIVE. Por debajo de esto se sirve de caché.
TRACE_LIVE_REPRICE_SECONDS = max(1.0, float(os.getenv("ITM_TRACE_LIVE_REPRICE_SECONDS", "2")))

LOG_DIR = category_dir("audit") / "system_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
ERROR_LOG = LOG_DIR / "server.log"
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


LIVE_REFRESH_SCHEDULER = AdaptiveLiveScheduler(SchedulerCadence(
    structural_active=ADAPTIVE_STRUCT_ACTIVE_SECONDS,
    structural_normal=ADAPTIVE_STRUCT_NORMAL_SECONDS,
    structural_quiet=ADAPTIVE_STRUCT_QUIET_SECONDS,
    flow_active=ADAPTIVE_FLOW_ACTIVE_SECONDS,
    flow_normal=ADAPTIVE_FLOW_NORMAL_SECONDS,
    flow_quiet=ADAPTIVE_FLOW_QUIET_SECONDS,
    wake=ADAPTIVE_WAKE_SECONDS,
))


_ASSET_WARM_TASKS: dict[int, asyncio.Task] = {}
_TRACE_BOOT_TASKS: dict[int, asyncio.Task] = {}
# El bucle de eventos solo guarda referencias DEBILES a las tareas. Una tarea de
# fondo cuya unica referencia fuerte es una variable local del handler puede ser
# recolectada a media ejecucion en cuanto el handler retorna. Por eso todo lo que
# se lanza en segundo plano vive en uno de estos registros hasta que termina.
_TASTY_PREPARE_TASKS: dict[int, asyncio.Task] = {}
_READY_PERSIST_LOCK = asyncio.Lock()
_LAST_READY_PERSIST_MONO = 0.0
_SOPHIA_OPTION_CURSOR: dict[str, int] = {}
# Cache exclusivo de la sección observacional; nunca se guarda dentro de STATE.
_RETURN_ANOMALY_CACHE: dict[tuple, dict] = {}


def _build_ready_package_sync(state_obj: PlatformState, *, source: str = "LIVE_ENGINE_PRECOMPUTED", seal: bool = False):
    """Pre-render the default terminal payload away from the HTTP request path."""
    if state_obj.replay_context.is_replay or not state_obj.gamma_delta:
        return {"ok": False, "reason": "LIVE_READY_STATE_REQUIRED"}
    state=_jsonable(state_obj.public_state())
    charts=_jsonable(state_obj.charts(view="all"))
    tables=_jsonable(state_obj.tables())
    by_tf={f"{m}m":AGGRESSION_DELTA.snapshot(state_obj.symbol,f"{m}m",limit=500) for m in TIMEFRAMES}
    state["aggression_delta"]={"ready":any((x or {}).get("ready") for x in by_tf.values()),"symbol":state_obj.symbol,"by_timeframe":by_tf,
                               "frames":(by_tf.get("1m") or {}).get("frames",{}),"role":"ENTRY_TIMING_CONFIRMATION_ONLY","scanner_authority":True,"precomputed":True}
    state["sophia"]={**SOPHIA.status(),"voice":SOPHIA_VOICE.status()}
    day=pd.Timestamp.now(tz="America/New_York").date().isoformat()
    return READY_STORE.save_ready_package(state_obj.symbol,state=state,charts=charts,tables=tables,session_day=day,source=source,seal=seal)


async def _persist_current_ready_package(*, force: bool = False, source: str = "LIVE_ENGINE_PRECOMPUTED"):
    """Keep browser-ready state/charts warm without delaying provider/Scanner work."""
    global _LAST_READY_PERSIST_MONO
    if STATE.replay_context.is_replay or not STATE.gamma_delta:
        return {"ok":False,"reason":"NOT_LIVE_READY"}
    now=asyncio.get_running_loop().time()
    if not force and now-_LAST_READY_PERSIST_MONO < float(os.getenv("ITM_READY_PACKAGE_SECONDS","20")):
        return {"ok":False,"reason":"THROTTLED"}
    async with _READY_PERSIST_LOCK:
        now=asyncio.get_running_loop().time()
        if not force and now-_LAST_READY_PERSIST_MONO < float(os.getenv("ITM_READY_PACKAGE_SECONDS","20")):
            return {"ok":False,"reason":"THROTTLED"}
        rep=await asyncio.to_thread(_build_ready_package_sync,STATE,source=source,seal=False)
        if rep.get("ok"):
            _LAST_READY_PERSIST_MONO=now
            terminal_metrics.inc("always_on_ready_package_write")
        return rep


def _ready_has_v1270_payload(pkg: dict | None) -> bool:
    """True only when a READY package already carries the v1.27 entry-timing payload."""
    if not isinstance(pkg, dict):
        return False
    state = pkg.get("state") or {}
    agg = state.get("aggression_delta") or {}
    by = agg.get("by_timeframe") or {}
    return all(isinstance(by.get(tf), dict) for tf in ("1m", "3m", "5m", "15m"))


def _precompute_session_sync(symbol: str, day: str):
    """Build one date package proactively; stale pre-v1.27 packages are upgraded in background."""
    existing = READY_STORE.load_session(symbol,day)
    if _ready_has_v1270_payload(existing):
        return {"ok":True,"already_ready":True,"date":day}
    worker=PlatformState(symbol=str(symbol).upper())
    worker.replay_context=ReplayContext.at(day,None,worker.symbol)
    bundle=worker._build_replay_bundle(force=True)
    if not bundle.get("ready"):
        return {"ok":False,"date":day,"reason":bundle.get("error") or "REPLAY_NOT_READY"}
    state=_jsonable(worker._public_replay_state())
    charts=_jsonable(worker.charts(view="all"))
    tables=_jsonable(worker.tables())
    state["aggression_delta"]=_jsonable(AGGRESSION_DELTA.build_all_from_dataframe(worker.symbol,bundle.get("tape"),limit=500))
    state["sophia"]={**SOPHIA.status(),"voice":SOPHIA_VOICE.status(),"historical_context":True}
    return READY_STORE.save_ready_package(worker.symbol,state=state,charts=charts,tables=tables,session_day=day,source="HISTORICAL_BACKGROUND_PRECOMPUTE",seal=True,publish_live=False)


async def _historical_precompute_loop(symbol: str):
    """Low-priority one-time indexer so historical dates are instant when clicked."""
    await asyncio.sleep(float(os.getenv("ITM_HISTORY_PRECOMPUTE_DELAY_SECONDS","8")))
    sym=str(symbol).upper()
    try:
        days=replay_available_sessions(alpaca_data.DATA_DIR,sym)[:int(os.getenv("ITM_HISTORY_PRECOMPUTE_SESSIONS","60"))]
    except Exception:
        days=[]
    for day in days:
        try:
            existing = READY_STORE.load_session(sym,day)
            if _ready_has_v1270_payload(existing):
                continue
            await asyncio.to_thread(_precompute_session_sync,sym,day)
            terminal_metrics.inc("historical_session_precomputed")
        except Exception:
            terminal_metrics.inc("historical_session_precompute_error")
        await asyncio.sleep(0.15)

async def _warm_asset_quant(symbol: str, epoch: int):
    """Build heavy Quant state off the request path with progressive publication and watchdog."""
    target=str(symbol).upper()
    worker=STATE.make_asset_warm_worker(target, epoch)
    core_adopted=False
    publication_open=True
    loop=asyncio.get_running_loop()

    def report_progress(phase: str, pct: int, detail: str):
        # Worker runs in a thread; marshal UI progress safely back to the event loop.
        loop.call_soon_threadsafe(
            lambda ph=phase,pc=pct,de=detail: STATE.update_asset_warmup(
                target,epoch,phase=str(ph),progress_pct=int(pc),detail=str(de)))

    worker.warm_progress_callback=report_progress

    def publish_core(w):
        nonlocal core_adopted
        if core_adopted or not publication_open:
            return
        if STATE.adopt_asset_core_worker(w, target, epoch):
            core_adopted=True
            try:
                if w.snapshot is not None and not w.snapshot.empty:
                    OPTION_STREAM.set_universe(w.snapshot)
            except Exception as _e:
                _obs_note('main:197', _e)
            terminal_metrics.inc("asset_warmup_core_ready")

    try:
        STATE.update_asset_warmup(target, epoch, phase="CHAIN_DISCOVERY", progress_pct=30, detail="Cadena propia + proveedores en paralelo")
        timeout=max(30.0,min(float(os.getenv("ITM_QUANT_WARMUP_TIMEOUT_SECONDS","90")),240.0))
        deadline=loop.time()+timeout
        last_exc=None
        # Retry only transient chain/bootstrap failures. Deterministic auth/configuration
        # errors still fail fast. The total watchdog budget is shared across every attempt.
        for attempt in range(3):
            remaining=max(0.25,deadline-loop.time())
            try:
                await asyncio.wait_for(asyncio.to_thread(worker.refresh, False, True, publish_core), timeout=remaining)
                last_exc=None
                break
            except asyncio.TimeoutError as exc:
                publication_open=False
                terminal_metrics.inc("asset_warmup_timeout")
                raise RuntimeError(f"WATCHDOG: Quant excedió {timeout:.0f}s; precio/TRACE siguen operativos") from exc
            except Exception as exc:
                last_exc=exc
                if attempt>=2 or not is_transient_chain_failure(exc) or loop.time()>=deadline:
                    raise
                retry_delay=(0.75,1.5)[attempt]
                STATE.update_asset_warmup(target, epoch, phase="CHAIN_RETRY", progress_pct=34+attempt*2, detail=f"Proveedor temporalmente ocupado; reintento automático {attempt+2}/3")
                terminal_metrics.inc("asset_warmup_chain_retry")
                await asyncio.sleep(min(retry_delay,max(0.0,deadline-loop.time())))
        if last_exc is not None:
            raise last_exc
        STATE.update_asset_warmup(target, epoch, phase="PUBLISHING", progress_pct=97, detail="Publicando diagnósticos finales versionados")
        if STATE.adopt_asset_warm_worker(worker, target, epoch):
            try:
                if worker.snapshot is not None and not worker.snapshot.empty:
                    OPTION_STREAM.set_universe(worker.snapshot)
            except Exception as _e:
                _obs_note('main:233', _e)
            terminal_metrics.inc("asset_warmup_success")
            try:
                await _persist_current_ready_package(force=True,source="QUANT_WARMUP_READY")
            except Exception:
                terminal_metrics.inc("always_on_ready_package_error")
        else:
            terminal_metrics.inc("asset_warmup_stale")
    except Exception as exc:
        publication_open=False
        STATE.fail_asset_warmup(target, epoch, exc)
        terminal_metrics.inc("asset_warmup_error")
    finally:
        worker.warm_progress_callback=None
        _ASSET_WARM_TASKS.pop(int(epoch), None)

def _schedule_asset_warmup(symbol: str, epoch: int):
    task=asyncio.create_task(_warm_asset_quant(symbol, int(epoch)))
    _ASSET_WARM_TASKS[int(epoch)]=task
    return task



async def _prime_trace_history(symbol: str, epoch: int):
    """Prime observed price history before Quant is ready. Never blocks Scanner math."""
    target=str(symbol).upper()
    try:
        STATE.update_asset_warmup(target, epoch, phase="PRICE_HISTORY", progress_pct=18, detail="Cargando histórico observado para TRACE")
        # Cache is effectively free; provider backfill runs only if cache cannot satisfy the session.
        cached=await asyncio.to_thread(STATE.trace_session_bootstrap,"1m",False,target,"full_day",True,True)
        if not cached.get("ready"):
            await asyncio.to_thread(STATE.trace_session_bootstrap,"1m",True,target,"full_day",False,True)
        STATE.update_asset_warmup(target, epoch, phase="PRICE_READY", progress_pct=25, detail="Precio/TRACE listos; Quant continúa en segundo plano")
        terminal_metrics.inc("instant_boot_price_history_success")
    except Exception:
        terminal_metrics.inc("instant_boot_price_history_error")
    finally:
        _TRACE_BOOT_TASKS.pop(int(epoch),None)

def _schedule_tastytrade_prepare(symbol: str, epoch: int):
    """Adelanta el arranque de DXLink para el failover de cadena propia.

    No bloquea el cambio de activo, pero SI tiene que sobrevivir a que el handler
    retorne: es justo el trabajo que hace que el siguiente activo cargue rapido.
    """
    old=_TASTY_PREPARE_TASKS.get(int(epoch))
    if old and not old.done():
        return old
    task=asyncio.create_task(TASTYTRADE.select_asset(symbol),
                             name=f"itmq-tastytrade-{str(symbol).upper()}-{int(epoch)}")

    def _done(t: asyncio.Task, _epoch: int = int(epoch)) -> None:
        _TASTY_PREPARE_TASKS.pop(_epoch, None)
        if t.cancelled():
            return
        exc=t.exception()
        if exc is not None:
            # Sin esto la excepcion queda sin recoger y solo aparece como un aviso
            # suelto del recolector, sin decir que activo fallo.
            _obs_note('main:tastytrade_prepare', exc, severity="DEGRADED")

    task.add_done_callback(_done)
    _TASTY_PREPARE_TASKS[int(epoch)]=task
    return task


def _schedule_trace_bootstrap(symbol: str, epoch: int):
    old=_TRACE_BOOT_TASKS.get(int(epoch))
    if old and not old.done():
        return old
    task=asyncio.create_task(_prime_trace_history(symbol,int(epoch)),name=f"itmq-price-bootstrap-{str(symbol).upper()}-{int(epoch)}")
    _TRACE_BOOT_TASKS[int(epoch)]=task
    return task

async def background_loop():
    """Adaptive heavy-refresh loop. SIP/OPRA streams themselves stay event-driven."""
    last_fixed_refresh = 0.0
    last_fixed_flow = 0.0
    while True:
        try:
            now = asyncio.get_running_loop().time()
            warm = dict(STATE.asset_warmup or {})
            if warm.get("active"):
                # Do not launch a second heavy refresh while the isolated symbol worker
                # is already building the new asset. UI/state reads remain non-blocking.
                await asyncio.sleep(max(0.25, min(1.0, ADAPTIVE_WAKE_SECONDS)))
                continue
            market_state = (STATE.meta or {}).get("market_state", "")
            flow_clock_state = "LONDON" if STATE.mode == "LIVE" and str(market_state).upper() == "CLOSED" and flow_session_phase() == "LONDON" else market_state
            if ADAPTIVE_REFRESH_ENABLED:
                LIVE_REFRESH_SCHEDULER.observe(
                    PRICE_TICK_FABRIC.scheduler_status(STATE.symbol), OPTION_FLOW_FABRIC.scheduler_status(STATE.symbol),
                    market_mode=STATE.mode, market_state=flow_clock_state, now=now,
                )
                structural_due, flow_due = LIVE_REFRESH_SCHEDULER.due(now=now)
                due = structural_due or flow_due
                fetch_flow = bool(flow_due)
                sleep_for = LIVE_REFRESH_SCHEDULER.cadence.wake
            else:
                if STATE.mode == "LIVE" and market_state == "REGULAR":
                    structural_interval = float(REFRESH_SECONDS)
                elif STATE.mode == "LIVE" and market_state == "PREMARKET":
                    structural_interval = max(45.0, float(REFRESH_SECONDS))
                elif STATE.mode == "LIVE" and flow_session_phase() == "LONDON":
                    structural_interval = max(20.0, float(REFRESH_SECONDS))
                else:
                    structural_interval = 90.0
                due = now - last_fixed_refresh >= structural_interval
                fetch_flow = now - last_fixed_flow >= float(FLOW_REFRESH_SECONDS)
                due = due or fetch_flow
                sleep_for = min(2.0, max(0.5, ADAPTIVE_WAKE_SECONDS))

            if not due:
                await asyncio.sleep(sleep_for)
                continue

            await asyncio.to_thread(STATE.refresh, fetch_flow)
            terminal_metrics.inc("refresh_success")
            try:
                await _persist_current_ready_package(force=False,source="ALWAYS_ON_BACKGROUND")
            except Exception:
                terminal_metrics.inc("always_on_ready_package_error")
            if ADAPTIVE_REFRESH_ENABLED:
                LIVE_REFRESH_SCHEDULER.mark_refresh(structural=True, flow=fetch_flow, now=now)
                sch = LIVE_REFRESH_SCHEDULER.snapshot()
                terminal_metrics.set_gauge("adaptive_refresh_interval_s", sch.get("structural_interval_s", 0))
                terminal_metrics.set_gauge("adaptive_flow_interval_s", sch.get("flow_interval_s", 0))
                terminal_metrics.set_gauge("sip_trade_rate_eps", sch.get("sip_trade_rate_eps", 0))
                terminal_metrics.set_gauge("opra_trade_rate_eps", sch.get("opra_trade_rate_eps", 0))
            else:
                last_fixed_refresh = now
                if fetch_flow:
                    last_fixed_flow = now
            try:
                terminal_metrics.set_gauge("data_quality", (STATE.data_quality_report or {}).get("score", 0))
                terminal_metrics.set_gauge("model_health", (STATE.model_health or {}).get("score", 0))
                terminal_metrics.set_gauge("dealer_confidence", (STATE.dealer_intelligence_report or {}).get("confidence", 0))
                terminal_metrics.set_gauge("causal_event_count", (STATE.causality_report or {}).get("event_count", 0))
                terminal_metrics.set_gauge("latency_p95_ms", ((STATE.causality_report or {}).get("latency") or {}).get("p95_ms", 0))
            except Exception as _e:
                _obs_note('main:346', _e)
        except Exception:
            terminal_metrics.inc("refresh_error")
            sleep_for = max(1.0, ADAPTIVE_WAKE_SECONDS)
        await asyncio.sleep(sleep_for)


def _publication_gate_snapshot() -> dict:
    """Current fail-closed publication gate; raw ingestion may continue while open.

    Freshness is evaluated against the latest observed underlying event, not only
    against the timestamp captured by the slower structural-chain refresh.
    """
    try:
        gate=STATE.current_publication_gate()
        if isinstance(gate,dict):
            return dict(gate)
    except Exception as _e:
        _obs_note('main:publication_gate_snapshot', _e)
    return {"estado":"UNKNOWN","publicar_permitido":False,"motivo":"frescura critica no verificable"}

def _publication_blocked_payload(surface: str) -> dict | None:
    if STATE.replay_context.is_replay:
        return None
    gate=_publication_gate_snapshot()
    if gate.get("publicar_permitido") is True:
        return None
    return {"ready":False,"blocked":True,"reason":"PUBLICATION_BLOCKED_STALE_DATA","surface":surface,"symbol":STATE.symbol,"symbol_epoch":int(STATE.symbol_epoch),"publication_gate":gate}

def _pack_sophia_flow(row: dict | None) -> dict | None:
    if not isinstance(row, dict) or not row:
        return None
    sign=int(float(row.get("direction_sign") or 0))
    ts=row.get("timestamp")
    try: ts=pd.Timestamp(ts).isoformat()
    except Exception: ts=str(ts or "")
    return {"timestamp":ts,"side":"BUY" if sign>0 else "SELL" if sign<0 else "NEUTRAL",
            "score":float(row.get("flow_score") or 0),"strike":row.get("strike"),
            "amount":float(row.get("premium") or row.get("directional_premium") or 0),
            "source":row.get("canonical_source") or row.get("provider_source") or row.get("flow_source"),
            "fabric_seq":int(row.get("fabric_seq") or 0)}


def _sophia_hot_state(flow_override: dict | None = None) -> dict:
    """Small in-memory state digest for Sophia watches; no heavy chart/render work."""
    with STATE.lock:
        sym=str(STATE.symbol).upper()
        scanner=dict(STATE.scanner or {})
        gd=dict(STATE.gamma_delta or {}) if isinstance(STATE.gamma_delta,dict) else {}
        meta=dict(STATE.meta or {})
    consensus=PROVIDER_BUS.snapshot(sym)
    spot=consensus.get("consensus_price") if consensus.get("ready") else gd.get("spot")
    flow_latest=flow_override if isinstance(flow_override,dict) else _pack_sophia_flow(OPTION_FLOW_FABRIC.latest(sym))
    return {"ready":bool(gd),"active_symbol":sym,"spot":spot,"scanner":scanner,"meta":meta,
            "gamma_center":gd.get("gamma_center"),"delta_center":gd.get("delta_center"),"gamma_flip":gd.get("gamma_flip"),
            "gamma_migration":{"direction":gd.get("migration_direction"),"strength":gd.get("migration_strength")},
            "delta_migration":{"direction":gd.get("delta_migration_direction"),"strength":gd.get("delta_migration_strength")},
            "gamma_delta_alignment":{"label":gd.get("gamma_delta_alignment_label"),"score":gd.get("gamma_delta_alignment_score")},
            "flow_pro":{"state":"ACTIVE" if flow_latest and float(flow_latest.get("score") or 0)>=70 else "WAITING","latest":flow_latest},
            "data_quality":(STATE.data_quality_report or {}).get("score") if isinstance(STATE.data_quality_report,dict) else None,
            "publication_gate":_publication_gate_snapshot(),
            "model_health":STATE.model_health if isinstance(STATE.model_health,(int,float)) else (STATE.model_health or {}).get("score") if isinstance(STATE.model_health,dict) else None}


def _tool_hot_state() -> dict:
    """Bounded cached state for Tool Runtime; never performs provider/network work."""
    base=_sophia_hot_state()
    with STATE.lock:
        base.update({
            "symbol_epoch":int(STATE.symbol_epoch),
            "calibration":dict(STATE.calibration or {}) if isinstance(STATE.calibration,dict) else {},
            "regime_context":dict(STATE.regime_context or {}) if isinstance(STATE.regime_context,dict) else {},
            "structural_intelligence":dict(STATE.structural_intelligence_report or {}) if isinstance(STATE.structural_intelligence_report,dict) else {},
            "trace_orderflow":dict(STATE.trace_orderflow_report or {}) if isinstance(STATE.trace_orderflow_report,dict) else {},
            "macro":dict(STATE.macro or {}) if isinstance(STATE.macro,dict) else {},
            "large_prints":dict(STATE.large_print_summary or {}) if isinstance(STATE.large_print_summary,dict) else {},
            "liquidity_zones":((STATE.large_print_summary or {}).get("liquidity_zones",{}) if isinstance(STATE.large_print_summary,dict) else {}),
            "volatility":dict(STATE.vol or {}) if isinstance(STATE.vol,dict) else {},
            "net_drift_summary":{},
            "decision_intelligence":dict(STATE.decision_intelligence_report or {}) if isinstance(STATE.decision_intelligence_report,dict) else {},
            "market_truth":dict(STATE.market_truth_report or {}) if isinstance(STATE.market_truth_report,dict) else {},
            "data_quality_report":dict(STATE.data_quality_report or {}) if isinstance(STATE.data_quality_report,dict) else {},
            "last_refresh_ec":STATE.last_refresh_ec,
        })
    base["conditional_outcomes"]=conditional_outcome_report(base.get("scanner") or {},base.get("calibration") or {},base.get("regime_context") or {})
    base["tool_quality_flags"]=quality_flags(base)
    return base


async def aggression_sophia_loop():
    """Always-on entry timing + Sophia event watches, independent of the browser."""
    last_eval=0.0
    while True:
        try:
            sym=str(STATE.symbol).upper()
            AGGRESSION_DELTA.update_from_fabric(sym)
            now=time.monotonic()
            if now-last_eval>=0.20:
                agg=AGGRESSION_DELTA.snapshot(sym,"1m",limit=80)
                # Consume every new canonical option print so a fast unusual event cannot be
                # hidden by a later ordinary print before the next 200 ms watch evaluation.
                cursor=int(_SOPHIA_OPTION_CURSOR.get(sym,0))
                packet=OPTION_FLOW_FABRIC.since(sym,after=cursor,limit=1200)
                events=list(packet.get("events") or [])
                if events:
                    for row in events:
                        packed=_pack_sophia_flow(row)
                        if packed is not None:
                            SOPHIA.evaluate(_sophia_hot_state(packed),agg)
                    _SOPHIA_OPTION_CURSOR[sym]=max(cursor,int(packet.get("last_seq") or cursor))
                # Also evaluate Scanner/Gamma/Delta/price/aggression conditions when no
                # option print arrived. Duplicate signatures suppress repeated alerts.
                _hot=_sophia_hot_state()
                SOPHIA.evaluate(_hot,agg)
                try:
                    _hot["tool_quality_flags"]=quality_flags(_hot)
                    STRUCTURED_ALERTS.evaluate(_hot)
                except Exception as _e:
                    _obs_note('main:structured_alerts',_e)
                last_eval=now
        except asyncio.CancelledError:
            raise
        except Exception:
            terminal_metrics.inc("sophia_aggression_loop_error")
        await asyncio.sleep(0.055)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start hot transports immediately; hydrate Quant/providers in the background.

    v1.27.0 keeps the browser off the critical path: transports, Quant hydration,
    READY snapshots and historical session indexing run in the persistent engine.
    Opening the UI only reads already-computed state whenever it exists.
    """
    # v1.42: el universo es Equity + ETF, no sólo ETF. Se hidrata primero desde la
    # caché para que el buscador sirva antes de que termine la red; el refresco es
    # de fondo. Tras registrar, se recalculan capacidades: un activo cuya cadena
    # ningún proveedor activo sirve deja de ofrecerse como analizable.
    register_provider_assets(load_cached_universe())
    apply_roster_capabilities()
    etf_catalog_task = asyncio.create_task(sync_provider_asset_catalog(), name="itmq-provider-asset-catalog-sync")

    PRICE_STREAM.set_symbol(STATE.symbol)
    PRICE_STREAM.start()
    OPTION_STREAM.start()
    RUST_CAUSAL_INGRESS.start()
    RUST_CAUSAL_BRIDGE.start()

    # Mark initial state as warming without taking the main state lock for heavy work.
    with STATE.lock:
        if not STATE.gamma_delta:
            STATE.asset_warmup = {
                "active": True, "status": "WARMING", "phase": "PRICE_HANDOFF", "progress_pct": 10,
                "symbol": STATE.symbol, "epoch": int(STATE.symbol_epoch),
                "authority": "NO_SIGNAL_UNTIL_READY",
            }
    # Start the structural failover provider first. Yield one event-loop turn so OAuth/
    # discovery can begin before the Quant worker asks for a chain. This does not block
    # HTTP startup; provider and Quant hydration still run concurrently.
    # El roster activo se anuncia en la primera línea del log. Un proveedor retirado
    # que aun así apareciera reconectándose sería inmediatamente visible como
    # contradicción, en vez de confundirse con un fallo del motor.
    from .core.provider_parity import OPTIONS_PEERS as _ACTIVE_PEERS, peer_enabled as _peer_on
    # Retirar un proveedor no puede dejar activos huérfanos que sigan pareciendo
    # analizables y luego muestren paneles vacíos sin explicación. Aquí cada activo
    # se reasigna a quien pueda servirlo de verdad, o se declara por qué nadie puede.
    try:
        _reassigned = apply_roster_capabilities()
        if _reassigned:
            logging.getLogger("itm.providers").info(
                "activos recalculados por el roster: %s",
                ", ".join(f"{k} ({v['motivo']})" for k, v in _reassigned.items()))
    except Exception as _e:
        _obs_note("main:apply_roster_capabilities", _e, severity="DEGRADED")
    logging.getLogger("itm.providers").info(
        "roster de proveedores: %s%s", ", ".join(_ACTIVE_PEERS),
        "" if os.getenv("ITM_OPTIONS_PEERS") else "  (por defecto · ITM_OPTIONS_PEERS para cambiarlo)")
    provider_task = (asyncio.create_task(TASTYTRADE.start(STATE.symbol), name="itmq-tastytrade-start")
                     if _peer_on("TASTYTRADE") else None)
    quantdata_task = asyncio.create_task(QUANTDATA.start(STATE.symbol), name="itmq-quantdata-start")
    # v1.41.0 · carril separado para las páginas integradas del proveedor. Va aparte
    # del carril del motor para que una herramienta de presentación no pueda retrasar
    # ni consumir la cuota que necesita la hidratación de la estructura.
    qd_pages_task = asyncio.create_task(QUANTDATA_INTELLIGENCE.start(STATE.symbol), name="itmq-quantdata-pages-start")
    await asyncio.sleep(0)
    initial_trace = _schedule_trace_bootstrap(STATE.symbol, int(STATE.symbol_epoch))
    initial_quant = asyncio.create_task(_warm_asset_quant(STATE.symbol, int(STATE.symbol_epoch)), name="itmq-initial-quant-hydration")
    task = asyncio.create_task(background_loop(), name="itmq-background-refresh")
    sophia_task = asyncio.create_task(aggression_sophia_loop(), name="itmq-sophia-aggression-always-on")
    # Proactively index every DIRECT DOW instrument that already has persisted replay
    # material. Symbols without raw sessions exit immediately. This keeps the calendar
    # click path read-only/instant once the background index has caught up.
    history_precompute_tasks = [
        asyncio.create_task(_historical_precompute_loop(str(a.get("symbol"))), name=f"itmq-history-precompute-{a.get('symbol')}")
        for a in core_selectable_assets()
    ]
    # WARM/COLD collection is detached from HOT startup. It processes one asset at a
    # time and writes only through the asynchronous data-lake queue.
    PROVIDER_LIBRARY.start()
    # v1.27.0: normalized direct-Dow ecosystem context runs independently of
    # the heavy option/Scanner hydration and never blocks asset switching.
    ECOSYSTEM_RUNTIME.start(STATE.symbol)
    try:
        yield
    finally:
        _shutdown_tasks = [task, sophia_task, initial_trace, initial_quant, provider_task, quantdata_task, qd_pages_task, etf_catalog_task, *history_precompute_tasks]
        for t in _shutdown_tasks:
            if t and not t.done():
                t.cancel()
        try:
            await QUANTDATA_INTELLIGENCE.stop()
        except Exception as _e:
            _obs_note('main:quantdata_pages_stop', _e)
        try:
            await ECOSYSTEM_RUNTIME.stop()
        except Exception as _e:
            _obs_note('main:473', _e)
        try:
            await PROVIDER_LIBRARY.stop()
        except Exception as _e:
            _obs_note('main:477', _e)
        try:
            await QUANTDATA.stop()
        except Exception as _e:
            _obs_note('main:quantdata_stop', _e)
        try:
            await TASTYTRADE.stop()
        except Exception as _e:
            _obs_note('main:481', _e)
        PRICE_STREAM.stop()
        OPTION_STREAM.stop()
        RUST_CAUSAL_BRIDGE.stop()
        RUST_CAUSAL_INGRESS.stop()
        for t in _shutdown_tasks:
            if t is None:
                continue
            try:
                await t
            except asyncio.CancelledError:
                _obs_expected('main:shutdown_task_cancelled')
                continue
            except Exception as _e:
                _obs_note('main:shutdown_task', _e)


_BIND_HOST = os.getenv("ITM_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
net_guard.assert_safe_bind(_BIND_HOST)
app = FastAPI(title=APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None)

# El bundle pesa ~90 KB y el trace ~100 KB, y se piden cada pocos segundos. En local
# eso no se nota; contra un VPS son cientos de KB por minuto atravesando la red y es
# la mitad de la lentitud que se percibe al cargar. Estos payloads son JSON denso y
# repetitivo: comprimen alrededor de un 85%. El umbral deja pasar sin comprimir lo
# pequeño (el tick son 130 bytes), donde comprimir costaría más de lo que ahorra.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

# v1.27.1 · Si el bind no es loopback, exigir X-ITM-Token en todas las rutas
# salvo /health. En uso local (el caso normal) esto no añade middleware alguno.
net_guard.install(app, _BIND_HOST)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    terminal_metrics.inc("http_requests")
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=(), xr-spatial-tracking=(self)"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self' ws: wss:; media-src 'self' blob:; font-src 'self' data:;"
    )
    return response


@app.exception_handler(Exception)
async def local_exception_handler(request: Request, exc: Exception):
    incident_id = uuid.uuid4().hex[:12]
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as f:
            f.write("\n\n=== UNHANDLED ERROR ===\n")
            f.write(f"Path: {request.url.path}\n")
            f.write(f"Incident: {incident_id}\n")
            f.write(traceback.format_exc())
    except Exception as _e:
        _obs_note('main:522', _e)
    if request.url.path.startswith('/api/'):
        return JSONResponse({"ok":False,"detail":"Error interno","incident_id":incident_id}, status_code=500)
    return HTMLResponse(
        "<html><body style=\"font-family:Segoe UI;background:#0b0d10;color:#fff;padding:36px\">"
        "<h1>ITM QUANT MULTI ASSET</h1><h2>Error interno del servidor</h2>"
        f"<p>La versión {APP_VERSION} guardó el detalle técnico en la memoria persistente de Auditor.</p>"
        "<p>Cierra esta ventana, ejecuta <b>CERRAR_WEB.bat</b> y luego <b>INICIAR_WEB.bat</b>.</p>"
        "</body></html>", status_code=500)


@app.get("/", response_class=HTMLResponse)
async def terminal(request: Request):
    """Terminal de analista: sólo el resultado de los análisis del motor."""
    return templates.TemplateResponse(request=request, name="terminal.html",
                                      context={"app_name": APP_NAME, "version": APP_VERSION})


@app.get("/legacy", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard anterior. Se conserva para comparar y para diagnóstico interno."""
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"app_name": APP_NAME, "app_version": APP_VERSION})


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Minimal local login page. The master token is POSTed, never placed in the URL."""
    return HTMLResponse("""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>ITM QUANT · Acceso</title><style>body{margin:0;background:#070b12;color:#eaf7ff;font-family:system-ui;display:grid;place-items:center;min-height:100vh}.card{width:min(460px,88vw);padding:28px;border:1px solid #24435a;border-radius:18px;background:#0c1420;box-shadow:0 18px 60px #0008}h1{margin:0 0 8px}p{color:#9fb4c7}input,button{box-sizing:border-box;width:100%;padding:13px 14px;border-radius:10px;border:1px solid #31536b;background:#07111b;color:#fff;font-size:16px}button{margin-top:12px;background:#123a55;cursor:pointer;font-weight:800}.err{min-height:22px;color:#ff9dac;margin-top:10px}</style></head><body><div class='card'><h1>ITM QUANT</h1><p>MULTI ASSET · acceso protegido</p><input id='token' type='password' autocomplete='current-password' placeholder='ITM_ACCESS_TOKEN'><button id='go'>ENTRAR</button><div id='err' class='err'></div></div><script>async function go(){const t=document.getElementById('token').value;const e=document.getElementById('err');e.textContent='';try{const r=await fetch('/auth/session',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token:t})});if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||d.error||'Acceso denegado')}location.replace('/')}catch(x){e.textContent=x.message}}document.getElementById('go').onclick=go;document.getElementById('token').addEventListener('keydown',e=>{if(e.key==='Enter')go()});</script></body></html>""", headers={"Cache-Control":"no-store"})

@app.post("/auth/session")
async def auth_session(request: Request):
    ip=net_guard.client_ip(request.scope)
    if net_guard.is_rate_limited(ip):
        return JSONResponse({"ok":False,"detail":"demasiados intentos"},status_code=429,headers={"Cache-Control":"no-store"})
    try:data=await request.json()
    except Exception:data={}
    token=str((data or {}).get("token") or "")
    if not net_guard.token_matches(token):
        net_guard.record_failure(ip)
        return JSONResponse({"ok":False,"detail":"token invalido"},status_code=401,headers={"Cache-Control":"no-store"})
    net_guard.clear_failures(ip)
    response=JSONResponse({"ok":True,"session":"created"},headers={"Cache-Control":"no-store"})
    response.set_cookie(net_guard.COOKIE_NAME,net_guard.issue_session(),max_age=net_guard.session_ttl_seconds(),httponly=True,samesite="strict",secure=net_guard.session_cookie_secure(request.scope),path="/")
    return response

@app.post("/logout")
async def logout(request: Request):
    cred=request.cookies.get(net_guard.COOKIE_NAME)
    revoked=net_guard.revoke_session(cred) if cred else False
    response=JSONResponse({"ok":True,"revoked":bool(revoked)})
    response.delete_cookie(net_guard.COOKIE_NAME,path="/")
    return response

@app.post("/logout-all")
async def logout_all():
    epoch=net_guard.revoke_all_sessions()
    response=JSONResponse({"ok":True,"revoked_all":True,"session_epoch":int(epoch)})
    response.delete_cookie(net_guard.COOKIE_NAME,path="/")
    return response

@app.get("/account")
async def old_account():
    return RedirectResponse("/", status_code=303)


def _stale_read(detail: str, **extra):
    """Return an expected stale-read guard as HTTP 200.

    Symbol/epoch mismatches are normal during an atomic asset switch. They are not
    transport failures and must not flood DevTools with 409 errors. Callers discard
    payloads carrying ``stale=True``. Write conflicts continue to use 409.
    """
    _sym, _epoch = STATE.active()
    payload={"ok":False,"stale":True,"detail":str(detail),
             "active_symbol":_sym,"symbol_epoch":int(_epoch)}
    payload.update(extra)
    return JSONResponse(_jsonable(payload), status_code=200)


@app.get("/api/assets/catalog")
async def api_assets_catalog():
    """Return the full searchable provider ETF universe outside the hot state payload."""
    rows=selectable_assets(include_partial=True)
    return JSONResponse(_jsonable({"assets":rows,"count":len(rows),"active":STATE.symbol,"version":APP_VERSION}))


@app.get("/api/state")
async def api_state():
    _hsym, _hepoch = STATE.active()  # v1.27.21: coherent symbol/epoch for this response
    # Attach transport/runtime health from the application layer without forcing the
    # quantitative service to own provider transports. All values are local snapshots;
    # this endpoint never performs a blocking network probe.
    payload = STATE.public_state()
    if not payload.get("ready") and not STATE.replay_context.is_replay:
        cached=READY_STORE.load_live(STATE.symbol)
        if isinstance(cached,dict) and isinstance(cached.get("state"),dict):
            payload=dict(cached["state"])
            payload["ready"]=True;payload["cached_boot"]=True;payload["active_symbol"]=STATE.symbol;payload["symbol_epoch"]=int(STATE.symbol_epoch)
            payload["assets"]=core_selectable_assets();payload["asset"]=asset_info(STATE.symbol)
            payload["always_on_ready"]={"source":cached.get("source"),"generated_at_utc":cached.get("generated_at_utc"),"session_date":cached.get("session_date"),"authority":"PRESENTATION_READY_CACHE_ONLY"}
    payload["provider_runtime"] = {
        "policy": "NO_FIXED_PROVIDER_RANK · QUALITY_BY_OBSERVATION",
        "alpaca": {"configured": bool(alpaca_data.load_settings()), "first_class": True},
        "tastytrade": {**TASTYTRADE.status(), "first_class": True, "active_provider": bool(TASTYTRADE.configured)},
        "quantdata": QUANTDATA.status(),
        "market_truth": MARKET_TRUTH.snapshot(_hsym),
        "causality": CAUSAL_RUNTIME.status(_hsym),
    }
    if not STATE.replay_context.is_replay:
        try:
            AGGRESSION_DELTA.update_from_fabric(STATE.symbol)
            payload["aggression_delta"] = AGGRESSION_DELTA.snapshot(STATE.symbol,"1m",limit=40)
        except Exception:
            payload.setdefault("aggression_delta",{"ready":False,"role":"ENTRY_TIMING_CONFIRMATION_ONLY"})
    payload["sophia"]={**SOPHIA.status(),"voice":SOPHIA_VOICE.status()}
    return JSONResponse(_jsonable(payload))


def _current_aggression_payload(timeframe: str = "1m", limit: int = 120) -> dict:
    tf=str(timeframe or "1m").lower()
    if tf not in {f"{m}m" for m in TIMEFRAMES}:tf="1m"
    if STATE.replay_context.is_replay:
        try:
            pkg=STATE._instant_replay_package()
            agg=((pkg or {}).get("state") or {}).get("aggression_delta") if isinstance(pkg,dict) else None
            if isinstance(agg,dict):
                got=((agg.get("by_timeframe") or {}).get(tf))
                if isinstance(got,dict):return got
        except Exception as _e:
            _obs_note('main:602', _e)
        try:
            bundle=STATE._build_replay_bundle()
            if bundle.get("ready"):
                return AGGRESSION_DELTA.build_from_dataframe(STATE.symbol,bundle.get("tape"),tf,limit=limit)
        except Exception as _e:
            _obs_note('main:607', _e)
        return {"ready":False,"symbol":STATE.symbol,"timeframe":tf,"replay":True,"reason":"AGGRESSION_HISTORY_UNAVAILABLE"}
    blocked=_publication_blocked_payload("AGGRESSION_DELTA")
    if blocked is not None:
        blocked["timeframe"]=tf
        return blocked
    AGGRESSION_DELTA.update_from_fabric(STATE.symbol)
    return AGGRESSION_DELTA.snapshot(STATE.symbol,tf,limit=limit)


@app.get("/api/aggression-delta")
async def api_aggression_delta(timeframe: str = "1m", limit: int = 120):
    return JSONResponse(_jsonable(_current_aggression_payload(timeframe,max(20,min(int(limit),500)))))


@app.get("/api/sophia/status")
async def api_sophia_status():
    return JSONResponse(_jsonable({**SOPHIA.status(),"voice":SOPHIA_VOICE.status(),"watches":SOPHIA.watches()}))


@app.get("/api/sophia/watches")
async def api_sophia_watches():
    return JSONResponse({"ok":True,"watches":_jsonable(SOPHIA.watches())})


@app.delete("/api/sophia/watch/{watch_id}")
async def api_sophia_cancel_watch(watch_id: str):
    rule=SOPHIA.cancel_watch(str(watch_id))
    return JSONResponse({"ok":bool(rule),"cancelled":_jsonable(rule.__dict__) if rule else None,"watches":_jsonable(SOPHIA.watches())})


@app.post("/api/sophia/watches/clear")
async def api_sophia_clear_watches():
    n=SOPHIA.clear_watches()
    return JSONResponse({"ok":True,"cancelled":n,"watches":[]})


@app.post("/api/sophia/chat")
async def api_sophia_chat(request: Request):
    try:data=await request.json()
    except Exception:data={}
    message=str((data or {}).get("message") or "").strip()
    if not message:return JSONResponse({"ok":False,"reply":"Escribe o dime que quieres revisar."},status_code=400)
    # Conversational queries may read the complete platform snapshot. This work happens
    # only on user request; the always-on Watch Rules keep using the lightweight HOT digest.
    state=await asyncio.to_thread(STATE.public_state)
    agg=_current_aggression_payload(str((data or {}).get("timeframe") or "1m"),80)
    reply=await SOPHIA.handle_message(message,state,agg)
    watch=(reply or {}).get("watch") if isinstance(reply,dict) else None
    if isinstance(watch,dict) and str(watch.get("kind") or "").upper()=="UNUSUAL_FLOW":
        # Start from the exact canonical option-event cursor present when the user issued
        # the command. Old unusual prints must never fire a newly-created watch.
        seq=int((OPTION_FLOW_FABRIC.scheduler_status(STATE.symbol) or {}).get("last_seq") or 0)
        rule=SOPHIA.set_watch_baseline(str(watch.get("id") or ""),min_fabric_seq=seq)
        if rule is not None:
            reply["watch"]=_jsonable(rule.__dict__)
            reply["watches"]=_jsonable(SOPHIA.watches())
    return JSONResponse(_jsonable(reply))


@app.get("/api/sophia/capabilities")
async def api_sophia_capabilities():
    """Qué sabe hacer Sophia en ESTA instalación, antes de que nadie lo intente.

    El frontend pedía transcripción sin saber si había motor de voz instalado. Sin
    faster-whisper ni whisper.cpp, el backend respondía 503 —correctamente— pero el
    cliente reintentaba, y la consola acumulaba tres errores rojos por cada intento
    de hablar. El fallo no era el 503: era preguntar sin haber comprobado.

    Con esto el micrófono se deshabilita de entrada y se explica por qué, en vez de
    ofrecer un botón que sólo puede fallar.
    """
    st = SOPHIA_VOICE.status() if SOPHIA_VOICE else {}
    stt = bool((st.get("stt") or {}).get("configured"))
    tts = bool((st.get("tts") or {}).get("configured"))
    return JSONResponse({
        "text": True,
        "stt": stt,
        "tts": tts,
        "detail": st,
        "stt_reason": None if stt else (
            "No hay motor de voz a texto instalado. Sophia responde por escrito. "
            "Para dictarle, instale faster-whisper (pip install faster-whisper) o "
            "declare la ruta de whisper.cpp en ITM_WHISPER_CLI."),
        "tts_reason": None if tts else (
            "No hay motor de texto a voz disponible. En Windows se usa SAPI, que viene "
            "con el sistema; en Linux hay que declarar la ruta de Piper en ITM_PIPER_BIN."),
        "billing": "LOCAL_NO_CREDITS",
    })


@app.post("/api/sophia/voice/transcribe")
async def api_sophia_transcribe(file: UploadFile = File(...)):
    # Puerta de capacidad antes de tocar el audio: si no hay STT, el error es 501
    # (no implementado en esta instalación), no 503 (servicio caído). Un 503 invita
    # a reintentar; un 501 dice que reintentar no va a servir de nada.
    st = SOPHIA_VOICE.status() if SOPHIA_VOICE else {}
    if not (st.get("stt") or {}).get("configured"):
        raise HTTPException(status_code=501, detail=(
            "Voz a texto no instalada en esta instalación. Sophia sigue respondiendo "
            "por escrito. Instale faster-whisper o configure ITM_WHISPER_CLI."))
    data=await file.read()
    if len(data)>12*1024*1024:raise HTTPException(status_code=413,detail="Audio demasiado grande")
    if not data:raise HTTPException(status_code=400,detail="Audio vacío")
    suffix=Path(file.filename or "speech.webm").suffix or ".webm"
    try:text=await asyncio.to_thread(SOPHIA_VOICE.transcribe_bytes,data,suffix)
    except Exception as exc:raise HTTPException(status_code=503,detail=str(exc)[:240])
    return JSONResponse({"ok":True,"text":text,"billing":"LOCAL_NO_CREDITS"})


@app.post("/api/sophia/voice/synthesize")
async def api_sophia_synthesize(request: Request):
    try:data=await request.json()
    except Exception:data={}
    text=str((data or {}).get("text") or "").strip()[:900]
    if not text:raise HTTPException(status_code=400,detail="Texto vacio")
    _st = SOPHIA_VOICE.status() if SOPHIA_VOICE else {}
    if not (_st.get("tts") or {}).get("configured"):
        raise HTTPException(status_code=501, detail=(
            "Texto a voz no disponible en esta instalación. Sophia responde por escrito."))
    try:audio=await asyncio.to_thread(SOPHIA_VOICE.synthesize_wav,text)
    except Exception as exc:raise HTTPException(status_code=503,detail=str(exc)[:240])
    return Response(content=audio,media_type="audio/wav",headers={"Cache-Control":"no-store","X-Sophia-Voice":"LOCAL_NO_CREDITS"})


@app.get("/api/tools/registry")
async def api_tool_registry():
    return JSONResponse(_jsonable(registry_payload()))


@app.get("/api/tools/{tool_id}/digest")
async def api_tool_digest(tool_id: str):
    tid=str(tool_id or "").lower().strip()
    if tid not in TOOL_REGISTRY: raise HTTPException(status_code=404,detail="Tool no registrado")
    state=_tool_hot_state();digest=build_tool_digest(tid,state)
    return JSONResponse(_jsonable({"ready":True,"tool":tid,"digest":digest,"signature":digest_signature(digest),"symbol_epoch":state.get("symbol_epoch")}))


@app.get("/api/alerts/structured")
async def api_structured_alerts(limit: int = 50):
    return JSONResponse(_jsonable({"events":STRUCTURED_ALERTS.recent(limit),"authority":"CONTEXT_ONLY"}))


@app.get("/api/analytics/conditional-outcomes")
async def api_conditional_outcomes():
    with STATE.lock:
        report=conditional_outcome_report(STATE.scanner or {},STATE.calibration or {},STATE.regime_context or {})
    return JSONResponse(_jsonable(report))


def _gex_cell_payload(strike: float, expiration: str | None = None) -> dict:
    with STATE.lock:
        gd=STATE.gamma_delta or {};sym=str(STATE.symbol).upper();epoch=int(STATE.symbol_epoch)
        e=gd.get("enriched",pd.DataFrame()).copy() if isinstance(gd,dict) else pd.DataFrame()
        spot=gd.get("spot") if isinstance(gd,dict) else None
    if e.empty:return {"ready":False,"reason":"NO_CHAIN","symbol":sym,"symbol_epoch":epoch}
    e["timestamp"]=pd.to_datetime(e.get("timestamp"),errors="coerce");e["strike"]=numeric_column(e,"strike",float("nan"));e=e.dropna(subset=["timestamp","strike"])
    if e.empty:return {"ready":False,"reason":"NO_CHAIN","symbol":sym,"symbol_epoch":epoch}
    e=e[e["timestamp"]==e["timestamp"].max()].copy();e=e[(e["strike"]-float(strike)).abs()<1e-8]
    if expiration and "expiration_date" in e.columns:e=e[e["expiration_date"].astype(str)==str(expiration)]
    if e.empty:return {"ready":False,"reason":"CELL_NOT_FOUND","symbol":sym,"strike":strike,"expiration":expiration}
    rows=[]
    for _,r in e.sort_values([c for c in ("expiration_date","option_type") if c in e.columns]).iterrows():
        rows.append({"expiration":str(r.get("expiration_date") or r.get("expiration") or ""),"option_type":str(r.get("option_type") or ""),"strike":float(r.get("strike")),
                     "open_interest":_jsonable(r.get("open_interest")),"volume":_jsonable(r.get("volume",r.get("option_volume"))),
                     "gamma":_jsonable(r.get("gamma")),"delta":_jsonable(r.get("delta")),"gex":_jsonable(r.get("signed_gex_proxy")),
                     "dex":_jsonable(r.get("option_delta_exposure_info")),"iv":_jsonable(r.get("implied_volatility",r.get("iv"))),
                     "bid":_jsonable(r.get("bid_price",r.get("bid"))),"ask":_jsonable(r.get("ask_price",r.get("ask")))})
    total_gex=float(numeric_column(e,"signed_gex_proxy",float("nan")).sum()) if "signed_gex_proxy" in e.columns else None
    total_dex=float(numeric_column(e,"option_delta_exposure_info",float("nan")).sum()) if "option_delta_exposure_info" in e.columns else None
    total_oi=float(numeric_column(e,"open_interest",0).sum()) if "open_interest" in e.columns else None
    total_volume=float(pd.to_numeric(e.get("volume",e.get("option_volume")),errors="coerce").fillna(0).sum()) if ("volume" in e.columns or "option_volume" in e.columns) else None
    iv_series=pd.to_numeric(e.get("implied_volatility",e.get("iv")),errors="coerce") if ("implied_volatility" in e.columns or "iv" in e.columns) else pd.Series(dtype=float)
    avg_iv=float(iv_series.dropna().mean()) if not iv_series.dropna().empty else None
    return {"ready":True,"symbol":sym,"symbol_epoch":epoch,"spot":spot,"strike":float(strike),"expiration":expiration,
            "total_gex":total_gex,"total_dex":total_dex,"total_open_interest":total_oi,"total_volume":total_volume,
            "avg_iv":avg_iv,"contract_count":len(rows),"contracts":rows,"authority":"DRILLDOWN_ONLY"}


@app.get("/api/analytics/gex-cell")
async def api_gex_cell(strike: float, expiration: str | None = None):
    return JSONResponse(_jsonable(await asyncio.to_thread(_gex_cell_payload,strike,expiration)))


@app.websocket("/ws/tools/{tool_id}")
async def ws_tool_runtime(websocket: WebSocket, tool_id: str):
    tid=str(tool_id or "").lower().strip()
    if tid not in TOOL_REGISTRY:
        await websocket.close(code=1008);return
    await websocket.accept();last_sig=None;seq=0
    try:
        while True:
            state=_tool_hot_state();digest=build_tool_digest(tid,state);sig=digest_signature(digest)
            if sig!=last_sig:
                seq+=1;last_sig=sig
                await websocket.send_json(_jsonable({"type":"TOOL_UPDATE","tool_id":tid,"seq":seq,"signature":sig,"symbol":state.get("active_symbol"),"symbol_epoch":state.get("symbol_epoch"),"digest":digest}))
            await asyncio.sleep(0.20)
    except (WebSocketDisconnect,asyncio.CancelledError):
        return
    except Exception as exc:
        _obs_note('main:ws_tool_runtime',exc)


@app.get("/api/charts")
async def api_charts(view: str = "all", chain_metric: str = "Q-Score", surface_metric: str = "Gamma",
                     net_drift_scope: str = "Todas exp.", exposure_metric: str = "Gamma",
                     positioning_metric: str = "Delta-adjusted", volume_metric: str = "Calls vs Puts",
                     gex_matrix_metric: str = "GEX", gex_matrix_baseline: str = "OPEN", gex_matrix_threshold: str = "AUTO",
                     gamma_migration_mode: str = "DIFFERENCE",
                     trace_landscape_lens: str = "Gamma", trace_landscape_scale: str = "session",
                     surface_render_style: str = "Superficie", surface_option_view: str = "Net",
                     surface_slice_metric: str = "Gamma", surface_slice_render: str = "Barras",
                     expected_symbol: str | None = None, expected_epoch: int | None = None, prefer_ready_cache: bool = False):
    """Lean analytical chart endpoint.

    TRACE and the primary 3D Surface have dedicated NextGen endpoints and are
    intentionally not rebuilt here. This endpoint serves only companion charts.
    """
    allowed_chain = {"Q-Score", "Open Interest", "Volumen", "Gamma", "GEX", "Delta", "DEX", "Vanna", "Charm", "Speed", "Actividad inusual"}
    allowed_surface = {"Gamma", "GEX", "Delta", "DEX", "Vanna", "Charm", "Speed", "Open Interest", "Net OI", "Volumen", "Volumen Neto", "Score cuantitativo", "Actividad inusual"}
    allowed_surface_slice_render = {"Barras", "Líneas", "Puntos", "Picos", "Olas"}
    allowed_net_drift_scope = {"Todas exp.", "0DTE"}
    allowed_exposure_metric = {"Gamma", "Delta", "Net OI", "Volumen Neto"}
    allowed_positioning_metric = {"Delta-adjusted", "OI Call-Put", "Gamma-adjusted"}
    allowed_volume_metric = {"Calls vs Puts", "Volumen Total", "Vol/OI"}
    allowed_gex_matrix_metric = {"GEX", "ΔGEX"}
    allowed_gex_matrix_baseline = {"OPEN", "PREVIOUS"}
    allowed_gamma_migration_mode = {"VALUE", "DIFFERENCE"}
    allowed_landscape_lens = {"Gamma","Delta","Charm"}
    allowed_landscape_scale = {"column","session","anchor"}
    allowed_surface_style = {"Superficie","Barras","Líneas","Puntos","Picos","Olas"}
    allowed_surface_view = {"Net","Calls","Puts"}
    if chain_metric not in allowed_chain: chain_metric = "Q-Score"
    if surface_metric not in allowed_surface: surface_metric = "Gamma"
    if surface_slice_metric not in allowed_surface: surface_slice_metric = surface_metric
    if surface_slice_render not in allowed_surface_slice_render: surface_slice_render = "Barras"
    if net_drift_scope not in allowed_net_drift_scope: net_drift_scope = "Todas exp."
    if exposure_metric not in allowed_exposure_metric: exposure_metric = "Gamma"
    if positioning_metric not in allowed_positioning_metric: positioning_metric = "Delta-adjusted"
    if volume_metric not in allowed_volume_metric: volume_metric = "Calls vs Puts"
    if gex_matrix_metric not in allowed_gex_matrix_metric: gex_matrix_metric = "GEX"
    gex_matrix_baseline = str(gex_matrix_baseline or "OPEN").upper()
    if gex_matrix_baseline not in allowed_gex_matrix_baseline: gex_matrix_baseline = "OPEN"
    gamma_migration_mode=str(gamma_migration_mode or "DIFFERENCE").upper()
    if gamma_migration_mode not in allowed_gamma_migration_mode: gamma_migration_mode="DIFFERENCE"
    _thr = str(gex_matrix_threshold or "AUTO").upper()
    if _thr != "AUTO":
        try: gex_matrix_threshold = str(max(0.0, float(gex_matrix_threshold)))
        except Exception: gex_matrix_threshold = "AUTO"
    if trace_landscape_lens not in allowed_landscape_lens: trace_landscape_lens = "Gamma"
    if trace_landscape_scale not in allowed_landscape_scale: trace_landscape_scale = "session"
    if surface_render_style not in allowed_surface_style: surface_render_style = "Superficie"
    if surface_option_view not in allowed_surface_view: surface_option_view = "Net"
    if expected_symbol and str(expected_symbol).upper()!=str(STATE.symbol).upper():
        return _stale_read("STALE_SYMBOL_REQUEST", expected_symbol=str(expected_symbol).upper(), active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    if expected_epoch is not None and int(expected_epoch)!=int(STATE.symbol_epoch):
        return _stale_read("STALE_SYMBOL_EPOCH", expected_epoch=int(expected_epoch), active_epoch=STATE.symbol_epoch, active_symbol=STATE.symbol)
    # Charts are observational/context surfaces. A freshness gate may make LIVE
    # actionability fail-closed, but it must not erase the last valid structure from
    # the screen. The gate is attached to the response so the browser can label it
    # STRUCTURAL/STALE without presenting it as actionable.
    chart_publication_gate=_publication_gate_snapshot()
    payload=None
    if prefer_ready_cache and not STATE.replay_context.is_replay:
        cached_pkg=READY_STORE.load_live(STATE.symbol)
        if isinstance(cached_pkg,dict) and isinstance(cached_pkg.get("charts"),dict):
            payload=dict(cached_pkg["charts"])
            payload["symbol"]=STATE.symbol;payload["symbol_epoch"]=int(STATE.symbol_epoch);payload["ready_cache"]=True
            payload["ready_cache_generated_at_utc"]=cached_pkg.get("generated_at_utc")
    if payload is None:
        payload=await asyncio.to_thread(STATE.charts,
            chain_metric=chain_metric, surface_metric=surface_metric,
            net_drift_scope=net_drift_scope, exposure_metric=exposure_metric,
            positioning_metric=positioning_metric, volume_metric=volume_metric,
            gex_matrix_metric=gex_matrix_metric, gex_matrix_baseline=gex_matrix_baseline, gex_matrix_threshold=gex_matrix_threshold, gamma_migration_mode=gamma_migration_mode,
            trace_landscape_lens=trace_landscape_lens, trace_landscape_scale=trace_landscape_scale,
            surface_render_style=surface_render_style, surface_option_view=surface_option_view,
            surface_slice_metric=surface_slice_metric, surface_slice_render=surface_slice_render, view=view)
    payload["publication_gate"]=chart_publication_gate
    payload["context_only"]=chart_publication_gate.get("publicar_permitido") is not True
    if expected_symbol and str(payload.get("symbol") or "").upper()!=str(expected_symbol).upper():
        return _stale_read("STALE_CHART_RESULT", expected_symbol=str(expected_symbol).upper(), result_symbol=payload.get("symbol"), active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    if expected_epoch is not None and int(payload.get("symbol_epoch",-1))!=int(expected_epoch):
        return _stale_read("STALE_CHART_EPOCH", expected_epoch=int(expected_epoch), result_epoch=payload.get("symbol_epoch"), active_epoch=STATE.symbol_epoch, active_symbol=STATE.symbol)
    view=str(view or "all").strip().lower()
    view_keys={
        "command":{"trace_orderflow"},"trace":{"trace_orderflow","flow_pro"},
        "scanner":{"scanner"},"structure":{"chain"},"flow":{"flow","flow_pro"},"netdrift":{"net_drift","net_drift_summary"},
        "exposure":{"exposure_strike","exposure_summary"},"gexmatrix":{"gex_matrix","gamma_migration","gamma_migration_digest"},"volatility":{"volatility","skew","skew_summary"},
        "positioning":{"net_positioning","volume","volume_summary"},"macro":{"macro"},"prints":{"large_prints"},
        "surface":{"trace_landscape","surface_slice","surface_main_slice"},
        "equity_hub":{"equity_hub_gamma_model","equity_hub_monte_carlo","equity_hub_monte_carlo_summary"},
    }
    if view!="all" and view in view_keys:
        base={k:payload.get(k) for k in ("symbol","symbol_epoch","chart_state","replay","publication_gate","context_only") if k in payload}
        for k in view_keys[view]:
            if k in payload: base[k]=payload[k]
        base["view"]=view;base["lean_payload"]=True
        payload=base
    return JSONResponse(_jsonable(payload))


@app.get("/api/charts/surface-slice")
async def api_surface_slice_chart(metric: str = "Gamma", option_view: str = "Net", render_style: str = "Barras",
                                  expected_symbol: str | None = None, expected_epoch: int | None = None):
    allowed_metric={"Gamma","GEX","Delta","DEX","Vanna","Charm","Speed","Open Interest","Net OI","Volumen","Volumen Neto","Score cuantitativo","Actividad inusual"}
    allowed_view={"Net","Calls","Puts"}
    allowed_render={"Barras","Líneas","Puntos","Picos","Olas"}
    if metric not in allowed_metric: metric="Gamma"
    if option_view not in allowed_view: option_view="Net"
    if render_style not in allowed_render: render_style="Barras"
    if expected_symbol and str(expected_symbol).upper()!=str(STATE.symbol).upper():
        return _stale_read("STALE_SYMBOL_REQUEST", expected_symbol=str(expected_symbol).upper(), active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    if expected_epoch is not None and int(expected_epoch)!=int(STATE.symbol_epoch):
        return _stale_read("STALE_SYMBOL_EPOCH", expected_epoch=int(expected_epoch), active_epoch=STATE.symbol_epoch, active_symbol=STATE.symbol)
    blocked=_publication_blocked_payload("SURFACE_SLICE")
    if blocked is not None:
        return JSONResponse(_jsonable(blocked))
    payload=await asyncio.to_thread(STATE.surface_slice_chart,metric,option_view,render_style)
    if expected_symbol and str(payload.get("symbol") or "").upper()!=str(expected_symbol).upper():
        return _stale_read("STALE_CHART_RESULT", expected_symbol=str(expected_symbol).upper(), result_symbol=payload.get("symbol"), active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    if expected_epoch is not None and int(payload.get("symbol_epoch",-1))!=int(expected_epoch):
        return _stale_read("STALE_CHART_EPOCH", expected_epoch=int(expected_epoch), result_epoch=payload.get("symbol_epoch"), active_epoch=STATE.symbol_epoch, active_symbol=STATE.symbol)
    return JSONResponse(_jsonable(payload))


@app.post("/api/expiry/select")
async def api_expiry_select(window: str = "AUTO"):
    try:
        return JSONResponse(_jsonable(await asyncio.to_thread(STATE.set_expiry_window, window)))
    except ValueError as e:
        return JSONResponse(_jsonable({"ok":False,"detail":str(e),"window":STATE.expiry_window}),status_code=409)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/always-on/status")
async def api_always_on_status():
    return JSONResponse(_jsonable({"ready_store":READY_STORE.status(STATE.symbol),"engine_process":"RUNNING","ui_dependency":"NONE","note":"Para continuar con la PC apagada, este mismo servidor debe ejecutarse en un VPS/host 24/7."}))


@app.get("/api/always-on/session")
async def api_always_on_session(date: str):
    try:
        pkg=READY_STORE.load_session(STATE.symbol,date)
    except Exception as exc:
        raise HTTPException(status_code=400,detail=str(exc))
    if not pkg:
        return JSONResponse({"ready":False,"date":date,"symbol":STATE.symbol,"reason":"SESSION_NOT_PRECOMPUTED_YET"})
    return JSONResponse(_jsonable({"ready":True,"date":date,"symbol":STATE.symbol,"generated_at_utc":pkg.get("generated_at_utc"),"sealed":pkg.get("sealed"),"source":pkg.get("source")}))


@app.get("/api/assets")
async def api_assets():
    return {"active": STATE.symbol, "symbol_epoch": STATE.symbol_epoch, "assets": selectable_assets(), "ecosystem": asset_info(STATE.symbol).get("ecosystem",{})}


@app.get("/api/assets/ecosystem")
async def api_asset_ecosystem(symbol: str | None = None):
    sym = str(symbol or STATE.symbol).upper().strip()
    return JSONResponse(_jsonable({
        "symbol": sym, "definition": public_summary(sym),
        "runtime": ECOSYSTEM_RUNTIME.status() if sym == str(STATE.symbol).upper() else {},
        "market_components": {
            "related_etfs_equities": {c: PROVIDER_BUS.snapshot(c) for c in public_summary(sym).get("related_etfs_equities", [])},
            "indices": {c: PROVIDER_BUS.snapshot(c) for c in public_summary(sym).get("indices", [])},
            "futures": {c: PROVIDER_BUS.snapshot(c) for c in public_summary(sym).get("futures", [])},
        },
        "feature_fusion": FEATURE_BUS.snapshot(sym),
        "policy": "ETF_INDEX_FUTURE_DERIVATIVES_SEPARATE_THEN_NORMALIZED",
    }))


@app.get("/api/board")
async def api_board():
    return JSONResponse(_jsonable(STATE.board_state()))


@app.post("/api/board/refresh")
async def api_board_refresh():
    return JSONResponse(_jsonable(await asyncio.to_thread(STATE.refresh_board_next)))


@app.post("/api/asset/select")
async def api_asset_select(symbol: str):
    old_symbol=STATE.symbol
    target=str(symbol or "").upper().strip()
    try:
        cfg=asset_info(target)
        provider=str(cfg.get("market_provider") or cfg.get("quant_provider") or "ALPACA").upper()
        # Prepare the owning market transport BEFORE publishing the new STATE symbol.
        # This preserves the atomic switch invariant: no consumer can observe the new
        # instrument while its live-price transport still points at the previous symbol.
        if provider=="TASTYTRADE":
            await TASTYTRADE.select_asset(target)
        else:
            PRICE_STREAM.set_symbol(target)
        result=STATE.begin_asset_switch(target)
        if not result.get("ok"):
            try:
                old_cfg=asset_info(old_symbol);old_provider=str(old_cfg.get("market_provider") or old_cfg.get("quant_provider") or "ALPACA").upper()
                if old_provider=="TASTYTRADE":await TASTYTRADE.select_asset(old_symbol)
                else:PRICE_STREAM.set_symbol(old_symbol)
            except Exception as _e:
                _obs_note('main:910', _e)
            return JSONResponse(_jsonable(result),status_code=409)
        epoch=int(result.get("symbol_epoch",STATE.symbol_epoch))
        # Give tastytrade/DXLink a head start for OWN-chain failover even when Alpaca owns
        # the selected ETF. It stays non-blocking; the Quant worker can use it only after
        # real contracts are present in memory.
        if provider!="TASTYTRADE" and TASTYTRADE.configured:
            _schedule_tastytrade_prepare(target, epoch)
            await asyncio.sleep(0)
        _schedule_trace_bootstrap(target, epoch)
        if result.get("progressive"):
            _schedule_asset_warmup(target, epoch)
        ECOSYSTEM_RUNTIME.select_asset(target)  # internal confluence only; never a visible blended instrument
        # v1.44.0 · El cambio de activo invalida TODO lo que es por símbolo, no sólo
        # las cachés del proveedor. Los muros, la procedencia y el Last Known Good
        # son estado por símbolo: dejarlos vivos haría que el Call Wall de DIA
        # apareciera un instante sobre el gráfico de AAPL, que es exactamente la
        # clase de mezcla que nadie detecta mirando.
        _invalidate_symbol_state(old_symbol, target)
        await QUANTDATA.select_asset(target)  # background options intelligence; never blocks asset switch
        await QUANTDATA_INTELLIGENCE.select_asset(target)  # páginas integradas del proveedor
        return JSONResponse(_jsonable({"ok":True,"result":result,"state":STATE.public_state()}))
    except Exception as e:
        # Restore only the provider that owned the previous instrument.
        try:
            old_cfg=asset_info(old_symbol);old_provider=str(old_cfg.get("market_provider") or old_cfg.get("quant_provider") or "ALPACA").upper()
            if old_provider=="TASTYTRADE":await TASTYTRADE.select_asset(old_symbol)
            else:PRICE_STREAM.set_symbol(old_symbol)
        except Exception as _e:
            _obs_note('main:931', _e)
        return JSONResponse(_jsonable({"ok":False,"detail":str(e),"symbol":target,"kept_symbol":old_symbol,"log":"logs/server.log"}),status_code=409)


def _invalidate_symbol_state(previous: str, target: str) -> dict:
    """Invalida el estado por símbolo al cambiar de activo.

    Se limpian los DOS símbolos: el anterior porque ya no va a mostrarse, y el
    nuevo porque pudo quedar sembrado por una consulta previa con datos más viejos
    que este cambio, y servirlos como si fueran de ahora sería peor que no tener
    nada.

    No falla el cambio de activo si algo aquí falla: la invalidación es higiene,
    no una precondición. Un fallo se anota y se sigue.
    """
    from .core.data_lineage import LINEAGE
    from .core.wall_engine import WALLS
    from .core.data_hub_runtime import HUB_RUNTIME

    report = {"previous": str(previous or "").upper(), "target": str(target or "").upper()}
    for sym in {report["previous"], report["target"]} - {""}:
        for name, fn in (("lineage", LINEAGE.clear_symbol),
                         ("walls", WALLS.clear_symbol),
                         ("last_known_good", lambda s: HUB_RUNTIME.clear_symbol(s))):
            try:
                fn(sym)
            except Exception as exc:
                _obs_note(f"main:invalidate_symbol:{name}", exc, severity="DEGRADED")
                report.setdefault("errors", []).append(f"{name}:{type(exc).__name__}")
    return report


@app.get("/__visual_harness", include_in_schema=False)
async def __visual_harness():
    """Banco visual multi-activo. Herramienta de desarrollo, no ruta de producto.

    Carga los módulos de render REALES y los alimenta con perfiles de cinco activos
    de escalas incomparables. Es la única forma de comprobar que la legibilidad de
    las barras no depende de un ticker cuando el universo se descubre en runtime y
    el entorno de desarrollo sólo tiene credenciales de uno.
    """
    from fastapi.responses import FileResponse, JSONResponse as _J
    p = Path(__file__).resolve().parent.parent / "tools" / "visual_harness.html"
    if not p.is_file():
        return _J({"detail": "banco visual no empaquetado"}, status_code=404)
    return FileResponse(str(p), media_type="text/html")


@app.get("/api/replay/sessions")
async def api_replay_sessions(start: str | None = None, end: str | None = None):
    return JSONResponse(_jsonable(await asyncio.to_thread(STATE.replay_sessions, start, end)))


@app.post("/api/replay/set")
async def api_replay_set(date: str, asof: str | None = None):
    """Set the single global historical context used by every visible module."""
    try:
        return JSONResponse(_jsonable(await asyncio.to_thread(STATE.set_replay, date, asof)))
    except ValueError as e:
        return JSONResponse(_jsonable({"ok":False,"detail":str(e)}), status_code=409)


@app.get("/api/architecture")
async def api_architecture():
    """Los contratos de v1.42, leídos del sistema en marcha.

    Un contrato que sólo vive en la documentación se desincroniza del código en dos
    versiones. Aquí se publican desde los propios módulos: qué unidades existen, quién
    es autoridad de cada métrica, qué modelo valora cada clase de instrumento y cuántas
    veces pasó cada sección por el dispatcher único en este proceso.
    """
    from .core import units_registry, metric_authority, greeks_service, macro_factor_engine
    from .core import oi_semantics, dark_pool_taxonomy, series_metadata
    from .core.quant_errors import degradations

    return JSONResponse({
        "scope": "MULTI_ASSET",
        "version": APP_VERSION,
        "units": units_registry.registry_snapshot(),
        "metric_authority": metric_authority.authority_map(),
        "pricing_dispatch": {
            sym: greeks_service.describe_dispatch(sym)
            for sym in ("DIA", "AAPL", "DJX", "YM", "VIX")
        },
        "dispatcher_usage": greeks_service.usage_stats(),
        "macro_factors": macro_factor_engine.factor_contract(),
        "open_interest": oi_semantics.describe_contract(),
        "dark_pool": {
            "categories": [dark_pool_taxonomy.CONFIRMED_OFF_EXCHANGE,
                           dark_pool_taxonomy.LARGE_PRINT,
                           dark_pool_taxonomy.DERIVED_LIQUIDITY_ZONE],
            "impact_horizons_s": list(dark_pool_taxonomy.IMPACT_HORIZONS_S),
        },
        "series_types": series_metadata.bundle([])["legend"],
        "degradations": degradations(25),
        "doctrine": (
            "Un contrato, un precio, una IV, una Gamma, un multiplicador, una unidad y "
            "una marca de tiempo significan exactamente lo mismo en todo ITM QUANT."
        ),
    })


@app.get("/api/backtest/calendar")
async def api_backtest_calendar(start: str | None = None, end: str | None = None):
    """Qué se puede reproducir de cada día de mercado, y con qué fidelidad.

    No es una lista de fechas: es una lista de NIVELES. Un backtest de estructura de
    opciones sobre un día del que sólo hay precio no mide lo que dice medir, así que
    la diferencia viaja marcada en cada día en lugar de quedar implícita.
    """
    from .core.backtest_calendar import build_calendar
    ref = _dt.date.today()
    try:
        b = _dt.date.fromisoformat(str(end)[:10]) if end else ref
        a = _dt.date.fromisoformat(str(start)[:10]) if start else (b - _dt.timedelta(days=45))
    except Exception:
        return JSONResponse({"ready": False, "reason": "FECHAS INVÁLIDAS", "days": []}, status_code=400)

    sym = STATE.symbol
    archived = set(await asyncio.to_thread(replay_available_sessions, alpaca_data.DATA_DIR, sym))
    try:
        archived |= set(READY_STORE.available_sessions(sym, 10000))
    except Exception as exc:
        _obs_note("main:backtest_calendar_ready", exc)
    cached = await asyncio.to_thread(_cached_price_days, sym)
    cal = await asyncio.to_thread(
        build_calendar, alpaca_data.DATA_DIR, sym, a, b,
        archived=archived, cached_price=cached,
        provider_configured=bool(alpaca_data.load_settings()), today=ref)
    return JSONResponse(_jsonable(cal))


def _cached_price_days(symbol: str) -> set:
    """Días cuyas barras históricas ya están en caché local."""
    out: set = set()
    try:
        base = routed_dir(alpaca_data.DATA_DIR, "sessions")
        sym = str(symbol).lower()
        for p in base.glob(f"*{sym}*bars*.csv"):
            for token in p.stem.replace("-", "_").split("_"):
                if len(token) == 8 and token.isdigit():
                    out.add(f"{token[:4]}-{token[4:6]}-{token[6:]}")
            for token in p.stem.split("_"):
                if len(token) == 10 and token[4] == "-" and token[7] == "-":
                    out.add(token)
    except Exception as exc:
        _obs_note("main:cached_price_days", exc)
    return out


@app.post("/api/backtest/hydrate")
async def api_backtest_hydrate(date: str, timeframe: str = "1m"):
    """Descarga las barras históricas de un día para poder reproducirlo.

    Trae el recorrido del PRECIO. La cadena de opciones de una fecha pasada no se
    puede recuperar de un endpoint de snapshot —los snapshots describen el presente—
    así que este día reproduce precio, no estructura, y así se devuelve etiquetado.
    """
    try:
        day = _dt.date.fromisoformat(str(date)[:10])
    except Exception:
        return JSONResponse({"ok": False, "reason": "FECHA INVÁLIDA"}, status_code=400)
    if day > _dt.date.today():
        return JSONResponse({"ok": False, "reason": "FECHA EN EL FUTURO"}, status_code=400)
    if not alpaca_data.load_settings():
        return JSONResponse({"ok": False, "reason": "ALPACA NO CONFIGURADO"}, status_code=409)
    try:
        bars = await asyncio.to_thread(
            alpaca_data.fetch_stock_session_bars, None, STATE.symbol,
            str(timeframe or "1m"), day, "rth")
    except Exception as exc:
        _obs_note("main:backtest_hydrate", exc, severity="DEGRADED")
        return JSONResponse({"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:180]}, status_code=502)
    n = 0 if bars is None or getattr(bars, "empty", True) else int(len(bars))
    return JSONResponse(_jsonable({
        "ok": n > 0, "symbol": STATE.symbol, "date": day.isoformat(), "bars": n,
        "level": "PRECIO" if n else "NO_DISPONIBLE",
        "replays_structure": False,
        "reason": None if n else "El proveedor no devolvió barras para esa fecha.",
        "note": ("Reproduce el recorrido del precio. La cadena de opciones de una fecha "
                 "pasada no se puede recuperar a posteriori, así que GEX, DEX y los muros "
                 "quedarán vacíos con su motivo en ese día."),
    }))


@app.get("/api/replay/clock")
async def api_replay_clock(date: str, step_minutes: float = 1.0):
    """Marcas de tiempo archivadas de una sesión, para reproducirla paso a paso.

    El backtesting de esta terminal no simula: recorre los instantes que el motor
    realmente observó ese día. Cada marca es un `asof` válido para /api/replay/set,
    así que avanzar por ellas reproduce la sesión con la misma causalidad con la que
    ocurrió — sin ver nada que en ese instante todavía no existía.
    """
    try:
        day = _dt.date.fromisoformat(str(date)[:10])
    except Exception:
        return JSONResponse({"ready": False, "reason": "FECHA INVÁLIDA"}, status_code=400)
    step = max(0.25, min(float(step_minutes or 1.0), 60.0))
    clock = await asyncio.to_thread(replay_session_clock, alpaca_data.DATA_DIR, STATE.symbol, day, step)
    payload = dict(clock or {})
    payload["symbol"] = STATE.symbol
    payload["date"] = day.isoformat()
    payload["step_minutes"] = step
    return JSONResponse(_jsonable(payload))


@app.post("/api/replay/live")
async def api_replay_live():
    return JSONResponse(_jsonable(await asyncio.to_thread(STATE.exit_replay)))


@app.get("/api/trace/dates")
async def api_trace_dates():
    all_dates=STATE.available_trace_dates()
    ready_dates=READY_STORE.available_sessions(STATE.symbol,370)
    return {"dates":all_dates,"ready_dates":ready_dates,"symbol":STATE.symbol,"policy":"GLOBAL_DATE_CONTEXT; RAW_ARCHIVE_CAUSAL_FIRST; READY_PACKAGE_MIGRATION_FALLBACK"}


@app.get("/api/tables")
async def api_tables():
    # Tables are context, not an execution permission. Preserve visibility while the
    # publication gate separately blocks actionable LIVE output.
    payload=dict(STATE.tables() or {})
    gate=_publication_gate_snapshot()
    payload["publication_gate"]=gate
    payload["context_only"]=gate.get("publicar_permitido") is not True
    return JSONResponse(_jsonable(payload))


@app.post("/api/refresh")
async def api_refresh():
    try:
        await asyncio.to_thread(STATE.refresh, True)
        return JSONResponse(_jsonable({"ok": True, "state": STATE.public_state()}))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))



@app.post("/api/premarket/analyze")
async def api_premarket_analyze():
    try:
        report = await asyncio.to_thread(STATE.analyze_premarket, True)
        return JSONResponse(_jsonable({"ok": True, "premarket_analysis": report}))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.post("/api/premarket/freeze")
async def api_freeze():
    try:
        return JSONResponse(_jsonable({"ok": True, "premarket": STATE.force_freeze_premarket()}))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))



@app.get("/api/nextgen/trace")
async def api_nextgen_trace(timeframe: str = "1m", tail_minutes: int = 60, window: float | None = None, expected_symbol: str | None = None, expected_epoch: int | None = None):
    timeframe = str(timeframe or "1m").lower()
    if timeframe not in {"1m", "3m", "5m", "15m"}:
        timeframe = "1m"
    try:
        raw_tail = int(tail_minutes)
        # 0 is an explicit TRACE contract: show every retained tick/bar.  It must not
        # be silently rewritten to 15m because the UI exposes TODO as a real choice.
        tail_minutes = 0 if raw_tail == 0 else max(15, min(raw_tail, 390))
    except Exception:
        tail_minutes = 60
    # v1.46.0 · Sin `window` explícito, la banda la resuelve el ACTIVO a partir de
    # su propio precio. El valor por omisión de 12 $ —y su recorte a [3, 30] $—
    # eran razonables para un ETF de ~500 $ y absurdos para una acción de 9 $, que
    # con el mínimo de 3 $ seguía recibiendo una banda del ±31 %.
    # Un `window` pedido a mano sigue respetándose: es una anulación explícita.
    if window is None:
        window = None
    else:
        try:
            window = max(0.05, min(float(window), 5000.0))
        except (TypeError, ValueError):
            window = None
    if expected_symbol and str(expected_symbol).upper()!=str(STATE.symbol).upper():
        return _stale_read("STALE_SYMBOL_REQUEST", expected_symbol=str(expected_symbol).upper(), active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    if expected_epoch is not None and int(expected_epoch)!=int(STATE.symbol_epoch):
        return _stale_read("STALE_SYMBOL_EPOCH", expected_epoch=int(expected_epoch), active_epoch=STATE.symbol_epoch, active_symbol=STATE.symbol)
    # La clave llevaba sólo la revisión del motor, que avanza cada 5-20 s. Entre
    # ciclos se devolvía el payload cacheado con el spot de entonces, así que los
    # perfiles GEX/DEX y el Gamma Flip no se revalorizaban y parecían congelados
    # aunque el precio se estuviera moviendo. build_trace_pulse reprecia contra el
    # spot LIVE en cada construcción: basta con dejarla reconstruir a un ritmo
    # acorde al del panel. En replay no aplica: allí el reloj lo manda el usuario.
    _live_bucket = 0 if STATE.replay_context.is_replay else int(time.time() / TRACE_LIVE_REPRICE_SECONDS)
    _wkey = "auto" if window is None else f"{window:.3f}"
    key=f"trace|{STATE.symbol}|{STATE.symbol_epoch}|{STATE.analytics_revision}|{timeframe}|{tail_minutes}|{_wkey}|{STATE.replay_context.mode}|{_live_bucket}"
    try:
        payload=await asyncio.to_thread(CHART_DATA_CACHE.get_or_build,key,lambda: STATE.nextgen_trace(timeframe=timeframe,tail_minutes=tail_minutes,visual_window=window))
    except Exception as exc:
        terminal_metrics.inc("trace_fail_soft_recovery")
        payload=await asyncio.to_thread(STATE.nextgen_trace_price_only,timeframe,tail_minutes,window,f"TRACE_DEGRADED: {type(exc).__name__}")
        payload["upstream_error"]=f"{type(exc).__name__}: {str(exc)[:240]}"
    # v1.43.0 · El fondo dinámico de TRACE es el Interval Map de Quant Data, no un
    # heat map estático. Se adjunta FUERA de la caché del trace porque su cadencia
    # es la del proveedor, no la del motor: meterlo dentro congelaría el mapa hasta
    # la siguiente revisión analítica.
    payload = _attach_hub_layers(payload)
    return JSONResponse(_jsonable(payload))


def _attach_hub_layers(payload: dict) -> dict:
    """Las cuatro matrices del Interval Map, listas para alternar en el cliente.

    Se envían las cuatro griegas de una vez —no una por petición— porque cambiar
    de GAMMA a DELTA es una decisión de lectura, no de datos: obligar a un viaje de
    red por cada clic haría que el mapa parpadeara justo cuando se está comparando.
    El coste de cuota es cero: son matrices ya descargadas por los carriles.
    """
    if not isinstance(payload, dict):
        return payload
    try:
        from .core import quant_data_hub as HUB
        intel = QUANTDATA_INTELLIGENCE.snapshot()
        symbol = str(payload.get("symbol") or STATE.symbol).upper()
        price = [{"t": str(c.get("t")), "v": c.get("c")}
                 for c in (payload.get("candles") or [])[-900:]
                 if isinstance(c, dict) and c.get("t") is not None and c.get("c") is not None]
        hh = payload.get("heatmap_history") or {}
        maps = {}
        for greek in HUB.INTERVAL_GREEKS:
            maps[greek] = HUB.interval_map(symbol, intel, greek,
                                           engine_heatmap=hh, price=price)
        payload["interval_maps"] = maps
        payload["interval_map"] = maps.get("GAMMA")
        payload["interval_map_greeks"] = list(HUB.INTERVAL_GREEKS)

        # ── CALL WALL / PUT WALL · autoridad ÚNICA ────────────────────────────
        # El Wall Engine resuelve los dos muros UNA vez, sobre el snapshot del Hub,
        # y el resultado sustituye a los niveles que venían del cálculo por sección.
        # TRACE y FLUJO DE ÓRDENES leen los dos esta misma lista, así que ya no
        # pueden discrepar: antes cada sección llamaba a `structural_walls()` con su
        # propio frame y el mismo nombre podía señalar dos strikes distintos.
        payload["walls"] = _resolve_walls(payload, intel, symbol)

        # ── GAMMA MIGRATION · anclada al strike donde se confirmó ─────────────
        payload["gamma_migration"] = _resolve_gamma_migration(payload, intel, symbol)
        # QFLOW sobre el mismo eje temporal que el precio: el nivel estructural y
        # las concentraciones tienen que dibujarse contra las MISMAS velas o la
        # lectura no se puede cerrar.
        from .terminal_api import _qflow
        payload["qflow"] = _qflow(STATE.public_state(), intel)
    except Exception as exc:
        _obs_note("main:attach_hub_layers", exc, severity="DEGRADED")
        payload.setdefault("interval_maps", {})
    return payload


def _resolve_walls(payload: dict, intel: dict, symbol: str) -> dict:
    """Call Wall y Put Wall, resueltos una sola vez para toda la terminal.

    El cálculo propio anterior (`structural_walls`, que ya viajaba dentro de
    `levels`) entra como RESPALDO declarado: sostiene la vista cuando el proveedor
    no sirve exposición por strike de ese activo, y va etiquetado para que nunca se
    confunda con el resultado principal.

    Los niveles `call_wall`/`put_wall` que llegaban de la sección se SUSTITUYEN por
    los del Wall Engine. Dejarlos convivir habría vuelto a permitir dos muros con
    el mismo nombre en la misma pantalla.
    """
    from .core import quant_data_hub as HUB
    from .core import wall_engine as WE

    levels = payload.get("levels") or []
    spot = None
    candles = payload.get("candles") or []
    if candles and isinstance(candles[-1], dict):
        spot = candles[-1].get("c")
    if spot is None:
        spot = (payload.get("profiles") or {}).get("spot")

    previous = {lv.get("kind"): lv.get("price") for lv in levels if isinstance(lv, dict)}
    fallback = {"call_wall": previous.get("call_wall"),
                "put_wall": previous.get("put_wall"),
                "method": "structural-walls-engine"}

    hub = HUB.hub_snapshot(symbol, intel,
                           engine_heatmap=(payload.get("heatmap_history") or {}))
    walls = WE.walls_from_hub(symbol, hub, spot=spot, fallback=fallback)
    # v1.56.0 · CÓMO se calculó cada muro, con los números que lo sostienen.
    # Un muro dibujado es una afirmación sobre el mercado; si su único respaldo
    # es que hay una línea en el gráfico, no hay forma de discutirla ni de
    # detectar que el cálculo empezó a medir otra cosa.
    try:
        payload["wall_audit"] = WE.wall_audit(walls, hub)
    except Exception as exc:
        _obs_note("main:wall_audit", exc, severity="DEGRADED")
        payload["wall_audit"] = {"authority": "ITMQ_WALL_ENGINE",
                                 "note": "la auditoría de muros no se pudo construir"}

    kept = [lv for lv in levels
            if not (isinstance(lv, dict) and lv.get("kind") in ("call_wall", "put_wall"))]
    for lv in walls.get("levels") or []:
        kept.append({"kind": lv["kind"], "name": lv["name"], "price": lv["price"],
                     "source_mode": lv["source_mode"], "score": lv.get("score"),
                     "fallback_used": lv.get("fallback_used", False),
                     "authority": "ITMQ_WALL_ENGINE"})
    payload["levels"] = kept
    # IDENTIDAD de cada línea que TRACE va a dibujar. No calcula ni renombra
    # nada: adjunta la procedencia que el nivel ya tiene, para que una línea sin
    # etiqueta se pueda identificar sin deducirla por el color.
    # PLAN del Scanner. TRACE lo REPRESENTA; no recalcula dirección ni deriva
    # entrada, objetivos o invalidación cuando el Scanner no los publica.
    try:
        from .core import scanner_plan as _SP
        plan = _SP.build(STATE.scanner or {}, spot=spot)
        payload["scanner_plan"] = plan
        # v1.54.0 · El plan SUSTITUYE a `target` y `risk`, no convive con ellos.
        #
        # Los dos salen del MISMO campo del Scanner —`target1/2` e
        # `invalidation`—, así que dibujarlos a la vez pintaba dos líneas
        # idénticas en el mismo precio con dos etiquetas encima: «OBJ1 533.70»
        # pegada a «OBJ 533.70». Un nivel duplicado no es un nivel más fuerte:
        # es la misma información ocupando el sitio de otra.
        if plan.get("ready"):
            kept = [lv for lv in kept
                    if not (isinstance(lv, dict) and lv.get("kind") in ("target", "risk"))]
        # Sus líneas entran en el MISMO conjunto que dibuja TRACE, con su kind
        # propio: así no hay una segunda ruta de render que mantener.
        for ln in plan.get("lines") or []:
            kept.append({"kind": ln["kind"], "name": ln["name"], "price": ln["price"],
                         "authority": "SCANNER", "source_mode": "SCANNER",
                         "direction": ln["direction"], "strength": ln["strength"],
                         "thesis_id": plan.get("thesis_id")})
        payload["levels"] = kept
    except Exception as exc:
        _obs_note("main:scanner_plan", exc, severity="DEGRADED")
        payload["scanner_plan"] = {"ready": False, "state": "ESPERANDO",
                                   "detail": "el plan del Scanner no se pudo construir",
                                   "lines": []}
    try:
        from .core import level_identity as _LI
        # v1.55.0 · Ademas de describir, DENUNCIA. Una lista larga de filas
        # correctas esconde bien las dos que no lo son, asi que el recuento de
        # lineas sin identidad viaja aparte y el Auditor lo puede leer de un
        # vistazo. Mientras `unidentified` no sea cero, hay un defecto abierto.
        # v1.57.0 · El ciclo se NOMBRA. `persistence.cycles` sostiene la frase
        # «este muro lleva en pie N ciclos»; si el ciclo no tiene nombre, cada
        # lector que describa los niveles suma uno más. El instante de la última
        # vela es lo que de verdad distingue un refresco del siguiente aquí.
        _velas = payload.get("candles") or []
        _ultima = _velas[-1].get("t") if (_velas and isinstance(_velas[-1], dict)) else None
        _ciclo = f"{symbol}#{_ultima}" if _ultima else None
        auditoria = _LI.audit(kept, symbol=symbol, cycle_id=_ciclo)
        payload["level_identity"] = auditoria["rows"]
        payload["level_identity_audit"] = {k: v for k, v in auditoria.items() if k != "rows"}
    except Exception as exc:
        _obs_note("main:level_identity", exc, severity="DEGRADED")
        payload["level_identity"] = []
        payload["level_identity_audit"] = {"ok": False, "unidentified": None,
                                           "detail": "la identidad de niveles no se pudo resolver"}
    return walls


def _resolve_gamma_migration(payload: dict, intel: dict, symbol: str) -> dict:
    """Migración de gamma anclada al STRIKE donde se confirmó.

    El motor ya la calculaba; lo que faltaba era publicar el par de strikes —de
    dónde salió la exposición y adónde fue— para poder dibujarlo sobre el eje de
    precio en vez de resumirlo en una tarjeta. Un número de migración sin strike no
    se puede leer contra el gráfico.

    `QD_INTERVAL_MAP` es DIRECT_PROVIDER; esta lectura es DERIVED.
    """
    from .core import quant_data_hub as HUB
    from .core import itmq_intelligence as IQ

    im = payload.get("interval_map") or {}
    if not im.get("ready"):
        im = HUB.interval_map(symbol, intel, "GAMMA",
                              engine_heatmap=(payload.get("heatmap_history") or {}))
    mig = IQ.gamma_migration(symbol, im)
    if not mig.get("ready"):
        return mig

    top = mig.get("top_strikes") or []
    # De dónde salió (mayor caída) y adónde fue (mayor subida). Si todo el cambio va
    # en el mismo sentido no hay migración que dibujar: hay acumulación o descarga,
    # que es otra cosa y se declara como tal.
    gained = max((t for t in top if (t.get("change") or 0) > 0),
                 key=lambda t: t["change"], default=None)
    lost = min((t for t in top if (t.get("change") or 0) < 0),
               key=lambda t: t["change"], default=None)
    mig["from_strike"] = (lost or {}).get("strike")
    mig["to_strike"] = (gained or {}).get("strike")
    mig["kind"] = ("MIGRATION" if (gained and lost)
                   else "ACCUMULATION" if gained else "DISCHARGE" if lost else "NONE")
    if mig["from_strike"] is not None and mig["to_strike"] is not None:
        mig["label"] = f"Γ MIG {mig['from_strike']:g} → {mig['to_strike']:g}"
    elif mig["to_strike"] is not None:
        mig["label"] = f"Γ +{mig['to_strike']:g}"
    elif mig["from_strike"] is not None:
        mig["label"] = f"Γ −{mig['from_strike']:g}"
    else:
        mig["label"] = None
    return mig


@app.get("/api/nextgen/market-truth")
async def api_market_truth(symbol: str | None = None):
    return JSONResponse(_jsonable(MARKET_TRUTH.snapshot(str(symbol or STATE.symbol).upper())))


@app.get("/api/nextgen/feature-intelligence")
async def api_feature_intelligence():
    v=STATE.view()
    return JSONResponse(_jsonable(build_feature_intelligence(
        feature_snapshot=FEATURE_BUS.snapshot(v.symbol or STATE.symbol),
        market_state=v.get("market_state_field") or {}, regime_context=v.get("regime_context") or {},
        calibration=v.get("calibration") or {}, scanner=v.get("scanner") or {})))


@app.get("/api/nextgen/decision-intelligence")
async def api_decision_intelligence():
    state=STATE.public_state()
    return JSONResponse(_jsonable(state.get("decision_intelligence") or {}))


@app.get("/api/nextgen/flow-kinematics")
async def api_flow_kinematics():
    """Observed time-derivative flow diagnostics; SHADOW/context only."""
    with STATE.lock:
        out=dict(STATE.flow_kinematics_report or {})
        symbol=STATE.symbol
    if not out:
        out={"ready":False,"symbol":symbol,"status":"WAITING_FLOW","authority":"SHADOW_CONTEXT_ONLY"}
    return JSONResponse(_jsonable(out))


# ---------------------------------------------------------------------------
# SECCIÓN SIEMPRE ACTIVA · Anomalías & Momentum causal SHADOW (v1.40.8)
#
# Sección observacional aislada. Desde v1.40.8 no existe feature flag ni opt-out:
# siempre está disponible en la UI y solo LEE historia ya publicada por STATE.
# No participa en Scanner, no altera el motor y nunca ejecuta órdenes.
# ---------------------------------------------------------------------------
@app.get("/api/seccion/anomalias-rendimientos")
async def api_seccion_anomalias_rendimientos():
    """Anomalías de retorno + momentum causal SHADOW. Siempre activa, sin autoridad direccional."""
    try:
        from .core.return_anomalies import analizar_anomalias_momentum
        with STATE.lock:
            historia = STATE.history.copy() if isinstance(STATE.history, pd.DataFrame) else pd.DataFrame()
            simbolo = STATE.symbol
            # Preferimos barras 1m YA PUBLICADAS del mismo underlying desde Londres.
            # No hay llamada de red desde este endpoint: session_flow_tape fue producido
            # por el ciclo del motor y aquí solo se copia. Si no existe, se degrada al
            # histórico estructural ya disponible.
            _bars = (STATE.session_flow_tape or {}).get("bars") if isinstance(STATE.session_flow_tape, dict) else None
            precio_hist = _bars.copy() if isinstance(_bars, pd.DataFrame) and not _bars.empty else historia
            fuente_precio = "SESSION_ACTIVITY_BARS" if isinstance(_bars, pd.DataFrame) and not _bars.empty else "STATE_HISTORY"
            # Solo snapshots ya publicados. Esta sección no invoca providers, no recalcula
            # el motor y no escribe en STATE. El dict se copia para impedir mutación accidental.
            flow_ctx = dict(STATE.flow_kinematics_report or {})
        # Las barras de session_flow_tape se almacenan naive en hora Ecuador
        # para esta vertical. Se normalizan aquí una sola vez a UTC antes de entrar al módulo
        # aislado, que así no necesita conocer proveedores ni timezone del motor.
        if fuente_precio == "SESSION_ACTIVITY_BARS" and isinstance(precio_hist, pd.DataFrame) and "timestamp" in precio_hist.columns:
            _ts = pd.to_datetime(precio_hist["timestamp"], errors="coerce")
            try:
                if _ts.dt.tz is None:
                    _ts = _ts.dt.tz_localize("America/Guayaquil").dt.tz_convert("UTC")
                else:
                    _ts = _ts.dt.tz_convert("UTC")
                precio_hist = precio_hist.copy(); precio_hist["timestamp"] = _ts
            except Exception as _tz_exc:
                _obs_note("main:seccion_anomalias_timezone", _tz_exc)
        _last = None
        if isinstance(precio_hist, pd.DataFrame) and not precio_hist.empty and "timestamp" in precio_hist.columns:
            try:
                _last = str(pd.to_datetime(precio_hist["timestamp"], errors="coerce").max())
            except Exception:
                _last = None
        _cand = flow_ctx.get("candidate") if isinstance(flow_ctx, dict) else {}
        _cache_key = (simbolo, fuente_precio, int(len(precio_hist)) if isinstance(precio_hist, pd.DataFrame) else 0,
                      _last, str((_cand or {}).get("p_value")), str((_cand or {}).get("absorption_score")))
        reporte = _RETURN_ANOMALY_CACHE.get(_cache_key)
        if reporte is None:
            # CPU estadística fuera del event loop. Solo se recalcula cuando cambia el
            # último dato observado / contexto flow; los polls repetidos leen cache.
            reporte = await asyncio.to_thread(
                analizar_anomalias_momentum, precio_hist, simbolo=simbolo,
                contexto={"flow_kinematics": flow_ctx}
            )
            reporte["fuente_precio"] = fuente_precio
            _RETURN_ANOMALY_CACHE.clear()
            _RETURN_ANOMALY_CACHE[_cache_key] = reporte
        return JSONResponse(_jsonable({**reporte, "activa": True}))
    except Exception as _e:
        _obs_note("main:seccion_anomalias", _e)
        return JSONResponse(_jsonable({
            "seccion": "ANOMALIAS_RENDIMIENTOS", "activa": True, "listo": False,
            "direccion": None, "estado": "ERROR_SECCION",
            "detalle": "La sección falló de forma aislada; el motor no se ve afectado.",
        }))


@app.get("/api/nextgen/research-validation")
async def api_research_validation():
    v=STATE.view()
    return JSONResponse(_jsonable(build_research_validation(
        v.get("calibration") or {}, v.get("research_storage_report") or {}, v.get("scanner") or {})))


@app.get("/api/nextgen/causality-live")
async def api_causality_live(symbol: str | None = None):
    return JSONResponse(_jsonable(CAUSAL_RUNTIME.status(str(symbol or STATE.symbol).upper())))


@app.get("/api/nextgen/temporal-truth")
async def api_temporal_truth(symbol: str | None = None):
    return JSONResponse(_jsonable(TEMPORAL_TRUTH.snapshot(str(symbol or STATE.symbol).upper())))


@app.get("/api/nextgen/data-health")
async def api_data_health(symbol: str | None = None):
    report = TEMPORAL_TRUTH.snapshot(str(symbol or STATE.symbol).upper())
    return JSONResponse(_jsonable({"ready":report.get("ready"),"symbol":report.get("symbol"),"providers":report.get("provider_health"),
                                   "critical_stale_channels":report.get("critical_stale_channels"),"decision_safe":report.get("decision_safe"),
                                   "policy":"PER_PROVIDER_PER_CHANNEL_HEALTH_NO_GLOBAL_PROVIDER_RANK"}))


@app.get("/api/nextgen/profiles")
async def api_profiles(view: str = "Net"):
    v=STATE.view()
    snap=v.get("snapshot")
    return JSONResponse(_jsonable(build_profile_bundle(snap,view=view,spot=(v.get("gamma_delta") or {}).get("spot"))))


@app.get("/api/nextgen/expiry-intelligence")
async def api_expiry_intelligence():
    v=STATE.view()
    snap=v.get("snapshot")
    if snap is None:
        snap=v.get("history")
    return JSONResponse(_jsonable(build_expiry_intelligence(snap)))


@app.get("/api/nextgen/derivatives-intelligence")
async def api_derivatives_intelligence():
    v=STATE.view()
    return JSONResponse(_jsonable(v.get("derivatives_intelligence_report") or
        build_derivatives_intelligence(v.get("history"),observed=None)))


@app.get("/api/nextgen/structural-intelligence")
@app.get("/api/nextgen/proximity-scanner")
async def api_structural_intelligence():
    return JSONResponse(_jsonable(STATE.view().get("structural_intelligence_report") or {}))


@app.get("/api/nextgen/versioned-state")
async def api_versioned_state(symbol: str | None = None):
    sym=str(symbol or STATE.symbol).upper()
    return JSONResponse(_jsonable({"latest":VERSIONED_MARKET_STATE.latest(sym),"history":VERSIONED_MARKET_STATE.history(sym,20),"status":VERSIONED_MARKET_STATE.status()}))


@app.get("/api/nextgen/transport-benchmark")
async def api_transport_benchmark(iterations: int = 1000):
    return JSONResponse(_jsonable(await asyncio.to_thread(benchmark_transport,iterations)))


def _ndjson_line(kind: str, payload) -> bytes:
    return (json.dumps(_jsonable({"kind":kind,"data":payload}),ensure_ascii=False,separators=(",",":")) + "\n").encode("utf-8")


@app.get("/api/nextgen/trace-history.ndjson")
async def api_trace_history_ndjson(timeframe: str = "1m", tail_minutes: int = 0, window: float = 12.0):
    """Price-first progressive history. Quant overlays keep using the shared TRACE cache.

    Cache is emitted first, then a provider refresh may extend/replace it. The response is
    NDJSON so the browser can paint candles before Gamma/Delta/Scanner hydration finishes.
    """
    timeframe=str(timeframe or "1m").lower()
    if timeframe not in {"1m","3m","5m","15m"}: timeframe="1m"
    try: tail_minutes=0 if int(tail_minutes)==0 else max(15,min(int(tail_minutes),390))
    except Exception: tail_minutes=0
    async def gen():
        yield _ndjson_line("meta",{"ready":True,"symbol":STATE.symbol,"symbol_epoch":STATE.symbol_epoch,"timeframe":timeframe,
                                  "versioned_market_state":VERSIONED_MARKET_STATE.latest(STATE.symbol),
                                  "transport":"NDJSON_PRICE_FIRST_THEN_ITMQ_BINARY_LIVE"})
        cache=await asyncio.to_thread(STATE.trace_session_bootstrap,timeframe,False,STATE.symbol,"full_day",True,True)
        cached_bars=cache.get("bars") or []
        for i in range(0,len(cached_bars),500):
            yield _ndjson_line("candles",{"phase":"CACHE","replace":i==0,"rows":cached_bars[i:i+500]});await asyncio.sleep(0)
        fresh=await asyncio.to_thread(STATE.trace_session_bootstrap,timeframe,False,STATE.symbol,"full_day",False,True)
        fresh_bars=fresh.get("bars") or []
        if fresh_bars:
            for i in range(0,len(fresh_bars),500):
                yield _ndjson_line("candles",{"phase":"PROVIDER","replace":i==0,"rows":fresh_bars[i:i+500]});await asyncio.sleep(0)
        yield _ndjson_line("done",{"cache_rows":len(cached_bars),"provider_rows":len(fresh_bars),"source":fresh.get("source") or cache.get("source"),
                                    "handoff":"CACHE→PROVIDER→LIVE_BINARY","state_id":(VERSIONED_MARKET_STATE.latest(STATE.symbol) or {}).get("state_id")})
    return StreamingResponse(gen(),media_type="application/x-ndjson",headers={"Cache-Control":"no-store","X-ITMQ-Stream":"TRACE_HISTORY_V1"})


@app.get("/api/trace/session-bootstrap")
async def api_trace_session_bootstrap(
    timeframe: str = "1m", force: bool = False, symbol: str | None = None, scope: str = "full_day",
    cache_only: bool = False, allow_display_fallback: bool = True,
):
    timeframe=str(timeframe or "1m").lower()
    if timeframe not in {"1m","3m","5m","15m"}: timeframe="1m"
    scope=str(scope or "full_day").lower()
    if scope not in {"full_day","premarket_rth","rth"}: scope="full_day"
    return JSONResponse(_jsonable(await asyncio.to_thread(
        STATE.trace_session_bootstrap,timeframe,bool(force),symbol,scope,bool(cache_only),bool(allow_display_fallback)
    )))


@app.get("/api/nextgen/surface")
async def api_nextgen_surface(iv_shift: float = 0.0, option_view: str = "Net", expected_symbol: str | None = None, expected_epoch: int | None = None):
    shift=max(-0.20,min(0.20,float(iv_shift)))
    if option_view not in {"Net","Calls","Puts"}: option_view="Net"
    if expected_symbol and str(expected_symbol).upper()!=str(STATE.symbol).upper():
        return _stale_read("STALE_SYMBOL_REQUEST", expected_symbol=str(expected_symbol).upper(), active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    if expected_epoch is not None and int(expected_epoch)!=int(STATE.symbol_epoch):
        return _stale_read("STALE_SYMBOL_EPOCH", expected_epoch=int(expected_epoch), active_epoch=STATE.symbol_epoch, active_symbol=STATE.symbol)
    blocked=_publication_blocked_payload("NEXTGEN_SURFACE")
    if blocked is not None:
        return JSONResponse(_jsonable(blocked))
    return JSONResponse(_jsonable(await asyncio.to_thread(STATE.nextgen_surface, iv_shift=shift, option_view=option_view)))


@app.get("/api/nextgen/readiness")
async def api_nextgen_readiness():
    return JSONResponse(_jsonable(STATE.operational_readiness_report or {}))


@app.get("/api/nextgen/scenario")
async def api_nextgen_scenario():
    return JSONResponse(_jsonable(STATE.scenario_lab_report or {}))


@app.get("/api/nextgen/quantum")
async def api_nextgen_quantum():
    return JSONResponse(_jsonable(STATE.quantum_shadow_report or {}))


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(terminal_metrics.prometheus_text(), media_type="text/plain; version=0.0.4")



@app.websocket("/ws/aggression-delta")
async def ws_aggression_delta(websocket: WebSocket):
    """Compact LIVE/Replay aggression candles. Browser rendering stays presentation-only."""
    await websocket.accept()
    last_signature = None
    try:
        while True:
            blocked=_publication_blocked_payload("AGGRESSION_DELTA_WS")
            if blocked is not None:
                signature=("BLOCKED",str(blocked.get("publication_gate")))
                if signature != last_signature:
                    await websocket.send_json(_jsonable(blocked)); last_signature=signature
                await asyncio.sleep(0.25)
                continue
            tf = str(websocket.query_params.get("timeframe") or "1m").lower()
            if tf not in {f"{m}m" for m in TIMEFRAMES}:
                tf = "1m"
            payload = _current_aggression_payload(tf, 96)
            candles = payload.get("candles") or []
            tail = candles[-1] if candles else {}
            signature = (
                str(payload.get("symbol") or STATE.symbol), tf, int(payload.get("revision") or 0),
                str(tail.get("time") or ""), str(tail.get("direction") or ""),
                bool(tail.get("forming")), float(tail.get("close") or 0.0),
            )
            if signature != last_signature:
                await websocket.send_json(_jsonable(payload))
                last_signature = signature
            await asyncio.sleep(0.10 if not STATE.replay_context.is_replay else 0.35)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except Exception:
        try:
            await websocket.close()
        except Exception as _e:
            _obs_note('main:1281', _e)


@app.websocket("/ws/sophia/events")
async def ws_sophia_events(websocket: WebSocket):
    """Event-driven Sophia alerts; no polling of a paid AI service."""
    await websocket.accept()
    queue = SOPHIA.events.register()
    try:
        await websocket.send_json({"type":"SOPHIA_READY","status":_jsonable({**SOPHIA.status(),"voice":SOPHIA_VOICE.status()}),"watches":_jsonable(SOPHIA.watches()),"publication_gate":_jsonable(_publication_gate_snapshot())})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20.0)
                await websocket.send_json(_jsonable(event))
                try:
                    queue.task_done()
                except Exception as _e:
                    _obs_note('main:1298', _e)
            except asyncio.TimeoutError:
                await websocket.send_json({"type":"HEARTBEAT","watch_count":len(SOPHIA.watches())})
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except Exception:
        try:
            await websocket.close()
        except Exception as _e:
            _obs_note('main:1307', _e)
    finally:
        SOPHIA.events.unregister(queue)


@app.websocket("/ws/nextgen/ticks-bin")
async def ws_nextgen_ticks_bin(websocket: WebSocket):
    """Binary live-tick stream. Initial history still comes from the causal REST payload.

    This path avoids JSON allocations during the high-frequency incremental render loop.
    It does not change market data or Scanner logic.
    """
    await websocket.accept()
    cursor = 0
    signal_cursor = 0
    active_symbol = None
    cold_start = True
    try:
        while True:
            symbol=str(STATE.symbol).upper()
            if symbol!=active_symbol:
                # History arrives through the causal REST bootstrap.  The binary lane only
                # sends the newest observed tick on attach/symbol switch, then continues
                # incrementally.  This avoids replaying thousands of already-rendered ticks.
                active_symbol=symbol;cursor=0;cold_start=True
                signal_cursor=PRICE_TICK_FABRIC.signal_seq(symbol)
            batch=PRICE_TICK_FABRIC.since(symbol,after=cursor,limit=1 if cold_start else 4096)
            cold_start=False
            cursor = int(batch.get("last_seq") or cursor)
            ticks = batch.get("ticks") or []
            if ticks:
                await websocket.send_bytes(pack_tick_batch(batch.get("symbol") or symbol, ticks, cursor))

            # Event-driven wakeup replaces the old 25/75 ms polling loop.  A tiny 4 ms
            # microbatch absorbs a burst into one binary frame while keeping display
            # latency below one refresh frame on normal 60/120 Hz monitors.
            previous_signal=signal_cursor
            signal_cursor=await asyncio.to_thread(PRICE_TICK_FABRIC.wait_for_update, symbol, signal_cursor, 0.25)
            if signal_cursor>previous_signal:
                await asyncio.sleep(0.004)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except Exception:
        try: await websocket.close()
        except Exception as _e:
            _obs_note('main:1345', _e)


@app.websocket("/ws/nextgen/surface-bin")
async def ws_nextgen_surface_bin(websocket: WebSocket, field: str = "Gamma"):
    """Binary float32 surface updates for direct GPU upload."""
    await websocket.accept()
    seq = 0
    current = str(field or "Gamma")
    try:
        while True:
            blocked=_publication_blocked_payload("SURFACE_WS")
            if blocked is not None:
                await asyncio.sleep(0.25)
                continue
            # Optional text control frame lets the browser switch fields without reconnecting.
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=0.001)
                if msg: current = msg.strip()[:32]
            except asyncio.TimeoutError:
                _obs_expected('main:surface_control_poll_idle')
                continue
            payload = await asyncio.to_thread(STATE.nextgen_surface)
            fields = (payload or {}).get("fields") or {}
            matrix = fields.get(current)
            if matrix is None and fields:
                current = next(iter(fields)); matrix = fields[current]
            if matrix:
                rows = len(matrix); cols = len(matrix[0]) if rows else 0
                if rows and cols:
                    seq += 1
                    await websocket.send_bytes(pack_surface_frame(field=current, rows=rows, cols=cols, values=matrix, sequence=seq))
            await asyncio.sleep(0.20)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except Exception:
        try: await websocket.close()
        except Exception as _e:
            _obs_note('main:1377', _e)


@app.get("/api/nextgen/low-latency")
async def api_nextgen_low_latency():
    from .core.accelerated_quant import backend_status
    return JSONResponse({"rust_causality":RUST_CAUSAL_BRIDGE.status(),"rust_ingress":RUST_CAUSAL_INGRESS.status(),"live_causality":CAUSAL_RUNTIME.status(STATE.symbol),"chart_data_cache":CHART_DATA_CACHE.status(),"versioned_market_state":VERSIONED_MARKET_STATE.status(),"temporal_truth":TEMPORAL_TRUTH.snapshot(STATE.symbol),"quant_acceleration":backend_status(),"wire_protocol":"ITMQ-BINARY-v1","wasm_built":(Path(__file__).resolve().parent/"static"/"wasm"/"itmq_wasm_bridge.js").exists(),"version":APP_VERSION})

@app.get("/api/nextgen/live-scheduler")
async def api_nextgen_live_scheduler():
    _hsym, _hepoch = STATE.active()  # v1.27.21: coherent symbol/epoch for scheduler health
    payload = LIVE_REFRESH_SCHEDULER.snapshot() if ADAPTIVE_REFRESH_ENABLED else {
        "enabled": False, "state": "FIXED", "structural_interval_s": REFRESH_SECONDS,
        "flow_interval_s": FLOW_REFRESH_SECONDS, "policy": "FIXED_REFRESH_COMPAT",
        "authority": "SCHEDULING_ONLY",
    }
    payload["price_stream"] = PRICE_TICK_FABRIC.health(STATE.symbol)
    payload["option_stream"] = OPTION_FLOW_FABRIC.health(_hsym)
    payload["native_provider_streams"] = {
        "alpaca_sip": {k:v for k,v in PRICE_STREAM.status().items() if k != "last_tick"},
        "alpaca_opra": {k:v for k,v in OPTION_STREAM.status().items() if k != "last_event"},
        "tastytrade": TASTYTRADE.status(),
    }
    payload["background_analytics"] = {"quantdata": QUANTDATA.status()}
    payload["deferred_providers"] = {
    }
    return JSONResponse(_jsonable(payload))


@app.get("/api/trace/pulse")
async def api_trace_pulse(window: float = 12.0, expected_symbol: str | None = None, expected_epoch: int | None = None):
    try:
        window = min(max(float(window), 3.0), 30.0)
    except Exception:
        window = 12.0
    if expected_symbol and str(expected_symbol).upper() != str(STATE.symbol).upper():
        return _stale_read("STALE_SYMBOL_REQUEST", active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    if expected_epoch is not None and int(expected_epoch) != int(STATE.symbol_epoch):
        return _stale_read("STALE_SYMBOL_EPOCH", active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    blocked=_publication_blocked_payload("TRACE_PULSE")
    if blocked is not None:
        return JSONResponse(_jsonable(blocked))
    payload=normalize_trace_pulse_contract(STATE.trace_pulse(window), source="api/trace/pulse")
    if expected_symbol and str(payload.get("symbol") or "").upper() != str(expected_symbol).upper():
        return _stale_read("STALE_SYMBOL_RESPONSE", active_symbol=STATE.symbol, symbol_epoch=STATE.symbol_epoch)
    return JSONResponse(_jsonable(payload))


@app.get("/api/live/ticks")
async def api_live_ticks(after: int = 0):
    try:
        await asyncio.to_thread(STATE.archive_tape, False)
    except Exception as _e:
        _obs_note('main:1424', _e)
    batch=PRICE_TICK_FABRIC.since(STATE.symbol,after=after,limit=1800)
    # Cold-start compatibility while the first provider event is still entering the fabric.
    if not batch.get("ticks") and not batch.get("source"):
        batch=PRICE_STREAM.since(after,limit=1800)
        batch["fabric_cold_start_fallback"]="ALPACA_ONLY_UNTIL_PROVIDER_FABRIC_WARMS"
    return JSONResponse(_jsonable(batch))

@app.get("/api/terminal/tick")
async def api_terminal_tick():
    """Precio en vivo y nada más, para que la vela en formación se mueva tick a tick.

    El bundle y el trace son caros: reconstruyen estructura, perfiles y niveles, así
    que no pueden pedirse cada 250 ms. Pero el precio sí puede, y sin él la última
    vela sólo avanzaba cuando llegaba el ciclo pesado — que es exactamente el retraso
    que se ve en pantalla. Aquí no se calcula nada: se lee lo que la fabric de precio
    ya tiene publicado.
    """
    sym = STATE.symbol
    consensus = PROVIDER_BUS.snapshot(sym)
    ps = PRICE_STREAM.status()
    tick = ps.get("last_tick") if ps.get("symbol") == sym else None
    rust = RUST_CAUSAL_BRIDGE.latest_price(sym) or {}
    price = None
    source = None
    if consensus.get("ready") and consensus.get("consensus_price") is not None:
        price, source = consensus.get("consensus_price"), "PROVIDER_CONSENSUS"
    elif rust.get("price") is not None:
        price, source = rust.get("price"), "RUST_BRIDGE"
    elif isinstance(tick, dict) and tick.get("price") is not None:
        price, source = tick.get("price"), "PRICE_STREAM"
    return JSONResponse(_jsonable({
        "symbol": sym,
        "symbol_epoch": STATE.symbol_epoch,
        "price": price,
        "source": source,
        "timestamp": (rust.get("timestamp") or (tick or {}).get("timestamp")),
        "connected": bool(ps.get("connected")),
        "market_state": ps.get("market_state"),
        # En replay el reloj lo manda el usuario: la interfaz no debe adelantar la vela.
        "replay": bool(STATE.replay_context.is_replay),
    }))


@app.get("/api/providers/tastytrade/health")
async def api_tastytrade_health():
    return JSONResponse(_jsonable(TASTYTRADE.status()))


@app.get("/api/providers/tastytrade/test")
async def api_tastytrade_test(symbol: str = "DIA"):
    """Read-only diagnostics. Never executes orders and never returns credentials/tokens."""
    sym = str(symbol or STATE.symbol).upper().strip()
    if not TASTYTRADE.configured:
        return JSONResponse(_jsonable({
            "ready": False, "provider": "TASTYTRADE", "symbol": sym,
            "reason": "NOT_CONFIGURED", "scope": "READ ONLY",
            "health": TASTYTRADE.status(), "consensus": PROVIDER_BUS.snapshot(sym),
        }))
    try:
        if not TASTYTRADE.client:
            await TASTYTRADE.start(sym)
        plan = await TASTYTRADE.select_asset(sym)
        snapshot = []
        if TASTYTRADE.client and sym not in {"SPX", "NDX", "VIX"}:
            try:
                snapshot = await TASTYTRADE.client.market_data_by_type("equity", [sym])
            except Exception:
                snapshot = []
        return JSONResponse(_jsonable({
            "ready": True, "provider": "TASTYTRADE", "symbol": sym, "scope": "READ ONLY",
            "health": TASTYTRADE.status(), "ecosystem": plan, "market_snapshot": snapshot,
            "consensus": PROVIDER_BUS.snapshot(sym),
        }))
    except Exception as exc:
        return JSONResponse(_jsonable({
            "ready": False, "provider": "TASTYTRADE", "symbol": sym, "scope": "READ ONLY",
            "error": f"{type(exc).__name__}: {str(exc)[:180]}", "health": TASTYTRADE.status(),
            "consensus": PROVIDER_BUS.snapshot(sym),
        }), status_code=502)



@app.get("/api/providers/consensus")
async def api_provider_consensus(symbol: str | None = None):
    return JSONResponse(_jsonable(PROVIDER_BUS.snapshot(str(symbol or STATE.symbol).upper())))


@app.get("/providers/coverage", response_class=HTMLResponse)
async def provider_coverage_page(request: Request):
    return templates.TemplateResponse("provider_coverage.html", {"request": request, "app_version": APP_VERSION})


@app.get("/api/providers/coverage")
async def api_provider_coverage():
    """Coverage auditor: what the program has actually observed and archived."""
    return JSONResponse(_jsonable({
        "version": APP_VERSION,
        "data_lake": DATA_LAKE.status(),
        "library_collector": PROVIDER_LIBRARY.status(),
        "asset_ecosystem": ECOSYSTEM_RUNTIME.status(),
        "tastytrade_catalog": (TASTYTRADE.status().get("catalog") or {}),
        "temporal_truth": TEMPORAL_TRUTH.snapshot(STATE.symbol),
        "versioned_market_state": VERSIONED_MARKET_STATE.latest(STATE.symbol),
        "versioned_market_state_status": VERSIONED_MARKET_STATE.status(),
        "transport_policy": transport_policy(),
        "policy": "MEASURED_COVERAGE_ONLY · NO_FIXED_PROVIDER_RANK · QUALITY_BY_OBSERVATION · NEVER_CLAIM_UNDOCUMENTED_PROVIDER_LIBRARY",
    }))


@app.get("/api/providers/data-lake")
async def api_provider_data_lake():
    return JSONResponse(_jsonable(DATA_LAKE.status()))


@app.post("/api/providers/library/collect-next")
async def api_provider_library_collect_next():
    """Manual WARM/COLD step for diagnostics; does not alter LIVE subscriptions."""
    try:
        out = await PROVIDER_LIBRARY.collect_next_asset()
        return JSONResponse(_jsonable({"ok": True, "result": out, "status": PROVIDER_LIBRARY.status()}))
    except Exception as exc:
        return JSONResponse(_jsonable({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}", "status": PROVIDER_LIBRARY.status()}), status_code=502)


@app.get("/api/providers/flow-health")
async def api_provider_flow_health(symbol: str | None = None):
    """Fast local proof of what is actually flowing through ITM QUANT.

    No network calls occur on this health path. Live market transport is Alpaca +
    tastytrade; Quant Data is reported as normalized options-intelligence context.
    """
    sym=str(symbol or STATE.symbol).upper().strip()
    market=PROVIDER_BUS.snapshot(sym)
    price_flow=PRICE_TICK_FABRIC.health(sym)
    option_flow=OPTION_FLOW_FABRIC.health(sym)
    feature_flow=FEATURE_BUS.snapshot(sym)
    lake=DATA_LAKE.status()
    alp=alpaca_data.load_settings(); tasty=TASTYTRADE.status(); qd=QUANTDATA.status()
    try:
        opra_diag=STATE._opra_diagnostics(sym)
    except Exception as exc:
        opra_diag={"symbol":sym,"status":"ERROR","last_error":f"{type(exc).__name__}: {str(exc)[:120]}"}
    lake_providers=dict(lake.get("providers") or {})
    macro_state=dict(getattr(STATE,"macro",{}) or {})
    qdepth=int(lake.get("queue_depth") or 0); qcap=int(lake.get("queue_capacity") or 0)
    pressure=(qdepth/qcap) if qcap>0 else 0.0
    issues=[]; advisories=[]
    clock=dict(price_flow.get("session_expectation") or option_flow.get("session_expectation") or {})
    expected_price=bool(clock.get("expected_price_live")); expected_options=bool(clock.get("expected_option_flow_live"))
    if expected_price and price_flow.get("state") not in {"LIVE"}:issues.append(str(price_flow.get("bottleneck") or "NO_FRESH_PRICE_PROVIDER"))
    elif price_flow.get("state") in {"STRUCTURAL","EXPECTED_IDLE"}:advisories.append(f"PRICE_{price_flow.get('state')}")
    if expected_options and option_flow.get("state") in {"NO_UNIVERSE"}:issues.append(str(option_flow.get("bottleneck") or "NO_OPTION_CONTRACT_UNIVERSE"))
    elif option_flow.get("bottleneck"):advisories.append(str(option_flow.get("bottleneck")))
    if pressure>=0.80:issues.append("DATA_LAKE_QUEUE_PRESSURE")
    alp_price=PRICE_STREAM.status(); alp_opt=OPTION_STREAM.status()
    tasty_connected=str(tasty.get("dxlink") or tasty.get("state") or "").upper() in {"CONNECTED","ACTIVE","READY"}
    futures_clock=str(clock.get("asset_clock") or "").upper()=="FUTURES"
    if tasty.get("configured") and not tasty_connected:
        (issues if futures_clock and expected_price else advisories).append("TASTYTRADE_DXLINK_NOT_LIVE")
    if alp and expected_price and not bool(alp_price.get("connected")) and "ALPACA_SIP" not in (price_flow.get("providers_live") or []):issues.append("ALPACA_SIP_NOT_LIVE")
    elif alp and not bool(alp_price.get("connected")):advisories.append("ALPACA_SIP_IDLE_OR_DISCONNECTED")
    if alp and expected_options and not bool(alp_opt.get("connected")) and "ALPACA_OPRA" not in (option_flow.get("live_trade_sources") or []):advisories.append("ALPACA_OPRA_NOT_LIVE")
    issues=list(dict.fromkeys(x for x in issues if x and x!="CLEAR")); advisories=list(dict.fromkeys(x for x in advisories if x and x!="CLEAR"))
    configured_scope=[]
    if alp: configured_scope.extend(["ALPACA_SIP","ALPACA_OPRA"])
    if tasty.get("configured"): configured_scope.append("TASTYTRADE_DXLINK")
    if qd.get("configured"): configured_scope.append("QUANTDATA")
    connected_scope=[]
    if bool(alp_price.get("connected")): connected_scope.append("ALPACA_SIP")
    if bool(alp_opt.get("connected")): connected_scope.append("ALPACA_OPRA")
    if tasty_connected: connected_scope.append("TASTYTRADE_DXLINK")
    if qd.get("running"): connected_scope.append("QUANTDATA")
    observed_scope=list(dict.fromkeys(str(x) for x in ((price_flow.get("providers_live") or [])+(option_flow.get("live_trade_sources") or [])+(feature_flow.get("usable_providers") or [])) if x))
    connected_scope=list(dict.fromkeys(connected_scope)); configured_scope=list(dict.fromkeys(configured_scope))
    return JSONResponse(_jsonable({
        "version":APP_VERSION,"symbol":sym,"state":("DEGRADED" if issues else (price_flow.get("state") if price_flow.get("state") in {"STRUCTURAL","EXPECTED_IDLE"} else "CLEAR")),
        "issues":issues,"advisories":advisories,"session_expectation":clock,
        "price":{**price_flow,"market_consensus":market},"options":option_flow,"feature_fusion":feature_flow,
        "configured":{"alpaca":bool(alp),"tastytrade":bool(tasty.get("configured")),"quantdata":bool(qd.get("configured")),"official_macro":True},
        "configured_provider_scope":configured_scope,"connected_provider_scope":connected_scope,
        "active_observed_provider_scope":observed_scope,"active_provider_scope":observed_scope,
        "runtime":{
            "alpaca_sip":{k:v for k,v in alp_price.items() if k!="last_tick"},
            "alpaca_opra":{k:v for k,v in alp_opt.items() if k!="last_event"},
            "tastytrade":tasty,"quantdata":qd,"option_diagnostics":opra_diag,
            "official_macro":{"observed":bool(macro_state.get("series") or macro_state.get("events")),"series_points":len(macro_state.get("series") or []),"events":len(macro_state.get("events") or []),"data_lake":lake_providers.get("OFFICIAL_MACRO",{})},
            "provider_observation_coverage":lake_providers,
            "data_lake_queue":{"depth":qdepth,"capacity":qcap,"pressure_pct":round(pressure*100,2),"dropped":lake.get("dropped_archive_records")},
        },
        "redundancy_state":"MULTI_PROVIDER" if int(option_flow.get("redundancy") or 0)>=2 else "SINGLE_PROVIDER" if int(option_flow.get("redundancy") or 0)==1 else "NO_LIVE_OPTION_FLOW",
        "health_contract":"NON_BLOCKING_HEALTH · NO NETWORK CALLS",
        "policy":"NO NETWORK CALLS ON HEALTH PATH · NATIVE ITM STRUCTURE · QUANTDATA FEATURE CORROBORATION · BOUNDED LANES · NO RAW TAPE DOUBLE COUNT",
    }))


@app.get("/api/providers/status")
async def api_providers_status(symbol: str | None = None):
    """Fast local provider status; configured-only connectors are not treated as observed."""
    sym = str(symbol or STATE.symbol).upper()
    alp = alpaca_data.load_settings(); qd=QUANTDATA.status()
    price_health=PRICE_TICK_FABRIC.health(sym); option_health=OPTION_FLOW_FABRIC.health(sym)
    alp_runtime=PRICE_STREAM.status(); opra_runtime=OPTION_STREAM.status(); tasty_runtime=TASTYTRADE.status()
    alp_observed=bool("ALPACA_SIP" in (price_health.get("providers_live") or []) or "ALPACA_OPRA" in (option_health.get("live_trade_sources") or []))
    tasty_observed=bool("TASTYTRADE_DXLINK" in (price_health.get("providers_live") or []) or "TASTYTRADE_DXLINK" in (option_health.get("live_trade_sources") or []))
    feature=FEATURE_BUS.snapshot(sym)
    qd_observed=bool("QUANTDATA" in (feature.get("usable_providers") or []))
    providers={
        "alpaca":{"configured":bool(alp),"role":"MARKET_DATA_PROVIDER","first_class":True,"active_provider":alp_observed,"connected":bool(alp_runtime.get("connected") or opra_runtime.get("connected")),"observed_provider":alp_observed},
        "tastytrade":{**tasty_runtime,"role":"MARKET_DATA_DERIVATIVES_PROVIDER","first_class":True,"active_provider":tasty_observed,"observed_provider":tasty_observed},
        "quantdata":{**qd,"role":"OPTIONS_INTELLIGENCE_PROVIDER","first_class":True,"active_provider":qd_observed,"observed_provider":qd_observed,"authority":"CORROBORATION_ONLY_NATIVE_MATH_RETAINS_AUTHORITY"},
        "official_macro":{"configured":True,"role":"MACRO_EVENT_CONTEXT","first_class":True,"active_provider":True,"sources":["FRED","BLS","FEDERAL_RESERVE"]},
    }
    return JSONResponse(_jsonable({
        "symbol":sym,"policy":"ACTIVE_ONLY_AFTER_OBSERVED_DATA · QUALITY_BY_OBSERVATION",
        "providers":providers,"excluded_providers":{},
        "market_consensus":PROVIDER_BUS.snapshot(sym),"market_truth":MARKET_TRUTH.snapshot(sym),"feature_fusion":feature,
        "option_flow_fabric":OPTION_FLOW_FABRIC.health(sym),"price_tick_fabric":PRICE_TICK_FABRIC.health(sym),
        "asset_ecosystem":ECOSYSTEM_RUNTIME.status(),"ecosystem_definition":public_summary(sym),"data_lake":DATA_LAKE.status(),"library_collector":PROVIDER_LIBRARY.status(),
        "note":"Alpaca+tastytrade sostienen transporte LIVE; Quant Data aporta inteligencia de opciones normalizada. La estructura Gamma/Delta/GEX/DEX se calcula nativamente en ITM QUANT.",
    }))


_VERSION_PATH = Path(__file__).resolve().parents[1] / "VERSION.txt"


def _release_version() -> str:
    """Única fuente de verdad de la versión: VERSION.txt.

    Antes el número estaba escrito a mano en VERSION.txt, en
    .itm_quant_product.json y como literal en /health. Tres copias manuales es
    exactamente por qué las releases salían desincronizadas.
    """
    try:
        return _VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _obs_note("main:release_version", exc)
        return "unknown"



# ---------------------------------------------------------------------------
# TERMINAL DE ANALISTA (v1.41.0)
#
# Un endpoint por vista, más un bundle consolidado. La interfaz no recalcula
# nada: consume el resultado que el motor ya publicó.
# ---------------------------------------------------------------------------

def _parity_snapshot() -> dict:
    """Estado de paridad entre proveedores, sin tocar la red."""
    try:
        return parity_report(
            alpaca_configured=bool(alpaca_data.load_settings()),
            tastytrade_status=TASTYTRADE.status(),
            quantdata_status=QUANTDATA.status(),
            consensus=(STATE.public_state().get("provider_consensus") or {}),
            options_coverage=QUANTDATA_INTELLIGENCE.coverage(),
        )
    except Exception as exc:
        _obs_note("main:parity_snapshot", exc, severity="DEGRADED")
        return {"ready": False, "error": f"{type(exc).__name__}", "policy": parity_policy()}


@app.get("/api/terminal/bundle")
async def api_terminal_bundle(timeframe: str = "1m", tail_minutes: int = 390, interval_greek: str = "GAMMA"):
    """Todo lo que dibuja la terminal, en una sola lectura coherente.

    Se sirve el mismo trace que consume TRACE para que las cifras de cabecera y
    el gráfico no puedan describir dos instantes distintos.
    """
    timeframe = str(timeframe or "1m").lower()
    if timeframe not in {"1m", "3m", "5m", "15m"}:
        timeframe = "1m"
    try:
        tail_minutes = 0 if int(tail_minutes) == 0 else max(15, min(int(tail_minutes), 390))
    except Exception:
        tail_minutes = 390

    state = STATE.public_state()
    _live_bucket = 0 if STATE.replay_context.is_replay else int(time.time() / TRACE_LIVE_REPRICE_SECONDS)
    key = f"trace|{STATE.symbol}|{STATE.symbol_epoch}|{STATE.analytics_revision}|{timeframe}|{tail_minutes}|12.000|{STATE.replay_context.mode}|{_live_bucket}"
    try:
        trace = await asyncio.to_thread(
            CHART_DATA_CACHE.get_or_build, key,
            lambda: STATE.nextgen_trace(timeframe=timeframe, tail_minutes=tail_minutes, visual_window=None))
    except Exception as exc:
        _obs_note("main:terminal_bundle_trace", exc, severity="DEGRADED")
        trace = {"ready": False, "candles": [], "option_prints": [], "profiles": {}, "levels": []}

    bundle = build_terminal_bundle(
        state=state, trace=trace,
        intelligence=QUANTDATA_INTELLIGENCE.snapshot(),
        parity=_parity_snapshot(),
        coverage=QUANTDATA_INTELLIGENCE.coverage(),
        interval_greek=str(interval_greek or "GAMMA").upper(),
    )
    return JSONResponse(_jsonable(bundle))


@app.get("/api/terminal/diagnostics")
async def api_terminal_diagnostics(timeframe: str = "1m", tail_minutes: int = 390):
    """Por qué cada panel tiene o no tiene datos ahora mismo."""
    timeframe = str(timeframe or "1m").lower()
    if timeframe not in {"1m", "3m", "5m", "15m"}:
        timeframe = "1m"
    try:
        tail_minutes = 0 if int(tail_minutes) == 0 else max(15, min(int(tail_minutes), 390))
    except Exception:
        tail_minutes = 390
    state = STATE.public_state()
    try:
        trace = await asyncio.to_thread(
            STATE.nextgen_trace, timeframe=timeframe, tail_minutes=tail_minutes, visual_window=None)
    except Exception as exc:
        trace = {"ready": False, "upstream_error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return JSONResponse(_jsonable(build_diagnostics(
        state=state, trace=trace,
        coverage=QUANTDATA_INTELLIGENCE.coverage(),
        parity=_parity_snapshot(),
        # Los carriles del proveedor, para que el diagnóstico de Dark Pool mire
        # la MISMA fuente que la pantalla y no la capa derivada.
        intel=QUANTDATA_INTELLIGENCE.snapshot(),
    )))


@app.get("/api/providers/parity")
async def api_provider_parity():
    """Paridad de proveedores en opciones: quién participa y con qué calidad."""
    return JSONResponse(_jsonable(_parity_snapshot()))


@app.get("/api/quantdata/coverage")
async def api_quantdata_coverage():
    """Cobertura real de las páginas integradas de Quant Data."""
    return JSONResponse(_jsonable(QUANTDATA_INTELLIGENCE.coverage()))


@app.get("/api/quantdata/tool/{tool_key}")
async def api_quantdata_tool(tool_key: str):
    """Datos normalizados de una herramienta concreta del proveedor."""
    data = QUANTDATA_INTELLIGENCE.get(str(tool_key))
    if not data:
        return JSONResponse(_jsonable({"ready": False, "tool": tool_key, "reason": "NO_DATA_YET"}))
    return JSONResponse(_jsonable({**data, "tool": tool_key}))


@app.get("/healthz")
async def healthz():
    """Minimal unauthenticated liveness probe; no provider or filesystem details."""
    return {"status": "ok", "app": APP_NAME, "version": _release_version()}


@app.get("/health")
async def health():
    # v1.27.1: la versión se LEE de VERSION.txt en vez de estar hardcodeada aquí.
    # Estaba escrita a mano en tres sitios (VERSION.txt, .itm_quant_product.json y
    # este literal), que es justo el origen de la desincronización de releases.
    _bind_host = _BIND_HOST
    _loopback = net_guard.is_loopback(_bind_host)
    _hv = STATE.view()
    _hsym, _hepoch = STATE.active()
    return {
        "status": "ok", "app": APP_NAME, "version": _release_version(),
        "data_ready": bool(_hv.get("gamma_delta")), "mode": _hv.get("mode"),
        "auth": "disabled-local" if _loopback else "token-required",
        "bind_host": _bind_host,
        "state_view": {"generation": _hv.generation, "stage": _hv.stage,
                       "published_at": _hv.published_at.isoformat() if _hv.generation else None,
                       "active_symbol": _hsym, "symbol_epoch": int(_hepoch)},
        # v1.27.1 · Degradación silenciosa visible.
        # Antes había 181 `except Exception: pass`. Un fetch caído, una calibración
        # corrupta o un griego inválido degradaban a un default y NO se veía en
        # ningún lado. Ahora cada degradación tolerada queda contada por sitio:
        # si este bloque no está en OK, el motor está funcionando a medias.
        "degradations": obs.degradations(),
        "quant_governance": governance_contract(),
        "session_security": net_guard.session_state(),
        "oi_structural_freshness": ((STATE.data_quality_report or {}).get("oi_structural_freshness")
                                    if isinstance(STATE.data_quality_report, dict) else None),
        "providers": {
            "policy": "NO_FIXED_PROVIDER_RANK · QUALITY_BY_OBSERVATION",
            "alpaca": {"configured": bool(alpaca_data.load_settings()), "role": "MARKET_DATA_PROVIDER", "first_class": True},
            "quantdata": {**QUANTDATA.status(), "role": "OPTIONS_INTELLIGENCE_PROVIDER", "first_class": True, "authority": "CORROBORATION_ONLY"},
            "tastytrade": {**TASTYTRADE.status(), "role": "MARKET_DATA_DERIVATIVES_PROVIDER", "first_class": True},
            "market_consensus": PROVIDER_BUS.snapshot(_hsym),
            "market_truth": MARKET_TRUTH.snapshot(STATE.symbol),
            "feature_fusion": FEATURE_BUS.snapshot(_hsym),
            "option_flow_fabric": OPTION_FLOW_FABRIC.health(STATE.symbol),
            "live_causality": CAUSAL_RUNTIME.status(STATE.symbol),
            "chart_data_cache": CHART_DATA_CACHE.status(),
            "data_lake": DATA_LAKE.status(),
            "library_collector": PROVIDER_LIBRARY.status(),
        },
        "persistent_memory": {
            "root": str(PERSISTENCE_REPORT.persistent_root), "status": PERSISTENCE_REPORT.status,
            "source_policy": PERSISTENCE_REPORT.source_policy, "migrated_files": PERSISTENCE_REPORT.migrated_files,
            "backup": str(PERSISTENCE_REPORT.backup) if PERSISTENCE_REPORT.backup else None,
            "policy": "NO RESET LIVE/AUDITOR/CALIBRATION/REPLAY",
        },
    }

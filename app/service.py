from __future__ import annotations

import json
import re
import base64
import math
import os
import time as _time
import threading
from dataclasses import dataclass, field, field as dc_field
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .persistence import routed_dir
from .version import APP_VERSION
from .core.expiry_clock import year_fraction
from .core.volatility_surface import skew_term_structure
from .config import (
    DATA_MODE, STRIKE_WINDOW, PREMARKET_PATH,
    PREMARKET_FREEZE_MINUTES, HOT_SNAPSHOTS, CALIBRATION_MIN_SAMPLES, PERSISTENCE_REPORT,
)
from .core.frame_guards import numeric_column
from .core.engine import engine_config_for_asset, analyze_gamma_delta, audit as _engine_audit
from .core import alpaca_data
from .core.assets import ASSETS, DEFAULT_ASSET, asset_info, core_selectable_assets, chain_window_for
from .core.multi_asset_board import board_row, build_board
from .core.alpaca_data import load_settings, fetch_asset_options_snapshot as fetch_alpaca_options_snapshot, append_history, load_history
from .core.live_price import PRICE_STREAM
from .core.option_stream import OPTION_STREAM
from .core.provider_flow_fabric import OPTION_FLOW_FABRIC, PRICE_TICK_FABRIC
from .core.flow_intelligence import (
    fetch_recent_option_trades, load_flow_events, save_flow_events, flow_session_summary,
    premarket_tape_summary, london_activity_tape_summary, apply_normalized_flow_scores, flow_anchor_samples,
)
from .core.advanced_visuals import (
    trace_landscape_3d, add_quant_score, _aggression_threshold,
)
from .core.macro_dia import fetch_macro_context, macro_chart
from .core.large_prints import (
    fetch_recent_large_prints, load_large_prints, large_print_summary, large_print_chart,
    large_print_anchor_samples,
)
from .core.gamma_migration import gamma_migration_figure, migration_digest
from .core.conditional_outcomes import conditional_outcome_report
from .core.institutional_modules import (
    flow_unusual_pro_figure, flow_pro_summary, quantdata_net_drift_figure, exposure_by_strike_pro_figure, exposure_summary,
    gex_matrix_figure, net_positioning_by_strike_figure,
    volume_by_strike_figure, volume_summary,
)
from .core.scenario_engine import build_quant_scanner, scanner_route_figure, save_scanner_snapshot
from .core.expiry_window import (
    WINDOW_AUTO, WINDOWS, LABELS, normalize_window, apply_expiry_window, filter_events, expiry_confluence, zero_dte_status, effective_horizon_days,
)
from .core.precision_engine import (
    build_data_quality, build_model_health, what_changed, exposure_attribution, market_inputs, black_scholes_price,
    exposure_scenarios, american_model_check,
)
from .core.regime_engine import classify_regime
from .core.calibration import calibration_report
from .core import session_resolver
from .core.session_memory import append_session_metric, session_memory_summary, session_metric_frame, structural_flow_frame, iv_rank_native
from .core.freshness import evaluate_publication_gate, to_epoch
from .core.dealer_intelligence import dealer_intelligence, event_hedge_impact
from .core.external_markets import external_market_context, provider_redundancy
from .core.source_fusion import source_context, enrich_external_market_context, chain_validation, model_agreement
from .core.research_store import ResearchStore
from .core.audit_reporting import write_audit_reports
from .core.institutional_research import institutional_research_snapshot
from .core.plain_language import easy_view
from .core.quant_synthesis import build_quant_synthesis
from .core.market_state_field import build_market_state_field
from .core.premarket_intelligence import build_premarket_report
from .core.live_validation import live_validation_status
from .core.trace_analytics import (
    aggression_bars, aggression_strength, tape_confirmation, bar_cadence, multi_timeframe_aggression,
    flow_bar_anchor_samples, atm_iv_and_dte,
)
from .core.monte_carlo import monte_carlo_level_report
from .core.trace_live import build_trace_pulse
from .core.nextgen_terminal import build_nextgen_trace_payload, build_quant_surface_payload, candles_from_ticks, key_levels_report
from .core.causality_engine import causal_sort_frame, unify_market_events
from .core.low_latency_bridge import RUST_CAUSAL_BRIDGE
from .core.provider_bus import PROVIDER_BUS, FEATURE_BUS
from .core.market_truth import MARKET_TRUTH
from .core.feature_intelligence import build_feature_intelligence
from .core.research_validation import build_research_validation
from .core.decision_intelligence import build_decision_intelligence
from .core.temporal_truth import TEMPORAL_TRUTH
from .core.versioned_market_state import VERSIONED_MARKET_STATE
from .core.profile_engine import build_profile_bundle
from .core.expiry_intelligence import build_expiry_intelligence
from .core.derivatives_intelligence import build_derivatives_intelligence
from .core.structural_intelligence import STRUCTURAL_INTELLIGENCE
from .core.native_options_structure import build_native_options_structure
from .core.operational_readiness import build_operational_readiness, latency_summary
from .core.scenario_lab import build_scenario_lab
from .core.quantum_ready import build_qubo
from .core.flow_kinematics import build_flow_kinematics
from .core.instant_boot import get_chain_window_hint, save_chain_window_hint, status as instant_boot_hint_status
from .core.always_on_state import READY_STORE
from .providers.tastytrade import TASTYTRADE
from .core.replay import (
    ReplayContext, filter_asof, persist_tape, load_tape, available_sessions as replay_available_sessions,
    session_coverage as replay_session_coverage, session_clock as replay_session_clock, backtest_range as replay_backtest_range,
    load_structural_history as replay_load_structural_history,
)
from .core.historical_session_store import HistoricalSessionStore
from .core.obs import note as _obs_note, guard

def _archive_option_replay(symbol: str, snapshot, mode: str) -> None:
    """Graba la materia prima observada de la cadena para el replay histórico.

    Es la única forma de tener un backtest de opciones con bid/ask reales: los
    snapshots OPRA no se pueden reconstruir después. Sólo en LIVE, porque un
    histórico contaminado con DEMO no sirve para valorar nada.
    """
    if mode != "LIVE" or snapshot is None or len(snapshot) == 0:
        return
    try:
        from .core.option_replay_store import record_snapshot
        record_snapshot(snapshot, symbol=symbol)
    except Exception as exc:  # noqa: BLE001 - archivado, nunca crítico para la sesión
        _obs_note("service:option_replay_archive", exc, severity="DEGRADED")


EC = ZoneInfo("America/Guayaquil")
NY = ZoneInfo("America/New_York")




def _live_provider_configured(symbol: str) -> bool:
    cfg=asset_info(symbol)
    qp=str(cfg.get("quant_provider") or "ALPACA").upper()
    kind=str(cfg.get("kind") or cfg.get("category") or "").upper()
    tasty_ok=bool(TASTYTRADE.configured)
    if qp=="TASTYTRADE":
        return tasty_ok
    return bool(load_settings() is not None or tasty_ok)


def _tastytrade_own_chain_snapshot(symbol: str, strike_window: float, expiry_days: int):
    if not TASTYTRADE.configured:
        raise RuntimeError("tastytrade no está configurado")
    frame,meta=TASTYTRADE.market_data.structural_chain_frame(symbol,strike_window=strike_window,expiry_days=expiry_days)
    if not isinstance(frame,pd.DataFrame) or frame.empty:
        raise RuntimeError(str((meta or {}).get("reason") or "tastytrade DXLink todavía no tiene cadena propia hidratada"))
    return frame,meta


def is_transient_chain_failure(error: Exception | str) -> bool:
    """Classify only provider/startup conditions that are safe to retry automatically."""
    text=str(error or "").upper()
    transient=(
        "HTTP 504","HTTP 503","HTTP 502","HTTP 500","HTTP 429","TIMEOUT","TIMED OUT",
        "NO_OWN_STRUCTURAL_CONTRACTS_IN_MEMORY","NO_OWN_UNDERLYING_PRICE","CHAIN_WARMING","HYDRAT",
        "TRANSPORT TIMEOUT/ERROR","CONNECTIONERROR","READTIMEOUT","CONNECTTIMEOUT",
    )
    deterministic=("HTTP 401","HTTP 403","CREDENTIAL","NO ESTÁ CONFIGURADO","NOT_CONFIGURED")
    if any(x in text for x in deterministic):
        return False
    return any(x in text for x in transient)


def _wait_tastytrade_own_chain(symbol: str, strike_window: float, expiry_days: int, *, wait_seconds: float | None = None):
    """Wait briefly for already-starting DXLink OWN-instrument hydration after a primary failure.

    No REST/network request is made here.  The Quant worker polls only TASTYTRADE's in-memory
    structural chain while the async provider runtime continues OAuth/discovery/DXLink hydration
    on the event loop.  This closes the startup race without inventing proxy contracts.
    """
    if not TASTYTRADE.configured:
        raise RuntimeError("tastytrade no está configurado")
    if wait_seconds is None:
        try:
            wait_seconds=float(os.getenv("ITM_QUANT_TASTY_CHAIN_FAILOVER_WAIT_SECONDS","10"))
        except Exception:
            wait_seconds=10.0
    wait_seconds=max(0.0,min(float(wait_seconds),20.0))
    deadline=_time.monotonic()+wait_seconds
    last_exc: Exception | None=None
    while True:
        try:
            frame,meta=_tastytrade_own_chain_snapshot(symbol,strike_window,expiry_days)
            meta=dict(meta or {})
            meta["structural_route"]="TASTYTRADE_DXLINK_WARM_FAILOVER"
            meta["failover_wait_seconds"]=round(max(0.0,wait_seconds-max(0.0,deadline-_time.monotonic())),3)
            return frame,meta
        except Exception as exc:
            last_exc=exc
        if _time.monotonic() >= deadline:
            break
        _time.sleep(0.25)
    raise RuntimeError(f"CHAIN_WARMING: tastytrade own-chain hydration not ready ({type(last_exc).__name__}: {str(last_exc)[:120]})") from last_exc


def fetch_asset_options_snapshot(symbol: str, strike_window: float, expiry_days: int):
    """Provider-aware OWN-instrument chain route; no ETF/index/future proxy substitution.

    This public compatibility seam is intentionally monkeypatchable by regression tests
    and diagnostics.  It routes the selected instrument to its own derivative provider;
    related ETF/index/future instruments are never substituted as the primary chain.
    """
    cfg=asset_info(symbol);provider=str(cfg.get("quant_provider") or "ALPACA").upper()
    primary_error=None
    # First-class hot path: if tastytrade DXLink already holds a fresh OWN-instrument
    # chain, consume it immediately instead of blocking on a second REST provider.
    # The gate is deliberately conservative and never mixes related/future-option chains.
    if TASTYTRADE.configured:
        try:
            hot,hot_meta=_tastytrade_own_chain_snapshot(symbol,strike_window,expiry_days)
            hm=dict(hot_meta or {});age=_finite(hm.get("freshest_event_age_seconds"),999999.0) or 999999.0
            if len(hot)>=12 and int(hm.get("unique_strikes") or 0)>=4 and age<=180.0:
                hm["structural_route"]="TASTYTRADE_DXLINK_HOT_FIRST_CLASS"
                hm["parallel_provider_policy"]="NO_FIXED_RANK · USE_FRESHEST_OWN_INSTRUMENT_READY_SOURCE"
                hm["configured_quant_provider"]=provider
                return hot,hm
        except Exception as _e:
            # Hot-first is opportunistic. Startup hydration / no-own-price are expected
            # misses; the primary provider route below remains authoritative.
            if not is_transient_chain_failure(_e):
                _obs_note('service:hot_chain_probe', _e)
    try:
        if provider=="TASTYTRADE":
            return _tastytrade_own_chain_snapshot(symbol,strike_window,expiry_days)
        return fetch_alpaca_options_snapshot(symbol,strike_window,expiry_days)
    except Exception as exc:
        primary_error=exc
    # Resilience path: use only OWN-instrument tastytrade contracts.  If Alpaca failed
    # transiently while DXLink is still hydrating, wait a bounded period for the in-memory
    # chain instead of declaring the whole Quant engine degraded at BOOT 30%.
    try:
        if provider!="TASTYTRADE" and is_transient_chain_failure(primary_error):
            frame,meta=_wait_tastytrade_own_chain(symbol,strike_window,expiry_days)
        else:
            frame,meta=_tastytrade_own_chain_snapshot(symbol,strike_window,expiry_days)
        meta=dict(meta or {});meta["structural_failover_from"]=provider;meta["primary_error"]=f"{type(primary_error).__name__}: {str(primary_error)[:160]}"
        return frame,meta
    except Exception as fallback_exc:
        prefix="CHAIN_WARMING: " if is_transient_chain_failure(primary_error) or is_transient_chain_failure(fallback_exc) else ""
        raise RuntimeError(
            f"{prefix}{provider} own-chain unavailable ({type(primary_error).__name__}: {str(primary_error)[:120]}); "
            f"tastytrade own-chain failover unavailable ({type(fallback_exc).__name__}: {str(fallback_exc)[:120]})"
        ) from primary_error


def _fetch_asset_quant_snapshot(symbol: str, strike_window: float, expiry_days: int):
    return fetch_asset_options_snapshot(symbol, strike_window, expiry_days)


def _live_ticks_for(symbol: str) -> pd.DataFrame:
    """Canonical LIVE tape selected from all observed price providers.

    No instrument is permanently pinned to one provider. The provider-neutral fabric uses
    observed freshness/activity and trade quality with hysteresis; provider-specific lanes
    remain isolated for failover/diagnostics and are never blindly added together.
    """
    sym=str(symbol or "").upper().strip()
    df=PRICE_TICK_FABRIC.dataframe(sym)
    if isinstance(df,pd.DataFrame) and not df.empty:
        return df
    # Cold-start compatibility only: before a provider has published its first fabric event,
    # reuse the already-running native provider buffer. This path disappears as soon as the
    # first observation reaches the shared fabric.
    cfg=asset_info(sym);provider=str(cfg.get("market_provider") or cfg.get("quant_provider") or "ALPACA").upper()
    if provider=="TASTYTRADE" and TASTYTRADE.configured:
        tf=PRICE_TICK_FABRIC.dataframe(sym)
        if isinstance(tf,pd.DataFrame) and not tf.empty:return tf
    return PRICE_STREAM.dataframe(sym)

def _finite(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _freshest_observed_underlying_clock(symbol: str, fallback: Any = None) -> tuple[Any, str]:
    """Return the freshest *observed* price-event timestamp for ``symbol``.

    The structural option snapshot and the LIVE underlying do not share a refresh
    cadence.  In PREMARKET the option chain is intentionally refreshed relatively
    slowly, while SIP/DXLink quotes and trades remain event-driven.  Using the
    structural snapshot's stock timestamp as the LIVE freshness clock therefore
    creates a false 20s stale condition even when a newer quote/trade is already in
    memory.

    Only provider events with a valid event-time and an actual price/quote are
    eligible.  Receive-time proxies are deliberately rejected: this helper must not
    manufacture freshness.
    """
    sym = str(symbol or "").upper().strip()
    candidates: list[tuple[float, Any, str]] = []

    def add(value: Any, source: str) -> None:
        epoch = to_epoch(value)
        if epoch is not None:
            candidates.append((float(epoch), value, str(source)))

    add(fallback, "STRUCTURAL_SNAPSHOT")

    # Provider-neutral bus includes both QUOTE and TRADE events.  This matters for
    # premarket, where quotes can continue updating while prints are sparse.
    try:
        for event in PROVIDER_BUS.latest_events(sym):
            if not isinstance(event, dict) or event.get("event_time_valid") is not True:
                continue
            et = str(event.get("event_type") or "").upper()
            if et not in {"QUOTE", "TRADE", "SNAPSHOT", "BAR", "CANDLE"}:
                continue
            vals = event.get("values") or {}
            price = _finite(vals.get("price"))
            bid = _finite(vals.get("bid")); ask = _finite(vals.get("ask"))
            has_price = price is not None and price > 0
            has_quote = bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid
            if not (has_price or has_quote):
                continue
            add(event.get("timestamp"), f"PROVIDER_BUS:{event.get('source') or 'UNKNOWN'}:{et}")
    except Exception as exc:
        _obs_note("service:freshest_underlying_provider_bus", exc, severity="DEGRADED")

    # The price fabric is the low-latency canonical trade lane.  It is a second
    # independent path and covers providers that have not populated ProviderBus yet.
    try:
        status = PRICE_TICK_FABRIC.scheduler_status(sym) or {}
        tick = status.get("last_tick") or {}
        if tick and tick.get("event_time_valid", True) is True and _finite(tick.get("price")) not in (None, 0):
            add(tick.get("timestamp"), f"PRICE_TICK_FABRIC:{status.get('source') or tick.get('source') or 'UNKNOWN'}")
    except Exception as exc:
        _obs_note("service:freshest_underlying_price_fabric", exc, severity="DEGRADED")

    if not candidates:
        return fallback, "STRUCTURAL_SNAPSHOT"
    _, value, source = max(candidates, key=lambda item: item[0])
    return value, source


def _quality_meta_with_live_underlying(meta: Dict[str, Any] | None, symbol: str) -> Dict[str, Any]:
    """Copy provider metadata and bind freshness to the freshest observed price clock.

    The structural timestamp remains visible for audit.  Only the freshness input is
    replaced, and only when a newer *observed* event exists.
    """
    out = dict(meta or {})
    original = out.get("stock_market_timestamp")
    freshest, source = _freshest_observed_underlying_clock(symbol, original)
    if freshest is not None:
        out["stock_market_timestamp"] = freshest
    out["structural_stock_market_timestamp"] = original
    out["underlying_freshness_source"] = source
    return out


def _jsonable(v):
    """Return a strict-JSON-safe representation.

    Starlette/JSONResponse rejects NaN/Infinity. Multi-asset chains can legitimately
    contain missing metrics (for example sparse SPY IV/Greeks fields), so every API
    payload must convert non-finite scalars to null instead of leaking NaN.
    """
    if v is None:
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (np.integer, int)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, (np.floating, float)):
        x = float(v)
        return x if math.isfinite(x) else None
    if v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (pd.Timestamp, datetime, date, time)):
        try:
            return v.isoformat()
        except Exception:
            return str(v)
    if isinstance(v, np.ndarray):
        return [_jsonable(x) for x in v.tolist()]
    if isinstance(v, pd.Series):
        return [_jsonable(x) for x in v.tolist()]
    if isinstance(v, pd.DataFrame):
        return [_jsonable(x) for x in v.to_dict("records")]
    if isinstance(v, dict):
        return {str(k): _jsonable(val) for k, val in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    # Catch scalar missing values from pandas/NumPy extension dtypes.
    try:
        missing = pd.isna(v)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
    except Exception as _e:
        _obs_note('service:296', _e)
    return v


def _decode_plotly_typed_array(value: Any) -> Any:
    """Decode Plotly 6 binary JSON arrays into ordinary JSON lists.

    Plotly 6 serializes NumPy arrays as {dtype,bdata,shape}.  ITM QUANT's
    native Canvas renderers require ordinary arrays, so this is the canonical
    server-side compatibility boundary for every chart.
    """
    if isinstance(value, dict) and "dtype" in value and "bdata" in value:
        try:
            dtype_map = {
                "i1": np.dtype("<i1"), "u1": np.dtype("<u1"),
                "i2": np.dtype("<i2"), "u2": np.dtype("<u2"),
                "i4": np.dtype("<i4"), "u4": np.dtype("<u4"),
                "i8": np.dtype("<i8"), "u8": np.dtype("<u8"),
                "f4": np.dtype("<f4"), "f8": np.dtype("<f8"),
            }
            dt = dtype_map.get(str(value.get("dtype") or "").lower())
            if dt is None:
                return value
            raw = base64.b64decode(value.get("bdata") or "", validate=False)
            arr = np.frombuffer(raw, dtype=dt)
            shape = value.get("shape")
            if shape:
                dims = tuple(int(x.strip()) for x in str(shape).split(",") if x.strip())
                if dims and int(np.prod(dims)) == int(arr.size):
                    arr = arr.reshape(dims)
            return arr.tolist()
        except Exception:
            return value
    if isinstance(value, dict):
        return {str(k): _decode_plotly_typed_array(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_plotly_typed_array(v) for v in value]
    return value


#: El ticker que las figuras heredadas llevan escrito en sus rotulos. Se
#: sustituye por el activo activo al serializar. Vive aqui, con nombre, en vez
#: de repartido por el codigo como una constante anonima.
_LEGACY_FIGURE_TICKER = "DIA"
#: Con BORDE DE PALABRA. `str.replace` a secas convertia «MEDIA» en «MQQQ» y
#: «DIARIO» en «QQQRIO»: «DIA» es subcadena de palabras corrientes en espanol, y
#: ese reemplazo corrompia rotulos en todos los activos MENOS en el que llevaba
#: el ticker escrito, que era justo el unico que se probaba.
_LEGACY_TICKER_RE = re.compile(rf"\b{_LEGACY_FIGURE_TICKER}\b")


def _fig_json(fig: go.Figure, symbol: str = ""):
    """Serializa una figura heredada con el ticker del activo ACTIVO.

    v1.55.0 · Sin `if sym == "DIA"`. La rama existia como atajo y era la unica
    comparacion contra un ticker que quedaba en produccion; sustituir DIA por
    DIA no hace nada, asi que el atajo solo servia para tener una rama por
    ticker. El borde de palabra hace el resto del trabajo.
    """
    # Decode Plotly 6 typed-array JSON before it reaches native Canvas renderers.
    data=_decode_plotly_typed_array(json.loads(fig.to_json()))
    sym=str(symbol or "").upper()
    if not sym:
        return data
    def walk(v):
        if isinstance(v,dict): return {k:walk(x) for k,x in v.items()}
        if isinstance(v,list): return [walk(x) for x in v]
        if isinstance(v,str): return _LEGACY_TICKER_RE.sub(sym,v)
        return v
    return walk(data)



def _quantdata_options_values(symbol: str) -> Dict[str, Any]:
    """Read the freshest Quant Data OPTIONS_INTELLIGENCE slot without network I/O."""
    snap=FEATURE_BUS.snapshot(str(symbol or "").upper()) or {}
    rows=[]
    for row in snap.get("providers") or []:
        if str(row.get("source") or "").upper()!="QUANTDATA":
            continue
        if str(row.get("feature_group") or "").upper()!="OPTIONS_INTELLIGENCE":
            continue
        rows.append(row)
    if not rows:
        return {}
    usable=[r for r in rows if not r.get("stale") and float(((r.get("quality") or {}).get("quality_score") or 0))>=50]
    row=usable[-1] if usable else rows[-1]
    values=dict(row.get("values") or {})
    values["_provider_stale"]=bool(row.get("stale"))
    values["_provider_age_ms"]=row.get("age_ms")
    values["_provider_quality"]=((row.get("quality") or {}).get("quality_score"))
    return values

def _demo_history(symbol: str = "DIA") -> pd.DataFrame:
    src = Path(__file__).resolve().parent / "data" / "sample_options.csv"
    base = pd.read_csv(src)
    base["timestamp"] = pd.to_datetime(base["timestamp"])
    # sample_options.csv contains a historical sequence; v1.11 uses one option row per
    # strike/type as the template for each synthetic snapshot to avoid duplicate contracts.
    base = base.sort_values("timestamp").groupby(["strike","option_type"], as_index=False).tail(1).reset_index(drop=True)
    symbol=str(symbol).upper()
    demo_spots={"DIA":534.0,"QQQ":610.0,"TQQQ":55.0,"SPY":690.0,"GLD":365.0,"GDX":52.0,"AAPL":245.0,"VXX":38.0}
    target=demo_spots.get(symbol,534.0)
    old=float(pd.to_numeric(base["underlying_price"],errors="coerce").median())
    shift=target-old
    base["underlying_price"]=pd.to_numeric(base["underlying_price"],errors="coerce")+shift
    base["strike"]=pd.to_numeric(base["strike"],errors="coerce")+shift
    base["underlying_symbol"]=symbol
    start = datetime.now(EC).replace(hour=9, minute=30, second=0, microsecond=0, tzinfo=None)
    frames = []
    # DEMO v1.11 emulates a real multi-expiry chain so every Expiry Window can be tested offline.
    today = datetime.now(EC).date()
    friday = today + timedelta(days=(4 - today.weekday()) % 7)
    demo_dates = sorted(set([today, min(today + timedelta(days=2), friday), friday, friday + timedelta(days=7), today + timedelta(days=20)]))
    demo_dates = [d for d in demo_dates if d >= today]
    for i in range(10):
        for j, exp_date in enumerate(demo_dates):
            x = base.copy()
            x["timestamp"] = start + timedelta(minutes=i * 5)
            drift = (i - 4) * 0.06 + math.sin(i / 2) * 0.08
            x["underlying_price"] = pd.to_numeric(x["underlying_price"], errors="coerce") + drift
            x["iv"] = pd.to_numeric(x["iv"], errors="coerce") * (1 + 0.002 * (i - 5)) * (1 + 0.012*j)
            activity = max(0.45, 1.0 - 0.11*j)
            x["open_interest"] = (pd.to_numeric(x["open_interest"], errors="coerce").fillna(0) * (1 + 0.18*j)).round().clip(lower=0)
            x["volume"] = (pd.to_numeric(x["volume"], errors="coerce").fillna(0) * (1 + 0.09 * i) * activity).round().clip(lower=0)
            x["expiration_date"] = exp_date.isoformat()
            x["dte"] = max((exp_date - today).days, 0) + 0.25
            # Synthetic but internally consistent quote fields let DEMO exercise the same
            # Data Quality / straddle / fallback paths as LIVE without pretending to be market data.
            mi=market_inputs(symbol); mids=[]
            for rr in x.itertuples(index=False):
                try:
                    mids.append(black_scholes_price(float(rr.underlying_price),float(rr.strike),year_fraction(float(rr.dte)),float(rr.iv),str(rr.option_type),float(mi["risk_free_rate"]),float(mi["dividend_yield"])))
                except Exception: mids.append(float("nan"))
            x["last"]=mids;x["bid"]=[max(0.01,m*0.985) if math.isfinite(m) else np.nan for m in mids];x["ask"]=[max(0.02,m*1.015) if math.isfinite(m) else np.nan for m in mids]
            x["iv_source"]="DEMO";x["greeks_source"]="ITM_QUANT DEMO"
            frames.append(x)
    return pd.concat(frames, ignore_index=True)


def _latest_snapshot(history: pd.DataFrame) -> pd.DataFrame:
    h = history.copy()
    h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
    h = h.dropna(subset=["timestamp"])
    if h.empty:
        return h
    ts = h["timestamp"].max()
    return h[h["timestamp"] == ts].copy()


def _trim_history(history: pd.DataFrame, max_snapshots: int = HOT_SNAPSHOTS) -> pd.DataFrame:
    if history.empty:
        return history
    h = history.copy()
    h["timestamp"] = pd.to_datetime(h["timestamp"], errors="coerce")
    ts = sorted(h["timestamp"].dropna().unique())[-max_snapshots:]
    return h[h["timestamp"].isin(ts)].copy()


def _direction_sign(label: str) -> int:
    label = str(label).upper()
    if label in {"UP", "BUY", "BULLISH", "ALIGNED BULLISH"}:
        return 1
    if label in {"DOWN", "SELL", "BEARISH", "ALIGNED BEARISH"}:
        return -1
    return 0


def _current_structure_frame(result: Dict[str, Any]) -> pd.DataFrame:
    """Non-empty current structural frame, Delta-enriched when available."""
    for key in ("current_delta", "current"):
        frame = result.get(key, pd.DataFrame()) if isinstance(result, dict) else pd.DataFrame()
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            return frame.copy()
    return pd.DataFrame()


def _level_targets(result: Dict[str, Any]) -> Dict[str, Any]:
    cur = _current_structure_frame(result)
    spot = float(result.get("spot", np.nan))
    if cur.empty or not math.isfinite(spot):
        return {"active": None, "next": None, "extension": None, "low": None, "pivot": None, "high": None}
    score_col = "gamma_delta_level_score" if "gamma_delta_level_score" in cur.columns else "dominance_score"
    state_col = "state_delta" if "state_delta" in cur.columns else "state"
    conf_col = "state_confidence_delta" if "state_confidence_delta" in cur.columns else "state_confidence"
    cur["dist"] = (pd.to_numeric(cur["strike"], errors="coerce") - spot).abs()
    ranked = cur.sort_values(["dist", score_col], ascending=[True, False]).copy()
    active = ranked.iloc[0]
    dir_label = result.get("delta_pressure_direction", "NEUTRAL")
    if dir_label == "NEUTRAL":
        dir_label = result.get("pressure_direction", "NEUTRAL")
    sign = _direction_sign(dir_label)
    candidates = cur[pd.to_numeric(cur["strike"], errors="coerce") > spot] if sign >= 0 else cur[pd.to_numeric(cur["strike"], errors="coerce") < spot]
    candidates = candidates.sort_values("strike", ascending=(sign >= 0))
    # Prefer structurally relevant levels; fall back to nearest.
    strong = candidates[pd.to_numeric(candidates[score_col], errors="coerce").fillna(0) >= 45]
    use = strong if not strong.empty else candidates
    nxt = use.iloc[0] if len(use) else None
    ext = use.iloc[1] if len(use) > 1 else None
    below = cur[pd.to_numeric(cur["strike"], errors="coerce") < spot].sort_values(score_col, ascending=False)
    above = cur[pd.to_numeric(cur["strike"], errors="coerce") > spot].sort_values(score_col, ascending=False)
    low = float(below.iloc[0]["strike"]) if len(below) else None
    high = float(above.iloc[0]["strike"]) if len(above) else None
    pivot = float(active["strike"])
    return {
        "active": {"strike": float(active["strike"]), "state": str(active.get(state_col, "")), "confidence": float(active.get(conf_col, 0)), "score": float(active.get(score_col, 0))},
        "next": None if nxt is None else float(nxt["strike"]),
        "extension": None if ext is None else float(ext["strike"]),
        "low": low, "pivot": pivot, "high": high,
    }


def _greeks_diagnostics(result: Dict[str, Any]) -> Dict[str, Any]:
    enr = result.get("enriched", pd.DataFrame()).copy() if isinstance(result, dict) else pd.DataFrame()
    if enr.empty:
        return {}
    enr["timestamp"] = pd.to_datetime(enr.get("timestamp"), errors="coerce")
    enr = enr.dropna(subset=["timestamp"])
    if enr.empty:
        return {}
    x = enr[enr["timestamp"] == enr["timestamp"].max()].copy()
    mix = x.get("greeks_source", pd.Series(["UNKNOWN"] * len(x))).fillna("UNKNOWN").astype(str).value_counts().to_dict()
    def mean_abs(col):
        v = pd.to_numeric(x.get(col, pd.Series(dtype=float)), errors="coerce").abs().dropna()
        return float(v.mean()) if len(v) else float("nan")
    spot = float(numeric_column(x,"underlying_price",float("nan")).dropna().iloc[-1])
    oi = numeric_column(x,"open_interest",0)
    x["_vanna_mass"] = numeric_column(x,"calc_vanna",0).abs() * oi
    x["_charm_mass"] = numeric_column(x,"calc_charm",0).abs() * oi
    x["_speed_mass"] = numeric_column(x,"calc_speed",0).abs() * oi
    def top(col):
        if not len(x): return None
        a = x.groupby("strike", as_index=False)[col].sum().sort_values(col, ascending=False)
        if a.empty: return None
        r = a.iloc[0]
        return {"strike": float(r["strike"]), "mass": float(r[col])}
    pgap = pd.to_numeric(x.get("provider_gamma_gap_pct", pd.Series(dtype=float)), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    dgap = pd.to_numeric(x.get("provider_delta_gap", pd.Series(dtype=float)), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    return {
        "source_mix": {str(k): int(v) for k, v in mix.items()},
        "mean_abs_provider_delta_gap": float(dgap.abs().mean()) if len(dgap) else float("nan"),
        "median_abs_provider_gamma_gap_pct": float(pgap.abs().median()) if len(pgap) else float("nan"),
        "top_vanna": top("_vanna_mass"), "top_charm": top("_charm_mass"), "top_speed": top("_speed_mass"),
        "spot": spot,
        "note": "Vanna/Charm/Speed son sensibilidades del modelo ITM QUANT. La divergencia contra Greeks del proveedor es diagnóstico, no una señal direccional por sí sola."
    }


def _trace_anchors(symbol: str, expiry_window: str | None = None) -> dict:
    """Historical scale anchors for the TRACE heatmap, or {} until enough sessions."""
    try:
        from app.core.scale_anchors import load_anchors
        return load_anchors(alpaca_data.DATA_DIR, symbol, expiry_window) or {}
    except Exception:
        return {}


def _bars_from_ticks(ticks: pd.DataFrame | None, rule: str = "1min") -> pd.DataFrame | None:
    """Resample the SIP trade tape into OHLC bars.

    The Parkinson high/low estimator needs a range, which ticks provide naturally once
    bucketed. Falls back to None so the caller can use the close-to-close path.
    """
    if not isinstance(ticks, pd.DataFrame) or ticks.empty or "price" not in ticks.columns:
        return None
    try:
        t = ticks.copy()
        t["timestamp"] = pd.to_datetime(t.get("timestamp"), errors="coerce")
        t["price"] = pd.to_numeric(t["price"], errors="coerce")
        t = t.dropna(subset=["timestamp", "price"]).sort_values("timestamp")
        if len(t) < 30:
            return None
        bars = t.set_index("timestamp")["price"].resample(rule).ohlc().dropna()
        if bars.empty:
            return None
        return bars.reset_index()
    except Exception:
        return None


def _market_inputs_safe(symbol: str, dte: float | None = None) -> Dict[str, Any]:
    """market_inputs() that never raises inside a metrics path."""
    try:
        from app.core.precision_engine import market_inputs
        return market_inputs(symbol, dte)
    except Exception:
        return {"risk_free_rate": 0.045, "dividend_yield": 0.0}


def _volatility_metrics(result: Dict[str, Any], price_bars: pd.DataFrame | None = None) -> Dict[str, Any]:
    enr = result.get("enriched", pd.DataFrame()).copy()
    if enr.empty:
        return {}
    enr["timestamp"] = pd.to_datetime(enr["timestamp"], errors="coerce")
    latest_ts = enr["timestamp"].max()
    x = enr[enr["timestamp"] == latest_ts].copy()
    spot = float(result.get("spot", x["underlying_price"].iloc[-1]))
    if "expiration_date" not in x.columns:
        x["expiration_date"] = (pd.Timestamp(latest_ts) + pd.to_timedelta(pd.to_numeric(x["dte"], errors="coerce"), unit="D")).dt.date.astype(str)
    x["abs_delta"] = pd.to_numeric(x["calc_delta"], errors="coerce").abs()
    x["dist"] = (pd.to_numeric(x["strike"], errors="coerce") - spot).abs()
    nearest_exp = x.sort_values("dte")["expiration_date"].iloc[0]
    nx = x[x["expiration_date"] == nearest_exp].copy()
    atm = nx.sort_values("dist").head(4)
    atm_iv = float(pd.to_numeric(atm["iv"], errors="coerce").mean())
    dte = float(pd.to_numeric(nx["dte"], errors="coerce").median())
    model_expected_move = spot * atm_iv * math.sqrt(year_fraction(dte))

    # Market-implied move: ATM call + put mid for the nearest expiry. This is deliberately
    # shown separately from the model IV move rather than silently replacing it.
    market_implied_move = float("nan")
    straddle_strike = float("nan")
    if {"bid","ask"}.issubset(nx.columns):
        bx=pd.to_numeric(nx["bid"],errors="coerce"); ax=pd.to_numeric(nx["ask"],errors="coerce")
        nx["mid"] = ((bx+ax)/2.0).where(bx.gt(0)&ax.ge(bx))
        strikes=sorted(nx["strike"].dropna().astype(float).unique(), key=lambda k:abs(k-spot))
        for k in strikes:
            z=nx[np.isclose(pd.to_numeric(nx["strike"],errors="coerce"),k)]
            c=z[z["option_type"].astype(str).str.lower().str.startswith("c")]
            p=z[z["option_type"].astype(str).str.lower().str.startswith("p")]
            if len(c) and len(p):
                cm=float(pd.to_numeric(c["mid"],errors="coerce").dropna().iloc[0]) if pd.to_numeric(c["mid"],errors="coerce").notna().any() else float("nan")
                pm=float(pd.to_numeric(p["mid"],errors="coerce").dropna().iloc[0]) if pd.to_numeric(p["mid"],errors="coerce").notna().any() else float("nan")
                if math.isfinite(cm) and math.isfinite(pm) and cm+pm>0:
                    market_implied_move=cm+pm;straddle_strike=float(k);break

    skew_rows=[]
    for exp,g in x.groupby("expiration_date"):
        calls=g[g["option_type"].astype(str).str.lower().str.startswith("c")].copy()
        puts=g[g["option_type"].astype(str).str.lower().str.startswith("p")].copy()
        call25=calls.iloc[(calls["abs_delta"]-0.25).abs().argsort()[:max(1,min(3,len(calls)))]] if len(calls) else pd.DataFrame()
        put25=puts.iloc[(puts["abs_delta"]-0.25).abs().argsort()[:max(1,min(3,len(puts)))]] if len(puts) else pd.DataFrame()
        civ=float(pd.to_numeric(call25.get("iv",pd.Series(dtype=float)),errors="coerce").mean()) if len(call25) else float("nan")
        piv=float(pd.to_numeric(put25.get("iv",pd.Series(dtype=float)),errors="coerce").mean()) if len(put25) else float("nan")
        sk=(piv-civ)*100 if math.isfinite(civ) and math.isfinite(piv) else float("nan")
        skew_rows.append({"expiration_date":str(exp),"dte":float(pd.to_numeric(g["dte"],errors="coerce").median()),"call25_iv":civ*100 if math.isfinite(civ) else float("nan"),"put25_iv":piv*100 if math.isfinite(piv) else float("nan"),"skew_25d":sk})
    skew_rows=sorted(skew_rows,key=lambda r:r["dte"])
    skew=next((r["skew_25d"] for r in skew_rows if math.isfinite(r["skew_25d"])),float("nan"))

    timestamps = sorted(enr["timestamp"].dropna().unique())
    # v1.47.0 · Sin dos observaciones NO hay cambio que medir, y eso no es un
    # cambio de cero. Publicar `0.0` hacía que la terminal mostrara «DERIVA DE
    # VOLATILIDAD +0.000 pp» mientras el panel de debajo decía «NO DISPONIBLE»:
    # dos afirmaciones contradictorias sobre el mismo dato, y la de arriba era
    # falsa. `nan` viaja como `None` y la interfaz escribe «—».
    iv_change = float("nan")
    if len(timestamps) > 1:
        prev = enr[enr["timestamp"] == timestamps[-2]].copy()
        prev_iv = float(pd.to_numeric(prev["iv"], errors="coerce").mean())
        now_iv = float(pd.to_numeric(x["iv"], errors="coerce").mean())
        iv_change = (now_iv - prev_iv) * 100
        if "expiration_date" not in prev.columns:
            prev["expiration_date"] = (pd.Timestamp(timestamps[-2]) + pd.to_timedelta(pd.to_numeric(prev["dte"], errors="coerce"), unit="D")).dt.date.astype(str)
        prev["abs_delta"] = numeric_column(prev,"calc_delta",float("nan")).abs()
        prev_skew={}
        for exp,g in prev.groupby("expiration_date"):
            calls=g[g["option_type"].astype(str).str.lower().str.startswith("c")].copy(); puts=g[g["option_type"].astype(str).str.lower().str.startswith("p")].copy()
            c25=calls.iloc[(calls["abs_delta"]-.25).abs().argsort()[:max(1,min(3,len(calls)))]] if len(calls) else pd.DataFrame(); p25=puts.iloc[(puts["abs_delta"]-.25).abs().argsort()[:max(1,min(3,len(puts)))]] if len(puts) else pd.DataFrame()
            civ=float(pd.to_numeric(c25.get("iv",pd.Series(dtype=float)),errors="coerce").mean()) if len(c25) else float("nan"); piv=float(pd.to_numeric(p25.get("iv",pd.Series(dtype=float)),errors="coerce").mean()) if len(p25) else float("nan")
            if math.isfinite(civ) and math.isfinite(piv): prev_skew[str(exp)]=(piv-civ)*100
        for row in skew_rows:
            old=prev_skew.get(str(row.get("expiration_date"))); cur=float(row.get("skew_25d",float("nan")))
            row["skew_migration_pp"]=(cur-old) if old is not None and math.isfinite(cur) else float("nan")
    else:
        for row in skew_rows: row["skew_migration_pp"]=float("nan")
    # El régimen se conserva en su vocabulario actual —lo consumen el motor de
    # escenarios y el de régimen— pero se declara si está MEDIDO o es el valor
    # por defecto de una sesión sin historia todavía. Cambiar la cadena habría
    # movido puntuaciones aguas abajo sin que nadie lo pidiera.
    iv_change_measured = math.isfinite(iv_change)
    regime = ("EXPANSION" if iv_change > 0.15 else "COMPRESSION" if iv_change < -0.15
              else "STABLE")
    # v1.15 TERM STRUCTURE.
    # Was: mean IV across every strike of the expiry. That mixes the level of
    # volatility with the shape of the smile and with how many wing strikes each
    # expiry happens to list, so CONTANGO/BACKWARDATION could be driven purely by
    # differences in strike coverage. We now use forward-ATM IV per expiry:
    # K* = S*exp((r-q)T), averaging the few contracts closest to that strike.
    _mi_term = _market_inputs_safe(str(result.get("symbol", "")) or "DIA")
    _r_t = float(_mi_term.get("risk_free_rate", 0.045)); _q_t = float(_mi_term.get("dividend_yield", 0.0))
    _term_rows = []
    for _exp, _g in x.groupby("expiration_date"):
        _dte = float(pd.to_numeric(_g["dte"], errors="coerce").median())
        _T = year_fraction(_dte)
        _fwd = spot * math.exp((_r_t - _q_t) * _T)
        _gg = _g.assign(_d=(pd.to_numeric(_g["strike"], errors="coerce") - _fwd).abs()).sort_values("_d")
        _atm = _gg.head(4)
        _iv = float(pd.to_numeric(_atm["iv"], errors="coerce").mean())
        _term_rows.append({"expiration_date": str(_exp), "iv": _iv, "dte": _dte,
                           "forward_strike": float(_fwd), "mean_iv_all_strikes": float(pd.to_numeric(_g["iv"], errors="coerce").mean())})
    term = pd.DataFrame(_term_rows).sort_values("dte").reset_index(drop=True)
    dislocation = ((market_implied_move/model_expected_move)-1.0)*100 if math.isfinite(market_implied_move) and model_expected_move>0 else float("nan")
    iv_mix={}
    if "iv_source" in x.columns:
        vc=x["iv_source"].fillna("UNKNOWN").astype(str).value_counts();iv_mix={str(k):int(v) for k,v in vc.items()}
    # Realized-volatility diagnostic from unique structural snapshot spots. This is not
    # a high-frequency realized-vol estimator; it uses the snapshots actually stored by ITM.
    # v1.15 REALIZED VOL.
    # The old version accepted 5 log-returns. The standard error of a volatility
    # estimate from n returns is roughly sigma/sqrt(2n): with n=5 a true 20% vol is
    # estimated anywhere between 8% and 31% at 90% confidence, so IV-RV was dominated
    # by estimation noise rather than signal. We now require a real sample and prefer
    # OHLC bars, where the Parkinson range estimator is several times more efficient
    # than the standard deviation of closes.
    RV_MIN_RETURNS=60
    rv=float("nan");vol_of_vol=float("nan");rv_n=0;rv_method="UNAVAILABLE";rv_se_pct=float("nan")
    try:
        if isinstance(price_bars,pd.DataFrame) and not price_bars.empty and {"high","low"}.issubset(price_bars.columns):
            b=price_bars.copy()
            hi_=pd.to_numeric(b["high"],errors="coerce");lo_=pd.to_numeric(b["low"],errors="coerce")
            ok=hi_.gt(0)&lo_.gt(0)&hi_.ge(lo_)
            hl=np.log((hi_[ok]/lo_[ok]).to_numpy(float))
            if len(hl)>=RV_MIN_RETURNS:
                bt=pd.to_datetime(b.loc[ok,"timestamp"],errors="coerce") if "timestamp" in b.columns else None
                med=float(bt.diff().dt.total_seconds().dropna().median()) if bt is not None and bt.notna().sum()>1 else 60.0
                per_year=max(1.0,252*6.5*3600/max(med,1.0))
                # Parkinson: var_per_bar = mean(ln(H/L)^2) / (4 ln 2)
                var_bar=float(np.mean(hl**2)/(4.0*math.log(2.0)))
                rv=float(math.sqrt(max(var_bar,0.0)*per_year)*100);rv_n=int(len(hl))
                rv_method=f"PARKINSON · {rv_n} barras"
        if not math.isfinite(rv):
            spath=enr.groupby("timestamp",as_index=False)["underlying_price"].median().dropna().sort_values("timestamp")
            rr=np.log(pd.to_numeric(spath["underlying_price"],errors="coerce")).diff().dropna()
            if len(rr)>=RV_MIN_RETURNS:
                dt=pd.to_datetime(spath["timestamp"]).diff().dt.total_seconds().dropna()
                med=float(dt[dt>0].median()) if (dt>0).any() else 15.0
                annual=max(1.0,252*6.5*3600/max(med,1.0))
                rv=float(rr.std(ddof=1)*math.sqrt(annual)*100);rv_n=int(len(rr))
                rv_method=f"CLOSE-TO-CLOSE · {rv_n} retornos"
            elif len(rr):
                rv_method=f"INSUFICIENTE · {len(rr)}/{RV_MIN_RETURNS} retornos"
        if rv_n>0:
            # Parkinson is ~5x more efficient per observation than close-to-close.
            eff=5.0 if rv_method.startswith("PARKINSON") else 1.0
            rv_se_pct=float(100.0/math.sqrt(2.0*rv_n*eff))
        ivpath=enr.groupby("timestamp",as_index=False)["iv"].mean().sort_values("timestamp")
        ivret=pd.to_numeric(ivpath["iv"],errors="coerce").diff().dropna()*100
        if len(ivret)>=20:vol_of_vol=float(ivret.std(ddof=1))
    except Exception as _e:
        _obs_note('service:676', _e)
    iv_rv_spread=(atm_iv*100-rv) if math.isfinite(rv) else float("nan")
    term_state="FLAT";forward_vol=float("nan");front_forward_spread=float("nan")
    try:
        if len(term)>=2:
            first=float(term.iloc[0]["iv"]);last=float(term.iloc[-1]["iv"]);term_state="CONTANGO" if last>first+.005 else "BACKWARDATION" if first>last+.005 else "FLAT"
            t1=year_fraction(float(term.iloc[0]["dte"]));t2=year_fraction(float(term.iloc[1]["dte"]));iv1=float(term.iloc[0]["iv"]);iv2=float(term.iloc[1]["iv"])
            if t2>t1:
                fv=(iv2*iv2*t2-iv1*iv1*t1)/(t2-t1)
                if fv>0: forward_vol=math.sqrt(fv)*100;front_forward_spread=iv1*100-forward_vol
    except Exception as _e:
        _obs_note('service:686', _e)
    return {
        "atm_iv": atm_iv * 100,
        "nearest_expiry": str(nearest_exp),
        "nearest_dte": float(dte) if math.isfinite(dte) else None,
        "expected_move": model_expected_move,
        "model_expected_move": model_expected_move,
        "market_implied_move": market_implied_move,
        "straddle_strike": straddle_strike,
        "expected_move_dislocation_pct": dislocation,
        "expected_low": spot - model_expected_move,
        "expected_high": spot + model_expected_move,
        "skew_25d": skew,
        "skew_by_expiry": skew_rows,
        "iv_change_pp": iv_change,
        "iv_change_measured": iv_change_measured,
        "regime": regime,
        "regime_measured": iv_change_measured,
        "term_structure": term.to_dict("records"),
        "term_structure_state": term_state,
        "forward_volatility_pct": forward_vol,
        "front_vs_forward_vol_pp": front_forward_spread,
        "event_premium_proxy_pp": front_forward_spread,
        "realized_volatility_pct": rv,
        "realized_vol_method": rv_method,
        "realized_vol_samples": rv_n,
        "realized_vol_rel_std_error_pct": None if not math.isfinite(rv_se_pct) else round(rv_se_pct, 1),
        "iv_minus_rv_pp": iv_rv_spread,
        "vol_of_vol_pp": vol_of_vol,
        "iv_source_mix": iv_mix,
    }


def _positioning_metrics(result: Dict[str, Any]) -> Dict[str, Any]:
    enr = result.get("enriched", pd.DataFrame()).copy()
    if enr.empty:
        return {}
    enr["timestamp"] = pd.to_datetime(enr["timestamp"], errors="coerce")
    x = enr[enr["timestamp"] == enr["timestamp"].max()].copy()
    is_call = x["option_type"].astype(str).str.lower().str.startswith("c")
    call_oi = float(pd.to_numeric(x.loc[is_call, "open_interest"], errors="coerce").sum())
    put_oi = float(pd.to_numeric(x.loc[~is_call, "open_interest"], errors="coerce").sum())
    call_vol = float(pd.to_numeric(x.loc[is_call, "volume"], errors="coerce").sum())
    put_vol = float(pd.to_numeric(x.loc[~is_call, "volume"], errors="coerce").sum())
    by = x.groupby(["strike", "option_type"], as_index=False).agg(oi=("open_interest", "sum"), volume=("volume", "sum"))
    call_rows = by[by["option_type"].astype(str).str.lower().str.startswith("c")]
    put_rows = by[by["option_type"].astype(str).str.lower().str.startswith("p")]
    top_call = call_rows.sort_values("oi", ascending=False).head(1)
    top_put = put_rows.sort_values("oi", ascending=False).head(1)
    return {
        "call_oi": call_oi, "put_oi": put_oi, "put_call_oi_ratio": put_oi / max(call_oi, 1.0),
        "call_volume": call_vol, "put_volume": put_vol, "put_call_volume_ratio": put_vol / max(call_vol, 1.0),
        "top_call_oi_strike": None if top_call.empty else float(top_call.iloc[0]["strike"]),
        "top_put_oi_strike": None if top_put.empty else float(top_put.iloc[0]["strike"]),
        "net_gex": float(result.get("total_signed_gex", 0.0)),
        "gross_gex": float(result.get("total_gross_gex", 0.0)),
        "net_delta": float(result.get("net_delta_exposure", 0.0)),
    }


def _chain_figure(result: Dict[str, Any], metric: str = "Q-Score", symbol: str = "UNKNOWN") -> go.Figure:
    enr = result.get("enriched", pd.DataFrame()).copy()
    if enr.empty:
        return go.Figure().add_annotation(text="Sin datos", showarrow=False)
    enr["timestamp"] = pd.to_datetime(enr["timestamp"], errors="coerce")
    x = enr[enr["timestamp"] == enr["timestamp"].max()].copy()
    if "expiration_date" not in x.columns:
        return go.Figure().add_annotation(text="Sin expiration_date: Cadena no infiere vencimientos desde DTE", showarrow=False)
    x = add_quant_score(x, delta_mode=True)
    if metric == "Open Interest":
        x["v"] = pd.to_numeric(x["open_interest"], errors="coerce").fillna(0)
        title = "Open Interest"
    elif metric == "Volumen":
        x["v"] = pd.to_numeric(x["volume"], errors="coerce").fillna(0)
        title = "Volumen"
    elif metric in {"Gamma", "GEX"}:
        x["v"] = pd.to_numeric(x["gross_gex"], errors="coerce").fillna(0) / 1e6
        title = "Gamma / GEX bruto (M)"
    elif metric in {"Delta", "DEX"}:
        x["v"] = pd.to_numeric(x["option_delta_exposure_info"], errors="coerce").fillna(0).abs() / 1e6
        title = "Delta Exposure abs. (M)"
    elif metric == "Actividad inusual":
        x["v"] = pd.to_numeric(x["activity_ratio"], errors="coerce").fillna(0)
        title = "Vol/OI"
    elif metric == "Vanna":
        x["v"] = numeric_column(x,"calc_vanna",0).abs() * numeric_column(x,"open_interest",0) * 100.0
        title = "Vanna Exposure Proxy · |Vanna| × OI × 100"
    elif metric == "Charm":
        x["v"] = numeric_column(x,"calc_charm",0).abs() * numeric_column(x,"open_interest",0) * 100.0
        title = "Charm Exposure Proxy · |Charm| × OI × 100"
    elif metric == "Speed":
        x["v"] = numeric_column(x,"calc_speed",0).abs() * numeric_column(x,"open_interest",0) * 100.0
        title = "Speed Exposure Proxy · |Speed| × OI × 100"
    else:
        x["v"] = pd.to_numeric(x["quant_score"], errors="coerce").fillna(0)
        title = "Q-Score"
    x["side"] = np.where(x["option_type"].astype(str).str.lower().str.startswith("c"), "CALL", "PUT")
    x["col"] = x["expiration_date"].astype(str) + " · " + x["side"]
    x["signed_v"] = np.where(x["side"] == "CALL", x["v"], -x["v"])
    piv = x.pivot_table(index="strike", columns="col", values="signed_v", aggfunc="max", fill_value=0).sort_index(ascending=False)
    q = x.pivot_table(index="strike", columns="col", values="quant_score", aggfunc="max", fill_value=0).reindex(index=piv.index, columns=piv.columns)
    absval = np.abs(piv.values)
    maxv = max(float(np.nanmax(absval)), 1e-9)
    z = piv.values / maxv
    text = np.empty_like(z, dtype=object)
    for i in range(z.shape[0]):
        for j in range(z.shape[1]):
            val = abs(piv.values[i, j])
            qq = q.values[i, j]
            star = "🔥" if qq >= 85 else "★" if qq >= 70 else ""
            if title in {"Q-Score", "Vol/OI"}:
                sval = f"{val:.1f}"
            elif val >= 1e6:
                sval = f"{val/1e6:.1f}M"
            elif val >= 1e3:
                sval = f"{val/1e3:.1f}K"
            else:
                sval = f"{val:.0f}" if title not in {"Gamma / GEX bruto (M)", "Delta Exposure abs. (M)"} else f"{val:.1f}M"
            text[i, j] = f"{sval} {star}"
    fig = go.Figure(go.Heatmap(
        z=z, x=list(piv.columns), y=piv.index, text=text, texttemplate="%{text}",
        colorscale=[[0, "#7f1d2d"], [0.49, "#151b24"], [0.51, "#151b24"], [1, "#0f7a4a"]], zmid=0,
        showscale=False,
        customdata=np.dstack([absval, q.values]),
        hovertemplate="Strike %{y}<br>%{x}<br>Valor %{customdata[0]:.2f}<br>Q-Score %{customdata[1]:.1f}<extra></extra>",
    ))
    strike_labels=[f"{float(v):g}" for v in piv.index]
    chain_h=int(min(1180,max(650,150+18*len(strike_labels))))
    fig.update_layout(template="plotly_dark", height=chain_h, margin=dict(l=82, r=20, t=55, b=120),
                      paper_bgcolor="#080b10", plot_bgcolor="#0c1118", title=f"CADENA CUANTITATIVA {str(symbol).upper()} — {title}",
                      xaxis_tickangle=-35, yaxis_title=f"Strike {str(symbol).upper()}")
    fig.update_xaxes(type="category",tickmode="array",tickvals=list(piv.columns),ticktext=list(piv.columns))
    fig.update_yaxes(type="category",tickmode="array",tickvals=list(piv.index),ticktext=strike_labels,tickfont=dict(size=9),automargin=True)
    return fig


def _surface_slice_figure(result: Dict[str, Any], metric: str = "Gamma", option_view: str = "Net", render_style: str = "Barras") -> go.Figure:
    """Interactive 2D strike cross-section with explicit gross/net semantics.

    Bars/lines/points/peaks all use the same aggregated strike values.  Peaks are
    genuine stems from zero plus a terminal marker, not a renamed lines+markers trace.
    """
    enr=result.get("enriched",pd.DataFrame()).copy() if isinstance(result,dict) else pd.DataFrame()
    if enr.empty:return go.Figure().add_annotation(text="Sin cross-section",showarrow=False)
    enr["timestamp"]=pd.to_datetime(enr.get("timestamp"),errors="coerce");enr=enr.dropna(subset=["timestamp"]);x=enr[enr["timestamp"]==enr["timestamp"].max()].copy()
    if x.empty:return go.Figure().add_annotation(text="Sin cross-section",showarrow=False)
    x["strike"]=numeric_column(x,"strike",float("nan"));x=x.dropna(subset=["strike"])
    x["_is_call"]=x.get("option_type",pd.Series("call",index=x.index)).astype(str).str.lower().str.startswith("c")
    x["_oi"]=numeric_column(x,"open_interest",0.0)
    x["_vol"]=numeric_column(x,"volume",0.0)
    x["_net_oi"]=np.where(x["_is_call"],x["_oi"],-x["_oi"])
    x["_net_vol"]=np.where(x["_is_call"],x["_vol"],-x["_vol"])
    v=str(option_view or "Net").strip().lower();m=str(metric or "Gamma")
    # Net OI / Net Volume are intrinsically cross-side metrics. Calls/Puts view still
    # works and shows that side with its economic sign (PUT negative).
    if v.startswith("call") or v.startswith("put"):
        want=True if v.startswith("call") else False
        x=x[x["_is_call"]==want].copy()
        if x.empty:return go.Figure().add_annotation(text=f"Sin contratos {option_view}",showarrow=False)
    if m in {"Gamma","GEX"}: x["v"]=numeric_column(x,"signed_gex_proxy",0)/1e6;agg="sum";unit="$M signed GEX"
    elif m in {"Delta","DEX"}: x["v"]=numeric_column(x,"option_delta_exposure_info",0)/1e6;agg="sum";unit="$M Delta Exposure"
    elif m=="Vanna": x["v"]=numeric_column(x,"calc_vanna",0)*x["_oi"]*100;agg="sum";unit="Vanna exposure proxy"
    elif m=="Charm": x["v"]=numeric_column(x,"calc_charm",0)*x["_oi"]*100;agg="sum";unit="Charm exposure proxy"
    elif m=="Speed": x["v"]=numeric_column(x,"calc_speed",0)*x["_oi"]*100;agg="sum";unit="Speed exposure proxy"
    elif m=="Open Interest": x["v"]=x["_oi"];agg="sum";unit="OI bruto"
    elif m in {"Net OI","OI Net"}: x["v"]=x["_net_oi"];agg="sum";unit="Call OI - Put OI"
    elif m=="Volumen": x["v"]=x["_vol"];agg="sum";unit="Volumen bruto"
    elif m=="Volumen Neto": x["v"]=x["_net_vol"];agg="sum";unit="Call Vol - Put Vol"
    elif m=="Actividad inusual": x["v"]=numeric_column(x,"activity_ratio",0);agg="mean";unit="Vol/OI"
    else:
        x=add_quant_score(x,delta_mode=True);x["v"]=numeric_column(x,"quant_score",0);agg="mean";unit="Q-Score"
    a=x.groupby("strike",as_index=False)["v"].agg(agg).sort_values("strike")
    detail=x.groupby("strike",as_index=False).agg(call_oi=("_oi",lambda q:float(q[x.loc[q.index,"_is_call"]].sum())),put_oi=("_oi",lambda q:float(q[~x.loc[q.index,"_is_call"]].sum())),call_vol=("_vol",lambda q:float(q[x.loc[q.index,"_is_call"]].sum())),put_vol=("_vol",lambda q:float(q[~x.loc[q.index,"_is_call"]].sum())))
    detail["net_oi"]=detail["call_oi"]-detail["put_oi"];detail["net_vol"]=detail["call_vol"]-detail["put_vol"]
    a=a.merge(detail,on="strike",how="left")
    custom=np.column_stack([a["call_oi"],a["put_oi"],a["net_oi"],a["call_vol"],a["put_vol"],a["net_vol"]])
    colors=np.where(a["v"]>=0,"#36b6ff","#f04488")
    style=str(render_style or "Barras").strip().lower();fig=go.Figure()
    hover="Strike %{x:.2f}<br>Valor %{y:,.3f}<br>Call OI %{customdata[0]:,.0f} · Put OI %{customdata[1]:,.0f}<br>Net OI %{customdata[2]:,.0f}<br>Call Vol %{customdata[3]:,.0f} · Put Vol %{customdata[4]:,.0f}<br>Net Vol %{customdata[5]:,.0f}<extra></extra>"
    if style.startswith("ola"):
        fig.add_trace(go.Scatter(x=a["strike"],y=a["v"],mode="lines",name=m,line=dict(color="#36b6ff",width=2.4,shape="spline",smoothing=1.15),customdata=custom,hovertemplate=hover,connectgaps=False))
    elif style.startswith("lín") or style.startswith("lin"):
        fig.add_trace(go.Scatter(x=a["strike"],y=a["v"],mode="lines",name=m,line=dict(color="#36b6ff",width=2),customdata=custom,hovertemplate=hover,connectgaps=False))
    elif style.startswith("punt"):
        fig.add_trace(go.Scatter(x=a["strike"],y=a["v"],mode="markers",name=m,marker=dict(size=8,color=colors,line=dict(width=1,color="#0b1118")),customdata=custom,hovertemplate=hover))
    elif style.startswith("pic"):
        sx=[];sy=[]
        for k,val in zip(a["strike"],a["v"]):sx.extend([k,k,None]);sy.extend([0,val,None])
        fig.add_trace(go.Scatter(x=a["strike"],y=a["v"],mode="markers",name=m,marker=dict(size=8,color=colors,line=dict(width=1,color="#0b1118")),customdata=custom,hovertemplate=hover,showlegend=False))
        fig.add_trace(go.Scatter(x=sx,y=sy,mode="lines",name="Picos",line=dict(color="#526a7c",width=1.5),hoverinfo="skip",showlegend=False,connectgaps=False))
    else:
        fig.add_trace(go.Bar(x=a["strike"],y=a["v"],name=m,marker_color=colors,customdata=custom,hovertemplate=hover))
    spot=_finite(result.get("spot"));
    if spot is not None:fig.add_vline(x=spot,line_color="#eef4ff",line_dash="dot",annotation_text=f"SPOT {spot:.2f}")
    fig.update_layout(template="plotly_dark",height=390,paper_bgcolor="#080b10",plot_bgcolor="#0c1118",margin=dict(l=60,r=30,t=55,b=65),title=f"CROSS-SECTION 2D · {m} · {str(option_view).upper()} · {str(render_style).upper()}",xaxis_title="Strike",yaxis_title=unit,showlegend=False,uirevision=f"surface-slice-{m}-{option_view}",meta={"metric":m,"render":render_style,"option_view":option_view,"unit":unit})
    return fig

def _flow_figure(events: pd.DataFrame, history: pd.DataFrame, symbol: str = "UNKNOWN") -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.67, 0.33], vertical_spacing=0.08,
                        subplot_titles=(f"{str(symbol).upper()} + EVENTOS DE FLOW", "FLOW NETO POR MINUTO"))
    if history is not None and not history.empty:
        px = history[["timestamp", "underlying_price"]].copy()
        px["timestamp"] = pd.to_datetime(px["timestamp"], errors="coerce")
        px = px.dropna().drop_duplicates("timestamp").sort_values("timestamp")
        tx=pd.to_datetime(px["timestamp"],errors="coerce").reset_index(drop=True);py=pd.to_numeric(px["underlying_price"],errors="coerce").reset_index(drop=True);dif=tx.diff().dt.total_seconds();pos=dif[dif.gt(0)].sort_values();expected=float(pos.iloc[:max(1,len(pos)//2)].median()) if len(pos) else 0.0;gap=max(90.0,expected*4.0 if expected>0 else 90.0);xx=[];yy=[]
        for i,(t,v) in enumerate(zip(tx,py)):
            if i and (t-tx.iloc[i-1]).total_seconds()>gap:xx.append(None);yy.append(None)
            xx.append(t);yy.append(None if not math.isfinite(float(v)) else float(v))
        fig.add_trace(go.Scatter(x=xx, y=yy, mode="lines", name=str(symbol).upper(), connectgaps=False, line=dict(width=2.5, color="#dce7f3")), row=1, col=1)
    if events is not None and not events.empty:
        e = events.copy()
        e["timestamp"] = pd.to_datetime(e["timestamp"], errors="coerce")
        e = e.dropna(subset=["timestamp"])
        if not e.empty:
            score = numeric_column(e,"flow_score",0)
            top = e[score >= 70].copy()
            if not top.empty:
                ss = pd.to_numeric(top["direction_sign"], errors="coerce").fillna(0)
                pp = pd.to_numeric(top["premium"], errors="coerce").fillna(0)
                qq = pd.to_numeric(top["flow_score"], errors="coerce").fillna(0)
                txt = [f"{'+' if s > 0 else '-'}${abs(p)/1e6:.1f}M · QF{q:.0f}" for s, p, q in zip(ss, pp, qq)]
                fig.add_trace(go.Scatter(x=top["timestamp"], y=top["underlying_price"], mode="markers+text", text=txt, textposition="top center", name="Q-Flow ≥70",
                                         marker=dict(size=np.clip(qq/5, 10, 22), color=np.where(ss > 0, "#45e88c", "#ff6075"))), row=1, col=1)
            e["minute"] = e["timestamp"].dt.floor("min")
            m = e.groupby("minute", as_index=False)["directional_premium"].sum()
            fig.add_trace(go.Bar(x=m["minute"], y=m["directional_premium"] / 1e6, name="Net Flow (M)", marker_color=np.where(m["directional_premium"] >= 0, "#35c978", "#ef5268")), row=2, col=1)
    fig.update_layout(template="plotly_dark", height=650, paper_bgcolor="#080b10", plot_bgcolor="#0c1118", margin=dict(l=55, r=30, t=65, b=45), legend=dict(orientation="h"))
    fig.update_yaxes(title_text=str(symbol).upper(), row=1, col=1)
    fig.update_yaxes(title_text="$M", row=2, col=1)
    return fig


def _vol_figure(vol: Dict[str, Any]) -> go.Figure:
    term = pd.DataFrame(vol.get("term_structure", []))
    fig = go.Figure()
    if not term.empty:
        fig.add_trace(go.Scatter(x=term["expiration_date"], y=term["iv"] * 100, mode="lines+markers", name="IV media"))
    fig.update_layout(template="plotly_dark", height=420, paper_bgcolor="#080b10", plot_bgcolor="#0c1118", margin=dict(l=55, r=30, t=55, b=55),
                      title="VOLATILITY ENGINE — TERM STRUCTURE", xaxis_title="Vencimiento", yaxis_title="IV %")
    return fig


def _skew_current(gd: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    """Latest-snapshot 25-delta skew term structure (SpotGamma-style Skew Model).

    Computed only on demand for the Volatilidad section (same cadence as
    _vol_figure), never on the hot TRACE polling path -- fitting SVI per expiry
    is not free and has no reason to run on every live tick.
    """
    enr = gd.get("enriched") if isinstance(gd, dict) else None
    if not isinstance(enr, pd.DataFrame) or enr.empty or "timestamp" not in enr.columns:
        return {"ready": False, "reason": "NO_STRUCTURAL_HISTORY", "rows": []}
    x = enr.copy(); x["timestamp"] = pd.to_datetime(x["timestamp"], errors="coerce")
    x = x.dropna(subset=["timestamp"])
    if x.empty:
        return {"ready": False, "reason": "NO_VALID_HISTORY", "rows": []}
    latest = x[x["timestamp"] == x["timestamp"].max()].copy()
    spot = numeric_column(latest,"underlying_price",float("nan")).dropna()
    if spot.empty:
        return {"ready": False, "reason": "NO_SPOT", "rows": []}
    try:
        return skew_term_structure(latest, float(spot.iloc[-1]), symbol=symbol)
    except Exception as exc:
        _obs_note("service:_skew_current", exc, severity="DEGRADED")
        return {"ready": False, "reason": f"{type(exc).__name__}: {exc}"[:160], "rows": []}


def _skew_figure(skew: Dict[str, Any]) -> go.Figure:
    """SVI-modeled skew term structure.

    This is deliberately a SEPARATE number from the hero-card 'SKEW 25Δ' /
    skewExpiryGrid metric above, which averages the 1-3 OBSERVED listed contracts
    nearest to 0.25 delta (discrete, can land at 0.20-0.32 delta depending on
    strike spacing). This chart instead fits SVI to the whole smile and solves
    continuously for the exact 25-delta strike -- smoother and arbitrage-checked,
    but a MODEL read, not raw observed quotes. Neither replaces the other.
    """
    rows = pd.DataFrame(skew.get("rows", []) if isinstance(skew, dict) else [])
    fig = go.Figure()
    if not rows.empty:
        fig.add_trace(go.Scatter(x=rows["expiration_date"], y=rows["skew_25d_pct"], mode="lines+markers",
                                 name="Skew 25Δ SVI (Put IV − Call IV)", line=dict(color="#f2c24f")))
        fig.add_hline(y=0, line_dash="dot", line_color="rgba(200,200,200,.35)")
    fig.update_layout(template="plotly_dark", height=420, paper_bgcolor="#080b10", plot_bgcolor="#0c1118",
                      margin=dict(l=55, r=30, t=55, b=55),
                      title="SKEW MODEL (SVI) — 25Δ RISK REVERSAL POR VENCIMIENTO",
                      xaxis_title="Vencimiento", yaxis_title="Skew (vol pts) · Put IV − Call IV")
    return fig


# ============================================================
# EQUITY HUB (v1.36) — SpotGamma-style single-page summary.
# Every number below is read from a calculation that already exists and is
# already tested elsewhere (key_levels_report, gamma_squeeze, market_inputs,
# atm_iv_and_dte); this section only assembles/renders them, plus the new
# Monte Carlo probability cone. Computed on demand for this section only,
# never on the hot TRACE polling path.
# ============================================================

def _equity_hub_gamma_model_figure(gd: Dict[str, Any], symbol: str) -> go.Figure:
    """SpotGamma-style Gamma Model: call (green) / put (red) dollar-gamma by
    strike, split rather than netted -- same convention as the TRACE chart's
    split profile lanes (trace_live.py call_gamma_m/put_gamma_m), computed
    directly from the latest chain snapshot so this panel does not depend on
    TRACE's live-repricing state.
    """
    fig = go.Figure()
    enr = gd.get("enriched") if isinstance(gd, dict) else None
    if not isinstance(enr, pd.DataFrame) or enr.empty or "timestamp" not in enr.columns:
        fig.update_layout(template="plotly_dark", height=420, paper_bgcolor="#080b10", plot_bgcolor="#0c1118",
                          title="GAMMA MODEL — SIN ESTRUCTURA DISPONIBLE")
        return fig
    x = enr.copy(); x["timestamp"] = pd.to_datetime(x["timestamp"], errors="coerce")
    x = x.dropna(subset=["timestamp"])
    latest = x[x["timestamp"] == x["timestamp"].max()].copy()
    latest["strike"] = numeric_column(latest,"strike",float("nan"))
    latest["gross_gex"] = numeric_column(latest,"gross_gex",0.0)
    latest = latest.dropna(subset=["strike"])
    is_call = latest.get("option_type", pd.Series("call", index=latest.index)).astype(str).str.lower().str.startswith("c")
    call_g = latest[is_call].groupby("strike")["gross_gex"].sum().div(1e6)
    put_g = latest[~is_call].groupby("strike")["gross_gex"].sum().div(1e6)
    strikes = sorted(set(call_g.index) | set(put_g.index))
    spot = _finite(gd.get("spot"))
    if spot is not None and len(strikes) > 40:
        strikes = sorted(strikes, key=lambda k: abs(k - spot))[:40]
        strikes = sorted(strikes)
    if strikes:
        fig.add_trace(go.Bar(x=strikes, y=[float(call_g.get(k, 0.0)) for k in strikes], name="Call Gamma", marker_color="#22c55e"))
        fig.add_trace(go.Bar(x=strikes, y=[-float(put_g.get(k, 0.0)) for k in strikes], name="Put Gamma", marker_color="#ef4444"))
    if spot is not None:
        fig.add_vline(x=spot, line_dash="dash", line_color="rgba(230,235,240,.55)", annotation_text="SPOT")
    fig.update_layout(template="plotly_dark", height=420, paper_bgcolor="#080b10", plot_bgcolor="#0c1118",
                      barmode="relative", margin=dict(l=55, r=30, t=55, b=45),
                      title=f"GAMMA MODEL · {str(symbol or '').upper()} — CALL/PUT POR STRIKE (GROSS $GAMMA, M)",
                      xaxis_title="Strike", yaxis_title="Gross $Gamma (M) · call arriba / put abajo")
    return fig


def _monte_carlo_current(gd: Dict[str, Any], symbol: str, *, n_sims: int = 20_000) -> Dict[str, Any]:
    """Assemble Monte Carlo inputs from calculations that already exist: the
    latest chain snapshot's ATM IV/DTE (same helper Key Levels uses), the
    engine's own r/q (market_inputs), and the same key_levels_report() this
    session already computes for the TRACE HUD and Sophia's named-level watches.
    """
    enr = gd.get("enriched") if isinstance(gd, dict) else None
    if not isinstance(enr, pd.DataFrame) or enr.empty or "timestamp" not in enr.columns:
        return {"ready": False, "reason": "NO_STRUCTURAL_HISTORY"}
    x = enr.copy(); x["timestamp"] = pd.to_datetime(x["timestamp"], errors="coerce")
    x = x.dropna(subset=["timestamp"])
    if x.empty:
        return {"ready": False, "reason": "NO_VALID_HISTORY"}
    latest = x[x["timestamp"] == x["timestamp"].max()].copy()
    spot = _finite(gd.get("spot"))
    if spot is None:
        sp = numeric_column(latest,"underlying_price",float("nan")).dropna()
        spot = float(sp.iloc[-1]) if not sp.empty else None
    if spot is None:
        return {"ready": False, "reason": "NO_SPOT"}
    atm_iv, dte_days = atm_iv_and_dte(latest, spot)
    if atm_iv is None or dte_days is None:
        return {"ready": False, "reason": "NO_ATM_IV_OR_DTE"}
    mi = _market_inputs_safe(symbol, dte_days)
    klr = key_levels_report(gd, {}, curagg=gd.get("current"), spot=spot, symbol=symbol)
    levels = {k: klr.get(k) for k in ("zero_gamma", "call_wall", "put_wall", "vol_trigger", "max_pain")}
    try:
        return monte_carlo_level_report(
            spot, float(mi.get("risk_free_rate", 0.045)), float(mi.get("dividend_yield", 0.0)),
            atm_iv, dte_days, levels, n_sims=n_sims,
        )
    except Exception as exc:
        _obs_note("service:_monte_carlo_current", exc, severity="DEGRADED")
        return {"ready": False, "reason": f"{type(exc).__name__}: {exc}"[:160]}


_MC_LEVEL_LABELS = {"zero_gamma": "Zero Gamma", "call_wall": "Call Wall", "put_wall": "Put Wall",
                   "vol_trigger": "Vol Trigger", "max_pain": "Max Pain"}
_MC_LEVEL_COLORS = {"zero_gamma": "#e9edf0", "call_wall": "#22c55e", "put_wall": "#ef4444",
                    "vol_trigger": "#f59e0b", "max_pain": "#a78bfa"}


def _monte_carlo_figure(mc: Dict[str, Any]) -> go.Figure:
    """Probability-cone fan chart: p10/p25/p50/p75/p90 daily bands from today's
    spot to expiration, with the same structural levels drawn as horizontal
    reference lines so the cone can be read against Call Wall/Put Wall/Zero
    Gamma/Max Pain directly.
    """
    fig = go.Figure()
    if not isinstance(mc, dict) or not mc.get("ready"):
        fig.update_layout(template="plotly_dark", height=460, paper_bgcolor="#080b10", plot_bgcolor="#0c1118",
                          title=f"MONTE CARLO — {str((mc or {}).get('reason') or 'SIN DATOS SUFICIENTES')}")
        return fig
    days = mc["days_axis"]; cone = mc["cone_percentiles"]
    band_pairs = ((10, 90, "rgba(57,214,255,.10)", "p10–p90"), (25, 75, "rgba(57,214,255,.22)", "p25–p75"))
    for lo, hi, color, label in band_pairs:
        fig.add_trace(go.Scatter(x=days + days[::-1], y=cone[hi] + cone[lo][::-1], fill="toself",
                                 fillcolor=color, line=dict(width=0), name=label, showlegend=True))
    fig.add_trace(go.Scatter(x=days, y=cone[50], mode="lines", name="Mediana (p50)",
                             line=dict(color="#5d7dff", width=2)))
    for key, lvl in (mc.get("levels") or {}).items():
        price = lvl.get("level")
        if price is None:
            continue
        fig.add_hline(y=price, line_dash="dot", line_color=_MC_LEVEL_COLORS.get(key, "#8090a0"),
                     annotation_text=f"{_MC_LEVEL_LABELS.get(key, key)} {price:g}",
                     annotation_font_color=_MC_LEVEL_COLORS.get(key, "#8090a0"))
    fig.update_layout(template="plotly_dark", height=460, paper_bgcolor="#080b10", plot_bgcolor="#0c1118",
                      margin=dict(l=55, r=30, t=55, b=45),
                      title=f"MONTE CARLO — CONO DE PROBABILIDAD · {mc['n_sims']:,} SIMS · IV ATM {mc['atm_iv_pct']:.1f}% · {mc['dte_days']:.1f}D",
                      xaxis_title="Días desde hoy", yaxis_title="Precio simulado")
    return fig


def _top_chain_insights(result: Dict[str, Any]) -> Dict[str, Any]:
    enr = result.get("enriched", pd.DataFrame()).copy()
    if enr.empty:
        return {}
    enr["timestamp"] = pd.to_datetime(enr["timestamp"], errors="coerce")
    x = enr[enr["timestamp"] == enr["timestamp"].max()].copy()
    if "expiration_date" not in x.columns:
        return go.Figure().add_annotation(text="Sin expiration_date: Cadena no infiere vencimientos desde DTE", showarrow=False)
    x = add_quant_score(x, delta_mode=True)
    is_call = x["option_type"].astype(str).str.lower().str.startswith("c")
    def pack(r):
        if r is None: return None
        return {"strike": float(r["strike"]), "expiration": str(r["expiration_date"]), "qscore": float(r["quant_score"]), "oi": float(r["open_interest"]), "volume": float(r["volume"])}
    call = x[is_call].sort_values("quant_score", ascending=False).head(1)
    put = x[~is_call].sort_values("quant_score", ascending=False).head(1)
    unusual = x.sort_values("activity_ratio", ascending=False).head(1)
    gamma = x.sort_values("gross_gex", ascending=False).head(1)
    delta = x.assign(absd=x["option_delta_exposure_info"].abs()).sort_values("absd", ascending=False).head(1)
    return {
        "top_call": None if call.empty else pack(call.iloc[0]),
        "top_put": None if put.empty else pack(put.iloc[0]),
        "unusual": None if unusual.empty else {**pack(unusual.iloc[0]), "vol_oi": float(unusual.iloc[0]["activity_ratio"])},
        "gamma_hotspot": None if gamma.empty else {**pack(gamma.iloc[0]), "gex_m": float(gamma.iloc[0]["gross_gex"]) / 1e6},
        "delta_hotspot": None if delta.empty else {**pack(delta.iloc[0]), "delta_m": float(delta.iloc[0]["option_delta_exposure_info"]) / 1e6},
    }


def _asset_macro_context(macro: Dict[str, Any], symbol: str, regime: str = "") -> Dict[str, Any]:
    """Asset/regime-specific macro stress lens. Context only; never a standalone signal."""
    c=((macro or {}).get("stress") or {}).get("components",{}) or {}
    vals={
        "credit": float(np.clip(float(c.get("hy_z",0) or 0)/3.0*100,0,100)),
        "financial_conditions": float(np.clip(float(c.get("nfci_z",0) or 0)/3.0*100,0,100)),
        "rates": float(np.clip(float(c.get("treasury_10y_change_5",0) or 0)/0.30*100,0,100)),
        "curve": 100.0 if float(c.get("curve_10y2y",0) or 0)<-0.25 else 50.0 if float(c.get("curve_10y2y",0) or 0)<0 else 0.0,
        "event": float(np.clip(float(c.get("event_risk",0) or 0),0,100)),
    }
    sym=str(symbol).upper(); profiles={
        "DIA":dict(credit=.28,financial_conditions=.22,rates=.18,curve=.18,event=.14),
        "SPY":dict(credit=.25,financial_conditions=.20,rates=.20,curve=.15,event=.20),
        "QQQ":dict(credit=.14,financial_conditions=.14,rates=.36,curve=.10,event=.26),
        "TQQQ":dict(credit=.12,financial_conditions=.12,rates=.38,curve=.08,event=.30),
        "AAPL":dict(credit=.10,financial_conditions=.10,rates=.40,curve=.10,event=.30),
        "GLD":dict(credit=.08,financial_conditions=.10,rates=.38,curve=.14,event=.30),
        "GDX":dict(credit=.18,financial_conditions=.15,rates=.32,curve=.15,event=.20),
        "VXX":dict(credit=.30,financial_conditions=.25,rates=.12,curve=.08,event=.25),
    }; w=dict(profiles.get(sym,profiles["SPY"]))
    rg=str(regime).upper()
    if "VOLATILITY" in rg or "BREAKOUT" in rg:
        w["event"]*=1.18; w["rates"]*=1.08
    elif "PINNING" in rg or "MEAN REVERSION" in rg:
        w["event"]*=.90
    z=sum(w.values()) or 1.0; w={k:v/z for k,v in w.items()}
    score=sum(vals[k]*w[k] for k in w)
    return {"score":round(float(score),1),"weights":{k:round(v,3) for k,v in w.items()},"components":{k:round(v,1) for k,v in vals.items()},"symbol":sym,"regime":regime,"note":"Contexto macro ponderado por activo/régimen. No decide dirección por sí solo."}


# Columnas que sólo existen después de enriquecer la cadena. Quien las necesite
# tiene que recibir el frame enriquecido, no el snapshot crudo.
_EXPOSURE_COLUMNS = ("signed_gex_proxy", "option_delta_exposure_info")


def _exposure_frame(gd: Dict[str, Any] | None, snapshot: Any, fallback: Any = None):
    """Frame con las exposiciones ya calculadas, para desgloses por vencimiento y perfiles.

    `signed_gex_proxy` y `option_delta_exposure_info` las crea enrich_options(): el
    snapshot crudo no las tiene. Pasarle el snapshot a build_expiry_intelligence()
    hacía que gamma, delta, vanna y charm salieran en CERO por vencimiento mientras
    OI y volumen sí llegaban — un panel que parecía roto sin estarlo, y un desglose
    que nunca ha sido correcto.
    """
    enriched = (gd or {}).get("enriched")
    if isinstance(enriched, pd.DataFrame) and not enriched.empty \
            and all(c in enriched.columns for c in _EXPOSURE_COLUMNS):
        return enriched
    if isinstance(snapshot, pd.DataFrame) and not snapshot.empty:
        return snapshot
    if isinstance(fallback, pd.DataFrame) and not fallback.empty:
        return fallback
    return pd.DataFrame()


def _now_monotonic() -> float:
    """Reloj monótono para marcas de caché.

    En este módulo `time` es datetime.time (importado para los horarios de sesión),
    así que time.time() no existe. Aislarlo aquí evita repetir la confusión.
    """
    import time as _t
    return _t.monotonic()


def _asset_stress(macro: Dict[str, Any] | None) -> float:
    """Estrés macro del activo, 0-100, para ensanchar la varianza del escenario.

    _asset_macro_context publica esa lectura bajo `score`. Se leía `stress_score`,
    que nunca existió: build_scenario_lab recibía None y el ensanchamiento macro
    del Monte Carlo no se aplicaba jamás, ni en el peor día de crédito. El
    respaldo es el estrés global, que sí usa `score` con ese mismo nombre.
    """
    m = macro or {}
    ctx = m.get("asset_context") or {}
    for candidate in (ctx.get("score"), ctx.get("stress_score"), (m.get("stress") or {}).get("score")):
        if not isinstance(candidate, (int, float)) or isinstance(candidate, bool):
            continue
        v = float(candidate)
        if math.isfinite(v):
            return min(100.0, max(0.0, v))
    return 0.0


def _command_center(result: Dict[str, Any], flow: Dict[str, Any], vol: Dict[str, Any], targets: Dict[str, Any],
                    scanner: Dict[str, Any] | None = None, tape: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Command Center is a translator, never a second directional engine.

    Scanner owns thesis/direction/actionability. Tape owns entry timing only. Gamma/vol/flip
    are context and cannot vote, create or flip the headline direction.
    """
    sc = scanner or {}
    tp = tape or {}
    ready = bool(sc.get("ready"))
    direction = str(sc.get("direction") or "WAITING").upper() if ready else "WAITING"
    zone = sc.get("zone") or {}
    edge = str(sc.get("edge_state") or "WAITING").upper()
    gate = sc.get("edge_gate") or {}
    ev_active = bool(gate.get("active"))
    prob = _finite(gate.get("probability_t1_first")) if bool(gate.get("calibration_ready")) else None
    ev = _finite(gate.get("expected_value_r"))
    if ev_active:
        actionability_source = "EXPECTED VALUE"
    else:
        # Even if a calibrated EV exists in SHADOW, production actionability still comes
        # from the legacy evidence thresholds until the EV gate is explicitly active.
        actionability_source = "EVIDENCE THRESHOLD (LEGACY)"
    actionability = f"{edge} · {actionability_source}" if ready else "WAITING"

    conf = (tp.get("confirmation") or {}) if isinstance(tp, dict) else {}
    tape_state = str(conf.get("state") or "WAITING").upper()
    tape_progress = _finite(conf.get("progress_pct"))
    tape_seconds = _finite(conf.get("seconds_remaining"))

    active = targets.get("active") or {}
    state = active.get("state") or "—"
    if state == "BREAK": market_regime = "EXPANSION RISK"
    elif state == "CONTAINMENT": market_regime = "CONTAINMENT"
    elif state == "TRANSITION": market_regime = "TRANSITION"
    else: market_regime = "MIXED"

    spot = _finite(result.get("spot")); flip = _finite(result.get("gamma_flip")); em = _finite(vol.get("expected_move"))
    flip_distance = (spot - flip) if spot is not None and flip is not None else None
    flip_distance_em = (flip_distance / em) if flip_distance is not None and em is not None and em > 1e-12 else None
    if flip_distance is None:
        flip_context = "FLIP UNAVAILABLE"
    elif abs(flip_distance) < 1e-12:
        flip_context = "AT FLIP"
    else:
        side = "ABOVE" if flip_distance > 0 else "BELOW"
        flip_context = f"{side} FLIP · {abs(flip_distance):.2f}" + (f" · {abs(flip_distance_em):.2f} EM" if flip_distance_em is not None else "")

    return {
        # Compatibility field name kept for the UI/API, but it is now Scanner direction,
        # not an independently calculated bias. There is deliberately no 0-100 bias score.
        "bias": direction,
        "bias_score": None,
        "direction_source": "SCANNER",
        "zone": {"low": zone.get("low"), "high": zone.get("high"), "center": zone.get("center")},
        "actionability": actionability,
        "actionability_state": edge,
        "actionability_source": actionability_source,
        "gate_mode_raw": gate.get("mode"),
        "ev_gate_active": ev_active,
        "expected_value_r": ev,
        "probability_t1_first": prob,
        "probability_status": "CALIBRATED" if prob is not None else "COLLECTING",
        "tape_state": tape_state,
        "tape_progress_pct": tape_progress,
        "tape_seconds_remaining": tape_seconds,
        "tape_role": "TIMING_ONLY",
        "market_regime": market_regime,
        "gamma_regime": result.get("regime", "—"),
        "vol_regime": vol.get("regime", "—"),
        "flip_context": flip_context,
        "flip_distance": flip_distance,
        "flip_distance_expected_move": flip_distance_em,
        # Inputs remain visible strictly as context; they do not constitute a consensus.
        "gamma_pressure": f"{result.get('pressure_direction','—')} {float(result.get('pressure_score',0)):.0f}",
        "delta_pressure": f"{result.get('delta_pressure_direction','—')} {float(result.get('delta_pressure_score',0)):.0f}",
        "flow_regime": f"{flow.get('regime','WAITING')} {float(flow.get('confidence',0)):.0f}",
        "active_state": state,
        "active_confidence": float(active.get("confidence", 0) or 0),
        "note": "SCANNER MANDA · COMMAND CENTER TRADUCE · TAPE TEMPORIZA · GAMMA/VOL SOLO CONTEXTO",
    }


def _read_premarket_store() -> Dict[str, Any]:
    try:
        if PREMARKET_PATH.exists():
            return json.loads(PREMARKET_PATH.read_text(encoding="utf-8"))
    except Exception as _e:
        _obs_note('service:1068', _e)
    return {}


def _write_premarket_store(data: Dict[str, Any]):
    PREMARKET_PATH.write_text(json.dumps(_jsonable(data), indent=2), encoding="utf-8")


def _freeze_map_payload(result: Dict[str, Any], targets: Dict[str, Any], vol: Dict[str, Any], positioning: Dict[str, Any], tape: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    path_dir = result.get("delta_pressure_direction", "NEUTRAL")
    if path_dir == "NEUTRAL": path_dir = result.get("pressure_direction", "NEUTRAL")
    favored = [result.get("spot"), targets.get("pivot"), targets.get("high") if _direction_sign(path_dir) >= 0 else targets.get("low")]
    return {
        "captured_at_ec": datetime.now(EC).isoformat(),
        "spot": result.get("spot"),
        "low": targets.get("low"), "pivot": targets.get("pivot"), "high": targets.get("high"),
        "favored_direction": path_dir,
        "favored_path": [x for x in favored if x is not None],
        "gamma_center": result.get("gamma_center"), "gamma_flip": result.get("gamma_flip"),
        "gamma_regime": result.get("regime"),
        "gamma_pressure": {"direction": result.get("pressure_direction"), "score": result.get("pressure_score")},
        "delta_pressure": {"direction": result.get("delta_pressure_direction"), "score": result.get("delta_pressure_score")},
        "volatility": vol,
        "positioning": positioning,
        "premarket_tape": None if tape is None else {k: v for k, v in tape.items() if k != "bars"},
    }




def _fabric_last_price(symbol: str) -> float | None:
    """Último precio observado de la cinta, o `None`.

    v1.57.0 · `PRICE_TICK_FABRIC.snapshot(...)` NO EXISTE.

    Se llamaba así en `nextgen_trace_price_only`, dentro de un `try/except` que
    se tragaba el `AttributeError`. O sea: justo en el camino degradado —al que
    cae la terminal cuando la cadena no hidrata— el respaldo de precio no podía
    funcionar nunca, y nadie se enteraba porque el error moría en el `except`.

    El método real de la cinta es `dataframe()`.
    """
    try:
        df = PRICE_TICK_FABRIC.dataframe(symbol)
    except Exception as exc:
        _obs_note("service:fabric_last_price", exc, severity="DEGRADED")
        return None
    if df is None or getattr(df, "empty", True) or "price" not in df.columns:
        return None
    serie = pd.to_numeric(df["price"], errors="coerce").dropna()
    if not len(serie):
        return None
    valor = float(serie.iloc[-1])
    return valor if (valor == valor and valor > 0) else None


class ChainSpotUnavailable(RuntimeError):
    """La cadena llegó sin un precio del subyacente utilizable.

    Existe para que este fallo tenga NOMBRE. Antes reventaba como
    `IndexError: single positional indexer is out-of-bounds` desde lo hondo de
    pandas, y ese texto era todo lo que quedaba en `last_error` y en el Auditor.
    """


def _resolve_chain_spot(snapshot: pd.DataFrame, symbol: str) -> tuple[float, str]:
    """Precio del subyacente para procesar la cadena, con procedencia declarada.

    v1.57.0 · UN `.iloc[-1]` SIN RED TUMBABA LA TERMINAL ENTERA.

    Esto era una línea:

        spot0 = float(numeric_column(snapshot,"underlying_price",...).dropna().iloc[-1])

    Si la cadena llegaba sin `underlying_price`, con la columna entera a NaN o
    directamente vacía, `.iloc[-1]` lanza `IndexError`. Y esa línea está dentro
    del `try` grande del refresco, así que el refresco ABORTABA: `gamma_delta`
    se quedaba en `{}`, `nextgen_trace` se iba por `nextgen_trace_price_only` y
    la terminal publicaba velas y nada más.

    El resultado en pantalla, en TODOS los activos porque el fallo no depende
    del ticker: heatmap del TRACE vacío, perfiles por strike vacíos, niveles
    vacíos, cero prints de opciones —y por tanto ningún agresor, ningún
    BUY/SELL—, skew vacío y exposición por vencimiento vacía. Diez paneles
    diciendo SIN DATOS por una sola línea.

    Lo más absurdo: el precio NO falta. La terminal lo está enseñando arriba y
    dibuja cientos de velas con él. Lo que faltaba era ir a buscarlo donde sí
    estaba cuando la cadena no lo trae.

    El orden es el de siempre: primero el dato del proveedor dentro de la propia
    cadena; sólo si no está, la cinta de precio observada. La procedencia se
    devuelve para que no se confunda un precio de la cadena con uno prestado.
    """
    serie = numeric_column(snapshot, "underlying_price", float("nan")).dropna() \
        if snapshot is not None and not snapshot.empty else pd.Series(dtype="float64")
    if len(serie):
        valor = float(serie.iloc[-1])
        if valor == valor and valor > 0:
            return valor, "CHAIN_UNDERLYING_PRICE"

    # La cadena no lo trae. La cinta observada sí, y es el mismo instrumento.
    precio = _fabric_last_price(symbol)
    if precio is not None:
        return precio, "PRICE_TICK_FABRIC_FALLBACK"

    filas = 0 if snapshot is None else len(snapshot)
    columna = (snapshot is not None and "underlying_price" in getattr(snapshot, "columns", []))
    raise ChainSpotUnavailable(
        f"{symbol}: la cadena llegó con {filas} contrato(s) pero sin precio del "
        f"subyacente utilizable ("
        f"{'columna underlying_price presente pero sin valores' if columna else 'sin columna underlying_price'}"
        f"), y la cinta de precio observada tampoco tiene precio para este "
        f"activo. Sin precio no se puede centrar la cadena ni calcular exposición."
    )


def _snapshot_atm_iv_pct(snapshot: pd.DataFrame) -> float | None:
    if snapshot is None or snapshot.empty or "underlying_price" not in snapshot.columns:
        return None
    try:
        spot=float(pd.to_numeric(snapshot["underlying_price"],errors="coerce").dropna().iloc[-1])
        x=snapshot.copy(); x["strike"]=numeric_column(x,"strike",float("nan"))
        ivcol="iv" if "iv" in x.columns else "provider_iv" if "provider_iv" in x.columns else None
        if ivcol is None:return None
        x["_iv"]=pd.to_numeric(x[ivcol],errors="coerce")
        x=x.dropna(subset=["strike","_iv"]); x=x[x["_iv"]>0]
        if x.empty:return None
        x["_d"]=(x["strike"]-spot).abs(); near=x.nsmallest(min(12,len(x)),"_d")
        v=float(near["_iv"].median())
        return v*100.0 if v<2.5 else v
    except Exception:return None

def _expiry_load_days(mode: str, now_date: date | None = None) -> int:
    """Universal API expiry span by requested scope, never by ticker."""
    d=now_date or datetime.now(NY).date(); m=normalize_window(mode)
    friday=d+timedelta(days=(4-d.weekday())%7); next_friday=friday+timedelta(days=7)
    if m=="0DTE": return 1
    if m=="WEEK": return max(1,(friday-d).days+1)
    if m in {"2W","AUTO"}: return max(8,(next_friday-d).days+1)
    if m=="MONTH":
        nxt=(d.replace(day=28)+timedelta(days=4)).replace(day=1)
        return max(1,(nxt-d).days)
    return max(30,int(os.getenv("ITM_ALL_EXPIRY_DAYS","60")))


BOARD_REFRESH_SECONDS = max(3.0, float(os.getenv("ITM_BOARD_REFRESH_SECONDS", "8")))


def _board_expected_cadence() -> float:
    n=max(1,sum(1 for cfg in ASSETS.values() if cfg.get("full",False) and cfg.get("board_enabled",True)))
    return BOARD_REFRESH_SECONDS*n


def _board_scan_symbol(symbol: str, expiry_mode: str, previous: Dict[str,Any] | None = None) -> Dict[str,Any]:
    """Isolated structural Scanner refresh for the Board.

    It never changes PRICE_STREAM/OPTION_STREAM universes and never persists Scanner/Research rows.
    Therefore Board refreshes cannot contaminate calibration or execution timing.
    """
    cfg=asset_info(symbol)
    if not cfg.get("full",False):
        return {"symbol":symbol,"scanner":{"ready":False,"reason":cfg.get("reason")},"as_of":datetime.now(EC)}
    use_live = DATA_MODE == "live" or (DATA_MODE == "auto" and _live_provider_configured(symbol))
    legacy_window=float(cfg.get("window",STRIKE_WINDOW)); expiry=_expiry_load_days(expiry_mode)
    if use_live:
        initial_window, initial_source = get_chain_window_hint(symbol, expiry_mode, legacy_window)
        snapshot,meta=_fetch_asset_quant_snapshot(symbol,initial_window,expiry)
        iv_pct=_snapshot_atm_iv_pct(snapshot); hinfo=effective_horizon_days(snapshot,expiry_mode)
        spot0=float(numeric_column(snapshot,"underlying_price",float("nan")).dropna().iloc[-1])
        win=chain_window_for(symbol,spot0,iv_pct,hinfo.get("days") if hinfo.get("ready") else None)
        target=float(win.get("window",initial_window))
        if win.get("universal") and abs(target-initial_window)/max(initial_window,1e-9)>0.15:
            try: snapshot,meta=_fetch_asset_quant_snapshot(symbol,target,expiry)
            except Exception as _e:
                _obs_note('service:1156', _e)
        save_chain_window_hint(symbol, expiry_mode, target, source=str(win.get("method") or "BOARD_VALIDATED_CHAIN"))
        meta["initial_window_source"]=initial_source
        # One current structural snapshot is enough for the Board; history from storage gives context.
        try:
            hist=load_history(datetime.now(EC).date(),symbol)
            history=pd.concat([hist,snapshot],ignore_index=True) if isinstance(hist,pd.DataFrame) and not hist.empty else snapshot.copy()
        except Exception: history=snapshot.copy()
    else:
        history=_demo_history(symbol); snapshot=_latest_snapshot(history)
        meta={"source":"DEMO BOARD","symbol":symbol,"spot":float(snapshot["underlying_price"].iloc[-1]),"market_state":"DEMO"}
    selected,info=apply_expiry_window(history,expiry_mode)
    if selected.empty:
        return {"symbol":symbol,"scanner":{"ready":False,"reason":f"Sin contratos para {LABELS.get(expiry_mode,expiry_mode)}"},"as_of":datetime.now(EC),"expiry_label":LABELS.get(expiry_mode,expiry_mode)}
    gd=analyze_gamma_delta(selected,engine_config_for_asset(symbol,expiry_mode))
    try:
        events=load_flow_events(datetime.now(EC).date(),symbol) if use_live else pd.DataFrame()
        from app.core.scale_anchors import load_anchors
        events=apply_normalized_flow_scores(events,load_anchors(alpaca_data.DATA_DIR,symbol,expiry_mode) or {})
    except Exception: events=pd.DataFrame()
    selected_events=filter_events(events,info); flow=flow_session_summary(selected_events)
    try: large_df=load_large_prints(datetime.now(EC).date(),symbol) if use_live else pd.DataFrame()
    except Exception: large_df=pd.DataFrame()
    vol=_volatility_metrics(gd,pd.DataFrame()); pos=_positioning_metrics(gd)
    try: calib=calibration_report(alpaca_data.DATA_DIR,symbol,min_samples=CALIBRATION_MIN_SAMPLES,probability_expiry_mode=expiry_mode)
    except Exception: calib={"ready":False,"status":"COLLECTING","sample_size":0}
    regime_ctx=classify_regime(gd,vol,flow)
    regime_name=str(regime_ctx.get("regime","")); samples=float(calib.get("sample_size",0) or 0)
    sample_factor=float(np.clip(samples/max(float(CALIBRATION_MIN_SAMPLES),1.0),0,1)); base=dict(regime_ctx.get("profile",{}) or {})
    cal_mult=float((calib.get("calibrated_regime_multipliers_shadow") or {}).get(regime_name,1.0) or 1.0); blend=max(0.35,sample_factor)
    regime_ctx["profile_base"]=base; regime_ctx["profile"]={k:float(np.clip(1.0+(float(v)-1.0)*blend*cal_mult,0.82,1.18)) for k,v in base.items()}
    regime_ctx["calibration_ready"]=bool(calib.get("ready") and (calib.get("walk_forward") or {}).get("ready")); regime_ctx["calibration_samples"]=int(samples)
    try: macro=fetch_macro_context(force=False,max_cache_minutes=30)
    except Exception: macro={}
    macro=dict(macro or {}); macro["asset_context"]=_asset_macro_context(macro,symbol,regime_name)
    today=datetime.now(NY).date().isoformat(); pre=_read_premarket_store().get(f"{symbol}:{today}")
    provider_features = FEATURE_BUS.snapshot(symbol)
    scanner=build_quant_scanner(symbol,dict(gd),flow,vol,pos,selected_events,large_df,pre,macro,meta.get("market_state",""),previous=previous,live_ticks=pd.DataFrame(),regime_context=regime_ctx,expiry_mode=expiry_mode,provider_features=provider_features)
    scanner["expiry_window"]=info
    dq=build_data_quality(snapshot,_quality_meta_with_live_underlying(meta, symbol) if use_live else meta,gd,info,is_replay=False)
    try: greek_diag=_greeks_diagnostics(gd); amer=american_model_check(_latest_snapshot(selected),symbol)
    except Exception: greek_diag={}; amer={}
    mh=build_model_health(selected,gd,scanner,info,snapshot=snapshot,greeks_diag=greek_diag,american_diag=amer)
    # Same quality gate semantics as the primary Scanner. Direction is never changed here.
    original=str(scanner.get("edge_state","NO EDGE")); dq_score=float(dq.get("score",0) or 0); mh_score=float(mh.get("score",0) or 0)
    if dq_score<50 or mh_score<60: scanner["edge_state"]="NO EDGE"
    elif (dq_score<70 or mh_score<75) and original=="ACTIONABLE": scanner["edge_state"]="CAUTION"
    scanner["quality_gate"]={"before":original,"after":scanner.get("edge_state"),"data_quality":dq_score,"model_health":mh_score,"reasons":[]}
    _apply_freshness_publication_gate(scanner,dq)
    return {"symbol":symbol,"scanner":scanner,"spot":gd.get("spot"),"data_quality":dq_score,"model_health":mh_score,"expiry_label":info.get("label"),"as_of":datetime.now(EC)}


def _apply_freshness_publication_gate(scanner: Dict[str, Any], dq: Dict[str, Any], reasons: list[str] | None = None) -> bool:
    """Make stale critical data non-actionable without inventing a new direction.

    The Scanner may still preserve its internally-computed direction for audit, but it
    is marked not-ready and NO EDGE so Command/Sophia/presentation cannot treat it as
    an actionable LIVE signal. Replay is explicitly bypassed by the freshness module.
    """
    circuit=(dq or {}).get("circuito_frescura") if isinstance(dq,dict) else None
    allowed=bool(isinstance(circuit,dict) and circuit.get("publicar_permitido") is True)
    gate=scanner.setdefault("quality_gate",{})
    gate["freshness_gate"]=dict(circuit or {})
    if allowed:
        scanner["publication_blocked"]=False
        return True
    reason=str((circuit or {}).get("motivo") or "frescura critica no verificable")
    original_direction=scanner.get("direction")
    scanner["publication_blocked"]=True
    scanner["publication_block_reason"]=reason
    scanner["suppressed_direction"]=original_direction
    scanner["ready"]=False
    scanner["edge_state"]="NO EDGE"
    scanner["reason"]="PUBLICACION BLOQUEADA · DATOS RANCIOS/NO VERIFICABLES"
    gate["after"]="NO EDGE"
    gate.setdefault("reasons",[])
    if "FRESHNESS CIRCUIT OPEN" not in gate["reasons"]:
        gate["reasons"].append("FRESHNESS CIRCUIT OPEN")
    if reasons is not None and "FRESHNESS CIRCUIT OPEN" not in reasons:
        reasons.append("FRESHNESS CIRCUIT OPEN")
    return False


def _gamma_view_from_gamma_delta(gd: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the legacy Gamma-only view without repeating the expensive structure pass.

    ``analyze_gamma_delta`` is a strict superset of ``analyze``.  Only the audit
    differs, so regenerate that cheap audit to preserve observable compatibility.
    """
    view = dict(gd)
    try:
        a = _engine_audit(gd["enriched"], gd)
        view["audit"] = a
        view["data_quality"] = a.attrs["quality_score"]
    except Exception as exc:
        _obs_note("service:gamma_view_audit", exc, severity="DEGRADED")
    return view


# =====================================================================
# v1.27.15 · ATOMIC PUBLISHED STATE VIEW
# A refresh updates correlated fields across many assignments and may perform I/O
# between them. Readers must never observe a cross-section assembled from two
# different refresh generations.  A StateView is built under the writer lock and
# published with one attribute rebind; readers take that one reference without
# blocking the writer.
# =====================================================================
VIEW_FIELDS: tuple[str, ...] = (
    'symbol','symbol_epoch','mode','expiry_window','last_refresh_ec','last_error',
    'history','snapshot','meta','gamma','gamma_delta','flow_events','flow_summary',
    'vol','positioning','targets','command','chain_insights','macro','large_prints',
    'large_print_summary','premarket_tape','session_flow_tape','scanner',
    'model_controls_report','data_quality_report','model_health','regime_context','trace_attribution',
    'what_changed_rows','calibration','exposure_scenarios_report','american_model_report',
    'greeks_diagnostics','session_memory_report','dealer_intelligence_report',
    'external_market_report','source_health_report','source_fusion_report',
    'research_storage_report','institutional_research_report','tape_archive_report',
    'audit_persistence_report','market_state_field','market_truth_report',
    'feature_intelligence_report','research_validation_report','decision_intelligence_report',
    'decision_compare_snapshot','causality_report','temporal_truth_report',
    'derivatives_intelligence_report','expiry_intelligence_report','structural_intelligence_report',
    'profile_bundle_report','versioned_market_state_report','operational_readiness_report',
    'scenario_lab_report','quantum_shadow_report','flow_kinematics_report',
    'premarket_analysis_report','trace_orderflow_report','expiry_info','expiry_confluence_data',
    'analytics_revision','replay_context','asset_warmup',
)

def _qd_expiry_selection():
    """El registro de vencimientos, tomado del módulo en cada uso."""
    from .providers.quantdata import shared as _qd
    return _qd.EXPIRY_SELECTION


def _publicar_vencimientos(symbol: str, expiry_info: Dict[str, Any] | None) -> None:
    """Pasa al catálogo del proveedor los vencimientos que la terminal aplica.

    Puente de una sola dirección y sin lógica propia a propósito: el carril de
    páginas necesita el vencimiento para `max-pain`, y la única fuente legítima
    es la ventana que ya se aplicó a la cadena. Cualquier otra sería inventarse
    una fecha, y un max pain del vencimiento equivocado es un número creíble y
    falso, que es peor que no tener número.
    """
    try:
        # Del MÓDULO, no por referencia: `tools` lo lee así, y atarlo aquí por
        # referencia haría que los dos lados apuntaran a registros distintos.
        from .providers.quantdata import shared as _qd
        _qd.EXPIRY_SELECTION.publicar(symbol, list((expiry_info or {}).get("expirations") or []))
    except Exception as exc:
        _obs_note("service:publicar_vencimientos", exc)


@dataclass(frozen=True)
class StateView:
    generation: int
    stage: str
    published_at: datetime
    _fields: Dict[str, Any] = dc_field(default_factory=dict, repr=False)

    def get(self, name: str, default: Any = None) -> Any:
        return self._fields.get(name, default)

    def __getitem__(self, name: str) -> Any:
        return self._fields[name]

    def __contains__(self, name: str) -> bool:
        return name in self._fields

    @property
    def symbol(self) -> str:
        return str(self._fields.get('symbol') or '')

    @property
    def symbol_epoch(self) -> int:
        return int(self._fields.get('symbol_epoch') or 0)

    @property
    def ready(self) -> bool:
        return bool(self._fields.get('gamma_delta'))

_EMPTY_VIEW = StateView(
    generation=0,
    stage='EMPTY',
    published_at=datetime.min.replace(tzinfo=timezone.utc),
    _fields={},
)


@dataclass
class PlatformState:
    symbol: str = DEFAULT_ASSET
    symbol_epoch: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)
    _published_view: StateView = field(default_factory=lambda: _EMPTY_VIEW, repr=False)
    _view_generation: int = 0
    _active_pair: tuple[str, int] = field(default_factory=lambda: (DEFAULT_ASSET, 0), repr=False)
    history: pd.DataFrame = field(default_factory=pd.DataFrame)
    snapshot: pd.DataFrame = field(default_factory=pd.DataFrame)
    meta: Dict[str, Any] = field(default_factory=dict)
    gamma: Dict[str, Any] = field(default_factory=dict)
    gamma_delta: Dict[str, Any] = field(default_factory=dict)
    flow_events: pd.DataFrame = field(default_factory=pd.DataFrame)
    flow_summary: Dict[str, Any] = field(default_factory=dict)
    vol: Dict[str, Any] = field(default_factory=dict)
    positioning: Dict[str, Any] = field(default_factory=dict)
    targets: Dict[str, Any] = field(default_factory=dict)
    command: Dict[str, Any] = field(default_factory=dict)
    chain_insights: Dict[str, Any] = field(default_factory=dict)
    macro: Dict[str, Any] = field(default_factory=dict)
    large_prints: pd.DataFrame = field(default_factory=pd.DataFrame)
    large_print_summary: Dict[str, Any] = field(default_factory=dict)
    premarket_tape: Dict[str, Any] = field(default_factory=dict)
    session_flow_tape: Dict[str, Any] = field(default_factory=dict)
    scanner: Dict[str, Any] = field(default_factory=dict)
    #: IV evaluada contrato a contrato y ajuste SSVI de la cadena, para los
    #: controles del Auditor. Se calcula donde vive la cadena.
    model_controls_report: Dict[str, Any] = field(default_factory=dict)
    data_quality_report: Dict[str, Any] = field(default_factory=dict)
    model_health: Dict[str, Any] = field(default_factory=dict)
    regime_context: Dict[str, Any] = field(default_factory=dict)
    trace_attribution: Dict[str, Any] = field(default_factory=dict)
    what_changed_rows: list[Dict[str, Any]] = field(default_factory=list)
    calibration: Dict[str, Any] = field(default_factory=dict)
    exposure_scenarios_report: Dict[str, Any] = field(default_factory=dict)
    american_model_report: Dict[str, Any] = field(default_factory=dict)
    greeks_diagnostics: Dict[str, Any] = field(default_factory=dict)
    session_memory_report: Dict[str, Any] = field(default_factory=dict)
    dealer_intelligence_report: Dict[str, Any] = field(default_factory=dict)
    external_market_report: Dict[str, Any] = field(default_factory=dict)
    source_health_report: Dict[str, Any] = field(default_factory=dict)
    source_fusion_report: Dict[str, Any] = field(default_factory=dict)
    research_storage_report: Dict[str, Any] = field(default_factory=dict)
    institutional_research_report: Dict[str, Any] = field(default_factory=dict)
    premarket_analysis_report: Dict[str, Any] = field(default_factory=dict)
    trace_orderflow_report: Dict[str, Any] = field(default_factory=dict)
    replay_context: ReplayContext = field(default_factory=lambda: ReplayContext.live(DEFAULT_ASSET))
    replay_cache: Dict[str, Any] = field(default_factory=dict)
    trace_session_cache: Dict[str, Any] = field(default_factory=dict)
    tape_archive_report: Dict[str, Any] = field(default_factory=dict)
    audit_persistence_report: Dict[str, Any] = field(default_factory=dict)
    market_state_field: Dict[str, Any] = field(default_factory=dict)
    market_truth_report: Dict[str, Any] = field(default_factory=dict)
    feature_intelligence_report: Dict[str, Any] = field(default_factory=dict)
    research_validation_report: Dict[str, Any] = field(default_factory=dict)
    decision_intelligence_report: Dict[str, Any] = field(default_factory=dict)
    decision_compare_snapshot: Dict[str, Any] = field(default_factory=dict)
    causality_report: Dict[str, Any] = field(default_factory=dict)
    temporal_truth_report: Dict[str, Any] = field(default_factory=dict)
    derivatives_intelligence_report: Dict[str, Any] = field(default_factory=dict)
    expiry_intelligence_report: Dict[str, Any] = field(default_factory=dict)
    structural_intelligence_report: Dict[str, Any] = field(default_factory=dict)
    profile_bundle_report: Dict[str, Any] = field(default_factory=dict)
    versioned_market_state_report: Dict[str, Any] = field(default_factory=dict)
    operational_readiness_report: Dict[str, Any] = field(default_factory=dict)
    scenario_lab_report: Dict[str, Any] = field(default_factory=dict)
    quantum_shadow_report: Dict[str, Any] = field(default_factory=dict)
    flow_kinematics_report: Dict[str, Any] = field(default_factory=dict)
    last_tape_archive_ec: Optional[datetime] = None
    # Last canonical 1m candle archived per symbol. This lets Tastytrade's initial
    # historical Candle hydration seed Replay once, then keeps writes incremental.
    historical_candle_cursor: Dict[str, str] = field(default_factory=dict)
    board_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    board_scanner_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    board_cursor: int = 0
    expiry_window: str = WINDOW_AUTO
    expiry_info: Dict[str, Any] = field(default_factory=dict)
    expiry_confluence_data: Dict[str, Any] = field(default_factory=dict)
    last_refresh_ec: Optional[datetime] = None
    last_error: Optional[str] = None
    mode: str = "DEMO"
    asset_warmup: Dict[str, Any] = field(default_factory=dict)
    analytics_revision: int = 0
    _public_state_cache: Dict[str, Any] = field(default_factory=dict)
    surface_payload_cache: Dict[str, Any] = field(default_factory=dict)
    surface_slice_cache: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Keep the (symbol, epoch) pair coherent from object construction, including
        # isolated workers created with a non-default symbol.
        self._active_pair = (str(self.symbol), int(self.symbol_epoch))
        self.publish_view("INIT")

    def current_publication_gate(self) -> Dict[str, Any]:
        """Evaluate freshness against the current observed underlying clock.

        Heavy chain refreshes are intentionally slower than the LIVE price lane,
        especially in PREMARKET.  Publication must therefore follow two independent
        clocks: the structural option timestamp and the freshest valid underlying
        quote/trade already observed by the provider fabric.  This keeps the 20 s
        safety limit intact without blanking TRACE between structural refreshes.
        """
        with self.lock:
            meta = dict(self.meta or {})
            symbol = str(self.symbol)
            replay = bool(self.replay_context.is_replay)
        if replay:
            return evaluate_publication_gate(
                meta, market_state=str(meta.get("market_state") or "REPLAY"),
                is_replay=replay,
            )
        qmeta = _quality_meta_with_live_underlying(meta, symbol)
        gate = evaluate_publication_gate(
            qmeta, market_state=str(qmeta.get("market_state") or ""),
            is_replay=False,
        )
        gate = dict(gate or {})
        gate["underlying_freshness_source"] = qmeta.get("underlying_freshness_source")
        gate["effective_stock_market_timestamp"] = qmeta.get("stock_market_timestamp")
        gate["structural_stock_market_timestamp"] = qmeta.get("structural_stock_market_timestamp")
        return gate

    def _invalidate_visual_caches_locked(self) -> None:
        """Invalidate only presentation caches after analytical state changes.

        Must be called while ``self.lock`` is held. It never mutates Scanner, model,
        Replay, calibration or persisted research state.
        """
        self.analytics_revision = int(self.analytics_revision) + 1
        self.surface_payload_cache.clear()
        self.surface_slice_cache.clear()

    # ------------------------------------------------------------------
    # v1.27.1 · UNIFICACIÓN DE ENTRADAS ANALÍTICAS
    #
    # `refresh()` y `set_expiry_window()` recalculan el MISMO bloque analítico,
    # pero habían DIVERGIDO. Diferencias reales encontradas en v1.27.0:
    #
    #   entrada        refresh()                          set_expiry_window()
    #   -----------    --------------------------------   -----------------------------
    #   macro          fetch_macro_context(30 min)        self.macro  (PODÍA SER STALE)
    #   source_diag    OPTION_FLOW_FABRIC.scheduler_...   OPTION_STREAM.status()
    #   large prints   large_df (recién cargado)          self.large_prints (cacheado)
    #
    # Consecuencia observable: cambiar la ventana de expiración devolvía un
    # diagnóstico de salud de fuentes y un contexto macro DISTINTOS a los del
    # refresco normal, sin que nada lo indicara. Estos tres helpers son ahora la
    # única fuente de verdad; ambos caminos deben llamarlos.
    # ------------------------------------------------------------------

    def _current_macro(self, *, max_cache_minutes: int = 30) -> Dict[str, Any]:
        """Contexto macro compartido; LIVE refresca, DEMO nunca hace red.

        `set_expiry_window` y `refresh` comparten esta única ruta. En LIVE se usa
        cache/fetch oficial con límite de frescura. En DEMO se devuelve exclusivamente
        estado local determinista: una suite offline nunca puede quedarse bloqueada en
        DNS/FRED/BLS/Fed ni confundir ausencia de Internet con mercado tranquilo.
        """
        if str(self.mode or "").upper() == "DEMO":
            if self.macro:
                return dict(self.macro)
            return {
                "updated_ec": datetime.now(EC).isoformat(),
                "series": {}, "events": [],
                "stress": {"score": 0.0, "label": "DEMO", "components": {}},
                "errors": [],
                "source_note": "DEMO_OFFLINE_NO_NETWORK · macro oficial se consulta solo en LIVE.",
            }
        return guard(
            "service.macro_context",
            lambda: fetch_macro_context(force=False, max_cache_minutes=max_cache_minutes),
            default=dict(self.macro or {}),
        ) or dict(self.macro or {})

    def _build_source_diag(self, meta: Dict[str, Any], external_diag: Any) -> Any:
        """Salud de proveedores. UNA sola definición para los dos caminos.

        `refresh()` usaba el scheduler del fabric y `set_expiry_window()` el status
        del stream: dos métricas distintas presentadas con la misma etiqueta.
        Se fija el fabric (es el que ve el flujo real de opciones) y el status del
        stream queda como respaldo si el fabric no responde.
        """
        option_health = guard(
            "service.option_flow_health",
            lambda: OPTION_FLOW_FABRIC.scheduler_status(self.symbol),
            default=None,
        )
        if not option_health:
            option_health = guard("service.option_stream_status",
                                  lambda: OPTION_STREAM.status(), default={})
        return provider_redundancy(meta or {}, PRICE_STREAM.status(), option_health,
                                   external_diag, alpaca_data.DATA_DIR)

    def _current_large_prints(self, fresh: "pd.DataFrame | None" = None) -> "pd.DataFrame":
        """Large prints con la misma semántica en ambos caminos."""
        if isinstance(fresh, pd.DataFrame) and not fresh.empty:
            return fresh
        lp = self.large_prints
        return lp if isinstance(lp, pd.DataFrame) else pd.DataFrame()

    def refresh(self, fetch_flow: bool = True, isolated: bool = False, core_ready_callback=None) -> None:
        progress_cb=getattr(self,"warm_progress_callback",None)
        def _progress(phase: str, pct: int, detail: str) -> None:
            if callable(progress_cb):
                try: progress_cb(str(phase),int(pct),str(detail))
                except Exception as _e:
                    _obs_note('service:1303', _e)
        with self.lock:
            try:
                _progress("CHAIN_DISCOVERY",30,"Resolviendo cadena propia y proveedores disponibles")
                previous_gd = self.gamma_delta if self.gamma_delta else None
                previous_scanner = self.scanner if self.scanner else None
                cfg=asset_info(self.symbol)
                if not cfg.get("full",False):
                    raise RuntimeError(f"{self.symbol} está visible en la plataforma, pero todavía requiere una fuente directa de índice/opciones. No se usa proxy oculto.")
                use_live = DATA_MODE == "live" or (DATA_MODE == "auto" and _live_provider_configured(self.symbol))
                legacy_window=float(cfg.get("window",STRIKE_WINDOW))
                expiry=_expiry_load_days(self.expiry_window)
                if use_live:
                    # INSTANT BOOT: start from the last validated sigma-sized window when available.
                    # This preserves the full adaptive math while avoiding the old pattern of always
                    # downloading a legacy window first and then downloading the same chain again.
                    initial_window, initial_window_source = get_chain_window_hint(self.symbol, self.expiry_window, legacy_window)
                    snapshot, meta = _fetch_asset_quant_snapshot(self.symbol, initial_window, expiry)
                    _progress("CHAIN_READY",44,f"Cadena propia recibida · {len(snapshot)} contratos")
                    iv_pct=_snapshot_atm_iv_pct(snapshot)
                    hinfo=effective_horizon_days(snapshot,self.expiry_window)
                    spot0, spot0_source = _resolve_chain_spot(snapshot, self.symbol)
                    meta["chain_spot_source"] = spot0_source
                    win_info=chain_window_for(self.symbol,spot0,iv_pct,hinfo.get("days") if hinfo.get("ready") else None)
                    target_window=float(win_info.get("window",initial_window))
                    meta["initial_window"]=float(initial_window)
                    meta["initial_window_source"]=initial_window_source
                    # A second request is now exceptional rather than the default. It is used only
                    # when the fresh chain proves that the persisted/fallback window is materially
                    # wrong. Once validated, the target is persisted for the next boot.
                    if win_info.get("universal") and abs(target_window-initial_window)/max(initial_window,1e-9)>0.15:
                        try:
                            snapshot2, meta2=_fetch_asset_quant_snapshot(self.symbol,target_window,expiry)
                            snapshot,meta=snapshot2,meta2
                            meta["initial_window"]=float(initial_window)
                            meta["initial_window_source"]=initial_window_source
                            meta["sigma_refetch_performed"]=True
                        except Exception as exc:
                            meta["sigma_refetch_error"]=str(exc)[:180]
                            win_info["method"] += " · REFRESH FALLBACK"
                    else:
                        meta["sigma_refetch_performed"]=False
                    save_chain_window_hint(self.symbol, self.expiry_window, target_window, source=str(win_info.get("method") or "VALIDATED_CHAIN"))
                    meta["requested_window"]=target_window
                    meta["effective_window"]=target_window if win_info.get("universal") and "sigma_refetch_error" not in meta else initial_window
                    meta["effective_expiry_days"]=expiry
                    meta["chain_window_method"]=win_info.get("method")
                    meta["chain_horizon_days"]=hinfo.get("days")
                    meta["chain_horizon_source"]=hinfo.get("source")
                    if not isolated:
                        try: OPTION_STREAM.set_universe(snapshot)
                        except Exception as _e:
                            _obs_note('service:1353', _e)
                    append_history(snapshot, self.symbol)  # legacy compatibility mirror
                    try:
                        # Archive the chain as one observation snapshot. Provider event
                        # times remain in their provenance columns, while `timestamp` is
                        # the causal instant at which ITM QUANT observed this full state.
                        _archive_snapshot = snapshot.copy()
                        if "timestamp" in _archive_snapshot.columns:
                            _archive_snapshot["source_timestamp"] = _archive_snapshot["timestamp"]
                        _archive_snapshot["timestamp"] = datetime.now(EC).replace(tzinfo=None)
                        HistoricalSessionStore(alpaca_data.DATA_DIR).append_frame(
                            self.symbol, "option_chain", _archive_snapshot,
                            source=str(meta.get("source") or meta.get("option_feed") or "UNKNOWN"),
                        )
                    except Exception as _e:
                        _obs_note("service:historical_option_chain_archive", _e, severity="DEGRADED")
                    if not isolated:
                        try:
                            self.archive_tape(force=True)
                        except Exception as _e:
                            _obs_note('service:1358', _e)
                    history = load_history(datetime.now(EC).date(), self.symbol)
                    history = _trim_history(history, HOT_SNAPSHOTS)
                    self.mode = "LIVE"
                else:
                    history = _demo_history(self.symbol)
                    snapshot = _latest_snapshot(history)
                    meta = {
                        "source": "DEMO INTEGRADO", "symbol": self.symbol, "stock_feed": "DEMO", "option_feed": "DEMO",
                        "spot": float(snapshot["underlying_price"].iloc[-1]), "contracts": int(len(snapshot)),
                        "matched_snapshots": int(len(snapshot)), "provider_iv_count": int(len(snapshot)), "fallback_iv_count": 0, "unusable_iv_count": 0,
                        "unique_strikes": int(snapshot["strike"].nunique()), "expirations": int(snapshot.get("expiration_date", pd.Series(["demo"])).nunique()),
                        "market_state": "DEMO", "snapshot_ec": datetime.now(EC).isoformat(),
                        # v1.35: without these two keys, the freshness-gate helpers in
                        # precision_engine/freshness.py read age_of(None) -> UNKNOWN
                        # ("timestamp ausente o no interpretable") forever, latching the
                        # publication circuit OPEN and blocking every DEMO TRACE/GEX/DEX
                        # payload permanently -- DEMO never had a live provider timestamp
                        # to report, so the assembly time IS the honest observation time
                        # for synthetic data.
                        "latest_option_market_timestamp": datetime.now(EC).isoformat(),
                        "stock_market_timestamp": datetime.now(EC).isoformat(),
                    }
                    self.mode = "DEMO"
                # v1.11 Global Expiry Window: keep the full chain/history, then rebuild every
                # option-dependent module from the selected horizon. AUTO applies adaptive weights.
                selected_history, expiry_info = apply_expiry_window(history, self.expiry_window)
                if selected_history.empty:
                    # A strict window such as 0DTE may legitimately have no contracts. Keep state readable.
                    raise RuntimeError(f"{self.symbol}: no hay contratos para {LABELS.get(self.expiry_window,self.expiry_window)} en la cadena cargada.")
                # Historical magnitude anchors are strictly backward-looking. Promote
                # yesterday's staged distribution before today's calculation; today's
                # snapshots remain pending until a later NY session begins.
                if use_live:
                    try:
                        from app.core.scale_anchors import promote_pending_anchors
                        promote_pending_anchors(alpaca_data.DATA_DIR, self.symbol, datetime.now(NY).date().isoformat(), self.expiry_window)
                    except Exception as _e:
                        _obs_note('service:1387', _e)
                _progress("GREEKS_STRUCTURE",52,"Calculando Greeks, Gamma/Delta y estructura por strike")
                _cfg_engine = engine_config_for_asset(self.symbol, self.expiry_window)
                gd = analyze_gamma_delta(selected_history, _cfg_engine)
                gamma = _gamma_view_from_gamma_delta(gd)
                confluence = expiry_confluence(history, gd.get("spot"))
                _progress("GREEKS_READY",61,"Gamma/Delta y estructura principal disponibles")
                events = self.flow_events
                rust_ticks = pd.DataFrame()
                rust_market = {"option_trades": pd.DataFrame(), "price_ticks": pd.DataFrame(), "events": 0}
                if use_live:
                    # v1.26.2 progressive warm workers are isolated from shared hot streams.
                    # They build a complete symbol state in the background without consuming
                    # another symbol's Rust/OPRA queue or replacing the live option universe.
                    if not isolated:
                        try:
                            rust_market = RUST_CAUSAL_BRIDGE.drain_market_data(self.symbol, limit=100_000)
                            rust_opt = rust_market.get("option_trades")
                            rust_ticks = rust_market.get("price_ticks")
                            if isinstance(rust_opt, pd.DataFrame) and not rust_opt.empty:
                                try: save_flow_events(rust_opt, self.symbol)
                                except Exception as _e:
                                    _obs_note('service:1408', _e)
                                if isinstance(events, pd.DataFrame) and not events.empty:
                                    events = pd.concat([events, rust_opt], ignore_index=True, sort=False)
                                else:
                                    events = rust_opt.copy()
                        except Exception as _rust_exc:
                            rust_market = {"option_trades": pd.DataFrame(), "price_ticks": pd.DataFrame(), "events": 0, "error": str(_rust_exc)[:180]}
                            rust_ticks = pd.DataFrame()
                    if fetch_flow and meta.get("market_state") == "REGULAR":
                        try:
                            fetch_recent_option_trades(snapshot, lookback_seconds=40, max_contracts=500)
                        except Exception as _e:
                            _obs_note('service:1419', _e)
                    events = load_flow_events(datetime.now(EC).date(), self.symbol)
                    if not isolated:
                        try:
                            ws_events = OPTION_FLOW_FABRIC.dataframe(self.symbol, 30)
                            if not ws_events.empty:
                                save_flow_events(ws_events, self.symbol)
                                events = load_flow_events(datetime.now(EC).date(), self.symbol)
                        except Exception as _e:
                            _obs_note('service:1428', _e)
                # Consolidate observed underlying ticks for causal diagnostics, Dealer
                # Intelligence and Scanner timing.  Rust ticks are preferred only when
                # actually present; both sources keep their provenance for audit/replay.
                live_ticks = _live_ticks_for(self.symbol)
                if isinstance(rust_ticks, pd.DataFrame) and not rust_ticks.empty:
                    if isinstance(live_ticks, pd.DataFrame) and not live_ticks.empty:
                        live_ticks = pd.concat([live_ticks, rust_ticks], ignore_index=True, sort=False)
                        if "timestamp" in live_ticks.columns:
                            live_ticks["timestamp"] = pd.to_datetime(live_ticks["timestamp"], errors="coerce")
                            live_ticks = live_ticks.dropna(subset=["timestamp"]).sort_values(["timestamp", "seq"] if "seq" in live_ticks.columns else ["timestamp"], kind="mergesort")
                            # Transitional Python→Rust forwarding can make the same observed
                            # trade visible on both transports with different local sequence
                            # numbers. Dedupe on the market observation, never on transport seq.
                            _tick_dedupe=[c for c in ("timestamp","price","size","exchange") if c in live_ticks.columns]
                            live_ticks = live_ticks.drop_duplicates(_tick_dedupe or ["timestamp"], keep="last").reset_index(drop=True)
                    else:
                        live_ticks = rust_ticks.copy()

                # v1.15.7: normalized Q-Flow is SHADOW only. The legacy `flow_score`
                # remains the Scanner input until LIVE/OOS validation earns promotion.
                try:
                    from app.core.scale_anchors import load_anchors
                    _flow_anchors=load_anchors(alpaca_data.DATA_DIR,self.symbol,self.expiry_window) or {}
                except Exception:
                    _flow_anchors={}
                events=apply_normalized_flow_scores(events,_flow_anchors)
                events=causal_sort_frame(events)
                if use_live and isinstance(events,pd.DataFrame) and not events.empty:
                    try: save_flow_events(events,self.symbol)
                    except Exception as _e:
                        _obs_note('service:1459', _e)
                selected_events = causal_sort_frame(filter_events(events, expiry_info))
                try:
                    _causal = unify_market_events(symbol=self.symbol, price_ticks=live_ticks.tail(3000), option_events=selected_events.tail(3000), process_time=datetime.now(EC), max_lateness_ms=350.0)
                    self.causality_report = {**(_causal.get("status") or {}), "event_count": _causal.get("count",0), "causal": True, "latency": latency_summary(_causal), "rust_bridge": RUST_CAUSAL_BRIDGE.status(), "rust_batch_events": int(rust_market.get("events",0) or 0)}
                except Exception as _ce:
                    self.causality_report = {"causal": False, "error": str(_ce)[:160]}
                flow = flow_session_summary(selected_events)
                # Session-aware unusual activity is independent from the US option clock.
                # It starts at the DST-aware London open for every selected instrument and
                # uses only same-instrument observations. Real option trades are merged by
                # OPTION_FLOW_FABRIC whenever they actually exist; no OPRA event is invented.
                session_tape = self.session_flow_tape
                if use_live and not isolated:
                    try:
                        session_tape = london_activity_tape_summary(self.symbol, live_ticks=live_ticks)
                    except Exception as _session_flow_exc:
                        session_tape = {"state":"NO_DATA","symbol":self.symbol,"error":str(_session_flow_exc)[:180],"bars":pd.DataFrame()}
                tape = self.premarket_tape
                if use_live and (fetch_flow or meta.get("market_state") == "PREMARKET"):
                    try:
                        tape = premarket_tape_summary(self.symbol)
                    except Exception:
                        tape = self.premarket_tape or {}
                large_df = self.large_prints
                if use_live:
                    try:
                        from app.core.scale_anchors import promote_pending_anchors
                        promote_pending_anchors(alpaca_data.DATA_DIR,self.symbol,datetime.now(NY).date().isoformat(),"LARGE_PRINTS")
                    except Exception as _e:
                        _obs_note('service:1488', _e)
                    if fetch_flow and meta.get("market_state") in {"REGULAR", "PREMARKET", "AFTERHOURS"}:
                        try:
                            large_df = fetch_recent_large_prints(lookback_seconds=120, symbol=self.symbol)
                        except Exception:
                            large_df = load_large_prints(datetime.now(EC).date(), self.symbol)
                    else:
                        try:
                            large_df = load_large_prints(datetime.now(EC).date(), self.symbol)
                        except Exception as _e:
                            _obs_note('service:1498', _e)
                large_summary = large_print_summary(large_df, self.symbol)
                if use_live:
                    try:
                        from app.core.scale_anchors import stage_session_anchors
                        _lp_samples=large_print_anchor_samples(large_df)
                        if _lp_samples:
                            stage_session_anchors(alpaca_data.DATA_DIR,self.symbol,_lp_samples,datetime.now(NY).date().isoformat(),"LARGE_PRINTS")
                    except Exception as _e:
                        _obs_note('service:1507', _e)
                macro = self._current_macro(max_cache_minutes=30)
                vol = _volatility_metrics(gd, _bars_from_ticks(_live_ticks_for(self.symbol)))
                pos = _positioning_metrics(gd)
                _progress("FLOW_POSITIONING",69,"Flujo, volatilidad y positioning calculados")
                # Stage the current NY session for TOMORROW's magnitude anchors.
                # It is deliberately not folded into today's anchor, preventing
                # cross-snapshot look-ahead/self-normalisation. DEMO never contaminates it.
                if use_live:
                    try:
                        _cur_anchor = _current_structure_frame(gd)
                        if isinstance(_cur_anchor, pd.DataFrame) and not _cur_anchor.empty:
                            from app.core.scale_anchors import stage_session_anchors
                            from app.core.trace_analytics import charm_pressure_proxy
                            _delta_anchor = pd.to_numeric(_cur_anchor.get("delta_exposure", pd.Series(dtype=float)), errors="coerce").abs().dropna().tolist()
                            _charm_anchor = []
                            _enr_anchor = gd.get("enriched")
                            if isinstance(_enr_anchor, pd.DataFrame) and not _enr_anchor.empty:
                                _ea = _enr_anchor.copy(); _ea["timestamp"] = pd.to_datetime(_ea.get("timestamp"), errors="coerce")
                                _ea = _ea.dropna(subset=["timestamp"])
                                if not _ea.empty:
                                    _ea = _ea[_ea["timestamp"] == _ea["timestamp"].max()].copy()
                                    _ea["_charm_notional"] = charm_pressure_proxy(_ea, minutes=10.0, symbol=self.symbol)
                                    if "strike" in _ea.columns:
                                        _charm_anchor = pd.to_numeric(_ea.groupby("strike")["_charm_notional"].sum(), errors="coerce").abs().dropna().tolist()
                            _anchor_samples={
                                "gross_gex": pd.to_numeric(_cur_anchor.get("gross_gex", pd.Series(dtype=float)), errors="coerce").abs().dropna().tolist(),
                                "delta_exposure": _delta_anchor,
                                "charm_exposure": _charm_anchor,
                                "open_interest": _cur_anchor.get("open_interest", pd.Series(dtype=float)).tolist(),
                                "option_volume": _cur_anchor.get("option_volume", pd.Series(dtype=float)).tolist(),
                                "gamma_intensity": _cur_anchor.get("gamma_intensity", pd.Series(dtype=float)).tolist(),
                                "turnover": _cur_anchor.get("turnover", pd.Series(dtype=float)).tolist(),
                            }
                            _anchor_samples.update(flow_anchor_samples(selected_events))
                            _anchor_samples.update(flow_bar_anchor_samples(selected_events, "1min"))
                            stage_session_anchors(alpaca_data.DATA_DIR, self.symbol, _anchor_samples,
                                                  datetime.now(NY).date().isoformat(), self.expiry_window)
                    except Exception as _e:
                        _obs_note('service:1550', _e)
                try:
                    calib = calibration_report(alpaca_data.DATA_DIR, self.symbol, min_samples=CALIBRATION_MIN_SAMPLES, probability_expiry_mode=self.expiry_window)
                except Exception as exc:
                    calib = self.calibration or {"ready":False,"status":"COLLECTING","sample_size":0,"reason":str(exc)}
                regime_ctx = classify_regime(gd, vol, flow)
                # Regime weights are dynamic, but activation is gated by actual LIVE research.
                # Until then the adaptive path remains SHADOW and cannot silently alter production.
                regime_name=str(regime_ctx.get("regime","")); samples=float(calib.get("sample_size",0) or 0)
                sample_factor=float(np.clip(samples/max(float(CALIBRATION_MIN_SAMPLES),1.0),0,1))
                base_profile=dict(regime_ctx.get("profile",{}) or {})
                cal_mult=float((calib.get("calibrated_regime_multipliers_shadow") or {}).get(regime_name,1.0) or 1.0)
                blend=max(0.35,sample_factor)
                regime_ctx["profile_base"]=base_profile
                regime_ctx["profile"]={k:float(np.clip(1.0+(float(v)-1.0)*blend*cal_mult,0.82,1.18)) for k,v in base_profile.items()}
                regime_ctx["calibration_ready"]=bool(calib.get("ready") and (calib.get("walk_forward") or {}).get("ready"))
                regime_ctx["calibration_samples"]=int(samples)
                regime_ctx["calibrated_multiplier_shadow"]=cal_mult
                macro = dict(macro or {}); macro["asset_context"] = _asset_macro_context(macro, self.symbol, regime_name)
                targets = _level_targets(gd)
                cmd = {}  # built after Scanner; Command Center never leads direction
                insights = _top_chain_insights(gd)
                greeks_diag = _greeks_diagnostics(gd)
                american_diag = american_model_check(_latest_snapshot(selected_history), self.symbol)
                exposure_diag = exposure_scenarios(gd.get("enriched", pd.DataFrame()), selected_events, self.symbol)
                # v1.14 Dealer Intelligence + source/research infrastructure. Dealer outputs
                # remain explicitly estimated and do not silently alter the Scanner score.
                external_diag = external_market_context(self.symbol, alpaca_data.DATA_DIR)
                dealer_diag = dealer_intelligence(_latest_snapshot(selected_history), selected_events, self.symbol, alpaca_data.DATA_DIR,
                    float(gd.get("spot") or meta.get("spot") or 0), underlying_ticks=live_ticks)
                _progress("DEALER_READY",74,"Dealer Flow estimado y presión de hedge disponibles")
                fusion_diag = source_context(self.symbol, alpaca_data.DATA_DIR, float(gd.get("spot") or meta.get("spot") or 0), on_demand=False)
                source_diag = self._build_source_diag(meta, external_diag)
                source_diag["fusion_sources"] = fusion_diag.get("health", [])
                source_diag["fusion_usable"] = fusion_diag.get("usable_sources", 0)
                # Quant Scanner / Structural Path Engine. It synthesizes the already-computed
                # modules; it does not duplicate their signals as independent evidence.
                scan_result = dict(gd)
                try:
                    # No fixed provider rank: use the quality-aware comparable-price
                    # consensus when it is available; otherwise retain the existing
                    # causal Rust/SIP fallback path. Futures/index observations remain
                    # separate instruments and are never mixed into ETF spot here.
                    pcons = PROVIDER_BUS.snapshot(self.symbol)
                    cp = _finite(pcons.get("consensus_price")) if pcons.get("ready") else None
                    ps = PRICE_STREAM.status()
                    lt = ps.get("last_tick") if ps.get("symbol") == self.symbol else None
                    rust_lt = RUST_CAUSAL_BRIDGE.latest_price(self.symbol)
                    lp = _finite(cp, _finite((rust_lt or {}).get("price"), _finite((lt or {}).get("price"))))
                    if lp is not None:
                        scan_result["spot"] = lp
                    scan_result["provider_consensus"] = pcons
                except Exception as _e:
                    _obs_note('service:1603', _e)
                provider_features = FEATURE_BUS.snapshot(self.symbol)
                _progress("SCANNER_CORE",78,"Construyendo Structural Path y autoridad Scanner")
                scanner = build_quant_scanner(
                    self.symbol, scan_result, flow, vol, pos, selected_events, large_df,
                    self.premarket_map(), macro, meta.get("market_state", ""), previous=self.scanner,
                    live_ticks=live_ticks, regime_context=regime_ctx, expiry_mode=self.expiry_window,
                    provider_features=provider_features
                )
                scanner["expiry_window"] = expiry_info
                scanner["expiry_confluence"] = confluence
                # Add confluence to candidate zones and to the WHY layer without changing legacy scoring semantics.
                cz = scanner.get("candidate_zones") or []
                for z in cz:
                    try:
                        k=float(z.get("strike")); hit=min(confluence.get("zones",[]), key=lambda q:abs(float(q.get("strike"))-k)) if confluence.get("zones") else None
                        if hit and abs(float(hit.get("strike"))-k) <= 0.26:
                            z["expiry_confluence"] = hit
                            z.setdefault("specials",[]).append(f"EXP {hit.get('count')}/{hit.get('of')}")
                    except Exception as _e:
                        _obs_note('service:1627', _e)
                strong=next((z for z in confluence.get("zones",[]) if z.get("multi_horizon")),None)
                if strong:
                    scanner.setdefault("reasons",[]).append({"label":"MULTI-EXPIRY CONFIRMATION — FUERTE","detail":f"Strike {strong['strike']:.2f} relevante en {' + '.join(strong.get('horizons') or [])}.","value":strong.get("score",0)})
                hp=(dealer_diag.get("hedge_pressure") or {})
                hp_dir=str(hp.get("direction","NEUTRAL")); hp_conf=float(hp.get("confidence",0) or 0)
                scan_dir=str(scanner.get("direction",""))
                scanner["dealer_intelligence_shadow"]={"state":dealer_diag.get("state"),"synthetic_dealer_gex":dealer_diag.get("synthetic_dealer_gex"),"hedge_pressure":hp,"note":"SHADOW: no modifica Evidence Score hasta calibración suficiente."}
                if hp_conf>=45 and hp_dir in {"BUY","SELL"}:
                    item={"label":"HEDGE PRESSURE SHADOW","detail":f"Estimación {hp_dir} · 15m {float(hp.get('net_15m',0) or 0):,.0f} notional · conf {hp_conf:.0f}/100","value":hp_conf}
                    if hp_dir==scan_dir: scanner.setdefault("reasons",[]).append(item)
                    else: scanner.setdefault("contradictions",[]).append(item)
                dq = build_data_quality(snapshot, _quality_meta_with_live_underlying(meta, self.symbol) if use_live else meta, gd, expiry_info, is_replay=bool(self.replay_context.is_replay))
                mh = build_model_health(selected_history, gd, scanner, expiry_info, snapshot=snapshot, greeks_diag=greeks_diag, american_diag=american_diag)
                # Quality gate changes actionability only; it never flips BUY/SELL direction.
                original_edge=str(scanner.get("edge_state","NO EDGE"));dq_score=float(dq.get("score",0) or 0);mh_score=float(mh.get("score",0) or 0)
                gate_reason=[]
                if str(gd.get("model_inputs_source") or "") == "FALLBACK_DEFAULT_CARRY":
                    scanner["edge_state"]="NO EDGE"
                    scanner["publication_blocked"] = True
                    scanner["publication_block_reason"] = "MODEL_INPUTS_FALLBACK_CARRY"
                    gate_reason.append("Carry por activo no verificable; autoridad operativa bloqueada")
                elif dq_score < 50 or mh_score < 60:
                    scanner["edge_state"]="NO EDGE";gate_reason.append("Data Quality/Model Health insuficiente")
                elif (dq_score < 70 or mh_score < 75) and original_edge=="ACTIONABLE":
                    scanner["edge_state"]="CAUTION";gate_reason.append("Calidad utilizable pero no óptima")
                if source_diag.get("data_disagreement"):
                    if scanner.get("edge_state")=="ACTIONABLE": scanner["edge_state"]="CAUTION"
                    gate_reason.append("DATA DISAGREEMENT entre proveedores")
                scanner["source_health"]={"data_disagreement":bool(source_diag.get("data_disagreement")),"additional_provider_configured":bool(source_diag.get("secondary_configured")),"legacy_secondary_configured":bool(source_diag.get("secondary_configured")),"spot_disagreement_pct":source_diag.get("spot_disagreement_pct"),"policy":"NO_FIXED_PROVIDER_RANK"}
                # Descriptive reliability for the matching regime, never presented as guaranteed probability.
                rel=None
                for rr in calib.get("regime_breakdown",[]) or []:
                    if str(rr.get("regime"))==regime_name:
                        rel={"samples":rr.get("samples",0),"positive_end_pct":rr.get("directional_positive_pct"),"expectancy":rr.get("expectancy"),"profit_factor":rr.get("profit_factor")};break
                scanner["signal_reliability"]={"status":"MEASURED" if rel and int(rel.get("samples",0) or 0)>=10 else "COLLECTING","regime":regime_name,"descriptive":rel,"note":"Histórico descriptivo del mismo régimen; no es probabilidad garantizada."}
                scanner["quality_gate"]={"before":original_edge,"after":scanner.get("edge_state"),"data_quality":dq_score,"model_health":mh_score,"reasons":gate_reason}
                _apply_freshness_publication_gate(scanner,dq,gate_reason)
                cmd = _command_center(gd, flow, vol, targets, scanner, None)
                attr = exposure_attribution(selected_history, self.symbol)
                changed = what_changed(gd, previous_gd, scanner, previous_scanner)
                self.history, self.snapshot, self.meta = history, snapshot, meta
                # v1.57.0 · Los controles del Auditor se alimentan DONDE VIVE LA CADENA.
                #
                # `audit()` se llamaba sin `iv_assessments` y sin `surface`, así
                # que dos controles avisaban con «no se evaluó la
                # identificabilidad de la IV» y «no hay ajuste de superficie».
                # Las dos frases sugerían que el motor no existía. Existe:
                # `iv_quality.assess()` y `ssvi_shadow.fit_ssvi()`, con sus
                # condiciones de Durrleman y su monotonía de calendario. Faltaba
                # el cable, y va aquí: arrastrar el snapshot hasta la capa de
                # presentación para calcularlo allí sería llevar la cadena a
                # donde no pinta nada.
                try:
                    from .core import model_controls as _MC
                    self.model_controls_report = _MC.build(
                        snapshot, symbol=self.symbol,
                        spot=gd.get("spot") if isinstance(gd, dict) else None)
                except Exception as exc:
                    _obs_note("service:model_controls", exc, severity="DEGRADED")
                    self.model_controls_report = {
                        "iv_assessments": [], "iv_count": 0, "chain_rows": 0,
                        "surface": {"ready": False,
                                    "reason": f"el cálculo falló: {type(exc).__name__}"}}
                self.gamma, self.gamma_delta = gamma, gd
                self.expiry_info, self.expiry_confluence_data = expiry_info, confluence
                # v1.57.7 · Los vencimientos que la pantalla tiene DE VERDAD, para
                # que `/v1/options/tool/max-pain` —que exige `expirationDate`—
                # pueda construir un cuerpo válido sin inventarse una fecha.
                _publicar_vencimientos(self.symbol, expiry_info)
                self.flow_events, self.flow_summary = events, flow
                try:
                    _px_live = _live_ticks_for(self.symbol) if self.mode == "LIVE" else pd.DataFrame()
                    self.flow_kinematics_report = build_flow_kinematics(events, _px_live, symbol=self.symbol)
                except Exception as exc:
                    self.flow_kinematics_report = {"ready":False,"symbol":self.symbol,"authority":"SHADOW_CONTEXT_ONLY","status":"ERROR","error":str(exc)[:160]}
                self.vol, self.positioning, self.targets, self.command, self.chain_insights = vol, pos, targets, cmd, insights
                self.scanner = scanner
                self.data_quality_report, self.model_health = dq, mh
                self.regime_context, self.trace_attribution = regime_ctx, attr
                self.what_changed_rows, self.calibration = changed, calib
                self.exposure_scenarios_report, self.american_model_report, self.greeks_diagnostics = exposure_diag, american_diag, greeks_diag
                self.dealer_intelligence_report, self.external_market_report, self.source_health_report, self.source_fusion_report = dealer_diag, external_diag, source_diag, fusion_diag
                if self.mode == "LIVE":
                    save_scanner_snapshot(scanner)
                    try: append_session_metric(alpaca_data.DATA_DIR, self.symbol, gd, scanner, self.expiry_window, vol)
                    except Exception as _e:
                        _obs_note('service:1681', _e)
                    _archive_option_replay(self.symbol, self.snapshot, self.mode)
                self.session_memory_report = session_memory_summary(alpaca_data.DATA_DIR, self.symbol, self.expiry_window) if self.mode == "LIVE" else {"ready":False,"observations":0,"note":"DEMO no contamina Session Memory."}
                self.macro, self.large_prints, self.large_print_summary = macro, large_df, large_summary
                self.premarket_tape = tape or {}
                self.session_flow_tape = session_tape or {}
                self.last_refresh_ec = datetime.now(EC)
                self.last_error = None
                # INSTANT BOOT boundary: at this exact point provider chain, Greeks,
                # Gamma/Delta, Data Quality and the authoritative Scanner already exist.
                # A symbol warm worker may publish this CORE state immediately while the
                # explanatory/research layers below continue on the isolated worker.
                if callable(core_ready_callback):
                    try:
                        self.publish_view("CORE")
                        core_ready_callback(self)
                        _progress("CORE_PUBLISHED",84,"Scanner, Dealer y Positioning publicados; Research continúa")
                    except Exception as _e:
                        _obs_note('service:1696', _e)
                # Internal all-engine state equation. No new UI section and no authority
                # to flip Scanner direction. This structural snapshot is persisted so
                # Calibration/Research can later test whether context added value OOS.
                try:
                    self.market_state_field = build_market_state_field(
                        scanner, gd, flow, vol, {}, symbol=self.symbol, dealer=dealer_diag,
                        external=external_diag, positioning=pos, macro=macro,
                    )
                except Exception as exc:
                    self.market_state_field = {"ready":False,"role":"INTERNAL_CONTEXT_ONLY","error":str(exc)[:160]}
                # v1.26.2 NEXTGEN: Market Truth + Feature/Decision Intelligence are
                # audit/explanation layers. Scanner remains the sole direction authority.
                try:
                    self.market_truth_report = MARKET_TRUTH.snapshot(self.symbol)
                    self.feature_intelligence_report = build_feature_intelligence(
                        feature_snapshot=FEATURE_BUS.snapshot(self.symbol), market_state=self.market_state_field,
                        regime_context=self.regime_context or {}, calibration=self.calibration or {}, scanner=scanner)
                    self.research_validation_report = build_research_validation(self.calibration or {}, self.research_storage_report or {}, scanner)
                    _syn_refresh = build_quant_synthesis(scanner, gd, flow, vol, {}, spot=gd.get("spot"),
                        data_quality=dq.get("score"), model_health=mh.get("score"), source_health=source_diag,
                        dealer=dealer_diag, external=external_diag, positioning=pos, macro=macro, state_field=self.market_state_field)
                    self.decision_intelligence_report = build_decision_intelligence(
                        scanner=scanner, quant_synthesis=_syn_refresh, market_truth=self.market_truth_report,
                        feature_intelligence=self.feature_intelligence_report, research_validation=self.research_validation_report,
                        market_state=self.market_state_field, previous=self.decision_compare_snapshot)
                    self.decision_compare_snapshot = dict(self.decision_intelligence_report.get("snapshot_for_next_compare") or {})
                    # v1.26.2 UNIFIED MARKET INTELLIGENCE: one quantified chain state is
                    # transformed once and distributed to all presentation/research consumers.
                    self.temporal_truth_report = TEMPORAL_TRUTH.snapshot(self.symbol)
                    self.expiry_intelligence_report = build_expiry_intelligence(_exposure_frame(gd, self.snapshot, self.history))
                    self.profile_bundle_report = build_profile_bundle(_exposure_frame(gd, self.snapshot), view="Net", spot=gd.get("spot"))
                    self.derivatives_intelligence_report = build_derivatives_intelligence(self.history if isinstance(self.history,pd.DataFrame) else pd.DataFrame(), observed=None)
                    _extra_levels={"GAMMA_FLIP":gd.get("gamma_flip"),"GAMMA_CENTER":gd.get("gamma_center"),"DELTA_CENTER":gd.get("delta_center")}
                    for _k in ("poc","vah","val","vwap","support","resistance"):
                        if isinstance(self.targets,dict) and self.targets.get(_k) is not None:_extra_levels[_k.upper()]=self.targets.get(_k)
                    self.structural_intelligence_report = STRUCTURAL_INTELLIGENCE.update(self.symbol,self.history if isinstance(self.history,pd.DataFrame) else pd.DataFrame(),spot=gd.get("spot"),extra_levels=_extra_levels)
                    _di=self.derivatives_intelligence_report.get("directional") or {}
                    if self.derivatives_intelligence_report.get("ready"):
                        FEATURE_BUS.ingest(source="ITM_MODEL",symbol=self.symbol,feature_group="DERIVATIVES_FLOW",
                            values={"model":self.derivatives_intelligence_report.get("model"),"observed":self.derivatives_intelligence_report.get("observed"),
                                    "directional":{"derivatives":{"sign":int(_di.get("sign") or 0),"confidence":float(_di.get("confidence") or 0)}}},
                            timestamp=self.last_refresh_ec,received_at=self.last_refresh_ec,confidence=float(_di.get("confidence") or 0),ttl_ms=180000.0)
                    self.versioned_market_state_report = VERSIONED_MARKET_STATE.publish(self.symbol,{
                        "scanner":scanner,"gamma_delta":gd,"market_state":self.market_state_field,"market_truth":self.market_truth_report,
                        "feature_intelligence":self.feature_intelligence_report,"derivatives_intelligence":self.derivatives_intelligence_report,
                        "expiry_intelligence":self.expiry_intelligence_report,"structural_intelligence":self.structural_intelligence_report,
                        "flow_kinematics":self.flow_kinematics_report,"profiles":self.profile_bundle_report},asof=self.last_refresh_ec)
                except Exception as exc:
                    self.decision_intelligence_report={"ready":False,"authority":"SCANNER_ONLY","error":str(exc)[:160]}
                # v1.22 ALL-IN: advanced capabilities are explicit SHADOW/readiness layers.
                # They add context/observability but never change Scanner direction.
                try:
                    self.scenario_lab_report = build_scenario_lab(
                        symbol=self.symbol, spot=gd.get("spot"), atm_iv_pct=(vol or {}).get("atm_iv"),
                        horizon_minutes=90, macro_stress=_asset_stress(macro),
                    )
                except Exception as exc:
                    self.scenario_lab_report = {"ready":False,"state":"UNAVAILABLE","authority":"NONE","error":str(exc)[:160]}
                try:
                    self.operational_readiness_report = build_operational_readiness(
                        causality={"events":[], **(self.causality_report or {})}, source_health=source_diag, external_markets=external_diag
                    )
                    # Prefer the causality latency summary computed from the actual unified stream.
                    if isinstance(self.causality_report.get("latency"), dict):
                        self.operational_readiness_report["latency"] = dict(self.causality_report["latency"])
                except Exception as exc:
                    self.operational_readiness_report = {"generated_at":datetime.now(EC).isoformat(),"error":str(exc)[:160],"capabilities":{}}
                try:
                    self.quantum_shadow_report = build_qubo(
                        [v for v in (self.board_cache or {}).values() if isinstance(v,dict)], max_positions=3
                    )
                except Exception as exc:
                    self.quantum_shadow_report = {"ready":False,"state":"UNAVAILABLE","authority":"NONE","error":str(exc)[:160]}
                try:
                    rs=ResearchStore(routed_dir(alpaca_data.DATA_DIR, "research")/"research_v114.sqlite")
                    if self.mode=="LIVE":
                        rs.append_cycle({"timestamp":self.last_refresh_ec.isoformat(),"symbol":self.symbol,"mode":self.mode,"expiry_mode":self.expiry_window,"spot":gd.get("spot"),"gamma_center":gd.get("gamma_center"),"delta_center":gd.get("delta_center"),"gamma_flip":gd.get("gamma_flip"),"regime":regime_name,"scanner_direction":scanner.get("direction"),"edge_state":scanner.get("edge_state"),"evidence":scanner.get("evidence_score"),"data_quality":dq.get("score"),"model_health":mh.get("score"),"dealer_gex":dealer_diag.get("synthetic_dealer_gex"),"hedge_pressure":(dealer_diag.get("hedge_pressure") or {}).get("net_15m"),"payload":{"source_health":source_diag,"external":external_diag,"market_state_field":self.market_state_field,"causality":self.causality_report,"operational_readiness":self.operational_readiness_report,"scenario_lab":{k:v for k,v in (self.scenario_lab_report or {}).items() if k!="representative_paths"},"quantum_shadow":self.quantum_shadow_report,"flow_kinematics":self.flow_kinematics_report,"market_truth":self.market_truth_report,"feature_intelligence":self.feature_intelligence_report,"decision_intelligence":self.decision_intelligence_report,"research_validation":self.research_validation_report,"dealer_intelligence":{"state":dealer_diag.get("state"),"dealer_field":dealer_diag.get("dealer_field"),"dealer_state_score":dealer_diag.get("dealer_state_score"),"confidence":dealer_diag.get("confidence"),"confidence_label":dealer_diag.get("confidence_label"),"synthetic_dealer_gex":dealer_diag.get("synthetic_dealer_gex"),"hedge_pressure":dealer_diag.get("hedge_pressure"),"inventory_shift":dealer_diag.get("inventory_shift"),"flow_confirmation":dealer_diag.get("flow_confirmation"),"microstructure_quality":dealer_diag.get("microstructure_quality"),"packages":dealer_diag.get("packages")}}})
                    self.research_storage_report=rs.status(self.symbol)
                except Exception as exc:
                    self.research_storage_report={"backend":"SQLITE/WAL","error":str(exc),"research_ready":False}
                try:
                    self.institutional_research_report = institutional_research_snapshot(
                        alpaca_data.DATA_DIR, self.symbol, _live_ticks_for(self.symbol) if self.mode=="LIVE" else pd.DataFrame(),
                        self.vol or {}, self.calibration or {})
                except Exception as exc:
                    self.institutional_research_report = {"role":"RESEARCH_SHADOW","state":"COLLECTING","error":str(exc),"authority":"Scanner LIVE unchanged."}
                try:
                    self.audit_persistence_report = write_audit_reports(
                        alpaca_data.DATA_DIR, self.symbol, self.mode,
                        data_quality=self.data_quality_report, model_health=self.model_health,
                        calibration=self.calibration, scanner=self.scanner,
                        source_health=self.source_health_report, research_storage=self.research_storage_report,
                        tape_archive=self.tape_archive_report, now=self.last_refresh_ec,
                    )
                except Exception as exc:
                    self.audit_persistence_report = {"ready": False, "status": "ERROR", "error": str(exc)}
                # v1.28 provider-neutral historical archive.  Persist the observed families
                # under instrument + exchange trade date so the global calendar can replay
                # every module from one causal clock without vendor-specific filenames.
                if self.mode == "LIVE" and not isolated:
                    try:
                        _hist = HistoricalSessionStore(alpaca_data.DATA_DIR)
                        # Tastytrade hydrates 1m Candle history for the selected own
                        # instrument. Seed the provider-neutral archive once and then
                        # append only candles newer than the per-symbol cursor. This is
                        # essential for YM/MYM Replay after a restart: the prior-evening
                        # Globex leg can be recovered from provider-observed candles even
                        # if the process was not running at Sunday 18:00 ET.
                        if TASTYTRADE.configured:
                            try:
                                _candles = TASTYTRADE.market_data.candle_frame(self.symbol, "1m")
                                if isinstance(_candles, pd.DataFrame) and not _candles.empty:
                                    _candles = _candles.copy()
                                    _cts = pd.to_datetime(_candles.get("timestamp"), errors="coerce", utc=True)
                                    _candles = _candles.loc[_cts.notna()].copy()
                                    _cts = _cts.loc[_cts.notna()]
                                    _cursor_raw = self.historical_candle_cursor.get(self.symbol)
                                    if _cursor_raw:
                                        _cursor = pd.Timestamp(_cursor_raw)
                                        if _cursor.tzinfo is None:
                                            _cursor = _cursor.tz_localize("UTC")
                                        else:
                                            _cursor = _cursor.tz_convert("UTC")
                                        _mask = _cts > _cursor
                                        _candles = _candles.loc[_mask].copy()
                                        _cts = _cts.loc[_mask]
                                    if not _candles.empty:
                                        _hist.append_frame(
                                            self.symbol, "candles", _candles,
                                            source="TASTYTRADE_DXLINK_CANDLE",
                                            dedupe_keys=("timestamp", "period", "source"),
                                        )
                                        self.historical_candle_cursor[self.symbol] = pd.Timestamp(_cts.max()).isoformat()
                            except Exception as _e:
                                _obs_note("service:historical_candle_archive", _e, severity="DEGRADED")
                        if isinstance(selected_events, pd.DataFrame) and not selected_events.empty:
                            _flow_archive = selected_events.copy()
                            if "timestamp" in _flow_archive.columns:
                                _fts = pd.to_datetime(_flow_archive["timestamp"], errors="coerce")
                                _cut = pd.Timestamp(self.last_refresh_ec).tz_localize(None) - pd.Timedelta(minutes=30)
                                try:
                                    if getattr(_fts.dt, "tz", None) is not None:
                                        _fts = _fts.dt.tz_convert(EC).dt.tz_localize(None)
                                except Exception as _e:
                                    _obs_note("service:historical_flow_tz", _e, severity="DEGRADED")
                                _flow_archive = _flow_archive.loc[_fts >= _cut].copy()
                            if not _flow_archive.empty:
                                _hist.append_frame(self.symbol, "flow", _flow_archive, source="ITM_FLOW")
                        if isinstance(large_df, pd.DataFrame) and not large_df.empty:
                            _hist.append_frame(self.symbol, "large_prints", large_df, source="SIP_TAPE")
                        _hist.append_quant_state(self.symbol, self.last_refresh_ec, {
                            "symbol": self.symbol, "mode": self.mode, "expiry_window": self.expiry_window,
                            "spot": gd.get("spot"), "gamma_center": gd.get("gamma_center"),
                            "delta_center": gd.get("delta_center"), "gamma_flip": gd.get("gamma_flip"),
                            "scanner": {k: scanner.get(k) for k in ("direction","edge_state","evidence_score","zone","targets","invalidation")},
                            "flow_summary": flow, "volatility": vol, "positioning": pos,
                            "data_quality": dq, "model_health": mh, "market_state": self.market_state_field,
                            "source_health": source_diag, "derivatives_intelligence": self.derivatives_intelligence_report,
                        })
                    except Exception as _e:
                        _obs_note("service:historical_quant_archive", _e, severity="DEGRADED")
                _progress("FINALIZING",96,"Finalizando Research, auditoría y caches visuales")
                self._invalidate_visual_caches_locked()
                self._maybe_freeze_premarket()
                self.publish_view("FULL")
                _progress("READY",100,"Estado cuantitativo completo")
            except Exception as e:
                self.last_error = str(e)
                self.last_refresh_ec = datetime.now(EC)
                try:
                    self.audit_persistence_report = write_audit_reports(
                        alpaca_data.DATA_DIR, self.symbol, self.mode,
                        data_quality=self.data_quality_report, model_health=self.model_health,
                        calibration=self.calibration, scanner=self.scanner,
                        source_health=self.source_health_report, research_storage=self.research_storage_report,
                        tape_archive=self.tape_archive_report, error=self.last_error, now=self.last_refresh_ec,
                    )
                except Exception as _e:
                    _obs_note('service:1809', _e)
                raise

    def set_expiry_window(self, mode: str) -> Dict[str, Any]:
        """Change the global option horizon and rebuild every option-dependent diagnostic."""
        mode = normalize_window(mode)
        with self.lock:
            if self.replay_context.is_replay:
                previous=self.expiry_window;self.expiry_window=mode;self.replay_cache={}
                bundle=self._build_replay_bundle(force=True)
                if not bundle.get("ready"):
                    self.expiry_window=previous;self.replay_cache={};self._build_replay_bundle(force=True)
                    raise ValueError(bundle.get("error") or f"No hay contratos para {LABELS[mode]}")
                return {"ok":True,"window":bundle.get("expiry_info") or {"mode":mode,"label":LABELS[mode]},"state":self.public_state()}
            if mode == self.expiry_window and self.gamma_delta:
                return {"ok":True,"window":self.expiry_info or {"mode":mode,"label":LABELS[mode]},"state":self.public_state()}
            previous = self.expiry_window
            self.expiry_window = mode
            # Board rows from another expiry scope are not comparable; rebuild staggered.
            self.board_cache.clear(); self.board_scanner_cache.clear(); self.board_cursor=0
            if self.history is None or self.history.empty:
                return {"ok":True,"window":{"mode":mode,"label":LABELS[mode]},"state":self.public_state()}
            try:
                previous_gd = self.gamma_delta if self.gamma_delta else None
                previous_scanner = self.scanner if self.scanner else None
                selected_history, info = apply_expiry_window(self.history, mode)
                if selected_history.empty:
                    self.expiry_window = previous
                    raise ValueError(f"No hay contratos disponibles para {LABELS[mode]} en la cadena cargada.")

                _cfg_engine = engine_config_for_asset(self.symbol, mode)
                gd = analyze_gamma_delta(selected_history, _cfg_engine)
                gamma = _gamma_view_from_gamma_delta(gd)
                selected_events = filter_events(self.flow_events, info)
                flow = flow_session_summary(selected_events)
                vol = _volatility_metrics(gd, _bars_from_ticks(_live_ticks_for(self.symbol))); pos = _positioning_metrics(gd)
                try:
                    calib = calibration_report(alpaca_data.DATA_DIR, self.symbol, min_samples=CALIBRATION_MIN_SAMPLES, probability_expiry_mode=self.expiry_window)
                except Exception as exc:
                    calib = self.calibration or {"ready":False,"status":"COLLECTING","sample_size":0,"reason":str(exc)}
                regime_ctx = classify_regime(gd, vol, flow)
                regime_name=str(regime_ctx.get("regime","")); samples=float(calib.get("sample_size",0) or 0)
                sample_factor=float(np.clip(samples/max(float(CALIBRATION_MIN_SAMPLES),1.0),0,1))
                base_profile=dict(regime_ctx.get("profile",{}) or {})
                cal_mult=float((calib.get("calibrated_regime_multipliers_shadow") or {}).get(regime_name,1.0) or 1.0)
                blend=max(0.35,sample_factor)
                regime_ctx["profile_base"]=base_profile
                regime_ctx["profile"]={k:float(np.clip(1.0+(float(v)-1.0)*blend*cal_mult,0.82,1.18)) for k,v in base_profile.items()}
                regime_ctx["calibration_ready"]=bool(calib.get("ready") and (calib.get("walk_forward") or {}).get("ready"))
                regime_ctx["calibration_samples"]=int(samples)
                regime_ctx["calibrated_multiplier_shadow"]=cal_mult
                macro_ctx=dict(self._current_macro()); macro_ctx["asset_context"]=_asset_macro_context(macro_ctx,self.symbol,regime_name)

                targets = _level_targets(gd); cmd = {}; insights = _top_chain_insights(gd)
                confluence = expiry_confluence(self.history, gd.get("spot"))
                greeks_diag = _greeks_diagnostics(gd)
                american_diag = american_model_check(_latest_snapshot(selected_history), self.symbol)
                exposure_diag = exposure_scenarios(gd.get("enriched", pd.DataFrame()), selected_events, self.symbol)
                external_diag = external_market_context(self.symbol, alpaca_data.DATA_DIR)
                dealer_diag = dealer_intelligence(_latest_snapshot(selected_history), selected_events, self.symbol, alpaca_data.DATA_DIR,
                    float(gd.get("spot") or (self.meta or {}).get("spot") or 0), underlying_ticks=_live_ticks_for(self.symbol))
                fusion_diag = source_context(self.symbol, alpaca_data.DATA_DIR, float(gd.get("spot") or (self.meta or {}).get("spot") or 0), on_demand=False)
                source_diag = self._build_source_diag(self.meta or {}, external_diag)
                source_diag["fusion_sources"] = fusion_diag.get("health", [])
                source_diag["fusion_usable"] = fusion_diag.get("usable_sources", 0)

                scan_result=dict(gd)
                try:
                    pcons=PROVIDER_BUS.snapshot(self.symbol); cp=_finite(pcons.get("consensus_price")) if pcons.get("ready") else None
                    ps=PRICE_STREAM.status(); lt=ps.get("last_tick") if ps.get("symbol")==self.symbol else None
                    rust_lt=RUST_CAUSAL_BRIDGE.latest_price(self.symbol)
                    lp=_finite(cp,_finite((rust_lt or {}).get("price"),_finite((lt or {}).get("price"))))
                    if lp is not None: scan_result["spot"]=lp
                    scan_result["provider_consensus"]=pcons
                except Exception as _e:
                    _obs_note('service:1883', _e)
                provider_features=FEATURE_BUS.snapshot(self.symbol)
                scanner=build_quant_scanner(self.symbol,scan_result,flow,vol,pos,selected_events,self.large_prints,self.premarket_map(),macro_ctx,(self.meta or {}).get("market_state",""),previous=self.scanner,live_ticks=_live_ticks_for(self.symbol),regime_context=regime_ctx,expiry_mode=mode,provider_features=provider_features)
                scanner["expiry_window"]=info; scanner["expiry_confluence"]=confluence
                for z in scanner.get("candidate_zones") or []:
                    try:
                        k=float(z.get("strike")); hit=min(confluence.get("zones",[]),key=lambda q:abs(float(q.get("strike"))-k)) if confluence.get("zones") else None
                        if hit and abs(float(hit.get("strike"))-k)<=0.26:
                            z["expiry_confluence"]=hit; z.setdefault("specials",[]).append(f"EXP {hit.get('count')}/{hit.get('of')}")
                    except Exception as _e:
                        _obs_note('service:1896', _e)
                strong=next((z for z in confluence.get("zones",[]) if z.get("multi_horizon")),None)
                if strong:
                    scanner.setdefault("reasons",[]).append({"label":"MULTI-EXPIRY CONFIRMATION — FUERTE","detail":f"Strike {strong['strike']:.2f} relevante en {' + '.join(strong.get('horizons') or [])}.","value":strong.get("score",0)})
                hp=(dealer_diag.get("hedge_pressure") or {}); hp_dir=str(hp.get("direction","NEUTRAL")); hp_conf=float(hp.get("confidence",0) or 0); scan_dir=str(scanner.get("direction",""))
                scanner["dealer_intelligence_shadow"]={"state":dealer_diag.get("state"),"synthetic_dealer_gex":dealer_diag.get("synthetic_dealer_gex"),"hedge_pressure":hp,"note":"SHADOW: no modifica Evidence Score hasta calibración suficiente."}
                if hp_conf>=45 and hp_dir in {"BUY","SELL"}:
                    item={"label":"HEDGE PRESSURE SHADOW","detail":f"Estimación {hp_dir} · 15m {float(hp.get('net_15m',0) or 0):,.0f} notional · conf {hp_conf:.0f}/100","value":hp_conf}
                    if hp_dir==scan_dir: scanner.setdefault("reasons",[]).append(item)
                    else: scanner.setdefault("contradictions",[]).append(item)

                dq=build_data_quality(self.snapshot,_quality_meta_with_live_underlying(self.meta, self.symbol) if self.mode == "LIVE" else self.meta,gd,info,is_replay=bool(self.replay_context.is_replay))
                mh=build_model_health(selected_history,gd,scanner,info,snapshot=self.snapshot,greeks_diag=greeks_diag,american_diag=american_diag)
                original_edge=str(scanner.get("edge_state","NO EDGE"));dq_score=float(dq.get("score",0) or 0);mh_score=float(mh.get("score",0) or 0);gate_reason=[]
                if dq_score < 50 or mh_score < 60:
                    scanner["edge_state"]="NO EDGE";gate_reason.append("Data Quality/Model Health insuficiente")
                elif (dq_score < 70 or mh_score < 75) and original_edge=="ACTIONABLE":
                    scanner["edge_state"]="CAUTION";gate_reason.append("Calidad utilizable pero no óptima")
                if source_diag.get("data_disagreement"):
                    if scanner.get("edge_state")=="ACTIONABLE": scanner["edge_state"]="CAUTION"
                    gate_reason.append("DATA DISAGREEMENT entre proveedores")
                scanner["source_health"]={"data_disagreement":bool(source_diag.get("data_disagreement")),"additional_provider_configured":bool(source_diag.get("secondary_configured")),"legacy_secondary_configured":bool(source_diag.get("secondary_configured")),"spot_disagreement_pct":source_diag.get("spot_disagreement_pct"),"policy":"NO_FIXED_PROVIDER_RANK"}
                rel=None
                for rr in calib.get("regime_breakdown",[]) or []:
                    if str(rr.get("regime"))==regime_name:
                        rel={"samples":rr.get("samples",0),"positive_end_pct":rr.get("directional_positive_pct"),"expectancy":rr.get("expectancy"),"profit_factor":rr.get("profit_factor")};break
                scanner["signal_reliability"]={"status":"MEASURED" if rel and int(rel.get("samples",0) or 0)>=10 else "COLLECTING","regime":regime_name,"descriptive":rel,"note":"Histórico descriptivo del mismo régimen; no es probabilidad garantizada."}
                scanner["quality_gate"]={"before":original_edge,"after":scanner.get("edge_state"),"data_quality":dq_score,"model_health":mh_score,"reasons":gate_reason}
                _apply_freshness_publication_gate(scanner,dq,gate_reason)
                cmd = _command_center(gd, flow, vol, targets, scanner, None)

                attr=exposure_attribution(selected_history,self.symbol);changed=what_changed(gd,previous_gd,scanner,previous_scanner)
                self.gamma,self.gamma_delta=gamma,gd;self.expiry_info,self.expiry_confluence_data=info,confluence
                self.flow_summary=flow
                try:
                    self.flow_kinematics_report=build_flow_kinematics(selected_events,_live_ticks_for(self.symbol),symbol=self.symbol)
                except Exception as exc:
                    self.flow_kinematics_report={"ready":False,"symbol":self.symbol,"authority":"SHADOW_CONTEXT_ONLY","status":"ERROR","error":str(exc)[:160]}
                self.vol,self.positioning,self.targets,self.command,self.chain_insights=vol,pos,targets,cmd,insights;self.scanner=scanner
                self.data_quality_report,self.model_health=dq,mh;self.regime_context,self.trace_attribution=regime_ctx,attr;self.what_changed_rows=changed;self.calibration=calib;self.macro=macro_ctx
                self.exposure_scenarios_report,self.american_model_report,self.greeks_diagnostics=exposure_diag,american_diag,greeks_diag
                self.dealer_intelligence_report,self.external_market_report,self.source_health_report,self.source_fusion_report=dealer_diag,external_diag,source_diag,fusion_diag
                try: self.research_storage_report=ResearchStore(routed_dir(alpaca_data.DATA_DIR, "research")/"research_v114.sqlite").status(self.symbol)
                except Exception as _e:
                    _obs_note('service:1938', _e)
                if self.mode == "LIVE":
                    save_scanner_snapshot(scanner)
                    try: append_session_metric(alpaca_data.DATA_DIR, self.symbol, gd, scanner, self.expiry_window, vol)
                    except Exception as _e:
                        _obs_note('service:1942', _e)
                self.session_memory_report=session_memory_summary(alpaca_data.DATA_DIR,self.symbol,self.expiry_window) if self.mode=="LIVE" else {"ready":False,"observations":0,"note":"DEMO no contamina Session Memory."}
                try:
                    self.scenario_lab_report=build_scenario_lab(symbol=self.symbol,spot=gd.get("spot"),atm_iv_pct=(vol or {}).get("atm_iv"),horizon_minutes=90,macro_stress=_asset_stress(macro_ctx))
                except Exception as exc:
                    self.scenario_lab_report={"ready":False,"state":"UNAVAILABLE","authority":"NONE","error":str(exc)[:160]}
                try:
                    self.operational_readiness_report=build_operational_readiness(causality={"events":[],**(self.causality_report or {})},source_health=source_diag,external_markets=external_diag)
                    if isinstance(self.causality_report.get("latency"),dict): self.operational_readiness_report["latency"]=dict(self.causality_report["latency"])
                except Exception as exc:
                    self.operational_readiness_report={"error":str(exc)[:160],"capabilities":{}}
                try:
                    self.quantum_shadow_report=build_qubo([v for v in (self.board_cache or {}).values() if isinstance(v,dict)],max_positions=3)
                except Exception as exc:
                    self.quantum_shadow_report={"ready":False,"state":"UNAVAILABLE","authority":"NONE","error":str(exc)[:160]}
                self.last_refresh_ec=datetime.now(EC);self.last_error=None
                self._invalidate_visual_caches_locked()
                self.publish_view("FULL")
                return {"ok":True,"window":info,"state":self.public_state()}
            except Exception:
                self.expiry_window=previous
                raise

    @staticmethod
    def _symbol_state_fields() -> list[str]:
        """Fields that belong to one active asset and may be atomically replaced."""
        return [
            'history','snapshot','meta','gamma','gamma_delta','flow_events','flow_summary','vol','positioning','targets','command','chain_insights','macro',
            'large_prints','large_print_summary','premarket_tape','scanner','model_controls_report','data_quality_report','model_health','regime_context','trace_attribution',
            'what_changed_rows','calibration','exposure_scenarios_report','american_model_report','greeks_diagnostics','session_memory_report',
            'dealer_intelligence_report','external_market_report','source_health_report','source_fusion_report','research_storage_report',
            'institutional_research_report','tape_archive_report','audit_persistence_report','market_state_field','market_truth_report','feature_intelligence_report','research_validation_report','decision_intelligence_report','decision_compare_snapshot','causality_report','temporal_truth_report','derivatives_intelligence_report','expiry_intelligence_report','structural_intelligence_report','profile_bundle_report','versioned_market_state_report',
            'operational_readiness_report','scenario_lab_report','quantum_shadow_report','flow_kinematics_report','premarket_analysis_report','trace_orderflow_report',
            'expiry_info','expiry_confluence_data','last_refresh_ec','last_error','mode'
        ]

    def begin_asset_switch(self, symbol: str) -> Dict[str, Any]:
        """Commit a symbol change immediately; heavy Quant warmup happens elsewhere.

        This method is intentionally network-free and calculation-free.  It advances the
        generation token, clears only active-symbol quantitative state, and returns in
        milliseconds so the browser can show cached/Alpaca price history immediately.
        Scanner/Risk are NOT approximated while the new symbol is warming.
        """
        symbol=str(symbol).upper().strip()
        if symbol not in ASSETS:
            raise ValueError("Activo no reconocido")
        cfg=asset_info(symbol)
        if not cfg.get("selectable",True):
            return {"ok":False,"symbol":symbol,"status":"NO_SELECTABLE","reason":cfg.get("reason"),"proxy":cfg.get("proxy"),"symbol_epoch":self.symbol_epoch}
        if self.replay_context.is_replay:
            # Replay remains transactional because historical bundle integrity matters more
            # than click latency and there is no LIVE price handoff to preserve.
            return self.set_asset(symbol)
        with self.lock:
            if symbol == self.symbol and self.gamma_delta:
                self.asset_warmup={"active":False,"status":"READY","symbol":symbol,"epoch":self.symbol_epoch}
                return {"ok":True,"symbol":symbol,"asset":cfg,"symbol_epoch":self.symbol_epoch,"ready":True,"progressive":False}
            _sym, epoch = self.set_active(symbol)
            self.replay_context=ReplayContext.live(symbol)
            self.trace_session_cache={}
            self._invalidate_visual_caches_locked()
            # Do not carry any analytical state across symbols. Price bootstrap is handled
            # independently by the universal Alpaca session endpoint.
            self.history=pd.DataFrame(); self.snapshot=pd.DataFrame(); self.meta={}
            self.gamma={}; self.gamma_delta={}; self.flow_events=pd.DataFrame(); self.flow_summary={}
            self.vol={}; self.positioning={}; self.targets={}; self.command={}; self.chain_insights={}
            self.macro={}; self.large_prints=pd.DataFrame(); self.large_print_summary={}; self.premarket_tape={}; self.session_flow_tape={}; self.scanner={}
            self.expiry_info={}; self.expiry_confluence_data={}
            # Los vencimientos del activo anterior no describen al nuevo: se
            # retiran ANTES de que nadie pueda construir un cuerpo con ellos.
            _qd_expiry_selection().clear_symbol(_sym)
            self.model_controls_report={}; self.data_quality_report={}; self.model_health={}; self.regime_context={}; self.trace_attribution={}; self.what_changed_rows=[]; self.calibration={}
            self.exposure_scenarios_report={}; self.american_model_report={}; self.greeks_diagnostics={}; self.session_memory_report={}
            self.dealer_intelligence_report={}; self.external_market_report={}; self.source_health_report={}; self.source_fusion_report={}; self.research_storage_report={}; self.institutional_research_report={}; self.tape_archive_report={}; self.audit_persistence_report={}; self.market_state_field={}; self.causality_report={}; self.temporal_truth_report={}; self.derivatives_intelligence_report={}; self.expiry_intelligence_report={}; self.structural_intelligence_report={}; self.profile_bundle_report={}; self.versioned_market_state_report={}; self.operational_readiness_report={}; self.scenario_lab_report={}; self.quantum_shadow_report={}; self.flow_kinematics_report={}; self.premarket_analysis_report={}; self.trace_orderflow_report={}
            self.last_error=None
            self.publish_view("RESET")
            if not cfg.get("full",False):
                # Market-only first-class instrument: direct price/volume/OI may be shown,
                # but the option-Greeks/Scanner layer stays capability-gated. Never copy
                # ETF/index Gamma into a future merely to make the screen look complete.
                self.asset_warmup={
                    "active":False,"status":"MARKET_READY","phase":"DIRECT_MARKET_DATA","progress_pct":100,
                    "symbol":symbol,"epoch":epoch,"started_at":datetime.now(EC).isoformat(),
                    "authority":"PRICE_VOLUME_OI_ONLY · DERIVATIVES_CAPABILITY_GATED",
                    "detail":cfg.get("reason") or "Derivatives analytics are not available for this instrument yet.",
                }
                return {"ok":True,"symbol":symbol,"asset":cfg,"symbol_epoch":epoch,"ready":False,"progressive":False,
                        "price_only":True,"derivatives_ready":False,"warmup":dict(self.asset_warmup)}
            self.asset_warmup={
                "active":True,"status":"WARMING","phase":"PRICE_HANDOFF","progress_pct":10,"symbol":symbol,"epoch":epoch,
                "started_at":datetime.now(EC).isoformat(),"authority":"NO_SIGNAL_UNTIL_READY",
            }
            return {"ok":True,"symbol":symbol,"asset":cfg,"symbol_epoch":epoch,"ready":False,"progressive":True,"warmup":dict(self.asset_warmup)}

    def update_asset_warmup(self, symbol: str, epoch: int, *, phase: str, progress_pct: int, detail: str = "") -> bool:
        """Publish lightweight startup progress without touching quantitative state."""
        target=str(symbol).upper()
        with self.lock:
            if self.symbol != target or int(self.symbol_epoch) != int(epoch):
                return False
            current=dict(self.asset_warmup or {})
            current_progress=int(_finite(current.get("progress_pct"), 0) or 0)
            requested_progress=max(0,min(99,int(progress_pct)))
            next_progress=max(current_progress, requested_progress)
            phase_value=str(phase) if requested_progress >= current_progress else str(current.get("phase") or phase)
            current.update({
                "active":True,"status":"WARMING","symbol":target,"epoch":int(epoch),
                "phase":phase_value,"progress_pct":next_progress,"authority":"NO_SIGNAL_UNTIL_READY",
            })
            if detail and requested_progress >= current_progress: current["detail"]=str(detail)[:180]
            current.setdefault("started_at",datetime.now(EC).isoformat())
            self.asset_warmup=current
            return True

    def make_asset_warm_worker(self, symbol: str, epoch: int) -> "PlatformState":
        """Create an isolated state object for background chain/Quant computation."""
        with self.lock:
            expiry_window=self.expiry_window
            mode=self.mode
        worker=PlatformState(symbol=str(symbol).upper(),symbol_epoch=int(epoch),expiry_window=expiry_window,mode=mode)
        worker.replay_context=ReplayContext.live(str(symbol).upper())
        return worker

    def adopt_asset_core_worker(self, worker: "PlatformState", expected_symbol: str, expected_epoch: int) -> bool:
        """Publish Scanner-ready core state before background enrichment finishes."""
        target=str(expected_symbol).upper()
        with self.lock:
            if self.symbol != target or int(self.symbol_epoch) != int(expected_epoch):
                return False
            # Copy the same symbol-scoped contract. At the CORE boundary all trading
            # authority fields are populated; later research/explanation fields may still
            # be empty and are atomically replaced by the final adoption.
            for name in self._symbol_state_fields():
                setattr(self,name,getattr(worker,name))
            self._invalidate_visual_caches_locked()
            self.replay_context=ReplayContext.live(target)
            self.asset_warmup={
                "active":True,"status":"WARMING","phase":"BACKGROUND_ENRICHMENT","progress_pct":82,
                "symbol":target,"epoch":int(expected_epoch),"core_ready":True,
                "detail":"Scanner listo; Research/Surface/diagnósticos terminan en segundo plano",
                "authority":"QUANT_CORE_READY",
            }
            return True

    def adopt_asset_warm_worker(self, worker: "PlatformState", expected_symbol: str, expected_epoch: int) -> bool:
        """Atomically publish a completed worker only if the symbol generation is current."""
        target=str(expected_symbol).upper()
        with self.lock:
            if self.symbol != target or int(self.symbol_epoch) != int(expected_epoch):
                return False
            for name in self._symbol_state_fields():
                setattr(self,name,getattr(worker,name))
            self._invalidate_visual_caches_locked()
            self.replay_context=ReplayContext.live(target)
            self.asset_warmup={
                "active":False,"status":"READY","phase":"READY","progress_pct":100,"symbol":target,"epoch":int(expected_epoch),
                "finished_at":datetime.now(EC).isoformat(),"authority":"QUANT_READY",
            }
            return True

    def fail_asset_warmup(self, symbol: str, epoch: int, error: Exception | str) -> bool:
        with self.lock:
            if self.symbol != str(symbol).upper() or int(self.symbol_epoch) != int(epoch):
                return False
            self.last_error=str(error)[:300]
            self.asset_warmup={
                "active":False,"status":"DEGRADED","phase":"ERROR","symbol":self.symbol,"epoch":int(epoch),
                "finished_at":datetime.now(EC).isoformat(),"error":self.last_error,"authority":"NO_SIGNAL_UNTIL_REFRESH",
            }
            return True

    def set_asset(self, symbol: str) -> Dict[str, Any]:
        """Atomically switch the quantitative context to one supported asset.

        ``symbol_epoch`` is a monotonic generation token.  Every browser/API response
        can use it to reject results produced for a previous symbol after a rapid switch.
        The transactional restore keeps the last valid quantitative state on failure,
        while advancing the epoch again so intermediate responses are invalidated.
        """
        symbol=str(symbol).upper().strip()
        if symbol not in ASSETS:
            raise ValueError("Activo no reconocido")
        cfg=asset_info(symbol)
        if not cfg.get("full",False):
            return {"ok":False,"symbol":symbol,"status":"FUENTE PENDIENTE","reason":cfg.get("reason"),"proxy":cfg.get("proxy"),"symbol_epoch":self.symbol_epoch}
        if self.replay_context.is_replay:
            with self.lock:
                old_symbol=self.symbol;old_ctx=self.replay_context;old_cache=self.replay_cache
                _sym, switch_epoch = self.set_active(symbol)
                self.replay_context=ReplayContext.at(old_ctx.day,old_ctx.asof,symbol);self.replay_cache={}
                bundle=self._build_replay_bundle(force=True)
                if not bundle.get("ready"):
                    self.set_active(old_symbol);self.replay_context=old_ctx;self.replay_cache=old_cache
                    return {"ok":False,"symbol":symbol,"status":"REPLAY SIN HISTÓRICO","reason":bundle.get("error"),"kept_symbol":old_symbol,"symbol_epoch":self.symbol_epoch}
                return {"ok":True,"symbol":symbol,"asset":cfg,"replay":self.replay_context.describe(),"symbol_epoch":switch_epoch}
        if symbol == self.symbol and self.gamma_delta:
            self.replay_context=ReplayContext.live(symbol)
            return {"ok":True,"symbol":symbol,"asset":cfg,"message":"Ya estaba seleccionado","symbol_epoch":self.symbol_epoch}
        # Transactional switch: no state from the previous symbol is allowed to survive
        # inside the new active context.  Browser requests use the epoch to reject stale
        # responses that were already in flight.
        fields=['symbol','history','snapshot','meta','gamma','gamma_delta','flow_events','flow_summary','vol','positioning','targets','command','chain_insights','macro','large_prints','large_print_summary','premarket_tape','session_flow_tape','scanner','model_controls_report','data_quality_report','model_health','regime_context','trace_attribution','what_changed_rows','calibration','exposure_scenarios_report','american_model_report','greeks_diagnostics','session_memory_report','dealer_intelligence_report','external_market_report','source_health_report','source_fusion_report','research_storage_report','institutional_research_report','tape_archive_report','audit_persistence_report','market_state_field','market_truth_report','feature_intelligence_report','research_validation_report','decision_intelligence_report','decision_compare_snapshot','causality_report','temporal_truth_report','derivatives_intelligence_report','expiry_intelligence_report','structural_intelligence_report','profile_bundle_report','versioned_market_state_report','operational_readiness_report','scenario_lab_report','quantum_shadow_report','flow_kinematics_report','premarket_analysis_report','trace_orderflow_report','expiry_info','expiry_confluence_data','last_refresh_ec','last_error','mode']
        with self.lock:
            backup={k:getattr(self,k) for k in fields}
            _sym, switch_epoch = self.set_active(symbol)
            self.trace_session_cache={}
            self.history=pd.DataFrame(); self.snapshot=pd.DataFrame(); self.meta={}
            self.gamma={}; self.gamma_delta={}; self.flow_events=pd.DataFrame(); self.flow_summary={}
            self.vol={}; self.positioning={}; self.targets={}; self.command={}; self.chain_insights={}
            self.macro={}; self.large_prints=pd.DataFrame(); self.large_print_summary={}; self.premarket_tape={}; self.session_flow_tape={}; self.scanner={}
            self.expiry_info={}; self.expiry_confluence_data={}
            # Los vencimientos del activo anterior no describen al nuevo: se
            # retiran ANTES de que nadie pueda construir un cuerpo con ellos.
            _qd_expiry_selection().clear_symbol(_sym)
            self.model_controls_report={}; self.data_quality_report={}; self.model_health={}; self.regime_context={}; self.trace_attribution={}; self.what_changed_rows=[]; self.calibration={}
            self.exposure_scenarios_report={}; self.american_model_report={}; self.greeks_diagnostics={}; self.session_memory_report={}
            self.dealer_intelligence_report={}; self.external_market_report={}; self.source_health_report={}; self.source_fusion_report={}; self.research_storage_report={}; self.institutional_research_report={}; self.tape_archive_report={}; self.audit_persistence_report={}; self.market_state_field={}; self.causality_report={}; self.temporal_truth_report={}; self.derivatives_intelligence_report={}; self.expiry_intelligence_report={}; self.structural_intelligence_report={}; self.profile_bundle_report={}; self.versioned_market_state_report={}; self.operational_readiness_report={}; self.scenario_lab_report={}; self.quantum_shadow_report={}; self.flow_kinematics_report={}; self.premarket_analysis_report={}; self.trace_orderflow_report={}
            self.publish_view("RESET")
        try:
            # Fast symbol commit: build the quantitative state without forcing the
            # extra REST flow/large-print fetches on the critical click path. Existing
            # OPRA/SIP streams and persisted flow are still processed; the adaptive
            # background loop enriches fresh flow on its next scheduled pass.
            self.refresh(False)
        except Exception as exc:
            with self.lock:
                for k,v in backup.items(): setattr(self,k,v)
                self.symbol_epoch += 1
                self.last_error=f'No pude cargar {symbol}: {exc}'
            raise RuntimeError(f'No pude cargar {symbol}. Se conservó {backup["symbol"]}. Detalle: {exc}') from exc
        self.replay_context=ReplayContext.live(symbol)
        return {"ok":True,"symbol":symbol,"asset":cfg,"symbol_epoch":switch_epoch}

    def analyze_premarket(self, refresh_first: bool = False) -> Dict[str, Any]:
        """Generate an on-demand premarket intelligence report.

        The selected asset is always expressed in its native price. Related markets only
        confirm or contradict when verifiable bridge/feed data exist.
        """
        if refresh_first:
            self.refresh(True)
        with self.lock:
            if not self.gamma_delta:
                raise RuntimeError("No hay datos cuantitativos para analizar")
            # Provider corroboration is read from the normalized feature fabric only when
            # the user explicitly presses ANALIZAR PREMERCADO. Native ITM QUANT structure
            # remains authoritative; removed vendors are not queried or reported.
            spot = float((self.gamma_delta or {}).get("spot") or (self.meta or {}).get("spot") or 0)
            fusion = source_context(self.symbol, alpaca_data.DATA_DIR, spot, on_demand=True)
            ext = enrich_external_market_context(self.external_market_report or {}, fusion)
            report = build_premarket_report(
                symbol=self.symbol, result=self.gamma_delta, positioning=self.positioning or {},
                volatility=self.vol or {}, scanner=self.scanner or {}, expiry_confluence=self.expiry_confluence_data or {},
                flow=self.flow_summary or {}, dealer=self.dealer_intelligence_report or {}, external=ext,
                source_health=self.source_health_report or {}, source_fusion=fusion, macro=self.macro or {}, data_quality=self.data_quality_report or {},
                model_health=self.model_health or {}, meta=self.meta or {},
            )
            report["source_validation"] = chain_validation(report, fusion)
            report["external_model_agreement"] = model_agreement({
                "gamma_flip": (report.get("structure") or {}).get("gamma_flip"),
                "max_pain": (report.get("structure") or {}).get("max_pain"),
                "call_wall": (report.get("structure") or {}).get("call_wall"),
                "put_wall": (report.get("structure") or {}).get("put_wall"),
            }, fusion)
            self.source_fusion_report = fusion
            # Persist the on-demand source picture in the public state so Data Infrastructure
            # and the confirmation table explain the same reality after ANALIZAR PREMARKET.
            self.external_market_report = ext
            self.source_health_report = dict(self.source_health_report or {})
            self.source_health_report["fusion_sources"] = fusion.get("health", [])
            self.source_health_report["fusion_usable"] = fusion.get("usable_sources", 0)
            self.premarket_analysis_report = report
            return report

    def _maybe_freeze_premarket(self):
        if not self.gamma_delta:
            return
        now_ny = datetime.now(NY)
        today = now_ny.date().isoformat()
        store = _read_premarket_store()
        key = f"{self.symbol}:{today}"
        if key in store:
            return
        # Freeze automatically at 09:25 NY (or later if the app started late and premarket history exists).
        freeze_at = time(9, 30) if PREMARKET_FREEZE_MINUTES <= 0 else (datetime.combine(now_ny.date(), time(9, 30), tzinfo=NY) - timedelta(minutes=PREMARKET_FREEZE_MINUTES)).time()
        if now_ny.weekday() >= 5:
            return
        if now_ny.time() < freeze_at:
            return
        tape = None
        try:
            tape = premarket_tape_summary(self.symbol) if self.mode == "LIVE" else None
        except Exception:
            tape = None
        payload=_freeze_map_payload(self.gamma_delta, self.targets, self.vol, self.positioning, tape)
        payload["symbol"]=self.symbol
        payload["expiry_window"]=_jsonable(self.expiry_info or {"mode": self.expiry_window, "label": LABELS.get(self.expiry_window, self.expiry_window), "expirations": [], "count": 0})
        payload["zero_dte_status"]=_jsonable(zero_dte_status(self.history) if isinstance(self.history,pd.DataFrame) and not self.history.empty else {"available":False,"label":"SIN CADENA"})
        store[key] = payload
        _write_premarket_store(store)


    def _record_active_board_row(self) -> None:
        if not self.scanner: return
        cadence=_board_expected_cadence()
        row=board_row(self.symbol,self.scanner,spot=(self.gamma_delta or {}).get("spot"),
                      data_quality=(self.data_quality_report or {}).get("score"),
                      model_health=(self.model_health or {}).get("score"),
                      expiry_label=(self.expiry_info or {}).get("label"),
                      as_of=self.last_refresh_ec or datetime.now(EC),refresh_cadence_seconds=cadence)
        self.board_cache[self.symbol]=row; self.board_scanner_cache[self.symbol]=dict(self.scanner)

    def refresh_board_next(self) -> Dict[str,Any]:
        """Refresh exactly one FULL asset per call; selected asset stays authoritative/current."""
        with self.lock:
            self._record_active_board_row()
            symbols=[s for s,cfg in ASSETS.items() if cfg.get("full",False) and cfg.get("selectable",True) and cfg.get("board_enabled",True)]
            if not symbols: return build_board(self.board_cache.values())
            for _ in range(len(symbols)):
                sym=symbols[self.board_cursor % len(symbols)]; self.board_cursor=(self.board_cursor+1)%len(symbols)
                if sym!=self.symbol: break
            else: sym=self.symbol
            if sym!=self.symbol:
                previous=self.board_scanner_cache.get(sym)
                try:
                    r=_board_scan_symbol(sym,self.expiry_window,previous=previous)
                    sc=r.get("scanner") or {}; self.board_scanner_cache[sym]=dict(sc)
                    self.board_cache[sym]=board_row(sym,sc,spot=r.get("spot"),data_quality=r.get("data_quality"),
                        model_health=r.get("model_health"),expiry_label=r.get("expiry_label"),as_of=r.get("as_of"),
                        refresh_cadence_seconds=_board_expected_cadence())
                except Exception as exc:
                    old=self.board_cache.get(sym)
                    if old:
                        old=dict(old); old["note"]=f"Refresh Board falló: {type(exc).__name__}"; self.board_cache[sym]=old
            return build_board(self.board_cache.values())

    def board_state(self) -> Dict[str,Any]:
        with self.lock:
            self._record_active_board_row()
            return build_board(self.board_cache.values())

    # ------------------------------------------------------------------ v1.16.4 replay
    def archive_tape(self, force: bool = False) -> Dict[str, Any]:
        """Persist the currently selected SIP tape without touching Scanner/Research."""
        if self.mode != "LIVE":
            return {"written":0,"new_rows":0,"reason":"solo LIVE"}
        now=datetime.now(EC)
        if not force and self.last_tape_archive_ec is not None:
            if (now-self.last_tape_archive_ec).total_seconds() < 15.0:
                return self.tape_archive_report or {"written":0,"new_rows":0,"reason":"throttled"}
        ticks=_live_ticks_for(self.symbol)
        rep=persist_tape(alpaca_data.DATA_DIR,self.symbol,ticks)
        self.tape_archive_report=rep
        self.last_tape_archive_ec=now
        return rep

    def replay_sessions(self, start: str | None = None, end: str | None = None) -> Dict[str, Any]:
        days=replay_available_sessions(alpaca_data.DATA_DIR,self.symbol)
        out={"symbol":self.symbol,"sessions":replay_session_coverage(alpaca_data.DATA_DIR,self.symbol,days[:90]),
             "context":self.replay_context.describe()}
        if start and end:
            out["range"]=replay_backtest_range(alpaca_data.DATA_DIR,self.symbol,start,end,self.expiry_window)
        return out

    def set_replay(self, day_value: str, asof_value: str | None = None) -> Dict[str,Any]:
        """Activate one global historical date/clock for every visible module.

        v1.28 removes the old UI fiction that a date-only click belongs to a separate
        Backtest page. Selecting a historical date enters causal Replay immediately.
        If no clock is supplied, Replay starts at the first archived observation for
        that exchange trade date. READY packages remain an instant fallback for old
        sessions whose raw archive is no longer available.
        """
        with self.lock:
            requested_day=date.fromisoformat(str(day_value)[:10])
            raw_days=set(replay_available_sessions(alpaca_data.DATA_DIR,self.symbol))
            ready_days=set(READY_STORE.available_sessions(self.symbol,10000))
            if requested_day.isoformat() not in raw_days and requested_day.isoformat() not in ready_days:
                raise ValueError(f"No existe histórico guardado de {self.symbol} para {requested_day.isoformat()}")

            clock=replay_session_clock(alpaca_data.DATA_DIR,self.symbol,requested_day)
            resolved_asof=asof_value
            if not resolved_asof and clock.get("ready"):
                resolved_asof=str(clock.get("start") or "") or None

            # Preferred path: raw provider-neutral archive -> causal reconstruction.
            if requested_day.isoformat() in raw_days and resolved_asof:
                ctx=ReplayContext.at(requested_day,resolved_asof,self.symbol)
                self.replay_context=ctx;self.replay_cache={}
                bundle=self._build_replay_bundle(force=True)
                if not bundle.get("ready"):
                    self.replay_context=ReplayContext.live(self.symbol);self.replay_cache={}
                    raise ValueError(bundle.get("error") or "No se pudo reconstruir el replay")
                return {"ok":True,"instant":False,"context":ctx.describe(),"clock":clock,"state":self.public_state()}

            # Migration fallback: sealed full-session package from the always-on store.
            ready_pkg=READY_STORE.load_session(self.symbol,requested_day.isoformat())
            if isinstance(ready_pkg,dict) and ready_pkg.get("state"):
                ctx=ReplayContext.at(requested_day,None,self.symbol)
                self.replay_context=ctx
                self.replay_cache={"_key":f"READY::{self.symbol}::{requested_day.isoformat()}","ready":True,"instant_package":ready_pkg,"context":ctx}
                return {"ok":True,"instant":True,"context":ctx.describe(),"clock":clock,"state":self.public_state()}

            self.replay_context=ReplayContext.live(self.symbol);self.replay_cache={}
            raise ValueError(f"Histórico de {self.symbol} {requested_day.isoformat()} existe pero no contiene un reloj causal utilizable")

    def exit_replay(self) -> Dict[str,Any]:
        with self.lock:
            self.replay_context=ReplayContext.live(self.symbol);self.replay_cache={}
            return {"ok":True,"context":self.replay_context.describe(),"state":self.public_state()}

    @staticmethod
    def _replay_prob_override() -> Dict[str,Any]:
        # ITM did not historically snapshot the probability model at each timestamp.
        # Using today's model in yesterday's replay is look-ahead, so Replay falls back
        # explicitly to legacy Evidence actionability until dated model snapshots exist.
        return {"ready":False,"status":"REPLAY · HISTORICAL MODEL SNAPSHOT UNAVAILABLE",
                "stage":"REPLAY_CAUSAL_UNAVAILABLE","reason":"No existe snapshot fechado del modelo de probabilidad para ese instante."}

    def _replay_orderflow(self, scanner: Dict[str,Any], ticks: pd.DataFrame, asof: datetime | None) -> Dict[str,Any]:
        report={"confirmation":{"state":"WAITING","note":"Sin Tape archivado suficiente."},"aggression":{},"cadence":{},
                "multiframe":{"status":"COLLECTING","role":"CONTEXT_ONLY"},"threshold":None,"scanner_zone":None,
                "timing_gate":{"role":"TIMING_ONLY","entry_timing_confirmed":False,"affects_scanner_direction":False,"affects_scanner_score":False},
                "replay_reconstructed":True}
        if ticks is None or ticks.empty:return report
        try:
            thr=_aggression_threshold(ticks,"AUTO",self.symbol)
            bars=aggression_bars(ticks,thr,max_seconds=float(os.getenv("ITM_AGGRESSION_MAX_SECONDS","300")))
            agg=aggression_strength(bars,thr,lookback=20,dead_band=8.0);cad=bar_cadence(bars)
            zone=(scanner or {}).get("zone") or {};direction=str((scanner or {}).get("direction") or "").upper()
            lo,hi=zone.get("low"),zone.get("high")
            if scanner.get("ready") and direction in {"BUY","SELL"} and lo is not None and hi is not None:
                now=pd.Timestamp(asof or ticks["timestamp"].max()).to_pydatetime()
                conf=tape_confirmation(ticks,float(lo),float(hi),direction,float(thr),
                    confirm_fraction=float(os.getenv("ITM_TAPE_CONFIRM_FRACTION","0.50")),
                    absorption_multiple=float(os.getenv("ITM_TAPE_CHURN_MULTIPLE","3.0")),
                    budget_seconds=float(os.getenv("ITM_TAPE_CONFIRM_BUDGET_SECONDS","180")),now=pd.Timestamp(now))
            else:
                conf={"state":"WAITING","direction":direction or None,"in_zone":False,"progress_pct":0.0,"note":"Scanner sin zona/dirección activa en ese instante."}
            st=str(conf.get("state") or "WAITING").upper()
            return {"confirmation":_jsonable(conf),"aggression":_jsonable(agg),"cadence":_jsonable(cad),"threshold":int(thr),
                    "multiframe":_jsonable(multi_timeframe_aggression(ticks)),"scanner_zone":_jsonable(zone) if zone else None,
                    "timing_gate":{"role":"TIMING_ONLY","entry_timing_confirmed":st=="CONFIRMED","stand_down":st in {"ABSORBED","REJECTED","CHURN","EXPIRED"},
                                   "state":st,"affects_scanner_direction":False,"affects_scanner_score":False,
                                   "note":"Reconstruido causalmente con Tape archivado hasta el reloj de Replay."},
                    "replay_reconstructed":True}
        except Exception as exc:
            report["confirmation"]={"state":"WAITING","note":f"Replay Tape no disponible: {str(exc)[:120]}"};return report

    def _build_replay_bundle(self, force: bool = False) -> Dict[str,Any]:
        ctx=self.replay_context
        if not ctx.is_replay:return {"ready":False,"error":"Replay no activo"}
        key=(self.symbol,ctx.day.isoformat(),ctx.asof.isoformat() if ctx.asof else None,self.expiry_window)
        if not force and self.replay_cache.get("_key")==key:return self.replay_cache
        try:
            h=replay_load_structural_history(alpaca_data.DATA_DIR,self.symbol,ctx.day,ctx.asof)
            h=filter_asof(h,ctx.asof)
            if h is None or h.empty:raise RuntimeError("No hay snapshots hasta el reloj seleccionado")
            h["timestamp"]=pd.to_datetime(h["timestamp"],errors="coerce");h=h.dropna(subset=["timestamp"]).sort_values("timestamp")
            selected,info=apply_expiry_window(h,self.expiry_window)
            if selected.empty:raise RuntimeError(f"Sin contratos para {LABELS.get(self.expiry_window,self.expiry_window)} en ese instante")
            snapshot=_latest_snapshot(selected)
            _cfg_engine=engine_config_for_asset(self.symbol,self.expiry_window)
            gd=analyze_gamma_delta(selected,_cfg_engine); gamma=_gamma_view_from_gamma_delta(gd)
            tape=load_tape(alpaca_data.DATA_DIR,self.symbol,ctx.day,ctx.asof)
            # If Tape did not exist for the session, structural snapshots are a disclosed
            # low-resolution price fallback for volatility diagnostics only.
            if tape.empty:
                px=h[["timestamp","underlying_price"]].drop_duplicates("timestamp").rename(columns={"underlying_price":"price"})
                px["size"]=0;px["signed_volume"]=0;tape_for_vol=px
            else:tape_for_vol=tape
            _hist_store=HistoricalSessionStore(alpaca_data.DATA_DIR)
            events=_hist_store.load_frame(self.symbol,ctx.day,"flow",asof=ctx.asof)
            if events.empty:
                events=filter_asof(load_flow_events(ctx.day,self.symbol),ctx.asof)
            # No current/future historical scale anchors in replay: intraday-only SHADOW
            # is preferable to leaking an anchor promoted after the replay date.
            events=apply_normalized_flow_scores(events,{})
            selected_events=filter_events(events,info);flow=flow_session_summary(selected_events)
            try:
                large=_hist_store.load_frame(self.symbol,ctx.day,"large_prints",asof=ctx.asof)
                if large.empty:
                    large=filter_asof(load_large_prints(ctx.day,self.symbol),ctx.asof)
                from app.core.large_prints import _score_events
                large=_score_events(large,self.symbol,anchor={})
            except Exception:large=pd.DataFrame()
            large_summary=large_print_summary(large,self.symbol)
            vol=_volatility_metrics(gd,_bars_from_ticks(tape_for_vol));pos=_positioning_metrics(gd);targets=_level_targets(gd)
            confluence=expiry_confluence(h,gd.get("spot"))
            regime=classify_regime(gd,vol,flow);regime["calibration_ready"]=False;regime["calibration_samples"]=0
            macro={"status":"UNAVAILABLE_IN_CAUSAL_REPLAY","asset_context":{"stress_score":0},
                   "note":"Macro histórico no se sustituye por el dato actual en Replay."}
            pre=_read_premarket_store().get(f"{self.symbol}:{ctx.day.isoformat()}")
            prob_override=self._replay_prob_override()
            # Reconstruct one previous state from the immediately preceding structural
            # snapshot so the Scanner persistence filter behaves like a temporal process.
            previous=None;times=sorted(pd.to_datetime(selected["timestamp"],errors="coerce").dropna().unique())
            if len(times)>=2:
                prev_h=selected[pd.to_datetime(selected["timestamp"],errors="coerce")<=times[-2]].copy()
                if not prev_h.empty:
                    prev_gd=analyze_gamma_delta(prev_h,engine_config_for_asset(self.symbol,self.expiry_window))
                    prev_tape=filter_asof(tape,pd.Timestamp(times[-2]).to_pydatetime()) if not tape.empty else pd.DataFrame()
                    prev_flow_events=filter_asof(selected_events,pd.Timestamp(times[-2]).to_pydatetime()) if isinstance(selected_events,pd.DataFrame) else pd.DataFrame()
                    prev_flow=flow_session_summary(prev_flow_events);prev_vol=_volatility_metrics(prev_gd,_bars_from_ticks(prev_tape));prev_pos=_positioning_metrics(prev_gd)
                    previous=build_quant_scanner(self.symbol,prev_gd,prev_flow,prev_vol,prev_pos,prev_flow_events,filter_asof(large,pd.Timestamp(times[-2]).to_pydatetime()),pre,macro,"REPLAY",previous=None,live_ticks=prev_tape,regime_context=classify_regime(prev_gd,prev_vol,prev_flow),expiry_mode=self.expiry_window,probability_model_override=prob_override)
            scanner=build_quant_scanner(self.symbol,gd,flow,vol,pos,selected_events,large,pre,macro,"REPLAY",previous=previous,live_ticks=tape,regime_context=regime,expiry_mode=self.expiry_window,probability_model_override=prob_override)
            scanner["expiry_window"]=info;scanner["replay"]={"causal":bool(ctx.asof),"context":ctx.describe(),"historical_probability_model":"UNAVAILABLE"}
            strong=next((z for z in confluence.get("zones",[]) if z.get("multi_horizon")),None)
            if strong:scanner.setdefault("reasons",[]).append({"label":"MULTI-EXPIRY CONFIRMATION — FUERTE","detail":f"Strike {strong['strike']:.2f} relevante en {' + '.join(strong.get('horizons') or [])}.","value":strong.get("score",0)})
            # Historical freshness is judged against the replay clock, never against now.
            last_ts=pd.to_datetime(snapshot["timestamp"],errors="coerce").max();clock=ctx.asof or pd.Timestamp(last_ts).to_pydatetime()
            aware_clock=pd.Timestamp(clock).tz_localize(EC) if pd.Timestamp(clock).tzinfo is None else pd.Timestamp(clock)
            meta={"source":"REPLAY ARCHIVE","symbol":self.symbol,"market_state":"REPLAY","matched_snapshots":len(snapshot),
                  "contract_definitions":len(snapshot),"provider_iv_count":int(numeric_column(snapshot,"iv",float("nan")).notna().sum()),
                  "fallback_iv_count":0,"latest_option_market_timestamp":aware_clock.isoformat(),"stock_market_timestamp":aware_clock.isoformat(),"quality_clock":aware_clock.isoformat()}
            dq=build_data_quality(snapshot,meta,gd,info,is_replay=bool(self.replay_context.is_replay))
            try:greek_diag=_greeks_diagnostics(gd);amer=american_model_check(snapshot,self.symbol)
            except Exception:greek_diag={};amer={}
            mh=build_model_health(selected,gd,scanner,info,snapshot=snapshot,greeks_diag=greek_diag,american_diag=amer)
            original=str(scanner.get("edge_state","NO EDGE"));dq_score=float(dq.get("score",0) or 0);mh_score=float(mh.get("score",0) or 0)
            if dq_score<50 or mh_score<60:scanner["edge_state"]="NO EDGE"
            elif (dq_score<70 or mh_score<75) and original=="ACTIONABLE":scanner["edge_state"]="CAUTION"
            scanner["quality_gate"]={"before":original,"after":scanner.get("edge_state"),"data_quality":dq_score,"model_health":mh_score,"reasons":[],"replay":True}
            _apply_freshness_publication_gate(scanner,dq)
            trace_of=self._replay_orderflow(scanner,tape,ctx.asof)
            command=_command_center(gd,flow,vol,targets,scanner,trace_of)
            bundle={"_key":key,"ready":True,"context":ctx,"history":h,"selected_history":selected,"snapshot":snapshot,"gamma":gamma,"gd":gd,
                    "flow_events":events,"selected_events":selected_events,"flow":flow,"large":large,"large_summary":large_summary,"vol":vol,"positioning":pos,
                    "targets":targets,"scanner":scanner,"command":command,"trace_orderflow":trace_of,"expiry_info":info,"confluence":confluence,"meta":meta,
                    "data_quality":dq,"model_health":mh,"regime":regime,"macro":macro,"premarket":pre,"tape":tape,"probability_model":prob_override,
                    "zero_dte":zero_dte_status(h)}
            self.replay_cache=bundle;return bundle
        except Exception as exc:
            # Sin cadena en ese instante, el replay NO se rechaza: degrada a precio.
            # Rechazarlo dejaba la pantalla entera vacía con un cartel rojo, cuando el
            # recorrido del precio de esa sesión sí existe y sirve para leerla. Los
            # paneles de estructura se quedan vacíos con su motivo, que es lo honesto,
            # en vez de tirar abajo toda la reproducción.
            out={"_key":key,"ready":False,"error":str(exc)[:240],"context":ctx,
                 "degraded_to_price":self._replay_price_only(ctx)}
            self.replay_cache=out;return out

    def _replay_price_only(self, ctx) -> Dict[str,Any]:
        """Recorrido del precio de la sesión, cuando no hay estructura reconstruible."""
        try:
            tape=load_tape(alpaca_data.DATA_DIR,self.symbol,ctx.day,ctx.asof)
            rows=0 if tape is None or getattr(tape,"empty",True) else int(len(tape))
            return {"ready":rows>0,"rows":rows,
                    "reason":None if rows else "Tampoco hay cinta archivada en ese instante.",
                    "note":("La estructura de opciones no está archivada hasta más tarde en esta "
                            "sesión. GEX, DEX y los muros quedan vacíos con su motivo; el precio "
                            "sí se reproduce.")}
        except Exception as exc:
            _obs_note("service:replay_price_only", exc)
            return {"ready":False,"rows":0,"reason":str(exc)[:160]}

    def _instant_replay_package(self) -> Dict[str,Any] | None:
        pkg=(self.replay_cache or {}).get("instant_package") if isinstance(self.replay_cache,dict) else None
        return pkg if isinstance(pkg,dict) else None

    def _public_replay_state(self) -> Dict[str,Any]:
        instant=self._instant_replay_package()
        if instant is not None:
            ctx=self.replay_context
            state=dict(instant.get("state") or {})
            state.update({"ready":True,"mode":"REPLAY","active_symbol":self.symbol,"symbol_epoch":int(self.symbol_epoch),
                          "asset":asset_info(self.symbol),"assets":core_selectable_assets(),"data_age_seconds":0,
                          "price_stream":{"symbol":self.symbol,"connected":False,"replay":True,"precomputed":True},
                          "option_stream":{"connected":False,"replay":True,"precomputed":True},
                          "replay":{**ctx.describe(),"instant_precomputed":True,"session_package_generated_at":instant.get("generated_at_utc")},
                          "instant_session":{"ready":True,"source":instant.get("source"),"generated_at_utc":instant.get("generated_at_utc"),"sealed":instant.get("sealed",False)}})
            meta=dict(state.get("meta") or {});meta["market_state"]="HISTORICAL_SESSION_READY";meta["source"]="ALWAYS_ON PRECOMPUTED SESSION";state["meta"]=meta
            return _jsonable(state)
        b=self._build_replay_bundle()
        ctx=self.replay_context
        if not b.get("ready"):return {"ready":False,"mode":"REPLAY","active_symbol":self.symbol,"replay":ctx.describe(),"error":b.get("error")}
        gd=b["gd"];flow=b["flow"];sc=b["scanner"];tape=b["tape"]
        last_tick=None
        if isinstance(tape,pd.DataFrame) and not tape.empty:
            rr=tape.iloc[-1];last_tick={k:_jsonable(rr.get(k)) for k in tape.columns if k in {"timestamp","price","size","seq","exchange"}}
        return _jsonable({
            "ready":True,"mode":"REPLAY","active_symbol":self.symbol,"asset":asset_info(self.symbol),"assets":core_selectable_assets(),"meta":b["meta"],
            "last_refresh_ec":ctx.asof or (b["history"]["timestamp"].max() if not b["history"].empty else None),"data_age_seconds":0,
            "price_stream":{"symbol":self.symbol,"connected":False,"replay":True,"last_tick":last_tick},"option_stream":{"connected":False,"replay":True},
            "spot":gd.get("spot"),"model_spot":gd.get("spot"),"gamma_center":gd.get("gamma_center"),"delta_center":gd.get("delta_center"),"gamma_flip":gd.get("gamma_flip"),
            "gamma_flip_crossing":gd.get("gamma_flip_crossing"),"gamma_flip_direction":gd.get("flip_direction"),"gamma_flip_velocity":gd.get("flip_velocity_label"),
            "gamma_migration":{"direction":gd.get("migration_direction"),"strength":gd.get("migration_strength")},"delta_migration":{"direction":gd.get("delta_migration_direction"),"strength":gd.get("delta_migration_strength")},
            "gamma_delta_alignment":{"label":gd.get("gamma_delta_alignment_label"),"score":gd.get("gamma_delta_alignment_score")},
            "data_quality":(b["data_quality"] or {}).get("score"),"data_quality_report":b["data_quality"],"model_health":b["model_health"],"regime_context":b["regime"],
            "calibration":{"ready":False,"status":"REPLAY_CAUSAL_UNAVAILABLE","sample_size":0,"probability_model":b["probability_model"]},
            "live_validation":{"state":"REPLAY","note":"No se usa readiness actual para juzgar el pasado."},"targets":b["targets"],"command":b["command"],
            "flow":{"regime":flow.get("regime"),"confidence":flow.get("confidence"),"net":flow.get("net"),"bull":flow.get("bull"),"bear":flow.get("bear"),"microstructure":flow.get("microstructure",{})},
            "volatility":b["vol"],"positioning":b["positioning"],"chain_insights":{},"macro":b["macro"],"large_prints":b["large_summary"],"scanner":sc,
            "easy":easy_view(sc,gd,b["vol"],((b.get("trace_orderflow") or {}).get("confirmation") or {}),(b["data_quality"] or {}).get("score"),0,{"probability_model":b["probability_model"]}),
            "institutional_research":{"role":"RESEARCH_SHADOW","state":"REPLAY_DISABLED","note":"Labs actuales no se sustituyen por modelos presentes durante Replay. Requieren snapshots históricos propios para una reconstrucción causal."},
            "board":{"rows":[],"note":"Board multi-activo se desactiva en Replay: no se comparan activos cuyos estados no fueron reconstruidos al mismo reloj."},
            "trace_orderflow":b["trace_orderflow"],"expiry_window":b["expiry_info"],"zero_dte_status":b["zero_dte"],"expiry_windows":[{"mode":m,"label":LABELS[m]} for m in WINDOWS],
            "expiry_confluence":b["confluence"],"flow_pro":flow_pro_summary(b["selected_events"],b["selected_history"],gd,None),"exposure_gamma":exposure_summary(gd,"Gamma"),"exposure_delta":exposure_summary(gd,"Delta"),
            "premarket":b["premarket"],"premarket_analysis":None,"native_options_structure":build_native_options_structure(b.get("gd") or {}),"replay":{**ctx.describe(),"clock":replay_session_clock(alpaca_data.DATA_DIR,self.symbol,ctx.day),"tape_archived":not b["tape"].empty},
            "source_note":"REPLAY GLOBAL CAUSAL: Scanner, TRACE, Flow, Large Prints, Positioning, Volatilidad y Tape usan el mismo corte temporal. Inputs históricos no reconstruibles se marcan UNAVAILABLE; nunca se sustituyen por valores actuales."
        })

    def premarket_map(self) -> Optional[Dict[str, Any]]:
        today = datetime.now(NY).date().isoformat()
        return _read_premarket_store().get(f"{self.symbol}:{today}")

    def force_freeze_premarket(self) -> Dict[str, Any]:
        with self.lock:
            if not self.gamma_delta:
                raise RuntimeError("No hay datos para congelar")
            today = datetime.now(NY).date().isoformat()
            store = _read_premarket_store()
            tape = None
            try:
                tape = premarket_tape_summary(self.symbol) if self.mode == "LIVE" else None
            except Exception as _e:
                _obs_note('service:2515', _e)
            payload=_freeze_map_payload(self.gamma_delta, self.targets, self.vol, self.positioning, tape)
            payload["symbol"]=self.symbol
            payload["expiry_window"]=_jsonable(self.expiry_info or {"mode": self.expiry_window, "label": LABELS.get(self.expiry_window, self.expiry_window), "expirations": [], "count": 0})
            payload["zero_dte_status"]=_jsonable(zero_dte_status(self.history) if isinstance(self.history,pd.DataFrame) and not self.history.empty else {"available":False,"label":"SIN CADENA"})
            key=f"{self.symbol}:{today}"
            store[key] = payload
            _write_premarket_store(store)
            return store[key]

    def _trace_orderflow_state(self, persist: bool = True) -> Dict[str, Any]:
        """Execution timing layer. Never changes Scanner direction/evidence.

        Scanner finds the thesis/zone first. Tape only judges the CURRENT visit to that
        zone. State transitions are persisted separately so Research can later measure
        whether CONFIRMED/ABSORBED/REJECTED/CHURN/EXPIRED add edge.
        """
        report = {
            "confirmation": {"state": "WAITING", "note": "Esperando zona activa del Scanner."},
            "aggression": {}, "cadence": {}, "multiframe": {"status":"COLLECTING","role":"CONTEXT_ONLY"}, "threshold": None, "scanner_zone": None,
            "timing_gate": {"role": "TIMING_ONLY", "entry_timing_confirmed": False,
                            "affects_scanner_direction": False, "affects_scanner_score": False}
        }
        if self.mode != "LIVE":
            self.trace_orderflow_report = report
            return report
        try:
            ticks = _live_ticks_for(self.symbol)
            thr = _aggression_threshold(ticks, "AUTO", self.symbol)
            bars = aggression_bars(ticks, thr, max_seconds=float(os.getenv("ITM_AGGRESSION_MAX_SECONDS", "300")))
            agg = aggression_strength(bars, thr, lookback=20, dead_band=8.0)
            cad = bar_cadence(bars)
            scanner = self.scanner or {}
            zone = scanner.get("zone") or {}
            direction = str(scanner.get("direction") or "").upper()
            lo, hi = zone.get("low"), zone.get("high")
            if scanner.get("ready") and direction in ("BUY", "SELL") and lo is not None and hi is not None:
                now = pd.Timestamp(datetime.now(EC).replace(tzinfo=None))
                conf = tape_confirmation(
                    ticks, float(lo), float(hi), direction, float(thr),
                    confirm_fraction=float(os.getenv("ITM_TAPE_CONFIRM_FRACTION", "0.50")),
                    absorption_multiple=float(os.getenv("ITM_TAPE_CHURN_MULTIPLE", "3.0")),
                    budget_seconds=float(os.getenv("ITM_TAPE_CONFIRM_BUDGET_SECONDS", "180")),
                    now=now,
                )
            else:
                conf = {"state": "WAITING", "direction": direction or None, "in_zone": False,
                        "progress_pct": 0.0, "seconds_remaining": None, "zone_entry_time": None,
                        "note": "Scanner todavía no tiene una zona/dirección activa."}
            state = str(conf.get("state") or "WAITING").upper()
            timing = {
                "role": "TIMING_ONLY",
                "entry_timing_confirmed": state == "CONFIRMED",
                "stand_down": state in {"ABSORBED", "REJECTED", "CHURN", "EXPIRED"},
                "state": state,
                "affects_scanner_direction": False,
                "affects_scanner_score": False,
                "note": "Scanner manda zona/dirección; Tape solo confirma o rechaza el momento de entrada."
            }
            report = {"confirmation": _jsonable(conf), "aggression": _jsonable(agg),
                      "cadence": _jsonable(cad), "threshold": int(thr),
                      "multiframe": _jsonable(multi_timeframe_aggression(ticks)),
                      "scanner_zone": _jsonable(zone) if zone else None, "timing_gate": timing}

            # Persist each state transition once per uninterrupted visit to the zone.
            if persist and state != "WAITING" and conf.get("zone_entry_time"):
                rs = ResearchStore(routed_dir(alpaca_data.DATA_DIR, "research") / "research_v114.sqlite")
                zc = zone.get("center")
                event_key = "|".join([self.symbol, str(conf.get("zone_entry_time")), direction,
                                      f"{float(zc):.4f}" if zc is not None else "NA", state])
                rs.append_tape_event({
                    "event_key": event_key, "timestamp": conf.get("asof_timestamp") or datetime.now(EC).isoformat(),
                    "symbol": self.symbol, "expiry_mode": self.expiry_window,
                    "scanner_direction": direction, "edge_state": scanner.get("edge_state"),
                    "evidence": scanner.get("evidence_score"), "zone_low": lo, "zone_center": zc, "zone_high": hi,
                    "target1": scanner.get("target1"), "invalidation": scanner.get("invalidation"),
                    "tape_state": state, "zone_entry_time": conf.get("zone_entry_time"),
                    "progress_pct": conf.get("progress_pct"), "signed_volume": conf.get("signed_volume"),
                    "volume": conf.get("volume"), "buy_pct": conf.get("buy_pct"),
                    "seconds_in_zone": conf.get("seconds_in_zone"), "seconds_remaining": conf.get("seconds_remaining"),
                    "trades": conf.get("trades"), "price_move_favourable": conf.get("price_move_favourable"),
                    "efficiency_per_1k": conf.get("efficiency_per_1k"), "imbalance_ratio": conf.get("imbalance_ratio"),
                    "aggression_score": agg.get("score"), "aggression_control": agg.get("control"),
                    "aggression_churn_pct": agg.get("churn_pct"), "aggression_absorption_pct": agg.get("absorption_pct"),
                    "cadence_regime": cad.get("regime"), "bars_per_minute": cad.get("bars_per_minute"),
                    "payload": {"confirmation": conf, "aggression": agg, "cadence": cad, "multiframe": report.get("multiframe",{}), "timing_gate": timing}
                })
                self.research_storage_report = rs.status(self.symbol)
            self.trace_orderflow_report = report
            return report
        except Exception as exc:
            report["confirmation"] = {"state": "WAITING", "note": f"Tape timing no disponible: {str(exc)[:120]}"}
            self.trace_orderflow_report = report
            return report

    # ---------------------------------------------------- v1.27.15 · atomic view
    def set_active(self, symbol: str, epoch: int | None = None) -> tuple[str, int]:
        """Single writer for the (symbol, epoch) generation pair."""
        with self.lock:
            if epoch is None:
                self.symbol_epoch = int(self.symbol_epoch) + 1
            else:
                self.symbol_epoch = int(epoch)
            self.symbol = str(symbol)
            self._active_pair = (str(self.symbol), int(self.symbol_epoch))
            return self._active_pair

    def active(self) -> tuple[str, int]:
        """Read symbol+epoch with one atomic reference read."""
        pair = self._active_pair
        if isinstance(pair, tuple) and len(pair) == 2 and pair[0]:
            return str(pair[0]), int(pair[1])
        with self.lock:
            self._active_pair = (str(self.symbol), int(self.symbol_epoch))
            return self._active_pair

    def publish_view(self, stage: str = "FULL") -> StateView:
        """Publish a coherent cross-sectional view with one final reference rebind."""
        with self.lock:
            self._view_generation += 1
            snap: Dict[str, Any] = {}
            for name in VIEW_FIELDS:
                try:
                    snap[name] = getattr(self, name)
                except Exception as exc:
                    _obs_note("service:publish_view:" + name, exc, severity="DEGRADED")
                    snap[name] = None
            snap['symbol'], snap['symbol_epoch'] = self.active()
            view = StateView(
                generation=int(self._view_generation),
                stage=str(stage),
                published_at=datetime.now(timezone.utc),
                _fields=snap,
            )
            self._published_view = view
            return view

    def view(self) -> StateView:
        """Non-blocking coherent reader view."""
        return self._published_view

    # Coste medido de reconstruir el estado público: 75-140 ms. Lo caro no es leer
    # la estructura, es recomponer market_state_field, quant_synthesis,
    # decision_intelligence y compañía — que NO cambian entre ciclos del motor.
    # Recalcularlos en cada petición era el cuello de botella de toda la terminal.
    #
    # Se memoriza por revisión analítica y se refresca sólo lo que sí se mueve tick
    # a tick. La estructura congelada entre ciclos no es una concesión: es lo
    # correcto, porque el motor no ha publicado otra.
    _PUBLIC_LIVE_KEYS = ("spot", "price_stream", "provider_consensus", "data_age_seconds",
                         "option_stream", "iv_rank_native")

    def public_state(self) -> Dict[str, Any]:
        key = (str(self.symbol), int(self.symbol_epoch), int(self.analytics_revision),
               bool(self.replay_context.is_replay),
               self.replay_context.asof.isoformat() if self.replay_context.asof else None,
               str(self.expiry_window), str(self.mode))
        hit = self._public_state_cache
        if hit and hit.get("_key") == key:
            cached = hit.get("payload")
            if isinstance(cached, dict):
                return self._refresh_public_live(cached)
        payload = self._build_public_state()
        if isinstance(payload, dict):
            # `time` en este módulo es datetime.time; el reloj monótono viene de time_module.
            self._public_state_cache = {"_key": key, "payload": payload, "at": _now_monotonic()}
        return payload

    def _refresh_public_live(self, cached: Dict[str, Any]) -> Dict[str, Any]:
        """Devuelve el estado memorizado con el precio y la salud de feeds al día.

        Se copia en superficie: quien reciba el estado no puede mutar el que queda
        guardado, y las ramas pesadas se comparten sin volver a construirse.
        """
        out = dict(cached)
        try:
            if self.replay_context.is_replay:
                return out
            ps = PRICE_STREAM.status()
            consensus = PROVIDER_BUS.snapshot(self.symbol)
            tick = ps.get("last_tick") if ps.get("symbol") == self.symbol else None
            rust = RUST_CAUSAL_BRIDGE.latest_price(self.symbol) or {}
            live = _finite(
                consensus.get("consensus_price") if consensus.get("ready") else None,
                _finite(rust.get("price"), _finite((tick or {}).get("price"), out.get("spot"))),
            )
            if live is not None:
                out["spot"] = live
            out["price_stream"] = ps
            out["provider_consensus"] = consensus
            gate = self.current_publication_gate()
            out["publication_gate"] = gate
            dq = dict(out.get("data_quality_report") or {})
            dq["circuito_frescura"] = gate
            out["data_quality_report"] = dq
            out["cached_analytics"] = True
        except Exception as exc:
            _obs_note("service:refresh_public_live", exc)
        return out

    def _build_public_state(self) -> Dict[str, Any]:
        with self.lock:
            if self.replay_context.is_replay:
                return self._public_replay_state()
            if not self.gamma_delta:
                warm=dict(self.asset_warmup or {})
                ps=PRICE_STREAM.status()
                live_tick=ps.get("last_tick") if ps.get("symbol") == self.symbol else None
                rust_tick=RUST_CAUSAL_BRIDGE.latest_price(self.symbol)
                provider_consensus=PROVIDER_BUS.snapshot(self.symbol)
                live_spot=_finite(
                    provider_consensus.get("consensus_price") if provider_consensus.get("ready") else None,
                    _finite((rust_tick or {}).get("price"), _finite((live_tick or {}).get("price"))),
                )
                cfg=asset_info(self.symbol)
                memory_ready=bool(PERSISTENCE_REPORT.status)
                progress=int(_finite(warm.get("progress_pct"), 15) or 15)
                return {
                    "ready": False, "loading": bool(warm.get("active", True)),
                    "progressive_switch": bool(warm.get("active")), "warmup": warm,
                    "error": self.last_error, "mode": self.mode, "active_symbol": self.symbol,
                    "symbol_epoch": self.symbol_epoch, "asset": cfg, "assets": core_selectable_assets(),
                    "spot": live_spot, "price_stream": ps, "provider_consensus": provider_consensus,
                    "option_stream": OPTION_STREAM.status(), "opra_diagnostics": self._opra_diagnostics(self.symbol),
                    "meta": {"stock_feed": str(ps.get("feed") or ps.get("source") or "PRICE STREAM"),
                             "option_feed": "HYDRATING", "market_state": str(ps.get("market_state") or "LIVE")},
                    "persistent_memory": {"root": str(alpaca_data.DATA_DIR),
                        "policy": "NO RESET LIVE/AUDITOR/CALIBRATION/REPLAY", "version": APP_VERSION,
                        "migration_status": PERSISTENCE_REPORT.status, "source_policy": PERSISTENCE_REPORT.source_policy,
                        "legacy_sources": PERSISTENCE_REPORT.legacy_sources},
                    "expiry_window": {"mode":self.expiry_window,"label":LABELS.get(self.expiry_window,self.expiry_window),
                        "expirations":[],"count":None,"loading":True,"status":"VERIFYING_CHAIN"},
                    "zero_dte_status": {"available":False,"known":False,"loading":True,"label":"VERIFICANDO…"},
                    "instant_boot": {"stage":str(warm.get("phase") or "STARTING"),"progress_pct":progress,
                        "price_ready":live_spot is not None,"trace_path_ready":True,"chain_ready":False,"quant_ready":False,
                        "memory_ready":memory_ready,"hint":instant_boot_hint_status(self.symbol,self.expiry_window,float(cfg.get("window",STRIKE_WINDOW))),
                        "authority":"PRESENTATION_AND_STARTUP_ONLY"},
                    "board": self.board_state(),
                }
            gd = self.gamma_delta
            flow = self.flow_summary
            pre = self.premarket_map()
            selected_events = filter_events(self.flow_events, self.expiry_info)
            flow_pro = flow_pro_summary(selected_events, self.history, gd, ((self.session_flow_tape or {}).get("bars") if self.session_flow_tape else (self.premarket_tape or {}).get("bars")))
            exp_gamma = exposure_summary(gd, "Gamma")
            exp_delta = exposure_summary(gd, "Delta")
            largest = flow.get("largest")
            def event_pack(row):
                if row is None or isinstance(row, type(None)):
                    return None
                try:
                    return {
                        "timestamp": _jsonable(row.get("timestamp")), "strike": _finite(row.get("strike")),
                        "option_type": str(row.get("option_type", "")), "premium": _finite(row.get("premium"), 0),
                        "flow_score": _finite(row.get("flow_score"), 0), "direction_sign": int(_finite(row.get("direction_sign"), 0) or 0),
                        "aggressor": str(row.get("aggressor", "")),
                        "underlying_price": _finite(row.get("underlying_price")),
                        "expiration": str(row.get("expiration", row.get("expiration_date", ""))),
                        "directional_premium": _finite(row.get("directional_premium"), 0),
                    }
                except Exception:
                    return None
            audit = gd.get("audit", pd.DataFrame())
            warnings = []
            if isinstance(audit, pd.DataFrame) and not audit.empty:
                warnings = audit[audit["status"] != "OK"][["check", "detail"]].to_dict("records")
            latest_flow = None
            try:
                if isinstance(self.flow_events, pd.DataFrame) and not self.flow_events.empty:
                    fx=self.flow_events.copy()
                    fx["timestamp"]=pd.to_datetime(fx["timestamp"],errors="coerce")
                    fx=fx.dropna(subset=["timestamp"])
                    fx["flow_score"]=numeric_column(fx,"flow_score",0)
                    hot=fx[fx["flow_score"]>=70]
                    if not hot.empty: latest_flow=hot.sort_values("timestamp").iloc[-1]
            except Exception:
                latest_flow=None
            latest_ts = pd.to_datetime(self.snapshot.get("timestamp", pd.Series([pd.NaT])), errors="coerce").max() if not self.snapshot.empty else pd.NaT
            age = None
            if pd.notna(latest_ts):
                age = max(0.0, (datetime.now(EC).replace(tzinfo=None) - pd.Timestamp(latest_ts).to_pydatetime()).total_seconds())
            ps = PRICE_STREAM.status()
            live_tick = ps.get("last_tick") if ps.get("symbol") == self.symbol else None
            rust_tick = RUST_CAUSAL_BRIDGE.latest_price(self.symbol)
            provider_consensus = PROVIDER_BUS.snapshot(self.symbol)
            live_spot = _finite(
                provider_consensus.get("consensus_price") if provider_consensus.get("ready") else None,
                _finite((rust_tick or {}).get("price"), _finite((live_tick or {}).get("price"), gd.get("spot"))),
            )
            trace_orderflow = self._trace_orderflow_state(persist=True)
            command = _command_center(gd, flow, self.vol or {}, self.targets or {}, self.scanner or {}, trace_orderflow)
            self.command = command
            validation = live_validation_status(alpaca_data.DATA_DIR, self.symbol, self.expiry_window, self.calibration or {}, self.research_storage_report or {}, self.dealer_intelligence_report or {})
            easy = easy_view(self.scanner or {}, gd, self.vol or {}, (trace_orderflow.get("confirmation") or {}),
                             (self.data_quality_report or {}).get("score", gd.get("data_quality")),
                             int((self.calibration or {}).get("sessions",0) or 0), self.calibration or {})
            market_state_field = build_market_state_field(
                self.scanner or {}, gd, flow, self.vol or {}, trace_orderflow,
                symbol=self.symbol, dealer=self.dealer_intelligence_report or {},
                external=self.external_market_report or {}, positioning=self.positioning or {},
                macro=self.macro or {},
            )
            self.market_state_field = market_state_field
            quant_synthesis = build_quant_synthesis(
                self.scanner or {}, gd, flow, self.vol or {}, trace_orderflow,
                spot=live_spot,
                data_quality=(self.data_quality_report or {}).get("score", gd.get("data_quality")),
                model_health=(self.model_health or {}).get("score") if isinstance(self.model_health, dict) else self.model_health,
                source_health=self.source_health_report or {},
                dealer=self.dealer_intelligence_report or {},
                external=self.external_market_report or {},
                positioning=self.positioning or {},
                macro=self.macro or {},
                state_field=market_state_field,
            )
            market_truth = MARKET_TRUTH.snapshot(self.symbol)
            feature_intelligence = build_feature_intelligence(
                feature_snapshot=FEATURE_BUS.snapshot(self.symbol), market_state=market_state_field,
                regime_context=self.regime_context or {}, calibration=self.calibration or {}, scanner=self.scanner or {})
            research_validation = build_research_validation(self.calibration or {}, self.research_storage_report or {}, self.scanner or {})
            decision_intelligence = build_decision_intelligence(
                scanner=self.scanner or {}, quant_synthesis=quant_synthesis, market_truth=market_truth,
                feature_intelligence=feature_intelligence, research_validation=research_validation,
                market_state=market_state_field, previous=self.decision_compare_snapshot)
            live_publication_gate = self.current_publication_gate()
            data_quality_public = dict(self.data_quality_report or {})
            data_quality_public["circuito_frescura"] = live_publication_gate
            return _jsonable({
                "ready": True, "mode": self.mode, "active_symbol": self.symbol, "symbol_epoch": self.symbol_epoch, "asset": asset_info(self.symbol), "assets": core_selectable_assets(), "meta": self.meta, "progressive_switch": False, "warmup": dict(self.asset_warmup or {}),
                "last_refresh_ec": self.last_refresh_ec,
                "data_age_seconds": age, "price_stream": ps, "option_stream": OPTION_STREAM.status(),
                "provider_consensus": provider_consensus,
                "spot": live_spot, "model_spot": gd.get("spot"), "gamma_center": gd.get("gamma_center"), "delta_center": gd.get("delta_center"),
                "gamma_flip": gd.get("gamma_flip"), "gamma_flip_crossing": gd.get("gamma_flip_crossing"),
                "gamma_flip_direction": gd.get("flip_direction"), "gamma_flip_velocity": gd.get("flip_velocity_label"),
                "key_levels_report": key_levels_report(gd, self.scanner or {}, curagg=gd.get("current"), spot=live_spot, symbol=self.symbol),
                "gamma_regime": gd.get("regime"),
                "gamma_migration": {"direction": gd.get("migration_direction"), "strength": gd.get("migration_strength")},
                "delta_migration": {"direction": gd.get("delta_migration_direction"), "strength": gd.get("delta_migration_strength")},
                "gamma_delta_alignment": {"label": gd.get("gamma_delta_alignment_label"), "score": gd.get("gamma_delta_alignment_score")},
                "data_quality": (self.data_quality_report or {}).get("score", gd.get("data_quality")), "audit_warnings": warnings,
                "data_quality_report": data_quality_public, "publication_gate": live_publication_gate, "model_health": self.model_health,
                # IV evaluada contrato a contrato y ajuste SSVI de la cadena.
                # Alimentan los controles del Auditor, que antes avisaban de que
                # «no se evaluó» algo que sí tiene motor escrito detrás.
                "model_controls": self.model_controls_report or {},
                "regime_context": self.regime_context, "trace_attribution": self.trace_attribution,
                "what_changed": self.what_changed_rows, "calibration": self.calibration, "live_validation": validation,
                "exposure_scenarios": self.exposure_scenarios_report, "american_model": self.american_model_report, "greeks_diagnostics": self.greeks_diagnostics, "session_memory": self.session_memory_report,
                "dealer_intelligence": self.dealer_intelligence_report, "external_markets": self.external_market_report, "source_health": self.source_health_report, "source_fusion": self.source_fusion_report, "research_storage": self.research_storage_report,
                "auditor_persistence": self.audit_persistence_report,
                "persistent_memory": {"root": str(alpaca_data.DATA_DIR), "policy": "NO RESET LIVE/AUDITOR/CALIBRATION/REPLAY", "version": APP_VERSION, "migration_status": PERSISTENCE_REPORT.status, "source_policy": PERSISTENCE_REPORT.source_policy, "legacy_sources": PERSISTENCE_REPORT.legacy_sources},
                "instant_boot": {"stage":str((self.asset_warmup or {}).get("phase") or "READY"),"progress_pct":int(_finite((self.asset_warmup or {}).get("progress_pct"),100) or 100),"price_ready":True,"quant_ready":True,"chain_ready":True,"background_enrichment":bool((self.asset_warmup or {}).get("active")),"hint":instant_boot_hint_status(self.symbol,self.expiry_window,float(asset_info(self.symbol).get("window",STRIKE_WINDOW))),"authority":"PRESENTATION_AND_STARTUP_ONLY"},
                "flow_microstructure": (flow.get("microstructure") or {}),
                "model_inputs": market_inputs(self.symbol), "hot_memory_snapshots": HOT_SNAPSHOTS,
                "targets": self.targets, "command": command, "flow": {
                    "regime": flow.get("regime"), "confidence": flow.get("confidence"), "net": flow.get("net"),
                    "bull": flow.get("bull"), "bear": flow.get("bear"), "largest": event_pack(largest),
                    "latest": event_pack(latest_flow),
                    "bull_top": event_pack(flow.get("bull_top")), "bear_top": event_pack(flow.get("bear_top")),
                    "microstructure": flow.get("microstructure", {}),
                },
                "volatility": self.vol, "positioning": self.positioning, "chain_insights": self.chain_insights,
                # IV Rank con historia propia: el motor no puede depender de un solo
                # proveedor REST para un número que él mismo puede medir.
                "iv_rank_native": iv_rank_native(alpaca_data.DATA_DIR, self.symbol, self.expiry_window,
                                                 (self.vol or {}).get("atm_iv")),
                "macro": self.macro, "large_prints": self.large_print_summary,
                "liquidity_zones": (self.large_print_summary or {}).get("liquidity_zones",{}),
                "scanner": self.scanner,
                "conditional_outcomes": conditional_outcome_report(self.scanner or {}, self.calibration or {}, self.regime_context or {}),
                "quant_synthesis": quant_synthesis,
                "market_truth": market_truth,
                "feature_intelligence": feature_intelligence,
                "decision_intelligence": decision_intelligence,
                "research_validation": research_validation,
                "market_state_field": market_state_field,
                "causality": self.causality_report or {},
                "temporal_truth": TEMPORAL_TRUTH.snapshot(self.symbol),
                "derivatives_intelligence": self.derivatives_intelligence_report or {},
                "expiry_intelligence": self.expiry_intelligence_report or {},
                "structural_intelligence": self.structural_intelligence_report or {},
                "flow_kinematics": self.flow_kinematics_report or {},
                "profile_bundle": self.profile_bundle_report or {},
                "versioned_market_state": self.versioned_market_state_report or VERSIONED_MARKET_STATE.latest(self.symbol),
                "operational_readiness": self.operational_readiness_report or {},
                "scenario_lab": self.scenario_lab_report or {},
                "quantum_shadow": self.quantum_shadow_report or {},
                "easy": easy,
                "institutional_research": self.institutional_research_report,
                "board": self.board_state(),
                "trace_orderflow": trace_orderflow,
                "expiry_window": self.expiry_info or {"mode":self.expiry_window,"label":LABELS.get(self.expiry_window,self.expiry_window),"expirations":[],"count":0},
                "zero_dte_status": zero_dte_status(self.history) if isinstance(self.history,pd.DataFrame) and not self.history.empty else {"available":False,"label":"SIN CADENA"},
                "expiry_windows": [{"mode":m,"label":LABELS[m]} for m in WINDOWS],
                "expiry_confluence": self.expiry_confluence_data,
                "flow_pro": flow_pro, "exposure_gamma": exp_gamma, "exposure_delta": exp_delta,
                "premarket": pre,
                "premarket_analysis": self.premarket_analysis_report or None,
                "native_options_structure": build_native_options_structure(self.gamma_delta or {}),
                "itm_structural_flow": structural_flow_frame(alpaca_data.DATA_DIR,self.symbol,self.expiry_window,current_gd=gd),
                "opra_diagnostics": self._opra_diagnostics(self.symbol),
                "replay": self.replay_context.describe(),
                "source_note": f"{self.symbol}: ITM QUANT MULTI ASSET usa observaciones propias por instrumento. Alpaca SIP/OPRA y tastytrade/DXLink aportan datos LIVE según entitlement, freshness y calidad; Quant Data aporta inteligencia de opciones normalizada como corroboración externa. La estructura Gamma/Delta/GEX/DEX permanece calculada por ITM QUANT y Scanner conserva la autoridad direccional. EXPIRY WINDOW = {(self.expiry_info or {}).get('label','AUTO')} ({(self.expiry_info or {}).get('count',0)} vencimientos). Scanner conserva la única autoridad direccional. Gamma/Delta/GEX/DEX son exposiciones/proxies matemáticos; no se presentan como inventario real de dealers ni como hedge-flow observado.",
            })

    def trace_pulse(self, visual_window: float | None = None) -> Dict[str, Any]:
        """Lightweight live Gamma/Delta repricing for TRACE side profiles.

        Only spot/time are repriced between structural snapshots. IV/OI/official
        option volume remain frozen; OPRA WebSocket activity is returned separately.
        """
        with self.lock:
            if self.replay_context.is_replay:
                return {"ready": False, "reason": "REPLAY_USES_CAUSAL_SNAPSHOT", "replay": self.replay_context.describe()}
            if not self.gamma_delta:
                return {"ready": False, "reason": "NO_STRUCTURE"}
            status = PRICE_STREAM.status()
            tick = status.get("last_tick") or {}
            rust_tick = RUST_CAUSAL_BRIDGE.latest_price(self.symbol) or {}
            provider_consensus = PROVIDER_BUS.snapshot(self.symbol)
            live_spot = _finite(
                provider_consensus.get("consensus_price") if provider_consensus.get("ready") else None,
                _finite(rust_tick.get("price"), _finite(tick.get("price"), _finite(self.gamma_delta.get("spot")))),
            )
            # Consensus values do not fabricate event timestamps. Keep causal as-of from
            # an observed stream event; the consensus itself exposes its source timestamps.
            asof = rust_tick.get("timestamp") or tick.get("timestamp")
            try:
                events = OPTION_FLOW_FABRIC.dataframe(self.symbol, minutes=6)
                if isinstance(events, pd.DataFrame) and not events.empty and "underlying_symbol" in events.columns:
                    events = events[events["underlying_symbol"].astype(str).str.upper() == str(self.symbol).upper()].copy()
            except Exception:
                events = pd.DataFrame()
            # Rust option prints are already merged into self.flow_events during refresh;
            # include their recent tail so TRACE OPRA activity does not disappear merely
            # because the Python WebSocket fallback is idle.
            try:
                persisted = self.flow_events.copy() if isinstance(self.flow_events,pd.DataFrame) else pd.DataFrame()
                if not persisted.empty and "timestamp" in persisted.columns:
                    persisted["timestamp"] = pd.to_datetime(persisted["timestamp"],errors="coerce")
                    persisted = persisted.dropna(subset=["timestamp"])
                    if not persisted.empty:
                        cutoff = persisted["timestamp"].max()-pd.Timedelta(minutes=6)
                        persisted = persisted[persisted["timestamp"]>=cutoff]
                        if "underlying_symbol" in persisted.columns:
                            persisted = persisted[persisted["underlying_symbol"].astype(str).str.upper() == str(self.symbol).upper()].copy()
                        events = pd.concat([events,persisted],ignore_index=True,sort=False) if not events.empty else persisted
                        keys=[c for c in ("contract_symbol","timestamp","trade_price","contracts") if c in events.columns]
                        if keys: events=events.drop_duplicates(keys,keep="last")
            except Exception as _e:
                _obs_note('service:2848', _e)
            pulse = build_trace_pulse(
                self.gamma_delta, self.symbol, live_spot, option_events=events, asof=asof,
                visual_window=float(visual_window or ((self.meta or {}).get("effective_window") or asset_info(self.symbol).get("window", STRIKE_WINDOW))),
                contract_multiplier=100.0,
            )
            pulse["symbol"] = str(self.symbol).upper()
            pulse["symbol_epoch"] = int(self.symbol_epoch)
            pulse["price_stream_connected"] = bool(status.get("connected"))
            pulse["provider_consensus"] = provider_consensus
            try:
                flow_health = OPTION_FLOW_FABRIC.health(self.symbol)
                pulse["opra_stream_connected"] = bool(flow_health.get("live_trade_sources"))
                pulse["opra_contract_universe"] = max([int((r or {}).get("contracts") or 0) for r in (flow_health.get("providers") or [])] or [0])
                pulse["option_flow_sources"] = list(flow_health.get("live_trade_sources") or [])
                pulse["option_flow_source"] = flow_health.get("selected_source")
                pulse["option_flow_redundancy"] = int(flow_health.get("redundancy") or 0)
            except Exception:
                pulse["opra_stream_connected"] = False
                pulse["opra_contract_universe"] = 0
                pulse["option_flow_sources"] = []
                pulse["option_flow_source"] = None
                pulse["option_flow_redundancy"] = 0
            return pulse

    @staticmethod
    def _session_reference_segments(day: date) -> list[Dict[str, Any]]:
        """Display-only windows over the selected US asset's observed extended-hours tape.

        ASIA/LONDON are reference windows for contextual reading; they do not assert that
        a US ETF/equity is listed on those exchanges.  Only Alpaca-observed bars render.
        """
        # London is defined in Europe/London local time and converted for the
        # selected NY calendar day. This avoids hard-coding 03:00/04:00 ET through
        # the US/UK daylight-saving transition weeks. The window is display context;
        # it never manufactures US-asset bars before the provider actually trades.
        london=ZoneInfo("Europe/London")
        noon_ny=datetime.combine(day,time(12,0),NY)
        london_day=noon_ny.astimezone(london).date()
        london_open=datetime.combine(london_day,time(8,0),london).astimezone(NY)
        london_mid=datetime.combine(london_day,time(12,0),london).astimezone(NY)
        defs=[
            ("ASIA WINDOW",datetime.combine(day,time(0,0),NY),london_open),
            ("LONDON WINDOW",london_open,london_mid),
            ("US PREMARKET",datetime.combine(day,time(4,0),NY),datetime.combine(day,time(9,30),NY)),
            ("NEW YORK RTH",datetime.combine(day,time(9,30),NY),datetime.combine(day,time(16,0),NY)),
            ("AFTER HOURS",datetime.combine(day,time(16,0),NY),datetime.combine(day,time(20,0),NY)),
        ]
        out=[]
        for label,a,b in defs:
            out.append({"label":label,"start_ny":a.isoformat(),"end_ny":b.isoformat(),"start_ec":a.astimezone(EC).isoformat(),"end_ec":b.astimezone(EC).isoformat(),"role":"REFERENCE_WINDOW_ONLY"})
        return out

    def _opra_diagnostics(self, symbol: str | None = None) -> Dict[str, Any]:
        """Backward-compatible name; diagnostics are provider-neutral in v1.26.2.

        Alpaca OPRA and tastytrade DXLink may both observe option prints.  The raw tape
        consumed by TRACE/Flow comes from OPTION_FLOW_FABRIC's dynamic canonical lane,
        while all observed providers remain visible for redundancy/failover diagnostics.
        """
        sym=str(symbol or self.symbol).upper()
        try:
            opra=OPTION_STREAM.status() or {}
        except Exception as exc:
            opra={"connected":False,"contracts":0,"underlying":None,"last_error":str(exc)[:160]}
        try:
            fabric=OPTION_FLOW_FABRIC.health(sym)
        except Exception as exc:
            fabric={"symbol":sym,"state":"ERROR","providers":[],"live_trade_sources":[],"selected_source":None,"redundancy":0,"bottleneck":str(exc)[:160]}
        providers_live=list(fabric.get("live_trade_sources") or [])
        selected=str(fabric.get("selected_source") or "") or None
        state=str(fabric.get("state") or "WAITING").upper()
        if state in {"LIVE_REDUNDANT","LIVE_SINGLE_SOURCE"}:
            status="LIVE"
        elif state=="NO_UNIVERSE":
            status="NO_UNIVERSE"
        elif state=="ERROR":
            status="ERROR"
        else:
            status="WAITING"
        prints5=0
        last_age=None
        contracts=0
        try:
            f=OPTION_FLOW_FABRIC.dataframe(sym, minutes=5)
            prints5=int(len(f)) if isinstance(f,pd.DataFrame) else 0
        except Exception as _e:
            _obs_note('service:2935', _e)
        for row in fabric.get("providers") or []:
            if str(row.get("source") or "").upper()==str(selected or "").upper():
                last_age=row.get("last_trade_age_seconds")
                contracts=int(row.get("contracts") or 0)
                break
        if contracts<=0:
            contracts=max([int((r or {}).get("contracts") or 0) for r in (fabric.get("providers") or [])] or [int(opra.get("contracts") or 0)])
        return {
            "symbol":sym,"status":status,"connected":bool(providers_live),"underlying":sym,
            "same_symbol":True,"contracts":contracts,"last_event_age_seconds":last_age,"prints_5m":prints5,
            "selected_source":selected,"providers_live":providers_live,"redundancy":int(fabric.get("redundancy") or 0),
            "bottleneck":fabric.get("bottleneck"),"flow_fabric":fabric,
            "alpaca_opra":{"connected":bool(opra.get("connected")),"contracts":int(opra.get("contracts") or 0),
                            "underlying":opra.get("underlying"),"last_error":opra.get("last_error") or None},
            "last_error":opra.get("last_error") or None if not providers_live else None,
            "authority":"DATA_DIAGNOSTIC_ONLY · OBSERVED_PROVIDER_FLOW",
        }

    def trace_session_bootstrap(
        self,
        timeframe: str = "1m",
        force: bool = False,
        symbol: str | None = None,
        session_scope: str = "full_day",
        cache_only: bool = False,
        allow_display_fallback: bool = True,
    ) -> Dict[str, Any]:
        """Truthful price bootstrap for the DOW-specialized runtime.

        DIA/XLI/XLF use Alpaca SIP history/cache. YM/MYM/DJX/VIX/VXD use
        observed tastytrade/DXLink candle history when that instrument and entitlement
        are actually available. A same-instrument DXLink candle may be used as a
        presentation-only fallback; no cross-instrument mapping or synthetic bars are
        introduced here.
        """
        tf=str(timeframe or "1m").lower()
        if tf not in {"1m","3m","5m","15m"}: tf="1m"
        scope=str(session_scope or "full_day").lower()
        if scope not in {"full_day","premarket_rth","rth"}: scope="full_day"
        with self.lock:
            active=self.symbol; mode=self.mode; epoch=self.symbol_epoch
        target=str(symbol or active).upper().strip()
        cfg=asset_info(target) if target in ASSETS else {}
        if target not in ASSETS or not cfg.get("selectable",True):
            return {"ready":False,"symbol":target,"timeframe":tf,"scope":scope,"reason":"ASSET_NOT_SELECTABLE","bars":[],"bootstrap_status":"UNSUPPORTED"}
        # v1.42.1 · La sesión la resuelve el calendario, no el reloj.
        #
        # Antes esto era `datetime.now(NY).date()`. A la 1 de la madrugada pedía
        # barras del día que acababa de empezar, una sesión que todavía no existe.
        # Alpaca respondía correctamente con cero barras, el diagnóstico decía
        # `sip:NO_BARS; iex:NO_BARS` y TRACE se quedaba sin velas — no por un fallo
        # del proveedor ni del gráfico, sino por preguntar por un día sin mercado.
        # Ahora, fuera de sesión, se pide la última sesión COMPLETADA, que sí tiene
        # barras, y el gráfico arranca lleno a cualquier hora.
        _ses = session_resolver.resolve()
        day = _ses.session_date
        key = f"{target}:{day.isoformat()}:{tf}:{scope}"
        now=pd.Timestamp.now(tz="UTC")
        with self.lock:
            cached=(self.trace_session_cache or {}).get(key)
        if cached and not force and not cache_only:
            try:
                age=(now-pd.Timestamp(cached.get("fetched_at"))).total_seconds()
                if age < 12 and cached.get("ready"):
                    return dict(cached)
            except Exception as _e:
                _obs_note('service:2991', _e)
        if mode!="LIVE":
            return {"ready":False,"symbol":target,"timeframe":tf,"scope":scope,"session":day.isoformat(),"timezone":"America/New_York","reason":"SESSION_BOOTSTRAP_LIVE_ONLY","bars":[],"bootstrap_status":"LIVE_ONLY"}

        provider=str(cfg.get("market_provider") or cfg.get("quant_provider") or "ALPACA").upper()
        frame=pd.DataFrame()
        alpaca_exc=None

        def _filter_dxlink(cdf: pd.DataFrame, *, fallback: bool, requested_feed: str) -> pd.DataFrame:
            if not isinstance(cdf,pd.DataFrame) or cdf.empty:
                return pd.DataFrame()
            out=cdf.copy()
            out["timestamp"]=pd.to_datetime(out["timestamp"],errors="coerce",utc=True)
            out=out.dropna(subset=["timestamp"])
            if out.empty:
                return out
            ts=out["timestamp"].dt.tz_convert(NY); local_day=ts.dt.date; clock=ts.dt.hour*60+ts.dt.minute
            keep=(local_day==day)
            if scope=="premarket_rth": keep &= (clock>=240)&(clock<960)
            elif scope=="rth": keep &= (clock>=570)&(clock<960)
            out=out.loc[keep].copy()
            if not out.empty and tf!="1m":
                rule={"3m":"3min","5m":"5min","15m":"15min"}[tf]
                out=out.set_index("timestamp").resample(rule).agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna(subset=["open","high","low","close"]).reset_index()
            if not out.empty:
                out["trades"]=0
                out.attrs.update({
                    "source":"TASTYTRADE_DXLINK_CANDLE_DISPLAY_FALLBACK" if fallback else "TASTYTRADE_DXLINK_CANDLE",
                    "fallback":bool(fallback),
                    "used_feed":"tastytrade_dxlink_candle",
                    "requested_feed":requested_feed,
                    "diagnostic":"SAME_INSTRUMENT_DXLINK_CANDLE_HISTORY" if fallback else "",
                })
            return out

        try:
            if provider=="TASTYTRADE":
                if TASTYTRADE.configured:
                    frame=_filter_dxlink(TASTYTRADE.market_data.candle_frame(target,"1m"),fallback=False,requested_feed="tastytrade")
                if frame.empty:
                    frame=pd.DataFrame(); frame.attrs.update({"source":"TASTYTRADE_DXLINK_CANDLE_UNAVAILABLE","fallback":False,"diagnostic":"NO_OBSERVED_TASTYTRADE_CANDLES"})
            else:
                if cache_only:
                    frame=alpaca_data.load_cached_stock_session_bars(symbol=target,timeframe=tf,session_date=day,session_scope=scope)
                else:
                    try:
                        frame=alpaca_data.fetch_stock_session_bars(
                            symbol=target,timeframe=tf,session_date=day,session_scope=scope,prefer_cache=False,
                            allow_display_fallback=bool(allow_display_fallback),request_timeout=8.0,
                        )
                    except Exception as exc:
                        alpaca_exc=exc
                        frame=pd.DataFrame(); frame.attrs.update({"source":"ALPACA_HISTORICAL_UNAVAILABLE","fallback":False,"diagnostic":str(exc)[:420]})

                # Same-instrument fail-soft only. It never changes Scanner/Quant authority.
                if (not isinstance(frame,pd.DataFrame) or frame.empty) and TASTYTRADE.configured:
                    try:
                        fallback=_filter_dxlink(TASTYTRADE.market_data.candle_frame(target,"1m"),fallback=True,requested_feed="alpaca")
                        if not fallback.empty:
                            frame=fallback
                    except Exception as _e:
                        _obs_note('service:3052', _e)
                if (not isinstance(frame,pd.DataFrame) or frame.empty) and alpaca_exc is not None:
                    frame=pd.DataFrame(); frame.attrs.update({"source":"ALPACA_HISTORICAL_UNAVAILABLE","fallback":False,"diagnostic":str(alpaca_exc)[:420]})
        except Exception as exc:
            return {
                "ready":False,"symbol":target,"timeframe":tf,"scope":scope,"session":day.isoformat(),
                "session_phase":_ses.phase,"session_reason":_ses.reason,
                "timezone":"America/New_York","reason":f"{provider}_PRICE_BOOTSTRAP_EXCEPTION: {exc}"[:260],
                "bars":[],"source":f"{provider}_HISTORICAL_UNAVAILABLE","bootstrap_status":"ERROR",
                "diagnostics":{"cache_only":bool(cache_only),"error":str(exc)[:220]},
            }

        attrs=dict(getattr(frame,"attrs",{}) or {}) if isinstance(frame,pd.DataFrame) else {}
        bars=[]
        if isinstance(frame,pd.DataFrame) and not frame.empty:
            mins={"1m":1,"3m":3,"5m":5,"15m":15}[tf]
            last_ts=pd.Timestamp(frame["timestamp"].max())
            for r in frame.to_dict("records"):
                t=pd.Timestamp(r["timestamp"])
                bars.append({
                    "t":t.isoformat(),"o":float(r["open"]),"h":float(r["high"]),"l":float(r["low"]),"c":float(r["close"]),
                    "v":float(r.get("volume",0) or 0),"sv":0.0,"n":int(r.get("trades",0) or 0),
                    "complete":bool(t+pd.Timedelta(minutes=mins)<=last_ts),
                    "source":str(attrs.get("source") or ("TASTYTRADE_DXLINK_CANDLE" if provider=="TASTYTRADE" else ("ALPACA_LOCAL_PRICE_CACHE" if cache_only else "ALPACA_HISTORICAL"))),
                })
        source=str(attrs.get("source") or ("TASTYTRADE_DXLINK_CANDLE_UNAVAILABLE" if provider=="TASTYTRADE" else ("ALPACA_LOCAL_PRICE_CACHE" if cache_only else "ALPACA_HISTORICAL_UNAVAILABLE")))
        fallback=bool(attrs.get("fallback"))
        if bars:
            status="CACHE_READY" if cache_only or "CACHE" in source else ("DISPLAY_FALLBACK" if fallback else ("DXLINK_READY" if provider=="TASTYTRADE" else "SIP_READY"))
            reason=None
        else:
            status="CACHE_MISS" if cache_only else "PROVIDER_NO_BARS"
            reason=str(attrs.get("diagnostic") or f"NO_OBSERVED_{provider}_BARS")[:260]
        payload={
            "ready":bool(bars),"symbol":target,"active_symbol":active,"symbol_epoch":epoch if target==active else None,
            "timeframe":tf,"scope":scope,"session":day.isoformat(),"timezone":"America/New_York",
            "session_phase":_ses.phase,"session_reason":_ses.reason,
            "session_is_current":_ses.is_current,"session_note":_ses.note,
            "start":{"full_day":"00:00:00","premarket_rth":"04:00:00","rth":"09:30:00"}[scope],
            "open":"09:30:00","close":"16:00:00","rth_open":"09:30:00","rth_close":"16:00:00","bars":bars,
            "last_event_time":bars[-1]["t"] if bars else None,
            "handoff":("DXLINK_CANDLE→LIVE" if provider=="TASTYTRADE" else ("CACHE→ALPACA→MERGE→DEDUPE→LIVE_SIP" if cache_only else "BACKFILL→MERGE→DEDUPE→LIVE_SIP")),
            "handoff_source":source,"source":source,
            "source_priority":"QUALITY_AWARE_COMPARABLE_DATA · CAUSAL_DISPLAY_FALLBACK · LOCAL_CACHE_CONTINUITY",
            "reference_segments":self._session_reference_segments(day),
            "segment_disclosure":"ASIA/LONDON are reference windows over observed US-asset extended-hours data; missing bars remain missing.",
            "fetched_at":now.isoformat(),"bootstrap_status":status,"reason":reason,
            "display_only_fallback":fallback,"authority":"PRICE_PRESENTATION_ONLY",
            "diagnostics":{
                "cache_only":bool(cache_only),"cache_hit":bool(attrs.get("cache_hit")),
                "requested_feed":attrs.get("requested_feed"),"used_feed":attrs.get("used_feed"),
                "fallback":fallback,"attempts":attrs.get("attempts") or [],
                "detail":str(attrs.get("diagnostic") or "")[:420],"bar_count":len(bars),
            },
        }
        if payload["ready"] and not cache_only:
            with self.lock:
                self.trace_session_cache[key]=dict(payload)
                if len(self.trace_session_cache)>48:
                    self.trace_session_cache=dict(list(self.trace_session_cache.items())[-48:])
        return payload

    def nextgen_trace_price_only(self, timeframe: str = "1m", tail_minutes: int = 60, visual_window: float | None = None, reason: str = "QUANT_WARMING") -> Dict[str, Any]:
        """Fail-soft TRACE payload that keeps observed price/history visible while Quant hydrates.

        This path never fabricates Gamma/Delta/Scanner state. It publishes only same-instrument
        observed price candles (live fabric first, then cached provider backfill) and preserves
        empty structural collections until the full quantitative snapshot is ready.
        """
        symbol=str(self.symbol).upper(); epoch=int(self.symbol_epoch)
        ticks=_live_ticks_for(symbol)
        candles=candles_from_ticks(ticks,timeframe=timeframe,tail_minutes=tail_minutes)
        bootstrap={}
        tf_min = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(str(timeframe or "1m").lower(), 1)
        want_bars = 0 if int(tail_minutes) == 0 else max(1, int(tail_minutes) // tf_min)
        short_coverage = bool(want_bars) and len(candles) < min(want_bars, 30)
        if not candles or short_coverage:
            try:
                bootstrap=self.trace_session_bootstrap(timeframe,False,symbol,"full_day",True,True) or {}
                bars=bootstrap.get("bars") or []
                if isinstance(bars,list) and len(bars) > len(candles):
                    candles=[dict(x) for x in bars if isinstance(x,dict)]
            except Exception as exc:
                bootstrap={"ready":False,"reason":f"BOOTSTRAP_FAIL: {type(exc).__name__}"}
        spot=None
        try:
            if candles: spot=float(candles[-1].get("c"))
        except Exception: spot=None
        if spot is None:
            spot=_fabric_last_price(symbol)
        ready=bool(candles or spot is not None)
        payload={
            "ready":ready,"degraded":True,"quant_ready":False,"reason":str(reason or "QUANT_WARMING"),
            "symbol":symbol,"symbol_epoch":epoch,"timeframe":str(timeframe or "1m"),"tail_minutes":int(tail_minutes),
            "candles":candles,"option_prints":[],"levels":[],"hiro":{"ready":False,"reason":"QUANT_WARMING","points":[]},
            "key_levels_report":{"ready":False,"symbol":symbol},
            "profiles":{"ready":False,"symbol":symbol,"rows":[],"spot":spot,"gamma_delta_interaction":{}},
            "decision":{},"market_state":{"top_factors":[]},"quality":{},"dealer_intelligence":{},
            "native_options_structure":{"ready":False,"status":"QUANT_WARMING","authority":"ITM_QUANT_NATIVE_OPTIONS_STRUCTURE","provider_dependency":"NONE"},"provenance":{"authority":"OBSERVED_PRICE_ONLY","fail_soft":True},
            "model_risk":{"scenarios":[]},"session_bootstrap":bootstrap,
        }
        return payload

    def _resolve_visual_window(self, requested: float | None) -> float:
        """Banda de strikes del heatmap, PROPORCIONAL al precio del activo.

        v1.46.0 · Los llamadores pasaban `visual_window=12.0` —doce dólares— para
        cualquier símbolo. Sobre DIA (~534 $) eso es ±2.2 %, que es la banda con la
        que se validó; sobre una acción de 9.5 $ es ±126 %, o sea la cadena entera,
        y la estructura real quedaba comprimida en unos pocos píxeles. El heatmap
        «pobre o vacío» en varios activos empezaba aquí.

        Se resuelve desde el catálogo, que ya sabe derivar la ventana del precio.
        Un `window` declarado a mano para un instrumento con escala propia —los
        futuros— sigue mandando.

        v1.47.0 · Y traía un `NameError`. Esta función se escribió copiando dos
        líneas de `terminal_api`, donde el conversor numérico se llama `_f`; aquí
        se llama `_finite`. El resultado era que CADA construcción del trace
        lanzaba `NameError: name '_f' is not defined`, se tragaba en el `except`
        de `/api/terminal/bundle` y la terminal servía un trace vacío: velas,
        perfiles y niveles en blanco, con un DEGRADED en el registro y ninguna
        pista en pantalla. Un `except Exception` que convierte un fallo de
        programación en un panel vacío es el peor sitio donde puede esconderse
        un error, porque parece falta de datos.
        """
        from .core.assets import chain_window_for
        spot = _finite(self.gamma_delta.get("spot")) if isinstance(self.gamma_delta, dict) else None
        if spot is None:
            spot = _finite((self.snapshot or {}).get("spot") if isinstance(self.snapshot, dict) else None)
        try:
            resolved = float(chain_window_for(self.symbol, spot=spot).get("window") or 0.0)
        except Exception as exc:
            _obs_note("service:resolve_visual_window", exc, severity="DEGRADED")
            resolved = 0.0
        if resolved > 0:
            return resolved
        # Sin precio todavía no se puede derivar nada; se respeta lo pedido.
        return float(requested) if requested and requested > 0 else 12.0

    def nextgen_trace(self, timeframe: str = "1m", tail_minutes: int = 60,
                      visual_window: float | None = None) -> Dict[str, Any]:
        """Structured payload for the framework-independent TRACE renderer."""
        visual_window = self._resolve_visual_window(visual_window)
        with self.lock:
            if self.replay_context.is_replay:
                b = self._build_replay_bundle()
                if not b.get("ready"):
                    return {"ready": False, "error": b.get("error"), "replay": self.replay_context.describe()}
                gd = b["gd"]; ticks = b["tape"]; events = b["selected_events"]
                scanner = b["scanner"]; asof = self.replay_context.asof
                market_state = build_market_state_field(
                    scanner or {}, gd, flow_session_summary(events), b.get("vol") or {}, b.get("trace_orderflow") or {},
                    symbol=self.symbol, dealer=self.dealer_intelligence_report or {}, external=self.external_market_report or {},
                    positioning=self.positioning or {}, macro=b.get("macro") or {},
                )
            else:
                if not self.gamma_delta:
                    return self.nextgen_trace_price_only(timeframe, tail_minutes, visual_window, "QUANT_WARMING")
                gd = self.gamma_delta; ticks = _live_ticks_for(self.symbol)
                # DEMO must exercise the same session/timeframe renderer even when the
                # live websocket stream is intentionally absent.  Use the DEMO chain's
                # observed synthetic underlying snapshots as sparse price ticks.  LIVE
                # never fabricates this fallback: an empty LIVE stream remains empty so
                # the UI can report unavailable data honestly.
                if self.mode == "DEMO" and (not isinstance(ticks, pd.DataFrame) or ticks.empty):
                    try:
                        h = self.history.copy() if isinstance(self.history, pd.DataFrame) else pd.DataFrame()
                        if not h.empty and {"timestamp", "underlying_price"}.issubset(h.columns):
                            d = h[["timestamp", "underlying_price"]].copy()
                            d["timestamp"] = pd.to_datetime(d["timestamp"], errors="coerce")
                            d["price"] = pd.to_numeric(d["underlying_price"], errors="coerce")
                            d = d.dropna(subset=["timestamp", "price"]).groupby("timestamp", as_index=False)["price"].median().sort_values("timestamp")
                            d["size"] = 0.0; d["signed_volume"] = 0.0; d["seq"] = range(len(d))
                            ticks = d[["timestamp", "price", "size", "signed_volume", "seq"]]
                    except Exception as _e:
                        _obs_note('service:3184', _e)
                events = filter_events(self.flow_events, self.expiry_info)
                scanner = self.scanner or {}; asof = datetime.now(EC)
                market_state = self.market_state_field or build_market_state_field(
                    scanner, gd, self.flow_summary or {}, self.vol or {}, self._trace_orderflow_state(persist=True),
                    symbol=self.symbol, dealer=self.dealer_intelligence_report or {}, external=self.external_market_report or {},
                    positioning=self.positioning or {}, macro=self.macro or {},
                )
            _fg = None
            _structure_blocked = False
            _blocked_motive = ""
            if not self.replay_context.is_replay:
                _fg = self.current_publication_gate()
                _structure_blocked = not (isinstance(_fg, dict) and _fg.get("publicar_permitido") is True)
                if _structure_blocked:
                    # Un gate de frescura debe bloquear ACCIONABILIDAD, no borrar el
                    # último contexto estructural válido ni el flujo observado. Borrar
                    # perfiles/heatmap/niveles hacía que TRACE, OI y EXPOSICIÓN parecieran
                    # perder la cadena completa durante un retraso transitorio de OPRA.
                    # Se conserva el snapshot anterior como CONTEXTO NO ACCIONABLE y se
                    # mantiene vivo el precio/flujo observado. Nada se presenta como fresco.
                    _blocked_motive = str((_fg or {}).get("motivo") or (_fg or {}).get("reason") or
                                          "frescura critica no verificada")
            payload = build_nextgen_trace_payload(
                symbol=self.symbol, gd=gd,
                scanner=({} if _structure_blocked else scanner),
                market_state=({} if _structure_blocked else market_state),
                ticks=ticks, option_events=events, timeframe=timeframe, tail_minutes=tail_minutes,
                visual_window=visual_window, asof=asof,
                data_quality=((b.get("data_quality") or {}).get("score") if self.replay_context.is_replay else (self.data_quality_report or {}).get("score")),
                model_health=((b.get("model_health") or {}).get("score") if self.replay_context.is_replay else (self.model_health or {}).get("score")),
                dealer_report=({} if self.replay_context.is_replay else (self.dealer_intelligence_report or {})),
                native_options_structure=build_native_options_structure(gd),
            )
            payload["symbol_epoch"] = self.symbol_epoch
            if _structure_blocked:
                payload.update({
                    "blocked": True,
                    "context_only": True,
                    "actionable": False,
                    "quant_ready": False,
                    "structure_stale": True,
                    "reason": "PUBLICATION_BLOCKED_STALE_DATA",
                    "publication_gate": dict(_fg or {}),
                    "publication_blocked_motive": _blocked_motive,
                    "authority": "LAST_GOOD_STRUCTURE_CONTEXT_PLUS_OBSERVED_LIVE",
                })
                # Scanner/dirección jamás sobreviven a un gate de frescura. El
                # contexto gráfico sí; una señal operable, no.
                payload["decision"] = {}
                payload["market_state"] = {"top_factors": [], "probability": False}
                payload["disclosure"] = (
                    "CONTEXTO ESTRUCTURAL RETENIDO: snapshot de opciones fuera del SLA. "
                    "Precio y flujo observados siguen LIVE; niveles/perfiles se muestran "
                    "sólo como último contexto conocido y NO son accionables hasta refrescar."
                )
            # La fabric de ticks puede estar fría (arranque, reconexión de proveedor,
            # cambio de activo) mientras el motor ya publica estructura completa. Sin
            # este respaldo, TRACE, FLUJO, RESUMEN y DARK POOL se quedan sin gráfico
            # aunque el proveedor esté LIVE. El bootstrap devuelve historia observada
            # del mismo instrumento (Alpaca SIP / DXLink), no barras sintéticas.
            _have = payload.get("candles") or []
            # No basta con que haya velas: si la fabric sólo tiene los últimos
            # minutos, DARK POOL y FLUJO dibujan prints de toda la sesión contra una
            # línea de precio de un par de barras, que es lo que se veía "raro".
            _tf_min = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(str(timeframe or "1m").lower(), 1)
            _want_bars = 0 if int(tail_minutes) == 0 else max(1, int(tail_minutes) // _tf_min)
            _short = bool(_want_bars) and len(_have) < min(_want_bars, 30)
            if (not _have or _short) and not self.replay_context.is_replay:
                try:
                    boot = self.trace_session_bootstrap(timeframe, False, self.symbol, "full_day", False, True) or {}
                    bars = boot.get("bars") or []
                    if isinstance(bars, list) and len(bars) > len(_have):
                        payload["candles"] = [dict(x) for x in bars if isinstance(x, dict)]
                        payload["candle_source"] = "SESSION_BOOTSTRAP_BACKFILL"
                        payload["candle_backfill_reason"] = ("LIVE_TICK_FABRIC_EMPTY" if not _have
                                                             else "LIVE_TICK_FABRIC_SHORT_COVERAGE")
                    else:
                        payload["candle_backfill_reason"] = str(boot.get("reason") or boot.get("bootstrap_status") or "BOOTSTRAP_EMPTY")
                except Exception as exc:
                    payload["candle_backfill_reason"] = f"BOOTSTRAP_FAIL: {type(exc).__name__}"
                    _obs_note("service:trace_candle_backfill", exc, severity="DEGRADED")
            payload["profile_bundle"] = self.profile_bundle_report or build_profile_bundle(_exposure_frame(gd, self.snapshot),view="Net",spot=gd.get("spot"))
            payload["expiry_intelligence"] = self.expiry_intelligence_report or build_expiry_intelligence(_exposure_frame(gd, self.snapshot))
            payload["structural_intelligence"] = self.structural_intelligence_report or {}
            payload["flow_kinematics"] = (build_flow_kinematics(events, ticks, symbol=self.symbol) if self.replay_context.is_replay else (self.flow_kinematics_report or build_flow_kinematics(events, ticks, symbol=self.symbol)))
            payload["derivatives_intelligence"] = self.derivatives_intelligence_report or {}
            payload["versioned_market_state"] = self.versioned_market_state_report or VERSIONED_MARKET_STATE.latest(self.symbol)
            if not self.replay_context.is_replay:
                payload["itm_structural_flow"] = structural_flow_frame(alpaca_data.DATA_DIR, self.symbol, self.expiry_window, current_gd=gd)
                payload["opra_diagnostics"] = self._opra_diagnostics(self.symbol)
            else:
                payload["itm_structural_flow"] = {"ready":False,"status":"REPLAY_DISABLED","authority":"PRESENTATION_CONTEXT_ONLY","series":[]}
                payload["opra_diagnostics"] = {"symbol":self.symbol,"status":"REPLAY","connected":False,"authority":"DATA_DIAGNOSTIC_ONLY"}
            return payload

    def nextgen_surface(self, iv_shift: float = 0.0, option_view: str = "Net") -> Dict[str, Any]:
        """Structured multi-field surface payload with revision-aware caching.

        The payload already contains every 3D field (Gamma/Delta/Vanna/Charm/OI/etc.).
        Once a dataset is loaded the browser can switch fields locally without a network
        round trip; this cache prevents rebuilding the same matrix for duplicate requests.
        """
        view = str(option_view or "Net")
        shift = float(iv_shift)
        with self.lock:
            if self.replay_context.is_replay:
                b = self._build_replay_bundle()
                if not b.get("ready"):
                    return {"ready": False, "error": b.get("error")}
                gd = b.get("gd") or {}
            else:
                gd = self.gamma_delta
                _fg=self.current_publication_gate()
                if not (isinstance(_fg,dict) and _fg.get("publicar_permitido") is True):
                    return {"ready":False,"blocked":True,"reason":"PUBLICATION_BLOCKED_STALE_DATA","symbol":self.symbol,"symbol_epoch":self.symbol_epoch,"publication_gate":dict(_fg or {})}
            if not gd:
                return {"ready": False, "symbol": self.symbol, "symbol_epoch": self.symbol_epoch, "reason": "QUANT_WARMING"}
            symbol = self.symbol
            epoch = int(self.symbol_epoch)
            revision = int(self.analytics_revision)
            dealer = self.dealer_intelligence_report or {}
            calibration = self.calibration or {}
            replay = bool(self.replay_context.is_replay)
            key = f"{symbol}|{epoch}|{revision}|{view}|{shift:.6f}|{int(replay)}"
            cached = self.surface_payload_cache.get(key)
            if isinstance(cached, dict):
                return cached
        # Expensive matrix creation is deliberately outside the state lock. Analytical
        # snapshots are replaced atomically, not mutated in-place after publication.
        payload = build_quant_surface_payload(
            symbol=symbol, gd=gd, dealer=dealer, calibration=calibration,
            scenario_iv_shift=shift, option_view=view,
        )
        payload["symbol_epoch"] = epoch
        payload["analytics_revision"] = revision
        payload["cache_key"] = key
        with self.lock:
            if self.symbol == symbol and int(self.symbol_epoch) == epoch and int(self.analytics_revision) == revision:
                self.surface_payload_cache[key] = payload
        return payload

    def surface_slice_chart(self, metric: str = "Gamma", option_view: str = "Net", render_style: str = "Barras") -> Dict[str, Any]:
        """Fast 2D cross-section with latest-state, revision-aware representation cache."""
        metric = str(metric or "Gamma")
        option_view = str(option_view or "Net")
        render_style = str(render_style or "Barras")
        with self.lock:
            _fg=self.current_publication_gate()
            if not self.replay_context.is_replay and not (isinstance(_fg,dict) and _fg.get("publicar_permitido") is True):
                return {"ready":False,"blocked":True,"reason":"PUBLICATION_BLOCKED_STALE_DATA","symbol":self.symbol,"symbol_epoch":self.symbol_epoch,"publication_gate":dict(_fg or {})}
            if not self.gamma_delta:
                return {"ready":False,"symbol":self.symbol,"symbol_epoch":self.symbol_epoch,"reason":"QUANT_WARMING"}
            symbol = self.symbol
            epoch = int(self.symbol_epoch)
            revision = int(self.analytics_revision)
            gd = self.gamma_delta
            key = f"{symbol}|{epoch}|{revision}|{metric}|{option_view}|{render_style}"
            cached = self.surface_slice_cache.get(key)
            if isinstance(cached, dict):
                return cached
        fig = _surface_slice_figure(gd, metric, option_view, render_style)
        payload = {
            "ready":True,"symbol":symbol,"symbol_epoch":epoch,"analytics_revision":revision,
            "figure":_fig_json(fig,symbol),
            "chart_state":{"metric":metric,"option_view":option_view,"render":render_style},
        }
        with self.lock:
            if self.symbol == symbol and int(self.symbol_epoch) == epoch and int(self.analytics_revision) == revision:
                self.surface_slice_cache[key] = payload
        return payload

    def charts(self, chain_metric: str = "Q-Score", surface_metric: str = "Gamma",
               net_drift_scope: str = "Todas exp.", exposure_metric: str = "Gamma",
               positioning_metric: str = "Delta-adjusted", volume_metric: str = "Calls vs Puts",
               gex_matrix_metric: str = "GEX", gex_matrix_baseline: str = "OPEN", gex_matrix_threshold: Any = "AUTO",
               gamma_migration_mode: str = "DIFFERENCE",
               trace_landscape_lens: str = "Gamma", trace_landscape_scale: str = "session",
               surface_render_style: str = "Superficie", surface_option_view: str = "Net",
               surface_slice_metric: str = "Gamma", surface_slice_render: str = "Barras",
               view: str = "all") -> Dict[str, Any]:
        with self.lock:
            replay = self.replay_context.is_replay
            if replay:
                instant=self._instant_replay_package()
                if instant is not None:
                    cached=dict(instant.get("charts") or {})
                    if cached:
                        cached["symbol"]=self.symbol;cached["symbol_epoch"]=int(self.symbol_epoch);cached["replay"]={**self.replay_context.describe(),"instant_precomputed":True}
                        return cached
                b=self._build_replay_bundle()
                if not b.get("ready"): return {"error":b.get("error"),"replay":self.replay_context.describe()}
                gd=b["gd"]; livegd=gd; selected_events=b["selected_events"]
                history=b["selected_history"]; large_current=b["large"]; vol_current=b["vol"]; macro_current=b["macro"]; scanner_current=b["scanner"]
                live_trace_ticks=b["tape"]; trace_orderflow=b["trace_orderflow"]; premarket_bars=None
                anchors={}
            else:
                if not self.gamma_delta:return {}
                gd=self.gamma_delta;livegd=self.gamma_delta;selected_events=filter_events(self.flow_events,self.expiry_info)
                history=self.history;large_current=self.large_prints;vol_current=self.vol;macro_current=self.macro;scanner_current=self.scanner
                live_trace_ticks=_live_ticks_for(self.symbol);trace_orderflow=self._trace_orderflow_state(persist=True)
                premarket_bars=None
                try:
                    # Legacy variable name retained for figure compatibility; the frame now
                    # represents the global London→current-session activity tape.
                    premarket_bars=(self.session_flow_tape or {}).get("bars")
                    if premarket_bars is None and self.mode=="LIVE":
                        premarket_bars=london_activity_tape_summary(self.symbol, live_ticks=live_trace_ticks).get("bars")
                    if premarket_bars is None:
                        premarket_bars=(self.premarket_tape or {}).get("bars")
                except Exception:premarket_bars=None
                anchors=_trace_anchors(self.symbol,self.expiry_window)

            requested_view=str(view or "all").strip().lower()
            all_views=requested_view=="all"
            need_trace=all_views or requested_view in {"command","trace"}
            need_chain=all_views or requested_view=="structure"
            need_flow=all_views or requested_view in {"flow","trace","netdrift","prints"}
            need_netdrift=all_views or requested_view in {"netdrift"}
            need_exposure=all_views or requested_view=="exposure"
            need_gexmatrix=all_views or requested_view=="gexmatrix"
            need_positioning=all_views or requested_view=="positioning"
            need_volatility=all_views or requested_view=="volatility"
            need_surface=all_views or requested_view=="surface"
            need_macro=all_views or requested_view=="macro"
            need_prints=all_views or requested_view=="prints"
            need_scanner=all_views or requested_view=="scanner"
            need_equity_hub=all_views or requested_view=="equity_hub"

            result={"symbol":self.symbol,"symbol_epoch":self.symbol_epoch,
                    "chart_state":{"surface_metric":surface_metric,"surface_render":surface_render_style,
                                   "surface_option_view":surface_option_view,"surface_slice_metric":surface_slice_metric,
                                   "surface_slice_render":surface_slice_render,"net_drift_scope":net_drift_scope,"gamma_migration_mode":gamma_migration_mode},
                    "replay":self.replay_context.describe() if replay else ReplayContext.live(self.symbol).describe(),
                    "computed_view":requested_view}

            if need_trace:
                # TRACE rendering is exclusively NextGen Canvas (/api/nextgen/trace).
                # /api/charts only carries the order-flow companion payload used by
                # the surrounding diagnostics; no duplicate Plotly TRACE is built.
                result["trace_orderflow"]=_jsonable(trace_orderflow)

            if need_chain:
                result["chain"]=_fig_json(_chain_figure(livegd,chain_metric,self.symbol),self.symbol)

            session_drift=pd.DataFrame();flow_history=history
            if need_flow:
                session_drift=session_metric_frame(alpaca_data.DATA_DIR,self.symbol,self.expiry_window) if self.mode=="LIVE" and not replay else pd.DataFrame()
                # Presentation continuity is provider-aware. No secondary analytical module
                # is computed merely because a different screen is open.
                try:
                    if str(asset_info(self.symbol).get("market_provider") or asset_info(self.symbol).get("quant_provider") or "ALPACA").upper()=="ALPACA":
                        day_ny=datetime.now(NY).date()
                        px_cache=alpaca_data.load_cached_stock_session_bars(symbol=self.symbol,timeframe="1m",session_date=day_ny,session_scope="full_day")
                        if isinstance(px_cache,pd.DataFrame) and not px_cache.empty:
                            pc=px_cache[["timestamp","close"]].copy().rename(columns={"close":"underlying_price"})
                            pc["timestamp"]=pd.to_datetime(pc["timestamp"],errors="coerce");pc=pc.dropna(subset=["timestamp","underlying_price"])
                            if not pc.empty:flow_history=pd.concat([flow_history,pc],ignore_index=True,sort=False) if isinstance(flow_history,pd.DataFrame) and not flow_history.empty else pc
                except Exception as _e:
                    _obs_note('service:3403', _e)
                if isinstance(session_drift,pd.DataFrame) and not session_drift.empty:
                    sh=session_drift[["timestamp","spot"]].copy().rename(columns={"spot":"underlying_price"});sh=sh.dropna(subset=["timestamp","underlying_price"])
                    if not sh.empty:flow_history=pd.concat([flow_history,sh],ignore_index=True,sort=False) if isinstance(flow_history,pd.DataFrame) and not flow_history.empty else sh
                try:
                    if isinstance(live_trace_ticks,pd.DataFrame) and not live_trace_ticks.empty:
                        lp=live_trace_ticks[["timestamp","price"]].copy().rename(columns={"price":"underlying_price"})
                        lp["timestamp"]=pd.to_datetime(lp["timestamp"],errors="coerce");lp["underlying_price"]=pd.to_numeric(lp["underlying_price"],errors="coerce")
                        lp=lp.dropna(subset=["timestamp","underlying_price"])
                        if not lp.empty:flow_history=pd.concat([flow_history,lp],ignore_index=True,sort=False) if isinstance(flow_history,pd.DataFrame) and not flow_history.empty else lp
                except Exception as _e:
                    _obs_note('service:3413', _e)

            if all_views or requested_view in {"flow","trace"}:
                result["flow"]=_fig_json(_flow_figure(selected_events,flow_history,self.symbol),self.symbol)
                result["flow_pro"]=_fig_json(flow_unusual_pro_figure(selected_events,flow_history,livegd,premarket_bars,self.symbol),self.symbol)
            if need_netdrift:
                qd_values=_quantdata_options_values(self.symbol)
                result["net_drift"]=_fig_json(quantdata_net_drift_figure(qd_values,symbol=self.symbol,price_history=flow_history),self.symbol)
                qd_series=(qd_values.get("net_drift_series") or {}) if isinstance(qd_values,dict) else {}
                result["net_drift_summary"]=_jsonable({
                    "source":"QUANTDATA",
                    "observed":bool(qd_series.get("available")),
                    "provider_stale":bool(qd_values.get("_provider_stale")) if isinstance(qd_values,dict) else True,
                    "latest":qd_series.get("latest"),
                    "buckets":len(qd_series.get("buckets") or []),
                    "scope":"TODAS EXP. · QUANT DATA OBSERVED",
                })
            if need_exposure:
                result["exposure_strike"]=_fig_json(exposure_by_strike_pro_figure(livegd,metric=exposure_metric,symbol=self.symbol),self.symbol)
                result["exposure_summary"]=_jsonable(exposure_summary(livegd,exposure_metric))
            if need_gexmatrix:
                result["gex_matrix"]=_fig_json(gex_matrix_figure(livegd,exp_count=99,mode=gex_matrix_metric,baseline=gex_matrix_baseline,text_threshold_m=gex_matrix_threshold),self.symbol)
                result["gamma_migration"]=_fig_json(gamma_migration_figure(livegd,mode=gamma_migration_mode,max_strikes=19),self.symbol)
                result["gamma_migration_digest"]=_jsonable(migration_digest(livegd,mode=gamma_migration_mode))
            if need_positioning:
                result["net_positioning"]=_fig_json(net_positioning_by_strike_figure(livegd,mode=positioning_metric),self.symbol)
                result["volume"]=_fig_json(volume_by_strike_figure(livegd,mode=volume_metric),self.symbol)
                result["volume_summary"]=_jsonable(volume_summary(livegd))
            if need_volatility:
                result["volatility"]=_fig_json(_vol_figure(vol_current),self.symbol)
                skew_current=_skew_current(livegd,self.symbol)
                result["skew"]=_fig_json(_skew_figure(skew_current),self.symbol)
                result["skew_summary"]=_jsonable(skew_current)
            if need_equity_hub:
                result["equity_hub_gamma_model"]=_fig_json(_equity_hub_gamma_model_figure(livegd,self.symbol),self.symbol)
                mc_current=_monte_carlo_current(livegd,self.symbol)
                result["equity_hub_monte_carlo"]=_fig_json(_monte_carlo_figure(mc_current),self.symbol)
                result["equity_hub_monte_carlo_summary"]=_jsonable(mc_current)
            if need_surface:
                # The primary 3D surface is exclusively /api/nextgen/surface (GPU/WebGL).
                # Keep only the current 2D/session analytical companions here.
                result["trace_landscape"]=_fig_json(trace_landscape_3d(gd.get("enriched",pd.DataFrame()),spot=gd.get("spot"),lens=trace_landscape_lens,window_strikes=21,scale_mode=trace_landscape_scale,scale_anchors=anchors,symbol=self.symbol),self.symbol)
                result["surface_slice"]=_fig_json(_surface_slice_figure(livegd,surface_slice_metric,surface_option_view,surface_slice_render),self.symbol)
                result["surface_main_slice"]=_fig_json(_surface_slice_figure(livegd,surface_metric,surface_option_view,surface_render_style if str(surface_render_style)!="Superficie" else "Barras"),self.symbol)
            if need_macro:result["macro"]=_fig_json(macro_chart(macro_current or {}),self.symbol)
            if need_prints:result["large_prints"]=_fig_json(large_print_chart(large_current,flow_history,self.symbol),self.symbol)
            if need_scanner:result["scanner"]=_fig_json(scanner_route_figure(scanner_current),self.symbol)
            return result

    def _replay_tables(self) -> Dict[str,Any]:
        instant=self._instant_replay_package()
        if instant is not None:
            tables=dict(instant.get("tables") or {})
            tables["replay"]={**self.replay_context.describe(),"instant_precomputed":True}
            return tables
        b=self._build_replay_bundle()
        if not b.get("ready"):return {"levels":[],"flow_events":[],"large_prints":[],"macro_events":[],"audit":[{"check":"REPLAY","status":"WARN","detail":b.get("error")} ]}
        gd=b["gd"];cur=_current_structure_frame(gd)
        cols=[c for c in ["strike","signed_gex","delta_exposure","open_interest","option_volume","gamma_delta_level_score","state_delta","state_confidence_delta"] if c in cur.columns]
        by_strike=cur.sort_values("strike")[cols].head(100) if not cur.empty else pd.DataFrame()
        by_score=cur.sort_values("gamma_delta_level_score",ascending=False)[cols].head(60) if not cur.empty and "gamma_delta_level_score" in cur.columns else by_strike
        flow=b["selected_events"].copy() if isinstance(b.get("selected_events"),pd.DataFrame) else pd.DataFrame()
        if not flow.empty:
            flow=flow.sort_values("flow_score",ascending=False).head(80) if "flow_score" in flow.columns else flow.head(80)
        large=b["large"].copy() if isinstance(b.get("large"),pd.DataFrame) else pd.DataFrame()
        audit=[{"check":"REPLAY GLOBAL CLOCK","status":"OK" if self.replay_context.asof else "WARN","detail":self.replay_context.describe().get("note")},
               {"check":"NO FUTURE · STRUCTURE","status":"OK","detail":"Snapshots cortados antes del análisis."},
               {"check":"NO FUTURE · FLOW/PRINTS/TAPE","status":"OK","detail":"Eventos y Tape truncados al mismo asof."},
               {"check":"PROBABILITY MODEL","status":"WARN","detail":"Snapshot histórico del modelo no disponible: Replay usa Evidence legacy, no el modelo actual."},
               {"check":"MACRO HISTÓRICO","status":"WARN","detail":"No se sustituye por macro actual; queda UNAVAILABLE en Replay."}]
        return {"levels":_jsonable(by_strike.to_dict("records")) if not by_strike.empty else [],"levels_by_strike":_jsonable(by_strike.to_dict("records")) if not by_strike.empty else [],
                "levels_by_score":_jsonable(by_score.to_dict("records")) if not by_score.empty else [],"flow_events":_jsonable(flow.to_dict("records")) if not flow.empty else [],
                "large_prints":_jsonable(large.to_dict("records")) if not large.empty else [],"macro_events":[],"audit":audit,"replay":self.replay_context.describe()}

    def available_trace_dates(self) -> list[str]:
        # Replay only offers sessions that are actually persisted on disk.
        # A non-empty in-memory LIVE state is not enough to fabricate a replayable date.
        raw=replay_available_sessions(alpaca_data.DATA_DIR,self.symbol)
        ready=READY_STORE.available_sessions(self.symbol, 370)
        return sorted(set(raw)|set(ready), reverse=True)[:370]

    def tables(self) -> Dict[str, Any]:
        with self.lock:
            if self.replay_context.is_replay:
                return self._replay_tables()
            if not self.gamma_delta:
                return {}
            cur = _current_structure_frame(self.gamma_delta)
            if not cur.empty:
                cols = [c for c in ["strike", "signed_gex", "delta_exposure", "open_interest", "option_volume", "gamma_delta_level_score", "state_delta", "state_confidence_delta"] if c in cur.columns]
                cur_score = cur.sort_values("gamma_delta_level_score", ascending=False).head(40)[cols].copy()
                cur_strike = cur.sort_values("strike", ascending=True).head(80)[cols].copy()
            else:
                cur_score = pd.DataFrame(); cur_strike = pd.DataFrame()
            base_audit = [] if self.gamma_delta.get("audit", pd.DataFrame()).empty else _jsonable(self.gamma_delta["audit"].to_dict("records"))
            macro_ok = bool((self.macro or {}).get("series"))
            print_source_ok = self.mode != "LIVE" or load_settings() is not None
            dq=self.data_quality_report or {}; mh=self.model_health or {}; cal=self.calibration or {}
            base_audit += [
                {"check":"DATA QUALITY · cobertura IV", "status":"OK" if float(dq.get("iv_coverage_pct",0) or 0)>=85 else "WARN", "detail":f"{dq.get('iv_coverage_pct','—')}% utilizable · Alpaca {dq.get('provider_iv_count',0)} · fallback ITM QUANT {dq.get('fallback_iv_count',0)}"},
                {"check":"DATA QUALITY · quotes OPRA", "status":"OK" if float(dq.get("quote_coverage_pct",0) or 0)>=75 else "WARN", "detail":f"Cobertura bid/ask {dq.get('quote_coverage_pct','—')}% · calidad spread {dq.get('spread_quality_pct','—')}%"},
                {"check":"DATA QUALITY · frescura", "status":"OK" if dq.get("data_age_seconds") is None or float(dq.get("data_age_seconds",9999))<=180 else "WARN", "detail":f"Edad máxima observada {dq.get('data_age_seconds','—')} s"},
                {"check":"MODEL HEALTH", "status":"OK" if float(mh.get("score",0) or 0)>=85 else "WARN", "detail":f"{mh.get('status','—')} {mh.get('score','—')}/100 · coherencia, no acierto"},
                {"check":"RESEARCH CALIBRATION", "status":"OK" if cal.get("ready") else "WARN", "detail":f"{cal.get('status','COLLECTING')} · {cal.get('sample_size',0)} muestras depuradas · mínimo {cal.get('minimum_recommended',CALIBRATION_MIN_SAMPLES)}"},
                {"check":"0DTE · cobertura utilizable", "status":"OK" if dq.get("zero_dte_usable_pct") is None or float(dq.get("zero_dte_usable_pct",0) or 0)>=70 else "WARN", "detail":f"{dq.get('zero_dte_usable_pct','N/A')}% · IV/Greeks fallback ITM QUANT cuando el proveedor no entrega y existe quote válido"},
                {"check":"Greeks · proveedor vs ITM QUANT", "status":"OK" if float(dq.get("greeks_dislocation_pct",0) or 0)<=20 else "WARN", "detail":f"Dislocación diagnóstica {dq.get('greeks_dislocation_pct','—')}% · fuente propia nunca se oculta"},
                {"check":"OPRA WebSocket LIVE", "status":"OK" if self.mode!="LIVE" or bool(OPTION_STREAM.status().get("connected")) else "WARN", "detail":f"connected={OPTION_STREAM.status().get('connected')} · contratos stream {OPTION_STREAM.status().get('contracts',0)} · REST snapshot sigue como recalibración estructural"},
                {"check":"American model diagnostic", "status":"OK" if (self.american_model_report or {}).get("ready") else "WARN", "detail":f"CRR vs europeo · premium máximo {(self.american_model_report or {}).get('max_american_premium','—')} · diagnóstico, no señal"},
                {"check":"Exposure scenarios", "status":"OK" if (self.exposure_scenarios_report or {}).get("ready") else "WARN", "detail":"Structural GEX + Flow-adjusted proxy + sensibilidad spot ±0.25/0.5/1%"},
                {"check":"SESSION MEMORY", "status":"OK" if self.mode!="LIVE" or (self.session_memory_report or {}).get("ready") else "WARN", "detail":f"{(self.session_memory_report or {}).get('observations',0)} observaciones · {(self.session_memory_report or {}).get('minutes_covered',0)} min cubiertos · research full snapshots en disco"},
                {"check":"Walk-forward readiness", "status":"OK" if (cal.get("walk_forward") or {}).get("ready") else "WARN", "detail":f"{cal.get('walk_forward_readiness','COLLECTING')} · holdout cronológico, sin mezclar sesiones posteriores en train"},
                {"check":"Dealer Intelligence", "status":"OK" if (self.dealer_intelligence_report or {}).get("state") in {"ESTIMATED","COLLECTING"} else "WARN", "detail":f"{(self.dealer_intelligence_report or {}).get('state','WAITING')} · inventario sintético, nunca inventario dealer observado"},
                {"check":"Hedge Pressure", "status":"OK" if ((self.dealer_intelligence_report or {}).get("hedge_pressure") or {}).get("state") in {"ACTIVE","WAITING"} else "WARN", "detail":f"{((self.dealer_intelligence_report or {}).get('hedge_pressure') or {}).get('direction','NEUTRAL')} · escenario dealer-counterparty, no hedge observado"},
                {"check":"Provider data agreement", "status":"WARN" if (self.source_health_report or {}).get("data_disagreement") else "OK", "detail":f"additional source={'ON' if (self.source_health_report or {}).get('secondary_configured') else 'NOT CONFIGURED'} · disagreement={(self.source_health_report or {}).get('spot_disagreement_pct','—')} · no fixed provider rank"},
                {"check":"Futures / direct indexes", "status":"OK" if (self.external_market_report or {}).get("provider_futures_status")=="LIVE" else "WARN", "detail":f"Futures {(self.external_market_report or {}).get('provider_futures_status',(self.external_market_report or {}).get('futures_status','SOURCE PENDING'))} · Index {(self.external_market_report or {}).get('provider_index_status',(self.external_market_report or {}).get('index_status','SOURCE PENDING'))} · sin proxies ocultos"},
                {"check":"Research Store", "status":"OK" if (self.research_storage_report or {}).get("backend") else "WARN", "detail":f"{(self.research_storage_report or {}).get('backend','—')} · {(self.research_storage_report or {}).get('cycles',0)} cycles · {(self.research_storage_report or {}).get('sessions',0)} sessions"},
                {"check":"Persistent Quant Memory", "status":"OK" if str(alpaca_data.DATA_DIR) else "WARN", "detail":f"v{APP_VERSION} · {alpaca_data.DATA_DIR} · NO RESET LIVE/AUDITOR/CALIBRATION/REPLAY"},
                {"check":"Auditor persistente", "status":"OK" if self.mode!="LIVE" or (self.audit_persistence_report or {}).get("ready") else "WARN", "detail":f"{(self.audit_persistence_report or {}).get('status','COLLECTING')} · diario + acumulado · observacional, no altera Scanner"},
                {"check":"Macro Context", "status":"OK" if macro_ok else "WARN", "detail":"FRED/BLS/Fed context loaded" if macro_ok else "Macro public data unavailable; core Gamma/Delta remains independent"},
                {"check":"Large Prints SIP", "status":"OK" if print_source_ok else "WARN", "detail":f"{self.symbol} trades via Alpaca SIP; off-exchange labels require venue/tape evidence"},
                {"check":"Flujo Inusual Pro", "status":"OK", "detail":"Visualiza punto exacto, strip direccional, evento dominante y net flow sin mezclarlo con TRACE"},
                {"check":"Q-Flow normalized SHADOW", "status":"OK" if int(((self.flow_summary or {}).get("normalized_shadow") or {}).get("ready_samples",0) or 0)>0 else "WARN", "detail":f"{((self.flow_summary or {}).get('normalized_shadow') or {}).get('status','SHADOW · COLLECTING')} · legacy sigue LIVE hasta validación OOS"},
                {"check":"Net Drift · Quant Data", "status":"OK", "detail":"Net Drift observado desde buckets Quant Data; el drift estructural nativo permanece separado y no sustituye al proveedor"},
                {"check":"Exposure Strike Pro", "status":"OK", "detail":f"Gamma/Delta/Net OI/Volumen neto por strike calculados desde la cadena {self.symbol}"},
                {"check":"Volumen Pro", "status":"OK", "detail":"Calls vs Puts, Volumen Total y Vol/OI permanecen como módulo independiente; volumen no se interpreta como dirección por sí solo."},
                {"check":"Quant Scanner", "status":"OK" if (self.scanner or {}).get("ready") else "WARN", "detail":"Structural Path Engine sintetiza módulos sin contar dos veces la Superficie 3D; BOUNCE/BREAK/T1/T2 son hipótesis cuantitativas auditables."},
                {"check":"Global Expiry Window", "status":"OK", "detail":f"{(self.expiry_info or {}).get('label','AUTO')} · {(self.expiry_info or {}).get('count',0)} vencimientos · filtro compartido por todos los módulos dependientes de opciones."},
                {"check":"Native options structure", "status":"OK" if build_native_options_structure(self.gamma_delta or {}).get("ready") else "WARN", "detail":"Gamma Flip, majors OI/volumen y GEX estructural se calculan nativamente. Quant Data solo corrobora mediante FEATURE_BUS; Scanner conserva la síntesis final."},
            ]
            for c in (mh.get("checks") or []):
                base_audit.append({"check":f"MODEL · {c.get('name')}","status":"OK" if c.get("ok") else "WARN","detail":str(c.get("detail",""))})
            flow_table=filter_events(self.flow_events,self.expiry_info)
            if not flow_table.empty:
                # Comparison table is the union of top LIVE legacy and normalized SHADOW,
                # so the research model can surface events the legacy score would miss.
                _live=flow_table.sort_values("flow_score",ascending=False).head(40)
                if "flow_score_normalized" in flow_table.columns:
                    _shadow=flow_table.sort_values("flow_score_normalized",ascending=False).head(40)
                    flow_table=pd.concat([_live,_shadow],ignore_index=False).loc[lambda z:~z.index.duplicated(keep="first")].head(70).copy()
                else:
                    flow_table=_live.copy()
                extra=[]
                for _,rr in flow_table.iterrows():
                    try:
                        hi=event_hedge_impact(rr)
                        extra.append({"opening_probability":hi.get("opening_probability"),"opening_label":hi.get("label"),"estimated_hedge_notional":hi.get("estimated_hedge_notional"),"hedge_confidence":hi.get("confidence")})
                    except Exception:
                        extra.append({"opening_probability":None,"opening_label":"UNAVAILABLE","estimated_hedge_notional":None,"hedge_confidence":None})
                for k in extra[0].keys(): flow_table[k]=[z[k] for z in extra]
            return {
                "levels": [] if cur_strike.empty else _jsonable(cur_strike.to_dict("records")),
                "levels_by_strike": [] if cur_strike.empty else _jsonable(cur_strike.to_dict("records")),
                "levels_by_score": [] if cur_score.empty else _jsonable(cur_score.to_dict("records")),
                "flow_events": [] if flow_table.empty else _jsonable(flow_table.to_dict("records")),
                "large_prints": [] if self.large_prints.empty else _jsonable(self.large_prints.sort_values("q_print", ascending=False).head(80).to_dict("records")),
                "macro_events": _jsonable((self.macro or {}).get("events", [])),
                "audit": base_audit,
            }


STATE = PlatformState()

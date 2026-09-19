"""NEXT-GEN terminal payloads.

The browser renderer receives data, not Plotly figures.  This keeps the trading
workspace independent from a charting framework and lets ITM QUANT own zoom,
follow, crosshair, profiles and GPU rendering semantics.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
import math
import numpy as np
import pandas as pd

from .frame_guards import numeric_column
from .obs import note as _obs_note
from .trace_live import build_trace_pulse
from .trace_analytics import structural_walls, dealer_flow_line, max_pain as _max_pain, atm_iv_and_dte
from .expiry_clock import year_fraction
from .greeks_service import greeks_vector as _greeks_vector, model_inputs_vector
from .contract_spec import multiplier_for, multiplier_series
from .causality_engine import unify_market_events
from .field_normalization import robust_z
from .volatility_surface import surface_matrix as svi_surface_matrix
from .accelerated_quant import backend_status as quant_backend_status
from .trace_contract import normalize_nextgen_trace_contract
from .instruments import trace_grid_step
from .time_normalization import utc_ns
from ..version import APP_VERSION
from .expiry_clock import year_fraction_array


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _tf_minutes(tf: str) -> int:
    return {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(str(tf or "1m").lower(), 1)


def _tf_rule(tf: str) -> str:
    return f"{_tf_minutes(tf)}min"


def _window_minutes(value: Any) -> int:
    try:
        x = int(float(value))
    except Exception:
        x = 60
    # 0 is the explicit UI contract for all currently retained ticks.
    if x == 0:
        return 0
    return max(15, min(x, 390))


def _ticks_frame(ticks: Optional[pd.DataFrame]) -> pd.DataFrame:
    if not isinstance(ticks, pd.DataFrame) or ticks.empty:
        return pd.DataFrame(columns=["timestamp", "price", "size", "signed_volume", "seq"])
    x = ticks.copy()
    x["timestamp"] = utc_ns(x.get("timestamp"))
    x["price"] = numeric_column(x,"price",float("nan"))
    # Una fuente de ticks sin `size`/`signed_volume` es un estado válido y debe
    # degradar a cero, no tumbar el payload entero de TRACE.
    x["size"] = numeric_column(x, "size", 0.0)
    x["signed_volume"] = numeric_column(x, "signed_volume", 0.0)
    x = x.dropna(subset=["timestamp", "price"]).sort_values(["timestamp", "seq"] if "seq" in x.columns else ["timestamp"])
    return x


def candles_from_ticks(ticks: Optional[pd.DataFrame], timeframe: str = "1m", tail_minutes: int = 60) -> list[Dict[str, Any]]:
    x = _ticks_frame(ticks)
    if x.empty:
        return []
    window = _window_minutes(tail_minutes)
    if window > 0:
        cutoff = x["timestamp"].max() - pd.Timedelta(minutes=window)
        x = x[x["timestamp"] >= cutoff]
    if x.empty:
        return []
    rule = _tf_rule(timeframe)
    tf_minutes = _tf_minutes(timeframe)
    # One authoritative event-time bucket grid. All OHLCV components use the
    # same origin/closure so a 3m/5m/15m selection cannot visually drift.
    z = x.set_index("timestamp")
    rs = dict(rule=rule, label="left", closed="left", origin="start_day")
    ohlc = z["price"].resample(**rs).ohlc()
    vol = z["size"].resample(**rs).sum().rename("volume")
    signed = z["signed_volume"].resample(**rs).sum().rename("signed_volume")
    n = z["price"].resample(**rs).count().rename("trades")
    last_event_time = pd.Timestamp(x["timestamp"].max())
    bars = pd.concat([ohlc, vol, signed, n], axis=1).dropna(subset=["open", "high", "low", "close"]).reset_index()
    out = []
    for r in bars.to_dict("records"):
        bucket_start = pd.Timestamp(r["timestamp"])
        bucket_end = bucket_start + pd.Timedelta(minutes=tf_minutes)
        out.append({
            "t": bucket_start.isoformat(),
            "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"]),
            "v": float(r.get("volume", 0.0)), "sv": float(r.get("signed_volume", 0.0)), "n": int(r.get("trades", 0) or 0),
            "complete": bool(last_event_time >= bucket_end),
        })
    return out


def _nearest_price_at_events(events: pd.DataFrame, ticks: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return events
    e = events.copy(); e["timestamp"] = utc_ns(e.get("timestamp")); e = e.dropna(subset=["timestamp"]).sort_values("timestamp")
    t = _ticks_frame(ticks)
    if t.empty:
        e["chart_price"] = numeric_column(e,"underlying_price",float("nan"))
        return e
    right = t[["timestamp", "price"]].rename(columns={"price": "chart_price"}).sort_values("timestamp")
    # Causal asof: only the last underlying trade at or before the option print.
    m = pd.merge_asof(e, right, on="timestamp", direction="backward", tolerance=pd.Timedelta(seconds=5))
    fallback = numeric_column(m,"underlying_price",float("nan"))
    m["chart_price"] = numeric_column(m,"chart_price",float("nan")).fillna(fallback)
    return m


def observed_option_prints(events: Optional[pd.DataFrame], ticks: Optional[pd.DataFrame], symbol: str, tail_minutes: int = 60, max_points: int = 500) -> list[Dict[str, Any]]:
    if not isinstance(events, pd.DataFrame) or events.empty:
        return []
    x = events.copy(); x["timestamp"] = utc_ns(x.get("timestamp")); x = x.dropna(subset=["timestamp"])
    if "underlying_symbol" in x.columns:
        x = x[x["underlying_symbol"].astype(str).str.upper() == str(symbol).upper()]
    if x.empty:
        return []
    window = _window_minutes(tail_minutes)
    if window > 0:
        cutoff = x["timestamp"].max() - pd.Timedelta(minutes=window)
        x = x[x["timestamp"] >= cutoff]
    x = _nearest_price_at_events(x, _ticks_frame(ticks))
    for col in ("premium", "contracts", "direction_sign", "strike", "trade_price", "aggressor_confidence"):
        if col in x.columns:
            x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.sort_values(["premium", "timestamp"], ascending=[False, True]).head(int(max_points)).sort_values("timestamp")
    out = []
    for r in x.to_dict("records"):
        p = _f(r.get("chart_price"))
        if p is None:
            continue
        out.append({
            "t": pd.Timestamp(r["timestamp"]).isoformat(), "price": p,
            "strike": _f(r.get("strike")), "contracts": _f(r.get("contracts"), 0.0) or 0.0,
            "premium": _f(r.get("premium"), 0.0) or 0.0, "direction": int(_f(r.get("direction_sign"), 0.0) or 0.0),
            "aggressor": str(r.get("aggressor") or "UNKNOWN"), "confidence": _f(r.get("aggressor_confidence"), 0.0) or 0.0,
            "option_type": str(r.get("option_type") or ""), "trade_price": _f(r.get("trade_price")),
            "source": str(r.get("flow_source") or "OPRA"),
            "package_type": str(r.get("package_type") or ""), "package_id": str(r.get("package_id") or ""),
            "classification_method": str(r.get("classification_method") or ""),
            "nbbo_synced": bool(r.get("nbbo_synced")) if r.get("nbbo_synced") is not None else False,
            "quote_age_ms": _f(r.get("quote_age_ms")), "quote_quality": str(r.get("quote_quality") or "UNKNOWN"),
        })
    return out


def _level(name: str, value: Any, kind: str, *, secondary: Any = None) -> Optional[Dict[str, Any]]:
    x = _f(value)
    if x is None:
        return None
    rec = {"name": name, "price": x, "kind": kind}
    if secondary is not None:
        rec["secondary"] = secondary
    return rec


def structure_levels(gd: Dict[str, Any], scanner: Dict[str, Any], *,
                     curagg: Optional[pd.DataFrame] = None, spot: Any = None) -> list[Dict[str, Any]]:
    z = (scanner or {}).get("zone") or {}
    levels = [
        # SpotGamma naming: the zero-crossing of NetGEX(S) is "Zero Gamma" on their
        # charts. `kind` stays "flip" for backward compatibility with existing
        # frontend lookups (see updateSpatialHud); only the display name changes.
        _level("Zero Gamma", gd.get("gamma_flip"), "flip"),
        _level("Gamma Center", gd.get("gamma_center"), "gamma"),
        _level("Delta Center", gd.get("delta_center"), "delta"),
        _level("Zona Low", z.get("low"), "zone"), _level("Zona High", z.get("high"), "zone"),
        _level("T1", scanner.get("target1"), "target"), _level("T2", scanner.get("target2"), "target"),
        _level("Invalidación", scanner.get("invalidation"), "risk"),
    ]
    # SpotGamma-style structural walls (Call Wall / Put Wall / Vol Trigger), plus ITM's
    # own Hedge Wall proxy. Reuses structural_walls() exactly as already tested in
    # trace_analytics.py -- no new math, only exposed as chart levels.
    sp = _f(spot, _f(gd.get("spot")))
    if isinstance(curagg, pd.DataFrame) and not curagg.empty and sp is not None:
        try:
            walls = structural_walls(curagg, sp, enriched=_latest_chain(gd))
            levels += [
                _level("Call Wall", walls.get("call_wall"), "call_wall"),
                _level("Put Wall", walls.get("put_wall"), "put_wall"),
                _level("Vol Trigger", walls.get("volatility_trigger"), "vol_trigger"),
                # Si el Hedge Wall cae sobre el Call/Put Wall no se publica como nivel
                # aparte: sería la misma línea con dos etiquetas encima.
                _level("Hedge Wall (ITM)", walls.get("hedge_wall"), "hedge_wall") if not walls.get("hedge_wall_coincides_with") else None,
            ]
        except Exception as exc:
            _obs_note("nextgen_terminal:structure_levels_walls", exc, severity="DEGRADED")
    return [x for x in levels if x is not None]


def key_levels_report(gd: Dict[str, Any], scanner: Dict[str, Any], *, curagg: Optional[pd.DataFrame] = None,
                      spot: Any = None, symbol: str = "DIA") -> Dict[str, Any]:
    """SpotGamma-style Key Levels summary: one flat view of the levels that matter.

    Every number here comes from a calculation that already exists and is already
    tested elsewhere (structural_walls, gamma_flip, max_pain, sigma_horizon) --
    this function only assembles them into one report card, it computes nothing new.
    """
    sp = _f(spot, _f(gd.get("spot")))
    out: Dict[str, Any] = {
        "ready": sp is not None, "symbol": str(symbol or "").upper(), "spot": sp,
        "zero_gamma": _f(gd.get("gamma_flip")), "gamma_center": _f(gd.get("gamma_center")),
        "call_wall": None, "put_wall": None, "vol_trigger": None, "hedge_wall": None,
        "max_pain": None, "expected_move": None, "expected_low": None, "expected_high": None,
        "atm_iv_pct": None, "dte_days": None,
    }
    if isinstance(curagg, pd.DataFrame) and not curagg.empty and sp is not None:
        try:
            walls = structural_walls(curagg, sp, enriched=_latest_chain(gd), symbol=str(symbol or "DIA"))
            out.update({"call_wall": walls.get("call_wall"), "put_wall": walls.get("put_wall"),
                        "vol_trigger": walls.get("volatility_trigger"), "hedge_wall": walls.get("hedge_wall")})
        except Exception as exc:
            _obs_note("nextgen_terminal:key_levels_walls", exc, severity="DEGRADED")
    chain = _latest_chain(gd)
    if not chain.empty and sp is not None:
        try:
            out["max_pain"] = _max_pain(chain)
        except Exception as exc:
            _obs_note("nextgen_terminal:key_levels_max_pain", exc, severity="DEGRADED")
        try:
            atm_iv, dte_days = atm_iv_and_dte(chain, sp)
            if atm_iv is not None and dte_days is not None:
                # Same T convention used everywhere options are priced in this engine
                # (year_fraction), not the intraday trading-minutes clock in
                # instruments.sigma_horizon -- those answer a different question
                # ("move in the next N minutes") and must not be mixed here.
                move = sp * (atm_iv / 100.0) * math.sqrt(year_fraction(dte_days))
                if math.isfinite(move):
                    out.update({"atm_iv_pct": atm_iv, "dte_days": dte_days, "expected_move": move,
                                "expected_low": sp - move, "expected_high": sp + move})
        except Exception as exc:
            _obs_note("nextgen_terminal:key_levels_expected_move", exc, severity="DEGRADED")
    out["model_risk"] = ("Max Pain es un estimado de payout por OI, no una predicción. Expected Move es "
                         "una banda ±1σ derivada del IV ATM observado, no un rango garantizado.")
    return out


def hiro_series(option_events: Optional[pd.DataFrame], *, timeframe_minutes: int = 1,
                counterparty_share: float = 0.70) -> Dict[str, Any]:
    """HIRO-equivalent: cumulative estimated dealer hedge pressure through the session.

    Reuses dealer_flow_line() exactly as already tested in trace_analytics.py. Same
    disclosure as the source function: this is ITM's OPRA-flow-derived proxy, not
    observed dealer inventory or observed hedges.
    """
    out_empty = {"ready": False, "points": [], "label": "HIRO (ITM)"}
    if not isinstance(option_events, pd.DataFrame) or option_events.empty:
        return {**out_empty, "reason": "NO_FLOW_EVENTS"}
    try:
        g = dealer_flow_line(option_events, counterparty_share=counterparty_share,
                             resample=f"{max(1, int(timeframe_minutes))}min")
    except Exception as exc:
        return {**out_empty, "reason": f"{type(exc).__name__}: {exc}"[:160]}
    if g is None or g.empty:
        return {**out_empty, "reason": "NO_HEDGE_ROWS"}
    points = [
        {"t": pd.Timestamp(r["timestamp"]).isoformat(),
         "hedge_notional": _f(r.get("hedge_notional"), 0.0) or 0.0,
         "cumulative": _f(r.get("cumulative"), 0.0) or 0.0,
         "events": int(r.get("events") or 0)}
        for r in g.to_dict("records")
    ]
    share_col = g["counterparty_share_assumption_pct"] if "counterparty_share_assumption_pct" in g.columns else None
    return {
        "ready": True, "points": points,
        "counterparty_share_pct": float(share_col.iloc[-1]) if share_col is not None and len(share_col) else round(counterparty_share * 100.0, 1),
        "is_estimate": True, "label": "HIRO (ITM)",
        "model_risk": ("Presión de hedge estimada a partir de flujo OPRA observado (agresor, Delta, "
                      "contratos) bajo un supuesto de counterparty_share explícito. No es inventario "
                      "de dealer observado ni hedges observados."),
    }


def _trace_display_grid_step(symbol: str, spot: Any = None) -> float:
    """Display-only TRACE grid delegated to the instrument metadata layer."""
    return float(trace_grid_step(symbol, _f(spot, 0.0)))


def temporal_heatmap_history(gd: Dict[str, Any], spot: float | None, visual_window: float = 12.0, max_times: int = 120, max_strikes: int = 45) -> Dict[str, Any]:
    """Bounded historical Gamma/Delta field for TRACE + Replay.

    Uses only snapshots already present in ``gd['enriched']``. No API call occurs here.
    Gamma and Delta remain independent raw matrices; the combined matrix is only a
    signed visual classification and never a quantitative addition of unlike fields.
    """
    h = gd.get("enriched") if isinstance(gd, dict) else None
    if not isinstance(h, pd.DataFrame) or h.empty or "timestamp" not in h.columns or "strike" not in h.columns:
        return {"ready": False, "reason": "NO_STRUCTURAL_HISTORY"}
    x=h.copy();x["timestamp"]=pd.to_datetime(x["timestamp"],errors="coerce");x["strike"]=pd.to_numeric(x["strike"],errors="coerce")
    x=x.dropna(subset=["timestamp","strike"])
    if x.empty:return {"ready":False,"reason":"NO_VALID_HISTORY"}
    sp=_f(spot,_f(gd.get("spot")))
    if sp is not None:
        w=max(3.0,float(visual_window or 12.0));x=x[(x["strike"]>=sp-w)&(x["strike"]<=sp+w)]
    # v1.42.4 · La sesión entera en `max_times` columnas, no los últimos `max_times`
    # snapshots. Con cadencia de ~45 s una sesión deja cientos de snapshots: quedarse
    # con la cola dibujaba el heatmap sólo sobre el último tramo y dejaba el resto del
    # gráfico vacío, que es justo lo que se veía en pantalla. Agrupar en cubos cubre
    # toda la historia disponible sin que el payload crezca ni un byte.
    times_all=sorted(x["timestamp"].dropna().unique())
    n_keep=max(10,int(max_times))
    bucket_map=None
    if len(times_all)>n_keep:
        tser=pd.Series(times_all)
        bucket=(np.arange(len(tser))*n_keep)//len(tser)
        # Etiqueta de cada cubo = su último instante, para que el eje siga siendo tiempo real.
        rep=tser.groupby(bucket).max()
        bucket_map=dict(zip(tser,(rep.iloc[b] for b in bucket)))
        times=[pd.Timestamp(t) for t in rep]
    else:
        times=[pd.Timestamp(t) for t in times_all]
    x=x[x["timestamp"].isin(times_all)]
    strikes=sorted(x["strike"].dropna().unique(),key=lambda k:abs(float(k)-(sp or float(k))))[:max(7,int(max_strikes))]
    strikes=sorted(float(k) for k in strikes);x=x[x["strike"].isin(strikes)]
    def matrix(col: str) -> np.ndarray:
        if col not in x.columns:return np.zeros((len(strikes),len(times)),dtype=float)
        y=x.copy();y[col]=pd.to_numeric(y[col],errors="coerce").fillna(0.0)
        grid=y.pivot_table(index="strike",columns="timestamp",values=col,aggfunc="sum",fill_value=0.0)
        grid=grid.reindex(index=strikes,columns=[pd.Timestamp(t) for t in times_all],fill_value=0.0)
        if bucket_map is not None:
            # Dentro de un cubo se promedia, no se suma: la exposición es un estado en
            # un instante, no un flujo acumulable. Sumarla multiplicaría el campo por
            # el número de snapshots que cayeran en el cubo.
            cols=[pd.Timestamp(bucket_map[t]) for t in times_all]
            grid=grid.T.groupby(cols).mean().T
            grid=grid.reindex(index=strikes,columns=times,fill_value=0.0)
        return grid.to_numpy(float)
    gamma=matrix("signed_gex_proxy")
    delta=matrix("option_delta_exposure_info")
    # Net OI / Net Volume preserve option-side sign without mixing unlike fields.
    # Prefer explicit normalized columns when present; otherwise derive CALL-PUT sign.
    def _numeric_col(frame: pd.DataFrame, name: str | None) -> pd.Series:
        """Always return a Series aligned to `frame`.

        BUGFIX v1.27.1: `frame.get(None, 0.0)` returns the scalar 0.0, and a scalar
        has no .fillna(), so the previous one-liner raised AttributeError whenever
        neither candidate column was present. Missing column is a valid state
        (some providers omit volume), so it must degrade to zeros, not crash.
        """
        if name is None or name not in frame.columns:
            return pd.Series(0.0, index=frame.index, dtype=float)
        return pd.to_numeric(frame[name], errors="coerce").fillna(0.0)

    def _side_sign(frame: pd.DataFrame) -> pd.Series:
        typ = frame.get("option_type", frame.get("type", pd.Series("", index=frame.index))).astype(str).str.upper()
        return typ.map(lambda z: 1.0 if z.startswith("C") else -1.0 if z.startswith("P") else 0.0)

    if "net_oi" not in x.columns:
        oi_col = "open_interest" if "open_interest" in x.columns else ("oi" if "oi" in x.columns else None)
        x["net_oi"] = _numeric_col(x, oi_col) * _side_sign(x)
    if "net_volume" not in x.columns:
        vol_col = "volume" if "volume" in x.columns else ("option_volume" if "option_volume" in x.columns else None)
        x["net_volume"] = _numeric_col(x, vol_col) * _side_sign(x)
    net_oi=matrix("net_oi")
    net_volume=matrix("net_volume")
    # Charm remains an independent field.  We convert model Charm into a
    # notional exposure proxy solely for temporal visualization, then normalize
    # it independently so it is never numerically added to Gamma or Delta.
    if "charm_exposure" not in x.columns:
        oi_col = "open_interest" if "open_interest" in x.columns else ("oi" if "oi" in x.columns else None)
        oi_s = _numeric_col(x, oi_col)
        charm_s = _numeric_col(x, "calc_charm" if "calc_charm" in x.columns else ("charm" if "charm" in x.columns else None))
        if "underlying_price" in x.columns:
            spot_s = pd.to_numeric(x["underlying_price"], errors="coerce").fillna(sp or 0.0)
        else:
            spot_s = pd.Series(float(sp or 0.0), index=x.index, dtype=float)
        # El tamaño del contrato se deduce del propio frame (columna declarada o
        # símbolo OCC). Esta función no recibe `symbol` y adivinarlo sería peor.
        # v1.42.4 · Misma unidad canónica que el perfil por strike (CHARM_PER_DAY):
        # USD de delta por DÍA, con el signo call+/put- que usa el resto del motor.
        # Antes aquí era charm POR AÑO y sin signo, mientras el perfil lo publicaba sin
        # escalar por spot: dos secciones, dos escalas, el mismo nombre.
        _call_s = x.get("option_type", pd.Series("call", index=x.index)).astype(str).str.lower().str.startswith("c")
        _sign_s = pd.Series(np.where(_call_s, 1.0, -1.0), index=x.index, dtype=float)
        x["charm_exposure"] = _sign_s * charm_s / 365.0 * oi_s * multiplier_series(x) * spot_s
    charm=matrix("charm_exposure")
    def norm(a: np.ndarray) -> np.ndarray:
        if not a.size:return a
        vals=np.abs(a[np.isfinite(a)])
        scale=float(np.nanpercentile(vals,95)) if len(vals) else 1.0
        if not math.isfinite(scale) or scale<=1e-12:scale=max(float(np.nanmax(vals)) if len(vals) else 0.0,1.0)
        return np.clip(a/scale,-1.0,1.0)
    gn=norm(gamma);dn=norm(delta);cn=norm(charm);oin=norm(net_oi);vn=norm(net_volume)
    # visual-only joint coherence: + means same sign, - means opposite sign; magnitude
    # is the geometric mean of normalized intensities. Gamma/Delta are never added.
    joint=np.sign(gn*dn)*np.sqrt(np.abs(gn*dn))
    return {
        "ready":True,"times":[pd.Timestamp(t).isoformat() for t in times],"strikes":strikes,
        "gamma_m":(gamma/1e6).tolist(),"delta_m":(delta/1e6).tolist(),"charm_m":(charm/1e6).tolist(),
        "gamma_intensity":gn.tolist(),"delta_intensity":dn.tolist(),"charm_intensity":cn.tolist(),"net_oi_intensity":oin.tolist(),"net_volume_intensity":vn.tolist(),"joint_coherence":joint.tolist(),
        "normalization":"SESSION_P95_ABS_PER_FIELD","combined_semantics":"SIGN_COHERENCE_GEOMETRIC_INTENSITY_NOT_RAW_ADDITION",
        "time_coverage":"FULL_SESSION_BUCKETED" if len(times_all)>n_keep else "EVERY_SNAPSHOT",
        "snapshots_available":int(len(times_all)),"columns":int(len(times)),
        "strike_low":float(min(strikes)) if strikes else None,"strike_high":float(max(strikes)) if strikes else None,
        "incremental_key":pd.Timestamp(times[-1]).isoformat() if times else None,
        "authority":"VISUAL_HISTORY_ONLY_SCANNER_REMAINS_AUTHORITY",
    }


def build_nextgen_trace_payload(
    *,
    symbol: str,
    gd: Dict[str, Any],
    scanner: Dict[str, Any],
    market_state: Dict[str, Any],
    ticks: Optional[pd.DataFrame],
    option_events: Optional[pd.DataFrame],
    timeframe: str = "1m",
    tail_minutes: int = 60,
    visual_window: float = 12.0,
    asof: Any = None,
    model_risk: Optional[Dict[str, Any]] = None,
    data_quality: Any = None,
    model_health: Any = None,
    dealer_report: Optional[Dict[str, Any]] = None,
    native_options_structure: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tf = str(timeframe or "1m").lower()
    if tf not in {"1m", "3m", "5m", "15m"}:
        tf = "1m"
    tail = _window_minutes(tail_minutes)
    tx = _ticks_frame(ticks)
    live_spot = _f(tx.iloc[-1]["price"] if not tx.empty else None, _f(gd.get("spot")))
    pulse = build_trace_pulse(gd, symbol, live_spot, option_events, asof=asof, visual_window=float(visual_window))
    causal = unify_market_events(symbol=symbol, price_ticks=tx.tail(3000), option_events=option_events, process_time=asof, max_lateness_ms=350.0)
    payload = {
        "ready": bool(gd), "renderer": "ITM_QUANT_CANVAS_2D", "version": APP_VERSION,
        "symbol": str(symbol).upper(), "timeframe": tf, "timeframe_minutes": _tf_minutes(tf), "bar_interval_ms": _tf_minutes(tf) * 60_000, "tail_minutes": tail,
        "visual": {
            "price_grid_step": _trace_display_grid_step(symbol, live_spot),
            "integer_grid_opacity": 0.075,
            "minor_grid_opacity": 0.032,
            "layout": "TRACE_TRUE_CHART_SCALE_92",
            "flow_dock": True,
            "linked_strike_hover": True,
            "session_bootstrap": True,
            "node_inspector": True,
            "cross_section_multirender": True,
            "layout_compat": "TRACE_FULL_VIEWPORT_STRUCTURED_LANES_92",
            "true_x_scale": True,
            "bar_spacing_px_default": 9,
            "right_offset_bars": 7,
            "x_pan_zoom": True,
            "window_semantics": "DATA_HISTORY_NOT_STRETCH_TO_FIT",
            "auto_price_focus": True,
            "offscreen_level_indicators": True,
            "gravity_nodes": True,
            "label_collision_avoidance": True,
            "structured_lanes": ["GAMMA", "PRICE_FLOW", "DELTA", "STRIKE_LABELS"],
            "smart_tick_skipping": True,
            "timeframe_integrity": True,
            "institutional_visual_system": True,
            "universal_profiles": ["GEX", "DEX", "OI", "NET_OI", "VOLUME", "NET_VOLUME", "OPRA_NET"],
            "value_fields": ["GEX", "DEX", "NET_OI", "NET_VOLUME", "OPRA_NET"],
            "unusual_flow_overlay": "OPRA_OBSERVED_ONLY",
            "theme_modes": ["dark", "light"],
        },
        "candles": candles_from_ticks(tx, tf, tail),
        "candle_integrity": {"source": "SIP_EVENT_TIME", "timeframe": tf, "minutes": _tf_minutes(tf), "bucket_origin": "START_DAY", "synthetic_fill": False},
        "option_prints": observed_option_prints(option_events, tx, symbol, 0, max_points=1200),
        "heatmap_history": temporal_heatmap_history(gd, live_spot, visual_window=float(visual_window)),
        "profiles": pulse,
        "levels": structure_levels(gd, scanner or {}, curagg=gd.get("current"), spot=live_spot),
        "hiro": hiro_series(option_events, timeframe_minutes=_tf_minutes(tf)),
        "key_levels_report": key_levels_report(gd, scanner or {}, curagg=gd.get("current"), spot=live_spot, symbol=symbol),
        "decision": {
            "direction": (scanner or {}).get("direction"), "edge_state": (scanner or {}).get("edge_state"),
            "evidence": (scanner or {}).get("evidence_score"), "zone": (scanner or {}).get("zone"),
            "target1": (scanner or {}).get("target1"), "target2": (scanner or {}).get("target2"),
            "invalidation": (scanner or {}).get("invalidation"),
        },
        "market_state": {
            "phase": (market_state or {}).get("phase"), "stability": (market_state or {}).get("stability"),
            "instability": (market_state or {}).get("instability"), "directional_context": (market_state or {}).get("directional_context"),
            "confluence_index": (market_state or {}).get("confluence_index"), "probability": False,
            "dealer": (market_state or {}).get("dealer") or {},
            "gamma_squeeze": (market_state or {}).get("gamma_squeeze") or {},
            "top_factors": list((market_state or {}).get("top_factors") or [])[:3],
        },
        "quality": {"data_quality": _f(data_quality), "model_health": _f(model_health)},
        "dealer_intelligence": {
            "state": (dealer_report or {}).get("state"), "dealer_field": (dealer_report or {}).get("dealer_field"),
            "confidence": (dealer_report or {}).get("confidence"), "confidence_label": (dealer_report or {}).get("confidence_label"),
            "dealer_state_score": (dealer_report or {}).get("dealer_state_score"),
            "hedge_pressure": (dealer_report or {}).get("hedge_pressure") or {},
            "inventory_shift": (dealer_report or {}).get("inventory_shift") or {},
            "flow_confirmation": (dealer_report or {}).get("flow_confirmation") or {},
            "microstructure_quality": (dealer_report or {}).get("microstructure_quality") or {},
        },
        "native_options_structure": native_options_structure or {
            "ready": False, "status": "UNAVAILABLE", "authority": "ITM_QUANT_NATIVE_OPTIONS_STRUCTURE", "provider_dependency": "NONE"
        },
        "causality": causal.get("status"),
        "provenance": {
            "engine_version": APP_VERSION,
            "structure_asof": pd.Timestamp(_latest_chain(gd)["timestamp"].max()).isoformat() if not _latest_chain(gd).empty else None,
            "structure_source": str((gd or {}).get("source") or "OPTIONS_SNAPSHOT/MODEL"),
            "price_source": "SIP/LIVE_STREAM_OR_REPLAY_TAPE",
            "flow_source": "OPRA_OBSERVED_WHEN_AVAILABLE",
            "direction_authority": "SCANNER_ONLY",
        },
        "model_risk": model_risk or {
            "visible": True,
            "label": "MODELO TEÓRICO · IV/OI ENTRE SNAPSHOTS · NO ES PRECIO PROYECTADO",
            "scenarios": ["IV -Δ", "BASE", "IV +Δ"],
        },
        "disclosure": "ESTRUCTURA=OI/GEX/DEX · DINÁMICA=spot/time repricing · FLUJO REAL=prints OPRA observados. Scanner conserva autoridad direccional.",
    }
    return normalize_nextgen_trace_contract(payload, source="build_nextgen_trace_payload")


def _latest_chain(gd: Dict[str, Any]) -> pd.DataFrame:
    x = gd.get("enriched") if isinstance(gd, dict) else None
    if not isinstance(x, pd.DataFrame) or x.empty:
        return pd.DataFrame()
    x = x.copy(); x["timestamp"] = utc_ns(x.get("timestamp")); x = x.dropna(subset=["timestamp"])
    if x.empty:
        return pd.DataFrame()
    return x[x["timestamp"] == x["timestamp"].max()].copy()


def _exposure_columns(chain: pd.DataFrame, symbol: str) -> pd.DataFrame:
    x = chain.copy()
    for c in ("strike", "dte", "iv", "open_interest", "underlying_price"):
        x[c] = pd.to_numeric(x.get(c), errors="coerce")
    x = x.dropna(subset=["strike", "dte", "iv", "underlying_price"])
    if x.empty:
        return x
    S = x["underlying_price"].to_numpy(float); K = x["strike"].to_numpy(float); dte = np.maximum(x["dte"].to_numpy(float), 0.0)
    iv = np.maximum(x["iv"].to_numpy(float), 1e-6); oi = x["open_interest"].fillna(0).to_numpy(float)
    call = x.get("option_type", pd.Series("call", index=x.index)).astype(str).str.lower().str.startswith("c").to_numpy()
    r, q = model_inputs_vector(symbol, dte); g = _greeks_vector(symbol, S, K, year_fraction_array(dte), iv, call, r, q)
    sign = np.where(call, 1.0, -1.0); mult = multiplier_for(symbol)
    x["gamma_field"] = sign * g["gamma"] * oi * mult * (S ** 2) * 0.01
    x["delta_field"] = g["delta"] * oi * mult * S
    x["vanna_field"] = sign * g["vanna"] * 0.01 * oi * mult * S
    x["speed_field"] = sign * g["speed"] * (0.01 * S) * oi * mult * (S ** 2) * 0.01
    dte10 = np.maximum(dte - 10 / 1440, 0.0); r10, q10 = model_inputs_vector(symbol, dte10)
    g10 = _greeks_vector(symbol, S, K, year_fraction_array(dte10), iv, call, r10, q10)
    x["charm_field"] = sign * (g10["delta"] - g["delta"]) * oi * mult * S
    x["color_field"] = sign * (g10["gamma"] - g["gamma"]) * oi * mult * (S ** 2) * 0.01
    x["gex_field"] = pd.to_numeric(x.get("signed_gex_proxy", x["gamma_field"]), errors="coerce").fillna(x["gamma_field"])
    x["dex_field"] = pd.to_numeric(x.get("option_delta_exposure_info", x["delta_field"]), errors="coerce").fillna(x["delta_field"])
    return x


def build_quant_surface_payload(
    *,
    symbol: str,
    gd: Dict[str, Any],
    dealer: Optional[Dict[str, Any]] = None,
    calibration: Optional[Dict[str, Any]] = None,
    max_expiries: int = 6,
    max_strikes: int = 41,
    scenario_iv_shift: float = 0.0,
    option_view: str = "Net",
) -> Dict[str, Any]:
    chain = _exposure_columns(_latest_chain(gd), symbol)
    if chain.empty:
        return {"ready": False, "reason": "NO_CHAIN", "renderer": "ITM_QUANT_WEBGL"}
    view = str(option_view or "Net").strip().lower()
    if view.startswith("call") or view.startswith("put"):
        if "option_type" not in chain.columns:
            return {"ready": False, "reason": f"{option_view} UNAVAILABLE", "renderer": "ITM_QUANT_WEBGL"}
        want = "c" if view.startswith("call") else "p"
        chain = chain[chain["option_type"].astype(str).str.lower().str.startswith(want)].copy()
        if chain.empty:
            return {"ready": False, "reason": f"NO_{str(option_view).upper()}_CONTRACTS", "renderer": "ITM_QUANT_WEBGL"}
    if "expiration_date" not in chain.columns:
        ts = pd.to_datetime(chain.get("timestamp"), errors="coerce")
        chain["expiration_date"] = (ts + pd.to_timedelta(chain["dte"], unit="D")).dt.date.astype(str)
    chain["expiration_date"] = chain["expiration_date"].astype(str)
    exps = sorted(chain["expiration_date"].dropna().unique().tolist())[:max(1, int(max_expiries))]
    chain = chain[chain["expiration_date"].isin(exps)].copy()
    spot = _f(gd.get("spot"), _f(chain["underlying_price"].iloc[-1])) or 0.0
    strikes = sorted(chain["strike"].dropna().unique().tolist(), key=lambda k: abs(float(k) - spot))[:max(5, int(max_strikes))]
    strikes = sorted(float(k) for k in strikes)
    chain = chain[chain["strike"].isin(strikes)].copy()
    fields = ["gamma_field", "delta_field", "vanna_field", "charm_field", "speed_field", "color_field", "gex_field", "dex_field"]
    pivots: Dict[str, np.ndarray] = {}
    iv_pv = chain.pivot_table(index="strike", columns="expiration_date", values="iv", aggfunc="mean").reindex(index=strikes, columns=exps)
    raw_iv = iv_pv.to_numpy(float)
    try:
        smoothed_iv, iv_source, svi_report = svi_surface_matrix(chain, strikes, exps, spot, scenario_iv_shift=float(scenario_iv_shift))
    except Exception as exc:
        fallback=float(np.nanmedian(raw_iv)) if np.isfinite(raw_iv).any() else .20
        smoothed_iv=np.where(np.isfinite(raw_iv),raw_iv,fallback)
        iv_source=np.full(smoothed_iv.shape,"OBSERVED/FALLBACK",dtype=object)
        svi_report={"ready":False,"reason":f"{type(exc).__name__}: {exc}"[:160]}
    pivots["iv_field"] = smoothed_iv
    for f in fields:
        pv = chain.pivot_table(index="strike", columns="expiration_date", values=f, aggfunc="sum", fill_value=0.0).reindex(index=strikes, columns=exps, fill_value=0.0)
        pivots[f] = pv.to_numpy(float)
    # Observable contract activity fields. Gross OI/volume remain unsigned snapshots;
    # Net fields use CALL positive / PUT negative and therefore remain exact under
    # Calls/Puts/Net selector semantics.
    call_sign = np.where(chain.get("option_type", pd.Series("call", index=chain.index)).astype(str).str.lower().str.startswith("c"), 1.0, -1.0)
    chain["_oi_field"] = numeric_column(chain,"open_interest",0.0)
    chain["_volume_field"] = numeric_column(chain,"volume",0.0)
    chain["_net_oi_field"] = call_sign * chain["_oi_field"]
    chain["_net_volume_field"] = call_sign * chain["_volume_field"]
    chain["_activity_field"] = chain["_volume_field"] / (chain["_oi_field"] + 1.0)
    for src, dst, agg in (("_oi_field","oi_field","sum"),("_volume_field","volume_field","sum"),("_net_oi_field","net_oi_field","sum"),("_net_volume_field","net_volume_field","sum"),("_activity_field","activity_field","max")):
        pv = chain.pivot_table(index="strike", columns="expiration_date", values=src, aggfunc=agg, fill_value=0.0).reindex(index=strikes, columns=exps, fill_value=0.0)
        pivots[dst] = pv.to_numpy(float)

    dealer_pressure = _f(((dealer or {}).get("hedge_pressure") or {}).get("net_15m"), 0.0) or 0.0
    dealer_conf = max(0.0, min(1.0, (_f((dealer or {}).get("confidence"), 0.0) or 0.0) / 100.0))
    local_delta = pivots["dex_field"]
    # Prefer the persistent Synthetic Dealer Inventory surface when available. This is
    # still ESTIMATED inventory, but it is richer than smearing one scalar across DEX.
    hedge = np.zeros_like(local_delta)
    inv_rows=(dealer or {}).get("inventory_surface") or []
    try:
        inv=pd.DataFrame(inv_rows)
        if not inv.empty and {"strike","expiration_date","hedge_to_neutral_notional"}.issubset(inv.columns):
            inv["strike"]=pd.to_numeric(inv["strike"],errors="coerce")
            inv["expiration_date"]=inv["expiration_date"].astype(str)
            inv["hedge_to_neutral_notional"]=pd.to_numeric(inv["hedge_to_neutral_notional"],errors="coerce").fillna(0.0)
            hpv=inv.pivot_table(index="strike",columns="expiration_date",values="hedge_to_neutral_notional",aggfunc="sum",fill_value=0.0).reindex(index=strikes,columns=exps,fill_value=0.0)
            hedge=hpv.to_numpy(float)
    except Exception:
        hedge=np.zeros_like(local_delta)
    if not np.any(np.abs(hedge)>1e-12):
        denom = np.nanmax(np.abs(local_delta)) if local_delta.size else 0.0
        hedge = np.zeros_like(local_delta) if denom <= 0 or abs(dealer_pressure) <= 0 else np.sign(dealer_pressure) * dealer_conf * np.abs(local_delta) / denom

    # Q(K,T) is SHADOW unless an OOS calibration explicitly provides field weights.
    default_w = {"gamma": 0.20, "delta": 0.25, "vanna": 0.12, "charm": 0.12, "hedge": 0.18, "speed": 0.08, "color": 0.05}
    cal_w = ((calibration or {}).get("field_weights_oos") or {}) if isinstance(calibration, dict) else {}
    weights = dict(default_w); source = "SHADOW_PRIOR · NO PRODUCTION AUTHORITY"
    if isinstance(cal_w, dict) and cal_w:
        for k in list(weights):
            if _f(cal_w.get(k)) is not None:
                weights[k] = float(cal_w[k])
        total = sum(abs(v) for v in weights.values()) or 1.0
        weights = {k: v / total for k, v in weights.items()}
        source = "OOS_CALIBRATED"

    def zgrid(a: np.ndarray) -> np.ndarray:
        flat = np.asarray(robust_z(a.flatten()), dtype=float).reshape(a.shape)
        return np.clip(flat, -4.0, 4.0)

    q = (
        weights["gamma"] * zgrid(pivots["gamma_field"]) + weights["delta"] * zgrid(pivots["delta_field"]) +
        weights["vanna"] * zgrid(pivots["vanna_field"]) + weights["charm"] * zgrid(pivots["charm_field"]) +
        weights["speed"] * zgrid(pivots["speed_field"]) + weights["color"] * zgrid(pivots["color_field"]) +
        weights["hedge"] * zgrid(hedge)
    )
    # Visual uncertainty is explicit: sparse/far OTM/longer-dated cells get lower
    # rendering confidence. This drives fog/shader opacity only; it never changes Q.
    oi_pv = chain.pivot_table(index="strike", columns="expiration_date", values="open_interest", aggfunc="sum", fill_value=0.0).reindex(index=strikes, columns=exps, fill_value=0.0).to_numpy(float)
    vol_pv = chain.pivot_table(index="strike", columns="expiration_date", values="volume", aggfunc="sum", fill_value=0.0).reindex(index=strikes, columns=exps, fill_value=0.0).to_numpy(float) if "volume" in chain.columns else np.zeros_like(oi_pv)
    liq = np.log1p(np.maximum(0.0, oi_pv) + 2.0*np.maximum(0.0, vol_pv))
    liq = liq / (np.nanmax(liq) or 1.0)
    dist = np.array([abs(float(k)-spot) for k in strikes], dtype=float)[:,None]
    dscale = np.nanpercentile(dist, 75) if dist.size else 1.0
    dscale = max(float(dscale or 1.0), 1e-9)
    moneyness_conf = np.exp(-0.55*dist/dscale)
    expiry_conf = np.linspace(1.0, 0.78, max(1,len(exps)))[None,:]
    confidence = np.clip(0.18 + 0.58*liq + 0.24*moneyness_conf*expiry_conf, 0.08, 1.0)
    all_fields = {
        "IV": pivots["iv_field"], "IV Observed": np.where(np.isfinite(raw_iv), raw_iv, 0.0), "Gamma": pivots["gamma_field"], "Delta": pivots["delta_field"], "Vanna": pivots["vanna_field"],
        "Charm": pivots["charm_field"], "Speed": pivots["speed_field"], "Color": pivots["color_field"],
        "GEX": pivots["gex_field"], "DEX": pivots["dex_field"], "Open Interest": pivots["oi_field"], "Net OI": pivots["net_oi_field"],
        "Volumen": pivots["volume_field"], "Volumen Neto": pivots["net_volume_field"], "Actividad inusual": pivots["activity_field"],
        "Hedge": hedge, "Q": q,
    }
    return {
        "ready": True, "renderer": "ITM_QUANT_WEBGL", "version": APP_VERSION, "symbol": str(symbol).upper(), "spot": spot, "option_view": str(option_view or "Net"),
        "x_expirations": exps, "y_strikes": strikes,
        "fields": {k: [[float(v) for v in row] for row in a] for k, a in all_fields.items()},
        "confidence": [[float(v) for v in row] for row in confidence],
        "weights": weights, "weights_source": source,
        "q_is_probability": False, "q_production_authority": False, "q_weights_validated_oos": source == "OOS_CALIBRATED",
        "volatility_surface": {"model":"SVI","report":svi_report,"source_grid":[[str(v) for v in row] for row in iv_source],"observed_iv_preserved":True},
        "quant_acceleration": quant_backend_status(),
        "scenario_iv_shift": float(scenario_iv_shift),
        "model_risk": {
            "visible": True,
            "label": "SUPERFICIE TEÓRICA/ESTIMADA · NO INVENTARIO DEALER OBSERVADO",
            "assumptions": ["Black-Scholes local sensitivities", "Observed IV preserved in base surface; SVI fills gaps and powers explicit stress scenarios", f"IV scenario shift {float(scenario_iv_shift):+.4f}", "OI snapshot", "dealer hedge field estimated when available"],
        },
    }

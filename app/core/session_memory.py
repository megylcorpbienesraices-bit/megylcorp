from __future__ import annotations
from .atomic_store import append_row
from .frame_guards import numeric_column
from .obs import note as _obs_note

from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from app.persistence import routed_dir

EC = ZoneInfo("America/Guayaquil")


def _path(storage: Path, symbol: str, day: str | None = None) -> Path:
    day = day or datetime.now(EC).date().isoformat()
    return routed_dir(Path(storage), "sessions") / f"session_metrics_{str(symbol).lower()}_{day}.csv"


def append_session_metric(storage: Path, symbol: str, gd: Dict[str, Any], scanner: Dict[str, Any], expiry_mode: str,
                          vol: Dict[str, Any] | None = None) -> None:
    """Persist one lightweight structural row per refresh.

    Full option-chain snapshots remain the research store. This file is the cheap
    session-memory layer used for long intraday migrations without holding a full
    chain for the entire day in RAM.
    """
    if not gd:
        return
    enr = gd.get("enriched", pd.DataFrame()) if isinstance(gd, dict) else pd.DataFrame()
    latest_chain = pd.DataFrame()
    call_delta = put_delta = call_gex = put_gex = float("nan")
    if isinstance(enr, pd.DataFrame) and not enr.empty and "timestamp" in enr.columns:
        e = enr.copy(); e["timestamp"] = pd.to_datetime(e["timestamp"], errors="coerce"); e = e.dropna(subset=["timestamp"])
        if not e.empty:
            e = e[e["timestamp"] == e["timestamp"].max()].copy()
            latest_chain = e.copy()
            is_call = e.get("option_type", pd.Series("call", index=e.index)).astype(str).str.lower().str.startswith("c")
            dex = numeric_column(e,"option_delta_exposure_info",0.0)
            gex = numeric_column(e,"signed_gex_proxy",0.0)
            call_delta = float(dex[is_call].sum()); put_delta = float(dex[~is_call].sum())
            call_gex = float(gex[is_call].sum()); put_gex = float(gex[~is_call].sum())
    try:
        from .trace_analytics import max_pain as _max_pain
        max_pain_value = _max_pain(latest_chain) if not latest_chain.empty else None
    except Exception:
        max_pain_value = None
    row = {
        "timestamp": datetime.now(EC).replace(tzinfo=None).isoformat(),
        "symbol": str(symbol).upper(),
        "expiry_mode": str(expiry_mode),
        "spot": gd.get("spot"),
        "gamma_center": gd.get("gamma_center"),
        "delta_center": gd.get("delta_center"),
        "gamma_flip": gd.get("gamma_flip"),
        "max_pain": max_pain_value,
        "net_gex": gd.get("total_signed_gex"),
        "net_delta": gd.get("net_delta_exposure"),
        "call_delta": call_delta,
        "put_delta": put_delta,
        "call_gex": call_gex,
        "put_gex": put_gex,
        "direction": (scanner or {}).get("direction"),
        "edge_state": (scanner or {}).get("edge_state"),
        "evidence_score": (scanner or {}).get("evidence_score"),
        "regime": (scanner or {}).get("regime") or (scanner or {}).get("regime_context", {}).get("regime"),
        # La volatilidad observada se guarda con la estructura para que el IV Rank
        # pueda calcularse con historia propia. Sin esto, ese número dependía por
        # completo de un proveedor externo y la pantalla quedaba en blanco si faltaba.
        "atm_iv": (vol or {}).get("atm_iv"),
        "skew_25d": (vol or {}).get("skew_25d"),
        "realized_vol_pct": (vol or {}).get("realized_volatility_pct"),
    }
    p = _path(storage, symbol); p.parent.mkdir(parents=True, exist_ok=True)
    # Mismo motivo que en scanner_history: el esquema de la memoria de sesión crece
    # con la versión (atm_iv, skew_25d, realized_vol_pct se añadieron en v1.41.8),
    # y un append a ciegas deja filas incoherentes con la cabecera antigua.
    append_row(p, row)



def session_metric_frame(storage: Path, symbol: str, expiry_mode: str) -> pd.DataFrame:
    """Return the compact full-session structural timeline used by visual continuity.

    This is presentation/history support only.  It never feeds Scanner or any trading
    calculation.  Older rows may lack call/put decomposition; those fields stay NaN
    rather than being invented, while net Delta/GEX and spot remain continuous.
    """
    p = _path(storage, symbol)
    if not p.exists():
        return pd.DataFrame()
    try:
        x = pd.read_csv(p)
        x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce")
        x = x.dropna(subset=["timestamp"])
        if "expiry_mode" in x.columns:
            z = x[x["expiry_mode"].astype(str) == str(expiry_mode)]
            if not z.empty: x = z
        for c in ["spot","net_gex","net_delta","call_delta","put_delta","call_gex","put_gex",
                  "max_pain","atm_iv","skew_25d","realized_vol_pct"]:
            if c not in x.columns: x[c] = np.nan
            x[c] = pd.to_numeric(x[c], errors="coerce")
        return x.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


# Mínimo de observaciones para que un percentil signifique algo. Publicar un rank
# con cuatro lecturas es afirmar más de lo que el dato sostiene.
IV_RANK_MIN_SAMPLES = 30


def iv_rank_native(storage: Path, symbol: str, expiry_mode: str, current_iv: Any,
                   *, min_samples: int = IV_RANK_MIN_SAMPLES) -> Dict[str, Any]:
    """IV Rank e IV Percentile calculados con la historia que el motor ya observó.

    Dos números distintos que se confunden a menudo:

    * **IV Rank**  = (IV − mín) / (máx − mín). Dónde está la IV dentro de su rango
      observado. Dos valores extremos la dominan.
    * **IV Percentile** = proporción de observaciones por debajo de la IV actual. No
      lo mueve un pico aislado, así que describe mejor una distribución sesgada.

    Se publican los dos porque responden a preguntas distintas, y ninguno se publica
    con menos de ``min_samples`` observaciones: un percentil sobre cuatro lecturas no
    es un percentil, es una coincidencia.
    """
    try:
        iv = float(current_iv)
    except (TypeError, ValueError):
        return {"ready": False, "reason": "SIN IV ATM ACTUAL", "source": "ITM_QUANT"}
    if not np.isfinite(iv) or iv <= 0:
        return {"ready": False, "reason": "SIN IV ATM ACTUAL", "source": "ITM_QUANT"}

    df = session_metric_frame(storage, symbol, expiry_mode)
    if df.empty or "atm_iv" not in df.columns:
        return {"ready": False, "reason": "SIN HISTORIA DE IV OBSERVADA", "samples": 0, "source": "ITM_QUANT"}
    hist = pd.to_numeric(df["atm_iv"], errors="coerce").dropna()
    hist = hist[(hist > 0) & np.isfinite(hist)]
    n = int(len(hist))
    if n < int(min_samples):
        return {"ready": False, "reason": f"HISTORIA INSUFICIENTE · {n}/{int(min_samples)} observaciones",
                "samples": n, "source": "ITM_QUANT"}

    lo, hi = float(hist.min()), float(hist.max())
    span = hi - lo
    # v1.51.0 · Con un rango observado DEGENERADO —todas las lecturas iguales— ni el
    # rank ni el percentil dicen nada. El rank ya se retenia; el percentil no, y
    # salia 100% porque `hist <= iv` es cierto para todas. En pantalla eso se leia
    # como «la IV nunca ha estado mas alta» junto a una amplitud de 0.00 pp, que es
    # lo contrario: la IV no ha estado en ningun otro sitio. Se retienen los dos.
    degenerate = span <= 1e-9
    rank = None if degenerate else float(np.clip((iv - lo) / span * 100.0, 0.0, 100.0))
    pct = None if degenerate else round(float((hist <= iv).mean() * 100.0), 2)
    minutes = 0.0
    if "timestamp" in df.columns and len(df) > 1:
        minutes = max(0.0, (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).total_seconds() / 60.0)
    return {
        "ready": True, "source": "ITM_QUANT",
        "iv": iv, "rank": rank, "percentile": pct,
        "degenerate_window": bool(degenerate),
        "degenerate_reason": ("RANGO OBSERVADO SIN AMPLITUD · "
                              f"{n} lecturas idénticas") if degenerate else None,
        "low": lo, "high": hi, "median": float(hist.median()), "samples": n,
        "window_minutes": round(minutes, 1),
        "note": ("Rango y percentil sobre la IV ATM que este motor observó para el mismo "
                 "instrumento y ventana de vencimiento. No es un rank de 52 semanas: la "
                 "ventana es la que el programa lleva midiendo."),
    }


def max_pain_timeline_native(storage: Path, symbol: str, expiry_mode: str, *, max_points: int = 240) -> list[Dict[str, Any]]:
    """Historia intradía de Max Pain con autoridad ITM QUANT.

    Primero usa la memoria estructural ligera (barata). Al actualizar desde versiones
    antiguas, esas filas todavía no tienen ``max_pain``; en ese caso reconstruye UNA
    vez la sesión reciente desde los snapshots completos de ``option_chain`` que el
    propio motor ya archivó. Quant Data no es requisito para este gráfico.
    """
    limit = max(2, int(max_points))
    metric = session_metric_frame(storage, symbol, expiry_mode)
    points: list[Dict[str, Any]] = []
    if not metric.empty and "max_pain" in metric.columns:
        z = metric[["timestamp", "max_pain"]].copy()
        z["max_pain"] = pd.to_numeric(z["max_pain"], errors="coerce")
        z = z.dropna(subset=["timestamp", "max_pain"]).tail(limit)
        points = [{"t": pd.Timestamp(r.timestamp).isoformat(), "value": float(r.max_pain)} for r in z.itertuples()]
    if len(points) >= 2:
        return points

    # Compatibilidad inmediata con una sesión iniciada antes del hotfix: deriva el
    # historial desde snapshots causales ya guardados, sin consultar otro proveedor.
    try:
        from .historical_session_store import HistoricalSessionStore
        from .expiry_window import apply_expiry_window
        from .trace_analytics import max_pain as _max_pain
        store = HistoricalSessionStore(Path(storage))
        chain = pd.DataFrame()
        for day in store.available_sessions(symbol)[:3]:
            candidate = store.load_frame(symbol, day, "option_chain")
            if not candidate.empty:
                chain = candidate
                break
        if not chain.empty and "timestamp" in chain.columns:
            chain = chain.copy()
            chain["timestamp"] = pd.to_datetime(chain["timestamp"], errors="coerce")
            chain = chain.dropna(subset=["timestamp"])
            stamps = sorted(chain["timestamp"].drop_duplicates().tolist())[-limit:]
            if stamps:
                wanted = set(stamps)
                chain = chain[chain["timestamp"].isin(wanted)]
                rebuilt: list[Dict[str, Any]] = []
                for ts, group in chain.groupby("timestamp", sort=True):
                    selected, _info = apply_expiry_window(group.copy(), expiry_mode)
                    mp = _max_pain(selected)
                    if mp is not None and np.isfinite(float(mp)):
                        rebuilt.append({"t": pd.Timestamp(ts).isoformat(), "value": float(mp)})
                if rebuilt:
                    # Las filas nuevas de Session Memory prevalecen para timestamps
                    # idénticos; las antiguas se completan desde Research Storage.
                    merged = {str(r["t"]): r for r in rebuilt}
                    for r in points:
                        merged[str(r["t"])] = r
                    points = sorted(merged.values(), key=lambda r: str(r["t"]))[-limit:]
    except Exception as exc:
        _obs_note("session_memory:max_pain_timeline", exc, severity="DEGRADED")
    return points


def _run_minutes(df: pd.DataFrame, col: str) -> Dict[str, Any]:
    if col not in df.columns or "timestamp" not in df.columns:
        return {"direction": "—", "minutes": 0.0, "from": None, "to": None}
    x = df[["timestamp", col]].copy(); x[col] = pd.to_numeric(x[col], errors="coerce"); x = x.dropna()
    if len(x) < 3:
        return {"direction": "—", "minutes": 0.0, "from": None, "to": None}
    d = x[col].diff(); eps = max(float(x[col].std(ddof=0) or 0) * 0.03, 1e-8)
    signs = np.sign(d.where(d.abs() > eps, 0.0).to_numpy())
    nz = np.flatnonzero(signs != 0)
    if not len(nz):
        return {"direction": "STABLE", "minutes": float((x["timestamp"].iloc[-1]-x["timestamp"].iloc[0]).total_seconds()/60), "from": float(x[col].iloc[0]), "to": float(x[col].iloc[-1])}
    last_sign = signs[nz[-1]]; start = nz[-1]
    disagreements = 0
    for i in range(nz[-1]-1, 0, -1):
        if signs[i] == 0:
            continue
        if signs[i] != last_sign:
            disagreements += 1
            if disagreements >= 2:
                break
        else:
            start = i
    t0 = x["timestamp"].iloc[max(0, start-1)]; t1 = x["timestamp"].iloc[-1]
    return {"direction": "UP" if last_sign > 0 else "DOWN", "minutes": round(max(0.0,(t1-t0).total_seconds()/60),1), "from": float(x[col].iloc[max(0,start-1)]), "to": float(x[col].iloc[-1])}


def session_memory_summary(storage: Path, symbol: str, expiry_mode: str) -> Dict[str, Any]:
    p = _path(storage, symbol)
    if not p.exists():
        return {"ready": False, "observations": 0, "note": "Session Memory se inicia al acumular ciclos LIVE."}
    try:
        x = pd.read_csv(p); x["timestamp"] = pd.to_datetime(x["timestamp"], errors="coerce"); x = x.dropna(subset=["timestamp"])
        if "expiry_mode" in x.columns:
            z = x[x["expiry_mode"].astype(str) == str(expiry_mode)]
            if len(z): x = z
        x = x.sort_values("timestamp")
        if x.empty: return {"ready": False, "observations": 0}
        mins = max(0.0, (x["timestamp"].iloc[-1]-x["timestamp"].iloc[0]).total_seconds()/60)
        max_pain_series = max_pain_timeline_native(storage, symbol, expiry_mode)
        return {
            "ready": True, "observations": int(len(x)), "minutes_covered": round(float(mins),1),
            "max_pain_over_time": max_pain_series,
            "max_pain_source": "ITM_QUANT_CHAIN_HISTORY",
            "gamma_center_run": _run_minutes(x, "gamma_center"),
            "delta_center_run": _run_minutes(x, "delta_center"),
            "flip_run": _run_minutes(x, "gamma_flip"),
            "latest_edge": str(x.get("edge_state", pd.Series([""])).iloc[-1]) if "edge_state" in x else None,
            "latest_direction": str(x.get("direction", pd.Series([""])).iloc[-1]) if "direction" in x else None,
            "note": "SESSION MEMORY usa un resumen estructural ligero de toda la sesión. RESEARCH STORAGE conserva los snapshots completos en CSV.",
        }
    except Exception as exc:
        return {"ready": False, "observations": 0, "reason": str(exc)}


def structural_flow_frame(storage: Path, symbol: str, expiry_mode: str, *, max_rows: int = 720, current_gd: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Derive a causal, symbol-scoped *structural* flow timeline from saved snapshots.

    GEX/DEX flow here means the rate of change of ITM QUANT structural exposure between
    consecutive observed refresh snapshots.  It is not exchange order flow and never
    feeds Scanner.  The bounded convexity pressure is a presentation/context model built
    from robust-normalized exposure changes; it is not probability or dealer identity.
    """
    x = session_metric_frame(storage, symbol, expiry_mode)
    base = {
        "ready": False,
        "symbol": str(symbol).upper(),
        "authority": "PRESENTATION_CONTEXT_ONLY",
        "source": "ITM_SESSION_MEMORY",
        "gex_definition": "d(Net GEX)/dt from observed structural snapshots",
        "dex_definition": "d(Net Delta Exposure)/dt from observed structural snapshots",
        "convexity_definition": "bounded ITM model from robust-normalized structural flow; not probability",
        "observations": 0,
        "series": [],
        "current_gex": None,
        "current_dex": None,
    }
    if isinstance(current_gd, dict):
        def _finite_scalar(value):
            try:
                out = float(value)
            except (TypeError, ValueError):
                return None
            return out if np.isfinite(out) else None

        base["current_gex"] = _finite_scalar(current_gd.get("total_signed_gex"))
        base["current_dex"] = _finite_scalar(current_gd.get("net_delta_exposure"))
        # Fail-soft presentation fallback: some refresh paths publish the current
        # strike frame before the pre-aggregated totals. Never show a false dash when
        # the exposure rows themselves are already present. This does not feed Scanner.
        if base["current_gex"] is None or base["current_dex"] is None:
            _frame = pd.DataFrame()
            for _key in ("current_delta", "current"):
                _candidate = current_gd.get(_key)
                if isinstance(_candidate, pd.DataFrame) and not _candidate.empty:
                    _frame = _candidate
                    break
            if not _frame.empty:
                if base["current_gex"] is None and "signed_gex" in _frame.columns:
                    _g = float(pd.to_numeric(_frame["signed_gex"], errors="coerce").fillna(0.0).sum())
                    base["current_gex"] = _g if np.isfinite(_g) else None
                if base["current_dex"] is None and "delta_exposure" in _frame.columns:
                    _d = float(pd.to_numeric(_frame["delta_exposure"], errors="coerce").fillna(0.0).sum())
                    base["current_dex"] = _d if np.isfinite(_d) else None
    if x.empty or len(x) < 2:
        base["status"] = "CURRENT_STRUCTURE_ONLY" if base.get("current_gex") is not None or base.get("current_dex") is not None else "WAITING_FOR_2_SNAPSHOTS"
        base["ready"] = base["status"] == "CURRENT_STRUCTURE_ONLY"
        return base
    z = x[[c for c in ["timestamp", "spot", "net_gex", "net_delta", "gamma_center", "delta_center"] if c in x.columns]].copy()
    z["timestamp"] = pd.to_datetime(z["timestamp"], errors="coerce")
    z = z.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    for c in ["spot", "net_gex", "net_delta", "gamma_center", "delta_center"]:
        if c not in z.columns:
            z[c] = np.nan
        z[c] = pd.to_numeric(z[c], errors="coerce")
    dt_min = z["timestamp"].diff().dt.total_seconds().div(60.0)
    dt_min = dt_min.where((dt_min > 0.02) & (dt_min <= 120.0))
    z["gex_flow"] = z["net_gex"].diff().div(dt_min)
    z["dex_flow"] = z["net_delta"].diff().div(dt_min)
    z["gamma_center_velocity"] = z["gamma_center"].diff().div(dt_min)
    z["delta_center_velocity"] = z["delta_center"].diff().div(dt_min)

    def robust_unit(s: pd.Series) -> pd.Series:
        v = pd.to_numeric(s, errors="coerce")
        finite = v.replace([np.inf, -np.inf], np.nan).dropna()
        if finite.empty:
            return pd.Series(np.nan, index=v.index)
        med = float(finite.median())
        mad = float((finite - med).abs().median())
        scale = max(mad * 1.4826, float(finite.abs().median()) * 0.25, 1e-9)
        return np.tanh((v - med) / (3.0 * scale))

    gu = robust_unit(z["gex_flow"])
    du = robust_unit(z["dex_flow"])
    # Gamma-change dominates the curvature proxy; Delta-change provides directional
    # context.  This is deliberately a bounded model score, never a price forecast.
    z["convexity_pressure"] = (0.75 * gu + 0.25 * du).clip(-1.0, 1.0)
    z = z.tail(max(2, int(max_rows)))
    rows = []
    for r in z.to_dict("records"):
        def f(key):
            try:
                v = float(r.get(key))
                return v if np.isfinite(v) else None
            except Exception:
                return None
        rows.append({
            "timestamp": pd.Timestamp(r["timestamp"]).isoformat(),
            "spot": f("spot"),
            "gex_flow": f("gex_flow"),
            "dex_flow": f("dex_flow"),
            "convexity_pressure": f("convexity_pressure"),
            "gamma_center_velocity": f("gamma_center_velocity"),
            "delta_center_velocity": f("delta_center_velocity"),
        })
    latest = next((r for r in reversed(rows) if r.get("gex_flow") is not None or r.get("dex_flow") is not None), None)
    base.update({
        "ready": latest is not None,
        "status": "LIVE_STRUCTURAL_HISTORY" if latest is not None else "WAITING_FOR_CHANGE",
        "observations": len(rows),
        "latest": latest or {},
        "series": rows,
        "unit": "exposure-change per minute",
    })
    return base

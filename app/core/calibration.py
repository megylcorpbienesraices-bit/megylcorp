from __future__ import annotations

import math
import os
import csv
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable

import json

import numpy as np
import pandas as pd
from app.persistence import routed_dir
from .frame_guards import numeric_column
from .obs import note as _obs_note, get_logger
from .expiry_clock import year_fraction
from .safe_stats import correlation as safe_correlation
from .atomic_store import atomic_write_bytes


def _scanner_files(storage: Path, symbol: str) -> list[Path]:
    return sorted(routed_dir(Path(storage), "scanner_history").glob(f"scanner_history_{str(symbol).lower()}_*.csv"))


# v1.27.22 · Calibration CSV recovery is fail-visible, not fail-open.
# A malformed historical row is quarantined with provenance while the remaining
# syntactically valid rows stay available for research calibration. Desde H3,
# tras guardar una copia forense completa y las filas dañadas en cuarentena, la
# fuente se reescribe atómicamente con sólo las filas válidas para que el mismo
# daño no vuelva a diagnosticarse en cada arranque.
_CALIBRATION_RECOVERY_CACHE: dict[str, tuple[int, int, pd.DataFrame, dict[str, Any]]] = {}
_log = get_logger('calibration')


def _read_scanner_history_csv(path: Path, storage: Path, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    path=Path(path)
    try:
        return pd.read_csv(path), {"recovered":False,"quarantined_rows":0,"source":str(path)}
    except pd.errors.ParserError as exc:
        stat=path.stat()
        cache_key=str(path.resolve())
        cached=_CALIBRATION_RECOVERY_CACHE.get(cache_key)
        if cached and cached[0]==int(stat.st_mtime_ns) and cached[1]==int(stat.st_size):
            return cached[2].copy(), dict(cached[3])

        raw=path.read_bytes()
        digest=hashlib.sha256(raw).hexdigest()
        header: list[str]=[]; good: list[list[str]]=[]; bad: list[dict[str, Any]]=[]
        with path.open("r",encoding="utf-8-sig",errors="replace",newline="") as fh:
            reader=csv.reader(fh)
            try: header=next(reader)
            except StopIteration: header=[]
            expected=len(header)
            for line_no,row in enumerate(reader,start=2):
                if not row or all(not str(v).strip() for v in row):
                    continue
                if len(row)!=expected:
                    bad.append({"line":line_no,"expected_fields":expected,"actual_fields":len(row),"fields":row})
                else:
                    good.append(row)
        if not header:
            raise exc

        qdir=routed_dir(Path(storage),"quarantine")
        qpath=qdir/f"calibration_csv_{path.stem}_{digest[:16]}.jsonl"
        if bad and not qpath.exists():
            tmp=qpath.with_suffix(qpath.suffix+".tmp")
            with tmp.open("w",encoding="utf-8") as out:
                for row in bad:
                    payload={
                        "kind":"MALFORMED_CALIBRATION_CSV_ROW",
                        "source":str(path),
                        "source_sha256":digest,
                        "quarantined_at":datetime.now(timezone.utc).isoformat(),
                        **row,
                    }
                    out.write(json.dumps(payload,ensure_ascii=False)+"\n")
            tmp.replace(qpath)

        frame=pd.DataFrame(good,columns=header)
        forensic_copy = None
        source_repaired = False
        if bad:
            # Primero preservamos byte por byte la fuente original. Sólo después de
            # que esa copia y el JSONL de cuarentena existen, reemplazamos el CSV
            # activo por una versión limpia mediante temp → fsync → rename.
            forensic_copy = qdir / f"calibration_csv_{path.stem}_{digest[:16]}.source.csv"
            if not forensic_copy.exists():
                atomic_write_bytes(forensic_copy, raw)
            clean = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
            atomic_write_bytes(path, clean)
            source_repaired = True

        meta={
            "recovered":True,
            "quarantined_rows":len(bad),
            "valid_rows":len(frame),
            "source":str(path),
            "source_sha256":digest,
            "quarantine_file":str(qpath) if bad else None,
            "forensic_source_copy":str(forensic_copy) if forensic_copy else None,
            "source_repaired":source_repaired,
            "parser_error":str(exc)[:240],
        }
        # No cacheamos con el stat anterior cuando ya reparamos el archivo: la
        # próxima lectura debe entrar por pd.read_csv normal y confirmar que quedó sano.
        if not source_repaired:
            _CALIBRATION_RECOVERY_CACHE[cache_key]=(int(stat.st_mtime_ns),int(stat.st_size),frame.copy(),dict(meta))
        _log.info("cuarentena CSV aplicada y fuente saneada: %s · %d fila(s) malformada(s) aisladas · %d fila(s) válidas preservadas",
                  path.name, len(bad), len(frame))
        return frame,meta


def _market_file(storage: Path, symbol: str, day: str) -> Path:
    return routed_dir(Path(storage), "sessions") / f"alpaca_{str(symbol).lower()}_history_{day}.csv"


def _price_path(path: Path) -> pd.DataFrame:
    if not path.exists():return pd.DataFrame(columns=["timestamp","price"])
    try:
        h=pd.read_csv(path,usecols=lambda c:c in {"timestamp","underlying_price"});h["timestamp"]=pd.to_datetime(h["timestamp"],errors="coerce");h["price"]=pd.to_numeric(h["underlying_price"],errors="coerce")
        return h.dropna(subset=["timestamp","price"])[["timestamp","price"]].drop_duplicates("timestamp").sort_values("timestamp")
    except Exception:return pd.DataFrame(columns=["timestamp","price"])


def _price_path_for_session(storage: Path, symbol: str, day: str) -> tuple[pd.DataFrame, str]:
    """Prefer archived SIP tape; fall back explicitly to structural snapshots.

    Calibration is allowed to inspect the future path *after* a historical signal, but it
    must use an observed path source and disclose which one.  The tape gives higher
    temporal resolution for T1/invalidation and option repricing.
    """
    try:
        from .replay import load_tape
        d=pd.Timestamp(day).date(); t=load_tape(storage,symbol,d)
        if isinstance(t,pd.DataFrame) and not t.empty and {"timestamp","price"}.issubset(t.columns):
            px=t[["timestamp","price"]].copy();px["timestamp"]=pd.to_datetime(px["timestamp"],errors="coerce");px["price"]=pd.to_numeric(px["price"],errors="coerce")
            px=px.dropna(subset=["timestamp","price"]).sort_values("timestamp")
            if len(px): return px,"TAPE_ARCHIVE"
    except Exception as _e:
        _obs_note('calibration:46', _e)
    return _price_path(_market_file(storage,symbol,day)),"STRUCTURAL_SNAPSHOTS_FALLBACK"


def _dedupe_signals(df: pd.DataFrame, min_seconds: int=60) -> pd.DataFrame:
    if df.empty:return df
    x=df.sort_values("timestamp").copy();keep=[];last=None;last_key=None
    for i,r in x.iterrows():
        key=(str(r.get("direction","")),round(float(r.get("zone_center",r.get("spot",0)) or 0),2),str(r.get("state","")),str(r.get("edge_state","")),str(r.get("expiry_mode","")))
        ts=r["timestamp"]
        if last is None or key!=last_key or (ts-last).total_seconds()>=min_seconds:keep.append(i);last=ts;last_key=key
    return x.loc[keep].reset_index(drop=True)


def _eval_signal(r: pd.Series, px: pd.DataFrame, horizon_min: int) -> Dict[str, Any] | None:
    ts=r["timestamp"];end=ts+timedelta(minutes=int(horizon_min));future=px[(px["timestamp"]>=ts)&(px["timestamp"]<=end)]
    if future.empty:return None
    spot=float(r.get("spot",np.nan));direction=1 if str(r.get("direction","" )).upper()=="BUY" else -1 if str(r.get("direction","" )).upper()=="SELL" else 0
    if direction==0 or not math.isfinite(spot):return None
    values=future["price"].to_numpy(float);times=future["timestamp"].tolist();fav=(values-spot)*direction;adv=-(values-spot)*direction;mfe=float(np.nanmax(fav));mae=float(np.nanmax(adv))
    t1=float(r.get("target1",np.nan));t2=float(r.get("target2",np.nan));inv=float(r.get("invalidation",np.nan))
    t1_move=(t1-spot)*direction if math.isfinite(t1) else np.nan;t2_move=(t2-spot)*direction if math.isfinite(t2) else np.nan;risk=(spot-inv)*direction if math.isfinite(inv) else np.nan
    t1_hit=t2_hit=inv_hit=False;t1_minutes=t2_minutes=inv_minutes=float("nan");first_resolution="OPEN"
    for p,t in zip(values,times):
        move=(p-spot)*direction;mins=max(0.0,(pd.Timestamp(t)-pd.Timestamp(ts)).total_seconds()/60.0)
        # Before T1, invalidation and T1 compete as first resolution.
        if not t1_hit:
            if math.isfinite(risk) and risk>0 and move<=-risk:
                inv_hit=True;inv_minutes=mins;first_resolution="INVALIDATION";break
            if math.isfinite(t1_move) and t1_move>0 and move>=t1_move:
                t1_hit=True;t1_minutes=mins;first_resolution="T1"
                if math.isfinite(t2_move) and t2_move>0 and move>=t2_move:
                    t2_hit=True;t2_minutes=mins;first_resolution="T2";break
                continue
        # After T1, keep observing whether T2 is reached. We do not retroactively
        # turn a valid T1-first setup into a failure if thesis invalidation occurs later.
        if t1_hit and math.isfinite(t2_move) and t2_move>0 and move>=t2_move:
            t2_hit=True;t2_minutes=mins;first_resolution="T2";break
    end_move=float((values[-1]-spot)*direction)
    if t2_hit and math.isfinite(t2_move):resolved_move=float(t2_move)
    elif t1_hit and math.isfinite(t1_move):resolved_move=float(t1_move)
    elif inv_hit and math.isfinite(risk):resolved_move=float(-risk)
    else:resolved_move=end_move
    r_multiple=float(resolved_move/risk) if math.isfinite(risk) and risk>1e-12 else float("nan")
    out={"mfe":mfe,"mae":mae,"end_move":end_move,"resolved_move":resolved_move,"r_multiple":r_multiple,
         "t1_hit":bool(t1_hit),"t2_hit":bool(t2_hit),"invalidated":bool(inv_hit),"outcome":first_resolution,
         "t1_minutes":t1_minutes,"t2_minutes":t2_minutes,"invalidation_minutes":inv_minutes,
         "instrument_mode":_instrument_mode()}
    if _instrument_mode()=="options":
        resolution_minutes = (t2_minutes if t2_hit else t1_minutes if t1_hit else inv_minutes if inv_hit else float("nan"))
        opt=_option_premium_path(r,values,times,direction,resolution_minutes=resolution_minutes)
        out["option_ready"]=bool(opt.get("ready"))
        out["option_reason"]=str(opt.get("reason","")) if not opt.get("ready") else ""
        if opt.get("ready"):
            # In options mode the R multiple is measured on the PREMIUM actually risked,
            # so expectancy reflects what the account would see instead of a stock move.
            entry=float(opt["entry_premium"])
            out["premium_pnl"]=opt["premium_pnl"]; out["premium_mfe"]=opt["premium_mfe"]; out["premium_mae"]=opt["premium_mae"]
            out["premium_return_pct"]=opt["premium_return_pct"]; out["option_strike"]=opt["strike"]; out["option_type"]=opt["option_type"]
            out["option_resolution_minutes"]=opt.get("resolution_minutes"); out["option_pnl_model"]=opt.get("model")
            out["option_risk_unit"]=opt.get("risk_unit");out["option_model_risk_premium"]=opt.get("model_risk_premium");out["option_max_premium_at_risk"]=opt.get("max_premium_at_risk")
            mr=pd.to_numeric(opt.get("model_risk_premium"), errors="coerce")  # `opt` es un dict, no un DataFrame
            out["r_multiple"]=round(float(opt["premium_pnl"]/mr),4) if math.isfinite(float(mr)) and float(mr)>1e-9 else float("nan")
            out["resolved_move"]=float(opt["premium_pnl"])
    return out

def _instrument_mode() -> str:
    m=str(os.getenv("ITM_INSTRUMENT_MODE","underlying")).strip().lower()
    return "options" if m.startswith("opt") else "underlying"


def _option_premium_path(r: pd.Series, values: np.ndarray, times: list, direction: int,
                         resolution_minutes: float = float("nan")) -> Dict[str, Any]:
    """Simulate the PREMIUM P&L of an ATM option instead of the underlying move.

    WHY. `_eval_signal` measures the move of the stock. If you trade 0DTE options that
    is not your P&L: the position gains through delta but bleeds theta at a violent
    rate, and an IV drop after the move can leave you red with the underlying exactly
    where you wanted it. An R of +1.5 measured on the stock can be -0.3 on the premium.

    ASSUMPTIONS, all explicit:
      - long a single ATM option at the zone, expiry per the logged DTE;
      - IV held flat along the path (no vol crush modelled), so this is an OPTIMISTIC
        bound on the options result rather than a flattering-by-accident one;
      - entry at the ask and exit at the bid, via the configured half-spread;
      - calendar-time decay from the logged DTE, decremented along the path.
    Returns ready=False when atm_iv/dte are missing from the log rather than inventing them.
    """
    try:
        from .precision_engine import black_scholes_price, market_inputs
    except Exception:
        return {"ready": False, "reason": "precision_engine no disponible"}
    iv=float(pd.to_numeric(r.get("atm_iv_decimal"), errors="coerce")) if r.get("atm_iv_decimal") is not None else float("nan")
    dte0=float(pd.to_numeric(r.get("signal_dte"), errors="coerce")) if r.get("signal_dte") is not None else float("nan")
    spot0=float(pd.to_numeric(r.get("spot"), errors="coerce"))
    strike=float(pd.to_numeric(r.get("zone_center"), errors="coerce")) if r.get("zone_center") is not None else spot0
    if not all(math.isfinite(v) for v in (iv,dte0,spot0)) or iv<=0 or dte0<=0:
        return {"ready": False, "reason": "faltan atm_iv/dte en el log del scanner"}
    if not math.isfinite(strike) or strike<=0: strike=spot0
    half=float(os.getenv("ITM_OPTION_HALF_SPREAD_PCT","2.5"))/100.0
    typ="call" if direction>0 else "put"
    mi=market_inputs(str(r.get("symbol","DIA")),dte0)
    rf,q=mi["risk_free_rate"],mi["dividend_yield"]
    t0=pd.Timestamp(r["timestamp"])
    theo0=black_scholes_price(spot0,strike,year_fraction(dte0),iv,typ,rf,q)
    if not math.isfinite(theo0) or theo0<=0:
        return {"ready": False, "reason": "prima teórica no válida"}
    entry=theo0*(1.0+half)
    prem=[]
    for px,t in zip(values,times):
        mins=max(0.0,(pd.Timestamp(t)-t0).total_seconds()/60.0)
        dte_now=max(dte0-mins/1440.0,0.0)
        v=black_scholes_price(float(px),strike,year_fraction(dte_now),iv,typ,rf,q)
        prem.append(v*(1.0-half) if math.isfinite(v) else np.nan)
    prem=np.asarray(prem,dtype=float)
    if not np.isfinite(prem).any():
        return {"ready": False, "reason": "no se pudo revaluar la prima"}
    # Evaluate the premium at the SAME structural resolution used by the underlying
    # backtest (T1/T2/invalidation), not at an arbitrary later horizon.  If no event
    # resolves, use the horizon end.
    finite_idx=np.flatnonzero(np.isfinite(prem))
    exit_idx=int(finite_idx[-1])
    if math.isfinite(resolution_minutes):
        mins=np.asarray([max(0.0,(pd.Timestamp(t)-t0).total_seconds()/60.0) for t in times],dtype=float)
        candidates=np.flatnonzero(np.isfinite(prem) & (mins>=float(resolution_minutes)-1e-9))
        if candidates.size:
            exit_idx=int(candidates[0])
    pnl=prem-entry; last=float(pnl[exit_idx])
    path_pnl=pnl[:exit_idx+1]
    # Risk unit is the modelled loss to structural invalidation at the SAME elapsed
    # time as the resolved trade. FULL PREMIUM remains visible as max capital at risk,
    # but is no longer the denominator of R.
    model_risk=float("nan"); inv_premium=float("nan")
    try:
        inv=float(pd.to_numeric(r.get("invalidation"), errors="coerce"))
        elapsed=float(resolution_minutes) if math.isfinite(resolution_minutes) else max(0.0,(pd.Timestamp(times[exit_idx])-t0).total_seconds()/60.0)
        dte_inv=max(dte0-elapsed/1440.0,0.0)
        inv_theo=black_scholes_price(inv,strike,year_fraction(dte_inv),iv,typ,rf,q)
        inv_premium=float(inv_theo*(1.0-half)) if math.isfinite(inv_theo) else float("nan")
        model_risk=float(entry-inv_premium) if math.isfinite(inv_premium) else float("nan")
    except Exception as _e:
        _obs_note('calibration:187', _e)
    return {"ready": True,"option_type":typ,"strike":round(strike,2),"entry_premium":round(entry,4),
            "exit_premium":round(float(prem[exit_idx]),4),"premium_pnl":round(last,4),
            "premium_mfe":round(float(np.nanmax(path_pnl)),4),"premium_mae":round(float(np.nanmin(path_pnl)),4),
            "premium_return_pct":round(100.0*last/max(entry,1e-9),2),"half_spread_pct":round(half*100,2),
            "resolution_minutes":None if not math.isfinite(resolution_minutes) else round(float(resolution_minutes),3),
            "model":"THEORETICAL_IV_CONSTANT","risk_unit":"MODEL_TO_INVALIDATION_AT_RESOLUTION_TIME",
            "model_risk_premium":None if not math.isfinite(model_risk) else round(model_risk,4),
            "invalidation_premium":None if not math.isfinite(inv_premium) else round(inv_premium,4),
            "max_premium_at_risk":round(entry,4),
            "note":"P&L teórico: IV constante, spread fijo y sin vol crush. R usa riesgo modelado hasta invalidación; prima completa se reporta aparte."}


def _perf(g: pd.DataFrame) -> Dict[str, Any]:
    if g.empty:return {"samples":0}
    pnl=pd.to_numeric(g.get("resolved_move",g.get("end_move")),errors="coerce").fillna(0.0)
    gains=pnl[pnl>0];losses=pnl[pnl<0]
    pf=float(gains.sum()/max(abs(losses.sum()),1e-12)) if len(losses) else float("inf") if len(gains) else 0.0
    evidence=numeric_column(g,"evidence_score",float("nan")).clip(0,100)/100.0;actual=(pnl>0).astype(float);valid=evidence.notna()&actual.notna();brier=float(((evidence[valid]-actual[valid])**2).mean()) if valid.any() else float("nan")
    rmult=numeric_column(g,"r_multiple",float("nan"))
    def av(c):
        x=pd.to_numeric(g.get(c),errors="coerce").dropna();return float(x.mean()) if len(x) else float("nan")
    positive=float((pnl>0).mean()*100)
    out={"samples":int(len(g)),"directional_positive_pct":round(float((pd.to_numeric(g["end_move"],errors="coerce")>0).mean()*100),1),
         "positive_end_pct":round(positive,1),"resolved_positive_pct":round(positive,1),
         "t1_hit_pct":round(float(g["t1_hit"].mean()*100),1),"t2_hit_pct":round(float(g["t2_hit"].mean()*100),1),
         "invalidation_pct":round(float(g["invalidated"].mean()*100),1),"avg_mfe":round(float(g["mfe"].mean()),4),"avg_mae":round(float(g["mae"].mean()),4),
         "expectancy":round(float(pnl.mean()),4),"expectancy_r":None if not rmult.notna().any() else round(float(rmult.mean()),3),
         "profit_factor":None if not math.isfinite(pf) else round(pf,3),"brier_diagnostic":None if not math.isfinite(brier) else round(brier,4),
         "avg_t1_minutes":None if not math.isfinite(av("t1_minutes")) else round(av("t1_minutes"),2),
         "avg_t2_minutes":None if not math.isfinite(av("t2_minutes")) else round(av("t2_minutes"),2),
         "avg_invalidation_minutes":None if not math.isfinite(av("invalidation_minutes")) else round(av("invalidation_minutes"),2)}
    if "option_risk_unit" in g.columns:
        units=[str(x) for x in g["option_risk_unit"].dropna().unique() if str(x) and str(x).lower()!="nan"]
        out["risk_unit"]=units[0] if len(units)==1 else "MIXED" if units else "UNDERLYING_TO_INVALIDATION"
    else:
        out["risk_unit"]="UNDERLYING_TO_INVALIDATION"
    return out

def _cost_assumptions() -> Dict[str,Any]:
    def measured_nonnegative(name: str) -> tuple[float | None, bool]:
        raw=os.getenv(name)
        if raw is None or str(raw).strip()=="":
            return None,False
        try:
            x=float(raw)
            if not math.isfinite(x) or x<0:raise ValueError
            return x,True
        except Exception:
            return None,False
    slip,slip_measured=measured_nonnegative("ITM_BACKTEST_SLIPPAGE_BPS")
    fees,fees_measured=measured_nonnegative("ITM_BACKTEST_COST_BPS")
    cost_r,cost_r_measured=measured_nonnegative("ITM_BACKTEST_COST_R")
    bps_measured=bool(slip_measured and fees_measured)
    return {
            "slippage_bps":slip,"roundtrip_cost_bps":fees,
            "bps_measured":bps_measured,
            "bps_source":"CONFIGURED_MEASURED" if bps_measured else "UNAVAILABLE",
            # EV works in R units. Unknown execution cost is not equivalent to zero.
            "cost_r":cost_r,"cost_measured":cost_r_measured,
            "cost_source":"CONFIGURED_MEASURED" if cost_r_measured else "UNAVAILABLE"}

def _pnl_stats(pnl: pd.Series, prefix: str) -> Dict[str,Any]:
    gains=pnl[pnl>0];losses=pnl[pnl<0]
    pf=float(gains.sum()/max(abs(losses.sum()),1e-12)) if len(losses) else float("inf") if len(gains) else 0.0
    return {
        f"{prefix}_expectancy":round(float(pnl.mean()),4),
        f"{prefix}_profit_factor":None if not math.isfinite(pf) else round(pf,3),
        f"{prefix}_positive_pct":round(float((pnl>0).mean()*100),1),
    }

def _cost_adjust(g: pd.DataFrame, assumptions: Dict[str,Any]) -> Dict[str,Any]:
    if g.empty:return {"samples":0,"cost_bps":None,"cost_measured":False}
    x=g.copy()
    gross=numeric_column(x,"resolved_move",0)
    out={"samples":int(len(x)),"cost_bps":None,"cost_measured":False,**_pnl_stats(gross,"gross")}
    # A missing slippage/fee measurement is UNKNOWN, not zero.  In that state we
    # deliberately do not publish metrics labelled NET.
    if not bool(assumptions.get("bps_measured")):
        out.update({"net_expectancy":None,"net_profit_factor":None,"net_positive_pct":None,
                    "reason":"EXECUTION_BPS_UNAVAILABLE"})
        return out
    bps=float(assumptions["slippage_bps"])+float(assumptions["roundtrip_cost_bps"])
    spots=pd.to_numeric(x.get("spot",pd.Series(index=x.index,dtype=float)),errors="coerce") if "spot" in x.columns else pd.Series(np.nan,index=x.index)
    cost=spots.abs().fillna(0)*bps/10000.0
    pnl=gross-cost
    out.update({"cost_bps":round(bps,3),"cost_measured":True,**_pnl_stats(pnl,"net")})
    return out

# ============================================================================
# v1.14.3 · PROBABILITY CALIBRATION AND EXPECTED VALUE
# ----------------------------------------------------------------------------
# Evidence has always been an internal 0-100 relevance score, explicitly not a
# probability. That is honest, but it left the decision rule with nothing to work
# with: edge_state thresholded evidence at 52/65 while rr_t1 and rr_t2 were computed
# and then ignored. A setup with evidence 70 and R/R 0.4 outranked one with evidence
# 55 and R/R 2.5, even though the second is the better bet at any sensible hit rate.
#
# The fix is two steps. First map evidence to a MEASURED probability of the outcome
# that actually matters - reaching T1 before invalidation - using isotonic regression
# on the logged history. Isotonic is the right tool here: it assumes only that higher
# evidence should not mean a lower hit rate, and fits the shape from data rather than
# imposing a sigmoid. Second, combine that probability with the R/R the scanner
# already knows.
# ============================================================================

def _pava(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pool-Adjacent-Violators: weighted isotonic (non-decreasing) fit."""
    order = np.argsort(x, kind="mergesort")
    xs, ys, ws = x[order], y[order], w[order]
    vals = list(ys.astype(float)); wts = list(ws.astype(float)); idx = list(range(len(ys)))
    i = 0
    while i < len(vals) - 1:
        if vals[i] <= vals[i + 1] + 1e-12:
            i += 1; continue
        tw = wts[i] + wts[i + 1]
        vals[i] = (vals[i] * wts[i] + vals[i + 1] * wts[i + 1]) / max(tw, 1e-12)
        wts[i] = tw
        del vals[i + 1]; del wts[i + 1]; del idx[i + 1]
        if i > 0: i -= 1
    knots_x = xs[idx]
    return knots_x, np.asarray(vals, dtype=float)


def _isotonic_predict(knots_x: np.ndarray, knots_y: np.ndarray, q: np.ndarray) -> np.ndarray:
    if len(knots_x) == 0:
        return np.full(np.shape(q), 0.5, dtype=float)
    return np.interp(np.asarray(q, dtype=float), knots_x, knots_y,
                     left=float(knots_y[0]), right=float(knots_y[-1]))


def _brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((np.asarray(p, float) - np.asarray(y, float)) ** 2))


def _log_loss(p: np.ndarray, y: np.ndarray) -> float:
    pp=np.clip(np.asarray(p,float),1e-6,1-1e-6); yy=np.asarray(y,float)
    return float(-np.mean(yy*np.log(pp)+(1.0-yy)*np.log(1.0-pp)))

def _fit_platt(scores: np.ndarray, y: np.ndarray) -> dict[str,float] | None:
    """Two-parameter Platt calibration fitted by damped Newton iterations."""
    x=np.asarray(scores,float)/100.0; t=np.asarray(y,float)
    ok=np.isfinite(x)&np.isfinite(t);x=x[ok];t=t[ok]
    if len(x)<10 or len(np.unique(t))<2:return None
    n1=float(t.sum());n0=float(len(t)-n1)
    hi=(n1+1.0)/(n1+2.0);lo=1.0/(n0+2.0);tt=np.where(t>.5,hi,lo)
    a=b=0.0
    try:
        for _ in range(100):
            z=np.clip(a*x+b,-35,35);pr=1.0/(1.0+np.exp(-z));w=np.maximum(pr*(1-pr),1e-10)
            g=np.array([np.sum((pr-tt)*x),np.sum(pr-tt)])
            H=np.array([[np.sum(w*x*x),np.sum(w*x)],[np.sum(w*x),np.sum(w)]])+1e-8*np.eye(2)
            step=np.linalg.solve(H,g);a-=float(step[0]);b-=float(step[1])
            if float(np.max(np.abs(step)))<1e-9:break
        return {"a":float(a),"b":float(b)}
    except Exception:
        return None


def _predict_platt(params: dict[str,float] | None, scores: np.ndarray) -> np.ndarray:
    if not params:return np.full(np.shape(scores),np.nan,dtype=float)
    x=np.asarray(scores,float)/100.0;z=np.clip(float(params["a"])*x+float(params["b"]),-35,35)
    return 1.0/(1.0+np.exp(-z))


def _nested_session_split(sessions: list[str]) -> dict[str,list[str]]:
    """Chronological TRAIN -> purge -> VALIDATION -> purge -> FINAL OOS split."""
    ss=sorted(dict.fromkeys(str(x) for x in sessions))
    n=len(ss)
    if n<6:return {"train":ss,"purge1":[],"validation":[],"purge2":[],"final":[]}
    train_n=max(3,int(math.floor(n*.50)))
    val_n=max(1,int(math.floor(n*.20)))
    # Reserve two purge sessions and at least two final sessions when possible.
    while train_n+val_n+4>n and val_n>1:val_n-=1
    while train_n+val_n+4>n and train_n>3:train_n-=1
    i=train_n
    purge1=ss[i:i+1];i+=len(purge1)
    validation=ss[i:i+val_n];i+=len(validation)
    purge2=ss[i:i+1];i+=len(purge2)
    final=ss[i:]
    return {"train":ss[:train_n],"purge1":purge1,"validation":validation,"purge2":purge2,"final":final}


def _fit_calibrator(kind: str, scores: np.ndarray, y: np.ndarray) -> dict[str,Any] | None:
    kind=str(kind).upper()
    if kind=="PLATT":
        pars=_fit_platt(scores,y)
        return {"kind":"PLATT","params":pars} if pars else None
    if kind=="ISOTONIC":
        if len(scores)<5:return None
        kx,ky=_pava(np.asarray(scores,float),np.asarray(y,float),np.ones(len(y),dtype=float))
        return {"kind":"ISOTONIC","kx":kx,"ky":ky}
    return None


def _predict_calibrator(model: dict[str,Any] | None, scores: np.ndarray) -> np.ndarray:
    if not model:return np.full(np.shape(scores),np.nan,dtype=float)
    if model.get("kind")=="PLATT":return _predict_platt(model.get("params"),scores)
    if model.get("kind")=="ISOTONIC":return _isotonic_predict(np.asarray(model.get("kx"),float),np.asarray(model.get("ky"),float),np.asarray(scores,float))
    return np.full(np.shape(scores),np.nan,dtype=float)


def _calibrator_knots(model: dict[str,Any] | None) -> list[dict[str,float]]:
    if not model:return []
    if model.get("kind")=="ISOTONIC":
        return [{"evidence":round(float(a),2),"probability":round(float(np.clip(b,.01,.99)),4)} for a,b in zip(model.get("kx",[]),model.get("ky",[]))][:60]
    grid=np.linspace(0,100,41);pred=np.clip(_predict_calibrator(model,grid),.01,.99)
    return [{"evidence":round(float(a),2),"probability":round(float(b),4)} for a,b in zip(grid,pred)]


def _block_bootstrap_brier_skill(p_model: np.ndarray, p_base: np.ndarray, y: np.ndarray,
                                 session_ids: np.ndarray, n_boot: int = 1500,
                                 seed: int = 12712) -> np.ndarray:
    """Resample whole sessions, preserving intraday dependence inside each block."""
    pm=np.asarray(p_model,float);pb=np.asarray(p_base,float);yy=np.asarray(y,float);sid=np.asarray(session_ids)
    ok=np.isfinite(pm)&np.isfinite(pb)&np.isfinite(yy);pm=pm[ok];pb=pb[ok];yy=yy[ok];sid=sid[ok]
    uniq=np.unique(sid)
    if len(uniq)<2:return np.array([],dtype=float)
    blocks=[np.flatnonzero(sid==u) for u in uniq];rng=np.random.default_rng(seed);out=[]
    for _ in range(max(200,int(n_boot))):
        chosen=rng.integers(0,len(blocks),size=len(blocks));idx=np.concatenate([blocks[i] for i in chosen])
        bm=_brier(pm[idx],yy[idx]);bb=_brier(pb[idx],yy[idx])
        if math.isfinite(bm) and math.isfinite(bb) and bb>1e-12:out.append(1.0-bm/bb)
    return np.asarray(out,float)


def _resolution_time_summary(g: pd.DataFrame) -> Dict[str, Any]:
    """Historical minutes-to-resolution, used by the LIVE option economics model.

    The gate must not assume that T1/invalidation happen instantly: theta makes a 0DTE
    target reached in 3 minutes economically different from the same target reached in
    45 minutes.  We therefore persist empirical p25/median/p75 times, with no invented
    fallback when the sample is absent.
    """
    def stat(col: str, mask=None):
        x=pd.to_numeric(g.get(col),errors="coerce")
        if mask is not None: x=x[mask]
        x=x[np.isfinite(x) & (x>=0)]
        if len(x)<3:return {"n":int(len(x)),"p25":None,"median":None,"p75":None}
        return {"n":int(len(x)),"p25":round(float(x.quantile(.25)),3),
                "median":round(float(x.median()),3),"p75":round(float(x.quantile(.75)),3)}
    success=g.get("outcome",pd.Series(index=g.index,dtype=str)).astype(str).isin(["T1","T2"])
    invalid=g.get("outcome",pd.Series(index=g.index,dtype=str)).astype(str).eq("INVALIDATION")
    t1=stat("t1_minutes",success); inv=stat("invalidation_minutes",invalid)
    vals=[]
    for _,r in g.iterrows():
        o=str(r.get("outcome","")); v=(r.get("t1_minutes") if o in {"T1","T2"} else r.get("invalidation_minutes") if o=="INVALIDATION" else None)
        if v is None or pd.isna(v):
            continue
        f=pd.to_numeric(v,errors='coerce')
        if pd.isna(f):
            continue
        f=float(f)
        if math.isfinite(f) and f>=0:
            vals.append(f)
    if len(vals)>=3:
        a=pd.Series(vals,dtype=float); overall={"n":len(vals),"p25":round(float(a.quantile(.25)),3),"median":round(float(a.median()),3),"p75":round(float(a.quantile(.75)),3)}
    else: overall={"n":len(vals),"p25":None,"median":None,"p75":None}
    return {"t1":t1,"invalidation":inv,"overall":overall,
            "note":"Minutos históricos a T1-first/invalidation; solo datos observados y sin DEMO."}


def probability_calibration(ev: pd.DataFrame, sessions: list[str], horizon: int,
                            min_samples: int = 120, min_sessions: int = 8,
                            min_train_samples: int = 40, min_test_samples: int = 20,
                            promote: bool = True) -> Dict[str, Any]:
    """Nested chronological calibration with a final untouched OOS block.

    Candidate choice (Platt vs isotonic vs base rate) is made on VALIDATION only.
    The selected family is then refit using pre-final data and judged once on FINAL OOS.
    Promotion additionally requires a session-block-bootstrap lower confidence bound on
    Brier skill, so many correlated signals from one day cannot masquerade as evidence.
    """
    out={"ready":False,"status":"COLLECTING","stage":"COLLECTING","horizon_minutes":int(horizon),
         "method":"TRAIN -> purge -> VALIDATION(calibrator selection) -> purge -> FINAL OOS; session-block bootstrap"}
    g=ev[ev["horizon"]==horizon].copy()
    if g.empty:return {**out,"reason":"sin evaluaciones para ese horizonte"}
    g["success"]=g["outcome"].astype(str).isin(["T1","T2"]).astype(float)
    g=g[pd.to_numeric(g["evidence_score"],errors="coerce").notna()].copy()
    all_sessions=sorted(dict.fromkeys(str(x) for x in sessions))
    base_rate=float(g["success"].mean()) if len(g) else float("nan")
    out.update({"base_rate":None if not math.isfinite(base_rate) else round(base_rate,4),"samples":int(len(g)),"sessions":len(all_sessions)})
    if len(g)<min_samples or len(all_sessions)<min_sessions:
        return {**out,"reason":f"se requieren >={min_samples} señales y >={min_sessions} sesiones para investigar el calibrador"}

    split=_nested_session_split(all_sessions)
    tr=g[g["session_date"].astype(str).isin(set(split["train"]))].copy()
    va=g[g["session_date"].astype(str).isin(set(split["validation"]))].copy()
    te=g[g["session_date"].astype(str).isin(set(split["final"]))].copy()
    if len(tr)<int(min_train_samples) or len(va)<max(10,int(min_test_samples)//2) or len(te)<int(min_test_samples):
        return {**out,"reason":"TRAIN/VALIDATION/FINAL OOS insuficiente tras purge gaps",
                "train_samples":int(len(tr)),"validation_samples":int(len(va)),"test_samples":int(len(te)),
                "train_sessions":len(split["train"]),"validation_sessions":len(split["validation"]),"test_sessions":len(split["final"]),
                "purged_sessions":len(split["purge1"])+len(split["purge2"]),"split":split}

    xtr=pd.to_numeric(tr["evidence_score"],errors="coerce").clip(0,100).to_numpy(float);ytr=tr["success"].to_numpy(float)
    xva=pd.to_numeric(va["evidence_score"],errors="coerce").clip(0,100).to_numpy(float);yva=va["success"].to_numpy(float)
    train_base=float(np.clip(np.mean(ytr),.01,.99));base_va=np.full_like(yva,train_base)
    candidates=[{"kind":"BASE_RATE","model":None,"log_loss":_log_loss(base_va,yva),"brier":_brier(base_va,yva)}]
    for kind in ("PLATT","ISOTONIC"):
        model=_fit_calibrator(kind,xtr,ytr)
        if model:
            pp=np.clip(_predict_calibrator(model,xva),.01,.99)
            if np.isfinite(pp).all():candidates.append({"kind":kind,"model":model,"log_loss":_log_loss(pp,yva),"brier":_brier(pp,yva)})
    candidates.sort(key=lambda z:(z["log_loss"],z["brier"],0 if z["kind"]=="PLATT" else 1))
    selected=candidates[0]

    # Refit the selected family on all data that precedes the second purge. The FINAL
    # block remains untouched by both family selection and parameter fitting.
    pre_days=set(split["train"]+split["purge1"]+split["validation"])
    fitdf=g[g["session_date"].astype(str).isin(pre_days)].copy()
    xfit=pd.to_numeric(fitdf["evidence_score"],errors="coerce").clip(0,100).to_numpy(float);yfit=fitdf["success"].to_numpy(float)
    model=_fit_calibrator(selected["kind"],xfit,yfit) if selected["kind"]!="BASE_RATE" else None
    xt=pd.to_numeric(te["evidence_score"],errors="coerce").clip(0,100).to_numpy(float);yt=te["success"].to_numpy(float)
    final_base=float(np.clip(np.mean(yfit),.01,.99));p_base=np.full_like(yt,final_base)
    p_model=p_base.copy() if model is None else np.clip(_predict_calibrator(model,xt),.01,.99)
    b_model,b_base=_brier(p_model,yt),_brier(p_base,yt);ll_model,ll_base=_log_loss(p_model,yt),_log_loss(p_base,yt)
    skill=float(1.0-b_model/max(b_base,1e-12))
    try:min_skill=max(0.0,float(os.getenv("ITM_CALIB_MIN_BRIER_SKILL","0.02")))
    except Exception:min_skill=.02
    try:n_boot=max(500,int(os.getenv("ITM_CALIB_BOOTSTRAP_DRAWS","1500")))
    except Exception:n_boot=1500
    dist=_block_bootstrap_brier_skill(p_model,p_base,yt,te["session_date"].astype(str).to_numpy(),n_boot=n_boot)
    if len(dist)>=200:
        lo=float(np.quantile(dist,.025));hi=float(np.quantile(dist,.975));prob_pos=float(np.mean(dist>0))
    else:
        lo=hi=prob_pos=float("nan")
    try:promotion_sessions=max(40,int(os.getenv("ITM_CALIB_PROMOTION_MIN_SESSIONS","40")))
    except Exception:promotion_sessions=40
    try:final_oos_min_sessions=max(4,int(os.getenv("ITM_CALIB_FINAL_OOS_MIN_SESSIONS","8")))
    except Exception:final_oos_min_sessions=8
    research_floor=20
    if len(all_sessions)<research_floor: session_stage="SHADOW"
    elif len(all_sessions)<promotion_sessions: session_stage="PROVISIONAL_RESEARCH"
    else: session_stage="ELIGIBLE_FOR_PROMOTION"
    statistical_pass=bool(selected["kind"]!="BASE_RATE" and b_model<b_base and ll_model<=ll_base and math.isfinite(lo) and lo>min_skill
                          and len(split["final"])>=final_oos_min_sessions)
    beats=bool(promote and session_stage=="ELIGIBLE_FOR_PROMOTION" and statistical_pass)
    pooling_eligible=bool(selected["kind"]!="BASE_RATE" and b_model<=b_base and ll_model<=ll_base and len(te)>=int(min_test_samples))
    stage="CALIBRATED" if beats else session_stage if session_stage!="ELIGIBLE_FOR_PROMOTION" else "SHADOW · FINAL OOS/CI NOT PROMOTED"

    bins=_reliability_from_probs(p_model,yt)
    selection_table=[{"kind":c["kind"],"validation_log_loss":round(float(c["log_loss"]),5),"validation_brier":round(float(c["brier"]),5)} for c in candidates]
    return {**out,"ready":beats,"status":stage,"stage":stage,"eligible_for_activation":beats,
            "pooling_eligible":pooling_eligible,"promotion_enabled":bool(promote),"session_stage":session_stage,
            "selected_calibrator":selected["kind"],"selection_candidates":selection_table,
            "train_sessions":len(split["train"]),"validation_sessions":len(split["validation"]),"purged_sessions":len(split["purge1"])+len(split["purge2"]),"test_sessions":len(split["final"]),
            "train_samples":int(len(tr)),"validation_samples":int(len(va)),"test_samples":int(len(te)),"split":split,
            "final_oos_sessions":list(split["final"]),"brier_model":round(b_model,5),"brier_base_rate":round(b_base,5),
            "log_loss_model":round(ll_model,5),"log_loss_base_rate":round(ll_base,5),"brier_skill_score":round(skill,4),
            "brier_skill_ci_low":None if not math.isfinite(lo) else round(lo,4),"brier_skill_ci_high":None if not math.isfinite(hi) else round(hi,4),
            "bootstrap_prob_skill_positive":None if not math.isfinite(prob_pos) else round(prob_pos,4),"bootstrap_draws":int(len(dist)),
            "minimum_brier_skill":round(min_skill,4),"minimum_promotion_sessions":promotion_sessions,
            "minimum_final_oos_sessions":final_oos_min_sessions,"final_oos_session_gate_pass":len(split["final"])>=final_oos_min_sessions,
            "beats_base_rate":bool(b_model<b_base),"reliability":bins,"knots":_calibrator_knots(model),
            "train_success_rate":round(float(np.mean(yfit)),4),"statistical_promotion_pass":statistical_pass,
            "reason":None if beats else ("sesiones aun insuficientes para promocion" if session_stage!="ELIGIBLE_FOR_PROMOTION" else "limite inferior bootstrap / log-loss no supera el gate"),
            "note":"La familia se elige en VALIDATION y se juzga en FINAL OOS. Promocion requiere >=40 sesiones totales, un bloque FINAL OOS minimo y LCB bootstrap por sesion > skill minimo; el historial de evaluaciones se audita aparte por firma OOS."}



def _model_probability(model: Dict[str, Any], evidence: float) -> float | None:
    knots=model.get("knots") or []
    if not knots:return None
    try:
        xs=np.asarray([float(k["evidence"]) for k in knots],dtype=float);ys=np.asarray([float(k["probability"]) for k in knots],dtype=float)
        return float(np.clip(np.interp(float(evidence),xs,ys,left=ys[0],right=ys[-1]),.01,.99))
    except Exception:return None


def _reliability_from_probs(probabilities: np.ndarray, outcomes: np.ndarray) -> list[Dict[str, Any]]:
    bins=[]
    edges=[0,.2,.35,.5,.65,.8,1.01]
    p=np.asarray(probabilities,dtype=float); y=np.asarray(outcomes,dtype=float)
    for lo,hi in zip(edges[:-1],edges[1:]):
        m=(p>=lo)&(p<hi)
        if int(m.sum())>=5:
            bins.append({"predicted_range":f"{lo:.2f}-{hi:.2f}","n":int(m.sum()),
                         "mean_predicted":round(float(p[m].mean()),4),
                         "observed_frequency":round(float(y[m].mean()),4)})
    return bins


def _component_train_days(sessions: list[str]) -> tuple[set[str], set[str], set[str]]:
    """Return pre-final, purge and FINAL OOS sets from the nested calibration split."""
    sp=_nested_session_split(list(sessions or []))
    pre=set(sp["train"]+sp["purge1"]+sp["validation"])
    purge=set(sp["purge2"])
    return pre,purge,set(sp["final"])


def _prob_array(model: Dict[str, Any], evidence: pd.Series) -> np.ndarray | None:
    if not model or not (model.get("knots") or []):
        return None
    vals=[]
    for v in pd.to_numeric(evidence,errors="coerce"):
        q=_model_probability(model,float(v)) if math.isfinite(float(v)) else None
        vals.append(np.nan if q is None else float(q))
    a=np.asarray(vals,dtype=float)
    return a if np.isfinite(a).any() else None


def _select_blend_weight_oos(ev: pd.DataFrame, horizon: int, global_model: Dict[str, Any],
                             scope_model: Dict[str, Any], all_sessions: list[str],
                             scope_sessions: list[str]) -> Dict[str, Any]:
    """Select and *then* evaluate the pooling weight on strictly later scope sessions.

    The isotonic component curves are already fit on their earlier train blocks.  We
    take only scope sessions that are OOS for BOTH components, split those later
    sessions into a weight-selection block and a still-later final validation block,
    choose w by log-loss on the first block, and report Brier/log-loss on the final
    block.  Therefore the metrics shown for a blend are the metrics of the blend that
    actually runs, not one of its components.
    """
    out={"ready":False,"status":"COLLECTING","method":"OOS weight selection + later OOS blend validation"}
    if not global_model or not scope_model or not global_model.get("knots") or not scope_model.get("knots"):
        return {**out,"reason":"faltan curvas global/scope para mezclar"}
    gtr,gpur,goos=_component_train_days(all_sessions)
    strn,spur,soos=_component_train_days(scope_sessions)
    eligible=[d for d in scope_sessions if d in goos and d in soos and d not in gtr|gpur|strn|spur]
    if len(eligible)<2:
        return {**out,"reason":"faltan sesiones scope OOS comunes a ambos componentes","eligible_sessions":len(eligible)}
    cut=max(1,len(eligible)//2)
    select_days=eligible[:cut]; test_days=eligible[cut:]
    if not test_days:
        return {**out,"reason":"falta bloque final posterior para validar el blend","selection_sessions":len(select_days),"test_sessions":0}
    g=ev[ev["horizon"]==horizon].copy()
    if "expiry_mode" in g.columns and scope_sessions:
        scope_name=None
        # infer target scope from the first scope row/session; caller already prefiltered scope_sessions
        scope_dates=set(scope_sessions)
        scoped_rows=g[g["session_date"].astype(str).isin(scope_dates)].copy()
        if not scoped_rows.empty:
            modes=scoped_rows["expiry_mode"].astype(str).str.upper().value_counts()
            if len(modes): scope_name=str(modes.index[0])
        if scope_name:
            g=g[g["expiry_mode"].astype(str).str.upper()==scope_name]
    g["success"]=g["outcome"].astype(str).isin(["T1","T2"]).astype(float)
    sel=g[g["session_date"].astype(str).isin(set(select_days))].copy()
    tst=g[g["session_date"].astype(str).isin(set(test_days))].copy()
    try:min_sel=max(8,int(os.getenv("ITM_CALIB_POOL_MIN_SELECT_SAMPLES","10")))
    except Exception:min_sel=10
    try:min_tst=max(8,int(os.getenv("ITM_CALIB_POOL_MIN_TEST_SAMPLES","10")))
    except Exception:min_tst=10
    if len(sel)<min_sel or len(tst)<min_tst:
        return {**out,"reason":"muestra OOS insuficiente para seleccionar y validar w",
                "selection_sessions":len(select_days),"test_sessions":len(test_days),
                "selection_samples":int(len(sel)),"test_samples":int(len(tst)),
                "required_selection_samples":min_sel,"required_test_samples":min_tst}
    pg_sel=_prob_array(global_model,sel["evidence_score"]); ps_sel=_prob_array(scope_model,sel["evidence_score"])
    pg_tst=_prob_array(global_model,tst["evidence_score"]); ps_tst=_prob_array(scope_model,tst["evidence_score"])
    if any(x is None for x in (pg_sel,ps_sel,pg_tst,ps_tst)):
        return {**out,"reason":"una curva no pudo producir probabilidades OOS"}
    msel=np.isfinite(pg_sel)&np.isfinite(ps_sel); mtst=np.isfinite(pg_tst)&np.isfinite(ps_tst)
    if int(msel.sum())<min_sel or int(mtst.sum())<min_tst:
        return {**out,"reason":"probabilidades OOS finitas insuficientes"}
    ysel=sel["success"].to_numpy(float)[msel]; yt=tst["success"].to_numpy(float)[mtst]
    pg_sel=pg_sel[msel];ps_sel=ps_sel[msel];pg_tst=pg_tst[mtst];ps_tst=ps_tst[mtst]
    try:step=float(os.getenv("ITM_CALIB_POOL_WEIGHT_STEP","0.05"))
    except Exception:step=.05
    step=float(np.clip(step,.01,.25))
    grid=list(np.arange(0.0,1.0+step/2,step))
    # Keep the old n/(n+k) formula only as a candidate, never as a decree.
    try:k=max(1.0,float(os.getenv("ITM_CALIB_POOLING_K","100")))
    except Exception:k=100.0
    n_scope=float(scope_model.get("train_samples") or scope_model.get("samples") or 0.0)
    prior_w=float(np.clip(n_scope/(n_scope+k),0,1))
    grid=sorted(set([round(float(w),6) for w in grid]+[round(prior_w,6),1.0,0.0]))
    candidates=[]
    for w in grid:
        pp=np.clip((1-w)*pg_sel+w*ps_sel,.01,.99)
        candidates.append((float(_log_loss(pp,ysel)),float(_brier(pp,ysel)),float(w)))
    candidates.sort(key=lambda z:(z[0],z[1],abs(z[2]-prior_w)))
    sel_ll,sel_b,w=candidates[0]
    p=np.clip((1-w)*pg_tst+w*ps_tst,.01,.99)
    # Baseline is learned from historical scope train when available, otherwise global train.
    scope_train_rows=g[g["session_date"].astype(str).isin(strn)]
    if len(scope_train_rows)>=10:
        br=float(scope_train_rows["outcome"].astype(str).isin(["T1","T2"]).mean())
        base_source="SCOPE TRAIN"
    else:
        allg=ev[(ev["horizon"]==horizon)&(ev["session_date"].astype(str).isin(gtr))]
        br=float(allg["outcome"].astype(str).isin(["T1","T2"]).mean()) if len(allg) else .5
        base_source="GLOBAL TRAIN"
    pbase=np.full_like(yt,float(np.clip(br,.01,.99)))
    bm,bb=_brier(p,yt),_brier(pbase,yt); lm,lb=_log_loss(p,yt),_log_loss(pbase,yt)
    skill=float(1.0-bm/max(bb,1e-9))
    try:min_skill=max(0.0,float(os.getenv("ITM_CALIB_MIN_BRIER_SKILL","0.02")))
    except Exception:min_skill=.02
    ready=bool(bm<bb and skill>=min_skill and len(yt)>=min_tst)
    return {**out,"ready":ready,"status":"VALIDATED" if ready else "FAILED OOS",
            "selected_weight_scope":round(w,4),"selected_weight_global":round(1-w,4),
            "prior_formula_weight_scope":round(prior_w,4),"pooling_k_candidate":round(k,2),
            "selection_sessions":len(select_days),"test_sessions":len(test_days),
            "selection_samples":int(len(ysel)),"test_samples":int(len(yt)),
            "selection_log_loss":round(sel_ll,5),"selection_brier":round(sel_b,5),
            "brier_model":round(float(bm),5),"brier_base_rate":round(float(bb),5),
            "brier_skill_score":round(float(skill),4),"minimum_brier_skill":round(min_skill,4),
            "log_loss_model":round(float(lm),5),"log_loss_base_rate":round(float(lb),5),
            "base_rate":round(float(br),4),"base_rate_source":base_source,
            "beats_base_rate":bool(bm<bb),"reliability":_reliability_from_probs(p,yt),
            "candidate_count":len(candidates),
            "note":"w se elige en un bloque OOS y el blend se juzga en un bloque posterior distinto. k=100, si existe, solo propone un candidato."}


def _blend_probability_models(global_model: Dict[str, Any], scope_model: Dict[str, Any] | None,
                              weight: float, expiry_mode: str, resolution_time: Dict[str, Any],
                              blend_validation: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Build the curve that actually runs and attach *its own* OOS metrics."""
    gm=global_model or {}; sm=scope_model or {}; val=blend_validation or {}
    w=float(np.clip(weight,0,1)) if sm else 0.0
    xs=sorted(set([float(k["evidence"]) for k in (gm.get("knots") or [])]+[float(k["evidence"]) for k in (sm.get("knots") or [])]))
    knots=[]
    for x in xs:
        pg=_model_probability(gm,x); ps=_model_probability(sm,x) if sm else None
        if pg is None and ps is None:continue
        p=ps if pg is None else pg if ps is None else (1.0-w)*pg+w*ps
        knots.append({"evidence":round(x,2),"probability":round(float(p),4)})
    global_ready=bool(gm.get("ready")); scope_strict=bool(sm.get("ready"))
    if sm and w>0 and val:
        ready=bool(val.get("ready"))
        stage="CALIBRATED · PARTIAL POOL" if ready else "SHADOW · PARTIAL POOL"
        source="GLOBAL + EXPIRY · w OOS"
        metrics=val
    elif global_ready:
        ready=True;stage="CALIBRATED · GLOBAL BACKOFF";source="GLOBAL BACKOFF";metrics=gm
    elif scope_strict:
        ready=True;stage="CALIBRATED · EXPIRY ONLY";source="EXPIRY ONLY";metrics=sm
    else:
        ready=False;stage="SHADOW";source="SIN COMPONENTE CALIBRADO";metrics=val or gm or sm
    return {"ready":ready,"status":stage,"stage":stage,"source_label":source,"knots":knots,
            "expiry_mode":str(expiry_mode or "ALL"),"pooling_weight_scope":round(w,4),
            "pooling_weight_global":round(1.0-w,4),
            "global_samples":int(gm.get("samples",0) or 0),"scope_samples":int(sm.get("samples",0) or 0),
            "global_stage":gm.get("stage",gm.get("status","COLLECTING")),"scope_stage":sm.get("stage",sm.get("status","COLLECTING")),
            "base_rate":metrics.get("base_rate"),"brier_model":metrics.get("brier_model"),"brier_base_rate":metrics.get("brier_base_rate"),
            "brier_skill_score":metrics.get("brier_skill_score"),"log_loss_model":metrics.get("log_loss_model"),"log_loss_base_rate":metrics.get("log_loss_base_rate"),
            "beats_base_rate":metrics.get("beats_base_rate"),"reliability":metrics.get("reliability",[]),"resolution_time":resolution_time,
            "blend_validation":val,
            "global_component":{k:gm.get(k) for k in ("ready","stage","samples","train_samples","test_samples","brier_model","brier_base_rate","log_loss_model","log_loss_base_rate","brier_skill_score")},
            "scope_component":{k:sm.get(k) for k in ("ready","pooling_eligible","stage","samples","train_samples","test_samples","brier_model","brier_base_rate","log_loss_model","log_loss_base_rate","brier_skill_score")},
            "note":"Las métricas visibles pertenecen al modelo que corre. Un partial pool solo se promueve si su mezcla supera la tasa base en un bloque final OOS distinto del usado para elegir w."}


def hierarchical_probability_calibration(ev: pd.DataFrame, expiry_mode: str | None, horizon: int) -> Dict[str, Any]:
    """Global -> Expiry pooling; w is empirical and the running blend is validated OOS."""
    all_sessions=sorted(str(x) for x in ev.get("session_date",pd.Series(dtype=str)).dropna().unique())
    try: global_min_samples=max(30,int(os.getenv("ITM_CALIB_MIN_SAMPLES","120")))
    except Exception: global_min_samples=120
    try: global_min_sessions=max(40,int(os.getenv("ITM_CALIB_MIN_SESSIONS","40")))
    except Exception: global_min_sessions=40
    global_model=probability_calibration(ev,all_sessions,horizon,min_samples=global_min_samples,min_sessions=global_min_sessions)
    mode=str(expiry_mode or "").upper().strip()
    scoped=ev if not mode or mode=="ALL" else ev[ev.get("expiry_mode",pd.Series(index=ev.index,dtype=str)).astype(str).str.upper()==mode].copy()
    scope_sessions=sorted(str(x) for x in scoped.get("session_date",pd.Series(dtype=str)).dropna().unique())
    scope_strict=probability_calibration(scoped,scope_sessions,horizon,min_samples=global_min_samples,min_sessions=global_min_sessions) if mode and mode!="ALL" else global_model
    scope_component=scope_strict
    if mode and mode!="ALL" and not scope_strict.get("ready"):
        try: scope_min=max(30,int(os.getenv("ITM_CALIB_SCOPE_MIN_SAMPLES","40")))
        except Exception: scope_min=40
        try: scope_sessions_min=max(8,int(os.getenv("ITM_CALIB_SCOPE_MIN_SESSIONS","20")))
        except Exception: scope_sessions_min=20
        shadow=probability_calibration(scoped,scope_sessions,horizon,min_samples=scope_min,min_sessions=scope_sessions_min,
                                       min_train_samples=max(20,scope_min//2),min_test_samples=10,promote=False)
        if shadow.get("knots") and shadow.get("pooling_eligible"): scope_component=shadow
        elif not scope_strict.get("knots"): scope_component={}
    # Time model remains scope-first with explicit global backoff.
    time_horizon=int(max(numeric_column(ev,"horizon",float("nan")).dropna().tolist() or [horizon]))
    rt_scope=_resolution_time_summary(scoped[scoped["horizon"]==time_horizon]) if not scoped.empty else {}
    rt_global=_resolution_time_summary(ev[ev["horizon"]==time_horizon]) if not ev.empty else {}
    usable=lambda rt: int(((rt or {}).get("t1") or {}).get("n",0))>=3 and int(((rt or {}).get("invalidation") or {}).get("n",0))>=3
    resolution=rt_scope if usable(rt_scope) else rt_global
    resolution=dict(resolution or {});resolution["source"]="EXPIRY" if usable(rt_scope) else "GLOBAL BACKOFF" if usable(rt_global) else "UNAVAILABLE";resolution["horizon_minutes"]=time_horizon

    if not mode or mode=="ALL":
        model=_blend_probability_models(global_model,None,0.0,"ALL",resolution,{})
        model["pooling_weight_method"]="NO POOLING · GLOBAL"
        model["scope_samples_raw"]=int(len(scoped));model["global_samples_raw"]=int(len(ev))
        return model

    val=_select_blend_weight_oos(ev,horizon,global_model,scope_component or {},all_sessions,scope_sessions) if scope_component else {"ready":False,"status":"COLLECTING","reason":"scope sin curva"}
    if val.get("ready"):
        w=float(val.get("selected_weight_scope",0.0) or 0.0)
        model=_blend_probability_models(global_model,scope_component,w,mode,resolution,val)
        model["pooling_weight_method"]="EMPIRICAL OOS"
    elif global_model.get("ready"):
        # Do not run an unvalidated mixture.  Keep the proven global curve until the
        # scope blend earns promotion on its own final holdout.
        model=_blend_probability_models(global_model,None,0.0,mode,resolution,{})
        model["status"]="CALIBRATED · GLOBAL BACKOFF · SCOPE BLEND NOT VALIDATED"
        model["stage"]=model["status"];model["source_label"]="GLOBAL BACKOFF"
        model["blend_validation"]=val;model["pooling_weight_method"]="GLOBAL UNTIL BLEND PASSES OOS"
    elif scope_strict.get("ready"):
        model=_blend_probability_models({},scope_strict,1.0,mode,resolution,{})
        model["pooling_weight_method"]="EXPIRY ONLY"
    else:
        # Show a research blend with the old shrinkage formula if useful, but it is
        # explicitly SHADOW and can never activate the gate.
        try:k=max(1.0,float(os.getenv("ITM_CALIB_POOLING_K","100")))
        except Exception:k=100.0
        n_scope=float((scope_component or {}).get("train_samples") or (scope_component or {}).get("samples") or 0.0)
        w=n_scope/(n_scope+k) if scope_component else 0.0
        model=_blend_probability_models(global_model,scope_component if scope_component else None,w,mode,resolution,val)
        model["ready"]=False;model["status"]="SHADOW · POOLING COLLECTING";model["stage"]=model["status"]
        model["pooling_weight_method"]="SHADOW PRIOR ONLY · NOT ACTIVE"
    model["scope_samples_raw"]=int(len(scoped));model["global_samples_raw"]=int(len(ev))
    return model


def option_economic_rr(symbol: str, direction: str, spot: float, strike: float, target1: float,
                       invalidation: float, iv: float, dte: float, resolution_time: Dict[str, Any] | None,
                       half_spread_pct: float | None = None) -> Dict[str, Any]:
    """Instrument-consistent R/R for a long ATM call/put.

    Reward and risk are measured in OPTION PREMIUM, not underlying points.  Time to T1
    and invalidation comes from historical resolution distributions, so theta is part
    of the economics.  IV is intentionally held constant and the result is labelled
    theoretical; missing inputs return ready=False rather than a fabricated ratio.
    """
    try:
        from .precision_engine import black_scholes_price, market_inputs
        vals=[float(spot),float(strike),float(target1),float(invalidation),float(iv),float(dte)]
        if not all(math.isfinite(v) for v in vals) or iv<=0 or dte<=0 or spot<=0 or strike<=0:
            return {"ready":False,"reason":"faltan spot/strike/T1/invalidation/IV/DTE válidos"}
        rt=resolution_time or {}; ts=(rt.get("t1") or {}); ins=(rt.get("invalidation") or {})
        if ts.get("median") is None or ins.get("median") is None:
            return {"ready":False,"reason":"faltan tiempos históricos T1/invalidation para R/R de opciones","resolution_time_source":rt.get("source","UNAVAILABLE")}
        typ="call" if str(direction).upper()=="BUY" else "put" if str(direction).upper()=="SELL" else None
        if not typ:return {"ready":False,"reason":"dirección no válida"}
        half=(float(os.getenv("ITM_OPTION_HALF_SPREAD_PCT","2.5")) if half_spread_pct is None else float(half_spread_pct))/100.0
        mi=market_inputs(str(symbol),float(dte));rf,q=float(mi["risk_free_rate"]),float(mi["dividend_yield"])
        entry_theo=black_scholes_price(float(spot),float(strike),year_fraction(float(dte)),float(iv),typ,rf,q)
        if not math.isfinite(entry_theo) or entry_theo<=0:return {"ready":False,"reason":"prima de entrada no válida"}
        entry=entry_theo*(1+half)
        def px_at(s,mins):
            d=max(float(dte)-max(float(mins),0)/1440.0,0.0)
            theo=black_scholes_price(float(s),float(strike),year_fraction(d),float(iv),typ,rf,q)
            return float(theo*(1-half)) if math.isfinite(theo) else float("nan")
        def scenario(qname):
            tm=ts.get(qname);im=ins.get(qname)
            if tm is None or im is None:return None
            pt=px_at(target1,tm);pi=px_at(invalidation,im)
            reward=pt-entry;risk=entry-pi
            rr=reward/risk if math.isfinite(reward) and math.isfinite(risk) and reward>0 and risk>1e-9 else None
            return {"t1_minutes":float(tm),"invalidation_minutes":float(im),"premium_t1":round(pt,4) if math.isfinite(pt) else None,
                    "premium_invalidation":round(pi,4) if math.isfinite(pi) else None,"reward_premium":round(reward,4) if math.isfinite(reward) else None,
                    "model_risk_premium":round(risk,4) if math.isfinite(risk) else None,"rr":None if rr is None else round(float(rr),4)}
        scenarios={"fast":scenario("p25"),"base":scenario("median"),"slow":scenario("p75")}
        base=scenarios["base"]
        if not base or base.get("rr") is None:return {"ready":False,"reason":"R/R de prima no positivo bajo tiempos históricos","entry_premium":round(entry,4),"scenarios":scenarios}
        return {"ready":True,"instrument_mode":"options","option_type":typ,"strike":round(float(strike),4),"entry_premium":round(entry,4),
                "max_premium_at_risk":round(entry,4),"risk_unit":"MODEL_TO_INVALIDATION","rr":base["rr"],"scenarios":scenarios,
                "resolution_time_source":rt.get("source","UNKNOWN"),"iv_assumption":"CONSTANT","half_spread_pct":round(half*100,3),
                "note":"R/R teórico de prima: target e invalidación se reprician con tiempos históricos; IV constante. No son quotes históricos."}
    except Exception as exc:
        return {"ready":False,"reason":str(exc)[:180]}

def expected_value(probability: float, rr: float, cost_r: float | None = None) -> Dict[str, Any]:
    """EV in R multiples, fail-closed when execution cost is unknown.

    ``cost_r=0`` is valid only when the caller passes it explicitly. Omitting cost is
    not evidence that execution is free, so the function returns EV unavailable.
    """
    try:
        p = float(np.clip(float(probability), 0.0, 1.0)); r = float(rr)
        if cost_r is None:
            return {"ev_r": None, "breakeven_probability": None, "edge_over_breakeven": None,
                    "reason": "EXECUTION_COST_R_UNAVAILABLE"}
        c=float(cost_r)
        if not math.isfinite(r) or r <= 0 or not math.isfinite(c) or c < 0:
            return {"ev_r": None, "breakeven_probability": None, "edge_over_breakeven": None}
        ev = p * r - (1.0 - p) - c
        be = (1.0 + c) / (r + 1.0)
        return {"ev_r": round(float(ev), 4), "breakeven_probability": round(float(be), 4),
                "edge_over_breakeven": round(float(p - be), 4)}
    except Exception:
        return {"ev_r": None, "breakeven_probability": None, "edge_over_breakeven": None}


def _probability_model_path(storage: Path, symbol: str, expiry_mode: str | None = None) -> Path:
    scope=str(expiry_mode or "").strip().lower().replace(" ","_").replace("/","_")
    suffix=f"_{scope}" if scope else ""
    return routed_dir(Path(storage), "probability")/f"probability_model_{str(symbol).lower()}{suffix}.json"

def _persist_probability_model(storage: Path, symbol: str, model: Dict[str, Any], expiry_mode: str | None = None) -> None:
    try:
        _probability_model_path(storage,symbol,expiry_mode).write_text(
            json.dumps(model,ensure_ascii=False,default=str),encoding="utf-8")
    except Exception as _e:
        _obs_note('calibration:735', _e)


def _evaluation_audit_path(storage: Path, symbol: str, expiry_mode: str | None=None) -> Path:
    scope=str(expiry_mode or "ALL").strip().lower().replace(" ","_").replace("/","_")
    return routed_dir(Path(storage),"probability")/f"probability_gate_evaluations_{str(symbol).lower()}_{scope}.json"


def _audit_gate_evaluation(storage: Path, symbol: str, model: Dict[str,Any], expiry_mode: str | None=None) -> Dict[str,Any]:
    """Count distinct FINAL-OOS evaluations, not UI refreshes of the same block."""
    path=_evaluation_audit_path(storage,symbol,expiry_mode)
    signature="|".join(str(x) for x in (model.get("final_oos_sessions") or []))+"|"+str(model.get("selected_calibrator") or model.get("source_label") or "")
    state={"count":0,"last_signature":None,"history":[]}
    try:
        if path.exists():state.update(json.loads(path.read_text(encoding="utf-8")) or {})
    except Exception as _e:_obs_note("calibration:evaluation_audit_read",_e)
    if signature.strip("|") and signature!=state.get("last_signature"):
        state["count"]=int(state.get("count") or 0)+1
        state["last_signature"]=signature
        hist=list(state.get("history") or [])[-49:]
        hist.append({"at":datetime.now(timezone.utc).isoformat(),"signature":signature,"final_oos_sessions":model.get("final_oos_sessions") or [],
                     "brier_skill":model.get("brier_skill_score"),"ready":bool(model.get("ready"))})
        state["history"]=hist
        try:path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
        except Exception as _e:_obs_note("calibration:evaluation_audit_write",_e)
    return {"distinct_final_oos_evaluations":int(state.get("count") or 0),"last_signature":state.get("last_signature")}


def calibration_report(storage: Path, symbol: str, horizons: Iterable[int]=(5,10,20), min_samples: int=30,
                       probability_expiry_mode: str | None = None) -> Dict[str, Any]:
    files=_scanner_files(storage,symbol)
    if not files:
        prob={"ready":False,"status":"COLLECTING","stage":"COLLECTING","reason":"Aún no hay historial del Scanner guardado."}
        _persist_probability_model(storage,symbol,prob,probability_expiry_mode)
        return {"ready":False,"sample_size":0,"status":"COLLECTING","reason":prob["reason"],"horizons":{},"probability_model":prob}
    try:session_window=max(40,int(os.getenv("ITM_CALIB_SESSION_WINDOW","120")))
    except Exception:session_window=120
    frames=[]; input_quarantine=[]
    for p in files[-session_window:]:
        try:
            x,qmeta=_read_scanner_history_csv(p,storage,symbol)
            if qmeta.get("recovered"):
                input_quarantine.append(qmeta)
            x["timestamp"]=pd.to_datetime(x["timestamp"],errors="coerce");x=x.dropna(subset=["timestamp"]);x["session_date"]=p.stem.rsplit("_",1)[-1]
            # Never contaminate calibration with DEMO snapshots.
            if "mode" in x.columns:x=x[x["mode"].astype(str).str.upper()!="DEMO"]
            if len(x):frames.append(x)
        except Exception as _e:
            _obs_note('calibration:scanner_history_read', _e)
    if not frames:
        prob={"ready":False,"status":"COLLECTING","stage":"COLLECTING","reason":"No hay hipótesis LIVE válidas para calibración."}
        _persist_probability_model(storage,symbol,prob,probability_expiry_mode)
        return {"ready":False,"sample_size":0,"status":"COLLECTING","reason":prob["reason"],"horizons":{},"probability_model":prob,"input_quarantine":input_quarantine}
    sig=_dedupe_signals(pd.concat(frames,ignore_index=True),60);rows=[]
    for day,g in sig.groupby("session_date"):
        px,price_path_source=_price_path_for_session(storage,symbol,str(day))
        if px.empty:continue
        for _,r in g.iterrows():
            base={"timestamp":r["timestamp"],"spot":float(r.get("spot",np.nan)),"direction":r.get("direction"),"evidence_score":float(r.get("evidence_score",np.nan)),"structural_score":float(r.get("structural_score",np.nan)),"session_date":day,"price_path_source":price_path_source,
                  "edge_state":str(r.get("edge_state","UNKNOWN")),"regime":str(r.get("regime","UNKNOWN")),"expiry_mode":str(r.get("expiry_mode","UNKNOWN")),"adaptive_mode":str(r.get("adaptive_mode","UNKNOWN"))}
            for c in r.index:
                if str(c).startswith("feature_"):
                    try: base[str(c)]=float(r.get(c,np.nan))
                    except Exception: base[str(c)]=np.nan
            for h in horizons:
                ev=_eval_signal(r,px,int(h))
                if ev:rows.append({**base,"horizon":int(h),**ev})
    if not rows:
        prob={"ready":False,"status":"COLLECTING","stage":"COLLECTING","reason":"Hay hipótesis, pero aún no existe trayectoria posterior suficiente para evaluarlas."}
        _persist_probability_model(storage,symbol,prob,probability_expiry_mode)
        return {"ready":False,"sample_size":0,"status":"COLLECTING","reason":prob["reason"],"horizons":{},"probability_model":prob,"input_quarantine":input_quarantine}
    ev=pd.DataFrame(rows);out={}
    for h,g in ev.groupby("horizon"):out[str(int(h))]=_perf(g)
    h0=int(sorted(set(ev["horizon"]))[0]);smallest=ev[ev["horizon"]==h0].copy();smallest["evidence_bin"]=pd.cut(smallest["evidence_score"],bins=[0,50,60,70,80,90,100],include_lowest=True)
    bins=[]
    for b,g in smallest.groupby("evidence_bin",observed=True):
        if len(g):bins.append({"bin":str(b),**_perf(g)})
    def breakdown(col):
        arr=[]
        for name,g in smallest.groupby(col,dropna=False):arr.append({col:str(name),**_perf(g)})
        return sorted(arr,key=lambda r:r.get("samples",0),reverse=True)
    samples=int(len(smallest));sessions=sorted(str(x) for x in sig["session_date"].unique());session_count=len(sessions);status="RESEARCH READY" if samples>=min_samples else "COLLECTING"
    # Chronological holdout plus a one-session purge gap. The purge reduces leakage
    # from adjacent sessions/regime carry-over. No future session is ever used in train.
    wf={"ready":False,"train_sessions":0,"purged_sessions":0,"test_sessions":0,"test":{},"method":"chronological purged holdout"}
    if session_count>=6:
        split=max(3,int(math.floor(session_count*.65)))
        train_days=sessions[:split]; purge_days=sessions[split:split+1]; test_days=sessions[split+1:]
        test_df=smallest[smallest["session_date"].astype(str).isin(set(test_days))]
        wf={"ready":len(test_df)>0,"train_sessions":len(train_days),"purged_sessions":len(purge_days),"test_sessions":len(test_days),"test":_perf(test_df),"method":"chronological 65% train + 1 session purge gap + later-session holdout"}
    # Empirical regime multiplier suggestions for SHADOW mode only.  They are bounded
    # and never auto-activate the live scanner.
    reg_break=breakdown("regime");cal_profiles={}
    for r in reg_break:
        n=int(r.get("samples",0));exp=float(r.get("expectancy",0) or 0);pf=r.get("profit_factor")
        strength=1.0
        if n>=max(10,min_samples//3):
            strength=float(np.clip(1.0+0.08*np.tanh(exp/max(float(smallest["mfe"].median() or 1),1e-6))+(0.04 if pf is not None and pf>1.2 else -0.04 if pf is not None and pf<0.8 else 0),.88,1.12))
        cal_profiles[str(r["regime"])]=round(strength,3)

    # Feature diagnostics are the bridge from hand-set heuristics to measured weights.
    # They remain RESEARCH/SHADOW: no coefficient below can change LIVE Scanner weights automatically.
    feature_cols=[c for c in smallest.columns if str(c).startswith("feature_")]
    feature_diag=[]
    target=numeric_column(smallest,"resolved_move",float("nan"))
    for c in feature_cols:
        x=pd.to_numeric(smallest[c],errors="coerce");m=x.notna()&target.notna()
        if int(m.sum())<8:continue
        # Una feature constante en la ventana no está descorrelacionada del
        # resultado: simplemente no se puede medir. La guarda evita el aviso y,
        # sobre todo, evita que un NaN llegue al diagnóstico como si fuera un 0.
        _c=safe_correlation(x[m].to_numpy(),target[m].to_numpy(),method="spearman",min_samples=8)
        corr=_c.value if _c.usable else float("nan")
        won=target[m]>0
        feature_diag.append({"feature":c.replace("feature_",""),"samples":int(m.sum()),"spearman_to_resolved_move":None if not math.isfinite(corr) else round(corr,3),
                             "avg_when_positive":round(float(x[m][won].mean()),3) if won.any() else None,
                             "avg_when_nonpositive":round(float(x[m][~won].mean()),3) if (~won).any() else None})
    feature_diag=sorted(feature_diag,key=lambda z:abs(float(z.get("spearman_to_resolved_move") or 0)),reverse=True)

    weight_fit={"ready":False,"status":"COLLECTING","method":"ridge directional relevance · SHADOW only","suggested_weights":{},"test_direction_accuracy":None}
    if samples>=80 and session_count>=8 and len(feature_cols)>=5:
        try:
            split=max(5,int(math.floor(session_count*.65)));train_days=set(sessions[:split]);purge_days=set(sessions[split:split+1]);test_days=set(sessions[split+1:])
            cols=[c for c in feature_cols if pd.to_numeric(smallest[c],errors="coerce").notna().sum()>=max(30,int(samples*.5))]
            if len(cols)>=5 and test_days:
                tr=smallest[smallest["session_date"].astype(str).isin(train_days)].copy();te=smallest[smallest["session_date"].astype(str).isin(test_days)].copy()
                Xtr=tr[cols].apply(pd.to_numeric,errors="coerce");Xte=te[cols].apply(pd.to_numeric,errors="coerce")
                med=Xtr.median();Xtr=Xtr.fillna(med);Xte=Xte.fillna(med);mu=Xtr.mean();sd=Xtr.std(ddof=0).replace(0,1)
                A=((Xtr-mu)/sd).to_numpy(float);B=((Xte-mu)/sd).to_numpy(float);y=(pd.to_numeric(tr["resolved_move"],errors="coerce").fillna(0)>0).astype(float).to_numpy();yt=(pd.to_numeric(te["resolved_move"],errors="coerce").fillna(0)>0).astype(float).to_numpy()
                lam=3.0;beta=np.linalg.solve(A.T@A+lam*np.eye(A.shape[1]),A.T@(y-y.mean()))
                pred=B@beta+y.mean();acc=float(((pred>=0.5)==(yt>=0.5)).mean()*100) if len(yt) else float("nan")
                pos=np.maximum(beta,0);den=float(pos.sum())
                suggested={c.replace("feature_",""):round(float(w/den*100),2) for c,w in zip(cols,pos) if den>0 and w>0}
                weight_fit={"ready":True,"status":"SHADOW FIT READY","method":"ridge directional relevance on chronological train sessions; evaluated on later sessions; not a probability model","train_sessions":len(train_days),"purged_sessions":len(purge_days),"test_sessions":len(test_days),"test_samples":int(len(te)),"test_direction_accuracy":None if not math.isfinite(acc) else round(acc,1),"suggested_weights":suggested}
        except Exception as exc:
            weight_fit={"ready":False,"status":"FIT ERROR","reason":str(exc)[:180],"method":"ridge directional relevance · SHADOW only","suggested_weights":{}}

    assumptions=_cost_assumptions()
    cost_aware=_cost_adjust(smallest,assumptions)
    # v1.14.4: hierarchical Global -> Expiry partial pooling.  A sparse 0DTE/WEEK
    # sample backs off to the strict global model instead of staying inert for months.
    # The scope progressively gains weight as n_scope grows, while all promotion rules
    # remain out-of-sample and purged.
    avail_h=sorted(int(x) for x in set(pd.to_numeric(ev["horizon"],errors="coerce").dropna()))
    try: requested_h=int(os.getenv("ITM_EV_GATE_HORIZON_MIN","10"))
    except Exception: requested_h=10
    h0_int=min(avail_h,key=lambda x:abs(x-requested_h)) if avail_h else 10
    prob=hierarchical_probability_calibration(ev,probability_expiry_mode,h0_int);prob["gate_horizon_minutes"]=h0_int
    prob["evaluation_audit"]=_audit_gate_evaluation(storage,symbol,prob,probability_expiry_mode)
    # Persist every stage so a stale calibrated model can never survive a degradation.
    _persist_probability_model(storage,symbol,prob,probability_expiry_mode)
    # Also keep the strict global backoff model available under the no-scope path.
    if probability_expiry_mode:
        global_sessions=sorted(str(x) for x in ev["session_date"].dropna().unique())
        global_prob=probability_calibration(ev,global_sessions,h0_int)
        global_prob["expiry_mode"]="ALL"
        _persist_probability_model(storage,symbol,global_prob,None)
    ev_examples=[]
    if prob.get("ready") or prob.get("base_rate") is not None:
        pbase=float(prob.get("base_rate") or 0.5)
        for rr_demo in (0.5,1.0,2.0,3.0):
            if assumptions.get("cost_r") is None:
                ev_examples.append({"rr":rr_demo,"assumed_probability":round(pbase,4),"ev_r":None,"breakeven_probability":None,
                                    "edge_over_breakeven":None,"reason":"EXECUTION_COST_R_UNAVAILABLE"})
            else:
                ev_examples.append({"rr":rr_demo,"assumed_probability":round(pbase,4),
                                    **expected_value(pbase,rr_demo,float(assumptions["cost_r"]))})
    return {"ready":samples>=min_samples,"sample_size":samples,"status":status,"minimum_recommended":min_samples,"horizons":out,"evidence_bins":bins,
            "edge_breakdown":breakdown("edge_state"),"regime_breakdown":reg_break,"expiry_breakdown":breakdown("expiry_mode"),"sessions":session_count,
            "probability_model":prob,"expected_value_examples":ev_examples,
            "instrument_mode":_instrument_mode(),"calibration_session_window":session_window,
            "walk_forward":wf,"walk_forward_readiness":"READY" if wf.get("ready") and samples>=min_samples else "COLLECTING SESSIONS","execution_assumptions":assumptions,"cost_aware":cost_aware,
            "calibrated_regime_multipliers_shadow":cal_profiles,"feature_diagnostics":feature_diag,"weight_fit":weight_fit,
            "input_quarantine":input_quarantine,"method_note":"Resultados LIVE sobre snapshots guardados. v1.14.4 usa calibración jerárquica Global→Expiry con partial pooling; Evidence sigue siendo evidencia interna. P&L de opciones usa riesgo modelado hasta invalidación y IV constante, no ejecución histórica observada. Ajustes/regresión de features permanecen SHADOW hasta validación suficiente.","dedupe_seconds":60}

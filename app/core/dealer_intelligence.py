"""Dealer Intelligence & Hedge Impact Engine - ITM QUANT v1.14.

This module deliberately distinguishes observed data from inferred state.
OPRA trades/quotes are observed. Dealer inventory, opening/closing status and
participant identity are NOT directly observed by the current feeds, so every
output below is labelled as a model estimate and carries an uncertainty band.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd
from app.persistence import routed_dir
from .dealer_microstructure import attach_causal_underlying, enrich_option_packages, microstructure_quality, package_summary
from .hedge_confirmation import hedge_flow_confirmation
from .distributed_hot_state import HOT_STATE_REDIS
from .frame_guards import numeric_column
from .expiry_clock import year_fraction_array
from .contract_spec import multiplier_series, row_multiplier


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x=float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _clip01(v: float) -> float:
    return float(np.clip(float(v),0.0,1.0))


def opening_features(row: Dict[str, Any] | pd.Series) -> Dict[str, float]:
    """Feature vector shared by the heuristic prior and the fitted model.

    Keeping one definition means the calibration in OpeningCalibrationStore fits the
    exact same inputs the live path uses. Bounded transforms keep every feature in a
    comparable range so a linear fit is not dominated by one heavy tail.
    """
    oi=max(_f(row.get("open_interest"),0.0),0.0)
    vol=max(_f(row.get("daily_volume",row.get("volume",0)),0.0),0.0)
    size=max(_f(row.get("contracts",row.get("size",0)),0.0),0.0)
    conf=_clip01(_f(row.get("aggressor_confidence"),0.0))
    size_oi=size/max(oi,1.0); vol_oi=vol/max(oi,1.0)
    return {
        "size_oi": float(np.tanh(size_oi*25.0)),
        "low_turnover": float(np.tanh(max(.35-vol_oi,0.0)*2.0)),
        "high_turnover": float(np.tanh(max(vol_oi-.80,0.0))),
        "confidence": float(conf-.5),
        "_size_oi_raw": float(size_oi),
        "_vol_oi_raw": float(vol_oi),
    }


# Hand-set prior. These coefficients were never measured against anything; they are
# the fallback used until OpeningCalibrationStore has fitted replacements.
HEURISTIC_OPENING_COEFFS = {"intercept": .50, "size_oi": .16, "low_turnover": .08,
                            "high_turnover": -.12, "confidence": .05}


def opening_closing_likelihood(row: Dict[str, Any] | pd.Series,
                               model: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Opening-vs-closing likelihood, never presented as ground truth.

    v1.15: the coefficients are now a parameter rather than a constant. When a fitted
    model is supplied (see OpeningCalibrationStore, which regresses these same features
    on the NEXT session's realised open-interest change) the output is a measured
    estimate and `source` says FITTED. Otherwise the hand-set prior is used unchanged
    and `source` says HEURISTIC PRIOR, so the two are never confused downstream.
    """
    f=opening_features(row)
    coeffs=dict(HEURISTIC_OPENING_COEFFS); source="HEURISTIC PRIOR"; lo,hi=.20,.80
    if isinstance(model,dict) and model.get("ready"):
        coeffs=dict(model.get("coefficients") or coeffs); source="FITTED"
        lo,hi=float(model.get("clip_low",.05)),float(model.get("clip_high",.95))
    p=float(coeffs.get("intercept",.50))
    for k in ("size_oi","low_turnover","high_turnover","confidence"):
        p+=float(coeffs.get(k,0.0))*f[k]
    p=float(np.clip(p,lo,hi))
    close=1.0-p
    certainty=abs(p-.5)*2.0
    label="OPENING-LIKELY" if p>=.62 else "CLOSING-LIKELY" if p<=.38 else "UNCERTAIN"
    pct=round(p*100,1); close_pct=round(close*100,1)
    fitted=source == "FITTED"
    return {
        # Compatibility fields remain because internal hedge math consumes a 0-100
        # likelihood.  The provenance fields below prevent a hand-set prior from being
        # displayed or interpreted as a calibrated market probability.
        "opening_probability":pct,
        "closing_probability":close_pct,
        "opening_score":pct if not fitted else None,
        "opening_metric_type":"CALIBRATED_PROBABILITY" if fitted else "HEURISTIC_SCORE",
        "opening_probability_calibrated":bool(fitted),
        "certainty":round(certainty*100,1),
        "label":label,
        "size_oi_pct":round(f["_size_oi_raw"]*100,3),
        "session_vol_oi":round(f["_vol_oi_raw"],3),
        "model_source":source,
        "note":"Inferencia SHADOW. OPRA/OI no identifican opening/closing con certeza; "
               "el modelo FITTED se valida contra el cambio real de OI del día siguiente.",
    }


def _option_side(aggressor: Any) -> int:
    a=str(aggressor or "").upper()
    return 1 if a=="BUY" else -1 if a=="SELL" else 0


def _model_delta(row: Dict[str, Any] | pd.Series) -> float:
    for k in ("model_delta","fallback_delta","provider_delta","delta"):
        x=_f(row.get(k),float("nan"))
        if math.isfinite(x):return x
    return 0.0


def _model_gamma(row: Dict[str, Any] | pd.Series) -> float:
    for k in ("model_gamma","fallback_gamma","provider_gamma","gamma"):
        x=_f(row.get(k),float("nan"))
        if math.isfinite(x):return x
    return 0.0


def event_hedge_impact(row: Dict[str, Any] | pd.Series, counterparty_share: float = .70,
                       model: Dict[str, Any] | None = None, *, symbol: Any = None) -> Dict[str, Any]:
    """Estimate the underlying hedge response if a dealer is the option counterparty.

    Positive hedge_notional means estimated underlying BUY requirement; negative means
    SELL requirement. The output is an impact scenario, not observed dealer trading.
    """
    side=_option_side(row.get("aggressor"))
    size=max(_f(row.get("contracts"),0.0),0.0)
    spot=max(_f(row.get("underlying_price"),0.0),0.0)
    delta=_model_delta(row);gamma=_model_gamma(row)
    oc=opening_closing_likelihood(row,model)
    open_p=oc["opening_probability"]/100.0
    cert=oc["certainty"]/100.0
    # Only the portion that is more likely opening than closing creates fresh synthetic inventory.
    fresh=max(0.0,(open_p-.5)*2.0)
    cp=float(np.clip(counterparty_share,0.0,1.0))
    dealer_option_change=-side*size*fresh*cp
    # v1.42: el tamano economico sale del contrato. Un future option Dow no mueve
    # 100 unidades de subyacente por contrato, y un contrato ajustado tampoco.
    mult=row_multiplier(row,symbol)
    dealer_delta_change=dealer_option_change*delta*mult
    hedge_shares=-dealer_delta_change
    hedge_notional=hedge_shares*spot
    # Dollar gamma per 1% move for the inferred fresh dealer position.
    dealer_gex=dealer_option_change*gamma*(spot**2)*mult*.01
    confidence=_clip01((_f(row.get("aggressor_confidence"),0.0)*.55+cert*.30+.15)*cp)
    return {
        **oc,
        "dealer_counterparty_share":round(cp*100,1),
        "fresh_inventory_factor":round(fresh,4),
        "estimated_dealer_contract_change":round(dealer_option_change,3),
        "estimated_dealer_delta_change_shares":round(dealer_delta_change,2),
        "estimated_hedge_shares":round(hedge_shares,2),
        "estimated_hedge_notional":round(hedge_notional,2),
        "estimated_dealer_gex":round(dealer_gex,2),
        "confidence":round(confidence*100,1),
        "model":"DEALER-COUNTERPARTY SCENARIO",
    }


class OpeningCalibrationStore:
    """Measure the opening/closing model against realised open-interest changes.

    THE IDEA. Whether a particular trade opened or closed a position is not observable
    directly from standard OPRA prints. One session later, the *net change* in open
    interest provides delayed supervision about aggregate contract creation/destruction,
    so for a given contract over one session

        dOI = OI(t) - OI(t-1)

    and, under the usual symmetric two-sided approximation where a contract is created
    only when both counterparties open and destroyed only when both close,

        dOI ~ V * (2*o - 1)   =>   o ~ (dOI/V + 1) / 2

    with V the session volume and o the opening share of that volume. That gives a
    noisy supervision proxy (not ground truth). Regressing the same features the live
    path uses on that target turns a hand-set heuristic into a measured model.

    The approximation is crude for contracts where one side systematically opens while
    the other closes; that is why `o` is clipped, contracts with thin volume are
    dropped, and the fit reports an out-of-sample R2 on later sessions so a bad fit is
    visible rather than silently adopted.
    """

    FEATURES = ("size_oi", "low_turnover", "high_turnover", "confidence")

    def __init__(self, path: Path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self._init()

    def _conn(self):
        c=sqlite3.connect(self.path,timeout=5); c.execute("PRAGMA journal_mode=WAL"); return c

    def _init(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS oi_sessions(
                symbol TEXT NOT NULL, session_date TEXT NOT NULL, contract_symbol TEXT NOT NULL,
                open_interest REAL, volume REAL,
                f_size_oi REAL, f_low_turnover REAL, f_high_turnover REAL, f_confidence REAL,
                feature_weight REAL, updated_at TEXT,
                PRIMARY KEY(symbol, session_date, contract_symbol))""")
            c.execute("""CREATE TABLE IF NOT EXISTS opening_model(
                symbol TEXT PRIMARY KEY, fitted_at TEXT, payload_json TEXT)""")

    def record_session(self, symbol: str, chain: pd.DataFrame, events: pd.DataFrame | None,
                       session_date: str | None = None) -> Dict[str, Any]:
        """Store end-of-cycle OI/volume per contract plus volume-weighted live features."""
        if chain is None or chain.empty or "contract_symbol" not in chain.columns:
            return {"recorded":0,"reason":"sin cadena"}
        sym=str(symbol).upper()
        day=str(session_date or pd.Timestamp(chain["timestamp"].max()).date()) if "timestamp" in chain.columns else str(session_date or datetime.now(timezone.utc).date())
        x=chain.copy()
        x["contract_symbol"]=x["contract_symbol"].astype(str)
        x["open_interest"]=numeric_column(x,"open_interest",0).clip(lower=0)
        x["volume"]=numeric_column(x,"volume",0).clip(lower=0)
        latest=x.drop_duplicates("contract_symbol",keep="last").set_index("contract_symbol")

        # Volume-weighted mean of the live features per contract, from the trades seen today.
        feats: Dict[str, Dict[str, float]] = {}
        if events is not None and not events.empty and "contract_symbol" in events.columns:
            for _,r in events.iterrows():
                cs=str(r.get("contract_symbol",""))
                if not cs: continue
                w=max(_f(r.get("contracts"),0.0),0.0)
                if w<=0: continue
                f=opening_features(r)
                acc=feats.setdefault(cs,{k:0.0 for k in self.FEATURES}|{"_w":0.0})
                for k in self.FEATURES: acc[k]+=f[k]*w
                acc["_w"]+=w

        rows=[]; now=datetime.now(timezone.utc).isoformat()
        for cs,acc in feats.items():
            if cs not in latest.index: continue
            w=acc["_w"]
            if w<=0: continue
            rows.append((sym,day,cs,float(latest.at[cs,"open_interest"]),float(latest.at[cs,"volume"]),
                         acc["size_oi"]/w,acc["low_turnover"]/w,acc["high_turnover"]/w,acc["confidence"]/w,w,now))
        if not rows: return {"recorded":0,"reason":"sin eventos con features"}
        with self._conn() as c:
            c.executemany("""INSERT INTO oi_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,session_date,contract_symbol) DO UPDATE SET
                open_interest=excluded.open_interest, volume=excluded.volume,
                f_size_oi=excluded.f_size_oi, f_low_turnover=excluded.f_low_turnover,
                f_high_turnover=excluded.f_high_turnover, f_confidence=excluded.f_confidence,
                feature_weight=excluded.feature_weight, updated_at=excluded.updated_at""",rows)
        return {"recorded":len(rows),"session_date":day}

    def _training_frame(self, symbol: str, min_volume: float) -> pd.DataFrame:
        with self._conn() as c:
            df=pd.read_sql_query("SELECT * FROM oi_sessions WHERE symbol=? ORDER BY session_date,contract_symbol",
                                 c,params=(str(symbol).upper(),))
        if df.empty or df["session_date"].nunique()<2: return pd.DataFrame()
        df=df.sort_values(["contract_symbol","session_date"])
        df["prev_oi"]=df.groupby("contract_symbol")["open_interest"].shift(1)
        df["prev_date"]=df.groupby("contract_symbol")["session_date"].shift(1)
        df=df.dropna(subset=["prev_oi","prev_date"])
        if df.empty: return df
        df["d_oi"]=df["open_interest"]-df["prev_oi"]
        df=df[df["volume"]>=float(min_volume)]
        if df.empty: return df
        # Realised opening share implied by the OI identity, clipped to a valid range.
        df["realised_opening"]=((df["d_oi"]/df["volume"])+1.0)/2.0
        df["realised_opening"]=df["realised_opening"].clip(0.0,1.0)
        return df

    def fit(self, symbol: str, min_contracts: int = 200, min_sessions: int = 5,
            min_volume: float = 25.0, ridge: float = 1.0) -> Dict[str, Any]:
        """Ridge fit of the live features on the realised opening share.

        Split is chronological by session with a one-session purge gap, mirroring the
        Calibration Lab, so the reported R2 is on sessions the fit never saw.
        """
        base={"ready":False,"status":"COLLECTING","features":list(self.FEATURES),
              "method":"ridge on realised OI-implied opening share; chronological purged holdout"}
        df=self._training_frame(symbol,min_volume)
        if df.empty: return {**base,"reason":"sin pares de sesiones consecutivas con volumen suficiente"}
        sessions=sorted(df["session_date"].astype(str).unique())
        if len(sessions)<min_sessions or len(df)<min_contracts:
            return {**base,"samples":int(len(df)),"sessions":len(sessions),
                    "reason":f"se requieren >={min_contracts} contrato-sesiones y >={min_sessions} sesiones"}
        split=max(2,int(math.floor(len(sessions)*.65)))
        train_days=set(sessions[:split]); test_days=set(sessions[split+1:])
        tr=df[df["session_date"].astype(str).isin(train_days)]
        te=df[df["session_date"].astype(str).isin(test_days)]
        if tr.empty or te.empty: return {**base,"reason":"holdout vacío tras el purge gap"}
        cols=[f"f_{k}" for k in self.FEATURES]
        Xtr=tr[cols].to_numpy(float); ytr=tr["realised_opening"].to_numpy(float)
        Xte=te[cols].to_numpy(float); yte=te["realised_opening"].to_numpy(float)
        wtr=np.maximum(tr["volume"].to_numpy(float),1.0)
        mu=Xtr.mean(axis=0); ybar=float(np.average(ytr,weights=wtr))
        A=Xtr-mu; W=np.diag(wtr/wtr.mean())
        try:
            beta=np.linalg.solve(A.T@W@A+float(ridge)*np.eye(A.shape[1]),A.T@W@(ytr-ybar))
        except Exception as exc:
            return {**base,"status":"FIT ERROR","reason":str(exc)[:160]}
        pred=(Xte-mu)@beta+ybar
        ss_res=float(np.sum((yte-pred)**2)); ss_tot=float(np.sum((yte-yte.mean())**2))
        r2=1.0-ss_res/ss_tot if ss_tot>1e-12 else float("nan")
        base_mae=float(np.mean(np.abs(yte-ybar))); model_mae=float(np.mean(np.abs(yte-pred)))
        coeffs={"intercept":round(float(ybar-float(mu@beta)),6)}
        coeffs.update({k:round(float(b),6) for k,b in zip(self.FEATURES,beta)})
        out={**base,"ready":bool(np.isfinite(r2)),"status":"FITTED",
             "coefficients":coeffs,"train_samples":int(len(tr)),"test_samples":int(len(te)),
             "train_sessions":len(train_days),"purged_sessions":1,"test_sessions":len(test_days),
             "test_r2":None if not np.isfinite(r2) else round(float(r2),4),
             "test_mae":round(model_mae,4),"baseline_mae":round(base_mae,4),
             "beats_constant_baseline":bool(model_mae<base_mae),
             "realised_opening_mean":round(float(np.average(ytr,weights=wtr)),4),
             "clip_low":0.05,"clip_high":0.95,"fitted_at":datetime.now(timezone.utc).isoformat(),
             "note":"Si beats_constant_baseline es False, el modelo NO aporta sobre predecir la media; "
                    "en ese caso conviene seguir usando el prior heurístico."}
        with self._conn() as c:
            c.execute("INSERT INTO opening_model VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                      "fitted_at=excluded.fitted_at,payload_json=excluded.payload_json",
                      (str(symbol).upper(),out["fitted_at"],json.dumps(out,default=str)))
        return out

    def load_model(self, symbol: str) -> Dict[str, Any] | None:
        """Return the stored fit only if it is enabled AND actually beat the baseline."""
        if os.getenv("ITM_OPENING_CALIBRATION","0")!="1": return None
        try:
            with self._conn() as c:
                row=c.execute("SELECT payload_json FROM opening_model WHERE symbol=?",(str(symbol).upper(),)).fetchone()
            if not row: return None
            m=json.loads(row[0])
            return m if m.get("ready") and m.get("beats_constant_baseline") else None
        except Exception:
            return None


class SyntheticInventoryBook:
    """Persistent synthetic dealer inventory with idempotent event ingestion.

    SQLite is used intentionally: it is local, transactional and available in the
    Python standard library. It stores only model estimates and event fingerprints.
    """
    def __init__(self, path: Path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self._init()

    def _conn(self):
        c=sqlite3.connect(self.path,timeout=5)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS dealer_inventory(
                symbol TEXT NOT NULL, contract_symbol TEXT NOT NULL, strike REAL,
                expiration_date TEXT, option_type TEXT, dealer_contracts REAL NOT NULL DEFAULT 0,
                last_delta REAL, last_gamma REAL, last_vanna REAL, last_charm REAL,
                last_spot REAL, confidence REAL, updated_at TEXT,
                PRIMARY KEY(symbol, contract_symbol))""")
            c.execute("""CREATE TABLE IF NOT EXISTS dealer_events(
                event_id TEXT PRIMARY KEY, symbol TEXT, contract_symbol TEXT, timestamp TEXT,
                contracts REAL, aggressor TEXT, open_probability REAL, inventory_change REAL,
                hedge_notional REAL, confidence REAL, dealer_delta_change_shares REAL,
                package_id TEXT, package_type TEXT, classification_method TEXT, nbbo_synced INTEGER)""")
            cols={r[1] for r in c.execute("PRAGMA table_info(dealer_events)").fetchall()}
            for name,typ in (("dealer_delta_change_shares","REAL"),("package_id","TEXT"),("package_type","TEXT"),("classification_method","TEXT"),("nbbo_synced","INTEGER")):
                if name not in cols:
                    c.execute(f"ALTER TABLE dealer_events ADD COLUMN {name} {typ}")

    @staticmethod
    def event_id(row: Dict[str, Any] | pd.Series) -> str:
        raw="|".join(str(row.get(k,"")) for k in ("underlying_symbol","contract_symbol","timestamp","trade_price","contracts","exchange"))
        return hashlib.sha1(raw.encode("utf-8",errors="ignore")).hexdigest()

    def ingest(self, events: pd.DataFrame, counterparty_share: float=.70,
               model: Dict[str, Any] | None=None) -> Dict[str, Any]:
        if events is None or events.empty:return {"ingested":0,"skipped":0}
        ing=skip=0
        with self._conn() as c:
            for _,r in events.sort_values("timestamp").iterrows():
                event_id=self.event_id(r)
                if c.execute("SELECT 1 FROM dealer_events WHERE event_id=?",(event_id,)).fetchone():skip+=1;continue
                sym=str(r.get("underlying_symbol","DIA")).upper();cs=str(r.get("contract_symbol",""))
                if not cs:skip+=1;continue
                impact=event_hedge_impact(r,counterparty_share,model)
                cur=c.execute("SELECT dealer_contracts FROM dealer_inventory WHERE symbol=? AND contract_symbol=?",(sym,cs)).fetchone()
                current=float(cur[0]) if cur else 0.0
                open_p=impact["opening_probability"]/100.0; close_p=1-open_p; side=_option_side(r.get("aggressor"));qty=max(_f(r.get("contracts"),0),0)
                change=0.0
                if side and open_p>=.55:
                    change=-side*qty*((open_p-.5)*2.0)*float(np.clip(counterparty_share,0,1))
                elif close_p>=.55 and abs(current)>1e-9:
                    # When closing is more likely, reduce existing synthetic inventory rather
                    # than inventing the unknown original counterparty direction.
                    reduction=min(abs(current),qty*((close_p-.5)*2.0)*float(np.clip(counterparty_share,0,1)))
                    change=-math.copysign(reduction,current)
                new=current+change
                delta=_model_delta(r);gamma=_model_gamma(r);vanna=_f(r.get("calc_vanna"),0);charm=_f(r.get("calc_charm"),0);spot=_f(r.get("underlying_price"),0)
                c.execute("""INSERT INTO dealer_inventory(symbol,contract_symbol,strike,expiration_date,option_type,dealer_contracts,last_delta,last_gamma,last_vanna,last_charm,last_spot,confidence,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,contract_symbol) DO UPDATE SET
                    strike=excluded.strike,expiration_date=excluded.expiration_date,option_type=excluded.option_type,dealer_contracts=excluded.dealer_contracts,
                    last_delta=excluded.last_delta,last_gamma=excluded.last_gamma,last_vanna=excluded.last_vanna,last_charm=excluded.last_charm,last_spot=excluded.last_spot,
                    confidence=excluded.confidence,updated_at=excluded.updated_at""",
                    (sym,cs,_f(r.get("strike")),str(r.get("expiration_date","")),str(r.get("option_type","")),new,delta,gamma,vanna,charm,spot,impact["confidence"],str(r.get("timestamp",datetime.now(timezone.utc).isoformat()))))
                c.execute("""INSERT INTO dealer_events(
                    event_id,symbol,contract_symbol,timestamp,contracts,aggressor,open_probability,inventory_change,
                    hedge_notional,confidence,dealer_delta_change_shares,package_id,package_type,classification_method,nbbo_synced
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id,sym,cs,str(r.get("timestamp","")),qty,str(r.get("aggressor","")),impact["opening_probability"],change,
                     impact["estimated_hedge_notional"],impact["confidence"],impact["estimated_dealer_delta_change_shares"],
                     str(r.get("package_id") or ""),str(r.get("package_type") or "SINGLE"),str(r.get("classification_method") or ""),
                     1 if bool(r.get("nbbo_synced")) else 0))
                ing+=1
        return {"ingested":ing,"skipped":skip}

    def recent_events(self, symbol: str, minutes: int = 15) -> pd.DataFrame:
        try:
            with self._conn() as c:
                df=pd.read_sql_query("SELECT * FROM dealer_events WHERE symbol=?",c,params=(str(symbol).upper(),))
            if df.empty:return df
            df["timestamp"]=pd.to_datetime(df["timestamp"],errors="coerce");df=df.dropna(subset=["timestamp"])
            if df.empty:return df
            end=df["timestamp"].max();return df[df["timestamp"]>=end-pd.Timedelta(minutes=int(minutes))].sort_values("timestamp").reset_index(drop=True)
        except Exception as exc:
            _obs_note("dealer_intelligence:recent_events_query_failed", exc, severity="CRITICAL_DATA")
            return pd.DataFrame()

    def reconcile(self, chain: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        """Reconcile synthetic inventory against the latest structural chain.

        Inventory is capped by reported OI and contracts no longer present/expired are
        removed. This does not infer the true dealer position; it prevents the synthetic
        book from drifting beyond the observable structural universe.
        """
        if chain is None or chain.empty or "contract_symbol" not in chain.columns:
            return {"reconciled":0,"removed":0}
        x=chain.copy();x["contract_symbol"]=x["contract_symbol"].astype(str);x["open_interest"]=numeric_column(x,"open_interest",0).clip(lower=0)
        oi={str(r.contract_symbol):float(r.open_interest) for r in x[["contract_symbol","open_interest"]].drop_duplicates("contract_symbol").itertuples()}
        kept=set(oi);reconciled=removed=0
        with self._conn() as c:
            rows=c.execute("SELECT contract_symbol,dealer_contracts FROM dealer_inventory WHERE symbol=?",(str(symbol).upper(),)).fetchall()
            for cs,qty in rows:
                if cs not in kept:
                    c.execute("DELETE FROM dealer_inventory WHERE symbol=? AND contract_symbol=?",(str(symbol).upper(),cs));removed+=1;continue
                cap=max(oi.get(cs,0.0)*1.25,0.0)
                new=float(np.clip(float(qty),-cap,cap)) if cap>0 else 0.0
                if abs(new-float(qty))>1e-9:
                    c.execute("UPDATE dealer_inventory SET dealer_contracts=? WHERE symbol=? AND contract_symbol=?",(new,str(symbol).upper(),cs));reconciled+=1
        return {"reconciled":reconciled,"removed":removed}

    def snapshot(self, symbol: str, spot: float | None=None, chain: pd.DataFrame | None=None) -> pd.DataFrame:
        """Inventory valued with CURRENT greeks, not the greeks stored at trade time.

        v1.15 FIX. The previous version multiplied `last_gamma` - captured at the spot
        of the trade that created the position, possibly half an hour ago - by the LIVE
        spot squared. That mixes two different market states inside one number. For an
        ATM 0DTE contract a 1% spot move makes the stored gamma about 4.5x the true one,
        so the reported dealer GEX described nothing.

        When the current chain is supplied we re-price delta/gamma/vanna/charm at the
        live spot and the remaining DTE, matching contracts by contract_symbol. The
        stored values survive only as `stored_*` diagnostics and as a fallback for
        contracts missing from the chain.
        """
        with self._conn() as c:
            df=pd.read_sql_query("SELECT * FROM dealer_inventory WHERE symbol=?",c,params=(str(symbol).upper(),))
        if df.empty:return df
        sp=float(spot) if spot is not None and math.isfinite(float(spot)) else pd.to_numeric(df["last_spot"],errors="coerce").median()
        sp=max(float(sp or 0),1e-9)
        qty=pd.to_numeric(df["dealer_contracts"],errors="coerce").fillna(0)
        for src,dst in (("last_delta","stored_delta"),("last_gamma","stored_gamma"),
                        ("last_vanna","stored_vanna"),("last_charm","stored_charm")):
            df[dst]=pd.to_numeric(df[src],errors="coerce").fillna(0.0)
        delta=df["stored_delta"].copy(); gamma=df["stored_gamma"].copy()
        vanna=df["stored_vanna"].copy(); charm=df["stored_charm"].copy()
        df["greeks_valuation"]="STORED AT TRADE TIME"
        df["greeks_repriced_pct"]=0.0

        if isinstance(chain,pd.DataFrame) and not chain.empty and "contract_symbol" in chain.columns:
            try:
                from .greeks_service import greeks_vector as _greeks_vector, model_inputs_vector
                live=chain.copy()
                live["contract_symbol"]=live["contract_symbol"].astype(str)
                live=live.drop_duplicates("contract_symbol",keep="last").set_index("contract_symbol")
                idx=df["contract_symbol"].astype(str)
                iv=pd.to_numeric(idx.map(live.get("iv",pd.Series(dtype=float))),errors="coerce")
                dte=pd.to_numeric(idx.map(live.get("dte",pd.Series(dtype=float))),errors="coerce")
                strike=pd.to_numeric(df["strike"],errors="coerce")
                ok=(iv.notna()&(iv>0)&dte.notna()&strike.notna()).to_numpy()
                if ok.any():
                    K=strike.to_numpy(float)[ok]
                    ivv=iv.to_numpy(float)[ok]
                    dtev=np.maximum(dte.to_numpy(float)[ok],0.0)
                    is_call=df["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()[ok]
                    r_a,q_a=model_inputs_vector(str(symbol).upper(),dtev)
                    g=_greeks_vector(symbol,sp,K,year_fraction_array(dtev),ivv,is_call,r_a,q_a)
                    for series,key in ((delta,"delta"),(gamma,"gamma"),(vanna,"vanna"),(charm,"charm")):
                        series.iloc[np.where(ok)[0]]=g[key]
                    df.loc[ok,"greeks_valuation"]="REPRICED AT LIVE SPOT"
                    df["greeks_repriced_pct"]=round(100.0*float(ok.mean()),1)
            except Exception as _e:
                _obs_note('dealer_intelligence:494', _e)

        df["model_delta"]=delta; df["model_gamma"]=gamma
        mult=multiplier_series(df,symbol)
        df["contract_multiplier"]=mult
        df["dealer_delta_shares"]=qty*delta*mult
        df["dealer_gex"]=qty*gamma*(sp**2)*mult*.01
        df["dealer_vanna_exposure"]=qty*vanna*mult
        df["dealer_charm_exposure"]=qty*charm*mult
        df["hedge_to_neutral_shares"]=-df["dealer_delta_shares"]
        df["hedge_to_neutral_notional"]=df["hedge_to_neutral_shares"]*sp
        # Hedging responds to CHANGES in delta, not to the level. The level below is the
        # cumulative position; treat it as inventory, not as a pending order flow.
        df["valuation_spot"]=sp
        return df


def _hedge_rows(events: pd.DataFrame, model: Dict[str, Any] | None = None) -> pd.DataFrame:
    """Per-event hedge estimate at counterparty_share = 1, computed once, vectorised.

    Everything downstream scales linearly in counterparty share (see hedge_pressure),
    so the unit-share pass is the only expensive computation that is ever needed.
    """
    e=events.copy()
    e["timestamp"]=pd.to_datetime(e.get("timestamp"),errors="coerce")
    e=e.dropna(subset=["timestamp"]).sort_values("timestamp")
    if e.empty: return pd.DataFrame()

    def col(*names, default=0.0):
        """Always return a Series, even when the column is absent from the frame."""
        for nm in names:
            if nm in e.columns:
                return pd.to_numeric(e[nm],errors="coerce").fillna(default)
        return pd.Series(float(default),index=e.index,dtype=float)

    oi=col("open_interest").clip(lower=0)
    vol=col("daily_volume","volume").clip(lower=0)
    size=col("contracts","size").clip(lower=0)
    conf=col("aggressor_confidence").clip(0,1)
    spot=col("underlying_price").clip(lower=0)

    size_oi=size/oi.clip(lower=1.0); vol_oi=vol/oi.clip(lower=1.0)
    f={"size_oi":np.tanh(size_oi*25.0),
       "low_turnover":np.tanh((0.35-vol_oi).clip(lower=0.0)*2.0),
       "high_turnover":np.tanh((vol_oi-0.80).clip(lower=0.0)),
       "confidence":conf-0.5}
    coeffs=dict(HEURISTIC_OPENING_COEFFS); lo,hi=.20,.80
    if isinstance(model,dict) and model.get("ready"):
        coeffs=dict(model.get("coefficients") or coeffs)
        lo,hi=float(model.get("clip_low",.05)),float(model.get("clip_high",.95))
    p=pd.Series(float(coeffs.get("intercept",.50)),index=e.index,dtype=float)
    for k in ("size_oi","low_turnover","high_turnover","confidence"):
        p=p+float(coeffs.get(k,0.0))*f[k]
    p=p.clip(lo,hi)
    certainty=(p-0.5).abs()*2.0
    fresh=((p-0.5)*2.0).clip(lower=0.0)

    ag=(e["aggressor"] if "aggressor" in e.columns else pd.Series("",index=e.index)).astype(str).str.upper()
    side=np.where(ag=="BUY",1.0,np.where(ag=="SELL",-1.0,0.0))
    delta=col("model_delta","fallback_delta","provider_delta","delta")
    gamma=col("model_gamma","fallback_gamma","provider_gamma","gamma")

    # Unit-counterparty-share quantities. Sign convention: positive hedge_notional is an
    # estimated underlying BUY requirement. Customer buys a call -> dealer is short the
    # call -> short delta -> must buy underlying.
    contract_change=-side*size*fresh
    # `col()` fuerza a numerico, asi que el simbolo se deduce del propio frame.
    mult=multiplier_series(e)
    delta_change=contract_change*delta*mult
    return pd.DataFrame({
        "timestamp":e["timestamp"].to_numpy(),
        "strike":col("strike",default=float("nan")).to_numpy(),
        "expiration_date":(e["expiration_date"] if "expiration_date" in e.columns else pd.Series("",index=e.index)).astype(str).to_numpy(),
        "opening_probability":(p*100).to_numpy(),
        "unit_hedge_shares":(-delta_change).to_numpy(),
        "unit_hedge_notional":(-delta_change*spot).to_numpy(),
        "unit_dealer_gex":(contract_change*gamma*(spot**2)*mult*.01).to_numpy(),
        "unit_confidence":((conf*.55+certainty*.30+.15).clip(0,1)).to_numpy(),
    })


def hedge_pressure(events: pd.DataFrame, now: pd.Timestamp | None=None, counterparty_share: float=.70,
                   model: Dict[str, Any] | None=None, _rows: pd.DataFrame | None=None) -> Dict[str, Any]:
    """Window aggregation of estimated hedge requirement.

    v1.15: single vectorised pass. Every output is exactly proportional to
    counterparty_share, so scenarios are obtained by scaling rather than by rerunning
    the model - the previous version recomputed the full per-event loop four times to
    produce three numbers that were the same number times 0.30/0.70 and 1.00/0.70.
    """
    empty={"state":"WAITING","windows":{},"net_15m":0.0,"direction":"NEUTRAL","confidence":0.0,"top_strikes":[]}
    if _rows is None:
        if events is None or events.empty: return empty
        _rows=_hedge_rows(events,model)
    h=_rows
    if h is None or h.empty: return empty
    cp=float(np.clip(counterparty_share,0.0,1.0))
    end=pd.Timestamp(now) if now is not None else pd.Series(h["timestamp"]).max()
    ts=pd.Series(h["timestamp"])
    out={}
    for mins in (1,3,5,15):
        m=ts>=end-pd.Timedelta(minutes=mins)
        g=h[m.to_numpy()]
        net=float(g["unit_hedge_notional"].sum())*cp if len(g) else 0.0
        gross=float(g["unit_hedge_notional"].abs().sum())*cp if len(g) else 0.0
        conf=float(np.average(g["unit_confidence"],weights=np.maximum(g["unit_hedge_notional"].abs(),1)))*cp*100 if len(g) else 0.0
        out[str(mins)]={"minutes":mins,"net_notional":round(net,2),"gross_notional":round(gross,2),
                        "net_shares":round(float(g["unit_hedge_shares"].sum())*cp if len(g) else 0.0,2),
                        "confidence":round(conf,1),"events":int(len(g)),
                        "direction":"BUY" if net>0 else "SELL" if net<0 else "NEUTRAL"}
    n15=out["15"]["net_notional"]; latest=out["1"]["net_notional"]
    prevm=((ts<end-pd.Timedelta(minutes=1))&(ts>=end-pd.Timedelta(minutes=4))).to_numpy()
    prev=h[prevm]
    prev_rate=float(prev["unit_hedge_notional"].sum())*cp/3.0 if len(prev) else 0.0
    accel=(latest-prev_rate)/max(abs(prev_rate),1.0)*100.0
    w=h[(ts>=end-pd.Timedelta(minutes=15)).to_numpy()]
    top=[]
    if len(w):
        by=w.groupby("strike",as_index=False).agg(
            net=("unit_hedge_notional","sum"),
            gross=("unit_hedge_notional",lambda z:float(np.abs(z).sum())),
            events=("unit_hedge_notional","size"),confidence=("unit_confidence","mean"))
        by=by.sort_values("gross",ascending=False).head(8)
        top=[{"strike":float(r.strike),"net_notional":round(float(r.net)*cp,2),
              "gross_notional":round(float(r.gross)*cp,2),"events":int(r.events),
              "confidence":round(float(r.confidence)*cp*100,1),
              "direction":"BUY" if r.net>0 else "SELL" if r.net<0 else "NEUTRAL"} for r in by.itertuples()]
    return {"state":"ACTIVE","windows":out,"net_15m":n15,
            "direction":"BUY" if n15>0 else "SELL" if n15<0 else "NEUTRAL",
            "confidence":out["15"]["confidence"],"acceleration_pct":round(float(accel),1),
            "top_strikes":top,"counterparty_share_assumption_pct":round(cp*100,1),
            "note":"Hedge Pressure estima cobertura bajo escenario dealer-counterparty; no observa hedges reales. "
                   "Los escenarios de counterparty share son exactamente proporcionales entre sí, no exploraciones independientes."}


def synthetic_oi_proxy(chain: pd.DataFrame, events: pd.DataFrame, model: Dict[str,Any] | None=None) -> Dict[str,Any]:
    """Estimate intraday OI delta without overwriting official OI.

    Official OI remains the structural anchor. The synthetic delta is only an intraday
    research proxy derived from opening/closing likelihood of observed trades.
    """
    if chain is None or chain.empty:return {"state":"NO CHAIN","by_strike":[],"official_oi":0.0,"synthetic_oi":0.0}
    x=chain.copy();x["open_interest"]=numeric_column(x,"open_interest",0).clip(lower=0);official=float(x["open_interest"].sum())
    delta_by={}
    if events is not None and not events.empty:
        for _,r in events.iterrows():
            oc=opening_closing_likelihood(r,model);p=oc["opening_probability"]/100.0;certainty=oc["certainty"]/100.0;qty=max(_f(r.get("contracts"),0),0)
            d=qty*(1.0 if p>.5 else -1.0)*certainty
            cs=str(r.get("contract_symbol",""));delta_by[cs]=delta_by.get(cs,0.0)+d
    if "contract_symbol" in x.columns:x["synthetic_oi_delta"]=x["contract_symbol"].astype(str).map(delta_by).fillna(0.0)
    else:x["synthetic_oi_delta"]=0.0
    x["synthetic_oi"]=(x["open_interest"]+x["synthetic_oi_delta"]).clip(lower=0)
    by=x.groupby("strike",as_index=False).agg(official_oi=("open_interest","sum"),synthetic_delta=("synthetic_oi_delta","sum"),synthetic_oi=("synthetic_oi","sum")).copy()
    by["activity"]=by["synthetic_delta"].abs();by=by.sort_values("activity",ascending=False).head(12)
    rows=[{"strike":float(r.strike),"official_oi":round(float(r.official_oi),1),"synthetic_delta":round(float(r.synthetic_delta),1),"synthetic_oi":round(float(r.synthetic_oi),1)} for r in by.itertuples()]
    return {"state":"ESTIMATED","official_oi":round(official,1),"synthetic_oi":round(float(x["synthetic_oi"].sum()),1),"synthetic_delta":round(float(x["synthetic_oi_delta"].sum()),1),"by_strike":rows,"note":"OI oficial + delta intradía inferido; no sustituye OI oficial."}

def gex_reconciliation(chain: pd.DataFrame, dealer_gex: float, dealer_confidence: float, inventory_contracts: int, symbol: str = "DIA") -> Dict[str, Any]:
    """Keep structural GEX and inferred dealer GEX side by side.

    They answer different questions and are never fused into one score here. Structural
    GEX follows the transparent call+/put- OI convention; dealer GEX follows the
    synthetic inventory inferred from observed flow. Coverage is descriptive only.
    """
    structural=float("nan"); chain_contracts=0
    if isinstance(chain,pd.DataFrame) and not chain.empty:
        chain_contracts=int(chain["contract_symbol"].astype(str).nunique()) if "contract_symbol" in chain.columns else int(len(chain))
        if "signed_gex_proxy" in chain.columns:
            structural=float(pd.to_numeric(chain["signed_gex_proxy"],errors="coerce").fillna(0.0).sum())
        elif {"open_interest","option_type"}.issubset(chain.columns):
            # Transparent structural convention fallback when the dealer module receives
            # a current raw option snapshot rather than engine-enriched rows.
            oi=pd.to_numeric(chain["open_interest"],errors="coerce").fillna(0.0).clip(lower=0.0)
            if "underlying_price" in chain.columns:
                sp=pd.to_numeric(chain["underlying_price"],errors="coerce").replace([np.inf,-np.inf],np.nan).ffill().bfill().fillna(0.0)
            else:
                sp=pd.Series(np.zeros(len(chain)),index=chain.index)
            gamma=None
            if "calc_gamma" in chain.columns:
                gamma=pd.to_numeric(chain["calc_gamma"],errors="coerce")
            elif {"iv","dte","strike"}.issubset(chain.columns):
                try:
                    from .greeks_service import greeks_vector as _greeks_vector, model_inputs_vector
                    K=pd.to_numeric(chain["strike"],errors="coerce").to_numpy(float)
                    iv=pd.to_numeric(chain["iv"],errors="coerce").to_numpy(float)
                    dte=np.maximum(pd.to_numeric(chain["dte"],errors="coerce").to_numpy(float),0.0)
                    call=chain["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()
                    r_a,q_a=model_inputs_vector(str(symbol).upper(),dte)
                    # Structural snapshot can contain slightly different spot values by row;
                    # use their median for one internally consistent valuation instant.
                    S=float(np.nanmedian(sp.to_numpy(float)))
                    gamma=pd.Series(_greeks_vector(symbol,S,K,year_fraction_array(dte),iv,call,r_a,q_a)["gamma"],index=chain.index)
                    sp=pd.Series(np.full(len(chain),S),index=chain.index)
                except Exception:
                    gamma=None
            if gamma is not None:
                gamma=pd.to_numeric(gamma,errors="coerce").fillna(0.0)
                sign=np.where(chain["option_type"].astype(str).str.lower().str.startswith("c"),1.0,-1.0)
                structural=float(np.nansum(sign*gamma.to_numpy(float)*oi.to_numpy(float)*multiplier_series(chain,symbol).to_numpy(float)*(sp.to_numpy(float)**2)*0.01))
    dg=_f(dealer_gex,0.0)
    coverage=100.0*float(inventory_contracts)/max(float(chain_contracts),1.0) if chain_contracts else 0.0
    coverage=float(np.clip(coverage,0.0,100.0))
    eps=max(abs(structural)*0.01,1.0) if math.isfinite(structural) else 1.0
    sgn_s=0 if not math.isfinite(structural) or abs(structural)<eps else (1 if structural>0 else -1)
    sgn_d=0 if abs(dg)<max(abs(dg)*0.01,1.0) else (1 if dg>0 else -1)
    if sgn_s==0 or sgn_d==0: agreement="NEUTRAL / INSUFFICIENT"
    elif sgn_s==sgn_d: agreement="ALIGNED"
    else: agreement="CONFLICT"
    ratio=abs(dg)/abs(structural) if math.isfinite(structural) and abs(structural)>1e-9 else float("nan")
    return {
        "structural_gex":None if not math.isfinite(structural) else round(structural,2),
        "structural_label":"STRUCTURAL GEX PROXY",
        "estimated_dealer_gex":round(dg,2),
        "dealer_label":"ESTIMATED DEALER GEX · SHADOW",
        "agreement":agreement,
        "flow_inventory_coverage_pct":round(coverage,1),
        "dealer_confidence":round(float(dealer_confidence),1),
        "dealer_to_structural_abs_ratio":None if not math.isfinite(ratio) else round(float(ratio),4),
        "note":"Diagnóstico: Structural GEX y Estimated Dealer GEX son modelos distintos. CONFLICT no significa error; exige contexto y cobertura suficiente."
    }



# ---------------------------------------------------------------------------
# v1.23 HOT STATE: live synthetic inventory is memory-resident. SQLite remains
# the durable asynchronous archive/research store, not the event hot path.
# ---------------------------------------------------------------------------
import atexit
import threading
import time
from .obs import note as _obs_note

_HOT_BOOKS: dict[str, "HotSyntheticInventoryBook"] = {}
_HOT_BOOKS_LOCK = threading.Lock()


class HotSyntheticInventoryBook(SyntheticInventoryBook):
    """Memory-resident dealer inventory with asynchronous SQLite checkpoints.

    Existing `SyntheticInventoryBook` remains the durable archive implementation and
    keeps backwards-compatible tests/tools.  LIVE Dealer Intelligence uses this class:
    every ingest/reconcile/snapshot query runs against a shared in-memory SQLite database
    (no filesystem writes on the event path), while a daemon checkpoint copies the state
    to the existing SQLite/WAL archive every few seconds.

    This is deliberately process-local unless Redis is explicitly configured.  A Redis
    adapter is reported separately in operational readiness and can be used by a future
    multi-node deployment without changing this API.
    """
    def __init__(self, archive_path: Path, flush_seconds: float | None = None):
        self.archive_path=Path(archive_path); self.archive_path.parent.mkdir(parents=True,exist_ok=True)
        self.path=self.archive_path  # public compatibility
        key=hashlib.sha1(str(self.archive_path.resolve()).encode()).hexdigest()[:16]
        self._mem_uri=f"file:itmq_dealer_{key}?mode=memory&cache=shared"
        self._anchor=sqlite3.connect(self._mem_uri,uri=True,check_same_thread=False,timeout=2)
        self._io_lock=threading.RLock(); self._flush_lock=threading.Lock(); self._flush_thread=None
        self._flush_seconds=float(flush_seconds if flush_seconds is not None else os.getenv("ITM_HOTSTATE_FLUSH_SECONDS","5"))
        self._last_flush=time.monotonic(); self._dirty=False
        # Create/migrate the durable archive first, then copy it into RAM exactly once.
        archive=SyntheticInventoryBook(self.archive_path)
        try:
            with sqlite3.connect(self.archive_path,timeout=5) as src:
                src.backup(self._anchor)
        except Exception:
            self._init()
        self._anchor.execute("PRAGMA journal_mode=MEMORY")
        self._anchor.execute("PRAGMA synchronous=OFF")
        self._anchor.execute("PRAGMA temp_store=MEMORY")
        self._anchor.commit()

    def _conn(self):
        c=sqlite3.connect(self._mem_uri,uri=True,check_same_thread=False,timeout=2)
        c.execute("PRAGMA journal_mode=MEMORY"); c.execute("PRAGMA synchronous=OFF"); c.execute("PRAGMA temp_store=MEMORY")
        return c

    def ingest(self,*args,**kwargs):
        out=super().ingest(*args,**kwargs); self._dirty = self._dirty or bool(out.get("ingested")); self._schedule_flush(); return out

    def reconcile(self,*args,**kwargs):
        out=super().reconcile(*args,**kwargs); self._dirty = self._dirty or bool(out.get("reconciled") or out.get("removed")); self._schedule_flush(); return out

    def _schedule_flush(self, force: bool=False):
        if not self._dirty:return
        if not force and time.monotonic()-self._last_flush < max(.25,self._flush_seconds):return
        with self._flush_lock:
            if self._flush_thread and self._flush_thread.is_alive():return
            self._flush_thread=threading.Thread(target=self.flush_archive,name="itmq-dealer-archive",daemon=True)
            self._flush_thread.start()

    def flush_archive(self):
        if not self._dirty:return {"flushed":False,"reason":"clean"}
        with self._io_lock:
            try:
                dest=sqlite3.connect(self.archive_path,timeout=10)
                try:
                    self._anchor.backup(dest)
                    dest.commit()
                finally:
                    dest.close()
                self._dirty=False; self._last_flush=time.monotonic()
                return {"flushed":True,"path":str(self.archive_path)}
            except Exception as exc:
                return {"flushed":False,"reason":f"{type(exc).__name__}: {exc}"[:180]}

    def hot_status(self):
        return {"backend":"RAM_SQLITE_SHARED_CACHE","archive":"SQLITE_WAL_ASYNC","dirty":bool(self._dirty),
                "flush_seconds":self._flush_seconds,"archive_path":str(self.archive_path)}


def hot_inventory_book(path: Path) -> HotSyntheticInventoryBook:
    key=str(Path(path).resolve())
    with _HOT_BOOKS_LOCK:
        book=_HOT_BOOKS.get(key)
        if book is None:
            book=HotSyntheticInventoryBook(Path(path)); _HOT_BOOKS[key]=book
        return book


def _flush_all_hot_books():
    for b in list(_HOT_BOOKS.values()):
        try:b.flush_archive()
        except Exception as _e:
            _obs_note('dealer_intelligence:809', _e)

atexit.register(_flush_all_hot_books)

# ---------------------------------------------------------------------------
# Opening/closing calibration is intentionally OUTSIDE the LIVE event path.
# The event loop only reads the last cached model and enqueues a compact snapshot.
# Disk I/O + fitting happen on a daemon worker. This avoids SQLite/file jitter during
# bursts while preserving the same durable research dataset and OOS gate.
# ---------------------------------------------------------------------------
_CAL_MANAGERS: dict[str,"AsyncOpeningCalibration"] = {}
_CAL_MANAGERS_LOCK = threading.Lock()

class AsyncOpeningCalibration:
    def __init__(self, path: Path):
        self.path=Path(path); self.store=OpeningCalibrationStore(self.path)
        self._lock=threading.RLock(); self._pending=None; self._model=None
        self._fit={"ready":False,"status":"COLLECTING","reason":"background worker warming"}
        self._last_record=None; self._last_fit_at=0.0; self._last_error=None
        self._wake=threading.Event(); self._stop=threading.Event()
        self._thread=threading.Thread(target=self._run,name="itmq-opening-calibration",daemon=True); self._thread.start()
        # One startup read is outside the tick/event lifecycle.
        try:self._model=self.store.load_model("DIA") if False else None
        except Exception as _e:
            _obs_note('dealer_intelligence:832', _e)

    def submit(self, symbol: str, chain: pd.DataFrame, events: pd.DataFrame | None):
        # Reduce the queued frame to only columns needed by record_session/opening_features.
        ccols=[c for c in ("timestamp","contract_symbol","open_interest","volume") if isinstance(chain,pd.DataFrame) and c in chain.columns]
        ecols=[c for c in ("contract_symbol","contracts","volume","open_interest","aggressor_confidence","flow_score","turnover","direction_sign","timestamp") if isinstance(events,pd.DataFrame) and c in events.columns]
        c=chain[ccols].copy() if isinstance(chain,pd.DataFrame) and ccols else pd.DataFrame()
        e=events[ecols].copy() if isinstance(events,pd.DataFrame) and ecols else pd.DataFrame()
        with self._lock:self._pending=(str(symbol).upper(),c,e)
        self._wake.set()

    def current(self, symbol: str):
        with self._lock:
            model=self._model; fit=dict(self._fit); rec=self._last_record; err=self._last_error
        return model, {**fit,"background":True,"last_record":rec,"last_error":err}

    def _run(self):
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0); self._wake.clear()
            with self._lock: item=self._pending; self._pending=None
            if not item: continue
            sym,chain,events=item
            try:
                rec=self.store.record_session(sym,chain,events)
                now=time.monotonic()
                fit=self._fit
                if now-self._last_fit_at>=float(os.getenv("ITM_OPENING_FIT_SECONDS","300")):
                    fit=self.store.fit(sym,min_contracts=int(os.getenv("ITM_OPENING_MIN_CONTRACTS","200")),min_sessions=int(os.getenv("ITM_OPENING_MIN_SESSIONS","5")),min_volume=float(os.getenv("ITM_OPENING_MIN_VOLUME","25")),ridge=float(os.getenv("ITM_OPENING_RIDGE","1.0")))
                    self._last_fit_at=now
                model=self.store.load_model(sym)
                with self._lock:self._last_record=rec;self._fit=fit;self._model=model;self._last_error=None
            except Exception as exc:
                with self._lock:self._last_error=f"{type(exc).__name__}: {exc}"[:180]

    def stop(self):
        self._stop.set();self._wake.set()

def async_opening_calibration(path: Path)->AsyncOpeningCalibration:
    key=str(Path(path).resolve())
    with _CAL_MANAGERS_LOCK:
        m=_CAL_MANAGERS.get(key)
        if m is None:m=AsyncOpeningCalibration(Path(path));_CAL_MANAGERS[key]=m
        return m

def _stop_calibration_workers():
    for m in list(_CAL_MANAGERS.values()):
        try:m.stop()
        except Exception as _e:
            _obs_note('dealer_intelligence:879', _e)
atexit.register(_stop_calibration_workers)

def dealer_intelligence(chain: pd.DataFrame, events: pd.DataFrame, symbol: str, storage_dir: Path, spot: float, counterparty_share: float=.70, *, underlying_ticks: pd.DataFrame | None=None, futures_ticks: pd.DataFrame | None=None, related_ticks: pd.DataFrame | None=None) -> Dict[str, Any]:
    storage=Path(storage_dir)
    # Re-anchor every option print to the last causal underlying trade when available.
    # Snapshot spot remains an explicit fallback; no future underlying tick is used.
    if isinstance(events,pd.DataFrame) and not events.empty:
        events=attach_causal_underlying(events,underlying_ticks)
        events=enrich_option_packages(events)
    book=hot_inventory_book(routed_dir(storage, "research")/"dealer_inventory.sqlite")
    calib=async_opening_calibration(routed_dir(storage, "calibration")/"opening_calibration.sqlite")
    calib.submit(symbol,chain,events)
    model,shadow_fit=calib.current(symbol)
    recorded=shadow_fit.get("last_record") or {"recorded":0,"status":"QUEUED_ASYNC"}

    reconcile=book.reconcile(chain,symbol)
    ingest=book.ingest(events,counterparty_share,model) if events is not None and not events.empty else {"ingested":0,"skipped":0}
    inv=book.snapshot(symbol,spot,chain)
    # ONE expensive pass; the scenarios below are exact rescalings of it.
    rows=_hedge_rows(events,model) if events is not None and not events.empty else pd.DataFrame()
    hp=hedge_pressure(None,counterparty_share=counterparty_share,_rows=rows)
    hp_scenarios={str(int(cp*100)):hedge_pressure(None,counterparty_share=cp,_rows=rows)
                  for cp in (0.30,float(counterparty_share),1.0)}
    soi=synthetic_oi_proxy(chain,events,model)
    micro_quality=microstructure_quality(events)
    packages=package_summary(events,15)
    flow_confirmation=hedge_flow_confirmation(hp.get("net_15m",0.0),underlying_ticks=underlying_ticks,futures_ticks=futures_ticks,related_ticks=related_ticks)
    recent=book.recent_events(symbol,15)
    if isinstance(recent,pd.DataFrame) and not recent.empty:
        dchg=numeric_column(recent,"dealer_delta_change_shares",0.0)
        # Underlying-equivalent sign: dealer delta increase implies hedge SELL, hence minus.
        hedge_equiv=-dchg
        net_shift=float(hedge_equiv.sum());gross_shift=float(hedge_equiv.abs().sum())
        recent_conf=float(numeric_column(recent,"confidence",0).mean())
        inventory_shift_score=float(np.clip(100.0*net_shift/max(gross_shift,1.0)*(recent_conf/100.0),-100.0,100.0))
    else:
        net_shift=gross_shift=recent_conf=inventory_shift_score=0.0
    inventory_shift={"state":"ACTIVE" if gross_shift>0 else "WAITING","hedge_equivalent_shares":round(net_shift,2),
                     "gross_hedge_equivalent_shares":round(gross_shift,2),"score":round(inventory_shift_score,1),
                     "events":int(len(recent)) if isinstance(recent,pd.DataFrame) else 0,
                     "note":"Cambio de inventario sintético expresado en hedge equivalente; no inventario dealer observado."}
    opening_model={"source":"FITTED ACTIVE" if model else "HEURISTIC PRIOR",
                   "enabled_by_env":os.getenv("ITM_OPENING_CALIBRATION","0")=="1",
                   "session_recorded":recorded,
                   "shadow_fit":shadow_fit,
                   "test_r2":shadow_fit.get("test_r2"),
                   "test_mae":shadow_fit.get("test_mae"),
                   "baseline_mae":shadow_fit.get("baseline_mae"),
                   "beats_constant_baseline":shadow_fit.get("beats_constant_baseline"),
                   "note":"SHADOW por defecto. El objetivo usa cambio posterior de OI como proxy ruidoso; nunca se trata como ground truth. Solo puede activarse con ITM_OPENING_CALIBRATION=1 y holdout superior al baseline."}
    if inv.empty:
        gex_rec=gex_reconciliation(chain,0.0,0.0,0,symbol)
        quality_score=float(micro_quality.get("score",0.0) or 0.0)
        return {"state":"COLLECTING","dealer_field":"UNKNOWN","inventory_contracts":0,"inventory":[],"hedge_pressure":hp,
                "estimated_dealer_gex":0.0,
                "hedge_pressure_scenarios":hp_scenarios,"inventory_shift":inventory_shift,"flow_confirmation":flow_confirmation,
                "microstructure_quality":micro_quality,"packages":packages,"synthetic_oi":soi,"ingest":ingest,"reconcile":reconcile,
                "opening_model":opening_model,"gex_reconciliation":gex_rec,"confidence":round(quality_score*.55,1),"confidence_label":"LOW",
                "dealer_state_score":0.0,"hot_state":book.hot_status(),"distributed_hot_state":HOT_STATE_REDIS.status(),"note":"Inventario dealer es sintético e inferido; necesita flujo OPRA LIVE causal para poblarse."}
    by_surface=inv.groupby(["strike","expiration_date"],as_index=False).agg(
        dealer_contracts=("dealer_contracts","sum"),dealer_delta_shares=("dealer_delta_shares","sum"),
        dealer_gex=("dealer_gex","sum"),hedge_to_neutral_notional=("hedge_to_neutral_notional","sum"),confidence=("confidence","mean"))
    inventory_surface=[{"strike":float(r.strike),"expiration_date":str(r.expiration_date),
        "dealer_contracts":round(float(r.dealer_contracts),2),"dealer_delta_shares":round(float(r.dealer_delta_shares),2),
        "dealer_gex":round(float(r.dealer_gex),2),"hedge_to_neutral_notional":round(float(r.hedge_to_neutral_notional),2),
        "confidence":round(float(r.confidence),1)} for r in by_surface.itertuples()]
    by=inv.groupby("strike",as_index=False).agg(dealer_contracts=("dealer_contracts","sum"),dealer_delta_shares=("dealer_delta_shares","sum"),dealer_gex=("dealer_gex","sum"),dealer_vanna_exposure=("dealer_vanna_exposure","sum"),dealer_charm_exposure=("dealer_charm_exposure","sum"),hedge_to_neutral_notional=("hedge_to_neutral_notional","sum"),confidence=("confidence","mean"))
    by["magnitude"]=by["dealer_gex"].abs()+by["hedge_to_neutral_notional"].abs()/max(float(spot),1)
    by=by.sort_values("magnitude",ascending=False).head(15)
    rows=[]
    for r in by.itertuples():
        rows.append({"strike":float(r.strike),"dealer_contracts":round(float(r.dealer_contracts),2),"dealer_delta_shares":round(float(r.dealer_delta_shares),2),"dealer_gex":round(float(r.dealer_gex),2),"dealer_vanna_exposure":round(float(r.dealer_vanna_exposure),2),"dealer_charm_exposure":round(float(r.dealer_charm_exposure),2),"hedge_to_neutral_notional":round(float(r.hedge_to_neutral_notional),2),"confidence":round(float(r.confidence),1)})
    total_delta=float(inv["dealer_delta_shares"].sum());total_gex=float(inv["dealer_gex"].sum());total_hedge=float(inv["hedge_to_neutral_notional"].sum())
    conf=float(pd.to_numeric(inv["confidence"],errors="coerce").fillna(0).mean()) if len(inv) else 0.0
    gex_rec=gex_reconciliation(chain,total_gex,conf,int(len(inv)),symbol)
    structural_gex=_f(gex_rec.get("structural_gex"),0.0)
    field_sign=1 if total_gex>0 else -1 if total_gex<0 else 0
    dealer_field="LONG_GAMMA" if field_sign>0 else "SHORT_GAMMA" if field_sign<0 else "NEUTRAL"
    hp_score=(1 if hp.get("direction")=="BUY" else -1 if hp.get("direction")=="SELL" else 0)*float(hp.get("confidence",0.0) or 0.0)
    fc_score=float(flow_confirmation.get("score",0.0) or 0.0)*(float(flow_confirmation.get("confidence",0.0) or 0.0)/100.0)
    field_component=field_sign*min(100.0,max(conf,20.0))
    dealer_state_score=float(np.clip(.34*field_component+.34*hp_score+.20*inventory_shift_score+.12*fc_score,-100.0,100.0))
    # Confidence measures observability/model support, not chance of a profitable trade.
    components=[(float(micro_quality.get("score",0.0) or 0.0),.38),(float(hp.get("confidence",0.0) or 0.0),.22),(float(conf),.20)]
    if bool(shadow_fit.get("ready")) and bool(shadow_fit.get("beats_constant_baseline")):components.append((75.0,.10))
    else:components.append((30.0,.10))
    if flow_confirmation.get("state") not in {"UNAVAILABLE","NEUTRAL"}:components.append((float(flow_confirmation.get("confidence",0.0) or 0.0),.10))
    den=sum(w for _,w in components) or 1.0;overall_conf=float(np.clip(sum(v*w for v,w in components)/den,0,100))
    conf_label="HIGH" if overall_conf>=75 else "MEDIUM" if overall_conf>=50 else "LOW"
    try:
        HOT_STATE_REDIS.put(f"dealer:{str(symbol).upper()}", {"dealer_field":dealer_field,"dealer_state_score":round(dealer_state_score,1),"confidence":round(overall_conf,1),"inventory_surface":inventory_surface,"updated_at":datetime.now(timezone.utc).isoformat()}, ttl=7200)
    except Exception as _e:
        _obs_note('dealer_intelligence:970', _e)
    return {"state":"ESTIMATED","dealer_field":dealer_field,"inventory_contracts":int(len(inv)),
            "synthetic_dealer_delta_shares":round(total_delta,2),"synthetic_dealer_gex":round(total_gex,2),
            "estimated_dealer_gex":round(total_gex,2),
            "hedge_to_neutral_notional":round(total_hedge,2),"inventory":rows,"inventory_surface":inventory_surface,"hedge_pressure":hp,"hedge_pressure_scenarios":hp_scenarios,
            "inventory_shift":inventory_shift,"flow_confirmation":flow_confirmation,"microstructure_quality":micro_quality,"packages":packages,
            "synthetic_oi":soi,"ingest":ingest,"reconcile":reconcile,"opening_model":opening_model,"gex_reconciliation":gex_rec,
            "dealer_state_score":round(dealer_state_score,1),"dealer_state_is_probability":False,
            "greeks_valuation":str(inv["greeks_valuation"].mode().iloc[0]) if "greeks_valuation" in inv.columns and len(inv) else "UNKNOWN",
            "greeks_repriced_pct":float(inv["greeks_repriced_pct"].max()) if "greeks_repriced_pct" in inv.columns and len(inv) else 0.0,
            "inventory_confidence":round(conf,1),"confidence":round(overall_conf,1),"confidence_label":conf_label,
            "hot_state":book.hot_status(),"distributed_hot_state":HOT_STATE_REDIS.status(),
            "uncertainty_scenarios":{"low_counterparty_share_pct":30,"base_pct":round(counterparty_share*100,1),"high_pct":100},
            "note":"ESTIMATED DEALER INVENTORY. OPRA/NBBO y Tape son observados cuando disponibles; opening/closing, dealer inventory y hedge requirement siguen siendo inferencias."}

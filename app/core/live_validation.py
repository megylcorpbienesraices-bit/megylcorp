"""LIVE validation readiness dashboard for ITM QUANT v1.14.7.

This module does not change trading logic.  It only tells the user whether the
research/calibration machinery has enough *real LIVE data* to do anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import json
import os
import sqlite3
from app.persistence import routed_dir
from .obs import note as _obs_note


def _sql_count(path: Path, sql: str, params=()) -> int:
    try:
        if not path.exists():
            return 0
        with sqlite3.connect(path, timeout=2) as c:
            row=c.execute(sql, params).fetchone()
            return int(row[0] or 0) if row else 0
    except Exception as exc:
        _obs_note("live_validation:sql_count_failed", exc, severity="CRITICAL_DATA")
        return 0


def _anchor_status(storage: Path, symbol: str, expiry_mode: str | None) -> Dict[str, Any]:
    scope=str(expiry_mode or "").strip().lower().replace(" ","_").replace("/","_")
    suffix=f"_{scope}" if scope else ""
    astore=routed_dir(storage, "anchors")
    p=astore/f"scale_anchors_{str(symbol).lower()}{suffix}.json"
    pend=astore/f"scale_anchors_pending_{str(symbol).lower()}{suffix}.json"
    n=0; last=None
    try:
        if p.exists():
            j=json.loads(p.read_text(encoding="utf-8")); last=j.get("last_promoted_session")
            mets=j.get("metrics") or {}
            n=max([int((v or {}).get("n",0) or 0) for v in mets.values()] or [0])
    except Exception as _e:
        _obs_note('live_validation:40', _e)
    pending=False; pending_session=None
    try:
        if pend.exists():
            j=json.loads(pend.read_text(encoding="utf-8")); pending=bool(j); pending_session=j.get("session")
    except Exception as _e:
        _obs_note('live_validation:46', _e)
    try:req=max(1,int(os.getenv("ITM_ANCHOR_MIN_SESSIONS","3")))
    except Exception:req=3
    return {"sessions":n,"required_sessions":req,"ready":n>=req,"last_promoted_session":last,
            "pending":pending,"pending_session":pending_session}


def live_validation_status(storage_dir: Path, symbol: str, expiry_mode: str | None,
                           calibration: Dict[str, Any] | None,
                           research: Dict[str, Any] | None,
                           dealer: Dict[str, Any] | None) -> Dict[str, Any]:
    storage=Path(storage_dir); sym=str(symbol).upper(); cal=calibration or {}; rs=research or {}; dl=dealer or {}
    prob=cal.get("probability_model") or {}
    try:req_signals=max(1,int(os.getenv("ITM_CALIB_MIN_SAMPLES","120")))
    except Exception:req_signals=120
    try:req_sessions=max(1,int(os.getenv("ITM_CALIB_MIN_SESSIONS","40")))
    except Exception:req_sessions=8
    # probability_calibration currently defaults to 120/8; expose these exact targets
    # even if no model exists yet.
    signals=int(cal.get("sample_size",0) or 0); sessions=int(cal.get("sessions",0) or 0)
    dealer_db=routed_dir(storage, "research")/"dealer_inventory.sqlite"
    opening_db=routed_dir(storage, "calibration")/"opening_calibration.sqlite"
    dealer_events=_sql_count(dealer_db,"SELECT COUNT(*) FROM dealer_events WHERE symbol=?",(sym,))
    dealer_inventory=_sql_count(dealer_db,"SELECT COUNT(*) FROM dealer_inventory WHERE symbol=?",(sym,))
    oi_sessions=_sql_count(opening_db,"SELECT COUNT(DISTINCT session_date) FROM oi_sessions WHERE symbol=?",(sym,))
    opening_rows=_sql_count(opening_db,"SELECT COUNT(*) FROM oi_sessions WHERE symbol=?",(sym,))
    opening_models=_sql_count(opening_db,"SELECT COUNT(*) FROM opening_model WHERE symbol=?",(sym,))
    anchors=_anchor_status(storage,sym,expiry_mode)
    research_db=routed_dir(storage, "research")/"research_v114.sqlite"
    tape_events=_sql_count(research_db,"SELECT COUNT(*) FROM tape_events WHERE symbol=?",(sym,))
    tape_terminal=_sql_count(research_db,"SELECT COUNT(*) FROM tape_events WHERE symbol=? AND tape_state IN ('CONFIRMED','ABSORBED','REJECTED','CHURN','EXPIRED')",(sym,))
    gate_on=os.getenv("ITM_EV_GATE_ACTIVE","0").strip()=="1"
    instrument=os.getenv("ITM_INSTRUMENT_MODE","underlying").strip().lower() or "underlying"
    stage=str(prob.get("stage") or prob.get("status") or "COLLECTING")
    probability_ready=bool(prob.get("ready"))
    components=[
        {"key":"sessions","label":"Sesiones LIVE","value":sessions,"target":req_sessions,"ready":sessions>=req_sessions,
         "detail":"Sesiones independientes usadas por Calibration Lab."},
        {"key":"signals","label":"Señales evaluables","value":signals,"target":req_signals,"ready":signals>=req_signals,
         "detail":"Hipótesis LIVE con trayectoria posterior suficiente."},
        {"key":"research","label":"Ciclos cuantitativos","value":int(rs.get("cycles",0) or 0),"target":None,"ready":int(rs.get("cycles",0) or 0)>0,
         "detail":f"Research Store · {int(rs.get('sessions',0) or 0)} sesiones."},
        {"key":"tape","label":"Estados Tape Timing","value":tape_terminal,"target":None,"ready":tape_terminal>0,
         "detail":f"{tape_events} transiciones guardadas · timing separado del score estructural."},
        {"key":"dealer","label":"Eventos dealer/OPRA","value":dealer_events,"target":None,"ready":dealer_events>0,
         "detail":f"Inventario sintético: {dealer_inventory} contratos/filas activas."},
        {"key":"opening","label":"Sesiones OI para opening","value":oi_sessions,"target":5,"ready":oi_sessions>=5,
         "detail":f"{opening_rows} contrato-sesiones · modelo guardado {opening_models}."},
        {"key":"anchors","label":"Anclas históricas","value":anchors.get("sessions",0),"target":anchors.get("required_sessions",3),"ready":bool(anchors.get("ready")),
         "detail":f"Scope {str(expiry_mode or 'AUTO').upper()} · pending {anchors.get('pending_session') or '—'}."},
    ]
    if sessions==0 or signals==0:
        next_step="Ejecuta ITM QUANT en modo LIVE durante sesiones reales; DEMO no cuenta para calibración."
    elif not probability_ready:
        next_step="Sigue recolectando hasta que el modelo de probabilidad supere la tasa base fuera de muestra."
    elif not gate_on:
        next_step="La calibración ya puede mostrarse, pero el EV Gate sigue OFF por seguridad hasta habilitación explícita."
    else:
        next_step="EV Gate habilitado: vigila Brier/Log Loss OOS y desactívalo si el modelo deja de superar la tasa base."
    return {
        "phase":"LIVE VALIDATION","symbol":sym,"instrument_mode":instrument.upper(),"probability_stage":stage,
        "probability_ready":probability_ready,"ev_gate_active":gate_on,"components":components,
        "sessions":sessions,"signals":signals,"required_sessions":req_sessions,"required_signals":req_signals,
        "dealer_events":dealer_events,"dealer_inventory":dealer_inventory,"oi_sessions":oi_sessions,
        "tape_events":tape_events,"tape_terminal_events":tape_terminal,
        "anchors":anchors,"next_step":next_step,
        "note":"Este panel mide recolección/validación; Tape es timing/confirmación y no modifica BUY/SELL ni Evidence Score."
    }

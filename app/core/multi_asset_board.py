"""v1.16.3 · Board Multi-Activo.

Presenter/router only. It never computes direction or a cross-asset strength score.
Scanner remains the sole structural authority for each symbol.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable
import math

EDGE_RANK = {"ACTIONABLE": 0, "CAUTION": 1, "NO EDGE": 2, "SIN SEÑAL": 3}

ALLOWED_FIELDS = frozenset({
    "symbol","spot","direction","scenario_type","zone_low","zone_high","target1","target2",
    "invalidation","edge_state","evidence_score","contender_gap","rr_t1",
    "expected_value_r","probability_t1_first","gate_mode","gate_stage","model_calibrated",
    "ev_gate_active","ev_shadow_available","gate_bucket","data_quality","model_health",
    "expiry_label","as_of","age_seconds","freshness","status","note","refresh_cadence_seconds",
})


def _num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _iso(v):
    if v is None: return None
    if isinstance(v, datetime):
        d=v
    else:
        try: d=datetime.fromisoformat(str(v).replace("Z","+00:00"))
        except Exception: return None
    if d.tzinfo is None: d=d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def freshness_for(as_of, now=None, cadence_seconds: float=90.0):
    now=now or datetime.now(timezone.utc)
    ts=_iso(as_of)
    if ts is None: return None,"SIN DATO"
    age=max(0.0,(now-ts).total_seconds())
    cadence=max(float(cadence_seconds or 90.0),1.0)
    fresh_limit=min(max(1.5*cadence,45.0),120.0)
    stale_limit=min(max(4.0*cadence,180.0),300.0)
    label="EN VIVO" if age<=fresh_limit else "RETRASADO" if age<=stale_limit else "OBSOLETO"
    return round(age,1),label


def board_row(symbol: str, scanner: Dict[str,Any] | None, *, spot=None, data_quality=None,
              model_health=None, expiry_label=None, as_of=None, now=None,
              refresh_cadence_seconds: float=90.0) -> Dict[str,Any]:
    sc=scanner or {}; gate=sc.get("edge_gate") or {}; zone=sc.get("zone") or {}
    ready=bool(sc.get("ready"))
    calibrated=bool(gate.get("calibration_ready"))
    active=bool(gate.get("active"))
    ev=_num(gate.get("expected_value_r"))
    if active:
        bucket="EV GATE ACTIVE"
    elif calibrated:
        bucket="CALIBRATED · GATE OFF"
    else:
        bucket="EVIDENCE THRESHOLD (LEGACY)"
    age,fresh=freshness_for(as_of,now,refresh_cadence_seconds)
    row={
        "symbol":str(symbol).upper(), "spot":_num(spot if spot is not None else sc.get("spot")),
        "direction":sc.get("direction") if ready else None,
        "scenario_type":sc.get("scenario_type") if ready else None,
        "zone_low":_num(zone.get("low")), "zone_high":_num(zone.get("high")),
        "target1":_num(sc.get("target1")), "target2":_num(sc.get("target2")),
        "invalidation":_num(sc.get("invalidation")),
        "edge_state":str(sc.get("edge_state") or "SIN SEÑAL") if ready else "SIN SEÑAL",
        "evidence_score":_num(sc.get("evidence_score")), "contender_gap":_num(sc.get("contender_gap")),
        "rr_t1":_num(sc.get("rr_t1")), "expected_value_r":ev,
        "probability_t1_first":_num(gate.get("probability_t1_first")) if calibrated else None,
        "gate_mode":gate.get("mode"), "gate_stage":gate.get("stage") or "COLLECTING",
        "model_calibrated":calibrated, "ev_gate_active":active,
        "ev_shadow_available":bool(ev is not None and not active), "gate_bucket":bucket,
        "data_quality":_num(data_quality), "model_health":_num(model_health),
        "expiry_label":expiry_label or "—", "as_of":_iso(as_of).isoformat() if _iso(as_of) else None,
        "age_seconds":age, "freshness":fresh, "status":"LISTO" if ready else "SIN SEÑAL",
        "note":"Fila fuera del ranking operativo por antigüedad." if fresh=="OBSOLETO" else None,
        "refresh_cadence_seconds":round(float(refresh_cadence_seconds or 90.0),1),
    }
    extra=set(row)-ALLOWED_FIELDS
    if extra: raise ValueError(f"campo no permitido en el board: {sorted(extra)}")
    return row


def _live_sort(r: Dict[str,Any]):
    edge=EDGE_RANK.get(str(r.get("edge_state")),4)
    # EV and Evidence never share a numeric slot. Gate-active rows form their own family.
    family=0 if r.get("ev_gate_active") else 1
    primary=-(r.get("expected_value_r") or 0.0) if family==0 else -(r.get("evidence_score") or 0.0)
    return (edge,family,primary,-(r.get("contender_gap") or 0.0),r.get("symbol") or "")


def build_board(rows: Iterable[Dict[str,Any]]) -> Dict[str,Any]:
    items=[]
    for row in rows:
        if not row: continue
        r=dict(row); extra=set(r)-ALLOWED_FIELDS
        if extra: raise ValueError(f"campo no permitido en el board: {sorted(extra)}")
        items.append(r)
    live=[r for r in items if r.get("freshness")!="OBSOLETO"]
    stale=[r for r in items if r.get("freshness")=="OBSOLETO"]
    live.sort(key=_live_sort); stale.sort(key=lambda r:(r.get("age_seconds") or 1e12,r.get("symbol") or ""))
    ordered=live+stale
    counts={k:sum(1 for r in live if r.get("edge_state")==k) for k in EDGE_RANK}
    calibrated=sum(1 for r in items if r.get("model_calibrated"))
    gate_active=sum(1 for r in items if r.get("ev_gate_active"))
    ages=[r.get("age_seconds") for r in items if r.get("age_seconds") is not None]
    return {
        "rows":ordered,"assets":len(items),"live_rows":len(live),"stale_rows":len(stale),"counts":counts,
        "calibrated_models":calibrated,"calibrated_coverage_pct":round(100*calibrated/len(items),1) if items else 0.0,
        "ev_gate_active":gate_active,"ev_gate_active_pct":round(100*gate_active/len(items),1) if items else 0.0,
        "oldest_age_seconds":round(max(ages),1) if ages else None,
        "source":"QUANT SCANNER","ordering_label":"ORDEN DE ATENCIÓN",
        "note":"Scanner decide por activo. Board solo organiza atención. EV ordena únicamente dentro de filas con EV Gate ACTIVE; Evidence ordena las filas Legacy/Shadow. OBSOLETO queda fuera del ranking operativo.",
    }

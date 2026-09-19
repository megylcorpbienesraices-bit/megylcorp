"""Persistent technical/quantitative Auditor for ITM QUANT v1.21.0.

The Auditor is deliberately observational. It records how the platform behaved,
what data quality/model health looked like, and how Calibration/SHADOW readiness
evolved. It never changes Scanner direction, targets, stop or Evidence Score.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable
from zoneinfo import ZoneInfo
import hashlib
import json
import math

from app.persistence import routed_dir
from .obs import note as _obs_note

EC = ZoneInfo("America/Guayaquil")


def _num(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _stage(cal: Dict[str, Any]) -> str:
    p = (cal or {}).get("probability_model") or {}
    return str(p.get("stage") or p.get("status") or (cal or {}).get("status") or "COLLECTING")


def _event_id(payload: Dict[str, Any]) -> str:
    key = "|".join([
        str(payload.get("symbol") or ""),
        str(payload.get("timestamp") or ""),
        str((payload.get("scanner") or {}).get("generated_at") or ""),
        str((payload.get("scanner") or {}).get("direction") or ""),
        str((payload.get("scanner") or {}).get("edge_state") or ""),
    ])
    return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _load_jsonl(path: Path) -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []
    if not path.exists():
        return rows
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                try:
                    item = json.loads(raw)
                    if isinstance(item, dict):
                        rows.append(item)
                except Exception as _e:
                    _obs_note('audit_reporting:59', _e)
                    continue
    except Exception:
        return []
    return rows


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _mean(vals: Iterable[Any]):
    x = [_num(v) for v in vals]
    x = [v for v in x if v is not None]
    return round(sum(x) / len(x), 2) if x else None


def _min(vals: Iterable[Any]):
    x = [_num(v) for v in vals]
    x = [v for v in x if v is not None]
    return round(min(x), 2) if x else None


def _daily_summary(rows: list[Dict[str, Any]], symbol: str, day: str) -> Dict[str, Any]:
    if not rows:
        return {"symbol": symbol, "session_date": day, "observations": 0, "status": "COLLECTING"}
    dq = [r.get("data_quality") for r in rows]
    mh = [r.get("model_health") for r in rows]
    dirs = Counter(str((r.get("scanner") or {}).get("direction") or "WAITING") for r in rows)
    edges = Counter(str((r.get("scanner") or {}).get("edge_state") or "WAITING") for r in rows)
    issues = Counter()
    disagreements = 0
    errors = 0
    for r in rows:
        if (_num(r.get("data_quality"), 100) or 0) < 70:
            issues["DATA_QUALITY_LT_70"] += 1
        if (_num(r.get("model_health"), 100) or 0) < 75:
            issues["MODEL_HEALTH_LT_75"] += 1
        if bool((r.get("source_health") or {}).get("data_disagreement")):
            issues["SOURCE_DISAGREEMENT"] += 1
            disagreements += 1
        if r.get("error"):
            issues["REFRESH_ERROR"] += 1
            errors += 1
    latest = rows[-1]
    cal = latest.get("calibration") or {}
    scanner = latest.get("scanner") or {}
    tape = latest.get("tape_archive") or {}
    return {
        "version": "1.21.0",
        "symbol": symbol,
        "session_date": day,
        "status": "OK" if not errors else "DEGRADED",
        "first_observation": rows[0].get("timestamp"),
        "last_observation": latest.get("timestamp"),
        "observations": len(rows),
        "data_quality": {"latest": latest.get("data_quality"), "average": _mean(dq), "minimum": _min(dq)},
        "model_health": {"latest": latest.get("model_health"), "average": _mean(mh), "minimum": _min(mh)},
        "scanner": {
            "latest_direction": scanner.get("direction"),
            "latest_edge_state": scanner.get("edge_state"),
            "latest_evidence": scanner.get("evidence_score"),
            "direction_counts": dict(dirs),
            "edge_state_counts": dict(edges),
        },
        "calibration": {
            "status": cal.get("status") or "COLLECTING",
            "probability_stage": _stage(cal),
            "sample_size": int(cal.get("sample_size", 0) or 0),
            "sessions": int(cal.get("sessions", 0) or 0),
            "walk_forward_ready": bool((cal.get("walk_forward") or {}).get("ready")),
            "ev_gate_ready": bool((cal.get("probability_model") or {}).get("ready")),
        },
        "research": latest.get("research_storage") or {},
        "tape_archive": tape,
        "source_disagreements": disagreements,
        "errors": errors,
        "issues": [{"code": k, "count": v} for k, v in issues.most_common()],
        "note": "Auditor observacional. No modifica Scanner, Tape timing, targets, stop ni Evidence Score.",
    }


def _daily_text(d: Dict[str, Any]) -> str:
    dq = d.get("data_quality") or {}; mh = d.get("model_health") or {}; sc = d.get("scanner") or {}; cal = d.get("calibration") or {}
    issue_lines = [f"- {x.get('code')}: {x.get('count')}" for x in (d.get("issues") or [])] or ["- Sin patrón repetido registrado todavía."]
    return "\n".join([
        "ITM QUANT — INFORME DIARIO DE AUDITORÍA",
        f"Sesión: {d.get('session_date')} · Activo: {d.get('symbol')}",
        f"Estado: {d.get('status')} · Observaciones: {d.get('observations')}",
        f"Data Quality: latest {dq.get('latest')} · avg {dq.get('average')} · min {dq.get('minimum')}",
        f"Model Health: latest {mh.get('latest')} · avg {mh.get('average')} · min {mh.get('minimum')}",
        f"Scanner: {sc.get('latest_direction')} · {sc.get('latest_edge_state')} · Evidence {sc.get('latest_evidence')}",
        f"Calibration: {cal.get('status')} · stage {cal.get('probability_stage')} · N {cal.get('sample_size')} · sesiones {cal.get('sessions')}",
        f"Source disagreements: {d.get('source_disagreements')} · errores: {d.get('errors')}",
        "Patrones / incidencias:",
        *issue_lines,
        "",
        "Regla: una incidencia aislada no cambia el motor. Se revisan patrones repetidos con muestra suficiente.",
    ])


def _cumulative_summary(audit_root: Path, symbol: str) -> Dict[str, Any]:
    daily_files = sorted((audit_root / "daily").glob(f"*/auditor_daily_{symbol.lower()}.json"))
    days: list[Dict[str, Any]] = []
    for p in daily_files:
        try:
            x = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(x, dict) and x.get("observations", 0):
                days.append(x)
        except Exception as _e:
            _obs_note('audit_reporting:171', _e)
            continue
    if not days:
        return {"version": "1.21.0", "symbol": symbol, "sessions_reported": 0, "status": "COLLECTING"}
    total_obs = sum(int(d.get("observations", 0) or 0) for d in days)
    weighted_dq = []
    weighted_mh = []
    issues = Counter()
    for d in days:
        n = max(1, int(d.get("observations", 0) or 0))
        dq = _num((d.get("data_quality") or {}).get("average"))
        mh = _num((d.get("model_health") or {}).get("average"))
        if dq is not None: weighted_dq.extend([dq] * n)
        if mh is not None: weighted_mh.extend([mh] * n)
        for item in d.get("issues") or []:
            issues[str(item.get("code"))] += int(item.get("count", 0) or 0)
    latest = days[-1]
    return {
        "version": "1.21.0",
        "symbol": symbol,
        "status": "ACCUMULATING",
        "sessions_reported": len(days),
        "first_session": days[0].get("session_date"),
        "latest_session": latest.get("session_date"),
        "refresh_observations": total_obs,
        "data_quality_average": _mean(weighted_dq),
        "model_health_average": _mean(weighted_mh),
        "latest_calibration": latest.get("calibration") or {},
        "issue_frequency": [{"code": k, "count": v} for k, v in issues.most_common(12)],
        "latest_daily_status": latest.get("status"),
        "updated_at": latest.get("last_observation"),
        "note": "Tendencias históricas de estabilidad/calibración. No es una métrica de rentabilidad ni altera señales LIVE.",
    }


def _cumulative_text(c: Dict[str, Any]) -> str:
    cal = c.get("latest_calibration") or {}
    issue_lines = [f"- {x.get('code')}: {x.get('count')}" for x in (c.get("issue_frequency") or [])] or ["- Aún no hay incidencias repetidas."]
    return "\n".join([
        "ITM QUANT — INFORME ACUMULADO DE AUDITORÍA",
        f"Activo: {c.get('symbol')} · sesiones reportadas: {c.get('sessions_reported')}",
        f"Rango: {c.get('first_session')} → {c.get('latest_session')}",
        f"Observaciones: {c.get('refresh_observations')}",
        f"Data Quality media: {c.get('data_quality_average')} · Model Health media: {c.get('model_health_average')}",
        f"Calibration actual: {cal.get('status')} · stage {cal.get('probability_stage')} · N {cal.get('sample_size')} · sesiones {cal.get('sessions')}",
        "Incidencias acumuladas:",
        *issue_lines,
        "",
        "El Auditor registra comportamiento del sistema; no auto-modifica pesos ni reglas de trading.",
    ])


def write_audit_reports(storage: Path, symbol: str, mode: str, *,
                        data_quality: Dict[str, Any] | None = None,
                        model_health: Dict[str, Any] | None = None,
                        calibration: Dict[str, Any] | None = None,
                        scanner: Dict[str, Any] | None = None,
                        source_health: Dict[str, Any] | None = None,
                        research_storage: Dict[str, Any] | None = None,
                        tape_archive: Dict[str, Any] | None = None,
                        error: str | None = None,
                        now: datetime | None = None) -> Dict[str, Any]:
    """Persist one Auditor observation and refresh daily/cumulative reports."""
    if str(mode).upper() != "LIVE":
        return {"ready": False, "status": "DEMO_NOT_PERSISTED", "note": "DEMO no contamina Auditor LIVE."}

    now = now or datetime.now(EC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=EC)
    symbol = str(symbol).upper().strip()
    day = now.date().isoformat()
    root = routed_dir(Path(storage), "audit")
    events_dir = root / "events"; daily_dir = root / "daily" / day; cumulative_dir = root / "cumulative"
    events_dir.mkdir(parents=True, exist_ok=True); daily_dir.mkdir(parents=True, exist_ok=True); cumulative_dir.mkdir(parents=True, exist_ok=True)

    dq = data_quality or {}; mh = model_health or {}; cal = calibration or {}; sc = scanner or {}
    payload = {
        "timestamp": now.isoformat(), "symbol": symbol, "mode": "LIVE",
        "data_quality": _num(dq.get("score")), "model_health": _num(mh.get("score")),
        "scanner": {k: sc.get(k) for k in ("generated_at", "direction", "edge_state", "evidence_score", "state", "scenario_type")},
        "calibration": {
            "status": cal.get("status") or "COLLECTING", "sample_size": int(cal.get("sample_size", 0) or 0),
            "sessions": int(cal.get("sessions", 0) or 0), "probability_model": cal.get("probability_model") or {},
            "walk_forward": {"ready": bool((cal.get("walk_forward") or {}).get("ready"))},
        },
        "source_health": source_health or {}, "research_storage": research_storage or {},
        "tape_archive": tape_archive or {}, "error": str(error)[:500] if error else None,
    }
    payload["event_id"] = _event_id(payload)
    event_path = events_dir / f"audit_events_{symbol.lower()}_{day}.jsonl"

    # Avoid duplicate writes for the exact same structural snapshot.
    duplicate = False
    if event_path.exists():
        try:
            with event_path.open("rb") as f:
                f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", errors="ignore")
            duplicate = payload["event_id"] in tail
        except Exception:
            duplicate = False
    if not duplicate:
        with event_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    rows = _load_jsonl(event_path)
    daily = _daily_summary(rows, symbol, day)
    daily_json = daily_dir / f"auditor_daily_{symbol.lower()}.json"
    daily_txt = daily_dir / f"auditor_daily_{symbol.lower()}.txt"
    _atomic_json(daily_json, daily)
    daily_txt.write_text(_daily_text(daily), encoding="utf-8")

    cumulative = _cumulative_summary(root, symbol)
    cumulative_json = cumulative_dir / f"auditor_cumulative_{symbol.lower()}.json"
    cumulative_txt = cumulative_dir / f"auditor_cumulative_{symbol.lower()}.txt"
    _atomic_json(cumulative_json, cumulative)
    cumulative_txt.write_text(_cumulative_text(cumulative), encoding="utf-8")

    return {
        "ready": True, "status": "PERSISTENT", "symbol": symbol,
        "daily": daily, "cumulative": cumulative,
        "daily_report": str(daily_txt), "cumulative_report": str(cumulative_txt),
        "events_file": str(event_path),
    }

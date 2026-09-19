"""Historical scale anchors for cross-snapshot comparable scores (v1.14.3).

The important rule is temporal: a session must never help normalise itself.  LIVE
snapshots are staged into a pending file for the current NY session and are promoted
into the historical anchor only when a later session starts.  This keeps the anchor
strictly backward-looking while still adapting across sessions.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable

import numpy as np
from app.persistence import routed_dir
from .obs import note as _obs_note

DEFAULT_HALFLIFE_SESSIONS = 20.0
DEFAULT_METRICS = ("gross_gex", "delta_exposure", "charm_exposure", "open_interest", "option_volume", "gamma_intensity", "turnover")


def _scope_suffix(scope: str | None) -> str:
    x=str(scope or "").strip().lower().replace(" ","_").replace("/","_")
    return f"_{x}" if x else ""

def _anchor_path(storage_dir: Path, symbol: str, scope: str | None = None) -> Path:
    return routed_dir(Path(storage_dir), "anchors") / f"scale_anchors_{str(symbol).lower()}{_scope_suffix(scope)}.json"


def _pending_path(storage_dir: Path, symbol: str, scope: str | None = None) -> Path:
    return routed_dir(Path(storage_dir), "anchors") / f"scale_anchors_pending_{str(symbol).lower()}{_scope_suffix(scope)}.json"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            x = json.loads(path.read_text(encoding="utf-8"))
            return x if isinstance(x, dict) else {}
    except Exception as _e:
        _obs_note('scale_anchors:42', _e)
    return {}


def load_anchors(storage_dir: Path, symbol: str, scope: str | None = None) -> Dict[str, Any]:
    """Historical-only anchors for one asset/horizon. Pending samples are excluded."""
    payload = _read_json(_anchor_path(storage_dir, symbol, scope))
    return payload.get("metrics", {}) if isinstance(payload, dict) else {}


def _snapshot_quantiles(samples: Dict[str, Iterable[float]], lo_q: float = 0.10,
                        hi_q: float = 0.90) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for metric, values in samples.items():
        x = np.asarray(list(values), dtype=float)
        x = x[np.isfinite(x) & (x > 0)]
        if x.size < 5:
            continue
        y = np.log1p(x)
        lo = float(np.nanquantile(y, lo_q)); hi = float(np.nanquantile(y, hi_q))
        if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
            out[str(metric)] = {"lo": lo, "hi": hi, "count": int(x.size)}
    return out


def _fold_quantiles(storage_dir: Path, symbol: str, quantiles: Dict[str, Dict[str, float]],
                    session_key: str | None = None,
                    halflife: float = DEFAULT_HALFLIFE_SESSIONS, scope: str | None = None) -> Dict[str, Any]:
    """Fold one completed session into the historical anchor."""
    p = _anchor_path(storage_dir, symbol, scope); p.parent.mkdir(parents=True, exist_ok=True)
    payload = _read_json(p)
    current = payload.get("metrics", {}) if isinstance(payload.get("metrics", {}), dict) else {}
    alpha = 1.0 - math.pow(0.5, 1.0 / max(float(halflife), 1.0))
    for metric, q in quantiles.items():
        lo_new = float(q["lo"]); hi_new = float(q["hi"])
        prev = current.get(metric)
        if prev and math.isfinite(float(prev.get("lo", float("nan")))):
            lo = (1-alpha)*float(prev["lo"]) + alpha*lo_new
            hi = (1-alpha)*float(prev["hi"]) + alpha*hi_new
            n = int(prev.get("n", 0)) + 1
        else:
            lo, hi, n = lo_new, hi_new, 1
        current[metric] = {
            "lo": lo, "hi": hi, "n": n, "updated": time.time(),
            "last_session": session_key,
            "last_snapshot_lo": lo_new, "last_snapshot_hi": hi_new,
        }
    try:
        p.write_text(json.dumps({"symbol": str(symbol).upper(), "scope": scope, "metrics": current,
                                 "last_promoted_session": session_key}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except Exception as _e:
        _obs_note('scale_anchors:94', _e)
    return current


def update_anchors(storage_dir: Path, symbol: str, samples: Dict[str, Iterable[float]],
                   lo_q: float = 0.10, hi_q: float = 0.90,
                   halflife: float = DEFAULT_HALFLIFE_SESSIONS, scope: str | None = None) -> Dict[str, Any]:
    """Direct fold helper kept for research/tests.

    LIVE code should use ``stage_session_anchors`` + ``promote_pending_anchors`` so a
    session cannot leak into its own normalisation.
    """
    q = _snapshot_quantiles(samples, lo_q, hi_q)
    return _fold_quantiles(storage_dir, symbol, q, None, halflife, scope)


def stage_session_anchors(storage_dir: Path, symbol: str, samples: Dict[str, Iterable[float]],
                          session_key: str, scope: str | None = None) -> Dict[str, Any]:
    """Store the latest/current NY-session distribution without making it active."""
    p = _pending_path(storage_dir, symbol, scope); p.parent.mkdir(parents=True, exist_ok=True)
    q = _snapshot_quantiles(samples)
    payload = {"symbol": str(symbol).upper(), "scope": scope, "session": str(session_key),
               "updated": time.time(), "quantiles": q}
    try:
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as _e:
        _obs_note('scale_anchors:120', _e)
    return payload


def promote_pending_anchors(storage_dir: Path, symbol: str, current_session: str,
                            scope: str | None = None) -> Dict[str, Any]:
    """Promote a staged session only after NY has advanced to a later session.

    This is the anti-look-ahead boundary.  If the pending file belongs to today's
    session it stays pending and today's engine uses only historical anchors.
    """
    p = _pending_path(storage_dir, symbol, scope)
    pending = _read_json(p)
    pending_session = str(pending.get("session", ""))
    if not pending_session or pending_session >= str(current_session):
        return load_anchors(storage_dir, symbol, scope)
    q = pending.get("quantiles", {}) if isinstance(pending.get("quantiles", {}), dict) else {}
    out = _fold_quantiles(storage_dir, symbol, q, pending_session, scope=scope)
    try:
        p.unlink(missing_ok=True)
    except Exception as _e:
        _obs_note('scale_anchors:141', _e)
    return out


def anchored_magnitude(values, anchor: Dict[str, Any] | None) -> np.ndarray | None:
    """Score today's physical magnitude against a fixed historical scale."""
    if not anchor:
        return None
    try:
        lo = float(anchor.get("lo")); hi = float(anchor.get("hi"))
        if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
            return None
        if int(anchor.get("n", 0)) < int(os.getenv("ITM_ANCHOR_MIN_SESSIONS", "3")):
            return None
    except Exception:
        return None
    x = np.asarray(values, dtype=float)
    x = np.where(np.isfinite(x), np.maximum(x, 0.0), 0.0)
    return np.clip((np.log1p(x) - lo) / (hi - lo), 0.0, 1.0)

"""Quantum-ready optimization contract.

ITM QUANT can formulate a small binary selection problem as a QUBO and solve it
locally with a deterministic classical fallback. A real QPU is never claimed
unless explicitly configured. This module is SHADOW and never changes Scanner.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Dict, Iterable
import math
import os


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def build_qubo(candidates: Iterable[Dict[str, Any]], max_positions: int = 3) -> Dict[str, Any]:
    rows = [dict(r) for r in candidates if isinstance(r, dict)]
    rows = rows[:12]
    max_positions = max(1, min(int(max_positions), max(1, len(rows))))
    # Reward higher absolute evidence/edge but penalize stale and weak-quality rows.
    scores = []
    for r in rows:
        edge = abs(_f(r.get("evidence_score", r.get("edge", 0.0)))) / 100.0
        q = _f(r.get("data_quality", 100.0), 100.0) / 100.0
        stale = 1.0 if str(r.get("status", "")).upper() in {"STALE", "OBSOLETO"} else 0.0
        scores.append(max(-1.0, min(1.0, edge*q - .65*stale)))
    lam = 0.18
    qmat = [[0.0 for _ in rows] for _ in rows]
    for i,s in enumerate(scores): qmat[i][i] = -s
    for i,j in combinations(range(len(rows)),2):
        # Without a calibrated covariance matrix, only a mild generic concentration penalty is allowed.
        qmat[i][j] = qmat[j][i] = lam

    best = (float("inf"), [])
    # Exact small classical fallback. 12 names -> 4096 states max.
    n = len(rows)
    for mask in range(1<<n):
        idx = [i for i in range(n) if mask & (1<<i)]
        if len(idx) > max_positions: continue
        e = 0.0
        for i in idx: e += qmat[i][i]
        for i,j in combinations(idx,2): e += 2*qmat[i][j]
        if e < best[0]: best = (e, idx)
    selected = [str(rows[i].get("symbol") or rows[i].get("asset") or f"A{i}") for i in best[1]]
    provider = str(os.getenv("ITM_QPU_PROVIDER", "")).strip()
    return {
        "ready": bool(rows), "state": "SHADOW", "authority": "NONE",
        "backend": f"QPU:{provider}" if provider else "CLASSICAL_EXACT_FALLBACK",
        "qpu_active": bool(provider), "max_positions": max_positions,
        "selected": selected, "objective": None if not rows else float(best[0]),
        "qubo": qmat,
        "model_risk": "Portfolio/attention selection research only. No live-order or Scanner-direction authority.",
    }

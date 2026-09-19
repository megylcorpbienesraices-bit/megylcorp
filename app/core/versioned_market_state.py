"""Immutable versioned market-state registry: one calculation, many consumers."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import RLock
from typing import Any
import hashlib
import json
import math
from .obs import note as _obs_note


def _safe(v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {str(k): _safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe(x) for x in v]
    try:
        if hasattr(v, "item"):
            return _safe(v.item())
    except Exception as _e:
        _obs_note('versioned_market_state:26', _e)
    return str(v)


def _digest(v: Any) -> str:
    raw = json.dumps(_safe(v), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.blake2b(raw, digest_size=12).hexdigest()


class VersionedMarketState:
    def __init__(self, keep: int = 64) -> None:
        self._lock = RLock(); self._seq = 0
        self._latest: dict[str, dict[str, Any]] = {}
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=max(8, int(keep))))
        self._component_versions: dict[str, dict[str, int]] = defaultdict(dict)
        self._component_digests: dict[str, dict[str, str]] = defaultdict(dict)

    def publish(self, symbol: str, components: dict[str, Any], *, asof: Any = None) -> dict[str, Any]:
        sym = str(symbol or "").upper(); now = datetime.now(timezone.utc)
        with self._lock:
            self._seq += 1
            versions = self._component_versions[sym]
            digests = self._component_digests[sym]
            changed = []
            for name, payload in components.items():
                d = _digest(payload)
                if digests.get(name) != d:
                    versions[name] = int(versions.get(name, 0)) + 1
                    digests[name] = d; changed.append(name)
            rec = {
                "state_id": self._seq, "symbol": sym,
                "asof": str(asof or now.isoformat()), "published_at": now.isoformat(),
                "component_versions": dict(versions), "changed_components": changed,
                "component_digests": dict(digests),
                "consumer_contract": "SINGLE_COMPUTE_MULTI_CONSUMER",
                "authority": "VERSIONING_AND_CONSISTENCY_ONLY",
            }
            self._latest[sym] = rec; self._history[sym].append(dict(rec))
            return dict(rec)

    def latest(self, symbol: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._latest.get(str(symbol or "").upper(), {}))

    def history(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history.get(str(symbol or "").upper(), ())) [-max(1, min(int(limit), 200)):]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"states": len(self._latest), "published": self._seq,
                    "symbols": {k: v.get("state_id") for k, v in self._latest.items()},
                    "policy": "ONE_VERSIONED_STATE_FEEDS_SCANNER_TRACE_AUDITOR",
                    "authority": "STATE_DISTRIBUTION_ONLY"}


VERSIONED_MARKET_STATE = VersionedMarketState()

"""Persistent READY-state store for ITM QUANT's always-on runtime.

This module is deliberately presentation/recovery oriented.  It never becomes Scanner
or market-data authority.  The live engine keeps doing the quantitative work; after a
successful cycle we atomically persist the already-computed UI state/charts/tables.
That gives the browser an immediate READY payload after a reconnect/restart and gives
historical calendar dates a precomputed session package instead of rebuilding the day
on the click path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
import gzip
import json
import os
import tempfile

from ..persistence import category_dir
from .obs import note as _obs_note
from ..version import APP_VERSION

_SCHEMA = 2
_VERSION = APP_VERSION
_LOCK = RLock()
_ROOT = category_dir("system") / "always_on"
_SESSION_ROOT = category_dir("replay") / "instant_sessions"
_ROOT.mkdir(parents=True, exist_ok=True)
_SESSION_ROOT.mkdir(parents=True, exist_ok=True)


def _sym(symbol: str) -> str:
    return str(symbol or "DIA").upper().strip()


def _safe_day(day: str) -> str:
    raw = str(day or "").strip()[:10]
    # Date-only validation without timezone shifting.
    datetime.strptime(raw, "%Y-%m-%d")
    return raw


def _live_path(symbol: str) -> Path:
    return _ROOT / f"live_ready_{_sym(symbol).lower()}.json.gz"


def _session_path(symbol: str, day: str) -> Path:
    return _SESSION_ROOT / f"session_ready_{_sym(symbol).lower()}_{_safe_day(day)}.json.gz"


def _atomic_write_gz(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with gzip.open(tmp, "wb", compresslevel=4) as fh:
            fh.write(blob)
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception as _e:
            _obs_note('always_on_state:66', _e)


def _read_gz(path: Path) -> dict[str, Any] | None:
    try:
        if not path.exists():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict) or int(obj.get("schema", 0) or 0) != _SCHEMA:
            return None
        return obj
    except Exception:
        return None


class AlwaysOnReadyStore:
    """Atomic persistent UI-ready packages plus tiny in-process hot cache."""

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self._session_cache: dict[str, dict[str, Any]] = {}
        self._writes = 0
        self._reads = 0
        self._last_error = ""

    @staticmethod
    def _key(symbol: str) -> str:
        return _sym(symbol)

    @staticmethod
    def _session_key(symbol: str, day: str) -> str:
        return f"{_sym(symbol)}::{_safe_day(day)}"

    def save_ready_package(
        self,
        symbol: str,
        *,
        state: dict[str, Any],
        charts: dict[str, Any],
        tables: dict[str, Any],
        session_day: str,
        source: str = "LIVE_ENGINE_PRECOMPUTED",
        seal: bool = False,
        publish_live: bool = True,
    ) -> dict[str, Any]:
        sym = _sym(symbol)
        day = _safe_day(session_day)
        now = datetime.now(timezone.utc).isoformat()
        package = {
            "schema": _SCHEMA,
            "version": _VERSION,
            "symbol": sym,
            "session_date": day,
            "generated_at_utc": now,
            "source": str(source),
            "sealed": bool(seal),
            "authority": "PRESENTATION_READY_CACHE_ONLY",
            "state": state if isinstance(state, dict) else {},
            "charts": charts if isinstance(charts, dict) else {},
            "tables": tables if isinstance(tables, dict) else {},
        }
        try:
            with _LOCK:
                if publish_live:
                    _atomic_write_gz(_live_path(sym), package)
                    self._cache[self._key(sym)] = package
                _atomic_write_gz(_session_path(sym, day), package)
                self._session_cache[self._session_key(sym, day)] = package
                self._writes += 1
                self._last_error = ""
            return {"ok": True, "symbol": sym, "date": day, "generated_at_utc": now, "sealed": bool(seal), "publish_live": bool(publish_live)}
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"[:240]
            return {"ok": False, "symbol": sym, "date": day, "error": self._last_error}

    def load_live(self, symbol: str) -> dict[str, Any] | None:
        sym = _sym(symbol)
        key = self._key(sym)
        with _LOCK:
            cached = self._cache.get(key)
            if cached:
                self._reads += 1
                return cached
            obj = _read_gz(_live_path(sym))
            if obj:
                self._cache[key] = obj
                self._reads += 1
            return obj

    def load_session(self, symbol: str, day: str) -> dict[str, Any] | None:
        sym, d = _sym(symbol), _safe_day(day)
        key = self._session_key(sym, d)
        with _LOCK:
            cached = self._session_cache.get(key)
            if cached:
                self._reads += 1
                return cached
            obj = _read_gz(_session_path(sym, d))
            if obj:
                self._session_cache[key] = obj
                self._reads += 1
            return obj

    def available_sessions(self, symbol: str, limit: int = 370) -> list[str]:
        sym = _sym(symbol).lower()
        prefix = f"session_ready_{sym}_"
        rows: list[str] = []
        try:
            for p in _SESSION_ROOT.glob(f"{prefix}*.json.gz"):
                name = p.name
                day = name[len(prefix):len(prefix)+10]
                try:
                    _safe_day(day)
                    rows.append(day)
                except Exception as _e:
                    _obs_note('always_on_state:184', _e)
                    continue
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"[:240]
        return sorted(set(rows), reverse=True)[:max(1, int(limit))]

    def status(self, symbol: str = "DIA") -> dict[str, Any]:
        live = self.load_live(symbol)
        return {
            "state": "READY" if live else "COLLECTING",
            "symbol": _sym(symbol),
            "live_package": bool(live),
            "live_generated_at_utc": (live or {}).get("generated_at_utc"),
            "session_packages": len(self.available_sessions(symbol, 10000)),
            "writes": int(self._writes),
            "reads": int(self._reads),
            "last_error": self._last_error,
            "root": str(_ROOT),
            "session_root": str(_SESSION_ROOT),
            "authority": "PRESENTATION_READY_CACHE_ONLY",
        }


READY_STORE = AlwaysOnReadyStore()

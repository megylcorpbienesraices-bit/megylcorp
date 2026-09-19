"""Non-blocking provider data lake + coverage auditor for ITM QUANT.

Design goals
------------
* LIVE paths never wait on disk I/O.
* Every observation retains provider/source, event time, receive time and payload.
* RAW, NORMALIZED and CATALOG data are physically separated.
* Coverage reports what ITM QUANT actually observed/archived; it never claims access
  to undocumented or unauthorized provider data.
* Provider names never imply a fixed rank. Coverage is an observability concern only.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock, Thread
from typing import Any
import json
import os
import queue
import re
import time

from app.persistence import PERSISTENT_ROOT, routed_dir
from .obs import note as _obs_note, expected as _obs_expected


ROOT = routed_dir(PERSISTENT_ROOT, "research") / "provider_data_lake"
ROOT.mkdir(parents=True, exist_ok=True)
MANIFEST = ROOT / "coverage_manifest.json"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any | None = None) -> str:
    if v is None:
        return _utcnow().isoformat()
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except Exception:
            return str(v)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


def _safe_name(v: Any, fallback: str = "unknown") -> str:
    s = str(v or fallback).strip().lower()
    s = re.sub(r"[^a-z0-9_.:@+-]+", "_", s)
    return s[:160] or fallback


def _provider_family(source: str) -> str:
    s = str(source or "UNKNOWN").upper()
    if s.startswith("ALPACA"):
        return "ALPACA"
    if s.startswith("TASTYTRADE"):
        return "TASTYTRADE"
    if s in {"FRED", "BLS", "FED", "FEDERAL_RESERVE", "OFFICIAL_MACRO"}:
        return "OFFICIAL_MACRO"
    return s


def _json_default(v: Any) -> Any:
    if isinstance(v, datetime):
        return _iso(v)
    if isinstance(v, Path):
        return str(v)
    # numpy/pandas scalars without importing those packages on the hot path.
    item = getattr(v, "item", None)
    if callable(item):
        try:
            return item()
        except Exception as _e:
            _obs_note('provider_data_lake:82', _e)
    isoformat = getattr(v, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception as _e:
            _obs_note('provider_data_lake:88', _e)
    return str(v)


EXPECTED_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "ALPACA": (
        "SIP_QUOTE", "SIP_TRADE", "SIP_HISTORY", "OPTION_CONTRACT_CATALOG",
        "OPTION_SNAPSHOT", "OPRA_OPTION_QUOTE", "OPRA_OPTION_TRADE",
        "ECOSYSTEM_RELATED_SNAPSHOT", "ECOSYSTEM_OPTION_CONTRACT_CATALOG",
    ),
    "TASTYTRADE": (
        "UNDERLYING_INSTRUMENT", "FUTURE_INSTRUMENT", "INDEX_SNAPSHOT",
        "EQUITY_OPTION_CATALOG", "INDEX_OPTION_CATALOG", "FUTURE_OPTION_CATALOG",
        "QUOTE", "TRADE", "GREEKS", "SUMMARY",
    ),
    "OFFICIAL_MACRO": ("FRED", "BLS", "FEDERAL_RESERVE"),
}


class ProviderDataLake:
    def __init__(self, *, capacity: int | None = None) -> None:
        cap = int(capacity or int(os.getenv("PROVIDER_DATA_LAKE_QUEUE", "250000")))
        self.queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=max(10_000, cap))
        self._lock = RLock()
        self._started = False
        self._written = 0
        self._written_bytes = 0
        self._dropped = 0
        self._last_error = ""
        self._last_write_at: str | None = None
        self._observed: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._symbols: dict[str, set[str]] = defaultdict(set)
        self._catalog_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._raw_count: dict[str, int] = defaultdict(int)
        self._normalized_count: dict[str, int] = defaultdict(int)
        self._load_manifest()

    def _load_manifest(self) -> None:
        if not MANIFEST.exists():
            return
        try:
            raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
            for p, caps in (raw.get("observed_capabilities") or {}).items():
                for c, n in (caps or {}).items():
                    self._observed[str(p)][str(c)] = int(n or 0)
            for p, syms in (raw.get("symbols") or {}).items():
                self._symbols[str(p)].update(str(x).upper() for x in (syms or []))
            for p, cats in (raw.get("catalog_counts") or {}).items():
                for c, n in (cats or {}).items():
                    self._catalog_counts[str(p)][str(c)] = int(n or 0)
            for p, n in (raw.get("raw_count") or {}).items():
                self._raw_count[str(p)] = int(n or 0)
            for p, n in (raw.get("normalized_count") or {}).items():
                self._normalized_count[str(p)] = int(n or 0)
        except Exception as _e:
            _obs_note('provider_data_lake:146', _e)

    def _start(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            Thread(target=self._writer_loop, name="itmq-provider-data-lake", daemon=True).start()
            self._started = True

    def _enqueue(self, row: dict[str, Any]) -> None:
        self._start()
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    @staticmethod
    def _event_capability(source: str, event_type: str) -> str:
        src = _provider_family(source)
        evt = str(event_type or "UNKNOWN").upper()
        if src == "ALPACA":
            return {
                "QUOTE": "SIP_QUOTE", "TRADE": "SIP_TRADE",
                "OPTION_QUOTE": "OPRA_OPTION_QUOTE", "OPTION_TRADE": "OPRA_OPTION_TRADE",
                "BAR": "SIP_HISTORY", "CANDLE": "SIP_HISTORY",
                "OPTION_SNAPSHOT": "OPTION_SNAPSHOT",
            }.get(evt, evt)
        if src == "TASTYTRADE":
            return evt
        return evt

    def observe(self, source: str, capability: str, *, symbol: str | None = None, count: int = 1) -> None:
        provider = _provider_family(source)
        cap = str(capability or "UNKNOWN").upper()
        with self._lock:
            self._observed[provider][cap] += max(1, int(count or 1))
            if symbol:
                self._symbols[provider].add(str(symbol).upper())

    def archive_raw(self, *, source: str, symbol: str, event_type: str, payload: Any,
                    event_time: Any = None, receive_time: Any = None, metadata: dict[str, Any] | None = None) -> None:
        provider = _provider_family(source)
        evt = str(event_type or "RAW").upper()
        self.observe(provider, self._event_capability(provider, evt), symbol=symbol)
        with self._lock:
            self._raw_count[provider] += 1
        self._enqueue({
            "kind": "raw", "provider": provider, "source": str(source).upper(),
            "symbol": str(symbol or "UNKNOWN").upper(), "event_type": evt,
            "event_time": _iso(event_time), "receive_time": _iso(receive_time),
            "archived_at": _iso(), "payload": payload, "metadata": dict(metadata or {}),
        })

    def archive_normalized(self, *, source: str, symbol: str, event_type: str, values: Any,
                           event_time: Any = None, receive_time: Any = None,
                           latency_ms: float | None = None, metadata: dict[str, Any] | None = None) -> None:
        provider = _provider_family(source)
        evt = str(event_type or "NORMALIZED").upper()
        self.observe(provider, self._event_capability(provider, evt), symbol=symbol)
        with self._lock:
            self._normalized_count[provider] += 1
        self._enqueue({
            "kind": "normalized", "provider": provider, "source": str(source).upper(),
            "symbol": str(symbol or "UNKNOWN").upper(), "event_type": evt,
            "event_time": _iso(event_time), "receive_time": _iso(receive_time),
            "archived_at": _iso(), "latency_ms": latency_ms,
            "values": values, "metadata": dict(metadata or {}),
        })

    def archive_catalog(self, *, source: str, symbol: str, catalog_type: str,
                        items: Any, metadata: dict[str, Any] | None = None) -> None:
        provider = _provider_family(source)
        cat = str(catalog_type or "CATALOG").upper()
        count = len(items) if isinstance(items, (list, tuple, dict)) else 1
        self.observe(provider, cat, symbol=symbol, count=max(1, count))
        with self._lock:
            self._catalog_counts[provider][cat] = int(count)
        self._enqueue({
            "kind": "catalog", "provider": provider, "source": str(source).upper(),
            "symbol": str(symbol or "UNIVERSE").upper(), "catalog_type": cat,
            "archived_at": _iso(), "items": items, "metadata": dict(metadata or {}),
        })

    def _partition_path(self, row: dict[str, Any]) -> Path:
        kind = str(row.get("kind") or "normalized").lower()
        provider = _safe_name(row.get("provider"))
        stamp = str(row.get("archived_at") or _iso())
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            dt = _utcnow()
        day = dt.strftime("%Y-%m-%d")
        hour = dt.strftime("%H")
        symbol = _safe_name(row.get("symbol"))
        event = _safe_name(row.get("event_type") or row.get("catalog_type") or "event")
        if kind == "catalog":
            d = ROOT / "catalog" / provider / symbol / event
            d.mkdir(parents=True, exist_ok=True)
            return d / "latest.json"
        d = ROOT / kind / provider / day / hour / symbol
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{event}.jsonl"

    def _write_manifest(self) -> None:
        with self._lock:
            payload = {
                "updated_at": _iso(),
                "observed_capabilities": {p: dict(c) for p, c in self._observed.items()},
                "symbols": {p: sorted(v) for p, v in self._symbols.items()},
                "catalog_counts": {p: dict(c) for p, c in self._catalog_counts.items()},
                "raw_count": dict(self._raw_count),
                "normalized_count": dict(self._normalized_count),
                "dropped": self._dropped,
            }
        tmp = MANIFEST.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        tmp.replace(MANIFEST)

    def _writer_loop(self) -> None:
        last_manifest = time.monotonic()
        while True:
            try:
                first = self.queue.get()
                batch = [first]
                for _ in range(1999):
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        _obs_expected('provider_data_lake:end_of_batch')
                        break
                grouped: dict[Path, list[dict[str, Any]]] = defaultdict(list)
                catalogs: list[tuple[Path, dict[str, Any]]] = []
                for row in batch:
                    p = self._partition_path(row)
                    if row.get("kind") == "catalog":
                        catalogs.append((p, row))
                    else:
                        grouped[p].append(row)
                bytes_written = 0
                for p, rows in grouped.items():
                    text = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"), default=_json_default) + "\n" for r in rows)
                    with p.open("a", encoding="utf-8", newline="\n") as fh:
                        fh.write(text)
                    bytes_written += len(text.encode("utf-8"))
                for p, row in catalogs:
                    tmp = p.with_suffix(".tmp")
                    text = json.dumps(row, ensure_ascii=False, indent=2, default=_json_default)
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(p)
                    bytes_written += len(text.encode("utf-8"))
                with self._lock:
                    self._written += len(batch)
                    self._written_bytes += bytes_written
                    self._last_write_at = _iso()
                    self._last_error = ""
                for _ in batch:
                    self.queue.task_done()
                if time.monotonic() - last_manifest > 5.0:
                    try:
                        self._write_manifest()
                    except Exception as _e:
                        _obs_note('provider_data_lake:311', _e)
                    last_manifest = time.monotonic()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                time.sleep(0.05)

    def status(self) -> dict[str, Any]:
        with self._lock:
            observed = {p: dict(v) for p, v in self._observed.items()}
            symbols = {p: sorted(v) for p, v in self._symbols.items()}
            cats = {p: dict(v) for p, v in self._catalog_counts.items()}
            raw = dict(self._raw_count)
            norm = dict(self._normalized_count)
            dropped = int(self._dropped)
            written = int(self._written)
            written_bytes = int(self._written_bytes)
            last_error = self._last_error
            last_write = self._last_write_at
        providers: dict[str, Any] = {}
        for provider, expected in EXPECTED_CAPABILITIES.items():
            obs = observed.get(provider, {})
            met = [x for x in expected if int(obs.get(x, 0)) > 0]
            if not met:
                state = "NO_OBSERVATIONS"
            elif len(met) == len(expected):
                state = "OBSERVED_COMPLETE_FOR_IMPLEMENTED_SCOPE"
            else:
                state = "PARTIAL"
            providers[provider] = {
                "state": state,
                "expected_implemented_capabilities": list(expected),
                "observed_capabilities": obs,
                "capabilities_observed": len(met),
                "capabilities_expected": len(expected),
                "coverage_pct": round(100.0 * len(met) / max(1, len(expected)), 1),
                "symbols_archived": symbols.get(provider, []),
                "raw_records_seen": int(raw.get(provider, 0)),
                "normalized_records_seen": int(norm.get(provider, 0)),
                "catalog_counts": cats.get(provider, {}),
            }
        return {
            "ready": True,
            "root": str(ROOT),
            "architecture": "HOT_LIVE + WARM_UNIVERSE + COLD_DATA_LAKE",
            "scope": "ITM_QUANT_SUPPORTED_UNIVERSE_AND_ACTUALLY_ACCESSED_PROVIDER_DATA",
            "provider_total_external_library_claimed": False,
            "truth_note": "Coverage measures the capabilities and catalogs ITM QUANT actually implements/observes. It never claims undocumented, unauthorized or unrequested provider data.",
            "queue_depth": self.queue.qsize(),
            "queue_capacity": self.queue.maxsize,
            "records_written_this_process": written,
            "bytes_written_this_process": written_bytes,
            "dropped_archive_records": dropped,
            "lossless_archive": dropped == 0,
            "last_write_at": last_write,
            "last_error": last_error,
            "providers": providers,
        }


DATA_LAKE = ProviderDataLake()

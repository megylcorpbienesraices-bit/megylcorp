"""Provider-neutral historical session archive for ITM QUANT.

The archive is organised by instrument + exchange trade date, not by data vendor.
Providers remain provenance attached to each observation.  This lets Replay rebuild the
same instrument from Alpaca, tastytrade/DXLink, or any future provider without encoding
vendor names into the historical contract.

Canonical layout (under persistent ``sessions``)::

    historical/<SYMBOL>/<TRADE_DATE>/
        option_chain/HHMM.csv.gz
        price_ticks/HHMM.csv.gz
        candles/HHMM.csv.gz
        option_trades/HHMM.csv.gz
        flow/HHMM.csv.gz
        large_prints/HHMM.csv.gz
        quant_state/HHMM.csv.gz
        manifest.json

Legacy ``alpaca_<symbol>_history_<date>.csv`` files remain readable during migration,
but new Replay discovery and clocks use this provider-neutral store first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from app.persistence import routed_dir
from .instruments import get as get_instrument
from .obs import note as _obs_note

EC = ZoneInfo("America/Guayaquil")
NY = ZoneInfo("America/New_York")

FRAME_FAMILIES = {
    "option_chain", "price_ticks", "candles", "option_trades", "flow",
    "large_prints", "scanner_inputs", "quant_state", "macro",
}

_DEFAULT_KEYS: dict[str, tuple[str, ...]] = {
    "option_chain": ("timestamp", "contract_symbol", "expiration_date", "strike", "option_type"),
    "price_ticks": ("timestamp", "seq", "exchange", "source"),
    "candles": ("timestamp", "period", "source"),
    "option_trades": ("timestamp", "contract_symbol", "price", "size", "source"),
    "flow": ("timestamp", "contract_symbol", "strike", "premium", "source"),
    "large_prints": ("timestamp", "price", "size", "exchange", "source"),
    "scanner_inputs": ("timestamp", "source"),
    "quant_state": ("timestamp", "state_hash"),
    "macro": ("timestamp", "source"),
}


def _as_aware_ec(value: Any) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    try:
        if ts.tzinfo is None:
            return ts.tz_localize(EC)
        return ts.tz_convert(EC)
    except Exception:
        return None


def _next_weekday(d: date) -> date:
    out = d
    while out.weekday() >= 5:
        out += timedelta(days=1)
    return out


def trade_date_for_timestamp(symbol: str, value: Any) -> date | None:
    """Map an observation timestamp to the exchange trade date.

    CME futures sessions are keyed to the trade date whose session opens at 18:00 ET on
    the previous calendar evening.  This is the critical distinction that prevents the
    Sunday evening leg of Monday's YM session from being archived under Sunday.
    """
    ts_ec = _as_aware_ec(value)
    if ts_ec is None:
        return None
    inst = get_instrument(symbol)
    ts_ny = ts_ec.tz_convert(NY)
    d = ts_ny.date()
    if inst.session == "CME_FUTURES":
        if ts_ny.time() >= time(18, 0):
            return _next_weekday(d + timedelta(days=1))
        # 17:00-18:00 ET is the maintenance break. If an observation appears there,
        # keep it with the current trade date rather than silently moving it forward.
        return _next_weekday(d) if d.weekday() >= 5 else d
    return d


def session_bounds(symbol: str, trade_date: date) -> tuple[datetime, datetime]:
    """Canonical exchange-session bounds returned in platform-local (EC) time."""
    inst = get_instrument(symbol)
    if inst.session == "CME_FUTURES":
        start_ny = datetime.combine(trade_date - timedelta(days=1), time(18, 0), tzinfo=NY)
        end_ny = datetime.combine(trade_date, time(17, 0), tzinfo=NY)
    elif inst.session == "US_INDEX":
        start_ny = datetime.combine(trade_date, time(9, 30), tzinfo=NY)
        end_ny = datetime.combine(trade_date, time(16, 15), tzinfo=NY)
    else:
        start_ny = datetime.combine(trade_date, time(9, 30), tzinfo=NY)
        end_ny = datetime.combine(trade_date, time(16, 0), tzinfo=NY)
    return start_ny.astimezone(EC), end_ny.astimezone(EC)


def _normalise_frame_ts(df: pd.DataFrame, column: str = "timestamp") -> pd.DataFrame:
    if df is None or df.empty or column not in df.columns:
        return pd.DataFrame() if df is None else df.copy()
    out = df.copy()
    raw = pd.to_datetime(out[column], errors="coerce")
    try:
        if getattr(raw.dt, "tz", None) is not None:
            raw = raw.dt.tz_convert(EC).dt.tz_localize(None)
    except Exception as exc:
        _obs_note("historical_store:normalise_ts", exc)
    out[column] = raw
    return out.dropna(subset=[column])


@dataclass
class HistoricalSessionStore:
    storage: Path

    @property
    def root(self) -> Path:
        p = routed_dir(Path(self.storage), "sessions") / "historical"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def session_dir(self, symbol: str, day: date | str) -> Path:
        d = day if isinstance(day, date) else date.fromisoformat(str(day))
        p = self.root / str(symbol).upper() / d.isoformat()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def family_dir(self, symbol: str, day: date | str, family: str) -> Path:
        fam = str(family).strip().lower()
        if fam not in FRAME_FAMILIES:
            raise ValueError(f"Familia histórica no soportada: {family}")
        p = self.session_dir(symbol, day) / fam
        p.mkdir(parents=True, exist_ok=True)
        return p

    def frame_path(self, symbol: str, day: date | str, family: str) -> Path:
        """Legacy single-file path retained only for v1.28 migration reads."""
        fam = str(family).strip().lower()
        if fam not in FRAME_FAMILIES:
            raise ValueError(f"Familia histórica no soportada: {family}")
        return self.session_dir(symbol, day) / f"{fam}.csv.gz"

    @staticmethod
    def _bucket_key(value: Any, minutes: int = 15) -> str:
        ts = pd.Timestamp(value)
        minute = (int(ts.minute) // minutes) * minutes
        return f"{int(ts.hour):02d}{minute:02d}"

    def _manifest_path(self, symbol: str, day: date | str) -> Path:
        return self.session_dir(symbol, day) / "manifest.json"

    def _read_manifest(self, symbol: str, day: date | str) -> dict[str, Any]:
        p = self._manifest_path(symbol, day)
        if not p.exists():
            return {"symbol": str(symbol).upper(), "trade_date": str(day), "families": {}, "providers": []}
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception as exc:
            _obs_note("historical_store:read_manifest", exc, severity="DEGRADED")
            return {}

    def _write_manifest(self, symbol: str, day: date | str, manifest: dict[str, Any]) -> None:
        p = self._manifest_path(symbol, day)
        p.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def append_frame(
        self,
        symbol: str,
        family: str,
        frame: pd.DataFrame,
        *,
        source: str = "UNKNOWN",
        trade_date: date | str | None = None,
        timestamp_column: str = "timestamp",
        dedupe_keys: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        fam = str(family).strip().lower()
        if fam not in FRAME_FAMILIES:
            raise ValueError(f"Familia histórica no soportada: {family}")
        if frame is None or frame.empty:
            return {"written": 0, "new_rows": 0, "family": fam, "reason": "empty"}
        df = _normalise_frame_ts(frame, timestamp_column)
        if df.empty:
            return {"written": 0, "new_rows": 0, "family": fam, "reason": "no valid timestamps"}
        if timestamp_column != "timestamp":
            df = df.rename(columns={timestamp_column: "timestamp"})
        df["source"] = df.get("source", pd.Series(index=df.index, dtype=object)).fillna(str(source)).replace("", str(source))
        df["archive_source"] = str(source)

        if trade_date is not None:
            d = trade_date if isinstance(trade_date, date) else date.fromisoformat(str(trade_date))
            groups = [(d, df)]
        else:
            tagged = []
            for idx, ts in df["timestamp"].items():
                td = trade_date_for_timestamp(symbol, ts)
                if td is not None:
                    tagged.append((idx, td))
            if not tagged:
                return {"written": 0, "new_rows": 0, "family": fam, "reason": "no trade date"}
            tag_map = dict(tagged)
            df = df.loc[list(tag_map)].copy()
            df["trade_date"] = [tag_map[i].isoformat() for i in df.index]
            groups = [(date.fromisoformat(day), part.drop(columns=["trade_date"])) for day, part in df.groupby("trade_date", sort=True)]

        totals = {"written": 0, "new_rows": 0, "family": fam, "sessions": []}
        for d, part in groups:
            # Partition by 15-minute event-time buckets. Only touched buckets are
            # rewritten; immutable earlier buckets are represented by manifest metadata.
            # This prevents the full-session O(n²) gzip rewrite pattern.
            part = part.copy()
            part["_bucket"] = [self._bucket_key(x) for x in part["timestamp"]]
            manifest = self._read_manifest(symbol, d)
            families = dict(manifest.get("families") or {})
            prior_family = dict(families.get(fam) or {})
            partition_meta = dict(prior_family.get("partition_meta") or {})
            session_new = 0
            touched: list[str] = []
            for bucket, bucket_part in part.groupby("_bucket", sort=True):
                bucket_part = bucket_part.drop(columns=["_bucket"])
                p = self.family_dir(symbol, d, fam) / f"{bucket}.csv.gz"
                before = 0
                if p.exists():
                    try:
                        prev = pd.read_csv(p, compression="gzip")
                        prev = _normalise_frame_ts(prev)
                        before = len(prev)
                        bucket_part = pd.concat([prev, bucket_part], ignore_index=True, sort=False)
                    except Exception as exc:
                        _obs_note("historical_store:read_existing_bucket", exc, severity="DEGRADED")
                keys = list(dedupe_keys or _DEFAULT_KEYS.get(fam, ("timestamp", "source")))
                keys = [k for k in keys if k in bucket_part.columns]
                if keys:
                    bucket_part = bucket_part.drop_duplicates(subset=keys, keep="last")
                bucket_part = bucket_part.sort_values("timestamp")
                p.parent.mkdir(parents=True, exist_ok=True)
                bucket_part.to_csv(p, index=False, compression="gzip")
                new_rows = max(0, len(bucket_part) - before)
                session_new += int(new_rows)
                touched.append(p.name)
                partition_meta[p.name] = {
                    "rows": int(len(bucket_part)),
                    "first": pd.Timestamp(bucket_part["timestamp"].min()).isoformat() if len(bucket_part) else None,
                    "last": pd.Timestamp(bucket_part["timestamp"].max()).isoformat() if len(bucket_part) else None,
                }

            family_rows = int(sum(int((v or {}).get("rows") or 0) for v in partition_meta.values()))
            starts = [pd.Timestamp(v.get("first")) for v in partition_meta.values() if (v or {}).get("first")]
            ends = [pd.Timestamp(v.get("last")) for v in partition_meta.values() if (v or {}).get("last")]
            first_ts = min(starts) if starts else None
            last_ts = max(ends) if ends else None
            totals["written"] += family_rows
            totals["new_rows"] += int(session_new)
            totals["sessions"].append(d.isoformat())

            providers = set(str(x) for x in manifest.get("providers", []) if x)
            providers.update(str(x) for x in part.get("source", pd.Series(dtype=str)).dropna().astype(str) if x)
            families[fam] = {
                "rows": family_rows,
                "first": pd.Timestamp(first_ts).isoformat() if first_ts is not None else None,
                "last": pd.Timestamp(last_ts).isoformat() if last_ts is not None else None,
                "path": f"{fam}/*.csv.gz",
                "partitions": int(len(partition_meta)),
                "bucket_minutes": 15,
                "touched": touched,
                "partition_meta": partition_meta,
            }
            manifest.update({
                "schema": 2,
                "symbol": str(symbol).upper(),
                "trade_date": d.isoformat(),
                "providers": sorted(providers),
                "families": families,
                "updated_at": datetime.now(tz=EC).isoformat(),
                "session_model": get_instrument(symbol).session,
            })
            self._write_manifest(symbol, d, manifest)
        return totals

    def append_quant_state(self, symbol: str, timestamp: Any, state: dict[str, Any], *, source: str = "ITM_QUANT") -> dict[str, Any]:
        try:
            payload = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        except Exception as exc:
            _obs_note("historical_store:serialize_quant_state", exc, severity="DEGRADED")
            payload = json.dumps({"status": "SERIALIZATION_FAILED"})
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
        df = pd.DataFrame([{"timestamp": timestamp, "state_hash": digest, "payload_json": payload, "source": source}])
        return self.append_frame(symbol, "quant_state", df, source=source)

    def load_frame(self, symbol: str, day: date | str, family: str, *, asof: Any = None) -> pd.DataFrame:
        fam = str(family).strip().lower()
        files = sorted(self.family_dir(symbol, day, fam).glob("*.csv.gz"))
        legacy = self.frame_path(symbol, day, fam)
        if legacy.exists():
            files = [legacy] + files
        if not files:
            return pd.DataFrame()
        frames = []
        cut = _as_aware_ec(asof) if asof is not None else None
        naive_cut = cut.tz_localize(None) if cut is not None else None
        for p in files:
            try:
                df = pd.read_csv(p, compression="gzip")
                df = _normalise_frame_ts(df)
                if naive_cut is not None and not df.empty:
                    df = df[pd.to_datetime(df["timestamp"], errors="coerce") <= naive_cut].copy()
                if not df.empty:
                    frames.append(df)
            except Exception as exc:
                _obs_note("historical_store:load_frame", exc, severity="DEGRADED")
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True, sort=False)
        keys = [k for k in _DEFAULT_KEYS.get(fam, ("timestamp", "source")) if k in out.columns]
        if keys:
            out = out.drop_duplicates(subset=keys, keep="last")
        return out.sort_values("timestamp").reset_index(drop=True)

    def available_sessions(self, symbol: str) -> list[str]:
        root = self.root / str(symbol).upper()
        if not root.exists():
            return []
        out = []
        for p in root.iterdir():
            if not p.is_dir():
                continue
            try:
                date.fromisoformat(p.name)
                if any(x.is_file() for x in p.iterdir()):
                    out.append(p.name)
            except Exception as exc:
                _obs_note("historical_store:session_discovery", exc, severity="DEGRADED")
                continue
        return sorted(set(out), reverse=True)

    def family_counts(self, symbol: str, day: date | str) -> dict[str, int]:
        m = self._read_manifest(symbol, day)
        return {k: int((v or {}).get("rows") or 0) for k, v in (m.get("families") or {}).items()}

    def session_clock(self, symbol: str, day: date | str, *, step_minutes: float = 1.0) -> dict[str, Any]:
        d = day if isinstance(day, date) else date.fromisoformat(str(day))
        timestamps: list[pd.Timestamp] = []
        family_ts: dict[str, pd.Series] = {}
        for fam in ("price_ticks", "candles", "option_chain", "quant_state", "option_trades", "flow", "large_prints"):
            df = self.load_frame(symbol, d, fam)
            if not df.empty and "timestamp" in df.columns:
                fam_ts = pd.Series(pd.to_datetime(df["timestamp"], errors="coerce")).dropna().sort_values().drop_duplicates()
                if not fam_ts.empty:
                    family_ts[fam] = fam_ts
                    timestamps.extend(fam_ts.tolist())
        if not timestamps:
            return {"ready": False, "reason": "sin observaciones históricas proveedor-independientes", "trade_date": d.isoformat()}
        ts = pd.Series(pd.to_datetime(timestamps, errors="coerce")).dropna().sort_values().drop_duplicates()
        if ts.empty:
            return {"ready": False, "reason": "sin timestamps válidos", "trade_date": d.isoformat()}
        observed_start, end = ts.iloc[0], ts.iloc[-1]
        # Full quantitative replay requires at least one option-chain snapshot. Price
        # history may legitimately begin earlier (e.g. DXLink Candle backfill). Start
        # the global scrubber at the first quant-reconstructible instant so choosing a
        # date never lands on a clock where the whole quant bundle must fail. Earlier
        # candles remain available as visual context behind that first causal state.
        quant_series = family_ts.get("option_chain")
        quant_start = quant_series.iloc[0] if quant_series is not None and not quant_series.empty else None
        start = quant_start if quant_start is not None else observed_start
        step = pd.Timedelta(minutes=max(float(step_minutes), 0.25))
        marks: list[str] = []
        cursor = start
        while cursor <= end:
            marks.append(pd.Timestamp(cursor).isoformat())
            cursor += step
        if not marks or marks[-1] != pd.Timestamp(end).isoformat():
            marks.append(pd.Timestamp(end).isoformat())
        gaps = ts.diff().dt.total_seconds().dropna()
        canonical_start, canonical_end = session_bounds(symbol, d)
        return {
            "ready": True,
            "trade_date": d.isoformat(),
            "session_model": get_instrument(symbol).session,
            "start": pd.Timestamp(start).isoformat(),
            "end": pd.Timestamp(end).isoformat(),
            "observed_start": pd.Timestamp(observed_start).isoformat(),
            "quant_start": pd.Timestamp(quant_start).isoformat() if quant_start is not None else None,
            "quant_ready": bool(quant_start is not None),
            "canonical_start": canonical_start.replace(tzinfo=None).isoformat(),
            "canonical_end": canonical_end.replace(tzinfo=None).isoformat(),
            "snapshots": int(len(ts)),
            "marks": marks,
            "median_gap_seconds": round(float(gaps.median()), 1) if len(gaps) else None,
            "max_gap_seconds": round(float(gaps.max()), 1) if len(gaps) else None,
            "families": self.family_counts(symbol, d),
        }

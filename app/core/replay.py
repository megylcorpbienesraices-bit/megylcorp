"""Global causal replay, tape archive and backtest coverage (v1.16.4).

REPLAY = one symbol/date/clock shared by every visible panel.
REVIEW = whole saved session (no clock).
BACKTEST = date range / research coverage, not a chart mode.

No future rule: data are cut at ``asof`` before analysis.  If a historical input
cannot be reconstructed causally, callers must mark it unavailable instead of
substituting a current value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo

import pandas as pd
from app.persistence import routed_dir
from .frame_guards import numeric_column
from .obs import note as _obs_note
from .historical_session_store import HistoricalSessionStore

EC = ZoneInfo("America/Guayaquil")
NY = ZoneInfo("America/New_York")

TAPE_COLUMNS = (
    "seq", "timestamp", "price", "size", "bid", "ask", "bid_size", "ask_size",
    "aggressor_sign", "signed_volume", "exchange", "conditions", "tape",
)


def _tape_path(storage: Path, symbol: str, day: date) -> Path:
    return routed_dir(Path(storage), "tape_archive") / f"tape_{str(symbol).lower()}_{day.isoformat()}.csv.gz"


def _normalise_ts(s: pd.Series) -> pd.Series:
    """Return naive local timestamps, matching the rest of ITM QUANT storage."""
    ts = pd.to_datetime(s, errors="coerce")
    try:
        if getattr(ts.dt, "tz", None) is not None:
            ts = ts.dt.tz_convert(EC).dt.tz_localize(None)
    except Exception as _e:
        _obs_note('replay:44', _e)
    return ts


def _coerce_asof(asof: datetime | str | pd.Timestamp | None) -> pd.Timestamp | None:
    if asof is None or asof == "":
        return None
    try:
        t = pd.Timestamp(asof)
        if t.tzinfo is not None:
            t = t.tz_convert(EC).tz_localize(None)
        return t
    except Exception:
        return None


def filter_asof(df: pd.DataFrame, asof: datetime | str | None,
                column: str = "timestamp") -> pd.DataFrame:
    """Causal choke point: remove rows strictly after the global replay clock."""
    if df is None:
        return pd.DataFrame()
    if df.empty or asof is None or column not in df.columns:
        return df.copy()
    cut = _coerce_asof(asof)
    if cut is None:
        return df.copy()
    out = df.copy()
    ts = _normalise_ts(out[column])
    return out.loc[ts.notna() & (ts <= cut)].copy()


def _serialise_conditions(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v if v is not None else [], ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(v or "")


def persist_tape(storage: Path, symbol: str, ticks: pd.DataFrame,
                 day: date | None = None) -> Dict[str, Any]:
    """Persist SIP tape incrementally with robust deduplication.

    ``seq`` is preferred when present because two legitimate trades can share timestamp,
    price and size.  The fallback key therefore includes venue/conditions/tape rather than
    collapsing trades on only three fields.
    """
    out: Dict[str, Any] = {"written": 0, "new_rows": 0, "path": None, "reason": None,
                           "dedupe_key": None}
    if ticks is None or ticks.empty or "timestamp" not in ticks.columns:
        out["reason"] = "sin ticks"
        return out
    t = ticks.copy()
    t["timestamp"] = _normalise_ts(t["timestamp"])
    t = t.dropna(subset=["timestamp"]).sort_values("timestamp")
    if t.empty:
        out["reason"] = "sin timestamps válidos"
        return out
    if "conditions" in t.columns:
        t["conditions"] = t["conditions"].map(_serialise_conditions)
    day = day or pd.Timestamp(t["timestamp"].iloc[-1]).date()
    # Never write ticks from a different local date into the requested archive.
    t = t[pd.to_datetime(t["timestamp"], errors="coerce").dt.date == day].copy()
    if t.empty:
        out["reason"] = "sin ticks de la sesión solicitada"
        return out
    cols = [c for c in TAPE_COLUMNS if c in t.columns]
    t = t[cols]
    p = _tape_path(storage, symbol, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    before = 0
    try:
        if p.exists():
            prev = pd.read_csv(p, compression="gzip")
            if "timestamp" in prev:
                prev["timestamp"] = _normalise_ts(prev["timestamp"])
            before = len(prev)
            # Pandas 2.x warns that concat dtype inference with empty/all-NA columns
            # will change. Drop only all-NA columns for inference, concatenate the
            # observed fields, then restore the exact union schema.
            union_cols = list(dict.fromkeys([*prev.columns.tolist(), *t.columns.tolist()]))
            concat_parts = [frame.dropna(axis=1, how="all") for frame in (prev, t) if not frame.empty]
            t = pd.concat(concat_parts, ignore_index=True, sort=False).reindex(columns=union_cols)
        seq = numeric_column(t,"seq",float("nan")) if "seq" in t.columns else pd.Series(dtype=float)
        if len(seq) and seq.notna().any() and (seq.fillna(0) > 0).any():
            # local stream sequence is unique within the selected symbol/session; timestamp
            # guards against a process restart that resets seq during the same day.
            keys = [c for c in ("timestamp", "seq") if c in t.columns]
            out["dedupe_key"] = "+".join(keys)
        else:
            keys = [c for c in ("timestamp", "price", "size", "exchange", "conditions", "tape") if c in t.columns]
            out["dedupe_key"] = "+".join(keys)
        t = t.drop_duplicates(subset=keys, keep="last").sort_values([c for c in ("timestamp", "seq") if c in t.columns])
        t.to_csv(p, index=False, compression="gzip")
        out.update({"written": int(len(t)), "new_rows": max(0, int(len(t) - before)), "path": str(p)})
        # v1.28 canonical archive: provider-neutral by symbol/trade-date. The legacy
        # tape file is retained only as a migration/compatibility mirror.
        try:
            # Do NOT force the local calendar date into the canonical archive.
            # For CME futures (YM/MYM), Sunday 18:00 ET belongs to Monday's trade
            # date. HistoricalSessionStore maps every tick by exchange session.
            canon = HistoricalSessionStore(Path(storage)).append_frame(
                symbol, "price_ticks", t, source="ITM_QUANT_TAPE",
                dedupe_keys=[c for c in ("timestamp","seq","exchange","source") if c in t.columns or c=="source"],
            )
            out["canonical"] = canon
        except Exception as canon_exc:
            _obs_note("replay:persist_tape_canonical", canon_exc, severity="DEGRADED")
    except Exception as exc:
        out["reason"] = str(exc)[:180]
    return out


def load_tape(storage: Path, symbol: str, day: date,
              asof: datetime | str | None = None) -> pd.DataFrame:
    # Canonical archive first; legacy tape remains a migration fallback.
    canonical = HistoricalSessionStore(Path(storage)).load_frame(symbol, day, "price_ticks", asof=asof)
    if not canonical.empty:
        for c in ("price", "size", "bid", "ask", "bid_size", "ask_size", "aggressor_sign", "signed_volume", "seq"):
            if c in canonical.columns:
                canonical[c] = pd.to_numeric(canonical[c], errors="coerce")
        return canonical
    p = _tape_path(storage, symbol, day)
    if not p.exists():
        return pd.DataFrame(columns=list(TAPE_COLUMNS))
    try:
        t = pd.read_csv(p, compression="gzip")
        if "timestamp" not in t.columns:
            return pd.DataFrame(columns=list(TAPE_COLUMNS))
        t["timestamp"] = _normalise_ts(t["timestamp"])
        t = t.dropna(subset=["timestamp"]).sort_values([c for c in ("timestamp", "seq") if c in t.columns])
        for c in ("price", "size", "bid", "ask", "bid_size", "ask_size", "aggressor_sign", "signed_volume", "seq"):
            if c in t.columns:
                t[c] = pd.to_numeric(t[c], errors="coerce")
        return filter_asof(t, asof)
    except Exception:
        return pd.DataFrame(columns=list(TAPE_COLUMNS))


def available_sessions(storage: Path, symbol: str) -> List[str]:
    """Return provider-neutral sessions plus readable legacy archives.

    New sessions live under ``historical/<SYMBOL>/<TRADE_DATE>``. Legacy Alpaca-named
    files remain discoverable during migration so accumulated quantitative memory is not
    thrown away merely because the storage contract was corrected.
    """
    sym = str(symbol).lower()
    days: List[str] = list(HistoricalSessionStore(Path(storage)).available_sessions(symbol))
    for p in routed_dir(Path(storage), "sessions").glob(f"alpaca_{sym}_history_*.csv"):
        stem = p.stem.replace(f"alpaca_{sym}_history_", "")
        try:
            date.fromisoformat(stem)
            days.append(stem)
        except Exception as _e:
            _obs_note('replay:available_sessions_legacy', _e)
            continue
    return sorted(set(days), reverse=True)


def load_structural_history(storage: Path, symbol: str, day: date, asof: datetime | str | None = None) -> pd.DataFrame:
    """Load the canonical historical option-chain snapshots, with legacy fallback."""
    store = HistoricalSessionStore(Path(storage))
    df = store.load_frame(symbol, day, "option_chain", asof=asof)
    if not df.empty:
        return df
    hp = routed_dir(Path(storage), "sessions") / f"alpaca_{str(symbol).lower()}_history_{day.isoformat()}.csv"
    if not hp.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(hp)
        if "timestamp" in df.columns:
            df["timestamp"] = _normalise_ts(df["timestamp"])
            df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
        return filter_asof(df, asof)
    except Exception as exc:
        _obs_note('replay:load_structural_history_legacy', exc, severity="DEGRADED")
        return pd.DataFrame()


@dataclass(frozen=True)
class ReplayContext:
    day: date | None = None
    asof: datetime | None = None
    symbol: str = "UNKNOWN"

    @classmethod
    def live(cls, symbol: str = "UNKNOWN") -> "ReplayContext":
        return cls(None, None, str(symbol or "UNKNOWN").upper())

    @classmethod
    def at(cls, day: date | str, asof: datetime | str | None = None,
           symbol: str = "UNKNOWN") -> "ReplayContext":
        d = day if isinstance(day, date) else date.fromisoformat(str(day))
        a = _coerce_asof(asof)
        # User-facing replay time is interpreted in the platform local clock if no tz is supplied.
        adt = None if a is None else a.to_pydatetime()
        return cls(d, adt, str(symbol or "UNKNOWN").upper())

    @property
    def is_replay(self) -> bool:
        return self.day is not None

    @property
    def is_review(self) -> bool:
        return self.day is not None and self.asof is None

    @property
    def mode(self) -> str:
        """Derived global replay mode used by API/cache contracts.

        Keep this derived instead of storing a mutable/default ``mode`` field: contexts
        created with :meth:`at` must never accidentally remain LIVE.
        """
        if not self.is_replay:
            return "LIVE"
        return "REVIEW" if self.is_review else "REPLAY"

    def describe(self) -> Dict[str, Any]:
        if not self.is_replay:
            return {"mode": "LIVE", "date": None, "asof": None, "symbol": self.symbol,
                    "banner": "EN VIVO", "causal": True,
                    "note": "Todos los paneles muestran el estado LIVE actual."}
        clock = self.asof.strftime("%H:%M:%S") if self.asof else "SESIÓN COMPLETA"
        return {"mode": "REVIEW" if self.is_review else "REPLAY",
                "date": self.day.isoformat(), "asof": self.asof.isoformat() if self.asof else None,
                "symbol": self.symbol, "banner": f"{'REVIEW' if self.is_review else 'REPLAY'} · {self.day.isoformat()} · {clock}",
                "causal": not self.is_review,
                "note": ("Todos los paneles usan el mismo corte temporal; ningún dato posterior al reloj puede entrar."
                         if self.asof else "Review de sesión completa: sirve para estudiar qué pasó, no para juzgar una decisión en tiempo real.")}


def _first_structural_timestamp(storage: Path, symbol: str, day: date):
    """Primer instante del día con cadena de opciones archivada.

    Reproducir estructura (GEX, DEX, muros) exige al menos un snapshot de cadena.
    El precio puede empezar antes; empezar ahí el reloj deja al motor sin nada que
    reconstruir en la primera marca.
    """
    try:
        h = load_structural_history(Path(storage), symbol, day, None)
        if h is None or getattr(h, "empty", True) or "timestamp" not in h.columns:
            return None
        ts = _normalise_ts(h["timestamp"]).dropna()
        return None if ts.empty else ts.min()
    except Exception as exc:
        _obs_note("replay:first_structural_timestamp", exc)
        return None


def session_clock(storage: Path, symbol: str, day: date,
                  step_minutes: float = 1.0) -> Dict[str, Any]:
    # Canonical provider-neutral clock first. This also understands CME trade dates,
    # including the prior-evening leg of YM/MYM sessions.
    canonical = HistoricalSessionStore(Path(storage)).session_clock(symbol, day, step_minutes=step_minutes)
    if canonical.get("ready"):
        return canonical
    # Migration fallback for sessions captured before v1.28.0.
    hp = routed_dir(Path(storage), "sessions") / f"alpaca_{str(symbol).lower()}_history_{day.isoformat()}.csv"
    if not hp.exists():
        return {"ready": False, "reason": "sin histórico para esa fecha"}
    try:
        h = pd.read_csv(hp, usecols=["timestamp"])
        ts = _normalise_ts(h["timestamp"]).dropna().sort_values().drop_duplicates()
        if ts.empty:
            return {"ready": False, "reason": "sin timestamps"}
    except Exception as exc:
        return {"ready": False, "reason": str(exc)[:140]}
    # El reloj arranca en el primer instante RECONSTRUIBLE, no en el primer dato.
    # El fichero de historia puede empezar la tarde anterior (sesión extendida,
    # backfill de velas) mientras la cadena de opciones todavía no existe: elegir esa
    # primera marca hacía que el motor rechazara el replay con "No hay snapshots
    # hasta el reloj seleccionado" en cuanto se abría la sesión.
    start, end = ts.iloc[0], ts.iloc[-1]
    structural = _first_structural_timestamp(storage, symbol, day)
    if structural is not None and start <= structural <= end:
        start = structural
    step = pd.Timedelta(minutes=float(max(step_minutes, 0.25)))
    marks: List[str] = []
    t = start
    while t <= end:
        marks.append(t.isoformat())
        t += step
    if not marks or marks[-1] != end.isoformat():
        marks.append(end.isoformat())
    gaps = ts.diff().dt.total_seconds().dropna()
    return {"ready": True, "start": start.isoformat(), "end": end.isoformat(),
            "observed_start": ts.iloc[0].isoformat(),
            "structural_start": None if structural is None else structural.isoformat(),
            "trade_date": day.isoformat(), "legacy": True,
            "snapshots": int(len(ts)), "marks": marks,
            "median_gap_seconds": round(float(gaps.median()), 1) if len(gaps) else None,
            "max_gap_seconds": round(float(gaps.max()), 1) if len(gaps) else None}


def _session_minutes_for(symbol: str) -> float:
    try:
        from .instruments import get
        return float(get(symbol).minutes_per_year / 252.0)
    except Exception:
        return 390.0


def session_coverage(storage: Path, symbol: str, days: Iterable[str]) -> List[Dict[str, Any]]:
    """Coverage is temporal, not a magic snapshot count."""
    expected = max(_session_minutes_for(symbol), 1.0)
    rows: List[Dict[str, Any]] = []
    for raw in days:
        try:
            day = date.fromisoformat(str(raw))
        except Exception as _e:
            _obs_note('replay:269', _e)
            continue
        snaps = 0; first = last = None; span = 0.0; median_gap = max_gap = None
        try:
            clk = session_clock(storage, symbol, day, step_minutes=1.0)
            if clk.get("ready"):
                first = pd.Timestamp(clk.get("start")); last = pd.Timestamp(clk.get("end"))
                snaps = int(clk.get("snapshots") or 0)
                span = max(0.0, (last-first).total_seconds()/60.0)
                median_gap = clk.get("median_gap_seconds"); max_gap = clk.get("max_gap_seconds")
        except Exception as _e:
            _obs_note('replay:coverage_clock', _e)
        pct = min(100.0, 100.0 * span / expected) if snaps else 0.0
        if pct >= 80.0:
            quality, usable = "FULL", True
        elif pct >= 40.0:
            quality, usable = "PARTIAL", False
        else:
            quality, usable = "INSUFFICIENT", False
        tp = _tape_path(storage, symbol, day)
        note = None
        if quality == "PARTIAL": note = "Cobertura parcial: visible para review, excluida del rango calibrable."
        if quality == "INSUFFICIENT": note = "Cobertura temporal insuficiente para un replay/calibración representativos."
        rows.append({"date": day.isoformat(), "snapshots": snaps,
                     "first_snapshot": first.isoformat() if first is not None else None,
                     "last_snapshot": last.isoformat() if last is not None else None,
                     "coverage_minutes": round(span, 1), "expected_session_minutes": round(expected, 1),
                     "coverage_pct": round(pct, 1), "median_gap_seconds": None if median_gap is None else round(median_gap,1),
                     "max_gap_seconds": None if max_gap is None else round(max_gap,1),
                     "coverage_class": quality, "usable": usable, "note": note,
                     "tape_archived": tp.exists(), "tape_bytes": tp.stat().st_size if tp.exists() else 0})
    return rows


def _range_signal_count(storage: Path, symbol: str, start: date, end: date) -> int:
    total = 0
    for p in sorted(routed_dir(Path(storage), "scanner_history").glob(f"scanner_history_{str(symbol).lower()}_*.csv")):
        try:
            day = date.fromisoformat(p.stem.rsplit("_", 1)[-1])
            if not (start <= day <= end): continue
            x = pd.read_csv(p)
            if "mode" in x.columns:
                x = x[x["mode"].astype(str).str.upper() != "DEMO"]
            total += int(len(x))
        except Exception as _e:
            _obs_note('replay:317', _e)
    return total


def backtest_range(storage: Path, symbol: str, start: str, end: str,
                   expiry_mode: str = "AUTO") -> Dict[str, Any]:
    try:
        a, b = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except Exception:
        return {"ready": False, "reason": "rango de fechas inválido"}
    if b < a: a, b = b, a
    days = [d for d in available_sessions(storage, symbol) if a <= date.fromisoformat(d) <= b]
    cov = session_coverage(storage, symbol, days)
    usable = [c for c in cov if c["usable"]]
    dropped = [c for c in cov if not c["usable"]]
    signals = _range_signal_count(storage, symbol, a, b)
    try:
        from .calibration import calibration_report
        current = calibration_report(Path(storage), symbol, probability_expiry_mode=expiry_mode)
        pm = current.get("probability_model") or {}
        model = {"status": current.get("status"), "sample_size": current.get("sample_size", 0),
                 "stage": pm.get("stage") or pm.get("status"), "ready": bool(pm.get("ready")),
                 "reason": pm.get("reason") or current.get("reason")}
    except Exception as exc:
        model = {"status": "UNAVAILABLE", "stage": "UNAVAILABLE", "ready": False, "reason": str(exc)[:140]}
    min_sessions = 8; min_signals = 120
    sample_cov = {"sessions": len(usable), "sessions_required": min_sessions,
                  "signals_logged": int(signals), "signals_required": min_signals,
                  "minimum_counts_met": len(usable) >= min_sessions and signals >= min_signals}
    return {"ready": bool(usable), "start": a.isoformat(), "end": b.isoformat(),
            "symbol": str(symbol).upper(), "sessions_found": len(cov), "sessions_usable": len(usable),
            "sessions_dropped": len(dropped), "dropped_detail": dropped[:50],
            "sessions_with_tape": sum(1 for c in usable if c["tape_archived"]),
            "total_snapshots": int(sum(c["snapshots"] for c in usable)),
            "dates": [c["date"] for c in usable], "sample_coverage": sample_cov,
            "current_calibration_model": model,
            "note": "Cobertura de rango y readiness del modelo son cosas distintas. Un mínimo de sesiones/señales no promueve el modelo; la promoción real exige validación OOS del Calibration Engine."}

from __future__ import annotations

import io
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from app.persistence import PERSISTENT_ROOT, routed_dir
from concurrent.futures import ThreadPoolExecutor, as_completed
from .obs import note as _obs_note

EC = ZoneInfo("America/Guayaquil")
NY = ZoneInfo("America/New_York")
BASE = Path(__file__).resolve().parent
STORE = routed_dir(PERSISTENT_ROOT, "research")
STORE.mkdir(parents=True, exist_ok=True)
CACHE = STORE / "macro_dia_cache.json"

# Public official / public-data sources. No paid API is required.
FRED_SERIES = {
    "DGS2": {"label": "Treasury 2Y", "unit": "%", "kind": "rate"},
    "DGS10": {"label": "Treasury 10Y", "unit": "%", "kind": "rate"},
    "DGS30": {"label": "Treasury 30Y", "unit": "%", "kind": "rate"},
    "T10Y2Y": {"label": "Curva 10Y-2Y", "unit": "pp", "kind": "curve"},
    "DFF": {"label": "Fed Funds efectivo", "unit": "%", "kind": "rate"},
    "BAMLH0A0HYM2": {"label": "HY OAS", "unit": "%", "kind": "credit"},
    "NFCI": {"label": "Chicago Fed NFCI", "unit": "", "kind": "conditions"},
    "CPIAUCSL": {"label": "CPI", "unit": "index", "kind": "macro"},
    "UNRATE": {"label": "Desempleo", "unit": "%", "kind": "macro"},
}

HIGH_IMPACT_WORDS = (
    "consumer price index", "employment situation", "producer price index",
    "job openings and labor turnover", "employment cost index",
    "productivity and costs", "import and export price", "real earnings",
)


def _safe_num(v, default=None):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _fred_csv(series_id: str, timeout: float = 12.0) -> pd.DataFrame:
    # FRED's public chart CSV endpoint does not require an API key and is sufficient
    # for a local dashboard. It returns the public observation history for a series.
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    r = requests.get(url, params={"id": series_id}, timeout=timeout, headers={"User-Agent": "ITM-QUANT-DIA/1.4"})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    date_col = "DATE" if "DATE" in df.columns else "observation_date" if "observation_date" in df.columns else None
    if df.empty or date_col is None:
        raise RuntimeError(f"FRED no devolvió datos válidos para {series_id}")
    val_col = series_id if series_id in df.columns else [c for c in df.columns if c != date_col][0]
    df = df.rename(columns={val_col: "value"})
    df["date"] = pd.to_datetime(df[date_col], errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    out = df[["date", "value"]].dropna().sort_values("date")
    try:
        from .provider_data_lake import DATA_LAKE
        DATA_LAKE.archive_catalog(source="FRED", symbol=series_id, catalog_type="FRED",
                                  items=[{"date": r.date.date().isoformat(), "value": float(r.value)} for r in out.itertuples()],
                                  metadata={"series_id": series_id, "scope": "PUBLIC_SERIES_HISTORY_RETURNED"})
    except Exception as _e:
        _obs_note('macro_dia:73', _e)
    return out


def _robust_z(s: pd.Series, x: float) -> float:
    s = pd.to_numeric(s, errors="coerce").dropna()
    if len(s) < 12 or not math.isfinite(x):
        return 0.0
    med = float(s.median())
    mad = float((s - med).abs().median())
    if mad > 1e-12:
        return float(0.67448975 * (x - med) / mad)
    sd = float(s.std(ddof=0))
    return 0.0 if sd <= 1e-12 else float((x - float(s.mean())) / sd)


def _series_payload(series_id: str, df: pd.DataFrame) -> Dict[str, Any]:
    meta = FRED_SERIES[series_id]
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    lookback = df.tail(260)
    x = float(latest["value"])
    change = x - float(prev["value"])
    change_5 = x - float(df.iloc[-6]["value"]) if len(df) >= 6 else change
    z = _robust_z(lookback["value"], x)
    pct = float((lookback["value"] <= x).mean() * 100.0) if len(lookback) else 50.0
    return {
        "id": series_id,
        "label": meta["label"],
        "unit": meta["unit"],
        "kind": meta["kind"],
        "value": x,
        "date": latest["date"].date().isoformat(),
        "change_1": change,
        "change_5": change_5,
        "z": z,
        "percentile": pct,
        "history": [{"date": r.date.date().isoformat(), "value": float(r.value)} for r in lookback.tail(120).itertuples()],
    }


def _parse_ics_datetime(raw: str) -> datetime | None:
    raw = raw.strip()
    # BLS calendar times are effectively Eastern; convert to NY-aware.
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            d = datetime.strptime(raw.rstrip("Z"), fmt)
            if "T" not in raw:
                d = d.replace(hour=8, minute=30)
            return d.replace(tzinfo=NY)
        except Exception as _e:
            _obs_note('macro_dia:124', _e)
    return None


def _bls_events(timeout: float = 12.0) -> List[Dict[str, Any]]:
    url = "https://www.bls.gov/schedule/news_release/bls.ics"
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "ITM-QUANT-DIA/1.4"})
    r.raise_for_status()
    text = r.text.replace("\r\n ", "")
    blocks = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.S | re.I)
    now = datetime.now(NY)
    out = []
    for b in blocks:
        mdt = re.search(r"DTSTART(?:;[^:]*)?:(\S+)", b)
        ms = re.search(r"SUMMARY:(.+)", b)
        if not mdt or not ms:
            continue
        dt = _parse_ics_datetime(mdt.group(1))
        if dt is None or dt < now - timedelta(hours=2) or dt > now + timedelta(days=14):
            continue
        title = ms.group(1).replace("\\,", ",").strip()
        high = any(w in title.lower() for w in HIGH_IMPACT_WORDS)
        out.append({
            "source": "BLS",
            "title": title,
            "time_ny": dt.isoformat(),
            "time_ec": dt.astimezone(EC).isoformat(),
            "impact": "HIGH" if high else "MEDIUM",
        })
    out = sorted(out, key=lambda x: x["time_ny"])[:30]
    try:
        from .provider_data_lake import DATA_LAKE
        DATA_LAKE.archive_catalog(source="BLS", symbol="MACRO", catalog_type="BLS", items=out, metadata={"scope": "OFFICIAL_RELEASE_CALENDAR"})
    except Exception as _e:
        _obs_note('macro_dia:158', _e)
    return out


def _fed_events(timeout: float = 12.0) -> List[Dict[str, Any]]:
    # Official Federal Reserve monthly calendar. We intentionally keep this parser
    # conservative: only explicit FOMC Meeting / Press Conference / Beige Book items.
    now = datetime.now(NY)
    out: List[Dict[str, Any]] = []
    for month_dt in (now, (now + timedelta(days=31))):
        slug = month_dt.strftime("%Y-%B").lower()
        url = f"https://www.federalreserve.gov/newsevents/{slug}.htm"
        try:
            r = requests.get(url, timeout=timeout, headers={"User-Agent": "ITM-QUANT-DIA/1.4"})
            if r.status_code >= 400:
                continue
            html = re.sub(r"\s+", " ", r.text)
            # Capture a small context around explicit event names, then infer day + time.
            for label, impact in (("FOMC Meeting", "HIGH"), ("FOMC Press Conference", "HIGH"), ("Beige Book", "MEDIUM")):
                for m in re.finditer(re.escape(label), html, flags=re.I):
                    ctx = html[max(0, m.start()-1200):m.end()+1200]
                    # The Fed calendar renders release date(s) close to the event. Prefer a standalone day 1-31.
                    days = [int(x) for x in re.findall(r">\s*([0-3]?\d)\s*<", ctx) if 1 <= int(x) <= 31]
                    times = re.findall(r"([0-1]?\d(?::\d\d)?\s*(?:a\.m\.|p\.m\.))", ctx, flags=re.I)
                    if not days:
                        continue
                    day = days[-1]
                    hour, minute = (14, 0)
                    if times:
                        tm = times[0].lower().replace(".", "")
                        mt = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", tm)
                        if mt:
                            hour = int(mt.group(1)) % 12 + (12 if mt.group(3) == "pm" else 0)
                            minute = int(mt.group(2) or 0)
                    try:
                        dt = datetime(month_dt.year, month_dt.month, day, hour, minute, tzinfo=NY)
                    except Exception as _e:
                        _obs_note('macro_dia:196', _e)
                        continue
                    if dt < now - timedelta(hours=2) or dt > now + timedelta(days=45):
                        continue
                    out.append({"source": "FED", "title": label, "time_ny": dt.isoformat(), "time_ec": dt.astimezone(EC).isoformat(), "impact": impact})
        except Exception as _e:
            _obs_note('macro_dia:201', _e)
            continue
    # de-duplicate
    seen = set(); dedup = []
    for e in sorted(out, key=lambda x: x["time_ny"]):
        k = (e["title"], e["time_ny"][:13])
        if k in seen: continue
        seen.add(k); dedup.append(e)
    dedup = dedup[:20]
    try:
        from .provider_data_lake import DATA_LAKE
        DATA_LAKE.archive_catalog(source="FEDERAL_RESERVE", symbol="MACRO", catalog_type="FEDERAL_RESERVE", items=dedup, metadata={"scope": "OFFICIAL_EVENT_CALENDAR"})
    except Exception as _e:
        _obs_note('macro_dia:212', _e)
    return dedup


def _macro_stress(series: Dict[str, Dict[str, Any]], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    hy = series.get("BAMLH0A0HYM2", {})
    nfci = series.get("NFCI", {})
    y10 = series.get("DGS10", {})
    curve = series.get("T10Y2Y", {})
    now = datetime.now(NY)
    next_high = None
    minutes = None
    for e in events:
        if e.get("impact") != "HIGH": continue
        try: dt = datetime.fromisoformat(e["time_ny"])
        except Exception as _e:
            _obs_note('macro_dia:229', _e)
            continue
        if dt >= now:
            next_high = e
            minutes = (dt - now).total_seconds()/60.0
            break

    # Quantitative stress context, not a trading direction.
    # Each component is capped so no single slow-moving macro series dominates.
    hy_z = max(0.0, _safe_num(hy.get("z"), 0.0) or 0.0)
    nfci_z = max(0.0, _safe_num(nfci.get("z"), 0.0) or 0.0)
    y10_move = abs(_safe_num(y10.get("change_5"), 0.0) or 0.0)
    curve_val = _safe_num(curve.get("value"), 0.0) or 0.0
    event_risk = 0.0
    if minutes is not None:
        if minutes <= 30: event_risk = 100
        elif minutes <= 120: event_risk = 75
        elif minutes <= 1440: event_risk = 45
        elif minutes <= 4320: event_risk = 20
    score = (
        min(hy_z/3.0, 1.0)*28.0 +
        min(nfci_z/3.0, 1.0)*22.0 +
        min(y10_move/0.30, 1.0)*20.0 +
        (18.0 if curve_val < -0.25 else 9.0 if curve_val < 0 else 0.0) +
        event_risk*0.12
    )
    score = float(np.clip(score, 0, 100))
    label = "ALTO" if score >= 70 else "ELEVADO" if score >= 50 else "MODERADO" if score >= 30 else "BAJO"
    return {
        "score": score,
        "label": label,
        "next_high_event": next_high,
        "minutes_to_next_high": minutes,
        "components": {
            "hy_z": hy_z,
            "nfci_z": nfci_z,
            "treasury_10y_change_5": y10_move,
            "curve_10y2y": curve_val,
            "event_risk": event_risk,
        }
    }


def fetch_macro_context(force: bool = False, max_cache_minutes: int = 30) -> Dict[str, Any]:
    if not force and CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(cached.get("updated_ec", ""))
            if datetime.now(EC) - ts < timedelta(minutes=max_cache_minutes):
                return cached
        except Exception as _e:
            _obs_note('macro_dia:277', _e)

    series: Dict[str, Dict[str, Any]] = {}
    errors = []
    # Fetch independent public sources concurrently so Macro DIA never blocks the dashboard for a long time.
    def _one_series(sid):
        return sid, _series_payload(sid, _fred_csv(sid, timeout=6.0))
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_one_series, sid) for sid in FRED_SERIES]
        for fut in as_completed(futs):
            try:
                sid, payload = fut.result(); series[sid] = payload
            except Exception as e:
                errors.append(f"FRED: {e}")
    events: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        fm = {ex.submit(_bls_events, 6.0): "BLS calendar", ex.submit(_fed_events, 6.0): "FED calendar"}
        for fut, name in fm.items():
            try: events.extend(fut.result())
            except Exception as e: errors.append(f"{name}: {e}")
    events = sorted(events, key=lambda x: x.get("time_ny", ""))
    stress = _macro_stress(series, events)
    payload = {
        "updated_ec": datetime.now(EC).isoformat(),
        "series": series,
        "events": events,
        "stress": stress,
        "errors": errors,
        "source_note": "Macro DIA usa datos públicos/oficiales. FRED aporta series macro; BLS/Federal Reserve aportan calendario. Es contexto, no reemplaza Gamma/Delta/Flow.",
    }
    try:
        CACHE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as _e:
        _obs_note('macro_dia:310', _e)
    try:
        from .provider_data_lake import DATA_LAKE
        DATA_LAKE.archive_normalized(source="OFFICIAL_MACRO", symbol="MACRO", event_type="MACRO_CONTEXT", values=payload, event_time=payload.get("updated_ec"))
    except Exception as _e:
        _obs_note('macro_dia:315', _e)
    return payload


def macro_chart(macro: Dict[str, Any]):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    s = macro.get("series", {}) if macro else {}
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.58, 0.42], vertical_spacing=0.10,
                        subplot_titles=("TREASURY YIELDS", "RIESGO / CONDICIONES FINANCIERAS"))
    for sid, label in (("DGS2", "2Y"), ("DGS10", "10Y"), ("DGS30", "30Y")):
        h = pd.DataFrame(s.get(sid, {}).get("history", []))
        if not h.empty:
            h["date"] = pd.to_datetime(h["date"], errors="coerce")
            fig.add_trace(go.Scatter(x=h["date"], y=h["value"], mode="lines", name=label), row=1, col=1)
    hy = pd.DataFrame(s.get("BAMLH0A0HYM2", {}).get("history", []))
    if not hy.empty:
        hy["date"] = pd.to_datetime(hy["date"], errors="coerce")
        fig.add_trace(go.Scatter(x=hy["date"], y=hy["value"], mode="lines", name="HY OAS"), row=2, col=1)
    nf = pd.DataFrame(s.get("NFCI", {}).get("history", []))
    if not nf.empty:
        nf["date"] = pd.to_datetime(nf["date"], errors="coerce")
        # scale is different; normalize to a z-like display for one shared panel.
        vals = pd.to_numeric(nf["value"], errors="coerce")
        sd = vals.std(ddof=0)
        z = (vals - vals.mean()) / sd if sd and math.isfinite(float(sd)) else vals*0
        fig.add_trace(go.Scatter(x=nf["date"], y=z, mode="lines", name="NFCI z"), row=2, col=1)
    fig.update_layout(template="plotly_dark", height=650, paper_bgcolor="#080b10", plot_bgcolor="#0c1118",
                      margin=dict(l=55, r=30, t=65, b=45), legend=dict(orientation="h"), title="MACRO DIA — CONTEXTO OFICIAL")
    fig.update_yaxes(title_text="%", row=1, col=1)
    fig.update_yaxes(title_text="Stress", row=2, col=1)
    return fig

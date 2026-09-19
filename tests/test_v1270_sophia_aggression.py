from pathlib import Path
import asyncio
import os

import pandas as pd

from app.core.aggression_delta import AggressionDeltaEngine, TIMEFRAMES
from app.core.sophia_core import SophiaRuntime, LocalLLM
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]


def _now_market():
    return pd.Timestamp.now(tz="America/New_York").tz_localize(None).floor("min")


def _feed(engine, sign=1, n=14, symbol="YM"):
    base=_now_market()
    for i in range(n):
        engine.ingest_row(symbol, {
            "seq": i+1, "event_type": "TRADE", "timestamp": base + pd.Timedelta(seconds=i+1),
            "price": 45000.0 + sign*i*0.25, "size": 10.0, "signed_volume": float(sign*10),
        })


def test_aggression_live_buy_and_all_timeframes():
    e=AggressionDeltaEngine(); _feed(e, 1)
    s=e.snapshot("YM","1m")
    assert s["ready"] is True
    assert s["live"]["direction"] == "BUY"
    assert s["live"]["forming"] is True
    assert s["scanner_authority"] is True
    assert set(s["frames"]) == {"1m","3m","5m","15m"}
    assert all(s["frames"][f"{m}m"]["direction"] == "BUY" for m in TIMEFRAMES)
    # Public candle intentionally omits the verbose internal diagnostics.
    assert "_details" not in s["live"]


def test_aggression_live_sell():
    e=AggressionDeltaEngine(); _feed(e, -1)
    s=e.snapshot("YM","3m")
    assert s["live"]["direction"] == "SELL"
    assert s["frames"]["1m"]["direction"] == "SELL"


def test_aggression_historical_precomputes_all_timeframes():
    base=_now_market()-pd.Timedelta(days=1)
    rows=[]
    for i in range(40):
        rows.append({"timestamp":base+pd.Timedelta(seconds=i*20),"price":525+i*.01,"size":5,"signed_volume":5,"event_type":"TRADE"})
    payload=AggressionDeltaEngine().build_all_from_dataframe("DIA",pd.DataFrame(rows),limit=100)
    assert payload["precomputed"] is True
    assert set(payload["by_timeframe"]) == {"1m","3m","5m","15m"}
    assert all(payload["by_timeframe"][k]["ready"] for k in payload["by_timeframe"])


def test_sophia_parses_unusual_flow_watch_and_ignores_old_event():
    s=SophiaRuntime()
    w=s.parse_watch("Sophia, avísame cuando haya señal de flujo inusual de compra")
    assert w and w.kind == "UNUSUAL_FLOW" and w.one_shot is False
    s.set_watch_baseline(w.id,min_fabric_seq=50)
    state={"active_symbol":"DIA","spot":525,"scanner":{},"gamma_delta_alignment":{},"publication_gate":{"publicar_permitido":True,"estado":"CLOSED"},"flow_pro":{"latest":{"side":"BUY","score":91,"strike":525,"timestamp":"old","fabric_seq":50}}}
    assert s.evaluate(state,{"frames":{}}) == []
    state["flow_pro"]["latest"].update({"timestamp":"new","fabric_seq":51})
    out=s.evaluate(state,{"frames":{}})
    assert len(out)==1 and out[0]["kind"]=="UNUSUAL_FLOW"
    # Persistent watcher stays active and only fires for a distinct event.
    assert len(s.watches())==1
    assert s.evaluate(state,{"frames":{}})==[]


def test_sophia_price_watch_is_one_shot():
    s=SophiaRuntime()
    w=s.parse_watch("Sophia avísame cuando el precio llegue al nivel 525")
    assert w and w.kind=="PRICE"
    out=s.evaluate({"active_symbol":"DIA","spot":525.02,"scanner":{},"gamma_delta_alignment":{},"flow_pro":{}},{"frames":{}})
    assert out and out[0]["kind"]=="PRICE"
    assert s.watches()==[]


def test_sophia_local_llm_rejects_non_loopback(monkeypatch):
    monkeypatch.setenv("ITM_SOPHIA_LLM_URL","https://api.example.com/v1/chat/completions")
    assert LocalLLM().configured() is False
    monkeypatch.setenv("ITM_SOPHIA_LLM_URL","http://127.0.0.1:11434/api/chat")
    assert LocalLLM().configured() is True


def test_sophia_full_digest_has_platform_modules():
    s=SophiaRuntime()
    d=s._digest({
        "active_symbol":"DIA","spot":525,"scanner":{"direction":"BUY","strength":80},
        "targets":{"pivot":525,"resistance":527},"volatility":{"expected_move":4.1},
        "positioning":{"state":"POSITIVE"},"trace_orderflow":{"timing_gate":{"state":"CONFIRMED"}},
        "flow_pro":{"latest":{"side":"BUY","score":80}},"gamma_delta_alignment":{"label":"BUY"},
        "market_state_field":{"state":"TREND"},"structural_intelligence":{"state":"ACTIVE"},
    }, {"frames":{"1m":{"direction":"BUY"}}})
    assert d["targets_levels"]["pivot"]==525
    assert d["volatility"]["expected_move"]==4.1
    assert d["trace_orderflow"]["timing_gate"]["state"]=="CONFIRMED"
    assert d["aggression"]["1m"]["direction"]=="BUY"


def test_dashboard_contains_minimal_aggression_and_sophia_ui():
    html=(ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8")
    assert "itm_quant_iq_logo.png" in html
    assert 'id="aggressionDeltaCanvas"' in html
    assert 'id="sophiaPanel"' in html
    assert 'id="sophiaMic"' in html
    assert "/static/aggression_delta.js?v={{ app_version }}" in html
    assert "/static/sophia.js?v={{ app_version }}" in html


def test_main_wires_local_voice_and_event_websockets():
    text=(ROOT/"app/main.py").read_text(encoding="utf-8")
    assert '@app.websocket("/ws/aggression-delta")' in text
    assert '@app.websocket("/ws/sophia/events")' in text
    assert 'microphone=(self)' in text
    assert '/api/sophia/voice/transcribe' in text
    assert '/api/sophia/voice/synthesize' in text


def test_version_is_1270():
    assert_version_at_least('1.27.0')
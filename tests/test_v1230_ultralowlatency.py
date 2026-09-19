from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.binary_protocol import (
    BinaryEvent, pack_surface_frame, unpack_surface_frame,
    pack_tick_batch, unpack_tick_batch,
)
from app.core.causality_engine import EventEnvelope, CausalityOrderer
from app.core.accelerated_quant import black_scholes_batch, backend_status
from app.core.volatility_surface import fit_svi_slice, svi_basic_checks, surface_matrix
from app.core.nextgen_terminal import build_quant_surface_payload
from app.core.low_latency_bridge import RustCausalSubscriber

ROOT = Path(__file__).resolve().parents[1]


def test_binary_event_roundtrip_crc_and_exact_length():
    ev = BinaryEvent(
        event_time_ns=1_726_000_000_123_456_789,
        receive_time_ns=1_726_000_000_123_556_789,
        process_time_ns=1_726_000_000_123_656_789,
        source_seq=42,
        priority=20,
        symbol="DIA",
        source="OPRA_DIRECT",
        event_type="OPTION_TRADE",
        payload={"contract_symbol": "DIA260908C00530000", "price": 1.42, "size": 375},
        flags=2,
    )
    raw = ev.pack()
    got = BinaryEvent.unpack(raw)
    assert got == ev
    with pytest.raises(ValueError):
        BinaryEvent.unpack(raw + b"x")
    bad = bytearray(raw); bad[-1] ^= 0x01
    with pytest.raises(ValueError, match="CRC"):
        BinaryEvent.unpack(bad)


def test_surface_and_tick_binary_frames_reject_trailing_or_truncated_bytes():
    sf = pack_surface_frame(field="GEX", rows=2, cols=3, values=[[1, 2, 3], [4, 5, 6]], sequence=7)
    got = unpack_surface_frame(sf)
    assert got["field"] == "GEX" and got["values"].shape == (2, 3)
    for raw in (sf[:-1], sf + b"x"):
        with pytest.raises(ValueError):
            unpack_surface_frame(raw)

    ticks = [{"timestamp": "2026-09-08T10:00:00-05:00", "price": 530.25, "size": 10, "signed_volume": 10, "seq": 9}]
    tf = pack_tick_batch("DIA", ticks, last_seq=9)
    tg = unpack_tick_batch(tf)
    assert tg["symbol"] == "DIA" and tg["ticks"][0]["seq"] == 9
    for raw in (tf[:-1], tf + b"x"):
        with pytest.raises(ValueError):
            unpack_tick_batch(raw)


def test_equal_timestamp_quote_priority_precedes_option_trade():
    ts = pd.Timestamp("2026-09-08T10:00:00-05:00")
    quote = EventEnvelope.build(source="OPRA", symbol="DIA", event_type="OPTION_QUOTE", event_time=ts, source_seq=1)
    trade = EventEnvelope.build(source="OPRA", symbol="DIA", event_type="OPTION_TRADE", event_time=ts, source_seq=1)
    assert quote.ordering_key() < trade.ordering_key()
    o=CausalityOrderer(max_lateness_ms=0); o.push(trade); o.push(quote)
    out=o.drain_ready(flush=True)
    assert [x.event_type for x in out] == ["OPTION_QUOTE", "OPTION_TRADE"]


def test_rust_bridge_enriches_option_trade_with_prior_nbbo(monkeypatch):
    # Directly exercise the subscriber's normalized receive boundary. ZeroMQ transport
    # itself is covered by pyzmq and the bridge loop; this test focuses on causal semantics.
    monkeypatch.setenv("ITM_RUST_CAUSALITY", "1")
    b = RustCausalSubscriber(capacity=10_000)
    base = 1_726_000_000_000_000_000
    quote = BinaryEvent(base, base + 1000, base + 2000, 1, 10, "DIA", "OPRA", "OPTION_QUOTE", {
        "contract_symbol": "DIA260908C00530000", "bid": 1.39, "ask": 1.42, "bid_size": 50, "ask_size": 40,
    })
    trade = BinaryEvent(base + 500_000, base + 501_000, base + 502_000, 2, 20, "DIA", "OPRA", "OPTION_TRADE", {
        "contract_symbol": "DIA260908C00530000", "underlying_symbol": "DIA", "option_type": "call",
        "trade_price": 1.42, "contracts": 375, "multiplier": 100,
    })
    b._accept(quote)
    b._accept(trade)
    got = b.drain_market_data("DIA")
    assert got["events"] == 1
    row = got["option_trades"].iloc[0]
    assert row["bid"] == pytest.approx(1.39)
    assert row["ask"] == pytest.approx(1.42)
    assert row["aggressor"] == "BUY"
    assert bool(row["nbbo_synced"]) is True
    assert str(row["classification_method"]).startswith("RUST_CAUSAL_")
    assert row["premium"] == pytest.approx(1.42 * 375 * 100)
    assert row["causal_transport"] == "RUST_ZMQ"


def test_actual_zeromq_binary_boundary_when_pyzmq_available(monkeypatch):
    zmq = pytest.importorskip("zmq")
    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB); port = pub.bind_to_random_port("tcp://127.0.0.1")
    endpoint = f"tcp://127.0.0.1:{port}"
    monkeypatch.setenv("ITM_RUST_CAUSALITY", "1")
    sub = RustCausalSubscriber(endpoint=endpoint, capacity=10_000)
    assert sub.start()
    try:
        time.sleep(0.20)  # PUB/SUB subscription handshake
        now = time.time_ns()
        ev = BinaryEvent(now, now + 1000, now + 2000, 1, 20, "DIA", "TEST_DIRECT", "TRADE", {"price": 530.25, "size": 10})
        # Send a few times to remove slow-joiner flakiness; dedupe is a higher layer concern.
        for i in range(3):
            e = BinaryEvent(ev.event_time_ns + i, ev.receive_time_ns + i, ev.process_time_ns + i, i + 1, 20, "DIA", "TEST_DIRECT", "TRADE", {"price": 530.25 + i * 0.01, "size": 10})
            pub.send(e.pack())
            time.sleep(0.02)
        deadline = time.time() + 2.0
        while time.time() < deadline and (sub.status().get("received") or 0) < 1:
            time.sleep(0.02)
        assert (sub.status().get("received") or 0) >= 1
        data = sub.drain_market_data("DIA")
        assert not data["price_ticks"].empty
        assert data["price_ticks"]["source"].str.contains("RUST CAUSAL").all()
    finally:
        sub.stop(); pub.close(0)


def test_accelerated_greeks_match_numpy_reference(monkeypatch):
    n = 64
    S = np.full(n, 530.0); K = np.linspace(500, 560, n); T = np.linspace(0.001, 0.10, n)
    iv = np.linspace(0.15, 0.35, n); calls = np.arange(n) % 2 == 0
    r = np.full(n, 0.045); q = np.zeros(n)
    ref = black_scholes_batch(S, K, T, iv, calls, r, q, prefer_accelerated=False)
    got = black_scholes_batch(S, K, T, iv, calls, r, q, prefer_accelerated=True)
    for key in ("delta", "gamma", "vanna", "charm", "speed"):
        np.testing.assert_allclose(got[key], ref[key], rtol=2e-8, atol=2e-10)
    assert backend_status()["backend"] in {"NUMPY", "JAX", "CUPY"}


def _svi_chain():
    rows=[]
    for exp,dte in [("2026-09-11",3.0),("2026-09-18",10.0)]:
        for k in [500,510,520,530,540,550,560]:
            logm=np.log(k/530.0)
            iv=0.18 + 0.22*logm*logm - 0.04*logm + 0.002*(dte/7)
            rows.append({"expiration_date":exp,"dte":dte,"strike":k,"iv":iv,"underlying_price":530.0})
    return pd.DataFrame(rows)


def test_svi_preserves_observed_base_and_labels_stress_model():
    chain=_svi_chain(); strikes=[500,510,520,530,540,550,560]; expiries=["2026-09-11","2026-09-18"]
    base, src, fit = surface_matrix(chain, strikes, expiries, 530.0, scenario_iv_shift=0.0)
    raw=chain.pivot_table(index="strike",columns="expiration_date",values="iv").reindex(index=strikes,columns=expiries).to_numpy(float)
    np.testing.assert_allclose(base, raw, rtol=0, atol=1e-12)
    assert set(np.unique(src)) == {"OBSERVED"}
    stressed, ssrc, sfit = surface_matrix(chain, strikes, expiries, 530.0, scenario_iv_shift=0.02)
    assert stressed.shape == base.shape
    assert all("SCENARIO" in str(x) or "SHIFT" in str(x) for x in np.unique(ssrc))
    assert sfit["scenario_mode"] is True


def test_svi_basic_checks_reject_invalid_wing_slope():
    bad = svi_basic_checks({"a":0.01,"b":3.0,"rho":0.9,"m":0.0,"sigma":0.2})
    assert bad["pass"] is False
    assert bad["checks"]["lee_right_wing_slope_le_2"] is False


def test_q_never_becomes_scanner_authority_even_with_oos_weights():
    rows=[]
    ts=pd.Timestamp("2026-09-08T14:00:00Z")
    for expiry,dte in [("2026-09-08",0.20),("2026-09-11",3.20)]:
        for strike in [528.,529.,530.,531.,532.]:
            for typ in ["call","put"]:
                rows.append({"timestamp":ts,"expiration_date":expiry,"dte":dte,"strike":strike,"iv":0.18,
                             "open_interest":1000,"volume":200,"underlying_price":530.0,"option_type":typ})
    cal={"field_weights_oos":{"Gamma":1,"Delta":1,"Vanna":1,"Charm":1,"Speed":1,"Color":1,"Hedge":1}}
    r=build_quant_surface_payload(symbol="DIA",gd={"spot":530.0,"enriched":pd.DataFrame(rows)},dealer={},calibration=cal)
    assert r["ready"] is True
    assert r["q_is_probability"] is False
    assert r.get("q_production_authority") is False



def test_hot_dealer_inventory_is_ram_first_and_archive_is_explicit_checkpoint(tmp_path):
    from app.core.dealer_intelligence import HotSyntheticInventoryBook
    archive=tmp_path/"dealer_inventory.sqlite"
    b=HotSyntheticInventoryBook(archive, flush_seconds=3600)
    st=b.hot_status()
    assert st["backend"] == "RAM_SQLITE_SHARED_CACHE"
    assert st["archive"] == "SQLITE_WAL_ASYNC"
    # The durable schema may exist at construction for recovery compatibility, but
    # subsequent LIVE mutations stay in the shared-memory DB until a checkpoint.
    before=archive.stat().st_mtime_ns if archive.exists() else 0
    event=pd.DataFrame([{
        "underlying_symbol":"DIA","contract_symbol":"DIA260908C00530000",
        "timestamp":pd.Timestamp("2026-09-08T10:00:00-05:00"),"trade_price":1.42,"contracts":100,
        "exchange":"TEST","aggressor":"BUY","option_type":"call","strike":530.0,
        "expiration_date":"2026-09-08","underlying_price":530.0,"calc_delta":0.5,"calc_gamma":0.02,
        "aggressor_confidence":0.95,"nbbo_synced":True,"classification_method":"CAUSAL_NBBO_ASK",
    }])
    out=b.ingest(event)
    assert out["ingested"] == 1
    assert b.hot_status()["dirty"] is True
    after=archive.stat().st_mtime_ns if archive.exists() else 0
    assert after == before
    flushed=b.flush_archive()
    assert flushed["flushed"] is True
    assert b.hot_status()["dirty"] is False


def test_service_consumes_rust_bridge_in_live_pipeline_source():
    src=(ROOT/"app/service.py").read_text(encoding="utf-8")
    assert "RUST_CAUSAL_BRIDGE.drain_market_data" in src
    assert "RUST_CAUSAL_BRIDGE.latest_price" in src
    assert "rust_causal" in src.lower()
    assert "combined" in src.lower() or "live_ticks" in src

def test_v123_frontend_native_terminal_and_trace_contracts():
    ultra=(ROOT/"app/static/ultra_charts.js").read_text(encoding="utf-8")
    nxt=(ROOT/"app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    app=(ROOT/"app/static/app.js").read_text(encoding="utf-8")
    dash=(ROOT/"app/templates/dashboard.html").read_text(encoding="utf-8")
    for cid in ["flowProChart","netDriftChart","exposureChart","gexMatrixChart","netPositioningChart","volumeChart","volChart","macroChart","printsChart"]:
        assert cid in ultra
    assert app.find("ITMQUltraCharts?.render") < app.find("Plotly.react")
    assert "drawProfiles" in nxt and "gamma_m" in nxt and "delta_m" in nxt
    assert "ancho = exposición" in nxt and "intensidad = liquidez" in nxt
    assert "FOLLOW" in nxt or "follow" in nxt
    assert "tracePriceStyle" in dash and "SESIÓN" in dash
    assert "QUÉ ESTOY VIENDO" in dash


def test_v123_rust_source_has_nonblocking_publisher_and_late_event_guard():
    main=(ROOT/"rust/causality_engine/src/main.rs").read_text(encoding="utf-8")
    order=(ROOT/"rust/causality_engine/src/orderer.rs").read_text(encoding="utf-8")
    proto=(ROOT/"rust/causality_engine/src/protocol.rs").read_text(encoding="utf-8")
    assert "DONTWAIT" in main
    assert "unbounded" in main
    assert "late_dropped" in order
    assert "BinaryHeap" in order
    assert "ITMQ" in proto and "crc" in proto.lower()


def test_wasm_bridge_exposes_pointer_as_usize_and_exact_frame_guard():
    src=(ROOT/"rust/wasm_bridge/src/lib.rs").read_text(encoding="utf-8")
    assert "pubfnpointer(&self)->usize" in src.replace(" ","")
    assert "expected" in src.lower() or "frame" in src.lower()


def test_python_alpaca_to_rust_ingress_binary_boundary(monkeypatch):
    zmq = pytest.importorskip("zmq")
    from app.core.low_latency_bridge import RustCausalIngressPublisher
    from app.core.binary_protocol import BinaryEvent
    ctx = zmq.Context.instance()
    pull = ctx.socket(zmq.PULL)
    port = pull.bind_to_random_port("tcp://127.0.0.1")
    endpoint = f"tcp://127.0.0.1:{port}"
    monkeypatch.setenv("ITM_RUST_CAUSALITY", "1")
    monkeypatch.setenv("ITM_RUST_PYTHON_FORWARD", "1")
    pub = RustCausalIngressPublisher(endpoint=endpoint)
    assert pub.start()
    try:
        time.sleep(0.05)
        ok = pub.publish(
            event_time=pd.Timestamp("2026-09-08T10:00:00-05:00"),
            receive_time=pd.Timestamp("2026-09-08T10:00:00.001-05:00"),
            source_seq=17, priority=20, symbol="DIA", source="ALPACA_SIP_PY_FORWARD",
            event_type="TRADE", payload={"price":530.25,"size":10,"exchange":"Q"},
        )
        assert ok is True
        poller=zmq.Poller(); poller.register(pull, zmq.POLLIN)
        ready=dict(poller.poll(1000)); assert pull in ready
        raw=pull.recv()
        ev=BinaryEvent.unpack(raw)
        assert ev.symbol=="DIA" and ev.source_seq==17 and ev.event_type=="TRADE"
        assert ev.payload["price"]==pytest.approx(530.25)
        assert pub.status()["sent"] >= 1
    finally:
        pub.stop(); pull.close(0)


def test_rust_source_accepts_transitional_itmq_pull_ingress():
    main=(ROOT/"rust/causality_engine/src/main.rs").read_text(encoding="utf-8")
    metrics=(ROOT/"rust/causality_engine/src/metrics.rs").read_text(encoding="utf-8")
    assert "ITM_RUST_INGEST_BIND" in main
    assert "zmq::PULL" in main
    assert "EventEnvelope::from_wire" in main
    assert "ingress_decode_errors" in main
    assert "itmq_rust_ingress_decode_errors_total" in metrics


def test_alpaca_streams_forward_normalized_events_without_changing_fallback():
    opt=(ROOT/"app/core/option_stream.py").read_text(encoding="utf-8")
    sip=(ROOT/"app/core/live_price.py").read_text(encoding="utf-8")
    for src in (opt,sip):
        assert "RUST_CAUSAL_INGRESS.publish" in src
        # v1.27.1: este assert EXIGÍA literalmente `except Exception: pass`, es decir
        # blindaba el antipatrón que hacía invisibles 181 fallos. La intención real
        # era "publicar al bus causal nunca puede tumbar el stream de mercado".
        # Eso ahora se cumple MEJOR: sigue degradando, pero queda contado en
        # /health -> degradations en vez de desaparecer.
        assert "try:" in src
        assert "_obs_note(" in src, (
            "la publicación al bus causal debe degradar de forma AUDITABLE. "
            "Usa obs.swallow/obs.guard/_obs_note, nunca `except Exception: pass`."
        )
        assert "except Exception: pass" not in src
    assert 'event_type="OPTION_QUOTE"' in opt and 'event_type="OPTION_TRADE"' in opt
    assert 'event_type="QUOTE"' in sip and 'event_type="TRADE"' in sip


def test_ultralow_latency_start_script_keeps_honest_fallback_contract():
    wrapper=(ROOT/"INICIAR_ULTRA_LOW_LATENCY.bat").read_text(encoding="utf-8")
    motor=(ROOT/"INICIAR_MOTOR_24_7.bat").read_text(encoding="utf-8")
    doc=(ROOT/"docs/ULTRA_LOW_LATENCY_DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "call INICIAR_MOTOR_24_7.bat" in wrapper
    assert "ITM_RUST_PYTHON_FORWARD=1" in motor
    assert "scripts\\BUILD_RUST_CAUSALITY.bat" in doc
    assert "does not turn Alpaca into a direct/HFT feed" in doc
    assert "Direct institutional" in doc

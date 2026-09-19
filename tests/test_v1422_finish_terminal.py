"""H3 · cierres visuales/semánticos encontrados en navegador real."""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from app import terminal_api
from app.core import session_memory
from app.providers.quantdata.settings import load_settings
from app.providers.quantdata.tools import QuantDataTool

ROOT = Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_trace_candles_remain_visible_on_shared_strike_axis():
    src = text("app/static/itmq_trace.js")
    block = src[src.find("function drawCandles"):src.find("function drawPriceLine")]
    assert "MIN_BODY_PX = 3.0" in block
    assert "MIN_WICK_PX = 5.0" in block
    assert "bodyTop = mid - MIN_BODY_PX / 2" in block
    assert "wickTop = mid - MIN_WICK_PX / 2" in block


def test_orderflow_net_lane_uses_directional_option_premium_not_underlying_volume():
    src = text("app/static/itmq_orderflow.js")
    rebuild = src[src.find("function rebuild"):src.find("function effectiveMinPremium")]
    assert "b.underlyingNet += Q.num(c.sv, 0);" in rebuild
    assert "b.net = Q.num(b.buy, 0) - Q.num(b.sell, 0);" in rebuild
    assert "b.net += Q.num(c.sv, 0)" not in rebuild
    draw = src[src.find("function drawNetFlow"):src.find("function drawTotal")]
    assert "FLUJO NETO · PRIMA DIRECCIONAL" in draw


def test_orderflow_large_print_markers_are_bucketed_and_anchored_to_price_curve():
    src = text("app/static/itmq_orderflow.js")
    block = src[src.find("// Prints grandes: se agregan"):src.find("// niveles estructurales")]
    assert "markBuckets = new Map()" in block
    assert "nearestPrice(t)" in block
    assert "sy(near.v)" in block
    assert ".slice(0, 5)" in block


def test_native_max_pain_timeline_reaches_open_interest_without_quantdata(tmp_path: Path):
    p = session_memory._path(tmp_path, "DIA")
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([
        {"timestamp": "2026-09-18T08:50:00", "symbol": "DIA", "expiry_mode": "0DTE", "max_pain": 515.0, "gamma_center": 514.5, "delta_center": 515.0, "gamma_flip": 519.0},
        {"timestamp": "2026-09-18T08:51:00", "symbol": "DIA", "expiry_mode": "0DTE", "max_pain": 516.0, "gamma_center": 514.6, "delta_center": 515.1, "gamma_flip": 519.0},
    ]).to_csv(p, index=False)
    sm = session_memory.session_memory_summary(tmp_path, "DIA", "0DTE")
    assert [r["value"] for r in sm["max_pain_over_time"]] == [515.0, 516.0]
    assert sm["max_pain_source"] == "ITM_QUANT_CHAIN_HISTORY"

    state = {
        "positioning": {"call_oi": 100, "put_oi": 120},
        "key_levels_report": {"max_pain": 516.0},
        "session_memory": sm,
        "spot": 514.8,
        "expiry_intelligence": {"per_expiration": []},
    }
    trace = {"profiles": {"rows": [{"strike": 515, "call_oi": 100, "put_oi": 120, "net_oi": -20, "oi": 220, "volume_snapshot": 10}]}}
    intel = {"max_pain_over_time": {"rows": [{"t": "bad-external", "value": 999.0}]}}
    out = terminal_api._open_interest(trace, state, intel)
    assert [r["value"] for r in out["max_pain_over_time"]] == [515.0, 516.0]
    assert out["max_pain_history_source"] == "ITM_QUANT_CHAIN_HISTORY"


def test_premarket_has_one_gamma_wall_authority_and_separate_oi_peaks():
    src = text("app/core/premarket_intelligence.py")
    assert "walls = _structural_walls(wall_frame, spot)" in src
    assert '"call_wall":call_wall,"put_wall":put_wall' in src
    assert '"max_call_oi_strike":max_call_oi' in src
    assert '"max_put_oi_strike":max_put_oi' in src
    dash = text("app/templates/dashboard.html")
    assert "Call Wall · Gamma" in dash and "Put Wall · Gamma" in dash
    assert "Mayor Call OI" in dash and "Mayor Put OI" in dash


def test_sources_never_presents_external_qd_zero_as_core_exposure_zero():
    src = text("app/static/itmq_app.js")
    assert "SIN CONTRASTE" in src
    assert "núcleo ${esc(c.native_authority || 'ITM')} activo" in src
    html = text("app/templates/terminal.html")
    assert "<th>ROL</th><th>COBERTURA</th>" in html


def test_architecture_fallback_column_is_explicitly_policy_not_live_failure():
    html = text("app/templates/terminal.html")
    assert "POLÍTICA SI FALTA" in html
    assert "no indica un fallo actual" in html
    app = text("app/static/itmq_app.js")
    assert "BLOQUEAR MÉTRICA" in app
    assert "MOSTRAR N/D" in app
    assert "CONTINUAR DEGRADADO" in app



def test_line_panels_render_one_real_observation_instead_of_calling_it_no_data():
    src = text("app/static/itmq_panels.js")
    assert "if (t1 <= t0) { t0 -= 30_000; t1 += 30_000; }" in src
    assert "if (pts.length === 1)" in src

def test_quantdata_timeout_uses_backoff_and_success_resets_it():
    tool = QuantDataTool("x", "Exposure", "X", ("/x",), lambda _: {}, lambda p: p, "FAST")
    now = time.time()
    tool.mark_transient("timed out", now=now)
    assert tool.transient_failures == 1
    assert tool.unavailable_until >= now + 29
    assert not tool.available(now + 5)
    tool.mark_success()
    assert tool.transient_failures == 0 and tool.unavailable_until == 0 and tool.last_error is None


def test_quantdata_default_timeout_allows_slow_analytics_without_blocking_core(monkeypatch):
    monkeypatch.delenv("QUANTDATA_TIMEOUT_SECONDS", raising=False)
    assert load_settings().request_timeout_seconds == 10.0


def test_auto_accelerator_does_not_select_jax_cpu_only(monkeypatch):
    from app.core import accelerated_quant as aq
    monkeypatch.setenv("ITM_ACCELERATED_QUANT", "1")
    monkeypatch.setenv("ITM_ACCELERATOR_BACKEND", "AUTO")
    monkeypatch.setattr(aq, "_cupy_status", lambda: {"available": False, "gpu": False})
    monkeypatch.setattr(aq, "_jax_status", lambda: {"available": True, "gpu": False, "devices": ["cpu:cpu"]})
    assert aq._selected_backend() == "NUMPY"

    # Si el operador lo pide explícitamente, JAX CPU continúa siendo una opción.
    monkeypatch.setenv("ITM_ACCELERATOR_BACKEND", "JAX")
    assert aq._selected_backend() == "JAX"

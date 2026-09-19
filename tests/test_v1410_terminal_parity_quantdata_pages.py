"""v1.41.0 · Terminal de analista, paridad de proveedores y páginas Quant Data."""
from __future__ import annotations

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ─────────────────────────────────────────────── paridad de proveedores

def test_options_peers_are_all_first_class_without_confirmation_role():
    from app.core.provider_parity import parity_policy, OPTIONS_PEERS, KNOWN_PROVIDERS

    policy = parity_policy()
    # El roster por defecto es Alpaca + Quant Data. Lo que esta prueba fija no es
    # QUIÉNES están, sino que todos los que están entran como pares de pleno derecho:
    # ninguno queda relegado a un papel de confirmación.
    assert set(OPTIONS_PEERS) <= set(KNOWN_PROVIDERS)
    assert "ALPACA" in OPTIONS_PEERS and "QUANTDATA" in OPTIONS_PEERS
    assert policy["fixed_rank"] is False
    assert policy["confirmation_only_providers"] == []
    assert policy["policy"] == "EQUAL_PEER_OPTIONS_PARITY"


def test_parity_report_marks_every_provider_as_peer():
    from app.core.provider_parity import parity_report

    report = parity_report(
        alpaca_configured=True,
        tastytrade_status={"configured": True, "connected": True},
        quantdata_status={"configured": True, "last_success": "2026-09-17T14:00:00+00:00"},
    )
    from app.core.provider_parity import OPTIONS_PEERS

    assert report["equal_weight"] is True
    assert report["confirmation_only"] == []
    names = {p["provider"] for p in report["providers"]}
    # Sólo el roster activo llega a la pantalla: un proveedor retirado no es una
    # carencia que reportar, es una decisión de esta instalación.
    assert names == set(OPTIONS_PEERS)
    for p in report["providers"]:
        assert p["role"] == "PEER"
        assert p["first_class"] is True


def test_provider_name_never_adds_weight():
    """Dos observaciones idénticas salvo el proveedor deben pesar exactamente igual."""
    from app.core.provider_parity import weight_for

    base = {"age_ms": 500.0, "freshness_horizon_ms": 5_000.0, "status": "LIVE",
            "fields_present": 9, "fields_expected": 10}
    weights = {
        name: weight_for({**base, "provider": name})
        for name in ("ALPACA", "TASTYTRADE", "QUANTDATA")
    }
    assert len(set(round(w, 9) for w in weights.values())) == 1


def test_quality_decides_channel_not_provider_order():
    from app.core.provider_parity import rank_peers

    ranked = rank_peers([
        {"provider": "ALPACA", "quality_score": 61.0},
        {"provider": "QUANTDATA", "quality_score": 93.0},
        {"provider": "TASTYTRADE", "quality_score": 74.0},
    ], channel="OPTION_CHAIN")
    # Quant Data gana el canal de cadena por calidad: antes era imposible por rol.
    assert ranked["selected"] == "QUANTDATA"
    assert ranked["usable_count"] == 3


def test_fusion_falls_back_to_best_peer_when_sources_diverge():
    """Dentro de una métrica SÍ fusionable, la divergencia publica al mejor par.

    El precio del subyacente es el caso legítimo: dos lecturas del mismo
    instrumento. Si aun así divergen por encima de la tolerancia, no describen el
    mismo instante y se publica la de mayor calidad, no su media.
    """
    from app.core.provider_parity import fuse_values

    fused = fuse_values([
        {"provider": "ALPACA", "quality_score": 90.0, "underlying_price": 100.0},
        {"provider": "QUANTDATA", "quality_score": 55.0, "underlying_price": 400.0},
    ], "underlying_price", channel="PRICE", max_divergence=0.05)
    assert fused["diverged"] is True
    assert fused["method"] == "HIGHEST_QUALITY_PEER"
    # Nunca una media entre cifras que no describen el mismo mercado.
    assert fused["value"] == 100.0


def test_fusion_weights_agreeing_peers_by_quality():
    """Con contrato de fusión y acuerdo, la media ponderada sigue siendo correcta."""
    from app.core.provider_parity import fuse_values

    fused = fuse_values([
        {"provider": "ALPACA", "quality_score": 90.0, "underlying_price": 100.0},
        {"provider": "QUANTDATA", "quality_score": 60.0, "underlying_price": 102.0},
    ], "underlying_price", channel="PRICE", max_divergence=0.10)
    assert fused["diverged"] is False
    assert fused["method"] == "QUALITY_WEIGHTED_SAME_PHENOMENON_MEAN"
    assert 100.0 < fused["value"] < 101.0


def test_gex_is_never_averaged_between_providers():
    """v1.42 · El GEX de Quant Data y el de ITM no son la misma magnitud.

    Son dos construcciones con su propio universo de vencimientos, su hipótesis de
    posicionamiento y posiblemente otra representación de unidades. Promediarlas
    produce una cifra que no corresponde al libro de nadie y, peor, borra la señal
    más útil: que difieren. La fusión debe quedar prohibida por política.
    """
    from app.core.provider_parity import fuse_values
    from app.core.metric_authority import fusion_allowed

    assert not fusion_allowed("gex")
    fused = fuse_values([
        {"provider": "ALPACA", "quality_score": 90.0, "gex": 100.0},
        {"provider": "QUANTDATA", "quality_score": 60.0, "gex": 102.0},
    ], "gex", max_divergence=0.10)
    assert fused["ready"] is False
    assert fused["status"] == "FUSION_FORBIDDEN"
    assert "value" not in fused
    # v1.43.0 · La autoridad de GEX pasó a Quant Data. Lo que la prueba defiende no
    # es quién manda, sino que la fusión siga prohibida: sean quienes sean las dos
    # partes, promediar dos construcciones distintas no describe ningún mercado.
    assert "autoridad es QUANTDATA" in fused["reason"]


def test_low_quality_observation_is_excluded_with_a_reason():
    from app.core.provider_parity import rank_peers, MIN_USABLE_QUALITY

    ranked = rank_peers([{"provider": "ALPACA", "quality_score": MIN_USABLE_QUALITY - 1}])
    assert ranked["usable_count"] == 0
    assert ranked["peers"][0]["excluded_reason"] == "QUALITY_BELOW_MIN_USABLE"


def test_options_channels_have_their_own_arbitration_policy():
    from app.core.source_arbitration import channel_policy, _channel

    for channel in ("OPTION_FLOW", "EXPOSURE", "IMPLIED_VOLATILITY", "DARK_POOL", "EQUITY_PRINT"):
        assert channel_policy(channel) != channel_policy("DEFAULT"), channel
    assert _channel({"channel": "GEX"}) == "EXPOSURE"
    assert _channel({"channel": "NET_FLOW"}) == "OPTION_FLOW"
    assert _channel({"channel": "DARKPOOL"}) == "DARK_POOL"


def test_source_fusion_no_longer_limits_quantdata_to_confirmation():
    src = text("app/core/source_fusion.py")
    assert "never performs network I/O" in src          # el contrato de red se mantiene
    assert "corroboration only" not in src


# ────────────────────────────────────────────── páginas de Quant Data

def test_catalog_covers_every_built_in_page():
    from app.providers.quantdata.tools import build_catalog, PAGES

    catalog = build_catalog()
    expected_pages = {
        "Dashboard", "Exposure", "Flow Analysis", "Dark Pool / Equities",
        "Statistics", "Open Interest", "Volatility Analysis",
    }
    assert expected_pages == set(PAGES)
    for page, keys in PAGES.items():
        assert keys, page
        for key in keys:
            assert key in catalog, f"{page} → {key}"


def test_exposure_page_covers_all_four_greeks_on_both_axes():
    from app.providers.quantdata.tools import build_catalog

    catalog = build_catalog()
    for greek in ("gex", "dex", "vex", "chex"):
        assert f"{greek}_by_strike" in catalog
        assert f"{greek}_by_expiration" in catalog


def test_tools_declare_candidate_paths_and_a_cadence():
    from app.providers.quantdata.tools import build_catalog, CADENCE

    for key, tool in build_catalog().items():
        assert tool.paths, key
        assert all(p.startswith("/v1/") for p in tool.paths), key
        assert tool.cadence in CADENCE, key


def test_missing_tool_is_detected_and_cooled_down():
    from app.providers.quantdata.tools import build_catalog, is_missing_tool_error

    assert is_missing_tool_error("Quant Data HTTP 404: not found") is True
    assert is_missing_tool_error("Quant Data request timed out") is False

    tool = build_catalog()["market_share"]
    assert tool.available() is True
    tool.mark_unavailable("Quant Data HTTP 404")
    # Una herramienta inexistente no debe reintentarse en cada ciclo.
    assert tool.available() is False


def test_intelligence_lane_is_independent_from_engine_lane():
    from app.providers.quantdata.intelligence import QUANTDATA_INTELLIGENCE
    from app.providers.quantdata.runtime import QUANTDATA

    assert QUANTDATA_INTELLIGENCE is not QUANTDATA
    cov = QUANTDATA_INTELLIGENCE.coverage()
    assert cov["total_tools"] >= 25
    assert len(cov["pages"]) == 7


def test_normalizers_tolerate_alternative_provider_envelopes():
    from app.providers.quantdata.tools import norm_by_strike, norm_levels

    a = norm_by_strike({"data": [{"strike": 534.0, "gamma": 12.5}]}, ("gamma",))
    b = norm_by_strike({"results": {"strikes": [{"strikePrice": 534.0, "value": 12.5}]}}, ("gamma", "value"))
    assert a["rows"][0]["strike"] == 534.0 == b["rows"][0]["strike"]
    assert a["rows"][0]["value"] == 12.5 == b["rows"][0]["value"]

    lv = norm_levels({"levels": [{"price": 533.0, "notional": 5.0}, {"price": 534.0, "notional": 9.0}]})
    assert [r["price"] for r in lv["rows"]] == [534.0, 533.0]   # mayor notional primero


# ─────────────────────────────────────────────────── bundle de terminal

def _bundle():
    from app.terminal_api import build_terminal_bundle
    state = {
        "ready": True, "active_symbol": "DIA", "spot": 534.22, "asset": {"name": "SPDR Dow", "kind": "ETF"},
        "scanner": {"direction": "SELL", "edge_state": "ACTIONABLE", "zone": {"low": 534.1, "high": 534.3}},
        "key_levels_report": {"zero_gamma": 535.15, "call_wall": 534.7, "put_wall": 533.7, "max_pain": 534.2},
        "positioning": {"call_oi": 100.0, "put_oi": 150.0, "net_gex": -9.1e6, "net_delta": -2.7e7},
        "volatility": {"atm_iv": 19.7, "skew_25d": 0.32,
                       "skew_by_expiry": [{"dte": 0.25, "skew_25d": 0.32, "call25_iv": 21.5, "put25_iv": 21.8}],
                       "term_structure": [{"dte": 0.25, "iv": 0.197, "expiration_date": "2026-09-17"}]},
        "large_prints": {"count": 0, "total_notional": 0.0, "top": []},
        "liquidity_zones": {"ready": False, "zones": [], "reason": "NO_PRINTS"},
        "expiry_intelligence": {"per_expiration": [{"expiration": "2026-09-17", "gamma": 5.0, "oi": 26571.0, "volume": 6725.0}]},
    }
    trace = {
        "candles": [{"t": "2026-09-17T14:00:00", "o": 534.1, "h": 534.3, "l": 534.0, "c": 534.2, "v": 100, "sv": 20}],
        "option_prints": [{"t": "2026-09-17T14:00:30", "premium": 250000.0, "contracts": 50,
                           "strike": 534.0, "option_type": "call", "direction": 1, "aggressor": "BUY"}],
        "profiles": {"spot": 534.22, "rows": [
            {"strike": 534.0, "gamma_m": 6.18, "delta_m": 3.73, "vanna_1vol_m": -0.007, "charm_10m_m": 0.0003,
             "oi": 10000, "call_oi": 6000, "put_oi": 4000, "net_oi": 2000,
             "volume_snapshot": 500, "net_volume": 100},
        ]},
        "levels": [{"name": "Zero Gamma", "price": 535.15, "kind": "flip"}],
    }
    return build_terminal_bundle(state=state, trace=trace)


def test_bundle_exposes_every_terminal_section():
    b = _bundle()
    for key in ("resumen", "exposicion", "open_interest", "volatilidad", "estadisticas", "dark_pool", "fuentes"):
        assert key in b, key
    assert b["contract"] == "ITMQ_TERMINAL_BUNDLE_V2"
    assert b["symbol"] == "DIA"


def test_bundle_converts_profile_millions_into_absolute_units():
    b = _bundle()
    row = b["exposicion"]["by_strike"][0]
    assert row["gex"] == pytest.approx(6.18e6)
    assert row["dex"] == pytest.approx(3.73e6)


def test_bundle_reports_missing_data_instead_of_inventing_it():
    """Sin prints de equity el dark pool viaja vacío con su motivo, no con ceros creíbles."""
    b = _bundle()
    dp = b["dark_pool"]
    assert dp["levels"] == []
    # v1.42.7 · Quant Data es ahora la fuente primaria de dark pool, así que el
    # motivo nombra primero al proveedor: sin respuesta suya no hay ni niveles, ni
    # flujo oscuro, ni prints que clasificar. "NO_PRINTS" sólo aplica cuando el
    # proveedor sí respondió y lo que falta es la cinta.
    assert dp["reason"] == "QUANT_DATA_SIN_RESPUESTA"
    assert dp["dominant_level"] is None


def test_bundle_prefers_native_engine_breakdown_over_provider():
    b = _bundle()
    assert b["exposicion"]["by_expiration"][0]["source"] == "ITM_QUANT"


def test_bundle_levels_carry_distance_to_spot():
    b = _bundle()
    by_name = {lv["name"]: lv for lv in b["resumen"]["levels"]}
    assert by_name["Zero Gamma"]["distance"] == pytest.approx(535.15 - 534.22, abs=1e-6)


def test_bundle_statistics_are_built_from_observed_prints():
    b = _bundle()
    st = b["estadisticas"]
    assert st["print_count"] == 1
    assert st["buy_premium"] == pytest.approx(250000.0)
    assert st["contract_rows"][0]["label"].startswith("C ")


# ─────────────────────────────────────────────────────── capa de interfaz

def test_terminal_template_and_assets_exist():
    assert (ROOT / "app/templates/terminal.html").exists()
    for asset in ("itmq_core.js", "itmq_trace.js", "itmq_orderflow.js", "itmq_panels.js",
                  "itmq_app.js", "itmq_terminal.css"):
        assert (ROOT / "app/static" / asset).exists(), asset


def test_terminal_is_served_at_root_and_legacy_dashboard_is_kept():
    src = text("app/main.py")
    assert 'name="terminal.html"' in src
    assert '@app.get("/legacy"' in src


def test_trace_panels_share_one_price_axis():
    src = text("app/static/itmq_trace.js")
    # Los tres paneles derivan su eje Y del mismo par priceLo/priceHi: es lo que
    # garantiza que una barra de strike esté a la altura exacta de su nivel.
    assert "function priceScale(box)" in src
    assert src.count("priceScale(box)") >= 3


def test_render_core_avoids_short_circuiting_animation_steps():
    """step() tiene efecto lateral: encadenarlo con || congela la segunda escala."""
    import re
    # `x.step(dt) || y` es seguro: el izquierdo siempre se evalúa. El peligro es
    # `... || y.step(dt)`, donde y.step() no corre si el izquierdo ya es true.
    risky = re.compile(r"\|\|\s*[A-Za-z_$][\w.$\[\]']*\.step\s*\(")
    for rel in ("app/static/itmq_trace.js", "app/static/itmq_panels.js", "app/static/itmq_orderflow.js"):
        assert not risky.search(text(rel)), rel


def test_missing_values_are_not_rendered_as_zero():
    src = text("app/static/itmq_core.js")
    assert "if (v === null || v === undefined || v === '') return d;" in src


def test_new_terminal_routes_are_registered():
    src = text("app/main.py")
    for route in ("/api/terminal/bundle", "/api/providers/parity",
                  "/api/quantdata/coverage", "/api/quantdata/tool/{tool_key}"):
        assert route in src, route

"""v1.41.5 · IV Rank propio del motor y procedencia del volumen.

Dos huecos que quedaban en pantalla no por falta de dato, sino por depender de una
sola fuente:

* El IV Rank sólo lo podía aportar Quant Data. Si esa herramienta no respondía, el
  número se quedaba vacío toda la sesión aunque el motor llevara horas observando
  la misma IV. Eso contradice la paridad de proveedores del resto del programa.
* CONTRATOS NEGOCIADOS mostraba 0 cuando no había cinta, aunque la cadena
  reportara miles de contratos de volumen oficial. Un cero es una afirmación sobre
  el mercado; "todavía no he visto ninguna impresión" es otra cosa.
"""
from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ────────────────────────── IV Rank nativo

@pytest.fixture()
def storage(tmp_path):
    return tmp_path


def _seed(storage, symbol, ivs, expiry_mode="AUTO"):
    """Escribe una historia de sesión con las IV dadas, como haría el motor."""
    from app.core.session_memory import _path
    p = _path(storage, symbol)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [{
        "timestamp": (pd.Timestamp("2026-09-17 09:30") + pd.Timedelta(minutes=i)).isoformat(),
        "symbol": symbol.upper(), "expiry_mode": expiry_mode, "spot": 534.0 + i * 0.01,
        "gamma_center": 534.0, "delta_center": 534.0, "gamma_flip": 533.0,
        "net_gex": 1.0, "net_delta": 1.0, "call_delta": 1.0, "put_delta": -1.0,
        "call_gex": 1.0, "put_gex": -1.0, "direction": "NEUTRAL", "edge_state": "",
        "evidence_score": 50.0, "regime": "STABLE",
        "atm_iv": iv, "skew_25d": 0.3, "realized_vol_pct": None,
    } for i, iv in enumerate(ivs)]
    pd.DataFrame(rows).to_csv(p, index=False)
    return p


def test_engine_persists_the_volatility_it_observes(storage):
    """Sin guardar la IV con la estructura, no hay historia con la que rankearla."""
    from app.core.session_memory import append_session_metric, session_metric_frame
    append_session_metric(storage, "DIA", {"spot": 534.0, "gamma_center": 534.0},
                          {"direction": "ALCISTA"}, "AUTO",
                          {"atm_iv": 19.7, "skew_25d": 0.32, "realized_volatility_pct": 14.2})
    df = session_metric_frame(storage, "DIA", "AUTO")
    assert float(df["atm_iv"].iloc[-1]) == pytest.approx(19.7)
    assert float(df["skew_25d"].iloc[-1]) == pytest.approx(0.32)


def test_volatility_argument_is_optional_for_older_callers(storage):
    from app.core.session_memory import append_session_metric, session_metric_frame
    append_session_metric(storage, "DIA", {"spot": 534.0}, {"direction": "NEUTRAL"}, "AUTO")
    df = session_metric_frame(storage, "DIA", "AUTO")
    assert df["atm_iv"].isna().all()          # vacío, no cero


def test_iv_rank_places_the_current_reading_inside_its_observed_range(storage):
    from app.core.session_memory import iv_rank_native
    _seed(storage, "DIA", list(np.linspace(16.0, 24.0, 60)))
    r = iv_rank_native(storage, "DIA", "AUTO", 20.0)
    assert r["ready"] and r["source"] == "ITM_QUANT"
    assert r["low"] == pytest.approx(16.0) and r["high"] == pytest.approx(24.0)
    assert r["rank"] == pytest.approx(50.0, abs=0.01)      # (20−16)/(24−16)


def test_iv_at_the_extremes_ranks_at_the_extremes(storage):
    from app.core.session_memory import iv_rank_native
    _seed(storage, "DIA", list(np.linspace(16.0, 24.0, 60)))
    assert iv_rank_native(storage, "DIA", "AUTO", 16.0)["rank"] == pytest.approx(0.0)
    assert iv_rank_native(storage, "DIA", "AUTO", 24.0)["rank"] == pytest.approx(100.0)
    # Fuera del rango observado se recorta en vez de pasar de 100 o bajar de 0.
    assert iv_rank_native(storage, "DIA", "AUTO", 40.0)["rank"] == 100.0
    assert iv_rank_native(storage, "DIA", "AUTO", 1.0)["rank"] == 0.0


def test_rank_and_percentile_are_different_numbers(storage):
    """Con una distribución sesgada, un pico aislado domina el rank pero apenas
    mueve el percentil. Publicar sólo uno de los dos ocultaría esa diferencia."""
    from app.core.session_memory import iv_rank_native
    ivs = [18.0] * 55 + [40.0] * 5          # un pico aislado al final
    _seed(storage, "DIA", ivs)
    r = iv_rank_native(storage, "DIA", "AUTO", 19.0)
    assert r["rank"] < 10.0                  # el máximo de 40 aplasta el rango
    assert r["percentile"] > 90.0            # pero casi todo está por debajo de 19
    assert r["median"] == pytest.approx(18.0)


def test_a_short_history_refuses_to_publish_a_rank(storage):
    """Un percentil sobre cuatro lecturas no es un percentil, es una coincidencia."""
    from app.core.session_memory import iv_rank_native
    _seed(storage, "DIA", [18.0, 19.0, 20.0, 21.0])
    r = iv_rank_native(storage, "DIA", "AUTO", 20.0)
    assert r["ready"] is False
    assert r["samples"] == 4 and "INSUFICIENTE" in r["reason"]


def test_missing_history_and_missing_iv_are_reported_separately(storage):
    from app.core.session_memory import iv_rank_native
    assert "HISTORIA" in iv_rank_native(storage, "NADA", "AUTO", 20.0)["reason"]
    _seed(storage, "DIA", list(np.linspace(16.0, 24.0, 60)))
    assert "IV ATM" in iv_rank_native(storage, "DIA", "AUTO", None)["reason"]
    assert "IV ATM" in iv_rank_native(storage, "DIA", "AUTO", 0.0)["reason"]


def test_a_flat_history_has_no_rank_but_still_has_a_percentile(storage):
    """Rango cero: dividir por él daría infinito. El rank viaja vacío; el percentil
    sigue siendo legítimo."""
    from app.core.session_memory import iv_rank_native
    _seed(storage, "DIA", [20.0] * 60)
    r = iv_rank_native(storage, "DIA", "AUTO", 20.0)
    assert r["ready"] and r["rank"] is None
    assert r["percentile"] == pytest.approx(100.0)


def test_rank_is_scoped_to_the_expiry_window(storage):
    """0DTE y mensual no comparten régimen de volatilidad: mezclarlos daría un
    rank que no describe ninguna de las dos."""
    from app.core.session_memory import iv_rank_native, _path
    _seed(storage, "DIA", list(np.linspace(16.0, 24.0, 40)), expiry_mode="ZERO_DTE")
    extra = pd.DataFrame([{
        "timestamp": (pd.Timestamp("2026-09-17 14:00") + pd.Timedelta(minutes=i)).isoformat(),
        "symbol": "DIA", "expiry_mode": "MONTHLY", "spot": 534.0, "atm_iv": 60.0,
    } for i in range(40)])
    extra.to_csv(_path(storage, "DIA"), mode="a", index=False, header=False)
    r = iv_rank_native(storage, "DIA", "ZERO_DTE", 20.0)
    assert r["high"] == pytest.approx(24.0)      # la IV de 60 del mensual no entra


# ────────────────────────── paridad motor / proveedor en el IV Rank

def _native(**over):
    base = {"ready": True, "rank": 62.5, "percentile": 71.0, "samples": 120,
            "window_minutes": 380.0, "low": 16.0, "high": 24.0, "median": 19.0, "note": "n"}
    base.update(over)
    return base


def test_the_engine_reading_wins_when_it_has_enough_history():
    from app.terminal_api import _volatilidad
    v = _volatilidad({"volatility": {"atm_iv": 19.7}, "iv_rank_native": _native()},
                     {"iv_rank": {"raw": {"ivRank": 44.0}}})
    assert v["iv_rank"] == 62.5 and v["iv_rank_source"] == "ITM_QUANT"
    assert v["iv_rank_provider"] == 44.0          # la lectura del proveedor no se pierde


def test_the_provider_covers_the_engine_while_it_has_no_history():
    from app.terminal_api import _volatilidad
    v = _volatilidad({"volatility": {"atm_iv": 19.7},
                      "iv_rank_native": {"ready": False, "reason": "HISTORIA INSUFICIENTE · 4/30 observaciones"}},
                     {"iv_rank": {"raw": {"ivRank": 44.0}}})
    assert v["iv_rank"] == 44.0 and v["iv_rank_source"] == "QUANTDATA"


def test_without_either_source_the_reason_travels_instead_of_a_number():
    from app.terminal_api import _volatilidad
    v = _volatilidad({"volatility": {"atm_iv": 19.7},
                      "iv_rank_native": {"ready": False, "reason": "SIN HISTORIA DE IV OBSERVADA"}}, {})
    assert v["iv_rank"] is None and v["iv_rank_source"] is None
    assert v["iv_rank_reason"] == "SIN HISTORIA DE IV OBSERVADA"


def test_the_two_readings_are_compared_never_averaged():
    """Miden ventanas distintas: promediarlas daría un número que no describe
    ninguna de las dos. La separación se publica como aviso."""
    from app.terminal_api import _volatilidad
    v = _volatilidad({"volatility": {"atm_iv": 19.7}, "iv_rank_native": _native(rank=80.0)},
                     {"iv_rank": {"raw": {"ivRank": 30.0}}})
    assert v["iv_rank"] == 80.0                    # no 55.0
    assert v["iv_rank_divergence"] == 50.0


def test_iv_rank_provenance_reaches_the_screen():
    app = text("app/static/itmq_app.js")
    assert "v.iv_rank_source" in app and "motor · historia propia" in app
    html = text("app/templates/terminal.html")
    for el_id in ("volRankPill", "volPct", "volRange", "volMedian", "volDiverge", "volRankNote"):
        assert f'id="{el_id}"' in html, el_id


def test_engine_publishes_the_native_rank_in_its_state():
    src = text("app/service.py")
    assert '"iv_rank_native": iv_rank_native(' in src
    assert "self.expiry_window," in src
    # La IV observada se persiste en las dos rutas de refresco, no sólo en una.
    assert src.count("self.expiry_window, vol)") == 2


# ────────────────────────── procedencia del volumen en estadísticas

def _chain(rows):
    return {"option_prints": [], "profiles": {"rows": rows}}


def test_official_chain_volume_replaces_a_misleading_zero():
    """CONTRATOS NEGOCIADOS = 0 con la cadena reportando miles es una afirmación
    falsa sobre el mercado, no un hueco honesto."""
    from app.terminal_api import _estadisticas
    st = _estadisticas(_chain([
        {"strike": 535.0, "call_volume": 900.0, "put_volume": 700.0},
        {"strike": 536.0, "call_volume": 120.0, "put_volume": 0.0},
    ]), {}, {})
    assert st["contracts"] == pytest.approx(1720.0)
    assert st["volume_source"] == "CADENA_OFICIAL"
    assert {r["source"] for r in st["contract_rows"]} == {"CADENA_OFICIAL"}


def test_observed_prints_always_win_over_the_chain():
    from app.terminal_api import _estadisticas
    st = _estadisticas({"option_prints": [
        {"strike": 535.0, "option_type": "call", "contracts": 10, "premium": 5000, "direction": 1}],
        "profiles": {"rows": [{"strike": 535.0, "call_volume": 900.0, "put_volume": 0.0}]}}, {}, {})
    assert st["volume_source"] == "PRINTS_OBSERVADOS"
    assert st["contracts"] == pytest.approx(10.0)


def test_chain_rows_do_not_invent_premium_trades_or_bias():
    """En la cadena oficial no hay cinta que clasificar: esos campos viajan vacíos
    en lugar de rellenarse con ceros que parecerían medidos."""
    from app.terminal_api import _estadisticas
    row = _estadisticas(_chain([{"strike": 535.0, "call_volume": 900.0, "put_volume": 0.0}]), {}, {})["contract_rows"][0]
    assert row["premium"] is None and row["trades"] is None and row["bias"] is None
    assert row["contracts"] == 900.0


def test_average_print_size_needs_prints():
    """Con volumen de cadena no hay impresiones que promediar."""
    from app.terminal_api import _estadisticas
    assert _estadisticas(_chain([{"strike": 535.0, "call_volume": 900.0, "put_volume": 0.0}]), {}, {})["avg_size"] is None


def test_engine_aggregate_is_the_last_resort():
    from app.terminal_api import _estadisticas
    st = _estadisticas({"option_prints": []}, {"positioning": {"call_volume": 6856.0, "put_volume": 6022.0}}, {})
    assert st["volume_source"] == "AGREGADO_DEL_MOTOR"
    assert st["contracts"] == pytest.approx(12878.0)


def test_with_no_source_at_all_the_reason_is_explicit():
    from app.terminal_api import _estadisticas
    st = _estadisticas({"option_prints": []}, {}, {})
    assert st["volume_source"] is None
    assert st["volume_reason"] == "SIN CINTA NI VOLUMEN DE CADENA"


def test_volume_provenance_reaches_the_screen():
    app = text("app/static/itmq_app.js")
    assert "s.volume_source === 'CADENA_OFICIAL'" in app
    assert "r.premium == null ? '—'" in app       # nunca un cero inventado
    html = text("app/templates/terminal.html")
    assert 'id="stContractsDetail"' in html
    head = html[html.find('id="tblContracts"'):]
    assert "FUENTE" in head[:head.find("</thead>")]


# ────────────────────────── descubrimiento de rutas del proveedor

def test_routes_are_canonical_and_never_derived():
    """v1.42.1 · Las rutas no se adivinan.

    Hasta v1.42.0 cada herramienta derivaba hasta doce variantes (camelCase,
    snake_case, sin `/options`, colgando de `/v1/tool`...). La intención era
    sobrevivir a un renombrado del proveedor; el efecto real fue peor:

        dark-pool-levels → HTTP 404 en '/v1/tool/dark-pool-levels' · 6/6 probadas

    Esa URL no existe en ninguna versión de la API: la inventó el derivador. El
    diagnóstico mostraba el ÚLTIMO intento, así que el operador leía una ruta falsa
    y concluía que la herramienta no estaba disponible. Además, cada herramienta
    ausente gastaba seis peticiones de cuota por ciclo en URLs imaginarias.
    """
    from app.providers.quantdata.tools import path_variants

    v = path_variants(("/v1/options/tool/order-flow",))
    assert v == ("/v1/options/tool/order-flow",)
    for inventada in ("/v1/options/tool/orderFlow", "/v1/options/tool/order_flow",
                      "/v1/tool/order-flow", "/v1/options/order-flow"):
        assert inventada not in v


def test_every_tool_declares_exactly_one_canonical_route():
    from app.providers.quantdata.tools import build_catalog

    multi = {k: t.paths for k, t in build_catalog().items() if len(t.paths) != 1}
    assert not multi, f"herramientas sin ruta canónica única: {multi}"


def test_the_four_wrong_routes_are_corrected():
    """Las rutas que producían 404 en producción, contra la API vigente."""
    from app.providers.quantdata.tools import build_catalog

    c = build_catalog()
    assert c["options_order_flow"].paths == ("/v1/options/tool/order-flow/consolidated",)
    assert c["options_order_flow_raw"].paths == ("/v1/options/tool/order-flow/unconsolidated",)
    assert c["gainers_losers"].paths == ("/v1/options/tool/gainers-losers",)
    assert c["news"].paths == ("/v1/news/tool/news-articles",)
    assert c["dark_pool_levels"].paths == ("/v1/equities/tool/dark-pool-levels",)
    assert c["equity_prints"].paths == ("/v1/equities/tool/equity-prints",)
    assert c["stock_price_over_time"].paths == ("/v1/equities/tool/stock-price-over-time",)


def test_a_rejected_canonical_route_is_actionable():
    """Un 404 sobre la ruta oficial debe decir qué hacer, no sólo que no hay datos."""
    from app.providers.quantdata.tools import build_catalog, route_diagnostic, ROUTE_INVALID

    tool = build_catalog()["dark_pool_levels"]
    tool.route_state = ROUTE_INVALID
    d = route_diagnostic(tool)
    assert d["state"] == ROUTE_INVALID
    assert d["route"] == "/v1/equities/tool/dark-pool-levels"
    assert "plan no incluye" in d["detail"] or "renombró" in d["detail"]


def test_declared_paths_keep_their_priority():
    from app.providers.quantdata.tools import path_variants
    v = path_variants(("/v1/a/tool/x-y", "/v1/b/tool/z"))
    assert v[0] == "/v1/a/tool/x-y"
    assert "/v1/b/tool/z" in v


def test_variants_are_bounded():
    """Cada variante es una petición contra la cuota: probarlas todas en cada ciclo
    costaría más de lo que vale descubrir la ruta."""
    from app.providers.quantdata.tools import path_variants
    assert len(path_variants(("/v1/a/tool/a-b-c-d", "/v1/b/tool/e-f-g"), limit=5)) <= 5


def test_a_resolved_path_stops_the_search():
    from app.providers.quantdata.tools import build_catalog
    tool = build_catalog()["net_flow"]
    tool.resolved_path = "/v1/options/tool/net-flow"
    assert tool.candidates() == ("/v1/options/tool/net-flow",)


def test_every_attempt_is_recorded_with_what_the_provider_answered():
    from app.providers.quantdata.tools import build_catalog
    tool = build_catalog()["news"]
    tool.note_attempt("/v1/options/tool/news", "404 tool not found")
    tool.note_attempt("/v1/tool/news", None)
    assert [a["ok"] for a in tool.attempts] == [False, True]
    assert tool.attempts[0]["error"] == "404 tool not found"


def test_retrying_the_same_path_replaces_its_previous_attempt():
    """Un reintento no debe acumular la misma ruta una y otra vez hasta llenar la
    tabla de diagnóstico con la misma línea."""
    from app.providers.quantdata.tools import build_catalog
    tool = build_catalog()["news"]
    for _ in range(5):
        tool.note_attempt("/v1/options/tool/news", "404")
    assert len(tool.attempts) == 1


def test_coverage_publishes_the_attempts_and_the_candidates():
    src = text("app/providers/quantdata/intelligence.py")
    assert '"attempts": [' in src and '"candidates": list(tool.candidates())' in src
    assert src.count("tool.note_attempt(") == 3       # fallo de ruta, excepción y éxito


def test_the_screen_turns_an_unavailable_tool_into_something_actionable():
    app = text("app/static/itmq_app.js")
    assert "function qdDiagnosis(t)" in app
    assert "rutas probadas" in app
    html = text("app/templates/terminal.html")
    head = html[html.find('id="tblQdTools"'):]
    assert "RUTA / DIAGNÓSTICO" in head[:head.find("</thead>")]


# ────────────────────────── autonomía de la terminal en el VPS

def test_the_terminal_loads_nothing_from_the_internet():
    """Un VPS con salida restringida no puede depender de que un CDN responda para
    dibujar la pantalla principal. Esta prueba impide que vuelva a entrar una."""
    import re
    html = text("app/templates/terminal.html")
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not external, f"la terminal cargaría recursos externos: {external}"
    assert "@import" not in html and "fonts.googleapis" not in html


def test_every_terminal_module_is_served_from_the_app_itself():
    import re
    html = text("app/templates/terminal.html")
    srcs = re.findall(r'<script src="([^"]+)"', html)
    assert srcs, "la terminal no carga ningún módulo"
    assert all(s.startswith("/static/") for s in srcs), srcs
    for s in srcs:
        rel = s.split("?")[0].lstrip("/")
        assert (ROOT / "app" / rel[len("static/"):] if False else ROOT / "app" / rel).is_file(), s


def test_plotly_is_vendored_not_fetched():
    dash = text("app/templates/dashboard.html")
    assert '<script src="/static/vendor/plotly.min.js"></script>' in dash
    assert (ROOT / "app/static/vendor/plotly.min.js").is_file()


def test_the_legacy_page_degrades_instead_of_breaking_without_its_cdn():
    """Lightweight Charts se sirve desde un CDN sólo en la página heredada. Si no
    llega, el render cae a Plotly, que sí es local: la página no se rompe."""
    src = text("app/static/market_line_terminal.js")
    assert "if(!L()?.createChart)return cleanPlotlyFallback(id,spec);" in src
    trace = text("app/static/lightweight_trace.js")
    assert "if(!host||!L?.createChart" in trace          # sale sin lanzar
    # Y la terminal principal no usa esa biblioteca en absoluto.
    for module in ("itmq_core.js", "itmq_trace.js", "itmq_orderflow.js",
                   "itmq_panels.js", "itmq_app.js"):
        assert "LightweightCharts" not in text(f"app/static/{module}"), module


# ────────────────────────── forma del perfil de exposición

def _profile():
    return [{"strike": 530.0, "gex": -5e6}, {"strike": 534.0, "gex": 2e6},
            {"strike": 538.0, "gex": 9e6}, {"strike": 540.0, "gex": 1e5}]


def test_concentration_measures_how_much_lives_in_the_top_strikes():
    """Si la gamma está en tres strikes, esos strikes mandan. Si está repartida
    entre cuarenta, ninguno lo hace. El total no distingue esos dos mercados."""
    from app.terminal_api import _exposure_shape
    tight = _exposure_shape([{"strike": 530.0 + i, "gex": 1e7 if i < 3 else 1e3}
                             for i in range(40)], 534.0)
    spread = _exposure_shape([{"strike": 530.0 + i, "gex": 1e6} for i in range(40)], 534.0)
    assert tight["concentration_pct"] > 95.0
    assert spread["concentration_pct"] < 12.0


def test_dominant_strike_is_the_largest_absolute_exposure():
    """Absoluta, no con signo: un muro de puts enorme pesa tanto como uno de calls."""
    from app.terminal_api import _exposure_shape
    sh = _exposure_shape([{"strike": 530.0, "gex": -9e6}, {"strike": 538.0, "gex": 2e6}], 534.0)
    assert sh["dominant_strike"] == 530.0
    assert sh["dominant_distance_pct"] == pytest.approx(-0.749, abs=0.01)


def test_balance_says_which_side_of_price_the_hedging_sits_on():
    from app.terminal_api import _exposure_shape
    sh = _exposure_shape(_profile(), 534.2)
    assert sh["gex_below"] == pytest.approx(-3e6)     # 530 y 534 quedan debajo
    assert sh["gex_above"] == pytest.approx(9.1e6)
    assert sh["gex_balance_pct"] > 0                  # pesa arriba


def test_balance_reaches_its_extremes_when_everything_is_on_one_side():
    from app.terminal_api import _exposure_shape
    arriba = _exposure_shape([{"strike": 540.0, "gex": 5e6}, {"strike": 545.0, "gex": 3e6}], 534.0)
    abajo = _exposure_shape([{"strike": 520.0, "gex": 5e6}, {"strike": 525.0, "gex": 3e6}], 534.0)
    assert arriba["gex_balance_pct"] == pytest.approx(100.0)
    assert abajo["gex_balance_pct"] == pytest.approx(-100.0)


def test_shape_without_a_profile_says_so_instead_of_returning_zeros():
    from app.terminal_api import _exposure_shape
    sh = _exposure_shape([], 534.0)
    assert sh["concentration_pct"] is None and sh["dominant_strike"] is None
    assert sh["shape_reason"] == "SIN PERFIL POR STRIKE"


def test_shape_without_a_spot_still_reports_concentration():
    """El reparto arriba/abajo necesita un precio de referencia; la concentración no."""
    from app.terminal_api import _exposure_shape
    sh = _exposure_shape(_profile(), None)
    assert sh["concentration_pct"] is not None
    assert sh["gex_below"] is None and sh["gex_balance_pct"] is None


def test_a_flat_profile_does_not_divide_by_zero():
    from app.terminal_api import _exposure_shape
    sh = _exposure_shape([{"strike": 530.0, "gex": 0.0}, {"strike": 540.0, "gex": 0.0}], 534.0)
    assert sh["concentration_pct"] is None and sh["gex_balance_pct"] is None


def test_exposure_section_carries_the_shape_and_shows_it():
    from app.terminal_api import _exposicion
    ex = _exposicion({"profiles": {"rows": [{"strike": 535.0, "gamma_m": 3.0, "delta_m": 1.0}], "spot": 534.0}},
                     {"spot": 534.0}, {})
    for key in ("concentration_pct", "dominant_strike", "gex_balance_pct", "strikes_counted"):
        assert key in ex, key
    html = text("app/templates/terminal.html")
    for el_id in ("exConc", "exDom", "exBalance", "exCount"):
        assert f'id="{el_id}"' in html, el_id
    assert "ex.concentration_pct" in text("app/static/itmq_app.js")


# ────────────────────────── exposición por vencimiento: cero no era cero

def _enriched():
    """Cadena ya enriquecida, como la que produce enrich_options()."""
    return pd.DataFrame([
        {"timestamp": "2026-09-17T15:00:00", "expiration_date": "2026-09-17", "strike": 534.0,
         "option_type": "call", "open_interest": 1000.0, "volume": 400.0,
         "signed_gex_proxy": 5.0e6, "option_delta_exposure_info": 2.0e6,
         "calc_vanna": 0.01, "calc_charm": -0.02, "contract_multiplier": 100.0,
         "underlying_price": 534.0},
        {"timestamp": "2026-09-17T15:00:00", "expiration_date": "2026-09-18", "strike": 535.0,
         "option_type": "put", "open_interest": 800.0, "volume": 300.0,
         "signed_gex_proxy": -3.0e6, "option_delta_exposure_info": -1.0e6,
         "calc_vanna": 0.02, "calc_charm": 0.01, "contract_multiplier": 100.0,
         "underlying_price": 534.0},
    ])


def _raw():
    """El mismo snapshot SIN enriquecer: le faltan las columnas de exposición."""
    return _enriched().drop(columns=["signed_gex_proxy", "option_delta_exposure_info"])


def test_the_raw_snapshot_cannot_produce_a_greek_breakdown():
    """Demuestra el defecto: con el snapshot crudo, gamma y delta salen en CERO por
    vencimiento aunque OI y volumen lleguen bien. Un panel que parecía roto sin
    estarlo, y un desglose que nunca fue correcto."""
    from app.core.expiry_intelligence import build_expiry_intelligence
    rep = build_expiry_intelligence(_raw(), asof=pd.Timestamp("2026-09-17").date())
    per = rep["per_expiration"]
    assert all(r["gamma"] == 0.0 and r["delta"] == 0.0 for r in per)
    assert any(r["oi"] > 0 for r in per)          # el OI sí llegaba: por eso engañaba


def test_the_enriched_frame_produces_the_real_breakdown():
    from app.core.expiry_intelligence import build_expiry_intelligence
    rep = build_expiry_intelligence(_enriched(), asof=pd.Timestamp("2026-09-17").date())
    by = {r["expiration"]: r for r in rep["per_expiration"]}
    assert by["2026-09-17"]["gamma"] == pytest.approx(5.0e6)
    assert by["2026-09-18"]["gamma"] == pytest.approx(-3.0e6)
    assert by["2026-09-17"]["delta"] == pytest.approx(2.0e6)


def test_the_engine_hands_over_the_enriched_frame():
    from app.service import _exposure_frame
    enr, raw = _enriched(), _raw()
    assert _exposure_frame({"enriched": enr}, raw) is enr


def test_a_partial_enriched_frame_is_not_trusted():
    """Si al frame enriquecido le faltan justo esas columnas, usarlo devolvería los
    mismos ceros silenciosos: se prefiere el snapshot y se deja constancia."""
    from app.service import _exposure_frame
    raw = _raw()
    assert _exposure_frame({"enriched": raw.copy()}, raw) is raw


def test_exposure_frame_falls_back_without_ever_raising():
    from app.service import _exposure_frame
    raw, hist = _raw(), _enriched()
    assert _exposure_frame(None, raw) is raw
    assert _exposure_frame({}, pd.DataFrame(), hist) is hist
    assert _exposure_frame(None, None, None).empty      # vacío, no excepción


def test_every_exposure_consumer_receives_the_enriched_frame():
    src = text("app/service.py")
    assert src.count("_exposure_frame(gd, self.snapshot") == 4
    # Ya no queda ninguna llamada con el snapshot crudo.
    for fn in ("build_expiry_intelligence(", "build_profile_bundle("):
        for call in [c for c in src.split(fn)[1:]]:
            head = call[:60]
            assert "self.snapshot if isinstance" not in head, f"{fn}{head}"

"""v1.41.8 · Arranque real, murallas por lado, detalle de strike y fluidez.

La suite de v1.41.7 pasaba entera con la aplicación ROTA: el arranque lanzaba
`NameError` y ninguna prueba ejecutaba el lifespan. Aquí se arregla ese punto
ciego y se cubre el resto de lo que el usuario vio en su VPS.
"""
from __future__ import annotations

import importlib
import pathlib

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ────────────────────────── el arranque tiene que arrancar

def test_the_application_actually_boots(monkeypatch):
    """Punto ciego real: la suite pasaba con la app rota porque nada ejecutaba el
    lifespan. Un NameError en el arranque llegaba al usuario intacto."""
    monkeypatch.setenv("ITM_DEMO_MODE", "1")
    from fastapi.testclient import TestClient
    import app.main as M
    with TestClient(M.app) as client:
        assert client.get("/health").status_code == 200


def test_the_startup_imports_what_it_logs_with():
    """El NameError del arranque fue exactamente esto: usar un logger sin importarlo.
    La prueba de arranque de arriba lo detecta ejecutando; ésta lo deja explícito."""
    src = text("app/main.py")
    assert "\nimport logging\n" in src
    assert "get_logger(\"itm.providers\")" not in src      # el nombre que no existía


def test_the_startup_announces_the_active_roster():
    src = text("app/main.py")
    assert 'logging.getLogger("itm.providers")' in src
    assert "roster de proveedores" in src


# ────────────────────────── tastytrade: el roster no admite anulación

def test_the_env_flag_can_no_longer_smuggle_a_retired_provider(monkeypatch):
    """Un .env heredado con TASTYTRADE_ENABLED=1 seguía levantando el DXLink aunque
    el proveedor estuviera retirado: el operador veía en el log justo lo que había
    pedido quitar."""
    import app.core.provider_parity as P
    import app.providers.tastytrade.settings as S
    monkeypatch.delenv("ITM_OPTIONS_PEERS", raising=False)
    monkeypatch.setenv("TASTYTRADE_ENABLED", "1")
    monkeypatch.setenv("TASTYTRADE_CLIENT_SECRET", "x")
    monkeypatch.setenv("TASTYTRADE_REFRESH_TOKEN", "y")
    importlib.reload(P); importlib.reload(S)
    assert S.load_settings() is None
    monkeypatch.setenv("ITM_OPTIONS_PEERS", "ALPACA,TASTYTRADE,QUANTDATA")
    importlib.reload(P); importlib.reload(S)
    assert S.load_settings() is not None
    monkeypatch.delenv("ITM_OPTIONS_PEERS", raising=False)
    importlib.reload(P); importlib.reload(S)


def test_a_retired_provider_is_never_even_scheduled():
    src = text("app/main.py")
    assert 'if _peer_on("TASTYTRADE") else None' in src
    # Y el apagado tolera que esa tarea no exista.
    assert "if t is None:\n                continue" in src


# ────────────────────────── murallas por lado, no por neto

def _chain():
    """522 tiene la mayor gamma DE CALLS, pero 524 gana en neto porque en 522 las
    puts compensan. El método neto marcaba el muro en el sitio equivocado."""
    return pd.DataFrame([
        {"strike": 522.0, "signed_gex": 1.0e6, "gross_gex": 9.0e6, "call_gamma": 5.0e6, "put_gamma": -4.0e6},
        {"strike": 524.0, "signed_gex": 3.0e6, "gross_gex": 3.0e6, "call_gamma": 3.0e6, "put_gamma": 0.0},
        {"strike": 516.0, "signed_gex": -2.0e6, "gross_gex": 8.0e6, "call_gamma": 3.0e6, "put_gamma": -5.0e6},
        {"strike": 514.0, "signed_gex": -4.0e6, "gross_gex": 4.0e6, "call_gamma": 0.0, "put_gamma": -4.0e6},
    ])


def test_the_call_wall_is_where_call_gamma_concentrates():
    from app.core.trace_analytics import structural_walls
    w = structural_walls(_chain(), 520.0)
    assert w["call_wall"] == 522.0
    assert w["put_wall"] == 516.0
    assert w["method"] == "gamma-weighted-by-side"


def test_without_the_split_the_net_method_still_works_and_says_so():
    """Una cadena antigua sin desglose no se queda sin muros: usa el neto y lo
    declara, en vez de fingir una precisión que no tiene."""
    from app.core.trace_analytics import structural_walls
    w = structural_walls(_chain().drop(columns=["call_gamma", "put_gamma"]), 520.0)
    assert w["call_wall"] == 524.0 and w["method"] == "gamma-weighted-net"


def test_walls_stay_on_their_side_of_spot():
    from app.core.trace_analytics import structural_walls
    w = structural_walls(_chain(), 520.0)
    assert w["call_wall"] > 520.0 > w["put_wall"]


def test_the_aggregation_now_carries_the_call_put_split():
    """Sin estas columnas la mejora de los muros sería inerte."""
    from app.core.engine import aggregate_strikes
    g = aggregate_strikes(pd.DataFrame([
        {"timestamp": "t1", "strike": 522.0, "option_type": "call", "signed_gex_proxy": 5e6,
         "gross_gex": 5e6, "open_interest": 100, "volume": 10, "iv": 0.2, "underlying_price": 520.0},
        {"timestamp": "t1", "strike": 522.0, "option_type": "put", "signed_gex_proxy": -4e6,
         "gross_gex": 4e6, "open_interest": 80, "volume": 8, "iv": 0.2, "underlying_price": 520.0},
    ]))
    r = g.iloc[0]
    assert r["call_gamma"] == pytest.approx(5e6) and r["put_gamma"] == pytest.approx(-4e6)
    assert r["call_oi"] == 100 and r["put_oi"] == 80
    assert r["signed_gex"] == pytest.approx(1e6)      # el neto no cambia


def test_a_chain_without_option_type_does_not_break_the_aggregation():
    from app.core.engine import aggregate_strikes
    g = aggregate_strikes(pd.DataFrame([
        {"timestamp": "t1", "strike": 522.0, "signed_gex_proxy": 5e6, "gross_gex": 5e6,
         "open_interest": 100, "volume": 10, "iv": 0.2, "underlying_price": 520.0}]))
    assert g.iloc[0]["call_gamma"] == 0.0


# ────────────────────────── clasificación del agresor

def test_the_tick_rule_rescues_the_tape_that_had_no_quote():
    """Casi toda la prima quedaba en UNKNOWN sin NBBO del instante, y eso dejaba el
    carril de flujo neto plano con millones negociados delante."""
    from app.core.flow_intelligence import _tick_rule
    assert _tick_rule(1.25, 1.20) == ("BUY", 0.30)
    assert _tick_rule(1.15, 1.20) == ("SELL", 0.30)


def test_the_tick_rule_refuses_to_guess():
    from app.core.flow_intelligence import _tick_rule
    for args in ((1.20, 1.20), (1.20, None), (1.20, 0.0), (0.0, 1.20)):
        assert _tick_rule(*args) == (None, 0.0), args


def test_the_quote_rule_always_wins_and_the_method_travels():
    from app.core.flow_intelligence import _classify_aggressor
    assert _classify_aggressor(1.30, 1.20, 1.30) == ("BUY", 1.0)
    src = text("app/core/flow_intelligence.py")
    assert "'aggressor_method':method" in src
    assert "method='QUOTE' if ag in ('BUY','SELL','MID')" in src
    assert "'TICK_RULE'" in src


def test_the_tick_rule_is_less_confident_than_a_quote():
    """Es una inferencia más débil y su confianza tiene que reflejarlo."""
    from app.core.flow_intelligence import _tick_rule, _classify_aggressor
    assert _tick_rule(1.25, 1.20)[1] < _classify_aggressor(1.30, 1.20, 1.30)[1]


# ────────────────────────── detalle del strike seleccionado

def test_a_click_pins_a_strike_and_a_second_click_releases_it():
    """Con CALL+PUT se ve la barra partida, pero sus NÚMEROS no estaban en ninguna
    parte, y al soltar el ratón se perdía hasta el resalte."""
    src = text("app/static/itmq_trace.js")
    assert "function onSideClick(side)" in src
    assert "S.pinnedStrike = (Q.isNum(S.pinnedStrike)" in src
    assert "onClick: onSideClick('left')" in src and "onClick: onSideClick('right')" in src


def test_the_strike_detail_carries_the_call_put_split_and_the_total():
    src = text("app/static/itmq_trace.js")
    i = src.find("function strikeRow(k)")
    block = src[i:src.find("\n  }", i)]
    for field in ("gex_call", "gex_put", "gex:", "dex_call", "dex_put", "dex:",
                  "call_oi", "put_oi", "call_volume", "put_volume",
                  "gex_live", "dex_live", "liquidity", "gravity"):
        assert field in block, field


def test_the_pinned_strike_refreshes_with_every_data_cycle():
    """Si no, el detalle se quedaría con los números del instante del clic."""
    src = text("app/static/itmq_trace.js")
    assert "if (Q.isNum(S.pinnedStrike)) emitStrike();" in src


def test_the_detail_card_exists_and_is_wired():
    html = text("app/templates/terminal.html")
    assert 'id="strikeCard"' in html
    for el_id in ("skStrike", "skDist", "skRows", "skClose"):
        assert f'id="{el_id}"' in html, el_id
    app = text("app/static/itmq_app.js")
    assert "Trace.onStrike(renderStrikeCard)" in app
    assert "function renderStrikeCard(r)" in app


# ────────────────────────── fluidez

def test_the_public_state_is_memoised_by_analytics_revision():
    """Reconstruirlo costaba 75-140 ms y lo caro son analíticas que NO cambian entre
    ciclos del motor. Era el cuello de botella de toda la terminal."""
    src = text("app/service.py")
    assert "_public_state_cache" in src
    assert "def _build_public_state(self)" in src
    key = src[src.find("        key = (str(self.symbol)"):]
    key = key[:key.find("\n        hit")]
    for part in ("symbol_epoch", "analytics_revision", "is_replay", "expiry_window"):
        assert part in key, part


def test_the_cached_state_still_refreshes_the_live_price():
    """Congelar la estructura entre ciclos es correcto; congelar el precio no."""
    src = text("app/service.py")
    block = src[src.find("def _refresh_public_live"):src.find("def _build_public_state")]
    assert 'out["spot"] = live' in block
    assert "PROVIDER_BUS.snapshot" in block
    # En replay el reloj lo manda el usuario: no se adelanta nada.
    assert "if self.replay_context.is_replay:" in block


def test_the_cache_hands_out_a_copy_not_its_own_dict():
    """Quien reciba el estado no puede mutar el que queda guardado."""
    src = text("app/service.py")
    assert "out = dict(cached)" in src


def test_heavy_payloads_are_compressed_for_the_vps():
    """El bundle y el trace son ~90 y ~100 KB cada pocos segundos: contra un VPS son
    cientos de KB por minuto. Comprimen alrededor de un 65%."""
    src = text("app/main.py")
    assert "app.add_middleware(GZipMiddleware" in src
    assert "minimum_size=1024" in src          # el tick de 130 B no se comprime


def test_the_monotonic_clock_helper_avoids_the_datetime_time_shadow():
    """En service.py `time` es datetime.time: time.time() no existe ahí."""
    from app.service import _now_monotonic
    a = _now_monotonic()
    assert isinstance(a, float) and _now_monotonic() >= a


# ────────────────────────── replay: empieza donde puede reconstruir

def test_the_fallback_clock_starts_at_the_first_structural_instant():
    """El fichero de historia puede empezar la tarde anterior mientras la cadena aún
    no existe: elegir esa marca hacía que el motor rechazara el replay nada más
    abrir la sesión."""
    src = text("app/core/replay.py")
    assert "def _first_structural_timestamp(" in src
    block = src[src.find("    start, end = ts.iloc[0], ts.iloc[-1]"):]
    block = block[:block.find("step = pd.Timedelta")]
    assert "structural = _first_structural_timestamp(storage, symbol, day)" in block
    assert "start = structural" in block


def test_the_clock_reports_both_starts():
    src = text("app/core/replay.py")
    assert '"observed_start"' in src and '"structural_start"' in src


def test_a_replay_without_structure_degrades_instead_of_being_rejected():
    """Rechazarlo dejaba la pantalla entera vacía con un cartel rojo, cuando el
    recorrido del precio de esa sesión sí existe y sirve para leerla."""
    src = text("app/service.py")
    assert '"degraded_to_price":self._replay_price_only(ctx)' in src
    block = src[src.find("def _replay_price_only"):src.find("def _instant_replay_package")]
    assert "load_tape" in block
    assert "GEX, DEX y los muros quedan vacíos con su motivo" in block

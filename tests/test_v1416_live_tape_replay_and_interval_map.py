"""v1.41.6 · Cinta OPRA reparada, velas tick a tick, replay e Interval Map nativo.

Cuatro cosas que el usuario vio en su VPS y que eran defectos reales:

* `Alpaca HTTP 400: unexpected query parameter(s): feed` cada dos minutos. El
  endpoint de trades de opciones no acepta ese parámetro y se lo enviábamos, así
  que la cinta OPRA se perdía entera en cada ciclo.
* Las velas sólo avanzaban cada 2,5 s, el ritmo del trace pesado.
* El Interval Map salía vacío aunque el motor ya calculaba esa misma matriz.
* No había forma de reproducir una sesión archivada para hacer backtesting.
"""
from __future__ import annotations

import pathlib

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ────────────────────────── el 400 que perdía la cinta OPRA

def test_option_trades_no_longer_sends_the_parameter_alpaca_rejects():
    """/v1beta1/options/trades no acepta `feed`. Enviarlo devolvía 400 y la petición
    entera se perdía: el flujo de opciones quedaba sin datos en cada ciclo."""
    src = text("app/core/flow_intelligence.py")
    block = src[src.find("def fetch_recent_option_trades"):]
    block = block[:block.find("\ndef ", 10)]
    params = block[block.find("params={"):block.find("\n", block.find("params={"))]
    assert "'feed'" not in params and '"feed"' not in params, params
    assert "'symbols'" in params and "'start'" in params and "'end'" in params


def test_the_client_learns_which_parameters_an_endpoint_rejects():
    """El proveedor nombra el parámetro en el propio mensaje de error. Aprovecharlo
    convierte un 400 repetido cada ciclo en una petición que se corrige sola."""
    import app.core.flow_intelligence as F
    m = F._UNEXPECTED_PARAM_RE.search("unexpected query parameter(s): feed")
    assert m and m.group(1).strip() == "feed"
    m2 = F._UNEXPECTED_PARAM_RE.search("unexpected query parameter(s): feed, currency")
    assert {n.strip() for n in m2.group(1).split(",")} == {"feed", "currency"}


def test_a_rejected_parameter_is_remembered_per_endpoint():
    """La segunda llamada ya sale limpia: no se repite el 400 en cada ciclo."""
    import app.core.flow_intelligence as F
    url = "https://data.example/v1beta1/options/trades"
    F._REJECTED_PARAMS.pop(F._endpoint_key(url), None)
    assert F._drop_known_rejected(url, {"feed": "opra", "limit": 10})[0] == {"feed": "opra", "limit": 10}
    F._remember_rejected(url, {"feed"})
    cleaned, dropped = F._drop_known_rejected(url + "?x=1", {"feed": "opra", "limit": 10})
    assert cleaned == {"limit": 10} and dropped == {"feed"}
    # Lo aprendido es por endpoint, no global: otro endpoint sigue intacto.
    otro = "https://data.example/v2/stocks/DIA/bars"
    assert F._drop_known_rejected(otro, {"feed": "sip"})[0] == {"feed": "sip"}
    F._REJECTED_PARAMS.pop(F._endpoint_key(url), None)


def test_only_parameters_actually_sent_are_dropped():
    """Un mensaje que nombra algo que no enviamos no debe vaciar la petición."""
    import app.core.flow_intelligence as F
    src = text("app/core/flow_intelligence.py")
    assert "names &= set(params)" in src
    # Y el reintento es uno solo: no se entra en bucle contra el proveedor.
    assert "for attempt in (0, 1):" in src


# ────────────────────────── velas tick a tick

def test_a_price_only_endpoint_exists_and_calculates_nothing():
    """El bundle y el trace reconstruyen estructura: no pueden pedirse cada 350 ms.
    El precio sí, porque aquí sólo se lee lo que la fabric ya publicó."""
    src = text("app/main.py")
    assert '@app.get("/api/terminal/tick")' in src
    block = src[src.find('@app.get("/api/terminal/tick")'):]
    block = block[:block.find("@app.get", 10)]
    for heavy in ("nextgen_trace", "build_terminal_bundle", "CHART_DATA_CACHE"):
        assert heavy not in block, heavy
    assert "PROVIDER_BUS.snapshot" in block


def test_the_tick_declares_its_source_and_replay_state():
    src = text("app/main.py")
    block = src[src.find('@app.get("/api/terminal/tick")'):]
    block = block[:block.find("@app.get", 10)]
    for key in ('"price"', '"source"', '"replay"', '"symbol_epoch"'):
        assert key in block, key


def test_the_live_tick_extends_the_forming_candle_only():
    """Extiende la vela abierta: cierre al precio, máximo y mínimo si los rompe. No
    crea velas nuevas ni toca el volumen, que llega del motor con su cadencia."""
    src = text("app/static/itmq_app.js")
    block = src[src.find("async function pullTick"):]
    block = block[:block.find("async function pullDiagnostics")]
    assert "last.c = p;" in block
    assert "Math.max(Q.num(last.h, p), p)" in block
    assert "Math.min(Q.num(last.l, p), p)" in block
    assert "candles.push" not in block and ".v =" not in block


def test_the_tick_refuses_a_stale_symbol_and_a_replay_clock():
    src = text("app/static/itmq_app.js")
    block = src[src.find("async function pullTick"):]
    block = block[:block.find("async function pullDiagnostics")]
    assert "t.replay" in block
    assert "String(t.symbol || '') !== String(state.trace.symbol || '')" in block
    assert "replay.active" in src[src.find("if (state.busyTick"):src.find("if (state.busyTick") + 120]


def test_price_polls_much_faster_than_structure():
    src = text("app/static/itmq_app.js")
    assert "pullTick(); }, 350)" in src
    assert "pullTrace(); }, 2500)" in src


# ────────────────────────── reproducción de sesión (backtesting)

def test_a_session_clock_endpoint_exposes_the_archived_marks():
    """El backtesting no simula: recorre los instantes que el motor observó."""
    src = text("app/main.py")
    assert '@app.get("/api/replay/clock")' in src
    block = src[src.find('@app.get("/api/replay/clock")'):]
    block = block[:block.find("@app.post", 10)]
    assert "replay_session_clock" in block
    assert "step_minutes" in block


def test_the_clock_step_is_bounded():
    src = text("app/main.py")
    block = src[src.find('@app.get("/api/replay/clock")'):]
    block = block[:block.find("@app.post", 10)]
    assert "max(0.25, min(float(step_minutes or 1.0), 60.0))" in block


def test_the_replay_bar_has_a_calendar_and_transport_controls():
    html = text("app/templates/terminal.html")
    assert 'id="replayBar"' in html and 'id="replayBtn"' in html
    for el_id in ("rpDate", "rpStep", "rpSpeed", "rpPlay", "rpPrev", "rpNext",
                  "rpScrub", "rpClock", "rpProgress", "rpLive"):
        assert f'id="{el_id}"' in html, el_id


def test_seeking_replays_every_section_not_only_trace():
    """Todas las secciones leen del mismo contexto histórico global del motor, así
    que pedir bundle y trace reproduce la terminal entera."""
    src = text("app/static/itmq_app.js")
    block = src[src.find("async function replaySeek"):]
    block = block[:block.find("function replayStep")]
    assert "/api/replay/set?date=" in block and "asof=" in block
    assert "Promise.all([pullBundle(), pullTrace()])" in block


def test_the_live_cycle_never_overwrites_the_instant_being_replayed():
    src = text("app/static/itmq_app.js")
    for guard in ("pullTrace(); }, 2500)", "pullBundle(); }, 6000)", "pullTick(); }, 350)"):
        i = src.find(guard)
        assert i > 0, guard
        assert "!replay.active" in src[max(0, i - 160):i], guard


def test_playback_waits_for_each_step_instead_of_stacking_requests():
    """Encadenar tras completar el paso evita que una reproducción rápida acumule
    peticiones encima de las anteriores y desordene la sesión."""
    src = text("app/static/itmq_app.js")
    block = src[src.find("function replayPlay"):src.find("function replayStop")]
    assert "await replaySeek(replay.index + 1);" in block
    assert "if (replay.playing) replay.timer = setTimeout(tick, replay.speedMs);" in block


def test_replay_is_visually_impossible_to_confuse_with_live():
    """Confundir un backtest con el mercado real es el error más caro que puede
    cometer esta pantalla."""
    css = text("app/static/itmq_terminal.css")
    assert ".replaybar" in css and "--warn" in css[css.find(".replaybar"):css.find(".replaybar") + 700]
    assert "body.replaying main" in css
    src = text("app/static/itmq_app.js")
    assert "document.body.classList.toggle('replaying'" in src


def test_returning_to_live_clears_the_replay_state():
    src = text("app/static/itmq_app.js")
    block = src[src.find("async function replayExitToLive"):src.find("function bindReplay")]
    assert "/api/replay/live" in block
    assert "replay.active = false" in block and "replay.marks = []" in block


# ────────────────────────── Interval Map nativo

def _hh(**over):
    base = {"ready": True, "strikes": [530.0, 531.0, 532.0],
            "times": ["09:30", "09:35"],
            "gamma_m": [[1.0, 2.0], [-0.5, 0.25], [0.1, 0.0]],
            "delta_m": [[3.0, 4.0], [1.0, 2.0], [0.5, 0.5]],
            "charm_m": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]}
    base.update(over)
    return base


def test_the_engine_is_the_first_source_of_the_interval_map():
    """El motor ya calculaba esta matriz para el heatmap de TRACE. El panel quedaba
    vacío en cuanto la herramienta del proveedor fallaba, con el dato ya dentro."""
    from app.terminal_api import _interval_map
    im = _interval_map({}, {"heatmap_history": _hh()})
    assert im["ready"] and im["source"] == "ITM_QUANT"
    assert im["strikes"] == [530.0, 531.0, 532.0] and im["times"] == ["09:30", "09:35"]
    assert im["matrix"][0] == [1e6, 2e6]


@pytest.mark.parametrize("greek,label,first", [
    ("GAMMA", "GEX", 1e6), ("DELTA", "DEX", 3e6), ("CHARM", "CHEX", 0.1e6)])
def test_every_greek_has_its_own_matrix(greek, label, first):
    from app.terminal_api import _interval_map
    im = _interval_map({}, {"heatmap_history": _hh()}, greek)
    assert im["label"] == label
    assert im["matrix"][0][0] == pytest.approx(first)


def test_an_unknown_greek_falls_back_to_gamma_instead_of_failing():
    from app.terminal_api import _interval_map
    assert _interval_map({}, {"heatmap_history": _hh()}, "NOEXISTE")["label"] == "GEX"


def test_a_ragged_matrix_is_padded_never_truncated_into_misalignment():
    """Una fila corta desplazaría todos los puntos de ese strike a intervalos que no
    les corresponden: el mapa mentiría sin que nada fallara."""
    from app.terminal_api import _interval_map
    im = _interval_map({}, {"heatmap_history": _hh(gamma_m=[[1.0], [], [2.0, 3.0]])})
    assert all(len(r) == len(im["times"]) for r in im["matrix"])
    assert im["matrix"][1] == [0.0, 0.0]


def test_the_price_travels_with_the_map():
    """Un mapa de exposición sin el precio encima dice dónde estaba la exposición,
    no por qué zonas pasó el mercado."""
    from app.terminal_api import _interval_map
    im = _interval_map({}, {"heatmap_history": _hh(),
                            "candles": [{"t": "09:30", "c": 530.4}, {"t": "09:35", "c": 531.2}]})
    assert [p["v"] for p in im["price"]] == [530.4, 531.2]


def test_the_provider_covers_the_engine_with_parallel_axes():
    from app.terminal_api import _interval_map
    im = _interval_map({"options_heat_map": {"ready": True, "raw": {"result": {
        "strikes": [415.0, 425.0], "times": ["09:30", "09:35"],
        "matrix": [[1.0, 2.0], [3.0, 4.0]]}}}}, {})
    assert im["ready"] and im["source"] == "QUANTDATA"
    assert im["matrix"] == [[1.0, 2.0], [3.0, 4.0]]


def test_the_provider_cell_list_is_still_understood():
    from app.terminal_api import _interval_map
    im = _interval_map({"options_heat_map": {"ready": True, "raw": {"data": [
        {"strike": 418.0, "time": "09:30", "gamma": 1.0},
        {"strike": 420.0, "time": "09:35", "gamma": -1.5}]}}}, {})
    assert im["strikes"] == [418.0, 420.0]
    assert im["matrix"][1] == [0.0, -1.5]


def test_an_unreadable_payload_publishes_its_shape():
    """«No reconocido» a secas no se puede corregir sin adivinar qué devolvió."""
    from app.terminal_api import _interval_map
    im = _interval_map({"options_heat_map": {"ready": True, "raw": {"result": {"msg": "x", "n": 3}}}}, {})
    assert im["ready"] is False
    assert im["payload_keys"] == {"result": {"msg": "str", "n": "int"}}


def test_the_greek_selector_reaches_the_backend():
    assert 'id="imGreek"' in text("app/templates/terminal.html")
    app = text("app/static/itmq_app.js")
    assert "interval_greek=${state.intervalGreek}" in app
    assert 'interval_greek: str = "GAMMA"' in text("app/main.py")


# ────────────────────────── liquidez visible en TRACE

def test_liquidity_and_live_exposure_drift_are_selectable_metrics():
    """La liquidez de una zona no cambia CUÁNTA gamma hay en un strike: cambia
    cuánto pesa. Por eso es métrica propia y entra en GRAVEDAD, no dentro del GEX.
    Interpolar OI entre snapshots sería inventar contratos."""
    src = text("app/static/itmq_trace.js")
    assert "LIQUIDITY: {" in src and "r.liquidity_score" in src
    assert "GEX_LIVE: {" in src and "r.gamma_change_m" in src
    assert "DEX_LIVE: {" in src and "r.delta_change_m" in src
    html = text("app/templates/terminal.html")
    # Disponibles en los dos perfiles, izquierdo y derecho.
    assert html.count('<option value="LIQUIDITY">') == 2
    assert html.count('<option value="GEX_LIVE">') == 2


def test_the_engine_reprices_exposure_against_live_spot():
    """gamma_change = gamma en vivo − gamma del snapshot. Es la prueba de que GEX y
    DEX se mueven con el precio y no sólo al refrescar la cadena."""
    src = text("app/core/trace_live.py")
    assert 'agg["gamma_change"] = agg["gamma"] - agg["gamma_base"]' in src
    assert 'agg["delta_change"] = agg["delta"] - agg["delta_base"]' in src


def test_liquidity_feeds_relevance_not_the_exposure_value():
    src = text("app/core/trace_live.py")
    gravity = src[src.find("_gravity = 100.0 * np.clip"):src.find('agg["gravity_score"] = _gravity')]
    assert "_liq_strength" in gravity and "_opra_strength" in gravity
    # Y no aparece en el cálculo de la exposición en sí.
    gamma_calc = src[src.find("gamma_live = sign"):src.find("gamma_live = sign") + 200]
    assert "liquidity" not in gamma_calc.lower()

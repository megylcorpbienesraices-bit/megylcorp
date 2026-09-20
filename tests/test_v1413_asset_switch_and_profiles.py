"""v1.41.3 · Cambio de activo, revalorización LIVE y perfiles call/put."""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ────────────────────────── cambio de activo

def test_asset_select_is_called_with_a_query_parameter():
    """El endpoint declara `symbol: str` sin Body(), así que FastAPI lo toma como
    parámetro de consulta. Enviarlo en el cuerpo JSON devuelve 422 y el cambio
    fallaba en silencio: la cabecera se quedaba en CAMBIANDO ACTIVO."""
    src = text("app/static/itmq_app.js")
    block = src[src.find("async function selectSymbol"):src.find("/* -------------------------------------------------------------- boot */")]
    assert "/api/asset/select?symbol=" in block
    assert "JSON.stringify({ symbol })" not in block


def test_asset_select_endpoint_still_declares_a_query_parameter():
    """Si el backend pasara a aceptar cuerpo, esta prueba avisa antes de que la
    interfaz vuelva a fallar en silencio."""
    src = text("app/main.py")
    sig = src[src.find('@app.post("/api/asset/select")'):]
    sig = sig[:sig.find("\n")+200]
    assert "async def api_asset_select(symbol: str)" in sig


def test_failed_switch_is_surfaced_instead_of_looping_silently():
    src = text("app/static/itmq_app.js")
    block = src[src.find("async function selectSymbol"):src.find("/* -------------------------------------------------------------- boot */")]
    assert "if (!r.ok || (payload && payload.ok === false))" in block
    assert "kept_symbol" in block          # el motivo del rechazo llega al usuario
    assert "SIGUE EN" in block


def test_switch_waits_for_the_engine_to_confirm_the_new_symbol():
    """La conmutación es progresiva: dar por hecho el cambio dejaba la cabecera
    colgada mientras el motor seguía hidratando el activo anterior."""
    src = text("app/static/itmq_app.js")
    block = src[src.find("async function selectSymbol"):src.find("/* -------------------------------------------------------------- boot */")]
    assert "const deadline = Date.now()" in block
    assert "active === target" in block


def test_header_is_not_overwritten_while_switching():
    src = text("app/static/itmq_app.js")
    assert "if (state.switching) {" in src
    assert "CAMBIANDO A ${state.switching}" in src


# ────────────────────────── revalorización LIVE

def test_trace_cache_key_lets_profiles_reprice_against_live_spot():
    """La clave sólo llevaba analytics_revision, que avanza cada 5-20 s: entre
    ciclos se devolvía el payload de antes y GEX/DEX parecían congelados aunque
    el precio se moviera."""
    src = text("app/main.py")
    assert "TRACE_LIVE_REPRICE_SECONDS" in src
    assert src.count("_live_bucket = 0 if STATE.replay_context.is_replay") == 2
    for key in re.findall(r'key\s*=\s*f"trace\|[^"]+"', src):
        assert "{_live_bucket}" in key, key


def test_replay_clock_is_not_disturbed_by_live_repricing():
    """En replay el reloj lo manda el usuario: el bucket queda fijo en 0."""
    src = text("app/main.py")
    assert "0 if STATE.replay_context.is_replay else int(time.time() / TRACE_LIVE_REPRICE_SECONDS)" in src


def test_reprice_interval_is_configurable_and_bounded():
    src = text("app/main.py")
    assert 'os.getenv("ITM_TRACE_LIVE_REPRICE_SECONDS", "2")' in src
    assert "max(1.0, float(" in src           # nunca por debajo de 1 s
    assert "ITM_TRACE_LIVE_REPRICE_SECONDS" in text(".env.example")


# ────────────────────────── niveles compartidos TRACE ↔ FLUJO

def test_level_styles_have_a_single_definition():
    """Un Call Wall debe verse igual en TRACE y en el flujo: dos definiciones
    acabarían siendo dos verdades sobre el mismo precio."""
    core = text("app/static/itmq_core.js")
    assert "const LEVELS = {" in core
    assert "FLOW_LEVEL_KINDS" in core and "levelStyle" in core
    trace = text("app/static/itmq_trace.js")
    assert "const LEVEL_STYLE = Q.LEVELS;" in trace
    assert "flip: { color:" not in trace       # ya no hay copia local


def test_flow_panel_draws_the_same_structural_levels_as_trace():
    src = text("app/static/itmq_orderflow.js")
    assert "Q.FLOW_LEVEL_KINDS.indexOf(l.kind) >= 0" in src
    assert "Q.levelStyle(lv.kind)" in src
    # Las etiquetas se apilan: dos niveles cercanos no pueden taparse.
    # v1.51.0 · El hueco entre etiquetas deja de ser un 15 escrito aqui y pasa a
    # `Q.LEVEL_LABEL_GAP`, en el nucleo, junto a la fuente y el alto de la
    # pastilla. TRACE, la cinta y Net Drift dibujan los MISMOS niveles: con la
    # medida repetida en tres sitios volvian a divergir al primer ajuste.
    assert "Q.stackLabels(drawn, Q.LEVEL_LABEL_GAP)" in src
    assert "Q.LEVEL_FONT" in src and "Q.LEVEL_LABEL_H" in src


def test_flow_levels_include_the_walls_and_the_gamma_flip():
    src = text("app/static/itmq_core.js")
    block = src[src.find("const FLOW_LEVEL_KINDS"):src.find("function levelStyle")]
    for kind in ("flip", "call_wall", "put_wall", "vol_trigger", "gamma"):
        assert f"'{kind}'" in block, kind


# ────────────────────────── desglose call/put en los perfiles

def test_profile_metrics_declare_their_call_put_components():
    src = text("app/static/itmq_trace.js")
    metrics = src[src.find("const METRICS = {"):src.find("const HEATFIELDS")]
    for name in ("GEX", "DEX", "OI", "VOLUME"):
        block = metrics[metrics.find(f"{name}: {{"):]
        block = block[:block.find("\n    },") + 6] if "\n    }," in block else block[:400]
        assert "parts:" in block, name


def test_gex_breakdown_uses_the_chain_call_put_split():
    src = text("app/static/itmq_trace.js")
    assert "call_gamma_m" in src and "put_gamma_m" in src
    assert "call_delta_m" in src and "put_delta_m" in src


def test_breakdown_scale_covers_the_components_not_only_the_net():
    """Una call y una put que se compensan dan un neto pequeño; si la escala
    sólo mirase el neto, las dos barras se saldrían del panel."""
    src = text("app/static/itmq_trace.js")
    block = src[src.find("let peak = 0;"):src.find("cfg.max.set(peak > 0 ? peak : 1);")]
    assert "parts(r)" in block
    assert "Math.abs(Q.num(p.call))" in block and "Math.abs(Q.num(p.put))" in block


def test_breakdown_lanes_are_animated_every_frame():
    src = text("app/static/itmq_trace.js")
    assert "const calling = cfg.call.step(env.dt);" in src
    assert "const putting = cfg.put.step(env.dt);" in src
    assert "return gliding || calling || putting || scaling || priceAxisSettling();" in src


def test_call_and_put_have_their_own_colors():
    """No pueden reutilizar pos/neg: ahí el color indica el signo de la
    exposición, no si el contrato es call o put."""
    css = text("app/static/itmq_terminal.css")
    assert "--call:" in css and "--put:" in css
    src = text("app/static/itmq_trace.js")
    assert "Q.token('--call'" in src and "Q.token('--put'" in src


def test_breakdown_control_is_exposed_and_wired():
    assert 'id="traceBreakdown"' in text("app/templates/terminal.html")
    app = text("app/static/itmq_app.js")
    assert "segment('traceBreakdown'" in app
    assert "Trace.setBreakdown(parts)" in app


def test_metrics_without_a_split_keep_working():
    """FLUJO 5M o GRAVEDAD no publican desglose: deben seguir dibujando el neto
    en vez de quedarse en blanco al activar el modo."""
    src = text("app/static/itmq_trace.js")
    assert "function hasBreakdown(side)" in src
    assert "const showParts = S.breakdown && hasBreakdown(side);" in src
    apply_fn = src[src.find("function applyMetric"):src.find("function hasBreakdown")]
    assert "cfg.call.setAll([]);" in apply_fn      # se limpian los carriles

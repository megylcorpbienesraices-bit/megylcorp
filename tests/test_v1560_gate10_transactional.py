"""GATE 10 · Transaccionalidad, Londres, multiactivo y evidencia LIVE.

Puntos 31 a 39 y 55.

DOS HUELLAS, DOS PREGUNTAS
--------------------------
    generation_id   ¿DE QUÉ activo es esta respuesta?
    cycle_id        ¿es la MISMA respuesta que la anterior?

Hacen falta las dos. Una sección congelada por un fallo de refresco tiene la
misma generación que la anterior y un contenido idéntico: sin la segunda huella
es indistinguible de un mercado sin actividad nueva.

EL COMMIT TRANSACCIONAL
-----------------------
Descartar la generación ajena no basta. Durante la hidratación, el bundle
empieza a llegar con el símbolo NUEVO pero con secciones a medio llenar:

    Net Drift de QQQ  +  GEX todavía de DIA  +  muros antiguos

No es un estado intermedio inocente: son tres lecturas de tres momentos
distintos presentadas como una sola foto del mercado.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.core import session_mode as SM
from app.terminal_api import build_terminal_bundle

APP = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
LIVE = Path("scripts/verify_live_quantdata.py").read_text(encoding="utf-8")
OCHO = ("DIA", "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "AMD")


def _bundle(sym="QQQ", epoch=1, velas=2):
    return build_terminal_bundle(
        state={"active_symbol": sym, "symbol_epoch": epoch, "ready": True},
        trace={"candles": [{"t": i, "c": 1.0, "v": 1} for i in range(velas)], "levels": []})


# ── 31 · Las dos huellas ─────────────────────────────────────────────────

def test_el_bundle_lleva_generacion_ciclo_y_sesion():
    b = _bundle()
    assert b["generation_id"] == "QQQ#1"
    assert b["cycle_id"] and len(b["cycle_id"]) == 12
    assert b["session_date"]


def test_el_mismo_contenido_produce_el_mismo_ciclo():
    assert _bundle()["cycle_id"] == _bundle()["cycle_id"]


def test_contenido_nuevo_produce_ciclo_nuevo():
    assert _bundle(velas=2)["cycle_id"] != _bundle(velas=3)["cycle_id"]


def test_cambiar_de_activo_cambia_las_dos_huellas():
    a, b = _bundle("QQQ", 1), _bundle("SPY", 2)
    assert a["generation_id"] != b["generation_id"]
    assert a["cycle_id"] != b["cycle_id"]


def test_la_generacion_responde_de_que_activo_y_el_ciclo_si_cambio():
    """Son preguntas distintas: el mismo activo puede traer contenido nuevo."""
    a, b = _bundle("QQQ", 1, velas=2), _bundle("QQQ", 1, velas=5)
    assert a["generation_id"] == b["generation_id"]
    assert a["cycle_id"] != b["cycle_id"]


# ── 31 · Commit transaccional ────────────────────────────────────────────

def test_el_cliente_descarta_la_generacion_ajena_entera():
    cuerpo = APP[APP.index("async function pullBundle()"):APP.index("renderAll(d);")]
    assert "llega !== esperado" in cuerpo
    assert cuerpo.index("llega !== esperado") < cuerpo.index("state.bundle = d;")


def test_no_se_publica_un_snapshot_a_medio_llenar():
    """Media pantalla de cada activo son tres lecturas de tres momentos
    distintos presentadas como una sola foto del mercado."""
    cuerpo = APP[APP.index("async function pullBundle()"):APP.index("renderAll(d);")]
    assert "COMMIT TRANSACCIONAL DEL SNAPSHOT" in cuerpo
    for critico in ("walls", "exposicion", "interval_map"):
        assert f"'{critico}'" in cuerpo, critico
    assert "snapshot incompleto, sin commit" in cuerpo


def test_mientras_hidrata_la_cabecera_lo_dice():
    """Lo que se ve es de antes, y se dice."""
    cuerpo = APP[APP.index("async function pullBundle()"):APP.index("renderAll(d);")]
    assert "engineState('WAIT', `HIDRATANDO ${esperado}`)" in cuerpo


def test_el_cliente_guarda_las_dos_huellas():
    assert "state.generation = gen || state.generation;" in APP
    assert "state.cycle = String(d.cycle_id || '')" in APP


# ── 32 · Cambio rápido ───────────────────────────────────────────────────

def test_una_secuencia_rapida_no_mezcla_activos():
    """DIA → QQQ → SPY → NVDA → AAPL → DIA, varias veces."""
    secuencia = ["DIA", "QQQ", "SPY", "NVDA", "AAPL", "DIA"] * 3
    vistos = []
    for i, sym in enumerate(secuencia, 1):
        b = _bundle(sym, i)
        assert b["generation_id"] == f"{sym}#{i}"
        assert b["symbol"] == sym
        vistos.append(b["generation_id"])
    # Ninguna generación se repite: cada cambio incrementa la época.
    assert len(set(vistos)) == len(vistos)


def test_volver_al_activo_anterior_no_reutiliza_su_generacion():
    """Si DIA volviera con su generación vieja, una respuesta atrasada del
    primer DIA se aceptaría como válida para el segundo."""
    primero = _bundle("DIA", 1)
    ultimo = _bundle("DIA", 6)
    assert primero["generation_id"] != ultimo["generation_id"]


# ── 33-35 · Londres ──────────────────────────────────────────────────────

def _ec(h, m=0, d=16):
    return datetime(2026, 3, d, h, m, tzinfo=SM.EC).astimezone(timezone.utc)


def test_a_las_cuatro_de_la_manana_de_ecuador_empieza_londres():
    assert SM.resolve(_ec(3, 59))["mode"] == SM.PRE_LONDON
    assert SM.resolve(_ec(4, 0))["mode"] == SM.LONDON_MONITOR


def test_la_hora_sale_de_la_zona_no_de_un_utc_escrito_a_mano():
    """Ecuador no aplica horario de verano y Nueva York sí: un UTC fijo
    acertaría medio año y fallaría el otro medio sin que nada avisara."""
    invierno = SM.ny_open_in_ec(datetime(2026, 1, 15).date())
    verano = SM.ny_open_in_ec(datetime(2026, 7, 15).date())
    assert invierno.hour != verano.hour
    assert str(SM.EC) == "America/Guayaquil"


def test_londres_y_nueva_york_son_acumulados_separados():
    SM.reset()
    SM.accumulate("QQQ", price=500.0, volume=100.0, now=_ec(6))
    SM.accumulate("QQQ", price=502.0, volume=50.0, now=_ec(6, 30))
    SM.accumulate("QQQ", price=505.0, volume=200.0, now=_ec(11))
    ambas = SM.both("QQQ", now=_ec(11))
    assert ambas["london"]["volume"] == 150.0
    assert ambas["new_york"]["volume"] == 200.0
    assert ambas["london"]["open"] == 500.0


def test_al_abrir_nueva_york_londres_se_sella_y_no_se_borra():
    SM.reset()
    SM.accumulate("QQQ", price=500.0, volume=100.0, now=_ec(6))
    sellado = SM.close_session("QQQ", SM.LONDON_MONITOR, now=_ec(11))
    assert sellado["closed"] is True
    assert sellado["volume"] == 100.0
    assert SM.snapshot("QQQ", SM.LONDON_MONITOR, now=_ec(11))["volume"] == 100.0


def test_sin_dato_real_no_se_inventa_actividad():
    """«Esperando flujo nuevo» no es lo mismo que «volumen cero»."""
    SM.reset()
    SM.accumulate("QQQ", price=500.0, now=_ec(5))      # sólo precio
    s = SM.snapshot("QQQ", SM.LONDON_MONITOR, now=_ec(5))
    assert s["volume"] is None and s["prints"] is None
    assert s["last"] == 500.0


def test_el_monitor_no_toca_los_datos_del_proveedor():
    """Es monitorización: DIRECT_PROVIDER permanece intacto."""
    src = Path("app/core/session_mode.py").read_text(encoding="utf-8")
    assert "No borra nada al cambiar de sesión" in src
    for prohibido in ("norm_", "quantdata", "alpaca"):
        assert prohibido not in src.lower()


# ── 36 · Multiactivo ─────────────────────────────────────────────────────

def test_los_ocho_activos_producen_la_misma_forma_de_bundle():
    formas = {tuple(sorted(_bundle(s, i).keys())) for i, s in enumerate(OCHO, 1)}
    assert len(formas) == 1


def test_ningun_activo_toma_una_rama_propia():
    from tests.test_v1550_generation_lkg_multiasset import (_codigo_js,
                                                            _codigo_python)
    import re
    tickers = "QQQ|SPY|SPX|IWM|DIA|AAPL|NVDA|TSLA|AMD|VIX"
    js = re.compile(rf"""(symbol|ticker|sym)\s*(===|==|!==|!=)\s*['"]({tickers})""")
    for ruta in Path("app").rglob("*.js"):
        if "test" in ruta.name:
            continue
        assert not js.search(_codigo_js(ruta)), ruta


# ── 37-39 · El guion de evidencia LIVE ───────────────────────────────────

def test_el_guion_live_tiene_los_tres_modos_forenses():
    for modo in ("--aggressor", "--interval-map", "--cierre", "--dark-pool"):
        assert modo.lstrip("-").replace("-", "_") in LIVE or modo in LIVE, modo


def test_la_cesta_de_cierre_son_los_ocho_activos():
    from importlib import util
    spec = util.spec_from_file_location("vlq", "scripts/verify_live_quantdata.py")
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.CIERRE_BASKET) == set(OCHO)


def test_el_modo_agresor_contrasta_las_cuatro_etapas():
    cuerpo = LIVE[LIVE.index("def probe_aggressor"):LIVE.index("def render_aggressor")]
    assert "build_evidence" in cuerpo
    assert "flow_marks" in cuerpo and "trace_marks" in cuerpo
    cabecera = LIVE[LIVE.index("def render_aggressor"):LIVE.index("def probe_interval_map")]
    for col in ("tradeSideCode", "esperado", "clasif.", "FLUJO", "TRACE"):
        assert col in cabecera, col


def test_el_modo_interval_map_compara_el_signo_celda_a_celda():
    cuerpo = LIVE[LIVE.index("def probe_interval_map"):LIVE.index("def render_interval_map")]
    assert "audit_grid" in cuerpo
    render = LIVE[LIVE.index("def render_interval_map"):LIVE.index("def main()")]
    assert "El signo NO puede cambiar" in render
    assert "sign_flip_count" in render


def test_el_guion_falla_cuando_una_etapa_cambia_el_lado():
    render = LIVE[LIVE.index("def render_aggressor"):LIVE.index("def probe_interval_map")]
    assert "CAMBIAN DE LADO" in render
    assert "todo_ok = False" in render

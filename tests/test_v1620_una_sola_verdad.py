"""v1.62.0 · TRES PANTALLAS, TRES VEREDICTOS, UNA SOLA EJECUCIÓN.

LO QUE ENSEÑABAN LAS CAPTURAS LIVE
----------------------------------
Sobre el MISMO ciclo y el MISMO carril:

    Herramientas Quant Data    equity_prints   PROVIDER_ERROR (tardó >11 s)
    Diagnóstico de paneles     equity_prints   SIN DATOS · EL_PROVEEDOR_NO_DEVOLVIÓ_FILAS

    Dark Pool · por carril     dark_pool_levels   STALE · 382
    Pantalla del analista      dark_pool_levels   CON DATOS · 382

Las dos son imposibles para una misma ejecución.

LA CAUSA, QUE ERA LA MISMA
--------------------------
Cada pantalla volvía a DEDUCIR el estado desde el bloque publicado, con una
regla distinta:

    lane_state()       miraba `lane_status` y la clasificación del hub
    _dark_pool_rows()  miraba `bool(rows)` y, si no había, INVENTABA el motivo
    la tabla           miraba `ready` y contaba filas

Un refresco fallido CONSERVA el bloque anterior —y debe conservarlo, para no
tirar el último dato bueno—, así que `ready` seguía en `True` y `rows` seguía
trayendo 382. Quien mirara `bool(rows)` concluía CON DATOS sobre una llamada
muerta por plazo. Ninguno mentía sobre lo que miraba: los tres miraban la
sombra del hecho en vez del hecho.

Y cuando no quedaba bloque, el diagnóstico rellenaba con
`EL_PROVEEDOR_NO_DEVOLVIÓ_FILAS`, una causa INVENTADA: el proveedor no devolvió
nada porque la petición murió por plazo, no porque no tuviera filas.

LO QUE ATA ESTE FICHERO
-----------------------
    · un objeto canónico por carril, escrito en la EJECUCIÓN
    · las cuatro reglas de estado, sin una quinta
    · las cuatro vistas dicen lo MISMO del mismo carril
    · quien sirve LKG no puede llamarlo dato fresco en ninguna parte
    · esperar no es fallar, pero esperar sin fin es una anomalía
    · cada fallo dice QUÉ FASE expiró y dónde se fueron los milisegundos
    · el snapshot de muros publica SIEMPRE uno de cuatro estados y su causa
"""
from __future__ import annotations

import time

import pytest

from app.core import dark_pool_state as DPS
from app.core import lane_truth as LT
from app.core import quant_data_hub as HUB
from app.core import wall_engine as WE
from app.core import wall_snapshot as WS
import app.terminal_api as TA


@pytest.fixture(autouse=True)
def _verdad_limpia():
    LT.TRUTH.reset()
    WS.SNAPSHOTS.reset()
    yield
    LT.TRUTH.reset()
    WS.SNAPSHOTS.reset()


def _fila(panel, ok, count, source, reason=None):
    return {"panel": panel, "ok": bool(ok), "count": count,
            "state": ("CON DATOS" if ok else "SIN DATOS"),
            "source": source, "reason": None if ok else reason}


def _las_cuatro_vistas(tool: str, panel: str, intel: dict):
    """El mismo carril, leído por las cuatro superficies que lo enseñan."""
    autoridad = LT.TRUTH.read(tool)
    carril = HUB.dark_pool("SPY", intel)["lanes"][tool]
    diagnostico = {r["panel"]: r for r in TA._dark_pool_rows(_fila, intel)}[panel]
    # La pantalla del analista imprime la etiqueta, y sale de la MISMA función.
    pantalla = LT.screen_label(autoridad)
    return autoridad, carril, diagnostico, pantalla


def _vista_analista(tool: str, intel: dict) -> dict:
    """Lo que publica la sección del analista para ese carril."""
    from app.core import dark_pool_view as DPV
    bloque = (intel or {}).get(tool) or {}
    return DPV._lane_status(bloque, list(bloque.get("rows") or []), tool)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · LAS CUATRO REGLAS, Y NO HAY UNA QUINTA
# ═══════════════════════════════════════════════════════════════════════════

def test_respuesta_valida_con_filas_es_live():
    t = LT.executed("dark_flow", rows=216)
    assert t.current_status == LT.LIVE
    assert t.serving == LT.SERVING_LIVE and t.served_rows == 216
    assert t.fresh is True


def test_respuesta_valida_sin_filas_es_no_data_y_no_es_averia():
    t = LT.executed("dark_flow", rows=0)
    assert t.current_status == LT.NO_DATA
    assert t.is_failure is False
    assert t.serving == LT.SERVING_NONE


def test_fallo_con_lkg_sirve_lkg_y_lo_declara():
    t = LT.executed("dark_pool_levels", rows=0, status=LT.PROVIDER_ERROR,
                    error="timeout", lkg_rows=382, lkg_age=47.0)
    assert t.current_status == LT.PROVIDER_ERROR
    assert t.serving == LT.SERVING_LKG and t.served_rows == 382
    assert t.fresh is False, "382 filas viejas NO son dato de este ciclo"
    assert "DATO ANTERIOR" in t.screen and "47s" in t.screen


def test_fallo_sin_lkg_no_tiene_nada_que_servir():
    t = LT.executed("equity_prints", rows=0, status=LT.PROVIDER_ERROR, error="timeout")
    assert t.serving == LT.SERVING_NONE and t.served_rows == 0


def test_nadie_puede_declarar_live_sin_filas():
    """LIVE significa que llegó dato. Sin filas, es NO_DATA."""
    assert LT.executed("x", rows=0, status=LT.LIVE).current_status == LT.NO_DATA


def test_lo_que_no_se_ha_ejecutado_no_es_no_data():
    """Decir «el proveedor no devolvió filas» de una llamada que nunca salió le
    atribuye al proveedor un silencio que es NUESTRO."""
    t = LT.TRUTH.read("jamas_ejecutada")
    assert t["current_status"] in LT.WAITING_STATES
    assert t["current_status"] != LT.NO_DATA
    assert t["is_failure"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LA CONTRADICCIÓN DE EQUITY PRINTS
# ═══════════════════════════════════════════════════════════════════════════

def test_equity_prints_tiene_UNA_sola_causa():
    """EL DEFECTO EXACTO DE LA CAPTURA.

    Arriba PROVIDER_ERROR por tardar >11 s; abajo «SIN DATOS ·
    EL_PROVEEDOR_NO_DEVOLVIÓ_FILAS». Imposible para la misma ejecución.
    """
    LT.TRUTH.put(LT.executed(
        "equity_prints", symbol="SPY", rows=0, status=LT.PROVIDER_ERROR,
        error="el canal tardó más de 11.4 s", phase="READ_TIMEOUT",
        request_id="equity_prints#7", cycle_id="41"))
    intel = {"equity_prints": {"ready": False, "rows": [], "count": 0}}
    autoridad, carril, diag, pantalla = _las_cuatro_vistas(
        "equity_prints", "DARK POOL · prints de equity", intel)

    assert LT.same_semantics(autoridad, carril)
    assert LT.same_semantics(autoridad, diag)
    assert diag["current_status"] == LT.PROVIDER_ERROR
    assert carril["state"] == DPS.PROVIDER_ERROR
    # Y la causa es la REAL, no la inventada.
    assert "11.4 s" in (diag["reason"] or "")
    assert "READ_TIMEOUT" in (diag["reason"] or "")
    assert "NO_DEVOLVIO_FILAS" not in (diag["reason"] or "").upper()


def test_las_dos_vistas_declaran_la_misma_ejecucion():
    """Con `request_id`, dos pantallas que discrepan dejan de ser una discusión."""
    LT.TRUTH.put(LT.executed("equity_prints", symbol="SPY", rows=0,
                             status=LT.PROVIDER_ERROR, error="timeout",
                             request_id="equity_prints#9", cycle_id="41"))
    intel = {"equity_prints": {"ready": False, "rows": [], "count": 0}}
    _, carril, diag, _ = _las_cuatro_vistas(
        "equity_prints", "DARK POOL · prints de equity", intel)
    assert carril["request_id"] == diag["request_id"] == "equity_prints#9"
    assert carril["cycle_id"] == diag["cycle_id"] == "41"


# ═══════════════════════════════════════════════════════════════════════════
# 3 · LA CONTRADICCIÓN DE DARK POOL LEVELS
# ═══════════════════════════════════════════════════════════════════════════

def test_dark_pool_levels_dice_en_todas_partes_que_sus_382_filas_son_LKG():
    """Arriba STALE · 382, abajo CON DATOS · 382. Las 382 son las mismas."""
    LT.TRUTH.put(LT.executed(
        "dark_pool_levels", symbol="SPY", rows=0, status=LT.PROVIDER_ERROR,
        error="timeout", phase="READ_TIMEOUT", lkg_rows=382, lkg_age=47.0,
        request_id="dark_pool_levels#7", cycle_id="41"))
    intel = {"dark_pool_levels": {"ready": True, "rows": [{}] * 382, "count": 382}}
    autoridad, carril, diag, pantalla = _las_cuatro_vistas(
        "dark_pool_levels", "DARK POOL · niveles", intel)

    assert LT.same_semantics(autoridad, carril)
    assert LT.same_semantics(autoridad, diag)
    for vista in (autoridad, carril, diag):
        assert vista["serving"] == LT.SERVING_LKG
        assert int(vista["served_rows"]) == 382
        assert vista["fresh"] is False, "ninguna vista puede llamarlo dato fresco"
    # Y la antigüedad es VISIBLE, no un detalle enterrado.
    assert "47" in str(carril["lkg_age"]) or carril["lkg_age"] == 47.0
    assert "DATO ANTERIOR" in pantalla and "47" in pantalla
    assert "CON DATOS" not in str(diag["state"]).upper()
    # Y la CUARTA vista —la del analista—, que era de donde salía el «CON
    # DATOS · 382»: ahora declara que las 382 son del ciclo anterior.
    analista = _vista_analista("dark_pool_levels", intel)
    assert analista["state"] == "STALE_LKG"
    assert analista["rows"] == 382 and analista["fresh"] is False
    assert "último ciclo bueno" in analista["detail"]


def test_ninguna_vista_puede_llamar_fresco_a_un_lkg():
    """La regla, aplicada a los tres carriles a la vez."""
    for tool in DPS.DARK_POOL_LANES:
        LT.TRUTH.put(LT.executed(tool, symbol="SPY", rows=0,
                                 status=LT.PROVIDER_ERROR, error="timeout",
                                 lkg_rows=100, lkg_age=60.0))
    intel = {t: {"ready": True, "rows": [{}] * 100, "count": 100}
             for t in DPS.DARK_POOL_LANES}
    lanes = HUB.dark_pool("SPY", intel)["lanes"]
    for tool in DPS.DARK_POOL_LANES:
        assert lanes[tool]["fresh"] is False, tool
        assert lanes[tool]["serving"] == LT.SERVING_LKG, tool


# ═══════════════════════════════════════════════════════════════════════════
# 4 · LA PRUEBA DE CONSISTENCIA, SOBRE TODOS LOS ESCENARIOS
# ═══════════════════════════════════════════════════════════════════════════

ESCENARIOS = [
    ("vivo", dict(rows=216), 216),
    ("sin actividad", dict(rows=0), 0),
    ("caído con respaldo", dict(rows=0, status=LT.PROVIDER_ERROR, error="timeout",
                                lkg_rows=382, lkg_age=47.0), 382),
    ("caído sin respaldo", dict(rows=0, status=LT.PROVIDER_ERROR, error="timeout"), 0),
    ("cuerpo rechazado", dict(rows=0, status=LT.REQUEST_INVALID, error="HTTP 400"), 0),
    ("ilegible", dict(rows=0, status=LT.PARSER_ERROR, error="no se pudo leer"), 0),
]


@pytest.mark.parametrize("nombre,kwargs,servidas", ESCENARIOS)
@pytest.mark.parametrize("tool,panel", [
    ("dark_flow", "DARK POOL · dark flow"),
    ("dark_pool_levels", "DARK POOL · niveles"),
    ("equity_prints", "DARK POOL · prints de equity"),
])
def test_las_cuatro_vistas_dicen_lo_mismo(nombre, kwargs, servidas, tool, panel):
    """LA REGRESIÓN OBLIGATORIA.

    Si una dice PROVIDER_ERROR, otra no puede decir NO_DATA. Si una sirve LKG,
    ninguna puede llamarlo dato fresco.
    """
    LT.TRUTH.put(LT.executed(tool, symbol="SPY", **kwargs))
    filas = max(int(kwargs.get("rows") or 0), int(kwargs.get("lkg_rows") or 0))
    intel = {tool: {"ready": filas > 0, "rows": [{}] * filas, "count": filas}}
    autoridad, carril, diag, pantalla = _las_cuatro_vistas(tool, panel, intel)

    assert LT.same_semantics(autoridad, carril), f"{nombre}: carril discrepa"
    assert LT.same_semantics(autoridad, diag), f"{nombre}: diagnóstico discrepa"
    assert int(diag["served_rows"]) == servidas
    assert pantalla == autoridad["screen"]
    # La cuarta vista entra en la comparación con las otras tres.
    analista = _vista_analista(tool, intel)
    assert int(analista["rows"]) == servidas, f"{nombre}: el analista discrepa"
    assert bool(analista.get("fresh")) == bool(autoridad["fresh"]), (
        f"{nombre}: uno lo llama fresco y el otro no")
    # Y la regla que separa las dos preguntas: la SECCIÓN sólo se pinta como
    # rota cuando no tiene nada que servir. Ver el test de abajo.
    if autoridad["serving"] != LT.SERVING_NONE:
        assert carril["is_failure"] is False, (
            f"{nombre}: la sección tiene {servidas} filas que enseñar")


def test_el_estado_de_la_EJECUCION_y_el_de_la_SECCION_son_dos_preguntas():
    """La distinción que hay que mantener para no volver a la contradicción.

    Son dos preguntas distintas sobre el mismo carril, y las dos respuestas son
    verdad a la vez:

        ¿falló el refresco?        `current_status` = PROVIDER_ERROR   → sí
        ¿la sección está rota?     `is_failure`     = False            → no,
                                    porque hay 382 filas buenas que enseñar

    Lo que NO puede pasar —y es lo que pasaba— es que una vista use la segunda
    respuesta para afirmar la primera: «CON DATOS» decía que el refresco había
    ido bien, y no fue así. Por eso `current_status` y `serving` son idénticos
    en todas partes, y `is_failure` responde sólo a lo suyo.
    """
    LT.TRUTH.put(LT.executed("dark_flow", symbol="SPY", rows=0,
                             status=LT.PROVIDER_ERROR, error="timeout",
                             lkg_rows=382, lkg_age=47.0))
    intel = {"dark_flow": {"ready": True, "rows": [{}] * 382, "count": 382}}
    carril = HUB.dark_pool("SPY", intel)["lanes"]["dark_flow"]
    assert carril["current_status"] == LT.PROVIDER_ERROR, "el refresco SÍ falló"
    assert carril["is_failure"] is False, "y la sección NO está rota: hay 382 filas"
    assert carril["state"] == DPS.STALE
    assert carril["fresh"] is False, "pero no son de este ciclo, y se dice"
    # Sin respaldo, la misma ejecución sí rompe la sección.
    LT.TRUTH.put(LT.executed("dark_flow", symbol="SPY", rows=0,
                             status=LT.PROVIDER_ERROR, error="timeout"))
    vacio = HUB.dark_pool("SPY", {"dark_flow": {"ready": False, "rows": [], "count": 0}})
    assert vacio["lanes"]["dark_flow"]["is_failure"] is True


def test_un_carril_esperando_no_se_presenta_como_averia_en_ninguna_vista():
    LT.TRUTH.put(LT.waiting("dark_flow", symbol="SPY", state=LT.WAITING_RATE_LIMIT,
                            reason="presupuesto de cuota agotado",
                            waiting_since=time.time() - 12.0))
    intel = {"dark_flow": {"ready": False, "rows": [], "count": 0}}
    autoridad, carril, diag, _ = _las_cuatro_vistas(
        "dark_flow", "DARK POOL · dark flow", intel)
    assert autoridad["is_failure"] is False
    assert carril["is_failure"] is False
    assert carril["state"] == DPS.ESPERANDO
    assert "cuota" in (diag["reason"] or "")


# ═══════════════════════════════════════════════════════════════════════════
# 5 · ESPERAR NO ES FALLAR, PERO ESPERAR SIN FIN ES UNA ANOMALÍA
# ═══════════════════════════════════════════════════════════════════════════

def test_la_espera_publica_desde_cuando_por_que_y_hasta_cuando():
    ahora = time.time()
    t = LT.waiting("max_pain", state=LT.WAITING_DEPENDENCY,
                   reason="le falta expirationDate",
                   waiting_since=ahora - 30.0, next_eligible_at=ahora + 15.0)
    d = t.as_dict()
    assert d["waiting_since"] and d["next_eligible_at"]
    assert d["waiting_seconds"] == pytest.approx(30.0, abs=1.5)
    assert "expirationDate" in d["waiting_reason"]


def test_una_espera_normal_no_genera_anomalia():
    t = LT.waiting("x", waiting_since=time.time() - 5.0)
    assert t.anomaly() is None


def test_una_espera_sin_fin_si_la_genera():
    """No cambia la severidad ni convierte la espera en error: la nombra."""
    t = LT.waiting("x", state=LT.WAITING_RATE_LIMIT, reason="cuota",
                   waiting_since=time.time() - (LT.MAX_WAITING_S + 30.0))
    an = t.anomaly()
    assert an and an["anomaly"] == "ESPERA_EXCESIVA"
    assert an["waiting_seconds"] >= LT.MAX_WAITING_S
    assert t.is_failure is False, "sigue sin ser un fallo"


def test_las_anomalias_salen_en_el_resumen():
    LT.TRUTH.put(LT.waiting("a", waiting_since=time.time() - 600.0))
    LT.TRUTH.put(LT.waiting("b", waiting_since=time.time() - 3.0))
    resumen = LT.TRUTH.snapshot()
    assert [a["tool"] for a in resumen["anomalies"]] == ["a"]


# ═══════════════════════════════════════════════════════════════════════════
# 6 · CADA FALLO DICE QUÉ FASE EXPIRÓ
# ═══════════════════════════════════════════════════════════════════════════

def test_el_fallo_publica_la_fase_y_el_desglose_de_tiempos():
    """«Tardó más de 11.4 s» no dice si se fueron en el pool, conectando o
    esperando al proveedor, y los tres se arreglan al revés."""
    t = LT.executed("equity_prints", rows=0, status=LT.PROVIDER_ERROR,
                    error="el canal tardó más de 11.4 s", phase="READ_TIMEOUT",
                    timing={"queue_wait_ms": 120.0, "pool_wait_ms": 8.1,
                            "connect_ms": None, "read_ms": 11380.0,
                            "request_ms": 11400.0, "timeout_budget_ms": 11400.0,
                            "phase_that_expired": "READ_TIMEOUT"})
    d = t.as_dict()
    assert d["refresh_error_phase"] == "READ_TIMEOUT"
    for campo in ("queue_wait_ms", "pool_wait_ms", "connect_ms", "read_ms",
                  "request_ms", "timeout_budget_ms", "phase_that_expired"):
        assert campo in d["timing"], campo


def test_la_fase_llega_hasta_el_diagnostico():
    LT.TRUTH.put(LT.executed("equity_prints", symbol="SPY", rows=0,
                             status=LT.PROVIDER_ERROR, error="tardó 11.4 s",
                             phase="POOL_TIMEOUT"))
    intel = {"equity_prints": {"ready": False, "rows": [], "count": 0}}
    diag = {r["panel"]: r for r in TA._dark_pool_rows(_fila, intel)}
    fila = diag["DARK POOL · prints de equity"]
    assert fila["phase_that_expired"] == "POOL_TIMEOUT"
    assert "POOL_TIMEOUT" in (fila["reason"] or "")


# ═══════════════════════════════════════════════════════════════════════════
# 7 · EL SNAPSHOT DE MUROS NUNCA ES UNA CAJA VACÍA
# ═══════════════════════════════════════════════════════════════════════════

def test_el_snapshot_publica_siempre_uno_de_los_cuatro_estados():
    assert set(WS.ESTADOS) == {WS.COMPLETO, WS.LKG, WS.NO_CALCULABLE,
                               WS.WAITING_DEPENDENCY}


def test_sin_ingredientes_se_nombran_uno_a_uno():
    out = WE.walls_from_hub("SPY", {"contract_greeks": {"rows": []}},
                            spot=None, price_as_of=None, expiry=None)
    auditoria = WE.wall_audit(out, {})
    assert auditoria["snapshot_state"] in WS.ESTADOS
    causa = auditoria["controls_absent_because"]
    assert causa["missing"], "hay que NOMBRAR el ingrediente que falta"
    assert {"GAMMA", "SPOT", "VENCIMIENTO"} <= set(causa["missing"])
    assert len(causa["expected_controls"]) == 10


def test_si_el_carril_todavia_no_corrio_es_ESPERA_y_no_no_calculable():
    """«No calculable» afirma que el dato no existe; esperar dice la verdad."""
    LT.TRUTH.put(LT.waiting("options_order_flow_raw", symbol="SPY",
                            state=LT.WAITING_SCHEDULED, reason="aún sin turno"))
    out = WE.walls_from_hub("SPY", {"contract_greeks": {"rows": []}},
                            spot=None, price_as_of=None, expiry=None)
    assert out["snapshot_state"] == WS.WAITING_DEPENDENCY
    assert "ESPERANDO" in out["verdict_label"]
    causa = WE.wall_audit(out, {})["controls_absent_because"]
    assert "options_order_flow_raw" in causa["detail"]


def test_cuando_hay_snapshot_se_publican_los_diez_controles():
    filas = []
    for i in range(4):
        for tipo in ("call", "put"):
            filas.append({"strike": 100.0 + i, "option_type": tipo,
                          "expiration": "2026-10-16", "gamma": 0.01,
                          "open_interest": 500, "multiplier": 100.0})
    out = WE.walls_from_hub("SPY", {"contract_greeks": {"rows": filas}},
                            spot=103.0, price_as_of="2026-09-22T14:30:00+00:00",
                            expiry="2026-10-16")
    auditoria = WE.wall_audit(out, {})
    assert out["snapshot_state"] == WS.COMPLETO
    assert len(auditoria["controls"]) == 10
    assert auditoria["controls_absent_because"] == {}


def test_la_interfaz_pinta_la_causa_en_vez_del_hueco():
    js = (__import__("pathlib").Path("app/static/itmq_app.js")).read_text(encoding="utf-8")
    assert "controls_absent_because" in js
    assert "function wallControlsFallback(" in js
    # La frase de la caja vacía ya no se PASA como texto de respaldo; sólo puede
    # quedar citada en el comentario que explica por qué desapareció.
    llamada = js[js.index("fillTable('tblWallControls'"):]
    llamada = llamada[:llamada.index(");")]
    assert "wallControlsFallback(wc)" in llamada
    assert "no se publicaron" not in llamada

"""v1.57.1 · EL ENVEJECIMIENTO ESTABA A MEDIAS Y MEDÍA LA ESPERA DESDE 1970.

La corrección de v1.57.0 quitó el hambre de la cola, pero dejó dos costuras que
se ven al leer el código junto, no por separado:

1 · LA SENTINELA.  La espera se calculaba así:

        effective_priority(t.key, now - self._fetched_at.get(t.key, 0.0))

    `self._fetched_at` se VACÍA en cada cambio de activo y arranca vacío. Para
    toda herramienta nunca servida el defecto era `0.0`, así que la espera no
    era «cero segundos»: era `now`, unos 1.700 millones de segundos. Con
    AGING_SECONDS = 45 eso son treinta y ocho millones de ascensos y TODAS
    caen al suelo de prioridad en el primer ciclo. El orden de carga —la
    exposición primero, las noticias al final— dejaba de existir exactamente
    cuando más importa: en frío y justo después de cambiar de activo.

2 · LA RÁFAGA.  El lote ordenaba por prioridad EFECTIVA y la ráfaga filtraba
    por la BASE:

        priority_due = [t for t in remaining_due
                        if _PRIORITY.get(t.key, _PRIORITY_DEFAULT) <= BURST_MAX_PRIORITY]

    Una herramienta ya ascendida por espera a la clase que dibuja la pantalla
    seguía excluida de la ventana de arranque. Dos reglas para una decisión.

Y una tercera que no es un defecto de cálculo sino de información: «sin
intentos» no es un diagnóstico, es la ausencia de uno. El Auditor tiene que
decir si la herramienta está en enfriamiento, si su cadencia no ha vencido o si
es exigible y se quedó sin presupuesto. Son tres remedios distintos.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.providers.quantdata.intelligence import (
    AGING_FLOOR, AGING_SECONDS, BURST_MAX_PRIORITY, _PRIORITY, _PRIORITY_DEFAULT,
    QuantDataIntelligence, effective_priority, in_burst_class,
)


@pytest.fixture()
def motor():
    return QuantDataIntelligence()


def _orden(motor, now):
    """El MISMO criterio que usa `refresh_due` para formar el lote."""
    return sorted(motor.catalog.keys(),
                  key=lambda k: (effective_priority(k, motor._waited(k, now)),
                                 motor._fetched_at.get(k, motor._eligible_since)))


# ═══════════════════════════════════════════════════════════════════════════
# 1 · LA SENTINELA: NUNCA SERVIDA ≠ ESPERANDO DESDE EL EPOCH
# ═══════════════════════════════════════════════════════════════════════════

def test_una_herramienta_nunca_servida_no_ha_esperado_desde_1970(motor):
    now = time.time()
    esperado = motor._waited("news", now)
    assert esperado < 5.0, (
        f"una herramienta recién elegible lleva esperando {esperado:,.0f} s; "
        "el defecto de `_fetched_at.get(key, 0.0)` mide desde el epoch")


def test_la_espera_nunca_es_negativa(motor):
    """Un reloj que retrocede no puede producir prioridades imposibles."""
    assert motor._waited("news", motor._eligible_since - 600.0) == 0.0


def test_sin_la_sentinela_el_orden_de_carga_se_DESTRUYE():
    """La prueba de que el defecto era real y no una precaución teórica."""
    now = time.time()
    viejo = {k: effective_priority(k, now - 0.0) for k in ("gex_by_strike", "news")}
    assert viejo["gex_by_strike"] == viejo["news"] == AGING_FLOOR, (
        "con la espera medida desde el epoch, la exposición y las noticias "
        "empataban en el suelo de prioridad")


def test_con_la_sentinela_la_exposicion_va_ANTES_que_las_noticias(motor):
    now = time.time()
    orden = _orden(motor, now)
    assert orden.index("gex_by_strike") < orden.index("news")
    assert orden.index("dex_by_strike") < orden.index("gainers_losers")


def test_en_frio_el_primer_lote_es_la_clase_que_dibuja_la_pantalla(motor):
    """Las cuatro primeras de un presupuesto corto son de prioridad 0."""
    now = time.time()
    primeras = _orden(motor, now)[:4]
    assert all(_PRIORITY.get(k, _PRIORITY_DEFAULT) == 0 for k in primeras), primeras


def test_la_clase_de_prioridad_se_respeta_entera_en_frio(motor):
    """No sólo la cabeza: el orden completo es monótono por clase."""
    now = time.time()
    clases = [_PRIORITY.get(k, _PRIORITY_DEFAULT) for k in _orden(motor, now)]
    assert clases == sorted(clases), clases


def test_una_herramienta_ya_servida_cuenta_desde_su_ultimo_exito(motor):
    now = time.time()
    motor._fetched_at["news"] = now - 90.0
    assert motor._waited("news", now) == pytest.approx(90.0, abs=0.5)


def test_el_cambio_de_activo_reinicia_la_espera(motor):
    """Tras cambiar de activo nadie ha esperado nada TODAVÍA."""
    antiguo = motor._eligible_since
    motor._eligible_since = antiguo - 10_000.0
    motor._fetched_at["news"] = time.time()
    asyncio.run(motor.select_asset("QQQ"))
    assert motor._symbol == "QQQ"
    assert not motor._fetched_at, "el cambio de activo no vació `_fetched_at`"
    assert motor._waited("news", time.time()) < 5.0, (
        "tras el cambio de activo la espera seguía contando desde antes")


def test_el_cambio_de_activo_no_hunde_el_orden_de_carga(motor):
    asyncio.run(motor.select_asset("SPY"))
    orden = _orden(motor, time.time())
    assert _PRIORITY.get(orden[0], _PRIORITY_DEFAULT) == 0, orden[:3]


def test_el_envejecimiento_SIGUE_funcionando_con_la_sentinela(motor):
    """La corrección no puede devolver el hambre a la cola."""
    now = time.time()
    motor._fetched_at["news"] = now - 6 * AGING_SECONDS
    motor._fetched_at["gex_by_strike"] = now - 1.0
    orden = _orden(motor, now)
    assert orden.index("news") < orden.index("gex_by_strike"), (
        "una herramienta de cola que lleva seis periodos esperando tiene que "
        "adelantar a una de cabeza recién servida")


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LA RÁFAGA: UNA SOLA REGLA
# ═══════════════════════════════════════════════════════════════════════════

def test_la_rafaga_admite_lo_que_dibuja_la_pantalla():
    assert in_burst_class("gex_by_strike", 0.0)
    assert in_burst_class("net_flow", 0.0)


def test_la_rafaga_excluye_el_contexto_secundario_recien_servido():
    assert not in_burst_class("news", 0.0)
    assert not in_burst_class("term_structure", 0.0)


def test_la_rafaga_admite_lo_que_YA_ascendio_por_espera():
    """El defecto: `term_structure` con prioridad efectiva 0 y aun así fuera."""
    espera = 3 * AGING_SECONDS
    assert effective_priority("term_structure", espera) <= BURST_MAX_PRIORITY
    assert _PRIORITY["term_structure"] > BURST_MAX_PRIORITY, (
        "el caso pierde sentido si la prioridad base ya entraba en la ráfaga")
    assert in_burst_class("term_structure", espera), (
        "filtrar la ráfaga por prioridad BASE dejaba fuera a quien el lote ya "
        "ordenaba como cabeza")


def test_la_rafaga_y_el_lote_no_pueden_discrepar():
    """Una sola regla: lo que el lote pone en cabeza, la ráfaga lo admite."""
    for key in _PRIORITY:
        for espera in (0.0, AGING_SECONDS, 3 * AGING_SECONDS, 10 * AGING_SECONDS):
            assert in_burst_class(key, espera) == (
                effective_priority(key, espera) <= BURST_MAX_PRIORITY)


def test_la_rafaga_no_se_convierte_en_barra_libre():
    """Envejecer no es un pase: recién servida, la cola sigue fuera."""
    fuera = [k for k in _PRIORITY if not in_burst_class(k, 0.0)]
    assert len(fuera) >= 20, (
        f"sólo {len(fuera)} herramientas quedan fuera de la ráfaga en frío; "
        "la ventana de arranque habría dejado de estar acotada")


def test_el_codigo_no_conserva_la_regla_vieja():
    """Guardia contra la reaparición de las dos reglas paralelas."""
    import pathlib
    fuente = pathlib.Path("app/providers/quantdata/intelligence.py").read_text("utf-8")
    cuerpo = fuente.split("burst = self._bursting()", 1)[1].split("batch = sorted", 1)[0]
    assert "_PRIORITY.get" not in cuerpo, (
        "la ráfaga volvió a filtrar por la prioridad base en vez de la efectiva")


# ═══════════════════════════════════════════════════════════════════════════
# 3 · «SIN INTENTOS» TIENE QUE DECIR POR QUÉ
# ═══════════════════════════════════════════════════════════════════════════

class _Herramienta:
    attempts: list = []
    last_error = None
    provider_status = None
    stripped_fields: list = []
    validation_error = None

    def candidates(self):
        return ["/v1/x"]


def _diag(motor, **sched):
    base = {"waited_seconds": 12.0, "base_priority": 3, "effective_priority": 3,
            "due": True, "cooldown_seconds": 0.0, "never_fetched": True}
    base.update(sched)
    return motor._pending_diagnosis(_Herramienta(), "PENDIENTE", {}, base)


def test_sin_intentos_por_enfriamiento_lo_dice(motor):
    d = _diag(motor, cooldown_seconds=48.0)
    assert d["verdict"] == "SIN_INTENTAR"
    assert "enfriamiento" in d["action"]
    assert "48" in d["action"]


def test_sin_intentos_por_cadencia_no_es_un_fallo(motor):
    d = _diag(motor, due=False)
    assert "cadencia" in d["action"]
    assert "no es un fallo" in d["action"]


def test_sin_intentos_exigible_señala_el_presupuesto(motor):
    d = _diag(motor, due=True)
    assert "PRESUPUESTO DE CUOTA" in d["action"]


def test_la_evidencia_conserva_las_rutas_y_añade_la_espera(motor):
    d = _diag(motor, waited_seconds=137.0, base_priority=3, effective_priority=0)
    assert "1 ruta(s) declarada(s), 0 intentos" in d["evidence"]
    assert "espera 137 s" in d["evidence"]
    assert "prioridad 3→0" in d["evidence"]


def test_sin_estado_del_programador_el_diagnostico_no_revienta(motor):
    d = motor._pending_diagnosis(_Herramienta(), "PENDIENTE", {})
    assert d["verdict"] == "SIN_INTENTAR"
    assert "1 ruta(s) declarada(s), 0 intentos" in d["evidence"]


def test_un_error_real_NO_se_disfraza_de_sin_intentar(motor):
    """El diagnóstico nuevo no puede tapar un 403."""
    h = _Herramienta()
    h.last_error = "403 Forbidden"
    d = motor._pending_diagnosis(h, "PENDIENTE", {}, {"due": True})
    assert d["verdict"] == "NO_AUTORIZADO"


# ═══════════════════════════════════════════════════════════════════════════
# 4 · EL AUDITOR PUBLICA EL ESTADO DEL PROGRAMADOR
# ═══════════════════════════════════════════════════════════════════════════

def test_cada_herramienta_publica_su_estado_de_programador(motor):
    cov = motor.coverage()
    assert cov["tools"], "el catálogo llegó vacío"
    for t in cov["tools"]:
        s = t["scheduler"]
        assert set(s) == {"waited_seconds", "base_priority", "effective_priority",
                          "due", "cooldown_seconds", "never_fetched",
                          "pages_paused"}, s
        assert isinstance(s["due"], bool)
        assert isinstance(s["never_fetched"], bool)


def test_el_estado_del_programador_es_json_estricto(motor):
    json.dumps([t["scheduler"] for t in motor.coverage()["tools"]], allow_nan=False)


def test_en_frio_todas_constan_como_nunca_servidas(motor):
    cov = motor.coverage()
    assert all(t["scheduler"]["never_fetched"] for t in cov["tools"])
    assert all(t["scheduler"]["waited_seconds"] < 60.0 for t in cov["tools"]), (
        "una herramienta recién elegible no puede constar con siglos de espera")


def test_tras_servir_una_el_auditor_lo_refleja(motor):
    motor._fetched_at["news"] = time.time() - 30.0
    fila = next(t for t in motor.coverage()["tools"] if t["key"] == "news")
    assert fila["scheduler"]["never_fetched"] is False
    assert fila["scheduler"]["waited_seconds"] == pytest.approx(30.0, abs=1.0)


# ═══════════════════════════════════════════════════════════════════════════
# 5 · EL AUDITOR LO ENSEÑA, NO SÓLO LO CALCULA
# ═══════════════════════════════════════════════════════════════════════════

def _qd_diagnosis_src() -> str:
    import pathlib
    src = pathlib.Path("app/static/itmq_app.js").read_text("utf-8")
    cuerpo = src.split("function qdDiagnosis(t)", 1)[1]
    return cuerpo.split("\n  }\n", 1)[0]


def test_el_auditor_no_se_queda_en_sin_intentos():
    """Un diagnóstico que sólo vive en el JSON no lo lee nadie."""
    cuerpo = _qd_diagnosis_src()
    assert "t.scheduler" in cuerpo, "el Auditor no lee el estado del programador"
    assert "t.diagnosis" in cuerpo, "el Auditor no enseña la causa ya clasificada"
    for campo in ("waited_seconds", "base_priority", "effective_priority", "cooldown_seconds"):
        assert campo in cuerpo, f"el Auditor no enseña {campo}"


def test_la_causa_del_backend_va_escapada():
    """Texto que viene del servidor no se inyecta crudo en el DOM."""
    cuerpo = _qd_diagnosis_src()
    assert "esc(causa)" in cuerpo

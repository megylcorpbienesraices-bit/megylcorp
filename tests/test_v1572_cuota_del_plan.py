"""v1.57.2 · LAS 34 HERRAMIENTAS «SIN INTENTOS» ERAN EL PLAN AGOTADO.

La captura del Auditor lo decía entero y en dos sitios, pero en letra pequeña:

    cuota 7/240        ← quedan SIETE peticiones de 240
    0/34 · páginas pausadas

`ENGINE_RESERVE = 12`, así que `budget_for_pages` devolvía CERO en cada ciclo y
el carril de páginas llevaba veintitrés minutos sin pedir nada. Las herramientas
estaban exigibles, con prioridad efectiva 0, y aun así «sin intentos». No era el
proveedor, ni la autorización, ni el programador: era que no quedaba plan.

Tres defectos REALES detrás de eso:

1 · EL RITMO SE INFERÍA DE UNA CABECERA QUE NO DICE LO QUE PARECE. `Reset: 60`
    se tomaba como «la ventana del plan dura 60 s», y muchas veces describe un
    CUBO DE RITMO, no el tope contratado. Con `limit = 240` salían 4 req/s, el
    intervalo caía al suelo de 15 s y el carril del motor se comía las 240
    peticiones en un cuarto de hora. El comentario del propio código ya decía la
    regla correcta —«equivocarse por rápido agota el plan en minutos»— y el
    código la contradecía tres líneas más abajo.

2 · LA RÁFAGA SE SALTABA EL GUARDIÁN DE CUOTA ENTERO. `allowed = max(allowed,
    len(priority_due))` ignora `budget_for_pages`: con el plan en las últimas
    seguía pidiendo, y podía comerse la reserva del motor —la que sostiene la
    estructura— para dibujar tablas de presentación.

3 · EL AUDITOR NO LO DECÍA. Lo insinuaba en una pastilla y lo dejaba deducir de
    un número. Con 34 filas diciendo «sin intentos», se lee como un fallo del
    proveedor. Ahora hay una línea que lo nombra y dice qué hacer.
"""
from __future__ import annotations

import pytest

from app.providers.quantdata.shared import (
    ENGINE_FAST_REQUESTS, ENGINE_RESERVE, VENTANA_CREIBLE_S, QuotaGuard,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · UNA VENTANA CORTA NO ES PRUEBA DEL PLAN
# ═══════════════════════════════════════════════════════════════════════════

def test_un_reset_de_un_minuto_NO_autoriza_a_quemar_el_plan():
    """El caso exacto que dejó la terminal muda: 240 de tope, Reset de 60 s."""
    q = QuotaGuard()
    q.note(remaining=240, limit=240, reset_seconds=60.0)
    intervalo = q.recommended_interval(ENGINE_FAST_REQUESTS)
    por_dia = (86_400.0 / intervalo) * ENGINE_FAST_REQUESTS
    assert por_dia <= 240, (
        f"{por_dia:.0f} peticiones/día no caben en un plan de 240; a ese ritmo "
        "el plan se agota y la sesión entera se queda sin datos")


def test_un_reset_largo_SI_describe_el_plan_y_se_respeta():
    """Y no se castiga al plan que sí publica su ventana."""
    q = QuotaGuard()
    q.note(remaining=236, limit=240, reset_seconds=3600.0)
    intervalo = q.recommended_interval(ENGINE_FAST_REQUESTS)
    assert intervalo < 300.0, "un plan de 240/hora permite refrescar en minutos"
    por_hora = (3600.0 / intervalo) * ENGINE_FAST_REQUESTS
    assert por_hora < 240, f"{por_hora:.0f} peticiones/hora no caben en el plan"


def test_el_umbral_de_credibilidad_es_explicito():
    assert VENTANA_CREIBLE_S == 300.0


@pytest.mark.parametrize("reset", [1.0, 30.0, 60.0, 120.0, 299.0])
def test_ninguna_ventana_corta_supera_el_ritmo_diario(reset):
    q = QuotaGuard()
    q.note(remaining=240, limit=240, reset_seconds=reset)
    por_dia = (86_400.0 / q.recommended_interval(ENGINE_FAST_REQUESTS)) * ENGINE_FAST_REQUESTS
    assert por_dia <= 240


def test_el_plan_DECLARADO_manda_sobre_toda_conjetura():
    """Quien conoce su plan lo declara y se acabó la adivinanza."""
    q = QuotaGuard()
    q.declared_requests, q.declared_window_s = 240.0, 60.0
    q.note(remaining=240, limit=240, reset_seconds=86_400.0)
    assert q.recommended_interval(ENGINE_FAST_REQUESTS) == pytest.approx(15.0, abs=0.1)


def test_la_ventana_MEDIDA_manda_sobre_la_cabecera():
    q = QuotaGuard()
    q.observed_window_s = 3600.0
    q.note(remaining=240, limit=240, reset_seconds=60.0)
    intervalo = q.recommended_interval(ENGINE_FAST_REQUESTS)
    por_hora = (3600.0 / intervalo) * ENGINE_FAST_REQUESTS
    assert por_hora < 240


def test_sin_telemetria_no_se_corre():
    q = QuotaGuard()
    assert q.recommended_interval(ENGINE_FAST_REQUESTS) >= 60.0


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LA RÁFAGA NO PUEDE CRUZAR LA RESERVA DEL MOTOR
# ═══════════════════════════════════════════════════════════════════════════

def _agotado() -> QuotaGuard:
    q = QuotaGuard()
    q.note(remaining=7, limit=240, reset_seconds=60.0)
    return q


def test_con_el_plan_agotado_la_rafaga_no_pide_nada():
    assert _agotado().burst_ceiling(10) == 0


def test_con_el_proveedor_limitando_la_rafaga_no_pide_nada():
    q = QuotaGuard()
    q.note(remaining=200, limit=240, reset_seconds=60.0)
    q.note_rate_limited(30.0)
    assert q.burst_ceiling(10) == 0


def test_la_rafaga_no_se_come_la_reserva_del_motor():
    q = QuotaGuard()
    q.note(remaining=ENGINE_RESERVE + 3, limit=240, reset_seconds=60.0)
    assert q.burst_ceiling(10) == 3, "la ráfaga solo puede gastar por encima de la reserva"


def test_la_rafaga_sigue_sirviendo_para_lo_que_existe():
    """La corrección no puede dejarla inútil con el plan sano."""
    q = QuotaGuard()
    q.note(remaining=200, limit=240, reset_seconds=60.0)
    assert q.burst_ceiling(10) == 10


def test_sin_telemetria_la_rafaga_no_se_bloquea():
    assert QuotaGuard().burst_ceiling(10) == 10


def test_el_programador_aplica_el_tope_de_la_rafaga():
    """Guardia de código: el `max()` sin tope era el agujero."""
    import pathlib
    src = pathlib.Path("app/providers/quantdata/intelligence.py").read_text("utf-8")
    cuerpo = src.split("burst = self._bursting()", 1)[1].split("batch = sorted", 1)[0]
    assert "QUOTA.burst_ceiling" in cuerpo, "la ráfaga volvió a saltarse la cuota"
    assert "allowed = max(allowed, len(priority_due))" not in cuerpo


# ═══════════════════════════════════════════════════════════════════════════
# 3 · LA CAUSA SE DICE, NO SE DEDUCE
# ═══════════════════════════════════════════════════════════════════════════

def test_el_guardian_publica_por_que_estan_paradas_las_paginas():
    p = _agotado().pages_paused_reason()
    assert p["reason"] == "PLAN_AGOTADO"
    assert p["remaining"] == 7 and p["limit"] == 240
    assert p["engine_reserve"] == ENGINE_RESERVE


def test_la_pausa_coincide_SIEMPRE_con_el_presupuesto_real():
    """Decir «parado» y luego permitir pedir sería peor que no decir nada."""
    for restantes in (0, 1, ENGINE_RESERVE - 1, ENGINE_RESERVE, ENGINE_RESERVE + 1, 50, 240):
        q = QuotaGuard()
        q.note(remaining=restantes, limit=240, reset_seconds=60.0)
        parado = q.pages_paused_reason() is not None
        assert parado == (q.budget_for_pages(10) == 0), restantes


def test_el_limite_de_ritmo_tambien_se_nombra():
    q = QuotaGuard()
    q.note(remaining=200, limit=240, reset_seconds=60.0)
    q.note_rate_limited(45.0)
    p = q.pages_paused_reason()
    assert p["reason"] == "RATE_LIMITED" and p["seconds"] > 0


def test_con_el_plan_sano_no_se_inventa_una_pausa():
    q = QuotaGuard()
    q.note(remaining=200, limit=240, reset_seconds=60.0)
    assert q.pages_paused_reason() is None


def test_el_snapshot_lo_lleva_al_auditor():
    q = _agotado()
    assert (q.snapshot().get("pages_paused") or {}).get("reason") == "PLAN_AGOTADO"


def test_el_diagnostico_por_herramienta_nombra_la_cuota_exacta():
    # El guardián se toma DEL MÓDULO QUE SE PRUEBA, no de `shared`: otra prueba
    # de la suite hace `importlib.reload(shared)` y deja dos guardianes vivos —el
    # nuevo en `shared` y el viejo, que es el que `intelligence` sigue usando—.
    # Escribir en el que no se lee daba un verde o un rojo según el orden.
    from app.providers.quantdata import intelligence as I
    from app.providers.quantdata.intelligence import QuantDataIntelligence
    QUOTA = I.QUOTA
    previo = (QUOTA.remaining, QUOTA.limit, QUOTA.updated_at, QUOTA.rate_limited_until)
    try:
        # El guardián es global: esta prueba fija SU estado entero, porque otra
        # prueba de la suite pudo dejarlo limitado y el veredicto sería otro.
        QUOTA.rate_limited_until = 0.0
        QUOTA.note(remaining=7, limit=240, reset_seconds=60.0)
        fila = next(t for t in QuantDataIntelligence().coverage()["tools"]
                    if t["key"] == "max_pain")
        accion = fila["diagnosis"]["action"]
        assert "CUOTA DEL PLAN AGOTADA" in accion
        assert "7 de 240" in accion
        assert "No es el endpoint ni la autorización" in accion
        assert fila["scheduler"]["pages_paused"]["reason"] == "PLAN_AGOTADO"
    finally:
        (QUOTA.remaining, QUOTA.limit, QUOTA.updated_at,
         QUOTA.rate_limited_until) = previo


def test_el_auditor_lo_ENSEÑA_en_una_linea():
    import pathlib
    js = pathlib.Path("app/static/itmq_app.js").read_text("utf-8")
    assert "qdQuotaNote" in js, "sin la línea, la causa vuelve a deducirse"
    assert "CUOTA DEL PLAN AGOTADA" in js
    assert "QUANTDATA_PLAN_REQUESTS" in js, "hay que decir qué hacer, no sólo qué pasa"
    html = pathlib.Path("app/templates/terminal.html").read_text("utf-8")
    assert 'id="qdQuotaNote"' in html


def test_la_pastilla_se_pone_en_rojo_cuando_esta_parado():
    import pathlib
    js = pathlib.Path("app/static/itmq_app.js").read_text("utf-8")
    trozo = js.split("pill('qdPill'", 1)[1].split(");", 1)[0]
    assert "pagesPaused ? 'down'" in trozo, "parado tiene que verse parado"

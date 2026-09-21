"""v1.57.3 · EL CONTRATO PUBLICADO DE QUANT DATA, APLICADO AL PIE DE LA LETRA.

    240 peticiones / 60 s   en VENTANA DESLIZANTE
     20 peticiones /  1 s   de ráfaga
    `X-RateLimit-Reset`     segundos hasta que el cubo se rellena

La versión anterior trataba un `Reset: 60` como una señal DUDOSA —«una ventana
tan corta no puede ser el plan, será un cubo de ritmo»— y acotaba el ritmo con
una ventana DIARIA inventada. Dos errores en uno:

1 · Con el contrato real, 240/60 s son 4 peticiones por segundo sostenidas. El
    carril del motor pide 4 cada 15 s: 16 por minuto, el 6,7 % del presupuesto.
    NUNCA estuvo quemando el plan. La «corrección» anterior bajaba el ciclo del
    motor a 1.920 s —32 minutos— y habría dejado la terminal inservible.

2 · En una ventana DESLIZANTE no existe el «plan agotado». `remaining = 7`
    significa que las 233 anteriores siguen dentro de los últimos 60 s, y se
    reponen solas conforme van saliendo por el otro extremo. Es una espera de
    SEGUNDOS. Llamarlo agotamiento mandaba a buscar el fallo donde no estaba.

Lo que sí faltaba, y es lo que esta versión añade: ITM QUANT no llevaba NINGÚN
contador propio. Se enteraba de que se había pasado cuando el proveedor le
devolvía un 429. Con una ráfaga de arranque a 5 en paralelo cada 1,2 s, eso es
exactamente lo que pasaba.
"""
from __future__ import annotations

import time

import pytest

from app.providers.quantdata.shared import (
    BURST_LIMIT, BURST_WINDOW_S, ENGINE_FAST_REQUESTS, ENGINE_RESERVE,
    SUSTAINED_LIMIT, SUSTAINED_WINDOW_S, QuotaGuard, VentanaDeslizante,
)


def _sana() -> QuotaGuard:
    """Guardián con las cabeceras que publica el contrato."""
    q = QuotaGuard()
    q.note(remaining=SUSTAINED_LIMIT, limit=SUSTAINED_LIMIT,
           reset_seconds=SUSTAINED_WINDOW_S)
    return q


# ═══════════════════════════════════════════════════════════════════════════
# 1 · EL CONTRATO ESTÁ ESCRITO, NO ADIVINADO
# ═══════════════════════════════════════════════════════════════════════════

def test_las_constantes_son_las_del_contrato_oficial():
    assert (SUSTAINED_LIMIT, SUSTAINED_WINDOW_S) == (240, 60.0)
    assert (BURST_LIMIT, BURST_WINDOW_S) == (20, 1.0)


def test_el_ritmo_del_motor_sale_del_contrato_y_cabe_de_sobra():
    intervalo = _sana().recommended_interval(ENGINE_FAST_REQUESTS)
    assert intervalo == 15.0, "el contrato permite el suelo de 15 s"
    por_minuto = (60.0 / intervalo) * ENGINE_FAST_REQUESTS
    assert por_minuto == 16.0
    assert por_minuto < SUSTAINED_LIMIT * 0.1, (
        f"{por_minuto:.0f}/min sobre {SUSTAINED_LIMIT}/min: el carril del motor "
        "nunca fue el que agotaba nada")


def test_sin_cabeceras_todavia_se_asume_el_CONTRATO_no_una_ventana_diaria():
    """El fallo de v1.57.2: caer a 240/día y estrangular la terminal."""
    intervalo = QuotaGuard().recommended_interval(ENGINE_FAST_REQUESTS)
    assert intervalo == 15.0, (
        f"{intervalo} s por ciclo; se volvió a inventar una ventana más lenta "
        "que la que el proveedor publica")


def test_un_reset_de_60_segundos_ya_NO_se_trata_como_sospechoso():
    q = QuotaGuard()
    q.note(remaining=240, limit=240, reset_seconds=60.0)
    assert q.recommended_interval(ENGINE_FAST_REQUESTS) == 15.0


def test_el_limite_del_servidor_se_adopta_dinamicamente():
    """Si el proveedor cambia el plan, no hay que recompilar nada."""
    q = QuotaGuard()
    q.note(remaining=600, limit=600, reset_seconds=60.0)
    assert q.snapshot()["contract"]["sustained_limit"] == 600


def test_el_plan_declarado_por_el_operador_sigue_mandando():
    q = QuotaGuard()
    q.declared_requests, q.declared_window_s = 60.0, 60.0
    q.note(remaining=240, limit=240, reset_seconds=60.0)
    intervalo = q.recommended_interval(ENGINE_FAST_REQUESTS)
    assert (60.0 / intervalo) * ENGINE_FAST_REQUESTS <= 60.0


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LA VENTANA ES DESLIZANTE, NO UN CUBO QUE SE VACÍA DE GOLPE
# ═══════════════════════════════════════════════════════════════════════════

def test_la_ventana_suelta_por_el_extremo_viejo():
    v = VentanaDeslizante(3, 60.0)
    t = 1_000.0
    v.anotar(t); v.anotar(t + 10); v.anotar(t + 20)
    assert v.libres(t + 30) == 0
    assert v.libres(t + 61) == 1, "la primera ya salió de la ventana"
    assert v.libres(t + 71) == 2
    assert v.libres(t + 81) == 3


def test_una_ventana_deslizante_no_permite_el_doble_en_el_cambio_de_cubo():
    """El error clásico de la ventana FIJA: 2N peticiones a caballo del corte."""
    v = VentanaDeslizante(10, 60.0)
    t = 1_000.0
    v.anotar(t + 59, 10)
    assert v.libres(t + 60) == 0, (
        "con ventana fija aquí habría 10 huecos y se colarían 20 en dos segundos")
    assert v.libres(t + 118.9) == 0, "siguen dentro de los últimos 60 s"
    assert v.libres(t + 119.1) == 10, "salen 60 s después de HABERSE HECHO"


def test_la_espera_es_la_del_hueco_mas_proximo():
    v = VentanaDeslizante(2, 60.0)
    t = 1_000.0
    v.anotar(t); v.anotar(t + 30)
    assert v.espera_s(t + 30) == pytest.approx(30.0)
    assert v.espera_s(t + 61) == 0.0


def test_con_pocas_restantes_la_espera_es_de_SEGUNDOS_y_se_dice_transitoria():
    """`remaining = 7` en la captura del operador: no era un plan agotado."""
    q = QuotaGuard()
    q.note(remaining=7, limit=240, reset_seconds=60.0)
    p = q.pages_paused_reason()
    assert p is not None
    assert p["transient"] is True
    assert p["seconds"] <= SUSTAINED_WINDOW_S, (
        "una ventana deslizante se repone dentro de su propio periodo")
    assert p["reason"] != "PLAN_AGOTADO"


def test_ningun_veredicto_dice_ya_que_el_plan_esta_agotado():
    import pathlib
    for f in ("app/providers/quantdata/shared.py",
              "app/providers/quantdata/intelligence.py",
              "app/static/itmq_app.js"):
        txt = pathlib.Path(f).read_text("utf-8")
        # Se permite nombrarlo en la explicación de POR QUÉ era falso; lo que no
        # puede volver es el veredicto en una cadena que se le enseñe al operador.
        for linea in txt.splitlines():
            limpia = linea.strip()
            if limpia.startswith(("#", "//", "*")) or "v1.57.3" in linea:
                continue
            assert "PLAN_AGOTADO" not in limpia and "PLAN AGOTADA" not in limpia, (f, linea)


# ═══════════════════════════════════════════════════════════════════════════
# 3 · LAS 20 POR SEGUNDO SE RESPETAN ANTES DE QUE EL PROVEEDOR LAS NIEGUE
# ═══════════════════════════════════════════════════════════════════════════

def test_la_rafaga_del_contrato_se_agota_en_un_segundo_y_se_repone_al_siguiente():
    q = _sana()
    assert q.headroom() == BURST_LIMIT, "el freno de un segundo es el que muerde"
    q.spend(BURST_LIMIT)
    assert q.headroom() == 0
    p = q.pages_paused_reason()
    assert p["reason"] == "RAFAGA_20_POR_SEGUNDO"
    assert 0.0 < p["seconds"] <= BURST_WINDOW_S


def test_la_rafaga_de_arranque_no_puede_cruzar_las_20_por_segundo():
    """5 en paralelo cada 1,2 s era lo que provocaba los 429."""
    q = _sana()
    assert q.burst_ceiling(100) == BURST_LIMIT - ENGINE_RESERVE
    q.spend(BURST_LIMIT - ENGINE_RESERVE)
    assert q.burst_ceiling(100) == 0


def test_la_rafaga_nunca_se_come_la_reserva_del_motor():
    q = _sana()
    assert q.burst_ceiling(100) + ENGINE_RESERVE <= BURST_LIMIT


def test_el_carril_del_motor_conserva_su_reserva_cuando_las_paginas_no_pueden():
    q = _sana()
    q.spend(BURST_LIMIT - ENGINE_RESERVE)
    assert q.budget_for_pages(20) == 0
    assert q.headroom(reserve=0) == ENGINE_RESERVE, (
        "la reserva existe para que la estructura siga hidratándose")


def test_el_freno_sostenido_muerde_cuando_toca():
    q = _sana()
    t = time.time()
    # 240 peticiones repartidas a lo largo del minuto: ninguna ráfaga, pero la
    # ventana sostenida se llena igual.
    q._sostenida.anotar(t - 30.0, SUSTAINED_LIMIT)
    p = q.pages_paused_reason()
    assert p["reason"] == "VENTANA_DESLIZANTE"
    assert p["seconds"] == pytest.approx(30.0, abs=1.0)


def test_el_contador_local_frena_aunque_el_servidor_diga_que_queda_de_todo():
    """Sin contador propio sólo nos enterábamos con el 429."""
    q = QuotaGuard()
    q.note(remaining=240, limit=240, reset_seconds=60.0)
    q.spend(BURST_LIMIT)
    assert q.budget_for_pages(50) == 0, (
        "el servidor todavía dice 220 restantes, pero las 20 del segundo ya se usaron")


def test_el_contador_y_las_cabeceras_se_aplican_los_DOS():
    q = QuotaGuard()
    q.note(remaining=3, limit=240, reset_seconds=60.0)
    assert q.headroom() == 3, "manda la cabecera cuando es más restrictiva"


# ═══════════════════════════════════════════════════════════════════════════
# 4 · RETRY-AFTER Y EL 429
# ═══════════════════════════════════════════════════════════════════════════

def test_retry_after_manda_sobre_todo():
    q = _sana()
    q.note_rate_limited(12.0)
    p = q.pages_paused_reason()
    assert p["reason"] == "RATE_LIMITED"
    assert p["retry_after_s"] == 12.0
    assert 11.0 <= p["seconds"] <= 12.0


def test_sin_retry_after_se_usa_el_reset_de_la_cabecera():
    q = QuotaGuard()
    q.note(remaining=0, limit=240, reset_seconds=8.0)
    q.note_rate_limited(None)
    assert q.pages_paused_reason()["retry_after_s"] == 8.0


def test_la_espera_del_429_no_puede_tirar_media_ventana_a_la_basura():
    """El suelo era de 30 s, heredado de suponer una ventana diaria."""
    q = _sana()
    q.note_rate_limited(0.5)
    assert q.pages_paused_reason()["seconds"] <= BURST_WINDOW_S + 0.1


def test_la_espera_del_429_nunca_supera_la_ventana_sostenida():
    q = _sana()
    q.note_rate_limited(9_999.0)
    assert q.pages_paused_reason()["seconds"] <= SUSTAINED_WINDOW_S


def test_el_429_para_los_DOS_carriles():
    q = _sana()
    assert q.budget_for_pages(8) > 0
    q.note_rate_limited(5.0)
    assert q.budget_for_pages(8) == 0
    assert q.burst_ceiling(8) == 0
    assert q.headroom(reserve=0) == 0
    assert q.snapshot()["rate_limited"] is True


def test_una_respuesta_suelta_NO_levanta_la_retencion_antes_de_tiempo():
    """Retry-After es una orden del proveedor, no una sugerencia."""
    q = _sana()
    q.note_rate_limited(10.0)
    q.note(remaining=200, limit=240, reset_seconds=60.0)
    assert q.snapshot()["rate_limited"] is True
    assert q.budget_for_pages(10) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5 · UN SOLO SITIO CUENTA, Y CUENTA TAMBIÉN LOS ERRORES
# ═══════════════════════════════════════════════════════════════════════════

def _cliente_src() -> str:
    import pathlib
    return pathlib.Path("app/providers/quantdata/client.py").read_text("utf-8")


def test_el_cliente_anota_la_peticion_ANTES_de_hacerla():
    src = _cliente_src()
    cuerpo = src.split("async def post(", 1)[1]
    assert "QUOTA.spend(1)" in cuerpo.split("await self._client.post(", 1)[0], (
        "una petición en vuelo ya ocupa sitio en la ventana deslizante")


def test_el_cliente_lee_las_CUATRO_cabeceras():
    src = _cliente_src()
    for cabecera in ("X-RateLimit-Remaining", "X-RateLimit-Limit",
                     "X-RateLimit-Reset", "Retry-After"):
        assert cabecera in src, cabecera


def test_las_cabeceras_se_adoptan_tambien_en_las_respuestas_de_ERROR():
    src = _cliente_src()
    cuerpo = src.split("async def post(", 1)[1]
    pos_note = cuerpo.index("QUOTA.note(remaining=remaining")
    assert pos_note < cuerpo.index("if response.status_code == 429:"), (
        "leerlas sólo en el camino feliz deja el presupuesto ciego cuando el "
        "proveedor está devolviendo errores")


def test_retry_after_llega_al_guardian_sin_intermediarios():
    cuerpo = _cliente_src().split("if response.status_code == 429:", 1)[1]
    assert "QUOTA.note_rate_limited(retry_after)" in cuerpo


def test_ningun_carril_cuenta_por_su_cuenta():
    """Contar dos veces la misma petición apretaba el presupuesto de páginas."""
    import pathlib
    for f in ("app/providers/quantdata/runtime.py",
              "app/providers/quantdata/intelligence.py"):
        txt = pathlib.Path(f).read_text("utf-8")
        cuerpo = "\n".join(l for l in txt.splitlines() if not l.strip().startswith("#"))
        assert "QUOTA.spend(" not in cuerpo, f"{f} vuelve a contar por su cuenta"


def test_el_snapshot_publica_el_contrato_y_lo_gastado():
    q = _sana()
    q.spend(3)
    c = q.snapshot()["contract"]
    assert c["sustained_limit"] == 240 and c["sustained_window_seconds"] == 60.0
    assert c["burst_limit"] == 20 and c["burst_window_seconds"] == 1.0
    assert c["burst_used"] == 3 and c["sustained_used"] == 3


def test_el_snapshot_es_json_estricto():
    import json
    q = _sana()
    q.note_rate_limited(3.0)
    json.dumps(q.snapshot(), allow_nan=False)


def test_la_pausa_coincide_SIEMPRE_con_el_presupuesto_real():
    """Decir «parado» y luego permitir pedir sería peor que no decir nada."""
    for gastadas in (0, 5, BURST_LIMIT - ENGINE_RESERVE - 1,
                     BURST_LIMIT - ENGINE_RESERVE, BURST_LIMIT):
        q = _sana()
        q.spend(gastadas)
        assert (q.pages_paused_reason() is not None) == (q.budget_for_pages(10) == 0), gastadas

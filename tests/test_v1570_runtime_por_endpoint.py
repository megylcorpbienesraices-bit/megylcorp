"""v1.57.0 · RUNTIME POR ENDPOINT · plazo medido, jitter, cortacircuitos, aislamiento.

Tres defectos del runtime de proveedores, los tres medibles:

1. UN SOLO PLAZO para las treinta y seis herramientas. Un plazo fijo se
   equivoca en las DOS direcciones: demasiado paciente con el endpoint que
   contesta en 200 ms —diez segundos para enterarse de que está muerto, turno
   robado a los sanos— y demasiado impaciente con el que tarda ocho.

2. BACKOFF SIN JITTER. Media docena de endpoints que falla en el mismo ciclo
   reintentaba EN EL MISMO INSTANTE contra la misma cuenta: exactamente la
   congestión que causó el fallo.

3. SIN CORTACIRCUITOS. Un `unavailable_until` plano: se espera y se reintenta,
   para siempre. El endpoint caído sigue gastando un turno de cada ronda.
"""
from __future__ import annotations

import random

import pytest

from app.core import endpoint_runtime as ER
from app.core.endpoint_runtime import (
    CLOSED, HALF_OPEN, OPEN, EndpointRegistry, EndpointRuntime, backoff_delay,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · PLAZO CALIBRADO CON LATENCIA REAL
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_muestra_suficiente_se_usa_el_plazo_configurado():
    """Calibrar con tres datos es peor que no calibrar."""
    rt = EndpointRuntime("x", default_timeout=10.0)
    assert rt.calibrated() is False
    assert rt.timeout() == pytest.approx(10.0)
    for _ in range(ER.MIN_SAMPLES_TO_CALIBRATE - 1):
        rt.record_success(0.2)
    assert rt.calibrated() is False
    assert rt.timeout() == pytest.approx(10.0)


def test_un_endpoint_rapido_deja_de_robar_diez_segundos_a_la_ronda():
    rt = EndpointRuntime("gex_by_strike", default_timeout=10.0)
    for x in (0.18, 0.21, 0.19, 0.25, 0.22, 0.20, 0.31, 0.24, 0.19, 0.23):
        rt.record_success(x)
    assert rt.calibrated() is True
    assert rt.timeout() < 3.0, "un endpoint de 200 ms no puede esperar 10 s a morirse"
    assert rt.snapshot()["timeout_source"] == "MEASURED_P95"


def test_un_endpoint_legitimamente_lento_no_se_corta_antes_de_tiempo():
    rt = EndpointRuntime("dark_flow", default_timeout=5.0)
    for _ in range(12):
        rt.record_success(7.5)
    assert rt.timeout() > 7.5, "cortarlo cuando iba a contestar gasta cuota dos veces"


def test_el_plazo_esta_acotado_por_arriba_y_por_abajo():
    lento = EndpointRuntime("lento", default_timeout=10.0)
    for _ in range(12):
        lento.record_success(500.0)
    assert lento.timeout() == pytest.approx(ER.TIMEOUT_CEILING_S)
    rapido = EndpointRuntime("rapido", default_timeout=10.0)
    for _ in range(12):
        rapido.record_success(0.001)
    assert rapido.timeout() == pytest.approx(ER.TIMEOUT_FLOOR_S)


def test_un_fallo_no_borra_las_latencias_medidas():
    """El plazo volvería al configurado justo cuando más falta hace conocerlo."""
    rt = EndpointRuntime("x", default_timeout=10.0)
    for _ in range(12):
        rt.record_success(0.3)
    antes = rt.timeout()
    rt.record_failure("timeout")
    assert rt.timeout() == pytest.approx(antes)
    assert rt.snapshot()["samples"] == 12


def test_la_ventana_de_latencias_no_crece_sin_limite():
    rt = EndpointRuntime("x")
    for i in range(ER.LATENCY_WINDOW * 3):
        rt.record_success(0.1 + i * 0.001)
    assert rt.snapshot()["samples"] == ER.LATENCY_WINDOW


def test_el_plazo_sigue_a_un_cambio_real_de_la_api():
    """Si el proveedor se degrada de verdad, el plazo tiene que acompañarlo."""
    rt = EndpointRuntime("x", default_timeout=10.0)
    for _ in range(ER.LATENCY_WINDOW):
        rt.record_success(0.2)
    rapido = rt.timeout()
    for _ in range(ER.LATENCY_WINDOW):
        rt.record_success(4.0)
    assert rt.timeout() > rapido * 2


# ═══════════════════════════════════════════════════════════════════════════
# 2 · BACKOFF CON JITTER
# ═══════════════════════════════════════════════════════════════════════════

def test_seis_endpoints_que_fallan_a_la_vez_no_reintentan_a_la_vez():
    r = random.Random(7)
    esperas = [backoff_delay(3, rng=r) for _ in range(6)]
    assert len(set(round(e, 6) for e in esperas)) >= 5, (
        "sin jitter los seis reintentan en el mismo instante contra la misma cuenta")


def test_el_jitter_respeta_el_techo_exponencial():
    r = random.Random(1)
    for n in range(1, 9):
        techo = min(ER.BACKOFF_CAP_S, ER.BACKOFF_BASE_S * (2 ** (n - 1)))
        for _ in range(60):
            d = backoff_delay(n, rng=r)
            assert 0.0 <= d <= techo


def test_la_espera_crece_con_los_fallos():
    r = random.Random(3)
    medias = [sum(backoff_delay(n, rng=r) for _ in range(400)) / 400 for n in (1, 3, 5)]
    assert medias[0] < medias[1] < medias[2]


def test_sin_fallos_no_hay_espera():
    assert backoff_delay(0) == 0.0
    assert backoff_delay(-3) == 0.0


def test_el_techo_esta_acotado():
    r = random.Random(5)
    assert max(backoff_delay(40, rng=r) for _ in range(200)) <= ER.BACKOFF_CAP_S


# ═══════════════════════════════════════════════════════════════════════════
# 3 · CORTACIRCUITOS
# ═══════════════════════════════════════════════════════════════════════════

def test_el_circuito_abre_tras_los_fallos_declarados():
    rt = EndpointRuntime("x")
    t = 1000.0
    for i in range(ER.FAILURE_THRESHOLD - 1):
        rt.record_failure(f"timeout {i}", now=t + i)
        assert rt.allow(t + i)[0] is True, "no se abre antes de tiempo"
    rt.record_failure("el que abre", now=t + 10)
    assert rt.snapshot(t + 10)["breaker"] == OPEN
    assert rt.allow(t + 10)[0] is False


def test_abierto_no_se_martillea_al_proveedor():
    rt = EndpointRuntime("x")
    t = 1000.0
    for i in range(ER.FAILURE_THRESHOLD):
        rt.record_failure("caido", now=t)
    for dt in (1, 5, 10, ER.OPEN_SECONDS - 1):
        assert rt.allow(t + dt)[0] is False


def test_vencida_la_apertura_pasa_UNA_sola_prueba():
    rt = EndpointRuntime("x")
    t = 1000.0
    for _ in range(ER.FAILURE_THRESHOLD):
        rt.record_failure("caido", now=t)
    t2 = t + ER.OPEN_SECONDS + 1
    ok, motivo = rt.allow(t2)
    assert ok is True and motivo == HALF_OPEN
    assert rt.allow(t2)[0] is False, "varias pruebas serían la avalancha que esto evita"


def test_si_la_prueba_falla_el_circuito_se_reabre_mas_tiempo():
    rt = EndpointRuntime("x")
    t = 1000.0
    for _ in range(ER.FAILURE_THRESHOLD):
        rt.record_failure("caido", now=t)
    v1 = rt.snapshot(t)["open_window_seconds"]
    t2 = t + ER.OPEN_SECONDS + 1
    rt.allow(t2)
    rt.record_failure("sigue caido", now=t2)
    s = rt.snapshot(t2)
    assert s["breaker"] == OPEN
    assert s["open_window_seconds"] > v1


def test_la_ventana_de_apertura_esta_acotada():
    rt = EndpointRuntime("x")
    t = 1000.0
    for _ in range(ER.FAILURE_THRESHOLD):
        rt.record_failure("caido", now=t)
    for _ in range(30):
        t += rt.snapshot(t)["open_window_seconds"] + 1
        rt.allow(t)
        rt.record_failure("sigue", now=t)
    assert rt.snapshot(t)["open_window_seconds"] <= ER.OPEN_SECONDS_MAX


def test_una_prueba_acertada_cierra_el_circuito_y_reinicia_la_ventana():
    rt = EndpointRuntime("x")
    t = 1000.0
    for _ in range(ER.FAILURE_THRESHOLD):
        rt.record_failure("caido", now=t)
    t2 = t + ER.OPEN_SECONDS + 1
    rt.allow(t2)
    rt.record_success(0.3, now=t2)
    s = rt.snapshot(t2)
    assert s["breaker"] == CLOSED
    assert s["consecutive_failures"] == 0
    assert s["open_window_seconds"] == pytest.approx(ER.OPEN_SECONDS)


def test_el_circuito_abierto_deja_de_LLAMAR_no_de_MOSTRAR():
    """Es la distinción que importa: el último valor bueno se sigue publicando."""
    rt = EndpointRuntime("x")
    t = 1000.0
    for _ in range(ER.FAILURE_THRESHOLD):
        rt.record_failure("caido", now=t)
    s = rt.snapshot(t)
    assert s["breaker"] == OPEN
    assert s["serving_last_known_good"] is True


# ═══════════════════════════════════════════════════════════════════════════
# 4 · AISLAMIENTO
# ═══════════════════════════════════════════════════════════════════════════

def test_un_endpoint_caido_no_toca_el_estado_de_ningun_otro():
    from app.providers.quantdata.tools import build_catalog
    reg = EndpointRegistry(default_timeout=10.0)
    claves = list(build_catalog().keys())
    assert len(claves) > 20
    for k in claves:
        for _ in range(12):
            reg.get(k).record_success(0.25)
    t = 2000.0
    for _ in range(ER.FAILURE_THRESHOLD):
        reg.get("dark_flow").record_failure("timeout", now=t)

    snap = reg.snapshot(t)
    assert snap["open"] == ["dark_flow"]
    for k in claves:
        if k == "dark_flow":
            continue
        assert reg.get(k).allow(t)[0] is True, f"{k} se contaminó"
        assert reg.get(k).snapshot(t)["consecutive_failures"] == 0


def test_el_registro_publica_la_politica_para_poder_discutirla():
    reg = EndpointRegistry()
    reg.get("a").record_success(0.2)
    pol = reg.snapshot()["policy"]
    for pieza in ("p95", "jitter", "circuito"):
        assert pieza in pol


# ═══════════════════════════════════════════════════════════════════════════
# 5 · CABLEADO AL FETCH REAL
# ═══════════════════════════════════════════════════════════════════════════

def test_el_fetch_pregunta_al_cortacircuitos_antes_de_llamar():
    from pathlib import Path
    src = Path("app/providers/quantdata/intelligence.py").read_text(encoding="utf-8")
    assert "permitido, motivo = rt.allow()" in src
    assert "if not permitido:" in src
    assert src.index("rt.allow()") < src.index("for path in tool.candidates():")


def test_el_fetch_usa_el_plazo_medido_y_registra_la_latencia():
    """v1.58.1 · el plazo lo fija el registro, y viaja con la PETICIÓN.

    Antes se recalculaba aquí (`_rt.timeout() if _rt.calibrated() else
    configurado + 1`) y sólo gobernaba el reloj del ciclo. Ver
    `tests/test_v1581_plazo_del_transporte.py`.
    """
    from pathlib import Path
    src = Path("app/providers/quantdata/intelligence.py").read_text(encoding="utf-8")
    assert "_plazo = _rt.timeout()" in src
    assert "timeout_s=_plazo + CHANNEL_SLACK_S," in src
    assert "_client.post(path, body, timeout=_plazo)" in src
    assert "_rt.record_success(time.monotonic() - _t0)" in src
    assert "_rt.record_failure(detail, status=STATUS_TRANSIENT)" in src


def test_un_cuerpo_invalido_no_abre_el_circuito():
    """Un 400 es culpa del cuerpo. Reintentarlo menos no lo arregla."""
    from pathlib import Path
    src = Path("app/providers/quantdata/intelligence.py").read_text(encoding="utf-8")
    assert "if status != STATUS_REQUEST_INVALID:" in src

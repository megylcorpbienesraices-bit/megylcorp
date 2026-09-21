"""v1.57.0 · «SIN INTENTOS» NO ERA DEL PROVEEDOR: ERA NUESTRO.

El Auditor enseñaba una docena de herramientas en PENDIENTE con el detalle
«sin intentos · 1 rutas candidatas», y la cuota en «15/34 herramientas». Leído
así parece un problema de autorización o de endpoints que no existen.

No lo era. El lote de cada ciclo se elegía con un orden ESTRICTO por prioridad y
un corte por presupuesto:

    batch = sorted(due, key=lambda t: (_PRIORITY[t.key], ultimo_fetch))[:presupuesto]

Con cuota corta el presupuesto no da para todas, y como el orden es estricto una
herramienta de la cola no entra mientras quede una de cabeza pendiente. Las de
cabeza vencen en cada ciclo. Las de cola no entraban NUNCA.

Medido sobre el catálogo real: 26 de 36 herramientas sin un solo intento en 200
ciclos. El proveedor jamás llegó a saber que existían.
"""
from __future__ import annotations

import pytest

from app.providers.quantdata.intelligence import (
    AGING_FLOOR, AGING_SECONDS, _PRIORITY, _PRIORITY_DEFAULT, effective_priority,
)
from app.providers.quantdata.tools import build_catalog

CLAVES = list(build_catalog().keys())


def _simular(*, presupuesto=6, ciclos=200, dt=3.0, con_envejecimiento=True):
    """Reproduce el selector real y devuelve en qué ciclo se sirvió cada una."""
    fetched = {k: 0.0 for k in CLAVES}
    primera: dict[str, int] = {}
    for c in range(1, ciclos + 1):
        now = c * dt
        if con_envejecimiento:
            clave = lambda k: (effective_priority(k, now - fetched[k]), fetched[k])
        else:
            clave = lambda k: (_PRIORITY.get(k, _PRIORITY_DEFAULT), fetched[k])
        for k in sorted(CLAVES, key=clave)[:presupuesto]:
            fetched[k] = now
            primera.setdefault(k, c)
    return primera


# ═══════════════════════════════════════════════════════════════════════════
# 1 · NADIE SE QUEDA FUERA PARA SIEMPRE
# ═══════════════════════════════════════════════════════════════════════════

def test_ninguna_herramienta_se_queda_sin_intentar():
    servidas = _simular()
    nunca = sorted(set(CLAVES) - set(servidas))
    assert not nunca, (
        f"{len(nunca)} herramientas sin un solo intento; el proveedor nunca "
        f"llega a saber que existen: {nunca[:8]}")


def test_el_orden_estricto_SI_mataba_de_hambre_a_la_cola():
    """La prueba de que el defecto era real y no una precaución teórica."""
    servidas = _simular(con_envejecimiento=False)
    nunca = set(CLAVES) - set(servidas)
    assert len(nunca) >= 20, (
        "si el orden estricto ya no mata de hambre, esta prueba sobra")


@pytest.mark.parametrize("presupuesto", [3, 6, 10])
def test_aguanta_presupuestos_distintos(presupuesto):
    """La cuota cambia con la cuenta y con la hora. Ninguna debe morir de hambre."""
    servidas = _simular(presupuesto=presupuesto, ciclos=400)
    assert set(servidas) == set(CLAVES), f"presupuesto {presupuesto}: quedan sin servir"


# ═══════════════════════════════════════════════════════════════════════════
# 2 · PERO LO CRÍTICO SIGUE PRIMERO
# ═══════════════════════════════════════════════════════════════════════════

def test_lo_que_dibuja_el_grafico_se_sirve_en_el_primer_ciclo():
    """Corregir el hambre no puede costar la latencia del cambio de activo."""
    servidas = _simular()
    criticas = [k for k in CLAVES if _PRIORITY.get(k, _PRIORITY_DEFAULT) == 0]
    assert criticas, "el catálogo tiene que declarar su clase crítica"
    for k in criticas:
        assert servidas[k] == 1, f"{k} es crítica y no entró en el primer ciclo"


def test_el_orden_entre_clases_se_respeta():
    servidas = _simular()
    peor = {}
    for k in CLAVES:
        p = _PRIORITY.get(k, _PRIORITY_DEFAULT)
        peor[p] = max(peor.get(p, 0), servidas[k])
    clases = sorted(peor)
    for a, b in zip(clases, clases[1:]):
        assert peor[a] <= peor[b], (
            f"la clase {a} terminó de servirse después que la {b}")


def test_una_herramienta_recien_servida_conserva_su_prioridad():
    for k in CLAVES:
        base = _PRIORITY.get(k, _PRIORITY_DEFAULT)
        assert effective_priority(k, 0.0) == base
        assert effective_priority(k, AGING_SECONDS * 0.99) == base


# ═══════════════════════════════════════════════════════════════════════════
# 3 · LA REGLA DE ENVEJECIMIENTO, SOLA
# ═══════════════════════════════════════════════════════════════════════════

def test_cada_ventana_de_espera_gana_un_puesto():
    k = next(k for k in CLAVES if _PRIORITY.get(k, _PRIORITY_DEFAULT) == 3)
    assert effective_priority(k, AGING_SECONDS * 1) == 2
    assert effective_priority(k, AGING_SECONDS * 2) == 1
    assert effective_priority(k, AGING_SECONDS * 3) == 0


def test_nada_sube_por_encima_de_la_clase_critica():
    """La exposición no puede perder su turno frente a las noticias."""
    for k in CLAVES:
        assert effective_priority(k, 10_000_000.0) >= AGING_FLOOR


def test_una_critica_no_se_degrada_ni_se_mejora():
    criticas = [k for k in CLAVES if _PRIORITY.get(k, _PRIORITY_DEFAULT) <= AGING_FLOOR]
    for k in criticas:
        for espera in (0.0, AGING_SECONDS * 50):
            assert effective_priority(k, espera) == _PRIORITY.get(k, _PRIORITY_DEFAULT)


def test_una_espera_absurda_no_rompe_la_regla():
    k = next(k for k in CLAVES if _PRIORITY.get(k, _PRIORITY_DEFAULT) == 3)
    for espera in (-5.0, 0.0, None):
        assert effective_priority(k, espera or 0.0) == 3


def test_el_selector_real_usa_la_regla():
    from pathlib import Path
    src = Path("app/providers/quantdata/intelligence.py").read_text(encoding="utf-8")
    assert "key=lambda t: (effective_priority(" in src, (
        "el lote tiene que elegirse con la prioridad envejecida")

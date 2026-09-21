"""v1.57.4 · AL ABRIR EL PROGRAMA NO HABÍA NINGUNA RÁFAGA.

La ráfaga de arranque estaba armada SÓLO en `select_asset`. Al abrir la terminal
—el único momento en el que el operador está mirando la pantalla vacía— el
carril de páginas salía con el presupuesto de régimen: cuatro herramientas por
ciclo, ciclos de quince segundos, concurrencia dos.

Treinta y seis herramientas a ese ritmo son NUEVE CICLOS. Más de dos minutos.

Y encima el contrato lo pagaba de sobra: 240 peticiones por 60 s dan para
hidratar el catálogo entero en segundos. No era una limitación del proveedor;
era que nadie había armado la ráfaga en el único sitio donde más falta hacía.

Tres frenos, los tres nuestros:

    1 · sin ráfaga al arrancar         → 4 por ciclo en vez de 8 por segundo
    2 · presupuesto «nunca vista» = 4  → se trataba igual que telemetría rancia
    3 · la pantalla preguntaba cada 6 s y el diagnóstico cada 15 s
"""
from __future__ import annotations

import pytest

from app.providers.quantdata.intelligence import (
    BURST_CONCURRENCY, BURST_CYCLE_SECONDS, BURST_SECONDS, QuantDataIntelligence,
    effective_priority,
)
from app.providers.quantdata.shared import (
    BURST_LIMIT, BURST_WINDOW_S, ENGINE_RESERVE, QuotaGuard,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · LA RÁFAGA SE ARMA AL ARRANCAR
# ═══════════════════════════════════════════════════════════════════════════

def test_el_arranque_arma_la_rafaga(monkeypatch):
    """Con una clave válida, abrir el programa tiene que armar la ráfaga."""
    import asyncio
    import app.providers.quantdata.intelligence as I

    monkeypatch.setenv("QUANTDATA_API_KEY", "qd_" + "0" * 32)
    monkeypatch.setenv("QUANTDATA_ENABLED", "1")

    class _ClienteMudo:
        def __init__(self, *a, **k): pass
        async def start(self): pass
        async def close(self): pass

    monkeypatch.setattr(I, "QuantDataClient", _ClienteMudo)

    m = I.QuantDataIntelligence()
    assert not m._bursting(), "recién construido no hay ráfaga todavía"

    async def _abrir():
        await m.start("DIA")
        # El bucle no debe correr durante la prueba: sólo interesa el estado que
        # deja `start`.
        if m._task is not None:
            m._task.cancel()
            try:
                await m._task
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_abrir())
    assert m.settings.configured, "la clave de prueba no pasó la validación"
    assert m._bursting(), "al abrir el programa no había ráfaga"
    assert m._arranque_en_frio is True


def test_el_cambio_de_activo_NO_es_un_arranque_en_frio():
    """En un cambio la pantalla ya tiene forma: sólo urge lo que dibuja."""
    import asyncio
    m = QuantDataIntelligence()
    m._arranque_en_frio = True
    asyncio.run(m.select_asset("QQQ"))
    assert m._bursting()
    assert m._arranque_en_frio is False


def test_en_frio_la_rafaga_cubre_el_catalogo_entero():
    """Guardia de código: la clase de prioridad no puede excluir a nadie en frío."""
    import pathlib
    src = pathlib.Path("app/providers/quantdata/intelligence.py").read_text("utf-8")
    cuerpo = src.split("burst = self._bursting()", 1)[1].split("batch = sorted", 1)[0]
    assert "self._arranque_en_frio" in cuerpo, (
        "la ráfaga de arranque volvió a filtrar como si fuera un cambio de activo")


# ═══════════════════════════════════════════════════════════════════════════
# 2 · EL TAMAÑO DE LA RÁFAGA SALE DEL CONTRATO
# ═══════════════════════════════════════════════════════════════════════════

def test_la_concurrencia_es_exactamente_lo_que_el_contrato_deja():
    assert BURST_CONCURRENCY == BURST_LIMIT - ENGINE_RESERVE == 8


def test_el_ciclo_de_la_rafaga_es_la_ventana_de_rafaga():
    assert BURST_CYCLE_SECONDS == BURST_WINDOW_S == 1.0


def test_la_rafaga_entera_cabe_en_el_contrato():
    """25 s de ráfaga a 8 por segundo no pueden romper las 240/60 s."""
    peticiones = BURST_CONCURRENCY * (BURST_SECONDS / BURST_CYCLE_SECONDS)
    assert peticiones <= 240, f"{peticiones:.0f} peticiones en la ráfaga"


def test_el_presupuesto_en_frio_es_el_del_contrato_no_el_de_regimen():
    q = QuotaGuard()                      # nunca se vio una cabecera
    assert q.budget_for_pages(36) == BURST_LIMIT - ENGINE_RESERVE


def test_la_telemetria_RANCIA_sigue_avanzando_despacio():
    """No se relaja el caso que sí lo merece: otra instancia pudo gastar."""
    import time
    q = QuotaGuard()
    q.note(remaining=240, limit=240, reset_seconds=60.0)
    q.updated_at = time.time() - 300.0
    assert q.budget_for_pages(36) == 4


# ═══════════════════════════════════════════════════════════════════════════
# 3 · CUÁNTO TARDA DE VERDAD, MEDIDO
# ═══════════════════════════════════════════════════════════════════════════

def _avanzar(q: QuotaGuard, segundos: float) -> None:
    """Envejece el contador local sin dormir.

    El guardián mide con el reloj real; la simulación avanza por ciclos. Se
    retrasan los sellos en vez de esperar, que es lo mismo para una ventana
    deslizante y no hace la prueba dependiente de la velocidad de la máquina.
    """
    from collections import deque
    for v in (q._sostenida, q._rafaga):
        v._sellos = deque(t - segundos for t in v._sellos)


def _simular_arranque(*, con_rafaga: bool, tope_regimen: int | None = None) -> float:
    """Reproduce el selector real y devuelve los segundos hasta tener todo.

    No mide reloj de pared: cuenta ciclos del programador con su presupuesto y
    su cadencia, que es lo que decide la espera del operador.
    """
    m = QuantDataIntelligence()
    claves = list(m.catalog.keys())
    pendientes = set(claves)
    q = QuotaGuard()
    t = 0.0
    while pendientes and t < 600.0:
        if con_rafaga and t < BURST_SECONDS:
            presupuesto = q.burst_ceiling(len(pendientes))
            paso = BURST_CYCLE_SECONDS
        else:
            presupuesto = q.budget_for_pages(len(pendientes))
            if tope_regimen is not None:
                presupuesto = min(presupuesto, tope_regimen)
            paso = 15.0
        orden = sorted(pendientes, key=lambda k: effective_priority(k, 0.0))
        for k in orden[:presupuesto]:
            pendientes.discard(k)
        q.spend(presupuesto)
        t += paso
        _avanzar(q, paso)
    return t


def test_con_rafaga_el_catalogo_entero_esta_en_segundos():
    segundos = _simular_arranque(con_rafaga=True)
    assert segundos <= 10.0, f"{segundos:.0f} s hasta tener las 36 herramientas"


def test_como_estaba_tardaba_MAS_DE_DOS_MINUTOS():
    """La prueba de que el defecto era real y no una mejora cosmética.

    Tal y como estaba: sin ráfaga y con el presupuesto de «telemetría dudosa»
    aplicado también al primer ciclo del proceso —cuatro herramientas por ciclo
    de quince segundos—.
    """
    segundos = _simular_arranque(con_rafaga=False, tope_regimen=4)
    assert segundos >= 120.0, (
        f"{segundos:.0f} s; si esto ya fuera rápido, la corrección no haría falta")


def test_sin_rafaga_sigue_siendo_cosa_de_minutos():
    """Sólo con arreglar el presupuesto no bastaba: hacía falta la ráfaga."""
    segundos = _simular_arranque(con_rafaga=False)
    assert segundos >= 60.0, f"{segundos:.0f} s"


def test_la_rafaga_es_al_menos_diez_veces_mas_rapida():
    con = _simular_arranque(con_rafaga=True)
    antes = _simular_arranque(con_rafaga=False, tope_regimen=4)
    assert antes / max(con, 1e-9) >= 10.0, f"{antes:.0f} s → {con:.0f} s"


# ═══════════════════════════════════════════════════════════════════════════
# 4 · LA PANTALLA PREGUNTA MÁS DEPRISA MIENTRAS ARRANCA
# ═══════════════════════════════════════════════════════════════════════════

def _js() -> str:
    import pathlib
    return pathlib.Path("app/static/itmq_app.js").read_text("utf-8")


def test_el_primer_minuto_tiene_su_propia_cadencia():
    js = _js()
    assert "ARRANQUE_MS" in js, "la pantalla volvió a preguntar cada 6 s desde el segundo cero"
    assert "cadencia(pullBundle, 1500, 6000" in js
    assert "cadencia(pullDiagnostics, 4000, 15000" in js


def test_la_cadencia_rapida_CADUCA_sola():
    """Acelerar para siempre sería otra clase de defecto."""
    js = _js()
    trozo = js.split("const ARRANQUE_MS", 1)[1].split("cadencia(pullBundle", 1)[0]
    assert "Date.now() - abierto" in trozo
    assert "< ARRANQUE_MS" in trozo


def test_la_aceleracion_no_toca_la_cuota_del_proveedor():
    """Son llamadas al propio servidor de la terminal, no al proveedor."""
    js = _js()
    trozo = js.split("const ARRANQUE_MS", 1)[1].split("window.addEventListener", 1)[0]
    for prohibido in ("quantdata", "fetchProvider", "X-RateLimit"):
        assert prohibido not in trozo


def test_la_pestaña_oculta_sigue_sin_preguntar():
    js = _js()
    trozo = js.split("const cadencia = ", 1)[1].split("cadencia(pullBundle", 1)[0]
    assert "document.visibilityState === 'visible'" in trozo


# ═══════════════════════════════════════════════════════════════════════════
# 5 · LA LIMPIEZA BORRA CACHÉS, NO TRABAJO
# ═══════════════════════════════════════════════════════════════════════════

def _limpiar() -> str:
    import pathlib
    p = pathlib.Path("LIMPIAR.bat")
    assert p.exists(), "hace falta una forma de limpiar sin borrar nada del programa"
    crudo = p.read_bytes()
    assert b"\r\n" in crudo and b"\n" not in crudo.replace(b"\r\n", b""), "CRLF"
    return p.read_text("utf-8", errors="replace")


def test_solo_borra_lo_que_se_regenera_solo():
    txt = _limpiar()
    for cache in ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
                  "*.pyc", "*.pyo"):
        assert cache in txt, cache


#: Lo único que LIMPIAR.bat tiene permitido borrar. Todo lo demás es trabajo:
#: el entorno cuesta minutos de reinstalación, `app\storage` es la memoria
#: cuantitativa y el histórico de sesiones, `.env` son las credenciales, y los
#: logs y los datos de mercado no se regeneran solos NUNCA.
BORRABLE = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
            "*.pyc", "*.pyo")

INTOCABLE = (".venv", "storage", ".env", "logs", "data", "app", "rust",
             "frontend", "scripts", "tests", "docs", "deploy")


def _ordenes_de_borrado(txt: str) -> list[str]:
    import re
    return [l for l in txt.splitlines()
            if re.search(r"\b(rd|del|rmdir|erase|format)\b", l, re.I)
            and not l.strip().upper().startswith("REM")]


def test_NUNCA_toca_el_entorno_ni_la_memoria_ni_las_credenciales():
    """Un limpiador que borra de más es peor que no limpiar."""
    ordenes = _ordenes_de_borrado(_limpiar())
    assert ordenes, "el limpiador dejó de borrar nada"
    for orden in ordenes:
        for intocable in INTOCABLE:
            assert intocable not in orden, (intocable, orden)


def test_cada_orden_de_borrado_apunta_a_algo_de_la_lista_BLANCA():
    """No basta con prohibir: cada orden tiene que estar explícitamente permitida.

    Prohibir por lista negra deja la puerta abierta a lo que nadie previó. Aquí
    se exige lo contrario: si una orden no nombra uno de los seis patrones
    regenerables, la prueba falla aunque sea inofensiva.
    """
    for orden in _ordenes_de_borrado(_limpiar()):
        assert any(b in orden for b in BORRABLE), (
            f"orden de borrado fuera de la lista blanca: {orden!r}")


def test_los_seis_patrones_regenerables_siguen_cubiertos():
    txt = _limpiar()
    for b in BORRABLE:
        assert b in txt, b


def test_borra_por_patron_cerrado_no_por_comodin_abierto():
    import re
    txt = _limpiar()
    for l in txt.splitlines():
        if l.strip().upper().startswith("REM"):
            continue
        assert not re.search(r"\b(rd|rmdir)\s+/s\s+/q\s+\"?[A-Za-z]:", l), l
        assert not re.search(r"del\s+/s\s+/q\s+\*\.\*", l), l


def test_el_bat_es_de_windows():
    txt = _limpiar()
    for linux in ("/dev/null", "export ", "#!/bin/", "&&", "$(", "source "):
        assert linux not in txt, f"«{linux}» no existe en Windows"
    assert "pause" in txt


def test_no_queda_la_nota_suelta_de_la_auditoria():
    """`AUDIT_FIX.md` era una nota de una pasada concreta, ya en el CHANGELOG."""
    import pathlib
    assert not pathlib.Path("AUDIT_FIX.md").exists()
    changelog = pathlib.Path("CHANGELOG_v1.56.0.md").read_text("utf-8")
    assert "df2ca9d" in changelog, "el contenido tiene que seguir estando en algún sitio"


# ═══════════════════════════════════════════════════════════════════════════
# 6 · EL ARRANQUE EN FRÍO, ENTERO, CONTRA EL CONTRATO
#
# Las pruebas de arriba miden cada pieza por separado. Ésta es la que el
# operador pidió: los DOS carriles a la vez, desde el segundo cero, con el
# catálogo completo, y la comprobación de que ninguna ventana del contrato se
# rompe en ningún instante del arranque.
#
# Es la regresión que impide las dos recaídas posibles:
#
#   · volver a los ~135 s porque alguien desarme la ráfaga o baje el
#     presupuesto del primer ciclo;
#   · ganar velocidad rompiendo el contrato, que es peor que ir lento porque
#     se paga con 429 y con los dos carriles parados.
# ═══════════════════════════════════════════════════════════════════════════

from app.providers.quantdata.shared import (  # noqa: E402
    ENGINE_FAST_JOBS, ENGINE_SLOW_JOBS, SUSTAINED_LIMIT, SUSTAINED_WINDOW_S,
)


def _pico(sellos: list[float], ventana: float) -> int:
    """Máximo de peticiones que llegan a convivir en una ventana deslizante.

    Se evalúa en cada petición, que es donde el máximo puede ocurrir: si una
    ventana llega a contener N, hay una petición que es la última de esas N.
    """
    orden = sorted(sellos)
    pico, i = 0, 0
    for j, t in enumerate(orden):
        while orden[i] <= t - ventana:
            i += 1
        pico = max(pico, j - i + 1)
    return pico


def _arranque_completo(*, segundos: float = 120.0) -> dict:
    """Los dos carriles desde el segundo cero, con el guardián real decidiendo.

    Devuelve los sellos de tiempo virtuales de TODAS las peticiones y el
    instante en que el catálogo quedó hidratado.
    """
    m = QuantDataIntelligence()
    pendientes = set(m.catalog.keys())
    q = QuotaGuard()
    sellos: list[float] = []
    hidratado: float | None = None

    t = 0.0
    prox_paginas = 0.0
    prox_motor = 0.0
    paso = 0.5
    ciclo_motor = 0
    while t <= segundos:
        # ── carril del motor: 4 rápidos por ciclo, más el bloque estructural
        #    en el primero. Su ritmo lo fija el propio guardián.
        if t >= prox_motor:
            n = len(ENGINE_FAST_JOBS) + (len(ENGINE_SLOW_JOBS) if ciclo_motor == 0 else 0)
            sellos.extend([t] * n)
            q.spend(n)
            ciclo_motor += 1
            prox_motor = t + q.recommended_interval(len(ENGINE_FAST_JOBS))

        # ── carril de páginas: ráfaga mientras dura, régimen después
        if pendientes and t >= prox_paginas:
            en_rafaga = t < BURST_SECONDS
            presupuesto = (q.burst_ceiling(len(pendientes)) if en_rafaga
                           else q.budget_for_pages(len(pendientes)))
            if presupuesto > 0:
                orden = sorted(pendientes, key=lambda k: effective_priority(k, 0.0))
                for k in orden[:presupuesto]:
                    pendientes.discard(k)
                sellos.extend([t] * presupuesto)
                q.spend(presupuesto)
            prox_paginas = t + (BURST_CYCLE_SECONDS if en_rafaga else 15.0)
            if not pendientes and hidratado is None:
                hidratado = t

        t += paso
        _avanzar(q, paso)

    return {"sellos": sellos, "hidratado": hidratado, "pendientes": pendientes}


def test_el_catalogo_COMPLETO_se_hidrata_en_frio_en_segundos():
    r = _arranque_completo()
    assert not r["pendientes"], f"quedaron sin servir: {sorted(r['pendientes'])[:6]}"
    assert r["hidratado"] is not None
    assert r["hidratado"] <= 15.0, (
        f"{r['hidratado']:.1f} s hasta el catálogo entero; la ráfaga de arranque "
        "volvió a desarmarse o el presupuesto del primer ciclo volvió a ser el "
        "de la telemetría rancia")


def test_el_arranque_NO_supera_las_240_por_60_segundos():
    r = _arranque_completo()
    pico = _pico(r["sellos"], SUSTAINED_WINDOW_S)
    assert pico <= SUSTAINED_LIMIT, (
        f"{pico} peticiones llegaron a convivir en 60 s sobre un tope de "
        f"{SUSTAINED_LIMIT}: el arranque rompe la ventana deslizante")


def test_el_arranque_NO_supera_las_20_por_segundo():
    r = _arranque_completo()
    pico = _pico(r["sellos"], BURST_WINDOW_S)
    assert pico <= BURST_LIMIT, (
        f"{pico} peticiones en un mismo segundo sobre un tope de {BURST_LIMIT}: "
        "el arranque rompe la ráfaga del contrato")


def test_el_arranque_deja_sitio_al_carril_del_motor():
    """Hidratar deprisa no puede hacerse a costa de la estructura."""
    r = _arranque_completo()
    pico = _pico(r["sellos"], BURST_WINDOW_S)
    assert pico <= BURST_LIMIT, pico
    # En el peor segundo tiene que caber, además, un ciclo rápido del motor.
    holgura = BURST_LIMIT - pico
    assert holgura >= 0, holgura


def test_la_rafaga_se_apaga_y_el_regimen_NO_machaca_al_proveedor():
    """Pasado el arranque, el consumo vuelve a ser una fracción del contrato."""
    r = _arranque_completo(segundos=180.0)
    tardios = [s for s in r["sellos"] if s >= BURST_SECONDS + SUSTAINED_WINDOW_S]
    assert tardios, "la simulación no llegó al régimen permanente"
    pico = _pico(tardios, SUSTAINED_WINDOW_S)
    assert pico <= SUSTAINED_LIMIT * 0.25, (
        f"{pico} de {SUSTAINED_LIMIT} en régimen: la ráfaga no se apagó")


def test_la_simulacion_seria_ROJA_con_el_comportamiento_viejo():
    """Sin esto, la prueba de arriba podría pasar por accidente.

    Con el arranque tal y como estaba —sin ráfaga y con cuatro por ciclo de
    quince segundos— el catálogo NO está hidratado a los quince segundos.
    """
    segundos = _simular_arranque(con_rafaga=False, tope_regimen=4)
    assert segundos > 15.0, (
        f"{segundos:.0f} s; si el comportamiento viejo ya cumpliera el umbral, "
        "la regresión no estaría probando nada")

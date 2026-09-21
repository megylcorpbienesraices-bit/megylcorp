"""PRUEBAS LIVE · DIA · SPY · QQQ.  Se SALTAN salvo que haya instancia real.

═══════════════════════════════════════════════════════════════════════════
CÓMO SE EJECUTAN
═══════════════════════════════════════════════════════════════════════════

    export ITMQ_LIVE_TESTS=1
    export ITMQ_BASE_URL=http://127.0.0.1:8000     # opcional, éste es el valor por defecto
    python -m pytest tests/test_v1570_live_dia_spy_qqq.py -v

Con la terminal levantada y `QUANTDATA_API_KEY` puesta. Sin `ITMQ_LIVE_TESTS=1`
se SALTAN: una suite que falla en el portátil de quien no tiene credenciales no
protege nada, sólo enseña a ignorar el rojo.

═══════════════════════════════════════════════════════════════════════════
QUÉ COMPRUEBAN, Y QUÉ NO
═══════════════════════════════════════════════════════════════════════════

NO comprueban que haya actividad: un martes a las 03:00 no hay flujo de
opciones y eso es correcto. Comprueban que el sistema DIGA LA VERDAD sobre lo
que tiene, y que los invariantes matemáticos se cumplan sobre el dato real:

  · la presión delta del dealer cuadra operación a operación con la cinta
  · los muros salen de la autoridad única y declaran cómo se puntuó su strike
  · capacidad y ejecución no se confunden
  · un carril con dato bueno no se publica como sección rota
  · el Auditor clasifica cada herramienta pendiente con su causa exacta
  · el plazo de cada endpoint se calibra con su latencia real
"""
from __future__ import annotations

import os

import pytest

ACTIVOS = ["DIA", "SPY", "QQQ"]
BASE = os.getenv("ITMQ_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

def _live_habilitado() -> bool:
    return os.getenv("ITMQ_LIVE_TESTS") == "1"


def _get(path: str, timeout: float = 30.0):
    import httpx
    r = httpx.get(f"{BASE}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def _set_symbol(sym: str):
    import httpx
    httpx.post(f"{BASE}/api/asset", json={"symbol": sym}, timeout=30.0).raise_for_status()
    # El cambio de símbolo es transaccional: se espera a que el bundle sea del nuevo.
    import time
    for _ in range(40):
        b = _get("/api/terminal/bundle")
        if str(b.get("symbol") or "").upper() == sym:
            return b
        time.sleep(1.0)
    pytest.fail(f"la terminal no cambió a {sym} en 40 s")


@pytest.fixture(scope="module", params=ACTIVOS)
def bundle(request):
    """El bundle del activo, o se salta si no hay instancia.

    El salto vive AQUÍ y no en un `pytestmark` de módulo a propósito: el
    proyecto prohíbe aparcar un fichero entero como saltado, y con razón —así
    es como una suite se muere sin que nadie se entere—. Cada prueba declara
    que depende de una terminal viva, y se salta por esa dependencia concreta,
    no por una marca global.
    """
    if not _live_habilitado():
        pytest.skip("LIVE: exporta ITMQ_LIVE_TESTS=1 con la terminal levantada "
                    "y QUANTDATA_API_KEY puesta")
    return _set_symbol(request.param)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · DELTA / MIN sobre la cinta real
# ═══════════════════════════════════════════════════════════════════════════

def test_delta_min_cuadra_operacion_a_operacion(bundle):
    """La suma de los minutos tiene que reproducir el stock reconstruido a mano."""
    dm = (bundle.get("flujo_ordenes") or {}).get("delta_min") or {}
    if not dm.get("ready"):
        pytest.skip(f"sin presión delta medible: {dm.get('detail')}")
    total = sum(float(s["dealer_delta_shares"]) for s in dm["series"])
    assert abs(total - float(dm["total_dealer_delta_shares"])) < 1e-6


def test_delta_min_publica_su_cobertura(bundle):
    """Sin la cifra, un carril que enseña el 40 % del flujo parece un mercado tranquilo."""
    dm = (bundle.get("flujo_ordenes") or {}).get("delta_min") or {}
    cob = dm.get("coverage") or {}
    assert "pct" in cob and "total" in cob and "counted" in cob
    if cob.get("total"):
        assert cob["counted"] <= cob["total"]
        assert 0.0 <= float(cob["pct"]) <= 100.0


def test_delta_min_no_depende_de_la_cadena(bundle):
    """Si hay prints, tiene que haber DELTA/MIN, hidrate o no la cadena."""
    flujo = bundle.get("flujo_ordenes") or {}
    prints = int(((flujo.get("order_flow_unconsolidated") or {}).get("count")) or 0)
    dm = flujo.get("delta_min") or {}
    if prints > 0 and (dm.get("coverage") or {}).get("counted"):
        assert dm.get("ready") is True, (
            f"{prints} prints en la cinta y el carril vacío: hay una dependencia "
            f"que no debería existir")


def test_los_dolares_son_las_acciones_por_el_spot(bundle):
    dm = (bundle.get("flujo_ordenes") or {}).get("delta_min") or {}
    if not dm.get("ready"):
        pytest.skip("sin serie")
    for s in dm["series"]:
        if s.get("dealer_delta_dollars") is None:
            continue
        acc = float(s["dealer_delta_shares"])
        if acc == 0:
            continue
        spot = float(s["dealer_delta_dollars"]) / acc
        assert 1.0 < spot < 100_000.0, f"spot implícito imposible: {spot}"


# ═══════════════════════════════════════════════════════════════════════════
# 2 · MUROS sobre la cadena real
# ═══════════════════════════════════════════════════════════════════════════

def test_los_muros_declaran_como_se_puntuo_SU_strike(bundle):
    aud = ((bundle.get("auditor") or {}).get("wall_consistency") or {})
    sides = ((aud.get("audit") or {}).get("sides")) or aud.get("sides") or {}
    if not sides:
        pytest.skip("sin auditoría de muros en este ciclo")
    for lado, v in sides.items():
        if v.get("selected_strike") is None:
            continue
        assert v.get("method"), f"{lado}: sin método declarado"
        if v.get("open_interest") is None:
            assert v.get("method") == "exposure-only-oi-missing", (
                f"{lado}: sin interés abierto y declara «{v.get('method')}»")


def test_el_muro_es_DERIVED_nunca_dato_del_proveedor(bundle):
    aud = ((bundle.get("auditor") or {}).get("wall_consistency") or {})
    sides = ((aud.get("audit") or {}).get("sides")) or aud.get("sides") or {}
    for lado, v in sides.items():
        if v.get("selected_strike") is not None:
            assert v.get("source_mode") == "DERIVED", f"{lado}: {v.get('source_mode')}"


# ═══════════════════════════════════════════════════════════════════════════
# 3 · CAPACIDAD vs EJECUCIÓN sobre el proveedor real
# ═══════════════════════════════════════════════════════════════════════════

def test_ninguna_herramienta_se_publica_LIVE_con_su_llamada_fallando(bundle):
    qd = (bundle.get("auditor") or {}).get("quantdata") or {}
    for t in (qd.get("tools") or []):
        rt = t.get("runtime") or {}
        if t.get("state") == "LIVE":
            assert rt.get("breaker") != "OPEN", (
                f"{t.get('key')}: se enseña LIVE con el circuito abierto")


def test_un_carril_con_filas_no_se_publica_como_seccion_rota(bundle):
    dp = (bundle.get("auditor") or {}).get("dark_pool") or {}
    for carril in (dp.get("lanes") or []):
        if int(carril.get("rows") or 0) > 0 and carril.get("state") == "PROVIDER_ERROR":
            pytest.fail(f"{carril.get('lane')}: {carril['rows']} filas y estado roto")


def test_cero_filas_no_se_publica_como_error(bundle):
    dp = (bundle.get("auditor") or {}).get("dark_pool") or {}
    for carril in (dp.get("lanes") or []):
        if int(carril.get("rows") or 0) == 0 and carril.get("is_failure"):
            assert carril.get("call_failed") or carril.get("error"), (
                f"{carril.get('lane')}: cero filas declarado fallo sin error que lo respalde")


# ═══════════════════════════════════════════════════════════════════════════
# 4 · EL AUDITOR CLASIFICA CADA PENDIENTE  ·  punto 4
# ═══════════════════════════════════════════════════════════════════════════

def test_toda_herramienta_que_no_sirve_lleva_su_causa_clasificada(bundle):
    """Para no tener que buscarlo a mano, que es lo que se pidió."""
    qd = (bundle.get("auditor") or {}).get("quantdata") or {}
    sin_causa = []
    for t in (qd.get("tools") or []):
        if t.get("state") == "LIVE":
            continue
        d = t.get("diagnosis") or {}
        if not d.get("verdict"):
            sin_causa.append(t.get("key"))
    assert not sin_causa, f"sin veredicto: {sin_causa}"


def test_el_resumen_agrupa_las_pendientes_por_remedio(bundle):
    qd = (bundle.get("auditor") or {}).get("quantdata") or {}
    assert "pending_by_verdict" in qd
    validos = {"SIN_INTENTAR", "NO_AUTORIZADO", "NO_EXISTE",
               "EXISTE_CUERPO_INVALIDO", "EXISTE_FALLA", "SIN_DATOS"}
    for v in (qd.get("pending_by_verdict") or {}):
        assert v in validos, f"veredicto desconocido: {v}"


def test_ninguna_herramienta_sigue_sin_intentarse_tras_varios_ciclos(bundle):
    """El hambre del programador estaba corregida: esto lo confirma en vivo.

    Se ejecuta tras el arranque, así que las de cola pueden no haber llegado.
    Falla sólo si el ciclo ya es alto y siguen sin un intento.
    """
    qd = (bundle.get("auditor") or {}).get("quantdata") or {}
    ciclo = int(qd.get("cycle") or 0)
    if ciclo < 60:
        pytest.skip(f"ciclo {ciclo}: hay que dejar que el envejecimiento dé la vuelta")
    sin_intentar = [t.get("key") for t in (qd.get("tools") or [])
                    if (t.get("diagnosis") or {}).get("verdict") == "SIN_INTENTAR"]
    assert not sin_intentar, f"tras {ciclo} ciclos siguen sin intentarse: {sin_intentar}"


# ═══════════════════════════════════════════════════════════════════════════
# 5 · RUNTIME POR ENDPOINT sobre latencia real
# ═══════════════════════════════════════════════════════════════════════════

def test_el_plazo_se_calibra_con_la_latencia_medida(bundle):
    qd = (bundle.get("auditor") or {}).get("quantdata") or {}
    er = qd.get("endpoint_runtime") or {}
    if not er.get("calibrated"):
        pytest.skip("todavía no hay muestra suficiente en ningún endpoint")
    for fila in er.get("endpoints") or []:
        if fila.get("timeout_source") != "MEASURED_P95":
            continue
        assert 2.0 <= float(fila["timeout_seconds"]) <= 20.0
        assert fila.get("latency_p95") is not None
        assert int(fila["samples"]) >= 8


def test_un_circuito_abierto_no_arrastra_a_los_demas(bundle):
    qd = (bundle.get("auditor") or {}).get("quantdata") or {}
    er = qd.get("endpoint_runtime") or {}
    abiertos = set(er.get("open") or [])
    if not abiertos:
        pytest.skip("ningún circuito abierto en este ciclo")
    sanos = [f for f in (er.get("endpoints") or []) if f["key"] not in abiertos]
    assert sanos, "todos los endpoints abiertos a la vez no es aislamiento"
    for f in sanos:
        assert f["breaker"] != "OPEN"


# ═══════════════════════════════════════════════════════════════════════════
# 6 · LA CAUSA RAÍZ, si la hay
# ═══════════════════════════════════════════════════════════════════════════

def test_si_varios_paneles_caen_a_la_vez_se_declara_UN_eslabon(bundle):
    rc = ((bundle.get("auditor") or {}).get("diagnostics") or {}).get("root_cause") or {}
    diag = (bundle.get("auditor") or {}).get("diagnostics") or {}
    fallando = list(diag.get("failing") or [])
    if len(fallando) < 2:
        pytest.skip(f"sólo {len(fallando)} panel(es) sin datos")
    if rc.get("detected"):
        assert rc.get("link") and rc.get("detail")
        assert rc.get("what_to_check")

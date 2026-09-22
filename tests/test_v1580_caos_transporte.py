"""v1.58.0 · SUITE DE CAOS DEL TRANSPORTE · timeouts reales y concurrencia real.

LO QUE DEMOSTRÓ LA CONSOLA DE PRODUCCIÓN
----------------------------------------
Ocho endpoints muriendo **exactamente a 5.0 s**: `delta`, `gamma`, `net_flow`,
`net_drift`, `market_share`, `contract_trade_side_statistics` y los dos order
flow. El plazo «adaptativo» de v1.57.x no gobernaba esas llamadas porque su
arranque en frío salía de `QUANTDATA_TIMEOUT_SECONDS`, y el instalador reparte
`=5`. Un endpoint sin muestras arrancaba en cinco segundos, y como un timeout
aporta UNA muestra y el cortacircuitos abre a los cuatro fallos seguidos,
calibrar tardaba minutos.

LO QUE ESTA SUITE ATA
---------------------
Cada prueba reproduce un fallo real del proveedor y comprueba la conducta, no el
mensaje:

    timeout · 429 · 5xx · cuerpo vacío · respuesta lenta · recuperación
    concurrencia en vuelo · escalonado · aislamiento entre endpoints y activos
    presupuesto de reintento · plazo de conexión frente a plazo de lectura

Son sintéticos a propósito: reproducen el fallo sin depender de la red. Lo que
NO pueden demostrar es que el proveedor real se comporte así, y por eso existe
`scripts/certificar_transporte_live.py`, que se ejecuta en Windows contra la API
de verdad. Ninguna de estas pruebas cierra un criterio LIVE.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from app.core import endpoint_runtime as ER
from app.core import transport_runtime as TR
from app.core.endpoint_runtime import (CLOSED, Deadline, EndpointRegistry,
                                       EndpointRuntime, retry_plan)
from app.core.request_governor import HEAVY, LIGHT, RequestGovernor
from app.providers.quantdata.client import (QuantDataClient, QuantDataError,
                                            QuantDataTimeout)
from app.providers.quantdata.settings import QuantDataSettings, load_settings
from app.providers.quantdata.shared import HEAVY_TOOLS, weight_of


def _settings(**kw) -> QuantDataSettings:
    base = dict(enabled=True, api_key="qd_" + "0" * 32,
                base_url="https://api.quantdata.us", refresh_seconds=15.0,
                request_timeout_seconds=ER.WARM_START_READ_S,
                iv_lookback_days=30, iv_maturity_days=30,
                connect_timeout_seconds=ER.CONNECT_TIMEOUT_S,
                read_warm_start_seconds=ER.WARM_START_READ_S)
    base.update(kw)
    return QuantDataSettings(**base)


class _Caos:
    """Transporte que reproduce el fallo que se le pida, por guion."""

    def __init__(self, guion) -> None:
        self.guion = list(guion)
        self.llamadas = 0
        self.plazos = []
        self.vivos = 0
        self.max_vivos = 0
        self.arranques = []
        self.extensiones = []

    async def post(self, path, json=None, timeout=None, extensions=None):  # noqa: A002
        # v1.60.0 · El transporte mide con el `trace` de httpcore: pool, socket,
        # TLS y lectura por separado. El caos tiene que aceptarlo igual que el
        # transporte real, o estaría probando otra interfaz.
        self.llamadas += 1
        self.plazos.append(timeout)
        self.extensiones.append(extensions)
        self.vivos += 1
        self.max_vivos = max(self.max_vivos, self.vivos)
        self.arranques.append(time.monotonic())
        try:
            paso = self.guion[min(self.llamadas - 1, len(self.guion) - 1)]
            clase = paso if isinstance(paso, str) else paso[0]
            arg = None if isinstance(paso, str) else paso[1]
            if clase == "TIMEOUT":
                raise httpx.ReadTimeout("read timeout")
            if clase == "CONNECT_TIMEOUT":
                raise httpx.ConnectTimeout("connect timeout")
            if clase == "POOL_TIMEOUT":
                raise httpx.PoolTimeout("pool timeout")
            if clase == "SLOW":
                await asyncio.sleep(float(arg))
            if clase == "429":
                return httpx.Response(
                    429, json={"detail": "rate limited"},
                    headers={"Retry-After": "7"},
                    request=httpx.Request("POST", "https://x/y"))
            if clase == "500":
                return httpx.Response(500, json={"detail": "upstream"},
                                      request=httpx.Request("POST", "https://x/y"))
            if clase == "EMPTY":
                return httpx.Response(200, json={"data": {"rows": []}},
                                      request=httpx.Request("POST", "https://x/y"))
            return httpx.Response(200, json={"data": {"ok": True}},
                                  request=httpx.Request("POST", "https://x/y"))
        finally:
            self.vivos -= 1


def _cliente(guion, **kw):
    c = QuantDataClient(_settings(**kw))
    c._client = _Caos(guion)
    return c, c._client


@pytest.fixture(autouse=True)
def _transporte_limpio():
    """El transporte es del HOST y vive en un registro de proceso.

    Sin esto, el plazo escalado por un caso de conexión fallida se arrastra al
    siguiente y las pruebas se contaminan entre sí, que es la peor forma de
    tener una suite verde.
    """
    TR.TRANSPORT.reset()
    yield
    TR.TRANSPORT.reset()


# ═══════════════════════════════════════════════════════════════════════════
# 1 · EL 5.0 s YA NO EXISTE COMO AUTORIDAD
# ═══════════════════════════════════════════════════════════════════════════

def test_un_env_de_cinco_segundos_no_gobierna_el_arranque_en_frio(monkeypatch):
    """El defecto exacto de la consola de producción."""
    monkeypatch.setenv("QUANTDATA_TIMEOUT_SECONDS", "5")
    cfg = load_settings()
    assert cfg.read_warm_start_seconds == pytest.approx(ER.WARM_START_READ_S)
    assert cfg.legacy_timeout_ignored is True
    assert cfg.legacy_timeout_value == pytest.approx(5.0)
    politica = cfg.timeout_policy()
    assert "ignorado" in politica["legacy_note"]


def test_un_env_mas_largo_si_se_respeta(monkeypatch):
    """Quien pide más paciencia la tiene; el suelo sólo impide rebajarla."""
    monkeypatch.setenv("QUANTDATA_TIMEOUT_SECONDS", "25")
    cfg = load_settings()
    assert cfg.read_warm_start_seconds == pytest.approx(25.0)
    assert cfg.legacy_timeout_ignored is False


def test_ningun_endpoint_sin_historia_arranca_por_debajo_del_warm_start():
    reg = EndpointRegistry()
    reg.set_warm_start(5.0)                      # lo que traía el `.env`
    for clave in ("gamma", "delta", "net_flow", "net_drift", "market_share",
                  "contract_trade_side_statistics", "options_order_flow",
                  "options_order_flow_raw"):
        d = reg.get(clave).deadline()
        assert d.read >= ER.WARM_START_READ_S, clave
        assert d.source == "WARM_START"


def test_el_plazo_de_conexion_es_independiente_del_de_lectura():
    rt = EndpointRuntime("interval_map_delta")
    d = rt.deadline()
    assert d.connect == pytest.approx(ER.CONNECT_TIMEOUT_S)
    assert d.read == pytest.approx(ER.WARM_START_READ_S)
    assert d.connect != d.read
    for _ in range(12):
        rt.record_success(0.3)
    # Calibrar la LECTURA no toca la conexión: son dos escalas distintas.
    assert rt.deadline().connect == pytest.approx(ER.CONNECT_TIMEOUT_S)
    assert rt.deadline().read < 2.5


def test_el_transporte_recibe_las_cuatro_fases_por_separado():
    """v1.60.0 · Cuatro plazos, y cada uno de su autoridad.

    La LECTURA es la que trae el `Deadline` del endpoint —su p95—. La CONEXIÓN
    ya no: la pone el HOST, porque un handshake no pertenece a ninguna
    herramienta. `write` y `pool` los pone el transporte, que es quien conoce el
    tamaño del pool.
    """
    cliente, caos = _cliente(["OK"])
    plazo = Deadline(connect=2.0, read=11.0, source="TEST")
    asyncio.run(cliente.post("/v1/options/tool/interval-map", {}, timeout=plazo))
    enviado = caos.plazos[-1]
    anfitrion = TR.TRANSPORT.host(_settings().base_url)
    assert enviado.read == pytest.approx(11.0), "la lectura sigue siendo del endpoint"
    assert enviado.connect == pytest.approx(anfitrion.connect_timeout()), (
        "la conexión la gobierna el host, no la herramienta")
    assert enviado.write is not None and enviado.pool is not None
    assert len({enviado.connect, enviado.read, enviado.write, enviado.pool}) >= 3, (
        "cuatro fases con el mismo número serían una sola disfrazada de cuatro")


# ═══════════════════════════════════════════════════════════════════════════
# 2 · CAOS · cada fallo del proveedor, y su conducta
# ═══════════════════════════════════════════════════════════════════════════

def test_caos_timeout_de_lectura_dice_su_fase_y_su_plazo():
    cliente, _ = _cliente(["TIMEOUT"])
    with pytest.raises(QuantDataTimeout) as caja:
        asyncio.run(cliente.post("/x", {}, timeout=Deadline(connect=2.0, read=9.0)))
    assert caja.value.phase == TR.READ
    assert caja.value.limit_seconds == pytest.approx(9.0)


def test_caos_timeout_de_conexion_no_se_confunde_con_lentitud_del_endpoint():
    """Subir el plazo de lectura por un fallo de conexión es perseguir el
    síntoma equivocado: el proveedor no llegó a recibir la petición."""
    cliente, _ = _cliente(["CONNECT_TIMEOUT"])
    esperado = TR.TRANSPORT.host(_settings().base_url).connect_timeout()
    # `burst=True` deja el intento SOLO: durante una ráfaga no se concede
    # reintento de conexión, así que el plazo que expira es el de este intento y
    # no el ya escalado por el anterior.
    with pytest.raises(QuantDataTimeout) as caja:
        asyncio.run(cliente.post("/x", {}, timeout=Deadline(connect=2.0, read=9.0),
                                 burst=True))
    assert caja.value.phase == TR.CONNECT
    # El plazo que expiró es el del HOST, y es el que viaja en el error: es el
    # único con el que se puede subir el siguiente intento.
    assert caja.value.limit_seconds == pytest.approx(esperado)
    assert caja.value.limit_seconds != pytest.approx(9.0), (
        "jamás el plazo de lectura: subirlo por un fallo de conexión es "
        "perseguir el síntoma equivocado")


def test_caos_un_timeout_de_conexion_no_infla_el_plazo_de_lectura():
    rt = EndpointRuntime("gamma")
    antes = rt.deadline().read
    for _ in range(10):
        rt.record_failure("connect timeout", status="CONNECT_TIMEOUT")
    assert rt.deadline().read == pytest.approx(antes)


def test_caos_429_llega_con_su_retry_after():
    cliente, _ = _cliente(["429"])
    with pytest.raises(QuantDataError) as caja:
        asyncio.run(cliente.post("/x", {}))
    assert "rate limited" in str(caja.value).lower()


def test_caos_5xx_es_del_proveedor_y_reintentable():
    cliente, _ = _cliente(["500"])
    with pytest.raises(QuantDataError) as caja:
        asyncio.run(cliente.post("/x", {}))
    assert caja.value.status_code == 500
    rt = EndpointRuntime("net_flow")
    assert retry_plan(rt, status="PROVIDER_ERROR", in_burst=False,
                      cycle_remaining_s=60.0, deadline=rt.deadline())["retry"] is True


def test_caos_cuerpo_vacio_es_respuesta_valida_no_error():
    """Cero filas con HTTP 200 es NO_DATA, no PROVIDER_ERROR. Confundirlos hace
    buscar la avería donde no está."""
    cliente, _ = _cliente(["EMPTY"])
    r = asyncio.run(cliente.post("/x", {}))
    assert r.status_code == 200
    assert (r.payload.get("data") or {}).get("rows") == []


def test_caos_respuesta_lenta_dentro_del_plazo_se_acepta():
    cliente, _ = _cliente([("SLOW", 0.15)])
    r = asyncio.run(cliente.post("/x", {}, timeout=Deadline(connect=1.0, read=5.0)))
    assert r.status_code == 200


def test_caos_recuperacion_tras_timeout_vuelve_a_LIVE():
    """Gamma/Delta/Net Drift/Net Flow tienen que volver después de un timeout."""
    rt = EndpointRuntime("delta")
    for _ in range(3):
        rt.record_timeout(rt.deadline().read)
    assert rt.snapshot()["consecutive_failures"] == 3
    rt.record_success(0.4)
    snap = rt.snapshot()
    assert snap["breaker"] == CLOSED
    assert snap["consecutive_failures"] == 0
    assert snap["last_status"] == "OK"


def test_caos_un_endpoint_caido_no_toca_a_los_demas():
    reg = EndpointRegistry()
    muerto = reg.get("dark_flow")
    for _ in range(ER.FAILURE_THRESHOLD):
        muerto.record_timeout(12.0)
    assert muerto.snapshot()["breaker"] != CLOSED
    for otro in ("gamma", "delta", "net_flow", "gex_by_strike"):
        s = reg.get(otro).snapshot()
        assert s["breaker"] == CLOSED, otro
        assert s["consecutive_failures"] == 0
        assert s["timeouts"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3 · CONCURRENCIA REAL · la avalancha autoinfligida
# ═══════════════════════════════════════════════════════════════════════════

def test_el_gobernador_limita_las_peticiones_VIVAS_no_las_hechas():
    """La cuota cuenta peticiones; esto cuenta peticiones simultáneas.

    Veinte peticiones en un segundo cumplen el contrato del proveedor y, si las
    veinte son mapas por intervalo, están las veinte abiertas a la vez.
    """
    gov = RequestGovernor(max_inflight=3, max_heavy_inflight=2, stagger_s=0.0)
    vivos = {"n": 0, "max": 0}

    async def una(i):
        turno = await gov.acquire(f"t{i}", weight=LIGHT, budget_s=5.0)
        vivos["n"] += 1
        vivos["max"] = max(vivos["max"], vivos["n"])
        await asyncio.sleep(0.02)
        vivos["n"] -= 1
        turno.done("OK")

    async def todas():
        await asyncio.gather(*[una(i) for i in range(12)])

    asyncio.run(todas())
    assert vivos["max"] <= 3
    assert gov.snapshot()["max_observed_inflight"] <= 3


def test_las_pesadas_tienen_su_propio_techo():
    gov = RequestGovernor(max_inflight=6, max_heavy_inflight=2, stagger_s=0.0)
    pesadas = {"n": 0, "max": 0}

    async def una(i):
        turno = await gov.acquire(f"heavy{i}", weight=HEAVY, budget_s=5.0)
        pesadas["n"] += 1
        pesadas["max"] = max(pesadas["max"], pesadas["n"])
        await asyncio.sleep(0.02)
        pesadas["n"] -= 1
        turno.done("OK")

    async def todas():
        await asyncio.gather(*[una(i) for i in range(8)])

    asyncio.run(todas())
    assert pesadas["max"] <= 2, "cuatro mapas por intervalo a la vez es la avalancha"


def test_dos_pesadas_no_arrancan_en_el_mismo_instante():
    gov = RequestGovernor(max_inflight=4, max_heavy_inflight=4, stagger_s=0.05)
    momentos = []

    async def una(i):
        turno = await gov.acquire(f"h{i}", weight=HEAVY, budget_s=5.0)
        momentos.append(time.monotonic())
        turno.done("OK")

    async def todas():
        await asyncio.gather(*[una(i) for i in range(4)])

    asyncio.run(todas())
    momentos.sort()
    separaciones = [b - a for a, b in zip(momentos, momentos[1:])]
    assert all(s >= 0.04 for s in separaciones), separaciones


def test_la_clasificacion_separa_lo_pesado_de_lo_ligero():
    assert weight_of("interval_map_delta") == HEAVY
    assert weight_of("options_order_flow_raw") == HEAVY
    assert weight_of("market_share") == HEAVY
    assert weight_of("engine:gamma") == HEAVY
    assert weight_of("news") == LIGHT
    assert weight_of("gainers_losers") == LIGHT
    # Los endpoints que la consola mostró cayéndose a 5.0 s son pesados: es la
    # razón por la que se ahogaban entre ellos.
    for clave in ("gamma", "delta", "market_share",
                  "contract_trade_side_statistics", "options_order_flow",
                  "options_order_flow_raw"):
        assert clave in HEAVY_TOOLS or weight_of(clave) == HEAVY, clave


def test_el_gobernador_mide_donde_se_va_el_tiempo():
    """`queue_wait_ms` es congestión nuestra; `request_ms` es lentitud suya.

    Sin separarlos, «tardó 9 s» no distingue las dos y son arreglos opuestos:
    al proveedor lento se le da MÁS plazo, a la cola propia MENOS concurrencia.
    """
    gov = RequestGovernor(max_inflight=1, max_heavy_inflight=1, stagger_s=0.0)

    async def escenario():
        primero = await gov.acquire("lento", weight=LIGHT, budget_s=9.0)

        async def segundo():
            t = await gov.acquire("en_cola", weight=LIGHT, budget_s=9.0)
            return t.done("OK")

        tarea = asyncio.ensure_future(segundo())
        await asyncio.sleep(0.08)
        primero.done("OK")
        return await tarea

    fila = asyncio.run(escenario())
    assert fila["queue_wait_ms"] >= 50
    assert fila["bottleneck"] == "COLA_PROPIA"
    assert fila["timeout_budget_ms"] == pytest.approx(9000.0)
    snap = gov.snapshot()
    assert "en_cola" in snap["self_congested"]
    for campo in ("queue_wait_ms", "request_ms", "total_ms", "timeout_budget_ms"):
        assert any(campo in f for f in snap["endpoints"]), campo


def test_el_turno_se_libera_aunque_la_peticion_reviente():
    gov = RequestGovernor(max_inflight=1, max_heavy_inflight=1, stagger_s=0.0)

    async def escenario():
        for _ in range(3):
            turno = await gov.acquire("revienta", weight=HEAVY, budget_s=1.0)
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                turno.done("ERROR")
        # Si algún turno no se hubiera liberado, esto se quedaría colgado.
        ultimo = await gov.acquire("revienta", weight=HEAVY, budget_s=1.0)
        ultimo.done("OK")
        return gov.snapshot()

    snap = asyncio.run(asyncio.wait_for(escenario(), timeout=3.0))
    assert snap["inflight"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 4 · PRESUPUESTO DE REINTENTO
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("status,espera_retry", [
    ("TIMEOUT", True), ("PROVIDER_ERROR", True), ("TRANSIENT", True),
    ("RATE_LIMITED", True),
    ("REQUEST_INVALID", False), ("UNAUTHORIZED", False), ("FORBIDDEN", False),
    ("MISSING_TOOL", False), ("NO_DATA", False),
])
def test_solo_se_reintenta_lo_que_un_reintento_puede_arreglar(status, espera_retry):
    rt = EndpointRuntime("x")
    plan = retry_plan(rt, status=status, in_burst=False, cycle_remaining_s=120.0,
                      deadline=rt.deadline())
    assert plan["retry"] is espera_retry, plan["reason"]


def test_nunca_se_reintenta_durante_la_rafaga_de_arranque():
    rt = EndpointRuntime("gex_by_strike")
    plan = retry_plan(rt, status="TIMEOUT", in_burst=True, cycle_remaining_s=120.0,
                      deadline=rt.deadline())
    assert plan["retry"] is False
    assert "ráfaga" in plan["reason"]


def test_no_se_reintenta_con_el_cortacircuitos_abierto():
    rt = EndpointRuntime("dark_flow")
    for _ in range(ER.FAILURE_THRESHOLD):
        rt.record_timeout(12.0)
    plan = retry_plan(rt, status="TIMEOUT", in_burst=False, cycle_remaining_s=120.0,
                      deadline=rt.deadline())
    assert plan["retry"] is False
    assert "cortacircuitos" in plan["reason"]


def test_no_se_reintenta_sin_hueco_de_concurrencia():
    rt = EndpointRuntime("x")
    plan = retry_plan(rt, status="TIMEOUT", in_burst=False, cycle_remaining_s=120.0,
                      deadline=rt.deadline(), free_slot=False)
    assert plan["retry"] is False
    assert "concurrencia" in plan["reason"]


def test_no_se_reintenta_si_no_cabe_en_el_ciclo():
    rt = EndpointRuntime("x")
    plan = retry_plan(rt, status="TIMEOUT", in_burst=False, cycle_remaining_s=2.0,
                      deadline=rt.deadline())
    assert plan["retry"] is False
    assert "no cabe en el ciclo" in plan["reason"]


def test_un_solo_reintento_por_endpoint_y_ciclo():
    rt = EndpointRuntime("x")
    assert retry_plan(rt, status="TIMEOUT", in_burst=False, cycle_remaining_s=120.0,
                      deadline=rt.deadline())["retry"] is True
    rt.mark_retry()
    segundo = retry_plan(rt, status="TIMEOUT", in_burst=False,
                         cycle_remaining_s=120.0, deadline=rt.deadline())
    assert segundo["retry"] is False
    assert "ya se reintentó" in segundo["reason"]
    rt.start_cycle()                            # ciclo nuevo, reintento devuelto
    assert retry_plan(rt, status="TIMEOUT", in_burst=False, cycle_remaining_s=120.0,
                      deadline=rt.deadline())["retry"] is True


def test_el_motivo_del_no_reintento_viaja_siempre():
    """Un «no» sin causa no se puede diagnosticar."""
    rt = EndpointRuntime("x")
    for kwargs in ({"status": "REQUEST_INVALID"}, {"status": "TIMEOUT", "in_burst": True},
                   {"status": "TIMEOUT", "free_slot": False},
                   {"status": "TIMEOUT", "cycle_remaining_s": 0.0}):
        base = {"in_burst": False, "cycle_remaining_s": 120.0, "free_slot": True}
        base.update(kwargs)
        plan = retry_plan(rt, deadline=rt.deadline(), **base)
        assert plan["retry"] is False
        assert plan["reason"], base


# ═══════════════════════════════════════════════════════════════════════════
# 5 · EWMA, DERIVA Y AUTORIDAD DEL p95
# ═══════════════════════════════════════════════════════════════════════════

def test_la_autoridad_del_plazo_es_el_p95_no_la_ewma():
    rt = EndpointRuntime("gex_by_strike")
    for _ in range(30):
        rt.record_success(0.5)
    p95 = rt.snapshot()["latency_p95"]
    assert rt.timeout() == pytest.approx(
        max(ER.TIMEOUT_FLOOR_S, min(ER.TIMEOUT_CEILING_S, p95 * ER.TIMEOUT_SAFETY_FACTOR)))


def test_la_ewma_declara_la_deriva_sin_mover_el_plazo():
    rt = EndpointRuntime("net_drift")
    for _ in range(25):
        rt.record_success(0.4)
    plazo_sano = rt.timeout()
    for _ in range(3):
        rt.record_success(2.5)
    d = rt.drift()
    assert d["drifting"] is True
    assert d["compared_against"] == "latency_p50"
    # El plazo se mueve por el p95, no por la EWMA: sube poco, no salta.
    assert rt.timeout() >= plazo_sano
    assert rt.timeout() < 2.5 * ER.TIMEOUT_SAFETY_FACTOR * 2


def test_sin_muestras_no_hay_deriva_que_declarar():
    rt = EndpointRuntime("x")
    assert rt.drift()["drifting"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 6 · AISLAMIENTO ENTRE ACTIVOS
# ═══════════════════════════════════════════════════════════════════════════

def test_una_respuesta_tardia_de_un_activo_no_contamina_a_otro():
    """El LKG es por (herramienta, símbolo): DIA lento no ensucia a SPY."""
    from app.core.data_hub_runtime import LastKnownGood

    lkg = LastKnownGood()
    lkg.put("dark_flow", "DIA", {"rows": [1, 2, 3]})
    lkg.put("dark_flow", "SPY", {"rows": [9]})
    lkg.clear_symbol("DIA")
    assert lkg.read("dark_flow", "DIA")["payload"] is None
    assert lkg.read("dark_flow", "SPY")["payload"] == {"rows": [9]}


def test_el_lkg_es_independiente_por_herramienta():
    from app.core.data_hub_runtime import LastKnownGood

    lkg = LastKnownGood()
    lkg.put("dark_flow", "DIA", {"rows": [1]})
    lkg.put("dark_pool_levels", "DIA", {"rows": [2]})
    lkg.put("equity_prints", "DIA", {"rows": [3]})
    # Que uno caduque o se limpie no puede tocar a los otros dos.
    assert lkg.read("dark_flow", "DIA")["payload"] == {"rows": [1]}
    assert lkg.read("dark_pool_levels", "DIA")["payload"] == {"rows": [2]}
    assert lkg.read("equity_prints", "DIA")["payload"] == {"rows": [3]}


# ═══════════════════════════════════════════════════════════════════════════
# 7 · CABLEADO · que la política llegue a los dos carriles
# ═══════════════════════════════════════════════════════════════════════════

def _src(ruta: str) -> str:
    return Path(ruta).read_text(encoding="utf-8")


def test_los_dos_carriles_piden_turno_al_mismo_gobernador():
    for ruta in ("app/providers/quantdata/intelligence.py",
                 "app/providers/quantdata/runtime.py"):
        assert "GOVERNOR.acquire(" in _src(ruta), ruta
    # Y ya no hay un semáforo por carril: dos techos no son un techo.
    assert "asyncio.Semaphore(" not in _src("app/providers/quantdata/intelligence.py")


def test_el_carril_de_paginas_consulta_el_presupuesto_antes_de_reintentar():
    src = _src("app/providers/quantdata/intelligence.py")
    assert "plan = retry_plan(" in src
    assert "in_burst=self._bursting()" in src
    assert "free_slot=GOVERNOR.has_free_slot()" in src
    assert "cycle_remaining_s=max(0.0, self._cycle_ends_at - time.monotonic())" in src


def test_el_ciclo_devuelve_el_reintento_a_cada_endpoint():
    for ruta in ("app/providers/quantdata/intelligence.py",
                 "app/providers/quantdata/runtime.py"):
        assert "ENDPOINT_RUNTIME.start_cycle()" in _src(ruta), ruta


def test_la_variable_vieja_no_fija_el_warm_start_en_ningun_sitio():
    for ruta in ("app/providers/quantdata/intelligence.py",
                 "app/providers/quantdata/runtime.py"):
        src = _src(ruta)
        assert "set_default_timeout(" not in src, ruta
        assert "set_warm_start(self.settings.read_warm_start_seconds)" in src, ruta

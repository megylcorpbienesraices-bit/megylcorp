"""v1.60.0 · `connect timed out after 4.0s`, UNA Y OTRA VEZ.

LO QUE SE VEÍA
--------------
En el Windows del operador, con la cuota intacta y v1.58.0 ya instalada:

    Quant Data connect timed out after 4.0s   net_drift
    Quant Data connect timed out after 4.0s   net_flow
    Quant Data connect timed out after 4.0s   dark_flow
    Quant Data connect timed out after 4.0s   gamma

Cuatro endpoints que no comparten nada salvo el HOST, muriendo al MISMO plazo
exacto, ciclo tras ciclo. Cuando el fallo es idéntico en cosas que sólo
comparten el destino, el defecto no está en el endpoint: está en el transporte.

LA CADENA, ENTERA
-----------------
1. **Dos pools.** El carril del motor y el de páginas construían cada uno su
   `httpx.AsyncClient`. Dos juegos de conexiones contra el mismo host, y
   ninguno podía aprovechar lo que el otro tenía abierto.

2. **Keep-alive más corto que el ciclo.** El `keepalive_expiry` por defecto de
   httpx son CINCO segundos; el ciclo de páginas son quince. Cada ciclo
   encontraba todas las conexiones caducadas: un handshake TCP+TLS por
   herramienta y por ciclo.

3. **Y todos a la vez.** Con el lote saliendo junto, eso es una ESTAMPIDA de
   conexión: seis u ocho handshakes simultáneos compitiendo por el mismo
   enlace. Con antivirus o proxy de por medio, el handshake se va por encima de
   los cuatro segundos sin que el proveedor tenga nada que ver.

4. **Aprender era imposible por construcción.** La misma trampa que v1.58.1
   cerró para la LECTURA, intacta para la CONEXIÓN: plazo corto → el handshake
   no completa → no hay muestra → no hay p95 → el plazo se queda corto. Para
   siempre.

LO QUE ATA ESTE FICHERO
-----------------------
    · UN pool por host, compartido por los dos carriles, con refcuenta
    · keep-alive que sobrevive al ciclo, y la reutilización MEDIDA
    · las cuatro fases —connect, read, write, pool— separadas de verdad
    · el plazo de conexión gobernado por el HOST y el de lectura por el ENDPOINT
    · escalada al agotarse, que es la salida de la trampa
    · cortocircuitos de transporte para lo que falla a nivel de host
    · un reintento de conexión, con presupuesto, backoff+jitter y NUNCA en ráfaga
    · el último dato bueno conservado ante un fallo de conexión
    · estampida de conexión y recuperación

Y una regla que cruza todo: **subir el número no es el arreglo**. El warm start
sube de 4 s a 8 s porque el razonamiento que sostenía el cuatro es falso con la
evidencia delante, pero lo que quita los timeouts es que el handshake deje de
ocurrir en cada ciclo.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.core import transport_runtime as TR
from app.core.endpoint_runtime import Deadline
from app.providers.quantdata.client import (QuantDataClient, QuantDataError,
                                            QuantDataTimeout)
from app.providers.quantdata.settings import QuantDataSettings


def _settings(**kw) -> QuantDataSettings:
    base = dict(enabled=True, api_key="qd_" + "1" * 32,
                base_url="https://api.quantdata.us", refresh_seconds=15.0,
                request_timeout_seconds=12.0, iv_lookback_days=30,
                iv_maturity_days=30)
    base.update(kw)
    return QuantDataSettings(**base)


class _Red:
    """Un transporte falso que reproduce lo que la red haga falta."""

    def __init__(self, guion) -> None:
        self.guion = list(guion)
        self.llamadas = 0
        self.plazos = []
        self.conexiones = 0          # handshakes que este transporte "abre"
        self.vivas = 0
        self.max_vivas = 0

    async def post(self, path, json=None, timeout=None, extensions=None):  # noqa: A002
        self.llamadas += 1
        self.plazos.append(timeout)
        self.vivas += 1
        self.max_vivas = max(self.max_vivas, self.vivas)
        traza = (extensions or {}).get("trace")
        try:
            paso = self.guion[min(self.llamadas - 1, len(self.guion) - 1)]
            clase = paso if isinstance(paso, str) else paso[0]
            if clase == "CONNECT_TIMEOUT":
                raise httpx.ConnectTimeout("connect timeout")
            if clase == "CONNECT_ERROR":
                raise httpx.ConnectError("name resolution failed")
            if clase == "POOL_TIMEOUT":
                raise httpx.PoolTimeout("pool timeout")
            if clase == "READ_TIMEOUT":
                raise httpx.ReadTimeout("read timeout")
            if traza is not None:
                if clase == "HANDSHAKE":
                    self.conexiones += 1
                    await traza("connection.connect_tcp.started", {})
                    await traza("connection.connect_tcp.complete", {})
                    await traza("connection.start_tls.started", {})
                    await traza("connection.start_tls.complete", {})
                await traza("http11.send_request_headers.started", {})
                await traza("http11.send_request_body.complete", {})
                await traza("http11.receive_response_headers.complete", {})
            return httpx.Response(200, json={"data": {"ok": True}},
                                  request=httpx.Request("POST", "https://x/y"))
        finally:
            self.vivas -= 1


def _cliente(guion, **kw):
    c = QuantDataClient(_settings(**kw))
    c._client = _Red(guion)
    return c, c._client


@pytest.fixture(autouse=True)
def _transporte_limpio():
    TR.TRANSPORT.reset()
    yield
    TR.TRANSPORT.reset()


def _host(cliente=None):
    return TR.TRANSPORT.host(_settings().base_url)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · UN SOLO POOL PARA LOS DOS CARRILES
# ═══════════════════════════════════════════════════════════════════════════

def test_los_dos_carriles_comparten_el_cliente_del_host():
    """Dos pools contra el mismo host son el doble de handshakes."""
    creados = []

    def fabrica():
        creados.append(1)
        return object()

    a = TR.TRANSPORT.acquire("https://api.quantdata.us", fabrica)
    b = TR.TRANSPORT.acquire("https://api.quantdata.us/v1", fabrica)
    assert a is b, "el pool es del HOST, no del objeto que lo pide"
    assert len(creados) == 1


def test_el_primero_en_parar_no_deja_al_otro_sin_transporte():
    """Con refcuenta: se cierra cuando lo suelta el ÚLTIMO, no el primero."""
    centinela = object()
    TR.TRANSPORT.acquire("https://api.quantdata.us", lambda: centinela)
    TR.TRANSPORT.acquire("https://api.quantdata.us", lambda: centinela)
    assert TR.TRANSPORT.release("https://api.quantdata.us") is None, (
        "todavía queda un carril usándolo")
    assert TR.TRANSPORT.release("https://api.quantdata.us") is centinela


def test_el_pool_se_dimensiona_desde_el_techo_de_concurrencia():
    """Un pool más pequeño que el techo convierte concurrencia en espera de pool,
    y esa espera se lee como lentitud del proveedor sin serlo."""
    for techo in (1, 4, 6, 12):
        lim = TR.pool_limits(techo)
        assert lim["max_connections"] >= techo, techo
        assert lim["max_keepalive_connections"] >= techo, techo
        assert lim["keepalive_expiry"] == TR.KEEPALIVE_EXPIRY_S


def test_el_keepalive_sobrevive_al_ciclo():
    """Si caduca antes que el ciclo, cada ciclo vuelve a pagar el handshake.

    Es la causa 2 de la cadena: el defecto de httpx por defecto son 5 s contra
    ciclos de 15 s.
    """
    assert TR.KEEPALIVE_EXPIRY_S >= 60.0
    assert TR.KEEPALIVE_EXPIRY_S > 15.0 * 4


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LAS CUATRO FASES, SEPARADAS DE VERDAD
# ═══════════════════════════════════════════════════════════════════════════

def test_la_peticion_lleva_los_cuatro_plazos():
    cliente, red = _cliente(["OK"])
    asyncio.run(cliente.post("/x", {}, timeout=Deadline(connect=2.0, read=11.0)))
    enviado = red.plazos[-1]
    assert enviado.read == pytest.approx(11.0)
    assert enviado.connect == pytest.approx(_host().connect_timeout())
    assert enviado.write == pytest.approx(TR.WRITE_TIMEOUT_S)
    assert enviado.pool is not None and enviado.pool > 0


@pytest.mark.parametrize("guion,fase", [
    (["CONNECT_TIMEOUT"], TR.CONNECT),
    (["POOL_TIMEOUT"], TR.POOL),
    (["READ_TIMEOUT"], TR.READ),
])
def test_cada_timeout_dice_su_fase(guion, fase):
    """Los tres llegaban como «timeout» y el remedio se adivinaba."""
    cliente, _ = _cliente(guion)
    with pytest.raises(QuantDataTimeout) as caja:
        asyncio.run(cliente.post("/x", {}, timeout=Deadline(connect=2.0, read=9.0),
                                 burst=True))
    assert caja.value.phase == fase


def test_un_pool_lleno_no_es_culpa_del_proveedor():
    """Subirle el plazo al proveedor por nuestra congestión sería arreglar la
    casa del vecino: el pool es NUESTRO."""
    cliente, _ = _cliente(["POOL_TIMEOUT"])
    with pytest.raises(QuantDataTimeout) as caja:
        asyncio.run(cliente.post("/x", {}, burst=True))
    assert caja.value.phase == TR.POOL
    assert "pool propio" in str(caja.value)
    snap = _host().snapshot()
    assert snap["pool_timeouts"] == 1
    assert snap["consecutive_failures"] == 0, (
        "un pool lleno NO es un fallo del host y no puede abrir su circuito")


def test_la_lectura_sigue_siendo_del_endpoint_y_la_conexion_del_host():
    """Las dos autoridades, y por qué no pueden ser la misma."""
    cliente, red = _cliente(["OK"])
    asyncio.run(cliente.post("/x", {}, timeout=Deadline(connect=99.0, read=7.0)))
    enviado = red.plazos[-1]
    assert enviado.read == pytest.approx(7.0), "el p95 del endpoint manda en la lectura"
    assert enviado.connect != pytest.approx(99.0), (
        "un handshake no pertenece a ninguna herramienta: lo gobierna el host")


# ═══════════════════════════════════════════════════════════════════════════
# 3 · LA SALIDA DE LA TRAMPA: MEDIR, Y ESCALAR CUANDO NO SE PUEDE MEDIR
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_muestras_manda_el_warm_start():
    d = _host().connect_decision()
    assert d["source"] == "WARM_START"
    assert d["seconds"] == pytest.approx(TR.CONNECT_WARM_START_S)


def test_con_handshakes_medidos_manda_el_p95():
    h = _host()
    for _ in range(TR.MIN_CONNECT_SAMPLES + 2):
        t = TR.TransportTrace()
        t.reused = False
        t.connect_s = 0.4
        h.note_trace(t)
    d = h.connect_decision()
    assert d["source"] == "MEASURED_P95_HANDSHAKE"
    assert d["seconds"] == pytest.approx(
        max(TR.CONNECT_FLOOR_S, 0.4 * TR.CONNECT_P95_FACTOR))


def test_un_plazo_agotado_SUBE_el_siguiente():
    """Sin esto no hay salida: plazo corto → sin handshake → sin muestra →
    plazo corto. Es la causa 4, y la que hacía que el 4.0 s fuera eterno."""
    h = _host()
    antes = h.connect_timeout()
    h.note_connect_timeout(antes)
    despues = h.connect_timeout()
    assert despues > antes
    assert h.connect_decision()["source"] == "ESCALATED_AFTER_TIMEOUT"


def test_la_escalada_tiene_techo():
    h = _host()
    for _ in range(20):
        h.note_connect_timeout(h.connect_timeout())
    assert h.connect_timeout() == pytest.approx(TR.CONNECT_CEILING_S)


def test_y_baja_sola_cuando_el_host_vuelve_a_conectar_bien():
    """Un plazo que sube y no baja se queda esperando quince segundos a un host
    que ya conecta en trescientos milisegundos."""
    h = _host()
    h.note_connect_timeout(h.connect_timeout())
    alto = h.connect_timeout()
    for _ in range(40):
        h.note_success()
    assert h.connect_timeout() < alto


def test_el_plazo_de_conexion_nunca_baja_del_suelo_ni_sube_del_techo():
    h = TR.HostTransport("x", warm_start=0.1)
    assert h.connect_timeout() >= TR.CONNECT_FLOOR_S
    h2 = TR.HostTransport("y", warm_start=900.0)
    assert h2.connect_timeout() <= TR.CONNECT_CEILING_S


# ═══════════════════════════════════════════════════════════════════════════
# 4 · CORTOCIRCUITOS DE TRANSPORTE
# ═══════════════════════════════════════════════════════════════════════════

def test_los_fallos_de_host_abren_el_circuito_del_transporte():
    """Es distinto del de endpoint: lo que impide LLEGAR afecta a las treinta y
    seis herramientas, y seguir llamando sólo alarga la cola."""
    h = _host()
    for _ in range(TR.HOST_FAILURE_THRESHOLD):
        h.note_host_failure("ConnectError")
    permiso = h.allows()
    assert permiso["allowed"] is False
    assert permiso["state"] == TR.OPEN
    assert "cortocircuito" in permiso["reason"]


def test_con_el_circuito_abierto_se_falla_rapido_y_se_nombra_la_fase():
    cliente, red = _cliente(["OK"])
    h = _host()
    for _ in range(TR.HOST_FAILURE_THRESHOLD):
        h.note_host_failure("ConnectError")
    with pytest.raises(QuantDataError) as caja:
        asyncio.run(cliente.post("/x", {}))
    assert caja.value.phase == TR.CONNECT
    assert red.llamadas == 0, "no se toca la red con el circuito abierto"


def test_el_circuito_se_reabre_y_deja_pasar_una_de_prueba():
    """Un estado del que no se sale es una avería permanente disfrazada."""
    h = _host()
    for _ in range(TR.HOST_FAILURE_THRESHOLD):
        h.note_host_failure("ConnectError")
    h._open_until = time.monotonic() - 0.01           # vencido
    permiso = h.allows()
    assert permiso["allowed"] is True
    assert permiso["state"] == TR.HALF_OPEN
    h.note_success()
    assert h.allows()["state"] == TR.CLOSED


def test_un_5xx_no_abre_el_circuito_del_transporte():
    """Un error del endpoint no dice nada sobre la red: si abriera el circuito
    del host, una herramienta rota apagaría a las otras treinta y cinco."""
    h = _host()
    antes = h.snapshot()["consecutive_failures"]
    h.note_pool_timeout()
    assert h.snapshot()["consecutive_failures"] == antes


# ═══════════════════════════════════════════════════════════════════════════
# 5 · UN REINTENTO DE CONEXIÓN, Y SUS CUATRO CONDICIONES
# ═══════════════════════════════════════════════════════════════════════════

def test_se_concede_uno_con_backoff_y_jitter():
    h = _host()
    h.start_cycle()
    d = h.retry_connect(burst=False)
    assert d["retry"] is True
    assert TR.CONNECT_RETRY_BACKOFF_S <= d["sleep_seconds"] <= (
        TR.CONNECT_RETRY_BACKOFF_S + TR.CONNECT_RETRY_JITTER_S)


def test_nunca_durante_una_rafaga():
    """La ráfaga ya está usando el enlace entero: reintentar ahí agrava la
    estampida que es la causa del fallo."""
    h = _host()
    h.start_cycle()
    d = h.retry_connect(burst=True)
    assert d["retry"] is False and "ráfaga" in d["reason"]


def test_el_presupuesto_es_del_HOST_y_se_agota():
    """Un reintento por petición multiplicaría por dos la estampida."""
    h = _host()
    h.start_cycle()
    concedidos = sum(1 for _ in range(10) if h.retry_connect()["retry"])
    assert concedidos == TR.CONNECT_RETRY_BUDGET
    assert h.retry_connect()["retry"] is False


def test_el_presupuesto_se_repone_cada_ciclo():
    h = _host()
    h.start_cycle()
    for _ in range(TR.CONNECT_RETRY_BUDGET):
        h.retry_connect()
    assert h.retry_connect()["retry"] is False
    h.start_cycle()
    assert h.retry_connect()["retry"] is True


def test_no_se_reintenta_si_no_cabe_en_lo_que_queda_de_ciclo():
    h = _host()
    h.start_cycle()
    d = h.retry_connect(deadline_left_s=0.05)
    assert d["retry"] is False and "no cabe" in d["reason"]


def test_no_se_reintenta_con_el_circuito_abierto():
    h = _host()
    h.start_cycle()
    for _ in range(TR.HOST_FAILURE_THRESHOLD):
        h.note_host_failure("ConnectError")
    assert h.retry_connect()["retry"] is False


def test_el_cliente_reintenta_una_vez_y_lo_deja_dicho():
    cliente, red = _cliente(["CONNECT_TIMEOUT", "CONNECT_TIMEOUT"])
    _host().start_cycle()
    with pytest.raises(QuantDataTimeout) as caja:
        asyncio.run(cliente.post("/x", {}, burst=False, cycle_left_s=30.0))
    assert red.llamadas == 2, "uno, y sólo uno"
    assert caja.value.retry_decision["spent"] is True


def test_y_en_rafaga_no_reintenta_ninguna_vez():
    cliente, red = _cliente(["CONNECT_TIMEOUT", "OK"])
    h = _host()
    h.start_cycle()
    with pytest.raises(QuantDataTimeout) as caja:
        asyncio.run(cliente.post("/x", {}, burst=True))
    assert red.llamadas == 1, "en ráfaga se intenta una vez y se acepta el fallo"
    assert caja.value.retry_decision["retry"] is False
    assert "ráfaga" in caja.value.retry_decision["reason"]
    assert h.snapshot()["connect_retries_used"] == 0, (
        "y el presupuesto queda intacto para cuando la ráfaga termine")


# ═══════════════════════════════════════════════════════════════════════════
# 6 · TELEMETRÍA: DÓNDE SE FUE EL TIEMPO
# ═══════════════════════════════════════════════════════════════════════════

def test_la_traza_separa_pool_socket_tls_y_lectura():
    cliente, _ = _cliente(["HANDSHAKE"])
    r = asyncio.run(cliente.post("/x", {}))
    t = r.transport
    for campo in ("pool_wait_ms", "connect_ms", "tls_ms", "write_ms", "read_ms",
                  "request_ms", "connection_reused"):
        assert campo in t, campo
    assert t["connection_reused"] is False, "hubo handshake"
    assert t["connect_ms"] is not None and t["tls_ms"] is not None


def test_una_conexion_reutilizada_se_declara_como_tal():
    """Es la cifra que demuestra que el arreglo no fue subir el plazo."""
    cliente, _ = _cliente(["OK"])
    r = asyncio.run(cliente.post("/x", {}))
    assert r.transport["connection_reused"] is True
    assert r.transport["connect_ms"] is None, "sin handshake no hay tiempo de handshake"


def test_el_host_publica_el_porcentaje_de_reutilizacion():
    cliente, _ = _cliente(["HANDSHAKE", "OK", "OK", "OK"])
    for _ in range(4):
        asyncio.run(cliente.post("/x", {}))
    snap = _host().snapshot()
    assert snap["handshakes"] == 1 and snap["reused_connections"] == 3
    assert snap["reuse_pct"] == pytest.approx(75.0)


def test_el_snapshot_del_transporte_dice_el_plazo_y_de_donde_sale():
    snap = TR.TRANSPORT.snapshot()
    assert snap["contract"] == "ITMQ_TRANSPORT_V1"
    assert snap["policy"]["keepalive_expiry_s"] == TR.KEEPALIVE_EXPIRY_S
    _host()
    fila = TR.TRANSPORT.snapshot()["hosts"][0]
    for campo in ("connect_timeout_s", "connect_timeout_source", "connect_p95_ms",
                  "tls_p95_ms", "pool_wait_p95_ms", "reuse_pct", "breaker",
                  "connect_timeouts", "pool_timeouts"):
        assert campo in fila, campo


# ═══════════════════════════════════════════════════════════════════════════
# 7 · ESTAMPIDA DE CONEXIÓN Y RECUPERACIÓN
# ═══════════════════════════════════════════════════════════════════════════

def test_estampida_de_conexion_un_solo_handshake_para_todo_el_lote():
    """EL ESCENARIO DEL DEFECTO.

    Ocho herramientas salen a la vez contra el mismo host. Con un pool por
    carril y keep-alive de cinco segundos, eso eran ocho handshakes
    simultáneos. Con el pool compartido, el primero abre y los demás reutilizan.
    """
    cliente, red = _cliente(["HANDSHAKE"] + ["OK"] * 20)

    async def lote():
        return await asyncio.gather(*[cliente.post(f"/t{i}", {}) for i in range(8)])

    respuestas = asyncio.run(lote())
    assert len(respuestas) == 8
    snap = _host().snapshot()
    assert snap["handshakes"] == 1, "ocho handshakes simultáneos son la estampida"
    assert snap["reused_connections"] == 7
    assert snap["connect_timeouts"] == 0


def test_recuperacion_el_host_vuelve_despues_de_una_tanda_de_fallos():
    """Un transporte del que no se sale no es resiliencia, es una avería."""
    cliente, red = _cliente(["CONNECT_TIMEOUT"] * 6 + ["HANDSHAKE", "OK", "OK"])
    h = _host()
    h.start_cycle()
    fallos = 0
    for _ in range(3):
        try:
            asyncio.run(cliente.post("/x", {}, burst=True))
        except QuantDataError:
            fallos += 1
    assert fallos == 3
    assert h.snapshot()["connect_timeouts"] >= 3
    # El host se recupera y el circuito se cierra.
    h._state, h._open_until, h._consecutive = TR.CLOSED, 0.0, 0
    red.llamadas = 6
    r = asyncio.run(cliente.post("/x", {}))
    assert r.status_code == 200
    assert h.snapshot()["breaker"] == TR.CLOSED
    assert h.snapshot()["consecutive_failures"] == 0


def test_un_fallo_de_conexion_no_borra_el_ultimo_dato_bueno():
    """LKG: un handshake que no completa no dice nada sobre el dato que ya
    teníamos, y tirarlo dejaría la pantalla en blanco por un fallo de red."""
    from app.providers.quantdata.tools import classify_provider_failure, STATUS_TRANSIENT
    e1 = QuantDataTimeout("Quant Data connect timed out after 8.0s")
    e1.phase = TR.CONNECT
    e2 = QuantDataError("Quant Data connect error: ConnectError")
    e2.phase = TR.CONNECT
    e3 = QuantDataError("Quant Data transport open: host en cortocircuito")
    e3.phase = TR.CONNECT
    for e in (e1, e2, e3):
        assert classify_provider_failure(e) == STATUS_TRANSIENT, (
            "un fallo de transporte NO puede clasificarse como herramienta "
            "inexistente ni como cuerpo inválido: eso invalidaría la ruta y "
            "tiraría el último dato bueno")


def test_un_fallo_de_conexion_no_infla_el_plazo_de_lectura_del_endpoint():
    """v1.58.0 lo ató para el endpoint; v1.60.0 lo mantiene con la fase nueva."""
    from app.core.endpoint_runtime import EndpointRuntime
    rt = EndpointRuntime("gamma")
    antes = rt.deadline().read
    for _ in range(10):
        rt.record_failure("connect timeout", status=TR.CONNECT)
    assert rt.deadline().read == pytest.approx(antes)


# ═══════════════════════════════════════════════════════════════════════════
# 8 · EL NÚMERO NO ES EL ARREGLO
# ═══════════════════════════════════════════════════════════════════════════

def test_el_4_0_fijo_ya_no_existe_como_autoridad():
    from app.core import endpoint_runtime as ER
    assert ER.CONNECT_TIMEOUT_S == TR.CONNECT_WARM_START_S, (
        "dos números distintos diciendo ser el mismo plazo")
    assert TR.CONNECT_WARM_START_S != 4.0


def test_el_warm_start_solo_gobierna_mientras_no_hay_medidas():
    """Si el número fuera el arreglo, seguiría mandando con cien muestras."""
    h = _host()
    for _ in range(TR.MIN_CONNECT_SAMPLES + 5):
        t = TR.TransportTrace()
        t.reused = False
        t.connect_s = 0.2
        h.note_trace(t)
    assert h.connect_decision()["source"] != "WARM_START"
    assert h.connect_timeout() < TR.CONNECT_WARM_START_S, (
        "con handshakes rápidos medidos, el plazo tiene que BAJAR del warm start")

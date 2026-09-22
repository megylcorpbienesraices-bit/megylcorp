"""v1.58.1 · EL PLAZO MEDIDO NO LLEGABA A LA PETICIÓN.

LO QUE SE VEÍA
--------------
El registro del motor, media hora seguida, con la cuota en 7 de 240:

    degradado en data_hub:dark_flow:late      [DEGRADED]: Quant Data request timed out
    degradado en data_hub:gamma:late          [DEGRADED]: Quant Data request timed out
    degradado en data_hub:max_pain:late       [DEGRADED]: Quant Data request timed out
    degradado en data_hub:interval_map_delta  [DEGRADED]: Quant Data request timed out

Y en pantalla: los dos carriles de dark pool en STALE con «el canal tardó más de
6.0 s», media docena de herramientas en DEGRADADO y la cobertura por canal entre
el 27 % y el 62 %. Nada de eso era el proveedor: la cuota estaba intacta.

LA CADENA
---------
1. El instalador reparte `QUANTDATA_TIMEOUT_SECONDS=5`, y ése era el plazo de
   TODAS las peticiones de las treinta y seis herramientas y de los dos carriles.
   El plazo del canal se calculaba como ese valor + 1 → los 6.0 s exactos que
   enseñaba la pantalla.

2. Los endpoints pesados del proveedor no contestan en cinco segundos, así que
   morían por plazo en cada ciclo.

3. Y no había salida: `record_failure` no toca las latencias —correcto para un
   500 o un 404, que no dicen nada sobre cuánto tarda el endpoint—, así que un
   endpoint que nunca llegaba a responder no dejaba NUNCA una muestra, no
   alcanzaba nunca las ocho que hacen falta para calibrar, y se le seguía
   llamando con cinco segundos. El techo de 20 s que `endpoint_runtime` publica
   era inalcanzable por construcción.

4. Encima el plazo calibrado sólo gobernaba el reloj del CICLO: la petición
   seguía viva con el plazo del constructor del cliente. Cuando el calibrado
   bajaba del configurado, el ciclo se rendía primero, la petición huérfana
   seguía ocupando conexión y cuota, y al morir soltaba un SEGUNDO aviso por el
   mismo hecho. De ahí los `:late` del registro.

LO QUE ATA ESTE FICHERO
-----------------------
· el plazo viaja CON la petición, y es el que mide el registro;
· un timeout es una cota inferior de latencia y SUBE el plazo siguiente;
· subir el plazo no es llamar para siempre: el timeout sigue contando para el
  cortacircuitos;
· una respuesta real vuelve a bajarlo;
· el ciclo espera un pelo más que la petición, así que el fallo se cuenta UNA vez.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.core import endpoint_runtime as ER
from app.core import obs
from app.core.data_hub_runtime import CHANNEL_SLACK_S, ChannelIsolator
from app.core.endpoint_runtime import OPEN, EndpointRegistry, EndpointRuntime
from app.providers.quantdata.client import (
    QuantDataClient, QuantDataError, QuantDataTimeout,
)
from app.providers.quantdata.settings import QuantDataSettings


def _texto(ruta: str) -> str:
    return Path(ruta).read_text(encoding="utf-8")


def _settings(plazo: float = 5.0) -> QuantDataSettings:
    return QuantDataSettings(
        enabled=True,
        api_key="qd_" + "0" * 32,          # 35 caracteres, como exige `configured`
        base_url="https://api.quantdata.us",
        refresh_seconds=15.0,
        request_timeout_seconds=plazo,
        iv_lookback_days=30,
        iv_maturity_days=30,
    )


class _Transporte:
    """Cliente httpx de mentira que apunta con qué plazo se le llamó."""

    def __init__(self, *, revienta: BaseException | None = None) -> None:
        self.revienta = revienta
        self.plazos: list[object] = []

    async def post(self, path, json=None, timeout=None):   # noqa: A002
        self.plazos.append(timeout)
        if self.revienta is not None:
            raise self.revienta
        return httpx.Response(200, json={"data": {"ok": True}},
                              request=httpx.Request("POST", "https://x/y"))

    @property
    def ultimo_plazo_s(self) -> float:
        t = self.plazos[-1]
        return float(getattr(t, "read", t))


# ═══════════════════════════════════════════════════════════════════════════
# 1 · EL PLAZO VIAJA CON LA PETICIÓN
# ═══════════════════════════════════════════════════════════════════════════

def test_el_plazo_de_la_peticion_es_el_que_se_le_pasa():
    """Y no el del constructor: es el único sitio donde el plazo medido sirve."""
    cliente = QuantDataClient(_settings(plazo=5.0))
    cliente._client = transporte = _Transporte()

    asyncio.run(cliente.post("/v1/options/tool/interval-map", {}, timeout=14.0))

    assert transporte.ultimo_plazo_s == pytest.approx(14.0), (
        "el endpoint pesado se cortaba a los 5 s por mucho que su plazo medido "
        "dijera 14")


def test_sin_plazo_explicito_manda_el_configurado():
    cliente = QuantDataClient(_settings(plazo=7.5))
    cliente._client = transporte = _Transporte()

    asyncio.run(cliente.post("/v1/options/tool/net-flow", {}))

    assert transporte.ultimo_plazo_s == pytest.approx(7.5)


def test_un_plazo_agotado_dice_QUE_plazo_expiro():
    """Sin el número, «este endpoint está muerto» y «se le dieron cinco segundos
    y necesita doce» se leen igual, y son dos arreglos opuestos."""
    cliente = QuantDataClient(_settings())
    cliente._client = _Transporte(revienta=httpx.ReadTimeout("boom"))

    with pytest.raises(QuantDataTimeout) as caja:
        asyncio.run(cliente.post("/v1/options/tool/dark-flow", {}, timeout=6.0))

    assert caja.value.limit_seconds == pytest.approx(6.0)
    # El prefijo se conserva: hay clasificadores que lo leen.
    assert "Quant Data request timed out" in str(caja.value)
    assert "6.0s" in str(caja.value)


def test_el_timeout_es_reconocible_por_TIPO_no_por_texto():
    assert issubclass(QuantDataTimeout, QuantDataError)


# ═══════════════════════════════════════════════════════════════════════════
# 2 · UN TIMEOUT ENSEÑA ALGO: EL POZO TIENE SALIDA
# ═══════════════════════════════════════════════════════════════════════════

def test_un_endpoint_lento_ya_no_se_queda_clavado_en_el_plazo_corto():
    """El defecto exacto: llamado con 5 s, necesita más, y nunca subía."""
    rt = EndpointRuntime("dark_flow", default_timeout=5.0)

    for _ in range(ER.MIN_SAMPLES_TO_CALIBRATE):
        rt.record_timeout(rt.timeout())

    assert rt.calibrated() is True
    assert rt.timeout() > 5.0, (
        "sin esto no dejaba muestra, no calibraba nunca, y se le seguía llamando "
        "con cinco segundos para siempre")
    assert rt.timeout() <= ER.TIMEOUT_CEILING_S


def test_el_plazo_sube_acotado_por_el_techo_publicado():
    rt = EndpointRuntime("interval_map_charm", default_timeout=5.0)
    for _ in range(80):
        rt.record_timeout(rt.timeout())
    assert rt.timeout() == pytest.approx(ER.TIMEOUT_CEILING_S)


def test_subir_el_plazo_no_es_llamar_para_siempre():
    """Un endpoint que sólo da timeouts tiene que acabar abriendo el circuito."""
    rt = EndpointRuntime("equity_prints", default_timeout=5.0)
    for _ in range(ER.FAILURE_THRESHOLD):
        rt.record_timeout(5.0)
    assert rt.snapshot()["breaker"] == OPEN
    assert rt.allow()[0] is False


def test_una_respuesta_real_vuelve_a_bajar_el_plazo():
    """La cota inferior es provisional: manda la latencia medida cuando existe."""
    rt = EndpointRuntime("gex_by_strike", default_timeout=5.0)
    for _ in range(10):
        rt.record_timeout(12.0)
    alto = rt.timeout()
    for _ in range(ER.LATENCY_WINDOW):
        rt.record_success(0.2)
    assert rt.timeout() < alto
    assert rt.timeout() == pytest.approx(ER.TIMEOUT_FLOOR_S)


def test_un_fallo_que_NO_es_de_plazo_sigue_sin_tocar_las_latencias():
    """Un 500 o un 404 no dicen nada sobre cuánto tarda el endpoint."""
    rt = EndpointRuntime("news", default_timeout=5.0)
    for _ in range(20):
        rt.record_failure("Quant Data HTTP 500", status="PROVIDER_ERROR")
    assert rt.calibrated() is False
    assert rt.timeout() == pytest.approx(5.0)


def test_los_timeouts_se_publican_para_poder_leerlos_en_el_auditor():
    rt = EndpointRuntime("max_pain", default_timeout=5.0)
    rt.record_timeout(5.0)
    rt.record_timeout(5.0)
    assert rt.snapshot()["timeouts"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# 3 · UNA SOLA AUTORIDAD PARA EL PLAZO CONFIGURADO
# ═══════════════════════════════════════════════════════════════════════════

def test_el_plazo_configurado_entra_en_el_registro_ya_creado():
    """El registro se construye al importar, antes de que existan las settings."""
    reg = EndpointRegistry(default_timeout=10.0)
    rt = reg.get("dark_pool_levels")
    assert rt.timeout() == pytest.approx(10.0)

    reg.set_default_timeout(12.0)

    assert rt.timeout() == pytest.approx(12.0), "el ya creado se queda descolgado"
    assert reg.get("otro").timeout() == pytest.approx(12.0)


def test_el_plazo_configurado_respeta_el_suelo():
    reg = EndpointRegistry(default_timeout=10.0)
    reg.set_default_timeout(0.1)
    assert reg.get("x").timeout() == pytest.approx(ER.TIMEOUT_FLOOR_S)


def test_los_dos_carriles_ponen_el_plazo_configurado_al_arrancar():
    """Cualquiera de los dos puede arrancar primero; el que arranque lo deja puesto."""
    for ruta in ("app/providers/quantdata/runtime.py",
                 "app/providers/quantdata/intelligence.py"):
        src = _texto(ruta)
        bloque = src[src.index("async def start(self, symbol: str)"):]
        bloque = bloque[:bloque.index("self._wake.set()")]
        assert ("ENDPOINT_RUNTIME.set_default_timeout("
                "self.settings.request_timeout_seconds)") in bloque, ruta


def test_los_dos_carriles_comparten_el_registro():
    """Los dos hablan con los mismos endpoints: el plazo medido es el mismo.

    Se comprueba sobre el CÓDIGO y no con `is`: varias pruebas de la suite hacen
    `importlib.reload`, que deja dos copias de un módulo vivas a la vez, y con
    ellas la identidad deja de significar lo que aquí importa —que ninguno de
    los dos carriles se fabrique su propio registro—.
    """
    for ruta in ("app/providers/quantdata/intelligence.py",
                 "app/providers/quantdata/runtime.py"):
        src = _texto(ruta)
        assert "ENDPOINT_RUNTIME" in src, ruta
        assert "EndpointRegistry(" not in src, (
            f"{ruta} se fabrica su propio registro: dos plazos para un endpoint")
    assert "ENDPOINT_RUNTIME = EndpointRegistry(" in _texto(
        "app/providers/quantdata/shared.py")


def test_el_carril_del_motor_no_mezcla_sus_latencias_con_las_de_las_paginas():
    src = _texto("app/providers/quantdata/runtime.py")
    assert 'ENDPOINT_RUNTIME.get(f"engine:{name}")' in src


# ═══════════════════════════════════════════════════════════════════════════
# 4 · EL CICLO ESPERA UN PELO MÁS QUE LA PETICIÓN
# ═══════════════════════════════════════════════════════════════════════════

def test_la_holgura_del_ciclo_es_positiva():
    assert CHANNEL_SLACK_S > 0, (
        "si el ciclo se rinde antes que la petición, el mismo fallo se cuenta "
        "dos veces y la huérfana sigue gastando conexión y cuota")


def test_el_carril_de_paginas_da_la_holgura_al_ciclo_no_a_la_peticion():
    src = _texto("app/providers/quantdata/intelligence.py")
    assert "_client.post(path, body, timeout=_plazo)" in src
    assert "timeout_s=_plazo + CHANNEL_SLACK_S," in src


def test_el_carril_del_motor_da_la_holgura_al_ciclo_no_a_la_peticion():
    src = _texto("app/providers/quantdata/runtime.py")
    assert "self._post(path, payload_body, timeout=plazo)" in src
    assert "timeout_s=plazo + CHANNEL_SLACK_S)" in src


def test_el_carril_del_motor_aprende_de_sus_timeouts():
    src = _texto("app/providers/quantdata/runtime.py")
    assert "rt.record_timeout(" in src
    assert "rt.record_success(time.monotonic() - t0)" in src


def test_el_ultimo_valor_bueno_no_se_anota_como_latencia_de_exito():
    """`ready` no significa «respondió»: el dato sirve y la llamada falló.

    Anotarlo como éxito metía una latencia de microsegundos en la calibración y
    hundía el plazo del endpoint justo cuando va lento.
    """
    src = _texto("app/providers/quantdata/runtime.py")
    bloque = src[src.index("async def _channel(name: str):"):]
    bloque = bloque[:bloque.index("jobs = {")]
    assert 'if gate.get("source") == "LIVE":' in bloque
    assert bloque.index('gate.get("source") == "LIVE"') < bloque.index("record_success")


def test_el_carril_de_paginas_aprende_de_sus_timeouts():
    src = _texto("app/providers/quantdata/intelligence.py")
    assert "isinstance(exc, QuantDataTimeout)" in src
    assert "record_timeout(" in src


def test_un_fallo_tardio_no_se_cuenta_como_una_segunda_degradacion():
    """El ciclo ya lo contó al agotarse el plazo del canal.

    Con el aviso duplicado, /health enseñaba dos incidencias donde había una, y
    la del huérfano se leía como avería del proveedor aunque el ciclo la hubiera
    dado por perdida a propósito.
    """
    obs.reset_degradations()
    try:
        async def escenario():
            iso = ChannelIsolator(timeout_s=0.05)

            async def tarda_y_revienta():
                await asyncio.sleep(0.15)
                raise QuantDataTimeout("Quant Data request timed out after 0.1s")

            res = await iso.call("dark_flow", tarda_y_revienta)
            assert res["timeout"] is True
            # Se deja terminar a la huérfana: es la que soltaba el segundo aviso.
            await asyncio.sleep(0.25)

        asyncio.run(escenario())
        sitios = {r["site"]: r for r in obs.degradations()["sites"]}
        tardio = sitios.get("data_hub:dark_flow:late")
        assert tardio is not None, "el texto del error tiene que conservarse"
        assert tardio["severity"] == "OPTIONAL", (
            "el fallo ya lo contó el canal; repetirlo como DEGRADED lo duplica")
    finally:
        obs.reset_degradations()


# ═══════════════════════════════════════════════════════════════════════════
# 5 · LO QUE SE REPARTE AL INSTALAR
# ═══════════════════════════════════════════════════════════════════════════

def test_el_plazo_que_se_reparte_cabe_en_el_ciclo_del_motor():
    """5 s cortaba los endpoints pesados en cada ciclo; 12 s + holgura < 15 s."""
    import re

    for ruta, patron in ((".env.example",
                          r"^QUANTDATA_TIMEOUT_SECONDS=([\d.]+)\s*$"),
                         ("setup_local_gui.py",
                          r'"QUANTDATA_TIMEOUT_SECONDS":\s*"([\d.]+)"')):
        halladas = re.findall(patron, _texto(ruta), flags=re.MULTILINE)
        assert len(halladas) == 1, f"{ruta}: {len(halladas)} valores, se espera 1"
        valor = float(halladas[0])
        assert 8.0 <= valor <= 14.0, f"{ruta} reparte un plazo de {valor} s"
        assert valor + CHANNEL_SLACK_S < 15.0, (
            "el plazo más la holgura tienen que caber en el ciclo del motor")


def test_el_plazo_por_defecto_del_codigo_coincide_con_el_que_se_reparte():
    import os

    from app.providers.quantdata.settings import load_settings

    previo = os.environ.pop("QUANTDATA_TIMEOUT_SECONDS", None)
    try:
        assert load_settings().request_timeout_seconds == pytest.approx(12.0)
    finally:
        if previo is not None:
            os.environ["QUANTDATA_TIMEOUT_SECONDS"] = previo

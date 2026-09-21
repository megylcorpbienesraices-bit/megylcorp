"""v1.57.7 · LOS DOS ERRORES DEL ARRANQUE, CONTRA EL CONTRATO OFICIAL.

La consola del motor daba dos fallos que no eran del proveedor sino de nuestras
dos integraciones:

    HTTP 400  data_hub:max_pain  ·  Request validation failed
    HTTP 404  data_hub:trade_side_statistics
              No resource found at 'v1/options/tool/trade-side-statistics'

La documentación oficial de Quant Data los explica los dos:

MAX PAIN · `/v1/options/tool/max-pain`
    Obligatorios `filter.ticker` Y `filter.expirationDate`. `sessionDate` es
    opcional y no admite campos ajenos. Enviábamos sólo el ticker: de ahí el 400.

    Para el max pain de TODOS los vencimientos existe otro endpoint,
    `/v1/options/tool/max-pain-over-time`, que sólo pide el ticker. Es el que ya
    usa el carril del motor.

TRADE SIDE · la ruta estaba mal
    No es `/v1/options/tool/trade-side-statistics` —no existe— sino
    `/v1/options/tool/contract-trade-side-statistics`, y exige `dataMode`, que
    sólo admite PREMIUM, TRADE_COUNT o VOLUME.

Lo importante del arreglo de max-pain: el vencimiento NO se inventa. Sale de la
ventana de vencimientos que la terminal ya aplicó a la cadena. Si no hay, la
herramienta dice qué le falta en vez de mandar un cuerpo que el proveedor va a
rechazar. Un max pain del vencimiento equivocado es un número creíble y falso,
que es peor que no tener número.
"""
from __future__ import annotations

import pathlib

import pytest

import app.providers.quantdata.shared as _shared
from app.providers.quantdata.tools import (
    FaltaRequisito, TRADE_SIDE_DATA_MODE, TRADE_SIDE_DATA_MODES, build_catalog,
)


class _Registro:
    """El registro se toma del MÓDULO, no por referencia.

    Otra prueba de la suite hace `importlib.reload(shared)` y deja dos registros
    vivos: el nuevo en `shared` y el viejo, que es el que conserva quien lo
    importó antes. Escribir en uno y leer del otro daba verde o rojo según el
    orden de ejecución.
    """

    def __getattr__(self, nombre):
        return getattr(_shared.EXPIRY_SELECTION, nombre)


EXPIRY_SELECTION = _Registro()

VENC = "2026-09-21"


@pytest.fixture()
def catalogo():
    EXPIRY_SELECTION.clear_symbol("DIA")
    yield build_catalog()
    EXPIRY_SELECTION.clear_symbol("DIA")


# ═══════════════════════════════════════════════════════════════════════════
# 1 · MAX PAIN · EL CUERPO QUE EL CONTRATO EXIGE
# ═══════════════════════════════════════════════════════════════════════════

def test_la_ruta_de_max_pain_es_la_oficial(catalogo):
    assert catalogo["max_pain"].paths == ("/v1/options/tool/max-pain",)


def test_con_vencimiento_el_cuerpo_lleva_los_DOS_obligatorios(catalogo):
    EXPIRY_SELECTION.publicar("DIA", [VENC])
    body = catalogo["max_pain"].request_body("DIA")
    assert body == {"filter": {"ticker": "DIA", "expirationDate": VENC}}


def test_el_cuerpo_NO_lleva_campos_ajenos(catalogo):
    """El endpoint rechaza lo que no es suyo: eso era el 400."""
    EXPIRY_SELECTION.publicar("DIA", [VENC])
    body = catalogo["max_pain"].request_body("DIA")
    for ajeno in ("aggregationPeriod", "pagination", "snapshotTime", "limit",
                  "timeRange", "projection", "sort", "lookBackPeriod"):
        assert ajeno not in body, ajeno
    assert set(body) == {"filter"}
    assert set(body["filter"]) == {"ticker", "expirationDate"}


def test_SIN_vencimiento_no_se_manda_nada_y_se_dice_que_falta(catalogo):
    """Ni se inventa una fecha ni se dispara un 400 predecible."""
    with pytest.raises(FaltaRequisito) as exc:
        catalogo["max_pain"].request_body("DIA")
    assert exc.value.campo == "filter.expirationDate"
    assert "max-pain-over-time" in exc.value.detalle, (
        "hay que decir cuál es la alternativa legítima")


def test_el_vencimiento_sale_de_la_ventana_de_la_terminal(catalogo):
    """Y es el más cercano de los que están en pantalla, no uno cualquiera."""
    EXPIRY_SELECTION.publicar("DIA", ["2026-12-18", VENC, "2026-10-16"])
    body = catalogo["max_pain"].request_body("DIA")
    assert body["filter"]["expirationDate"] == VENC


def test_el_vencimiento_de_OTRO_activo_no_sirve(catalogo):
    EXPIRY_SELECTION.publicar("SPY", [VENC])
    with pytest.raises(FaltaRequisito):
        catalogo["max_pain"].request_body("DIA")
    EXPIRY_SELECTION.clear_symbol("SPY")


def test_un_vencimiento_viejo_CADUCA(catalogo):
    """Publicado hace un cuarto de hora puede ser de otra sesión."""
    import time
    EXPIRY_SELECTION.publicar("DIA", [VENC])
    assert EXPIRY_SELECTION.principal("DIA") == VENC
    assert EXPIRY_SELECTION.principal("DIA", max_age_s=-1.0) is None


def test_una_fecha_con_formato_raro_NO_entra(catalogo):
    EXPIRY_SELECTION.publicar("DIA", ["mañana", "21/09/2026", ""])
    assert EXPIRY_SELECTION.principal("DIA") is None


def test_max_pain_over_time_sigue_pidiendo_solo_el_ticker(catalogo):
    """La alternativa legítima para todos los vencimientos."""
    t = catalogo["max_pain_over_time"]
    assert "max-pain-over-time" in t.paths[0]
    assert t.request_body("DIA") == {"filter": {"ticker": "DIA"}}


def test_el_carril_del_motor_usa_el_endpoint_SIN_vencimiento():
    src = pathlib.Path("app/providers/quantdata/runtime.py").read_text("utf-8")
    assert '"max_pain": ("/v1/options/tool/max-pain-over-time"' in src


# ═══════════════════════════════════════════════════════════════════════════
# 2 · TRADE SIDE · LA RUTA Y EL `dataMode` OFICIALES
# ═══════════════════════════════════════════════════════════════════════════

def test_la_ruta_vieja_NO_puede_volver():
    """404: no existe. Si reaparece, la suite cae."""
    for f in ("app/providers/quantdata/tools.py",
              "app/providers/quantdata/intelligence.py",
              "app/core/quant_data_hub.py",
              "app/terminal_api.py",
              "scripts/certificar_live.py"):
        txt = pathlib.Path(f).read_text("utf-8")
        for linea in txt.splitlines():
            if linea.strip().startswith("#"):
                continue
            assert "/v1/options/tool/trade-side-statistics" not in linea, (f, linea)


def test_la_ruta_oficial_esta_en_el_catalogo(catalogo):
    t = catalogo["contract_trade_side_statistics"]
    assert t.paths == ("/v1/options/tool/contract-trade-side-statistics",)


def test_la_clave_vieja_ya_no_existe(catalogo):
    assert "trade_side_statistics" not in catalogo


def test_el_cuerpo_lleva_dataMode_obligatorio(catalogo):
    body = catalogo["contract_trade_side_statistics"].request_body("DIA")
    assert body == {"dataMode": "PREMIUM", "filter": {"ticker": "DIA"}}


def test_el_dataMode_es_uno_de_los_tres_del_contrato():
    assert TRADE_SIDE_DATA_MODES == ("PREMIUM", "TRADE_COUNT", "VOLUME")
    assert TRADE_SIDE_DATA_MODE in TRADE_SIDE_DATA_MODES


def test_se_pide_PREMIUM_porque_es_lo_que_el_panel_DIBUJA():
    """Pedir TRADE_COUNT y dibujarlo como prima mezcla dos magnitudes."""
    assert TRADE_SIDE_DATA_MODE == "PREMIUM"
    api = pathlib.Path("app/terminal_api.py").read_text("utf-8")
    bloque = api.split('_qd_rows(intel, "contract_trade_side_statistics")', 1)[1][:500]
    assert 'r.get("premium")' in bloque, (
        "el panel dejó de consumir prima; revisa el dataMode que se pide")


@pytest.mark.parametrize("fichero,aguja", [
    ("app/core/quant_data_hub.py", '"contract_trade_side_statistics": "QD_TRADE_SIDE_STATISTICS"'),
    ("app/terminal_api.py", '_qd_rows(intel, "contract_trade_side_statistics")'),
    ("scripts/certificar_live.py", '"contract_trade_side_statistics": {"modulo"'),
    ("app/providers/quantdata/intelligence.py", '"contract_trade_side_statistics": 3'),
])
def test_el_renombrado_llego_a_todos_los_consumidores(fichero, aguja):
    assert aguja in pathlib.Path(fichero).read_text("utf-8"), fichero


# ═══════════════════════════════════════════════════════════════════════════
# 3 · «FALTA UN REQUISITO» NO ES «SIN DATOS» NI UN FALLO DEL PROVEEDOR
# ═══════════════════════════════════════════════════════════════════════════

def test_el_programador_no_llama_cuando_falta_el_requisito():
    src = pathlib.Path("app/providers/quantdata/intelligence.py").read_text("utf-8")
    bloque = src.split("for _attempt in range(3):", 1)[1].split("outcome, detail =", 1)[0]
    assert "except FaltaRequisito" in bloque
    assert "REQUISITO_AUSENTE" in bloque
    assert "return" in bloque, "tiene que cortar, no seguir al proveedor"


def test_el_estado_dice_QUE_campo_falta():
    src = pathlib.Path("app/providers/quantdata/intelligence.py").read_text("utf-8")
    bloque = src.split("except FaltaRequisito as falta:", 1)[1][:900]
    assert '"lane_fields": [falta.campo]' in bloque
    assert '"state": "REQUISITO_AUSENTE"' in bloque


def test_NO_se_disfraza_de_sin_datos():
    """El operador pidió esto explícitamente: nada de esconder el 400 como SIN_DATOS."""
    src = pathlib.Path("app/providers/quantdata/intelligence.py").read_text("utf-8")
    bloque = src.split("except FaltaRequisito as falta:", 1)[1][:900]
    assert "NO_PROVIDER_DATA" not in bloque
    assert '"ready": False' in bloque


# ═══════════════════════════════════════════════════════════════════════════
# 4 · EL PUENTE DESDE LA TERMINAL, SIN INVENTAR FECHAS
# ═══════════════════════════════════════════════════════════════════════════

def test_la_terminal_publica_sus_vencimientos_reales():
    from app.service import _publicar_vencimientos

    _publicar_vencimientos("DIA", {"expirations": ["2026-10-16", VENC]})
    assert EXPIRY_SELECTION.principal("DIA") == VENC
    EXPIRY_SELECTION.clear_symbol("DIA")


def test_sin_ventana_resuelta_no_se_publica_nada():
    from app.service import _publicar_vencimientos

    _publicar_vencimientos("DIA", {})
    assert EXPIRY_SELECTION.principal("DIA") is None
    _publicar_vencimientos("DIA", None)
    assert EXPIRY_SELECTION.principal("DIA") is None


def test_el_puente_no_revienta_con_basura():
    from app.service import _publicar_vencimientos

    _publicar_vencimientos("DIA", {"expirations": "no es una lista"})
    _publicar_vencimientos("", {"expirations": [VENC]})
    EXPIRY_SELECTION.clear_symbol("DIA")


def test_el_cambio_de_activo_retira_los_vencimientos_del_anterior():
    src = pathlib.Path("app/service.py").read_text("utf-8")
    assert src.count("_qd_expiry_selection().clear_symbol(_sym)") == 2, (
        "los dos reseteos de símbolo tienen que retirar los vencimientos")


def test_la_publicacion_cuelga_de_la_ventana_ya_aplicada():
    """Que salga de `expiry_info` y no de una fecha calculada aparte."""
    src = pathlib.Path("app/service.py").read_text("utf-8")
    assert "_publicar_vencimientos(self.symbol, expiry_info)" in src

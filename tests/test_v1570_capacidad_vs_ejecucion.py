"""v1.57.0 · CAPACIDAD ≠ EJECUCIÓN, y un fallo de llamada no borra el dato.

Tres defectos de la misma familia, todos visibles en el Auditor en vivo:

    Dark Flow | PROVIDER_ERROR | 216 filas

El carril decía «roto» y al lado contaba 216 filas. Dos afirmaciones
contradictorias sobre el mismo dato, y ninguna forma de saber cuál creer.

1. `classify()` devolvía `rows: 0` SIEMPRE que hubiera `error`, así que aguas
   abajo nadie podía decidir usar el último valor bueno: desde su punto de
   vista no había nada que usar.
2. Que una herramienta EXISTA y esté habilitada no autoriza a decir que su dato
   es de ahora; y que su llamada falle ahora no la convierte en inexistente.
3. Un proveedor que responde bien y no trae filas porque no hubo actividad no
   es un proveedor roto.
"""
from __future__ import annotations

import pytest

from app.core import data_lineage as DL
from app.core.quant_data_hub import classify
from app.core import dark_pool_state as DPS


# ═══════════════════════════════════════════════════════════════════════════
# 1 · UN FALLO DE LLAMADA NO BORRA LAS FILAS
# ═══════════════════════════════════════════════════════════════════════════

def test_un_error_ya_no_pone_el_recuento_a_cero():
    """El caso exacto de la captura: 216 filas y el plazo agotado."""
    c = classify({"rows": [{"i": i} for i in range(216)],
                  "error": "el canal tardó más de 6.0 s", "ready": False})
    assert c["state"] == DL.PROVIDER_ERROR, "el fallo de la llamada se sigue diciendo"
    assert c["rows"] == 216, "las filas que hay se cuentan; antes salía 0"
    assert c["has_payload"] is True


def test_un_error_sin_filas_sigue_siendo_cero():
    c = classify({"rows": [], "error": "403 no autorizado", "ready": False})
    assert c["state"] == DL.PROVIDER_ERROR
    assert c["rows"] == 0 and c["has_payload"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 2 · CAPACIDAD Y EJECUCIÓN SON DOS PREGUNTAS
# ═══════════════════════════════════════════════════════════════════════════

def test_una_llamada_fallida_no_convierte_el_endpoint_en_inexistente():
    c = classify({"rows": [{"i": 1}], "error": "timeout", "ready": False})
    assert c["capability"] == DL.CAPABILITY_AVAILABLE, (
        "el endpoint existe y estaba respondiendo: lo que falló es ESTA llamada")
    assert c["state"] == DL.PROVIDER_ERROR


def test_un_bloque_que_el_proveedor_no_publica_si_es_capacidad_ausente():
    assert classify(None)["capability"] == DL.CAPABILITY_UNAVAILABLE
    assert classify("no soy un dict")["capability"] == DL.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize("bloque", [
    {"rows": [{"i": 1}], "ready": True},
    {"rows": [], "ready": True},
    {"rows": [], "ready": False, "reason": "sin actividad"},
    {"rows": [{"i": 1}], "ready": False, "reason": "ninguna utilizable"},
])
def test_toda_respuesta_del_proveedor_declara_su_capacidad(bloque):
    c = classify(bloque)
    assert c["capability"] in DL.CAPABILITIES
    assert "has_payload" in c


def test_las_dos_dimensiones_no_comparten_vocabulario():
    """Si compartieran nombres volverían a confundirse en cuanto alguien lea rápido."""
    assert set(DL.CAPABILITIES).isdisjoint(set(DL.DATA_STATES))


# ═══════════════════════════════════════════════════════════════════════════
# 3 · CERO FILAS NO ES UN ERROR
# ═══════════════════════════════════════════════════════════════════════════

def test_el_proveedor_que_responde_sin_actividad_no_es_un_proveedor_roto():
    c = classify({"rows": [], "ready": False, "reason": "sin operaciones en la ventana"})
    assert c["state"] == DL.NO_PROVIDER_DATA
    assert c["state"] != DL.PROVIDER_ERROR
    assert c["capability"] == DL.CAPABILITY_AVAILABLE
    assert "sin operaciones" in c["detail"]


# ═══════════════════════════════════════════════════════════════════════════
# 4 · EL CARRIL: DATO VIEJO EN VEZ DE SECCIÓN ROTA
# ═══════════════════════════════════════════════════════════════════════════

def _carril(rows, *, estado=DL.PROVIDER_ERROR, detalle="el canal tardó más de 6.0 s",
            provider_status="", clasificadas=None):
    cls = {"state": estado, "rows": rows, "detail": detalle,
           "has_payload": rows > 0, "capability": DL.CAPABILITY_AVAILABLE}
    block = {"lane_detail": detalle}
    if provider_status:
        block["lane_status"] = provider_status
    return DPS.lane_state(block, cls,
                          classified=(rows if clasificadas is None else clasificadas),
                          unclassified=0, market_open=True)


def test_un_carril_con_dato_bueno_y_plazo_agotado_no_rompe_la_seccion():
    v = _carril(216)
    assert v["state"] == DPS.STALE
    assert v["is_failure"] is False, "216 filas buenas no son una sección rota"
    assert v["rows"] == 216


def test_pero_el_fallo_de_la_llamada_no_se_esconde():
    """Bajar la severidad sin conservar la causa sería taparlo, no corregirlo."""
    v = _carril(216)
    assert v["call_failed"] is True
    assert "6.0 s" in v["last_error"]
    assert v["capability"] == DL.CAPABILITY_AVAILABLE


def test_un_carril_que_falla_sin_dato_guardado_sigue_siendo_un_fallo():
    v = _carril(0)
    assert v["state"] == DPS.PROVIDER_ERROR
    assert v["is_failure"] is True


def test_un_400_no_se_rebaja_aunque_haya_filas_antiguas():
    """Reintentar un cuerpo mal formado no lo arregla: ése sí es fallo nuestro."""
    v = _carril(216, provider_status="REQUEST_INVALID")
    assert v["state"] == DPS.REQUEST_INVALID
    assert v["is_failure"] is True


def test_un_carril_sano_no_se_ve_afectado():
    v = _carril(216, estado=DL.DATA_OK, detalle="")
    assert v["state"] == DPS.DIRECT_PROVIDER_OK
    assert v["call_failed"] is False and v["last_error"] == ""


def test_un_carril_roto_no_arrastra_a_los_otros_dos():
    """Un fallo de Dark Pool no puede bloquear al resto de la sección."""
    lanes = {
        "dark_flow": _carril(216),                       # viejo, pero con dato
        "dark_pool_levels": _carril(169, estado=DL.DATA_OK, detalle=""),
        "equity_prints": _carril(0, estado=DL.NO_PROVIDER_DATA,
                                 detalle="el proveedor respondió sin filas"),
    }
    sec = DPS.section_state(lanes)
    assert sec["state"] == DPS.DIRECT_PROVIDER_OK, (
        "un carril sano basta para que la sección tenga dato")


# ═══════════════════════════════════════════════════════════════════════════
# 5 · RECUPERACIÓN DESPUÉS DEL FALLO
# ═══════════════════════════════════════════════════════════════════════════

def test_el_carril_vuelve_a_LIVE_cuando_el_proveedor_se_recupera():
    """Un estado del que no se sale es una avería permanente disfrazada."""
    secuencia = [
        (_carril(216, estado=DL.DATA_OK, detalle=""), DPS.DIRECT_PROVIDER_OK),
        (_carril(216), DPS.STALE),                       # se agota el plazo
        (_carril(0), DPS.PROVIDER_ERROR),                # y además se pierde el dato
        (_carril(240, estado=DL.DATA_OK, detalle=""), DPS.DIRECT_PROVIDER_OK),
    ]
    for v, esperado in secuencia:
        assert v["state"] == esperado
    assert secuencia[-1][0]["call_failed"] is False
    assert secuencia[-1][0]["is_failure"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 6 · DELTA / MIN NO DEPENDE DE LA CADENA
# ═══════════════════════════════════════════════════════════════════════════

def test_delta_min_funciona_con_el_motor_completamente_vacio():
    """Los prints vienen de Order Flow, no de la cadena de opciones.

    Un fallo de hidratación de la cadena apaga GEX, DEX, OI, muros y skew.
    NO puede apagar DELTA/MIN: sus cuatro entradas —delta, tamaño, spot y
    dirección— viajan en el propio print.
    """
    from app.terminal_api import _delta_min_block
    rows = [{"t": "2026-09-21T14:30:00Z", "option_type": "CALL", "size": 40,
             "direction": 1, "spot": 520.0, "greeks": {"delta": 0.55}}]
    # Estado del motor VACÍO: sin cadena, sin gamma_delta, sin trace.
    d = _delta_min_block("SPY", rows, {})
    assert d["ready"] is True
    assert d["series"][0]["dealer_delta_dollars"] == pytest.approx(-1_144_000.0)


def test_delta_min_no_lee_ninguna_estructura_de_la_cadena():
    from pathlib import Path
    src = Path("app/core/delta_flow.py").read_text(encoding="utf-8")
    for prohibido in ("gamma_delta", "chain", "snapshot", "open_interest", "gex"):
        assert prohibido not in src.lower().replace("stockprice", ""), (
            f"{prohibido}: DELTA/MIN no puede depender de la cadena")


def test_delta_min_declara_que_sus_entradas_son_del_print():
    from app.core.delta_flow import build_delta_flow
    d = build_delta_flow([], symbol="SPY")
    assert d["inputs"] == ["QD_OPTION_FLOW.delta", "QD_OPTION_FLOW.size",
                           "QD_OPTION_FLOW.stockPrice", "ITMQ_AGGRESSOR.direction"]


@pytest.mark.parametrize("sym", ["DIA", "SPY", "QQQ"])
def test_delta_min_multiactivo_sin_cadena(sym):
    from app.terminal_api import _delta_min_block
    rows = [{"t": "2026-09-21T14:30:00Z", "option_type": "CALL", "size": 10,
             "direction": 1, "spot": 400.0, "greeks": {"delta": 0.5}},
            {"t": "2026-09-21T14:31:00Z", "option_type": "PUT", "size": 10,
             "direction": -1, "spot": 400.0, "greeks": {"delta": -0.5}}]
    d = _delta_min_block(sym, rows, {})
    assert d["ready"] is True and d["buckets"] == 2
    assert d["symbol"] == sym
    assert d["coverage"]["pct"] == pytest.approx(100.0)

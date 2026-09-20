"""GATE 1 · BUY/SELL forense. Puntos 1, 43 y 44 del cierre integral.

LA REGLA, CON SU ORDEN DE AUTORIDAD
-----------------------------------
    1. tradeSideCode        el campo OFICIAL. Si viene, decide y se acabó.
    2. otro campo de lado   declarado por el proveedor
    3. NBBO                 medición, no heurística
    4. UNKNOWN              y se dice por qué

    ASK / ABOVE_ASK   -> BUY
    BID / BELOW_BID   -> SELL
    MID_MARKET        -> UNKNOWN

NUNCA `CALL = BUY` ni `PUT = SELL`. Una put se compra y eso es una COMPRA.

POR QUÉ EL PASO 1 VA APARTE DEL 2
---------------------------------
Si `tradeSideCode` dice `MID_MARKET`, la respuesta es UNKNOWN y **no se cae al
NBBO**. El proveedor ya ha dicho que nadie cruzó el spread; volver a
preguntárselo al precio es discutirle el dato oficial hasta que conteste lo que
queremos oír. Eso es exactamente cómo se fabrica una flecha inventada.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.aggressor import (SRC_FIELD, SRC_NBBO, SRC_NONE, SRC_PRIMARY,
                                WHY_MID, WHY_NBBO_MISSING, WHY_NO_SIDE_FIELD,
                                WHY_OK, classify_trade)
from app.core.aggressor_evidence import build_evidence, expected_side
from app.providers.quantdata.tools import norm_option_order_flow


def _t(code=None, price=2.47, bid=2.45, ask=2.50, **extra):
    r = {"timestamp": "2026-09-19T14:00:00Z", "optionPrice": price,
         "bidPrice": bid, "askPrice": ask, **extra}
    if code is not None:
        r["tradeSideCode"] = code
    return r


# ── 1 · El campo oficial manda ───────────────────────────────────────────

@pytest.mark.parametrize("code,esperado", [
    ("ASK", "BUY"), ("AT_ASK", "BUY"), ("ABOVE_ASK", "BUY"),
    ("BID", "SELL"), ("AT_BID", "SELL"), ("BELOW_BID", "SELL"),
    ("MID_MARKET", "UNKNOWN"), ("MID", "UNKNOWN"), ("MIDPOINT", "UNKNOWN"),
])
def test_el_codigo_oficial_se_traduce_segun_el_contrato(code, esperado):
    v = classify_trade(_t(code))
    assert v["aggressor"] == esperado
    assert v["classification_source"] == SRC_PRIMARY


def test_at_bid_es_venta_y_no_compra():
    """El defecto historico: `startswith("A")` clasificaba AT_BID como COMPRA
    porque empieza por A. Es la inversion que no puede ocurrir."""
    assert classify_trade(_t("AT_BID"))["aggressor"] == "SELL"
    assert classify_trade(_t("ABOVE_BID"))["aggressor"] == "SELL"


def test_mid_market_no_cae_al_nbbo_aunque_el_precio_toque_el_ask():
    """El proveedor ya dijo que nadie cruzo. Preguntarselo al precio es
    discutirle el dato oficial hasta que conteste lo que queremos oir."""
    v = classify_trade(_t("MID_MARKET", price=2.50, bid=2.45, ask=2.50))
    assert v["aggressor"] == "UNKNOWN"
    assert v["classification_source"] == SRC_PRIMARY
    assert v["why"] == WHY_MID


def test_el_codigo_oficial_gana_a_cualquier_otro_campo():
    v = classify_trade(_t("BID", side="AT_ASK", aggressor="BUY"))
    assert v["aggressor"] == "SELL"
    assert v["classification_source"] == SRC_PRIMARY
    assert v["side_field"] == "tradeSideCode"


# ── 2 · NBBO solo cuando falta el campo ──────────────────────────────────

def test_sin_codigo_se_mide_contra_el_nbbo():
    assert classify_trade(_t(None, price=2.50, bid=2.45, ask=2.50))["aggressor"] == "BUY"
    assert classify_trade(_t(None, price=2.45, bid=2.45, ask=2.50))["aggressor"] == "SELL"
    medio = classify_trade(_t(None, price=2.475, bid=2.45, ask=2.50))
    assert medio["aggressor"] == "UNKNOWN"
    assert medio["classification_source"] == SRC_NBBO
    assert medio["why"] == WHY_MID


def test_la_holgura_es_relativa_al_spread_no_en_dolares():
    """La MISMA distancia en dolares significa cosas opuestas segun el spread.

    Diez centimos por debajo del ask:
      · con spread de 0,05  ->  esta por debajo del bid: fue una VENTA
      · con spread de 10    ->  es el 1 % del spread: cruzo, fue una COMPRA

    Una holgura escrita en dolares daria el mismo veredicto a los dos, y uno de
    los dos estaria invertido.
    """
    estrecho = classify_trade(_t(None, price=2.40, bid=2.45, ask=2.50))
    ancho = classify_trade(_t(None, price=39.90, bid=30.0, ask=40.0))
    assert estrecho["aggressor"] == "SELL"
    assert ancho["aggressor"] == "BUY"
    # Y en el ancho, el punto medio sigue sin producir lado.
    assert classify_trade(_t(None, price=35.0, bid=30.0, ask=40.0))["aggressor"] == "UNKNOWN"


def test_sin_nbbo_utilizable_se_dice_por_que():
    v = classify_trade({"timestamp": "x", "optionPrice": 2.5})
    assert v["aggressor"] == "UNKNOWN"
    assert v["classification_source"] == SRC_NONE
    assert v["why"] == WHY_NO_SIDE_FIELD
    v2 = classify_trade({"timestamp": "x", "optionPrice": 2.5, "side": "CALL"})
    assert v2["why"] == WHY_NBBO_MISSING


def test_un_mercado_cruzado_no_produce_lado():
    assert classify_trade(_t(None, price=2.45, bid=2.50, ask=2.40))["aggressor"] == "UNKNOWN"


# ── 3 · CALL/PUT no son direcciones ──────────────────────────────────────

def test_el_tipo_de_contrato_nunca_decide_el_lado():
    for tipo in ("CALL", "PUT", "C", "P"):
        v = classify_trade({"timestamp": "x", "optionPrice": 2.5, "tradeSideCode": tipo})
        assert v["aggressor"] == "UNKNOWN", tipo


def test_una_put_comprada_es_una_compra():
    """El mapeo antiguo `PUT = SELL` la marcaba venta. Una put se compra."""
    r = _t("ASK", price=0.95, bid=0.90, ask=0.95, optionType="PUT", strike=490)
    assert classify_trade(r)["aggressor"] == "BUY"
    assert expected_side(r) == "BUY"


def test_una_call_vendida_es_una_venta():
    r = _t("BID", price=2.45, bid=2.45, ask=2.50, optionType="CALL", strike=500)
    assert classify_trade(r)["aggressor"] == "SELL"


# ── 4 · Los 17 campos forenses por operación ─────────────────────────────

FORENSES = ("trade_id", "tradeTime", "ticker", "option_symbol", "option_type",
            "strike", "expiration", "dte", "optionPrice", "bidPrice", "askPrice",
            "size", "premium", "trade_side_code", "aggressor",
            "classification_source", "classification_reason")


def test_cada_print_conserva_todo_lo_que_hace_falta_para_auditarlo():
    fila = norm_option_order_flow({"data": [{
        "id": "T-99", "timestamp": "2026-09-19T14:00:00Z", "osi": "QQQ260920C00500000",
        "ticker": "QQQ", "optionType": "CALL", "strike": 500, "expirationDate": "2026-09-20",
        "dte": 1, "price": 2.50, "size": 10, "premium": 2500.0,
        "bidPrice": 2.45, "askPrice": 2.50, "tradeSideCode": "ASK",
    }]})["rows"][0]
    faltan = [c for c in FORENSES if c not in fila]
    assert not faltan, f"sin estos campos la clasificacion no se puede auditar: {faltan}"
    assert fila["trade_id"] == "T-99"
    assert fila["option_symbol"] == "QQQ260920C00500000"
    assert fila["trade_side_code"] == "ASK"
    assert fila["classification_source"] == SRC_PRIMARY
    assert fila["classification_reason"] == WHY_OK


def test_el_nbbo_del_instante_viaja_con_la_fila():
    """Sin el, «esta marca dice compra» no se contrasta con nada."""
    fila = norm_option_order_flow({"data": [_t("ASK", price=2.5, bid=2.45, ask=2.5,
                                               timestamp="2026-09-19T14:00:00Z")]})["rows"][0]
    assert fila["bidPrice"] == pytest.approx(2.45)
    assert fila["askPrice"] == pytest.approx(2.50)
    assert fila["optionPrice"] == pytest.approx(2.50)


# ── 5 · Contadores exactos de la auditoría ───────────────────────────────

def _muestra():
    return [
        _t("ASK", timestamp="2026-09-19T14:00:00Z"),
        _t("BID", timestamp="2026-09-19T14:01:00Z"),
        _t("MID_MARKET", timestamp="2026-09-19T14:02:00Z"),
        _t(None, price=2.50, timestamp="2026-09-19T14:03:00Z"),
        _t(None, price=2.47, timestamp="2026-09-19T14:04:00Z"),
        {"timestamp": "2026-09-19T14:05:00Z", "optionPrice": 2.47},
    ]


def test_la_cobertura_separa_lo_declarado_de_lo_medido_y_de_lo_ausente():
    cov = norm_option_order_flow({"data": _muestra()})["aggressor_coverage"]
    assert cov["tape_rows"] == 6
    assert cov["explicit_side_rows"] == 2      # ASK y BID por campo oficial
    assert cov["nbbo_classified_rows"] == 1    # el que tocó el ask
    assert cov["mid_trade_rows"] == 2          # MID_MARKET declarado + medio por NBBO
    assert cov["without_side_field_rows"] == 1
    assert cov["buy_rows"] == 2 and cov["sell_rows"] == 1
    assert cov["unknown_rows"] == 3


def test_mid_trade_y_nbbo_missing_son_averias_distintas():
    """Una se cierra y la otra se persigue. Contarlas juntas mandaba al sitio
    equivocado: una cinta sana con muchas ejecuciones al medio parecia rota."""
    from app.core.qflow import aggressor_diagnosis
    solo_medio = [_t("MID_MARKET", timestamp=f"2026-09-19T14:0{i}:00Z") for i in range(5)]
    d = aggressor_diagnosis([], {"events": []}, norm_option_order_flow({"data": solo_medio}))
    assert d["state"] == "MID_TRADE"
    assert "No es una avería" in d["remedy"] or "no es una avería" in d["remedy"].lower()

    sin_nada = [{"timestamp": f"2026-09-19T14:0{i}:00Z", "optionPrice": 2.4} for i in range(3)]
    d2 = aggressor_diagnosis([], {"events": []}, norm_option_order_flow({"data": sin_nada}))
    assert d2["state"] == "TAPE_WITHOUT_SIDE"


def test_el_diagnostico_publica_los_contadores_con_los_nombres_exigidos():
    from app.core.qflow import aggressor_diagnosis
    d = aggressor_diagnosis([], {"events": []}, norm_option_order_flow({"data": _muestra()}))
    for k in ("tape_rows", "explicit_side_rows", "nbbo_classified_rows", "unknown_rows",
              "attribution_matches", "attribution_misses", "buy_rows", "sell_rows",
              "coverage_pct", "broken_at"):
        assert k in d, k
    assert set(d["states"]) == {"DATA_OK", "TAPE_MISSING", "TAPE_WITHOUT_SIDE",
                                "NBBO_MISSING", "MID_TRADE", "ATTRIBUTION_NO_MATCH",
                                "NO_DOMINANCE"}


# ── 6 · La tabla de evidencia (punto 44) ─────────────────────────────────

RAW = [
    {"id": "1", "timestamp": "2026-09-19T14:00:00Z", "osi": "QQQ260920C00500000",
     "optionType": "CALL", "strike": 500, "expiration": "2026-09-20",
     "price": 2.50, "size": 10, "premium": 2500.0,
     "bidPrice": 2.45, "askPrice": 2.50, "tradeSideCode": "ASK"},
    {"id": "2", "timestamp": "2026-09-19T14:01:00Z", "osi": "QQQ260920P00495000",
     "optionType": "PUT", "strike": 495, "expiration": "2026-09-20",
     "price": 1.20, "size": 8, "premium": 960.0,
     "bidPrice": 1.20, "askPrice": 1.25, "tradeSideCode": "BID"},
    {"id": "3", "timestamp": "2026-09-19T14:02:00Z", "osi": "QQQ260920P00490000",
     "optionType": "PUT", "strike": 490, "expiration": "2026-09-20",
     "price": 0.95, "size": 30, "premium": 2850.0,
     "bidPrice": 0.90, "askPrice": 0.95, "tradeSideCode": "ASK"},
    {"id": "4", "timestamp": "2026-09-19T14:03:00Z", "osi": "QQQ260920C00505000",
     "optionType": "CALL", "strike": 505, "expiration": "2026-09-20",
     "price": 1.12, "size": 5, "premium": 560.0,
     "bidPrice": 1.10, "askPrice": 1.15, "tradeSideCode": "MID_MARKET"},
]


def test_la_tabla_de_evidencia_cuadra_en_toda_la_cadena():
    norm = norm_option_order_flow({"data": RAW})["rows"]
    ev = build_evidence(RAW, norm)
    assert ev["ok"] is True, ev["mismatches"]
    assert ev["count"] == 4
    lados = {r["trade_id"]: r["classifier"] for r in ev["rows"]}
    assert lados == {"1": "BUY", "2": "SELL", "3": "BUY", "4": "UNKNOWN"}


def test_la_put_comprada_aparece_como_compra_en_la_tabla():
    norm = norm_option_order_flow({"data": RAW})["rows"]
    fila = next(r for r in build_evidence(RAW, norm)["rows"] if r["trade_id"] == "3")
    assert fila["contractType"] == "PUT"
    assert fila["expected"] == "BUY" and fila["classifier"] == "BUY"


def test_una_inversion_en_cualquier_etapa_hace_FALLAR_la_tabla():
    """Es la prueba de que la tabla sirve para algo: si el clasificador
    invirtiera un lado, tiene que saltar aqui."""
    norm = norm_option_order_flow({"data": RAW})["rows"]
    norm[0] = {**norm[0], "aggressor": "SELL"}       # inversion inyectada
    ev = build_evidence(RAW, norm)
    assert ev["ok"] is False
    assert ev["mismatch_count"] == 1
    malo = ev["mismatches"][0]
    assert malo["expected"] == "BUY" and malo["classifier"] == "SELL"


def test_una_marca_que_contradice_a_su_operacion_hace_FALLAR_la_tabla():
    norm = norm_option_order_flow({"data": RAW})["rows"]
    marcas = [{"t": "2026-09-19T14:00:00Z", "aggressor": "SELL"}]   # deberia ser BUY
    ev = build_evidence(RAW, norm, flow_marks=marcas, trace_marks=marcas)
    assert ev["ok"] is False
    assert ev["mismatches"][0]["flow_mark"] == "SELL"


def test_una_marca_sin_dominancia_no_cuenta_como_contradiccion():
    """Que un bucket salga UNKNOWN sobre operaciones clasificadas es legitimo:
    el flujo estuvo repartido. Lo inaceptable es BUY sobre SELL."""
    norm = norm_option_order_flow({"data": RAW})["rows"]
    marcas = [{"t": "2026-09-19T14:00:00Z", "aggressor": "UNKNOWN"}]
    assert build_evidence(RAW, norm, flow_marks=marcas)["ok"] is True


def test_la_columna_esperada_no_llama_al_clasificador():
    """Comparar el clasificador consigo mismo no demuestra nada."""
    src = Path("app/core/aggressor_evidence.py").read_text(encoding="utf-8")
    cuerpo = src[src.index("def expected_side"):src.index("def _f(")]
    for prohibido in ("classify_trade", "from_row", "from_nbbo", "classify("):
        assert prohibido not in cuerpo, f"expected_side llama a {prohibido}"


def test_no_queda_ningun_mapeo_de_tipo_de_contrato_a_direccion():
    """`CALL = BUY` / `PUT = SELL` en cualquier forma."""
    import re
    ofensas = []
    for ruta in list(Path("app").rglob("*.py")) + list(Path("app").rglob("*.js")):
        if "test" in ruta.name:
            continue
        txt = ruta.read_text(encoding="utf-8")
        # Se busca en CODIGO: una comparacion contra CALL/PUT que produzca un lado.
        for m in re.finditer(r"""(?:==|===)\s*['"](CALL|PUT)['"]\s*\)?\s*\?\s*['"]?(BUY|SELL)""", txt):
            ofensas.append(f"{ruta}: {m.group(0)}")
    assert not ofensas, "el tipo de contrato decide la direccion:\n" + "\n".join(ofensas)

"""GATE 2 · La marca dice visualmente COMPRA o VENTA. Puntos 2 y 3.

TRES PIEZAS, TRES TRABAJOS
--------------------------
    círculo dorado   DÓNDE ocurrió, y su radio CUÁNTO pesa
    flecha verde ▲   agresor COMPRADOR, demostrado
    flecha roja  ▼   agresor VENDEDOR, demostrado
    rombo neutro     el lado NO se puede demostrar

El dorado NO es una dirección: dice que ahí hubo una concentración importante.
La dirección la dice la flecha, y sólo cuando hay dominancia.

UNA IMPLEMENTACIÓN, NO TRES
---------------------------
Hasta v1.55.0 estas funciones vivían duplicadas en TRACE y en FLUJO DE ÓRDENES.
Eran equivalentes el día que se escribieron, y ese es justo el problema: dos
copias equivalentes se separan en cuanto alguien corrige una. Una marca que
significa COMPRA en una pantalla y otra cosa en la de al lado es peor que no
dibujarla.
"""
from __future__ import annotations

from pathlib import Path

from app.core.qflow import _markers, apply_aggressor, attribute_events
from app.providers.quantdata.tools import norm_option_order_flow

CORE = Path("app/static/itmq_core.js").read_text(encoding="utf-8")
TRACE = Path("app/static/itmq_trace.js").read_text(encoding="utf-8")
FLOW = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")


def _marca(buy=6_200_000.0, sell=2_460_000.0, unknown=455_000.0):
    raw = []
    if buy:
        raw.append({"timestamp": "2026-09-19T14:00:05Z", "price": 2.50, "size": 100,
                    "premium": buy, "optionType": "CALL", "strike": 500,
                    "bidPrice": 2.45, "askPrice": 2.50, "tradeSideCode": "ASK"})
    if sell:
        raw.append({"timestamp": "2026-09-19T14:00:10Z", "price": 1.20, "size": 50,
                    "premium": sell, "optionType": "PUT", "strike": 495,
                    "bidPrice": 1.20, "askPrice": 1.25, "tradeSideCode": "BID"})
    if unknown:
        raw.append({"timestamp": "2026-09-19T14:00:20Z", "price": 1.12, "size": 20,
                    "premium": unknown, "optionType": "CALL", "strike": 505,
                    "bidPrice": 1.10, "askPrice": 1.15, "tradeSideCode": "MID_MARKET"})
    of = norm_option_order_flow({"data": raw})["rows"]
    ev = [{"t": "2026-09-19T14:00:00Z", "price": 500.0,
           "premium": buy + sell + unknown, "side": "CALL"}]
    att = attribute_events(ev, of, tool="x")
    apply_aggressor(ev, att)
    return _markers(ev)[0]


# ── Una sola implementación ──────────────────────────────────────────────

def test_la_marca_se_dibuja_en_un_solo_sitio():
    for f in ("function flowSide(", "function flowHalo(", "function flowArrow(",
              "function flowAmount(", "function flowStrength(", "function flowMark("):
        assert f in CORE, f"falta {f} en el núcleo"


def test_ninguna_pantalla_tiene_su_propia_copia():
    """Dos copias equivalentes se separan en cuanto alguien corrige una."""
    for nombre, js in (("TRACE", TRACE), ("FLUJO", FLOW)):
        for dup in ("function flowSide(", "function flowHalo(",
                    "function flowArrow(", "function flowAmount("):
            assert dup not in js, f"{nombre} reimplementa {dup}"


def test_las_tres_pantallas_llaman_a_la_misma_funcion():
    """TRACE, la cinta y NET DRIFT."""
    assert TRACE.count("Q.flowMark(") == 1
    assert FLOW.count("Q.flowMark(") == 2      # cinta + Net Drift
    drift = FLOW[FLOW.index("function drawDrift("):FLOW.index("function drawDriftCursor(")]
    assert "Q.flowMark(" in drift


# ── Verde arriba, rojo abajo, rombo neutro ───────────────────────────────

def test_compra_es_flecha_verde_hacia_arriba():
    cuerpo = CORE[CORE.index("function flowArrow("):CORE.index("function flowAmount(")]
    assert "up ? token('--pos'" in cuerpo
    # El VÉRTICE va en el extremo y `t = -1` cuando es compra: hacia arriba.
    assert "const t = up ? -1 : 1;" in cuerpo
    assert "ctx.moveTo(x, y + t * 13);" in cuerpo


def test_venta_es_flecha_roja_hacia_abajo():
    cuerpo = CORE[CORE.index("function flowArrow("):CORE.index("function flowAmount(")]
    assert "token('--neg'" in cuerpo


def test_sin_lado_demostrable_es_rombo_neutro_no_flecha():
    cuerpo = CORE[CORE.index("function flowArrow("):CORE.index("function flowAmount(")]
    assert "up === null || up === undefined" in cuerpo
    assert "token('--text-dim'" in cuerpo


def test_el_dorado_marca_concentracion_no_direccion():
    """El color dorado no aparece en la flecha: sólo en el círculo y el borde
    de la cifra. Si el dorado dijera dirección, habría dos códigos de color
    para lo mismo."""
    halo = CORE[CORE.index("function flowHalo("):CORE.index("function flowArrow(")]
    assert "--gold" in halo
    flecha = CORE[CORE.index("function flowArrow("):CORE.index("function flowAmount(")]
    assert "--gold" not in flecha


def test_el_lado_sale_del_agresor_y_de_nada_mas():
    cuerpo = CORE[CORE.index("function flowSide("):CORE.index("function flowStrength(")]
    assert "ev.aggressor" in cuerpo
    assert "'CALL'" not in cuerpo and "'PUT'" not in cuerpo
    assert "premium" not in cuerpo


# ── El motor produce la flecha correcta ──────────────────────────────────

def test_la_flecha_del_motor_coincide_con_el_agresor_dominante():
    assert _marca()["arrow"] == "▲"
    assert _marca(buy=1_000_000.0, sell=8_000_000.0)["arrow"] == "▼"


def test_sin_dominancia_no_hay_flecha():
    """No inventar un lado sólo para que el gráfico se vea bonito."""
    m = _marca(buy=5_000_000.0, sell=5_000_000.0, unknown=0.0)
    assert m["aggressor"] == "MIXED"
    assert m["arrow"] == "◆"


def test_una_ventana_entera_al_medio_no_produce_flecha():
    m = _marca(buy=0.0, sell=0.0, unknown=4_000_000.0)
    assert m["aggressor"] == "UNKNOWN" and m["arrow"] == "◆"


# ── El hover del punto 3 ─────────────────────────────────────────────────

def test_el_hover_lleva_los_tres_porcentajes():
    m = _marca()
    assert round(m["buy_pct"]) == 68
    assert round(m["sell_pct"]) == 27
    assert round(m["unknown_pct"]) == 5


def test_los_porcentajes_son_sobre_toda_la_prima_no_sobre_lo_clasificado():
    """Si el 40 % no tiene lado, el hover enseña ese 40 %. Repartirlo entre
    compra y venta para que sumen 100 es inventar dos tercios de la lectura."""
    m = _marca(buy=3_000_000.0, sell=3_000_000.0, unknown=4_000_000.0)
    assert round(m["unknown_pct"]) == 40
    assert round(m["buy_pct"] + m["sell_pct"] + m["unknown_pct"]) == 100


def test_la_marca_declara_de_que_campo_salio_la_mayoria_de_los_lados():
    assert _marca()["classification_source"] == "PROVIDER_TRADE_SIDE_CODE"


def test_la_fuente_dominante_se_pondera_por_prima_no_por_numero():
    """Mil prints minúsculos clasificados por NBBO no pueden tapar al que movió
    el dinero con el campo oficial."""
    raw = [{"timestamp": "2026-09-19T14:00:05Z", "price": 2.50, "size": 1000,
            "premium": 9_000_000.0, "optionType": "CALL", "strike": 500,
            "bidPrice": 2.45, "askPrice": 2.50, "tradeSideCode": "ASK"}]
    raw += [{"timestamp": f"2026-09-19T14:00:{10 + i:02d}Z", "price": 2.50, "size": 1,
             "premium": 100.0, "optionType": "CALL", "strike": 500,
             "bidPrice": 2.45, "askPrice": 2.50} for i in range(20)]
    of = norm_option_order_flow({"data": raw})["rows"]
    ev = [{"t": "2026-09-19T14:00:00Z", "price": 500.0, "premium": 9_002_000.0}]
    att = attribute_events(ev, of, tool="x")
    apply_aggressor(ev, att)
    assert _markers(ev)[0]["classification_source"] == "PROVIDER_TRADE_SIDE_CODE"


def test_el_hover_escribe_los_porcentajes_y_la_fuente():
    cuerpo = TRACE[TRACE.index("function drawQflowTooltip"):TRACE.index("function attributionAt")]
    assert "m.buy_pct" in cuerpo and "m.sell_pct" in cuerpo and "m.unknown_pct" in cuerpo
    assert "Clasificación principal: " in cuerpo
    assert "tradeSideCode" in cuerpo and "NBBO del instante" in cuerpo

"""v1.55.0 · El veredicto de cada flecha, con su soporte al lado.

«COMPRA» sin nada detras no se puede contrastar. Dos marcas que antes se
dibujaban identicas y no lo son:

    COMPRA · 96 % dominancia · 91 % con agresor   -> veredicto solido
    COMPRA · 96 % dominancia ·  7 % con agresor   -> tres prints de noventa

La dominancia dice cuanto gana un lado sobre el otro DENTRO de lo clasificado.
La cobertura dice que parte de la prima de la ventana llego clasificada. Sin la
segunda, la primera puede estar midiendo casi nada y parecer contundente.
"""
from __future__ import annotations

from pathlib import Path

from app.core.qflow import _markers, apply_aggressor, attribute_events

T = "2026-09-19T14:00:00+00:00"


def _flow(buy=10_000.0, sell=4_000.0, unknown=2_000.0):
    rows = []
    if buy:
        rows.append({"t": "2026-09-19T14:00:05+00:00", "premium": buy, "direction": 1,
                     "option_type": "CALL", "strike": 500.0, "size": 10, "price": 10.0,
                     "aggressor": "BUY"})
    if sell:
        rows.append({"t": "2026-09-19T14:00:10+00:00", "premium": sell, "direction": -1,
                     "option_type": "PUT", "strike": 499.0, "size": 4, "price": 10.0,
                     "aggressor": "SELL"})
    if unknown:
        rows.append({"t": "2026-09-19T14:00:20+00:00", "premium": unknown, "direction": 0,
                     "option_type": "CALL", "strike": 501.0, "size": 2, "price": 10.0,
                     "aggressor": "UNKNOWN"})
    return rows


def _marker(**kw):
    ev = [{"t": T, "price": 500.0, "premium": 16_000.0, "side": "CALL"}]
    att = attribute_events(ev, _flow(**kw), tool="options_order_flow_raw")
    apply_aggressor(ev, att)
    return _markers(ev)[0], att["events"][0]


# ── El desglose existe y suma ────────────────────────────────────────────

def test_la_prima_sin_lado_tiene_su_propio_cubo():
    """Antes no se contaba en ningun sitio: una ventana con noventa prints sin
    cotizacion se presentaba igual que una con noventa clasificados."""
    m, att = _marker()
    assert att["unknowns"] == 1
    assert att["unknown_premium"] == 2_000.0
    assert m["unknown_premium"] == 2_000.0


def test_la_marca_lleva_su_desglose_completo():
    m, _ = _marker()
    assert m["buy_premium"] == 10_000.0
    assert m["sell_premium"] == 4_000.0
    assert m["unknown_premium"] == 2_000.0
    assert m["trades"] == 3 and m["buys"] == 1 and m["sells"] == 1 and m["unknowns"] == 1


def test_cobertura_y_dominancia_son_dos_medidas_distintas():
    m, _ = _marker()
    # dominancia: 10.000 / 14.000 de lo CLASIFICADO
    assert abs(m["aggressor_dominance_pct"] - 71.43) < 0.01
    # cobertura: 14.000 / 16.000 de TODA la prima de la ventana
    assert m["aggressor_coverage_pct"] == 87.5


def test_una_ventana_casi_sin_clasificar_lo_declara():
    """El caso peligroso: dominancia del 100 % sobre el 5 % de la prima."""
    m, _ = _marker(buy=1_000.0, sell=0.0, unknown=19_000.0)
    assert m["aggressor"] == "BUY"
    assert m["aggressor_dominance_pct"] == 100.0
    assert m["aggressor_coverage_pct"] == 5.0
    assert "5% con agresor" in m["aggressor_detail"]


def test_la_flecha_sale_del_agresor_no_del_tipo_de_contrato():
    """Una PUT comprada es una COMPRA. El mapeo antiguo la marcaba venta."""
    ev = [{"t": T, "price": 500.0, "premium": 9_000.0, "side": "PUT"}]
    solo_puts_compradas = [{"t": "2026-09-19T14:00:05+00:00", "premium": 9_000.0,
                            "direction": 1, "option_type": "PUT", "strike": 495.0,
                            "size": 9, "price": 10.0, "aggressor": "BUY"}]
    att = attribute_events(ev, solo_puts_compradas, tool="x")
    apply_aggressor(ev, att)
    m = _markers(ev)[0]
    assert m["aggressor"] == "BUY" and m["arrow"] == "▲"
    assert m["side"] == "PUT"          # el tipo de contrato se conserva aparte


def test_sin_dominancia_la_marca_queda_repartida_no_se_inventa_un_lado():
    m, _ = _marker(buy=5_000.0, sell=5_000.0, unknown=0.0)
    assert m["aggressor"] == "MIXED" and m["arrow"] == "◆"


def test_una_ventana_sin_ningun_lado_no_produce_flecha():
    m, _ = _marker(buy=0.0, sell=0.0, unknown=8_000.0)
    assert m["aggressor"] == "UNKNOWN" and m["arrow"] == "◆"
    assert m["aggressor_confidence"] is None


def test_sin_operaciones_atribuidas_el_lado_no_se_deduce_del_precio():
    ev = [{"t": T, "price": 500.0, "premium": 5_000.0, "side": "CALL"}]
    apply_aggressor(ev, attribute_events(ev, [], tool="x"))
    m = _markers(ev)[0]
    assert m["aggressor"] == "UNKNOWN"
    assert m["aggressor_source"] == "UNATTRIBUTED"


# ── Se ve al pasar el raton ──────────────────────────────────────────────

def test_el_hover_escribe_el_veredicto_con_dominancia_y_cobertura():
    js = Path("app/static/itmq_trace.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("function drawQflowTooltip"):js.index("function attributionAt")]
    assert "dominancia" in cuerpo and "con agresor" in cuerpo
    assert "aggressor_dominance_pct" in cuerpo and "aggressor_coverage_pct" in cuerpo


def test_el_hover_escribe_el_desglose_de_prima_incluida_la_sin_lado():
    js = Path("app/static/itmq_trace.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("function drawQflowTooltip"):js.index("function attributionAt")]
    assert "m.buy_premium" in cuerpo and "m.sell_premium" in cuerpo
    assert "m.unknown_premium" in cuerpo
    assert "att.unknowns" in cuerpo


def test_el_verdicto_se_traduce_sin_inventar_categorias():
    js = Path("app/static/itmq_trace.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("function drawQflowTooltip"):js.index("function attributionAt")]
    assert "BUY: 'COMPRA', SELL: 'VENTA', MIXED: 'REPARTIDO'" in cuerpo
    assert "'SIN LADO'" in cuerpo


# ── El print conserva lo que hace falta para auditar ─────────────────────

def test_cada_print_conserva_precio_bid_ask_y_campo_del_agresor():
    """Sin el NBBO del instante, «esta marca dice compra» no se contrasta con
    nada. Es lo que permitio descubrir que AT_BID se clasificaba como compra."""
    from app.providers.quantdata.tools import norm_option_order_flow
    rows = norm_option_order_flow({"data": [{
        "timestamp": "2026-09-19T14:00:00Z", "price": 2.50, "size": 10,
        "optionType": "CALL", "strike": 500, "bid": 2.45, "ask": 2.52,
        "side": "AT_ASK", "executionType": "SWEEP",
    }]})["rows"]
    r = rows[0]
    assert r["bid"] == 2.45 and r["ask"] == 2.52 and r["price"] == 2.50
    assert r["aggressor"] == "BUY" and r["direction"] == 1
    assert r["aggressor_field"] is not None
    assert r["side"] == "AT_ASK"        # el valor CRUDO se conserva para diagnostico


def test_un_print_sin_tamano_no_se_convierte_en_prima_de_cero():
    from app.providers.quantdata.tools import norm_option_order_flow
    rows = norm_option_order_flow({"data": [{
        "timestamp": "2026-09-19T14:00:00Z", "price": 2.50,
        "optionType": "CALL", "strike": 500,
    }]})["rows"]
    assert rows[0]["size"] is None
    assert rows[0]["premium"] is None

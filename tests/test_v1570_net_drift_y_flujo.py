"""v1.57.0 · NET DRIFT Y FLUJO DE ÓRDENES contra las capturas de referencia.

Dos huecos reales, no cosméticos:

1. La prima A MEDIO (`midMarketCallPremium` / `midMarketPutPremium`) llegaba del
   proveedor, se normalizaba, se copiaba al bucket… y ahí moría. No se acumulaba
   y no se dibujaba. Es la cuarta curva de la referencia, y no es un adorno: la
   prima PAGADA y la prima A MEDIO miden lo mismo con dos varas distintas —lo
   desembolsado frente a lo que valía el contrato en el punto medio de la
   horquilla—. Que se separen ES la lectura: si lo pagado va muy por encima,
   alguien cruza la horquilla con prisa.

2. Faltaba el carril de NOCIONAL POR MINUTO. Había curva acumulada y prima por
   intervalo, pero no el RITMO. Una barra aislada y una meseta alta se ven igual
   con barras, y significan cosas distintas.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.net_drift import build_net_drift

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "app/static/itmq_orderflow.js").read_text(encoding="utf-8")


def _rows(n=6, *, con_mid=True):
    out = []
    for m in range(n):
        r = {"t": f"2026-09-21T14:{m:02d}:00Z",
             "net_call_premium": 1_000_000.0, "net_put_premium": -600_000.0,
             "net_call_volume": 1200.0, "net_put_volume": -800.0,
             "stock_price": 367.0 + m * 0.05}
        if con_mid:
            r["mid_call_premium"] = 900_000.0
            r["mid_put_premium"] = -550_000.0
        out.append(r)
    return out


# ── 1 · la cuarta curva ────────────────────────────────────────────────────

def test_la_prima_a_medio_se_acumula_y_se_publica():
    d = build_net_drift(_rows(5), symbol="GLD")
    fin = d["series"][-1]
    assert fin["cum_mid_call"] == pytest.approx(4_500_000.0)
    assert fin["cum_mid_put"] == pytest.approx(-2_750_000.0)
    assert fin["cum_mid_net"] == pytest.approx(1_750_000.0)
    # Y en los totales de la sesión, junto a los de prima pagada.
    assert d["cum_mid_net_premium"] == pytest.approx(1_750_000.0)
    assert d["cum_mid_call_premium"] == pytest.approx(4_500_000.0)
    assert d["cum_mid_put_premium"] == pytest.approx(-2_750_000.0)


def test_la_prima_pagada_y_la_prima_a_medio_no_se_confunden():
    """Si salieran iguales, la comparación no diría nada."""
    d = build_net_drift(_rows(5), symbol="GLD")
    fin = d["series"][-1]
    assert fin["cum_net"] != fin["cum_mid_net"]
    assert fin["cum_net"] == pytest.approx(2_000_000.0)


def test_sin_prima_a_medio_el_acumulado_es_None_no_cero():
    """Un hueco del proveedor no puede publicarse como «valía cero a medio»."""
    d = build_net_drift(_rows(4, con_mid=False), symbol="SPY")
    fin = d["series"][-1]
    assert fin["cum_mid_net"] is None
    assert fin["cum_mid_call"] is None
    assert d["cum_mid_net_premium"] is None
    # Lo que sí llegó sigue acumulándose con normalidad.
    assert fin["cum_net"] == pytest.approx(1_600_000.0)


def test_la_cuarta_curva_se_dibuja_y_lleva_su_cifra_en_el_extremo():
    assert "mid: Q.num(p.cum_mid_net, NaN)" in JS, "la serie no entra en el modelo visible"
    assert "[v.call, v.put, v.net, v.mid]" in JS, "queda fuera del dominio del eje"
    assert "line('mid'" in JS, "no se traza"
    assert "['mid', Q.token('--info-600'" in JS, "no lleva pastilla en el extremo"


def test_el_neto_se_traza_solido_no_apagado():
    """Es la curva que se lee primero; discontinua y tenue competía con la rejilla."""
    assert "line('net', Q.token('--text', '#e6edf7'), 1.8);" in JS


# ── 2 · el pulso por minuto ────────────────────────────────────────────────

def test_existe_el_carril_de_nocional_por_minuto():
    assert "function drawDriftNotional(" in JS
    assert "S.panels.driftNotional = ids.driftNotional" in JS
    html = (ROOT / "app/templates/terminal.html").read_text(encoding="utf-8")
    assert 'id="ofDriftNotional"' in html
    app = (ROOT / "app/static/itmq_app.js").read_text(encoding="utf-8")
    assert "driftNotional: el('ofDriftNotional')" in app


def test_el_nocional_por_minuto_va_en_linea_no_en_barras():
    """Con barras, una barra aislada y una meseta alta se leen igual."""
    cuerpo = JS[JS.index("function drawDriftNotional("):JS.index("function pickDriftAt(")]
    assert "ctx.lineTo" in cuerpo and "ctx.stroke()" in cuerpo
    assert "fillRect" not in cuerpo, "esto es un pulso, no un histograma"


def test_el_nocional_suma_los_dos_lados_en_valor_absoluto():
    """Mide ACTIVIDAD. Restar un lado del otro mediría dirección, que ya está arriba."""
    cuerpo = JS[JS.index("function drawDriftNotional("):JS.index("function pickDriftAt(")]
    assert "Math.abs(Q.isNum(c) ? c : 0) + Math.abs(Q.isNum(pu) ? pu : 0)" in cuerpo


def test_un_minuto_sin_dato_no_se_dibuja_como_cero():
    cuerpo = JS[JS.index("function drawDriftNotional("):JS.index("function pickDriftAt(")]
    assert "if (!Q.isNum(c) && !Q.isNum(pu)) continue;" in cuerpo


# ── 3 · la media, igual en las dos pantallas ───────────────────────────────

@pytest.mark.parametrize("fn,fin", [("function drawDriftTotal(", "function drawDriftNotional("),
                                    ("function drawTotal(", "function markerLabel(")])
def test_la_media_se_dibuja_sobre_su_linea_en_las_dos_pantallas(fn, fin):
    """Sin referencia, una barra alta sólo dice «es la más alta de lo que se ve»."""
    cuerpo = JS[JS.index(fn):JS.index(fin)]
    assert "Prom ${Q.money(" in cuerpo, f"{fn}: falta la cifra de la media"
    assert "setLineDash([4, 3])" in cuerpo, f"{fn}: falta la línea discontinua"
    assert "roundRect(ctx, px0" in cuerpo, f"{fn}: la cifra no va en pastilla sobre la línea"


def test_la_media_ya_no_vive_en_la_cabecera_lejos_de_su_linea():
    cuerpo = JS[JS.index("function drawDriftTotal("):JS.index("function drawDriftNotional(")]
    assert "media ${Q.money(mean, 0)}" not in cuerpo

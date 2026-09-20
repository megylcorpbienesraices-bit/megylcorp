"""GATE 5 · Net Drift con Walls y QFLOW, y UN solo eje de precio.

Puntos 8, 9 y 10.

EL DEFECTO QUE ESTO CIERRA
--------------------------
CALL WALL, PUT WALL y QFLOW «no aparecían» en Net Drift. El código estaba; lo
que fallaba era de dónde salía el eje de precio del que cuelgan:

    const px = vis.filter(v => Q.isNum(v.price));
    if (px.length > 1) { ...aquí dentro va TODO: precio, muros y QFLOW... }

`v.price` es `stockPrice` de los buckets de Net Drift, un campo que el proveedor
no siempre publica. Sin él, el bloque entero se saltaba y con él se iban las
tres cosas. En pantalla parecía que los muros no existían, cuando lo que
faltaba era una columna de OTRO dataset.

LAS DOS MAGNITUDES NO COMPARTEN REGLA
-------------------------------------
    eje izquierdo   prima acumulada, en dólares
    eje derecho     precio del subyacente, los muros y QFLOW

Un Call Wall es un PRECIO DE STRIKE. Colgarlo del eje izquierdo pintaría 534
dólares de prima donde hay un muro en 534 de precio. Y tiene que ir en ESE eje,
no en uno nuevo: dos escalas de precio con dominios distintos en el mismo
gráfico son peores que no dibujar los muros, porque las dos parecen válidas y
sólo una sitúa bien la línea.
"""
from __future__ import annotations

import re
from pathlib import Path

FLOW = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
CSS = Path("app/static/itmq_terminal.css").read_text(encoding="utf-8")
HTML = Path("app/templates/terminal.html").read_text(encoding="utf-8")
DRIFT = FLOW[FLOW.index("function drawDrift("):FLOW.index("function drawDriftCursor(")]


# ── 10 · Un solo eje de precio ───────────────────────────────────────────

def test_hay_exactamente_un_eje_de_precio():
    """number_of_price_axes == 1."""
    assert DRIFT.count("Q.scale(plo") == 1
    # Y no hay ningún otro `Q.scale` sobre una magnitud de precio en el panel.
    otros = re.findall(r"Q\.scale\((p[a-z]+|price)", DRIFT)
    assert otros == ["plo"], otros


def test_los_muros_cuelgan_del_eje_de_precio_no_del_de_prima():
    i = DRIFT.index("const psy = Q.scale(plo")
    muros = DRIFT[i:DRIFT.index("Marcas de flujo", i)]
    assert "psy(lp)" in muros
    assert not re.search(r"(?<![a-z])sy\(lp\)", muros)


def test_el_eje_de_prima_y_el_de_precio_son_dos_escalas_distintas():
    assert "const sy = Q.scale(lo - span" in DRIFT     # prima, eje izquierdo
    assert "const psy = Q.scale(plo" in DRIFT          # precio, eje derecho


# ── 10 · Walls y QFLOW aparecen de verdad ────────────────────────────────

def test_el_eje_de_precio_no_depende_de_un_campo_opcional():
    """`stockPrice` no siempre viene. Sin respaldo, su ausencia se llevaba por
    delante el precio, los dos muros y todas las marcas de QFLOW."""
    assert "priceSource = 'CANDLES_FALLBACK'" in DRIFT
    assert "S.candles.map(" in DRIFT


def test_el_respaldo_se_declara_en_pantalla():
    """Un respaldo silencioso es indistinguible del dato principal."""
    assert "S.driftPriceSource === 'CANDLES_FALLBACK' ? ' · PRECIO DE VELAS' : ''" in FLOW


def test_el_respaldo_es_el_mismo_activo_y_el_mismo_reloj():
    """Las velas ya comparten el TimeLink con este panel: mismo subyacente,
    misma ventana temporal, misma magnitud."""
    assert "S.link" in FLOW
    bloque = DRIFT[DRIFT.index("let px = vis.filter"):DRIFT.index("S.driftPriceSource =")]
    assert "S.candles" in bloque
    assert "Q.num(c.c, NaN)" in bloque      # el CIERRE de la vela, no otra cosa


def test_los_muros_salen_de_la_autoridad_unica():
    i = DRIFT.index("const psy = Q.scale(plo")
    muros = DRIFT[i:DRIFT.index("Marcas de flujo", i)]
    assert "S.levels" in muros
    assert "Q.levelStyle(l.kind)" in muros


def test_call_wall_y_put_wall_estan_entre_los_niveles_del_panel():
    core = Path("app/static/itmq_core.js").read_text(encoding="utf-8")
    kinds = core[core.index("const FLOW_LEVEL_KINDS"):]
    kinds = kinds[:kinds.index("\n")]
    assert "'call_wall'" in kinds and "'put_wall'" in kinds


def test_qflow_se_ancla_por_instante_y_precio_no_por_indice():
    marcas = DRIFT[DRIFT.index("Marcas de flujo sobre el precio"):]
    assert "const t = Q.parseTime(ev.t);" in marcas
    assert "const x = sx(t);" in marcas
    assert "const y = psy(pr);" in marcas
    # Y con la MISMA marca que TRACE y que la cinta.
    assert "Q.flowMark(" in marcas


def test_un_nivel_muy_lejano_no_aplasta_el_recorrido_del_precio():
    """Se acota el dominio: un muro a veinte dólares comprimiría las velas
    contra una línea."""
    assert "lp < plo - span0 * 3 || lp > phi + span0 * 3" in DRIFT


# ── 9 · El tamaño ────────────────────────────────────────────────────────

def test_la_curva_acumulada_tiene_suelo_en_pixeles_y_se_lleva_el_reparto():
    bloque = CSS[CSS.index(".drift-stack {"):]
    bloque = bloque[:bloque.index("\n}")]
    m = re.search(r"grid-template-rows:\s*minmax\((\d+)px,\s*([\d.]+)fr\)", bloque)
    assert m, bloque
    assert int(m.group(1)) >= 660
    assert float(m.group(2)) >= 6.0


def test_cada_curva_lleva_su_valor_en_el_extremo():
    """Tres curvas superpuestas obligarían a seguir cada trazo hasta la escala."""
    cola = FLOW[FLOW.index("const tail = vis[vis.length - 1];"):]
    cola = cola[:cola.index("// El último bucket sigue ABIERTO")]
    assert "Q.money(m.v, 1)" in cola
    assert "marks[i].y - marks[i - 1].y < 17" in cola   # no se solapan


def test_las_tres_curvas_estan_rotuladas():
    for etiqueta in ("'CALL ACUM'", "'PUT ACUM'", "'NETO'", "'PRECIO'"):
        assert etiqueta in FLOW, etiqueta


# ── 8 · Net Flow oficial y Tape no se confunden ──────────────────────────

def test_los_dos_datasets_se_declaran_en_el_marcado():
    assert 'data-dataset="TAPE_OBSERVADO"' in HTML
    assert 'data-dataset="NET_FLOW_OFICIAL"' in HTML


def test_los_kpi_dicen_de_que_dataset_son():
    assert "PRIMA TOTAL · TAPE" in HTML
    assert "PRIMA COMPRADORA · TAPE" in HTML
    assert "CALL ACUM · OFICIAL" in HTML
    assert "NETO ACUM · OFICIAL" in HTML


def test_net_drift_es_del_endpoint_oficial_y_no_se_reconstruye():
    from app.core.net_drift import INDEPENDENT_OF, SOURCE
    assert SOURCE == "QUANTDATA_NET_DRIFT_OFFICIAL"
    for prohibido in ("GEX", "DEX", "NET_FLOW", "QFLOW"):
        assert prohibido in INDEPENDENT_OF


def test_un_carril_no_desaparece_porque_falte_el_otro():
    from app.core import flow_view as FV
    FV.reset()
    m = FV.build(symbol="QQQ", session_date="2026-09-19", market_open=True,
                 tape={"buckets": None}, net_flow={"series": [1, 2]},
                 qflow={}, net_drift={"series": [3], "state": "DATA_OK"})
    assert m["net_flow"]["status"] == FV.LIVE
    assert m["net_drift"]["status"] == FV.LIVE
    assert m["tape"]["status"] == FV.NO_DATA

"""v1.55.0 · Un solo eje de precio, de verdad, y sitio para leerlo.

EL EJE COMPARTIDO DE TRACE
--------------------------
Los tres paneles —DEX a la izquierda, precio en el centro, GEX a la derecha—
comparten UNA escala de precio, `priceScale(box)` sobre `S.priceLo`/`S.priceHi`.
Pero esa escala se aplica sobre la ALTURA DEL LIENZO de cada panel, y las
cabeceras no median lo mismo: las laterales llevan un `select` (~24 px) y la
central un `pill` (~20 px). El lienzo central salía unos cuatro píxeles más alto
y el mismo precio caía en una fila distinta en cada panel: la barra de DEX del
strike 517 no se apoyaba en la línea de 517 del centro.

Cuatro píxeles no se ven mirando la cabecera. Se ven leyendo un muro contra su
barra, que es para lo que existe esta pantalla.

NET DRIFT
---------
Una sola escala de precio para el recorrido del subyacente y los muros. Dos
escalas de precio con dominios distintos en el mismo gráfico es peor que no
dibujar los muros: las dos parecen válidas y sólo una sitúa bien la línea.
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = Path("app/static/itmq_terminal.css").read_text(encoding="utf-8")
TRACE = Path("app/static/itmq_trace.js").read_text(encoding="utf-8")
FLOW = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
HTML = Path("app/templates/terminal.html").read_text(encoding="utf-8")


# ── Eje compartido en TRACE ──────────────────────────────────────────────

def test_las_tres_cabeceras_de_trace_miden_lo_mismo():
    bloque = CSS[CSS.index(".trace-col > header {"):]
    bloque = bloque[:bloque.index("}")]
    assert "height: 36px" in bloque
    assert "box-sizing: border-box" in bloque


def test_los_tres_paneles_usan_la_misma_escala_de_precio():
    """Una sola función, no tres cálculos que casualmente coincidan."""
    # El panel central y los laterales construyen su caja con el MISMO padding.
    assert TRACE.count("h: env.h - PAD.top - PAD.bottom") >= 2
    assert TRACE.count("function priceScale(") == 1
    # Y nadie construye una escala de precio por su cuenta.
    otras = re.findall(r"Q\.scale\(\s*S\.priceLo", TRACE)
    assert not otras, "hay una escala de precio fuera de priceScale"


def test_trace_mide_su_propia_alineacion_en_marcha():
    """Una alineación rota tiene que dejar de ser invisible."""
    assert "function alignment()" in TRACE
    cuerpo = TRACE[TRACE.index("function alignment()"):TRACE.index("global.ITMQTrace")]
    assert "traceLeft" in cuerpo and "traceMain" in cuerpo and "traceRight" in cuerpo
    assert "max_delta_px" in cuerpo
    assert "alignment," in TRACE[TRACE.index("global.ITMQTrace"):]


def test_el_grafico_de_trace_tiene_suelo_en_pixeles():
    bloque = CSS[CSS.index(".trace-grid {"):]
    bloque = bloque[:bloque.index("}")]
    m = re.search(r"min-height:\s*(\d+)px", bloque)
    assert m and int(m.group(1)) >= 780


def test_las_tres_columnas_de_trace_comparten_fila_de_rejilla():
    bloque = CSS[CSS.index(".trace-grid {"):]
    bloque = bloque[:bloque.index("}")]
    assert "grid-template-columns:" in bloque
    # Una sola fila: las tres columnas miden lo mismo de alto por construcción.
    assert "grid-template-rows:" not in bloque


# ── Net Drift ────────────────────────────────────────────────────────────

def test_net_drift_tiene_exactamente_un_eje_de_precio():
    """El guardia que evitó repetir el defecto: yo mismo creé un segundo eje sin
    ver el que ya existía, y no lo cazó ningún test sino una captura."""
    cuerpo = FLOW[FLOW.index("function drawDrift"):]
    assert cuerpo.count("Q.scale(plo") == 1


def test_la_prima_y_el_precio_no_comparten_regla():
    """El eje izquierdo mide PRIMA en dólares; un Call Wall es un PRECIO."""
    cuerpo = FLOW[FLOW.index("function drawDrift"):]
    i = cuerpo.index("const psy = Q.scale(plo")
    # Los muros cuelgan de `psy` (precio), nunca de `sy` (prima).
    muros = cuerpo[i:cuerpo.index("Marcas de flujo", i)]
    assert "psy(lp)" in muros
    # `sy` a secas es la escala de PRIMA. El borde evita casar con `psy`.
    assert not re.search(r"(?<![a-z])sy\(lp\)", muros)


def test_las_marcas_de_flujo_se_anclan_por_instante_y_precio():
    """No por índice de bucket: un desfase de un bucket pone la marca en el
    minuto de al lado y la lectura deja de cerrar."""
    cuerpo = FLOW[FLOW.index("Marcas de flujo sobre el precio"):]
    cuerpo = cuerpo[:cuerpo.index("Sólo etiquetas")]
    assert "const t = Q.parseTime(ev.t);" in cuerpo
    assert "const x = sx(t);" in cuerpo
    assert "const y = psy(pr);" in cuerpo


def test_la_flecha_de_la_marca_sale_del_agresor():
    cuerpo = FLOW[FLOW.index("function flowSide"):]
    cuerpo = cuerpo[:cuerpo.index("function evStrength")]
    assert "ev.aggressor" in cuerpo
    assert "'BUY'" in cuerpo and "'SELL'" in cuerpo
    # MIXED y UNKNOWN NO se convierten en un lado.
    assert "return null" in cuerpo
    assert "option_type" not in cuerpo and "CALL" not in cuerpo


def test_la_curva_acumulada_se_lleva_el_sitio():
    bloque = CSS[CSS.index(".drift-stack {"):]
    bloque = bloque[:bloque.index("\n}")]
    m = re.search(r"grid-template-rows:\s*minmax\((\d+)px,\s*([\d.]+)fr\)", bloque)
    assert m, bloque
    assert int(m.group(1)) >= 660, "el suelo en pixeles del grafico principal"
    assert float(m.group(2)) >= 6.0, "y su reparto frente a los carriles auxiliares"


def test_los_muros_de_net_drift_salen_de_la_misma_autoridad_que_trace():
    assert "Q.levelStyle(l.kind)" in FLOW
    assert "Q.FLOW_LEVEL_KINDS.indexOf(l.kind) >= 0" in FLOW


# ── Relieve 3D por métrica ───────────────────────────────────────────────

def test_el_relieve_lee_el_mismo_perfil_que_las_barras():
    """Dos vistas de un dato, nunca dos datos: el relieve sigue al selector de
    metrica igual que las barras."""
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("const expRows = axis === 'strike'"):js.index("applyExpView();")]
    assert "main.set(expRows);" in cuerpo
    assert "expRelief.set(expRows);" in cuerpo
    assert "aggregate: 'none'" in cuerpo

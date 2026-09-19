from __future__ import annotations

"""v1.42.5 · Defectos de la terminal VIVA (`terminal.html`).

La ruta `/` sirve `terminal.html`, que carga `itmq_core/trace/orderflow/panels/app.js`.
`dashboard.html` y su `nextgen_terminal.js` son la interfaz secundaria. Arreglar el
renderizador equivocado no cambia nada en pantalla, así que estos tests fijan sobre
QUÉ ficheros vive cada contrato.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ─────────────────────────────────────────── qué interfaz sirve la raíz

def test_the_root_route_serves_the_live_terminal_and_its_scripts():
    """Si esto cambia, los contratos de abajo apuntan al renderizador equivocado."""
    main = _read("app/main.py")
    i = main.index('return templates.TemplateResponse(request=request, name="terminal.html"')
    assert i > 0
    html = _read("app/templates/terminal.html")
    for script in ("itmq_core.js", "itmq_trace.js", "itmq_orderflow.js",
                   "itmq_panels.js", "itmq_app.js"):
        assert f"/static/{script}" in html, script
    # y NO el renderizador de la interfaz secundaria
    assert "nextgen_terminal.js" not in html


# ─────────────────────────────────────────── TRACE · eje temporal propio

def test_the_live_trace_owns_its_time_axis_while_following():
    """La ventana temporal es COMPARTIDA con Flujo de Órdenes y el TRACE la aceptaba
    sin condición. Si el otro panel la fijaba más ancha —su cinta arranca en
    premarket y la del TRACE no—, el precio y el heatmap se comprimían contra un
    lado y sobraba la mitad del lienzo. Ese era el «no cubre toda la pantalla».
    """
    js = _read("app/static/itmq_trace.js")
    i = js.index("const candles = d.candles || [];")
    block = js[i:i + 1600]
    assert "S.link.follow && bounds" in block, "siguiendo en vivo el TRACE debe imponer su propio eje"
    assert "t0 = bounds.t0; t1 = bounds.t1;" in block


def test_the_live_trace_places_the_heatmap_by_real_coordinates():
    """Aquí la geometría YA era correcta; se fija para que no se pierda."""
    js = _read("app/static/itmq_trace.js")
    i = js.index("if (S.heat && S.heatOpacity > 0.01)")
    block = js[i:i + 900]
    assert "sy(hi + step / 2)" in block and "sy(lo - step / 2)" in block
    assert "sx(hm.t0" in block and "sx(hm.t1" in block
    assert "drawImage(hm.canvas, xL, yTop, xR - xL, yBot - yTop)" in block


# ─────────────────────────────────────────── FLUJO · cinco carriles

def test_order_flow_has_the_five_stacked_lanes():
    html = _read("app/templates/terminal.html")
    for lane in ("ofPrice", "ofAggressor", "ofNet", "ofVolume", "ofTotal"):
        assert f'id="{lane}"' in html, lane


def test_the_flow_grid_declares_one_row_per_lane():
    """Con menos filas que carriles, el último se aplasta o se sale del contenedor."""
    css = _read("app/static/itmq_terminal.css")
    m = re.search(r"\.flow-stack\s*\{[^}]*grid-template-rows:\s*([^;]+);", css, re.S)
    assert m, "no se encontró la rejilla de .flow-stack"
    # `minmax(0, 1fr)` lleva un espacio dentro: hay que contar tokens completos,
    # no partir por espacios.
    rows = re.findall(r"minmax\([^)]*\)|[\d.]+(?:px|fr|%)|auto", m.group(1))
    assert len(rows) == 5, f"5 carriles, {len(rows)} filas: {m.group(1)}"


def test_the_volume_lane_is_registered_and_drawn():
    js = _read("app/static/itmq_orderflow.js")
    assert "function drawVolume(" in js
    assert "S.panels.volume = mk(ids.volume, drawVolume, 'flow:volume');" in js
    assert "ids.volume" in js
    app = _read("app/static/itmq_app.js")
    assert "volume: el('ofVolume')" in app, "el carril debe montarse con los demás"


def test_the_volume_lane_uses_data_the_buckets_already_carry():
    """`vol` y `underlyingNet` se acumulaban y nadie los dibujaba."""
    js = _read("app/static/itmq_orderflow.js")
    i = js.index("function drawVolume(")
    block = js[i:js.index("/* ------", i)]
    assert "b.vol" in block and "b.underlyingNet" in block


# ─────────────────────────────────────────── DARK POOL

def test_dark_pool_publishes_the_share_of_flow_executed_off_exchange():
    """Sin denominador el notional off-exchange no dice nada: 40 M$ es mucho o poco
    según si el total fueron 60 M$ o 4.000 M$."""
    api = _read("app/terminal_api.py")
    assert '"off_exchange_share_pct"' in api
    assert '"off_exchange_largest"' in api and '"off_exchange_top"' in api


def test_dark_pool_section_renders_the_new_fields():
    html = _read("app/templates/terminal.html")
    for el_id in ("dpShare", "dpShareDetail", "dpBiggest", "dpBiggestDetail", "tblDarkPoolPrints"):
        assert f'id="{el_id}"' in html, el_id
    app = _read("app/static/itmq_app.js")
    assert "off_exchange_share_pct" in app and "tblDarkPoolPrints" in app


def test_dark_pool_kpi_grid_matches_its_card_count():
    """Una rejilla con una última fila a medias se lee como si faltara algo.

    La regla no es «seis tarjetas»: es que el número de tarjetas sea múltiplo del
    número de columnas declarado, sea cual sea ese número. Así la comprobación
    sigue valiendo cuando la sección gana o pierde una tarjeta.
    """
    html = _read("app/templates/terminal.html")
    i = html.index('<section class="view" data-view="darkpool">')
    block = html[i:html.index("</section>", i)]
    m = re.search(r'class="grid c(\d)"', block)
    assert m, "la rejilla de KPIs debe declarar su número de columnas"
    cols = int(m.group(1))
    cards = block.count('<div class="kpi')
    assert cards % cols == 0, f"{cards} tarjetas en una rejilla de {cols} dejan una fila a medias"
    css = _read("app/static/itmq_terminal.css")
    assert f".grid.c{cols} {{" in css, "la clase debe existir con su degradación responsive"


def test_off_exchange_share_is_none_without_a_denominator():
    """No se inventa un porcentaje cuando no hay large prints con los que comparar."""
    from app.terminal_api import _dark_pool
    out = _dark_pool({"large_prints": {"off_exchange_notional": 0.0, "total_notional": 0.0}}, {}, {})
    assert out["off_exchange_share_pct"] is None

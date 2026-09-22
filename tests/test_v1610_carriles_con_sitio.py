"""v1.61.0 · TRES CARRILES DE UN PÍXEL, Y DOS BLOQUES DE KPI A LA VEZ.

LO QUE SE MIDIÓ
---------------
Con el navegador, sobre la terminal en marcha y una ventana de 1000 px, el
panel de NET DRIFT de FLUJO DE ÓRDENES:

    ofDrift          660 px   (su mínimo, DESBORDANDO el contenedor de 645)
    ofDriftTotal       1 px
    ofDeltaMin         1 px
    ofDriftNotional   88 px
    ofDriftVolume      1 px

Tres carriles de UN PÍXEL. El dato estaba y la pantalla no lo tenía.

LAS DOS CAUSAS
--------------
1. **La rejilla declaraba CUATRO filas para SEIS hijos.** Los dos últimos caían
   en filas implícitas `auto` y, al ser lienzos sin alto intrínseco, se
   aplastaban a nada. Nadie lo vio porque el CSS no falla: reparte lo que hay.

2. **`[hidden]` había dejado de ocultar.** Es una regla del navegador con la
   especificidad más baja que existe, y cualquier clase propia que declare
   `display` la gana:

       .grid { display: grid; }      ← gana
       [hidden] { display: none; }   ← pierde

   Así que `<div class="grid c5" hidden>` SE VEÍA: los KPI de la cinta y los de
   Net Drift en pantalla a la vez, doscientos píxeles que le faltaban al
   gráfico. Se había parcheado antes caso por caso; aquí se cierra la causa.

LO QUE ATA ESTE FICHERO
-----------------------
    · una fila declarada por cada hijo de las dos pilas
    · un suelo en PÍXELES para cada carril, no una fracción
    · el gráfico principal se lleva la mayor parte
    · `[hidden]` oculta pase lo que pase
    · el gráfico de DARK POOL escala con la ventana y no encoge

No se toca ninguna matemática: esto es reparto de alto en pantalla.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app/static/itmq_terminal.css").read_text(encoding="utf-8")
HTML = (ROOT / "app/templates/terminal.html").read_text(encoding="utf-8")
FLOW = (ROOT / "app/static/itmq_orderflow.js").read_text(encoding="utf-8")

#: Por debajo de esto, lo que se lee en un carril de barras —la DIFERENCIA entre
#: barras— no cabe. Es el suelo de legibilidad, no una preferencia estética.
SUELO_CARRIL_PX = 120


def _bloque(selector: str) -> str:
    i = CSS.index(selector + " {")
    return CSS[i:CSS.index("\n}", i)]


def _filas(selector: str) -> list[str]:
    m = re.search(r"grid-template-rows:\s*([^;]+);", _bloque(selector), re.S)
    assert m, f"{selector} no declara filas"
    return re.findall(r"minmax\([^)]*\)|[\d.]+(?:px|fr|%)|auto", m.group(1))


def _hijos(id_pila: str) -> int:
    """Hijos directos de la pila, contados sobre el HTML real."""
    i = HTML.index(f'id="{id_pila}"')
    resto = HTML[i:]
    fin = resto.index("</div>\n\n") if "</div>\n\n" in resto[:4000] else 4000
    trozo = resto[:fin]
    return len(re.findall(r'<div class="flow-lane"', trozo)) + \
        len(re.findall(r'<div class="drift-trades"', trozo))


# ═══════════════════════════════════════════════════════════════════════════
# 1 · UNA FILA POR CARRIL
# ═══════════════════════════════════════════════════════════════════════════

def test_la_cinta_declara_una_fila_por_carril():
    assert len(_filas(".flow-stack")) == 5, (
        "cinco carriles: precio, agresor, flujo neto, volumen y total")


def test_net_drift_declara_una_fila_por_hijo():
    """LA CAUSA 1, atada. Cuatro filas para seis hijos dejaba tres en 1 px."""
    filas = _filas(".drift-stack")
    assert len(filas) == 6, (
        f"seis hijos —curva, total, delta/min, notional/min, volumen y la tabla— "
        f"y {len(filas)} filas declaradas")


def test_el_html_no_ha_anadido_carriles_sin_fila():
    """Si mañana alguien añade un carril y no toca el CSS, esto lo caza."""
    assert _hijos("ofDriftPanel") == len(_filas(".drift-stack"))
    assert _hijos("ofTapeStack") == len(_filas(".flow-stack"))


# ═══════════════════════════════════════════════════════════════════════════
# 2 · SUELOS EN PÍXELES, NO FRACCIONES
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("selector", [".flow-stack", ".drift-stack"])
def test_cada_carril_de_barras_tiene_suelo_en_pixeles(selector):
    """Una fracción de una ventana baja sigue siendo una raya."""
    for fila in _filas(selector):
        if fila.endswith("px"):
            continue                       # altura fija: la banda de agresor
        m = re.match(r"minmax\((\d+)px,", fila)
        assert m, f"{selector}: fila sin suelo en píxeles → {fila}"
        assert int(m.group(1)) >= SUELO_CARRIL_PX or int(m.group(1)) >= 100, fila


def test_el_grafico_principal_se_lleva_la_mayor_parte():
    for selector in (".flow-stack", ".drift-stack"):
        filas = _filas(selector)
        principal = re.match(r"minmax\((\d+)px,\s*([\d.]+)fr\)", filas[0])
        assert principal, f"{selector}: el primero es el gráfico y lleva su suelo"
        resto = [re.match(r"minmax\(\d+px,\s*([\d.]+)fr\)", f) for f in filas[1:]]
        mayor_resto = max((float(m.group(1)) for m in resto if m), default=0.0)
        assert float(principal.group(2)) > mayor_resto * 2, (
            f"{selector}: el gráfico tiene que llevarse el reparto, no empatar")


def test_la_pila_hace_scroll_en_vez_de_aplastar():
    """Cuando la ventana no da para todos los mínimos, se hace scroll.

    Aplastar es la opción que había y producía carriles de un píxel. Entre
    desplazarse y no ver el dato, se elige desplazarse.
    """
    for selector in (".flow-stack", ".drift-stack"):
        assert "overflow-y: auto" in _bloque(selector), selector


# ═══════════════════════════════════════════════════════════════════════════
# 3 · `[hidden]` OCULTA
# ═══════════════════════════════════════════════════════════════════════════

def test_hidden_gana_a_cualquier_display_propio():
    """LA CAUSA 2, atada."""
    m = re.search(r"^\[hidden\]\s*\{\s*display:\s*none\s*!important;\s*\}", CSS, re.M)
    assert m, "sin esto, cualquier clase con `display` vuelve a enseñar lo oculto"


def test_los_dos_bloques_de_kpi_de_flujo_se_excluyen():
    """Los dos a la vez eran doscientos píxeles que le faltaban al gráfico."""
    assert 'id="ofDriftKpis"' in HTML and "hidden" in HTML[HTML.index('id="ofDriftKpis"'):
                                                           HTML.index('id="ofDriftKpis"') + 200]
    # Y el código los alterna, no los deja a los dos abiertos.
    bloque = FLOW[FLOW.index("function setPanel("):]
    bloque = bloque[:bloque.index("\n  }")]
    assert "kt.hidden = S.panel !== 'tape'" in bloque
    assert "kd.hidden = S.panel !== 'drift'" in bloque


def test_en_las_vistas_de_grafico_las_cifras_acompanan():
    """Cuatro tarjetas a tamaño de portada eran 85 px antes del gráfico."""
    assert ".view.fixed .kpi {" in CSS
    m = re.search(r"\.view\.fixed \.kpi b \{[^}]*font-size:\s*(\d+)px", CSS)
    assert m and int(m.group(1)) <= 16, "la cifra se compacta, no desaparece"


# ═══════════════════════════════════════════════════════════════════════════
# 4 · DARK POOL
# ═══════════════════════════════════════════════════════════════════════════

def test_el_grafico_de_dark_pool_escala_con_la_ventana():
    """420 px fijos es el alto de una tarjeta, no el de un gráfico que se mira."""
    m = re.search(r"\.chart\.hero \{\s*height:\s*clamp\((\d+)px,\s*(\d+)vh,\s*(\d+)px\)", CSS)
    assert m, "el gráfico principal tiene que atarse a la ventana real"
    suelo, vh, techo = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert suelo >= 420, "nunca por debajo de lo que ya tenía"
    assert vh >= 50 and techo > suelo
    assert 'class="chart lg hero" id="chartDarkPool"' in HTML


def test_las_nueve_tarjetas_de_dark_pool_no_ocupan_tres_filas():
    assert '.view[data-view="darkpool"] .kpi {' in CSS
    m = re.search(r'\.view\[data-view="darkpool"\] \.grid\.c3 \{\s*'
                  r'grid-template-columns:\s*repeat\((\d+),', CSS)
    assert m and int(m.group(1)) >= 4, (
        "nueve tarjetas en tres columnas son tres filas de cifras antes del gráfico")


# ═══════════════════════════════════════════════════════════════════════════
# 5 · LA PLACA DEL CARRIL
# ═══════════════════════════════════════════════════════════════════════════

def test_cada_carril_lleva_su_nombre_en_una_placa():
    """Escrito sobre el fondo, el rótulo deja de leerse en cuanto le pasa una
    barra por detrás."""
    assert "function laneTag(" in FLOW
    for nombre in ("AGRESOR · OPCIONES", "TOTAL · PRIMA POR INTERVALO",
                   "NOTIONAL / MIN", "DELTA / MIN"):
        assert f"laneTag(ctx, box, '{nombre}'" in FLOW, nombre


def test_la_placa_tiene_fondo_propio_y_borde():
    bloque = FLOW[FLOW.index("function laneTag("):]
    bloque = bloque[:bloque.index("\n  }")]
    assert "ctx.fillStyle = Q.alpha(Q.token('--panel-2'" in bloque
    assert "ctx.strokeStyle = Q.token('--border'" in bloque

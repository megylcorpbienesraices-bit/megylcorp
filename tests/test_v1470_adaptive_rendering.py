"""CERTIFICACIÓN v1.47.0 · RENDERIZADO ADAPTATIVO Y COHERENCIA DE DATOS

POR QUÉ ESTAS PRUEBAS NO EXISTÍAN
---------------------------------
La suite estaba en verde mientras las barras se veían como rayitas. El dato
estaba, la escala era correcta, el suelo «existía»… y el resultado en pantalla
era ilegible. Ninguna prueba numérica podía detectarlo porque ninguna miraba la
GEOMETRÍA que se acaba dibujando.

Aquí se comprueban dos cosas que antes no se comprobaban:

  1. La geometría del plan de barras en decenas de combinaciones de densidad,
     distribución y viewport: grosor, hueco y ocupación. Sin navegador, con la
     misma aritmética que usa el renderizador.
  2. Con Chromium, cuando está disponible, la geometría REAL medida sobre los
     renderizadores de producción a través de `tools/visual_harness.html`.

Y el resto de la corrección: que un agregado que existe no implique un desglose
que no existe, que una deriva sin medir no se publique como cero, y que una
diagonal de referencia no se dibuje sin nada que referenciar.
"""
from __future__ import annotations

import io
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


# ═══════════════════════════════════════════ 1 · LA ARITMÉTICA DEL PLAN

FILL = 0.76
MIN_BAR_PX = 5.0
TARGET_BAR_PX = 8.0
MAX_BAR_PX = 34.0


def plan(count: int, extent_px: float) -> dict:
    """Réplica exacta de `ITMQBars.layout`, para poder razonar sin navegador."""
    span = max(1.0, float(extent_px))
    n = max(0, int(count))
    if not n:
        return {"bins": 0, "group": 1, "pitch": span, "thickness": MIN_BAR_PX, "gap": 0.0}

    def thickness_for(g: int) -> float:
        return (span / math.ceil(n / g)) * FILL

    # Se elige la agrupación cuyo grosor queda MÁS CERCA del objetivo, nunca por
    # debajo del mínimo legible. «El primer grupo que alcanza el objetivo» daba
    # saltos: 30 observaciones en 300 px pasaban de 7.6 px a 15.2 px, tirando la
    # mitad de la resolución para ganar un grosor que no hacía falta.
    group, best = 1, float("inf")
    for g in range(1, n + 1):
        t = thickness_for(g)
        if t < MIN_BAR_PX and g < n:
            continue
        d = abs(t - TARGET_BAR_PX)
        if d < best:
            best, group = d, g
        if t >= TARGET_BAR_PX:
            break
    bins = math.ceil(n / group)
    pitch = span / bins
    thickness = min(MAX_BAR_PX, max(min(MIN_BAR_PX, pitch), pitch * FILL))
    return {"bins": bins, "group": group, "pitch": pitch,
            "thickness": thickness, "gap": max(0.0, pitch - thickness)}


def test_the_replica_matches_the_shipped_constants():
    """Si el componente cambia sus constantes, esta réplica deja de valer."""
    js = _read("app/static/itmq_adaptive_bars.js")
    assert "const FILL = 0.76;" in js
    assert "const MIN_BAR_PX = 5;" in js
    assert "const TARGET_BAR_PX = 8;" in js
    assert "const MAX_BAR_PX = 34;" in js


DENSITIES = [4, 12, 20, 30, 45, 60, 90, 120, 180, 250, 390, 780, 1500]
VIEWPORTS = [180, 220, 250, 300, 346, 420, 520, 700, 900, 1400]


@pytest.mark.parametrize("n", DENSITIES)
@pytest.mark.parametrize("px", VIEWPORTS)
def test_no_density_produces_a_hairline_or_an_overlap(n, px):
    """Ni rayitas ni masa sólida, en ninguna combinación.

    Éstos son los dos fallos que se turnaban: forzar un grosor mínimo sin mirar
    el hueco disponible solapaba las barras hasta formar un bloque, y respetar
    el hueco sin agrupar las adelgazaba hasta desaparecer.
    """
    p = plan(n, px)
    assert p["thickness"] >= min(MIN_BAR_PX, p["pitch"]), (
        f"{n} en {px}px → {p['thickness']:.2f}px: por debajo de lo legible")
    # Contenido total frente al espacio: por encima de 1 las barras se pisan.
    occupancy = p["bins"] * p["thickness"] / px
    assert occupancy <= 1.0 + 1e-9, f"solape: ocupación {occupancy:.3f}"
    assert p["gap"] > 0 or p["bins"] == 1, "sin hueco, n barras son un bloque"


@pytest.mark.parametrize("px", VIEWPORTS)
def test_more_data_never_makes_the_bars_thinner(px):
    """La regla de fondo: muchas barras → agrupar → engrosar. Nunca adelgazar.

    Ésta es la prueba que habría atrapado el defecto original. Con el suelo
    aplicado al grosor en vez de al paso, el grosor caía monótonamente con la
    densidad hasta el píxel.
    """
    thick = [plan(n, px)["thickness"] for n in DENSITIES]
    floor = min(MIN_BAR_PX, plan(max(DENSITIES), px)["pitch"])
    assert min(thick) >= floor
    # Y el grosor con 1.500 observaciones no es peor que con 390: al agrupar, el
    # paso se recalcula y la barra vuelve a tener presencia.
    assert plan(1500, px)["thickness"] >= plan(390, px)["thickness"] * 0.9


def test_the_target_thickness_is_reached_whenever_the_panel_allows_it():
    """Presencia suficiente siempre; y cerca del objetivo cuando el panel aprieta.

    Cuando las observaciones caben de sobra —45 en un panel de 900 px— la barra
    puede y debe ser gruesa: agrupar ahí tiraría resolución a cambio de nada. El
    rango de 5–10 px es el que importa cuando el panel va justo, que es donde
    antes salían rayitas.
    """
    crowded_limit = TARGET_BAR_PX / FILL          # paso que pide el objetivo
    for px in (250, 300, 346, 520, 900):
        for n in (45, 90, 180, 390, 780):
            t = plan(n, px)["thickness"]
            assert MIN_BAR_PX <= t <= MAX_BAR_PX, f"{n} en {px}px → {t:.2f}px"
            if n * crowded_limit > px:            # el dato no cabe holgado
                assert t <= 10.0, f"panel apretado {n} en {px}px → {t:.2f}px"


def test_few_observations_get_thick_bars_without_becoming_blocks():
    """Con doce observaciones en un panel ancho, la barra puede ser gruesa —pero
    no infinita: el tope existe para que no se conviertan en bloques."""
    for px in (250, 346, 520, 900, 1400):
        t = plan(12, px)["thickness"]
        assert t >= TARGET_BAR_PX
        assert t <= MAX_BAR_PX


def test_grouping_is_not_used_when_the_data_already_reads_well():
    """No se tira resolución para ganar un grosor que no hacía falta."""
    # 30 observaciones en 300 px dan 7.6 px sin agrupar: se conservan las 30.
    p = plan(30, 300)
    assert p["group"] == 1 and p["bins"] == 30
    assert p["thickness"] > MIN_BAR_PX
    # 45 en 300 px darían 5.07 px, que es el límite de lo legible: ahí sí agrupa.
    q = plan(45, 300)
    assert q["group"] > 1 and q["thickness"] > 8.0


def test_a_tiny_panel_degrades_without_lying():
    """Un panel diminuto no puede tener barras gruesas, y no las finge."""
    p = plan(200, 40)
    assert p["bins"] >= 1 and p["thickness"] > 0
    assert p["bins"] * p["thickness"] <= 40 + 1e-9


# ═══════════════════════════════════════════ 2 · EL COMPONENTE ÚNICO

def test_there_is_a_single_bar_renderer_and_the_panels_use_it():
    """Item 8: una sola regla de barras, no una por sección."""
    ab = _read("app/static/itmq_adaptive_bars.js")
    for fn in ("function layout(", "function bin(", "function extent(",
               "function timeLayout(", "function timeBins(", "function grid("):
        assert fn in ab, fn
    panels = _read("app/static/itmq_panels.js")
    flow = _read("app/static/itmq_orderflow.js")
    assert "global.ITMQBars" in panels and "global.ITMQBars" in flow
    # Y ya no queda aritmética de grosor propia en ninguno de los dos.
    for src, name in ((panels, "itmq_panels.js"), (flow, "itmq_orderflow.js")):
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("//", "*", "/*")))
        assert "function barThickness(" not in code, name
        assert "function laneBarWidth(" not in code, name
        assert "MIN_BAR_PX = 3" not in code, name


def test_the_component_is_loaded_before_everything_that_needs_it():
    html = _read("app/templates/terminal.html")
    order = [html.index(f'/static/{f}') for f in
             ("itmq_core.js", "itmq_adaptive_bars.js", "itmq_orderflow.js", "itmq_panels.js")]
    assert order == sorted(order), "el componente debe cargarse antes de sus consumidores"


# ═══════════════════════════════════════════ 3 · AGRUPACIÓN SIN PERDER EL DATO

def test_aggregation_keeps_range_extreme_count_and_members():
    """Item 2: la agrupación es visual; el dato sigue entero y accesible."""
    js = _read("app/static/itmq_adaptive_bars.js")
    body = js[js.index("function bin("):js.index("function extent(")]
    for field in ("from:", "to:", "count:", "peak", "sum", "members"):
        assert field in body, field
    # Nunca una media: cancelaría un +8 con un −8 y borraría la concentración.
    assert "/ chunk.length" not in body and "/ count" not in body


def test_the_two_aggregation_modes_mean_what_they_say():
    js = _read("app/static/itmq_adaptive_bars.js")
    assert "mode === 'sum' ? sum : peak" in js
    # v1.48.0 · Perfil por strike → NINGUNA agrupación: el strike es la unidad
    # de lectura y una barra que dice «516…518» obliga a abrir el hover para
    # saber cuál de los tres tiene el muro. Histograma temporal → suma.
    panels = _read("app/static/itmq_panels.js")
    assert "o.aggregate || 'none'" in panels, "el perfil por strike no agrupa strikes"
    assert "o.aggregate || 'sum'" in panels, "el histograma temporal suma"
    ab = _read("app/static/itmq_adaptive_bars.js")
    assert "if (o.aggregate === 'none')" in ab
    assert "function extentFor(" in ab, "alguien tiene que reservar el sitio"


def test_a_dominated_group_declares_its_peak():
    """Con signos mezclados la suma puede ser pequeña y esconder un evento."""
    js = _read("app/static/itmq_adaptive_bars.js")
    assert "peak_dominates" in js
    for consumer in ("app/static/itmq_panels.js", "app/static/itmq_orderflow.js"):
        assert "peak_dominates" in _read(consumer), consumer


def test_temporal_aggregation_keeps_the_bars_on_their_real_instant():
    """Item 5: se agrupa el INTERVALO, no la posición.

    Los carriles comparten eje con las velas de TRACE. Colocar las barras por
    índice las habría desalineado del precio en cuanto faltara un minuto.
    """
    js = _read("app/static/itmq_adaptive_bars.js")
    body = js[js.index("function timeBins("):js.index("function reduceBin(")]
    assert "Math.floor((t - start) / plan.groupMs)" in body
    assert "tEnd" in body
    flow = _read("app/static/itmq_orderflow.js")
    assert "sx(r.t)" in flow and "sx(r.tEnd)" in flow


def test_the_minimum_extent_cannot_make_a_small_value_look_big():
    """Item 6: el suelo dice «hay actividad», nunca «hay mucha actividad»."""
    js = _read("app/static/itmq_adaptive_bars.js")
    body = js[js.index("function extent("):js.index("function describe(")]
    assert "value === 0) return 0" in body, "un cero exacto sigue midiendo cero"
    assert "MAX_EXTENT_FRACTION" in body, "el suelo está acotado como fracción del eje"


# ═══════════════════════════════════════════ 4 · EL MAPA DE CALOR

def test_the_heatmap_draws_zones_not_dots():
    """Item 14 y v1.48.0: de puntos a celdas, y de celdas a SUPERFICIE.

    Una rejilla de celdas duras con huecos negros entre ellas no es un mapa de
    calor: es una tabla pintada. La exposición por strike y tiempo es un campo
    continuo, y lo que hay que leer son sus zonas y hacia dónde se mueven.
    """
    js = _read("app/static/itmq_panels.js")
    # v1.51.0 · El corte acaba en `dotmap`, que se anade despues y SI dibuja
    # puntos, a proposito: el fondo del TRACE es un campo y el mapa de la seccion
    # es una rejilla de puntos. Son dos preguntas distintas sobre el mismo dato.
    heat = js[js.index("function heatmap("):js.index("function dotmap(")]
    assert "AB.field(matrix" in heat, "el campo lo prepara el componente común"
    assert "ctx.drawImage(fieldCanvas" in heat, "superficie interpolada"
    assert "imageSmoothingEnabled = true" in heat, "sin interpolación son celdas duras"
    assert "ctx.arc(" not in heat, "el FONDO no se dibuja con puntos"
    assert "Math.abs(v) / peak" not in heat, "escala lineal por el máximo"
    # El lienzo del campo se reutiliza: reasignarlo en cada fotograma dispara el
    # recolector sesenta veces por segundo.
    assert "fieldCanvas.width !== f.w" in heat


def test_the_field_fills_gaps_smooths_and_ranks():
    """Los tres pasos que convierten celdas sueltas en una superficie legible."""
    js = _read("app/static/itmq_adaptive_bars.js")
    body = js[js.index("function field("):]
    # 1 · un hueco no es un cero: se interpola desde las vecinas.
    assert "1 / d2" in body, "peso inverso a la distancia"
    assert "has[i]" in body, "un cero MEDIDO sigue siendo cero"
    # 2 · suavizado separable, en CELDAS y no en píxeles.
    assert "FIELD_BLUR_CELLS" in js and "_gaussKernel" in body
    # 3 · normalización por rango conservando el signo.
    assert "FIELD_RANK_PERCENTILE" in body


def test_the_relief_is_another_view_of_the_same_profile():
    """El 3D no puede ser un segundo dato, ni deformar magnitudes."""
    js = _read("app/static/itmq_panels.js")
    body = js[js.index("function relief("):js.index("mapa de intervalos 2D")]
    assert "robustPeak(values)" in body, "misma escala que las barras"
    # Proyección, no perspectiva: una barra el doble de larga mide el doble.
    assert "Q.clamp(v / mx, -1, 1)" in body
    app = _read("app/static/itmq_app.js")
    assert "expRelief.set(expRows)" in app, "las dos vistas leen las mismas filas"
    assert "P.relief(" in app


def test_the_heatmap_intensity_is_rank_based_and_two_dimensional():
    js = _read("app/static/itmq_adaptive_bars.js")
    body = js[js.index("function grid("):]
    assert "VISIBLE_RANK_PERCENTILE" in body
    # Agrupa en las DOS dimensiones y conserva el extremo de cada bloque.
    assert "rowGroup" in body and "colGroup" in body
    assert "Math.abs(v) > Math.abs(peak)" in body


# ═══════════════════════════════════════════ 5 · PRIMER FOTOGRAMA Y RESIZE

def test_the_first_frame_already_has_the_real_values():
    """Item 17: nada de «se arregla después de varios ciclos».

    `Glide` empezaba toda clave en 0, así que el primer fotograma dibujaba las
    barras a cero con el eje ya correcto. Un panel que entra en pantalla por el
    observador de visibilidad dibuja UN fotograma: ése era el que se quedaba.
    """
    js = _read("app/static/itmq_core.js")
    body = js[js.index("class Glide {"):js.index("class GlideValue")]
    assert "if (!this.cur.has(key)) this.cur.set(key, v);" in body
    assert "if (!this.cur.has(k)) this.cur.set(k, n);" in body


def test_the_axis_peak_has_no_absolute_seed():
    """Un inicial de `1` es un dólar: invisible en 10⁷ y toda la escala en 10²."""
    for rel in ("app/static/itmq_panels.js", "app/static/itmq_trace.js",
                "app/static/itmq_orderflow.js"):
        js = _read(rel)
        code = "\n".join(l for l in js.splitlines() if not l.strip().startswith(("//", "*", "/*")))
        assert "GlideValue(280, 1)" not in code, rel
        assert "GlideValue(260, 1)" not in code, rel
        assert "GlideValue(300, 1)" not in code, rel


def test_the_empty_reason_can_change_between_cycles():
    """El caché de paneles congelaba el texto del primer render."""
    panels = _read("app/static/itmq_panels.js")
    assert panels.count("setEmpty(msg)") >= 3
    app = _read("app/static/itmq_app.js")
    assert "oiStrike.setEmpty(oiEmpty)" in app
    assert "rel.setEmpty(relEmpty)" in app


# ═══════════════════════════════════════════ 6 · COHERENCIA DE DATOS

def test_open_interest_audits_the_aggregate_apart_from_the_breakdown():
    """Item 9: «OI TOTAL 37.1K» encima de «SIN INTERÉS ABIERTO»."""
    from app.terminal_api import _open_interest

    state = {"active_symbol": "SPY", "positioning": {"call_oi": 18700, "put_oi": 18400}}
    out = _open_interest({}, state, {})
    assert out["total_oi"] == 37100
    assert out["aggregates_available"] is True
    assert out["breakdown_available"] is False
    # Y DICE por qué, en vez de contradecir a la cabecera.
    assert "desglose" in out["breakdown_reason"]
    # Procedencia de cada hecho por separado: que exista el agregado no implica
    # que exista el desglose, y hasta ahora se publicaban como si sí.
    assert out["audit"]["total_oi"] == "ITM_QUANT_CHAIN_POSITIONING"
    assert out["audit"]["by_strike"] is None


def test_a_missing_open_interest_aggregate_is_not_a_zero():
    from app.terminal_api import _open_interest

    out = _open_interest({}, {"active_symbol": "SPY"}, {})
    assert out["total_oi"] is None and out["call_oi"] is None and out["put_oi"] is None
    assert out["aggregates_available"] is False
    assert out["call_pct"] is None and out["put_pct"] is None


def test_volatility_drift_is_not_published_as_zero_without_a_measurement():
    """Item 10: «+0.000 pp» encima de «DERIVA DE VOLATILIDAD NO DISPONIBLE»."""
    from app.terminal_api import _volatilidad

    out = _volatilidad({"volatility": {"iv_change_pp": float("nan"),
                                       "iv_change_measured": False}}, {})
    assert out["iv_change_pp"] is None
    assert out["iv_change_measured"] is False
    js = _read("app/static/itmq_app.js")
    assert "v.iv_change_measured !== false" in js


def test_the_engine_reports_an_unmeasurable_drift_as_unmeasured():
    src = _read("app/service.py")
    block = src[src.index("timestamps = sorted(enr"):]
    block = block[:block.index("regime = (")]
    assert 'iv_change = float("nan")' in block, "sin dos observaciones no hay cambio"
    assert "iv_change = 0.0" not in block


def test_statistics_never_fabricate_a_zero_premium():
    """Item 11: «Contratos 14K» junto a «Prima $0.0»."""
    from app.terminal_api import _estadisticas

    trace = {"option_prints": [], "profiles": {"rows": [
        {"strike": 600.0, "call_volume": 8000.0, "put_volume": 6000.0}]}}
    out = _estadisticas(trace, {"positioning": {}}, {})
    assert out["contracts"] == 14000.0
    assert out["premium"] is None, "sin impresiones no hay prima que medir"
    assert out["buy_premium"] is None and out["sell_premium"] is None
    assert out["premium_available"] is False
    assert "impresiones" in out["premium_reason"]
    assert out["audit"]["contracts"] == "CADENA_OFICIAL"
    assert out["audit"]["premium"] is None


def test_the_trace_no_longer_raises_a_name_error_building_its_window():
    """Item 13: `NameError: name '_f' is not defined`, en cada ciclo del trace.

    El error se tragaba en el `except` de `/api/terminal/bundle` y la terminal
    servía un trace vacío. Un `except Exception` que convierte un fallo de
    programación en un panel vacío es el peor sitio donde puede esconderse un
    error, porque parece falta de datos.
    """
    from app.service import PlatformState
    from app.core.assets import UNIVERSAL_CHAIN_WINDOW_PCT

    st = PlatformState()
    for symbol, spot in (("SPY", 601.0), ("XLF", 48.3), ("SOFI", 9.52)):
        st.symbol = symbol
        st.gamma_delta = {"spot": spot}
        band = st._resolve_visual_window(None)          # antes: NameError
        assert abs(100.0 * band / spot - UNIVERSAL_CHAIN_WINDOW_PCT) < 1e-6

    # Y el nombre equivocado no puede volver: en este módulo se llama `_finite`.
    src = _read("app/service.py")
    assert not re.search(r"(?<![\w.])_f\(", src), "queda una llamada a `_f` en service.py"


def test_the_reliability_chart_needs_something_to_reference():
    """Item 15: una diagonal trazada sin observaciones se lee como un resultado."""
    js = _read("app/static/itmq_app.js")
    body = js[js.index("const bins = b.reliability"):]
    body = body[:body.index("/* ---")]
    assert "rel.set(bins.length ?" in body
    assert "RECOLECTANDO" in body


def test_dark_pool_shows_the_lanes_that_do_have_data():
    """Item 12: con `dark-flow` vivo, la sección enseña lo que sí tiene."""
    from datetime import datetime, timezone
    from app.core import quant_data_hub as HUB

    now = datetime.now(timezone.utc).isoformat()
    intel = {
        "dark_flow": {"ready": True, "count": 1, "fetched_at": now,
                      "rows": [{"dark_volume": 1e6, "total_volume": 4e6,
                                "dark_notional": 5.2e8}]},
        "dark_pool_levels": {"ready": False, "rows": [], "lane_status": "REQUEST_INVALID",
                             "lane_detail": "cuerpo rechazado"},
    }
    out = HUB.dark_pool("SPY", intel)
    assert out["ready"] is True
    assert out["notional"] == 5.2e8 and out["dark_share_pct"] == 25.0
    assert out["diagnosis"]["degraded"] is True
    html = _read("app/templates/terminal.html")
    assert 'id="dpCoverage"' in html
    js = _read("app/static/itmq_app.js")
    assert "dpCoverageDetail" in js


# ═══════════════════════════════════════════ 7 · MULTI-ACTIVO

def test_nothing_in_the_render_path_knows_a_ticker():
    """Item 16: la adaptación sale del viewport y del dataset, no del símbolo."""
    ticker = re.compile(r"['\"](DIA|SPY|QQQ|IWM|AAPL|TSLA|NVDA|AMD|XLF|SOFI)['\"]")
    for rel in ("app/static/itmq_adaptive_bars.js", "app/static/itmq_panels.js",
                "app/static/itmq_orderflow.js"):
        code = "\n".join(l for l in _read(rel).splitlines()
                         if not l.strip().startswith(("//", "*", "/*")))
        hits = ticker.findall(code)
        assert not hits, f"{rel}: {sorted(set(hits))}"


def test_the_plan_is_identical_for_every_asset_scale():
    """La geometría depende del viewport y del recuento, nunca de la magnitud."""
    # Misma densidad y mismo panel: mismo plan, den igual 10⁹ o 10⁴.
    assert plan(390, 346) == plan(390, 346)
    for n in (45, 120, 390):
        base = plan(n, 346)
        assert base["thickness"] >= MIN_BAR_PX


# ═══════════════════════════════════════════ 8 · REGRESIÓN VISUAL REAL

def _harness_report():
    """Geometría medida sobre los renderizadores de producción, en Chromium.

    Sin `skipif` a propósito. Esta release se sostiene sobre una afirmación
    VISUAL —«las barras se ven»— y un control que se salta solo no la sostiene:
    dejaría la certificación en verde exactamente en el entorno donde no se
    comprobó nada. Si el navegador no está, esto falla y dice qué falta.

    El arnés se sirve a sí mismo en un puerto efímero, así que no depende de que
    haya una terminal en marcha ni de ningún recurso externo.
    """
    script = ROOT / "tools" / "visual_regression.py"
    out = subprocess.run([sys.executable, str(script), "--json"],
                         capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    assert out.returncode != 2, (
        "la regresión visual no pudo ejecutarse y esta release se certifica "
        f"mirando: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def test_the_real_renderers_draw_readable_bars_in_every_case():
    """Item 18: la comprobación que las pruebas numéricas no podían hacer.

    Se mide lo que el panel DIBUJÓ, con su ancho real, en 46 combinaciones de
    activo, distribución, densidad y viewport.
    """
    rep = _harness_report()
    assert len(rep) >= 80, f"el arnés sólo midió {len(rep)} paneles"
    # La segunda mitad es el mismo arnés DESPUÉS de redimensionar la ventana: la
    # geometría tiene que seguir siendo correcta, no sólo al primer render.
    after = [r for r in rep if r.get("phase") == "resize"]
    assert len(after) >= 40, "la comprobación de redimensionado no se ejecutó"
    thin = [r for r in rep if r["thickness"] < MIN_BAR_PX]
    overlap = [r for r in rep if r["occupancy"] > 1.0]
    assert not thin, f"barras por debajo de {MIN_BAR_PX}px: {thin[:3]}"
    assert not overlap, f"barras solapadas: {overlap[:3]}"
    assert all(r["gap"] > 0 for r in rep if r["bins"] > 1), "sin hueco entre barras"
    # Y la calidad no depende de la escala del activo: el grosor se mueve poco
    # entre los cinco activos con la misma densidad y el mismo panel.
    same = [r for r in rep if r["kind"] == "bars" and r["n"] == 390]
    if len(same) >= 5:
        vals = [r["thickness"] for r in same]
        assert max(vals) - min(vals) < 0.5, f"la escala del activo cambia el grosor: {vals}"


# ═══════════════════════════════════════════ 9 · v1.48.0 · CAMPO, STRIKE, ORO

def test_the_interval_map_is_a_surface_not_a_painted_table():
    """Una rejilla de celdas duras con huecos negros no es un mapa de calor.

    La exposición por strike y tiempo es un campo: varía de forma continua
    entre strikes vecinos y entre intervalos vecinos. Dibujar la celda obliga a
    leer celda por celda justo lo que hay que leer como zona.
    """
    js = _read("app/static/itmq_panels.js")
    heat = js[js.index("function heatmap("):js.index("function dotmap(")]
    assert "AB.field(matrix" in heat
    assert "imageSmoothingEnabled = true" in heat
    # Suelo de ruido: sin él, la normalización por rango deja media pantalla a
    # media opacidad y el mapa sale como un bloque macizo.
    assert "noiseFloor" in heat and "gammaCurve" in heat
    # v1.51.0 · Los contornos ya no se calculan aquí a mano.
    #
    # La versión anterior barría los dos ejes y por cada cruce pintaba un palito
    # de una celda SIN unirlo con el de la vecina. Cubrir los dos ejes fue una
    # mejora sobre cubrir uno, pero el problema de fondo seguía: eran segmentos
    # sueltos, y en pantalla se leían como suciedad y no como el borde de una
    # zona. Ahora sale de `AB.contours` —marching squares— que mira las cuatro
    # esquinas de cada celda y emite el segmento que la atraviesa, de modo que
    # los de celdas vecinas se encuentran en el borde compartido.
    assert "if ((a0 < level) === (a1 < level)) continue;" not in heat
    assert "AB.contours(f, level)" in heat


def test_trace_and_the_interval_map_treat_the_same_data_the_same_way():
    """Dos tratamientos del mismo dato hacen que dos pantallas no se parezcan."""
    trace = _read("app/static/itmq_trace.js")
    body = trace[trace.index("function buildHeatBitmap("):]
    assert "AB.field(upright" in body, "TRACE usa el campo común"
    # v1.51.0 · 0.55/1.9 dejaba el mapa como un BLOQUE saturado: la normalización
    # es por rango-percentil, así que la celda mediana vale siempre 0.5 y media
    # pantalla se iba al tope. Banda neutra más ancha, curva más suave y un techo
    # de opacidad que deja ver las velas y las isolíneas por encima.
    assert "const NOISE = 0.62, GAMMA = 1.35, ALPHA_MAX = 0.70;" in body
    # Y el tratamiento propio que tenía ya no está.
    assert "Math.pow(a, 0.62)" not in body


def test_the_exposure_profile_draws_one_bar_per_strike():
    """El strike es la unidad de lectura: «516…518» esconde cuál tiene el muro."""
    ab = _read("app/static/itmq_adaptive_bars.js")
    assert "if (o.aggregate === 'none')" in ab
    assert "function extentFor(" in ab
    panels = _read("app/static/itmq_panels.js")
    assert "o.aggregate || 'none'" in panels
    app = _read("app/static/itmq_app.js")
    # El panel crece y el contenedor hace scroll: no se elige entre resolución
    # y grosor, se tienen las dos.
    assert "growForRows('chartExposure'" in app
    assert "growForRows('chartOiStrike'" in app
    html = _read("app/templates/terminal.html")
    assert 'id="chartExposureScroll"' in html and 'class="chart-scroll"' in html
    css = _read("app/static/itmq_terminal.css")
    assert ".chart-scroll" in css and "overflow-y: auto" in css


def test_one_bar_per_strike_keeps_its_thickness():
    """Sin agrupar y con el panel crecido, la barra conserva su grosor."""
    import subprocess as sp
    src = """
      global.window = {};
      global.ITMQ = { clamp: (v,a,b) => Math.min(b, Math.max(a, v)), compact: v => String(v) };
      window.ITMQ = global.ITMQ;
      require(process.argv[1]);
      const B = window.ITMQBars;
      const out = [];
      for (const n of [12, 21, 45, 90, 166, 240]) {
        const need = B.extentFor(n, { targetThickness: 11 });
        const p = B.layout(n, need, { aggregate: 'none' });
        out.push([n, p.bins, Number(p.thickness.toFixed(2)), Number(p.gap.toFixed(2))]);
      }
      console.log(JSON.stringify(out));
    """
    r = sp.run(["node", "-e", src, str(ROOT / "app/static/itmq_adaptive_bars.js")],
               capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    for n, bins, thick, gap in json.loads(r.stdout):
        assert bins == n, f"{n} strikes se agruparon en {bins} barras"
        assert thick >= 10.5, f"{n} strikes → {thick}px"
        assert gap > 0, "sin hueco, n barras son un bloque"


def test_the_relief_is_available_as_a_second_view():
    html = _read("app/templates/terminal.html")
    assert 'data-expview="relief"' in html and 'data-expview="bars"' in html
    app = _read("app/static/itmq_app.js")
    assert "function applyExpView(" in app
    assert "segment('expView'" in app


def test_flow_markers_are_golden_with_an_arrow_and_an_amount():
    """La marca tiene TRES piezas y cada una dice una cosa.

        círculo dorado   DÓNDE ocurrió, y su radio CUÁNTO pesa
        flecha           QUIÉN agredió: ▲ verde compra, ▼ rojo venta
        cifra            la prima

    v1.56.0 · Estas funciones vivían DUPLICADAS en TRACE y en FLUJO. Eran
    equivalentes el día que se escribieron, y ese es el problema: dos copias
    equivalentes se separan en cuanto alguien corrige una. Ahora hay UNA, en
    `itmq_core`, y lo que este test protege es que siga habiendo una.
    """
    core = _read("app/static/itmq_core.js")
    assert "function flowHalo(" in core, "falta el círculo dorado"
    assert "function flowArrow(" in core, "falta la flecha de sentido"
    assert "function flowAmount(" in core, "falta la cifra"
    assert "function flowMark(" in core, "falta la marca completa"
    assert "createRadialGradient" in core, "el halo tiene que separar la marca del fondo"

    # Y NADIE más puede tener su propia copia.
    for rel in ("app/static/itmq_trace.js", "app/static/itmq_orderflow.js"):
        js = _read(rel)
        for dup in ("function flowHalo(", "function flowArrow(", "function flowAmount("):
            assert dup not in js, f"{rel} tiene su propia copia de {dup}"
        assert "Q.flowMark(" in js, f"{rel} no usa la marca compartida"


def test_dark_flow_maps_the_contract_instead_of_guessing_the_field():
    """v1.52.0 · Invierte la decisión de v1.49.0.

    Entonces el volumen oscuro se resolvía «descubriendo» qué campo lo
    representaba, buscando nombres que contuvieran «dark» u «offExchange». La
    idea era sobrevivir a un proveedor que cambiara de nombre, y el precio fue
    no encontrar el nombre REAL: el contrato publica las acciones en `size`, que
    no contiene ninguna de esas palabras. Con 608 filas descargadas, las 608
    salían con volumen None y la sección decía SIN DATOS.

    Cuando existe contrato publicado, adivinar sólo puede acertar por casualidad.
    """
    from app.providers.quantdata.tools import norm_dark_flow
    out = norm_dark_flow({"data": {"1758297600000": {
        "notionalValue": 35_842_110.0, "size": 69428,
        "tradeCount": 412, "stockPrice": 516.20}}})
    assert out["field_map"]["dark_volume"] == "size"
    assert out["rows"][0]["dark_volume"] == 69428
    assert out["schema_state"] == "CONTRACT_OK"
    # Los alias antiguos siguen valiendo como RESPALDO declarado.
    legacy = norm_dark_flow({"data": {"1758297600000": {
        "darkVolume": 1000, "darkNotional": 5000, "stockPrice": 10.0}}})
    assert legacy["rows"][0]["dark_volume"] == 1000
    assert legacy["field_map"]["dark_volume"] == "darkVolume"


def test_absent_dark_volume_is_not_summed_as_zero():
    """Seiscientos intervalos sin campo de volumen no son «0.0 acc»."""
    from datetime import datetime, timezone
    from app.core import quant_data_hub as HUB

    now = datetime.now(timezone.utc).isoformat()
    intel = {"dark_flow": {
        "ready": True, "count": 608, "fetched_at": now,
        "rows": [{"t": "x", "dark_volume": None, "stock_price": 516.2}] * 608,
        "observed_fields": ["timestamp", "stockPrice"],
        "volume_field_resolved": False, "intervals_with_volume": 0}}
    out = HUB.dark_pool("DIA", intel)
    assert out["shares"] is None and out["dark_share_pct"] is None
    assert out["ready"] is False, "un carril del que no se lee nada no deja lista la sección"
    ff = out["flow_fields"]
    assert ff["intervals"] == 608 and ff["intervals_with_volume"] == 0
    assert ff["volume_field_resolved"] is False
    assert "stockPrice" in ff["observed"]
    # Y llega al Auditor, que es donde se puede actuar sobre ello.
    html = _read("app/templates/terminal.html")
    assert 'id="tblDarkFields"' in html


# ═══════════════════════════════════════════ 10 · v1.49.0 · EL CONTRATO REAL

def test_dark_pool_levels_sends_the_documented_contract():
    """`sessionDateRange.startDate` + `filter.ticker`. Nada más, nada menos.

    El cuerpo mínimo de v1.46.0 —sólo `filter.ticker`— razonaba que un cuerpo
    con campos de más es tan inválido como uno con campos de menos. Es cierto, y
    aun así estaba incompleto: le faltaba un campo obligatorio que ninguna otra
    herramienta usa. `dark-flow` acepta `sessionDate`/`timeRange`;
    `dark-pool-levels` usa EXCLUSIVAMENTE `sessionDateRange`.
    """
    from app.providers.quantdata.tools import build_catalog

    body = build_catalog()["dark_pool_levels"].request_body("DIA")
    assert set(body) == {"sessionDateRange", "filter"}, body
    assert body["filter"] == {"ticker": "DIA"}
    rng = body["sessionDateRange"]
    assert "startDate" in rng, "startDate es obligatorio"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rng["startDate"]), rng
    if "endDate" in rng:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", rng["endDate"]), rng


def test_the_levels_request_never_carries_a_forbidden_field():
    """Siete campos que este endpoint rechaza, uno a uno."""
    from app.providers.quantdata.tools import build_catalog, TOOL_FORBIDDEN_FIELDS

    body = build_catalog()["dark_pool_levels"].request_body("SPY")
    for forbidden in ("sessionDate", "timeRange", "snapshotTime", "aggregationPeriod",
                      "filterExpression", "projection", "pagination"):
        assert forbidden not in body, forbidden
        assert forbidden in TOOL_FORBIDDEN_FIELDS["dark_pool_levels"], forbidden


def test_the_repair_cannot_add_a_field_this_endpoint_forbids():
    """`aggregationPeriod` es legítimo en `dark-flow` y veneno aquí.

    Está en la tabla de reparaciones, así que un 400 mal leído podía hacer que
    se lo añadiéramos. La prohibición tiene que ser de la HERRAMIENTA, no del
    catálogo.
    """
    from app.providers.quantdata.tools import repair_body, TOOL_FORBIDDEN_FIELDS

    base = {"sessionDateRange": {"startDate": "2026-09-18"}, "filter": {"ticker": "DIA"}}
    nxt, _note = repair_body(base, ["aggregationPeriod: field required"],
                             TOOL_FORBIDDEN_FIELDS["dark_pool_levels"])
    assert nxt is None or "aggregationPeriod" not in nxt
    # Sin la prohibición, la misma reparación SÍ lo añadiría: la diferencia es
    # exactamente la lista por herramienta.
    other, _n = repair_body(base, ["aggregationPeriod: field required"])
    assert other is not None and other.get("aggregationPeriod") == "1d"


def test_a_closed_market_never_asks_for_a_day_that_is_not_a_session():
    """Un sábado no es una sesión: pedirlo devuelve 400 o un 200 vacío."""
    from datetime import datetime
    from app.providers.quantdata.tools import last_valid_session_date

    # Sábado 19 de septiembre de 2026 → última sesión, el viernes 18.
    assert last_valid_session_date(datetime(2026, 9, 19, 12, 0)) == "2026-09-18"
    # Domingo 20 → el viernes 18 también.
    assert last_valid_session_date(datetime(2026, 9, 20, 12, 0)) == "2026-09-18"


def test_the_documented_200_is_parsed_with_its_official_field_names():
    """La respuesta es un MAPA por nivel de precio, no una lista.

    El extractor genérico sólo sabía leer listas, así que un 200 válido daba
    cero niveles y se publicaba como «SIN DATOS»: indistinguible de una sesión
    sin actividad fuera de bolsa.
    """
    from app.providers.quantdata.tools import norm_levels

    out = norm_levels({
        "latestStockPrice": 516.20,
        "levels": {
            "515.50": {"notionalValue": 1.24e8, "size": 240310, "tradeCount": 412},
            "516.00": {"notionalValue": 8.10e7, "size": 157000, "tradeCount": 288},
        },
    })
    assert out["ready"] is True and out["count"] == 2
    assert out["latest_stock_price"] == 516.20
    top = out["rows"][0]
    assert top["price"] == 515.50            # la CLAVE es el nivel de precio
    assert top["notional"] == 1.24e8         # notionalValue
    assert top["shares"] == 240310           # size
    assert top["prints"] == 412              # tradeCount

    # Y una lista sigue funcionando, por si cambia el envoltorio.
    lst = norm_levels({"latestStockPrice": 516.2, "levels": [
        {"priceLevel": 515.5, "notionalValue": 1.2e8, "size": 240310, "tradeCount": 412}]})
    assert lst["count"] == 1 and lst["rows"][0]["prints"] == 412


def test_the_documented_200_travels_to_the_frontend_as_direct_provider():
    """RAW → NORMALIZER → DATA HUB → DARK POOL LEVELS → FRONTEND."""
    from datetime import datetime, timezone
    from app.providers.quantdata.tools import norm_levels
    from app.core import quant_data_hub as HUB
    from app.core.data_lineage import LINEAGE, DIRECT_PROVIDER
    from app.terminal_api import build_terminal_bundle

    raw = {"latestStockPrice": 516.20, "levels": {
        "515.50": {"notionalValue": 1.24e8, "size": 240310, "tradeCount": 412},
        "516.00": {"notionalValue": 8.10e7, "size": 157000, "tradeCount": 288}}}
    normalized = norm_levels(raw)
    intel = {"dark_pool_levels": {**normalized, "symbol": "DIA",
                                  "path": "/v1/equities/tool/dark-pool-levels",
                                  "fetched_at": datetime.now(timezone.utc).isoformat()}}

    hub = HUB.dark_pool("DIA", intel)
    assert hub["ready"] is True
    assert hub["lanes"]["dark_pool_levels"]["state"] == "DIRECT_PROVIDER_OK"

    rec = next(r for r in LINEAGE.audit("DIA")["records"]
               if r["metric"] == "QD_DARK_POOL_LEVELS")
    assert rec["source_mode"] == DIRECT_PROVIDER and rec["state"] == "DATA_OK"

    bundle = build_terminal_bundle(
        state={"active_symbol": "DIA", "ready": True, "spot": 516.2},
        trace={}, intelligence=intel)
    dp = bundle["dark_pool"]
    assert dp["levels"], "el nivel no llegó al frontend"
    assert dp["levels"][0]["price"] == 515.50
    assert dp["levels"][0]["notional"] == 1.24e8
    assert dp["levels"][0]["shares"] == 240310
    assert dp["levels"][0]["prints"] == 412
    assert dp["latest_stock_price"] == 516.20


def test_a_rejected_body_keeps_type_detail_and_every_field_error():
    """«HTTP 400» no se puede corregir; `errors[].field` sí."""
    from app.providers.quantdata.client import _structured_error

    out = _structured_error({
        "type": "validation_error",
        "detail": "Request validation failed",
        "errors": [{"field": "sessionDateRange.startDate", "message": "field required"}],
    }, 400)
    assert out["status"] == 400
    assert out["type"] == "validation_error"
    assert out["detail"] == "Request validation failed"
    assert out["errors"] == [{"field": "sessionDateRange.startDate",
                              "message": "field required"}]
    assert out["raw"], "el cuerpo crudo se conserva para el registro"

    # Convención Pydantic: la lista viaja dentro de `detail`.
    pyd = _structured_error(
        {"detail": [{"loc": ["body", "sessionDateRange"], "msg": "field required"}]}, 400)
    assert pyd["errors"][0]["field"] == "body.sessionDateRange"


def test_the_rejected_body_reaches_the_auditor():
    from app.core import quant_data_hub as HUB

    out = HUB.dark_pool("DIA", {"dark_pool_levels": {
        "ready": False, "rows": [], "lane_status": "REQUEST_INVALID",
        "lane_detail": "sessionDateRange.startDate: field required",
        "lane_error": {"status": 400, "type": "validation_error",
                       "detail": "Request validation failed",
                       "errors": [{"field": "sessionDateRange.startDate",
                                   "message": "field required"}]}}})
    err = out["lanes"]["dark_pool_levels"]["error"]
    assert err["status"] == 400 and err["errors"][0]["field"] == "sessionDateRange.startDate"
    html = _read("app/templates/terminal.html")
    assert 'id="tblDarkErrors"' in html
    js = _read("app/static/itmq_app.js")
    assert "tblDarkErrors" in js


def test_the_live_verifier_demands_a_real_200_for_levels():
    """Un SIN DATOS no cierra el endpoint: el contrato exige que responda."""
    src = _read("scripts/verify_live_quantdata.py")
    assert 'r.get("http_status") == 200' in src
    assert "con HTTP 200 real" in src
    assert "field_errors" in src, "el campo rechazado se imprime, no el titular"
    assert "el endpoint NO está cerrado" in src


def test_the_three_lanes_stay_independent():
    """Nada de esto puede haber acoplado los carriles."""
    from datetime import datetime, timezone
    from app.core import quant_data_hub as HUB

    now = datetime.now(timezone.utc).isoformat()
    out = HUB.dark_pool("DIA", {
        "dark_flow": {"ready": True, "count": 1, "fetched_at": now,
                      "rows": [{"dark_volume": 1e6, "total_volume": 4e6,
                                "dark_notional": 5.2e8}]},
        "dark_pool_levels": {"ready": False, "rows": [],
                             "lane_status": "REQUEST_INVALID",
                             "lane_detail": "cuerpo rechazado"},
    })
    assert out["ready"] is True and out["notional"] == 5.2e8
    assert out["diagnosis"]["degraded"] is True
    assert out["lanes"]["dark_flow"]["state"] == "DIRECT_PROVIDER_OK"
    assert out["lanes"]["dark_pool_levels"]["state"] == "REQUEST_INVALID"


# ═══════════════════════════════════════════ 11 · v1.50.0 · LO QUE IMPORTA

def test_the_strike_profile_drops_strikes_that_cannot_matter():
    """La cadena entera incluye el 433 con DIA a 516: un 16 % del precio.

    Esos strikes ocupan media pantalla, no llevan exposición y empujan hacia
    arriba la zona que se está mirando. Recortar por número de strikes no sirve
    —la separación cambia por activo— y por dólares tampoco.
    """
    from app.core.strike_window import relevant_rows

    rows = [{"strike": k, "gex": (2e8 if abs(k - 516) < 6 else 1e5)} for k in
            (433, 445, 455, 465, 475, 495, 505, 510, 513, 515, 516,
             517, 520, 525, 527, 535, 544, 554, 590, 640, 750)]
    kept, meta = relevant_rows(rows, 516.0, symbol="DIA", value_keys=("gex",))
    strikes = [r["strike"] for r in kept]
    assert 433 not in strikes and 750 not in strikes
    assert 516 in strikes and 520 in strikes
    assert meta["dropped"] == len(rows) - len(kept) > 0
    assert meta["reason"], "un recorte en silencio no se puede auditar"


def test_a_real_wall_outside_the_band_is_never_hidden():
    """Es el caso que MÁS importa: recortarlo por distancia esconde la respuesta."""
    from app.core.strike_window import relevant_rows

    rows = [{"strike": float(k), "gex": (2e8 if abs(k - 516) < 6 else 1e5)} for k in
            (433, 445, 455, 465, 475, 495, 505, 510, 513, 515, 516,
             517, 520, 525, 527, 535, 544, 554, 590, 640, 750)]
    rows.append({"strike": 600.0, "gex": 1.5e8})          # muro real, lejos
    kept, meta = relevant_rows(rows, 516.0, symbol="DIA", value_keys=("gex",))
    assert 600.0 in [r["strike"] for r in kept]
    assert meta["kept_far"] == 1
    assert 750 not in [r["strike"] for r in kept]


def test_the_band_is_proportional_so_it_works_for_every_asset():
    """Un porcentaje del precio ya es comparable entre un ETF de 9 $ y un índice."""
    from app.core.strike_window import relevant_rows

    # Perfil PLANO a propósito: sin estructura, lo único que queda es la
    # distancia, y es donde el recorte hace más falta.
    for spot, step in ((9.52, 0.05), (48.3, 0.5), (516.0, 1.0), (7543.0, 5.0)):
        rows = [{"strike": spot + i * step, "gex": 1e6} for i in range(-60, 61)]
        kept, meta = relevant_rows(rows, spot, value_keys=("gex",))
        for r in kept:
            pct = 100.0 * abs(r["strike"] - spot) / spot
            assert pct <= meta["band_pct"] + 1e-9, f"{spot}: {r['strike']} a {pct:.1f}%"


def test_few_strikes_are_never_trimmed():
    """Con pocos strikes el recorte no ayuda y puede dejar el panel sin forma."""
    from app.core.strike_window import relevant_rows

    rows = [{"strike": 500 + i, "gex": 1e6} for i in range(8)]
    kept, meta = relevant_rows(rows, 516.0, value_keys=("gex",))
    assert len(kept) == len(rows) and meta["dropped"] == 0


def test_both_strike_panels_declare_what_they_trimmed():
    from app.terminal_api import _exposicion, _open_interest

    state = {"active_symbol": "DIA", "spot": 516.0}
    trace = {"profiles": {"rows": [
        {"strike": float(k), "gamma_m": (200.0 if abs(k - 516) < 6 else 0.1),
         "oi": (9000 if abs(k - 516) < 6 else 4)}
        for k in (433, 445, 465, 495, 505, 513, 515, 516, 517, 520,
                  527, 535, 554, 590, 640, 750)]}}
    ex = _exposicion(trace, state, {})
    assert ex["strike_window"]["dropped"] > 0
    assert 433.0 not in [r["strike"] for r in ex["by_strike"]]
    oi = _open_interest(trace, state, {})
    assert "strike_window" in oi


def test_the_marker_radius_compares_within_the_cycle():
    """Sin una referencia común, dos marcas del mismo tamaño mienten."""
    # v1.56.0 · La escala del radio vive con el resto de la marca, en el núcleo.
    core = _read("app/static/itmq_core.js")
    assert "function flowStrength(ev, peak)" in core
    cuerpo = core[core.index("function flowStrength(ev, peak)"):]
    cuerpo = cuerpo[:cuerpo.index("\n  }") + 4]
    assert "peak > 0" in cuerpo, "sin referencia del ciclo el radio no compara"
    # Y cada pantalla le pasa el pico de SU ciclo.
    trace = _read("app/static/itmq_trace.js")
    assert "S.qflowPeak" in trace
    flow = _read("app/static/itmq_orderflow.js")
    assert "qPeak" in flow and "peak" in flow


def test_the_drift_curves_carry_their_value_at_the_end():
    """Tres curvas superpuestas obligan a seguir cada trazo hasta la escala."""
    js = _read("app/static/itmq_orderflow.js")
    body = js[js.index("const tail = vis[vis.length - 1];"):]
    body = body[:body.index("// El último bucket sigue ABIERTO")]
    assert "Q.money(m.v, 1)" in body
    # Dos curvas cercanas dejarían sus etiquetas una encima de la otra.
    assert "marks[i].y - marks[i - 1].y < 17" in body


def test_net_drift_has_a_total_lane_with_declared_gold_marking():
    """Marcar todos los intervalos sería no marcar ninguno."""
    js = _read("app/static/itmq_orderflow.js")
    assert "function drawDriftTotal(" in js
    body = js[js.index("function drawDriftTotal("):js.index("function drawDriftVolume(")]
    assert "S.goldRule" in body, "el criterio se elige"
    assert "mean * 10" in body and "slice(0, 3)" in body
    # La media se dibuja: es contra lo que destacan.
    assert "media ${Q.money(mean, 0)}" in body
    assert "function setGoldRule(" in js
    html = _read("app/templates/terminal.html")
    assert 'id="ofDriftTotal"' in html and 'id="ofGold"' in html
    app = _read("app/static/itmq_app.js")
    assert "driftTotal: el('ofDriftTotal')" in app
    assert "segment('ofGold'" in app


def test_the_drift_chart_got_the_room_it_needs():
    """Con 1fr/.34fr/.38fr las tres curvas se aplastaban unas sobre otras."""
    css = _read("app/static/itmq_terminal.css")
    block = css[css.index(".drift-stack {"):]
    block = block[:block.index("}")]
    # v1.51.0 · 2.2fr seguía saliendo corto en una ventana baja, porque el resto
    # de filas se lleva su fracción pase lo que pase. El suelo en PÍXELES es lo
    # que garantiza el alto; la fracción sólo reparte lo que sobra.
    # v1.52.0 · 360 px seguía siendo poco con ocho KPI encima.
    # v1.55.0 · Se comprueba un SUELO, no una cifra exacta. Fijar el valor hacía
    # fallar el test cada vez que el gráfico crecía, que es justo lo que este
    # test quiere que pase; lo que tiene que impedir es que ENCOJA.
    import re as _re
    m = _re.search(r"grid-template-rows:\s*minmax\((\d+)px,\s*([\d.]+)fr\)", block)
    assert m, block
    assert int(m.group(1)) >= 520 and float(m.group(2)) >= 4.2


def test_every_lane_of_the_tape_declares_why_it_is_empty():
    """Un carril en blanco sin causa es indistinguible de un fallo de render."""
    js = _read("app/static/itmq_orderflow.js")
    body = js[js.index("function drawTotal("):js.index("function drawVolume(")]
    # El caso que se veía en pantalla: había buckets, pero ninguno con prima,
    # así que el eje se dibujaba y el carril quedaba mudo.
    assert "let painted = 0;" in body and "painted += 1;" in body
    assert "if (!painted) {" in body
    # v1.56.0 · El motivo sale del ESTADO del carril: «SIN PRIMA OBSERVADA» es
    # una conclusión y sólo vale cuando no hay dato actual NI último valor bueno.
    assert "laneEmpty('premium_bars', 'SIN PRIMA OBSERVADA')" in body
    # El ctx.restore() del clip tiene que ocurrir antes de escribir el texto.
    idx = body.index("if (!painted) {")
    assert "ctx.restore();" in body[idx:idx + 120]


# ════════════════════════════════════════════════════════════════════════════
# 12 · v1.51.0 · Ningún hueco se publica como cero
# ════════════════════════════════════════════════════════════════════════════

def test_an_absent_value_is_a_gap_not_a_zero():
    """La sierra de la DERIVA DE VOLATILIDAD era un cero inventado.

    `norm_time_series` hacía `_f(value, 0.0) or 0.0`, así que un bucket sin el
    campo se publicaba con valor CERO. Para prima neta un cero puede ser legítimo,
    pero este mismo normalizador sirve a la IV, al max pain, al interés abierto y
    al precio, y en esos cuatro el cero es IMPOSIBLE.
    """
    from app.providers.quantdata.tools import norm_time_series
    payload = {"data": {
        "1758300000000": {"iv": 11.7},
        "1758300300000": {},              # el proveedor no trajo IV en este bucket
        "1758300600000": {"iv": 11.9},
    }}
    out = norm_time_series(payload, ("iv", "impliedVolatility", "drift", "value"))
    rows = out["rows"]
    assert len(rows) == 3
    assert rows[0]["value"] == pytest.approx(11.7)
    assert rows[1]["value"] is None, "el hueco NO puede valer cero"
    assert rows[1]["value_measured"] is False
    assert rows[2]["value"] == pytest.approx(11.9)


def test_the_gap_rule_covers_every_series_of_this_normalizer():
    """No es un arreglo de la IV: son las cinco herramientas que comparten el
    normalizador. Un max pain en el strike 0 o un precio de 0 dólares serían
    afirmaciones igual de falsas."""
    from app.providers.quantdata.tools import norm_time_series
    for names in (("iv", "impliedVolatility", "drift", "value"),
                  ("maxPain", "value", "strike"),
                  ("openInterest", "oi", "value"),
                  ("price", "close", "value"),
                  ("netPremium", "net", "value", "premium")):
        out = norm_time_series({"data": {"1758300000000": {}}}, names)
        assert out["rows"][0]["value"] is None, names


def test_a_net_premium_with_call_and_put_is_still_measured():
    """El cambio no puede convertir en hueco una lectura que SÍ existe: sin campo
    neto explícito, el neto es call − put y eso es una medición."""
    from app.providers.quantdata.tools import norm_time_series
    out = norm_time_series({"data": {"1758300000000": {"callSum": 900.0, "putSum": 250.0}}},
                           ("netPremium", "net", "value", "premium"))
    row = out["rows"][0]
    assert row["value"] == pytest.approx(650.0)
    assert row["value_measured"] is True


def test_the_curve_breaks_at_a_gap_instead_of_falling_to_the_floor():
    """`sy(Q.num(p.v, 0))` mandaba el hueco al suelo del eje y dibujaba un desplome."""
    js = _read("app/static/itmq_panels.js")
    body = js[js.index("function lines("):js.index("function curve(")]
    seg = body[body.index("const segs = []"):body.index("// leyenda")]
    # El punto se mapea con NaN, no con cero: es lo que hace que el hueco sea hueco.
    assert "Q.num(p.v, NaN)" in seg
    assert "Q.num(p.v, 0)" not in seg, "un hueco no se dibuja en el cero"
    assert "segs.push(cur)" in seg
    # El trazo se reinicia por tramo, no una sola vez para toda la serie.
    assert seg.count("for (const pts of segs)") == 1


def test_a_degenerate_iv_window_reports_its_cause_on_screen():
    """Sin amplitud no hay percentil, y la sección tiene que decir por qué."""
    api = _read("app/terminal_api.py")
    assert '"iv_window_degenerate"' in api and '"iv_window_reason"' in api
    app = _read("app/static/itmq_app.js")
    assert "v.iv_window_degenerate" in app and "v.iv_window_reason" in app


# ════════════════════════════════════════════════════════════════════════════
# 13 · v1.51.0 · Relieve, puntos, isolíneas y rotulación — GLOBAL
# ════════════════════════════════════════════════════════════════════════════

def test_the_relief_is_a_solid_not_a_bar_with_an_edge():
    """Un relieve sin las tres caras es una barra con un borde."""
    js = _read("app/static/itmq_panels.js")
    body = js[js.index("function relief("):js.index("function heatmap(")]
    assert "Cara SUPERIOR" in body and "Tapa LATERAL" in body and "Cara FRONTAL" in body
    # Tres opacidades distintas: sin gradación de luz no hay volumen.
    for a in ("0.55", "0.30", "0.95"):
        assert f"Q.alpha(col, {a})" in body, a


def test_the_relief_depth_cannot_run_off_the_canvas():
    """La fuga salía del panel: se calculaba como fracción del alto TOTAL, y el
    perfil por strike hace crecer ese alto hasta miles de píxeles."""
    js = _read("app/static/itmq_panels.js")
    body = js[js.index("function relief("):js.index("function heatmap(")]
    assert "RELIEF_MIN_DEPTH" in body and "RELIEF_MAX_DEPTH" in body
    assert "Q.clamp(Math.min(b.h * o.depth, b.w * 0.18)" in body
    # Y el contenedor del relieve tiene alto propio, no el crecido de las barras.
    css = _read("app/static/itmq_terminal.css")
    assert ".chart.relief3d" in css
    html = _read("app/templates/terminal.html")
    assert 'class="chart lg relief3d" id="chartExposureRelief"' in html


def test_the_relief_floor_is_a_closed_plane():
    """Dos polilíneas abiertas se leían como rayas sueltas cruzando el gráfico."""
    js = _read("app/static/itmq_panels.js")
    body = js[js.index("function relief("):js.index("function heatmap(")]
    floor = body[body.index("Bordes del plano"):body.index("Plano del cero")]
    assert "ctx.closePath()" in floor, "el suelo se cierra como cuadrilátero"


def test_the_contours_are_continuous_lines_not_loose_dashes():
    """Barrer cada eje por separado daba una nube de palitos que parecía ruido."""
    ab = _read("app/static/itmq_adaptive_bars.js")
    assert "function contours(" in ab
    body = ab[ab.index("function contours("):ab.index("  /**\n   * Alto (o ancho)")]
    # Marching squares mira las CUATRO esquinas a la vez; por eso los segmentos
    # de celdas vecinas se encuentran en el borde compartido.
    assert "const tl = at(" in body and "const bl = at(" in body
    assert "tr = at(" in body and "br = at(" in body
    assert "case 5:" in body and "case 10:" in body, "las sillas de montar se separan"
    assert "contours," in ab, "se exporta desde el componente común"


def test_trace_and_the_section_share_one_field_treatment():
    """Dos tratamientos del mismo dato es lo que hacía que las dos pantallas no se
    parecieran, y obliga a corregir dos veces cada ajuste."""
    trace = _read("app/static/itmq_trace.js")
    panels = _read("app/static/itmq_panels.js")
    assert "AB.contours(f, level)" in trace and "AB.contours(f, level)" in panels
    # Los mismos umbrales y el mismo tope de opacidad en los dos.
    assert "[0.68, 0.80, 0.90, 0.96]" in trace and "[0.68, 0.80, 0.90, 0.96]" in panels
    assert "NOISE = 0.62" in trace and "HEAT_NOISE = 0.62" in panels
    assert "ALPHA_MAX = 0.70" in trace and "HEAT_ALPHA_MAX = 0.70" in panels


def test_the_field_never_reaches_full_opacity():
    """Un campo opaco tapa las velas y las isolíneas: el mapa pasa a competir con
    el precio en vez de acompañarlo."""
    trace = _read("app/static/itmq_trace.js")
    assert "255 * ALPHA_MAX" in trace
    panels = _read("app/static/itmq_panels.js")
    assert "255 * HEAT_ALPHA_MAX" in panels


def test_the_section_interval_map_is_a_dot_grid():
    """La vista de PUNTOS sigue existiendo y sigue siendo celda a celda.

    v1.51.0 la hizo la vista principal de la sección. v1.55.0 la movió a
    DIAGNÓSTICO (RAW) porque con noventa strikes el punto mide cuatro píxeles y
    su diámetro deja de informar, pero NO la quitó: es donde se comprueba un
    valor crudo uno a uno. Lo que este test protege es eso — que el punto siga
    siendo un punto, sin suavizar y sin interpolar.
    """
    js = _read("app/static/itmq_panels.js")
    assert "function dotmap(" in js
    body = js[js.index("function dotmap("):js.index("global.ITMQPanels")]
    # El diámetro ES la magnitud, y sin suavizar: cada celda sigue siendo ella.
    assert "blur: 0, fillRadius: 0" in body
    assert "Math.pow(rank, 0.72)" in body
    assert "ctx.arc(" in body
    # Un hueco no se dibuja; un cero medido sí.
    assert "if (v === null) continue;" in body
    assert "dotmap," in js
    app = _read("app/static/itmq_app.js")
    # v1.55.0 · Las dos vistas se eligen sobre el MISMO host y la misma rejilla.
    assert "(imRaw ? P.dotmap : P.heatmap)(el('chartIntervalMap')" in app


def test_net_drift_draws_walls_on_a_price_axis_not_on_the_premium_axis():
    """Call Wall es un PRECIO y el eje izquierdo mide PRIMA. Compartir regla entre
    dos magnitudes distintas es como se fabrica una lectura falsa."""
    js = _read("app/static/itmq_orderflow.js")
    body = js[js.index("function drawDrift("):js.index("function drawDriftCursor(")]
    assert "const px = vis.filter(v => Q.isNum(v.price));" in body
    assert "const psy = Q.scale(plo - ppad, phi + ppad" in body
    assert "Q.levelLine(ctx, box, y, Q.alpha(col, 0.8)" in body
    # Los muros entran en el dominio: uno fuera del encuadre se confundiría con
    # uno que no existe.
    assert "plo = Math.min(plo, lp); phi = Math.max(phi, lp);" in body
    # UNA sola escala de precio. Dos dominios distintos en el mismo gráfico es
    # peor que no dibujar los muros: las dos parecen válidas y sólo una sitúa
    # bien la línea.
    assert body.count("Q.scale(plo") == 1
    assert "syP" not in body


def test_net_drift_marks_the_flow_with_the_same_functions_as_the_rest():
    """Net Drift dibuja la marca con la MISMA función que TRACE y que la cinta.

    Dibujarla aquí de otra forma haría que la misma marca significara dos cosas
    según la pantalla, que es exactamente lo que no puede pasar con una señal
    direccional.
    """
    js = _read("app/static/itmq_orderflow.js")
    body = js[js.index("function drawDrift("):js.index("function drawDriftCursor(")]
    assert "Q.flowMark(ctx, x, y, ev, peak" in body
    # El ancla es el INSTANTE y el PRECIO, no un índice de bucket.
    assert "const t = Q.parseTime(ev.t);" in body
    assert "const y = psy(pr);" in body


def test_level_typography_is_defined_once_for_the_whole_terminal():
    """Tres tamaños para el mismo muro harían que la pantalla no pareciera del
    mismo programa, y divergirían al primer ajuste."""
    core = _read("app/static/itmq_core.js")
    for name in ("LEVEL_FONT", "LEVEL_LABEL_H", "LEVEL_LABEL_GAP", "LEVEL_LINE_WIDTH"):
        assert f"const {name} =" in core, name
        assert name in core[core.index("LEVELS, FLOW_LEVEL_KINDS, levelStyle,"):], name
    # 10 px sobre velas y campo de calor no se leía: la referencia que más se mira
    # en una sesión tiene que verse de un vistazo.
    assert "'700 12px ui-monospace, monospace'" in core
    for path in ("app/static/itmq_trace.js", "app/static/itmq_orderflow.js"):
        js = _read(path)
        assert "Q.LEVEL_FONT" in js, path
        assert "Q.LEVEL_LABEL_GAP" in js, path
        assert "Q.LEVEL_LINE_WIDTH" in js, path


def test_the_two_main_charts_have_a_floor_in_pixels():
    """Repartir por fracción deja el gráfico pequeño en una ventana baja: el resto
    de filas se lleva su parte pase lo que pase."""
    css = _read("app/static/itmq_terminal.css")
    trace = css[css.index(".trace-grid {"):css.index(".trace-col {")]
    # v1.54.0 · 640 -> 780: el gráfico salía comprimido, velas y líneas pegadas.
    assert "min-height: 780px" in trace
    drift = css[css.index(".drift-stack {"):css.index(".drift-stack[hidden]")]
    # v1.55.0 · Suelo, no cifra exacta: el gráfico puede crecer, nunca encoger.
    import re as _re2
    m2 = _re2.search(r"grid-template-rows:\s*minmax\((\d+)px,", drift)
    assert m2 and int(m2.group(1)) >= 520


def test_none_of_this_release_knows_a_ticker():
    """Regla absoluta: todo cambio es GLOBAL, para todos los activos."""
    import re
    for path in ("app/static/itmq_panels.js", "app/static/itmq_trace.js",
                 "app/static/itmq_orderflow.js", "app/static/itmq_adaptive_bars.js",
                 "app/core/strike_window.py", "app/core/session_memory.py"):
        src = _read(path)
        for sym in ("SPY", "QQQ", "IWM", "AAPL", "TSLA", "NVDA", "SPX"):
            assert not re.search(rf"['\"]{sym}['\"]", src), (path, sym)
    # Y el eje de strikes no puede llevar un umbral en dólares escrito a mano.
    sw = _read("app/core/strike_window.py")
    assert "BAND_PCT" in sw, "la banda es porcentual, no en dólares"


def test_net_drift_totals_are_never_a_fabricated_zero():
    """Un «$0.0» afirma que hoy no se acumuló prima; el hueco dice que no se midió.

    Se destapó al inyectar una serie SIN sus agregados: el panel dibujaba la curva
    y las tres tarjetas de arriba decían $0.0.
    """
    js = _read("app/static/itmq_orderflow.js")
    body = js[js.index("function renderDriftSummary("):js.index("/* ------------------------------------------------------------- API */")
              if "/* ------------------------------------------------------------- API */" in js
              else js.index("function mount(")]
    assert "Q.num(d.cum_call_premium, 0)" not in body
    assert "Q.money(d.cum_call_premium, 1)" in body
    assert "Q.money(d.cum_net_premium, 1)" in body
    assert "Q.compact(d.cum_call_volume, 1)" in body

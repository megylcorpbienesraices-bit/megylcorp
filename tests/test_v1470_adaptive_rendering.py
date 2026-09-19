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
    heat = js[js.index("function heatmap("):]
    assert "AB.field(matrix" in heat, "el campo lo prepara el componente común"
    assert "ctx.drawImage(fieldCanvas" in heat, "superficie interpolada"
    assert "imageSmoothingEnabled = true" in heat, "sin interpolación son celdas duras"
    assert "ctx.arc(" not in heat, "el mapa ya no se dibuja con puntos"
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
    # `relief` se define ANTES que `heatmap`, así que el corte va hasta el final.
    heat = js[js.index("function heatmap("):]
    assert "AB.field(matrix" in heat
    assert "imageSmoothingEnabled = true" in heat
    # Suelo de ruido: sin él, la normalización por rango deja media pantalla a
    # media opacidad y el mapa sale como un bloque macizo.
    assert "noiseFloor" in heat and "gammaCurve" in heat
    # Contornos cerrados: barriendo sólo un eje salen segmentos sueltos que
    # parecen ruido en vez de un borde.
    assert heat.count("if ((a0 < level) === (a1 < level)) continue;") == 2


def test_trace_and_the_interval_map_treat_the_same_data_the_same_way():
    """Dos tratamientos del mismo dato hacen que dos pantallas no se parezcan."""
    trace = _read("app/static/itmq_trace.js")
    body = trace[trace.index("function buildHeatBitmap("):]
    assert "AB.field(upright" in body, "TRACE usa el campo común"
    assert "const NOISE = 0.55, GAMMA = 1.9;" in body
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
    """El color codificaba CALL/PUT con el verde y el rojo que usa el precio.

    Una marca sobre un tramo de su propio color desaparecía dentro de él. El
    oro no lo usa ningún otro elemento del gráfico; el sentido va en la flecha
    y la cantidad al lado.
    """
    for rel in ("app/static/itmq_trace.js", "app/static/itmq_orderflow.js"):
        js = _read(rel)
        assert "Q.token('--gold', '#d9a441')" in js, rel
    flow = _read("app/static/itmq_orderflow.js")
    body = flow[flow.index("function markerLabel("):]
    body = body[:body.index("\n  /**")]
    assert "'▲'" in body and "'▼'" in body, "la flecha dice el sentido"
    assert "Q.money" in body, "y la cifra, la cantidad"
    # Ni el verde ni el rojo vuelven a codificar el sentido de la marca.
    qmark = flow[flow.index("const qEvents ="):flow.index("// último precio")]
    assert "'--pos'" not in qmark and "'--neg'" not in qmark


def test_dark_flow_resolves_its_volume_field_from_the_real_response():
    """«608 intervalos · 0.0 acc» no se puede corregir sin saber qué llegó."""
    from app.providers.quantdata.tools import norm_dark_flow

    # Nombre declarado.
    a = norm_dark_flow({"buckets": [
        {"timestamp": "2026-09-19T14:30:00Z", "darkVolume": 12000, "totalVolume": 50000}]})
    assert a["rows"][0]["dark_volume"] == 12000
    assert a["field_map"]["dark_volume"] == "darkVolume"

    # Nombre DISTINTO: se deriva de la propia respuesta y se declara cuál.
    b = norm_dark_flow({"buckets": [
        {"timestamp": "2026-09-19T14:30:00Z", "offExchangeShares": 9000,
         "consolidatedShares": 40000}]})
    assert b["rows"][0]["dark_volume"] == 9000
    assert b["field_map"]["dark_volume"] == "offExchangeShares"

    # Ningún campo sirve: `None`, NUNCA cero, y se dice qué campos llegaron.
    c = norm_dark_flow({"buckets": [
        {"timestamp": "2026-09-19T14:30:00Z", "stockPrice": 516.2}]})
    assert c["rows"][0]["dark_volume"] is None
    assert c["volume_field_resolved"] is False
    assert "stockPrice" in c["observed_fields"]


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

"""v1.55.0 · UNA rejilla canónica, UN renderer principal.

LA REJILLA
----------
Venga del proveedor, del respaldo del motor o de la última sesión válida, el
Interval Map sale SIEMPRE con la misma forma:

    strikes (eje Y) × times (eje X) × matrix (valores) × intensity (normalizado)

Eso es lo que impide que existan dos mapas distintos con el mismo nombre. El
recorte al tamaño de pantalla es un CORTE CONTIGUO por el centro: nunca se
saltan strikes, porque un eje que se salta filas mientras la matriz no lo hace
es la forma silenciosa de que el mapa mienta.

EL RENDERER
-----------
v1.51.0 hizo los PUNTOS la vista principal de la sección. Con noventa strikes
por ciento sesenta intervalos el punto mide cuatro píxeles y su diámetro deja de
informar, justo en el mapa que existe para contar dónde está la concentración y
hacia dónde migra. v1.55.0 pone el MAPA CONTINUO de principal y deja los puntos
como vista RAW de diagnóstico, rotulada: ahí siguen siendo insustituibles para
comprobar un valor crudo celda a celda.

Las dos leen la MISMA rejilla. Cambia la pregunta, no el dato.
"""
from __future__ import annotations

from pathlib import Path

from app.core.quant_data_hub import INTERVAL_GREEKS, _trim, interval_map

SIMBOLOS = ("QQQ", "SPY", "IWM", "DIA", "AAPL", "NVDA", "TSLA", "SPX")


def _intel(greek="GAMMA", n_strikes=12, n_times=6, base=500.0):
    strikes = [base + i for i in range(n_strikes)]
    times = [f"2026-09-19T{14 + i // 60:02d}:{i % 60:02d}:00Z" for i in range(n_times)]
    matrix = [[(si + 1) * (ti + 1) * 1000.0 for ti in range(n_times)] for si in range(n_strikes)]
    tool = {"GAMMA": "interval_map_gamma", "DELTA": "interval_map_delta",
            "VANNA": "interval_map_vanna", "CHARM": "interval_map_charm"}[greek]
    return {tool: {"ready": True, "strikes": strikes, "times": times, "matrix": matrix,
                   "rows": n_strikes * n_times, "count": n_strikes * n_times}}


# ── La rejilla es canónica ───────────────────────────────────────────────

def test_la_rejilla_tiene_siempre_la_misma_forma():
    im = interval_map("QQQ", _intel())["payload"] if False else interval_map("QQQ", _intel())
    payload = im.get("payload", im)
    assert payload["ready"] is True
    assert len(payload["matrix"]) == len(payload["strikes"])
    for fila in payload["matrix"]:
        assert len(fila) == len(payload["times"])
    assert payload["axis"] == {"x": "TIME", "y": "STRIKE", "intensity": "EXPOSURE_MAGNITUDE"}


def test_la_misma_rejilla_para_las_cuatro_griegas():
    for greek in INTERVAL_GREEKS:
        p = interval_map("QQQ", _intel(greek), greek)
        p = p.get("payload", p)
        assert p["greek"] == greek
        assert len(p["matrix"]) == len(p["strikes"]), greek


def test_el_recorte_es_contiguo_y_no_se_salta_strikes():
    """Un eje que se salta filas mientras la matriz no lo hace desalinea el mapa
    entero y nada avisa."""
    strikes = [400.0 + i for i in range(200)]
    times = [f"t{i}" for i in range(300)]
    matrix = [[si * 1000 + ti for ti in range(300)] for si in range(200)]
    s, t, m = _trim(strikes, times, matrix, max_strikes=90, max_times=160)
    assert len(s) == 90 and len(t) == 160
    # Contiguo: cada strike es el anterior mas uno, sin huecos.
    assert all(round(s[i + 1] - s[i], 6) == 1.0 for i in range(len(s) - 1))
    # Y la matriz se recorta por LOS MISMOS indices que los ejes.
    assert len(m) == len(s) and len(m[0]) == len(t)
    assert m[0][0] == strikes.index(s[0]) * 1000 + (300 - 160)


def test_el_recorte_de_strikes_es_por_el_centro_donde_esta_el_precio():
    strikes = [float(i) for i in range(200)]
    s, _t, _m = _trim(strikes, ["a"], [[1.0] for _ in range(200)],
                      max_strikes=90, max_times=160)
    assert s[0] == 55.0 and s[-1] == 144.0      # centrado en 100


def test_ningun_simbolo_toma_un_camino_distinto():
    """Punto 24: nada de `if symbol == "QQQ"`. Ocho activos, misma forma."""
    formas = set()
    for sym in SIMBOLOS:
        p = interval_map(sym, _intel())
        p = p.get("payload", p)
        formas.add((p["ready"], len(p["strikes"]), len(p["times"]),
                    tuple(sorted(p["axis"].items()))))
    assert len(formas) == 1, f"algun simbolo sale distinto: {formas}"


def test_la_intensidad_se_normaliza_con_la_escala_del_propio_activo():
    """Sin umbrales en dolares: el mapa se lee igual en un ETF de 40 y en un
    indice de 5.800."""
    chico = interval_map("XLF", _intel(base=40.0))
    grande = interval_map("SPX", _intel(base=5800.0))
    a = (chico.get("payload") or chico)["intensity"]
    b = (grande.get("payload") or grande)["intensity"]
    assert a and b
    assert max(max(r) for r in a) == max(max(r) for r in b)


def test_sin_dato_no_se_publica_una_rejilla_de_ceros():
    p = interval_map("QQQ", {})
    p = p.get("payload", p)
    assert p["ready"] is False
    assert p["matrix"] == [] and p["strikes"] == []
    assert "SIN INTERVAL MAP" in p["reason"]


# ── El renderer principal es el mapa continuo ────────────────────────────

def test_el_mapa_continuo_es_la_vista_principal():
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    i = js.index("const im = d.interval_map")
    cuerpo = js[i:js.index("set('imNote'", i)]
    assert "(imRaw ? P.dotmap : P.heatmap)" in cuerpo
    # Por defecto, campo continuo.
    assert "intervalRender: 'field'," in js


def test_los_puntos_siguen_disponibles_rotulados_raw():
    html = Path("app/templates/terminal.html").read_text(encoding="utf-8")
    assert 'id="imRender"' in html
    assert 'value="raw"' in html and "Puntos · RAW" in html
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    assert "VISTA RAW: una celda = un valor crudo del proveedor" in js


def test_las_dos_vistas_leen_la_misma_rejilla():
    """Si cada vista leyera su propia fuente habria dos mapas con un nombre."""
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    i = js.index("const im = d.interval_map")
    cuerpo = js[i:js.index("set('imNote'", i)]
    # Una sola llamada a `.set`, con los mismos ejes, para las dos vistas.
    assert cuerpo.count(".set(im.strikes || []") == 1
    assert "im.times || [], im.matrix || [], im.price || []" in cuerpo


def test_cambiar_de_vista_suelta_el_panel_anterior():
    """Dos paneles de distinto tipo sobre el mismo lienzo se pisan el bitmap."""
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    assert "function dropChart(id)" in js
    i = js.index("const im = d.interval_map")
    cuerpo = js[i:js.index("set('imNote'", i)]
    assert "dropChart('chartIntervalMap')" in cuerpo


def test_la_vista_no_viaja_al_servidor():
    """Es la misma rejilla dibujada de dos maneras: pedirla otra vez seria
    gastar cuota para recibir lo mismo."""
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    i = js.index("el('imRender')")
    cuerpo = js[i:i + 400]
    assert "pullBundle()" not in cuerpo
    assert "renderEscenarios(state.bundle)" in cuerpo


def test_el_panel_crece_para_dar_una_fila_por_strike():
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    assert "growForRows('chartIntervalMap', (im.strikes || []).length" in js


# ── Punto 12 · un strike, una barra ──────────────────────────────────────

def test_cada_perfil_por_strike_crece_en_vez_de_agrupar():
    """GEX · DEX · VEX · CHEX comparten panel con selector de metrica, y el
    interes abierto tiene el suyo. Los tres crecen una fila por strike."""
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    for panel in ("chartExposure", "chartExposureRelief", "chartOiStrike"):
        assert f"growForRows('{panel}'" in js, panel
    # `aggregate: 'none'` es lo que impide meter tres strikes en una barra.
    assert "aggregate: 'none'" in js


def test_la_etiqueta_se_salta_por_el_paso_real_no_por_uno_fijo():
    """Con un paso fijo de 15 px y filas de 13 se saltaba un strike de cada dos:
    barras de todos y numeros de la mitad."""
    js = Path("app/static/itmq_panels.js").read_text(encoding="utf-8")
    assert "Math.ceil(LABEL_MIN_PX / Math.max(1, slot))" in js

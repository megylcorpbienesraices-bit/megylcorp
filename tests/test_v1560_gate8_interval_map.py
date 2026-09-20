"""GATE 8 · Interval Map: una cadena, un renderer, el signo intacto.

Puntos 17-20 y 50-54.

POR QUÉ NO BASTA COMPARAR CAPTURAS
----------------------------------
«El mapa de QQQ sale casi todo rojo» tiene dos causas opuestas:

    el proveedor devuelve exposición negativa   ->  el rojo es CORRECTO
    la agregación o el renderizador invierten   ->  es un BUG

Mirar la pantalla al lado de la web del proveedor no las distingue: las dos
producen una imagen roja. Lo que las distingue son los NÚMEROS, celda a celda.

Primero se demuestra qué manda Quant Data. Después, si hace falta, se toca la
paleta — y no antes.

QUÉ PUEDE CAMBIAR Y QUÉ NO
--------------------------
La normalización por rango cambia la MAGNITUD a propósito: sustituye cada celda
por el percentil que ocupa, y eso es lo que hace el mapa legible en cualquier
activo. Lo que no puede cambiar nunca es el SIGNO, el ORDEN, ni convertir un
hueco en una celda medida.
"""
from __future__ import annotations

from pathlib import Path

from app.core.asset_normalization import normalize_matrix
from app.core.interval_map_audit import audit_grid, compare_rows, sample_cells
from app.core.quant_data_hub import INTERVAL_GREEKS, interval_map

SIMBOLOS = ("DIA", "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "AMD")
APP = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
PANELS = Path("app/static/itmq_panels.js").read_text(encoding="utf-8")
TRACE = Path("app/static/itmq_trace.js").read_text(encoding="utf-8")
BARS = Path("app/static/itmq_adaptive_bars.js").read_text(encoding="utf-8")


def _intel(greek="GAMMA", n=12, m=8, negativo=False, huecos=True):
    strikes = [500.0 + i for i in range(n)]
    times = [f"2026-09-19T14:{i:02d}:00Z" for i in range(m)]
    signo = -1 if negativo else 1
    mat = [[signo * (si + 1) * (ti + 1) * 1e5 * (1 if (si + ti) % 3 else -1)
            for ti in range(m)] for si in range(n)]
    if huecos:
        mat[2][3] = None
        mat[7][1] = None
    tool = {"GAMMA": "interval_map_gamma", "DELTA": "interval_map_delta",
            "VANNA": "interval_map_vanna", "CHARM": "interval_map_charm"}[greek]
    return {tool: {"ready": True, "strikes": strikes, "times": times, "matrix": mat}}


def _rejilla(symbol="QQQ", **kw):
    p = interval_map(symbol, _intel(**kw))
    return p.get("payload", p)


# ── 52 · El hueco sobrevive a la normalización ───────────────────────────

def test_una_celda_ausente_no_se_convierte_en_cero():
    """Ese cero viajaba como si fuera una medición: entraba en el percentil,
    contaba como «celda observada sin exposición» y llegaba al renderizador
    indistinguible de un cero real."""
    r = normalize_matrix([[1e6, None], [None, -2e6]], symbol="QQQ")
    assert r["matrix"][0][1] is None
    assert r["matrix"][1][0] is None
    assert r["missing_cells"] == 2
    assert r["observed_cells"] == 2


def test_un_cero_medido_sigue_siendo_cero():
    """La corrección no puede convertir una medición en un hueco."""
    r = normalize_matrix([[1e6, 0.0], [None, -2e6]], symbol="QQQ")
    assert r["matrix"][0][1] == 0.0
    assert r["matrix"][1][0] is None


def test_una_matriz_toda_a_cero_conserva_sus_huecos():
    r = normalize_matrix([[0.0, None], [0.0, 0.0]], symbol="QQQ")
    assert r["normalization"] == "ALL_ZERO_OBSERVED"
    assert r["matrix"][0][1] is None
    assert r["matrix"][0][0] == 0.0


def test_el_renderizador_distingue_hueco_de_cero():
    """`ITMQBars.field` sabe tratar un hueco —lo rellena desde sus vecinas— pero
    sólo si le llega como hueco."""
    cuerpo = BARS[BARS.index("function field(matrix, opts)"):]
    cuerpo = cuerpo[:cuerpo.index("// ── 2 ·")]
    assert "Un 0 explícito es una medición" in cuerpo
    assert "v !== null && v !== undefined && Number.isFinite(n)" in cuerpo


def test_la_rejilla_declara_cuantas_celdas_no_publico_el_proveedor():
    r = normalize_matrix([[1e6, None, 2e6], [None, 3e6, None]], symbol="QQQ")
    assert r["missing_cells"] == 3


# ── 52-53 · El signo no se invierte ──────────────────────────────────────

def test_el_signo_se_conserva_en_toda_la_rejilla():
    a = audit_grid(_rejilla(), limit=40)
    assert a["ok"] is True, a["detail"]
    assert a["sign_flip_count"] == 0


def test_una_rejilla_negativa_sale_negativa():
    """Si Quant Data devuelve exposición negativa, el rojo es correcto y no hay
    nada que arreglar en la paleta."""
    a = audit_grid(_rejilla(negativo=True), limit=40)
    assert a["ok"] is True
    negativas = [c for c in a["cells"] if c["raw_sign"] == -1]
    assert negativas
    assert all(c["canonical_sign"] in (-1, 0) for c in negativas)
    assert all(c["expected_color"] == "rojo" for c in negativas)


def test_atenuar_por_el_suelo_de_ruido_no_es_invertir():
    a = audit_grid(_rejilla(), limit=40)
    assert a["noise_floor_muted"] > 0
    assert a["sign_flip_count"] == 0


def test_una_inversion_inyectada_hace_FALLAR_la_auditoria():
    """Es la prueba de que la auditoría sirve para algo."""
    p = _rejilla()
    # Una celda con intensidad REAL: las de magnitud pequeña quedan atenuadas
    # por el suelo de ruido y ahí no habría signo que invertir.
    y, x = next((y, x) for y, fila in enumerate(p["intensity"])
                for x, v in enumerate(fila)
                if v not in (None, 0) and p["matrix"][y][x] not in (None, 0))
    p["intensity"][y][x] = -p["intensity"][y][x]
    a = audit_grid(p, limit=200)
    assert a["ok"] is False
    assert a["sign_flip_count"] >= 1


def test_un_hueco_publicado_como_medido_hace_FALLAR_la_auditoria():
    p = _rejilla()
    y, x = 2, 3
    assert p["matrix"][y][x] is None
    p["intensity"][y][x] = 0.5          # el defecto, inyectado
    a = audit_grid(p, limit=120)
    assert a["ok"] is False
    assert a["missing_became_measured"]


# ── 18 y 53 · Los ocho activos, celda a celda ────────────────────────────

def test_los_ocho_activos_conservan_el_signo():
    for sym in SIMBOLOS:
        a = audit_grid(_rejilla(symbol=sym, negativo=(sym == "QQQ")), limit=40)
        assert a["ok"] is True, f"{sym}: {a['detail']}"
        assert a["checked"] >= 20, sym


def test_las_cuatro_griegas_pasan_la_misma_auditoria():
    for greek in INTERVAL_GREEKS:
        p = interval_map("QQQ", _intel(greek), greek)
        a = audit_grid(p.get("payload", p), limit=30)
        assert a["ok"] is True, f"{greek}: {a['detail']}"


def test_la_muestra_se_reparte_por_la_rejilla():
    """Las primeras filas suelen ser los strikes lejanos, donde casi todo es
    cero, y una muestra de ceros no demuestra nada sobre el signo."""
    p = _rejilla()
    celdas = sample_cells(p["strikes"], p["times"], p["matrix"], p["intensity"], limit=20)
    filas = {c["row"] for c in celdas}
    assert len(filas) > 1


# ── 54 · Contra el crudo del proveedor ───────────────────────────────────

def test_cada_celda_coincide_con_el_crudo_del_proveedor():
    p = _rejilla()
    filas = []
    for y, k in enumerate(p["strikes"][:6]):
        for x, t in enumerate(p["times"][:4]):
            v = p["matrix"][y][x]
            if v is None:
                continue
            # Se reparte entre call y put; la suma tiene que volver a salir.
            filas.append({"strike": k, "time": t,
                          "call_exposure": v * 0.6, "put_exposure": v * 0.4})
    r = compare_rows(filas, p, limit=50)
    assert r["ok"] is True, r["detail"]
    assert r["checked"] >= 10


def test_la_reduccion_se_declara_y_es_la_misma_para_todos():
    p = _rejilla()
    r = compare_rows([{"strike": p["strikes"][0], "time": p["times"][0],
                       "call_exposure": p["matrix"][0][0], "put_exposure": 0.0}], p)
    assert "call_exposure + put_exposure" in r["reduction"]
    assert "MISMA reducción para todos los activos" in r["reduction"]


def test_un_desajuste_contra_el_crudo_se_denuncia():
    p = _rejilla()
    r = compare_rows([{"strike": p["strikes"][0], "time": p["times"][0],
                       "call_exposure": 999.0, "put_exposure": 0.0}], p)
    assert r["ok"] is False and r["mismatch_count"] == 1


# ── 19, 50, 51 · Un renderer común, campo continuo ───────────────────────

def test_trace_y_escenarios_leen_la_misma_rejilla_canonica():
    assert "d.interval_maps" in TRACE       # TRACE lee las cuatro griegas
    assert "d.interval_map" in APP          # la sección lee la misma estructura


def test_los_dos_construyen_el_campo_con_la_misma_funcion():
    """Huecos rellenados desde sus vecinas, suavizado gaussiano y normalización
    por rango con signo: una sola implementación."""
    assert "AB.field(" in TRACE
    assert "AB.field(" in PANELS
    assert "function field(matrix, opts)" in BARS


def test_la_vista_principal_de_la_seccion_es_el_campo_continuo():
    i = APP.index("const im = d.interval_map")
    cuerpo = APP[i:APP.index("set('imNote'", i)]
    assert "(imRaw ? P.dotmap : P.heatmap)" in cuerpo
    assert "intervalRender: 'field'," in APP


def test_los_puntos_quedan_como_vista_raw_de_diagnostico():
    html = Path("app/templates/terminal.html").read_text(encoding="utf-8")
    assert "Puntos · RAW" in html
    assert "VISTA RAW: una celda = un valor crudo del proveedor" in APP


def test_el_campo_continuo_lleva_isolineas():
    assert "contours(" in BARS or "contours(" in PANELS


# ── 20 · Normalización sobre lo visible ──────────────────────────────────

def test_el_trace_normaliza_sobre_la_ventana_visible():
    """No contra noventa strikes cuando sólo se ven trece: el campo entero
    aplastaba el contraste justo en la zona que se está mirando."""
    assert "HEAT_MARGIN" in TRACE
    cuerpo = TRACE[TRACE.index("HEAT_MARGIN"):]
    assert "0.35" in cuerpo[:400]


def test_el_recorte_visual_no_altera_la_matriz_cruda():
    """Es una transformación de presentación; el RAW se conserva intacto."""
    p = _rejilla()
    assert len(p["matrix"]) == len(p["strikes"])
    assert p["matrix"][2][3] is None       # el hueco original sigue ahí

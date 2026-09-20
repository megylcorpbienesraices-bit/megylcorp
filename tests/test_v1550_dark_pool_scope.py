"""v1.55.0 · Dark Pool: muros, identidad de ciclo, ventana y nombres.

CUATRO DEFECTOS DE LECTURA, NO DE DATO
--------------------------------------
El dato ya llegaba bien desde v1.52.0. Lo que seguía mal era lo que la pantalla
permitía concluir con él:

15 · Las concentraciones eran una TABLA. Un nivel de dark pool es un precio con
     tamaño detrás y se lee contra el precio, igual que un muro.

16 · No había forma de distinguir «esto es lo de hace diez minutos» de «esto
     acaba de llegar y resulta que es idéntico». Una sección congelada por un
     fallo de refresco se ve igual que un mercado sin actividad nueva.

17 · Los tres carriles NO cubren la misma ventana: `dark-flow` llega por
     intervalos de toda la sesión y `equity-prints` como una cola reciente.

18 · «NOTIONAL OFF-EXCHANGE» al lado de «PRINT MAYOR» invita a dividir uno
     entre otro, y esa división no significa nada.
"""
from __future__ import annotations

from app.core import level_identity as LI
from app.core.dark_pool_view import DARK_WALL_KIND, build

FLOW = {"ready": True, "rows": [
    {"t": "2026-09-19T14:00:00Z", "dark_notional": 1e6, "dark_volume": 2000,
     "dark_prints": 5, "stock_price": 500.0},
    {"t": "2026-09-19T14:01:00Z", "dark_notional": 2e6, "dark_volume": 4000,
     "dark_prints": 7, "stock_price": 501.0},
]}
LEVELS = {"ready": True, "rows": [
    {"price": 505.0, "notional": 9e6, "shares": 18000, "prints": 40},
    {"price": 495.0, "notional": 7e6, "shares": 14000, "prints": 30},
    {"price": 508.0, "notional": 3e6, "shares": 6000, "prints": 12},
    {"price": 490.0, "notional": 1e6, "shares": 2000, "prints": 4},
]}
PRINTS = {"ready": True, "rows": [
    {"t": "2026-09-19T15:30:00Z", "price": 500.5, "size": 5000, "notional": 2.5e6,
     "off_exchange": True, "venue": "D"},
    {"t": "2026-09-19T15:31:00Z", "price": 500.7, "size": 1000, "notional": 5e5,
     "off_exchange": False},
]}


def _build(**kw):
    a = dict(flow_block=FLOW, levels_block=LEVELS, prints_block=PRINTS,
             spot=500.0, symbol="QQQ", session={"resolved": "2026-09-19"})
    a.update(kw)
    return build(**a)


# ── 15 · Muros de dark pool ──────────────────────────────────────────────

def test_las_concentraciones_salen_como_lineas_dibujables():
    walls = _build()["walls"]
    assert walls
    for w in walls:
        assert w["kind"] == DARK_WALL_KIND
        assert w["authority"] == "QUANTDATA_DARK_POOL_LEVELS"
        assert w["source_mode"] == "DIRECT_PROVIDER"
        assert w["price"] is not None and w["notional"] is not None


def test_hay_muros_a_los_dos_lados_del_precio():
    """Enseñar solo los mayores deja un lado sin representar cuando todo el peso
    esta arriba, que es justo cuando hace falta ver que debajo no hay nada."""
    walls = _build()["walls"]
    lados = {w["side"] for w in walls}
    assert lados == {"ABOVE_SPOT", "BELOW_SPOT"}


def test_la_fuerza_es_relativa_al_propio_activo():
    """Sin umbrales en dolares: el mayor del dia es 100 y el resto se mide
    contra el, igual en un ETF de 40 que en un indice de 5.800."""
    walls = _build()["walls"]
    assert walls[0]["strength"] == 100.0
    assert all(0 < w["strength"] <= 100 for w in walls)


def test_sin_precio_de_referencia_no_se_inventa_un_lado():
    walls = _build(spot=None)["walls"]
    assert walls
    assert all(w["side"] is None for w in walls)


def test_un_nivel_sin_nocional_no_se_convierte_en_muro():
    lv = {"ready": True, "rows": [{"price": 505.0}, {"price": 495.0, "notional": 1e6}]}
    walls = _build(levels_block=lv)["walls"]
    assert [w["price"] for w in walls] == [495.0]


def test_el_muro_de_dark_pool_tiene_identidad_propia_en_el_registro():
    """Y NO es un muro de opciones: miden cosas distintas."""
    fila = LI.audit(_build()["walls"], symbol="QQQ")["rows"][0]
    assert fila["identity_status"] == LI.IDENTIFIED
    assert fila["source"] == "QUANTDATA_DARK_POOL_LEVELS"
    origen = LI.LEVEL_ORIGIN[DARK_WALL_KIND]
    assert "off-exchange" in origen["method"]
    assert origen["function"] == "dark_pool_view._dark_walls"


# ── 16 · Identidad del ciclo ─────────────────────────────────────────────

def test_el_mismo_ciclo_produce_la_misma_huella():
    assert _build()["cycle_id"] == _build()["cycle_id"]


def test_cualquier_cambio_del_dato_cambia_la_huella():
    base = _build()["cycle_id"]
    flow2 = {"ready": True, "rows": FLOW["rows"] + [
        {"t": "2026-09-19T14:02:00Z", "dark_notional": 5e5, "dark_volume": 900,
         "dark_prints": 2, "stock_price": 501.5}]}
    assert _build(flow_block=flow2)["cycle_id"] != base
    assert _build(symbol="SPY")["cycle_id"] != base
    assert _build(session={"resolved": "2026-09-18"})["cycle_id"] != base


def test_el_modelo_dice_cuando_se_construyo():
    m = _build()
    assert m["built_at"].startswith("20")
    assert m["symbol"] == "QQQ"


# ── 17 · Alcance temporal ────────────────────────────────────────────────

def test_cada_carril_declara_la_ventana_que_REALMENTE_llego():
    sc = _build()["temporal_scope"]
    assert sc["dark_flow"]["first"] == "2026-09-19T14:00:00Z"
    assert sc["dark_flow"]["last"] == "2026-09-19T14:01:00Z"
    # Los prints son otra ventana, mas tardia y mas corta.
    assert sc["equity_prints"]["first"] == "2026-09-19T15:30:00Z"
    assert sc["dark_flow"]["last"] < sc["equity_prints"]["first"]


def test_los_niveles_declaran_que_no_tienen_eje_temporal():
    """Es una foto acumulada, no una serie: fingir una ventana seria peor que
    decir que no la tiene."""
    sc = _build()["temporal_scope"]["dark_pool_levels"]
    assert sc["first"] is None and sc["last"] is None
    assert "foto acumulada" in sc["note"]


def test_un_carril_vacio_declara_ventana_vacia_no_cero():
    sc = _build(prints_block={"ready": False, "rows": []})["temporal_scope"]
    assert sc["equity_prints"] == {"first": None, "last": None, "points": 0}


# ── 18 · Nombres de KPI ──────────────────────────────────────────────────

def test_cada_kpi_dice_que_mide_con_que_formula_y_de_que_carril():
    meta = _build()["kpi_meta"]
    for clave, m in meta.items():
        assert m["label"] and m["measures"] and m["formula"], clave
        assert m["lane"] in ("dark_flow", "dark_pool_levels", "equity_prints"), clave
        assert m["unit"], clave


def test_cada_kpi_lleva_la_ventana_de_su_propio_carril():
    m = _build()
    meta, sc = m["kpi_meta"], m["temporal_scope"]
    assert meta["dark_notional"]["window"] == sc["dark_flow"]
    assert meta["largest_print"]["window"] == sc["equity_prints"]
    assert meta["dark_vwap"]["window"] == sc["equity_prints"]


def test_el_kpi_que_invita_a_una_division_sin_sentido_lo_advierte():
    meta = _build()["kpi_meta"]
    assert "no se puede dividir" in meta["largest_print"]["caveat"]
    assert "no se estima" in meta["dark_share_pct"]["caveat"]


def test_todo_kpi_publicado_tiene_su_definicion():
    m = _build()
    sin_meta = [k for k in m["kpis"]
                if k in ("dark_notional", "dark_volume", "dark_print_count",
                         "dominant_level", "largest_print", "dark_vwap", "dark_share_pct")
                and k not in m["kpi_meta"]]
    assert not sin_meta, sin_meta


def test_la_cuota_fuera_de_bolsa_sin_universo_completo_es_hueco_no_cero():
    solo_oscuros = {"ready": True, "rows": [PRINTS["rows"][0]]}
    k = _build(prints_block=solo_oscuros)["kpis"]
    assert k["dark_share_pct"] is None
    assert "universo completo" in k["dark_share_reason"]

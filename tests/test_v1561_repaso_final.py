"""REPASO PUNTO POR PUNTO · los huecos que el cierre había dado por cerrados.

Este fichero existe porque la respuesta «sí, están todos» era falsa. Al revisar
los 55 puntos contra el código aparecieron nueve huecos. Los que se podían
cerrar aquí están cerrados, y cada uno tiene su prueba para que no vuelvan.

Un punto dado por hecho sin comprobarlo es peor que uno pendiente declarado: el
pendiente se ve en la lista y el otro no.
"""
from __future__ import annotations

from pathlib import Path

from app.core.dark_pool_view import build as dp_build
from app.core.level_identity import audit, describe
from app.terminal_api import build_terminal_bundle, wall_consistency

HTML = Path("app/templates/terminal.html").read_text(encoding="utf-8")
APP = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
HARNESS = Path("tools/visual_harness.html").read_text(encoding="utf-8")
VISUAL = Path("tools/visual_regression.py").read_text(encoding="utf-8")


# ── PUNTO 16 · `level_id` y `name` faltaban ──────────────────────────────

CAMPOS_16 = ("level_id", "name", "type", "price", "source", "method",
             "timestamp", "persistence", "magnitude")


def test_cada_linea_lleva_los_nueve_campos_pedidos():
    fila = describe([{"kind": "call_wall", "price": 510.0}], symbol="QQQ")[0]
    faltan = [c for c in CAMPOS_16 if c not in fila]
    assert not faltan, faltan


def test_el_identificador_distingue_dos_lineas_del_mismo_tipo():
    """Sin él, el Auditor sólo podía decir «hay un `target`», no CUÁL."""
    filas = describe([{"kind": "scanner_target1", "price": 505.0},
                      {"kind": "scanner_target2", "price": 510.0}], symbol="QQQ")
    assert filas[0]["level_id"] != filas[1]["level_id"]


def test_el_identificador_es_estable_mientras_el_nivel_no_se_mueve():
    """Es lo que hace que `persistence` signifique algo."""
    a = describe([{"kind": "flip", "price": 517.25}], symbol="QQQ")[0]
    b = describe([{"kind": "flip", "price": 517.25}], symbol="QQQ")[0]
    c = describe([{"kind": "flip", "price": 519.00}], symbol="QQQ")[0]
    assert a["level_id"] == b["level_id"] != c["level_id"]


def test_el_nombre_visible_sale_del_motor_o_del_registro_nunca_vacio():
    delmotor = describe([{"kind": "flip", "price": 517.2, "name": "Zero Gamma"}], symbol="X")[0]
    delregistro = describe([{"kind": "flip", "price": 517.2}], symbol="X")[0]
    desconocido = describe([{"kind": "raro", "price": 1.0}], symbol="X")[0]
    assert delmotor["name"] == "Zero Gamma"
    assert delregistro["name"] == "Zero Gamma"      # del registro
    assert desconocido["name"] == "raro"
    # Y `engine_name` sigue crudo al lado, para distinguir uno de otro.
    assert delregistro["engine_name"] is None


# ── PUNTO 12 · la igualdad de muros no llevaba su contexto ───────────────

def test_la_comparacion_de_muros_declara_de_que_ciclo_es():
    """«Los muros coinciden» sin activo, sesión ni ciclo no es evidencia
    archivable: dos capturas de momentos distintos parecen la misma."""
    b = build_terminal_bundle(
        state={"active_symbol": "QQQ", "symbol_epoch": 1, "ready": True},
        trace={"candles": [{"t": 1, "c": 1.0, "v": 1}], "levels": [],
               "walls": {"call_wall": {"strike": 510.0}}})
    w = b["auditor"]["walls"]
    assert w["symbol"] == "QQQ"
    assert w["session_date"]
    assert w["cycle_id"] == b["cycle_id"]


def test_el_contexto_no_se_inventa_cuando_no_se_pasa():
    r = wall_consistency({"walls": {}, "levels": []}, {})
    assert r["symbol"] is None and r["cycle_id"] is None


# ── PUNTO 25 · el recuento se paraba en el modelo ────────────────────────

def _dp(n_flow=608, n_levels=349):
    return dp_build(
        flow_block={"ready": True, "rows": [{"t": f"2026-09-19T14:{i % 60:02d}:00Z",
                                             "dark_notional": 1e6} for i in range(n_flow)]},
        levels_block={"ready": True, "rows": [{"price": 500.0 + i, "notional": 1e6}
                                              for i in range(n_levels)]},
        prints_block={"ready": True, "rows": []},
        spot=500.0, symbol="QQQ", session={"resolved": "2026-09-19"})


def test_el_recuento_llega_hasta_lo_que_se_entrega():
    """`lineage` contaba proveedor → modelo y ahí se paraba. Si el proveedor da
    608, el modelo publica 608 y la pantalla dibuja 40, el recorte está en el
    frontend y ninguna de las dos cifras lo delata."""
    rc = _dp()["row_counts"]
    for etapa in ("provider", "view_model", "delivered"):
        assert rc[etapa]["dark_flow"] == 608, etapa
        assert rc[etapa]["dark_pool_levels"] == 349, etapa
    assert rc["consistent"] is True


def test_un_recorte_por_el_camino_se_denuncia():
    """Un nivel sin precio se descarta, y el recuento tiene que decirlo."""
    m = dp_build(flow_block={"ready": True, "rows": [{"t": "2026-09-19T14:00:00Z"}]},
                 levels_block={"ready": True, "rows": [{"price": 500.0, "notional": 1.0},
                                                       {"notional": 2.0}]},
                 prints_block={"ready": True, "rows": []},
                 spot=500.0, symbol="QQQ", session={"resolved": "2026-09-19"})
    rc = m["row_counts"]
    assert rc["provider"]["dark_pool_levels"] == 2
    assert rc["delivered"]["dark_pool_levels"] == 1
    assert rc["consistent"] is False
    assert "recorta filas" in rc["detail"]
    assert m["lineage"]["dark_pool_levels"]["drop_reason"]


# ── PUNTO 26 · el período viajaba pero no se veía ────────────────────────

def test_el_periodo_tiene_su_tarjeta():
    assert 'id="dpPeriodo"' in HTML
    assert "ventana realmente observada por carril" in HTML


def test_la_tarjeta_lee_la_ventana_de_cada_carril():
    cuerpo = APP[APP.index("PERÍODO REALMENTE OBSERVADO"):APP.index("set('dpVwap'")]
    assert "vm.temporal_scope" in cuerpo
    assert "ventana('dark_flow')" in cuerpo
    assert "ventana('equity_prints')" in cuerpo
    assert "niveles: foto acumulada" in cuerpo


# ── PUNTO 40 · la regresión visual sólo medía barras ─────────────────────

def test_el_arnes_cubre_los_cuatro_tipos_que_faltaban():
    """Campo de calor, relieve, curvas y puntos: la mitad de la terminal."""
    for kind in ("'heatmap'", "'relief'", "'lines'", "'dotmap'"):
        assert f"kind: {kind}" in HARNESS, kind


def test_los_paneles_sin_barras_se_juzgan_por_si_pintan():
    """No tienen grosor de barra que medir. Lo que sí puede romperse es que no
    dibujen NADA con datos delante, y eso la suite numérica no lo ve."""
    assert "MIN_INK" in VISUAL
    cuerpo = VISUAL[VISUAL.index("def evaluate("):VISUAL.index("def render(")]
    assert 'if r.get("thickness") is None:' in cuerpo
    assert "panel prácticamente vacío" in cuerpo


def test_el_arnes_prueba_datos_positivos_negativos_y_mixtos():
    for signo in ("'positivo'", "'negativo'", "'mixto'"):
        assert signo in HARNESS, signo


def test_el_arnes_mete_huecos_a_proposito():
    """El renderizador tiene que saber tratarlos: es el defecto que se corrigió
    en `normalize_matrix`."""
    cuerpo = HARNESS[HARNESS.index("function matriz("):HARNESS.index("function serie(")]
    assert "fila.push(null)" in cuerpo


def test_las_curvas_se_prueban_de_12_a_780_buckets():
    cuerpo = HARNESS[HARNESS.index("Curvas acumuladas"):]
    assert "[12, 120, 390, 780]" in cuerpo


# ── PUNTOS 21, 22 y 27 · cerrados en el repaso anterior ──────────────────

def test_las_seis_magnitudes_siguen_en_el_selector():
    bloque = HTML[HTML.index('id="expMetric"'):]
    bloque = bloque[:bloque.index("</select>")]
    for m in ("GEX", "DEX", "VEX", "CHEX", "OI", "VOLUME"):
        assert f'value="{m}"' in bloque, m


def test_los_kpi_de_dark_pool_dicen_que_miden():
    assert "<span>VWAP DARK POOL</span>" in HTML
    assert "OPERACIONES OSCURAS (AGREGADO)" in HTML
    assert "PRINT MAYOR (INDIVIDUAL)" in HTML

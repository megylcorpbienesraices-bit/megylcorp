"""GATE 7 · TRACE: espacio, paneles idénticos y muros lejanos. Puntos 13-16.

EL EJE COMPARTIDO ES UNA AFIRMACIÓN QUE HAY QUE PROTEGER
--------------------------------------------------------
Los tres paneles usan UNA escala de precio, pero se aplica sobre la altura del
LIENZO de cada uno. Una diferencia de cuatro píxeles entre cabeceras —un
`select` mide más que un `pill`— hacía que el mismo precio cayera en una fila
distinta en cada panel: la barra de DEX del strike 517 no se apoyaba en la
línea de 517 del centro. Nadie ve esos píxeles mirando la cabecera; se ven
leyendo un muro contra su barra, que es para lo que existe la pantalla.

UN MURO LEJANO NO PUEDE DEFORMAR EL GRÁFICO
-------------------------------------------
Con QQQ en 722 y un PUT WALL en 700, estirar la escala para que quepa dejaría
veintidós dólares vacíos y aplastaría las velas contra una línea. El muro se
conserva en los datos, se ancla al borde con una punta de flecha, y lleva su
precio y su distancia.
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = Path("app/static/itmq_terminal.css").read_text(encoding="utf-8")
TRACE = Path("app/static/itmq_trace.js").read_text(encoding="utf-8")
HTML = Path("app/templates/terminal.html").read_text(encoding="utf-8")
DOMAIN = TRACE[TRACE.index("function computePriceDomain()"):TRACE.index("function priceAxisSettling()")]
LEVELS = TRACE[TRACE.index("function drawLevels("):TRACE.index("function drawLevelHover")]


# ── 13 · Espacio ─────────────────────────────────────────────────────────

def test_el_area_central_tiene_suelo_en_pixeles():
    bloque = CSS[CSS.index(".trace-grid {"):]
    bloque = bloque[:bloque.index("}")]
    m = re.search(r"min-height:\s*(\d+)px", bloque)
    assert m and int(m.group(1)) >= 940, bloque


def test_las_columnas_laterales_dejan_sitio_a_las_cifras_del_eje():
    bloque = CSS[CSS.index(".trace-grid {"):]
    bloque = bloque[:bloque.index("}")]
    m = re.search(r"--trace-side,\s*(\d+)px", bloque)
    assert m and int(m.group(1)) >= 252


def test_el_grafico_no_cede_alto_a_lo_de_abajo():
    """El HUD acompaña al gráfico; no compite con él."""
    assert ".trace-hud { display: flex;" in CSS
    hud = CSS[CSS.index(".trace-hud {"):]
    hud = hud[:hud.index("}")]
    assert "flex: 0 0 auto" in hud


# ── 13 · Los tres paneles, idénticos ─────────────────────────────────────

def test_las_tres_cabeceras_miden_exactamente_lo_mismo():
    bloque = CSS[CSS.index(".trace-col > header {"):]
    bloque = bloque[:bloque.index("}")]
    assert "height: 36px" in bloque and "box-sizing: border-box" in bloque


def test_los_tres_lienzos_usan_el_mismo_padding():
    """Mismo `plotHeight`, mismo `topPadding`, mismo `bottomPadding`."""
    assert TRACE.count("h: env.h - PAD.top - PAD.bottom") >= 2
    assert "const PAD = { top: 12, bottom: 26 };" in TRACE


def test_hay_una_sola_funcion_de_escala_de_precio():
    assert TRACE.count("function priceScale(") == 1
    assert not re.findall(r"Q\.scale\(\s*S\.priceLo", TRACE)


def test_la_alineacion_se_mide_en_marcha_y_se_publica():
    assert "function alignment()" in TRACE
    cuerpo = TRACE[TRACE.index("function alignment()"):TRACE.index("global.ITMQTrace")]
    assert "max_delta_px" in cuerpo
    for panel in ("traceLeft", "traceMain", "traceRight"):
        assert panel in cuerpo


def test_las_tres_columnas_comparten_fila_de_rejilla():
    bloque = CSS[CSS.index(".trace-grid {"):]
    bloque = bloque[:bloque.index("}")]
    assert "grid-template-columns:" in bloque
    assert "grid-template-rows:" not in bloque


# ── 14 · Un muro lejano no deforma la escala ─────────────────────────────

def test_la_escala_no_se_construye_con_los_niveles():
    """Si un muro entrara en el dominio, uno a veinte dólares aplastaría las
    velas contra una línea."""
    assert "d.levels" not in DOMAIN
    assert "call_wall" not in DOMAIN and "put_wall" not in DOMAIN


def test_la_ventana_se_acota_alrededor_del_precio():
    assert "const half = Math.max(pSpan * 1.6, step * 7" in DOMAIN
    assert "lo = Math.max(lo, anchor - half);" in DOMAIN
    assert "hi = Math.min(hi, anchor + half);" in DOMAIN


def test_un_muro_fuera_de_ventana_se_conserva_y_se_ancla_al_borde():
    """Descartarlo hacía que el KPI lo publicara y el gráfico no lo enseñara:
    parecía que no existía."""
    assert "const above = y < box.y - 2, below = y > box.y + box.h + 2;" in LEVELS
    assert "off ? (above ? box.y + 1 : box.y + box.h - 1) : y" in LEVELS


def test_el_muro_lejano_lleva_flecha_precio_y_distancia():
    assert "const arrow = it.off < 0 ? '▲ ' : it.off > 0 ? '▼ ' : '';" in LEVELS
    assert "it.away >= 0 ? '+' : ''" in LEVELS
    assert "it.price.toFixed(digits)" in LEVELS


def test_la_linea_del_muro_lejano_se_distingue_de_la_cercana():
    """Está ahí, pero no es un precio que las velas estén tocando."""
    assert "it.off ? 0.42 : 0.72" in LEVELS
    assert "it.off ? [3, 4] : [6, 5]" in LEVELS


# ── 15 · Scanner ─────────────────────────────────────────────────────────

def test_las_cuatro_lineas_del_plan_existen_y_no_se_duplican():
    from app.core.scanner_plan import build
    plan = build({"ready": True, "direction": "BUY", "entry": 500.0,
                  "invalidation": 495.0, "target1": 505.0, "target2": 510.0,
                  "evidence_score": 72})
    assert [l["name"] for l in plan["lines"]] == ["ENTRADA", "INVAL", "OBJ1", "OBJ2"]
    assert plan["state"] == "ACTIVO"
    assert plan["thesis_id"]


def test_trace_no_recalcula_la_direccion():
    from app.core.scanner_plan import build
    plan = build({"ready": True, "entry": 500.0, "invalidation": 495.0,
                  "target1": 505.0})       # sin dirección
    assert plan["state"] == "ESPERANDO"
    assert "dirección" in plan["detail"]
    assert plan["lines"] == []


def test_el_plan_sustituye_a_target_y_risk_no_convive():
    main = Path("app/main.py").read_text(encoding="utf-8")
    assert 'lv.get("kind") in ("target", "risk")' in main


# ── 16 · Todas las líneas identificadas ──────────────────────────────────

def test_todo_kind_que_el_renderer_dibuja_tiene_procedencia():
    from app.core.level_identity import LEVEL_ORIGIN
    core = Path("app/static/itmq_core.js").read_text(encoding="utf-8")
    tabla = core[core.index("const LEVELS = {"):core.index("/** Niveles que el panel de flujo")]
    kinds = set(re.findall(r"^\s{4}([a-z_0-9]+):\s*\{", tabla, re.M))
    faltan = sorted(kinds - set(LEVEL_ORIGIN))
    assert not faltan, faltan


def test_una_linea_sin_identidad_se_denuncia():
    from app.core.level_identity import UNIDENTIFIED, audit
    a = audit([{"kind": "misterio", "price": 517.2}], symbol="QQQ")
    assert a["ok"] is False and a["rows"][0]["identity_status"] == UNIDENTIFIED


def test_la_identidad_no_se_deduce_del_color():
    """Rojo es `put_wall` Y `risk`; verde es `call_wall` Y `target`."""
    from app.core.level_identity import LEVEL_ORIGIN
    core = Path("app/static/itmq_core.js").read_text(encoding="utf-8")
    assert LEVEL_ORIGIN["put_wall"]["function"] != LEVEL_ORIGIN["scanner_inval"]["function"]
    # Dos `kind` DISTINTOS comparten el mismo token de color, así que deducir
    # la identidad del color acierta la mitad de las veces y no avisa al fallar.
    rojos = set(re.findall(r"^\s{4}([a-z_0-9]+):\s*\{ color: '--neg'", core, re.M))
    assert len(rojos) >= 2, rojos
    # Y cada uno declara una procedencia distinta en el registro.
    funciones = {LEVEL_ORIGIN[k]["function"] for k in rojos if k in LEVEL_ORIGIN}
    assert len(funciones) >= 2, funciones

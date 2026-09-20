"""v1.54.0 · Política de frescura de FLUJO DE ÓRDENES y diagnóstico del agresor.

La pantalla mostraba a la vez «405 buckets · último hace 3610 min» y
«PRIMA TOTAL: SIN DATOS». Dos afirmaciones contradictorias sobre el mismo
dato, porque el módulo tenía UN estado global: si el ciclo venía vacío, todo
se vaciaba aunque los carriles siguieran siendo válidos.
"""
from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
T0 = datetime(2026, 9, 21, 14, 30, tzinfo=timezone.utc)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean():
    from app.core import flow_view as FV
    FV.reset()
    yield
    FV.reset()


# ════════════════════════════════════════════════════════════════════════════
# Los siete escenarios obligatorios
# ════════════════════════════════════════════════════════════════════════════

def test_A_live_con_datos_nuevos():
    from app.core import flow_view as FV
    r = FV.lane("DIA", "2026-09-21", "premium_buy", 8_400_000.0, now=T0)
    assert r["status"] == FV.LIVE
    assert r["current"] == pytest.approx(8_400_000.0)
    assert r["screen_note"] == ""


def test_B_ciclo_siguiente_vacio_conserva_el_ultimo_valido():
    """ÉSTE es el defecto: 405 buckets válidos desaparecían porque no llegó
    el 406."""
    from app.core import flow_view as FV
    buckets = [{"t": f"T{i}"} for i in range(405)]
    FV.lane("DIA", "2026-09-21", "tape_buckets", buckets, now=T0)
    r = FV.lane("DIA", "2026-09-21", "tape_buckets", None, now=T0 + timedelta(minutes=9))
    assert r["status"] == FV.STALE
    assert len(r["current"]) == 405, "la serie NO se borra"
    assert r["age_minutes"] == pytest.approx(9.0)
    assert "último dato" in r["screen_note"]


def test_B2_un_hueco_corto_sigue_siendo_live():
    """Un mercado tranquilo no es un fallo: medio minuto sin print nuevo no
    convierte el dato en viejo."""
    from app.core import flow_view as FV
    FV.lane("DIA", "2026-09-21", "qflow", [1, 2], now=T0)
    r = FV.lane("DIA", "2026-09-21", "qflow", None, now=T0 + timedelta(seconds=40))
    assert r["status"] == FV.LIVE


def test_C_mercado_cerrado_carga_la_ultima_sesion_valida():
    from app.core import flow_view as FV
    FV.lane("DIA", "2026-09-18", "premium_total", 12_000_000.0, now=T0)
    r = FV.lane("DIA", "2026-09-18", "premium_total", None,
                now=T0 + timedelta(hours=20), market_open=False)
    assert r["status"] == FV.HISTORICAL
    assert r["current"] == pytest.approx(12_000_000.0)


def test_D_cambiar_de_activo_no_arrastra_el_dato_anterior():
    """DIA no puede aparecer ni un segundo bajo QQQ."""
    from app.core import flow_view as FV
    FV.lane("DIA", "2026-09-21", "premium_buy", 8_400_000.0, now=T0)
    r = FV.lane("QQQ", "2026-09-21", "premium_buy", None, now=T0 + timedelta(minutes=1))
    assert r["status"] == FV.NO_DATA
    assert r["current"] is None
    # Y DIA conserva el suyo.
    d = FV.lane("DIA", "2026-09-21", "premium_buy", None, now=T0 + timedelta(minutes=5))
    assert d["current"] == pytest.approx(8_400_000.0)


def test_D2_la_sesion_tambien_forma_parte_de_la_clave():
    """El cierre de ayer no puede mostrarse como si fuera de hoy."""
    from app.core import flow_view as FV
    FV.lane("DIA", "2026-09-18", "premium_total", 5.0, now=T0)
    r = FV.lane("DIA", "2026-09-21", "premium_total", None, now=T0)
    assert r["status"] == FV.NO_DATA


def test_E_net_flow_disponible_y_tape_ausente():
    """Vaciar un dataset porque falta el otro es perder un dato bueno para
    señalar uno malo."""
    from app.core import flow_view as FV
    FV.lane("DIA", "2026-09-21", "tape_buckets", [{"t": "T"}], now=T0)
    vm = FV.build(symbol="DIA", session_date="2026-09-21", market_open=True,
                  tape={"buckets": None},
                  net_flow={"series": [{"t": "T", "v": 1}]},
                  now=T0 + timedelta(minutes=10))
    assert vm["net_flow"]["status"] == FV.LIVE
    assert vm["tape"]["status"] == FV.STALE
    assert vm["tape"]["current"] is not None, "la cinta conserva su último bueno"


def test_F_tape_disponible_y_net_flow_ausente():
    from app.core import flow_view as FV
    vm = FV.build(symbol="DIA", session_date="2026-09-21", market_open=True,
                  tape={"buckets": [{"t": "T"}], "total_premium": 3.0},
                  net_flow={"series": None}, now=T0)
    assert vm["tape"]["status"] == FV.LIVE
    assert vm["premiums"]["total"]["current"] == pytest.approx(3.0)
    assert vm["net_flow"]["status"] == FV.NO_DATA


def test_G_sin_historico_ni_actual_es_el_unico_sin_datos():
    from app.core import flow_view as FV
    vm = FV.build(symbol="SOFI", session_date="2026-09-21", market_open=True,
                  tape={}, net_flow={}, qflow={}, now=T0)
    assert vm["tape"]["status"] == FV.NO_DATA
    assert vm["freshness"]["status"] == FV.NO_DATA


# ════════════════════════════════════════════════════════════════════════════
# Reglas transversales
# ════════════════════════════════════════════════════════════════════════════

def test_un_cero_medido_no_es_un_hueco():
    """`0` es una afirmación —ese minuto no se pagó prima— y viaja como 0."""
    from app.core import flow_view as FV
    r = FV.lane("DIA", "2026-09-21", "premium_buy", 0.0, now=T0)
    assert r["status"] == FV.LIVE
    assert r["current"] == 0.0, "el cero MEDIDO se publica"
    # Y el siguiente ciclo vacío conserva ese cero, no lo convierte en None.
    r2 = FV.lane("DIA", "2026-09-21", "premium_buy", None, now=T0 + timedelta(minutes=9))
    assert r2["current"] == 0.0 and r2["status"] == FV.STALE


def test_un_error_del_proveedor_no_borra_el_ultimo_bueno():
    from app.core import flow_view as FV
    FV.lane("DIA", "2026-09-21", "qflow", [1, 2, 3], now=T0)
    r = FV.lane("DIA", "2026-09-21", "qflow", None, now=T0 + timedelta(minutes=2),
                error="HTTP 503")
    assert r["status"] == FV.PROVIDER_ERROR
    assert r["current"] == [1, 2, 3]
    assert "503" in r["detail"]


def test_la_prima_sin_agresor_tiene_su_propio_cubo():
    """`CALL` no es compra y `PUT` no es venta: la prima sin lado no se reparte."""
    from app.core import flow_view as FV
    vm = FV.build(symbol="DIA", session_date="2026-09-21", market_open=True,
                  tape={"buy_premium": 4.0, "sell_premium": 3.0, "unknown_premium": 9.0},
                  now=T0)
    assert vm["premiums"]["unclassified"]["current"] == pytest.approx(9.0)
    assert vm["premiums"]["buy"]["current"] == pytest.approx(4.0)


def test_la_pantalla_operativa_no_escribe_jerga_tecnica():
    """Proveedores, endpoints y trazas van al Auditor, no al panel de análisis."""
    from app.core import flow_view as FV
    FV.lane("DIA", "2026-09-21", "qflow", [1], now=T0)
    r = FV.lane("DIA", "2026-09-21", "qflow", None, now=T0 + timedelta(minutes=30))
    note = r["screen_note"].lower()
    for jerga in ("http", "quantdata", "endpoint", "null", "none", "traceback"):
        assert jerga not in note, note


def test_la_seccion_esta_viva_mientras_algo_suyo_lo_este():
    from app.core import flow_view as FV
    vm = FV.build(symbol="DIA", session_date="2026-09-21", market_open=True,
                  tape={}, net_flow={"series": [1]}, qflow={}, now=T0)
    assert vm["freshness"]["status"] == FV.LIVE
    assert vm["freshness"]["lanes_live"] >= 1


# ════════════════════════════════════════════════════════════════════════════
# Diagnóstico del agresor · dónde se rompe la cadena
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("events,attribution,order_flow,expected", [
    ([{"aggressor": "UNKNOWN"}], {"events": []}, {"rows": []},
     "TAPE_MISSING"),
    ([{"aggressor": "UNKNOWN"}], {"events": []},
     {"rows": [{}] * 10, "aggressor_coverage": {"with_side": 0, "total": 10}},
     "TAPE_WITHOUT_SIDE"),
    ([{"aggressor": "UNKNOWN"}], {"events": [{"matched": False}]},
     {"rows": [{}] * 10, "aggressor_coverage": {"with_side": 8, "total": 10}},
     "ATTRIBUTION_NO_MATCH"),
    ([{"aggressor": "MIXED"}],
     {"events": [{"matched": True, "buy_premium": 500, "sell_premium": 500}]},
     {"rows": [{}] * 10, "aggressor_coverage": {"with_side": 8, "total": 10}},
     "NO_DOMINANCE"),
    ([{"aggressor": "BUY"}],
     {"events": [{"matched": True, "buy_premium": 900, "sell_premium": 100}]},
     {"rows": [{}] * 10, "aggressor_coverage": {"with_side": 8, "total": 10}},
     None),
])
def test_el_diagnostico_senala_el_primer_eslabon_roto(events, attribution, order_flow, expected):
    """Los eslabones posteriores fallan por consecuencia; señalarlos manda al
    sitio equivocado."""
    from app.core.qflow import aggressor_diagnosis
    d = aggressor_diagnosis(events, attribution, order_flow)
    assert d["broken_at"] == expected
    assert len(d["chain"]) == 4
    if expected:
        assert d["remedy"], "un diagnóstico sin remedio no es accionable"


def test_el_diagnostico_viaja_en_qflow():
    api = _read("app/terminal_api.py")
    assert "order_flow_block=of" in api
    assert '"aggressor_coverage": blk.get("aggressor_coverage")' in api
    q = _read("app/core/qflow.py")
    assert '"aggressor_diagnosis": agg_diag' in q


# ════════════════════════════════════════════════════════════════════════════
# Plan del Scanner en TRACE
# ════════════════════════════════════════════════════════════════════════════

def test_sin_tesis_el_plan_espera_y_no_inventa_nada():
    """Derivar una entrada que el Scanner no publicó crearía un SEGUNDO motor
    direccional, y cuando discrepen nadie sabrá cuál mirar."""
    from app.core import scanner_plan as SP
    p = SP.build({"ready": False}, spot=516.0)
    assert p["state"] == SP.WAITING
    assert p["lines"] == []
    assert p["entry"] is None and p["target1"] is None and p["invalidation"] is None
    assert p["detail"], "tiene que decir qué falta"


def test_una_tesis_completa_produce_las_cuatro_lineas():
    from app.core import scanner_plan as SP
    p = SP.build({"ready": True, "direction": "BUY", "evidence_score": 82,
                  "entry": 515.40, "invalidation": 513.80,
                  "target1": 517.00, "target2": 518.20,
                  "edge_state": "ACTIONABLE"}, spot=515.50)
    assert p["state"] == SP.ACTIVE
    assert p["direction"] == "COMPRA" and p["strength"] == 82
    assert [l["name"] for l in p["lines"]] == ["ENTRADA", "INVAL", "OBJ1", "OBJ2"]
    for l in p["lines"]:
        assert l["source"] == "SCANNER"
        assert l["direction"] == "COMPRA"
        assert l["timestamp"]


def test_el_plan_es_transaccional():
    """Media tesis vieja con media nueva parece coherente y no lo es."""
    from app.core import scanner_plan as SP
    a = SP.build({"ready": True, "direction": "BUY", "entry": 515.4,
                  "invalidation": 513.8, "target1": 517.0, "target2": 518.2})
    b = SP.build({"ready": True, "direction": "BUY", "entry": 515.4,
                  "invalidation": 513.8, "target1": 517.0, "target2": 518.2})
    assert a["thesis_id"] == b["thesis_id"], "la misma tesis, el mismo id"
    c = SP.build({"ready": True, "direction": "BUY", "entry": 515.4,
                  "invalidation": 513.8, "target1": 517.5, "target2": 518.2})
    assert c["thesis_id"] != a["thesis_id"], "cambia un objetivo, cambia el bloque"


def test_el_precio_que_cruza_la_invalidacion_marca_el_plan_invalidado():
    """No es recalcular dirección: es leer el nivel que el Scanner publicó."""
    from app.core import scanner_plan as SP
    p = SP.build({"ready": True, "direction": "BUY", "entry": 515.4,
                  "invalidation": 513.8, "target1": 517.0,
                  "edge_state": "ACTIONABLE"}, spot=513.5)
    assert p["state"] == SP.INVALIDATED
    assert p["invalidation_crossed"] is True
    # En venta, la invalidación se cruza por arriba.
    v = SP.build({"ready": True, "direction": "SELL", "entry": 515.4,
                  "invalidation": 517.2, "target1": 513.0,
                  "edge_state": "ACTIONABLE"}, spot=517.5)
    assert v["state"] == SP.INVALIDATED


def test_el_plan_no_conoce_ningun_ticker():
    src = _read("app/core/scanner_plan.py")
    import re
    for sym in ("DIA", "SPY", "QQQ", "IWM", "AAPL", "SPX"):
        assert not re.search(rf"['\"]{sym}['\"]", src), sym


def test_las_lineas_del_scanner_tienen_identidad_y_prioridad_alta():
    from app.core.level_identity import LEVEL_ORIGIN
    for kind in ("scanner_entry", "scanner_inval", "scanner_target1", "scanner_target2"):
        assert kind in LEVEL_ORIGIN, kind
        assert LEVEL_ORIGIN[kind]["function"] == "scanner_plan.build"
    core = _read("app/static/itmq_core.js")
    # Orden 0: son las que se miran para decidir, nunca pueden quedarse sin
    # etiqueta por detrás de un centroide.
    for kind in ("scanner_entry", "scanner_inval", "scanner_target1", "scanner_target2"):
        assert f"{kind}:" in core and "order: 0" in core[core.index(f"{kind}:"):core.index(f"{kind}:") + 140]


def test_la_barra_del_scanner_vive_en_el_encabezado_de_trace():
    """No es una sección nueva: es la misma barra del TRACE."""
    html = _read("app/templates/terminal.html")
    assert 'id="scannerBar"' in html
    shell = html[html.index('class="trace-shell"'):html.index('class="trace-grid"')]
    assert 'id="scannerBar"' in shell, "dentro del encabezado de TRACE"
    for el_id in ("sbDirection", "sbStrength", "sbEntry", "sbInval",
                  "sbObj1", "sbObj2", "sbState", "sbDetail"):
        assert f'id="{el_id}"' in html, el_id
    app = _read("app/static/itmq_app.js")
    assert "function renderScannerBar(" in app
    assert "renderScannerBar(d);" in app


def test_el_hover_identifica_la_linea_bajo_el_cursor():
    js = _read("app/static/itmq_trace.js")
    assert "function drawLevelHover(" in js
    body = js[js.index("function drawLevelHover("):js.index("function drawCrosshair(")]
    assert "lv.authority || id.source" in body
    assert "id.timestamp" in body
    # Dirección y fuerza SOLO si el nivel las trae: en un muro no significan nada.
    assert "if (lv.direction)" in body
    assert "drawLevelHover(ctx, box, sy" in js


def test_el_trace_tiene_mas_alto():
    css = _read("app/static/itmq_terminal.css")
    block = css[css.index(".trace-grid {"):css.index(".trace-col {")]
    assert "min-height: 780px" in block


# ════════════════════════════════════════════════════════════════════════════
# Modo de sesión · Londres desde las 04:00 de Ecuador
# ════════════════════════════════════════════════════════════════════════════

def _ec(y, m, d, hh, mm=0):
    from zoneinfo import ZoneInfo
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/Guayaquil"))


@pytest.fixture
def _sesion_limpia():
    from app.core import session_mode as SM
    SM.reset()
    yield SM
    SM.reset()


def test_a_las_cuatro_de_ecuador_entra_london_monitor(_sesion_limpia):
    SM = _sesion_limpia
    assert SM.resolve(_ec(2026, 9, 21, 3, 59))["mode"] == SM.PRE_LONDON
    assert SM.resolve(_ec(2026, 9, 21, 4, 0))["mode"] == SM.LONDON_MONITOR
    assert SM.resolve(_ec(2026, 9, 21, 7, 0))["mode"] == SM.LONDON_MONITOR


def test_el_corte_de_nueva_york_sigue_su_horario_de_verano(_sesion_limpia):
    """Ecuador no cambia la hora y Nueva York sí: una hora UTC fija acertaría
    medio año y fallaría el otro medio sin avisar."""
    SM = _sesion_limpia
    verano = SM.resolve(_ec(2026, 7, 15, 8, 35))     # NY en EDT → 08:30 EC
    invierno = SM.resolve(_ec(2026, 12, 15, 8, 35))  # NY en EST → 09:30 EC
    assert verano["mode"] == SM.NEW_YORK
    assert invierno["mode"] == SM.LONDON_MONITOR, "en invierno NY aún no ha abierto"


def test_londres_no_se_borra_cuando_empieza_nueva_york(_sesion_limpia):
    SM = _sesion_limpia
    SM.accumulate("DIA", price=516.0, volume=1000.0, now=_ec(2026, 9, 21, 5, 0))
    SM.accumulate("DIA", price=517.2, volume=500.0, now=_ec(2026, 9, 21, 7, 0))
    cerrada = SM.close_session("DIA", SM.LONDON_MONITOR, now=_ec(2026, 9, 21, 8, 31))
    assert cerrada["closed"] is True
    SM.accumulate("DIA", price=518.0, volume=2000.0, now=_ec(2026, 9, 21, 9, 0))
    dos = SM.both("DIA", now=_ec(2026, 9, 21, 10, 0))
    assert dos["london"]["high"] == pytest.approx(517.2)
    assert dos["london"]["volume"] == pytest.approx(1500.0)
    assert dos["new_york"]["volume"] == pytest.approx(2000.0)
    assert dos["london"]["closed"] is True


def test_el_acumulado_solo_crece_con_lo_medido(_sesion_limpia):
    """Un ciclo sin volumen NO significa volumen cero."""
    SM = _sesion_limpia
    SM.accumulate("QQQ", price=722.0, volume=None, now=_ec(2026, 9, 21, 5, 0))
    s = SM.snapshot("QQQ", SM.LONDON_MONITOR, now=_ec(2026, 9, 21, 5, 1))
    assert s["volume"] is None, "sin observación, el acumulado no inventa un cero"
    SM.accumulate("QQQ", price=723.0, volume=300.0, now=_ec(2026, 9, 21, 5, 2))
    s = SM.snapshot("QQQ", SM.LONDON_MONITOR, now=_ec(2026, 9, 21, 5, 3))
    assert s["volume"] == pytest.approx(300.0)
    assert s["move"] == pytest.approx(1.0)


def test_el_acumulado_se_guarda_por_simbolo_fecha_y_sesion(_sesion_limpia):
    SM = _sesion_limpia
    SM.accumulate("DIA", price=516.0, volume=10.0, now=_ec(2026, 9, 21, 5, 0))
    assert SM.snapshot("QQQ", SM.LONDON_MONITOR, now=_ec(2026, 9, 21, 5, 1)) is None
    assert SM.snapshot("DIA", SM.LONDON_MONITOR, "2026-09-18") is None
    assert SM.snapshot("DIA", SM.NEW_YORK, now=_ec(2026, 9, 21, 5, 1)) is None


def test_el_vwap_de_la_sesion_pondera_por_volumen(_sesion_limpia):
    SM = _sesion_limpia
    SM.accumulate("SPY", price=100.0, volume=300.0, now=_ec(2026, 9, 21, 5, 0))
    SM.accumulate("SPY", price=110.0, volume=100.0, now=_ec(2026, 9, 21, 5, 1))
    s = SM.snapshot("SPY", SM.LONDON_MONITOR, now=_ec(2026, 9, 21, 5, 2))
    assert s["vwap"] == pytest.approx((100 * 300 + 110 * 100) / 400)


def test_la_sesion_no_conoce_ningun_ticker():
    src = _read("app/core/session_mode.py")
    import re
    for sym in ("DIA", "SPY", "QQQ", "IWM", "AAPL"):
        assert not re.search(rf"['\"]{sym}['\"]", src), sym
    assert "America/Guayaquil" in src
    assert "America/New_York" in src, "el corte de NY se construye en SU hora"


def test_el_plan_sustituye_a_target_y_risk_en_vez_de_convivir():
    """Los dos salen del MISMO campo del Scanner: dibujarlos a la vez pintaba
    «OBJ1 533.70» pegada a «OBJ 533.70», la misma información dos veces."""
    main = _read("app/main.py")
    start = main.index("plan = _SP.build(")
    block = main[start:main.index('payload["levels"] = kept', start)]
    assert 'lv.get("kind") in ("target", "risk")' in block
    assert 'if plan.get("ready"):' in block, "sólo se sustituye si hay tesis"

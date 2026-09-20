"""v1.55.0 · El FlowViewModel deja de ser un modulo suelto y pasa a MANDAR.

Estaba escrito y probado desde v1.54.0, pero la seccion FLUJO DE ORDENES no lo
consumia: seguia recalculando sus tarjetas con un estado GLOBAL. Por eso en
pantalla convivian

    ESTADO            DATO ANTIGUO · 405 buckets · ultimo hace 3610 min
    PRIMA TOTAL       SIN DATOS
    PRIMA COMPRADORA  SIN DATOS

Un modelo correcto que nadie lee no corrige nada.
"""
from __future__ import annotations

from pathlib import Path

from app.core import flow_view as FV
from app.terminal_api import _flow_view_model, _tape_totals

ROWS = [
    {"t": "2026-09-19T14:00:00+00:00", "premium": 10_000.0, "size": 10,
     "aggressor": "BUY", "strike": 500.0, "option_type": "CALL"},
    {"t": "2026-09-19T14:01:00+00:00", "premium": -4_000.0, "size": 4,
     "aggressor": "SELL", "strike": 501.0, "option_type": "PUT"},
    {"t": "2026-09-19T14:02:00+00:00", "premium": 2_000.0, "size": 2,
     "aggressor": "UNKNOWN", "strike": 502.0, "option_type": "CALL"},
]


# ── La cinta se reparte por AGRESOR, no por tipo de contrato ─────────────

def test_la_prima_se_reparte_solo_por_agresor_resuelto():
    t = _tape_totals(ROWS)
    assert t["buy_premium"] == 10_000.0
    assert t["sell_premium"] == 4_000.0          # magnitud, el lado ya lo dice el campo
    assert t["unknown_premium"] == 2_000.0       # NO se reparte a ningun lado
    assert t["total_premium"] == 16_000.0


def test_la_cobertura_del_agresor_viaja_con_el_sesgo():
    """Un 8 % clasificado no sostiene la misma lectura que un 95 %."""
    t = _tape_totals(ROWS)
    assert t["aggressor_coverage_pct"] == 87.5


def test_sin_prints_todo_es_none_y_nada_es_cero():
    """`$0.0` afirma «hoy no se negocio prima». Eso es una conclusion."""
    t = _tape_totals([])
    for k in ("buy_premium", "sell_premium", "total_premium", "unknown_premium",
              "volume", "largest_print"):
        assert t[k] is None, k


def test_el_print_mayor_se_elige_por_magnitud_no_por_signo():
    t = _tape_totals(ROWS)
    assert t["largest_print"]["premium"] == 10_000.0


# ── El modelo llega a la seccion ─────────────────────────────────────────

def test_el_modelo_publica_los_carriles_por_separado():
    FV.reset()
    vm = _flow_view_model("QQQ", {}, {**_tape_totals(ROWS), "buckets": ROWS},
                          [{"t": "x"}], {"markers": []})
    assert vm["tape"]["status"] == FV.LIVE
    assert vm["net_flow"]["status"] == FV.LIVE
    # QFLOW sin marcas NO arrastra a los demas: cada carril lleva su estado.
    assert vm["qflow"]["status"] == FV.NO_DATA
    assert vm["freshness"]["status"] == FV.LIVE


def test_un_ciclo_vacio_no_borra_lo_que_ya_era_bueno():
    """Es el defecto exacto: el ultimo ciclo sin prints vaciaba la seccion."""
    FV.reset()
    _flow_view_model("QQQ", {}, {**_tape_totals(ROWS), "buckets": ROWS},
                     [{"t": "x"}], {"markers": []})
    vacio = _flow_view_model("QQQ", {}, {**_tape_totals([]), "buckets": None},
                             None, {"markers": []})
    assert vacio["premiums"]["buy"]["current"] == 10_000.0
    assert vacio["premiums"]["buy"]["status"] in (FV.LIVE, FV.STALE, FV.HISTORICAL)
    assert vacio["tape"]["current"] == ROWS


def test_el_lkg_no_cruza_de_un_activo_a_otro():
    """Sin `symbol` en la clave, al pasar de DIA a QQQ el valor de DIA aparecia
    unos segundos bajo QQQ: un numero correcto en el sitio equivocado."""
    FV.reset()
    _flow_view_model("DIA", {}, {**_tape_totals(ROWS), "buckets": ROWS}, None, {})
    otro = _flow_view_model("QQQ", {}, {**_tape_totals([]), "buckets": None}, None, {})
    assert otro["premiums"]["buy"]["current"] is None
    assert otro["premiums"]["buy"]["status"] == FV.NO_DATA


def test_la_seccion_incluye_el_modelo_en_su_salida():
    import app.terminal_api as TA
    src = Path("app/terminal_api.py").read_text(encoding="utf-8")
    cuerpo = src[src.index("def _flujo_ordenes"):src.index("def _flow_view_model")]
    assert '"view_model": view_model' in cuerpo
    assert hasattr(TA, "_flow_view_model")


# ── El frontend lo consume ───────────────────────────────────────────────

def test_el_cliente_pide_el_modelo_y_lo_pasa_a_la_seccion():
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    assert "Flow.applyFlowView((d.flujo_ordenes || {}).view_model)" in js


def test_las_tarjetas_leen_del_modelo_cuando_existe():
    js = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    assert "function renderSummaryFromModel" in js
    assert "if (S.flowView) { renderSummaryFromModel(S.flowView); return; }" in js
    assert "applyFlowView" in js[js.index("global.ITMQFlow ="):]


def test_la_tarjeta_escribe_la_edad_del_dato_en_vez_de_sin_datos():
    """Un valor de hace diez minutos sigue siendo informacion; un hueco no."""
    js = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("function renderSummaryFromModel"):js.index("function applyFlowView")]
    assert "screen_note" in cuerpo
    assert "ofBuyNote" in cuerpo and "ofSellNote" in cuerpo
    # SIN DATOS solo cuando el carril NO tiene valor.
    assert "if (lane.current == null) return 'SIN DATOS';" in cuerpo


def test_la_cinta_local_sigue_siendo_el_respaldo():
    """Si el modelo no viaja, la seccion no puede quedarse muda."""
    js = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    resumen = js[js.index("function renderSummary()"):]
    assert "S.buckets" in resumen[:2000]

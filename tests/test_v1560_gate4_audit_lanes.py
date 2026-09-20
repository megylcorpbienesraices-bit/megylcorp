"""GATE 4 · Auditoría del agresor y LKG por carril. Puntos 4 y 7.

DOS COSAS DISTINTAS
-------------------
La AUDITORÍA responde «¿por qué esta marca está neutra?» con el eslabón exacto.
El LKG responde «¿lo que veo es de ahora o de antes?» carril por carril.

Las dos existen porque «no hay datos» era la única respuesta que sabía dar la
pantalla, y esa respuesta es indistinguible de seis averías con arreglos
distintos.
"""
from __future__ import annotations

from app.core import flow_view as FV
from app.core.qflow import aggressor_diagnosis
from app.providers.quantdata.tools import norm_option_order_flow
from app.terminal_api import build_terminal_bundle

ESTADOS = {"DATA_OK", "TAPE_MISSING", "TAPE_WITHOUT_SIDE", "NBBO_MISSING",
           "MID_TRADE", "ATTRIBUTION_NO_MATCH", "NO_DOMINANCE"}

CONTADORES = ("tape_rows", "explicit_side_rows", "nbbo_classified_rows",
              "unknown_rows", "attribution_matches", "attribution_misses",
              "buy_rows", "sell_rows", "coverage_pct", "broken_at")


def setup_function():
    FV.reset()


# ── 4 · La auditoría del agresor ─────────────────────────────────────────

def test_el_auditor_publica_los_diez_contadores():
    a = build_terminal_bundle(state={"active_symbol": "QQQ", "symbol_epoch": 1,
                                     "ready": True}, trace={})["auditor"]["aggressor"]
    faltan = [k for k in CONTADORES if k not in a]
    assert not faltan, faltan


def test_los_siete_estados_estan_declarados():
    a = build_terminal_bundle(state={"active_symbol": "QQQ", "symbol_epoch": 1,
                                     "ready": True}, trace={})["auditor"]["aggressor"]
    assert set(a["states"]) == ESTADOS


def test_sin_cinta_el_estado_es_tape_missing_y_no_otro():
    """El PRIMER eslabón que falla es el que hay que arreglar; los de después
    fallan por consecuencia y señalarlos manda al sitio equivocado."""
    a = build_terminal_bundle(state={"active_symbol": "QQQ", "symbol_epoch": 1,
                                     "ready": True}, trace={})["auditor"]["aggressor"]
    assert a["state"] == "TAPE_MISSING"
    assert a["tape_rows"] == 0


def test_la_auditoria_declara_su_autoridad():
    a = build_terminal_bundle(state={"active_symbol": "QQQ", "symbol_epoch": 1,
                                     "ready": True}, trace={})["auditor"]["aggressor"]
    assert a["authority"] == "ITMQ_AGGRESSOR_TRADE_SIDE_CODE_FIRST"


def test_cada_estado_trae_su_remedio_en_lenguaje_accionable():
    filas = [{"timestamp": f"2026-09-19T14:0{i}:00Z", "price": 2.4, "size": 1,
              "optionType": "CALL", "strike": 500} for i in range(3)]
    d = aggressor_diagnosis([], {"events": []},
                            norm_option_order_flow({"data": filas}))
    assert d["state"] == "TAPE_WITHOUT_SIDE"
    assert d["remedy"] and len(d["remedy"]) > 30


def test_la_atribucion_cuenta_aciertos_y_fallos():
    d = aggressor_diagnosis([], {"events": [{"matched": True}, {"matched": False},
                                            {"matched": False}]},
                            {"rows": [], "aggressor_coverage": {}})
    assert d["attribution_matches"] == 1
    assert d["attribution_misses"] == 2


# ── 7 · LKG por carril ───────────────────────────────────────────────────

CAMPOS = ("current", "last_known_good", "status", "timestamp", "age_minutes",
          "symbol", "session_date", "dataset", "source_mode")


def test_cada_carril_lleva_los_nueve_campos():
    l = FV.lane("QQQ", "2026-09-19", "tape_buckets", [1, 2, 3])
    faltan = [c for c in CAMPOS if c not in l]
    assert not faltan, faltan


def test_el_lkg_se_publica_aparte_del_valor_actual():
    """Cuando el carril está vivo son el mismo valor; cuando está viejo,
    `current` ES el LKG, y quien lea la respuesta tiene que poder saberlo sin
    deducirlo del estado."""
    FV.lane("QQQ", "2026-09-19", "tape_buckets", [1, 2, 3])
    viejo = FV.lane("QQQ", "2026-09-19", "tape_buckets", None)
    assert viejo["current"] == [1, 2, 3]
    assert viejo["last_known_good"] == [1, 2, 3]


def test_la_clave_completa_viaja_en_la_respuesta():
    """Un LKG mal indexado enseña un número correcto en el sitio equivocado."""
    l = FV.lane("SPY", "2026-09-18", "net_flow", [1])
    assert (l["symbol"], l["session_date"], l["dataset"]) == ("SPY", "2026-09-18", "net_flow")


def test_los_ocho_carriles_existen():
    m = FV.build(symbol="QQQ", session_date="2026-09-19", market_open=True,
                 tape={"buckets": [{"t": "2026-09-19T14:00:00Z", "premium": 1.0,
                                    "size": 1, "aggressor": "BUY"}],
                       "aggressor_coverage_pct": 100.0},
                 net_flow={"series": [1]}, qflow={"markers": [1]},
                 net_drift={"series": [1], "state": "DATA_OK"})
    faltan = [l for l in FV.LANES if l not in m]
    assert not faltan, faltan
    assert len(FV.LANES) == 8


def test_net_drift_es_su_propio_carril():
    """Que falte no puede vaciar la cinta, y que falte la cinta no puede
    vaciarlo a él: son dos endpoints distintos."""
    m = FV.build(symbol="QQQ", session_date="2026-09-19", market_open=True,
                 tape={"buckets": None}, net_flow={}, qflow={},
                 net_drift={"series": [1, 2], "state": "DATA_OK"})
    assert m["net_drift"]["status"] == FV.LIVE
    assert m["tape"]["status"] == FV.NO_DATA


def test_el_agresor_es_un_carril_porque_puede_fallar_solo():
    """La cinta llega entera y aun así ninguna operación resuelve lado."""
    m = FV.build(symbol="QQQ", session_date="2026-09-19", market_open=True,
                 tape={"buckets": [{"t": "2026-09-19T14:00:00Z", "premium": 1.0,
                                    "size": 1, "aggressor": "UNKNOWN"}],
                       "aggressor_coverage_pct": None},
                 net_flow={}, qflow={})
    assert m["tape"]["status"] == FV.LIVE
    assert m["aggressor"]["status"] == FV.NO_DATA
    assert m["aggressor"]["source_mode"] == "DERIVED"


def test_las_cinco_reglas_de_frescura():
    FV.reset()
    # 1 · dato nuevo -> LIVE
    assert FV.lane("A", "d", "x", [1])["status"] == FV.LIVE
    # 2 · sin dato nuevo + LKG -> se muestra el LKG
    assert FV.lane("A", "d", "x", None)["current"] == [1]
    # 3 · mercado cerrado + sesión válida -> HISTÓRICO
    assert FV.lane("A", "d", "x", None, market_open=False)["status"] == FV.HISTORICAL
    # 4 · nunca hubo dato -> SIN DATOS
    assert FV.lane("A", "d", "nuevo", None)["status"] == FV.NO_DATA
    # 5 · error + LKG -> se conserva el valor y se declara el error
    err = FV.lane("A", "d", "x", None, error="502")
    assert err["status"] == FV.PROVIDER_ERROR and err["current"] == [1]


def test_el_inventario_del_lkg_llega_al_auditor():
    FV.lane("QQQ", "2026-09-19", "tape_buckets", [1])
    a = build_terminal_bundle(state={"active_symbol": "QQQ", "symbol_epoch": 1,
                                     "ready": True}, trace={})["auditor"]["last_known_good"]
    assert a["count"] >= 1
    assert a["key"] == "(symbol, session_date, dataset)"

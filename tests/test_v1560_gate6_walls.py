"""GATE 6 · Los muros: cómo se calculan y quién los lee. Puntos 11, 12 y 24.

NO BASTA CON QUE APAREZCA LA LÍNEA
----------------------------------
Un muro dibujado es una afirmación: «aquí hay una concentración que va a frenar
el precio». Si su único respaldo es que hay una línea en el gráfico, no hay
forma de discutirla ni de detectar que el cálculo empezó a medir otra cosa.

UNA AUTORIDAD, SEIS LECTORES
----------------------------
El defecto original no fue un cálculo mal hecho: fue que cada sección llamaba a
`structural_walls()` con su propio frame, así que el mismo nombre señalaba dos
strikes distintos en dos pantallas a la vez y ninguna avisaba.
"""
from __future__ import annotations

from app.core.wall_engine import (HYSTERESIS_MARGIN, MAX_DISTANCE_PCT, OI_WEIGHT,
                                  resolve_walls, wall_audit)
from app.terminal_api import wall_consistency

SECCIONES = ("TRACE", "FLUJO", "NET_DRIFT", "DARK_POOL", "RESUMEN", "SCANNER")


def _cadena(n=11, base=500.0):
    exp = [{"strike": base + i, "call_gex": 1e6 * (10 - abs(i - 5)),
            "put_gex": 5e5 * (1 + i % 3)} for i in range(n)]
    oi = [{"strike": base + i, "call_oi": 1000 * (10 - abs(i - 5)),
           "put_oi": 700 * (1 + i % 3)} for i in range(n)]
    return exp, oi


def _auditoria(symbol="QQQ", spot=502.0):
    exp, oi = _cadena()
    w = resolve_walls(symbol, exposure_rows=exp, oi_rows=oi, spot=spot)
    return w, wall_audit(w, {"exposure_by_strike": {"rows": exp}})


# ── 11 · La auditoría del cálculo ────────────────────────────────────────

def test_la_auditoria_publica_todo_lo_que_sostiene_la_decision():
    _w, a = _auditoria()
    for k in ("symbol", "spot", "snapshotTime", "representationMode",
              "expirations", "formula", "sides", "authority"):
        assert k in a, k
    assert a["authority"] == "ITMQ_WALL_ENGINE"
    assert a["representationMode"] == "STRIKE_EXPOSURE_BY_SIDE"


def test_cada_candidato_lleva_sus_numeros_y_su_puesto():
    _w, a = _auditoria()
    fila = a["sides"]["call_wall"]["ranking"][0]
    for k in ("ranking", "strike", "exposure", "exposure_norm", "open_interest",
              "oi_norm", "distance_pct", "within_range", "score",
              "callExposure", "putExposure", "selected"):
        assert k in fila, k
    assert fila["ranking"] == 1
    assert fila["selected"] is True


def test_el_ranking_esta_ordenado_por_puntuacion():
    _w, a = _auditoria()
    scores = [c["score"] for c in a["sides"]["call_wall"]["ranking"]]
    assert scores == sorted(scores, reverse=True)


def test_la_exposicion_cruda_de_call_y_put_viaja_al_lado_del_veredicto():
    """Es lo que permite contrastar la decisión contra el crudo sin recalcular."""
    _w, a = _auditoria()
    fila = a["sides"]["call_wall"]["ranking"][0]
    assert fila["callExposure"] is not None
    assert fila["putExposure"] is not None


def test_la_formula_se_publica_entera_con_sus_constantes():
    _w, a = _auditoria()
    f = a["formula"]
    assert "GEOMÉTRICA" in f
    assert str(OI_WEIGHT) in f
    assert str(MAX_DISTANCE_PCT) in f and str(HYSTERESIS_MARGIN) in f
    assert a["oi_weight"] == OI_WEIGHT


def test_el_muro_de_calls_solo_puede_estar_por_encima_del_precio():
    """Uno por DEBAJO ya fue atravesado: es historia, no resistencia."""
    w, a = _auditoria(spot=505.5)
    for c in a["sides"]["call_wall"]["ranking"]:
        assert c["strike"] > 505.5
    for c in a["sides"]["put_wall"]["ranking"]:
        assert c["strike"] < 505.5


def test_sin_interes_abierto_no_se_multiplica_por_cero():
    """Eso afirmaría que no hay libro, no que no se sabe."""
    exp, _oi = _cadena()
    w = resolve_walls("SPY", exposure_rows=exp, oi_rows=[], spot=502.0)
    a = wall_audit(w, {"exposure_by_strike": {"rows": exp}})
    lado = a["sides"]["call_wall"]
    assert lado["method"] == "exposure-only-no-open-interest"
    assert lado["oi_available"] is False
    assert lado["score"] and lado["score"] > 0


def test_la_auditoria_no_cambia_la_metodologia():
    """Publica los números con los que el motor YA decidía."""
    w, a = _auditoria()
    assert a["sides"]["call_wall"]["selected_strike"] == w["call_wall"]["strike"]
    assert a["sides"]["call_wall"]["score"] == w["call_wall"]["score"]
    assert a["sides"]["put_wall"]["selected_strike"] == w["put_wall"]["strike"]


def test_la_auditoria_viaja_al_trace_y_al_auditor():
    from pathlib import Path
    main = Path("app/main.py").read_text(encoding="utf-8")
    api = Path("app/terminal_api.py").read_text(encoding="utf-8")
    assert 'payload["wall_audit"] = WE.wall_audit(walls, hub)' in main
    assert 'auditor["wall_calculation"]' in api


# ── 12 · Una autoridad, seis lectores ────────────────────────────────────

def _trace(call=510.0, put=495.0, dibujado=None):
    return {
        "walls": {"call_wall": {"strike": call, "source_mode": "PROVIDER_STRIKE_EXPOSURE"},
                  "put_wall": {"strike": put}},
        "levels": [{"kind": "call_wall", "price": call if dibujado is None else dibujado},
                   {"kind": "put_wall", "price": put}],
    }


def test_las_seis_secciones_leen_el_mismo_muro():
    r = wall_consistency(_trace(), {"call_wall": 510.0, "put_wall": 495.0})
    assert r["ok"] is True
    for fila in r["rows"]:
        consumidores = fila["consumers"]
        assert set(consumidores) == set(SECCIONES)
        valores = {v for v in consumidores.values() if v is not None}
        assert len(valores) == 1, f"{fila['side']}: {consumidores}"


def test_call_wall_es_identico_en_las_seis():
    r = wall_consistency(_trace(), {"call_wall": 510.0, "put_wall": 495.0})
    cw = next(f for f in r["rows"] if f["side"] == "call_wall")
    assert all(v == 510.0 for v in cw["consumers"].values())


def test_put_wall_es_identico_en_las_seis():
    r = wall_consistency(_trace(), {"call_wall": 510.0, "put_wall": 495.0})
    pw = next(f for f in r["rows"] if f["side"] == "put_wall")
    assert all(v == 495.0 for v in pw["consumers"].values())


def test_un_centimo_de_diferencia_rompe_la_igualdad():
    """Un muro es un strike, no una estimación: no hay tolerancia que aplicar."""
    r = wall_consistency(_trace(dibujado=510.01), {"call_wall": 510.0, "put_wall": 495.0})
    assert r["ok"] is False


def test_ninguna_seccion_recalcula_su_muro():
    """Todas leen `trace.levels` y `trace.walls`; ninguna llama al motor."""
    from pathlib import Path
    for rel in ("app/static/itmq_trace.js", "app/static/itmq_orderflow.js"):
        js = Path(rel).read_text(encoding="utf-8")
        assert "structural_walls" not in js, rel
        assert "walls_from_hub" not in js, rel


def test_el_muro_de_dark_pool_no_se_confunde_con_el_de_opciones():
    """Miden cosas distintas: dinero cruzado fuera de bolsa frente a exposición
    de opciones. Comparten eje de precio y nada más."""
    from app.core.dark_pool_view import DARK_WALL_KIND
    assert DARK_WALL_KIND == "dark_pool_wall"
    assert DARK_WALL_KIND not in ("call_wall", "put_wall")


def test_los_muros_son_iguales_en_cualquier_activo():
    """Punto 24: ningún ticker toma un camino distinto."""
    formas = set()
    for sym in ("DIA", "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "AMD"):
        _w, a = _auditoria(symbol=sym)
        formas.add((a["representationMode"], a["formula"], a["oi_weight"]))
    assert len(formas) == 1

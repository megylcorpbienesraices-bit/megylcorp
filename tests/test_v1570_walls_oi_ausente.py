"""WALLS v1.57.0 · un OI que falta no es un OI de cero.

`wall_engine` lo tenía escrito en su propia documentación:

    «Cuando el proveedor no publica OI para ese activo NO se penaliza al strike:
     se puntúa sólo con exposición y se declara. Multiplicar por un dato ausente
     es inventar un veredicto.»

La regla existía y se aplicaba al activo ENTERO. Por strike no: la normalización
recibía `p["oi"] or 0.0`, así que un strike sin OI publicado entraba como cero
medido, salía con `oi_norm = 0` y su score se anulaba —es una media geométrica—
por grande que fuera su exposición.

El efecto es el peor posible en un muro: el strike con MÁS cobertura desaparece
como candidato, y la línea que se dibuja en TRACE señala al segundo. No hay
error, no hay excepción, no hay aviso. Sólo un muro en el sitio equivocado.
"""
from __future__ import annotations

import pytest

from app.core import wall_engine as WE


def _cadena(base: float, exposiciones, *, lado: str):
    """Cadena con exposición decreciente a partir de `base`."""
    paso = 1 if lado == WE.CALL_WALL else -1
    campo = "call_gex" if lado == WE.CALL_WALL else "put_gex"
    otro = "put_gex" if lado == WE.CALL_WALL else "call_gex"
    return [{"strike": base + paso * i, campo: g, otro: 0.0}
            for i, g in enumerate(exposiciones)]


def _oi(strikes, *, lado: str, valor: float = 50_000.0):
    campo = "call_oi" if lado == WE.CALL_WALL else "put_oi"
    return [{"strike": k, campo: valor} for k in strikes]


@pytest.mark.parametrize("lado,spot", [(WE.CALL_WALL, 99.0), (WE.PUT_WALL, 101.0)])
def test_el_strike_de_mayor_exposicion_no_se_pierde_por_un_oi_que_falta(lado, spot):
    """El strike del muro es el único sin OI publicado. Antes puntuaba 0,0."""
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8, 7.0e8, 6.0e8], lado=lado)
    paso = 1 if lado == WE.CALL_WALL else -1
    # OI para todos MENOS el del muro.
    oi = _oi([100.0 + paso * i for i in range(1, 5)], lado=lado)

    c = WE.build_candidates("DIA", exp, oi, spot=spot, side=lado)
    fila = next(r for r in c["rows"] if r["strike"] == 100.0)

    assert fila["oi"] is None
    assert fila["oi_missing"] is True
    assert fila["oi_norm"] is None, "un OI ausente no puede publicarse como 0"
    assert fila["score"] > 0, "el strike de mayor exposición fue anulado por un hueco del proveedor"
    assert c["rows"][0]["strike"] == 100.0, (
        f"el muro se fue al strike {c['rows'][0]['strike']} porque al del muro "
        f"le faltaba el OI")
    assert fila["score_method"] == "exposure-only-oi-missing"
    assert c["oi_missing_strikes"] == 1


@pytest.mark.parametrize("lado,spot", [(WE.CALL_WALL, 99.0), (WE.PUT_WALL, 101.0)])
def test_un_oi_medido_a_cero_y_un_oi_ausente_no_se_publican_igual(lado, spot):
    """Son cosas distintas: «no hay libro» y «no se sabe si lo hay»."""
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8, 7.0e8], lado=lado)
    paso = 1 if lado == WE.CALL_WALL else -1
    resto = [100.0 + paso * i for i in range(1, 4)]
    campo = "call_oi" if lado == WE.CALL_WALL else "put_oi"

    medido = [{"strike": 100.0, campo: 0.0}] + _oi(resto, lado=lado)
    ausente = _oi(resto, lado=lado)

    a = next(r for r in WE.build_candidates("DIA", exp, medido, spot=spot, side=lado)["rows"]
             if r["strike"] == 100.0)
    b = next(r for r in WE.build_candidates("DIA", exp, ausente, spot=spot, side=lado)["rows"]
             if r["strike"] == 100.0)

    assert a["oi"] == 0.0 and a["oi_missing"] is False
    assert b["oi"] is None and b["oi_missing"] is True
    assert a["score"] != b["score"], (
        "un cero medido y un hueco del proveedor producen el mismo score")
    assert a["score"] == 0.0, "un cero MEDIDO sí anula el muro, y debe seguir haciéndolo"


def test_con_oi_completo_la_media_geometrica_no_cambia():
    """La corrección no puede alterar el caso normal, que es la inmensa mayoría."""
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8, 7.0e8], lado=WE.CALL_WALL)
    oi = _oi([100.0, 101.0, 102.0, 103.0], lado=WE.CALL_WALL)
    c = WE.build_candidates("DIA", exp, oi, spot=99.0, side=WE.CALL_WALL)
    assert c["oi_missing_strikes"] == 0
    for r in c["rows"]:
        assert r["oi_missing"] is False
        assert r["score_method"] == "geometric-exposure-x-open-interest"
        esperado = round(100.0 * (r["exposure_norm"] ** (1 - WE.OI_WEIGHT))
                         * (r["oi_norm"] ** WE.OI_WEIGHT), 4)
        assert r["score"] == pytest.approx(esperado)


def test_sin_oi_en_toda_la_cadena_se_sigue_puntuando_solo_con_exposicion():
    """El caso que ya estaba bien: no se toca."""
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8, 7.0e8], lado=WE.CALL_WALL)
    c = WE.build_candidates("DIA", exp, [], spot=99.0, side=WE.CALL_WALL)
    assert c["oi_available"] is False
    assert c["method"] == "exposure-only-no-open-interest"
    assert c["rows"][0]["strike"] == 100.0
    assert all(r["oi_norm"] is None for r in c["rows"])


def test_los_ocho_activos_puntuan_con_la_misma_regla():
    """Un muro no puede depender del ticker: no hay ramas por símbolo."""
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8, 7.0e8, 6.0e8], lado=WE.CALL_WALL)
    oi = _oi([101.0, 102.0, 103.0, 104.0], lado=WE.CALL_WALL)   # falta el 100
    vistos = set()
    for sym in ("DIA", "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "MSFT"):
        c = WE.build_candidates(sym, exp, oi, spot=99.0, side=WE.CALL_WALL)
        vistos.add((c["rows"][0]["strike"], c["rows"][0]["score"],
                    c["oi_missing_strikes"]))
    assert len(vistos) == 1, f"el mismo mercado dio {len(vistos)} resultados distintos"
    assert vistos.pop()[0] == 100.0

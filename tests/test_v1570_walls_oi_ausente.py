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


# ═══════════════════════════════════════════════════════════════════════════
# LA DERIVACIÓN PUBLICADA TIENE QUE SER LA QUE SE USÓ
# ═══════════════════════════════════════════════════════════════════════════
#
# Call Wall y Put Wall son DERIVED · ITM QUANT: el proveedor NO los emite. Emite
# exposición por strike e interés abierto por strike, y la conclusión la firma el
# motor. Cuando la conclusión es propia, la única forma de discutirla es que el
# auditor diga con qué fórmula se sacó. Si dice una que no es, el número deja de
# ser auditable aunque sea correcto.


@pytest.mark.parametrize("lado,spot", [(WE.CALL_WALL, 99.0), (WE.PUT_WALL, 101.0)])
def test_el_muro_publica_como_se_puntuo_SU_strike_no_solo_la_cadena(lado, spot):
    """Decía «media geométrica exposición × OI» con `oi: None` justo debajo."""
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8, 7.0e8], lado=lado)
    paso = 1 if lado == WE.CALL_WALL else -1
    oi = _oi([100.0 + paso * i for i in range(1, 4)], lado=lado)   # falta el del muro

    w = WE.resolve_walls("SPY", exposure_rows=exp, oi_rows=oi, spot=spot)
    muro = w[lado]

    assert muro["strike"] == 100.0
    assert muro["oi"] is None
    assert muro["method"] == "exposure-only-oi-missing", (
        f"el auditor declara «{muro['method']}» para un strike sin interés abierto")
    # La cadena sí se puntuó con la media geométrica, y eso también se dice.
    assert muro["chain_method"] == "geometric-exposure-x-open-interest"
    assert muro["oi_missing"] is True
    assert muro["oi_missing_strikes"] == 1


def test_el_auditor_deja_ver_el_hueco_sin_deducirlo_de_un_nulo():
    """«Mire el oi_missing_strikes» no vale si el campo no llega al auditor."""
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8, 7.0e8], lado=WE.CALL_WALL)
    oi = _oi([101.0, 102.0], lado=WE.CALL_WALL)      # faltan 100 y 103
    lado = WE.wall_audit(WE.resolve_walls(
        "DIA", exposure_rows=exp, oi_rows=oi, spot=99.0))["sides"][WE.CALL_WALL]

    assert lado["oi_missing"] is True
    assert lado["oi_missing_strikes"] == 2
    assert lado["method"] == "exposure-only-oi-missing"
    assert lado["chain_method"] == "geometric-exposure-x-open-interest"


def test_con_oi_completo_el_metodo_publicado_es_la_media_geometrica():
    """El caso normal no puede quedar marcado como si le faltara nada."""
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8], lado=WE.CALL_WALL)
    oi = _oi([100.0, 101.0, 102.0], lado=WE.CALL_WALL)
    muro = WE.resolve_walls("DIA", exposure_rows=exp, oi_rows=oi, spot=99.0)[WE.CALL_WALL]

    assert muro["method"] == "geometric-exposure-x-open-interest"
    assert muro["chain_method"] == "geometric-exposure-x-open-interest"
    assert muro["oi_missing"] is False
    assert muro["oi_missing_strikes"] == 0


def test_el_muro_nunca_se_publica_como_dato_del_proveedor():
    """El proveedor no emite muros. Publicarlos como suyos sería atribuirle
    un nivel que no calcula, y el operador no podría saber de quién es."""
    from app.core import data_lineage as DL
    exp = (_cadena(100.0, [1.0e9, 9.0e8], lado=WE.CALL_WALL)
           + _cadena(99.0, [1.0e9, 9.0e8], lado=WE.PUT_WALL))
    oi = _oi([98.0, 99.0, 100.0, 101.0], lado=WE.CALL_WALL)
    w = WE.resolve_walls("DIA", exposure_rows=exp, oi_rows=oi, spot=99.5)

    assert w["source_mode"] == DL.DERIVED
    assert w["authority"] == "ITMQ_WALL_ENGINE"
    for lado in (WE.CALL_WALL, WE.PUT_WALL):
        assert w[lado]["source_mode"] == DL.DERIVED
        assert w[lado]["provider"] == DL.ITM_QUANT
    for nivel in w["levels"]:
        assert nivel["source_mode"] == DL.DERIVED


def test_activo_sin_oi_y_hueco_puntual_no_se_diagnostican_igual():
    """Dos cosas distintas, y una corrección apresurada las llamó igual.

    Que el proveedor no publique interés abierto para un ACTIVO describe su
    cobertura. Que lo publique y le falte UN strike describe un agujero dentro
    de una cadena que por lo demás llegó. Mezclarlos hace que un activo entero
    sin OI se lea como si le faltara un dato suelto.

    Las dos pruebas que ya existían —`test_missing_open_interest_does_not_zero_a
    _strike` y `test_sin_interes_abierto_no_se_multiplica_por_cero`— cazaron
    exactamente esto. Esta las acompaña por el otro lado.
    """
    exp = _cadena(100.0, [1.0e9, 9.0e8, 8.0e8], lado=WE.CALL_WALL)

    # Activo sin OI en absoluto.
    sin = WE.resolve_walls("NOI", exposure_rows=exp, oi_rows=[], spot=99.0)[WE.CALL_WALL]
    assert sin["oi_available"] is False
    assert sin["method"] == "exposure-only-no-open-interest"
    assert sin["oi_missing"] is False, "no falta un dato: no hay cobertura para el activo"
    assert sin["oi_missing_strikes"] == 0

    # Cadena con OI a la que le falta un strike.
    hueco = WE.resolve_walls("DIA", exposure_rows=exp,
                             oi_rows=_oi([101.0, 102.0], lado=WE.CALL_WALL),
                             spot=99.0)[WE.CALL_WALL]
    assert hueco["oi_available"] is True
    assert hueco["method"] == "exposure-only-oi-missing"
    assert hueco["oi_missing"] is True
    assert hueco["oi_missing_strikes"] == 1

    assert sin["method"] != hueco["method"]

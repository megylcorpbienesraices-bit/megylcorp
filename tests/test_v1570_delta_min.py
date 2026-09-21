"""DELTA / MIN · la matemática, cuadrante a cuadrante.

El carril mide cuánta delta tiene que absorber el DEALER cada minuto por lo que
hizo el cliente:

    Dealer_Delta_Shares  = −( Δ_opción × contratos × 100 × Side_cliente )
    Dealer_Delta_Dollars =    Dealer_Delta_Shares × spot

`Side_cliente` responde UNA pregunta —¿compró o vendió la opción?— y nada más.
NO se le aplica encima ningún signo «alcista/bajista»: la delta del put ya es
negativa de fábrica. Quien añada un `if PUT: signo = -1` duplica el signo y los
invierte, que es la familia del `CALL = BUY / PUT = SELL` de siempre.

Por eso la tabla de cuatro cuadrantes es una prueba por fila, no un comentario.
"""
from __future__ import annotations

import pytest

from app.core import delta_flow as DF


# ═══════════════════════════════════════════════════════════════════════════
# 1 · LOS CUATRO CUADRANTES
# ═══════════════════════════════════════════════════════════════════════════

CUADRANTES = [
    # (qué hizo el cliente, Δ contrato, side, Δ dealer esperada, lectura)
    ("compra CALL", 0.45, +1, -450.0, "el dealer queda CORTO de delta"),
    ("compra PUT", -0.38, +1, +380.0, "el dealer queda LARGO de delta"),
    ("vende  CALL", 0.45, -1, +450.0, "el dealer queda LARGO de delta"),
    ("vende  PUT", -0.38, -1, -380.0, "el dealer queda CORTO de delta"),
]


@pytest.mark.parametrize("caso,delta,side,esperado,lectura", CUADRANTES)
def test_los_cuatro_cuadrantes_del_signo(caso, delta, side, esperado, lectura):
    """10 contratos. El signo sale del producto, sin ayuda."""
    got = DF.dealer_delta_shares(delta, 10, side)
    assert got == pytest.approx(esperado), f"{caso}: {lectura}"


def test_el_put_no_lleva_signo_adicional():
    """La trampa exacta: aplicar «bajista» encima de una delta ya negativa.

    Si alguien escribe `if PUT: side = -side`, la compra de put sale con el
    signo cambiado y el carril entero miente en el lado que más importa.
    """
    compra_put = DF.dealer_delta_shares(-0.38, 10, +1)
    assert compra_put > 0, (
        "comprar puts deja al DEALER largo de delta; si sale negativo es que "
        "se ha duplicado el signo del put")
    # Y la simetría: vender el mismo put da exactamente lo contrario.
    assert DF.dealer_delta_shares(-0.38, 10, -1) == pytest.approx(-compra_put)


def test_call_y_put_con_la_misma_operacion_empujan_en_sentidos_opuestos():
    compra_call = DF.dealer_delta_shares(0.45, 10, +1)
    compra_put = DF.dealer_delta_shares(-0.45, 10, +1)
    assert compra_call < 0 < compra_put
    assert compra_call == pytest.approx(-compra_put)


def test_comprar_y_vender_invierten_la_contribucion():
    for delta in (0.60, -0.60, 0.05, -0.99):
        a = DF.dealer_delta_shares(delta, 7, +1)
        b = DF.dealer_delta_shares(delta, 7, -1)
        assert a == pytest.approx(-b), f"delta {delta}: comprar y vender no se invierten"


def test_el_multiplicador_es_cien_acciones_por_contrato():
    assert DF.CONTRACT_MULTIPLIER == 100.0
    assert DF.dealer_delta_shares(1.0, 1, +1) == pytest.approx(-100.0)


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LO AMBIGUO NO SE FUERZA
# ═══════════════════════════════════════════════════════════════════════════

def test_una_operacion_sin_lado_no_entra_con_signo_inventado():
    assert DF.dealer_delta_shares(0.45, 10, 0) is None


@pytest.mark.parametrize("falta,motivo", [
    ({"greeks": {"delta": None}}, DF.SIN_DELTA),
    ({"size": None}, DF.SIN_TAMANO),
    ({"direction": 0}, DF.SIN_LADO),
])
def test_cada_hueco_se_declara_con_su_motivo(falta, motivo):
    row = {"t": "2026-09-21T14:30:00Z", "option_type": "CALL", "size": 10,
           "direction": 1, "spot": 500.0, "greeks": {"delta": 0.45}}
    row.update(falta)
    v = DF.trade_delta_flow(row)
    assert v["counted"] is False
    assert v["reason"] == motivo
    assert v["dealer_delta_shares"] is None or v["reason"] != DF.CONTADA


def test_un_mid_market_no_contamina_la_suma():
    """Es el caso real: el print existe y su lado NO es demostrable."""
    base = dict(t="2026-09-21T14:30:00Z", option_type="CALL", size=10,
                spot=500.0, greeks={"delta": 0.50})
    claros = [{**base, "direction": 1} for _ in range(3)]
    ambiguo = [{**base, "direction": 0} for _ in range(7)]
    d = DF.build_delta_flow(claros + ambiguo, symbol="SPY")
    # 3 compras de call: -0.50*10*100 * 3 = -1500 acciones
    assert d["series"][0]["dealer_delta_shares"] == pytest.approx(-1500.0)
    assert d["coverage"]["counted"] == 3
    assert d["coverage"]["total"] == 10
    assert d["coverage"]["pct"] == pytest.approx(30.0)
    assert d["coverage"]["by_reason"][DF.SIN_LADO] == 7


def test_la_cobertura_se_publica_siempre():
    """Con 40 % sin clasificar, el carril enseña el 60 % y parece tranquilo.

    Sin la cifra de cobertura el panel mentiría por omisión.
    """
    d = DF.build_delta_flow([], symbol="DIA")
    assert "coverage" in d and d["coverage"]["total"] == 0
    assert d["ready"] is False


# ═══════════════════════════════════════════════════════════════════════════
# 3 · MINUTO A MINUTO
# ═══════════════════════════════════════════════════════════════════════════

def _print(minuto, *, delta=0.50, size=10, side=1, tipo="CALL", spot=500.0, seg=0):
    return {"t": f"2026-09-21T14:{minuto:02d}:{seg:02d}Z", "option_type": tipo,
            "size": size, "direction": side, "spot": spot,
            "greeks": {"delta": delta}}


def test_el_calculo_cambia_minuto_a_minuto():
    rows = [_print(30), _print(30, seg=30),          # dos en el minuto 30
            _print(31, size=5),                       # uno en el 31
            _print(33, side=-1)]                      # una venta en el 33
    d = DF.build_delta_flow(rows, symbol="SPY")
    s = {r["t"][11:16]: r for r in d["series"]}
    assert set(s) == {"14:30", "14:31", "14:33"}, "los minutos vacíos no se rellenan"
    assert s["14:30"]["dealer_delta_shares"] == pytest.approx(-1000.0)
    assert s["14:30"]["trades"] == 2
    assert s["14:31"]["dealer_delta_shares"] == pytest.approx(-250.0)
    assert s["14:33"]["dealer_delta_shares"] == pytest.approx(+500.0)


def test_un_minuto_sin_operaciones_no_aparece_como_cero():
    """Un minuto sin flujo y un minuto con flujo neto nulo son cosas distintas."""
    d = DF.build_delta_flow([_print(30), _print(35)], symbol="SPY")
    assert [r["t"][11:16] for r in d["series"]] == ["14:30", "14:35"]


def test_calls_y_puts_del_mismo_minuto_se_desglosan_y_se_suman():
    rows = [_print(30, tipo="CALL", delta=0.50, size=10, side=1),
            _print(30, tipo="PUT", delta=-0.40, size=10, side=1, seg=10)]
    r = DF.build_delta_flow(rows, symbol="SPY")["series"][0]
    assert r["dealer_delta_shares_call"] == pytest.approx(-500.0)
    assert r["dealer_delta_shares_put"] == pytest.approx(+400.0)
    assert r["dealer_delta_shares"] == pytest.approx(-100.0)


# ═══════════════════════════════════════════════════════════════════════════
# 4 · LAS DOS UNIDADES
# ═══════════════════════════════════════════════════════════════════════════

def test_los_dolares_son_las_acciones_por_el_spot():
    r = DF.build_delta_flow([_print(30, delta=0.50, size=10, spot=500.0)],
                            symbol="SPY")["series"][0]
    assert r["dealer_delta_shares"] == pytest.approx(-500.0)
    assert r["dealer_delta_dollars"] == pytest.approx(-250_000.0)
    assert r["dealer_delta_dollars"] == pytest.approx(r["dealer_delta_shares"] * 500.0)


def test_sin_spot_no_se_inventa_la_conversion_pero_las_acciones_siguen():
    """Rellenar con el spot de otro instante sería inventar el precio."""
    row = _print(30); row.pop("spot")
    r = DF.build_delta_flow([row], symbol="SPY")["series"][0]
    assert r["dealer_delta_shares"] == pytest.approx(-500.0)
    assert r["dealer_delta_dollars"] is None


def test_el_spot_de_cada_print_manda_sobre_el_de_respaldo():
    row = _print(30, spot=500.0)
    v = DF.trade_delta_flow(row, spot_fallback=999.0)
    assert v["spot"] == pytest.approx(500.0)
    row.pop("spot")
    assert DF.trade_delta_flow(row, spot_fallback=999.0)["spot"] == pytest.approx(999.0)


# ═══════════════════════════════════════════════════════════════════════════
# 5 · FLUJO CONTRA STOCK  ·  la identidad contable
# ═══════════════════════════════════════════════════════════════════════════

def test_la_suma_del_flujo_reproduce_el_cambio_del_stock():
    """Interval Map DELTA es un STOCK (exposición abierta). Esto es un FLUJO.

    La integral del flujo tiene que seguir al cambio del stock. Si divergen,
    uno de los dos está mal. Es la validación más fuerte que admite el carril.
    """
    rows = [_print(30, delta=0.50, size=10, side=1),
            _print(31, delta=-0.30, size=20, side=1),
            _print(32, delta=0.25, size=40, side=-1),
            _print(33, delta=0.60, size=5, side=1)]
    d = DF.build_delta_flow(rows, symbol="SPY")

    # Stock reconstruido a mano, operación a operación.
    stock = 0.0
    for r in rows:
        stock += -(r["greeks"]["delta"] * r["size"] * 100.0 * r["direction"])

    assert d["total_dealer_delta_shares"] == pytest.approx(stock)
    assert sum(s["dealer_delta_shares"] for s in d["series"]) == pytest.approx(stock)


def test_el_total_en_dolares_solo_existe_si_todos_los_minutos_lo_tienen():
    sin_spot = _print(31); sin_spot.pop("spot")
    d = DF.build_delta_flow([_print(30), sin_spot], symbol="SPY")
    assert d["total_dealer_delta_shares"] is not None
    assert d["total_dealer_delta_dollars"] is None, (
        "sumar los minutos que sí tienen spot daría un total que no es el total")


# ═══════════════════════════════════════════════════════════════════════════
# 6 · GLOBAL, Y CON SU PROCEDENCIA
# ═══════════════════════════════════════════════════════════════════════════

def test_funciona_igual_en_todos_los_activos():
    """La fórmula no puede depender del ticker."""
    rows = [_print(30), _print(31, side=-1)]
    vistos = set()
    for sym in ("DIA", "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "MSFT", "GLD"):
        d = DF.build_delta_flow(rows, symbol=sym)
        vistos.add((d["total_dealer_delta_shares"], d["buckets"], d["coverage"]["pct"]))
        assert d["symbol"] == sym
    assert len(vistos) == 1, f"el mismo flujo dio {len(vistos)} resultados distintos"


def test_declara_convenio_procedencia_y_metodo():
    d = DF.build_delta_flow([_print(30)], symbol="SPY")
    assert d["convention"] == "DEALER"
    assert d["source_mode"] == "DERIVED"
    assert d["authority"] == "ITMQ_DELTA_FLOW"
    assert "side_cliente" in d["method"] and "x 100" in d["method"]
    assert "QD_OPTION_FLOW.delta" in d["inputs"]
    assert "ITMQ_AGGRESSOR.direction" in d["inputs"]


def test_no_reimplementa_la_clasificacion_del_agresor():
    """Dos clasificadores producen dos verdades. Sólo puede haber una."""
    from pathlib import Path
    src = Path("app/core/delta_flow.py").read_text(encoding="utf-8")
    assert "from .aggressor import from_row" in src
    for inventado in ("tradeSideCode", "ABOVE_ASK", "BELOW_BID", "nbbo"):
        assert inventado not in src, f"{inventado}: eso se resuelve en aggressor.py"


def test_el_volumen_simple_no_se_usa_como_sustituto():
    from pathlib import Path
    src = Path("app/core/delta_flow.py").read_text(encoding="utf-8")
    for prohibido in ("net_call_volume", "net_put_volume"):
        assert prohibido not in src, "el volumen no es delta"


# ═══════════════════════════════════════════════════════════════════════════
# 7 · CABLEADO · de la cinta a la pantalla
# ═══════════════════════════════════════════════════════════════════════════

def test_el_bloque_sale_del_bundle_calculado_desde_la_cinta():
    from app.terminal_api import _delta_min_block
    rows = [_print(30, tipo="CALL", delta=0.50, size=10, side=1),
            _print(31, tipo="PUT", delta=-0.40, size=20, side=1)]
    d = _delta_min_block("SPY", rows, {"spot": 500.0})
    assert d["ready"] is True and d["buckets"] == 2
    assert d["series"][0]["dealer_delta_dollars"] == pytest.approx(-250_000.0)
    assert d["series"][1]["dealer_delta_dollars"] == pytest.approx(+400_000.0)
    assert d["coverage"]["pct"] == pytest.approx(100.0)


def test_el_bloque_viaja_en_la_seccion_de_flujo_aparte_de_net_drift():
    from pathlib import Path
    src = Path("app/terminal_api.py").read_text(encoding="utf-8")
    assert '"delta_min": delta_min,' in src
    assert "delta_min = _delta_min_block(symbol, tape_rows, state)" in src, (
        "tiene que calcularse desde la CINTA, no desde Net Drift")


def test_la_interfaz_ensena_dolares_y_deja_lo_tecnico_al_auditor():
    from pathlib import Path
    js = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("function drawDeltaMin("):js.index("function pickDriftAt(")]
    assert "dealer_delta_dollars" in cuerpo, "la pantalla principal va en dólares"
    assert "dealer_delta_shares" not in cuerpo, "las acciones son para el Auditor"
    assert "coverage" not in cuerpo, "la cobertura es para el Auditor"
    assert "'DELTA / MIN'" in cuerpo, "la etiqueta, limpia"
    assert "DERIVED" not in cuerpo


def test_el_carril_tiene_su_sitio_y_su_entrada_de_datos():
    from pathlib import Path
    js = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    assert "S.panels.deltaMin = ids.deltaMin" in js
    assert "function applyDeltaMin(" in js
    assert "applyDeltaMin," in js, "sin exportar no lo alimenta nadie"
    app = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    assert "Flow.applyDeltaMin(d.delta_min)" in app
    assert "deltaMin: el('ofDeltaMin')" in app
    html = Path("app/templates/terminal.html").read_text(encoding="utf-8")
    assert 'id="ofDeltaMin"' in html


def test_un_minuto_sin_dolares_no_se_dibuja_como_cero():
    from pathlib import Path
    js = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("function drawDeltaMin("):js.index("function pickDriftAt(")]
    assert "if (!Q.isNum(v)) continue;" in cuerpo


def test_el_eje_es_simetrico_para_no_exagerar_un_lado():
    from pathlib import Path
    js = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("function drawDeltaMin("):js.index("function pickDriftAt(")]
    assert "Q.scale(-mag * 1.15, mag * 1.15" in cuerpo


# ═══════════════════════════════════════════════════════════════════════════
# 8 · CINTA REALISTA · el caso que se va a ver en vivo
# ═══════════════════════════════════════════════════════════════════════════

def _cinta_realista():
    """Sesión con las cuatro operaciones, un MID_MARKET y un print sin delta."""
    return [
        # 14:30 — el cliente compra calls con fuerza
        _print(30, tipo="CALL", delta=0.55, size=40, side=1, spot=520.0),
        _print(30, tipo="CALL", delta=0.48, size=25, side=1, spot=520.0, seg=20),
        # …y alguien vende puts en el mismo minuto
        _print(30, tipo="PUT", delta=-0.31, size=30, side=-1, spot=520.0, seg=40),
        # 14:31 — MID_MARKET: existe, no se puede clasificar, no contamina
        {**_print(31, tipo="CALL", delta=0.50, size=100, spot=520.5), "direction": 0},
        # 14:32 — print sin delta del proveedor
        {**_print(32, tipo="PUT", size=10, side=1, spot=520.5), "greeks": {"delta": None}},
        # 14:33 — venta de calls
        _print(33, tipo="CALL", delta=0.62, size=15, side=-1, spot=521.0),
    ]


def test_cinta_realista_cuadra_operacion_a_operacion():
    d = DF.build_delta_flow(_cinta_realista(), symbol="SPY")
    s = {r["t"][11:16]: r for r in d["series"]}

    # 14:30 — a mano: -(0.55*40*100) -(0.48*25*100) -(-0.31*30*100*-1)
    esperado = -(0.55 * 40 * 100) - (0.48 * 25 * 100) - (-0.31 * 30 * 100 * -1)
    assert s["14:30"]["dealer_delta_shares"] == pytest.approx(esperado)
    assert s["14:30"]["dealer_delta_shares"] < 0, "compra de calls deja al dealer corto"
    assert s["14:30"]["trades"] == 3

    # 14:31 y 14:32 no existen: uno ambiguo y otro sin delta.
    assert "14:31" not in s and "14:32" not in s

    # 14:33 — venta de calls: el dealer queda largo.
    assert s["14:33"]["dealer_delta_shares"] == pytest.approx(0.62 * 15 * 100)
    assert s["14:33"]["dealer_delta_shares"] > 0

    # Cobertura honesta: 4 de 6.
    assert d["coverage"]["counted"] == 4 and d["coverage"]["total"] == 6
    assert d["coverage"]["by_reason"][DF.SIN_LADO] == 1
    assert d["coverage"]["by_reason"][DF.SIN_DELTA] == 1


def test_cinta_realista_los_dolares_usan_el_spot_de_cada_print():
    d = DF.build_delta_flow(_cinta_realista(), symbol="SPY")
    s = {r["t"][11:16]: r for r in d["series"]}
    assert s["14:30"]["dealer_delta_dollars"] == pytest.approx(
        s["14:30"]["dealer_delta_shares"] * 520.0)
    assert s["14:33"]["dealer_delta_dollars"] == pytest.approx(
        s["14:33"]["dealer_delta_shares"] * 521.0)


def test_el_orden_de_llegada_no_altera_el_resultado():
    """La cinta no siempre llega ordenada."""
    rows = _cinta_realista()
    a = DF.build_delta_flow(rows, symbol="SPY")
    b = DF.build_delta_flow(list(reversed(rows)), symbol="SPY")
    assert a["total_dealer_delta_shares"] == pytest.approx(b["total_dealer_delta_shares"])
    assert [r["t"] for r in a["series"]] == [r["t"] for r in b["series"]]


# ═══════════════════════════════════════════════════════════════════════════
# 9 · EL AUDITOR VE LO QUE LA PANTALLA NO ENSEÑA
# ═══════════════════════════════════════════════════════════════════════════

def test_el_auditor_publica_cobertura_acciones_y_metodo():
    """Si el 40 % no se clasifica, el gráfico enseña el 60 % y parece tranquilo.

    La pantalla va limpia a propósito; la cifra que impide que mienta por
    omisión tiene que estar en el Auditor.
    """
    from pathlib import Path
    src = Path("app/terminal_api.py").read_text(encoding="utf-8")
    bloque = src[src.index('auditor["delta_min"] = {'):]
    bloque = bloque[:bloque.index("\n        }\n") + 10]
    for campo in ("coverage_pct", "trades_total", "trades_counted",
                  "excluded_by_reason", "total_dealer_delta_shares",
                  "method", "convention", "source_mode", "authority"):
        assert f'"{campo}"' in bloque, f"al Auditor le falta {campo}"


def test_la_seccion_de_flujo_se_calcula_una_sola_vez():
    """Dos llamadas darían dos verdades sobre el mismo ciclo."""
    from pathlib import Path
    src = Path("app/terminal_api.py").read_text(encoding="utf-8")
    assert src.count("_flujo_ordenes(state, intel, trace)") == 1
    assert '"flujo_ordenes": flujo,' in src
    assert src.index("flujo = _flujo_ordenes(") < src.index('auditor["delta_min"]')

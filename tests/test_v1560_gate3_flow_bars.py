"""GATE 3 · Las barras que faltaban. Puntos 45 a 49.

EL SÍNTOMA
----------
En pantalla convivían:

    VOLUMEN SUBYACENTE   barras llenas
    AGRESOR              SIN FLUJO DIRECCIONAL
    PRIMA                SIN PRIMA OBSERVADA

LA CAUSA
--------
Los tres carriles se alimentaban de sitios distintos. El volumen sale de las
VELAS; el agresor y la prima salían de `trace.option_prints`, en el cliente. Ese
campo llegaba vacío aunque `order-flow` hubiera devuelto cientos de operaciones,
y entonces los dos carriles de opciones se vaciaban mientras el de velas seguía
lleno.

En pantalla eso se lee como «hoy no hubo flujo de opciones», que es una
conclusión sobre el mercado. Lo que había era una ruta de datos rota.

LA CORRECCIÓN
-------------
Las barras se agrupan en el servidor, desde la MISMA cinta que alimenta las
tarjetas y QFLOW, y entran en el LKG por carril: un ciclo vacío deja de
borrarlas. El recuento de la cadena se publica para que un fallo de integración
se pueda señalar en su etapa exacta en vez de instrumentarlo todo otra vez.
"""
from __future__ import annotations

from pathlib import Path

from app.core import flow_view as FV
from app.core.flow_view import BUCKET_MS, bucketize

FLOW_JS = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")


def setup_function():
    FV.reset()


ROWS = [
    {"t": "2026-09-19T14:00:05Z", "premium": 10_000.0, "size": 10, "aggressor": "BUY"},
    {"t": "2026-09-19T14:00:40Z", "premium": 4_000.0, "size": 4, "aggressor": "SELL"},
    {"t": "2026-09-19T14:01:10Z", "premium": 2_000.0, "size": 2, "aggressor": "UNKNOWN"},
]


def _model(rows=ROWS, symbol="QQQ", provider=None):
    return FV.build(symbol=symbol, session_date="2026-09-19", market_open=True,
                    tape={"buckets": rows,
                          "provider_count": len(rows or []) if provider is None else provider},
                    net_flow={"series": None}, qflow={"markers": None})


# ── 46 · Barras direccionales por intervalo ──────────────────────────────

def test_cada_bucket_lleva_los_tres_lados_por_separado():
    b = bucketize(ROWS)
    assert len(b) == 2
    assert b[0]["buy_premium"] == 10_000.0
    assert b[0]["sell_premium"] == 4_000.0
    assert b[1]["unknown_premium"] == 2_000.0


def test_el_lado_sin_clasificar_no_se_reparte():
    """Repartirlo inclinaría el sesgo hacia el lado que tocara por azar justo
    cuando la cinta llega sin cotización, que es cuando peor se lee."""
    b = bucketize([{"t": "2026-09-19T14:00:00Z", "premium": 9_000.0, "size": 9,
                    "aggressor": "UNKNOWN"}])[0]
    assert b["buy_premium"] == 0.0 and b["sell_premium"] == 0.0
    assert b["unknown_premium"] == 9_000.0
    assert b["net_premium"] == 0.0
    assert b["coverage_pct"] == 0.0


def test_el_bucket_lleva_prima_y_volumen_por_lado():
    b = bucketize(ROWS)[0]
    for k in ("buy_premium", "sell_premium", "unknown_premium", "net_premium",
              "buy_volume", "sell_volume", "unknown_volume", "net_volume",
              "total_premium", "classified_premium", "coverage_pct",
              "trades", "buys", "sells", "unknowns"):
        assert k in b, k
    assert b["net_volume"] == 6.0


def test_el_eje_es_el_instante_no_el_indice():
    """Colocar por índice de array pone la barra en el minuto de al lado."""
    b = bucketize(ROWS)
    assert b[0]["t"] % BUCKET_MS == 0
    assert b[1]["t"] - b[0]["t"] == BUCKET_MS
    assert b[0]["t_iso"].startswith("2026-09-19T14:00:00")


def test_un_ciclo_sin_filas_no_fabrica_barras():
    assert bucketize([]) == [] and bucketize(None) == []


# ── 47 · LKG: el histórico no se borra ───────────────────────────────────

def test_un_ciclo_vacio_conserva_las_barras_anteriores():
    _model()
    vacio = _model(rows=None, provider=0)
    assert len(vacio["aggressor_bars"]["current"] or []) == 2
    assert len(vacio["premium_bars"]["current"] or []) == 2


def test_sin_dato_y_sin_historico_si_se_dice_que_no_hay():
    """`SIN PRIMA OBSERVADA` sólo es válido aquí."""
    vacio = _model(rows=None, provider=0, symbol="NUEVO")
    assert vacio["premium_bars"]["current"] is None
    assert vacio["premium_bars"]["status"] == FV.NO_DATA


def test_el_lkg_de_las_barras_no_cruza_de_un_activo_a_otro():
    _model(symbol="DIA")
    otro = _model(rows=None, provider=0, symbol="QQQ")
    assert otro["aggressor_bars"]["current"] is None


# ── 45 · Recuento de la cadena ───────────────────────────────────────────

def test_la_cadena_publica_su_recuento_etapa_por_etapa():
    p = _model()["pipeline"]
    for k in ("provider_count", "normalized_count", "hub_bucket_count",
              "viewmodel_bucket_count", "rendered_bar_count"):
        assert k in p, k
    assert p["provider_count"] == 3
    assert p["viewmodel_bucket_count"] == 2
    assert p["ok"] is True


def test_filas_del_proveedor_y_cero_barras_es_un_fallo_de_integracion():
    """Si el proveedor trae filas y no sale ninguna barra, el fallo es nuestro
    y tiene que poder señalarse sin instrumentar la cadena entera."""
    p = FV.build(symbol="SPY", session_date="2026-09-19", market_open=True,
                 tape={"buckets": [], "provider_count": 7},
                 net_flow={}, qflow={})["pipeline"]
    assert p["ok"] is False
    assert "fallo de integración" in p["detail"]


def test_sin_filas_del_proveedor_no_se_denuncia_nada():
    """Cero barras con cero filas no es un fallo: es un ciclo sin operaciones."""
    p = _model(rows=None, provider=0)["pipeline"]
    assert p["ok"] is True
    assert "sin filas del proveedor" in p["detail"]


def test_el_cliente_publica_cuantas_barras_dibuja_de_verdad():
    assert "function renderedBarCount()" in FLOW_JS
    assert "renderedBarCount," in FLOW_JS[FLOW_JS.index("global.ITMQFlow ="):]


# ── 49 · El modelo es la única fuente de esas barras ─────────────────────

def test_las_barras_de_opciones_salen_del_modelo():
    cuerpo = FLOW_JS[FLOW_JS.index("function rebuild()"):FLOW_JS.index("function effectiveMinPremium()")]
    assert "S.flowView.aggressor_bars" in cuerpo
    assert "r.buy_premium" in cuerpo and "r.sell_premium" in cuerpo


def test_el_modelo_al_llegar_reconstruye_las_barras():
    """Si sólo se repintaran las tarjetas, los carriles seguirían enseñando lo
    que hubiera calculado la cinta local, que es la ruta que se sustituye."""
    cuerpo = FLOW_JS[FLOW_JS.index("function applyFlowView(vm)"):FLOW_JS.index("function renderedBarCount()")]
    assert "rebuild();" in cuerpo


def test_la_cinta_local_sigue_de_respaldo_si_el_modelo_no_viaja():
    cuerpo = FLOW_JS[FLOW_JS.index("function rebuild()"):FLOW_JS.index("function effectiveMinPremium()")]
    assert "} else {" in cuerpo and "for (const p of S.prints)" in cuerpo


def test_los_prints_individuales_no_pueden_vaciar_la_barra():
    """Son el DETALLE de un intervalo, no la barra."""
    cuerpo = FLOW_JS[FLOW_JS.index("function rebuild()"):FLOW_JS.index("function effectiveMinPremium()")]
    bloque = cuerpo[cuerpo.index("if (barras) {"):cuerpo.index("} else {")]
    assert "b.prints.push(p)" in bloque
    assert "b.total +=" not in bloque.split("for (const p of S.prints)")[1]


# ── 48 · Tres datasets, tres nombres ─────────────────────────────────────

def test_el_volumen_del_subyacente_no_se_confunde_con_el_flujo_de_opciones():
    assert "'VOLUMEN SUBYACENTE · SIP · COLOR = SIGNO DE CINTA'" in FLOW_JS
    assert "'AGRESOR · OPCIONES'" in FLOW_JS
    assert "'PRIMA · OPCIONES'" in FLOW_JS


def test_el_vacio_de_un_carril_dice_el_motivo_real():
    """«SIN PRIMA OBSERVADA» es una conclusión: sólo vale sin dato y sin LKG."""
    assert "function laneEmpty(dataset, porDefecto)" in FLOW_JS
    cuerpo = FLOW_JS[FLOW_JS.index("function laneEmpty(dataset, porDefecto)"):FLOW_JS.index("function rebuild()")]
    assert "lane.status === 'NO_DATA'" in cuerpo
    assert "lane.screen_note" in cuerpo
    assert "laneEmpty('premium_bars'" in FLOW_JS
    assert "laneEmpty('aggressor_bars'" in FLOW_JS


def test_el_carril_de_volumen_sigue_siendo_de_las_velas():
    """Un carril puede tener dato aunque otro no: son datasets distintos."""
    cuerpo = FLOW_JS[FLOW_JS.index("function rebuild()"):FLOW_JS.index("function effectiveMinPremium()")]
    assert "for (const c of S.candles)" in cuerpo
    assert "b.underlyingNet += Q.num(c.sv, 0)" in cuerpo

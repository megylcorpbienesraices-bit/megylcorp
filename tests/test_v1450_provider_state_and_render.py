"""CERTIFICACIÓN v1.45.0 · Estado real del proveedor y legibilidad multi-activo.

Los defectos que cierra esta release comparten una forma: **un dato ausente
convertido en una afirmación**. El 400 que no decía qué campo faltaba, el flag de
dark pool que por defecto era «no», la barra de un píxel que decía «casi cero», el
mapa vacío que decía «sin estructura», y la etiqueta que decía «contraste» de lo
que era autoridad. Ninguno fallaba de forma ruidosa; todos mentían en silencio.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


# ═══════════════════════════════════════════ 1 · EL 400 DICE QUÉ FALTA

def test_a_validation_error_keeps_the_field_the_provider_named():
    """«Request validation failed» a secas no se puede accionar.

    El campo que falta venía en la respuesta y el cliente lo tiraba: leía sólo
    `detail`/`title` y lo recortaba a 180 caracteres.
    """
    from app.providers.quantdata.client import _describe_error

    class R:
        def __init__(self, body): self._b = body; self.text = str(body)
        def json(self): return self._b

    # Forma FastAPI/Pydantic
    msg, fields = _describe_error(R({"detail": "Request validation failed",
                                     "errors": [{"loc": ["body", "lookBackPeriod"],
                                                 "msg": "field required"}]}))
    assert "lookBackPeriod" in msg and "field required" in msg
    assert fields == ["body.lookBackPeriod: field required"]

    # `detail` como lista, sin titular
    msg2, f2 = _describe_error(R({"detail": [{"loc": ["body", "limit"],
                                              "msg": "value is not a valid integer"}]}))
    assert "limit" in msg2 and f2

    # Otra convención: field/message
    msg3, f3 = _describe_error(R({"message": "Bad request", "validationErrors": [
        {"field": "aggregationPeriod", "message": "must be one of 1m,5m"}]}))
    assert "aggregationPeriod" in msg3 and f3

    # El titular no se duplica como si fuera un error de campo.
    msg4, f4 = _describe_error(R({"detail": "Request validation failed"}))
    assert msg4 == "Request validation failed" and f4 == []


def test_a_rejected_body_is_repairable_while_a_missing_route_is_not():
    """v1.46.0 · 400 y 422 dejaron de ser lo mismo, y no lo eran.

    Esta prueba afirmaba que un 422 también era «cuerpo reparable». Es falso y
    tenía consecuencias: un 422 significa que la petición ERA VÁLIDA y el
    proveedor no tiene datos, así que tratarlo como cuerpo defectuoso hacía que
    el programa corrigiera un payload correcto y marcara como averiada una
    herramienta sana. Cinco códigos, cinco tratamientos.
    """
    from app.providers.quantdata.tools import (
        is_validation_error, classify_provider_failure,
        STATUS_REQUEST_INVALID, STATUS_NO_DATA, STATUS_MISSING_TOOL,
        STATUS_PROVIDER_ERROR, STATUS_TRANSIENT)
    from app.providers.quantdata.client import QuantDataError

    def err(status):
        e = QuantDataError(f"HTTP {status}"); e.status_code = status
        return e

    assert classify_provider_failure(err(400)) == STATUS_REQUEST_INVALID
    assert classify_provider_failure(err(422)) == STATUS_NO_DATA
    assert classify_provider_failure(err(404)) == STATUS_MISSING_TOOL
    assert classify_provider_failure(err(503)) == STATUS_PROVIDER_ERROR
    assert classify_provider_failure(TimeoutError("agotado")) == STATUS_TRANSIENT

    assert is_validation_error(err(400)), "sólo el 400 dice que el cuerpo está mal"
    assert not is_validation_error(err(422)), (
        "un 422 es una petición VÁLIDA sin datos: repararla corrompe un cuerpo correcto")
    assert not is_validation_error(err(404)), "un 404 no es un cuerpo reparable"


def test_the_body_is_repaired_once_and_then_remembered():
    """Descubrir el contrato en cada ciclo sería pagar la búsqueda una y otra vez.

    v1.46.0 · Lo que cambió es CÓMO se descubre. Antes se probaban formas
    candidatas hasta que una entraba, que es adivinar con más pasos; ahora se lee
    el campo que el proveedor nombra en el 400 y se corrige ése.
    """
    from app.providers.quantdata.intelligence import QuantDataIntelligence
    from app.providers.quantdata.client import QuantDataError, QuantDataResponse
    from app.core.data_hub_runtime import HUB_RUNTIME

    class FakeClient:
        def __init__(self): self.seen = []
        async def post(self, path, body, *, timeout=None):
            self.seen.append(dict(body))
            if "lookBackPeriod" not in body:
                e = QuantDataError("Quant Data HTTP 400: Request validation failed")
                e.status_code = 400
                e.validation_fields = ["body.lookBackPeriod: field required"]
                raise e
            return QuantDataResponse(payload={"levels": [
                {"price": 601.2, "notional": 4.1e7, "shares": 180000, "prints": 12}]},
                status_code=200)

    async def go():
        HUB_RUNTIME.reset()
        qi = QuantDataIntelligence(); qi.client = FakeClient(); qi._symbol = "SPY"
        tool = qi.catalog["dark_pool_levels"]
        await qi._fetch(tool)
        first = len(qi.client.seen)
        data = qi.get("dark_pool_levels")
        qi.client.seen.clear(); HUB_RUNTIME.reset()
        await qi._fetch(tool)
        return first, dict(tool.repair_added), list(tool.repair_log), data, len(qi.client.seen)

    first, added, log, data, second = asyncio.run(go())
    # v1.46.0 · La corrección ya no se busca probando formas candidatas: se LEE
    # del propio 400. El proveedor nombró `aggregationPeriod`, así que ése y sólo
    # ése es el campo que se añade, y queda escrito de dónde salió.
    assert first == 2, ("debe bastar una corrección: la primera petición y la "
                        "corregida, sin variantes intermedias adivinadas")
    # v1.49.0 · `aggregationPeriod` es legítimo en `dark-flow` y lo RECHAZA
    # `dark-pool-levels`, así que la reparación ya no puede añadírselo: la
    # prohibición es de la herramienta, no del catálogo. Lo que se comprueba
    # aquí sigue siendo lo mismo —una sola corrección, y recordada—, con el
    # campo que este endpoint sí admite.
    assert added == {"lookBackPeriod": 1}, added
    assert log and "lookBackPeriod" in log[0]
    assert data.get("ready") is True and data.get("count") == 1
    assert second == 1, "el contrato descubierto no se recordó"


def test_the_channel_isolator_carries_the_original_exception():
    """Sin la excepción, el clasificador no puede distinguir 400 de 404."""
    from app.core.data_hub_runtime import DataHubRuntime

    async def go():
        rt = DataHubRuntime()

        async def boom():
            raise ValueError("marcador")

        return await rt.fetch("x", "SPY", boom, accept_stale=False)

    res = asyncio.run(go())
    assert isinstance(res.get("exception"), ValueError)


def test_the_diagnosis_tells_a_rejected_body_from_a_missing_route():
    from app.providers.quantdata.tools import build_catalog, route_diagnostic
    t = build_catalog()["dark_pool_levels"]
    t.note_validation_failure("body.limit: field required")
    t.variants_tried = 3
    d = route_diagnostic(t)
    assert d["state"] == "BODY_REJECTED"
    assert "body.limit" in d["detail"] and "3 formas" in d["detail"]


# ═══════════════════════════════════════════ 2 · LA ETIQUETA DE AUTORIDAD

@pytest.mark.parametrize("channel", ["EXPOSURE", "OPEN_INTEREST",
                                     "IMPLIED_VOLATILITY", "OPTION_FLOW", "DARK_POOL"])
def test_quant_data_is_the_authority_of_the_channels_it_owns(channel):
    """v1.43.0 pasó estas métricas a QUANTDATA y un SEGUNDO mapa no se movió."""
    from app.core.provider_parity import channel_authority, _qd_is_authority
    assert channel_authority(channel) == "QUANTDATA"
    assert _qd_is_authority(channel) is True


def test_the_channel_authority_is_derived_never_declared_twice():
    """Tener la autoridad escrita en dos sitios garantiza que un día discrepen."""
    src = text("app/core/provider_parity.py")
    assert "_NATIVE_CHANNEL_AUTHORITY" not in src, "volvió el segundo mapa a mano"
    assert "from .metric_authority import policy" in src
    assert "def channel_authority(" in src


def test_the_screen_calls_it_primary_authority_not_contrast():
    js = text("app/static/itmq_app.js")
    assert "AUTORIDAD PRIMARIA" in js
    assert "authority_is_quantdata" in js
    # CONTRASTE ACTIVO sigue existiendo para los canales donde QD SÍ es contraste.
    assert "CONTRASTE ACTIVO" in js


def test_open_interest_does_not_fall_back_to_alpaca_silently():
    from app.core.metric_authority import policy
    p = policy("open_interest")
    assert p.authority == "QUANTDATA"
    assert "ALPACA" in p.validation
    assert p.fallback == "DEGRADED", "un respaldo silencioso no es un respaldo"


def test_the_two_registries_agree_about_dark_pool_prints():
    """`metric_authority` y `data_lineage` se contradecían sobre el mismo dato."""
    from app.core.metric_authority import policy
    from app.core.data_lineage import METRIC_PLAN
    assert policy("dark_pool_prints").authority == "QUANTDATA"
    assert METRIC_PLAN["QD_EQUITY_PRINTS"].provider == "QUANTDATA"


# ═══════════════════════════════════════════ 3 · PROCEDENCIA ≠ CARRIL

def test_a_provider_dataset_stays_direct_provider_after_the_engine_reads_it():
    from app.core import quant_data_hub as HUB
    from app.core import itmq_intelligence as IQ
    from app.core.data_lineage import LINEAGE

    intel = {k: {"ready": True, "rows": [{"strike": 600.0 + i, "value": 1e8 * (i - 2)}
                                         for i in range(5)]}
             for k in ("gex_by_strike", "dex_by_strike", "vex_by_strike",
                       "chex_by_strike", "oi_by_strike")}
    intel["net_flow"] = {"ready": True, "rows": [{"t": "2026-09-18T14:30:00+00:00",
                                                  "value": 1e6}]}
    hub = HUB.hub_snapshot("SPY", intel)
    HUB.flow("SPY", intel)
    IQ.analyze("SPY", hub, spot=602.0, levels=[], price_series=[600, 601, 602])

    for metric in ("QD_GEX", "QD_DEX", "QD_VEX", "QD_CHEX",
                   "QD_OPEN_INTEREST_BY_STRIKE", "QD_NET_FLOW"):
        rec = LINEAGE.for_metric(metric, "SPY")
        assert rec is not None, metric
        assert rec["source_mode"] == "DIRECT_PROVIDER", metric
        assert rec["provider"] == "QUANTDATA", metric
    # Y lo propio sigue siendo propio.
    for metric in ("ITMQ_GAMMA_PRESSURE", "ITMQ_STRUCTURAL_SCORE"):
        assert LINEAGE.for_metric(metric, "SPY")["source_mode"] == "DERIVED"


def test_the_tools_table_separates_provenance_from_the_lane_that_fetched_it():
    """«MOTOR» en una columna llamada ORIGEN se leía como autoría."""
    html = text("app/templates/terminal.html")
    # Se comprueba la cabecera de la tabla de herramientas, no el documento entero:
    # otras tablas usan ORIGEN con otro significado legítimo.
    head = html[html.index('id="tblQdTools"') - 400:html.index('id="tblQdTools"') + 400]
    assert "<th>PROCEDENCIA</th>" in head and "<th>CARRIL</th>" in head
    assert "<th>ORIGEN</th>" not in head
    js = text("app/static/itmq_app.js")
    assert "DIRECT_PROVIDER · QUANTDATA" in js
    assert "'<span class=\"pos\">MOTOR</span>'" not in js


def test_the_flow_datasets_leave_a_provenance_record():
    from app.core import quant_data_hub as HUB
    from app.core.data_lineage import LINEAGE
    HUB.flow("SPY", {"net_flow": {"ready": True, "rows": [{"t": "a", "value": 1}]},
                     "options_order_flow_raw": {"ready": True, "rows": [{"t": "a"}]}})
    assert LINEAGE.for_metric("QD_NET_FLOW", "SPY")["source_mode"] == "DIRECT_PROVIDER"
    assert LINEAGE.for_metric("QD_ORDER_FLOW_UNCONSOLIDATED", "SPY")["source_mode"] == "DIRECT_PROVIDER"
    # Ausente se declara ausente, no se omite.
    assert LINEAGE.for_metric("QD_NET_DRIFT", "SPY")["source_mode"] == "UNAVAILABLE"


# ═══════════════════════════════════════════ 4 · DARK POOL: AUSENCIA ≠ CERO

def test_a_missing_off_exchange_flag_is_unknown_not_false():
    """El defecto que dejaba DARK POOL vacío con cientos de prints descargados."""
    from app.providers.quantdata.tools import norm_prints
    out = norm_prints({"prints": [{"timestamp": "t1", "price": 10, "size": 100}]})
    assert out["rows"][0]["off_exchange"] is None, "un dato ausente se volvió un 'no'"
    assert out["off_exchange_unknown"] == 1
    assert out["classification"] == {"UNKNOWN": 1}


def test_the_venue_code_classifies_when_the_provider_does_not_declare():
    from app.providers.quantdata.tools import norm_prints
    out = norm_prints({"prints": [
        {"timestamp": "t1", "price": 10, "size": 100, "venue": "FINRA/TRF"},
        {"timestamp": "t2", "price": 10, "size": 100, "venue": "XNAS"}]})
    assert out["off_exchange_confirmed"] == 1
    assert out["classification"] == {"VENUE_CODE": 2}
    assert out["rows"][0]["off_exchange"] is True
    assert out["rows"][1]["off_exchange"] is False


def test_the_provider_flag_wins_over_the_venue_guess():
    from app.providers.quantdata.tools import norm_prints
    out = norm_prints({"prints": [{"timestamp": "t1", "price": 10, "size": 100,
                                   "venue": "XNAS", "offExchange": True}]})
    assert out["rows"][0]["off_exchange"] is True
    assert out["rows"][0]["off_exchange_method"] == "PROVIDER_FLAG"


@pytest.mark.parametrize("rows,expected", [
    ([], "no llegaron impresiones en este ciclo"),
    ([{"t": "t", "price": 1, "size": 1, "off_exchange": None}], "sin clasificar"),
    ([{"t": "t", "price": 1, "size": 1, "off_exchange": False}], "en bolsa"),
])
def test_dark_pool_says_why_it_is_empty(rows, expected):
    """«SIN DATOS» a secas no distingue un mercado tranquilo de un parser roto."""
    from app.core.quant_data_hub import dark_pool
    intel = {"equity_prints": {"ready": bool(rows), "rows": rows}} if rows else {}
    assert expected in dark_pool("SPY", intel)["coverage_reason"]


# ═══════════════════════════════════════════ 5 · LEGIBILIDAD MULTI-ACTIVO

def test_bars_have_a_visibility_floor_that_does_not_invent_a_zero():
    """v1.47.0 · El suelo se mudó al componente común, y dejó de ser 3 px.

    Un suelo de 3 px aplicado al GROSOR mientras la agrupación se calculaba
    sobre el PASO garantizaba justo lo que no se quería: `fitBars` dejaba barras
    de 3 px de paso y `barThickness` cogía el 82 % de eso. La barra medía 2.46 px
    —por debajo del mínimo que el código creía estar imponiendo—. Ahora el suelo
    se aplica al paso, que es lo que tiene que caber.
    """
    js = text("app/static/itmq_adaptive_bars.js")
    assert "MIN_BAR_PX = 5" in js, "por debajo de 5 px una barra se lee como una raya"
    assert "MIN_EXTENT_PX" in js and "MAX_EXTENT_FRACTION" in js
    body = js[js.index("function extent("):]
    body = body[:body.index("\n  /**")]
    # Un cero sigue midiendo cero: el suelo es de visibilidad, no de magnitud.
    assert "value === 0) return 0" in body
    # Y el suelo no puede acercarse a una barra grande: está acotado como
    # fracción del eje, así que «hay algo» nunca se lee como «hay mucho».
    assert "MAX_EXTENT_FRACTION" in body


def test_one_dominant_strike_no_longer_flattens_the_whole_profile():
    """Escalar por el máximo escondía el perfil entero tras la barra dominante."""
    js = text("app/static/itmq_panels.js")
    assert "function robustPeak(" in js
    assert "OUTLIER_RATIO" in js
    assert "clipMark" in js, "una barra recortada debe decirlo, no truncarse"
    # v1.48.0 · Tres familias: barras verticales, perfil horizontal y relieve.
    # El relieve es OTRA VISTA del mismo perfil, así que tiene que compartir la
    # escala: si la comprimiera de otra forma, las dos vistas del mismo dato se
    # contradirían al alternarlas.
    assert js.count("= robustPeak(values)") == 3


def test_bars_aggregate_instead_of_overlapping_when_they_do_not_fit():
    """Muchas barras → agrupar → engrosar. Nunca → adelgazar hasta desaparecer."""
    js = text("app/static/itmq_adaptive_bars.js")
    assert "function layout(" in js and "function bin(" in js
    body = js[js.index("function bin("):]
    body = body[:body.index("\n  /**")]
    # Dos modos, ninguno es una media: una media cancela un +8 con un −8 vecinos
    # y hace desaparecer la concentración justo donde hay que verla.
    assert "mode === 'sum' ? sum : peak" in body
    # El contenedor conserva de dónde a dónde va, cuántos agrupa y su extremo.
    assert "from: chunk[0].label" in body and "count: chunk.length" in body
    assert "members" in body and "peak" in body


def test_the_flow_lanes_share_the_same_floors():
    """v1.47.0 · Los carriles dejan de tener su propia aritmética de grosor.

    `laneBarWidth` repetía el defecto de los paneles con otras constantes, así
    que cada corrección había que hacerla dos veces y sólo se hacía en una.
    Ahora la sirven los mismos contenedores, y la agrupación es del INTERVALO
    —2 m, 3 m, 5 m…— para que el carril siga alineado con las velas de TRACE.
    """
    js = text("app/static/itmq_orderflow.js")
    visible = re.sub(r"//[^\n]*", "", js)
    assert "function laneBarWidth(" not in visible, "grosor propio del carril"
    assert "AB.timeBins(" in visible and "AB.reduceBin(" in visible
    # Ya no quedan barras con suelo de un píxel en los carriles.
    assert "Math.max(1, (x1 - x0) * 0.7)" not in visible
    assert "Math.max(1, Math.min(9," not in visible


def test_the_heatmap_fills_the_same_regardless_of_the_asset_distribution():
    """La normalización lineal era un parámetro implícito por ticker.

    Cuanto más concentrada la cadena, más vacío el mapa. La normalización por
    rango reparte por construcción, así que un ETF con cola pesada y una acción
    con el campo plano producen mapas igual de legibles.
    """
    import numpy as np
    from app.core.asset_normalization import normalize_matrix

    rng = np.random.default_rng(11)
    heavy = (rng.pareto(0.6, size=(30, 50)) * 1e6).tolist()     # cadena concentrada
    flat = (rng.normal(0, 1e6, size=(30, 50))).tolist()         # campo repartido

    lin_h = normalize_matrix(heavy, symbol="A", mode="LINEAR")["filled_ratio"]
    lin_f = normalize_matrix(flat, symbol="B", mode="LINEAR")["filled_ratio"]
    rnk_h = normalize_matrix(heavy, symbol="A")["filled_ratio"]
    rnk_f = normalize_matrix(flat, symbol="B")["filled_ratio"]

    assert abs(lin_h - lin_f) > 0.25, "la lineal ya no dependía del activo"
    assert abs(rnk_h - rnk_f) < 0.05, "la de rango debe ser estable entre activos"
    assert rnk_h > 0.4 and rnk_f > 0.4, "el mapa seguiría viéndose vacío"
    assert normalize_matrix(heavy, symbol="A")["normalization"] == "ASSET_RANK_PERCENTILE"


def test_an_all_zero_field_stays_empty_instead_of_manufacturing_contrast():
    from app.core.asset_normalization import normalize_matrix
    out = normalize_matrix([[0.0] * 10] * 10, symbol="X")
    assert out["normalization"] == "ALL_ZERO_OBSERVED"
    assert all(v == 0.0 for row in out["matrix"] for v in row)


def test_no_ticker_is_hardcoded_in_the_render_path():
    for rel in ("app/static/itmq_panels.js", "app/core/asset_normalization.py"):
        found = re.findall(r'["\'](DIA|SPY|QQQ|AAPL|IWM|TSLA|NVDA|XLF)["\']', text(rel))
        assert not found, f"{rel} tiene tickers fijados: {sorted(set(found))}"


# ═══════════════════════════════════════════ 6 · MERCADO CERRADO

def test_the_interval_map_falls_back_to_the_last_valid_session():
    """Fin de semana explica que no haya datos nuevos, no que TRACE se vacíe."""
    from app.core import quant_data_hub as HUB
    from app.core.data_hub_runtime import HUB_RUNTIME
    from app.providers.quantdata.tools import norm_interval_map

    qd = {"data": {str(1758205800000 + i * 300000):
                   {"E": {str(530 + j): {"CALL": 10.0 * (j + 1), "PUT": -4.0}
                          for j in range(5)}} for i in range(6)}}
    live = HUB.interval_map("QQQ", {"interval_map_gamma": norm_interval_map(qd, "GAMMA")},
                            "GAMMA")
    assert live["ready"] and not live.get("last_known_good")

    closed = HUB.interval_map("QQQ", {}, "GAMMA")
    assert closed["ready"] is True, "TRACE se quedó sin fondo con mercado cerrado"
    assert closed["last_known_good"] is True
    assert "última sesión válida" in closed["session_note"]
    assert HUB_RUNTIME.lkg.get("interval_map:GAMMA", "QQQ") is not None


def test_a_fresh_engine_matrix_beats_a_stale_provider_one():
    """Un dato propio de ahora vale más que uno ajeno de hace dos días."""
    from app.core import quant_data_hub as HUB
    from app.providers.quantdata.tools import norm_interval_map
    qd = {"data": {"1758205800000": {"E": {"530": {"CALL": 1.0, "PUT": 0.0}}}}}
    HUB.interval_map("QQQ", {"interval_map_gamma": norm_interval_map(qd, "GAMMA")}, "GAMMA")
    engine = {"ready": True, "strikes": [600.0], "times": ["09:30", "09:35"],
              "gamma_m": [[1.0, 2.0]]}
    out = HUB.interval_map("QQQ", {}, "GAMMA", engine_heatmap=engine)
    assert out["source"] == "ITM_QUANT" and out["source_mode"] == "FALLBACK"
    assert not out.get("last_known_good")


# ═══════════════════════════════════════════ 7 · EL AUDITOR, APARTE

def test_the_diagnostic_panels_are_separated_from_the_analysis_tabs():
    """No se elimina la información: se deja de presentarla como análisis."""
    html = text("app/templates/terminal.html")
    assert 'class="nav-audit"' in html
    assert "AUDITOR · FUENTES" in html and "AUDITOR · ARQUITECTURA" in html
    assert 'class="nav-sep"' in html
    css = text("app/static/itmq_terminal.css")
    assert "nav.sections button.nav-audit" in css
    # Y la información sigue ahí.
    assert 'id="tblQdTools"' in html and 'id="tblChannels"' in html


def test_the_visual_harness_covers_incomparable_scales():
    """Sin credenciales sólo hay un activo: el banco es la única vía de validar."""
    h = text("tools/visual_harness.html")
    for sym in ("SPY", "QQQ", "DIA", "XLF", "SOFI"):
        assert f"'{sym}'" in h, sym
    assert "1.4e9" in h and "2.2e4" in h, "las escalas deben ser incomparables"

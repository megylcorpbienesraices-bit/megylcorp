"""v1.42.1 · Verdad de sesión, estado de proveedor y semántica de vacío.

Las diez correcciones nacen de una misma pantalla: a la una de la madrugada TRACE
aparecía sin velas, cuatro herramientas decían SIN_DATOS, la cabecera contaba
«1/2 en vivo» con las dos insignias en LIVE, y la consola acumulaba 503 de voz y
avisos de correlación. Casi nada de eso era una avería: era el programa incapaz de
distinguir «no hay mercado» de «algo se rompió».
"""

from __future__ import annotations

import csv
import io
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]


# ══════════════════════════════════════════════ SessionResolver

def test_the_early_morning_case_that_emptied_trace():
    """A la 1:00 del 18/09 se pedían barras del 18/09, un día sin sesión aún."""
    from app.core import session_resolver as S

    s = S.resolve(datetime(2026, 9, 18, 1, 0, tzinfo=NY))
    assert s.reason == S.REASON_SESSION_NOT_STARTED
    assert s.session_date.isoformat() == "2026-09-17"      # la última COMPLETADA
    assert s.is_current is False
    assert "aún no ha comenzado" in s.note


def test_every_phase_resolves_to_a_session_with_data():
    from app.core import session_resolver as S

    cases = {
        datetime(2026, 9, 18, 1, 0, tzinfo=NY): (S.PHASE_CLOSED, "2026-09-17"),
        datetime(2026, 9, 18, 7, 0, tzinfo=NY): (S.PHASE_PREMARKET, "2026-09-18"),
        datetime(2026, 9, 18, 11, 0, tzinfo=NY): (S.PHASE_REGULAR, "2026-09-18"),
        datetime(2026, 9, 18, 17, 0, tzinfo=NY): (S.PHASE_AFTERHOURS, "2026-09-18"),
        datetime(2026, 9, 19, 11, 0, tzinfo=NY): (S.PHASE_WEEKEND, "2026-09-18"),
    }
    for when, (phase, session) in cases.items():
        r = S.resolve(when)
        assert r.phase == phase, when
        assert r.iso == session, when


def test_bootstrap_window_is_made_of_completed_sessions():
    """Un gráfico no puede quedarse en blanco porque el mercado esté cerrado."""
    from app.core import session_resolver as S

    w = S.bootstrap_window(datetime(2026, 9, 18, 1, 0, tzinfo=NY), sessions=3)
    assert w["sessions"] == ["2026-09-15", "2026-09-16", "2026-09-17"]
    assert all(S.is_trading_day(datetime.fromisoformat(d).date()) for d in w["sessions"])


def test_market_closed_is_not_a_failure_but_an_open_market_gap_is():
    from app.core import session_resolver as S

    noche = S.empty_reason(datetime(2026, 9, 18, 1, 0, tzinfo=NY), channel="TRACE · velas")
    assert noche["is_failure"] is False
    assert noche["state"] == S.REASON_SESSION_NOT_STARTED

    abierto = S.empty_reason(datetime(2026, 9, 18, 11, 0, tzinfo=NY), channel="TRACE · velas")
    assert abierto["is_failure"] is True
    assert "requiere revisión" in abierto["detail"]


def test_premarket_tells_the_truth_per_data_kind():
    """En premarket la cinta de equity SÍ imprime; las opciones no.

    Decir «premarket, las opciones no imprimen» sobre un gráfico de velas sería
    falso, y una explicación falsa manda a buscar donde no hay nada.
    """
    from app.core import session_resolver as S

    when = datetime(2026, 9, 18, 7, 0, tzinfo=NY)
    eq = S.empty_reason(when, channel="TRACE · velas", kind="EQUITY")
    op = S.empty_reason(when, channel="FLUJO · prints", kind="OPTION")
    assert eq["is_failure"] is True                 # con equity imprimiendo, un hueco es real
    assert op["is_failure"] is False
    assert "opciones todavía no imprimen" in op["detail"]


def test_the_candle_bootstrap_no_longer_asks_for_todays_clock():
    """Guardia de regresión sobre la causa exacta del `sip:NO_BARS`."""
    src = (ROOT / "app" / "service.py").read_text(encoding="utf-8")
    i = src.index("def trace_session_bootstrap")
    block = src[i:i + 4000]
    assert "session_resolver.resolve()" in block
    assert "day=datetime.now(NY).date()" not in block


# ══════════════════════════════════════════════ rutas Quant Data

def test_no_tool_derives_invented_routes():
    from app.providers.quantdata.tools import FaltaRequisito, build_catalog, path_variants

    assert path_variants(("/v1/options/tool/order-flow",)) == ("/v1/options/tool/order-flow",)
    multi = {k: t.paths for k, t in build_catalog().items() if len(t.paths) != 1}
    assert not multi, f"sin ruta canónica única: {multi}"


def test_the_routes_that_were_404_in_production_are_fixed():
    from app.providers.quantdata.tools import build_catalog

    c = build_catalog()
    assert c["gainers_losers"].paths == ("/v1/options/tool/gainers-losers",)
    assert c["news"].paths == ("/v1/news/tool/news-articles",)
    assert c["options_order_flow"].paths == ("/v1/options/tool/order-flow/consolidated",)
    assert c["options_order_flow_raw"].paths == ("/v1/options/tool/order-flow/unconsolidated",)


def test_a_404_on_the_canonical_route_names_what_to_do():
    from app.providers.quantdata.tools import build_catalog, route_diagnostic, ROUTE_INVALID

    t = build_catalog()["dark_pool_levels"]
    t.route_state = ROUTE_INVALID
    d = route_diagnostic(t)
    assert d["route"] == "/v1/equities/tool/dark-pool-levels"
    assert "plan no incluye" in d["detail"] or "renombró" in d["detail"]


# ══════════════════════════════════════════════ estado de proveedores

def _session(hour: int) -> dict:
    from app.core import session_resolver as S
    return S.resolve(datetime(2026, 9, 18, hour, 0, tzinfo=NY)).describe()


def test_badges_and_counter_can_no_longer_contradict_each_other():
    """El bug: «ALPACA LIVE · QUANTDATA LIVE» junto a «1/2 en vivo».

    Eran dos definiciones de LIVE conviviendo. Ahora el contador se DERIVA de los
    mismos estados que las insignias, así que discrepar es imposible por construcción.
    """
    from app.core import provider_state as P

    states = [
        P.classify("ALPACA", in_roster=True, configured=True, responding=True,
                   has_data=True, age_seconds=5, channel="PRICE", market_session=_session(11)),
        P.classify("QUANTDATA", in_roster=True, configured=True, responding=True,
                   has_data=True, age_seconds=34, channel="EXPOSURE", market_session=_session(11)),
    ]
    s = P.summarize(states)
    live_badges = sum(1 for r in s["providers"] if r["state"] == P.LIVE)
    assert s["operational"] == live_badges == 2
    assert s["headline"].startswith("2/2")


def test_a_quiet_provider_at_night_is_not_an_alarm():
    """Contar como caído a un proveedor en silencio de madrugada convertía cada
    noche en una falsa alarma, y enseñaba a ignorar el indicador."""
    from app.core import provider_state as P

    st = P.classify("ALPACA", in_roster=True, configured=True, responding=True,
                    has_data=True, age_seconds=3600, channel="PRICE",
                    market_session=_session(1))
    assert st.state == P.MARKET_CLOSED
    assert st.counts_as_live is True


def test_the_same_staleness_is_a_problem_when_the_market_is_open():
    from app.core import provider_state as P

    st = P.classify("ALPACA", in_roster=True, configured=True, responding=True,
                    has_data=True, age_seconds=3600, channel="PRICE",
                    market_session=_session(11))
    assert st.state == P.STALE
    assert st.counts_as_live is False
    assert "mercado abierto" in st.detail


def test_the_provider_own_health_signal_is_not_overridden():
    """Si el adaptador ya sabe que su stream va entrecortado, esa evidencia manda."""
    from app.core import provider_state as P

    st = P.classify("TASTYTRADE", in_roster=True, configured=True, responding=True,
                    has_data=True, age_seconds=1, channel="PRICE",
                    market_session=_session(11), provider_signal="DEGRADED")
    assert st.state == P.DEGRADED


def test_a_provider_out_of_the_roster_is_not_reported_as_a_gap():
    from app.core import provider_state as P

    st = P.classify("TASTYTRADE", in_roster=False, configured=True)
    assert st.state == P.DISABLED
    assert st.counts_as_live is False
    s = P.summarize([st])
    assert s["total"] == 0          # no se le exige nada, no cuenta como carencia


# ══════════════════════════════════════════════ calidad N/A

def test_quality_distinguishes_unmeasured_from_measured_zero():
    from app.core.provider_parity import parity_report

    rep = parity_report(alpaca_configured=True, tastytrade_status=None,
                        quantdata_status={"configured": True},
                        options_coverage={"configured": True, "tools": [
                            {"page": "Exposure", "state": "PENDIENTE", "key": "gex_by_strike"}]})
    row = next((c for c in rep["channels"] if c["provider"] == "QUANTDATA"), None)
    assert row is not None
    assert row["quality"] is None                      # nunca un 0.0 engañoso
    assert row["quality_state"] in ("N/A", "MARKET_CLOSED")


def test_an_errored_tool_scores_zero_because_it_was_measured():
    from app.core.provider_parity import parity_report

    rep = parity_report(alpaca_configured=True, tastytrade_status=None,
                        quantdata_status={"configured": True},
                        options_coverage={"configured": True, "tools": [
                            {"page": "Exposure", "state": "NO_DISPONIBLE",
                             "key": "gex_by_strike", "error": "HTTP 404"}]})
    row = next(c for c in rep["channels"] if c["provider"] == "QUANTDATA")
    assert row["quality"] == 0.0
    assert row["selected"] is False


# ══════════════════════════════════════════════ correlación

def test_a_constant_series_yields_undefined_not_zero():
    """`corr = cov/(σx·σy)` con σx = 0 no está definida. Cero afirmaría independencia."""
    from app.core.safe_stats import correlation

    c = correlation([1, 1, 1, 1, 1], [2, 3, 4, 5, 6])
    assert c.value is None
    assert c.state == "UNDEFINED_CONSTANT_SERIES"
    assert c.usable is False
    assert "no es correlación cero" in c.detail.lower()


def test_correlation_is_never_called_when_undefined():
    """Ni un solo ConstantInputWarning: se comprueba ANTES de llamar."""
    import warnings
    from app.core.safe_stats import correlation, safe_corrcoef

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert correlation([5] * 20, list(range(20))).value is None
        assert safe_corrcoef([5] * 20, list(range(20))) is None
        assert correlation(list(range(20)), [7] * 20, method="spearman").value is None


def test_correlation_still_works_where_it_is_defined():
    from app.core.safe_stats import correlation

    assert correlation([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]).value == pytest.approx(1.0)
    assert correlation([1, 2, 3, 4], [4, 3, 2, 1], method="spearman").value == pytest.approx(-1.0)


def test_no_module_calls_corrcoef_directly_anymore():
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path.name in ("safe_stats.py",) or "__pycache__" in path.parts:
            continue
        if "np.corrcoef" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"correlación sin guarda en: {offenders}"


# ══════════════════════════════════════════════ persistencia atómica

def test_a_growing_schema_no_longer_writes_malformed_rows():
    """La causa de «22 filas malformadas»: append con esquema variable.

    El registro del Scanner añade columnas dinámicas (`feature_*`, `stop_shadow_k_*`).
    La cabecera se escribía con las del PRIMER ciclo; en cuanto un ciclo traía una
    más, la fila salía con más campos que la cabecera. Sin error al escribir.
    """
    from app.core.atomic_store import append_row, verify

    p = Path(tempfile.mkdtemp()) / "scanner_history.csv"
    append_row(p, {"timestamp": "t1", "spot": 1.0})
    append_row(p, {"timestamp": "t2", "spot": 2.0})
    r = append_row(p, {"timestamp": "t3", "spot": 3.0, "feature_nueva": 9})
    assert r["mode"] == "RESCHEMA" and r["rows_preserved"] == 2
    append_row(p, {"timestamp": "t4", "spot": 4.0, "feature_nueva": 10})

    v = verify(p)
    assert v["ok"] is True and v["malformed"] == 0 and v["rows"] == 4
    rows = list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8"))))
    assert [x["timestamp"] for x in rows] == ["t1", "t2", "t3", "t4"]
    assert rows[0]["feature_nueva"] == ""        # las antiguas se conservan legibles


def test_writers_go_through_the_atomic_store():
    for rel in ("app/core/scenario_engine.py", "app/core/session_memory.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "append_row(" in src, rel
        assert 'to_csv(p, mode="a"' not in src, rel
        assert 'to_csv(path, mode="a"' not in src, rel


def test_atomic_write_never_leaves_a_half_file():
    from app.core.atomic_store import atomic_write_text

    p = Path(tempfile.mkdtemp()) / "x.txt"
    atomic_write_text(p, "uno")
    atomic_write_text(p, "dos")
    assert p.read_text(encoding="utf-8") == "dos"
    assert not list(p.parent.glob(".*tmp")), "quedaron temporales huérfanos"


# ══════════════════════════════════════════════ Sophia Voice

def test_voice_capability_is_declared_before_it_is_offered():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        cap = c.get("/api/sophia/capabilities").json()
    assert cap["text"] is True
    assert isinstance(cap["stt"], bool) and isinstance(cap["tts"], bool)
    if not cap["stt"]:
        assert "faster-whisper" in (cap["stt_reason"] or "")


def test_transcribing_without_an_engine_is_501_not_503():
    """503 invita a reintentar —y el cliente lo hacía tres veces—. 501 dice que
    reintentar no puede funcionar, y el micrófono se apaga."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.sophia_voice import SophiaVoice

    if (SophiaVoice().status().get("stt") or {}).get("configured"):
        pytest.skip("esta máquina sí tiene motor de voz a texto")
    with TestClient(app) as c:
        r = c.post("/api/sophia/voice/transcribe",
                   files={"file": ("a.webm", io.BytesIO(b"x"), "audio/webm")})
    assert r.status_code == 501
    assert "escrito" in r.json()["detail"]


def test_the_client_checks_capability_before_recording():
    js = (ROOT / "app" / "static" / "itmq_app.js").read_text(encoding="utf-8")
    assert "/api/sophia/capabilities" in js
    assert "sophia.stt === false" in js          # no graba sin motor
    assert "sophiaApplyVoiceCapability" in js    # y lo deshabilita visiblemente


# ══════════════════════════════════════════════ hotfix de ejecución v1.42.1

def test_quantdata_catalog_bodies_all_build_without_nameerror():
    """Cada herramienta debe poder construir su request antes de tocar la red.

    La regresión real dejaba `_tf` fuera del módulo: la conexión figuraba LIVE pero
    GEX/DEX/VEX/CHEX/Order Flow caían en DEGRADED al construir el cuerpo.
    """
    from app.providers.quantdata.tools import build_catalog

    catalog = build_catalog()
    assert len(catalog) >= 31
    for key, tool in catalog.items():
        try:
            body = tool.body("dia")
        except FaltaRequisito as falta:
            # v1.57.7 · `max-pain` EXIGE `filter.expirationDate`. Cuando la
            # terminal todavía no ha resuelto la ventana de vencimientos no hay
            # cuerpo válido que construir, y mandar uno inválido es exactamente
            # el 400 que teníamos. Levantar `FaltaRequisito` es la conducta
            # correcta; lo que sería un fallo es un NameError, un KeyError o
            # inventarse la fecha.
            assert falta.campo, f"{key}: falta un campo sin decir cuál"
            assert falta.detalle, f"{key}: falta sin explicación"
            continue
        assert isinstance(body, dict), key


def test_quantdata_ticker_filter_is_canonical_for_affected_tools():
    from app.providers.quantdata.tools import build_catalog

    c = build_catalog()
    affected = (
        "gex_by_expiration", "dex_by_expiration", "vex_by_expiration", "chex_by_expiration",
        "gex_by_strike", "dex_by_strike", "vex_by_strike", "chex_by_strike",
        "options_order_flow", "options_order_flow_raw", "net_flow", "net_drift",
    )
    for key in affected:
        assert c[key].body("dia")["filter"]["ticker"] == "DIA", key


def test_flow_pro_summary_tolerates_missing_activity_score_column():
    """Un frame válido sin score aún calculado no puede derribar terminal_bundle."""
    from app.core.institutional_modules import flow_pro_summary

    bars = pd.DataFrame([{
        "timestamp": "2026-09-18T07:40:00",
        "signed_notional_proxy": 1000.0,
        "activity_ready": True,
        "close": 515.80,
    }])
    out = flow_pro_summary(pd.DataFrame(), pd.DataFrame(), {}, bars)
    assert out["state"] == "WAITING"
    assert out["net"] == 0.0


def test_flow_pro_figure_tolerates_missing_activity_score_column():
    """La vista y el resumen comparten la misma entrada parcial y ambos deben ser fail-soft."""
    from app.core.institutional_modules import flow_unusual_pro_figure

    bars = pd.DataFrame([{
        "timestamp": "2026-09-18T07:40:00",
        "signed_notional_proxy": -2500.0,
        "activity_ready": True,
        "close": 515.75,
    }])
    fig = flow_unusual_pro_figure(pd.DataFrame(), pd.DataFrame(), {}, bars, "DIA")
    assert fig is not None
    assert len(fig.data) >= 1


def test_alpaca_stock_snapshot_uses_freshest_observed_price_not_stale_trade():
    """Un trade viejo no debe volver rancio un snapshot con NBBO más reciente."""
    from app.core.alpaca_data import _stock_spot_from_snapshot

    js = {
        "latestTrade": {"p": 515.70, "t": "2026-09-18T12:43:00Z"},
        "latestQuote": {"bp": 515.79, "ap": 515.81, "t": "2026-09-18T12:43:38Z"},
        "minuteBar": {"c": 515.75, "t": "2026-09-18T12:43:00Z"},
    }
    spot, ts = _stock_spot_from_snapshot(js)
    assert spot == pytest.approx(515.80)
    assert ts == "2026-09-18T12:43:38Z"


def test_fresh_quote_prevents_false_premarket_publication_retention():
    """Reproduce la captura: trade >20 s viejo, quote fresca, opciones estructurales cerradas.

    El gate debe aceptar la estructura porque en PREMARKET la cadena usa reloj de
    sesión y el subyacente está vivo por la cotización más reciente.
    """
    from app.core.alpaca_data import _stock_spot_from_snapshot
    from app.core.freshness import evaluate_publication_gate, reset_publication_gate

    js = {
        "latestTrade": {"p": 515.70, "t": "2026-09-18T12:43:00Z"},
        "latestQuote": {"bp": 515.79, "ap": 515.81, "t": "2026-09-18T12:43:38Z"},
    }
    _, stock_ts = _stock_spot_from_snapshot(js)
    reset_publication_gate("DIA")
    now = pd.Timestamp("2026-09-18T12:43:40Z").timestamp()
    gate = evaluate_publication_gate({
        "symbol": "DIA",
        "market_state": "PREMARKET",
        "latest_option_market_timestamp": "2026-09-17T20:00:00Z",
        "stock_market_timestamp": stock_ts,
        "quality_clock": "2026-09-18T12:43:40Z",
    }, market_state="PREMARKET", now=now, is_replay=False)
    assert gate["publicar_permitido"] is True
    assert gate["subyacente"]["antiguedad_s"] == pytest.approx(2.0)


def test_london_activity_summary_tolerates_partial_bars_without_score(monkeypatch):
    from app.core import flow_intelligence as F

    partial = pd.DataFrame([{
        "timestamp": "2026-09-18T07:40:00",
        "open": 515.7, "high": 515.9, "low": 515.6, "close": 515.8,
        "volume": 1000.0, "signed_notional_proxy": 5000.0,
    }])
    monkeypatch.setattr(F, "fetch_london_activity_bars", lambda symbol, live_ticks=None: partial.copy())
    out = F.london_activity_tape_summary("DIA")
    assert out["state"] == "ACTIVE"
    assert out["unusual_events"] == 0
    assert out["score"] == 0.0


def test_premarket_freshness_uses_live_provider_event_not_slow_structural_clock(monkeypatch):
    """Reproduce la captura HOTFIX1: snapshot 25 s viejo pero quote LIVE 2 s vieja.

    El límite de 20 s se conserva. Lo que cambia es el reloj: el subyacente debe
    usar la observación de precio más reciente que ya entró por los proveedores,
    mientras la cadena de opciones conserva su reloj estructural de PREMARKET.
    """
    from datetime import datetime, timedelta, timezone
    from app import service as S
    from app.core.freshness import reset_publication_gate

    now = datetime.now(timezone.utc)
    structural = (now - timedelta(seconds=25)).isoformat()
    live_quote = (now - timedelta(seconds=2)).isoformat()
    option_close = (now - timedelta(hours=12)).isoformat()

    monkeypatch.setattr(S.PROVIDER_BUS, "latest_events", lambda symbol: [{
        "source": "ALPACA_SIP", "symbol": "DIA", "event_type": "QUOTE",
        "timestamp": live_quote, "event_time_valid": True,
        "values": {"bid": 515.79, "ask": 515.81},
    }])
    monkeypatch.setattr(S.PRICE_TICK_FABRIC, "scheduler_status", lambda symbol: {
        "symbol": "DIA", "connected": True, "source": "ALPACA_SIP",
        "last_seq": 0, "last_tick": None,
    })

    reset_publication_gate("DIA")
    state = S.PlatformState(symbol="DIA", mode="LIVE")
    state.meta = {
        "symbol": "DIA", "market_state": "PREMARKET",
        "latest_option_market_timestamp": option_close,
        "stock_market_timestamp": structural,
        "quality_clock": structural,
    }
    gate = state.current_publication_gate()

    assert gate["publicar_permitido"] is True
    assert gate["subyacente"]["antiguedad_s"] < 5.0
    assert gate["subyacente"]["limite_s"] == 20.0
    assert gate["underlying_freshness_source"] == "PROVIDER_BUS:ALPACA_SIP:QUOTE"
    assert gate["structural_stock_market_timestamp"] == structural
    assert gate["effective_stock_market_timestamp"] == live_quote


def test_premarket_freshness_does_not_invent_live_clock_when_providers_are_stale(monkeypatch):
    """No se arregla maquillando el gate: sin observación LIVE real, debe bloquear."""
    from datetime import datetime, timedelta, timezone
    from app import service as S
    from app.core.freshness import reset_publication_gate

    now = datetime.now(timezone.utc)
    stale = (now - timedelta(seconds=30)).isoformat()
    option_close = (now - timedelta(hours=12)).isoformat()

    monkeypatch.setattr(S.PROVIDER_BUS, "latest_events", lambda symbol: [])
    monkeypatch.setattr(S.PRICE_TICK_FABRIC, "scheduler_status", lambda symbol: {
        "symbol": "DIA", "connected": False, "source": None,
        "last_seq": 0, "last_tick": None,
    })

    reset_publication_gate("DIA")
    state = S.PlatformState(symbol="DIA", mode="LIVE")
    state.meta = {
        "symbol": "DIA", "market_state": "PREMARKET",
        "latest_option_market_timestamp": option_close,
        "stock_market_timestamp": stale,
        "quality_clock": stale,
    }
    gate = state.current_publication_gate()

    assert gate["publicar_permitido"] is False
    assert gate["subyacente"]["estado"] == "STALE"
    assert gate["subyacente"]["limite_s"] == 20.0
    assert gate["underlying_freshness_source"] == "STRUCTURAL_SNAPSHOT"


def test_quality_gate_meta_prefers_fresh_quote_over_25s_structural_snapshot(monkeypatch):
    """El Scanner tampoco debe nacer bloqueado cuando la fabric ya tiene un quote fresco."""
    from datetime import datetime, timedelta, timezone
    from app import service as S

    now = datetime.now(timezone.utc)
    old = (now - timedelta(seconds=25)).isoformat()
    fresh = (now - timedelta(seconds=1)).isoformat()
    monkeypatch.setattr(S.PROVIDER_BUS, "latest_events", lambda symbol: [{
        "source": "ALPACA_SIP", "event_type": "QUOTE", "timestamp": fresh,
        "event_time_valid": True, "values": {"bid": 515.0, "ask": 515.1},
    }])
    monkeypatch.setattr(S.PRICE_TICK_FABRIC, "scheduler_status", lambda symbol: {
        "symbol": symbol, "last_tick": None, "source": None,
    })

    out = S._quality_meta_with_live_underlying({
        "symbol": "DIA", "market_state": "PREMARKET", "stock_market_timestamp": old,
    }, "DIA")
    assert out["stock_market_timestamp"] == fresh
    assert out["structural_stock_market_timestamp"] == old
    assert out["underlying_freshness_source"] == "PROVIDER_BUS:ALPACA_SIP:QUOTE"

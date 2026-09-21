"""v1.41.1 · Recuperación de datos en vivo y cuota compartida de Quant Data.

Cada prueba aquí corresponde a un fallo observado en producción con los tres
proveedores configurados y operativos.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]



@pytest.fixture()
def con_tastytrade(monkeypatch):
    """Roster con tastytrade dentro.

    Por defecto el roster es Alpaca + Quant Data, así que tastytrade no aparece en el
    informe. Estas pruebas verifican cómo se lee su salud CUANDO sí forma parte del
    roster: esa lógica sigue siendo necesaria y no puede quedar sin cubrir sólo
    porque la instalación por defecto ya no lo use.
    """
    import importlib
    import app.core.provider_parity as P
    monkeypatch.setenv("ITM_OPTIONS_PEERS", "ALPACA,TASTYTRADE,QUANTDATA")
    importlib.reload(P)
    yield P
    monkeypatch.delenv("ITM_OPTIONS_PEERS", raising=False)
    importlib.reload(P)


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ────────────────────────── TRACE: el panel no puede quedarse congelado

def test_trace_profile_has_no_undeclared_identifier():
    """`moving` se referenciaba sin declararse y congelaba los dos laterales.

    El error era de ejecución, no de sintaxis: `node --check` lo dejaba pasar y
    sólo se veía como "RENDER NO DISPONIBLE" sobre el panel ya en blanco.
    """
    src = text("app/static/itmq_trace.js")
    body = src[src.find("function drawProfile"):src.find("function strikeStep")]
    assert body, "drawProfile no encontrado"
    keywords = {"true", "false", "null", "undefined", "this", "new", "typeof", "void", "await"}
    referenced = set(re.findall(r"\breturn\s+([A-Za-z_$][\w$]*)", body)) - keywords
    assert referenced, "la prueba debe estar mirando identificadores reales"
    for name in referenced:
        declared = re.search(rf"\b(?:const|let|var|function)\s+{re.escape(name)}\b", body)
        assert declared, f"drawProfile devuelve '{name}' sin declararlo en su ámbito"


def test_side_panels_do_not_advance_the_shared_price_axis():
    """Sólo drawMain hace avanzar el eje; si un lateral también lo hiciera, el
    precio se movería al doble de velocidad en pantalla."""
    src = text("app/static/itmq_trace.js")
    settling = src[src.find("function priceAxisSettling"):src.find("function priceScale")]
    assert settling
    assert ".step(" not in settling


def test_render_failures_are_recorded_for_inspection():
    src = text("app/static/itmq_core.js")
    assert "RENDER_ERRORS" in src
    assert "renderErrors" in src and "healthy()" in src


def test_panel_failure_cannot_stop_the_data_cycle():
    src = text("app/static/itmq_app.js")
    cycle = src[src.find("async function pullTrace"):src.find("async function pullDiagnostics")]
    assert "try { Trace.applyData(d); }" in cycle
    assert "try { Flow.applyTrace(d); }" in cycle


def test_frontend_lint_contract_exists():
    cfg = json.loads(text(".eslintrc.json"))
    # no-undef es la regla que habría atrapado el fallo de v1.41.0.
    assert cfg["rules"]["no-undef"] == "error"
    assert (ROOT / "scripts/lint_frontend.sh").is_file()


def test_terminal_modules_parse_under_node():
    modules = ("itmq_core.js", "itmq_trace.js", "itmq_orderflow.js", "itmq_panels.js", "itmq_app.js")
    node = shutil.which("node")
    if node is None:
        # Sin node se verifica lo que sí es verificable aquí: que los módulos
        # existen y están declarados en la plantilla. La comprobación sintáctica
        # completa la ejecuta scripts/lint_frontend.sh en la máquina de release.
        tpl = text("app/templates/terminal.html")
        for name in modules:
            assert (ROOT / "app/static" / name).is_file(), name
            assert name in tpl, name
        return
    for name in modules:
        r = subprocess.run([node, "--check", str(ROOT / "app/static" / name)],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{name}: {r.stderr[:200]}"


# ────────────────────────── velas: la fabric fría no puede vaciar la pantalla

def test_live_trace_backfills_candles_when_tick_fabric_is_cold():
    """Con el motor ya publicando estructura pero la fabric aún vacía, TRACE,
    FLUJO, RESUMEN y DARK POOL se quedaban sin gráfico. Ahora recurren al
    bootstrap de sesión, que es historia observada del mismo instrumento."""
    src = text("app/service.py")
    block = src[src.find("def nextgen_trace("):src.find("def nextgen_surface(")]
    assert "if (not _have or _short) and not self.replay_context.is_replay:" in block
    assert "trace_session_bootstrap" in block
    assert 'payload["candle_source"] = "SESSION_BOOTSTRAP_BACKFILL"' in block
    # El motivo viaja siempre, también cuando el respaldo tampoco encuentra barras.
    assert 'payload["candle_backfill_reason"]' in block


def test_backfill_also_covers_a_half_empty_tick_fabric():
    """Un puñado de velas no es un gráfico: con la fabric arrancando llegaban 3 o
    4 barras y el panel se veía vacío sin estar vacío. El respaldo entra también
    cuando la cobertura es corta, y sólo si trae más barras de las que ya había."""
    src = text("app/service.py")
    block = src[src.find("def nextgen_trace("):src.find("def nextgen_surface(")]
    assert "_short = bool(_want_bars) and len(_have) < min(_want_bars, 30)" in block
    assert "if isinstance(bars, list) and len(bars) > len(_have):" in block
    assert "LIVE_TICK_FABRIC_SHORT_COVERAGE" in block


def test_prints_render_without_a_price_curve():
    """Un print observado es dato por sí solo; el panel no puede quedarse en
    blanco sólo porque la curva de precio aún no haya llegado."""
    src = text("app/static/itmq_panels.js")
    pp = src[src.find("function pricePrints"):]
    assert "if (!candles.length && !marks.length)" in pp


# ────────────────────────── Quant Data: cuota compartida, sin duplicar

def test_pages_lane_reuses_every_endpoint_the_engine_already_fetched():
    from app.providers.quantdata.shared import ENGINE_SHARED_KEYS
    from app.providers.quantdata.tools import build_catalog

    catalog = build_catalog()
    for tool_key in ENGINE_SHARED_KEYS:
        assert tool_key in catalog, tool_key
    # Nueve peticiones por ciclo que dejan de gastarse.
    assert len(ENGINE_SHARED_KEYS) >= 9


def test_engine_lane_publishes_raw_payloads_for_sharing():
    src = text("app/providers/quantdata/runtime.py")
    assert "RAW_CACHE.put(_name, target.active_symbol, _payload)" in src
    # v1.57.3 · La contabilidad de cuota vive en `QuantDataClient.post`, el único
    # sitio por el que pasan LOS DOS carriles. Antes cada uno anotaba lo suyo: el
    # del motor hacía `note()` + `spend()` y el de páginas sólo `spend()`, así que
    # la mitad de las respuestas no actualizaba las cabeceras y cada petición del
    # motor se contaba dos veces. La garantía es más fuerte ahora, no más débil:
    # ningún carril puede contar por su cuenta ni saltarse el contador.
    assert "QUOTA.spend(" not in src, "el carril del motor vuelve a contar por su cuenta"
    cliente = text("app/providers/quantdata/client.py")
    assert "QUOTA.spend(1)" in cliente and "QUOTA.note(remaining=remaining" in cliente


def test_raw_cache_expires_so_stale_data_is_never_shown_as_fresh():
    import time
    from app.providers.quantdata.shared import RawCache

    cache = RawCache()
    cache.put("gamma", "DIA", {"data": {"x": 1}})
    assert cache.get("gamma", "DIA", max_age_s=60) is not None
    assert cache.get("gamma", "SPY", max_age_s=60) is None, "otro símbolo nunca comparte payload"

    # Se envejece la entrada de forma determinista: medir el tiempo real entre
    # escritura y lectura haría la prueba dependiente de la velocidad de la máquina.
    key = ("gamma", "DIA")
    ts, payload = cache._data[key]
    cache._data[key] = (ts - 120.0, payload)
    assert cache.get("gamma", "DIA", max_age_s=90) is None
    assert cache.age("gamma", "DIA") >= 120.0


def test_symbol_change_drops_the_previous_symbol_payloads():
    from app.providers.quantdata.shared import RawCache

    cache = RawCache()
    cache.put("gamma", "DIA", {"a": 1})
    cache.put("gamma", "SPY", {"a": 2})
    cache.clear_symbol("DIA")
    assert cache.get("gamma", "DIA") is None
    assert cache.get("gamma", "SPY") is not None


def test_engine_lane_keeps_a_reserved_share_of_the_quota():
    from app.providers.quantdata.shared import QuotaGuard, ENGINE_RESERVE

    q = QuotaGuard()
    q.note(remaining=ENGINE_RESERVE + 5, limit=100, reset_seconds=30)
    assert q.budget_for_pages(20) == 5           # las páginas sólo usan el excedente
    q.note(remaining=ENGINE_RESERVE, limit=100, reset_seconds=30)
    assert q.budget_for_pages(20) == 0           # el resto queda para la estructura


def test_rate_limit_stops_both_lanes_not_just_one():
    from app.providers.quantdata.shared import QuotaGuard

    q = QuotaGuard()
    q.note(remaining=500, limit=1000, reset_seconds=30)
    assert q.budget_for_pages(8) > 0
    q.note_rate_limited(30.0)
    assert q.budget_for_pages(8) == 0
    assert q.snapshot()["rate_limited"] is True


def test_client_reports_rate_limit_to_the_shared_budget():
    src = text("app/providers/quantdata/client.py")
    block = src[src.find("if response.status_code == 429:"):]
    # `Retry-After` llega SIN mezclar con `Reset`: el guardián cae a `Reset` solo
    # si la cabecera no viene, y así puede distinguir una de otra en el Auditor.
    assert "QUOTA.note_rate_limited(retry_after)" in block


def test_unknown_quota_advances_slowly_instead_of_blindly():
    from app.providers.quantdata.shared import QuotaGuard

    q = QuotaGuard()                      # sin telemetría todavía
    assert 1 <= q.budget_for_pages(30) <= 4


# ────────────────────────── estado de proveedores y cobertura por canal

def test_tastytrade_live_is_read_from_its_real_health_keys(con_tastytrade):
    """La salud se publica como dxlink/market_data/last_event_age_ms; las claves
    connected/running que se leían antes no existen en ese contrato."""
    parity_report = con_tastytrade.parity_report

    streaming = parity_report(
        alpaca_configured=True,
        tastytrade_status={"configured": True, "dxlink": "CONNECTED",
                           "market_data": "STREAMING", "last_event_age_ms": 800.0},
        quantdata_status={"configured": False},
    )
    tt = next(p for p in streaming["providers"] if p["provider"] == "TASTYTRADE")
    assert tt["status"] == "LIVE"
    assert tt["age_seconds"] == pytest.approx(0.8, abs=1e-6)

    idle = parity_report(
        alpaca_configured=True,
        tastytrade_status={"configured": True, "dxlink": "DISCONNECTED", "market_data": "IDLE"},
        quantdata_status={"configured": False},
    )
    # v1.42.1 · El vocabulario es el de `provider_state`: un proveedor configurado y
    # respondiendo, pero sin dato vivo, es CONNECTED. «CONFIGURED» describía la
    # configuración, no el estado operativo, y por eso convivía con el contador.
    assert next(p for p in idle["providers"] if p["provider"] == "TASTYTRADE")["status"] == "CONNECTED"


def test_stale_stream_is_degraded_not_live(con_tastytrade):
    parity_report = con_tastytrade.parity_report

    rep = parity_report(
        alpaca_configured=False,
        tastytrade_status={"configured": True, "dxlink": "CONNECTED",
                           "market_data": "STREAMING", "last_event_age_ms": 600_000.0},
        quantdata_status={"configured": False},
    )
    assert next(p for p in rep["providers"] if p["provider"] == "TASTYTRADE")["status"] == "DEGRADED"


def test_channel_coverage_reads_quality_from_its_real_nesting(con_tastytrade):
    """provider_bus anida la calidad bajo row['quality']; leerla en la raíz
    devolvía DEFAULT y 0.0 para todos los canales."""
    parity_report = con_tastytrade.parity_report

    rep = parity_report(
        alpaca_configured=True,
        tastytrade_status={"configured": True, "dxlink": "CONNECTED", "market_data": "STREAMING",
                           "last_event_age_ms": 500.0},
        quantdata_status={"configured": True, "last_success": None},
        consensus={
            "selected_provider": "TASTYTRADE",
            "providers": [
                {"source": "ALPACA", "age_ms": 400.0,
                 "quality": {"quality_score": 71.5, "quality_label": "GOOD", "channel_policy": "EQUITY_TRADE"}},
                {"source": "TASTYTRADE", "age_ms": 120.0,
                 "quality": {"quality_score": 93.2, "quality_label": "HIGH", "channel_policy": "OPTION_QUOTE"}},
            ],
        },
    )
    by = {c["provider"]: c for c in rep["channels"]}
    assert by["ALPACA"]["channel"] == "EQUITY_TRADE"
    assert by["ALPACA"]["quality"] == pytest.approx(71.5)
    assert by["TASTYTRADE"]["channel"] == "OPTION_QUOTE"
    assert by["TASTYTRADE"]["selected"] is True
    assert by["ALPACA"]["selected"] is False


def test_quantdata_freshness_decides_live_not_merely_having_a_key():
    from app.core.provider_parity import parity_report
    from datetime import datetime, timezone

    fresh = datetime.now(timezone.utc).isoformat()
    rep = parity_report(alpaca_configured=False, tastytrade_status={},
                        quantdata_status={"configured": True, "last_success": fresh})
    assert next(p for p in rep["providers"] if p["provider"] == "QUANTDATA")["status"] == "LIVE"

    rep2 = parity_report(alpaca_configured=False, tastytrade_status={},
                         quantdata_status={"configured": True, "last_success": "2020-01-01T00:00:00+00:00"})
    assert next(p for p in rep2["providers"] if p["provider"] == "QUANTDATA")["status"] == "DEGRADED"


# ────────────────────────── valores que se mostraban vacíos teniendo dato

def test_iv_rank_is_read_from_the_engine_summary():
    from app.terminal_api import build_terminal_bundle

    state = {
        "ready": True, "active_symbol": "DIA", "spot": 534.0,
        "volatility": {"atm_iv": 19.7},
        "source_fusion": {"sources": {"quantdata": {"values": {
            "iv_rank": {"available": True, "call_rank": 40.0, "put_rank": 60.0}}}}},
    }
    vol = build_terminal_bundle(state=state, trace={})["volatilidad"]
    assert vol["iv_rank"] == pytest.approx(50.0)
    assert vol["iv_rank_call"] == pytest.approx(40.0)
    assert vol["iv_rank_put"] == pytest.approx(60.0)


def test_iv_rank_is_found_however_the_provider_nests_it():
    from app.terminal_api import _deep_find

    assert _deep_find({"data": {"2026-09-17": {"ivRank": 42.0}}}, "ivRank") == 42.0
    assert _deep_find({"a": {"b": {"c": {"iv_rank": 7.0}}}}, "ivRank") == 7.0
    assert _deep_find({"nothing": 1}, "ivRank") is None


def test_vwap_is_computed_on_the_backend_from_the_same_candles():
    from app.terminal_api import build_terminal_bundle

    trace = {"candles": [
        {"t": "2026-09-17T14:00:00", "c": 100.0, "v": 100.0},
        {"t": "2026-09-17T14:01:00", "c": 102.0, "v": 300.0},
    ]}
    dp = build_terminal_bundle(state={"ready": True, "active_symbol": "DIA"}, trace=trace)["dark_pool"]
    assert dp["vwap"] == pytest.approx((100 * 100 + 102 * 300) / 400)


def test_vwap_is_absent_rather_than_zero_without_volume():
    from app.terminal_api import build_terminal_bundle

    dp = build_terminal_bundle(state={"ready": True}, trace={"candles": [{"t": "x", "c": 100.0, "v": 0}]})["dark_pool"]
    assert dp["vwap"] is None


# ────────────────────────── diagnóstico: por qué un panel está vacío

def test_diagnostics_names_the_reason_for_every_empty_panel():
    from app.terminal_api import build_diagnostics

    d = build_diagnostics(
        state={"ready": True, "active_symbol": "DIA", "mode": "LIVE",
               "liquidity_zones": {"zones": [], "reason": "NO_PRINTS"},
               "large_prints": {"count": 0}},
        trace={"candles": [], "candle_backfill_reason": "BOOTSTRAP_EMPTY",
               "option_prints": [], "profiles": {"rows": []}, "heatmap_history": {}, "levels": []},
    )
    by = {c["panel"]: c for c in d["checks"]}
    assert by["TRACE · velas"]["ok"] is False
    assert by["TRACE · velas"]["reason"] == "BOOTSTRAP_EMPTY"
    # v1.52.1 · La causa ya no sale de las zonas de liquidez propias —una capa
    # DERIVADA— sino del carril del proveedor, que es el que la pantalla lee.
    assert by["DARK POOL · niveles"]["reason"] == "EL_PROVEEDOR_NO_DEVOLVIO_FILAS"
    assert by["DARK POOL · niveles"]["source"] == "QUANTDATA_DARK_POOL_LEVELS"
    assert by["DARK POOL · dark flow"]["source"] == "QUANTDATA_DARK_FLOW"
    assert "TRACE · velas" in d["failing"]


def test_diagnostics_reports_the_backfill_source_when_it_worked():
    from app.terminal_api import build_diagnostics

    d = build_diagnostics(
        state={"ready": True},
        trace={"candles": [{"t": "x", "c": 1.0}], "candle_source": "SESSION_BOOTSTRAP_BACKFILL"},
    )
    velas = next(c for c in d["checks"] if c["panel"] == "TRACE · velas")
    assert velas["ok"] is True
    assert velas["source"] == "SESSION_BOOTSTRAP_BACKFILL"


def test_diagnostics_route_is_registered():
    assert "/api/terminal/diagnostics" in text("app/main.py")


# ────────────────────────── puerta de publicación: retener no es apagar

def test_blocked_publication_still_serves_observed_price():
    """La cadena vieja bloquea acción, no borra el último contexto conocido.

    Precio y flujo siguen observados; perfiles/heatmap/niveles pueden permanecer
    visibles sólo como contexto retenido y la decisión queda vacía.
    """
    src = text("app/service.py")
    block = src[src.find("def nextgen_trace("):src.find("def nextgen_surface(")]
    assert "current_publication_gate" in block
    assert "LAST_GOOD_STRUCTURE_CONTEXT_PLUS_OBSERVED_LIVE" in block
    assert 'payload["decision"] = {}' in block
    assert '"context_only": True' in block
    gate = block[block.find("_structure_blocked ="):block.find("payload = build_nextgen_trace_payload")]
    assert "return" not in gate
    assert "nextgen_trace_price_only" not in gate


def test_diagnostics_attributes_structural_gaps_to_one_cause():
    from app.terminal_api import build_diagnostics

    d = build_diagnostics(
        state={"ready": True, "active_symbol": "DIA"},
        trace={"blocked": True, "publication_blocked_motive": "option_age 900s > umbral",
               "candles": [{"t": "x", "c": 1.0}], "option_prints": [],
               "profiles": {"rows": []}, "heatmap_history": {}, "levels": []},
    )
    assert d["publication_blocked"] is True
    by = {c["panel"]: c for c in d["checks"]}
    # Una sola causa explicada una sola vez, no nueve motivos distintos.
    assert "PUBLICACIÓN RETENIDA" in by["TRACE · perfiles por strike"]["reason"]
    assert "option_age 900s" in by["TRACE · niveles"]["reason"]
    # El precio observado sigue publicándose durante el bloqueo.
    assert by["TRACE · velas"]["ok"] is True


def test_bundle_carries_the_block_so_the_header_can_warn():
    from app.terminal_api import build_terminal_bundle

    b = build_terminal_bundle(
        state={"ready": True, "active_symbol": "DIA"},
        trace={"blocked": True, "publication_blocked_motive": "frescura",
               "candle_source": "SESSION_BOOTSTRAP_BACKFILL"})
    assert b["publication_blocked"] is True
    assert b["publication_blocked_motive"] == "frescura"
    assert b["candle_source"] == "SESSION_BOOTSTRAP_BACKFILL"


# ────────────────────────── cadencia: el plan manda, no una constante

def test_cadence_is_derived_from_the_plan_not_hardcoded():
    """Nueve endpoints cada 15 s son 2160 peticiones/hora. Contra un plan de 240
    la cuota se agota en minutos y la terminal se queda muda."""
    from app.providers.quantdata.shared import QuotaGuard, ENGINE_FAST_REQUESTS

    q = QuotaGuard()
    q.note(remaining=236, limit=240, reset_seconds=3600)
    interval = q.recommended_interval(ENGINE_FAST_REQUESTS)
    per_hour = (3600.0 / interval) * ENGINE_FAST_REQUESTS
    assert per_hour < 240, f"{per_hour:.0f} peticiones/hora no caben en el plan"
    assert interval < 300, "un plan de 240/hora permite refrescar en menos de 5 minutos"


def test_generous_plan_refreshes_faster_than_a_small_one():
    from app.providers.quantdata.shared import QuotaGuard, ENGINE_FAST_REQUESTS

    small, big = QuotaGuard(), QuotaGuard()
    small.note(remaining=236, limit=240, reset_seconds=3600)
    big.note(remaining=9_000, limit=10_000, reset_seconds=3600)
    assert big.recommended_interval(ENGINE_FAST_REQUESTS) < small.recommended_interval(ENGINE_FAST_REQUESTS)


def test_nearly_exhausted_quota_backs_off_on_its_own():
    from app.providers.quantdata.shared import QuotaGuard, ENGINE_FAST_REQUESTS

    q = QuotaGuard()
    q.note(remaining=4, limit=240, reset_seconds=1800)
    assert q.recommended_interval(ENGINE_FAST_REQUESTS) >= 300


def test_unknown_window_falls_back_to_the_PUBLISHED_contract():
    """v1.57.3 · Esta prueba exigía asumir una ventana DIARIA cuando el proveedor
    no manda `Reset`. Era una precaución de cuando no conocíamos el plan.

    La documentación de Quant Data publica 240 peticiones / 60 s en ventana
    deslizante. Asumir 240/día con eso escrito no es prudencia: es estrangular la
    terminal a un ciclo cada 32 minutos por una ventana que nadie ha contratado.
    Lo que se exige ahora es lo correcto —vivir dentro del contrato publicado— y
    se sigue midiendo con el mismo criterio: que el carril del motor no se acerque
    al tope.
    """
    from app.providers.quantdata.shared import (
        QuotaGuard, ENGINE_FAST_REQUESTS, SUSTAINED_LIMIT, SUSTAINED_WINDOW_S)

    q = QuotaGuard()
    q.note(remaining=None, limit=240, reset_seconds=None)
    por_ventana = (SUSTAINED_WINDOW_S / q.recommended_interval(ENGINE_FAST_REQUESTS)) * ENGINE_FAST_REQUESTS
    assert por_ventana <= SUSTAINED_LIMIT
    assert por_ventana <= SUSTAINED_LIMIT * 0.1, (
        f"{por_ventana:.0f} de {SUSTAINED_LIMIT} por ventana: el carril del motor "
        "tiene que seguir siendo una fracción pequeña del contrato")


def test_engine_tiers_endpoints_by_how_fast_they_actually_move():
    from app.providers.quantdata.shared import ENGINE_FAST_JOBS, ENGINE_SLOW_JOBS

    assert set(ENGINE_FAST_JOBS) == {"gamma", "delta", "net_drift", "net_flow"}
    # Estructurales: pedirlos al ritmo del flujo sólo quemaba cuota.
    assert {"vanna", "charm", "max_pain", "iv_rank"} <= set(ENGINE_SLOW_JOBS)
    assert not set(ENGINE_FAST_JOBS) & set(ENGINE_SLOW_JOBS)


def test_skipped_structural_blocks_reuse_their_last_payload():
    """Un bloque lento que no toca en este ciclo es dato vigente, no un hueco."""
    src = text("app/providers/quantdata/runtime.py")
    assert "slow_due" in src and "ENGINE_SLOW_EVERY_N_CYCLES" in src
    assert 'RAW_CACHE.get(name, target.active_symbol, max_age_s=3600.0)' in src


def test_engine_loop_uses_the_quota_derived_interval():
    src = text("app/providers/quantdata/runtime.py")
    assert "QUOTA.recommended_interval(ENGINE_FAST_REQUESTS)" in src


def test_quota_snapshot_never_deadlocks():
    """snapshot() tomaba el lock y luego llamaba a recommended_interval(), que pide
    el mismo lock. threading.Lock no es reentrante: el proceso se colgaba entero en
    cuanto la interfaz pedía la cobertura del proveedor."""
    import threading
    from app.providers.quantdata.shared import QuotaGuard

    q = QuotaGuard()
    q.note(remaining=500, limit=1000, reset_seconds=30)

    done = threading.Event()
    result = {}

    def call():
        result["snap"] = q.snapshot()
        done.set()

    threading.Thread(target=call, daemon=True).start()
    assert done.wait(timeout=5.0), "snapshot() se bloqueó sobre su propio lock"
    assert result["snap"]["engine_interval_seconds"] > 0

"""H4 · resiliencia LIVE de equities: frescura, continuidad visual y Walls."""
from __future__ import annotations

from pathlib import Path

from app.core.alpaca_data import _option_snapshot_values

ROOT = Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_option_freshness_uses_newer_quote_when_last_trade_is_old():
    snap = {
        "latestTrade": {"p": 1.23, "t": "2026-09-18T13:58:00Z"},
        "latestQuote": {"bp": 1.20, "ap": 1.25, "t": "2026-09-18T14:00:07.500Z"},
        "dailyBar": {"v": 10},
    }
    vals = _option_snapshot_values(snap)
    assert vals["option_market_timestamp"] == "2026-09-18T14:00:07.500Z"


def test_option_freshness_still_uses_newer_trade_when_trade_is_newer():
    snap = {
        "latestTrade": {"p": 1.23, "t": "2026-09-18T14:00:08Z"},
        "latestQuote": {"bp": 1.20, "ap": 1.25, "t": "2026-09-18T14:00:07Z"},
    }
    vals = _option_snapshot_values(snap)
    assert vals["option_market_timestamp"] == "2026-09-18T14:00:08Z"


def test_stale_structure_blocks_actionability_not_trace_visibility():
    src = text("app/service.py")
    block = src[src.find("def nextgen_trace("):src.find("def nextgen_surface(")]
    assert "LAST_GOOD_STRUCTURE_CONTEXT_PLUS_OBSERVED_LIVE" in block
    assert 'payload["decision"] = {}' in block
    assert '"context_only": True' in block
    assert 'build_nextgen_trace_payload(' in block
    # La rama de gate ya no debe salir con el payload price-only vacío.
    gate = block[block.find("_structure_blocked ="):block.find("payload = build_nextgen_trace_payload")]
    assert "return _blocked" not in gate
    assert "nextgen_trace_price_only" not in gate


def test_blocked_trace_still_reaches_session_backfill_for_dark_pool_and_flow():
    src = text("app/service.py")
    block = src[src.find("def nextgen_trace("):src.find("def nextgen_surface(")]
    build_at = block.find("payload = build_nextgen_trace_payload")
    backfill_at = block.find("LIVE_TICK_FABRIC_SHORT_COVERAGE")
    assert build_at >= 0 and backfill_at > build_at
    assert 'len(_have) < min(_want_bars, 30)' in block


def test_orderflow_scale_cannot_clip_current_directional_bars():
    src = text("app/static/itmq_orderflow.js")
    net = src[src.find("function drawNetFlow"):src.find("function drawTotal")]
    total = src[src.find("function drawTotal"):src.find("/* -------------------------------------------------------------- API")]
    # La garantía es que el pico REAL de la ventana entra siempre en la escala:
    # el valor suavizado no puede dejar una barra actual fuera del lienzo.
    assert "actualPeak" in net and "actualPeak" in total
    assert "Math.max(smoothed, actualPeak," in net
    assert "actualPeak" in total and "S._totMax.get()" in total

    # v1.46.0 · Lo que SÍ cambió: el suelo dejó de ser «1» —un dólar— porque un
    # suelo absoluto no es multi-activo. En un subyacente de 10⁸ es invisible; en
    # uno de 10² es la escala entera. El suelo es ahora el mínimo representable,
    # que sólo evita la división por cero.
    for lane in (net, total):
        assert "actualPeak, 1)" not in lane, "suelo absoluto en dólares"
        assert "Number.MIN_VALUE" in lane
    # Y un `get()` que todavía no tiene valor no puede envenenar el Math.max:
    # `Math.max(NaN, x)` es NaN, así que el valor se sanea antes de comparar.
    assert "Q.num(S._netMax.get(), 0)" in net or "Q.num(S._netMax.get(), 0)" in src
    assert "Q.num(S._totMax.get(), 0)" in total


def test_live_spot_immediately_enforces_wall_side_in_trace_and_flow():
    trace = text("app/static/itmq_trace.js")
    flow = text("app/static/itmq_orderflow.js")
    assert "Q.num(lastCandle.c, Q.num(prof.spot, NaN))" in trace
    assert "if (l.kind === 'call_wall') return p > spot;" in trace
    assert "if (l.kind === 'put_wall') return p < spot;" in trace
    assert "Q.num(last && last.c, Q.num(payload?.profiles?.spot, NaN))" in flow
    assert "if (l.kind === 'call_wall') return p > spot;" in flow
    assert "if (l.kind === 'put_wall') return p < spot;" in flow


def test_windows_atomic_store_does_not_attempt_directory_fsync():
    src = text("app/core/atomic_store.py")
    assert 'if os.name != "nt":' in src
    assert 'atomic_store:dir_fsync_unsupported' not in src
    assert 'os.replace(tmp, path)' in src
    assert 'os.fsync(fh.fileno())' in src


def test_quantdata_page_lane_is_bounded_and_not_motor_authority():
    src = text("app/providers/quantdata/intelligence.py")
    assert "PAGE_MAX_CONCURRENCY = 2" in src
    # v1.58.0 · LA COTA SIGUE, Y AHORA ES COMPARTIDA.
    #
    # Hasta aquí cada carril tenía su propio semáforo: dos techos independientes
    # para un mismo proveedor, que no son un techo. Nueve peticiones del motor y
    # ocho de páginas podían salir a la vez cumpliendo los dos límites y
    # ahogándose entre ellas. Ahora el número de peticiones VIVAS lo gobierna
    # `shared.GOVERNOR`, con un tope aparte para las pesadas y escalonado entre
    # ellas. Sigue siendo acotado y declarado, nunca ilimitado.
    assert "GOVERNOR.acquire(" in src
    from app.providers.quantdata.shared import GOVERNOR
    assert GOVERNOR.max_inflight >= 1
    assert GOVERNOR.max_heavy_inflight <= GOVERNOR.max_inflight
    # v1.57.4 · La constante ya no es un número a ojo: sale del contrato
    # publicado —20 peticiones por segundo menos la reserva del motor—, así que
    # la ráfaga no puede rozar el límite por mucho que cambie el plan.
    assert "BURST_CONCURRENCY = BURST_LIMIT - ENGINE_RESERVE" in src
    from app.providers.quantdata.intelligence import BURST_CONCURRENCY
    from app.providers.quantdata.shared import BURST_LIMIT, ENGINE_RESERVE
    assert 0 < BURST_CONCURRENCY <= BURST_LIMIT - ENGINE_RESERVE
    # Y la ráfaga está acotada por plazo, alcance y salida anticipada.
    assert "BURST_SECONDS" in src and "BURST_MAX_PRIORITY" in src
    assert "self._burst_until = 0.0" in src
    assert "ENGINE_SHARED_KEYS" in src
    assert "carril del motor" in src


def test_trace_stale_context_is_labeled_not_disguised_as_live():
    src = text("app/static/itmq_app.js")
    assert "d.blocked || d.context_only || d.structure_stale" in src
    assert "RETENIDO" in src


def test_dark_pool_uses_only_confirmed_off_exchange_not_all_large_prints():
    import pandas as pd
    from app.core.large_prints import large_print_summary
    from app import terminal_api

    df = pd.DataFrame([
        {"timestamp": "2026-09-18T10:00:00", "price": 515.0, "size": 8000,
         "notional": 4_120_000.0, "q_print": 80.0, "exchange": "D", "exchange_name": "FINRA TRF",
         "tape": "O", "conditions": "", "off_exchange_confirmed": True},
        {"timestamp": "2026-09-18T10:01:00", "price": 515.1, "size": 9000,
         "notional": 4_635_900.0, "q_print": 85.0, "exchange": "N", "exchange_name": "NYSE",
         "tape": "A", "conditions": "", "off_exchange_confirmed": False},
    ])
    lp = large_print_summary(df, "DIA")
    assert lp["count"] == 2 and lp["off_exchange_count"] == 1
    assert len(lp["off_exchange_top"]) == 1

    out = terminal_api._dark_pool(
        {"spot": 515.05, "large_prints": lp},
        {"candles": [{"t": "2026-09-18T10:00:00", "c": 515.05, "v": 1000}]},
        {},
    )
    assert out["count"] == 1
    assert out["notional"] == 4_120_000.0
    assert len(out["prints"]) == 1
    assert out["prints"][0]["price"] == 515.0
    # v1.42.7 · Quant Data pasa a ser la fuente directa de dark pool. Sin bloque del
    # proveedor (intel={}), la clasificación por venue sigue sosteniendo la vista
    # ella sola: eso es lo que comprueba este caso, y por eso la semántica publicada
    # ya no es "sólo off-exchange confirmado" sino "proveedor con auditoría de venue".
    assert out["semantics"] == "PROVIDER_DIRECT_WITH_VENUE_AUDIT"
    assert out["sources"]["prints"] == "ITM_QUANT_VENUE_CLASSIFICATION"
    assert out["off_exchange_share_source"] == "ITM_QUANT_VENUE_CLASSIFICATION"


def test_dark_pool_ui_says_off_exchange_instead_of_relabeling_every_equity_print():
    html = text("app/templates/terminal.html")
    assert "OFF-EXCHANGE CONFIRMADOS" in html
    assert "PRECIO / TIEMPO · OFF-EXCHANGE CONFIRMADO" in html
    assert "ZONAS OFF-EXCHANGE" in html

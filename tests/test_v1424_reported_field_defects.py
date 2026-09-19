from __future__ import annotations

"""v1.42.4 · Los tres defectos reportados desde la terminal en producción.

Ninguno se veía en la suite: el motor devolvía estructuras bien formadas, con los
campos correctos y valores plausibles. Lo que fallaba era QUÉ tramo de la sesión
cubría el heatmap, DÓNDE lo pintaba el lienzo, y que la prueba de off-exchange
dependiera de un catálogo que puede no llegar nunca.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.engine import analyze_gamma_delta, engine_config_for_asset
from app.core.nextgen_terminal import temporal_heatmap_history
from app.core.large_prints import _is_off_exchange, OFF_EXCHANGE_CODES, large_print_summary

ROOT = Path(__file__).resolve().parents[1]


def _chain(n_snaps: int, spot: float = 600.0) -> pd.DataFrame:
    t0 = datetime(2026, 9, 18, 4, 0, 0)
    rows = []
    for i in range(n_snaps):
        ts = t0 + timedelta(seconds=45 * i)
        for K in np.arange(spot * 0.97, spot * 1.03, 1.0):
            m = np.log(K / spot)
            iv = float(np.clip(0.17 + 0.5 * m * m - 0.4 * m, 0.05, 1.2))
            for ot in ("call", "put"):
                rows.append({"timestamp": ts, "underlying_symbol": "SPY", "underlying_price": spot,
                             "strike": float(K), "option_type": ot, "dte": 2.0,
                             "expiration_date": (t0 + timedelta(days=2)).date().isoformat(),
                             "open_interest": 2500.0, "volume": 400.0, "iv": iv,
                             "contract_multiplier": 100.0})
    return pd.DataFrame(rows)


# ───────────────────────────────── heatmap · cobertura temporal

def test_heatmap_covers_the_whole_session_not_just_the_tail():
    """El defecto: se quedaba con los últimos `max_times` snapshots. Con cadencia de
    ~45 s una sesión deja cientos, así que el heatmap cubría sólo el último tramo y
    el resto del gráfico salía vacío. Agrupar en cubos cubre toda la historia sin que
    el payload crezca.
    """
    gd = analyze_gamma_delta(_chain(400), engine_config_for_asset("SPY", "AUTO"))
    enr = gd["enriched"]
    hm = temporal_heatmap_history(gd, 600.0)
    assert hm["ready"] is True
    assert hm["time_coverage"] == "FULL_SESSION_BUCKETED"
    assert hm["snapshots_available"] == 400
    session = (enr["timestamp"].max() - enr["timestamp"].min()).total_seconds()
    covered = (pd.Timestamp(hm["times"][-1]) - pd.Timestamp(hm["times"][0])).total_seconds()
    assert covered / session > 0.95, f"el heatmap sólo cubre {100*covered/session:.0f}% de la sesión"


def test_heatmap_payload_does_not_grow_with_session_length():
    """La cobertura no puede pagarse con un payload que crece toda la sesión: el panel
    se refresca cada pocos segundos."""
    small = temporal_heatmap_history(analyze_gamma_delta(_chain(60), engine_config_for_asset("SPY", "AUTO")), 600.0)
    big = temporal_heatmap_history(analyze_gamma_delta(_chain(500), engine_config_for_asset("SPY", "AUTO")), 600.0)
    assert big["columns"] <= 120
    assert len(big["gamma_m"][0]) == big["columns"]
    assert small["columns"] <= big["columns"]


def test_bucketed_heatmap_averages_within_a_bucket_and_never_sums():
    """La exposición es un estado en un instante, no un flujo. Sumar los snapshots de
    un cubo multiplicaría el campo por cuántos hayan caído dentro."""
    gd_few = analyze_gamma_delta(_chain(100), engine_config_for_asset("SPY", "AUTO"))
    gd_many = analyze_gamma_delta(_chain(400), engine_config_for_asset("SPY", "AUTO"))
    few = np.array(temporal_heatmap_history(gd_few, 600.0)["gamma_m"])
    many = np.array(temporal_heatmap_history(gd_many, 600.0)["gamma_m"])
    # Misma cadena, sólo más snapshots: la magnitud del campo no puede dispararse.
    assert np.nanmax(np.abs(many)) == pytest.approx(np.nanmax(np.abs(few)), rel=0.25)


def test_heatmap_declares_its_strike_extent():
    """El lienzo necesita saber a qué precios corresponde la imagen para colocarla;
    sin extremos declarados sólo puede estirarla y adivinar."""
    hm = temporal_heatmap_history(analyze_gamma_delta(_chain(30), engine_config_for_asset("SPY", "AUTO")), 600.0)
    assert hm["strike_low"] is not None and hm["strike_high"] is not None
    assert hm["strike_low"] == min(hm["strikes"]) and hm["strike_high"] == max(hm["strikes"])


# ───────────────────────────────── heatmap · geometría del lienzo

def test_canvas_places_the_heatmap_by_coordinates_not_by_stretching():
    """El lienzo estiraba la imagen al rectángulo COMPLETO del gráfico, sin usar en
    ningún momento las coordenadas que representa. Las velas sí se sitúan por tiempo
    (xMapTime) y los niveles por precio (yMap), así que el campo de gamma quedaba
    desalineado de todo lo demás: el strike 520 no caía sobre la línea Call Wall 520.
    """
    js = (ROOT / "app/static/nextgen_terminal.js").read_text(encoding="utf-8")
    i = js.index("drawTemporalHeatmap(c,r,pr){")      # la definición, no el sitio de llamada
    block = js[i:js.index("drawValueField(c,r,pr){", i)]
    assert "this.xMapTime(tFirst" in block, "las columnas deben situarse por tiempo real"
    assert "this.yMap(sHi" in block and "this.yMap(sLo" in block, "las filas deben situarse por precio real"
    assert "c.drawImage(this.heatmapSurface,x0,yTop" in block
    assert "drawImage(this.heatmapSurface,r.left,r.top,r.right-r.left,r.bottom-r.top)" not in block


# ───────────────────────────────── dark pool

def test_the_sip_venue_code_alone_confirms_an_off_exchange_print():
    """El defecto: la única prueba aceptada era que el NOMBRE del venue contuviera
    FINRA/TRF/ADF. Ese nombre sale de un catálogo remoto; si la llamada falla el mapa
    queda vacío, `name` cae al propio código ("D") y la prueba devuelve False para
    siempre. Cero prints off-exchange en todos los activos, sin un error que lo
    explicara — y el dato venía en cada trade.
    """
    assert "D" in OFF_EXCHANGE_CODES          # FINRA ADF en el SIP consolidado
    assert _is_off_exchange("D", "D", "A") is True        # catálogo caído
    assert _is_off_exchange("FINRA/NYSE TRF", "D", "A") is True
    assert _is_off_exchange("", "D", "") is True


def test_a_lit_exchange_is_never_labelled_off_exchange():
    """La otra mitad: la corrección no puede volverse permisiva."""
    for name, code in (("NASDAQ", "Q"), ("NYSE ARCA", "P"), ("NYSE", "N"),
                       ("Cboe BZX", "Z"), ("MEMX", "U"), ("IEX", "V")):
        assert _is_off_exchange(name, code, "A") is False, f"{name}/{code}"


def test_dark_pool_summary_counts_prints_without_any_venue_catalogue():
    """De punta a punta: un tape con sólo el código de venue ya produce sección."""
    tape = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-18 10:00", periods=20, freq="1min"),
        "price": [600.0] * 20,
        "size": [12_000.0] * 20,
        "exchange": ["D"] * 10 + ["Q"] * 10,
        "tape": ["A"] * 20,
    })
    tape["notional"] = tape["price"] * tape["size"]
    tape["off_exchange_confirmed"] = [_is_off_exchange("", c, "A") for c in tape["exchange"]]
    out = large_print_summary(tape, symbol="SPY")
    assert out["off_exchange_count"] == 10
    assert out["off_exchange_notional"] > 0


# ───────────────────────────────── hedge wall duplicado

def test_hedge_wall_does_not_stack_a_second_label_on_the_call_wall():
    from app.core.trace_analytics import structural_walls
    curagg = pd.DataFrame({
        "strike": [440.0, 441.0, 445.0, 446.0],
        "gross_gex": [1.0e6, 2.0e6, 9.0e6, 1.0e6],
        "signed_gex": [-1.0e6, -2.0e6, 9.0e6, 1.0e6],
    })
    w = structural_walls(curagg, 443.0)
    assert w["hedge_wall"] == w["call_wall"]
    assert w["hedge_wall_coincides_with"] == "call_wall"


# ───────────────────────────────── frontend · trampas de ámbito global

def _static(name: str) -> str:
    return (ROOT / "app/static" / name).read_text(encoding="utf-8")


def test_no_two_scripts_define_the_same_global_helper_with_different_signatures():
    """`api` estaba declarada a nivel global en app.js (url, opts) y en
    institutional_terminal.js (sólo url). Cuál quedaba activa dependía del orden de
    los <script>: con el orden invertido, todos los POST (refresh, premarket, replay,
    board) habrían perdido `opts` y se habrían convertido en GET silenciosos, sin un
    solo error en consola.
    """
    pat = re.compile(r"^\s*async function api\s*\(", re.M)
    definers = [n for n in ("app.js", "institutional_terminal.js", "itmq_core.js",
                            "nextgen_terminal.js", "itmq_trace.js")
                if pat.search(_static(n))]
    assert definers == ["app.js"], f"`api` global definida en más de un script: {definers}"


def test_the_two_argument_api_signature_is_the_one_that_survives():
    """Los POST dependen de que `opts` llegue a fetch."""
    assert re.search(r"async function api\(url\s*,\s*opts\s*=\s*\{\}\)", _static("app.js"))


def test_the_anomalies_poller_can_be_stopped():
    """El intervalo de 3 s se asignaba y no se volvía a leer nunca: quedaba vivo el
    resto de la sesión sin forma de pararlo."""
    js = _static("return_anomalies.js")
    assert "clearInterval" in js, "el poller debe poder detenerse"
    assert "stopPolling" in js and "startPolling" in js
    assert "pagehide" in js


def test_every_shipped_script_parses():
    """El gate ya lo comprueba, pero aquí queda atado a la suite."""
    import subprocess, shutil
    node = shutil.which("node") or "/opt/node22/bin/node"
    if not Path(node).exists():
        pytest.skip("node no disponible en este entorno")
    for path in sorted((ROOT / "app/static").glob("*.js")):
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, f"{path.name}: {r.stderr[:200]}"

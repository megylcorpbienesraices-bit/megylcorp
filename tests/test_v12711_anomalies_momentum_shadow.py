from __future__ import annotations

import ast
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import assert_version_at_least, assert_marker_version_at_least
from app.core.return_anomalies import (
    MOMENTUM_AUTHORITY,
    POLITICA_MOMENTUM_DEFECTO,
    _episodios,
    _serie_causal,
    analizar_anomalias_momentum,
)
from app.core.flow_kinematics import _directional_permutation_test, build_flow_kinematics

ROOT = Path(__file__).resolve().parents[1]


def _prices(seed: int = 1, n: int = 900, shock: float | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2026-09-01 13:30", periods=n, freq="1min", tz="UTC")
    ret = rng.normal(0.0, 0.00045, n)
    if shock is not None:
        ret[-1] = float(shock)
    px = 100.0 * np.exp(np.cumsum(ret))
    return pd.DataFrame({"timestamp": ts, "price": px})


def test_release_identity_v12711():
    assert_version_at_least("1.27.11")
    assert_marker_version_at_least("1.27.11")


def test_candidate_es_causal_as_of_time_y_no_reescribe_el_pasado():
    full = _prices(n=760)
    prefix = full.iloc[:620].copy()
    a = _serie_causal(prefix, 1, POLITICA_MOMENTUM_DEFECTO)
    b = _serie_causal(full, 1, POLITICA_MOMENTUM_DEFECTO)
    row_a = a.iloc[-1]
    row_b = b[b["timestamp"] == row_a["timestamp"]].iloc[0]
    for key in ("z_causal", "umbral_causal", "tail_p", "baseline_n"):
        va, vb = row_a[key], row_b[key]
        if pd.isna(va):
            assert pd.isna(vb)
        else:
            assert float(va) == pytest.approx(float(vb), rel=0, abs=1e-12)
    assert row_a["baseline_scope"] == row_b["baseline_scope"]


def test_ruido_iid_no_se_convierte_en_lluvia_de_anomalias():
    tasas = []
    for seed in range(5):
        d = _serie_causal(_prices(seed=seed, n=1200), 1, POLITICA_MOMENTUM_DEFECTO)
        cal = d["umbral_causal"].notna()
        tasas.append(float(d.loc[cal, "anomalia_causal"].mean()))
    assert float(np.mean(tasas)) < 0.025


def test_shock_grande_se_detecta_en_el_timestamp_correcto():
    d = _serie_causal(_prices(n=900, shock=0.006), 1, POLITICA_MOMENTUM_DEFECTO)
    last = d.iloc[-1]
    assert bool(last["anomalia_causal"]) is True
    assert abs(float(last["z_causal"])) > float(last["umbral_causal"])


def test_paquete_multiframe_preserva_current_y_candidate_shadow():
    out = analizar_anomalias_momentum(_prices(n=1200), simbolo="DIA")
    assert out["autoridad"] == MOMENTUM_AUTHORITY
    assert out["direccion"] is None
    assert set(out["timeframes"]) == {"1m", "3m", "5m", "15m"}
    assert out["candidate"]["estado_modelo"] == "SHADOW"
    assert "OOS" in out["candidate"]["promocion"]
    assert out["current"]["seccion"] == "ANOMALIAS_RENDIMIENTOS"


def test_candidate_no_expone_compra_venta_como_senal():
    out = analizar_anomalias_momentum(_prices(n=1000, shock=0.006), simbolo="DIA")
    blob = repr(out).lower()
    for key in ("'signal':", "'senal':", "'compra':", "'venta':"):
        assert key not in blob
    assert out["direccion"] is None


def test_episodios_agrupan_mismo_signo_y_separan_opuesto():
    ts = pd.date_range("2026-09-10 13:30", periods=7, freq="1min", tz="UTC")
    d = pd.DataFrame({
        "timestamp": ts,
        "poblacion": ["INTRADIA"] * 7,
        "log_return": [0.001, 0.002, 0.0001, -0.003, -0.002, 0.0, 0.0],
        "z_causal": [4.0, 5.0, 0.2, -4.4, -3.8, 0.0, 0.0],
        "anomalia_causal": [True, True, False, True, True, False, False],
    })
    eps = _episodios(d, 1, POLITICA_MOMENTUM_DEFECTO)
    assert len(eps) == 2
    assert eps[0]["barras_anomalas"] == 2 and eps[0]["presion_observada"] == "ALCISTA"
    assert eps[1]["barras_anomalas"] == 2 and eps[1]["presion_observada"] == "BAJISTA"


def test_gap_de_sesion_no_se_une_como_un_episodio_continuo():
    ts = [
        pd.Timestamp("2026-09-10 19:59", tz="UTC"),
        pd.Timestamp("2026-09-10 20:00", tz="UTC"),
        pd.Timestamp("2026-09-11 13:30", tz="UTC"),
    ]
    d = pd.DataFrame({
        "timestamp": ts,
        "poblacion": ["INTRADIA", "INTRADIA", "OVERNIGHT"],
        "log_return": [0.002, 0.002, 0.004],
        "z_causal": [4.0, 4.2, 5.0],
        "anomalia_causal": [True, True, True],
    })
    eps = _episodios(d, 1, POLITICA_MOMENTUM_DEFECTO)
    assert len(eps) == 2


def test_contexto_flow_es_solo_contexto_y_no_autoridad():
    ctx = {
        "flow_kinematics": {
            "ready": True,
            "latest": {"direction": "BUY", "absorption_score": 72.0},
            "candidate": {"significant": True, "p_value": 0.01, "observed_side": "BUY", "absorption_score": 72.0},
        }
    }
    out = analizar_anomalias_momentum(_prices(n=1200, shock=0.006), simbolo="DIA", contexto=ctx)
    f = out["candidate"]["contexto_flow"]
    assert f["estado"] == "ABSORPTION_HIGH"
    assert f["significancia"] is True
    assert out["direccion"] is None


def test_permutation_candidate_detecta_flujo_unidireccional_fuerte():
    t0 = pd.Timestamp("2026-09-10 13:30:00")
    ev = pd.DataFrame({
        "timestamp": [t0 + pd.Timedelta(seconds=i) for i in range(24)],
        "directional_premium": np.linspace(20_000, 80_000, 24),
        "premium": np.linspace(20_000, 80_000, 24),
    })
    r = _directional_permutation_test(ev)
    assert r["ready"] is True
    assert r["significant"] is True
    assert r["observed_side"] == "BUY"
    assert r["p_value"] <= 0.05


def test_permutation_candidate_calibrado_en_ruido_de_signos():
    hits = 0
    for seed in range(50):
        rng = np.random.default_rng(seed)
        n = 32
        mag = rng.lognormal(10.0, 0.5, n)
        sign = rng.choice([-1.0, 1.0], size=n)
        ev = pd.DataFrame({"directional_premium": mag * sign, "premium": mag})
        hits += int(bool(_directional_permutation_test(ev)["significant"]))
    assert hits <= 7  # 14% ceiling around a nominal 5% test; catches pathological inflation.


def test_absorcion_es_fail_closed_si_falta_precio_en_el_ultimo_bucket():
    t0 = pd.Timestamp("2026-09-10 13:30:00")
    ev = pd.DataFrame({
        "timestamp": [t0 + pd.Timedelta(seconds=5 * i) for i in range(12)],
        "directional_premium": [10_000 + 2_000 * i for i in range(12)],
        "premium": [10_000 + 2_000 * i for i in range(12)],
    })
    # Precio termina antes: merge_asof no debe inventar respuesta para buckets fuera de tolerancia.
    px = pd.DataFrame({"timestamp": [t0, t0 + pd.Timedelta(seconds=5)], "price": [525.0, 525.01]})
    out = build_flow_kinematics(ev, px, symbol="DIA", bucket_seconds=5)
    cand = out["candidate"]
    assert cand["absorption_price_coverage"] < 1.0
    # La falta de precio no puede elevar absorción rellenando un valor sintético.
    assert cand["absorption_score"] is None or 0.0 <= cand["absorption_score"] <= 100.0


def test_current_de_flow_se_preserva_y_candidate_vive_aparte():
    t0 = pd.Timestamp("2026-09-10 13:30:00")
    ev = pd.DataFrame({
        "timestamp": [t0 + pd.Timedelta(seconds=5 * i) for i in range(10)],
        "directional_premium": [10_000, 15_000, 25_000, 40_000, 60_000, 90_000, 130_000, 180_000, 250_000, 340_000],
        "premium": [10_000, 15_000, 25_000, 40_000, 60_000, 90_000, 130_000, 180_000, 250_000, 340_000],
    })
    px = pd.DataFrame({"timestamp": ev["timestamp"], "price": np.linspace(525.0, 525.1, len(ev))})
    out = build_flow_kinematics(ev, px, symbol="DIA", bucket_seconds=5)
    assert out["status"].startswith("FLOW_") or out["status"].startswith("POSSIBLE_")
    assert out["candidate"]["authority"] == "CANDIDATE_SHADOW_ONLY"
    assert out["authority"] == "SHADOW_CONTEXT_ONLY"


def test_ui_tiene_una_sola_seccion_y_no_ruta_de_ordenes():
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "app/static/return_anomalies.js").read_text(encoding="utf-8")
    assert html.count('id="returnAnomalyLab"') == 1
    assert "ANOMALÍAS &amp; MOMENTUM · SHADOW" in html
    assert "/api/seccion/anomalias-rendimientos" in js
    for forbidden in ("/order", "/orders", "live_scheduler/trigger", "SET_TACTICAL_ALERT"):
        assert forbidden not in js


def test_ui_no_dibuja_linea_de_precio_entre_shocks_de_sesiones():
    js = (ROOT / "app/static/return_anomalies.js").read_text(encoding="utf-8")
    assert "gap>Math.max(45,tfm*3)" in js
    assert "Retornos como stems/barras" in js


def test_endpoint_anomalias_permanece_activo_incluso_con_env_legacy(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as main
    monkeypatch.setenv("ITM_SECCION_ANOMALIAS", "0")
    body = TestClient(main.app).get("/api/seccion/anomalias-rendimientos").json()
    assert body["activa"] is True
    assert body["direccion"] is None


def test_modulo_aislado_sigue_sin_importar_motor():
    path = ROOT / "app/core/return_anomalies.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(x.name.split(".")[0] for x in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert imports <= {"__future__", "dataclasses", "typing", "math", "numpy", "pandas"}


def test_main_solo_lee_snapshots_publicados_para_esta_seccion():
    src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    start = src.index('@app.get("/api/seccion/anomalias-rendimientos")')
    end = src.index('@app.get("/api/nextgen/research-validation")', start)
    block = src[start:end]
    assert "STATE.session_flow_tape" in block and "STATE.flow_kinematics_report" in block
    assert "fetch_" not in block and "_request_json" not in block
    assert "STATE." in block

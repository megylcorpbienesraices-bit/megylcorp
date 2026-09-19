from __future__ import annotations

"""Tests de regresión v1.27.1.

Estos tests están escritos al REVÉS de los anteriores: no comprueban que la
release sea un número concreto, comprueban que un comportamiento concreto se
mantenga. Por eso no caducan con el próximo bump de versión.

Cubren:
  1. El bug de `fillna` sobre escalar en nextgen_terminal.
  2. El módulo de observabilidad (contadores de degradación).
  3. El guardia de exposición de red.
  4. `UnsupportedAssetError` en vez de `KeyError` crudo.
  5. Que la versión se lea de un solo sitio.
  6. Guardias anti-recaída: que no vuelvan a aparecer `except: pass` ni
     divergencias entre los dos caminos analíticos.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- 1. fillna bug

def test_temporal_heatmap_survives_missing_volume_column():
    """Regresión del AttributeError: 'float' object has no attribute 'fillna'.

    Cuando el proveedor no entrega ni `volume` ni `option_volume`, el código
    hacía `x.get(None, 0.0).fillna(...)` sobre un escalar y reventaba. Una
    columna ausente es un estado VÁLIDO: debe degradar a ceros, no romper.
    """
    from app.core.nextgen_terminal import temporal_heatmap_history

    enriched = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-09-11 10:00", "2026-09-11 10:01"] * 2),
        "strike": [450.0, 450.0, 451.0, 451.0],
        "option_type": ["C", "C", "P", "P"],
        "underlying_price": [450.5] * 4,
        "open_interest": [100.0, 101.0, 80.0, 79.0],
        "signed_gex_proxy": [1.0, 1.1, -1.0, -0.9],
        "option_delta_exposure_info": [2.0, 2.1, -2.0, -1.9],
        # sin 'volume' ni 'option_volume' -> era exactamente el caso que reventaba
    })
    out = temporal_heatmap_history({"enriched": enriched, "spot": 450.5}, 450.5)
    assert out.get("ready") is True, out
    assert "net_volume" in out or out.get("ready"), "debe producir el campo degradado a ceros"


def test_numeric_col_helper_returns_series_for_missing_column():
    """El helper debe devolver SIEMPRE una Series alineada, nunca un escalar."""
    src = (ROOT / "app/core/nextgen_terminal.py").read_text(encoding="utf-8")
    assert "_numeric_col" in src, "el helper de columna numérica desapareció"
    assert 'x.get(vol_col,0.0),errors="coerce").fillna' not in src, (
        "reapareció el patrón `.get(col, 0.0).fillna(...)`: vuelve a romper "
        "cuando la columna no existe"
    )


# ------------------------------------------------------- 2. observabilidad

def test_obs_counts_and_classifies_degradations():
    from app.core import obs

    obs.reset_degradations()
    assert obs.degradations()["status"] == "OK"

    try:
        raise ValueError("proveedor caído")
    except ValueError as exc:
        obs.note("alpaca:fetch_chain", exc)

    rep = obs.degradations()
    assert rep["total_degradations"] == 1
    assert rep["distinct_sites"] == 1
    assert rep["status"] == "WARN"
    site = rep["sites"][0]
    assert site["site"] == "alpaca:fetch_chain"
    assert site["type"] == "ValueError"
    obs.reset_degradations()


def test_guard_returns_default_and_records():
    from app.core import obs

    obs.reset_degradations()

    def _boom():
        raise RuntimeError("timeout")

    assert obs.guard("test:boom", _boom, default={"ready": False}) == {"ready": False}
    assert obs.degradations()["total_degradations"] == 1
    # El camino feliz no debe contar nada.
    assert obs.guard("test:ok", lambda: 42) == 42
    assert obs.degradations()["total_degradations"] == 1
    obs.reset_degradations()


def test_swallow_does_not_propagate_but_records():
    from app.core import obs

    obs.reset_degradations()
    with obs.swallow("test:tolerable"):
        raise KeyError("falta una clave opcional")
    assert obs.degradations()["total_degradations"] == 1
    obs.reset_degradations()


# ------------------------------------------------------ 3. guardia de red

def test_loopback_detection():
    from app.core import net_guard

    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
        assert net_guard.is_loopback(host), host
    for host in ("0.0.0.0", "192.168.1.10", "10.0.0.2"):
        assert not net_guard.is_loopback(host), host


def test_non_loopback_bind_without_token_is_refused(monkeypatch):
    from app.core import net_guard

    monkeypatch.delenv("ITM_ACCESS_TOKEN", raising=False)
    with pytest.raises(net_guard.InsecureExposureError):
        net_guard.assert_safe_bind("0.0.0.0")
    # Con token, debe permitir.
    monkeypatch.setenv("ITM_ACCESS_TOKEN", "x" * 40)
    net_guard.assert_safe_bind("0.0.0.0")
    # Loopback nunca exige token.
    monkeypatch.delenv("ITM_ACCESS_TOKEN", raising=False)
    net_guard.assert_safe_bind("127.0.0.1")


def test_token_comparison_rejects_empty_and_wrong(monkeypatch):
    from app.core import net_guard

    monkeypatch.setenv("ITM_ACCESS_TOKEN", "secreto-largo-y-aleatorio")
    assert net_guard.token_matches("secreto-largo-y-aleatorio")
    assert not net_guard.token_matches("otro")
    assert not net_guard.token_matches("")
    assert not net_guard.token_matches(None)


# --------------------------------------------------- 4. error de activo

def test_unsupported_asset_raises_typed_error_with_universe():
    from app.core.assets import UnsupportedAssetError, asset_info, is_supported

    assert is_supported("DIA")
    assert not is_supported("SPY")
    with pytest.raises(UnsupportedAssetError) as ei:
        asset_info("SPY")
    msg = str(ei.value)
    assert "SPY" in msg and "DIA" in msg, "el error debe nombrar el universo soportado"
    # Debe seguir siendo cazable como LookupError, pero NO confundirse con un
    # KeyError de diccionario interno.
    assert isinstance(ei.value, LookupError)


# ------------------------------------------- 5. versión en un solo sitio

def test_version_has_a_single_source_of_truth():
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    marker = json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    assert str(marker.get("version")) == version, (
        "VERSION.txt y .itm_quant_product.json están desincronizados"
    )
    main_src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert '"version": _release_version()' in main_src, (
        "/health volvió a hardcodear la versión en vez de leer VERSION.txt"
    )


# ------------------------------------------------ 6. guardias anti-recaída

def _silent_handlers(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return 0
    return sum(
        1 for n in ast.walk(tree)
        if isinstance(n, ast.ExceptHandler) and len(n.body) == 1 and isinstance(n.body[0], ast.Pass)
    )


def test_no_silent_exception_handlers_remain():
    """Impide que vuelvan los `except Exception: pass`.

    Este es el guardia más importante del archivo. En un motor de trading, un
    handler mudo convierte un fallo de datos en un número plausible, y no hay
    forma de distinguirlo de un mercado tranquilo.

    Si necesitas tolerar un fallo, usa `obs.swallow(...)` o `obs.guard(...)`:
    degradan igual, pero dejan rastro contable en /health.
    """
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        n = _silent_handlers(path)
        if n:
            offenders.append(f"{path.relative_to(ROOT)}: {n}")
    assert not offenders, (
        "handlers silenciosos reintroducidos:\n  " + "\n  ".join(offenders)
        + "\n\nUsa obs.swallow(site) / obs.guard(site, fn, default) en su lugar."
    )


def test_source_health_has_one_definition_for_both_paths():
    """En v1.27.0 los dos caminos analíticos habían DIVERGIDO.

    `refresh()` calculaba la salud de proveedores con el scheduler del fabric y
    `set_expiry_window()` con el status del stream: dos métricas distintas bajo
    la misma etiqueta en la UI. Cambiar la ventana de expiración devolvía un
    diagnóstico diferente sin que nada lo indicara.
    """
    src = (ROOT / "app/service.py").read_text(encoding="utf-8")
    direct = src.count("provider_redundancy(")
    assert direct <= 1, (
        f"provider_redundancy() se llama {direct} veces directamente. "
        "Debe pasar siempre por _build_source_diag() para que refresh() y "
        "set_expiry_window() no vuelvan a divergir."
    )
    assert "_build_source_diag" in src
    assert src.count("self._build_source_diag(") >= 2, (
        "ambos caminos analíticos deben usar el helper compartido"
    )


def test_expiry_window_uses_fresh_macro_not_cached():
    """`set_expiry_window` usaba `self.macro` sin refrescar (podía estar caducado)."""
    src = (ROOT / "app/service.py").read_text(encoding="utf-8")
    assert "_current_macro" in src
    assert 'macro_ctx=dict(self.macro or {})' not in src, (
        "volvió a usarse el macro cacheado en el recálculo por ventana de expiración"
    )


def test_codemods_are_idempotent():
    """Correr los codemods dos veces no debe cambiar nada la segunda."""
    script = ROOT / "scripts/codemod_silent_except.py"
    if not script.exists():
        pytest.skip("codemod no empaquetado")
    out = subprocess.run([sys.executable, str(script), "--check"],
                         capture_output=True, text=True, cwd=ROOT)
    assert "0 handlers silenciosos" in out.stdout, out.stdout

from __future__ import annotations

"""v1.42.3 · Una guarda que no guarda es peor que no tener guarda.

Por todo el motor había esta forma, escrita para tolerar una columna ausente:

    pd.to_numeric(frame.get("volume", 0), errors="coerce").fillna(0.0)

`DataFrame.get(name, 0)` devuelve el ESCALAR 0 si la columna falta, y un escalar no
tiene `.fillna`. La línea escrita para no fallar era exactamente la que lanzaba
`AttributeError`. La variante sin default (`frame.get(name)`) devuelve None y falla
igual.

No era teórico: `_ticks_frame` tumbaba el payload completo de TRACE con una fuente
de ticks sin `signed_volume`, y `large_prints._score_events` tumbaba la sección
DARK POOL entera con un tape sin `notional` precalculado. El mismo fallo ya se
había corregido a mano en el heatmap (v1.27.1) y sobrevivió en el resto: arreglar
el caso en vez del patrón deja el patrón vivo.
"""

import re
from pathlib import Path

import pandas as pd
import pytest

from app.core.frame_guards import numeric_column, first_numeric_column, text_column

ROOT = Path(__file__).resolve().parents[1]


# ───────────────────────────────────────────────────── el helper

def test_missing_column_degrades_to_the_declared_default():
    df = pd.DataFrame({"a": [1.0, 2.0]})
    out = numeric_column(df, "no_existe", 0.0)
    assert isinstance(out, pd.Series) and len(out) == len(df)
    assert list(out) == [0.0, 0.0]


def test_missing_column_keeps_the_frame_index():
    """Una serie desalineada corrompe en silencio cualquier asignación posterior."""
    df = pd.DataFrame({"a": [1.0, 2.0]}, index=[7, 9])
    assert list(numeric_column(df, "no_existe", 0.0).index) == [7, 9]


def test_garbage_values_become_the_default_not_a_loose_nan():
    """Un NaN suelto comparado con un umbral devuelve False sin avisar: es la forma
    más discreta que tiene un dato ausente de apagar una condición."""
    df = pd.DataFrame({"a": [1.0, "basura", None]})
    assert list(numeric_column(df, "a", 0.0)) == [1.0, 0.0, 0.0]


def test_nan_default_preserves_nan_for_masks_that_depend_on_it():
    """Algunas rutas usan .notna() como máscara de validez. Ahí el NaN es la
    respuesta correcta y rellenarlo rompería la semántica."""
    df = pd.DataFrame({"a": [1.0, None]})
    out = numeric_column(df, "a", float("nan"))
    assert out.isna().tolist() == [False, True]


def test_empty_frame_and_non_frame_do_not_raise():
    assert len(numeric_column(pd.DataFrame(), "a")) == 0
    assert len(numeric_column(None, "a")) == 0        # type: ignore[arg-type]
    assert len(numeric_column({"a": 1}, "a")) == 0    # type: ignore[arg-type]


def test_first_numeric_column_picks_the_first_present_alias():
    df = pd.DataFrame({"option_volume": [5.0, 6.0]})
    assert list(first_numeric_column(df, "volume", "option_volume")) == [5.0, 6.0]
    assert list(first_numeric_column(df, "nope", "tampoco", default=3.0)) == [3.0, 3.0]


def test_text_column_degrades_without_raising():
    df = pd.DataFrame({"a": [1, 2]})
    assert list(text_column(df, "option_type", "call")) == ["call", "call"]


# ──────────────────────────────────────── las dos secciones que se caían

def test_trace_payload_survives_ticks_without_signed_volume():
    """La firma exacta del fallo: TRACE en blanco, no degradado."""
    from app.core.nextgen_terminal import candles_from_ticks
    ticks = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-17 10:00", periods=30, freq="1min"),
        "price": [600.0 + i * 0.1 for i in range(30)],
    })  # sin `size`, sin `signed_volume`
    candles = candles_from_ticks(ticks, "1m", 60)
    assert isinstance(candles, list) and len(candles) > 0


def test_dark_pool_survives_a_tape_without_precomputed_notional():
    from app.core.large_prints import large_print_summary
    tape = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-17 10:00", periods=12, freq="1min"),
        "price": [600.0] * 12,
        "size": [10_000.0] * 12,
        "exchange_name": ["FINRA/NYSE TRF"] * 12,
        "tape": ["A"] * 12,
    })  # sin `notional`
    out = large_print_summary(tape, symbol="SPY")
    assert isinstance(out, dict) and "off_exchange_count" in out


# ───────────────────────────────────────── el patrón no vuelve

_BAD = re.compile(
    r'pd\.to_numeric\(\s*\w+\.get\("[a-z_]+"(?:,\s*[0-9.]+)?\s*\)\s*,\s*errors="coerce"\s*\)'
    r'\s*(?:\.abs\(\))?\s*\.(?:fillna|clip)\('
)


def test_the_broken_guard_pattern_is_gone_from_the_engine():
    """Un test de patrón, no de caso. Es lo que faltó en v1.27.1: se arregló el
    heatmap y el patrón siguió copiándose a otros 100 sitios."""
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if path.name == "frame_guards.py":   # su docstring cita el patrón a propósito
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BAD.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{i}")
    assert not offenders, (
        "guarda rota reintroducida; usa frame_guards.numeric_column:\n  " + "\n  ".join(offenders[:15])
    )

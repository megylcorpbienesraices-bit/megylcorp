"""Regresión del bug de resolución temporal de pandas 3.0 (v1.27.3).

El bug
------
pandas 3.0 cambió la resolución por defecto de `datetime64[ns]` a `datetime64[us]`.
El idioma `serie.astype("int64") // 1_000_000` asume nanosegundos: con
microsegundos devuelve SEGUNDOS, valores 1000× mayores, sin lanzar error.

Medido en v1.27.2 con pandas 3.0.2:

    _bucket250   250 ms  ->  250 s   (4.2 min)
    _bucket75     75 ms  ->   75 s   (1.25 min)
    Hawkes        60 s   ->  16.7 h ; tau 10 s -> 10 000 s

Consecuencia operativa: un sweep es un fenómeno de milisegundos entre venues.
Agrupar 4.2 minutos de flujo sin relación como un solo SWEEP_CANDIDATE produce
falsos positivos masivos justo en el módulo que alimenta el Aggression Trigger.

Por qué era tan peligroso
-------------------------
Latente en pandas 2.x, activo en pandas 3.x, y `requirements.txt` declaraba
`pandas>=2.0` sin fijar. Funcionaba en la máquina de desarrollo y se habría roto
en la instalación limpia del VPS — el paso siguiente del plan.

`pd.Timestamp.value` (escalar) SÍ devuelve nanosegundos siempre, así que
`causality_engine`, `provider_flow_fabric` y `temporal_causality` nunca
estuvieron afectados. El fallo es exclusivo de `Series.astype("int64")`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
import json

import numpy as np
import pandas as pd
import pytest

from app.core import timeunits as tu

ROOT = Path(__file__).resolve().parents[1]
BASE = pd.Timestamp("2026-09-08 10:00:00")


# --------------------------------------------- el helper es agnóstico a la unidad

@pytest.mark.parametrize("unit", ["ns", "us", "ms"])
def test_epoch_ms_is_correct_in_every_pandas_resolution(unit):
    s = pd.Series([BASE, BASE + pd.Timedelta(milliseconds=100)]).astype(f"datetime64[{unit}]")
    ms = tu.epoch_ms(s)
    assert int(ms.iloc[1] - ms.iloc[0]) == 100, (
        f"con dtype datetime64[{unit}] la diferencia de 100 ms se calculó mal"
    )


@pytest.mark.parametrize("unit", ["ns", "us", "ms"])
def test_bucket_width_is_real_milliseconds(unit):
    """Un bucket de 250 ms debe separar marcas a 300 ms y unir marcas a 100 ms."""
    s = pd.Series([
        BASE,
        BASE + pd.Timedelta(milliseconds=100),
        BASE + pd.Timedelta(seconds=30),
    ]).astype(f"datetime64[{unit}]")
    b = tu.bucket(s, 250)
    assert b.iloc[0] == b.iloc[1], "100 ms deben caer en el mismo bucket de 250 ms"
    assert b.iloc[2] != b.iloc[0], (
        "30 segundos NO pueden caer en el mismo bucket de 250 ms. "
        "Si este assert falla, el bucket volvió a medir segundos en vez de milisegundos."
    )


@pytest.mark.parametrize("tz", [None, "UTC", "America/New_York", "America/Guayaquil"])
def test_timezone_aware_input_is_handled(tz):
    """El `astype("int64")` original toleraba tz-aware; el reemplazo también debe.

    Restar un Timestamp(0) tz-naive de una serie tz-aware lanza TypeError: fue un
    fallo real de la primera versión de este helper, detectado por la suite.
    """
    s = pd.Series([BASE, BASE + pd.Timedelta(milliseconds=100)])
    if tz:
        s = s.dt.tz_localize(tz)
    ms = tu.epoch_ms(s)
    assert int(ms.iloc[1] - ms.iloc[0]) == 100


def test_nat_gets_an_isolated_bucket_and_never_joins_real_data():
    b = tu.bucket(pd.Series([BASE, pd.NaT, BASE]), 250)
    assert b.iloc[0] == b.iloc[2]
    assert b.iloc[1] != b.iloc[0], "una marca no parseable no puede agruparse con datos reales"
    assert tu.epoch_seconds(pd.Series([BASE, pd.NaT])).isna().iloc[1]


def test_epoch_seconds_preserves_subsecond_resolution():
    s = pd.Series([BASE, BASE + pd.Timedelta(milliseconds=250)])
    secs = tu.epoch_seconds_array(s)
    assert np.isclose(secs[1] - secs[0], 0.25), "se perdió la parte fraccionaria"


def test_bucket_rejects_non_positive_width():
    with pytest.raises(ValueError):
        tu.bucket(pd.Series([BASE]), 0)


# ------------------------------------------- el clasificador vuelve a funcionar

def _trade(ts, contract="DIA20260909C00100000", exchange="CBOE", aggressor="BUY", contracts=60):
    return {
        "timestamp": ts, "underlying_symbol": "DIA", "contract_symbol": contract,
        "exchange": exchange, "aggressor": aggressor, "contracts": float(contracts),
        "price": 1.20, "premium": float(contracts) * 120.0, "size": float(contracts),
        "strike": 100.0, "expiration_date": "2026-09-09", "option_type": "C",
    }


def test_sweep_and_multileg_are_distinguished_not_collapsed():
    """Regresión directa del bug: las 4 filas salían todas MULTI_LEG_CANDIDATE.

    Con buckets de 250 s y 75 s, operaciones separadas por más de un segundo caían
    en el mismo grupo y la pasada de multi-leg pisaba la de sweep.
    """
    from app.core.dealer_microstructure import enrich_option_packages

    rows = [_trade(BASE + pd.Timedelta(milliseconds=ms), exchange=v)
            for ms, v in [(0, "CBOE"), (100, "ISE")]]
    a = _trade(BASE + pd.Timedelta(seconds=1), contracts=80)
    b = _trade(BASE + pd.Timedelta(seconds=1, milliseconds=40),
               contract="DIA20260909C00101000", aggressor="SELL", contracts=75)
    b["strike"] = 101.0
    out = enrich_option_packages(pd.DataFrame(rows + [a, b]))

    types = set(out["package_type"])
    assert "SWEEP_CANDIDATE" in types, (
        "la ráfaga entre CBOE/ISE en 100 ms dejó de detectarse como sweep"
    )
    assert "MULTI_LEG_CANDIDATE" in types


def test_trades_minutes_apart_are_never_one_package():
    """La prueba que el bug hacía imposible pasar.

    Con el bucket roto, dos operaciones separadas 3 minutos compartían bucket.
    """
    from app.core.dealer_microstructure import enrich_option_packages

    rows = [
        _trade(BASE, exchange="CBOE"),
        _trade(BASE + pd.Timedelta(minutes=3), exchange="ISE"),
    ]
    out = enrich_option_packages(pd.DataFrame(rows))
    assert set(out["package_type"]) <= {"SINGLE", "BLOCK_CANDIDATE"}, (
        "dos operaciones a 3 minutos de distancia se agruparon como un paquete"
    )


# ------------------------------------------------ guardia: el idioma no vuelve

UNSAFE = re.compile(r'astype\(\s*["\']int64["\']\s*\)\s*(//|/)\s*(1_000_000|1000000|1e6|1e9|1_000_000_000)')


def test_no_module_assumes_nanosecond_resolution():
    """Prohíbe reintroducir `serie.astype("int64") // 1_000_000`.

    Es el idioma que rompió sweep, multi-leg y Hawkes. Usa
    `timeunits.epoch_ms`, `epoch_seconds` o `bucket`, que funcionan en ns, us y ms.
    """
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "timeunits.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if UNSAFE.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{i}")
    assert not offenders, (
        "se asumió resolución de nanosegundos en:\n  " + "\n  ".join(offenders)
        + "\n\nUsa app.core.timeunits (epoch_ms / epoch_seconds / bucket)."
    )


def test_pandas_is_pinned_to_a_tested_major():
    """Pandas must remain an exact, hashed 2.x pin in the canonical production lock.

    `requirements.txt` is now only a compatibility pointer to the hash-pinned lock;
    duplicating pandas there would create a second dependency source of truth.
    """
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").strip()
    assert req == "-r requirements.production.lock.txt"
    lock = (ROOT / "requirements.production.lock.txt").read_text(encoding="utf-8")
    match = re.search(r"(?m)^pandas==([^\s\\]+)\s*\\(?P<body>(?:\n\s+--hash=sha256:[0-9a-f]+\s*\\?)+)", lock)
    assert match, "pandas debe estar fijado con hashes en requirements.production.lock.txt"
    version = match.group(1)
    assert version.split(".", 1)[0] == "2", f"major de pandas no probada: {version}"
    assert "--hash=sha256:" in match.group("body")


def test_every_version_declaration_is_in_sync():
    """All release declarations must follow VERSION.txt."""
    import json as _json

    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    marker = _json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    assert str(marker["version"]) == version, "marker de producto desincronizado"

    package = _json.loads((ROOT / "frontend/solid-shell/package.json").read_text(encoding="utf-8"))
    assert str(package.get("version")) == version, "frontend/solid-shell/package.json desincronizado"

    for cargo in sorted((ROOT / "rust").glob("*/Cargo.toml")):
        declared = next(
            (l.split("=", 1)[1].strip().strip('\"')
             for l in cargo.read_text(encoding="utf-8").splitlines()
             if l.startswith("version")),
            None,
        )
        assert declared == version, (
            f"{cargo.relative_to(ROOT)} declara {declared!r} pero la release es {version!r}"
        )

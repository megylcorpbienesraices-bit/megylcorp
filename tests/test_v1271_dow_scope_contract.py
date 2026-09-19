"""Contrato del universo MULTI_ASSET (v1.27.1 → reescrito en v1.42).

Qué pasó
--------
v1.27.0 estrechó el universo visible de un catálogo multi-activo a los
instrumentos directos del Dow. Los tests antiguos seguían afirmando el catálogo
ancho y fallaban:

    assert 'NVDA' in ASSETS                       # test_v114_dealer_institutional
    public_summary('SPY') / public_summary('QQQ') # test_v12515, test_v1255
    eco['architecture'] == 'PRIMARY + ETF/EQUITY + INDEX + FUTURE + DERIVATIVES'
    'DJI' in public_summary('DIA')['indices']

La evidencia de que fue DELIBERADO, no un borrado accidental:
  - `.itm_quant_product.json` declara `scope: MULTI_ASSET`.
  - `assets.py` documenta *"VISIBLE UNIVERSE = DIRECT DOW INSTRUMENTS ONLY"*.
  - `asset_ecosystems` devuelve `architecture = 'MULTI_ASSET · …'`.
  - `feed_adapters.descriptors()` habla de *"this DOW-specialized build"*.

Cuatro sitios independientes dicen lo mismo. Eso es una decisión, no un despiste.

Dos mejoras reales que el estrechamiento trajo
----------------------------------------------
1. `indices` de DIA es ahora **DJX**, no DJI. DJX (DJIA/100) es el índice
   **operable con opciones**; DJI es el índice crudo, sin cadena. La versión
   anterior invitaba a pedir una cadena que no existe.
2. `public_summary('SPY')` degrada a `family='UNSUPPORTED'` en vez de reventar,
   mientras que `asset_info('SPY')` sí lanza `UnsupportedAssetError`. La
   distinción es correcta: consultar el ecosistema es informativo, entrar al
   pipeline con un símbolo no soportado es un error.

SI DECIDES RE-EXPANDIR el universo, este archivo es el contrato que hay que
cambiar: cada test dice qué debe cumplirse para que un símbolo nuevo entre.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.assets import (
    ASSETS,
    DEFAULT_ASSET,
    UnsupportedAssetError,
    asset_info,
    is_supported,
    selectable_assets,
)
from app.core.asset_ecosystems import public_summary

ROOT = Path(__file__).resolve().parents[1]

DOW_UNIVERSE = {"DIA", "DJX", "YM", "MYM", "XLI", "XLF", "VIX", "VXD"}
# Símbolos del catálogo ancho anterior. Si alguno vuelve, debe traer proveedor,
# ventana y política de expiración propias, no heredarlas de DIA.
RETIRED_SYMBOLS = {"NVDA", "MSFT", "META", "AMZN", "TSLA", "SPY", "QQQ", "GLD", "VXX", "IWM"}


# --------------------------------------------------- el universo es el que es

def test_visible_universe_is_exactly_the_dow_set():
    assert set(ASSETS) == DOW_UNIVERSE, (
        f"el universo visible cambió: {sorted(set(ASSETS) ^ DOW_UNIVERSE)}. "
        "Si es intencional, actualiza DOW_UNIVERSE y el CHANGELOG."
    )
    assert DEFAULT_ASSET in ASSETS


def test_product_marker_declares_the_same_scope():
    """El alcance no puede declararse en un sitio y contradecirse en otro."""
    marker = json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    assert marker["scope"] == "MULTI_ASSET"


@pytest.mark.parametrize("sym", sorted(RETIRED_SYMBOLS))
def test_retired_symbols_raise_typed_error_not_silent_fallback(sym):
    """Un símbolo fuera del universo debe FALLAR, nunca caer en DIA por defecto.

    El fallo peligroso sería sustituir silenciosamente: pedir SPY y recibir
    matemática de DIA presentada como SPY. El error tipado lo hace imposible.
    """
    assert not is_supported(sym)
    with pytest.raises(UnsupportedAssetError):
        asset_info(sym)


def test_unsupported_error_names_the_visible_universe():
    with pytest.raises(UnsupportedAssetError) as ei:
        asset_info("SPY")
    msg = str(ei.value)
    for sym in ("DIA", "DJX", "YM"):
        assert sym in msg, "el error debe ser accionable: listar qué SÍ se puede pedir"


# ------------------------------------------------ cada instrumento es él mismo

def test_every_asset_declares_its_own_provider_and_window():
    """Ningún instrumento puede heredar identidad de otro.

    Es el invariante central del rediseño: DIA nunca sustituye a YM/DJX.
    """
    for sym, cfg in ASSETS.items():
        assert cfg.get("market_provider") or cfg.get("quant_provider"), f"{sym} sin proveedor"
        assert float(cfg.get("window", 0)) > 0, f"{sym} sin ventana de cadena"
        assert cfg.get("family"), f"{sym} sin familia"


def test_full_assets_have_their_own_option_chain_path():
    """`full=True` significa cadena de opciones PROPIA, no confluencia prestada."""
    for sym, cfg in ASSETS.items():
        if not cfg.get("full"):
            continue
        assert cfg.get("quant_provider"), f"{sym} es full pero no declara quant_provider"
        assert cfg.get("expiry_days"), f"{sym} es full pero no declara expiry_days"
        eco = asset_info(sym)["ecosystem"]
        own = set(eco["derivatives"]["equity_options"]) | set(eco["derivatives"]["index_options"]) | set(eco["derivatives"].get("future_options", []))
        assert sym in own, f"{sym} es full pero no aparece en su propia cadena"


def test_context_only_assets_are_not_selectable():
    """XLI/XLF/VIX son contexto interno: no deben ofrecerse como activo operable."""
    selectable = {a["symbol"] for a in selectable_assets()}
    for sym, cfg in ASSETS.items():
        if cfg.get("internal_context"):
            assert sym not in selectable, f"{sym} es contexto interno pero es seleccionable"


# ------------------------------------------------------- ecosistema coherente

def test_architecture_string_matches_the_declared_scope():
    eco = public_summary("DIA")
    assert eco["architecture"] == "MULTI_ASSET · SEPARATE INSTRUMENT MATH"
    assert eco["fusion_rule"] == "SEPARATE_INSTRUMENT_MATH_THEN_NORMALIZED_FEATURE_FUSION"


def test_dia_index_root_is_the_optionable_one():
    """DJX, no DJI.

    DJX (DJIA/100) tiene cadena de opciones; DJI es el índice crudo y no la tiene.
    Devolver DJI invitaba a pedir una cadena inexistente. Esta corrección es una
    MEJORA del estrechamiento, no un efecto colateral.
    """
    eco = public_summary("DIA")
    assert "DJX" in eco["indices"]
    assert "DJI" not in eco["indices"], (
        "DJI no es operable con opciones; si vuelve, alguien puede pedirle una cadena"
    )
    assert {"YM", "MYM"} <= set(eco["futures"])


def test_ecosystem_of_unsupported_symbol_degrades_instead_of_raising():
    """Consultar es informativo; entrar al pipeline es un error. Semánticas distintas."""
    eco = public_summary("SPY")
    assert eco["family"] == "UNSUPPORTED"
    assert eco["derivatives"]["equity_options"] == []
    # Pero el pipeline sí debe rechazarlo.
    with pytest.raises(UnsupportedAssetError):
        asset_info("SPY")


def test_no_asset_maps_to_another_instruments_math():
    """Cada ecosistema se refiere a su propio símbolo, sin proxies cruzados."""
    for sym in ASSETS:
        eco = public_summary(sym)
        assert eco["symbol"] == sym
        if eco["family"] != "UNSUPPORTED":
            assert eco["family"].upper().startswith(("DOW", "VOL")), (
                f"{sym} declara familia {eco['family']!r}, fuera del alcance Dow"
            )

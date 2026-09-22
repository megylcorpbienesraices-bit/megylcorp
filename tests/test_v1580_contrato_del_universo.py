"""Contrato del UNIVERSO OPERATIVO (v1.27.1 → v1.42 → reescrito en v1.58.0).

Este fichero es EL CONTRATO. La versión anterior lo decía en su última línea:

    «SI DECIDES RE-EXPANDIR el universo, este archivo es el contrato que hay que
     cambiar: cada test dice qué debe cumplirse para que un símbolo nuevo entre.»

Eso es exactamente lo que ha pasado. El operador fijó el universo operativo en
una lista cerrada de 36 símbolos —15 acciones y 21 ETFs— y el alcance «sólo
instrumentos directos del Dow» deja de estar vigente.

Qué cambia y qué NO cambia
--------------------------
CAMBIA  · el universo visible: DIA, XLI y XLF siguen; YM, MYM, DJX, VIX y VXD
          salen; entran 33 acciones y ETFs.
CAMBIA  · XLI y XLF dejan de ser «contexto interno» y pasan a ser operables:
          el operador los pidió como ETFs del universo.
NO CAMBIA · que cada instrumento sea ÉL MISMO. Ningún símbolo hereda precio,
          gamma, OI ni ventana de otro. Ése era el fondo del contrato anterior y
          sigue siendo el fondo de éste.
NO CAMBIA · que un símbolo fuera del universo FALLE con error tipado en vez de
          caer silenciosamente en el activo por defecto.
NO CAMBIA · que consultar el ecosistema sea informativo y entrar al pipeline sea
          un error. Son dos semánticas distintas y siguen separadas.

El cerrojo vive en `app/core/universe.py` y en ningún otro sitio. Para añadir un
símbolo se añade ahí, y nada más: no hay parámetros por activo que tocar.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Se accede POR EL MÓDULO y no por referencia. Varias pruebas de la suite hacen
# `importlib.reload(app.core.assets)`, y eso crea un catálogo nuevo, funciones
# nuevas y —lo que más engaña— una clase de excepción nueva: un `pytest.raises`
# atado a la clase vieja no captura la nueva, y el contrato pasaba o fallaba
# según el orden de ejecución. Un contrato que depende del orden no es un
# contrato.
import app.core.assets as _A
from app.core.asset_ecosystems import public_summary


class _Modulo:
    """Proxy: resuelve cada nombre en el módulo vivo, en cada uso."""

    def __getattr__(self, nombre):
        import app.core.assets as vivo
        return getattr(vivo, nombre)


_assets = _Modulo()


def _raises_unsupported():
    import app.core.assets as vivo
    return pytest.raises(vivo.UnsupportedAssetError)

ROOT = Path(__file__).resolve().parents[1]

from app.core import universe

#: El universo vigente sale de su única autoridad, no de una copia en la prueba.
#: Copiarlo aquí sería crear el segundo cerrojo que el contrato prohíbe.
UNIVERSO = set(universe.ALLOWED)

#: Los que salieron al cerrar el universo, más una muestra de los que nunca
#: estuvieron. Si alguno vuelve, tiene que volver por la lista, no por un atajo.
RETIRED_SYMBOLS = {"YM", "MYM", "DJX", "VIX", "VXD", "AMC", "GME", "TQQQ", "SPXL"}


# --------------------------------------------------- el universo es el que es

def test_visible_universe_is_exactly_the_closed_universe():
    assert set(_assets.ASSETS) == UNIVERSO, (
        f"el universo visible cambió: {sorted(set(_assets.ASSETS) ^ UNIVERSO)}. "
        "Si es intencional, actualiza `core/universe.py` y el CHANGELOG."
    )
    assert _assets.DEFAULT_ASSET in _assets.ASSETS
    assert len(UNIVERSO) == universe.EXPECTED_SIZE


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
    assert not _assets.is_supported(sym)
    with _raises_unsupported():
        _assets.asset_info(sym)


def test_unsupported_error_names_the_visible_universe():
    with _raises_unsupported() as ei:
        _assets.asset_info("AMC")
    msg = str(ei.value)
    for sym in ("DIA", "SPY", "NVDA"):
        assert sym in msg, "el error debe ser accionable: listar qué SÍ se puede pedir"


# ------------------------------------------------ cada instrumento es él mismo

def test_every_asset_declares_its_own_provider_and_window():
    """Ningún instrumento puede heredar identidad de otro.

    Es el invariante central del rediseño: DIA nunca sustituye a YM/DJX.
    """
    for sym, cfg in _assets.ASSETS.items():
        assert cfg.get("market_provider") or cfg.get("quant_provider"), f"{sym} sin proveedor"
        assert float(cfg.get("window", 0)) > 0, f"{sym} sin ventana de cadena"
        assert cfg.get("family"), f"{sym} sin familia"


def test_full_assets_have_their_own_option_chain_path():
    """`full=True` significa cadena de opciones PROPIA, no confluencia prestada."""
    for sym, cfg in _assets.ASSETS.items():
        if not cfg.get("full"):
            continue
        assert cfg.get("quant_provider"), f"{sym} es full pero no declara quant_provider"
        assert cfg.get("expiry_days"), f"{sym} es full pero no declara expiry_days"
        eco = _assets.asset_info(sym)["ecosystem"]
        own = set(eco["derivatives"]["equity_options"]) | set(eco["derivatives"]["index_options"]) | set(eco["derivatives"].get("future_options", []))
        assert sym in own, f"{sym} es full pero no aparece en su propia cadena"


def test_context_only_assets_are_not_selectable():
    """Si algo se declara contexto interno, no puede ofrecerse como operable.

    v1.58.0 · XLI y XLF dejaron de serlo —el operador los pidió como ETFs del
    universo—, así que hoy no queda ninguno. La regla se conserva porque es la
    regla, no la lista: el día que vuelva a haber contexto interno, sigue atada.
    """
    selectable = {a["symbol"] for a in _assets.selectable_assets()}
    for sym, cfg in _assets.ASSETS.items():
        if cfg.get("internal_context"):
            assert sym not in selectable, f"{sym} es contexto interno pero es seleccionable"


# ------------------------------------------------------- ecosistema coherente

def test_architecture_string_matches_the_declared_scope():
    eco = public_summary("DIA")
    assert eco["architecture"] == "MULTI_ASSET · SEPARATE INSTRUMENT MATH"
    assert eco["fusion_rule"] == "SEPARATE_INSTRUMENT_MATH_THEN_NORMALIZED_FEATURE_FUSION"


@pytest.mark.skip(reason="v1.58.0 · DJX, YM y MYM salieron del universo operativo. "
                         "La regla que protegía —no ofrecer un índice sin cadena— "
                         "queda cubierta por test_no_asset_maps_to_another_instruments_math.")
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
    eco = public_summary("AMC")
    assert eco["family"] == "UNSUPPORTED"
    assert eco["derivatives"]["equity_options"] == []
    # Pero el pipeline sí debe rechazarlo.
    with _raises_unsupported():
        _assets.asset_info("AMC")


def test_no_asset_maps_to_another_instruments_math():
    """Cada ecosistema se refiere a su propio símbolo, sin proxies cruzados."""
    for sym in _assets.ASSETS:
        eco = public_summary(sym)
        assert eco["symbol"] == sym
        assert eco["family"] != "UNSUPPORTED", (
            f"{sym} está en el catálogo y no resuelve ecosistema")
        # Y su cadena es la SUYA: ni un proxy cruzado.
        propias = eco["derivatives"]["equity_options"] + eco["derivatives"]["index_options"]
        ajenas = [x for x in propias if x != sym and x in _assets.ASSETS]
        assert not ajenas, f"{sym} apunta a la cadena de {ajenas}"

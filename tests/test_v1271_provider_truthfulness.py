"""Veracidad del catálogo de proveedores (tests v1.27.1).

Qué pasó
--------
Dos tests fallaban con `KeyError`:

    rows["opra_direct"]["state"] != "ACTIVE"        # test_v1240
    st["providers"]["TRADESTATION"]["state"] in {...}  # test_v12514

Ambas claves desaparecieron en v1.27.0. No es un bug: `feed_adapters.descriptors()`
ahora dice *"Expose only the provider stack actually used by this DOW-specialized
build"*, y `app/providers/tradestation/` nunca llegó a empaquetarse.

Por qué el cambio fue CORRECTO
------------------------------
La intención original de esos tests era *"opra_direct no debe declararse ACTIVE
sin estar configurado"*: un test de veracidad sobre no exagerar capacidades.

El diseño nuevo satisface esa intención de forma **más fuerte**: el adaptador no
aparece en absoluto, así que no puede exagerar nada. Un placeholder listado como
READY invita a creer que la ruta existe y solo falta enchufarla; no listarlo dice
la verdad, que es que no está implementada.

Este módulo fija el invariante general en vez de nombres concretos, así que
sobrevive a que añadas o quites proveedores.
"""

from __future__ import annotations

import os

import pytest

from app.core.feed_adapters import descriptors, status
from app.core.provider_data_lake import DATA_LAKE

# Proveedores que la build MULTI_ASSET implementa de verdad.
IMPLEMENTED_PROVIDERS = {"ALPACA", "TASTYTRADE", "OFFICIAL_MACRO"}

# Adaptadores retirados en v1.27.0 por no estar implementados.
# Si alguno vuelve al catálogo, debe ser con una implementación real detrás.
RETIRED_ADAPTERS = {"opra_direct", "udp_direct", "tradestation", "databento", "cme_mdp"}


# ------------------------------------------------- invariante de veracidad

def test_no_adapter_claims_active_without_configuration():
    """ACTIVE exige credencial/endpoint real. READY es 'existe el borde, no la conexión'.

    Este es el invariante que de verdad importaba en el test viejo, y ahora se
    comprueba para TODOS los adaptadores en vez de para uno nombrado a mano.
    """
    for row in descriptors():
        if row["state"] == "ACTIVE":
            assert row["configured"] is True, (
                f"el adaptador {row['key']!r} declara ACTIVE sin `configured=True`. "
                "ACTIVE sin credencial hace creer que hay un feed vivo que no existe."
            )


def test_every_listed_adapter_has_a_declared_transport_and_detail():
    """Nada entra al catálogo sin decir por dónde habla y qué hace."""
    for row in descriptors():
        assert row["transport"], f"{row['key']} no declara transporte"
        assert row["detail"], f"{row['key']} no declara detalle"
        assert row["state"] in {"ACTIVE", "READY", "NOT_CONFIGURED", "DISABLED"}, row


@pytest.mark.parametrize("key", sorted(RETIRED_ADAPTERS))
def test_retired_adapters_are_absent_not_listed_as_inactive(key):
    """Retirado significa AUSENTE, no 'listado pero apagado'.

    Un placeholder en READY sugiere que la ruta existe y solo falta enchufarla.
    No listarlo dice la verdad: no está implementada.
    """
    keys = {r["key"] for r in descriptors()}
    assert key not in keys, (
        f"{key!r} volvió al catálogo de adaptadores. Si ahora hay una "
        "implementación real detrás, quítalo de RETIRED_ADAPTERS y añade un test "
        "que ejercite su transporte. Si no la hay, no debe aparecer."
    )


# ----------------------------------------------------- data lake coherente

def test_data_lake_lists_exactly_the_implemented_providers():
    st = DATA_LAKE.status()
    listed = set((st.get("providers") or {}).keys())
    assert listed == IMPLEMENTED_PROVIDERS, (
        f"el data lake lista {sorted(listed)} pero la build implementa "
        f"{sorted(IMPLEMENTED_PROVIDERS)}. Registro y realidad deben coincidir: "
        "un proveedor listado sin implementación produce huecos silenciosos en la "
        "cobertura histórica."
    )


def test_data_lake_does_not_claim_total_external_coverage():
    """El lake no debe afirmar que tiene la librería externa completa."""
    st = DATA_LAKE.status()
    assert st["provider_total_external_library_claimed"] is False
    assert st["architecture"] == "HOT_LIVE + WARM_UNIVERSE + COLD_DATA_LAKE"


def test_adapter_catalog_and_data_lake_do_not_contradict_each_other():
    """Ningún adaptador ACTIVE puede referirse a un proveedor que el lake ignora.

    Esta contradicción es exactamente el tipo de deriva que produjo los KeyError:
    dos registros del mismo hecho que se actualizan por separado.
    """
    lake_providers = set((DATA_LAKE.status().get("providers") or {}).keys())
    alias = {
        "alpaca_ws": "ALPACA",
        "tastytrade_dxlink": "TASTYTRADE",
        "official_macro": "OFFICIAL_MACRO",
    }
    for row in descriptors():
        if row["state"] != "ACTIVE":
            continue
        mapped = alias.get(row["key"])
        if mapped is None:
            continue  # p.ej. `replay`, que no es un proveedor externo
        assert mapped in lake_providers, (
            f"{row['key']} está ACTIVE pero {mapped} no existe en el data lake"
        )


def test_status_contract_is_stable():
    s = status()
    for key in ("state", "active", "adapters", "contract", "note"):
        assert key in s, f"falta la clave {key!r} en feed_adapters.status()"
    assert isinstance(s["adapters"], list) and s["adapters"]
    assert set(s["active"]) <= {r["key"] for r in s["adapters"]}


def test_replay_adapter_is_never_a_live_market_source():
    """Replay tiene que existir, pero nunca puede contarse como feed en vivo."""
    rows = {r["key"]: r for r in descriptors()}
    if "replay" not in rows:
        pytest.skip("esta build no empaqueta el adaptador de replay")
    replay = rows["replay"]
    assert replay["direct"] is False
    assert "FILE" in replay["transport"] or "REPLAY" in replay["transport"].upper()

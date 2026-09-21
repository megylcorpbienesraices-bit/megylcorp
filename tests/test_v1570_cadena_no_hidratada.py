"""v1.57.0 · LA LÍNEA QUE APAGABA LA TERMINAL ENTERA.

En vivo, con la sesión abierta y el proveedor respondiendo, la terminal publicaba
velas y nada más. En TODOS los activos. El Auditor mostraba diez paneles en SIN
DATOS con diez motivos distintos —`CADENA_NO_HIDRATADA`, `SIN_HISTORIA_
ESTRUCTURAL`, `SCANNER_NO_PUBLICO_NIVELES`, `NO_UNIVERSE`, `SIN_CADENA`,
`SIN_DESGLOSE_POR_VENCIMIENTO`…— y ninguno era la causa. Eran diez síntomas de
una línea:

    spot0 = float(numeric_column(snapshot,"underlying_price",...).dropna().iloc[-1])

Si la cadena llegaba sin `underlying_price`, con la columna a NaN o vacía,
`.iloc[-1]` lanza `IndexError`. Esa línea vive dentro del `try` grande del
refresco, así que el refresco ABORTABA entero: `gamma_delta` se quedaba en `{}`,
`nextgen_trace` se iba por `nextgen_trace_price_only`, y con él se caían el
heatmap, los perfiles por strike, los niveles, los prints de opciones —y por
tanto TODO el agresor BUY/SELL—, el skew y la exposición por vencimiento.

Y lo que quedaba en `last_error` era:

    single positional indexer is out-of-bounds

Un error interno de pandas. Ni el activo, ni el dato que faltaba, ni qué hacer.

Lo más absurdo del caso: el precio NO faltaba. La terminal lo enseñaba arriba y
dibujaba cientos de velas con él.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import app.service as S

ROOT = Path(__file__).resolve().parents[1]


def _cinta(precio: float) -> pd.DataFrame:
    return pd.DataFrame({"seq": [1], "timestamp": [pd.Timestamp.now(tz="UTC")],
                         "price": [precio], "size": [1.0], "signed_volume": [1.0]})


CADENAS_ROTAS = {
    "sin columna": pd.DataFrame({"strike": [100.0, 101.0]}),
    "columna a NaN": pd.DataFrame({"strike": [100.0], "underlying_price": [np.nan]}),
    "cadena vacía": pd.DataFrame(columns=["strike", "underlying_price"]),
}


@pytest.mark.parametrize("caso", sorted(CADENAS_ROTAS))
def test_una_cadena_sin_precio_ya_no_tumba_el_refresco(caso):
    """Con la cinta viva, la cadena se sigue procesando. Antes: IndexError."""
    with patch.object(S.PRICE_TICK_FABRIC, "dataframe", return_value=_cinta(767.55)):
        spot, origen = S._resolve_chain_spot(CADENAS_ROTAS[caso], "SPY")
    assert spot == pytest.approx(767.55)
    assert origen == "PRICE_TICK_FABRIC_FALLBACK", "el préstamo tiene que declararse"


def test_el_precio_de_la_cadena_manda_sobre_el_de_la_cinta():
    """El orden de autoridad no cambia: primero el proveedor, luego el respaldo."""
    cadena = pd.DataFrame({"strike": [100.0], "underlying_price": [500.25]})
    with patch.object(S.PRICE_TICK_FABRIC, "dataframe", return_value=_cinta(767.55)):
        spot, origen = S._resolve_chain_spot(cadena, "SPY")
    assert spot == pytest.approx(500.25)
    assert origen == "CHAIN_UNDERLYING_PRICE"


def test_sin_precio_en_ninguna_parte_el_fallo_tiene_nombre_y_causa():
    """`single positional indexer is out-of-bounds` no le dice nada a nadie."""
    with patch.object(S.PRICE_TICK_FABRIC, "dataframe", return_value=pd.DataFrame()):
        with pytest.raises(S.ChainSpotUnavailable) as e:
            S._resolve_chain_spot(pd.DataFrame({"strike": [100.0, 101.0]}), "QQQ")
    texto = str(e.value)
    assert "QQQ" in texto, "el error tiene que decir de qué activo habla"
    assert "underlying_price" in texto
    assert "cinta de precio observada" in texto
    assert "indexer" not in texto and "out-of-bounds" not in texto


def test_un_precio_imposible_no_se_acepta_como_bueno():
    """Un cero o un negativo centrarían la cadena en el sitio equivocado."""
    for malo in (0.0, -12.0, float("nan")):
        with patch.object(S.PRICE_TICK_FABRIC, "dataframe", return_value=_cinta(767.55)):
            spot, origen = S._resolve_chain_spot(
                pd.DataFrame({"strike": [100.0], "underlying_price": [malo]}), "SPY")
        assert spot == pytest.approx(767.55)
        assert origen == "PRICE_TICK_FABRIC_FALLBACK"


def test_el_respaldo_de_precio_llama_a_un_metodo_QUE_EXISTE():
    """`PRICE_TICK_FABRIC.snapshot(...)` no existe y nunca existió.

    Se llamaba así en `nextgen_trace_price_only`, dentro de un `try/except` que
    se tragaba el `AttributeError`. Justo en el camino degradado —al que cae la
    terminal cuando la cadena no hidrata— el respaldo de precio no podía
    funcionar nunca, y nadie se enteraba.
    """
    from app.core.provider_flow_fabric import PRICE_TICK_FABRIC
    assert not hasattr(PRICE_TICK_FABRIC, "snapshot"), (
        "si la cinta gana un `snapshot()`, revisar esta prueba y el respaldo")
    assert hasattr(PRICE_TICK_FABRIC, "dataframe")

    src = (ROOT / "app/service.py").read_text(encoding="utf-8")
    assert "PRICE_TICK_FABRIC.snapshot(" not in re.sub(r"·[^\n]*", "", src), (
        "se sigue llamando a un método que no existe")


def test_la_cinta_da_el_ultimo_precio_observado():
    with patch.object(S.PRICE_TICK_FABRIC, "dataframe",
                      return_value=pd.DataFrame({"price": [100.0, 101.0, 767.55]})):
        assert S._fabric_last_price("SPY") == pytest.approx(767.55)
    for vacio in (pd.DataFrame(), pd.DataFrame({"price": [np.nan]}), None):
        with patch.object(S.PRICE_TICK_FABRIC, "dataframe", return_value=vacio):
            assert S._fabric_last_price("SPY") is None


def test_el_refresco_registra_de_donde_salio_el_precio_de_la_cadena():
    """Sin la procedencia, un precio prestado se confunde con uno del proveedor."""
    src = (ROOT / "app/service.py").read_text(encoding="utf-8")
    assert "spot0, spot0_source = _resolve_chain_spot(snapshot, self.symbol)" in src
    assert 'meta["chain_spot_source"] = spot0_source' in src
    assert ".dropna().iloc[-1]" not in src.split("def _resolve_chain_spot")[0].split(
        "_fetch_asset_quant_snapshot(self.symbol, initial_window, expiry)")[-1][:400]


# ═══════════════════════════════════════════════════════════════════════════
# EL AUDITOR DICE LA CAUSA, NO DIEZ SÍNTOMAS
# ═══════════════════════════════════════════════════════════════════════════

def _checks(caidos, velas_ok=True):
    todos = ["TRACE · velas", "TRACE · perfiles por strike", "TRACE · heatmap",
             "TRACE · niveles", "FLUJO · prints de opciones", "VOLATILIDAD · skew",
             "EXPOSICIÓN · por vencimiento"]
    return [{"panel": p, "ok": (p not in caidos) and (p != "TRACE · velas" or velas_ok)}
            for p in todos]


def test_seis_paneles_vacios_por_la_cadena_se_declaran_como_UN_fallo():
    from app.terminal_api import _causa_raiz
    caidos = ["TRACE · perfiles por strike", "TRACE · heatmap", "TRACE · niveles",
              "FLUJO · prints de opciones", "VOLATILIDAD · skew",
              "EXPOSICIÓN · por vencimiento"]
    rc = _causa_raiz(_checks(caidos),
                     {"last_error": "single positional indexer is out-of-bounds"})
    assert rc["detected"] is True
    assert rc["link"] == "ITM_QUANT_CHAIN"
    assert len(rc["panels"]) == 6
    assert "MISMO eslabón" in rc["detail"]
    # Con velas, el corte no está en la red, y decirlo ahorra buscar donde no es.
    assert rc["price_available"] is True
    assert "no son 6 fallos" in rc["detail"].lower()
    assert rc["engine_error"]
    assert rc["what_to_check"]


def test_un_solo_panel_vacio_no_se_vende_como_causa_raiz():
    """Inventar una causa común donde no la hay sería el defecto opuesto."""
    from app.terminal_api import _causa_raiz
    rc = _causa_raiz(_checks(["VOLATILIDAD · skew"]), {})
    assert rc["detected"] is False
    assert "cada panel vacío tiene su propio motivo" in rc["detail"]


def test_sin_velas_el_diagnostico_no_afirma_que_el_precio_llega():
    from app.terminal_api import _causa_raiz
    caidos = ["TRACE · velas", "TRACE · heatmap", "TRACE · niveles",
              "FLUJO · prints de opciones"]
    rc = _causa_raiz(_checks(caidos, velas_ok=False), {})
    assert rc["detected"] is True
    assert rc["price_available"] is False
    assert "las velas se están dibujando" not in rc["detail"]


def test_la_causa_raiz_viaja_al_frontend_y_se_pinta_antes_de_la_tabla():
    api = (ROOT / "app/terminal_api.py").read_text(encoding="utf-8")
    assert '"root_cause": _causa_raiz(checks, state),' in api

    js = (ROOT / "app/static/itmq_app.js").read_text(encoding="utf-8")
    assert "renderRootCause((state.diagnostics || {}).root_cause);" in js
    assert js.index("renderRootCause((state.diagnostics") < js.index("fillTable('tblDiag'"), \
        "la causa tiene que leerse ANTES que la tabla de síntomas"

    html = (ROOT / "app/templates/terminal.html").read_text(encoding="utf-8")
    assert 'id="diagRootCause"' in html
    assert html.index('id="diagRootCause"') < html.index('id="tblDiag"')

"""v1.58.0 · EL UNIVERSO OPERATIVO ESTÁ CERRADO, Y EL MOTOR SIGUE SIENDO GENÉRICO.

El catálogo dinámico registraba TODO lo que el proveedor cataloga —miles de
símbolos—. Cada uno entraba en el selector, en el scanner, en la precarga y, lo
que de verdad cuesta, en las peticiones al proveedor. Con un contrato de 240
peticiones por 60 s, gastarlas en símbolos que nadie mira es gastarlas en nada.

Y un universo que crece solo tampoco se puede certificar: «funciona en todos los
activos» no es comprobable sobre una lista que cambia cada vez que el proveedor
publica la suya.

Estas pruebas fijan las DOS mitades del encargo, que se contradicen si se leen
mal y no se contradicen si se leen bien:

  1 · El universo queda acotado a la lista del operador.
  2 · La MATEMÁTICA no se acota: ni una rama por activo. El motor trata a NVDA y
      a SPY igual que trataría a cualquier símbolo que se añadiera mañana, y
      añadir uno es añadirlo a la lista, no tocar el cálculo.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.core import universe
# Por el MÓDULO, no por referencia: otras pruebas hacen
# `importlib.reload(app.core.assets)` y entonces la función registra en un
# catálogo y la prueba mira otro.
import app.core.assets as _A


class _Catalogo:
    def __getattr__(self, nombre):
        import app.core.assets as vivo
        return getattr(vivo, nombre)


_assets = _Catalogo()


def ASSETS():
    import app.core.assets as vivo
    return vivo.ASSETS


def register_provider_assets(filas):
    import app.core.assets as vivo
    return vivo.register_provider_assets(filas)


def selectable_assets(**kw):
    import app.core.assets as vivo
    return vivo.selectable_assets(**kw)

RAIZ = pathlib.Path(".")

#: Los que estaban en el catálogo estático y el operador NO incluyó.
RETIRADOS = ("YM", "MYM", "DJX", "VIX", "VXD")


# ═══════════════════════════════════════════════════════════════════════════
# 1 · LA LISTA ES LA QUE ES
# ═══════════════════════════════════════════════════════════════════════════

def test_el_universo_tiene_el_tamaño_declarado():
    assert len(universe.ALLOWED) == universe.EXPECTED_SIZE == 36


def test_las_quince_acciones_estan():
    assert universe.EQUITIES == (
        "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "GOOGL", "AMD", "AVGO",
        "NFLX", "JPM", "BAC", "XOM", "PLTR", "COIN")
    assert len(universe.EQUITIES) == 15


def test_los_veintiun_etfs_estan():
    assert universe.ETFS == (
        "SPY", "QQQ", "IWM", "DIA",
        "XLK", "XLF", "XLI", "XLY", "XLP", "XLE", "XLV", "XLU",
        "SMH", "SOXX", "GLD", "GDX", "TLT", "HYG", "USO", "EEM", "FXI")
    assert len(universe.ETFS) == 21


def test_no_hay_duplicados_entre_acciones_y_etfs():
    assert len(universe.ORDERED) == len(set(universe.ORDERED)) == len(universe.ALLOWED)


def test_el_universo_es_inmutable():
    """Una lista que se puede mutar en runtime no es un cerrojo."""
    assert isinstance(universe.ALLOWED, frozenset)
    assert isinstance(universe.EQUITIES, tuple) and isinstance(universe.ETFS, tuple)


@pytest.mark.parametrize("sym", RETIRADOS)
def test_los_retirados_ya_no_pertenecen_al_universo(sym):
    assert not universe.is_allowed(sym)


# ═══════════════════════════════════════════════════════════════════════════
# 2 · NADA DE FUERA ENTRA POR NINGUNA PUERTA
# ═══════════════════════════════════════════════════════════════════════════

def test_el_catalogo_estatico_solo_tiene_permitidos():
    fuera = sorted(s for s in ASSETS() if not universe.is_allowed(s))
    assert not fuera, f"símbolos fuera del universo en el catálogo: {fuera}"


def test_el_registro_dinamico_RECHAZA_lo_que_no_esta():
    antes = set(ASSETS())
    intrusos = ["BASURA", "AMC", "GME", "SPXL", "TQQQ", "NVDL"]
    filas = [{"symbol": s, "asset_class": "EQUITY", "options_enabled": True}
             for s in intrusos]
    register_provider_assets(filas)
    entraron = sorted(set(ASSETS()) - antes)
    assert not entraron, f"el catálogo dinámico dejó entrar: {entraron}"


def test_el_registro_dinamico_SI_admite_los_permitidos():
    """El cerrojo no puede dejar el universo vacío."""
    antes = set(ASSETS())
    register_provider_assets([{"symbol": "NVDA", "asset_class": "EQUITY",
                               "options_enabled": True}])
    assert "NVDA" in ASSETS() or "NVDA" in antes


def test_la_cache_del_catalogo_tampoco_engorda():
    from app.core.provider_asset_catalog import select_published

    filas = [{"symbol": s} for s in ("NVDA", "BASURA", "SPY", "AMC", "TQQQ")]
    publicados = {r["symbol"] for r in select_published(filas)}
    assert publicados == {"NVDA", "SPY"}


def test_una_cache_VIEJA_se_filtra_al_leerla():
    """Un despliegue que venga de antes trae el universo entero en disco."""
    import json
    from app.core import provider_asset_catalog as cat

    original = cat.CATALOG_PATH
    tmp = RAIZ / "tests" / "_cache_universo_tmp.json"
    try:
        tmp.write_text(json.dumps({"assets": [
            {"symbol": "SPY"}, {"symbol": "AMC"}, {"symbol": "NVDA"}, {"symbol": "GME"}]}),
            encoding="utf-8")
        cat.CATALOG_PATH = tmp
        leidos = {r["symbol"] for r in cat.load_cached_universe()}
        assert leidos == {"SPY", "NVDA"}
    finally:
        cat.CATALOG_PATH = original
        tmp.unlink(missing_ok=True)


def test_el_selector_solo_ofrece_permitidos():
    fuera = [r["symbol"] for r in selectable_assets(include_partial=True)
             if not universe.is_allowed(r["symbol"])]
    assert not fuera, fuera


def test_ninguna_lista_visible_nombra_un_retirado():
    """Ni el diagnóstico de arquitectura ni ninguna lista escrita a mano."""
    for f in ("app/main.py", "app/service.py", "app/terminal_api.py"):
        txt = (RAIZ / f).read_text("utf-8")
        for linea in txt.splitlines():
            limpia = linea.strip()
            if limpia.startswith("#") or '"""' in limpia:
                continue
            # Un conjunto de EXCLUSIÓN no es una lista visible: nombrar un símbolo
            # para NO pedirlo es lo contrario de ofrecerlo.
            if " not in " in limpia:
                continue
            for sym in RETIRADOS:
                # Sólo como literal de símbolo entre comillas: `YM` aparece dentro
                # de palabras y de claves que no son tickers.
                assert f'"{sym}"' not in limpia and f"'{sym}'" not in limpia, (f, sym, linea)


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Y EL MOTOR SIGUE SIENDO GENÉRICO
# ═══════════════════════════════════════════════════════════════════════════

def test_el_universo_no_lleva_NI_UN_parametro_de_calculo():
    """El cerrojo es de catálogo. Si aquí apareciera una ventana, una griega o un
    proveedor por activo, habríamos convertido una lista en una tabla de
    comportamiento y roto la arquitectura multi-activo."""
    src = (RAIZ / "app/core/universe.py").read_text("utf-8")
    cuerpo = "\n".join(l for l in src.splitlines()
                       if not l.strip().startswith(("#", '"""', "*")))
    for prohibido in ("window", "expiry_days", "multiplier", "provider", "accent",
                      "iv_", "gamma", "delta", "vega", "strike", "beta"):
        assert prohibido not in cuerpo.lower(), (
            f"«{prohibido}» en el módulo del universo: eso es lógica por activo")


def test_el_universo_no_aparece_en_el_nucleo_de_calculo():
    """Ningún módulo matemático puede preguntar a qué universo pertenece nada."""
    matematicos = ["app/core/greeks_service.py", "app/core/net_drift.py",
                   "app/core/wall_engine.py", "app/core/delta_flow.py",
                   "app/core/units_registry.py", "app/core/expiry_window.py"]
    for f in matematicos:
        p = RAIZ / f
        if not p.is_file():
            continue
        txt = p.read_text("utf-8")
        assert "universe" not in txt or "core.universe" not in txt, (
            f"{f} consulta el universo: la matemática dejó de ser genérica")


def test_añadir_un_simbolo_es_añadirlo_a_la_LISTA_y_nada_mas(universo_ampliado):
    """La prueba de que la arquitectura sigue siendo universal.

    Se admite un símbolo nuevo tocando SÓLO la lista, y el registro dinámico lo
    acepta con la configuración genérica de siempre: sin ventana propia, sin
    proveedor propio y sin ninguna rama de cálculo.
    """
    try:
        universo_ampliado("ZZZZ")
        register_provider_assets([{"symbol": "ZZZZ", "asset_class": "EQUITY",
                                   "name": "Prueba", "options_enabled": True}])
        assert "ZZZZ" in ASSETS(), "no bastó con añadirlo a la lista"
        cfg = ASSETS()["ZZZZ"]
        assert cfg["dynamic"] is True and cfg["kind"] == "ACCION"
        assert cfg["quant_provider"] == "ALPACA"
    finally:
        ASSETS().pop("ZZZZ", None)


def test_cada_simbolo_del_universo_tiene_su_clase_correcta():
    """El registro de instrumentos no es el universo, pero no puede mentir.

    Antes, una acción que no estuviera en el registro caía en la plantilla
    genérica y se publicaba como ETF. La física era la misma —mismo
    multiplicador, misma sesión, mismo modelo— pero la etiqueta era falsa, y una
    etiqueta falsa en un panel es un dato falso.
    """
    from app.core.instruments import get as resolve

    for sym in universe.EQUITIES:
        assert resolve(sym).asset_class == "EQUITY", sym
    for sym in universe.ETFS:
        assert resolve(sym).asset_class == "ETF", sym


def test_los_instrumentos_del_universo_comparten_la_MISMA_fisica():
    """Ni un parámetro por activo: si alguien mete uno, se ve aquí."""
    from app.core.instruments import get as resolve

    fisica = {(resolve(s).multiplier, resolve(s).session, resolve(s).settlement,
               resolve(s).exercise) for s in universe.ORDERED}
    assert len(fisica) == 1, f"el universo dejó de compartir plantilla: {fisica}"


def test_un_simbolo_desconocido_sigue_resolviendo():
    """La plantilla genérica es la que hace universal al motor."""
    from app.core.instruments import get as resolve

    inst = resolve("SIMBOLO_QUE_NO_EXISTE")
    assert inst.multiplier == 100.0 and inst.session == "US_EQUITY"


def test_el_cerrojo_del_universo_vive_en_UN_solo_modulo():
    """Quien decide QUÉ SÍMBOLOS EXISTEN es `universe`, y nadie más.

    No se persigue cualquier lista de tickers —el registro de instrumentos tiene
    una legítima, que es física y no universo—: se persigue que ningún otro
    módulo tenga su propia idea de qué está permitido.
    """
    culpables = []
    for f in sorted((RAIZ / "app").rglob("*.py")):
        if f.name == "universe.py":
            continue
        txt = f.read_text("utf-8", errors="replace")
        for linea in txt.splitlines():
            limpia = linea.strip()
            if limpia.startswith("#"):
                continue
            if re.search(r"\b(ALLOWED|WHITELIST|UNIVERSO_PERMITIDO|ALLOWED_SYMBOLS)\b\s*=", limpia):
                culpables.append((str(f.relative_to(RAIZ)), limpia[:80]))
    assert not culpables, f"un segundo cerrojo del universo: {culpables}"


def test_los_dos_puntos_de_entrada_consultan_el_cerrojo():
    for f in ("app/core/assets.py", "app/core/provider_asset_catalog.py"):
        txt = (RAIZ / f).read_text("utf-8")
        assert "universe.is_allowed" in txt, f

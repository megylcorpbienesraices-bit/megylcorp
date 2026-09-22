from __future__ import annotations

import math

from . import universe
from .asset_ecosystems import public_summary
from .obs import note as _obs_note
from .expiry_clock import year_fraction

# CATÁLOGO DEL UNIVERSO OPERATIVO
#
# v1.58.0 · El universo está CERRADO a los 37 símbolos de `core.universe`. Lo que
# no está en esa lista no se registra, y lo que no se registra no se ofrece en el
# selector, no entra en el scanner, no se precarga y —lo que de verdad importa—
# no gasta una sola petición del contrato del proveedor.
#
# El cierre es de CATÁLOGO, no de matemática: aquí no hay ni una rama por activo.
# El motor sigue siendo genérico, y añadir un símbolo mañana es añadirlo a la
# lista, no tocar el cálculo.
# Static Dow instruments remain first-class. Provider-discovered ETFs are registered at
# runtime and keep separate per-instrument math; no symbol ever borrows another symbol
# price/Gamma/OI. `full=True` means an own-instrument options path is available.
ASSETS = {
    "DIA": {"name":"SPDR Dow Jones Industrial Average ETF","description":"SPDR Dow Jones Industrial Average ETF","kind":"ETF","category":"ETFs","exchange":"ARCA","family":"Dow","full":True,"selectable":True,"quant_provider":"ALPACA","market_provider":"ALPACA","window":12.0,"expiry_days":21,"accent":"#39d6ff"},
    "XLI": {"name":"Industrial Select Sector SPDR Fund","description":"Industrial Select Sector SPDR Fund","kind":"ETF","category":"ETFs","exchange":"ARCA","family":"Sectoriales","full":True,"selectable":True,"quant_provider":"ALPACA","market_provider":"ALPACA","window":12.0,"expiry_days":21,"accent":"#f59e0b"},
    "XLF": {"name":"Financial Select Sector SPDR Fund","description":"Financial Select Sector SPDR Fund","kind":"ETF","category":"ETFs","exchange":"ARCA","family":"Sectoriales","full":True,"selectable":True,"quant_provider":"ALPACA","market_provider":"ALPACA","window":8.0,"expiry_days":21,"accent":"#22c55e"},
}

def _seed_universe() -> None:
    """Siembra el catálogo con el universo operativo completo.

    v1.58.0 · Antes sólo existían los activos revisados a mano y el resto llegaba
    del descubrimiento del proveedor. Con el universo cerrado eso deja al selector
    dependiendo de que el catálogo del proveedor se haya podido descargar: sin
    red, sin clave o con la caché fría, el operador veía tres activos en vez de
    los suyos.

    La lista decide QUÉ EXISTE; el proveedor decide QUÉ PUEDE HACERSE con cada
    uno. Son dos preguntas distintas y ahora cada una la contesta quien la sabe.
    La configuración sembrada es la genérica —la misma para todos— y el
    descubrimiento la enriquece después con la capacidad real de cadena.
    """
    for sym in universe.ORDERED:
        if sym in ASSETS:
            continue                      # revisado a mano: manda su configuración
        es_etf = sym in universe.ETFS
        ASSETS[sym] = {
            "name": sym, "description": sym,
            "kind": "ETF" if es_etf else "ACCION",
            "category": "ETFs" if es_etf else "Acciones",
            "exchange": "US", "family": "ETF" if es_etf else "Equity",
            # Capacidad desconocida hasta que un proveedor la confirme: se ofrece
            # el activo, no se promete la cadena.
            "full": False, "selectable": True, "dynamic": True, "board_enabled": False,
            "quant_provider": "ALPACA", "market_provider": "ALPACA",
            "window": 12.0, "expiry_days": 21,
            "accent": "#39d6ff" if es_etf else "#a78bfa",
            "asset_class": "ETF" if es_etf else "EQUITY",
            "reason": ("Del universo operativo. Los derivados quedan "
                       "capability-gated hasta que un proveedor activo confirme "
                       "cadena propia."),
        }


#: Los revisados A MANO. Se congela ANTES de sembrar: si la siembra entrara aquí,
#: `register_provider_assets` trataría los 36 como configuración revisada y nunca
#: les aplicaría la capacidad real de cadena que descubre el proveedor.
STATIC_ASSET_SYMBOLS = frozenset(ASSETS)

_seed_universe()

def _generic_etf_ecosystem(symbol: str) -> dict:
    sym=str(symbol or "").upper().strip()
    return {
        "symbol":sym,"family":"ETF","primary_type":"ETF",
        "related_etfs_equities":[],"indices":[],"futures":[],
        "derivatives":{"equity_options":[sym],"index_options":[],"future_options":[]},
        "architecture":"MULTI_ASSET · SEPARATE INSTRUMENT MATH",
        "fusion_rule":"SEPARATE_INSTRUMENT_MATH_THEN_NORMALIZED_FEATURE_FUSION",
        "provider_rule":"OBSERVATION_QUALITY_FRESHNESS_PROVENANCE",
    }

def _generic_equity_ecosystem(symbol: str, cfg: dict | None = None) -> dict:
    """Ecosistema de una acción descubierta en el proveedor.

    Una acción no hereda nada de otra: su cadena, su gamma y su OI son suyos. Lo
    único que se declara aquí es el sector, cuando el proveedor lo da, porque el
    motor de factores macro lo usa como una exposición más, no como un proxy.
    """
    sym=str(symbol or "").upper().strip()
    cfg=cfg or {}
    return {
        "symbol":sym,"family":str(cfg.get("sector") or "Equity"),"primary_type":"EQUITY",
        "related_etfs_equities":[],"indices":[],"futures":[],
        "derivatives":{"equity_options":[sym],"index_options":[],"future_options":[]},
        "architecture":"MULTI_ASSET · SEPARATE INSTRUMENT MATH",
        "fusion_rule":"SEPARATE_INSTRUMENT_MATH_THEN_NORMALIZED_FEATURE_FUSION",
        "provider_rule":"OBSERVATION_QUALITY_FRESHNESS_PROVENANCE",
        "sector":cfg.get("sector"),"industry":cfg.get("industry"),
    }


def _asset_ecosystem(symbol: str, cfg: dict) -> dict:
    if symbol in STATIC_ASSET_SYMBOLS:
        return public_summary(symbol)
    kind=str(cfg.get("kind") or "").upper()
    if kind in {"ETF","ETN"}:
        return _generic_etf_ecosystem(symbol)
    if kind in {"ACCION","ACCIÓN","EQUITY"}:
        return _generic_equity_ecosystem(symbol,cfg)
    return public_summary(symbol)

def register_provider_etfs(rows) -> int:
    """Compatibilidad v1.41: registra filas de ETF por el camino universal.

    Se mantiene el nombre porque hay despliegues y tests que lo llaman, pero ya no
    tiene lógica propia: dos caminos de registro es como el catálogo de ETF y el
    registro estático de acciones acabaron divergiendo.
    """
    normalized = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item.setdefault("asset_class", "ETF")
        item.setdefault("options_enabled", bool(item.get("alpaca") and item.get("alpaca_has_options")))
        normalized.append(item)
    return register_provider_assets(normalized)



# ─────────────────────────────────────── registro universal Equity + ETF (v1.42)

_KIND_BY_ASSET_CLASS = {"ETF": "ETF", "ETN": "ETF", "EQUITY": "ACCION"}
_CATEGORY_BY_KIND = {"ETF": "ETFs", "ACCION": "Acciones"}
_ACCENT_BY_KIND = {"ETF": "#39d6ff", "ACCION": "#a78bfa"}


def register_provider_assets(rows) -> int:
    """Registra el universo descubierto (acciones y ETF) en el catálogo de runtime.

    Sustituye a `register_provider_etfs`, que sólo admitía ETF. La regla de
    capacidad no cambia y es la que importa: `full=True` sólo si Alpaca confirma
    cadena propia. Un símbolo que Alpaca cataloga pero cuya cadena no sirve se
    publica con precio y sin promesa de derivados, diciendo por qué — que es
    exactamente lo que faltaba cuando QQQ aparecía analizable y no hidrataba.
    """
    added = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        if not universe.is_allowed(sym):
            # El cerrojo del universo. Antes entraba aquí TODO lo que el proveedor
            # cataloga —miles de símbolos— y cada uno acababa en el selector, en el
            # scanner, en la precarga y en las peticiones al proveedor.
            continue
        klass = str(row.get("asset_class") or "ETF").upper()
        kind = _KIND_BY_ASSET_CLASS.get(klass, "ETF")
        has_chain = bool(row.get("options_enabled", row.get("alpaca_has_options")))

        if sym in STATIC_ASSET_SYMBOLS:
            # Un símbolo estático manda sobre el descubrimiento: su configuración
            # está revisada a mano. Sólo se anota qué proveedores lo catalogan.
            cfg = ASSETS[sym]
            cfg["provider_catalog"] = {"alpaca": bool(row.get("alpaca")),
                                       "quantdata": bool(row.get("quantdata"))}
            continue

        cfg = {
            "name": str(row.get("name") or sym), "description": str(row.get("name") or sym),
            "kind": kind, "category": _CATEGORY_BY_KIND.get(kind, "ETFs"),
            "exchange": str(row.get("exchange") or "US"),
            "family": str(row.get("sector") or ("ETF" if kind == "ETF" else "Equity")),
            "full": has_chain, "selectable": True, "dynamic": True, "board_enabled": False,
            "quant_provider": "ALPACA", "market_provider": "ALPACA",
            "window": 12.0, "expiry_days": 21, "accent": _ACCENT_BY_KIND.get(kind, "#39d6ff"),
            "provider_catalog": {"alpaca": bool(row.get("alpaca")),
                                 "quantdata": bool(row.get("quantdata"))},
            "quantdata_supported": bool(row.get("quantdata")),
            "alpaca_has_options": has_chain,
            "asset_class": klass,
            "sector": row.get("sector"), "industry": row.get("industry"),
            "shortable": bool(row.get("shortable", False)),
            "fractionable": bool(row.get("fractionable", False)),
            "tradable": bool(row.get("tradable", True)),
            "reason": ("Análisis completo" if has_chain else
                       f"{'Acción' if kind == 'ACCION' else 'ETF'} detectado en los proveedores. "
                       "Precio y evidencia de mercado disponibles; los derivados quedan "
                       "capability-gated hasta que un proveedor activo confirme cadena propia."),
        }
        ASSETS[sym] = cfg
        added += 1
    return added

# ─────────────────────────────────────── proveedor efectivo por activo

# Qué puede servir cada proveedor como CADENA propia. No es una preferencia: es una
# capacidad. Alpaca no sirve futuros, y un índice sólo si su cadena está en OPRA.
_QUANT_CAPABILITY = {
    "ALPACA": {"ETF", "ACCION"},
    "QUANTDATA": {"ETF", "ACCION", "ÍNDICE"},
    "TASTYTRADE": {"ETF", "ACCION", "ÍNDICE", "FUTURO"},
}


def _can_serve_chain(name: str, cfg: dict, kind: str, peer_enabled) -> bool:
    """¿Puede ESTE proveedor hidratar la cadena de ESTE activo?

    La capacidad por tipo no basta. Alpaca cataloga miles de ETF pero sólo sirve
    cadena de los que tienen opciones listadas; prometer los demás es exactamente
    lo que dejaba a QQQ con `CADENA_NO_HIDRATADA` y el panel vacío. El guard vale
    igual si Alpaca es el proveedor declarado que si se llega a él por descarte:
    un proveedor declarado no es una excepción a lo que el proveedor puede hacer.
    """
    if not name or not peer_enabled(name):
        return False
    if kind not in _QUANT_CAPABILITY.get(name, set()):
        return False
    if name == "ALPACA" and cfg.get("dynamic") and not cfg.get("alpaca_has_options", True):
        return False
    return True


def resolve_quant_provider(cfg: dict) -> tuple[str | None, str]:
    """Proveedor que puede hidratar la cadena de este activo, dentro del roster activo.

    Retirar un proveedor no puede dejar activos huérfanos que sigan pareciendo
    seleccionables y luego fallen en silencio con la cadena vacía. Aquí se resuelve
    quién puede servirlo de verdad, o se devuelve el motivo por el que nadie puede.
    """
    from .provider_parity import peer_enabled
    kind = str(cfg.get("kind") or "ETF").upper()
    declared = str(cfg.get("quant_provider") or "").upper()

    # 1 · El proveedor declarado, si sigue en el roster y puede con este activo.
    if declared and _can_serve_chain(declared, cfg, kind, peer_enabled):
        return declared, "DECLARADO"
    # 2 · Cualquier otro del roster que sí pueda servirlo.
    for name in ("ALPACA", "QUANTDATA"):
        if name == declared:
            continue
        if _can_serve_chain(name, cfg, kind, peer_enabled):
            return name, ("REASIGNADO" if declared else "POR_CAPACIDAD")
    # 3 · Nadie del roster puede. Se dice, en vez de dejarlo roto.
    if declared and not peer_enabled(declared):
        why = f"{declared} está fuera del roster de proveedores"
    elif kind not in _QUANT_CAPABILITY.get(declared or "ALPACA", set()):
        why = f"ningún proveedor activo sirve cadena de tipo {kind}"
    else:
        why = "ningún proveedor activo confirma cadena propia para este símbolo"
    return None, why


def apply_roster_capabilities() -> dict:
    """Recalcula `full`/`selectable` de todo el catálogo según el roster activo.

    Se llama al arrancar y cada vez que cambia el roster. Un activo que ningún
    proveedor puede hidratar deja de ofrecerse como analizable: aparece con su
    motivo en lugar de dejar al analista frente a paneles vacíos sin explicación.
    """
    from .provider_parity import peer_enabled
    changed = {}
    for sym, cfg in ASSETS.items():
        provider, why = resolve_quant_provider(cfg)
        before = (cfg.get("full"), cfg.get("selectable"), cfg.get("quant_provider"))
        if provider is None:
            cfg["full"] = False
            cfg["quant_provider_effective"] = None
            cfg["quant_provider_reason"] = why
            # El precio puede seguir estando disponible por otro proveedor; lo que se
            # retira es la promesa de análisis de derivados.
            if not cfg.get("internal_context"):
                cfg["selectable"] = bool(cfg.get("market_provider") and
                                         peer_enabled(cfg.get("market_provider")))
            cfg["reason"] = (f"Análisis de derivados no disponible: {why}. "
                             "Precio y evidencia de mercado siguen activos si su proveedor lo está.")
        else:
            cfg["quant_provider_effective"] = provider
            cfg["quant_provider_reason"] = why
            if why == "REASIGNADO":
                cfg["quant_provider"] = provider
        after = (cfg.get("full"), cfg.get("selectable"), cfg.get("quant_provider"))
        if before != after:
            changed[sym] = {"antes": before, "despues": after, "motivo": why}
    return changed


class UnsupportedAssetError(LookupError):
    """El símbolo no pertenece al universo visible de esta release.

    Se usa un tipo propio en vez de `KeyError` crudo por tres razones:
      - `KeyError` se confunde con un fallo de diccionario interno y se traga en
        cualquier `except KeyError` genérico aguas arriba.
      - la capa HTTP puede mapear esto a 404/422 en vez de a un 500.
      - lleva el universo soportado en el propio error, así el mensaje es accionable.
    """

    def __init__(self, symbol: str, supported: list[str]):
        self.symbol = symbol
        self.supported = supported
        super().__init__(
            f"Activo no soportado en el catálogo activo: {symbol!r}. "
            f"Universo visible: {', '.join(supported)}"
        )


def asset_info(symbol: str):
    sym = str(symbol or "").upper().strip()
    if sym not in ASSETS:
        raise UnsupportedAssetError(sym, sorted(ASSETS))
    cfg=ASSETS[sym]
    return {"symbol": sym, **cfg, "ecosystem": _asset_ecosystem(sym,cfg)}


def is_supported(symbol: str) -> bool:
    """Chequeo sin excepción, para validar antes de entrar al pipeline."""
    return str(symbol or "").upper().strip() in ASSETS


def selectable_assets(*, include_partial: bool = True):
    rows=[]
    for sym,cfg in ASSETS.items():
        selectable=bool(cfg.get("selectable",True))
        dynamic=bool(cfg.get("dynamic",False))
        if not selectable and not (include_partial and dynamic):
            continue
        rows.append({"symbol":sym,**cfg,"ecosystem":_asset_ecosystem(sym,cfg)})
    return rows

def core_selectable_assets():
    return [{"symbol":s,**ASSETS[s],"ecosystem":_asset_ecosystem(s,ASSETS[s])} for s in STATIC_ASSET_SYMBOLS if ASSETS[s].get("selectable",True)]

DEFAULT_ASSET = "DIA"


def sigma_chain_window(spot: float, iv_pct: float, horizon_days: float, max_sigma: float = 2.5) -> float:
    import math
    s=float(spot); iv=float(iv_pct)/100.0; d=max(float(horizon_days),1.0/(24.0*60.0))
    if not all(math.isfinite(v) for v in (s,iv,d)) or s<=0 or iv<=0:
        return float("nan")
    sigma=s*iv*math.sqrt(year_fraction(d))
    return float(max(float(max_sigma)*sigma, s*0.005))


# Banda de strikes, EN PORCENTAJE DEL PRECIO, cuando no hay IV con la que
# calcular la sigma. Es la única forma de que la misma regla valga para un ETF de
# 600 $ y para una acción de 9 $.
#
# El 2.25 % no es arbitrario: es exactamente la banda que venía usándose para DIA
# (12 $ sobre ~534 $), o sea la que ya estaba validada visualmente. Lo que cambia
# es que ahora ESA banda se aplica a todos los activos en vez de aplicarse a todos
# la cantidad de DÓLARES que le correspondía a uno solo.
UNIVERSAL_CHAIN_WINDOW_PCT = 2.25


def proportional_chain_window(symbol: str, spot: float | None) -> float | None:
    """Ventana de strikes proporcional al precio del activo.

    v1.46.0 · El catálogo daba `window: 12.0` —doce dólares— a TODO activo
    descubierto en runtime. Sobre los precios reales eso significaba:

        SPY  601 $  →  ± 2.0 %      XLF   48 $  →  ± 24.8 %
        QQQ  487 $  →  ± 2.5 %      acción 9.5 $ →  ±126.1 %
        DIA  534 $  →  ± 2.2 %      acción 320 $ →  ±  3.8 %

    Es decir: una banda razonable para los ETF que rondan los 500 $ —los que se
    usaron para desarrollar— y absurda para todo lo demás. En una acción barata el
    mapa abarcaba toda la cadena y la estructura real quedaba en unos pocos
    píxeles; en una cara, recortaba justo la zona que importa.

    Ningún ticker aparece aquí: la banda sale del precio del propio activo.
    """
    try:
        px = float(spot)
    except (TypeError, ValueError):
        return None
    if not (px > 0) or not math.isfinite(px):
        return None
    return px * (UNIVERSAL_CHAIN_WINDOW_PCT / 100.0)


def chain_window_for(symbol: str, spot: float | None = None, iv_pct: float | None = None,
                     horizon_days: float | None = None, max_sigma: float = 2.5) -> dict:
    sym = str(symbol).upper()
    record = ASSETS.get(sym, {})
    declared = record.get("window")
    # Una ventana declarada manda sólo cuando el INSTRUMENTO tiene otra escala,
    # no cuando el ticker está en una lista. Es la diferencia entre una regla y un
    # caso especial, y la lista era un caso especial disfrazado: DIA y DJX
    # quedaban en ±2.9 % por casualidad, XLF en ±16.5 % y XLI en ±9.2 %, que son
    # tres bandas distintas para tres ETF que se leen igual.
    #
    # Dos clases tienen escala propia de verdad:
    #   FUTURO       cotiza en puntos de índice (YM ~44.000), no en el precio del
    #                subyacente: un porcentaje de su cotización no es comparable.
    #   Volatilidad  una cadena de VIX abarca puntos de volatilidad absolutos; un
    #                ±2 % sobre un nivel de 17 no cubriría ni el primer strike.
    # Todo lo demás —ETF, acciones, índices al contado, descubiertos o no— deriva
    # la banda de su propio precio.
    proportional = proportional_chain_window(sym, spot)
    own_scale = (str(record.get("kind") or "").upper() == "FUTURO"
                 or str(record.get("category") or "").upper().startswith("VOLATILIDAD"))
    if declared is not None and own_scale:
        legacy = float(declared)
    elif proportional is not None:
        legacy = proportional
    else:
        legacy = float(declared if declared is not None else 12.0)
    try:
        if spot is not None and iv_pct is not None and horizon_days is not None:
            w=sigma_chain_window(float(spot),float(iv_pct),float(horizon_days),float(max_sigma))
            import math
            if math.isfinite(w) and w>0:
                return {"window":round(w,4),"method":f"SIGMA · ±{max_sigma:g}σ · HORIZONTE EFECTIVO",
                        "horizon_days":float(horizon_days),"max_sigma":float(max_sigma),
                        "legacy_window":legacy,"universal":True}
    except Exception as _e:
        _obs_note('assets:53', _e)
    proportional_used = (declared is None or not own_scale) and proportional is not None
    return {"window":legacy,
            "method":("PROPORCIONAL · ±%.2f%% DEL PRECIO · IV/HORIZONTE NO DISPONIBLES"
                      % UNIVERSAL_CHAIN_WINDOW_PCT) if proportional_used
                     else "CATÁLOGO · VENTANA DECLARADA · IV/HORIZONTE NO DISPONIBLES",
            "horizon_days":horizon_days,"max_sigma":None,"legacy_window":legacy,
            "window_pct_of_spot":(round(UNIVERSAL_CHAIN_WINDOW_PCT,4) if proportional_used else None),
            # `universal` decía False para la vía proporcional, que es precisamente
            # la universal. Lo que no es universal es la ventana declarada a mano.
            "universal":bool(proportional_used)}

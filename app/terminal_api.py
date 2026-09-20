"""Contrato de datos de la terminal de analista · ITM QUANT v1.41.0

Un único payload con el resultado de los análisis que el motor ya calculó,
recortado a lo que la interfaz dibuja. La terminal no vuelve a derivar métricas
ni recompone estructura: lee este bundle y lo pinta.

Regla de procedencia, en este orden:
  1. Cálculo nativo de ITM QUANT (es el motor del usuario y su autoridad).
  2. Observación de proveedor para lo que el motor no calcula por sí mismo
     (cuota de mercado, niveles de dark pool, curvas del proveedor).
  3. Si no hay ninguna de las dos, el campo viaja vacío con su motivo. Nunca se
     rellena con un valor plausible: un hueco visible es información, un número
     inventado es una decisión equivocada esperando a ocurrir.
"""
from __future__ import annotations

from typing import Any, Dict, List
import math

from .core import session_resolver
from .core.obs import note as _obs_note


def _f(v: Any, default: float | None = None) -> float | None:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _d(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _deep_find(obj: Any, *names: str, depth: int = 6) -> Any:
    """Busca la primera clave con uno de esos nombres, a cualquier profundidad.

    Las respuestas del proveedor anidan el dato bajo envoltorios que cambian entre
    herramientas; fijar una única ruta hacía que un campo presente se mostrara
    como ausente.
    """
    if depth < 0 or obj is None:
        return None
    wanted = {n.lower().replace("_", "") for n in names}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower().replace("_", "") in wanted and v is not None and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            hit = _deep_find(v, *names, depth=depth - 1)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj[:24]:
            hit = _deep_find(v, *names, depth=depth - 1)
            if hit is not None:
                return hit
    return None


def _qd_block(intel: Dict[str, Any], key: str) -> Dict[str, Any]:
    """El bloque completo del proveedor, no sólo sus filas.

    Algunos campos oficiales viajan a nivel de respuesta y no por fila —el precio
    de referencia del subyacente en `dark-pool-levels` es el caso claro—, así que
    leer sólo `rows` los perdía.
    """
    block = intel.get(key) if isinstance(intel, dict) else None
    return block if isinstance(block, dict) else {}


def _qd_rows(intel: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    block = intel.get(key) if isinstance(intel, dict) else None
    if not isinstance(block, dict) or not block.get("ready"):
        return []
    rows = block.get("rows")
    return rows if isinstance(rows, list) else []


# ───────────────────────────────────────────────────────────────── resumen

def _resumen(state: Dict[str, Any], trace: Dict[str, Any],
             intel: Dict[str, Any] | None = None) -> Dict[str, Any]:
    levels = state.get("key_levels_report") or {}
    pos = state.get("positioning") or {}
    msf = state.get("market_state_field") or {}
    scanner = state.get("scanner") or {}
    zone = scanner.get("zone") or {}
    spot = _f(state.get("spot"))

    # v1.44.0 · Call Wall y Put Wall vienen del WALL ENGINE, no de `key_levels_report`.
    # Ese informe sigue recalculándolos por su cuenta sobre otro frame, y mientras
    # RESUMEN los leyera de ahí podía enseñar un muro distinto del que TRACE dibuja.
    # Hay UNA autoridad, y es la que viaja en el trace.
    walls = (trace or {}).get("walls") or {}
    wall_override = {
        "call_wall": _f((walls.get("call_wall") or {}).get("strike")),
        "put_wall": _f((walls.get("put_wall") or {}).get("strike")),
    }

    level_rows = []
    for name, key in (
        ("Zero Gamma", "zero_gamma"), ("Call Wall", "call_wall"), ("Put Wall", "put_wall"),
        ("Vol Trigger", "vol_trigger"), ("Hedge Wall", "hedge_wall"), ("Max Pain", "max_pain"),
        ("Gamma Center", "gamma_center"),
        ("Rango esperado alto", "expected_high"), ("Rango esperado bajo", "expected_low"),
    ):
        v = wall_override[key] if key in wall_override else _f(levels.get(key))
        if v is None:
            continue
        level_rows.append({
            "name": name, "price": v,
            "distance": None if spot is None else round(v - spot, 4),
            "distance_pct": None if not spot else round((v - spot) / spot * 100.0, 3),
        })

    prints = []
    for p in (trace.get("option_prints") or [])[-60:]:
        prints.append({
            "t": p.get("t"), "premium": _f(p.get("premium"), 0.0),
            "contracts": _f(p.get("contracts"), 0.0),
            "strike": _f(p.get("strike")), "option_type": p.get("option_type"),
            "aggressor": p.get("aggressor"), "direction": p.get("direction"),
        })
    prints.sort(key=lambda r: -abs(r["premium"] or 0.0))

    flow = state.get("flow") or {}

    # v1.43.0 · RESUMEN no es otro Dashboard: es el destilado de las secciones que
    # ya existen. Antes vivía sólo del Scanner y del posicionamiento, así que el
    # operador tenía que recorrer cinco secciones para reconstruir a mano lo que
    # esta pantalla ya podía decirle en una línea.
    _sum = _resumen_sections(state, trace, intel or {})

    return {
        "direction": scanner.get("direction"),
        "edge_state": scanner.get("edge_state"),
        "evidence": _f(scanner.get("evidence_score")),
        "zone_low": _f(zone.get("low")), "zone_high": _f(zone.get("high")),
        "target1": _f(scanner.get("target1")), "target2": _f(scanner.get("target2")),
        "invalidation": _f(scanner.get("invalidation")),
        "phase": msf.get("phase"), "stability": _f(msf.get("stability")),
        "confluence": _f(msf.get("confluence_index")),
        "net_gex": _f(pos.get("net_gex")), "net_delta": _f(pos.get("net_delta")),
        "gross_gex": _f(pos.get("gross_gex")),
        "zero_gamma": _f(levels.get("zero_gamma")), "max_pain": _f(levels.get("max_pain")),
        "call_wall": wall_override["call_wall"], "put_wall": wall_override["put_wall"],
        "walls": walls,
        "levels": level_rows,
        "prints": prints[:40],
        "flow_regime": flow.get("regime"), "flow_net": _f(flow.get("net")),
        "flow_bull": _f(flow.get("bull")), "flow_bear": _f(flow.get("bear")),
        # v1.42.7 · Contexto de la página Dashboard del proveedor. Se descargaba a
        # cadencia diaria y nadie lo leía. Es contexto, nunca señal: no entra en el
        # Scanner ni en la autoridad direccional, y va etiquetado como del proveedor.
        "news": [
            {"title": r.get("title"), "t": r.get("t"), "source": r.get("source"), "url": r.get("url")}
            for r in _qd_rows(intel or {}, "news")[:12]
        ],
        "movers": [
            {"symbol": r.get("symbol"), "change_pct": _f(r.get("change_pct")),
             "price": _f(r.get("price")), "volume": _f(r.get("volume"))}
            for r in _qd_rows(intel or {}, "gainers_losers")[:12]
        ],
        "context_source": "QUANTDATA_DASHBOARD",
        # Destilado de las demás secciones. Cada entrada lleva su estado para que un
        # hueco nunca se lea como un cero.
        "sections": _sum,
    }


def _resumen_sections(state: Dict[str, Any], trace: Dict[str, Any],
                      intel: Dict[str, Any]) -> Dict[str, Any]:
    """Lo principal de Net Flow, Net Drift, QFLOW, exposición, OI, Dark Pool y volatilidad.

    Se leen los MISMOS bloques que dibujan esas secciones, no una segunda ruta de
    cálculo: si RESUMEN dijera un número distinto del de EXPOSICIÓN, uno de los dos
    estaría mintiendo y no habría forma de saber cuál.
    """
    from .core import quant_data_hub as HUB

    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    qflow = _qflow(state, intel)
    drift = _net_drift(state, intel)
    exposure = HUB.exposure_by_strike(symbol, intel)
    oi = HUB.open_interest(symbol, intel)
    dark = HUB.dark_pool(symbol, intel)
    vol = HUB.volatility(symbol, intel)
    net_flow_rows = _qd_rows(intel, "net_flow")

    def _card(name: str, ready: Any, state_code: Any, **rest: Any) -> Dict[str, Any]:
        out = {"name": name, "ready": bool(ready), "state": state_code,
               "display": None if ready else "SIN DATOS"}
        out.update(rest)
        return out

    events = qflow.get("events") or []
    top_event = events[0] if events else None
    return {
        "net_flow": _card("NET FLOW", bool(net_flow_rows),
                          "DATA_OK" if net_flow_rows else "NO_PROVIDER_DATA",
                          buckets=len(net_flow_rows)),
        "net_drift": _card("NET DRIFT", drift.get("ready"), drift.get("state"),
                           net_premium=drift.get("net_premium"),
                           buckets=drift.get("buckets")),
        "qflow": _card("QFLOW", qflow.get("ready"), qflow.get("state"),
                       level=(qflow.get("level") or {}).get("price") if qflow.get("level") else None,
                       net_premium=qflow.get("net_premium"),
                       concentrations=len(events),
                       top_concentration=top_event),
        "exposicion": _card("EXPOSICIÓN", exposure.get("ready"), exposure.get("state"),
                            strikes=len(exposure.get("rows") or []),
                            source_mode=exposure.get("source_mode")),
        "open_interest": _card("INTERÉS ABIERTO", oi.get("ready"), oi.get("state"),
                               max_pain=oi.get("max_pain"),
                               strikes=len(oi.get("by_strike") or [])),
        "dark_pool": _card("DARK POOL", dark.get("ready"), dark.get("state"),
                           notional=dark.get("notional"),
                           share_pct=dark.get("dark_share_pct"),
                           levels=dark.get("level_count")),
        "volatilidad": _card("VOLATILIDAD", vol.get("ready"), vol.get("state"),
                             skew_points=len(vol.get("skew") or []),
                             term_points=len(vol.get("term_structure") or [])),
        "events": [
            {"t": e.get("t"), "kind": "QFLOW_CONCENTRATION", "side": e.get("side"),
             "premium": e.get("premium"), "price": e.get("price")}
            for e in events[:6]
        ],
    }


# ─────────────────────────────────────────────────────────────── exposición

def _exposicion(trace: Dict[str, Any], state: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    """EXPOSICIÓN con Quant Data como fuente PRIMARIA.

    v1.43.0 invierte la prioridad. Hasta v1.42.7 el perfil del motor ganaba siempre
    y el del proveedor entraba «sólo cuando no hay perfil nativo»; además la
    sustitución era muda, así que nadie —ni el operador ni el Auditor— podía saber
    cuál de los dos estaba viendo.

    Ahora:

        Exposure by Strike / by Expiration de Quant Data  →  DIRECT_PROVIDER
        perfil del motor                                  →  FALLBACK declarado
                                                              y canal de auditoría

    El cálculo propio NO se borra: sigue publicándose en `audit` para poder
    contrastar las dos construcciones, que es donde está la señal cuando difieren.
    Lo que deja de poder hacer es taparlo sin decirlo.
    """
    from .core import quant_data_hub as HUB
    from .core import data_lineage as DL

    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()

    # ── perfil propio del motor (auditoría y respaldo) ───────────────────────
    rows = _d(trace, "profiles", "rows", default=[]) or []
    engine_rows = []
    for r in rows:
        k = _f(r.get("strike"))
        if k is None:
            continue
        engine_rows.append({
            "strike": k,
            "gex": (_f(r.get("gamma_m"), 0.0) or 0.0) * 1e6,
            "dex": (_f(r.get("delta_m"), 0.0) or 0.0) * 1e6,
            "vex": (_f(r.get("vanna_1vol_m"), 0.0) or 0.0) * 1e6,
            "chex": (_f(r.get("charm_10m_m"), 0.0) or 0.0) * 1e6,
            "oi": _f(r.get("oi"), 0.0),
            "call_oi": _f(r.get("call_oi"), 0.0), "put_oi": _f(r.get("put_oi"), 0.0),
            "net_oi": _f(r.get("net_oi"), 0.0),
            "volume": _f(r.get("volume_snapshot"), 0.0),
            "net_volume": _f(r.get("net_volume"), 0.0),
            "source": "ITM_QUANT",
        })

    provider = HUB.exposure_by_strike(symbol, intel)
    provider_rows = list(provider.get("rows") or [])

    if provider_rows:
        by_strike = provider_rows
        exposure_source = "QUANTDATA"
        source_mode = DL.DIRECT_PROVIDER
        # El OI y el volumen no los publica exposure-by-strike. Se completan con el
        # perfil del motor CUANDO existe, marcando de dónde sale cada cosa; lo que
        # no haya queda en None, nunca en 0: un 0 aquí diría «no hay OI».
        engine_by_k = {r["strike"]: r for r in engine_rows}
        for row in by_strike:
            peer = engine_by_k.get(row["strike"]) or {}
            for absent in ("oi", "call_oi", "put_oi", "net_oi", "volume", "net_volume"):
                row.setdefault(absent, peer.get(absent))
    else:
        healthy = HUB.provider_healthy_for(intel, "gex_by_strike", "dex_by_strike",
                                           "vex_by_strike", "chex_by_strike")
        DL.guard_primary_source("QD_GEX", symbol, publishing_mode=DL.FALLBACK,
                                provider_healthy=healthy, strict=False,
                                detail="perfil por strike del motor")
        by_strike = engine_rows
        exposure_source = "ITM_QUANT" if engine_rows else None
        source_mode = DL.FALLBACK if engine_rows else DL.UNAVAILABLE
        if engine_rows:
            DL.LINEAGE.record("QD_GEX", symbol, source_mode=DL.FALLBACK,
                              state=provider.get("state") or DL.NO_PROVIDER_DATA,
                              fallback_used=True,
                              derivation="perfil por strike del motor ITM QUANT",
                              rows=len(engine_rows),
                              detail="Quant Data no sirvió exposure-by-strike en este ciclo")

    # ── v1.50.0 · recorte a los strikes que IMPORTAN ─────────────────────────
    #
    # `exposure-by-strike` devuelve la cadena entera: en DIA a 516 $ eso incluye
    # el 433 y el 473, a un 16 % del precio y con exposición nula, que ocupan
    # media pantalla y empujan hacia arriba la zona que se está mirando.
    #
    # Se recorta por distancia PORCENTUAL —comparable entre un ETF de 9 $ y un
    # índice de 7.500— y se conserva todo strike lejano cuya exposición sea
    # comparable a la de la zona central: un muro real fuera de la banda es
    # justo el caso que más importa.
    from .core.strike_window import relevant_rows
    _spot = _f(state.get("spot"))
    by_strike, strike_meta = relevant_rows(
        by_strike, _spot, symbol=symbol, value_keys=("gex", "dex", "vex", "chex"))

    # ── por vencimiento: mismo orden de autoridad ────────────────────────────
    provider_exp = HUB.exposure_by_expiration(symbol, intel)
    by_exp = list(provider_exp.get("rows") or [])
    expiry_source = "QUANTDATA" if by_exp else None
    if not by_exp:
        for e in _d(state, "expiry_intelligence", "per_expiration", default=[]) or []:
            if not isinstance(e, dict):
                continue
            exp = e.get("expiration") or e.get("expiration_date")
            if not exp:
                continue
            by_exp.append({
                "expiration": str(exp),
                "gex": _f(e.get("gamma"), 0.0), "dex": _f(e.get("delta"), 0.0),
                "vex": _f(e.get("vanna"), 0.0), "chex": _f(e.get("charm"), 0.0),
                "oi": _f(e.get("oi"), 0.0), "volume": _f(e.get("volume"), 0.0),
                "source": "ITM_QUANT",
            })
        if by_exp:
            expiry_source = "ITM_QUANT"

    spot = _f(_d(trace, "profiles", "spot")) or _f(state.get("spot"))
    return {
        "by_strike": by_strike, "by_expiration": by_exp,
        "spot": spot,
        # Qué se recortó y por qué. Un panel que recorta en silencio es
        # indistinguible de un proveedor que no envió esos strikes.
        "strike_window": strike_meta,
        "by_strike_source": exposure_source,
        "by_expiration_source": expiry_source,
        "source_mode": source_mode,
        "state": provider.get("state"),
        "fallback_used": source_mode == DL.FALLBACK,
        # El cálculo propio no desaparece: queda para contrastar las dos
        # construcciones, que es donde está la señal cuando no coinciden.
        "audit": {
            "engine_rows": len(engine_rows),
            "provider_rows": len(provider_rows),
            "engine_by_strike": engine_rows if provider_rows else [],
            "note": ("GEX/DEX del proveedor y del motor son construcciones distintas "
                     "(otro universo de vencimientos, otra hipótesis de dealer). No se "
                     "promedian: se publican y se contrastan."),
        },
        "lineage": provider.get("lineage"),
        **_exposure_shape(by_strike, spot),
        "ready": bool(by_strike or by_exp),
    }


def _exposure_shape(by_strike: List[Dict[str, Any]], spot: float | None) -> Dict[str, Any]:
    """Cómo está repartida la exposición, no sólo cuánta hay.

    El total ya se ve en RESUMEN. Lo que decide si un muro aguanta o se atraviesa es
    la FORMA del perfil: si la gamma está concentrada en tres strikes, esos strikes
    mandan; si está repartida entre cuarenta, ninguno lo hace. Y el reparto por
    encima y por debajo del precio dice hacia dónde empuja la cobertura.
    """
    rows = [r for r in by_strike if _f(r.get("gex")) is not None]
    if not rows:
        return {"concentration_pct": None, "dominant_strike": None, "dominant_gex": None,
                "dominant_distance_pct": None, "gex_below": None, "gex_above": None,
                "gex_balance_pct": None, "strikes_counted": 0, "shape_reason": "SIN PERFIL POR STRIKE"}

    mags = sorted((abs(_f(r["gex"], 0.0) or 0.0) for r in rows), reverse=True)
    total = sum(mags)
    # Concentración = cuánta de toda la exposición vive en los tres strikes mayores.
    concentration = None if total <= 0 else round(100.0 * sum(mags[:3]) / total, 1)

    top = max(rows, key=lambda r: abs(_f(r["gex"], 0.0) or 0.0))
    dom_k = _f(top.get("strike"))
    dom_g = _f(top.get("gex"))

    below = sum(_f(r["gex"], 0.0) or 0.0 for r in rows
                if spot and (_f(r.get("strike")) or 0.0) < spot)
    above = sum(_f(r["gex"], 0.0) or 0.0 for r in rows
                if spot and (_f(r.get("strike")) or 0.0) > spot)
    mass = abs(below) + abs(above)
    # +100 = toda la gamma está por encima del precio; −100 = toda por debajo.
    balance = None if mass <= 0 else round(100.0 * (abs(above) - abs(below)) / mass, 1)

    return {
        "concentration_pct": concentration,
        "dominant_strike": dom_k,
        "dominant_gex": dom_g,
        "dominant_distance_pct": (None if not (spot and dom_k) else
                                  round((dom_k - spot) / spot * 100.0, 3)),
        "gex_below": below if spot else None,
        "gex_above": above if spot else None,
        "gex_balance_pct": balance,
        "strikes_counted": len(rows),
        "shape_reason": None,
    }


# ──────────────────────────────────────────────────────────── interés abierto

def _open_interest(trace: Dict[str, Any], state: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    pos = state.get("positioning") or {}
    levels = state.get("key_levels_report") or {}
    rows = _d(trace, "profiles", "rows", default=[]) or []

    by_strike = []
    for r in rows:
        k = _f(r.get("strike"))
        if k is None:
            continue
        oi = _f(r.get("oi"), 0.0) or 0.0
        vol = _f(r.get("volume_snapshot"), 0.0) or 0.0
        by_strike.append({
            "strike": k, "call_oi": _f(r.get("call_oi"), 0.0), "put_oi": _f(r.get("put_oi"), 0.0),
            "net_oi": _f(r.get("net_oi"), 0.0), "oi": oi, "volume": vol,
            "vol_oi": round(vol / oi, 3) if oi > 0 else None,
        })

    # v1.43.0 · Mismo criterio que EXPOSICIÓN, con la prioridad ya invertida:
    # manda `open-interest-by-strike` del proveedor, y el snapshot de cadena del
    # motor queda como respaldo declarado. El OI NUNCA se reconstruye con volumen:
    # el volumen dice cuántos contratos cambiaron de manos, no cuántos quedaron
    # abiertos, y confundirlos fabrica muros que no existen.
    engine_oi = list(by_strike)
    change = {_f(r.get("strike")): _f(r.get("value")) for r in _qd_rows(intel, "oi_change")}
    provider_oi = []
    for r in _qd_rows(intel, "oi_by_strike"):
        k = _f(r.get("strike"))
        if k is None:
            continue
        peer = next((x for x in engine_oi if x["strike"] == k), {})
        provider_oi.append({
            "strike": k,
            "call_oi": _f(r.get("call")), "put_oi": _f(r.get("put")),
            "net_oi": None, "oi": _f(r.get("value"), 0.0) or 0.0,
            # El volumen no viene con open-interest-by-strike. Se toma del snapshot
            # propio cuando existe; si no, queda vacío, nunca en cero.
            "volume": peer.get("volume"), "vol_oi": peer.get("vol_oi"),
            "oi_change": change.get(k),
            "source": "QUANTDATA",
        })
    if provider_oi:
        by_strike = provider_oi
        oi_source = "QUANTDATA"
        oi_source_mode = "DIRECT_PROVIDER"
    else:
        oi_source = "ITM_QUANT" if by_strike else None
        oi_source_mode = "FALLBACK" if by_strike else "UNAVAILABLE"
        if by_strike:
            from .core import data_lineage as _DL
            from .core import quant_data_hub as _HUB
            _sym = str(state.get("active_symbol") or state.get("symbol") or "").upper()
            _DL.guard_primary_source(
                "QD_OPEN_INTEREST_BY_STRIKE", _sym, publishing_mode=_DL.FALLBACK,
                provider_healthy=_HUB.provider_healthy_for(intel, "oi_by_strike"),
                strict=False, detail="snapshot de cadena del motor")
            _DL.LINEAGE.record("QD_OPEN_INTEREST_BY_STRIKE", _sym,
                               source_mode=_DL.FALLBACK, state=_DL.NO_PROVIDER_DATA,
                               fallback_used=True, rows=len(by_strike),
                               derivation="snapshot de cadena de ITM QUANT",
                               detail="Quant Data no sirvió open-interest-by-strike")

    # Mismo recorte que EXPOSICIÓN: el perfil de interés abierto se lee
    # alrededor del precio, y un strike al que el mercado no puede llegar sólo
    # empuja hacia arriba la zona que importa.
    from .core.strike_window import relevant_rows as _relevant
    by_strike, oi_strike_meta = _relevant(
        by_strike, _f(state.get("spot")),
        symbol=str(state.get("active_symbol") or state.get("symbol") or "").upper(),
        value_keys=("oi", "call_oi", "put_oi"))

    by_exp = [
        {"expiration": str(e.get("expiration") or e.get("expiration_date") or ""), "oi": _f(e.get("oi"), 0.0)}
        for e in (_d(state, "expiry_intelligence", "per_expiration", default=[]) or [])
        if isinstance(e, dict) and (e.get("expiration") or e.get("expiration_date"))
    ]
    if not by_exp:
        by_exp = [{"expiration": str(r.get("expiration")), "oi": _f(r.get("value"), 0.0)}
                  for r in _qd_rows(intel, "oi_by_expiration")]

    # Max Pain es cálculo nativo de ITM QUANT. Su gráfico temporal debe usar la
    # misma autoridad, no desaparecer porque una herramienta REST externa agote
    # cuota o responda tarde. Quant Data queda únicamente como respaldo declarado
    # para instalaciones antiguas que todavía no acumularon historia nativa.
    native_mp = _d(state, "session_memory", "max_pain_over_time", default=[]) or []
    max_pain_time = [
        {"t": r.get("t"), "value": _f(r.get("value"))}
        for r in native_mp if isinstance(r, dict) and r.get("t") is not None and _f(r.get("value")) is not None
    ]
    max_pain_history_source = "ITM_QUANT_CHAIN_HISTORY" if max_pain_time else None
    if not max_pain_time:
        max_pain_time = [
            {"t": r.get("t"), "value": _f(r.get("value"))}
            for r in _qd_rows(intel, "max_pain_over_time")
            if r.get("t") is not None and _f(r.get("value")) is not None
        ]
        if max_pain_time:
            max_pain_history_source = "QUANTDATA_EXTERNAL_FALLBACK"

    # Historia de interés abierto del proveedor: la evolución del OI total de la
    # sesión, que el motor propio no acumula. Se descargaba sin usarse.
    oi_history = [{"t": r.get("t"), "value": _f(r.get("value"))}
                  for r in _qd_rows(intel, "oi_over_time")
                  if r.get("t") is not None and _f(r.get("value")) is not None]

    # v1.47.0 · Los AGREGADOS y el DESGLOSE son datos distintos, de fuentes
    # distintas, y hasta ahora se publicaban como si uno implicara al otro.
    #
    # La cabecera leía `state.positioning` —el agregado de cadena del motor— y
    # `by_strike` leía el perfil del trace o `open-interest-by-strike` del
    # proveedor. Que existiera el primero no decía NADA sobre el segundo, así que
    # la pantalla podía mostrar «OI TOTAL 37.1K» encima de «SIN INTERÉS ABIERTO»
    # y «Sin cadena de opciones cargada». Las dos cosas eran ciertas por separado
    # y juntas se leían como un fallo.
    #
    # Además `_f(pos.get("call_oi"), 0.0) or 0.0` convertía un agregado AUSENTE en
    # un cero, y entonces `total` valía 0 y los porcentajes desaparecían sin que
    # nadie pudiera decir si es que no había OI o si es que no había dato.
    call_oi = _f(pos.get("call_oi"))
    put_oi = _f(pos.get("put_oi"))
    total = (call_oi or 0.0) + (put_oi or 0.0) if (call_oi is not None or put_oi is not None) else None

    # Procedencia de CADA hecho por separado. Es lo que permite responder «¿de
    # dónde salió este número?» sin suponer que el de al lado viene del mismo
    # sitio.
    _agg_src = "ITM_QUANT_CHAIN_POSITIONING" if (call_oi is not None or put_oi is not None) else None
    audit = {
        "total_oi": _agg_src, "call_oi": _agg_src, "put_oi": _agg_src,
        "by_strike": oi_source,
        "by_expiration": ("ITM_QUANT_EXPIRY_INTELLIGENCE"
                          if _d(state, "expiry_intelligence", "per_expiration", default=[])
                          else ("QUANTDATA_OI_BY_EXPIRATION" if by_exp else None)),
        "oi_change": "QUANTDATA_OPEN_INTEREST_CHANGE" if change else None,
        "max_pain": "ITM_QUANT_CHAIN" if _f(levels.get("max_pain")) is not None else None,
        "oi_history": "QUANTDATA_OI_OVER_TIME" if oi_history else None,
    }
    # Por qué falta el desglose teniendo agregados. Un mensaje contradictorio es
    # peor que un hueco: manda a buscar un fallo donde no lo hay.
    if by_strike:
        breakdown_reason = ""
    elif total:
        breakdown_reason = ("el agregado de la cadena está disponible, pero el desglose "
                            "por strike no: el proveedor no sirvió open-interest-by-strike "
                            "y el perfil del motor todavía no tiene filas")
    else:
        breakdown_reason = "no hay interés abierto publicado en este ciclo"

    return {
        "ready": bool(by_strike),
        "max_pain": _f(levels.get("max_pain")),
        "spot": _f(state.get("spot")),
        "total_oi": total, "call_oi": call_oi, "put_oi": put_oi,
        "call_pct": (round(100.0 * (call_oi or 0.0) / total, 1)
                     if total and call_oi is not None else None),
        "put_pct": (round(100.0 * (put_oi or 0.0) / total, 1)
                    if total and put_oi is not None else None),
        "aggregates_available": total is not None,
        "breakdown_available": bool(by_strike),
        "breakdown_reason": breakdown_reason,
        "audit": audit,
        "put_call_ratio": _f(pos.get("put_call_oi_ratio")),
        "top_call_strike": _f(pos.get("top_call_oi_strike")),
        "top_put_strike": _f(pos.get("top_put_oi_strike")),
        "by_strike": by_strike, "by_expiration": by_exp,
        "strike_window": oi_strike_meta,
        "by_strike_source": oi_source,
        "by_strike_source_mode": oi_source_mode,
        "reconstruction": "NEVER_FROM_VOLUME",
        "oi_change": [{"strike": k, "change": v} for k, v in sorted(change.items())
                      if k is not None],
        "oi_change_source": "QUANTDATA_OPEN_INTEREST_CHANGE" if change else None,
        "oi_history": oi_history,
        "oi_history_source": "QUANTDATA_OI_OVER_TIME" if oi_history else None,
        "max_pain_over_time": max_pain_time,
        "max_pain_history_source": max_pain_history_source,
    }


# ─────────────────────────────────────────────────────────────── volatilidad

def _volatilidad(state: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    vol = state.get("volatility") or {}

    # Skew: el motor lo publica por vencimiento; se dibuja contra DTE.
    # v1.43.0 · Volatility Skew y Term Structure son del proveedor. El modelo propio
    # (SVI/SSVI sobre la cadena) no desaparece: viaja en `skew_curve_engine` y
    # `term_structure_engine` para poder contrastar las dos lecturas, y sostiene la
    # vista como respaldo declarado cuando el proveedor no sirve la herramienta.
    engine_skew = []
    for r in vol.get("skew_by_expiry") or []:
        dte = _f(r.get("dte"))
        sk = _f(r.get("skew_25d"))
        if dte is None or sk is None:
            continue
        engine_skew.append({"x": dte, "y": sk, "label": str(r.get("expiration_date") or ""),
                            "call_iv": _f(r.get("call25_iv")), "put_iv": _f(r.get("put25_iv")),
                            "source": "ITM_QUANT"})
    provider_skew = [{"x": r.get("x"), "y": r.get("y"), "label": r.get("label"),
                      "source": "QUANTDATA"}
                     for r in _qd_rows(intel, "volatility_skew")]
    skew_curve = provider_skew or engine_skew
    skew_source = "QUANTDATA" if provider_skew else ("ITM_QUANT" if engine_skew else None)

    engine_term = []
    for r in vol.get("term_structure") or []:
        dte = _f(r.get("dte"))
        iv = _f(r.get("iv"))
        if dte is None or iv is None:
            continue
        # El motor publica IV en fracción; la interfaz muestra puntos porcentuales.
        engine_term.append({"x": dte, "y": iv * 100.0 if iv < 3 else iv,
                            "label": str(r.get("expiration_date") or ""), "source": "ITM_QUANT"})
    provider_term = [{"x": r.get("x"), "y": r.get("y"), "label": r.get("label"),
                      "source": "QUANTDATA"}
                     for r in _qd_rows(intel, "term_structure")]
    term = provider_term or engine_term
    term_source = "QUANTDATA" if provider_term else ("ITM_QUANT" if engine_term else None)

    drift = [{"t": r.get("t"), "value": _f(r.get("value"))} for r in _qd_rows(intel, "volatility_drift")]

    # IV Rank con paridad real: el motor y el proveedor son pares, no uno de
    # confirmación. Antes esto dependía sólo de Quant Data y el número quedaba en
    # blanco toda la sesión si esa herramienta no respondía.
    native = state.get("iv_rank_native") or {}
    iv_summary = _d(state, "source_fusion", "sources", "quantdata", "values", "iv_rank", default={}) or {}
    ranks = [_f(iv_summary.get("call_rank")), _f(iv_summary.get("put_rank"))]
    ranks = [r for r in ranks if r is not None]
    provider_rank = sum(ranks) / len(ranks) if ranks else None
    if provider_rank is None:
        raw = _d(intel, "iv_rank", "raw", default={}) or {}
        provider_rank = _f(_deep_find(raw, "ivRank", "iv_rank", "rank", "ivPercentile"))

    native_rank = _f(native.get("rank")) if native.get("ready") else None
    # v1.43.0 · IV Rank es una métrica que Quant Data entrega directamente, así que
    # manda el proveedor. La lectura propia no se pierde —viaja en
    # `iv_rank_native` y su divergencia se publica—, y sostiene el número como
    # respaldo declarado cuando el proveedor no responde.
    if provider_rank is not None:
        rank_value, rank_source, rank_mode = provider_rank, "QUANTDATA", "DIRECT_PROVIDER"
    elif native_rank is not None:
        rank_value, rank_source, rank_mode = native_rank, "ITM_QUANT", "FALLBACK"
    else:
        rank_value, rank_source, rank_mode = None, None, "UNAVAILABLE"

    rank_reason = None
    if rank_value is None:
        rank_reason = native.get("reason") or "SIN IV RANK DE NINGUNA FUENTE"

    # Discrepancia entre las dos lecturas: no se promedian (miden ventanas
    # distintas), pero una separación grande es información que el analista debe ver.
    divergence = (None if (native_rank is None or provider_rank is None)
                  else round(abs(native_rank - provider_rank), 2))

    return {
        "ready": bool(vol),
        "atm_iv": _f(vol.get("atm_iv")),
        "iv_rank_call": _f(iv_summary.get("call_rank")),
        "iv_rank_put": _f(iv_summary.get("put_rank")),
        "iv_change_pp": _f(vol.get("iv_change_pp")),
        # Si no hubo dos observaciones, la deriva no se ha medido: la sección lo
        # dice en vez de publicar un cero que el panel de al lado desmiente.
        "iv_change_measured": bool(vol.get("iv_change_measured")),
        "skew_25d": _f(vol.get("skew_25d")),
        "regime": vol.get("regime"),
        "regime_measured": bool(vol.get("regime_measured")),
        "term_structure_state": vol.get("term_structure_state"),
        "expected_move": _f(vol.get("expected_move")),
        "expected_low": _f(vol.get("expected_low")), "expected_high": _f(vol.get("expected_high")),
        "iv_rank": rank_value,
        "iv_rank_source": rank_source,
        "iv_rank_reason": rank_reason,
        "iv_rank_provider": provider_rank,
        "iv_rank_native": native_rank,
        "iv_rank_divergence": divergence,
        "iv_percentile": _f(native.get("percentile")) if native.get("ready") else None,
        # Ventana degenerada: todas las lecturas identicas. Ni rank ni percentil
        # significan nada ahi, y la seccion tiene que poder decir por que.
        "iv_window_degenerate": bool(native.get("degenerate_window")),
        "iv_window_reason": native.get("degenerate_reason"),
        "iv_samples": _f(native.get("samples")),
        "iv_window_minutes": _f(native.get("window_minutes")),
        "iv_low": _f(native.get("low")), "iv_high": _f(native.get("high")),
        "iv_median": _f(native.get("median")),
        "iv_rank_note": native.get("note"),
        "iv_rank_source_mode": rank_mode,
        "skew_curve": skew_curve, "term_structure": term, "drift": drift,
        "skew_source": skew_source, "term_structure_source": term_source,
        "drift_source": "QUANTDATA_VOLATILITY_DRIFT" if drift else None,
        # Las dos lecturas conviven para poder contrastarlas; no se promedian.
        "skew_curve_engine": engine_skew,
        "term_structure_engine": engine_term,
    }


# ────────────────────────────────────────────────────────────── estadísticas

def _trade_side_from_provider(intel: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Reparto comprador/vendedor de `trade-side-statistics` de Quant Data.

    Sin cinta observada, las tres barras salían a cero y un cero se lee como «no se
    negoció nada», que es una afirmación falsa sobre el mercado. El proveedor publica
    ese reparto y hasta ahora se descargaba sin usarse.
    """
    rows = _qd_rows(intel, "trade_side_statistics")
    if not rows:
        return []
    ask = sum(_f(r.get("ask_side"), 0.0) or 0.0 for r in rows)
    bid = sum(_f(r.get("bid_side"), 0.0) or 0.0 for r in rows)
    total = sum(abs(_f(r.get("premium"), 0.0) or 0.0) for r in rows)
    out = [{"label": "Comprador (ask)", "value": ask},
           {"label": "Vendedor (bid)", "value": bid}]
    middle = total - ask - bid
    if middle > 0:
        out.append({"label": "Medio / indeterminado", "value": middle})
    return out


def _estadisticas(trace: Dict[str, Any], state: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    prints = trace.get("option_prints") or []
    pos = state.get("positioning") or {}

    # v1.47.0 · La PRIMA se mide sobre impresiones; los CONTRATOS pueden venir de
    # la cadena oficial cuando no hay cinta. Eran dos disponibilidades distintas
    # publicadas como si fueran una: sin prints, `sum(...)` daba 0.0 y la sección
    # mostraba «Contratos negociados 14K» junto a «Prima negociada $0.0», que es
    # una afirmación falsa —no hubo prima cero: no hubo prima observada—.
    contracts = sum(abs(_f(p.get("contracts"), 0.0) or 0.0) for p in prints)
    premium = (sum(abs(_f(p.get("premium"), 0.0) or 0.0) for p in prints)
               if prints else None)
    buy = (sum(abs(_f(p.get("premium"), 0.0) or 0.0) for p in prints
               if (_f(p.get("direction"), 0) or 0) > 0) if prints else None)
    sell = (sum(abs(_f(p.get("premium"), 0.0) or 0.0) for p in prints
                if (_f(p.get("direction"), 0) or 0) < 0) if prints else None)

    # Agregado por contrato a partir de los prints observados: es la estadística
    # de contratos que el analista necesita y no depende de que el proveedor la sirva.
    agg: Dict[str, Dict[str, Any]] = {}
    for p in prints:
        strike = _f(p.get("strike"))
        kind = str(p.get("option_type") or "").upper()[:1]
        if strike is None:
            continue
        label = f"{kind or '?'} {strike:g}"
        slot = agg.setdefault(label, {"label": label, "premium": 0.0, "contracts": 0.0, "trades": 0, "buy": 0.0, "sell": 0.0})
        prem = abs(_f(p.get("premium"), 0.0) or 0.0)
        slot["premium"] += prem
        slot["contracts"] += abs(_f(p.get("contracts"), 0.0) or 0.0)
        slot["trades"] += 1
        d = _f(p.get("direction"), 0) or 0
        if d > 0:
            slot["buy"] += prem
        elif d < 0:
            slot["sell"] += prem
    contract_rows = sorted(agg.values(), key=lambda r: -r["premium"])[:40]
    for r in contract_rows:
        tot = r["buy"] + r["sell"]
        r["bias"] = round(100.0 * (r["buy"] - r["sell"]) / tot, 1) if tot else None
        r["source"] = "PRINTS_OBSERVADOS"

    # Sin prints observados el panel mostraba CONTRATOS NEGOCIADOS = 0 aunque la
    # cadena reportara doce mil contratos de volumen oficial. Cero no es lo mismo
    # que "todavía no he visto ninguna impresión": el primero es una afirmación
    # falsa sobre el mercado. Cuando no hay cinta, se muestra el volumen oficial de
    # la cadena, etiquetado como lo que es.
    volume_source = "PRINTS_OBSERVADOS" if prints else None
    if not prints:
        chain = _d(trace, "profiles", "rows", default=[]) or []
        chain_rows = []
        for r in chain:
            k = _f(r.get("strike"))
            if k is None:
                continue
            cv = _f(r.get("call_volume"), 0.0) or 0.0
            pv = _f(r.get("put_volume"), 0.0) or 0.0
            for kind, vv in (("C", cv), ("P", pv)):
                if vv <= 0:
                    continue
                chain_rows.append({
                    "label": f"{kind} {k:g}", "premium": None, "contracts": vv,
                    "trades": None, "buy": None, "sell": None, "bias": None,
                    "source": "CADENA_OFICIAL",
                })
        # v1.42.7 · Tercer escalón: la estadística de contratos del propio proveedor.
        # `contract_statistics` se pedía cada ciclo y nadie la consumía, así que la
        # tabla quedaba vacía aun teniendo el dato descargado. Entra la última —lo
        # observado y la cadena oficial son más cercanos— y va etiquetada como suya.
        if not chain_rows:
            for r in _qd_rows(intel, "contract_statistics"):
                label = str(r.get("label") or "")
                if not label:
                    continue
                bid = _f(r.get("bid_side"))
                ask = _f(r.get("ask_side"))
                tot = (bid or 0.0) + (ask or 0.0)
                chain_rows.append({
                    "label": label,
                    "premium": _f(r.get("premium")),
                    "contracts": _f(r.get("contracts")),
                    "trades": int(_f(r.get("trades"), 0) or 0) or None,
                    "buy": ask, "sell": bid,
                    "bias": round(100.0 * (ask - bid) / tot, 1) if (tot and ask is not None and bid is not None) else None,
                    "source": "QUANTDATA_CONTRACT_STATISTICS",
                })
        if chain_rows:
            contract_rows = sorted(chain_rows, key=lambda r: -(r["contracts"] or 0.0))[:40]
            contracts = sum(_f(r.get("call_volume"), 0.0) or 0.0 for r in chain)
            contracts += sum(_f(r.get("put_volume"), 0.0) or 0.0 for r in chain)
            volume_source = "CADENA_OFICIAL"
    if volume_source is None and (_f(pos.get("call_volume")) or _f(pos.get("put_volume"))):
        # Último recurso: el agregado que el motor ya publica en positioning.
        contracts = (_f(pos.get("call_volume"), 0.0) or 0.0) + (_f(pos.get("put_volume"), 0.0) or 0.0)
        volume_source = "AGREGADO_DEL_MOTOR"

    market_share = _qd_rows(intel, "market_share")
    if not market_share:
        # Reparto por vencimiento con el volumen que ya calculó el motor.
        market_share = [
            {"label": str(e.get("expiration") or e.get("expiration_date") or ""), "contracts": _f(e.get("volume"), 0.0)}
            for e in (_d(state, "expiry_intelligence", "per_expiration", default=[]) or [])
            if isinstance(e, dict)
        ]

    # Cada KPI con su disponibilidad declarada por separado. Un agregado que
    # existe no implica que exista el detalle del que normalmente se deriva.
    audit = {
        "contracts": volume_source,
        "premium": "PRINTS_OBSERVADOS" if prints else None,
        "buy_premium": "PRINTS_OBSERVADOS" if prints else None,
        "sell_premium": "PRINTS_OBSERVADOS" if prints else None,
        "contract_rows": (contract_rows[0].get("source") if contract_rows else None),
        "market_share": ("QUANTDATA_MARKET_SHARE" if _qd_rows(intel, "market_share")
                         else ("ITM_QUANT_EXPIRY_INTELLIGENCE" if market_share else None)),
    }
    premium_reason = "" if prints else (
        "el volumen viene de la cadena oficial; la prima sólo se puede medir sobre "
        "impresiones y en este ciclo no llegó ninguna"
        if volume_source else "no hay actividad publicada en este ciclo")

    return {
        "ready": bool(prints) or bool(pos),
        "contracts": contracts, "premium": premium,
        "buy_premium": buy, "sell_premium": sell,
        "premium_available": premium is not None,
        "premium_reason": premium_reason,
        "audit": audit,
        # El tamaño medio por impresión sólo existe si hay impresiones: con volumen
        # oficial de cadena no hay "prints" que promediar y el campo viaja vacío.
        "avg_size": round(contracts / len(prints), 1) if prints else None,
        "print_count": len(prints),
        "volume_source": volume_source,
        "volume_reason": None if volume_source else "SIN CINTA NI VOLUMEN DE CADENA",
        "put_call_volume_ratio": _f(pos.get("put_call_volume_ratio")),
        "call_volume": _f(pos.get("call_volume")), "put_volume": _f(pos.get("put_volume")),
        "contract_rows": contract_rows,
        "market_share": market_share,
        # El reparto por lado sale de los prints observados. Si no hubo cinta, se usa
        # la estadística por lado del proveedor en vez de publicar tres ceros, que se
        # leerían como "no se negoció nada".
        # `premium` es ahora None cuando no hay cinta que medir, así que la
        # comparación se hace sobre un valor que puede no existir.
        "trade_side": ([
            {"label": "Comprador (ask)", "value": buy},
            {"label": "Vendedor (bid)", "value": sell},
            {"label": "Medio / indeterminado",
             "value": max(0.0, premium - (buy or 0.0) - (sell or 0.0))},
        ] if (premium or 0.0) > 0 else _trade_side_from_provider(intel)),
        "trade_side_source": ("PRINTS_OBSERVADOS" if (premium or 0.0) > 0
                              else ("QUANTDATA_TRADE_SIDE" if _qd_rows(intel, "trade_side_statistics") else None)),
    }


# ─────────────────────────────────────────────────────────────── dark pool

def _dark_pool(state: Dict[str, Any], trace: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    """DARK POOL con Quant Data como fuente DIRECTA.

    Hasta v1.42.6 esta sección sólo sabía de dark pool lo que podía DEDUCIR del campo
    `venue`/`exchange` de la cinta de otro proveedor: si el código era FINRA/TRF/ADF,
    la ejecución se daba por fuera de bolsa. Esa vía es indirecta por construcción y
    se cae por tres sitios a la vez —que el proveedor publique el venue, que lo
    publique en cada print, y que el código se interprete bien—; cuando alguno
    fallaba, la sección mostraba 0 en todos los activos sin poder decir por qué.

    Ahora la autoridad es Quant Data, que YA SABE qué ejecución fue off-exchange:

        POST /v1/equities/tool/dark-flow          volumen oscuro por intervalo
        POST /v1/equities/tool/dark-pool-levels   niveles de precio con volumen oscuro
        POST /v1/equities/tool/equity-prints      cada impresión individual

    La clasificación por venue se conserva, pero como fuente ADICIONAL y de
    AUDITORÍA: el bloque `audit` contrasta las dos y publica su discrepancia. Dos
    medidas independientes que coinciden valen mucho más que una sola; dos que no
    coinciden son información, no un fallo que haya que esconder.
    """
    lp = state.get("large_prints") or {}
    spot = _f(state.get("spot"))

    # ── 1. Niveles ───────────────────────────────────────────────────────────
    # Primero el proveedor, que mide sobre TODO el volumen oscuro de la sesión.
    # Las zonas propias miden sólo sobre los large prints que la cinta dejó ver.
    levels: List[Dict[str, Any]] = []
    for r in _qd_rows(intel, "dark_pool_levels"):
        price = _f(r.get("price"))
        if price is None:
            continue
        levels.append({
            "price": price, "low": None, "high": None,
            "notional": _f(r.get("notional"), 0.0) or 0.0,
            "shares": _f(r.get("shares"), 0.0) or 0.0,
            "prints": int(_f(r.get("prints"), 0) or 0),
            "concentration": None, "zone_type": None, "side": None, "score": None,
            "distance_pct": None if not spot else round((price - spot) / spot * 100.0, 3),
            "source": "QUANTDATA_DARK_POOL_LEVELS",
        })

    own_levels: List[Dict[str, Any]] = []
    for z in (_d(lp, "off_exchange_liquidity_zones", "zones", default=[]) or []):
        if not isinstance(z, dict):
            continue
        price = _f(z.get("price") or z.get("level") or z.get("center"))
        if price is None:
            continue
        own_levels.append({
            "price": price,
            "low": _f(z.get("low")), "high": _f(z.get("high")),
            "notional": _f(z.get("notional"), 0.0) or 0.0,
            "shares": None,
            "prints": int(_f(z.get("prints") or z.get("count"), 0) or 0),
            "concentration": _f(z.get("concentration")),
            "zone_type": z.get("type"), "side": z.get("side"),
            "score": _f(z.get("score")),
            "distance_pct": None if not spot else round((price - spot) / spot * 100.0, 3),
            "source": "ITM_QUANT_CONFIRMED_OFF_EXCHANGE",
        })
    # Las dos listas conviven: si el proveedor entrega, manda él y las zonas propias
    # quedan detrás como confirmación; si no entrega, las propias sostienen la vista.
    levels_source = "QUANTDATA_DARK_POOL_LEVELS" if levels else (
        "ITM_QUANT_VENUE_CLASSIFICATION" if own_levels else None)
    merged_levels = levels + [z for z in own_levels
                              if not any(abs(z["price"] - l["price"]) < 1e-9 for l in levels)]
    merged_levels.sort(key=lambda r: -(r.get("notional") or 0.0))

    # ── 2. Impresiones individuales ──────────────────────────────────────────
    marks: List[Dict[str, Any]] = []
    qd_prints = _qd_rows(intel, "equity_prints")
    qd_dark = [r for r in qd_prints if r.get("off_exchange") is True]
    for r in qd_dark[:200]:
        marks.append({"t": r.get("t"), "price": _f(r.get("price")),
                      "size": _f(r.get("size"), 0.0) or 0.0,
                      "side": str(r.get("side") or "").upper(),
                      "notional": _f(r.get("notional"), 0.0) or 0.0,
                      "venue": r.get("venue"),
                      "source": "QUANTDATA_EQUITY_PRINTS"})
    own_marks: List[Dict[str, Any]] = []
    for p in (lp.get("off_exchange_top") or [])[:120]:
        if not isinstance(p, dict):
            continue
        price = _f(p.get("price"))
        if price is None:
            continue
        own_marks.append({"t": p.get("timestamp") or p.get("t"), "price": price,
                          "size": _f(p.get("size") or p.get("shares"), 0.0) or 0.0,
                          "side": str(p.get("side") or "").upper(),
                          "notional": _f(p.get("notional"), 0.0) or 0.0,
                          "venue": p.get("venue") or p.get("exchange"),
                          "source": "ITM_QUANT_VENUE_CLASSIFICATION"})
    if not marks:
        marks = own_marks
    prints_source = ("QUANTDATA_EQUITY_PRINTS" if qd_dark else
                     ("ITM_QUANT_VENUE_CLASSIFICATION" if own_marks else None))

    # ── 3. Flujo oscuro por intervalo ────────────────────────────────────────
    # Ésta es la métrica que de verdad define un dark pool: qué PROPORCIÓN del
    # volumen se ejecuta fuera de bolsa, minuto a minuto.
    flow_rows = _qd_rows(intel, "dark_flow")
    flow: List[Dict[str, Any]] = []
    dark_vol = total_vol = 0.0
    for r in flow_rows:
        d = _f(r.get("dark_volume"), 0.0) or 0.0
        tot = _f(r.get("total_volume"))
        dark_vol += d
        if tot:
            total_vol += tot
        flow.append({"t": r.get("t"), "dark_volume": d, "total_volume": tot,
                     "lit_volume": _f(r.get("lit_volume")),
                     "dark_share_pct": _f(r.get("dark_share_pct")),
                     "price": _f(r.get("stock_price"))})
    provider_share = round(100.0 * dark_vol / total_vol, 2) if total_vol > 0 else None

    # ── 4. Auditoría: las dos vías, una al lado de la otra ───────────────────
    own_notional = _f(lp.get("off_exchange_notional"), 0.0) or 0.0
    own_total = _f(lp.get("total_notional"), 0.0) or 0.0
    own_share = round(100.0 * own_notional / own_total, 2) if own_total > 0 else None
    audit = {
        "provider_dark_share_pct": provider_share,
        "venue_classification_share_pct": own_share,
        "delta_pp": (None if (provider_share is None or own_share is None)
                     else round(provider_share - own_share, 2)),
        "note": ("El proveedor mide sobre todo el volumen de la sesión; la "
                 "clasificación por venue mide sólo sobre los large prints que la "
                 "cinta dejó ver. No tienen por qué coincidir: se publican las dos."),
    }

    # ── 5. VWAP y cobertura ──────────────────────────────────────────────────
    candles = trace.get("candles") or []
    candle_source = trace.get("candle_source")
    if not candles:
        # El panel PRECIO / TIEMPO quedaba en blanco cuando la cinta propia no tenía
        # velas, aunque el proveedor publicara la serie de precio del subyacente y ya
        # estuviera descargada. Las velas del proveedor sólo traen cierre: se declara.
        provider_px = _qd_rows(intel, "stock_price_over_time")
        if provider_px:
            candles = [{"t": r.get("t"), "c": _f(r.get("value")), "o": None, "h": None,
                        "l": None, "v": None}
                       for r in provider_px if _f(r.get("value")) is not None]
            if candles:
                candle_source = "QUANTDATA_STOCK_PRICE_OVER_TIME"

    vwap = None
    tv = tpv = 0.0
    for c in candles:
        v = _f(c.get("v"), 0.0) or 0.0
        px = _f(c.get("c"))
        if px is None or v <= 0:
            continue
        tv += v
        tpv += px * v
    if tv > 0:
        vwap = tpv / tv

    cov = None
    if candles and marks:
        ct = [str(c.get("t")) for c in candles if c.get("t")]
        mt = [str(m.get("t")) for m in marks if m.get("t")]
        if ct and mt:
            cov = {"price_from": min(ct), "price_to": max(ct),
                   "prints_from": min(mt), "prints_to": max(mt)}

    largest = None
    if marks:
        largest = max(marks, key=lambda m: m.get("notional") or 0.0)
    elif lp.get("off_exchange_largest"):
        largest = lp.get("off_exchange_largest")

    ready = bool(merged_levels or marks or flow)
    # Sin dato NO se publica un cero: se dice cuál de las tres vías falló.
    from .core import quant_data_hub as _HUB
    from .core import data_lineage as _DL
    _sym = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    _hub_dp = _HUB.dark_pool(_sym, intel)
    data_state = _hub_dp.get("state")
    if ready:
        reason = None
    elif not (flow_rows or qd_prints or _qd_rows(intel, "dark_pool_levels")):
        reason = "QUANT_DATA_SIN_RESPUESTA"
    elif int(_f(lp.get("count"), 0) or 0) == 0:
        reason = "NO_PRINTS"
    else:
        reason = "SIN_OFF_EXCHANGE_CONFIRMADO"

    # v1.43.0 · Un fallo de datos NO puede salir en pantalla como 0. El notional y
    # el recuento sólo llevan número cuando de verdad hubo datos válidos; en
    # cualquier otro caso van en None y la interfaz muestra SIN DATOS. Un cero aquí
    # afirmaba «hoy no hubo dark pool», que es una conclusión, no un hueco.
    _marks_notional = sum(m.get("notional") or 0.0 for m in marks) if marks else None
    _notional = (_marks_notional if _marks_notional is not None
                 else (_hub_dp.get("notional")
                       if _hub_dp.get("notional") is not None
                       else (own_notional if (own_notional and data_state == _DL.DATA_OK) else None)))
    _count = (len(marks) or int(_f(lp.get("off_exchange_count"), 0) or 0)) or None

    # ── 6 · DarkPoolViewModel · la ÚNICA salida que consume la pantalla ──────
    #
    # Se arma desde los TRES carriles del proveedor y nada más. Las zonas de
    # liquidez propias y la clasificación por venue siguen viajando en `audit`,
    # pero ya no pueden decidir si esta sección tiene datos: eran capas DERIVADAS
    # bloqueando a la fuente DIRECTA, y por eso el Auditor decía «608 filas» al
    # lado de un panel que decía SIN DATOS.
    from .core import dark_pool_view as _DPV
    from .providers.quantdata.tools import last_valid_session_date as _session
    view_model = _DPV.build(
        flow_block=_qd_block(intel, "dark_flow"),
        levels_block=_qd_block(intel, "dark_pool_levels"),
        prints_block=_qd_block(intel, "equity_prints"),
        spot=spot,
        symbol=str(state.get("active_symbol") or state.get("symbol") or "").upper(),
        session={"resolved": _session(), "authority": "MarketSessionResolver"},
    )

    return {
        "ready": ready or bool(view_model.get("ready")),
        "view_model": view_model,
        "state": data_state,
        "data_state_detail": _hub_dp.get("detail") or None,
        "source_mode": _hub_dp.get("source_mode"),
        "display": None if ready else _DL.NO_DATA_LABEL,
        "vwap": vwap,
        "coverage": cov,
        "candle_source": candle_source,
        "count": _count,
        "notional": _notional,
        "off_exchange_count": _count,
        "off_exchange_notional": _notional,
        "large_print_count": int(_f(lp.get("count"), 0) or 0),
        "large_print_notional": own_total or None,
        # La proporción del proveedor manda cuando existe: mide sobre el volumen
        # total real, no sólo sobre los large prints que la cinta dejó ver.
        "off_exchange_share_pct": provider_share if provider_share is not None else own_share,
        "off_exchange_share_source": ("QUANTDATA_DARK_FLOW" if provider_share is not None
                                      else ("ITM_QUANT_VENUE_CLASSIFICATION" if own_share is not None else None)),
        "off_exchange_largest": largest,
        "off_exchange_top": marks[:25],
        "dark_volume": dark_vol if flow else None,
        "trades": _hub_dp.get("trades"),
        "lineage": _hub_dp.get("lineage"),
        "total_volume": total_vol if total_vol > 0 else None,
        "flow": flow,
        "dominant_level": merged_levels[0]["price"] if merged_levels else None,
        "dominant_notional": merged_levels[0]["notional"] if merged_levels else None,
        "levels": merged_levels[:40],
        "prints": marks,
        "audit": audit,
        "candles": candles,
        "reason": reason,
        # v1.46.0 · La pantalla del analista sigue diciendo SIN DATOS; aquí viaja
        # CUÁL de los ocho estados internos lo produjo, por carril, para que el
        # Auditor pueda decir si hay que corregir el payload, esperar al mercado o
        # reintentar. `screen` es lo único que la terminal está autorizada a pintar.
        "diagnosis": _hub_dp.get("diagnosis"),
        "lanes": _hub_dp.get("lane_rows"),
        "flow_fields": _hub_dp.get("flow_fields"),
        "display_reason": ((_hub_dp.get("diagnosis") or {}).get("screen") or None) if not ready else None,
        "latest_stock_price": (_qd_block(intel, "dark_pool_levels") or {}).get("latest_stock_price"),
        "sources": {
            "levels": levels_source,
            "prints": prints_source,
            "flow": "QUANTDATA_DARK_FLOW" if flow else None,
            "primary": "QUANT_DATA",
            "secondary": "ITM_QUANT_VENUE_CLASSIFICATION",
        },
        "semantics": "PROVIDER_DIRECT_WITH_VENUE_AUDIT",
    }


# ─────────────────────────────────────────────────────────────────── bundle

def _net_drift(state: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    """Net Drift OFICIAL de Quant Data para el símbolo ACTIVO.

    La única fuente es el bloque `net_drift` del proveedor, que viene de
    `POST /v1/options/tool/net-drift`. No se sustituye por Net Flow, ni por QFLOW,
    ni por GEX/DEX si falta: si el proveedor no entrega, la sección dice SIN DATOS.
    Rellenar el hueco con otra magnitud sería publicar una curva que el operador
    leería como Net Drift sin serlo.

    El ticker sale del estado, nunca de una constante: cualquier activo que el
    proveedor sirva recorre exactamente este camino.
    """
    from .core.net_drift import build_net_drift, NO_PROVIDER_DATA

    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    block = intel.get("net_drift") if isinstance(intel, dict) else None
    # El Order Flow viaja SIEMPRE, también cuando no hay curva. Que Net Drift falte
    # no implica que falte la cinta, y publicar la clave sólo en el camino feliz
    # obliga al consumidor a distinguir "no hay cinta" de "la clave no vino".
    order_flow = _net_drift_order_flow(intel)

    if not isinstance(block, dict):
        return {**build_net_drift(None, symbol=symbol), "state": NO_PROVIDER_DATA,
                "detail": "sin datos de deriva neta en este ciclo",
                "order_flow": order_flow}
    if not block.get("ready"):
        err = block.get("error") or block.get("reason") or block.get("detail")
        if err:
            return {**build_net_drift(None, symbol=symbol, provider_error=str(err)),
                    "order_flow": order_flow}
        return {**build_net_drift(None, symbol=symbol), "state": NO_PROVIDER_DATA,
                "detail": "la serie de deriva neta aún no está lista",
                "order_flow": order_flow}

    out = build_net_drift(block.get("rows"), symbol=symbol)
    out["tool"] = "net_drift"
    out["route"] = block.get("route") or block.get("path")
    out["fetched_at"] = block.get("fetched_at")
    out["order_flow"] = order_flow
    return out


def _net_drift_order_flow(intel: Dict[str, Any]) -> Dict[str, Any]:
    """Impresiones del Order Flow de Quant Data para explicar un punto de la curva.

    Seleccionar un punto de Net Drift tiene que poder responder «¿qué operaciones
    produjeron este movimiento?», y la respuesta legítima son las impresiones del
    MISMO proveedor que publicó la curva. La cinta propia sirve de respaldo, pero
    mezclar las dos sin decirlo haría imposible saber qué se está mirando.

    Se prefiere la cinta SIN CONSOLIDAR: es la impresión individual, que es la
    granularidad de la pregunta. La consolidada agrega por contrato y perdería
    justo el detalle —cuántas operaciones, a qué hora exacta— que se busca.
    """
    for key in ("options_order_flow_raw", "options_order_flow"):
        rows = _qd_rows(intel, key)
        if rows:
            blk = _qd_block(intel, key)
            return {"ready": True, "source": "QUANTDATA", "tool": key,
                    "consolidated": key == "options_order_flow",
                    "rows": rows[:1500], "count": len(rows),
                    # La cobertura del lado agresor viaja con la cinta: es lo que
                    # convierte «todas las marcas salen neutras» en un diagnóstico.
                    "aggressor_coverage": blk.get("aggressor_coverage") or {}}
    block = intel.get("options_order_flow_raw") if isinstance(intel, dict) else None
    detail = None
    if isinstance(block, dict):
        detail = block.get("error") or block.get("reason")
    return {"ready": False, "source": "QUANTDATA", "tool": "options_order_flow_raw",
            "rows": [], "count": 0,
            "detail": detail or "sin cinta de opciones en este ciclo",
            "fallback": "cinta propia de opciones"}


def _qflow(state: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    """QFLOW: serie sobre `net-flow` + concentraciones ATRIBUIDAS con `order-flow`.

    v1.43.0 · QFLOW ya no se queda en Net Flow. Net Flow sigue dando la serie base
    —es la prima neta por intervalo, con su `stockPrice`—, pero una concentración
    sin atribuir es un pico anónimo: no dice si fueron calls o puts, ni comprados o
    vendidos, ni en qué strike, ni a qué vencimiento, ni si fue un BLOCK negociado
    o un SWEEP barriendo bolsas. Eso está en `order-flow/consolidated` y
    `order-flow/unconsolidated`, y es lo que se cruza aquí.

    El umbral de «concentración importante» se mide sobre el PROPIO activo. No hay
    ningún límite fijo en dólares, así que la misma regla vale para un ETF enorme y
    para una acción mediana.

    El ticker sale siempre del estado, nunca de una constante.
    """
    from .core.qflow import build_qflow, NO_PROVIDER_DATA

    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    block = intel.get("net_flow") if isinstance(intel, dict) else None

    # La cinta viaja SIEMPRE, también cuando no hay serie: que falte Net Flow no
    # implica que falte el Order Flow, y publicar la clave sólo en el camino feliz
    # obliga al consumidor a distinguir «no hubo» de «no vino».
    of = _net_drift_order_flow(intel)
    of_rows = of.get("rows") or []
    of_tool = of.get("tool") or ""

    if not isinstance(block, dict):
        return {**build_qflow(None, symbol=symbol), "state": NO_PROVIDER_DATA,
                "detail": "sin serie de flujo neto en este ciclo",
                "order_flow": of}
    if not block.get("ready"):
        err = block.get("error") or block.get("reason") or block.get("detail")
        if err:
            return {**build_qflow(None, symbol=symbol, provider_error=str(err)),
                    "order_flow": of}
        return {**build_qflow(None, symbol=symbol), "state": NO_PROVIDER_DATA,
                "detail": "la serie de flujo neto aún no está lista",
                "order_flow": of}

    out = build_qflow(block.get("rows"), symbol=symbol,
                      order_flow=of_rows, order_flow_tool=of_tool,
                      order_flow_block=of)
    out["provider"] = "QUANT_DATA"
    out["tool"] = "net_flow"
    out["route"] = block.get("route")
    out["order_flow"] = of
    return out


def _flujo_ordenes(state: Dict[str, Any], intel: Dict[str, Any],
                   trace: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """FLUJO DE ÓRDENES: la sección que ya existe, con todo lo que le faltaba dentro.

    No es una sección nueva. Es la misma, reuniendo las seis piezas que la
    especificación pide que convivan:

        Net Flow · Net Drift · Order Flow consolidado · Order Flow sin consolidar
        · QFLOW · concentración QFLOW

    Net Drift viene del endpoint OFICIAL. No se reconstruye desde GEX, DEX, Net
    Flow ni de ninguna fórmula antigua: si el proveedor no entrega, dice SIN DATOS.
    """
    from .core import quant_data_hub as HUB

    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    # El Hub registra la procedencia de los cuatro datasets de flujo. Antes se
    # consumían directos desde aquí y no dejaban rastro en el Auditor.
    HUB.flow(symbol, intel)
    qflow = _qflow(state, intel)
    drift = _net_drift(state, intel)
    net_flow_block = intel.get("net_flow") if isinstance(intel, dict) else None
    net_flow_rows = (net_flow_block or {}).get("rows") if isinstance(net_flow_block, dict) else None

    # ── FlowViewModel · politica UNICA de frescura ───────────────────────────
    #
    # v1.55.0 · Hasta aqui la seccion tenia un estado GLOBAL: si el ultimo ciclo
    # venia vacio, TODO se vaciaba, y en pantalla convivian «405 buckets» con
    # tres tarjetas diciendo SIN DATOS. Dos afirmaciones contrarias sobre el
    # mismo dato.
    #
    # Ahora cada carril lleva su propio ultimo valor bueno, indexado por
    # (simbolo, fecha de sesion, dataset), y dice si lo que muestra es de este
    # ciclo o de hace diez minutos. «No llego nada nuevo» y «no hay nada» dejan
    # de dibujarse igual.
    unconsolidated = _order_flow_block(intel, "options_order_flow_raw")
    consolidated = _order_flow_block(intel, "options_order_flow")
    tape_rows = unconsolidated.get("rows") or consolidated.get("rows") or []
    tape = _tape_totals(tape_rows)
    tape["buckets"] = tape_rows or None
    view_model = _flow_view_model(symbol, state, tape, net_flow_rows, qflow)

    return {
        "symbol": symbol,
        # El modelo que la seccion CONSUME. Las tarjetas leen de aqui su valor,
        # su estado y su edad; no vuelven a deducirlos por su cuenta.
        "view_model": view_model,
        "net_flow": {
            "ready": bool(net_flow_rows),
            "rows": list(net_flow_rows or []),
            "source_mode": ("DIRECT_PROVIDER" if net_flow_rows else "UNAVAILABLE"),
            "tool": "net_flow",
            "display": None if net_flow_rows else "SIN DATOS",
        },
        "net_drift": drift,
        "order_flow_consolidated": consolidated,
        "order_flow_unconsolidated": unconsolidated,
        "qflow": qflow,
        "qflow_concentration": {
            "events": qflow.get("events") or [],
            "markers": qflow.get("markers") or [],
            "attribution": qflow.get("attribution") or {"ready": False},
            "threshold": qflow.get("threshold"),
            "normalization": "ASSET_RELATIVE_TRIPLE_GATE",
            "source_mode": "DERIVED",
        },
        "qflow_level": qflow.get("level"),
        "ready": bool(qflow.get("ready") or drift.get("ready") or net_flow_rows),
        "section": "FLUJO_DE_ORDENES",
        "note": ("Una sola sección de flujo. Net Drift es del endpoint oficial; QFLOW "
                 "es cálculo propio de ITM QUANT sobre datos del proveedor."),
    }


def _tape_totals(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Primas de la cinta, repartidas SOLO por agresor resuelto.

    Un `CALL` no es una compra y un `PUT` no es una venta: lo que decide el lado
    es quien cruzo el spread. La prima cuyo print no trae agresor no se reparte
    a ningun lado — se queda en su propio cubo, visible, en vez de inclinar el
    sesgo hacia el lado que toque por azar.

    Si no hubo NINGUN print, todas las magnitudes salen `None`, no cero: «no se
    negocio prima» es una conclusion y aqui no hay con que sostenerla.
    """
    from .core.aggressor import BUY, SELL

    buy = sell = unknown = total = None
    prints = 0
    volume = None
    largest = None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        prints += 1
        prem = _f(r.get("premium"))
        size = _f(r.get("size"))
        if size is not None:
            volume = size if volume is None else volume + size
        if prem is None:
            continue
        mag = abs(prem)
        total = mag if total is None else total + mag
        agg = str(r.get("aggressor") or "").upper()
        if agg == BUY:
            buy = mag if buy is None else buy + mag
        elif agg == SELL:
            sell = mag if sell is None else sell + mag
        else:
            unknown = mag if unknown is None else unknown + mag
        if largest is None or mag > abs(_f(largest.get("premium")) or 0.0):
            largest = {"t": r.get("t"), "premium": prem, "strike": r.get("strike"),
                       "option_type": r.get("option_type"), "aggressor": r.get("aggressor"),
                       "execution": r.get("execution")}
    classified = None
    if buy is not None or sell is not None:
        classified = (buy or 0.0) + (sell or 0.0)
    return {
        "buy_premium": buy, "sell_premium": sell, "total_premium": total,
        "unknown_premium": unknown, "largest_print": largest,
        "volume": volume, "prints": (rows or None) and list(rows or [])[:400] or None,
        "print_count": prints,
        # Cobertura del agresor: un 8 % clasificado no sostiene la misma lectura
        # que un 95 %, y esa cifra tiene que viajar junto al sesgo.
        "aggressor_coverage_pct": (None if not total else round(100.0 * (classified or 0.0) / total, 2)),
        "classified_premium": classified,
    }


def wall_consistency(trace: Dict[str, Any], resumen: Dict[str, Any]) -> Dict[str, Any]:
    """¿Los tres sitios donde sale un muro dicen el MISMO precio?

    v1.55.0 · El Wall Engine ya resuelve los muros una sola vez, pero «hay una
    autoridad» es una afirmacion de arquitectura y esto la vuelve comprobable.
    El defecto original no fue que el calculo estuviera mal: fue que cada seccion
    llamaba a `structural_walls()` con su propio frame, asi que el mismo nombre
    señalaba dos strikes distintos en dos pantallas a la vez y ninguna avisaba.

    Se comparan los tres consumidores:

        MOTOR    trace['walls'][lado]['strike']     lo que resolvio el Wall Engine
        TRACE    trace['levels'] con ese `kind`     la linea que se dibuja
        RESUMEN  resumen[lado]                      la cifra de la tarjeta

    La comparacion es EXACTA sobre el precio publicado: un muro es un strike, no
    una estimacion, asi que una diferencia de un centimo ya es dos autoridades.
    """
    walls = (trace or {}).get("walls") or {}
    levels = [lv for lv in ((trace or {}).get("levels") or []) if isinstance(lv, dict)]
    res = resumen or {}
    rows = []
    for side in ("call_wall", "put_wall"):
        engine = _f((walls.get(side) or {}).get("strike"))
        drawn = [_f(lv.get("price")) for lv in levels if lv.get("kind") == side]
        card = _f(res.get(side))
        # Mas de una linea con el mismo `kind` YA es el defecto, aunque coincidan.
        duplicated = len(drawn) > 1
        values = [v for v in ([engine] + drawn + [card]) if v is not None]
        agree = (not duplicated) and (len(set(values)) <= 1)
        rows.append({
            "side": side,
            "engine": engine,
            "trace_levels": drawn,
            "resumen": card,
            "authority": "ITMQ_WALL_ENGINE",
            "source_mode": (walls.get(side) or {}).get("source_mode"),
            "duplicated_lines": duplicated,
            "agree": agree,
            "detail": ("" if agree else
                       "mas de una linea con el mismo nombre" if duplicated else
                       "el motor, la linea dibujada y la tarjeta no coinciden"),
        })
    ok = all(r["agree"] for r in rows)
    return {
        "ok": ok, "rows": rows,
        "detail": ("los muros coinciden en motor, TRACE y RESUMEN" if ok else
                   "hay mas de una autoridad de muros en pantalla"),
        "note": ("TRACE y FLUJO DE ORDENES dibujan la MISMA lista `levels`, asi "
                 "que comprobarla una vez los cubre a los dos."),
    }


def _flow_view_model(symbol: str, state: Dict[str, Any], tape: Dict[str, Any],
                     net_flow_rows: Any, qflow: Dict[str, Any]) -> Dict[str, Any]:
    """Arma el FlowViewModel con la sesion y el estado de mercado reales.

    `tape` y `net_flow` son DATASETS DISTINTOS y entran por separado: la ausencia
    de uno no puede vaciar al otro. Mezclarlos era la causa de que Net Flow
    desapareciera cuando la cinta venia sin prints.
    """
    from .core import flow_view as _FV
    from .core import session_mode as _SM
    from .providers.quantdata.tools import last_valid_session_date as _session

    try:
        ses = _SM.resolve()
        market_open = ses["mode"] in (_SM.LONDON_MONITOR, _SM.NEW_YORK)
        session_date = ses["trading_date"] if market_open else _session()
        model = _FV.build(
            symbol=symbol, session_date=session_date, market_open=market_open,
            tape=tape,
            net_flow={"series": list(net_flow_rows) if net_flow_rows else None},
            qflow={"markers": (qflow or {}).get("markers") or None},
        )
        model["session_mode"] = ses["mode"]
        model["aggressor_coverage_pct"] = tape.get("aggressor_coverage_pct")
        return model
    except Exception as exc:
        _obs_note("terminal_api:flow_view_model", exc, severity="DEGRADED")
        return {"symbol": symbol, "freshness": {"status": _flow_view_error_state()},
                "error": f"{type(exc).__name__}"}


def _flow_view_error_state() -> str:
    from .core import flow_view as _FV
    return _FV.PROVIDER_ERROR


def _order_flow_block(intel: Dict[str, Any], key: str) -> Dict[str, Any]:
    """La cinta de opciones tal y como la publica el proveedor, con su estado."""
    from .core import quant_data_hub as HUB

    block = intel.get(key) if isinstance(intel, dict) else None
    cls = HUB.classify(block if isinstance(block, dict) else None, cadence="FAST")
    rows = _qd_rows(intel, key)
    return {
        "ready": bool(rows), "rows": rows[:1500], "count": len(rows),
        "tool": key, "consolidated": key == "options_order_flow",
        "state": cls["state"], "detail": cls["detail"],
        "source_mode": ("DIRECT_PROVIDER" if rows else "UNAVAILABLE"),
        "display": None if rows else "SIN DATOS",
    }


def _dark_pool_rows(row, intel: Dict[str, Any] | None) -> List[Dict[str, Any]]:
    """Las tres filas de Dark Pool, desde los carriles del PROVEEDOR.

    Cada carril es independiente y lleva su propio estado: que Equity Prints
    esté en MARKET_CLOSED no puede hacer que Dark Flow parezca vacío.
    """
    intel = intel if isinstance(intel, dict) else {}
    out: List[Dict[str, Any]] = []
    for label, key, tool in (
        ("DARK POOL · dark flow", "dark_flow", "QUANTDATA_DARK_FLOW"),
        ("DARK POOL · niveles", "dark_pool_levels", "QUANTDATA_DARK_POOL_LEVELS"),
        ("DARK POOL · prints de equity", "equity_prints", "QUANTDATA_EQUITY_PRINTS"),
    ):
        block = _qd_block(intel, key)
        rows_ = _qd_rows(intel, key)
        reason = (block.get("schema_detail") or block.get("error")
                  or block.get("reason") or block.get("detail")
                  or "EL_PROVEEDOR_NO_DEVOLVIO_FILAS")
        out.append(row(label, bool(rows_), len(rows_), tool, reason))
    return out


def build_diagnostics(*, state: Dict[str, Any], trace: Dict[str, Any],
                      coverage: Dict[str, Any] | None = None,
                      parity: Dict[str, Any] | None = None,
                      intel: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Por qué cada panel tiene o no tiene datos, sin tener que adivinarlo.

    Cada fila responde una sola pregunta: ¿hay dato?, ¿de dónde viene?, y si no
    lo hay, ¿cuál es la causa exacta en esta ejecución? Es la herramienta para
    distinguir "el proveedor no entrega" de "el motor aún no publicó" de
    "la interfaz no lo está pintando".
    """
    candles = trace.get("candles") or []
    prints = trace.get("option_prints") or []
    rows = _d(trace, "profiles", "rows", default=[]) or []
    heat = trace.get("heatmap_history") or {}
    levels = trace.get("levels") or []
    lp = state.get("large_prints") or {}
    zones = _d(lp, "off_exchange_liquidity_zones", "zones", default=[]) or []
    cov = coverage or {}

    # Un bloqueo de publicación apaga toda la estructura a la vez. Decirlo una
    # sola vez y con su motivo evita que el analista busque nueve causas distintas
    # para lo que en realidad es una.
    blocked_motive = trace.get("publication_blocked_motive") if trace.get("blocked") else None
    structural = {"TRACE · perfiles por strike", "TRACE · heatmap", "TRACE · niveles",
                  "EXPOSICIÓN · por vencimiento"}

    def row(panel: str, ok: bool, count: Any, source: str, reason: str | None = None) -> Dict[str, Any]:
        why = reason or "SIN_DATO"
        if not ok and blocked_motive and panel in structural:
            why = f"PUBLICACIÓN RETENIDA · {blocked_motive}"
        # v1.42.1 · Un panel vacío con el mercado cerrado no es una avería, y mostrarlo
        # igual que una entrenaba a ignorar los dos. Si la sesión explica el vacío, se
        # dice eso; sólo cuando el mercado está ABIERTO y aun así no llega nada, el
        # panel se marca como problema real.
        state_flag = "CON DATOS" if ok else "SIN DATOS"
        # El contexto de sesión sólo rellena el hueco cuando NO hay un motivo
        # específico. Un `BOOTSTRAP_EMPTY` dice exactamente qué falló y es más útil
        # que «mercado cerrado»; taparlo sería perder la única pista accionable.
        generic = (not reason) or str(reason) in _GENERIC_REASONS
        if not ok and generic and not (blocked_motive and panel in structural):
            ctx = session_resolver.empty_reason(
                channel=panel, kind="EQUITY" if _is_equity_panel(panel) else "OPTION")
            if not ctx["is_failure"]:
                why = ctx["detail"]
                state_flag = ctx["state"]
        return {"panel": panel, "ok": bool(ok), "count": count, "state": state_flag,
                "source": source, "reason": None if ok else why}

    # v1.52.1 · El diagnóstico de Dark Pool mira los carriles del PROVEEDOR.
    #
    # Declaraba como origen las capas DERIVADAS —la clasificación por venue de la
    # cinta propia y las zonas de liquidez—, así que con cientos de filas del
    # proveedor ya descargadas el Auditor seguía señalando a la cinta propia como
    # autoridad. Un diagnóstico que no mira la misma fuente que la vista no sirve
    # para diagnosticar nada.
    checks = [
        row("TRACE · velas", bool(candles), len(candles),
            trace.get("candle_source") or "PRICE_TICK_FABRIC",
            trace.get("candle_backfill_reason") or _candles_reason(trace)),
        row("TRACE · perfiles por strike", bool(rows), len(rows), "ITM_QUANT_CHAIN",
            _d(trace, "profiles", "reason") or "CADENA_NO_HIDRATADA"),
        row("TRACE · heatmap", heat.get("ready") is True, len(heat.get("times") or []),
            "ITM_QUANT_CHAIN_HISTORY", heat.get("reason") or "SIN_HISTORIA_ESTRUCTURAL"),
        row("TRACE · niveles", bool(levels), len(levels), "ITM_QUANT_STRUCTURE", "SCANNER_NO_PUBLICO_NIVELES"),
        row("FLUJO · prints de opciones", bool(prints), len(prints), "OPRA_OBSERVED",
            _d(state, "opra_diagnostics", "status") or "SIN_PRINTS_OBSERVADOS"),
        *_dark_pool_rows(row, intel),
        row("VOLATILIDAD · skew", bool(_d(state, "volatility", "skew_by_expiry", default=[])),
            len(_d(state, "volatility", "skew_by_expiry", default=[]) or []), "ITM_QUANT_CHAIN", "SIN_CADENA"),
        row("EXPOSICIÓN · por vencimiento",
            bool(_d(state, "expiry_intelligence", "per_expiration", default=[])),
            len(_d(state, "expiry_intelligence", "per_expiration", default=[]) or []),
            "ITM_QUANT_EXPIRY_INTELLIGENCE", "SIN_DESGLOSE_POR_VENCIMIENTO"),
    ]

    blocked = state.get("publication_gate") or {}
    return {
        "publication_blocked": bool(trace.get("blocked")),
        "publication_blocked_motive": blocked_motive,
        "publication_gate_state": blocked.get("estado"),
        "symbol": state.get("active_symbol"),
        "mode": state.get("mode"),
        "engine_ready": bool(state.get("ready")),
        "data_age_seconds": _f(state.get("data_age_seconds")),
        "publication_allowed": blocked.get("publicar_permitido"),
        "checks": checks,
        "failing": [c["panel"] for c in checks if not c["ok"]],
        "quantdata": {
            "configured": cov.get("configured"),
            "live_tools": cov.get("live_tools"),
            "total_tools": cov.get("total_tools"),
            "shared_with_engine": cov.get("shared_with_engine"),
            "quota": cov.get("quota"),
            "unavailable": [t["title"] for t in (cov.get("tools") or []) if t.get("state") == "NO_DISPONIBLE"],
            "degraded": [t["title"] for t in (cov.get("tools") or []) if t.get("state") == "DEGRADADO"],
        },
        "providers": [
            {"provider": p.get("provider"), "status": p.get("status"), "age_seconds": p.get("age_seconds"),
             "error": p.get("error")}
            for p in ((parity or {}).get("providers") or [])
        ],
    }


# ─────────────────────────────────────────────────────────────────── macro

# Un día de sesión regular son 390 minutos; el año operativo, 252 de esos días.
# Es el mismo reloj que usa el laboratorio de escenarios, así que el rango de la
# sesión y el Monte Carlo no pueden contradecirse por usar convenciones distintas.
SESSION_MINUTES = 390.0
TRADING_DAYS = 252.0


def _hist(series: Dict[str, Any], limit: int = 120) -> List[Dict[str, Any]]:
    """Historia de una serie FRED en el formato {t, v} que dibuja la interfaz."""
    out: List[Dict[str, Any]] = []
    for h in (series or {}).get("history") or []:
        if not isinstance(h, dict):
            continue
        t = h.get("date") or h.get("t")
        v = _f(h.get("value") if "value" in h else h.get("v"))
        if t is None or v is None:
            continue
        out.append({"t": str(t), "v": v})
    return out[-max(1, int(limit)):]


def _session_minutes_left(state: Dict[str, Any]) -> float:
    """Minutos que le quedan a la sesión regular de Nueva York.

    Fuera del horario regular devuelve la sesión completa: la pregunta entonces
    es cuánto puede moverse el precio en la *próxima* sesión, no en lo que resta
    de una que ya cerró.
    """
    meta = state.get("meta") or {}
    ms = str(meta.get("market_state") or "").upper()
    raw = _f(_deep_find(state, "minutes_to_close", "session_minutes_remaining"))
    if raw is not None and 0.0 < raw <= SESSION_MINUTES:
        return raw
    if "REGULAR" not in ms and "OPEN" not in ms and "LIVE" not in ms:
        return SESSION_MINUTES
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/New_York"))
        close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        left = (close - now).total_seconds() / 60.0
        return SESSION_MINUTES if left <= 0 else min(left, SESSION_MINUTES)
    except Exception:
        return SESSION_MINUTES


def _session_outlook(state: Dict[str, Any], macro: Dict[str, Any],
                     stress: Dict[str, Any]) -> Dict[str, Any]:
    """Qué implica el calendario macro para la sesión de Nueva York.

    Procedencia explícita, porque aquí es donde más tienta inventar:
      * El rango sale de la volatilidad implícita ATM que ya cotiza el mercado de
        opciones, escalada al tiempo que queda de sesión. No es una predicción:
        es el movimiento que las propias opciones están pagando.
      * El sesgo sale de lo que el motor ya midió (Scanner, GEX neto, flujo, curva
        y crédito). No hay consenso de analistas en ninguna fuente conectada, así
        que no se presenta ninguno: lo que se publica es la lectura del mercado.
    """
    spot = _f(state.get("spot"))
    vol = state.get("volatility") or {}
    iv = _f(vol.get("atm_iv")) or _f(vol.get("iv30")) or _f(_deep_find(vol, "atm_iv", "iv"))
    minutes = _session_minutes_left(state)

    band: Dict[str, Any] = {"low": None, "high": None, "low_2s": None, "high_2s": None,
                            "expected_move": None, "expected_move_pct": None}
    if spot and spot > 0 and iv and iv > 0:
        t = max(minutes, 1.0) / (TRADING_DAYS * SESSION_MINUTES)
        sigma = (iv / 100.0) * math.sqrt(t)
        # El estrés macro ensancha la varianza del escenario; nunca inclina el signo.
        sigma *= 1.0 + min(1.0, abs(_f(stress.get("score"), 0.0) or 0.0) / 100.0) * 0.35
        # El movimiento se redondea UNA vez y de ahí salen las cuatro bandas. Redondear
        # cada una por separado hacía que la banda de 2σ no fuera exactamente el doble
        # de la de 1σ publicada, y quien compare las dos cifras en pantalla tiene razón
        # en esperar que cuadren.
        move = round(spot * sigma, 4)
        band = {
            "low": round(spot - move, 4), "high": round(spot + move, 4),
            "low_2s": round(spot - 2.0 * move, 4), "high_2s": round(spot + 2.0 * move, 4),
            "expected_move": move,
            "expected_move_pct": round(move / spot * 100.0, 3),
        }

    scanner = state.get("scanner") or {}
    pos = state.get("positioning") or {}
    flow = state.get("flow") or {}
    curve = _f(_d(macro, "series", "T10Y2Y", "value"))
    credit_z = _f(_d(macro, "series", "BAMLH0A0HYM2", "z"))

    votes: List[Dict[str, Any]] = []

    # Los pesos no son opinión: separan lo que mueve una sesión de lo que mueve un
    # trimestre. La curva y el crédito son contexto de fondo; medir su voto igual
    # que el del flujo del día haría que dos series que apenas cambian decidieran
    # el sesgo intradía.
    def _vote(name: str, value: Any, sign: int, weight: float, detail: str,
              kind: str = "DIRECCIONAL") -> None:
        votes.append({"factor": name, "value": value, "sign": int(sign),
                      "weight": float(weight), "detail": detail, "kind": kind})

    # El motor publica la dirección en español y en inglés según el módulo que la
    # escriba. Reconocer sólo una de las dos formas dejaba a Scanner como NEUTRAL
    # con una dirección declarada delante.
    d = str(scanner.get("direction") or "").upper()
    up = ("ALCIS", "LONG", "BULL", "BUY", "COMPRA", "UP")
    down = ("BAJIS", "SHORT", "BEAR", "SELL", "VENTA", "DOWN")
    if any(w in d for w in up):
        _vote("Scanner", scanner.get("direction"), 1, 2.0, "Autoridad direccional del motor")
    elif any(w in d for w in down):
        _vote("Scanner", scanner.get("direction"), -1, 2.0, "Autoridad direccional del motor")
    else:
        _vote("Scanner", scanner.get("direction") or "NEUTRAL", 0, 2.0, "Sin dirección declarada")

    fnet = _f(flow.get("net"))
    if fnet is not None and abs(fnet) > 0:
        _vote("Flujo neto", fnet, 1 if fnet > 0 else -1, 1.5,
              "Prima direccional observada en la sesión")

    # Delta neta sí es direccional: mide de qué lado está la exposición.
    ndex = _f(pos.get("net_delta"))
    if ndex is not None and abs(ndex) > 0:
        _vote("Delta neta", ndex, 1 if ndex > 0 else -1, 1.0,
              "Exposición delta del lado cliente")

    if curve is not None:
        _vote("Curva 10Y−2Y", curve, -1 if curve < 0 else 1, 0.75,
              "Curva invertida: bonos descuentan deterioro" if curve < 0
              else "Pendiente positiva: bonos sin señal de estrés")
    if credit_z is not None:
        _vote("Crédito HY", credit_z, -1 if credit_z > 1.0 else 1 if credit_z < 0 else 0, 0.75,
              "Diferencial de crédito tensionándose" if credit_z > 1.0
              else "Crédito estable o relajándose")

    # El GEX neto NO es direccional y no vota. Gamma positiva no significa que el
    # mercado suba: significa que la cobertura del dealer va contra el movimiento,
    # sea cual sea su signo. Entra como régimen, que es lo que de verdad mide, y
    # modula la amplitud del rango, no su dirección.
    ngex = _f(pos.get("net_gex"))
    regime = None
    if ngex is not None:
        regime = "AMORTIGUA" if ngex > 0 else "AMPLIFICA" if ngex < 0 else "NEUTRO"
        _vote("GEX neto", ngex, 0, 0.0,
              "Gamma positiva: la cobertura frena el movimiento" if ngex > 0
              else "Gamma negativa: la cobertura acelera el movimiento" if ngex < 0
              else "Gamma neta plana", kind="REGIMEN")

    directional = [v for v in votes if v["kind"] == "DIRECCIONAL"]
    signed = [v for v in directional if v["sign"] != 0]
    mass = sum(v["weight"] for v in signed)
    net = sum(v["sign"] * v["weight"] for v in signed)
    agreement = 0.0 if mass <= 0 else net / mass

    # Scanner conserva la autoridad direccional del motor. Si ha declinado declarar
    # dirección, el resumen de la sesión no la inventa por él: sólo se pronuncia si
    # al menos dos factores independientes apuntan al mismo lado sin excepción. Un
    # único factor suelto no es un sesgo, es un dato.
    scanner_sign = votes[0]["sign"] if votes else 0
    unanimous = len(signed) >= 2 and len({v["sign"] for v in signed}) == 1
    if scanner_sign == 0 and not unanimous:
        bias, priority = "MIXTO", "SIN PRIORIDAD CLARA"
    elif agreement >= 0.34:
        bias, priority = "POSITIVO", "COMPRAS"
    elif agreement <= -0.34:
        bias, priority = "NEGATIVO", "VENTAS"
    else:
        bias, priority = "MIXTO", "SIN PRIORIDAD CLARA"

    ev = stress.get("next_high_event") or {}
    mins = _f(stress.get("minutes_to_next_high"))
    return {
        "event_title": ev.get("title"),
        "event_when": ev.get("time_ny"),
        "event_source": ev.get("source"),
        "event_impact": ev.get("impact"),
        "minutes_to_event": None if mins is None else round(mins, 1),
        "in_session": bool(mins is not None and 0 <= mins <= minutes),
        "session_minutes_left": round(minutes, 1),
        "atm_iv": iv,
        "bias": bias,
        "bias_score": round(net, 3),
        "bias_agreement": round(abs(agreement), 3),
        "bias_factors": len(signed),
        "hedging_regime": regime,
        "priority": priority,
        "votes": votes,
        **band,
        "note": ("Rango implícito por la volatilidad ATM que cotiza el mercado de opciones, "
                 "escalado al tiempo restante de sesión. El sesgo resume los factores "
                 "DIRECCIONALES que el motor mide: Scanner, flujo, delta neta, curva y crédito. "
                 "El GEX neto no vota porque no es direccional: entra como régimen y dice si la "
                 "cobertura frena o acelera el movimiento, sea cual sea su sentido. No hay "
                 "consenso de analistas en las fuentes conectadas, así que no se publica ninguno."),
    }


def _macro(state: Dict[str, Any]) -> Dict[str, Any]:
    macro = state.get("macro") or {}
    if not isinstance(macro, dict):
        macro = {}

    # El impacto sobre la sesión se calcula igualmente: sólo necesita spot, IV ATM y
    # lo que el motor ya midió. Que FRED no responda deja sin bonos ni calendario,
    # pero no es razón para dejar al analista sin el rango de la sesión.
    def _shell(reason: str) -> Dict[str, Any]:
        return {"ready": False, "reason": reason, "rates": [], "events": [],
                "curve": {}, "credit": {}, "asset_components": {}, "asset_weights": {},
                "session": _session_outlook(state, macro, macro.get("stress") or {})}

    if not macro:
        return _shell("MACRO_NO_CARGADO")
    if str(macro.get("status") or "").startswith("UNAVAILABLE"):
        return _shell(str(macro.get("status")))

    series = macro.get("series") or {}
    stress = macro.get("stress") or {}
    asset = macro.get("asset_context") or {}

    rates = []
    for sid in ("DGS2", "DGS10", "DGS30"):
        s = series.get(sid) or {}
        v = _f(s.get("value"))
        if v is None:
            continue
        rates.append({
            "id": sid, "label": s.get("label") or sid, "value": v,
            "change_1": _f(s.get("change_1")), "change_5": _f(s.get("change_5")),
            "z": _f(s.get("z")), "percentile": _f(s.get("percentile")),
            "as_of": s.get("date"), "history": _hist(s),
        })

    curve_s = series.get("T10Y2Y") or {}
    credit_s = series.get("BAMLH0A0HYM2") or {}
    curve = {"value": _f(curve_s.get("value")), "change_5": _f(curve_s.get("change_5")),
             "as_of": curve_s.get("date"), "history": _hist(curve_s)} if curve_s else {}
    credit = {"value": _f(credit_s.get("value")), "z": _f(credit_s.get("z")),
              "as_of": credit_s.get("date"), "history": _hist(credit_s)} if credit_s else {}

    events = []
    for e in (macro.get("events") or [])[:40]:
        if not isinstance(e, dict):
            continue
        events.append({
            "title": e.get("title"), "when": e.get("time_ny"), "when_ec": e.get("time_ec"),
            "importance": e.get("impact"), "source": e.get("source"),
            # FRED/BLS/Fed publican el calendario, no el consenso: estas tres
            # columnas viajan vacías en lugar de rellenarse con una estimación.
            "previous": e.get("previous"), "forecast": e.get("forecast"), "actual": e.get("actual"),
        })

    return {
        "ready": bool(rates or events),
        "reason": None if (rates or events) else "SIN SERIES NI CALENDARIO",
        "stress_score": _f(stress.get("score")),
        "stress_label": stress.get("label"),
        "asset_score": _f(asset.get("score")),
        "regime": asset.get("regime") or (state.get("regime_context") or {}).get("regime"),
        "asset_components": asset.get("components") or {},
        "asset_weights": asset.get("weights") or {},
        "rates": rates, "curve": curve, "credit": credit, "events": events,
        "session": _session_outlook(state, macro, stress),
        "updated": macro.get("updated_ec"),
        "errors": macro.get("errors") or [],
        "source": "FRED · BLS · FEDERAL RESERVE",
    }


# ────────────────────────────────────────────────────────────── monte carlo

_QUANTILE_LABELS = (
    ("p05", "5% · cola baja"), ("p16", "16% · −1σ"), ("p50", "50% · mediana"),
    ("p84", "84% · +1σ"), ("p95", "95% · cola alta"),
)


def _montecarlo(state: Dict[str, Any]) -> Dict[str, Any]:
    lab = state.get("scenario_lab") or {}
    if not lab.get("ready"):
        return {"ready": False, "reason": lab.get("reason") or "SIN SIMULACION",
                "quantiles": [], "paths": [], "iv_scenarios": []}

    spot = _f(lab.get("spot")) or _f(state.get("spot"))
    qs = lab.get("quantiles") or {}
    quantiles = []
    for key, label in _QUANTILE_LABELS:
        price = _f(qs.get(key))
        if price is None:
            continue
        quantiles.append({
            "key": key, "label": label, "price": price,
            "delta": None if spot is None else round(price - spot, 4),
            "delta_pct": None if not spot else round((price - spot) / spot * 100.0, 3),
        })

    iv_scenarios = []
    for name, band in (lab.get("iv_scenarios") or {}).items():
        if not isinstance(band, dict):
            continue
        iv_scenarios.append({
            "name": name, "iv_pct": _f(band.get("iv_pct")),
            "low": _f(band.get("low")), "median": _f(band.get("median")),
            "high": _f(band.get("high")),
        })
    iv_scenarios.sort(key=lambda r: _f(r.get("iv_pct"), 0.0) or 0.0)

    # 30 trayectorias bastan para leer la dispersión; mandar las 25 completas por
    # ciclo con 90 pasos es ruido de red que el panel no llega a dibujar.
    paths = []
    for row in (lab.get("representative_paths") or [])[:30]:
        if isinstance(row, list) and row:
            paths.append([_f(v) for v in row if _f(v) is not None])

    return {
        "ready": True,
        "spot": spot,
        "engine": lab.get("engine"),
        "state": lab.get("state"),
        "authority": lab.get("authority"),
        "is_forecast": bool(lab.get("is_forecast")),
        "path_count": _f(lab.get("path_count")),
        "horizon_minutes": _f(lab.get("horizon_minutes")),
        "atm_iv_pct": _f(lab.get("atm_iv_pct")),
        "effective_iv_pct": _f(lab.get("effective_iv_pct")),
        "quantiles": quantiles, "iv_scenarios": iv_scenarios, "paths": paths,
        "model_risk": lab.get("model_risk"),
    }


# ────────────────────────────────────────────────────── exposure forecast

def _exposure_forecast(trace: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    """Cómo cambia la exposición de gamma si el precio se desplaza.

    Dos mitades que responden a preguntas distintas:
      * `gex` por escenario viene del motor, que reprecia la gamma de toda la
        cadena al spot desplazado con IV y OI congelados.
      * `gex_below` / `gex_above` reparten el perfil por strike alrededor de ese
        precio. Es lo que dice hacia dónde empuja la cobertura: el dealer se
        apoya en la gamma que le queda debajo y vende contra la que tiene encima.
    """
    scen = state.get("exposure_scenarios") or {}
    sens = scen.get("sensitivity") or []
    if not scen.get("ready") or not sens:
        return {"ready": False, "reason": scen.get("reason") or "SIN ESCENARIOS DE EXPOSICION",
                "scenarios": []}

    rows = _d(trace, "profiles", "rows", default=[]) or []
    profile = []
    for r in rows:
        k = _f(r.get("strike"))
        g = _f(r.get("gamma_m"))
        if k is None or g is None:
            continue
        profile.append((k, g * 1e6))

    spot = _f(state.get("spot"))
    structural = _f(scen.get("structural_gex"), 0.0) or 0.0
    out = []
    for s in sens:
        if not isinstance(s, dict):
            continue
        price = _f(s.get("spot"))
        shift = _f(s.get("spot_shift_pct"), 0.0) or 0.0
        gex = _f(s.get("gex"))
        if price is None or gex is None:
            continue
        below = sum(v for k, v in profile if k < price)
        above = sum(v for k, v in profile if k > price)
        out.append({
            "label": "SPOT" if abs(shift) < 1e-9 else f"{shift:+.2f}%",
            "delta_pct": shift, "price": price, "gex_net": gex,
            "gex_below": below if profile else None,
            "gex_above": above if profile else None,
            "change_vs_now": _f(s.get("change_vs_now"), 0.0),
            # Gamma neta positiva = la cobertura del dealer va contra el movimiento.
            "regime": "AMORTIGUA" if gex > 0 else "AMPLIFICA" if gex < 0 else "NEUTRO",
        })
    out.sort(key=lambda r: _f(r.get("delta_pct"), 0.0) or 0.0)

    # El cruce por cero entre dos escenarios contiguos es el precio al que la
    # cobertura cambia de signo: es el flip proyectado, no el flip actual.
    flip = None
    for a, b in zip(out, out[1:]):
        ga, gb = _f(a.get("gex_net")), _f(b.get("gex_net"))
        if ga is None or gb is None or (ga > 0) == (gb > 0):
            continue
        pa, pb = _f(a.get("price")), _f(b.get("price"))
        if pa is None or pb is None or (gb - ga) == 0:
            continue
        flip = round(pa + (pb - pa) * (-ga) / (gb - ga), 4)
        break

    return {
        "ready": True, "spot": spot, "scenarios": out,
        "structural_gex": structural,
        "flow_adjusted_gex": _f(scen.get("flow_adjusted_gex_proxy")),
        "flow_adjustment": _f(scen.get("flow_gamma_adjustment_proxy")),
        "flow_events_used": _f(scen.get("flow_events_used"), 0.0),
        "projected_flip": flip,
        "profile_strikes": len(profile),
        "note": scen.get("note"),
    }


# ────────────────────────────────────────────────────────── interval map

# Griegas que el motor publica como matriz strike × tiempo en heatmap_history.
# Griegas que el motor publica como matriz strike × tiempo en heatmap_history. El
# motor NO tiene matriz de VANNA: el Interval Map del proveedor sí, y por eso la
# griega existe en la interfaz aunque el respaldo propio no pueda cubrirla.
_INTERVAL_FIELDS = {
    "GAMMA": ("gamma_m", 1e6, "GEX"),
    "DELTA": ("delta_m", 1e6, "DEX"),
    "VANNA": (None, 1e6, "VEX"),
    "CHARM": ("charm_m", 1e6, "CHEX"),
}

INTERVAL_GREEKS = ("GAMMA", "DELTA", "VANNA", "CHARM")


def _interval_map(intel: Dict[str, Any], trace: Dict[str, Any] | None = None,
                  greek: str = "GAMMA") -> Dict[str, Any]:
    """MAPA DE INTERVALOS: eje X tiempo · eje Y strike · intensidad exposición.

    v1.43.0 invierte la procedencia. Antes la vía 1 era `heatmap_history` del motor
    y el proveedor entraba «si el motor todavía no tiene historia»; el resultado es
    que el fondo de TRACE era el cálculo propio incluso con el Interval Map del
    proveedor descargado y sano.

    Ahora:

        1. `interval-map` de Quant Data, con una matriz por griega
           (GAMMA · DELTA · VANNA · CHARM) que la interfaz alterna.
        2. `heatmap_history` del motor, como FALLBACK declarado, para el activo o el
           momento en que el proveedor no sirva la herramienta.

    El precio viaja con el mapa sobre el MISMO eje temporal: un mapa de exposición
    sin el precio encima dice dónde estaba la exposición, no por qué zonas pasó el
    mercado.
    """
    from .core import quant_data_hub as HUB

    tr = trace or {}
    key = str(greek or "GAMMA").upper()
    if key not in _INTERVAL_FIELDS:
        key = "GAMMA"
    symbol = str(tr.get("symbol") or "").upper()

    times_hint = [str(t) for t in ((tr.get("heatmap_history") or {}).get("times") or []) if t]
    out = HUB.interval_map(symbol, intel or {}, key,
                           engine_heatmap=(tr.get("heatmap_history") or {}),
                           price=_interval_price(tr, times_hint))

    if not out.get("ready"):
        # Se conserva el vocabulario que ya consumen la interfaz y las pruebas:
        # `reason` explica por qué está vacío en vez de publicar una matriz de ceros.
        out.setdefault("reason", "SIN INTERVAL MAP DEL PROVEEDOR NI HISTORIA DEL MOTOR")
        if not out.get("strikes") and not out.get("times"):
            out.setdefault("payload_keys", _payload_shape(
                (_d(intel or {}, "interval_map_" + key.lower(), "raw", default=None)
                 or _d(intel or {}, "options_heat_map", "raw", default=None))))
    return out


def _interval_price(trace: Dict[str, Any], times: List[str]) -> List[Dict[str, Any]]:
    """Recorrido del precio sobre los mismos intervalos, para leer el mapa contra él.

    Un mapa de exposición sin el precio encima no dice por qué zonas pasó el
    mercado: sólo dónde estaba la exposición.
    """
    candles = trace.get("candles") or []
    out = []
    for c in candles[-600:]:
        t, v = c.get("t"), _f(c.get("c"))
        if t is None or v is None:
            continue
        out.append({"t": str(t), "v": v})
    return out


def _interval_axes(raw: Any, depth: int = 0) -> Dict[str, Any] | None:
    """Payload con `strikes`, `times` y una matriz en paralelo."""
    if depth > 5 or not isinstance(raw, dict):
        if isinstance(raw, list) and depth <= 5:
            for item in raw[:12]:
                hit = _interval_axes(item, depth + 1)
                if hit:
                    return hit
        return None
    ks = next((raw[k] for k in ("strikes", "strikePrices", "y", "rows") if isinstance(raw.get(k), list)), None)
    ts = next((raw[k] for k in ("times", "timestamps", "intervals", "x", "columns") if isinstance(raw.get(k), list)), None)
    mx = next((raw[k] for k in ("matrix", "values", "data", "z", "grid") if isinstance(raw.get(k), list)), None)
    if ks and ts and mx and isinstance(mx[0], list):
        strikes = [_f(k) for k in ks if _f(k) is not None]
        times = [str(t) for t in ts if t is not None]
        matrix = []
        for row in mx[:len(strikes)]:
            vals = [_f(v, 0.0) or 0.0 for v in (row if isinstance(row, list) else [])][:len(times)]
            vals += [0.0] * (len(times) - len(vals))
            matrix.append(vals)
        if strikes and times and matrix:
            return {"strikes": strikes, "times": times, "matrix": matrix,
                    "cells": len(strikes) * len(times)}
    for child in raw.values():
        if isinstance(child, (dict, list)):
            hit = _interval_axes(child, depth + 1)
            if hit:
                return hit
    return None


def _interval_collect(node: Any, cells: Dict[float, Dict[str, float]],
                      strike: Any = None, depth: int = 0) -> None:
    """Celdas sueltas, con los nombres que el proveedor usa en cada variante."""
    if depth > 7 or node is None:
        return
    if isinstance(node, list):
        for item in node[:6000]:
            _interval_collect(item, cells, strike, depth + 1)
        return
    if not isinstance(node, dict):
        return
    k = _f(_first(node, "strike", "strikePrice", "strike_price", "y", "price"))
    if k is None:
        k = _f(strike)
    t = _first(node, "time", "timestamp", "t", "interval", "x", "bucket", "date")
    v = _f(_first(node, "gamma", "value", "gex", "netGamma", "exposure", "z", "v", "delta", "charm"))
    if k is not None and t is not None and v is not None:
        cells.setdefault(k, {})[str(t)] = v
        return
    for child in node.values():
        if isinstance(child, (list, dict)):
            _interval_collect(child, cells, k if k is not None else strike, depth + 1)


def _first(node: Dict[str, Any], *names: str) -> Any:
    for n in names:
        if node.get(n) is not None:
            return node[n]
    return None


def _payload_shape(raw: Any, depth: int = 0) -> Any:
    """Esqueleto del payload: claves y tipos, sin volcar los datos."""
    if depth > 3:
        return "…"
    if isinstance(raw, dict):
        return {k: _payload_shape(v, depth + 1) for k, v in list(raw.items())[:12]}
    if isinstance(raw, list):
        return [f"lista[{len(raw)}]", _payload_shape(raw[0], depth + 1)] if raw else "lista[0]"
    return type(raw).__name__


# ──────────────────────────────────────────────────────────── backtesting

def _backtest(state: Dict[str, Any]) -> Dict[str, Any]:
    """Estado del backtesting y del modelo aprendido, con su disciplina a la vista.

    El motor ya entrena un modelo de probabilidad con validación walk-forward fuera
    de muestra: bloques de entrenamiento, validación y test separados por una PURGA
    que impide que una sesión filtre información a la siguiente. Se juzga con Brier
    y log-loss contra la tasa base, y con un intervalo de confianza bootstrap.

    Lo que se publica aquí es ese proceso, no un número suelto. Un modelo que no ha
    superado su test fuera de muestra viaja como SHADOW y **no decide nada**: la
    autoridad direccional sigue siendo del Scanner. Un backtest que no enseña su
    validación es una promesa, no una medición.
    """
    calib = state.get("calibration") or {}
    prob = calib.get("probability_model") or {}
    wf = calib.get("walk_forward") or prob if prob.get("train_sessions") is not None else calib.get("walk_forward") or {}

    def _n(key, src=None):
        return _f((src if src is not None else wf).get(key))

    brier_m, brier_b = _n("brier_model"), _n("brier_base_rate")
    ll_m, ll_b = _n("log_loss_model"), _n("log_loss_base_rate")
    skill = _n("brier_skill_score")
    lo, hi = _n("brier_skill_ci_low"), _n("brier_skill_ci_high")

    stage = str(wf.get("stage") or prob.get("stage") or calib.get("status") or "COLLECTING")
    promoted = bool(wf.get("ready") or wf.get("eligible_for_activation"))

    gates = []

    def _gate(name: str, passed: Any, detail: str) -> None:
        gates.append({"gate": name, "passed": None if passed is None else bool(passed), "detail": detail})

    sessions = _f(calib.get("sessions"), _f(calib.get("sample_size")))
    min_sessions = _n("minimum_promotion_sessions")
    _gate("Sesiones acumuladas", None if (sessions is None or min_sessions is None) else sessions >= min_sessions,
          f"{_d0(sessions)} de {_d0(min_sessions)} necesarias")
    oos, min_oos = _f(wf.get("test_sessions")), _n("minimum_final_oos_sessions")
    _gate("Bloque fuera de muestra", wf.get("final_oos_session_gate_pass"),
          f"{_d0(oos)} sesiones de test, mínimo {_d0(min_oos)}")
    _gate("Supera la tasa base", wf.get("beats_base_rate"),
          "Brier del modelo por debajo del de la tasa base"
          if brier_m is not None and brier_b is not None else "sin evaluación todavía")
    _gate("Intervalo bootstrap", wf.get("statistical_promotion_pass"),
          f"skill {_d3(skill)} · IC [{_d3(lo)}, {_d3(hi)}]")
    _gate("Purga entre bloques", None if wf.get("purged_sessions") is None else _f(wf.get("purged_sessions"), 0) > 0,
          f"{_d0(_f(wf.get('purged_sessions')))} sesiones descartadas entre entrenamiento y test")

    return {
        "ready": bool(calib),
        "reason": None if calib else "SIN HISTORIAL DEL SCANNER TODAVÍA",
        "stage": stage,
        "promoted": promoted,
        "authority": "SHADOW" if not promoted else "CALIBRATED_SHADOW",
        "sessions": sessions,
        "sample_size": _f(calib.get("sample_size")),
        "split": {
            "train_sessions": _f(wf.get("train_sessions")),
            "validation_sessions": _f(wf.get("validation_sessions")),
            "purged_sessions": _f(wf.get("purged_sessions")),
            "test_sessions": _f(wf.get("test_sessions")),
        },
        "scores": {
            "brier_model": brier_m, "brier_base_rate": brier_b,
            "log_loss_model": ll_m, "log_loss_base_rate": ll_b,
            "brier_skill": skill, "ci_low": lo, "ci_high": hi,
            # Mejora relativa sobre la tasa base: negativa significa que el modelo
            # es PEOR que no modelar nada, y eso hay que poder verlo.
            "improvement_pct": (None if not (brier_m is not None and brier_b and brier_b > 0)
                                else round((brier_b - brier_m) / brier_b * 100.0, 2)),
        },
        "gates": gates,
        "gates_passed": sum(1 for g in gates if g["passed"]),
        "gates_total": len(gates),
        "calibrator": wf.get("selected_calibrator"),
        "candidates": wf.get("selection_candidates") or [],
        "reliability": wf.get("reliability") or [],
        "note": (wf.get("note") or
                 "El modelo se entrena y se juzga en bloques separados por una purga. "
                 "Mientras no supere su test fuera de muestra viaja como SHADOW y no "
                 "decide nada: la autoridad direccional sigue siendo del Scanner."),
    }


def _d0(v: Any) -> str:
    x = _f(v)
    return "—" if x is None else f"{x:.0f}"


def _d3(v: Any) -> str:
    x = _f(v)
    return "—" if x is None else f"{x:.3f}"


# Motivos que no explican nada por sí solos: ahí sí ayuda el contexto de sesión.
_GENERIC_REASONS = frozenset({
    "SIN_DATO", "SIN_DATOS", "FABRIC_VACIA_Y_BOOTSTRAP_SIN_BARRAS",
    "SIN_PRINTS_OBSERVADOS", "SIN_PRINTS_DE_EQUITY_EN_SESION", "NO_PRINTS",
    "SCANNER_NO_PUBLICO_NIVELES", "SIN_HISTORIA_ESTRUCTURAL", "CADENA_NO_HIDRATADA",
})

# Paneles que se alimentan de la cinta de EQUITY: en premarket sí deben tener datos.
_EQUITY_PANELS = ("velas", "equity", "dark pool")


def _is_equity_panel(panel: str) -> bool:
    low = str(panel or "").lower()
    return any(tok in low for tok in _EQUITY_PANELS)


def _candles_reason(trace: Dict[str, Any]) -> str:
    """Por qué no hay velas, distinguiendo el bootstrap del calendario.

    `sip:NO_BARS; iex:NO_BARS` describía el síntoma, no la causa. Cuando la sesión
    consultada aún no había empezado, ese mensaje llevaba a buscar una avería de
    proveedor que no existía.
    """
    phase = str(trace.get("session_phase") or "")
    reason = str(trace.get("session_reason") or "")
    if reason == "SESSION_NOT_STARTED":
        return ("La sesión de hoy aún no ha comenzado; el gráfico arranca con la última "
                "sesión completada. Si sigue vacío, el fallo es del bootstrap histórico.")
    if phase in ("WEEKEND", "HOLIDAY", "MARKET_CLOSED"):
        return "Mercado cerrado: se publican las velas de la última sesión."
    return "FABRIC_VACIA_Y_BOOTSTRAP_SIN_BARRAS"


def _arquitectura(state: Dict[str, Any], trace: Dict[str, Any]) -> Dict[str, Any]:
    """Contratos de v1.42 publicados junto a los datos, no sólo en la documentación.

    Un contrato que vive en un .md se desincroniza del código en dos versiones. Aquí
    el propio bundle publica qué modelo valoró, con qué unidades, quién manda en cada
    métrica y qué dijo el auditor, para que la interfaz —y quien audite— lo lea del
    sistema en marcha en vez de creerse un documento.
    """
    from .core import units_registry, metric_authority, greeks_service
    from .core.model_risk_auditor import audit as _audit
    from .core.contract_spec import from_symbol

    symbol = state.get("active_symbol") or state.get("symbol") or "DIA"
    spec = from_symbol(symbol)
    dispatch = greeks_service.describe_dispatch(symbol)

    flow = state.get("flow_summary") or {}
    scanner = state.get("scanner") or {}
    audit = _audit(
        chain=None, symbol=symbol,
        flow={"non_causal_quotes": int(flow.get("non_causal_quotes") or 0),
              "classified_pct": _f(flow.get("classified_pct"))},
        scanner={"input_age_seconds": _f(state.get("data_age_seconds"))},
        ev=state.get("ev_gate") or state.get("ev"),
        calibration=state.get("calibration"),
        dealer=state.get("dealer_state"),
        replay={"active": bool((state.get("replay") or {}).get("active")),
                "cutoff": (state.get("replay") or {}).get("asof"),
                "latest_event": (state.get("replay") or {}).get("latest_event")},
    )

    return {
        "scope": "MULTI_ASSET",
        "contrato": {
            "contrato_del_instrumento": spec.describe(),
            "despacho_de_valoracion": dispatch,
        },
        "unidades": units_registry.registry_snapshot(),
        "autoridad_por_metrica": metric_authority.authority_map(),
        "uso_del_dispatcher": greeks_service.usage_stats(),
        "auditoria": {
            "headline": audit["headline"],
            "checks_run": audit["checks_run"],
            "failures": audit["failures"][:12],
            "failures_by_area": audit["failures_by_area"],
        },
        "doctrina": (
            "Un contrato, un precio, una IV, una Gamma, un multiplicador, una unidad y "
            "una marca de tiempo significan exactamente lo mismo en todo el programa."
        ),
    }


def _inteligencia(state: Dict[str, Any], trace: Dict[str, Any],
                  intel: Dict[str, Any]) -> Dict[str, Any]:
    """INTELIGENCIA PROPIA de ITM QUANT sobre los datos del proveedor.

    El motor ya no reconstruye GEX, DEX, OI ni Net Drift: los lee del Data Hub —el
    MISMO bloque que alimenta la interfaz— y produce encima lo que ningún
    proveedor entrega: confluencia, divergencia, persistencia, migración de gamma,
    strikes dominantes, BREAK/CONTAINMENT/TRANSITION, régimen y Structural Score.

    Todo lo que sale de aquí es DERIVED y lleva prefijo `ITMQ_`. Nunca se presenta
    como dato directo del proveedor.
    """
    from .core import quant_data_hub as HUB
    from .core import itmq_intelligence as IQ

    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    spot = _f(state.get("spot")) or _f(_d(trace, "profiles", "spot"))
    candles = trace.get("candles") or []
    price_series = [_f(c.get("c")) for c in candles[-120:] if isinstance(c, dict)]
    price_series = [p for p in price_series if p is not None]

    hub = HUB.hub_snapshot(symbol, intel,
                           engine_heatmap=(trace.get("heatmap_history") or {}))
    qflow = _qflow(state, intel)
    drift = _net_drift(state, intel)
    dark = hub.get("dark_pool") or {}

    drift_series = [_f(r.get("net_premium")) for r in (drift.get("series") or [])]
    drift_series = [v for v in drift_series if v is not None]
    net_flow_rows = _qd_rows(intel, "net_flow")
    net_flow_total = sum(_f(r.get("value"), 0.0) or 0.0 for r in net_flow_rows) if net_flow_rows else None

    # Sesgo del dark pool: proporción oscura por encima o por debajo del precio.
    dark_bias = None
    if dark.get("ready") and spot:
        above = sum(_f(l.get("notional"), 0.0) or 0.0
                    for l in (dark.get("levels") or []) if (_f(l.get("price")) or 0) > spot)
        below = sum(_f(l.get("notional"), 0.0) or 0.0
                    for l in (dark.get("levels") or []) if (_f(l.get("price")) or 0) < spot)
        if above or below:
            dark_bias = below - above

    out = IQ.analyze(symbol, hub, spot=spot,
                     levels=trace.get("levels") or [],
                     price_series=price_series,
                     net_drift=_f(drift.get("net_premium")),
                     net_flow=net_flow_total,
                     qflow_net=_f(qflow.get("net_premium")),
                     dark_pool_bias=dark_bias,
                     drift_series=drift_series)
    out["capabilities"] = hub.get("capabilities")
    return out


def _greeks(state: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    """Greeks POR CONTRATO del proveedor.

    Quant Data es la fuente primaria de Delta, Gamma, Theta, Vega, Rho y de los de
    orden superior. ITM QUANT los usa como entrada de sus modelos; no los vuelve a
    calcular cuando el proveedor ya los entrega válidos, porque dos Deltas
    distintos del mismo contrato en la misma pantalla es peor que ninguno.
    """
    from .core import quant_data_hub as HUB
    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    return HUB.contract_greeks(symbol, intel)


def _capacidades(state: Dict[str, Any], intel: Dict[str, Any]) -> Dict[str, Any]:
    """Qué sirve el proveedor PARA ESTE activo.

    El mismo pipeline recorre cualquier símbolo:
        ticker → capability check → Quant Data → normalización → motor → frontend
    Saber de antemano qué herramientas responden es lo que distingue «esta sección
    no aplica a este activo» de «esta sección está rota».
    """
    from .core import quant_data_hub as HUB
    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    return HUB.capabilities(symbol, intel)


def _auditor(state: Dict[str, Any]) -> Dict[str, Any]:
    """Procedencia completa, SÓLO para el Auditor.

    La pantalla principal no muestra nombres de proveedor, endpoints, errores ni
    diagnósticos: muestra análisis. Todo eso vive aquí.
    """
    from .core.data_lineage import LINEAGE
    symbol = str(state.get("active_symbol") or state.get("symbol") or "").upper()
    report = LINEAGE.audit(symbol or None)
    report["visibility"] = "AUDITOR_ONLY_NEVER_MAIN_SCREEN"
    return report


def build_terminal_bundle(*, state: Dict[str, Any], trace: Dict[str, Any],
                          intelligence: Dict[str, Any] | None = None,
                          parity: Dict[str, Any] | None = None,
                          coverage: Dict[str, Any] | None = None,
                          interval_greek: str = "GAMMA") -> Dict[str, Any]:
    intel = intelligence or {}
    asset = state.get("asset") or {}
    scanner = state.get("scanner") or {}

    # La sección se calcula ANTES del sobre para que su diagnóstico por carril
    # pueda viajar también al Auditor. La pantalla principal seguirá leyendo
    # `dark_pool.display_reason`; la causa exacta vive en `auditor`.
    dark_pool_section = _dark_pool(state, trace, intel)
    auditor = _auditor(state)
    auditor["dark_pool"] = {
        "lanes": dark_pool_section.get("lanes") or [],
        "diagnosis": dark_pool_section.get("diagnosis") or {},
        # v1.48.0 · Con qué campo se leyó cada magnitud del flujo oscuro y qué
        # campos publicó el proveedor. Sin esto, un carril que responde con
        # seiscientos intervalos y un volumen de cero no se puede corregir: no
        # hay forma de saber si el mercado no tuvo actividad o si el campo se
        # llama de otra manera.
        "flow_fields": dark_pool_section.get("flow_fields") or {},
        "note": ("Tres carriles independientes. Que uno rechace el cuerpo no dice "
                 "nada sobre los otros dos, y presentarlos juntos hacía parecer "
                 "rota la sección entera teniendo dos de tres sanos."),
    }
    # v1.55.0 · AUTORIDAD ÚNICA DE MUROS, comprobada y no sólo declarada.
    resumen_block = _resumen(state, trace, intel)
    auditor["walls"] = wall_consistency(trace, resumen_block)
    # IDENTIDAD DE LÍNEAS. Mientras `unidentified` no sea cero, hay una línea
    # anónima sobre el gráfico de operativa y eso es un defecto abierto.
    auditor["level_identity"] = ((trace or {}).get("level_identity_audit")
                                 or {"ok": None, "detail": "el trace no publicó la auditoría"})

    return {
        "ready": bool(state.get("ready")),
        "symbol": state.get("active_symbol") or state.get("symbol"),
        "symbol_epoch": state.get("symbol_epoch"),
        "asset_name": asset.get("name"),
        "asset_kind": asset.get("kind"),
        "spot": _f(state.get("spot")),
        "mode": state.get("mode"),
        "data_age_seconds": _f(state.get("data_age_seconds")),
        "data_quality": _f(state.get("data_quality")),
        "last_refresh": state.get("last_refresh_ec"),
        "scanner_ready": bool(scanner.get("ready")),
        "publication_blocked": bool(trace.get("blocked")),
        "publication_blocked_motive": trace.get("publication_blocked_motive"),
        "candle_source": trace.get("candle_source"),
        "resumen": resumen_block,
        "exposicion": _exposicion(trace, state, intel),
        "open_interest": _open_interest(trace, state, intel),
        "volatilidad": _volatilidad(state, intel),
        "estadisticas": _estadisticas(trace, state, intel),
        "dark_pool": dark_pool_section,
        "macro": _macro(state),
        "qflow": _qflow(state, intel),
        "net_drift": _net_drift(state, intel),
        # La sección de FLUJO DE ÓRDENES que ya existía, con Net Flow, Net Drift,
        # Order Flow consolidado y sin consolidar, QFLOW y su concentración dentro.
        # No es una sección nueva: es la misma, completa.
        "flujo_ordenes": _flujo_ordenes(state, intel, trace),
        "greeks": _greeks(state, intel),
        "inteligencia": _inteligencia(state, trace, intel),
        "capacidades": _capacidades(state, intel),
        "montecarlo": _montecarlo(state),
        "exposure_forecast": _exposure_forecast(trace, state),
        "interval_map": _interval_map(intel, trace, interval_greek),
        # Autoridad única de muros y lectura de migración, tal y como las resolvió
        # el Wall Engine sobre el trace. Ninguna sección las recalcula.
        "walls": (trace or {}).get("walls") or {},
        "gamma_migration": (trace or {}).get("gamma_migration") or {},
        "backtest": _backtest(state),
        "arquitectura": _arquitectura(state, trace),
        "fuentes": {
            "parity": parity or {},
            "quantdata_coverage": coverage or {},
            "provider_consensus": state.get("provider_consensus") or {},
        },
        # PROCEDENCIA · va en el bundle para el AUDITOR, no para la pantalla
        # principal. La terminal muestra análisis; los nombres de proveedor,
        # endpoint, estado y fallback se leen aquí y sólo aquí.
        "auditor": auditor,
        "contract": "ITMQ_TERMINAL_BUNDLE_V2",
    }

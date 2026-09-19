"""Paridad de proveedores en opciones · ITM QUANT v1.41.0

Hasta v1.40.8 Quant Data entraba en la arquitectura con el rol explícito de
"corroboración": aunque el arbitraje por canal ya era neutral, la capa semántica
lo marcaba como evidencia secundaria y sus observaciones no podían ganar un canal
de opciones aunque llegaran más frescas y completas que las del resto.

Este módulo elimina esa asimetría. Alpaca, tastytrade y Quant Data entran a los
canales de opciones como pares: los tres pueden ser la fuente seleccionada y los
tres contribuyen al valor fusionado. Lo único que decide es la calidad observada
de cada paquete (frescura, latencia, completitud, integridad de secuencia y
concordancia con el resto), nunca el nombre del proveedor.

Dos reglas que se mantienen intactas porque no son jerarquía de proveedor:
  * No se promedian instrumentos ni semánticas distintas. Dos fuentes se fusionan
    sólo si describen el mismo contrato con la misma convención.
  * La autoridad direccional sigue siendo del Scanner. La paridad es sobre datos
    de entrada, no sobre la decisión del motor.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List
import math
import os
import time

from . import session_resolver
from .obs import note as _obs_note

# Proveedores que participan como pares de pleno derecho en los canales de opciones.
# La lista es de participación, no de precedencia: el orden aquí no puntúa.
#
# El roster es configurable con ITM_OPTIONS_PEERS. Por defecto son Alpaca y Quant
# Data: dos fuentes que cubren precio, cadena, flujo y analítica sin depender de un
# WebSocket que se reconecta. tastytrade sigue en el código y vuelve a la lista sin
# tocar nada añadiéndolo a esa variable, pero no se cuenta como ausente si no está:
# un proveedor que no forma parte del roster no es una carencia, es una decisión.
DEFAULT_OPTIONS_PEERS: tuple[str, ...] = ("ALPACA", "QUANTDATA")
KNOWN_PROVIDERS: tuple[str, ...] = ("ALPACA", "TASTYTRADE", "QUANTDATA")


def _configured_peers() -> tuple[str, ...]:
    raw = str(os.getenv("ITM_OPTIONS_PEERS", "") or "").strip()
    if not raw:
        return DEFAULT_OPTIONS_PEERS
    names = [_norm_name(x) for x in raw.replace(";", ",").split(",")]
    kept = tuple(n for n in names if n in KNOWN_PROVIDERS)
    # Una variable mal escrita no puede dejar la terminal sin ningún proveedor.
    return kept or DEFAULT_OPTIONS_PEERS


OPTIONS_PEERS: tuple[str, ...] = _configured_peers()


def peer_enabled(name: Any) -> bool:
    """Si un proveedor forma parte del roster activo de esta instalación."""
    return _norm_name(name) in _configured_peers()

# Canales donde se aplica la paridad. Cada uno arbitra por separado porque un
# proveedor puede ser excelente en cadena y pobre en flujo, y al revés.
PARITY_CHANNELS: tuple[str, ...] = (
    "OPTION_CHAIN", "OPTION_QUOTE", "OPTION_FLOW", "OPEN_INTEREST",
    "IMPLIED_VOLATILITY", "EXPOSURE", "DARK_POOL", "EQUITY_PRINT",
)

# Peso mínimo para que un proveedor entre en la fusión. Por debajo de esto la
# observación es demasiado pobre para aportar y sólo añadiría ruido.
MIN_USABLE_QUALITY = 35.0


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _norm_name(value: Any) -> str:
    raw = str(value or "").upper().strip()
    if not raw:
        return ""
    # Los transportes concretos se pliegan a su proveedor: DXLink es tastytrade,
    # SIP/OPRA es Alpaca. Si no, el mismo proveedor competiría consigo mismo.
    if "DXLINK" in raw or "TASTY" in raw:
        return "TASTYTRADE"
    if "ALPACA" in raw or raw in {"SIP", "OPRA"}:
        return "ALPACA"
    if "QUANT" in raw and "DATA" in raw:
        return "QUANTDATA"
    if raw.startswith("QD"):
        return "QUANTDATA"
    return raw


# Página integrada del proveedor -> canal de arbitraje al que pertenece.
_PAGE_CHANNEL: Dict[str, str] = {
    "Exposure": "EXPOSURE",
    "Flow Analysis": "OPTION_FLOW",
    "Dashboard": "OPTION_FLOW",
    "Open Interest": "OPEN_INTEREST",
    "Volatility Analysis": "IMPLIED_VOLATILITY",
    "Statistics": "OPTION_FLOW",
    "Dark Pool / Equities": "DARK_POOL",
}


# Qué métricas representa cada canal. El canal es la vista de la interfaz; la
# métrica es donde vive la política. Con este mapa, la autoridad del canal se
# DERIVA en vez de declararse por segunda vez.
_CHANNEL_METRICS: Dict[str, tuple[str, ...]] = {
    "EXPOSURE": ("gex", "dex", "vex", "chex"),
    "OPEN_INTEREST": ("open_interest",),
    "IMPLIED_VOLATILITY": ("iv_rank", "volatility_skew", "term_structure"),
    "OPTION_FLOW": ("net_flow", "order_flow", "net_drift"),
    "DARK_POOL": ("dark_flow", "dark_pool_levels"),
    "EQUITY_PRINT": ("dark_pool_prints",),
}


def channel_authority(channel: str) -> str:
    """Quién MANDA en este canal, leído de la política de métricas.

    v1.45.0 · Antes esto era un segundo mapa escrito a mano:

        "EXPOSURE": "ITM", "OPEN_INTEREST": "ALPACA", "DARK_POOL": "ALPACA", …

    v1.43.0 pasó esas métricas a QUANTDATA en `metric_authority`, pero este mapa no
    se movió. Resultado: la interfaz seguía anunciando a Quant Data como
    «CONTRASTE ACTIVO» y al núcleo nativo como autoridad, cuando internamente ya
    mandaba Quant Data. La etiqueta era falsa, no el comportamiento.

    Tener la autoridad escrita en dos sitios garantiza que un día discrepen. Ahora
    hay una sola fuente y ésta la consulta.
    """
    from .metric_authority import policy as _metric_policy
    metrics = _CHANNEL_METRICS.get(str(channel or "").upper(), ())
    if not metrics:
        return "ITM"
    authorities: list[str] = []
    for m in metrics:
        a = str(_metric_policy(m).authority).upper()
        if a not in authorities:
            authorities.append(a)
    return " / ".join(authorities) if authorities else "ITM"


# Quién sostiene el canal cuando la autoridad no está disponible. Es un RESPALDO
# declarado, no una autoridad: lo que publique va etiquetado como tal.
_NATIVE_FALLBACK: Dict[str, str] = {
    "EXPOSURE": "ITM",
    "OPEN_INTEREST": "ALPACA / ITM",
    "IMPLIED_VOLATILITY": "ITM",
    "OPTION_FLOW": "ALPACA / ITM",
    "DARK_POOL": "ALPACA",
    "EQUITY_PRINT": "ALPACA",
}


def _qd_is_authority(channel: str) -> bool:
    return "QUANTDATA" in channel_authority(channel).upper()


def parity_policy() -> Dict[str, Any]:
    """Contrato público de la política. La UI lo muestra tal cual."""
    return {
        "version": "1.41.0",
        "policy": "EQUAL_PEER_OPTIONS_PARITY",
        "peers": list(OPTIONS_PEERS),
        "channels": list(PARITY_CHANNELS),
        "fixed_rank": False,
        "confirmation_only_providers": [],
        "selection_criteria": ["freshness", "latency", "completeness", "sequence_integrity", "cross_source_agreement"],
        "min_usable_quality": MIN_USABLE_QUALITY,
        "note": (
            "Todos los proveedores configurados compiten en igualdad en cada canal de opciones. "
            "Ninguno está limitado a confirmar a otro. El nombre del proveedor no otorga peso; "
            "sólo la calidad observada del paquete decide."
        ),
        "invariants": [
            "No se fusionan semánticas ni instrumentos distintos.",
            "La autoridad direccional permanece en el Scanner.",
        ],
    }


def weight_for(observation: Dict[str, Any]) -> float:
    """Peso de una observación: calidad normalizada, sin bonus por proveedor.

    Se toma `quality_score` si el arbitraje por canal ya lo calculó; si falta,
    se reconstruye un proxy conservador desde frescura y completitud para no
    premiar a un proveedor que simplemente no reporta telemetría.
    """
    if not isinstance(observation, dict):
        return 0.0
    q = observation.get("quality_score")
    if q is not None:
        return max(0.0, min(100.0, _f(q)))

    age_ms = _f(observation.get("age_ms"), float("inf"))
    horizon = max(1.0, _f(observation.get("freshness_horizon_ms"), 15_000.0))
    fresh = max(0.0, 1.0 - min(age_ms / horizon, 1.0)) if math.isfinite(age_ms) else 0.0

    fields = observation.get("fields_present")
    expected = observation.get("fields_expected")
    if fields is not None and expected:
        completeness = max(0.0, min(1.0, _f(fields) / max(_f(expected), 1.0)))
    else:
        # Sin telemetría de completitud no se asume perfección.
        completeness = 0.5

    live = 1.0 if str(observation.get("status", "")).upper() in {"LIVE", "ACTIVE", "CONNECTED", "OK", "GOOD"} else 0.0
    return max(0.0, min(100.0, 100.0 * (0.55 * fresh + 0.30 * completeness + 0.15 * live)))


def rank_peers(observations: Iterable[Dict[str, Any]], *, channel: str = "OPTION_CHAIN") -> Dict[str, Any]:
    """Ordena las observaciones de un canal por calidad, sin ranking de proveedor.

    Devuelve la lista completa (también las descartadas, con su motivo) para que
    la UI pueda mostrar por qué un proveedor no participó en ese ciclo.
    """
    rows: List[Dict[str, Any]] = []
    for obs in observations or []:
        if not isinstance(obs, dict):
            continue
        name = _norm_name(obs.get("provider") or obs.get("source") or obs.get("name"))
        if not name:
            continue
        w = weight_for(obs)
        rows.append({
            **obs,
            "provider": name,
            "channel": channel,
            "quality_score": round(w, 2),
            "usable": w >= MIN_USABLE_QUALITY,
            "excluded_reason": None if w >= MIN_USABLE_QUALITY else "QUALITY_BELOW_MIN_USABLE",
            "role": "PEER",              # nunca "CONFIRMATION"
            "first_class": True,
        })

    # Desempate estable por nombre: sin esto, dos proveedores con idéntica calidad
    # alternarían entre ciclos y el panel parpadearía sin que cambie nada real.
    rows.sort(key=lambda r: (-_f(r.get("quality_score")), str(r.get("provider"))))
    usable = [r for r in rows if r.get("usable")]
    return {
        "channel": channel,
        "ready": bool(usable),
        "selected": usable[0]["provider"] if usable else None,
        "peers": rows,
        "usable_count": len(usable),
        "configured_count": len(rows),
        "policy": "EQUAL_PEER_OPTIONS_PARITY",
    }


def fuse_values(observations: Iterable[Dict[str, Any]], field: str, *,
                channel: str = "OPTION_CHAIN", max_divergence: float | None = None,
                metric: str | None = None) -> Dict[str, Any]:
    """Combina observaciones del MISMO fenómeno con pesos de calidad.

    v1.42 · La combinación dejó de ser el comportamiento por defecto. Antes, dos
    cifras «suficientemente cercanas» se promediaban aunque fueran construcciones
    distintas con el mismo nombre: el GEX de Quant Data y el de ITM no son dos
    medidas ruidosas de una cantidad común, y su media no describe el libro de
    nadie. Ahora la métrica debe tener contrato de fusión en `metric_authority`;
    si no lo tiene, se devuelve FUSION_FORBIDDEN con el motivo en vez de un número.

    `max_divergence` sigue siendo la dispersión máxima tolerada DENTRO de una
    métrica sí fusionable (dos precios del mismo instrumento, por ejemplo).
    """
    from .metric_authority import policy as _metric_policy

    pol = _metric_policy(metric or field)
    if pol.fusion_contract is None:
        return {
            "ready": False, "status": "FUSION_FORBIDDEN", "field": field, "channel": channel,
            "metric": pol.metric, "authority": pol.authority, "contributors": [],
            "policy": "METRIC_AUTHORITY_V1_42", "method": "NO_FUSION",
            "reason": (f"«{pol.metric}» no admite fusión entre proveedores: su autoridad es "
                       f"{pol.authority}. Promediarla con otra fuente daría una cifra que no "
                       "corresponde a ningún mercado observable."),
        }

    ranked = rank_peers(observations, channel=channel)
    usable = [r for r in ranked["peers"] if r.get("usable") and r.get(field) is not None]
    if not usable:
        return {"ready": False, "field": field, "channel": channel, "reason": "NO_USABLE_OBSERVATION",
                "contributors": [], "policy": "EQUAL_PEER_OPTIONS_PARITY"}

    tol = _f(os.getenv("ITM_PARITY_MAX_DIVERGENCE", "0.05"), 0.05) if max_divergence is None else float(max_divergence)
    values = [_f(r.get(field)) for r in usable]
    weights = [max(_f(r.get("quality_score")), 1e-6) for r in usable]
    total_w = sum(weights) or 1.0
    fused = sum(v * w for v, w in zip(values, weights)) / total_w

    scale = max(abs(fused), 1e-9)
    spread = (max(values) - min(values)) / scale if len(values) > 1 else 0.0
    diverged = len(values) > 1 and spread > tol

    best = usable[0]
    contributors = [
        {"provider": r["provider"], "value": _f(r.get(field)),
         "quality": _f(r.get("quality_score")), "weight_pct": round(100.0 * w / total_w, 1)}
        for r, w in zip(usable, weights)
    ]

    return {
        "ready": True,
        "field": field,
        "channel": channel,
        "value": _f(best.get(field)) if diverged else fused,
        "method": "HIGHEST_QUALITY_PEER" if diverged else "QUALITY_WEIGHTED_SAME_PHENOMENON_MEAN",
        "metric": pol.metric,
        "fusion_contract": pol.fusion_contract,
        "diverged": diverged,
        "spread_pct": round(100.0 * spread, 3),
        "tolerance_pct": round(100.0 * tol, 3),
        "contributors": contributors,
        "selected": best["provider"],
        "policy": "METRIC_AUTHORITY_V1_42",
        "note": None if not diverged else (
            "Las fuentes divergen por encima de la tolerancia; se publica la de mayor calidad "
            "en lugar de una media que no describiría ningún mercado real."
        ),
    }


def parity_report(*, alpaca_configured: bool, tastytrade_status: Dict[str, Any] | None,
                  quantdata_status: Dict[str, Any] | None,
                  consensus: Dict[str, Any] | None = None,
                  options_coverage: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Estado de paridad listo para la interfaz.

    Reúne lo que cada proveedor aporta ahora mismo y confirma que ninguno está
    restringido a un rol de confirmación.
    """
    tt = dict(tastytrade_status or {})
    qd = dict(quantdata_status or {})
    now = time.time()

    def _age(status: Dict[str, Any]) -> float | None:
        last = status.get("last_success")
        if not last:
            return None
        try:
            if isinstance(last, (int, float)):
                return max(0.0, now - float(last))
            import datetime as _dt
            ts = _dt.datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            return max(0.0, (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds())
        except Exception as exc:
            _obs_note("provider_parity:age", exc)
            return None

    # tastytrade publica su salud como dxlink/market_data/last_event_age_ms; no
    # existen las claves connected/running que se leían antes, así que el proveedor
    # se quedaba en CONFIGURED aunque estuviera transmitiendo.
    def _tastytrade_state(st: Dict[str, Any]) -> str:
        if not st.get("configured"):
            return "NOT_CONFIGURED"
        dxlink = str(st.get("dxlink") or "").upper()
        market = str(st.get("market_data") or "").upper()
        age_ms = _f(st.get("last_event_age_ms"), float("inf"))
        streaming = dxlink in {"CONNECTED", "LIVE", "SUBSCRIBED"} or market in {"STREAMING", "LIVE", "ACTIVE"}
        if streaming and age_ms <= 120_000:
            return "LIVE"
        if streaming or st.get("last_event_at"):
            return "DEGRADED"
        return "CONFIGURED"

    def _quantdata_state(st: Dict[str, Any]) -> str:
        if not st.get("configured"):
            return "NOT_CONFIGURED"
        if not st.get("last_success"):
            return "CONFIGURED"
        age = _age(st)
        # Un error puntual con datos frescos sigue siendo un carril vivo; lo que
        # degrada es que el dato deje de renovarse.
        if age is not None and age <= 180.0:
            return "LIVE"
        return "DEGRADED"

    providers = [
        {
            "provider": "ALPACA",
            "configured": bool(alpaca_configured),
            "status": "LIVE" if alpaca_configured else "NOT_CONFIGURED",
            "detail": "Entitlement SIP/OPRA activo." if alpaca_configured else "Sin credenciales configuradas.",
            "role": "PEER",
            "first_class": True,
            "channels": ["EQUITY_TRADE", "EQUITY_QUOTE", "OPTION_CHAIN", "OPTION_QUOTE", "OPTION_FLOW", "EQUITY_PRINT", "DARK_POOL"],
            "contributes": "Cadena de opciones, prints OPRA, tape de equity y breadth SIP.",
            "age_seconds": None,
            "error": None,
        },
        {
            "provider": "TASTYTRADE",
            "configured": bool(tt.get("configured")),
            "status": _tastytrade_state(tt),
            "role": "PEER",
            "first_class": True,
            "channels": ["EQUITY_QUOTE", "OPTION_QUOTE", "OPTION_CHAIN", "FUTURES_TICK", "IMPLIED_VOLATILITY"],
            "contributes": "Quotes DXLink, cadena con greeks y ticks de futuros.",
            "age_seconds": (_f(tt.get("last_event_age_ms"), None) / 1000.0
                            if tt.get("last_event_age_ms") is not None else _age(tt)),
            "error": tt.get("last_error") or None,
        },
        {
            "provider": "QUANTDATA",
            "configured": bool(qd.get("configured")),
            "status": _quantdata_state(qd),
            "role": "PEER",
            "first_class": True,
            "channels": ["EXPOSURE", "OPTION_FLOW", "OPEN_INTEREST", "IMPLIED_VOLATILITY", "DARK_POOL", "EQUITY_PRINT"],
            "contributes": "Exposición por strike/vencimiento, flujo y drift, OI, volatilidad, dark pool y prints.",
            "age_seconds": _age(qd),
            "error": qd.get("last_error"),
        },
    ]

    # Sólo el roster activo llega a la pantalla. Un proveedor fuera del roster no es
    # una carencia que haya que reportar como DEGRADED: es una decisión de esta
    # instalación, y mostrarlo en rojo sólo enseñaría a ignorar los avisos de verdad.
    active = _configured_peers()
    providers = [p for p in providers if p["provider"] in active]

    # v1.42.1 · Insignias y contador salen de la MISMA máquina de estados.
    #
    # Antes la insignia de cada fila usaba una definición de «LIVE» (configurado y
    # respondiendo) y el contador otra (dato fresco en ventana). Por eso la pantalla
    # podía decir «ALPACA LIVE · QUANTDATA LIVE» y «1/2 en vivo» a la vez, que es
    # una contradicción que le quita el valor a los dos indicadores.
    from .provider_state import classify as _classify, summarize as _summarize

    _session = session_resolver.resolve().describe()
    _states = []
    for p_row in providers:
        raw = str(p_row.get("status") or "").upper()
        # El estado que el propio proveedor publica sobre su salud NO se descarta:
        # si su adaptador ya dijo DEGRADED o STALE, esa evidencia es más específica
        # que cualquier inferencia por antigüedad, y la máquina la respeta.
        st = _classify(
            p_row["provider"],
            in_roster=p_row["provider"] in active,
            configured=bool(p_row.get("configured")),
            responding=raw not in ("NOT_CONFIGURED", "UNAVAILABLE", ""),
            has_data=raw in ("LIVE", "DEGRADED", "STALE"),
            age_seconds=p_row.get("age_seconds"),
            error=p_row.get("error"),
            channel=("PRICE" if p_row["provider"] == "ALPACA" else "EXPOSURE"),
            market_session=_session,
            provider_signal=raw,
        )
        _states.append(st)
        # La fila publica el estado canónico, no su propia interpretación.
        p_row["status"] = st.state
        p_row["state_detail"] = st.detail
        p_row["operational"] = st.operational

    _summary = _summarize(_states)
    configured = [p for p in providers if p["configured"]]
    live = [p for p in providers if p.get("operational")]

    # La calidad observada por canal la publica el consenso del motor; se adjunta
    # sin reinterpretarla para que la UI muestre exactamente lo que arbitró.
    channel_view = []
    cons = dict(consensus or {})
    selected_name = _norm_name(cons.get("selected_provider"))
    for row in cons.get("providers") or []:
        if not isinstance(row, dict):
            continue
        q = row.get("quality") if isinstance(row.get("quality"), dict) else {}
        name = _norm_name(row.get("source") or row.get("name"))
        if name in KNOWN_PROVIDERS and name not in active:
            continue
        channel_view.append({
            "provider": name,
            "channel": (q.get("channel_policy") or row.get("channel_policy")
                        or row.get("channel") or "DEFAULT"),
            "quality": _f(q.get("quality_score"), _f(row.get("quality_score"))),
            "label": q.get("quality_label") or row.get("quality_label"),
            "age_ms": _f(row.get("age_ms"), None),
            "stale": bool(row.get("stale")) if row.get("stale") is not None else None,
            "selected": name == selected_name,
        })
    # El consenso de arriba es la fabric de PRECIO. Quant Data es un proveedor REST
    # de analítica de opciones: no publica quotes, así que jamás aparecería ahí y la
    # tabla daba la impresión de que no participaba en ningún canal. Sus canales de
    # opciones se añaden desde la cobertura real de sus herramientas.
    cov = dict(options_coverage or {})
    if cov.get("configured"):
        by_channel: Dict[str, list] = {}
        for tool in cov.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            ch = _PAGE_CHANNEL.get(str(tool.get("page") or ""), "EXPOSURE")
            by_channel.setdefault(ch, []).append(tool)
        for ch, tools in by_channel.items():
            live = [t for t in tools if t.get("state") == "LIVE"]
            ages = [_f(t.get("age_seconds")) for t in live if t.get("age_seconds") is not None]
            # Calidad del canal = proporción de herramientas vivas, penalizada por
            # la edad del dato. Es la misma idea que usa el arbitraje de precio.
            share = len(live) / max(1, len(tools))
            age = (sum(ages) / len(ages)) if ages else None
            freshness = 1.0 if age is None else max(0.0, 1.0 - min(age / 900.0, 1.0))
            # v1.42.1 · `0.0` significaba dos cosas incompatibles: «se evaluó y salió
            # cero» y «todavía no hay nada que evaluar». La segunda no es una calidad
            # mala, es la ausencia de medida, y presentarlas igual hacía que toda la
            # tabla pareciera averiada durante la madrugada.
            # Una herramienta que ERRÓ sí fue evaluada: su calidad es 0, no «sin medir».
            # Sólo es N/A cuando todavía no se ha intentado nada.
            errored = any(t.get("error") or t.get("state") in ("NO_DISPONIBLE", "RUTA_INVALIDA",
                                                               "DEGRADADO") for t in tools)
            evaluated = errored or any(t.get("last_success") or t.get("age_seconds") is not None
                                       for t in tools)
            closed = not bool(_session.get("tradeable_now"))
            if live:
                quality = round(100.0 * share * (0.55 + 0.45 * freshness), 1)
                label = "HIGH" if share >= 0.8 and freshness > 0.6 else "GOOD"
            elif errored:
                quality, label = 0.0, "POOR"
            elif not evaluated:
                quality, label = None, "N/A"
            elif closed:
                quality, label = None, "MARKET_CLOSED"
            else:
                quality, label = 0.0, "POOR"
            channel_view.append({
                "provider": "QUANTDATA",
                "channel": ch,
                "quality": quality,
                "quality_state": label,
                "label": label,
                "age_ms": None if age is None else round(age * 1000.0, 0),
                "stale": None if age is None else age > 900.0,
                "tools_live": len(live),
                "tools_total": len(tools),
                # El alcance ya no se declara a mano: se deriva de quién es la
                # autoridad de las métricas de este canal. Cuando Quant Data lo es
                # —que es el caso de exposición, OI, volatilidad, flujo y dark
                # pool desde v1.43.0— este canal es autoridad primaria, no
                # corroboración externa, y la interfaz debe decirlo.
                "scope": ("PRIMARY_AUTHORITY" if _qd_is_authority(ch)
                          else "EXTERNAL_CORROBORATION"),
                "authority": channel_authority(ch),
                "native_authority": channel_authority(ch),
                "authority_is_quantdata": _qd_is_authority(ch),
                # `selected` significa que el canal tiene observación utilizable.
                "selected": bool(live),
                "fallback_authority": _NATIVE_FALLBACK.get(ch, "ITM"),
            })

    channel_view.sort(key=lambda r: (str(r["channel"]), -(_f(r.get("quality")) or 0.0)))

    return {
        "ready": bool(configured),
        "policy": parity_policy(),
        "providers": providers,
        "configured_count": _summary["total"],
        "live_count": _summary["operational"],
        "state_summary": _summary,
        "session": _session,
        "channels": channel_view,
        "selected_price_provider": _norm_name(cons.get("selected_provider")) or None,
        "consensus_confidence": cons.get("confidence"),
        "equal_weight": True,
        "confirmation_only": [],
        "roster": list(active),
        "roster_source": "ITM_OPTIONS_PEERS" if os.getenv("ITM_OPTIONS_PEERS") else "POR_DEFECTO",
    }

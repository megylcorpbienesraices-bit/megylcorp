"""PROCEDENCIA DE DATOS · ITM QUANT v1.43.0

POR QUÉ EXISTE ESTE MÓDULO
--------------------------
Un número en pantalla no dice de dónde viene. `GEX: 1.24B` se lee igual si lo
publicó Quant Data, si lo calculó el motor propio, o si es el último valor bueno
de hace veinte minutos. Las tres cosas significan cosas distintas y sólo una de
ellas es «el GEX del proveedor».

Hasta v1.42.7 la sustitución era SILENCIOSA: `_exposicion` prefería el perfil del
motor y sólo bajaba al proveedor si el motor no tenía nada, sin dejar rastro de
cuál de los dos había ganado. El operador no podía saberlo, y el Auditor tampoco.

Aquí se registra, métrica a métrica y símbolo a símbolo:

    metric  symbol  provider  endpoint  source_mode  timestamp
    raw_value  normalized_value  final_value  fallback_used  derivation

LOS CUATRO MODOS
----------------
    DIRECT_PROVIDER   el proveedor lo publica y es lo que se muestra
    DERIVED           ITM QUANT lo calcula a partir de otras entradas
    FALLBACK          la fuente primaria no estaba y se usó un respaldo declarado
    UNAVAILABLE       no hay valor; NO se publica un cero en su lugar

LOS SEIS ESTADOS DE DATO
------------------------
    DATA_OK  NO_PROVIDER_DATA  FILTERED_ALL  PROVIDER_ERROR  PARSER_ERROR  STALE

Un cero sólo es legítimo cuando el estado es DATA_OK y la aritmética da cero.

LA REGLA DURA
-------------
`guard_primary_source()` falla —no avisa: falla— cuando una métrica declarada como
`DIRECT_PROVIDER` se intenta publicar con un valor derivado mientras la fuente
primaria estaba sana. Eso es exactamente la sustitución silenciosa que la
especificación prohíbe, y una prueba puede ejercitarla.

NADA DE ESTO SE PINTA EN LA PANTALLA PRINCIPAL. El registro alimenta al Auditor.
La terminal muestra análisis, no nombres de endpoints.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ── modos de fuente ────────────────────────────────────────────────────────────
DIRECT_PROVIDER = "DIRECT_PROVIDER"
DERIVED = "DERIVED"
FALLBACK = "FALLBACK"
UNAVAILABLE = "UNAVAILABLE"

SOURCE_MODES = (DIRECT_PROVIDER, DERIVED, FALLBACK, UNAVAILABLE)

# ── estados de dato ────────────────────────────────────────────────────────────
DATA_OK = "DATA_OK"
NO_PROVIDER_DATA = "NO_PROVIDER_DATA"
FILTERED_ALL = "FILTERED_ALL"
PROVIDER_ERROR = "PROVIDER_ERROR"
PARSER_ERROR = "PARSER_ERROR"
STALE = "STALE"

DATA_STATES = (DATA_OK, NO_PROVIDER_DATA, FILTERED_ALL, PROVIDER_ERROR, PARSER_ERROR, STALE)

# Etiqueta única para el hueco. Nunca «$0.0».
NO_DATA_LABEL = "SIN DATOS"

# ── proveedores ────────────────────────────────────────────────────────────────
QUANTDATA = "QUANTDATA"
ALPACA = "ALPACA"
ITM_QUANT = "ITM_QUANT"

# Cuántos registros se conservan por símbolo. El Auditor necesita la sesión, no
# la historia entera: un anillo acotado no puede hacer crecer la memoria.
RING_PER_SYMBOL = 600


@dataclass(frozen=True)
class MetricPlan:
    """Quién DEBE producir una métrica, y con qué modo.

    `mode` es el modo ESPERADO. Si en ejecución se publica otro, el registro lo
    deja ver y `guard_primary_source` lo bloquea cuando no estaba justificado.
    """

    metric: str
    provider: str
    endpoint: str
    mode: str
    unit: Optional[str] = None
    fallback_provider: Optional[str] = None
    derivation: str = ""
    note: str = ""

    def describe(self) -> Dict[str, Any]:
        return {"metric": self.metric, "provider": self.provider, "endpoint": self.endpoint,
                "source_mode": self.mode, "unit": self.unit,
                "fallback_provider": self.fallback_provider,
                "derivation": self.derivation, "note": self.note}


def _plan(*rows: MetricPlan) -> Dict[str, MetricPlan]:
    return {r.metric: r for r in rows}


# ── EL PLAN CANÓNICO ───────────────────────────────────────────────────────────
# Las métricas `QD_*` son del proveedor y se publican con su nombre. Las `ITMQ_*`
# son inteligencia propia: nunca pueden presentarse como dato directo.
#
#   Alpaca      → qué está haciendo el precio del subyacente.
#   Quant Data  → cómo está posicionada la estructura de opciones (y sus Greeks).
#   ITM QUANT   → qué significa todo eso junto.
METRIC_PLAN: Dict[str, MetricPlan] = _plan(
    # ── mercado del subyacente · ALPACA ────────────────────────────────────────
    MetricPlan("UNDERLYING_PRICE", ALPACA, "alpaca:sip/trades", DIRECT_PROVIDER, "USD",
               note="Alpaca SIP es la autoridad del precio real del activo."),
    MetricPlan("UNDERLYING_CANDLES", ALPACA, "alpaca:sip/bars", DIRECT_PROVIDER, "OHLC",
               note="Velas del subyacente. Quant Data no las sustituye."),
    MetricPlan("UNDERLYING_QUOTES", ALPACA, "alpaca:sip/quotes", DIRECT_PROVIDER, "USD"),
    MetricPlan("UNDERLYING_VOLUME", ALPACA, "alpaca:sip/bars", DIRECT_PROVIDER, "SHARES"),

    # ── exposición · QUANT DATA ────────────────────────────────────────────────
    MetricPlan("QD_GEX", QUANTDATA, "POST /v1/options/tool/exposure-by-strike?GAMMA",
               DIRECT_PROVIDER, "GEX_PER_1PCT", fallback_provider=ITM_QUANT,
               note="Gamma Exposure oficial. El cálculo propio queda como auditoría."),
    MetricPlan("QD_DEX", QUANTDATA, "POST /v1/options/tool/exposure-by-strike?DELTA",
               DIRECT_PROVIDER, "DEX_NOTIONAL", fallback_provider=ITM_QUANT),
    MetricPlan("QD_VEX", QUANTDATA, "POST /v1/options/tool/exposure-by-strike?VANNA",
               DIRECT_PROVIDER, "VANNA_RAW", fallback_provider=ITM_QUANT),
    MetricPlan("QD_CHEX", QUANTDATA, "POST /v1/options/tool/exposure-by-strike?CHARM",
               DIRECT_PROVIDER, "CHARM_RAW", fallback_provider=ITM_QUANT),
    MetricPlan("QD_GEX_BY_EXPIRATION", QUANTDATA,
               "POST /v1/options/tool/exposure-by-expiration?GAMMA", DIRECT_PROVIDER),
    MetricPlan("QD_DEX_BY_EXPIRATION", QUANTDATA,
               "POST /v1/options/tool/exposure-by-expiration?DELTA", DIRECT_PROVIDER),
    MetricPlan("QD_VEX_BY_EXPIRATION", QUANTDATA,
               "POST /v1/options/tool/exposure-by-expiration?VANNA", DIRECT_PROVIDER),
    MetricPlan("QD_CHEX_BY_EXPIRATION", QUANTDATA,
               "POST /v1/options/tool/exposure-by-expiration?CHARM", DIRECT_PROVIDER),
    MetricPlan("QD_INTERVAL_MAP", QUANTDATA, "POST /v1/options/tool/interval-map",
               DIRECT_PROVIDER, "EXPOSURE_BY_TIME_STRIKE", fallback_provider=ITM_QUANT,
               note="Mapa dinámico tiempo × strike. Es el fondo de TRACE, no un heat "
                    "map estático."),

    # ── Greeks por contrato · QUANT DATA ───────────────────────────────────────
    MetricPlan("QD_GREEK_DELTA", QUANTDATA, "POST /v1/options/tool/order-flow/unconsolidated",
               DIRECT_PROVIDER, "GREEK"),
    MetricPlan("QD_GREEK_GAMMA", QUANTDATA, "POST /v1/options/tool/order-flow/unconsolidated",
               DIRECT_PROVIDER, "GREEK"),
    MetricPlan("QD_GREEK_THETA", QUANTDATA, "POST /v1/options/tool/order-flow/unconsolidated",
               DIRECT_PROVIDER, "GREEK"),
    MetricPlan("QD_GREEK_VEGA", QUANTDATA, "POST /v1/options/tool/order-flow/unconsolidated",
               DIRECT_PROVIDER, "GREEK"),
    MetricPlan("QD_GREEK_RHO", QUANTDATA, "POST /v1/options/tool/order-flow/unconsolidated",
               DIRECT_PROVIDER, "GREEK"),
    MetricPlan("QD_GREEK_VANNA", QUANTDATA, "POST /v1/options/tool/order-flow/unconsolidated",
               DIRECT_PROVIDER, "GREEK"),
    MetricPlan("QD_GREEK_CHARM", QUANTDATA, "POST /v1/options/tool/order-flow/unconsolidated",
               DIRECT_PROVIDER, "GREEK"),

    # ── flujo · QUANT DATA ─────────────────────────────────────────────────────
    MetricPlan("QD_NET_FLOW", QUANTDATA, "POST /v1/options/tool/net-flow", DIRECT_PROVIDER,
               "USD_PREMIUM"),
    MetricPlan("QD_NET_DRIFT", QUANTDATA, "POST /v1/options/tool/net-drift", DIRECT_PROVIDER,
               "USD_PREMIUM",
               note="Endpoint oficial. Prohibido reconstruirlo desde GEX, DEX o Net Flow."),
    MetricPlan("QD_ORDER_FLOW_CONSOLIDATED", QUANTDATA,
               "POST /v1/options/tool/order-flow/consolidated", DIRECT_PROVIDER),
    MetricPlan("QD_ORDER_FLOW_UNCONSOLIDATED", QUANTDATA,
               "POST /v1/options/tool/order-flow/unconsolidated", DIRECT_PROVIDER),

    # ── interés abierto · QUANT DATA ───────────────────────────────────────────
    MetricPlan("QD_OPEN_INTEREST_BY_STRIKE", QUANTDATA,
               "POST /v1/options/tool/open-interest-by-strike", DIRECT_PROVIDER, "CONTRACTS",
               note="Nunca se reconstruye OI con volumen ni con aproximaciones."),
    MetricPlan("QD_OPEN_INTEREST_BY_EXPIRATION", QUANTDATA,
               "POST /v1/options/tool/open-interest-by-expiration", DIRECT_PROVIDER, "CONTRACTS"),
    MetricPlan("QD_OPEN_INTEREST_CHANGE", QUANTDATA,
               "POST /v1/options/tool/open-interest-change", DIRECT_PROVIDER, "CONTRACTS"),
    MetricPlan("QD_OPEN_INTEREST_OVER_TIME", QUANTDATA,
               "POST /v1/options/tool/open-interest-over-time", DIRECT_PROVIDER, "CONTRACTS"),
    MetricPlan("QD_MAX_PAIN", QUANTDATA, "POST /v1/options/tool/max-pain", DIRECT_PROVIDER, "STRIKE"),
    MetricPlan("QD_MAX_PAIN_OVER_TIME", QUANTDATA,
               "POST /v1/options/tool/max-pain-over-time", DIRECT_PROVIDER, "STRIKE"),

    # ── volatilidad · QUANT DATA ───────────────────────────────────────────────
    MetricPlan("QD_IV_RANK", QUANTDATA, "POST /v1/options/tool/iv-rank", DIRECT_PROVIDER, "PCT"),
    MetricPlan("QD_VOLATILITY_SKEW", QUANTDATA, "POST /v1/options/tool/volatility-skew",
               DIRECT_PROVIDER, "IV"),
    MetricPlan("QD_TERM_STRUCTURE", QUANTDATA, "POST /v1/options/tool/term-structure",
               DIRECT_PROVIDER, "IV"),
    MetricPlan("QD_VOLATILITY_DRIFT", QUANTDATA, "POST /v1/options/tool/volatility-drift",
               DIRECT_PROVIDER, "IV"),

    # ── dark pool · QUANT DATA ─────────────────────────────────────────────────
    MetricPlan("QD_DARK_FLOW", QUANTDATA, "POST /v1/equities/tool/dark-flow", DIRECT_PROVIDER,
               "SHARES", fallback_provider=ITM_QUANT,
               note="Fuente principal. La clasificación por `venue` es auditoría."),
    MetricPlan("QD_DARK_POOL_LEVELS", QUANTDATA, "POST /v1/equities/tool/dark-pool-levels",
               DIRECT_PROVIDER, "USD", fallback_provider=ITM_QUANT),
    MetricPlan("QD_EQUITY_PRINTS", QUANTDATA, "POST /v1/equities/tool/equity-prints",
               DIRECT_PROVIDER, fallback_provider=ITM_QUANT),

    # ── estadísticas · QUANT DATA (contexto secundario) ────────────────────────
    MetricPlan("QD_CONTRACT_STATISTICS", QUANTDATA,
               "POST /v1/options/tool/contract-statistics", DIRECT_PROVIDER),
    MetricPlan("QD_TRADE_SIDE_STATISTICS", QUANTDATA,
               "POST /v1/options/tool/trade-side-statistics", DIRECT_PROVIDER),
    MetricPlan("QD_MARKET_SHARE", QUANTDATA, "POST /v1/options/tool/market-share", DIRECT_PROVIDER),
    MetricPlan("QD_GAINERS_LOSERS", QUANTDATA, "POST /v1/options/tool/gainers-losers", DIRECT_PROVIDER),

    # ── INTELIGENCIA PROPIA · ITM QUANT ────────────────────────────────────────
    # Ninguna de éstas puede presentarse como dato del proveedor.
    MetricPlan("ITMQ_GAMMA_PRESSURE", ITM_QUANT, "engine:gamma_pressure", DERIVED,
               derivation="QD_GEX + QD_INTERVAL_MAP + UNDERLYING_PRICE"),
    MetricPlan("ITMQ_GAMMA_MIGRATION", ITM_QUANT, "engine:gamma_migration", DERIVED,
               derivation="QD_INTERVAL_MAP sobre ventanas consecutivas"),
    MetricPlan("ITMQ_FLOW_CONFLUENCE", ITM_QUANT, "engine:flow_confluence", DERIVED,
               derivation="QD_NET_DRIFT + QD_NET_FLOW + QD_DEX + QD_DARK_FLOW"),
    MetricPlan("ITMQ_STRUCTURAL_SCORE", ITM_QUANT, "engine:structural_score", DERIVED,
               derivation="QD_GEX + QD_DEX + QD_OPEN_INTEREST_BY_STRIKE + QD_INTERVAL_MAP"),
    MetricPlan("ITMQ_QFLOW_CONCENTRATION", ITM_QUANT, "engine:qflow", DERIVED,
               derivation="QD_NET_FLOW + QD_ORDER_FLOW_* normalizado por activo"),
    MetricPlan("ITMQ_QFLOW_LEVEL", ITM_QUANT, "engine:qflow_level", DERIVED,
               derivation="media de UNDERLYING_PRICE ponderada por |prima| de QD_NET_FLOW"),
    MetricPlan("ITMQ_REGIME", ITM_QUANT, "engine:regime", DERIVED,
               derivation="QD_GEX + QD_VEX + QD_CHEX + volatilidad + precio"),
    MetricPlan("ITMQ_PERSISTENCE", ITM_QUANT, "engine:persistence", DERIVED),
    MetricPlan("ITMQ_DIVERGENCE", ITM_QUANT, "engine:divergence", DERIVED),
    MetricPlan("ITMQ_DOMINANT_STRIKE", ITM_QUANT, "engine:dominant_strike", DERIVED),
    MetricPlan("ITMQ_STRUCTURAL_LEVELS", ITM_QUANT, "engine:structural_levels", DERIVED),
    MetricPlan("ITMQ_BREAK_CONTAINMENT", ITM_QUANT, "engine:break_containment", DERIVED),
)

# Métricas cuya autoridad es el proveedor: ninguna de ellas puede quedar cubierta
# en silencio por un cálculo propio.
PRIMARY_PROVIDER_METRICS: Tuple[str, ...] = tuple(
    sorted(m for m, p in METRIC_PLAN.items() if p.mode == DIRECT_PROVIDER))

DERIVED_METRICS: Tuple[str, ...] = tuple(
    sorted(m for m, p in METRIC_PLAN.items() if p.mode == DERIVED))


class SilentSubstitution(RuntimeError):
    """Se intentó tapar una fuente primaria sana con un cálculo propio."""


@dataclass
class LineageRecord:
    metric: str
    symbol: str
    provider: str
    endpoint: str
    source_mode: str
    timestamp: str
    state: str = DATA_OK
    raw_value: Any = None
    normalized_value: Any = None
    final_value: Any = None
    fallback_used: bool = False
    derivation: str = ""
    unit: Optional[str] = None
    detail: str = ""
    age_seconds: Optional[float] = None
    rows: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact(value: Any, depth: int = 0) -> Any:
    """Resumen publicable de un valor. Una matriz entera no cabe en un registro.

    Se conserva el número cuando es un número —que es lo que hay que poder
    auditar— y se resume la forma cuando es una colección.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > 200:
            return value[:200] + "…"
        return value
    if depth >= 2:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        if len(value) > 8:
            return {"__keys__": sorted(map(str, list(value)[:8])), "__size__": len(value)}
        return {str(k): _compact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > 4:
            return {"__list__": len(value), "__head__": [_compact(v, depth + 1) for v in value[:2]]}
        return [_compact(v, depth + 1) for v in value]
    return f"<{type(value).__name__}>"


class LineageRegistry:
    """Anillo por símbolo con el último registro vivo de cada métrica.

    Dos lecturas distintas y las dos necesarias:
      * `latest()`  — el estado actual de cada métrica; es lo que juzga el Auditor.
      * `history()` — cómo se llegó hasta aquí; es lo que explica una discrepancia.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._latest: Dict[Tuple[str, str], LineageRecord] = {}
        self._ring: Dict[str, List[LineageRecord]] = {}
        self._violations: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ escribir

    def record(self, metric: str, symbol: str, *, source_mode: str,
               state: str = DATA_OK, provider: Optional[str] = None,
               endpoint: Optional[str] = None, raw_value: Any = None,
               normalized_value: Any = None, final_value: Any = None,
               fallback_used: bool = False, derivation: str = "",
               unit: Optional[str] = None, detail: str = "",
               age_seconds: Optional[float] = None,
               rows: Optional[int] = None) -> LineageRecord:
        m = str(metric or "").strip().upper()
        sym = str(symbol or "").strip().upper()
        plan = METRIC_PLAN.get(m)
        mode = str(source_mode or "").strip().upper()
        if mode not in SOURCE_MODES:
            mode = UNAVAILABLE
        st = str(state or "").strip().upper()
        if st not in DATA_STATES:
            st = PARSER_ERROR
        rec = LineageRecord(
            metric=m, symbol=sym,
            provider=str(provider or (plan.provider if plan else ITM_QUANT)).upper(),
            endpoint=str(endpoint or (plan.endpoint if plan else "")),
            source_mode=mode, timestamp=_now_iso(), state=st,
            raw_value=_compact(raw_value), normalized_value=_compact(normalized_value),
            final_value=_compact(final_value), fallback_used=bool(fallback_used),
            derivation=str(derivation or (plan.derivation if plan else "")),
            unit=unit or (plan.unit if plan else None),
            detail=str(detail or "")[:240], age_seconds=age_seconds,
            rows=None if rows is None else int(rows),
        )
        with self._lock:
            self._latest[(sym, m)] = rec
            ring = self._ring.setdefault(sym, [])
            ring.append(rec)
            if len(ring) > RING_PER_SYMBOL:
                del ring[: len(ring) - RING_PER_SYMBOL]
        return rec

    def note_violation(self, metric: str, symbol: str, detail: str) -> None:
        with self._lock:
            self._violations.append({"metric": str(metric).upper(), "symbol": str(symbol).upper(),
                                     "detail": str(detail)[:300], "at": _now_iso()})
            if len(self._violations) > 200:
                del self._violations[: len(self._violations) - 200]

    # -------------------------------------------------------------------- leer

    def latest(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            rows = list(self._latest.values())
        if symbol:
            sym = str(symbol).upper()
            rows = [r for r in rows if r.symbol == sym]
        return [r.to_dict() for r in sorted(rows, key=lambda r: (r.symbol, r.metric))]

    def for_metric(self, metric: str, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._latest.get((str(symbol).upper(), str(metric).upper()))
        return rec.to_dict() if rec else None

    def history(self, symbol: str, limit: int = 120) -> List[Dict[str, Any]]:
        with self._lock:
            ring = list(self._ring.get(str(symbol).upper(), []))
        return [r.to_dict() for r in ring[-max(1, int(limit)):]]

    def violations(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._violations]

    def clear_symbol(self, symbol: str) -> None:
        sym = str(symbol).upper()
        with self._lock:
            for key in [k for k in self._latest if k[0] == sym]:
                self._latest.pop(key, None)
            self._ring.pop(sym, None)

    def reset(self) -> None:
        with self._lock:
            self._latest.clear()
            self._ring.clear()
            self._violations.clear()

    # ------------------------------------------------------------------ auditar

    def audit(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Informe para el Auditor. NO se publica en la pantalla principal."""
        rows = self.latest(symbol)
        by_mode: Dict[str, int] = {m: 0 for m in SOURCE_MODES}
        by_state: Dict[str, int] = {s: 0 for s in DATA_STATES}
        for r in rows:
            by_mode[r["source_mode"]] = by_mode.get(r["source_mode"], 0) + 1
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        direct = [r for r in rows if r["source_mode"] == DIRECT_PROVIDER]
        derived = [r for r in rows if r["source_mode"] == DERIVED]
        fallbacks = [r for r in rows if r["fallback_used"]]
        planned = set(PRIMARY_PROVIDER_METRICS)
        covered = {r["metric"] for r in direct}
        return {
            "symbol": (str(symbol).upper() if symbol else None),
            "records": rows,
            "by_source_mode": by_mode,
            "by_state": by_state,
            "direct_provider_metrics": sorted(covered),
            "derived_metrics": sorted({r["metric"] for r in derived}),
            "fallbacks_in_use": [{"metric": r["metric"], "provider": r["provider"],
                                  "detail": r["detail"]} for r in fallbacks],
            "primary_not_covered": sorted(planned - covered),
            "violations": self.violations(),
            "plan": {m: p.describe() for m, p in sorted(METRIC_PLAN.items())},
            "doctrine": ("Alpaca → mercado del subyacente · Quant Data → opciones y "
                         "estructura · ITM QUANT → interpretación. Un DERIVED jamás se "
                         "publica como DIRECT_PROVIDER."),
        }


LINEAGE = LineageRegistry()


# ── utilidades de publicación ──────────────────────────────────────────────────

def guard_primary_source(metric: str, symbol: str, *, publishing_mode: str,
                         provider_healthy: bool, detail: str = "",
                         strict: bool = True) -> None:
    """Bloquea la sustitución silenciosa de una fuente primaria sana.

    Si `metric` está declarada como `DIRECT_PROVIDER` en el plan, el proveedor está
    sano, y aun así se intenta publicar un valor `DERIVED`, eso es exactamente lo
    que la especificación prohíbe: el usuario vería un número propio creyendo que
    es del proveedor.

    Un `FALLBACK` declarado SÍ está permitido, porque va etiquetado como tal y el
    Auditor lo ve. Lo prohibido es el disfraz, no el respaldo.
    """
    m = str(metric or "").upper()
    plan = METRIC_PLAN.get(m)
    if plan is None or plan.mode != DIRECT_PROVIDER:
        return
    if not provider_healthy:
        return
    if str(publishing_mode).upper() == DERIVED:
        msg = (f"«{m}» tiene a {plan.provider} como fuente primaria y {plan.provider} "
               f"está sano, pero se intentó publicar un valor DERIVED. "
               f"{detail}".strip())
        LINEAGE.note_violation(m, symbol, msg)
        if strict:
            raise SilentSubstitution(msg)


def publish(metric: str, symbol: str, value: Any, *, source_mode: str,
            state: str = DATA_OK, **kw: Any) -> Dict[str, Any]:
    """Registra la procedencia y devuelve el sobre que consume la interfaz.

    El sobre lleva `value` y `state`, nunca un cero de relleno: cuando no hay dato,
    `value` es None y `display` es la etiqueta SIN DATOS. Es lo que impide que un
    fallo de datos se lea como «el mercado está plano».
    """
    rec = LINEAGE.record(metric, symbol, source_mode=source_mode, state=state,
                         final_value=value, **kw)
    ok = (state == DATA_OK and source_mode != UNAVAILABLE and value is not None)
    return {
        "metric": rec.metric, "symbol": rec.symbol, "value": value if ok else None,
        "ready": bool(ok), "state": rec.state, "source_mode": rec.source_mode,
        "provider": rec.provider, "fallback_used": rec.fallback_used,
        "display": None if ok else NO_DATA_LABEL,
        "detail": rec.detail or None,
    }


def real_zero(value: Any, state: str) -> bool:
    """¿Este cero es un cero de verdad?

    Sólo cuando hay datos válidos y la aritmética dio cero. Un cero con estado
    distinto de DATA_OK es un hueco disfrazado y no debe mostrarse como número.
    """
    try:
        return state == DATA_OK and value is not None and float(value) == 0.0
    except (TypeError, ValueError):
        return False


def mode_of(metric: str) -> str:
    plan = METRIC_PLAN.get(str(metric or "").upper())
    return plan.mode if plan else DERIVED


def plan_for(metric: str) -> Optional[MetricPlan]:
    return METRIC_PLAN.get(str(metric or "").upper())


def is_direct_provider_metric(metric: str) -> bool:
    return mode_of(metric) == DIRECT_PROVIDER


def certification_chain(metric: str, symbol: str) -> Dict[str, Any]:
    """RAW → NORMALIZER → ENGINE → API INTERNA → FRONTEND para una métrica.

    La certificación que pide la especificación no la pasa un endpoint que
    responde: la pasa poder enseñar que el valor y su significado sobreviven a
    los cinco tramos. Este informe es lo que una prueba compara.
    """
    rec = LINEAGE.for_metric(metric, symbol)
    plan = plan_for(metric)
    if rec is None:
        return {"metric": str(metric).upper(), "symbol": str(symbol).upper(),
                "ready": False, "reason": "SIN REGISTRO DE PROCEDENCIA",
                "plan": plan.describe() if plan else None}
    stages = [
        {"stage": "RAW_PROVIDER", "present": rec["raw_value"] is not None,
         "endpoint": rec["endpoint"], "provider": rec["provider"]},
        {"stage": "NORMALIZER", "present": rec["normalized_value"] is not None},
        {"stage": "ENGINE", "present": rec["source_mode"] in (DIRECT_PROVIDER, DERIVED, FALLBACK)},
        {"stage": "INTERNAL_API", "present": rec["final_value"] is not None},
        {"stage": "FRONTEND", "present": rec["state"] == DATA_OK and rec["final_value"] is not None},
    ]
    return {
        "metric": rec["metric"], "symbol": rec["symbol"], "plan": plan.describe() if plan else None,
        "record": rec, "stages": stages,
        "ready": all(s["present"] for s in stages),
        "broken_at": next((s["stage"] for s in stages if not s["present"]), None),
        "meaning_preserved": (rec["source_mode"] != DERIVED
                              or not (plan and plan.mode == DIRECT_PROVIDER)),
    }


__all__ = [
    "DIRECT_PROVIDER", "DERIVED", "FALLBACK", "UNAVAILABLE", "SOURCE_MODES",
    "DATA_OK", "NO_PROVIDER_DATA", "FILTERED_ALL", "PROVIDER_ERROR", "PARSER_ERROR",
    "STALE", "DATA_STATES", "NO_DATA_LABEL", "QUANTDATA", "ALPACA", "ITM_QUANT",
    "MetricPlan", "METRIC_PLAN", "PRIMARY_PROVIDER_METRICS", "DERIVED_METRICS",
    "LineageRecord", "LineageRegistry", "LINEAGE", "SilentSubstitution",
    "guard_primary_source", "publish", "real_zero", "mode_of", "plan_for",
    "is_direct_provider_metric", "certification_chain",
]

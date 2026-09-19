"""Cortacircuitos de frescura de datos.

Motivacion (auditoria v1.27.4): el motor absorbe errores de proveedor en bloques
``except Exception`` amplios. El resultado no es una caida, sino algo peor para
capital real: una superficie GEX/DEX plausible calculada sobre datos viejos.

Este modulo convierte esa degradacion silenciosa en un estado explicito. La
regla es una sola: si el dato no es suficientemente reciente, no se publica un
numero; se publica una negativa.

Uso tipico::

    breaker = FreshnessBreaker(OPTION_CHAIN_POLICY)
    estado = breaker.observe(snapshot_timestamp, market_open=True)
    if not breaker.allow():
        return {"ok": False, "circuito": estado}

El cortacircuitos no adivina el calendario de mercado: quien llama declara
``market_open``. Con el mercado cerrado la antiguedad se mide contra un umbral
distinto, porque un snapshot del cierre del viernes no esta corrupto el sabado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from threading import RLock
from typing import Any
from .obs import note as _obs_note
from .session_expectations import activity_expectation

FRESH = "FRESH"
WARN = "WARN"
STALE = "STALE"
UNKNOWN = "UNKNOWN"

CLOSED = "CLOSED"   # circuito cerrado: el flujo pasa
OPEN = "OPEN"       # circuito abierto: publicar numeros esta prohibido
BLOCKED_PENDING = "BLOCKED_PENDING"  # lectura mala actual, aun sin latch persistente
BYPASS_REPLAY = "BYPASS_REPLAY"      # replay historico: la edad contra reloj real no aplica


class StaleDataError(RuntimeError):
    """El dato exigido no cumple la politica de frescura."""

    def __init__(self, detail: str, assessment: dict[str, Any] | None = None):
        super().__init__(detail)
        self.assessment = assessment or {}


@dataclass(frozen=True)
class FreshnessPolicy:
    name: str
    max_age_seconds: float
    warn_age_seconds: float | None = None
    closed_max_age_seconds: float | None = None

    def limit(self, market_open: bool) -> float:
        if market_open or self.closed_max_age_seconds is None:
            return float(self.max_age_seconds)
        return float(self.closed_max_age_seconds)

    def warn_threshold(self, market_open: bool = True) -> float:
        """Umbral de aviso proporcional al limite vigente.

        Con el mercado cerrado el limite se relaja, y el aviso debe relajarse en
        la misma proporcion: si no, todo snapshot nocturno nace en WARN y el
        aviso deja de significar nada.
        """
        base = float(self.warn_age_seconds) if self.warn_age_seconds is not None else self.max_age_seconds / 2.0
        limite = self.limit(market_open)
        if limite <= 0 or self.max_age_seconds <= 0:
            return base
        return base * (limite / float(self.max_age_seconds))


# Politicas por defecto. Conservadoras a proposito: es mas barato negarse de mas
# que publicar una superficie de gamma vieja.
OPTION_CHAIN_POLICY = FreshnessPolicy("cadena_opciones", max_age_seconds=90.0, warn_age_seconds=35.0,
                                      # Structural option data legitimately spans the overnight/weekend gap.
                                      # PREMARKET/AFTERHOURS use this closed-session limit for the option clock
                                      # while the underlying clock remains LIVE-strict.  Five days covers a
                                      # normal/holiday weekend without ever relaxing the 90s RTH requirement.
                                      closed_max_age_seconds=120 * 3600.0)
UNDERLYING_POLICY = FreshnessPolicy("subyacente", max_age_seconds=20.0, warn_age_seconds=8.0,
                                    closed_max_age_seconds=120 * 3600.0)
MACRO_POLICY = FreshnessPolicy("macro", max_age_seconds=36 * 3600.0, warn_age_seconds=12 * 3600.0,
                               closed_max_age_seconds=96 * 3600.0)


def to_epoch(value: Any) -> float | None:
    """Normaliza datetime / pandas.Timestamp / epoch / ISO-8601 a epoch UTC."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v == v and abs(v) != float("inf") else None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    ts = getattr(value, "timestamp", None)          # pandas.Timestamp y similares
    if callable(ts):
        try:
            return float(ts())
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    return None


def age_seconds(value: Any, now: float | None = None) -> float | None:
    epoch = to_epoch(value)
    if epoch is None:
        return None
    return (time.time() if now is None else float(now)) - epoch


def assess(value: Any, policy: FreshnessPolicy, *, market_open: bool = True,
           now: float | None = None) -> dict[str, Any]:
    """Evalua un timestamp contra la politica. Nunca lanza."""
    age = age_seconds(value, now)
    limit = policy.limit(market_open)
    if age is None:
        return {"politica": policy.name, "estado": UNKNOWN, "antiguedad_s": None,
                "limite_s": limit, "mercado_abierto": market_open,
                "detalle": "timestamp ausente o no interpretable"}
    # Un timestamp en el futuro indica reloj desincronizado: tambien es un fallo.
    if age < -5.0:
        return {"politica": policy.name, "estado": STALE, "antiguedad_s": round(age, 3),
                "limite_s": limit, "mercado_abierto": market_open,
                "detalle": "timestamp en el futuro: reloj desincronizado"}
    if age > limit:
        estado, detalle = STALE, f"antiguedad {age:.1f}s supera el limite {limit:.1f}s"
    elif age > policy.warn_threshold(market_open):
        estado, detalle = WARN, f"antiguedad {age:.1f}s por encima del umbral de aviso"
    else:
        estado, detalle = FRESH, "dentro de la politica"
    return {"politica": policy.name, "estado": estado, "antiguedad_s": round(age, 3),
            "limite_s": limit, "mercado_abierto": market_open, "detalle": detalle}


def assess_age(age: float | None, policy: FreshnessPolicy, *, market_open: bool = True) -> dict[str, Any]:
    """Variante para quien ya calculo la antiguedad en segundos."""
    if age is None:
        return assess(None, policy, market_open=market_open)
    return assess(-float(age), policy, market_open=market_open, now=0.0)


def require_fresh(value: Any, policy: FreshnessPolicy, *, market_open: bool = True,
                  now: float | None = None) -> dict[str, Any]:
    """Igual que :func:`assess` pero falla ruidosamente. Usar antes de publicar cifras."""
    result = assess(value, policy, market_open=market_open, now=now)
    if result["estado"] in {STALE, UNKNOWN}:
        raise StaleDataError(f"[{policy.name}] {result['detalle']}", result)
    return result


@dataclass
class FreshnessBreaker:
    """Cortacircuitos con histeresis.

    No abre al primer tropiezo (una lectura perdida no es una averia) ni cierra
    de golpe: exige lecturas frescas consecutivas para rearmarse.
    """

    policy: FreshnessPolicy
    trip_after: int = 2
    recover_after: int = 3
    state: str = CLOSED
    consecutive_stale: int = 0
    consecutive_fresh: int = 0
    last_assessment: dict[str, Any] = field(default_factory=dict)
    opened_at: float | None = None
    trips: int = 0

    def observe(self, value: Any, *, market_open: bool = True,
                now: float | None = None) -> dict[str, Any]:
        result = assess(value, self.policy, market_open=market_open, now=now)
        bad = result["estado"] in {STALE, UNKNOWN}
        if bad:
            self.consecutive_stale += 1
            self.consecutive_fresh = 0
            if self.state == CLOSED and self.consecutive_stale >= self.trip_after:
                self.state = OPEN
                self.opened_at = time.time() if now is None else float(now)
                self.trips += 1
        else:
            self.consecutive_fresh += 1
            self.consecutive_stale = 0
            if self.state == OPEN and self.consecutive_fresh >= self.recover_after:
                self.state = CLOSED
                self.opened_at = None
        result["circuito"] = self.state
        result["fallos_consecutivos"] = self.consecutive_stale
        result["aperturas"] = self.trips
        self.last_assessment = result
        return result

    def allow(self) -> bool:
        """True si es legitimo publicar numeros calculados con este dato."""
        return self.state == CLOSED

    def guard(self) -> None:
        if not self.allow():
            raise StaleDataError(
                f"[{self.policy.name}] circuito ABIERTO: {self.last_assessment.get('detalle', 'datos rancios')}",
                self.last_assessment)

    def reset(self) -> None:
        self.state = CLOSED
        self.consecutive_stale = 0
        self.consecutive_fresh = 0
        self.opened_at = None

    def snapshot(self) -> dict[str, Any]:
        return {"politica": self.policy.name, "circuito": self.state,
                "fallos_consecutivos": self.consecutive_stale, "aperturas": self.trips,
                "abierto_desde": self.opened_at, "ultima_evaluacion": dict(self.last_assessment)}


@dataclass
class PublicationFreshnessGate:
    """Gate compuesto y stateful para publicacion LIVE.

    La lectura mala actual bloquea inmediatamente la publicacion. Dos ciclos malos
    consecutivos dejan el circuito *latched* en OPEN; una vez abierto exige tres
    ciclos frescos consecutivos para rearmarse. Esto evita tanto publicar una cifra
    rancia en el primer fallo como rearmar de golpe tras una sola lectura buena.
    """

    key: str
    option_breaker: FreshnessBreaker = field(default_factory=lambda: FreshnessBreaker(OPTION_CHAIN_POLICY, trip_after=2, recover_after=3))
    underlying_breaker: FreshnessBreaker = field(default_factory=lambda: FreshnessBreaker(UNDERLYING_POLICY, trip_after=2, recover_after=3))
    last_cycle_id: str | None = None
    last_result: dict[str, Any] = field(default_factory=dict)

    def observe(self, option_timestamp: Any, underlying_timestamp: Any, *, market_open: bool,
                option_market_open: bool | None = None, underlying_market_open: bool | None = None,
                cycle_id: str | None = None, now: float | None = None) -> dict[str, Any]:
        # Option structure and the underlying do not share a clock outside RTH.
        # PREMARKET/AFTERHOURS may have a LIVE underlying while listed equity options
        # are closed.  Keeping these clocks separate prevents the 90-second option
        # quote policy from incorrectly hiding valid structural GEX/DEX before 09:30 NY.
        opt_open = bool(market_open if option_market_open is None else option_market_open)
        stk_open = bool(market_open if underlying_market_open is None else underlying_market_open)
        # Cache only an *identical observation*, never merely the same quality_clock.
        opt_epoch = to_epoch(option_timestamp)
        stk_epoch = to_epoch(underlying_timestamp)
        cid = f"{cycle_id or ''}|{opt_epoch!r}|{stk_epoch!r}|{int(opt_open)}|{int(stk_open)}"
        if cid == self.last_cycle_id and self.last_result:
            return dict(self.last_result)
        opt = self.option_breaker.observe(option_timestamp, market_open=opt_open, now=now)
        stk = self.underlying_breaker.observe(underlying_timestamp, market_open=stk_open, now=now)
        current_bad = any(x.get("estado") in {STALE, UNKNOWN} for x in (opt, stk))
        latched_open = (not self.option_breaker.allow()) or (not self.underlying_breaker.allow())
        state = OPEN if latched_open else BLOCKED_PENDING if current_bad else CLOSED
        allow = (not current_bad) and (not latched_open)
        if latched_open:
            detail = next((x.get("detalle") for x in (opt, stk) if x.get("circuito") == OPEN), "circuito de frescura abierto")
        elif current_bad:
            detail = next((x.get("detalle") for x in (opt, stk) if x.get("estado") in {STALE, UNKNOWN}), "dato critico no fresco")
        else:
            detail = "dentro de politica"
        out = {
            "estado": state,
            "publicar_permitido": bool(allow),
            "cadena_opciones": opt,
            "subyacente": stk,
            "motivo": detail,
            "histeresis": {
                "trip_after": 2, "recover_after": 3,
                "option_open": not self.option_breaker.allow(),
                "underlying_open": not self.underlying_breaker.allow(),
            },
            "gate_key": self.key,
            "clock_policy": {
                "option_market_open": opt_open,
                "underlying_market_open": stk_open,
                "option_mode": "LIVE_QUOTES" if opt_open else "STRUCTURAL_SESSION",
                "underlying_mode": "LIVE" if stk_open else "CLOSED_SESSION",
            },
        }
        self.last_cycle_id = cid or None
        self.last_result = dict(out)
        return out

    def reset(self) -> None:
        self.option_breaker.reset(); self.underlying_breaker.reset()
        self.last_cycle_id = None; self.last_result = {}


_REGISTRY_LOCK = RLock()
_PUBLICATION_GATES: dict[str, PublicationFreshnessGate] = {}

def publication_gate_for(key: str) -> PublicationFreshnessGate:
    k = str(key or "GLOBAL").upper()
    with _REGISTRY_LOCK:
        gate = _PUBLICATION_GATES.get(k)
        if gate is None:
            gate = PublicationFreshnessGate(k)
            _PUBLICATION_GATES[k] = gate
        return gate

def reset_publication_gate(key: str | None = None) -> None:
    with _REGISTRY_LOCK:
        if key is None:
            _PUBLICATION_GATES.clear()
        else:
            _PUBLICATION_GATES.pop(str(key).upper(), None)

def evaluate_publication_gate(meta: dict[str, Any], *, market_state: str = "", now: float | None = None,
                              is_replay: bool | None = None) -> dict[str, Any]:
    """Evaluate the real publication gate from provider timestamps.

    Replay is exempt: old timestamps are the subject of replay and are validated by
    archive/session integrity rather than wall-clock freshness.

    v1.27.8 · La exencion exige AUTORIDAD, no una cadena. Hasta v1.27.7 bastaba con
    ``meta["market_state"] == "REPLAY"`` para desactivar por completo el gate. Una
    clave de diccionario no puede ser la unica llave de una puerta de seguridad:
    hoy solo la abre ``is_replay=True``, que el llamador obtiene de
    ``ReplayContext.is_replay``, la misma autoridad que ya usaban el WebSocket y
    ``_publication_blocked_payload``. Una declaracion sin respaldo no exime: se
    evalua la frescura con normalidad y se deja constancia del desajuste.
    """
    meta = dict(meta or {})
    state = str(market_state or meta.get("market_state") or "").upper()
    declarado = state == "REPLAY"
    if is_replay is True:
        return {"estado": BYPASS_REPLAY, "publicar_permitido": True,
                "motivo": "replay historico: wall-clock freshness no aplica",
                "replay": True, "autoridad": "replay_context"}
    key = str(meta.get("symbol") or meta.get("underlying_symbol") or "GLOBAL").upper()
    clock = activity_expectation(key, datetime.fromtimestamp(float(now), tz=timezone.utc) if now is not None else None)
    if state == "CLOSED" and clock.get("session_phase") == "LONDON":
        state = "LONDON"
    market_open = state not in {"CLOSED", "DEMO", "LONDON"}
    # Session-aware clocks: equity/index options do not trade during US PREMARKET or
    # AFTERHOURS, but their last valid structural chain/OI is still usable to reprice
    # Gamma/Delta/GEX/DEX against a fresh underlying.  During REGULAR/FUTURES_SESSION
    # both clocks remain strict.  Unknown state remains strict/fail-closed.
    if state in {"PREMARKET", "AFTERHOURS"}:
        option_market_open = False
        underlying_market_open = True
    elif state == "LONDON":
        option_market_open = bool(clock.get("expected_option_flow_live"))
        underlying_market_open = bool(clock.get("expected_price_live"))
    elif state in {"CLOSED", "DEMO"}:
        option_market_open = False
        underlying_market_open = False
    else:
        option_market_open = True
        underlying_market_open = True
    cycle_id = str(meta.get("quality_clock") or meta.get("fetched_at") or meta.get("received_at") or "")
    out = publication_gate_for(key).observe(
        meta.get("latest_option_market_timestamp"),
        meta.get("stock_market_timestamp"),
        market_open=market_open, option_market_open=option_market_open,
        underlying_market_open=underlying_market_open, cycle_id=cycle_id, now=now,
    )
    out = dict(out)
    out["session_expectation"] = clock
    if state == "LONDON":
        out["london_structural_mode"] = True
        out["london_note"] = "LONDON: US cash/options may be expected idle; stale-red is reserved for feeds expected to be LIVE."
    if state == "PREMARKET":
        out = dict(out)
        out["premarket_structural_options"] = True
        out["premarket_note"] = (
            "PREMARKET: underlying exige frescura LIVE; cadena de opciones usa reloj estructural "
            "de sesión cerrada. No se exige flujo OPRA antes de 09:30 NY."
        )
    if declarado:
        out = dict(out)
        out["replay_declarado_sin_autoridad"] = True
        out["motivo"] = ("meta declara REPLAY sin autoridad de contexto: se aplica frescura "
                         f"de reloj ({out.get('motivo')})")
    return out

def publication_allowed(report: Any) -> bool:
    """Fail-closed helper used by Scanner, Sophia and presentation surfaces."""
    try:
        circuit = (report or {}).get("circuito_frescura") if isinstance(report, dict) else None
        return bool(isinstance(circuit, dict) and circuit.get("publicar_permitido") is True)
    except Exception as exc:
        _obs_note("freshness:publication_gate_exception", exc, severity="CRITICAL_DATA")
        return False

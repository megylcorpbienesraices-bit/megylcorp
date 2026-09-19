"""Autoridad por métrica: quién manda, quién valida, qué pasa si falta (v1.42).

EL CAMBIO DE DOCTRINA
---------------------
Hasta v1.41, `provider_parity.fuse_values` promediaba con pesos de calidad cualquier
campo numérico cuando dos proveedores estaban «suficientemente cerca»:

    X = (w_A·X_A + w_Q·X_Q) / (w_A + w_Q)

Esa media protege contra el ruido cuando las dos observaciones son el MISMO hecho
—dos cotizaciones del mismo instrumento en el mismo instante—. Para todo lo demás
es una operación sin significado. El GEX de Quant Data y el GEX de ITM no son dos
medidas ruidosas de una misma cantidad: son dos CONSTRUCCIONES distintas, con su
propio universo de vencimientos, su propia hipótesis de posicionamiento de dealer y
posiblemente otra representación de unidades. Promediarlas produce un número que no
describe el libro de nadie, y además borra la señal más valiosa que hay: que difieren.

LA REGLA DE v1.42
-----------------
Cada métrica declara tres cosas:

    AUTHORITY  — quién la produce. Es la respuesta publicada.
    VALIDATION — quién puede corroborarla. Nunca la modifica; sólo la contrasta.
    FALLBACK   — qué se hace si la autoridad no está: fallar, degradar o declarar
                 no disponible. Nunca «usar al otro como si fuera lo mismo».

La fusión numérica queda permitida sólo con un contrato explícito de fusión, y ese
contrato exige demostrar antes que ambas observaciones son el mismo fenómeno
(mismo instrumento, ventana temporal, unidades, mercado y semántica).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from .quant_errors import ProviderUnavailable

# ── política de ausencia ────────────────────────────────────────────────────────
FALLBACK_FAIL = "FAIL"                  # ruta crítica: sin autoridad no hay número
FALLBACK_UNAVAILABLE = "UNAVAILABLE"    # se publica «no disponible» y se sigue
FALLBACK_DEGRADED = "DEGRADED"          # se publica con etiqueta de degradación
FALLBACK_PEER = "PEER_IF_POLICY_ALLOWS" # sólo si la política de la métrica lo permite

# ── tipo de verdad ──────────────────────────────────────────────────────────────
KIND_OBSERVED = "OBSERVED"    # un hecho de mercado que alguien publicó
KIND_DERIVED = "DERIVED"      # calculado de forma determinista desde observaciones
KIND_INFERRED = "INFERRED"    # estimado con un modelo con incertidumbre

ITM = "ITM"          # el propio motor
ALPACA = "ALPACA"
QUANTDATA = "QUANTDATA"


@dataclass(frozen=True)
class MetricPolicy:
    """Contrato de una métrica. `fusion_contract` es la única puerta a promediar."""

    metric: str
    authority: str
    kind: str
    validation: Tuple[str, ...] = ()
    fallback: str = FALLBACK_UNAVAILABLE
    unit: Optional[str] = None
    fusion_contract: Optional[str] = None   # None = prohibido fusionar
    note: str = ""

    def describe(self) -> Dict[str, Any]:
        return {"metric": self.metric, "authority": self.authority, "kind": self.kind,
                "validation": list(self.validation), "fallback": self.fallback,
                "unit": self.unit, "fusion_allowed": self.fusion_contract is not None,
                "fusion_contract": self.fusion_contract, "note": self.note}


# ── el registro ─────────────────────────────────────────────────────────────────
# Sólo `underlying_price` y `option_quote` admiten fusión, y con contrato: son dos
# lecturas del MISMO instrumento en la misma ventana. Todo lo demás, no.
_POLICIES: Dict[str, MetricPolicy] = {p.metric: p for p in (
    MetricPolicy("underlying_price", ALPACA, KIND_OBSERVED, (QUANTDATA,), FALLBACK_FAIL,
                 unit="USD", fusion_contract="SAME_INSTRUMENT_SAME_WINDOW_SAME_UNITS",
                 note="Alpaca SIP es la autoridad de precio. Quant Data corrobora."),
    MetricPolicy("option_quote", ALPACA, KIND_OBSERVED, (), FALLBACK_FAIL,
                 unit="USD", fusion_contract="SAME_CONTRACT_SAME_NBBO_WINDOW",
                 note="OPRA vía Alpaca. Sin bid/ask no hay valoración honesta."),
    MetricPolicy("option_chain", ALPACA, KIND_OBSERVED, (QUANTDATA,), FALLBACK_FAIL,
                 note="La cadena la sirve quien tiene los contratos, no quien los resume."),
    MetricPolicy("option_trades", ALPACA, KIND_OBSERVED, (QUANTDATA,), FALLBACK_DEGRADED,
                 note="Cinta OPRA. Quant Data agrega, no reemplaza el print."),
    MetricPolicy("open_interest", QUANTDATA, KIND_OBSERVED, (ALPACA, ITM), FALLBACK_DEGRADED,
                 note="OI es de cierre de sesión anterior; nunca tick-by-tick, y nunca "
                      "reconstruido con volumen. Open Interest by Strike del proveedor "
                      "es la autoridad; la cadena propia corrobora y respalda."),
    MetricPolicy("implied_volatility", ITM, KIND_DERIVED, (ALPACA, QUANTDATA), FALLBACK_UNAVAILABLE,
                 note="Se resuelve con el precio observado y se contrasta con la IV del proveedor."),
    MetricPolicy("gamma", ITM, KIND_DERIVED, (ALPACA, QUANTDATA), FALLBACK_FAIL,
                 note="Una sola Gamma en todo el programa, la del dispatcher."),
    MetricPolicy("delta", ITM, KIND_DERIVED, (ALPACA, QUANTDATA), FALLBACK_FAIL),
    # v1.43.0 · GEX y DEX cambian de autoridad. Hasta v1.42.7 la autoridad era ITM
    # y Quant Data sólo validaba, de modo que el cálculo propio podía tapar el del
    # proveedor sin que nada lo dijera. Ahora la autoridad es QUANT DATA, el cálculo
    # propio pasa a VALIDACIÓN, y la política de ausencia es DEGRADED: sin
    # proveedor se publica la lectura propia MARCADA como degradada, nunca como si
    # fuera la del proveedor. Sigue prohibido promediarlas: son construcciones
    # distintas, y que difieran es la señal, no el problema.
    MetricPolicy("gex", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_DEGRADED,
                 unit="GEX_PER_1PCT",
                 note="Exposure by Strike · GAMMA es la autoridad. El perfil propio "
                      "corrobora y respalda declarado, nunca en silencio."),
    MetricPolicy("dex", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_DEGRADED,
                 unit="DEX_NOTIONAL",
                 note="Exposure by Strike · DELTA es la autoridad."),
    MetricPolicy("vex", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_DEGRADED,
                 unit="VANNA_RAW", note="Exposure by Strike · VANNA."),
    MetricPolicy("chex", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_DEGRADED,
                 unit="CHARM_RAW", note="Exposure by Strike · CHARM."),
    MetricPolicy("interval_map", QUANTDATA, KIND_OBSERVED, (ITM,), FALLBACK_DEGRADED,
                 note="Mapa tiempo × strike. Es el fondo dinámico de TRACE."),
    MetricPolicy("option_greeks", QUANTDATA, KIND_OBSERVED, (ITM,), FALLBACK_DEGRADED,
                 note="Delta, Gamma, Theta, Vega, Rho y los de orden superior los "
                      "publica el proveedor por contrato. ITM los usa como entrada; "
                      "no los recalcula cuando ya vienen válidos."),
    MetricPolicy("net_flow", QUANTDATA, KIND_OBSERVED, (), FALLBACK_UNAVAILABLE,
                 note="Prima neta por intervalo. Serie base de QFLOW."),
    MetricPolicy("order_flow", QUANTDATA, KIND_OBSERVED, (ITM,), FALLBACK_DEGRADED,
                 note="La cinta de opciones del proveedor, con el contrato entero."),
    MetricPolicy("dark_flow", QUANTDATA, KIND_OBSERVED, (ITM,), FALLBACK_DEGRADED,
                 note="Fuente principal de Dark Pool. La clasificación por venue es "
                      "auditoría, no la única vía."),
    MetricPolicy("dark_pool_levels", QUANTDATA, KIND_OBSERVED, (ITM,), FALLBACK_DEGRADED),
    MetricPolicy("iv_rank", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_DEGRADED, unit="PCT"),
    MetricPolicy("volatility_skew", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_DEGRADED),
    MetricPolicy("term_structure", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_DEGRADED),
    MetricPolicy("max_pain", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_DEGRADED, unit="STRIKE"),
    # ── inteligencia propia · nunca se presenta como dato del proveedor ────────
    MetricPolicy("itmq_gamma_pressure", ITM, KIND_INFERRED, (), FALLBACK_UNAVAILABLE,
                 note="Conclusión propia sobre datos del proveedor. DERIVED, no dato directo."),
    MetricPolicy("itmq_flow_confluence", ITM, KIND_INFERRED, (), FALLBACK_UNAVAILABLE),
    MetricPolicy("itmq_structural_score", ITM, KIND_INFERRED, (), FALLBACK_UNAVAILABLE),
    MetricPolicy("quantdata_gex", QUANTDATA, KIND_DERIVED, (ITM,), FALLBACK_UNAVAILABLE,
                 note="El GEX de Quant Data es SUYO. Se publica con su nombre o no se publica."),
    MetricPolicy("net_drift", QUANTDATA, KIND_OBSERVED, (), FALLBACK_UNAVAILABLE,
                 note="Net Drift es una métrica propia de Quant Data (prima y volumen "
                      "call/put agregados por bucket). ITM no tiene un Net Drift."),
    MetricPolicy("dark_pool_prints", ALPACA, KIND_OBSERVED, (QUANTDATA,), FALLBACK_UNAVAILABLE,
                 note="Prints fuera de bolsa identificados por condición/exchange."),
    MetricPolicy("flow_aggression", ITM, KIND_INFERRED, (QUANTDATA,), FALLBACK_DEGRADED,
                 note="Clasificación probabilística del agresor, no un hecho observado."),
    MetricPolicy("dealer_state", ITM, KIND_INFERRED, (QUANTDATA,), FALLBACK_DEGRADED,
                 note="Inferencia con incertidumbre explícita. Nunca inventario observado."),
    MetricPolicy("scanner_direction", ITM, KIND_INFERRED, (), FALLBACK_FAIL,
                 note="La dirección NUNCA viene de un proveedor. Autoridad única del Scanner."),
    MetricPolicy("macro_series", "FRED", KIND_OBSERVED, (), FALLBACK_UNAVAILABLE),
)}


def policy(metric: str) -> MetricPolicy:
    key = str(metric or "").strip().lower()
    if key not in _POLICIES:
        # Una métrica sin política es una métrica sin dueño: se trata como inferida,
        # de ITM y no fusionable. Es el default conservador, no un permiso.
        return MetricPolicy(key or "unknown", ITM, KIND_INFERRED, (), FALLBACK_UNAVAILABLE,
                            note="Métrica sin política declarada; se aplica el default estricto.")
    return _POLICIES[key]


def registered_metrics() -> Tuple[str, ...]:
    return tuple(sorted(_POLICIES))


def fusion_allowed(metric: str) -> bool:
    return policy(metric).fusion_contract is not None


def _norm(name: Any) -> str:
    return str(name or "").strip().upper()


def resolve(metric: str, observations: Iterable[Dict[str, Any]], *,
            peer_enabled=None) -> Dict[str, Any]:
    """Aplica la política de la métrica sobre las observaciones disponibles.

    `observations` son dicts con al menos `provider` y `value`; opcionalmente
    `quality_score`, `age_seconds` y `usable`. El retorno dice qué se publica, de
    quién, y qué dijeron los validadores, SIN mezclar sus números.
    """
    pol = policy(metric)
    rows = [dict(o) for o in (observations or []) if isinstance(o, dict)]
    for r in rows:
        r["provider"] = _norm(r.get("provider"))
    if peer_enabled is not None:
        for r in rows:
            if r["provider"] in (ALPACA, QUANTDATA) and not peer_enabled(r["provider"]):
                r["usable"] = False
                r["excluded_reason"] = "fuera del roster activo"

    def _usable(r: Dict[str, Any]) -> bool:
        return bool(r.get("usable", True)) and r.get("value") is not None

    authority_rows = [r for r in rows if r["provider"] == _norm(pol.authority) and _usable(r)]
    validators = [r for r in rows
                  if r["provider"] in {_norm(v) for v in pol.validation} and _usable(r)]

    out: Dict[str, Any] = {
        "metric": pol.metric, "policy": pol.describe(),
        "validators": [{"provider": r["provider"], "value": r.get("value"),
                        "quality": r.get("quality_score"), "age_seconds": r.get("age_seconds")}
                       for r in validators],
        "fusion_applied": False,
    }

    if authority_rows:
        best = max(authority_rows, key=lambda r: float(r.get("quality_score") or 0.0))
        out.update(ready=True, status="AUTHORITY", value=best.get("value"),
                   source=best["provider"], kind=pol.kind,
                   note=pol.note or "Publicado por su autoridad declarada.")
        return out

    # Sin autoridad. Aquí es donde v1.41 habría promediado al par.
    if pol.fallback == FALLBACK_FAIL:
        raise ProviderUnavailable(pol.authority,
                                  f"es la autoridad de «{pol.metric}» y no hay observación utilizable",
                                  metric=pol.metric)
    if pol.fallback == FALLBACK_PEER and validators:
        best = max(validators, key=lambda r: float(r.get("quality_score") or 0.0))
        out.update(ready=True, status="PEER_BY_POLICY", value=best.get("value"),
                   source=best["provider"], kind=pol.kind,
                   note=f"La política de «{pol.metric}» autoriza expresamente al par como respaldo.")
        return out
    if pol.fallback == FALLBACK_DEGRADED and validators:
        best = max(validators, key=lambda r: float(r.get("quality_score") or 0.0))
        out.update(ready=True, status="DEGRADED", value=best.get("value"),
                   source=best["provider"], kind=pol.kind,
                   note=(f"{pol.authority} no está disponible. Se publica la observación de "
                         f"{best['provider']} marcada como degradada, no como equivalente."))
        return out

    out.update(ready=False, status="UNAVAILABLE", value=None, source=None, kind=pol.kind,
               note=(f"{pol.authority} es la autoridad de «{pol.metric}» y no está disponible. "
                     "No se sustituye por otra métrica distinta con nombre parecido."))
    return out


def fuse(metric: str, observations: Iterable[Dict[str, Any]], *,
         same_phenomenon: bool, evidence: Dict[str, Any] | None = None,
         **fuse_kwargs: Any) -> Dict[str, Any]:
    """Única entrada autorizada a una media entre proveedores.

    Exige DOS cosas a la vez: que la métrica tenga contrato de fusión y que el
    llamador afirme, con evidencia, haber comprobado que ambas observaciones son
    el mismo fenómeno. Si falta cualquiera, no se promedia y se dice por qué.
    """
    pol = policy(metric)
    if pol.fusion_contract is None:
        return {"ready": False, "status": "FUSION_FORBIDDEN", "metric": pol.metric,
                "policy": pol.describe(),
                "reason": (f"«{pol.metric}» no tiene contrato de fusión: su autoridad es "
                           f"{pol.authority} y promediarla con otra fuente produciría un "
                           "número que no describe ningún mercado real.")}
    if not same_phenomenon:
        return {"ready": False, "status": "NOT_COMPARABLE", "metric": pol.metric,
                "policy": pol.describe(), "evidence": dict(evidence or {}),
                "reason": "No se demostró que las observaciones describan el mismo fenómeno."}

    from .provider_parity import fuse_values
    out = dict(fuse_values(observations, fuse_kwargs.pop("field", "value"), **fuse_kwargs))
    out.update(metric=pol.metric, policy=pol.describe(), fusion_applied=True,
               fusion_contract=pol.fusion_contract, evidence=dict(evidence or {}))
    return out


def authority_map() -> Dict[str, Any]:
    """Tabla publicable: qué hace cada proveedor, métrica a métrica."""
    by_provider: Dict[str, Dict[str, list]] = {}
    for pol in _POLICIES.values():
        by_provider.setdefault(_norm(pol.authority), {"authority": [], "validation": []})
        by_provider[_norm(pol.authority)]["authority"].append(pol.metric)
        for v in pol.validation:
            by_provider.setdefault(_norm(v), {"authority": [], "validation": []})
            by_provider[_norm(v)]["validation"].append(pol.metric)
    for rows in by_provider.values():
        rows["authority"].sort()
        rows["validation"].sort()
    return {
        "policies": {m: p.describe() for m, p in sorted(_POLICIES.items())},
        "by_provider": by_provider,
        "doctrine": ("AUTHORITY · VALIDATION · FALLBACK. No se promedian conceptos: "
                     "la fusión numérica requiere contrato explícito y prueba de que "
                     "ambas observaciones son el mismo fenómeno."),
        "fusable": sorted(m for m, p in _POLICIES.items() if p.fusion_contract),
    }

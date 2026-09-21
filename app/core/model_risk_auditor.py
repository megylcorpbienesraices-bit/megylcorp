"""Auditor de riesgo de modelo y calidad de datos (v1.42).

QUÉ DEJA DE SER
---------------
«API conectada» no es una auditoría. Un proveedor puede estar perfectamente
conectado y entregar una cadena con el bid por encima del ask, un OI de hace tres
sesiones, una IV no identificable en la mitad de los contratos y una superficie con
arbitraje de mariposa. Todo eso produce números. Ninguno sirve para operar.

QUÉ ES
------
Una batería de invariantes. Cada uno es una afirmación que DEBE ser cierta para que
la cifra publicada signifique lo que dice, y cada uno se comprueba con los datos del
ciclo, no con una configuración.

    COTIZACIÓN    bid ≤ ask · no rancia · dos lados
    CONTRATO      multiplicador válido · vencimiento válido · deliverable conocido
    VALORACIÓN    dentro de las cotas de no arbitraje
    IV            vega identificable
    GREEKS        valores finitos
    SUPERFICIE    sin arbitraje de mariposa · sin arbitraje de calendario
    OI            fecha efectiva conocida
    EXPOSICIÓN    unidad declarada
    PROVEEDORES   misma semántica antes de comparar
    REPLAY        tiempo de evento ≤ corte
    FLUJO         quote causal anterior al trade
    SCANNER       entradas frescas
    EV            costes conocidos
    CALIBRACIÓN   OOS válido
    DEALER        etiquetado como inferencia

LA SALIDA
---------
Internamente puede haber doscientos controles. Hacia fuera, tres líneas:

    CALIDAD DE MODELO     BUENA
    CALIDAD DE DATOS      BUENA
    CALIDAD DE DECISIÓN   ACCIONABLE

El detalle está disponible, pero sólo se abre cuando algo falla. Una pantalla llena
de mensajes verdes entrena a no leerlos.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from . import units_registry
from .metric_authority import authority_map

SEVERITY_INFO = "INFO"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"

#: v1.57.0 · NO APLICA, CON MOTIVO. No es un aprobado ni un aviso.
#:
#: Faltaba el estado del medio y eso obligaba a mentir en las dos direcciones.
#: Un control cuyo objeto NO EXISTE todavía —auditar el purge gap de una
#: validación fuera de muestra que aún no se ha hecho— no puede avisar: estaría
#: denunciando un defecto metodológico que no hay, y quien lo lea se pondrá a
#: buscar un fallo inexistente. Pero tampoco puede aprobar: aprobar lo que no se
#: ha comprobado es exactamente lo que estos controles existen para impedir.
#:
#: `N/A` obliga a decir POR QUÉ no aplica. Un N/A sin motivo es un aviso
#: escondido, y se rechaza en las pruebas.
SEVERITY_NA = "N/A"

AREA_QUOTE = "COTIZACIÓN"
AREA_CONTRACT = "CONTRATO"
AREA_PRICING = "VALORACIÓN"
AREA_IV = "IV"
AREA_GREEKS = "GREEKS"
AREA_SURFACE = "SUPERFICIE"
AREA_OI = "OI"
AREA_EXPOSURE = "EXPOSICIÓN"
AREA_PROVIDERS = "PROVEEDORES"
AREA_REPLAY = "REPLAY"
AREA_FLOW = "FLUJO"
AREA_SCANNER = "SCANNER"
AREA_EV = "EV"
AREA_CALIBRATION = "CALIBRACIÓN"
AREA_DEALER = "DEALER"

# A qué dimensión afecta cada área. Un problema de superficie es riesgo de MODELO;
# un bid cruzado es calidad de DATOS. Mezclarlos impide saber qué hay que arreglar.
_DIMENSION = {
    AREA_QUOTE: "DATA", AREA_CONTRACT: "DATA", AREA_OI: "DATA", AREA_PROVIDERS: "DATA",
    AREA_REPLAY: "DATA", AREA_FLOW: "DATA",
    AREA_PRICING: "MODEL", AREA_IV: "MODEL", AREA_GREEKS: "MODEL", AREA_SURFACE: "MODEL",
    AREA_EXPOSURE: "MODEL", AREA_CALIBRATION: "MODEL", AREA_DEALER: "MODEL",
    AREA_SCANNER: "DECISION", AREA_EV: "DECISION",
}


@dataclass(frozen=True)
class Finding:
    area: str
    invariant: str
    passed: bool
    severity: str
    detail: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {"area": self.area, "invariant": self.invariant, "passed": self.passed,
                "severity": self.severity, "detail": self.detail,
                "dimension": _DIMENSION.get(self.area, "MODEL"), "evidence": self.evidence}


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _ok(area, inv, detail="", **ev) -> Finding:
    return Finding(area, inv, True, SEVERITY_INFO, detail or "correcto", ev)


def _fail(area, inv, detail, severity=SEVERITY_WARN, **ev) -> Finding:
    return Finding(area, inv, False, severity, detail, ev)


def _na(area, inv, motivo: str, **ev) -> Finding:
    """El control no aplica en este ciclo, y se dice por qué.

    `passed=True` a propósito: no arrastra la nota del modelo hacia abajo,
    porque no hay defecto. Pero su severidad es `N/A`, así que tampoco se
    confunde con un control que SÍ se ejecutó y pasó.
    """
    if not str(motivo or "").strip():
        raise ValueError("un N/A sin motivo es un aviso escondido")
    return Finding(area, inv, True, SEVERITY_NA, str(motivo), ev)


# ── controles sobre la cadena ───────────────────────────────────────────────────

def audit_chain(chain: pd.DataFrame, *, symbol: Any = None,
                max_quote_age_s: float = 30.0) -> List[Finding]:
    out: List[Finding] = []
    if chain is None or len(chain) == 0:
        return [_fail(AREA_CONTRACT, "cadena presente", "no hay cadena que auditar",
                      SEVERITY_CRITICAL)]
    n = len(chain)
    cols = set(chain.columns)

    # COTIZACIÓN · bid ≤ ask
    if {"bid", "ask"} <= cols:
        b = pd.to_numeric(chain["bid"], errors="coerce")
        a = pd.to_numeric(chain["ask"], errors="coerce")
        both = b.notna() & a.notna()
        crossed = int((both & (a < b)).sum())
        locked = int((both & (a == b) & (b > 0)).sum())
        out.append(_ok(AREA_QUOTE, "bid ≤ ask", f"{n - crossed}/{n} correctas")
                   if crossed == 0 else
                   _fail(AREA_QUOTE, "bid ≤ ask", f"{crossed} contratos con el mercado cruzado",
                         SEVERITY_CRITICAL, crossed=crossed))
        if locked:
            out.append(_fail(AREA_QUOTE, "mercado no bloqueado",
                             f"{locked} contratos con bid = ask", SEVERITY_WARN, locked=locked))
        two_sided = int((both & (b > 0) & (a > 0)).sum())
        pct = 100.0 * two_sided / n
        out.append(_ok(AREA_QUOTE, "cotización de dos lados", f"{pct:.1f}% con bid y ask")
                   if pct >= 60.0 else
                   _fail(AREA_QUOTE, "cotización de dos lados",
                         f"sólo {pct:.1f}% de la cadena tiene dos lados; sin ellos no hay "
                         "valoración honesta ni ejecución simulable", SEVERITY_WARN,
                         two_sided_pct=round(pct, 2)))
    else:
        out.append(_fail(AREA_QUOTE, "cotización presente",
                         "la cadena no trae bid/ask", SEVERITY_CRITICAL))

    # COTIZACIÓN · frescura
    if "quote_timestamp" in cols or "timestamp" in cols:
        col = "quote_timestamp" if "quote_timestamp" in cols else "timestamp"
        ts = pd.to_datetime(chain[col], errors="coerce", utc=True)
        if ts.notna().any():
            now = pd.Timestamp.now(tz="UTC")
            age = (now - ts.max()).total_seconds()
            out.append(_ok(AREA_QUOTE, "cotización fresca", f"{age:.1f}s de antigüedad")
                       if age <= max_quote_age_s else
                       _fail(AREA_QUOTE, "cotización fresca",
                             f"la observación más reciente tiene {age:.0f}s "
                             f"(límite {max_quote_age_s:.0f}s)", SEVERITY_WARN,
                             age_seconds=round(age, 1)))

    # CONTRATO · multiplicador y vencimiento
    from .contract_spec import multiplier_series
    mult = multiplier_series(chain, symbol)
    bad_mult = int(((mult <= 0) | ~np.isfinite(mult)).sum())
    out.append(_ok(AREA_CONTRACT, "multiplicador válido", f"{n - bad_mult}/{n} resueltos")
               if bad_mult == 0 else
               _fail(AREA_CONTRACT, "multiplicador válido",
                     f"{bad_mult} contratos sin tamaño económico resoluble", SEVERITY_CRITICAL,
                     invalid=bad_mult))
    distinct = sorted(set(round(float(x), 6) for x in mult.unique() if math.isfinite(float(x))))
    if len(distinct) > 1:
        out.append(_fail(AREA_CONTRACT, "deliverable homogéneo",
                         f"la cadena mezcla multiplicadores {distinct}: hay contratos ajustados, "
                         "y sus cifras monetarias no son comparables 1:1 con los estándar",
                         SEVERITY_WARN, multipliers=distinct))

    if "expiration_date" in cols:
        exp = pd.to_datetime(chain["expiration_date"], errors="coerce")
        bad = int(exp.isna().sum())
        out.append(_ok(AREA_CONTRACT, "vencimiento válido", f"{n - bad}/{n}")
                   if bad == 0 else
                   _fail(AREA_CONTRACT, "vencimiento válido",
                         f"{bad} contratos con vencimiento ilegible", SEVERITY_CRITICAL, invalid=bad))

    # GREEKS finitos
    greek_cols = [c for c in ("calc_delta", "calc_gamma", "calc_vanna", "calc_charm") if c in cols]
    if greek_cols:
        bad = 0
        for c in greek_cols:
            v = pd.to_numeric(chain[c], errors="coerce")
            bad += int((~np.isfinite(v.to_numpy(float))).sum())
        total = n * len(greek_cols)
        pct = 100.0 * bad / max(total, 1)
        out.append(_ok(AREA_GREEKS, "Greeks finitos", f"{100.0 - pct:.1f}% finitos")
                   if pct <= 2.0 else
                   _fail(AREA_GREEKS, "Greeks finitos",
                         f"{pct:.1f}% de valores no finitos en {greek_cols}", SEVERITY_WARN,
                         non_finite_pct=round(pct, 2)))

    # OI con fecha efectiva
    if "open_interest" in cols:
        has_date = any(c in cols for c in ("oi_effective_date", "open_interest_date", "oi_asof"))
        out.append(_ok(AREA_OI, "fecha efectiva de OI", "declarada")
                   if has_date else
                   _fail(AREA_OI, "fecha efectiva de OI",
                         "el OI se publica sin la sesión a la que corresponde. El OI no es "
                         "tick-by-tick: sin su fecha no se puede distinguir 'estable' de 'viejo'",
                         SEVERITY_WARN))
    return out


def audit_iv(assessments: Sequence[Any], *, min_identifiable_pct: float = 55.0,
             reason: str = "") -> List[Finding]:
    # v1.57.0 · «no se evaluó» sugería que el motor no existía. Existe
    # —`iv_quality.assess()` y `chain_quality()`—, lo que faltaba era el cable.
    # Sin cadena en este ciclo no hay identificabilidad que medir: eso es N/A
    # con su motivo, no un aviso que manda a implementar lo ya implementado.
    if not assessments:
        return [_na(AREA_IV, "IV evaluada",
                    reason or ("no hay cadena de opciones en este ciclo: no hay "
                               "contratos cuya IV evaluar"))]
    from .iv_quality import chain_quality
    q = chain_quality(assessments)
    if not q.get("ready"):
        return [_fail(AREA_IV, "IV evaluada", str(q.get("reason")), SEVERITY_WARN)]
    pct = float(q["identifiable_pct"])
    f = (_ok(AREA_IV, "vega identificable", f"{pct:.1f}% de la cadena es identificable")
         if pct >= min_identifiable_pct else
         _fail(AREA_IV, "vega identificable",
               f"sólo {pct:.1f}% de los contratos tienen IV identificable; ajustar la "
               "superficie con el resto sería ajustar contra ruido", SEVERITY_WARN,
               identifiable_pct=pct))
    return [f, _ok(AREA_IV, "estados declarados", str(q.get("states")))]


def audit_surface(surface: Dict[str, Any] | None) -> List[Finding]:
    # v1.57.0 · Igual que IV: `ssvi_shadow.fit_ssvi()` existe con sus
    # condiciones de Durrleman y su monotonía de calendario. Cuando la cadena
    # del ciclo no da para ajustar —hacen falta dos vencimientos con cuatro
    # strikes— eso es una condición del DATO, y se dice cuál.
    if not surface:
        return [_na(AREA_SURFACE, "superficie ajustada",
                    "no se entregó ningún ajuste de superficie a este ciclo")]
    if surface.get("ready") is False:
        return [_na(AREA_SURFACE, "superficie ajustada",
                    str(surface.get("reason")
                        or "la cadena de este ciclo no permite ajustar la superficie"),
                    model=surface.get("model"),
                    slices_available=surface.get("slices_available"))]
    out: List[Finding] = []
    slices = surface.get("slices") or surface.get("per_slice") or []
    bf_bad = [s for s in slices if isinstance(s, dict)
              and (s.get("butterfly_state") not in (None, "OK", "PASS")
                   or (s.get("arbitrage_checks") or {}).get("pass") is False)]
    out.append(_ok(AREA_SURFACE, "sin arbitraje de mariposa", f"{len(slices)} cortes verificados")
               if not bf_bad else
               _fail(AREA_SURFACE, "sin arbitraje de mariposa",
                     f"{len(bf_bad)} cortes violan Durrleman: la densidad implícita es negativa "
                     "en algún strike, y eso no es una superficie de precios",
                     SEVERITY_CRITICAL, slices=len(bf_bad)))
    cal = surface.get("calendar") or surface.get("calendar_diagnostics") or {}
    if cal:
        bad = cal.get("violations") or (0 if cal.get("pass", True) else 1)
        out.append(_ok(AREA_SURFACE, "sin arbitraje de calendario", "varianza total no decreciente")
                   if not bad else
                   _fail(AREA_SURFACE, "sin arbitraje de calendario",
                         f"{bad} violaciones: un vencimiento más largo con menos varianza total "
                         "implica un arbitraje de calendario", SEVERITY_CRITICAL, violations=bad))
    return out


def audit_exposure(quantities: Iterable[Any]) -> List[Finding]:
    qs = [q for q in (quantities or []) if q is not None]
    if not qs:
        return [_fail(AREA_EXPOSURE, "unidad declarada",
                      "no se publicó ninguna exposición con unidad declarada", SEVERITY_WARN)]
    unknown = [getattr(q, "unit", None) for q in qs
               if getattr(q, "unit", None) not in units_registry.UNITS]
    return [_ok(AREA_EXPOSURE, "unidad declarada", f"{len(qs)} magnitudes con unidad canónica")
            if not unknown else
            _fail(AREA_EXPOSURE, "unidad declarada",
                  f"unidades no registradas: {unknown}", SEVERITY_CRITICAL, unknown=unknown)]


def audit_provider_comparison(comparisons: Iterable[Dict[str, Any]]) -> List[Finding]:
    rows = list(comparisons or [])
    if not rows:
        return [_ok(AREA_PROVIDERS, "semántica antes de comparar",
                    "no se comparó ninguna métrica entre proveedores en este ciclo")]
    bad = [r for r in rows if r.get("comparable") is False]
    forced = [r for r in rows if r.get("fusion_applied") and not r.get("fusion_contract")]
    out = [_ok(AREA_PROVIDERS, "semántica antes de comparar",
               f"{len(rows) - len(bad)}/{len(rows)} comparaciones válidas")
           if not bad else
           _fail(AREA_PROVIDERS, "semántica antes de comparar",
                 f"{len(bad)} comparaciones declaradas NOT_COMPARABLE; publicadas como tales "
                 "en vez de como conflicto o como media", SEVERITY_INFO, not_comparable=len(bad))]
    if forced:
        out.append(_fail(AREA_PROVIDERS, "sin fusión sin contrato",
                         f"{len(forced)} métricas se fusionaron sin contrato de fusión",
                         SEVERITY_CRITICAL, offenders=len(forced)))
    return out


def audit_replay(replay: Dict[str, Any] | None) -> List[Finding]:
    if not replay or not replay.get("active"):
        return [_ok(AREA_REPLAY, "tiempo de evento ≤ corte", "replay inactivo")]
    cutoff = replay.get("cutoff") or replay.get("asof")
    latest = replay.get("latest_event") or replay.get("max_event_time")
    if cutoff is None or latest is None:
        return [_fail(AREA_REPLAY, "tiempo de evento ≤ corte",
                      "el replay no declara su corte temporal", SEVERITY_CRITICAL)]
    c = pd.to_datetime(cutoff, errors="coerce")
    l = pd.to_datetime(latest, errors="coerce")
    if pd.isna(c) or pd.isna(l):
        return [_fail(AREA_REPLAY, "tiempo de evento ≤ corte",
                      "corte o evento no interpretables", SEVERITY_CRITICAL)]
    return [_ok(AREA_REPLAY, "tiempo de evento ≤ corte", "sin fuga de futuro")
            if l <= c else
            _fail(AREA_REPLAY, "tiempo de evento ≤ corte",
                  f"el replay expone eventos posteriores al corte ({l} > {c}): eso es mirar "
                  "hacia adelante y invalida cualquier conclusión del backtest",
                  SEVERITY_CRITICAL)]


def audit_flow(flow: Dict[str, Any] | None) -> List[Finding]:
    if not flow:
        return [_fail(AREA_FLOW, "quote causal anterior al trade",
                      "no hay evidencia de flujo en este ciclo", SEVERITY_INFO)]
    out: List[Finding] = []
    non_causal = int(flow.get("non_causal_quotes") or 0)
    out.append(_ok(AREA_FLOW, "quote causal anterior al trade", "todas las quotes son previas")
               if non_causal == 0 else
               _fail(AREA_FLOW, "quote causal anterior al trade",
                     f"{non_causal} prints emparejados con una cotización POSTERIOR: la "
                     "clasificación del agresor usaría información que aún no existía",
                     SEVERITY_CRITICAL, offenders=non_causal))
    pct = _f(flow.get("classified_pct"))
    if pct is not None:
        out.append(_ok(AREA_FLOW, "cinta clasificable", f"{pct:.1f}% clasificado")
                   if pct >= 40.0 else
                   _fail(AREA_FLOW, "cinta clasificable",
                         f"sólo {pct:.1f}% de la prima tiene agresor identificado; el sesgo "
                         "se calcula sobre esa parte y la otra se declara", SEVERITY_WARN,
                         classified_pct=pct))
    return out


def audit_decision(scanner: Dict[str, Any] | None, ev: Dict[str, Any] | None,
                   calibration: Dict[str, Any] | None,
                   dealer: Dict[str, Any] | None,
                   *, max_input_age_s: float = 120.0) -> List[Finding]:
    out: List[Finding] = []

    age = _f((scanner or {}).get("input_age_seconds") or (scanner or {}).get("data_age_seconds"))
    if scanner is None:
        out.append(_fail(AREA_SCANNER, "entradas frescas", "no hay lectura del Scanner",
                         SEVERITY_CRITICAL))
    elif age is None:
        out.append(_fail(AREA_SCANNER, "entradas frescas",
                         "el Scanner no declara la antigüedad de sus entradas", SEVERITY_WARN))
    else:
        out.append(_ok(AREA_SCANNER, "entradas frescas", f"{age:.0f}s")
                   if age <= max_input_age_s else
                   _fail(AREA_SCANNER, "entradas frescas",
                         f"las entradas del Scanner tienen {age:.0f}s (límite {max_input_age_s:.0f}s)",
                         SEVERITY_CRITICAL, age_seconds=age))

    if ev is None:
        # Sin EV publicado no hay costes que auditar. Avisar aquí denuncia un
        # «EV bruto» que no existe. Cuando SÍ hay EV y no declara costes, eso
        # sigue siendo CRÍTICO: un EV sin comisiones ni deslizamiento no es un EV.
        out.append(_na(AREA_EV, "costes conocidos",
                       "no se publica EV en este ciclo: no hay modelo de costes que auditar"))
    else:
        has_costs = any(k in ev for k in ("costs", "fees", "cost_model", "execution_costs"))
        out.append(_ok(AREA_EV, "costes conocidos", "modelo de costes declarado")
                   if has_costs else
                   _fail(AREA_EV, "costes conocidos",
                         "el EV no declara comisiones ni deslizamiento: un EV bruto no es un EV",
                         SEVERITY_CRITICAL))

    # v1.57.0 · TRES ESTADOS, NO DOS.
    #
    # Esto avisaba «la validación no declara purge gap» siempre que la
    # calibración no publicara la clave. Pero una calibración en COLLECTING no
    # tiene validación fuera de muestra TODAVÍA: no hay purge gap que declarar
    # porque no hay partición que purgar.
    #
    # Avisar ahí es un FALSO NEGATIVO que manda a buscar un defecto
    # metodológico inexistente —el motor parte TRAIN → purge → VALIDATION →
    # purge → FINAL OOS y lo publica en `method`—, y encima gasta la credibilidad
    # del aviso de verdad, que es el de una calibración LISTA sin purgar.
    if calibration is None:
        out.append(_na(AREA_CALIBRATION, "OOS válido",
                       "no hay calibración en este ciclo: no existe validación que auditar"))
    elif not calibration.get("ready"):
        estado = str(calibration.get("status") or calibration.get("stage") or "COLLECTING")
        out.append(_na(AREA_CALIBRATION, "OOS válido",
                       f"calibración en {estado}: todavía no hay partición fuera de muestra "
                       f"que purgar · método declarado: "
                       f"{str(calibration.get('method') or 'sin declarar')[:120]}",
                       status=estado))
    else:
        purged = bool(calibration.get("purged") or calibration.get("purge_gap")
                      or calibration.get("purged_sessions"))
        out.append(_ok(AREA_CALIBRATION, "OOS válido",
                       f"validación con purge gap · "
                       f"{calibration.get('purged_sessions')} sesión(es) purgada(s)")
                   if purged else
                   _fail(AREA_CALIBRATION, "OOS válido",
                         "la calibración está LISTA y no declara purge gap; sin él la muestra "
                         "'fuera de muestra' comparte información con el entrenamiento",
                         SEVERITY_WARN))

    if dealer is not None:
        labelled = str(dealer.get("kind") or "").upper() == "INFERRED"
        banded = all(k in dealer for k in ("p10", "median", "p90")) or "hedge_pressure" in dealer
        out.append(_ok(AREA_DEALER, "inferencia etiquetada", "publicado como inferencia con banda")
                   if (labelled and banded) else
                   _fail(AREA_DEALER, "inferencia etiquetada",
                         "el estado del dealer se publica sin etiquetarse como inferencia o sin "
                         "banda de incertidumbre; como cifra puntual invita a operarlo como si "
                         "fuera inventario observado", SEVERITY_WARN,
                         labelled=labelled, banded=banded))
    return out


# ── veredicto ───────────────────────────────────────────────────────────────────

def _grade(findings: Sequence[Finding], dimension: str) -> Dict[str, Any]:
    rows = [f for f in findings if _DIMENSION.get(f.area, "MODEL") == dimension]
    if not rows:
        return {"state": "SIN_EVIDENCIA", "checks": 0, "failed": 0, "critical": 0}
    failed = [f for f in rows if not f.passed]
    crit = [f for f in failed if f.severity == SEVERITY_CRITICAL]
    if crit:
        state = "DEGRADED" if dimension != "DECISION" else "NO_ACCIONABLE"
    elif failed:
        state = "FAIR" if dimension != "DECISION" else "ACCIONABLE_CON_RESERVAS"
    else:
        state = "GOOD" if dimension != "DECISION" else "ACCIONABLE"
    return {"state": state, "checks": len(rows), "failed": len(failed), "critical": len(crit),
            "blockers": [f.describe() for f in crit][:6]}


def audit(*, chain: pd.DataFrame | None = None, symbol: Any = None,
          iv_assessments: Sequence[Any] = (), surface: Dict[str, Any] | None = None,
          iv_reason: str = "",
          exposures: Iterable[Any] = (), comparisons: Iterable[Dict[str, Any]] = (),
          replay: Dict[str, Any] | None = None, flow: Dict[str, Any] | None = None,
          scanner: Dict[str, Any] | None = None, ev: Dict[str, Any] | None = None,
          calibration: Dict[str, Any] | None = None,
          dealer: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Ejecuta la batería completa y resume en tres líneas."""
    findings: List[Finding] = []
    if chain is not None:
        findings += audit_chain(chain, symbol=symbol)
    findings += audit_iv(iv_assessments, reason=str(iv_reason or ""))
    findings += audit_surface(surface)
    findings += audit_exposure(exposures)
    findings += audit_provider_comparison(comparisons)
    findings += audit_replay(replay)
    findings += audit_flow(flow)
    findings += audit_decision(scanner, ev, calibration, dealer)

    model = _grade(findings, "MODEL")
    data = _grade(findings, "DATA")
    decision = _grade(findings, "DECISION")

    # Una decisión no puede ser accionable sobre datos degradados, por mucho que sus
    # propios controles pasen: el Scanner puede estar fresco y leyendo una cadena rota.
    if data["state"] == "DEGRADED" and decision["state"] == "ACCIONABLE":
        decision["state"] = "ACCIONABLE_CON_RESERVAS"
        decision["note"] = ("Los controles de decisión pasan, pero se apoyan en datos "
                            "degradados. La frescura del Scanner no arregla una cadena rota.")

    failures = [f.describe() for f in findings if not f.passed]
    return {
        "ready": True,
        "headline": {"MODEL_QUALITY": model["state"], "DATA_QUALITY": data["state"],
                     "DECISION_QUALITY": decision["state"]},
        "model": model, "data": data, "decision": decision,
        "checks_run": len(findings),
        "failures": failures,
        "failures_by_area": {a: sum(1 for f in failures if f["area"] == a)
                             for a in sorted({f["area"] for f in failures})},
        "all_findings": [f.describe() for f in findings],
        "contracts": {"units": units_registry.registry_snapshot(),
                      "authority": authority_map()},
        "doctrine": ("Un control verde no se muestra; un control rojo sí. Una pantalla llena "
                     "de mensajes correctos entrena a no leerlos."),
    }

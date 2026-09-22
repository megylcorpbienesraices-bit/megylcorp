"""WALL ENGINE · autoridad única de Call Wall y Put Wall · ITM QUANT v1.44.0

EL PROBLEMA QUE CIERRA
----------------------
Hasta v1.43.0 había TRES llamadas a `structural_walls()` —`nextgen_terminal`
(niveles de TRACE), `key_levels_report` (RESUMEN) y `premarket_intelligence`— y
cada una le pasaba un frame distinto:

    nextgen_terminal      curagg del snapshot vivo
    key_levels_report     el mismo curagg, pero con `enriched` a veces ausente
    premarket             `agg` reconstruido con `signed_gex` renombrado

Mismo nombre, tres entradas, tres resultados posibles. El Call Wall de TRACE
podía no ser el Call Wall de RESUMEN, y nada en el programa lo detectaba. Un nivel
estructural que cambia según la pestaña no es un nivel: es un rumor.

LA ARQUITECTURA
---------------
    Quant Data ──► Data Hub ──► Wall Engine ──► Call Wall / Put Wall
                                     ▲
                        lógica estructural ITM QUANT

Una entrada (el Data Hub), un cálculo (este módulo), una salida que todas las
secciones consumen SIN recalcular.

QUÉ ES UN MURO Y POR QUÉ SE MIDE ASÍ
------------------------------------
Un muro responde: **si el precio llegara a ese strike, cuánta cobertura tendría
que ajustar el dealer allí**. Eso no es «dónde hay más gamma ahora», que es una
pregunta distinta y sesgada hacia el dinero.

Se combinan dos evidencias que el proveedor entrega por separado:

  * **Exposición por strike** (`exposure-by-strike`, GAMMA) — la magnitud de la
    cobertura. Sin ella no hay muro, sólo contratos.
  * **Interés abierto por strike** (`open-interest-by-strike`) — cuántos contratos
    sostienen esa exposición. Sin él, un strike con exposición calculada pero sin
    libro detrás puntúa igual que uno con cien mil contratos abiertos.

El score es la **media geométrica** de las dos normalizadas, no la suma: la suma
deja pasar a los que sólo destacan en una cosa, y un muro que sólo existe en una
de las dos evidencias no es un muro.

Cuando el proveedor no publica OI para ese activo **no se penaliza al strike**: se
puntúa sólo con exposición y se declara (`oi_available: false`). Multiplicar por
un dato ausente es inventar un veredicto.

REGLA DE LADO
-------------
El Call Wall se busca SÓLO por encima del precio y con exposición de CALLS; el Put
Wall sólo por debajo y con exposición de PUTS. Un muro de calls por debajo del
precio ya fue atravesado y no es resistencia: es historia.

HISTÉRESIS
----------
Un muro que salta de strike en cada refresco es inútil para operar. El muro
vigente sólo se sustituye cuando el candidato nuevo supera al actual por
`HYSTERESIS_MARGIN`, o cuando el actual deja de ser válido (el precio lo
atravesó, o desapareció de la cadena). Así la línea de TRACE se mueve cuando la
estructura se mueve, no cuando el ruido cambia de sitio.

PROCEDENCIA
-----------
Call Wall y Put Wall son **DERIVED · ITM QUANT**. Las entradas son
`DIRECT_PROVIDER`; la conclusión es propia. Publicarlas como dato del proveedor
sería atribuirle a Quant Data un nivel que no emite.
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import data_lineage as DL
from . import wall_gex as WG
from . import wall_snapshot as WS
from .lane_truth import TRUTH as LANE_TRUTH
from .data_lineage import LINEAGE, DERIVED, DATA_OK, NO_PROVIDER_DATA, UNAVAILABLE
from .obs import note as _obs_note

# Cuánto tiene que mejorar un candidato para desbancar al muro vigente. Por debajo
# de este margen la diferencia es ruido y cambiar la línea sólo despista.
HYSTERESIS_MARGIN = 0.15

# Peso del interés abierto frente a la exposición dentro del score. 0.5 = media
# geométrica pura; por encima manda el libro, por debajo manda la cobertura.
OI_WEIGHT = 0.5

# Un muro a más de este porcentaje del precio no describe la sesión: describe la
# cola de la cadena. Se sigue publicando en `candidates`, pero no como muro vigente.
MAX_DISTANCE_PCT = 12.0

CALL_WALL = "call_wall"
PUT_WALL = "put_wall"


def _f(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WallState:
    """Muro vigente por símbolo y lado, con su histéresis.

    El estado vive aquí y no en cada sección: si TRACE y FLUJO guardaran su propio
    muro, volverían a poder discrepar, que es exactamente lo que este módulo
    existe para impedir.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def get(self, symbol: str, side: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._current.get((str(symbol).upper(), side))
            return dict(cur) if cur else None

    def set(self, symbol: str, side: str, wall: Optional[Dict[str, Any]]) -> None:
        key = (str(symbol).upper(), side)
        with self._lock:
            if wall is None:
                self._current.pop(key, None)
            else:
                self._current[key] = dict(wall)

    def clear_symbol(self, symbol: str) -> None:
        sym = str(symbol).upper()
        with self._lock:
            for key in [k for k in self._current if k[0] == sym]:
                self._current.pop(key, None)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {f"{s}:{side}": dict(v) for (s, side), v in self._current.items()}

    def reset(self) -> None:
        with self._lock:
            self._current.clear()


WALLS = WallState()


def _normalize(values: Sequence[float]) -> List[float]:
    """A [0,1] contra el máximo observado. Sin máximo positivo, todo a cero."""
    vals = [abs(float(v)) for v in values]
    peak = max(vals) if vals else 0.0
    if peak <= 1e-12:
        return [0.0] * len(vals)
    return [v / peak for v in vals]


def _side_exposure(row: Dict[str, Any], side: str) -> Optional[float]:
    """Exposición del LADO que corresponde al muro.

    El muro de calls es donde se concentra la gamma DE LAS CALLS, no donde el neto
    es mayor: un strike con mucha call gamma y mucha put gamma tiene un neto
    pequeño y el método neto lo pasa por alto justo donde más cobertura hay.

    Cuando el proveedor no desglosa por lado se cae al neto con su signo, que es la
    mejor aproximación disponible, y el método lo declara.
    """
    field = "call_gex" if side == CALL_WALL else "put_gex"
    explicit = _f(row.get(field))
    if explicit is not None:
        return abs(explicit)
    net = _f(row.get("gex"))
    if net is None:
        return None
    # Sin desglose: el neto positivo pesa para el muro de calls y el negativo para
    # el de puts. Usar |neto| en los dos lados haría que el mismo strike fuera
    # candidato a los dos muros a la vez.
    if side == CALL_WALL:
        return net if net > 0 else 0.0
    return -net if net < 0 else 0.0


def build_candidates(symbol: str, exposure_rows: Sequence[Dict[str, Any]],
                     oi_rows: Sequence[Dict[str, Any]] | None,
                     spot: Optional[float], side: str) -> Dict[str, Any]:
    """Puntúa cada strike del lado correcto. No elige todavía: sólo mide."""
    sym = str(symbol or "").upper()
    px = _f(spot)
    rows = [r for r in (exposure_rows or []) if isinstance(r, dict)]
    if px is None or not rows:
        return {"ready": False, "side": side, "symbol": sym, "rows": [],
                "reason": ("sin precio de referencia" if px is None
                           else "sin exposición por strike")}

    oi_by_k: Dict[float, float] = {}
    for r in (oi_rows or []):
        if not isinstance(r, dict):
            continue
        k = _f(r.get("strike"))
        v = _f(r.get("oi") if r.get("oi") is not None else r.get("value"))
        if k is not None and v is not None:
            oi_by_k[k] = abs(v)
    # El OI del LADO importa más que el total: un strike con 50k puts abiertas no
    # sostiene un muro de calls. Se usa cuando el proveedor lo desglosa.
    oi_side_by_k: Dict[float, float] = {}
    side_field = "call_oi" if side == CALL_WALL else "put_oi"
    for r in (oi_rows or []):
        if not isinstance(r, dict):
            continue
        k = _f(r.get("strike"))
        v = _f(r.get(side_field) if r.get(side_field) is not None
               else (r.get("call") if side == CALL_WALL else r.get("put")))
        if k is not None and v is not None:
            oi_side_by_k[k] = abs(v)

    picked: List[Dict[str, Any]] = []
    for r in rows:
        k = _f(r.get("strike"))
        if k is None:
            continue
        # Regla de lado: un muro de calls por debajo del precio ya fue atravesado.
        if side == CALL_WALL and k <= px:
            continue
        if side == PUT_WALL and k >= px:
            continue
        exp = _side_exposure(r, side)
        if exp is None or exp <= 0:
            continue
        oi = oi_side_by_k.get(k, oi_by_k.get(k))
        picked.append({"strike": k, "exposure": exp, "oi": oi,
                       "distance_pct": round((k - px) / px * 100.0, 4)})

    if not picked:
        return {"ready": False, "side": side, "symbol": sym, "rows": [],
                "reason": f"sin strikes con exposición de {'calls' if side == CALL_WALL else 'puts'} "
                          f"{'por encima' if side == CALL_WALL else 'por debajo'} del precio"}

    exp_norm = _normalize([p["exposure"] for p in picked])
    oi_values = [p["oi"] for p in picked if p["oi"] is not None]
    oi_available = len(oi_values) >= max(2, len(picked) // 4)

    # v1.57.0 · UN OI QUE FALTA NO ES UN OI DE CERO.
    #
    # La regla de arriba —«no se multiplica por cero: eso sería afirmar que no hay
    # libro, no que no se sabe»— estaba escrita, y sólo se aplicaba al activo
    # ENTERO. Por strike no: la normalización recibía `p["oi"] or 0.0`, así que un
    # strike sin OI publicado entraba como cero medido, salía con `oi_norm = 0` y
    # su score se anulaba —media geométrica— por muy grande que fuera su
    # exposición.
    #
    # Medido: en una cadena donde el strike de MAYOR exposición de calls es el
    # único sin OI, el muro se lo lleva el segundo. El dato que faltaba no era el
    # del muro; era el del proveedor. Y ocurría en los dos lados.
    #
    # Ahora el strike sin OI se puntúa con la evidencia que SÍ tiene —exposición
    # sola, el mismo tratamiento que recibe la cadena entera cuando el proveedor
    # no publica OI para ese activo— y se marca `oi_missing` para que el Auditor
    # lo vea en vez de tener que deducirlo de un cero.
    oi_norm = (_normalize([p["oi"] if p["oi"] is not None else 0.0 for p in picked])
               if oi_available else [None] * len(picked))

    sin_oi = 0
    for p, e, o in zip(picked, exp_norm, oi_norm):
        # DOS casos distintos, y no se pueden llamar igual:
        #
        #   el proveedor no publica OI para este ACTIVO   → oi_available False
        #   el proveedor publica OI pero no el de ESTE strike → hueco puntual
        #
        # El primero describe la cobertura del proveedor; el segundo, un agujero
        # dentro de una cadena que por lo demás llegó. Mezclarlos haría que un
        # activo entero sin interés abierto se leyera como si le faltara un dato
        # suelto, que es un diagnóstico distinto.
        falta_oi = oi_available and p["oi"] is None
        p["oi_missing"] = falta_oi
        p["exposure_norm"] = round(e, 6)
        p["oi_norm"] = (None if (o is None or falta_oi) else round(o, 6))
        if o is None or falta_oi:
            # Sin OI se puntúa sólo con exposición y se declara. No se multiplica
            # por cero: eso sería afirmar que no hay libro, no que no se sabe.
            p["score"] = round(100.0 * e, 4)
            p["score_method"] = ("exposure-only-oi-missing" if falta_oi
                                 else "exposure-only-no-open-interest")
            sin_oi += falta_oi
        else:
            p["score"] = round(100.0 * (e ** (1.0 - OI_WEIGHT)) * (o ** OI_WEIGHT), 4)
            p["score_method"] = "geometric-exposure-x-open-interest"
        p["within_range"] = abs(p["distance_pct"]) <= MAX_DISTANCE_PCT

    picked.sort(key=lambda p: -p["score"])
    return {"ready": True, "side": side, "symbol": sym, "rows": picked,
            "oi_available": oi_available,
            # Cuántos strikes se puntuaron sin OI porque el proveedor no lo
            # publicó para ellos. Cero es lo normal; que no lo sea no invalida el
            # muro, pero hay que poder verlo sin abrir el código.
            "oi_missing_strikes": sin_oi,
            "method": ("geometric-exposure-x-open-interest" if oi_available
                       else "exposure-only-no-open-interest"),
            "spot": px}


def _apply_hysteresis(symbol: str, side: str, candidates: List[Dict[str, Any]],
                      spot: float) -> Tuple[Optional[Dict[str, Any]], str, Optional[Dict[str, Any]]]:
    """Elige el muro vigente respetando el que ya estaba.

    Devuelve (muro, motivo del cambio, muro anterior retirado).
    """
    in_range = [c for c in candidates if c.get("within_range")]
    best = in_range[0] if in_range else None
    previous = WALLS.get(symbol, side)

    if best is None:
        if previous is not None:
            return None, "sin candidato válido en rango", previous
        return None, "sin candidato válido en rango", None

    if previous is None:
        return best, "primer muro de la sesión", None

    prev_strike = _f(previous.get("strike"))
    # ¿Sigue siendo válido el muro anterior? Deja de serlo si el precio lo
    # atravesó —un muro de calls por debajo del precio es historia— o si su strike
    # ya no aparece en la cadena.
    crossed = (prev_strike is not None
               and ((side == CALL_WALL and prev_strike <= spot)
                    or (side == PUT_WALL and prev_strike >= spot)))
    still_listed = next((c for c in candidates if prev_strike is not None
                         and abs(c["strike"] - prev_strike) < 1e-9), None)

    if crossed:
        return best, f"el precio atravesó {prev_strike:g}", previous
    if still_listed is None:
        return best, f"{prev_strike:g} desapareció de la cadena", previous
    if best["strike"] == prev_strike:
        return best, "sin cambio", None

    # Histéresis: el candidato tiene que ser CLARAMENTE mejor, no marginalmente.
    prev_score = float(still_listed["score"])
    if prev_score <= 0:
        return best, f"{prev_strike:g} perdió toda su exposición", previous
    improvement = (best["score"] - prev_score) / prev_score
    if improvement >= HYSTERESIS_MARGIN:
        return best, (f"{best['strike']:g} supera a {prev_strike:g} "
                      f"en {improvement * 100:.0f}%"), previous
    # No mejora lo suficiente: se mantiene el muro vigente para que la línea no
    # baile. Se devuelve el candidato ANTERIOR con su score actualizado.
    kept = dict(still_listed)
    return kept, "se mantiene por histéresis", None


def _publish_spec(symbol: str, side: str, spec: Dict[str, Any],
                  contrato: Dict[str, Any]) -> Dict[str, Any]:
    """El muro del contrato de GEX, con la forma que publica el Wall Engine.

    Traduce, no recalcula. El strike, la gamma, el OI, el precio y la hora son
    los que decidió `wall_gex`; aquí sólo se les pone el sobre que el resto de la
    terminal ya consume, para que no existan dos formas de leer un muro.
    """
    mejor = _f(spec.get("gex")) or 0.0
    pico = max([_f(r.get("gex")) or 0.0 for r in (spec.get("ranked") or [])] or [mejor])
    return {
        "ready": True, "side": side, "strike": spec.get("strike"),
        # `score` sigue siendo la magnitud relativa dentro del lado —el ganador
        # es 100— pero ahora dice DE QUÉ es porcentaje. Un número sin unidad en
        # una pantalla de operativa se acaba leyendo como cualquier cosa.
        "score": (round(100.0 * mejor / pico, 4) if pico > 0 else None),
        "score_metric": "GEX_NORMALIZADO_DEL_LADO",
        "exposure": spec.get("gex"),
        "gex": spec.get("gex"), "gamma": spec.get("gamma"),
        "oi": spec.get("open_interest"), "open_interest": spec.get("open_interest"),
        "iv": spec.get("iv"), "delta": spec.get("delta"),
        "spot": spec.get("spot"), "price_as_of": spec.get("price_as_of"),
        "expiry": spec.get("expiry"), "multiplier": spec.get("multiplier"),
        "contracts": spec.get("contracts"),
        "distance_pct": spec.get("distance_pct"), "position": spec.get("position"),
        "crossed": spec.get("crossed"),
        "runner_up": spec.get("runner_up"),
        "margin_over_runner_up_pct": spec.get("margin_over_runner_up_pct"),
        "verdict": spec.get("verdict"),
        "failed_controls": contrato.get("failed_controls") or [],
        "method": WG.FORMULA, "chain_method": WG.FORMULA,
        "positioning_convention": spec.get("positioning_convention"),
        "authority": "ITMQ_WALLS_GEX_V1",
        "oi_available": spec.get("open_interest") is not None,
        "oi_missing": spec.get("open_interest") is None,
        "oi_missing_strikes": 0,
        "source_mode": DERIVED, "provider": DL.ITM_QUANT,
        "fallback_used": False,
        "change_reason": ("strike de mayor Gamma Exposure del lado "
                          f"({spec.get('verdict')})"),
        "retired": None,
        "candidates": [{"strike": r.get("strike"), "exposure": r.get("gex"),
                        "oi": r.get("open_interest"), "gamma": r.get("gamma"),
                        "score": (round(100.0 * (_f(r.get("gex")) or 0.0) / pico, 4)
                                  if pico > 0 else None)}
                       for r in (spec.get("ranked") or [])],
        "updated_at": spec.get("updated_at") or _now(),
        "label": spec.get("label"),
        "note": contrato.get("note"),
    }


def resolve_walls(symbol: str, *, exposure_rows: Sequence[Dict[str, Any]],
                  oi_rows: Sequence[Dict[str, Any]] | None = None,
                  spot: Optional[float] = None,
                  fallback: Optional[Dict[str, Any]] = None,
                  provider_direct: bool = True,
                  contract_rows: Sequence[Dict[str, Any]] | None = None,
                  price_as_of: Optional[str] = None,
                  expiry: Optional[str] = None,
                  expiry_policy: str = WG.NEAREST,
                  multiplier: float = WG.MULTIPLIER_DEFAULT,
                  ages: Optional[Dict[str, Any]] = None,
                  sources: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """LA autoridad de Call Wall y Put Wall. Ninguna sección calcula otra cosa.

    `fallback` admite los muros del cálculo propio antiguo (`structural_walls`)
    para el caso en que el proveedor no sirva exposición por strike de ese activo.
    Entra etiquetado como respaldo, nunca disfrazado del resultado principal.
    """
    sym = str(symbol or "").upper()
    px = _f(spot)
    out: Dict[str, Any] = {
        "symbol": sym, "spot": px, "ready": False,
        "source_mode": DERIVED, "provider": DL.ITM_QUANT,
        "authority": "ITMQ_WALL_ENGINE",
        "inputs": ["QD_GEX", "QD_OPEN_INTEREST_BY_STRIKE", "UNDERLYING_PRICE"],
        "contract": "ITMQ_WALLS_V1",
    }

    # ═══════════════════════════════════════════════════════════════════════
    # VÍA PRINCIPAL · el contrato de Gamma Exposure, con su veredicto
    # ═══════════════════════════════════════════════════════════════════════
    #
    # v1.57.0 · UN MURO ES EL STRIKE DE MAYOR GAMMA EXPOSURE DE SU LADO.
    #
    #     GEX por strike = gamma × OI × multiplicador × precio² × 0.01
    #
    # Hasta aquí el muro se elegía con una media geométrica de la exposición que
    # publica el proveedor y el interés abierto. Ese método tenía un defecto que
    # no se ve en el resultado: el OI ya va DENTRO de la fórmula de exposición,
    # así que volver a multiplicar por él lo cuenta DOS VECES y desplaza el muro
    # hacia strikes con mucho libro y gamma pequeña.
    #
    # Con las griegas por contrato —gamma, OI, IV y delta, que Quant Data publica
    # por contrato— la exposición se calcula entera y el muro es, literalmente,
    # su máximo por lado. Ni OI solo, ni volumen, ni gamma neta, ni Max Pain.
    #
    # Y con un vencimiento DECLARADO. Mezclar vencimientos es legítimo si se
    # dice; hacerlo en silencio da un nivel sin dueño al que nadie puede poner
    # fecha.
    #
    # La vía anterior no se borra: sostiene la pantalla cuando el proveedor no
    # publica griegas por contrato, y entra ETIQUETADA como respaldo.
    contrato: Dict[str, Any] = {}
    if contract_rows:
        try:
            contrato = WG.walls(sym, contract_rows=contract_rows, spot=px,
                                price_as_of=price_as_of, expiry=expiry,
                                expiry_policy=expiry_policy, multiplier=multiplier,
                                ages=ages, sources=sources,
                                provider_exposure=exposure_rows)
        except Exception as exc:   # noqa: BLE001 — se degrada a la vía anterior
            _obs_note("wall_engine:gex_contract", exc, severity="DEGRADED")
            contrato = {}
    if contrato:
        out["gex_contract"] = {k: v for k, v in contrato.items()
                               if k not in (CALL_WALL, PUT_WALL)}
        out["verdict"] = contrato.get("verdict")
        out["expiry"] = contrato.get("expiry")
        out["formula"] = WG.FORMULA
        out["positioning_convention"] = contrato.get("positioning_convention")

    for side, metric in ((CALL_WALL, "ITMQ_CALL_WALL"), (PUT_WALL, "ITMQ_PUT_WALL")):
        spec = (contrato.get(side) or {}) if contrato else {}
        if spec.get("ready"):
            wall = _publish_spec(sym, side, spec, contrato)
            WALLS.set(sym, side, {"strike": wall["strike"], "score": wall["score"],
                                  "updated_at": wall["updated_at"]})
            out[side] = wall
            LINEAGE.record(metric, sym, source_mode=DERIVED, state=DATA_OK,
                           provider=DL.ITM_QUANT,
                           normalized_value=wall.get("score"),
                           final_value=wall.get("strike"),
                           derivation=WG.FORMULA,
                           rows=len(spec.get("ranked") or []),
                           detail=f"{wall.get('verdict')} · vencimiento "
                                  f"{spec.get('expiry')}")
            continue

        cand = build_candidates(sym, exposure_rows, oi_rows, px, side)
        if not cand.get("ready"):
            used = _fallback_wall(sym, side, fallback, px)
            out[side] = used
            LINEAGE.record(metric, sym,
                           source_mode=(DERIVED if used.get("ready") else UNAVAILABLE),
                           state=(DATA_OK if used.get("ready") else NO_PROVIDER_DATA),
                           provider=DL.ITM_QUANT,
                           final_value=used.get("strike"),
                           fallback_used=bool(used.get("fallback_used")),
                           derivation=used.get("method") or "",
                           detail=cand.get("reason") or "")
            continue

        chosen, reason, retired = _apply_hysteresis(sym, side, cand["rows"], px)
        if chosen is None:
            WALLS.set(sym, side, None)
            used = _fallback_wall(sym, side, fallback, px)
            used["retired"] = retired
            used["change_reason"] = reason
            out[side] = used
            LINEAGE.record(metric, sym,
                           source_mode=(DERIVED if used.get("ready") else UNAVAILABLE),
                           state=(DATA_OK if used.get("ready") else NO_PROVIDER_DATA),
                           provider=DL.ITM_QUANT, final_value=used.get("strike"),
                           fallback_used=bool(used.get("fallback_used")),
                           detail=reason)
            continue

        wall = {
            "ready": True, "side": side, "strike": chosen["strike"],
            "score": chosen["score"], "exposure": chosen.get("exposure"),
            "oi": chosen.get("oi"), "distance_pct": chosen.get("distance_pct"),
            # v1.57.0 · `method` describía la CADENA; para el strike elegido podía
            # ser mentira. Un muro cuyo strike no tenía OI se publicaba como
            # «media geométrica exposición × interés abierto» con `oi: None`
            # debajo: el auditor declaraba una fórmula que no es la que se usó.
            #
            # En una magnitud DERIVED —la conclusión la firmamos nosotros, el
            # proveedor no emite muros— que la derivación publicada no sea la
            # real es peor que el número equivocado: quita la única forma de
            # discutirlo.
            "method": chosen.get("score_method") or cand["method"],
            "chain_method": cand["method"],
            "oi_available": cand.get("oi_available", False),
            "oi_missing": bool(chosen.get("oi_missing")),
            "oi_missing_strikes": cand.get("oi_missing_strikes", 0),
            "source_mode": DERIVED, "provider": DL.ITM_QUANT,
            "fallback_used": False,
            "change_reason": reason,
            "retired": retired,
            "candidates": cand["rows"][:6],
            "updated_at": _now(),
            "label": ("CALL WALL" if side == CALL_WALL else "PUT WALL"),
        }
        WALLS.set(sym, side, {"strike": wall["strike"], "score": wall["score"],
                              "updated_at": wall["updated_at"]})
        out[side] = wall
        LINEAGE.record(metric, sym, source_mode=DERIVED, state=DATA_OK,
                       provider=DL.ITM_QUANT,
                       normalized_value=chosen["score"],
                       final_value=chosen["strike"],
                       derivation=cand["method"], rows=len(cand["rows"]),
                       detail=reason)

    out["ready"] = bool(out.get(CALL_WALL, {}).get("ready")
                        or out.get(PUT_WALL, {}).get("ready"))
    out["levels"] = [
        {"kind": side, "name": out[side]["label"], "price": out[side]["strike"],
         "source_mode": DERIVED, "score": out[side].get("score"),
         "fallback_used": out[side].get("fallback_used", False)}
        for side in (CALL_WALL, PUT_WALL)
        if out.get(side, {}).get("ready") and out[side].get("strike") is not None
    ]
    return out


def _fallback_wall(symbol: str, side: str, fallback: Optional[Dict[str, Any]],
                   spot: Optional[float]) -> Dict[str, Any]:
    """Muro del cálculo propio antiguo, etiquetado como respaldo.

    Se valida la regla de lado igual que en la vía principal: un respaldo que
    coloque un Call Wall por debajo del precio sería peor que no tener muro.
    """
    label = "CALL WALL" if side == CALL_WALL else "PUT WALL"
    base = {"ready": False, "side": side, "strike": None, "label": label,
            "source_mode": UNAVAILABLE, "provider": DL.ITM_QUANT,
            "fallback_used": False, "score": None, "candidates": [],
            "method": None, "display": DL.NO_DATA_LABEL}
    if not isinstance(fallback, dict):
        return base
    k = _f(fallback.get(side))
    px = _f(spot)
    if k is None:
        return base
    if px is not None:
        if side == CALL_WALL and k <= px:
            return base
        if side == PUT_WALL and k >= px:
            return base
    return {**base, "ready": True, "strike": k, "source_mode": DERIVED,
            "fallback_used": True, "display": None,
            "method": str(fallback.get("method") or "structural-walls-engine"),
            "distance_pct": (None if not px else round((k - px) / px * 100.0, 4)),
            "updated_at": _now(),
            "detail": "respaldo declarado: el proveedor no sirvió exposición por strike"}


def walls_from_hub(symbol: str, hub: Dict[str, Any], *, spot: Optional[float] = None,
                   fallback: Optional[Dict[str, Any]] = None,
                   price_as_of: Optional[str] = None,
                   price_age_s: Optional[float] = None,
                   price_source: Optional[str] = None,
                   expiry: Optional[str] = None,
                   expiry_policy: str = WG.NEAREST) -> Dict[str, Any]:
    """Entrada canónica: Data Hub → Wall Engine.

    Es la única función que las secciones deben llamar. Recibe el MISMO snapshot
    del Hub que alimenta a la interfaz, así que no hay forma de que TRACE y FLUJO
    midan sobre entradas distintas.

    v1.57.0 · Pasa también las GRIEGAS POR CONTRATO, que son las que permiten
    calcular la Gamma Exposure entera —gamma × OI × multiplicador × precio² ×
    0.01— en vez de puntuar la exposición ya agregada del proveedor. Y con ellas
    van las dos cosas sin las que el número no se puede auditar: la EDAD de cada
    entrada y su FUENTE, que es lo que decide si OI, gamma y precio describen el
    mismo instante del mercado o tres.
    """
    h = hub if isinstance(hub, dict) else {}
    exposure = (h.get("exposure_by_strike") or {}).get("rows") or []
    oi = (h.get("open_interest") or {}).get("by_strike") or []
    greeks = h.get("contract_greeks") or {}
    contract_rows = greeks.get("rows") or []
    lin = greeks.get("lineage") or {}
    fuente_griegas = greeks.get("source") or "QUANTDATA_CONTRACT_GREEKS"
    edades: Dict[str, Any] = {}
    if lin.get("age_seconds") is not None:
        # Gamma y OI salen de la MISMA lectura por contrato, así que comparten
        # edad y fuente por construcción. Es la diferencia con haberlos cruzado
        # de dos endpoints distintos, donde el desfase existe y no se ve.
        edades["gamma"] = lin.get("age_seconds")
        edades["open_interest"] = lin.get("age_seconds")
    if price_age_s is not None:
        edades["price"] = price_age_s
    # El vencimiento operativo es el que ya resolvió la terminal. Que el muro
    # eligiera el suyo por su cuenta permitiría una wall con fecha distinta a la
    # de la cadena que el operador está mirando.
    pedido = expiry
    if pedido is None:
        try:
            from ..providers.quantdata.shared import EXPIRY_SELECTION
            pedido = EXPIRY_SELECTION.principal(str(symbol or "").upper())
        except Exception as exc:   # noqa: BLE001
            _obs_note("wall_engine:expiry_selection", exc, severity="DEGRADED")
            pedido = None
    # ═══════════════════════════════════════════════════════════════════════
    # v1.59.0 · EL SNAPSHOT DECIDE, NO EL CICLO DE SONDEO
    # ═══════════════════════════════════════════════════════════════════════
    #
    # Las Walls se calculaban con lo que hubiera llegado EN ESTE ciclo. Si una
    # petición moría, el panel salía con «sin cálculo de muros en este ciclo»,
    # que no dice si faltó la gamma, el OI, el vencimiento o el precio, ni si
    # había muros válidos hace treinta segundos.
    #
    # Ahora se arma un snapshot coherente y se resuelve en uno de tres estados:
    # COMPLETO (se calcula), LKG (el ciclo perdió algo y hay uno anterior
    # válido, que se publica CON SU EDAD) o NO_CALCULABLE (se nombran los
    # ingredientes que faltan, uno a uno). Ninguno de los tres es una caja
    # vacía. La FÓRMULA no cambia: esto prepara su entrada.
    instantanea = WS.build(str(symbol or "").upper(),
                           contract_rows=contract_rows, spot=spot,
                           price_as_of=price_as_of, expiry=pedido,
                           source=fuente_griegas, ages=edades)
    # v1.62.0 · ¿FALTAN los ingredientes, o todavía NO SE HAN PEDIDO?
    #
    # Las griegas por contrato salen del order flow. Si ese carril aún no se ha
    # ejecutado en este ciclo, el snapshot no es NO_CALCULABLE —que afirma que
    # el dato no existe— sino WAITING_DEPENDENCY, que dice la verdad: no se ha
    # preguntado todavía.
    esperando_por = ""
    if not instantanea.get("ready"):
        for carril in ("options_order_flow_raw", "options_order_flow"):
            estado = LANE_TRUTH.read(carril)
            # `known == False` es «nadie lo ha medido», no «está esperando».
            # Tomarlo por espera afirmaría algo que nadie comprobó.
            if estado.get("known") and estado.get("is_waiting"):
                esperando_por = carril
                break
    resuelta = WS.resolve(str(symbol or "").upper(), instantanea,
                          waiting_on=esperando_por)
    filas_snapshot = list(resuelta.get("rows") or [])
    # El snapshot es COHERENTE o no es nada: si se sirve el anterior, se sirve
    # ENTERO. Tomar sus filas y el precio de ahora mezclaría una cadena de hace
    # cuarenta segundos con un spot de este instante, y el precio entra al
    # cuadrado en la exposición: sería una wall medida sobre dos mercados.
    precio_snapshot = resuelta.get("spot")
    out = resolve_walls(symbol, exposure_rows=exposure, oi_rows=oi,
                        spot=(precio_snapshot if precio_snapshot is not None else spot),
                        fallback=fallback,
                        contract_rows=(filas_snapshot or contract_rows),
                        price_as_of=(resuelta.get("price_as_of") or price_as_of),
                        expiry=(resuelta.get("expiry") or pedido),
                        expiry_policy=expiry_policy, ages=edades,
                        sources={"gamma": fuente_griegas,
                                 "open_interest": fuente_griegas,
                                 "price": price_source or "SESSION_PRICE_PIPELINE"})
    out["snapshot"] = {k: v for k, v in resuelta.items() if k != "rows"}
    out["snapshot_state"] = resuelta.get("state")
    etiqueta = WS.verdict_label(resuelta, str(out.get("verdict") or ""))
    out["verdict_label"] = etiqueta
    for lado in (CALL_WALL, PUT_WALL):
        muro = out.get(lado)
        if not isinstance(muro, dict):
            continue
        muro["snapshot_state"] = resuelta.get("state")
        muro["snapshot_age_seconds"] = resuelta.get("age_seconds")
        muro["from_lkg"] = bool(resuelta.get("from_lkg"))
        muro["verdict_label"] = etiqueta
        if resuelta.get("state") == WS.NO_CALCULABLE:
            # Ya no hay caja vacía: se dice QUÉ ingrediente falta.
            muro["ready"] = False
            muro["missing_ingredients"] = list(resuelta.get("missing_names") or [])
            muro["detail"] = resuelta.get("detail")
    return out


def _causa_sin_controles(walls: Dict[str, Any],
                         contrato: Dict[str, Any]) -> Dict[str, Any]:
    """Por qué NO hay diez controles que enseñar, dicho con nombres.

    Se publica siempre. Cuando hay controles queda en `None` y la tabla los
    pinta; cuando no los hay, esto es lo que la tabla pinta en su lugar, y
    nombra el ingrediente que falta en vez de la ausencia de la tabla.
    """
    if contrato.get("controls"):
        return {}
    instantanea = dict(walls.get("snapshot") or {})
    estado = str(walls.get("snapshot_state") or instantanea.get("state") or "")
    faltan = [str(x) for x in (instantanea.get("missing_names") or [])]
    detalle = str(instantanea.get("detail") or "")
    if estado == WS.WAITING_DEPENDENCY:
        resumen = (f"el carril del que salen los ingredientes todavía no se ha "
                   f"ejecutado en este ciclo: {instantanea.get('waiting_on') or '—'}")
    elif estado == WS.LKG:
        resumen = ("se está sirviendo el último snapshot bueno; sus controles son "
                   "los de ESE ciclo y no los de éste")
    elif faltan:
        resumen = "no se pudo armar el snapshot: falta " + ", ".join(faltan)
    else:
        resumen = (detalle or "no se publicó snapshot y tampoco su causa: "
                              "esto es un defecto del motor, no del proveedor")
    return {
        "snapshot_state": estado or WS.NO_CALCULABLE,
        "missing": faltan,
        "detail": resumen,
        "expected_controls": list(WG.CONTROL_KEYS),
        "policy": ("los diez controles se publican cuando hay snapshot; cuando no "
                   "lo hay se publica POR QUÉ, nunca una tabla vacía"),
    }


def wall_audit(walls: Dict[str, Any], hub: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """CÓMO se calculó cada muro, con los números que lo sostienen.

    ═══════════════════════════════════════════════════════════════════════
    POR QUÉ NO BASTA CON QUE APAREZCA LA LÍNEA
    ═══════════════════════════════════════════════════════════════════════

    Un muro dibujado es una afirmación: «aquí hay una concentración de gamma
    que va a frenar el precio». Si el único respaldo de esa afirmación es que
    hay una línea en el gráfico, no hay forma de discutirla — ni de detectar
    que el cálculo empezó a medir otra cosa.

    Esto publica los datos CON LOS QUE se decidió: la exposición de cada
    candidato, su interés abierto, su distancia, su puntuación y su puesto en
    el ranking, más la fórmula exacta que produjo esa puntuación.

    ═══════════════════════════════════════════════════════════════════════
    LA FÓRMULA, DICHA ENTERA
    ═══════════════════════════════════════════════════════════════════════

        1. Se descartan los strikes del lado equivocado. Un muro de calls por
           DEBAJO del precio ya fue atravesado: es historia, no resistencia.
        2. La exposición del lado se normaliza dentro de los candidatos, y el
           interés abierto del lado también.
        3. La puntuación es una media geométrica ponderada:

               score = 100 · exposición^(1−w) · interés_abierto^w     w = OI_WEIGHT

           Geométrica, no aritmética, porque un strike con mucha exposición y
           NADA de libro abierto no sostiene un muro: la media aritmética lo
           premiaría igual, y la geométrica lo hunde, que es lo correcto.
        4. Sin interés abierto utilizable NO se multiplica por cero —eso
           afirmaría que no hay libro, no que no se sabe—: se puntúa sólo con
           la exposición y se declara con `exposure-only-no-open-interest`.
        5. Entre candidatos válidos gana el de mayor puntuación, con histéresis:
           el nuevo tiene que ser CLARAMENTE mejor que el vigente, no
           marginalmente. Un muro que salta de strike cada ciclo no es un
           nivel: es ruido con nombre.

    No cambia ninguna metodología. Sólo la hace comprobable.
    """
    w = walls if isinstance(walls, dict) else {}
    h = hub if isinstance(hub, dict) else {}
    exposicion = (h.get("exposure_by_strike") or {})
    filas = exposicion.get("rows") or []

    # Exposición de CALL y de PUT por strike, tal y como llegó. Es lo que
    # permite contrastar el veredicto contra el crudo sin recalcular nada.
    por_strike: Dict[float, Dict[str, Any]] = {}
    for r in filas:
        if not isinstance(r, dict):
            continue
        k = _f(r.get("strike"))
        if k is None:
            continue
        por_strike[k] = {
            "strike": k,
            "callExposure": _f(r.get("call_gex") if r.get("call_gex") is not None
                               else r.get("call_exposure") or r.get("call")),
            "putExposure": _f(r.get("put_gex") if r.get("put_gex") is not None
                              else r.get("put_exposure") or r.get("put")),
        }

    lados = {}
    for side in (CALL_WALL, PUT_WALL):
        muro = w.get(side) or {}
        candidatos = []
        for puesto, c in enumerate(muro.get("candidates") or [], 1):
            if not isinstance(c, dict):
                continue
            k = _f(c.get("strike"))
            crudo = por_strike.get(k) if k is not None else None
            candidatos.append({
                "ranking": puesto,
                "strike": k,
                "exposure": c.get("exposure"),
                "exposure_norm": c.get("exposure_norm"),
                "open_interest": c.get("oi"),
                "oi_norm": c.get("oi_norm"),
                "distance_pct": c.get("distance_pct"),
                "within_range": c.get("within_range"),
                "score": c.get("score"),
                "callExposure": (crudo or {}).get("callExposure"),
                "putExposure": (crudo or {}).get("putExposure"),
                "selected": (k is not None and _f(muro.get("strike")) == k),
            })
        lados[side] = {
            "selected_strike": muro.get("strike"),
            "score": muro.get("score"),
            "score_metric": muro.get("score_metric"),
            "exposure": muro.get("exposure"),
            "open_interest": muro.get("oi"),
            "distance_pct": muro.get("distance_pct"),
            "method": muro.get("method"),
            # v1.57.0 · LOS CINCO NÚMEROS QUE SOSTIENEN EL MURO, JUNTOS.
            #
            # strike, gamma, interés abierto, precio y hora. Repartidos por
            # cuatro bloques distintos no se puede comprobar una wall: hay que
            # poder leer de un tirón «este strike, con esta gamma y este OI,
            # con el subyacente en este precio, a esta hora».
            "verdict": muro.get("verdict"),
            "gex": muro.get("gex"),
            "gamma": muro.get("gamma"),
            "iv": muro.get("iv"),
            "delta": muro.get("delta"),
            "spot_used": muro.get("spot"),
            "price_as_of": muro.get("price_as_of"),
            "expiry": muro.get("expiry"),
            "multiplier": muro.get("multiplier"),
            "contracts": muro.get("contracts"),
            "position": muro.get("position"),
            "crossed": muro.get("crossed"),
            "runner_up": muro.get("runner_up"),
            "margin_over_runner_up_pct": muro.get("margin_over_runner_up_pct"),
            "positioning_convention": muro.get("positioning_convention"),
            "authority": muro.get("authority") or "ITMQ_WALL_ENGINE",
            # Cómo se puntuó la cadena frente a cómo se puntuó ESTE strike, y
            # cuántos strikes del lado no traían OI del proveedor. Sin esto el
            # hueco sólo se podía deducir de un `open_interest: null`.
            "chain_method": muro.get("chain_method"),
            "oi_missing": muro.get("oi_missing"),
            "oi_missing_strikes": muro.get("oi_missing_strikes"),
            "oi_available": muro.get("oi_available"),
            "source_mode": muro.get("source_mode"),
            "fallback_used": muro.get("fallback_used"),
            "change_reason": muro.get("change_reason"),
            "retired": muro.get("retired"),
            "ranking": candidatos,
            "candidates_considered": len(candidatos),
        }

    contrato = w.get("gex_contract") or {}
    return {
        "symbol": w.get("symbol"),
        "spot": w.get("spot"),
        "snapshotTime": w.get("timestamp") or (exposicion.get("timestamp")),
        # v1.57.0 · EL VEREDICTO DE LOS DATOS, con los diez controles a la vista.
        #
        # CONFIRMADA no califica al muro: califica a la cadena con la que se
        # calculó. Un muro provisional se sigue pudiendo operar; lo que no se
        # puede es presentarlo como si la cadena estuviera completa.
        "verdict": w.get("verdict") or contrato.get("verdict"),
        # v1.59.0 · el estado del SNAPSHOT y la etiqueta que ve el operador:
        # COMPLETO, LKG con su edad, o NO CALCULABLE con lo que falta.
        "verdict_label": w.get("verdict_label"),
        "snapshot": w.get("snapshot") or {},
        "snapshot_state": w.get("snapshot_state"),
        "failed_controls": contrato.get("failed_controls") or [],
        # ═══════════════════════════════════════════════════════════════════
        # v1.62.0 · LOS DIEZ CONTROLES, O LA CAUSA DE QUE NO EXISTAN
        # ═══════════════════════════════════════════════════════════════════
        #
        # «Los controles del contrato de muros no se publicaron en este ciclo»
        # era una caja vacía: no decía si faltó la gamma, el vencimiento o el
        # precio, ni si el carril estaba esperando turno. Ahora, cuando no hay
        # controles, viaja el ESTADO del snapshot y el ingrediente que falta,
        # uno a uno. La tabla nunca se queda sin nada que decir.
        "controls": contrato.get("controls") or [],
        "controls_absent_because": _causa_sin_controles(w, contrato),
        "gex_formula": contrato.get("formula") or WG.FORMULA,
        "expiry": w.get("expiry") or contrato.get("expiry"),
        "expiry_reason": contrato.get("expiry_reason"),
        "expiry_policy": contrato.get("expiry_policy"),
        "expiries_available": contrato.get("expiries_available"),
        "positioning_convention": (w.get("positioning_convention")
                                   or contrato.get("positioning_convention")),
        "sides_kept_separate": contrato.get("sides_kept_separate"),
        "chain": contrato.get("chain"),
        "contracts_used": contrato.get("contracts_used"),
        "provider_crosscheck": contrato.get("provider_crosscheck"),
        "not_calculated_as": [
            "el strike con mayor interés abierto",
            "el strike con mayor volumen",
            "la gamma neta total",
            "el Max Pain",
            "una mezcla de vencimientos sin declararla",
        ],
        "wall_is": ("una concentración de cobertura probable, no una barrera "
                    "garantizada"),
        "representationMode": "STRIKE_EXPOSURE_BY_SIDE",
        "expirations": exposicion.get("expirations") or exposicion.get("expiration_scope"),
        "authority": "ITMQ_WALL_ENGINE",
        "formula": ("score = 100 · exposición_lado^(1-w) · interés_abierto_lado^w, "
                    f"w = {OI_WEIGHT}; media GEOMÉTRICA, no aritmética; "
                    "sin OI utilizable se puntúa sólo con exposición y se declara; "
                    f"sólo candidatos a menos del {MAX_DISTANCE_PCT} % del precio; "
                    f"histéresis del {HYSTERESIS_MARGIN} para sustituir al muro vigente"),
        "oi_weight": OI_WEIGHT,
        "max_distance_pct": MAX_DISTANCE_PCT,
        "hysteresis_margin": HYSTERESIS_MARGIN,
        "sides": lados,
        "note": ("No cambia ninguna metodología: publica los números con los que "
                 "el Wall Engine ya decidía, para que la decisión se pueda discutir."),
    }


__all__ = ["resolve_walls", "walls_from_hub", "build_candidates", "wall_audit", "WALLS",
           "WallState", "CALL_WALL", "PUT_WALL", "HYSTERESIS_MARGIN",
           "OI_WEIGHT", "MAX_DISTANCE_PCT"]

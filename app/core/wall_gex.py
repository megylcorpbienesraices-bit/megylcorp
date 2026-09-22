"""CALL WALL Y PUT WALL POR GAMMA EXPOSURE · el contrato, entero · v1.57.0

═══════════════════════════════════════════════════════════════════════════
QUÉ SE CALCULA, Y CON QUÉ
═══════════════════════════════════════════════════════════════════════════

    Gamma Exposure por strike = gamma × OI × multiplicador × precio² × 0.01

    Call Wall = strike con MAYOR Gamma Exposure de CALLS
    Put Wall  = strike con MAYOR Gamma Exposure de PUTS   (por valor absoluto)

Los dos lados se mantienen SEPARADOS de principio a fin. No hay un momento en
el cálculo en que se sumen, se resten o se comparen entre sí.

Diez datos hacen falta para que el resultado signifique algo, y los diez se
comprueban uno a uno en `audit()`:

     1  cadena completa del activo
     2  vencimiento definido y declarado
     3  todos los strikes, incluidos los que están fuera del precio
     4  OI por strike Y por tipo, calls y puts separados
     5  gamma válida por contrato en cada strike
     6  precio del subyacente con su hora de captura
     7  IV, delta y multiplicador, para calcular o validar la gamma
     8  convención de posicionamiento cliente↔dealer, declarada
     9  cobertura completa: sin strikes faltantes ni datos viejos mezclados
    10  misma hora y misma fuente para OI, gamma y precio

═══════════════════════════════════════════════════════════════════════════
CÓMO **NO** SE CALCULA
═══════════════════════════════════════════════════════════════════════════

Cinco métodos que se parecen y dan otra respuesta. Hay una prueba por cada uno
construida para que el método equivocado gane si alguien lo reintroduce:

    · NO es el strike con mayor OI          — el OI ya va DENTRO de la fórmula;
                                              volver a ordenar por él lo cuenta
                                              dos veces y premia strikes de
                                              gamma despreciable
    · NO es el strike con mayor volumen     — el volumen es rotación del día;
                                              la cobertura la sostiene el libro
                                              abierto, no el trasiego
    · NO es la gamma NETA                   — un strike con mucha call gamma y
                                              mucha put gamma tiene neto pequeño
                                              y es justo donde más cobertura hay
    · NO es el Max Pain                     — el Max Pain minimiza el valor
                                              liquidado de los tenedores; es otra
                                              pregunta y otro número
    · NO se mezclan vencimientos sin decirlo — mezclar es legítimo si se declara;
                                              hacerlo en silencio convierte el
                                              nivel en un promedio sin dueño

═══════════════════════════════════════════════════════════════════════════
CONFIRMADA O PROVISIONAL
═══════════════════════════════════════════════════════════════════════════

El veredicto **no** califica al muro: califica a los DATOS con los que se
calculó.

    WALL CONFIRMADA    los diez controles pasan
    WALL PROVISIONAL   falta algo, y se dice exactamente qué

Un muro provisional se sigue publicando —con la mejor evidencia disponible— y
se sigue pudiendo operar con él. Lo que no se hace es presentarlo como si la
cadena estuviera completa. La alternativa honesta a un dato incompleto es
decirlo, no ocultarlo ni rellenarlo.

═══════════════════════════════════════════════════════════════════════════
LO QUE UN MURO NO ES
═══════════════════════════════════════════════════════════════════════════

Una wall es una **concentración de cobertura probable**, no una barrera
garantizada. La fórmula dice dónde tendría que ajustar más el dealer si el
precio llegara allí; la reacción del precio depende además de la posición REAL
de clientes y dealers, que el proveedor no publica. Por eso la convención de
posicionamiento se declara en el resultado en vez de darse por supuesta: sin
ella sólo se conoce la gamma matemática, no la presión de cobertura.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

CONTRACT = "ITMQ_WALLS_GEX_V1"

#: Contratos por opción. 100 en acciones y ETFs de EE. UU.; se puede pasar otro.
MULTIPLIER_DEFAULT = 100.0

#: La fórmula mide la exposición por un movimiento del 1 % del subyacente.
MOVE_FRACTION = 0.01

FORMULA = "gamma × OI × multiplicador × precio² × 0.01"

#: Convención de posicionamiento. Es una CONVENCIÓN declarada, no una medición
#: del inventario del dealer: el proveedor no publica quién está largo de qué.
POSITIONING_CONVENTION = "CLIENTE_LARGO_OPCIONES__DEALER_CORTO_GAMMA"

#: Segundos de desfase que se admiten entre gamma, OI y precio. Por encima de
#: esto ya no describen el mismo instante del mercado.
MAX_SKEW_S = 90.0

#: Un dato más viejo que esto no entra como fresco en un muro.
MAX_AGE_S = 300.0

#: Cobertura mínima de la cadena a cada lado del precio, en porcentaje. Sin
#: strikes fuera del dinero el muro sólo puede caer donde llega la cadena.
MIN_OTM_COVERAGE_PCT = 3.0

#: Strikes mínimos por lado para que la cadena se considere completa.
MIN_STRIKES_PER_SIDE = 3

CONFIRMED = "WALL_CONFIRMADA"
PROVISIONAL = "WALL_PROVISIONAL"

CALL = "call"
PUT = "put"
CALL_WALL = "call_wall"
PUT_WALL = "put_wall"

#: Los diez controles, en el orden en que se exigen.
CONTROL_KEYS = (
    "CADENA_COMPLETA",
    "VENCIMIENTO_DEFINIDO",
    "STRIKES_FUERA_DEL_DINERO",
    "OI_POR_STRIKE_Y_LADO",
    "GAMMA_VALIDA_POR_CONTRATO",
    "PRECIO_CON_HORA",
    "IV_DELTA_MULTIPLICADOR",
    "CONVENCION_DE_POSICIONAMIENTO",
    "SIN_DATOS_VIEJOS_MEZCLADOS",
    "MISMA_HORA_MISMA_FUENTE",
)

#: Políticas de elección de vencimiento. `NEAREST` es el más cercano con
#: cadena; `WEEKLY_FRIDAY` el viernes más próximo.
NEAREST = "NEAREST"
WEEKLY_FRIDAY = "WEEKLY_FRIDAY"


def _f(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════ la fórmula

def gamma_exposure(*, gamma: Any, open_interest: Any, spot: Any,
                   multiplier: Any = MULTIPLIER_DEFAULT) -> Optional[float]:
    """`gamma × OI × multiplicador × precio² × 0.01`, o `None` si falta un factor.

    `None` y `0.0` son cosas distintas y no se pueden confundir: cero es «se
    midió y no hay exposición»; `None` es «falta un dato para saberlo». Un
    `None` tratado como cero es un strike que desaparece del ranking sin que
    nadie se entere.
    """
    g = _f(gamma)
    oi = _f(open_interest)
    px = _f(spot)
    mult = _f(multiplier, MULTIPLIER_DEFAULT)
    if g is None or oi is None or px is None or mult is None:
        return None
    if px <= 0 or mult <= 0 or oi < 0:
        return None
    return abs(g) * oi * mult * (px ** 2) * MOVE_FRACTION


# ═══════════════════════════════════════════════════ vencimiento operativo

def _side_of(row: Dict[str, Any]) -> Optional[str]:
    raw = str(row.get("option_type") or row.get("type") or "").strip().lower()
    if raw.startswith("c"):
        return CALL
    if raw.startswith("p"):
        return PUT
    return None


def _expiry_of(row: Dict[str, Any]) -> Optional[str]:
    v = row.get("expiration") or row.get("expiry") or row.get("expiration_date")
    if v is None:
        return None
    s = str(v).strip()
    return s[:10] if s else None


def _weekday(expiry: str) -> Optional[int]:
    try:
        return datetime.strptime(expiry[:10], "%Y-%m-%d").weekday()
    except Exception:
        return None


def available_expiries(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Vencimientos presentes en la cadena, ordenados. Sin inventar ninguno."""
    out = {e for r in rows if isinstance(r, dict) and (e := _expiry_of(r))}
    return sorted(out)


def select_expiry(rows: Sequence[Dict[str, Any]], *, policy: str = NEAREST,
                  requested: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Elige el vencimiento operativo y DICE con qué regla.

    Devuelve `(vencimiento, motivo)`. Nunca elige por promedio ni mezcla: un
    nivel calculado sobre varios vencimientos a la vez no tiene dueño, y quien
    lo mire no puede saber a qué fecha se refiere.
    """
    disponibles = available_expiries(rows)
    if not disponibles:
        return None, "la cadena no trae vencimiento en ninguna fila"
    if requested:
        pedido = str(requested)[:10]
        if pedido in disponibles:
            return pedido, "vencimiento pedido explícitamente"
        # El pedido no está en la cadena: se cae a la política Y SE DICE. Devolver
        # «ninguno» dejaría la pantalla sin muros por un desajuste de fechas entre
        # el vencimiento que resolvió la terminal y el que trae esta cadena, que
        # es un problema de sincronía, no de falta de datos.
        elegido, motivo = select_expiry(rows, policy=policy)
        return elegido, (f"el vencimiento pedido {pedido} no está en la cadena; "
                         f"se usa {elegido} ({motivo})")
    if str(policy).upper() == WEEKLY_FRIDAY:
        viernes = [e for e in disponibles if _weekday(e) == 4]
        if viernes:
            return viernes[0], "viernes más próximo con cadena"
        return disponibles[0], ("no hay viernes en la cadena; "
                                "se usa el más cercano y se declara")
    return disponibles[0], "vencimiento más cercano con cadena"


# ═══════════════════════════════════════════════ un contrato, una sola vez

def _contract_key(row: Dict[str, Any]) -> Optional[Tuple[str, float, str]]:
    exp = _expiry_of(row)
    k = _f(row.get("strike"))
    side = _side_of(row)
    if exp is None or k is None or side is None:
        return None
    return (exp, k, side)


def dedupe_contracts(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Una fila por CONTRATO, quedándose con la más reciente.

    Las griegas por contrato llegan dentro de las filas de order flow, que son
    OPERACIONES: el mismo contrato aparece tantas veces como veces se negoció.
    Sumar su gamma y su OI por cada aparición multiplicaría el interés abierto
    por el número de prints —cien operaciones en un strike lo convertirían en un
    muro de la nada— y ese error es invisible en el resultado: da un número
    grande y creíble.

    El OI y la gamma son propiedades DEL CONTRATO, no de la operación. Se
    deduplica por (vencimiento, strike, tipo).
    """
    mejor: Dict[Tuple[str, float, str], Dict[str, Any]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        key = _contract_key(r)
        if key is None:
            continue
        actual = mejor.get(key)
        if actual is None:
            mejor[key] = r
            continue
        # Gana la marca de tiempo más reciente; sin marca, la última vista.
        t_new = str(r.get("t") or r.get("timestamp") or "")
        t_old = str(actual.get("t") or actual.get("timestamp") or "")
        if t_new >= t_old:
            mejor[key] = r
    return list(mejor.values())


# ═══════════════════════════════════════════════════════ ranking por lado

def rank_side(contracts: Sequence[Dict[str, Any]], *, side: str, spot: float,
              multiplier: float = MULTIPLIER_DEFAULT) -> List[Dict[str, Any]]:
    """Gamma Exposure de UN lado, strike a strike, de mayor a menor.

    Los dos lados no se tocan: esta función sólo ve calls o sólo ve puts. Es la
    razón por la que un strike con mucha call gamma y mucha put gamma no se
    cancela solo, que es lo que le pasa al método neto justo donde más cobertura
    hay.
    """
    por_strike: Dict[float, Dict[str, Any]] = {}
    for r in contracts:
        if _side_of(r) != side:
            continue
        k = _f(r.get("strike"))
        if k is None:
            continue
        gex = gamma_exposure(gamma=r.get("gamma"),
                             open_interest=r.get("open_interest"),
                             spot=spot, multiplier=multiplier)
        slot = por_strike.setdefault(k, {
            "strike": k, "side": side, "gex": None, "gamma": None,
            "open_interest": None, "contracts": 0, "iv": None, "delta": None,
            "volume": None, "missing": [],
        })
        slot["contracts"] += 1
        slot["gamma"] = _f(r.get("gamma"))
        slot["open_interest"] = _f(r.get("open_interest"))
        slot["iv"] = _f(r.get("implied_volatility") if r.get("implied_volatility")
                        is not None else r.get("iv"))
        slot["delta"] = _f(r.get("delta"))
        slot["volume"] = _f(r.get("volume"))
        if gex is None:
            # Qué factor falta, por su nombre. «Sin exposición» sin decir cuál
            # obliga a adivinar entre gamma ausente, OI ausente y precio ausente.
            faltan = [n for n, v in (("gamma", r.get("gamma")),
                                     ("open_interest", r.get("open_interest")))
                      if _f(v) is None]
            slot["missing"] = faltan or ["precio_o_multiplicador"]
        else:
            slot["gex"] = round(gex, 6)
    filas = list(por_strike.values())
    for f in filas:
        f["position"] = ("ABOVE" if f["strike"] > spot
                         else "BELOW" if f["strike"] < spot else "AT")
        f["distance_pct"] = round((f["strike"] - spot) / spot * 100.0, 4) if spot else None
    # Sin GEX no se ordena como cero: se va al final y se declara.
    filas.sort(key=lambda f: (f["gex"] is None, -(f["gex"] or 0.0), f["strike"]))
    return filas


# ═══════════════════════════════════════════════════════ cadena y controles

def chain_report(contracts: Sequence[Dict[str, Any]], *,
                 spot: Optional[float]) -> Dict[str, Any]:
    """Estado de la cadena: escalón, huecos y cobertura a cada lado del precio."""
    strikes = sorted({k for r in contracts if (k := _f(r.get("strike"))) is not None})
    out: Dict[str, Any] = {
        "strikes": len(strikes), "min_strike": (strikes[0] if strikes else None),
        "max_strike": (strikes[-1] if strikes else None),
        "step": None, "gaps": [], "above": 0, "below": 0,
        "coverage_above_pct": None, "coverage_below_pct": None,
    }
    if len(strikes) >= 2:
        pasos = [round(b - a, 6) for a, b in zip(strikes, strikes[1:]) if b > a]
        if pasos:
            # El escalón es el MÁS FRECUENTE, no el mínimo: una cadena con un
            # strike intercalado a mitad de escalón no convierte a todos los
            # demás en huecos.
            paso = max(set(pasos), key=pasos.count)
            out["step"] = paso
            if paso > 0:
                huecos = []
                for a, b in zip(strikes, strikes[1:]):
                    salto = round((b - a) / paso)
                    if salto > 1:
                        huecos.append({"after": a, "before": b,
                                       "missing": int(salto - 1)})
                out["gaps"] = huecos
    px = _f(spot)
    if px and strikes:
        arriba = [k for k in strikes if k > px]
        abajo = [k for k in strikes if k < px]
        out["above"], out["below"] = len(arriba), len(abajo)
        out["coverage_above_pct"] = (round((max(arriba) - px) / px * 100.0, 4)
                                     if arriba else 0.0)
        out["coverage_below_pct"] = (round((px - min(abajo)) / px * 100.0, 4)
                                     if abajo else 0.0)
    return out


def _control(key: str, ok: bool, detail: str, **evidence: Any) -> Dict[str, Any]:
    return {"control": key, "ok": bool(ok), "detail": detail,
            "evidence": dict(evidence)}


def audit(*, contracts: Sequence[Dict[str, Any]], chain: Dict[str, Any],
          expiry: Optional[str], expiry_reason: str, expiries_available: int,
          mixed_expiries: bool, spot: Optional[float],
          price_as_of: Optional[str], multiplier: Optional[float],
          convention: Optional[str], ages: Optional[Dict[str, Any]] = None,
          sources: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Los diez controles, uno por uno, con la evidencia de cada veredicto."""
    ages = ages or {}
    sources = sources or {}
    filas: List[Dict[str, Any]] = []

    # 1 · cadena completa
    sin_huecos = not chain.get("gaps")
    bastantes = (chain.get("above", 0) >= MIN_STRIKES_PER_SIDE
                 and chain.get("below", 0) >= MIN_STRIKES_PER_SIDE)
    filas.append(_control(
        "CADENA_COMPLETA", bool(sin_huecos and bastantes),
        ("la cadena llega entera" if sin_huecos and bastantes else
         (f"{len(chain.get('gaps') or [])} huecos en la escalera de strikes"
          if not sin_huecos else
          f"menos de {MIN_STRIKES_PER_SIDE} strikes a algún lado del precio")),
        strikes=chain.get("strikes"), step=chain.get("step"),
        gaps=chain.get("gaps"), above=chain.get("above"), below=chain.get("below")))

    # 2 · vencimiento definido
    filas.append(_control(
        "VENCIMIENTO_DEFINIDO", bool(expiry) and not mixed_expiries,
        (f"vencimiento {expiry} · {expiry_reason}" if expiry and not mixed_expiries
         else ("hay varios vencimientos en las filas usadas" if mixed_expiries
               else f"sin vencimiento: {expiry_reason}")),
        expiry=expiry, reason=expiry_reason, available=expiries_available,
        mixed=mixed_expiries))

    # 3 · strikes fuera del dinero
    cob_a = chain.get("coverage_above_pct")
    cob_b = chain.get("coverage_below_pct")
    otm_ok = (cob_a is not None and cob_b is not None
              and cob_a >= MIN_OTM_COVERAGE_PCT and cob_b >= MIN_OTM_COVERAGE_PCT)
    filas.append(_control(
        "STRIKES_FUERA_DEL_DINERO", otm_ok,
        ("la cadena cubre fuera del dinero por los dos lados" if otm_ok else
         f"cobertura insuficiente fuera del dinero (mínimo {MIN_OTM_COVERAGE_PCT} % "
         f"por lado)"),
        coverage_above_pct=cob_a, coverage_below_pct=cob_b,
        minimum_pct=MIN_OTM_COVERAGE_PCT))

    # 4 · OI por strike y por tipo
    por_lado = {CALL: 0, PUT: 0}
    sin_oi: List[float] = []
    for r in contracts:
        side = _side_of(r)
        if side in por_lado:
            por_lado[side] += 1
        if _f(r.get("open_interest")) is None:
            k = _f(r.get("strike"))
            if k is not None:
                sin_oi.append(k)
    oi_ok = not sin_oi and por_lado[CALL] > 0 and por_lado[PUT] > 0
    filas.append(_control(
        "OI_POR_STRIKE_Y_LADO", oi_ok,
        ("interés abierto presente en calls y puts de cada strike" if oi_ok else
         (f"{len(sin_oi)} contratos sin interés abierto publicado" if sin_oi else
          "falta un lado entero: no hay calls o no hay puts")),
        call_contracts=por_lado[CALL], put_contracts=por_lado[PUT],
        strikes_without_oi=sorted(set(sin_oi))[:12]))

    # 5 · gamma válida por contrato
    sin_gamma = [k for r in contracts
                 if _f(r.get("gamma")) is None and (k := _f(r.get("strike"))) is not None]
    gamma_ok = bool(contracts) and not sin_gamma
    filas.append(_control(
        "GAMMA_VALIDA_POR_CONTRATO", gamma_ok,
        ("gamma válida en cada contrato de la cadena" if gamma_ok else
         (f"{len(sin_gamma)} contratos sin gamma" if contracts else
          "no hay contratos con griegas")),
        contracts=len(contracts), strikes_without_gamma=sorted(set(sin_gamma))[:12]))

    # 6 · precio con hora
    px = _f(spot)
    precio_ok = bool(px and px > 0 and price_as_of)
    filas.append(_control(
        "PRECIO_CON_HORA", precio_ok,
        ("precio del subyacente con su hora de captura" if precio_ok else
         ("el precio llega sin hora de captura" if px else "sin precio del subyacente")),
        spot=px, price_as_of=price_as_of))

    # 7 · IV, delta y multiplicador
    sin_iv = sum(1 for r in contracts
                 if _f(r.get("implied_volatility") if r.get("implied_volatility") is not None
                       else r.get("iv")) is None)
    sin_delta = sum(1 for r in contracts if _f(r.get("delta")) is None)
    mult = _f(multiplier)
    val_ok = bool(contracts) and sin_iv == 0 and sin_delta == 0 and bool(mult and mult > 0)
    filas.append(_control(
        "IV_DELTA_MULTIPLICADOR", val_ok,
        ("IV, delta y multiplicador disponibles para validar la gamma" if val_ok else
         "faltan IV, delta o multiplicador: la gamma no se puede validar, sólo usar"),
        contracts_without_iv=sin_iv, contracts_without_delta=sin_delta,
        multiplier=mult))

    # 8 · convención de posicionamiento
    conv_ok = bool(convention)
    filas.append(_control(
        "CONVENCION_DE_POSICIONAMIENTO", conv_ok,
        (f"convención declarada: {convention}. Es una CONVENCIÓN, no una medición "
         "del inventario del dealer" if conv_ok else
         "sin convención declarada: sólo se conoce la gamma matemática, no la "
         "presión de cobertura del dealer"),
        convention=convention, measured_dealer_inventory=False))

    # 9 · sin datos viejos mezclados
    edades = {k: _f(v) for k, v in ages.items() if _f(v) is not None}
    mas_vieja = max(edades.values()) if edades else None
    frescos = (mas_vieja is None or mas_vieja <= MAX_AGE_S)
    viejos_ok = bool(frescos and not mixed_expiries)
    filas.append(_control(
        "SIN_DATOS_VIEJOS_MEZCLADOS", viejos_ok,
        ("nada por encima del máximo de edad, y un solo vencimiento" if viejos_ok else
         (f"el dato más viejo tiene {mas_vieja:.0f} s (máximo {MAX_AGE_S:.0f} s)"
          if not frescos else "hay vencimientos mezclados en el cálculo")),
        ages_seconds=edades, max_age_s=MAX_AGE_S, mixed_expiries=mixed_expiries))

    # 10 · misma hora y misma fuente
    skew = (round(max(edades.values()) - min(edades.values()), 3)
            if len(edades) >= 2 else (0.0 if edades else None))
    fuentes = {k: v for k, v in sources.items() if v}
    misma_fuente = len({str(v) for k, v in fuentes.items()
                        if k in ("gamma", "open_interest")}) <= 1
    hora_ok = (skew is not None and skew <= MAX_SKEW_S
               and bool(fuentes) and misma_fuente)
    filas.append(_control(
        "MISMA_HORA_MISMA_FUENTE", hora_ok,
        ("OI, gamma y precio del mismo instante y con su fuente nombrada" if hora_ok
         else (f"desfase de {skew:.0f} s entre entradas (máximo {MAX_SKEW_S:.0f} s)"
               if skew is not None and skew > MAX_SKEW_S else
               ("gamma y OI vienen de fuentes distintas" if not misma_fuente else
                "las entradas no declaran hora ni fuente"))),
        skew_seconds=skew, max_skew_s=MAX_SKEW_S, sources=fuentes))

    return filas


# ═══════════════════════════════════════════════════════════ el resultado

def _wall_from(filas: List[Dict[str, Any]], *, side: str, spot: float,
               expiry: Optional[str], price_as_of: Optional[str],
               verdict: str, multiplier: float) -> Dict[str, Any]:
    """El muro de un lado: el strike de MAYOR Gamma Exposure, y nada más."""
    label = "CALL WALL" if side == CALL else "PUT WALL"
    kind = CALL_WALL if side == CALL else PUT_WALL
    validos = [f for f in filas if f.get("gex") is not None and f["gex"] > 0]
    if not validos:
        return {"ready": False, "side": kind, "label": label, "strike": None,
                "verdict": PROVISIONAL, "method": FORMULA,
                "detail": f"ningún strike de {side}s con Gamma Exposure medible",
                "expiry": expiry, "spot": spot, "price_as_of": price_as_of}
    mejor = validos[0]
    segundo = validos[1] if len(validos) > 1 else None
    margen = (round((mejor["gex"] - segundo["gex"]) / segundo["gex"] * 100.0, 2)
              if segundo and segundo["gex"] > 0 else None)
    return {
        "ready": True, "side": kind, "label": label,
        "strike": mejor["strike"],
        # Los cinco números que sostienen el muro, juntos y a la vista.
        "gex": mejor["gex"], "gamma": mejor["gamma"],
        "open_interest": mejor["open_interest"],
        "spot": spot, "price_as_of": price_as_of,
        "expiry": expiry, "multiplier": multiplier,
        "iv": mejor.get("iv"), "delta": mejor.get("delta"),
        "contracts": mejor.get("contracts"),
        "position": mejor.get("position"), "distance_pct": mejor.get("distance_pct"),
        # Un muro de calls por DEBAJO del precio ya fue atravesado: sigue siendo
        # el strike de mayor exposición —que es lo que mide la fórmula— y se
        # declara, en vez de esconderlo o de filtrarlo en silencio.
        "crossed": bool((side == CALL and mejor["strike"] < spot)
                        or (side == PUT and mejor["strike"] > spot)),
        "runner_up": (None if segundo is None else
                      {"strike": segundo["strike"], "gex": segundo["gex"]}),
        "margin_over_runner_up_pct": margen,
        "verdict": verdict, "method": FORMULA,
        "positioning_convention": POSITIONING_CONVENTION,
        "ranked": [{"strike": f["strike"], "gex": f["gex"],
                    "open_interest": f["open_interest"], "gamma": f["gamma"]}
                   for f in validos[:8]],
        "updated_at": _now_iso(),
    }


def walls(symbol: str, *, contract_rows: Sequence[Dict[str, Any]],
          spot: Optional[float], price_as_of: Optional[str] = None,
          expiry: Optional[str] = None, expiry_policy: str = NEAREST,
          multiplier: float = MULTIPLIER_DEFAULT,
          convention: Optional[str] = POSITIONING_CONVENTION,
          ages: Optional[Dict[str, Any]] = None,
          sources: Optional[Dict[str, Any]] = None,
          provider_exposure: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Call Wall y Put Wall con su veredicto, desde las griegas por contrato.

    `contract_rows` son contratos con `strike`, `option_type`, `expiration`,
    `gamma`, `open_interest` y —para poder validar— `implied_volatility` y
    `delta`. Es lo que publica Quant Data por contrato, que es la fuente
    principal; la exposición por strike del proveedor entra sólo como
    contraste, nunca como el número del muro.
    """
    sym = str(symbol or "").upper()
    px = _f(spot)
    contratos_todos = dedupe_contracts(contract_rows or [])
    venc, motivo = select_expiry(contratos_todos, policy=expiry_policy,
                                 requested=expiry)
    contratos = [r for r in contratos_todos if _expiry_of(r) == venc] if venc else []
    mezclados = len({_expiry_of(r) for r in contratos}) > 1
    cadena = chain_report(contratos, spot=px)

    controles = audit(contracts=contratos, chain=cadena, expiry=venc,
                      expiry_reason=motivo,
                      expiries_available=len(available_expiries(contratos_todos)),
                      mixed_expiries=mezclados, spot=px, price_as_of=price_as_of,
                      multiplier=multiplier, convention=convention,
                      ages=ages, sources=sources)
    fallidos = [c["control"] for c in controles if not c["ok"]]
    veredicto = CONFIRMED if not fallidos else PROVISIONAL

    out: Dict[str, Any] = {
        "contract": CONTRACT, "symbol": sym, "ready": False,
        "verdict": veredicto, "failed_controls": fallidos, "controls": controles,
        "formula": FORMULA, "method": FORMULA,
        "expiry": venc, "expiry_reason": motivo, "expiry_policy": expiry_policy,
        "expiries_available": available_expiries(contratos_todos),
        "spot": px, "price_as_of": price_as_of, "multiplier": _f(multiplier),
        "positioning_convention": convention,
        "sides_kept_separate": True,
        "chain": cadena,
        "contracts_used": len(contratos),
        "contracts_received": len(contract_rows or []),
        "note": ("una wall es una concentración de cobertura probable, no una "
                 "barrera garantizada"),
        "updated_at": _now_iso(),
    }
    if px is None or not contratos:
        out[CALL_WALL] = {"ready": False, "side": CALL_WALL, "label": "CALL WALL",
                          "strike": None, "verdict": PROVISIONAL,
                          "detail": ("sin precio del subyacente" if px is None
                                     else "sin contratos del vencimiento elegido")}
        out[PUT_WALL] = dict(out[CALL_WALL], side=PUT_WALL, label="PUT WALL")
        return out

    calls = rank_side(contratos, side=CALL, spot=px, multiplier=multiplier)
    puts = rank_side(contratos, side=PUT, spot=px, multiplier=multiplier)
    out[CALL_WALL] = _wall_from(calls, side=CALL, spot=px, expiry=venc,
                                price_as_of=price_as_of, verdict=veredicto,
                                multiplier=_f(multiplier) or MULTIPLIER_DEFAULT)
    out[PUT_WALL] = _wall_from(puts, side=PUT, spot=px, expiry=venc,
                               price_as_of=price_as_of, verdict=veredicto,
                               multiplier=_f(multiplier) or MULTIPLIER_DEFAULT)
    out["ready"] = bool(out[CALL_WALL].get("ready") or out[PUT_WALL].get("ready"))
    out["provider_crosscheck"] = _crosscheck(out, provider_exposure)
    return out


def _crosscheck(out: Dict[str, Any],
                provider_exposure: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, Any]:
    """Contraste con la exposición por strike del proveedor.

    NO decide el muro. Y no siempre es comparable: la exposición por strike del
    proveedor agrega TODOS los vencimientos, y este cálculo usa uno. Cuando no
    lo es, se dice —dar un cociente entre dos cosas distintas sería peor que no
    darlo.
    """
    filas = [r for r in (provider_exposure or []) if isinstance(r, dict)]
    if not filas:
        return {"available": False,
                "detail": "el proveedor no sirvió exposición por strike"}
    por_k = {k: r for r in filas if (k := _f(r.get("strike"))) is not None}
    lados = {}
    for kind, campo in ((CALL_WALL, "call_gex"), (PUT_WALL, "put_gex")):
        muro = out.get(kind) or {}
        k = _f(muro.get("strike"))
        fila = por_k.get(k) if k is not None else None
        prov = _f((fila or {}).get(campo))
        propio = _f(muro.get("gex"))
        lados[kind] = {
            "strike": k, "provider_exposure": prov, "own_gex": propio,
            "ratio": (round(propio / prov, 4) if prov and propio and prov != 0
                      else None),
        }
    return {"available": True, "comparable": False,
            "detail": ("la exposición del proveedor agrega todos los vencimientos; "
                       "este cálculo usa uno, así que el cociente es una referencia, "
                       "no una igualdad esperada"),
            "sides": lados}


__all__ = [
    "walls", "gamma_exposure", "rank_side", "select_expiry", "available_expiries",
    "dedupe_contracts", "chain_report", "audit",
    "CONTRACT", "FORMULA", "CONFIRMED", "PROVISIONAL", "CONTROL_KEYS",
    "POSITIONING_CONVENTION", "MULTIPLIER_DEFAULT", "MOVE_FRACTION",
    "MAX_SKEW_S", "MAX_AGE_S", "MIN_OTM_COVERAGE_PCT", "MIN_STRIKES_PER_SIDE",
    "NEAREST", "WEEKLY_FRIDAY", "CALL", "PUT", "CALL_WALL", "PUT_WALL",
]

"""CRITICIDAD POR CONSUMIDOR · ITM QUANT v1.59.0

═══════════════════════════════════════════════════════════════════════════
`OPTIONAL` NO ES UNA PROPIEDAD DEL ENDPOINT
═══════════════════════════════════════════════════════════════════════════

Hasta aquí una llamada fallida se marcaba `OPTIONAL` o `DEGRADED` **por sí
misma**, mirando sólo al endpoint. Eso es imposible de acertar, porque la
criticidad no vive en el dato: vive en el par **(dato, quien lo consume)**.

    gamma falla
      · para una tarjeta con respaldo propio        → opcional, se degrada y ya
      · para Call Wall / Put Wall                   → NO es opcional: sin gamma
                                                      no hay fórmula que aplicar

El mismo fallo, dos consecuencias. Con una sola etiqueta global hay que elegir
una de las dos, y las dos elecciones son malas: marcarlo opcional hace que las
Walls desaparezcan en silencio; marcarlo crítico llena la pantalla de alarmas
por una tarjeta que tenía respaldo.

═══════════════════════════════════════════════════════════════════════════
CÓMO SE ARREGLA
═══════════════════════════════════════════════════════════════════════════

Cada consumidor **declara** de qué depende y con qué nivel. La severidad de un
fallo se DERIVA de esa declaración:

    ningún consumidor lo exige   → OPTIONAL
    algún consumidor lo exige    → DEGRADED, nombrando a quién deja sin datos

Y cada consumidor puede decir su propio estado sin preguntar al endpoint:

    READY      tiene todo lo obligatorio
    DEGRADED   tiene lo obligatorio y le falta algo opcional
    BLOCKED    le falta algo OBLIGATORIO, y se dice cuál

Lo que NO hace este módulo: pedir datos, cachear, ni decidir cadencias. Es una
declaración y un evaluador puro; la autoridad de cada magnitud sigue donde
estaba.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

REQUIRED = "REQUIRED"
OPTIONAL = "OPTIONAL"

#: Estados de un consumidor.
READY = "READY"
DEGRADED = "DEGRADED"
BLOCKED = "BLOCKED"
#: Y el cuarto, que es el que impide mentir: falta MEDIR una dependencia
#: obligatoria. «No lo he medido» no es «no está»: declarar BLOCKED por un dato
#: que nadie preguntó pintaría de rojo una sección que funciona.
UNKNOWN = "UNKNOWN"

#: De qué carril sale cada dependencia. El evaluador no lo usa para decidir
#: nada: sirve para saber A QUIÉN preguntarle por lo que falta medir.
PAGES_LANE = "PAGES"        # herramienta del catálogo de páginas de Quant Data
HUB_LANE = "HUB"            # conjunto derivado del Data Hub
TERMINAL_LANE = "TERMINAL"  # lo que resuelve la terminal (vencimiento, precio)

#: Estados de disponibilidad que CUENTAN como dato utilizable. `STALE` entra a
#: propósito: un dato viejo declarado sigue sosteniendo una lectura, y quitarlo
#: dejaría la pantalla en blanco por un ciclo perdido.
USABLE = frozenset({"LIVE", "STALE", "STALE_LKG", "DATA_OK", "OK"})

#: Estados que significan «no hay dato» sin que sea una avería.
EMPTY = frozenset({"NO_DATA", "NO_PROVIDER_DATA", "MARKET_CLOSED"})

#: Estados SIN VEREDICTO todavía: la herramienta no ha llegado a servir ni ha
#: fallado. No son un fallo y no pueden bloquear a nadie —una sección no está
#: rota porque su dato siga en la cola—, pero tampoco son un dato disponible.
#: `COOLDOWN` queda fuera a propósito: viene de fallar, y eso sí es un veredicto.
PENDING = frozenset({"SCHEDULED", "RUNNING", "WAITING_RATE_LIMIT",
                     "PENDING", "PENDIENTE"})


@dataclass(frozen=True)
class Dependency:
    """Una dependencia declarada, con el nivel que le da ESTE consumidor."""

    tool: str
    level: str
    why: str
    lane: str = "PAGES"

    @property
    def required(self) -> bool:
        return self.level == REQUIRED


@dataclass(frozen=True)
class ConsumerContract:
    """Lo que un consumidor necesita, y qué hace cuando le falta algo."""

    consumer: str
    title: str
    dependencies: Tuple[Dependency, ...]
    degraded_behaviour: str
    blocked_behaviour: str

    def required(self) -> Tuple[str, ...]:
        return tuple(d.tool for d in self.dependencies if d.required)

    def optional(self) -> Tuple[str, ...]:
        return tuple(d.tool for d in self.dependencies if not d.required)


def _dep(tool: str, level: str, why: str,
         lane: str = PAGES_LANE) -> Dependency:
    return Dependency(tool=tool, level=level, why=why, lane=lane)


#: LOS CONTRATOS. Cada uno dice de qué depende y qué hace sin ello.
CONSUMERS: Dict[str, ConsumerContract] = {
    "WALLS": ConsumerContract(
        consumer="WALLS",
        title="Call Wall / Put Wall",
        dependencies=(
            _dep("contract_greeks", REQUIRED,
                 "gamma y OI POR CONTRATO: son dos de los cinco factores de la "
                 "fórmula, y sin ellos no hay nada que calcular", HUB_LANE),
            _dep("underlying_price", REQUIRED,
                 "el precio entra AL CUADRADO en la exposición", TERMINAL_LANE),
            _dep("expiry_selection", REQUIRED,
                 "un muro sin vencimiento declarado es un nivel sin fecha",
                 TERMINAL_LANE),
            _dep("gex_by_strike", OPTIONAL,
                 "contraste con la exposición agregada del proveedor; no decide "
                 "el muro"),
        ),
        degraded_behaviour=("se publica el muro sin el contraste del proveedor y "
                            "se declara en el veredicto"),
        blocked_behaviour=("NO CALCULABLE, nombrando el ingrediente que falta; "
                           "si hay un snapshot válido reciente se publica como "
                           "LKG con su edad")),
    "TRACE": ConsumerContract(
        consumer="TRACE",
        title="TRACE · mapa de intervalos",
        dependencies=(
            _dep("interval_map_gamma", REQUIRED,
                 "es la AUTORIDAD del campo: el heatmap es ese dato, no una "
                 "reconstrucción"),
            _dep("underlying_price", REQUIRED, "ancla el eje de precio",
                 TERMINAL_LANE),
            _dep("gex_by_strike", OPTIONAL, "perfil lateral por strike"),
            _dep("oi_by_strike", OPTIONAL, "perfil lateral de interés abierto"),
            _dep("interval_map_delta", OPTIONAL, "vista alternativa del campo"),
        ),
        degraded_behaviour="se dibuja el campo sin los perfiles laterales",
        blocked_behaviour="TRACE dice qué falta; no dibuja un campo inventado"),
    "DARK_POOL": ConsumerContract(
        consumer="DARK_POOL",
        title="Dark Pool",
        dependencies=(
            _dep("dark_flow", OPTIONAL, "carril independiente de flujo oscuro"),
            _dep("dark_pool_levels", OPTIONAL, "carril independiente de niveles"),
            _dep("equity_prints", OPTIONAL, "carril independiente de impresiones"),
        ),
        degraded_behaviour=("cada carril vive por su cuenta: el que falla no "
                            "apaga a los otros dos"),
        blocked_behaviour="los tres carriles caídos a la vez, y se nombran"),
    "EXPOSICION": ConsumerContract(
        consumer="EXPOSICION",
        title="Exposición por strike y vencimiento",
        dependencies=(
            _dep("gex_by_strike", REQUIRED, "la magnitud principal de la sección"),
            _dep("dex_by_strike", OPTIONAL, "segunda griega de la vista"),
            _dep("vex_by_strike", OPTIONAL, "tercera griega de la vista"),
            _dep("chex_by_strike", OPTIONAL, "cuarta griega de la vista"),
            _dep("gex_by_expiration", OPTIONAL, "corte por vencimiento"),
        ),
        degraded_behaviour="se muestran las griegas que llegaron, nombrando las que no",
        blocked_behaviour="sin GEX por strike no hay sección que dibujar"),
    "FLUJO": ConsumerContract(
        consumer="FLUJO",
        title="Flujo de órdenes",
        dependencies=(
            _dep("options_order_flow_raw", REQUIRED,
                 "la cinta con griegas por contrato es la base del lado agresor"),
            _dep("net_flow", OPTIONAL, "curva agregada de prima neta"),
            _dep("net_drift", OPTIONAL, "curva de deriva"),
        ),
        degraded_behaviour="la cinta se dibuja sin las curvas agregadas",
        blocked_behaviour="sin cinta no hay flujo que clasificar"),
    "VOLATILIDAD": ConsumerContract(
        consumer="VOLATILIDAD",
        title="Volatilidad",
        dependencies=(
            _dep("iv_rank", OPTIONAL, "posición de la IV en su rango"),
            _dep("volatility_skew", OPTIONAL, "sonrisa por strike"),
            _dep("term_structure", OPTIONAL, "estructura temporal"),
            _dep("volatility_drift", OPTIONAL, "deriva intradía"),
        ),
        degraded_behaviour="cada vista aparece si su dato llegó",
        blocked_behaviour="sin ninguna de las cuatro no hay sección"),
    "INTERES_ABIERTO": ConsumerContract(
        consumer="INTERES_ABIERTO",
        title="Interés abierto",
        dependencies=(
            _dep("oi_by_strike", REQUIRED, "el reparto por strike es la vista"),
            _dep("oi_by_expiration", OPTIONAL, "corte por vencimiento"),
            _dep("oi_change", OPTIONAL, "variación contra el día anterior"),
            _dep("max_pain", OPTIONAL, "referencia de dolor máximo"),
        ),
        degraded_behaviour="se dibuja el reparto sin los cortes secundarios",
        blocked_behaviour="sin OI por strike no hay reparto"),
}


# ═══════════════════════════════════════════════════════ índice inverso

def criticality(tool: str) -> Dict[str, Any]:
    """Para QUIÉN es obligatorio este dato, y para quién opcional.

    Es el índice que convierte «gamma falló» en «gamma falló y deja a WALLS sin
    fórmula». Sin él, la severidad de un fallo hay que adivinarla.
    """
    t = str(tool or "")
    obligatorio: List[str] = []
    opcional: List[str] = []
    motivos: Dict[str, str] = {}
    for contrato in CONSUMERS.values():
        for dep in contrato.dependencies:
            if dep.tool != t:
                continue
            motivos[contrato.consumer] = dep.why
            (obligatorio if dep.required else opcional).append(contrato.consumer)
    nivel = REQUIRED if obligatorio else (OPTIONAL if opcional else "UNUSED")
    return {
        "tool": t,
        "level": nivel,
        "critical_for": sorted(obligatorio),
        "optional_for": sorted(opcional),
        "why": motivos,
        # La severidad ya no se decide mirando al endpoint: se DERIVA de si
        # alguien lo exige. Un fallo que no deja a nadie sin nada es opcional;
        # uno que sí, no puede serlo por mucho que la llamada parezca menor.
        "severity": ("DEGRADED" if obligatorio else "OPTIONAL"),
        "detail": (f"obligatorio para {', '.join(sorted(obligatorio))}"
                   if obligatorio else
                   (f"opcional para {', '.join(sorted(opcional))}" if opcional
                    else "ningún consumidor declarado lo usa")),
    }


def severity_for(tool: str) -> str:
    """`DEGRADED` si algún consumidor lo exige; `OPTIONAL` si no."""
    return criticality(tool)["severity"]


# ═══════════════════════════════════════════════════════ evaluación

def _usable(estado: Any) -> bool:
    return str(estado or "").upper() in USABLE


def evaluate(consumer: str,
             availability: Mapping[str, Any]) -> Dict[str, Any]:
    """Estado de UN consumidor a partir de la disponibilidad de sus datos.

    `availability` es `{herramienta: estado}` con los estados que publica el
    Hub (`LIVE`, `STALE`, `NO_DATA`, `PROVIDER_ERROR`, …).

    Hay una distinción que este evaluador NO puede perder: una dependencia que
    alguien midió y salió mal no es lo mismo que una que **nadie midió**. La
    primera bloquea; la segunda deja al consumidor en `UNKNOWN`, nombrando el
    carril al que hay que preguntarle. Tratar la segunda como la primera pintaría
    de rojo una sección que está funcionando, y ésa es exactamente la clase de
    falso negativo que este bloque existe para quitar.
    """
    contrato = CONSUMERS.get(str(consumer or "").upper())
    if contrato is None:
        return {"consumer": str(consumer), "state": BLOCKED,
                "detail": "consumidor no declarado", "missing_required": [],
                "missing_optional": [], "unmeasured": [], "known": False}

    faltan_obl: List[Dict[str, str]] = []
    faltan_opt: List[Dict[str, str]] = []
    sin_medir: List[Dict[str, str]] = []
    presentes: List[str] = []
    for dep in contrato.dependencies:
        if dep.tool not in availability:
            sin_medir.append({"tool": dep.tool, "lane": dep.lane,
                              "level": dep.level, "why": dep.why,
                              "state": "NO_MEDIDO",
                              "reason": "este carril no mide este dato"})
            continue
        estado = availability.get(dep.tool)
        if str(estado or "").upper() in PENDING:
            sin_medir.append({"tool": dep.tool, "lane": dep.lane,
                              "level": dep.level, "why": dep.why,
                              "state": str(estado),
                              "reason": "aún sin veredicto: no ha servido ni ha fallado"})
            continue
        if _usable(estado):
            presentes.append(dep.tool)
            continue
        fila = {"tool": dep.tool, "state": str(estado or "MISSING"),
                "lane": dep.lane, "why": dep.why}
        (faltan_obl if dep.required else faltan_opt).append(fila)

    obl_sin_medir = [f for f in sin_medir if f["level"] == REQUIRED]

    if faltan_obl:
        estado_final, comportamiento = BLOCKED, contrato.blocked_behaviour
        detalle = ("falta lo obligatorio: "
                   + ", ".join(f"{f['tool']} ({f['state']})" for f in faltan_obl))
    elif obl_sin_medir or (sin_medir and not presentes):
        # Sin una sola dependencia medida tampoco se puede decir READY: un
        # consumidor de dependencias todas opcionales saldría «listo» sin que
        # nadie haya comprobado ninguna.
        pendientes = obl_sin_medir or sin_medir
        estado_final, comportamiento = UNKNOWN, ""
        detalle = ("sin veredicto todavía: "
                   + ", ".join(f"{f['tool']} [{f['state']}, carril {f['lane']}]"
                               for f in pendientes)
                   + ": sin ese dato no se puede afirmar ni que sirve ni que falla")
    elif faltan_opt:
        estado_final, comportamiento = DEGRADED, contrato.degraded_behaviour
        detalle = ("completo lo obligatorio; falta lo opcional: "
                   + ", ".join(f"{f['tool']} ({f['state']})" for f in faltan_opt))
    else:
        estado_final, comportamiento = READY, ""
        detalle = "todas las dependencias declaradas están disponibles"

    return {
        "consumer": contrato.consumer,
        "title": contrato.title,
        "state": estado_final,
        "detail": detalle,
        "behaviour": comportamiento,
        "missing_required": faltan_obl,
        "missing_optional": faltan_opt,
        "unmeasured": sin_medir,
        "present": sorted(presentes),
        "required": list(contrato.required()),
        "optional": list(contrato.optional()),
        "known": True,
    }


def evaluate_all(availability: Mapping[str, Any]) -> Dict[str, Any]:
    """Todos los consumidores a la vez, con el resumen que lee el Auditor."""
    disponible = resolve_availability(availability)
    filas = [evaluate(nombre, disponible) for nombre in sorted(CONSUMERS)]
    bloqueados = [f["consumer"] for f in filas if f["state"] == BLOCKED]
    degradados = [f["consumer"] for f in filas if f["state"] == DEGRADED]
    return {
        "contract": "ITMQ_CONSUMER_CRITICALITY_V1",
        "consumers": filas,
        "blocked": bloqueados,
        "degraded": degradados,
        "unknown": [f["consumer"] for f in filas if f["state"] == UNKNOWN],
        "ready": [f["consumer"] for f in filas if f["state"] == READY],
        "policy": ("la criticidad es del PAR (dato, consumidor): el mismo fallo "
                   "es opcional para quien tiene respaldo y bloqueante para "
                   "quien no lo tiene"),
    }


#: Dependencias que NO son una herramienta del catálogo sino un conjunto
#: derivado, y de qué herramientas sale cada una. Se declara aquí para que el
#: Auditor no tenga que saberlo y para que nadie lo adivine dos veces distintas.
SATISFIED_BY: Dict[str, Tuple[str, ...]] = {
    # Las griegas por contrato viajan en las filas del order flow: el Hub las
    # extrae de ahí, no de un endpoint propio.
    "contract_greeks": ("options_order_flow_raw", "options_order_flow"),
}

#: Orden de preferencia al derivar un estado de sus satisfactores.
_PREFERENCIA = ("LIVE", "DATA_OK", "OK", "STALE", "STALE_LKG")


def resolve_availability(reported: Mapping[str, Any]) -> Dict[str, Any]:
    """Completa los nombres DERIVADOS con el estado de quien los satisface.

    `contract_greeks` no se pide: se extrae del order flow. Si nadie informó de
    él pero sí de sus fuentes, su estado es el MEJOR de ellas —basta una para
    tener las griegas—. Si tampoco hay fuentes, se deja sin informar: que falte
    la medición es una respuesta distinta de que falte el dato.
    """
    fuera = dict(reported or {})
    for derivado, fuentes in SATISFIED_BY.items():
        if derivado in fuera:
            continue
        estados = [str(fuera[f]) for f in fuentes if f in fuera]
        if not estados:
            continue
        mejor = next((e for e in _PREFERENCIA
                      if any(str(x).upper() == e for x in estados)), None)
        fuera[derivado] = mejor or estados[0]
    return fuera


def tools_declared() -> List[str]:
    """Toda herramienta que algún consumidor declara. Sin duplicados."""
    return sorted({d.tool for c in CONSUMERS.values() for d in c.dependencies})


__all__ = [
    "CONSUMERS", "ConsumerContract", "Dependency", "criticality", "severity_for",
    "evaluate", "evaluate_all", "tools_declared", "resolve_availability",
    "SATISFIED_BY", "PAGES_LANE", "HUB_LANE", "TERMINAL_LANE",
    "REQUIRED", "OPTIONAL", "READY", "DEGRADED", "BLOCKED", "UNKNOWN",
    "USABLE", "EMPTY", "PENDING",
]

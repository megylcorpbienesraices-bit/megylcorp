"""WALL SNAPSHOT · el estado coherente del que salen las Walls · v1.59.0

═══════════════════════════════════════════════════════════════════════════
«SIN CÁLCULO DE MUROS EN ESTE CICLO» NO ES UN DIAGNÓSTICO
═══════════════════════════════════════════════════════════════════════════

Las Walls se calculaban con lo que hubiera llegado **en el ciclo de sondeo
actual**. Si un endpoint se perdía —un timeout, un turno sin cuota— el panel
salía vacío con esa frase, que no dice nada:

    · ¿faltó la gamma, el OI, el vencimiento o el precio?
    · ¿hubo Walls válidas hace treinta segundos?
    · ¿el dato existe y no se usó, o no existe?

Y además ataba una lectura estructural —que cambia despacio— al ritmo de un
ciclo de red, que falla a menudo. Un vencimiento no desaparece porque una
petición muera.

═══════════════════════════════════════════════════════════════════════════
LO QUE HACE ESTE MÓDULO
═══════════════════════════════════════════════════════════════════════════

Reúne los ingredientes en UN objeto coherente —todos del mismo instante, con su
procedencia— y decide en cuál de los tres estados está:

    COMPLETO      están los cinco ingredientes → se calcula
    LKG           este ciclo perdió algo, pero hay un snapshot anterior válido
                  dentro de la política de frescura → se publica con su EDAD
    NO_CALCULABLE nunca hubo snapshot suficiente → se nombran los ingredientes
                  que faltan, uno a uno

Los ingredientes, y cómo se llaman cuando faltan:

    GAMMA · OI · VENCIMIENTO · SPOT · COBERTURA · MULTIPLICADOR

**No toca la fórmula.** `wall_gex` sigue siendo la única autoridad del cálculo:
este módulo prepara su entrada y guarda la última buena. La matemática de
v1.57.2 queda intacta.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

CONTRACT = "ITMQ_WALL_SNAPSHOT_V1"

#: Estados del snapshot.
COMPLETO = "COMPLETO"
LKG = "LKG"
NO_CALCULABLE = "NO_CALCULABLE"

#: Los ingredientes, con el nombre EXACTO que se publica cuando faltan.
GAMMA = "GAMMA"
OI = "OI"
VENCIMIENTO = "VENCIMIENTO"
SPOT = "SPOT"
COBERTURA = "COBERTURA"
MULTIPLICADOR = "MULTIPLICADOR"
INGREDIENTES = (GAMMA, OI, VENCIMIENTO, SPOT, COBERTURA, MULTIPLICADOR)

#: Hasta esta edad, un snapshot anterior sigue sosteniendo las Walls. Por
#: encima, deja de ser «lo último bueno» y pasa a ser contexto histórico.
LKG_FRESCO_S = 180.0

#: Y a partir de aquí ni siquiera como contexto: la estructura del día ya es otra.
LKG_MAXIMO_S = 900.0

#: Contratos mínimos por lado para considerar que hay cobertura de cadena.
MIN_CONTRATOS_POR_LADO = 3


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(),
                                  tz=timezone.utc).isoformat()


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _side(row: Mapping[str, Any]) -> str:
    raw = str(row.get("option_type") or row.get("type") or "").strip().lower()
    return "call" if raw.startswith("c") else ("put" if raw.startswith("p") else "")


def build(symbol: str, *, contract_rows: Sequence[Mapping[str, Any]],
          spot: Any, price_as_of: Optional[str], expiry: Optional[str],
          source: str = "", multiplier_default: float = 100.0,
          ages: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Arma el snapshot y dice qué ingrediente falta, por su nombre.

    No calcula ninguna wall: sólo comprueba que los ingredientes estén y sean
    coherentes entre sí. El cálculo lo hace `wall_gex` con esta entrada.
    """
    sym = str(symbol or "").upper()
    filas = [r for r in (contract_rows or []) if isinstance(r, dict)]
    px = _f(spot)

    faltan: List[Dict[str, str]] = []

    def falta(nombre: str, motivo: str) -> None:
        faltan.append({"ingredient": nombre, "detail": motivo})

    # ── vencimiento ───────────────────────────────────────────────────────
    vencimientos = sorted({str(r.get("expiration") or r.get("expiry") or "")[:10]
                           for r in filas if (r.get("expiration") or r.get("expiry"))})
    elegido = (str(expiry)[:10] if expiry else (vencimientos[0] if vencimientos else None))
    if not elegido:
        falta(VENCIMIENTO, "ninguna fila trae fecha de vencimiento")
    del_vencimiento = [r for r in filas
                       if str(r.get("expiration") or r.get("expiry") or "")[:10] == elegido]

    # ── gamma y OI por contrato ───────────────────────────────────────────
    con_gamma = [r for r in del_vencimiento if _f(r.get("gamma")) is not None]
    con_oi = [r for r in del_vencimiento if _f(r.get("open_interest")) is not None]
    if not del_vencimiento:
        falta(GAMMA, "no hay contratos del vencimiento elegido")
    else:
        if not con_gamma:
            falta(GAMMA, "ningún contrato del vencimiento trae gamma")
        if not con_oi:
            falta(OI, "ningún contrato del vencimiento trae interés abierto")

    # ── precio ────────────────────────────────────────────────────────────
    if px is None or px <= 0:
        falta(SPOT, "sin precio del subyacente")
    elif not price_as_of:
        falta(SPOT, "el precio llega sin hora de captura y entra al cuadrado")

    # ── cobertura de cadena ───────────────────────────────────────────────
    calls = {_f(r.get("strike")) for r in del_vencimiento if _side(r) == "call"}
    puts = {_f(r.get("strike")) for r in del_vencimiento if _side(r) == "put"}
    calls.discard(None)
    puts.discard(None)
    if len(calls) < MIN_CONTRATOS_POR_LADO or len(puts) < MIN_CONTRATOS_POR_LADO:
        falta(COBERTURA,
              f"{len(calls)} strikes con calls y {len(puts)} con puts; hacen "
              f"falta {MIN_CONTRATOS_POR_LADO} por lado")

    # ── multiplicador ─────────────────────────────────────────────────────
    multiplicadores = sorted({
        _f(r.get("multiplier") or r.get("contract_size") or multiplier_default)
        for r in del_vencimiento} - {None})
    if not multiplicadores:
        falta(MULTIPLICADOR, "ningún contrato declara multiplicador ni hay defecto")

    completo = not faltan
    return {
        "contract": CONTRACT,
        "symbol": sym,
        "state": COMPLETO if completo else NO_CALCULABLE,
        "ready": completo,
        "expiry": elegido,
        "expiries_available": vencimientos,
        "contracts": len(del_vencimiento),
        "contracts_with_gamma": len(con_gamma),
        "contracts_with_oi": len(con_oi),
        "strikes_calls": len(calls),
        "strikes_puts": len(puts),
        "multipliers": multiplicadores,
        "spot": px,
        "price_as_of": price_as_of,
        "source": source or "QUANTDATA_CONTRACT_GREEKS",
        "ages": dict(ages or {}),
        "built_at": _iso(),
        "built_at_ts": _now(),
        "missing": faltan,
        "missing_names": [f["ingredient"] for f in faltan],
        "rows": del_vencimiento if completo else [],
    }


class WallSnapshotStore:
    """El último snapshot COMPLETO por símbolo, con su edad.

    Es lo que permite que una wall sobreviva a un ciclo perdido: la estructura
    cambia despacio y una petición que muere no la borra. Lo que no hace es
    esconder la edad — un muro servido desde aquí lo dice siempre.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._por_simbolo: Dict[str, Dict[str, Any]] = {}

    def put(self, snapshot: Mapping[str, Any]) -> None:
        if not snapshot or not snapshot.get("ready"):
            return                      # sólo se guarda lo COMPLETO
        sym = str(snapshot.get("symbol") or "").upper()
        if not sym:
            return
        with self._lock:
            self._por_simbolo[sym] = dict(snapshot)

    def get(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            guardado = self._por_simbolo.get(str(symbol or "").upper())
            return dict(guardado) if guardado else None

    def age_seconds(self, symbol: str) -> Optional[float]:
        guardado = self.get(symbol)
        if not guardado:
            return None
        return max(0.0, _now() - float(guardado.get("built_at_ts") or 0.0))

    def clear_symbol(self, symbol: str) -> None:
        """Al cambiar de activo, el snapshot del anterior no vale para el nuevo."""
        with self._lock:
            self._por_simbolo.pop(str(symbol or "").upper(), None)

    def reset(self) -> None:
        with self._lock:
            self._por_simbolo.clear()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            filas = []
            for sym, s in sorted(self._por_simbolo.items()):
                filas.append({
                    "symbol": sym, "expiry": s.get("expiry"),
                    "contracts": s.get("contracts"),
                    "age_seconds": round(max(0.0, _now() - float(
                        s.get("built_at_ts") or 0.0)), 2),
                    "built_at": s.get("built_at"),
                })
        return {"contract": CONTRACT, "stored": filas,
                "fresh_seconds": LKG_FRESCO_S, "max_seconds": LKG_MAXIMO_S}


SNAPSHOTS = WallSnapshotStore()


def resolve(symbol: str, actual: Mapping[str, Any], *,
            store: Optional[WallSnapshotStore] = None) -> Dict[str, Any]:
    """Decide con qué snapshot se calculan las Walls y en qué estado queda.

    Tres salidas, y ninguna es una caja vacía:

        COMPLETO       el ciclo trajo todo
        LKG            el ciclo perdió algo; hay uno anterior dentro de la
                       política de frescura, y se publica CON SU EDAD
        NO_CALCULABLE  no hay ni uno ni otro, y se nombran los ingredientes
    """
    st = store or SNAPSHOTS
    sym = str(symbol or "").upper()
    if actual.get("ready"):
        st.put(actual)
        return {**actual, "state": COMPLETO, "from_lkg": False,
                "age_seconds": 0.0,
                "detail": "el ciclo trajo los seis ingredientes"}

    guardado = st.get(sym)
    edad = st.age_seconds(sym)
    if guardado and edad is not None and edad <= LKG_MAXIMO_S:
        fresco = edad <= LKG_FRESCO_S
        return {
            **guardado,
            "state": LKG,
            "from_lkg": True,
            "age_seconds": round(edad, 2),
            "stale": not fresco,
            "missing": list(actual.get("missing") or []),
            "missing_names": list(actual.get("missing_names") or []),
            "detail": (f"el ciclo perdió {', '.join(actual.get('missing_names') or []) or 'un ingrediente'}; "
                       f"se publica el último snapshot válido de hace {edad:.0f} s"),
        }

    return {
        **actual,
        "state": NO_CALCULABLE,
        "from_lkg": False,
        "age_seconds": None,
        "detail": ("nunca hubo un snapshot suficiente: faltan "
                   + (", ".join(actual.get("missing_names") or []) or "ingredientes")),
    }


def verdict_label(resuelto: Mapping[str, Any], wall_verdict: str = "") -> str:
    """La etiqueta que ve el operador, con la edad cuando corresponde."""
    estado = str(resuelto.get("state") or "")
    if estado == NO_CALCULABLE:
        faltan = ", ".join(resuelto.get("missing_names") or []) or "ingredientes"
        return f"NO CALCULABLE · falta {faltan}"
    if estado == LKG:
        edad = resuelto.get("age_seconds")
        viejo = resuelto.get("stale")
        base = "WALL PROVISIONAL · STALE" if viejo else f"{wall_verdict or 'WALL'} · LKG"
        return f"{base} · edad {float(edad or 0):.0f}s"
    return wall_verdict or "WALL"


__all__ = [
    "build", "resolve", "verdict_label", "WallSnapshotStore", "SNAPSHOTS",
    "CONTRACT", "COMPLETO", "LKG", "NO_CALCULABLE", "INGREDIENTES",
    "GAMMA", "OI", "VENCIMIENTO", "SPOT", "COBERTURA", "MULTIPLICADOR",
    "LKG_FRESCO_S", "LKG_MAXIMO_S", "MIN_CONTRATOS_POR_LADO",
]

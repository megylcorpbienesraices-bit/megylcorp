"""FlowViewModel · política ÚNICA de frescura para FLUJO DE ÓRDENES.

═══════════════════════════════════════════════════════════════════════════
EL DEFECTO QUE ESTO ELIMINA
═══════════════════════════════════════════════════════════════════════════

La sección borraba información válida porque el ÚLTIMO ciclo no trajo prints
nuevos. En pantalla se veía a la vez:

    ESTADO            DATO ANTIGUO · 405 buckets · último hace 3610 min
    PRIMA TOTAL       SIN DATOS
    PRIMA COMPRADORA  SIN DATOS
    PRIMA VENDEDORA   SIN DATOS

El estado sabía que había 405 buckets y las tarjetas decían que no había
nada. Dos afirmaciones contradictorias sobre el mismo dato.

La causa es de diseño: **un único estado global**. Si el ciclo venía vacío,
TODO el módulo se vaciaba, aunque QFLOW, Net Flow o los buckets siguieran
siendo perfectamente válidos.

═══════════════════════════════════════════════════════════════════════════
LA POLÍTICA
═══════════════════════════════════════════════════════════════════════════

    DATO NUEVO                        →  mostrar nuevo            LIVE
    SIN DATO NUEVO + EXISTE LKG       →  mostrar LKG + edad       STALE
    MERCADO CERRADO + SESIÓN PREVIA   →  mostrar histórico        HISTORICAL
    NUNCA HUBO DATO                   →  SIN DATOS                NO_DATA
    EL PROVEEDOR FALLÓ                →  decirlo                  ERROR

«No llegó nada nuevo» y «no hay nada» son cosas distintas, y hasta ahora se
dibujaban igual.

═══════════════════════════════════════════════════════════════════════════
LKG POR CARRIL, NO GLOBAL
═══════════════════════════════════════════════════════════════════════════

Cada carril guarda su propio último valor bueno. Si QFLOW está disponible y
la cinta no, QFLOW sigue visible y sólo la cinta queda en STALE. Vaciar el
módulo entero porque falló un carril es perder seis datos buenos para
señalar uno malo.

═══════════════════════════════════════════════════════════════════════════
LA CLAVE DEL LKG
═══════════════════════════════════════════════════════════════════════════

    (symbol, session_date, dataset)

Las tres. Sin `symbol`, al pasar de DIA a QQQ el valor de DIA aparecería
unos segundos bajo QQQ. Sin `session_date`, el cierre de ayer se mostraría
como si fuera de hoy. Un LKG mal indexado es peor que no tener LKG: enseña
un número correcto en el sitio equivocado.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ── Estados de un carril ─────────────────────────────────────────────────
LIVE = "LIVE"                       # dato nuevo en este ciclo
STALE = "STALE"                     # no llegó nada nuevo; se muestra el último bueno
HISTORICAL = "HISTORICAL"           # sesión cerrada; se muestra la última válida
NO_DATA = "NO_DATA"                 # nunca hubo dato para este símbolo y sesión
PROVIDER_ERROR = "PROVIDER_ERROR"   # el proveedor falló y lo dijo

#: A partir de cuántos minutos sin dato nuevo se considera viejo. Por debajo
#: de esto el carril sigue siendo LIVE aunque no haya cambiado: un mercado
#: tranquilo no es un fallo.
STALE_AFTER_MINUTES = 3.0

_LOCK = threading.Lock()
#: (symbol, session_date, dataset) → {value, at, meta}
_LKG: Dict[tuple, Dict[str, Any]] = {}


def _now(now: Optional[datetime] = None) -> datetime:
    ref = now or datetime.now(timezone.utc)
    return ref if ref.tzinfo else ref.replace(tzinfo=timezone.utc)


def _key(symbol: str, session_date: str, dataset: str) -> tuple:
    return (str(symbol or "").upper(), str(session_date or ""), str(dataset or ""))


def remember(symbol: str, session_date: str, dataset: str, value: Any,
             *, now: Optional[datetime] = None, meta: Optional[Dict[str, Any]] = None) -> None:
    """Guarda el último valor BUENO de un carril. `None` no se guarda."""
    if value is None:
        return
    ref = _now(now)
    with _LOCK:
        _LKG[_key(symbol, session_date, dataset)] = {
            "value": value, "at": ref.isoformat(), "meta": meta or {},
        }


def recall(symbol: str, session_date: str, dataset: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        entry = _LKG.get(_key(symbol, session_date, dataset))
        return dict(entry) if entry else None


def lane(symbol: str, session_date: str, dataset: str, value: Any, *,
         now: Optional[datetime] = None,
         error: Optional[str] = None,
         market_open: bool = True,
         meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Un carril con su valor, su estado y su edad.

    `value is None` significa «este ciclo no trajo dato», NO «el dato vale
    cero». Un cero medido se pasa como `0` y se publica como `0`.
    """
    ref = _now(now)

    if error:
        prev = recall(symbol, session_date, dataset)
        return _pack(prev["value"] if prev else None, PROVIDER_ERROR, ref,
                     prev_at=(prev or {}).get("at"), detail=str(error)[:240],
                     meta=(prev or {}).get("meta") or meta)

    if value is not None:
        remember(symbol, session_date, dataset, value, now=ref, meta=meta)
        return _pack(value, LIVE, ref, prev_at=ref.isoformat(), detail="", meta=meta)

    prev = recall(symbol, session_date, dataset)
    if prev is None:
        # Nunca hubo dato para este símbolo y esta sesión. Éste es el ÚNICO
        # caso en el que la pantalla puede decir SIN DATOS.
        return _pack(None, NO_DATA, ref, prev_at=None,
                     detail="sin dato para este activo en esta sesión", meta=meta)

    age = _age_minutes(prev.get("at"), ref)
    state = HISTORICAL if not market_open else (
        STALE if (age is None or age >= STALE_AFTER_MINUTES) else LIVE)
    return _pack(prev["value"], state, ref, prev_at=prev.get("at"),
                 detail="", meta=prev.get("meta") or meta)


def _age_minutes(at: Any, ref: datetime) -> Optional[float]:
    if not at:
        return None
    try:
        t = datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return round(max(0.0, (ref - t).total_seconds() / 60.0), 1)


def _pack(value: Any, state: str, ref: datetime, *, prev_at: Optional[str],
          detail: str, meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    age = _age_minutes(prev_at, ref)
    return {
        "current": value,
        "status": state,
        "last_good_at": prev_at,
        "age_minutes": age,
        "detail": detail,
        "meta": meta or {},
        # Lo único que la pantalla operativa escribe cuando el dato es viejo.
        # Ni proveedor, ni endpoint, ni traza técnica: eso va al Auditor.
        "screen_note": ("" if state == LIVE else
                        "sin dato para este activo en esta sesión" if state == NO_DATA else
                        "el proveedor falló en este ciclo" if state == PROVIDER_ERROR else
                        f"último dato {_hhmm(prev_at)}" if prev_at else "dato antiguo"),
    }


def _hhmm(at: Any) -> str:
    try:
        t = datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return "—"
    return t.strftime("%H:%M")


def reset() -> None:
    """Olvida todos los LKG. Para pruebas."""
    with _LOCK:
        _LKG.clear()


def build(*, symbol: str, session_date: str, market_open: bool,
          tape: Optional[Dict[str, Any]] = None,
          net_flow: Optional[Dict[str, Any]] = None,
          qflow: Optional[Dict[str, Any]] = None,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """El modelo único que consume la sección FLUJO DE ÓRDENES.

    `tape` y `net_flow` son DATASETS DISTINTOS y se publican por separado:

        TAPE      prima compradora / vendedora / total, print mayor
        NET FLOW  call acumulado, put acumulado, neto acumulado

    Mezclarlos hacía que la ausencia de uno vaciara al otro. Si Net Flow tiene
    dato y la cinta está vieja, los dos se muestran con su estado real.
    """
    ref = _now(now)
    t = tape or {}
    nf = net_flow or {}
    q = qflow or {}

    def L(dataset: str, value: Any, error: Optional[str] = None,
          meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return lane(symbol, session_date, dataset, value, now=ref,
                    error=error, market_open=market_open, meta=meta)

    # La prima compradora y vendedora salen SÓLO de prints con agresor
    # clasificado. Un `CALL` no es una compra y un `PUT` no es una venta; la
    # prima sin lado se queda en su propio cubo en vez de repartirse.
    premiums = {
        "buy": L("premium_buy", t.get("buy_premium")),
        "sell": L("premium_sell", t.get("sell_premium")),
        "total": L("premium_total", t.get("total_premium")),
        "unclassified": L("premium_unknown", t.get("unknown_premium")),
        "largest_print": L("largest_print", t.get("largest_print")),
    }

    model = {
        "symbol": str(symbol or "").upper(),
        "session": {"date": str(session_date or ""), "market_open": bool(market_open)},
        "tape": L("tape_buckets", t.get("buckets"), error=t.get("error"),
                  meta={"count": len(t.get("buckets") or [])}),
        "net_flow": L("net_flow", nf.get("series"), error=nf.get("error"),
                      meta={"count": len(nf.get("series") or [])}),
        "qflow": L("qflow", q.get("markers"), error=q.get("error"),
                   meta={"count": len(q.get("markers") or [])}),
        "volume": L("volume", t.get("volume")),
        "prints": L("prints", t.get("prints"), meta={"count": len(t.get("prints") or [])}),
        "premiums": premiums,
    }

    # Frescura de la SECCIÓN: el carril más fresco manda, porque la sección
    # está viva mientras algo suyo lo esté. Antes bastaba un carril vacío para
    # que todo pareciera muerto.
    states = [v.get("status") for k, v in model.items()
              if isinstance(v, dict) and "status" in v]
    states += [v.get("status") for v in premiums.values()]
    rank = {LIVE: 0, STALE: 1, HISTORICAL: 2, PROVIDER_ERROR: 3, NO_DATA: 4}
    best = min(states, key=lambda s: rank.get(s, 9)) if states else NO_DATA
    ages = [v.get("age_minutes") for k, v in model.items()
            if isinstance(v, dict) and v.get("age_minutes") is not None]
    model["freshness"] = {
        "status": best,
        "age_minutes": min(ages) if ages else None,
        "lanes_live": sum(1 for s in states if s == LIVE),
        "lanes_total": len(states),
    }
    return model

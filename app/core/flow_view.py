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
from typing import Any, Dict, List, Optional

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

#: Cuántas FECHAS de sesión se conservan. Tres —la de hoy y las dos anteriores—
#: son las que alguien puede llegar a pedir: el LKG existe para tapar un ciclo
#: sin dato, no para hacer de archivo histórico.
#:
#: v1.57.0 · EL ÚLTIMO VALOR BUENO CADUCA.
#:
#: Este almacén no borraba nunca. La clave lleva la fecha dentro, así que cada
#: día abre entradas nuevas y las del día anterior quedan ahí con su valor
#: entero —series, matrices, cintas— sin que nada las vuelva a leer jamás.
#: Medido: 30 sesiones x 8 activos x 10 carriles = 2.400 entradas vivas, y
#: subiendo. En un portátil que se reinicia cada tarde no se nota; en el VPS,
#: que no se apaga, es una fuga.
RETAIN_SESSIONS = 3


def _purge_locked() -> int:
    """Deja sólo las `RETAIN_SESSIONS` fechas más recientes. Devuelve cuántas borró.

    Se llama con `_LOCK` tomado. Ordena por la fecha de la clave, que es ISO y
    por tanto ordenable como texto. Una entrada SIN fecha se considera la más
    antigua de todas: no se sabe a qué sesión pertenece, así que no se protege.
    """
    fechas = {k[1] for k in _LKG}
    if len(fechas) <= RETAIN_SESSIONS:
        return 0
    conservar = set(sorted(fechas, reverse=True)[:RETAIN_SESSIONS])
    caducas = [k for k in _LKG if k[1] not in conservar]
    for k in caducas:
        del _LKG[k]
    return len(caducas)


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
        _purge_locked()


def recall(symbol: str, session_date: str, dataset: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        entry = _LKG.get(_key(symbol, session_date, dataset))
        return dict(entry) if entry else None


def lane(symbol: str, session_date: str, dataset: str, value: Any, *,
         now: Optional[datetime] = None,
         error: Optional[str] = None,
         market_open: bool = True,
         source_mode: str = "DIRECT_PROVIDER",
         meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Un carril con su valor, su estado y su edad.

    `value is None` significa «este ciclo no trajo dato», NO «el dato vale
    cero». Un cero medido se pasa como `0` y se publica como `0`.
    """
    ref = _now(now)

    # La identidad del carril viaja SIEMPRE, pase lo que pase con el dato. Un
    # LKG mal indexado es peor que no tenerlo —enseña un número correcto en el
    # sitio equivocado—, así que la clave completa tiene que poder leerse en la
    # propia respuesta sin reconstruirla desde fuera.
    ident = {"symbol": str(symbol or "").upper(), "session_date": str(session_date or ""),
             "dataset": str(dataset or ""), "source_mode": source_mode,
             "timestamp": ref.isoformat()}

    if error:
        prev = recall(symbol, session_date, dataset)
        return _pack(prev["value"] if prev else None, PROVIDER_ERROR, ref,
                     prev_at=(prev or {}).get("at"), detail=str(error)[:240],
                     meta=(prev or {}).get("meta") or meta, ident=ident,
                     lkg=(prev or {}).get("value"))

    if value is not None:
        remember(symbol, session_date, dataset, value, now=ref, meta=meta)
        return _pack(value, LIVE, ref, prev_at=ref.isoformat(), detail="",
                     meta=meta, ident=ident, lkg=value)

    prev = recall(symbol, session_date, dataset)
    if prev is None:
        # Nunca hubo dato para este símbolo y esta sesión. Éste es el ÚNICO
        # caso en el que la pantalla puede decir SIN DATOS.
        return _pack(None, NO_DATA, ref, prev_at=None,
                     detail="sin dato para este activo en esta sesión",
                     meta=meta, ident=ident, lkg=None)

    age = _age_minutes(prev.get("at"), ref)
    state = HISTORICAL if not market_open else (
        STALE if (age is None or age >= STALE_AFTER_MINUTES) else LIVE)
    return _pack(prev["value"], state, ref, prev_at=prev.get("at"),
                 detail="", meta=prev.get("meta") or meta, ident=ident,
                 lkg=prev.get("value"))


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
          detail: str, meta: Optional[Dict[str, Any]],
          ident: Optional[Dict[str, Any]] = None,
          lkg: Any = None) -> Dict[str, Any]:
    age = _age_minutes(prev_at, ref)
    return {
        **(ident or {}),
        "current": value,
        # `last_known_good` se publica APARTE de `current` aunque coincidan.
        # Cuando el carril está vivo son el mismo valor; cuando está viejo,
        # `current` ES el LKG, y quien lea la respuesta tiene que poder saberlo
        # sin deducirlo del estado.
        "last_known_good": lkg,
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


def coverage(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Qué carriles tienen último valor bueno guardado, y de cuándo.

    v1.55.0 · La política de frescura nació dentro de FLUJO DE ÓRDENES y sirve
    para cualquier sección: la clave `(símbolo, sesión, dataset)` no tiene nada
    de particular de esa pantalla. Publicar el inventario es lo que permite
    contestar «¿qué está protegido por LKG y qué no?» sin leer el código, que es
    la pregunta que se hace cuando una sección se vacía y otra no.

    No inventa cobertura: lista lo que REALMENTE hay guardado.
    """
    ref = _now(now)
    with _LOCK:
        entradas = dict(_LKG)
    filas = []
    for (symbol, session_date, dataset), entry in sorted(entradas.items()):
        filas.append({
            "symbol": symbol, "session_date": session_date, "dataset": dataset,
            "at": entry.get("at"), "age_minutes": _age_minutes(entry.get("at"), ref),
        })
    datasets = sorted({f["dataset"] for f in filas})
    return {
        "rows": filas,
        "count": len(filas),
        "datasets": datasets,
        "symbols": sorted({f["symbol"] for f in filas}),
        "key": "(symbol, session_date, dataset)",
        "stale_after_minutes": STALE_AFTER_MINUTES,
        "retain_sessions": RETAIN_SESSIONS,
        "session_dates": sorted({f["session_date"] for f in filas}, reverse=True),
        "detail": (f"{len(filas)} carril(es) con último valor bueno"
                   if filas else "todavía no hay ningún último valor bueno guardado"),
    }


#: Anchura del bucket de las barras de flujo. Un minuto es el intervalo con el
#: que el proveedor publica Net Flow y Net Drift, así que las tres series caen
#: sobre el mismo reloj y se pueden leer una contra otra.
BUCKET_MS = 60_000


def bucketize(rows: Any, *, bucket_ms: int = BUCKET_MS) -> List[Dict[str, Any]]:
    """Agrupa la cinta de opciones en barras por intervalo.

    ═══════════════════════════════════════════════════════════════════════
    POR QUÉ ESTO ESTÁ AQUÍ Y NO EN EL NAVEGADOR
    ═══════════════════════════════════════════════════════════════════════

    Las barras de AGRESOR y de PRIMA se construían en el cliente a partir de
    `trace.option_prints`. Cuando ese campo venía vacío —y venía vacío aunque
    `order-flow` hubiera devuelto cientos de operaciones— los dos carriles se
    quedaban en «SIN FLUJO DIRECCIONAL» y «SIN PRIMA OBSERVADA», mientras el
    carril de VOLUMEN SUBYACENTE, que se alimenta de las velas, seguía lleno.

    En pantalla eso se lee como «no hubo flujo de opciones», que es una
    conclusión sobre el mercado. Lo que había era una ruta de datos rota.

    Construyéndolas aquí, las barras salen de la MISMA cinta que las tarjetas y
    que QFLOW, entran en el LKG por carril como todo lo demás, y un ciclo vacío
    deja de borrarlas.

    ═══════════════════════════════════════════════════════════════════════
    QUÉ LLEVA CADA BARRA
    ═══════════════════════════════════════════════════════════════════════

    Prima y volumen, separados por lado, y el lado SIN CLASIFICAR en su propio
    cubo. Repartirlo entre compra y venta inclinaría el sesgo hacia el lado que
    tocara por azar justo cuando la cinta llega sin cotización, que es cuando
    peor se lee.
    """
    if not isinstance(rows, (list, tuple)) or not rows:
        return []
    ancho = max(1000, int(bucket_ms))
    por_bucket: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        ms = _epoch_ms(r.get("t") or r.get("tradeTime") or r.get("timestamp"))
        if ms is None:
            continue
        k = (ms // ancho) * ancho
        b = por_bucket.get(k)
        if b is None:
            b = {"t": k, "t_iso": datetime.fromtimestamp(k / 1000.0, tz=timezone.utc).isoformat(),
                 "buy_premium": 0.0, "sell_premium": 0.0, "unknown_premium": 0.0,
                 "buy_volume": 0.0, "sell_volume": 0.0, "unknown_volume": 0.0,
                 "trades": 0, "buys": 0, "sells": 0, "unknowns": 0}
            por_bucket[k] = b
        prem = abs(_num(r.get("premium")) or 0.0)
        size = abs(_num(r.get("size")) or 0.0)
        lado = str(r.get("aggressor") or "UNKNOWN").upper()
        destino = "buy" if lado == "BUY" else "sell" if lado == "SELL" else "unknown"
        b[f"{destino}_premium"] += prem
        b[f"{destino}_volume"] += size
        b[destino + "s"] += 1
        b["trades"] += 1

    salida: List[Dict[str, Any]] = []
    for k in sorted(por_bucket):
        b = por_bucket[k]
        b["net_premium"] = b["buy_premium"] - b["sell_premium"]
        b["net_volume"] = b["buy_volume"] - b["sell_volume"]
        b["total_premium"] = b["buy_premium"] + b["sell_premium"] + b["unknown_premium"]
        b["classified_premium"] = b["buy_premium"] + b["sell_premium"]
        b["coverage_pct"] = (None if b["total_premium"] <= 0 else
                             round(100.0 * b["classified_premium"] / b["total_premium"], 1))
        salida.append(b)
    return salida


def _epoch_ms(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        n = float(v)
        return int(n if abs(n) > 1e11 else n * 1000.0)
    raw = str(v).strip()
    if not raw:
        return None
    if raw.isdigit():
        n = float(raw)
        return int(n if abs(n) > 1e11 else n * 1000.0)
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int(t.timestamp() * 1000)


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


#: Los OCHO carriles de la sección. Se declaran aquí para que «faltaba un
#: carril» sea una comprobación y no una impresión.
LANES = ("tape", "qflow", "net_flow", "net_drift", "premiums", "prints",
         "volume", "aggressor")


def build(*, symbol: str, session_date: str, market_open: bool,
          tape: Optional[Dict[str, Any]] = None,
          net_flow: Optional[Dict[str, Any]] = None,
          qflow: Optional[Dict[str, Any]] = None,
          net_drift: Optional[Dict[str, Any]] = None,
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
    nd = net_drift or {}

    def L(dataset: str, value: Any, error: Optional[str] = None,
          meta: Optional[Dict[str, Any]] = None,
          source_mode: str = "DIRECT_PROVIDER") -> Dict[str, Any]:
        return lane(symbol, session_date, dataset, value, now=ref,
                    error=error, market_open=market_open, meta=meta,
                    source_mode=source_mode)

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

    # ── LAS BARRAS ───────────────────────────────────────────────────────
    #
    # v1.56.0 · Se construyen AQUÍ, no en el navegador.
    #
    # Antes salían de `trace.option_prints` en el cliente, y cuando ese campo
    # venía vacío —aunque `order-flow` hubiera devuelto cientos de operaciones—
    # los carriles de AGRESOR y PRIMA se quedaban en «SIN FLUJO DIRECCIONAL» y
    # «SIN PRIMA OBSERVADA» mientras el de VOLUMEN SUBYACENTE, alimentado por
    # las velas, seguía lleno. En pantalla eso se lee como «no hubo flujo de
    # opciones», que es una conclusión sobre el mercado; lo que había era una
    # ruta rota.
    #
    # Aquí salen de la MISMA cinta que las tarjetas y que QFLOW, y entran en el
    # LKG por carril como todo lo demás: un ciclo vacío deja de borrarlas.
    barras = bucketize(t.get("buckets")) or None

    model = {
        "symbol": str(symbol or "").upper(),
        "session": {"date": str(session_date or ""), "market_open": bool(market_open)},
        "tape": L("tape_buckets", t.get("buckets"), error=t.get("error"),
                  meta={"count": len(t.get("buckets") or [])}),
        # Dos carriles SEPARADOS sobre las mismas barras: uno responde «¿de qué
        # lado fue?» y el otro «¿cuánto dinero?». Un carril no desaparece porque
        # falte el otro.
        "aggressor_bars": L("aggressor_bars", barras,
                            meta={"count": len(barras or []), "bucket_ms": BUCKET_MS}),
        "premium_bars": L("premium_bars", barras,
                          meta={"count": len(barras or []), "bucket_ms": BUCKET_MS}),
        "net_flow": L("net_flow", nf.get("series"), error=nf.get("error"),
                      meta={"count": len(nf.get("series") or [])}),
        "qflow": L("qflow", q.get("markers"), error=q.get("error"),
                   meta={"count": len(q.get("markers") or [])}),
        "volume": L("volume", t.get("volume")),
        "prints": L("prints", t.get("prints"), meta={"count": len(t.get("prints") or [])}),
        "premiums": premiums,
        # NET DRIFT es su propio carril: viene del endpoint OFICIAL y no se
        # reconstruye con Net Flow ni con QFLOW. Que falte no puede vaciar la
        # cinta, y que falte la cinta no puede vaciarlo a él.
        "net_drift": L("net_drift", nd.get("series"), error=nd.get("error"),
                       meta={"count": len(nd.get("series") or []),
                             "state": nd.get("state")}),
        # El AGRESOR es un carril propio porque puede fallar solo: la cinta
        # llega entera y aun así ninguna operación resuelve lado.
        "aggressor": L("aggressor", t.get("aggressor_coverage_pct"),
                       meta={"classified_premium": t.get("classified_premium"),
                             "unknown_premium": t.get("unknown_premium")},
                       source_mode="DERIVED"),
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

    # ── RECUENTO DE LA CADENA ────────────────────────────────────────────
    #
    # Si el proveedor trae filas y no se dibuja ninguna barra, hay un fallo de
    # integración y tiene que poder señalarse la ETAPA exacta. Sin estos
    # números, «no se ven barras» obliga a instrumentar la cadena entera cada
    # vez. Con ellos se lee de un vistazo dónde se pierden.
    provider = int(t.get("provider_count") or 0)
    normalized = len(t.get("buckets") or [])
    model["pipeline"] = {
        "provider_count": provider,
        "normalized_count": normalized,
        "hub_bucket_count": normalized,
        "viewmodel_bucket_count": len(barras or []),
        # Lo que el navegador dibuja de verdad lo publica él mismo al pintar;
        # aquí viaja el resto de la cadena para poder compararlo.
        "rendered_bar_count": None,
        "bucket_ms": BUCKET_MS,
        "ok": (provider == 0) or bool(barras),
        "detail": ("sin filas del proveedor en este ciclo" if provider == 0 else
                   f"{provider} filas del proveedor -> {len(barras or [])} barras"
                   if barras else
                   f"{provider} filas del proveedor y NINGUNA barra: fallo de integración"),
    }
    return model

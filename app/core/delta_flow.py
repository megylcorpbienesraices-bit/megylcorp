"""DELTA / MIN · presión delta del dealer, operación a operación.

═══════════════════════════════════════════════════════════════════════════
QUÉ MIDE, Y QUÉ NO
═══════════════════════════════════════════════════════════════════════════

Cuánta delta tiene que absorber el dealer cada minuto por lo que hizo el
cliente. Es un FLUJO: el cambio por minuto, no la posición abierta.

No es volumen. No es prima. No se deriva de Net Drift ni lo sustituye. Net
Drift mide DINERO acumulado; esto mide EXPOSICIÓN DIRECCIONAL por minuto. Dos
magnitudes distintas sobre el mismo reloj, y mezclarlas produciría un número
que no significa nada.

═══════════════════════════════════════════════════════════════════════════
LA FÓRMULA
═══════════════════════════════════════════════════════════════════════════

    Dealer_Delta_Shares  = −( Δ_opción × contratos × 100 × Side_cliente )
    Dealer_Delta_Dollars =    Dealer_Delta_Shares × spot

    Side_cliente = +1 el cliente COMPRÓ la opción
    Side_cliente = −1 el cliente VENDIÓ la opción

El signo negativo de fuera es el convenio de DEALER: el dealer toma el otro
lado de lo que hace el cliente. Si el cliente compra calls, el dealer queda
corto de delta y tiene que comprar subyacente para cubrirse.

═══════════════════════════════════════════════════════════════════════════
EL ERROR DE SIGNO QUE ESTO EVITA
═══════════════════════════════════════════════════════════════════════════

`Side_cliente` responde UNA sola pregunta: ¿compró o vendió la opción? Nada
más. NO se le aplica encima ningún signo «alcista/bajista», porque la delta
del contrato YA lo lleva: la del put es negativa de fábrica.

Quien añada un `if PUT: signo = -1` duplica el signo de los puts y los
invierte. Es exactamente la familia del `CALL = BUY / PUT = SELL` que este
proyecto arrastró durante meses. La dirección EMERGE del producto:

    operación cliente   Δ contrato   Side   Δ cliente   Δ DEALER
    ──────────────────────────────────────────────────────────────
    compra CALL           +0.45       +1      +0.45      −0.45
    compra PUT            −0.38       +1      −0.38      +0.38
    vende  CALL           +0.45       −1      −0.45      +0.45
    vende  PUT            −0.38       −1      +0.38      −0.38

Los cuatro cuadrantes salen bien sin un solo signo adicional. Hay un test de
regresión por cada fila de esa tabla.

═══════════════════════════════════════════════════════════════════════════
LO QUE NO SE FUERZA
═══════════════════════════════════════════════════════════════════════════

Una operación sin lado demostrable (MID_MARKET, sin NBBO) NO entra con signo
inventado y NO entra como cero: queda fuera del sumatorio y se cuenta aparte.

Por eso se publica la COBERTURA. Si el 40 % de los prints no se pueden
clasificar, el carril enseña el 60 % del flujo y parece un mercado tranquilo.
Sin esa cifra, el panel mentiría por omisión.

═══════════════════════════════════════════════════════════════════════════
PROCEDENCIA
═══════════════════════════════════════════════════════════════════════════

La delta por contrato y el spot vienen del PROVEEDOR en la propia operación
—`delta` y `stockPrice` del print—, así que son del instante exacto del cruce.
Importa: en 0DTE una delta de cadena con cinco minutos de retraso es basura.

El agregado por minuto es DERIVED · ITM QUANT: la suma y el convenio de dealer
son nuestros. Las entradas son DIRECT_PROVIDER.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import data_lineage as DL
from .data_lineage import DERIVED

#: Acciones por contrato. Estándar de opciones sobre acciones y ETF de EE. UU.
CONTRACT_MULTIPLIER = 100.0

#: Anchura del bucket. Un minuto es el reloj con el que se publican Net Flow y
#: Net Drift, así que los tres carriles caen sobre la misma rejilla y se pueden
#: leer uno contra otro.
BUCKET_SECONDS = 60

#: Por qué una operación no entró en el sumatorio.
SIN_DELTA = "SIN_DELTA_DEL_PROVEEDOR"
SIN_LADO = "AGRESOR_NO_DEMOSTRABLE"
SIN_TAMANO = "SIN_NUMERO_DE_CONTRATOS"
SIN_INSTANTE = "SIN_INSTANTE_UTILIZABLE"
CONTADA = "CONTADA"

AUTHORITY = "ITMQ_DELTA_FLOW"
METHOD = ("dealer_delta_shares = -(delta_contrato x contratos x 100 x side_cliente); "
          "dealer_delta_dollars = dealer_delta_shares x spot; "
          "suma por minuto; las operaciones sin lado demostrable NO se fuerzan")


def _f(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _parse_ts(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        # Épocas en milisegundos y en segundos conviven en las respuestas.
        if x > 1e11:
            x /= 1000.0
        try:
            return datetime.fromtimestamp(x, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str) and v.strip():
        txt = v.strip().replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(txt)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _delta_of(row: Dict[str, Any]) -> Optional[float]:
    """Delta POR CONTRATO del proveedor, en el instante de la operación."""
    g = row.get("greeks")
    if isinstance(g, dict):
        d = _f(g.get("delta"))
        if d is not None:
            return d
    return _f(row.get("delta") if not isinstance(row.get("delta"), dict) else None)


def _side_client(row: Dict[str, Any]) -> int:
    """+1 el cliente compró, −1 vendió, 0 no se puede demostrar.

    Se lee la MISMA autoridad del agresor que el resto del programa. Aquí no se
    reimplementa la clasificación: dos clasificadores producen dos verdades.
    """
    d = row.get("direction")
    if isinstance(d, (int, float)) and not isinstance(d, bool):
        v = int(d)
        return 1 if v > 0 else -1 if v < 0 else 0
    from .aggressor import from_row as _agg_from_row
    verdict, _ = _agg_from_row(row)
    return 1 if verdict == "BUY" else -1 if verdict == "SELL" else 0


def dealer_delta_shares(delta: Optional[float], contracts: Optional[float],
                        side_client: int) -> Optional[float]:
    """La fórmula, sola y sin contexto, para poder probarla a mano.

    `None` cuando falta cualquier factor. Nunca cero: un factor ausente no es
    un flujo de cero delta.
    """
    d = _f(delta)
    n = _f(contracts)
    if d is None or n is None or side_client == 0:
        return None
    return -(d * n * CONTRACT_MULTIPLIER * float(side_client))


def trade_delta_flow(row: Dict[str, Any], *,
                     spot_fallback: Optional[float] = None) -> Dict[str, Any]:
    """Veredicto forense de UNA operación: qué entró, qué no, y por qué."""
    if not isinstance(row, dict):
        return {"counted": False, "reason": SIN_INSTANTE}

    delta = _delta_of(row)
    contracts = _f(row.get("size") if row.get("size") is not None else row.get("contracts"))
    side = _side_client(row)
    spot = _f(row.get("spot"))
    if spot is None:
        spot = _f(row.get("stockPrice")) if row.get("stockPrice") is not None else None
    if spot is None:
        spot = _f(spot_fallback)

    if delta is None:
        reason = SIN_DELTA
    elif contracts is None:
        reason = SIN_TAMANO
    elif side == 0:
        reason = SIN_LADO
    else:
        reason = CONTADA

    shares = dealer_delta_shares(delta, contracts, side)
    dollars = None if (shares is None or spot is None) else shares * spot
    return {
        "counted": reason == CONTADA,
        "reason": reason,
        "option_type": str(row.get("option_type") or row.get("optionType") or "").upper(),
        "delta": delta,
        "contracts": contracts,
        "side_client": side,
        "spot": spot,
        "client_delta_shares": None if shares is None else -shares,
        "dealer_delta_shares": shares,
        "dealer_delta_dollars": dollars,
    }


def _bucket(ts: datetime, seconds: int) -> datetime:
    base = ts.replace(second=0, microsecond=0)
    if seconds == 60:
        return base
    step = max(1, seconds // 60)
    return base - timedelta(minutes=base.minute % step)


def build_delta_flow(rows: Any, *, symbol: str,
                     bucket_seconds: int = BUCKET_SECONDS,
                     spot_fallback: Optional[float] = None) -> Dict[str, Any]:
    """Presión delta del dealer por minuto, con su cobertura y su forense.

    Devuelve SIEMPRE las dos unidades: delta-acciones (el cálculo base) y
    dólares (delta-acciones x spot). La pantalla principal enseña dólares; las
    acciones, la cobertura y el método quedan para el Auditor.
    """
    sym = str(symbol or "").upper()
    base = {
        "symbol": sym, "ready": False, "series": [],
        "source_mode": DERIVED, "provider": DL.ITM_QUANT, "authority": AUTHORITY,
        "method": METHOD, "bucket_seconds": int(bucket_seconds),
        "contract_multiplier": CONTRACT_MULTIPLIER,
        "convention": "DEALER",
        "inputs": ["QD_OPTION_FLOW.delta", "QD_OPTION_FLOW.size",
                   "QD_OPTION_FLOW.stockPrice", "ITMQ_AGGRESSOR.direction"],
    }
    lista = [r for r in (rows or []) if isinstance(r, dict)]
    if not lista:
        return {**base, "detail": "sin cinta de opciones en este ciclo",
                "coverage": {"total": 0, "counted": 0, "pct": None, "by_reason": {}}}

    motivos: Dict[str, int] = {}
    cubos: Dict[datetime, Dict[str, Any]] = {}
    contadas = 0
    for r in lista:
        ts = _parse_ts(r.get("t") or r.get("timestamp") or r.get("tradeTime"))
        v = trade_delta_flow(r, spot_fallback=spot_fallback)
        motivo = v["reason"] if ts is not None else SIN_INSTANTE
        motivos[motivo] = motivos.get(motivo, 0) + 1
        if ts is None or not v["counted"]:
            continue
        contadas += 1
        k = _bucket(ts, int(bucket_seconds))
        c = cubos.get(k)
        if c is None:
            c = cubos[k] = {"shares": 0.0, "dollars": 0.0, "dollars_ok": True,
                            "trades": 0, "call": 0.0, "put": 0.0}
        c["shares"] += v["dealer_delta_shares"]
        if v["dealer_delta_dollars"] is None:
            # Sin spot no hay conversión posible para ESTE minuto. No se
            # rellena con el spot de otro instante: sería inventar el precio.
            c["dollars_ok"] = False
        else:
            c["dollars"] += v["dealer_delta_dollars"]
        c["trades"] += 1
        if v["option_type"].startswith("C"):
            c["call"] += v["dealer_delta_shares"]
        elif v["option_type"].startswith("P"):
            c["put"] += v["dealer_delta_shares"]

    series: List[Dict[str, Any]] = []
    for k in sorted(cubos):
        c = cubos[k]
        series.append({
            "t": k.isoformat(),
            "timestamp_ms": int(k.timestamp() * 1000),
            "dealer_delta_shares": round(c["shares"], 4),
            "dealer_delta_dollars": (round(c["dollars"], 2) if c["dollars_ok"] else None),
            "dealer_delta_shares_call": round(c["call"], 4),
            "dealer_delta_shares_put": round(c["put"], 4),
            "trades": c["trades"],
        })

    total = len(lista)
    pct = round(contadas / total * 100.0, 2) if total else None
    cobertura = {
        "total": total, "counted": contadas,
        "pct": pct,
        "by_reason": motivos,
        "detail": (f"{contadas} de {total} operaciones con delta, tamaño y lado "
                   f"demostrables" if total else "sin operaciones"),
    }
    if not series:
        return {**base, "coverage": cobertura,
                "detail": ("ninguna operación pudo clasificarse: "
                           + ", ".join(f"{k}={v}" for k, v in sorted(motivos.items())))}

    total_shares = sum(s["dealer_delta_shares"] for s in series)
    dolares = [s["dealer_delta_dollars"] for s in series if s["dealer_delta_dollars"] is not None]
    return {
        **base,
        "ready": True,
        "series": series,
        "buckets": len(series),
        "coverage": cobertura,
        "total_dealer_delta_shares": round(total_shares, 4),
        "total_dealer_delta_dollars": (round(sum(dolares), 2) if len(dolares) == len(series) else None),
        "detail": f"{len(series)} minuto(s) con presión delta medible",
    }


def canonical_quantities(report: Dict[str, Any]) -> List[Any]:
    """Los totales, como magnitudes CANÓNICAS de la capa de unidades.

    v1.57.0 · TODA EXPOSICIÓN SALE DE UNA SOLA CAPA DE UNIDADES.

    El auditor de riesgo de modelo avisaba con «no se publicó ninguna exposición
    con unidad declarada». No era falso: la capa canónica existía, con su
    registro de unidades, su convenio de signo y su multiplicador… y nadie le
    entregaba nada. Un registro que no registra es documentación, no un control.

    Aquí se entrega lo que este módulo mide, en sus DOS representaciones, cada
    una con la unidad que le corresponde:

        DEX_SHARES    delta-acciones     el cálculo base
        DEX_NOTIONAL  dólares            acciones x spot

    El convenio es DEALER, declarado, no supuesto: positivo = el dealer está
    largo de delta. Y viaja el spot con el que se convirtió, para que nadie
    pueda comparar estos dólares con los de otro spot sin enterarse.
    """
    from . import units_registry as UR

    if not isinstance(report, dict) or not report.get("ready"):
        return []
    sym = str(report.get("symbol") or "").upper()
    if not sym:
        return []

    acciones = _f(report.get("total_dealer_delta_shares"))
    dolares = _f(report.get("total_dealer_delta_dollars"))
    if acciones is None:
        return []
    # El spot con el que se convirtió, deducido de lo publicado. Sin conversión
    # posible no se inventa uno: se entrega sólo la magnitud en acciones.
    spot = (dolares / acciones) if (dolares is not None and acciones) else None

    out: List[Any] = []
    comunes = dict(underlying=sym, sign_convention=UR.SIGN_DEALER,
                   source="ITMQ_DELTA_FLOW", multiplier_source="REGISTRY",
                   notes="presion delta del dealer acumulada en la ventana publicada")
    try:
        if spot is not None:
            out.append(UR.delta_exposure(acciones, spot=spot,
                                         representation=UR.REP_SHARES, **comunes))
            out.append(UR.delta_exposure(dolares, spot=spot,
                                         representation=UR.REP_NOTIONAL, **comunes))
        else:
            # Sin spot no hay nocional. La magnitud en acciones sigue siendo
            # válida y se publica; inventar un spot para poder convertir sería
            # exactamente lo que esta capa existe para impedir.
            out.append(UR.delta_exposure(acciones, spot=0.0,
                                         representation=UR.REP_SHARES, **comunes))
    except Exception:
        # Una unidad mal declarada tiene que romper el control, no colarse.
        return []
    return out


__all__ = ["build_delta_flow", "trade_delta_flow", "dealer_delta_shares",
           "canonical_quantities",
           "CONTRACT_MULTIPLIER", "BUCKET_SECONDS", "AUTHORITY", "METHOD",
           "SIN_DELTA", "SIN_LADO", "SIN_TAMANO", "SIN_INSTANTE", "CONTADA"]

"""Replay histórico de opciones con P&L ejecutable (v1.42).

EL PROBLEMA CON EL BACKTEST ANTERIOR
------------------------------------
Valorar una estrategia de opciones con Black-Scholes e IV constante y llamar a eso
P&L es la forma más común de producir una curva de equity que nadie puede reproducir
con dinero. Dos razones, ambas materiales:

  1. Se compra en el ASK y se vende en el BID. Un backtest que entra y sale al
     precio teórico se regala medio spread en cada extremo. En opciones, donde un
     spread del 4 % sobre la prima es normal y del 20 % nada raro, eso puede ser
     TODO el beneficio de la estrategia.
  2. La IV no es constante. Una posición ganadora en dirección puede perder por
     compresión de volatilidad, y el modelo con IV fija no lo vería nunca.

LO QUE HACE ESTE MÓDULO
-----------------------
Persiste la materia prima observada —bid, ask, mid, último trade, marca de tiempo
de la cotización, spread, IV, Greeks, subyacente y DTE— y valora con ella:

    P&L (compra) = BidSalida − AskEntrada − comisiones − deslizamiento

No `PrecioModelo_salida − PrecioModelo_entrada`. El modelo sirve para entender la
posición; el bid y el ask son los que pagan.

Alpaca entrega snapshots OPRA con último trade, cotización y Greeks, así que el
histórico de alta calidad se construye desde hoy grabando lo que ya llega, en vez
de reconstruirlo después con supuestos.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import numpy as np
import pandas as pd

from app.persistence import PERSISTENT_ROOT, routed_dir
from .obs import note as _obs_note
from .quant_errors import ExecutionCostUnavailable, StaleData

STORE = routed_dir(PERSISTENT_ROOT, "replay")
SNAPSHOT_DIR = Path(STORE) / "option_snapshots"

# Columnas mínimas para que un replay sea ejecutable. Faltando cualquiera, la fila
# se archiva igual pero no se puede usar para valorar una entrada o una salida.
REQUIRED = ("timestamp", "contract_symbol", "bid", "ask")
COLUMNS = ("timestamp", "contract_symbol", "underlying_symbol", "option_type", "strike",
           "expiration_date", "dte", "bid", "ask", "mid", "last_trade", "quote_timestamp",
           "spread", "spread_pct", "iv", "delta", "gamma", "vega", "theta",
           "underlying_price", "open_interest", "volume")

# Costes por defecto. Son configurables porque cambian por bróker; lo que NO es
# configurable es operar sin declararlos: un backtest sin costes no es un backtest.
DEFAULT_FEE_PER_CONTRACT = 0.65      # comisión típica retail por contrato
DEFAULT_EXCHANGE_FEE = 0.15         # tasas de mercado/regulatorias por contrato
DEFAULT_SLIPPAGE_TICKS = 0.0        # deslizamiento adicional, en ticks del contrato
MAX_QUOTE_AGE_S = 30.0


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _day_path(symbol: Any, day: date) -> Path:
    sym = str(symbol or "UNKNOWN").upper().strip()
    return SNAPSHOT_DIR / sym / f"{day.isoformat()}.jsonl"


def record_snapshot(frame: pd.DataFrame, *, symbol: Any, asof: Any = None) -> Dict[str, Any]:
    """Archiva un snapshot de cadena tal y como se observó.

    Se guarda en JSONL por día y símbolo: es append-only, resistente a un corte a
    mitad de escritura y legible sin dependencias. Un formato columnar sería más
    compacto, pero perder el histórico por un archivo corrupto cuesta más que el disco.
    """
    if frame is None or len(frame) == 0:
        return {"ready": False, "reason": "snapshot vacío"}
    missing = [c for c in REQUIRED if c not in frame.columns]
    if missing:
        return {"ready": False, "reason": f"faltan columnas imprescindibles: {missing}"}

    df = frame.copy()
    if "mid" not in df.columns:
        b = pd.to_numeric(df["bid"], errors="coerce")
        a = pd.to_numeric(df["ask"], errors="coerce")
        df["mid"] = np.where((a >= b) & (b >= 0), 0.5 * (a + b), np.nan)
    if "spread" not in df.columns:
        df["spread"] = pd.to_numeric(df["ask"], errors="coerce") - pd.to_numeric(df["bid"], errors="coerce")
    if "spread_pct" not in df.columns:
        mid = pd.to_numeric(df["mid"], errors="coerce")
        df["spread_pct"] = np.where(mid > 0, df["spread"] / mid * 100.0, np.nan)
    df["underlying_symbol"] = str(symbol or "").upper()

    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    ref = pd.to_datetime(asof, errors="coerce") if asof is not None else ts.max()
    day = (ref if pd.notna(ref) else pd.Timestamp.now(tz="UTC")).date()

    keep = [c for c in COLUMNS if c in df.columns]
    path = _day_path(symbol, day)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = df[keep].copy()
        for col in ("timestamp", "quote_timestamp", "expiration_date"):
            if col in payload.columns:
                payload[col] = payload[col].astype(str)
        with path.open("a", encoding="utf-8") as fh:
            for rec in payload.to_dict(orient="records"):
                fh.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")
    except OSError as exc:
        _obs_note("option_replay_store:write", exc, severity="DEGRADED")
        return {"ready": False, "reason": f"no se pudo escribir el snapshot: {exc}"}

    return {"ready": True, "rows": int(len(df)), "path": str(path), "day": day.isoformat(),
            "columns": keep,
            "note": "Materia prima observada. El replay valorará con estos bid/ask, no con un modelo."}


def load_window(symbol: Any, start: Any, end: Any) -> pd.DataFrame:
    """Carga los snapshots archivados entre dos instantes."""
    s = pd.to_datetime(start, errors="coerce")
    e = pd.to_datetime(end, errors="coerce")
    if pd.isna(s) or pd.isna(e):
        return pd.DataFrame(columns=list(COLUMNS))
    sym = str(symbol or "").upper().strip()
    base = SNAPSHOT_DIR / sym
    if not base.is_dir():
        return pd.DataFrame(columns=list(COLUMNS))
    frames = []
    for day in pd.date_range(s.normalize(), e.normalize(), freq="D"):
        path = base / f"{day.date().isoformat()}.jsonl"
        if not path.is_file():
            continue
        try:
            frames.append(pd.read_json(path, lines=True))
        except ValueError as exc:
            _obs_note("option_replay_store:read", exc, severity="DEGRADED")
    if not frames:
        return pd.DataFrame(columns=list(COLUMNS))
    out = pd.concat(frames, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    return out[(out["timestamp"] >= s) & (out["timestamp"] <= e)].sort_values("timestamp")


def available_days(symbol: Any) -> list[str]:
    base = SNAPSHOT_DIR / str(symbol or "").upper().strip()
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.jsonl"))


@dataclass(frozen=True)
class ExecutionCosts:
    fee_per_contract: float = DEFAULT_FEE_PER_CONTRACT
    exchange_fee: float = DEFAULT_EXCHANGE_FEE
    slippage_ticks: float = DEFAULT_SLIPPAGE_TICKS
    tick_size: float = 0.01

    @property
    def per_contract(self) -> float:
        return self.fee_per_contract + self.exchange_fee

    def slippage(self) -> float:
        return self.slippage_ticks * self.tick_size

    def describe(self) -> Dict[str, Any]:
        return {"fee_per_contract": self.fee_per_contract, "exchange_fee": self.exchange_fee,
                "slippage_ticks": self.slippage_ticks, "tick_size": self.tick_size,
                "round_trip_per_contract": round(2.0 * self.per_contract, 4)}


@dataclass(frozen=True)
class Fill:
    price: float
    side: str            # BUY | SELL
    assumption: str
    quote_age_s: Optional[float]
    spread_pct: Optional[float]


def assume_fill(row: Dict[str, Any], *, side: str,
                costs: ExecutionCosts, max_quote_age_s: float = MAX_QUOTE_AGE_S,
                asof: Any = None) -> Fill:
    """Precio de ejecución conservador a partir de la cotización observada.

    COMPRA → se paga el ASK. VENTA → se recibe el BID. Nada de mid, y menos aún de
    precio teórico: asumir el mid es asumir que siempre hay alguien dispuesto a
    cruzar en el punto medio, que es justo lo que no ocurre cuando hace falta.
    """
    b, a = _f(row.get("bid")), _f(row.get("ask"))
    if b is None or a is None or a < b or a <= 0:
        raise ExecutionCostUnavailable(
            f"{row.get('contract_symbol')}: sin cotización de dos lados utilizable "
            "en el instante de la ejecución")

    age = None
    qts = row.get("quote_timestamp") or row.get("timestamp")
    ref = pd.to_datetime(asof, errors="coerce") if asof is not None else pd.to_datetime(row.get("timestamp"), errors="coerce")
    qt = pd.to_datetime(qts, errors="coerce")
    if pd.notna(qt) and pd.notna(ref):
        age = abs(float((ref - qt).total_seconds()))
        if age > float(max_quote_age_s):
            raise StaleData(f"cotización de {row.get('contract_symbol')}", age, max_quote_age_s)

    slip = costs.slippage()
    mid = 0.5 * (a + b)
    spread_pct = (a - b) / mid * 100.0 if mid > 0 else None
    if str(side).upper() == "BUY":
        return Fill(a + slip, "BUY", "se paga el ASK más deslizamiento", age, spread_pct)
    return Fill(max(b - slip, 0.0), "SELL", "se recibe el BID menos deslizamiento", age, spread_pct)


def price_round_trip(entry_row: Dict[str, Any], exit_row: Dict[str, Any], *,
                     quantity: int = 1, direction: str = "LONG",
                     multiplier: float = 100.0,
                     costs: ExecutionCosts | None = None) -> Dict[str, Any]:
    """P&L realizable de una ida y vuelta, con sus costes explícitos."""
    c = costs or ExecutionCosts()
    long_side = str(direction).upper() == "LONG"
    entry = assume_fill(entry_row, side="BUY" if long_side else "SELL", costs=c)
    exit_ = assume_fill(exit_row, side="SELL" if long_side else "BUY", costs=c)

    gross_per_contract = (exit_.price - entry.price) if long_side else (entry.price - exit_.price)
    gross = gross_per_contract * quantity * multiplier
    fees = 2.0 * c.per_contract * quantity
    net = gross - fees

    theo_entry, theo_exit = _f(entry_row.get("mid")), _f(exit_row.get("mid"))
    theoretical = None
    if theo_entry is not None and theo_exit is not None:
        theo_gross = ((theo_exit - theo_entry) if long_side else (theo_entry - theo_exit))
        theoretical = theo_gross * quantity * multiplier

    return {
        "ready": True, "direction": direction, "quantity": int(quantity),
        "entry_price": round(entry.price, 4), "entry_assumption": entry.assumption,
        "exit_price": round(exit_.price, 4), "exit_assumption": exit_.assumption,
        "gross_pnl": round(gross, 2), "fees": round(fees, 2), "net_pnl": round(net, 2),
        "entry_spread_pct": None if entry.spread_pct is None else round(entry.spread_pct, 3),
        "exit_spread_pct": None if exit_.spread_pct is None else round(exit_.spread_pct, 3),
        "entry_quote_age_s": entry.quote_age_s, "exit_quote_age_s": exit_.quote_age_s,
        "mid_to_mid_pnl": None if theoretical is None else round(theoretical, 2),
        "execution_drag": None if theoretical is None else round(theoretical - net, 2),
        "costs": c.describe(),
        "method": "OBSERVED_QUOTE_EXECUTION",
        "note": ("Compra en el ask, venta en el bid, comisiones aparte. La diferencia con "
                 "el P&L mid-a-mid es el coste real de ejecutar, y en opciones no es menor."),
    }


def replay_trades(trades: Iterable[Dict[str, Any]], snapshots: pd.DataFrame, *,
                  multiplier: float = 100.0,
                  costs: ExecutionCosts | None = None) -> Dict[str, Any]:
    """Valora una lista de operaciones contra el histórico observado.

    Cada operación necesita `contract_symbol`, `entry_time`, `exit_time` y opcionalmente
    `quantity` y `direction`. Se toma el snapshot vigente EN O ANTES de cada instante:
    usar el posterior sería mirar hacia adelante, que es la forma más silenciosa de
    fabricar un backtest ganador.
    """
    if snapshots is None or len(snapshots) == 0:
        return {"ready": False, "reason": "sin snapshots archivados para ese periodo"}
    snaps = snapshots.copy()
    snaps["timestamp"] = pd.to_datetime(snaps["timestamp"], errors="coerce")
    snaps = snaps.dropna(subset=["timestamp"]).sort_values("timestamp")

    results, errors = [], []
    for t in trades or []:
        sym = str(t.get("contract_symbol") or "")
        sub = snaps[snaps["contract_symbol"].astype(str) == sym]
        if sub.empty:
            errors.append({"contract_symbol": sym, "reason": "sin histórico para ese contrato"})
            continue
        t_in = pd.to_datetime(t.get("entry_time"), errors="coerce")
        t_out = pd.to_datetime(t.get("exit_time"), errors="coerce")
        if pd.isna(t_in) or pd.isna(t_out) or t_out < t_in:
            errors.append({"contract_symbol": sym, "reason": "instantes de entrada/salida no válidos"})
            continue
        e_rows = sub[sub["timestamp"] <= t_in]
        x_rows = sub[sub["timestamp"] <= t_out]
        if e_rows.empty or x_rows.empty:
            errors.append({"contract_symbol": sym,
                           "reason": "no hay cotización observada en o antes de esos instantes "
                                     "(usar la posterior sería mirar hacia adelante)"})
            continue
        try:
            out = price_round_trip(e_rows.iloc[-1].to_dict(), x_rows.iloc[-1].to_dict(),
                                   quantity=int(t.get("quantity", 1) or 1),
                                   direction=str(t.get("direction", "LONG")),
                                   multiplier=float(multiplier), costs=costs)
        except (ExecutionCostUnavailable, StaleData) as exc:
            errors.append({"contract_symbol": sym, "reason": str(exc)})
            continue
        out["contract_symbol"] = sym
        out["entry_time"] = str(e_rows.iloc[-1]["timestamp"])
        out["exit_time"] = str(x_rows.iloc[-1]["timestamp"])
        results.append(out)

    if not results:
        return {"ready": False, "reason": "ninguna operación valorable", "errors": errors}

    net = np.array([r["net_pnl"] for r in results], dtype=float)
    drag = [r["execution_drag"] for r in results if r["execution_drag"] is not None]
    wins, losses = net[net > 0], net[net < 0]
    return {
        "ready": True, "trades": len(results), "rejected": len(errors), "errors": errors[:20],
        "net_pnl": round(float(net.sum()), 2),
        "expectancy": round(float(net.mean()), 2),
        "hit_rate_pct": round(100.0 * float((net > 0).mean()), 2),
        "profit_factor": (round(float(wins.sum() / -losses.sum()), 3)
                          if losses.size and losses.sum() < 0 else None),
        "total_execution_drag": round(float(np.sum(drag)), 2) if drag else None,
        "results": results,
        "method": "HISTORICAL_OPTION_REPLAY",
        "note": ("Valorado contra bid/ask observados con costes explícitos. El "
                 "«execution drag» es lo que un backtest mid-a-mid se habría regalado."),
    }

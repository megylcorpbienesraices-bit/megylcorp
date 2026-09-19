"""ContractSpec — la única autoridad sobre qué ES un contrato (v1.42).

POR QUÉ EXISTE ESTE MÓDULO
--------------------------
Hasta v1.41 el tamaño económico de un contrato viajaba por el programa como un
literal: `premium = precio * size * 100.0` aparecía en `flow_intelligence`, en
`option_stream`, en `dealer_intelligence` y en `trace_analytics`. Cada uno de esos
`100.0` era una afirmación independiente sobre el mundo, y las afirmaciones
independientes se desincronizan.

El `×100` es correcto para una opción equity/ETF estándar. No es una ley:

  - Una acción corporativa (split no redondo, spin-off, fusión en efectivo) cambia
    el *deliverable* y deja contratos ajustados cuyo subyacente ya no son 100
    acciones limpias. OCC los publica con raíz distinta (AAPL1, AAPL2...).
  - Un future option no vale 100 × F. YM vale 5 × F, MYM 0.5 × F, ES 50 × F.
  - Un índice puede tener multiplicador propio.

Cuando el multiplicador es un literal repetido, un contrato ajustado o un futuro
no dan un error: dan una cifra monetaria plausible y equivocada. Eso es peor.

LA REGLA
--------
Ninguna fórmula monetaria recibe `symbol` y `multiplier` por separado. Recibe un
`ContractSpec` y consulta `spec.multiplier`. Si el contrato no se puede describir,
la ruta crítica falla cerrada (`InvalidContract`) en vez de inventar un tamaño.

LO QUE ESTE MÓDULO NO HACE
--------------------------
No decide señales, no pondera proveedores y no elige modelo de precio por gusto:
`option_model` sale de la física del instrumento (equity americano, índice europeo
cash-settled, futuro Black-76), no de una preferencia de configuración.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, Optional

from . import instruments
from .obs import note as _obs_note
from .quant_errors import InvalidContract

# ── convenciones OCC ────────────────────────────────────────────────────────────
# OSI 21: raíz (6, padded) + YYMMDD + C/P + strike×1000 en 8 dígitos.
_OSI_RE = re.compile(r"^(?P<root>[A-Z][A-Z0-9]{0,5})\s*(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")
# Una raíz que termina en dígito señala deliverable NO estándar (ajuste por acción
# corporativa). OCC asigna AAPL1/AAPL2 precisamente para no reutilizar la raíz.
_ADJUSTED_ROOT_RE = re.compile(r"^(?P<base>[A-Z]{1,5})(?P<seq>[1-9])$")

STANDARD_EQUITY_MULTIPLIER = 100.0

# Estados de procedencia del propio spec: de dónde salió cada campo duro.
SOURCE_PROVIDER = "PROVIDER"          # el proveedor declaró multiplier/deliverable
SOURCE_REGISTRY = "REGISTRY"          # convención del instrumento (instruments.py)
SOURCE_OSI = "OSI"                    # deducido del símbolo OCC
SOURCE_DEFAULT = "DEFAULT"            # convención equity estándar, sin confirmación


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _clean(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


@dataclass(frozen=True)
class ContractSpec:
    """Descripción física y completa de un contrato negociable.

    Congelado a propósito: si una sección pudiera mutar el multiplicador después de
    que otra ya calculó con él, volveríamos al problema que este módulo elimina.
    Para derivar una variante se usa `spec.with_(...)`, que devuelve otra instancia.
    """

    contract_symbol: str
    underlying: str
    asset_class: str                     # EQUITY | ETF | INDEX | FUTURE
    option_type: Optional[str]           # CALL | PUT | None (subyacente)
    strike: Optional[float]
    expiration: Optional[date]
    multiplier: float
    exercise_style: str                  # AMERICAN | EUROPEAN
    settlement: str                      # PHYSICAL | CASH
    option_model: str                    # instruments.MODEL_*
    tick_size: float
    currency: str = "USD"
    deliverable_shares: Optional[float] = None
    deliverable_cash: float = 0.0
    adjusted: bool = False
    supported: bool = True
    session: str = "US_EQUITY"
    multiplier_source: str = SOURCE_DEFAULT
    source: str = "ITM"
    timestamp: Optional[str] = None
    notes: str = ""
    extra: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    # ── derivados ───────────────────────────────────────────────────────────────
    @property
    def is_option(self) -> bool:
        return self.option_type in ("CALL", "PUT")

    @property
    def is_call(self) -> bool:
        return self.option_type == "CALL"

    @property
    def minutes_per_year(self) -> float:
        s = instruments.SESSIONS.get(self.session, instruments.SESSIONS["US_EQUITY"])
        return float(s["minutes_per_day"]) * float(s["days_per_year"])

    def dte(self, asof: date | datetime | None = None) -> Optional[float]:
        """Días naturales hasta el vencimiento. None si no hay expiración conocida."""
        if self.expiration is None:
            return None
        ref = asof or datetime.now(timezone.utc)
        ref_d = ref.date() if isinstance(ref, datetime) else ref
        return float((self.expiration - ref_d).days)

    def notional(self, price: float, quantity: float = 1.0) -> float:
        """Valor económico de `quantity` contratos a `price` por unidad de subyacente.

        Este es el único sitio del programa autorizado a convertir prima a dinero.
        """
        p = _f(price)
        q = _f(quantity)
        if p is None or q is None:
            raise InvalidContract(self.contract_symbol, "prima o cantidad no numérica")
        return p * q * self.multiplier

    def with_(self, **changes: Any) -> "ContractSpec":
        return replace(self, **changes)

    def describe(self) -> Dict[str, Any]:
        return {
            "contract_symbol": self.contract_symbol,
            "underlying": self.underlying,
            "asset_class": self.asset_class,
            "option_type": self.option_type,
            "strike": self.strike,
            "expiration": self.expiration.isoformat() if self.expiration else None,
            "dte": self.dte(),
            "multiplier": self.multiplier,
            "multiplier_source": self.multiplier_source,
            "exercise_style": self.exercise_style,
            "settlement": self.settlement,
            "option_model": self.option_model,
            "tick_size": self.tick_size,
            "currency": self.currency,
            "deliverable_shares": self.deliverable_shares,
            "deliverable_cash": self.deliverable_cash,
            "adjusted": self.adjusted,
            "supported": self.supported,
            "session": self.session,
            "source": self.source,
            "timestamp": self.timestamp,
            "notes": self.notes,
        }


# ── parsing OSI ─────────────────────────────────────────────────────────────────

def parse_osi(contract_symbol: Any) -> Optional[Dict[str, Any]]:
    """Descompone un símbolo OCC/OSI. Devuelve None si no lo es (no lanza).

    Se acepta con y sin relleno de espacios porque los proveedores no coinciden:
    Alpaca entrega `AAPL240119C00150000`, otros entregan la forma padded de 21.
    """
    raw = _clean(contract_symbol)
    if not raw:
        return None
    m = _OSI_RE.match(raw)
    if not m:
        return None
    root = m.group("root")
    try:
        exp = date(2000 + int(m.group("y")), int(m.group("m")), int(m.group("d")))
    except ValueError:
        return None
    adj = _ADJUSTED_ROOT_RE.match(root)
    return {
        "root": root,
        "underlying": adj.group("base") if adj else root,
        "expiration": exp,
        "option_type": "CALL" if m.group("cp") == "C" else "PUT",
        "strike": int(m.group("strike")) / 1000.0,
        "adjusted_root": bool(adj),
    }


def _as_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:len(fmt) + 2] if "T" in fmt else text, fmt).date()
        except ValueError:
            # Formato no coincidente: se prueba el siguiente. Sólo si TODOS fallan
            # el resultado es None, y eso sí lo ve el llamador.
            _tried = fmt
    try:  # ISO con zona
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _option_type(value: Any) -> Optional[str]:
    t = str(value or "").strip().upper()
    if not t:
        return None
    if t.startswith("C"):
        return "CALL"
    if t.startswith("P"):
        return "PUT"
    return None


def _deliverable_multiplier(row: Dict[str, Any]) -> tuple[Optional[float], Optional[float], float]:
    """Lee `deliverables` de Alpaca. Devuelve (multiplier, acciones, efectivo).

    Alpaca publica los deliverables de un contrato ajustado como una lista de
    componentes con `type` (equity|cash) y `amount`. Un contrato estándar entrega
    100 acciones; uno ajustado puede entregar 62 acciones + $173.40, y entonces el
    valor de un punto de subyacente YA NO son 100 dólares.
    """
    raw = row.get("deliverables")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None, None, 0.0
    shares = 0.0
    cash = 0.0
    for comp in raw:
        if not isinstance(comp, dict):
            continue
        amount = _f(comp.get("amount"))
        if amount is None:
            continue
        kind = str(comp.get("type") or "").strip().lower()
        if kind in ("equity", "stock", "share", "shares"):
            shares += amount
        elif kind in ("cash", "currency"):
            cash += amount
    if shares <= 0.0:
        return None, None, cash
    return shares, shares, cash


def from_symbol(symbol: Any, *, source: str = "ITM", timestamp: str | None = None) -> ContractSpec:
    """Spec del SUBYACENTE (no de una opción). Útil para exposición a nivel activo."""
    sym = _clean(symbol) or "UNKNOWN"
    inst = instruments.get(sym)
    return ContractSpec(
        contract_symbol=sym, underlying=sym, asset_class=inst.asset_class,
        option_type=None, strike=None, expiration=None,
        multiplier=float(inst.multiplier), exercise_style=inst.exercise,
        settlement=inst.settlement, option_model=inst.option_model,
        tick_size=float(inst.tick_size), session=inst.session,
        supported=bool(inst.supported),
        multiplier_source=SOURCE_REGISTRY if sym in instruments.REGISTRY else SOURCE_DEFAULT,
        source=source, timestamp=timestamp,
        notes=inst.reason or "",
    )


def from_row(row: Dict[str, Any], *, underlying: Any = None, source: str = "PROVIDER",
             timestamp: str | None = None) -> ContractSpec:
    """Construye el spec de UNA fila de cadena, mezclando proveedor + convención.

    Orden de autoridad sobre el multiplicador, de mayor a menor:
      1. `deliverables` explícitos (lo que realmente se entrega).
      2. `multiplier`/`contract_size` declarado por el proveedor.
      3. Convención del instrumento en `instruments.REGISTRY`.
      4. Equity estándar.
    Cada nivel se registra en `multiplier_source` para que el Auditor pueda exigir
    confirmación antes de tratar una cifra monetaria como institucional.
    """
    if not isinstance(row, dict):
        raise InvalidContract(str(row), "la fila del contrato no es un mapeo")

    csym = _clean(row.get("contract_symbol") or row.get("symbol") or row.get("contractSymbol") or "")
    osi = parse_osi(csym)

    und = _clean(underlying or row.get("underlying") or row.get("underlying_symbol")
                 or row.get("root_symbol") or (osi or {}).get("underlying") or "")
    if not und and osi:
        und = osi["underlying"]
    if not und:
        raise InvalidContract(csym or "?", "no se puede determinar el subyacente")

    inst = instruments.get(und)

    otype = _option_type(row.get("option_type") or row.get("type") or row.get("right")) \
        or (osi or {}).get("option_type")
    strike = _f(row.get("strike") or row.get("strike_price")) or (osi or {}).get("strike")
    expiry = _as_date(row.get("expiration") or row.get("expiration_date") or row.get("expiry")) \
        or (osi or {}).get("expiration")

    dl_mult, dl_shares, dl_cash = _deliverable_multiplier(row)
    declared = _f(row.get("multiplier") or row.get("contract_size") or row.get("size"))

    if dl_mult is not None and dl_mult > 0:
        multiplier, msource = float(dl_mult), SOURCE_PROVIDER
    elif declared is not None and declared > 0:
        multiplier, msource = float(declared), SOURCE_PROVIDER
    elif und in instruments.REGISTRY:
        multiplier, msource = float(inst.multiplier), SOURCE_REGISTRY
    else:
        multiplier, msource = STANDARD_EQUITY_MULTIPLIER, SOURCE_DEFAULT

    adjusted = bool((osi or {}).get("adjusted_root")) or bool(row.get("adjusted")) \
        or (dl_mult is not None and abs(dl_mult - STANDARD_EQUITY_MULTIPLIER) > 1e-9) \
        or dl_cash != 0.0

    style = str(row.get("style") or row.get("exercise_style") or "").strip().upper()
    exercise = style if style in ("AMERICAN", "EUROPEAN") else inst.exercise

    notes = inst.reason or ""
    if adjusted:
        notes = ("Contrato AJUSTADO: el deliverable no son 100 acciones limpias. "
                 "Toda cifra monetaria usa el multiplicador real. " + notes).strip()

    return ContractSpec(
        contract_symbol=csym or und, underlying=und, asset_class=inst.asset_class,
        option_type=otype, strike=strike, expiration=expiry,
        multiplier=multiplier, exercise_style=exercise,
        settlement=str(row.get("settlement") or inst.settlement).upper(),
        option_model=inst.option_model, tick_size=float(inst.tick_size),
        session=inst.session, deliverable_shares=dl_shares, deliverable_cash=dl_cash,
        adjusted=adjusted, supported=bool(inst.supported), multiplier_source=msource,
        source=source, timestamp=timestamp, notes=notes,
    )


# ── camino vectorizado ──────────────────────────────────────────────────────────

def multiplier_for(symbol: Any, row: Dict[str, Any] | None = None) -> float:
    """Atajo escalar. Sigue pasando por el spec: no hay una segunda regla aquí."""
    if row:
        try:
            return from_row(row, underlying=symbol).multiplier
        except InvalidContract as exc:
            # La fila no describe un contrato; se cae a la convención del
            # instrumento, pero queda contabilizado: una cadena que degrada aquí
            # sistemáticamente significa que el proveedor cambió el esquema.
            _obs_note("contract_spec:multiplier_for", exc, severity="DEGRADED")
    return from_symbol(symbol).multiplier


def infer_symbol(frame, fallback: Any = None) -> str:
    """Subyacente de un frame de cadena o de cinta, sin adivinar.

    Se mira primero la columna explícita; si no está, se deduce del símbolo OCC del
    primer contrato. Sólo si nada de eso existe se usa el `fallback` del llamador.
    """
    for col in ("underlying_symbol", "underlying", "root_symbol", "symbol"):
        if col in getattr(frame, "columns", ()):
            vals = frame[col].dropna().astype(str)
            if len(vals):
                cand = _clean(vals.iloc[0])
                if cand:
                    parsed = parse_osi(cand)
                    return parsed["underlying"] if parsed else cand
    if "contract_symbol" in getattr(frame, "columns", ()):
        vals = frame["contract_symbol"].dropna().astype(str)
        if len(vals):
            parsed = parse_osi(vals.iloc[0])
            if parsed:
                return parsed["underlying"]
    return _clean(fallback) or "UNKNOWN"


def row_multiplier(row: Any, symbol: Any = None) -> float:
    """Multiplicador de UNA fila (dict o Series). Nunca devuelve un literal a ciegas.

    Orden: columna `contract_multiplier` del proveedor → deliverables/OSI → registro
    del instrumento. El `100.0` que antes estaba escrito en cada fórmula monetaria
    sólo sobrevive como convención de `instruments.py` para equity/ETF, donde es
    correcta y está documentada en un único sitio.
    """
    try:
        data = dict(row) if not isinstance(row, dict) else row
    except (TypeError, ValueError):
        return from_symbol(symbol).multiplier
    declared = _f(data.get("contract_multiplier"))
    if declared is not None and declared > 0:
        return float(declared)
    und = symbol or data.get("underlying_symbol") or data.get("underlying")
    try:
        return from_row(data, underlying=und).multiplier
    except InvalidContract:
        return from_symbol(und or symbol).multiplier


def multiplier_series(frame, symbol: Any = None):
    """Serie de multiplicadores alineada a `frame`, para el camino DataFrame.

    Los bucles por fila sobre 5.000 contratos costarian mas que todo el calculo de
    Greeks, asi que aqui se resuelve por columnas: si el proveedor trae
    `contract_multiplier`, `multiplier` o `deliverables` se respeta fila a fila; si
    no, se aplica la convencion del instrumento a todo el bloque. El resultado es
    identico al de `from_row`, y en ningun caso aparece un `100.0` escrito a mano.
    """
    import numpy as np
    import pandas as pd

    n = len(frame)
    und = _clean(symbol if isinstance(symbol, (str, bytes)) or symbol is None else None) \
        or infer_symbol(frame)
    base = from_symbol(und).multiplier
    index = getattr(frame, "index", None)
    out = pd.Series(np.full(n, float(base)), index=index, dtype=float)
    if n == 0:
        return out

    columns = tuple(getattr(frame, "columns", ()))
    for col in ("contract_multiplier", "multiplier", "contract_size"):
        if col in columns:
            declared = pd.to_numeric(frame[col], errors="coerce")
            good = declared.notna() & (declared > 0)
            if good.any():
                out = out.where(~good, declared)
            break

    if "deliverables" in columns:
        for idx, raw in frame["deliverables"].items():
            dl, _shares, _cash = _deliverable_multiplier({"deliverables": raw})
            if dl is not None and dl > 0:
                out.at[idx] = float(dl)
    return out.astype(float)


def specs_from_frame(frame, symbol: Any) -> Iterable[ContractSpec]:
    """Itera specs de una cadena. Para auditoría y diagnóstico, no para el hot path."""
    for _idx, row in frame.iterrows():
        try:
            yield from_row(row.to_dict(), underlying=symbol)
        except InvalidContract as exc:
            # Una fila que no describe un contrato no puede auditarse, pero tampoco
            # debe tumbar la auditoría de las otras 4.999. Queda contabilizada.
            _obs_note("contract_spec:specs_from_frame", exc, severity="DEGRADED")

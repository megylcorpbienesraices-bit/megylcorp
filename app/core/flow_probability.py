"""Clasificación probabilística del agresor (v1.42).

DE UNA ETIQUETA A UNA DISTRIBUCIÓN
----------------------------------
v1.41 devolvía `BUY`, `SELL`, `MID` o `UNKNOWN` con un escalar de confianza pegado
al lado. Eso obliga a quien consume el dato a elegir un umbral y tirar el resto de
la información. Peor: agregar etiquetas duras hace que un print a mitad de spread
con una cotización de 4 segundos pese lo mismo que uno pegado al ask con NBBO viva.

Aquí la salida es una distribución:

    {"buy": 0.91, "sell": 0.07, "unknown": 0.02}

Y el flujo neto se agrega con ESA probabilidad, no con la etiqueta. Una cinta de
3.000 prints ambiguos deja de fabricar una convicción direccional que no existe.

QUÉ ENTRA EN EL MODELO
----------------------
  posición en el spread   (2s − 1), s = (precio − bid)/(ask − bid)
  vigencia de la quote    decaimiento exponencial con la edad
  anchura del spread      un spread ancho hace el mid menos informativo
  regla del tick          respaldo causal contra el print anterior
  agresión por tamaño     una operación mayor que el tamaño en el toque barre el libro
  condiciones del print   cruzado, bloqueado, reporte tardío, corrección

HONESTIDAD SOBRE LOS COEFICIENTES
---------------------------------
Los pesos son `EXPERT_BASELINE_V1`: vienen de la microestructura conocida
(Lee-Ready y su literatura posterior), no de un ajuste sobre datos etiquetados —
porque no existe una etiqueta pública de quién inició cada print de opciones.
Por eso el módulo expone `calibrate()`: cuando el replay histórico acumule prints
con quote causal y reacción posterior, se puede reajustar y someterlo a la puerta
champion/challenger como cualquier otro modelo. Declararlos experto y dejarlos
auditables es honesto; llamarlos «calibrados» sin datos no lo sería.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

BASELINE_ID = "EXPERT_BASELINE_V1"

# Coeficientes en log-odds de «iniciado por el comprador».
COEFFS: Dict[str, float] = {
    "intercept": 0.0,
    "spread_position": 3.20,   # el dominante: dónde cayó el print dentro del spread
    "tick_rule": 0.85,         # respaldo causal
    "size_sweep": 0.70,        # barrer el tamaño del toque revela urgencia
    "at_touch": 0.55,          # exactamente en bid o ask
}

# Constantes de microestructura.
QUOTE_HALFLIFE_MS = 900.0      # a 900 ms la quote vale la mitad como evidencia
WIDE_SPREAD_RATIO = 0.30       # spread/mid por encima del cual el mid deja de informar
MAX_USEFUL_QUOTE_MS = 6000.0   # más allá, la quote no clasifica nada


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


@dataclass(frozen=True)
class FlowProbability:
    """Distribución sobre quién inició el print, más el rastro de por qué."""

    buy: float
    sell: float
    unknown: float
    method: str
    quote_weight: float
    features: Dict[str, float] = field(default_factory=dict)
    conditions: tuple = ()
    note: str = ""

    @property
    def net_sign(self) -> float:
        """Signo esperado, en [-1, 1]. Esto es lo que debe agregarse, no la etiqueta."""
        return self.buy - self.sell

    @property
    def label(self) -> str:
        """Etiqueta legible. Existe para la interfaz; el motor usa la distribución."""
        if self.unknown >= 0.5:
            return "UNKNOWN"
        return "BUY" if self.buy > self.sell else "SELL"

    def describe(self) -> Dict[str, Any]:
        return {"buy": round(self.buy, 4), "sell": round(self.sell, 4),
                "unknown": round(self.unknown, 4), "net_sign": round(self.net_sign, 4),
                "label": self.label, "method": self.method,
                "quote_weight": round(self.quote_weight, 4),
                "features": {k: round(v, 5) for k, v in self.features.items()},
                "conditions": list(self.conditions), "note": self.note,
                "model": BASELINE_ID}


def _condition_flags(conditions: Any) -> tuple:
    """Normaliza códigos de condición OPRA a los que cambian la interpretación."""
    raw = conditions
    if isinstance(raw, str):
        parts = [p.strip().upper() for p in raw.replace(",", " ").split() if p.strip()]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(p).strip().upper() for p in raw if str(p).strip()]
    else:
        parts = []
    flags = []
    # OPRA: 'a' late report, 'b' out of sequence / cancel, 'z' corrected.
    for p in parts:
        if p in ("A", "LATE", "LATE_REPORT"):
            flags.append("LATE_REPORT")
        elif p in ("B", "OUT_OF_SEQUENCE", "CANCEL", "CANCELLED"):
            flags.append("OUT_OF_SEQUENCE")
        elif p in ("Z", "CORRECTED", "CORRECTION"):
            flags.append("CORRECTED")
        elif p in ("I", "ODD_LOT"):
            flags.append("ODD_LOT")
    return tuple(dict.fromkeys(flags))


def classify(*, price: Any, bid: Any = None, ask: Any = None,
             quote_age_ms: Any = None, prev_trade_price: Any = None,
             size: Any = None, bid_size: Any = None, ask_size: Any = None,
             conditions: Any = None) -> FlowProbability:
    """Distribución de probabilidad sobre el iniciador de un print de opciones."""
    px = _f(price)
    b, a = _f(bid), _f(ask)
    flags = _condition_flags(conditions)

    if px is None or px <= 0:
        return FlowProbability(0.0, 0.0, 1.0, "NO_TRADE_PRICE", 0.0,
                               conditions=flags, note="sin precio de operación utilizable")

    # Un print corregido o fuera de secuencia no describe la agresión del momento.
    if "CORRECTED" in flags or "OUT_OF_SEQUENCE" in flags:
        return FlowProbability(0.0, 0.0, 1.0, "NON_CLASSIFIABLE_CONDITION", 0.0,
                               conditions=flags,
                               note="print corregido o fuera de secuencia: no representa "
                                    "la agresión en ese instante")

    feats: Dict[str, float] = {}
    conditions_out = list(flags)

    # ── evidencia de cotización ─────────────────────────────────────────────────
    quote_weight = 0.0
    spread_pos_signal = 0.0
    at_touch = 0.0
    if b is not None and a is not None and b >= 0 and a > 0:
        if a < b:
            conditions_out.append("CROSSED_QUOTE")
        elif a == b:
            conditions_out.append("LOCKED_QUOTE")
        else:
            spread = a - b
            mid = 0.5 * (a + b)
            s = (px - b) / spread
            s_cl = min(max(s, 0.0), 1.0)
            feats["spread_position_raw"] = s
            spread_pos_signal = 2.0 * s_cl - 1.0

            age = _f(quote_age_ms)
            if age is None:
                quote_weight = 0.55   # sin edad declarada no se puede presumir vigencia
                conditions_out.append("QUOTE_AGE_UNKNOWN")
            elif age > MAX_USEFUL_QUOTE_MS:
                quote_weight = 0.0
                conditions_out.append("QUOTE_TOO_OLD")
            else:
                quote_weight = math.exp(-math.log(2.0) * max(age, 0.0) / QUOTE_HALFLIFE_MS)

            rel_spread = spread / max(mid, 1e-9)
            width_penalty = 1.0 / (1.0 + max(rel_spread / WIDE_SPREAD_RATIO, 0.0) ** 2)
            quote_weight *= width_penalty
            feats["relative_spread"] = rel_spread
            feats["quote_age_ms"] = 0.0 if age is None else float(age)

            tick = max(0.01, round(spread, 4) * 0.0 + 0.01)
            if abs(px - a) <= tick * 0.5:
                at_touch = 1.0
            elif abs(px - b) <= tick * 0.5:
                at_touch = -1.0

    # ── regla del tick (causal, contra el print anterior del mismo contrato) ─────
    tick_signal = 0.0
    prev = _f(prev_trade_price)
    if prev is not None and prev > 0:
        if px > prev:
            tick_signal = 1.0
        elif px < prev:
            tick_signal = -1.0
    feats["tick_rule"] = tick_signal

    # ── agresión por tamaño ─────────────────────────────────────────────────────
    sweep_signal = 0.0
    sz = _f(size)
    if sz and sz > 0:
        touch = _f(ask_size) if spread_pos_signal >= 0 else _f(bid_size)
        if touch and touch > 0:
            ratio = sz / touch
            feats["size_vs_touch"] = ratio
            if ratio > 1.0:
                # Barre más de lo que había en el toque: urgencia, en la dirección
                # que ya insinúa la posición en el spread.
                sweep_signal = math.copysign(min(math.log1p(ratio - 1.0), 1.5),
                                             spread_pos_signal if spread_pos_signal != 0 else tick_signal)
                conditions_out.append("SWEEP_LIKE")

    feats["spread_position_signal"] = spread_pos_signal
    feats["quote_weight"] = quote_weight
    feats["at_touch"] = at_touch
    feats["size_sweep"] = sweep_signal

    # ── log-odds ────────────────────────────────────────────────────────────────
    z = (COEFFS["intercept"]
         + COEFFS["spread_position"] * spread_pos_signal * quote_weight
         + COEFFS["at_touch"] * at_touch * quote_weight
         + COEFFS["tick_rule"] * tick_signal * (1.0 if quote_weight < 0.25 else 0.45)
         + COEFFS["size_sweep"] * sweep_signal * quote_weight)

    evidence = (abs(spread_pos_signal) * quote_weight
                + 0.45 * abs(tick_signal) * (1.0 if quote_weight < 0.25 else 0.45))
    if evidence <= 1e-6:
        return FlowProbability(0.0, 0.0, 1.0, "NO_EVIDENCE", quote_weight,
                               features=feats, conditions=tuple(dict.fromkeys(conditions_out)),
                               note="ni la cotización ni la regla del tick clasifican este print")

    # `unknown` es masa de probabilidad reservada: cuanta menos evidencia, más queda
    # sin asignar. Esto es lo que impide que 3.000 prints ambiguos fabriquen una
    # convicción direccional agregada que los datos no respaldan.
    unknown = float(min(0.90, math.exp(-2.2 * evidence)))
    p_buy_cond = _sigmoid(z)
    buy = (1.0 - unknown) * p_buy_cond
    sell = (1.0 - unknown) * (1.0 - p_buy_cond)

    method = ("QUOTE_PROBABILISTIC" if quote_weight >= 0.25
              else ("TICK_RULE_PROBABILISTIC" if tick_signal != 0 else "WEAK_QUOTE"))
    if "LATE_REPORT" in flags:
        # El print es real, pero la quote pareada no es la del momento de la ejecución.
        unknown = min(0.95, unknown + 0.25 * (1.0 - unknown))
        scale = (1.0 - unknown) / max(buy + sell, 1e-12)
        buy, sell = buy * scale, sell * scale
        method += "_LATE"

    return FlowProbability(buy, sell, 1.0 - buy - sell, method, quote_weight,
                           features=feats, conditions=tuple(dict.fromkeys(conditions_out)),
                           note="distribución sobre el iniciador; agregar por probabilidad, "
                                "no por etiqueta")


def classify_frame(events: pd.DataFrame) -> pd.DataFrame:
    """Aplica `classify` a una cinta y devuelve las columnas probabilísticas."""
    if events is None or len(events) == 0:
        return pd.DataFrame(columns=["p_buy", "p_sell", "p_unknown", "net_sign",
                                     "flow_method", "flow_conditions"])
    rows = []
    prev_px: Dict[str, float] = {}
    for _idx, ev in events.iterrows():
        key = str(ev.get("contract_symbol") or "")
        fp = classify(price=ev.get("trade_price", ev.get("price")),
                      bid=ev.get("bid"), ask=ev.get("ask"),
                      quote_age_ms=ev.get("quote_age_ms"),
                      prev_trade_price=prev_px.get(key),
                      size=ev.get("contracts", ev.get("size")),
                      bid_size=ev.get("bid_size"), ask_size=ev.get("ask_size"),
                      conditions=ev.get("conditions"))
        px = _f(ev.get("trade_price", ev.get("price")))
        if px:
            prev_px[key] = px
        rows.append({"p_buy": fp.buy, "p_sell": fp.sell, "p_unknown": fp.unknown,
                     "net_sign": fp.net_sign, "flow_method": fp.method,
                     "flow_conditions": ",".join(fp.conditions)})
    return pd.DataFrame(rows, index=events.index)


def aggregate(events: pd.DataFrame, *, premium_column: str = "premium") -> Dict[str, Any]:
    """Flujo neto agregado POR PROBABILIDAD, con la masa sin clasificar declarada."""
    if events is None or len(events) == 0:
        return {"ready": False, "reason": "sin eventos"}
    probs = classify_frame(events)
    prem = pd.to_numeric(events.get(premium_column), errors="coerce").fillna(0.0).abs()
    buy_prem = float((probs["p_buy"] * prem).sum())
    sell_prem = float((probs["p_sell"] * prem).sum())
    unk_prem = float((probs["p_unknown"] * prem).sum())
    total = buy_prem + sell_prem + unk_prem
    classified = buy_prem + sell_prem
    return {
        "ready": True, "events": int(len(events)),
        "buy_premium": round(buy_prem, 2), "sell_premium": round(sell_prem, 2),
        "unclassified_premium": round(unk_prem, 2),
        "net_premium": round(buy_prem - sell_prem, 2),
        "classified_pct": round(100.0 * classified / total, 2) if total > 0 else 0.0,
        "bias_pct": round(100.0 * (buy_prem - sell_prem) / classified, 2) if classified > 0 else 0.0,
        "method": "PROBABILITY_WEIGHTED_PREMIUM",
        "model": BASELINE_ID,
        "note": ("El sesgo se calcula sobre la prima clasificada, no sobre el total. "
                 "La prima sin agresor identificado se declara, no se reparte."),
    }


def calibrate(labelled: pd.DataFrame, *, label_column: str = "buyer_initiated") -> Dict[str, Any]:
    """Reajusta los coeficientes sobre prints etiquetados, si algún día los hay.

    No se usa en producción hasta que el replay histórico acumule etiquetas y el
    resultado supere al baseline en la puerta champion/challenger. Devolver los
    coeficientes ajustados sin esa puerta sería sustituir un criterio declarado por
    un sobreajuste silencioso.
    """
    if labelled is None or len(labelled) < 400 or label_column not in labelled.columns:
        return {"ready": False, "reason": "muestra etiquetada insuficiente (< 400 prints)",
                "model": BASELINE_ID}
    probs = classify_frame(labelled)
    X = np.column_stack([
        np.ones(len(labelled)),
        probs["net_sign"].to_numpy(float),
    ])
    y = pd.to_numeric(labelled[label_column], errors="coerce").fillna(0.5).to_numpy(float)
    # Regresión logística por IRLS con ridge suave; el objetivo es recalibrar la
    # pendiente, no reinventar las features.
    beta = np.zeros(X.shape[1])
    for _ in range(50):
        eta = X @ beta
        p = 1.0 / (1.0 + np.exp(-np.clip(eta, -30, 30)))
        W = np.maximum(p * (1.0 - p), 1e-6)
        z = eta + (y - p) / W
        A = X.T @ (X * W[:, None]) + 1e-3 * np.eye(X.shape[1])
        new = np.linalg.solve(A, X.T @ (W * z))
        if np.max(np.abs(new - beta)) < 1e-8:
            beta = new
            break
        beta = new
    return {"ready": True, "model": "CALIBRATED_CHALLENGER_V1",
            "intercept": float(beta[0]), "slope": float(beta[1]),
            "samples": int(len(labelled)),
            "note": "Challenger. No sustituye al baseline sin pasar la puerta de promoción."}

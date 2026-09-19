"""Analytics layer for TRACE PRO 3 (v1.15.4).

Separated from the plotting code on purpose: every number below is testable without
rendering a figure, which the previous monolithic `trace_pro_2` was not.

What this adds over the v1.14.8 TRACE:

1. `heat_matrix`      - the heatmap denominator becomes comparable across time.
2. `dealer_flow_line` - cumulative delta-weighted *estimated* hedge pressure built
                        from ITM's OPRA-flow model. It is a dealer-counterparty proxy,
                        not observed dealer inventory or observed hedges.
3. `structural_walls` - Call/Put Wall by GAMMA rather than by raw OI, plus ITM gamma
                        balance trigger and hedge-wall proxies.
4. `expected_move_band` - a transparent ±1σ implied-move model with explicit time anchor.
5. `forward_profile` - conditional Gamma/Delta repricing after time decay, holding
                        spot/IV/OI fixed. It is a scenario, not a prediction.
6. `aggression_bars` / `aggression_strength` - information bars with CHURN/TIMEOUT,
                        continuous volume-weighted control and true-absorption flag.
7. `tape_confirmation` / `bar_cadence` - tick-level confirmation inside the active
                        Scanner zone plus participation regime.
8. `time_aggression_bars` / `multi_timeframe_aggression` - clocked order-flow context
                        normalised by trade-size variance, strictly causal and SHADOW-only.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, Iterable

import numpy as np
import pandas as pd
from .frame_guards import numeric_column
from .expiry_clock import year_fraction, year_fraction_array
from .obs import note as _obs_note
from .contract_spec import multiplier_series


# ---------------------------------------------------------------- heat scaling
def heat_matrix(pivot: pd.DataFrame, scale_mode: str = "session",
                anchor: Dict[str, Any] | None = None,
                neutral_millions: float = 0.0,
                neutral_pct: float = 12.0) -> Dict[str, Any]:
    """Signed heat matrix with a denominator that does not move column by column.

    THE BUG THIS FIXES. v1.14.8 used ``den = nanmax(|raw|, axis=0)``: one denominator
    per timestamp. Colour then encoded "how big is this strike relative to the rest of
    THIS instant", so a session whose gamma decayed 10x rendered pixel-identical to one
    that held. On a panel whose entire premise is gamma THROUGH TIME, the time axis
    carried no magnitude information at all.

    Modes:
      column   - legacy behaviour. Reads pure shape. Kept because it is genuinely the
                 better view when you only care about which strike dominates right now.
      session  - one denominator for the whole window (p98 of |raw|). Columns become
                 comparable to each other.
      anchor   - denominator from this asset's own history (core.scale_anchors), so
                 columns are comparable across SESSIONS too, not just within one.
    """
    raw = np.asarray(pivot.values, dtype=float) / 1e6
    out = {"raw": raw, "scale_mode": str(scale_mode), "denominator": None,
           "denominator_source": None, "comparable_across_time": False,
           "comparable_across_sessions": False}
    if raw.size == 0:
        out["z"] = raw
        return out

    mode = str(scale_mode or "session").strip().lower()
    den = None
    if mode == "anchor" and anchor:
        try:
            hi = float(anchor.get("hi"))
            n = int(anchor.get("n", 0))
            if math.isfinite(hi) and n >= 3:
                den = float(np.expm1(hi)) / 1e6
                out["denominator_source"] = f"ANCLA HISTÓRICA · {n} sesiones"
                out["comparable_across_time"] = True
                out["comparable_across_sessions"] = True
        except Exception:
            den = None
        if den is None or den <= 1e-12:
            mode = "session"   # anchor not usable yet; degrade, never fabricate

    if den is None and mode == "session":
        finite = np.abs(raw[np.isfinite(raw)])
        den = float(np.nanpercentile(finite, 98)) if finite.size else 1.0
        out["denominator_source"] = "P98 DE LA VENTANA"
        out["comparable_across_time"] = True

    if den is None or not math.isfinite(den) or den <= 1e-12:
        if mode not in ("column",):
            den = 1.0
        else:
            per_col = np.nanmax(np.abs(raw), axis=0)
            den = np.where(per_col > 1e-12, per_col, 1.0)
            out["denominator_source"] = "MÁXIMO POR COLUMNA (relativo)"

    if mode == "column":
        per_col = np.nanmax(np.abs(raw), axis=0)
        den = np.where(per_col > 1e-12, per_col, 1.0)
        out["denominator_source"] = "MÁXIMO POR COLUMNA (relativo)"

    z = raw / den * 100.0
    z = np.clip(z, -100.0, 100.0)

    # The neutral cut must be absolute whenever the scale is absolute, otherwise
    # "low concentration" silently means a different dollar amount every column.
    if mode == "column":
        z = np.where(np.abs(z) < float(neutral_pct), 0.0, z)
        out["neutral_rule"] = f"|z| < {neutral_pct:.0f}% del máximo de la columna"
    else:
        cut = float(neutral_millions) if neutral_millions > 0 else float(np.asarray(den).max()) * 0.12
        z = np.where(np.abs(raw) < cut, 0.0, z)
        out["neutral_rule"] = f"|GEX| < {cut:.3f}M (absoluto)"
        out["neutral_millions"] = round(cut, 4)

    out["z"] = z
    out["denominator"] = float(np.asarray(den).max())
    return out



# ----------------------------------------- estimated delta-weighted hedge pressure
def dealer_flow_line(flow_events: pd.DataFrame, counterparty_share: float = 0.70,
                     resample: str = "1min") -> pd.DataFrame:
    """Cumulative delta-weighted *estimated* hedge pressure through the session.

    This is a model proxy, not observed dealer inventory or observed hedges. ITM infers
    a possible dealer-counterparty requirement from OPRA aggressor, Delta, contracts,
    opening likelihood and the explicit ``counterparty_share`` assumption.

    Positive = estimated cumulative BUY pressure on the underlying under that scenario.
    """
    cols = ["timestamp", "hedge_notional", "cumulative", "events"]
    if flow_events is None or flow_events.empty:
        return pd.DataFrame(columns=cols)
    try:
        from .dealer_intelligence import _hedge_rows
        rows = _hedge_rows(flow_events)
    except Exception:
        return pd.DataFrame(columns=cols)
    if rows is None or rows.empty:
        return pd.DataFrame(columns=cols)

    cp = float(np.clip(counterparty_share, 0.0, 1.0))
    df = pd.DataFrame({"timestamp": pd.to_datetime(rows["timestamp"], errors="coerce"),
                       "hedge_notional": pd.to_numeric(rows["unit_hedge_notional"],
                                                       errors="coerce").fillna(0.0) * cp})
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
    if df.empty:
        return pd.DataFrame(columns=cols)
    g = (df.set_index("timestamp")
           .resample(str(resample))
           .agg(hedge_notional=("hedge_notional", "sum"))
           .fillna(0.0))
    g["events"] = (df.set_index("timestamp").resample(str(resample)).size().reindex(g.index).fillna(0).astype(int))
    g["cumulative"] = g["hedge_notional"].cumsum()
    g["counterparty_share_assumption_pct"] = round(cp * 100.0, 1)
    g["is_estimate"] = True
    return g.reset_index()


def max_pain(chain: pd.DataFrame) -> float | None:
    """Open-interest max-pain estimate for the currently selected expiry window.

    Moved here from premarket_intelligence.py (v1.31.0) so the SpotGamma-style
    Key Levels report and the premarket panel share one implementation instead
    of two copies of the same payout-minimization logic.
    """
    if chain is None or chain.empty:
        return None
    x = chain.copy()
    x["strike"] = numeric_column(x,"strike",float("nan"))
    x["open_interest"] = numeric_column(x,"open_interest",0.0)
    x = x.dropna(subset=["strike"])
    if x.empty:
        return None
    strikes = np.sort(x["strike"].unique())
    k = x["strike"].to_numpy(float)
    oi = x["open_interest"].to_numpy(float)
    is_call = x.get("option_type", pd.Series([""] * len(x), index=x.index)).astype(str).str.lower().str.startswith("c").to_numpy()
    payouts = []
    for settle in strikes:
        intrinsic = np.where(is_call, np.maximum(settle - k, 0.0), np.maximum(k - settle, 0.0))
        payouts.append(float(np.sum(intrinsic * oi)))
    return float(strikes[int(np.argmin(payouts))]) if payouts else None


def atm_iv_and_dte(chain: pd.DataFrame, spot: float) -> tuple[float | None, float | None]:
    """Median ATM implied vol (%) and DTE (days) from the nearest-to-spot strikes.

    Same selection logic already used by expected_move_band_from_chain(), factored
    out so a single scalar Expected Move (Key Levels report) does not need a whole
    time-series band just to read one number.
    """
    if chain is None or chain.empty:
        return None, None
    k = numeric_column(chain,"strike",float("nan"))
    iv = numeric_column(chain,"iv",float("nan"))
    dte = numeric_column(chain,"dte",float("nan"))
    valid = k.notna() & iv.notna() & dte.notna() & (iv > 0) & (dte > 0)
    if not valid.any():
        return None, None
    dist = (k[valid] - float(spot)).abs()
    md = float(dist.min())
    atm = valid & ((k - float(spot)).abs() <= md + 1e-9)
    iv_pct = float(pd.to_numeric(iv[atm], errors="coerce").median() * 100.0)
    dte_days = float(pd.to_numeric(dte[atm], errors="coerce").median())
    return iv_pct, dte_days


# -------------------------------------------------------------- key structures
def _barrier_wall_strength(enriched: pd.DataFrame, symbol: str = "DIA") -> pd.DataFrame | None:
    """Fuerza de cobertura EN CADA STRIKE, evaluando la gamma con el spot en ese strike.

    POR QUÉ NO VALE LA GAMMA AL SPOT ACTUAL
    ---------------------------------------
    La gamma de Black-Scholes es máxima en el dinero. Si se pondera cada strike por
    su gamma evaluada al spot DE AHORA, el argmax tiende al ATM por construcción, y
    a DTE corto lo hace de forma abrumadora: medido sobre cadena sintética con un
    muro sembrado a 2.6 sigmas, a 1 DTE hacía falta 40x el OI del ATM para que el
    Call Wall lo detectara, y el Put Wall no lo detectaba NUNCA. Resultado: los dos
    muros se publicaban pegados al spot (0.19 y 0.21 sigmas), que es el strike ATM
    dicho dos veces, no una estructura.

    LA PREGUNTA CORRECTA
    --------------------
    Un muro no pregunta «¿dónde está la gamma ahora?» — eso es Gamma Flip / NetGEX.
    Pregunta «si el precio LLEGARA a ese strike, cuánta cobertura resistiría allí».
    Esa es la gamma evaluada con S = K. Cada strike se juzga en su propio dinero, así
    que la ventaja del ATM desaparece y lo que decide es la concentración real de OI,
    que es como se forman los muros de verdad.

    Es el mismo razonamiento que el motor ya aplica en `dynamic_gamma_flip_snapshot`,
    que reprecia la cadena entera a spots hipotéticos en vez de leer un único punto.
    Los muros simplemente nunca habían recibido ese tratamiento.

    Devuelve un frame por strike con la fuerza de call y de put, o None si la cadena
    no trae lo necesario. No inventa: sin IV/DTE no hay muro por esta vía.
    """
    need = {"strike", "iv", "dte", "option_type", "open_interest"}
    if enriched is None or not isinstance(enriched, pd.DataFrame) or enriched.empty:
        return None
    if not need.issubset(set(enriched.columns)):
        return None
    x = enriched
    if "timestamp" in x.columns:
        ts = pd.to_datetime(x["timestamp"], errors="coerce")
        if ts.notna().any():
            x = x[ts == ts.max()]
    x = x.copy()
    K = pd.to_numeric(x["strike"], errors="coerce").to_numpy(float)
    iv = pd.to_numeric(x["iv"], errors="coerce").to_numpy(float)
    dte = pd.to_numeric(x["dte"], errors="coerce").to_numpy(float)
    oi = pd.to_numeric(x["open_interest"], errors="coerce").fillna(0.0).to_numpy(float)
    ok = np.isfinite(K) & np.isfinite(iv) & np.isfinite(dte) & (K > 0) & (iv > 0) & (dte > 0)
    if not ok.any():
        return None
    x = x.loc[x.index[ok]]
    K, iv, dte, oi = K[ok], iv[ok], dte[ok], oi[ok]
    is_call = x["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()
    sym = str(symbol or "DIA").upper()
    if "underlying_symbol" in x.columns:
        vals = x["underlying_symbol"].astype(str).str.upper()
        if vals.nunique() == 1:
            sym = str(vals.iloc[0])
    try:
        from .greeks_service import greeks_vector as _greeks_vector, model_inputs_vector
        r, q = model_inputs_vector(sym, dte)
        # S = K: cada strike evaluado en su propio dinero. Se pasa por la MISMA puerta
        # de Greeks que usa el resto del motor; aquí no hay una segunda fórmula.
        g = _greeks_vector(sym, K, K, year_fraction_array(dte), iv, is_call, r, q)
        mult = pd.to_numeric(multiplier_series(x), errors="coerce").fillna(100.0).to_numpy(float)
    except Exception as exc:
        _obs_note("trace_analytics:barrier_wall_strength", exc, severity="DEGRADED")
        return None
    strength = np.abs(np.asarray(g["gamma"], dtype=float)) * oi * mult * (K ** 2) * 0.01
    strength = np.where(np.isfinite(strength), strength, 0.0)
    out = pd.DataFrame({
        "strike": K,
        "_call_barrier": np.where(is_call, strength, 0.0),
        "_put_barrier": np.where(~is_call, strength, 0.0),
    })
    return out.groupby("strike", as_index=False).sum()


def structural_walls(curagg: pd.DataFrame, spot: float,
                     enriched: pd.DataFrame | None = None,
                     symbol: str = "DIA") -> Dict[str, Any]:
    """Call/Put Wall by Gamma, plus ITM Gamma-Balance and Hedge-Wall proxies.

    v1.14.8 drew the walls from raw open interest. OI is a headcount; what pins price
    is gamma, which is OI weighted by each contract's convexity at the current spot.
    A far strike with huge stale OI and near-zero gamma is not a wall, and the OI
    version drew it as one.

    v1.42.3 corrige el sesgo OPUESTO que introdujo aquella versión. La gamma medida
    al spot ACTUAL es máxima en el dinero, así que el argmax tendía al ATM por
    construcción. Medido sobre cadena sintética con un muro sembrado a 2.6 sigmas:
    a 1 DTE hacían falta 40x el OI del ATM para que el Call Wall lo viera, y el Put
    Wall no lo veía nunca; los dos muros salían a 0.19 y 0.21 sigmas del spot. Pasar
    de ponderar por OI a ponderar por gamma-al-spot cambió un sesgo por el contrario.

    Con ``enriched`` disponible se evalúa cada strike en su propio dinero (S=K), que
    es la pregunta que un muro hace de verdad: si el precio llegara ahí, cuánta
    cobertura resistiría. Sin la cadena se mantiene el comportamiento anterior y se
    declara en ``method``.
    """
    out = {"call_wall": None, "put_wall": None, "volatility_trigger": None,
           "hedge_wall": None, "key_gamma": None, "method": "gamma-weighted"}
    if curagg is None or curagg.empty or "strike" not in curagg.columns:
        return out
    d = curagg.copy()
    d["strike"] = pd.to_numeric(d["strike"], errors="coerce")
    d = d.dropna(subset=["strike"])
    if d.empty:
        return out
    gross = numeric_column(d,"gross_gex",0.0)
    signed = pd.to_numeric(d.get("signed_gex", d.get("signed_gex_proxy", 0)), errors="coerce").fillna(0.0)
    d = d.assign(_gross=gross, _signed=signed)

    # El muro de calls es donde se concentra la gamma DE LAS CALLS, no donde el neto
    # es mayor. Un strike con mucha call gamma y mucha put gamma tiene un neto
    # pequeño: el método neto lo pasaba por alto justo donde más cobertura hay que
    # ajustar. Cuando el desglose por tipo está disponible se usa; si no, el neto
    # sigue siendo el respaldo y queda declarado en `method`.
    call_col = next((c for c in ("call_gamma", "call_gex", "call_signed_gex") if c in d.columns), None)
    put_col = next((c for c in ("put_gamma", "put_gex", "put_signed_gex") if c in d.columns), None)
    if call_col is not None:
        d = d.assign(_call=pd.to_numeric(d[call_col], errors="coerce").fillna(0.0).abs())
    if put_col is not None:
        d = d.assign(_put=pd.to_numeric(d[put_col], errors="coerce").fillna(0.0).abs())
    out["method"] = ("gamma-weighted-by-side" if (call_col and put_col) else "gamma-weighted-net")

    above = d[d["strike"] > float(spot)]
    below = d[d["strike"] < float(spot)]
    if len(above):
        if call_col is not None and float(above["_call"].max()) > 0:
            out["call_wall"] = float(above.loc[above["_call"].idxmax(), "strike"])
        elif above["_signed"].max() > 0:
            out["call_wall"] = float(above.loc[above["_signed"].idxmax(), "strike"])
        else:
            out["call_wall"] = float(above.loc[above["_gross"].idxmax(), "strike"])
    if len(below):
        if put_col is not None and float(below["_put"].max()) > 0:
            out["put_wall"] = float(below.loc[below["_put"].idxmax(), "strike"])
        elif below["_signed"].min() < 0:
            out["put_wall"] = float(below.loc[below["_signed"].idxmin(), "strike"])
        else:
            out["put_wall"] = float(below.loc[below["_gross"].idxmax(), "strike"])

    # v1.42.3 · Muro = resistencia de cobertura EN el strike, no gamma al spot de ahora.
    # La gamma al spot actual es máxima en el dinero, así que a DTE corto arrastraba
    # los dos muros al ATM y enterraba la concentración real de OI. Cuando la cadena
    # está disponible se reevalúa cada strike en su propio dinero (S=K); el valor
    # anterior se conserva publicado como diagnóstico para poder comparar.
    barrier = _barrier_wall_strength(enriched, symbol) if enriched is not None else None
    if barrier is not None and not barrier.empty:
        out["call_wall_gamma_at_spot"] = out["call_wall"]
        out["put_wall_gamma_at_spot"] = out["put_wall"]
        b_above = barrier[barrier["strike"] > float(spot)]
        b_below = barrier[barrier["strike"] < float(spot)]
        if len(b_above) and float(b_above["_call_barrier"].max()) > 0:
            out["call_wall"] = float(b_above.loc[b_above["_call_barrier"].idxmax(), "strike"])
        if len(b_below) and float(b_below["_put_barrier"].max()) > 0:
            out["put_wall"] = float(b_below.loc[b_below["_put_barrier"].idxmax(), "strike"])
        out["method"] = "barrier-gamma-at-strike"
    out["key_gamma"] = float(d.loc[d["_gross"].idxmax(), "strike"])

    # ITM Gamma Balance Trigger proxy: cumulative signed-gamma zero crossing.
    s = d.sort_values("strike")
    cum = s["_signed"].cumsum().to_numpy(float)
    ks = s["strike"].to_numpy(float)
    sign_change = np.where(np.sign(cum[:-1]) != np.sign(cum[1:]))[0]
    if sign_change.size:
        i = int(sign_change[0])
        a, b = cum[i], cum[i + 1]
        out["volatility_trigger"] = float(ks[i] + (ks[i + 1] - ks[i]) * (0.0 - a) / (b - a)) if b != a else float(ks[i])

    # ITM Hedge Wall proxy: strike where positive signed-gamma concentration peaks.
    # This is structural positioning under ITM's transparent proxy, not observed dealer inventory.
    pos = s[s["_signed"] > 0]
    if len(pos):
        out["hedge_wall"] = float(pos.loc[pos["_signed"].idxmax(), "strike"])

    # El Hedge Wall es el pico de gamma positiva; en una cadena dominada por calls eso
    # cae en el mismo strike que el Call Wall. Dibujarlos como dos niveles distintos
    # apila dos etiquetas sobre la misma línea y los presenta como dos evidencias
    # independientes cuando son la misma observación medida de dos formas. Se declara
    # la coincidencia para que la interfaz pueda mostrarlo una sola vez.
    hw = out.get("hedge_wall")
    if hw is not None:
        same = [k for k in ("call_wall", "put_wall")
                if out.get(k) is not None and abs(float(out[k]) - float(hw)) < 1e-9]
        out["hedge_wall_coincides_with"] = same[0] if same else None
    else:
        out["hedge_wall_coincides_with"] = None
    return out


def charm_pressure_proxy(enriched: pd.DataFrame, minutes: float = 10.0,
                         symbol: str = "DIA", contract_multiplier: float = 100.0) -> pd.Series:
    """Dollar Delta-exposure drift caused only by time decay over ``minutes``.

    Spot, IV and OI are held fixed and Delta is repriced at the reduced DTE. This keeps
    TRACE Charm in the same physical unit used by the Delta heatmap (notional dollars).
    Contracts that expire before the horizon contribute zero after expiry.
    """
    if enriched is None or enriched.empty:
        return pd.Series(dtype=float)
    try:
        from .greeks_service import greeks_vector as _greeks_vector, model_inputs_vector
        x=enriched.copy()
        S=numeric_column(x,"underlying_price",float("nan")).to_numpy(float)
        K=numeric_column(x,"strike",float("nan")).to_numpy(float)
        iv=numeric_column(x,"iv",float("nan")).to_numpy(float)
        dte=numeric_column(x,"dte",float("nan")).to_numpy(float)
        oi=numeric_column(x,"open_interest",0.0).to_numpy(float)
        call=x.get("option_type",pd.Series("call",index=x.index)).astype(str).str.lower().str.startswith("c").to_numpy()
        ok=np.isfinite(S)&np.isfinite(K)&np.isfinite(iv)&np.isfinite(dte)&(S>0)&(K>0)&(iv>0)&(dte>0)
        out=np.zeros(len(x),dtype=float)
        if not ok.any(): return pd.Series(out,index=x.index)
        ahead=max(float(minutes),0.0)/1440.0
        active=dte[ok]>ahead
        d0=np.maximum(dte[ok],0.0)
        d1=np.maximum(dte[ok]-ahead,0.0)
        r0,q0=model_inputs_vector(symbol,d0); r1,q1=model_inputs_vector(symbol,d1)
        g0=_greeks_vector(symbol,S[ok],K[ok],year_fraction_array(d0),iv[ok],call[ok],r0,q0)
        g1=_greeks_vector(symbol,S[ok],K[ok],year_fraction_array(d1),iv[ok],call[ok],r1,q1)
        delta1=np.where(active,g1["delta"],0.0)
        drift=(delta1-g0["delta"])*oi[ok]*float(contract_multiplier)*S[ok]
        out[np.flatnonzero(ok)]=drift
        return pd.Series(out,index=x.index)
    except Exception:
        return pd.Series(np.zeros(len(enriched)),index=enriched.index,dtype=float)


def expected_move_band(spot: float, atm_iv_pct: float, dte_days: float,
                       times: Iterable, sigmas: float = 1.0,
                       dte_asof: Any | None = None) -> Dict[str, Any]:
    """±sigma envelope the option market is pricing for the remaining session.

    The band CONTRACTS through the day because remaining time shrinks; drawing a flat
    band from the open would overstate the range every minute after it.
    """
    out = {"ready": False, "upper": [], "lower": [], "times": []}
    try:
        iv = float(atm_iv_pct) / 100.0
        s = float(spot); dte = float(dte_days)
        if not all(map(math.isfinite, (iv, s, dte))) or iv <= 0 or s <= 0 or dte <= 0:
            return out
    except Exception:
        return out
    ts = [pd.Timestamp(t) for t in times]
    if not ts:
        return out
    t0, tN = min(ts), max(ts)
    total_min = max((tN - t0).total_seconds() / 60.0, 1e-9)
    # If dte_asof is provided, dte_days belongs to that timestamp (normally NOW/latest
    # snapshot). Earlier points therefore had *more* DTE. Without it we preserve the
    # original semantics: dte_days belongs to the first timestamp.
    ref = pd.Timestamp(dte_asof) if dte_asof is not None else t0
    up, dn = [], []
    for t in ts:
        if dte_asof is not None:
            remaining_days = max(dte + (ref - pd.Timestamp(t)).total_seconds()/86400.0, 0.0)
        else:
            elapsed = (pd.Timestamp(t) - t0).total_seconds() / 60.0
            remaining_days = max(dte - elapsed / 1440.0, 0.0)
        move = s * iv * math.sqrt(year_fraction(remaining_days)) * float(sigmas)
        up.append(s + move); dn.append(s - move)
    return {"ready": True, "times": ts, "upper": up, "lower": dn,
            "sigmas": float(sigmas), "atm_iv_pct": float(atm_iv_pct),
            "window_minutes": round(total_min, 2), "dte_asof": str(ref),
            "note": "Banda ±1σ condicional: spot/IV constantes; DTE anclado al timestamp indicado."}


def expected_move_band_from_chain(enriched: pd.DataFrame, sigmas: float = 1.0) -> Dict[str, Any]:
    """Historical ±1σ band using each TRACE snapshot's own spot, ATM IV and DTE.

    This avoids applying the latest IV/DTE retroactively to the morning. The band may
    widen even as time passes if implied volatility rises; that is market information,
    not an error.
    """
    out={"ready":False,"upper":[],"lower":[],"times":[],"spot":[],"atm_iv_pct":[],"dte":[]}
    if enriched is None or enriched.empty or "timestamp" not in enriched.columns:
        return out
    x=enriched.copy(); x["timestamp"]=pd.to_datetime(x["timestamp"],errors="coerce")
    x=x.dropna(subset=["timestamp"])
    rows=[]
    for ts,g in x.groupby("timestamp",sort=True):
        sp=numeric_column(g,"underlying_price",float("nan")).dropna()
        if sp.empty: continue
        s=float(sp.iloc[-1])
        k=numeric_column(g,"strike",float("nan"))
        iv=numeric_column(g,"iv",float("nan"))
        dte=numeric_column(g,"dte",float("nan"))
        valid=k.notna()&iv.notna()&dte.notna()&(iv>0)&(dte>0)
        if not valid.any(): continue
        dist=(k[valid]-s).abs(); md=float(dist.min())
        atm=valid & ((k-s).abs()<=md+1e-9)
        iv0=float(pd.to_numeric(iv[atm],errors="coerce").median())
        d0=float(pd.to_numeric(dte[atm],errors="coerce").median())
        if not all(math.isfinite(v) for v in (s,iv0,d0)) or iv0<=0 or d0<=0: continue
        move=s*iv0*math.sqrt(year_fraction(d0))*float(sigmas)
        rows.append((pd.Timestamp(ts),s,iv0,d0,s+move,s-move))
    if not rows:return out
    return {"ready":True,"times":[r[0] for r in rows],"spot":[r[1] for r in rows],
            "atm_iv_pct":[r[2]*100.0 for r in rows],"dte":[r[3] for r in rows],
            "upper":[r[4] for r in rows],"lower":[r[5] for r in rows],"sigmas":float(sigmas),
            "source":"EACH TRACE SNAPSHOT","note":"Cada punto usa su propio spot, IV ATM y DTE; no retrotrae la IV actual."}


def forward_profile(enriched: pd.DataFrame, minutes_ahead: float = 120.0,
                    spot: float | None = None, symbol: str = "DIA",
                    contract_multiplier: float = 100.0) -> pd.DataFrame:
    """Conditional Gamma/Delta profile after ``minutes_ahead`` of time decay.

    Spot, IV and OI are held fixed. Both Gamma and Delta are repriced directly at the
    reduced DTE; Charm is not used as a shortcut. Contracts expiring before the horizon
    contribute zero to the forward profile. This is a scenario/decay projection, not a
    prediction of where a wall will actually be after price and volatility move.
    """
    cols = ["strike", "gamma_now", "gamma_forward", "gamma_decay",
            "delta_now", "delta_forward", "delta_drift"]
    if enriched is None or enriched.empty:
        return pd.DataFrame(columns=cols)
    try:
        from .greeks_service import greeks_vector as _greeks_vector, model_inputs_vector
    except Exception:
        return pd.DataFrame(columns=cols)

    d = enriched.copy()
    ts = pd.to_datetime(d.get("timestamp"), errors="coerce")
    if ts.notna().any():
        d = d[ts == ts.max()]
    S = float(spot) if spot is not None else float(pd.to_numeric(d["underlying_price"], errors="coerce").dropna().iloc[-1])
    K = pd.to_numeric(d["strike"], errors="coerce").to_numpy(float)
    iv = pd.to_numeric(d["iv"], errors="coerce").to_numpy(float)
    dte = pd.to_numeric(d["dte"], errors="coerce").to_numpy(float)
    oi = numeric_column(d,"open_interest",0).to_numpy(float)
    is_call = d["option_type"].astype(str).str.lower().str.startswith("c").to_numpy()
    sgn = np.where(is_call, 1.0, -1.0)
    ok = np.isfinite(K) & np.isfinite(iv) & np.isfinite(dte) & (iv > 0)
    if not ok.any():
        return pd.DataFrame(columns=cols)
    K, iv, dte, oi, is_call, sgn = (v[ok] for v in (K, iv, dte, oi, is_call, sgn))

    ahead_days = max(float(minutes_ahead), 0.0) / 1440.0
    active = dte > ahead_days + 1e-12
    dte_fwd = np.maximum(dte - ahead_days, 0.0)
    r0, q0 = model_inputs_vector(symbol, dte)
    r1, q1 = model_inputs_vector(symbol, dte_fwd)
    g_now = _greeks_vector(symbol, S, K, year_fraction_array(dte), iv, is_call, r0, q0)
    g_fwd = _greeks_vector(symbol, S, K, year_fraction_array(dte_fwd), iv, is_call, r1, q1)

    gamma_scale = oi * contract_multiplier * (S ** 2) * 0.01
    gex_now = sgn * g_now["gamma"] * gamma_scale
    gex_fwd = np.where(active, sgn * g_fwd["gamma"] * gamma_scale, 0.0)
    # Match engine.option_delta_exposure_info: Delta * OI * multiplier * underlying.
    delta_scale = oi * contract_multiplier * S
    dex_now = g_now["delta"] * delta_scale
    dex_fwd = np.where(active, g_fwd["delta"] * delta_scale, 0.0)

    out = pd.DataFrame({"strike": K, "gamma_now": gex_now, "gamma_forward": gex_fwd,
                        "delta_now": dex_now, "delta_forward": dex_fwd})
    out.attrs["forward_assumptions"] = "SPOT_IV_OI_CONSTANT"
    out.attrs["minutes_ahead"] = float(minutes_ahead)
    out.attrs["expired_contract_rows"] = int((~active).sum())
    out = out.groupby("strike", as_index=False).sum().sort_values("strike")
    out["gamma_decay"] = out["gamma_forward"] - out["gamma_now"]
    out["delta_drift"] = out["delta_forward"] - out["delta_now"]
    return out[cols]

# ------------------------------------------------------- aggression / delta bars
def aggression_bars(ticks: pd.DataFrame, threshold: float, absorption_multiple: float = 3.0,
                    max_seconds: float | None = 300.0) -> pd.DataFrame:
    """Order-flow bars that close on imbalance OR on absorption.

    THE PROBLEM WITH A PURE IMBALANCE TRIGGER. Closing a bar only when |delta| reaches
    the threshold means a bar can only close when one side WINS. A balanced, high-volume
    fight at a level - absorption, the single most informative pattern in order flow -
    never trips the trigger, so it gets swallowed into one oversized bar. Measured on a
    synthetic tape with identical tick count: a 50/50 tape produces 259 bars averaging
    1,117 shares each, while an 85/15 tape produces 702 bars averaging 412. The screen
    shows the LEAST detail exactly where the most is happening.

    THE FIX. Two triggers:
      IMBALANCE  - |delta| >= threshold. One side won. Coloured by side.
      CHURN      - total volume >= absorption_multiple * threshold with |delta| still
                   below threshold. Heavy two-sided trade with no winner.

    NOTE ON TERMINOLOGY, because I got this wrong in the first draft. CHURN is NOT
    absorption. Churn is balanced aggression: buyers and sellers hitting each other, no
    conviction either way. TRUE ABSORPTION is the opposite shape - heavy ONE-SIDED
    aggression that fails to move price, because passive size is sitting there eating
    it. Those two look nothing alike on the tape and mean opposite things: churn is
    indecision, absorption is a defended level and one of the strongest reversal tells
    there is. Absorption is therefore flagged separately via `absorbed`, computed from
    price displacement per unit of signed volume, not from the delta balance.
      TIMEOUT    - `max_seconds` elapsed with neither trigger hit. Without this a bar
                   has NO time bound at all: measured on a synthetic pinned tape (10
                   ticks/min, small lots, ~50/50 flow) bars averaged 15.4 minutes and
                   the worst took 17.3. A chart that prints nothing for a quarter of an
                   hour is not a chart. A TIMEOUT bar is also information in its own
                   right: it says nobody is participating at this price.

    CHURN/TIMEOUT are reported in `bar_type`; true absorption is carried by the separate
    `absorbed` flag so direction and defended-level information are not collapsed together.
    """
    cols = ["start", "end", "mid_time", "open", "high", "low", "close", "delta",
            "buy_volume", "sell_volume", "volume", "trades", "notional",
            "duration_s", "control", "bar_type", "complete", "buy_pct", "absorption_ratio", "efficiency", "absorbed"]
    if ticks is None or ticks.empty:
        return pd.DataFrame(columns=cols)
    x = ticks.copy()
    x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce")
    x["price"] = numeric_column(x,"price",float("nan"))
    x["size"] = numeric_column(x,"size",0.0)
    x = x.dropna(subset=["timestamp", "price"]).sort_values("timestamp")
    if x.empty:
        return pd.DataFrame(columns=cols)

    sign = numeric_column(x,"aggressor_sign",0)
    if not sign.abs().sum():
        sign = np.sign(x["price"].diff()).replace(0, np.nan).ffill().fillna(0)

    # Iterate over numpy arrays, not DataFrame rows. The reset-on-threshold logic is
    # genuinely path dependent so it cannot be a pure cumsum, but iterrows() was costing
    # ~2.3s on 60k ticks; this is the same algorithm on raw arrays.
    ts = x["timestamp"].to_numpy()
    px = x["price"].to_numpy(float)
    sz = x["size"].to_numpy(float)
    sg = sign.to_numpy(float)
    thr = max(float(threshold), 1.0)
    abs_thr = thr * max(float(absorption_multiple), 1.0)

    rows = []
    cur = None
    for i in range(len(px)):
        p, s, g = px[i], sz[i], sg[i]
        if cur is None:
            cur = {"start": ts[i], "open": p, "high": p, "low": p, "delta": 0.0,
                   "buy_volume": 0.0, "sell_volume": 0.0, "trades": 0, "notional": 0.0}
        cur["end"] = ts[i]
        cur["high"] = max(cur["high"], p); cur["low"] = min(cur["low"], p)
        cur["close"] = p; cur["delta"] += g * s; cur["trades"] += 1
        cur["notional"] += p * s
        if g > 0: cur["buy_volume"] += s
        elif g < 0: cur["sell_volume"] += s
        vol = cur["buy_volume"] + cur["sell_volume"]
        elapsed = (pd.Timestamp(ts[i]) - pd.Timestamp(cur["start"])).total_seconds()
        if abs(cur["delta"]) >= thr:
            cur["control"] = "BUY" if cur["delta"] > 0 else "SELL"
            cur["bar_type"] = "IMBALANCE"; cur["complete"] = True
            rows.append(cur); cur = None
        elif max_seconds and elapsed >= float(max_seconds):
            cur["control"] = "BUY" if cur["delta"] > 0 else "SELL" if cur["delta"] < 0 else "BALANCED"
            cur["bar_type"] = "TIMEOUT"; cur["complete"] = True
            rows.append(cur); cur = None
        elif vol >= abs_thr:
            # Heavy two-sided trade with no winner: a level is being absorbed.
            cur["control"] = "BALANCED"
            cur["bar_type"] = "CHURN"; cur["complete"] = True
            rows.append(cur); cur = None
    if cur is not None:
        cur["control"] = "BUY" if cur["delta"] > 0 else "SELL" if cur["delta"] < 0 else "BALANCED"
        cur["bar_type"] = "FORMING"; cur["complete"] = False
        rows.append(cur)

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=cols)
    out["volume"] = out["buy_volume"] + out["sell_volume"]
    tot = out["volume"].replace(0, np.nan)
    out["buy_pct"] = (out["buy_volume"] / tot * 100).fillna(50.0)
    # How much volume it took to move the delta. High ratio = the level absorbed size.
    out["absorption_ratio"] = (out["volume"] / out["delta"].abs().clip(lower=1.0)).round(2)
    # TRUE ABSORPTION: strong one-sided aggression that barely moved price. Efficiency is
    # price displacement per 1,000 units of signed volume; when a bar pushes a full
    # threshold of delta and the close barely leaves the open, someone passive ate it.
    disp = (out["close"] - out["open"]).abs()
    out["efficiency"] = (disp / out["delta"].abs().clip(lower=1.0) * 1000.0).round(4)
    # A pinned/unchanged tape legitimately produces efficiency == 0 for every bar.
    # Pandas delegates an all-NaN median to np.nanmedian, which emits
    # ``RuntimeWarning: Mean of empty slice``.  Zero here means "no displacement",
    # not missing data, so keep the semantic fallback at 0 without calling median on
    # an empty finite sample.
    _eff_sample = (pd.to_numeric(out["efficiency"], errors="coerce")
                   .replace([np.inf, -np.inf, 0], np.nan)
                   .dropna())
    typical = float(_eff_sample.median()) if not _eff_sample.empty else 0.0
    out["absorbed"] = (out["bar_type"].astype(str).eq("IMBALANCE") &
                       (out["delta"].abs() >= float(threshold) * 0.9) &
                       (out["efficiency"] <= max(typical * 0.35, 1e-9)))
    out["duration_s"] = (pd.to_datetime(out["end"]) - pd.to_datetime(out["start"])).dt.total_seconds().clip(lower=0)
    out["mid_time"] = pd.to_datetime(out["start"]) + (pd.to_datetime(out["end"]) - pd.to_datetime(out["start"])) / 2
    return out[cols]


def aggression_strength(bars: pd.DataFrame, threshold: float, lookback: int = 20,
                        dead_band: float = 8.0) -> Dict[str, Any]:
    """Continuous aggression score, weighted by the volume behind each bar.

    The v1.14.8 score was ``50 + 35*mean(sign(delta))`` over the last 5 bars. Since an
    imbalance bar closes AT the threshold by construction, |delta|/thr is ~1 for all of
    them, so the expression collapsed to a 5-bar majority vote with exactly six
    reachable values - 15, 29, 43, 57, 71, 85 - displayed to one decimal as if it were
    continuous. This version weights by volume, discounts absorption bars (no winner is
    not weak evidence for either side, it is evidence of a contested level), and returns
    the absorption share separately instead of hiding it inside the direction score.
    """
    out = {"score": 50.0, "control": "WAIT", "bars": 0, "absorption_pct": 0.0, "churn_pct": 0.0,
           "threshold": int(threshold), "delta": 0.0}
    if bars is None or bars.empty:
        return out
    b = bars.tail(int(max(lookback, 1))).copy()
    vol = pd.to_numeric(b["volume"], errors="coerce").fillna(0.0).clip(lower=1.0)
    delta = pd.to_numeric(b["delta"], errors="coerce").fillna(0.0)
    imbalance = b["bar_type"].astype(str).ne("CHURN")
    # Directional pull per bar in [-1, 1]; absorption contributes 0, not a fake vote.
    pull = np.clip(delta / max(float(threshold), 1.0), -1.5, 1.5) * imbalance.to_numpy(float)
    w = vol.to_numpy(float)
    net = float(np.average(pull, weights=w)) if w.sum() > 0 else 0.0
    out["score"] = round(float(np.clip(50.0 + 33.0 * net, 0.0, 100.0)), 1)
    out["bars"] = int(len(bars))
    out["churn_pct"] = round(100.0 * float(b["bar_type"].astype(str).eq("CHURN").mean()), 1)
    absorbed_col = b["absorbed"] if "absorbed" in b.columns else pd.Series(False, index=b.index)
    out["absorption_pct"] = round(100.0 * float(absorbed_col.fillna(False).astype(bool).mean()), 1)
    out["delta"] = float(delta.iloc[-1])
    # Dead band calibrated on a genuinely 50/50 tape: with lookback 5 the score wanders
    # more than 8 points from neutral 48% of the time by pure chance, with lookback 20
    # only 12%. Short windows plus a narrow band would print a direction on noise, so the
    # default window is 20 and anything inside +/-8 is reported as MIXED, not as a side.
    band = float(dead_band)
    out["control"] = ("BUY" if out["score"] > 50 + band
                      else "SELL" if out["score"] < 50 - band else "MIXED")
    # Absorption is reported ALONGSIDE the direction, not instead of it. A 62/38 tape is
    # genuinely buy-controlled even while a level is being defended; collapsing that to
    # "BALANCED" would throw away the direction the flow actually has.
    out["contested"] = bool((out["churn_pct"] or 0) >= 45.0 or (out["absorption_pct"] or 0) >= 25.0)
    out["lookback"] = int(lookback)
    out["note"] = ("CHURN alto = indecisión; ABSORCIÓN alta = nivel defendido contra el lado agresor. "
                   "MIXED significa que la evidencia no supera el ruido de una cinta equilibrada.")
    return out


# ------------------------------------------------- entry confirmation on the tape
def tape_confirmation(ticks: pd.DataFrame, zone_low: float, zone_high: float,
                      direction: str, threshold: float,
                      confirm_fraction: float = 0.50,
                      absorption_multiple: float = 3.0,
                      budget_seconds: float = 180.0,
                      now: pd.Timestamp | None = None) -> Dict[str, Any]:
    """Live entry confirmation from the FORMING bar, never from a closed one.

    THE PROBLEM THIS SOLVES. Aggression bars close on order-flow imbalance, so they have
    no time bound. On a pinned tape they can take a quarter of an hour. If you treat the
    bar CLOSE as your entry trigger, the entry is long gone by the time it prints.

    THE REFRAME. The delta is accumulating tick by tick; there is no reason to wait for
    the bar to close. What matters is whether aggression is showing up IN YOUR ZONE,
    RIGHT NOW, in your direction. So this function ignores bar boundaries entirely and
    measures the signed flow since price entered the zone, against an explicit time
    budget.

    States:
      WAITING   - price is not in the zone. Nothing to judge yet.
      ARMED     - price is in the zone, flow is still building. `progress_pct` shows how
                  far the confirmation is.
      CONFIRMED - signed flow in your direction reached confirm_fraction x threshold.
                  This is the entry signal, and it fires at a FRACTION of a full bar.
      REJECTED  - signed flow reached that size AGAINST you. Stand down.
      ABSORBED  - your side IS aggressing hard, and price is not moving. Passive size is
                  eating your flow. This is a warning, not a green light: the level is
                  defended against you. Excellent for a fade, terrible for a breakout.
      CHURN     - heavy two-sided trade with no winner. Indecision, not defence.
      EXPIRED   - the time budget ran out with no participation. Nobody cared about this
                  level. That is a decision too, and it arrives on schedule instead of
                  never.

    `confirm_fraction` is the knob that trades speed against certainty: 0.5 confirms in
    roughly half the flow a full bar needs, 1.0 is equivalent to waiting for the close.
    """
    out = {"state": "WAITING", "direction": str(direction).upper(),
           "zone": [float(zone_low), float(zone_high)], "in_zone": False,
           "zone_entry_time": None, "asof_timestamp": None,
           "signed_volume": 0.0, "volume": 0.0, "buy_pct": None,
           "progress_pct": 0.0, "seconds_in_zone": 0.0,
           "seconds_remaining": float(budget_seconds), "trades": 0,
           "required_signed_volume": float(threshold) * float(confirm_fraction),
           "absorption_ratio": None,
           "note": "Confirmación sobre la vela EN FORMACIÓN; no espera al cierre."}
    if ticks is None or ticks.empty:
        return out
    x = ticks.copy()
    x["timestamp"] = pd.to_datetime(x.get("timestamp"), errors="coerce")
    x["price"] = numeric_column(x,"price",float("nan"))
    x["size"] = numeric_column(x,"size",0.0)
    x = x.dropna(subset=["timestamp", "price"]).sort_values("timestamp")
    # "As of now": anything after the evaluation instant is future information and must
    # be dropped, otherwise a replay evaluates a moment using ticks that had not printed
    # yet and seconds_in_zone can even come out negative.
    if now is not None:
        x = x[x["timestamp"] <= pd.Timestamp(now)]
    if x.empty:
        return out
    sign = numeric_column(x,"aggressor_sign",0)
    if not sign.abs().sum():
        sign = np.sign(x["price"].diff()).replace(0, np.nan).ffill().fillna(0)
    x["_sign"] = sign

    lo, hi = float(min(zone_low, zone_high)), float(max(zone_low, zone_high))
    inside = (x["price"] >= lo) & (x["price"] <= hi)
    if not bool(inside.iloc[-1]):
        return out                     # price is elsewhere; nothing to confirm

    # Only the CURRENT uninterrupted visit to the zone counts. Flow from a visit two
    # hours ago says nothing about whether size is here now.
    left = (~inside).to_numpy()
    idx = np.flatnonzero(left)
    start = int(idx[-1]) + 1 if idx.size else 0
    seg = x.iloc[start:]
    if seg.empty:
        return out

    t_now = pd.Timestamp(now) if now is not None else pd.Timestamp(seg["timestamp"].iloc[-1])
    secs = max(0.0, float((t_now - pd.Timestamp(seg["timestamp"].iloc[0])).total_seconds()))
    signed = float((seg["_sign"] * seg["size"]).sum())
    vol = float(seg["size"].sum())
    buy_vol = float(seg.loc[seg["_sign"] > 0, "size"].sum())
    need = float(threshold) * float(confirm_fraction)
    want = 1.0 if str(direction).upper() in {"BUY", "LONG", "BOUNCE"} else -1.0
    aligned = signed * want

    out.update({"in_zone": True,
                "zone_entry_time": pd.Timestamp(seg["timestamp"].iloc[0]).isoformat(),
                "asof_timestamp": pd.Timestamp(t_now).isoformat(),
                "signed_volume": round(signed, 1), "volume": round(vol, 1),
                "buy_pct": round(100.0 * buy_vol / vol, 1) if vol > 0 else None,
                "trades": int(len(seg)), "seconds_in_zone": round(secs, 1),
                "seconds_remaining": round(max(float(budget_seconds) - secs, 0.0), 1),
                "progress_pct": round(float(np.clip(aligned / max(need, 1e-9), -1.5, 1.5)) * 100.0, 1),
                "absorption_ratio": round(vol / max(abs(signed), 1.0), 2)})

    # ORDER MATTERS. On a balanced tape the running signed volume random-walks and will
    # cross +/-need by chance, so checking the partial imbalance first labelled genuine
    # absorption as CONFIRMED or REJECTED depending on which way it happened to wander.
    # Heavy two-sided trade is therefore judged BEFORE the partial threshold, and only a
    # decisive imbalance - a full threshold, not a fraction of one - overrides it.
    heavy = vol >= float(threshold) * float(absorption_multiple)
    decisive = abs(aligned) >= float(threshold)
    imbalance_ratio = abs(signed) / max(vol, 1.0)
    # Displacement of price in the trade's favour since entering the zone, per unit of
    # signed volume. Aggression that is not moving price is being absorbed.
    px0 = float(seg["price"].iloc[0]); px1 = float(seg["price"].iloc[-1])
    favourable_move = (px1 - px0) * want
    efficiency = favourable_move / max(abs(signed), 1.0) * 1000.0
    out["price_move_favourable"] = round(favourable_move, 4)
    out["efficiency_per_1k"] = round(efficiency, 4)
    out["imbalance_ratio"] = round(imbalance_ratio, 3)
    zone_width = max(abs(hi - lo), 1e-9)
    min_response = max(zone_width * 0.08, 0.01)
    absorbed = bool(aligned >= need and imbalance_ratio >= 0.20 and favourable_move <= min_response)
    out["minimum_price_response"] = round(min_response, 4)

    if absorbed:
        # Your own side is pushing and price will not go. Do not call that a confirmation.
        out["state"] = "ABSORBED"
    elif decisive and imbalance_ratio >= 0.20 and aligned > 0:
        out["state"] = "CONFIRMED"
    elif decisive and imbalance_ratio >= 0.20 and aligned < 0:
        out["state"] = "REJECTED"
    elif heavy and imbalance_ratio < 0.20:
        out["state"] = "CHURN"
    elif aligned >= need:
        out["state"] = "CONFIRMED"
    elif aligned <= -need:
        out["state"] = "REJECTED"
    elif secs >= float(budget_seconds):
        out["state"] = "EXPIRED"
    else:
        out["state"] = "ARMED"
    return out


def bar_cadence(bars: pd.DataFrame, window_minutes: float = 15.0) -> Dict[str, Any]:
    """Participation regime read from how fast bars are printing.

    The formation RATE is itself a signal that the bars alone do not show. Slow bars mean
    nobody is trading size, which is exactly what a strong positive-gamma pin looks like;
    a sudden acceleration is the tape telling you the pin is breaking before price does.
    """
    out = {"bars_per_minute": None, "regime": "SIN DATOS", "timeout_pct": None,
           "median_seconds": None, "window_minutes": float(window_minutes)}
    if bars is None or bars.empty or "end" not in bars.columns:
        return out
    b = bars.copy()
    b["end"] = pd.to_datetime(b["end"], errors="coerce")
    b = b.dropna(subset=["end"])
    if b.empty:
        return out
    cutoff = b["end"].max() - pd.Timedelta(minutes=float(window_minutes))
    w = b[b["end"] >= cutoff]
    if w.empty:
        return out
    span = max((w["end"].max() - w["end"].min()).total_seconds() / 60.0, 1e-9)
    rate = float(len(w) / span)
    out["bars_per_minute"] = round(rate, 2)
    _dur = pd.to_numeric(w.get("duration_s", pd.Series(dtype=float)), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    out["median_seconds"] = round(float(_dur.median()), 1) if not _dur.empty else None
    out["timeout_pct"] = round(100.0 * float(w["bar_type"].astype(str).eq("TIMEOUT").mean()), 1)
    out["regime"] = ("SIN PARTICIPACIÓN" if rate < 0.3 or (out["timeout_pct"] or 0) >= 50
                     else "TRANQUILO" if rate < 1.5
                     else "ACTIVO" if rate < 6.0 else "AGRESIÓN INTENSA")
    return out


# ---------------------------------------------------- time-clocked aggression context

def time_aggression_bars(ticks: pd.DataFrame, interval_minutes: float = 1.0,
                         baseline_bars: int = 30, absorption_efficiency: float = 0.35,
                         min_ready_bars: int = 0) -> pd.DataFrame:
    """Clocked aggression candles with a causal, activity-consistent signed-flow z score.

    The raw signed volume of a one-minute bar is not comparable across the session because
    activity is strongly U-shaped.  Under a neutral tape with heterogeneous trade sizes,
    Var(sum(sign_i*size_i)) is proportional to sum(size_i**2), not total volume itself.
    TRACE therefore standardises signed volume by sqrt(sum(size_i**2)).  This uses only
    trades inside the bar, handles unequal trade sizes, and never looks into the future.

    `z_ready` remains False until the bar is complete. `min_ready_bars` can optionally
    require extra prior bars, but defaults to zero because the variance denominator is
    self-contained and needs no future/rolling activity calibration.  Multi-timeframe alignment must respect that state;
    missing history is COLLECTING, never silently BALANCED.

    Absorption is also strictly backward-looking: a bar can be labelled absorbed only
    when flow is genuinely one-sided and price-response efficiency is abnormally low
    versus PRIOR completed bars.  CHURN/absorption remain context, not an entry trigger.
    """
    cols = ["bucket", "open", "high", "low", "close", "delta", "delta_z_raw", "delta_z",
            "z_ready", "complete", "buy_volume", "sell_volume", "volume", "trades", "notional",
            "control", "buy_pct", "efficiency", "absorbed", "interval_minutes", "variance_denom"]
    if ticks is None or ticks.empty:
        return pd.DataFrame(columns=cols)
    x=ticks.copy()
    x["timestamp"]=pd.to_datetime(x.get("timestamp"),errors="coerce")
    x["price"]=numeric_column(x,"price",float("nan"))
    x["size"]=numeric_column(x,"size",0.0).clip(lower=0.0)
    x=x.dropna(subset=["timestamp","price"]).sort_values("timestamp")
    if x.empty:return pd.DataFrame(columns=cols)
    sign=numeric_column(x,"aggressor_sign",0.0)
    if not sign.abs().sum():
        sign=np.sign(x["price"].diff()).replace(0,np.nan).ffill().fillna(0)
    x["_sign"]=sign
    x["_sv"]=x["_sign"]*x["size"]
    x["_size_sq"]=x["size"]**2
    x["_notional"]=x["price"]*x["size"]
    x["_buy"]=np.where(x["_sign"]>0,x["size"],0.0)
    x["_sell"]=np.where(x["_sign"]<0,x["size"],0.0)
    seconds=max(int(round(float(interval_minutes)*60.0)),1)
    rule=f"{seconds}s"
    g=x.set_index("timestamp").resample(rule)
    out=pd.DataFrame({
        "open":g["price"].first(),"high":g["price"].max(),"low":g["price"].min(),"close":g["price"].last(),
        "delta":g["_sv"].sum(),"_size_sq":g["_size_sq"].sum(),"buy_volume":g["_buy"].sum(),
        "sell_volume":g["_sell"].sum(),"trades":g["price"].count(),"notional":g["_notional"].sum()})
    out=out.dropna(subset=["open"]).reset_index().rename(columns={"timestamp":"bucket"})
    if out.empty:return pd.DataFrame(columns=cols)
    out["volume"]=out["buy_volume"]+out["sell_volume"]
    tot=out["volume"].replace(0,np.nan)
    out["buy_pct"]=(out["buy_volume"]/tot*100.0).fillna(50.0).round(1)
    denom=np.sqrt(pd.to_numeric(out["_size_sq"],errors="coerce").clip(lower=0.0))
    out["variance_denom"]=denom
    raw=(pd.to_numeric(out["delta"],errors="coerce")/denom.replace(0,np.nan))
    out["delta_z_raw"]=raw.round(4)

    # A bucket is comparable only after it has fully elapsed. The last forming bar is
    # deliberately COLLECTING and cannot create an alignment verdict.
    asof=pd.Timestamp(x["timestamp"].max())
    out["complete"]=(pd.to_datetime(out["bucket"])+pd.to_timedelta(seconds,unit="s") <= asof)
    prior_complete=out["complete"].astype(int).cumsum().shift(1).fillna(0)
    out["z_ready"]=out["complete"] & (prior_complete>=int(max(min_ready_bars,0))) & raw.notna()
    out["delta_z"]=raw.where(out["z_ready"]).round(3)

    disp=(pd.to_numeric(out["close"],errors="coerce")-pd.to_numeric(out["open"],errors="coerce")).abs()
    out["efficiency"]=(disp/pd.to_numeric(out["delta"],errors="coerce").abs().clip(lower=1.0)*1000.0).round(4)
    # Strictly causal baseline: no whole-frame median and no current-bar self-reference.
    eff=pd.to_numeric(out["efficiency"],errors="coerce").replace(0,np.nan)
    prior_eff=eff.rolling(int(max(baseline_bars,3)),min_periods=3).median().shift(1)
    one_sided=(out["buy_pct"]>=65.0)|(out["buy_pct"]<=35.0)
    materially_signed=raw.abs()>=1.0
    out["absorbed"]=(out["complete"] & one_sided & materially_signed & prior_eff.notna() &
                     (eff<=prior_eff*float(absorption_efficiency))).fillna(False)
    out["control"]=np.where(pd.to_numeric(out["delta"],errors="coerce")>0,"BUY",
                     np.where(pd.to_numeric(out["delta"],errors="coerce")<0,"SELL","BALANCED"))
    out["interval_minutes"]=float(interval_minutes)
    return out[cols]


def multi_timeframe_aggression(ticks: pd.DataFrame, intervals: Iterable[float] = (1,3,5),
                               lookback_bars: int = 3, dead_band: float | None = None) -> Dict[str, Any]:
    """1m/3m/5m order-flow CONTEXT as one de-correlated family, never a Scanner vote.

    `dead_band=1.60` is only the current SHADOW starting value from synthetic testing.
    It is configurable with ITM_TIMEFLOW_DEAD_BAND and is not promoted as a market law.
    Alignment requires every requested frame to have enough completed `z_ready` bars.
    """
    if dead_band is None:
        try: dead_band=float(os.getenv("ITM_TIMEFLOW_DEAD_BAND","1.60"))
        except Exception: dead_band=1.60
    dead=float(max(abs(float(dead_band)),0.05))
    out={"frames":{},"ready":False,"status":"COLLECTING","aligned":False,"direction":"MIXED",
         "agreement":0.0,"fast_vs_slow":"—","absorbed_frames":[],"dead_band":dead,
         "dead_band_status":"SHADOW · CALIBRABLE","family":"MULTITIMEFRAME_TAPE_CONTEXT",
         "role":"CONTEXT_ONLY","affects_scanner_direction":False,"affects_scanner_score":False,
         "affects_timing_trigger":False}
    if ticks is None or ticks.empty:return out
    ready_z={}; requested=[]
    for iv in intervals:
        iv=float(iv); key=f"{int(iv)}m" if iv.is_integer() else f"{iv:g}m"; requested.append(key)
        bars=time_aggression_bars(ticks,iv)
        valid=bars[bars.get("z_ready",False).fillna(False).astype(bool)].copy() if not bars.empty else pd.DataFrame()
        if len(valid)<int(max(lookback_bars,1)):
            out["frames"][key]={"bars":int(len(bars)),"ready_bars":int(len(valid)),"ready":False,
                                "delta_z":None,"control":"COLLECTING","absorbed":False}
            continue
        tail=valid.tail(int(max(lookback_bars,1)))
        zv=pd.to_numeric(tail["delta_z"],errors="coerce").dropna()
        if len(zv)<int(max(lookback_bars,1)):
            out["frames"][key]={"bars":int(len(bars)),"ready_bars":int(len(valid)),"ready":False,
                                "delta_z":None,"control":"COLLECTING","absorbed":False}
            continue
        z=float(zv.mean()); ready_z[key]=z
        ctrl="BUY" if z>dead else "SELL" if z<-dead else "BALANCED"
        absorbed=bool(tail.get("absorbed",pd.Series(False,index=tail.index)).fillna(False).any())
        if absorbed:out["absorbed_frames"].append(key)
        out["frames"][key]={"bars":int(len(bars)),"ready_bars":int(len(valid)),"ready":True,
                            "delta_z":round(z,3),"control":ctrl,"last_delta":float(tail["delta"].iloc[-1]),
                            "buy_pct":float(tail["buy_pct"].iloc[-1]),"absorbed":absorbed}
    if len(ready_z)!=len(requested):
        out["note"]="Falta al menos un marco 1m/3m/5m listo; no se convierte ausencia de muestra en BALANCED."
        return out
    out["ready"]=True
    controls=[out["frames"][k]["control"] for k in requested]
    directional=[c for c in controls if c in {"BUY","SELL"}]
    if len(directional)==len(requested) and len(set(directional))==1:
        out["aligned"]=True;out["direction"]=directional[0];out["status"]=f"ALIGNED {directional[0]}";out["agreement"]=100.0
    else:
        out["status"]="MIXED"
        if directional:
            majority=max((directional.count("BUY"),"BUY"),(directional.count("SELL"),"SELL"))[1]
            out["agreement"]=round(100.0*directional.count(majority)/len(requested),1)
    keys=sorted(requested,key=lambda k:float(k[:-1]))
    if len(keys)>=2:
        fast=ready_z[keys[0]];slow=ready_z[keys[-1]]
        if abs(fast)>dead and abs(slow)>dead and np.sign(fast)!=np.sign(slow):
            out["fast_vs_slow"]="DIVERGENCIA · rápido gira contra lento"
        elif abs(fast)>abs(slow)*1.5 and abs(fast)>dead:
            out["fast_vs_slow"]="ACELERANDO · rápido domina"
        elif abs(slow)>dead and abs(fast)<=dead:
            out["fast_vs_slow"]="ENFRIANDO · rápido pierde dirección"
        else:out["fast_vs_slow"]="COHERENTE"
    out["note"]=("Contexto multimarco 1m/3m/5m normalizado por sqrt(sum(size²)). Es una sola familia "
                 "de contexto, SHADOW y calibrable; no suma tres confirmaciones ni cambia Scanner/Tape trigger.")
    return out


# ---------------------------------------------------------- v1.15.8 3D session landscape
def session_landscape(history: pd.DataFrame, lens: str = "gamma",
                      window_strikes: int = 21, scale_mode: str = "session",
                      anchor: Dict[str, Any] | None = None,
                      spot: float | None = None, symbol: str = "DIA") -> Dict[str, Any]:
    """TRACE 3D session landscape with honest units and exact lens semantics.

    Height is ALWAYS the selected signed exposure in millions of dollars.  Session/anchor
    scaling is used only as ``surfacecolor`` so it can improve visual comparability without
    replacing economic magnitude with an artificial 0-100 height.

    The selected lens never silently falls back to Gamma. If Delta/Charm inputs are missing,
    the view returns ready=False. The overlaid flip line is ALWAYS computed from Gamma,
    regardless of the selected surface lens.
    """
    mode=str(scale_mode or "session").strip().lower()
    if mode not in {"column","session","anchor"}: mode="session"
    ln=str(lens or "gamma").strip().lower()
    if ln.startswith("delta"): ln="delta"
    elif ln.startswith("charm"): ln="charm"
    else: ln="gamma"
    out={"ready":False,"strikes":[],"times":[],"z":None,"z_scaled":None,
         "gamma_flip_line":[],"price_line":[],"scale_mode":mode,"denominator":None,
         "lens":ln,"reason":None,"height_unit":"M$ SIGNED EXPOSURE"}
    if history is None or history.empty:
        out["reason"]="sin histórico"; return out
    h=history.copy()
    h["timestamp"]=pd.to_datetime(h.get("timestamp"),errors="coerce")
    h["strike"]=numeric_column(h,"strike",float("nan"))
    h=h.dropna(subset=["timestamp","strike"])
    if h.empty:
        out["reason"]="sin timestamp/strike válidos"; return out
    h_all=h.copy()  # gamma flip/price remain independent of the selected lens.

    # Exact lens only. Never substitute a different exposure behind the label.
    if ln=="gamma":
        col=next((c for c in ("signed_gex_proxy","signed_gex") if c in h.columns),None)
        if col is None:
            out["reason"]="GAMMA UNAVAILABLE · falta signed_gex"; return out
        h["_landscape_exposure"]=pd.to_numeric(h[col],errors="coerce")
    elif ln=="delta":
        col=next((c for c in ("option_delta_exposure_info","delta_exposure","signed_dex") if c in h.columns),None)
        if col is None:
            out["reason"]="DELTA UNAVAILABLE · falta exposición Delta"; return out
        h["_landscape_exposure"]=pd.to_numeric(h[col],errors="coerce")
    else:
        required={"underlying_price","iv","dte","open_interest","option_type"}
        if not required.issubset(set(h.columns)):
            out["reason"]="CHARM UNAVAILABLE · faltan IV/DTE/OI/tipo"; return out
        cp=charm_pressure_proxy(h,minutes=10.0,symbol=str(symbol))
        if len(cp)!=len(h):
            out["reason"]="CHARM UNAVAILABLE · no se pudo repreciar Delta"; return out
        h["_landscape_exposure"]=pd.to_numeric(cp,errors="coerce")
    h=h[pd.to_numeric(h["_landscape_exposure"],errors="coerce").notna()].copy()
    if h.empty:
        out["reason"]=f"{ln.upper()} UNAVAILABLE · exposición no finita"; return out

    if spot is None:
        spv=numeric_column(h,"underlying_price",float("nan")).dropna()
        if spv.empty:
            out["reason"]="sin spot para centrar strikes"; return out
        sp=float(spv.iloc[-1])
    else:
        try: sp=float(spot)
        except Exception:
            out["reason"]="spot inválido"; return out
    ks=np.sort(h["strike"].unique())
    if not len(ks): out["reason"]="sin strikes"; return out
    centre=int(np.argmin(np.abs(ks-sp)))
    half=max(int(max(window_strikes,5))//2,2)
    keep=ks[max(0,centre-half):centre+half+1]
    h=h[h["strike"].isin(keep)]
    h_all=h_all[h_all["strike"].isin(keep)]

    pivot=(h.groupby(["strike","timestamp"])["_landscape_exposure"].sum()
             .unstack(fill_value=0.0).sort_index())
    if pivot.empty or pivot.shape[1]<2:
        out["reason"]="se necesitan al menos 2 snapshots"; return out
    hm=heat_matrix(pivot,scale_mode=mode,anchor=anchor)
    raw=hm["raw"]  # millions; this is the physical height

    # Gamma flip ALWAYS comes from Gamma, not from the selected lens.
    gcol=next((c for c in ("signed_gex_proxy","signed_gex") if c in h_all.columns),None)
    flips=[]
    if gcol is not None:
        gp=(h_all.assign(_gamma=pd.to_numeric(h_all[gcol],errors="coerce").fillna(0.0))
              .groupby(["strike","timestamp"])["_gamma"].sum()
              .unstack(fill_value=0.0).reindex(index=pivot.index,columns=pivot.columns,fill_value=0.0))
        strikes=pivot.index.to_numpy(float)
        gv=gp.to_numpy(float)/1e6
        for j in range(gv.shape[1]):
            cum=np.cumsum(gv[:,j]); idx=np.flatnonzero(np.sign(cum[:-1])!=np.sign(cum[1:]))
            if idx.size:
                i=int(idx[0]); a,b=cum[i],cum[i+1]
                flips.append(float(strikes[i]+(strikes[i+1]-strikes[i])*(-a)/(b-a)) if b!=a else float(strikes[i]))
            else: flips.append(float("nan"))
    else:
        flips=[float("nan")]*pivot.shape[1]

    price=[]
    if "underlying_price" in h_all.columns:
        ps=(h_all.assign(_spot=pd.to_numeric(h_all["underlying_price"],errors="coerce"))
              .groupby("timestamp")["_spot"].last().reindex(pivot.columns))
        price=[float(v) if pd.notna(v) else float("nan") for v in ps]
    else: price=[float("nan")]*pivot.shape[1]

    out.update({"ready":True,"strikes":pivot.index.to_numpy(float).tolist(),
                "times":[pd.Timestamp(t) for t in pivot.columns],"z":raw,"z_scaled":hm["z"],
                "gamma_flip_line":flips,"price_line":price,
                "denominator":hm.get("denominator"),"denominator_source":hm.get("denominator_source"),
                "comparable_across_time":hm.get("comparable_across_time",False),
                "comparable_across_sessions":hm.get("comparable_across_sessions",False),
                "note":"Altura = exposición firmada real en M$. Session/Anchor modifican solo color/intensidad. Gamma Flip siempre se calcula con Gamma."})
    return out

# =============================================================================
# v1.16.0 · TRACE FUSION / FLOW BARS / EXPOSURE CUBE
# Built on v1.15.8 guardrails: no silent metric fallback, no future-session
# normalisation, and all contextual views remain outside Scanner authority.
# =============================================================================

def _normalize_view(view: str) -> str:
    v=str(view or "net").strip().lower()
    if v.startswith("call"): return "calls"
    if v.startswith("put"): return "puts"
    return "net"


def _filter_option_view(df: pd.DataFrame, view: str) -> pd.DataFrame:
    v=_normalize_view(view)
    if v=="net": return df.copy()
    if "option_type" not in df.columns: return pd.DataFrame(columns=df.columns)
    want="c" if v=="calls" else "p"
    return df[df["option_type"].astype(str).str.lower().str.startswith(want)].copy()


def strike_profile(current: pd.DataFrame, metric: str = "gex", view: str = "net",
                   history: pd.DataFrame | None = None, asof: Any | None = None) -> pd.DataFrame:
    """Current per-strike GEX/DEX profile plus causal session high/low wicks.

    The current bar and the wick ALWAYS use the same metric and Calls/Puts/Net view.
    There is no Gamma->Delta fallback.  ``asof`` can be supplied for replay so future
    snapshots never leak into an earlier wick.
    """
    cols=["strike","value","wick_low","wick_high","side","metric","view"]
    if current is None or current.empty or "strike" not in current.columns:
        return pd.DataFrame(columns=cols)
    m=str(metric or "gex").strip().lower()
    field={"gex":"signed_gex_proxy","gamma":"signed_gex_proxy",
           "dex":"option_delta_exposure_info","delta":"option_delta_exposure_info"}.get(m)
    if field is None or field not in current.columns:
        return pd.DataFrame(columns=cols)
    d=_filter_option_view(current,view)
    if d.empty:return pd.DataFrame(columns=cols)
    d["strike"]=pd.to_numeric(d["strike"],errors="coerce")
    d[field]=pd.to_numeric(d[field],errors="coerce").fillna(0.0)
    d=d.dropna(subset=["strike"])
    out=d.groupby("strike",as_index=False)[field].sum().rename(columns={field:"value"})
    out["wick_low"]=out["value"];out["wick_high"]=out["value"]
    if history is not None and not history.empty and field in history.columns and "timestamp" in history.columns:
        h=history.copy();h["timestamp"]=pd.to_datetime(h["timestamp"],errors="coerce")
        h=h.dropna(subset=["timestamp"])
        if asof is not None:
            try:h=h[h["timestamp"]<=pd.Timestamp(asof)]
            except Exception as _e:
                _obs_note('trace_analytics:1032', _e)
        h=_filter_option_view(h,view)
        if not h.empty:
            h["strike"]=pd.to_numeric(h["strike"],errors="coerce")
            h[field]=pd.to_numeric(h[field],errors="coerce").fillna(0.0)
            h=h.dropna(subset=["strike"])
            per=h.groupby(["timestamp","strike"],as_index=False)[field].sum()
            rng=per.groupby("strike")[field].agg(["min","max"]).reset_index()
            rng.columns=["strike","wick_low","wick_high"]
            out=out.drop(columns=["wick_low","wick_high"]).merge(rng,on="strike",how="left")
            out["wick_low"]=out["wick_low"].fillna(out["value"])
            out["wick_high"]=out["wick_high"].fillna(out["value"])
    out["side"]=np.where(out["value"]>=0,"POS","NEG")
    out["metric"]="GEX" if field=="signed_gex_proxy" else "DEX"
    out["view"]=_normalize_view(view).upper()
    return out.sort_values("strike")[cols].reset_index(drop=True)


def _exposure_metric_spec(metric: str) -> Dict[str, Any] | None:
    m=str(metric or "Gamma").strip().lower()
    specs={
        "gamma":{"field":"signed_gex_proxy","agg":"sum","divisor":1e6,"unit":"M$","signed":True},
        "gex":{"field":"signed_gex_proxy","agg":"sum","divisor":1e6,"unit":"M$","signed":True},
        "delta":{"field":"option_delta_exposure_info","agg":"sum","divisor":1e6,"unit":"M$","signed":True},
        "dex":{"field":"option_delta_exposure_info","agg":"sum","divisor":1e6,"unit":"M$","signed":True},
        # v1.42.4 · Unidades canónicas de units_registry en vez de "proxy" sin escalar.
        # Antes se publicaban como `calc_X * OI * mult`, sin escalar por el spot ni por
        # el horizonte temporal, mientras el heatmap publicaba la misma magnitud
        # multiplicada por el spot y la llamaba `$M`. Mismo nombre, dos escalas que se
        # diferencian en ~600x en SPY: invitaba a comparar lo que no es comparable.
        "vanna":{"field":"_vanna_exposure","agg":"sum","divisor":1e6,"unit":"$M Δ/punto de vol","signed":True,"unit_code":"VANNA_PER_VOL_POINT"},
        "charm":{"field":"_charm_exposure","agg":"sum","divisor":1e6,"unit":"$M Δ/día","signed":True,"unit_code":"CHARM_PER_DAY"},
        "speed":{"field":"_speed_exposure","agg":"sum","divisor":1e6,"unit":"$M GEX/1%","signed":True,"unit_code":"SPEED_PER_ONE_PERCENT"},
        "open interest":{"field":"open_interest","agg":"sum","divisor":1.0,"unit":"contratos","signed":False},
        "oi":{"field":"open_interest","agg":"sum","divisor":1.0,"unit":"contratos","signed":False},
        "net oi":{"field":"_net_oi","agg":"sum","divisor":1.0,"unit":"Call OI - Put OI","signed":True},
        "oi net":{"field":"_net_oi","agg":"sum","divisor":1.0,"unit":"Call OI - Put OI","signed":True},
        "volumen":{"field":"volume","agg":"sum","divisor":1.0,"unit":"contratos","signed":False},
        "volume":{"field":"volume","agg":"sum","divisor":1.0,"unit":"contratos","signed":False},
        "volumen neto":{"field":"_net_volume","agg":"sum","divisor":1.0,"unit":"Call Vol - Put Vol","signed":True},
        "net volume":{"field":"_net_volume","agg":"sum","divisor":1.0,"unit":"Call Vol - Put Vol","signed":True},
        "score cuantitativo":{"field":"quant_score","agg":"max","divisor":1.0,"unit":"0-100","signed":False},
        "q-score":{"field":"quant_score","agg":"max","divisor":1.0,"unit":"0-100","signed":False},
        "actividad inusual":{"field":"activity_ratio","agg":"max","divisor":1.0,"unit":"Vol/OI","signed":False},
    }
    return specs.get(m)


def exposure_cube(history: pd.DataFrame, metric: str = "Gamma", view: str = "net",
                  max_strikes: int = 25, spot: float | None = None) -> Dict[str, Any]:
    """Latest strike × expiry grid with exact metric semantics and Calls/Puts/Net view.

    If the requested metric is unavailable the function returns UNAVAILABLE. It never
    substitutes Gamma (or any other field) under a different label.
    """
    out={"ready":False,"strikes":[],"expiries":[],"z":None,"metric":str(metric),
         "view":_normalize_view(view).upper(),"reason":None}
    if history is None or history.empty:
        out["reason"]="sin datos";return out
    h=history.copy()
    if "expiration_date" not in h.columns or "strike" not in h.columns:
        out["reason"]="faltan strike o expiration_date";return out
    if "timestamp" in h.columns:
        ts=pd.to_datetime(h["timestamp"],errors="coerce")
        if ts.notna().any():h=h[ts==ts.max()].copy()
    h=_filter_option_view(h,view)
    if h.empty:
        out["reason"]=f"sin contratos {_normalize_view(view).upper()}";return out
    spec=_exposure_metric_spec(metric)
    if not spec:
        out["reason"]=f"métrica {metric} no soportada";return out
    # Same transparent proxy definitions already used by the existing v1.15.8 surface.
    oi=numeric_column(h,"open_interest",0.0)
    mult=multiplier_series(h)
    # Escala económica común: el mismo `sign` call+/put- que ya usa signed_gex_proxy y
    # el spot del propio contrato. Sin esto cada griega vivía en su propia escala.
    _S=numeric_column(h,"underlying_price",float("nan"))
    if not _S.notna().any():
        _S=pd.Series(float(spot) if spot is not None else 0.0,index=h.index,dtype=float)
    _S=_S.fillna(float(spot) if spot is not None else 0.0)
    _is_call=h.get("option_type",pd.Series("call",index=h.index)).astype(str).str.lower().str.startswith("c")
    _sign=pd.Series(np.where(_is_call,1.0,-1.0),index=h.index,dtype=float)
    if spec["field"]=="_vanna_exposure":
        if "calc_vanna" not in h.columns:out["reason"]="Vanna UNAVAILABLE";return out
        # USD de delta por 1 punto de volatilidad (1 % = 0.01 en decimal).
        h[spec["field"]]=_sign*numeric_column(h,"calc_vanna",float("nan"))*0.01*oi*mult*_S
    elif spec["field"]=="_charm_exposure":
        if "calc_charm" not in h.columns:out["reason"]="Charm UNAVAILABLE";return out
        # `calc_charm` es dDelta/dt POR AÑO (ACT/365); /365 lo lleva a por día.
        h[spec["field"]]=_sign*numeric_column(h,"calc_charm",float("nan"))/365.0*oi*mult*_S
    elif spec["field"]=="_speed_exposure":
        if "calc_speed" not in h.columns:out["reason"]="Speed UNAVAILABLE";return out
        # Cambio de $GEX ante un movimiento de +1 % del subyacente.
        h[spec["field"]]=_sign*numeric_column(h,"calc_speed",float("nan"))*(0.01*_S)*oi*mult*(_S**2)*0.01
    elif spec["field"]=="activity_ratio":
        h["activity_ratio"]=numeric_column(h,"volume",0)/(oi+1.0)
    elif spec["field"] in {"_net_oi","_net_volume"}:
        if "option_type" not in h.columns:
            out["reason"]=f"{metric} UNAVAILABLE · option_type ausente";return out
        side=np.where(h["option_type"].astype(str).str.lower().str.startswith("c"),1.0,-1.0)
        base=oi if spec["field"]=="_net_oi" else numeric_column(h,"volume",0.0)
        h[spec["field"]]=side*base
    elif spec["field"] not in h.columns:
        out["reason"]=f"{metric} UNAVAILABLE";return out
    h["strike"]=pd.to_numeric(h["strike"],errors="coerce")
    h[spec["field"]]=pd.to_numeric(h[spec["field"]],errors="coerce").fillna(0.0)
    h=h.dropna(subset=["strike"])
    if h.empty:out["reason"]="sin strikes válidos";return out
    sp=float(spot) if spot is not None else float(numeric_column(h,"underlying_price",float("nan")).dropna().iloc[-1])
    ks=np.sort(h["strike"].unique());centre=int(np.argmin(np.abs(ks-sp)));half=max(int(max_strikes),5)//2
    keep=ks[max(0,centre-half):centre+half+1];h=h[h["strike"].isin(keep)]
    grid=h.pivot_table(index="strike",columns="expiration_date",values=spec["field"],aggfunc=spec["agg"],fill_value=0).sort_index()
    if grid.empty:out["reason"]="grid vacío";return out
    order=sorted(grid.columns,key=lambda c:str(c));grid=grid[order]
    z=grid.to_numpy(float)/float(spec["divisor"])
    out.update({"ready":True,"strikes":grid.index.to_numpy(float).tolist(),"expiries":[str(c) for c in grid.columns],
                "z":z,"field":spec["field"],"spot":sp,"unit":spec["unit"],"signed":bool(spec["signed"]),
                "note":"Métrica y Calls/Puts/Net son exactos; no existe fallback silencioso entre exposiciones."})
    return out


def flow_bar_anchor_samples(events: pd.DataFrame, resample: str = "1min") -> Dict[str,list[float]]:
    """Current-session bucket premium samples staged for tomorrow's historical anchor."""
    if events is None or events.empty or "timestamp" not in events.columns:return {}
    e=events.copy();e["timestamp"]=pd.to_datetime(e["timestamp"],errors="coerce");e=e.dropna(subset=["timestamp"])
    if e.empty:return {}
    prem=numeric_column(e,"premium",0.0).abs()
    g=pd.DataFrame({"timestamp":e["timestamp"],"premium":prem}).set_index("timestamp").resample(str(resample))["premium"].sum()
    vals=[float(v) for v in g.to_numpy(float) if math.isfinite(float(v)) and float(v)>0]
    key="flow_bar_premium_"+str(resample).lower().replace(" ","_")
    return {key:vals} if vals else {}


def flow_bars(events: pd.DataFrame, resample: str = "1min", anchors: Dict[str,Any] | None = None,
              counterparty_share: float = 0.70, min_session_bars: int = 5) -> pd.DataFrame:
    """Causal premium anomaly bars + estimated delta-weighted hedge notional.

    Historical magnitude is read from a PRIOR-session anchor. The intraday mean/std are
    expanding and shifted one bar, so a bucket never normalises itself or sees the future.
    Early in the day historical scale dominates; session evidence earns more weight as
    completed buckets accumulate.
    """
    cols=["bucket","premium","premium_avg_prior","premium_z_session","premium_anchor_score",
          "premium_anomaly_score","anomaly_status","delta_notional","events","prior_bars"]
    if events is None or events.empty:return pd.DataFrame(columns=cols)
    e=events.copy();e["timestamp"]=pd.to_datetime(e.get("timestamp"),errors="coerce");e=e.dropna(subset=["timestamp"]).sort_values("timestamp")
    if e.empty:return pd.DataFrame(columns=cols)
    prem=numeric_column(e,"premium",0.0).abs()
    g=(pd.DataFrame({"timestamp":e["timestamp"],"premium":prem}).set_index("timestamp").resample(str(resample))
       .agg(premium=("premium","sum"),events=("premium","size")).fillna(0.0))
    out=g.reset_index().rename(columns={"timestamp":"bucket"})
    p=pd.to_numeric(out["premium"],errors="coerce").fillna(0.0)
    out["prior_bars"]=np.arange(len(out),dtype=int)
    out["premium_avg_prior"]=p.expanding(min_periods=1).mean().shift(1)
    prior_sd=p.expanding(min_periods=2).std(ddof=1).shift(1)
    ready=(out["prior_bars"]>=int(max(min_session_bars,2))) & prior_sd.gt(1e-9)
    out["premium_z_session"]=((p-out["premium_avg_prior"])/prior_sd).where(ready)
    anchor_key="flow_bar_premium_"+str(resample).lower().replace(" ","_")
    hist=np.full(len(out),np.nan,dtype=float)
    try:
        from .scale_anchors import anchored_magnitude
        a=anchored_magnitude(p.to_numpy(float),(anchors or {}).get(anchor_key))
        if a is not None:hist=np.asarray(a,dtype=float)
    except Exception as _e:
        _obs_note('trace_analytics:1178', _e)
    out["premium_anchor_score"]=hist
    z=pd.to_numeric(out["premium_z_session"],errors="coerce").to_numpy(float)
    session_score=np.where(np.isfinite(z),1.0/(1.0+np.exp(-np.clip(z,-8,8))),np.nan)
    prior=out["prior_bars"].to_numpy(float)
    w_session=np.clip(prior/(prior+10.0),0.0,0.80)
    comb=np.full(len(out),np.nan,dtype=float)
    for i in range(len(out)):
        h=hist[i] if np.isfinite(hist[i]) else np.nan;s=session_score[i] if np.isfinite(session_score[i]) else np.nan
        if np.isfinite(h) and np.isfinite(s):comb[i]=(1-w_session[i])*h+w_session[i]*s
        elif np.isfinite(h):comb[i]=h
        elif np.isfinite(s):comb[i]=s
    out["premium_anomaly_score"]=np.where(np.isfinite(comb),np.clip(comb*100.0,0,100),np.nan)
    out["anomaly_status"]=np.where(np.isfinite(out["premium_anchor_score"]),"HISTORICAL + CAUSAL SESSION",
                             np.where(np.isfinite(out["premium_z_session"]),"CAUSAL SESSION ONLY","COLLECTING"))
    hedge=dealer_flow_line(e,counterparty_share=counterparty_share,resample=str(resample))
    if not hedge.empty:
        out=out.merge(hedge[["timestamp","hedge_notional"]].rename(columns={"timestamp":"bucket","hedge_notional":"delta_notional"}),on="bucket",how="left")
    if "delta_notional" not in out.columns:out["delta_notional"]=0.0
    out["delta_notional"]=pd.to_numeric(out["delta_notional"],errors="coerce").fillna(0.0)
    return out[cols]

# ============================================================================
# v1.16.7 · TRACE Trading Workspace framing helpers
# ============================================================================
def price_view_range(prices, levels: Iterable[float] | None = None,
                     expected_move: float | None = None,
                     strike_min: float | None = None, strike_max: float | None = None,
                     mode: str = "auto", pad_pct: float = 0.18,
                     level_reach_em: float = 1.2) -> Dict[str, Any]:
    """Vertical range for TRACE's price panel, independent of the loaded strike ladder.

    `auto` is price-first: only visible prices determine the scale. Nearby structural
    levels are reported but do not expand the frame. `structure` may extend the range to
    nearby levels while distant levels remain out-of-view markers. `sigma1`/`sigma2`
    centre on the latest price, and `strikes` preserves the legacy full-ladder view.
    """
    out = {"range": None, "mode": str(mode), "span": None, "price_share_pct": None,
           "levels_kept": [], "levels_dropped": [], "reason": None}
    p = pd.to_numeric(pd.Series(list(prices) if prices is not None else []), errors="coerce").dropna()
    try:
        em = float(expected_move) if expected_move is not None and float(expected_move) > 0 else None
    except Exception:
        em = None
    m = str(mode or "auto").strip().lower()
    aliases = {"auto precio": "auto", "price": "auto", "estructura": "structure",
               "sigma 1": "sigma1", "sigma 2": "sigma2", "legacy": "strikes"}
    m = aliases.get(m, m)
    if m == "strikes":
        if strike_min is None or strike_max is None:
            out["reason"] = "sin escalera de strikes"
            return out
        lo, hi = float(strike_min) - .35, float(strike_max) + .35
    elif m in ("sigma1", "sigma2") and em and len(p):
        k = 1.0 if m == "sigma1" else 2.0
        c = float(p.iloc[-1])
        lo, hi = c - k * em, c + k * em
    else:
        if not len(p):
            out["reason"] = "sin precio en la ventana"
            return out
        lo_p, hi_p = float(p.min()), float(p.max())
        raw_span = max(hi_p - lo_p, 1e-9)
        floor = (em * 0.10) if em else max(abs(float(p.iloc[-1])) * 0.00035, 0.02)
        span = max(raw_span, floor)
        pad = span * float(pad_pct)
        lo, hi = lo_p - pad, hi_p + pad
        reach = (em * float(level_reach_em)) if em else span * 3.0
        for lv in (levels or []):
            try:
                v = float(lv)
            except Exception as _e:
                _obs_note('trace_analytics:1252', _e)
                continue
            if not math.isfinite(v):
                continue
            if lo_p - reach <= v <= hi_p + reach:
                out["levels_kept"].append(round(v, 4))
                if m == "structure":
                    lo, hi = min(lo, v - pad * .5), max(hi, v + pad * .5)
            else:
                out["levels_dropped"].append(round(v, 4))
    span = float(hi - lo)
    share = (100.0 * float(p.max() - p.min()) / span) if len(p) and span > 0 else None
    out.update({"range": [round(float(lo), 6), round(float(hi), 6)],
                "span": round(span, 6),
                "price_share_pct": None if share is None else round(share, 1),
                "note": "AUTO sigue al precio visible; ESTRUCTURA sólo añade niveles cercanos."})
    return out


def time_view_range(times, tail_minutes: float | None = None,
                    pad_pct: float = 0.025) -> Dict[str, Any]:
    """Horizontal range from actual price timestamps, optionally restricted to a tail."""
    out = {"range": None, "span_minutes": None, "reason": None}
    t = pd.to_datetime(pd.Series(list(times) if times is not None else []), errors="coerce").dropna()
    if t.empty:
        out["reason"] = "sin marcas de tiempo"
        return out
    end = t.max(); start = t.min()
    if tail_minutes is not None:
        try:
            tm = float(tail_minutes)
            if tm > 0:
                start = max(start, end - pd.Timedelta(minutes=tm))
        except Exception as _e:
            _obs_note('trace_analytics:1282', _e)
    span = max((end - start).total_seconds() / 60.0, 1.0)
    pad = pd.Timedelta(minutes=span * float(pad_pct))
    out.update({"range": [(start - pad).isoformat(), (end + pad).isoformat()],
                "span_minutes": round(span, 1),
                "start": start.isoformat(), "end": end.isoformat(),
                "note": "Rango temporal tomado del precio visible."})
    return out

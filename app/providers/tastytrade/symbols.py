from __future__ import annotations

from typing import Any

from ...core.asset_ecosystems import (
    ASSET_ECOSYSTEMS,
    ecosystem_for,
    derivative_roots,
)

# Backward-compatible public name.  The canonical source now lives in core/asset_ecosystems.py.
ECOSYSTEMS = ASSET_ECOSYSTEMS


def ecosystem(symbol: str) -> dict[str, Any]:
    return ecosystem_for(symbol)


async def _equity_streamer(client, symbol: str, *, role: str, instrument_type: str = "EQUITY") -> tuple[dict[str, Any] | None, str | None]:
    sym = str(symbol).upper()
    try:
        eq = await client.equity_instrument(sym)
        if eq and (eq.get("streamer-symbol") or eq.get("symbol")):
            return ({
                "role": role,
                "instrument_type": instrument_type,
                "canonical_symbol": sym,
                "provider_symbol": eq.get("symbol") or sym,
                "streamer_symbol": eq.get("streamer-symbol") or sym,
                "events": ["Quote", "Trade", "Summary"],
                "instrument": eq,
            }, None)
    except Exception as exc:
        return None, f"{sym}:{type(exc).__name__}:{str(exc)[:90]}"
    return None, f"{sym}:NO_INSTRUMENT"


async def ecosystem_subscription_plan(symbol: str, client) -> dict[str, Any]:
    """Resolve the HOT market-data ecosystem for the selected ITM QUANT asset.

    The fast path subscribes to the primary equity/ETF (when applicable), related
    ETFs/equities, benchmark indices when tastytrade exposes a streamer symbol, and
    the front contract of each configured futures product.  Index REST snapshots are
    used only as a truthful fallback when no streamer symbol can be resolved.
    """
    sym = str(symbol).upper()
    eco = ecosystem_for(sym)
    streamers: list[dict[str, Any]] = []
    errors: list[str] = []
    front_futures: dict[str, dict[str, Any]] = {}
    index_snapshot: list[dict[str, Any]] = []

    primary_type = str(eco.get("primary_type") or "EQUITY").upper()
    if primary_type != "INDEX":
        row, err = await _equity_streamer(client, sym, role="UNDERLYING", instrument_type=primary_type)
        if row:
            streamers.append(row)
        elif err:
            # Common listed equities/ETFs use their ticker as DXLink symbol.  Keep the
            # legacy fallback for the primary only; related/index symbols are never guessed.
            streamers.append({
                "role": "UNDERLYING", "instrument_type": primary_type,
                "canonical_symbol": sym, "provider_symbol": sym, "streamer_symbol": sym,
                "events": ["Quote", "Trade", "Summary"], "fallback_symbol": True,
            })
            errors.append(err)

    # Related ETFs/equities are true ecosystem observations, not visual-only breadth.
    for comp in eco.get("related_equities", []):
        rsym = str(comp.get("symbol") or "").upper()
        if not rsym or rsym == sym:
            continue
        row, err = await _equity_streamer(client, rsym, role=str(comp.get("role") or "RELATED_EQUITY"), instrument_type="EQUITY")
        if row:
            row["polarity"] = int(comp.get("polarity") or 1)
            streamers.append(row)
        elif err:
            errors.append(err)

    # Try a provider-issued streamer symbol for indices first.  tastytrade's equity
    # instrument schema can describe index instruments; if lookup does not resolve,
    # use a single REST market-data snapshot without inventing a streamer symbol.
    for idx in eco.get("indices", []):
        isym = str(idx.get("symbol") or "").upper()
        if not isym:
            continue
        row, err = await _equity_streamer(client, isym, role=str(idx.get("role") or "INDEX"), instrument_type="INDEX")
        if row:
            streamers.append(row)
        else:
            if err:
                errors.append(err)
            candidates = [isym] + [str(x).upper() for x in (idx.get("aliases") or []) if x]
            got = []
            used = None
            for candidate in candidates:
                try:
                    got = await client.market_data_by_type("index", [candidate])
                    if got:
                        used = candidate
                        break
                except Exception as exc:
                    errors.append(f"INDEX:{candidate}:{type(exc).__name__}:{str(exc)[:80]}")
            for r in got or []:
                if isinstance(r, dict):
                    index_snapshot.append({**r, "canonical_index": isym, "provider_index": used or isym, "role": idx.get("role") or "INDEX"})

    # Primary + micro/broad futures products.  Product codes are resolved to actual
    # active contracts by the provider; contract month symbols are never hand-built.
    for fcfg in eco.get("futures", []):
        product = str(fcfg.get("product") or "").upper()
        if not product:
            continue
        try:
            fut = await client.front_future(product)
            if fut:
                front_futures[product] = fut
                if fut.get("streamer-symbol"):
                    streamers.append({
                        "role": str(fcfg.get("role") or "FUTURE"),
                        "instrument_type": "FUTURE",
                        "canonical_symbol": product,
                        "provider_symbol": fut.get("symbol"),
                        "streamer_symbol": fut.get("streamer-symbol"),
                        "events": ["Quote", "Trade", "Summary"],
                        "product": product,
                        "preferred": bool(fcfg.get("preferred", False)),
                        "instrument": fut,
                    })
        except Exception as exc:
            errors.append(f"FUTURE:{product}:{type(exc).__name__}:{str(exc)[:90]}")

    return {
        "ready": bool(streamers or index_snapshot),
        "symbol": sym,
        "ecosystem": eco,
        "front_futures": front_futures,
        # Legacy singular field retained for older diagnostics.
        "front_future": next(iter(front_futures.values()), None),
        "index_snapshot": index_snapshot,
        "streamers": streamers,
        "errors": errors,
        "authority": "DATA_INPUT_ONLY",
        "normalization_required": True,
        "architecture": "PRIMARY + RELATED_ETF/EQUITY + INDEX + FUTURES + DERIVATIVES",
    }


def _num(v: Any) -> float | None:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except Exception:
        return None


def _snapshot_price(items: list[dict[str, Any]]) -> float | None:
    if not items:
        return None
    r = items[0] if isinstance(items[0], dict) else {}
    for k in ("lastExt", "last-ext", "last_ext", "last", "lastMkt", "last-mkt", "mark", "mid", "close"):
        x = _num(r.get(k))
        if x is not None and x > 0:
            return x
    bid = _num(r.get("bid")); ask = _num(r.get("ask"))
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / 2.0
    return None


def _select_option_instruments(items: list[dict[str, Any]], *, spot: float | None, max_dte: int, expiration_count: int, strikes_per_expiry: int, max_contracts: int) -> list[dict[str, Any]]:
    rows = []
    for x in items or []:
        if not isinstance(x, dict) or not x.get("active", True):
            continue
        dte = int(_num(x.get("days-to-expiration")) or 0)
        if dte < 0 or dte > int(max_dte):
            continue
        streamer = str(x.get("streamer-symbol") or "").strip()
        osym = str(x.get("symbol") or "").strip()
        strike = _num(x.get("strike-price"))
        exp = str(x.get("expires-at") or x.get("expiration-date") or "")
        if not streamer or not osym or strike is None or not exp:
            continue
        rows.append({**x, "_dte": dte, "_strike": strike, "_exp": exp, "_dist": abs(strike - spot) if spot is not None else abs(strike)})
    if not rows:
        return []
    expirations = sorted({(r["_dte"], r["_exp"]) for r in rows})[:max(1, int(expiration_count))]
    selected: list[dict[str, Any]] = []
    for _, exp in expirations:
        exp_rows = [r for r in rows if r["_exp"] == exp]
        strike_order: list[float] = []
        for r in sorted(exp_rows, key=lambda q: (q["_dist"], q["_strike"], str(q.get("option-type") or ""))):
            if r["_strike"] not in strike_order:
                strike_order.append(r["_strike"])
            if len(strike_order) >= max(1, int(strikes_per_expiry)):
                break
        keep = set(strike_order)
        selected.extend(r for r in exp_rows if r["_strike"] in keep)
    selected.sort(key=lambda q: (q["_dte"], q["_dist"], q["_strike"], str(q.get("option-type") or "")))
    return selected[:max(1, int(max_contracts))]


async def _spot_for_root(client, root: str, instrument_type: str) -> float | None:
    try:
        rows = await client.market_data_by_type(instrument_type, [root])
        return _snapshot_price(rows)
    except Exception:
        return None


async def derivative_subscription_plan(symbol: str, client, settings, base_plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve one globally bounded derivative universe across the whole asset ecosystem.

    The total DXLink option budget remains ``derivative_max_contracts``.  Adding more
    roots therefore broadens information without multiplying subscriptions without bound.
    """
    sym = str(symbol).upper(); roots = derivative_roots(sym); base_plan = base_plan or {}
    result: dict[str, Any] = {
        "symbol": sym, "equity_options": [], "index_options": [], "future_options": [],
        "errors": [], "authority": "DATA_INPUT_ONLY", "budget": int(settings.derivative_max_contracts),
    }
    all_roots = [("equity", x) for x in roots.get("equity_options", [])] + [("index", x) for x in roots.get("index_options", [])] + [("future", x) for x in roots.get("future_options", [])]
    root_count = max(1, len(all_roots))
    per_root = max(8, int(settings.derivative_max_contracts) // root_count)
    remaining = int(settings.derivative_max_contracts)

    async def add_equity_like(root: str, kind: str) -> None:
        nonlocal remaining
        if remaining <= 0:
            return
        try:
            spot = await _spot_for_root(client, root, "index" if kind == "index" else "equity")
            chain = await client.equity_option_instruments(root)
            selected = _select_option_instruments(
                chain, spot=spot, max_dte=settings.derivative_max_dte,
                expiration_count=settings.derivative_expirations,
                strikes_per_expiry=settings.derivative_strikes_per_expiry,
                max_contracts=min(remaining, per_root),
            )
            target = result["index_options"] if kind == "index" else result["equity_options"]
            role = "INDEX_OPTION" if kind == "index" else "EQUITY_OPTION"
            itype = "INDEX_OPTION" if kind == "index" else "EQUITY_OPTION"
            for x in selected:
                target.append({
                    "role": role, "instrument_type": itype,
                    "underlying_symbol": sym, "component_symbol": root,
                    "canonical_symbol": str(x.get("symbol") or "").upper(),
                    "provider_symbol": x.get("symbol"), "streamer_symbol": x.get("streamer-symbol"),
                    "strike": _num(x.get("strike-price")), "dte": int(_num(x.get("days-to-expiration")) or 0),
                    "expiration": x.get("expires-at") or x.get("expiration-date"), "option_type": x.get("option-type"),
                    "contract_multiplier": _num(x.get("shares-per-contract") or x.get("multiplier")),
                    "events": ["Quote", "Trade", "Greeks", "Summary"],
                })
            remaining -= len(selected)
        except Exception as exc:
            result["errors"].append(f"{kind}_options:{root}:{type(exc).__name__}:{str(exc)[:100]}")

    for root in roots.get("equity_options", []):
        await add_equity_like(str(root).upper(), "equity")
    for root in roots.get("index_options", []):
        await add_equity_like(str(root).upper(), "index")

    fronts = (base_plan or {}).get("front_futures") or {}
    for product in roots.get("future_options", []):
        if remaining <= 0:
            break
        product = str(product).upper()
        try:
            front = fronts.get(product) or await client.front_future(product) or {}
            fspot = None
            fsym = str(front.get("symbol") or "")
            if fsym:
                fspot = await _spot_for_root(client, fsym, "future")
            chain = await client.futures_option_instruments(product)
            selected = _select_option_instruments(
                chain, spot=fspot, max_dte=settings.derivative_max_dte,
                expiration_count=settings.derivative_expirations,
                strikes_per_expiry=settings.derivative_strikes_per_expiry,
                max_contracts=min(remaining, per_root),
            )
            for x in selected:
                result["future_options"].append({
                    "role": "FUTURE_OPTION", "instrument_type": "FUTURE_OPTION",
                    "underlying_symbol": sym, "component_symbol": product,
                    "canonical_symbol": str(x.get("symbol") or "").upper(),
                    "provider_symbol": x.get("symbol"), "streamer_symbol": x.get("streamer-symbol"),
                    "strike": _num(x.get("strike-price")), "dte": int(_num(x.get("days-to-expiration")) or 0),
                    "expiration": x.get("expires-at") or x.get("expiration-date"), "option_type": x.get("option-type"),
                    "contract_multiplier": _num(x.get("shares-per-contract") or x.get("multiplier")),
                    "events": ["Quote", "Trade", "Greeks", "Summary"],
                })
            remaining -= len(selected)
        except Exception as exc:
            result["errors"].append(f"future_options:{product}:{type(exc).__name__}:{str(exc)[:100]}")

    result["contracts"] = len(result["equity_options"]) + len(result["index_options"]) + len(result["future_options"])
    result["budget_remaining"] = max(0, remaining)
    result["normalization_required"] = True
    result["fusion_rule"] = "SEPARATE_OPTION_FAMILIES_THEN_NORMALIZE"
    return result

# ITM QUANT · Quant Math Specification

## Principle
Every mathematical output that can influence context, timing, risk or a displayed quantitative surface must be deterministic, testable and carry model provenance. A passing test means numerical agreement with an analytical identity, trusted reference value or invariant—not merely that a function executes.

## Option model layer
Baseline local sensitivities use Black–Scholes–Merton with explicit risk-free rate and carry/dividend input:
- price, Delta, Gamma, Vega where used;
- Vanna, Charm, Speed;
- Color via causal finite-time Gamma difference when a closed-form implementation is not used.

Tolerance tests cover calls/puts, ATM/ITM/OTM, short and longer DTE, low/high IV, and vector/scalar parity.

## Exposures
GEX/DEX and higher-order fields are transformations of model Greeks, OI/contract multiplier and spot. They are exposures/proxies, not proof of dealer inventory or observed hedge trades. Sign conventions must be explicit per calculation.

## Repricing
Between authoritative option snapshots, TRACE may reprice sensitivities using the latest valid IV/OI/DTE plus current spot/time. This is labeled `LIVE SPOT/TIME REPRICE`; observed OPRA volume remains a separate channel.

## Volatility surfaces
IV points originate from provider IV or a bounded implied-volatility inversion from valid quotes. Interpolation/smoothing may construct `sigma(K,T)` but does not create an observed quote. Surface quality reports point count, coverage, interpolation method and stale age.

## Market State Field
State is hierarchical, not a magic sum:
- directional pressure: Delta, aggressor flow, related-market breadth, estimated hedge pressure/positioning when available;
- stability/friction: Gamma regime and topology;
- volatility state;
- liquidity/flow kinetics;
- macro/cross-asset context.

Cross-asset values are normalized by own sigma where available, with robust MAD fallback and bounded transforms to prevent a high-volatility asset from dominating solely because of scale.

## Q(K,T)
The combined field is a SHADOW research field until OOS calibration supplies validated weights. `Q` is never shown as win probability. Default research weights have zero production authority.

## Calibration/OOS
Any learned mapping must separate fit/calibration samples from evaluation samples, preserve temporal order/purge gaps where configured, report coverage and base rate, and remain SHADOW until readiness gates are satisfied.

## Synthetic book / microstructure
Any synthetic order-book metric must be explicitly marked synthetic/estimated unless actual depth data is present. CVD/aggressor metrics require defensible bid/ask or trade classification. Missing L2 must not be invented.

## Required test families
- analytical/reference Greeks and finite-difference identities;
- vector/scalar parity;
- exposure sign/scale invariants;
- zero/flip interpolation sanity;
- repricing monotonic/invariant cases;
- robust normalization and outlier resistance;
- deterministic causal ordering;
- Replay no-future-data property;
- calibration temporal separation;
- synthetic-book disclosure/fallback behavior;
- multi-asset property tests.

## Dealer microstructure and synthetic inventory (v1.21)
For a classified option trade, the synthetic dealer-side change is the opposite side of the inferred customer aggressor. A simplified delta-inventory update is:

`ΔInventory_t = ΔInventory_(t-1) + DealerSide_i × Delta_i × Contracts_i × Multiplier`.

The hedge-to-neutral estimate is approximately the opposite underlying delta exposure, repriced as spot/IV/time evolve. This is an inventory/hedging model, not a participant-level observation.

Aggressor classification records method and confidence. Causal NBBO is preferred; tick rule is lower confidence; unsynchronized REST quote snapshots cannot receive high-confidence aggressor authority.

Opening/closing remains an inference unless a source provides authoritative capacity/open-close flags. Package detection labels `BLOCK_CANDIDATE`, `SWEEP_CANDIDATE`, and `MULTI_LEG_CANDIDATE`; these are clustering hypotheses, not exchange-confirmed strategy identifiers.

## Dealer state score and confidence
The displayed dealer-state score is a bounded contextual state assembled from estimated field sign, hedge requirement, recent inventory shift and observed-flow consistency. It is **not a probability** and cannot change Scanner direction.

Dealer confidence is an observability/model-support score driven primarily by synchronized NBBO coverage, aggressor quality, usable Greeks/OI coverage and related-flow availability. It must fall when the data needed to support the inference is absent.

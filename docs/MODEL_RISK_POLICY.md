# ITM QUANT · Model Risk Policy

## Core rule
Observed market data, model-derived values and estimated/proxy quantities must never share the same visual or textual claim without provenance.

## Mandatory UI disclosures
Theoretical option premium/P&L paths that assume constant IV and fixed spread display:
`MODELO TEÓRICO · IV CONSTANTE · SPREAD FIJO · NO ES PRECIO PROYECTADO`.

Scenario visualization should expose `IV -Δ`, `BASE`, `IV +Δ` when meaningful. Model surfaces state that they are theoretical/estimated and not observed dealer inventory.

## Examples
- OI: observed snapshot when sourced from provider.
- Gamma/Delta: model sensitivities/exposures calculated from inputs.
- Repriced Gamma/Delta between snapshots: theoretical live spot/time repricing.
- OPRA prints/contracts/premium: observed transactions when feed is live.
- Dealer hedge pressure: estimated unless a directly observable hedge trade source exists.
- Q(K,T): research confluence field, not a probability.

## Production authority
A SHADOW model cannot flip Scanner direction or claim calibrated probability. Promotion requires explicit OOS readiness criteria and Auditor evidence.

## Staleness and degradation
Every view consuming model inputs must expose stale/fallback state when material. A precise-looking number is not enough to override poor source quality.

## Dealer Intelligence disclosures (v1.21)
The UI must distinguish all of the following:
- `OBSERVED`: OPRA trade/contract/premium and synchronized quote fields when actually received;
- `CLASSIFIED`: aggressor side inferred from causal NBBO/tick rule, with method/confidence;
- `ESTIMATED`: dealer inventory, opening/closing, hedge requirement and dealer field;
- `CANDIDATE`: sweep/block/multi-leg clustering that has not been exchange-confirmed.

`Dealer State Score`, `Q`, `confidence`, and microstructure-quality scores are not win probabilities. No panel may display a named dealer or imply a real participant inventory without an authorized source that explicitly provides that information.

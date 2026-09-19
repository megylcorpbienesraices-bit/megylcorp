# ITM QUANT · Dealer Intelligence Specification

## Objective
Estimate the aggregate option-dealer risk field and the mechanical hedge pressure consistent with observable option microstructure. The engine is designed for trading context, not participant identification.

## Pipeline
`OPTIONS STRUCTURE → OI/IV/Greeks → STRUCTURAL DEALER FIELD`

`LIVE OPTION TRADES → causal NBBO → aggressor classification → package candidates → opening/closing inference → SYNTHETIC DEALER INVENTORY`

`UNDERLYING / optional FUTURES FLOW → HEDGE CONSISTENCY CHECK`

`Dealer Field + Inventory Shift + Hedge Pressure + Flow Confirmation → Dealer State (context only)`

## Level 1 · Structural field
Inputs: spot, strike, expiry, call/put, bid/ask, price, volume, OI, IV/model IV, Greeks/model inputs, timestamp, multiplier.
Outputs include Gamma/Delta/Vanna/Charm/Speed/Color, GEX/DEX and `H(K,T)` hedge-pressure surface. These are model-derived exposures.

## Level 2 · Synthetic inventory
Every option trade is paired with the most recent causal quote. The classifier reports method, spread position, quote age and confidence. The inferred customer side is inverted to estimate dealer-side inventory change. The inventory is persisted so it can evolve through the session and be repriced when `S`, `sigma` or `T` change.

## Package candidates
- `BLOCK_CANDIDATE`: robust size/premium outlier with minimum floor.
- `SWEEP_CANDIDATE`: same contract/aggressor clustered across multiple venues in a short event-time bucket.
- `MULTI_LEG_CANDIDATE`: temporally clustered distinct contracts with size/side consistency suggestive of a package.

These labels are hypotheses. They do not claim official complex-order linkage unless a future source provides that identifier.

## Opening vs closing
The current engine may use an explicit heuristic/SHADOW calibrated model. Without authoritative account/capacity flags it remains inferred. Official OI stays separate and is used for reconciliation rather than rewritten intraday.

## Hedge confirmation
Estimated hedge need is compared with actually observed flow. Futures flow has highest confirmation value when an authorized real feed is connected; underlying SIP tape is useful but partial. Related-asset breadth is context only. No observed trade is attributed to a dealer.

## Confidence
Confidence is a support/observability score, not probability. Key inputs include synchronized NBBO coverage, aggressor-classification quality, usable Greeks/OI coverage, event freshness and optional futures-flow availability.

## Hard disclosure boundary
Never output `REAL DEALER INVENTORY` or a named firm's position from public OPRA/NBBO inference. The correct terms are `ESTIMATED DEALER INVENTORY`, `DEALER FIELD ESTIMATE` and `HEDGE PRESSURE ESTIMATE`.

## Production authority
Dealer Intelligence is contextual/SHADOW. Scanner remains the sole directional authority until a separately approved OOS promotion policy says otherwise.

## Causal underlying synchronization
Before synthetic inventory is updated, each option print is re-anchored to the latest underlying trade satisfying `underlying.event_time <= option.event_time` within the configured freshness tolerance. The option-snapshot spot is preserved separately as fallback/provenance. This prevents a later underlying tick from leaking backward into the option hedge estimate.

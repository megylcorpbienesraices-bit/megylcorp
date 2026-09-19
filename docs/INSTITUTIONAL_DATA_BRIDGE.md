# ITM QUANT · Institutional Data Bridge

## Purpose
This document is the mandatory contract for adding an institutional or broker data source to ITM QUANT without coupling the Quant Core to a vendor. Every provider is adapted into canonical events first; vendor-specific objects must not leak into Scanner, Market State, Replay, Calibration or the renderers.

## Architecture contract
`Provider → Adapter → Schema validation → Quality/Source Health → Causality Orderer → Canonical Store/Streams → Quant Core`

A source may provide market data, options, futures, macro, positioning or reference metadata. The same rules apply to all of them.

## Canonical clocks
Every event MUST preserve three separate UTC clocks:
- `event_time`: timestamp assigned by the venue/source; authoritative for causal ordering.
- `receive_time`: moment ITM QUANT received the event; used for latency/source-health.
- `process_time`: moment the deterministic engine pass processed the event; used for audit/replay provenance.

Do not overwrite `event_time` with local receipt time. When a source sequence number exists, retain it as `source_seq`. The deterministic tie-break contract is:
`event_time → source_seq → event-type priority → source → event_id`.

## Canonical trade schema
Required/expected fields:
`symbol, event_time, receive_time, process_time, price, size, source, source_seq, exchange`.

## Canonical option-event schema
Required/expected fields:
`underlying_symbol, option_symbol, event_time, receive_time, process_time, expiration_date, strike, option_type, trade_price, contracts, bid, ask, premium, aggressor, direction_sign, source, source_seq`.

`aggressor` may be `ASK`, `BID`, `MID/UNKNOWN`. Never fabricate aggressor classification when the quote context is unavailable.

## Canonical option snapshot schema
At minimum:
`timestamp/event_time, underlying_symbol, underlying_price, expiration_date or dte, strike, option_type, open_interest, volume, bid, ask, iv (when provider supplies it)`.

Provider IV/Greeks and ITM QUANT model IV/Greeks must retain separate provenance fields. Calculated Greeks are model outputs, not observed market fields.

## Source health states
Each adapter exposes one of:
- `LIVE`: current and within latency/staleness limits.
- `DEGRADED`: usable but latency, gaps or partial fields exceed normal tolerances.
- `STALE`: last value is too old for live authority.
- `FALLBACK`: value is coming from a documented secondary/model source.
- `OFFLINE`: unavailable.

The Quant Core may reduce confidence/coverage when data is degraded; a fallback must never silently masquerade as direct data.

## Quality checks
Adapters must validate:
1. monotonic/valid timestamps and duplicate handling;
2. positive prices/sizes where applicable;
3. bid <= ask when both are valid;
4. valid option strike, expiry and side;
5. stale age and event latency;
6. sequence gaps when the venue supplies sequence numbers;
7. symbol/contract mapping integrity;
8. no forward-looking fields in Replay.

## Causality and Replay
All data that can affect a decision must be queryable `asof`. Replay must expose only events/snapshots with `event_time <= asof` and must reproduce the same ordering contract as LIVE. Research/calibration is never allowed to inject future outcomes into the live state.

## Fallback policy
Fallbacks are explicit and tagged. Examples:
- model-derived IV when a valid option quote exists but provider IV is missing;
- structural price path only when archived Tape is unavailable;
- cached official macro series when live macro is not applicable.

Fallback data may support context but its provenance and staleness are visible to Auditor.

## Adding a new provider
A new provider integration is complete only when it has:
1. adapter → canonical schema;
2. source-health/staleness policy;
3. timezone/clock specification;
4. deduplication/sequence strategy;
5. causal Replay test;
6. quality/fallback disclosure;
7. fixtures and math/integration tests;
8. Auditor provenance entry.

## Multi-asset rule
No adapter may encode directional logic for a specific ticker. Instrument-specific contract metadata belongs in instrument metadata/configuration. The Quant Core consumes canonical fields and instrument metadata, not vendor/ticker branches.

## OPRA trade + NBBO causal pairing (v1.21)
Dealer microstructure must not classify a trade using a quote that occurred after the trade. The option stream keeps a bounded per-contract quote history and pairs every trade with the most recent quote satisfying:
`quote.event_time <= trade.event_time`.

The canonical enriched trade may include:
`bid_size, ask_size, quote_event_time, quote_receive_time, quote_age_ms, nbbo_synced, quote_quality, spread_position, classification_method`.

Classification authority is tiered:
1. `CAUSAL_NBBO_ASK/BID/MID` when a causal quote is sufficiently fresh;
2. `TICK_RULE` only as a lower-confidence fallback when the synchronized quote is missing/stale;
3. `UNKNOWN` when neither basis is defensible.

A REST snapshot quote is explicitly tagged `UNSYNCED` and receives limited aggressor confidence; it must not masquerade as tick-synchronized NBBO.

## Optional related-futures flow bridge
Dealer hedge confirmation accepts an optional canonical futures tape. No synthetic futures confirmation is generated when that feed is absent. A future adapter should expose at minimum:
`symbol, event_time, receive_time, process_time, price, size, signed_volume/aggressor_side, source, source_seq`.

The resulting hedge confirmation is consistency evidence only. It does **not** claim that an observed futures trade was executed by a particular dealer.

## Dealer-private data boundary
Public/broker option feeds generally do not reveal participant identity, true dealer books, account-level opening/closing flags, clearing inventory, or cross-venue hedges. ITM QUANT therefore uses the labels `ESTIMATED DEALER INVENTORY`, `DEALER FIELD ESTIMATE`, and `HEDGE PRESSURE ESTIMATE` unless a future authorized source actually supplies those private fields. The adapter layer must not infer or invent participant identities.

## Underlying-at-print synchronization
For Dealer Intelligence, the canonical option event should be joined to the most recent causal underlying trade (`underlying.event_time <= option.event_time`). ITM QUANT records source and age; if no fresh causal underlying trade exists, the option snapshot's underlying price remains an explicit fallback rather than being presented as synchronized spot.

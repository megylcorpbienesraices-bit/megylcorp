# ITM QUANT · Causality & Replay Specification

## Goal
LIVE, REPLAY and REVIEW must share one causal interpretation of market history. A delayed event is not allowed to move backward in logical market time simply because it arrived late.

## Three clocks
Each event stores `event_time`, `receive_time`, `process_time`. Event time orders market causality; receive/process times quantify transport/engine latency.

## Event unifier
The Causality Orderer receives canonical events from SIP/OPRA/other adapters and uses a bounded event-time reorder buffer. Equal event times are resolved deterministically with source sequence then event-type/source identifiers.

## Watermark
LIVE uses an explicit maximum-lateness allowance. Replay flushes the full selected historical set in deterministic event-time order. Buffer overflow is auditable and must never be silent.

## As-of contract
For any replay timestamp `t`, every consumer receives only data whose causal event time is <= `t`. Options snapshots, prints, Tape, overlays, model snapshots and related-market context all obey this rule.

## Determinism
Given the same stored events, configuration and code/model version, Replay should produce the same ordered stream and state outputs. Decision provenance stores the relevant version/source snapshot.

## Late events
Late events may update subsequent state after they enter the watermark, but may not rewrite a previously emitted LIVE decision as if the information had been known earlier. Research may study the effect separately.

## Tests
Tests intentionally scramble arrival order and confirm stable event ordering, as-of exclusion of future events, same-timestamp tie behavior and deterministic output across repeated runs.

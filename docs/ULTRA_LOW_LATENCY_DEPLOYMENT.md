# ITM QUANT · Ultra-Low-Latency Deployment

## Honest runtime tiers

1. **Fallback** — Python SIP/OPRA + Python causal ordering. Always available when Alpaca is configured.
2. **Transitional Rust** — existing Alpaca WebSockets decode in Python, then normalized ITMQ binary frames are forwarded over ZeroMQ PUSH/PULL to the independent Rust causal orderer. Rust publishes the ordered stream back to Python over PUB/SUB. This isolates ordering/backpressure but **does not turn Alpaca into a direct/HFT feed**.
3. **Direct institutional** — replace the Python forwarder with a provider-specific Rust adapter (Databento/PCAP/direct exchange). Quant Core contracts do not change.

## Local endpoints

- Python -> Rust ingress: `tcp://127.0.0.1:5554` (`ITM_RUST_INGEST_BIND/ENDPOINT`)
- Rust -> consumers ordered PUB: `tcp://127.0.0.1:5555`
- Rust Prometheus metrics: `127.0.0.1:9555`

## Start

1. Install Rust using rustup and install native ZeroMQ development/runtime support.
2. Run `scripts\BUILD_RUST_CAUSALITY.bat`.
3. Run `INICIAR_ULTRA_LOW_LATENCY.bat`.
4. `/api/nextgen/low-latency` must report both `rust_ingress` and `rust_causality`. `ACTIVE` is only valid after actual frames have crossed each boundary.

## Critical guarantees

- Python forwarding is non-blocking; a saturated/down Rust process cannot freeze Alpaca capture.
- Rust is **ingestion/causality authority only**. It cannot alter Scanner direction.
- The transitional path may expose the same observation on Python and Rust transports. Deduplication uses market observation keys (timestamp/price/size/exchange or contract/timestamp/trade-price/contracts), not transport sequence.
- `event_time`, `receive_time`, `process_time` and `source_seq` remain available for Replay/Audit.
- SQLite is not in the Dealer Inventory hot mutation path; it is an asynchronous archive/checkpoint.

## What this does NOT claim

- It does not make Alpaca nanosecond/direct-exchange data.
- It does not guarantee zero packet loss under operating-system/network failure.
- It does not claim HFT/co-location performance without direct feeds, tuned Linux/Windows networking, CPU affinity validation and dedicated hardware.

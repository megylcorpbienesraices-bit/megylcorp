# HFT / Low-Latency Infrastructure Readiness

ITM QUANT v1.22 introduces **readiness contracts and latency telemetry**. It does not pretend to be co-located or FPGA accelerated when the required hardware is absent.

## Event-time latency
The causal engine preserves event, receive and process timestamps. `/metrics` exposes local operational gauges and `/api/nextgen/readiness` exposes p50/p95/p99 event latency when measurable.

## Optional environment contracts
- `ITM_COLOCATION_REGION`: declares a real configured co-location/near-exchange region.
- `ITM_FPGA_ENABLED=1` or `ITM_FPGA_BRIDGE_URL`: declares an installed hardware feed bridge.
- `ITM_LATENCY_TARGET_MS`: local latency budget.

If these variables are absent, the UI reports **READY** or **UNAVAILABLE**, never ACTIVE.

## What still requires external infrastructure
True sub-millisecond exchange proximity, specialized NICs, FPGA decoding, direct exchange sessions and order-routing latency are infrastructure projects. Software hooks cannot manufacture those properties on a normal desktop.

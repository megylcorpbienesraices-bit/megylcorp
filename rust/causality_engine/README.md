# ITM QUANT Rust Causality Engine v1.24.4

Independent process for direct-feed ingest and deterministic event-time ordering.
It is deliberately isolated from Python so Python GC/GIL pauses cannot stop capture.

**Truth boundary:** the shipped source includes the orderer, binary wire contract and
ZeroMQ publisher. A real PCAP/UDP/direct-exchange decoder is provider-specific and only
becomes `ACTIVE` when credentials/hardware/feed adapters are configured.

The public wire format is an explicit little-endian header + MessagePack payload. We do
not send raw `repr(C)` memory or Python JSON. Bincode is reserved for optional Rust-local
spool files because it is not a stable Python/Rust network ABI.

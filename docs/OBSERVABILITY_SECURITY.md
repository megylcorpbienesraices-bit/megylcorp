# Observability & Security

## Local observability
- `/metrics`: Prometheus-compatible text metrics.
- `/api/nextgen/readiness`: capability/readiness truth table.
- Auditor persistent reports remain the long-horizon technical record.

## Security posture
The packaged deployment is local-only, disables the legacy public login flow, sends CSP/security headers and does not expose a broker execution API.

External deployment requires host/network controls, TLS, secrets management, process isolation and account-level execution kill switches. ITM QUANT's signal-export layer is not a substitute for broker/EA risk controls.

# Analyst View · v1.40.3

La vista por defecto está diseñada para el analista, no para el operador del backend.

## Visible

- dirección/accionabilidad del Scanner
- fuerza estructural
- zona, T1/T2 e invalidación
- Gamma/Delta/OI y estructura por strike
- Flow, Dealer/Hedge, volatilidad y derivados
- TRACE, niveles, premarket, macro y contexto Dow
- alertas de calidad sólo cuando cambian la posibilidad de usar el análisis

## Interno

- nombres y rutas de providers
- estados de suscripción e integración
- health matrices y diagnostics
- latencia, renderer/backend, queues/retries/watchdogs
- HFT/FPGA/COLO/QPU/XR readiness
- stores, persistencia y observabilidad

El modo interno se habilita manualmente agregando `?internal=1` a la URL local. No existe un botón visible en Analyst View.

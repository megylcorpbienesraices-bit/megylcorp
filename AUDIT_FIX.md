# ITM QUANT · revisión aplicada sobre df2ca9d

## Corregido en esta pasada

- QFLOW: marca visual inequívoca `BUY` / `SELL` / `UNKNOWN` junto al importe.
- QFLOW: flecha completa para compra/venta; UNKNOWN conserva rombo neutro.
- TRACE / FLUJO / NET DRIFT: una sola implementación de marca en `itmq_core.js`.
- Eliminados alias muertos dejados por la unificación anterior.
- Interval Map: una celda RAW medida no nula ya no desaparece visualmente sólo por caer bajo el suelo de concentración; los huecos siguen siendo huecos en la máscara RAW.
- Inventario y trazabilidad de tests resincronizados a 2.558 casos / 171 ficheros.
- Documentación de estado corregida: red disponible; LIVE bloqueado por credencial/proceso, no por conectividad.

## Verificación ejecutada aquí

- 132 pruebas focalizadas: PASS.
- Gates v1.56 completos con PYTHONPATH correcto: 204 PASS.
- Suites v1.55 + v1.56: 356 PASS.
- Trazabilidad/release: 82 PASS.
- Sintaxis Python: compileall PASS.
- Frontend: `lint_frontend.sh` llegó a `SINTAXIS OK (5 módulos)`; el comando completo excedió la ventana del entorno.
- Suite completa: intentada dos veces, excedió la ventana de ejecución del entorno antes de terminar; por eso NO se declara full-suite certificada en esta copia.

## Monte Carlo

No se cambió el modelo. La suite focalizada que contrasta GBM, esperanza, varianza lognormal, N(d2), primer paso continuo/Brownian bridge, determinismo, error estándar, sensibilidad y convergencia continúa pasando.

## Pendiente LIVE

Requiere `QUANTDATA_API_KEY` y el terminal activo para cerrar con datos reales:

`python scripts/verify_live_quantdata.py --cierre`

Eso debe validar BUY/SELL real, Interval Map celda a celda, walls y Dark Pool en los ocho activos.

## Packaging

El preflight ya llega al veredicto real. En este entorno queda bloqueado por toolchain/dependencias:

- Python 3.13.5 != 3.12.14
- pip 25.1.1 != 26.2.1
- pip-audit ausente != 2.10.1
- ruff ausente != 0.16.7
- Rust/Cargo ausente != 1.98.1
- Docker ausente
- pyzmq 27.1.0 != 27.2.0

Esta copia NO se presenta como ZIP certificado.

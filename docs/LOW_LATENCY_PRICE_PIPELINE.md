# ITM QUANT v1.40.2 · Ultra Low Latency Price Pipeline

## Objetivo
Mover el precio observado al píxel en <= 1 frame de render local, sin tocar autoridad cuantitativa.

## Ruta LIVE
`provider callback -> PRICE_TICK_FABRIC -> condition wakeup -> 4 ms binary microbatch -> browser -> rAF coalescing -> Lightweight Charts series.update()`

## Invariantes
- Scanner sigue siendo la única autoridad direccional.
- No se cambian fórmulas, señales, riesgo ni ejecución.
- `setData()` es bootstrap/reconciliación; el hot path usa `update()`.
- Gamma/Delta/OI no se recalculan por cada tick de precio.
- El feed observado conserva event-time; no se sintetizan ticks ni velas.

## Telemetría
`window.ITMQLiveLatency.snapshot()` expone p95 navegador->update, navegador->paint y event-time->browser para diagnóstico.

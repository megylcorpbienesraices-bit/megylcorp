# ITM QUANT · Instant Boot Architecture

## Problema resuelto
El estado global `ready` hacía que la interfaz pareciera congelada hasta que terminaba una hidratación cuantitativa amplia. A la vez, la carga inicial podía repetir la consulta de cadena cuando el rango sigma fresco difería del rango preliminar.

## Contrato HOT → CORE → ENRICHMENT

```text
BOOT
  ├─ HOT: symbol + price stream + cached/session price history
  │      └─ TRACE / CHART TW pueden hacerse visibles
  ├─ CORE: option contracts + chain (concurrentes)
  │      └─ Greeks → Gamma/Delta → Expiry → Scanner
  │             └─ publicar QUANT_CORE_READY
  └─ ENRICHMENT: Research / diagnostics / secondary analytics
         └─ READY final
```

## Window hint
`app/core/instant_boot.py` guarda por símbolo/expiry el último strike-window validado. El hint es una optimización de scheduling, no una fuente de verdad. La cadena fresca sigue recalculando la ventana sigma; si la diferencia supera el umbral del motor, se permite una segunda consulta y se actualiza el hint.

## Concurrencia
Una vez conocido el spot, definiciones de contratos y snapshot/chain pueden solicitarse concurrentemente. La fusión posterior conserva las mismas validaciones de contrato, IV/Greeks y calidad que antes.

## CORE-FIRST
El worker aislado puede publicar el núcleo utilizable sin esperar módulos secundarios. La adopción está condicionada por símbolo y epoch actuales; un resultado de un activo anterior se descarta. La publicación CORE-FIRST no salta Data Quality ni Scanner: solo evita que Research/diagnósticos secundarios bloqueen su visibilidad.

## Semántica UI
Mientras la cadena no existe, Expiry Intelligence no debe afirmar `0` vencimientos ni `NO 0DTE`; usa estados de conocimiento explícitos. El precio y la memoria tienen estados independientes del Quant global.

## Medición recomendada en PC LIVE
Registrar por arranque y cambio de activo:
- `t_price_visible_ms`
- `t_trace_history_ms`
- `t_contracts_chain_ms`
- `t_greeks_ms`
- `t_scanner_core_ready_ms`
- `t_full_enrichment_ms`
- número de `sigma_refetch_performed`
- P50/P95/P99 por activo y proveedor

La mejora se evalúa con estas métricas; no se declara una latencia fija sin medir los feeds reales.

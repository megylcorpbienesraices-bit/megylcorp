# Unified Market Intelligence — Especificación

## Flujo autoritativo

```text
PROVEEDORES FIRST-CLASS
  Alpaca · tastytrade · Quant Data · adapters futuros
          ↓
NORMALIZACIÓN + PROVENANCE
          ↓
TEMPORAL TRUTH / TIME-SYNC
 event_time · receive_time · source_seq · freshness · latency · quality
          ↓
QUALITY ARBITRATION
          ↓
VERSIONED MARKET STATE
          ↓
QUANT FEATURE ENGINE
 Gamma/GEX · Delta/DEX · Vanna · Charm · OI · Volume · Volatility
 Derivatives Flow · Ecosystem · Structural · Expiry
          ↓
        EVENT BUS / CACHE
      ↙       ↓        ↘
 Scanner    TRACE     CHART TW
              ↘       ↙
               Auditor/Replay
```

## Regla `single compute → multi consumer`
Una feature cuantitativa calculada para un `state_id` se distribuye a los consumidores. El renderer no vuelve a calcular la matemática del Quant Engine. Los consumidores pueden cambiar presentación, ventana, zoom, perfil o capa visible sin alterar la autoridad matemática del estado fuente.

## Verdad temporal
Un dato no es utilizable solo porque haya llegado recientemente. Se conserva el momento del evento y el momento de recepción. Para pricing cross-provider, v1.25.17 usa una cohorte de event-time acotada; observaciones fuera de ventana no entran en el mismo composite. Causality Runtime conserva el diagnóstico de orden/watermark para Replay.

## Salud granular
El estado se mide por `provider + channel`. Una fuente puede tener Quote LIVE y OI stale simultáneamente. La degradación de un canal no degrada automáticamente todos los canales del mismo proveedor.

## Derivados
`OBSERVED_PROVIDER_NATIVE` y `ITM_MODEL` son namespaces conceptualmente separados. ITM no reverse-engineerea fórmulas privadas. El motor puede comparar signo/consistencia y, cuando exista una normalización validada, construir features comparables sin promediar magnitudes de unidades diferentes.

## Vencimientos
La fecha de vencimiento es la verdad. Aliases externos como `zero` y `one` nunca significan automáticamente 0DTE y 1DTE. Los buckets operativos se construyen dinámicamente con fechas reales.

## Render y transporte
- Control/diagnóstico pequeño: JSON.
- Histórico grande: NDJSON progresivo.
- Tick LIVE denso: ITMT fixed binary.
- Surface: ITMS Float32 binary.
- LIVE genérico: MessagePack/ITMQ binary seleccionado por benchmark.
- Canvas/WebGL/WebGPU son capas de presentación; la ausencia de WebGPU debe degradar renderer, no matemática.

## Autoridad
Scanner conserva la síntesis direccional final. Structural/Proximity/Expiry/Derivatives/Temporal Truth son contexto, calidad o evidencia. Probability/EV solo se presentan como probabilidad cuando la Calibration causal lo soporta.

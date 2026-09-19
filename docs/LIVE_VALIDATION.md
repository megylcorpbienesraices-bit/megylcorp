# LIVE Validation

## Qué significa “SIN DATO”

`SIN DATO` no significa que la información no exista en Internet. Significa que **ITM QUANT no recibió un dato programático autorizado y actual** para esa fuente en ese análisis.

El programa local no comparte automáticamente el navegador de ChatGPT ni puede convertir una página web manual en un feed. Para usar datos de forma automática necesita una de estas rutas:

- API + credencial/entitlement;
- bridge local autorizado;
- export del usuario;
- fuente primaria ya conectada como Alpaca SIP/OPRA.

Si ninguna existe, la fuente pesa 0. Nunca se rellena con un proxy oculto.

## DIA: qué puede funcionar sin proveedor adicional

Cuando Alpaca LIVE está configurado, ITM puede usar la misma conexión SIP para obtener **XLI y XLF** como breadth sectorial bajo demanda al pulsar ANALIZAR PREMARKET. No hace falta una segunda página para esos ETF.

## DIA: qué sigue requiriendo otra conexión

- **YM:** CME o proveedor autorizado que escriba el bridge CME.
- **DJX:** Cboe/direct index autorizado.
- **VXD:** Cboe/direct volatility source autorizado.
- **VIX:** Cboe/direct volatility source autorizado.
- **Quant Data:** `QUANTDATA_API_KEY` válida; participa en igualdad con Alpaca y tastytrade en los canales de opciones (ver `app/core/provider_parity.py`).
- **Bookmap:** addon/bridge local.

## Flujo y Dealer antes de 9:30 NY

Antes de la apertura regular, el flujo OPRA de la sesión y Dealer/Hedge LIVE todavía no tienen por qué existir. Por eso v1.14.7 los etiqueta `NO APLICA PREMARKET` en vez de `SIN DATO/ESPERANDO` cuando el mercado está realmente en PREMARKET. Después de la apertura se activan normalmente.

## LIVE Validation

La pantalla de Research muestra cuánto historial real existe. Los objetivos 8 sesiones y 120 señales son mínimos iniciales de calibración, no garantías de edge.

Un modelo solo progresa cuando hay observaciones reales y suficientes. DEMO no se utiliza como evidencia de efectividad.

## Partial Pooling

La mezcla Global/Expiry se trata como un modelo nuevo:

1. los componentes se entrenan con historia;
2. un bloque OOS temporal selecciona el peso del scope;
3. un bloque temporal posterior mide el blend final;
4. se reportan Brier/Log Loss del **blend**;
5. si no supera base rate con el margen requerido, no se promueve.

Así el modelo que toma la decisión debe demostrar su propia calidad, no heredar la métrica de uno de sus componentes.

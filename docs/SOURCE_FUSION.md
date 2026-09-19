# ITM QUANT — Data Fusion & Source Intelligence

## Principio

Las fuentes nuevas aportan materia prima al motor; no crean más paneles de trading.
La salida visible sigue siendo simple. Cada observación conserva fuente, estado,
timestamp, edad, rol y si es observada, calculada, inferida o propietaria.

## Jerarquía

1. **Primario LIVE**: Alpaca SIP/OPRA para activos/ETF y cadena de opciones soportada.
2. **Exchange/futuro**: CME por bridge autorizado para YM/ES/NQ/GC/VX.
3. **Índice/opciones de contraste**: Cboe All Access cuando existen OAuth + entitlement.
4. **Paridad de opciones (v1.41.0)**: Alpaca, tastytrade y Quant Data participan como pares en cada canal de opciones. Ninguno tiene rango fijo ni queda restringido a confirmar; gana el canal la observación de mayor calidad del ciclo. La estructura publicada la calcula ITM QUANT.
5. **Validación secundaria**: ChartExchange API, OptionCharts export, Market Chameleon export.
6. **Ejecución**: Bookmap bridge para CVD/profundidad/absorción; no crea niveles de opciones.
7. **Propietario importado**: SpotGamma export; no se trata como observación independiente.
8. **Noticias**: Reuters solo mediante bridge licenciado; contexto/event risk, nunca señal autónoma.

## Regla DIA

Los niveles PREMARKET visibles nacen de DIA. YM/DJX/VXD/VIX/sectores/modelos externos
pueden confirmar, contradecir o detectar discrepancias. El ajuste externo sobre la
fuerza queda limitado a ±8 puntos y requiere al menos dos confirmaciones utilizables.
La dirección no puede nacer de las fuentes externas.

## Ecosistemas por activo

- DIA → YM / DJX / VXD,VIX / XLI,XLF / Bookmap YM.
- SPY → ES / SPX / VIX / XLF,XLK.
- QQQ,TQQQ → NQ / NDX / VXN,VIX / XLK,SOXX.
- GLD,GDX → GC + relación GLD/GDX.
- AAPL,MSFT → NQ/NDX + QQQ/XLK.
- NVDA → NQ/NDX + QQQ/SOXX.
- META → NQ/NDX + QQQ/XLC.
- AMZN,TSLA → NQ/NDX + QQQ/XLY.
- VXX,VIX → VX/VIX + SPY.

## Activación

Fuentes externas auxiliares se consultan por red solo cuando el usuario pulsa
**ANALIZAR PREMERCADO**. El refresco normal no hace llamadas ocultas a proveedores.
CME/Bookmap/Reuters leen únicamente bridges autorizados locales. Si no están
configurados, `SIN DATO`/`SOURCE PENDING` y peso cero.

SPX/NDX/VIX siguen visibles como activos pendientes de motor primario completo. La
presencia de un validador Cboe no se presenta falsamente como una cadena principal
LIVE completa.

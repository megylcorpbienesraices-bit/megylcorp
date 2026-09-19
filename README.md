# ITM QUANT · MULTI ASSET

## v1.42.7 · Net Drift oficial, Dark Pool directo y Quant Data como proveedor

- **La causa raíz era el lector, no el proveedor.** Quant Data devuelve un mapa
  `{época: fila}`, no una lista; `_rows` sólo recorría listas y devolvía `[]` ante
  toda respuesta válida. Eso dejaba sin datos a `net-drift`, a `net-flow` —y con él
  a QFLOW, que nunca llegaba a activarse—, a `exposure-by-strike/-expiration` y a
  cuatro series más. Se añaden `_keyed_rows` y `_ts_iso`; **la vía de lista sigue
  siendo la primera**.
- **NET DRIFT OFICIAL.** Fuente única: `POST /v1/options/tool/net-drift`. No se
  reconstruye con GEX, DEX, Net Flow, QFLOW ni fórmulas propias. Se conservan los
  **siete campos** del endpoint, se respeta el signo del proveedor (`call + put`, no
  `call − put`) y se construye la curva acumulada de la sesión. El acumulado
  publicado es **matemáticamente idéntico** a sumar la respuesta cruda:
  `certify_against_raw()` lo comprueba punto a punto.
- **El bucket abierto se marca y no se suma dos veces.** La curva se reconstruye
  entera en cada ciclo; un instante repetido gana el último.
- **Cuatro elementos** en la lectura: CALL acumulado, PUT acumulado, precio sobre el
  **mismo eje temporal**, y subgráfico de **volumen neto CALL/PUT**. Al pulsar un
  punto se listan los trades del Order Flow de ese minuto.
- **Dentro de `FLUJO DE ÓRDENES`**, con un conmutador `Cinta / Net Drift`. No se crea
  ninguna sección nueva.
- **SIN DATOS, nunca una curva de ceros.** Seis estados diferenciados; los acumulados
  viajan en `None` cuando no hay dato.
- **Universal.** Ningún ticker fijado en código: misma respuesta, misma curva en DIA,
  SPY, QQQ, IWM y AAPL.
- **DARK POOL directo.** `dark-flow` (nuevo), `dark-pool-levels` y `equity-prints`
  como fuente primaria; la clasificación por venue queda como auditoría, con su
  discrepancia publicada en puntos porcentuales y el origen de cada cifra a la vista.
- **Exposure alimenta EXPOSICIÓN** por primera vez: se lee la forma real
  `data[TICKER].exposureMap[vencimiento][strike]`.

No se modifican fórmulas, pesos del Scanner ni autoridad direccional.

Detalle en `CHANGELOG_v1.42.7.md`.


## v1.42.6 · Capa QFLOW y universalidad multi-activo

**Quant Data no publica un "QFLOW"**: su documentación cubre `net-flow`,
`net-drift` y `order-flow`, pero no existe ese endpoint ni una fórmula oficial para
un nivel horizontal. El dato viene del proveedor; **el nivel QFLOW lo calcula ITM
QUANT a partir de él**.

- **La causa raíz estaba en el normalizador.** Leía `netCallPremium`/`netPutPremium`
  y Quant Data publica **`callSum`/`putSum`**; y **descartaba `stockPrice` entero**,
  que es el precio del subyacente en cada intervalo — sin él no hay nivel posible.
  Además publicaba `0.0` cuando faltaba el campo neto, convirtiendo una respuesta
  válida en una serie plana.
- **QFLOW · serie.** Línea de prima neta **acumulada** en el carril que ya existe,
  **sin sustituir las barras**: barras = qué pasó en este intervalo; línea = hacia
  dónde se ha inclinado la sesión entera.
- **QFLOW · nivel.** Precio donde se concentra la prima, ponderado por **|prima|**
  (no por el neto: 2M en calls y 2M en puts dan neto cero pero son un precio con
  enorme actividad) y con decaimiento de 90 min para que no quede clavado donde el
  dinero ya no está. No es strike dominante, ni Net Drift, ni Gamma Center, ni Zero
  Gamma.
- **Concentraciones** marcadas sobre el precio y reflejadas en TOTAL, cerrando la
  cadena evento → QFLOW → precio → nivel.
- **Se acabó la excepción de DIA.** `_target()` usaba una lista blanca y **toda
  acción** caía a `direct=False` pese a consultar su propio ticker, degradando su
  evidencia a canal de contexto. Ahora la regla es universal: la evidencia es directa
  cuando se consulta el ticker del propio instrumento. Verificado en DIA, SPY, QQQ,
  IWM, AAPL, NVDA, TSLA, MSFT y GLD.
- **Seis estados diferenciados** (`DATA_OK`, `NO_PROVIDER_DATA`, `FILTERED_ALL`,
  `PROVIDER_ERROR`, `PARSER_ERROR`, `STALE`). Un fallo de datos nunca se publica
  como `$0.0`.

No se modifican fórmulas, pesos del Scanner ni autoridad direccional.

Detalle en `CHANGELOG_v1.42.6.md` (release retirada).


## v1.41.2 · Causa raíz de los paneles vacíos

Con Alpaca y tastytrade en LIVE seguían apareciendo paneles sin datos. Una sola
causa detrás de los cuatro paneles de TRACE, otra detrás de Quant Data:

1. **La puerta de publicación apagaba la pantalla entera.** Al retener la
   estructura por frescura devolvía una carcasa vacía, sin motivo y sin precio.
   Ahora retiene sólo lo estructural, publica el precio observado y declara
   `publication_blocked_motive`; la cabecera muestra `ESTRUCTURA RETENIDA`.
2. **La cadencia no cabía en el plan.** Nueve endpoints cada 15 s son 2160
   peticiones/hora contra un plan de 240. El intervalo se deriva ahora de la
   cuota real que reporta el proveedor y los endpoints se escalonan por velocidad
   de cambio: de 2160 a menos de 200 peticiones/hora.
3. **Deadlock** en el presupuesto de cuota (`snapshot()` pedía dos veces un lock
   no reentrante) que colgaba el proceso al consultar la cobertura.

Ninguna de estas causas es de rendimiento: un VPS no las habría cambiado.

Detalle en `CHANGELOG_v1.42.1.md`.


## v1.41.1 · Datos en vivo recuperados en todos los paneles

Correctivo de disponibilidad. Cuatro causas reales de paneles en blanco con los
tres proveedores operativos:

1. **TRACE congelado** por una variable referenciada sin declarar en el render.
   No era error de sintaxis, así que sólo aparecía en ejecución. Se incorpora
   `no-undef` como contrato del proyecto (`scripts/lint_frontend.sh`).
2. **Sin velas** cuando la fabric de ticks estaba fría: el camino normal no
   recurría al bootstrap de sesión y dejaba sin gráfico a TRACE, FLUJO, RESUMEN y
   DARK POOL a la vez.
3. **Cuota de Quant Data agotada**: los dos carriles pedían los mismos nueve
   endpoints. Ahora el carril de páginas reutiliza los payloads del motor y
   ambos comparten presupuesto, con margen reservado para la estructura.
4. **Estado de proveedores mal leído**: tastytrade se evaluaba con claves
   inexistentes y la calidad por canal se leía fuera de su anidamiento.

Nuevo: `/api/terminal/diagnostics` y la tabla al pie de FUENTES dicen, panel por
panel, si hay dato, de dónde viene y, si falta, por qué.

Detalle en `CHANGELOG_v1.42.1.md`.


## v1.41.0 · Terminal de analista + paridad de proveedores

La raíz `/` sirve una terminal centrada en el resultado del motor: RESUMEN, TRACE,
FLUJO DE ÓRDENES, EXPOSICIÓN, INTERÉS ABIERTO, VOLATILIDAD, ESTADÍSTICAS, DARK POOL
y FUENTES. El dashboard anterior sigue disponible en `/legacy`.

TRACE se dibuja en tres paneles con un eje de precio compartido (perfil por strike a
cada lado, heatmap + precio + niveles en el centro). El panel de flujo apila precio,
agresor, flujo neto y prima total sobre el mismo reloj. Todo pasa por un motor de
render propio con un único bucle de animación, interpolación entre snapshots y zoom
compartido.

Alpaca, tastytrade y Quant Data participan como **pares en igualdad** en los canales
de opciones: ninguno está limitado a confirmar a otro y el nombre del proveedor no
otorga peso. Gana el canal la observación de mayor calidad del ciclo
(`app/core/provider_parity.py`). Las siete páginas integradas de Quant Data
(30 herramientas) se recolectan en un carril propio, separado del que hidrata el motor.

Detalle completo en `CHANGELOG_v1.42.1.md`.

# ITM QUANT v1.40.8 · PRE-VPS

## v1.40.8 · Quant Data Net Drift + Dynamic ETF Universe

Net Drift visible usa los buckets observados de Quant Data (CALL/PUT/NET), con precio cero rechazado y sin mezclar DEX/GEX bajo el mismo nombre. El buscador incorpora un catálogo ETF dinámico descubierto desde Alpaca + Quant Data; los instrumentos con cadena propia confirmada habilitan análisis completo y el resto permanece en mercado directo con derivados capability-gated.


v1.40.2 mantiene el motor cuantitativo y la autoridad del Scanner de la línea v1.40, y optimiza exclusivamente la ruta LIVE de precio/renderizado para reducir latencia visible.

## v1.40.8 · Runtime/UI Corrective Release

Flujo Inusual usa un terminal sincronizado de cuatro paneles: **PRECIO + burbujas**, **AGRESOR**, **TOTAL** y **NET FLOW**. Call Wall/Put Wall reutilizan la matemática estructural nativa; Gamma Flip solo se publica cuando existe un cruce real de `NetGEX(S)=0`. Anomalías & Momentum SHADOW queda siempre activa, y continúa sin autoridad sobre Scanner ni ejecución.

## v1.40.5 · Native Options Structure

GEX/DEX, Gamma Flip/Zero Gamma, majors por OI/volumen y presión estructural se calculan nativamente en ITM QUANT. Quant Data queda como corroboración externa normalizada; Scanner conserva la única autoridad direccional. Se retiró la dependencia analítica externa anterior, junto con credenciales, rutas, UI, Data Lake, scripts y tests específicos.


## v1.40.4 · Quant Data Options Intelligence

Quant Data se integra como una lane REST interna de inteligencia de opciones. Alimenta la fusión semántica con GEX/DEX/Vanna/Charm, Net Drift/Net Flow, Gamma Migration, IV Rank y Max Pain sin entrar en la ruta de precio/TRACE ni mostrar provider diagnostics en Analyst View. DIA/XLI/XLF usan evidencia directa del mismo instrumento; YM/MYM/DJX/VIX/VXD sólo reciben contexto normalizado del ecosistema DIA, nunca strikes o exposición cruda como proxy.

## v1.40.3 · Analyst View

La interfaz normal muestra sólo información útil para análisis. Diagnóstico de providers, infraestructura, latencia y backend permanece interno y se habilita únicamente con `?internal=1`. Ver `CHANGELOG_v1.40.3.md` y `docs/ANALYST_VIEW.md`.


## Contrato principal

`PROVIDER → PRICE_TICK_FABRIC → BINARY WS → CANDLE AGGREGATOR → LIGHTWEIGHT CHARTS`

La arquitectura cuantitativa permanece separada de la vía de presentación de baja latencia. El Scanner conserva `SOLE_DIRECTIONAL_AUTHORITY`; el transporte visual no puede modificar BUY/SELL, riesgo ni cálculos.

## v1.40.2 · Low Latency Price Pipeline

- WebSocket de ticks event-driven con microbatch de 4 ms, sin polling fijo 25/75 ms.
- lectura incremental O(ticks nuevos) del fabric, con failover de proveedor invalidado inmediatamente ante challenger fresco;
- arranque del stream binario con un único tick actual, sin replay masivo de historia ya cargada por REST;
- nueva vela LIVE insertada realmente en el `RingBuffer`;
- Lightweight Charts usa `series.update()` en LIVE y reserva `setData()` para bootstrap/reconciliación;
- precio desacoplado de Gamma/Delta/OI en el hot path;
- cola de ticks coalescida al frame de pantalla y telemetría de browser→paint p95.

Ver `CHANGELOG_v1.40.2.md`, `QUANT_ENGINE_AUDIT_v1.40.2.md`, `VALIDACION_PRE_VPS_v1.40.2.md` y `docs/LOW_LATENCY_PRICE_PIPELINE.md`.
# ITM QUANT MULTI ASSET · v1.42.7 — Net Drift oficial, Dark Pool directo y Quant Data como proveedor de las secciones existentes

## Matemática del motor

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.**

Net Drift **no se calcula**: se lee. La única autoridad del dato es el endpoint
oficial del proveedor, y lo único que ITM QUANT añade es una **suma corrida** sobre
los valores que el proveedor ya publica por intervalo. Nada de GEX, DEX, Net Flow,
QFLOW, Gamma ni Delta Exposure entra en ese camino, ni siquiera como corrección.

---

## 1 · La causa raíz: el lector no entendía la forma de las respuestas

Quant Data **no devuelve listas**. Devuelve un objeto cuya clave es la propia
coordenada —el instante en milisegundos para las series, el strike para los
perfiles— y cuyo valor es la fila:

    {"data": {"1758205800000": {"netCallPremium": 1200000, "stockPrice": 461.2, ...}}}

El extractor `_rows` sólo recorría listas, así que devolvía `[]` ante **toda
respuesta válida** con esta forma. Y una lista vacía es indistinguible de "el
proveedor no tiene datos".

Consecuencias que estaban en producción y no se veían como un fallo de lectura:

| Herramienta | Lo que se veía | Lo que pasaba |
|---|---|---|
| `net-drift` | sin datos | respuesta completa, descartada al leerla |
| `net-flow` | sin datos → **QFLOW nunca se activaba** | ídem |
| `exposure-by-strike` / `-by-expiration` | Exposure sin cobertura | forma `exposureMap`, no lista |
| `max-pain-over-time`, `oi-over-time`, `volatility-drift`, `stock-price-over-time` | sin datos | ídem |

Se añade `_keyed_rows`, que reinyecta la clave como un campo más, y `_ts_iso`, que
convierte la época en milisegundos a ISO-8601. Sin esa conversión,
`datetime.fromisoformat("1758205800000")` no fallaba de forma ruidosa: **descartaba
la fila en silencio** aguas abajo.

**La vía de lista sigue siendo la primera.** El mapa es el respaldo. Ninguna
respuesta que funcionaba cambia de comportamiento.

---

## 2 · NET DRIFT OFICIAL

### Autoridad del dato

    POST /v1/options/tool/net-drift

No se reconstruye mediante GEX, DEX, Net Flow, fórmulas propias ni aproximaciones.
`app/core/net_drift.py` no importa `qflow`, ni `engine`, ni `trace_analytics`, ni
`precision_engine`; un test lo verifica sobre el propio código fuente.

### Los siete campos, enteros

`norm_time_series` colapsaba la fila a `value`/`call`/`put` y tiraba cuatro de los
siete campos oficiales. Sin los volúmenes netos no hay subgráfico de volumen; sin
las primas a precio medio no hay con qué contrastar lo pagado contra el punto medio
del mercado. `norm_net_drift` conserva los siete:

    netCallPremium  netPutPremium  netCallVolume  netPutVolume
    midMarketCallPremium  midMarketPutPremium  stockPrice

### Signo

`netPutPremium` y `netPutVolume` llegan **ya firmados** por el proveedor. No se les
aplica ningún signo adicional: la prima neta del intervalo es `call + put`, no
`call − put`. Restar un número que ya es negativo invertiría la dirección de la
sesión entera.

### La curva acumulada

La API entrega **por bucket**, no acumulado. ITM QUANT ordena por instante y acumula.
Se publican las cuatro cosas que pide la lectura oficial:

1. **línea CALL** — Net Call Premium acumulado
2. **línea PUT** — Net Put Premium acumulado
3. **precio del activo** sobre el **mismo eje temporal** (el `TimeLink` compartido
   con la cinta: es el mismo reloj, no dos que coinciden)
4. **subgráfico de volumen neto CALL/PUT**

### El bucket todavía abierto

El último bucket sigue formándose y el proveedor lo republica con valores mayores en
cada refresco. Por eso **la curva se reconstruye entera en cada ciclo** desde la
respuesta cruda, en vez de sumar lo nuevo sobre lo ya acumulado: sumar
incrementalmente contaría ese bucket tantas veces como refrescos hubiera. Dentro de
una misma respuesta, un instante repetido **gana el último, no se suma**.

El bucket abierto se marca en el gráfico con un punto hueco y se publica además el
acumulado **cerrado**, el que ya no puede cambiar.

### Certificación contra la respuesta cruda

`certify_against_raw()` recalcula la suma desde las filas del proveedor —sin pasar
por la curva— y la contrasta punto a punto. Sobre 90 buckets sintéticos:

    puntos comparados        90
    desvío relativo peor     5.3e-08
    desvío absoluto peor     5.0e-05  (= el cuanto de redondeo de la publicación)

Es decir: el acumulado publicado es **matemáticamente idéntico** a sumar el crudo, y
lo único que queda es el redondeo a 4 decimales con el que se publica la serie.

### Sin datos es SIN DATOS

Seis estados diferenciados (`DATA_OK`, `NO_PROVIDER_DATA`, `FILTERED_ALL`,
`PROVIDER_ERROR`, `PARSER_ERROR`, `STALE`). Cuando no hay curva, los acumulados
viajan en `None` y la interfaz escribe **SIN DATOS**. Una recta en cero y una sesión
realmente equilibrada se dibujan igual, y esa ambigüedad es peor que un hueco
declarado.

### Universal, sin ningún ticker en el código

El símbolo sale siempre del estado y viaja sólo como trazabilidad: no condiciona
ningún cálculo, ningún umbral y ninguna rama. Verificado con la misma respuesta
sobre DIA, SPY, QQQ, IWM y AAPL: curvas idénticas punto a punto.

### Selección de punto → trades que lo produjeron

Al pulsar sobre la curva se fija el instante y se listan las impresiones del Order
Flow de ese mismo minuto, ordenadas por prima. El enlace es directo porque las dos
series viven en el mismo reloj y en buckets del mismo tamaño.

### La tarjeta de RESUMEN que decía NET DRIFT y no lo era

Había ya una tarjeta titulada `NET DRIFT · CALL / PUT ACUMULADO` en RESUMEN. **No
dibujaba Net Drift**: acumulaba la cinta propia de opciones y la presentaba con el
nombre de una magnitud que sólo publica el proveedor. En pantalla era
indistinguible de la real.

Arrastraba además un error de signo: `premium * (direction || 1)`. Cuando el
agresor no está clasificado, `direction` vale `0`, y `0 || 1` vale `1` en
JavaScript — así que **toda la prima sin clasificar se contaba como compra**. La
curva se inclinaba sola a comprador justo cuando la cinta llegaba sin cotización,
que es precisamente cuando peor se lee.

Ahora esa tarjeta lee el mismo bloque oficial que el panel de flujo, y cuando el
proveedor no entrega escribe SIN DATOS con su estado.

### Dónde vive

**Dentro de `FLUJO DE ÓRDENES`. No se crea ninguna sección nueva.** La barra de la
sección gana un conmutador `Cinta / Net Drift`; los cinco carriles de la cinta siguen
intactos. Un test comprueba que no existe ninguna vista `netdrift` en la terminal.

---

## 3 · DARK POOL con Quant Data como fuente directa

Hasta v1.42.6 la sección sólo sabía de dark pool lo que podía **deducir** del campo
`venue`/`exchange` de la cinta de otro proveedor. Esa vía es indirecta por
construcción y se cae por tres sitios a la vez —que el proveedor publique el venue,
que lo publique en cada print, y que el código se interprete bien—; cuando alguno
fallaba, la sección mostraba 0 en todos los activos sin poder decir por qué.

Ahora la autoridad es Quant Data, que **ya sabe** qué ejecución fue off-exchange:

    POST /v1/equities/tool/dark-flow          volumen oscuro por intervalo   (NUEVO)
    POST /v1/equities/tool/dark-pool-levels   niveles con volumen oscuro
    POST /v1/equities/tool/equity-prints      cada impresión individual

La clasificación por venue **se conserva**, como fuente adicional y de auditoría. El
bloque `audit` publica las dos medidas y su discrepancia en puntos porcentuales, y
la sección dice **cuál de las dos** está sosteniendo el número: no miden lo mismo —el
proveedor mide sobre todo el volumen de la sesión; el venue, sólo sobre los large
prints que la cinta dejó ver—, así que publicar la cifra sin su origen la hacía
inutilizable.

Otros arreglos de la sección:

- **La columna ACCIONES deja de ser fantasma.** `dark-pool-levels` sí publica
  `shares`; las zonas propias no, y ahí va vacío. Nunca un 0 que se lea como "no
  hubo acciones".
- **Columna ORIGEN** en la tabla de zonas: `QUANT DATA` o `VENUE`.
- **Ocho tarjetas en una rejilla de cuatro**: dos filas completas en vez de una fila
  a medias. La comprobación ya no fija un número de tarjetas, sino que exige que sea
  múltiplo del número de columnas declarado.
- `QUANT_DATA_SIN_RESPUESTA` como motivo propio, distinto de `NO_PRINTS`.

---

## 4 · Quant Data no crea secciones: alimenta las que ya existen

| Página del proveedor | Sección de ITM QUANT |
|---|---|
| Dashboard | RESUMEN |
| Flow Analysis | FLUJO DE ÓRDENES |
| Exposure | EXPOSICIÓN |
| Dark Pool / Equities | DARK POOL |
| Statistics | ESTADÍSTICAS |
| Open Interest | INTERÉS ABIERTO |
| Volatility Analysis | VOLATILIDAD |

La página **Exposure** era la que peor estaba: sus normalizadores esperaban listas y
la respuesta real es `data[TICKER].exposureMap[vencimiento][strike]`, así que no
alimentaba nada y su cobertura se leía como "sin datos" cuando era "sin leer". Se
añaden `norm_exposure_by_strike` y `norm_exposure_by_expiration`, que proyectan ese
mapa sobre los dos ejes sumando con el signo del proveedor.

### Doce herramientas se descargaban y nadie las leía

Cada petición por ciclo cuenta contra un plan que puede ser de 240 peticiones a la
hora. El catálogo declaraba 32 herramientas y **doce no tenían ningún consumidor**:
gastaban cuota y, cuando el motor propio no tenía dato, la sección quedaba vacía
teniendo la respuesta del proveedor ya descargada en memoria.

| Herramienta | Sección que ahora la consume |
|---|---|
| `gex/dex/vex/chex_by_strike` | EXPOSICIÓN · respaldo del perfil por strike |
| `oi_by_strike` + `oi_change` | INTERÉS ABIERTO · respaldo del perfil por strike |
| `oi_over_time` | INTERÉS ABIERTO · historia de OI de la sesión |
| `contract_statistics` | ESTADÍSTICAS · tabla de contratos sin cinta observada |
| `trade_side_statistics` | ESTADÍSTICAS · reparto comprador/vendedor sin cinta |
| `stock_price_over_time` | DARK POOL · respaldo del panel PRECIO / TIEMPO |
| `news` · `gainers_losers` | RESUMEN · contexto del Dashboard, nunca señal |

En todos los casos **manda el motor propio y el proveedor sólo respalda**, y cada
bloque declara su origen (`by_strike_source`, `oi_history_source`,
`trade_side_source`, `candle_source`, `context_source`). Lo que el proveedor no
publica viaja vacío, nunca a cero: un `0` en interés abierto o en volumen es una
afirmación falsa sobre el mercado, no un hueco.

Un test recorre el catálogo entero y falla si alguna herramienta vuelve a quedarse
sin consumidor.

---

## 5 · Alcance de la verificación

- **1694 casos** en la suite completa, sin omisiones ni fallos.
- **60 casos nuevos** en `tests/test_v1427_net_drift_official.py`, más uno en la batería de Dark Pool.
- Arranque real de la aplicación comprobado: `/api/terminal/bundle` publica
  `net_drift` y `dark_pool` con sus estados, y `/` sirve la sección de flujo con el
  panel de Net Drift dentro.

### Lo que NO se ha podido verificar aquí

Se declara explícitamente porque un informe que calla sus límites no sirve:

- **No hay navegador en este entorno.** Ninguna comprobación visual es directa: lo
  que se verifica es la estructura del HTML, el CSS y el código del renderizador,
  no el píxel pintado.
- **No hay credenciales de Quant Data.** La forma de las respuestas se reproduce a
  partir de la que el propio código de producción ya documentaba
  (`_net_drift_series`, `_bucket_flow`, `_exposure_summary`, escritos contra la API
  real). La certificación de la curva se hace contra esa forma, no contra una
  sesión en vivo. **La comparación final contra la respuesta cruda de una sesión
  real queda pendiente de ejecutarse con clave en el VPS**, y para eso está
  `certify_against_raw()`, que se puede invocar sobre el payload en vivo.

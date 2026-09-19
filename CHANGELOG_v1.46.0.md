# ITM QUANT MULTI ASSET · v1.46.0 — Tres carriles, cinco códigos, ningún cero inventado

Release: `ITM_QUANT_v1.46.0_PRE_VPS` · Base: `v1.45.0` · Alcance: `MULTI_ASSET`

Esta release **no rehace nada**. La autoridad de Quant Data, el Data Hub, el Wall
Engine, QFLOW, Gamma Migration, el tri-estado de dark pool, el aislamiento por
canal, el Last Known Good y el registro de procedencia siguen exactamente como
quedaron. Lo que se corrige aquí no había sido corregido antes.

## Matemática del motor

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Ninguna
métrica cambia de valor por esta release.

## El patrón común

Los defectos de esta release son variaciones de una sola idea equivocada:
**tratar cosas distintas como si fueran la misma**.

| Dónde | Lo que se confundía | La consecuencia |
|---|---|---|
| Cliente del proveedor | un 400 con un 422 | se «reparaba» un cuerpo correcto y se marcaba como averiada una herramienta sana |
| Dark Pool | tres carriles con una sección | un cuerpo rechazado en `dark-pool-levels` apagaba la sección entera |
| Estado de un carril | «rechazado» con «todavía no llamado» | el Auditor no podía decir qué hacer |
| Ventana de cadena | una clase de instrumento con una lista de tickers | tres ETF con tres bandas distintas |
| Formateadores | un hueco con un cero | `$0.0` donde no hubo dato |
| Escala de los paneles | un dólar con «ninguna escala» | el primer fotograma saturaba las barras de los activos pequeños |

---

## 1 · Cinco clases de fallo, cinco tratamientos

Antes, `is_validation_error()` devolvía `True` para 400 **y** 422, así que los dos
entraban en el mismo ciclo de corrección del cuerpo. Son cosas opuestas:

```
400 REQUEST_INVALID   el cuerpo está mal          → se lee el campo y se corrige
422 NO_DATA           la petición era VÁLIDA      → no es un fallo de nadie
404 MISSING_TOOL      el plan no la incluye       → no se prueban rutas alternativas
5xx PROVIDER_ERROR    fallo suyo                  → reintento con backoff
--- TRANSIENT         red, timeout, rate limit    → reintento con backoff
```

Un 422 marcaba la herramienta como rota y disparaba correcciones sobre un cuerpo
que ya era correcto. Y un 400 entraba en el ciclo de reintentos, que es la razón
de que `dark-pool-levels` llevara ciclos enteros en DEGRADADO reintentando un
cuerpo que nunca iba a ser aceptado: **un 400 no se arregla esperando**.

Ahora un 400 se corrige como máximo tres veces por ciclo, y **cada corrección la
dicta el proveedor**: se lee `errors[].field` y se añade, se quita o se cambia ese
campo. Si no sabemos corregirlo, se dice qué campo es y se para. No se prueban
variantes al azar.

## 2 · `dark-pool-levels` envía su propio contrato

El cuerpo de partida era el genérico compartido con otras herramientas. Un cuerpo
con campos de más es tan inválido como uno con campos de menos, y
`dark-pool-levels` no comparte contrato con `dark-flow` (`aggregationPeriod`) ni
con `equity-prints` (`limit`).

```python
def _dark_pool_levels_body(ticker): return _tf(ticker)   # {"filter": {"ticker": …}}
```

Y una lista de bloqueo impide que vuelva a heredarlos aunque alguien los añada más
tarde: `sessionDate`, `timeRange`, `snapshotTime`, `filterExpression`,
`pagination`, `projection`, `sort`, `orderBy`, `cursor`, `offset`.

## 3 · Tres carriles, no una sección

`dark-flow`, `dark-pool-levels` y `equity-prints` son ahora canales separados de
verdad: cada uno con su timeout, su aislamiento, su Last Known Good y su estado.
Que uno rechace el cuerpo no dice **nada** sobre los otros dos, y la sección lo
declara:

```json
{"lanes_live": ["dark_flow", "equity_prints"],
 "lanes_broken": ["dark_pool_levels"], "degraded": true}
```

Dos carriles sanos bastan para que la sección tenga dato. El roto se declara
igualmente: media verdad sobre la cobertura es peor que ninguna.

## 4 · Ocho estados con causa y remedio

`DIRECT_PROVIDER_OK`, `SIN_DATOS_REALES`, `MARKET_CLOSED`, `STALE`,
`REQUEST_INVALID`, `PROVIDER_ERROR`, `PARSER_ERROR`, `NO_CLASIFICABLE`.

Cada uno lleva **qué hacer con él**; un estado sin acción asociada no sirve de
nada. La pantalla del analista sigue diciendo «SIN DATOS» —o «MERCADO CERRADO»
cuando eso es lo cierto—; el Auditor conserva cuál de los ocho fue, qué campo
rechazó el proveedor y qué corrige el problema.

Antes, cuando una herramienta fallaba **no se escribía nada**: el bloque quedaba
como estaba y era imposible distinguir «el proveedor rechazó el cuerpo» de
«todavía no se ha llamado en este ciclo». Ahora la causa viaja siempre, y el
último dato bueno se conserva degradado por edad: un fallo de refresco no es razón
para tirar lo que ya teníamos.

## 5 · El normalizador de niveles conserva los campos

`norm_levels` se quedaba cuatro campos y descartaba el resto en silencio. Ahora
conserva nivel de precio, nocional, acciones, número de operaciones, volumen
oscuro, volumen en bolsa, porcentaje sobre el volumen, el **precio de referencia
del subyacente** —que viaja a nivel de respuesta y leer sólo `rows` perdía— y
cualquier campo escalar que el proveedor mande y aquí no tenga nombre, bajo
`extra`.

Descartar un campo oficial porque el normalizador no lo conocía es
indistinguible, desde la pantalla, de que el proveedor no lo haya enviado.

## 6 · La ventana de cadena depende del instrumento, no del ticker

Una ventana declarada mandaba para una **lista de símbolos**. El resultado:

| | precio | ventana | banda |
|---|---|---|---|
| DIA | 410 $ | 12.0 | ±2.9 % |
| DJX | 410 $ | 12.0 | ±2.9 % |
| XLF | 48 $ | 8.0 | ±16.5 % |
| XLI | 130 $ | 12.0 | ±9.2 % |

Tres ETF que se leen igual con tres bandas distintas, y las dos primeras correctas
por casualidad. Ahora la excepción la define la **clase de instrumento**: un
futuro cotiza en puntos de índice (YM ~44.000) y un índice de volatilidad abarca
puntos de volatilidad absolutos; ni uno ni otro son un porcentaje del subyacente.
Todo lo demás —ETF, acciones, índices al contado, descubiertos o no— deriva su
banda del propio precio, ±2.25 %.

## 7 · Un hueco deja de formatearse como cero

`Q.num(null)` devuelve 0, así que `Q.money(Q.num(x), 1)` escribía **`$0.0`** cuando
no había dato, y `signedCompact` escribía `0.00`. Eso convierte la ausencia de un
dato en la afirmación de que vale cero, que es exactamente lo que la
especificación prohíbe.

El guardia se ha puesto en el **formateador**, no en cada una de las treinta y
ocho llamadas: una regla en un sitio se sostiene, treinta y ocho no. Las llamadas
que declaran un default explícito (`Q.num(x, 0)`) se respetan: ahí alguien eligió
el cero a propósito.

## 8 · La escala de los paneles ya no arranca en un dólar

```js
const maxG = new Q.GlideValue(280, 1);   // ← el «1» es un dólar
```

`GlideValue` sólo mueve `cur` hacia `tgt` al animar, así que el **primer
fotograma de cada panel** se dibujaba contra una escala de un dólar. En un activo
de 10⁷ eso se corrige en unas décimas y no se nota; en uno de 10² la escala falsa
es de la misma magnitud que el dato, y en un panel que sólo alcanza a dibujar un
fotograma —quince paneles compitiendo por el mismo bucle— era lo único que se
veía: el carril denso salía **saturado, todas las barras al tope**.

Sin valor inicial, el primer pico real se adopta de golpe y sólo se animan los
cambios posteriores. Se verificó mirando: cinco activos de 10⁹ a 10⁴, quince
paneles, captura a 900 ms.

---

## Lo que NO se ha podido verificar

- **No hay un 200 real de `dark-pool-levels`.** El entorno de construcción no
  tiene salida hacia `quantdata.us`, así que el contrato del proveedor no se ha
  podido leer de su documentación ni confirmar contra una respuesta real. Lo que
  se certifica es que el cuerpo que se envía es el mínimo propio de la
  herramienta, que un 400 se lee entero y se corrige con lo que el proveedor
  nombra, y que un rechazo no apaga los otros dos carriles.
- **La validación LIVE queda pendiente.** `verify_live_quantdata.py --dark-pool`
  recorre los tres carriles sobre `DIA`, `SPY`, `QQQ`, `AAPL`, `NVDA`, `TSLA` y
  `AMD` publicando el código HTTP y el campo rechazado de cada 400; hay que
  ejecutarlo donde exista la API key.
- **La comprobación visual es sobre el banco de pruebas**, que alimenta los
  renderizadores reales; la comparación contra una sesión de mercado real queda
  pendiente en el VPS.

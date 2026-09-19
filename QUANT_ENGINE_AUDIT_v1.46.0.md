# AUDITORÍA DEL MOTOR · ITM QUANT v1.46.0

## Veredicto

v1.45.0 auditó si **lo que faltaba** se estaba presentando como dato. v1.46.0
audita algo distinto: **si cosas que no son iguales se estaban tratando igual**.
Se estaban, en seis sitios, y el patrón es siempre el mismo — una abstracción
demasiado gruesa que ahorra una distinción que sí importaba.

---

## 1 · Seis colapsos de categoría

```python
is_validation_error(exc)        # 400 y 422 en el mismo saco
_dark_pool_levels_body(t)       # el cuerpo genérico de «una herramienta cualquiera»
# (nada escrito al fallar)      # «rechazado» y «todavía no llamado», indistinguibles
sym in STATIC_ASSET_SYMBOLS     # la clase del instrumento, reducida a una lista de tickers
Q.money(Q.num(x), 1)            # el hueco y el cero, el mismo «$0.0»
new Q.GlideValue(280, 1)        # «sin escala todavía» y «la escala es un dólar»
```

Ninguna de las seis produce un error. Las seis producen una respuesta
**plausible** que no es la correcta, y eso es mucho peor: un error se ve, una
respuesta plausible se usa.

---

## 2 · 400 y 422 son opuestos, no vecinos

Un 400 dice «tu petición está mal». Un 422 dice «tu petición está bien y no tengo
datos». Tratarlos igual produce dos daños simétricos:

- un 422 disparaba correcciones sobre un cuerpo **correcto** y marcaba como
  averiada una herramienta **sana**;
- un 400 entraba en el ciclo de reintentos, que no lo arregla nunca. Ésa es la
  razón de que `dark-pool-levels` llevara ciclos en DEGRADADO: se estaba
  esperando a que se curase solo un cuerpo que el proveedor no iba a aceptar.

La corrección no es más lógica: es **menos**. Cinco códigos, cinco caminos, y el
400 sale del ciclo de reintentos por definición.

## 3 · Reparar no es adivinar

La versión anterior probaba variantes del cuerpo hasta que una entraba. Funciona
—a veces— y es adivinar con más pasos: no queda constancia de cuál era el
contrato, sólo de cuál fue la variante que tuvo suerte.

Ahora la corrección la **dicta el proveedor**: se lee `errors[].field`, se añade
el campo que falta, se quita el que sobra o se corrige el valor inválido, y se
recuerda. Si el campo nombrado no está en nuestro catálogo de correcciones, la
respuesta correcta es decir **qué campo es** y parar. Un «no sé» explícito es
información; una cuarta variante no lo es.

## 4 · Un carril no es una sección

`dark-flow`, `dark-pool-levels` y `equity-prints` miden cosas distintas, por
endpoints distintos, con contratos distintos. Presentarlos como una sola unidad
significaba que el más frágil decidía por los tres.

Ahora cada uno lleva su estado, y la sección declara su propia degradación:
`lanes_live`, `lanes_broken`, `degraded`. Dos carriles sanos bastan para tener
dato; el roto se declara igual. **Media verdad sobre la cobertura es peor que
ninguna**, porque invita a confiar en lo que se ve sin saber qué falta.

## 5 · Un estado sin remedio no es un estado

Los ocho estados internos llevan, cada uno, qué hacer con él:

| estado | qué hacer |
|---|---|
| `REQUEST_INVALID` | corregir el payload — reintentar no sirve |
| `PROVIDER_ERROR` | esperar: reintento con backoff ya programado |
| `PARSER_ERROR` | corregir el normalizador |
| `MARKET_CLOSED` | nada: el vacío es el resultado correcto |
| `STALE` | el dato es de antes; no operar con él como si fuera de ahora |
| `NO_CLASIFICABLE` | llegaron impresiones sin señal de centro de ejecución |
| `SIN_DATOS_REALES` | no hay actividad fuera de bolsa en esta ventana |
| `DIRECT_PROVIDER_OK` | — |

Un estado que no cambia lo que haces a continuación es ruido con nombre propio.

## 6 · Una lista de tickers no es una regla

La ventana de cadena tenía una excepción declarada **por símbolo**. Medida sobre
los precios reales, esa excepción daba ±2.9 % a DIA, ±2.9 % a DJX, ±16.5 % a XLF y
±9.2 % a XLI: cuatro ETF, cuatro bandas, y las dos primeras correctas por
casualidad. Una lista de casos especiales siempre acaba así, porque nada obliga a
que sus miembros sean coherentes entre sí.

La excepción legítima existe y es **de instrumento**: un futuro cotiza en puntos
de índice y un índice de volatilidad abarca puntos de volatilidad absolutos.
Expresada así, la regla se sostiene sola y cubre los activos que todavía no
existen en el catálogo.

## 7 · El guardia va donde se toma la decisión

`Q.money(Q.num(x), 1)` producía `$0.0` sin dato, en treinta y ocho sitios. La
tentación es arreglar los treinta y ocho. La corrección real es una: el
formateador devuelve «—» ante un valor que no es un número, y las llamadas dejan
de neutralizarlo forzando el cero antes de llamarlo.

Treinta y ocho guardias se desincronizan; uno no. Las llamadas que declaran un
default explícito se respetan: ahí alguien eligió el cero a propósito, y esa
distinción —también ella— hay que conservarla.

## 8 · La escala inicial era una magnitud disfrazada de neutro

```js
const maxG = new Q.GlideValue(280, 1);
```

El `1` parece «sin valor todavía». Es un dólar. El primer fotograma de cada panel
se dibujaba contra esa escala, y el efecto dependía del **tamaño del activo**: en
10⁷ se corrige en décimas y nadie lo nota; en 10² la escala falsa tiene la misma
magnitud que el dato y todas las barras salen al tope. Con quince paneles
compitiendo por el mismo bucle de animación, los últimos alcanzaban a dibujar un
fotograma, y ese fotograma era lo único que se veía.

Se encontró **mirando**, no leyendo: la suite no falla, porque el defecto sólo
existe en la primera vuelta del bucle de render.

---

## Lo que esta auditoría NO puede afirmar

- Que `dark-pool-levels` devuelva 200 con el cuerpo actual. No hay salida de red
  hacia el proveedor desde el entorno de construcción, así que el contrato no se
  ha podido leer ni confirmar. Lo que sí se afirma: el cuerpo es el mínimo propio
  de la herramienta, un 400 se lee entero, la corrección la dicta el proveedor y
  un rechazo no apaga los otros dos carriles.
- Que los números coincidan con una sesión real de mercado. Eso lo decide
  `verify_live_quantdata.py` donde exista la API key.

# AUDITORÍA DEL MOTOR · ITM QUANT v1.45.0

## Veredicto

v1.44.0 auditó si el mismo número decía lo mismo en todas partes. v1.45.0 audita
algo anterior: **si lo que faltaba se estaba presentando como si fuera un dato**.
Se estaba, en cinco sitios distintos, y en ninguno se notaba.

---

## 1 · Cinco caras del mismo error

```python
bool(_pick(r, "offExchange", "darkPool", "isDarkPool") or False)   # None → False
str(obj.get("detail") or obj.get("title") or "")[:180]             # campo → nada
Math.max(1, Math.abs(y - y0))                                      # pequeño → cero
const k = a < 0.07 ? 0 : …                                         # bajo p95 → vacío
"scope": "EXTERNAL_CORROBORATION"                                  # autoridad → contraste
```

Las cinco líneas son defendibles leídas solas. Juntas son el mismo patrón: cuando
no hay dato, se escribe el valor más cómodo en vez de admitir que no se sabe. Y el
valor más cómodo siempre resulta ser una afirmación.

El coste no es un error visible. Es que **la pantalla dice algo falso con total
confianza**, y no hay forma de distinguirlo de la verdad mirándola.

---

## 2 · El 400 traía la respuesta y la tirábamos

El caso más claro. El proveedor mandaba:

```json
{"detail": "Request validation failed",
 "errors": [{"loc": ["body", "…"], "msg": "field required"}]}
```

y el cliente leía `detail`, lo recortaba a 180 caracteres y descartaba `errors`. Un
mes de `dark-pool-levels` en DEGRADADO con la causa escrita en cada respuesta.

**Corrección.** Se recorren las convenciones conocidas (`loc`/`msg` de Pydantic,
`field`/`message` de otros) en profundidad, y el campo viaja estructurado en la
excepción.

**Lo que no se pudo hacer.** Leer la documentación del proveedor: el egress a
`quantdata.us` está bloqueado en este entorno. Así que la reparación prueba
variantes en vez de aplicar el contrato. Es honesto —el orden lo fijan los campos
que otras herramientas de equities ya tienen confirmados contra esta cuenta, y el
resultado se recuerda— pero **es una búsqueda, no un contrato leído**, y hasta que
se ejecute contra la API real no se puede afirmar que `dark-pool-levels` funcione.

---

## 3 · La autoridad escrita dos veces, otra vez

v1.44.0 cerró tres llamadas a `structural_walls()` con tres frames distintos.
v1.45.0 encuentra el mismo patrón un nivel más arriba: `_NATIVE_CHANNEL_AUTHORITY`
era un segundo registro de autoridad, escrito a mano, que no se movió cuando
v1.43.0 cambió la política.

Que se pareciera tanto al defecto anterior es lo que lo hace interesante: **no fue
un descuido, fue una estructura que invita al descuido**. Dos sitios donde escribir
la misma verdad, y ninguna comprobación de que coincidan.

**Corrección.** `channel_authority()` deriva de `metric_authority`. No hay dónde
discrepar.

Al hacerlo salió una tercera discrepancia: `dark_pool_prints` era `ALPACA` en
`metric_authority` y `QUANTDATA` en `data_lineage`. Dos registros, el mismo dato,
dos respuestas.

---

## 4 · Transporte disfrazado de autoría

`ENGINE_LANE_SHARED` significa «el carril del motor ya trajo este payload, así que
el de páginas lo adopta en vez de gastar otra petición». Es una optimización de
cuota. En una columna llamada `ORIGEN` se convertía en `MOTOR`, que se lee como
«este dato lo calculó el motor».

Verifiqué que la procedencia interna **siempre fue correcta**: `DIRECT_PROVIDER ·
QUANTDATA` antes y después de que el motor consuma el dato. El defecto era sólo de
presentación — pero la presentación es lo único que el operador ve.

Dos columnas, dos preguntas. Y en Dark Pool, `ORIGEN` → `VÍA`, porque allí sus
valores dicen por qué camino se midió la zona, no de dónde viene el dato.

---

## 5 · El heatmap tenía un parámetro por ticker y nadie lo había escrito

Éste es el hallazgo que más me interesa, porque **no había ninguna constante por
activo en el código** y aun así el comportamiento dependía del activo.

`x / p95` con un campo de cola pesada deja la mayoría de celdas cerca de cero. Y la
cola es más pesada cuanto más concentrada la cadena. Así que el mapa se veía peor en
unos activos que en otros **por la forma de su distribución**, sin que nadie hubiera
decidido eso.

Un parámetro implícito por ticker no necesita estar escrito para existir: basta
elegir una transformación cuyo resultado dependa de una propiedad que varía entre
activos.

**Corrección.** Normalización por rango: cada celda se sustituye por su percentil
dentro de la matriz. La salida se reparte por construcción, sea cual sea la
distribución de entrada. Medido: la lineal oscila 45 %–97 % de celdas visibles según
el activo; la de rango se queda en 64 % en ambos casos.

**El coste, declarado.** El orden se conserva exacto; la proporcionalidad de la
intensidad no. Un mapa de calor sirve para ver dónde y cómo migra la concentración;
quien necesite magnitudes tiene la matriz cruda y el tooltip. Cambiar fidelidad de
magnitud por legibilidad es una decisión, y por eso está escrita aquí y en el
`normalization` que publica cada respuesta.

---

## 6 · Lo que sólo se vio al renderizar

Puse un suelo de 3 px al grosor de las barras. Correcto en el perfil por strike.
En el carril denso —390 buckets en 330 px, o sea 0.85 px por hueco— convierte el
carril en un bloque sólido donde ya no se distingue un bucket de otro.

Había cambiado «invisible» por «indistinguible». La información se pierde igual;
sólo que ahora parece llena.

**Corrección.** Cuando no caben, se agrupa: menos barras, cada una legible, cada una
cubriendo un intervalo real. Se conserva el **extremo** del grupo, no la media,
porque un pico aplanado por el promedio es justo lo que hay que ver en un carril de
flujo — y porque el extremo es un valor que existió, mientras que una media es un
número que nadie observó.

Ninguna prueba de lógica habría encontrado esto.

---

## 7 · Una prueba mal escrita que acertó

Al escribir la certificación puse un fixture con strikes separados un dólar para
los cinco activos. Falló para el de 9.5 $, porque un dólar ahí es un 10 % y tres
dólares se salen del rango operable.

El código tenía razón y la prueba estaba mal: **había cometido en el fixture el
mismo error de escala que el programa evita**. Se corrigió el fixture y se añadió
una prueba explícita del tope relativo.

Lo anoto porque es el segundo caso en dos releases —el anterior fue el fixture de
Walls— y sugiere que el sesgo de razonar en dólares absolutos es más persistente de
lo que parece.

---

## 8 · Lo que esta auditoría NO puede afirmar

- **Que `dark-pool-levels` funcione.** La reparación del cuerpo no se ha ejecutado
  contra la API real. Las variantes son candidatas razonadas; el contrato del
  proveedor no se pudo leer.
- **Que los datasets lleguen como `DIRECT_PROVIDER` en producción.** Sin
  credenciales todo cae a respaldo declarado.
- **Que Dark Pool tenga datos.** Se corrigió la clasificación que los perdía; si
  hay o no prints off-exchange sólo lo dice una sesión real.
- **Que el heatmap se vea bien con datos reales.** Se validó sobre campos
  sintéticos con la forma correcta —cola pesada— y sobre los renderers reales. Una
  sesión de mercado es otra comprobación.

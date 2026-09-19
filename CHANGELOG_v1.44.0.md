# ITM QUANT MULTI ASSET · v1.44.0 — Autoridad única de Wall, anclaje real y resiliencia del Data Hub

Release: `ITM_QUANT_v1.44.0_PRE_VPS` · Base: `v1.43.0` · Alcance: `MULTI_ASSET`

Esta release **no rehace nada de v1.43.0**. Todo lo de entonces —Quant Data como
autoridad de la estructura de opciones, el registro de procedencia, el Interval Map
como fondo de TRACE, QFLOW atribuido, la normalización por activo— sigue en pie.
Aquí se cierran ocho puntos pendientes.

## Matemática del motor · CAMBIO DE FÓRMULA DECLARADO

**No se modifican los pesos del Scanner ni su autoridad direccional.** Net Drift se
sigue leyendo del endpoint oficial.

**Lo que sí cambia:**

1. **Call Wall y Put Wall se calculan de otra forma y desde otro sitio.** Antes:
   concentración de gamma por lado sobre el frame que cada sección tuviera a mano.
   Ahora: media geométrica de **exposición por strike × interés abierto por
   strike**, ambos del proveedor, con regla de lado, histéresis y tope de distancia
   relativo. El número puede cambiar, y el motivo está abajo.
2. **Tres métricas propietarias nuevas**: `ITMQ_CALL_WALL`, `ITMQ_PUT_WALL` y el
   par de strikes de `ITMQ_GAMMA_MIGRATION`. Todas `DERIVED`.

---

## 1 · El defecto de fondo: tres muros con el mismo nombre

`structural_walls()` se llamaba desde tres sitios, cada uno con un frame distinto:

    nextgen_terminal      curagg del snapshot vivo          → niveles de TRACE
    key_levels_report     el mismo curagg, `enriched` a veces ausente → RESUMEN
    premarket             `agg` reconstruido con otro nombre de columna

Mismo nombre, tres entradas, tres resultados posibles. **El Call Wall de TRACE
podía no ser el de RESUMEN y nada en el programa lo detectaba.** Un nivel
estructural que cambia según la pestaña no es un nivel: es un rumor.

### La arquitectura que lo cierra

    Quant Data ──► Data Hub ──► Wall Engine ──► Call Wall / Put Wall
                                     ▲
                        lógica estructural ITM QUANT

Una entrada, un cálculo, una salida. TRACE y FLUJO DE ÓRDENES leen la **misma
lista de niveles**; RESUMEN dejó de leerlos de `key_levels_report`.

### Por qué exposición × interés abierto, y por qué media geométrica

Un muro responde: *si el precio llegara ahí, cuánta cobertura habría que ajustar*.

- La **exposición** da la magnitud. Sin ella no hay muro, sólo contratos.
- El **interés abierto** dice cuántos contratos la sostienen. Sin él, un strike
  con exposición calculada pero sin libro detrás puntúa igual que uno con cien mil
  contratos abiertos.

Se multiplican, no se suman: **la suma deja pasar a los que sólo destacan en una
cosa**, y un muro que sólo existe en una de las dos evidencias no es un muro.

Cuando el proveedor no publica OI, **no se penaliza al strike**: se puntúa sólo con
exposición y se declara (`oi_available: false`). Multiplicar por un dato ausente es
inventar un veredicto.

### Histéresis

Un muro que salta de strike en cada refresco no se puede operar. El vigente sólo se
sustituye si el candidato lo supera por más del 15 %, **o** si deja de ser válido
—el precio lo atravesó, o desapareció de la cadena—. Un muro atravesado se retira
al instante: la histéresis protege del ruido, no de la realidad.

### Tope de distancia relativo, no en dólares

Un strike a 3 $ del precio es medio por ciento en un índice de 600 y un 31 % en una
acción de 9.5. El tope es un porcentaje, así que la cola de la cadena de un activo
barato no puede presentarse como su muro. *(Este detalle lo destapó una prueba que
escribí mal: el fixture usaba pasos de un dólar para los tres activos, cometiendo
en la prueba el mismo error de escala que el programa evita.)*

## 2 · Las mismas Walls en TRACE y en FLUJO DE ÓRDENES

`CALL WALL 518.00` y `PUT WALL 515.00` se dibujan como líneas horizontales
discretas en los dos sitios, desde la misma lista. FLUJO **no recalcula nada**: ya
filtraba `payload.levels`, y ahora esos niveles llevan `authority:
ITMQ_WALL_ENGINE`. Los de la sección se **sustituyen**, no conviven: dejarlos
habría vuelto a permitir dos muros con el mismo nombre en la misma pantalla.

Así se puede mirar qué hacen precio, QFLOW, Net Flow, Net Drift y agresión cuando
el precio llega a una Wall, sin abrir ningún panel nuevo.

## 3 · Gamma Migration sobre el strike, no en una tarjeta

El motor ya la calculaba. Lo que faltaba era publicar **el par de strikes**: de
dónde salió la exposición y adónde fue.

    Γ MIG 516 → 517

Se dibuja a la altura del strike que ganó exposición, con una flecha desde el que
la perdió. Cuando todo el cambio va en el mismo sentido no hay migración: hay
`ACCUMULATION` o `DISCHARGE`, que es otra cosa y se declara como tal.

`QD_INTERVAL_MAP` sigue siendo `DIRECT_PROVIDER`; `ITMQ_GAMMA_MIGRATION` es
`DERIVED`.

## 4 · QFLOW anclado a la vela exacta

Antes la marca se dibujaba a la altura de `m.price`, que es el precio de referencia
del bucket del proveedor de **opciones** —otro reloj, otra granularidad que la vela
del subyacente—. La marca quedaba cerca del gráfico sin pertenecer a ninguna vela.

Ahora se resuelve la vela que **contiene** el instante (búsqueda binaria sobre
`[t, t+bar)`), y la marca se coloca en su centro temporal, a la altura del máximo
(calls) o el mínimo (puts) **de esa vela**.

Un evento que cae en un hueco de la serie no se asigna a la vela anterior: se
dibuja atenuado y declarado. El detalle enriquecido del Order Flow —cuántas calls,
compras o ventas, BLOCK/SWEEP/SPLIT, strike dominante, vencimiento y DTE— vive en
el **hover**, no pintado encima de las velas.

## 5 · Las marcas derivadas aún no aprobadas no se dibujan

`Confluence`, `Containment`, `Break`, `Divergence` y `Transition` se siguen
calculando y viajando para el motor, pero **no se pintan en TRACE**. Una prueba
inspecciona el código visible del renderer para que no se cuelen por descuido.

Visualmente, hoy: QFLOW sobre velas · Gamma Migration sobre strike · Call Wall ·
Put Wall · más lo ya aprobado.

## 6 · Resiliencia del Data Hub

Tres piezas ya existían y **no se rehacen**: caché por ticker/dataset (`RawCache`),
backpressure por cuota (`QuotaGuard`) y reintento con backoff. Se añaden las cuatro
que faltaban:

| Pieza | Qué fallaba sin ella |
|---|---|
| **Deduplicación en vuelo** | La caché deduplica *después* de la primera respuesta. Al abrir la terminal, TRACE y EXPOSICIÓN piden GEX a la vez con la caché vacía: **las dos salen a la red**. |
| **Last Known Good** | La caché caduca a `None`, indistinguible de «no hay datos». Un dato de hace tres minutos no es un hueco: es un dato viejo. Ahora se degrada por edad (`FRESH`→`DEGRADED`→`STALE`) y lo declara. |
| **Merge incremental** | Sustituir la serie entera pierde historia si el proveedor republica sólo la cola; acumular a ciegas cuenta el bucket abierto una vez por refresco. Gana el valor más reciente de cada instante. |
| **Aislamiento por canal** | Un `gather` desnudo termina cuando termina el más lento: un `dark-flow` de 9 s retrasaba 9 s la estructura de TRACE. Ahora cada canal lleva su timeout y su cortocircuito. |

**Lo que llega tarde no se tira**: alimenta el Last Known Good y sirve en el ciclo
siguiente. Descartarlo obligaría a pagar otra vez la misma cuota por el mismo dato.

**El precio no pasa por este carril** y no puede quedarse esperando a un endpoint
de opciones.

## 7 · Cambio de símbolo transaccional

La **época** avanza antes de tocar nada. Cualquier respuesta en vuelo del símbolo
anterior llega con época caducada y **se descarta en vez de escribirse bajo el
ticker nuevo**: números correctos bajo el símbolo equivocado es el defecto más
difícil de detectar de todos.

Se invalidan **todas** las capas por símbolo —procedencia, muros y Last Known
Good—, y de **los dos** tickers: el anterior porque ya no va a mostrarse, y el
nuevo porque pudo quedar sembrado por una consulta previa más vieja que el cambio.

Los datasets **críticos cargan primero** (exposición, OI, Interval Map, flujo);
noticias y gainers/losers esperan al ciclo siguiente sin que se note. La
invalidación es higiene, no precondición: si falla, se anota y el cambio de activo
sigue.

## 8 · Verificador LIVE

`scripts/verify_live_quantdata.py` comprueba, con credenciales reales y la terminal
en marcha, que cada dataset llega como `DIRECT_PROVIDER` y que la muestra
**sobrevive** los tres tramos:

    RAW PROVIDER ──► NORMALIZADOR ──► API INTERNA ──► lo que vería el frontend

    python scripts/verify_live_quantdata.py --ticker SPY --ticker AAPL --json informe.json

Distingue tres formas de no-aprobado: `SIN DATOS` (el plan no incluye la
herramienta o el mercado está cerrado), `NO DIRECTO` (el proveedor respondió pero
la terminal sirvió un respaldo) y `ERROR`. **Que el endpoint conteste no basta**:
un 200 con cero filas sigue siendo un fallo y se reporta como tal.

---

## Auditoría

El razonamiento detrás de cada decisión —por qué media geométrica y no suma, por
qué la contención de la vela es estricta, por qué la petición que expira no se
cancela— está en `QUANT_ENGINE_AUDIT_v1.44.0.md`.

## Límites declarados

1. **La validación LIVE sigue PENDIENTE.** Esta release se certifica con datos
   controlados; no dispongo de credenciales de Quant Data. El verificador existe y
   está probado en su lógica, pero **no se ha ejecutado contra la API real**.
   `certification_status: FUNCTIONAL_VERIFIED_LIVE_PENDING`.
2. **El artefacto oficial sigue sin generarse.** `scripts/package_release_artifact.py`
   exige un toolchain pinado (Python 3.12.14, pip/node/npm exactos) del que este
   entorno no dispone y falla cerrado a propósito. El ZIP entregado es un
   **checkpoint funcional**, no el artefacto reproducible certificado.
3. **Los umbrales de Wall no están calibrados**, sólo adaptados. La histéresis del
   15 % y el tope del 12 % son puntos de partida razonables; la calibración sólo la
   da la observación en vivo.

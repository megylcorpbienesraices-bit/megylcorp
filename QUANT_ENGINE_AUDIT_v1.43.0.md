# AUDITORÍA DEL MOTOR · ITM QUANT v1.43.0

## Veredicto

v1.42.6 auditó si el programa sabía **cuándo no tenía por qué haber datos**. v1.43.0
audita algo anterior y más incómodo: **si el programa sabía de quién era cada
número**. No lo sabía. Y esa ignorancia no producía errores visibles —producía algo
peor: números plausibles de procedencia desconocida.

---

## 1 · El defecto de fondo: la sustitución silenciosa

En v1.42.7, `_exposicion` empezaba así:

```python
exposure_source = "ITM_QUANT" if by_strike else None
if not by_strike:
    ...   # el proveedor entra sólo si el motor no tiene nada
```

Y lo mismo `_open_interest` («Mismo criterio que EXPOSICIÓN: manda el motor»), y
`_interval_map` («1. `heatmap_history` del motor … 2. la herramienta del
proveedor»), y el IV Rank («El motor manda cuando tiene historia suficiente»).

Cuatro sitios, un mismo patrón, y ninguno de los cuatro **dejaba constancia**. En
pantalla, `GEX 1.24B` se leía exactamente igual viniera del proveedor o del motor.

Eso importa por una razón concreta: el GEX de Quant Data y el de ITM QUANT **no son
la misma magnitud**. Tienen distinto universo de vencimientos, distinta hipótesis
de posicionamiento de dealer y posiblemente distinta representación de unidades.
Intercambiarlos sin decirlo no es una degradación: es cambiar de definición a mitad
de la lectura.

**Corrección.** La autoridad pasa al proveedor, el cálculo propio pasa a
`FALLBACK` **declarado** o a canal de auditoría, y `guard_primary_source()` **falla**
si alguien intenta lo contrario con el proveedor sano. La prueba
`test_a_healthy_primary_source_cannot_be_covered_in_silence` ejercita esa excepción.

Lo que **no** se hizo: borrar el cálculo propio. Sigue publicándose en `audit` y en
`*_engine`. Que las dos construcciones difieran es la señal más útil que hay, y
promediarlas la borraría. La fusión numérica sigue prohibida por política.

---

## 2 · El Interval Map estaba descargado y sin usar

`interval-map` se pedía cada ciclo —gastando cuota— y su normalizador era:

```python
lambda p: {"ready": bool(p), "raw": p}
```

Es decir: se guardaba el payload crudo y se dejaba que cada consumidor lo
interpretase. `_interval_map` intentaba adivinar su forma en tiempo de
presentación, y el carril del motor sólo lo usaba para un cálculo de migración.

El coste real: **TRACE dibujaba el cálculo propio como fondo** teniendo el mapa del
proveedor ya en memoria, y las griegas DELTA, VANNA y CHARM no existían como mapa
porque sólo se pedía GAMMA.

**Corrección.** `norm_interval_map()` lee la forma real
(`{instante_ms: {vencimiento: {strike: {CALL, PUT}}}}`) y publica matrices neta,
call y put; cuatro herramientas, una por griega; y tres formas alternativas de
envoltorio reconocidas, porque el proveedor cambia el sobre entre cuentas y fijar
una ruta única fue lo que dejó el mapa vacío en su día.

**Detalle de signo que habría invertido el mapa.** `putExposure` llega **ya
firmada**. El neto es `call + put`, no `call − put`. Restar una magnitud que ya es
negativa duplica el signo, y el mapa entero saldría al revés sin que nada fallara.

---

## 3 · QFLOW se quedaba en Net Flow

`build_qflow` consumía sólo los buckets de `net-flow`. Eso da la serie y el nivel,
pero deja las concentraciones **anónimas**: un pico de 4.2 M$ sin saber si fueron
calls o puts, comprados o vendidos, en qué strike, a qué vencimiento, ni si fue un
BLOCK negociado o un SWEEP barriendo bolsas.

La información estaba disponible: `order-flow/consolidated` y
`order-flow/unconsolidated` se descargaban y se usaban sólo para una lista bajo Net
Drift.

**Corrección.** `attribute_events()` cruza los instantes marcados con las
impresiones, en una ventana de 90 s —los relojes del agregado y de la cinta no
tienen por qué coincidir al milisegundo—. Si no hay cinta, se dice: una
concentración sin atribuir es mejor que una atribuida a operaciones que nadie vio.

---

## 4 · El umbral que sólo podía estar bien para un activo

```python
EVENT_QUANTILE = 0.97
EVENT_MIN_SHARE = 0.25
```

Esto era mejor que un límite en dólares —ya era relativo a la sesión— pero seguía
teniendo un agujero: **el cuantil 97 de una sesión plana sigue siendo ruido**. Y
sobre el ruido, `share_of_peak` tampoco salva, porque el pico también es ruido.

**Corrección.** Tres lentes en **conjunción**: cuantil de sesión, fracción del pico
y z robusta (mediana + MAD). La mediana y el MAD no se mueven porque haya cuatro
barras enormes, que es exactamente cuando hace falta que no se muevan. Tomar el
máximo de las tres y no el mínimo es deliberado: pasar la lente más laxa es fácil, y
así se fabrican los falsos positivos.

La z robusta devuelve `None` cuando no hay dispersión medible, en vez de un infinito
que aguas abajo marcaría **todo** como evento.

---

## 5 · Decisiones de la inteligencia propia que merecen justificación

**La confluencia mide acuerdo de signos, no suma de magnitudes.** Net Drift está en
dólares de prima, DEX en delta nocional y el sesgo de dark pool en dólares de otra
cosa. Sumarlos produce un número sin unidad ni significado. Lo que sí significa algo
es si apuntan al mismo sitio.

**Una señal ausente pesa cero, no cuenta como neutra.** Si tres de cinco corrientes
coinciden y dos no llegaron, la confluencia de las tres es total. Contar las dos
ausentes como «neutras» la rebajaría al 60 % e invitaría a no actuar sobre una
lectura que sí es unánime en todo lo observable.

**`TRANSITION` se evalúa antes que `BREAK`.** Si la gamma está migrando, los niveles
de hace media hora ya no describen el libro actual, y un «BREAK» de un nivel que ya
no existe es una lectura falsa. Es el error que más caro sale cuando la estructura
rota rápido, y el orden de los `if` es lo único que lo evita.

**El Structural Score declara su cobertura.** Un 62 calculado con dos de seis
componentes y un 62 calculado con los seis se ven idénticos. `coverage_pct` y
`actionable` existen para que no lo sean.

**Los strikes dominantes usan media geométrica, no suma.** Un strike con mucha gamma
y sin contratos abiertos es una cifra sin libro detrás; uno con mucho OI y poca
gamma es peso muerto. La suma deja pasar a los que sólo destacan en una cosa. Y
cuando no hay OI, no se multiplica por cero: se puntúa sólo con exposición y se
declara, porque multiplicar por un dato ausente es inventar un veredicto.

---

## 6 · Lo que esta auditoría NO puede afirmar

- **Que los números del proveedor sean correctos.** Se certifica que ITM QUANT
  publica lo que el proveedor emite, sin deformarlo. Si el proveedor se equivoca,
  ITM QUANT publicará su error fielmente y el canal de auditoría —la construcción
  propia, que sigue ahí— es lo único que puede señalarlo.
- **Que el renderizado sea correcto con datos reales.** La suite ejercita la lógica
  con datos controlados. Ver TRACE, QFLOW, Net Drift y Dark Pool dibujando una
  sesión real exige mercado abierto y cuenta activa:
  `docs/operations/VALIDACION_LIVE_PRE_PRODUCCION_v1.43.0.md`.
- **Que los umbrales estén bien calibrados para todos los activos.** Se certifica
  que **se adaptan** a la escala de cada activo, que es distinto de estar
  calibrados. La calibración sólo la da la observación en vivo.

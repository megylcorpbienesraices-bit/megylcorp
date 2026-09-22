# AUDITORÍA DEL MOTOR · ITM QUANT v1.59.0

## Veredicto

Esta release **no cambia ninguna fórmula**. Lo que corrige es anterior a la
fórmula: los cuatro sitios donde el motor daba una respuesta que no distinguía
entre causas distintas, y por tanto no se podía actuar sobre ella.

Los cuatro tienen la misma forma. Un cajón genérico —`DEGRADED`, «sin cálculo
en este ciclo», `SIN_DATOS`, «sin intentos»— agrupaba entre tres y seis
situaciones que se arreglan de maneras opuestas. Mientras estuvieron juntas, la
única manera de saber cuál era la de hoy era leer el código.

---

## 1 · Una etiqueta de criticidad en el sitio equivocado

Un fallo se marcaba `OPTIONAL` o `DEGRADED` mirando **al endpoint**. Eso es
imposible de acertar, porque la criticidad no vive en el dato:

```
gamma falla
  · para una tarjeta con respaldo propio   → opcional: se degrada y ya
  · para Call Wall / Put Wall              → gamma ES un factor de la fórmula
```

El mismo fallo, dos consecuencias. Con una etiqueta global hay que elegir una, y
las dos elecciones son malas: marcarlo opcional hace desaparecer las Walls en
silencio; marcarlo crítico llena la pantalla de alarmas por una tarjeta que
tenía respaldo.

La corrección es declarativa: **cada consumidor dice de qué depende y con qué
nivel**, y la severidad se DERIVA (`severity_for`). No hay tabla de severidades
que mantener a mano, y añadir un consumidor no obliga a reclasificar endpoints.

### Lo que esta auditoría vigila aquí

Un evaluador de dependencias tiene una tentación clara: tratar «no lo sé» como
«no está». Es lo que produce el falso negativo más caro —una sección en rojo que
funciona— y es exactamente lo que pasaría si el Auditor del carril de páginas
evaluara Walls, que dependen del precio del subyacente, que ese carril no mide.

Por eso hay un cuarto estado, `UNKNOWN`, y dos reglas duras:

* lo que **no está en el mapa** no se afirma: sale como *no medido*, con el
  carril al que hay que preguntarle;
* lo que está **todavía en la cola** (`SCHEDULED`, `RUNNING`,
  `WAITING_RATE_LIMIT`) tampoco bloquea: no ha servido, pero tampoco ha fallado.

`COOLDOWN` queda deliberadamente **fuera** de esa exención: viene de fallar, y
eso sí es un veredicto.

---

## 2 · Una lectura estructural atada al ritmo de la red

Las Walls se calculaban con lo que hubiera llegado **en el ciclo de sondeo
actual**. Un timeout dejaba el panel con «sin cálculo de muros en este ciclo»,
que no dice si faltó la gamma, el OI, el vencimiento o el precio, ni si había
muros válidos hace treinta segundos.

El defecto de fondo no es la frase: es **atar una magnitud que cambia despacio
—la estructura de la cadena— a un canal que falla a menudo**. Un vencimiento no
desaparece porque una petición muera.

`wall_snapshot` reúne los seis ingredientes en un objeto coherente y resuelve en
uno de tres estados, ninguno de ellos una caja vacía:

```
COMPLETO       el ciclo trajo todo                     → se calcula
LKG            el ciclo perdió algo, hay uno anterior  → se publica CON SU EDAD
NO_CALCULABLE  nunca hubo suficiente                   → se nombra qué falta
```

### El detalle que esta auditoría considera no negociable

Un snapshot es coherente **o no es nada**. Servir el LKG significa servir sus
filas **y su precio**: combinar una cadena de hace cuarenta segundos con el spot
de este instante produce una wall medida sobre dos mercados, y el precio entra
**al cuadrado** en la exposición, así que el error no es proporcional al desfase.
Está atado en regresión.

La política de frescura es explícita y declarada: **180 s** para sostener el muro
como LKG, **900 s** de tope absoluto. Por encima, la estructura del día ya es
otra y se dice `NO_CALCULABLE` en vez de seguir enseñando una línea.

---

## 3 · Un par de campos para dos preguntas distintas

El carril de dark pool respondía con **un** par `state`/`rows` a dos preguntas
que no son la misma:

* ¿qué pasó en **este** refresco?
* ¿qué hay **guardado** del último que funcionó?

Con un timeout y 216 filas del ciclo anterior, cualquiera de las dos lecturas
posibles era falsa: `SIN_DATOS` tira un dato bueno; `OK` esconde que el refresco
murió. Ahora son cinco hechos independientes, más la conclusión (`serving`), y
los tres significados dejan de confundirse:

```
HTTP 200 con cero filas   → NO_DATA        no hay actividad, no hay avería
timeout/5xx con LKG       → STALE_LKG      hay dato real, es viejo, va con su edad
timeout/5xx sin LKG       → PROVIDER_ERROR
```

Un 400 **no** se rebaja por tener filas antiguas: reintentar un cuerpo mal
formado no lo arregla, y ése sí es un fallo nuestro.

---

## 4 · El defecto que el cajón genérico escondía

«Sin intentos» juntaba seis situaciones:

```
su cadencia no vence            → no pasa nada, es el ritmo
la cuota está llena             → esperar segundos
le falta un dato del que depende→ arreglar OTRA cosa
viene de fallar, en enfriamiento→ esperar el backoff
está llamando AHORA             → esperar milisegundos
lleva exigible diez minutos
  sin que nadie la llame        → ESO es un defecto del programador
```

Sólo la última es un defecto, y era la que quedaba tapada por las otras cinco.
Los nueve estados la separan, y `NUNCA_LLAMADA` sale **como anomalía y no como
estado**, porque un estado describe a la herramienta y una anomalía acusa al
programador. No comparten cajón.

### Dos decisiones de orden, que no son cosméticas

* **Primero lo que pasa AHORA.** `RUNNING` se comprueba antes que el resultado
  de la última llamada: al revés, una herramienta que está llamando en este
  instante se declararía con el estado de su intento anterior.
* **Exigible y sin un solo intento es `SCHEDULED`, no `NO_DATA`.** Decir
  `NO_DATA` ahí atribuye al proveedor un silencio que es nuestro. Y la exención
  de la anomalía es por **cadencia**, nunca por estado: una herramienta exigible
  en `SCHEDULED` es precisamente a la que nadie está llamando.

---

## Lo que esta auditoría NO afirma

* Que las Walls que salen ahora sean las correctas: la fórmula, su convención
  declarada (`CLIENTE_LARGO_OPCIONES__DEALER_CORTO_GAMMA`) y sus límites siguen
  siendo los de v1.57.2, y esta release **no los toca**.
* Que el transporte esté certificado en producción: las medidas de v1.58.0
  siguen pendientes de la evidencia LIVE en Windows.
* Que un consumidor en `READY` esté dibujando bien: `READY` dice que sus
  dependencias declaradas están disponibles, no que el resultado sea correcto.
  Eso lo dicen las auditorías de cada sección, no ésta.

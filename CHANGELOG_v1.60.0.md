# ITM QUANT MULTI ASSET · v1.60.0 — El transporte, de raíz

Release: `ITM_QUANT_v1.60.0_PRE_VPS` · Base: `v1.59.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Call
Wall / Put Wall y los bloques 3, 4, 5 y 7 de v1.59.0 quedan intactos.

> **Este bloque NO se da por cerrado con esta entrega.** Se cierra cuando el
> certificador LIVE, ejecutado en el Windows del operador contra la API real,
> demuestre que desaparece la repetición de `connect timed out`. Lo que hay aquí
> es la corrección y su regresión; la prueba es suya.

---

## 0 · LO QUE EL REGISTRO DE WINDOWS ENSEÑABA

Con v1.58.0 instalada y la cuota intacta:

```
Quant Data connect timed out after 4.0s   net_drift
Quant Data connect timed out after 4.0s   net_flow
Quant Data connect timed out after 4.0s   dark_flow
Quant Data connect timed out after 4.0s   gamma
```

Cuatro endpoints que **no comparten nada salvo el host**, muriendo al mismo
plazo exacto, ciclo tras ciclo. Cuando el fallo es idéntico en cosas que sólo
comparten el destino, el defecto no está en el endpoint: está en el transporte.

v1.58.0 separó lectura de conexión y calibró la **lectura** con el p95 por
endpoint. Eso arregló los cortes a 5.0 s. Y dejó el otro lado intacto: la
conexión seguía siendo una constante aplicada por petición, sin medir nada.

---

## 1 · LA CADENA, ENTERA

### Causa 1 · dos pools contra el mismo host

El carril del motor y el de páginas construían cada uno su
`httpx.AsyncClient`. Dos juegos de conexiones, y ninguno podía aprovechar lo que
el otro tenía abierto.

### Causa 2 · keep-alive más corto que el ciclo

```
httpx keepalive_expiry por defecto    5 s
ciclo del carril de páginas          15 s
```

Cada ciclo encontraba **todas** las conexiones caducadas. Un handshake TCP + TLS
por herramienta y por ciclo.

### Causa 3 · y todos a la vez

Con el lote saliendo junto, eso es una **estampida de conexión**: seis u ocho
handshakes simultáneos contra el mismo host, compitiendo por el mismo enlace.
Con antivirus o proxy corporativo de por medio, el handshake TLS se va por
encima de los cuatro segundos sin que el proveedor tenga nada que ver.

### Causa 4 · y aprender era imposible por construcción

La misma trampa que v1.58.1 cerró para la lectura, intacta para la conexión:

```
plazo corto → el handshake no completa → no hay muestra
            → no hay p95 → el plazo sigue corto → …
```

El sistema no podía salir de ahí ni con una hora de tráfico.

---

## 2 · LO QUE CAMBIA · `app/core/transport_runtime.py` (NUEVO)

```
pool            UNO por host, compartido por los dos carriles, con refcuenta
keep-alive      90 s · sobrevive al ciclo, así el handshake se paga UNA vez
connect         p95 del handshake MEDIDO, a nivel de HOST · suelo 3 s · techo 15 s
escalada        al agotarse un plazo, el siguiente sube ×1,6 — la salida de la trampa
fases           connect · read · write · pool, con nombre propio cada una
breaker         de TRANSPORTE, por host, separado del de endpoint
retry           UNO de conexión · presupuesto por ciclo · backoff+jitter · NUNCA en ráfaga
telemetría      pool_wait_ms · connect_ms · tls_ms · write_ms · read_ms · reuse_pct
```

### Las dos autoridades, y por qué no pueden ser la misma

```
CONEXIÓN  →  del HOST      un handshake no pertenece a ninguna herramienta
LECTURA   →  del ENDPOINT  una respuesta lenta no dice nada sobre la red
```

Medir la conexión por endpoint reparte treinta y seis veces la misma muestra y
hace falta treinta y seis veces más tráfico para aprender lo mismo.

### El pool se dimensiona DESDE el techo de concurrencia

Un pool más pequeño que el techo convierte concurrencia en espera de pool, y esa
espera **se lee como lentitud del proveedor sin serlo**. Uno mucho mayor devuelve
la estampida por el otro lado.

### Las cuatro fases, con su remedio

```
CONNECT_TIMEOUT  no se alcanzó al host      red/DNS/TLS · afecta a las 36 a la vez
POOL_TIMEOUT     nuestro pool lleno         congestión PROPIA · subir el plazo del
                                            proveedor no la toca
READ_TIMEOUT     conectó y no contestó      el endpoint tarda: su p95 manda
WRITE_TIMEOUT    no se pudo enviar          enlace de subida
```

Antes los cuatro llegaban como «timeout» y el remedio se adivinaba.

---

## 3 · POR QUÉ EL NÚMERO TAMBIÉN CAMBIA, Y POR QUÉ NO ES EL ARREGLO

El warm start de conexión pasa de **4.0 s a 8.0 s**. No porque ocho funcione
mejor que cuatro, sino porque el razonamiento que sostenía el cuatro —«si la
conexión no se establece en cuatro segundos, no va a establecerse»— **es falso
con la evidencia delante**: un handshake TLS con inspección de por medio tarda
más. Un supuesto que el campo contradice se corrige.

Pero subir el número no arregla nada por sí solo. **Con el pool compartido y el
keep-alive largo, el handshake deja de ocurrir en cada ciclo**: se paga una vez y
se reutiliza. Ésa es la diferencia entre no cortar la conexión y no tener que
abrirla.

La medida que lo demuestra es `reuse_pct`, y está en el informe LIVE. En el
ensayo en seco contra un proveedor local: **13 peticiones, 1 handshake, 92 % de
reutilización**.

Y el `4.0` fijo **ya no existe en ningún sitio**:
`endpoint_runtime.CONNECT_TIMEOUT_S` se lee del transporte, para que no haya dos
números distintos diciendo ser el mismo plazo.

---

## 4 · REGRESIÓN

`tests/test_v1600_transporte_de_conexion.py` · **38 casos**, entre ellos los dos
que el operador pidió por su nombre:

* **estampida de conexión** — ocho herramientas salen a la vez y se paga **un
  solo handshake**; las otras siete reutilizan;
* **recuperación** — tras una tanda de fallos el host vuelve, el circuito se
  cierra y el contador de fallos consecutivos se pone a cero.

Y los que atan lo que no se puede volver a perder: el pool es del host y no del
carril; el primero en parar no deja al otro sin transporte; el keep-alive
sobrevive al ciclo; cada timeout dice su fase; un pool lleno no abre el circuito
del host; un plazo agotado SUBE el siguiente y tiene techo, y baja solo; el
reintento respeta las cuatro condiciones; **un fallo de conexión no borra el
último dato bueno** y no infla el plazo de lectura del endpoint.

---

## 5 · AUDITORÍA DEL MOTOR

La auditoría de esta release, con lo que **no** afirma, está en
`QUANT_ENGINE_AUDIT_v1.60.0.md`.

---

## 6 · LO QUE FALTA PARA DAR ESTO POR CERRADO

La certificación LIVE en Windows. El certificador trae siete afirmaciones nuevas
(8 a 14) que sólo se pueden medir contra la red real:

```
py -3.13 scripts\certificar_transporte_live.py --symbols DIA,SPY,QQQ --cycles 12
```

Devuelve `CERTIFICACION_LIVE_TRANSPORTE.json` y `.md`.

---

# Historial · v1.59.0 — Cuatro cajones genéricos, abiertos uno a uno

Release: `ITM_QUANT_v1.59.0_PRE_VPS` · Base: `v1.58.0` · Alcance: `MULTI_ASSET`

## 0 · POR QUÉ ESTOS CUATRO Y POR QUÉ JUNTOS

Los cuatro bloques de esta release corrigen el mismo error en cuatro sitios
distintos: **una respuesta que junta causas que se arreglan de maneras
opuestas**.

```
DEGRADED                       ← el endpoint, sin saber quién lo consume
«sin cálculo en este ciclo»    ← ¿faltó gamma, OI, vencimiento o precio?
SIN_DATOS con 216 filas        ← ¿lo de ahora o lo guardado?
«sin intentos»                 ← seis situaciones, una de ellas un defecto
```

Mientras estuvieron juntas, saber cuál era la de hoy exigía leer el código.

---

## 1 · BLOQUE 3 · LA CRITICIDAD ES DEL PAR (DATO, CONSUMIDOR)

### El defecto

`OPTIONAL` y `DEGRADED` eran propiedades **del endpoint**. Pero:

```
gamma falla
  · para una tarjeta con respaldo propio   → opcional
  · para Call Wall / Put Wall              → gamma ES un factor de la fórmula
```

Una etiqueta global obliga a elegir una de las dos lecturas, y la elegida es
falsa la mitad del tiempo.

### Lo que cambia · `app/core/consumer_contracts.py` (NUEVO)

Siete consumidores —`WALLS`, `TRACE`, `DARK_POOL`, `EXPOSICION`, `FLUJO`,
`VOLATILIDAD`, `INTERES_ABIERTO`— **declaran** de qué dependen, con qué nivel y
por qué, y qué hacen cuando les falta algo. La severidad se DERIVA:

```python
severity_for("interval_map_gamma")  # DEGRADED · TRACE lo exige
severity_for("max_pain")            # OPTIONAL · nadie lo exige
```

Y cada consumidor dice su propio estado sin preguntarle al endpoint:

```
READY     tiene todo lo obligatorio
DEGRADED  tiene lo obligatorio, le falta algo opcional
BLOCKED   le falta algo OBLIGATORIO, y se dice cuál
UNKNOWN   falta MEDIR algo obligatorio
```

### El cuarto estado no es un adorno

`UNKNOWN` es lo que impide el falso negativo más caro: **declarar bloqueada una
sección que funciona** porque nadie preguntó por uno de sus datos. Dos reglas:

* lo que no está en el mapa sale como *no medido*, nombrando el carril
  (`PAGES`, `HUB`, `TERMINAL`) al que hay que preguntarle;
* lo que está todavía en la cola —`SCHEDULED`, `RUNNING`,
  `WAITING_RATE_LIMIT`— tampoco bloquea: no ha servido, pero no ha fallado.

`COOLDOWN` queda fuera de esa exención a propósito: viene de fallar.

---

## 2 · BLOQUE 4 · EL SNAPSHOT DE MUROS

### El defecto

Las Walls se calculaban con lo que llegara **en el ciclo de sondeo actual**. Un
timeout dejaba «sin cálculo de muros en este ciclo»: una frase que no dice qué
ingrediente faltó ni si había muros válidos hace treinta segundos. Y ataba una
lectura **estructural** —que cambia despacio— al ritmo de un canal que falla a
menudo.

### Lo que cambia · `app/core/wall_snapshot.py` (NUEVO)

Los seis ingredientes —`GAMMA`, `OI`, `VENCIMIENTO`, `SPOT`, `COBERTURA`,
`MULTIPLICADOR`— en un objeto coherente, con tres salidas y ninguna vacía:

```
COMPLETO       → se calcula
LKG            → se publica el anterior CON SU EDAD   (fresco 180 s · tope 900 s)
NO_CALCULABLE  → «NO CALCULABLE · falta VENCIMIENTO, GAMMA, SPOT…»
```

En pantalla:

```
WALL_CONFIRMADA · LKG · edad 42s
WALL PROVISIONAL · STALE · edad 240s
NO CALCULABLE · falta VENCIMIENTO, GAMMA, SPOT, COBERTURA, MULTIPLICADOR
```

### El detalle que costaba una wall falsa

Un snapshot es coherente **o no es nada**. Al servir el LKG se sirve también
**su precio**: mezclar una cadena de hace cuarenta segundos con el spot de ahora
mide dos mercados, y el precio entra **al cuadrado** en la exposición. Se
detectó al escribir la regresión —el muro salía `None` sirviendo desde LKG— y
está atado.

**La fórmula no se toca.** `wall_gex` sigue siendo la única autoridad del
cálculo; este bloque prepara su entrada y guarda la última buena.

---

## 3 · BLOQUE 5 · DARK POOL · AHORA Y ÚLTIMO BUENO, SIN MEZCLAR

Un par `state`/`rows` respondía a dos preguntas distintas. Con un timeout y 216
filas del ciclo anterior, las dos lecturas posibles eran falsas.

Cinco hechos, cinco campos, más la conclusión:

```
current_status   qué pasó en ESTE refresco
current_rows     filas que trajo ESTE refresco (0 si falló)
lkg_rows         filas del último refresco que SÍ funcionó
lkg_age          cuántos segundos tiene
last_success_at  cuándo fue
serving          LIVE · STALE_LKG · NONE
```

```
HTTP 200 con cero filas   → NO_DATA        no hay actividad, no hay avería
timeout/5xx con LKG       → STALE_LKG      dato real, viejo, con su edad
timeout/5xx sin LKG       → PROVIDER_ERROR
```

Un 400 **no** se rebaja por tener filas antiguas.

---

## 4 · BLOQUE 7 · NUEVE ESTADOS, Y LA ANOMALÍA FUERA DE ELLOS

«Sin intentos» juntaba seis situaciones con cinco remedios distintos y un
defecto real escondido entre ellas.

```
SCHEDULED            su cadencia aún no vence
WAITING_RATE_LIMIT   exigible, presupuesto de cuota agotado
WAITING_DEPENDENCY   exigible, le falta un dato del que depende
COOLDOWN             en enfriamiento tras un fallo, con su backoff
RUNNING              petición en vuelo AHORA
LIVE                 sirviendo dato fresco
NO_DATA              respondió bien y no hay datos
STALE                sirve su último valor bueno, con la edad declarada
PROVIDER_ERROR       falla y no hay valor bueno que servir
```

Y aparte, **no como estado**: `NUNCA_LLAMADA`, para la herramienta exigible que
pasó el calentamiento sin un solo intento con la cuota libre. Un estado describe
a la herramienta; una anomalía acusa al programador.

### Dos decisiones de orden

* **`RUNNING` se comprueba antes que el resultado de la última llamada.** Al
  revés, una herramienta que está llamando ahora se declararía con el estado de
  su intento anterior.
* **Exigible y sin un solo intento es `SCHEDULED`, no `NO_DATA`.** Decir
  `NO_DATA` ahí atribuye al proveedor un silencio que es nuestro. Y la exención
  de la anomalía es por **cadencia**, nunca por estado.

`RUNNING` dejó de ser una deducción: el carril de páginas mantiene el conjunto
de claves **en vuelo**.

---

## 5 · DOS DEFECTOS ENCONTRADOS AL CABLEAR

Ninguno de los dos se buscaba.

1. **Una herramienta decía `LIVE` con el último intento fallido.** Bastaba con
   que quedara dato anterior publicado: `data["ready"]` se comprobaba antes que
   `tool.last_error`. Eso es servir el ciclo anterior con la etiqueta del
   actual. Ahora declara `DEGRADADO`/`NO_DISPONIBLE` y `lifecycle` dice `STALE`
   con la edad.

2. **La wall servida desde LKG salía sin precio** (ver bloque 4).

---

## 6 · QUÉ PUBLICA EL AUDITOR AHORA

`/api/quantdata/coverage` añade:

```
consumers          quién se queda sin qué, con su estado y su comportamiento
scheduler_states   recuento por estado + las anomalías, aparte
tools[].lifecycle  el estado real de esa herramienta, con su causa
tools[].anomaly    el defecto del programador, si lo hay
tools[].criticality  para quién es obligatoria y para quién opcional
```

---

## 7 · REGRESIÓN

`tests/test_v1590_criticidad_y_estados.py` · **54 casos**, un bloque por cajón,
más la integración de los cuatro en el Auditor y en el motor de muros. Entre
ellos, los que atan lo que no se puede volver a perder:

* el mismo dato obligatorio para uno y opcional para otro;
* lo no medido nunca se declara bloqueado;
* una dependencia en la cola no bloquea; en enfriamiento, sí;
* el LKG sirve su precio y su cadena, o no sirve;
* un LKG por encima del tope deja de sostener el muro;
* el snapshot de un activo no respalda a otro;
* una petición en vuelo manda sobre el resultado anterior;
* exigible y sin intentos no es que el proveedor calle;
* y `wall_gex` sigue siendo la única autoridad de la fórmula.

---

## 8 · AUDITORÍA DEL MOTOR

La auditoría del motor de esta release, con lo que **no** afirma, está en
`QUANT_ENGINE_AUDIT_v1.59.0.md`.

---

## 9 · LO QUE SIGUE PENDIENTE

* La evidencia **LIVE en Windows** del certificador de transporte de v1.58.0.
* **TRACE visual** sobre el Interval Map como autoridad: entrega aparte.
* **GLOBAL/LOCAL** por sección con subconjuntos de herramientas: entrega aparte.
* **Bloque 6** (unidades de exposición tipadas tras un adaptador): el último.

---

# Historial · v1.58.0 — Timeouts reales y concurrencia real

Release: `ITM_QUANT_v1.58.0_PRE_VPS` · Base: `v1.56.0` · Alcance: `MULTI_ASSET`

## 0 · BLOQUE 1+2 · EL TRANSPORTE, DE RAÍZ

### El defecto, demostrado en producción

La consola de Windows mostró ocho endpoints muriendo **exactamente a 5.0 s**:
`delta`, `gamma`, `net_flow`, `net_drift`, `market_share`,
`contract_trade_side_statistics`, `options_order_flow` y `options_order_flow_raw`.

El plazo «adaptativo» de v1.57.x no gobernaba esas llamadas. Tres líneas lo
explican:

```python
ENDPOINT_RUNTIME.set_default_timeout(self.settings.request_timeout_seconds)  # ← 5
_plazo = _rt.timeout()        # sin muestras → devuelve el default → 5.0
timeout=httpx.Timeout(self.settings.request_timeout_seconds)                 # ← 5
```

El arranque en frío salía de `QUANTDATA_TIMEOUT_SECONDS`, y el instalador
reparte `=5`. Aprender de ahí es lento **por construcción**: hacen falta ocho
muestras para calibrar, un timeout aporta una, y el cortacircuitos abre a los
cuatro fallos seguidos con esperas que se duplican. En una sesión recién
arrancada, los endpoints pesados mueren a 5.0 s durante minutos.

Y `httpx.Timeout(plazo)` ponía **el mismo número en connect, read, write y
pool**: un endpoint que conecta en 80 ms y calcula en 9 s se cortaba por lectura
con un presupuesto pensado para la conexión.

### Lo que cambia

```
warm start        12 s de POLÍTICA, no del .env
connect / read    dos plazos, dos causas, dos escalas
autoridad         p95 medido por endpoint
EWMA              solo declara DERIVA, nunca fija el plazo
concurrencia      techo de peticiones VIVAS, compartido por los dos carriles
clases            ligero / pesado, con techo propio para las pesadas
escalonado        por reserva de turno, no por «mirar y dormir»
retry             una vez, y solo con las cinco condiciones
telemetría        queue_wait_ms · request_ms · total_ms · timeout_budget_ms
```

### `QUANTDATA_TIMEOUT_SECONDS` deja de ser la autoridad

No se elimina —hay `.env` en marcha con ella— pero cambia de significado y **se
declara en el Auditor**:

* valor **más largo** que el warm start → se respeta (alguien pide paciencia);
* valor **más corto** → se **ignora**, y la pantalla dice que se ignoró.

Nadie tiene que editar su `.env` para que el producto se comporte bien.

### El límite de concurrencia no es opcional

Respetar 240/60 s y 20/1 s **no basta**: veinte peticiones en un segundo cumplen
el contrato y, si las veinte son mapas por intervalo, están las veinte **vivas**
a la vez compitiendo entre ellas. La congestión la provocamos nosotros y se lee
como lentitud del proveedor.

Y subir plazos **sin** límite de concurrencia lo empeora: los sockets viven más
y se solapan más. Por eso los bloques 1 y 2 son una sola medida.

El escalonado tenía además un defecto propio que la suite destapó: comprobar
cuánto ha pasado desde el último arranque y dormir la diferencia **no
serializa** —tres pesadas leen el mismo instante y despiertan juntas—. Ahora
cada una **reserva** su hueco sobre un reloj que sólo avanza.

### El reintento tiene presupuesto, no solo jitter

Reintentar un timeout duplica la petición contra la misma cuenta. Las cinco
condiciones, y el motivo cuando falta alguna:

```
clase reintentable · cortacircuitos cerrado · fuera de la ráfaga
hueco de concurrencia AHORA · cabe en lo que queda de ciclo
```

Nunca para 400/401/403/404. Uno por endpoint y por ciclo.

### Dónde se va el tiempo

`queue_wait_ms` es congestión **nuestra**; `request_ms` es lentitud **suya**. Sin
separarlas, «tardó nueve segundos» no distingue las dos, y los arreglos son
opuestos: al proveedor lento se le da más plazo; a la cola propia, menos
concurrencia. Panel nuevo en AUDITOR: **QUANT DATA · TIEMPOS Y CONCURRENCIA**.

### Lo que esto NO demuestra

44 pruebas de caos —timeout, 429, 5xx, cuerpo vacío, respuesta lenta,
recuperación, aislamiento— reproducen los fallos **sin tocar la red**. Demuestran
la conducta del código; no pueden demostrar la del proveedor.

Las siete afirmaciones del bloque se cierran **sólo** con tráfico real:

```
CERTIFICAR_TRANSPORTE.bat        →  JSON + MD con las siete medidas
```

Mide masa de timeouts por plazo (la firma del 5.0 s), concurrencia máxima
observada, recuperación tras timeout, aislamiento entre endpoints y entre
activos, y el reparto cola/proveedor. **Ningún criterio LIVE se cierra aquí.**

---

## 1 · Los muros, la suma por contrato y el nombre del control

**No cambia la metodología de walls.** El concepto de Call Wall y Put Wall es el
mismo de v1.57.0; esto cierra tres condiciones del contrato que no estaban
cumplidas al pie de la letra.

---

### LAS TRES CONDICIONES DEL CONTRATO (v1.57.2)

### 1 · El GEX se SUMA por contrato dentro del strike

```
GEX_strike = Σ( gamma_i × OI_i × multiplicador_i × precio² × 0.01 )
```

El cálculo ya era por contrato —nunca `gamma agregada × OI agregado`— pero el
código **asignaba en vez de acumular**:

```python
slot["gex"] = round(gex, 6)      # ← el último contrato ganaba
```

Con un contrato por (vencimiento, strike, tipo) el número salía bien, y eso es
precisamente lo que hacía peligroso el atajo: **funciona hasta que hay dos**. Un
segundo contrato en el mismo strike —una mini junto a la estándar— se perdía en
silencio.

Tres cosas cambian con la suma:

* **el multiplicador es por contrato** (`multiplicador_i`): una mini de 10 ya no
  se cuenta como una estándar de 100;
* **la identidad del contrato** es su símbolo de opción cuando viene, y si no
  (vencimiento, strike, tipo, **multiplicador**). Con la clave corta, una mini y
  una estándar del mismo strike colisionaban y una desaparecía;
* **la gamma publicada del strike** es la media ponderada por interés abierto,
  que es la única que reproduce la suma: `Σ(γᵢ·OIᵢ) = γ_pub × OI_total`. Con
  cualquier otra media, los cinco números del panel no cuadrarían entre sí.

La prueba que lo ata mide los tres caminos sobre los mismos datos:

```
Σ(γᵢ·OIᵢ)              = 0,10·100 + 0,01·1000 = 20      ← el correcto
γ sumada × OI sumado   = 0,11 · 1100          = 121     ← seis veces más
γ media  × OI sumado   = 0,055 · 1100         = 60,5    ← tres veces más
```

### 2 · Put Wall por MAYOR VALOR ABSOLUTO, con la exposición firmada

La fórmula toma `|gamma|`, así que el GEX publicado ya era una magnitud y el
orden ya era por módulo. Lo que **faltaba era la prueba** que lo garantice
cuando el proveedor firma el lado:

```
strike 95 → gex_signed = −2.000.000      ← el muro
strike 97 → gex_signed =   −100.000
```

Ordenar por el signo crudo elegiría **97**, porque −100.000 > −2.000.000: la put
más pequeña. Ese error sólo se ve con un lado firmado, y ahora hay una regresión
construida exactamente para provocarlo.

El muro publica además `gex_signed` —negativo en puts bajo la convención
declarada— y `selection: MAYOR_VALOR_ABSOLUTO_DEL_LADO`. El signo viaja para
poder leerlo; **no** para ordenar.

### 3 · `STRIKES_FUERA_DEL_DINERO` → `TODOS_LOS_STRIKES_DEL_VENCIMIENTO`

El requisito es usar **todos** los strikes del vencimiento definido, incluidos
los que están lejos del precio. El nombre anterior sugería lo contrario
—quedarse sólo con los que están fuera del dinero— cuando lo que medía era la
cobertura de la cadena. Un control cuyo nombre describe una regla distinta de la
que aplica es peor que no tenerlo: **se cita el nombre**.

El ranking nunca filtró por precio, pero eso había que creérselo. Ahora se
**demuestra**: el control compara los strikes que existen en el vencimiento por
lado con los que entraron en el ranking y publica `descartados_por_precio`, que
tiene que ser cero. Por eso el ranking pasó a calcularse **antes** de la
auditoría: auditar primero obligaba a suponer el filtro en vez de medirlo.

Y sigue en pie lo de v1.57.0: si el máximo de un lado cae al otro lado del
precio, se publica con `crossed: true` y su posición declarada, no se filtra.

---

## 2 · RUNTIME CERTIFICADO · un Python que Windows puede instalar

---

### 3.12.14 → 3.13.12

### El gate exigía un Python que python.org no distribuye para Windows

```
.python-version      3.12.14      ← lo que el gate exigía
INSTALAR_WEB.bat     3.12.10      ← lo que el instalador ponía, a mano
```

La rama 3.12 está en fase de **solo seguridad**: sus releases se publican
*source-only* (PEP 693) y python.org **no distribuye instalador de Windows desde
3.12.10** (abril 2025). Para certificar en Windows había que compilar CPython a
mano o instalar un binario no oficial, y las dos cosas destruyen exactamente lo
que el gate existe para garantizar.

Y el repositorio **ya se había contradicho** para salir del paso: se instalaba
3.12.10 y después el gate rechazaba certificar ese mismo entorno. El comentario
del `.bat` lo llamaba «Windows hotfix» y dejaba el gate intacto. No lo detectó
nadie porque **ningún control comparaba los dos ficheros**.

### La determinación, medida antes de tocar el control

```
cobertura de ruedas del lock de Windows (38 paquetes, contra PyPI)
  cp312   38/38      cp313   38/38      cp314   37/38  (falta pandas)

instalación de los cinco locks en CPython 3.13.12
  --require-hashes --no-deps --only-binary=:all:  →  0 errores, 0 compilaciones

regresión completa en CPython 3.13.12
  2989 passed, 49 skipped, 0 failed
```

**Opción 1 —certificar 3.12.10—** estaba disponible y era mínima, pero congelaba
Windows en un intérprete de abril de 2025 que **no puede recibir ninguna
corrección de seguridad más en forma de binario oficial**: 3.12.11–3.12.14 son
source-only y la rama seguirá así hasta 2028.

**Opción 2 —migrar de rama— es la que corresponde**, y los locks ya la cubrían
sin regenerar nada. El pin es **3.13.12 y no 3.13.15**, la última de la rama,
porque 3.13.12 es la versión en la que se ha corrido la regresión: el pin no va
por delante de la evidencia. Moverlo es una línea y una re-ejecución.

**3.14 queda nombrado, no descartado:** lo bloquea `pandas==2.2.3`, que no
publica rueda `cp314` win_amd64; la primera que la publica es **2.3.3**.

### El control nuevo · `windows_runtime_guard()`

No baja el control: antes exigía una versión exacta sin preguntarse si existía
para la plataforma de producción. Ahora exige lo mismo **y además** que exista:

1. `.python-version` y `.python-runtime.json` declaran la misma versión;
2. su rama está declarada certificable en Windows;
3. el parche no es posterior al último con instalador de esa rama;
4. la declaración **caduca** (`revisar_antes_de`): cuando la rama sale de la fase
   de corrección de errores, el control vence en vez de callarse;
5. `INSTALAR_WEB.bat` **lee** el pin del mismo fichero que el gate, así que la
   contradicción no se puede reintroducir.

Corre **antes** del preflight de toolchain: si el runtime no existe para la
plataforma, lo demás no significa nada.

La determinación entera, con su evidencia, en `docs/RUNTIME_CERTIFICADO.md`.
La rama 3.13 hay que revisarla **antes del 2026-10-31**.

---

## 3 · HISTORIAL · los muros, con su fórmula y su veredicto

### CAMBIO DE FÓRMULA DECLARADO (v1.57.0)

**Una fórmula sí cambia**, y con ella un número en pantalla: el strike de Call
Wall y Put Wall. El método anterior —media geométrica de la exposición agregada
del proveedor y el interés abierto— y el nuevo —máximo de Gamma Exposure del
lado, `gamma × OI × multiplicador × precio² × 0.01`— **no coinciden**, y el
motivo está en la sección 1.

Qué **no** cambia, para que el alcance quede acotado en vez de quedar a la
imaginación del lector:

* no se modifican los **pesos del Scanner**;
* no se modifica la **autoridad direccional** ni ninguna de sus componentes;
* no se toca Gamma, Delta, GEX, DEX, VEX, CHEX, Vanna, Charm ni el cálculo de
  griegas por contrato: el muro CONSUME esas magnitudes, no las redefine;
* no cambia el Max Pain, ni el Zero Gamma, ni los centroides de delta y gamma.

La auditoría del motor de esta release está en `QUANT_ENGINE_AUDIT_v1.57.0.md`.

El changelog de v1.56.0 vive en el historial de git (`CHANGELOG_v1.56.0.md`, en
`fe8534c` y anteriores).

---

### CALL WALL Y PUT WALL · el contrato, entero (v1.57.0)

### La fórmula, y sólo la fórmula

```
Gamma Exposure por strike = gamma × OI × multiplicador × precio² × 0.01

Call Wall = strike con MAYOR Gamma Exposure de CALLS
Put Wall  = strike con MAYOR Gamma Exposure de PUTS
```

Los dos lados se mantienen **separados de principio a fin**. No hay un momento
del cálculo en que se sumen, se resten o se comparen entre sí.

### Qué se hacía antes, y por qué era otro número

El muro se elegía con una **media geométrica** de la exposición ya agregada que
publica el proveedor y el interés abierto del strike:

```
score = 100 · exposición_lado^(1−w) · interés_abierto_lado^w      w = 0.5
```

El defecto no se ve en el resultado, y eso es lo peor que puede tener un defecto:
**el OI ya va DENTRO de la fórmula de exposición**. Multiplicar otra vez por él lo
cuenta dos veces y desplaza el muro hacia strikes con mucho libro abierto y gamma
pequeña. Daba un número grande, plausible y en el strike equivocado.

Con las griegas **por contrato** —gamma, OI, IV y delta, que Quant Data publica
por contrato— la exposición se calcula entera y el muro es, literalmente, su
máximo por lado.

### Cómo NO se calcula

Cinco métodos que se parecen y dan otra respuesta. Cada uno tiene una prueba
construida para que el método equivocado **gane** si alguien lo reintroduce: la
cadena de prueba pone el mayor OI, el mayor volumen, el neto más grande y el
centro de masa del OI en strikes DISTINTOS al muro.

```
NO es el strike con mayor OI          el OI va dentro de la fórmula
NO es el strike con mayor volumen     el volumen es rotación, no libro abierto
NO es la gamma NETA                   un strike con mucha call y mucha put gamma
                                      tiene neto pequeño, y es donde más cobertura hay
NO es el Max Pain                     otra pregunta y otro número
NO se mezclan vencimientos            mezclar es legítimo si se declara; en
  sin declararlo                      silencio da un nivel sin dueño
```

### Un contrato, una sola vez

Las griegas por contrato llegan dentro de las filas de order flow, que son
**operaciones**. Cien prints del mismo contrato sumados darían cien veces su
interés abierto: un muro de la nada con un número grande y creíble. El OI y la
gamma son propiedades DEL CONTRATO, así que se deduplica por
`(vencimiento, strike, tipo)`.

### El vencimiento se elige y se dice

El muro usa el vencimiento operativo que ya resolvió la terminal
(`EXPIRY_SELECTION`), no uno propio: una wall con fecha distinta a la de la
cadena que el operador mira es una wall de otro mercado. Si ese vencimiento no
está en la cadena se cae a la política declarada —más cercano, o viernes
semanal— y **se dice cuál se usó y por qué**, en vez de dejar la pantalla sin
muros por un desajuste de fechas.

### WALL CONFIRMADA · WALL PROVISIONAL

El veredicto **no califica al muro: califica a los datos** con los que se
calculó. Los diez controles, uno a uno, con su evidencia:

```
 1  CADENA_COMPLETA               sin huecos en la escalera de strikes
 2  VENCIMIENTO_DEFINIDO          uno, declarado, y no mezclado
 3  STRIKES_FUERA_DEL_DINERO      cobertura a los dos lados del precio
 4  OI_POR_STRIKE_Y_LADO          calls y puts por separado
 5  GAMMA_VALIDA_POR_CONTRATO     gamma finita en cada contrato
 6  PRECIO_CON_HORA               con su hora de captura
 7  IV_DELTA_MULTIPLICADOR        para poder validar la gamma
 8  CONVENCION_DE_POSICIONAMIENTO declarada, no supuesta
 9  SIN_DATOS_VIEJOS_MEZCLADOS    nada por encima del máximo de edad
10  MISMA_HORA_MISMA_FUENTE       OI, gamma y precio del mismo instante
```

Los diez pasan → `WALL CONFIRMADA`. Falta uno → `WALL PROVISIONAL`, **y se
nombra cuál**. Un muro provisional se sigue publicando y se sigue pudiendo
operar; lo que no se hace es presentarlo como si la cadena estuviera completa.

### El precio viaja con su hora

La Gamma Exposure lleva el precio **al cuadrado**, así que un precio de hace
cinco minutos no es «casi el mismo número»: es un muro medido sobre otro
mercado. Sin la hora, el requisito de «misma hora para OI, gamma y precio» no se
podía comprobar porque a una de las tres entradas le faltaba la hora.

### Se puede ver desde la pantalla

El cálculo de los muros ya viajaba en el JSON del Auditor y **no se pintaba en
ninguna parte**: para comprobar una wall había que abrir la respuesta a mano. Dos
paneles nuevos en AUDITOR:

* **MUROS · CÁLCULO Y VEREDICTO** — los cinco números que sostienen cada muro
  (strike, gamma, OI, precio, hora), el vencimiento, el GEX, el margen sobre el
  siguiente strike y el veredicto.
* **MUROS · LOS DIEZ DATOS QUE EXIGE EL CONTRATO** — control a control, con el
  detalle de qué falta cuando falta.

### Lo que un muro no es

Una wall es una **concentración de cobertura probable, no una barrera
garantizada**. La fórmula dice dónde tendría que ajustar más el dealer si el
precio llegara allí; la reacción real depende además de la posición de clientes y
dealers, que el proveedor no publica. Por eso la convención de posicionamiento se
**declara** en el resultado —`CLIENTE_LARGO_OPCIONES__DEALER_CORTO_GAMMA`— y el
control 8 dice explícitamente `measured_dealer_inventory: false`.

### La vía anterior no se borra

Sostiene la pantalla cuando el proveedor no publica griegas por contrato, y entra
**etiquetada** como respaldo: `fallback_used`, con su método propio y sin
veredicto. Nunca disfrazada del resultado principal.

---

## 4 · PLAZOS · primer intento, incompleto

El registro del motor, media hora seguida, con la cuota en **7 de 240**:

```
degradado en data_hub:dark_flow:late          [DEGRADED]: Quant Data request timed out
degradado en data_hub:gamma:late              [DEGRADED]: Quant Data request timed out
degradado en data_hub:max_pain:late           [DEGRADED]: Quant Data request timed out
degradado en data_hub:interval_map_delta:late [DEGRADED]: Quant Data request timed out
```

Y en pantalla: los dos carriles de dark pool en `STALE` con «el canal tardó más
de 6.0 s», media docena de herramientas en `DEGRADADO`, y la cobertura por canal
entre el **27 %** y el **62 %**. El Auditor decía la verdad —los canales no
respondían— pero el culpable no era ninguno de los que señalaba la pantalla: la
cuota estaba intacta, la autorización era correcta y el proveedor contestaba.

### La cadena

El instalador reparte `QUANTDATA_TIMEOUT_SECONDS=5`, y ése era el plazo de
**todas** las peticiones, de las treinta y seis herramientas y de los dos
carriles. El plazo del canal se calculaba como ese valor **+ 1**: los `6.0 s`
exactos que enseñaba la pantalla no eran una coincidencia, eran aritmética.

Los endpoints pesados del proveedor —`interval-map`, `max-pain-over-time`,
exposición por vencimiento, `dark-flow`— no contestan en cinco segundos. Morían
por plazo en cada ciclo.

Y no había salida. `record_failure` no toca las latencias medidas, y eso es
correcto para un 500 o un 404: no dicen nada sobre cuánto tarda el endpoint
cuando funciona. Pero un **timeout sí dice algo**, y era justo lo que se tiraba.
Un endpoint que necesita doce segundos, llamado con cinco, no dejaba NUNCA una
muestra, así que nunca alcanzaba las ocho que hacen falta para calibrar, así que
se le seguía llamando con cinco. Para siempre. El techo de veinte segundos que
`endpoint_runtime` publica era **inalcanzable por construcción**, y en pantalla
se leía como un proveedor caído.

Encima el plazo calibrado no llegaba a la petición. `QuantDataClient` se
construía con un plazo fijo y `post()` no aceptaba otro, así que el plazo por
endpoint sólo gobernaba el reloj del **ciclo**. Cuando el calibrado bajaba del
configurado —un endpoint rápido, p95 de 200 ms— el ciclo se rendía a los dos
segundos, la petición huérfana seguía viva ocupando conexión y cuota hasta los
cinco, y al morir soltaba un **segundo** aviso `DEGRADED` por el mismo hecho. De
ahí los `:late` del registro: una incidencia contada dos veces.

### Lo que cambia

```
plazo de la petición    lo pasa el llamador · QuantDataClient.post(..., timeout=)
plazo del ciclo         plazo de la petición + CHANNEL_SLACK_S
autoridad del plazo     shared.ENDPOINT_RUNTIME, para los DOS carriles
un timeout              cota inferior de latencia: el plazo siguiente SUBE
plazo repartido         5 s → 12 s (12 + 1 caben en el ciclo de 15 s del motor)
```

Un timeout no dice cuánto tarda el endpoint; dice que tarda **más** que el
plazo. Eso es una cota inferior y como tal se guarda: el plazo sube —acotado por
el techo de 20 s— hasta que el endpoint contesta y sus latencias reales lo
vuelven a bajar. Lo que **no** hace es llamar para siempre: el timeout sigue
contando para el cortacircuitos, que abre a los cuatro fallos seguidos.

Eso arregla también las instalaciones que ya tienen el `5` escrito en su `.env`:
el plazo se corrige solo, sin que el operador toque un fichero.

El carril del **motor** entra en el mismo régimen. Era el que más `:late`
acumulaba —`gamma`, `delta`, `max_pain`, `iv_rank`— y no tenía plazo medido
porque el registro vivía en el carril de páginas. Con una trampa que costaba
caro: en ese carril `ready` no significa «respondió», porque puede venir del
último valor bueno. Anotar eso como éxito metía una latencia de microsegundos en
la calibración y hundía el plazo del endpoint **justo cuando va lento**. Sólo se
anota éxito cuando la procedencia es `LIVE`.

El aviso `:late` deja de ser una degradación: el ciclo ya contó ese fallo al
agotarse el plazo del canal. El texto del error se conserva —es donde se ve qué
plazo expiró— al nivel de lo esperado, no al de lo averiado.

### Dos rojos que venían de antes

`F821` en el gate de release, los dos silenciosos porque `from __future__ import
annotations` no evalúa las anotaciones: `List` anotado y nunca importado en
`intelligence.py`, y `FaltaRequisito` importado dentro de **otra** prueba, así
que el `except` que la prueba existe para comprobar habría dado `NameError` justo
al cumplirse.

### Verificación

```
suite            2950 passed, 51 skipped, 0 failed
ruff del gate    E9,F63,F7,F82 → All checks passed (venía con 2 F821)
arranque en frío 0 degradaciones
smoke test       / · /legacy · /api/assets · /api/terminal/bundle · /api/state · /health → HTTP 200
```

24 regresiones nuevas en `tests/test_v1581_plazo_del_transporte.py`. Inventario:
**3001 casos / 189 ficheros**.

## Verificación de v1.58.0

```
intérprete       CPython 3.13.12 · el certificado
suite            3068 passed, 49 skipped, 0 failed
caos             44 pruebas nuevas en tests/test_v1580_caos_transporte.py
ruff del gate    E9,F63,F7,F82 -> All checks passed
eslint           no-undef limpio · sintaxis OK
arranque en frío 0 degradaciones · 6 rutas -> HTTP 200
```

**Pendiente, y dicho como tal:** las siete afirmaciones LIVE del transporte
—incluida «ninguna llamada muere sistemáticamente a 5.0 s»— **no están
cerradas**. Se cierran ejecutando `CERTIFICAR_TRANSPORTE.bat` en Windows contra
la API real. El manifiesto de release lo registra como
`live_certification.status: PENDIENTE_DE_EJECUCION_EN_WINDOWS`.

Bloques **3, 4, 5, 7** (criticidad por consumidor, WallSnapshot, dark pool con
estados separados, máquina de estados del scheduler) y el **6** (unidades
tipadas detrás de un adaptador) no entran en esta versión.

Inventario: **3117 casos / 192 ficheros**.

---

## Verificación de v1.57.2

```
intérprete       CPython 3.13.12 · el certificado, no otro
locks            bootstrap · production · test · rust-bridge instalados con
                 --require-hashes --no-deps --only-binary=:all: · 0 compilaciones
suite            3023 passed, 49 skipped, 0 failed
ruff del gate    E9,F63,F7,F82 -> All checks passed
eslint           no-undef limpio · sintaxis OK (5 módulos)
arranque en frío 0 degradaciones
smoke test       / · /legacy · /api/assets · /api/terminal/bundle · /api/state · /health -> HTTP 200
versión activa   1.57.2 en VERSION.txt, marcador, rust, frontend y documentos
```

15 regresiones nuevas en `tests/test_v1570_contrato_de_muros.py` para las tres
condiciones del contrato de walls —la suma por contrato frente a los dos atajos
de agregación, el módulo frente al signo crudo, y la prueba de que no se descarta
ningún strike por su distancia al precio—, 19 en `tests/test_v1571_runtime_windows.py` —el guardián
rechaza cada forma del defecto: rama source-only, parche sin binario, pin y
declaración discrepando, declaración caducada e instalador con la versión
escrita a mano—, 37 en `tests/test_v1570_contrato_de_muros.py` y 24 en
`tests/test_v1581_plazo_del_transporte.py`. Inventario: **3072 casos / 191
ficheros**.

Lo que esta release **no** demuestra: que los muros que salen ahora sean los que
frenen al precio, que los endpoints lentos del proveedor contesten dentro del
plazo nuevo, ni que el instalador de Windows funcione en una máquina Windows
real —la migración de runtime se verificó en Linux con el mismo intérprete y los
mismos locks; el `.bat` no se ha podido ejecutar aquí—. Lo primero depende del posicionamiento real de clientes y dealers,
que ningún proveedor conectado publica; lo segundo exige la API real, y este
entorno no tiene credenciales ni salida a `quantdata.us`.

---

# Historial · v1.56.0 — Cierre integral por gates

> Incorporado al renumerar: la raíz sólo admite el changelog de la
> release vigente, así que este documento es acumulativo. El título
> original era: ITM QUANT MULTI ASSET · v1.56.0 — Cierre integral por gates

Release: `ITM_QUANT_v1.56.0_PRE_VPS` · Base: `v1.55.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.**

---

## GATE 1 · El lado agresor, con su orden de autoridad

```
1. tradeSideCode        el campo OFICIAL. Si viene, decide y se acabó.
2. otro campo de lado   declarado por el proveedor
3. NBBO                 medición, no heurística
4. UNKNOWN              y se dice por qué

ASK / ABOVE_ASK → BUY    BID / BELOW_BID → SELL    MID_MARKET → UNKNOWN
```

El paso 1 va aparte del 2 **a propósito**. Si `tradeSideCode` dice `MID_MARKET`,
la respuesta es `UNKNOWN` y **no se cae al NBBO** aunque el precio toque el ask.
El proveedor ya ha dicho que nadie cruzó el spread; volver a preguntárselo al
precio es discutirle el dato oficial hasta que conteste lo que queremos oír, y
así es exactamente como se fabrica una flecha inventada.

**Nunca `CALL = BUY` ni `PUT = SELL`.** Una put se compra, y eso es una COMPRA.

Cada operación conserva 17 campos: `trade_id`, `tradeTime`, `ticker`,
`option_symbol`, tipo, `strike`, `expiration`, `DTE`, `optionPrice`, `bidPrice`,
`askPrice`, `size`, `premium`, `trade_side_code`, `aggressor`,
`classification_source` y `classification_reason`.

`MID_TRADE` deja de contarse como avería. Una cinta con muchas ejecuciones al
punto medio es una cinta **sana**; hasta ahora producía el mismo mensaje que una
a la que le falta el campo, y son dos cosas con arreglos opuestos.

### La tabla de evidencia

`aggressor_evidence` recorre la cadena operación por operación y exige que el
lado sea el mismo en RAW, clasificador, marca de FLUJO y marca de TRACE. La
columna «esperado» **reimplementa la regla del contrato a mano**, sin llamar al
clasificador: compararlo consigo mismo no demuestra nada. Un test inyecta una
inversión y comprueba que la tabla FALLA.

## GATE 2 · La marca, en un solo sitio

Las cinco funciones de la marca vivían **duplicadas** en TRACE y en FLUJO. Eran
equivalentes el día que se escribieron, y ese es el problema: dos copias
equivalentes se separan en cuanto alguien corrige una, y una marca que significa
COMPRA en una pantalla y otra cosa en la de al lado es peor que no dibujarla.

```
círculo dorado  = concentración (NO dirección)
flecha verde ▲  = compra demostrada
flecha roja  ▼  = venta demostrada
rombo neutro    = lado no demostrable
```

El hover lleva los tres porcentajes **sobre toda la prima de la ventana** —si el
40 % no tiene lado, se ve ese 40 %— y de qué campo salió la mayoría de los
lados, ponderado por prima.

## GATE 3 · Las barras que faltaban

El síntoma: `VOLUMEN SUBYACENTE` lleno, `AGRESOR → SIN FLUJO DIRECCIONAL`,
`PRIMA → SIN PRIMA OBSERVADA`, todo a la vez.

La causa: los tres carriles se alimentaban de sitios distintos. El volumen sale
de las **velas**; el agresor y la prima salían de `trace.option_prints`, en el
cliente. Ese campo llegaba vacío aunque `order-flow` hubiera devuelto cientos de
operaciones. En pantalla eso se lee como «hoy no hubo flujo de opciones» —una
conclusión sobre el mercado— donde había una ruta rota.

Ahora se agrupan en el servidor, desde la misma cinta que las tarjetas, con LKG
por carril. El recuento de la cadena se publica etapa por etapa, y si el
proveedor trae filas y salen cero barras se declara **fallo de integración**.

## GATE 4 · Auditoría y ocho carriles

Los diez contadores con sus nombres y los siete estados, armados desde la
**misma** cinta y la misma atribución que dibujan las marcas: recalcularlos
aparte daría un segundo veredicto que podría discrepar del que se está viendo.

Ocho carriles declarados: `tape`, `qflow`, `net_flow`, `net_drift`, `premiums`,
`prints`, `volume`, `aggressor`. `last_known_good` se publica **aparte** de
`current`: cuando el carril está vivo coinciden, y cuando está viejo `current`
ES el LKG, cosa que quien lea la respuesta tiene que poder saber sin deducirla.

## GATE 5 · Net Drift: por qué los muros «no aparecían»

```js
const px = vis.filter(v => Q.isNum(v.price));
if (px.length > 1) { ...aquí dentro iba TODO: precio, muros y QFLOW... }
```

`v.price` es `stockPrice`, un campo que el proveedor no siempre publica. Sin él,
el bloque entero se saltaba. Parecía que los muros no existían cuando lo que
faltaba era una columna de **otro** dataset.

Ahora el precio cae a las velas —mismo subyacente, mismo reloj— y se rotula
`PRECIO DE VELAS`. Sigue habiendo **exactamente un** eje de precio.

## GATE 6 · Cómo se calcula un muro

Un muro dibujado es una afirmación sobre el mercado. Si su único respaldo es que
hay una línea en el gráfico, no hay forma de discutirla.

```
score = 100 · exposición_lado^(1−w) · interés_abierto_lado^w
```

**Geométrica**, no aritmética: un strike con mucha exposición y nada de libro
abierto no sostiene un muro, y la media aritmética lo premiaría igual. Sin OI
utilizable no se multiplica por cero —eso afirmaría que no hay libro, no que no
se sabe—. La igualdad se verifica en las **seis** secciones.

## GATE 7 · TRACE

Área central de 780 a 940 px. Un muro lejano sigue sin poder deformar la escala:
con QQQ en 722 y un PUT WALL en 700 no se abren veintidós dólares vacíos; el
muro se ancla al borde con flecha, precio y distancia.

## GATE 8 · Interval Map: una celda ausente no es un cero

En `normalize_matrix`, una celda que el proveedor no publica se convertía en
`0.0`. Ese cero viajaba como si fuera una medición: entraba en el percentil,
contaba como «celda observada sin exposición» y llegaba al renderizador
indistinguible de un cero real. Una zona no publicada se dibujaba como una zona
medida y vacía.

`interval_map_audit` compara RAW → canónico → signo → intensidad y exige que el
signo se conserve. Atenuar por el suelo de ruido **no** cuenta como inversión.
Dos tests inyectan el defecto y comprueban que la auditoría falla.

## GATE 9 · Monte Carlo congelado y las acumulaciones auditadas

Nueve pruebas contra soluciones **cerradas** pasan al gate de release, antes que
la suite particionada: si la matemática está rota, el resto no significa nada.

Las acumulaciones, una a una y sin reemplazo global:

| Fichero | Veredicto |
|---|---|
| `aggression_delta` | **VÁLIDO** · cada `or 0.0` es una contribución |
| `qflow` | **VÁLIDO** · sumas sobre operaciones |
| `dealer_intelligence` | **CORREGIDO** · un componente ausente puntuaba 0 con todo su peso |
| `market_state_field` | **CORREGIDO** · sin exposición afirmaba `TRANSITION` |
| `nextgen_terminal` | **CORREGIDO** · `spot or 0.0` ordenaba los strikes por distancia a CERO |

Pendientes: **0**.

## GATE 10 · Dos huellas y un commit transaccional

```
generation_id   ¿DE QUÉ activo es esta respuesta?
cycle_id        ¿es la MISMA respuesta que la anterior?
```

Descartar la generación ajena no basta: durante la hidratación el bundle llega
con el símbolo nuevo y secciones a medio llenar. `Net Drift de QQQ + GEX todavía
de DIA + muros antiguos` son tres lecturas de tres momentos distintos
presentadas como una sola foto del mercado. El snapshot sólo se publica cuando
los datasets críticos ya son del activo nuevo.

---

## Lo que NO alcanza esta release

Los puntos 5, 10, 18, 25, 27, 37, 38 y 53 exigen la API real con ocho activos.
El entorno de desarrollo no tiene credenciales ni salida a `quantdata.us`. Las
herramientas existen:

```
python scripts/verify_live_quantdata.py --cierre
```

El estado por módulo, con los cinco niveles y sin inflar ninguno, está en
`docs/operations/ESTADO_v1.56.0.md`.

---

## REVISIÓN ADICIONAL SOBRE `df2ca9d`

- Eliminados alias muertos del frontend (`flowArrow`, `flowAmount`, `markerStrength`, `markerAmount`, `evStrength`, `LEVEL_STYLE`) que quedaron tras centralizar QFLOW.
- `TRACE`, `FLUJO` y `NET DRIFT` siguen usando una sola autoridad visual: `ITMQ.flowMark`.
- La marca QFLOW ahora escribe también `BUY`, `SELL` o `UNKNOWN` junto al importe y usa una flecha completa (asta + punta), para que la dirección no dependa sólo del color ni de una cuña diminuta.
- El Interval Map conserva la diferencia entre celda RAW medida y hueco. Una observación real no nula que cae bajo el suelo de concentración queda tenue en vez de desaparecer a alfa 0; un `MISSING` no se promociona a observación.
- Se mantuvo el contrato matemático de Monte Carlo sin cambiar de modelo; sus pruebas contra solución cerrada continúan pasando en la suite focalizada.
- El manifiesto e inventario se resincronizaron a 2.558 casos / 171 ficheros tras añadir regresiones para las nuevas garantías visuales.

## REVISIÓN SOBRE `9fabecd` · El envejecimiento del programador, entero

El reparto por espera de v1.57.0 quitó el hambre de la cola pero quedó a medias
en dos sitios que sólo se ven leyendo el archivo entero:

- **La espera de una herramienta nunca servida se medía desde 1970.**
  `self._fetched_at` se vacía en cada cambio de activo, y el defecto del `get`
  era `0.0`: la espera no era «cero segundos» sino `now` —unos 1.700 millones—,
  así que **todas** las herramientas sin servir caían al suelo de prioridad en el
  primer ciclo y el orden de carga dejaba de existir justo cuando importa, en
  frío y tras cambiar de activo. Medido sobre el catálogo real: `oi_by_strike`
  (prioridad 0) salía en el **puesto 20 de 36** y `dark_flow` (prioridad 1) en el
  **32**; ahora salen en el 4 y el 9. Hay una referencia explícita,
  `_eligible_since`, que se reinicia con el activo.
- **La ráfaga de arranque filtraba por la prioridad BASE** mientras el lote
  ordenaba por la EFECTIVA. Una herramienta ya ascendida por espera a la clase
  que dibuja la pantalla seguía excluida de la ventana de arranque. Ahora hay una
  sola regla, `in_burst_class`, y una prueba que impide que vuelvan a ser dos.

Y una tercera que no era de cálculo sino de información: **«sin intentos» no es
un diagnóstico**, es la ausencia de uno. El Auditor publica ahora el estado del
programador por herramienta —espera, prioridad base → efectiva, exigible,
enfriamiento, nunca servida— y el veredicto `SIN_INTENTAR` distingue las tres
causas con sus tres remedios: enfriamiento tras un fallo, cadencia todavía no
vencida, o exigible sin presupuesto de cuota. El frontend lo enseña en la misma
línea en vez de quedarse en «sin intentos · 1 rutas candidatas».

28 regresiones nuevas fijan estas rutas, incluida la que mide el defecto de la
sentinela en vez de describirlo. Inventario: **2977 casos / 188 ficheros**.

## REVISIÓN SOBRE `293bdb9` · El .bat de Windows estaba roto

Revisando el procedimiento LIVE antes de pedir que se ejecute, dos errores que
lo habrían hecho fallar **en la máquina del operador**:

- `>/dev/null 2>&1` es Linux. En `cmd.exe` no silencia nada: intenta redirigir a
  una ruta inexistente, el comando falla, y **las cuatro comprobaciones previas
  quedaban inservibles**.
- El chequeo de que la terminal responde iba escrito como
  `os.environ[\x27ITMQ_BASE_URL\x27]`. Python no interpreta `\x27` fuera de una
  cadena: es `SyntaxError: unexpected character after line continuation
  character`. La comprobación fallaba **siempre**, así que el .bat habría dicho
  «la terminal no responde» con la terminal levantada.

Ambos fijados por regresión: una prueba prohíbe `/dev/null` y exige `>nul`, y
otra **compila** cada trozo de Python incrustado en el .bat. La guardia anterior
—«el .bat no usa comandos de Linux»— miraba `export`, `&&` y `source` y se le
pasó lo más obvio.

## REVISIÓN SOBRE `2515b0e` · Las 34 herramientas «sin intentos» eran el PLAN AGOTADO

La captura del Auditor lo decía entero, en dos sitios y en letra pequeña:
`cuota 7/240` y `páginas pausadas`. Con `ENGINE_RESERVE = 12`,
`budget_for_pages` devolvía **cero** en cada ciclo: las herramientas estaban
exigibles, con prioridad efectiva 0 y veintitrés minutos de espera, y aun así sin
un solo intento. No era el proveedor, ni la autorización, ni el programador.

Tres defectos reales detrás:

1. **El ritmo se infería de una cabecera que no dice lo que parece.** `Reset: 60`
   se tomaba como «la ventana del plan dura 60 s», y a menudo describe un **cubo
   de ritmo**, no el tope contratado. Con `limit = 240` salían 4 req/s, el
   intervalo caía al suelo de 15 s y el carril del motor se comía las 240
   peticiones en un cuarto de hora. El comentario del propio código ya decía la
   regla —«equivocarse por rápido agota el plan en minutos»— y el código la
   contradecía tres líneas más abajo. Ahora una ventana por debajo de cinco
   minutos no se acepta como prueba del plan y se acota con la diaria; una
   ventana larga se sigue respetando tal cual.
2. **La ráfaga se saltaba el guardián de cuota entero.** `allowed = max(allowed,
   len(priority_due))` ignoraba `budget_for_pages`: con el plan en las últimas
   seguía pidiendo y podía comerse la reserva del motor —la que sostiene la
   estructura— para dibujar tablas de presentación. Ahora la ráfaga adelanta el
   **ritmo** pero no cruza el **límite** (`burst_ceiling`).
3. **El Auditor no lo decía.** Lo insinuaba en una pastilla y lo dejaba deducir de
   un número. Ahora hay una línea que lo nombra —cuánto queda, cuál es la reserva,
   que el carril está parado hasta el reinicio de la ventana— y que dice qué
   hacer: declarar `QUANTDATA_PLAN_REQUESTS` y `QUANTDATA_PLAN_WINDOW_SECONDS`.
   El diagnóstico por herramienta nombra la cuota exacta en vez de un genérico.

Y una prueba propia que daba verde o rojo según el orden: otra prueba de la suite
hace `importlib.reload(shared)` y deja **dos guardianes vivos** —el nuevo en
`shared` y el viejo, que es el que `intelligence` sigue usando—. La prueba
escribía en uno y leía del otro. Ahora toma el guardián del módulo que prueba.

25 regresiones nuevas. Inventario: **2977 casos / 188 ficheros**.

## REVISIÓN SOBRE `98210b1` · El contrato publicado de Quant Data, al pie de la letra

La documentación oficial publica:

```
240 peticiones / 60 s   en VENTANA DESLIZANTE
 20 peticiones /  1 s   de ráfaga
X-RateLimit-Reset       segundos hasta que el cubo se rellena
```

Con eso escrito, la revisión anterior estaba equivocada en sus dos conclusiones:

- **`Reset: 60` no era una señal dudosa.** Se trataba como «una ventana tan corta
  no puede ser el plan» y se acotaba el ritmo con una ventana DIARIA inventada.
  Con el contrato real, 240/60 s son 4 peticiones por segundo sostenidas y el
  carril del motor —4 cada 15 s— consume **el 6,7 %**. Nunca estuvo quemando el
  plan, y la «corrección» bajaba su ciclo a 1.920 s (32 minutos), que habría
  dejado la terminal inservible. Revertido: la base del ritmo es el contrato y
  las cabeceras, no una conjetura.
- **`remaining = 7` no era un plan agotado.** En una ventana deslizante son las
  233 anteriores todavía dentro de los últimos 60 s; se reponen solas conforme
  salen por el otro extremo. Es una espera de **segundos**. El veredicto
  `PLAN_AGOTADO` desaparece, y con él el consejo de declarar 86.400 s.

Lo que sí faltaba de verdad, y es lo que entra ahora:

1. **ITM QUANT no llevaba ningún contador propio.** Se enteraba de haberse pasado
   cuando llegaba el 429. Ahora hay dos ventanas deslizantes reales —240/60 s y
   20/1 s— que frenan **antes** de pedir. Una ventana deslizante de verdad, no un
   cubo fijo: una petición hecha hace 59,5 s todavía cuenta.
2. **Las cuatro cabeceras se leen dinámicamente** —`X-RateLimit-Limit`,
   `-Remaining`, `-Reset` y `Retry-After`— y también **en las respuestas de
   error**, que es justo cuando el presupuesto se quedaba ciego. Si el proveedor
   cambia el tope, se adopta solo.
3. **Una sola contabilidad.** Antes el carril del motor hacía `note()` +
   `spend()` y el de páginas sólo `spend()`: la mitad de las respuestas no
   actualizaba la telemetría y cada petición del motor se contaba dos veces.
   Ahora cuenta `QuantDataClient.post`, el único sitio por donde pasan los dos.
4. **La ráfaga de arranque respeta las 20/s.** Era 5 en paralelo cada 1,2 s sin
   ningún freno propio: el origen más probable de los 429.
5. **El Auditor nombra el freno exacto** —ráfaga, ventana deslizante, reserva del
   motor o 429— con los segundos que dura, y dice que es transitorio.
6. `Retry-After` llega al guardián **sin mezclarse** con `Reset`, y el suelo de
   espera del 429 baja de 30 s a 1 s: con una ventana de 60 s, esperar 30
   tiraba media ventana a la basura.

Dos pruebas anteriores exigían asumir la ventana diaria a falta de cabeceras.
Eran precauciones de cuando no conocíamos el plan; se reescriben contra el
contrato publicado, manteniendo el criterio de medida (que el motor siga siendo
una fracción pequeña del tope). 32 regresiones nuevas de ventana deslizante y
ráfaga. Inventario: **2977 casos / 188 ficheros**.

## REVISIÓN SOBRE `d6b8611` · Al abrir el programa no había ninguna ráfaga

La ráfaga de arranque estaba armada **sólo** en `select_asset`. Al abrir la
terminal —el único momento en el que el operador mira una pantalla vacía— el
carril de páginas salía con el presupuesto de régimen: cuatro herramientas por
ciclo, ciclos de quince segundos, concurrencia dos. Treinta y seis herramientas
a ese ritmo son **nueve ciclos**.

Y el contrato lo pagaba de sobra: 240 peticiones / 60 s dan para hidratar el
catálogo entero en segundos. No era una limitación del proveedor.

Tres frenos, los tres nuestros:

1. **Sin ráfaga al arrancar.** Ahora se arma en `start()`, y en frío cubre el
   catálogo **entero** —no sólo lo que dibuja la pantalla—, porque en frío no hay
   a quién ceder el turno. En un cambio de activo sigue cubriendo sólo lo que
   dibuja, que es donde esa distinción tiene sentido.
2. **«Nunca vista» se trataba igual que «rancia».** El presupuesto de cuatro por
   ciclo era para telemetría envejecida —otra instancia pudo gastar sin que nos
   enteráramos—. En el primer ciclo del proceso no ha habido ningún «mientras»:
   el freno es el contrato. La telemetría rancia sigue avanzando despacio.
3. **La pantalla preguntaba cada 6 s y el diagnóstico cada 15 s** desde el
   segundo cero. El primer minuto va a 1,5 s y 4 s, y vuelve al régimen solo.
   Son llamadas al propio servidor: no tocan la cuota del proveedor.

La concurrencia de ráfaga y su ciclo dejan de ser números a ojo y salen del
contrato: `BURST_CONCURRENCY = BURST_LIMIT − ENGINE_RESERVE` = 8 por segundo, con
ciclos de 1 s —la ventana de ráfaga—. Medido con el selector real:

```
ANTES (sin ráfaga, 4 por ciclo de 15 s) ····· 135 s
AHORA (ráfaga en frío, 8 por segundo) ·······   5 s
```

### Residuos

Barrido completo del repositorio: módulos de `app/` sin referencias (0), ficheros
vacíos, sufijos de copia (`.bak`, `.orig`, `~`), `.bat` apuntando a rutas
inexistentes, estáticos no cargados por ninguna plantilla y scripts sin usar.
**El único huérfano real era `AUDIT_FIX.md`**, una nota de una pasada concreta
cuyo contenido ya está en este CHANGELOG. Eliminado.

Se añade `LIMPIAR.bat`, que borra **sólo** lo que se regenera solo —`__pycache__`,
`*.pyc`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache`— y tiene regresiones que
le prohíben tocar `.venv`, `app\storage`, `.env`, logs o datos de mercado.

21 regresiones nuevas. Inventario: **2977 casos / 188 ficheros**.

## REVISIÓN SOBRE `45fc6ad` · La regresión que fija el arranque en frío

El arranque quedó como estaba —ráfaga desde `start()`, presupuesto derivado del
contrato, «nunca vista» separada de «rancia» y refresco rápido del frontend sin
tocar la cuota del proveedor— y ahora hay una regresión que lo **sostiene**.

`_arranque_completo()` simula **los dos carriles a la vez** desde el segundo cero
con el guardián real decidiendo cada presupuesto, y registra el sello de tiempo
de cada petición. Sobre ese registro se comprueban las tres cosas que importan:

```
peticiones totales en el arranque : 77
catálogo completo hidratado a los : 5,0 s   (umbral de la prueba: 15 s)
pico en 60 s                      : 57 / 240
pico en  1 s                      :  9 / 20
```

Impide las **dos** recaídas posibles, no sólo una: volver a los ~135 s porque
alguien desarme la ráfaga o baje el presupuesto del primer ciclo, y ganar
velocidad rompiendo el contrato —que es peor que ir lento, porque se paga con
429 y con los dos carriles parados—. Una prueba extra verifica que el
comportamiento viejo **no** cumpliría el umbral, para que el umbral no pueda
pasarse por accidente.

### `LIMPIAR.bat`, por lista blanca

La guardia pasa de prohibir a **permitir**: cada orden de borrado tiene que
nombrar uno de los seis patrones regenerables, y falla aunque la orden sea
inofensiva. Eso destapó una debilidad real —`rd /s /q "%%d"` borraba «lo que
hubiera en la variable»—, así que ahora el nombre se vuelve a comprobar justo
antes de borrar y las tres cachés sueltas van en líneas literales.
`.env`, `.venv`, `app\storage`, logs y datos de mercado están en la lista de
intocables, comprobada orden por orden.

### `/legacy` congelado, no eliminado

Se conserva como referencia de comparación y diagnóstico **hasta que el frontend
actual pase la certificación LIVE completa en DIA, SPY y QQQ**. Seis pruebas lo
fijan: la ruta existe, su plantilla existe, **todos** sus estáticos existen, la
razón está escrita en el propio código, la terminal actual **no depende** de él
—para que el día que se quite salga de una pieza— y la limpieza no puede
llevárselo por delante.

### La certificación LIVE mide ahora el arranque y la ventana

Dos columnas nuevas en el informe, medidas contra la terminal viva:

- **HIDRATACIÓN MEDIDA** · segundos hasta la primera herramienta LIVE y hasta la
  meseta, con el recuento final. La meseta se detecta contando **lecturas** sin
  crecimiento, no segundos: si la terminal tarda en responder, eso es una espera,
  no una meseta. Una meseta en cero no cierra la medición.
- **CONTRATO DE CUOTA APLICADO** · qué ventana está usando la terminal. Si vuelve
  a aparecer `ASUMIDA_DIARIA`, o el ciclo del motor pasa de 60 s con un contrato
  de 240/60 s, el informe lo marca **FAIL** con el motivo. El episodio de los
  1.920 s no puede repetirse en silencio.

Un activo que falle sigue sin detener a los demás. 24 regresiones nuevas.
Inventario: **2977 casos / 188 ficheros**.

## REVISIÓN SOBRE `76521d9` · La colisión de carriles en la ranura del hub

La consola del arranque real:

```
WARNING itm.quantdata_intelligence  degradado en
    quantdata_intelligence:normalize:net_flow [DEGRADED]:
    AttributeError: 'dict' object has no attribute 'payload'
WARNING itm.quantdata_intelligence  degradado en
    quantdata_intelligence:normalize:net_drift [DEGRADED]: (idéntico)
```

El hub funde las peticiones duplicadas de los dos carriles por
`(dataset, símbolo)`, que es lo correcto y ahorra cuota de verdad. El problema
era **qué** metía cada uno en esa ranura compartida:

```
carril del motor   →  self._post(...)     devuelve el DICT del payload
carril de páginas  →  _client.post(...)   devolvía el OBJETO QuantDataResponse
```

Quien llegaba segundo recibía el objeto del otro. Sólo puede ocurrir donde el
nombre coincide palabra por palabra, y coincide en exactamente tres:
`net_flow`, `net_drift` e `iv_rank`. Los demás van renombrados
(`gex_by_strike` → `gamma`) y por eso nunca fallaron. **Se volvió sistemático con
la ráfaga de arranque**: antes los dos carriles rara vez pedían lo mismo a la
vez; ahora arrancan juntos.

Arreglado en el origen, no envolviendo la lectura: los dos carriles meten el
**mismo tipo**, así que la fusión sigue funcionando y ya no puede mentir sobre
lo que devuelve. Más un cinturón que, si algo vuelve a meter otro tipo, lo dice
**con el nombre del tipo** en vez de reventar cincuenta líneas más allá.

Y un defecto latente que destapó: `"ready": bool(p)` daba `True` para
**cualquier** objeto no vacío, así que una respuesta colada por la ranura se
publicaba como `ready: True` con el objeto entero en `raw` —la herramienta
parecía viva y lo que servía no era del proveedor—. Ahora `es_payload()` exige
un diccionario con algo dentro. Ninguna herramienta puede declararse viva porque
«venía llena».

### El 400 enseña el campo, no sólo que hubo un 400

```
WARNING itm.data_hub   degradado en data_hub:max_pain [DEGRADED]:
    QuantDataError: Quant Data HTTP 400: Request validation failed
WARNING itm.quantdata  degradado en quantdata:max_pain:unrepairable
```

El backend guarda **qué campo** señaló el proveedor desde v1.45.0 y el veredicto
clasificado desde v1.57.0. Nada de eso llegaba a la pantalla: la fila enseñaba el
error crudo cortado a setenta caracteres, donde un 400 se lee igual que un 404 y
el único dato accionable se quedaba en el JSON. Ahora la fila lleva el veredicto,
su remedio, el **campo rechazado** y la evidencia completa en el título.

Esto no adivina el cuerpo correcto de `max_pain` ni inventa una ruta para
`trade_side_statistics` (404 en `/v1/options/tool/trade-side-statistics`): las
dos necesitan lo que conteste el proveedor, y ahora se ve.

23 regresiones nuevas. Inventario: **2977 casos / 188 ficheros**.

## REVISIÓN SOBRE `bc5a228` · Max Pain y Trade Side, contra el contrato oficial

Los dos fallos del arranque no eran del proveedor: eran de nuestras dos
integraciones, y la documentación oficial de Quant Data los explica los dos.

### MAX PAIN · faltaba el vencimiento

`/v1/options/tool/max-pain` exige `filter.ticker` **y** `filter.expirationDate`.
Enviábamos sólo el ticker: de ahí el `400 Request validation failed`.

Lo importante del arreglo es de dónde sale el vencimiento: **no se inventa**.
Sale de la ventana de vencimientos que la terminal ya aplicó a la cadena, que es
la única fuente legítima. `service` la publica en `EXPIRY_SELECTION` cuando la
resuelve y la retira en los dos reseteos de símbolo, para que los vencimientos
de DIA no construyan nunca un cuerpo de SPY.

Y cuando todavía no hay ventana resuelta, la herramienta **no llama**: levanta
`FaltaRequisito` y publica `REQUISITO_AUSENTE` diciendo qué campo falta y cuál es
la alternativa legítima. No es `SIN_DATOS` —el operador lo pidió explícitamente—
ni un fallo del proveedor: es un requisito que aún no tenemos. Un max pain del
vencimiento equivocado es un número creíble y falso, que es peor que no tenerlo.

Para el max pain de **todos** los vencimientos ya existe
`/v1/options/tool/max-pain-over-time`, que sólo pide el ticker; es el que usa el
carril del motor y el que sirve `max_pain_over_time`.

El cuerpo va con lo obligatorio y nada más. `sessionDate` es opcional y además
está en la lista de campos heredados que se quitan del catálogo entero —se metía
de una herramienta en otra y provocaba 400 donde no tocaba—, así que no se manda.

### TRADE SIDE · la ruta estaba mal

No es `/v1/options/tool/trade-side-statistics` —no existe, de ahí el 404— sino
**`/v1/options/tool/contract-trade-side-statistics`**, y exige `dataMode`, que
sólo admite `PREMIUM`, `TRADE_COUNT` o `VOLUME`.

Se pide **PREMIUM** porque es lo que el panel dibuja: reparto de prima entre lado
comprador y vendedor. Pedir `TRADE_COUNT` y pintarlo como prima sería mezclar dos
magnitudes bajo la misma barra, y hay una prueba que ata las dos cosas: si el
panel deja de consumir `premium`, salta.

La herramienta se renombra a `contract_trade_side_statistics` en sus cinco
consumidores, y una regresión recorre los cinco ficheros línea a línea para que
la ruta vieja no pueda volver.

28 regresiones nuevas. Inventario: **2977 casos / 188 ficheros**.

## REVISIÓN SOBRE `c8a8247` · Universo cerrado a 36 símbolos y limpieza con evidencia

### El universo

`app/core/universe.py` es ahora la **única** autoridad sobre qué símbolos existen
para el programa: 15 acciones y 21 ETFs. El cierre se aplica en los dos puntos de
entrada del catálogo —el registro dinámico y la caché en disco, al escribirla y
al leerla— así que lo que no está en la lista no se registra, y lo que no se
registra no se ofrece, no entra en el scanner, no se precarga y **no gasta una
sola petición** del contrato del proveedor.

**El encargo decía «Total: 37 activos» y la lista enumerada suma 36** (15 + 21).
Se respeta la enumeración, que es el dato concreto, y no el total. Añadir un
símbolo a ojo para cuadrar la cuenta habría metido en el universo un activo que
nadie pidió.

Lo que **no** cambia, y hay pruebas que lo atan:

- Ni una rama de cálculo por activo. Una prueba prohíbe que el módulo del
  universo contenga ventanas, griegas, multiplicadores o proveedores; otra
  verifica que ningún módulo matemático lo consulte; otra demuestra que **añadir
  un símbolo es añadirlo a la lista y nada más**.
- Los 36 comparten la misma física —mismo multiplicador, misma sesión, mismo
  modelo, mismo ejercicio— y un símbolo desconocido sigue resolviendo por la
  plantilla genérica. El motor sigue siendo universal.

El catálogo se **siembra** con el universo completo en vez de esperar al
descubrimiento del proveedor: sin red, sin clave o con la caché fría, el operador
veía tres activos en lugar de los suyos. La lista decide qué existe; el proveedor
decide qué puede hacerse con cada uno.

Salen del universo `YM`, `MYM`, `DJX`, `VIX` y `VXD`. `XLI` y `XLF` dejan de ser
«contexto interno del Dow» y pasan a ser ETFs operables, con su propia cadena:
arrastrar `DIA` en sus derivados era correcto cuando sólo servían de confirmación
sectorial y es un proxy cruzado ahora que el analista puede operarlos.

**El contrato del universo se reescribió, no se borró.** El fichero anterior
decía en su última línea: «si decides re-expandir el universo, este archivo es el
contrato que hay que cambiar». Eso se ha hecho: `tests/test_v1271_dow_scope_contract.py`
es ahora `tests/test_v1580_contrato_del_universo.py`, y conserva cada invariante
que seguía vigente —ningún activo hereda la matemática de otro, un símbolo ajeno
falla con error tipado, consultar el ecosistema es informativo y entrar al
pipeline es un error—.

Ocho ficheros de pruebas más se re-anclaron al universo nuevo en lugar de
silenciarlos. Donde la regla protegía algo que ya no existe —los futuros del
Dow—, se omite **nombrando** qué prueba la cubre ahora.

Y dos defectos de aislamiento que esto destapó: `ASSETS` es un diccionario vivo
que varias pruebas mutaban, y varias hacen `importlib.reload`, que deja dos
catálogos y **dos clases de excepción** vivos a la vez. El contrato pasaba o
fallaba según el orden de ejecución. Un contrato que depende del orden no es un
contrato: ahora se restaura el catálogo entre pruebas y se accede por el módulo.

### La limpieza

Auditoría de los 57 ficheros de la raíz y los 375 módulos Python contra seis
controles. El resultado honesto: **el repositorio ya estaba limpio** —cero
módulos huérfanos, cero sufijos de copia, cero `.bat` rotos, cero estáticos sin
cargar—. Se eliminó `WINDOWS_INSTALL_HOTFIX_31210.txt` (nota de v1.40.0, sin una
sola referencia) y se movieron dos documentos a `docs/`.

El inventario completo `archivo → función → decisión → evidencia`, incluido **lo
que NO se borró y por qué**, está en `docs/INVENTARIO_LIMPIEZA.md`. Tres ficheros
parecían huérfanos y los exige el gate de release construyendo su nombre con la
versión; otros son puntos de entrada que el operador abre con doble clic.

Sí se limpió residuo **dentro** del código: las fichas de ecosistema de los cinco
símbolos retirados y las referencias cruzadas que apuntaban a cadenas que el
programa ya no puede pedir.

### Verificación

```
suite            2926 passed, 51 skipped
arranque en frío 0 errores en el log
smoke test       / · /legacy · /api/assets · /api/terminal/bundle · /api/state → HTTP 200
/api/assets      36 activos · fuera del universo: ninguno
```

61 regresiones nuevas. Inventario: **2977 casos / 188 ficheros**.

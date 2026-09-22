# ITM QUANT · MULTI ASSET

## v1.61.0 · Los carriles, con sitio para leerse

Medido con el navegador sobre la terminal en marcha, no supuesto: en el panel de
**Net Drift** tres carriles medían **un píxel de alto** —`TOTAL`, `DELTA / MIN` y
`VOLUMEN NETO`— y la curva desbordaba su contenedor. El dato estaba; la pantalla
no lo tenía.

- **La rejilla declaraba cuatro filas para seis carriles.** Los dos últimos
  caían en filas implícitas y, al ser lienzos sin alto propio, se aplastaban a
  nada. El CSS no falla: reparte lo que hay y sigue, sin un aviso.
- **`[hidden]` había dejado de ocultar.** Es la regla del navegador con menos
  especificidad, y `.grid { display: grid }` le gana: los dos bloques de KPI de
  FLUJO salían a la vez y se comían 200 px que le faltaban al gráfico.
- **Ahora cada carril tiene su fila y su suelo en píxeles** (132 px), el gráfico
  principal el suyo (420 px en la cinta, 660 px en Net Drift), y cuando la
  ventana no da, la pila **se desplaza en vez de aplastar**.
- **DARK POOL** deja los 420 px fijos por `clamp(420px, 56vh, 760px)`, y sus
  nueve tarjetas pasan de tres filas a dos.

El suelo del gráfico principal se había subido tres veces —360 → 520 → 660—
persiguiendo el síntoma. Cuando el mismo número hay que subirlo tres veces, el
número no es el problema.

## v1.60.0 · El transporte, de raíz

`Quant Data connect timed out after 4.0s`, repetido en `net_drift`, `net_flow`,
`dark_flow` y `gamma`. Cuatro endpoints que no comparten nada salvo el **host**,
muriendo al mismo plazo exacto, ciclo tras ciclo. Cuando el fallo es idéntico en
cosas que sólo comparten el destino, el defecto es del transporte.

- **Dos pools eran el doble de handshakes.** Cada carril construía su propio
  `httpx.AsyncClient`. Ahora hay **uno por host**, compartido, con refcuenta.
- **El keep-alive caducaba antes que el ciclo** —5 s por defecto contra ciclos de
  15 s—, así que cada ciclo volvía a abrir una conexión por herramienta, y todas
  a la vez: una **estampida de conexión** contra el mismo host. Ahora 90 s: el
  handshake se paga una vez y se reutiliza.
- **El plazo de conexión lo gobierna el p95 del handshake medido, por HOST**, no
  una constante por petición: un handshake no pertenece a ninguna herramienta.
  La **lectura** sigue gobernada por el p95 **por endpoint**.
- **Y un plazo agotado ahora SUBE el siguiente.** Sin eso el bucle no tenía
  salida: plazo corto → el handshake no completa → no hay muestra → no hay p95 →
  el plazo sigue corto.
- **Cuatro fases con nombre propio** —`CONNECT`, `POOL`, `READ`, `WRITE`—, un
  cortocircuitos de transporte por host, **un** reintento de conexión con
  presupuesto y jitter que nunca se concede en ráfaga, y telemetría de
  `pool_wait_ms`, `connect_ms`, `tls_ms`, `read_ms` y `reuse_pct`.

El warm start pasa de 4 s a 8 s porque el razonamiento que sostenía el cuatro es
falso con la evidencia delante — pero **subir el número no es el arreglo**: lo
que quita los timeouts es que el handshake deje de ocurrir en cada ciclo.

> Este bloque queda **abierto** hasta que el certificador LIVE, en el Windows del
> operador, demuestre que la repetición desaparece.

## v1.59.0 · Cuatro cajones genéricos, abiertos uno a uno

Ninguna fórmula cambia. Lo que cambia son los cuatro sitios donde una respuesta
juntaba causas que se arreglan de maneras opuestas.

- **La criticidad es del PAR (dato, consumidor).** `gamma` falla: para una
  tarjeta con respaldo es opcional; para Call Wall / Put Wall es un **factor de
  la fórmula**. Siete consumidores declaran de qué dependen y con qué nivel, y la
  severidad se **deriva**. Con un cuarto estado, `UNKNOWN`: lo que no se ha
  medido no se declara bloqueado, y una dependencia que sigue en la cola tampoco.
- **Las Walls dejan de depender del ciclo de sondeo.** Los seis ingredientes se
  arman en un snapshot coherente con tres salidas y ninguna vacía: `COMPLETO`,
  `LKG` **con su edad**, o `NO_CALCULABLE` **nombrando el ingrediente que
  falta**. Al servir el LKG se sirve también **su precio**: el precio entra al
  cuadrado, y mezclarlo con la cadena de hace cuarenta segundos mide dos
  mercados.
- **Dark Pool separa lo de ahora de lo guardado.** Un timeout con 216 filas del
  ciclo anterior ya no obliga a elegir entre tirar el dato o esconder el fallo:
  `current_status`, `current_rows`, `lkg_rows`, `lkg_age`, `last_success_at` y
  `serving`.
- **Nueve estados del programador, y la anomalía fuera de ellos.** «Sin
  intentos» juntaba seis situaciones con cinco remedios distintos y un defecto
  real escondido entre ellas: `NUNCA_LLAMADA` sale ahora **como anomalía**,
  porque un estado describe a la herramienta y una anomalía acusa al programador.

## v1.58.0 · Dónde se rompe, y qué no se borra

- **El agresor: «no lo sé» no era suficiente.** v1.53.0 dejó de inventar el lado
  y las marcas salieron neutras — honesto y **no accionable**. Ahora se
  diagnostica la cadena en cuatro eslabones y se señala **el primero** que falla:
  la cinta no llega, llega sin lado, no cruza con la concentración, o el flujo
  estuvo genuinamente repartido. Cada uno con su remedio.
- **FLUJO: un ciclo vacío ya no borra lo que sí había.** La pantalla decía
  «405 buckets · último hace 3610 min» y «PRIMA TOTAL: SIN DATOS» a la vez,
  porque el módulo tenía **un estado global**. Ahora hay cinco estados y **LKG
  por carril**, con clave `(symbol, session_date, dataset)` — las tres, porque un
  LKG mal indexado enseña un número correcto en el sitio equivocado. Los siete
  escenarios A–G están probados.
- **El Scanner dentro de TRACE**: barra en el encabezado y sus cuatro líneas en
  el gráfico. **El Scanner sigue siendo la única autoridad direccional**; TRACE
  representa y no recalcula. Sin tesis lista sale `ESPERANDO` y no se dibuja
  nada. El plan es transaccional (`thesis_id`) y **sustituye** a `target`/`risk`
  en vez de duplicarlos.
- **Hover sobre una línea**: nombre, precio, dirección, fuerza, fuente y
  timestamp — dirección y fuerza sólo si el nivel las trae.
- **TRACE más amplio**: 640 → 780 px.
- **Sesión de Londres desde las 04:00 de `America/Guayaquil`**, con acumulado
  propio; al empezar Nueva York, Londres **se sella, no se borra**. El corte se
  construye en hora de Nueva York: una hora UTC fija fallaría medio año por el
  horario de verano.

## v1.53.1 · La identidad de cada línea

Había líneas dibujadas en TRACE **sin etiqueta**, y no se pueden identificar por
el color: `--neg` agrupa `put_wall` **Y** `risk`; `--pos` agrupa `call_wall` **Y**
`target`. Deducir del color acierta la mitad de las veces y no avisa cuando falla.

Volcadas desde el motor en ejecución, las dos anónimas son:

- **La roja** → `type=risk`, nombre del motor **`Invalidación`**, de
  `nextgen_terminal.structure_levels()` ← `scanner['invalidation']`.
- **La verde** → `type=target`, nombre del motor **`T1`**/**`T2`**, de la misma
  función ← `scanner['target1'|'target2']`.

Ninguna es un nivel de exposición: las dos salen del **Scanner**, no de la cadena
de griegas. Quedaban sin rotular por ser las últimas en prioridad.

El Auditor expone ahora `precio · type · source · campo · method · magnitud ·
persistencia · timestamp` de cada línea visible, y el gráfico las etiqueta todas
—con nombre corto antes que quedarse mudas—. Nada se renombra ni se recalcula.

## v1.53.0 · El agresor en producción y el campo visible

- **Todas las marcas salían en rombo neutro.** v1.52.0 hizo bien en no inventar
  un lado, pero el resultado en producción fue que **ninguna** lo tenía. El
  proveedor no siempre publica campo de lado; sí publica precio y NBBO del
  instante, y compararlos **no es adivinar: es la definición operativa del
  agresor**. El campo declarado siempre gana; el NBBO sólo actúa cuando no hay
  ninguno, y va etiquetado.
- **El umbral bajó de 2:1 a 60/40.** Una concentración con el 65 % de la prima
  agredida del lado comprador **es** compradora. La confianza viaja en el evento.
- **La cinta publica su cobertura**: cuántos prints con lado y de qué campo. Si
  vuelve a salir neutro, ahí está la causa.
- **El mapa de calor salía macizo en QQQ, SPY y todos los ETF**, y la causa era
  contra qué se normalizaba: el Interval Map cubre todo el libro y la ventana son
  unos dólares, así que el percentil se calculaba contra strikes que ni se ven.
  Cuantos más datos, menos contraste. Ahora se recorta al rango visible antes de
  normalizar: 90 strikes → 13 filas, 94 → 210 segmentos de isolínea.
- **Un muro fuera de ventana ya no desaparece**: se ancla al borde con su
  distancia, en vez de estirar la ventana y aplastar las velas.
- **El diagnóstico de Dark Pool mira los carriles del proveedor**, no las capas
  derivadas, y el modelo publica el conteo por etapa.

## v1.52.0 · El lado agresor, y el dato que ya estaba

Ninguna fórmula cambia. **Sí cambia la clasificación del lado agresor**, que
estaba produciendo lecturas invertidas.

- **`AT_BID` se clasificaba como COMPRA.** La regla comparaba el prefijo del
  valor contra una letra suelta —`"A"`— y `AT_BID` empieza por A, pero es el
  **vendedor** cruzando el spread. Ahora la clasificación vive en
  `app/core/aggressor.py`, busca por subcadena y tiene una prueba por valor.
- **Una PUT comprada se marcaba como VENTA.** La marca leía `side`, que el motor
  calcula como dominancia de prima por tipo de contrato: mide **qué contrato
  pesó más**, no **quién agredió**. Un intervalo dominado por calls puede ser
  calls *vendidas*. Ahora son dos campos, `premium_side` y `aggressor`, y la
  interfaz sólo mira el segundo, que sale de la cinta de order-flow.
- **Sin agresor conocido no se elige lado**: rombo neutro. `flowSide` devuelve
  tres estados, no un booleano — es el cambio de tipo lo que hace imposible el
  defecto.
- **Dark Pool: el dato llegaba y la pantalla decía SIN DATOS.** Dos causas.
  `dark-flow` publica las acciones en **`size`** y el normalizador las buscaba
  por heurística («dark», «offExchange»), así que 608 filas salían con volumen
  `None`. Y los paneles colgaban de la clasificación por venue, una capa
  **derivada** bloqueando a la fuente **directa**. Ahora hay un
  `DarkPoolViewModel` único desde los tres carriles del proveedor.
- **`equity-prints` pide `sessionDate`** resuelto a la última sesión válida, y
  el % fuera de bolsa **no se estima**: sin universo dark + lit completo va en
  SIN DATOS.
- **Relieve 3D**: una fila por strike, secuencia completa, con el panel creciendo.
- **Mapa dinámico**: las ocho opciones verificadas en la terminal real. Un mapa
  sin dato declara su causa en vez de quedarse en blanco.
- **Cambio de activo**: ráfaga de arranque acotada por plazo, alcance y salida
  anticipada. Antes la pantalla tardaba minutos en llenarse.

## v1.51.0 · Un hueco deja de valer cero

Ninguna fórmula cambia, y **ningún cambio es por activo**: todo vive en el
normalizador compartido, en el componente de render o en el núcleo.

- **La sierra de la DERIVA DE VOLATILIDAD era un cero inventado.** Una IV de cero
  es imposible: eso era un hueco pintado en el suelo del eje. El fallo estaba en
  el **normalizador** (`_f(value, 0.0) or 0.0`), no en la presentación, que es
  donde se habían buscado los ceros hasta ahora. Afecta a las **cinco**
  herramientas que comparten ese normalizador: en cuatro de ellas —IV, max pain,
  interés abierto, precio— el cero es imposible; en la quinta, prima neta, puede
  ser legítimo y por eso se distingue con `value_measured`.
- **Y había un segundo cero en el renderizador**: un punto sin valor se dibujaba
  en `sy(0)`. Ahora el trazo **se parte** en el hueco: se ve que falta un tramo,
  no un desplome.
- **`IV PERCENTIL 100 %` sobre `amplitud 0.00 pp`.** Está bien calculado y no
  informa de nada: con todas las lecturas iguales vale 100 % siempre. Se retiene
  junto al rank, con la causa escrita. Invierte una decisión de v1.41.5.
- **El relieve 3D no se entendía.** Caras de dos píxeles y dos polilíneas
  abiertas que parecían rayas sueltas. La profundidad se calculaba como fracción
  del alto **total** del lienzo, y el perfil por strike lo hace crecer a miles de
  píxeles. Ahora: prismas con tres caras iluminadas, suelo cerrado en fuga,
  profundidad acotada en **píxeles absolutos** y contenedor con alto propio.
- **Una etiqueta por strike.** El salto se medía contra un paso fijo de 15 px
  cuando el panel crece con las filas; ahora sale del paso real.
- **Los contornos del mapa no eran contornos**: un palito suelto por cruce, sin
  unir con el de la celda vecina. Ahora **marching squares** en el componente
  común, y un campo pastel con banda neutra y techo de opacidad en lugar de un
  bloque saturado.
- **El Interval Map de la sección vuelve a ser una rejilla de puntos.** En el
  TRACE el mapa es **fondo** y hace falta la forma de la zona; en la sección es el
  **sujeto** y se lee celda a celda, y ahí el diámetro del punto es la magnitud.
- **Net Drift: muros y marcas doradas sobre el eje del PRECIO**, no sobre el de la
  prima, y con **una sola escala** — el primer intento creó un eje nuevo y salían
  dos escalas de precio con dominios distintos, que es peor que no dibujarlos.
- **Rotulación de niveles definida una vez** en el núcleo y subida a 12 px; TRACE
  y Net Drift con suelo de alto **en píxeles**.

## v1.50.0 · Lo que se mira, y dónde ocurrió

Ninguna fórmula cambia. Cambia **qué se muestra y con cuánto sitio**.

- **El perfil de strikes deja de arrastrar lo que no puede importar.** Con el
  subyacente en 534, los strikes 433 y 445 ocupaban medio eje. El corte no es un
  número fijo —±5 dólares es todo el libro en un ETF de 40 y ruido en un índice
  de 5.800—: es una **banda proporcional** (6 % del subyacente) más
  **materialidad relativa**. Una fila lejana se conserva si llega al 18 % del
  máximo **y** destaca 3× sobre la mediana de las lejanas: en un perfil plano la
  primera condición sola no filtra nada. **Un muro real fuera de la banda nunca
  se oculta.**
- **El recorte es presentación, no dato.** Las filas siguen en el bundle, los
  agregados siguen saliendo del libro entero, y el panel publica `strike_window`
  con banda, total, conservadas, descartadas y motivo.
- **La marca de flujo separa el hecho de la dirección.** Círculo **dorado** donde
  ocurrió (el hecho), radio contra el pico del ciclo (cuánto), flecha
  **verde/roja** (compra o venta) e importe. Idéntica en TRACE y en FLUJO DE
  ÓRDENES, desde las mismas funciones.
- **TRACE, disposición definitiva y global**: DEX a la izquierda, campo continuo
  con contornos y velas al centro, GEX a la derecha, los tres sobre el mismo eje
  de precio. Sin tratamiento por activo — hay un test que lo comprueba.
- **Net Drift con sitio**: `2.2fr` en vez de `1fr`, el valor de cada curva al
  final del trazo, y un carril **TOTAL** que **suma** la prima del intervalo con
  marcado dorado de criterio elegible (`Top 3` o `≥10×` la media) y la media
  dibujada como referencia.
- **Ningún carril mudo**: con ejes dibujados y sin dato, el carril declara la
  causa en vez de quedarse en blanco.

## v1.49.0 · El contrato de `dark-pool-levels`

El cuerpo mínimo estaba **incompleto**, y por eso salía 400. El contrato exige
`sessionDateRange.startDate` además de `filter.ticker` — un campo obligatorio que
**ninguna otra herramienta del catálogo usa**, así que recortar desde el cuerpo
de la vecina no podía llegar a él.

- **`dark-flow` acepta `sessionDate`/`timeRange`; `dark-pool-levels` usa
  exclusivamente `sessionDateRange`.** Dos nombres parecidos, dos contratos: el
  cuerpo no fallaba por llevar un campo de más, sino por llevar el concepto
  correcto con el nombre equivocado.
- **La fecha es la última sesión válida.** Un sábado no es una sesión, y pedirlo
  da 400 o un 200 vacío — las dos cosas se leen como «no hay dark pool».
- **Los campos prohibidos son de la herramienta**, no del catálogo:
  `aggregationPeriod` es legítimo en `dark-flow` y la reparación guiada por error
  podía añadírselo justo al endpoint que lo rechaza.
- **La respuesta es un mapa por nivel de precio**, no una lista. El parser sólo
  leía listas, así que un 200 válido daba cero niveles y salía «SIN DATOS».
  Ahora se leen `priceLevel`, `notionalValue`, `size`, `tradeCount` y
  `latestStockPrice`.
- **El 400 se conserva entero** —`type`, `detail`, `errors[].field`,
  `errors[].message`— en una tabla propia del Auditor.
- **El verificador LIVE exige un HTTP 200 real** para dar el endpoint por
  cerrado.

## v1.48.0 · Un campo, un strike por barra, una marca que se ve

- **El Interval Map era una tabla pintada.** Celdas duras sobre fondo negro, con
  huecos sin rellenar. La exposición por strike y tiempo es un **campo**: huecos
  interpolados desde las vecinas (un cero medido sigue siendo cero), suavizado en
  celdas, normalización por rango **con suelo de ruido** —sin él media pantalla
  sale a media opacidad y el mapa es un bloque macizo— y contornos cerrados en
  los dos ejes.
- **TRACE trataba el mismo dato de otra forma**, con su propio umbral y su propia
  curva. Los dos pasan ahora por el mismo campo.
- **Una barra por strike, siempre.** El strike es la unidad de lectura: `516…518`
  obliga a abrir el hover para saber cuál de los tres tiene el muro. El panel
  crece —166 strikes → 2.403 px, 11 px por barra— y el contenedor hace scroll.
- **Relieve 3D** del mismo perfil: proyección isométrica con los mismos ejes y la
  misma escala. Las barras comparan magnitudes; el relieve enseña la forma.
- **Marcas de flujo doradas** con flecha y cantidad. El verde y el rojo ya los usa
  el precio, y una marca sobre un tramo de su color desaparecía dentro de él.
- **Dark Flow publicaba ceros que no eran ceros.** Con una lista fija de nombres,
  un campo con otro nombre daba `0.0` y salía «608 intervalos · 0.0 acc». Ahora el
  campo se **deriva de la respuesta**, se declara cuál se usó en el Auditor, y si
  ninguno sirve el valor es `None`.

## v1.47.0 · Densidad no se resuelve adelgazando

Se estaba intentando resolver **densidad de datos con grosor fijo**. No existe un
grosor correcto para «20 barras en 300 px» y «390 barras en 300 px» a la vez, y
cada constante que se probó —2, 3, 5 px— rompía en cuanto cambiaba la cantidad de
strikes, de buckets o el tamaño del activo.

- **El suelo estaba aplicado donde no servía.** `fitBars` agrupaba hasta dejar
  barras de 3 px de **paso** y `barThickness` cogía después el 82 % de ese paso:
  2.46 px de barra, por debajo del mínimo que el código creía garantizar.
- **`AdaptiveBarProfile`**, un solo componente para EXPOSICIÓN, FLUJO, OI,
  ESTADÍSTICAS y el mapa de calor. Grosor, agrupación, hueco, autoescala y hover
  salen del espacio de pantalla y de la densidad. `pocas barras → gruesas` ·
  `muchas barras → AGRUPAR → gruesas`. Nunca `→ cada vez más finas`.
- **La agrupación es visual; el dato sigue entero.** Cada contenedor conserva
  rango, extremo, suma, recuento y miembros, y el `hover` los enseña. Nunca una
  media: cancelaría un +8 con un −8 vecinos y borraría la concentración.
- **Los carriles de FLUJO agrupan el INTERVALO** —2 m, 3 m, 5 m…— y no la
  posición, para seguir alineados con las velas de TRACE.
- **El mapa de calor pinta zonas, no puntos.** Eran círculos de 1.3 px con escala
  lineal por el máximo; ahora son celdas rellenas, agrupadas en las dos
  dimensiones y con intensidad por rango.
- **El primer fotograma ya es correcto.** `Glide` animaba cada clave desde cero,
  así que un panel que entra en pantalla —y dibuja un solo fotograma— se quedaba
  con el eje bien y las barras a cero.
- **`NameError: name '_f' is not defined`** en cada construcción del trace,
  tragado por un `except` que devolvía un trace vacío. Indistinguible de un
  mercado sin datos.
- **Agregado ≠ desglose**, en tres secciones: `OI TOTAL 37.1K` sobre `SIN INTERÉS
  ABIERTO`, `14K contratos` sobre `$0.0 de prima`, `+0.000 pp` sobre `NO
  DISPONIBLE`.
- **Regresión visual automática** (`tools/visual_regression.py`): 46
  combinaciones de activo, distribución, densidad y viewport, midiendo la
  geometría real de los renderizadores. Es la comprobación que las pruebas
  numéricas no podían hacer.

## v1.46.0 · Tres carriles, cinco códigos, ningún cero inventado

Los defectos de esta release son variaciones de una sola idea equivocada:
**tratar cosas distintas como si fueran la misma**.

- **400 y 422 son opuestos.** Un 400 dice «tu petición está mal»; un 422 dice «tu
  petición está bien y no tengo datos». Tratarlos igual hacía dos daños a la vez:
  se «reparaba» un cuerpo correcto y se marcaba como averiada una herramienta
  sana, y un 400 entraba en el ciclo de reintentos, que no lo arregla **nunca** —
  ésa era la razón de que `dark-pool-levels` llevara ciclos en DEGRADADO.
- **Reparar no es adivinar.** La corrección del cuerpo la **dicta el proveedor**:
  se lee `errors[].field` y se corrige ese campo, como máximo tres veces por
  ciclo. Si el campo no está en el catálogo de correcciones, se dice cuál es y se
  para. No se prueban variantes.
- **`dark-pool-levels` envía su propio contrato**, el mínimo, sin un solo campo
  heredado de otra herramienta (`sessionDate`, `timeRange`, `snapshotTime`,
  `filterExpression`, `pagination`, `projection`…), con lista de bloqueo para que
  no vuelvan a colarse.
- **Tres carriles, no una sección.** Dark Flow, Dark Pool Levels y Equity Prints
  son canales separados con su timeout, su Last Known Good y su estado. Dos sanos
  bastan para que la sección tenga dato; el roto se declara igual (`degraded`).
- **Ocho estados con remedio.** `REQUEST_INVALID` (corregir el payload),
  `PROVIDER_ERROR` (esperar), `PARSER_ERROR` (corregir el normalizador),
  `MARKET_CLOSED`, `STALE`, `NO_CLASIFICABLE`, `SIN_DATOS_REALES`,
  `DIRECT_PROVIDER_OK`. La pantalla del analista sigue diciendo SIN DATOS; el
  Auditor conserva cuál fue y qué campo rechazó el proveedor.
- **El normalizador de niveles conserva los campos oficiales**, incluido el precio
  de referencia del subyacente —que viaja a nivel de respuesta— y los que todavía
  no tienen nombre, bajo `extra`.
- **La ventana de cadena depende del instrumento, no del ticker.** Una lista de
  símbolos daba ±2.9 % a DIA, ±2.9 % a DJX, ±16.5 % a XLF y ±9.2 % a XLI. Ahora la
  excepción es de clase —futuros, índices de volatilidad— y todo lo demás deriva
  su banda del propio precio.
- **Un hueco deja de formatearse como cero.** `Q.money(Q.num(x))` escribía `$0.0`
  sin dato, en treinta y ocho sitios. El guardia va en el formateador: uno se
  sostiene, treinta y ocho se desincronizan.
- **La escala de los paneles ya no arranca en un dólar.** El `1` inicial de
  `GlideValue` saturaba el primer fotograma, y el efecto dependía del tamaño del
  activo. Encontrado **mirando**: el carril denso de un valor de 9,52 $ salía con
  las 390 barras al tope mientras los ETF grandes se leían bien.

## v1.45.0 · Un dato ausente deja de ser una afirmación

Cinco defectos, un mismo patrón: **cuando no había dato, se escribía el valor más
cómodo en vez de admitir que no se sabía** — y el valor más cómodo siempre resultaba
ser una afirmación.

- **El 400 ya dice qué falta.** El proveedor nombraba el campo que rechazaba y el
  cliente lo tiraba (leía sólo `detail`, recortado a 180 caracteres). Ahora se
  extrae, y la herramienta **repara el cuerpo** probando variantes hasta que una
  pasa, recordando la aceptada.
- **«CONTRASTE ACTIVO» era una etiqueta falsa.** Había un **segundo** mapa de
  autoridad escrito a mano que no se movió cuando v1.43.0 cambió la política. Ahora
  se **deriva** de `metric_authority`: EXPOSURE, OPEN_INTEREST, IMPLIED_VOLATILITY,
  OPTION_FLOW y DARK_POOL salen como AUTORIDAD PRIMARIA.
- **PROCEDENCIA ≠ CARRIL.** `MOTOR` en una columna llamada ORIGEN se leía como
  autoría cuando era transporte. Dos columnas, dos preguntas.
- **Dark Pool: ausencia ≠ cero.** `off_exchange` devolvía `False` cuando el
  proveedor no publicaba el campo, así que **todas** las impresiones quedaban
  clasificadas como en bolsa. Ahora es tri-estado, con deducción por centro de
  ejecución, y la sección dice **por qué** está vacía.
- **Barras legibles en todos los activos.** Suelos de grosor y extensión (un cero
  sigue midiendo cero), escala robusta que marca lo que recorta, y agrupación
  cuando no caben — forzar 3 px con 390 buckets los solapaba en un bloque sólido.
- **El heatmap tenía un parámetro por ticker sin estar escrito.** `x / p95` con un
  campo de cola pesada deja casi todo bajo el suelo de opacidad, y la cola es más
  pesada cuanto más concentrada la cadena. Normalización **por rango**: llena el
  64 % del rango visual sea cual sea la distribución, frente al 45–97 % de la lineal.
- **Mercado cerrado** cae a la última sesión válida, marcada.
- **El Auditor** queda separado de las pestañas de análisis, sin eliminarse.

## v1.44.0 · Autoridad única de Wall, anclaje real y resiliencia del Data Hub

- **Un solo Call Wall y un solo Put Wall.** Antes `structural_walls()` se llamaba
  desde tres sitios con tres frames distintos: el Call Wall de TRACE podía no ser
  el de RESUMEN y **nada lo detectaba**. Ahora hay un `Wall Engine` con una entrada
  —el Data Hub— y una salida que TRACE, FLUJO DE ÓRDENES y RESUMEN consumen sin
  recalcular. Se mide como **exposición por strike × interés abierto**, con regla
  de lado, histéresis y tope de distancia **relativo al precio**, no en dólares.
- **Gamma Migration sobre el strike.** `Γ MIG 516 → 517` a la altura del strike que
  ganó exposición, con flecha desde el que la perdió. Sin tarjeta ni panel.
- **QFLOW anclado a la vela exacta.** La marca se resuelve a la vela que *contiene*
  el instante y se apoya en su máximo o su mínimo, no en un precio aproximado de
  otro feed. El detalle enriquecido del Order Flow vive en el hover.
- **Resiliencia del Data Hub:** deduplicación en vuelo, Last Known Good que se
  degrada por edad en vez de desaparecer, merge incremental que no duplica el
  bucket abierto, y aislamiento por canal. Un endpoint lento ya no puede retener el
  ciclo, y **lo que llega tarde alimenta el respaldo** en vez de tirarse.
- **Cambio de símbolo transaccional.** La época avanza antes de tocar nada: una
  respuesta en vuelo del activo anterior se descarta en vez de aterrizar bajo el
  ticker nuevo. Los datasets críticos cargan primero.
- **Verificador LIVE** (`scripts/verify_live_quantdata.py`) para comprobar con
  credenciales reales que cada dataset llega como `DIRECT_PROVIDER` y que la
  muestra sobrevive de la respuesta cruda al frontend.

## v1.43.0 · Quant Data como autoridad de la estructura de opciones

- **El reparto, escrito.** `Alpaca → qué hace el precio` · `Quant Data → cómo está
  posicionada la estructura de opciones y sus Greeks` · `ITM QUANT → qué significa
  todo eso junto`. Hasta v1.42.7 varias secciones preferían el cálculo propio y
  dejaban entrar al proveedor sólo cuando el motor no tenía nada —y lo hacían **en
  silencio**, sin que el operador ni el Auditor pudieran saber cuál veían.
- **Procedencia obligatoria.** Cada métrica registra proveedor, endpoint,
  `source_mode`, valor crudo, normalizado y final, si se usó respaldo y de qué se
  derivó. Cuatro modos: `DIRECT_PROVIDER`, `DERIVED`, `FALLBACK`, `UNAVAILABLE`. Si
  la fuente primaria está sana, ningún cálculo interno puede sustituirla: la guarda
  **falla**, no avisa.
- **TRACE deja de tener un fondo estático.** El mapa es ahora el **Interval Map**
  del proveedor —`eje X = tiempo`, `eje Y = strike`, `intensidad = exposición`—
  alternable entre **GAMMA · DELTA · VANNA · CHARM**, con las velas encima y sobre
  el mismo eje temporal. Así se ve cómo la exposición aparece, crece, se reduce y
  **migra** durante la sesión.
- **QFLOW ya no se queda en Net Flow.** Las concentraciones se atribuyen con
  `order-flow` consolidado y sin consolidar: CALL/PUT, BUY/SELL, strike,
  vencimiento, DTE, prima, agresor y BLOCK/SWEEP/SPLIT. Se marcan `▲ $X.XM` /
  `▼ $X.XM` **a la vez** en el precio y en el panel de flujo.
- **Sin umbrales en dólares.** La concentración se mide contra la distribución del
  **propio activo**, con tres lentes simultáneas. Ningún ticker escrito en código.
- **Dark Pool con fuente directa.** `dark-flow`, `dark-pool-levels` y
  `equity-prints`; el `venue` de otra cinta queda como auditoría. Si hay fallo de
  datos, dice **SIN DATOS**, nunca `$0.0`.
- **El motor, redefinido.** Deja de reconstruir lo que el proveedor entrega y pasa a
  interpretarlo: presión y migración de gamma, confluencia y divergencia,
  persistencia, strikes dominantes, `BREAK`/`CONTAINMENT`/`TRANSITION`, régimen y
  `Structural Score` 0–100. Todo `DERIVED`, con prefijo `ITMQ_`, nunca presentado
  como dato del proveedor.
- **La pantalla muestra análisis.** Endpoints, proveedores y diagnósticos viven en
  el Auditor.

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
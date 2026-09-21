# ITM QUANT MULTI ASSET · v1.56.0 — Cierre integral por gates

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
`ESTADO_v1.56.0.md`.

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
sentinela en vez de describirlo. Inventario: **2852 casos / 183 ficheros**.

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

25 regresiones nuevas. Inventario: **2852 casos / 183 ficheros**.

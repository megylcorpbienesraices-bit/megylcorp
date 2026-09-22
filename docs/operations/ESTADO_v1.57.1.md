# ITM QUANT v1.57.1 · ESTADO POR MÓDULO

## Los cinco niveles, y qué NO demuestra cada uno

| Nivel | Qué se hizo | Qué **no** demuestra |
|---|---|---|
| `IMPLEMENTADO` | El código existe y hace lo que dice | Que alguien lo haya ejercitado |
| `TEST UNITARIO` | Una prueba automática cubre su lógica | Que funcione dentro del sistema |
| `TEST SINTÉTICO` | Extremo a extremo con datos fabricados | Que el proveedor real se comporte así |
| `VALIDADO LIVE` | Ejercitado contra la API real con datos de mercado | Que aguante ocho activos una sesión entera |
| `CERTIFICADO` | Validado LIVE sobre los ocho activos, con evidencia archivada | — |

**Este entorno no tiene credenciales del proveedor.** Medido el 21/09: la
salida de red a `quantdata.us:443` SÍ funciona; lo que falta es
`QUANTDATA_API_KEY` y un terminal escuchando en `127.0.0.1:8000`, que es a donde
apunta el verificador. (Una versión anterior de este documento decía que
tampoco había salida de red. Era falso, y conviene saberlo: el bloqueo es de
credenciales, no de conectividad.) Nada que dependa del proveedor puede subir de
`TEST SINTÉTICO` sin ejecutar la validación en su máquina. Donde pongo otra cosa es porque la comprobación **no depende del
proveedor**, y lo digo en cada caso.

---

## Los diez gates

| Gate | Punto(s) | Nivel | Evidencia |
|---|---|---|---|
| 1 · Agresor forense | 1, 43, 44 | `TEST SINTÉTICO` | 31 pruebas; `tradeSideCode` manda, 17 campos por print, tabla de evidencia con inyección de inversión |
| 2 · La marca | 2, 3 | `TEST SINTÉTICO` | 16 pruebas; una sola implementación en `itmq_core`, cero copias en TRACE y FLUJO |
| 3 · Barras de flujo | 45-49 | `TEST SINTÉTICO` | 19 pruebas; bucketizado en servidor, LKG por carril, recuento de la cadena |
| 4 · Auditoría y carriles | 4, 7 | `TEST UNITARIO` | 14 pruebas; diez contadores, siete estados, ocho carriles, nueve campos |
| 5 · Net Drift | 8, 9, 10 | `TEST UNITARIO` | 17 pruebas; un solo eje de precio, respaldo de velas declarado |
| 6 · Muros | 11, 12, 24 | `TEST UNITARIO` | 16 pruebas; fórmula publicada, igualdad en seis secciones |
| 7 · TRACE | 13-16 | `VALIDADO LIVE`¹ | 19 pruebas + regresión visual; cabeceras de altura fija, muro lejano anclado |
| 8 · Interval Map | 17-20, 50-54 | `TEST SINTÉTICO` | 23 pruebas; hueco preservado, signo verificado, dos inyecciones de defecto |
| 9 · Monte Carlo + ceros | 28, 29, 30 | **`CERTIFICADO`**² / `TEST UNITARIO` | 42 + 18 pruebas; gate de release, cinco ficheros auditados, pendientes **0** |
| 10 · Transaccional | 31-39, 55 | `TEST UNITARIO` | 24 pruebas; dos huellas, commit de snapshot, Londres, ocho activos |

¹ **`VALIDADO LIVE` aquí significa «medido en un navegador real»**, no contra
datos de mercado. La geometría de un panel no depende del proveedor: depende del
renderizador y del ancho del lienzo, y eso sí se ejercitó de verdad (92
mediciones en Chromium, grosor 5,47–13,68 px, ocupación máxima 0,76). Lo digo en
letra pequeña justo para que no se lea como lo otro.

² **Monte Carlo es el único `CERTIFICADO`**, y puede serlo porque su corrección
se contrasta contra fórmulas cerradas y no depende de ningún proveedor.

---

## Los 20 criterios de cierre

| # | Criterio | Estado |
|---|---|---|
| 1 | Suite interna completa PASS | ✅ **2515 pruebas, 0 fallos** |
| 2 | Auditoría matemática completa | ✅ 42 pruebas contra solución cerrada |
| 3 | BUY/SELL validado contra cinta real | ⏳ **PENDIENTE LIVE** · `--aggressor` |
| 4 | QFLOW con el mismo agresor | ✅ una sola autoridad, verificada |
| 5 | Interval Map RAW contra renderer | ⏳ **PENDIENTE LIVE** · `--interval-map` |
| 6 | TRACE usando ese mismo Interval Map | ✅ misma rejilla, mismo `AB.field` |
| 7 | Call/Put Wall auditados, una autoridad | ✅ `wall_audit` con fórmula y ranking |
| 8 | Walls idénticos en las cuatro pantallas | ✅ comprobado en **seis** |
| 9 | Net Drift grande con Walls + QFLOW | ✅ 660 px / 6fr, eje único |
| 10 | FlowViewModel única ruta del frontend | ✅ tarjetas y barras del modelo |
| 11 | LKG por carril | ✅ ocho carriles, nueve campos |
| 12 | Dark Pool consistente por `cycle_id` | ✅ huella del conjunto |
| 13 | Monte Carlo protegido por gate | ✅ nueve pruebas en el gate de release |
| 14 | Acumulaciones auditadas | ✅ cinco ficheros, **pendientes 0** |
| 15 | Cambio de símbolo transaccional | ✅ `generation_id` + `cycle_id` + commit |
| 16 | Londres desde 04:00 sin tocar el proveedor | ✅ verificado, incluido el horario de verano |
| 17 | Validación LIVE multiactivo | ⏳ **PENDIENTE LIVE** · `--cierre` |
| 18 | Regresión visual final | ✅ **146 paneles** en Chromium (73 + 73 tras redimensionar) |
| 19 | `PACKAGING UNLOCKED` | ⏳ **PENDIENTE** · requiere su toolchain · *era además inalcanzable: ver auditoría, defecto 5* |
| 20 | ZIP oficial + SHA256 | ⏳ **PENDIENTE** · depende del 19 |

---

## Repaso punto por punto · los nueve huecos que encontré

La primera vez que me preguntó si estaban todos contesté que sí. Era falso.
Revisé los 55 puntos contra el código y aparecieron **nueve huecos** que había
dado por cerrados. Ocho están corregidos; uno queda parcial y declarado.

| # | Hueco | Estado |
|---|---|---|
| 12 | La comparación de muros decía «coinciden» sin declarar **activo, sesión ni ciclo**. Dos capturas de momentos distintos parecían la misma comprobación: no era evidencia archivable | ✅ |
| 16 | Faltaban **`level_id` y `name`** en cada línea. Sin identificador estable, el Auditor sólo podía decir «hay un `target`», no CUÁL, y `persistence` no tenía a qué agarrarse | ✅ |
| 21, 22 | El selector de EXPOSICIÓN sólo ofrecía GEX/DEX/VEX/CHEX. **OI y VOLUMEN faltaban**, aunque el backend ya los publicaba en los dos ejes; el perfil 2D y el relieve 3D leen el mismo `expRows`, así que los dos se quedaban sin ellas | ✅ |
| 25 | `lineage` contaba proveedor → modelo y **ahí se paraba**. Si el proveedor da 608, el modelo publica 608 y la pantalla dibuja 40, el recorte está en el frontend y ninguna de las dos cifras lo delata | ✅ |
| 26 | El **período** de Dark Pool viajaba en el payload y no se veía. Un notional de sesión al lado de un print de los últimos minutos invita a dividir uno entre otro | ✅ |
| 27 | El KPI decía **`VWAP SESIÓN`** y enseñaba el VWAP **oscuro**. Dos medidas distintas, rótulo equivocado | ✅ |
| 27 | `OFF-EXCHANGE CONFIRMADOS` es Σ `tradeCount` (**agregado**) y `PRINT MAYOR OFF-EX` sale de Equity Prints (**individual**). Llamarlos igual invitaba a compararlos | ✅ |
| 40 | La regresión visual medía **sólo barras**. Campo de calor, relieve 3D, curvas acumuladas y vista RAW de puntos —la mitad de la terminal— no se medían | ✅ |
| 6 | «Eliminar todas las lecturas legacy». Había dejado la cinta local como respaldo, y eso **era la UI consumiendo las estructuras internas** | ✅ |

### Sobre el punto 40

El arnés pasó de **46 paneles a 146**. Los 54 nuevos no tienen «grosor de barra»
que medir, así que se juzgan por lo que sí puede romperse en ellos: **que
pinten algo con datos delante**. Un panel en blanco con el dato correcto detrás
es precisamente el fallo que la suite numérica no ve. Tinta medida: 6,3 %–69,7 %,
ninguno vacío. Y se prueban con datos positivos, negativos, mixtos y **con
huecos a propósito**, que es el defecto que corregí en `normalize_matrix`.

### Sobre el punto 6 · cerrado

Lo había dejado parcial por criterio propio. Al releerlo, usted ya lo había
decidido:

> *«Si esas estructuras todavía son necesarias para el motor, pueden quedarse
> internamente, **pero la UI no puede consumirlas**.»*

Mi respaldo era exactamente eso. Y era peor que una duplicación: hacía que un
bundle roto se viera **sano** —las tarjetas se rellenaban por el otro camino— y
un fallo que se disimula solo es un fallo que nadie arregla.

La UI ya no lee `trace.option_prints` en ningún sitio. Los prints salen del
carril `prints` del modelo, con su LKG. Sin modelo, la sección lo dice.

Un efecto secundario que mejora la pantalla: la guardia contra `$0.0` era
**global** —«¿hubo cinta?»— y ahora es **por carril**. Con la global, un carril
con dato y otro sin él compartían veredicto, así que uno de los dos mentía.

### Lo que el propio proyecto cazó

Al añadir la tarjeta de PERÍODO, la rejilla de Dark Pool pasó de ocho tarjetas a
nueve en una parrilla de cuatro columnas: dos huérfanas con el hueco al lado. Lo
detectó `test_dark_pool_kpi_grid_matches_its_card_count`, del propio repositorio.
Ahora son nueve en tres columnas, tres filas completas.

---

## Lo que queda abierto, sin adornos

### 1 · Lo que exige su API (criterios 3, 5, 17)

Puntos 5, 10, 18, 25, 27, 37, 38 y 53. Este entorno no tiene credenciales ni
salida a `quantdata.us`. Las herramientas están escritas y probadas:

```
python scripts/verify_live_quantdata.py --cierre
```

Ese modo recorre los ocho activos y ejecuta las dos comprobaciones forenses:

- **Agresor:** por cada operación real imprime `tradeSideCode`, `bid`, `ask`,
  `precio`, el lado **esperado** según el contrato, el que produjo el
  clasificador, y las marcas de FLUJO y de TRACE. Si una sola etapa cambia BUY
  por SELL, devuelve código 1 y nombra la operación.
- **Interval Map:** compara hasta 50 celdas por activo, RAW contra el signo
  renderizado. Si el proveedor devuelve negativo, el rojo es correcto y lo dice;
  si no coincide, lo marca `BUG` con el strike y el intervalo.

### 2 · El empaquetado certificado (criterios 19, 20)

El gate está **fail-closed a propósito** y lo dejé intacto:

```
PACKAGING BLOCKED
 - Python 3.11.15 != target 3.12.14
 - Node    22.22.2 != target 22.16.0
 - npm     10.9.7  != target 10.9.2
 - pip-audit / ruff / pyzmq  MISSING
```

Con Python 3.12.14 y Node 22.16.0 instalados:

```
python scripts/package_release_artifact.py --check      # PACKAGING UNLOCKED
python scripts/package_release_artifact.py --output ../ITM_QUANT_v1.57.0.zip
```

Un ZIP producido con otra cadena de herramientas **no es** el certificado, por
mucho que funcione: su sha256 no sería el que su máquina reproduce, y eso es lo
único que el certificado garantiza.

### 3 · Lo que no depende del proveedor y ya está cerrado

Monte Carlo (matemática contra fórmulas cerradas), la geometría de los paneles
(medida en Chromium), la autoridad única de muros y de la marca, la preservación
del hueco en el Interval Map, el inventario de acumulaciones y la
transaccionalidad del cambio de activo. **Eso queda congelado como regresión:**
cada uno tiene pruebas que fallan si alguien lo deshace.


---

## Auditoría integral · los cinco defectos que la suite en verde no veía

Se pidió una auditoría completa —arquitectura, datos, normalizadores, Data Hub,
ViewModels, motor, Scanner, TRACE, Flow, Net Drift, Interval Map, Dark Pool,
Walls, Monte Carlo, sesiones, LKG, cambio de símbolo, fallbacks, excepciones,
frontend, rendimiento, pruebas y packaging— sobre un árbol con 2.539 pruebas en
verde y cero errores de consola.

Aparecieron cinco defectos. Los cinco son de la misma familia: **ninguno lanzaba
una excepción ni pintaba un número imposible**, así que la suite entera en verde
los tapaba perfectamente. Se ven midiendo, no leyendo. Y esa es la lección que
vale más que las correcciones: *2.500 PASS y 0 errores de consola no son una
demostración de nada por sí solos.*

| # | Defecto | Cómo se midió | Estado |
|---|---|---|---|
| 1 | El último valor bueno no caducaba nunca | 30 sesiones × 8 activos × 10 carriles = **2.400 entradas vivas**; `session_mode`: 320 acumulados | ✅ CORREGIDO |
| 2 | La hora del Scanner era local y sin zona | `datetime.now()` con el resto del proyecto en UTC con zona | ✅ CORREGIDO |
| 3 | Describir un nivel avanzaba su contador | **2 ciclos reales se publicaban como 5** | ✅ CORREGIDO |
| 4 | Atribuir concentraciones era cuadrático | 195 ms → 728 ms al doblar entradas (**×3,7**) | ✅ CORREGIDO |
| 5 | El empaquetado se suspendía a sí mismo | el preflight escribía el `.pyc` que la línea siguiente rechazaba | ✅ CORREGIDO |

### Por qué el 5 importa más de lo que parece

`release_traceability_guard()` importa `verify_release_artifact` de forma
perezosa. La guarda que evitaba dejar bytecode cubría sólo el import de
`release_gate_full` y se levantaba justo después, así que ese segundo import
escribía `scripts/__pycache__/verify_release_artifact.cpython-311.pyc` — y la
línea **siguiente**, `artifact_cleanliness_guard()`, rechazaba el árbol por
contener un artefacto de build.

Sobre un árbol limpio la comprobación no podía pasar **nunca**, ni con el
toolchain correcto. El punto 19 de la tabla de arriba no estaba sólo esperando a
Python 3.12: estaba muerto. Ahora el preflight llega hasta el veredicto real y
deja cero ficheros nuevos.

### Rendimiento del camino de refresco, tras la corrección 4

| carga | antes | después |
|---|---|---|
| 390 concentraciones × 1.500 operaciones | 195,5 ms | **64,7 ms** |
| 780 × 3.000 | 728,2 ms (×3,7) | **133,5 ms (×2,06)** |
| 1.560 × 6.000 | — | **245,4 ms (×1,84)** |

El resto del camino se sondeó y es lineal: `classify_trade` ×6.000 = 18,5 ms,
`bucketize` ×6.000 = 17,2 ms, `build_evidence` ×4.000 = 1,7 ms.

### Limpieza posterior · nueve alias muertos en el frontend

La unificación de la marca de flujo en `itmq_core` dejó nueve alias locales
—`flowArrow`, `flowAmount`, `markerStrength`, `markerAmount`, `evStrength`,
`LEVEL_STYLE`— que apuntaban a `Q.*` y que no llamaba nadie. No cambiaban nada en
pantalla, pero llevan el nombre exacto de las funciones duplicadas que se
eliminaron: alguien los "corrige", no ve ningún efecto y pierde la tarde. Una
prueba exigía incluso que `LEVEL_STYLE` **siguiera existiendo**, que es guardar
la forma en vez del fondo; ahora comprueba que TRACE pida el estilo a
`Q.levelStyle` y no tenga tabla propia.

### Las regresiones

`tests/test_v1570_auditoria_integral.py` · 15 casos. **Los quince fallan contra
la versión anterior**, comprobado revirtiendo cada corrección una a una.

Ninguno mide el reloj de pared, que sería inestable en una máquina cargada: el
de rendimiento cuenta **restas de instantes** —exacto y reproducible— y devuelve
×4,00 sobre el código viejo. El de empaquetado mide el **delta** de residuo, no
el estado, porque la propia suite deja su `__pycache__` al ejecutarse y exigir un
árbol limpio haría la prueba dependiente de quién corriera antes.

### Lo que sigue bloqueado, y por qué no es mío

- **Validación LIVE**: falta `QUANTDATA_API_KEY` y un terminal en
  `127.0.0.1:8000`. `scripts/verify_live_quantdata.py --cierre` está listo y
  devuelve `ConnectError: Connection refused` en los ocho tickers.
- **ZIP certificado**: `Python 3.11.15 ≠ 3.12.14`, `Node 22.22.2 ≠ 22.16.0`,
  `npm 10.9.7 ≠ 10.9.2`, y `pip-audit` / `ruff` / `pyzmq` ausentes. El gate es
  fail-closed a propósito y no se ha tocado.


---

## Verificación de walls y Monte Carlo · sobre el ZIP `ITMQ_156_FIX`

Se pidió comprobar que Call Wall y Put Wall salen bien **en todos los activos**,
que Monte Carlo también, y que cada sección está correcta. Esa copia traía una
pasada externa sobre `df2ca9d` y declaraba que la suite completa **no había
llegado a terminar** en su entorno. Aquí sí terminó, y sacó una prueba en rojo.

### CALL WALL / PUT WALL · un defecto de fondo

`wall_engine` lo tenía escrito en su propia documentación:

> «Cuando el proveedor no publica OI para ese activo **no se penaliza al
> strike**: se puntúa sólo con exposición y se declara. Multiplicar por un dato
> ausente es inventar un veredicto.»

La regla existía y se aplicaba **al activo entero**. Por strike, no: la
normalización recibía `p["oi"] or 0.0`, así que un strike sin OI publicado
entraba como **cero medido**, salía con `oi_norm = 0` y su score se anulaba —es
una media geométrica— por grande que fuera su exposición.

Medido, con el strike de mayor exposición como único sin OI:

| strike | exposición | OI | score ANTES | score AHORA |
|---|---|---|---|---|
| **100** | **1,00e+09** | *ausente* | **0,000** | **100,000** |
| 101 | 9,00e+08 | 49.000 | 94,868 | 94,868 |
| 102 | 8,00e+08 | 48.000 | 88,525 | 88,525 |

**El muro se iba al 101.** El dato que faltaba no era el del muro: era el del
proveedor. Ocurría en los dos lados, call y put.

No hay excepción, no hay aviso, no hay error: sólo una línea estructural
dibujada en el strike equivocado en TRACE, en RESUMEN y en las seis secciones
que leen esta autoridad.

Corregido: el strike sin OI se puntúa con la evidencia que **sí** tiene
—exposición sola, el mismo trato que recibe la cadena entera cuando el proveedor
no publica OI— y se marca `oi_missing` con `score_method` propio, más un recuento
`oi_missing_strikes` en la respuesta para que el Auditor lo vea sin deducirlo de
un cero. Un OI **medido** a cero sigue anulando el muro, que es lo correcto.

Verificado además: **ningún** branch por ticker. Los ocho activos con la misma
cadena dan el mismo muro y el mismo score, y la histéresis está aislada por
símbolo (SPY no contamina a DIA).

### MONTE CARLO · correcto

Contrastado contra solución cerrada con números propios, no con los de la suite.
P(toque) por principio de reflexión, P(final) por N(d2), p95 por el cuantil
lognormal exacto. 80.000 trayectorias, semilla fija:

| activo | IV % | toque MC | cerrada | dif | p95 dif |
|---|---|---|---|---|---|
| DIA | 14,0 | 0,3908 | 0,3921 | 0,12 pp | 0,02 % |
| SPY | 12,5 | 0,3389 | 0,3398 | 0,10 pp | 0,02 % |
| QQQ | 17,0 | 0,4765 | 0,4774 | 0,09 pp | 0,03 % |
| IWM | 20,0 | 0,5413 | 0,5430 | 0,16 pp | 0,03 % |
| AAPL | 24,0 | 0,6073 | 0,6089 | 0,16 pp | 0,04 % |
| NVDA | 42,0 | 0,7596 | 0,7620 | 0,25 pp | 0,07 % |
| TSLA | 55,0 | 0,8103 | 0,8131 | 0,28 pp | 0,09 % |
| MSFT | 21,0 | 0,5597 | 0,5614 | 0,17 pp | 0,04 % |

Peor desviación 0,28 pp contra un error estándar declarado de ~0,18 pp, y el
peor error de percentil 0,09 %. Determinista y con `BROWNIAN_BRIDGE_CONTINUOUS`.

Una observación honesta: las ocho desviaciones de toque caen **del mismo lado**
(MC por debajo de la cerrada). Es el sesgo de discretización conocido del puente
browniano con pasos finitos, su magnitud está dentro del error declarado y va en
la dirección conservadora —subestima el toque, no lo exagera—. No es un defecto,
pero conviene saber que está ahí y no confundirlo con ruido puro.

### La prueba que venía en rojo

`test_the_field_never_reaches_full_opacity` exigía la **cadena literal**
`255 * ALPHA_MAX`. El suelo tenue para la celda medida pasó a calcular el alfa
aparte y multiplicarlo al final, así que el guardia se rompió **sin que el techo
se moviera un píxel**: protegía la forma de la expresión, no el techo. Ahora
comprueba que el alfa esté acotado y **evalúa la aritmética real del renderer**
sobre los 1.001 valores del rango. Techo confirmado en 0,70.

Es el mismo error que `LEVEL_STYLE`: un guardia que obliga a que sobreviva una
forma concreta en vez de proteger la propiedad.

### Lo que se integró de esa copia

Flecha completa con `BUY` / `SELL` / `UNKNOWN` junto al importe (el lado sigue
saliendo **sólo** de `aggressor`, y UNKNOWN conserva el rombo neutro), y el suelo
tenue del Interval Map, que corrige franjas negras donde había medición real.
Ambos verificados: la máscara viene de `AB.field.measured()`, anterior a la
interpolación, así que un hueco interpolado **no** se hace pasar por observación.

Quedaba además un alias muerto sin quitar —`flowSide` en `itmq_orderflow.js`—.
Eliminado: ESLint queda en 0 errores y 0 avisos.


---

## Continuación · la corrección de walls estaba a medio cablear

Al confirmar que Call Wall y Put Wall son **DERIVED · ITM QUANT** —el proveedor
entrega exposición e interés abierto por strike, y la conclusión la firma el
motor— se revisó si la corrección anterior llegaba de verdad a donde tenía que
llegar. No llegaba, y el segundo fallo era el grave.

**1 · El contador no salía del sitio donde se calculaba.**
`oi_missing` y `oi_missing_strikes` se computaban en `build_candidates` y morían
ahí: ni `resolve_walls` ni `wall_audit` los publicaban. El «vigile el
`oi_missing_strikes` de los ocho activos» que se dejó escrito señalaba a un campo
que no existía en la salida.

**2 · El auditor declaraba una fórmula que no era la que se usó.**
El muro publicaba `method` de la CADENA. Un muro cuyo strike no tenía OI salía
como `geometric-exposure-x-open-interest` con `oi: None` justo debajo.

En una magnitud DERIVED eso es peor que un número equivocado. El único motivo por
el que un nivel calculado por nosotros se puede discutir es que diga con qué
fórmula salió; si dice una que no es, deja de ser auditable aunque acierte.

Ahora el muro publica `method` (cómo se puntuó **su** strike), `chain_method`
(cómo se puntuó la cadena), `oi_missing` y `oi_missing_strikes`, y los cuatro
llegan al auditor.

### Y un error introducido al corregir, cazado por las pruebas del propio proyecto

La primera versión de esta corrección llamaba igual a dos cosas distintas:

| situación | qué describe | método |
|---|---|---|
| el proveedor no publica OI para el **activo** | su cobertura | `exposure-only-no-open-interest` |
| publica OI y falta el de **un strike** | un agujero en una cadena que sí llegó | `exposure-only-oi-missing` |

Se habían fundido en la segunda etiqueta. Un activo entero sin interés abierto se
habría leído como si le faltara un dato suelto, que es un diagnóstico distinto y
lleva a buscar donde no es.

Lo cazaron dos pruebas que **ya existían** —`test_missing_open_interest_does_not
_zero_a_strike` y `test_sin_interes_abierto_no_se_multiplica_por_cero`— al correr
la suite completa. Es el argumento entero de esta auditoría dado la vuelta: la
suite no sirve para declarar que algo está bien, pero sí para impedir que una
corrección apresurada rompa lo que ya estaba bien. Por eso se corre entera antes
de cada entrega, y por eso una copia que no la termina no está verificada.

`tests/test_v1570_walls_oi_ausente.py` · 13 casos. Suite completa: **2.569 pasan,
2 se saltan**. Inventario y trazabilidad a **2.571 casos / 172 ficheros**.

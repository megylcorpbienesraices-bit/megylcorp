# VALIDACIÓN PRE-VPS · ITM QUANT v1.56.0

Release: `ITM_QUANT_v1.56.0_PRE_VPS` · Alcance: `MULTI_ASSET`

> Documento acumulativo. La sección **A19** es la de esta release.

## Suite

- Inventario nominal: **2541 casos / 170 ficheros**
- Particiones: 21 · `plan_sha256`: `80e21e9db087ebdd31736904ff797619a0edbd0ad5ab516ebf8ab0a87df293ca`
- Resultado: **PASS**

---

## A19 · v1.56.0 · Dónde se rompe, y qué no se borra

### Qué se certifica

Treinta y siete casos en `tests/test_v1540_flow_freshness.py`.

**Los siete escenarios obligatorios de FLUJO (A–G).** A: live con datos nuevos.
B: ciclo vacío conserva 405 buckets y marca STALE. C: mercado cerrado carga la
última sesión válida. D: DIA → QQQ sin arrastre, y la sesión también forma parte
de la clave. E: Net Flow vivo con cinta vieja. F: cinta viva con Net Flow
ausente. G: sin histórico ni actual, **sólo entonces** SIN DATOS.

**Más las reglas transversales:** un cero medido se publica como cero y se
conserva; un error del proveedor no borra el último bueno; la prima sin agresor
tiene su propio cubo; la pantalla operativa no escribe jerga técnica; un hueco
corto sigue siendo LIVE porque un mercado tranquilo no es un fallo.

**El diagnóstico del agresor en sus cinco estados**, parametrizado, comprobando
que se señala el PRIMER eslabón roto y que cada uno lleva remedio.

**El plan del Scanner:** sin tesis no inventa nada; una tesis completa produce
las cuatro líneas con fuente `SCANNER`; el `thesis_id` cambia si cambia
cualquier pieza; el precio que cruza la invalidación marca el plan invalidado
—leyendo el nivel que el Scanner publicó, no recalculando dirección—; no conoce
ningún ticker; y sustituye a `target`/`risk` en vez de duplicarlos.

**La sesión de Londres:** entra a las 04:00 de Ecuador; el corte de Nueva York
sigue SU horario de verano (julio vs. diciembre); Londres no se borra al empezar
Nueva York; el acumulado sólo crece con lo medido; la clave es símbolo + fecha +
sesión; el VWAP pondera por volumen.

### Verificación en la terminal real

`127.0.0.1:8839`, **0 errores de consola**.

```
SCANNER  VENTA  80/100  Entrada 534.20  INVAL 535.62  OBJ1 533.70  OBJ2 533.20  ACTIVO
plan: ACTIVO · 4 líneas
agresor: cadena sana (broken_at = None)
```

El TRACE dibuja `INVAL`, `ENT`, `OBJ1` y `OBJ2` **una sola vez cada una**: antes
de la deduplicación salían `OBJ1 533.70` y `OBJ 533.70` pegadas.

### Límites de esta verificación

- **La cadena del agresor sale SANA aquí**, así que el caso del usuario no se
  reproduce en este entorno. El diagnóstico es la herramienta para localizarlo
  en su terminal LIVE.
- **El `FlowViewModel` está probado y publicado pero aún no cableado a la
  pantalla**: la sección sigue leyendo por la ruta anterior. Queda para la
  siguiente entrega.
- **El acumulado de Londres se prueba con relojes construidos.**
- **Empaquetado certificado imposible aquí**: 3.11.15 / 22.22.2.

---

## A18 · v1.53.1 · La identidad de cada línea

### Qué se certifica

Once casos en `tests/test_v1531_level_identity.py`.

**1 · El color NO puede identificar una línea.** Se comprueba sobre la tabla de
estilo que `--neg` agrupa `put_wall` Y `risk`, y `--pos` agrupa `call_wall` Y
`target`. Si alguien añade un `kind` a esos colores, salta.

**2 · Todo `kind` que emite el motor tiene origen declarado.** Se extraen los
`kind` de `structure_levels` por análisis del propio fuente y se exige que estén
en `LEVEL_ORIGIN`.

**3 · El origen nombra la función y el campo reales**, no una descripción.

**4 · `describe` expone los siete campos** pedidos, y sin magnitud NO fabrica un
cero.

**5 · Un `kind` no registrado lo dice** en vez de inventarse un origen.

**6 · La autoridad del nivel gana al registro**: el Wall Engine manda sobre el
respaldo.

**7 · La persistencia cuenta ciclos consecutivos** y se reinicia al moverse.

**8 · La tolerancia es relativa**: un dólar en 5.800 es el mismo nivel; en 40, es
otro.

**9 · La identidad viaja en el bundle** sin cambiar el cálculo.

**10 · El Auditor muestra las nueve columnas.**

**11 · Toda línea visible lleva etiqueta**, con abreviatura antes que silencio.

### Volcado contra el motor real

`127.0.0.1:8819` · **11 líneas**, cada una con su función y su campo. Cálculo de
cuáles quedaban sin rotular con el cupo anterior:

```
SIN ETIQUETA:
  533.70  T1            type=target  (order 7)
  533.20  T2            type=target  (order 7)
  535.62  Invalidación  type=risk    (order 7)
```

### Verificación en la terminal real

`127.0.0.1:8823`, **0 errores de consola**. Tabla del Auditor con 11 filas, y el
TRACE rotulando las once líneas: `INVAL 535.62`, `Zero Gamma 535.16`,
`CALL WALL 534.70`, `ZONA 534.29`, `Delta Center 534.28`, `Gamma Center 534.23`,
`Zona Low 534.11`, `PUT WALL 533.70`, `OBJ 533.70`, `OBJ 533.20`,
`▼ VT 530.20 −4.02`.

### Límites de esta verificación

- Los precios de la sesión del usuario —roja ~517.2, verde ~515.0— son de su
  activo en vivo. Aquí el mismo `type` sale en otros precios porque el demo
  cotiza en otro nivel. **La identidad es la misma y es la que se comprobó.**
- Empaquetado certificado imposible aquí: 3.11.15 / 22.22.2 frente a
  3.12.14 / 22.16.0 fail-closed.

---

## A17 · v1.53.0 · El agresor en producción y el campo visible

### Qué se certifica

Nueve casos nuevos en `tests/test_v1520_aggressor_and_dark_pool.py`.

**1 · El agresor cae al NBBO cuando ningún campo lo declara.** Precio en el ask
→ COMPRA; en el bid → VENTA; en medio → sin agresor. Y el campo declarado
**siempre gana** al NBBO.

**2 · La holgura es relativa al spread.** Un céntimo por debajo del ask sigue
siendo compra con spread ancho, y ya no cruza con spread estrecho.

**3 · Un mercado cruzado o bloqueado no da lado.**

**4 · La cinta publica su cobertura**, con el campo que sirvió cada print, y el
NBBO viaja en la fila para poder auditar la clasificación.

**5 · Un lado dominante deja de llamarse repartido.** 65/35 y 61/39 son COMPRA;
55/45 sigue siendo MIXED. La confianza viaja.

**6 · Ninguna fila se pierde entre el proveedor y el modelo.** 608 y 349 llegan
enteras, con el conteo por etapa publicado, y `equity_prints` en MARKET_CLOSED no
vacía a los otros dos.

**7 · El diagnóstico nombra el carril del proveedor**, no el derivado.

**8 · Un nivel fuera de ventana se ancla, no se descarta**, y los de dentro
conservan prioridad de etiqueta.

**9 · El campo de calor se normaliza sobre lo que está en pantalla**, con margen,
con suelo de filas y respetando las dos orientaciones de la matriz.

### Verificación en la terminal real

`127.0.0.1:8811` y `:8815`, **0 errores de consola**.

Recorte del campo, con una matriz de 90 strikes inyectada —la forma que tiene el
Interval Map real del proveedor y que en demo no existe:

```
enviados 90 strikes · ventana 530.68 – 538.24
resultado: 13 filas · 77 recortadas · rango 528.46 – 540.46
origen DIRECT_PROVIDER · 210 segmentos de isolínea (antes 94)
```

Niveles: el TRACE dibuja ahora ocho etiquetas —Zero Gamma, CALL WALL, Zona High,
Delta Center, Gamma Center, Zona Low, PUT WALL, T1— donde antes cabían seis.

### Límites de esta verificación

- **La vía NBBO no se ha contrastado contra una cinta real.** Sin credenciales ni
  salida al proveedor. La cobertura publicada es la herramienta para comprobarlo
  en la terminal LIVE del usuario.
- **El recorte se verificó por inyección**, porque en demo el respaldo del motor
  ya cabe en la ventana y no hay nada que recortar.
- **Empaquetado certificado imposible aquí**: 3.11.15 / 22.22.2 frente a
  3.12.14 / 22.16.0 fail-closed.

---

## A16 · v1.52.0 · El lado agresor, y el dato que ya estaba

### Qué se certifica

Fichero nuevo `tests/test_v1520_aggressor_and_dark_pool.py`, 57 casos.

**1 · El lado agresor, valor a valor.** 30 casos parametrizados, incluidos los
dos que la versión anterior **invertía**: `AT_BID` y `ABOVE_BID` pasan de COMPRA
a VENTA. También `MID` y `MIDPOINT` (sin agresor, no compra) y `CALL`/`PUT` (el
tipo de contrato no es una dirección).

**2 · El campo inequívoco gana al ambiguo.** `aggressor` antes que `side`,
porque en algunas respuestas `side` es el tipo de contrato.

**3 · Ningún módulo clasifica por su cuenta.** Se comprueba sobre el código que
la comparación por prefijo corto ya no existe en el normalizador ni en QFLOW.

**4 · Dominancia de prima ≠ dirección.** Un intervalo `CALL_DOMINANT` con
120.000 de compra y 980.000 de venta sale `aggressor: SELL`.

**5 · Un bucket repartido es MIXED**, no un lado.

**6 · Sin cinta en la ventana, `UNKNOWN` con su causa.** Y cero prima agredida
es «no medido», no «repartido».

**7 · La flecha sigue al agresor.** Una put **comprada** lleva ▲; una call
**vendida**, ▼; lo desconocido y lo repartido, ◆.

**8 · El frontend no dibuja flecha sin lado conocido.** Se comprueba que
`flowSide` sólo mira `ev.aggressor`, que no menciona `CALL`, `PUT` ni `premium`,
y que devuelve `null`.

**9 · Dark Flow mapea el contrato.** `field_map` sale exactamente
`{dark_volume: size, dark_notional: notionalValue, dark_prints: tradeCount,
stock_price: stockPrice}`.

**10 · Sin `size` es SCHEMA_MISMATCH**, no cero.

**11 · La cadena entera, valor a valor.** Tres buckets reales y tres niveles
reales: `69428 → 69428 → 69428`, agregados que son la suma exacta, nivel
dominante por notional y distancia al spot derivada.

**12 · Una capa derivada ya no bloquea a la fuente directa.** Se comprueba
además sobre el código que el modelo no lee `large_prints`,
`off_exchange_liquidity_zones` ni `venue_classification`.

**13 · Cada carril declara su estado con causa**: `NO_PROVIDER_DATA`,
`PROVIDER_ERROR`, `SCHEMA_MISMATCH`.

**14 · El % fuera de bolsa no se estima**, y con prints dark + lit sí se mide.

**15 · El VWAP oscuro pondera por acciones.**

**16 · Equity Prints pide una sesión real**, y un fin de semana resuelve al
viernes.

**17 · La ráfaga de arranque está acotada** por plazo, alcance y salida
anticipada, y el cambio de símbolo **sigue siendo transaccional**: la época
avanza antes de tocar nada.

**18 · Las ocho opciones del mapa tienen destino**, y un mapa sin dato declara
su causa en vez de quedarse en blanco.

### Verificación en la terminal real

`127.0.0.1:8803` y `:8807`, **0 errores de consola**.

Las ocho opciones del MAPA DINÁMICO, ejercitadas una por una:

```
OPCIÓN       ETIQUETA       ORIGEN DECLARADO                    CAMPO
gamma        GAMMA          estructura propia (respaldo)        10x21 · 94 seg
delta        DELTA          estructura propia (respaldo)        10x21 · 95 seg
vanna        VANNA          SIN INTERVAL MAP NI HISTORIA        sin campo
charm        CHARM          estructura propia (respaldo)        10x21 · 85 seg
joint        Γ + Δ          modelo propio                       10x21 · 93 seg
net_oi       OI NETO        modelo propio                       10x21 · 68 seg
net_volume   VOLUMEN NETO   modelo propio                       10x21 · 72 seg
off          —              mapa apagado                        sin campo
```

Siete pintan campo con isolíneas; VANNA es la única sin dato **y lo declara**.

Relieve 3D: panel crecido a 665 px con scroll, y el eje numera la secuencia
completa —530.20, 530.70, 531.20, 531.70…— sin saltarse ningún strike.

### Límites de esta verificación

- **El lado agresor no se ha contrastado contra una cinta real**: este entorno no
  tiene credenciales ni salida al proveedor. Se verifica el clasificador valor a
  valor y la cadena del normalizador.
- **La cadena de Dark Pool** se comprueba con la forma publicada del contrato; la
  ejecución contra el proveedor real queda en la terminal del usuario, que ya
  está LIVE.
- **Con proveedor LIVE** las cuatro griegas deberían venir del Interval Map en
  vez del respaldo del motor; aquí el respaldo es lo que hay.
- **Empaquetado certificado imposible aquí**: 3.11.15 / 22.22.2 frente a
  3.12.14 / 22.16.0 fail-closed.

---

## A15 · v1.51.0 · Un hueco deja de valer cero

### Qué se certifica

Dieciséis casos nuevos en `tests/test_v1470_adaptive_rendering.py`, más uno
reescrito en `tests/test_v1415_native_iv_rank_and_volume_provenance.py`.

**1 · Un valor ausente es un hueco, no un cero.**
`test_an_absent_value_is_a_gap_not_a_zero`: un bucket sin el campo de IV entre
dos que sí lo tienen sale `None` con `value_measured: False`.

**2 · La regla cubre las cinco herramientas del normalizador.**
`test_the_gap_rule_covers_every_series_of_this_normalizer`: IV, max pain, interés
abierto, precio y prima neta. Un max pain en el strike 0 o un precio de 0 dólares
son afirmaciones igual de falsas que una IV de 0 %.

**3 · Una prima neta con call y put SIGUE siendo una medición.**
`test_a_net_premium_with_call_and_put_is_still_measured`: 900 − 250 = 650, con
`value_measured: True`. El cambio no puede convertir en hueco una lectura real.

**4 · La curva se parte en el hueco.**
`test_the_curve_breaks_at_a_gap_instead_of_falling_to_the_floor`: el punto se
mapea con `NaN`, no con cero, y el trazo se reinicia por tramo.

**5 · La ventana degenerada de IV no publica ni rank ni percentil.**
`test_a_flat_history_publishes_neither_rank_nor_percentile`, sobre **seis
activos** (DIA, SPY, QQQ, IWM, AAPL, SOFI) y comprobando además que con amplitud
real los dos números vuelven. Invierte explícitamente la decisión de v1.41.5.

**6 · Y la sección dice la causa.**
`test_a_degenerate_iv_window_reports_its_cause_on_screen`.

**7 · Los totales de Net Drift tampoco fabrican ceros.**
`test_net_drift_totals_are_never_a_fabricated_zero`.

**8 · El relieve es un sólido, no una barra con un borde.**
`test_the_relief_is_a_solid_not_a_bar_with_an_edge`: tres caras con tres
opacidades distintas. Sin gradación de luz no hay volumen.

**9 · La fuga no puede salirse del lienzo.**
`test_the_relief_depth_cannot_run_off_the_canvas`: acotada en píxeles absolutos
**y** el contenedor con alto propio, no el crecido del perfil de barras.

**10 · El suelo es un plano cerrado.**
`test_the_relief_floor_is_a_closed_plane`.

**11 · Los contornos son líneas continuas.**
`test_the_contours_are_continuous_lines_not_loose_dashes`: marching squares, con
las sillas de montar separadas en dos ramas para no inventar una conexión que el
campo no tiene.

**12 · TRACE y la sección comparten tratamiento del campo.**
`test_trace_and_the_section_share_one_field_treatment`: mismos umbrales, mismo
suelo de ruido, mismo techo de opacidad.

**13 · El campo nunca llega a opaco.**
`test_the_field_never_reaches_full_opacity`.

**14 · El Interval Map de la sección es una rejilla de puntos.**
`test_the_section_interval_map_is_a_dot_grid`: sin suavizar ni rellenar, con el
diámetro como magnitud, y un hueco no se dibuja mientras que un cero medido sí.

**15 · Net Drift cuelga los muros del eje del PRECIO, con UNA sola escala.**
`test_net_drift_draws_walls_on_a_price_axis_not_on_the_premium_axis` y
`test_net_drift_marks_the_flow_with_the_same_functions_as_the_rest`.

**16 · La rotulación de niveles se define una vez.**
`test_level_typography_is_defined_once_for_the_whole_terminal`.

**17 · Los dos gráficos principales tienen suelo en píxeles.**
`test_the_two_main_charts_have_a_floor_in_pixels`.

**18 · Nada de esta release conoce un ticker.**
`test_none_of_this_release_knows_a_ticker`: seis símbolos contra seis ficheros.
Todo cambio es global.

### El error que se cometió y se corrigió dentro de la propia release

La primera implementación de los muros en Net Drift **creó un eje de precio
nuevo**. En la captura salieron **dos columnas de precios a la derecha**, con
dominios distintos: la mía abarcaba 530–535 porque incluía el Vol Trigger, y la
que ya existía abarcaba 533.20–534.20. Dos escalas de precio en el mismo gráfico
es peor que no dibujar los muros: las dos parecen válidas y sólo una sitúa bien
la línea. Se eliminó el eje nuevo y los muros y las marcas pasaron al que ya
había.

Se detectó **mirando la captura**, no por un test. Por eso hay ahora un test que
cuenta las escalas: `assert body.count("Q.scale(plo") == 1`.

### Verificación visual

Terminal real en `127.0.0.1:8795`, **0 errores de consola**, siete capturas:

| Panel | Qué se ve |
|---|---|
| TRACE | Ocupa ~800 px de alto. Etiquetas de nivel legibles a tamaño normal. Isolíneas continuas sobre un campo pastel. |
| EXPOSICIÓN · Relieve 3D | Prismas con volumen real, suelo en fuga, escala de valor abajo y **los 21 strikes etiquetados**. |
| INTERÉS ABIERTO | 530.20, 530.70, 531.20, 531.70… **sin saltarse ninguno**. |
| INTERVAL MAP | Rejilla de puntos roja/verde con el diámetro como magnitud y la línea de precio azul encima. |
| VOLATILIDAD | `IV PERCENTIL —` con `RANGO OBSERVADO SIN AMPLITUD · 44 lecturas idénticas`. |
| NET DRIFT | Zero Gamma, Call Wall, Δ Center, Γ Center y Put Wall en el eje del precio; marcas doradas con flecha e importe; una sola escala. |
| FLUJO | Carril TOTAL declarando su causa en vez de quedarse mudo. |

### Límites de esta verificación

- **Net Drift se verificó inyectando una serie sintética** en el navegador real:
  este entorno no tiene serie de net-drift del proveedor. Lo verificado es la
  **geometría del render**, no el dato del proveedor.
- **Sin cinta de opciones real**: las marcas doradas sobre flujo vivo no se han
  podido fotografiar.
- **Sin HTTP 200 real de `dark-pool-levels`**: sin salida ni credenciales.
- **Empaquetado certificado imposible aquí**: 3.11.15 / 22.22.2 frente a
  3.12.14 / 22.16.0 fail-closed.

---

## A14 · v1.50.0 · Ventana de strikes, marca del flujo y espacio del Net Drift

### Qué se certifica

Diez casos nuevos en `tests/test_v1470_adaptive_rendering.py`.

**1 · El perfil descarta los strikes que no pueden importar.**
`test_the_strike_profile_drops_strikes_that_cannot_matter`: con el subyacente en
534 y filas hasta 433, las lejanas irrelevantes se van y las de la zona operable
se quedan.

**2 · Un muro real fuera de la banda NUNCA se oculta.**
`test_a_real_wall_outside_the_band_is_never_hidden`. Es el caso que la regla
existe para proteger: si se perdiera un muro por recortar, el recorte sería peor
que el problema.

**3 · La banda es proporcional, así que sirve para cualquier activo.**
`test_the_band_is_proportional_so_it_works_for_every_asset`: el mismo 6 % da 32
dólares en un ETF de 534 y 348 en un índice de 5.800. Ningún umbral absoluto.

**4 · Pocos strikes no se recortan nunca.**
`test_few_strikes_are_never_trimmed`: `MIN_ROWS = 12` protege el libro corto.

**5 · Los dos paneles declaran lo que recortaron.**
`test_both_strike_panels_declare_what_they_trimmed`: EXPOSICIÓN e INTERÉS
ABIERTO publican `strike_window` con banda, total, conservadas, descartadas y
motivo. Un recorte silencioso no sería auditable.

**6 · La marca de flujo es dorada, con flecha e importe.**
`test_flow_markers_are_golden_with_an_arrow_and_an_amount`, sobre TRACE **y**
sobre FLUJO DE ÓRDENES, desde las mismas funciones (`flowHalo`, `flowArrow`,
`flowAmount`, `flowIsBuy`).

**7 · El radio compara dentro del ciclo.**
`test_the_marker_radius_compares_within_the_cycle`: `S.qflowPeak` / `qPeak`. Con
una referencia absoluta, dos marcas del mismo tamaño no significarían nada.

**8 · Net Drift tiene carril TOTAL con marcado dorado declarado.**
`test_net_drift_has_a_total_lane_with_declared_gold_marking`: el criterio es
`Top 3` o `≥10×` la media, elegible, y la media se dibuja como referencia.

**9 · Cada curva lleva su valor al final.**
`test_the_drift_curves_carry_their_value_at_the_end`, con separación de 17 px
cuando dos etiquetas colisionan.

**10 · El gráfico tiene el sitio que necesita.**
`test_the_drift_chart_got_the_room_it_needs`: `minmax(0, 2.2fr)`.

**+ Ningún carril mudo.**
`test_every_lane_of_the_tape_declares_why_it_is_empty`: con ejes dibujados y
ningún contenedor con prima, el carril declara `SIN PRIMA OBSERVADA` en vez de
quedarse en blanco.

### El defecto que encontró el propio test

`test_the_band_is_proportional_so_it_works_for_every_asset` falló la primera vez
con un perfil **plano**: al ser todas las filas parecidas, *cada* strike lejano
superaba `MATERIAL_FRACTION` del máximo y no se recortaba nada. La regla se
completó con `MATERIAL_STANDOUT = 3.0` contra la **mediana de las lejanas**: lo
que importa no es ser grande, es destacar en su propio vecindario.

### Verificación visual

Terminal real en `127.0.0.1:8781`, **0 errores de consola**, cinco capturas:

| Panel | Qué se ve |
|---|---|
| EXPOSICIÓN | 21 strikes de 530.20 a 540.20, una barra gruesa por strike, con el conmutador `Barras / Relieve 3D`. Los 433, 445 y 473 de antes ya no están. |
| INTERÉS ABIERTO | Mismo recorte, barras call/put gruesas y separadas sobre el eje de strike. |
| TRACE | DEX a la izquierda, campo continuo con contornos y velas al centro, GEX a la derecha, todos sobre el mismo eje de precio. |
| FLUJO DE ÓRDENES | Curva del subyacente con walls, y los tres carriles inferiores declarando su causa cuando no hay cinta. |
| NET DRIFT | Gráfico grande con sus controles `Cinta / Net Drift` y `Dorado: Top 3 / ≥10×`. |

### Límites de esta verificación

- **Sin cinta de opciones real** en este entorno: las marcas doradas se validan
  por test sobre el código de render y por la regresión visual geométrica, no
  fotografiadas sobre flujo vivo.
- **Sin HTTP 200 real de `dark-pool-levels`**: sin salida a `quantdata.us` ni
  credenciales. Pendiente de `scripts/verify_live_quantdata.py` con la API key.
- **Empaquetado certificado imposible aquí**: el empaquetador oficial es
  fail-closed sobre Python 3.12.14 / Node 22.16.0 y este entorno corre
  3.11.15 / 22.22.2.

---

## A13 · v1.49.0 · El contrato de `dark-pool-levels`

### Qué se certifica

Diez casos nuevos en `tests/test_v1470_adaptive_rendering.py`.

**1 · El cuerpo publicado.** Que el request sea exactamente
`{sessionDateRange: {startDate…}, filter: {ticker}}`, con `startDate` presente y
en formato `YYYY-MM-DD`, y sin ningún otro campo en el nivel superior.

**2 · Los siete campos prohibidos**, uno a uno: `sessionDate`, `timeRange`,
`snapshotTime`, `aggregationPeriod`, `filterExpression`, `projection`,
`pagination`.

**3 · La reparación no puede romper el cuerpo.** Que `repair_body` no añada
`aggregationPeriod` a `dark-pool-levels` —y que sin la prohibición por
herramienta **sí** lo añadiría, que es la diferencia que importa—.

**4 · La fecha nunca es un día sin sesión.** Sábado 19 y domingo 20 de
septiembre de 2026 resuelven los dos al viernes 18.

**5 · El 200 documentado.** Que el mapa por nivel de precio se lea con la clave
como precio y con `notionalValue`, `size`, `tradeCount` y `latestStockPrice`; y
que una lista siga funcionando por si cambia el envoltorio.

**6 · La cadena completa.** `RAW → NORMALIZER → DATA HUB → DARK POOL LEVELS →
FRONTEND`, con `DIRECT_PROVIDER` en el registro y los cuatro campos llegando al
bundle con su valor.

**7 · El 400 entero.** `type`, `detail` y cada `errors[].field` con su mensaje,
en las dos convenciones, y su llegada a la tabla del Auditor.

**8 · El verificador exige un 200 real** y imprime el campo rechazado, no el
titular.

**9 · Los tres carriles siguen independientes.**

### Verificación

Cuerpo generado para DIA un sábado:

```json
{"sessionDateRange": {"startDate": "2026-09-18", "endDate": "2026-09-18"},
 "filter": {"ticker": "DIA"}}
```

Respuesta documentada, parseada: 2 niveles, `latestStockPrice` 516.20,
`price` 515.50 · `notional` 1.24e8 · `shares` 240310 · `prints` 412.

### Lo que esta validación NO cubre

**No hay un 200 real.** Este entorno no tiene salida hacia `quantdata.us` ni
credenciales. El endpoint sigue declarado **pendiente** hasta ejecutar:

```
python scripts/verify_live_quantdata.py --dark-pool --json dark_pool.json
```

Si con este cuerpo todavía devolviera 400, el siguiente paso no es otro payload:
es leer `errors[].field` y `errors[].message` en el Auditor.

---

## A12 · v1.48.0 · Campo continuo, un strike por barra y marca dorada

### Qué se certifica

`tests/test_v1470_adaptive_rendering.py` — 178 casos; ocho bloques de v1.47.0 más
un noveno para esta release.

**1 · El Interval Map es una superficie.** Que el campo lo prepare el componente
común, que se dibuje con interpolación activada, que tenga suelo de ruido y curva
—sin ellos la normalización por rango deja media pantalla a media opacidad y sale
un bloque macizo— y que los contornos se cierren en **los dos** ejes: barriendo
sólo uno salían segmentos sueltos que parecían ruido.

**2 · Los tres pasos del campo.** Relleno de huecos con peso inverso a la
distancia conservando el cero medido, suavizado gaussiano declarado en **celdas**
y normalización por rango con signo.

**3 · TRACE y la sección tratan el dato igual.** Que TRACE use el campo común y
que su tratamiento propio —`a^0.62` y el umbral de 0.02— haya desaparecido. Dos
tratamientos del mismo dato es lo que hace que dos pantallas del mismo programa
no se parezcan.

**4 · Una barra por strike.** Que exista el modo sin agrupar, que exista la cuenta
del alto que hace falta, que EXPOSICIÓN e INTERÉS ABIERTO la usen, y que el
contenedor con scroll esté en el marcado y en el CSS. Y la comprobación
ejecutada en el navegador: de 12 a 240 strikes, **tantas barras como strikes**,
ninguna por debajo de 10.5 px y todas con hueco.

**5 · El relieve es otra vista del mismo dato.** Que comparta la escala robusta
—si la comprimiera de otra forma, las dos vistas se contradirían al alternarlas—,
que sea proyección y no perspectiva, y que lea las mismas filas.

**6 · Las marcas son doradas.** Que el oro esté en TRACE y en FLUJO, que la
flecha diga el sentido y la cifra la cantidad, y que **ni el verde ni el rojo**
vuelvan a codificar el sentido de la marca en el gráfico de precio.

**7 · Dark Flow deriva su campo.** Con el nombre declarado, con un nombre
distinto —se deriva y se declara cuál— y sin ningún campo útil: `None` y nunca
cero, con los campos observados publicados. Y que 608 intervalos sin volumen
legible **no** se sumen como «0.0 acc» ni dejen la sección lista.

### Verificación visual

Banco de regresión, 46 paneles + 46 tras redimensionar: grosor 5.47–13.68 px,
ocupación máxima 0.760, ningún panel con rayitas, masa sólida ni barras sin
separación.

Campo del Interval Map, 90 strikes × 81 intervalos con un 22 % de huecos: la
superficie sale continua, mayormente vacía, con las dos zonas —resistencia arriba,
soporte abajo— separadas por el recorrido del precio y con sus contornos
trazados. Sin suelo de ruido el mismo dato salía como dos bloques macizos.

Perfil por strike, 21 y 166 strikes: una barra por strike en los dos, 11 px de
grosor, el panel creciendo a 304 px y 2.403 px respectivamente. El relieve del
mismo perfil enseña la forma con las etiquetas en el margen.

Terminal real, cero errores de consola: EXPOSICIÓN dibuja 21 barras —una por
strike— con el conmutador **Barras / Relieve 3D** operativo; TRACE dibuja el mapa
como superficie continua con sus muros y su migración de gamma.

### Lo que esta validación NO cubre

- No hay un 200 real de `dark-pool-levels`. Sigue **pendiente**.
- Sin credenciales, el Interval Map y el mapa de TRACE se verifican con el campo
  del motor y con datasets sintéticos de la forma real. Con la clave del usuario
  la misma ruta la alimenta el proveedor.
- El carril de FLUJO no se ha podido ver con cinta real en este entorno.

---

## A11 · v1.47.0 · Renderizado adaptativo y coherencia de datos

### Qué se certifica

`tests/test_v1470_adaptive_rendering.py` — 172 casos en ocho bloques.

**1 · La aritmética del plan.** 130 combinaciones de densidad (4 → 1.500
observaciones) × viewport (180 → 1.400 px), comprobando que ninguna produce una
barra por debajo de lo legible ni una ocupación mayor que 1 —que es solape— y que
siempre queda hueco entre barras. Y la prueba que habría atrapado el defecto
original: **más datos nunca adelgazan las barras**. Con el suelo aplicado al
grosor en vez de al paso, el grosor caía monótonamente con la densidad hasta el
píxel.

**2 · Un solo componente.** Que `layout`, `bin`, `extent`, `timeLayout`,
`timeBins` y `grid` vivan en un sitio, que los paneles y los carriles lo usen, y
que no quede aritmética de grosor propia en ninguno de los dos —`barThickness`,
`laneBarWidth`, `MIN_BAR_PX = 3`—. Y que el componente se cargue antes que sus
consumidores.

**3 · Agrupación sin perder el dato.** Que cada contenedor lleve rango, extremo,
suma, recuento y miembros; que **nunca** se calcule una media; que el perfil por
strike conserve el extremo y el histograma temporal sume; que un grupo cuyo pico
domina a la suma lo declare; y que la agrupación temporal mantenga cada barra en
su instante real, porque los carriles comparten eje con las velas de TRACE.

**4 · El mapa de calor.** Que dibuje celdas rellenas y no círculos, que no quede
ninguna llamada a `ctx.arc` ni la escala lineal `|v| / max`, y que la intensidad
sea por rango con agrupación en las dos dimensiones.

**5 · Primer fotograma y resize.** Que `Glide` adopte la primera aparición de
cada clave en vez de animar desde cero, que no quede ningún pico de eje con
semilla absoluta, y que el motivo de un panel vacío pueda cambiar entre ciclos
—el caché de paneles congelaba el texto del primer render—.

**6 · Coherencia de datos.** Que el interés abierto audite agregado y desglose
por separado y explique la ausencia en vez de contradecirse; que un agregado
ausente no sea un cero; que la deriva de volatilidad sin medir viaje como `None`;
que la prima sin cinta viaje como `None` con su motivo; que
`_resolve_visual_window` ya no lance `NameError` —ejecutando exactamente esa
ruta— y que no quede ninguna llamada a `_f` en `service.py`; y que la diagonal de
fiabilidad no se dibuje sin observaciones.

**7 · Multi-activo.** Que no haya un solo ticker escrito en el componente, en los
paneles ni en los carriles, y que el plan dependa del viewport y del recuento y
nunca de la magnitud del activo.

**8 · Regresión visual real.** `tools/visual_regression.py` sirve el arnés,
alimenta los **renderizadores de producción** y mide la geometría con el ancho
medido del lienzo.

### Verificación visual

46 paneles: cinco activos de 10⁹ a 10⁴ × cinco distribuciones (normal, cola
pesada, un dominante, muy disperso, valores diminutos), densidades de 12 a 780
observaciones y cinco viewports de 220×140 a 1.400×320.

```
46 paneles + 46 tras redimensionar · grosor 5.47–13.68 px · ocupación máxima 0.760
Ningún panel dibuja rayitas, masa sólida ni barras sin separación.
```

| | v1.46.0 | v1.47.0 |
|---|---|---|
| 45 strikes en 250 px | 2.5 px | **10.9 px** |
| 120 buckets en 330 px | 2.5 px | **8.8 px** |
| 390 buckets en 330 px | 2.5 px, solapadas | **8.1 px**, separadas |
| 780 buckets en 330 px | masa sólida | **8.1 px**, separadas |

Y el grosor no depende de la escala del activo: los cinco activos con 390
buckets en el mismo panel dan **8.13 px** los cinco.

Terminal real (`/` sobre uvicorn, sin credenciales, fin de semana): el trace deja
de llegar vacío —era el `NameError`—, y ya no aparecen ni el `0.00` de GEX/DEX
NETO, ni el `$0.0` de FLUJO 5M y PRIMA NEGOCIADA, ni el `+0.000 pp` de DERIVA DE
VOLATILIDAD, ni la diagonal de fiabilidad sin sesiones medidas.

Comprobado sección a sección con la terminal en marcha y cero errores de
consola: EXPOSICIÓN dibuja 21 strikes con barras gruesas y separadas; INTERÉS
ABIERTO muestra agregado **y** desglose coherentes (68.1K · 33.2K call · 34.9K
put) con su tabla de cambio poblada; ESTADÍSTICAS dice «14K contratos · volumen
oficial de la cadena · sin cinta» y deja la prima en «—» explicando por qué;
TRACE dibuja el mapa de intervalos como zonas continuas con sus muros y la
migración de gamma; ESCENARIOS traza Monte Carlo y el `exposure forecast` con
barras anchas.

### Lo que esta validación NO cubre

- No hay un 200 real de `dark-pool-levels`: sin salida hacia `quantdata.us`, el
  contrato del proveedor sigue sin poder leerse ni confirmarse.
- La validación LIVE con API key queda pendiente, incluida `--dark-pool` sobre la
  cesta multi-activo.
- La regresión visual usa datasets sintéticos de las formas que rompen. La
  comparación contra una sesión de mercado real queda pendiente en el VPS.

---

## A10 · v1.46.0 · Dark Pool por carriles y corrección global multi-activo

### Qué se certifica

`tests/test_v1460_dark_pool_lanes_and_multiasset.py` — 28 casos en diez bloques.

**1 · El cuerpo sin herencias.** Que `dark-pool-levels` envíe exactamente
`{"filter": {"ticker": …}}` y ninguno de los campos que el usuario señaló uno a
uno: `sessionDate`, `timeRange`, `snapshotTime`, `aggregationPeriod`,
`filterExpression`, `pagination`, `projection`. Y que la lista de bloqueo los
quite aunque alguien los añada más tarde: la defensa es el código, no la
disciplina de quien edita el catálogo.

**2 · El 400, entero.** Que `errors[].field` y `errors[].message` sobrevivan, en
las dos convenciones (`field`/`message` y `loc`/`msg`), sin truncar la parte
accionable.

**3 · Cinco códigos, cinco tratamientos.** 400 → `REQUEST_INVALID`, 422 →
`NO_DATA`, 404 → `MISSING_TOOL`, 5xx → `PROVIDER_ERROR`, timeout → `TRANSIENT`.
Que un 400 sin corrección conocida **pare en el primer intento** y deje escrito el
campo. Que un 422 **no** marque la herramienta como averiada ni entre en el
backoff. Que un 5xx sí programe reintento.

**4 · El 200 conserva los campos.** Nivel de precio, nocional, acciones, número de
operaciones (también cuando llega como `tradeCount`), volumen oscuro, volumen en
bolsa, porcentaje, el precio de referencia del subyacente a nivel de respuesta y
los campos sin nombrar bajo `extra`. Y la cadena entera —RAW → NORMALIZADOR → DATA
HUB → FRONTEND— con `source_mode = DIRECT_PROVIDER` al final.

**5 · Carriles independientes.** Que un `dark-pool-levels` rechazado **no** tumbe a
`dark-flow` ni a `equity-prints`: la sección sigue lista, el nocional y la
proporción siguen siendo los del proveedor, y la degradación se declara. Y que
cada carril sea de verdad su propio canal en el runtime del Data Hub.

**6 · Tri-estado fuera de bolsa.** Que un flag ausente siga siendo `None` y no
`False`, que el print no se descarte, y que unas impresiones que llegan sin poder
clasificarse **no** cuenten como carril sano.

**7 · Ocho estados, una pantalla.** Que los ocho existan con remedio declarado,
que la pantalla del analista nunca lea un código del proveedor, y que la tabla por
carril viva en la vista de AUDITOR y no en la de análisis.

**8 · Verificación multi-activo.** Que `verify_live_quantdata.py` mida los tres
carriles sobre una cesta de siete activos —tres ETF de escalas distintas y cuatro
equities líquidos—, que envíe **el mismo cuerpo que producción** y que conserve el
campo rechazado, no la primera línea del mensaje.

**9 · Multi-activo de verdad.** Que no haya un solo ticker escrito a mano en la
ruta que produce la sección; que la ventana de cadena sea proporcional para SPY,
QQQ, XLF y una acción de 9,52 $; que la escala del carril de órdenes no tenga
suelo absoluto en dólares; y que el heatmap llene lo mismo con cuatro escalas
separadas por seis órdenes de magnitud.

**10 · Ningún cero fabricado.** Que un fallo técnico deje `None` en nocional,
acciones, operaciones y proporción —nunca cero—, y que la causa exacta llegue al
Auditor mientras la pantalla dice SIN DATOS.

### Verificación visual

Programa en ejecución, renderizado en Chromium sobre `tools/visual_harness.html`
con los renderizadores reales y cinco activos de 10⁹ a 10⁴ (SPY, QQQ, DIA, XLF,
SOFI), quince paneles simultáneos:

- **Antes**: el carril denso de SOFI salía saturado —las 390 barras al tope, eje
  ±1.0— mientras los otros cuatro se leían bien. El defecto no estaba en el
  activo: estaba en que la escala inicial de cada panel era un dólar y sólo los
  activos grandes salían de ella a tiempo.
- **Después**, captura a 900 ms: los quince paneles muestran el perfil completo,
  con su eje correcto desde el primer fotograma. Los cinco activos se leen igual
  sin una sola constante por ticker.

Terminal real (`/` sobre uvicorn, sin credenciales de proveedor, fin de semana):
todas las secciones publican SIN DATOS / SIN ESTRUCTURA, que es el resultado
correcto, y **ya no aparecen** el `0.00` de GEX/DEX NETO ni el `$0.0` de FLUJO 5M
que la especificación prohíbe.

### Lo que esta validación NO cubre

- No hay un 200 real de `dark-pool-levels`: sin salida hacia `quantdata.us` desde
  el entorno de construcción, el contrato del proveedor no se ha podido leer ni
  confirmar.
- La validación LIVE con API key —incluida `--dark-pool` sobre DIA, SPY, QQQ,
  AAPL, NVDA, TSLA y AMD— queda pendiente de ejecutarse donde exista la clave.
- La comparación contra una sesión de mercado real queda pendiente en el VPS.

---

## A9 · v1.45.0 · Estado real del proveedor y legibilidad multi-activo

### Qué se certifica

`tests/test_v1450_provider_state_and_render.py` — 34 casos en siete bloques.

**1 · El 400 dice qué falta.** Tres convenciones de error de validación, y que el
titular no se duplique como si fuera un error de campo. Que un 400/422 sea
reparable y un 404 no. Que el cuerpo se repare **una vez** y se recuerde: el
segundo ciclo hace una sola petición. Que el aislador de canal propague la
excepción original, sin la cual el clasificador no puede distinguir los dos casos.

**2 · La etiqueta de autoridad.** Que los cinco canales cuya política es
`QUANTDATA` se declaren AUTORIDAD PRIMARIA, que la autoridad se **derive** y no se
escriba por segunda vez, y que los dos registros coincidan sobre
`dark_pool_prints`.

**3 · Procedencia ≠ carril.** Que GEX, DEX, VEX, CHEX, OI y Net Flow sigan siendo
`DIRECT_PROVIDER · QUANTDATA` **después** de que el motor los consuma, y que lo
propio siga siendo `DERIVED`.

**4 · Dark Pool.** Que un flag ausente sea `None` y no `False`; que el centro de
ejecución clasifique cuando el proveedor no declara; que el flag del proveedor gane
sobre la deducción; y que la sección diga **por qué** está vacía en los tres casos
distintos.

**5 · Legibilidad multi-activo.** Suelos que no inventan un cero, escala robusta
con marca de recorte, agrupación en vez de solape, y —la prueba que más importa—
que el heatmap llene **lo mismo** con una distribución de cola pesada y con una
repartida, mientras la lineal se diferencia en más de 25 puntos.

**6 · Mercado cerrado.** Que el Interval Map caiga a la última sesión válida
marcada, y que una matriz fresca del motor gane a una del proveedor de hace días.

**7 · El Auditor aparte.** Que los diagnósticos estén separados de las pestañas de
análisis **sin eliminarse**.

### Verificación visual

Programa en ejecución, renderizado en Chromium sobre `tools/visual_harness.html`,
que alimenta los renderers reales con SPY (1.4 B), QQQ (900 M), DIA (410 M),
XLF (6 M) y SOFI (22 K):

- Perfil por strike: los cinco muestran la forma completa del perfil, no una barra
  dominante y cuarenta invisibles.
- Carril de 120 buckets: barras individuales legibles en los cinco.
- Carril de 390 buckets: agrupado, con etiquetas de rango, separado — no el bloque
  sólido que producía el suelo de 3 px sin agrupación.
- Cero errores de consola.

> El universo se descubre desde Alpaca en runtime y este entorno sólo tiene
> credenciales para DIA. El banco es la única vía de validar la legibilidad
> multi-activo, que es justo lo que esta release corrige.

### Límites declarados

1. **La validación LIVE sigue PENDIENTE**, y con un punto nuevo: **la reparación
   del cuerpo de `dark-pool-levels` no se ha confirmado contra el proveedor**. El
   egress a su documentación está bloqueado en este entorno, así que las variantes
   son candidatas razonadas, no el contrato leído. Si ninguna encaja, el Auditor
   dirá qué campo pide — pero eso sólo se sabrá ejecutándolo.
2. **Que Dark Pool tenga datos** no se puede afirmar: se corrigió la clasificación
   que los perdía; si hay prints off-exchange sólo lo dice una sesión real.
3. **El artefacto oficial no se ha generado.** Requiere el toolchain pinado.

---

# Histórico · v1.42.7 y anteriores

## Hotfix runtime posterior a la primera certificación

Sobre el **mismo artefacto v1.42.6** se fijaron tres fallos observados en ejecución real,
sin cambiar la autoridad métrica ni las fórmulas de Gamma/Delta:

- `flow_pro_summary()` ya no puede derribar `/api/terminal/bundle` cuando un frame no
  trae `activity_score`: el fallback es una `Series` alineada al índice, nunca un escalar.
  La misma guarda se aplica al resumen/figura y al tape de actividad de Londres.
- El catálogo de `quantdata_intelligence` recupera el constructor canónico `_tf(ticker)`
  (`{"filter": {"ticker": ...}}`). GEX/DEX/VEX/CHEX por vencimiento y Order Flow ya no
  fallan por `NameError: _tf is not defined`.
- En premarket, el precio subyacente del snapshot elige la **observación válida más
  reciente** entre último trade y midpoint NBBO. Así una operación antigua no marca
  falsamente como vieja una cotización que sí está actualizada. El límite de frescura
  de 20 s **no se amplió ni se maquilló**.

Se conserva la semántica de ausencia real de datos: que Dark Pool no tenga prints en
un momento dado no fabrica datos ni fuerza un estado LIVE.

### HOTFIX2 · bloqueo intermitente de TRACE observado en ejecución real

- PREMARKET ejecuta el ciclo estructural pesado cada 45 s, mientras el contrato de
  frescura del subyacente permanece en 20 s. El gate ya no usa el timestamp envejecido
  del último snapshot pesado para decidir si el precio está vivo: toma el evento de
  precio válido más reciente ya recibido por `PROVIDER_BUS`/`PRICE_TICK_FABRIC` y
  conserva por separado el timestamp estructural para auditoría. No se amplió el límite.
- Se reproduce y corrige `RuntimeWarning: Mean of empty slice` de TRACE: una cinta
  completamente plana generaba eficiencia cero en todas las velas y luego llamaba a
  `median()` después de convertir esos ceros a NaN. Ahora la mediana sólo se calcula
  cuando existe muestra finita; la ausencia se declara explícitamente.
- Las superficies y cortes que aplican el gate consultan la misma evaluación viva; ya
  no existe una copia estática del gate capaz de apagar perfiles/heatmap/niveles entre
  dos refresh estructurales.

Doce pruebas nuevas fijan las regresiones acumuladas de v1.42.6/HOTFIX1/HOTFIX2. Inventario actual: **1509 casos / 133
ficheros**.

### H3 · cierre visual, semántico y de resiliencia

- TRACE conserva el mismo OHLC y escala estructural, pero aplica un mínimo exclusivamente
  raster al cuerpo/mecha para que una variación de centavos no desaparezca entre strikes.
- Flujo de Órdenes separa unidades: la banda central es prima direccional `BUY-SELL`;
  el volumen firmado del subyacente queda auditado aparte. Los prints grandes se agrupan
  por minuto y se anclan a la curva del subyacente, evitando nubes doradas desordenadas.
- Max Pain / Tiempo tiene autoridad nativa ITM: Session Memory y, al actualizar una sesión
  antigua, Chain History. Una sola observación se dibuja como punto; con historia forma línea.
- Call Wall y Put Wall significan únicamente walls por Gamma de cada lado. Los máximos de OI
  se muestran como `Mayor Call OI` y `Mayor Put OI`, sin reutilizar el nombre Wall.
- FUENTES distingue núcleo/autoritativo de contraste Quant Data. Un `QD 0/N` ya no se presenta
  como si la exposición u OI del motor valieran cero. ARQUITECTURA etiqueta los fallbacks como
  política contractual, no como estado LIVE.
- Los timeouts de Quant Data son transitorios: backoff exponencial, reserva del motor intacta
  y reintento visible; no se inventa una ruta inválida ni se consume cuota en bucle.
- Un CSV histórico malformado se copia íntegro a cuarentena y el activo se reescribe de forma
  atómica con las filas válidas, por lo que el mismo aviso no reaparece en cada arranque.
- El selector de aceleración `AUTO` ya no elige JAX cuando sólo existe CPU. En ese caso usa
  NumPy para conservar igualdad bit-a-bit con replay/auditoría; JAX CPU sigue disponible si se
  solicita explícitamente. No cambia ninguna fórmula de Greeks.

Once regresiones nuevas fijan estas rutas. Inventario: **1531 casos / 135 ficheros**.

## Certificación en cuatro niveles

`tests/test_v1420_universal_quantitative_truth.py` certifica cada pieza nueva en los
cuatro niveles que exige el contrato de esta versión, marcados en el nombre del test:

| Nivel | Qué demuestra | Ejemplo |
|---|---|---|
| `unit` | la fórmula está implementada como dice | parsing OSI y resolución de multiplicador |
| `golden` | coincide con una referencia independiente | vega contra diferencia finita del precio |
| `property` | cumple los invariantes que DEBE cumplir | americana ≥ europea; notional lineal |
| `economic` | distingue lo que dice distinguir | el challenger verdadero gana, el plano nunca |

## **Una advertencia importante: este release SÍ cambia números**

A diferencia de las anteriores, v1.42.6 **modifica cifras publicadas**. No es una
refactorización neutra y no debe presentarse como tal:

1. **Greeks de futuros e índices en TRACE, Dealer, Market State y NextGen.** Antes
   usaban Black-Scholes sobre el precio del futuro; ahora usan Black-76. En YM la
   delta se desplaza un 2,6 % y la **vanna cambia de signo**. La cifra anterior era
   la equivocada, pero si tiene capturas o notas previas, no coincidirán.
2. **Cifras monetarias de futuros y contratos ajustados.** Antes multiplicaban por
   100; ahora por el multiplicador real (YM = 5, MYM = 0,5, ajustados = su
   entregable). Para DIA, SPY, QQQ y cualquier equity/ETF estándar **no cambia nada**.
3. **Comparaciones entre proveedores.** Donde antes aparecía una media ponderada de
   GEX o IV, ahora aparece la cifra de la autoridad declarada y, si procede, un
   `NOT_COMPARABLE` explicando por qué no se pueden enfrentar.
4. **Pendiente de calibración.** El valor cambia de escala al pasar de OLS a Cox. Una
   pendiente de 1,07 en v1.42 es mejor calibración que un 0,21 de v1.41; no son
   comparables entre versiones.

Para un ETF como DIA operado normalmente, el panel se verá **igual** salvo por las
secciones nuevas. Las diferencias se concentran en futuros, índices y contratos
ajustados, que es donde estaba el defecto.

## Análisis estático de la interfaz

`bash scripts/lint_frontend.sh` → obligatorio antes de publicar: `node --check` no
detecta una variable no declarada, y eso es exactamente lo que deja un panel en
RENDER NO DISPONIBLE.

## Configuración obligatoria antes de subir al VPS

Declarar el plan real de Quant Data en `.env`. Sin esto la cadencia asume la ventana
**diaria**, que es correcta pero lenta si su plan es más amplio:

```
QUANTDATA_PLAN_REQUESTS=240
QUANTDATA_PLAN_WINDOW_SECONDS=86400
```

Roster de proveedores activo (por defecto Alpaca + Quant Data):

```
ITM_OPTIONS_PEERS=ALPACA,QUANTDATA
```

## Comprobación de contratos en caliente

Tras arrancar, `GET /api/architecture` debe responder con:

- `scope: MULTI_ASSET`
- `pricing_dispatch.YM.option_model = FUTURE_OPTION`
- `pricing_dispatch.DJX.option_model = INDEX_OPTION`
- `pricing_dispatch.VIX.option_model = UNSUPPORTED` (es correcto: las VIX estándar son
  cash-settled sobre VRO y requieren curva forward)
- `metric_authority.by_provider.QUANTDATA.authority` incluye `net_drift`

Si `dispatcher_usage` queda vacío tras varios ciclos, ninguna sección está pasando por
la puerta única y hay que investigarlo antes de operar.

## Historial de opciones para backtesting

Desde esta versión, cada ciclo LIVE archiva el snapshot OPRA observado en
`<persistent>/replay/option_snapshots/<SÍMBOLO>/<fecha>.jsonl`. **Ese histórico no se
puede reconstruir a posteriori**: el backtest realista depende de haber grabado bid y
ask en su momento. Cuanto antes esté el VPS en marcha, antes habrá histórico usable.

## Autonomía de red

La terminal principal no carga recursos externos en su ruta crítica. Las series macro
(FRED) y los proveedores de mercado sí requieren salida a internet, como es lógico.

## Lo que esta versión no promete

Microestructura de market maker real. Sin libro a nivel de orden no se pueden estudiar
cola, llegadas, cancelaciones, reposición, probabilidad de ejecución ni selección
adversa. Alpaca y Quant Data no entregan eso, y simularlo y llamarlo L3 sería mentir.


## v1.42.6 · Comprobación en caliente de la verdad de sesión

Tras arrancar, y **a cualquier hora**, la pestaña TRACE debe mostrar velas. Si está
vacía fuera de sesión, el bootstrap histórico está fallando de verdad y el
diagnóstico lo dirá con esas palabras en lugar de `sip:NO_BARS`.

En el panel FUENTES, comprobar:

- la cabecera dice `N/N operativos` y **ninguna** fila contradice ese número;
- de madrugada los proveedores aparecen como `MARKET_CLOSED`, no como caídos;
- las calidades sin medir dicen `N/A`, no `0.0`;
- ninguna herramienta muestra más de una ruta probada.

Si una herramienta aparece como `RUTA_INVALIDA`, el mensaje nombra la ruta canónica:
o su plan de Quant Data no la incluye, o el proveedor la renombró y hay que
actualizar el registro. No hay una tercera posibilidad y no se prueban alternativas.

## Voz de Sophia

`GET /api/sophia/capabilities` declara qué hay instalado. En una máquina sin
faster-whisper ni whisper.cpp, `stt` es `false`, el micrófono aparece deshabilitado y
**no se envía ninguna petición de transcripción**. Para habilitar el dictado:

```
pip install faster-whisper
```

o declarar la ruta del binario en `ITM_WHISPER_CLI`. La síntesis de voz usa SAPI en
Windows (viene con el sistema) o Piper vía `ITM_PIPER_BIN`.


## H4 · continuidad LIVE de equities

- Suite final sobre el árbol H4: **1549 PASS · 0 SKIP · 0 FAIL · 0 ERROR · 21/21 particiones**.
- Inventario exacto: **1633 casos / 141 ficheros**.
- Particiones: **21**.
- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.
- OPRA: el reloj de frescura usa el evento más nuevo entre trade y NBBO quote por contrato.
- Un snapshot estructural fuera de SLA ya no borra TRACE/OI/EXPOSICIÓN: queda visible como **contexto retenido no accionable** mientras precio y flujo observados siguen LIVE.
- El carril neto/total de Flujo escala al pico visible real para que una ráfaga no se recorte fuera del canvas.
- En Windows se conserva `fsync(file) → os.replace()` y se omite `fsync(directory)`, que NTFS no expone mediante `os.open` de directorios.
- Call Wall sólo se presenta por encima del spot LIVE y Put Wall sólo por debajo; si el precio cruza un muro entre snapshots, se oculta hasta el recálculo, sin inventar otro nivel.
- Quant Data páginas queda limitado a dos requests simultáneos y nunca gobierna el núcleo de equities.
- Alcance operativo actual solicitado: **equities/ETF**. La ausencia de proveedor de futuros no degrada DIA ni sus opciones.

- Dark Pool deja de rebautizar cualquier large print como dark pool: sólo muestra off-exchange confirmado por venue/tape (o dato externo que lo declare explícitamente).


## A1 · Auditoría cuantitativa independiente (v1.42.6)

Revisión de las fórmulas del motor contra referencias **externas al propio código**
—formas cerradas publicadas, identidades exactas y diferencias finitas—, nunca
contra el motor consigo mismo. Un motor internamente coherente puede estar
coherentemente equivocado, y tres de los siete hallazgos lo estaban con la suite
en verde.

El detalle completo, con las cifras medidas, está en `CHANGELOG_v1.42.6.md`.

### Verificado correcto · sin cambios

| Área | Referencia externa | Resultado |
|---|---|---|
| Greeks BS y Black-76 (δ, Γ, vanna, charm, speed) | diferencias finitas + forma cerrada independiente | error rel. **0.0** |
| Paridad put-call BS y B76 | identidad exacta | **1e-14** |
| GEX `sign·Γ·OI·mult·S²·0.01` | los 9 módulos que la calculan | idéntica en todos |
| DEX `Δ·OI·mult·S` | idem | idéntica en todos |
| Isotónica PAVA (calibración) | óptimo por fuerza bruta SLSQP | **2.8e-14** |
| Platt scaling | MLE independiente Nelder-Mead | coincide |
| SVI, SSVI, Durrleman g, Gatheral-Jacquier | formulación publicada | correctas |
| Reloj ACT/365 vs reloj de sesión | separación explícita | correcta |
| Gamma flip `NetGEX(S)=0` | raíz verificada numéricamente | es raíz real |
| Max pain · EV en R · breakeven `(1+c)/(RR+1)` | derivación algebraica | correctas |

### Corregido

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F1 | **CRÍTICA** | Monte Carlo: `prob_touch_by_expiry_pct` contaba cruces sobre malla discreta. Con 1 paso/día un 0DTE tenía UN tramo, así que «tocar» degeneraba en «terminar más allá». | **17.30 % → 34.18 %** (exacto 34.30 %) |
| F7 | **CRÍTICA** | `Cargo.lock` fijaba 1.42.0 con `Cargo.toml` en 1.42.1, en los dos crates. `cargo check --locked` —el comando del gate y del CI— fallaba con exit 101. El artefacto no podía desplegarse por su propio CI. | exit 101 → **PASS** |
| F5 | **ALTA** | `if f(lo)==0: return lo` publicaba **IV = 0.5 %** cuando el precio era indistinguible del de vol cero. Como Γ ∝ 1/σ, esa IV inventada inflaba la gamma y contaminaba GEX → Call/Put Wall → Flip. | número inventado → **NaN declarado** |
| F6 | **ALTA** | El adelanto de DXLink en `/api/asset/select` guardaba la Task sólo en una variable local. El bucle de eventos sólo mantiene referencias **débiles**: la tarea podía morir a media ejecución al retornar el handler, dejando el cambio de activo sin failover preparado y sin ningún error visible. | referencia fuerte + `add_done_callback` |
| F3 | MEDIA | IV no recuperada en contratos muy ITM: se perdía el contrato entero de la cadena. | paridad put-call → **100 % de los invertibles** |
| F2 | RENDIMIENTO | El barrido del gamma flip evaluaba 4 mallas de las que sólo 2 eran distintas. | **14.20 ms → 8.12 ms (−43 %)** por snapshot |
| F4 | HIGIENE | 77 importaciones y 6 variables muertas en `app/` y `scripts/`. | eliminadas |

F1 y F5 se refuerzan entre sí: el Monte Carlo respondía «¿llega al Call Wall?» con
la mitad de la probabilidad real, sobre un Call Wall que podía estar colocado con
gammas infladas por IVs inventadas. Ninguno era visible desde dentro del motor,
porque ambos producían números perfectamente plausibles.

### Estado final

    FULL RELEASE GATE PASS · v1.42.6 · PRE-VPS
    SUITE PARTICIONADA: 1551 passed · 0 skipped · 0 failed · 0 errors · 21/21 particiones

- Regresiones fijadas en `tests/test_v1422_quant_audit_corrections.py` (20 casos).
- `cargo check --locked` PASS en `causality_engine` y `wasm_bridge`.
- JavaScript: 25/25 ficheros validados.
- Locks instalados con `--require-hashes` y `pip check` limpio.
- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A2 · Auditoría por secciones (v1.42.6)

Ejecución real, no lectura: cadena sintética con respuestas sembradas, 62 endpoints
GET golpeados y el diagnóstico propio del programa como juez.

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F8 | **ALTA** | Call/Put Wall ponderados por gamma al spot: el argmax se iba al ATM por construcción. A 1 DTE hacían falta 40× el OI ATM para ver el Call Wall y el Put Wall no aparecía nunca; ambos salían a ~0.2σ del precio. | gamma en S=K → **12/12 escenarios detectados** |
| F9 | **ALTA** | `frame.get("col",0).fillna()` devuelve un escalar si falta la columna: la guarda defensiva era la que lanzaba `AttributeError`. Tumbaba TRACE (ticks sin `signed_volume`) y DARK POOL (tape sin `notional`/`conditions`). | **112 sitios** + test de patrón |

Verificación funcional:

- 62 endpoints GET: **0 fallos 5xx, 0 excepciones** (3 códigos 4xx son validación correcta).
- Diagnóstico por panel del propio programa: **9/9 CON DATOS**.
- `key_levels_report` == `structure_levels` en Call Wall y Put Wall.
- Parkinson, volatilidad forward, detección off-exchange y heatmap: verificados correctos, sin cambios.

Pendiente declarado: Vanna/Charm/Speed conviven con unidades distintas por sección
(`proxy` sin escalar frente a `$M`). Etiquetados y no incorrectos, pero un mismo
nombre con dos escalas invita a comparar lo que no es comparable. `units_registry`
ya tiene los conceptos y ninguna de las dos rutas lo usa.

- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A3 · Defectos reportados en producción (v1.42.6)

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F10 | **ALTA** | El heatmap publicaba sólo los últimos 120 snapshots: con cadencia de ~45 s, el 30 % de la sesión. | **30 % → 99.2 %** de cobertura, mismo payload |
| F11 | **ALTA** | El lienzo estiraba la imagen del heatmap al rectángulo completo, sin usar las coordenadas que representa: desalineada de velas y niveles. | colocada por `xMapTime`/`yMap` |
| F12 | **ALTA** | Off-exchange sólo se confirmaba por el NOMBRE del venue, que viene de un catálogo remoto. Si falla: 0 prints en todos los activos, para siempre, sin error. | código SIP `D` (FINRA ADF) como prueba primaria |
| F13 | MEDIA | «Hedge Wall (ITM)» se dibujaba sobre «Call Wall»: dos etiquetas apiladas como evidencias independientes. | coincidencia declarada, nivel no duplicado |
| F14 | MEDIA | Vanna/Charm/Speed con escalas distintas por sección (~600× en SPY). | unidades canónicas de `units_registry`, ratio medido **0.971** |
| F15 | MEDIA | `api` declarada dos veces a nivel global con firmas distintas; cuál ganaba dependía del orden de los `<script>`. Con el orden invertido, todos los POST se habrían vuelto GET silenciosos. | renombrada a `itGetJson` |
| F16 | BAJA | El poller de Anomalías quedaba vivo toda la sesión sin forma de pararlo. | arranca y para con el panel |

Call Wall / Put Wall verificados sobre cadena DIA realista con muros sembrados en
520/510 y spot 515.20: aciertan en 0-1 DTE, semana y mes, del lado correcto del
precio, y `key_levels_report` coincide con `structure_levels`.

Frontend: **0 errores de eslint** en los 25 scripts (antes 18).

- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A4 · Interfaz viva (v1.42.6)

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F17 | **ALTA** | v1.42.4 corrigió `nextgen_terminal.js`, pero la raíz sirve `terminal.html` con otros scripts: el arreglo no tocaba la pantalla en uso. | contrato de renderizador fijado por test |
| F18 | **ALTA** | El TRACE aceptaba sin condición la ventana temporal compartida con Flujo de Órdenes; si ésta era más ancha, precio y heatmap se comprimían y sobraba lienzo. | en vivo el TRACE impone su propio eje |
| F19 | MEDIA | Faltaba el carril de volumen del subyacente; los buckets ya lo acumulaban sin dibujarlo. | quinto carril, rejilla de 5 filas |
| F20 | MEDIA | Dark Pool publicaba el notional off-exchange sin denominador. | **% fuera de bolsa** + mayor print + tabla |
| F21 | BAJA | En la interfaz secundaria los paneles sumaban 957 px dentro de un contenedor de 900 px con `overflow:hidden`. | alturas por peso sobre el alto real |

- Frontend: **0 errores de eslint** en los 25 scripts.
- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A5 · Capa QFLOW y multi-activo (v1.42.6)

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F22 | **ALTA** | `_target()` mandaba TODA acción a `direct=False` pese a consultar su propio ticker; su evidencia llegaba degradada a canal de contexto y las secciones quedaban vacías fuera de DIA. | regla universal: directa si el ticker es el del instrumento |
| F23 | **ALTA** | El normalizador leía `netCallPremium`/`netPutPremium` y el proveedor publica `callSum`/`putSum`; `stockPrice` se descartaba entero. | campos reales capturados |
| F24 | MEDIA | Sin campo neto explícito se publicaba `0.0`, convirtiendo una respuesta válida en serie plana. | neto derivado `call − put` |
| F25 | NUEVO | No existía capa QFLOW: ni serie acumulada, ni nivel de precio, ni eventos. | `app/core/qflow.py` + render |

Universalidad verificada: DIA · SPY · QQQ · IWM · AAPL · NVDA · TSLA · MSFT · GLD
consultan su propio ticker con evidencia directa. YM · MYM · DJX · VIX · VXD siguen
declarados como contexto del ecosistema Dow, que es la única razón legítima para
consultar otro ticker.

Un fallo de datos nunca se publica como `$0.0`: seis estados con su motivo.

- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A6 · Net Drift oficial, Dark Pool directo y Quant Data como proveedor (v1.42.7)

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F26 | **CRÍTICA** | `_rows` sólo recorría listas y Quant Data devuelve un mapa `{época_ms: fila}`. Devolvía `[]` ante **toda respuesta válida**: `net-drift`, `net-flow` (y con él QFLOW, que nunca llegó a activarse), `exposure-by-strike/-expiration`, `max-pain-over-time`, `oi-over-time`, `volatility-drift` y `stock-price-over-time`. | `_keyed_rows` + `_ts_iso`; la vía de lista sigue siendo la primera |
| F27 | **ALTA** | La época en milisegundos viajaba cruda como `t`; `datetime.fromisoformat("1758205800000")` descartaba la fila **en silencio** aguas abajo. | conversión única a ISO-8601 UTC |
| F28 | **ALTA** | `net_drift` usaba el normalizador compartido, que colapsa la fila a `value`/`call`/`put` y tira los dos volúmenes netos y las dos primas a precio medio. | `norm_net_drift` con los siete campos oficiales |
| F29 | **ALTA** | No existía curva acumulada de Net Drift ni distinción del bucket abierto. | `app/core/net_drift.py` con reconstrucción íntegra por ciclo |
| F30 | **ALTA** | Dark Pool dependía **exclusivamente** de inferir el venue desde la cinta de otro proveedor; si ese campo faltaba, mostraba 0 en todos los activos sin poder decir por qué. | `dark-flow` (nuevo) + `dark-pool-levels` + `equity-prints` como fuente directa; venue como auditoría |
| F31 | MEDIA | La página Exposure no alimentaba EXPOSICIÓN: sus normalizadores esperaban listas y la respuesta es `data[TICKER].exposureMap[vto][strike]`. | `norm_exposure_by_strike` / `norm_exposure_by_expiration` |
| F32 | MEDIA | La columna ACCIONES de las zonas mostraba 0 en todas las filas porque ninguna fuente publicaba `shares`. | la publica `dark-pool-levels`; sin dato va vacío, nunca 0 |
| F33 | BAJA | La rejilla de KPIs de Dark Pool dejaba una fila a medias y `.grid.c5` no tenía puntos de ruptura. | ocho tarjetas en rejilla de cuatro + degradación responsive |
| F34 | **ALTA** | Doce de las 32 herramientas del catálogo se descargaban cada ciclo y **ningún consumidor las leía**: quemaban cuota y las secciones quedaban vacías con el dato ya en memoria. | las doce conectadas a su sección del mapa, con el motor propio mandando y el proveedor como respaldo declarado |
| F35 | MEDIA | `norm_prints` es genérico y dejaba las impresiones de opciones sin strike, vencimiento, tipo ni prima: filas anónimas imposibles de relacionar con un punto de la curva. | `norm_option_order_flow` con el contrato entero y el tipo de ejecución |
| F36 | MEDIA | El bloque `order_flow` sólo viajaba en el camino feliz de Net Drift; que falte la curva no implica que falte la cinta. | se publica en todos los caminos |
| F37 | **CRÍTICA** | La tarjeta de RESUMEN titulada **NET DRIFT** no dibujaba Net Drift: acumulaba la cinta propia de opciones y la presentaba con el nombre de una magnitud que sólo publica el proveedor. En pantalla era indistinguible de la real. | pasa a leer el bloque oficial `d.net_drift`; sin dato, SIN DATOS |
| F38 | **ALTA** | En esa misma tarjeta, `premium * (direction \|\| 1)` contaba como COMPRA toda la prima sin agresor clasificado, porque `0 \|\| 1` vale 1: la curva se inclinaba sola a comprador justo cuando la cinta llegaba sin cotización. | eliminado con la reconstrucción |

### Reparto visual de todas las secciones

La comprobación de rejillas ya no fija un número de tarjetas para una sección: recorre
las trece vistas de la terminal y exige que **toda** rejilla de KPIs tenga un número de
tarjetas múltiplo de sus columnas, y que toda anchura declarada (`c2`…`c6`) degrade en
pantalla estrecha. Resultado actual: **0 rejillas desequilibradas**.

### Certificación del acumulado contra la respuesta cruda

`certify_against_raw()` recalcula la suma desde las filas del proveedor, sin pasar
por la curva, y la contrasta punto a punto. Sobre 90 buckets:

    puntos comparados        90
    desvío relativo peor     5.3e-08
    desvío absoluto peor     5.0e-05   (= cuanto de redondeo de la publicación)

### Separación declarada

`net_drift` publica `independent_of = [GEX, DEX, NET_FLOW, QFLOW, GAMMA_EXPOSURE,
DELTA_EXPOSURE]` y `reconstructed = false`. El módulo no importa `qflow`, `engine`,
`trace_analytics` ni `precision_engine`; un test lo comprueba sobre el código fuente.

### Universalidad

Misma respuesta → misma curva punto a punto en DIA · SPY · QQQ · IWM · AAPL. Ningún
símbolo aparece como literal en `app/core/net_drift.py`.

### Límites de esta verificación

- **Sin navegador**: no hay comprobación visual directa; se verifica la estructura
  del HTML, el CSS y el código del renderizador, no el píxel pintado.
- **Sin credenciales de Quant Data**: la forma de las respuestas se reproduce a
  partir de la que el propio código de producción ya documentaba. **La comparación
  final contra una sesión real queda pendiente de ejecutarse en el VPS**, y para eso
  está `certify_against_raw()`.

- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.

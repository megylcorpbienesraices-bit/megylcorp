# ITM QUANT MULTI ASSET · v1.47.0 — Densidad no se resuelve adelgazando

Release: `ITM_QUANT_v1.47.0_PRE_VPS` · Base: `v1.46.0` · Alcance: `MULTI_ASSET`

Esta release **no rehace nada**. QFLOW, Gamma Migration, los Walls, el Data Hub,
la autoridad de Quant Data, el tri-estado de dark pool y los carriles
independientes siguen exactamente como quedaron.

## Matemática del motor

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Ninguna
métrica cambia de valor. Lo que cambia es **representación, agrupación visual y
escalado**. Ningún dato se altera para que el gráfico parezca mejor.

---

## El error de fondo

Se estaba intentando resolver **densidad de datos con grosor fijo**. Son dos
situaciones distintas y no admiten la misma respuesta:

```
 20 barras en 300 px   →  caben gruesas, con holgura
390 barras en 300 px   →  a 4 px serían 1.560 px de contenido en 300
```

No existe un grosor correcto para las dos. Cada intento —`2`, `3`, `5`— fallaba
en cuanto cambiaba la cantidad de strikes, de buckets o el tamaño del activo.

Y el suelo que sí había estaba en el sitio equivocado:

```js
fitBars(data, w)        // agrupaba hasta dejar barras de MIN_BAR_PX de PASO
barThickness(slot)      // …y luego cogía el 82 % de ese paso
```

3 px de paso × 0.82 = **2.46 px de barra**: por debajo del mínimo que el código
creía estar garantizando. El suelo nunca se alcanzaba.

**La regla correcta:**

```
pocas barras   →  hacerlas gruesas directamente
muchas barras  →  AGRUPAR visualmente  →  y entonces hacerlas gruesas
```

nunca `muchas barras → cada vez más finas hasta que desaparecen`.

---

## 1 · `AdaptiveBarProfile`: una sola regla de barras

`app/static/itmq_adaptive_bars.js`. Lo usan EXPOSICIÓN, FLUJO DE ÓRDENES,
INTERÉS ABIERTO, ESTADÍSTICAS, el mapa de calor y cualquier histograma futuro.
Una corrección aquí beneficia a todos a la vez; antes había tres aritméticas
distintas y cada arreglo se hacía en dos de las tres.

Todo sale del **espacio de pantalla** y de la **densidad**:

```
pasoNecesario = grosorObjetivo / FILL
binsMáximos   = floor(extentPx / pasoNecesario)
grosor        = clamp(paso * FILL, MIN_BAR_PX, MAX_BAR_PX)
```

`FILL = 0.76` deja el 24 % restante como hueco: sin separación, cuarenta barras
son un bloque sólido. `MIN_BAR_PX = 5` es donde una barra deja de leerse como
barra. `TARGET_BAR_PX = 8` es la presencia que se busca.

Y la agrupación se elige **por cercanía al objetivo**, no «el primer grupo que
lo alcanza». `ceil(n/g)` da saltos: con 30 observaciones en 300 px, no agrupar
da 7.6 px —perfectamente legible— y agrupar de dos en dos salta a 15.2 px,
tirando la mitad de la resolución para ganar un grosor que no hacía falta.

Medido sobre los renderizadores reales, 46 combinaciones:

| | antes | ahora |
|---|---|---|
| 45 strikes en 250 px | 2.5 px | **10.9 px** |
| 120 buckets en 330 px | 2.5 px | **8.8 px** |
| 390 buckets en 330 px | 2.5 px, solapadas | **8.1 px**, separadas |
| 780 buckets en 330 px | masa sólida | **8.1 px**, separadas |

Ocupación máxima 0.76 en los 46 paneles: nunca se pisan.

## 2 · La agrupación es visual; el dato sigue entero

El dataset no se toca. Cada contenedor conserva **rango, magnitud, signo,
extremo, cuántos agrupa y los índices originales**, y el `hover` los enseña: si
en pantalla pone `514…516`, ahí siguen 514, 515 y 516 por separado.

Nunca una media. Una media cancela: un +8 y un −8 vecinos dan 0 y la
concentración desaparece justo donde importa. Dos modos, según lo que signifique
el eje:

- **`extreme`** — perfiles por strike. El contenedor muestra el valor más grande
  en magnitud, que es un valor que **existió de verdad**.
- **`sum`** — histogramas temporales. «Lo que pasó en estos cinco minutos» es la
  suma, no el máximo. Se conserva además el pico del grupo y **se marca** cuando
  domina a la suma, para que una anomalía no quede escondida dentro de su
  intervalo.

## 3 · Los carriles de FLUJO agrupan el INTERVALO, no la posición

AGRESOR, VOLUMEN, TOTAL, flujo direccional y volumen neto CALL/PUT compartían el
defecto con otras constantes. Ahora, cuando un minuto no da para una barra con
presencia, se agrupa en **2 m, 3 m, 5 m…** según el ancho REAL del panel:

```
 60 min en 900 px  →  1 min  · 11.4 px
390 min en 900 px  →  5 min  ·  8.8 px
390 min en 330 px  → 13 min  ·  8.4 px
```

Se agrupa el intervalo y no el índice **a propósito**: los carriles comparten eje
de tiempo con las velas de TRACE, y colocar las barras por posición las habría
desalineado del precio en cuanto faltara un minuto. Cada contenedor ocupa su
tramo real del eje.

## 4 · El mapa de calor: zonas, no puntos

Cada celda era un **círculo** de radio `min(cw,ch)*0.46`. Con 90 strikes en
250 px eso da 1.3 px. Y la intensidad era `|v| / max` —lineal— que con una cadena
concentrada deja casi todo por debajo del umbral y sin pintar. El resultado eran
puntos diminutos y dispersos teniendo 7.290 observaciones.

Ahora la celda se agrupa en **las dos dimensiones** hasta tener tamaño real, se
pinta **rellena** —así una concentración se lee como una zona continua— y la
intensidad es por **rango dentro de lo visible**, que es lo que hace que el mapa
se lea igual en un ETF enorme y en una acción pequeña.

## 5 · El primer fotograma ya es correcto

`Glide` empezaba toda clave en 0 y subía animándose. El primer fotograma de un
panel dibujaba **todas las barras a cero** con el eje ya en su escala correcta:
un panel con eje de ±4.2 B y ni una sola barra. Se arreglaba solo a los pocos
fotogramas… si el bucle de animación seguía vivo. Un panel que entra en pantalla
por el observador de visibilidad dibuja **un** fotograma, y ése era el que se
quedaba.

Ahora la primera aparición de cada clave se adopta de golpe y sólo se animan los
cambios posteriores: la transición se conserva donde tiene sentido —un valor que
se mueve— y desaparece donde no lo tenía.

## 6 · Regresión visual automática

`tools/visual_regression.py`. Las pruebas numéricas no detectaron nada de esto:
el dato estaba, la escala era correcta, el suelo «existía» y la suite estaba en
verde. Ninguna miraba la **geometría que se acaba dibujando**.

Ahora se mide, sobre los renderizadores reales: cinco activos de 10⁹ a 10⁴, cinco
distribuciones (normal, cola pesada, un dominante, muy disperso, valores
diminutos), densidades de 12 a 780 y cinco viewports. Falla si algún panel dibuja
barras por debajo del mínimo, barras solapadas o barras sin separación.

Y una segunda pasada **después de redimensionar la ventana**: una geometría
correcta sólo al primer render no sirve de nada en una terminal que se
redimensiona. Los 46 paneles se vuelven a medir con el lienzo cambiado.

La comprobación **no lleva `skipif`**, a propósito. Esta release se sostiene
sobre una afirmación visual, y un control que se salta solo dejaría la
certificación en verde justo en el entorno donde no se comprobó nada.

---

## Coherencia de datos

**`NameError: name '_f' is not defined`** — `_resolve_visual_window` se escribió
copiando dos líneas de `terminal_api`, donde el conversor numérico se llama `_f`;
en `service.py` se llama `_finite`. **Cada** construcción del trace lanzaba el
error, se tragaba en el `except` de `/api/terminal/bundle` y la terminal servía
un trace vacío: velas, perfiles y niveles en blanco. Un `except Exception` que
convierte un fallo de programación en un panel vacío es el peor sitio donde puede
esconderse un error, porque **parece falta de datos**. Corregido en su origen,
con una prueba que ejecuta exactamente esa ruta.

**INTERÉS ABIERTO** — la cabecera leía `state.positioning` y el desglose leía otra
fuente. Que existiera el agregado no decía nada sobre el desglose, así que la
pantalla mostraba `OI TOTAL 37.1K` encima de `SIN INTERÉS ABIERTO`. Ahora cada
hecho declara su procedencia por separado, un agregado ausente ya no se convierte
en cero, y el panel **dice por qué** falta el desglose en vez de contradecir a la
cabecera.

**DERIVA DE VOLATILIDAD** — `iv_change = 0.0` cuando sólo hay una observación.
Sin dos observaciones no hay cambio que medir, y eso no es un cambio de cero: se
mostraba `+0.000 pp` junto a un panel que decía `NO DISPONIBLE`.

**ESTADÍSTICAS** — la prima se sumaba sobre impresiones mientras los contratos
podían venir de la cadena oficial. Sin cinta, `sum(...)` daba `0.0` y salía
`Contratos 14K` junto a `Prima $0.0`. No hubo prima cero: **no hubo prima
observada**.

**FLUJO 5M** — `sum()` sobre una columna rellenada con ceros publicaba `$0.0`
sin una sola impresión observada. Un cero ahí afirma «en los últimos cinco
minutos no se pagó prima direccional»; lo cierto es que **no se observó ninguna
impresión con la que medirlo**.

**BACKTEST** — la curva de fiabilidad dibujaba su diagonal de referencia también
con cero sesiones medidas. Una recta trazada de esquina a esquina se lee como un
resultado; ahora la referencia sólo aparece junto a algo que comparar con ella, y
el estado de recolección se declara.

**DARK POOL** — con `dark-flow` vivo y `dark-pool-levels` rechazado, la sección
enseña lo que sí tiene y declara la cobertura por vía, en vez de parecer entera o
parecer vacía.

---

## Lo que NO se ha podido verificar

- **No hay un 200 real de `dark-pool-levels`.** Sigue sin haber salida hacia
  `quantdata.us` desde el entorno de construcción, así que el contrato del
  proveedor no se ha podido leer ni confirmar. **Dark Pool Levels sigue
  pendiente.**
- **La validación LIVE con API key queda pendiente**, incluida
  `verify_live_quantdata.py --dark-pool` sobre la cesta multi-activo.
- **La regresión visual usa datasets sintéticos** de las formas que rompen,
  alimentando los renderizadores reales. La comparación contra una sesión de
  mercado real queda pendiente en el VPS.

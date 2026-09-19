# ITM QUANT MULTI ASSET · v1.48.0 — Un campo, un strike por barra, una marca que se ve

Release: `ITM_QUANT_v1.48.0_PRE_VPS` · Base: `v1.47.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Ninguna
métrica cambia de valor. El cambio es de **representación, agrupación visual y
escalado**; los valores originales no se tocan.

---

## 1 · El Interval Map era una tabla pintada

Se dibujaba como una rejilla de celdas duras sobre fondo negro, con huecos sin
pintar entre ellas. Eso obliga a leer celda por celda justo lo que hay que leer
como **zona**: dónde está la concentración, qué forma tiene y hacia dónde se
mueve.

La exposición por strike y tiempo es un **campo**: varía de forma continua entre
strikes vecinos y entre intervalos vecinos. `ITMQBars.field` lo prepara en tres
pasos y lo pinta como superficie:

1. **Relleno de huecos.** Una celda sin observación no es un cero: es un hueco.
   Se interpola desde sus vecinas con peso inverso a la distancia, así que una
   cresta no queda cortada porque falte un intervalo. **Un cero medido sigue
   siendo cero.**
2. **Suavizado gaussiano separable**, con el radio en **celdas** — no en píxeles
   ni en dólares —, así que no depende del panel ni del activo.
3. **Normalización por rango con signo**, con **suelo de ruido**. Sin el suelo,
   los percentiles se reparten uniformemente y media pantalla sale a media
   opacidad: un bloque macizo de verde y rojo donde no se distingue nada. Por
   debajo del percentil de fondo no se pinta.

Y **contornos cerrados**: antes se barría sólo el eje X, así que salían segmentos
verticales sueltos que parecían ruido. Ahora se cruzan los dos ejes y el contorno
rodea la zona.

## 2 · TRACE trataba el mismo dato de otra forma

TRACE tenía su propio umbral (`a < 0.02`) y su propia curva (`a^0.62`), así que
el mapa de fondo de TRACE y el INTERVAL MAP de la sección **se veían distintos
con el mismo dato**. Dos tratamientos del mismo dato es lo que hace que dos
pantallas del mismo programa no se parezcan, y obliga a corregir dos veces cada
ajuste. Los dos pasan ahora por el mismo campo.

## 3 · Una barra por strike, siempre

El perfil agrupaba tres strikes en una barra. El **strike es la unidad de lectura
de un perfil de exposición**: una barra que dice `516…518` obliga a abrir el
hover para saber cuál de los tres tiene el muro, que es justo lo que se estaba
mirando.

`aggregate: 'none'` no agrupa nunca, y `ITMQBars.extentFor` dice cuánto alto
necesita el panel para dar a cada strike una barra de grosor real. El panel crece
y el contenedor hace scroll, así que **no hay que elegir entre resolución y
grosor**:

| strikes | alto del panel | barras | grosor |
|---|---|---|---|
| 21 | 304 px | 21 | 11.0 px |
| 45 | 652 px | 45 | 11.0 px |
| 166 | 2.403 px | 166 | 11.0 px |
| 240 | 3.474 px | 240 | 11.0 px |

Vale igual para EXPOSICIÓN y para INTERÉS ABIERTO.

## 4 · Relieve 3D: el mismo perfil, otra pregunta

No sustituye a las barras. Las barras comparan **magnitudes** con precisión; el
relieve enseña la **forma** de la estructura: dónde está la masa, cómo de abrupto
es el borde y en qué strike cambia el signo. Con ciento sesenta y seis strikes eso
no se lee en una lista de barras aunque cada una sea legible.

Es una **proyección, no una simulación**: dos ejes reales —strike y valor— y una
profundidad constante que sólo da volumen. No hay perspectiva que deforme
magnitudes, y comparte la escala robusta de las barras: si la comprimiera de otra
forma, las dos vistas del mismo dato se contradirían al alternarlas.

El relieve **sí** agrupa, y por la razón contraria: es una vista de forma, y con
ciento sesenta y seis caras en un panel fijo cada una mediría dos píxeles.

## 5 · Las marcas de flujo son doradas

El color codificaba CALL/PUT con **el mismo verde y el mismo rojo que usa el
precio**, así que una marca de prima grande sobre un tramo de su propio color
desaparecía dentro de él.

El oro no lo usa ningún otro elemento del gráfico. El **sentido** va en la flecha
—▲ compra, ▼ venta— y la **cantidad** al lado, sobre un fondo propio para que no
se pierda encima del mapa de calor. En TRACE, además, una punta de flecha rellena
marca el punto exacto del precio y una guía la une con la cifra.

Igual en TRACE y en FLUJO DE ÓRDENES.

## 6 · Dark Flow publicaba ceros que no eran ceros

`norm_dark_flow` buscaba el volumen en una lista fija de nombres. Cuando el
proveedor usa otro, devolvía `0.0` y la sección publicaba **«608 intervalos ·
0.0 acc»**: un cero que afirma que no hubo actividad fuera de bolsa cuando lo
cierto es que no se pudo leer.

Ahora:

- se intenta primero por **nombre declarado**, que es lo correcto cuando el
  contrato se conoce;
- si ninguno aparece, se **deriva de la propia respuesta**: el campo numérico
  cuyo nombre contiene a la vez el concepto y una pista de magnitud. No es
  adivinar un valor —el valor lo manda el proveedor—; es descubrir bajo qué
  nombre lo manda;
- se **declara cuál se usó**, y la nueva tabla **DARK POOL · CAMPOS DE LA
  RESPUESTA** del Auditor enseña el mapa de campos, cuántos intervalos traen
  valor y qué campos publicó el proveedor;
- si ninguno sirve, el valor es `None` y no cero, y un carril del que no se puede
  leer nada **no deja la sección lista**.

---

## Lo que NO se ha podido verificar

- **`dark-pool-levels` sigue pendiente.** No hay salida hacia `quantdata.us`
  desde el entorno de construcción, así que no hay un 200 real ni forma de leer
  el contrato. La sección lo declara por carril y enseña lo que sí tiene.
- **Sin credenciales de Quant Data aquí**, el Interval Map y el mapa de TRACE se
  han verificado con el campo del motor y con datasets sintéticos de la forma
  real —90 strikes × 81 intervalos, con huecos—. Con la clave del usuario la
  misma ruta la alimenta el proveedor.
- **El carril de FLUJO no se ha podido ver con cinta real** en este entorno: su
  geometría está certificada en el banco con 390 y 780 buckets.

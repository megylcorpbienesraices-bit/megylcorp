# ITM QUANT MULTI ASSET · v1.51.0 — Un hueco deja de valer cero

Release: `ITM_QUANT_v1.51.0_PRE_VPS` · Base: `v1.50.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Lo que
cambia es que una lectura AUSENTE deja de publicarse como un cero: ninguna
métrica medida cambia de valor, y las que antes salían como `0.0` sin haberse
medido salen ahora como hueco.

**Ningún cambio es por activo.** Todo lo de aquí vive en el normalizador
compartido, en el componente de render o en el núcleo, y por tanto aplica a
**todos los activos y a todo el programa**. Hay un test que lo comprueba sobre el
código: ningún fichero de esta release contiene un ticker.

---

## 1 · La sierra de la deriva de volatilidad era un cero inventado

La curva de IV bajaba a **0.00** y volvía a subir, decenas de veces por sesión.
Una IV de cero es imposible, así que eso no era un dato: era un hueco pintado en
el suelo del eje. El fallo estaba en una línea del normalizador compartido:

```python
"value": _f(value, 0.0) or 0.0     # un bucket sin el campo → CERO
```

Y no es un problema de la volatilidad. `norm_time_series` sirve a **cinco**
herramientas, y en cuatro de ellas el cero es imposible:

| Serie | Lo que afirmaba un hueco convertido en cero |
|---|---|
| `volatility_drift` | la IV del subyacente es 0 % |
| `max_pain_over_time` | el max pain está en el strike 0 |
| `oi_over_time` | no hay ni un contrato abierto |
| `stock_price_over_time` | el subyacente cotiza a 0 dólares |
| `net_flow` | no se pagó prima (aquí **sí** puede ser cierto, y se distingue) |

Ahora un hueco viaja como `None` con `value_measured: false`. La prima neta
sigue siendo una medición cuando hay `call` y `put`: el neto **es** call − put.

**Y había un segundo cero, en el renderizador:** `sy(Q.num(p.v, 0))` mandaba al
suelo del eje todo punto sin valor. Ahora el trazo **se parte** en el hueco y
vuelve a empezar después. Se ve que falta un tramo, en vez de leerse un desplome.

## 2 · IV PERCENTIL 100 % sobre una amplitud de 0.00 pp

En pantalla convivían «IV PERCENTIL **100 %**» y «RANGO OBSERVADO 11.69 – 11.69 %,
amplitud **0.00 pp**». El rank ya se retenía —dividir por un rango cero da
infinito—, pero el percentil no, porque `(hist <= iv).mean()` sigue estando
*definido*. Definido sí, informativo no: con todas las lecturas iguales vale
100 % **siempre**. Se lee como «la IV nunca ha estado más alta» y lo cierto es lo
contrario: no ha estado en ningún otro sitio.

Esto **invierte** una decisión de v1.41.5. Ahora se retienen los dos y la sección
escribe la causa: `RANGO OBSERVADO SIN AMPLITUD · 44 lecturas idénticas`.

## 3 · Los totales de Net Drift tampoco

Lo destapó inyectar una serie **sin** sus agregados: el panel dibujaba la curva y
las tres tarjetas de arriba decían `$0.0`. `Q.num(x, 0)` tapaba el guardia que el
formateador ya tenía. Quitado el cero por defecto, un agregado ausente sale `—`.

## 4 · El relieve 3D no se entendía

Era cierto. Dibujaba caras de dos o tres píxeles y remataba con dos polilíneas
abiertas que se leían como rayas sueltas cruzando el gráfico. Dos causas:

- **La profundidad se calculaba como fracción del alto TOTAL del lienzo**, y el
  perfil por strike hace crecer ese lienzo hasta miles de píxeles. La fuga se
  calculaba sobre 2.880 px cuando en pantalla se veían 340. Ahora está acotada
  también en **píxeles absolutos** (26–96) y el contenedor del relieve tiene
  **alto propio**: el relieve agrupa, no necesita crecer.
- **Un suelo abierto no es un suelo.** Ahora es un cuadrilátero cerrado en fuga
  con sus líneas de valor.

Cada strike es un **prisma** con cara frontal, cara superior y tapa lateral,
sombreadas desde una sola fuente de luz. Sin las tres caras no hay volumen: hay
una barra con un borde.

## 5 · Una etiqueta por strike, sin saltarse ninguno

El eje numeraba uno de cada dos. El salto se calculaba contra un paso **fijo** de
15 px, pero el panel de strikes **crece** para dar a cada strike su fila, y ese
paso es el que manda. Con filas de 11 px y un divisor de 15, saltar era
inevitable. Ahora el salto sale del paso real y el paso sube a 13 px: si la fila
da para escribir, se escribe.

## 6 · El mapa de calor

Dos cosas estaban mal y las dos se notaban:

- **Los contornos no eran contornos.** Barrían cada eje y por cada cruce pintaban
  un palito de una celda **sin unirlo con el de la vecina**: una nube de rayitas
  que se leía como suciedad. Ahora es **marching squares** en el componente
  común: mira las cuatro esquinas de cada celda y emite el segmento que la
  atraviesa, así los de celdas vecinas se encuentran en el borde compartido y la
  curva se cierra alrededor de la zona.
- **El campo era un bloque saturado.** La normalización es por rango-percentil,
  así que la celda mediana vale siempre 0.5 exacto y media pantalla se iba al
  tope. Banda neutra más ancha (0.62), curva más suave (1.35) y un **techo de
  opacidad** del 0.70 que deja ver las velas y las isolíneas por encima.

TRACE y el panel de la sección comparten ahora umbrales, techo y suavizado.

## 7 · El Interval Map de la sección, con el diseño original

Tenía el mismo campo continuo que el fondo del TRACE, y ahí es la representación
equivocada, porque las dos pantallas no responden lo mismo:

- **TRACE** · el mapa es **fondo**. Va debajo de las velas y hace falta la
  **forma** de la zona. Un campo con isolíneas es eso, y al ser translúcido deja
  ver el precio.
- **Sección** · el mapa es el **sujeto**. Se viene a leer celda a celda. Un
  degradado interpola entre vecinas y no deja saber dónde acaba una celda; un
  punto por celda sí, porque su **diámetro es la magnitud**.

Rejilla de puntos, rojo/verde por signo, con el recorrido del precio superpuesto.

## 8 · Net Drift: muros y flujo, en el eje correcto

El eje izquierdo mide **prima acumulada en dólares** y Call Wall es un **precio
de strike**. Colgarlos del mismo eje pintaría 534 dólares de prima donde hay un
muro en 534 de precio.

Los muros y las marcas doradas van sobre el eje del **precio**, que ya existía a
la derecha. Lo intenté primero con un eje nuevo y estaba mal: salían **dos
escalas de precio con dominios distintos** en el mismo gráfico, que es peor que
no dibujar los muros, porque las dos parecen válidas y sólo una sitúa bien la
línea. Una sola escala.

Un nivel muy lejano se excluye del encuadre en vez de aplastar el recorrido del
precio contra una línea.

## 9 · Rotulación y tamaño

- Las etiquetas de nivel pasan de 10 a **12 px** en pastilla de 20 px, y la línea
  de 1 a 1.6 px. Definidas **una sola vez** en el núcleo: TRACE, la cinta y Net
  Drift dibujan los mismos niveles y con la medida repetida en tres sitios
  volvían a divergir al primer ajuste.
- **TRACE**: suelo de **640 px** y columnas laterales a 252 px.
- **Net Drift**: suelo de **360 px** en píxeles. Repartir sólo por fracción dejaba
  el gráfico pequeño en cuanto la ventana bajaba.

---

## Límites declarados

- **Sigue sin haber un HTTP 200 real de `dark-pool-levels`**: sin salida a
  `quantdata.us` ni credenciales.
- **Net Drift se verificó inyectando una serie sintética** en el navegador real,
  porque aquí no hay serie del proveedor. Lo verificado es la **geometría** del
  render, no el dato.
- **Las marcas doradas sobre cinta real no se han podido fotografiar**: no hay
  cinta de opciones en este entorno.
- **El ZIP es de fuentes, sin certificar**: el empaquetador oficial es
  fail-closed sobre Python 3.12.14 / Node 22.16.0 y aquí corre 3.11.15 / 22.22.2.

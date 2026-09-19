# AUDITORÍA DEL MOTOR · ITM QUANT v1.47.0

## Veredicto

v1.46.0 auditó cosas distintas tratadas como iguales. v1.47.0 audita algo más
incómodo: **defectos que la suite no podía ver**, porque estaban en la geometría
que se dibuja y no en el número que se calcula.

La suite llevaba tres releases en verde mientras las barras se veían como
rayitas. No había ningún fallo que detectar: el dato estaba, la escala era
correcta y el suelo de grosor «existía». Estaba aplicado al sitio equivocado, y
ninguna aserción numérica podía notarlo.

---

## 1 · Un suelo aplicado donde no servía

```js
const maxBars = Math.floor(widthPx / MIN_BAR_PX);   // 3 px de PASO
…
return Math.max(MIN_BAR_PX, Math.min(maxBar, slot * 0.82));   // 82 % del paso
```

Las dos líneas son razonables leídas por separado. Juntas garantizan lo
contrario de lo que prometen: se reserva un paso de 3 px y luego se dibuja el
82 % de él, **2.46 px**. El mínimo no se alcanzaba nunca.

Es el mismo patrón que esta serie de releases viene encontrando: dos decisiones
correctas en sitios distintos que se contradicen al juntarse, y nada que lo
declare. Aquí la contradicción no producía un error ni un número falso — producía
un panel ilegible, que es más difícil de reportar y más fácil de normalizar.

## 2 · Grosor fijo no puede resolver densidad variable

```
 20 barras en 300 px  →  caben gruesas
390 barras en 300 px  →  a 4 px, 1.560 px de contenido en 300
```

Cualquier constante que funcione para la primera rompe la segunda. Las versiones
anteriores probaron 2, 5 y 3 px, y cada una falló en cuanto cambió la cantidad de
strikes, de buckets o el tamaño del activo.

La salida no es otra constante mejor: es **cambiar qué se ajusta**. Con pocas
barras se ajusta el grosor; con muchas se ajusta **cuántas barras hay**. La
agrupación visual es lo único que convierte un problema sin solución en uno que
sí la tiene.

## 3 · Una agrupación que no puede perder información

La objeción obvia a agrupar es que oculta. Se evita con tres decisiones:

- **Nunca una media.** Cancela: un +8 y un −8 vecinos dan 0 y borra la
  concentración justo donde hay que verla. Se conserva el **extremo** —un valor
  que alguien observó— o la **suma**, según lo que signifique el eje.
- **El contenedor lleva su rango, su recuento y sus miembros.** El `hover`
  inspecciona los originales; la agrupación es sólo lo que se dibuja.
- **El pico se marca cuando domina a la suma.** En un intervalo con signos
  mezclados la suma puede ser pequeña y esconder un evento grande.

## 4 · El primer fotograma también es un resultado

`Glide` animaba cada clave desde cero. Nadie lo consideró un defecto porque «se
arregla en dos fotogramas»… mientras el bucle de animación siga vivo. Un panel
que entra en pantalla por el observador de visibilidad dibuja **uno**.

El resultado era un panel con el eje correcto —±4.2 B— y ni una sola barra. Es el
mismo error que `GlideValue` en v1.46.0, en la clase de al lado, y no se encontró
entonces porque se arregló el síntoma donde se veía en vez de buscar el patrón.

## 5 · El `except` que convertía un bug en «no hay datos»

```
NameError: name '_f' is not defined
```

en cada construcción del trace, tragado por el `except Exception` de
`/api/terminal/bundle`, que devolvía un trace vacío. La terminal enseñaba velas,
perfiles y niveles en blanco. **Indistinguible de un mercado sin datos.**

Un `except Exception` alrededor de una construcción de datos es el peor sitio
donde puede esconderse un fallo de programación, porque el modo de fallo que
produce —un panel vacío— es también el modo de fallo legítimo. La corrección es
el nombre; la lección es que ese `except` necesita distinguir «el proveedor no
trajo nada» de «nuestro código se rompió», y hoy no lo hace.

## 6 · Agregado ≠ desglose, tres veces

Tres secciones publicaban dos hechos de fuentes distintas como si uno implicara
al otro:

| sección | agregado | desglose | lo que se veía |
|---|---|---|---|
| Interés abierto | `positioning` | perfil / proveedor | `37.1K` sobre `SIN INTERÉS ABIERTO` |
| Estadísticas | cadena oficial | impresiones | `14K contratos` sobre `$0.0 de prima` |
| Volatilidad | — | dos observaciones | `+0.000 pp` sobre `NO DISPONIBLE` |

Las seis cifras eran ciertas por separado. Juntas se leen como un fallo del
programa, y el analista no tiene forma de saber cuál de las dos creer. La
corrección es la misma en los tres casos: **declarar la disponibilidad de cada
hecho por su cuenta**, y cuando falte el detalle, decir por qué en vez de publicar
el cero que lo sustituye.

---

## Lo que esta auditoría NO puede afirmar

- Que `dark-pool-levels` devuelva 200. Sigue sin haber salida hacia el proveedor
  desde el entorno de construcción.
- Que los paneles se lean bien con una sesión real de mercado. Lo que sí se
  afirma, medido sobre los renderizadores reales: en 46 combinaciones de activo,
  distribución, densidad y viewport, ninguna barra baja de 5 px, ninguna se
  solapa y ninguna se queda sin separación.

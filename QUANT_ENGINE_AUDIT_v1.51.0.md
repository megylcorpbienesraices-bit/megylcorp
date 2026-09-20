# AUDITORÍA DEL MOTOR · ITM QUANT v1.51.0

## Veredicto

Ninguna fórmula cambia. Lo que cambia es que el programa **deja de afirmar
ceros que nadie midió**, y eso, aunque suene a presentación, es corrección de
datos: un cero es una afirmación sobre el mercado.

Merece la pena mirar por qué el fallo sobrevivió a cuatro releases que ya iban
persiguiendo exactamente esto.

---

## 1 · El cero estaba una capa más abajo de donde se buscó

Desde v1.46.0 se han ido eliminando ceros fabricados: en el formateador, en el
FLUJO 5M, en GEX/DEX, en la deriva de IV del KPI, en la prima de estadísticas.
Todos ellos, sin excepción, estaban en la **capa de presentación**.

El que quedaba estaba en el **normalizador del proveedor**:

```python
"value": _f(value, 0.0) or 0.0
```

Y por eso no lo encontró ninguna de las revisiones anteriores: se estaba mirando
dónde se *escribe* el número, no dónde se *construye*. Para cuando la fila llegaba
a la interfaz ya era un cero legítimo, indistinguible de uno medido. Ninguna
comprobación aguas abajo podía deshacerlo.

La lección operativa es estrecha y concreta: **la regla de «ningún cero
fabricado» tiene que aplicarse en el punto de entrada del dato, no en el de
salida.** Aguas abajo sólo se puede propagar lo que el normalizador decidió.

## 2 · Por qué esta línea era peligrosa y parecía inocente

`norm_time_series` nació para `net-flow`, donde un cero **sí** puede ser una
lectura: ese minuto no se pagó prima. Con ese uso, el `0.0` por defecto es
defendible. Después la función se reutilizó para otras cuatro herramientas —max
pain, interés abierto, precio, deriva de IV— y en las cuatro el cero es
**imposible**, no improbable.

El defecto no se introdujo al escribir la línea: se introdujo al **reutilizar la
función sin revisar si su valor por defecto seguía teniendo sentido**. Un
`default=0.0` es una afirmación sobre el dominio, y al cambiar de dominio deja de
ser cierta sin que nada falle.

```
IV = 0 %            imposible
max pain = strike 0 imposible
OI = 0 contratos    posible pero no es lo que significa un campo ausente
precio = $0         imposible
prima neta = 0      POSIBLE, y por eso se distingue con value_measured
```

## 3 · Un percentil definido no es un percentil informativo

`(hist <= iv).mean()` está perfectamente definido con rango cero. Vale 1.0.
v1.41.5 razonó desde ahí y conservó el número, reteniendo sólo el rank, que sí
diverge.

El razonamiento es correcto y la conclusión era mala, por una razón que no se ve
desde la fórmula: **un estadístico que vale lo mismo pase lo que pase no informa
de nada**, aunque esté bien calculado. Con todas las lecturas iguales, el
percentil dice 100 % tanto si la IV está alta como si está baja, porque no hay
distribución contra la que compararla.

Y en pantalla, junto a «amplitud 0.00 pp», se leía exactamente al revés de lo que
pasaba. Los dos números se retienen y se escribe la causa.

## 4 · Dos escalas para la misma magnitud

Al colgar los muros del Net Drift creé un eje de precio nuevo, sin ver que ya
había uno. El resultado fueron **dos columnas de precios a la derecha con
dominios distintos**.

Esto es peor que el problema que venía a resolver. Un muro no dibujado es una
ausencia evidente: se ve que no está. Dos escalas de precio son una ambigüedad
**invisible**: las dos parecen válidas, y quien mire la que no corresponde situará
el muro en el sitio equivocado sin ninguna señal de que algo va mal.

El test que lo impide cuenta las escalas, no las comprueba una a una:

```python
assert body.count("Q.scale(plo") == 1
```

Y lo encontré mirando la captura, no ejecutando tests. La verificación visual no
es un extra sobre la suite: hay fallos que sólo existen en el píxel.

## 5 · Una medida repartida en tres sitios diverge

Las etiquetas de nivel se dibujaban con un 10, un 15 y un 17 escritos a mano en
tres ficheros. Los tres eran el mismo concepto —cómo se rotula un muro— y al
subir el tamaño en uno solo, las tres pantallas habrían dejado de parecerse.

Ahora `LEVEL_FONT`, `LEVEL_LABEL_H`, `LEVEL_LABEL_GAP` y `LEVEL_LINE_WIDTH` viven
en el núcleo, junto a la tabla `LEVELS` que ya definía el color y el nombre.

## 6 · Una fracción sin tope es una constante oculta

La profundidad del relieve era `b.h * 0.34`. Parece adimensional y adaptativo, y
lo es mientras `b.h` se mueva en un rango razonable. El perfil por strike hace
crecer el lienzo hasta miles de píxeles, y entonces esa fracción produce una
fuga de casi mil píxeles dentro de una ventana visible de trescientos.

Es el mismo error de fondo que el grosor fijo de las barras, sólo que al revés:
allí una constante absoluta no escalaba; aquí una fracción sin límites escalaba
demasiado. **Las dos necesitan acotarse por los dos lados.**

---

## Lo que esta auditoría NO puede afirmar

- **Que el Net Drift se vea así con datos del proveedor.** Se verificó con una
  serie **inyectada** en el navegador real. La geometría está comprobada; el dato
  no, porque aquí no hay serie de net-drift.
- **Que las marcas doradas funcionen sobre cinta real.** No hay cinta de opciones
  en este entorno.
- **Que `dark-pool-levels` devuelva 200.** Sin salida a `quantdata.us` ni
  credenciales.
- **Que el artefacto esté certificado.** Toolchain fail-closed no disponible aquí.

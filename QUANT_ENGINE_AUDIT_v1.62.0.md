# AUDITORÍA DEL MOTOR · ITM QUANT v1.62.0

## Veredicto

**Ninguna fórmula cambia.** Lo que esta release corrige es de dónde sale el
ESTADO, y el defecto es de una clase que conviene nombrar porque no se detecta
mirando ningún número: **cuatro sitios deducían el mismo hecho, y cada uno
acertaba sobre lo que miraba**.

Ninguno de los cuatro tenía un error de lógica. Los cuatro leían una **sombra**
del hecho —el bloque publicado— en vez del hecho.

---

## 1 · Por qué la contradicción era inevitable, y no un descuido

El bloque de un carril conserva sus datos cuando el refresco falla. Eso es
**correcto y deliberado**: tirar 382 filas buenas porque una petición se murió
por plazo dejaría la pantalla en blanco por un fallo de red.

Pero esa decisión correcta convierte al bloque en una fuente **ambigua**:

```
ready = True, rows = 382     ¿son de este ciclo, o del anterior?
                             el bloque NO lo dice
```

Cuatro lectores, cuatro respuestas, todas defendibles:

```
mirando `bool(rows)`     → CON DATOS      (cierto: hay filas)
mirando `lane_status`    → STALE          (cierto: el refresco falló)
mirando `ready`          → DATA_OK        (cierto: el bloque está listo)
```

La lección: **cuando un dato tiene que sobrevivir a su propia caducidad, deja de
poder responder por sí solo si está vigente.** Hace falta un registro aparte de
lo que pasó, y es lo que no existía.

---

## 2 · El registro va en la ejecución, no en la lectura

`lane_truth` se escribe en el instante de la llamada, donde se conoce:

* qué contestó el proveedor, o qué fase del transporte expiró;
* cuántas filas trajo **esta** ejecución;
* qué quedaba del último ciclo bueno y de cuándo;
* a qué `request_id` y `cycle_id` pertenece todo lo anterior.

Los identificadores no son decoración. Con ellos, «una pantalla dice A y otra
dice B» deja de ser una discusión: o las dos citan la misma ejecución, o se ve
en el identificador que no. Es la diferencia entre auditar y opinar.

---

## 3 · El error que casi cometo al cablearlo

Al principio, `read()` devolvía una verdad de ESPERA cuando no había registro.
Parecía inocuo —«si nadie ha escrito nada, es que aún no ha corrido»— y es
**falso**: que nadie lo haya escrito no demuestra que esté esperando. Con el
registro vacío, los 36 carriles se habrían declarado «EN COLA» aunque tuvieran
datos.

Es exactamente el mismo error que esta release corrige, cometido por el otro
lado: **afirmar un estado que nadie midió**. Por eso existe `known`, y por eso
las cuatro vistas caen a lo que sepan por su cuenta cuando vale `False`.

Apareció en tres sitios distintos —el carril, el diagnóstico y el motor de
muros—, lo que sugiere que la tentación es estructural y no un descuido puntual.

---

## 4 · Dos preguntas que hay que mantener separadas

```
¿falló el refresco?      current_status = PROVIDER_ERROR   → sí
¿la sección está rota?   is_failure     = False            → no: hay 382 filas
```

Las dos son verdad a la vez y **no se pueden fusionar**. Fusionarlas en un
sentido da el defecto de v1.57.0 —una sección declarada rota con datos buenos en
pantalla—; fusionarlas en el otro da el de estas capturas —«CON DATOS» sobre una
llamada muerta—.

Lo que las mantiene separadas es que `current_status` y `serving` son idénticos
en todas partes, y `is_failure` responde **sólo** a si hay algo que enseñar.

---

## 5 · `EL_PROVEEDOR_NO_DEVOLVIÓ_FILAS`

Merece un apartado porque no era un error de cálculo sino de **atribución**: se
ponía cuando no había filas, sin haber comprobado por qué no las había. Culpaba
al proveedor de un hueco que, en el caso de la captura, era del transporte
propio —una petición muerta a los 11.4 s—.

Una causa por defecto que nombra a un tercero es peor que no tener causa: manda
a buscar el problema en el sitio equivocado, y lo hace con aplomo.

---

## 6 · Lo que esta auditoría NO afirma

* **Que las contradicciones hayan desaparecido en producción.** Lo medido aquí
  son sintéticos y un ciclo real contra un proveedor local. Las capturas que
  abrieron el caso son del Windows del operador, y ahí se cierra o no se cierra.
* **Que el transporte esté sano.** Los 11 segundos ahora se explican por fase;
  explicarlos no los acorta. Ese bloque sigue abierto.
* **Que las Walls que salen sean correctas.** La fórmula, su convención y sus
  límites siguen siendo los de v1.57.2, intactos. Lo que cambia es que cuando
  no se calculan, se dice por qué.

# AUDITORÍA DEL MOTOR · ITM QUANT v1.49.0

## Veredicto

Tres releases persiguiendo un 400 sin poder leer el contrato. La corrección
final no vino de más razonamiento sobre el código: vino de **leer la
documentación**, que era lo que faltaba desde el principio.

Merece la pena mirar por qué el razonamiento, siendo correcto, no bastaba.

---

## 1 · Un razonamiento correcto con una conclusión falsa

v1.46.0 concluyó que había que partir del cuerpo mínimo:

> Un cuerpo con campos de más es tan inválido como uno con campos de menos, y
> `dark-pool-levels` no comparte contrato con `dark-flow` ni con
> `equity-prints`.

Las dos premisas son ciertas. Lo que falta es que **el mínimo correcto no era el
mínimo que se podía deducir**: el contrato exige `sessionDateRange`, un campo que
ninguna otra herramienta del catálogo usa. No estaba en el espacio de
posibilidades que se podía construir mirando las vecinas, por mucho que se
recortara.

Ésta es la forma de fallo que conviene reconocer: **razonar dentro del espacio
equivocado**. La disciplina de «no adivinar» era correcta y por sí sola no podía
llegar a la respuesta. Lo que hacía falta era una fuente externa, y la decisión
que sí se puede criticar es haber seguido tres releases sin decir con suficiente
claridad que *eso* era el bloqueo.

## 2 · Dos nombres parecidos, dos contratos

`dark-flow` acepta `sessionDate` y `timeRange`. `dark-pool-levels` usa
`sessionDateRange`. Un parámetro de fecha, tres nombres, dos endpoints
hermanos.

Por eso «heredar el payload de la vecina» falla de una manera especialmente
difícil de diagnosticar: no falla por llevar un campo de más —eso se corrige
quitando— sino por llevar el **concepto correcto con el nombre equivocado**, y
ninguna cantidad de recorte convierte `sessionDate` en `sessionDateRange`.

## 3 · La prohibición estaba en el sitio equivocado

`aggregationPeriod` es legítimo en `dark-flow`, está en la tabla de reparaciones
y lo rechaza `dark-pool-levels`. La lista de campos prohibidos era **del
catálogo**, así que un 400 mal leído podía hacer que la reparación guiada por el
error se lo añadiera justo al endpoint que lo prohíbe.

Es el mismo patrón que esta serie viene encontrando —una regla escrita a un nivel
más grueso del que le corresponde— y aquí tenía una consecuencia concreta: el
mecanismo diseñado para corregir el cuerpo podía romperlo.

## 4 · Un 200 que se leía como SIN DATOS

La respuesta es un **mapa por nivel de precio**; el extractor sólo leía listas.
Con un mapa devolvía cero niveles.

El modo de fallo vuelve a ser el peor: **plausible**. Un 200 válido, con datos
dentro, publicado como «SIN DATOS» e indistinguible de una sesión sin actividad
fuera de bolsa. Si el cuerpo se hubiera corregido sin corregir esto, el endpoint
habría respondido bien y la pantalla habría seguido vacía — y se habría vuelto a
buscar el fallo en el request.

Que las dos correcciones vayan juntas no es casualidad: las dos vienen de haber
leído el contrato, que describe la petición **y** la respuesta.

## 5 · Lo que impide que esto se repita

- El cuerpo se comprueba contra el contrato en una prueba que enumera los siete
  campos prohibidos uno a uno.
- La fecha nunca puede ser un día no bursátil, con la prueba escrita sobre un
  sábado y un domingo concretos.
- El 400 se conserva desglosado y llega al Auditor: la próxima vez que un
  endpoint rechace un cuerpo, el campo estará en pantalla y no habrá que
  deducirlo.
- El verificador LIVE no da por cerrado el endpoint sin un **HTTP 200 real**.

---

## Lo que esta auditoría NO puede afirmar

Que el endpoint responda 200. El cuerpo es el del contrato publicado y el parser
cubre la respuesta documentada, pero **este entorno no tiene salida hacia
`quantdata.us` ni credenciales**. La comprobación es una orden de una línea, y
hasta que se ejecute el endpoint sigue declarado como pendiente.

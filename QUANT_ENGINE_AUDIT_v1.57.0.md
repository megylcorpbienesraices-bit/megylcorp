# AUDITORÍA DEL MOTOR · ITM QUANT v1.57.0

## Veredicto

Esta release corrige dos defectos que llevaban tiempo activos y que tienen la
misma raíz: **adivinar el significado de un campo en vez de leer el contrato.**

Uno de los dos producía lecturas **invertidas** sobre un gráfico de operativa.
Eso no es un problema de presentación.

---

## 1 · Por qué un prefijo de una letra sobrevivió tanto

```python
direction = 1 if side.startswith(("BUY", "ASK", "A")) else \
           -1 if side.startswith(("SELL", "BID", "B")) else 0
```

La lógica parece razonable: se añadieron `"A"` y `"B"` como red de seguridad,
por si el proveedor mandaba `ABOVE_ASK` o `BELOW_BID`. Y funcionan para esos dos.

El problema es que `"A"` y `"B"` no son abreviaturas de «ask» y «bid»: son
prefijos de cualquier palabra. `AT_BID` empieza por `"A"` y **`AT_BID` es una
venta**. La red de seguridad capturaba justo el caso contrario al que pretendía
cubrir.

No falló nunca de forma visible porque **una clasificación invertida no produce
ningún error**. Produce una flecha del color equivocado, y sólo se detecta
mirando el dato real o auditando la regla.

La lección que queda escrita: **una heurística que no puede fallar ruidosamente
necesita una prueba por cada valor que pretende cubrir.** `aggressor.py` tiene
treinta.

## 2 · Dos campos con el mismo nombre y significados distintos

```python
"side": "CALL" if net > 0 else "PUT" if net < 0     # motor: dominancia de prima
"side": "AT_ASK"                                     # cinta: posición en el NBBO
```

Los dos se llamaban `side` y los dos llegaban a la misma función de la interfaz,
que los trataba igual:

```js
if (dir === 'BUY' || dir === 'ASK' || dir === 'CALL') return true;
```

Ese `|| dir === 'CALL'` es el punto exacto donde una magnitud se convirtió en
otra. Y el efecto es asimétrico y peligroso: no marcaba «no sé», marcaba lo
**contrario** para la mitad de los casos. Una put comprada —posición bajista
alcista en delta, pero una compra— salía con flecha roja de venta.

Ahora son `premium_side` y `aggressor`, con nombres que no se pueden confundir
al leerlos, y la interfaz sólo mira el segundo.

## 3 · «Desconocido» tiene que existir como estado

El defecto anterior no era sólo de mapeo. Era que **no había forma de decir «no
lo sé»**: `flowIsBuy` devolvía un booleano, así que todo evento acababa siendo
compra o venta. Un tipo booleano para una magnitud de tres estados fuerza a
inventar el tercero.

`flowSide` devuelve `true`, `false` o `null`, y `null` se dibuja como rombo. El
cambio de tipo es lo que hace imposible el defecto, no la corrección del mapeo.

## 4 · La heurística de campos, otra vez

`dark-flow` publica las acciones en `size`. El normalizador buscaba un campo que
contuviera «dark» u «offExchange». Ninguna de las dos vías podía encontrarlo.

Es el mismo error que `dark-pool-levels` en v1.49.0: partir de lo que uno espera
que el proveedor haya llamado a las cosas, en vez de leer cómo las llamó. Y
tiene la misma respuesta: **cuando hay contrato publicado, el mapeo es
determinista y los alias van detrás, declarados.**

Lo que sí se conserva de aquella versión es el diagnóstico: `field_map` dice qué
clave acabó sirviendo cada magnitud. Sin eso, «608 intervalos · 0.0 acc» no se
puede corregir sin volver a capturar la respuesta entera.

## 5 · Una fuente derivada no puede bloquear a la directa

Los paneles de Dark Pool esperaban a `VENUE_CONFIRMED`, que **infiere** el dark
pool del campo `venue` de otra cinta. Es una aproximación útil como segunda
medida y pésima como puerta: cuando no confirmaba, la sección decía SIN DATOS
con 608 filas del proveedor ya descargadas.

La regla que queda: **la dirección de la dependencia va de lo derivado a lo
directo, nunca al revés.** Las capas propias siguen en `audit`, que es donde
sirven: dos medidas independientes que coinciden valen más que una; dos que no
coinciden son información.

## 6 · Un ritmo correcto en el estado equivocado

4 peticiones por ciclo, concurrencia 2, 15 s entre ciclos. Es una política
sensata para régimen permanente y produce dos minutos de pantalla vacía después
de un cambio de activo.

El error no era el número: era aplicar **una sola política a dos estados
distintos**. Durante el arranque no hay nada que proteger, porque no hay nada
dibujado. La ráfaga está acotada por plazo, por alcance —sólo lo que dibuja la
pantalla— y por una salida anticipada, y nunca toca la reserva del motor.

---

## Lo que esta auditoría NO puede afirmar

- **Que el lado agresor sea correcto contra una cinta real.** Se ha verificado el
  clasificador valor a valor y la cadena del normalizador, incluidos los casos
  que la versión anterior invertía. Contra el proveedor real, no.
- **Que Dark Pool muestre datos en la terminal del usuario.** La cadena se
  comprueba con la forma publicada del contrato; la ejecución LIVE es suya.
- **Que las cuatro griegas vengan del Interval Map.** Aquí salen del respaldo
  del motor declarado, porque no hay proveedor.
- **Que el artefacto esté certificado.** Toolchain fail-closed no disponible.

---

## 7 · v1.53.0 · Cuando «no inventar» tampoco basta

v1.52.0 quitó el lado inventado y dejó rombos neutros. Era correcto y en
producción resultó **inútil**: ninguna marca tenía lado, así que la pantalla
pasó de mentir a no decir nada. Las dos cosas fallan, sólo que de forma distinta.

Lo que faltaba era ver que **había otra medición disponible**. El proveedor no
siempre manda un campo de lado, pero sí manda el precio y el NBBO del instante, y
comparar los dos no es una heurística: es la definición operativa del agresor.

La lección: cuando un dato no viene declarado, antes de publicar «desconocido»
hay que preguntarse si se puede **medir** desde lo que sí viene. «No inventar» y
«no medir» no son lo mismo, y confundirlos deja la pantalla vacía con la
información delante.

## 8 · Un umbral defendible no es necesariamente el correcto

2:1 se justificó como «no etiquetar ruido». Es defendible y estaba mal calibrado:
descartaba como «repartido» el 65/35, que es un sesgo direccional claro. Un
umbral conservador no es neutral — desplaza el error hacia el otro lado y pierde
señal real.

Publicar la **confianza** junto al veredicto es lo que hace que el umbral importe
menos: quien mira puede distinguir un 61 % de un 95 % sin que el corte decida por
él.

## 9 · Normalizar contra lo que no se ve

El campo de calor se normalizaba por rango-percentil sobre toda la matriz del
proveedor, y la ventana visible es una franja estrecha de ella. El efecto es
contraintuitivo y por eso costó verlo: **cuantos más strikes trae el proveedor,
menos contraste tiene lo que se está mirando**, porque el percentil se calcula
contra celdas fuera de pantalla.

La regla que queda: **una normalización relativa tiene que calcularse sobre el
mismo conjunto contra el que el ojo compara.** Si la vista recorta, la
normalización recorta antes.

## 10 · Un diagnóstico que mira otra fuente no diagnostica

El ViewModel ya leía al proveedor y el Diagnóstico de Paneles seguía declarando
las capas derivadas. Es el peor estado posible: la herramienta que existe para
localizar el fallo estaba señalando al sitio equivocado, y eso cuesta más tiempo
que no tener diagnóstico.

La regla: **el diagnóstico y la vista leen la misma fuente, siempre.** Si divergen,
el diagnóstico deja de ser un instrumento y pasa a ser otra cosa que auditar.

## 11 · v1.57.0 · Un factor contado dos veces no se ve en el resultado

Call Wall y Put Wall se elegían con una media geométrica de dos evidencias: la
exposición por strike **que publica el proveedor** y el interés abierto del
strike.

```
score = 100 · exposición_lado^(1−w) · interés_abierto_lado^w      w = 0.5
```

El razonamiento parecía sólido —«un strike con exposición calculada pero sin
libro detrás no es un muro»— y el defecto estaba un nivel por debajo: **el
interés abierto ya es un factor de la exposición**.

```
Gamma Exposure = gamma × OI × multiplicador × precio² × 0.01
                          ↑
                    ya está aquí
```

Multiplicarlo otra vez lo cuenta dos veces. La consecuencia es un sesgo: el muro
se desplaza hacia strikes con mucho libro abierto y gamma pequeña, que son
precisamente los que menos cobertura obligan a ajustar. Y no hay forma de
detectarlo mirando la salida, porque el resultado es un strike plausible con una
puntuación alta.

La regla: **cuando una magnitud derivada ya contiene un factor, ponderar por ese
factor no añade evidencia; introduce un sesgo del que no queda rastro.** La única
defensa es calcular la magnitud entera desde sus ingredientes —gamma y OI por
contrato— y ordenar por ella.

Lo que esta auditoría **no** puede afirmar: que el strike que sale ahora sea el
que frene al precio. La fórmula mide dónde tendría que ajustar más el dealer si
el precio llegara allí, bajo una convención de posicionamiento **declarada**
(`CLIENTE_LARGO_OPCIONES__DEALER_CORTO_GAMMA`), no medida. El inventario real de
clientes y dealers no lo publica ningún proveedor de los conectados. Una wall es
una concentración de cobertura probable, no una barrera.

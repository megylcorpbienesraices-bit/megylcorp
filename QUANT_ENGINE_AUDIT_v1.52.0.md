# AUDITORÍA DEL MOTOR · ITM QUANT v1.52.0

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

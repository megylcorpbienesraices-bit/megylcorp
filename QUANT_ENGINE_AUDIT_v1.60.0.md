# AUDITORÍA DEL MOTOR · ITM QUANT v1.60.0

## Veredicto

Esta release **no cambia ninguna fórmula ni toca los bloques cerrados en
v1.59.0**. Corrige el transporte, que es lo que estaba impidiendo que los datos
llegaran para alimentarlas.

El defecto tiene una forma que conviene nombrar, porque se repite: **una
constante ocupando el sitio de una medida, en un bucle que impedía tomar la
medida**.

---

## 1 · Cómo se distingue un defecto del endpoint de uno del transporte

La evidencia de Windows no era ambigua, y la regla que la lee tampoco:

```
net_drift · net_flow · dark_flow · gamma       →  connect timed out after 4.0s
```

Cuatro endpoints con cuerpos distintos, cadencias distintas, pesos distintos y
rutas distintas. Lo único que comparten es el **host**. Un fallo idéntico en
cosas que sólo comparten el destino no puede ser de las cosas: es del destino, o
del camino hasta él.

Esa regla es la que decide dónde vive cada autoridad a partir de ahora:

```
CONEXIÓN  →  del HOST      un handshake no pertenece a ninguna herramienta
LECTURA   →  del ENDPOINT  una respuesta lenta no dice nada sobre la red
```

Y también dice por qué medir la conexión **por endpoint** era un error incluso
antes de fallar: reparte treinta y seis veces la misma muestra, y hace falta
treinta y seis veces más tráfico para aprender lo mismo.

---

## 2 · El bucle que impedía aprender

Esto es lo que esta auditoría considera el hallazgo de la release, porque ya
había aparecido una vez y volvió a aparecer en otro sitio:

```
plazo corto → el handshake no completa → no hay muestra
            → no hay p95 → el plazo sigue corto → …
```

v1.58.1 cerró exactamente este bucle para la **lectura** (`record_failure` no
tocaba las latencias, así que un endpoint que nunca respondía nunca dejaba una
muestra y nunca calibraba). La corrección de entonces no se generalizó, y el
mismo bucle seguía abierto para la **conexión**.

La salida es la misma en los dos casos: **un plazo agotado tiene que dejar
constancia y subir el siguiente**. Un fallo que no enseña nada es un fallo que se
repite idéntico.

La escalada tiene techo (15 s) y decae sola cuando vuelve a haber handshakes
buenos. Sin el decaimiento, un mal minuto condenaría a la sesión entera a
esperar quince segundos a un host que ya conecta en trescientos milisegundos.

---

## 3 · Lo que de verdad quita los timeouts

No es el plazo. Es que **el handshake deje de ocurrir**.

```
antes   2 pools × keep-alive 5 s × ciclo 15 s  →  un handshake por herramienta
                                                  y por ciclo, todos a la vez
ahora   1 pool  × keep-alive 90 s              →  se paga una vez y se reutiliza
```

La cifra que lo demuestra es `reuse_pct`, y por eso se publica por host en el
Auditor y en el informe LIVE. En el ensayo en seco contra un proveedor local:
13 peticiones, **1 handshake**, 92 % de reutilización.

El warm start sube de 4 s a 8 s, y esta auditoría quiere ser explícita sobre lo
que eso es y lo que no es. **Es** la corrección de un supuesto falso: «si la
conexión no se establece en cuatro segundos, no va a establecerse» es una
afirmación sobre la red que la evidencia contradice. **No es** el arreglo: si lo
fuera, el número seguiría mandando con cien muestras encima, y hay una regresión
que comprueba precisamente que **no** manda.

---

## 4 · Una contradicción encontrada al escribir el certificador

Merece quedar escrita porque casi produce una prueba imposible.

Dos afirmaciones del informe LIVE se pedían a la vez:

* «las conexiones se reutilizan» (keep-alive funcionando);
* «el plazo de conexión lo gobierna el p95 del handshake medido».

**No pueden pasar las dos.** Si el keep-alive funciona, casi no hay handshakes;
sin handshakes no hay p95 que medir. Exigir las dos habría hecho fallar el
informe precisamente cuando el arreglo funciona.

La afirmación se reformuló a lo que de verdad hay que descartar: que el plazo
esté **escalando**, porque eso significa que se siguen agotando handshakes.
`WARM_START` con pocos handshakes no es un fallo: es la prueba del arreglo.

---

## 5 · Lo que esta auditoría NO afirma

* **Que el defecto esté corregido en producción.** Lo dicho aquí está medido
  contra sintéticos y contra un proveedor local. La estampida real, el enlace
  real y el antivirus real del operador sólo se miden en su Windows. Por eso el
  bloque queda **abierto** hasta que el certificador LIVE lo cierre.
* **Que el proveedor sea rápido.** Nada de esto acelera a Quant Data: quita
  trabajo nuestro —handshakes innecesarios— y mide el resto con honestidad.
* **Que 8 s sea el plazo correcto.** Es el arranque mientras no hay medidas. El
  plazo correcto es el que salga del p95 del handshake en la red del operador, y
  ése lo dirá el informe LIVE.
* Nada sobre las fórmulas: esta release no toca ninguna.

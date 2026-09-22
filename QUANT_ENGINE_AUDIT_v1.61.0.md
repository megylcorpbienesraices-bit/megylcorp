# AUDITORÍA DEL MOTOR · ITM QUANT v1.61.0

## Veredicto

Esta release **no toca ninguna magnitud**. No cambia una fórmula, un peso, una
autoridad ni un contrato de datos. Reparte alto en pantalla.

Y aun así merece auditoría, porque lo que corrige es un **fallo de entrega**: el
motor calculaba `DELTA / MIN`, `TOTAL` y `VOLUMEN NETO`, el dato viajaba entero
hasta el navegador, y el operador no podía verlo porque su carril medía **un
píxel de alto**. Un número correcto que no llega a la pantalla no vale más que
uno que no se calculó.

---

## 1 · Por qué el CSS es un sitio donde se pierde dato en silencio

Las dos causas comparten una propiedad incómoda: **ninguna de las dos falla**.

```css
.drift-stack { grid-template-rows: <cuatro filas>; }   /* y seis hijos */
```

CSS no tiene concepto de error. Ante seis hijos y cuatro filas declaradas, crea
dos filas implícitas `auto`, las dimensiona al contenido —un `<canvas>` sin alto
intrínseco: cero— y sigue. No hay excepción, no hay aviso, no hay registro. El
panel se dibuja, el navegador queda satisfecho y el carril desaparece.

```css
.grid { display: grid; }      /* gana */
[hidden] { display: none; }   /* pierde */
```

Lo mismo: un elemento marcado como oculto **se ve**, y nada lo reporta.

La consecuencia para esta auditoría es una regla de trabajo: **el reparto de
alto se mide, no se lee**. Las tres pruebas que ya defendían el alto del gráfico
principal leían el CSS y lo daban por bueno; ninguna preguntó nunca cuánto medía
el carril **en el navegador**. La medición que abrió este caso se hizo con
Chromium sobre la terminal en marcha, y es la que hay que repetir.

---

## 2 · Un síntoma perseguido tres veces

El historial del propio fichero lo cuenta sin querer:

```
v1.51.0   suelo del gráfico principal 360 px
v1.52.0   360 -> 520 px   «seguía saliendo corto»
v1.55.0   520 -> 660 px   «seguía saliendo corto»
```

Tres subidas del mismo número, cada una con su razonamiento correcto y su
conclusión equivocada. El gráfico salía pequeño porque las demás filas se
llevaban su fracción —cierto— **y porque dos de ellas no existían** —que nadie
miró—. Subir el suelo del primero no podía arreglar a los que no tenían fila.

La señal de alarma, retrospectivamente, es el patrón: **cuando el mismo número
hay que subirlo tres veces, el número no es el problema**. Es el mismo error de
forma que el bucle de conexión de v1.60.0, en otro material.

---

## 3 · Qué se eligió cuando no cabe todo

Con el suelo de cada carril en píxeles, hay ventanas donde la suma no cabe.
Había dos salidas y no son equivalentes:

* **aplastar** — repartir por fracción y que cada uno mida lo que salga. Es lo
  que había, y su caso límite es el carril de un píxel;
* **desplazar** — que cada carril conserve su altura legible y la pila haga
  scroll.

Se elige desplazar. Un carril de barras por debajo de unos 120 px no comunica lo
que existe para comunicar —la **diferencia** entre barras—, así que reducirlo
por debajo de eso no es enseñar menos: es no enseñar.

---

## 4 · Lo que esta auditoría NO afirma

* **Que la pantalla se vea como la referencia del operador.** Lo medido es el
  reparto de alto, con cifras, antes y después. La apariencia final con datos
  reales, en su monitor y con su resolución, sólo la ve él.
* **Que los carriles tengan datos.** Este cambio les da sitio. Que lleguen datos
  depende del proveedor y del transporte, y el transporte sigue **abierto** a la
  espera de la certificación LIVE en Windows.
* Nada sobre ninguna magnitud: no se ha tocado una sola.

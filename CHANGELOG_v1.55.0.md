# ITM QUANT MULTI ASSET · v1.55.0 — Ausente no es cero

Release: `ITM_QUANT_v1.55.0_PRE_VPS` · Base: `v1.54.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.**

---

## 1 · Un campo que no llega no vale cero

`_f(campo, 0.0) or 0.0` convertía un campo que el proveedor no publica en un
cero. Parece defensivo y es una afirmación sobre el mercado:

| Lo que llegó | Lo que se publicaba | Lo que eso dice |
|---|---|---|
| campo ausente | `0.0` | «este minuto no entró dinero» |
| campo con valor 0 | `0.0` | «este minuto no entró dinero» |

Dos hechos distintos colapsados en el mismo número. En pantalla el cero se
dibuja como una barra al ras del eje y como una curva que se desploma — la
misma sierra que ya apareció en la deriva de volatilidad en v1.51.0.

Corregido en los tres puntos de la cadena de Net Drift (normalizador, motor y el
segundo constructor del runtime), en los niveles de dark pool, en las
estadísticas por contrato y en los prints sin tamaño. **Un cero que sí viene en
la respuesta se sigue publicando como cero**: lo que desaparece es el inventado.

El motor declara además cuántos buckets llegaron sin cada campo, y el
certificador **rechaza** una publicación que ponga un número donde el crudo no
tiene dato. En el gráfico, el hueco **parte el trazo** en vez de hundirlo al eje.

## 2 · Monte Carlo: auditado contra fórmulas cerradas

No se certifica porque «corre». Se contrasta contra la solución exacta:

| Qué | Contra qué | Resultado |
|---|---|---|
| `E[S_T]` | `S₀·e^{(r−q)T}` | 400 k trayectorias, dentro de 0,5 σ |
| Varianza | Lognormal exacta | +0,076 % |
| P(terminar más allá) | `N(d₂)` | 12 casos, todos < 2,1 σ |
| **P(tocar)** | Primer paso con reflexión | 12 casos, todos < 1,2 σ |
| Percentiles | Cuantil lognormal | ≤ 1,4 puntos básicos |
| Convergencia | `1/√N` | ratio 0,90–1,13, sin sesgo |
| Sensibilidad | IV, DTE, r, q | monótona y con el signo correcto |

**La corrección de puente browniano, medida:** con un paso por día y 1 DTE,
contar trayectorias da **17,2 %** donde la respuesta es **34,30 %**. El puente
da **34,27 %**.

**Defecto encontrado durante la auditoría:** producción llamaba **sin semilla**.
Cada refresco sorteaba números nuevos, así que el mismo mercado quieto publicaba
43,8 % y un minuto después 44,2 %. Con 20 000 trayectorias el error típico es
~0,35 pp y un operador no tiene cómo distinguir eso de un movimiento real. La
semilla se deriva ahora **de las entradas**: mismo mercado, mismo número, y
cambia en cuanto cambia el spot, la IV, el DTE o un nivel.

## 3 · El FlowViewModel pasa a mandar

Estaba escrito y probado desde v1.54.0 y la sección no lo consumía. Por eso
convivían en pantalla:

```
ESTADO            DATO ANTIGUO · 405 buckets · último hace 3610 min
PRIMA TOTAL       SIN DATOS
```

Un modelo correcto que nadie lee no corrige nada. Ahora las tarjetas leen del
modelo: un carril viejo escribe su valor y la hora del último dato, y `SIN
DATOS` queda solo para el carril que nunca tuvo nada.

## 4 · El veredicto de cada flecha, con su soporte

Dos marcas que antes se dibujaban idénticas:

```
COMPRA · 96 % dominancia · 91 % con agresor    veredicto sólido
COMPRA · 96 % dominancia ·  7 % con agresor    tres prints de noventa
```

La **dominancia** mide cuánto gana un lado dentro de lo clasificado; la
**cobertura**, qué parte de la prima llegó clasificada. Las dos viajan con la
marca, junto al desglose de prima de compra, de venta y sin lado, y se ven al
pasar el ratón.

## 5 · Ninguna línea anónima, un solo muro

El respaldo del renderer daba color neutro y el nombre interno del motor a un
`kind` desconocido, con lo que la línea pasaba por una más. Ahora se denuncia
como `UNIDENTIFIED_LEVEL`, con recuento en el Auditor y trazo marcado en el
gráfico; un test comprueba que todo `kind` que el renderer sabe dibujar está en
el registro de procedencia.

«Hay una sola autoridad de muros» era una afirmación de arquitectura.
`wall_consistency` la comprueba comparando motor, línea dibujada y tarjeta, y
dos líneas con el mismo nombre cuentan como defecto **aunque coincidan**.

## 6 · Los tres paneles de TRACE se alinean

Comparten una escala de precio, pero se aplica sobre la altura del lienzo de
cada uno, y las cabeceras no medían lo mismo: las laterales llevan un `select`
(~24 px) y la central un `pill` (~20 px). El lienzo central salía cuatro píxeles
más alto y **la barra de DEX del strike 517 no se apoyaba en la línea de 517 del
centro**. Cabecera de altura fija, y TRACE mide su propia alineación en marcha.

## 7 · Interval Map: una rejilla, un renderer principal

v1.51.0 hizo los puntos la vista principal. Con noventa strikes por ciento
sesenta intervalos el punto mide cuatro píxeles y su diámetro deja de informar,
justo en el mapa que existe para contar dónde está la concentración y hacia
dónde migra. El **mapa continuo** pasa a principal; los puntos quedan como vista
**RAW** de diagnóstico, donde siguen siendo insustituibles.

## 8 · Dark Pool: ventana declarada e identidad de ciclo

Los tres carriles no cubren la misma ventana. `NOTIONAL FUERA DE BOLSA` (toda la
sesión) al lado de `OPERACIÓN MAYOR` (cola reciente) invita a dividir uno entre
otro, y esa división no significa nada. Cada carril publica la ventana que
**realmente llegó**, cada KPI dice qué mide y con qué fórmula, y `cycle_id`
distingue «esto es lo de hace diez minutos» de «acaba de llegar y es idéntico».

Las concentraciones salen además como **líneas dibujables** a ambos lados del
precio, con identidad propia — y **no** son muros de opciones: comparten eje de
precio y nada más.

## 9 · El cambio de activo es una transacción

O la pantalla entera es del activo nuevo, o sigue siendo del anterior.
`generation_id` es «SÍMBOLO#época» y el cliente **descarta entera** una respuesta
de otra generación: aprovechar «lo que sirva» es imposible, porque después no se
distingue lo que sirve de lo que no.

## 10 · Un reemplazo de ticker que corrompía rótulos

`str.replace("DIA", sym)` convertía **`MEDIA MÓVIL`** en **`MEQQQ MÓVIL`** y
**`DIARIO`** en **`QQQRIO`**. «DIA» es subcadena de palabras corrientes en
español, así que el defecto afectaba a todos los activos **menos** al único que
se probaba, que era justo el que llevaba el ticker escrito. Reemplazo con borde
de palabra, y con ello desaparece la última comparación contra un ticker que
quedaba en producción.

---

## Lo que NO alcanza esta release

Los puntos 10, 25 y 27 de la corrección integral piden evidencia LIVE contra la
API real con ocho activos. El entorno de desarrollo no tiene credenciales ni
salida a `quantdata.us`. La herramienta existe
(`scripts/verify_live_quantdata.py`) y se ejecuta desde la máquina del operador.

El estado por módulo, con los cinco niveles y sin inflar ninguno, está en
`ESTADO_v1.55.0.md`.

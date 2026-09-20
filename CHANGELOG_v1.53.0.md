# ITM QUANT MULTI ASSET · v1.53.0 — El agresor en producción y el campo visible

Release: `ITM_QUANT_v1.53.0_PRE_VPS` · Base: `v1.52.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.**

Esta release corrige tres cosas que sólo se veían con datos reales.

---

## 1 · Todas las marcas salían en rombo neutro

v1.52.0 hizo lo correcto —no inventar un lado— pero se quedó corta: en producción
el resultado fue que **ninguna** marca tenía lado, y una marca neutra no informa
de nada aunque sea honesta.

La causa: el proveedor **no siempre publica un campo de lado**. Lo que sí publica
en la cinta de opciones es el precio de la operación y el mejor bid/ask del
instante.

**Comparar los dos no es adivinar: es la definición operativa del agresor.**

```
precio >= ask − holgura   →  COMPRA   (cruzó el spread hacia arriba)
precio <= bid + holgura   →  VENTA    (lo cruzó hacia abajo)
entre medias              →  sin agresor
```

La holgura es una **fracción del spread**, no un número en dólares: un céntimo es
mucho en una opción de 0.05 y nada en una de 40.

El campo declarado por el proveedor **siempre gana** al NBBO; éste sólo actúa
cuando no hay ninguno, y el resultado viaja etiquetado como `NBBO` para que se
sepa de dónde salió.

### El umbral estaba pasado de frenada

2:1 (66.7 %) se eligió para no etiquetar ruido. Pero una concentración con el
**65 %** de la prima agredida del lado comprador **es** una concentración
compradora, y llamarla «repartida» esconde información que el operador necesita.
Baja a **60/40**, el corte habitual de sesgo direccional, y la confianza exacta
viaja en el evento: un 61 % y un 95 % no se leen igual aunque los dos digan
COMPRA.

### Y ahora se puede diagnosticar

La cinta publica su **cobertura**: cuántos prints obtuvieron lado, qué porcentaje
y **de qué campo salió cada uno**. Si vuelve a salir todo neutro,
`aggressor_coverage.by_field` dice si el proveedor no manda campo, lo manda con
otro nombre, o son ejecuciones en el medio.

## 2 · El mapa de calor salía macizo en QQQ, SPY y todos los ETF

Y la causa no era el color ni la curva: era **contra qué se normalizaba**.

El Interval Map del proveedor cubre **todo el libro** —noventa strikes que en QQQ
van de 454 a 547—. La ventana de precio de TRACE son unos pocos dólares alrededor
del spot: 708–736. Lo que se ve en pantalla es una franja estrecha de una matriz
muchísimo más ancha.

La normalización es por rango-percentil sobre **toda** la matriz, así que el
percentil de una celda se calculaba contra strikes que ni siquiera están en
pantalla. Los strikes lejanos concentran la exposición extrema, de modo que las
celdas visibles caían todas en el mismo tramo: arriba nada, abajo todo saturado.
**Con más datos, menos contraste** — lo contrario de lo que debería pasar.

Ahora se **recorta al rango visible** (con un 35 % de margen para que el campo no
se corte en seco) y se normaliza después. Verificado inyectando una matriz de 90
strikes en el navegador real:

```
90 strikes  →  13 filas visibles · 77 recortadas
isolíneas: 94  →  210 segmentos
```

## 3 · Un muro fuera de ventana desaparecía

QQQ en 722 publicaba `PUT WALL 700.00` en el KPI y no lo dibujaba en ninguna
parte, así que parecía no existir. Y no se arregla estirando la ventana: un muro
a veinte dólares aplastaría las velas contra una línea.

Se **ancla al borde** con una punta de flecha que dice hacia dónde queda y la
distancia al spot en la etiqueta, con línea más tenue para distinguirlo de uno
que el precio está tocando. Los niveles de dentro conservan prioridad de
etiqueta, y el cupo sube de 6 a 8.

## 4 · Dark Pool: el diagnóstico decía otra cosa que la pantalla

El ViewModel ya leía al proveedor, pero **el Diagnóstico de Paneles seguía
declarando como origen las capas derivadas** —la clasificación por venue y las
zonas de liquidez—, así que con cientos de filas descargadas el Auditor seguía
señalando a la cinta propia como autoridad. Un diagnóstico que no mira la misma
fuente que la vista no sirve para diagnosticar nada.

Ahora son **tres filas independientes**, una por carril del proveedor, cada una
con su estado y su causa. Que `equity_prints` esté en `MARKET_CLOSED` no puede
hacer que `dark_flow` parezca vacío.

Y el modelo publica el **conteo por etapa**:

```
lineage.dark_flow = {provider: 608, view_model: 608, dropped: 0}
```

Si una etapa recorta, se ve dónde y se puede exigir el filtro explícito que lo
justifique.

## 5 · El Interval Map con noventa strikes

Celda de 4.6 px y radio máximo de 2: todos los puntos parecían iguales y el mapa
se leía como una nube de motas. Si el diámetro es la magnitud, hace falta rango
de diámetros. El panel **crece** con los strikes —la misma solución que el perfil
por strike— y el radio máximo tiene suelo.

---

## Límites declarados

- **La vía NBBO no se ha contrastado contra una cinta real**: se verifica valor a
  valor sobre el clasificador. La cobertura publicada es la herramienta para
  comprobarlo en su terminal LIVE.
- **El recorte del campo se verificó inyectando** una matriz de 90 strikes,
  porque en demo el respaldo del motor ya cabe en la ventana.
- **El ZIP es de fuentes, sin certificar.**

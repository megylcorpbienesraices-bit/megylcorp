# ITM QUANT MULTI ASSET · v1.54.0 — Dónde se rompe, y qué no se borra

Release: `ITM_QUANT_v1.54.0_PRE_VPS` · Base: `v1.53.1` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.**

---

## 1 · El agresor: «no lo sé» no era suficiente

v1.53.0 dejó de inventar el lado y las marcas salieron en rombo neutro. Es
honesto **y no accionable**: no decía cuál de los cuatro eslabones fallaba.

La cadena tiene cuatro puntos y cada uno la rompe por su cuenta:

| # | Eslabón | Si falla |
|---|---|---|
| 1 | **La cinta llega** | `TAPE_MISSING` — sin operaciones no hay agresor que medir |
| 2 | **La cinta trae lado** | `TAPE_WITHOUT_SIDE` — ni campo del proveedor ni bid/ask |
| 3 | **La atribución cruza** | `ATTRIBUTION_NO_MATCH` — los relojes no coinciden |
| 4 | **Hay dominancia** | `NO_DOMINANCE` — el flujo estuvo repartido de verdad |

Se señala **el primero** que falla: los de después fallan por consecuencia, y
nombrarlos manda al sitio equivocado. Cada uno lleva su remedio escrito.

En este entorno la cadena sale **sana**, así que su caso no se puede reproducir
aquí — y por eso el diagnóstico es justamente la herramienta para localizarlo en
su terminal.

## 2 · FLUJO: un ciclo vacío ya no borra lo que sí había

La pantalla mostraba a la vez:

```
ESTADO            DATO ANTIGUO · 405 buckets · último hace 3610 min
PRIMA TOTAL       SIN DATOS
PRIMA COMPRADORA  SIN DATOS
```

El estado sabía que había 405 buckets y las tarjetas decían que no había nada.
La causa es de diseño: **un único estado global**. Si el ciclo venía vacío, todo
se vaciaba aunque los carriles siguieran siendo válidos.

Ahora hay cinco estados y **LKG por carril**:

```
DATO NUEVO                      →  LIVE
SIN DATO NUEVO + EXISTE LKG     →  STALE       (+ la hora del último)
MERCADO CERRADO + SESIÓN PREVIA →  HISTORICAL
NUNCA HUBO DATO                 →  NO_DATA     ← el único «SIN DATOS»
EL PROVEEDOR FALLÓ              →  PROVIDER_ERROR
```

La clave del LKG es **`(symbol, session_date, dataset)`**, las tres. Sin
`symbol`, DIA aparecería unos segundos bajo QQQ; sin `session_date`, el cierre de
ayer se vería como de hoy. Un LKG mal indexado enseña un número correcto en el
sitio equivocado, que es peor que no tenerlo.

Si QFLOW está vivo y la cinta vieja, **QFLOW sigue visible**. Y `Net Flow` y
`Tape` son datasets distintos: la ausencia de uno ya no vacía al otro.

Los siete escenarios A–G de la especificación están probados uno a uno.

## 3 · El Scanner, dentro de TRACE

Barra en el encabezado —no una sección nueva:

```
SCANNER  VENTA  80/100   Entrada 534.20 | INVAL 535.62 | OBJ1 533.70 | OBJ2 533.20   ACTIVO
```

Y sus cuatro líneas en el gráfico, con `order: 0`: son las que se miran para
decidir, así que nunca pueden quedarse sin etiqueta por detrás de un centroide.

**El Scanner sigue siendo la única autoridad direccional.** TRACE representa; no
recalcula. Si el Scanner no publica entrada, objetivos o invalidación, el plan
sale `ESPERANDO` y esas líneas **no se dibujan** — derivarlas crearía un segundo
motor direccional, y cuando discrepen nadie sabrá cuál mirar.

**Transaccional**: el plan lleva `thesis_id`, huella del conjunto entero. Si
cambia cualquier pieza, cambia el id y el bloque se sustituye completo. Media
tesis vieja con media nueva parece coherente y no lo es.

Y **sustituye** a `target`/`risk` en vez de convivir: los dos salían del mismo
campo del Scanner, así que se pintaban dos líneas idénticas en el mismo precio
—`OBJ1 533.70` pegada a `OBJ 533.70`—. Un nivel duplicado no es más fuerte: es
la misma información ocupando el sitio de otra.

## 4 · Hover sobre una línea

Nombre, precio, dirección, fuerza, fuente y timestamp. Dirección y fuerza **sólo
si el nivel las trae**: en un muro no significan nada y escribirlas sugeriría que
sí.

## 5 · TRACE más amplio

`min-height` de **640 → 780 px**. El gráfico salía comprimido, con velas y líneas
pegadas justo donde hay que leer la separación.

## 6 · Sesión de Londres desde las 04:00 de Ecuador

`LONDON_MONITOR` a las 04:00 de `America/Guayaquil`, con acumulado propio
—recorrido, máximo, mínimo, VWAP, volumen, primas, prints—, y `NEW_YORK` después.

Al pasar a Nueva York, Londres **se sella, no se borra**: los dos quedan lado a
lado para comparar.

El corte de Nueva York se construye **en hora de Nueva York** y se convierte.
Ecuador no aplica horario de verano y Nueva York sí: una hora UTC fija acertaría
medio año y fallaría el otro medio sin que nada avisara. Hay un test con una
fecha de julio y otra de diciembre que lo fija.

El acumulado **sólo crece con lo medido**: un ciclo sin volumen no es volumen
cero.

---

## Límites declarados

- **El diagnóstico del agresor se prueba en sus cinco estados** con datos
  construidos. Aquí la cadena sale sana, así que su caso concreto no se
  reproduce en este entorno — el diagnóstico es la herramienta para localizarlo
  en el suyo.
- **El `FlowViewModel` existe, está probado y publicado, pero la sección todavía
  lee sus valores por la ruta anterior.** El cableado a la pantalla queda para la
  siguiente entrega; lo digo antes de que lo vea usted.
- **El acumulado de Londres se prueba con relojes construidos.** No se ha podido
  observar una sesión de Londres real aquí.
- **El ZIP es de fuentes, sin certificar.**

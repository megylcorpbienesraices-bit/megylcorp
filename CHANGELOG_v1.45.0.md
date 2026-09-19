# ITM QUANT MULTI ASSET · v1.45.0 — Un dato ausente deja de ser una afirmación

Release: `ITM_QUANT_v1.45.0_PRE_VPS` · Base: `v1.44.0` · Alcance: `MULTI_ASSET`

Esta release **no rehace nada**. Walls, QFLOW, Gamma Migration, Data Hub y la
arquitectura de Quant Data siguen exactamente como quedaron en v1.44.0.

## Matemática del motor

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Ninguna
métrica cambia de valor por esta release. Lo que cambia es qué se puede ver y qué
se declara.

## El patrón común

Los cinco defectos de esta release son el mismo error con cinco caras: **un dato
ausente convertido en una afirmación**.

| Dónde | El dato ausente | La afirmación falsa |
|---|---|---|
| Cliente HTTP | el campo que el 400 nombraba | «Request validation failed», sin más |
| Dark Pool | el flag `offExchange` | «se ejecutó en bolsa» |
| Barras | un valor pequeño pero real | una línea indistinguible del cero |
| Heatmap | celdas bajo el suelo de opacidad | «no hay estructura» |
| Cobertura | que la autoridad había cambiado | «CONTRASTE ACTIVO» |

Ninguno fallaba de forma ruidosa. Todos mentían en silencio.

---

## 1 · Dark Pool Levels · el 400 ya dice qué falta

El cliente leía **sólo** `detail`/`title`, recortado a 180 caracteres:

```python
detail = str(obj.get("detail") or obj.get("title") or "")[:180]
```

El proveedor mandaba el campo concreto que rechazaba —en `errors`, en `detail` como
lista, o en `validationErrors` según la convención— y lo tirábamos nosotros. El
operador leía «Quant Data HTTP 400: Request validation failed»: cierto, inútil, y
sin lo único accionable.

Ahora se extraen los errores por campo (formas Pydantic y alternativas), la
excepción los lleva estructurados, y el diagnóstico los publica.

**Y además se repara.** La herramienta declara variantes del cuerpo; ante un 400 se
prueban en orden, se **recuerda** la que el proveedor acepta, y los ciclos
siguientes hacen una sola petición. El orden de las variantes no es arbitrario:
primero los campos que otras herramientas de equities ya tienen confirmados contra
esta misma cuenta (`limit` de `equity-prints`, `aggregationPeriod` de `dark-flow`).

Un detalle que lo bloqueaba: el aislador de canal **se tragaba la excepción** y
devolvía sólo su texto, así que el clasificador no podía distinguir un 400 reparable
de un 404 definitivo. Ahora la excepción original viaja en el resultado.

> **Nota:** el entorno de desarrollo no tiene acceso a la documentación del
> proveedor (egress bloqueado), así que las variantes son candidatas razonadas, no
> el contrato leído. Si ninguna encaja, el Auditor dirá exactamente qué campo pide.

## 2 · «CONTRASTE ACTIVO» era una etiqueta falsa

`provider_parity` tenía un **segundo** mapa de autoridad escrito a mano:

```python
_NATIVE_CHANNEL_AUTHORITY = {"EXPOSURE": "ITM", "OPEN_INTEREST": "ALPACA",
                             "DARK_POOL": "ALPACA", …}
```

v1.43.0 pasó esas métricas a `QUANTDATA` en `metric_authority`, pero este mapa no se
movió. La interfaz anunciaba a Quant Data como contraste y al núcleo nativo como
autoridad cuando internamente ya mandaba Quant Data.

**La etiqueta mentía, no el comportamiento** — lo comprobé: la procedencia interna
ya era `DIRECT_PROVIDER · QUANTDATA` y sigue siéndolo después de que el motor
consuma el dato.

La corrección no es renombrar: es **derivar**. `channel_authority()` consulta
`metric_authority`, que es donde vive la política. Tener la autoridad escrita en dos
sitios garantiza que un día discrepen — es el mismo defecto que los tres Walls de
v1.44.0.

EXPOSURE, OPEN_INTEREST, IMPLIED_VOLATILITY, OPTION_FLOW y DARK_POOL salen ahora
como **AUTORIDAD PRIMARIA**. Sin datos se declara el respaldo, en vez de llamarlo
«núcleo activo», que sugería que era lo normal.

También se resolvió una contradicción entre registros: `dark_pool_prints` era
`ALPACA` en `metric_authority` y `QUANTDATA` en `data_lineage`. Ahora es
`QUANTDATA` con la cinta como auditoría.

## 3 · PROCEDENCIA no es CARRIL

La columna `ORIGEN` mostraba `MOTOR` cuando el carril del motor había traído el
dato. Eso es **transporte** —y un ahorro de cuota— pero en una columna llamada
«origen» se lee como **autoría**.

Dos columnas, porque son dos preguntas:

- **PROCEDENCIA** — quién produjo el dato: `DIRECT_PROVIDER · QUANTDATA`. Lo sigue
  siendo aunque el motor lo consuma después para derivar inteligencia.
- **CARRIL** — qué lane hizo la petición: compartido o páginas.

Y en las zonas de Dark Pool, `ORIGEN` pasa a `VÍA`: sus valores dicen por qué
**camino** se midió la zona, no de dónde viene el dato.

Además, Net Flow, Net Drift y Order Flow no dejaban **ningún** registro de
procedencia: se consumían desde `terminal_api` sin pasar por el Hub. Los valores
eran correctos; faltaba la trazabilidad. `HUB.flow()` los registra.

## 4 · Dark Pool · ausencia ≠ cero

```python
"off_exchange": bool(_pick(r, "offExchange", "darkPool", "isDarkPool") or False)
```

Si la cuenta no publica esos tres campos exactos, `_pick` devuelve `None`,
`bool(None or False)` es `False`, y **todas** las impresiones quedaban marcadas como
ejecutadas en bolsa. Cientos de prints descargados y la sección en SIN DATOS.

Ahora son **tres estados**: `True`, `False` y `None`. Cuando el proveedor no lo
declara se deduce del centro de ejecución (TRF/FINRA/ADF = fuera de bolsa), y si
tampoco hay venue se admite que **no se sabe**.

Y la sección dice **por qué** está vacía, que no es lo mismo en los tres casos:

- «no llegaron impresiones en este ciclo»
- «llegaron sin clasificar: el proveedor no declara off-exchange y no hay centro de ejecución que interpretar»
- «todas las impresiones se ejecutaron en bolsa»

## 5 · Barras legibles en todos los activos

Tres defectos compartidos por `bars`, `hbars` y los cuatro carriles del flujo:

1. **`Math.max(1, slot * 0.66)`** — slivers de un píxel con muchas categorías.
2. **`Math.max(1, alto)`** — un valor real pequeño, indistinguible del cero.
3. **`peak = max(|v|)`** — un strike dominante, que es lo **normal** en una cadena,
   aplastaba el perfil entero contra cero.

Corrección: suelo de grosor 3 px, suelo de extensión 2.5 px para valores no nulos
—**un cero sigue midiendo cero**—, escala robusta que sólo se comprime cuando un
atípico domina y **marca** la barra recortada, alfa 0.62 → 0.9/0.95 y borde.

Y una consecuencia que **sólo se vio al renderizar**: forzar 3 px con 390 buckets en
330 px los solapa hasta convertir el carril en un bloque sólido. Cuando no caben se
**agrupa**, conservando el valor **extremo** del grupo —no la media, que es un
número que nadie observó— con etiqueta de rango.

## 6 · El heatmap tenía un parámetro por ticker sin saberlo

La normalización era `x / p95`. El campo de exposición tiene cola pesadísima, así
que la mayoría de celdas caía bajo el suelo de opacidad y el mapa se veía vacío. No
le faltaban datos: **le sobraba dinámica para una escala lineal**.

Y el efecto era peor cuanto más concentrada la cadena. Eso es exactamente un
parámetro implícito por activo, que es lo que esta arquitectura evita.

Nueva normalización **por rango**: cada celda se sustituye por el percentil que
ocupa dentro de la matriz, conservando el signo. Medido sobre campos sintéticos:

| Distribución | Lineal | Rango |
|---|---|---|
| Cola pesada (cadena concentrada) | 45 % visible | **64 %** |
| Campo repartido | 97 % visible | **64 %** |

La lineal oscila con el activo; la de rango no. Se publica `filled_ratio` para
saberlo sin mirar la pantalla.

**Qué se conserva y qué no:** el **orden** de las celdas es exacto —si A tiene más
exposición que B, se ve más intensa— pero la intensidad ya no es proporcional a la
magnitud. Para eso está la matriz cruda, que viaja aparte. Un mapa de calor sirve
para ver **dónde** y **cómo se mueve** la concentración; leer magnitudes en él nunca
fue fiable.

## 7 · Mercado cerrado

El Interval Map cae a la **última sesión válida** cuando no hay dato fresco, marcada
por su edad. El orden importa: primero la matriz del motor **si tiene historia de
esta sesión** —un dato propio de ahora vale más que uno ajeno de hace dos días—, y
la última sesión del proveedor después. Lo que no vale nunca es dejar el gráfico
vacío teniendo cualquiera de los dos.

## 8 · El Auditor, aparte

Cobertura por canal, herramientas, cuotas, rutas y diagnósticos **no se eliminan**:
son útiles para auditar. Pasan detrás de un separador, con su propio estilo y
etiquetados `AUDITOR · FUENTES` y `AUDITOR · ARQUITECTURA`, para que nunca se lean
como una pestaña de análisis más.

---

## Validación

`tests/test_v1450_provider_state_and_render.py` — 34 casos. La suite completa pasa
en **1812 casos**.

Validación visual con el programa en ejecución, sobre `tools/visual_harness.html`,
que alimenta los renderers **reales** con cinco activos de escalas deliberadamente
incomparables: SPY (1.4 B), QQQ (900 M), DIA (410 M), XLF (6 M) y SOFI (22 K). Los
cinco se leen igual de bien sin tocar ninguna constante.

> El universo de activos se descubre desde Alpaca en runtime y este entorno sólo
> tiene credenciales para DIA. Sin el banco no habría forma de validar la
> legibilidad multi-activo, que es justo lo que esta release corrige.

## Límites declarados

1. **La validación LIVE sigue PENDIENTE.** Sin credenciales de Quant Data, todos los
   datasets caen a respaldo declarado. `scripts/verify_live_quantdata.py` está listo
   y su lógica probada, pero **no se ha ejecutado contra la API real**. En
   particular, **la reparación del cuerpo de `dark-pool-levels` no se ha confirmado
   contra el proveedor**: las variantes son candidatas razonadas, no el contrato.
2. **El artefacto oficial no se ha generado.** Requiere el toolchain pinado.
3. Ver `QUANT_ENGINE_AUDIT_v1.45.0.md` para el razonamiento de cada decisión.

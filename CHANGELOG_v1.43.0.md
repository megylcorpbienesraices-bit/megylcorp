# ITM QUANT MULTI ASSET · v1.43.0 — Quant Data como autoridad de la estructura de opciones

Release: `ITM_QUANT_v1.43.0_PRE_VPS` · Base: `v1.42.7` · Alcance: `MULTI_ASSET`

## El reparto, escrito

    Alpaca      →  qué está haciendo el precio del subyacente.
    Quant Data  →  cómo está posicionada la estructura de opciones (y sus Greeks).
    ITM QUANT   →  qué significa todo eso junto.

Esto no era una duda de diseño: era una ambigüedad que en v1.42.7 se resolvía sola
y mal. Varias secciones preferían el cálculo propio y dejaban entrar al proveedor
«sólo cuando el motor no tenía nada» —y lo hacían **en silencio**, sin que el
operador ni el Auditor pudieran saber cuál de los dos estaban viendo.

## Matemática del motor · CAMBIO DE FÓRMULA DECLARADO

**No se modifican los pesos del Scanner ni su autoridad direccional.** El Scanner
sigue siendo la única autoridad de dirección, y Net Drift se sigue leyendo del
endpoint oficial sin reconstruirse con nada.

**Lo que sí cambia, y se declara:**

1. **La AUTORIDAD de trece métricas.** `gex`, `dex`, `vex`, `chex`,
   `open_interest`, `interval_map`, `option_greeks`, `iv_rank`, `volatility_skew`,
   `term_structure`, `max_pain`, `dark_flow` y `dark_pool_levels` pasan de `ITM` o
   `ALPACA` a **`QUANTDATA`**, con política de ausencia `DEGRADED`. El número que
   se muestra en esas trece métricas **cambia de origen**, y en algunos casos de
   valor, porque las dos construcciones nunca fueron la misma magnitud.
   *Por qué:* el GEX del proveedor y el del motor tienen distinto universo de
   vencimientos, distinta hipótesis de posicionamiento de dealer y posiblemente
   distinta representación de unidades. Intercambiarlos en silencio, como hacía
   v1.42.7, no era una degradación: era cambiar de definición a mitad de la lectura.
2. **El umbral de concentración de QFLOW.** De `cuantil 0.97 ∧ 25 % del pico` a la
   conjunción de **tres** lentes, añadiendo una z robusta (mediana + MAD).
   *Por qué:* el cuantil 97 de una sesión plana sigue siendo ruido, y sobre ruido
   `share_of_peak` tampoco salva porque el pico también lo es.
3. **Ocho métricas propietarias nuevas**, todas `DERIVED` y con prefijo `ITMQ_`.
   No sustituyen a ninguna existente ni entran en la autoridad direccional.

**Lo que se elimina es duplicación, no inteligencia.** Los cálculos propios que
reconstruían una métrica oficial dejan de publicarse **como si fueran** la métrica
oficial —siguen disponibles en `audit` y `*_engine` para contrastar—; los que
producen lectura se conservan y se amplían.

---

## 1 · Procedencia obligatoria · `app/core/data_lineage.py`

Cada métrica registra `metric`, `symbol`, `provider`, `endpoint`, `source_mode`,
`timestamp`, `raw_value`, `normalized_value`, `final_value`, `fallback_used` y
`derivation`.

Cuatro modos de fuente:

    DIRECT_PROVIDER   el proveedor lo publica y es lo que se muestra
    DERIVED           ITM QUANT lo calcula a partir de otras entradas
    FALLBACK          la primaria no estaba y se usó un respaldo DECLARADO
    UNAVAILABLE       no hay valor; no se publica un cero en su lugar

`guard_primary_source()` **falla** —no avisa— cuando una métrica declarada
`DIRECT_PROVIDER` se intentaría publicar con un valor derivado mientras la fuente
primaria estaba sana. Un `FALLBACK` etiquetado sí está permitido: lo prohibido es
el disfraz, no el respaldo.

Nada de esto se pinta en la pantalla principal. El registro alimenta al **Auditor**.

## 2 · Autoridad invertida · `app/core/metric_authority.py`

| Métrica | Antes | Ahora | Ausencia |
|---|---|---|---|
| `gex` `dex` `vex` `chex` | ITM | **QUANTDATA** | DEGRADED |
| `open_interest` | ALPACA | **QUANTDATA** | DEGRADED |
| `interval_map` `option_greeks` | — | **QUANTDATA** | DEGRADED |
| `iv_rank` `volatility_skew` `term_structure` `max_pain` | ITM | **QUANTDATA** | DEGRADED |
| `dark_flow` `dark_pool_levels` `order_flow` `net_flow` | — | **QUANTDATA** | DEGRADED |
| `underlying_price` `option_quote` `option_chain` | ALPACA | **ALPACA** | FAIL |

La **fusión numérica sigue prohibida**. El GEX del proveedor y el del motor son dos
construcciones distintas; promediarlas produce un número que no describe el libro
de nadie y borra la señal más útil, que es **que difieren**. Ahora se publican las
dos por separado (`audit`, `*_engine`) y se contrastan.

## 3 · Data Hub · `app/core/quant_data_hub.py`

Ruta nueva y explícita:

    Quant Data → Data Hub → interfaz        (sin esperar al motor)
    Quant Data → Data Hub → motor           (en PARALELO, como entrada)

Incluye **capability check por activo**: qué herramientas sirve el proveedor para
*este* símbolo, que es lo que distingue «esta sección no aplica a este activo» de
«esta sección está rota».

Cada bloque lleva su **estado de dato**: `DATA_OK`, `NO_PROVIDER_DATA`,
`FILTERED_ALL`, `PROVIDER_ERROR`, `PARSER_ERROR`, `STALE`. Un cero sólo se muestra
cuando el estado es `DATA_OK` y la aritmética da cero.

## 4 · TRACE · el mapa deja de ser estático

El fondo de TRACE es ahora el **Interval Map** de Quant Data:

    eje X = tiempo · eje Y = strike · intensidad = magnitud de exposición

alternable entre **GAMMA · DELTA · VANNA · CHARM**. Es lo que permite ver cómo la
exposición aparece, aumenta, disminuye y **migra** durante la sesión; un perfil por
strike sólo sabe decir dónde está ahora.

- `norm_interval_map()` lee la forma real del proveedor
  (`{instante_ms: {vencimiento: {strike: {CALL, PUT}}}}`) y publica matrices
  **neta, call y put**. El neto es `call + put`: `putExposure` llega ya firmada.
- Las cuatro matrices viajan en una sola respuesta: cambiar de griega es una
  decisión de lectura, no un viaje de red.
- Las velas se mantienen encima y sobre el **mismo eje temporal**.
- `heatmap_history` del motor pasa a **FALLBACK declarado**, y sigue siendo la única
  fuente de OI neto y volumen neto por intervalo, que el Interval Map no responde.
- Los niveles existentes (Gamma Center, Delta Center, Zero Gamma, Vol Trigger, Call
  Wall, Put Wall…) se conservan, ahora con su procedencia registrada.

## 5 · Flujo de órdenes · una sección, completa

No se crea ninguna sección nueva. La que ya existía reúne las seis piezas:
**Net Flow · Net Drift · Order Flow consolidado · Order Flow sin consolidar ·
QFLOW · concentración QFLOW**.

**QFLOW deja de quedarse en Net Flow.** Net Flow sigue dando la serie base, pero una
concentración sin atribuir es un pico anónimo. Ahora se cruza con
`order-flow/consolidated` y `order-flow/unconsolidated` para decir qué operaciones
la produjeron: `CALL/PUT`, `BUY/SELL`, `strike`, `expiration`, `DTE`, `premium`,
`aggressor`, `BLOCK`/`SWEEP`/`SPLIT` y número de operaciones.

Las concentraciones se marcan **con la misma etiqueta** en el gráfico de precio y en
el panel de flujo: `▲ $4.2M` / `▼ $1.8M`. Se publican además la **línea QFLOW** en
el panel y el **nivel QFLOW** sobre el gráfico principal cuando la concentración es
estructuralmente relevante.

## 6 · Normalización por activo · `app/core/asset_normalization.py`

**No hay ningún umbral fijo en dólares.** «Concentración importante» se mide contra
la distribución del propio activo en su propia sesión, con tres lentes a la vez:

- **cuantil de sesión** — qué es alto hoy;
- **fracción del pico** — protege de la sesión plana que marcaría su propio ruido;
- **z robusta (mediana + MAD)** — no se mueve porque haya cuatro barras enormes.

Se exige la **conjunción**. Pasar una sola lente es fácil y produce falsos eventos.

## 7 · Dark Pool · fuente directa

`dark-flow`, `dark-pool-levels` y `equity-prints` son la vía principal. La
clasificación por `venue` de otra cinta se conserva como **auditoría**, no como
única vía. Si el proveedor tiene datos válidos, la sección no puede quedarse en `0`
por un problema de parser o de venue; y si no los tiene, muestra **SIN DATOS**, no
un cero.

## 8 · El motor, redefinido · `app/core/itmq_intelligence.py`

El motor deja de gastar recursos reconstruyendo lo que el proveedor entrega. Recibe
esos datos en paralelo y produce lo que ningún proveedor puede dar:

`ITMQ_GAMMA_PRESSURE` · `ITMQ_GAMMA_MIGRATION` · `ITMQ_FLOW_CONFLUENCE` ·
`ITMQ_PERSISTENCE` · `ITMQ_DOMINANT_STRIKE` · `ITMQ_BREAK_CONTAINMENT` ·
`ITMQ_REGIME` · `ITMQ_STRUCTURAL_SCORE`

Tres decisiones que merecen nombre:

- **La confluencia mide ACUERDO DE SIGNOS, no suma de magnitudes.** Cinco señales
  con unidades distintas no se suman. Y una señal ausente **pesa cero**: contarla
  como «neutra» rebajaría artificialmente la confluencia de las que sí llegaron.
- **`TRANSITION` se evalúa antes que `BREAK`.** Un «BREAK» de un nivel que la
  migración de gamma ya deshizo es una lectura falsa, y es la que más caro sale.
- **El Structural Score declara su cobertura.** Un score con dos de seis
  componentes no significa lo mismo que uno con seis; presentarlos igual sería la
  forma más silenciosa de mentir con un número redondo.

Separación estricta: todo lo anterior es `DERIVED` y lleva prefijo `ITMQ_`. Ningún
resultado propio se presenta como dato directo del proveedor.

## 9 · La pantalla muestra análisis

No se añade ningún panel técnico. Nombres de proveedor, endpoints, estados de error
y diagnósticos viven en el bloque `auditor` del bundle, marcado
`AUDITOR_ONLY_NEVER_MAIN_SCREEN`.

---

## Certificación

`tests/test_v1430_quant_data_authority.py` ejercita, con **tres activos de escalas
deliberadamente incomparables** y ninguno de ellos el usado para desarrollar:

1. **Autoridad de fuente** — con el proveedor sano, el valor publicado es el suyo:
   se le da al motor un perfil en un strike que el proveedor no publica y se
   comprueba que ese strike **no aparece**.
2. **Fallback honesto** — sin proveedor, el respaldo sostiene la vista etiquetado
   `FALLBACK`; ninguno se hace pasar por dato directo.
3. **Cadena de custodia** — `RAW PROVIDER → NORMALIZER → ENGINE → API INTERNA →
   FRONTEND`, comprobando que el valor y su significado se conservan.
4. **Estados de dato** — los siete casos, y que un fallo nunca sale como `$0.0`.
5. **Multi-activo** — el mismo pipeline completo por los tres activos.
6. **Sin tickers en código** — se rechaza cualquier símbolo escrito como literal en
   los módulos nuevos.
7. **Separación `DIRECT_PROVIDER` / `DERIVED`** — namespaces disjuntos, verificado
   métrica a métrica.

### Límite declarado

La validación de esta release es **funcional y sobre datos controlados**. La
comprobación de renderizado contra un mercado en vivo —TRACE, QFLOW, Net Drift y
Dark Pool dibujando datos reales de sesión— requiere una cuenta de proveedor activa
en horario de mercado y queda fuera de lo que esta suite puede afirmar. El
procedimiento está en `docs/operations/VALIDACION_LIVE_PRE_PRODUCCION_v1.43.0.md`.

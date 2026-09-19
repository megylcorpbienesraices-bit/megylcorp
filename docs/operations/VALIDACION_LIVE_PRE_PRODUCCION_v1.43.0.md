# VALIDACIÓN LIVE PRE-PRODUCCIÓN · v1.43.0

Este documento describe la comprobación que **ninguna suite puede sustituir**: ver el
programa dibujar datos reales con mercado abierto. Sintaxis, lint y tests certifican
que el código hace lo que dice; no certifican que en pantalla se vea lo que debe.

Requisitos: cuenta de Quant Data activa (`QUANTDATA_API_KEY`), feed de subyacente
operativo y mercado abierto. **Repetir con al menos tres activos de comportamiento
distinto** —un índice muy líquido, un ETF sectorial y una acción individual—. Que
funcione en uno no certifica nada.

---

## 1 · TRACE · mapa dinámico bajo el precio

- [ ] El fondo del gráfico central **cambia a lo largo de la sesión**. Si la mancha
      es idéntica de un refresco a otro durante media hora, no es el Interval Map:
      es el respaldo del motor. Comprobar en FUENTES qué está alimentando el mapa.
- [ ] El selector **MAPA DINÁMICO** alterna GAMMA · DELTA · VANNA · CHARM y la
      mancha **cambia de forma** en cada una. Si las cuatro se ven iguales, se está
      dibujando la misma matriz con cuatro etiquetas: es un defecto.
- [ ] Las **velas se mantienen encima** del mapa y están **alineadas**: el borde
      derecho del mapa coincide con la última vela, no se queda atrás ni la
      adelanta.
- [ ] Al hacer zoom o arrastrar, mapa y velas **se mueven juntos**. Si se desfasan,
      los dos ejes temporales no son el mismo.
- [ ] Los niveles —Gamma Center, Delta Center, Zero Gamma, Vol Trigger, Call Wall,
      Put Wall— siguen dibujándose y sus etiquetas no se solapan con el precio.

## 2 · GEX / DEX / VEX / CHEX y Open Interest

- [ ] Los perfiles laterales traen valores con la cadena viva.
- [ ] En el bloque de auditoría, `by_strike_source` debe decir **QUANTDATA** con el
      proveedor sano. Si dice `ITM_QUANT` con el proveedor LIVE, avisar: es
      exactamente el defecto que esta versión corrige.
- [ ] `Open Interest Change` trae valores por strike y **no** coincide con una
      diferencia de volumen. El OI no se reconstruye con volumen.

## 3 · Flujo de órdenes · QFLOW y Net Drift

- [ ] La sección es **una sola**. No debe haber aparecido ninguna sección nueva.
- [ ] Net Drift dibuja CALL acumulado, PUT acumulado, precio sobre el mismo eje y el
      subgráfico de volumen neto. Con el proveedor caído debe decir **SIN DATOS**,
      nunca una curva plana en cero.
- [ ] Cuando haya una concentración, aparece la marca `▲ $X.XM` o `▼ $X.XM`
      **a la vez** sobre el precio y en el panel de flujo, en el mismo instante.
- [ ] Bajo la marca del panel de flujo se lee la atribución: cuántas calls, cuántas
      puts, compras/ventas, etiqueta BLOCK/SWEEP/SPLIT y strike dominante. Si sale
      vacía con la cinta LIVE, el cruce con `order-flow` no está llegando.
- [ ] El **nivel QFLOW** sobre el gráfico principal aparece sólo cuando la
      concentración es relevante. Si aparece siempre, el filtro no está actuando.
- [ ] **Comprobación multi-activo crítica:** el número de concentraciones marcadas
      debe ser comparable entre un índice enorme y una acción pequeña. Si el índice
      marca veinte y la acción ninguna, el umbral se ha vuelto absoluto.

## 4 · Dark Pool

- [ ] Notional, acciones, número de trades y niveles traen valores con el proveedor
      sano.
- [ ] Con el proveedor caído, la sección dice **SIN DATOS**. Un `$0.0` aquí es un
      defecto, no un mercado tranquilo.
- [ ] El bloque de auditoría muestra las dos vías —proveedor y clasificación por
      venue— y su discrepancia. Que no coincidan es información, no una avería.

## 5 · Volatilidad y estadísticas

- [ ] Skew y Term Structure se dibujan con la fuente del proveedor; la lectura
      propia sigue disponible para contrastar.
- [ ] IV Rank muestra el valor del proveedor y, al lado, la lectura propia y su
      divergencia. Una separación grande no es un error: las ventanas no describen
      lo mismo.
- [ ] Las estadísticas son **contexto secundario**: no deben haber desplazado en la
      jerarquía visual a flujo, exposición, dark pool, OI ni volatilidad.

## 6 · La pantalla no es un panel técnico

- [ ] En ninguna pantalla principal se leen nombres de endpoints, rutas `/v1/...`,
      nombres de proveedor en crudo ni códigos de error como `PARSER_ERROR`.
- [ ] Todo eso sí aparece en el **Auditor**, que es donde debe estar.

## 7 · Cambio de activo

- [ ] Al cambiar de símbolo, todas las secciones se vacían y se rellenan con el
      nuevo. Ningún número del activo anterior debe sobrevivir bajo el ticker nuevo.
- [ ] El capability check declara qué herramientas sirve el proveedor para ese
      activo. Una sección vacía porque el activo no tiene esa herramienta debe
      distinguirse de una sección rota.

---

## Qué hacer con un defecto

Copiar la línea del Auditor que corresponde a la métrica afectada: lleva `provider`,
`endpoint`, `source_mode`, `state` y `fallback_used`. Esas cinco cosas dicen si el
problema es del proveedor, del normalizador o del consumidor, y evitan tener que
adivinarlo.

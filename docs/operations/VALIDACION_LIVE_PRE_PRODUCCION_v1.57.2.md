# VALIDACIÓN LIVE PRE-PRODUCCIÓN · v1.57.2

Dos comprobaciones distintas, y hacen falta las dos:

1. **`scripts/verify_live_quantdata.py`** — que el dato llegue del proveedor y
   sobreviva los tres tramos. Automática.
2. **Esta lista** — que lo que se dibuja signifique lo que parece. Manual, con
   mercado abierto.

Ninguna sustituye a la otra: el verificador no sabe si una línea está en el sitio
correcto de la pantalla, y la vista no sabe si el número viene del proveedor o de
un respaldo.

---

## Paso 0 · Verificación automática de fuente

Con `QUANTDATA_API_KEY` configurada y la terminal en marcha:

```
python scripts/verify_live_quantdata.py --ticker SPY --ticker XLF --ticker <una acción> --json informe.json
```

- [ ] Cada dataset configurado como autoridad de Quant Data sale **OK**.
- [ ] Ninguno sale `NO DIRECTO`. Si alguno lo hace, el proveedor respondió pero la
      terminal sirvió un respaldo: **eso es un defecto**, no una limitación.
- [ ] Los que salgan `SIN DATOS` se contrastan con el plan contratado: puede ser
      correcto que una herramienta no esté incluida.
- [ ] La columna MUESTRA trae un número plausible, no `—`.

Repetir con **al menos tres activos de comportamiento distinto**. Que funcione en
uno no certifica nada.

---

## Paso 0-ter · GEOMETRÍA DE LOS PANELES (v1.47.0)

Antes de mirar números, comprobar que se pueden mirar:

```
python tools/visual_regression.py
```

- [ ] `Ningún panel dibuja rayitas, masa sólida ni barras sin separación.`
- [ ] Grosor mínimo ≥ 5 px y ocupación máxima ≤ 1.0 en los 46 paneles.
- [ ] Con la terminal en marcha y mercado abierto, repetir a ojo en
      EXPOSICIÓN, FLUJO DE ÓRDENES, INTERÉS ABIERTO y ESCENARIOS sobre al menos
      tres activos de escalas distintas: las barras deben verse **gruesas y
      separadas**, y al aumentar la densidad el panel debe **agrupar** —la
      etiqueta del eje pasa a decir `514…516`— en vez de adelgazar.
- [ ] Pasar el cursor por una barra agrupada: el `hover` dice el rango, cuántas
      agrupa y su extremo.
- [ ] Cambiar de temporalidad y de ventana: el carril de flujo reagrupa el
      intervalo y **sigue alineado con las velas** de TRACE.
- [ ] Redimensionar la ventana: el primer fotograma tras el resize ya tiene la
      escala correcta; no puede «arreglarse» a los pocos ciclos.

---

## Paso 0-bis · DARK POOL, carril por carril y sobre una cesta (v1.46.0)

Los tres carriles de dark pool son independientes y se verifican por separado.
Medirlos juntos, o sobre un solo activo, es lo que hacía parecer rota la sección
entera teniendo dos de tres sanos:

```
python scripts/verify_live_quantdata.py --dark-pool --json dark_pool.json
```

La cesta por defecto es `DIA SPY QQQ AAPL NVDA TSLA AMD`: tres ETF de escalas
distintas y cuatro equities líquidos. Se puede sustituir con `--ticker`.

- [ ] `Dark Flow`, `Dark Pool Levels` y `Equity Prints` salen **OK** o **SIN
      DATOS**, nunca `CUERPO RECHAZADO`.
- [ ] Si alguno sale `CUERPO RECHAZADO`, la columna DETALLE nombra **el campo
      exacto** que el proveedor rechaza. Ese campo es lo que hay que corregir en
      `_FIELD_DEFAULTS` o en el cuerpo mínimo de la herramienta: **no se reintenta
      y no se prueban variantes**.
- [ ] La última sección del informe dice si un carril falla en **todos** los
      activos —entonces el defecto es del cuerpo que enviamos— o sólo en algunos
      —entonces es de esos activos—. Esa distinción decide dónde mirar.
- [ ] En `Dark Pool Levels` con datos, la línea reporta `preserved_fields` con
      `price`, `notional`, `shares` y `prints`, y `latest_stock_price` con el
      precio de referencia del subyacente.
- [ ] En la terminal, AUDITOR · FUENTES → **DARK POOL · ESTADO POR CARRIL** muestra
      las tres filas con su estado, su causa y qué hacer. La pantalla de DARK POOL
      sigue diciendo sólo SIN DATOS: es correcto, la causa exacta vive en el
      Auditor.
- [ ] Un carril roto **no** apaga a los otros dos: con `dark-pool-levels` en
      `REQUEST_INVALID`, la sección sigue publicando el nocional y la proporción
      de `dark-flow`, y declara `degraded`.

---

## 1 · Call Wall y Put Wall · autoridad única

- [ ] En TRACE aparecen `CALL WALL` y `PUT WALL` como líneas horizontales
      discretas, con su precio en la etiqueta.
- [ ] **Prueba clave:** abre FLUJO DE ÓRDENES y compara. Los dos valores tienen
      que ser **idénticos**, al decimal. Si difieren, la autoridad única se rompió.
- [ ] Abre RESUMEN y compáralos con la tabla de niveles. También idénticos.
- [ ] A lo largo de la sesión, la línea **no debe bailar** de strike cada pocos
      segundos. Si lo hace, la histéresis no está actuando.
- [ ] Cuando el precio **atraviesa** una Wall, el nivel anterior se retira y
      aparece el siguiente. Comprueba que no queda la línea vieja dibujada.
- [ ] El Call Wall nunca aparece por debajo del precio ni el Put Wall por encima.

## 2 · Gamma Migration

- [ ] Cuando la exposición migra, aparece `Γ MIG <origen> → <destino>` a la altura
      del strike que **ganó** exposición, con una flecha desde el que la perdió.
- [ ] El strike señalado coincide con lo que se ve en el mapa de intervalos: la
      mancha debe estar desplazándose hacia ahí.
- [ ] No aparece ninguna tarjeta ni panel nuevo para ella.
- [ ] Si la etiqueta no aparece, comprueba que el strike esté dentro del rango
      visible: fuera de rango **no se dibuja a propósito**, en vez de colocarse en
      un sitio aproximado.

## 3 · QFLOW anclado a la vela

- [ ] Cada marca `▲ $X.XM` / `▼ $X.XM` cae **sobre una vela concreta**, no entre
      dos ni flotando al lado. Amplía el zoom para verificarlo: a máximo zoom la
      marca debe estar centrada sobre su vela.
- [ ] La ▲ se apoya en el **máximo** de la vela y la ▼ en el **mínimo**.
- [ ] Una marca **atenuada** significa que el evento no cayó en ninguna vela (hueco
      de la serie). Es correcto que se vea distinta; si todas salen atenuadas, el
      anclaje no está funcionando.
- [ ] Al pasar el cursor por encima aparece el detalle: cuántas calls y puts,
      compras y ventas, etiqueta BLOCK/SWEEP/SPLIT, strike dominante, vencimiento
      y DTE. Si dice «sin operaciones atribuidas» con la cinta LIVE, el cruce con
      `order-flow` no está llegando.
- [ ] La misma marca, con la misma etiqueta, aparece en el panel de flujo.

## 4 · Marcas que NO deben aparecer todavía

- [ ] En TRACE **no** hay marcas de Confluence, Containment, Break, Divergence ni
      Transition. Si aparece alguna, es una regresión.

## 5 · Resiliencia del Data Hub

- [ ] **Endpoint lento:** con la terminal abierta, observa que el precio y las
      velas siguen actualizándose aunque alguna sección de opciones se quede
      atrás. El precio no puede congelarse por un endpoint de opciones.
- [ ] **Hueco del proveedor:** si una sección pierde datos momentáneamente, debe
      mostrar el último valor conocido con su edad, no vaciarse y reaparecer.
- [ ] **Sin duplicados:** al abrir la terminal, el consumo de cuota no debe
      dispararse. Compruébalo en FUENTES: el contador de peticiones restantes no
      debe caer en bloque al arrancar.
- [ ] **Cortocircuito:** si una herramienta falla de forma repetida, deja de
      intentarse durante un minuto en vez de reintentar cada ciclo.

## 6 · Cambio de símbolo

- [ ] Cambia de activo con la terminal cargada. **Ningún número del activo
      anterior puede sobrevivir** bajo el ticker nuevo, ni siquiera un instante.
- [ ] Presta atención especial a Call Wall y Put Wall: son los que más se notarían.
- [ ] El frontend **no se congela** durante el cambio.
- [ ] Las secciones críticas —exposición, OI, mapa de intervalos, flujo— se llenan
      **antes** que noticias y gainers/losers.
- [ ] Cambia de activo **mientras** una sección está cargando. Cuando la respuesta
      lenta llegue, no debe aparecer bajo el ticker nuevo.

## 7 · La pantalla sigue siendo de análisis

- [ ] En ninguna pantalla principal se leen rutas `/v1/...`, nombres de proveedor
      en crudo ni códigos como `PARSER_ERROR`.
- [ ] Todo eso sí está en FUENTES / Auditor.

---

## Qué hacer con un defecto

Del informe JSON del verificador, copia la fila del dataset afectado: lleva
`endpoint`, `verdict`, `published_mode` y `fallback_used`. De la pantalla, copia la
línea del Auditor de esa métrica. Esas dos cosas juntas dicen si el problema está
en el proveedor, en el normalizador o en el consumidor, sin tener que adivinarlo.

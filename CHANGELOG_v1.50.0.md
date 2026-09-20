# ITM QUANT MULTI ASSET · v1.50.0 — Lo que se mira, y dónde ocurrió

Release: `ITM_QUANT_v1.50.0_PRE_VPS` · Base: `v1.49.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Ninguna
métrica cambia de valor. Todo lo de esta release es **ventana, marca y espacio**.

---

## 1 · La ventana de strikes deja de arrastrar lo que no puede importar

v1.48.0 arregló el grosor: una barra por strike, gruesa, con el panel creciendo.
Lo que quedó al descubierto en cuanto las barras se vieron bien es que el panel
pintaba strikes que **no pueden mover nada**: 433 y 445 con el subyacente en
534. Ocupaban la mitad del eje y comprimían la zona donde sí se opera.

La tentación obvia —y equivocada— era cortar por un número: *«±10 strikes»*,
*«±5 dólares»*. Eso vuelve a romper en cuanto cambia el activo: ±5 dólares es
todo el libro en un ETF de 40 y es ruido en un índice de 5.800.

`app/core/strike_window.py` decide con dos reglas, ambas **relativas**:

| Regla | Qué hace |
|---|---|
| Banda proporcional | `DEFAULT_BAND_PCT = 6.0` sobre el subyacente. El 6 % de 534 son 32 dólares; el 6 % de 5.800 son 348. La misma regla sirve para los dos. |
| Materialidad relativa | Fuera de la banda, una fila se conserva si su magnitud llega a `MATERIAL_FRACTION = 0.18` del máximo **y** destaca `MATERIAL_STANDOUT = 3.0×` sobre la mediana de las lejanas. |

La segunda condición no es decorativa. Con sólo la primera, un perfil **plano**
—todas las filas parecidas— hacía que *cada* strike lejano pareciera material y
no se recortaba nada. Destacar contra la mediana de su propio vecindario es lo
que distingue un muro real de un fondo uniforme.

**Un muro de verdad fuera de la banda nunca se oculta.** Ese es el caso que la
regla existe para proteger, y tiene su propio test.

`MIN_ROWS = 12` impide que un libro corto se quede sin panel. Y el recorte es
**presentación, no dato**: las filas descartadas siguen en el bundle, y el panel
publica `strike_window` con banda, total, conservadas, descartadas y motivo.

## 2 · Dónde ocurrió el flujo: círculo dorado, flecha e importe

Antes, una marca de flujo se pintaba verde o roja según el lado. Eso mezcla dos
cosas: **que hubo un evento** y **hacia dónde iba**. Con la marca entera
coloreada por dirección, un evento grande y uno pequeño del mismo lado se ven
igual, y el ojo tiene que leer el tamaño en vez de verlo.

Ahora, en TRACE y en FLUJO DE ÓRDENES, desde las mismas funciones:

- `flowHalo(ctx, x, y, r)` — círculo **dorado** en el punto exacto del precio y
  el instante donde ocurrió. El dorado dice *aquí pasó algo*.
- `flowArrow(ctx, x, y, up)` — flecha **verde arriba / roja abajo**. La flecha,
  y sólo la flecha, dice compra o venta.
- `flowAmount(ctx, …)` — el importe. Sin adornos: la cifra.

El radio del halo sale de `markerStrength` / `evStrength` contra el **pico del
ciclo** (`S.qflowPeak`, `qPeak`), no contra una constante. Dos marcas del mismo
tamaño en pantalla significan dos eventos del mismo tamaño *en ese ciclo*; con
una referencia absoluta, no significarían nada.

La flecha se corrigió además en geometría: el vértice iba en el lado de la base,
así que apuntaba al revés.

## 3 · TRACE: la disposición definitiva, y es global

Perfil **DEX a la izquierda**, mapa de intervalos continuo con contornos y velas
**al centro**, perfil **GEX a la derecha**. Los tres comparten el eje de precio,
así que una banda del mapa, un nivel del DEX y un nivel del GEX se leen en la
misma horizontal sin cruzarlos a ojo.

No hay tratamiento por activo. No hay `if symbol == …` en ninguna parte del
camino de render — hay un test que lo comprueba sobre el código.

El mapa es **dinámico sobre la liquidez** de la griega seleccionada (gamma,
delta, vanna, charm): campo continuo con relleno de huecos por distancia
inversa, desenfoque gaussiano separable en celdas, normalización por
rango-percentil con signo, suelo de ruido y curva gamma. Es una superficie, no
una tabla pintada.

## 4 · Net Drift: sitio para las tres curvas, y un carril TOTAL

`.drift-stack` pasa a `minmax(0, 2.2fr)`. Con `1fr/.34fr/.38fr` las tres curvas
se aplastaban unas sobre otras y no se distinguía cuál era cuál.

Cada curva lleva **su valor al final del trazo**, con separación de 17 px cuando
dos etiquetas colisionan.

Carril **TOTAL** nuevo (`drawDriftTotal`): la prima **del intervalo** —no el
acumulado—, sumada por contenedor. Lo que destaca se marca en dorado, con el
criterio **explícito y elegible** en la barra de controles:

- `Top 3` — los tres intervalos mayores de la ventana.
- `≥10×` — los que superan diez veces la media.

La media se dibuja como línea de referencia: es contra lo que destacan.

## 5 · Ningún carril se queda mudo

El carril TOTAL de la cinta dibujaba sus ejes y, si ningún contenedor tenía
prima, se quedaba en blanco **sin decir por qué**. Un carril vacío sin causa es
indistinguible de un fallo de render. Ahora declara `SIN PRIMA OBSERVADA`.

---

## Lo que NO cambió

- Ninguna fórmula, ningún peso, ninguna autoridad direccional.
- El recorte de strikes no borra dato: filtra lo que se dibuja.
- Las marcas doradas no crean dato: pintan el flujo ya observado.
- El carril TOTAL **suma**; no promedia ni cancela.

## Límite declarado

**Sigue sin haber un HTTP 200 real de `dark-pool-levels`**: este entorno no tiene
salida a `quantdata.us` ni credenciales. El cuerpo es el del contrato publicado y
el parser cubre la respuesta documentada, pero la certificación LIVE queda
pendiente de `scripts/verify_live_quantdata.py` con la API key real.

Las marcas doradas se validan por test sobre el código de render y por regresión
visual: sin cinta de opciones real en este entorno no hay marcas que fotografiar.

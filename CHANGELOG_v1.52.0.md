# ITM QUANT MULTI ASSET · v1.52.0 — El lado agresor, y el dato que ya estaba

Release: `ITM_QUANT_v1.52.0_PRE_VPS` · Base: `v1.51.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.**
Lo que sí cambia —y hay que leerlo entero— es **la clasificación del lado
agresor**, que estaba produciendo lecturas **invertidas**.

Todo es global: ningún cambio conoce un ticker.

---

## 1 · Había dos formas de marcar una compra como venta

Y las dos estaban activas a la vez.

### 1.1 · `AT_BID` se clasificaba como COMPRA

```python
direction = 1 if side.startswith(("BUY", "ASK", "A")) else \
           -1 if side.startswith(("SELL", "BID", "B")) else 0
```

Ese `"A"` suelto es el fallo. Los proveedores publican el lado como posición
respecto al NBBO, y dos de los valores más frecuentes son:

| Valor | Qué significa | Antes | Ahora |
|---|---|---|---|
| `AT_ASK` | el comprador cruzó el spread | COMPRA ✓ | COMPRA |
| `AT_BID` | **el vendedor cruzó el spread** | **COMPRA ✗** | **VENTA** |
| `ABOVE_BID` | venta agresiva | **COMPRA ✗** | **VENTA** |
| `BELOW_ASK` | compra agresiva | COMPRA ✓ | COMPRA |

Ahora la regla vive en `app/core/aggressor.py`, busca por **subcadena** y nunca
por prefijo corto, y tiene una prueba por cada valor.

### 1.2 · Una PUT comprada se marcaba como VENTA

La marca de flujo decidía así:

```js
if (dir === 'BUY' || dir === 'ASK' || dir === 'CALL') return true;   // compra
if (dir === 'SELL' || dir === 'BID' || dir === 'PUT') return false;  // venta
```

Y el campo que llegaba ahí era `side`, que el motor calcula como:

```python
"side": "CALL" if net > 0 else "PUT" if net < 0    # net = prima call − prima put
```

Eso mide **qué contrato pesó más**, no **quién agredió**. Dos consecuencias:

- Un intervalo dominado por calls puede estar formado íntegramente por calls
  **vendidas** —venta de volatilidad, lectura bajista o neutra— y la pantalla
  dibujaba una flecha **verde de compra** encima.
- **Comprar una put es una compra.** Marcarla como venta por ser put invierte el
  sentido de la operación que se está señalando.

Ahora son dos campos distintos y no se pueden confundir:

```
premium_side   CALL_DOMINANT · PUT_DOMINANT · BALANCED     qué contrato pesó
aggressor      BUY · SELL · MIXED · UNKNOWN                 quién agredió
```

El agresor sale de la **cinta de order-flow**, que ya se cruzaba con cada
concentración: ahí están `buy_premium` y `sell_premium` reales. Hace falta 2:1
en prima agredida para llamarlo compra o venta; por debajo es `MIXED`.

### 1.3 · Sin agresor conocido, no se elige lado

Antes siempre se dibujaba verde o roja, así que un evento sin lado salía pintado
como compra por un valor por defecto. Ahora un lado desconocido se dibuja como
**rombo neutro**. Una flecha inventada sobre un gráfico de operativa puede
costar dinero; un rombo que dice «no sé» no.

---

## 2 · Dark Pool: el dato llegaba y la pantalla decía SIN DATOS

El Auditor mostraba, a la vez que los paneles decían SIN DATOS:

```
Dark Flow         DATO DIRECTO · 608 filas
Dark Pool Levels  DATO DIRECTO · 349 filas
```

Dos causas, las dos de arquitectura.

### 2.1 · La heurística no podía encontrar el campo

`dark-flow` publica las acciones en **`size`**, y el normalizador intentaba
*descubrir* el campo buscando nombres que contuvieran «dark» u «offExchange».
`size` no encaja en ninguno, así que las 608 filas llegaban con
`dark_volume = None`.

La heurística era el error de fondo, no la lista: **cuando existe contrato
publicado, adivinar sólo puede acertar por casualidad.**

```
notionalValue → dark_notional
size          → dark_volume      (acciones off-exchange)
tradeCount    → dark_prints
stockPrice    → stock_price
```

Si una respuesta que debería traer `size` no lo trae, eso **no es cero
acciones**: es `SCHEMA_MISMATCH`, con los campos que sí llegaron.

### 2.2 · Una capa derivada bloqueaba a la fuente directa

Los paneles colgaban de `EQUITY_TAPE + VENUE_CONFIRMED` y de
`ITM_QUANT_LIQUIDITY_ZONES`, que **infieren** el dark pool del campo `venue` de
otra cinta. Mientras esas capas no confirmaran, la sección decía SIN DATOS
aunque el dato directo estuviera descargado.

Ahora hay un **`DarkPoolViewModel`** único, armado desde los tres carriles del
proveedor y nada más. Las capas propias siguen existiendo en `audit`, pero no
deciden si la sección tiene datos.

### 2.3 · Y lo demás de la especificación

- **`equity-prints` pide `sessionDate`** resuelto a la última sesión válida.
  Fuera de horario devolvía vacío y se publicaba como «mercado cerrado, cero
  prints» cuando lo que pasaba es que no se le pedía ninguna sesión.
- **El % fuera de bolsa no se estima.** `dark-flow` sólo trae lo oscuro, así que
  ahí no hay denominador; sin universo completo de prints dark + lit va en
  `SIN DATOS`, nunca en `0%`.
- **VWAP oscuro** = Σ(precio × acciones) / Σ(acciones) sobre prints `DARK_POOL`.
- **Cero y hueco siguen siendo distintos** en toda la ruta del proveedor.

---

## 3 · Relieve: una fila por strike

Con 62 strikes agrupaba y el eje numeraba 540.00, 537.50, 535.00… El strike es
la unidad de lectura aquí también. Ahora el panel **crece** como el de barras y
cada strike tiene su fila y su etiqueta, en secuencia completa.

## 4 · Mapa dinámico: las ocho opciones, verificadas

Ejercitadas una por una en la terminal real:

| Opción | Resultado |
|---|---|
| Gamma · Delta · Charm | campo con isolíneas |
| Γ + Δ · OI neto · Volumen neto | campo con isolíneas |
| Vanna | **sin dato, y lo dice** |
| Apagado | mapa apagado |

Antes, una griega sin mapa del proveedor dejaba el fondo **en blanco sin decir
nada**. Ahora hay respaldo del motor declarado donde existe, y causa escrita
donde no.

## 5 · Cambio de activo

Al cambiar de símbolo se vacía todo y las ~30 herramientas quedan vencidas a la
vez, pero se servían con el ritmo de régimen permanente: 4 por ciclo,
concurrencia 2, 15 s entre ciclos ≈ **dos minutos** hasta tener la pantalla.

Ese ritmo es correcto cuando la pantalla ya está dibujada. No lo es justo
después de un cambio, donde no hay nada. Ahora hay una **ráfaga acotada por los
tres lados**: sólo lo que dibuja la pantalla (prioridad 0 y 1), plazo fijo de
25 s, y se apaga sola en cuanto esa prioridad está servida. Nunca toca la
reserva del motor.

---

## Límites declarados

- **El lado agresor no se ha podido contrastar contra una cinta real aquí.** Se
  verifica valor a valor sobre el clasificador y sobre la cadena del
  normalizador, incluidos los casos que la versión anterior invertía.
- **La cadena de Dark Pool** se comprueba valor a valor con la forma publicada
  del contrato; la ejecución contra el proveedor real queda en su terminal.
- **Con proveedor LIVE**, las cuatro griegas deberían venir del Interval Map en
  vez del respaldo del motor.
- **El ZIP es de fuentes, sin certificar.**

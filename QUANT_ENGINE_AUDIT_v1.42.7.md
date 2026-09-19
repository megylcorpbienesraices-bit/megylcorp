# AUDITORÍA DEL MOTOR · ITM QUANT v1.42.7

## Veredicto

v1.42.0 auditó si las mismas Greeks se usaban en todas partes. v1.42.6 audita algo
distinto: **si el programa sabe cuándo NO tiene por qué haber datos**. No lo sabía, y
eso producía cinco síntomas que parecían averías independientes.

## 0 · v1.42.6 · Un error de reloj que parecía cuatro fallos

`trace_session_bootstrap` resolvía la sesión con `datetime.now(NY).date()`. A la 1:00
del 18 de septiembre pedía barras del **18 de septiembre**:

```
sesión solicitada     2026-09-18   (aún no ha ocurrido)
barras devueltas      0            (respuesta correcta del proveedor)
diagnóstico           sip:NO_BARS; iex:NO_BARS
```

El mismo error explicaba, simultáneamente, `stock-price-over-time`, `equity-prints` y
las exposiciones vacías. Cuatro síntomas, una causa.

`session_resolver` resuelve ahora por calendario. Verificado:

| Hora (NY) | Fase | Sesión resuelta | Motivo |
|---|---|---|---|
| 18/09 01:00 | MARKET_CLOSED | **2026-09-17** | SESSION_NOT_STARTED |
| 18/09 07:00 | PREMARKET | 2026-09-18 | WAITING_FOR_PRINTS |
| 18/09 11:00 | REGULAR | 2026-09-18 | LIVE |
| 18/09 17:00 | AFTERHOURS | 2026-09-18 | MARKET_CLOSED |
| 19/09 11:00 | WEEKEND | 2026-09-18 | MARKET_CLOSED |

La ventana de arranque entrega `['2026-09-15', '2026-09-16', '2026-09-17']`: tres
sesiones completadas, así que **TRACE tiene velas a cualquier hora**.

### 0.1 · El derivador de rutas inventaba URLs

`path_variants` producía hasta doce rutas por herramienta. `/v1/tool/dark-pool-levels`
no existe en ninguna versión de la API del proveedor: la generó la regla «quitar el
segmento intermedio». Como el diagnóstico publicaba el **último** intento, el operador
leía esa URL falsa y concluía que la herramienta no estaba disponible.

Coste medido: 6 peticiones de cuota por herramienta ausente y por ciclo.

Cuatro rutas estaban además mal declaradas, y eran 404 legítimos:

| Herramienta | Declarada | Correcta |
|---|---|---|
| Order Flow | `/v1/options/tool/order-flow` | `/v1/options/tool/order-flow/consolidated` |
| Gainers/Losers | `/v1/equities/tool/gainers-losers` | `/v1/options/tool/gainers-losers` |
| Noticias | `/v1/news` | `/v1/news/tool/news-articles` |
| Prints de equity | `/v1/equities/tool/prints` | `/v1/equities/tool/equity-prints` |

### 0.2 · Dos definiciones de «LIVE» en la misma pantalla

La insignia usaba «configurado y respondiendo»; el contador, «dato fresco en
ventana». De ahí «ALPACA LIVE · QUANTDATA LIVE» junto a «1/2 en vivo».

Ahora el contador se **deriva** de los mismos estados. Verificado en los dos
extremos:

```
madrugada · ALPACA sin ticks desde hace 3600s  → MARKET_CLOSED · cuenta como operativo
sesión    · ALPACA sin ticks desde hace 3600s  → STALE         · NO cuenta
```

La misma antigüedad, dos veredictos, y los dos correctos: lo que cambia es si el
mercado estaba abierto.

### 0.3 · La correlación devolvía NaN donde no existe

`corr = cov/(σx·σy)`. Con σx = 0 el coeficiente no está definido. NumPy devolvía
`nan` y avisaba; el `nan` seguía circulando y, comparado con cualquier umbral,
devolvía `False` **en silencio**, apagando condiciones sin dejar rastro.

Verificado con `warnings.simplefilter("error")`: cero avisos, y el estado publicado
es `UNDEFINED_CONSTANT_SERIES`. Nunca 0, porque «no se puede medir» y «no hay
relación» son afirmaciones distintas.

### 0.4 · Las filas malformadas las producía el escritor

```
scanner_history_dia_2026-09-09.csv · 22 malformadas · 400 válidas
```

Causa: `to_csv(mode="a")` escribe la cabecera con las columnas del **primer** ciclo.
El registro del Scanner añade columnas dinámicas (`feature_*`, `stop_shadow_k_*`), así
que un ciclo posterior con una feature más produce una fila con más campos que la
cabecera — **sin lanzar ningún error**.

Verificado con esquema creciente: 4 filas, 0 malformadas, y las filas antiguas siguen
siendo legibles con el esquema ampliado.

---

## 1 · v1.42.0 · Defectos de arquitectura encontrados y medidos

### 0.1 · Cuatro Gammas presentadas como una

`precision_engine.greeks_vector_for_symbol` despachaba correctamente entre
Black-Scholes y Black-76. Sólo `engine.py` lo usaba. Cinco módulos —`trace_analytics`,
`trace_live`, `market_state_field`, `nextgen_terminal`, `dealer_intelligence`—
importaban la implementación de Black-Scholes directamente.

Medido sobre YM (F = K = 45.000, T = 0,05, σ = 15 %, r = 4,5 %):

| Griega | Black-76 (correcto) | BSM sobre F (lo que hacían) | Desviación |
|---|---|---|---|
| delta | 0,507013 | 0,520019 | +2,57 % |
| gamma | 0,021978 | 0,021981 | +0,01 % |
| vanna | **+0,044505** | **−0,024179** | **signo opuesto** |
| charm | −0,059828 | −0,195963 | ×3,3 |

Gamma casi coincide, y por eso el problema era invisible en una inspección rápida.
Vanna cambia de signo: TRACE y el Scanner publicaban conclusiones contrarias sobre la
misma exposición, con el mismo nombre.

**Corregido**: única puerta `greeks_service`, con test de regresión que prohíbe el
import directo fuera de la implementación. Se eliminó además la segunda
implementación de Black-Scholes que `engine.py` conservaba en su rama de respaldo.

### 0.2 · El multiplicador como literal repetido

Siete módulos escribían `× 100.0` en cifras monetarias. Es correcto para una opción
equity estándar; no lo es para un futuro (YM = 5, MYM = 0,5, ES = 50) ni para un
contrato ajustado por acción corporativa.

Verificado con un contrato ajustado real (raíz OCC `AAPL1`, entregable 62 acciones +
$173,40): el multiplicador correcto es **62**, y la prima de un contrato a $2,00 vale
**$124**, no $200. Con el literal, esa cifra salía plausible y equivocada — que es
peor que salir vacía.

### 0.3 · Unidades de exposición no declaradas

Para DIA a 450, la misma gamma agregada vale 1 (RAW), 450 (por $1) o 2.025 (por 1 %).
Comparar contra Quant Data sin declarar representación produce divergencias del 400 %
que no son divergencias. Ahora la comparación exige siete coincidencias y, si falta
alguna, responde `NOT_COMPARABLE`.

### 0.4 · Calibración medida con la regresión equivocada

La pendiente de calibración se estimaba con mínimos cuadrados sobre el logit. Con `y`
binaria eso devuelve ~0,2 incluso para un modelo perfecto. Corregido a regresión
logística (Cox): el modelo verdadero da **1,067**; el sobreconfiado, **2,268**. Con el
método anterior, el bueno parecía peor calibrado que uno plano.

### 0.5 · Riesgo de ejercicio anticipado, ahora medido

SPY, put K = 450 / S = 380 / T = 1 año / r = 5 % / σ = 20 %:

```
Europea (BSM)            $61,72
Americana (CRR 160)      $71,06
Prima de ejercicio       $ 9,34   →  15,13 % del precio europeo
Diferencia de delta       0,175
```

`MODEL_RISK = ELEVATED`. Para una call sin dividendo la prima es **exactamente cero**
(resultado clásico) y se cribra por razonamiento, sin pagar el árbol.

### 0.6 · Probabilidad de toque: GBM la subestima

Con agrupamiento de volatilidad realista, probabilidad de tocar 455 desde 450 en una
sesión:

```
GBM                   0,305
Jump diffusion        0,310
Block bootstrap       0,519
Regime-conditioned    0,640
```

Casi toda decisión de opciones depende de una probabilidad de toque, no de la
varianza del cierre. GBM conserva la autoridad publicada hasta ganar fuera de
muestra, pero la discrepancia queda registrada.

### 0.7 · Coste de ejecución omitido en el backtest

Ida y vuelta de 10 contratos, spread de $0,20 en ambos extremos:

```
mid-a-mid          $900,00
neto ejecutable    $684,00
arrastre           $216,00   (24,0 %)
```

---

## 2 · Auditoría numérica heredada de v1.41.8 (revalidada)

Cada fórmula contrastada contra su propia definición:

| Sección | Contraste | Resultado |
|---|---|---|
| Rango de sesión | σ = IV·√t, t = min/(252·390) | exacto |
| Ensanchamiento macro | ×1,35 con estrés 100 | exacto |
| Monte Carlo | distancias al spot y porcentajes | exactos |
| Exposure Forecast | flip = cero de la recta interpolada | residuo 10⁻¹⁴ |
| Forma del perfil | concentración, dominante, balance | exactos |
| Interval Map | escala ×10⁶, signo, alineación de filas | exactos |
| IV Rank / Percentile | (iv−mín)/(máx−mín) y proporción ≤ iv | exactos |

**Defecto corregido**: la banda de 2σ del rango de sesión no era exactamente el
doble de la de 1σ publicada, porque cada banda se redondeaba por separado. El
movimiento se redondea ahora una vez y de ahí salen las cuatro.

### 2.1 · Movimiento de las barras GEX/DEX · medido

Repreciando los mismos Greeks contra spots distintos sobre 21 strikes:

* +0,05 USD → **21/21 barras** se mueven, máximo 0,93 %
* +2,50 USD → 21/21, máximo 48,52 %
* Un tick de 0,01 USD mueve el GEX un **0,19 %** del máximo
* El centro de gamma se desplaza +1,58 con el spot +2,00
* **Ninguna barra invierte signo** por un tick (indicaría inestabilidad numérica)

La liquidez de zona entra en GRAVEDAD y como métrica propia, **no en el valor del
GEX**: no cambia cuánta gamma hay en un strike, cambia cuánto pesa ese strike.

### 2.2 · Defecto corregido · exposición por vencimiento en cero

`_metric_totals()` suma `signed_gex_proxy` y `option_delta_exposure_info`. Esas dos
columnas las crea `enrich_options()`; el snapshot crudo no las tiene. A
`build_expiry_intelligence()` y `build_profile_bundle()` se les pasaba el snapshot
crudo, así que `pd.Series(0.0)` sustituía a las columnas ausentes y **gamma, delta,
vanna y charm salían en 0.0 para todos los vencimientos**.

Por qué no se detectó antes: OI y volumen sí existen en el snapshot crudo y
llegaban correctos. El panel parecía tener datos; sólo la gráfica salía plana.

La fórmula no cambia. Lo que cambia es a qué frame se le aplica. Dos pruebas fijan
el comportamiento: una demuestra los ceros con el snapshot crudo, la otra exige los
valores reales con el enriquecido.

### 2.3 · IV Rank e IV Percentile · estadística descriptiva, no señal

Dos números distintos que se confunden a menudo:

* **IV Rank** = (IV − mín) / (máx − mín) · posición dentro del rango observado. Dos
  valores extremos la dominan.
* **IV Percentile** = proporción de observaciones por debajo · robusto a picos
  aislados, describe mejor una distribución sesgada.

Ambos se calculan sobre la IV ATM que **este motor** observó para el mismo
instrumento y ventana de vencimiento. No es un rank de 52 semanas: la ventana es la
que el programa lleva midiendo, y se publica junto al número.

Umbral de 30 observaciones antes de publicar nada. Con rango cero el rank viaja
vacío (dividir por él daría infinito) mientras el percentil sigue siendo legítimo.
Fuera del rango observado el rank se recorta a [0, 100] en lugar de pasarse.

**No entra en Scanner ni en ninguna decisión direccional.** Es contexto.

### 2.4 · Forma del perfil de exposición

Tres estadísticos descriptivos sobre el perfil por strike que el motor ya publica:
concentración en los tres strikes mayores, strike de mayor exposición absoluta, y
reparto de gamma por encima y por debajo del precio. Ninguno introduce matemática
nueva: son agregaciones del mismo perfil que dibuja la gráfica.

Las divisiones están protegidas: un perfil plano devuelve vacío en lugar de
dividir por cero, y sin precio de referencia el reparto arriba/abajo no se publica
aunque la concentración sí.

### 2.5 · Verificación numérica de las Greeks · sin cambios

| Greek | Contraste | Desvío relativo p99 |
|---|---|---|
| Delta | ∂Precio/∂S | 1.1 × 10⁻⁹ |
| Gamma | ∂²Precio/∂S² | 1.8 × 10⁻⁵ |
| Vanna | ∂Δ/∂σ | 6.2 × 10⁻⁹ |
| Charm | ∂Δ/∂t | 5.7 × 10⁻⁹ |
| Speed | ∂Γ/∂S | 4.7 × 10⁻¹⁰ |

Paridad put-call de delta exacta a 1.1 × 10⁻¹⁶. Gamma, vanna y speed idénticas en
call y put. Paridad put-call de Black-76 exacta. Backend acelerado idéntico bit a
bit a la referencia NumPy. Max Pain y Exposure Scenarios verificados contra sus
propias curvas.

## 3 · Lo que el motor NO puede saber

Sin cambios respecto a v1.41.4, y conviene repetirlo:

El motor **no observa el inventario de los dealers**. OPRA publica precio, tamaño y
agresor, no la cuenta que hay detrás. GEX, DEX, VEX y CHEX son **exposiciones proxy
calculadas sobre interés abierto** con la convención habitual del lado cliente.

Lo medible, y que la terminal publica: dónde se concentra la presión de cobertura,
en qué régimen está (AMORTIGUA / AMPLIFICA y el precio donde cambia de signo), y
qué se está negociando ahora con agresor observado.

Lo que **no** se publica porque no se puede medir: el punto exacto donde un dealer
compra o vende. Esa cifra no existe en ninguna fuente conectada.

## Suite

1406 casos en 130 ficheros · PASS · 0 omitidos
`plan_sha256` `32615c17439f44468cf96ba9b669d3bb753f7e74757aaef52496e7846446e4cd`

## 4 · Hotfix de ejecución observado en terminal

La inspección del runtime posterior a la primera pasada de v1.42.6 encontró tres
fallos de integración que no justifican tocar las fórmulas cuantitativas:

- **Bundle institucional:** ausencia de `activity_score` podía convertir el fallback en
  un `int` y llamar `.fillna()` sobre él. Se exige desde ahora fallback vectorial
  alineado al índice.
- **Quant Data intelligence:** `_tf` era referenciado por el catálogo sin definición.
  El helper canónico produce exclusivamente el filtro de ticker esperado por el
  runtime. Cada body del catálogo se ejecuta en regresión para impedir que reaparezca.
- **Frescura de TRACE:** el snapshot de equity elegía `latestTrade` por prioridad fija
  aun cuando el NBBO era posterior. En premarket eso podía convertir un trade de 36 s
  en la edad de toda la estructura y disparar `PUBLICACIÓN RETENIDA`. La fuente de spot
  usa ahora la observación válida con timestamp más reciente; empate conserva trade y
  la ausencia de timestamps conserva el fallback histórico.

No se eleva `UNDERLYING_POLICY` de 20 s, no se modifica GEX/DEX/VEX/CHEX, no se altera
la autoridad métrica y no se fabrican prints de Dark Pool.

Regresión específica añadida: 7 casos. Inventario v1.42.6 actualizado a **1504 casos
en 133 ficheros** con `plan_sha256`
`1e036b5e8b884a72df7678595a219af5164b17d6d576ffef2c12c076de4b7d2f`.



## 5 · Cierre H3

La capa visual y de integración queda cerrada sin cambiar las fórmulas de Gamma/Delta:
TRACE conserva OHLC real con mínimo raster; Flujo de Órdenes usa prima direccional en una
sola unidad; Max Pain / Tiempo usa memoria nativa; Call/Put Wall mantiene autoridad Gamma;
Quant Data es contraste externo con backoff; y los CSV dañados se reparan sólo después de
preservar una copia forense.

Durante la validación se detectó que `AUTO` seleccionaba JAX incluso cuando sólo existía CPU.
Eso no cambiaba la fórmula, pero introducía diferencias de pocos ULP frente a NumPy y violaba
el contrato bit-a-bit de replay/auditoría. `AUTO` usa ahora NumPy en CPU-only; JAX CPU sigue
disponible cuando el operador lo selecciona explícitamente, y GPU/TPU continúan acelerados.

Suite final H3: **1520 casos / 134 ficheros · 1520 PASS · 0 SKIP · 0 FAIL · 0 ERROR · 21/21**.
`plan_sha256` `2fd05de190b4bc66fe07f77e273276466548dbc277d7fe60ba8db2928af4ca96`.


## H4 · Auditoría de resiliencia LIVE equities

- Suite/inventario objetivo de este cierre: **1531 casos / 135 ficheros · 21 particiones**.
- `plan_sha256`: `670ffc65a06423c2fb09b2d1bc0547274835fd5ca8db750291e99f6773458a22`.
- Frescura OPRA corregida para elegir el evento más reciente trade/quote.
- Gate de frescura desacoplado de visibilidad: estructura retenida = contexto no accionable, no pantalla vacía.
- Flujo direccional protegido contra clipping de escala.
- Dark Pool conserva cobertura de precio de sesión incluso con fabric corta.
- `fsync(directory)` no se intenta en Windows.
- Invariante de Walls aplicada también contra el spot LIVE del frontend.
- Quant Data de páginas limitado a concurrencia 2; autoridad del núcleo ITM/Alpaca sin cambios.


---

# v1.42.7 · Net Drift oficial

## Qué se audita aquí

Esta release **no cambia ninguna fórmula del motor**. Lo que se audita es una
frontera distinta: **de quién es el dato**.

Net Drift es un dato del proveedor, no un cálculo de ITM QUANT. La tentación
—reconstruirlo con GEX, DEX, Net Flow o una combinación propia cuando el endpoint no
responde— produce una curva que en pantalla es indistinguible de la real y que el
operador leería como Net Drift sin serlo. Por eso la regla es absoluta:

> **Si `POST /v1/options/tool/net-drift` no entrega, la sección dice SIN DATOS.**

Lo único que ITM QUANT añade sobre el dato del proveedor es una **suma corrida**, y
esa suma se certifica contra el crudo en vez de darse por buena.

## El signo

`netPutPremium` llega ya firmado. La prima neta del intervalo es `call + put`.
Restarlo invertiría la dirección de la sesión entera: una sesión que el proveedor
describe como vendedora se dibujaría compradora. Hay un test dedicado a este signo
porque es el error que más fácilmente pasa inadvertido —la curva sigue teniendo
forma de curva—.

## El bucket abierto

Es el otro sitio por donde una curva acumulada se corrompe sin avisar. El proveedor
republica el último bucket con valores mayores en cada refresco; sumar
incrementalmente lo contaría tantas veces como refrescos hubiera, y la sesión
terminaría con un múltiplo del valor real. La curva se reconstruye entera en cada
ciclo, y dentro de una misma respuesta un instante repetido gana el último.

## Lo que este informe no puede afirmar

Sin credenciales del proveedor, la certificación se hace contra la **forma** de la
respuesta documentada por el propio código de producción, no contra una sesión en
vivo. La comparación de varios puntos contra la respuesta cruda real es un paso que
debe ejecutarse en el VPS con clave; `certify_against_raw()` existe exactamente para
poder hacerlo sobre el payload en vivo sin escribir nada nuevo.

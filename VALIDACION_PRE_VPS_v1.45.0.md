# VALIDACIÓN PRE-VPS · ITM QUANT v1.45.0

Release: `ITM_QUANT_v1.45.0_PRE_VPS` · Alcance: `MULTI_ASSET`

> Documento acumulativo. La sección **A9** es la de esta release.

## Suite

- Inventario nominal: **1812 casos / 145 ficheros**
- Particiones: 21 · `plan_sha256`: `c094d3be6c4e2cf70de4204d6d03b391c7a7f83f9267bb9cc8f57adbbab4533b`
- Resultado: **PASS**

---

## A9 · v1.45.0 · Estado real del proveedor y legibilidad multi-activo

### Qué se certifica

`tests/test_v1450_provider_state_and_render.py` — 34 casos en siete bloques.

**1 · El 400 dice qué falta.** Tres convenciones de error de validación, y que el
titular no se duplique como si fuera un error de campo. Que un 400/422 sea
reparable y un 404 no. Que el cuerpo se repare **una vez** y se recuerde: el
segundo ciclo hace una sola petición. Que el aislador de canal propague la
excepción original, sin la cual el clasificador no puede distinguir los dos casos.

**2 · La etiqueta de autoridad.** Que los cinco canales cuya política es
`QUANTDATA` se declaren AUTORIDAD PRIMARIA, que la autoridad se **derive** y no se
escriba por segunda vez, y que los dos registros coincidan sobre
`dark_pool_prints`.

**3 · Procedencia ≠ carril.** Que GEX, DEX, VEX, CHEX, OI y Net Flow sigan siendo
`DIRECT_PROVIDER · QUANTDATA` **después** de que el motor los consuma, y que lo
propio siga siendo `DERIVED`.

**4 · Dark Pool.** Que un flag ausente sea `None` y no `False`; que el centro de
ejecución clasifique cuando el proveedor no declara; que el flag del proveedor gane
sobre la deducción; y que la sección diga **por qué** está vacía en los tres casos
distintos.

**5 · Legibilidad multi-activo.** Suelos que no inventan un cero, escala robusta
con marca de recorte, agrupación en vez de solape, y —la prueba que más importa—
que el heatmap llene **lo mismo** con una distribución de cola pesada y con una
repartida, mientras la lineal se diferencia en más de 25 puntos.

**6 · Mercado cerrado.** Que el Interval Map caiga a la última sesión válida
marcada, y que una matriz fresca del motor gane a una del proveedor de hace días.

**7 · El Auditor aparte.** Que los diagnósticos estén separados de las pestañas de
análisis **sin eliminarse**.

### Verificación visual

Programa en ejecución, renderizado en Chromium sobre `tools/visual_harness.html`,
que alimenta los renderers reales con SPY (1.4 B), QQQ (900 M), DIA (410 M),
XLF (6 M) y SOFI (22 K):

- Perfil por strike: los cinco muestran la forma completa del perfil, no una barra
  dominante y cuarenta invisibles.
- Carril de 120 buckets: barras individuales legibles en los cinco.
- Carril de 390 buckets: agrupado, con etiquetas de rango, separado — no el bloque
  sólido que producía el suelo de 3 px sin agrupación.
- Cero errores de consola.

> El universo se descubre desde Alpaca en runtime y este entorno sólo tiene
> credenciales para DIA. El banco es la única vía de validar la legibilidad
> multi-activo, que es justo lo que esta release corrige.

### Límites declarados

1. **La validación LIVE sigue PENDIENTE**, y con un punto nuevo: **la reparación
   del cuerpo de `dark-pool-levels` no se ha confirmado contra el proveedor**. El
   egress a su documentación está bloqueado en este entorno, así que las variantes
   son candidatas razonadas, no el contrato leído. Si ninguna encaja, el Auditor
   dirá qué campo pide — pero eso sólo se sabrá ejecutándolo.
2. **Que Dark Pool tenga datos** no se puede afirmar: se corrigió la clasificación
   que los perdía; si hay prints off-exchange sólo lo dice una sesión real.
3. **El artefacto oficial no se ha generado.** Requiere el toolchain pinado.

---

# Histórico · v1.42.7 y anteriores

## Hotfix runtime posterior a la primera certificación

Sobre el **mismo artefacto v1.42.6** se fijaron tres fallos observados en ejecución real,
sin cambiar la autoridad métrica ni las fórmulas de Gamma/Delta:

- `flow_pro_summary()` ya no puede derribar `/api/terminal/bundle` cuando un frame no
  trae `activity_score`: el fallback es una `Series` alineada al índice, nunca un escalar.
  La misma guarda se aplica al resumen/figura y al tape de actividad de Londres.
- El catálogo de `quantdata_intelligence` recupera el constructor canónico `_tf(ticker)`
  (`{"filter": {"ticker": ...}}`). GEX/DEX/VEX/CHEX por vencimiento y Order Flow ya no
  fallan por `NameError: _tf is not defined`.
- En premarket, el precio subyacente del snapshot elige la **observación válida más
  reciente** entre último trade y midpoint NBBO. Así una operación antigua no marca
  falsamente como vieja una cotización que sí está actualizada. El límite de frescura
  de 20 s **no se amplió ni se maquilló**.

Se conserva la semántica de ausencia real de datos: que Dark Pool no tenga prints en
un momento dado no fabrica datos ni fuerza un estado LIVE.

### HOTFIX2 · bloqueo intermitente de TRACE observado en ejecución real

- PREMARKET ejecuta el ciclo estructural pesado cada 45 s, mientras el contrato de
  frescura del subyacente permanece en 20 s. El gate ya no usa el timestamp envejecido
  del último snapshot pesado para decidir si el precio está vivo: toma el evento de
  precio válido más reciente ya recibido por `PROVIDER_BUS`/`PRICE_TICK_FABRIC` y
  conserva por separado el timestamp estructural para auditoría. No se amplió el límite.
- Se reproduce y corrige `RuntimeWarning: Mean of empty slice` de TRACE: una cinta
  completamente plana generaba eficiencia cero en todas las velas y luego llamaba a
  `median()` después de convertir esos ceros a NaN. Ahora la mediana sólo se calcula
  cuando existe muestra finita; la ausencia se declara explícitamente.
- Las superficies y cortes que aplican el gate consultan la misma evaluación viva; ya
  no existe una copia estática del gate capaz de apagar perfiles/heatmap/niveles entre
  dos refresh estructurales.

Doce pruebas nuevas fijan las regresiones acumuladas de v1.42.6/HOTFIX1/HOTFIX2. Inventario actual: **1509 casos / 133
ficheros**.

### H3 · cierre visual, semántico y de resiliencia

- TRACE conserva el mismo OHLC y escala estructural, pero aplica un mínimo exclusivamente
  raster al cuerpo/mecha para que una variación de centavos no desaparezca entre strikes.
- Flujo de Órdenes separa unidades: la banda central es prima direccional `BUY-SELL`;
  el volumen firmado del subyacente queda auditado aparte. Los prints grandes se agrupan
  por minuto y se anclan a la curva del subyacente, evitando nubes doradas desordenadas.
- Max Pain / Tiempo tiene autoridad nativa ITM: Session Memory y, al actualizar una sesión
  antigua, Chain History. Una sola observación se dibuja como punto; con historia forma línea.
- Call Wall y Put Wall significan únicamente walls por Gamma de cada lado. Los máximos de OI
  se muestran como `Mayor Call OI` y `Mayor Put OI`, sin reutilizar el nombre Wall.
- FUENTES distingue núcleo/autoritativo de contraste Quant Data. Un `QD 0/N` ya no se presenta
  como si la exposición u OI del motor valieran cero. ARQUITECTURA etiqueta los fallbacks como
  política contractual, no como estado LIVE.
- Los timeouts de Quant Data son transitorios: backoff exponencial, reserva del motor intacta
  y reintento visible; no se inventa una ruta inválida ni se consume cuota en bucle.
- Un CSV histórico malformado se copia íntegro a cuarentena y el activo se reescribe de forma
  atómica con las filas válidas, por lo que el mismo aviso no reaparece en cada arranque.
- El selector de aceleración `AUTO` ya no elige JAX cuando sólo existe CPU. En ese caso usa
  NumPy para conservar igualdad bit-a-bit con replay/auditoría; JAX CPU sigue disponible si se
  solicita explícitamente. No cambia ninguna fórmula de Greeks.

Once regresiones nuevas fijan estas rutas. Inventario: **1531 casos / 135 ficheros**.

## Certificación en cuatro niveles

`tests/test_v1420_universal_quantitative_truth.py` certifica cada pieza nueva en los
cuatro niveles que exige el contrato de esta versión, marcados en el nombre del test:

| Nivel | Qué demuestra | Ejemplo |
|---|---|---|
| `unit` | la fórmula está implementada como dice | parsing OSI y resolución de multiplicador |
| `golden` | coincide con una referencia independiente | vega contra diferencia finita del precio |
| `property` | cumple los invariantes que DEBE cumplir | americana ≥ europea; notional lineal |
| `economic` | distingue lo que dice distinguir | el challenger verdadero gana, el plano nunca |

## **Una advertencia importante: este release SÍ cambia números**

A diferencia de las anteriores, v1.42.6 **modifica cifras publicadas**. No es una
refactorización neutra y no debe presentarse como tal:

1. **Greeks de futuros e índices en TRACE, Dealer, Market State y NextGen.** Antes
   usaban Black-Scholes sobre el precio del futuro; ahora usan Black-76. En YM la
   delta se desplaza un 2,6 % y la **vanna cambia de signo**. La cifra anterior era
   la equivocada, pero si tiene capturas o notas previas, no coincidirán.
2. **Cifras monetarias de futuros y contratos ajustados.** Antes multiplicaban por
   100; ahora por el multiplicador real (YM = 5, MYM = 0,5, ajustados = su
   entregable). Para DIA, SPY, QQQ y cualquier equity/ETF estándar **no cambia nada**.
3. **Comparaciones entre proveedores.** Donde antes aparecía una media ponderada de
   GEX o IV, ahora aparece la cifra de la autoridad declarada y, si procede, un
   `NOT_COMPARABLE` explicando por qué no se pueden enfrentar.
4. **Pendiente de calibración.** El valor cambia de escala al pasar de OLS a Cox. Una
   pendiente de 1,07 en v1.42 es mejor calibración que un 0,21 de v1.41; no son
   comparables entre versiones.

Para un ETF como DIA operado normalmente, el panel se verá **igual** salvo por las
secciones nuevas. Las diferencias se concentran en futuros, índices y contratos
ajustados, que es donde estaba el defecto.

## Análisis estático de la interfaz

`bash scripts/lint_frontend.sh` → obligatorio antes de publicar: `node --check` no
detecta una variable no declarada, y eso es exactamente lo que deja un panel en
RENDER NO DISPONIBLE.

## Configuración obligatoria antes de subir al VPS

Declarar el plan real de Quant Data en `.env`. Sin esto la cadencia asume la ventana
**diaria**, que es correcta pero lenta si su plan es más amplio:

```
QUANTDATA_PLAN_REQUESTS=240
QUANTDATA_PLAN_WINDOW_SECONDS=86400
```

Roster de proveedores activo (por defecto Alpaca + Quant Data):

```
ITM_OPTIONS_PEERS=ALPACA,QUANTDATA
```

## Comprobación de contratos en caliente

Tras arrancar, `GET /api/architecture` debe responder con:

- `scope: MULTI_ASSET`
- `pricing_dispatch.YM.option_model = FUTURE_OPTION`
- `pricing_dispatch.DJX.option_model = INDEX_OPTION`
- `pricing_dispatch.VIX.option_model = UNSUPPORTED` (es correcto: las VIX estándar son
  cash-settled sobre VRO y requieren curva forward)
- `metric_authority.by_provider.QUANTDATA.authority` incluye `net_drift`

Si `dispatcher_usage` queda vacío tras varios ciclos, ninguna sección está pasando por
la puerta única y hay que investigarlo antes de operar.

## Historial de opciones para backtesting

Desde esta versión, cada ciclo LIVE archiva el snapshot OPRA observado en
`<persistent>/replay/option_snapshots/<SÍMBOLO>/<fecha>.jsonl`. **Ese histórico no se
puede reconstruir a posteriori**: el backtest realista depende de haber grabado bid y
ask en su momento. Cuanto antes esté el VPS en marcha, antes habrá histórico usable.

## Autonomía de red

La terminal principal no carga recursos externos en su ruta crítica. Las series macro
(FRED) y los proveedores de mercado sí requieren salida a internet, como es lógico.

## Lo que esta versión no promete

Microestructura de market maker real. Sin libro a nivel de orden no se pueden estudiar
cola, llegadas, cancelaciones, reposición, probabilidad de ejecución ni selección
adversa. Alpaca y Quant Data no entregan eso, y simularlo y llamarlo L3 sería mentir.


## v1.42.6 · Comprobación en caliente de la verdad de sesión

Tras arrancar, y **a cualquier hora**, la pestaña TRACE debe mostrar velas. Si está
vacía fuera de sesión, el bootstrap histórico está fallando de verdad y el
diagnóstico lo dirá con esas palabras en lugar de `sip:NO_BARS`.

En el panel FUENTES, comprobar:

- la cabecera dice `N/N operativos` y **ninguna** fila contradice ese número;
- de madrugada los proveedores aparecen como `MARKET_CLOSED`, no como caídos;
- las calidades sin medir dicen `N/A`, no `0.0`;
- ninguna herramienta muestra más de una ruta probada.

Si una herramienta aparece como `RUTA_INVALIDA`, el mensaje nombra la ruta canónica:
o su plan de Quant Data no la incluye, o el proveedor la renombró y hay que
actualizar el registro. No hay una tercera posibilidad y no se prueban alternativas.

## Voz de Sophia

`GET /api/sophia/capabilities` declara qué hay instalado. En una máquina sin
faster-whisper ni whisper.cpp, `stt` es `false`, el micrófono aparece deshabilitado y
**no se envía ninguna petición de transcripción**. Para habilitar el dictado:

```
pip install faster-whisper
```

o declarar la ruta del binario en `ITM_WHISPER_CLI`. La síntesis de voz usa SAPI en
Windows (viene con el sistema) o Piper vía `ITM_PIPER_BIN`.


## H4 · continuidad LIVE de equities

- Suite final sobre el árbol H4: **1549 PASS · 0 SKIP · 0 FAIL · 0 ERROR · 21/21 particiones**.
- Inventario exacto: **1633 casos / 141 ficheros**.
- Particiones: **21**.
- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.
- OPRA: el reloj de frescura usa el evento más nuevo entre trade y NBBO quote por contrato.
- Un snapshot estructural fuera de SLA ya no borra TRACE/OI/EXPOSICIÓN: queda visible como **contexto retenido no accionable** mientras precio y flujo observados siguen LIVE.
- El carril neto/total de Flujo escala al pico visible real para que una ráfaga no se recorte fuera del canvas.
- En Windows se conserva `fsync(file) → os.replace()` y se omite `fsync(directory)`, que NTFS no expone mediante `os.open` de directorios.
- Call Wall sólo se presenta por encima del spot LIVE y Put Wall sólo por debajo; si el precio cruza un muro entre snapshots, se oculta hasta el recálculo, sin inventar otro nivel.
- Quant Data páginas queda limitado a dos requests simultáneos y nunca gobierna el núcleo de equities.
- Alcance operativo actual solicitado: **equities/ETF**. La ausencia de proveedor de futuros no degrada DIA ni sus opciones.

- Dark Pool deja de rebautizar cualquier large print como dark pool: sólo muestra off-exchange confirmado por venue/tape (o dato externo que lo declare explícitamente).


## A1 · Auditoría cuantitativa independiente (v1.42.6)

Revisión de las fórmulas del motor contra referencias **externas al propio código**
—formas cerradas publicadas, identidades exactas y diferencias finitas—, nunca
contra el motor consigo mismo. Un motor internamente coherente puede estar
coherentemente equivocado, y tres de los siete hallazgos lo estaban con la suite
en verde.

El detalle completo, con las cifras medidas, está en `CHANGELOG_v1.42.6.md`.

### Verificado correcto · sin cambios

| Área | Referencia externa | Resultado |
|---|---|---|
| Greeks BS y Black-76 (δ, Γ, vanna, charm, speed) | diferencias finitas + forma cerrada independiente | error rel. **0.0** |
| Paridad put-call BS y B76 | identidad exacta | **1e-14** |
| GEX `sign·Γ·OI·mult·S²·0.01` | los 9 módulos que la calculan | idéntica en todos |
| DEX `Δ·OI·mult·S` | idem | idéntica en todos |
| Isotónica PAVA (calibración) | óptimo por fuerza bruta SLSQP | **2.8e-14** |
| Platt scaling | MLE independiente Nelder-Mead | coincide |
| SVI, SSVI, Durrleman g, Gatheral-Jacquier | formulación publicada | correctas |
| Reloj ACT/365 vs reloj de sesión | separación explícita | correcta |
| Gamma flip `NetGEX(S)=0` | raíz verificada numéricamente | es raíz real |
| Max pain · EV en R · breakeven `(1+c)/(RR+1)` | derivación algebraica | correctas |

### Corregido

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F1 | **CRÍTICA** | Monte Carlo: `prob_touch_by_expiry_pct` contaba cruces sobre malla discreta. Con 1 paso/día un 0DTE tenía UN tramo, así que «tocar» degeneraba en «terminar más allá». | **17.30 % → 34.18 %** (exacto 34.30 %) |
| F7 | **CRÍTICA** | `Cargo.lock` fijaba 1.42.0 con `Cargo.toml` en 1.42.1, en los dos crates. `cargo check --locked` —el comando del gate y del CI— fallaba con exit 101. El artefacto no podía desplegarse por su propio CI. | exit 101 → **PASS** |
| F5 | **ALTA** | `if f(lo)==0: return lo` publicaba **IV = 0.5 %** cuando el precio era indistinguible del de vol cero. Como Γ ∝ 1/σ, esa IV inventada inflaba la gamma y contaminaba GEX → Call/Put Wall → Flip. | número inventado → **NaN declarado** |
| F6 | **ALTA** | El adelanto de DXLink en `/api/asset/select` guardaba la Task sólo en una variable local. El bucle de eventos sólo mantiene referencias **débiles**: la tarea podía morir a media ejecución al retornar el handler, dejando el cambio de activo sin failover preparado y sin ningún error visible. | referencia fuerte + `add_done_callback` |
| F3 | MEDIA | IV no recuperada en contratos muy ITM: se perdía el contrato entero de la cadena. | paridad put-call → **100 % de los invertibles** |
| F2 | RENDIMIENTO | El barrido del gamma flip evaluaba 4 mallas de las que sólo 2 eran distintas. | **14.20 ms → 8.12 ms (−43 %)** por snapshot |
| F4 | HIGIENE | 77 importaciones y 6 variables muertas en `app/` y `scripts/`. | eliminadas |

F1 y F5 se refuerzan entre sí: el Monte Carlo respondía «¿llega al Call Wall?» con
la mitad de la probabilidad real, sobre un Call Wall que podía estar colocado con
gammas infladas por IVs inventadas. Ninguno era visible desde dentro del motor,
porque ambos producían números perfectamente plausibles.

### Estado final

    FULL RELEASE GATE PASS · v1.42.6 · PRE-VPS
    SUITE PARTICIONADA: 1551 passed · 0 skipped · 0 failed · 0 errors · 21/21 particiones

- Regresiones fijadas en `tests/test_v1422_quant_audit_corrections.py` (20 casos).
- `cargo check --locked` PASS en `causality_engine` y `wasm_bridge`.
- JavaScript: 25/25 ficheros validados.
- Locks instalados con `--require-hashes` y `pip check` limpio.
- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A2 · Auditoría por secciones (v1.42.6)

Ejecución real, no lectura: cadena sintética con respuestas sembradas, 62 endpoints
GET golpeados y el diagnóstico propio del programa como juez.

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F8 | **ALTA** | Call/Put Wall ponderados por gamma al spot: el argmax se iba al ATM por construcción. A 1 DTE hacían falta 40× el OI ATM para ver el Call Wall y el Put Wall no aparecía nunca; ambos salían a ~0.2σ del precio. | gamma en S=K → **12/12 escenarios detectados** |
| F9 | **ALTA** | `frame.get("col",0).fillna()` devuelve un escalar si falta la columna: la guarda defensiva era la que lanzaba `AttributeError`. Tumbaba TRACE (ticks sin `signed_volume`) y DARK POOL (tape sin `notional`/`conditions`). | **112 sitios** + test de patrón |

Verificación funcional:

- 62 endpoints GET: **0 fallos 5xx, 0 excepciones** (3 códigos 4xx son validación correcta).
- Diagnóstico por panel del propio programa: **9/9 CON DATOS**.
- `key_levels_report` == `structure_levels` en Call Wall y Put Wall.
- Parkinson, volatilidad forward, detección off-exchange y heatmap: verificados correctos, sin cambios.

Pendiente declarado: Vanna/Charm/Speed conviven con unidades distintas por sección
(`proxy` sin escalar frente a `$M`). Etiquetados y no incorrectos, pero un mismo
nombre con dos escalas invita a comparar lo que no es comparable. `units_registry`
ya tiene los conceptos y ninguna de las dos rutas lo usa.

- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A3 · Defectos reportados en producción (v1.42.6)

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F10 | **ALTA** | El heatmap publicaba sólo los últimos 120 snapshots: con cadencia de ~45 s, el 30 % de la sesión. | **30 % → 99.2 %** de cobertura, mismo payload |
| F11 | **ALTA** | El lienzo estiraba la imagen del heatmap al rectángulo completo, sin usar las coordenadas que representa: desalineada de velas y niveles. | colocada por `xMapTime`/`yMap` |
| F12 | **ALTA** | Off-exchange sólo se confirmaba por el NOMBRE del venue, que viene de un catálogo remoto. Si falla: 0 prints en todos los activos, para siempre, sin error. | código SIP `D` (FINRA ADF) como prueba primaria |
| F13 | MEDIA | «Hedge Wall (ITM)» se dibujaba sobre «Call Wall»: dos etiquetas apiladas como evidencias independientes. | coincidencia declarada, nivel no duplicado |
| F14 | MEDIA | Vanna/Charm/Speed con escalas distintas por sección (~600× en SPY). | unidades canónicas de `units_registry`, ratio medido **0.971** |
| F15 | MEDIA | `api` declarada dos veces a nivel global con firmas distintas; cuál ganaba dependía del orden de los `<script>`. Con el orden invertido, todos los POST se habrían vuelto GET silenciosos. | renombrada a `itGetJson` |
| F16 | BAJA | El poller de Anomalías quedaba vivo toda la sesión sin forma de pararlo. | arranca y para con el panel |

Call Wall / Put Wall verificados sobre cadena DIA realista con muros sembrados en
520/510 y spot 515.20: aciertan en 0-1 DTE, semana y mes, del lado correcto del
precio, y `key_levels_report` coincide con `structure_levels`.

Frontend: **0 errores de eslint** en los 25 scripts (antes 18).

- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A4 · Interfaz viva (v1.42.6)

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F17 | **ALTA** | v1.42.4 corrigió `nextgen_terminal.js`, pero la raíz sirve `terminal.html` con otros scripts: el arreglo no tocaba la pantalla en uso. | contrato de renderizador fijado por test |
| F18 | **ALTA** | El TRACE aceptaba sin condición la ventana temporal compartida con Flujo de Órdenes; si ésta era más ancha, precio y heatmap se comprimían y sobraba lienzo. | en vivo el TRACE impone su propio eje |
| F19 | MEDIA | Faltaba el carril de volumen del subyacente; los buckets ya lo acumulaban sin dibujarlo. | quinto carril, rejilla de 5 filas |
| F20 | MEDIA | Dark Pool publicaba el notional off-exchange sin denominador. | **% fuera de bolsa** + mayor print + tabla |
| F21 | BAJA | En la interfaz secundaria los paneles sumaban 957 px dentro de un contenedor de 900 px con `overflow:hidden`. | alturas por peso sobre el alto real |

- Frontend: **0 errores de eslint** en los 25 scripts.
- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A5 · Capa QFLOW y multi-activo (v1.42.6)

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F22 | **ALTA** | `_target()` mandaba TODA acción a `direct=False` pese a consultar su propio ticker; su evidencia llegaba degradada a canal de contexto y las secciones quedaban vacías fuera de DIA. | regla universal: directa si el ticker es el del instrumento |
| F23 | **ALTA** | El normalizador leía `netCallPremium`/`netPutPremium` y el proveedor publica `callSum`/`putSum`; `stockPrice` se descartaba entero. | campos reales capturados |
| F24 | MEDIA | Sin campo neto explícito se publicaba `0.0`, convirtiendo una respuesta válida en serie plana. | neto derivado `call − put` |
| F25 | NUEVO | No existía capa QFLOW: ni serie acumulada, ni nivel de precio, ni eventos. | `app/core/qflow.py` + render |

Universalidad verificada: DIA · SPY · QQQ · IWM · AAPL · NVDA · TSLA · MSFT · GLD
consultan su propio ticker con evidencia directa. YM · MYM · DJX · VIX · VXD siguen
declarados como contexto del ecosistema Dow, que es la única razón legítima para
consultar otro ticker.

Un fallo de datos nunca se publica como `$0.0`: seis estados con su motivo.

- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.


## A6 · Net Drift oficial, Dark Pool directo y Quant Data como proveedor (v1.42.7)

| # | Severidad | Defecto | Antes → después |
|---|---|---|---|
| F26 | **CRÍTICA** | `_rows` sólo recorría listas y Quant Data devuelve un mapa `{época_ms: fila}`. Devolvía `[]` ante **toda respuesta válida**: `net-drift`, `net-flow` (y con él QFLOW, que nunca llegó a activarse), `exposure-by-strike/-expiration`, `max-pain-over-time`, `oi-over-time`, `volatility-drift` y `stock-price-over-time`. | `_keyed_rows` + `_ts_iso`; la vía de lista sigue siendo la primera |
| F27 | **ALTA** | La época en milisegundos viajaba cruda como `t`; `datetime.fromisoformat("1758205800000")` descartaba la fila **en silencio** aguas abajo. | conversión única a ISO-8601 UTC |
| F28 | **ALTA** | `net_drift` usaba el normalizador compartido, que colapsa la fila a `value`/`call`/`put` y tira los dos volúmenes netos y las dos primas a precio medio. | `norm_net_drift` con los siete campos oficiales |
| F29 | **ALTA** | No existía curva acumulada de Net Drift ni distinción del bucket abierto. | `app/core/net_drift.py` con reconstrucción íntegra por ciclo |
| F30 | **ALTA** | Dark Pool dependía **exclusivamente** de inferir el venue desde la cinta de otro proveedor; si ese campo faltaba, mostraba 0 en todos los activos sin poder decir por qué. | `dark-flow` (nuevo) + `dark-pool-levels` + `equity-prints` como fuente directa; venue como auditoría |
| F31 | MEDIA | La página Exposure no alimentaba EXPOSICIÓN: sus normalizadores esperaban listas y la respuesta es `data[TICKER].exposureMap[vto][strike]`. | `norm_exposure_by_strike` / `norm_exposure_by_expiration` |
| F32 | MEDIA | La columna ACCIONES de las zonas mostraba 0 en todas las filas porque ninguna fuente publicaba `shares`. | la publica `dark-pool-levels`; sin dato va vacío, nunca 0 |
| F33 | BAJA | La rejilla de KPIs de Dark Pool dejaba una fila a medias y `.grid.c5` no tenía puntos de ruptura. | ocho tarjetas en rejilla de cuatro + degradación responsive |
| F34 | **ALTA** | Doce de las 32 herramientas del catálogo se descargaban cada ciclo y **ningún consumidor las leía**: quemaban cuota y las secciones quedaban vacías con el dato ya en memoria. | las doce conectadas a su sección del mapa, con el motor propio mandando y el proveedor como respaldo declarado |
| F35 | MEDIA | `norm_prints` es genérico y dejaba las impresiones de opciones sin strike, vencimiento, tipo ni prima: filas anónimas imposibles de relacionar con un punto de la curva. | `norm_option_order_flow` con el contrato entero y el tipo de ejecución |
| F36 | MEDIA | El bloque `order_flow` sólo viajaba en el camino feliz de Net Drift; que falte la curva no implica que falte la cinta. | se publica en todos los caminos |
| F37 | **CRÍTICA** | La tarjeta de RESUMEN titulada **NET DRIFT** no dibujaba Net Drift: acumulaba la cinta propia de opciones y la presentaba con el nombre de una magnitud que sólo publica el proveedor. En pantalla era indistinguible de la real. | pasa a leer el bloque oficial `d.net_drift`; sin dato, SIN DATOS |
| F38 | **ALTA** | En esa misma tarjeta, `premium * (direction \|\| 1)` contaba como COMPRA toda la prima sin agresor clasificado, porque `0 \|\| 1` vale 1: la curva se inclinaba sola a comprador justo cuando la cinta llegaba sin cotización. | eliminado con la reconstrucción |

### Reparto visual de todas las secciones

La comprobación de rejillas ya no fija un número de tarjetas para una sección: recorre
las trece vistas de la terminal y exige que **toda** rejilla de KPIs tenga un número de
tarjetas múltiplo de sus columnas, y que toda anchura declarada (`c2`…`c6`) degrade en
pantalla estrecha. Resultado actual: **0 rejillas desequilibradas**.

### Certificación del acumulado contra la respuesta cruda

`certify_against_raw()` recalcula la suma desde las filas del proveedor, sin pasar
por la curva, y la contrasta punto a punto. Sobre 90 buckets:

    puntos comparados        90
    desvío relativo peor     5.3e-08
    desvío absoluto peor     5.0e-05   (= cuanto de redondeo de la publicación)

### Separación declarada

`net_drift` publica `independent_of = [GEX, DEX, NET_FLOW, QFLOW, GAMMA_EXPOSURE,
DELTA_EXPOSURE]` y `reconstructed = false`. El módulo no importa `qflow`, `engine`,
`trace_analytics` ni `precision_engine`; un test lo comprueba sobre el código fuente.

### Universalidad

Misma respuesta → misma curva punto a punto en DIA · SPY · QQQ · IWM · AAPL. Ningún
símbolo aparece como literal en `app/core/net_drift.py`.

### Límites de esta verificación

- **Sin navegador**: no hay comprobación visual directa; se verifica la estructura
  del HTML, el CSS y el código del renderizador, no el píxel pintado.
- **Sin credenciales de Quant Data**: la forma de las respuestas se reproduce a
  partir de la que el propio código de producción ya documentaba. **La comparación
  final contra una sesión real queda pendiente de ejecutarse en el VPS**, y para eso
  está `certify_against_raw()`.

- `plan_sha256`: `662e190cd9805bd29a342982cc52e0ac0bdde5b5624259c4b014b5727f68e43a`.

# DIVISIÓN DE TRABAJO ENTRE PROVEEDORES · ITM QUANT

Roster activo: **Alpaca + Quant Data**. Configurable con `ITM_OPTIONS_PEERS`.

---

## ALPACA · la materia prima observada

Alpaca es la fuente de **hechos de mercado**: lo que se negoció, a qué precio y
cuándo. Sin Alpaca el motor no tiene sobre qué calcular.

| Qué aporta | Endpoint | Para qué se usa |
|---|---|---|
| Barras de equity (1m, histórico) | `/v2/stocks/{s}/bars` | Velas de TRACE, backtesting, volatilidad realizada |
| Snapshot de equity | `/v2/stocks/{s}/snapshot` | Precio de referencia al arrancar |
| Cadena de opciones completa | `/v1beta1/options/snapshots/{s}` | **Strike, IV, interés abierto, bid/ask, volumen** |
| Cinta de operaciones de opciones | `/v1beta1/options/trades` | Flujo OPRA: prima, agresor, prints grandes |
| Stream de precio en vivo | WebSocket SIP | Tick a tick de la vela en formación |

**Lo más importante que da Alpaca es la cadena.** De ahí salen strike, IV, OI y
vencimiento — los cuatro insumos con los que el motor calcula **por sí mismo** las
Greeks (Black-Scholes) y de ahí GEX, DEX, VEX, CHEX, Gamma Flip, Call Wall, Put
Wall y Max Pain.

Sin Alpaca: sin cadena, sin estructura, sin muros. Es el proveedor crítico.

## QUANT DATA · analítica ya agregada

Quant Data es un proveedor **REST de analítica de opciones**. No publica quotes ni
cinta cruda: publica **resultados ya agregados** por su propio motor.

| Página | Herramientas | Para qué se usa |
|---|---|---|
| Exposure | GEX/DEX/VEX/CHEX por strike y vencimiento | Contraste con el cálculo propio |
| Flow Analysis | Net flow, net drift, interval map | Flujo de prima y mapa temporal |
| Open Interest | OI por strike/vencimiento, max pain, cambios | Contraste de OI y max pain |
| Volatility | IV rank, skew, estructura temporal, drift | IV rank cuando el motor aún no tiene historia |
| Statistics | Cuota de mercado, estadística por contrato | Reparto por vencimiento |
| Dark Pool / Equities | Niveles de dark pool, prints de equity | Niveles fuera de bolsa |
| Dashboard | Order flow, movers, noticias | Contexto |

**Quant Data no es imprescindible.** Todo lo estructural lo calcula el motor con la
cadena de Alpaca. Si Quant Data cae, la terminal pierde contraste externo y algunas
tablas de contexto, pero **GEX, DEX, muros y Gamma Flip siguen intactos**.

## Cómo conviven

La regla es **paridad, no jerarquía**: ninguno es "de confirmación".

1. **Cálculo propio primero.** Lo que el motor puede calcular con la cadena, lo
   calcula él. No delega su estructura en nadie.
2. **El proveedor cubre lo que el motor no observa.** Niveles de dark pool, cuota
   de mercado, y el IV rank mientras el motor no acumula 30 observaciones propias.
3. **Cuando ambos miden lo mismo, se comparan, no se promedian.** Dos lecturas
   sobre ventanas distintas promediadas dan un número que no describe ninguna de
   las dos. La divergencia se publica como aviso.

## Qué pasa si uno falla

| Cae | Consecuencia |
|---|---|
| **Alpaca** | Crítico. Sin cadena no hay estructura. El programa lo declara y no inventa. |
| **Quant Data** | Degradado. Se pierden tablas de contexto; la estructura sigue entera. |

## Reactivar tastytrade

```
ITM_OPTIONS_PEERS=ALPACA,TASTYTRADE,QUANTDATA
```

Su código sigue completo y con pruebas. Se retiró del roster por defecto porque su
DXLink se reconectaba continuamente y llenaba el log sin aportar nada que Alpaca no
diera ya.


---

## v1.42 · La división del trabajo deja de ser una convención y pasa a ser un contrato

Hasta v1.41 este documento describía qué hacía cada proveedor. Describir no es hacer
cumplir: `fuse_values` podía promediar GEX de dos proveedores si sus cifras estaban
cerca, aunque fueran dos construcciones distintas con el mismo nombre.

Desde v1.42 la división vive en el código (`app/core/metric_authority.py`) y se publica
en caliente en `GET /api/architecture`. Cada métrica declara tres cosas:

```
AUTHORITY   quién la produce. Es la respuesta publicada.
VALIDATION  quién puede corroborarla. Nunca la modifica.
FALLBACK    qué pasa si la autoridad no está: fallar, degradar o declarar no disponible.
```

| Métrica | Autoridad | Valida | Si falta | Tipo | ¿Fusión? |
|---|---|---|---|---|---|
| `underlying_price` | Alpaca | Quant Data | **falla cerrado** | observado | **sí**, con contrato |
| `option_quote` | Alpaca | — | **falla cerrado** | observado | **sí**, con contrato |
| `option_chain` | Alpaca | Quant Data | **falla cerrado** | observado | no |
| `option_trades` | Alpaca | Quant Data | degradado | observado | no |
| `open_interest` | Alpaca | Quant Data | degradado | observado | no |
| `implied_volatility` | ITM | Alpaca, Quant Data | no disponible | derivado | no |
| `gamma` / `delta` | ITM | Alpaca, Quant Data | **falla cerrado** | derivado | no |
| `gex` / `dex` | ITM | Quant Data | no disponible | derivado | no |
| `quantdata_gex` | Quant Data | ITM | no disponible | derivado | no |
| `net_drift` | **Quant Data** | — | no disponible | observado | no |
| `dark_pool_prints` | Alpaca | Quant Data | no disponible | observado | no |
| `flow_aggression` | ITM | Quant Data | degradado | **inferido** | no |
| `dealer_state` | ITM | Quant Data | degradado | **inferido** | no |
| `scanner_direction` | **ITM** | ninguna | **falla cerrado** | inferido | no |
| `macro_series` | FRED | — | no disponible | observado | no |

### Sólo dos métricas admiten fusión, y con condiciones

`underlying_price` y `option_quote` son dos lecturas del **mismo instrumento** en la
misma ventana temporal. Ahí una media ponderada por calidad tiene sentido físico. Aun
así, si divergen por encima de la tolerancia se publica la de mayor calidad: si dos
fuentes discrepan un 4 % sobre el precio de DIA, no están describiendo el mismo
instante, y su media no corresponde a ninguna cotización real.

Para todo lo demás, `fuse_values` devuelve `FUSION_FORBIDDEN` con el motivo.

### Por qué el GEX no se promedia nunca

El GEX de Quant Data y el de ITM no son dos medidas ruidosas de una cantidad común.
Son dos construcciones con su propio universo de vencimientos, su propia hipótesis de
posicionamiento del dealer y posiblemente otra representación de unidades. Promediarlas
produce una cifra que no describe el libro de nadie y, peor, borra la información más
útil que hay: **que difieren**. Esa discrepancia es una señal; la media la destruye.

Antes de enfrentarlos siquiera, `units_registry` exige siete coincidencias: misma
griega, misma representación, mismo universo de vencimientos, mismo instante, mismo
subyacente, mismas unidades y misma convención de signo. Si falta alguna, la respuesta
es `NOT_COMPARABLE` — que no es lo mismo que «conflicto».

### La dirección jamás viene de un proveedor

`scanner_direction` falla cerrado por diseño. Un proveedor puede entregar su propia
lectura direccional; ITM QUANT no la publica como suya ni la mezcla con la del Scanner.
Si el Scanner no tiene entradas utilizables, no hay dirección — que es una respuesta
honesta y verificable.

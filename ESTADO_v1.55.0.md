# ITM QUANT v1.55.0 · ESTADO POR MÓDULO

**Los cinco niveles, y qué significa cada uno**

| Nivel | Qué se ha hecho | Qué NO demuestra |
|---|---|---|
| `IMPLEMENTADO` | El código existe y hace lo que dice | Que alguien lo haya ejercitado |
| `TEST UNITARIO` | Una prueba automática cubre su lógica | Que funcione dentro del sistema |
| `TEST SINTÉTICO` | Probado de extremo a extremo con datos fabricados | Que el proveedor real se comporte así |
| `VALIDADO LIVE` | Ejercitado contra la API real, con datos de mercado | Que aguante ocho activos y una sesión entera |
| `CERTIFICADO` | Validado LIVE sobre los ocho activos, con evidencia archivada | — |

**Este entorno no tiene credenciales ni salida a `quantdata.us`.** Nada de lo
que sigue puede subir de `TEST SINTÉTICO` sin ejecutar la validación LIVE en su
máquina. Donde pone `VALIDADO LIVE` o `CERTIFICADO` es porque la comprobación
no depende del proveedor —matemática pura o geometría medida en un navegador
real—, y se dice en cada caso por qué.

---

## Puntos de la corrección integral

| # | Módulo | Nivel alcanzado | Evidencia |
|---|---|---|---|
| 1 | Agresor por marca · desglose y cobertura | `TEST SINTÉTICO` | 13 pruebas; desglose compra/venta/sin-lado con dominancia y cobertura, visible en el hover |
| 2 | FlowViewModel cableado al frontend | `TEST SINTÉTICO` | 12 pruebas; el modelo manda sobre las tarjetas, LKG por carril |
| 3 | Net Flow separado de la cinta | `TEST SINTÉTICO` | Datasets independientes en `flow_view.build`; la ausencia de uno no vacía al otro |
| 4 | Espacio en TRACE y eje Y compartido | `VALIDADO LIVE`¹ | Cabecera de altura fija; `ITMQTrace.alignment()` mide los tres lienzos en marcha |
| 5 | Scanner dentro de TRACE | `TEST SINTÉTICO` | `scanner_plan` con `thesis_id`; sustituye a `target`/`risk`, no convive |
| 6 | Toda línea con identidad · `UNIDENTIFIED_LEVEL` | `TEST UNITARIO` | 8 pruebas; recuento en el Auditor y trazo marcado en el gráfico |
| 7 | Call/Put Wall · autoridad única comprobada | `TEST UNITARIO` | `wall_consistency` compara motor, línea dibujada y tarjeta; duplicado = defecto |
| 8-9 | Interval Map · una rejilla, un renderer | `TEST SINTÉTICO` | 15 pruebas; mapa continuo principal, puntos como vista RAW |
| 10 | Interval Map QQQ contra RAW del proveedor | **`NO ALCANZADO`** | Requiere API real. La rejilla canónica está probada con datos sintéticos |
| 11 | Normalización sobre la ventana visible | `TEST SINTÉTICO` | TRACE recorta a la ventana + 35 %; verificado por inyección (90 → 13 filas, isolíneas 94 → 210) |
| 12 | Un strike, una barra | `VALIDADO LIVE`¹ | Regresión visual: 92 mediciones, grosor 5,47–13,68 px, sin agrupar |
| 13 | Relieve 3D útil | `VALIDADO LIVE`¹ | Mismo perfil que las barras, `aggregate: 'none'`, profundidad en píxeles absolutos |
| 14 | Net Drift · tamaño, un eje, anclaje | `TEST UNITARIO` | Suelo 660 px / 6fr; un solo `Q.scale(plo…)`; marcas por instante y precio |
| 15 | Muros de Dark Pool | `TEST UNITARIO` | 6 pruebas; líneas a ambos lados del spot, fuerza relativa al activo |
| 16 | `cycle_id` de Dark Pool | `TEST UNITARIO` | Estable con el mismo dato, cambia con cualquier pieza |
| 17 | Alcance temporal declarado | `TEST UNITARIO` | Ventana REALMENTE observada por carril; los niveles declaran no tener eje |
| 18 | KPI que dicen qué miden | `TEST UNITARIO` | `kpi_meta` con fórmula, carril, unidad y advertencia donde invita a error |
| 19 | **Monte Carlo · auditoría matemática** | **`CERTIFICADO`**² | 42 pruebas contra solución cerrada. Ver abajo |
| 20 | Cambio de símbolo transaccional | `TEST UNITARIO` | `generation_id` = «SÍMBOLO#época»; el cliente descarta la generación ajena entera |
| 21 | Monitorización de Londres | `TEST UNITARIO` | `session_mode` desde 04:00 `America/Guayaquil`, acumulado propio, sella sin borrar |
| 22 | LKG global | `TEST UNITARIO` | Inventario real en el Auditor: qué carriles están protegidos y desde cuándo |
| 23 | Ceros fabricados | `TEST UNITARIO` | 12 pruebas; corregido en normalizador, motor, segundo constructor y renderer |
| 24 | Multiactivo sin ramas por ticker | `TEST UNITARIO` | Barrido sobre CÓDIGO (sin comentarios ni cadenas): cero infracciones |
| 25 | Validación DIRECT_PROVIDER | **`NO ALCANZADO`** | Requiere API real. La herramienta existe: `scripts/verify_live_quantdata.py` |
| 26 | Regresión visual real | `VALIDADO LIVE`¹ | 92 paneles en Chromium con los renderizadores reales |
| 27 | Certificación LIVE con 8 activos | **`NO ALCANZADO`** | Requiere API real |

¹ **`VALIDADO LIVE` aquí significa «medido en un navegador real»**, no «contra
datos de mercado». La geometría de un panel no depende del proveedor: depende
del renderizador y del ancho del lienzo, y eso sí se ha ejercitado de verdad.
Lo digo con esta letra pequeña precisamente para que no se lea como lo otro.

² **Monte Carlo es el único `CERTIFICADO`**, y puede serlo porque su corrección
no depende de ningún proveedor: se contrasta contra fórmulas cerradas.

---

## Punto 19 · Por qué Monte Carlo sí está certificado

No se certifica porque «corre». Se contrasta contra soluciones **cerradas**:

| Qué se comprueba | Contra qué | Resultado |
|---|---|---|
| Precio esperado | `E[S_T] = S₀·e^{(r−q)T}` | 400 k trayectorias, dentro de 0,5 errores típicos |
| Varianza terminal | Varianza lognormal exacta | desvío +0,076 % |
| P(terminar más allá) | `N(d₂)` | 12 combinaciones (1/5/30 DTE × 4 niveles), todas < 2,1 σ |
| **P(tocar)** | Primer paso con reflexión de Girsanov | 12 combinaciones, todas < 1,2 σ |
| Percentiles terminales | Cuantil lognormal | desvío máximo 1,4 puntos básicos |
| Convergencia | `1/√N` teórico | ratio 0,90–1,13 entre N = 1 000 y N = 256 000, sin sesgo |
| Sensibilidad | Monotonía en IV, DTE, r, q | correcta en los cuatro, con el signo que toca |

**La corrección de puente browniano, medida:** con un paso por día y 1 DTE,
contar trayectorias da **17,2 %** donde la respuesta exacta es **34,30 %**. El
puente da **34,27 %**. No es una aproximación razonable: es la respuesta.

**Defecto encontrado y corregido durante la auditoría.** Producción llamaba
**sin semilla**, así que cada refresco sorteaba números nuevos: el mismo mercado
quieto publicaba 43,8 % y un minuto después 44,2 %. Con 20 000 trayectorias el
error típico es ~0,35 pp, y un operador no tiene cómo distinguir eso de un
movimiento real. Ahora la semilla se **deriva de las entradas**: mismo mercado,
mismo número; y en cuanto el spot, la IV, el DTE o un nivel cambian, cambia con
ellos. Se publica además el error típico de la propia simulación.

---

## Lo que queda abierto, dicho sin adornos

1. **Puntos 10, 25 y 27 no están hechos.** Piden evidencia LIVE contra la API
   real con ocho activos. Este entorno no tiene credenciales ni salida a
   `quantdata.us`. La herramienta para ejecutarlos existe
   (`scripts/verify_live_quantdata.py`) y se lanza desde su máquina con la clave.

2. **Nada que dependa del proveedor pasa de `TEST SINTÉTICO`.** La cadena del
   agresor, los recuentos de Dark Pool y la verificación del Interval Map contra
   el RAW de QQQ están probados con datos fabricados que imitan el contrato
   publicado. Si el proveedor difiere del contrato en algún campo, se verá ahí.

3. **El empaquetado certificado no se puede producir aquí.** Requiere Python
   3.12.14 y Node 22.16.0; este entorno tiene 3.11.15 y 22.22.2.

4. **La auditoría de ceros fabricados cubrió las rutas críticas**, no las ~40
   acumulaciones internas de `aggression_delta`, `dealer_intelligence` y
   `market_state_field`, donde el cero significa «no aportó nada» y no se
   publica como KPI. Las dejé a propósito y lo digo en vez de darlas por
   revisadas.

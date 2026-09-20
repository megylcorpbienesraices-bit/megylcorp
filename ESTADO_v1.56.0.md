# ITM QUANT v1.56.0 · ESTADO POR MÓDULO

## Los cinco niveles, y qué NO demuestra cada uno

| Nivel | Qué se hizo | Qué **no** demuestra |
|---|---|---|
| `IMPLEMENTADO` | El código existe y hace lo que dice | Que alguien lo haya ejercitado |
| `TEST UNITARIO` | Una prueba automática cubre su lógica | Que funcione dentro del sistema |
| `TEST SINTÉTICO` | Extremo a extremo con datos fabricados | Que el proveedor real se comporte así |
| `VALIDADO LIVE` | Ejercitado contra la API real con datos de mercado | Que aguante ocho activos una sesión entera |
| `CERTIFICADO` | Validado LIVE sobre los ocho activos, con evidencia archivada | — |

**Este entorno no tiene credenciales ni salida a `quantdata.us`.** Nada que
dependa del proveedor puede subir de `TEST SINTÉTICO` sin ejecutar la validación
en su máquina. Donde pongo otra cosa es porque la comprobación **no depende del
proveedor**, y lo digo en cada caso.

---

## Los diez gates

| Gate | Punto(s) | Nivel | Evidencia |
|---|---|---|---|
| 1 · Agresor forense | 1, 43, 44 | `TEST SINTÉTICO` | 31 pruebas; `tradeSideCode` manda, 17 campos por print, tabla de evidencia con inyección de inversión |
| 2 · La marca | 2, 3 | `TEST SINTÉTICO` | 16 pruebas; una sola implementación en `itmq_core`, cero copias en TRACE y FLUJO |
| 3 · Barras de flujo | 45-49 | `TEST SINTÉTICO` | 19 pruebas; bucketizado en servidor, LKG por carril, recuento de la cadena |
| 4 · Auditoría y carriles | 4, 7 | `TEST UNITARIO` | 14 pruebas; diez contadores, siete estados, ocho carriles, nueve campos |
| 5 · Net Drift | 8, 9, 10 | `TEST UNITARIO` | 17 pruebas; un solo eje de precio, respaldo de velas declarado |
| 6 · Muros | 11, 12, 24 | `TEST UNITARIO` | 16 pruebas; fórmula publicada, igualdad en seis secciones |
| 7 · TRACE | 13-16 | `VALIDADO LIVE`¹ | 19 pruebas + regresión visual; cabeceras de altura fija, muro lejano anclado |
| 8 · Interval Map | 17-20, 50-54 | `TEST SINTÉTICO` | 23 pruebas; hueco preservado, signo verificado, dos inyecciones de defecto |
| 9 · Monte Carlo + ceros | 28, 29, 30 | **`CERTIFICADO`**² / `TEST UNITARIO` | 42 + 18 pruebas; gate de release, cinco ficheros auditados, pendientes **0** |
| 10 · Transaccional | 31-39, 55 | `TEST UNITARIO` | 24 pruebas; dos huellas, commit de snapshot, Londres, ocho activos |

¹ **`VALIDADO LIVE` aquí significa «medido en un navegador real»**, no contra
datos de mercado. La geometría de un panel no depende del proveedor: depende del
renderizador y del ancho del lienzo, y eso sí se ejercitó de verdad (92
mediciones en Chromium, grosor 5,47–13,68 px, ocupación máxima 0,76). Lo digo en
letra pequeña justo para que no se lea como lo otro.

² **Monte Carlo es el único `CERTIFICADO`**, y puede serlo porque su corrección
se contrasta contra fórmulas cerradas y no depende de ningún proveedor.

---

## Los 20 criterios de cierre

| # | Criterio | Estado |
|---|---|---|
| 1 | Suite interna completa PASS | ✅ **2515 pruebas, 0 fallos** |
| 2 | Auditoría matemática completa | ✅ 42 pruebas contra solución cerrada |
| 3 | BUY/SELL validado contra cinta real | ⏳ **PENDIENTE LIVE** · `--aggressor` |
| 4 | QFLOW con el mismo agresor | ✅ una sola autoridad, verificada |
| 5 | Interval Map RAW contra renderer | ⏳ **PENDIENTE LIVE** · `--interval-map` |
| 6 | TRACE usando ese mismo Interval Map | ✅ misma rejilla, mismo `AB.field` |
| 7 | Call/Put Wall auditados, una autoridad | ✅ `wall_audit` con fórmula y ranking |
| 8 | Walls idénticos en las cuatro pantallas | ✅ comprobado en **seis** |
| 9 | Net Drift grande con Walls + QFLOW | ✅ 660 px / 6fr, eje único |
| 10 | FlowViewModel única ruta del frontend | ✅ tarjetas y barras del modelo |
| 11 | LKG por carril | ✅ ocho carriles, nueve campos |
| 12 | Dark Pool consistente por `cycle_id` | ✅ huella del conjunto |
| 13 | Monte Carlo protegido por gate | ✅ nueve pruebas en el gate de release |
| 14 | Acumulaciones auditadas | ✅ cinco ficheros, **pendientes 0** |
| 15 | Cambio de símbolo transaccional | ✅ `generation_id` + `cycle_id` + commit |
| 16 | Londres desde 04:00 sin tocar el proveedor | ✅ verificado, incluido el horario de verano |
| 17 | Validación LIVE multiactivo | ⏳ **PENDIENTE LIVE** · `--cierre` |
| 18 | Regresión visual final | ✅ 92 paneles en Chromium |
| 19 | `PACKAGING UNLOCKED` | ⏳ **PENDIENTE** · requiere su toolchain |
| 20 | ZIP oficial + SHA256 | ⏳ **PENDIENTE** · depende del 19 |

---

## Lo que queda abierto, sin adornos

### 1 · Lo que exige su API (criterios 3, 5, 17)

Puntos 5, 10, 18, 25, 27, 37, 38 y 53. Este entorno no tiene credenciales ni
salida a `quantdata.us`. Las herramientas están escritas y probadas:

```
python scripts/verify_live_quantdata.py --cierre
```

Ese modo recorre los ocho activos y ejecuta las dos comprobaciones forenses:

- **Agresor:** por cada operación real imprime `tradeSideCode`, `bid`, `ask`,
  `precio`, el lado **esperado** según el contrato, el que produjo el
  clasificador, y las marcas de FLUJO y de TRACE. Si una sola etapa cambia BUY
  por SELL, devuelve código 1 y nombra la operación.
- **Interval Map:** compara hasta 50 celdas por activo, RAW contra el signo
  renderizado. Si el proveedor devuelve negativo, el rojo es correcto y lo dice;
  si no coincide, lo marca `BUG` con el strike y el intervalo.

### 2 · El empaquetado certificado (criterios 19, 20)

El gate está **fail-closed a propósito** y lo dejé intacto:

```
PACKAGING BLOCKED
 - Python 3.11.15 != target 3.12.14
 - Node    22.22.2 != target 22.16.0
 - npm     10.9.7  != target 10.9.2
 - pip-audit / ruff / pyzmq  MISSING
```

Con Python 3.12.14 y Node 22.16.0 instalados:

```
python scripts/package_release_artifact.py --check      # PACKAGING UNLOCKED
python scripts/package_release_artifact.py --output ../ITM_QUANT_v1.56.0.zip
```

Un ZIP producido con otra cadena de herramientas **no es** el certificado, por
mucho que funcione: su sha256 no sería el que su máquina reproduce, y eso es lo
único que el certificado garantiza.

### 3 · Lo que no depende del proveedor y ya está cerrado

Monte Carlo (matemática contra fórmulas cerradas), la geometría de los paneles
(medida en Chromium), la autoridad única de muros y de la marca, la preservación
del hueco en el Interval Map, el inventario de acumulaciones y la
transaccionalidad del cambio de activo. **Eso queda congelado como regresión:**
cada uno tiene pruebas que fallan si alguien lo deshace.

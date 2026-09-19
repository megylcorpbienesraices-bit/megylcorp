# ITM QUANT MULTI ASSET · v1.49.0 — El contrato de `dark-pool-levels`

Release: `ITM_QUANT_v1.49.0_PRE_VPS` · Base: `v1.48.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Ninguna
métrica cambia de valor.

---

## Por qué el cuerpo mínimo estaba mal

v1.46.0 razonó así: un cuerpo con campos de más es tan inválido como uno con
campos de menos, y `dark-pool-levels` no comparte contrato con sus vecinas, así
que se parte del mínimo y se sube sólo si el proveedor nombra un campo.

El razonamiento es correcto. La conclusión era falsa, por una razón que no se
podía deducir recortando: **el contrato exige un campo obligatorio que ninguna
otra herramienta del catálogo usa.**

```json
{
  "sessionDateRange": { "startDate": "2026-09-18", "endDate": "2026-09-18" },
  "filter": { "ticker": "DIA" }
}
```

- `sessionDateRange.startDate` — **obligatorio**
- `filter.ticker` — **obligatorio**
- `sessionDateRange.endDate` — opcional

Y la trampa concreta: **`dark-flow` acepta `sessionDate` y `timeRange`; 
`dark-pool-levels` usa exclusivamente `sessionDateRange`.** Dos nombres
parecidos para dos contratos distintos. Partir de la herramienta vecina y
recortar no podía llegar nunca al nombre correcto, por mucho que se recortara.

Este endpoint además **no acepta** `sessionDate`, `timeRange`, `snapshotTime`,
`aggregationPeriod`, `filterExpression`, `projection` ni `pagination`.

## 1 · La fecha es la última sesión válida

Un sábado no es una sesión. Pedir el día en curso sin comprobarlo da un 400 o un
200 vacío según cómo lo trate el proveedor, y **las dos cosas se leen en pantalla
como «no hay dark pool»** cuando lo que pasa es que se pidió un día que no
existe. `last_valid_session_date()` lo resuelve con el calendario que el programa
ya tenía.

## 2 · Los campos prohibidos son de la herramienta, no del catálogo

`aggregationPeriod` es legítimo en `dark-flow` y está en la tabla de
reparaciones, así que un 400 mal leído podía hacer que se lo añadiéramos a
`dark-pool-levels`, que lo rechaza. `TOOL_FORBIDDEN_FIELDS` impide que la
reparación guiada por el error añada a un endpoint algo que su propio contrato
prohíbe.

## 3 · La respuesta es un mapa, no una lista

El proveedor documenta un **mapa por nivel de precio** —la clave *es* el precio—
y el extractor genérico sólo sabía leer listas. Con un mapa devolvía cero
niveles, así que **un 200 perfectamente válido se publicaba como «SIN DATOS»** e
era indistinguible de una sesión sin actividad fuera de bolsa.

Ahora se leen las dos formas, con los nombres oficiales:

| contrato | campo publicado |
|---|---|
| la clave del mapa | `price` |
| `notionalValue` | `notional` |
| `size` | `shares` |
| `tradeCount` | `prints` |
| `latestStockPrice` | `latest_stock_price` |

Certificado de extremo a extremo:
`QUANT DATA RAW → NORMALIZER → DATA HUB → DARK POOL LEVELS → FRONTEND`, con
`DIRECT_PROVIDER · QUANTDATA` en el registro de procedencia.

## 4 · El 400 se conserva entero

`type`, `detail` y **cada `errors[].field` con su `errors[].message`** viajan
desglosados hasta una tabla propia del Auditor: **DARK POOL · CUERPO RECHAZADO
POR EL PROVEEDOR**. También se lee la convención Pydantic, donde la lista va
dentro de `detail`.

Un 400 es **validación inválida, no ausencia de datos**: no entra en el ciclo de
reintentos. Un 422 es petición válida sin datos. Un 5xx sí admite backoff. Los
tres carriles siguen siendo independientes.

## 5 · El verificador exige un 200

`verify_live_quantdata.py --dark-pool` ya no da por cerrado el endpoint con un
SIN DATOS. Publica cuántos activos devolvieron **HTTP 200 real**, imprime el
cuerpo enviado para poder cotejarlo con el contrato, y cuando hay rechazo
imprime **el campo y el mensaje**, no el titular.

---

## Lo que falta para cerrarlo

El cuerpo es ahora el del contrato publicado y el parser cubre la respuesta
documentada, pero **sigue sin haber un 200 real**: este entorno no tiene salida
hacia `quantdata.us` ni credenciales.

```
python scripts/verify_live_quantdata.py --dark-pool --json dark_pool.json
```

Si con este cuerpo todavía devolviera 400, **el siguiente paso no es otro
payload**: es leer `errors[].field` y `errors[].message`, que ahora se conservan
enteros y se enseñan en el Auditor.

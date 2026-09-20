# ITM QUANT MULTI ASSET · v1.53.1 — La identidad de cada línea

Release: `ITM_QUANT_v1.53.1_PRE_VPS` · Base: `v1.53.0` · Alcance: `MULTI_ASSET`

**No se modifican fórmulas, pesos del Scanner ni autoridad direccional.** Ningún
nivel cambia de valor y ninguno se renombra: se adjunta la identidad que ya
tenían.

---

## El problema

Había líneas dibujadas en TRACE **sin etiqueta**. Una línea sin nombre sobre un
gráfico de operativa es peor que no dibujarla: se ve, parece significar algo, y
no hay forma de saber qué.

## Por qué no se puede deducir por el color

Porque la tabla de estilo **agrupa varios `kind` bajo el mismo token**:

```
--neg  (rojo)   →  put_wall   Y   risk
--pos  (verde)  →  call_wall  Y   target
```

Deducir por color acierta la mitad de las veces y **no avisa cuando falla**. Hay
un test que fija esto (`test_the_colour_cannot_identify_a_line`): si alguien
añade un `kind` a uno de esos colores, salta.

## La cadena real, seguida hasta el motor

```
nextgen_terminal.structure_levels()      ← construye el nivel con _level(name, value, kind)
  → _attach_hub_layers() en main.py      ← el Wall Engine SUSTITUYE call_wall/put_wall
    → payload["levels"]                  ← lo que viaja en el bundle de TRACE
      → drawLevels() en itmq_trace.js    ← el renderer
```

Volcado **contra el motor en ejecución**, no deducido. Once líneas:

| precio | type | nombre del motor | source | campo |
|---|---|---|---|---|
| 535.62 | `risk` | Invalidación | `nextgen_terminal.structure_levels` | `scanner['invalidation']` |
| 535.16 | `flip` | Zero Gamma | `nextgen_terminal.structure_levels` | `gd['gamma_flip']` |
| 534.70 | `call_wall` | CALL WALL | `ITMQ_WALL_ENGINE` | `walls['call_wall']` |
| 534.29 | `zone` | Zona High | `nextgen_terminal.structure_levels` | `scanner['zone']['high']` |
| 534.28 | `delta` | Delta Center | `nextgen_terminal.structure_levels` | `gd['delta_center']` |
| 534.23 | `gamma` | Gamma Center | `nextgen_terminal.structure_levels` | `gd['gamma_center']` |
| 534.11 | `zone` | Zona Low | `nextgen_terminal.structure_levels` | `scanner['zone']['low']` |
| 533.70 | `target` | T1 | `nextgen_terminal.structure_levels` | `scanner['target1']` |
| 533.70 | `put_wall` | PUT WALL | `ITMQ_WALL_ENGINE` | `walls['put_wall']` |
| 533.20 | `target` | T2 | `nextgen_terminal.structure_levels` | `scanner['target2']` |
| 530.20 | `vol_trigger` | Vol Trigger | `nextgen_terminal.structure_levels` | `structural_walls()['volatility_trigger']` |

## Cuáles eran las anónimas — calculado, no supuesto

El renderer rotulaba sólo las primeras por prioridad. Con el orden de
`LEVEL_STYLE`, las que caían fuera del cupo eran exactamente:

```
T1            type=target   order 7
T2            type=target   order 7
Invalidación  type=risk     order 7
```

- **La roja** es `type=risk`, nombre del motor **`Invalidación`**, producida por
  `nextgen_terminal.structure_levels()` a partir de `scanner['invalidation']`.
  Método: *precio que invalida la tesis direccional*.
- **La verde** es `type=target`, nombre del motor **`T1`** (o `T2`), producida
  por la misma función a partir de `scanner['target1'|'target2']`. Método:
  *objetivo de la tesis direccional*.

**Ninguna de las dos es un nivel de exposición de opciones.** Las dos salen del
Scanner, no de la cadena de griegas — que es justo lo que el color hacía
imposible distinguir, porque comparten token con `call_wall` y `put_wall`.

## Lo que ahora expone el Auditor

Tabla nueva `TRACE · IDENTIDAD DE CADA LÍNEA`, con una fila por línea visible:

```
precio · type · nombre del motor · source · campo · method · magnitud
       · persistencia · timestamp
```

- **magnitud**: la fuerza del nivel cuando el cálculo publica una. Cuando no,
  dice *«el cálculo no publica magnitud»* — **nunca un cero**.
- **persistencia**: ciclos consecutivos en el mismo sitio y minutos sostenidos.
  La tolerancia es **relativa al precio**, no en dólares: un dólar en un índice
  de 5.800 es el mismo nivel; en un ETF de 40, es otro.
- **`kind` no registrado**: si aparece uno que el registro no conoce, lo dice con
  esas palabras en vez de inventarle un origen.

## Y en el gráfico

**Toda línea visible lleva etiqueta.** El sitio sale del alto del panel, no de un
cupo fijo. Si no caben todas con su nombre largo, las de menor prioridad pasan al
nombre **corto** antes que quedarse mudas: una abreviatura identifica, una línea
anónima no.

```
INVAL 535.62 · 0Γ · CW · PW · VT · ΓC · ΔC · ZONA · OBJ
```

`short` **no renombra**: el nombre que manda sigue siendo el que publica el
motor, y es el que aparece en el Auditor.

---

## Límite declarado

Los precios de su sesión —roja ~517.2, verde ~515.0— son de su activo en vivo.
Aquí el mismo `type` sale en otros precios porque el demo cotiza en otro nivel.
**La identidad es la misma y es la que se comprobó**: `risk` por encima del spot,
`target` por debajo, las dos desde el Scanner vía `structure_levels`.

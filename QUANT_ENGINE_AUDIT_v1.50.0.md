# AUDITORÍA DEL MOTOR · ITM QUANT v1.50.0

## Veredicto

Ninguna fórmula cambia en esta release. Lo que cambia es **qué se muestra y con
cuánto sitio**, y eso tiene una trampa que merece dejar escrita: cada vez que se
toca la presentación, la vía fácil es tocar el dato. Aquí no se ha tocado.

---

## 1 · Recortar sin borrar

El panel de strikes pintaba filas que no pueden mover nada. La corrección obvia
—quitarlas del cálculo— habría sido un error de otra clase: el máximo, el total
y el porcentaje call/put se calculan sobre **todo** el libro, y recortarlo los
habría movido sin que nadie lo pidiera.

Por eso `relevant_rows()` devuelve `(rows, meta)` y el bundle sigue llevando las
filas completas. El panel dibuja el subconjunto; los agregados siguen saliendo
del conjunto entero. Y `meta` viaja con el panel, así que el recorte es
auditable: banda, total, conservadas, descartadas y motivo.

```
strike_window = {band_pct, total, kept, dropped, kept_far, low, high, reason}
```

Si alguna vez un agregado y su panel discrepan, `strike_window` dice por qué.

## 2 · Por qué la materialidad tiene dos condiciones y no una

La primera versión conservaba una fila lejana si su magnitud llegaba al 18 % del
máximo. Parece suficiente. No lo es, y el propio test nuevo lo destapó:

> Con un perfil **plano** —todas las filas de magnitud parecida— *cada* strike
> lejano supera el 18 % del máximo. El filtro no filtra nada.

El fallo es conceptual: *ser grande* no es la propiedad que distingue un muro.
La propiedad es **destacar contra su entorno**. `MATERIAL_STANDOUT = 3.0` mide
contra la **mediana de las filas lejanas**, no contra el máximo global. En un
perfil plano la mediana lejana es alta, nada destaca y todo se recorta. Con un
muro real la mediana lejana es baja y el muro se queda.

Las dos condiciones son conjuntivas a propósito. La primera evita conservar
ruido que destaca sobre un fondo de ceros; la segunda evita conservar un fondo
uniforme que casualmente es alto.

## 3 · La marca separa el hecho de la dirección

Antes: marca verde = compra, marca roja = venta. Una sola señal cargando dos
significados, y el tamaño —lo único que dice la magnitud— compitiendo con el
color por la atención.

Ahora son tres canales independientes:

| Canal | Qué codifica |
|---|---|
| Círculo dorado | **Que ocurrió** un flujo, y dónde exactamente |
| Radio del círculo | **Cuánto**, relativo al pico del ciclo |
| Flecha verde/roja | **Hacia dónde** |
| Cifra | El importe, sin interpretación |

El radio es lo delicado. `markerStrength` / `evStrength` normalizan contra
`S.qflowPeak` / `qPeak` —el pico **de este ciclo**—, no contra una constante.
Con una constante, un día tranquilo pintaría todo diminuto y un día agitado
todo saturado, y el tamaño dejaría de informar. Con el pico del ciclo, dos
círculos iguales son dos eventos comparables, que es exactamente lo que el ojo
asume al mirarlos.

## 4 · El carril TOTAL suma; no promedia

Al agrupar visualmente, la media es la reducción tentadora y la equivocada: dos
intervalos de signo opuesto se cancelan y el contenedor sale vacío cuando el
minuto estuvo lleno de actividad. `AB.reduceBin` en modo `sum` conserva la suma
**y** el pico, y el contenedor se pinta como grande si su pico lo merece, para
que un print relevante no se diluya entre vecinos pequeños.

El criterio de qué se marca en oro es **explícito y elegible** (`Top 3`, `≥10×`
la media) porque no existe un umbral universalmente correcto; lo que sí es
incorrecto es tener uno oculto. La media se dibuja como línea: sin ella, «esto
destaca» es una afirmación sin referencia.

## 5 · Un carril mudo es un bug de comunicación

El carril TOTAL de la cinta dibujaba ejes y se quedaba en blanco cuando ningún
contenedor tenía prima. Para quien mira, eso es idéntico a un fallo de render.
La regla que ya gobernaba Dark Pool —**nunca SIN DATOS sin saber por qué**—
aplica igual aquí: si hay ejes y no hay barras, el carril dice la causa.

---

## Lo que esta auditoría NO puede afirmar

- **Que las marcas doradas se vean bien sobre flujo real.** Este entorno no tiene
  cinta de opciones. Se ha verificado el código de render y la geometría; no el
  píxel sobre datos vivos.
- **Que `dark-pool-levels` devuelva 200.** Sin salida a `quantdata.us` ni
  credenciales. El cuerpo es el del contrato publicado; la certificación LIVE
  sigue pendiente.
- **Que el artefacto esté certificado.** El empaquetador oficial es fail-closed
  sobre Python 3.12.14 / Node 22.16.0; aquí corre 3.11.15 / 22.22.2.

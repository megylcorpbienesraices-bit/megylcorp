# AUDITORÍA DEL MOTOR · ITM QUANT v1.44.0

## Veredicto

v1.43.0 auditó **de quién era cada número**. v1.44.0 audita algo más incómodo:
**si el mismo número decía lo mismo en todas partes**. No lo decía.

---

## 1 · Tres muros con el mismo nombre

```
nextgen_terminal   structural_walls(curagg, sp, enriched=_latest_chain(gd))
key_levels_report  structural_walls(curagg, sp, enriched=_latest_chain(gd), symbol=…)
premarket          _structural_walls(wall_frame, spot)     # otro frame, otra columna
```

Tres llamadas, tres entradas posibles, y ninguna comprobación de que coincidieran.
El fallo no era ruidoso: los tres devolvían un número plausible. Simplemente no
tenía por qué ser el mismo, y el operador que compara TRACE con RESUMEN no tiene
forma de saber cuál mirar.

**Corrección.** Un `wall_engine` con una sola entrada —el snapshot del Data Hub— y
una sola salida que todas las secciones consumen. RESUMEN dejó de calcular; el
`key_levels_report` sigue existiendo para lo demás, pero sus muros ya no se leen.

**Lo que NO se hizo:** borrar `structural_walls()`. Sigue viva y entra como
respaldo declarado cuando el proveedor no sirve exposición por strike del activo.

---

## 2 · Decisiones del Wall Engine que merecen justificación

**Media geométrica, no suma.** Exposición y OI miden cosas distintas: magnitud de
cobertura y libro que la sostiene. La suma deja pasar a los que sólo destacan en
una, y un muro que sólo existe en una de las dos evidencias no es un muro. El
producto exige las dos.

**Sin OI no se multiplica por cero.** Se puntúa sólo con exposición y se declara.
Multiplicar por un dato ausente afirma que no hay libro; lo cierto es que no se
sabe, y son cosas distintas.

**La histéresis cede ante la realidad, no ante el ruido.** Un candidato marginal no
mueve la línea; un muro atravesado por el precio se retira en el acto. Confundir
los dos casos produce o una línea que baila o una que miente.

**El tope de distancia es relativo.** Un strike a 3 $ es medio por ciento en un
índice de 600 y un 31 % en una acción de 9.5. Este detalle lo destapó una prueba
que escribí mal: el fixture usaba pasos de un dólar para los tres activos, o sea
cometía en la prueba exactamente el error de escala que el programa evita. La
prueba falló, el código tenía razón, y el fixture se corrigió.

---

## 3 · Anclaje: la diferencia entre «cerca» y «aquí»

La marca de QFLOW se dibujaba a la altura de `m.price`. Ese precio viene del bucket
de 1 min del proveedor de **opciones**; la vela viene del feed del **subyacente**,
con otro reloj. La marca quedaba a unos píxeles de la vela a la que pertenecía, y
con el eje comprimido esos píxeles son varios ticks de mentira.

**Corrección.** Se resuelve la vela que CONTIENE el instante, en `[t, t+bar)`. La
contención es estricta a propósito: un evento que cae en un hueco de la serie no se
asigna a la vela anterior —eso sería inventar dónde pasó—, se dibuja atenuado y
declarado.

---

## 4 · El `gather` desnudo

```python
results = await asyncio.gather(*jobs.values(), return_exceptions=True)
```

Nueve endpoints, un solo tiempo de espera: el del más lento. `dark-flow` tardando
nueve segundos retrasaba nueve segundos la estructura que sostiene TRACE, aunque
la exposición hubiera llegado en trescientos milisegundos.

**Corrección.** Cada canal con su timeout y su cortocircuito. Y un detalle que
importa más de lo que parece: **la petición que expira no se cancela**. Se la deja
terminar y su resultado alimenta el Last Known Good. Cancelarla habría obligado a
volver a pedir —y a pagar otra vez— el mismo dato que ya venía de camino.

*(Durante la implementación, la primera versión de ese mecanismo producía avisos
de «Future exception was never retrieved»: ruido que esconde los fallos de verdad,
exactamente lo que el propio docstring decía evitar. Se corrigió consumiendo la
excepción del futuro compartido, que el llamador original recibe igualmente.)*

---

## 5 · El fallo que nadie ve

Una respuesta lenta de DIA aterrizando en la pantalla de AAPL con los números de
DIA. Todo correcto salvo el ticker. Nada falla, nada se registra, y el operador
lee un mercado que no está mirando.

**Corrección.** Época de símbolo que avanza ANTES de tocar nada, y una comprobación
en el punto de escritura. Y la invalidación de **los dos** tickers, no sólo el
anterior: el nuevo pudo quedar sembrado por una consulta previa, y servir eso como
si fuera de ahora es peor que no tener nada.

---

## 6 · Lo que esta auditoría NO puede afirmar

- **Que los muros estén bien calibrados.** Se certifica que se adaptan a la escala
  del activo y que son estables frente al ruido. Si el 15 % de histéresis o el 12 %
  de distancia son los valores correctos sólo lo dice la observación en vivo.
- **Que los datasets lleguen como DIRECT_PROVIDER en producción.** Sin credenciales
  todo cae a respaldo declarado. `scripts/verify_live_quantdata.py` está para
  responder esa pregunta, y **está sin ejecutar** contra la API real.
- **Que el renderizado sea correcto con una sesión real.** Se verificó el
  renderizado con datos de respaldo; con mercado abierto es otra comprobación, y
  está en `docs/operations/VALIDACION_LIVE_PRE_PRODUCCION_v1.44.0.md`.

# ITM QUANT — Chart State & Session Continuity

## Regla de autoridad
Scanner sigue siendo la única autoridad direccional. Este cambio corrige continuidad y presentación; no cambia dirección, Evidence Score, Risk ni señal.

## Continuidad temporal
Las series que sobreviven un reinicio visual usan una secuencia de presentación `MERGE -> DEDUPE -> GAP SEGMENTATION -> LIVE`. Un hueco grande se representa como un corte explícito, nunca como una diagonal interpolada. El historial compacto puede aportar spot/Net Delta/Net GEX y, desde v1.25.4, Call/Put Delta/GEX cuando estuvieron realmente disponibles.

## Estado de gráficos
Los selectores forman un snapshot de estado por request. Cada respuesta lleva el estado aplicado por backend y las respuestas antiguas se descartan. Así se impide que un polling de 30 segundos pinte Gamma/Barras encima de una selección manual posterior Net OI/Líneas.

## Surface
`Superficie` usa GPU 3D. Barras/Líneas/Puntos/Picos/Olas usan el renderer 2D nativo cuando el spec es compatible. Plotly queda como fallback. Las lentes 3D son campos visuales, no señales.

## Ruta cuantitativa
La ruta PREMARKET/LIVE interpola visualmente origen, T1/T2 e invalidación que ya produjo Scanner. La curva no modela por sí misma una trayectoria futura ni tiene autoridad probabilística.

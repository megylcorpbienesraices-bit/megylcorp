# ITM QUANT · GPU / Native Rendering Architecture

## Objective
The trading terminal owns the visual behavior. TRACE must not depend on Plotly for its primary price workspace. Quant surfaces use native WebGL payload rendering; Python computes data and the browser renders it.

## Data path
`Quant Core → API/WebSocket payload → Visualization Ring Buffer → renderer → screen`

The ring buffer caps visual working memory while the persistent/research store retains the complete causal history.

## TRACE 2D
Renderer: ITM QUANT Canvas 2D.
- price-first candles and volume;
- shared crosshair/tooltip;
- wheel zoom under cursor, drag pan, independent Y zoom;
- FOLLOW disables when the user takes manual control and resumes only explicitly;
- Gamma profile left / Delta profile right on the same strike/price axis;
- structural snapshot vs live repricing vs observed OPRA prints are visually distinct;
- responsive ResizeObserver without resetting the user viewport.

## Quant surfaces
Renderer: ITM QUANT WebGL.
Fields: IV when available, Gamma, Delta, Vanna, Charm, Speed, Color, GEX, DEX, estimated Hedge pressure and research Q(K,T).

The GPU receives typed numeric arrays and renders the selected field without recomputing quantitative math in the shader. Model-risk disclosure remains visible.

## LOD / performance
The renderer may decimate only the displayed representation according to viewport/zoom; source data and research history remain untouched. Large future streams should use TypedArrays and a circular visualization buffer so network/event frequency is decoupled from frame rate.

## Failure behavior
If WebGL is unavailable, the UI reports renderer degradation rather than fabricating a surface. Quant computation remains available to the engine/decision layer.

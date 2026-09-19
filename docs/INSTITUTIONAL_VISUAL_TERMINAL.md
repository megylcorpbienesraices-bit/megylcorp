# ITM QUANT · Institutional Visual Terminal

## Goal
The UI is a trading instrument, not a collection of dashboards. The main hierarchy is **DECIDE → OBSERVE → VALIDATE → RESEARCH**. Important information is never deleted; lower-frequency diagnostics are moved behind drawers or dedicated research pages.

## Native rendering
- **TRACE:** owned Canvas 2D renderer. Price, volume, Gamma/Delta profiles, observed OPRA prints, levels, crosshair, zoom, pan and FOLLOW are controlled by ITM QUANT.
- **Critical 2D analytics:** owned Canvas renderer for compatible bar/scatter/heatmap figures. Plotly remains a locally bundled diagnostic fallback only for complex legacy figures.
- **Multi-field surfaces:** owned WebGL renderer with scientific mesh, ground contours and an explicit confidence/uncertainty shader.
- **WebXR:** optional browser/hardware capability. The button is disabled unless the browser reports immersive support. XR readiness is not a trading signal.

## Coordinated interactions
A shared `itmq:strike-hover` event bus links compatible 2D diagnostics to TRACE. Hovering a strike can therefore highlight the same level inside TRACE instead of forcing the trader to mentally align panels.

## Visual semantics
- Structure: OI / GEX / DEX / walls.
- Dynamics: Spot / IV / time / Vanna / Charm / Speed / Color.
- Observed flow: OPRA prints / contracts / premium / aggressor / Tape.
- Modelled/inferred data always carries model-risk wording.

## Performance rules
1. Ring buffers cap browser memory.
2. Incremental LIVE ticks update native TRACE without rebuilding the entire DOM.
3. Device pixel ratio is capped for predictable GPU/CPU load.
4. Diagnostic figures use native Canvas when their schema is supported.
5. WebGL surface geometry is rebuilt only when the matrix/field changes.

# ITM QUANT — Performance & Interaction Core

## Objective

Improve browser fluency without weakening the quantitative engine. The performance layer has `PRESENTATION_ONLY` authority.

## Non-negotiable invariant

The governor never throttles, samples, drops, rewrites or bypasses:

- Market-data processing in the Quant Core;
- Scanner calculations or directional authority;
- risk / signal / execution logic;
- Gamma, Delta, GEX, DEX or dealer mathematics;
- Tape/Flow causal event logging;
- Auditor, Replay, Calibration or Scanner History persistence.

Only redundant browser-side display work can be deferred or coalesced.

## Final browser path

```text
DATA PROVIDERS
      ↓
EXISTING ITMQ-BINARY-v1 TRANSPORT
      ↓
RUST / PYTHON QUANT CORE
      ↓
AUTHORIZED DISPLAY OUTPUT
      ↓
FRONTEND RING QUEUE
      ↓
HOT DISPLAY STATE
      ↓
DIRTY REGISTRY
      ↓
FRAME SCHEDULER
      ↓
TRACE LAYERS / WEBGPU / CANVAS / SOLIDJS
```

## Ring queue and time budgets

The frontend display bus uses a circular queue instead of repeated `Array.shift()` reindexing. Queue draining is time-budgeted and yields back to the browser so pointer input and `requestAnimationFrame` cannot be starved by an endless microtask chain.

Tick batches can be merged for one browser event while retaining every tick record in the merged batch. This reduces dispatch overhead without removing data from TRACE OHLC/volume construction.

## TRACE scene layers

TRACE uses cached offscreen layers:

1. `base` — background, grid, sessions, structural levels;
2. `price` — candles/line;
3. `options` — Gamma/Delta/profile structures;
4. `flow` — OPRA flow strip and prints;
5. `interaction` — hover, crosshair, linked strike, selection HUD.

A hover move only dirties `interaction`. Zoom/pan change camera geometry and therefore dirty the geometric layers, but they never trigger quantitative recalculation.

## Performance Governor

The governor observes only browser-side indicators:

- frame time / FPS;
- detected display refresh capability;
- presentation queue size;
- long tasks;
- JS heap when the browser exposes it;
- tab visibility.

Modes: `MAX → SMOOTH → BALANCED → PROTECT`.

Mode transitions use hysteresis and dwell time so quality does not oscillate around a threshold.

## Adaptive quality

Only rendering cost is reduced under pressure:

- backdrop blur / decorative shadows;
- nonessential transitions / animations;
- GPU render resolution for Surface and TRACE presentation.

Quantitative data sampling remains unchanged.

## Binary minimal-copy policy

Surface binary frames use a `Float32Array` view over the received WebSocket `ArrayBuffer` when alignment and endianness make that safe. Wasm-decoded views are copied before deferred dispatch because Wasm memory can be overwritten by the next frame. Correct ownership is preferred over a misleading zero-copy claim.

## One transport

v1.25.3 does not create a second market WebSocket. `binary_transport.js` remains the single browser transport for the existing binary tick and Surface routes; decoded display frames are handed to the Performance Core before existing UI events are emitted.

## Authority

`PerformanceGovernor.authority = PRESENTATION_ONLY`.

Scanner remains the only directional authority.

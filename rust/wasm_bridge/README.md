# ITM QUANT WebAssembly Binary Bridge
Build with `wasm-pack build --target web --release`. If the generated package is present,
the browser uses it to decode ITMS binary surface frames into contiguous Wasm memory.
Without a compiled Wasm artifact the runtime uses a DataView/Float32Array fallback and
reports WASM as READY, not ACTIVE.

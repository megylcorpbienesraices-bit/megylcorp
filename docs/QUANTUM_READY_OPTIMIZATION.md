# Quantum-Ready Optimization

ITM QUANT can formulate a small binary attention/portfolio research problem as a QUBO/Ising-compatible matrix. The default solver is a deterministic **classical exact fallback** for small candidate sets.

A real quantum backend is ACTIVE only when `ITM_QPU_PROVIDER` is explicitly configured and a compatible adapter exists. Without that, the UI displays **QUANTUM READY · CLASSICAL FALLBACK**.

This layer is SHADOW and cannot change Scanner direction, entry, target or invalidation.

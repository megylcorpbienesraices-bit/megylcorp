# ITM QUANT · Multi-Asset Schema

## Rule
ITM QUANT is a universal multi-asset engine. DIA/YM/DJX is one ecosystem, not the architecture.

## Instrument metadata
Each supported asset declares canonical symbol, asset class, multiplier/tick size, market calendar/timezone, option root when available, futures/index/ETF relations, volatility proxies and execution/price source metadata.

## Canonical fields
Quant modules consume normalized fields (`spot`, `strike`, `expiry/dte`, `iv`, `oi`, `volume`, Greeks/exposures, flow, timestamps, source health) rather than ticker-specific branches.

## Cross-asset normalization
Related markets are compared on normalized fields using own volatility/sigma when available, robust cross-sectional scale otherwise. Raw percentage moves from unlike assets are not directly summed.

## Missing capabilities
Unsupported data is explicit (`NO_L2`, `NO_OPTIONS`, `NO_FORWARD_CURVE`, etc.) and reduces coverage instead of being synthesized as observed data.

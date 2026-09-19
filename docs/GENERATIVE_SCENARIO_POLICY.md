# Generative Scenario Lab Policy

The Scenario Lab is a **SHADOW research layer**. The local fallback creates reproducible stochastic paths from Spot, local IV, time horizon and optional stress widening.

It reports envelopes, not forecasts. It has no Scanner authority.

## Modes
- `STOCHASTIC_GENERATIVE_FALLBACK`: always local and reproducible.
- External GenAI: may be connected later through a provider adapter. A configured language/generative model may explain scenarios but may not fabricate market data or convert a scenario into a live directional signal.

## Visible model risk
The UI must show that scenarios are theoretical and include IV−, BASE and IV+ views. No generated path is presented as an observed or predicted future price.

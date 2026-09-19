/* ITM QUANT modern chart system
 * Centralizes the visual language of every Plotly chart and adds a non-invasive
 * HUD to native Canvas, TRACE and WebGPU chart hosts.
 */
(() => {
  'use strict';

  const chartMeta = {
    operativaTraceChart: ['TRACE PRO', 'LIVE'],
    scannerChart: ['SCANNER ROUTE', 'LIVE'],
    traceChart: ['TRACE MICROSTRUCTURE', 'LIVE'],
    traceFlowMiniChart: ['FLOW PULSE', 'LIVE'],
    traceDriftMiniChart: ['NET DRIFT', 'LIVE'],
    traceDealerMiniChart: ['DEALER FIELD', 'MODELLED'],
    chainChart: ['OPTIONS STRUCTURE', 'LIVE'],
    flowProChart: ['UNUSUAL FLOW PRO', 'LIVE'],
    netDriftChart: ['NET DRIFT', 'LIVE'],
    exposureChart: ['EXPOSURE STRIKE', 'MODELLED'],
    gexMatrixChart: ['GEX MATRIX', 'MODELLED'],
    volChart: ['VOLATILITY FIELD', 'MODELLED'],
    netPositioningChart: ['NET POSITIONING', 'MODELLED'],
    volumeChart: ['VOLUME PROFILE', 'LIVE'],
    macroChart: ['MACRO CONTEXT', 'EXTERNAL'],
    printsChart: ['LARGE PRINTS', 'LIVE'],
    surfaceChart: ['SURFACE LAB 4D', 'MODELLED'],
    surfaceAltChart: ['CROSS SECTION', 'MODELLED'],
    surfaceSliceChart: ['STRIKE SLICE', 'MODELLED']
  };

  function addHud(hostId) {
    const host = document.getElementById(hostId);
    const meta = chartMeta[hostId];
    if (!host || !meta || host.querySelector('.iq-chart-hud')) return;
    const hud = document.createElement('div');
    hud.className = 'iq-chart-hud';
    hud.dataset.state = meta[1];
    hud.textContent = `${meta[0]} · ${meta[1]} DATA`;
    host.appendChild(hud);
  }

  function addAllHuds() {
    Object.keys(chartMeta).forEach(addHud);
  }

  function initialize() {
    addAllHuds();
    const observer = new MutationObserver(() => addAllHuds());
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('itmq:section-change', () => {
      window.setTimeout(addAllHuds, 60);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
  else initialize();
})();

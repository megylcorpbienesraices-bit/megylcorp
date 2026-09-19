# ITM Aggression Trigger

## Propósito

ITM Aggression Trigger es una **capa de timing de entrada**. No crea la dirección del mercado y no reemplaza al Scanner. Scanner continúa siendo la única autoridad direccional de ITM QUANT.

El objetivo visible es deliberadamente simple: mostrar velas de Delta de Agresión en **1m, 3m, 5m y 15m**.

- **Cian/azul:** domina agresión compradora observada.
- **Fucsia/rojo:** domina agresión vendedora observada.
- **Gris:** no existe ventaja suficientemente confiable.
- La vela con borde/brillo está **LIVE y en formación**; la vela sólida ya cerró.

No se muestran tablas ni métricas dentro de las velas. La complejidad queda en el motor.

## Verdad de mercado

La clasificación usa, en orden:

1. `signed_volume` observado del proveedor cuando existe.
2. `aggressor_sign` observado.
3. Trade ejecutado en Ask o Bid sincronizado.
4. Tick rule, con menor confianza, únicamente como fallback.
5. Si no puede clasificarse, el print queda sin clasificar. **No se inventa agresor.**

La base es:

`Aggressive Buy Volume at Ask - Aggressive Sell Volume at Bid`

Cada intervalo conserva internamente Buy/Sell volume, delta, trayectoria OHLC de delta, cobertura de clasificación, actividad relativa, persistencia y pendiente/aceleración. Esos campos sirven al motor, Auditor/Sophia y pruebas; no cargan la UI normal.

## LIVE antes del cierre

La confirmación de entrada no necesita esperar al cierre total del intervalo. La vela LIVE cambia de estado únicamente cuando supera filtros adaptativos internos de:

- persistencia;
- actividad relativa a la sesión/hora;
- imbalance y delta normalizados;
- cobertura/calidad de clasificación;
- número mínimo de prints utilizables;
- slope/aceleración de la presión.

Un único print no puede voltear el gatillo.

## Jerarquía temporal

- **15m:** contexto/régimen.
- **5m:** desarrollo y confirmación operativa principal.
- **3m:** transición/agote de la presión previa.
- **1m LIVE:** gatillo/sniper.

Una vela 5m roja durante un retroceso no invalida automáticamente una compra si el contexto lo permite, 3m muestra agotamiento vendedor y 1m LIVE confirma compradores en el nivel esperado. La agresión temporiza; Scanner y la estructura deciden la tesis.

## Always-On / Replay

El motor corre independientemente del navegador y forma las velas desde el Tape canónico. Los paquetes históricos READY incluyen los cuatro timeframes precomputados. Los paquetes anteriores a v1.27.0 que no contienen esta capa se actualizan en background; un click de calendario no debe hacer el cálculo pesado.

## Rendimiento

El frontend recibe un payload compacto y dibuja solo unas pocas decenas de velas en Canvas 2D. No se crean paneles por métrica. El cálculo, la clasificación y la persistencia permanecen en backend/VPS.

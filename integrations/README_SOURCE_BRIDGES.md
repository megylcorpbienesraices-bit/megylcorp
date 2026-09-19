# Bridges autorizados v1.14.6

ITM QUANT no intenta evadir licencias ni hacer scraping de páginas privadas.
Los bridges son archivos JSON normalizados escritos por un cliente/add-on que el
usuario tenga autorizado.

- `app/storage/cme_bridge.json`: YM/ES/NQ/GC/VX. Ver ejemplo incluido.
- `app/storage/bookmap_bridge.json`: CVD/profundidad/absorción/liquidez. Ver ejemplo.
- `app/storage/reuters_bridge.json`: titulares/eventos de un feed Reuters licenciado.

Los archivos pueden escribirse de forma atómica (archivo temporal + rename). ITM
solo los lee. Si no existen o están viejos, la interfaz muestra SOURCE PENDING o
STALE y la fuente no pesa en la decisión.

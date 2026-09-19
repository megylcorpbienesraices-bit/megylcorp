# VALIDACIÓN LIVE PRE-PRODUCCIÓN · v1.42.7

Con mercado abierto:

1. **Exposición por vencimiento.** Las barras deben traer valores. Si salen todas a
   cero con OI presente, el frame enriquecido no está llegando: avisar, porque es
   exactamente el defecto que esta versión corrige.
2. **KPIs de exposición.** Concentración coherente con la gráfica: alta cuando
   dos o tres strikes dominan el perfil, baja cuando está repartido. El strike
   dominante debe coincidir con la barra más larga.
3. **IV Rank.** Al arrancar dirá `HISTORIA INSUFICIENTE · N/30`: es correcto, el
   motor está acumulando. Tras unos 30 ciclos debe pasar a `motor · N observaciones`.
   Con Quant Data activo, comparar las dos lecturas: una separación grande no es un
   error, significa que las ventanas no describen lo mismo.
4. **Estadísticas.** CONTRATOS NEGOCIADOS nunca debe ser 0 mientras la cadena tenga
   volumen. La columna FUENTE dirá CINTA con mercado abierto y CADENA fuera de él.
5. **Herramientas del proveedor.** En FUENTES, cada herramienta debe mostrar su
   ruta resuelta. Las que fallen mostrarán la última ruta probada y el error:
   **copiar esa línea** — dice si la ruta es otra o si el plan no la incluye.
6. **Cuota.** `window_source` debe decir DECLARADA si el plan está en `.env`, y el
   intervalo del motor mantenerse estable toda la sesión.
7. **MACRO.** Las tres series del Tesoro dibujadas, la curva con su lectura y el
   calendario de la Fed del día. Rango de sesión encerrando al spot.
8. **ESCENARIOS.** Cuantiles coherentes (p05 < p16 < p50 < p84 < p95). Exposure
   Forecast cambiando de AMORTIGUA a AMPLIFICA al cruzar el flip proyectado.
9. **Sin red externa.** Bloquear la salida a internet salvo los proveedores: la
   terminal debe seguir dibujando entera.
10. En la consola del navegador, `ITMQ.renderErrors` debe seguir vacío tras
    recorrer las once secciones.

11. **Cinta OPRA.** En el log del motor **no debe aparecer**
    `unexpected query parameter(s): feed`. Si aparece con otro parámetro, el cliente
    lo descarta solo y reintenta: la línea sale una vez y no se repite.
12. **Velas tick a tick.** Con el mercado moviéndose, la última vela debe avanzar
    de forma continua, no a saltos. El precio de la cabecera se actualiza con ella.
13. **Interval Map.** Debe salir del motor (`motor · N strikes × M intervalos`) con
    los puntos y la línea de precio. Cambiar la griega a DEX o CHEX repinta el mapa.
14. **Δ GEX VIVO.** En el perfil de TRACE, con el precio moviéndose, esta métrica
    debe traer valores distintos de cero: es la revalorización contra el spot en vivo.
15. **Reproducción.** Botón ⏱ → elegir una sesión → ▶. TRACE, FLUJO, EXPOSICIÓN y
    el resto deben avanzar con el mismo reloj. Comprobar que la barra va en ámbar y
    que `VOLVER A VIVO` restablece el mercado real.

Antes de publicar: `bash scripts/lint_frontend.sh`

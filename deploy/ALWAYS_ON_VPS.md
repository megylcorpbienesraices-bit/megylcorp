# ITM QUANT v1.49.0 — ALWAYS-ON VPS

El navegador es solo la interfaz. El proceso `run_always_on.py` / contenedor mantiene proveedores, cálculo cuantitativo, persistencia, sesión actual y paquetes históricos READY aunque no haya ningún navegador abierto.

## Regla física
Un proceso local no puede trabajar con el PC físicamente apagado. Para continuidad real 24/7, ejecuta este mismo paquete en un VPS/servidor externo encendido permanentemente.

## Flujo reproducible de release y despliegue

### A. Preparación/certificación del candidato (entorno de build con red y toolchains exactos)
1. Parte de un árbol limpio, todavía **no empaquetado**.
2. Instala/usa exactamente `.python-version`, `.node-version` y `rust-toolchain.toml`, además de Docker.
3. Ejecuta `python scripts/prepare_release_assets.py --all`. Esta es la única fase que puede resolver/generar los dos `Cargo.lock` y vendorizar Lightweight Charts; ocurre **antes** de certificar bytes.
4. Revisa los locks/vendor generados y ejecuta `python scripts/prepare_release_assets.py --check`.
5. Ejecuta `python scripts/package_release_artifact.py --output <ruta-fuera-del-repo>.zip`. El empaquetador se niega a escribir bytes si el gate de producción está bloqueado; después ejecuta el gate completo, crea el ZIP determinista, calcula SHA-256 y corre `verify_release_artifact.py` sobre ese ZIP exacto.
6. Conserva el ZIP + sidecars SHA/verificación. **No vuelvas a ejecutar `--all` sobre ese artefacto verificado.**

### B. Despliegue del artefacto exacto en el VPS de producción
1. Copia **ese ZIP ya verificado** al VPS y extráelo en un directorio limpio.
2. No crees `.env` dentro del repositorio. Crea `/etc/itm-quant/itm-quant.env` (o define `ITM_ENV_FILE` con otra ruta absoluta externa), genera `ITM_ACCESS_TOKEN` aleatorio de al menos 32 caracteres y restringe permisos.
3. Instala/usa los toolchains exactos declarados por el artefacto. El VPS de producción **no genera locks ni vendor**: solo los valida.
4. Ejecuta `bash deploy/VALIDAR_Y_DESPLEGAR_VPS.sh`. El script usa venv temporal externo, valida `prepare_release_assets.py --check`, ejecuta `release_gate_full.py --production`, construye la imagen Docker fijada por digest y después arranca Compose.
5. Verifica `docker compose -f docker-compose.always-on.yml ps` y `/healthz`. Compose usa `/etc/itm-quant/itm-quant.env` como default externo seguro y permite override con `ITM_ENV_FILE`.
6. Accede mediante VPN privada o reverse proxy HTTPS. El compose publica únicamente `127.0.0.1:8000` de forma intencional.

## Qué persiste
El volumen `/data` conserva sesiones, Scanner History, Replay, Auditor, calibración, Contract/Session READY y demás memoria cuantitativa. Reiniciar código o contenedor no borra esa memoria.

## Calendar / Session READY
El motor genera en segundo plano paquetes por fecha con `state + charts + tables`. Al escoger una fecha ya preparada, la UI no reconstruye esa sesión en el click path. El Replay intradía con hora exacta conserva el motor causal normal.

## Regla de certificación
No se considera PRODUCCIÓN por el solo hecho de que pytest pase. La promoción exige toolchains exactos, locks nativos reales, auditoría de dependencias, build Rust bloqueado por lock, bundle visual vendorizado/verificado, build Docker real, proveedores/entitlements y validación LIVE/OOS. La certificación técnica no demuestra alpha ni rentabilidad.

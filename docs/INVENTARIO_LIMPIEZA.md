# Inventario de limpieza · v1.58.0

`archivo → función → decisión → evidencia`

El método, antes que el resultado: **nada se borra por parecer residuo**. Cada
fichero de la raíz se comprobó contra seis preguntas —imports y referencias,
llamadas desde `.bat`/Python/JS/configuración, dependencias del arranque, tests,
rutas del frontend/backend, y migraciones o datos persistentes— y sólo se tocó lo
que sale negativo en las seis.

El resultado honesto es que **este repositorio ya estaba limpio**. La auditoría
recorrió 57 ficheros en la raíz y 375 módulos Python: **cero módulos huérfanos**,
cero sufijos de copia (`.bak`, `.orig`, `~`), cero `.bat` apuntando a rutas
inexistentes, cero estáticos sin cargar y ningún fichero de pruebas íntegramente
omitido. Lo que había era tres cosas, y están abajo.

---

## Eliminado

| archivo | función | evidencia |
|---|---|---|
| `WINDOWS_INSTALL_HOTFIX_31210.txt` | Nota de una incidencia de instalación de **v1.40.0** | Cero referencias en todo el árbol. Describe un checkpoint de hace dieciséis versiones; su contenido no aplica a v1.56.0 y no lo consulta ningún script, `.bat`, prueba ni documento. |

## Movido

| archivo | destino | función | evidencia |
|---|---|---|---|
| `LEEME_TASTYTRADE.txt` | `docs/TASTYTRADE_SETUP.txt` | Guía de configuración del proveedor tastytrade | Cero referencias desde código. **No se elimina** porque el proveedor sigue vivo (`app/providers/tastytrade/`, `PROBAR_TASTYTRADE.bat`): es documentación, y la documentación va en `docs/`. |
| `ESTADO_v1.56.0.md` | `docs/operations/` | Estado de la release | El gate de release exige en la raíz `CHANGELOG`, `VALIDACION_PRE_VPS`, `RELEASE_MANIFEST` y `QUANT_ENGINE_AUDIT` (`scripts/release_gate_full.py:161-170`). `ESTADO` **no** está en esa lista; su única mención era una línea del CHANGELOG, actualizada al mover. Va a `docs/operations/` porque `docs/` exige nombres sin versión (`test_v1393_runtime_hygiene.py`), y este documento la lleva por naturaleza. |

## Conservado, con el motivo

Estos aparecían como «sin referencias» en un barrido ingenuo. Los seis controles
demuestran lo contrario, y ésa es exactamente la razón por la que el encargo
pedía comprobarlos antes de borrar.

| archivo | por qué se queda |
|---|---|
| `QUANT_ENGINE_AUDIT_v1.58.0.md`, `VALIDACION_PRE_VPS_v1.58.0.md`, `RELEASE_MANIFEST_v1.58.0.json` | **Los exige el gate de release.** No aparecen en ningún grep porque el nombre se construye con la versión: `ROOT / f"QUANT_ENGINE_AUDIT_v{version}.md"`. Borrarlos rompe el empaquetado. |
| `ABRIR_AUDITOR_DATOS.bat`, `DESINSTALAR_AUTOINICIO_MOTOR_24_7.bat`, `MIGRAR_MEMORIA_ANTERIOR.bat`, `LIMPIAR.bat`, `CERTIFICAR_LIVE.bat` | **Son puntos de entrada.** Nadie los llama desde código porque los abre el operador con doble clic. Un punto de entrada sin referencias internas es lo normal, no un residuo. |
| `requirements-ultralowlatency.txt` | Ruta opcional de aceleración, con su prueba (`tests/test_v1230_ultralowlatency.py`), su documento (`docs/ULTRA_LOW_LATENCY_DEPLOYMENT.md`) y su lanzador. El fichero no se nombra en ellos, pero la función existe: borrarlo dejaría la ruta sin sus dependencias declaradas. |
| `app/core/provider_etf_catalog.py` | Parece legado —lo sustituyó `provider_asset_catalog`— pero éste **importa de él** (`_looks_like_etf`, `_discover_quantdata_etfs`, `load_cached_provider_etfs`). Es una dependencia viva, no un resto. |
| `app/providers/tastytrade/` | El proveedor sigue en el código y en el roster configurable. Que los activos que lo declaraban —YM, MYM, DJX— hayan salido del universo **no retira al proveedor**: retirarlo es otra decisión, y no se toma por inercia de ésta. |
| `/legacy` y sus 19 estáticos | Congelado por decisión explícita del operador hasta que el frontend actual pase la certificación LIVE. Seis pruebas lo fijan en `tests/test_v1575_legacy_congelado.py`. |

## Lo que sí se limpió, y no era un fichero

Residuo **dentro** del código, retirado con el cierre del universo:

- Las fichas de ecosistema de `YM`, `MYM`, `DJX`, `VIX` y `VXD` en
  `app/core/asset_ecosystems.py`: describían una arquitectura retirada.
- Las referencias cruzadas de `DIA`, `XLI` y `XLF` a esos instrumentos:
  apuntaban a cadenas que el programa ya no puede pedir.
- La lista de diagnóstico `("DIA","AAPL","DJX","YM","VIX")` en `app/main.py`,
  que preguntaba por tres símbolos inexistentes.

## Regla para lo que venga

**La raíz no crece.** Si una función pertenece a un módulo que ya existe, va ahí.
Scripts auxiliares a `scripts/`, documentación a `docs/`, pruebas a `tests/`. En
la raíz sólo quedan puntos de entrada, ficheros de dependencias, configuración de
despliegue y los cuatro documentos que el gate de release exige por nombre.

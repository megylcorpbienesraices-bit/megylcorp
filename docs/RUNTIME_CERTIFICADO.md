# RUNTIME CERTIFICADO · ITM QUANT v1.57.1

> La determinación técnica de qué CPython certifica esta release, con la
> evidencia medida y lo que queda pendiente. Lo que aquí se declara es lo que
> `scripts/release_gate_full.py::windows_runtime_guard` exige.

## El problema

El gate exigía `sys.version_info[:3] == (3, 12, 14)` y **en Windows eso no se
puede cumplir**:

* la rama 3.12 está en fase de **solo seguridad**; sus releases se publican
  *source-only* (PEP 693);
* python.org **no distribuye instalador de Windows desde 3.12.10** (abril 2025);
* 3.12.11, 3.12.12, 3.12.13 y 3.12.14 existen, pero sólo como código fuente.

Certificar en Windows obligaba a compilar CPython a mano o a instalar un binario
no oficial. Las dos cosas destruyen exactamente lo que este gate existe para
garantizar: que el entorno de producción es reproducible byte a byte.

### Y el repositorio ya se había contradicho

```
.python-version      3.12.14      ← lo que el gate exigía
INSTALAR_WEB.bat     3.12.10      ← lo que el instalador ponía, escrito a mano
```

En Windows se instalaba 3.12.10 —porque es el último con binario— y después el
gate rechazaba certificar **ese mismo entorno**. El comentario del `.bat` lo
llamaba «Windows hotfix» y dejaba el gate intacto. Nadie lo detectó porque
**ningún control comparaba los dos ficheros**.

## Las dos opciones, medidas

### Opción 1 · certificar Windows con 3.12.10

Disponible, y mínima: los locks cubren `cp312` (38/38 paquetes del lock de
Windows). Pero certifica un intérprete que **no puede recibir ninguna corrección
de seguridad más en Windows en forma de binario**: 3.12.11–3.12.14 son
source-only y la rama seguirá siéndolo hasta su fin de vida en 2028. Sería
congelar Windows en un intérprete de abril de 2025 durante el resto de la vida
del producto.

### Opción 2 · migrar a una rama con binario oficial

La rama 3.13 está en fase de corrección de errores: cada parche se publica con
instalador de Windows. **Y los locks ya la cubren sin regenerar nada.**

## Evidencia

### 1 · Cobertura de ruedas en el lock de Windows, contra PyPI

Para cada uno de los 38 paquetes de `requirements.windows.lock.txt` se comprobó
si el hash de la rueda que instalaría Windows x64 con cada ABI **ya está en el
lock** (pura `-none-any`, `py3-none-win_amd64`, `abi3` o `cp3XX` exacta):

```
cp312:  38/38 cubiertos
cp313:  38/38 cubiertos      ← migrar no exige regenerar el lock
cp314:  37/38 cubiertos      ← falta pandas
```

### 2 · Instalación real de los cinco locks en el intérprete candidato

CPython **3.13.12**, con `--require-hashes --no-deps --only-binary=:all:`, es
decir con verificación de hash y **prohibido compilar desde código fuente**:

```
bootstrap · production · test · rust-bridge     → instalados, 0 errores
numpy 2.3.5 · scipy 1.17.0 · pandas 2.2.3 · pyzmq 27.2.0 · uvloop 0.22.1
```

### 3 · Regresión completa en el intérprete candidato

```
CPython 3.13.12 → 2989 passed, 49 skipped, 0 failed
```

(Dos pruebas que se saltan en 3.11 sí se ejecutan en 3.13: 2989 + 49 = 3038,
el inventario completo.)

### 4 · Por qué no 3.14

`pandas==2.2.3` **no publica rueda `cp314` win_amd64**. La primera versión que la
publica es **2.3.3**. Migrar a 3.14 exige subir pandas y re-resolver los locks
hash-cerrados, con su regresión completa. No es un bloqueo permanente: es una
migración de dependencias que todavía no se ha hecho, y ahora está **nombrada**
con su versión mínima exacta.

## Determinación

**Opción 2.** El runtime certificado pasa a ser **CPython 3.13.12**, la misma
versión en Windows, en el contenedor y en CI:

```
.python-version      3.13.12
.python-runtime.json 3.13.12 · rama 3.13 · certificable en Windows
Dockerfile           python:3.13.12-slim-bookworm@sha256:a58daefb…
INSTALAR_WEB.bat     lee .python-version · ninguna versión escrita a mano
CI                   python-version-file: .python-version
```

El pin es **3.13.12 y no 3.13.15** —la última de la rama— por una razón: 3.13.12
es la versión en la que se ha corrido la regresión. El pin no va por delante de
la evidencia. Moverlo a 3.13.15 es una línea y una re-ejecución de la suite: la
misma rama, el mismo ABI, los mismos locks.

## El control que impide que vuelva a pasar

`windows_runtime_guard()` comprueba, antes de cualquier otra cosa del gate:

1. `.python-version` y `.python-runtime.json` declaran la **misma** versión;
2. su rama está declarada **certificable en Windows**;
3. el parche fijado **no es posterior** al último con instalador de esa rama
   —fijar 3.12.14 cuando el último binario es 3.12.10 es el defecto original—;
4. la declaración **no ha caducado** (`revisar_antes_de`): cuando una rama sale
   de la fase de corrección de errores deja de publicar binarios, y el control
   **vence** en vez de callarse;
5. `INSTALAR_WEB.bat` **no fija ninguna versión a mano**: la lee del mismo
   fichero que el gate.

No baja el control. Antes exigía una versión exacta sin preguntarse si existía
para la plataforma de producción; ahora exige lo mismo **y además** que exista.

## Cuándo hay que volver aquí

`revisar_antes_de` de la rama 3.13 es **2026-10-31**: la rama sale de la fase de
corrección de errores en octubre de 2026 y a partir de ahí sus parches también
serán source-only. Antes de esa fecha hay que recertificar sobre la siguiente
rama en fase de corrección de errores —hoy 3.14—, que exige:

1. subir `pandas` a ≥ 2.3.3 y re-resolver los locks hash-cerrados;
2. comprobar la cobertura de ruedas `cp314` del lock resultante;
3. instalar los locks con hashes en ese intérprete;
4. pasar la regresión completa;
5. y sólo entonces mover `.python-version` y `.python-runtime.json`.

Si esa fecha pasa sin hacerlo, el gate **bloquea el empaquetado** y dice por qué.
Eso es deliberado: es preferible no poder empaquetar a empaquetar contra un
intérprete que Windows no puede instalar.

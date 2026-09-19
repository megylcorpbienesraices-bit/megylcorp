#!/usr/bin/env python3
"""Inventario nominal de la suite (v1.27.8).

Por qué existe
--------------
Hasta v1.27.7 el release gate exigía un total exacto de tests
(``EXPECTED_TOTAL_TESTS = 773``). Eso protege contra la desaparición de tests,
pero tiene dos defectos graves:

1. Bloquea el crecimiento. Añadir un test correcto que pasa hace fallar la
   release con ``numero total de tests inesperado: 774``.
2. Obliga a editar la constante a mano en cada cambio. Ese hábito es
   exactamente el que acaba neutralizando el guardia: cuando desaparezcan tests
   de verdad, la reacción aprendida será subir el número y seguir.

Además, un total agregado es ciego a la compensación: borrar 5 tests y añadir 5
deja el total intacto.

Qué hace en su lugar
--------------------
Registra, por fichero, el conjunto de nombres de test y cuántos identificadores
recolectados aporta cada uno. El gate comprueba tres cosas:

* ningún fichero del inventario ha desaparecido,
* ningún **nombre** de test registrado ha desaparecido (se reporta por nombre),
* ningún fichero ha reducido su número de casos recolectados.

Añadir tests o ficheros nuevos pasa sin tocar nada. El inventario solo se
regenera de forma deliberada con ``--update``, que es un acto consciente y
revisable en el diff.

Uso
---
    python scripts/test_inventory.py --check
    python scripts/test_inventory.py --update
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tests" / "INVENTORY.json"

# tests/test_x.py::TestClase::test_nombre[param-0]
NODE = re.compile(r"^(?P<file>[\w./\\-]+\.py)::(?P<rest>.+)$")


def _base_name(rest: str) -> str:
    """Nombre estable del test: sin parametrización, con clase si la hay."""
    partes = rest.split("::")
    partes[-1] = partes[-1].split("[", 1)[0]
    return "::".join(partes)


def collect() -> dict[str, dict]:
    env = dict(os.environ)
    # Un plugin global del entorno puede alterar la recolección: se aísla igual
    # que en el release gate, para que el inventario sea reproducible.
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    if proc.returncode not in (0, 5):
        sys.stderr.write(proc.stdout[-4000:] + proc.stderr[-2000:])
        raise SystemExit(f"la recolección de tests falló (rc={proc.returncode})")

    nombres: dict[str, set[str]] = defaultdict(set)
    casos: dict[str, int] = defaultdict(int)
    for linea in proc.stdout.splitlines():
        linea = linea.strip()
        m = NODE.match(linea)
        if not m:
            continue
        fichero = m.group("file").replace("\\", "/")
        nombres[fichero].add(_base_name(m.group("rest")))
        casos[fichero] += 1
    if not casos:
        raise SystemExit("no se recolectó ningún test: revisa el entorno")
    return {f: {"tests": sorted(nombres[f]), "casos": casos[f]} for f in sorted(casos)}


def cargar() -> dict:
    if not MANIFEST.exists():
        raise SystemExit(f"falta el inventario {MANIFEST.relative_to(ROOT)}; "
                         "generalo con: python scripts/test_inventory.py --update")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def check() -> int:
    esperado = cargar()
    actual = collect()
    problemas: list[str] = []

    for fichero, datos in esperado.get("ficheros", {}).items():
        if fichero not in actual:
            problemas.append(f"fichero de tests DESAPARECIDO: {fichero}")
            continue
        perdidos = sorted(set(datos["tests"]) - set(actual[fichero]["tests"]))
        if perdidos:
            problemas.append(f"{fichero}: tests desaparecidos -> {perdidos}")
        if actual[fichero]["casos"] < datos["casos"]:
            problemas.append(
                f"{fichero}: casos recolectados {actual[fichero]['casos']} < "
                f"{datos['casos']} registrados (¿parametrización eliminada?)")

    total_actual = sum(d["casos"] for d in actual.values())
    piso = int(esperado.get("total_minimo", 0))
    if total_actual < piso:
        problemas.append(f"total {total_actual} por debajo del suelo {piso}")

    if problemas:
        print("INVENTARIO DE TESTS: FALLO")
        for p in problemas:
            print(f"  - {p}")
        return 1

    nuevos = total_actual - piso
    extra = f" (+{nuevos} desde el último inventario)" if nuevos else ""
    print(f"INVENTARIO DE TESTS: OK · {total_actual} casos en {len(actual)} ficheros{extra}")
    return 0


def update() -> int:
    actual = collect()
    total = sum(d["casos"] for d in actual.values())
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    MANIFEST.write_text(json.dumps(
        {"_nota": "Regenerar solo de forma deliberada: 'python scripts/test_inventory.py --update'. "
                  "El diff debe revisarse — una linea eliminada aqui es un test que dejo de existir.",
         "version": version, "total_minimo": total, "ficheros": actual},
        indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(f"inventario actualizado: {total} casos en {len(actual)} ficheros (v{version})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--check", action="store_true")
    grupo.add_argument("--update", action="store_true")
    args = ap.parse_args()
    return update() if args.update else check()


if __name__ == "__main__":
    raise SystemExit(main())

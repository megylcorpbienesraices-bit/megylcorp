#!/usr/bin/env python3
"""Codemod: `assert version == 'X'` -> `assert_version_at_least('X')`.

Convierte los asserts de release en asserts de comportamiento monótono, para que
un bump de versión deje de romper tests no relacionados.

Uso:
    python scripts/codemod_version_asserts.py --check
    python scripts/codemod_version_asserts.py --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# assert <lo que sea que lea VERSION.txt> == "1.26.2"
VERSION_EQ = re.compile(
    r"""assert\s+.*VERSION\.txt.*?==\s*['"](?P<v>\d+\.\d+(?:\.\d+)?)['"]\s*$""",
    re.MULTILINE,
)
# assert marker["version"] == "1.26.2"  /  assert json.loads(...)["version"] == "..."
MARKER_EQ = re.compile(
    r"""assert\s+.*\[['"]version['"]\]\s*==\s*['"](?P<v>\d+\.\d+(?:\.\d+)?)['"]\s*$""",
    re.MULTILINE,
)
# assert <lee VERSION.txt> in {"1.24.0","1.24.2",...}
# Este patrón es el peor: obliga a APPEND manual en cada release. Se colapsa al mínimo.
VERSION_IN_SET = re.compile(
    r"""assert\s+.*VERSION\.txt.*?\sin\s*\{(?P<set>\s*['"][\d.]+['"](?:\s*,\s*['"][\d.]+['"])*\s*,?\s*)\}\s*$""",
    re.MULTILINE,
)
IMPORT_LINE = "from conftest import assert_version_at_least, assert_marker_version_at_least\n"

# v1.27.3 · HUECO CERRADO
# Las reglas de arriba solo cazaban la versión como VALOR comparado. Dos asserts
# sobrevivieron a v1.27.2 porque la llevaban INCRUSTADA en una cadena:
#
#     assert '/static/ultra_charts.js?v=1.26.2' in text('app/templates/dashboard.html')
#     assert 'version:\'1.26.2\'' in js
#
# Estos no se reescriben automáticamente —el reemplazo correcto depende de si el
# asset usa {{ app_version }} o window.ITMQ_VERSION— pero SÍ se reportan, para que
# el guardia del CI los detenga en vez de dejarlos pasar hasta el siguiente release.
EMBEDDED_VERSION = re.compile(
    r"""assert\s+[^\n]*['"][^'"\n]*\b\d+\.\d+\.\d+\b[^'"\n]*['"][^\n]*\sin\s""",
    re.MULTILINE,
)


def process(path: Path, apply: bool) -> int:
    src = path.read_text(encoding="utf-8")
    original = src
    n = 0

    def _sub(pattern: re.Pattern, fn_name: str, text: str) -> tuple[str, int]:
        count = 0
        while True:
            m = pattern.search(text)
            if not m:
                break
            # text[:m.start()] YA contiene la indentación de la línea: el match
            # empieza en la palabra `assert`. Añadirla otra vez rompía el bloque.
            text = text[: m.start()] + f"{fn_name}({m.group('v')!r})" + text[m.end():]
            count += 1
        return text, count

    # El patrón `in {...}` primero: si no, VERSION_EQ podría morder dentro del set.
    c0 = 0
    while True:
        m = VERSION_IN_SET.search(src)
        if not m:
            break
        versions = re.findall(r"['\"]([\d.]+)['\"]", m.group("set"))
        floor = min(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
        src = src[: m.start()] + f"assert_version_at_least({floor!r})" + src[m.end():]
        c0 += 1

    embedded_matches = []
    for m in EMBEDDED_VERSION.finditer(src):
        line = src[m.start():src.find("\n", m.start()) if src.find("\n", m.start()) >= 0 else len(src)]
        if " not in " in line:
            continue
        # Dependency pins are intentionally versioned and are not application-release
        # assertions. Example: lightweight-charts@5.2.1. The guard only targets
        # hard-coded ITM release/cache-busting strings that should follow VERSION.txt.
        if "lightweight-charts@" in line:
            continue
        embedded_matches.append(m)
    embedded = len(embedded_matches)
    if embedded:
        for m in embedded_matches:
            line_no = src[: m.start()].count("\n") + 1
            print(f"  !! {path.name}:{line_no} versión incrustada en una cadena "
                  f"-> reemplázala por el marcador runtime ({{{{ app_version }}}} / window.ITMQ_VERSION)")

    src, c1 = _sub(VERSION_EQ, "assert_version_at_least", src)
    src, c2 = _sub(MARKER_EQ, "assert_marker_version_at_least", src)
    n = c0 + c1 + c2 + embedded

    if n and "from conftest import" not in src:
        lines = src.splitlines(keepends=True)
        insert_at = 0
        for i, line in enumerate(lines):
            if line.startswith(("import ", "from ")):
                insert_at = i + 1
        lines.insert(insert_at, IMPORT_LINE)
        src = "".join(lines)

    if (c0 + c1 + c2) and apply and src != original:
        path.write_text(src, encoding="utf-8")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.check

    total = files = 0
    for path in sorted(TESTS.glob("test_*.py")):
        n = process(path, apply)
        if n:
            files += 1
            total += n
            print(f"  {path.name}: {n}")
    print(f"\n{total} asserts de versión {'reescritos' if apply else 'detectados'} en {files} archivos.")
    if args.check and total:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

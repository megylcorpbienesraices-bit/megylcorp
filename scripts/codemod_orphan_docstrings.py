#!/usr/bin/env python3
"""Codemod: repara docstrings de módulo huérfanos.

El problema
-----------
68 de 70 módulos de `app/` tienen este orden:

    from __future__ import annotations

    \"\"\"Descripción del módulo...\"\"\"

Como el string NO es la primera sentencia, Python no lo registra como docstring:
`modulo.__doc__` devuelve None. Toda la documentación que escribiste es invisible
para `help()`, para los tooltips del IDE, para `pydoc` y para cualquier
herramienta de documentación. El texto está en el archivo pero no en el objeto.

PEP 236 permite explícitamente que el docstring preceda a `from __future__`, así
que el arreglo es puro reordenamiento:

    \"\"\"Descripción del módulo...\"\"\"

    from __future__ import annotations

Uso:
    python scripts/codemod_orphan_docstrings.py --check
    python scripts/codemod_orphan_docstrings.py --apply
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ["app", "scripts"]


def find_orphan(tree: ast.Module) -> tuple[ast.Expr, ast.ImportFrom] | None:
    """Devuelve (string_huérfano, future_import) si el patrón está presente."""
    body = tree.body
    if len(body) < 2:
        return None
    first = body[0]
    if not (isinstance(first, ast.ImportFrom) and first.module == "__future__"):
        return None
    if ast.get_docstring(tree) is not None:
        return None  # ya tiene docstring real
    for node in body[1:3]:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            return node, first
    return None


def process(path: Path, apply: bool) -> bool:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    found = find_orphan(tree)
    if not found:
        return False
    doc_node, future_node = found

    lines = src.splitlines(keepends=True)
    d0, d1 = doc_node.lineno - 1, (doc_node.end_lineno or doc_node.lineno)
    f0, f1 = future_node.lineno - 1, (future_node.end_lineno or future_node.lineno)
    if d0 < f0:
        return False  # ya está antes; nada que hacer

    doc_block = lines[d0:d1]
    future_block = lines[f0:f1]

    # Reconstruir: docstring, línea en blanco, future import, y el resto sin ambos.
    head = lines[:f0]
    middle = lines[f1:d0]
    tail = lines[d1:]

    # Quitar líneas en blanco sobrantes que quedaban entre future y docstring.
    while middle and not middle[0].strip():
        middle.pop(0)
    while tail and not tail[0].strip():
        tail.pop(0)

    new = head + doc_block + ["\n"] + future_block + ["\n"] + middle + tail
    new_src = "".join(new)

    # Verificación dura: debe seguir parseando Y ahora sí tener docstring.
    try:
        new_tree = ast.parse(new_src)
    except SyntaxError as exc:
        print(f"  !! {path}: el reordenamiento rompe la sintaxis ({exc}); se omite")
        return False
    if ast.get_docstring(new_tree) is None:
        print(f"  !! {path}: tras reordenar sigue sin docstring; se omite")
        return False

    if apply:
        path.write_text(new_src, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.check

    n = 0
    for d in TARGETS:
        base = ROOT / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if process(path, apply):
                n += 1
                print(f"  {path.relative_to(ROOT)}")
    print(f"\n{n} docstrings huérfanos {'reparados' if apply else 'detectados'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

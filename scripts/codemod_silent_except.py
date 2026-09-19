#!/usr/bin/env python3
"""Codemod: convierte handlers silenciosos en degradación registrada.

Cubre `pass`, `continue` y `break` (v1.27.8). Las dos últimas conservan su
semántica de control: solo se antepone el registro.

Por qué AST y no regex
----------------------
`pass` aparece en cuerpos de clase, en `if`, en stubs. Solo queremos los
`ExceptHandler` cuyo cuerpo COMPLETO es un único `Pass`: ese es el patrón que
borra información de fallo sin dejar rastro.

La transformación es mínima e idempotente:

    except Exception:            except Exception as _e:
        pass              ->        _obs_note("modulo:123", _e)

No cambia el flujo de control, no cambia la indentación, no cambia qué se captura.
Solo hace que el fallo sea contable y logueable.

Uso:
    python scripts/codemod_silent_except.py --check      # solo reporta
    python scripts/codemod_silent_except.py --apply      # reescribe
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET_DIRS = ["app"]


def _helper_import(path: Path) -> str:
    """Import relativo correcto a app/core/obs.py desde cualquier profundidad.

    app/service.py              -> from .core.obs import ...
    app/core/engine.py          -> from .obs import ...
    app/providers/tastytrade/x  -> from ...core.obs import ...
    """
    rel = path.relative_to(ROOT / "app").parent          # '' | 'core' | 'providers/tastytrade'
    parts = [p for p in rel.parts if p]
    if parts and parts[0] == "core" and len(parts) == 1:
        return "from .obs import note as _obs_note"
    dots = "." * (len(parts) + 1)
    return f"from {dots}core.obs import note as _obs_note"


# v1.27.8: `pass` no era la unica forma silenciosa. Dentro de un bucle sobre la
# cadena de opciones, `except Exception: continue` descarta filas y encoge una
# superficie GEX/DEX sin dejar rastro — peor que `pass`, porque el resultado
# sigue pareciendo valido. `break` aborta el recorrido entero en silencio.
SILENT_BODIES = (ast.Pass, ast.Continue, ast.Break)


def _tail_keyword(node: ast.stmt) -> str | None:
    """Sentencia de control que hay que CONSERVAR tras registrar la degradacion."""
    if isinstance(node, ast.Continue):
        return "continue"
    if isinstance(node, ast.Break):
        return "break"
    return None


def _silent_handlers(tree: ast.AST) -> list[ast.ExceptHandler]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if len(node.body) != 1 or not isinstance(node.body[0], SILENT_BODIES):
            continue
        out.append(node)
    return out


def _module_tag(path: Path) -> str:
    return path.stem


def process(path: Path, apply: bool) -> tuple[int, list[int]]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"  !! no parsea {path}: {exc}")
        return 0, []
    handlers = _silent_handlers(tree)
    if not handlers:
        return 0, []

    lines = src.splitlines(keepends=True)
    tag = _module_tag(path)
    touched: list[int] = []

    # Procesar de abajo hacia arriba para no invalidar números de línea.
    for h in sorted(handlers, key=lambda n: n.lineno, reverse=True):
        body_stmt = h.body[0]
        tail = _tail_keyword(body_stmt)          # None para `pass`
        p_idx = body_stmt.lineno - 1
        h_idx = h.lineno - 1
        body_line = lines[p_idx]
        indent = body_line[: len(body_line) - len(body_line.lstrip())]
        site = f"{tag}:{h.lineno}"

        if p_idx == h_idx:
            # Forma en una línea:  `except Exception: pass` / `:continue` / `:break`
            head = lines[h_idx]
            stripped = head.rstrip("\n")
            before, _, _ = stripped.partition(":")
            base_indent = head[: len(head) - len(head.lstrip())]
            binder = before if " as " in before else f"{before} as _e"
            rebuilt = f"{binder}:\n{base_indent}    _obs_note({site!r}, _e)\n"
            if tail:
                rebuilt += f"{base_indent}    {tail}\n"
            lines[h_idx] = rebuilt
        else:
            head = lines[h_idx].rstrip("\n")
            if " as " in head:
                name = head.split(" as ", 1)[1].split(":")[0].strip()
            else:
                name = "_e"
                lines[h_idx] = head.replace(":", " as _e:", 1) + "\n"
            replacement = f"{indent}_obs_note({site!r}, {name})\n"
            if tail:
                replacement += f"{indent}{tail}\n"
            lines[p_idx] = replacement
        touched.append(h.lineno)

    new_src = "".join(lines)

    # Inyectar el import una sola vez, después del bloque de imports inicial.
    if "_obs_note" in new_src and "import note as _obs_note" not in new_src:
        imp = _helper_import(path)
        tree2 = ast.parse(new_src)
        insert_at = 0
        for node in tree2.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                insert_at = node.end_lineno or node.lineno
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                insert_at = max(insert_at, node.end_lineno or node.lineno)
        out_lines = new_src.splitlines(keepends=True)
        out_lines.insert(insert_at, imp + "\n")
        new_src = "".join(out_lines)

    if apply:
        path.write_text(new_src, encoding="utf-8")
    return len(handlers), sorted(touched)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.check

    total = 0
    files = 0
    for d in TARGET_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "obs.py":
                continue
            n, lns = process(path, apply)
            if n:
                files += 1
                total += n
                print(f"  {path.relative_to(ROOT)}: {n} handler(s) silencioso(s)")
    verb = "reescritos" if apply else "detectados"
    print(f"\n{total} handlers silenciosos {verb} en {files} archivos.")
    if args.check and total:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

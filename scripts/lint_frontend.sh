#!/usr/bin/env bash
# Análisis estático de los módulos de la terminal.
#
# La regla que importa es no-undef: una variable referenciada pero no declarada
# no es un error de sintaxis, así que `node --check` la deja pasar y sólo
# aparece en tiempo de ejecución, congelando el panel que la contiene. Esto es
# exactamente lo que ocurrió con `moving` en v1.41.0.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FILES=(app/static/itmq_core.js app/static/itmq_trace.js app/static/itmq_orderflow.js
       app/static/itmq_panels.js app/static/itmq_app.js)

for f in "${FILES[@]}"; do
  node --check "$f"
done
echo "SINTAXIS OK (${#FILES[@]} módulos)"

if command -v npx >/dev/null 2>&1; then
  npx --yes eslint@8 --no-eslintrc -c .eslintrc.json "${FILES[@]}"
  echo "ESLINT OK · no-undef limpio"
else
  echo "AVISO: npx no disponible; sólo se comprobó la sintaxis." >&2
fi

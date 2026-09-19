#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

die(){ echo "ERROR: $*" >&2; exit 1; }
need(){ command -v "$1" >/dev/null 2>&1 || die "falta $1"; }

PY_TARGET="$(tr -d '[:space:]' < .python-version)"
NODE_TARGET="$(tr -d '[:space:]' < .node-version)"
NPM_TARGET="$(tr -d '[:space:]' < .npm-version)"
RUST_TARGET="$(sed -n 's/^channel = "\([^"]*\)"/\1/p' rust-toolchain.toml)"
VERSION="$(tr -d '[:space:]' < VERSION.txt)"
OUTPUT="${1:-$ROOT/../ITM_QUANT_v${VERSION}_PRODUCTION_CERTIFIED.zip}"

case "$OUTPUT" in
  "$ROOT"|"$ROOT"/*) die "la salida final debe vivir fuera del árbol del release" ;;
esac

problems=()
command -v python3 >/dev/null 2>&1 || problems+=("Python MISSING != target $PY_TARGET")
command -v node >/dev/null 2>&1 || problems+=("Node MISSING != target $NODE_TARGET")
command -v npm >/dev/null 2>&1 || problems+=("npm MISSING != target $NPM_TARGET")
command -v cargo >/dev/null 2>&1 || problems+=("Cargo MISSING != target $RUST_TARGET")
command -v rustc >/dev/null 2>&1 || problems+=("rustc MISSING != target $RUST_TARGET")
command -v docker >/dev/null 2>&1 || problems+=("Docker MISSING")
command -v git >/dev/null 2>&1 || problems+=("git MISSING")

if command -v python3 >/dev/null 2>&1; then
  PY_ACTUAL="$(python3 -c 'import sys; print(sys.version.split()[0])')"
  [[ "$PY_ACTUAL" == "$PY_TARGET" ]] || problems+=("Python $PY_ACTUAL != target $PY_TARGET")
fi
if command -v node >/dev/null 2>&1; then
  NODE_ACTUAL="$(node --version | sed 's/^v//')"
  [[ "$NODE_ACTUAL" == "$NODE_TARGET" ]] || problems+=("Node $NODE_ACTUAL != target $NODE_TARGET")
fi
if command -v npm >/dev/null 2>&1; then
  NPM_ACTUAL="$(npm --version)"
  [[ "$NPM_ACTUAL" == "$NPM_TARGET" ]] || problems+=("npm $NPM_ACTUAL != target $NPM_TARGET")
fi
if command -v rustc >/dev/null 2>&1; then
  RUST_ACTUAL="$(rustc --version | awk '{print $2}')"
  [[ "$RUST_ACTUAL" == "$RUST_TARGET" ]] || problems+=("rustc $RUST_ACTUAL != target $RUST_TARGET")
fi
if command -v docker >/dev/null 2>&1; then
  docker compose version >/dev/null 2>&1 || problems+=("Docker Compose plugin MISSING")
fi

if ((${#problems[@]})); then
  echo "TOOLCHAIN PREFLIGHT BLOCKED:" >&2
  printf ' - %s\n' "${problems[@]}" >&2
  exit 2
fi

if [[ -e "$OUTPUT" || -e "$OUTPUT.sha256" || -e "$OUTPUT.verify.json" ]]; then
  die "la salida o sus sidecars ya existen; no se sobrescriben"
fi

VENV="$(mktemp -d /tmp/itm-quant-cert-venv.XXXXXX)"
cleanup(){ rm -rf "$VENV"; }
trap cleanup EXIT
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Bootstrap exacto y dependencias hash-locked. Nada se resuelve dentro del gate.
python -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.bootstrap.lock.txt
python -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.production.lock.txt
python -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.test.lock.txt
python -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.rust-bridge.lock.txt
python -m pip check

# Preparación supply-chain revisable: Cargo.lock + Lightweight Charts local con integridad npm.
python scripts/prepare_release_assets.py --all
python scripts/prepare_release_assets.py --check

# Certificación real y fail-closed.
python scripts/release_gate_full.py --production
python scripts/package_release_artifact.py --output "$OUTPUT"

DIGEST="$(sha256sum "$OUTPUT" | awk '{print $1}')"
python scripts/verify_release_artifact.py --zip "$OUTPUT" --sha256 "$DIGEST" --particiones 21 --timeout 180

echo "PASS · ITM QUANT v${VERSION} · ZIP PRODUCCIÓN CERTIFICADO"
echo "ZIP: $OUTPUT"
echo "SHA-256: $DIGEST"

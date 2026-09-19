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
ENV_FILE="${ITM_ENV_FILE:-/etc/itm-quant/itm-quant.env}"

echo "ITM QUANT $(cat VERSION.txt) · validación y despliegue VPS"
[[ "$ENV_FILE" = /* ]] || die "ITM_ENV_FILE debe ser una ruta absoluta fuera del repositorio"
[[ -f "$ENV_FILE" ]] || die "falta archivo de credenciales externo: $ENV_FILE"
case "$ENV_FILE" in "$ROOT"/*) die "ITM_ENV_FILE no puede vivir dentro del repositorio";; esac

need python3
need node
need npm
need cargo
need rustc
need docker
need curl

docker compose version >/dev/null 2>&1 || die "Docker Compose plugin no disponible"

python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1])
vals={}
for raw in path.read_text(encoding='utf-8',errors='replace').splitlines():
    line=raw.strip()
    if line and not line.startswith('#') and '=' in line:
        k,v=line.split('=',1); vals[k.strip()]=v.strip().strip('"').strip("'")
tok=vals.get('ITM_ACCESS_TOKEN','')
if len(tok)<32 or 'PEGAR_AQUI' in tok or 'REEMPLAZAR' in tok:
    raise SystemExit('ITM_ACCESS_TOKEN debe ser aleatorio y tener al menos 32 caracteres')
for key in ('ALPACA_API_KEY','ALPACA_SECRET_KEY'):
    if not vals.get(key) or 'PEGAR_AQUI' in vals.get(key,''):
        raise SystemExit(f'{key} no configurada')
PY

[[ "$(python3 -c 'import sys; print(sys.version.split()[0])')" == "$PY_TARGET" ]] || die "se requiere Python $PY_TARGET exacto"
[[ "$(node --version | sed 's/^v//')" == "$NODE_TARGET" ]] || die "se requiere Node $NODE_TARGET exacto"
[[ "$(npm --version)" == "$NPM_TARGET" ]] || die "se requiere npm $NPM_TARGET exacto"
[[ "$(rustc --version | awk '{print $2}')" == "$RUST_TARGET" ]] || die "se requiere rustc $RUST_TARGET exacto"

# El venv vive FUERA del árbol para no autoinvalidar artifact_cleanliness_guard.
VENV="$(mktemp -d /tmp/itm-quant-release-venv.XXXXXX)"
trap 'rm -rf "$VENV"' EXIT
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.bootstrap.lock.txt
python -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.production.lock.txt
python -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.test.lock.txt
python -m pip install --require-hashes --no-deps --only-binary=:all: -r requirements.rust-bridge.lock.txt
python -m pip check

# Locks/vendor se preparan deliberadamente ANTES de certificación, nunca dentro del gate.
python scripts/prepare_release_assets.py --check
python scripts/release_gate_full.py --production

export ITM_ENV_FILE="$ENV_FILE"
docker compose -f docker-compose.always-on.yml config --quiet
docker compose -f docker-compose.always-on.yml build --pull=false
docker compose -f docker-compose.always-on.yml up -d

for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    echo "PASS · ITM QUANT está levantado y /healthz responde"
    docker compose -f docker-compose.always-on.yml ps
    exit 0
  fi
  sleep 2
done

docker compose -f docker-compose.always-on.yml logs --tail=120 itm-quant || true
die "el contenedor no alcanzó healthz en 60 segundos"

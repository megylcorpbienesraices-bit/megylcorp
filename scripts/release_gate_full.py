"""Single fail-closed release gate for ITM QUANT pre-VPS and production validation."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# v1.57.0 · El gate comprueba que el árbol está limpio de artefactos de build.
# Si sus propios imports y sus subprocesos de pytest dejan `__pycache__` por el
# camino, se suspende a sí mismo. Sólo lee y verifica: no escribe bytecode.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
PRODUCTION_LOCK = ROOT / "requirements.production.lock.txt"
WINDOWS_LOCK = ROOT / "requirements.windows.lock.txt"
TEST_LOCK = ROOT / "requirements.test.lock.txt"
RUST_BRIDGE_LOCK = ROOT / "requirements.rust-bridge.lock.txt"
BOOTSTRAP_LOCK = ROOT / "requirements.bootstrap.lock.txt"
TEST_INVENTORY = ROOT / "tests" / "INVENTORY.json"
EXPECTED_PREVPS_SKIPS = 0
EXPECTED_PRODUCTION_SKIPS = 0

# Complete-suite contract retained for release-integrity tests:
# run(sys.executable,"-m","pytest","-q")

# Pytest isolation is enforced again by verify_release_artifact._entorno().
# Keep it explicit here because release policy must not depend on user-installed plugins.
PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
PYTHONDONTWRITEBYTECODE = "1"

REQUIRED_RUNTIME_MODULES = (
    "fastapi", "starlette", "uvicorn", "jinja2", "multipart", "itsdangerous",
    "pandas", "numpy", "scipy", "plotly", "requests", "websockets",
    "msgpack", "httpx", "zmq",
)


def run(*cmd: str, capture: bool = False, env: dict[str, str] | None = None):
    print("+", *cmd, flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=True, text=True, capture_output=capture, env=env)


def inventory_data() -> dict:
    try:
        data = json.loads(TEST_INVENTORY.read_text(encoding="utf-8"))
        files = data.get("ficheros") or {}
        total = sum(int(v.get("casos", 0)) for v in files.values())
        floor = int(data["total_minimo"])
    except Exception as exc:
        raise SystemExit(f"inventario de tests ilegible ({exc}); regenera con scripts/test_inventory.py --update") from exc
    if total != floor:
        raise SystemExit(f"tests/INVENTORY.json incoherente: suma {total} != total_minimo {floor}")
    return {"raw": data, "total": total, "files": len(files)}


def minimum_total_tests() -> int:
    return int(inventory_data()["total"])


def _requirement_blocks(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        raise SystemExit(f"lock ausente: {path.relative_to(ROOT)}")
    text = path.read_text(encoding="utf-8", errors="strict")
    pattern = re.compile(r"(?m)^(?P<name>[A-Za-z0-9_.-]+)(?:\[[^]]+\])?==(?P<version>[^\s\\]+)")
    matches = list(pattern.finditer(text))
    out: dict[str, dict[str, object]] = {}
    for i, match in enumerate(matches):
        name = match.group("name").lower().replace("_", "-")
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[match.start():end]
        if name in out:
            raise SystemExit(f"lock {path.name} contiene requirement duplicado: {name}")
        hashes = re.findall(r"--hash=sha256:([0-9a-fA-F]{64})", block)
        if not hashes:
            raise SystemExit(f"lock {path.name} sin hash SHA-256 para {name}")
        out[name] = {"version": match.group("version"), "hashes": hashes}
    if not out:
        raise SystemExit(f"lock vacío o ilegible: {path.name}")
    return out


def locked_version(path: Path, package: str) -> str:
    key = package.lower().replace("_", "-")
    data = _requirement_blocks(path)
    if key not in data:
        raise SystemExit(f"{package} no está fijado en {path.name}")
    return str(data[key]["version"])


def lock_structure_guard() -> None:
    production = _requirement_blocks(PRODUCTION_LOCK)
    windows = _requirement_blocks(WINDOWS_LOCK)
    test = _requirement_blocks(TEST_LOCK)
    rust_bridge = _requirement_blocks(RUST_BRIDGE_LOCK)
    bootstrap = _requirement_blocks(BOOTSTRAP_LOCK)

    if set(bootstrap) != {"pip"}:
        raise SystemExit("requirements.bootstrap.lock.txt solo puede fijar pip")
    if bootstrap["pip"]["version"] != test.get("pip", {}).get("version"):
        raise SystemExit("pip bootstrap/test lock desincronizados")

    expected_windows = {k: v["version"] for k, v in production.items() if k != "uvloop"}
    actual_windows = {k: v["version"] for k, v in windows.items()}
    if actual_windows != expected_windows:
        missing = sorted(set(expected_windows) - set(actual_windows))
        extra = sorted(set(actual_windows) - set(expected_windows))
        changed = sorted(k for k in set(actual_windows) & set(expected_windows) if actual_windows[k] != expected_windows[k])
        raise SystemExit(f"Windows lock != production - uvloop; missing={missing}, extra={extra}, changed={changed}")
    if "uvloop" in windows:
        raise SystemExit("requirements.windows.lock.txt no puede incluir uvloop")

    all_locks = {
        "production": production, "windows": windows, "test": test,
        "rust-bridge": rust_bridge, "bootstrap": bootstrap,
    }
    versions: dict[str, set[str]] = {}
    for data in all_locks.values():
        for package, meta in data.items():
            versions.setdefault(package, set()).add(str(meta["version"]))
    conflicts = {k: sorted(v) for k, v in versions.items() if len(v) > 1}
    if conflicts:
        raise SystemExit(f"conflictos de versiones entre locks: {conflicts}")

    if (ROOT / "requirements.txt").read_text(encoding="utf-8").strip() != "-r requirements.production.lock.txt":
        raise SystemExit("requirements.txt debe delegar al lock de producción, no mantener rangos paralelos")
    print(
        "LOCKS PASS · "
        f"production {len(production)} · windows {len(windows)} · test {len(test)} · "
        f"rust-bridge {len(rust_bridge)} · bootstrap {len(bootstrap)}"
    )


def version_sync() -> str:
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    marker = json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    release = str(marker.get("release") or "")
    if str(marker.get("version")) != version or marker.get("scope") != "MULTI_ASSET":
        raise SystemExit("VERSION.txt/.itm_quant_product.json desincronizados")
    if not release.endswith("_PRE_VPS"):
        raise SystemExit(f"marker de release sin sufijo _PRE_VPS: {release!r}")
    package = json.loads((ROOT / "frontend/solid-shell/package.json").read_text(encoding="utf-8"))
    if str(package.get("version")) != version:
        raise SystemExit("frontend/solid-shell/package.json desincronizado")
    for cargo in sorted((ROOT / "rust").glob("*/Cargo.toml")):
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', cargo.read_text(encoding="utf-8"))
        if not match or match.group(1) != version:
            raise SystemExit(f"{cargo.relative_to(ROOT)} desincronizado")
    return version


def release_traceability_guard(version: str) -> None:
    marker = json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    release = str(marker.get("release") or "")
    required = (
        ROOT / f"CHANGELOG_v{version}.md",
        ROOT / f"VALIDACION_PRE_VPS_v{version}.md",
        ROOT / f"RELEASE_MANIFEST_v{version}.json",
        ROOT / f"QUANT_ENGINE_AUDIT_v{version}.md",
        ROOT / "docs" / "operations" / f"VALIDACION_LIVE_PRE_PRODUCCION_v{version}.md",
    )
    missing = [str(p.relative_to(ROOT)) for p in required if not p.is_file()]
    if missing:
        raise SystemExit(f"documentos activos de release ausentes: {missing}")
    expected_tag = f"v{version}"
    for path in (*required[:2], required[3], required[4], ROOT / "README.md"):
        head = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:20])
        if expected_tag not in head:
            raise SystemExit(f"documento activo desincronizado: {path.relative_to(ROOT)}")
    manifest = json.loads(required[2].read_text(encoding="utf-8"))
    if str(manifest.get("version")) != version or str(manifest.get("release")) != release:
        raise SystemExit("RELEASE_MANIFEST activo desincronizado con VERSION/marker")
    inv = inventory_data()
    inventory_total = int(inv["total"])
    inventory_files = int(inv["files"])
    tests = manifest.get("tests") or {}
    if int(tests.get("collected", -1)) != inventory_total or int(tests.get("files", -1)) != inventory_files:
        raise SystemExit("RELEASE_MANIFEST tests collected/files desincronizados con INVENTORY")
    try:
        from verify_release_artifact import _json_hash, plan_particiones
    except ModuleNotFoundError:  # package import in tests/tooling
        from scripts.verify_release_artifact import _json_hash, plan_particiones
    release_plan, release_collected = plan_particiones(ROOT, int(tests.get("partitions", 21)))
    plan_sha256 = _json_hash({"groups": release_plan, "collected": release_collected})
    if release_collected != inventory_total or str(tests.get("plan_sha256") or "") != plan_sha256:
        raise SystemExit("RELEASE_MANIFEST plan_sha256 desincronizado con el plan pytest actual")
    validation = required[1].read_text(encoding="utf-8", errors="replace")
    if f"{inventory_total} casos / {inventory_files} ficheros" not in validation:
        raise SystemExit("VALIDACION_PRE_VPS no refleja el inventario exacto")
    if plan_sha256 not in validation:
        raise SystemExit("VALIDACION_PRE_VPS no refleja el plan_sha256 exacto")
    active = sorted(ROOT.glob("VALIDACION_PRE_VPS_v*.md"))
    if active != [ROOT / f"VALIDACION_PRE_VPS_v{version}.md"]:
        raise SystemExit("debe existir una sola validación PRE-VPS activa en raíz")
    banners = (
        ROOT / "LEEME.txt", ROOT / "deploy" / "ALWAYS_ON_VPS.md",
        ROOT / "PROBAR_PROVEEDORES.bat", ROOT / "PROBAR_TASTYTRADE.bat",
    )
    stale = []
    for path in banners:
        head = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:12])
        if expected_tag not in head:
            stale.append(str(path.relative_to(ROOT)))
    if stale:
        raise SystemExit(f"banners operativos desincronizados: {stale}")


BUILD_RESIDUE_DIRS = {"__pycache__", ".ruff_cache", ".pytest_cache", ".venv-release", "node_modules", "target"}
BUILD_RESIDUE_FILES = {".release_verify_journal.json"}
BUILD_RESIDUE_SUFFIXES = {".pyc", ".pyo", ".new", ".bak-release-prep"}


def _is_build_residue(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in BUILD_RESIDUE_DIRS for part in rel.parts):
        return True
    if rel.as_posix().startswith("frontend/solid-shell/dist/"):
        return True
    if path.name in BUILD_RESIDUE_FILES:
        return True
    if any(path.name.endswith(suffix) for suffix in BUILD_RESIDUE_SUFFIXES):
        return True
    return False


#: Las pruebas que CONGELAN la certificación matemática del Monte Carlo.
#: Cada una contrasta contra una solución cerrada, no contra el propio
#: simulador, así que ninguna puede pasar por accidente.
MONTE_CARLO_GATE = (
    "tests/test_v1550_monte_carlo_audit.py::test_el_precio_esperado_es_el_del_activo_sin_arbitraje",
    "tests/test_v1550_monte_carlo_audit.py::test_la_varianza_terminal_es_la_lognormal_exacta",
    "tests/test_v1550_monte_carlo_audit.py::test_el_mismo_mercado_publica_el_mismo_numero",
    "tests/test_v1550_monte_carlo_audit.py::test_si_el_mercado_cambia_la_simulacion_cambia",
    "tests/test_v1550_monte_carlo_audit.py::test_contar_trayectorias_subestimaria_el_toque_a_la_mitad",
    "tests/test_v1550_monte_carlo_audit.py::test_los_percentiles_terminales_son_los_cuantiles_lognormales",
    "tests/test_v1550_monte_carlo_audit.py::test_el_error_cae_como_uno_partido_por_raiz_de_n",
    "tests/test_v1550_monte_carlo_audit.py::test_tocar_nunca_es_menos_probable_que_terminar_mas_alla",
    "tests/test_v1550_monte_carlo_audit.py::test_entradas_imposibles_no_producen_un_cono_falso",
)


def monte_carlo_math_gate() -> None:
    """Gate matemático del Monte Carlo. Punto 28 del cierre integral.

    ═══════════════════════════════════════════════════════════════════════
    POR QUÉ ESTO ES UN GATE Y NO UNA PRUEBA MÁS
    ═══════════════════════════════════════════════════════════════════════

    El resto de la suite protege el comportamiento. Esto protege la CORRECCIÓN
    MATEMÁTICA, que es distinta: un simulador con la deriva mal puesta corre
    igual de bien, pasa todos los tests de humo y publica probabilidades
    equivocadas con seis decimales de aparente precisión.

    Estas nueve pruebas se contrastan contra soluciones CERRADAS —`N(d₂)`, la
    varianza lognormal exacta, la fórmula de primer paso con reflexión, el
    cuantil lognormal— y contra el comportamiento asintótico del error. Ninguna
    puede pasar por accidente.

    Lo que impiden, en concreto:

        · quitar la semilla determinista, y que el mismo mercado quieto
          publique 43,8 % y un minuto después 44,2 %
        · romper la corrección de puente browniano, y que P(tocar) vuelva a
          salir a la mitad de lo que es
        · romper la convergencia, y que más trayectorias dejen de comprar
          precisión
        · introducir NaN/Inf, o producir un cono con entradas imposibles

    Se ejecutan APARTE de la suite particionada y antes que ella: si la
    matemática está rota, el resto de la validación no significa nada.
    """
    run(sys.executable, "-m", "pytest", "-q", "--no-header", *MONTE_CARLO_GATE)
    print(f"MONTE CARLO MATH GATE PASS · {len(MONTE_CARLO_GATE)} pruebas contra solución cerrada")


def artifact_cleanliness_guard() -> None:
    leaked = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if _is_build_residue(p)]
    if leaked:
        raise SystemExit(f"artefactos de desarrollo/temporal/build empaquetados: {leaked[:25]}")


def cleanup_generated_artifacts(root: Path = ROOT) -> int:
    """Remove only deterministic Python/test caches generated by validation itself."""
    removed = 0
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir() and path.name in {"__pycache__", ".pytest_cache", ".ruff_cache"}:
                shutil.rmtree(path, ignore_errors=True); removed += 1
            elif path.is_file() and path.suffix.lower() in {".pyc", ".pyo"}:
                path.unlink(missing_ok=True); removed += 1
        except OSError:
            pass
    return removed


def secret_file_guard() -> None:
    bad = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.name == ".env" or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            bad.append(str(path.relative_to(ROOT)))
    if bad:
        raise SystemExit(f"secret/private-key files packaged: {bad}")


def _target_python() -> str:
    return (ROOT / ".python-version").read_text(encoding="utf-8").strip()


def _target_node() -> str:
    return (ROOT / ".node-version").read_text(encoding="utf-8").strip()


def _target_npm() -> str:
    return (ROOT / ".npm-version").read_text(encoding="utf-8").strip()


def _target_rust() -> str:
    text = (ROOT / "rust-toolchain.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^channel\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("rust-toolchain.toml sin channel")
    return match.group(1)


def deployment_guard() -> None:
    required = (
        ROOT / "Dockerfile", ROOT / "docker-compose.yml", ROOT / "docker-compose.always-on.yml",
        ROOT / ".dockerignore", ROOT / ".gitignore", ROOT / ".github/workflows/ci.yml",
        ROOT / "deploy/VALIDAR_Y_DESPLEGAR_VPS.sh", ROOT / "scripts/prepare_release_assets.py",
        ROOT / "INSTALAR_WEB.bat", WINDOWS_LOCK, ROOT / "requirements.windows.in",
        ROOT / "LEEME_WINDOWS.txt", BOOTSTRAP_LOCK, ROOT / ".python-version",
        ROOT / ".node-version", ROOT / ".npm-version", ROOT / "rust-toolchain.toml",
    )
    missing = [str(p.relative_to(ROOT)) for p in required if not p.is_file()]
    if missing:
        raise SystemExit(f"deployment/CI files missing: {missing}")
    workflow_dir = ROOT / ".github" / "workflows"
    workflow_files = sorted([*workflow_dir.glob("*.yml"), *workflow_dir.glob("*.yaml")])
    if not workflow_files:
        raise SystemExit("no existen workflows de GitHub Actions")
    for workflow in workflow_files:
        workflow_text = workflow.read_text(encoding="utf-8")
        if re.search(r"uses:\s+[^\s]+@v\d", workflow_text):
            raise SystemExit(f"workflow contiene GitHub Action por tag móvil: {workflow.relative_to(ROOT)}")
        for match in re.finditer(r"uses:\s+[^\s]+@([^\s#]+)", workflow_text):
            ref = match.group(1)
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                raise SystemExit(f"workflow action no fijada a SHA completo: {workflow.relative_to(ROOT)} · {ref}")

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    always = (ROOT / "docker-compose.always-on.yml").read_text(encoding="utf-8")
    vps = (ROOT / "deploy/VALIDAR_Y_DESPLEGAR_VPS.sh").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    if "runs-on: ubuntu-24.04" not in ci:
        raise SystemExit("CI debe fijar ubuntu-24.04")
    for token in ("python-version-file: .python-version", "node-version-file: .node-version", "npm --version", ".npm-version", "release_gate_full.py --production", "docker build --pull=false", "import app.main"):
        if token not in ci:
            raise SystemExit(f"CI incompleto: falta {token}")
    for forbidden in ("cargo generate-lockfile", "npm install --package-lock-only", "rustup toolchain install stable", "npm install --upgrade"):
        if forbidden in ci:
            raise SystemExit(f"CI resuelve dinámicamente durante certificación: {forbidden}")

    base = f"FROM python:{_target_python()}-slim-bookworm@sha256:"
    if base not in dockerfile:
        raise SystemExit("Dockerfile debe fijar Python patch + digest")
    if "COPY --chown=itmquant:itmquant . ." in dockerfile or re.search(r"(?m)^COPY\s+\.\s+\.\s*$", dockerfile):
        raise SystemExit("Dockerfile no puede copiar el repositorio completo")
    for token in ("requirements.bootstrap.lock.txt", "requirements.production.lock.txt", "--require-hashes", "--only-binary=:all:", "frontend/three-adapter"):
        if token not in dockerfile:
            raise SystemExit(f"Dockerfile endurecido incompleto: falta {token}")

    if "${ITM_ENV_FILE:-/etc/itm-quant/itm-quant.env}" not in always:
        raise SystemExit("Compose always-on debe usar env externo seguro por defecto")
    if '"127.0.0.1:8000:8000"' not in always:
        raise SystemExit("Compose always-on debe limitar puerto a loopback")
    if ".venv-release" in vps or "[[ -f .env ]]" in vps or "--env-file .env" in vps:
        raise SystemExit("VPS no puede crear venv/secretos dentro del repositorio")
    for forbidden in ("cargo generate-lockfile", "npm install --package-lock-only"):
        if forbidden in vps:
            raise SystemExit(f"VPS no puede resolver locks durante certificación: {forbidden}")
    for token in ("/etc/itm-quant/itm-quant.env", "mktemp -d /tmp/itm-quant-release-venv", "prepare_release_assets.py --check", "release_gate_full.py --production"):
        if token not in vps:
            raise SystemExit(f"VPS endurecido incompleto: falta {token}")

    required_ignore = (
        ".env", "!.env.example", "app/storage/", ".venv-release/", ".venv/",
        "**/node_modules/", "frontend/solid-shell/dist/", "rust/**/target/", "*.new", "*.bak-release-prep",
    )
    if any(token not in dockerignore for token in required_ignore):
        raise SystemExit(".dockerignore no excluye todos los secretos/build outputs")

    win = (ROOT / "INSTALAR_WEB.bat").read_text(encoding="utf-8", errors="replace")
    for token in ("py -3.12 -m venv .venv", "requirements.bootstrap.lock.txt", "requirements.windows.lock.txt", "--require-hashes --no-deps --only-binary=:all:"):
        if token not in win:
            raise SystemExit(f"instalador Windows no endurecido: falta {token}")
    if "requirements.production.lock.txt" in win:
        raise SystemExit("Windows no debe instalar el lock Linux")

    sophia = (ROOT / "deploy/INSTALL_SOPHIA_LOCAL_WINDOWS.ps1").read_text(encoding="utf-8", errors="replace")
    for token in ("requirements.sophia-local.lock.txt", "--require-hashes --no-deps --only-binary=:all:", "requirements-sophia-local.in"):
        if token not in sophia:
            raise SystemExit(f"instalador Sophia local no fail-closed: falta {token}")
    for forbidden in ("pip install --upgrade pip", "winget install", "ollama pull"):
        if forbidden in sophia:
            raise SystemExit(f"instalador Sophia local contiene resolución/descarga flotante: {forbidden}")
    print("DEPLOYMENT CONTRACT PASS")


def _installed_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def toolchain_preflight(production: bool) -> list[str]:
    problems: list[str] = []
    expected_pip = locked_version(BOOTSTRAP_LOCK, "pip")
    actual_pip = _installed_version("pip")
    if actual_pip != expected_pip:
        problems.append(f"pip {actual_pip or 'MISSING'} != lock {expected_pip}")
    for package in ("pip-audit", "ruff"):
        expected = locked_version(TEST_LOCK, package)
        actual = _installed_version(package)
        if actual != expected:
            problems.append(f"{package} {actual or 'MISSING'} != lock {expected}")
    if not production:
        return problems

    actual_py = ".".join(map(str, sys.version_info[:3]))
    if actual_py != _target_python():
        problems.append(f"Python {actual_py} != target {_target_python()}")
    node = shutil.which("node")
    npm = shutil.which("npm")
    if not node:
        problems.append(f"Node MISSING != target {_target_node()}")
    else:
        node_v = subprocess.check_output([node, "--version"], text=True).strip().lstrip("v")
        if node_v != _target_node():
            problems.append(f"Node {node_v} != target {_target_node()}")
    if not npm:
        problems.append(f"npm MISSING != target {_target_npm()}")
    else:
        npm_v = subprocess.check_output([npm, "--version"], text=True).strip()
        if npm_v != _target_npm():
            problems.append(f"npm {npm_v} != target {_target_npm()}")
    rustc = shutil.which("rustc")
    cargo = shutil.which("cargo")
    if not rustc or not cargo:
        problems.append(f"Rust/Cargo MISSING != target {_target_rust()}")
    else:
        rust_v = subprocess.check_output([rustc, "--version"], text=True).split()[1]
        if rust_v != _target_rust():
            problems.append(f"rustc {rust_v} != target {_target_rust()}")
    if not shutil.which("docker"):
        problems.append("Docker MISSING")
    expected_zmq = locked_version(RUST_BRIDGE_LOCK, "pyzmq")
    actual_zmq = _installed_version("pyzmq")
    if actual_zmq != expected_zmq:
        problems.append(f"pyzmq {actual_zmq or 'MISSING'} != lock {expected_zmq}")
    for cargo_manifest in sorted((ROOT / "rust").glob("*/Cargo.toml")):
        lock = cargo_manifest.with_name("Cargo.lock")
        if not lock.is_file():
            problems.append(f"{lock.relative_to(ROOT)} MISSING")
    vendor_files = (
        ROOT / "app/static/vendor/lightweight-charts.standalone.production.js",
        ROOT / "app/static/vendor/LIGHTWEIGHT_CHARTS_LICENSE",
        ROOT / "app/static/vendor/lightweight-charts.lock.json",
    )
    for path in vendor_files:
        if not path.is_file():
            problems.append(f"{path.relative_to(ROOT)} MISSING")
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8", errors="replace")
    main = (ROOT / "app/main.py").read_text(encoding="utf-8", errors="replace")
    if "unpkg.com" in html or "cdn.jsdelivr.net" in html or "unpkg.com" in main or "cdn.jsdelivr.net" in main:
        problems.append("runtime todavía contiene CDN ejecutable (Lightweight Charts)")
    return problems


def dependency_audit() -> None:
    expected = locked_version(TEST_LOCK, "pip-audit")
    actual = _installed_version("pip-audit")
    if actual != expected:
        raise SystemExit(f"pip-audit exacto requerido: {expected}; encontrado {actual or 'MISSING'}")
    command = [sys.executable, "-m", "pip_audit", "-r", PRODUCTION_LOCK.name, "--no-deps", "--disable-pip", "--strict", "--progress-spinner", "off"]
    print("+", *command, flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode:
        raise SystemExit(f"auditoría de dependencias falló (pip-audit exit {result.returncode})")
    print("DEPENDENCY AUDIT PASS · 0 vulnerabilidades no permitidas")


def ruff_guard() -> None:
    expected = locked_version(TEST_LOCK, "ruff")
    actual = _installed_version("ruff")
    if actual != expected:
        raise SystemExit(f"Ruff exacto requerido: {expected}; encontrado {actual or 'MISSING'}")
    run(sys.executable, "-m", "ruff", "check", "app", "scripts", "tests", "--select", "E9,F63,F7,F82")


def production_runtime_guard(production: bool) -> None:
    if not production:
        return
    from prepare_release_assets import validate_cargo_lock, validate_optional_package_lock, validate_vendor
    for manifest in sorted((ROOT / "rust").glob("*/Cargo.toml")):
        validate_cargo_lock(manifest.with_name("Cargo.lock"))
    validate_optional_package_lock(ROOT / "frontend/solid-shell/package-lock.json")
    validate_vendor()
    missing = []
    for module in REQUIRED_RUNTIME_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:
            missing.append(f"{module}: {exc}")
    if missing:
        raise SystemExit("production requiere runtime crítico: " + "; ".join(missing))
    run(sys.executable, "-m", "pip", "check")


def check_junit(path: Path, production: bool) -> tuple[int, int, int, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag.endswith("testsuite") else list(root.iter("testsuite"))
    total = passed = skipped = failed = 0
    reasons = []
    for suite in suites:
        total += int(suite.attrib.get("tests", 0))
        skipped += int(suite.attrib.get("skipped", 0))
        failed += int(suite.attrib.get("failures", 0)) + int(suite.attrib.get("errors", 0))
        for case in suite.iter("testcase"):
            if case.find("failure") is None and case.find("error") is None and case.find("skipped") is None:
                passed += 1
            item = case.find("skipped")
            if item is not None:
                reasons.append((item.attrib.get("message") or "").lower())
    expected_skips = EXPECTED_PRODUCTION_SKIPS if production else EXPECTED_PREVPS_SKIPS
    if skipped > expected_skips:
        raise SystemExit(f"release con skips prohibidos: {reasons[:8] or skipped}")
    minimo = minimum_total_tests()
    if total < minimo:
        raise SystemExit(f"la suite ENCOGIÓ: {total}; inventario {minimo}")
    if failed:
        raise SystemExit(f"suite con {failed} fallos")
    return total, passed, skipped, failed


def partitioned_pytest_gate(production: bool) -> tuple[int, int, int, int]:
    from verify_release_artifact import PartitionTimeout, ejecutar_particion, plan_particiones
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", PYTEST_DISABLE_PLUGIN_AUTOLOAD)
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", PYTHONDONTWRITEBYTECODE)
    partitions = max(1, int(os.getenv("ITM_RELEASE_TEST_PARTITIONS", "21")))
    timeout_s = max(30, int(os.getenv("ITM_RELEASE_TEST_TIMEOUT_SEC", "180")))
    plan, collected = plan_particiones(ROOT, partitions)
    minimum = minimum_total_tests()
    if collected < minimum:
        raise SystemExit(f"la suite ENCOGIÓ: {collected}; inventario {minimum}")
    total = passed = skipped = failed = errors = 0
    for idx, files in enumerate(plan, start=1):
        print(f"+ pytest partition {idx}/{len(plan)} · {len(files)} files", flush=True)
        try:
            result = ejecutar_particion(ROOT, idx, files, timeout_s)
        except PartitionTimeout as exc:
            diagnostic = "\n".join(exc.log_tail[-40:]) if exc.log_tail else ""
            raise SystemExit(
                f"partición pytest {idx} excedió {timeout_s}s; files={exc.files}\n{diagnostic}"
            ) from exc
        total += int(result.get("total", 0)); passed += int(result.get("passed", 0))
        skipped += int(result.get("skipped", 0)); failed += int(result.get("failed", 0)); errors += int(result.get("error", 0))
        if not result.get("ok") or result.get("skips_no_auditados"):
            raise SystemExit(f"partición pytest {idx} falló: {result.get('diagnostico','')[-2500:]}")
    if total != collected:
        raise SystemExit(f"suite particionada incompleta: {total}/{collected}")
    expected_skips = EXPECTED_PRODUCTION_SKIPS if production else EXPECTED_PREVPS_SKIPS
    if skipped > expected_skips:
        raise SystemExit(f"release con skips prohibidos: {skipped}")
    if failed or errors:
        raise SystemExit(f"suite con {failed} fallos y {errors} errores")
    print(f"SUITE PARTICIONADA: {passed} passed · 0 skipped · 0 failed · 0 errors · {total} collected")
    return total, passed, skipped, failed + errors


def _js_guard(production: bool) -> int:
    node = shutil.which("node")
    js_files = sorted((ROOT / "app/static").rglob("*.js"))
    if not node:
        if production:
            raise SystemExit("production requiere Node.js")
        print("! Node.js ausente: sintaxis JS diferida")
        return 0
    if production:
        actual = subprocess.check_output([node, "--version"], text=True).strip().lstrip("v")
        if actual != _target_node():
            raise SystemExit(f"Node {actual} != target {_target_node()}")
    for js in js_files:
        run(node, "--check", str(js.relative_to(ROOT)))
    print(f"JAVASCRIPT PASS · {len(js_files)}/{len(js_files)}")
    return len(js_files)


def _rust_guard(production: bool) -> None:
    cargo = shutil.which("cargo")
    manifests = sorted((ROOT / "rust").glob("*/Cargo.toml"))
    if not cargo:
        if production:
            raise SystemExit("production requiere Cargo")
        print("! Cargo ausente: compilación Rust diferida al VPS")
        return
    with tempfile.TemporaryDirectory(prefix="itm-cargo-target-") as td:
        env = dict(os.environ); env["CARGO_TARGET_DIR"] = td
        for manifest in manifests:
            args = [cargo, "check", "--locked", "--quiet", "--manifest-path", str(manifest.relative_to(ROOT))]
            run(*args, env=env)
            if production:
                run(cargo, "build", "--locked", "--release", "--manifest-path", str(manifest.relative_to(ROOT)), env=env)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    try:
        version = version_sync()
        release_traceability_guard(version)
        artifact_cleanliness_guard()  # dirty input must fail; do not silently clean it first
        secret_file_guard()
        deployment_guard()
        lock_structure_guard()

        problems = toolchain_preflight(args.production)
        if problems:
            raise SystemExit("TOOLCHAIN PREFLIGHT BLOCKED:\n - " + "\n - ".join(problems))
        dependency_audit()
        production_runtime_guard(args.production)
        ruff_guard()

        run(sys.executable, "-m", "compileall", "-q", "app", "scripts", "tests")
        for guard in ("codemod_silent_except.py", "codemod_version_asserts.py", "codemod_orphan_docstrings.py"):
            run(sys.executable, f"scripts/{guard}", "--check")
        run(sys.executable, "scripts/test_inventory.py", "--check")
        # La matemática primero: si está rota, el resto de la validación no
        # significa nada.
        monte_carlo_math_gate()
        _js_guard(args.production)
        _rust_guard(args.production)
        partitioned_pytest_gate(args.production)

        cleanup_generated_artifacts()
        artifact_cleanliness_guard()
        mode = "PRODUCTION" if args.production else "PRE-VPS"
        print(f"FULL RELEASE GATE PASS · v{version} · {mode}")
        return 0
    finally:
        cleanup_generated_artifacts()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Hash-bound, resumable, strict verifier for an exact ITM QUANT release artifact."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

DEFAULT_JOURNAL = Path(".release_verify_journal.json")
NODE = re.compile(r"^(?P<file>[\w./\\-]+\.py)::")


class PartitionTimeout(subprocess.TimeoutExpired):
    def __init__(self, cmd, timeout, *, partition: int, files: list[str], log_tail: list[str]):
        super().__init__(cmd, timeout)
        self.partition = partition
        self.files = list(files)
        self.log_tail = list(log_tail)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _json_hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _entorno() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ITM_ACCELERATOR_BACKEND"] = "NUMPY"
    return env


def environment_identity() -> dict[str, object]:
    packages = sorted(
        f"{(dist.metadata.get('Name') or '').lower()}=={dist.version}"
        for dist in importlib.metadata.distributions() if dist.metadata.get("Name")
    )
    try:
        pytest_version = importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        pytest_version = "MISSING"
    return {
        "python": sys.version,
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "pytest": pytest_version,
        "packages_sha256": _json_hash(packages),
    }


def _unsafe_packaged_name(name: str) -> str | None:
    pure = PurePosixPath(name.replace("\\", "/"))
    parts = pure.parts
    lower = [p.lower() for p in parts]
    if pure.is_absolute() or ".." in parts:
        return "ruta insegura"
    if "__pycache__" in parts or any(p in {".pytest_cache", ".ruff_cache", ".venv-release", ".venv", "node_modules", "target"} for p in parts):
        return "artefacto de desarrollo/temporal/build"
    if len(parts) >= 3 and parts[-3:] == ("frontend", "solid-shell", "dist"):
        return "artefacto de desarrollo/temporal/build"
    if name.endswith((".pyc", ".pyo", ".new", ".bak-release-prep")) or pure.name == ".release_verify_journal.json":
        return "artefacto de desarrollo/temporal/build"
    if pure.name == ".env" or (pure.name.startswith(".env.") and pure.name != ".env.example"):
        return "secret file"
    if pure.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        return "private key file"
    return None


def zip_preflight(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        bad_crc = archive.testzip()
        if bad_crc:
            raise SystemExit(f"ZIP CRC falló: {bad_crc}")
        bad = []
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                bad.append((name, "symlink")); continue
            reason = _unsafe_packaged_name(name)
            if reason:
                bad.append((name, reason))
        if bad:
            raise SystemExit(f"ZIP contiene desarrollo/temporal, secreto o ruta insegura: {bad[:20]}")
    print("ZIP PREFLIGHT PASS · CRC + paths + secrets + build residues")


def safe_extract(zip_path: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts:
                raise SystemExit(f"ZIP inseguro: ruta fuera del artefacto: {info.filename!r}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise SystemExit(f"ZIP inseguro: symlink no permitido: {info.filename!r}")
            target = (root / Path(*pure.parts)).resolve()
            if target != root and root not in target.parents:
                raise SystemExit(f"ZIP inseguro: extracción fuera del destino: {info.filename!r}")
        archive.extractall(root)


def resolve_tree(extracted: Path) -> Path:
    roots = [p for p in extracted.iterdir() if p.is_dir() and not p.name.startswith("__MACOSX")]
    if len(roots) == 1 and (roots[0] / "tests").is_dir():
        return roots[0]
    return extracted


def plan_particiones(tree: Path, n: int) -> tuple[list[list[str]], int]:
    if n < 1:
        raise SystemExit("--particiones debe ser >= 1")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=tree, env=_entorno(), capture_output=True, text=True,
    )
    if proc.returncode not in (0, 5):
        sys.stderr.write(proc.stdout[-3000:] + proc.stderr[-1500:])
        raise SystemExit(f"la recolección falló (rc={proc.returncode})")
    cases: dict[str, int] = defaultdict(int)
    for line in proc.stdout.splitlines():
        match = NODE.match(line.strip())
        if match:
            cases[match.group("file").replace("\\", "/")] += 1
    if not cases:
        raise SystemExit("no se recolectó ningún test")
    items = list(cases.items())
    group_count = min(n, len(items))
    groups: list[list[str]] = []
    cursor = 0
    remaining_weight = sum(cases.values())
    for group_idx in range(group_count):
        remaining_groups = group_count - group_idx
        if remaining_groups == 1:
            groups.append([name for name, _ in items[cursor:]]); break
        target = remaining_weight / remaining_groups
        group: list[str] = []
        load = 0
        max_take = len(items) - cursor - (remaining_groups - 1)
        while len(group) < max_take:
            name, weight = items[cursor]
            if group and abs(load - target) <= abs((load + weight) - target):
                break
            group.append(name); load += weight; cursor += 1
        if not group:
            name, weight = items[cursor]; group.append(name); load += weight; cursor += 1
        groups.append(group); remaining_weight -= load
    return groups, sum(cases.values())


def inventory_identity(tree: Path) -> dict[str, object]:
    path = tree / "tests" / "INVENTORY.json"
    if not path.is_file():
        raise SystemExit("falta tests/INVENTORY.json")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode())
        files = data.get("ficheros") or {}
        minimum = int(data["total_minimo"])
        actual = sum(int(v.get("casos", 0)) for v in files.values())
    except Exception as exc:
        raise SystemExit(f"inventario de tests ilegible: {exc}") from exc
    if actual != minimum:
        raise SystemExit(f"inventario incoherente: suma {actual} != total_minimo {minimum}")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "total_minimo": minimum, "files": len(files)}


def structural_artifact_guard(tree: Path) -> None:
    version = (tree / "VERSION.txt").read_text(encoding="utf-8").strip()
    marker = json.loads((tree / ".itm_quant_product.json").read_text(encoding="utf-8"))
    if marker.get("version") != version or marker.get("scope") != "MULTI_ASSET" or not str(marker.get("release", "")).endswith("_PRE_VPS"):
        raise SystemExit("identidad VERSION/marker inválida dentro del artefacto")
    inventory = inventory_identity(tree)
    manifest_path = tree / f"RELEASE_MANIFEST_v{version}.json"
    validation_path = tree / f"VALIDACION_PRE_VPS_v{version}.md"
    if not manifest_path.is_file() or not validation_path.is_file():
        raise SystemExit("faltan manifest/validación activos dentro del artefacto")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tests = manifest.get("tests") or {}
    if manifest.get("version") != version or manifest.get("release") != marker.get("release"):
        raise SystemExit("manifest desincronizado dentro del artefacto")
    if int(tests.get("collected", -1)) != int(inventory["total_minimo"]) or int(tests.get("files", -1)) != int(inventory["files"]):
        raise SystemExit("manifest/inventario desincronizados dentro del artefacto")
    validation = validation_path.read_text(encoding="utf-8", errors="replace")
    if f"{inventory['total_minimo']} casos / {inventory['files']} ficheros" not in validation:
        raise SystemExit("validación PRE-VPS no refleja inventario dentro del artefacto")
    active = sorted(tree.glob("VALIDACION_PRE_VPS_v*.md"))
    if active != [validation_path]:
        raise SystemExit("debe existir una sola validación PRE-VPS activa")

    required = (
        tree / "requirements.bootstrap.lock.txt", tree / ".python-version", tree / ".node-version",
        tree / "rust-toolchain.toml", tree / "Dockerfile", tree / ".github/workflows/ci.yml",
        tree / "deploy/VALIDAR_Y_DESPLEGAR_VPS.sh",
        tree / "rust/causality_engine/Cargo.lock", tree / "rust/wasm_bridge/Cargo.lock",
        tree / "app/static/vendor/lightweight-charts.standalone.production.js",
        tree / "app/static/vendor/LIGHTWEIGHT_CHARTS_LICENSE",
        tree / "app/static/vendor/lightweight-charts.lock.json",
    )
    missing = [str(p.relative_to(tree)) for p in required if not p.is_file()]
    if missing:
        raise SystemExit("estructura de producción incompleta en ZIP: " + ", ".join(missing))
    html = (tree / "app/templates/dashboard.html").read_text(encoding="utf-8", errors="replace")
    main = (tree / "app/main.py").read_text(encoding="utf-8", errors="replace")
    if "unpkg.com" in html or "cdn.jsdelivr.net" in html or "unpkg.com" in main or "cdn.jsdelivr.net" in main:
        raise SystemExit("ZIP conserva CDN ejecutable")
    if "/static/vendor/lightweight-charts.standalone.production.js" not in html:
        raise SystemExit("ZIP no usa Lightweight Charts local")
    ci = (tree / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    if "cargo generate-lockfile" in ci or "npm install --package-lock-only" in ci:
        raise SystemExit("CI dentro del ZIP resuelve locks durante certificación")
    print("STRUCTURAL ARTIFACT GUARD PASS")


def plan_traceability_guard(tree: Path, plan: list[list[str]], collected: int) -> str:
    version = (tree / "VERSION.txt").read_text(encoding="utf-8").strip()
    manifest_path = tree / f"RELEASE_MANIFEST_v{version}.json"
    validation_path = tree / f"VALIDACION_PRE_VPS_v{version}.md"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tests = manifest.get("tests") or {}
    plan_sha256 = _json_hash({"groups": plan, "collected": collected})
    if int(tests.get("partitions", -1)) != len(plan):
        raise SystemExit("manifest partitions desincronizado con el plan exacto")
    if str(tests.get("plan_sha256") or "") != plan_sha256:
        raise SystemExit("manifest plan_sha256 desincronizado con el plan exacto")
    validation = validation_path.read_text(encoding="utf-8", errors="replace")
    if plan_sha256 not in validation:
        raise SystemExit("validación PRE-VPS no contiene el plan_sha256 exacto")
    print(f"PLAN TRACEABILITY PASS · {plan_sha256}")
    return plan_sha256


def validation_identity(artifact_hash: str, plan: list[list[str]], collected: int, inventory: dict[str, object]) -> dict[str, object]:
    environment = environment_identity()
    return {
        "artifact_sha256": artifact_hash,
        "plan_sha256": _json_hash({"groups": plan, "collected": collected}),
        "environment_sha256": _json_hash(environment),
        "inventory_sha256": inventory["sha256"],
        "collected": collected,
        "particiones_totales": len(plan),
        "environment": environment,
    }


def leer_diario(path: Path, identity: dict[str, object]) -> dict[str, object]:
    if path.exists():
        try: old = json.loads(path.read_text(encoding="utf-8"))
        except Exception: old = {}
        keys = ("artifact_sha256", "plan_sha256", "environment_sha256", "inventory_sha256", "collected", "particiones_totales")
        if all(old.get(k) == identity.get(k) for k in keys):
            return old
        if old:
            print("! identidad de validación cambió: se invalida el diario previo")
    return {**identity, "resultados": {}}


def guardar_diario(path: Path, journal: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(journal, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def parse_junit(path: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag.endswith("testsuite") else list(root.iter("testsuite"))
    total = passed = skipped = failed = errors = 0
    reasons: list[str] = []
    for suite in suites:
        total += int(suite.attrib.get("tests", 0)); skipped += int(suite.attrib.get("skipped", 0))
        failed += int(suite.attrib.get("failures", 0)); errors += int(suite.attrib.get("errors", 0))
        for case in suite.iter("testcase"):
            skip = case.find("skipped")
            if skip is not None:
                reasons.append((skip.attrib.get("message") or skip.text or "").strip().lower())
            elif case.find("failure") is None and case.find("error") is None:
                passed += 1
    # Any skip blocks certification. Historical retired-surface skips no longer count as PASS.
    return {
        "total": total, "passed": passed, "skipped": skipped, "failed": failed, "error": errors,
        "skip_reasons": reasons, "skips_no_auditados": reasons[:8],
    }


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None: return
    if os.name == "nt":
        try: subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except Exception:
            try: proc.kill()
            except Exception: pass
    else:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try: proc.kill()
            except Exception: pass
    try: proc.wait(timeout=5)
    except Exception: pass


def _cleanup_process_group_after_success(proc: subprocess.Popen) -> None:
    if os.name == "nt": return
    try:
        os.killpg(proc.pid, signal.SIGTERM); time.sleep(0.05)
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    except Exception: pass


def ejecutar_particion(tree: Path, idx: int, files: list[str], timeout_s: int) -> dict[str, object]:
    started = time.time()
    with tempfile.TemporaryDirectory(prefix=f"itm_part_{idx}_") as td:
        junit = Path(td) / "pytest.xml"; log_path = Path(td) / "pytest.log"
        cmd = [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", f"--junitxml={junit}", *files]
        kwargs: dict[str, object] = {"cwd": tree, "env": _entorno(), "stderr": subprocess.STDOUT}
        if os.name == "nt": kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else: kwargs["start_new_session"] = True
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            kwargs["stdout"] = log
            proc = subprocess.Popen(cmd, **kwargs)
            try:
                returncode = proc.wait(timeout=timeout_s); _cleanup_process_group_after_success(proc)
            except subprocess.TimeoutExpired as exc:
                _kill_process_tree(proc); log.flush()
                tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
                raise PartitionTimeout(cmd, timeout_s, partition=idx, files=files, log_tail=tail) from exc
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if not junit.is_file():
            return {"particion": idx, "ficheros": len(files), "file_plan_sha256": _json_hash(files), "total": 0, "passed": 0, "skipped": 0, "failed": 0, "error": 1, "skips_no_auditados": [], "ok": False, "segundos": round(time.time()-started,1), "cuando": datetime.now(timezone.utc).isoformat(timespec="seconds"), "diagnostico": log_text[-4000:]}
        metrics = parse_junit(junit)
        ok = returncode == 0 and metrics["failed"] == 0 and metrics["error"] == 0 and metrics["skipped"] == 0
        return {"particion": idx, "ficheros": len(files), "file_plan_sha256": _json_hash(files), **{k: metrics[k] for k in ("total","passed","skipped","failed","error","skips_no_auditados")}, "ok": ok, "segundos": round(time.time()-started,1), "cuando": datetime.now(timezone.utc).isoformat(timespec="seconds"), "diagnostico": "" if ok else log_text[-4000:]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--zip", type=Path)
    source.add_argument("--tree", type=Path)
    ap.add_argument("--sha256")
    ap.add_argument("--particiones", type=int, default=21)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--diario", type=Path, default=DEFAULT_JOURNAL)
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.zip:
            artifact = args.zip.resolve()
            if not artifact.is_file(): raise SystemExit(f"no existe artefacto: {artifact}")
            zip_preflight(artifact)
            digest = sha256(artifact)
            if args.sha256:
                expected = args.sha256.strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise SystemExit("--sha256 debe contener 64 hex")
                if digest != expected:
                    raise SystemExit(f"SHA-256 no coincide: esperado {expected}; calculado {digest}")
            tmp = tempfile.TemporaryDirectory(prefix="itm_verify_")
            safe_extract(artifact, Path(tmp.name)); tree = resolve_tree(Path(tmp.name))
        else:
            tree = args.tree.resolve()
            if not args.sha256 or not re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256.strip()):
                raise SystemExit("--tree exige --sha256 de 64 hex")
            digest = args.sha256.strip().lower()

        if not (tree / "tests").is_dir(): raise SystemExit(f"no encuentro tests/ en {tree}")
        structural_artifact_guard(tree)
        print(f"artefacto  : {digest}")
        print(f"árbol      : {tree}")
        plan, collected = plan_particiones(tree, args.particiones)
        plan_traceability_guard(tree, plan, collected)
        inventory = inventory_identity(tree)
        if collected < int(inventory["total_minimo"]):
            raise SystemExit(f"la suite ENCOGIÓ: {collected}; inventario {inventory['total_minimo']}")
        identity = validation_identity(digest, plan, collected, inventory)
        journal = leer_diario(args.diario.resolve(), identity); done = journal.setdefault("resultados", {})
        print(f"tests      : {collected} · inventario {inventory['total_minimo']}")
        print(f"particiones: {len(plan)} · verificadas {sum(bool(v.get('ok')) for v in done.values())}/{len(plan)}")
        if args.status:
            for key in sorted(done, key=int):
                r=done[key]; print(f"  #{key}: {r.get('passed',0)} PASS · {r.get('skipped',0)} SKIP · {r.get('failed',0)} FAIL · {r.get('error',0)} ERROR")
            return 0
        for idx, files in enumerate(plan, start=1):
            key=str(idx); previous=done.get(key)
            if previous and previous.get("ok") and previous.get("file_plan_sha256") == _json_hash(files):
                print(f"  #{idx}: ya verificada ({previous['passed']} PASS)"); continue
            print(f"  #{idx}: ejecutando {len(files)} ficheros…", flush=True)
            try:
                result=ejecutar_particion(tree,idx,files,args.timeout)
            except PartitionTimeout as exc:
                done[key] = {"ok": False, "timeout": True, "files": exc.files, "diagnostico": "\n".join(exc.log_tail)}
                guardar_diario(args.diario.resolve(), journal)
                print(f"  #{idx}: TIMEOUT {args.timeout}s · files={exc.files}")
                if exc.log_tail: print("\n".join(exc.log_tail))
                return 2
            done[key]=result; guardar_diario(args.diario.resolve(),journal)
            print(f"  #{idx}: {'OK' if result['ok'] else 'FALLO'} · {result['passed']} PASS · {result['skipped']} SKIP · {result['failed']} FAIL · {result['error']} ERROR · {result['segundos']}s")
            if not result["ok"]:
                if result.get("diagnostico"): print(result["diagnostico"])
                print("VEREDICTO: FALLO"); return 1
        complete=len(done)==len(plan) and all(v.get("ok") for v in done.values())
        passed=sum(int(v["passed"]) for v in done.values()); skipped=sum(int(v["skipped"]) for v in done.values())
        failed=sum(int(v["failed"]) for v in done.values()); errors=sum(int(v["error"]) for v in done.values()); total=sum(int(v["total"]) for v in done.values())
        print("-"*68); print(f"TOTAL: {passed} PASS · {skipped} SKIP · {failed} FAIL · {errors} ERROR · {total} casos")
        if total != collected or not complete or skipped or failed or errors:
            print("VEREDICTO: INCOMPLETO/FALLO"); return 2
        print(f"VEREDICTO: ARTEFACTO VERIFICADO AL COMPLETO · sha256 {digest}")
        return 0
    finally:
        if tmp is not None: tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())

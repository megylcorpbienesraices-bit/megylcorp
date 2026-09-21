#!/usr/bin/env python3
"""Deterministically package and exact-byte verify an ITM QUANT release.

This script is the *only* supported final ZIP path. It never resolves dependencies
or native/browser assets. Those must already be reviewed and present. Packaging is
fail-closed: the full production gate must pass before a ZIP byte is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
ZIP_COMPRESSION = zipfile.ZIP_STORED  # cross-host byte reproducibility; no zlib dependency

# The packager must not dirty the tree merely by importing its release guards.
#
# v1.57.0 · LA GUARDA SE LEVANTABA DEMASIADO PRONTO.
#
# Esto protegía SÓLO el import de `release_gate_full` y devolvía la bandera a su
# sitio a continuación. Pero `release_traceability_guard()` importa
# `verify_release_artifact` de forma perezosa, ya dentro del preflight y con la
# bandera restaurada, así que escribía
# `scripts/__pycache__/verify_release_artifact.cpython-3XX.pyc`… y la línea
# siguiente, `artifact_cleanliness_guard()`, rechazaba el árbol por contener un
# artefacto de build. Sobre un árbol limpio la comprobación fallaba por algo que
# se acababa de escribir ella misma, y no había forma de que pasara nunca.
#
# El empaquetador sólo LEE y COMPRIME: no tiene ningún motivo para dejar
# bytecode en el árbol que está a punto de sellar. Así que la bandera se pone y
# se queda puesta durante toda la ejecución, y viaja también por el entorno para
# que los subprocesos —el gate completo lanza pytest por particiones— no
# ensucien lo que luego se va a hashear.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.path.insert(0, str(SCRIPTS))
import release_gate_full as gate  # noqa: E402


class PackageError(SystemExit):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _forbidden_path(path: Path) -> str | None:
    rel = path.relative_to(ROOT)
    parts = rel.parts
    if any(p in {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".venv-release", "node_modules", "target"} for p in parts):
        return "development/build residue"
    if len(parts) >= 3 and parts[:3] == ("frontend", "solid-shell", "dist"):
        return "optional frontend build output"
    if path.name.endswith((".pyc", ".pyo", ".new", ".bak-release-prep")):
        return "temporary residue"
    if path.name == ".release_verify_journal.json":
        return "verification journal"
    if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        return "secret file"
    if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        return "private-key material"
    return None


def release_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(ROOT.rglob("*"), key=lambda p: p.relative_to(ROOT).as_posix()):
        if path.is_symlink():
            raise PackageError(f"symlink no permitido en artefacto: {path.relative_to(ROOT)}")
        if not path.is_file():
            continue
        reason = _forbidden_path(path)
        if reason:
            raise PackageError(f"árbol no empaquetable: {path.relative_to(ROOT)} · {reason}")
        files.append(path)
    if not files:
        raise PackageError("árbol vacío")
    return files


def production_preflight() -> list[str]:
    """Cheap preflight only; the actual package path still runs the full gate."""
    gate.version_sync()
    gate.release_traceability_guard((ROOT / "VERSION.txt").read_text(encoding="utf-8").strip())
    gate.artifact_cleanliness_guard()
    gate.secret_file_guard()
    gate.deployment_guard()
    gate.lock_structure_guard()
    return gate.toolchain_preflight(True)


def _normalized_mode(path: Path) -> int:
    rel = path.relative_to(ROOT).as_posix()
    executable = rel.endswith(".sh") or (rel.startswith("scripts/") and path.suffix == ".py")
    return 0o755 if executable else 0o644


def write_deterministic_zip(output: Path, files: list[Path]) -> None:
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    prefix = f"ITM_QUANT_v{version}/"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Store entries without deflate. This intentionally trades a few MB for
    # cross-host byte reproducibility: ZIP bytes no longer depend on the host zlib build.
    with zipfile.ZipFile(output, "w", compression=ZIP_COMPRESSION, strict_timestamps=True) as zf:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(prefix + rel, date_time=FIXED_ZIP_TIME)
            info.compress_type = ZIP_COMPRESSION
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | _normalized_mode(path)) << 16
            info.flag_bits |= 0x800  # UTF-8 names
            zf.writestr(info, path.read_bytes(), compress_type=ZIP_COMPRESSION)


def run_full_production_gate() -> None:
    cmd = [sys.executable, str(SCRIPTS / "release_gate_full.py"), "--production"]
    print("+", *cmd, flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def verify_exact_zip(output: Path, digest: str, journal: Path) -> None:
    cmd = [
        sys.executable,
        str(SCRIPTS / "verify_release_artifact.py"),
        "--zip", str(output),
        "--sha256", digest,
        "--particiones", "21",
        "--timeout", "180",
        "--diario", str(journal),
    ]
    print("+", *cmd, flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def _sidecar_paths(output: Path) -> tuple[Path, Path]:
    return (
        output.with_suffix(output.suffix + ".sha256"),
        output.with_suffix(output.suffix + ".verification.json"),
    )


def _rollback_publication(output: Path) -> None:
    for path in (output, *_sidecar_paths(output)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def write_sidecars(output: Path, digest: str, journal: Path) -> None:
    sha_path, verification_path = _sidecar_paths(output)
    sha_path.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    sidecar = {
        "artifact": output.name,
        "sha256": digest,
        "size_bytes": output.stat().st_size,
        "verified": True,
        "verification_journal": journal.name,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Sidecar only; it is not embedded in the already-verified ZIP bytes.",
    }
    verification_path.write_text(
        json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def package(output: Path) -> tuple[Path, str]:
    output = output.resolve()
    try:
        output.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise PackageError("el ZIP final debe escribirse fuera del árbol de release")
    collisions = [path for path in (output, *_sidecar_paths(output)) if path.exists()]
    if collisions:
        raise PackageError("salida/sidecar ya existe; no se sobrescribe: " + ", ".join(map(str, collisions)))

    blockers = production_preflight()
    if blockers:
        raise PackageError("PACKAGING BLOCKED:\n - " + "\n - ".join(blockers))

    # Expensive certification is deliberately last before bytes are created.
    run_full_production_gate()
    gate.artifact_cleanliness_guard()
    files = release_files()

    tmp = output.with_name(output.name + ".new")
    journal = output.with_suffix(output.suffix + ".verify.json")
    if tmp.exists():
        tmp.unlink()
    if journal.exists():
        journal.unlink()
    published = False
    try:
        write_deterministic_zip(tmp, files)
        digest = sha256_file(tmp)
        verify_exact_zip(tmp, digest, journal)
        os.replace(tmp, output)
        published = True
        # Digest is invariant under rename; verify again defensively.
        if sha256_file(output) != digest:
            raise PackageError("SHA-256 cambió después de publicar el ZIP")
        write_sidecars(output, digest, journal)
        return output, digest
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        if published:
            _rollback_publication(output)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="solo comprobar si el packaging final está desbloqueado")
    parser.add_argument("--output", type=Path, help="ruta final del ZIP, siempre fuera del árbol")
    args = parser.parse_args()

    blockers = production_preflight()
    if args.check:
        if blockers:
            print("PACKAGING BLOCKED")
            for item in blockers:
                print(" -", item)
            return 2
        print("PACKAGING PREFLIGHT PASS")
        return 0
    if not args.output:
        parser.error("--output es obligatorio salvo con --check")
    if blockers:
        raise PackageError("PACKAGING BLOCKED:\n - " + "\n - ".join(blockers))
    output, digest = package(args.output)
    print(f"FINAL ZIP VERIFIED · {output} · sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

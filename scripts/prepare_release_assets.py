#!/usr/bin/env python3
"""Prepare release-only native locks and vendored browser assets outside certification.

This script is intentionally separate from release_gate_full.py.  Certification
must consume reviewed, immutable locks/assets; it must never resolve them.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
VENDOR_VERSION = "5.2.1"
VENDOR_DIR = ROOT / "app" / "static" / "vendor"
VENDOR_JS = VENDOR_DIR / "lightweight-charts.standalone.production.js"
VENDOR_LICENSE = VENDOR_DIR / "LIGHTWEIGHT_CHARTS_LICENSE"
VENDOR_LOCK = VENDOR_DIR / "lightweight-charts.lock.json"
DASHBOARD = ROOT / "app" / "templates" / "dashboard.html"
MAIN = ROOT / "app" / "main.py"
CARGO_MANIFESTS = (
    ROOT / "rust" / "causality_engine" / "Cargo.toml",
    ROOT / "rust" / "wasm_bridge" / "Cargo.toml",
)


def die(msg: str) -> "NoReturn":
    raise SystemExit(msg)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _display_path(path: Path) -> str:
    """Stable diagnostic path for repo files and temporary staging files."""
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


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
        die("rust-toolchain.toml sin channel")
    return match.group(1)


def require_exact_toolchains(*, need_node: bool = False, need_rust: bool = False) -> None:
    actual_py = ".".join(map(str, sys.version_info[:3]))
    if actual_py != _target_python():
        die(f"Python exacto requerido: {_target_python()}; encontrado {actual_py}")
    if need_node:
        node = shutil.which("node")
        npm = shutil.which("npm")
        if not node or not npm:
            die("vendorization requiere node + npm")
        node_v = subprocess.check_output([node, "--version"], text=True).strip().lstrip("v")
        if node_v != _target_node():
            die(f"Node exacto requerido: {_target_node()}; encontrado {node_v}")
        npm_v = subprocess.check_output([npm, "--version"], text=True).strip()
        if npm_v != _target_npm():
            die(f"npm exacto requerido: {_target_npm()}; encontrado {npm_v}")
    if need_rust:
        cargo = shutil.which("cargo")
        rustc = shutil.which("rustc")
        if not cargo or not rustc:
            die("generación de Cargo.lock requiere cargo + rustc")
        rust_v = subprocess.check_output([rustc, "--version"], text=True).split()[1]
        if rust_v != _target_rust():
            die(f"Rust exacto requerido: {_target_rust()}; encontrado {rust_v}")


def validate_cargo_lock(path: Path) -> None:
    if not path.is_file():
        die(f"falta {_display_path(path)}")
    text = path.read_text(encoding="utf-8", errors="strict")
    if not re.search(r'(?m)^version\s*=\s*[34]\s*$', text):
        die(f"{_display_path(path)}: lock format inesperado")
    packages = text.split("[[package]]")[1:]
    if not packages:
        die(f"{_display_path(path)}: sin paquetes")
    for pkg in packages:
        sm = re.search(r'(?m)^source\s*=\s*"([^"]+)"', pkg)
        cm = re.search(r'(?m)^checksum\s*=\s*"([0-9a-fA-F]+)"', pkg)
        if sm:
            source = sm.group(1)
            allowed = source.startswith("registry+https://github.com/rust-lang/crates.io-index") or source.startswith("sparse+https://index.crates.io/")
            if not allowed:
                die(f"{_display_path(path)}: source no permitido: {source}")
            if not cm or not re.fullmatch(r"[0-9a-fA-F]{64}", cm.group(1)):
                die(f"{_display_path(path)}: paquete registry sin checksum SHA-256")
        elif "git+" in pkg or "http://" in pkg:
            die(f"{_display_path(path)}: dependencia remota no registry")


def validate_optional_package_lock(path: Path) -> None:
    """Validate Solid reference lock if present; it is not a production requirement."""
    if not path.is_file():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("lockfileVersion", 0)) != 3:
        die("frontend/solid-shell/package-lock.json debe usar lockfileVersion 3")
    for key, pkg in (data.get("packages") or {}).items():
        if not isinstance(pkg, dict):
            continue
        resolved = str(pkg.get("resolved") or "")
        integrity = str(pkg.get("integrity") or "")
        if resolved and not resolved.startswith("https://registry.npmjs.org/"):
            die(f"package-lock opcional contiene resolved no permitido: {resolved}")
        if resolved and not integrity.startswith("sha512-"):
            die(f"package-lock opcional sin integridad SHA-512: {key}")


def _vendor_lock_payload(js: bytes, license_bytes: bytes, dashboard: bytes, main: bytes, integrity: str) -> dict[str, str]:
    return {
        "package": "lightweight-charts",
        "version": VENDOR_VERSION,
        "source": "npm-registry",
        "dist_integrity": integrity,
        "bundle_sha256": sha256_bytes(js),
        "license_sha256": sha256_bytes(license_bytes),
        "dashboard_sha256": sha256_bytes(dashboard),
        "main_sha256": sha256_bytes(main),
    }


def validate_vendor() -> None:
    missing = [p for p in (VENDOR_JS, VENDOR_LICENSE, VENDOR_LOCK) if not p.is_file()]
    if missing:
        die("vendor Lightweight Charts pendiente: " + ", ".join(str(p.relative_to(ROOT)) for p in missing))
    lock = json.loads(VENDOR_LOCK.read_text(encoding="utf-8"))
    if lock.get("package") != "lightweight-charts" or str(lock.get("version")) != VENDOR_VERSION:
        die("vendor lock de Lightweight Charts desincronizado")
    if not str(lock.get("dist_integrity") or "").startswith("sha512-"):
        die("vendor lock sin dist.integrity SHA-512")
    expected = {
        "bundle_sha256": sha256_file(VENDOR_JS),
        "license_sha256": sha256_file(VENDOR_LICENSE),
        "dashboard_sha256": sha256_file(DASHBOARD),
        "main_sha256": sha256_file(MAIN),
    }
    for key, value in expected.items():
        if lock.get(key) != value:
            die(f"vendor lock hash mismatch: {key}")
    html = DASHBOARD.read_text(encoding="utf-8")
    main = MAIN.read_text(encoding="utf-8")
    if "/static/vendor/lightweight-charts.standalone.production.js" not in html:
        die("dashboard no usa el bundle local de Lightweight Charts")
    if "unpkg.com" in html or "cdn.jsdelivr.net" in html or "unpkg.com" in main or "cdn.jsdelivr.net" in main:
        die("runtime conserva CDN ejecutable para Lightweight Charts")


def _publish_transaction(items: dict[Path, bytes]) -> None:
    """Best-effort all-or-rollback publication. Residues are release blockers."""
    backups: dict[Path, Path | None] = {}
    staged: dict[Path, Path] = {}
    try:
        for dst, data in items.items():
            dst.parent.mkdir(parents=True, exist_ok=True)
            new = dst.with_name(dst.name + ".new")
            bak = dst.with_name(dst.name + ".bak-release-prep")
            new.write_bytes(data)
            staged[dst] = new
            if dst.exists():
                if bak.exists():
                    bak.unlink()
                dst.replace(bak)
                backups[dst] = bak
            else:
                backups[dst] = None
        for dst, new in staged.items():
            new.replace(dst)
        for bak in backups.values():
            if bak and bak.exists():
                bak.unlink()
    except BaseException:
        for dst, bak in backups.items():
            try:
                if dst.exists():
                    dst.unlink()
                if bak and bak.exists():
                    bak.replace(dst)
            except Exception:
                pass
        # Remove every unpublished staging/backup residue as part of rollback.
        for new in staged.values():
            try:
                new.unlink(missing_ok=True)
            except OSError:
                pass
        for bak in backups.values():
            if bak:
                try:
                    bak.unlink(missing_ok=True)
                except OSError:
                    pass
        raise


def prepare_cargo_locks() -> None:
    require_exact_toolchains(need_rust=True)
    cargo = shutil.which("cargo")
    assert cargo
    published: dict[Path, bytes] = {}
    with tempfile.TemporaryDirectory(prefix="itm-release-locks-") as td:
        stage = Path(td)
        for manifest in CARGO_MANIFESTS:
            crate_src = manifest.parent
            crate_dst = stage / crate_src.name
            shutil.copytree(crate_src, crate_dst, ignore=shutil.ignore_patterns("target", "Cargo.lock"))
            staged_manifest = crate_dst / "Cargo.toml"
            subprocess.run([cargo, "generate-lockfile", "--manifest-path", str(staged_manifest)], check=True, cwd=ROOT)
            lock = crate_dst / "Cargo.lock"
            validate_cargo_lock(lock)
            published[crate_src / "Cargo.lock"] = lock.read_bytes()
    _publish_transaction(published)
    print("Cargo locks preparados y publicados transaccionalmente")


def _safe_extract_tar(tar_path: Path, dest: Path) -> None:
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                die(f"tar npm inseguro: {member.name}")
            if member.issym() or member.islnk():
                die(f"tar npm contiene link no permitido: {member.name}")
            if not (member.isfile() or member.isdir()):
                die(f"tar npm contiene tipo especial no permitido: {member.name}")
        tf.extractall(dest, filter="data")


def _verify_npm_tarball_integrity(tar_path: Path, integrity: str) -> None:
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        die("dist.integrity debe ser SHA-512")
    algo, b64 = integrity.split("-", 1)
    if algo != "sha512":
        die("integridad npm inesperada")
    try:
        expected = base64.b64decode(b64, validate=True)
    except Exception:
        die("dist.integrity SHA-512 no es base64 válido")
    actual = hashlib.sha512(tar_path.read_bytes()).digest()
    if actual != expected:
        die("tarball npm no coincide con dist.integrity")


def _publish_vendor_from_verified_tarball(tar_path: Path, integrity: str) -> None:
    _verify_npm_tarball_integrity(tar_path, integrity)
    with tempfile.TemporaryDirectory(prefix="itm-lightweight-charts-unpack-") as td:
        extracted = Path(td) / "unpacked"
        extracted.mkdir()
        _safe_extract_tar(tar_path, extracted)
        package = extracted / "package"
        package_json = package / "package.json"
        if not package_json.is_file():
            die("tarball npm sin package.json")
        metadata = json.loads(package_json.read_text(encoding="utf-8"))
        if metadata.get("name") != "lightweight-charts" or str(metadata.get("version")) != VENDOR_VERSION:
            die("tarball npm no corresponde a lightweight-charts@" + VENDOR_VERSION)
        js_path = package / "dist" / "lightweight-charts.standalone.production.js"
        if not js_path.is_file():
            die("tarball npm sin bundle standalone production")
        js = js_path.read_bytes()
        license_path = package / "LICENSE"
        if not license_path.is_file():
            die("tarball npm sin LICENSE")
        license_bytes = license_path.read_bytes()

        old_html = DASHBOARD.read_text(encoding="utf-8")
        local_tag = f'<script src="/static/vendor/lightweight-charts.standalone.production.js?v={VENDOR_VERSION}"></script>'
        remote_re = re.compile(r'<script[^>]+src="https://unpkg\.com/lightweight-charts@5\.2\.1/dist/lightweight-charts\.standalone\.production\.js"[^>]*></script>')
        if remote_re.search(old_html):
            new_html = remote_re.sub(local_tag, old_html, count=1)
        elif "/static/vendor/lightweight-charts.standalone.production.js" in old_html:
            new_html = old_html
        else:
            die("no encuentro el tag de Lightweight Charts en dashboard")

        old_main = MAIN.read_text(encoding="utf-8")
        new_main = old_main.replace(" https://unpkg.com https://cdn.jsdelivr.net", "")
        if "unpkg.com" in new_main or "cdn.jsdelivr.net" in new_main:
            die("quedó un CDN ejecutable en app/main.py")

        payload = _vendor_lock_payload(js, license_bytes, new_html.encode(), new_main.encode(), integrity)
        lock_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        _publish_transaction({
            VENDOR_JS: js,
            VENDOR_LICENSE: license_bytes,
            VENDOR_LOCK: lock_bytes,
            DASHBOARD: new_html.encode(),
            MAIN: new_main.encode(),
        })
    validate_vendor()


def prepare_vendor(*, tar_path: Path | None = None, integrity: str | None = None) -> None:
    if tar_path is None:
        require_exact_toolchains(need_node=True)
        npm = shutil.which("npm")
        assert npm
        with tempfile.TemporaryDirectory(prefix="itm-lightweight-charts-") as td:
            stage = Path(td)
            integrity_raw = subprocess.check_output(
                [npm, "view", f"lightweight-charts@{VENDOR_VERSION}", "dist.integrity", "--json"],
                cwd=stage,
                text=True,
            ).strip()
            integrity = json.loads(integrity_raw)
            if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
                die("npm no devolvió dist.integrity SHA-512")
            pack_raw = subprocess.check_output(
                [npm, "pack", f"lightweight-charts@{VENDOR_VERSION}", "--json", "--ignore-scripts"],
                cwd=stage,
                text=True,
            )
            pack = json.loads(pack_raw)
            filename = pack[0]["filename"]
            tar_path = stage / filename
            _publish_vendor_from_verified_tarball(tar_path, integrity)
    else:
        if not tar_path.is_file():
            die(f"tarball npm no existe: {tar_path}")
        if not integrity:
            die("--vendor-tar requiere --vendor-integrity")
        _publish_vendor_from_verified_tarball(tar_path, integrity)
    print("Lightweight Charts vendorizado y runtime/CSP migrados a local")


def check() -> None:
    for manifest in CARGO_MANIFESTS:
        validate_cargo_lock(manifest.with_name("Cargo.lock"))
    validate_optional_package_lock(ROOT / "frontend" / "solid-shell" / "package-lock.json")
    validate_vendor()
    print("PREPARED RELEASE ASSETS PASS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--locks", action="store_true")
    ap.add_argument("--vendor", action="store_true")
    ap.add_argument("--vendor-tar", type=Path, help="tarball npm predescargado para preparación offline verificada")
    ap.add_argument("--vendor-integrity", help="dist.integrity oficial sha512-... del tarball npm")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if bool(args.vendor_tar) != bool(args.vendor_integrity):
        die("--vendor-tar y --vendor-integrity deben usarse juntos")
    if not any((args.check, args.locks, args.vendor, args.all, args.vendor_tar)):
        args.check = True
    if args.all or args.locks:
        prepare_cargo_locks()
    if args.all or args.vendor or args.vendor_tar:
        prepare_vendor(tar_path=args.vendor_tar, integrity=args.vendor_integrity)
    if args.check or args.all:
        check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Persistent data layer for ITM QUANT.

v1.21.0 keeps quantitative memory outside replaceable application code and makes
legacy migration *operator-selected*.  The application never scans sibling folders
and never guesses which old installation is authoritative.

Safety principles
-----------------
1. New code can be replaced; accumulated LIVE memory is not reset.
2. In-place legacy ``app/storage`` is trusted because it belongs to the running tree.
3. Side-by-side migration requires an explicit source path.
4. Modern packages are authenticated with an ITM QUANT product marker.
5. Pre-marker packages (<= v1.17) are accepted only when explicitly selected and
   when a strong legacy fingerprint matches the ITM QUANT application layout.
6. Every merge is backed up first and never overwrites a different persistent file.
7. A no-source bootstrap does not consume the one-time migration opportunity.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional
import hashlib
import json
import os
import shutil
import zipfile
from .core.obs import note as _obs_note
from .version import APP_VERSION

TARGET_VERSION = APP_VERSION
# v1.42: la identidad vuelve a ser multi-activo. El identificador NO decide dónde
# viven los datos (eso es `default_persistent_root`, que no lo usa), así que el
# cambio no reubica nada; sólo se mantiene el id anterior como legado para que el
# marcador de una instalación v1.41 siga validando al importarla.
PRODUCT_ID = "com.itmquant.multiasset"
PRODUCT_NAME = "ITM QUANT MULTI ASSET"
LEGACY_PRODUCT_IDS = {"com.itmquant.multiasset.institutional",
                      "com.itmquant.dow.specialized"}
PRODUCT_MARKER_NAME = ".itm_quant_product.json"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
LEGACY_STORAGE_DIR = PACKAGE_ROOT / "app" / "storage"

CATEGORY_NAMES = (
    "sessions",
    "scanner_history",
    "tape_archive",
    "calibration",
    "probability",
    "replay",
    "audit",
    "anchors",
    "research",
    "quarantine",
    "system",
    "backups",
)


def default_persistent_root() -> Path:
    override = (os.getenv("ITM_QUANT_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local = (os.getenv("LOCALAPPDATA") or "").strip()
        base = Path(local).expanduser() if local else (Path.home() / "AppData" / "Local")
        return (base / "ITM_QUANT" / "persistent_data").resolve()
    return (Path.home() / ".itm_quant" / "persistent_data").resolve()


PERSISTENT_ROOT = default_persistent_root()


def category_dir(category: str, storage_root: Optional[Path] = None) -> Path:
    root = Path(storage_root or PERSISTENT_ROOT)
    cat = str(category).strip().lower()
    if cat not in CATEGORY_NAMES:
        raise ValueError(f"Categoría persistente no reconocida: {category}")
    out = root / cat
    out.mkdir(parents=True, exist_ok=True)
    return out


def routed_dir(storage: Path, category: str) -> Path:
    """Route canonical storage to a category while preserving tmp-path test behavior."""
    p = Path(storage)
    try:
        canonical = p.expanduser().resolve()
    except Exception:
        canonical = p
    try:
        root = PERSISTENT_ROOT.resolve()
    except Exception:
        root = PERSISTENT_ROOT
    if canonical == root:
        return category_dir(category, PERSISTENT_ROOT)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _classify(name: str) -> str:
    n = name.lower()
    if n == ".gitkeep":
        return "system"
    if n.startswith("scanner_history_"):
        return "scanner_history"
    if n.startswith("calibration_csv_") and n.endswith(".jsonl"):
        return "quarantine"
    if n.startswith("probability_model_"):
        return "probability"
    if n.startswith("scale_anchors_") or n.startswith("scale_anchors_pending_"):
        return "anchors"
    if n.startswith("tape_") and (n.endswith(".csv") or n.endswith(".csv.gz")):
        return "tape_archive"
    if n.startswith("auditor_") or "audit" in n:
        return "audit"
    if n in {"opening_calibration.sqlite"} or n.startswith("calibration_"):
        return "calibration"
    if n in {"research_v114.sqlite", "dealer_inventory.sqlite"}:
        return "research"
    if "large_prints" in n or n in {
        "alpaca_stock_exchanges.json", "macro_dia_cache.json"
    }:
        return "research"
    if n.startswith("alpaca_") and "_history_" in n:
        return "sessions"
    if n.startswith("session_metrics_") or n.startswith("option_flow_events_"):
        return "sessions"
    if n == "premarket_maps.json":
        return "sessions"
    return "research"


def _iter_data_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and p.name != ".gitkeep"]


def _version_key(text: str) -> tuple[int, ...]:
    raw = str(text or "").strip().lower().lstrip("v")
    out: list[int] = []
    for token in raw.split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out or [0])


def _read_product_marker(root: Path) -> dict:
    marker = Path(root) / PRODUCT_MARKER_NAME
    if not marker.exists():
        return {}
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _legacy_fingerprint(root: Path) -> bool:
    """Strong fingerprint for pre-marker packages, accepted only after explicit selection."""
    root = Path(root)
    required = [
        root / "VERSION.txt",
        root / "README.md",
        root / "INICIAR_WEB.bat",
        root / "app" / "service.py",
        root / "app" / "core" / "engine.py",
        root / "app" / "storage",
    ]
    if not all(p.exists() for p in required):
        return False
    try:
        readme = (root / "README.md").read_text(encoding="utf-8", errors="ignore").upper()
    except Exception:
        return False
    return "ITM QUANT" in readme and "MULTI-ASSET" in readme


def validate_migration_source(source: Path, *, explicit: bool = False) -> tuple[Path, dict]:
    """Validate a selected previous installation and return its storage directory.

    ``source`` may be the package root or its ``app/storage`` directory.  Modern
    packages require the exact product marker.  Older pre-marker packages are
    accepted only for *explicit* operator-selected migration and only when their
    application fingerprint matches ITM QUANT.
    """
    raw = Path(source).expanduser().resolve()
    root = raw
    if raw.name.lower() == "storage" and raw.parent.name.lower() == "app":
        root = raw.parent.parent
    storage = root / "app" / "storage"
    if not storage.exists() or not any(_iter_data_files(storage)):
        raise ValueError("La instalación seleccionada no contiene app/storage con datos reales.")

    marker = _read_product_marker(root)
    if marker:
        marker_product = str(marker.get("product_id") or "")
        if marker_product != PRODUCT_ID and marker_product not in LEGACY_PRODUCT_IDS:
            raise ValueError("El marcador de producto no corresponde a ITM QUANT.")
        marker_version = str(marker.get("version") or "")
        version_file = (root / "VERSION.txt").read_text(encoding="utf-8", errors="ignore").strip() if (root / "VERSION.txt").exists() else marker_version
        return storage, {
            "validation": "PRODUCT_MARKER",
            "product_id": marker_product,
            "target_product_id": PRODUCT_ID,
            "version": version_file or marker_version,
            "root": str(root),
        }

    if not explicit:
        raise ValueError("La instalación no tiene marcador ITM QUANT; selección explícita requerida.")
    if not _legacy_fingerprint(root):
        raise ValueError("La carpeta seleccionada no coincide con la huella de una instalación ITM QUANT anterior.")
    version = (root / "VERSION.txt").read_text(encoding="utf-8", errors="ignore").strip()
    return storage, {
        "validation": "EXPLICIT_LEGACY_FINGERPRINT",
        "product_id": PRODUCT_ID,
        "version": version,
        "root": str(root),
    }


def _legacy_sources(package_root: Path, explicit_source: Optional[Path] = None) -> tuple[list[Path], list[dict]]:
    """Return only trusted/explicit sources. Never scan sibling directories."""
    sources: list[Path] = []
    validations: list[dict] = []

    direct = Path(package_root) / "app" / "storage"
    if direct.exists() and any(_iter_data_files(direct)):
        sources.append(direct)
        validations.append({
            "validation": "IN_PLACE_RUNNING_PACKAGE",
            "product_id": PRODUCT_ID,
            "version": (Path(package_root) / "VERSION.txt").read_text(encoding="utf-8", errors="ignore").strip() if (Path(package_root) / "VERSION.txt").exists() else "unknown",
            "root": str(Path(package_root).resolve()),
        })

    env_source = (os.getenv("ITM_QUANT_MIGRATE_FROM") or "").strip()
    selected = Path(explicit_source) if explicit_source else (Path(env_source) if env_source else None)
    if selected is not None:
        storage, meta = validate_migration_source(selected, explicit=True)
        try:
            already = any(storage.resolve() == s.resolve() for s in sources)
        except Exception:
            already = storage in sources
        if not already:
            sources.append(storage)
            validations.append(meta)
    return sources, validations


def _zip_backup(backup_path: Path, roots: Dict[str, Path]) -> int:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for label, root in roots.items():
            if not root.exists():
                continue
            for p in _iter_data_files(root):
                try:
                    rel = p.relative_to(PERSISTENT_ROOT)
                    if rel.parts and rel.parts[0] == "backups":
                        continue
                except Exception as _e:
                    _obs_note('persistence:269', _e)
                try:
                    arc = Path(label) / p.relative_to(root)
                except Exception:
                    arc = Path(label) / p.name
                try:
                    zf.write(p, arcname=str(arc))
                    count += 1
                except Exception as _e:
                    _obs_note('persistence:280', _e)
                    continue
    return count


@dataclass
class MigrationReport:
    version: str
    persistent_root: str
    status: str
    source_policy: str
    backup: Optional[str]
    backup_files: int
    migrated_files: int
    skipped_identical: int
    conflicts_preserved: int
    legacy_sources: list[str]
    source_validation: list[dict]
    created_at_utc: str


_BOOTSTRAP_CACHE: Optional[MigrationReport] = None


def _source_signature(src: Path) -> str:
    """Stable-enough source revision fingerprint without reading huge archives twice.

    File count alone was unsafe: a LIVE database could change while keeping the same
    number of files.  The signature now includes relative path, size and nanosecond
    mtime for every source file, so a later explicit migration can detect an updated
    closed-session source without silently treating it as already migrated.
    """
    try:
        root = src.parent.parent.resolve()
    except Exception:
        root = Path(src)
    h = hashlib.sha256(str(root).encode("utf-8"))
    for item in sorted(_iter_data_files(src), key=lambda p: str(p).lower()):
        try:
            st = item.stat()
            rel = item.relative_to(src)
            token = f"|{rel}|{st.st_size}|{st.st_mtime_ns}"
        except Exception:
            token = f"|{item}"
        h.update(token.encode("utf-8", errors="ignore"))
    return h.hexdigest()[:20]


def bootstrap_persistence(package_root: Optional[Path] = None, target_version: str = TARGET_VERSION,
                          legacy_source: Optional[Path] = None) -> MigrationReport:
    """Initialize persistent storage and optionally merge one explicitly selected legacy source.

    Calling this with no legacy source is safe and does *not* scan neighboring folders.
    A later explicit migration remains possible even if the app already booted once.
    """
    global _BOOTSTRAP_CACHE
    if _BOOTSTRAP_CACHE is not None and legacy_source is None and not (os.getenv("ITM_QUANT_MIGRATE_FROM") or "").strip():
        return _BOOTSTRAP_CACHE

    package_root = Path(package_root or PACKAGE_ROOT)
    PERSISTENT_ROOT.mkdir(parents=True, exist_ok=True)
    for cat in CATEGORY_NAMES:
        category_dir(cat)

    state_path = category_dir("system") / "migration_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    completed = list(state.get("completed_migrations") or [])

    sources, validations = _legacy_sources(package_root, legacy_source)
    fresh_sources: list[Path] = []
    fresh_validations: list[dict] = []
    completed_sigs = {str(x.get("source_signature")) for x in completed if isinstance(x, dict)}
    for src, meta in zip(sources, validations):
        sig = _source_signature(src)
        if sig in completed_sigs:
            continue
        meta = dict(meta)
        meta["source_signature"] = sig
        fresh_sources.append(src)
        fresh_validations.append(meta)

    has_existing = any(_iter_data_files(category_dir(cat)) for cat in CATEGORY_NAMES if cat not in {"backups", "system"})
    has_legacy = any(any(_iter_data_files(src)) for src in fresh_sources)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path: Optional[Path] = None
    backup_files = 0
    if has_legacy:
        backup_path = category_dir("backups") / f"backup_before_v{target_version}_{stamp}.zip"
        roots: Dict[str, Path] = {}
        if has_existing:
            roots["persistent_before_upgrade"] = PERSISTENT_ROOT
        for i, src in enumerate(fresh_sources, start=1):
            roots[f"selected_legacy_source_{i}"] = src
        backup_files = _zip_backup(backup_path, roots)
        if backup_files == 0:
            try:
                backup_path.unlink(missing_ok=True)
            except Exception as _e:
                _obs_note('persistence:379', _e)
            backup_path = None

    migrated = 0
    skipped = 0
    conflicts = 0
    conflict_root = category_dir("backups") / f"migration_conflicts_v{target_version}_{stamp}"

    for src in fresh_sources:
        for item in _iter_data_files(src):
            category = _classify(item.name)
            dest = category_dir(category) / item.name
            try:
                if not dest.exists():
                    shutil.copy2(item, dest)
                    migrated += 1
                    continue
                if _sha256(item) == _sha256(dest):
                    skipped += 1
                    continue
                safe_source = src.parent.parent.name.replace(" ", "_") or "legacy"
                cdir = conflict_root / safe_source
                cdir.mkdir(parents=True, exist_ok=True)
                candidate = cdir / item.name
                suffix = 1
                while candidate.exists():
                    candidate = cdir / f"{item.stem}.{suffix}{item.suffix}"
                    suffix += 1
                shutil.copy2(item, candidate)
                conflicts += 1
            except Exception as _e:
                _obs_note('persistence:412', _e)
                continue

    now = datetime.now(timezone.utc).isoformat()
    status = "MIGRATED" if fresh_sources else ("ALREADY_MIGRATED" if sources else "INITIALIZED_NO_SOURCE")
    report = MigrationReport(
        version=target_version,
        persistent_root=str(PERSISTENT_ROOT),
        status=status,
        source_policy="NO_SIBLING_SCAN · IN_PLACE_OR_EXPLICIT_ONLY",
        backup=str(backup_path) if backup_path else None,
        backup_files=backup_files,
        migrated_files=migrated,
        skipped_identical=skipped,
        conflicts_preserved=conflicts,
        legacy_sources=[str(s) for s in fresh_sources],
        source_validation=fresh_validations,
        created_at_utc=now,
    )

    for src, meta in zip(fresh_sources, fresh_validations):
        completed.append({
            "source": str(src),
            "source_signature": meta.get("source_signature"),
            "validation": meta.get("validation"),
            "source_version": meta.get("version"),
            "target_version": target_version,
            "completed_at_utc": now,
            "migrated_files": migrated,
        })

    state = {
        "layout_version": target_version,
        "product_id": PRODUCT_ID,
        "last_report": asdict(report),
        "completed_migrations": completed,
        "source_policy": report.source_policy,
        "rule": "NO RESET DE DATOS LIVE / AUDITOR / CALIBRATION / REPLAY",
    }
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_path)

    manifest = category_dir("system") / "data_manifest.json"
    counts = {}
    for cat in CATEGORY_NAMES:
        if cat == "backups":
            continue
        try:
            counts[cat] = sum(1 for p in category_dir(cat).rglob("*") if p.is_file())
        except Exception:
            counts[cat] = 0
    manifest.write_text(json.dumps({
        "version": target_version,
        "product_id": PRODUCT_ID,
        "persistent_root": str(PERSISTENT_ROOT),
        "categories": counts,
        "updated_at_utc": now,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if legacy_source is None and not (os.getenv("ITM_QUANT_MIGRATE_FROM") or "").strip():
        _BOOTSTRAP_CACHE = report
    return report


def migrate_from_path(source: Path, package_root: Optional[Path] = None,
                      target_version: str = TARGET_VERSION) -> MigrationReport:
    """Explicit migration entry point used by the migration helper script."""
    global _BOOTSTRAP_CACHE
    _BOOTSTRAP_CACHE = None
    return bootstrap_persistence(package_root or PACKAGE_ROOT, target_version, legacy_source=Path(source))

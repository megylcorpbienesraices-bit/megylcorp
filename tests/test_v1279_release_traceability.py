"""Active-release traceability and artifact-cleanliness contracts."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _version() -> str:
    return (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()


def test_release_identity_is_synchronized_across_runtime_surfaces():
    v = _version()
    marker = json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    assert marker["version"] == v and marker["scope"] == "MULTI_ASSET"
    assert str(marker["release"]).endswith("_PRE_VPS")
    package = json.loads((ROOT / "frontend/solid-shell/package.json").read_text(encoding="utf-8"))
    assert package["version"] == v
    for cargo in sorted((ROOT / "rust").glob("*/Cargo.toml")):
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', cargo.read_text(encoding="utf-8"))
        assert m and m.group(1) == v


def test_one_complete_active_release_document_set_matches_marker():
    v = _version()
    marker = json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))
    required = [
        ROOT / f"CHANGELOG_v{v}.md", ROOT / f"VALIDACION_PRE_VPS_v{v}.md",
        ROOT / f"RELEASE_MANIFEST_v{v}.json", ROOT / f"QUANT_ENGINE_AUDIT_v{v}.md",
        ROOT / "docs/operations" / f"VALIDACION_LIVE_PRE_PRODUCCION_v{v}.md",
    ]
    assert all(p.is_file() for p in required)
    manifest = json.loads((ROOT / f"RELEASE_MANIFEST_v{v}.json").read_text(encoding="utf-8"))
    assert manifest["version"] == v and manifest["release"] == marker["release"]
    active = sorted(ROOT.glob("VALIDACION_PRE_VPS_v*.md"))
    assert active == [ROOT / f"VALIDACION_PRE_VPS_v{v}.md"]


def test_historical_release_archive_is_not_shipped_in_runtime_artifact():
    archive = ROOT / "docs/archive/release-history"
    assert not archive.exists(), "el paquete operativo no debe cargar certificaciones de releases retiradas"


def test_operational_banners_report_current_version():
    tag = f"v{_version()}"
    for rel in ("LEEME.txt", "deploy/ALWAYS_ON_VPS.md", "PROBAR_PROVEEDORES.bat", "PROBAR_TASTYTRADE.bat"):
        head = "\n".join((ROOT / rel).read_text(encoding="utf-8", errors="replace").splitlines()[:10])
        assert tag in head, rel


def test_release_gate_checks_artifact_cleanliness_before_running_suite():
    src = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    assert "def artifact_cleanliness_guard" in src
    main = src[src.find("def main"):]
    assert main.find("artifact_cleanliness_guard()") < main.find("compileall")
    assert "__pycache__" in src and ".ruff_cache" in src and ".pytest_cache" in src

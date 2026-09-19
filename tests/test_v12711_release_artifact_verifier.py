from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_release_artifact.py"


def _module():
    spec = importlib.util.spec_from_file_location("verify_release_artifact", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_verifier_exists_and_is_bound_to_artifact_plan_inventory_and_environment():
    text = SCRIPT.read_text(encoding="utf-8")
    for token in ("artifact_sha256", "plan_sha256", "inventory_sha256", "environment_sha256"):
        assert token in text
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in text
    assert "--junitxml=" in text


def test_journal_credit_is_reused_only_for_same_validation_identity(tmp_path):
    mod = _module()
    identity = {
        "artifact_sha256": "a" * 64,
        "plan_sha256": "b" * 64,
        "environment_sha256": "c" * 64,
        "inventory_sha256": "d" * 64,
        "collected": 10,
        "particiones_totales": 2,
        "environment": {},
    }
    journal = tmp_path / "j.json"
    data = {**identity, "resultados": {"1": {"ok": True}}}
    journal.write_text(json.dumps(data), encoding="utf-8")
    assert mod.leer_diario(journal, identity)["resultados"]["1"]["ok"] is True

    changed = {**identity, "environment_sha256": "e" * 64}
    reset = mod.leer_diario(journal, changed)
    assert reset["resultados"] == {}


def test_safe_extract_rejects_zip_path_traversal(tmp_path):
    mod = _module()
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "no")
    with pytest.raises(SystemExit, match="ZIP inseguro"):
        mod.safe_extract(archive, tmp_path / "out")


def test_safe_extract_accepts_normal_tree(tmp_path):
    mod = _module()
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("release/tests/example.txt", "ok")
    out = tmp_path / "out"
    out.mkdir()
    mod.safe_extract(archive, out)
    assert (out / "release/tests/example.txt").read_text() == "ok"


def test_parse_junit_preserves_skip_as_release_blocker(tmp_path):
    mod = _module()
    junit = tmp_path / "x.xml"
    junit.write_text('<?xml version="1.0"?><testsuite tests="1" skipped="1" failures="0" errors="0"><testcase classname="x" name="t"><skipped message="retired surface"/></testcase></testsuite>', encoding="utf-8")
    metrics = mod.parse_junit(junit)
    assert metrics["skipped"] == 1
    assert metrics["passed"] == 0
    assert metrics["skips_no_auditados"]


def test_partition_timeout_carries_partition_files_and_log_tail():
    mod = _module()
    exc = mod.PartitionTimeout(["python", "-m", "pytest"], 10, partition=7, files=["tests/test_x.py"], log_tail=["last line"])
    assert exc.partition == 7
    assert exc.files == ["tests/test_x.py"]
    assert exc.log_tail == ["last line"]


def test_zip_preflight_rejects_packaged_build_residue(tmp_path):
    mod = _module()
    archive = tmp_path / "bad-build.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("release/rust/causality_engine/target/debug/x", "bad")
    with pytest.raises(SystemExit, match="desarrollo/temporal"):
        mod.zip_preflight(archive)


def test_zip_preflight_rejects_packaged_secret(tmp_path):
    mod = _module()
    archive = tmp_path / "bad-secret.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("release/.env", "TOKEN=secret")
    with pytest.raises(SystemExit, match="secret"):
        mod.zip_preflight(archive)


def test_inventory_identity_rejects_internally_inconsistent_inventory(tmp_path):
    mod = _module()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/INVENTORY.json").write_text(json.dumps({"version": "x", "total_minimo": 2, "ficheros": {"tests/test_x.py": {"tests": ["test_x"], "casos": 1}}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="inventario"):
        mod.inventory_identity(tmp_path)


def test_zip_preflight_rejects_release_preparation_residue(tmp_path):
    mod = _module()
    archive = tmp_path / "bad-prep.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("release/app/main.py.new", "partial")
    with pytest.raises(SystemExit, match="desarrollo/temporal"):
        mod.zip_preflight(archive)

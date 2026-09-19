from __future__ import annotations

import json
from pathlib import Path

import app.persistence as persistence
from app.core.audit_reporting import write_audit_reports


def _legacy_install(root: Path, version: str = "1.16.8") -> Path:
    storage = root / "app" / "storage"
    (root / "app" / "core").mkdir(parents=True, exist_ok=True)
    storage.mkdir(parents=True, exist_ok=True)
    (root / "VERSION.txt").write_text(version, encoding="utf-8")
    (root / "README.md").write_text("# ITM QUANT MULTI-ASSET INSTITUTIONAL\n", encoding="utf-8")
    (root / "INICIAR_WEB.bat").write_text("@echo off\n", encoding="utf-8")
    (root / "app" / "service.py").write_text("# itm quant\n", encoding="utf-8")
    (root / "app" / "core" / "engine.py").write_text("# itm quant engine\n", encoding="utf-8")
    return storage


def test_v118_routed_dir_splits_only_canonical_storage(tmp_path, monkeypatch):
    root = tmp_path / "persistent_data"
    monkeypatch.setattr(persistence, "PERSISTENT_ROOT", root)
    assert persistence.routed_dir(root, "scanner_history") == root / "scanner_history"
    custom = tmp_path / "unit_test_flat"
    assert persistence.routed_dir(custom, "scanner_history") == custom


def test_v118_never_auto_scans_sibling_installations(tmp_path, monkeypatch):
    """Regression for the v1.17 contamination vector reported in audit."""
    root = tmp_path / "persistent_data"
    monkeypatch.setattr(persistence, "PERSISTENT_ROOT", root)
    monkeypatch.setattr(persistence, "_BOOTSTRAP_CACHE", None)

    new = tmp_path / "ITM_QUANT_v1.18.0"
    (new / "app" / "storage").mkdir(parents=True)
    (new / "VERSION.txt").write_text("1.18.0", encoding="utf-8")

    sibling = tmp_path / "PKG167"
    storage = _legacy_install(sibling)
    (storage / "probability_model_dia_auto.json").write_text('{"wrong_source":true}', encoding="utf-8")

    report = persistence.bootstrap_persistence(new, "1.18.0")
    assert report.status == "INITIALIZED_NO_SOURCE"
    assert report.migrated_files == 0
    assert report.legacy_sources == []
    assert not (root / "probability" / "probability_model_dia_auto.json").exists()
    assert report.source_policy == "NO_SIBLING_SCAN · IN_PLACE_OR_EXPLICIT_ONLY"


def test_v118_explicit_legacy_migration_backs_up_and_routes_data(tmp_path, monkeypatch):
    root = tmp_path / "persistent_data"
    monkeypatch.setattr(persistence, "PERSISTENT_ROOT", root)
    monkeypatch.setattr(persistence, "_BOOTSTRAP_CACHE", None)

    new = tmp_path / "ITM_QUANT_v1.18.0"
    (new / "app" / "storage").mkdir(parents=True)
    (new / "VERSION.txt").write_text("1.18.0", encoding="utf-8")

    old = tmp_path / "ITM_QUANT_v1.16.8"
    storage = _legacy_install(old)
    (storage / "scanner_history_dia_2026-09-08.csv").write_text("x\n1\n", encoding="utf-8")
    (storage / "tape_dia_2026-09-08.csv.gz").write_bytes(b"tape-live-bytes")
    (storage / "probability_model_dia_auto.json").write_text('{"stage":"COLLECTING"}', encoding="utf-8")
    (storage / "scale_anchors_dia_auto.json").write_text('{"n":3}', encoding="utf-8")
    (storage / "research_v114.sqlite").write_bytes(b"research")

    report = persistence.bootstrap_persistence(new, "1.18.0", legacy_source=old)
    assert report.migrated_files == 5
    assert report.backup and Path(report.backup).exists()
    assert report.source_validation[0]["validation"] == "EXPLICIT_LEGACY_FINGERPRINT"
    assert (root / "scanner_history" / "scanner_history_dia_2026-09-08.csv").exists()
    assert (root / "tape_archive" / "tape_dia_2026-09-08.csv.gz").exists()
    assert (root / "probability" / "probability_model_dia_auto.json").exists()
    assert (root / "anchors" / "scale_anchors_dia_auto.json").exists()
    assert (root / "research" / "research_v114.sqlite").exists()
    state = json.loads((root / "system" / "migration_state.json").read_text(encoding="utf-8"))
    assert state["rule"] == "NO RESET DE DATOS LIVE / AUDITOR / CALIBRATION / REPLAY"

    report2 = persistence.bootstrap_persistence(new, "1.18.0", legacy_source=old)
    assert report2.status == "ALREADY_MIGRATED"
    assert report2.migrated_files == 0


def test_v118_no_source_boot_does_not_consume_later_explicit_migration(tmp_path, monkeypatch):
    root = tmp_path / "persistent_data"
    monkeypatch.setattr(persistence, "PERSISTENT_ROOT", root)
    monkeypatch.setattr(persistence, "_BOOTSTRAP_CACHE", None)
    new = tmp_path / "ITM_QUANT_v1.18.0"
    (new / "app" / "storage").mkdir(parents=True)
    (new / "VERSION.txt").write_text("1.18.0", encoding="utf-8")
    old = tmp_path / "ITM_QUANT_v1.16.8"
    storage = _legacy_install(old)
    (storage / "session_metrics_dia_2026-09-08.csv").write_text("x\n1\n", encoding="utf-8")

    first = persistence.bootstrap_persistence(new, "1.18.0")
    assert first.status == "INITIALIZED_NO_SOURCE"
    second = persistence.bootstrap_persistence(new, "1.18.0", legacy_source=old)
    assert second.status == "MIGRATED"
    assert second.migrated_files == 1
    assert (root / "sessions" / "session_metrics_dia_2026-09-08.csv").exists()


def test_v118_rejects_generic_version_plus_storage_folder(tmp_path, monkeypatch):
    root = tmp_path / "persistent_data"
    monkeypatch.setattr(persistence, "PERSISTENT_ROOT", root)
    monkeypatch.setattr(persistence, "_BOOTSTRAP_CACHE", None)
    new = tmp_path / "new"
    (new / "app" / "storage").mkdir(parents=True)
    wrong = tmp_path / "not_itm_quant"
    (wrong / "app" / "storage").mkdir(parents=True)
    (wrong / "VERSION.txt").write_text("9.9.9", encoding="utf-8")
    (wrong / "app" / "storage" / "calibration.sqlite").write_bytes(b"foreign")
    try:
        persistence.bootstrap_persistence(new, "1.18.0", legacy_source=wrong)
    except ValueError as exc:
        assert "huella" in str(exc).lower() or "marcador" in str(exc).lower()
    else:
        raise AssertionError("A generic sibling folder must never be accepted as ITM QUANT memory")


def test_v118_auditor_writes_daily_and_cumulative_without_touching_signal(tmp_path):
    scanner = {"generated_at": "2026-09-08T10:00:00-05:00", "direction": "BUY", "edge_state": "ACTIONABLE", "evidence_score": 78}
    original = dict(scanner)
    result = write_audit_reports(
        tmp_path, "DIA", "LIVE",
        data_quality={"score": 91}, model_health={"score": 88},
        calibration={"status": "COLLECTING", "sample_size": 41, "sessions": 4, "probability_model": {"stage": "COLLECTING"}},
        scanner=scanner, source_health={"data_disagreement": False},
        research_storage={"research_ready": True}, tape_archive={"written": 230},
    )
    assert result["ready"] is True
    assert Path(result["daily_report"]).exists()
    assert Path(result["cumulative_report"]).exists()
    assert result["daily"]["observations"] == 1
    assert result["cumulative"]["sessions_reported"] == 1
    assert scanner == original, "Auditor must remain observational"


def test_v118_demo_does_not_contaminate_auditor(tmp_path):
    result = write_audit_reports(tmp_path, "DIA", "DEMO", scanner={"direction": "SELL"})
    assert result["ready"] is False
    assert result["status"] == "DEMO_NOT_PERSISTED"
    assert not (tmp_path / "events").exists()

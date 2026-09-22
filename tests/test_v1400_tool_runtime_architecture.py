from __future__ import annotations

from pathlib import Path
import json
import pandas as pd

from app.core.tool_registry import TOOL_REGISTRY, build_tool_digest, registry_payload
from app.core.gamma_migration import migration_frame, gamma_migration_figure
from app.core.liquidity_zones import liquidity_zones
from app.core.conditional_outcomes import conditional_outcome_report
from app.core.institutional_modules import gex_matrix_figure
from app.core.sophia_core import SophiaRuntime

ROOT = Path(__file__).resolve().parents[1]


def _chain() -> dict:
    t0 = pd.Timestamp("2026-09-14T14:30:00Z")
    t1 = pd.Timestamp("2026-09-14T14:35:00Z")
    rows = []
    for ts, bump in ((t0, 0.0), (t1, 1.0)):
        for strike, base in ((530.0, 1_000_000.0), (531.0, -2_000_000.0), (532.0, 12_000_000.0)):
            for expiry in ("2026-09-18", "2026-09-25"):
                rows.append({
                    "timestamp": ts,
                    "strike": strike,
                    "expiration_date": expiry,
                    "option_type": "call",
                    "signed_gex_proxy": base + bump * (100_000 if strike != 531 else -200_000),
                    "open_interest": 100,
                    "volume": 10,
                    "implied_volatility": .25,
                })
    return {"spot": 531.2, "enriched": pd.DataFrame(rows)}


def test_tool_registry_has_single_directional_authority():
    authorities = {k: v.authority for k, v in TOOL_REGISTRY.items()}
    assert authorities["scanner"] == "SOLE_DIRECTIONAL_AUTHORITY"
    assert [k for k, v in authorities.items() if v == "SOLE_DIRECTIONAL_AUTHORITY"] == ["scanner"]
    assert authorities["conditional_outcomes"] == "EVIDENCE_ONLY"


def test_tool_registry_contracts_are_bounded_and_capability_driven():
    payload = registry_payload()
    assert payload["schema"] == "ITMQ_TOOL_REGISTRY_V1"
    assert payload["authority_rule"] == "SCANNER_SOLE_DIRECTIONAL_AUTHORITY"
    tools = {x["id"]: x for x in payload["tools"]}
    assert "MIGRATION" in tools["gexmatrix"]["capabilities"]
    assert "VALUE_DIFFERENCE" in tools["gamma_migration"]["capabilities"]
    assert "COALESCED" in tools["trace"]["capabilities"]


def test_tool_digest_does_not_invent_direction():
    state = {"active_symbol": "DIA", "scanner": {"ready": True, "direction": "BUY", "strength": 77}, "spot": 531.2}
    d = build_tool_digest("scanner", state)
    assert d["scanner"]["direction"] == "BUY"
    assert d["scanner"]["authority"] == "SOLE_DIRECTIONAL_AUTHORITY"
    f = build_tool_digest("flow", state)
    assert "direction" not in f


def test_gamma_migration_value_and_difference_are_distinct_and_causal():
    data = _chain()
    value, vm = migration_frame(data, "VALUE")
    diff, dm = migration_frame(data, "DIFFERENCE")
    assert not value.empty and not diff.empty
    assert vm["dealer_inventory_claim"] is False
    assert dm["interpretation"] == "CHANGE_IN_CALCULATED_EXPOSURE"
    assert diff["timestamp"].nunique() == 1  # first snapshot cannot be differenced
    # strike 530 changed +100k per expiration => +200k aggregate
    got = diff.loc[diff["strike"] == 530.0, "value"].iloc[0]
    assert got == 200_000.0


def test_gamma_migration_figure_declares_model_risk_semantics():
    fig = gamma_migration_figure(_chain(), "DIFFERENCE")
    assert fig.layout.meta["dealer_inventory_claim"] is False
    assert fig.layout.meta["mode"] == "DIFFERENCE"
    assert "DIFFERENCE" in fig.layout.title.text


def test_gex_matrix_uses_robust_display_scale_but_keeps_exact_values():
    fig = gex_matrix_figure(_chain(), exp_count=10, mode="GEX")
    meta = fig.layout.meta
    assert meta["renderer_intent"] == "MATRIX_EXACT_VALUES_ROBUST_SCALE"
    assert meta["exact_values_in_hover"] is True
    assert meta["mvc"] is not None
    assert meta["robust_scale_cap_m"] <= meta["raw_max_m"]
    assert meta["expiration_map"]["09-18"] == "2026-09-18"
    heat = fig.data[0]
    assert heat.customdata is not None


def test_liquidity_zones_are_adaptive_context_not_direction():
    df = pd.DataFrame({
        "price": [530.00, 530.02, 530.03, 531.00, 531.01, 531.02],
        "notional": [8e6, 12e6, 6e6, 4e6, 5e6, 4e6],
    })
    z = liquidity_zones(df, spot=530.5)
    assert z["authority"] == "CONTEXT_ONLY"
    assert z["adaptive_cluster_width"] > 0
    assert z["zones"]
    assert {x["type"] for x in z["zones"]} <= {"BLOCK", "CARPET"}


def test_conditional_outcomes_preserve_scanner_direction_and_quality_gate():
    scanner = {"ready": True, "direction": "SELL", "evidence_score": 83, "probability": {"p_target_before_invalidation": .64}}
    calibration = {"probability_model": {"ready": True, "scope_samples": 140, "test_sessions": 18, "brier_skill_score": .12, "log_loss_model": .58, "base_rate": .51}}
    r = conditional_outcome_report(scanner, calibration, {"regime": "POSITIVE_GAMMA"})
    assert r["direction"] == "SELL"
    assert r["statistical_evidence"] == "STRONG"
    assert r["authority"] == "EVIDENCE_ONLY"
    assert r["p_t1_before_invalidation"] == .64


def test_sophia_read_tool_data_is_read_only_structured_digest():
    state = {"active_symbol": "DIA", "scanner": {"ready": True, "direction": "BUY", "strength": 80}}
    r = SophiaRuntime.read_tool_data("scanner", state)
    assert r["ready"] is True
    assert r["authority"] == "SOLE_DIRECTIONAL_AUTHORITY"
    assert r["digest"]["scanner"]["direction"] == "BUY"
    assert SophiaRuntime.read_tool_data("unknown", state)["ready"] is False


def test_frontend_tool_runtime_uses_snapshot_then_incremental_ws_and_coalescing():
    js = (ROOT / "app/static/tool_runtime.js").read_text(encoding="utf-8")
    assert "/api/tools/${encodeURIComponent(tool)}/digest" in js
    assert "/ws/tools/${encodeURIComponent(tool)}" in js
    assert "setTimeout" in js and "200" in js
    assert "TOOL_SNAPSHOT" in js
    assert "itmq:tool-update" in js


def test_tool_worker_uses_typed_arrays_and_declared_jobs():
    js = (ROOT / "app/static/tool_worker.js").read_text(encoding="utf-8")
    for token in ("Float64Array", "Float32Array", "CUMULATIVE_SERIES", "DERIVATIVE_SERIES", "ROBUST_SCALE", "MIGRATION_DIFFERENCE", "VISIBLE_BUBBLE_SCALE", "SURFACE_NORMALIZE", "TRACE_VISIBLE_RANGE"):
        assert token in js


def test_dashboard_exposes_gamma_migration_drilldown_conditional_evidence_and_tool_runtime():
    html = (ROOT / "app/templates/dashboard.html").read_text(encoding="utf-8")
    for token in ("gammaMigrationMode", "gammaMigrationChart", "gexCellInspector", "conditionalEvidence", "liquidityZonesGrid", "/static/tool_runtime.js"):
        assert token in html


def test_app_js_wires_gamma_migration_and_exact_gex_drilldown():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    assert "gamma_migration_mode" in js
    assert "gammaMigrationChart" in js
    assert "/api/analytics/gex-cell" in js
    assert "bindGexMatrixDrilldown" in js


def test_main_exposes_tool_digest_alert_conditional_gex_and_ws_contracts():
    src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    for token in ('/api/tools/registry', '/api/tools/{tool_id}/digest', '/api/alerts/structured', '/api/analytics/conditional-outcomes', '/api/analytics/gex-cell', '/ws/tools/{tool_id}'):
        assert token in src

# ---------------------------------------------------------------------------
# v1.40 release/supply-chain hardening contracts. These tests protect the
# certification path; they do not change Scanner or quantitative authority.


def _release_module():
    import importlib.util
    path = ROOT / "scripts/release_gate_full.py"
    spec = importlib.util.spec_from_file_location("itmq_release_gate_contract", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _prep_module():
    import importlib.util
    path = ROOT / "scripts/prepare_release_assets.py"
    spec = importlib.util.spec_from_file_location("itmq_prepare_release_contract", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _package_module():
    import importlib.util
    path = ROOT / "scripts/package_release_artifact.py"
    spec = importlib.util.spec_from_file_location("itmq_package_release_contract", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_release_locks_are_hash_pinned_and_bootstrap_is_pip_only():
    gate = _release_module()
    gate.lock_structure_guard()
    bootstrap = gate._requirement_blocks(ROOT / "requirements.bootstrap.lock.txt")
    assert set(bootstrap) == {"pip"}
    assert bootstrap["pip"]["hashes"]


def test_bootstrap_pip_pin_matches_test_lock_pin():
    gate = _release_module()
    assert gate.locked_version(ROOT / "requirements.bootstrap.lock.txt", "pip") == gate.locked_version(ROOT / "requirements.test.lock.txt", "pip")


def test_windows_lock_is_production_lock_without_uvloop():
    gate = _release_module()
    prod = gate._requirement_blocks(ROOT / "requirements.production.lock.txt")
    win = gate._requirement_blocks(ROOT / "requirements.windows.lock.txt")
    assert set(win) == (set(prod) - {"uvloop"})
    assert all(win[name]["version"] == prod[name]["version"] for name in win)


def test_all_runtime_and_test_locks_have_sha256_hashes():
    gate = _release_module()
    for rel in ("requirements.production.lock.txt", "requirements.windows.lock.txt", "requirements.test.lock.txt", "requirements.rust-bridge.lock.txt", "requirements.bootstrap.lock.txt"):
        blocks = gate._requirement_blocks(ROOT / rel)
        assert blocks
        assert all(data["hashes"] for data in blocks.values())


def test_ci_uses_immutable_runner_and_action_revisions():
    import re
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "runs-on: ubuntu-24.04" in ci
    refs = re.findall(r"uses:\s+[^\s]+@([^\s#]+)", ci)
    assert refs and all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs)


def test_ci_uses_declared_toolchains_and_never_generates_locks():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python-version-file: .python-version" in ci
    assert "node-version-file: .node-version" in ci
    assert ".npm-version" in ci and "npm --version" in ci
    assert "rust-toolchain.toml" in ci
    assert "cargo generate-lockfile" not in ci
    assert "npm install --package-lock-only" not in ci
    assert "release_gate_full.py --production" in ci


def test_ci_builds_real_container_and_smokes_runtime_import():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "docker build --pull=false" in ci
    assert "import app.main" in ci
    assert ".python-version" in ci


def test_docker_base_is_patch_and_digest_pinned_and_runtime_copy_is_minimal():
    import re
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    target = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert re.search(rf"^FROM python:{re.escape(target)}-slim-bookworm@sha256:[0-9a-f]{{64}}$", docker, re.MULTILINE)
    assert not re.search(r"(?m)^COPY\s+\.\s+\.\s*$", docker)
    assert "COPY --chown=itmquant:itmquant app ./app" in docker


def test_docker_installs_hash_pinned_wheels_only():
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements.bootstrap.lock.txt" in docker
    assert "requirements.production.lock.txt" in docker
    assert docker.count("--require-hashes") >= 2
    assert docker.count("--only-binary=:all:") >= 2
    assert "python -m pip" in docker


def test_dockerignore_excludes_secrets_and_native_build_outputs():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for token in (".env", "!.env.example", ".venv-release/", "**/node_modules/", "frontend/solid-shell/dist/", "rust/**/target/", "*.new", "*.bak-release-prep"):
        assert token in text


def test_vps_uses_external_credentials_and_external_release_venv():
    vps = (ROOT / "deploy/VALIDAR_Y_DESPLEGAR_VPS.sh").read_text(encoding="utf-8")
    assert "/etc/itm-quant/itm-quant.env" in vps
    assert "mktemp -d /tmp/itm-quant-release-venv" in vps
    assert ".npm-version" in vps and "npm --version" in vps
    assert "[[ -f .env ]]" not in vps
    assert "--env-file .env" not in vps


def test_vps_requires_prepared_assets_and_never_resolves_locks_in_gate_path():
    vps = (ROOT / "deploy/VALIDAR_Y_DESPLEGAR_VPS.sh").read_text(encoding="utf-8")
    assert "prepare_release_assets.py --check" in vps
    assert "release_gate_full.py --production" in vps
    assert "cargo generate-lockfile" not in vps
    assert "npm install --package-lock-only" not in vps


def test_windows_installer_requires_exact_python_patch_and_bootstrap_lock():
    """v1.57.1 · EL CONTROL ANTI-DERIVA ESTABA SILENCIADO POR UN COMENTARIO.

    Esto exigía que la versión de `.python-version` apareciera en el `.bat`, y se
    cumplía… porque un `rem` decía «Historical Docker/Linux certification target
    remains 3.12.14» mientras el comando instalaba 3.12.10. La prueba pasaba con
    el instalador y el gate pidiendo versiones distintas, que es exactamente lo
    que existía para impedir.

    Un texto que aparece en un comentario no demuestra que el comando lo use. Lo
    que sí lo demuestra es que el `.bat` LEA el pin.
    """
    win = (ROOT / "INSTALAR_WEB.bat").read_text(encoding="utf-8", errors="replace")
    target = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert "set /p ITMQ_PYTHON=<.python-version" in win
    comandos = "\n".join(l for l in win.splitlines()
                         if not l.strip().lower().startswith(("rem ", "rem\t", "::")))
    assert target not in comandos, (
        "la versión vuelve a estar escrita a mano en un comando")
    assert "py -%ITMQ_PYMM% -m venv .venv" in win
    assert "requirements.bootstrap.lock.txt" in win
    assert "requirements.windows.lock.txt" in win
    assert "--require-hashes --no-deps --only-binary=:all:" in win
    assert "pip install --upgrade pip" not in win


def test_optional_sophia_installer_is_fail_closed_without_hash_pinned_lock():
    ps = (ROOT / "deploy/INSTALL_SOPHIA_LOCAL_WINDOWS.ps1").read_text(encoding="utf-8", errors="replace")
    resolver_input = (ROOT / "requirements-sophia-local.in").read_text(encoding="utf-8")
    assert "requirements.sophia-local.lock.txt" in ps
    assert "--require-hashes --no-deps --only-binary=:all:" in ps
    assert "requirements-sophia-local.in" in ps and "NO se instala directamente" in ps
    assert "pip install --upgrade pip" not in ps
    assert "winget install" not in ps and "ollama pull" not in ps
    assert "NEVER install directly" in resolver_input


def test_native_build_scripts_are_lock_closed():
    solid = (ROOT / "scripts/BUILD_SOLID_SHELL.bat").read_text(encoding="utf-8", errors="replace")
    rust = (ROOT / "scripts/BUILD_RUST_CAUSALITY.bat").read_text(encoding="utf-8", errors="replace")
    wasm = (ROOT / "scripts/BUILD_WASM_BRIDGE.bat").read_text(encoding="utf-8", errors="replace")
    assert "npm ci" in solid and "package-lock.json" in solid
    assert "cargo build --locked --release" in rust and "Cargo.lock" in rust
    assert "wasm-pack build --locked" in wasm and "Cargo.lock" in wasm
    assert "cargo install wasm-pack" not in wasm


def test_solid_shell_is_reference_optional_not_production_runtime():
    readme = (ROOT / "frontend/solid-shell/README.md").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    vps = (ROOT / "deploy/VALIDAR_Y_DESPLEGAR_VPS.sh").read_text(encoding="utf-8")
    assert "REFERENCE / OPTIONAL" in readme
    assert "npm ci" in readme
    assert "frontend/solid-shell" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "npm ci" not in ci and "npm ci" not in vps


def test_legacy_requirements_delegates_to_production_lock():
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").strip()
    assert text == "-r requirements.production.lock.txt"


def test_release_preparer_rejects_non_cratesio_cargo_sources_and_bad_checksum(tmp_path):
    import pytest
    prep = _prep_module()
    bad = tmp_path / "Cargo.lock"
    bad.write_text('version = 4\n[[package]]\nname = "x"\nversion = "1.0.0"\nsource = "git+https://example.invalid/x"\nchecksum = "0"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        prep.validate_cargo_lock(bad)


def test_release_preparer_accepts_structurally_valid_cratesio_lock(tmp_path):
    prep = _prep_module()
    lock = tmp_path / "Cargo.lock"
    lock.write_text('version = 4\n[[package]]\nname = "x"\nversion = "1.0.0"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\nchecksum = "' + ('a' * 64) + '"\n', encoding="utf-8")
    prep.validate_cargo_lock(lock)


def test_optional_package_lock_validation_rejects_untrusted_registry(tmp_path):
    import pytest
    prep = _prep_module()
    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps({"lockfileVersion": 3, "packages": {"node_modules/x": {"resolved": "http://evil.invalid/x.tgz", "integrity": "sha512-abc"}}}), encoding="utf-8")
    with pytest.raises(SystemExit):
        prep.validate_optional_package_lock(lock)


def test_vendor_payload_binds_bundle_license_dashboard_and_csp():
    prep = _prep_module()
    payload = prep._vendor_lock_payload(b"js", b"license", b"dashboard", b"main", "sha512-source")
    assert payload["package"] == "lightweight-charts"
    for key in ("bundle_sha256", "license_sha256", "dashboard_sha256", "main_sha256", "dist_integrity"):
        assert payload[key]


def test_vendor_preparation_verifies_npm_integrity_and_removes_remote_cdn_contract(tmp_path):
    import tarfile
    import pytest
    src = (ROOT / "scripts/prepare_release_assets.py").read_text(encoding="utf-8")
    assert "dist.integrity" in src
    assert "sha512" in src.lower()
    assert "unpkg.com" in src and "cdn.jsdelivr.net" in src
    assert "lightweight-charts.standalone.production.js" in src
    assert "LIGHTWEIGHT_CHARTS_LICENSE" in src
    assert "filter=\"data\"" in src and "tipo especial no permitido" in src
    prep = _prep_module()
    bad = tmp_path / "bad.tgz"
    with tarfile.open(bad, "w:gz") as tf:
        fifo = tarfile.TarInfo("package/evil-fifo")
        fifo.type = tarfile.FIFOTYPE
        tf.addfile(fifo)
    with pytest.raises(SystemExit):
        prep._safe_extract_tar(bad, tmp_path / "out")


def test_vendor_preparation_supports_offline_verified_tarball_without_network(monkeypatch, tmp_path):
    import base64
    import hashlib
    import io
    import tarfile
    prep = _prep_module()

    tar_path = tmp_path / "lightweight-charts-5.2.1.tgz"
    files = {
        "package/package.json": b'{"name":"lightweight-charts","version":"5.2.1"}',
        "package/dist/lightweight-charts.standalone.production.js": b"window.LightweightCharts={};",
        "package/LICENSE": b"Apache-2.0 fixture",
    }
    with tarfile.open(tar_path, "w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tar_path.read_bytes()).digest()).decode()

    dashboard = tmp_path / "dashboard.html"
    dashboard.write_text('<script src="https://unpkg.com/lightweight-charts@5.2.1/dist/lightweight-charts.standalone.production.js"></script>', encoding="utf-8")
    main = tmp_path / "main.py"
    main.write_text('CSP = "default-src self https://unpkg.com https://cdn.jsdelivr.net"\n', encoding="utf-8")
    vendor = tmp_path / "vendor"
    monkeypatch.setattr(prep, "VENDOR_DIR", vendor)
    monkeypatch.setattr(prep, "VENDOR_JS", vendor / "lightweight-charts.standalone.production.js")
    monkeypatch.setattr(prep, "VENDOR_LICENSE", vendor / "LIGHTWEIGHT_CHARTS_LICENSE")
    monkeypatch.setattr(prep, "VENDOR_LOCK", vendor / "lightweight-charts.lock.json")
    monkeypatch.setattr(prep, "DASHBOARD", dashboard)
    monkeypatch.setattr(prep, "MAIN", main)

    prep.prepare_vendor(tar_path=tar_path, integrity=integrity)
    prep.validate_vendor()
    assert (vendor / "lightweight-charts.standalone.production.js").read_bytes() == files["package/dist/lightweight-charts.standalone.production.js"]
    assert "unpkg.com" not in dashboard.read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" not in main.read_text(encoding="utf-8")


def test_vendor_offline_mode_rejects_wrong_integrity_and_wrong_package(monkeypatch, tmp_path):
    import base64
    import hashlib
    import io
    import tarfile
    import pytest
    prep = _prep_module()
    bad = tmp_path / "bad.tgz"
    with tarfile.open(bad, "w:gz") as tf:
        data = b'{"name":"wrong-package","version":"5.2.1"}'
        info = tarfile.TarInfo("package/package.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(bad.read_bytes()).digest()).decode()
    with pytest.raises(SystemExit, match="no corresponde"):
        prep._publish_vendor_from_verified_tarball(bad, integrity)
    with pytest.raises(SystemExit, match="no coincide"):
        prep._verify_npm_tarball_integrity(bad, "sha512-" + base64.b64encode(b"x" * 64).decode())


def test_release_publication_is_transactional_and_has_rollback_markers(monkeypatch, tmp_path):
    src = (ROOT / "scripts/prepare_release_assets.py").read_text(encoding="utf-8")
    assert ".new" in src
    assert ".bak-release-prep" in src
    assert "replace" in src
    assert "rollback" in src.lower() or "backup" in src.lower()
    prep = _prep_module()
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    original = Path.replace
    calls = {"n": 0}
    def flaky(self, target):
        if self.name.endswith(".new"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated publication failure")
        return original(self, target)
    monkeypatch.setattr(Path, "replace", flaky)
    import pytest
    with pytest.raises(OSError):
        prep._publish_transaction({a: b"A", b: b"B"})
    assert not a.exists() and not b.exists()
    assert not list(tmp_path.glob("*.new")) and not list(tmp_path.glob("*.bak-release-prep"))


def test_cleanliness_guard_rejects_python_native_frontend_and_prepare_residue():
    gate = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    for token in ("__pycache__", ".pytest_cache", ".ruff_cache", ".venv-release", "node_modules", "target", ".new", ".bak-release-prep"):
        assert token in gate


def test_cleanup_function_is_limited_to_deterministic_python_test_caches():
    import inspect
    gate = _release_module()
    src = inspect.getsource(gate.cleanup_generated_artifacts)
    for token in ("__pycache__", ".pytest_cache", ".ruff_cache", ".pyc", ".pyo"):
        assert token in src
    for forbidden in ("node_modules", "target", ".env", "storage"):
        assert forbidden not in src


def test_zero_skip_is_a_hard_release_invariant():
    gate = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts/verify_release_artifact.py").read_text(encoding="utf-8")
    assert 'metrics["skipped"] == 0' in verifier
    assert "skipped or failed or errors" in verifier
    assert "skipped" in gate and "SKIP" in gate


def test_release_traceability_is_bound_to_inventory_case_and_file_counts():
    src = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    assert "inventory_total" in src
    assert "inventory_files" in src
    assert "manifest" in src.lower()
    assert "VALIDACION_PRE_VPS" in src
    assert "plan_sha256" in src
    assert "plan_particiones" in src
    _release_module().release_traceability_guard((ROOT / "VERSION.txt").read_text(encoding="utf-8").strip())


def test_artifact_verifier_enforces_expected_zip_sha256():
    src = (ROOT / "scripts/verify_release_artifact.py").read_text(encoding="utf-8")
    assert 'if args.sha256:' in src
    assert 'SHA-256 no coincide' in src
    assert 'digest != expected' in src


def test_github_workflows_avoid_ambiguous_single_line_run_scalars():
    # Commands containing tokens such as --only-binary=:all: must use a block scalar;
    # otherwise the trailing colon followed by whitespace can make the YAML invalid.
    import re
    workflows = sorted([*(ROOT / ".github/workflows").glob("*.yml"), *(ROOT / ".github/workflows").glob("*.yaml")])
    assert workflows
    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("run: "):
                command = stripped[5:]
                assert not re.search(r":[ \t]", command), (workflow, line)


def test_every_github_workflow_pins_actions_and_release_workflow_is_fail_closed():
    import re
    workflows = sorted([*(ROOT / ".github/workflows").glob("*.yml"), *(ROOT / ".github/workflows").glob("*.yaml")])
    assert workflows
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        refs = re.findall(r"uses:\s+[^\s]+@([^\s#]+)", text)
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in refs), workflow
    release = (ROOT / ".github/workflows/release-artifact.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in release
    assert "prepare_release_assets.py --all" in release
    assert "prepare_release_assets.py --check" in release
    assert "package_release_artifact.py --output" in release
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in release
    assert release.index("prepare_release_assets.py --all") < release.index("package_release_artifact.py --output")


def test_artifact_verifier_runs_zip_preflight_before_extraction():
    import importlib.util
    src = (ROOT / "scripts/verify_release_artifact.py").read_text(encoding="utf-8")
    assert "zip_preflight(artifact)" in src
    assert src.index("zip_preflight(artifact)") < src.index("safe_extract(artifact")
    assert "plan_traceability_guard(tree, plan, collected)" in src
    path = ROOT / "scripts/verify_release_artifact.py"
    spec = importlib.util.spec_from_file_location("itmq_verify_release_contract", path)
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    plan, collected = verifier.plan_particiones(ROOT, 21)
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    expected = json.loads((ROOT / f"RELEASE_MANIFEST_v{version}.json").read_text(encoding="utf-8"))["tests"]["plan_sha256"]
    assert verifier.plan_traceability_guard(ROOT, plan, collected) == expected


def test_artifact_structural_guard_requires_native_locks_and_vendor_assets():
    src = (ROOT / "scripts/verify_release_artifact.py").read_text(encoding="utf-8")
    for token in ("rust/causality_engine/Cargo.lock", "rust/wasm_bridge/Cargo.lock", "lightweight-charts.standalone.production.js", "LIGHTWEIGHT_CHARTS_LICENSE", "lightweight-charts.lock.json"):
        assert token in src


def test_artifact_verifier_default_plan_is_21_partitions_with_bounded_timeout():
    src = (ROOT / "scripts/verify_release_artifact.py").read_text(encoding="utf-8")
    assert 'default=21' in src
    assert 'default=180' in src
    assert "PartitionTimeout" in src


def test_rust_certification_uses_temporary_target_outside_repository():
    src = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    assert "CARGO_TARGET_DIR" in src
    assert "TemporaryDirectory" in src
    assert '"--locked"' in src


def test_dependency_audit_and_ruff_require_exact_locked_versions():
    src = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    assert 'locked_version(TEST_LOCK, "pip-audit")' in src
    assert 'locked_version(TEST_LOCK, "ruff")' in src
    assert "pip_audit" in src
    assert '"ruff", "check"' in src


def test_production_preflight_reports_all_blockers_instead_of_first_only():
    src = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    section = src[src.index("def toolchain_preflight"):src.index("def dependency_audit")]
    assert "problems.append" in section
    assert "return problems" in section
    for token in ("Python", "Node", "Rust/Cargo", "Docker", "pyzmq", "Cargo.lock", "Lightweight Charts"):
        assert token in section


def test_official_packager_certifies_before_writing_and_verifies_before_publish():
    import inspect
    mod = _package_module()
    src = inspect.getsource(mod.package)
    assert src.index("production_preflight()") < src.index("run_full_production_gate()")
    assert src.index("run_full_production_gate()") < src.index("write_deterministic_zip")
    assert src.index("write_deterministic_zip") < src.index("verify_exact_zip")
    assert src.index("verify_exact_zip") < src.index("os.replace")
    assert src.index("os.replace") < src.index("write_sidecars")


def test_official_packager_is_byte_deterministic_for_same_tree(tmp_path):
    mod = _package_module()
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    (root / "VERSION.txt").write_text("9.9.9\n", encoding="utf-8")
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "scripts" / "x.py").write_text("print('x')\n", encoding="utf-8")
    mod.ROOT = root
    files = [root / "VERSION.txt", root / "a.txt", root / "scripts" / "x.py"]
    one = tmp_path / "one.zip"
    two = tmp_path / "two.zip"
    mod.write_deterministic_zip(one, files)
    mod.write_deterministic_zip(two, files)
    assert one.read_bytes() == two.read_bytes()
    import zipfile
    with zipfile.ZipFile(one) as archive:
        assert archive.infolist()
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())


def test_official_packager_never_overwrites_or_writes_inside_release_tree():
    src = (ROOT / "scripts/package_release_artifact.py").read_text(encoding="utf-8")
    assert "output.relative_to(ROOT.resolve())" in src
    assert "salida/sidecar ya existe; no se sobrescribe" in src
    assert "FIXED_ZIP_TIME" in src and "strict_timestamps=True" in src
    assert "symlink no permitido" in src
    assert "sys.dont_write_bytecode" in src


def test_vps_manual_never_mutates_an_already_verified_artifact():
    doc = (ROOT / "deploy/ALWAYS_ON_VPS.md").read_text(encoding="utf-8")
    assert "### A. Preparación/certificación del candidato" in doc
    assert "### B. Despliegue del artefacto exacto" in doc
    assert "No vuelvas a ejecutar `--all` sobre ese artefacto verificado" in doc
    production = doc.split("### B. Despliegue del artefacto exacto", 1)[1]
    assert "prepare_release_assets.py --all" not in production
    assert "prepare_release_assets.py --check" in production


def test_official_packager_rolls_back_if_sidecar_publication_fails(tmp_path, monkeypatch):
    import pytest
    mod = _package_module()
    root = tmp_path / "tree"
    root.mkdir()
    (root / "VERSION.txt").write_text("9.9.9\n", encoding="utf-8")
    (root / "payload.txt").write_text("payload\n", encoding="utf-8")
    mod.ROOT = root
    monkeypatch.setattr(mod, "production_preflight", lambda: [])
    monkeypatch.setattr(mod, "run_full_production_gate", lambda: None)
    monkeypatch.setattr(mod.gate, "artifact_cleanliness_guard", lambda: None)
    monkeypatch.setattr(mod, "verify_exact_zip", lambda output, digest, journal: None)
    def fail_sidecars(output, digest, journal):
        output.with_suffix(output.suffix + ".sha256").write_text("partial\n", encoding="utf-8")
        raise OSError("sidecar failure")
    monkeypatch.setattr(mod, "write_sidecars", fail_sidecars)
    out = tmp_path / "candidate.zip"
    with pytest.raises(OSError, match="sidecar failure"):
        mod.package(out)
    assert not out.exists()
    assert not out.with_suffix(out.suffix + ".sha256").exists()
    assert not out.with_suffix(out.suffix + ".verification.json").exists()

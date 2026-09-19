from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RETIRED=()
def test_no_module_level_quarantine_and_all_skip_reasons_are_retired_surfaces():
    for p in (ROOT/"tests").glob("test_*.py"):
        if p.name == "test_v1274_release_integrity.py":
            continue
        src=p.read_text(encoding="utf-8")
        assert "pytestmark =" not in src and "pytestmark=" not in src, f"module-level quarantine forbidden: {p.name}"
        for chunk in src.split("skipif(")[1:]:
            head=chunk[:450]
            assert any(x in head for x in RETIRED), f"unreviewed skipif in {p.name}: {head[:120]!r}"


def test_demo_refresh_is_network_free_for_macro(monkeypatch):
    import app.service as service
    def forbidden(*args, **kwargs):
        raise AssertionError("DEMO must not call official macro network fetch")
    monkeypatch.setattr(service, "fetch_macro_context", forbidden)
    state=service.PlatformState()
    state.mode="DEMO"
    state.refresh(True)
    assert state.mode=="DEMO"
    assert (state.macro or {}).get("source_note","").startswith("DEMO_OFFLINE_NO_NETWORK")


def test_guard_scripts_fail_check_mode_when_findings_exist():
    silent=(ROOT/"scripts/codemod_silent_except.py").read_text(encoding="utf-8")
    versions=(ROOT/"scripts/codemod_version_asserts.py").read_text(encoding="utf-8")
    assert "if args.check and total" in silent and "return 1" in silent
    assert "if args.check and total" in versions and "return 1" in versions

def test_release_gate_runs_complete_suite_not_named_subsets():
    gate=(ROOT/"scripts/release_gate_full.py").read_text(encoding="utf-8")
    assert 'run(sys.executable,"-m","pytest","-q")' in gate
    assert "test_v127" not in gate

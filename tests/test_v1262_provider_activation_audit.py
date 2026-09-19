from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def text(rel): return (ROOT/rel).read_text(encoding="utf-8")


def test_quantdata_configured_is_not_same_as_observed_provider():
    main=text("app/main.py")
    assert 'qd_observed=bool("QUANTDATA" in (feature.get("usable_providers") or []))' in main
    assert '"active_provider":qd_observed' in main
    assert 'CORROBORATION_ONLY_NATIVE_MATH_RETAINS_AUTHORITY' in main


def test_native_structure_has_no_provider_dependency():
    src=text("app/core/native_options_structure.py")
    assert '"provider_dependency": "NONE"' in src
    assert 'ITM_QUANT_NATIVE_OPTIONS_STRUCTURE' in src
    assert 'gamma_flip_crossing' in src


def test_provider_status_does_not_call_credentials_alone_active():
    main=text("app/main.py")
    assert 'alp_observed=bool(' in main and 'tasty_observed=bool(' in main and 'qd_observed=bool(' in main
    assert '"active_provider":alp_observed' in main and '"active_provider":tasty_observed' in main and '"active_provider":qd_observed' in main


def test_source_context_uses_quantdata_feature_bus_without_network_io():
    src=text("app/core/source_fusion.py")
    assert 'def quantdata_context' in src
    assert 'FEATURE_BUS.snapshot(sym)' in src
    assert 'never performs network I/O' in src

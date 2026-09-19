from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "app/static/app.css").read_text(encoding="utf-8")


def _root_and_body():
    m = re.search(r":root\s*\{(.*?)\}", CSS, re.S)
    assert m, "missing :root design tokens"
    return m.group(1), CSS[m.end():]


def test_design_system_owns_literal_colors():
    root, body = _root_and_body()
    # Literal palette values belong in one place. UI rules consume semantic tokens.
    assert re.search(r"--pos-600\s*:\s*#", root)
    assert re.search(r"--neg-600\s*:\s*#", root)
    assert re.search(r"--ink\s*:\s*#", root)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), "new hex found outside :root"
    assert body.count("var(") >= 300


def test_type_scale_has_no_micro_text_or_extreme_weight():
    _, body = _root_and_body()
    # All component font sizes should use the six-step token scale.
    px = [float(x) for x in re.findall(r"font-size\s*:\s*([0-9.]+)px", body)]
    assert not px, f"direct font-size px bypasses type scale: {sorted(set(px))}"
    weights = [int(x) for x in re.findall(r"font-weight\s*:\s*(\d+)", body)]
    assert not weights, "numeric font weights bypass semantic weight tokens"
    for token in ("--t1:11px", "--t2:13px", "--t3:15px", "--t4:19px", "--t5:26px", "--t6:38px"):
        assert token in CSS


def test_spacing_and_responsive_chart_tokens_exist():
    root, _ = _root_and_body()
    for token in ("--sp3:12px", "--sp4:16px", "--sp5:24px", "--sp6:32px"):
        assert token in root
    assert "--chart-h:clamp(" in root
    assert "--chart-tall:clamp(" in root
    assert "--chart-xl:clamp(" in root
    assert ".chart{height:var(--chart-h)!important" in CSS


def test_dense_grids_wrap_instead_of_squeezing_cards():
    assert ".hero-grid,.hero-grid.four{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))" in CSS
    assert ".highlight-grid{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))" in CSS
    assert ".op-decision{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))" in CSS


def test_decide_context_audit_hierarchy_is_explicit():
    assert "DECIDE > CONTEXTUALIZA > AUDITA" in CSS
    assert ".op-dir strong,.decision-main strong{font-size:var(--t6);font-weight:var(--w-decide)" in CSS
    assert ".op-context .ctx b{font-size:var(--t4);font-weight:var(--w-context)" in CSS
    assert ".timestamp,.muted,.small{color:var(--ink-3);font-weight:var(--w-body)" in CSS


def test_alert_danger_is_single_semantic_rule():
    # Prevent the accretion pattern that had multiple competing alert definitions.
    assert len(re.findall(r"\.alert-danger\{", CSS)) == 1

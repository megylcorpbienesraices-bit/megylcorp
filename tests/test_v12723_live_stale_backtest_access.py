from pathlib import Path
from conftest import assert_version_at_least, assert_marker_version_at_least

ROOT=Path(__file__).resolve().parents[1]
JS=(ROOT/'app/static/app.js').read_text(encoding='utf-8')
CSS=(ROOT/'app/static/app.css').read_text(encoding='utf-8')
HTML=(ROOT/'app/templates/dashboard.html').read_text(encoding='utf-8')


def test_release_identity_at_least_v12723():
    assert_version_at_least('1.27.23')
    assert_marker_version_at_least('1.27.23')


def test_live_stale_never_blurs_or_disables_active_page():
    assert 'body.publication-blocked .page-section.active{filter:none!important;opacity:1!important;pointer-events:auto!important;user-select:auto!important}' in CSS


def test_historical_calendar_is_always_reachable_without_stale_popup():
    assert 'id="topBacktestBtn"' not in HTML
    assert 'id="replayDateCalendar"' in HTML
    assert 'ABRIR HISTÓRICO' not in JS
    assert "navigateSection('backtest')" not in JS
    assert '.lean-replay{display:flex}' in CSS


def test_live_gate_remains_fail_closed_but_visual_warning_is_removed():
    assert "publicationBlocked=gate?.publicar_permitido!==true && replayMode==='LIVE'" in JS
    assert 'LIVE NO ACCIONABLE' not in JS
    assert 'NO ACCIONABLE · CONTEXTO VISIBLE' not in CSS


def test_replay_apply_uses_visible_calendar_date():
    assert "dateOverride||el('replayDateCalendar')?.value||el('traceDate')?.value" in JS
    assert "showError('Selecciona una fecha histórica.')" in JS
    assert "asof=${encodeURIComponent(asofISO)}" in JS

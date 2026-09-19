from pathlib import Path
import inspect
from app import main
from conftest import assert_version_at_least, assert_marker_version_at_least


ROOT=Path(__file__).resolve().parents[1]
def test_version_1260():
    assert_version_at_least('1.26.2')


def test_frontend_requests_view_scoped_charts():
    js=(ROOT/'app/static/app.js').read_text(encoding='utf-8')
    assert 'chartViewForSection' in js
    assert 'view=${encodeURIComponent(activeView)}' in js
    assert 'loadTablesIfNeeded' in js

def test_api_charts_supports_view_projection():
    sig=inspect.signature(main.api_charts)
    assert 'view' in sig.parameters
    src=inspect.getsource(main.api_charts)
    assert 'lean_payload' in src and 'view_keys' in src

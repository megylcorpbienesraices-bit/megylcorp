from pathlib import Path
from conftest import assert_marker_version_at_least

ROOT = Path(__file__).resolve().parents[1]

def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_visible_universe_is_the_closed_universe():
    """v1.58.0 · El alcance pasó de «instrumentos directos del Dow» al universo
    cerrado de 36 símbolos. El contrato completo está en
    tests/test_v1580_contrato_del_universo.py; aquí sólo se comprueba que lo
    VISIBLE coincida con lo permitido, que es lo que esta prueba vigilaba.
    """
    from app.core.assets import selectable_assets
    from app.core import universe

    visible = {row["symbol"] for row in selectable_assets()}
    assert visible == set(universe.ALLOWED)
    assert not [s for s in visible if not universe.is_allowed(s)]


def test_historical_precompute_never_overwrites_live_ready(tmp_path, monkeypatch):
    import app.core.always_on_state as aos
    monkeypatch.setattr(aos, "_ROOT", tmp_path / "live")
    monkeypatch.setattr(aos, "_SESSION_ROOT", tmp_path / "sessions")
    aos._ROOT.mkdir(parents=True, exist_ok=True)
    aos._SESSION_ROOT.mkdir(parents=True, exist_ok=True)
    store = aos.AlwaysOnReadyStore()

    live = {"ready": True, "spot": 526.5, "tag": "LIVE"}
    hist = {"ready": True, "spot": 520.0, "tag": "HISTORY"}
    assert store.save_ready_package(
        "DIA", state=live, charts={"c": "live"}, tables={},
        session_day="2026-09-11", source="LIVE", publish_live=True,
    )["ok"]
    assert store.save_ready_package(
        "DIA", state=hist, charts={"c": "history"}, tables={},
        session_day="2026-09-10", source="HISTORY", seal=True, publish_live=False,
    )["ok"]

    assert store.load_live("DIA")["state"]["tag"] == "LIVE"
    assert store.load_session("DIA", "2026-09-10")["state"]["tag"] == "HISTORY"


def test_calendar_date_only_prefers_causal_raw_archive_with_ready_package_as_migration_fallback():
    service = text("app/service.py")
    block = service[service.index("    def set_replay("):service.index("    def exit_replay(")]
    assert "READY_STORE.load_session" in block
    assert "instant_package" in block
    assert block.index("self._build_replay_bundle(force=True)") < block.index("READY_STORE.load_session")
    assert '"instant":True' in block  # compatibility fallback only
    assert "replay_session_clock" in block


def test_current_open_uses_ready_state_and_ready_chart_cache():
    main = text("app/main.py")
    state = main[main.index('@app.get("/api/state")'):main.index('@app.get("/api/charts")')]
    charts = main[main.index('@app.get("/api/charts")'):main.index('@app.get("/api/charts/surface-slice")')]
    assert "READY_STORE.load_live(STATE.symbol)" in state
    assert "cached_boot" in state
    assert "prefer_ready_cache" in charts
    assert "READY_STORE.load_live(STATE.symbol)" in charts
    assert 'payload["ready_cache"]=True' in charts


def test_frontend_calendar_is_global_and_date_selection_enters_causal_replay():
    html = text("app/templates/dashboard.html")
    js = text("app/static/app.js")
    assert 'id="replayDateCalendar"' in html
    assert 'id="replayLiveBtn"' in html
    assert "CONTEXTO TEMPORAL GLOBAL" in html
    assert "async function applyReplaySession" in js
    assert "/api/replay/set?date=" in js
    assert "replayClockMarks" in js
    assert "applyReplayClockIndex" in js


def test_browser_start_does_not_restart_engine_and_vps_artifacts_exist():
    web = text("INICIAR_WEB.bat")
    motor = text("INICIAR_MOTOR_24_7.bat")
    compose = text("docker-compose.always-on.yml")
    runner = text("run_always_on.py")
    assert "CERRAR_WEB" not in web
    assert "call INICIAR_MOTOR_24_7.bat" in web
    assert "ALWAYS-ON ya esta trabajando" in motor
    assert "restart: unless-stopped" in compose
    assert "run_always_on.py" in compose
    assert "Motor headless 24/7" in runner


def test_release_identity_keeps_dow_core_while_picker_allows_dynamic_etfs():
    import json
    product = json.loads(text(".itm_quant_product.json"))
    html = text("app/templates/dashboard.html")
    assets = text("app/core/assets.py")
    assert_marker_version_at_least('1.26.5')
    assert product.get("scope") == "MULTI_ASSET"
    assert str(product.get("release") or "").endswith("PRE_VPS")
    # v1.58.0 · La identidad de release ya no se ata a los símbolos del Dow: el
    # universo es una lista cerrada que vive en `core/universe.py`. Lo que esta
    # prueba protege —que el catálogo y el selector hablen del MISMO universo—
    # se comprueba contra esa lista.
    from app.core import universe

    assert "from . import universe" in assets, (
        "el catálogo dejó de consultar la autoridad del universo")
    assert "universe.is_allowed" in assets
    assert len(universe.ALLOWED) == universe.EXPECTED_SIZE
    assert 'data-symbol-category="ETFs"' in html


def test_persistence_identity_matches_specialized_release():
    persistence = text("app/persistence.py")
    config = text("app/config.py")
    assert 'PRODUCT_ID = "com.itmquant.multiasset"' in persistence
    assert '"com.itmquant.multiasset.institutional"' in persistence
    # v1.42: el id anterior pasa a legado para que un marcador v1.41 siga validando.
    assert '"com.itmquant.dow.specialized"' in persistence
    assert 'bootstrap_persistence(BASE_DIR, APP_VERSION)' in config

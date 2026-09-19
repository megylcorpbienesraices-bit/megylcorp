"""Contratos de fusión v1.27.7: security + release gate + freshness fail-closed."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fresh_meta(age_opt: float, age_stock: float, clock: str = "2026-09-11T15:30:00Z") -> dict:
    now = pd.Timestamp(clock)
    return {
        "symbol": "DIA",
        "quality_clock": now,
        "market_state": "OPEN",
        "latest_option_market_timestamp": now - pd.Timedelta(seconds=age_opt),
        "stock_market_timestamp": now - pd.Timedelta(seconds=age_stock),
    }


def test_same_quality_clock_never_replays_a_fresh_result_over_stale_inputs():
    from app.core.freshness import evaluate_publication_gate, reset_publication_gate

    reset_publication_gate("DIA")
    now = pd.Timestamp("2026-09-11T15:30:00Z").timestamp()
    good = evaluate_publication_gate(_fresh_meta(5, 2), now=now)
    bad = evaluate_publication_gate(_fresh_meta(900, 2), now=now)
    assert good["publicar_permitido"] is True
    assert bad["publicar_permitido"] is False
    assert bad["cadena_opciones"]["estado"] == "STALE"


def test_live_publication_gate_blocks_immediately_latches_then_requires_three_fresh_cycles():
    from app.core.freshness import CLOSED, OPEN, evaluate_publication_gate, reset_publication_gate

    reset_publication_gate("DIA")
    base = pd.Timestamp("2026-09-11T15:30:00Z")

    def check(age_opt: float, age_stock: float, offset: int):
        clock = base + pd.Timedelta(seconds=offset)
        m = _fresh_meta(age_opt, age_stock, clock.isoformat())
        return evaluate_publication_gate(m, now=clock.timestamp())

    first = check(900, 2, 0)
    assert first["estado"] == "BLOCKED_PENDING" and first["publicar_permitido"] is False
    second = check(900, 2, 1)
    assert second["estado"] == OPEN and second["publicar_permitido"] is False
    one = check(5, 2, 2)
    two = check(5, 2, 3)
    three = check(5, 2, 4)
    assert one["estado"] == OPEN and two["estado"] == OPEN
    assert three["estado"] == CLOSED and three["publicar_permitido"] is True


def test_replay_bypasses_wall_clock_freshness_but_missing_live_gate_fails_closed():
    from app.core.freshness import evaluate_publication_gate, publication_allowed, reset_publication_gate

    reset_publication_gate("DIA")
    replay = evaluate_publication_gate({"symbol": "DIA"}, is_replay=True)
    assert replay["publicar_permitido"] is True and replay["estado"] == "BYPASS_REPLAY"
    assert publication_allowed({}) is False
    assert publication_allowed({"circuito_frescura": {"publicar_permitido": False}}) is False


# --------------------------------------------------------------------------
# v1.27.8 · La exención de replay exige autoridad, no una cadena en meta.
# --------------------------------------------------------------------------

def _meta_rancio(symbol: str = "DIA") -> dict:
    import pandas as pd
    viejo = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=4)
    return {"symbol": symbol, "latest_option_market_timestamp": viejo,
            "stock_market_timestamp": viejo, "quality_clock": pd.Timestamp.now(tz="UTC")}


def test_la_cadena_replay_por_si_sola_ya_no_abre_el_gate():
    """Regresión v1.27.7: market_state='REPLAY' desactivaba el gate por completo."""
    from app.core.freshness import evaluate_publication_gate, reset_publication_gate

    for estado in ("REPLAY", "replay", "RePlAy"):
        reset_publication_gate("DIA")
        r = evaluate_publication_gate({**_meta_rancio(), "market_state": estado})
        assert r["publicar_permitido"] is False, f"{estado} no puede eximir sin autoridad"
        assert r["replay_declarado_sin_autoridad"] is True


def test_la_autoridad_del_contexto_si_exime_aunque_meta_no_lo_diga():
    from app.core.freshness import evaluate_publication_gate, reset_publication_gate

    reset_publication_gate("DIA")
    r = evaluate_publication_gate(_meta_rancio(), is_replay=True)
    assert r["publicar_permitido"] is True and r["autoridad"] == "replay_context"


def test_is_replay_false_bloquea_aunque_meta_declare_replay():
    from app.core.freshness import evaluate_publication_gate, reset_publication_gate

    reset_publication_gate("DIA")
    r = evaluate_publication_gate({**_meta_rancio(), "market_state": "REPLAY"}, is_replay=False)
    assert r["publicar_permitido"] is False


def test_build_data_quality_propaga_la_autoridad_y_no_la_cadena():
    import pandas as pd
    from app.core.precision_engine import build_data_quality
    from app.core.freshness import reset_publication_gate

    df = pd.DataFrame({"strike": [1.0], "bid": [1.0], "ask": [1.1],
                       "calc_delta": [0.5], "calc_gamma": [0.01]})
    info = {"count": 1, "mode": "WEEKLY"}
    meta = {**_meta_rancio("SPY"), "market_state": "REPLAY", "matched_snapshots": 1}

    reset_publication_gate("SPY")
    sin_autoridad = build_data_quality(df, meta, {}, info)
    assert sin_autoridad["circuito_frescura"]["publicar_permitido"] is False

    reset_publication_gate("SPY")
    con_autoridad = build_data_quality(df, meta, {}, info, is_replay=True)
    assert con_autoridad["circuito_frescura"]["publicar_permitido"] is True


def test_todas_las_llamadas_de_servicio_declaran_la_autoridad():
    """Ningún call site puede quedar sin declarar: el olvido reabriría el hueco."""
    import re
    from pathlib import Path

    src = Path("app/service.py").read_text(encoding="utf-8")
    llamadas = re.findall(r"build_data_quality\((?:[^()]|\([^()]*\))*\)", src)
    assert llamadas, "no se encontraron llamadas a build_data_quality"
    sin_declarar = [c for c in llamadas if "is_replay=" not in c]
    assert not sin_declarar, f"llamadas sin autoridad declarada: {sin_declarar}"


def test_el_unico_camino_que_declara_replay_verdadero_es_el_bundle_de_replay():
    import re
    from pathlib import Path

    src = Path("app/service.py").read_text(encoding="utf-8")
    # Nadie puede pasar is_replay=True literal: debe venir de replay_context.
    assert "is_replay=True" not in src, "la autoridad no puede estar hardcodeada"
    assert len(re.findall(r"is_replay=bool\(self\.replay_context\.is_replay\)", src)) >= 3


def test_scanner_is_made_non_actionable_when_freshness_blocks():
    from app.service import _apply_freshness_publication_gate

    scanner = {"direction": "BUY", "ready": True, "edge_state": "ACTIONABLE"}
    dq = {"circuito_frescura": {"estado": "OPEN", "publicar_permitido": False, "motivo": "cadena rancia"}}
    assert _apply_freshness_publication_gate(scanner, dq) is False
    assert scanner["direction"] == "BUY", "la auditoría conserva la dirección calculada"
    assert scanner["suppressed_direction"] == "BUY"
    assert scanner["ready"] is False and scanner["edge_state"] == "NO EDGE"
    assert scanner["publication_blocked"] is True


def test_sophia_treats_missing_or_closed_publication_gate_as_blocked():
    from app.core.sophia_core import SophiaRuntime

    blocked, why = SophiaRuntime._publication_blocked({})
    assert blocked is True and why
    blocked2, _ = SophiaRuntime._publication_blocked({"publication_gate": {"publicar_permitido": False, "motivo": "stale"}})
    assert blocked2 is True
    allowed, _ = SophiaRuntime._publication_blocked({"publication_gate": {"publicar_permitido": True}})
    assert allowed is False


def test_query_token_bootstrap_defaults_off_in_production(monkeypatch: pytest.MonkeyPatch):
    from app.core import net_guard

    monkeypatch.setenv("ITM_PRODUCTION", "1")
    monkeypatch.delenv("ITM_ALLOW_QUERY_TOKEN_BOOTSTRAP", raising=False)
    assert net_guard.allow_query_token_bootstrap() is False
    monkeypatch.setenv("ITM_ALLOW_QUERY_TOKEN_BOOTSTRAP", "1")
    assert net_guard.allow_query_token_bootstrap() is True, "solo una opt-in explícita puede reactivarlo"


def test_browser_login_uses_post_session_and_not_query_token():
    src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert '@app.post("/auth/session")' in src
    assert "fetch('/auth/session',{method:'POST'" in src
    assert "?token=" not in src[src.find("@app.get(\"/login\")"):src.find("@app.post(\"/logout\")")]


def test_frontend_keeps_stale_data_gate_internal_without_intrusive_surface():
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "app/static/app.css").read_text(encoding="utf-8")
    block=js[js.index("function renderFreshnessGate"):js.index("function renderState")]
    assert "publicationBlocked=gate?.publicar_permitido!==true && replayMode==='LIVE'" in block
    assert "publicationCritical=publicationBlocked&&expectedLive" in block
    assert "freshnessGateBanner" in block and ".remove()" in block
    assert "LIVE NO ACCIONABLE" not in block
    assert "NO ACCIONABLE · CONTEXTO VISIBLE" not in css


def test_release_gate_keeps_hardening_and_zero_skip_policy():
    src = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    for token in ("def dependency_audit", "def deployment_guard", "def production_runtime_guard", "def ruff_guard"):
        assert token in src
    assert "EXPECTED_PREVPS_SKIPS = 0" in src
    assert "EXPECTED_PRODUCTION_SKIPS = 0" in src
    assert "release con skips prohibidos" in src
    assert "pip-audit" in src
    assert "Cargo.lock" in src
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" in src


def test_el_gate_ya_no_exige_un_total_exacto_de_tests():
    """v1.27.7 fallaba al AÑADIR un test correcto. El suelo permite crecer."""
    src = (ROOT / "scripts/release_gate_full.py").read_text(encoding="utf-8")
    assert "EXPECTED_TOTAL_TESTS" not in src, "el total exacto bloqueaba el crecimiento"
    assert "total < minimo" in src, "debe comparar contra un suelo, no por igualdad"
    assert "skipped > expected_skips" in src, "los skips son techo, no igualdad"
    assert "test_inventory.py" in src, "la desaparicion se detecta por nombre"


def test_el_inventario_de_tests_existe_y_es_coherente():
    import json

    manifiesto = json.loads((ROOT / "tests/INVENTORY.json").read_text(encoding="utf-8"))
    ficheros = manifiesto["ficheros"]
    assert manifiesto["total_minimo"] == sum(d["casos"] for d in ficheros.values())
    # Cada fichero de tests del arbol debe estar inventariado por nombre.
    en_disco = {f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py")}
    faltan = sorted(en_disco - set(ficheros))
    assert not faltan, f"ficheros sin inventariar: {faltan}"
    # Los tests de este mismo fichero deben aparecer nominalmente.
    propios = ficheros["tests/test_v1277_fusion.py"]["tests"]
    assert "test_el_inventario_de_tests_existe_y_es_coherente" in propios


def test_el_inventario_detecta_la_desaparicion_de_un_test_por_nombre():
    """La comprobacion que un total agregado no puede hacer: borrar 5 y añadir 5."""
    import json
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    manifiesto = json.loads((ROOT / "tests/INVENTORY.json").read_text(encoding="utf-8"))
    registrados = set(manifiesto["ficheros"]["tests/test_v1276_golden_numerics.py"]["tests"])
    assert "test_paridad_put_call_se_cumple" in registrados
    # Un fichero con los mismos casos pero distinto nombre debe delatarse.
    actual = registrados - {"test_paridad_put_call_se_cumple"} | {"test_inventado"}
    assert sorted(registrados - actual) == ["test_paridad_put_call_se_cumple"]


def test_release_identity_is_synchronized_everywhere_critical():
    import json
    import re

    version = (ROOT / "VERSION.txt").read_text().strip()
    marker = json.loads((ROOT / ".itm_quant_product.json").read_text())
    assert marker["version"] == version
    assert str(marker["release"]).endswith("_PRE_VPS"), marker["release"]
    package = json.loads((ROOT / "frontend/solid-shell/package.json").read_text())
    assert package["version"] == version
    for cargo in (ROOT / "rust").glob("*/Cargo.toml"):
        text = cargo.read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        assert match and match.group(1) == version

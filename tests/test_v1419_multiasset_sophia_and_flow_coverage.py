"""v1.41.9 · Multi-activo tras retirar un proveedor, Sophia y cobertura del flujo.

Retirar tastytrade dejó activos huérfanos: YM, MYM y DJX lo tenían declarado como
proveedor de cadena y seguían pareciendo analizables mientras sus paneles salían
vacíos sin explicación. Aquí cada activo se reasigna a quien pueda servirlo de
verdad, o se declara por qué nadie puede.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture()
def roster(monkeypatch):
    import app.core.provider_parity as P
    import app.core.assets as A

    def _set(value=None):
        if value is None:
            monkeypatch.delenv("ITM_OPTIONS_PEERS", raising=False)
        else:
            monkeypatch.setenv("ITM_OPTIONS_PEERS", value)
        importlib.reload(P)
        importlib.reload(A)
        return A

    yield _set
    monkeypatch.delenv("ITM_OPTIONS_PEERS", raising=False)
    importlib.reload(P); importlib.reload(A)


# ────────────────────────── el activo se reasigna a quien pueda servirlo

def test_an_index_moves_to_the_provider_that_can_serve_it(roster):
    """DJX declaraba tastytrade. Quant Data sí sirve cadena de índices, así que el
    activo se reasigna en lugar de quedarse sin análisis."""
    A = roster(None)
    A.apply_roster_capabilities()
    cfg = A.ASSETS["DJX"]
    assert cfg["quant_provider_effective"] == "QUANTDATA"
    assert cfg["full"] is True and cfg["selectable"] is True


def test_futures_have_no_substitute_and_say_so(roster):
    """Ningún proveedor del roster sirve cadena de futuros. Fingir que sí dejaría al
    analista frente a paneles vacíos buscando un fallo que no existe."""
    A = roster(None)
    A.apply_roster_capabilities()
    for sym in ("YM", "MYM"):
        cfg = A.ASSETS[sym]
        assert cfg["quant_provider_effective"] is None, sym
        assert cfg["full"] is False and cfg["selectable"] is False, sym
        assert "fuera del roster" in cfg["quant_provider_reason"], sym
        assert "no disponible" in cfg["reason"].lower(), sym


def test_putting_the_provider_back_restores_its_assets(roster):
    A = roster("ALPACA,TASTYTRADE,QUANTDATA")
    A.apply_roster_capabilities()
    for sym in ("YM", "MYM"):
        assert A.ASSETS[sym]["quant_provider_effective"] == "TASTYTRADE", sym
        assert A.ASSETS[sym]["full"] is True, sym


def test_an_etf_keeps_its_declared_provider(roster):
    A = roster(None)
    A.apply_roster_capabilities()
    cfg = A.ASSETS["DIA"]
    assert cfg["quant_provider_effective"] == "ALPACA"
    assert cfg["quant_provider_reason"] == "DECLARADO"


def test_capability_is_by_instrument_type_not_by_preference(roster):
    """Alpaca no sirve futuros por mucho que esté en el roster: es una capacidad,
    no una preferencia."""
    A = roster("ALPACA")
    assert A.resolve_quant_provider({"kind": "FUTURO", "quant_provider": "ALPACA"})[0] is None
    assert A.resolve_quant_provider({"kind": "ETF", "quant_provider": "ALPACA"})[0] == "ALPACA"


def test_a_dynamic_etf_without_an_alpaca_chain_is_not_promised(roster):
    """Alpaca cataloga muchos ETF; sólo los que tienen cadena propia pueden
    analizarse. Prometer los demás es exactamente lo que dejaba QQQ sin hidratar."""
    A = roster(None)
    con = {"kind": "ETF", "dynamic": True, "alpaca_has_options": True, "quant_provider": "ALPACA"}
    sin = {"kind": "ETF", "dynamic": True, "alpaca_has_options": False, "quant_provider": "ALPACA"}
    assert A.resolve_quant_provider(con)[0] == "ALPACA"
    assert A.resolve_quant_provider(sin)[0] == "QUANTDATA"   # el par sí puede corroborarlo


def test_recalculation_reports_what_it_changed(roster):
    A = roster(None)
    changed = A.apply_roster_capabilities()
    assert "YM" in changed and "motivo" in changed["YM"]
    assert changed["YM"]["antes"] != changed["YM"]["despues"]


def test_the_startup_recalculates_the_catalog():
    src = text("app/main.py")
    assert "apply_roster_capabilities()" in src
    assert "activos recalculados por el roster" in src


# ────────────────────────── Sophia

def test_sophia_costs_nothing():
    """La condición que puso el usuario: sin créditos, sin tokens, sin mensualidad."""
    from app.core.sophia_core import SOPHIA
    st = SOPHIA.status()
    assert st["mode"] == "SELF_HOSTED_NO_CREDITS"
    doc = text("app/core/sophia_core.py")
    assert "No paid LLM/STT/TTS provider is wired here" in doc


def test_sophia_never_takes_directional_authority():
    from app.core.sophia_core import SOPHIA
    assert SOPHIA.status()["scanner_authority"] == "UNCHANGED"


def test_sophia_answers_by_the_same_channel_she_was_asked():
    """Escribir devuelve texto; hablar devuelve voz además del texto."""
    app = text("app/static/itmq_app.js")
    block = app[app.find("async function sophiaAsk"):app.find("async function sophiaSpeak")]
    assert "if (spoken) await sophiaSpeak(reply);" in block
    voz = app[app.find("sophia.rec.onstop"):app.find("sophia.rec.start()")]
    assert "sophiaAsk(body.text, { spoken: true })" in voz


def test_the_panel_exists_and_is_wired():
    html = text("app/templates/terminal.html")
    assert 'id="sophiaPanel"' in html and 'id="sophiaBtn"' in html
    for el_id in ("sophiaLog", "sophiaInput", "sophiaMic", "sophiaSend", "sophiaPill"):
        assert f'id="{el_id}"' in html, el_id
    # Fuera de <main>: un panel fijo dentro de un contenedor con su propio contexto
    # de apilamiento queda debajo del contenido y no se puede pulsar.
    assert html.find('id="sophiaPanel"') > html.find("</main>")
    app = text("app/static/itmq_app.js")
    assert "bindSophia();" in app


def test_sophia_declares_where_her_answer_came_from():
    """No es lo mismo un dato leído del motor que una interpretación del modelo."""
    app = text("app/static/itmq_app.js")
    assert "body.source" in app and "LOCAL_NO_CREDITS" in app


def test_voice_failures_degrade_instead_of_breaking_the_panel():
    app = text("app/static/itmq_app.js")
    assert "voz no disponible en este equipo" in app
    assert "este navegador no permite grabar" in app
    assert "permiso de micrófono denegado" in app


# ────────────────────────── flujo: la prima sin clasificar

def test_unclassified_premium_is_counted_separately():
    """$2,9M negociados con sólo $392K clasificados dejaban el carril de flujo neto
    plano, y eso se lee como avería cuando es falta de cotización."""
    src = text("app/static/itmq_orderflow.js")
    assert "else b.unknown += prem;" in src
    assert "unknown: 0" in src


def test_an_unclassifiable_tape_explains_itself_instead_of_drawing_nothing():
    src = text("app/static/itmq_orderflow.js")
    block = src[src.find("function drawNetFlow"):src.find("function drawTotal")]
    assert "PRIMA SIN AGRESOR IDENTIFICADO" in block
    assert "sin clasificar" in block


def test_the_bias_is_measured_over_classified_premium_only():
    """Dividir por prima sin agresor diluía el sesgo hacia EQUILIBRADO justo cuando
    peor se lee."""
    src = text("app/static/itmq_orderflow.js")
    assert "const clasificada = buy + sell;" in src
    assert "(buy - sell) / clasificada" in src
    assert "'SIN CLASIFICAR'" in src


def test_the_classification_coverage_reaches_the_screen():
    src = text("app/static/itmq_orderflow.js")
    assert "% con agresor" in src

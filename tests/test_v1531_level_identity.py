"""v1.53.1 · Identidad real de cada línea de TRACE.

Había líneas sin etiqueta en el gráfico. Una línea sin nombre sobre un gráfico
de operativa se ve, parece significar algo y no se puede identificar.

Lo que NO se puede hacer es deducirla por el color, y estas pruebas lo fijan:
la tabla de estilo agrupa `kind` distintos bajo el mismo token.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_colour_cannot_identify_a_line():
    """Rojo es `put_wall` Y `risk`. Verde es `call_wall` Y `target`. Ésta es la
    razón por la que el nombre tiene que salir del motor."""
    core = _read("app/static/itmq_core.js")
    block = core[core.index("const LEVELS = {"):core.index("/** Niveles que el panel de flujo")]
    by_colour = {}
    for kind, colour in re.findall(r"(\w+):\s*\{ color: '(--[\w-]+)'", block):
        by_colour.setdefault(colour, []).append(kind)
    # v1.54.0 · Con las líneas del Scanner el agrupamiento es AÚN mayor: rojo
    # es put_wall, risk Y la invalidación del Scanner; verde es call_wall,
    # target Y los dos objetivos. Razón de más para no deducir por color.
    assert set(by_colour["--neg"]) == {"put_wall", "risk", "scanner_inval"}
    assert set(by_colour["--pos"]) == {"call_wall", "target",
                                       "scanner_target1", "scanner_target2"}
    # Y ninguno de esos colores identifica un solo `kind`.
    for colour in ("--neg", "--pos"):
        assert len(by_colour[colour]) > 1, colour


def test_every_kind_the_engine_emits_has_a_declared_origin():
    """Un `kind` sin origen se dibujaría sin poder decir de dónde sale."""
    from app.core.level_identity import LEVEL_ORIGIN
    engine = _read("app/core/nextgen_terminal.py")
    body = engine[engine.index("def structure_levels("):engine.index("def key_levels_report(")]
    kinds = set(re.findall(r'_level\([^,]+,[^,]+,\s*"(\w+)"', body))
    assert kinds, "no se encontró ningún `kind` en structure_levels"
    faltan = kinds - set(LEVEL_ORIGIN)
    assert not faltan, f"kinds sin origen declarado: {faltan}"


def test_the_origin_names_the_real_function_and_field():
    """`source` y `method` son la procedencia, no una descripción."""
    from app.core.level_identity import LEVEL_ORIGIN
    assert LEVEL_ORIGIN["risk"]["function"] == "nextgen_terminal.structure_levels"
    assert LEVEL_ORIGIN["risk"]["field"] == "scanner['invalidation']"
    assert LEVEL_ORIGIN["target"]["field"] == "scanner['target1'|'target2']"
    # Los muros los resuelve el Wall Engine, no `structure_levels`.
    assert LEVEL_ORIGIN["call_wall"]["function"] == "wall_engine.walls_from_hub"
    assert LEVEL_ORIGIN["put_wall"]["function"] == "wall_engine.walls_from_hub"


def test_describe_exposes_the_seven_fields():
    from app.core.level_identity import describe, reset
    reset()
    rows = describe([{"name": "Invalidación", "price": 517.2, "kind": "risk"}], symbol="DIA")
    r = rows[0]
    for field in ("price", "type", "source", "method", "magnitude",
                  "persistence", "timestamp"):
        assert field in r, field
    assert r["price"] == pytest.approx(517.2)
    assert r["type"] == "risk"
    assert r["engine_name"] == "Invalidación"
    assert "structure_levels" in r["source"]
    assert "invalidation" in r["source_field"]
    # Sin magnitud NO se fabrica un cero: el cálculo no publica ninguna.
    assert r["magnitude"] is None


def test_an_unregistered_kind_says_so_instead_of_inventing_an_origin():
    from app.core.level_identity import describe, reset
    reset()
    r = describe([{"price": 1.0, "kind": "inventado"}], symbol="X")[0]
    assert r["known_to_registry"] is False
    assert "DESCONOCIDO" in r["source"] and "DESCONOCIDO" in r["method"]


def test_the_authority_of_the_level_wins_over_the_registry():
    """Si el nivel declara su autoridad, manda él: el registro es el respaldo."""
    from app.core.level_identity import describe, reset
    reset()
    r = describe([{"name": "CALL WALL", "price": 520.0, "kind": "call_wall",
                   "authority": "ITMQ_WALL_ENGINE", "score": 0.82}], symbol="DIA")[0]
    assert r["source"] == "ITMQ_WALL_ENGINE"
    assert r["magnitude"] == pytest.approx(0.82)
    assert r["magnitude_field"] == "score"


def test_persistence_counts_consecutive_cycles_and_resets_when_it_moves():
    from app.core.level_identity import describe, reset
    reset()
    for _ in range(3):
        r = describe([{"price": 534.70, "kind": "call_wall"}], symbol="DIA")[0]
    assert r["persistence"]["cycles"] == 3
    assert r["persistence"]["moved_this_cycle"] is False
    # Se mueve de sitio: el contador vuelve a empezar.
    r = describe([{"price": 536.00, "kind": "call_wall"}], symbol="DIA")[0]
    assert r["persistence"]["cycles"] == 1
    assert r["persistence"]["moved_this_cycle"] is True


def test_the_same_level_tolerance_is_relative_not_in_dollars():
    """Un céntimo es mucho en un ETF de 40 y nada en un índice de 5.800."""
    from app.core.level_identity import describe, reset, SAME_LEVEL_TOLERANCE
    assert SAME_LEVEL_TOLERANCE < 0.01
    reset()
    describe([{"price": 5800.0, "kind": "flip"}], symbol="SPX")
    r = describe([{"price": 5801.0, "kind": "flip"}], symbol="SPX")[0]
    assert r["persistence"]["cycles"] == 2, "un dólar en 5.800 es el mismo nivel"
    reset()
    describe([{"price": 40.00, "kind": "flip"}], symbol="XLF")
    r = describe([{"price": 41.00, "kind": "flip"}], symbol="XLF")[0]
    assert r["persistence"]["cycles"] == 1, "un dólar en 40 es otro nivel"


def test_the_identity_travels_in_the_trace_bundle():
    main = _read("app/main.py")
    assert 'payload["level_identity"] = _LI.describe(kept, symbol=symbol)' in main
    # Y NO cambia el cálculo: se describe lo que ya se publicó.
    block = main[main.index('payload["levels"] = kept'):main.index('return walls')]
    assert "describe(kept" in block


def test_the_auditor_shows_the_seven_columns():
    html = _read("app/templates/terminal.html")
    assert 'id="tblLevelIdentity"' in html
    head = html[html.index('id="tblLevelIdentity"'):]
    head = head[:head.index("</thead>")]
    for col in ("PRECIO", "TYPE", "SOURCE", "CAMPO", "METHOD", "MAGNITUD",
                "PERSISTENCIA", "TIMESTAMP"):
        assert col in head, col
    app = _read("app/static/itmq_app.js")
    assert "function renderLevelIdentity(" in app
    assert "state.trace || {}).level_identity" in app


def test_every_visible_line_gets_a_label():
    """`target` y `risk` —los últimos por prioridad— se dibujaban sin nombre."""
    js = _read("app/static/itmq_trace.js")
    body = js[js.index("function drawLevels("):js.index("function drawCrosshair(")]
    # Ya no hay cupo fijo: el sitio sale del alto del panel.
    assert "ordered.slice(0, 8)" not in body
    assert "Math.floor(box.h / Q.LEVEL_LABEL_GAP)" in body
    # Y cuando no caben todas con el nombre largo, se abrevia antes que callar.
    assert "it.rank < LONG ? it.name : it.short" in body
    core = _read("app/static/itmq_core.js")
    for kind in ("target", "risk", "zone"):
        assert re.search(rf"{kind}:\s*\{{[^}}]*short:", core), kind

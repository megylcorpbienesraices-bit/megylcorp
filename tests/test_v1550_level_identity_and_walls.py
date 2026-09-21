"""v1.55.0 · Ninguna linea anonima y una sola autoridad de muros.

DOS DEFECTOS DISTINTOS QUE SE PARECEN
-------------------------------------
1. Una linea SIN identidad. Se ve, parece significar algo y no hay forma de
   saber que. El respaldo del renderer le daba el color neutro y el nombre
   interno del motor, con lo que pasaba por una linea mas.

2. Un muro con DOS autoridades. No es que el calculo estuviera mal: cada seccion
   llamaba a `structural_walls()` con su propio frame, asi que el mismo nombre
   señalaba dos strikes distintos en dos pantallas y ninguna avisaba.

El primero se denuncia con `UNIDENTIFIED_LEVEL`; el segundo se comprueba
comparando los tres sitios donde sale el muro.
"""
from __future__ import annotations

from pathlib import Path

from app.core import level_identity as LI
from app.terminal_api import wall_consistency


def setup_function():
    LI.reset()


# ── UNIDENTIFIED_LEVEL ───────────────────────────────────────────────────

def test_un_kind_desconocido_se_denuncia_no_se_disimula():
    a = LI.audit([{"kind": "misterio", "price": 517.2}], symbol="QQQ")
    assert a["ok"] is False
    assert a["unidentified"] == 1
    assert a["unidentified_kinds"] == ["misterio"]
    assert a["rows"][0]["identity_status"] == LI.UNIDENTIFIED


def test_un_nivel_del_registro_esta_identificado():
    a = LI.audit([{"kind": "call_wall", "price": 510.0}], symbol="QQQ")
    assert a["ok"] is True
    assert a["rows"][0]["identity_status"] == LI.IDENTIFIED
    assert a["rows"][0]["source"] == "wall_engine.walls_from_hub"


def test_un_nivel_que_declara_su_autoridad_tambien_esta_identificado():
    """No hace falta estar en el registro si el propio nivel dice de donde sale."""
    a = LI.audit([{"kind": "otro", "price": 500.0, "authority": "ITMQ_WALL_ENGINE"}],
                 symbol="QQQ")
    assert a["rows"][0]["identity_status"] == LI.IDENTIFIED
    assert a["ok"] is True


def test_el_recuento_es_lo_que_convierte_esto_en_un_guardia():
    """Una lista larga de filas correctas esconde bien las dos que no lo son."""
    niveles = [{"kind": k, "price": 500.0 + i} for i, k in
               enumerate(("flip", "call_wall", "put_wall", "gamma", "delta"))]
    niveles += [{"kind": "raro_1", "price": 517.2}, {"kind": "raro_2", "price": 515.0}]
    a = LI.audit(niveles, symbol="SPY")
    assert a["total"] == 7 and a["unidentified"] == 2
    assert "raro_1" in a["detail"] and "raro_2" in a["detail"]


def test_todos_los_kinds_que_el_renderer_dibuja_estan_en_el_registro():
    """Si el renderer conoce un `kind` que el registro no, esa linea sale con
    color y sin procedencia: identificable a la vista, anonima en el Auditor."""
    js = Path("app/static/itmq_core.js").read_text(encoding="utf-8")
    tabla = js[js.index("const LEVELS = {"):js.index("/** Niveles que el panel de flujo")]
    import re
    kinds = set(re.findall(r"^\s{4}([a-z_0-9]+):\s*\{", tabla, re.M))
    faltan = sorted(kinds - set(LI.LEVEL_ORIGIN))
    assert not faltan, f"el renderer dibuja {faltan} y el registro no los conoce"


def test_el_estilo_de_respaldo_marca_la_linea_en_vez_de_normalizarla():
    js = Path("app/static/itmq_core.js").read_text(encoding="utf-8")
    i = js.index("function levelStyle(kind)")
    cuerpo = js[i:js.index("const ITMQ = {", i)]
    assert "unidentified: true" in cuerpo
    assert "SIN IDENTIDAD" in cuerpo


def test_la_linea_sin_identidad_se_dibuja_distinta():
    js = Path("app/static/itmq_trace.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("function drawLevels"):js.index("function drawLevelHover")]
    assert "unidentified: !!st.unidentified" in cuerpo
    assert "it.unidentified ? [1, 4]" in cuerpo


def test_el_trace_publica_la_auditoria_de_identidad():
    src = Path("app/main.py").read_text(encoding="utf-8")
    assert 'payload["level_identity_audit"]' in src
    assert "_LI.audit(kept, symbol=symbol, cycle_id=_ciclo)" in src


# ── Autoridad única de muros ─────────────────────────────────────────────

def _trace(call_engine=510.0, call_drawn=None, put=495.0):
    return {
        "walls": {"call_wall": {"strike": call_engine, "source_mode": "PROVIDER_STRIKE_EXPOSURE"},
                  "put_wall": {"strike": put}},
        "levels": [{"kind": "call_wall", "price": call_engine if call_drawn is None else call_drawn},
                   {"kind": "put_wall", "price": put}],
    }


def test_los_tres_consumidores_del_muro_dicen_lo_mismo():
    r = wall_consistency(_trace(), {"call_wall": 510.0, "put_wall": 495.0})
    assert r["ok"] is True
    fila = next(x for x in r["rows"] if x["side"] == "call_wall")
    assert fila["engine"] == 510.0 and fila["trace_levels"] == [510.0] and fila["resumen"] == 510.0
    assert fila["authority"] == "ITMQ_WALL_ENGINE"


def test_un_centimo_de_diferencia_ya_son_dos_autoridades():
    """Un muro es un strike, no una estimacion: no hay tolerancia que aplicar."""
    r = wall_consistency(_trace(call_drawn=510.01), {"call_wall": 510.0, "put_wall": 495.0})
    assert r["ok"] is False
    assert "no coinciden" in next(x for x in r["rows"] if x["side"] == "call_wall")["detail"]


def test_dos_lineas_con_el_mismo_nombre_son_un_defecto_aunque_coincidan():
    """Un nivel duplicado no es un nivel mas fuerte: es la misma informacion
    ocupando el sitio de otra."""
    tr = _trace()
    tr["levels"].append({"kind": "call_wall", "price": 510.0})
    r = wall_consistency(tr, {"call_wall": 510.0, "put_wall": 495.0})
    assert r["ok"] is False
    assert next(x for x in r["rows"] if x["side"] == "call_wall")["duplicated_lines"] is True


def test_un_muro_que_nadie_publica_no_rompe_la_comprobacion():
    r = wall_consistency({"walls": {}, "levels": []}, {})
    assert r["ok"] is True


def test_trace_y_flujo_leen_la_misma_lista_de_niveles():
    """Por eso comprobar `levels` una vez cubre las dos secciones."""
    js = Path("app/static/itmq_orderflow.js").read_text(encoding="utf-8")
    assert "Q.FLOW_LEVEL_KINDS.indexOf(l.kind) >= 0" in js
    core = Path("app/static/itmq_core.js").read_text(encoding="utf-8")
    assert "'call_wall', 'put_wall'" in core[core.index("FLOW_LEVEL_KINDS"):][:200]


def test_el_bundle_publica_las_dos_auditorias():
    src = Path("app/terminal_api.py").read_text(encoding="utf-8")
    # v1.56.1 · La comparación lleva ahora su contexto (activo, sesión, ciclo).
    assert 'auditor["walls"] = wall_consistency(trace, resumen_block, symbol=_sym,' in src
    assert 'auditor["level_identity"]' in src

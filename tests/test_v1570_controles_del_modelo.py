"""v1.57.0 · LOS CINCO CONTROLES, AUDITADOS UNO A UNO.

La regla: si el control APLICA, se alimenta con el dato correcto y pasa por
mérito propio. Si matemáticamente NO aplica, queda N/A con justificación.
Nunca se rebaja una severidad para que desaparezca el aviso.

  IV          aplica · `iv_quality.assess()` existía y nadie lo llamaba
  SUPERFICIE  aplica · `ssvi_shadow.fit_ssvi()` existía y nadie lo llamaba
  EXPOSICIÓN  aplica · la capa canónica existía y nadie le entregaba nada
  EV          aplica cuando hay EV; sin EV no hay costes que auditar
  CALIBRACIÓN aplica cuando está LISTA; en COLLECTING no hay partición que purgar

Tres de los cinco avisaban con frases que sugerían «esto no está implementado»
—«no se evaluó», «no hay ajuste de superficie», «no se publicó ninguna
exposición»— cuando el motor estaba escrito y sólo faltaba el cable.
"""
from __future__ import annotations

import pytest

from app.core import model_controls as MC
from app.core.model_risk_auditor import (
    SEVERITY_CRITICAL, SEVERITY_NA, SEVERITY_WARN,
    audit_decision, audit_exposure, audit_iv, audit_surface,
)


def _cadena(vencimientos=(7, 30), strikes=9, spot=500.0):
    filas = []
    for dte in vencimientos:
        for i in range(-(strikes // 2), strikes // 2 + 1):
            K = spot + i * 5
            filas.append({"strike": K, "dte": dte, "option_type": "CALL",
                          "iv": 0.18 + 0.0008 * i * i, "underlying_price": spot,
                          "vega": 10.0, "bid": 3.0, "ask": 3.2})
    return filas


# ═══════════════════════════════════════════════════════════════════════════
# 1 · EL ESTADO N/A EXISTE Y OBLIGA A JUSTIFICARSE
# ═══════════════════════════════════════════════════════════════════════════

def test_un_na_sin_motivo_se_rechaza():
    """Un N/A sin causa es un aviso escondido."""
    from app.core.model_risk_auditor import _na
    with pytest.raises(ValueError):
        _na("AREA", "invariante", "")
    with pytest.raises(ValueError):
        _na("AREA", "invariante", "   ")


def test_el_na_no_se_confunde_ni_con_aprobado_ni_con_aviso():
    from app.core.model_risk_auditor import _na
    f = _na("AREA", "inv", "no hay objeto que auditar en este ciclo")
    assert f.severity == SEVERITY_NA
    assert f.severity not in (SEVERITY_WARN, SEVERITY_CRITICAL)
    assert f.passed is True, "no hay defecto, así que no arrastra la nota"
    assert f.detail


# ═══════════════════════════════════════════════════════════════════════════
# 2 · IV  ·  aplica, y ahora se alimenta
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_cadena_la_IV_es_NA_con_motivo_no_un_aviso():
    f = audit_iv([])[0]
    assert f.severity == SEVERITY_NA
    assert "no hay cadena" in f.detail
    assert "no se evaluó" not in f.detail, (
        "esa frase sugería que el motor de IV no existía; existe")


def test_con_cadena_la_IV_se_mide_de_verdad():
    out = MC.build(_cadena(), symbol="SPY", spot=500.0)
    assert out["iv_count"] > 0
    fs = audit_iv(out["iv_assessments"])
    assert all(f.severity != SEVERITY_NA for f in fs), "con cadena ya no es N/A"
    assert any("identificable" in f.detail for f in fs)


def test_una_cadena_con_IV_mala_sigue_avisando():
    """Alimentar el control no puede volverlo complaciente."""
    filas = _cadena()
    for r in filas:                       # sin extrínseco: IV indeterminable
        r["bid"] = r["ask"] = 0.0
        r["iv"] = None
    out = MC.build(filas, symbol="SPY", spot=500.0)
    fs = audit_iv(out["iv_assessments"])
    if out["iv_count"]:
        assert any((not f.passed) or f.severity == SEVERITY_WARN for f in fs), (
            "una cadena sin IV identificable tiene que denunciarse")


# ═══════════════════════════════════════════════════════════════════════════
# 3 · SUPERFICIE  ·  aplica, y el ajustador ya existía
# ═══════════════════════════════════════════════════════════════════════════

def test_el_ajustador_de_superficie_existia_y_no_lo_llamaba_nadie():
    from app.core import ssvi_shadow
    assert hasattr(ssvi_shadow, "fit_ssvi")
    assert hasattr(ssvi_shadow, "butterfly_conditions")
    assert hasattr(ssvi_shadow, "calendar_monotonic")


def test_con_cadena_suficiente_la_superficie_se_ajusta():
    sup = MC.surface_report(_cadena(), spot=500.0)
    assert sup.get("ready") is True, sup.get("reason")
    assert sup.get("model") in ("SSVI", "eSSVI")
    fs = audit_surface(sup)
    assert all(f.severity != SEVERITY_NA for f in fs)


def test_sin_cortes_suficientes_es_NA_con_el_numero_exacto():
    """No es «no hay ajuste de superficie»: es que la cadena no da."""
    sup = MC.surface_report(_cadena(vencimientos=(7,)), spot=500.0)
    assert sup.get("ready") is False
    f = audit_surface(sup)[0]
    assert f.severity == SEVERITY_NA
    assert "corte" in f.detail and "SSVI necesita" in f.detail


def test_pocos_strikes_por_corte_no_se_ajustan():
    """Ajustar con tres puntos no es ajustar, es unir puntos."""
    cortes = MC.surface_slices(_cadena(strikes=3), spot=500.0)
    assert cortes == []


def test_una_superficie_con_arbitraje_sigue_siendo_CRITICO():
    mala = {"ready": True, "model": "SSVI",
            "slices": [{"butterfly_state": "VIOLATION",
                        "arbitrage_checks": {"pass": False}}],
            "calendar": {"pass": False, "violations": 2}}
    fs = audit_surface(mala)
    assert any(f.severity == SEVERITY_CRITICAL for f in fs), (
        "una densidad implícita negativa no puede pasar por N/A")


# ═══════════════════════════════════════════════════════════════════════════
# 4 · EXPOSICIÓN  ·  la capa canónica existía y nadie le entregaba nada
# ═══════════════════════════════════════════════════════════════════════════

def test_sin_magnitudes_avisa_y_con_magnitudes_pasa_por_merito():
    from app.core.delta_flow import build_delta_flow, canonical_quantities
    assert audit_exposure([])[0].severity == SEVERITY_WARN
    rows = [{"t": "2026-09-21T14:30:00Z", "option_type": "CALL", "size": 40,
             "direction": 1, "spot": 520.0, "greeks": {"delta": 0.55}}]
    qs = canonical_quantities(build_delta_flow(rows, symbol="SPY"))
    assert len(qs) == 2
    f = audit_exposure(qs)[0]
    assert f.passed is True and "unidad canónica" in f.detail


def test_una_unidad_fuera_del_registro_es_CRITICO():
    class Falsa:
        unit = "INVENTADA_POR_MI"
    f = audit_exposure([Falsa()])[0]
    assert f.severity == SEVERITY_CRITICAL


# ═══════════════════════════════════════════════════════════════════════════
# 5 · EV Y CALIBRACIÓN  ·  tres estados, no dos
# ═══════════════════════════════════════════════════════════════════════════

def _hallazgo(findings, inv):
    return next(f for f in findings if f.invariant == inv)


def test_sin_EV_no_hay_costes_que_auditar():
    f = _hallazgo(audit_decision(None, None, None, None), "costes conocidos")
    assert f.severity == SEVERITY_NA


def test_un_EV_publicado_SIN_costes_sigue_siendo_CRITICO():
    """Un EV bruto no es un EV. Eso no se rebaja."""
    f = _hallazgo(audit_decision(None, {"expected_value_r": 0.4}, None, None),
                  "costes conocidos")
    assert f.severity == SEVERITY_CRITICAL


def test_un_EV_con_costes_pasa():
    f = _hallazgo(audit_decision(None, {"expected_value_r": 0.4, "costs": {"fee": 0.65}},
                                 None, None), "costes conocidos")
    assert f.passed is True and f.severity != SEVERITY_NA


@pytest.mark.parametrize("cal,esperado", [
    (None, SEVERITY_NA),
    ({"ready": False, "status": "COLLECTING",
      "method": "TRAIN -> purge -> VALIDATION -> purge -> FINAL OOS"}, SEVERITY_NA),
    ({"ready": True, "purged_sessions": 2}, None),          # pasa
    ({"ready": True, "purged_sessions": 0}, SEVERITY_WARN),  # defecto real
])
def test_la_calibracion_distingue_todavia_no_hay_de_esta_mal_hecha(cal, esperado):
    f = _hallazgo(audit_decision(None, None, cal, None), "OOS válido")
    if esperado is None:
        assert f.passed is True and f.severity != SEVERITY_NA
        assert "purge gap" in f.detail
    else:
        assert f.severity == esperado


def test_el_motor_SI_hace_purge_gap_y_ahora_se_declara():
    """El aviso era un falso negativo: la metodología estaba bien."""
    from pathlib import Path
    src = Path("app/core/calibration.py").read_text(encoding="utf-8")
    assert "TRAIN -> purge -> VALIDATION" in src
    assert '"purged_sessions"' in src


def test_en_COLLECTING_se_enseña_el_metodo_declarado():
    cal = {"ready": False, "status": "COLLECTING",
           "method": "TRAIN -> purge -> VALIDATION(calibrator selection) -> purge -> FINAL OOS"}
    f = _hallazgo(audit_decision(None, None, cal, None), "OOS válido")
    assert "TRAIN -> purge" in f.detail, (
        "decir que no aplica sin enseñar el método deja al lector sin saber si lo hay")


# ═══════════════════════════════════════════════════════════════════════════
# 6 · EL CABLE, DE PUNTA A PUNTA
# ═══════════════════════════════════════════════════════════════════════════

def test_los_controles_se_calculan_donde_vive_la_cadena():
    from pathlib import Path
    src = Path("app/service.py").read_text(encoding="utf-8")
    assert "self.model_controls_report = _MC.build(" in src
    assert '"model_controls": self.model_controls_report or {}' in src


def test_el_auditor_recibe_IV_y_SUPERFICIE():
    from pathlib import Path
    src = Path("app/terminal_api.py").read_text(encoding="utf-8")
    assert "iv_assessments=_iv, surface=_sup, iv_reason=_iv_motivo," in src


def test_el_reporte_sobrevive_a_un_cambio_de_simbolo():
    from pathlib import Path
    src = Path("app/service.py").read_text(encoding="utf-8")
    assert "'model_controls_report','data_quality_report'" in src, (
        "sin estar en la lista de respaldo, un cambio de activo fallido lo perdería")
    assert src.count("self.model_controls_report={}") >= 2, (
        "tiene que limpiarse en los dos reseteos de cambio de símbolo")


@pytest.mark.parametrize("sym", ["DIA", "SPY", "QQQ"])
def test_funciona_igual_en_los_tres_activos(sym):
    out = MC.build(_cadena(), symbol=sym, spot=500.0)
    assert out["symbol"] == sym
    assert out["iv_count"] > 0
    assert out["surface"].get("ready") is True

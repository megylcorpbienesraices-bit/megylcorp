"""Tests del cortacircuitos de frescura (v1.27.6)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.core.freshness import (
    CLOSED,
    FRESH,
    MACRO_POLICY,
    OPEN,
    OPTION_CHAIN_POLICY,
    STALE,
    UNDERLYING_POLICY,
    UNKNOWN,
    WARN,
    FreshnessBreaker,
    FreshnessPolicy,
    StaleDataError,
    assess,
    require_fresh,
    reset_publication_gate,
    to_epoch,
)


@pytest.fixture(autouse=True)
def _fresh_publication_gate_registry():
    # Integration tests must not leak the stateful LIVE breaker across cases.
    reset_publication_gate()
    yield
    reset_publication_gate()

AHORA = 1_800_000_000.0


def _hace(segundos: float) -> float:
    return AHORA - segundos


# ------------------------------------------------------- normalizacion


def test_acepta_epoch_datetime_pandas_e_iso():
    momento = datetime(2026, 9, 11, 15, 30, tzinfo=timezone.utc)
    esperado = momento.timestamp()
    assert to_epoch(esperado) == pytest.approx(esperado)
    assert to_epoch(momento) == pytest.approx(esperado)
    assert to_epoch(pd.Timestamp(momento)) == pytest.approx(esperado)
    assert to_epoch("2026-09-11T15:30:00Z") == pytest.approx(esperado)
    assert to_epoch("2026-09-11T15:30:00+00:00") == pytest.approx(esperado)


def test_naive_se_interpreta_como_utc_no_como_hora_local():
    naive = datetime(2026, 9, 11, 15, 30)
    aware = datetime(2026, 9, 11, 15, 30, tzinfo=timezone.utc)
    assert to_epoch(naive) == pytest.approx(to_epoch(aware))


def test_basura_devuelve_none_en_vez_de_reventar():
    for valor in (None, "no es fecha", object(), True, float("inf")):
        assert to_epoch(valor) is None


# ------------------------------------------------------------ evaluacion


def test_dato_reciente_es_fresco():
    r = assess(_hace(5), OPTION_CHAIN_POLICY, now=AHORA)
    assert r["estado"] == FRESH and r["antiguedad_s"] == pytest.approx(5.0)


def test_zona_intermedia_avisa_sin_bloquear():
    assert assess(_hace(50), OPTION_CHAIN_POLICY, now=AHORA)["estado"] == WARN


def test_por_encima_del_limite_es_rancio():
    assert assess(_hace(120), OPTION_CHAIN_POLICY, now=AHORA)["estado"] == STALE


def test_timestamp_ausente_no_se_confunde_con_fresco():
    """El fallo mas peligroso: sin timestamp, jamas asumir que el dato sirve."""
    assert assess(None, OPTION_CHAIN_POLICY, now=AHORA)["estado"] == UNKNOWN


def test_timestamp_en_el_futuro_se_trata_como_fallo():
    r = assess(AHORA + 3600, UNDERLYING_POLICY, now=AHORA)
    assert r["estado"] == STALE and "reloj" in r["detalle"]


def test_con_mercado_cerrado_rige_el_limite_amplio():
    viejo = _hace(6 * 3600)
    assert assess(viejo, OPTION_CHAIN_POLICY, market_open=True, now=AHORA)["estado"] == STALE
    assert assess(viejo, OPTION_CHAIN_POLICY, market_open=False, now=AHORA)["estado"] == FRESH


def test_ni_con_mercado_cerrado_se_acepta_un_dato_de_la_semana_pasada():
    semana = _hace(8 * 24 * 3600)
    assert assess(semana, OPTION_CHAIN_POLICY, market_open=False, now=AHORA)["estado"] == STALE


def test_politicas_por_defecto_estan_ordenadas_por_exigencia():
    assert UNDERLYING_POLICY.max_age_seconds < OPTION_CHAIN_POLICY.max_age_seconds < MACRO_POLICY.max_age_seconds


def test_require_fresh_lanza_con_el_diagnostico_adjunto():
    with pytest.raises(StaleDataError) as exc:
        require_fresh(_hace(600), UNDERLYING_POLICY, now=AHORA)
    assert exc.value.assessment["estado"] == STALE
    assert exc.value.assessment["politica"] == "subyacente"


def test_require_fresh_deja_pasar_el_dato_bueno():
    assert require_fresh(_hace(2), UNDERLYING_POLICY, now=AHORA)["estado"] == FRESH


# --------------------------------------------------------- cortacircuitos


def test_un_solo_fallo_no_abre_el_circuito():
    b = FreshnessBreaker(OPTION_CHAIN_POLICY, trip_after=2)
    b.observe(_hace(900), now=AHORA)
    assert b.state == CLOSED and b.allow()


def test_fallos_consecutivos_abren_el_circuito():
    b = FreshnessBreaker(OPTION_CHAIN_POLICY, trip_after=2)
    b.observe(_hace(900), now=AHORA)
    estado = b.observe(_hace(900), now=AHORA)
    assert b.state == OPEN and not b.allow()
    assert estado["circuito"] == OPEN and b.trips == 1


def test_una_lectura_buena_reinicia_el_contador_de_fallos():
    b = FreshnessBreaker(OPTION_CHAIN_POLICY, trip_after=3)
    b.observe(_hace(900), now=AHORA)
    b.observe(_hace(1), now=AHORA)
    b.observe(_hace(900), now=AHORA)
    assert b.state == CLOSED


def test_el_rearme_exige_varias_lecturas_frescas_seguidas():
    b = FreshnessBreaker(OPTION_CHAIN_POLICY, trip_after=1, recover_after=3)
    b.observe(_hace(900), now=AHORA)
    assert b.state == OPEN
    b.observe(_hace(1), now=AHORA)
    b.observe(_hace(1), now=AHORA)
    assert b.state == OPEN, "no debe rearmarse a la primera"
    b.observe(_hace(1), now=AHORA)
    assert b.state == CLOSED and b.allow()


def test_guard_impide_publicar_cifras_con_el_circuito_abierto():
    b = FreshnessBreaker(OPTION_CHAIN_POLICY, trip_after=1)
    b.observe(None, now=AHORA)
    with pytest.raises(StaleDataError):
        b.guard()


def test_guard_no_estorba_con_el_circuito_cerrado():
    b = FreshnessBreaker(OPTION_CHAIN_POLICY)
    b.observe(_hace(1), now=AHORA)
    b.guard()


def test_snapshot_expone_el_estado_para_diagnostico():
    b = FreshnessBreaker(FreshnessPolicy("prueba", 10.0), trip_after=1)
    b.observe(_hace(999), now=AHORA)
    s = b.snapshot()
    assert s["circuito"] == OPEN and s["politica"] == "prueba" and s["aperturas"] == 1
    assert s["ultima_evaluacion"]["estado"] == STALE


def test_reset_manual_cierra_el_circuito():
    b = FreshnessBreaker(OPTION_CHAIN_POLICY, trip_after=1)
    b.observe(None, now=AHORA)
    assert not b.allow()
    b.reset()
    assert b.allow() and b.consecutive_stale == 0


# ------------------------- cableado real en build_data_quality


def _meta(option_age_s: float, stock_age_s: float, market_state: str = "OPEN") -> dict:
    reloj = pd.Timestamp("2026-09-11 15:30:00", tz="UTC")
    return {"quality_clock": reloj, "market_state": market_state,
            "latest_option_market_timestamp": reloj - pd.Timedelta(seconds=option_age_s),
            "stock_market_timestamp": reloj - pd.Timedelta(seconds=stock_age_s),
            "matched_snapshots": 10, "contract_definitions": 10}


def _data_quality(meta: dict) -> dict:
    from app.core.precision_engine import build_data_quality
    df = pd.DataFrame({"strike": [1.0], "bid": [1.0], "ask": [1.1],
                       "calc_delta": [0.5], "calc_gamma": [0.01]})
    return build_data_quality(df, meta, {}, {"count": 1, "mode": "WEEKLY"})


def test_data_quality_expone_el_circuito_cerrado_con_datos_frescos():
    circuito = _data_quality(_meta(5, 2))["circuito_frescura"]
    assert circuito["estado"] == CLOSED and circuito["publicar_permitido"] is True


def test_data_quality_bloquea_desde_la_primera_cadena_rancia():
    circuito = _data_quality(_meta(900, 2))["circuito_frescura"]
    # v1.27.7: fail-closed inmediato, latch OPEN tras dos ciclos malos.
    assert circuito["estado"] == "BLOCKED_PENDING" and circuito["publicar_permitido"] is False
    assert circuito["cadena_opciones"]["estado"] == STALE


def test_data_quality_latch_open_en_segundo_ciclo_rancio_y_rearma_en_tres_frescos():
    m1 = _meta(900, 2)
    c1 = _data_quality(m1)["circuito_frescura"]
    assert c1["estado"] == "BLOCKED_PENDING" and not c1["publicar_permitido"]

    m2 = _meta(900, 2)
    m2["quality_clock"] = m1["quality_clock"] + pd.Timedelta(seconds=1)
    m2["latest_option_market_timestamp"] = m2["quality_clock"] - pd.Timedelta(seconds=900)
    m2["stock_market_timestamp"] = m2["quality_clock"] - pd.Timedelta(seconds=2)
    c2 = _data_quality(m2)["circuito_frescura"]
    assert c2["estado"] == OPEN and not c2["publicar_permitido"]

    for i in range(3):
        good = _meta(5, 2)
        good["quality_clock"] = m2["quality_clock"] + pd.Timedelta(seconds=i + 1)
        good["latest_option_market_timestamp"] = good["quality_clock"] - pd.Timedelta(seconds=5)
        good["stock_market_timestamp"] = good["quality_clock"] - pd.Timedelta(seconds=2)
        c = _data_quality(good)["circuito_frescura"]
        if i < 2:
            assert c["estado"] == OPEN and not c["publicar_permitido"]
    assert c["estado"] == CLOSED and c["publicar_permitido"] is True


def test_data_quality_abre_el_circuito_con_subyacente_rancio():
    """El score suave solo penalizaba; el gate debe negarse."""
    resultado = _data_quality(_meta(5, 600))
    assert resultado["circuito_frescura"]["publicar_permitido"] is False
    assert resultado["circuito_frescura"]["subyacente"]["estado"] == STALE


def test_modo_demo_no_finge_frescura_perfecta_en_el_gate():
    """Con market_state DEMO el score se forzaba a 1.0; el gate usa el limite de cerrado."""
    resultado = _data_quality(_meta(10 * 24 * 3600, 10 * 24 * 3600, market_state="DEMO"))
    assert resultado["components"]["freshness"] == 100.0, "el score suave sigue igual"
    assert resultado["circuito_frescura"]["publicar_permitido"] is False, "el gate duro si lo detecta"


def test_sin_timestamps_el_gate_no_asume_que_el_dato_sirve():
    resultado = _data_quality({"quality_clock": pd.Timestamp("2026-09-11 15:30:00", tz="UTC"),
                               "market_state": "OPEN", "matched_snapshots": 10})
    assert resultado["circuito_frescura"]["publicar_permitido"] is False


def test_assess_age_equivale_a_assess_con_timestamp():
    from app.core.freshness import assess_age
    directo = assess(_hace(50), OPTION_CHAIN_POLICY, now=AHORA)
    por_edad = assess_age(50.0, OPTION_CHAIN_POLICY)
    assert directo["estado"] == por_edad["estado"]
    assert por_edad["antiguedad_s"] == pytest.approx(50.0)

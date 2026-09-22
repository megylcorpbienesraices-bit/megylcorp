"""v1.57.0 · CONTRATO DE CALL WALL Y PUT WALL · la fórmula, los diez datos y el veredicto.

    Gamma Exposure por strike = gamma × OI × multiplicador × precio² × 0.01

    Call Wall = strike con MAYOR Gamma Exposure de CALLS
    Put Wall  = strike con MAYOR Gamma Exposure de PUTS

Cada método equivocado tiene aquí una prueba CONSTRUIDA PARA QUE GANE si alguien
lo reintroduce: la cadena de `_cadena()` pone el mayor OI, el mayor volumen, el
neto más grande y el Max Pain en strikes DISTINTOS al muro. Si el ranking vuelve
a ordenar por cualquiera de ellos, el muro se mueve y la prueba lo dice.

El defecto de fondo que cierra esta versión: el muro se elegía con una media
geométrica de la exposición YA AGREGADA del proveedor y el interés abierto. El OI
va dentro de la fórmula de exposición, así que multiplicar otra vez por él lo
cuenta DOS VECES y desplaza el muro hacia strikes con mucho libro y gamma
pequeña. No se ve en el resultado: da un número grande y creíble.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core import wall_gex as WG
from app.core import wall_engine as WE


VENCIMIENTO = "2026-09-25"          # viernes
OTRO_VENCIMIENTO = "2026-10-16"
SPOT = 100.0
AHORA = "2026-09-22T15:30:00+00:00"


def _contrato(strike, side, *, gamma, oi, volume=10.0, expiry=VENCIMIENTO,
              iv=0.22, delta=0.5, t="2026-09-22T15:29:58+00:00"):
    return {"strike": strike, "option_type": side, "expiration": expiry,
            "gamma": gamma, "open_interest": oi, "volume": volume,
            "implied_volatility": iv, "delta": delta, "t": t}


def _cadena():
    """Cadena sana de 21 strikes, con las trampas en su sitio.

        muro de calls   105  (mayor GEX de calls)
        muro de puts     95  (mayor GEX de puts)
        mayor OI        110  con gamma despreciable   → si gana, se ordenó por OI
        mayor volumen    92  con gamma despreciable   → si gana, se ordenó por volumen
        neto ≈ 0        105  call y put enormes a la vez → el método neto lo pierde
    """
    filas = []
    for k in range(90, 111):
        k = float(k)
        gamma_call = 0.010
        gamma_put = 0.010
        oi_call = 1_000.0
        oi_put = 1_000.0
        vol = 10.0
        if k == 105.0:
            gamma_call, oi_call = 0.050, 4_000.0        # muro de calls
            gamma_put, oi_put = 0.050, 3_900.0          # y neto casi nulo aquí
        if k == 95.0:
            gamma_put, oi_put = 0.045, 5_000.0          # muro de puts
        if k == 110.0:
            gamma_call, oi_call = 0.0001, 90_000.0      # mayor OI, gamma mínima
            gamma_put, oi_put = 0.0001, 90_000.0
        if k == 92.0:
            vol = 500_000.0                             # mayor volumen
        filas.append(_contrato(k, "call", gamma=gamma_call, oi=oi_call, volume=vol))
        filas.append(_contrato(k, "put", gamma=gamma_put, oi=oi_put, volume=vol))
    return filas


def _muros(**kwargs):
    base = dict(contract_rows=_cadena(), spot=SPOT, price_as_of=AHORA,
                ages={"gamma": 10.0, "open_interest": 10.0, "price": 12.0},
                sources={"gamma": "QUANTDATA_CONTRACT_GREEKS",
                         "open_interest": "QUANTDATA_CONTRACT_GREEKS",
                         "price": "SESSION_CANDLE_CLOSE"})
    base.update(kwargs)
    return WG.walls("DIA", **base)


# ═══════════════════════════════════════════════════════════════════════════
# 1 · LA FÓRMULA
# ═══════════════════════════════════════════════════════════════════════════

def test_la_formula_es_la_publicada():
    """gamma × OI × multiplicador × precio² × 0.01, sin atajos."""
    gex = WG.gamma_exposure(gamma=0.05, open_interest=4_000.0, spot=100.0,
                            multiplier=100.0)
    assert gex == pytest.approx(0.05 * 4_000.0 * 100.0 * 100.0 ** 2 * 0.01)
    # 0,05 × 4.000 × 100 × 100² × 0,01 = 2.000.000 de exposición por un 1 %
    assert gex == pytest.approx(2_000_000.0)


def test_el_multiplicador_no_se_da_por_supuesto():
    cien = WG.gamma_exposure(gamma=0.05, open_interest=100, spot=50, multiplier=100)
    diez = WG.gamma_exposure(gamma=0.05, open_interest=100, spot=50, multiplier=10)
    assert cien == pytest.approx(diez * 10)


def test_un_factor_que_falta_no_es_un_cero():
    """Cero es «se midió y no hay»; None es «falta un dato». Confundirlos borra
    el strike del ranking sin que nadie se entere."""
    assert WG.gamma_exposure(gamma=None, open_interest=1, spot=1) is None
    assert WG.gamma_exposure(gamma=0.1, open_interest=None, spot=1) is None
    assert WG.gamma_exposure(gamma=0.1, open_interest=1, spot=None) is None
    assert WG.gamma_exposure(gamma=0.0, open_interest=1, spot=1) == 0.0


def test_el_precio_entra_al_cuadrado():
    uno = WG.gamma_exposure(gamma=0.1, open_interest=10, spot=100)
    dos = WG.gamma_exposure(gamma=0.1, open_interest=10, spot=200)
    assert dos == pytest.approx(uno * 4)


# ═══════════════════════════════════════════════════════════════════════════
# 2 · EL MURO ES EL MÁXIMO DE SU LADO, Y LOS LADOS NO SE TOCAN
# ═══════════════════════════════════════════════════════════════════════════

def test_call_wall_es_el_strike_de_mayor_gex_de_calls():
    out = _muros()
    assert out[WG.CALL_WALL]["ready"] is True
    assert out[WG.CALL_WALL]["strike"] == 105.0


def test_put_wall_es_el_strike_de_mayor_gex_de_puts():
    out = _muros()
    assert out[WG.PUT_WALL]["ready"] is True
    assert out[WG.PUT_WALL]["strike"] == 95.0


def test_los_dos_lados_se_calculan_por_separado():
    out = _muros()
    assert out["sides_kept_separate"] is True
    calls = WG.rank_side(WG.dedupe_contracts(_cadena()), side=WG.CALL, spot=SPOT)
    puts = WG.rank_side(WG.dedupe_contracts(_cadena()), side=WG.PUT, spot=SPOT)
    # Ninguna fila de un lado puede llevar el OI del otro.
    assert all(f["side"] == WG.CALL for f in calls)
    assert all(f["side"] == WG.PUT for f in puts)
    c105 = next(f for f in calls if f["strike"] == 105.0)
    p105 = next(f for f in puts if f["strike"] == 105.0)
    assert c105["open_interest"] == 4_000.0
    assert p105["open_interest"] == 3_900.0


def test_el_muro_publica_los_cinco_numeros_que_lo_sostienen():
    """strike, gamma, OI, precio y hora. Y el vencimiento, que los ata a una fecha."""
    muro = _muros()[WG.CALL_WALL]
    assert muro["strike"] == 105.0
    assert muro["gamma"] == pytest.approx(0.050)
    assert muro["open_interest"] == pytest.approx(4_000.0)
    assert muro["spot"] == pytest.approx(SPOT)
    assert muro["price_as_of"] == AHORA
    assert muro["expiry"] == VENCIMIENTO
    assert muro["gex"] == pytest.approx(0.05 * 4_000 * 100 * SPOT ** 2 * 0.01)
    assert muro["method"] == WG.FORMULA


# ═══════════════════════════════════════════════════════════════════════════
# 3 · CÓMO **NO** SE CALCULA · una prueba por método equivocado
# ═══════════════════════════════════════════════════════════════════════════

def test_no_es_el_strike_con_mayor_interes_abierto():
    """110 tiene 90.000 contratos —el mayor OI de la cadena— y gamma mínima."""
    filas = WG.rank_side(WG.dedupe_contracts(_cadena()), side=WG.CALL, spot=SPOT)
    por_oi = max(filas, key=lambda f: f["open_interest"])
    assert por_oi["strike"] == 110.0, "la trampa dejó de estar en 110"
    assert _muros()[WG.CALL_WALL]["strike"] != 110.0
    assert _muros()[WG.CALL_WALL]["strike"] == 105.0


def test_no_es_el_strike_con_mayor_volumen():
    """92 rota medio millón de contratos y no sostiene ningún muro."""
    filas = WG.rank_side(WG.dedupe_contracts(_cadena()), side=WG.PUT, spot=SPOT)
    por_volumen = max(filas, key=lambda f: f["volume"] or 0.0)
    assert por_volumen["strike"] == 92.0, "la trampa dejó de estar en 92"
    assert _muros()[WG.PUT_WALL]["strike"] == 95.0


def test_no_es_la_gamma_neta():
    """En 105 la call gamma y la put gamma son enormes y casi se cancelan.

    El método neto mira ese strike y ve poco. Es justo donde más cobertura hay.
    """
    contratos = WG.dedupe_contracts(_cadena())
    calls = {f["strike"]: f["gex"] for f in WG.rank_side(contratos, side=WG.CALL, spot=SPOT)}
    puts = {f["strike"]: f["gex"] for f in WG.rank_side(contratos, side=WG.PUT, spot=SPOT)}
    neto = {k: calls[k] - puts[k] for k in calls}
    assert abs(neto[105.0]) < calls[105.0] * 0.05, "la trampa del neto se deshizo"
    assert max(neto, key=lambda k: abs(neto[k])) != 105.0
    assert _muros()[WG.CALL_WALL]["strike"] == 105.0


def test_no_es_el_max_pain():
    """El Max Pain minimiza el valor liquidado: otra pregunta y otro número.

    Con esta cadena el strike de mayor OI total es 110; cualquier método que
    tienda al centro de masa del OI —Max Pain incluido— se va hacia allí.
    """
    out = _muros()
    assert out[WG.CALL_WALL]["strike"] == 105.0
    assert out[WG.PUT_WALL]["strike"] == 95.0
    assert "el Max Pain" in " ".join(
        WE.wall_audit(WE.resolve_walls(
            "DIA", exposure_rows=[], spot=SPOT, contract_rows=_cadena(),
            price_as_of=AHORA)).get("not_calculated_as") or [])


def test_no_se_mezclan_vencimientos_en_silencio():
    """Un strike gigantesco en OTRO vencimiento no puede ganar el muro."""
    filas = _cadena() + [
        _contrato(107.0, "call", gamma=0.9, oi=50_000.0, expiry=OTRO_VENCIMIENTO),
        _contrato(93.0, "put", gamma=0.9, oi=50_000.0, expiry=OTRO_VENCIMIENTO),
    ]
    out = _muros(contract_rows=filas)
    assert out["expiry"] == VENCIMIENTO
    assert out[WG.CALL_WALL]["strike"] == 105.0
    assert out[WG.PUT_WALL]["strike"] == 95.0
    # Y lo que había disponible se declara, en vez de desaparecer.
    assert out["expiries_available"] == [VENCIMIENTO, OTRO_VENCIMIENTO]
    assert out["expiry_reason"]


def test_un_contrato_repetido_no_multiplica_su_exposicion():
    """Las griegas llegan en filas de ORDER FLOW, que son operaciones.

    Cien prints del mismo contrato sumados darían cien veces su interés abierto:
    un muro de la nada, con un número grande y creíble.
    """
    filas = _cadena() + [_contrato(101.0, "call", gamma=0.02, oi=2_000.0)] * 100
    out = _muros(contract_rows=filas)
    assert out[WG.CALL_WALL]["strike"] == 105.0
    fila101 = next(f for f in WG.rank_side(WG.dedupe_contracts(filas),
                                           side=WG.CALL, spot=SPOT)
                   if f["strike"] == 101.0)
    assert fila101["open_interest"] == pytest.approx(2_000.0)
    assert fila101["contracts"] == 1


# ═══════════════════════════════════════════════════════════════════════════
# 4 · VENCIMIENTO OPERATIVO
# ═══════════════════════════════════════════════════════════════════════════

def test_el_vencimiento_mas_cercano_es_el_de_por_defecto():
    venc, motivo = WG.select_expiry(_cadena() + [
        _contrato(100.0, "call", gamma=0.01, oi=1.0, expiry=OTRO_VENCIMIENTO)])
    assert venc == VENCIMIENTO
    assert "más cercano" in motivo


def test_se_puede_pedir_el_viernes_semanal():
    filas = [_contrato(100.0, "call", gamma=0.01, oi=1.0, expiry="2026-09-23"),
             _contrato(100.0, "call", gamma=0.01, oi=1.0, expiry=VENCIMIENTO)]
    venc, motivo = WG.select_expiry(filas, policy=WG.WEEKLY_FRIDAY)
    assert venc == VENCIMIENTO
    assert "viernes" in motivo


def test_un_vencimiento_pedido_que_no_esta_se_declara_y_no_apaga_la_pantalla():
    venc, motivo = WG.select_expiry(_cadena(), requested="2026-12-19")
    assert venc == VENCIMIENTO
    assert "2026-12-19" in motivo and "no está en la cadena" in motivo


def test_sin_vencimiento_en_las_filas_se_dice():
    venc, motivo = WG.select_expiry([{"strike": 100, "option_type": "call"}])
    assert venc is None
    assert "vencimiento" in motivo


# ═══════════════════════════════════════════════════════════════════════════
# 5 · LOS DIEZ CONTROLES Y EL VEREDICTO
# ═══════════════════════════════════════════════════════════════════════════

def test_con_los_diez_datos_la_wall_es_CONFIRMADA():
    out = _muros()
    assert out["verdict"] == WG.CONFIRMED, out["failed_controls"]
    assert out["failed_controls"] == []
    assert [c["control"] for c in out["controls"]] == list(WG.CONTROL_KEYS)
    assert all(c["ok"] for c in out["controls"])
    assert out[WG.CALL_WALL]["verdict"] == WG.CONFIRMED


def _falla(out, control):
    assert out["verdict"] == WG.PROVISIONAL
    assert control in out["failed_controls"], out["failed_controls"]
    fila = next(c for c in out["controls"] if c["control"] == control)
    assert fila["ok"] is False
    assert fila["detail"], "un control que falla sin decir por qué no sirve"
    return fila


def test_un_hueco_en_la_escalera_de_strikes_deja_la_wall_PROVISIONAL():
    filas = [r for r in _cadena() if r["strike"] not in (102.0, 103.0)]
    fila = _falla(_muros(contract_rows=filas), "CADENA_COMPLETA")
    assert fila["evidence"]["gaps"]


def test_sin_cobertura_fuera_del_dinero_la_wall_es_PROVISIONAL():
    filas = [r for r in _cadena() if 99.0 <= r["strike"] <= 101.0]
    out = _muros(contract_rows=filas)
    _falla(out, "STRIKES_FUERA_DEL_DINERO")


def test_un_contrato_sin_interes_abierto_deja_la_wall_PROVISIONAL():
    filas = _cadena()
    for r in filas:
        if r["strike"] == 104.0:
            r["open_interest"] = None
    fila = _falla(_muros(contract_rows=filas), "OI_POR_STRIKE_Y_LADO")
    assert 104.0 in fila["evidence"]["strikes_without_oi"]


def test_un_contrato_sin_gamma_deja_la_wall_PROVISIONAL():
    filas = _cadena()
    for r in filas:
        if r["strike"] == 106.0:
            r["gamma"] = None
    fila = _falla(_muros(contract_rows=filas), "GAMMA_VALIDA_POR_CONTRATO")
    assert 106.0 in fila["evidence"]["strikes_without_gamma"]


def test_un_precio_sin_hora_deja_la_wall_PROVISIONAL():
    _falla(_muros(price_as_of=None), "PRECIO_CON_HORA")


def test_sin_iv_ni_delta_la_gamma_no_se_puede_validar():
    filas = _cadena()
    for r in filas:
        r["implied_volatility"] = None
        r["delta"] = None
    fila = _falla(_muros(contract_rows=filas), "IV_DELTA_MULTIPLICADOR")
    assert fila["evidence"]["contracts_without_iv"] > 0


def test_sin_convencion_declarada_solo_hay_gamma_matematica():
    fila = _falla(_muros(convention=None), "CONVENCION_DE_POSICIONAMIENTO")
    assert "presión de cobertura" in fila["detail"]


def test_la_convencion_se_declara_y_no_se_presenta_como_medicion():
    out = _muros()
    fila = next(c for c in out["controls"]
                if c["control"] == "CONVENCION_DE_POSICIONAMIENTO")
    assert fila["evidence"]["convention"] == WG.POSITIONING_CONVENTION
    assert fila["evidence"]["measured_dealer_inventory"] is False
    assert out[WG.CALL_WALL]["positioning_convention"] == WG.POSITIONING_CONVENTION


def test_un_dato_viejo_deja_la_wall_PROVISIONAL():
    out = _muros(ages={"gamma": 900.0, "open_interest": 900.0, "price": 905.0})
    _falla(out, "SIN_DATOS_VIEJOS_MEZCLADOS")


def test_gamma_y_precio_de_horas_distintas_dejan_la_wall_PROVISIONAL():
    out = _muros(ages={"gamma": 5.0, "open_interest": 5.0, "price": 400.0})
    fila = next(c for c in out["controls"] if c["control"] == "MISMA_HORA_MISMA_FUENTE")
    assert fila["ok"] is False
    assert fila["evidence"]["skew_seconds"] > WG.MAX_SKEW_S


def test_gamma_y_oi_de_fuentes_distintas_dejan_la_wall_PROVISIONAL():
    out = _muros(sources={"gamma": "QUANTDATA_CONTRACT_GREEKS",
                          "open_interest": "OTRO_PROVEEDOR",
                          "price": "SESSION_CANDLE_CLOSE"})
    fila = _falla(out, "MISMA_HORA_MISMA_FUENTE")
    assert "fuentes distintas" in fila["detail"]


def test_una_wall_PROVISIONAL_se_sigue_publicando():
    """Faltar un dato no es motivo para dejar la pantalla en blanco."""
    out = _muros(price_as_of=None)
    assert out["verdict"] == WG.PROVISIONAL
    assert out[WG.CALL_WALL]["ready"] is True
    assert out[WG.CALL_WALL]["strike"] == 105.0
    assert out[WG.CALL_WALL]["verdict"] == WG.PROVISIONAL


def test_la_wall_se_declara_como_probabilidad_no_como_barrera():
    out = _muros()
    assert "no es una barrera garantizada" in out["note"] or \
           "no una barrera garantizada" in out["note"]


# ═══════════════════════════════════════════════════════════════════════════
# 6 · CABLEADO REAL · el Wall Engine sigue siendo la autoridad única
# ═══════════════════════════════════════════════════════════════════════════

def test_el_wall_engine_usa_la_formula_cuando_hay_griegas_por_contrato():
    WE.WALLS.reset()
    out = WE.resolve_walls("DIA", exposure_rows=[], oi_rows=[], spot=SPOT,
                           contract_rows=_cadena(), price_as_of=AHORA,
                           ages={"gamma": 5.0, "open_interest": 5.0, "price": 6.0},
                           sources={"gamma": "QD", "open_interest": "QD",
                                    "price": "SIP"})
    assert out["verdict"] == WG.CONFIRMED, out["gex_contract"]["failed_controls"]
    assert out[WG.CALL_WALL]["strike"] == 105.0
    assert out[WG.PUT_WALL]["strike"] == 95.0
    assert out[WG.CALL_WALL]["method"] == WG.FORMULA
    assert out[WG.CALL_WALL]["authority"] == "ITMQ_WALLS_GEX_V1"
    assert out[WG.CALL_WALL]["fallback_used"] is False
    # La línea que TRACE dibuja es exactamente el strike de la fórmula.
    niveles = {lv["kind"]: lv["price"] for lv in out["levels"]}
    assert niveles["call_wall"] == 105.0
    assert niveles["put_wall"] == 95.0


def test_sin_griegas_por_contrato_la_via_anterior_sostiene_la_pantalla():
    """El respaldo no desaparece: entra etiquetado, nunca disfrazado."""
    WE.WALLS.reset()
    exposicion = [{"strike": 104.0, "call_gex": 900.0, "put_gex": 10.0},
                  {"strike": 96.0, "call_gex": 10.0, "put_gex": 800.0}]
    out = WE.resolve_walls("SPY", exposure_rows=exposicion, oi_rows=[], spot=SPOT)
    assert out[WG.CALL_WALL]["strike"] == 104.0
    assert out[WG.CALL_WALL]["method"] != WG.FORMULA
    assert out.get("verdict") is None


def test_la_auditoria_publica_el_veredicto_y_los_diez_controles():
    WE.WALLS.reset()
    out = WE.resolve_walls("DIA", exposure_rows=[], spot=SPOT,
                           contract_rows=_cadena(), price_as_of=AHORA,
                           ages={"gamma": 5.0, "open_interest": 5.0, "price": 6.0},
                           sources={"gamma": "QD", "open_interest": "QD",
                                    "price": "SIP"})
    aud = WE.wall_audit(out, {})
    assert aud["verdict"] == WG.CONFIRMED
    assert [c["control"] for c in aud["controls"]] == list(WG.CONTROL_KEYS)
    assert aud["gex_formula"] == WG.FORMULA
    assert aud["expiry"] == VENCIMIENTO
    assert aud["positioning_convention"] == WG.POSITIONING_CONVENTION
    lado = aud["sides"][WG.CALL_WALL]
    for campo in ("selected_strike", "gamma", "open_interest", "spot_used",
                  "price_as_of", "expiry", "verdict"):
        assert lado.get(campo) is not None, campo
    # Los cinco métodos prohibidos viajan escritos, para que nadie los reinvente.
    prohibidos = " · ".join(aud["not_calculated_as"])
    for pieza in ("interés abierto", "volumen", "gamma neta", "Max Pain",
                  "vencimientos"):
        assert pieza in prohibidos


def test_el_hub_pasa_las_griegas_por_contrato_al_motor():
    src = Path("app/core/wall_engine.py").read_text(encoding="utf-8")
    bloque = src[src.index("def walls_from_hub"):src.index("def wall_audit")]
    assert 'h.get("contract_greeks")' in bloque
    assert "contract_rows=contract_rows" in bloque
    assert "ages=edades" in bloque
    assert '"gamma": fuente_griegas' in bloque


def test_el_precio_llega_con_su_hora_desde_la_terminal():
    src = Path("app/main.py").read_text(encoding="utf-8")
    bloque = src[src.index("def _resolve_walls"):]
    bloque = bloque[:bloque.index("payload[\"levels\"] = kept")]
    assert "price_as_of=price_as_of" in bloque
    assert "price_age_s=price_age_s" in bloque

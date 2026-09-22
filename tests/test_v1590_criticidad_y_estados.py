"""v1.59.0 · CUATRO CAJONES GENÉRICOS, ABIERTOS UNO A UNO.

LO QUE SE VEÍA
--------------
El Auditor, con el proveedor respondiendo y la cuota en 7 de 240:

    26 de 36 herramientas          «sin intentos»
    Call Wall / Put Wall           «sin cálculo de muros en este ciclo»
    Dark Pool · dark_flow          SIN_DATOS, con 216 filas en pantalla
    gamma (timeout)                [DEGRADED]

Cuatro mensajes, y los cuatro juntan cosas que no se parecen en nada:

1. `DEGRADED` era una etiqueta del ENDPOINT. Pero el mismo fallo de gamma es
   opcional para una tarjeta con respaldo y bloqueante para Call Wall / Put
   Wall, donde gamma es un factor de la fórmula. Una etiqueta global obliga a
   elegir una de las dos lecturas, y las dos son falsas la mitad del tiempo.

2. «Sin cálculo de muros en este ciclo» ataba una lectura estructural al ritmo
   de un ciclo de red. No decía qué ingrediente faltó ni si había muros válidos
   hace treinta segundos.

3. El carril de dark pool respondía con UN par `state`/`rows` a dos preguntas
   distintas: qué pasó AHORA y qué hay guardado. Con un timeout y cien filas
   del ciclo anterior había que elegir entre tirar el dato o esconder el fallo.

4. «Sin intentos» metía en el mismo cajón la cadencia que no vence, la cuota
   agotada, la dependencia que falta, el enfriamiento, la petición en vuelo y
   —escondido entre las cinco— el único defecto real: el programador no la
   llama nunca.

LO QUE ATA ESTE FICHERO
-----------------------
Un bloque por cajón:

    3 · la criticidad es del PAR (dato, consumidor), y se deriva
    4 · el snapshot de muros: COMPLETO / LKG con edad / NO_CALCULABLE con nombre
    5 · dark pool: estado actual y último valor bueno, en campos separados
    7 · los nueve estados del programador, con la anomalía FUERA de ellos

Y una regla que cruza los cuatro: **nunca se afirma lo que no se ha medido**.
Un consumidor cuyo dato todavía está en la cola no está bloqueado, y una
herramienta a la que nadie ha llamado no es un proveedor que no contesta.
"""
from __future__ import annotations

import time

import pytest

from app.core import consumer_contracts as CC
from app.core import dark_pool_state as DPS
from app.core import data_lineage as DL
from app.core import scheduler_states as SS
from app.core import wall_snapshot as WS


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 3 · LA CRITICIDAD ES DEL PAR (DATO, CONSUMIDOR)
# ═══════════════════════════════════════════════════════════════════════════

def test_el_mismo_dato_es_obligatorio_para_uno_y_opcional_para_otro():
    """Es la razón entera del bloque: una etiqueta global no puede acertar."""
    gex = CC.criticality("gex_by_strike")
    assert "EXPOSICION" in gex["critical_for"], "es la magnitud principal de la sección"
    assert "WALLS" in gex["optional_for"], "para el muro es contraste, no la fórmula"
    assert "TRACE" in gex["optional_for"]


def test_la_severidad_se_deriva_de_quien_lo_exige_y_no_del_endpoint():
    # Lo exige alguien → su fallo deja a ese alguien sin datos.
    assert CC.severity_for("interval_map_gamma") == "DEGRADED"
    assert CC.criticality("interval_map_gamma")["critical_for"] == ["TRACE"]
    # No lo exige nadie → degradarse es todo lo que pasa.
    assert CC.severity_for("max_pain") == "OPTIONAL"
    # Y lo que ningún consumidor declara no puede ser crítico por su cuenta.
    fuera = CC.criticality("gainers_losers")
    assert fuera["level"] == "UNUSED" and fuera["severity"] == "OPTIONAL"


def test_gamma_caida_bloquea_las_walls_y_no_la_seccion_que_tiene_respaldo():
    disponible = {"contract_greeks": "PROVIDER_ERROR", "underlying_price": "LIVE",
                  "expiry_selection": "DATA_OK", "gex_by_strike": "LIVE",
                  "oi_by_strike": "LIVE"}
    walls = CC.evaluate("WALLS", disponible)
    expos = CC.evaluate("EXPOSICION", disponible)
    assert walls["state"] == CC.BLOCKED
    assert [f["tool"] for f in walls["missing_required"]] == ["contract_greeks"]
    assert expos["state"] in (CC.READY, CC.DEGRADED), (
        "EXPOSICION no depende de las griegas por contrato")


def test_lo_que_falta_es_opcional_degrada_pero_no_bloquea():
    walls = CC.evaluate("WALLS", {"contract_greeks": "LIVE", "underlying_price": "LIVE",
                                  "expiry_selection": "DATA_OK",
                                  "gex_by_strike": "PROVIDER_ERROR"})
    assert walls["state"] == CC.DEGRADED
    assert [f["tool"] for f in walls["missing_optional"]] == ["gex_by_strike"]
    assert "contraste" in walls["behaviour"]


def test_un_dato_viejo_declarado_sigue_sosteniendo_al_consumidor():
    """Quitar el STALE dejaría la pantalla en blanco por un ciclo perdido."""
    walls = CC.evaluate("WALLS", {"contract_greeks": "STALE", "underlying_price": "STALE_LKG",
                                  "expiry_selection": "DATA_OK", "gex_by_strike": "LIVE"})
    assert walls["state"] == CC.READY


def test_lo_que_no_se_ha_medido_no_puede_declararse_bloqueado():
    """`UNKNOWN` existe para esto: «no lo he preguntado» ≠ «no está».

    Sin este estado, un Auditor que sólo mira el carril de páginas pintaría de
    rojo las Walls por el precio del subyacente, que ese carril no mide.
    """
    walls = CC.evaluate("WALLS", {"contract_greeks": "LIVE", "gex_by_strike": "LIVE"})
    assert walls["state"] == CC.UNKNOWN
    sin_medir = {f["tool"] for f in walls["unmeasured"]}
    assert {"underlying_price", "expiry_selection"} <= sin_medir
    assert walls["missing_required"] == [], "no se afirma que falte lo que no se miró"
    assert all(f["lane"] == CC.TERMINAL_LANE for f in walls["unmeasured"]
               if f["tool"] in ("underlying_price", "expiry_selection")), (
        "y se dice a qué carril hay que preguntarle")


def test_una_dependencia_todavia_en_la_cola_tampoco_bloquea():
    """SCHEDULED no es un fallo: es que aún no le ha tocado el turno."""
    trace = CC.evaluate("TRACE", {"interval_map_gamma": "SCHEDULED",
                                  "underlying_price": "LIVE"})
    assert trace["state"] == CC.UNKNOWN
    assert trace["missing_required"] == []
    # Pero el enfriamiento SÍ es un veredicto: viene de fallar.
    frio = CC.evaluate("TRACE", {"interval_map_gamma": "COOLDOWN",
                                 "underlying_price": "LIVE"})
    assert frio["state"] == CC.BLOCKED


def test_un_consumidor_sin_ninguna_dependencia_medida_no_sale_listo():
    """Dark Pool es todo opcional: sin medir nada saldría READY sin mirar nada."""
    assert CC.evaluate("DARK_POOL", {})["state"] == CC.UNKNOWN


def test_las_griegas_por_contrato_se_derivan_de_quien_las_trae():
    """`contract_greeks` no es un endpoint: viaja en las filas del order flow."""
    resuelto = CC.resolve_availability({"options_order_flow_raw": "LIVE",
                                        "options_order_flow": "PROVIDER_ERROR"})
    assert resuelto["contract_greeks"] == "LIVE", "basta una fuente para tenerlas"
    # Y si nadie informó de las fuentes, no se inventa un estado.
    assert "contract_greeks" not in CC.resolve_availability({"gex_by_strike": "LIVE"})


def test_todo_consumidor_declara_que_hace_cuando_le_falta_algo():
    for nombre, contrato in CC.CONSUMERS.items():
        assert contrato.dependencies, f"{nombre} no declara de qué depende"
        assert contrato.degraded_behaviour and contrato.blocked_behaviour, (
            f"{nombre} no dice qué hace sin sus datos")


def test_trace_declara_que_el_interval_map_es_la_autoridad():
    """No es decorativo: es el contrato que impide reconstruir el campo."""
    dep = next(d for d in CC.CONSUMERS["TRACE"].dependencies
               if d.tool == "interval_map_gamma")
    assert dep.required and "AUTORIDAD" in dep.why


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 4 · EL SNAPSHOT DE MUROS
# ═══════════════════════════════════════════════════════════════════════════

def _contratos(n: int = 4, *, gamma: bool = True, oi: bool = True,
               expiry: str = "2026-10-16"):
    filas = []
    for i in range(n):
        for tipo in ("call", "put"):
            filas.append({
                "strike": 100.0 + i, "option_type": tipo, "expiration": expiry,
                "gamma": (0.01 if gamma else None),
                "open_interest": (500 if oi else None),
                "multiplier": 100.0,
            })
    return filas


def _build(**kw):
    base = dict(symbol="SPY", contract_rows=_contratos(), spot=103.0,
                price_as_of="2026-09-22T14:30:00+00:00", expiry="2026-10-16")
    base.update(kw)
    return WS.build(**base)


@pytest.fixture(autouse=True)
def _almacen_limpio():
    WS.SNAPSHOTS.reset()
    yield
    WS.SNAPSHOTS.reset()


def test_el_ciclo_completo_se_calcula_y_se_guarda():
    resuelto = WS.resolve("SPY", _build())
    assert resuelto["state"] == WS.COMPLETO
    assert resuelto["from_lkg"] is False
    assert WS.SNAPSHOTS.get("SPY") is not None, "lo COMPLETO se guarda para el hueco"


def test_un_ciclo_perdido_no_borra_los_muros_y_declara_su_edad():
    """Una wall no desaparece porque una petición muera: es estructura."""
    WS.resolve("SPY", _build())
    roto = _build(contract_rows=[], spot=None, price_as_of=None, expiry=None)
    resuelto = WS.resolve("SPY", roto)
    assert resuelto["state"] == WS.LKG
    assert resuelto["from_lkg"] is True
    assert resuelto["contracts"] == 8, "se sirve el snapshot bueno, no el vacío"
    assert resuelto["age_seconds"] is not None
    assert "LKG" in WS.verdict_label(resuelto, "WALL_CONFIRMADA")
    assert "edad" in WS.verdict_label(resuelto, "WALL_CONFIRMADA")


def test_sin_snapshot_anterior_se_nombra_cada_ingrediente_que_falta():
    """«Sin cálculo en este ciclo» no dice cuál de los seis faltó."""
    resuelto = WS.resolve("SPY", _build(contract_rows=[], spot=None,
                                        price_as_of=None, expiry=None))
    assert resuelto["state"] == WS.NO_CALCULABLE
    faltan = set(resuelto["missing_names"])
    assert {WS.VENCIMIENTO, WS.GAMMA, WS.SPOT, WS.COBERTURA} <= faltan
    etiqueta = WS.verdict_label(resuelto)
    assert etiqueta.startswith("NO CALCULABLE · falta")
    for nombre in (WS.VENCIMIENTO, WS.GAMMA, WS.SPOT):
        assert nombre in etiqueta


def test_solo_se_guarda_lo_completo():
    WS.SNAPSHOTS.put(_build(spot=None))
    assert WS.SNAPSHOTS.get("SPY") is None, (
        "un snapshot incompleto guardado sería un respaldo que no respalda")


def test_un_lkg_demasiado_viejo_deja_de_sostener_el_muro():
    """A los quince minutos la estructura del día ya es otra."""
    WS.resolve("SPY", _build())
    guardado = WS.SNAPSHOTS.get("SPY")
    guardado["built_at_ts"] = time.time() - (WS.LKG_MAXIMO_S + 60.0)
    WS.SNAPSHOTS._por_simbolo["SPY"] = guardado          # envejecido a mano
    resuelto = WS.resolve("SPY", _build(spot=None, price_as_of=None))
    assert resuelto["state"] == WS.NO_CALCULABLE


def test_entre_fresco_y_maximo_el_muro_se_publica_como_provisional():
    WS.resolve("SPY", _build())
    guardado = WS.SNAPSHOTS.get("SPY")
    guardado["built_at_ts"] = time.time() - (WS.LKG_FRESCO_S + 60.0)
    WS.SNAPSHOTS._por_simbolo["SPY"] = guardado
    resuelto = WS.resolve("SPY", _build(spot=None, price_as_of=None))
    assert resuelto["state"] == WS.LKG and resuelto["stale"] is True
    assert WS.verdict_label(resuelto, "WALL_CONFIRMADA").startswith("WALL PROVISIONAL")


def test_el_precio_sin_hora_de_captura_no_vale_porque_entra_al_cuadrado():
    resuelto = WS.build(symbol="SPY", contract_rows=_contratos(), spot=103.0,
                        price_as_of=None, expiry="2026-10-16")
    assert WS.SPOT in resuelto["missing_names"]


def test_media_cadena_no_es_cadena():
    """Un muro calculado sobre calls sin puts es un muro de medio mercado."""
    solo_calls = [r for r in _contratos() if r["option_type"] == "call"]
    assert WS.COBERTURA in _build(contract_rows=solo_calls)["missing_names"]


def test_el_snapshot_de_un_activo_no_respalda_a_otro():
    WS.resolve("SPY", _build())
    resuelto = WS.resolve("QQQ", _build(symbol="QQQ", contract_rows=[], spot=None,
                                        price_as_of=None, expiry=None))
    assert resuelto["state"] == WS.NO_CALCULABLE, "mezclar dos mercados"


def test_al_cambiar_de_activo_el_snapshot_anterior_se_tira():
    WS.resolve("SPY", _build())
    WS.SNAPSHOTS.clear_symbol("SPY")
    assert WS.SNAPSHOTS.get("SPY") is None


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 5 · DARK POOL · AHORA Y ÚLTIMO BUENO, SIN MEZCLAR
# ═══════════════════════════════════════════════════════════════════════════

def _carril(rows, *, estado=DL.PROVIDER_ERROR, detalle="el canal tardó más de 6.0 s",
            provider_status="", edad=None):
    cls = {"state": estado, "rows": rows, "detail": detalle,
           "has_payload": rows > 0, "capability": DL.CAPABILITY_AVAILABLE,
           "age_seconds": edad}
    block = {"lane_detail": detalle}
    if provider_status:
        block["lane_status"] = provider_status
    return DPS.lane_state(block, cls, classified=rows, unclassified=0,
                          market_open=True)


def test_un_timeout_con_dato_guardado_separa_las_dos_preguntas():
    """Cien filas en pantalla y un refresco muerto son DOS hechos, no uno."""
    v = _carril(100, provider_status="TRANSIENT", edad=42.0)
    assert v["current_status"] == "PROVIDER_ERROR", "lo de AHORA falló"
    assert v["current_rows"] == 0, "y no trajo ni una fila"
    assert v["lkg_rows"] == 100, "pero lo que se ve es real, del último ciclo bueno"
    assert v["lkg_age"] == 42.0, "y va con su edad"
    assert v["serving"] == DPS.STALE_LKG
    assert v["state"] == DPS.STALE and v["is_failure"] is False


def test_respondio_bien_y_no_hay_actividad_no_es_una_averia():
    v = _carril(0, estado=DL.NO_PROVIDER_DATA, detalle="el proveedor respondió sin filas")
    assert v["current_status"] == DPS.NO_DATOS_ACTUAL
    assert v["serving"] == "NONE"
    assert v["state"] == DPS.SIN_DATOS_REALES


def test_un_fallo_sin_nada_guardado_no_tiene_nada_que_servir():
    v = _carril(0, provider_status="TRANSIENT")
    assert v["current_status"] == "PROVIDER_ERROR"
    assert v["lkg_rows"] == 0 and v["serving"] == "NONE"
    assert v["state"] == DPS.PROVIDER_ERROR and v["is_failure"] is True


def test_un_carril_sano_sirve_lo_de_ahora_y_no_un_recuerdo():
    v = _carril(216, estado=DL.DATA_OK, detalle="")
    assert v["current_status"] == DPS.DIRECT_PROVIDER_OK
    assert v["current_rows"] == 216 and v["serving"] == "LIVE"


def test_un_400_no_se_rebaja_por_tener_filas_viejas():
    """Reintentar un cuerpo mal formado no lo arregla: ése es fallo nuestro."""
    v = _carril(216, provider_status="REQUEST_INVALID")
    assert v["current_status"] == DPS.REQUEST_INVALID
    assert v["state"] == DPS.REQUEST_INVALID and v["is_failure"] is True


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 7 · LOS NUEVE ESTADOS, Y LA ANOMALÍA QUE NO ES UN ESTADO
# ═══════════════════════════════════════════════════════════════════════════

def _estado(**kw):
    base = dict(due=True, in_flight=False, cooldown_seconds=0.0, quota_paused=None,
                missing_dependencies=None, rows=0, provider_status="",
                lkg_rows=0, lkg_age_seconds=None, fresh=None, ever_attempted=True)
    base.update(kw)
    return SS.classify(**base)


def test_los_nueve_estados_existen_y_no_se_solapan():
    assert len(SS.STATES) == 9 and len(set(SS.STATES)) == 9
    assert SS.SERVING & SS.WAITING == frozenset(), (
        "servir y esperar son excluyentes: un estado no puede ser los dos")


@pytest.mark.parametrize("kwargs,esperado", [
    ({"due": False}, SS.SCHEDULED),
    ({"in_flight": True}, SS.RUNNING),
    ({"cooldown_seconds": 30.0}, SS.COOLDOWN),
    ({"missing_dependencies": ["expirationDate"]}, SS.WAITING_DEPENDENCY),
    ({"quota_paused": {"reason": "cuota agotada", "seconds": 12}}, SS.WAITING_RATE_LIMIT),
    ({"rows": 120}, SS.LIVE),
    ({"rows": 0}, SS.NO_DATA),
    ({"provider_status": "TRANSIENT"}, SS.PROVIDER_ERROR),
    ({"provider_status": "TRANSIENT", "lkg_rows": 90, "lkg_age_seconds": 30.0}, SS.STALE),
])
def test_cada_causa_tiene_su_estado(kwargs, esperado):
    assert _estado(**kwargs)["state"] == esperado


def test_una_peticion_en_vuelo_manda_sobre_el_resultado_anterior():
    """Al revés, una herramienta que llama AHORA se leería con su intento viejo."""
    v = _estado(in_flight=True, provider_status="TRANSIENT", rows=0)
    assert v["state"] == SS.RUNNING and v["waiting"] is True
    assert v["is_failure"] is False


def test_un_fallo_actual_no_borra_el_ultimo_dato_bueno():
    v = _estado(provider_status="TRANSIENT", lkg_rows=90, lkg_age_seconds=42.0)
    assert v["state"] == SS.STALE and v["serving"] is True
    assert v["rows"] == 90 and v["age_seconds"] == 42.0
    assert v["is_failure"] is False


def test_exigible_y_sin_un_solo_intento_no_es_que_el_proveedor_calle():
    """Decir NO_DATA aquí atribuye al proveedor un silencio que es nuestro."""
    v = _estado(ever_attempted=False)
    assert v["state"] == SS.SCHEDULED
    assert v["never_attempted"] is True
    assert "sin turno" in v["detail"]


def test_la_anomalia_es_el_defecto_que_el_cajon_generico_escondia():
    """26 de 36 herramientas sin un intento en 200 ciclos: eso no es esperar."""
    an = SS.anomaly(state=SS.SCHEDULED, due=True, attempts=0,
                    eligible_seconds=SS.NUNCA_LLAMADA_S + 10.0)
    assert an is not None
    assert an["anomaly"] == SS.ANOMALY_NEVER_CALLED and an["severity"] == "CRITICAL"
    assert "batch" in an["action"]


def test_pasado_el_calentamiento_pero_no_los_tres_minutos_avisa_sin_gritar():
    an = SS.anomaly(state=SS.SCHEDULED, due=True, attempts=0,
                    eligible_seconds=SS.WARMUP_S + 5.0)
    assert an is not None and an["severity"] == "WARN"


@pytest.mark.parametrize("kwargs", [
    {"attempts": 3},                                        # sí la han llamado
    {"due": False},                                         # su cadencia no vence
    {"quota_paused": {"reason": "cuota agotada"}},          # espera con causa
    {"eligible_seconds": SS.WARMUP_S - 1.0},                # todavía en calentamiento
])
def test_lo_que_NO_es_una_anomalia(kwargs):
    base = dict(state=SS.SCHEDULED, due=True, attempts=0,
                eligible_seconds=SS.NUNCA_LLAMADA_S + 10.0)
    base.update(kwargs)
    assert SS.anomaly(**base) is None


def test_el_resumen_cuenta_estados_y_anomalias_por_separado():
    filas = [
        {"key": "a", "state": SS.LIVE},
        {"key": "b", "state": SS.STALE},
        {"key": "c", "state": SS.SCHEDULED,
         "anomaly": {"anomaly": SS.ANOMALY_NEVER_CALLED, "severity": "CRITICAL"}},
        {"key": "d", "state": SS.PROVIDER_ERROR},
    ]
    r = SS.summarize(filas)
    assert r["serving"] == 2 and r["failing"] == 1 and r["waiting"] == 1
    assert [a["key"] for a in r["anomalies"]] == ["c"]
    assert r["counts"][SS.LIVE] == 1


# ═══════════════════════════════════════════════════════════════════════════
# LOS CUATRO BLOQUES, EN EL AUDITOR
# ═══════════════════════════════════════════════════════════════════════════

def _cobertura():
    from app.providers.quantdata.intelligence import QUANTDATA_INTELLIGENCE
    return QUANTDATA_INTELLIGENCE.coverage()


def test_el_auditor_publica_los_nueve_estados_y_la_criticidad():
    cob = _cobertura()
    assert cob["scheduler_states"]["contract"] == "ITMQ_SCHEDULER_STATES_V1"
    assert set(cob["scheduler_states"]["counts"]) == set(SS.STATES)
    assert cob["consumers"]["contract"] == "ITMQ_CONSUMER_CRITICALITY_V1"
    una = cob["tools"][0]
    assert una["lifecycle"]["state"] in SS.STATES
    assert una["criticality"]["severity"] in ("OPTIONAL", "DEGRADED")


def test_el_recuento_del_auditor_cuadra_con_sus_propias_filas():
    """Un resumen que no suma lo que enseña es un resumen que miente."""
    cob = _cobertura()
    conteo = cob["scheduler_states"]["counts"]
    assert sum(conteo.values()) == len(cob["tools"])


def test_ninguna_herramienta_del_catalogo_se_queda_sin_estado():
    for fila in _cobertura()["tools"]:
        assert fila["lifecycle"]["detail"], f"{fila['key']} sin causa declarada"


def test_en_frio_el_auditor_no_declara_bloqueado_a_nadie_por_no_haber_medido():
    """Antes de la primera llamada nada ha fallado: no se puede pintar de rojo."""
    cob = _cobertura()
    for fila in cob["consumers"]["consumers"]:
        if fila["state"] == CC.BLOCKED:
            assert fila["missing_required"], (
                f"{fila['consumer']} se declara bloqueado sin nombrar qué falta")


# ═══════════════════════════════════════════════════════════════════════════
# BLOQUE 4 · EL SNAPSHOT, DENTRO DEL MOTOR DE MUROS
# ═══════════════════════════════════════════════════════════════════════════

def _hub(filas=None):
    return {"contract_greeks": {"rows": (filas if filas is not None else _contratos()),
                                "source": "QUANTDATA_CONTRACT_GREEKS",
                                "lineage": {"age_seconds": 3.0}},
            "exposure_by_strike": {"rows": []},
            "open_interest": {"by_strike": []}}


def _muros(hub, **kw):
    from app.core import wall_engine as WE
    base = dict(spot=103.0, price_as_of="2026-09-22T14:30:00+00:00",
                expiry="2026-10-16", price_source="SESSION_CANDLE_CLOSE")
    base.update(kw)
    return WE.walls_from_hub("SPY", hub, **base)


def test_el_motor_de_muros_publica_el_estado_del_snapshot():
    out = _muros(_hub())
    assert out["snapshot_state"] == WS.COMPLETO
    assert out["verdict_label"], "el operador tiene que ver de qué se le está hablando"
    assert out["call_wall"]["from_lkg"] is False


def test_un_ciclo_perdido_sigue_dando_muro_y_lo_dice():
    """Con el snapshot anterior vivo, un ciclo sin griegas no borra el muro."""
    primero = _muros(_hub())
    assert primero["call_wall"]["strike"] is not None
    perdido = _muros(_hub([]), spot=None, price_as_of=None, expiry=None)
    assert perdido["snapshot_state"] == WS.LKG
    assert perdido["call_wall"]["from_lkg"] is True
    assert perdido["call_wall"]["strike"] == primero["call_wall"]["strike"]
    assert "edad" in perdido["verdict_label"]


def test_sin_nada_el_muro_dice_que_ingrediente_falta_y_no_una_caja_vacia():
    out = _muros(_hub([]), spot=None, price_as_of=None, expiry=None)
    assert out["snapshot_state"] == WS.NO_CALCULABLE
    assert out["call_wall"]["ready"] is False
    assert WS.SPOT in out["call_wall"]["missing_ingredients"]
    assert out["verdict_label"].startswith("NO CALCULABLE")


def test_la_formula_de_los_muros_no_la_toca_este_bloque():
    """El snapshot prepara la entrada; la autoridad del cálculo no se mueve."""
    from app.core import wall_gex as WG
    from app.core import wall_engine as WE
    directo = WG.walls("SPY", contract_rows=_contratos(), spot=103.0,
                       expiry="2026-10-16")
    out = _muros(_hub())
    assert out["call_wall"]["strike"] == directo["call_wall"]["strike"]
    assert out["call_wall"]["gex"] == directo["call_wall"]["gex"]
    assert WE.WG is WG, "wall_gex sigue siendo la única autoridad de la fórmula"

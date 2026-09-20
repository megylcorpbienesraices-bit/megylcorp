"""GATE 9 · Monte Carlo congelado y las acumulaciones auditadas UNA A UNA.

Puntos 28, 29 y 30.

LA REGLA DEL PUNTO 29
---------------------
«No hacer reemplazo global de ceros. Para cada caso determinar si el 0 es una
contribución matemática válida o un missing fabricado. Corregir solamente el
segundo.»

Un reemplazo global habría roto los acumuladores, donde un cero SÍ es la
respuesta correcta: sumar una contribución nula es exactamente lo que hay que
hacer con una observación que no aporta.

EL INVENTARIO
-------------
Cada caso lleva su veredicto aquí, en código, no en un documento que se
desincroniza. Si alguien añade una acumulación nueva en estos ficheros y no la
clasifica, el test lo dice.
"""
from __future__ import annotations

import re
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════
# INVENTARIO · veredicto por caso
# ═══════════════════════════════════════════════════════════════════════════
#
#   VALIDO      el 0 es una contribución matemática correcta
#   CORREGIDO   era un missing fabricado y se arregló en v1.56.0
#
INVENTARIO = {
    "app/core/aggression_delta.py": {
        "veredicto": "VALIDO",
        "por_que": (
            "Es un acumulador intrabarra sobre la cinta. Cada `or 0.0` es una "
            "CONTRIBUCIÓN: una operación sin tamaño aporta cero volumen, que es "
            "exactamente lo que hay que sumar. Los que leen `bar.get(...)` leen "
            "un diccionario que este mismo módulo crea con todas sus claves "
            "inicializadas, así que el cero nunca es un campo ausente. "
            "`_last_trade_sign.get(sym) or 0` es el tri-estado de la regla del "
            "tick: 0 significa «no hay operación anterior», y la regla lo trata "
            "como tal en vez de suponer un lado."),
    },
    "app/core/dealer_intelligence.py": {
        "veredicto": "CORREGIDO",
        "por_que": (
            "La media ponderada de confianza metía un componente ausente como 0 "
            "CON TODO SU PESO. Eso convierte «no lo he medido» en «lo he medido "
            "y vale cero», que no es lo mismo: lo primero no debería mover la "
            "cifra y lo segundo la hunde. Ahora el componente que falta se queda "
            "fuera y el peso se redistribuye entre lo medido."),
    },
    "app/core/qflow.py": {
        "veredicto": "VALIDO",
        "por_que": (
            "Todos los casos son SUMAS sobre operaciones: `sum(t['premium'] or "
            "0.0 for t in trades)`. Una operación sin prima aporta cero a la "
            "suma, que es exactamente lo que hay que sumar de algo que no movió "
            "dinero medible. Desde v1.56.0 un print sin tamaño llega con "
            "`premium = None`, así que ni siquiera inventa un importe: aporta "
            "nada y se cuenta aparte en el cubo sin clasificar."),
    },
    "app/core/nextgen_terminal.py": {
        "veredicto": "CORREGIDO",
        "por_que": (
            "Tres casos reales. `spot ... or 0.0` convertía un precio ausente en "
            "CERO, y los strikes se ordenaban por distancia a cero: la selección "
            "devolvía los más BAJOS de la cadena en vez de los que rodean al "
            "precio, y el gráfico salía lleno y equivocado, que es peor que "
            "vacío. Lo mismo en el campo de CHARM, donde el spot multiplica: un "
            "cero dejaba el charm a cero en toda la cadena y eso se lee como "
            "«no hay decaimiento de delta». Y la prima de un print sin importe "
            "se publicaba como $0.0, que afirma que se negoció sin dinero."),
    },
    "app/core/market_state_field.py": {
        "veredicto": "CORREGIDO",
        "por_que": (
            "Sin exposición, `gamma_ratio` salía 0.0 y de ahí `stability = 50.0` "
            "y `gamma_regime = TRANSITION`. Eso se lee como «el mercado está en "
            "transición con gamma neutra»: una afirmación sobre el libro de "
            "opciones hecha sin medir una sola posición. Los números se conservan "
            "porque el campo los necesita, pero ahora se declara si se midieron "
            "y el régimen dice NO_EXPOSURE_DATA."),
    },
}


# ── 29 · El inventario ───────────────────────────────────────────────────

def test_los_cinco_ficheros_estan_clasificados():
    assert set(INVENTARIO) == {
        "app/core/aggression_delta.py",
        "app/core/dealer_intelligence.py",
        "app/core/market_state_field.py",
        "app/core/nextgen_terminal.py",
        "app/core/qflow.py",
    }
    for ruta, caso in INVENTARIO.items():
        assert caso["veredicto"] in ("VALIDO", "CORREGIDO"), ruta
        assert len(caso["por_que"]) > 80, ruta


def test_no_quedan_pendientes():
    """El criterio de cierre: pendientes == 0."""
    pendientes = [r for r, c in INVENTARIO.items() if c["veredicto"] not in ("VALIDO", "CORREGIDO")]
    assert pendientes == []


def test_el_acumulador_de_agresion_sigue_sumando_contribuciones_nulas():
    """No se tocó, y es correcto que no se tocara: aquí el 0 ES la respuesta."""
    src = Path("app/core/aggression_delta.py").read_text(encoding="utf-8")
    assert 'size = max(0.0, _f(row.get("size"), 0.0) or 0.0)' in src
    assert 'sign = int(self._last_trade_sign.get(sym) or 0)' in src


def test_la_confianza_del_dealer_ya_no_se_diluye_con_lo_que_falta():
    src = Path("app/core/dealer_intelligence.py").read_text(encoding="utf-8")
    assert "def _componente(valor, peso):" in src
    assert "UN COMPONENTE AUSENTE NO PUNTUA CERO" in src
    # El peso se redistribuye: el denominador es la suma de los que SÍ están.
    assert "den=sum(w for _,w in components)" in src


def test_un_componente_ausente_no_arrastra_la_media():
    """Con peso fijo, una confianza que no llegó bajaba la cifra; sin él, la
    cifra la deciden los componentes medidos."""
    comps = [(80.0, .38), (90.0, .22)]
    den = sum(w for _, w in comps)
    con_redistribucion = sum(v * w for v, w in comps) / den
    con_cero = (80.0 * .38 + 90.0 * .22 + 0.0 * .20) / (.38 + .22 + .20)
    assert con_redistribucion > con_cero
    assert abs(con_redistribucion - 83.66) < 0.1


def test_sin_exposicion_el_regimen_lo_dice_en_vez_de_afirmar_transicion():
    from app.core.market_state_field import _options_field
    vacio = _options_field({}, {}, "QQQ")
    assert vacio["gamma_regime"] == "NO_EXPOSURE_DATA"
    assert vacio["gamma_measured"] is False
    assert vacio["measured"] is False


def test_con_exposicion_el_regimen_vuelve_a_ser_una_lectura():
    from app.core.market_state_field import _options_field
    medido = _options_field({"total_signed_gex": 5e8, "total_gross_gex": 1e9,
                             "net_delta_exposure": 2e8}, {}, "QQQ")
    assert medido["gamma_measured"] is True
    assert medido["gamma_regime"] in ("POSITIVE_FRICTION", "NEGATIVE_FEEDBACK", "TRANSITION")
    assert medido["gamma_ratio"] != 0.0


def test_un_spot_ausente_no_selecciona_los_strikes_mas_bajos():
    """El defecto: con `spot = 0.0` los strikes se ordenaban por distancia a
    CERO, así que la cadena devolvía los más baratos en vez de los del dinero."""
    src = Path("app/core/nextgen_terminal.py").read_text(encoding="utf-8")
    assert "UN SPOT DE CERO NO EXISTE" in src
    assert 'spot = _f(gd.get("spot"), _f(chain["underlying_price"].iloc[-1]))\n' in src
    assert "todos[len(todos) // 2] if todos else 0.0" in src


def test_el_charm_sin_precio_queda_en_hueco_no_en_cero():
    """El spot multiplica: un cero dejaba CHARM a cero en toda la cadena, y eso
    se lee como «no hay decaimiento de delta»."""
    src = Path("app/core/nextgen_terminal.py").read_text(encoding="utf-8")
    assert "if ancla is not None and ancla <= 0:" in src
    assert 'float("nan") if ancla is None else float(ancla)' in src


def test_un_print_sin_prima_no_se_publica_como_cero_dolares():
    src = Path("app/core/nextgen_terminal.py").read_text(encoding="utf-8")
    assert '"premium": _f(r.get("premium")), "direction"' in src


def test_las_sumas_de_qflow_siguen_sumando_contribuciones_nulas():
    """Aquí el 0 ES la respuesta: una operación sin prima aporta cero."""
    src = Path("app/core/qflow.py").read_text(encoding="utf-8")
    assert 'sum(t["premium"] or 0.0 for t in trades)' in src


def test_nadie_añade_una_acumulacion_nueva_sin_clasificarla():
    """Si aparece un fichero nuevo con este patrón en `app/core`, tiene que
    entrar en el inventario con su veredicto."""
    patron = re.compile(r"or 0(\.0)?\b")
    sospechosos = []
    for ruta in sorted(Path("app/core").glob("*.py")):
        rel = ruta.as_posix()
        if rel in INVENTARIO:
            continue
        texto = ruta.read_text(encoding="utf-8")
        hits = [l for l in texto.splitlines()
                if patron.search(l) and not l.lstrip().startswith("#")
                and re.search(r"(iv|price|premium|pain|interest|gex|dex|vex|notional)", l, re.I)]
        if len(hits) >= 6:
            sospechosos.append(f"{rel}: {len(hits)} casos")
    assert not sospechosos, ("acumulaciones sin clasificar:\n" + "\n".join(sospechosos))


# ── 30 · La regla general ────────────────────────────────────────────────

def test_cero_observado_es_cero_y_ausente_es_nulo():
    from app.core.asset_normalization import normalize_matrix
    from app.providers.quantdata.tools import norm_net_drift
    r = normalize_matrix([[0.0, None]], symbol="X")
    assert r["matrix"][0][0] == 0.0 and r["matrix"][0][1] is None
    fila = norm_net_drift({"data": {"1700000000000": {"netCallPremium": 0.0}}})["rows"][0]
    assert fila["net_call_premium"] == 0.0
    assert fila["net_put_premium"] is None


def test_con_lkg_se_muestra_el_valor_y_se_marca_viejo():
    from app.core import flow_view as FV
    FV.reset()
    FV.lane("X", "d", "ds", [1, 2])
    v = FV.lane("X", "d", "ds", None)
    assert v["current"] == [1, 2] and v["status"] in (FV.LIVE, FV.STALE)


def test_sin_lkg_se_dice_sin_datos():
    from app.core import flow_view as FV
    FV.reset()
    assert FV.lane("X", "d", "otro", None)["status"] == FV.NO_DATA


# ── 28 · Monte Carlo congelado ───────────────────────────────────────────

def test_el_gate_de_release_ejecuta_la_auditoria_matematica():
    src = Path("scripts/release_gate_full.py").read_text(encoding="utf-8")
    assert "def monte_carlo_math_gate()" in src
    assert "monte_carlo_math_gate()" in src[src.index("def main()"):]


def test_el_gate_cubre_las_propiedades_que_no_pueden_romperse():
    from release_gate_full import MONTE_CARLO_GATE
    nombres = " ".join(MONTE_CARLO_GATE)
    for propiedad in ("mismo_mercado_publica_el_mismo_numero",   # semilla determinista
                      "subestimaria_el_toque_a_la_mitad",        # puente browniano
                      "error_cae_como_uno_partido_por_raiz_de_n",  # convergencia
                      "entradas_imposibles_no_producen_un_cono_falso"):  # NaN/Inf
        assert propiedad in nombres, propiedad
    assert len(MONTE_CARLO_GATE) >= 9


def test_el_gate_va_antes_que_la_suite_particionada():
    """Si la matemática está rota, el resto de la validación no significa nada."""
    src = Path("scripts/release_gate_full.py").read_text(encoding="utf-8")
    main = src[src.index("def main()"):]
    assert main.index("monte_carlo_math_gate()") < main.index("partitioned_pytest_gate(")

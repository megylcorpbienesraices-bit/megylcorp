"""v1.55.0 · AUSENTE NO ES CERO.

Un campo que el proveedor no publica y un campo que vale cero son dos hechos
distintos sobre el mercado, y el codigo los estaba colapsando en el mismo
numero con `_f(x, 0.0) or 0.0`.

La diferencia importa en operativa: una prima neta de 0 $ dice «este minuto no
entro dinero», que es una afirmacion; un hueco dice «no se sabe». En pantalla
el cero se dibuja como una barra al ras del eje y como una curva que se desploma
— exactamente la sierra que ya aparecio en la deriva de volatilidad.

Estas pruebas fijan la regla en los tres puntos de la cadena:

    NORMALIZADOR  el campo ausente sale `None`, el cero medido sale `0.0`
    MOTOR         el acumulado ignora el hueco sin estrenarse en cero
    CERTIFICADOR  ausencia contra numero NO se certifica
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.net_drift import build_net_drift, certify_against_raw
from app.providers.quantdata.tools import norm_levels, norm_net_drift

NOW = datetime(2023, 11, 14, 22, 20, 0, tzinfo=timezone.utc)


def _payload():
    return {"data": {
        "1700000000000": {"netCallPremium": 1000.0, "netPutPremium": -400.0,
                          "netCallVolume": 10, "netPutVolume": 4, "stockPrice": 500.0},
        # El proveedor NO publica el lado call en este bucket.
        "1700000060000": {"netPutPremium": -200.0, "stockPrice": 501.0},
        # Aqui si lo publica, y vale cero de verdad.
        "1700000120000": {"netCallPremium": 0.0, "netPutPremium": 0.0,
                          "netCallVolume": 0, "netPutVolume": 0, "stockPrice": 501.5},
    }}


# ── Normalizador ─────────────────────────────────────────────────────────

def test_campo_ausente_sale_none_no_cero():
    rows = norm_net_drift(_payload())["rows"]
    assert rows[1]["net_call_premium"] is None
    assert rows[1]["net_call_volume"] is None


def test_cero_medido_se_conserva_como_cero():
    """La correccion no puede convertir un cero real en un hueco."""
    rows = norm_net_drift(_payload())["rows"]
    assert rows[2]["net_call_premium"] == 0.0
    assert rows[2]["net_put_premium"] == 0.0
    assert rows[2]["net_premium"] == 0.0


def test_neto_del_intervalo_existe_si_un_lado_se_midio():
    rows = norm_net_drift(_payload())["rows"]
    # call ausente, put medido -> el neto es el put, no `None` ni `0`.
    assert rows[1]["net_premium"] == -200.0


def test_neto_es_none_cuando_faltan_los_dos_lados():
    rows = norm_net_drift({"data": {"1700000000000": {"stockPrice": 500.0}}})["rows"]
    assert rows[0]["net_premium"] is None


# ── Motor ────────────────────────────────────────────────────────────────

def test_el_hueco_no_hunde_el_acumulado():
    """El acumulado es una suma corrida: un bucket sin dato no resta nada.

    Con el defecto anterior el hueco entraba como 0.0 y la curva se quedaba
    plana en un sitio donde en realidad no se habia medido.
    """
    rows = norm_net_drift(_payload())["rows"]
    built = build_net_drift(rows, symbol="QQQ", now=NOW)
    s = built["series"]
    assert s[1]["call"] is None          # el bucket se dibuja como hueco
    assert s[1]["cum_call"] == 1000.0    # el acumulado conserva lo ya medido


def test_el_motor_declara_cuantos_buckets_llegaron_sin_cada_campo():
    rows = norm_net_drift(_payload())["rows"]
    built = build_net_drift(rows, symbol="QQQ", now=NOW)
    assert built["missing_by_field"]["call"] == 1
    assert built["missing_by_field"]["put"] == 0


def test_acumulado_sin_ningun_dato_medido_es_none_no_cero():
    """Una sesion en la que el proveedor nunca publico prima de calls NO dice
    «0 $ acumulados»: eso seria afirmar que el dinero no se movio."""
    rows = norm_net_drift({"data": {
        "1700000000000": {"netPutPremium": -100.0, "stockPrice": 500.0},
        "1700000060000": {"netPutPremium": -50.0, "stockPrice": 500.5},
    }})["rows"]
    built = build_net_drift(rows, symbol="SPY", now=NOW)
    assert built["cum_call_premium"] is None
    assert built["cum_put_premium"] == -150.0
    assert built["cum_net_premium"] == -150.0


# ── Certificador ─────────────────────────────────────────────────────────

def test_la_curva_se_certifica_valor_a_valor_contra_el_crudo():
    rows = norm_net_drift(_payload())["rows"]
    built = build_net_drift(rows, symbol="QQQ", now=NOW)
    cert = certify_against_raw(rows, built)
    assert cert["ok"] is True
    assert cert["points"] == len(built["series"])


def test_el_certificador_rechaza_un_cero_inventado():
    """Si la publicacion pusiera un numero donde el crudo no tiene dato, la
    certificacion tiene que FALLAR. Es la prueba de que el certificador mide la
    publicacion y no su propia aritmetica."""
    rows = norm_net_drift({"data": {
        "1700000000000": {"netPutPremium": -100.0, "stockPrice": 500.0},
    }})["rows"]
    built = build_net_drift(rows, symbol="SPY", now=NOW)
    assert built["series"][0]["cum_call"] is None
    built["series"][0]["cum_call"] = 0.0          # el defecto, inyectado a mano
    cert = certify_against_raw(rows, built)
    assert cert["ok"] is False
    assert "SIN DATO" in cert["reason"]


# ── Dark Pool · niveles ──────────────────────────────────────────────────

def test_nivel_sin_nocional_no_vale_cero_dolares():
    rows = norm_levels({"data": {
        "500.0": {"notionalValue": 1_000_000.0, "size": 100, "tradeCount": 3},
        "501.0": {"size": 50},
    }})["rows"]
    by_price = {r["price"]: r for r in rows}
    assert by_price[501.0]["notional"] is None
    assert by_price[501.0]["prints"] is None
    assert by_price[500.0]["notional"] == 1_000_000.0


def test_los_niveles_sin_nocional_van_al_final_no_entre_los_pequenos():
    rows = norm_levels({"data": {
        "500.0": {"notionalValue": 10.0},
        "501.0": {"size": 50},               # sin nocional
        "502.0": {"notionalValue": 1_000.0},
    }})["rows"]
    assert [r["price"] for r in rows] == [502.0, 500.0, 501.0]


# ── Guardia de regresion sobre el codigo fuente ──────────────────────────

def test_los_normalizadores_oficiales_no_reintroducen_el_patron():
    """`_f(campo, 0.0) or 0.0` en una magnitud de mercado es el defecto.

    Se vigila el fichero de normalizadores porque es la frontera: lo que salga
    de ahi con un cero inventado ya no hay forma de distinguirlo aguas abajo.
    """
    import re
    from pathlib import Path
    src = Path("app/providers/quantdata/tools.py").read_text(encoding="utf-8")
    campos = ("Premium", "Volume", "notionalValue", "stockPrice", "openInterest",
              "impliedVolatility", "maxPain")
    ofensas = []
    for line in src.splitlines():
        if ", 0.0) or 0.0" not in line and ", 0) or 0" not in line:
            continue
        if any(c in line for c in campos):
            ofensas.append(line.strip())
    assert not ofensas, "cero fabricado en una magnitud de mercado:\n" + "\n".join(ofensas)

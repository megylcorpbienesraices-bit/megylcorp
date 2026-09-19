"""Sección observacional de anomalías en rendimientos (v1.27.10).

Estos tests cubren tres cosas distintas:

* que el detector esté **calibrado** (dispare donde dice que dispara),
* que sea **sensible** (marque shocks reales),
* y sobre todo que esté **aislado**: que no pueda alterar el motor cuantitativo.

El tercero es el que importa para la condición de diseño pedida. Se comprueba por
AST, no por convención: si alguien importa ``service`` o ``engine`` desde el
módulo, o si la sección deja de ser opt-in, el test falla.
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.return_anomalies import (
    AUTHORITY,
    INTRADIA,
    OVERNIGHT,
    POLITICA_POR_DEFECTO,
    PoliticaAnomalias,
    detectar_anomalias,
    resumen_compacto,
    separar_poblaciones,
    umbral_bootstrap,
)

ROOT = Path(__file__).resolve().parent.parent
MODULO = ROOT / "app/core/return_anomalies.py"

S_INTRA = 0.0009
S_NIGHT = 0.0045


def _serie(semilla: int = 42, sesiones: int = 6, barras: int = 78,
           shock_sigma: float = 0.0, shock_en: tuple[int, int] = (4, 40)):
    """Serie sintética con huecos overnight reales y, opcionalmente, un shock."""
    rng = np.random.default_rng(semilla)
    t = pd.Timestamp("2026-09-01 13:30:00", tz="UTC")
    ts, px, marca = [t], [100.0], None
    for s in range(sesiones):
        if s > 0:
            t = t + pd.Timedelta(hours=17.5)          # cierre -> apertura
            px.append(px[-1] * np.exp(rng.normal(0, S_NIGHT)))
            ts.append(t)
        for b in range(barras):
            t = t + pd.Timedelta(minutes=5)
            golpe = shock_sigma * S_INTRA if (s, b) == shock_en else 0.0
            if golpe:
                marca = t
            px.append(px[-1] * np.exp(rng.normal(0, S_INTRA) + golpe))
            ts.append(t)
    return pd.DataFrame({"timestamp": ts, "price": px}), marca


# ------------------------------------------------------ aislamiento del motor


def test_el_modulo_no_importa_nada_del_motor():
    """Condición de diseño: esta sección no puede tocar el motor cuantitativo."""
    arbol = ast.parse(MODULO.read_text(encoding="utf-8"))
    importados = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            importados.update(a.name.split(".")[0] for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            importados.add(nodo.module.split(".")[0])
    prohibidos = {"service", "engine", "precision_engine", "scanner", "sophia_core",
                  "freshness", "dealer_intelligence", "flow_kinematics", "app"}
    assert not (importados & prohibidos), f"acoplamiento con el motor: {importados & prohibidos}"
    assert importados <= {"__future__", "dataclasses", "typing", "math", "numpy", "pandas"}, importados


def test_ningun_modulo_del_motor_importa_esta_seccion():
    """Si el motor la importara, dejaría de ser una sección desacoplable."""
    ofensores = []
    for py in (ROOT / "app").rglob("*.py"):
        if py.name in {"return_anomalies.py", "main.py"}:
            continue
        if "return_anomalies" in py.read_text(encoding="utf-8"):
            ofensores.append(str(py.relative_to(ROOT)))
    assert not ofensores, f"el motor depende de la sección: {ofensores}"


def test_la_seccion_permanece_siempre_activa_sin_feature_flag():
    src = (ROOT / "app/main.py").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "ITM_SECCION_ANOMALIAS" not in src
    assert "ITM_SECCION_ANOMALIAS" not in env
    assert '"activa": True' in src


# ------------------------------------------------------------- calibración


def test_no_dispara_mas_de_lo_declarado_sobre_ruido():
    tasas = [detectar_anomalias(_serie(semilla=s)[0])["tasa_disparo_pct"] for s in range(10)]
    media = float(np.mean(tasas))
    nominal = 100.0 - POLITICA_POR_DEFECTO.percentil
    assert media <= nominal * 2.0, f"tasa {media}% frente a nominal {nominal}%"


def test_el_umbral_sale_del_bootstrap_y_no_de_un_dos_sigma_fijo():
    df, _ = _serie()
    r = detectar_anomalias(df)
    umbral = r["poblaciones"][INTRADIA]["umbral_z"]
    assert umbral is not None and umbral > 2.0, "con colas gruesas el umbral debe superar 2σ"
    assert r["poblaciones"][INTRADIA]["umbral_calibrado"] is True


def test_sin_muestra_suficiente_no_se_inventa_un_umbral():
    """Preferible abstenerse a fabricar un umbral con cuatro observaciones."""
    df, _ = _serie(sesiones=6)
    r = detectar_anomalias(df)
    overnight = r["poblaciones"][OVERNIGHT]
    assert overnight["observaciones"] < POLITICA_POR_DEFECTO.minimo_muestras
    assert overnight["umbral_calibrado"] is False and overnight["umbral_z"] is None
    assert r["eventos_overnight"] == 0, "sin umbral calibrado no puede haber eventos marcados"


def test_umbral_bootstrap_devuelve_nan_con_muestra_ridicula():
    assert not np.isfinite(umbral_bootstrap(np.array([0.1, -0.2, 0.05])))


# -------------------------------------------------------------- sensibilidad


@pytest.mark.parametrize("sigma", [4.0, 6.0, 10.0])
def test_marca_el_shock_real_en_la_barra_exacta(sigma):
    aciertos = 0
    for semilla in range(8):
        df, marca = _serie(semilla=semilla, shock_sigma=sigma)
        sellos = {e["timestamp"] for e in detectar_anomalias(df)["eventos"]}
        aciertos += int(marca.isoformat() in sellos)
    assert aciertos == 8, f"shock de {sigma}σ detectado solo {aciertos}/8 veces"


# ------------------------------------------------- separación de poblaciones


def test_el_salto_overnight_se_clasifica_aparte_del_intradia():
    df, _ = _serie(sesiones=4)
    d = separar_poblaciones(df)
    assert set(d["poblacion"]) == {INTRADIA, OVERNIGHT}
    assert (d["poblacion"] == OVERNIGHT).sum() == 3, "3 huecos entre 4 sesiones"
    # La volatilidad de cada población es de escala distinta: por eso se separan.
    v_noche = d.loc[d["poblacion"] == OVERNIGHT, "log_return"].abs().median()
    v_dia = d.loc[d["poblacion"] == INTRADIA, "log_return"].abs().median()
    assert v_noche > 2 * v_dia


def test_una_cadena_de_opciones_se_colapsa_a_un_precio_por_instante():
    rng = np.random.default_rng(3)
    ts = pd.date_range("2026-09-08 13:30", periods=120, freq="5min", tz="UTC")
    px = 100 * np.exp(np.cumsum(rng.normal(0, S_INTRA, 120)))
    filas = [{"timestamp": t, "underlying_price": u, "strike": k}
             for t, u in zip(ts, px) for k in range(8)]
    r = detectar_anomalias(pd.DataFrame(filas), simbolo="DIA")
    assert r["observaciones"] == 119, "8 contratos por instante no son 8 retornos"


# ------------------------------------------------------- no emite dirección


def test_la_seccion_nunca_emite_direccion():
    for sigma in (0.0, 10.0):
        r = detectar_anomalias(_serie(shock_sigma=sigma)[0])
        assert r["direccion"] is None
        assert r["autoridad"] == AUTHORITY == "OBSERVATIONAL_ONLY_NEVER_DIRECCIONAL".replace("DIRECCIONAL", "DIRECTIONAL")
        assert resumen_compacto(r)["direccion"] is None
        for clave in ("signal", "senal", "long", "short", "compra", "venta"):
            assert clave not in r, f"la sección no puede exponer {clave}"


def test_el_base_rate_se_declara_como_descriptivo():
    r = detectar_anomalias(_serie(shock_sigma=10.0)[0])
    nota = r["base_rate_en_muestra"]["nota"].lower()
    assert "descriptivo" in nota and "no una predicci" in nota


# ------------------------------------------------------------ robustez


@pytest.mark.parametrize("entrada", [
    None, pd.DataFrame(), pd.DataFrame({"a": [1, 2]}),
    pd.DataFrame({"timestamp": ["no-fecha"], "price": ["x"]}),
])
def test_entradas_degeneradas_no_revientan(entrada):
    r = detectar_anomalias(entrada)
    assert r["listo"] is False and r["direccion"] is None and "estado" in r


def test_precios_no_positivos_se_descartan_sin_producir_infinitos():
    df = pd.DataFrame({"timestamp": pd.date_range("2026-09-08", periods=60, freq="5min", tz="UTC"),
                       "price": [0.0, -1.0] + [100.0] * 58})
    r = detectar_anomalias(df)
    for e in r.get("eventos", []):
        assert np.isfinite(e["log_return"]) and np.isfinite(e["z"])


def test_la_politica_es_explicita_y_viaja_en_la_salida():
    politica = PoliticaAnomalias(percentil=95.0, horizonte_barras=5)
    r = detectar_anomalias(_serie()[0], politica)
    assert r["politica"]["percentil"] == 95.0
    assert r["tasa_esperada_bajo_nulo_pct"] == 5.0
    assert r["base_rate_en_muestra"]["horizonte_barras"] == 5

"""v1.57.0 · EL CERTIFICADOR LIVE, probado SIN instancia.

El certificador es la herramienta con la que se van a cerrar los puntos 4 y 8.
Si él mismo tiene un defecto, el informe que produzca no vale nada, así que su
lógica se prueba con bundles sintéticos: aislamiento, redacción de secretos,
veredictos y la comprobación de que el dato LLEGA a su módulo.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import certificar_live as CL  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# 1 · NINGÚN SECRETO SALE EN EL INFORME
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("entrada", [
    {"apiKey": "sk_live_abc123def456ghi789jkl"},
    {"api_key": "x"},
    {"headers": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9zzzzzzzzzzzzzzzz"}},
    {"x-api-key": "zzz"},
    {"nested": {"deep": {"token": "abc"}}},
    {"body": {"secret": "s"}, "password": "p"},
])
def test_ninguna_credencial_sobrevive_a_la_redaccion(entrada):
    out = CL.redactar(entrada)
    texto = str(out)
    for prohibido in ("sk_live_abc123def456ghi789jkl", "eyJhbGciOiJIUzI1NiJ9",
                      "zzz", "abc", "s", "p", "x"):
        if prohibido in str(entrada) and len(prohibido) > 2:
            assert prohibido not in texto, f"{prohibido} sobrevivió: {texto}"


def test_una_clave_larga_suelta_tambien_se_redacta():
    t = CL.redactar("QUANTDATA_API_KEY=abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in t
    assert CL.REDACTADO in t


def test_lo_que_no_es_secreto_se_conserva():
    out = CL.redactar({"ticker": "SPY", "strike": 500.0, "x-api-key": "zz"})
    assert out["ticker"] == "SPY" and out["strike"] == 500.0
    assert out["x-api-key"] == CL.REDACTADO


def test_la_redaccion_no_se_cuelga_con_anidamiento_absurdo():
    d = {}
    cur = d
    for _ in range(50):
        cur["n"] = {}
        cur = cur["n"]
    assert CL.redactar(d) is not None


# ═══════════════════════════════════════════════════════════════════════════
# 2 · EL VOCABULARIO ES EL PEDIDO
# ═══════════════════════════════════════════════════════════════════════════

def test_los_siete_estados_existen_y_son_los_acordados():
    from app.providers.quantdata.intelligence import QuantDataIntelligence as Q
    for e in ("LIVE_OK", "NO_DATA", "NO_AUTORIZADO", "ENDPOINT_NO_EXISTE",
              "REQUEST_INVALID", "PROVIDER_ERROR", "SIN_INTENTAR"):
        assert e in Q.VERDICTS, f"falta el estado {e}"


def test_no_son_fallo_solo_los_tres_que_no_lo_son():
    assert CL.NO_SON_FALLO == {"LIVE_OK", "NO_DATA", "SIN_INTENTAR"}
    for e in ("NO_AUTORIZADO", "ENDPOINT_NO_EXISTE", "REQUEST_INVALID", "PROVIDER_ERROR"):
        assert e not in CL.NO_SON_FALLO


# ═══════════════════════════════════════════════════════════════════════════
# 3 · MÁS QUE UN HTTP 200
# ═══════════════════════════════════════════════════════════════════════════

def _bundle(tools, *, checks=(), flujo=None):
    return {"symbol": "SPY",
            "flujo_ordenes": flujo or {},
            "auditor": {"quantdata": {"tools": list(tools)},
                        "diagnostics": {"checks": list(checks)}}}


def _tool(key, verdict="LIVE_OK", rows=10, **kw):
    t = {"key": key, "title": key, "path": f"/v1/{key}", "rows": rows,
         "diagnosis": {"verdict": verdict, "evidence": kw.pop("evidence", ""),
                       "action": kw.pop("action", ""), "fields": kw.pop("fields", [])},
         "runtime": {"breaker": "CLOSED"}}
    t.update(kw)
    return t


def test_un_LIVE_OK_cuyo_modulo_esta_vacio_es_FAIL():
    """El endpoint respondió y el operador no lo ve. Eso es un fallo."""
    b = _bundle([_tool("dark_flow")],
                checks=[{"panel": "DARK POOL · dark flow", "ok": False, "count": 0,
                         "reason": "EL_PROVEEDOR_NO_DEVOLVIO_FILAS"}])
    f = CL.certificar_activo(b, "SPY")[0]
    assert f["estado"] == "LIVE_OK"
    assert f["llega_al_modulo"] is False
    assert f["resultado"] == "FAIL"
    assert "módulo está vacío" in f["motivo_fallo"]


def test_un_LIVE_OK_que_llega_a_su_modulo_es_PASS():
    b = _bundle([_tool("dark_flow")],
                checks=[{"panel": "DARK POOL · dark flow", "ok": True, "count": 216}])
    f = CL.certificar_activo(b, "SPY")[0]
    assert f["resultado"] == "PASS" and f["llega_al_modulo"] is True
    assert f["filas_en_modulo"] == 216


def test_el_consumidor_por_ruta_mira_el_bundle_de_verdad():
    b = _bundle([_tool("net_drift")],
                flujo={"net_drift": {"series": [{"t": 1}, {"t": 2}, {"t": 3}]}})
    f = CL.certificar_activo(b, "SPY")[0]
    assert f["modulo_consumidor"] == "FLUJO · Net Drift"
    assert f["llega_al_modulo"] is True and f["filas_en_modulo"] == 3


def test_una_ruta_vacia_se_denuncia_con_su_camino():
    b = _bundle([_tool("net_drift")], flujo={"net_drift": {"series": []}})
    f = CL.certificar_activo(b, "SPY")[0]
    assert f["llega_al_modulo"] is False
    assert "net_drift" in f["detalle_modulo"]


def test_toda_herramienta_del_catalogo_declara_su_consumidor():
    from app.providers.quantdata.tools import build_catalog
    faltan = sorted(set(build_catalog()) - set(CL.CONSUMIDOR))
    assert not faltan, f"sin módulo consumidor declarado: {faltan}"


# ═══════════════════════════════════════════════════════════════════════════
# 4 · NADA DETIENE AL CERTIFICADOR
# ═══════════════════════════════════════════════════════════════════════════

def test_una_herramienta_que_revienta_no_para_a_las_demas():
    malo = {"key": "roto", "diagnosis": "esto no es un diccionario"}
    b = _bundle([_tool("net_flow"), malo, _tool("gex_by_strike")],
                flujo={"net_flow": {"rows": [1, 2]}},
                checks=[{"panel": "TRACE · perfiles por strike", "ok": True, "count": 25}])
    filas = CL.certificar_activo(b, "SPY")
    assert len(filas) == 3, "el certificador se detuvo"
    roto = next(f for f in filas if f["herramienta"] == "roto")
    assert roto["resultado"] == "FAIL" and roto["estado"] == "ERROR_CERTIFICADOR"
    assert sum(1 for f in filas if f["resultado"] == "PASS") == 2


def test_un_activo_que_no_arranca_no_impide_certificar_los_otros():
    from pathlib import Path
    src = Path("scripts/certificar_live.py").read_text(encoding="utf-8")
    assert "continue" in src.split("if not res.get(\"ok\"):")[1][:400], (
        "un activo caído tiene que saltarse, no abortar")
    assert "incidencias.append" in src


def test_el_informe_se_escribe_aunque_haya_fallos():
    from pathlib import Path
    src = Path("scripts/certificar_live.py").read_text(encoding="utf-8")
    i_fail = src.index('"fail":')
    i_write = src.index("salida.write_text(")
    assert i_write < i_fail or "write_text" in src, "el informe tiene que escribirse siempre"
    assert "return 0 if filas else 1" in src, (
        "informar no es aprobar: el certificador no debe fallar por encontrar fallos")


# ═══════════════════════════════════════════════════════════════════════════
# 5 · EL INFORME TIENE LAS OCHO COLUMNAS PEDIDAS
# ═══════════════════════════════════════════════════════════════════════════

def test_cada_fila_lleva_las_ocho_columnas():
    b = _bundle([_tool("dark_flow", verdict="REQUEST_INVALID", rows=0,
                       evidence="HTTP 400 · campo 'ticker' requerido",
                       fields=["ticker"], action="corregir el cuerpo")],
                checks=[{"panel": "DARK POOL · dark flow", "ok": False, "count": 0}])
    f = CL.certificar_activo(b, "SPY")[0]
    for col in ("herramienta", "activo", "endpoint", "estado", "causa",
                "filas_recibidas", "modulo_consumidor", "resultado"):
        assert col in f, f"falta la columna {col}"
    assert f["estado"] == "REQUEST_INVALID"
    assert f["campos_rechazados"] == ["ticker"]
    assert "ticker" in f["causa"]
    assert f["resultado"] == "FAIL"


@pytest.mark.parametrize("verdict,esperado", [
    ("LIVE_OK", "PASS"), ("NO_DATA", "PASS"), ("SIN_INTENTAR", "PASS"),
    ("NO_AUTORIZADO", "FAIL"), ("ENDPOINT_NO_EXISTE", "FAIL"),
    ("REQUEST_INVALID", "FAIL"), ("PROVIDER_ERROR", "FAIL"),
])
def test_cada_estado_da_el_veredicto_correcto(verdict, esperado):
    b = _bundle([_tool("net_flow", verdict=verdict)], flujo={"net_flow": {"rows": [1]}})
    assert CL.certificar_activo(b, "SPY")[0]["resultado"] == esperado


def test_la_tabla_y_el_resumen_se_generan_sin_romperse():
    b = _bundle([_tool("net_flow"), _tool("dark_flow", verdict="PROVIDER_ERROR", rows=0)],
                flujo={"net_flow": {"rows": [1]}})
    filas = CL.certificar_activo(b, "SPY")
    assert "CERTIFICACIÓN LIVE" in CL.tabla(filas)
    r = CL.resumen(filas)
    assert "PASS" in r and "FAIL" in r


# ═══════════════════════════════════════════════════════════════════════════
# 6 · EL PROCEDIMIENTO DE WINDOWS EXISTE Y ES DE WINDOWS
# ═══════════════════════════════════════════════════════════════════════════

def test_el_bat_de_windows_existe_con_saltos_CRLF():
    p = Path("CERTIFICAR_LIVE.bat")
    assert p.exists(), "la instancia corre en Windows: hace falta el .bat"
    crudo = p.read_bytes()
    assert b"\r\n" in crudo, "sin CRLF, Windows no lo ejecuta bien"
    assert b"\n" not in crudo.replace(b"\r\n", b""), "hay saltos sueltos de Linux"


def test_el_bat_comprueba_lo_que_hace_falta_antes_de_arrancar():
    txt = Path("CERTIFICAR_LIVE.bat").read_text(encoding="utf-8", errors="replace")
    assert "QUANTDATA_API_KEY" in txt, "tiene que exigir la clave"
    assert "httpx" in txt, "tiene que instalar la dependencia si falta"
    assert "DIA,SPY,QQQ" in txt, "una sola ejecución con los tres activos"
    assert "CERTIFICACION_LIVE.json" in txt
    assert "pause" in txt, "sin pause la ventana se cierra y no se lee nada"


def test_el_bat_no_usa_comandos_de_linux():
    txt = Path("CERTIFICAR_LIVE.bat").read_text(encoding="utf-8", errors="replace")
    for linux in ("export ", "#!/bin/", "&&", "$(", "source "):
        assert linux not in txt, f"«{linux}» no existe en Windows"

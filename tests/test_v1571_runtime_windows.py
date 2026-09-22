"""v1.57.1 · EL RUNTIME CERTIFICADO TIENE QUE EXISTIR PARA WINDOWS.

EL DEFECTO
----------
El gate exigía `sys.version_info[:3] == (3, 12, 14)` y en Windows era imposible
cumplirlo: la rama 3.12 está en fase de solo seguridad, sus releases son
source-only (PEP 693) y python.org no distribuye instalador de Windows desde
3.12.10. Certificar obligaba a compilar CPython a mano —lo contrario de la
reproducibilidad que el gate existe para garantizar—.

Y el repositorio ya se había contradicho para salir del paso:

    .python-version      3.12.14      lo que el gate exigía
    INSTALAR_WEB.bat     3.12.10      lo que el instalador ponía, a mano

Se instalaba una versión y se certificaba contra otra. Nadie lo vio porque
NINGÚN control comparaba los dos ficheros. Esta suite añade ese control y los
cuatro que lo rodean.

LA DETERMINACIÓN, con su evidencia medida, está en docs/RUNTIME_CERTIFICADO.md.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(".").resolve()


def _texto(ruta: str) -> str:
    return (ROOT / ruta).read_text(encoding="utf-8", errors="replace")


def _gate():
    spec = importlib.util.spec_from_file_location(
        "gate_runtime", ROOT / "scripts" / "release_gate_full.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gate_runtime"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def declaracion():
    return json.loads(_texto(".python-runtime.json"))


@pytest.fixture()
def restaura():
    """Deja `.python-version` y `.python-runtime.json` como estaban.

    El guardián lee ficheros del árbol, así que probarlo exige tocarlos. Que una
    prueba deje el pin cambiado rompería el resto de la suite según el orden de
    ejecución, que es el defecto de aislamiento más caro de diagnosticar.
    """
    pin = _texto(".python-version")
    decl = _texto(".python-runtime.json")
    bat = _texto("INSTALAR_WEB.bat")
    yield
    (ROOT / ".python-version").write_text(pin, encoding="utf-8")
    (ROOT / ".python-runtime.json").write_text(decl, encoding="utf-8")
    (ROOT / "INSTALAR_WEB.bat").write_text(bat, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# 1 · EL PIN VIGENTE ES INSTALABLE EN WINDOWS
# ═══════════════════════════════════════════════════════════════════════════

def test_el_pin_y_la_declaracion_dicen_lo_mismo(declaracion):
    assert declaracion["certificado"]["version"] == _texto(".python-version").strip()


def test_la_rama_del_pin_es_certificable_en_windows(declaracion):
    pin = _texto(".python-version").strip()
    rama = ".".join(pin.split(".")[:2])
    info = declaracion["ramas"][rama]
    assert info["certificable_en_windows"] is True
    assert info["instaladores_windows"] is True


def test_el_parche_no_es_posterior_al_ultimo_con_instalador(declaracion):
    """Fijar 3.12.14 cuando el último binario es 3.12.10 es el defecto original."""
    pin = _texto(".python-version").strip()
    rama = ".".join(pin.split(".")[:2])
    ultimo = declaracion["ramas"][rama]["ultimo_instalador_windows"]
    a = tuple(int(x) for x in pin.split("."))
    b = tuple(int(x) for x in ultimo.split("."))
    assert a <= b, f"{pin} es posterior a {ultimo}, que no tiene binario oficial"


def test_la_rama_3_12_queda_declarada_como_no_certificable(declaracion):
    """La causa raíz, escrita: source-only desde 3.12.11."""
    r312 = declaracion["ramas"]["3.12"]
    assert r312["certificable_en_windows"] is False
    assert r312["instaladores_windows"] is False
    assert r312["ultimo_instalador_windows"] == "3.12.10"
    assert "source-only" in r312["motivo"]


def test_el_bloqueo_de_3_14_esta_nombrado_con_su_version_minima(declaracion):
    """«No se puede» sin decir qué falta no es accionable."""
    r314 = declaracion["ramas"]["3.14"]
    assert r314["certificable_en_windows"] is False
    assert "pandas" in r314["bloqueado_por"]
    assert "2.3.3" in r314["bloqueado_por"], "hace falta la versión mínima exacta"


def test_la_declaracion_no_ha_caducado(declaracion):
    pin = _texto(".python-version").strip()
    rama = ".".join(pin.split(".")[:2])
    limite = dt.date.fromisoformat(declaracion["ramas"][rama]["revisar_antes_de"])
    assert limite >= dt.date.today(), (
        "la rama certificada sale de la fase BUGFIX: recertifica antes de seguir")


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LOS TRES SITIOS QUE FIJABAN LA VERSIÓN YA NO PUEDEN DISCREPAR
# ═══════════════════════════════════════════════════════════════════════════

def test_el_instalador_de_windows_lee_el_pin_en_vez_de_fijarlo():
    bat = _texto("INSTALAR_WEB.bat")
    assert "set /p ITMQ_PYTHON=<.python-version" in bat
    assert "py -%ITMQ_PYMM% -m venv .venv" in bat


def test_el_instalador_no_lleva_ninguna_version_escrita_a_mano():
    """Una constante duplicada es una discrepancia futura.

    Se miran los COMANDOS: el `rem` que explica el defecto nombra las versiones
    de entonces, y eso es lo que hace útil el comentario.
    """
    lineas = [l for l in _texto("INSTALAR_WEB.bat").splitlines()
              if not l.strip().lower().startswith(("rem ", "rem\t", "::"))]
    huellas = re.findall(r"(?<![\d.])3\.\d+\.\d+(?![\d.])", "\n".join(lineas))
    assert not huellas, f"versiones fijadas a mano: {sorted(set(huellas))}"


def test_el_contenedor_usa_el_mismo_interprete_que_windows():
    pin = _texto(".python-version").strip()
    dockerfile = _texto("Dockerfile")
    assert f"FROM python:{pin}-slim-bookworm@sha256:" in dockerfile, (
        "el contenedor y Windows tienen que certificar el mismo intérprete")


def test_el_ci_lee_el_pin_del_fichero_y_no_una_constante():
    assert "python-version-file: .python-version" in _texto(".github/workflows/ci.yml")


def test_el_lock_de_windows_no_dice_una_rama_que_ya_no_es_la_certificada():
    """La cabecera del lock declaraba CPython 3.12 y era lo que se leía primero."""
    cabecera = "\n".join(_texto("requirements.windows.lock.txt").splitlines()[:6])
    pin = _texto(".python-version").strip()
    rama = ".".join(pin.split(".")[:2])
    assert rama in cabecera
    assert "CPython 3.12." not in cabecera


# ═══════════════════════════════════════════════════════════════════════════
# 3 · EL GUARDIÁN RECHAZA CADA FORMA DEL DEFECTO
# ═══════════════════════════════════════════════════════════════════════════

def _escribe_pin(version: str, *, rama: str | None = None) -> None:
    (ROOT / ".python-version").write_text(version + "\n", encoding="utf-8")
    decl = json.loads(_texto(".python-runtime.json"))
    decl["certificado"]["version"] = version
    decl["certificado"]["rama"] = rama or ".".join(version.split(".")[:2])
    (ROOT / ".python-runtime.json").write_text(json.dumps(decl), encoding="utf-8")


def test_el_guardian_acepta_el_pin_vigente():
    _gate().windows_runtime_guard()


def test_el_guardian_rechaza_una_version_source_only(restaura):
    """El caso exacto que bloqueaba la certificación en Windows."""
    _escribe_pin("3.12.14")
    with pytest.raises(SystemExit) as caja:
        _gate().windows_runtime_guard()
    assert "no es certificable en Windows" in str(caja.value)


def test_el_guardian_rechaza_un_parche_sin_binario_oficial(restaura):
    _escribe_pin("3.13.99")
    with pytest.raises(SystemExit) as caja:
        _gate().windows_runtime_guard()
    assert "posterior al ultimo con instalador" in str(caja.value)


def test_el_guardian_rechaza_que_el_pin_y_la_declaracion_discrepen(restaura):
    """El defecto que nadie miraba: dos ficheros con dos versiones."""
    (ROOT / ".python-version").write_text("3.13.15\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caja:
        _gate().windows_runtime_guard()
    assert "una sola version certificada" in str(caja.value)


def test_el_guardian_vence_cuando_la_rama_sale_de_bugfix(restaura):
    """Un control que no caduca se convierte en un comentario."""
    decl = json.loads(_texto(".python-runtime.json"))
    rama = ".".join(_texto(".python-version").strip().split(".")[:2])
    decl["ramas"][rama]["revisar_antes_de"] = "2020-01-01"
    (ROOT / ".python-runtime.json").write_text(json.dumps(decl), encoding="utf-8")
    with pytest.raises(SystemExit) as caja:
        _gate().windows_runtime_guard()
    assert "caduco" in str(caja.value)


def test_el_guardian_rechaza_un_instalador_que_fija_la_version(restaura):
    bat = _texto("INSTALAR_WEB.bat").replace(
        "py -%ITMQ_PYMM% -m venv .venv", "py -3.13 -m venv .venv\n  echo 3.13.12")
    (ROOT / "INSTALAR_WEB.bat").write_text(bat, encoding="utf-8")
    with pytest.raises(SystemExit) as caja:
        _gate().windows_runtime_guard()
    assert "a mano" in str(caja.value)


def test_el_guardian_corre_antes_del_preflight_de_toolchain():
    """Si el runtime no existe para la plataforma, lo demás no importa."""
    src = _texto("scripts/release_gate_full.py")
    assert src.index("windows_runtime_guard()\n        deployment_guard()") < \
        src.index("problems = toolchain_preflight(")


def test_la_declaracion_y_su_documento_son_obligatorios_para_desplegar():
    src = _texto("scripts/release_gate_full.py")
    bloque = src[src.index("def deployment_guard"):src.index("workflow_dir =")]
    assert "RUNTIME_DECL" in bloque
    assert 'ROOT / "docs" / "RUNTIME_CERTIFICADO.md"' in bloque

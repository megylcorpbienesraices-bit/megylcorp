"""v1.27.20 · Windows installation must be deterministic and never select Python 3.14."""
from pathlib import Path
import re
from conftest import assert_version_at_least, assert_marker_version_at_least
ROOT=Path(__file__).resolve().parents[1]

def text(rel):
    return (ROOT/rel).read_text(encoding="utf-8",errors="replace")

def test_windows_installer_requires_the_certified_cpython_x64_and_private_venv():
    """v1.57.1 · la rama exacta ya no se escribe aquí: se lee de `.python-version`.

    Esta prueba exigía `py -3.12` y `sys.version_info[:2]==(3,12)`. Fijar la rama
    en la prueba convertía el defecto en contrato: la rama 3.12 es source-only y
    python.org no distribuye instalador de Windows desde 3.12.10, así que exigir
    3.12 era exigir un binario que no existe. Ver docs/RUNTIME_CERTIFICADO.md.

    Lo que hay que seguir garantizando es lo de siempre: versión EXACTA, x64 y
    entorno privado. Sólo que ahora la versión la manda el pin.
    """
    s=text("INSTALAR_WEB.bat")
    assert 'set /p ITMQ_PYTHON=<.python-version' in s
    assert 'py -%ITMQ_PYMM% -m venv .venv' in s
    # La comprobación de versión es contra el pin LEÍDO, no contra una constante.
    assert "pathlib.Path('.python-version').read_text" in s
    assert "got==want" in s
    assert "platform.architecture()[0]=='64bit'" in s
    assert 'py -m venv .venv' not in s.replace('py -%ITMQ_PYMM% -m venv .venv','')

def test_windows_install_never_compiles_scientific_stack_from_source():
    s=text("INSTALAR_WEB.bat")
    assert '--require-hashes --no-deps --only-binary=:all:' in s
    assert '-r requirements.windows.lock.txt' in s
    assert 'requirements.production.lock.txt' not in s

def test_windows_lock_is_hash_complete_and_excludes_linux_only_uvloop():
    s=text("requirements.windows.lock.txt")
    assert 'uvloop==' not in s
    packages=[]
    for line in s.splitlines():
        if line and not line.startswith((' ','#','\\')) and '==' in line:
            packages.append(line.split('==',1)[0].lower())
    assert 'pandas' in packages and 'numpy' in packages and 'scipy' in packages
    assert len(packages) >= 39
    blocks=re.split(r'(?m)^(?=[A-Za-z0-9_.\-]+(?:\[[^\]]+\])?==)',s)
    pkgblocks=[b for b in blocks if '==' in b.splitlines()[0] if b.splitlines()]
    assert pkgblocks
    for b in pkgblocks:
        assert '--hash=sha256:' in b, b.splitlines()[0]

def test_windows_configuration_and_provider_tools_do_not_fallback_to_global_python():
    for rel in ("CONFIGURAR_WEB.bat","CONFIGURAR_WEB_CONSOLA.bat","PROBAR_PROVEEDORES.bat","PROBAR_TASTYTRADE.bat"):
        s=text(rel).lower()
        assert '.venv\\scripts\\python.exe' in s
        assert 'primero ejecuta instalar_web.bat' in s
    assert 'py setup_local' not in text('CONFIGURAR_WEB.bat').lower()
    assert 'py setup_local' not in text('CONFIGURAR_WEB_CONSOLA.bat').lower()

def test_release_identity_advanced_to_12720_without_quant_logic_change_claim():
    """Ninguna release puede cambiar la matemática del motor EN SILENCIO.

    El guardián original exigía la frase de "no se modifican fórmulas". Eso funciona
    mientras ninguna release toque el cálculo, pero obliga a escribir algo falso en
    cuanto una lo hace — que es peor que no declarar nada. Lo que el guardián
    protege es la ausencia de cambios ocultos, no la ausencia de cambios.

    Así que se acepta cualquiera de las dos declaraciones explícitas, y ninguna
    otra: o no cambia la matemática, o se dice cuál cambia y por qué.
    """
    assert_version_at_least('1.27.20')
    assert_marker_version_at_least('1.27.20')
    current=text('VERSION.txt').strip(); ch=text(f'CHANGELOG_v{current}.md')
    sin_cambio='No se modifican fórmulas, pesos del Scanner ni autoridad direccional.' in ch
    con_cambio=('**Una fórmula sí cambia**' in ch or 'CAMBIO DE FÓRMULA DECLARADO' in ch)
    assert sin_cambio or con_cambio, (
        'el CHANGELOG debe declarar explícitamente si la matemática del motor cambia')
    if con_cambio:
        # Un cambio declarado exige además decir qué NO cambió, para que el alcance
        # quede acotado en lugar de dejarlo a la imaginación del lector.
        assert 'pesos del Scanner' in ch and 'autoridad direccional' in ch
        assert f'QUANT_ENGINE_AUDIT_v{current}.md' in ch or 'auditoría' in ch.lower()

from __future__ import annotations

"""Utilidades compartidas de test (release consolidada).

Problema que resuelve
---------------------
Los tests estaban escritos como actas de aceptación de release:

    assert (ROOT/'VERSION.txt').read_text().strip() == '1.26.2'

Eso convierte cada test en desechable: la versión 1.27.0 rompe 28 tests que no
tienen NADA que ver con lo que cambió. Con el CI en rojo permanente, la suite
deja de proteger contra regresiones — que es su único trabajo.

La intención real de esos asserts era "esta funcionalidad existe desde 1.26.2".
Eso se expresa con una comparación monótona, no con igualdad.
"""

import pytest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _parse(v: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(v).strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))[:3]


def current_version() -> str:
    return (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()


def product_marker() -> dict:
    return json.loads((ROOT / ".itm_quant_product.json").read_text(encoding="utf-8"))


def assert_version_at_least(minimum: str) -> None:
    """La release empaquetada debe ser >= la versión que introdujo la feature.

    Reemplaza `assert version == 'X'`. Sigue detectando el fallo real (empaquetar
    una build vieja) sin romperse en cada bump de versión.
    """
    cur = current_version()
    assert _parse(cur) >= _parse(minimum), (
        f"VERSION.txt es {cur}, se esperaba >= {minimum}. "
        "El paquete contiene una build anterior a la que introdujo esta feature."
    )


def assert_marker_version_at_least(minimum: str) -> None:
    marker = product_marker()
    got = str(marker.get("version", "0"))
    assert _parse(got) >= _parse(minimum), (
        f".itm_quant_product.json declara {got}, se esperaba >= {minimum}"
    )
    assert _parse(got) == _parse(current_version()), (
        f"desincronización de release: VERSION.txt={current_version()} "
        f"pero .itm_quant_product.json={got}"
    )


def require_asset(*relative_paths: str) -> Path:
    """Require a release asset. Missing assets are a failing acceptance contract.

    A removed asset must be accompanied by deletion/migration of the test that named it;
    silently skipping is not a third option.
    """
    for rel in relative_paths:
        p = ROOT / rel
        if p.exists():
            return p
    raise AssertionError(
        "asset no empaquetado: " + " | ".join(relative_paths) +
        ". Restaura el asset o migra/elimina el test; no se permite skip por ausencia."
    )


def assert_dashboard_uses_runtime_version(html: str) -> None:
    """La UI debe consumir la versión runtime, nunca fijar un release histórico."""
    assert '{{ app_version }}' in html, 'dashboard debe recibir app_version desde VERSION.txt'
    assert '?v={{ app_version }}' in html, 'cache bust de assets debe seguir VERSION.txt'

def assert_source_uses_runtime_version(source: str) -> None:
    """Código operativo debe referenciar APP_VERSION en vez de literales de release."""
    assert 'APP_VERSION' in source


@pytest.fixture(autouse=True)
def _reset_per_symbol_runtime():
    """Estado por símbolo limpio entre pruebas.

    v1.45.0 · El Last Known Good, el registro de procedencia y los muros vigentes
    viven en memoria del proceso y sobreviven a una prueba. Eso es correcto en
    ejecución —es justo lo que sostiene TRACE cuando el mercado cierra— pero entre
    pruebas convierte el resultado de una en la entrada de la siguiente: una que
    guarda un Interval Map válido hace que la siguiente, que comprueba el caso
    «sin datos», reciba el mapa de la anterior y pase o falle por el motivo
    equivocado.

    Se limpia antes y después para que ninguna prueba dependa del orden.
    """
    def _clear():
        try:
            from app.core.data_hub_runtime import HUB_RUNTIME
            HUB_RUNTIME.reset()
        except Exception:
            pass
        try:
            from app.core.data_lineage import LINEAGE
            LINEAGE.reset()
        except Exception:
            pass
        try:
            from app.core.wall_engine import WALLS
            WALLS.reset()
        except Exception:
            pass

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _catalogo_de_activos_intacto():
    """Devuelve el catálogo de activos como estaba después de cada prueba.

    v1.58.0 · `ASSETS` es un diccionario de módulo, vivo y compartido. Varias
    pruebas registran activos, cambian `full`/`selectable` al mover proveedores
    de roster, o recalculan capacidades. Eso deja el catálogo distinto para las
    siguientes, y el contrato del universo —«el catálogo es EXACTAMENTE estos
    símbolos»— pasaba o fallaba según el orden de ejecución.

    Un contrato que depende del orden no es un contrato. Se restaura aquí en vez
    de en cada prueba porque el que ensucia no siempre es el que falla.
    """
    # Por el MÓDULO, no por referencia: varias pruebas hacen
    # `importlib.reload(app.core.assets)` y eso crea un diccionario nuevo. Guardar
    # la referencia vieja y restaurarla ahí dejaría el catálogo bueno en un objeto
    # que ya no mira nadie.
    import app.core.assets as _A

    copia = {k: dict(v) for k, v in _A.ASSETS.items()}
    try:
        yield
    finally:
        _A.ASSETS.clear()
        _A.ASSETS.update(copia)


@pytest.fixture()
def universo_ampliado():
    """Admite símbolos sintéticos en el universo durante una prueba.

    v1.58.0 · El universo está cerrado, y eso es lo correcto en producción: nada
    entra sin estar en la lista. Pero varias pruebas comprueban el CAMINO de
    registro —que una acción y un ETF entren por la misma puerta, que la cadena
    sólo se prometa cuando el proveedor la confirma— y para eso necesitan
    símbolos que no existen de verdad.

    Usar tickers reales para eso sería peor: ataría la prueba a que ese símbolo
    siga en el universo mañana. Aquí se abre el cerrojo lo justo y se cierra al
    terminar, pasa lo que pase.
    """
    import app.core.universe as U

    original = U.ALLOWED

    def _admitir(*simbolos: str):
        U.ALLOWED = frozenset(set(original) | {s.upper() for s in simbolos})
        return U.ALLOWED

    try:
        yield _admitir
    finally:
        U.ALLOWED = original

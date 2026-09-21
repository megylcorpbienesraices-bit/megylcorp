"""v1.57.5 · `/legacy` SE QUEDA HASTA QUE EL FRONTEND ACTUAL ESTÉ CERTIFICADO.

Durante el barrido de residuos apareció la ruta `/legacy`: el dashboard de la
generación anterior, con dieciocho ficheros JS y una hoja de estilo que la
terminal actual no carga. Por tamaño parece el candidato obvio a desaparecer.

No lo es todavía, y la decisión es del operador, no del barrido:

    «Déjalo temporalmente como referencia de comparación y diagnóstico hasta
     que el dashboard actual pase la certificación LIVE completa y quede
     estable en DIA, SPY y QQQ.»

La razón es buena y técnica: mientras la terminal nueva no esté certificada en
vivo, `/legacy` es la única forma de comparar lo que se ve contra una
implementación independiente. Borrarlo antes deja al operador sin testigo
justo en la fase en la que más falta hace.

Estas pruebas existen para que nadie —tampoco una pasada de limpieza futura— lo
quite por su cuenta. Se retiran cuando el operador dé la certificación LIVE por
cerrada y pida la auditoría final de `/legacy`.
"""
from __future__ import annotations

import pathlib

import pytest

RAIZ = pathlib.Path(".")

#: Lo que `/legacy` necesita para seguir sirviendo. Si algo de esto desaparece,
#: la ruta se queda a medias, que es peor que no tenerla.
PLANTILLA = "app/templates/dashboard.html"


def _main() -> str:
    return (RAIZ / "app/main.py").read_text("utf-8")


def test_la_ruta_legacy_sigue_existiendo():
    src = _main()
    assert '@app.get("/legacy"' in src, (
        "`/legacy` se eliminó antes de que el frontend actual estuviera "
        "certificado LIVE en DIA, SPY y QQQ")
    assert 'name="dashboard.html"' in src


def test_la_plantilla_de_legacy_sigue_en_su_sitio():
    assert (RAIZ / PLANTILLA).is_file(), PLANTILLA


def test_legacy_conserva_TODOS_sus_estaticos():
    """Una ruta que carga la mitad de sus ficheros no sirve para comparar."""
    import re
    html = (RAIZ / PLANTILLA).read_text("utf-8")
    refs = re.findall(r'(?:src|href)="/static/([^"?]+)', html)
    assert refs, "la plantilla dejó de declarar sus estáticos"
    faltan = [r for r in sorted(set(refs)) if not (RAIZ / "app/static" / r).is_file()]
    assert not faltan, f"estáticos de /legacy que ya no existen: {faltan}"


def test_la_razon_de_conservarlo_esta_escrita_en_el_codigo():
    """Para que el próximo que lo lea no lo tome por olvido."""
    src = _main()
    bloque = src[src.find('@app.get("/legacy"'):][:600]
    assert "Dashboard anterior" in bloque
    assert "compar" in bloque.lower(), (
        "la ruta tiene que decir POR QUÉ se conserva, no sólo que existe")


def test_la_terminal_actual_NO_depende_de_legacy():
    """Conservarlo no puede convertirse en una dependencia.

    `/legacy` es un testigo, no una pieza. El día que la certificación LIVE
    cierre, tiene que poder quitarse de una pieza y sin tocar nada más.
    """
    terminal = (RAIZ / "app/templates/terminal.html").read_text("utf-8")
    assert "dashboard.html" not in terminal
    assert "/legacy" not in terminal
    for js in ("itmq_app.js", "itmq_core.js", "itmq_trace.js",
               "itmq_panels.js", "itmq_orderflow.js"):
        txt = (RAIZ / "app/static" / js).read_text("utf-8")
        assert "/legacy" not in txt, js


def test_la_limpieza_no_puede_llevarselo_por_delante():
    limpiar = (RAIZ / "LIMPIAR.bat").read_text("utf-8", errors="replace")
    for pieza in ("dashboard", "legacy", "static"):
        assert pieza not in limpiar, pieza

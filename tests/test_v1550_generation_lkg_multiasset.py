"""v1.55.0 · Generación, último valor bueno y multiactivo de verdad.

TRES REGLAS
-----------
20 · Un cambio de activo es una TRANSACCIÓN. O la pantalla entera es del activo
     nuevo, o sigue siendo del anterior. Media pantalla de cada es lo que pasa
     cuando una respuesta lenta del símbolo viejo llega DESPUÉS de la del nuevo.

22 · El último valor bueno no es una promesa: es un inventario. Hay que poder
     contestar «¿qué está protegido y qué no?» sin leer el código.

24 · Nada de `if symbol == "QQQ"`. Una rama por ticker es cómo un motor
     multiactivo se convierte en trece motores de un activo que comparten
     repositorio.
"""
from __future__ import annotations

import re
from pathlib import Path

import plotly.graph_objects as go

from app.core import flow_view as FV
from app.service import _fig_json
from app.terminal_api import build_terminal_bundle

SIMBOLOS = ("QQQ", "SPY", "IWM", "DIA", "AAPL", "NVDA", "TSLA", "SPX")


# ── 20 · generation_id ───────────────────────────────────────────────────

def test_el_bundle_declara_su_generacion():
    b = build_terminal_bundle(state={"active_symbol": "QQQ", "symbol_epoch": 7,
                                     "ready": True}, trace={})
    assert b["generation_id"] == "QQQ#7"


def test_la_generacion_cambia_con_el_activo_y_con_la_epoca():
    def gen(sym, epoch):
        return build_terminal_bundle(state={"active_symbol": sym, "symbol_epoch": epoch,
                                            "ready": True}, trace={})["generation_id"]
    assert gen("QQQ", 7) != gen("SPY", 7)
    assert gen("QQQ", 7) != gen("QQQ", 8)
    assert gen("QQQ", 7) == gen("QQQ", 7)


def test_sin_epoca_la_generacion_no_se_inventa_un_numero():
    b = build_terminal_bundle(state={"active_symbol": "QQQ", "ready": True}, trace={})
    assert b["generation_id"] == "QQQ"


def test_el_cliente_descarta_un_bundle_de_otra_generacion():
    """Y lo descarta ENTERO: aprovechar «lo que sirva» es imposible, porque
    después no se distingue lo que sirve de lo que no."""
    js = Path("app/static/itmq_app.js").read_text(encoding="utf-8")
    cuerpo = js[js.index("async function pullBundle()"):js.index("renderAll(d);")]
    assert "d.generation_id" in cuerpo
    assert "state.switching" in cuerpo
    assert "return;" in cuerpo
    # El descarte ocurre ANTES de guardar el bundle y de repintar.
    assert cuerpo.index("llega !== esperado") < cuerpo.index("state.bundle = d;")


# ── 22 · Inventario de último valor bueno ────────────────────────────────

def test_el_inventario_lista_lo_que_realmente_hay_guardado():
    FV.reset()
    assert FV.coverage()["count"] == 0
    FV.lane("QQQ", "2026-09-19", "tape_buckets", [1, 2, 3])
    FV.lane("SPY", "2026-09-19", "net_flow", [1])
    c = FV.coverage()
    assert c["count"] == 2
    assert c["datasets"] == ["net_flow", "tape_buckets"]
    assert c["symbols"] == ["QQQ", "SPY"]


def test_el_inventario_declara_la_clave_de_indexacion():
    """Un LKG mal indexado es peor que no tenerlo: enseña un número correcto en
    el sitio equivocado."""
    assert FV.coverage()["key"] == "(symbol, session_date, dataset)"


def test_un_valor_nulo_no_entra_en_el_inventario():
    FV.reset()
    FV.lane("QQQ", "2026-09-19", "tape_buckets", None)
    assert FV.coverage()["count"] == 0


def test_el_inventario_viaja_al_auditor():
    src = Path("app/terminal_api.py").read_text(encoding="utf-8")
    assert 'auditor["last_known_good"] = _FV2.coverage()' in src


# ── 24 · Multiactivo sin ramas por ticker ────────────────────────────────

def _codigo_python(ruta: Path) -> str:
    """El fichero SIN comentarios ni literales de cadena.

    El barrido tiene que mirar CODIGO. Buscando sobre el texto crudo, la propia
    linea que documenta la regla —«`if symbol == "DIA"` is forbidden»— sale como
    infraccion, y un guardia que se denuncia a si mismo se acaba desactivando.
    """
    import io
    import tokenize
    trozos = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(ruta.read_text(encoding="utf-8")).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            trozos.append(f"{tok.start[0]}|{tok.string}")
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ""
    return "\n".join(trozos)


def _codigo_js(ruta: Path) -> str:
    txt = ruta.read_text(encoding="utf-8")
    txt = re.sub(r"/\*.*?\*/", " ", txt, flags=re.S)
    txt = re.sub(r"(?m)//.*$", " ", txt)
    return txt


def test_no_queda_ninguna_comparacion_contra_un_ticker_en_produccion():
    """Una rama por ticker es como un motor multiactivo se convierte en trece
    motores de un activo que comparten repositorio."""
    tickers = "QQQ|SPY|SPX|IWM|DIA|AAPL|NVDA|TSLA|VIX"
    js = re.compile(rf"""(symbol|ticker|sym)\s*(===|==|!==|!=)\s*['\"]({tickers})""")
    ofensas = []
    for ruta in Path("app").rglob("*.py"):
        if "test" in ruta.name:
            continue
        codigo = _codigo_python(ruta)
        # Tokens consecutivos: <nombre> == "TICKER"
        toks = [t.split("|", 1) for t in codigo.splitlines() if "|" in t]
        for i in range(len(toks) - 2):
            (_l0, a), (_l1, op), (_l2, b) = toks[i], toks[i + 1], toks[i + 2]
            if op in ("==", "!=") and re.fullmatch(r"(symbol|ticker|sym|_sym)", a):
                if re.fullmatch(rf"({tickers})", b.strip("'\"")):
                    ofensas.append(f"{ruta}:{toks[i][0]}: {a} {op} {b}")
    for ruta in Path("app").rglob("*.js"):
        if "test" in ruta.name:
            continue
        if js.search(_codigo_js(ruta)):
            ofensas.append(str(ruta))
    assert not ofensas, "rama por ticker en produccion:\n" + "\n".join(ofensas)


def test_el_reemplazo_del_ticker_heredado_respeta_los_bordes_de_palabra():
    """`str.replace("DIA", sym)` convertia «MEDIA» en «MEQQQ» y «DIARIO» en
    «QQQRIO». «DIA» es subcadena de palabras corrientes en espanol, asi que ese
    reemplazo corrompia rotulos en todos los activos MENOS en el unico que se
    probaba, que era justo el que llevaba el ticker escrito."""
    fig = go.Figure()
    fig.update_layout(title="MEDIA MÓVIL DIA · DIARIO")
    out = _fig_json(fig, "QQQ")["layout"]["title"]["text"]
    assert out == "MEDIA MÓVIL QQQ · DIARIO"


def test_el_reemplazo_sin_simbolo_no_toca_nada():
    fig = go.Figure()
    fig.update_layout(title="GEX DIA")
    assert _fig_json(fig)["layout"]["title"]["text"] == "GEX DIA"


def test_los_ocho_activos_recorren_el_mismo_camino():
    formas = set()
    for sym in SIMBOLOS:
        b = build_terminal_bundle(state={"active_symbol": sym, "symbol_epoch": 1,
                                         "ready": True}, trace={})
        formas.add(tuple(sorted(b.keys())))
    assert len(formas) == 1, "algun activo produce un bundle con otra forma"


def test_el_catalogo_de_instrumentos_describe_fisica_no_senales():
    """Un multiplicador es un hecho del contrato; un umbral es una decision de
    modelo. La linea entre las dos es lo que separa un motor multiactivo de
    trece motores de un activo."""
    ruta = Path("app/core/instruments.py")
    assert "multiplier" in ruta.read_text(encoding="utf-8")
    # Sobre el CODIGO, no sobre la prosa que explica la regla.
    codigo = _codigo_python(ruta).lower()
    for prohibido in ("threshold", "evidence_score", "signal_weight"):
        assert prohibido not in codigo, f"el catalogo contiene una decision de modelo: {prohibido}"

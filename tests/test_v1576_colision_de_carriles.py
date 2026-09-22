"""v1.57.6 · LOS DOS CARRILES METÍAN TIPOS DISTINTOS EN LA MISMA RANURA.

En el arranque real, la consola del motor:

    WARNING itm.quantdata_intelligence  degradado en
        quantdata_intelligence:normalize:net_flow [DEGRADED]:
        AttributeError: 'dict' object has no attribute 'payload'
    WARNING itm.quantdata_intelligence  degradado en
        quantdata_intelligence:normalize:net_drift [DEGRADED]: (idéntico)

El hub funde las peticiones duplicadas de los dos carriles por
`(dataset, símbolo)`, que es lo correcto: ahorra cuota de verdad. El problema
era QUÉ metía cada uno en esa ranura compartida:

    carril del motor   →  `self._post(...)`      devuelve el DICT del payload
    carril de páginas  →  `_client.post(...)`    devolvía el OBJETO QuantDataResponse

Quien llegaba segundo recibía el objeto del otro carril. El de páginas hacía
`response.payload` sobre un `dict` y reventaba.

Sólo puede pasar donde el nombre coincide palabra por palabra, y coincide en
exactamente tres: `net_flow`, `net_drift` e `iv_rank`. Los demás van
renombrados (`gex_by_strike` → `gamma`) y por eso nunca fallaron.

Y se volvió SISTEMÁTICO con la ráfaga de arranque de v1.57.4: antes los dos
carriles rara vez pedían lo mismo a la vez; ahora arrancan juntos.
"""
from __future__ import annotations

import pathlib

import pytest

from app.providers.quantdata.shared import ENGINE_SHARED_KEYS

FUENTE = pathlib.Path("app/providers/quantdata/intelligence.py").read_text("utf-8")
MOTOR = pathlib.Path("app/providers/quantdata/runtime.py").read_text("utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# 1 · LA COLISIÓN EXISTE Y ESTÁ IDENTIFICADA
# ═══════════════════════════════════════════════════════════════════════════

def test_hay_claves_que_COINCIDEN_entre_los_dos_carriles():
    """Si esto dejara de ser cierto, la prueba de abajo no probaría nada."""
    iguales = sorted(k for k, v in ENGINE_SHARED_KEYS.items() if k == v)
    assert iguales == ["iv_rank", "net_drift", "net_flow"], iguales


def test_el_hub_funde_por_dataset_y_simbolo():
    """La fusión es la que comparte la ranura; no se toca, se hace segura."""
    hub = pathlib.Path("app/core/data_hub_runtime.py").read_text("utf-8")
    assert 'key = (str(dataset), str(symbol).upper())' in hub


# ═══════════════════════════════════════════════════════════════════════════
# 2 · LOS DOS CARRILES METEN EL MISMO TIPO
# ═══════════════════════════════════════════════════════════════════════════

def test_el_carril_de_paginas_mete_el_PAYLOAD_no_la_respuesta():
    # v1.58.0 · el ancla era `_t0 = time.monotonic()`, que desapareció al medir
    # los tiempos en el gobernador. El invariante que defiende esta prueba no
    # cambia: lo que entra en el hub es el PAYLOAD, no el objeto de respuesta.
    bloque = FUENTE.split("_plazo = _rt.deadline()", 1)[1].split("except QuantDataError", 1)[0]
    assert "async def _pedir_payload" in bloque
    assert "return respuesta.payload" in bloque
    assert "lambda: _client.post(path, body)" not in bloque, (
        "el carril de páginas volvió a meter el objeto de respuesta en el hub")


def test_el_carril_del_motor_sigue_metiendo_el_payload():
    bloque = MOTOR.split("async def _channel", 1)[1].split("jobs =", 1)[0]
    assert "HUB_RUNTIME.fetch(" in bloque
    assert 'isinstance(gate.get("payload"), dict)' in bloque


def test_ya_no_se_lee_punto_payload_sobre_lo_que_da_el_hub():
    """La línea exacta que reventaba."""
    assert "normalize(response.payload)" not in FUENTE
    assert "normalized = tool.normalize(payload)" in FUENTE


def test_el_unico_punto_payload_que_queda_es_el_de_la_respuesta_real():
    apariciones = [l.strip() for l in FUENTE.splitlines() if ".payload" in l
                   and not l.strip().startswith("#")]
    assert apariciones == ["return respuesta.payload"], apariciones


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Y SI ALGUIEN VUELVE A METER OTRA COSA, SE DICE, NO SE REVIENTA
# ═══════════════════════════════════════════════════════════════════════════

def test_un_tipo_inesperado_se_denuncia_con_su_nombre():
    bloque = FUENTE.split('payload = gate["payload"]', 1)[1].split("tool.note_attempt(path, None)", 1)[0]
    assert "isinstance(payload, dict)" in bloque
    assert "type(payload).__name__" in bloque, (
        "hay que decir QUÉ tipo llegó, no sólo que no valía")
    assert "STATUS_TRANSIENT" in bloque, "no es un fallo de ruta ni de autorización"


def test_el_cinturon_va_ANTES_de_normalizar():
    bloque = FUENTE.split('payload = gate["payload"]', 1)[1]
    i_check = bloque.index("isinstance(payload, dict)")
    i_norm = bloque.index("normalized = tool.normalize(payload)")
    assert i_check < i_norm


# ═══════════════════════════════════════════════════════════════════════════
# 4 · COMPORTAMIENTO, NO SÓLO FORMA
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("clave", ["net_flow", "net_drift", "iv_rank"])
def test_normalizar_el_payload_del_MOTOR_funciona(clave):
    """El dict que deja el motor en la ranura tiene que servirle a páginas.

    Es la comprobación de fondo: da igual quién llegue primero al hub, lo que
    sale de la ranura es normalizable por el carril de páginas.
    """
    from app.providers.quantdata.tools import build_catalog

    tool = build_catalog()[clave]
    # Un payload del proveedor tal y como lo deja el carril del motor: un dict.
    for payload in ({}, {"data": []}, {"data": [{"ticker": "DIA"}]}):
        salida = tool.normalize(payload)
        assert isinstance(salida, dict), (clave, payload)
        assert "ready" in salida, (clave, payload)


@pytest.mark.parametrize("clave", ["net_flow", "net_drift", "iv_rank"])
def test_el_objeto_de_respuesta_YA_no_se_le_pasa_al_normalizador(clave):
    """Reproduce el fallo exacto: normalizar el objeto en vez del dict.

    Se conserva para que quede claro qué se rompía, y para que nadie «arregle»
    el cinturón haciendo que el normalizador acepte cualquier cosa.
    """
    from app.providers.quantdata.tools import build_catalog

    class _RespuestaFalsa:
        payload = {"data": []}

    tool = build_catalog()[clave]
    salida = tool.normalize(_RespuestaFalsa())
    assert not salida.get("ready"), (
        f"{clave}: el normalizador aceptó un objeto que no es el payload; "
        "eso vuelve a esconder la colisión en vez de impedirla")


# ═══════════════════════════════════════════════════════════════════════════
# 5 · `ready` NO PUEDE SER «VENÍA LLENO»
# ═══════════════════════════════════════════════════════════════════════════

def test_ready_exige_un_payload_de_verdad():
    """`bool(p)` daba True para cualquier objeto no vacío."""
    from app.providers.quantdata.tools import es_payload

    assert es_payload({"data": []}) is True
    assert es_payload({}) is False
    assert es_payload(None) is False
    assert es_payload([1, 2, 3]) is False
    assert es_payload("texto") is False

    class _Cualquiera:
        payload = {"data": []}

    assert es_payload(_Cualquiera()) is False


def test_ninguna_herramienta_declara_ready_por_simple_verdad():
    import pathlib
    src = pathlib.Path("app/providers/quantdata/tools.py").read_text("utf-8")
    assert '"ready": bool(p)' not in src, (
        "volvió una herramienta que se declara viva por que el payload «venía lleno»")


# ═══════════════════════════════════════════════════════════════════════════
# 6 · EL 400 TIENE QUE ENSEÑAR EL CAMPO, NO SÓLO QUE HUBO UN 400
#
# La consola del arranque:
#
#     WARNING itm.data_hub   degradado en data_hub:max_pain [DEGRADED]:
#         QuantDataError: Quant Data HTTP 400: Request validation failed
#     WARNING itm.quantdata  degradado en quantdata:max_pain:unrepairable
#
# El backend guarda QUÉ campo señaló el proveedor desde v1.45.0, y el veredicto
# clasificado desde v1.57.0. Nada de eso llegaba a la pantalla: la fila enseñaba
# el error crudo cortado a setenta caracteres, donde un 400 se lee igual que un
# 404 y el único dato accionable se quedaba en el JSON.
# ═══════════════════════════════════════════════════════════════════════════

def _qd_diagnosis() -> str:
    js = pathlib.Path("app/static/itmq_app.js").read_text("utf-8")
    return js.split("function qdDiagnosis(t)", 1)[1].split("\n  }\n", 1)[0]


def test_la_fila_enseña_el_veredicto_clasificado():
    cuerpo = _qd_diagnosis()
    assert "t.diagnosis" in cuerpo
    assert "dg.verdict" in cuerpo
    assert "dg.action" in cuerpo


def test_la_fila_enseña_el_CAMPO_exacto_que_el_proveedor_rechaza():
    cuerpo = _qd_diagnosis()
    assert "dg.fields" in cuerpo
    assert "campo rechazado" in cuerpo


def test_la_evidencia_no_se_duplica_con_el_error_crudo():
    """Enseñar dos veces lo mismo es ruido, no diagnóstico."""
    assert "dg.evidence !== msg" in _qd_diagnosis()


#: Interpolaciones que NO son texto del servidor y por tanto no necesitan
#: escaparse: números derivados por la propia pantalla. Todo lo demás, sí.
NUMERICAS = ("Math.round(", "Q.num(", "Number(", "failed.length", "tries.length",
             "cand", "sc.base_priority", "sc.effective_priority")


def test_todo_el_TEXTO_del_servidor_va_escapado():
    """Una cadena del proveedor inyectada cruda en el DOM es una vía de entrada."""
    import re
    cuerpo = _qd_diagnosis()
    interpolaciones = [e.strip() for e in re.findall(r"\$\{([^}]+)\}", cuerpo)]
    assert interpolaciones, "qdDiagnosis dejó de componer nada"
    for e in interpolaciones:
        if e.startswith("esc(") or e.startswith(NUMERICAS):
            continue
        # Variables locales ya compuestas con esc() más arriba.
        if e in ("espera", "prio", "frio", "causa", "veredicto", "campos", "detalle",
                 "msg", "s"):
            continue
        raise AssertionError(f"interpolación sin escapar en qdDiagnosis: {e}")


def test_los_tres_trozos_nuevos_se_componen_con_esc():
    cuerpo = _qd_diagnosis()
    for trozo in ("esc(dg.verdict)", "esc(dg.action)",
                  "esc((dg.fields || []).join(' · '))"):
        assert trozo in cuerpo, trozo


def test_el_backend_sigue_entregando_el_campo():
    """La pantalla no puede enseñar lo que el backend no publique."""
    src = pathlib.Path("app/providers/quantdata/intelligence.py").read_text("utf-8")
    bloque = src.split('if status == STATUS_REQUEST_INVALID or "400" in err', 1)[1][:600]
    assert '"fields": campos[:8]' in bloque
    assert '"verdict": "REQUEST_INVALID"' in bloque


def test_un_400_y_un_404_NO_se_leen_igual():
    """El motivo por el que esto existe."""
    from app.providers.quantdata.intelligence import QuantDataIntelligence

    class _H:
        attempts = [{"path": "/v1/options/tool/max-pain", "ok": False, "error": "x"}]
        stripped_fields = ["lookBackPeriod"]
        validation_error = "field required: lookBackPeriod"
        provider_status = None

        def candidates(self):
            return ["/v1/options/tool/max-pain"]

    m = QuantDataIntelligence()
    h = _H()
    h.last_error = "Quant Data HTTP 400: Request validation failed"
    cuatrocientos = m._pending_diagnosis(h, "DEGRADADO", {})
    h.last_error = "Quant Data HTTP 404: No resource found"
    cuatrocuatro = m._pending_diagnosis(h, "DEGRADADO", {})

    assert cuatrocientos["verdict"] == "REQUEST_INVALID"
    assert cuatrocuatro["verdict"] == "ENDPOINT_NO_EXISTE"
    assert cuatrocientos["verdict"] != cuatrocuatro["verdict"]
    assert cuatrocientos["fields"] == ["lookBackPeriod"]
    assert "lookBackPeriod" in cuatrocientos["evidence"]

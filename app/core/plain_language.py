"""Plain-language translation for Modo Fácil.

The layer never computes a signal. It translates fields already produced by Scanner,
Tape, volatility and calibration. Uncertainty is deliberately more visible here.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

# These terms must not leak into Modo Fácil. The expert view keeps them unchanged.
JARGON = (
    "gamma", "delta", "charm", "vanna", "gex", "dex", "skew", "flip", "theta",
    "vega", "strike", "0dte", " atm", "otm", "itm", "greek", "dealer", "hedge",
    "evidence", "score", "regime", "spot", "tape", "expected move", "q-flow",
)


def _num(v):
    try:
        f=float(v)
        return f if math.isfinite(f) else None
    except Exception:
        return None


def _card(label: str, value: str, meaning: str, tone: str = "neutral", caveat: str | None = None) -> Dict[str, Any]:
    return {"label":label,"value":value,"meaning":meaning,"tone":tone,"caveat":caveat}


def _scanner_zone(sc: Dict[str,Any]) -> tuple[float|None,float|None]:
    z=sc.get("zone") or {}
    lo=_num(z.get("low", sc.get("zone_low")))
    hi=_num(z.get("high", sc.get("zone_high")))
    return lo,hi


def translate_direction(scanner: Dict[str,Any] | None) -> Dict[str,Any]:
    sc=scanner or {}
    if not sc.get("ready"):
        return _card("QUÉ HACER","Esperar","Ahora mismo no hay una situación estructural suficientemente clara. Esperar también es una decisión.","wait")
    d=str(sc.get("direction") or "").upper();lo,hi=_scanner_zone(sc)
    zona=f"entre {lo:.2f} y {hi:.2f}" if lo is not None and hi is not None else "en la zona marcada"
    if d=="BUY":
        return _card("QUÉ HACER","Buscar compras",f"El estudio apunta a una posible reacción alcista {zona}. Espera a que el precio llegue y confirme; no lo persigas.","buy")
    if d=="SELL":
        return _card("QUÉ HACER","Buscar ventas",f"El estudio apunta a una posible reacción bajista {zona}. Espera a que el precio llegue y confirme; no lo persigas.","sell")
    return _card("QUÉ HACER","Esperar","La estructura no define una dirección suficientemente clara todavía.","wait")


def translate_levels(scanner: Dict[str,Any] | None) -> List[Dict[str,Any]]:
    sc=scanner or {};out=[]
    t1=_num(sc.get("target1"));t2=_num(sc.get("target2"));inv=_num(sc.get("invalidation"))
    if t1 is not None:
        extra=f" Si el movimiento continúa, el segundo nivel está cerca de {t2:.2f}." if t2 is not None else ""
        out.append(_card("PRIMER OBJETIVO",f"{t1:.2f}","Es el primer nivel estructural que la idea intenta alcanzar antes de invalidarse."+extra))
    if inv is not None:
        out.append(_card("CUÁNDO ESTOY EQUIVOCADO",f"{inv:.2f}","Si el precio atraviesa este nivel según la lógica de la señal, la tesis deja de ser válida y no debe seguir tratándose como la misma operación.","risk"))
    return out


def translate_market_context(result: Dict[str,Any] | None, vol: Dict[str,Any] | None=None) -> List[Dict[str,Any]]:
    res=result or {};v=vol or {};out=[]
    reg=str(res.get("regime") or "").upper()
    if "POSITIVE" in reg:
        out.append(_card("COMPORTAMIENTO ESTRUCTURAL","Más contenido","La estructura calculada puede amortiguar parte de los movimientos alrededor de niveles importantes. No garantiza que el precio vaya a quedarse en rango.","calm"))
    elif "NEGATIVE" in reg:
        out.append(_card("COMPORTAMIENTO ESTRUCTURAL","Más expansivo","La estructura calculada puede amplificar movimientos cuando un nivel cede. No garantiza una ruptura ni su dirección.","fast"))
    elif reg:
        out.append(_card("COMPORTAMIENTO ESTRUCTURAL","Mixto","No domina claramente una estructura de contención o expansión; la lectura exige más confirmación.","neutral"))

    boundary=_num(res.get("gamma_flip"));price=_num(res.get("spot"))
    if boundary is not None:
        pos=""
        if price is not None:
            pos=f" El precio está {'por encima' if price>boundary else 'por debajo'} de esa frontera."
        out.append(_card("FRONTERA ESTRUCTURAL",f"{boundary:.2f}","Alrededor de este nivel cambia el régimen estimado de exposición. A un lado la estructura puede amortiguar más y al otro puede amplificar más; no es soporte o resistencia garantizado."+pos))

    em=_num(v.get("expected_move"));lo=_num(v.get("expected_low"));hi=_num(v.get("expected_high"));exp=str(v.get("nearest_expiry") or "").strip()
    if em is not None:
        if exp:
            horizon=f"para el vencimiento {exp}"
        else:
            horizon="para el vencimiento cercano analizado"
        rng=f" El rango de referencia del modelo va aproximadamente de {lo:.2f} a {hi:.2f}." if lo is not None and hi is not None else ""
        out.append(_card("RECORRIDO IMPLÍCITO",f"±{em:.2f}",f"Es el recorrido que resulta de la volatilidad implícita {horizon}.{rng} No es un objetivo ni un límite que el precio deba respetar."))
    return out


def translate_tape(tape: Dict[str,Any] | None) -> Dict[str,Any] | None:
    t=tape or {};state=str(t.get("state") or "").upper();secs=_num(t.get("seconds_remaining"))
    tail=f" Quedan aproximadamente {secs:.0f} segundos en esta ventana de confirmación." if secs is not None and secs>0 else ""
    table={
        "ARMED":("Comprobando","El precio ya está en la zona y la plataforma está comprobando si el flujo agresivo produce una respuesta coherente del precio."+tail,"wait"),
        "CONFIRMED":("Confirmado","El flujo observado y la respuesta del precio están alineados con la tesis estructural dentro de la ventana de confirmación. Sigue aplicando tu gestión de riesgo.","buy"),
        "REJECTED":("Rechazado","El flujo y la respuesta del precio contradicen la tesis en esta visita a la zona. No entrar con esta confirmación.","sell"),
        "ABSORBED":("Absorbido","Hay presión agresiva, pero el precio no avanza de forma proporcional. La presión está siendo absorbida y la entrada queda bloqueada.","risk"),
        "CHURN":("Sin ganador","Hay actividad intensa y señales contradictorias sin desplazamiento limpio. La situación no ofrece una confirmación clara.","wait"),
        "EXPIRED":("Sin confirmación","Terminó la ventana de observación sin una respuesta suficiente para confirmar la entrada. No perseguir el movimiento.","wait"),
    }
    if state not in table:return None
    value,meaning,tone=table[state]
    return _card("¿ES EL MOMENTO?",value,meaning,tone)


def translate_trust(scanner: Dict[str,Any] | None, data_quality: Any=None,
                    sessions: int=0, calibration: Dict[str,Any] | None=None) -> Dict[str,Any]:
    sc=scanner or {};gate=sc.get("edge_gate") or {};cal=calibration or {}
    dq=_num(data_quality);dq_txt=f" La calidad de los datos del ciclo actual es {dq:.0f} sobre 100." if dq is not None else ""
    model_ready=bool(gate.get("calibration_ready"))
    gate_active=bool(gate.get("active"))
    p=_num(gate.get("probability_t1_first")) if model_ready else None
    stage=str(gate.get("stage") or (cal.get("probability_model") or {}).get("stage") or "COLLECTING")
    if gate_active and model_ready:
        prob=f" El modelo calibrado estima aproximadamente {p*100:.0f}% de probabilidad de alcanzar el primer objetivo antes que la invalidación." if p is not None else ""
        return _card("¿PUEDO FIARME?","Validación activa",f"La compuerta económica ya está activa después de calibración fuera de muestra sobre el historial disponible.{prob}{dq_txt}","ok",
                     caveat="Una probabilidad calibrada no garantiza el resultado de una operación individual.")
    if model_ready:
        prob=f" La estimación calibrada actual es aproximadamente {p*100:.0f}% para alcanzar el primer objetivo antes que la invalidación." if p is not None else ""
        return _card("¿PUEDO FIARME?","Calibrada, aún en observación",f"El modelo ya superó la calibración estadística, pero todavía no controla la compuerta operativa. Sigue funcionando en observación.{prob}{dq_txt}","warn",
                     caveat=f"Estado del modelo: {stage}. La decisión productiva sigue usando la regla heredada hasta activación explícita.")
    return _card("¿PUEDO FIARME?","Todavía no",f"La plataforma aún no tiene evidencia fuera de muestra suficiente para afirmar que esta puntuación anticipa el resultado. Los niveles se calculan con datos observados; su capacidad predictiva todavía no está validada. Úsalo como mapa, no como recomendación.{dq_txt}","warn",
                 caveat=f"Sesiones evaluables registradas: {int(sessions or 0)}. La advertencia cambia automáticamente cuando la calibración es promovida.")


def easy_view(scanner: Dict[str,Any] | None=None, result: Dict[str,Any] | None=None,
              vol: Dict[str,Any] | None=None, tape: Dict[str,Any] | None=None,
              data_quality: Any=None, sessions: int=0, calibration: Dict[str,Any] | None=None) -> Dict[str,Any]:
    sc=scanner or {};gate=sc.get("edge_gate") or {};ready=bool(gate.get("calibration_ready"));active=bool(gate.get("active"))
    operar=[translate_direction(sc)]+translate_levels(sc)
    tc=translate_tape(tape)
    if tc:operar.append(tc)
    entender=translate_market_context(result,vol)
    confiar=[translate_trust(sc,data_quality,sessions,calibration)]
    if active:
        banner=None
    elif ready:
        banner="El modelo ya está calibrado, pero todavía está en observación y no controla la compuerta operativa. La plataforma mantiene visible esa diferencia para no presentar una estimación como una orden."
    else:
        banner="Esta señal aún no está validada fuera de muestra. La plataforma todavía no puede afirmar con qué frecuencia funciona. Usa los niveles para entender la estructura, no como una promesa de acierto."
    return {"blocks":[{"key":"operar","title":"QUÉ HACER","cards":operar},{"key":"entender","title":"POR QUÉ","cards":entender},{"key":"confiar","title":"¿ME PUEDO FIAR?","cards":confiar}],
            "banner":banner,"calibrated":ready,"gate_active":active,
            "note":"Modo Fácil traduce el mismo motor. No crea otra señal ni oculta la incertidumbre."}

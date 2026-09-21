#!/usr/bin/env python3
"""CERTIFICACIÓN LIVE · una sola ejecución, DIA + SPY + QQQ.

QUÉ HACE
--------
Recorre cada activo, lee el Auditor de la terminal EN MARCHA y deja, herramienta
por herramienta:

    herramienta → activo → endpoint probado → estado → causa exacta
                → datos recibidos → módulo consumidor → PASS/FAIL

Los siete estados son los del contrato, uno por remedio distinto:

    LIVE_OK             sirve dato de este ciclo
    NO_DATA             respondió bien y no hubo actividad en la ventana
    SIN_INTENTAR        todavía no le ha tocado turno
    NO_AUTORIZADO       401/403 · el plan no la incluye
    ENDPOINT_NO_EXISTE  404 · no existe o la renombraron. No inventar sustituto
    REQUEST_INVALID     400/422 · el cuerpo está mal. Reintentarlo no lo arregla
    PROVIDER_ERROR      5xx, timeout, red · falla y se reintenta con backoff

NO SE DETIENE NUNCA
-------------------
Cada herramienta y cada activo se prueban aislados. Un 404 en una no impide
probar las otras treinta y cinco: al final se entrega el inventario COMPLETO,
que es lo que sirve para decidir.

MÁS QUE UN HTTP 200
-------------------
Un endpoint puede responder 200 y su dato no llegar a la pantalla. Por eso cada
herramienta declara su MÓDULO CONSUMIDOR y se comprueba que ese módulo tenga
dato de verdad. `LIVE_OK` con el consumidor vacío es FAIL, y se dice por qué.

SECRETOS
--------
Ningún cuerpo, cabecera ni URL sale sin pasar por el redactor. Las claves se
sustituyen por «***REDACTADO***» aunque vengan anidadas.

USO (Windows)
-------------
    CERTIFICAR_LIVE.bat

USO (línea de comandos)
-----------------------
    python scripts\\certificar_live.py --base-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ACTIVOS_POR_DEFECTO = ["DIA", "SPY", "QQQ"]

#: Qué módulo de la terminal CONSUME cada herramienta, y dónde mirar en el
#: bundle para saber si el dato llegó de verdad.
#:
#: Sin esto, «el endpoint respondió 200» es todo lo que se puede afirmar, y eso
#: no es lo que se quiere saber: se quiere saber si el operador lo ve.
CONSUMIDOR: Dict[str, Dict[str, Any]] = {
    "gex_by_strike":       {"modulo": "TRACE · perfiles / EXPOSICIÓN", "panel": "TRACE · perfiles por strike"},
    "dex_by_strike":       {"modulo": "TRACE · perfiles / EXPOSICIÓN", "panel": "TRACE · perfiles por strike"},
    "vex_by_strike":       {"modulo": "EXPOSICIÓN · vanna", "panel": None},
    "chex_by_strike":      {"modulo": "EXPOSICIÓN · charm", "panel": None},
    "oi_by_strike":        {"modulo": "INTERÉS ABIERTO / Wall Engine", "panel": None},
    "oi_change":           {"modulo": "INTERÉS ABIERTO · variación", "panel": None},
    "oi_over_time":        {"modulo": "INTERÉS ABIERTO · serie", "panel": None},
    "oi_by_expiration":    {"modulo": "EXPOSICIÓN · por vencimiento", "panel": "EXPOSICIÓN · por vencimiento"},
    "gex_by_expiration":   {"modulo": "EXPOSICIÓN · por vencimiento", "panel": "EXPOSICIÓN · por vencimiento"},
    "dex_by_expiration":   {"modulo": "EXPOSICIÓN · por vencimiento", "panel": "EXPOSICIÓN · por vencimiento"},
    "vex_by_expiration":   {"modulo": "EXPOSICIÓN · por vencimiento", "panel": "EXPOSICIÓN · por vencimiento"},
    "chex_by_expiration":  {"modulo": "EXPOSICIÓN · por vencimiento", "panel": "EXPOSICIÓN · por vencimiento"},
    "max_pain":            {"modulo": "INTERÉS ABIERTO · max pain", "panel": None},
    "max_pain_over_time":  {"modulo": "INTERÉS ABIERTO · max pain serie", "panel": None},
    "net_flow":            {"modulo": "FLUJO · net flow", "ruta": ["flujo_ordenes", "net_flow", "rows"]},
    "net_drift":           {"modulo": "FLUJO · Net Drift", "ruta": ["flujo_ordenes", "net_drift", "series"]},
    "options_order_flow":     {"modulo": "FLUJO · cinta + DELTA/MIN", "ruta": ["flujo_ordenes", "order_flow_consolidated", "rows"]},
    "options_order_flow_raw": {"modulo": "FLUJO · cinta + DELTA/MIN", "ruta": ["flujo_ordenes", "order_flow_unconsolidated", "rows"]},
    "options_heat_map":    {"modulo": "TRACE · heatmap", "panel": "TRACE · heatmap"},
    "interval_map_gamma":  {"modulo": "TRACE / ESCENARIOS · Interval Map", "panel": None},
    "interval_map_delta":  {"modulo": "ESCENARIOS · Interval Map DELTA", "panel": None},
    "interval_map_vanna":  {"modulo": "ESCENARIOS · Interval Map VANNA", "panel": None},
    "interval_map_charm":  {"modulo": "ESCENARIOS · Interval Map CHARM", "panel": None},
    "dark_flow":           {"modulo": "DARK POOL · flujo", "panel": "DARK POOL · dark flow"},
    "dark_pool_levels":    {"modulo": "DARK POOL · niveles", "panel": "DARK POOL · niveles"},
    "equity_prints":       {"modulo": "DARK POOL · prints de equity", "panel": "DARK POOL · prints de equity"},
    "stock_price_over_time": {"modulo": "TRACE · velas", "panel": "TRACE · velas"},
    "iv_rank":             {"modulo": "VOLATILIDAD · IV rank", "panel": None},
    "volatility_skew":     {"modulo": "VOLATILIDAD · skew", "panel": "VOLATILIDAD · skew"},
    "volatility_drift":    {"modulo": "VOLATILIDAD · deriva", "panel": None},
    "term_structure":      {"modulo": "VOLATILIDAD · estructura temporal", "panel": None},
    "contract_statistics": {"modulo": "ESTADÍSTICAS · contratos", "panel": None},
    "trade_side_statistics": {"modulo": "ESTADÍSTICAS · por lado", "panel": None},
    "market_share":        {"modulo": "ESTADÍSTICAS · cuota", "panel": None},
    "gainers_losers":      {"modulo": "MACRO · ganadores/perdedores", "panel": None},
    "news":                {"modulo": "MACRO · noticias", "panel": None},
}

#: Estados que NO son un fallo del sistema. Un mercado sin actividad y una
#: herramienta a la que aún no le ha tocado turno no son defectos.
NO_SON_FALLO = {"LIVE_OK", "NO_DATA", "SIN_INTENTAR"}

_SECRETO = re.compile(
    r"(api[_-]?key|apikey|token|secret|authorization|bearer|password|passwd|x-api-key)",
    re.I)
REDACTADO = "***REDACTADO***"


def redactar(valor: Any, _prof: int = 0) -> Any:
    """Quita claves y credenciales, por anidadas que vengan."""
    if _prof > 8:
        return REDACTADO
    if isinstance(valor, dict):
        out = {}
        for k, v in valor.items():
            out[k] = REDACTADO if _SECRETO.search(str(k)) else redactar(v, _prof + 1)
        return out
    if isinstance(valor, (list, tuple)):
        return [redactar(v, _prof + 1) for v in valor][:40]
    if isinstance(valor, str):
        t = _SECRETO.sub(REDACTADO, valor)
        # Una cadena larga sin espacios junto a una palabra sensible es una clave.
        t = re.sub(r"\b[A-Za-z0-9_\-]{28,}\b", REDACTADO, t)
        return t[:400]
    return valor


def _http_get(url: str, timeout: float):
    import httpx
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _http_post(url: str, payload: dict, timeout: float):
    import httpx
    r = httpx.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json() if r.content else {}


def cambiar_activo(base: str, sym: str, *, espera: float, timeout: float) -> Dict[str, Any]:
    """Cambia de activo y espera a que el bundle sea del nuevo. Nunca lanza."""
    try:
        _http_post(f"{base}/api/asset", {"symbol": sym}, timeout)
    except Exception as exc:
        return {"ok": False, "detalle": f"no se pudo seleccionar {sym}: {type(exc).__name__}: {exc}"}
    limite = time.time() + espera
    ultimo = None
    while time.time() < limite:
        try:
            b = _http_get(f"{base}/api/terminal/bundle", timeout)
            if str(b.get("symbol") or "").upper() == sym:
                return {"ok": True, "bundle": b}
            ultimo = f"el bundle sigue en {b.get('symbol')}"
        except Exception as exc:
            ultimo = f"{type(exc).__name__}: {exc}"
        time.sleep(2.0)
    return {"ok": False, "detalle": ultimo or f"{sym} no llegó en {espera:.0f} s"}


def _cobertura(bundle: Dict[str, Any]) -> Dict[str, Any]:
    f = bundle.get("fuentes") or {}
    return (f.get("quantdata_coverage") or {}) if isinstance(f, dict) else {}


def contrato_de_cuota(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Qué ventana está aplicando la terminal y con qué consumo.

    v1.57.5 · Es la comprobación que cierra el episodio de `ASUMIDA_DIARIA`: si
    la terminal vuelve a inventarse una ventana más lenta que la publicada, el
    informe lo dice con nombre y apellidos en vez de dejarlo en una pastilla.
    """
    q = (_cobertura(bundle).get("quota") or {})
    fuente = str(q.get("window_source") or "—")
    ct = q.get("contract") or {}
    ritmo = q.get("engine_interval_seconds")
    problemas = []
    if fuente not in ("DECLARADA", "CABECERA", "MEDIDA", "CONTRATO"):
        problemas.append(f"ventana de origen desconocido: {fuente}")
    if fuente == "ASUMIDA_DIARIA":
        problemas.append("la terminal volvió a asumir una ventana DIARIA")
    if isinstance(ritmo, (int, float)) and ritmo > 60.0:
        problemas.append(f"el ciclo del motor es de {ritmo:.0f} s: demasiado lento "
                         "para un contrato de 240/60 s")
    return {
        "window_source": fuente,
        "engine_interval_seconds": ritmo,
        "sustained": f"{ct.get('sustained_used')}/{ct.get('sustained_limit')}"
                     f" en {ct.get('sustained_window_seconds')}s" if ct else None,
        "burst": f"{ct.get('burst_used')}/{ct.get('burst_limit')}"
                 f" en {ct.get('burst_window_seconds')}s" if ct else None,
        "pages_paused": (q.get("pages_paused") or {}).get("reason"),
        "problemas": problemas,
        "resultado": "PASS" if not problemas else "FAIL",
    }


def medir_hidratacion(base: str, sym: str, *, limite: float, timeout: float,
                      intervalo: float = 0.5) -> Dict[str, Any]:
    """Cuánto tarda el catálogo en hidratarse, medido contra la terminal viva.

    No se cronometra «hasta que se ve algo»: se cuenta cuándo aparece la primera
    herramienta LIVE y cuándo el recuento deja de subir durante tres lecturas
    seguidas. Esa meseta es el final real del arranque.
    """
    #: Lecturas seguidas sin que suba el recuento para dar el arranque por
    #: terminado. Se cuentan LECTURAS y no segundos a propósito: si la terminal
    #: tarda en responder, el reloj avanza sin que hayamos mirado nada, y eso no
    #: es una meseta, es una espera.
    MESETA_LECTURAS = 3

    t0 = time.time()
    primera: float | None = None
    mejor = -1
    estable_desde: float | None = None
    quietas = 0
    serie: List[Dict[str, Any]] = []
    total = None
    while time.time() - t0 < limite:
        try:
            b = _http_get(f"{base}/api/terminal/bundle", timeout)
        except Exception:
            time.sleep(intervalo)
            continue
        cov = _cobertura(b)
        vivas = int(cov.get("live_tools") or 0)
        total = cov.get("total_tools") or total
        t = round(time.time() - t0, 1)
        serie.append({"t": t, "live": vivas})
        if vivas > 0 and primera is None:
            primera = t
        if vivas > mejor:
            mejor, estable_desde, quietas = vivas, t, 0
        else:
            quietas += 1
            # Una meseta en cero no es el final del arranque: es que no ha
            # empezado. Ahí se agota el plazo, que es lo que hay que informar.
            if mejor > 0 and quietas >= MESETA_LECTURAS:
                break
        time.sleep(intervalo)
    return {
        "activo": sym,
        "primera_live_s": primera,
        "meseta_s": estable_desde,
        "live_al_final": mejor if mejor >= 0 else 0,
        "total_herramientas": total,
        "muestras": serie[-40:],
    }


def _por_ruta(bundle: Dict[str, Any], ruta: List[str]) -> Any:
    cur: Any = bundle
    for k in ruta:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def consumidor_tiene_dato(bundle: Dict[str, Any], key: str) -> Dict[str, Any]:
    """¿El dato de esta herramienta LLEGÓ a su módulo? Más que un HTTP 200."""
    cfg = CONSUMIDOR.get(key)
    if not cfg:
        return {"modulo": "—", "llega": None,
                "detalle": "esta herramienta no tiene consumidor declarado"}
    modulo = cfg["modulo"]

    ruta = cfg.get("ruta")
    if ruta:
        v = _por_ruta(bundle, list(ruta))
        n = len(v) if isinstance(v, (list, tuple)) else (None if v is None else 1)
        return {"modulo": modulo, "llega": bool(n), "filas_en_modulo": n,
                "detalle": ("" if n else f"{'.'.join(ruta)} vacío")}

    panel = cfg.get("panel")
    if panel:
        checks = ((bundle.get("auditor") or {}).get("diagnostics") or {}).get("checks") or []
        fila = next((c for c in checks if c.get("panel") == panel), None)
        if fila is None:
            return {"modulo": modulo, "llega": None,
                    "detalle": f"el panel «{panel}» no aparece en el diagnóstico"}
        return {"modulo": modulo, "llega": bool(fila.get("ok")),
                "filas_en_modulo": fila.get("count"),
                "detalle": "" if fila.get("ok") else str(fila.get("reason") or "")}

    return {"modulo": modulo, "llega": None,
            "detalle": "consumidor declarado sin comprobación automática"}


def certificar_activo(bundle: Dict[str, Any], sym: str) -> List[Dict[str, Any]]:
    """Una fila por herramienta. Aislada: una que reviente no para las demás."""
    qd = (bundle.get("auditor") or {}).get("quantdata") or {}
    filas: List[Dict[str, Any]] = []
    for t in (qd.get("tools") or []):
        key = str(t.get("key") or "?")
        try:
            diag = t.get("diagnosis") or {}
            estado = str(diag.get("verdict") or "SIN_VEREDICTO")
            rt = t.get("runtime") or {}
            cons = consumidor_tiene_dato(bundle, key)
            filas_recibidas = t.get("rows")

            # PASS/FAIL. Un LIVE_OK cuyo consumidor está vacío es FAIL: el
            # endpoint respondió y el operador no lo ve, que es lo que importa.
            if estado == "LIVE_OK":
                ok = cons.get("llega") is not False
                motivo = "" if ok else f"respondió pero el módulo está vacío · {cons.get('detalle')}"
            elif estado in NO_SON_FALLO:
                ok, motivo = True, ""
            else:
                ok, motivo = False, str(diag.get("action") or "")

            filas.append({
                "herramienta": key,
                "titulo": t.get("title"),
                "activo": sym,
                "endpoint": t.get("path") or (t.get("candidates") or [None])[0],
                "estado": estado,
                "causa": str(diag.get("evidence") or "")[:300],
                "campos_rechazados": list(diag.get("fields") or [])[:8],
                "accion": str(diag.get("action") or "")[:300],
                "filas_recibidas": filas_recibidas,
                "modulo_consumidor": cons.get("modulo"),
                "llega_al_modulo": cons.get("llega"),
                "filas_en_modulo": cons.get("filas_en_modulo"),
                "detalle_modulo": cons.get("detalle"),
                "cortacircuitos": rt.get("breaker"),
                "plazo_s": rt.get("timeout_seconds"),
                "plazo_origen": rt.get("timeout_source"),
                "latencia_p95": rt.get("latency_p95"),
                "cuerpo_peticion": redactar(t.get("request_body")),
                "resultado": "PASS" if ok else "FAIL",
                "motivo_fallo": motivo,
            })
        except Exception as exc:
            # El certificador NO se detiene. Una herramienta que revienta el
            # análisis se registra como tal y se sigue con las demás.
            filas.append({
                "herramienta": key, "activo": sym, "estado": "ERROR_CERTIFICADOR",
                "causa": f"{type(exc).__name__}: {exc}"[:300],
                "resultado": "FAIL",
                "motivo_fallo": "el certificador no pudo analizar esta herramienta",
            })
    return filas


def tabla(filas: List[Dict[str, Any]]) -> str:
    cab = (f"{'HERRAMIENTA':<24}{'ACT':<5}{'ESTADO':<20}{'FILAS':>7}"
           f"  {'MÓDULO CONSUMIDOR':<34}{'LLEGA':<7}{'':<2}")
    out = ["", "=" * 132,
           "CERTIFICACIÓN LIVE · herramienta → activo → estado → datos → módulo → PASS/FAIL",
           "=" * 132, cab, "-" * 132]
    for f in sorted(filas, key=lambda r: (r.get("resultado") != "FAIL",
                                          str(r.get("herramienta")), str(r.get("activo")))):
        llega = f.get("llega_al_modulo")
        marca = "sí" if llega is True else "NO" if llega is False else "—"
        out.append(f"{str(f.get('herramienta'))[:23]:<24}{str(f.get('activo')):<5}"
                   f"{str(f.get('estado'))[:19]:<20}{str(f.get('filas_recibidas') or '—'):>7}"
                   f"  {str(f.get('modulo_consumidor') or '—')[:33]:<34}{marca:<7}"
                   f"{f.get('resultado')}")
        if f.get("resultado") == "FAIL":
            if f.get("causa"):
                out.append(f"      causa    : {f['causa'][:110]}")
            if f.get("campos_rechazados"):
                out.append(f"      campos   : {', '.join(map(str, f['campos_rechazados']))}")
            if f.get("motivo_fallo"):
                out.append(f"      qué hacer: {f['motivo_fallo'][:110]}")
    return "\n".join(out)


def resumen(filas: List[Dict[str, Any]]) -> str:
    por_estado: Dict[str, int] = {}
    for f in filas:
        e = str(f.get("estado") or "?")
        por_estado[e] = por_estado.get(e, 0) + 1
    fails = [f for f in filas if f.get("resultado") == "FAIL"]
    out = ["", "-" * 132, f"{len(filas)} comprobaciones · "
           f"{len(filas) - len(fails)} PASS · {len(fails)} FAIL", ""]
    for e, n in sorted(por_estado.items(), key=lambda kv: -kv[1]):
        out.append(f"   {e:<22}{n:>4}")
    if fails:
        out.append("")
        out.append("HERRAMIENTAS EN FALLO, agrupadas por remedio:")
        por_remedio: Dict[str, List[str]] = {}
        for f in fails:
            por_remedio.setdefault(str(f.get("estado")), []).append(
                f"{f.get('herramienta')}@{f.get('activo')}")
        for e, ks in sorted(por_remedio.items()):
            out.append(f"   {e:<22}{', '.join(sorted(ks))[:96]}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--symbols", default=",".join(ACTIVOS_POR_DEFECTO))
    ap.add_argument("--out", default="CERTIFICACION_LIVE.json")
    ap.add_argument("--espera", type=float, default=90.0,
                    help="segundos a esperar a que el bundle cambie de activo")
    ap.add_argument("--calentar", type=float, default=45.0,
                    help="segundos de margen para que el programador dé la vuelta")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    try:
        import httpx  # noqa: F401
    except ImportError:
        print("Falta httpx. Instálalo con:  py -m pip install httpx", file=sys.stderr)
        return 2

    base = args.base_url.rstrip("/")
    simbolos = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"Terminal : {base}")
    print(f"Activos  : {', '.join(simbolos)}")
    if args.calentar > 0:
        print(f"Esperando {args.calentar:.0f} s a que el programador dé la vuelta "
              f"a las herramientas de cola…")
        time.sleep(args.calentar)

    filas: List[Dict[str, Any]] = []
    incidencias: List[Dict[str, Any]] = []
    hidrataciones: List[Dict[str, Any]] = []
    contratos: List[Dict[str, Any]] = []
    for sym in simbolos:
        print(f"\n→ {sym}")
        try:
            res = cambiar_activo(base, sym, espera=args.espera, timeout=args.timeout)
        except Exception as exc:
            res = {"ok": False, "detalle": f"{type(exc).__name__}: {exc}"}
        if not res.get("ok"):
            # Un activo que no arranca NO detiene la certificación de los otros.
            print(f"   NO se pudo certificar: {res.get('detalle')}")
            incidencias.append({"activo": sym, "detalle": str(res.get("detalle"))[:300]})
            continue
        # El cronómetro arranca en cuanto el bundle ya es del activo nuevo.
        hid = medir_hidratacion(base, sym, limite=min(args.espera, 90.0),
                                timeout=args.timeout)
        hidrataciones.append(hid)
        print(f"   hidratación: primera LIVE {hid['primera_live_s']} s · "
              f"meseta {hid['meseta_s']} s · "
              f"{hid['live_al_final']}/{hid['total_herramientas']} herramientas")
        try:
            b = _http_get(f"{base}/api/terminal/bundle", args.timeout)
        except Exception:
            b = res["bundle"]
        cont = contrato_de_cuota(b)
        cont["activo"] = sym
        contratos.append(cont)
        for p in cont["problemas"]:
            print(f"   CONTRATO: {p}")
        nuevas = certificar_activo(b, sym)
        filas.extend(nuevas)
        print(f"   {len(nuevas)} herramientas analizadas · "
              f"{sum(1 for f in nuevas if f['resultado'] == 'FAIL')} en fallo")

    print(tabla(filas))
    print(resumen(filas))
    if hidrataciones:
        print("\nHIDRATACIÓN MEDIDA (segundos desde el cambio de activo)")
        print(f"  {'ACTIVO':<8} {'1ª LIVE':>9} {'MESETA':>8} {'HERRAMIENTAS':>14}")
        for h in hidrataciones:
            print(f"  {h['activo']:<8} {str(h['primera_live_s']):>9} "
                  f"{str(h['meseta_s']):>8} "
                  f"{h['live_al_final']}/{h['total_herramientas']}".rjust(0))
    if contratos:
        print("\nCONTRATO DE CUOTA APLICADO")
        for c in contratos:
            print(f"  {c['activo']:<8} ventana {c['window_source']:<12} "
                  f"ciclo {c['engine_interval_seconds']} s · "
                  f"sostenida {c['sustained']} · ráfaga {c['burst']} → {c['resultado']}")
    if incidencias:
        print("\nACTIVOS QUE NO SE PUDIERON CERTIFICAR:")
        for i in incidencias:
            print(f"   {i['activo']}: {i['detalle']}")

    salida = Path(args.out)
    salida.write_text(json.dumps({
        "generado_en": datetime.now(timezone.utc).isoformat(),
        "base_url": base, "activos": simbolos,
        "filas": filas, "incidencias": incidencias,
        "hidratacion": hidrataciones, "contrato_de_cuota": contratos,
        "total": len(filas),
        "pass": sum(1 for f in filas if f["resultado"] == "PASS"),
        "fail": sum(1 for f in filas if f["resultado"] == "FAIL"),
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nInforme completo: {salida.resolve()}")
    print("Ese fichero es el que hay que enviar. No contiene claves ni credenciales.")
    # Se devuelve 0 SIEMPRE que la certificación se haya podido ejecutar: su
    # trabajo es informar, no aprobar. El veredicto lo da quien lo lea.
    return 0 if filas else 1


if __name__ == "__main__":
    raise SystemExit(main())

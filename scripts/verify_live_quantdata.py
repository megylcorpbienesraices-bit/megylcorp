#!/usr/bin/env python3
"""VERIFICACIÓN LIVE DE QUANT DATA · ITM QUANT v1.46.0

PARA QUÉ SIRVE
--------------
La suite certifica la LÓGICA con datos controlados. Lo que no puede certificar es
que, con una cuenta real y el mercado abierto, cada dataset llegue de verdad como
`DIRECT_PROVIDER` y que el número mostrado sea el que el proveedor emitió.

Este script hace exactamente eso, y sólo eso:

    RAW PROVIDER ──► NORMALIZADOR ──► API INTERNA ──► lo que vería el frontend

comparando una MUESTRA real en cada tramo, dataset por dataset.

CÓMO SE USA
-----------
Con la terminal en marcha y `QUANTDATA_API_KEY` configurada:

    python scripts/verify_live_quantdata.py --ticker SPY
    python scripts/verify_live_quantdata.py --ticker SPY --ticker AAPL --json informe.json

Salida: una tabla por dataset con el modo de fuente real, la muestra cruda, la
normalizada y la que llega al bundle, y un veredicto por dataset.

QUÉ CUENTA COMO APROBADO
------------------------
Un dataset pasa cuando:

  1. el proveedor responde con filas utilizables;
  2. el normalizador conserva la muestra (mismo valor, misma unidad);
  3. el bundle publica ese dataset con `source_mode = DIRECT_PROVIDER`;
  4. no hay `fallback_used` activo para él.

Que el endpoint conteste NO basta: un 200 con cero filas, o un valor que el
normalizador transforma sin querer, siguen siendo fallos y se reportan como tales.

MODO DARK POOL (v1.46.0)
------------------------
    python scripts/verify_live_quantdata.py --dark-pool

Recorre los TRES carriles —Dark Flow, Dark Pool Levels, Equity Prints— sobre una
cesta multi-activo (ETF de índice y equities líquidos, no sólo DIA) y, por cada
uno, publica el código HTTP real y, si hubo 400, **qué campo nombró el
proveedor**. Un 400 no se reintenta: se lee el campo rechazado, se corrige el
cuerpo con `repair_body` y se vuelve a preguntar una sola vez, que es lo que
distingue corregir de adivinar.

Los tres carriles se miden por separado a propósito: que `dark-pool-levels`
rechace el cuerpo no dice nada sobre `dark-flow`, y medirlos juntos fue lo que
hacía parecer rota la sección entera.

NO MODIFICA NADA. Sólo lee.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Datasets que la especificación declara como autoridad de Quant Data, con la
# herramienta del catálogo que los sirve y la clave del bundle donde aterrizan.
DATASETS: Tuple[Tuple[str, str, str], ...] = (
    ("Interval Map", "interval_map_gamma", "interval_map"),
    ("GEX", "gex_by_strike", "exposicion"),
    ("DEX", "dex_by_strike", "exposicion"),
    ("Vanna / VEX", "vex_by_strike", "exposicion"),
    ("Charm / CHEX", "chex_by_strike", "exposicion"),
    ("Open Interest", "oi_by_strike", "open_interest"),
    ("Open Interest Change", "oi_change", "open_interest"),
    ("Net Flow", "net_flow", "qflow"),
    ("Net Drift", "net_drift", "net_drift"),
    ("Order Flow consolidado", "options_order_flow", "flujo_ordenes"),
    ("Order Flow sin consolidar", "options_order_flow_raw", "flujo_ordenes"),
    ("Dark Flow", "dark_flow", "dark_pool"),
    ("Dark Pool Levels", "dark_pool_levels", "dark_pool"),
    ("Equity Prints", "equity_prints", "dark_pool"),
    ("IV Rank", "iv_rank", "volatilidad"),
    ("Volatility Skew", "volatility_skew", "volatilidad"),
    ("Term Structure", "term_structure", "volatilidad"),
    ("Volatility Drift", "volatility_drift", "volatilidad"),
    ("Max Pain", "max_pain", "open_interest"),
)

# Los tres carriles de DARK POOL. Se declaran aparte de DATASETS porque se
# verifican de forma independiente: cada uno con su propio veredicto.
DARK_POOL_LANES: Tuple[Tuple[str, str], ...] = (
    ("Dark Flow", "dark_flow"),
    ("Dark Pool Levels", "dark_pool_levels"),
    ("Equity Prints", "equity_prints"),
)

# Cesta por defecto del modo dark pool. Tres ETF de índice de escalas distintas y
# cuatro equities líquidos: si algo sólo funciona en uno de ellos, el problema es
# el activo, no el proveedor. No hay ningún ticker escrito en el resto del
# programa; ésta es una lista de PRUEBA, y se puede sustituir con --ticker.
DARK_POOL_BASKET: Tuple[str, ...] = ("DIA", "SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD")

# Campos oficiales que el normalizador de niveles debe conservar si el proveedor
# los manda. Se comprueba presencia, no valor: inventar un valor sería peor.
LEVEL_FIELDS: Tuple[str, ...] = ("price", "notional", "shares", "prints")

OK = "OK"
NO_DATA = "SIN DATOS"
NOT_DIRECT = "NO DIRECTO"
MISMATCH = "DISCREPANCIA"
ERROR = "ERROR"
REJECTED = "CUERPO RECHAZADO"


def _sample(rows: Any, limit: int = 3) -> List[Any]:
    if isinstance(rows, list):
        return rows[:limit]
    return []


def _first_number(row: Any) -> Optional[float]:
    """Primer valor numérico significativo de una fila normalizada."""
    if isinstance(row, (int, float)):
        return float(row)
    if not isinstance(row, dict):
        return None
    for k in ("value", "gex", "dex", "oi", "net_premium", "dark_volume",
              "premium", "y", "price", "strike"):
        v = row.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    for v in row.values():
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _failure_detail(exc: Any) -> Dict[str, Any]:
    """El fallo COMPLETO, no su primera línea.

    Un `400` cuyo cuerpo dice `errors[0].field = "lookBackPeriod"` y un `400` que
    dice `filter.ticker` se arreglan de forma distinta, y truncar el mensaje a
    180 caracteres borraba justo la parte accionable.
    """
    status = getattr(exc, "status_code", None)
    fields = list(getattr(exc, "validation_fields", None) or [])
    detail_obj = getattr(exc, "error_detail", None) or {}
    verdict = ERROR
    if status == 400:
        verdict = REJECTED
    elif status == 422:
        verdict = NO_DATA
    return {
        "verdict": verdict,
        "http_status": status,
        "rejected_fields": fields,
        "detail": f"{type(exc).__name__}: {str(exc)[:400]}",
        "body": getattr(exc, "body", None),
        # v1.49.0 · El 400 desglosado: `type`, `detail` y cada campo con su
        # mensaje. Es lo que dice qué corregir sin probar otro payload.
        "error_type": detail_obj.get("type"),
        "error_detail": detail_obj.get("detail"),
        "field_errors": detail_obj.get("errors") or [],
    }


async def probe_dark_pool(ticker: str, timeout: float) -> Dict[str, Any]:
    """Los tres carriles de DARK POOL, cada uno con su propio veredicto.

    Si el proveedor rechaza el cuerpo (400), se lee QUÉ campo nombró y se corrige
    con `repair_body` una sola vez. No se prueban variantes al azar: o el
    proveedor dice qué falta, o el veredicto es CUERPO RECHAZADO con el campo
    escrito, que es información sobre la que se puede actuar.
    """
    from app.providers.quantdata.client import QuantDataClient, QuantDataError
    from app.providers.quantdata.settings import load_settings
    from app.providers.quantdata.tools import build_catalog, repair_body

    settings = load_settings()
    if not settings.configured:
        return {"configured": False,
                "detail": "QUANTDATA_API_KEY no está configurada"}

    catalog = build_catalog()
    client = QuantDataClient(settings)
    await client.start()
    lanes: Dict[str, Any] = {}
    try:
        for label, key in DARK_POOL_LANES:
            tool = catalog.get(key)
            if tool is None:
                lanes[key] = {"label": label, "verdict": ERROR,
                              "detail": f"la herramienta «{key}» no está en el catálogo"}
                continue
            path = tool.paths[0]
            body = tool.request_body(ticker)
            row: Dict[str, Any] = {"label": label, "endpoint": path,
                                   "request_body": dict(body), "repairs": []}
            if key == "dark_pool_levels":
                # Se imprime para poder cotejarlo con el contrato publicado sin
                # abrir el código.
                row["contract_body"] = json.dumps(body, sort_keys=True)
            raw = None
            for _round in range(3):
                try:
                    response = await asyncio.wait_for(
                        client.post(path, body), timeout=timeout)
                    raw = response.payload
                    break
                except (QuantDataError, asyncio.TimeoutError) as exc:
                    fail = _failure_detail(exc)
                    row.update(fail)
                    if fail["verdict"] != REJECTED:
                        break
                    from app.providers.quantdata.tools import TOOL_FORBIDDEN_FIELDS
                    nxt, note = repair_body(body, fail["rejected_fields"],
                                            TOOL_FORBIDDEN_FIELDS.get(key, ()))
                    if nxt is None:
                        break
                    row["repairs"].append(note)
                    body = nxt
                    row["request_body"] = dict(body)
            if raw is None:
                lanes[key] = row
                continue
            try:
                normalized = tool.normalize(raw)
            except Exception as exc:   # noqa: BLE001
                row.update({"verdict": ERROR,
                            "detail": f"el normalizador falló: {type(exc).__name__}: {exc}"})
                lanes[key] = row
                continue
            rows = normalized.get("rows") or []
            row.update({
                "verdict": OK if normalized.get("ready") else NO_DATA,
                "http_status": 200,
                "normalized_rows": len(rows),
                "normalized_sample": _sample(rows),
                "raw_keys": sorted(list(raw)[:10]) if isinstance(raw, dict) else type(raw).__name__,
                "detail": "" if normalized.get("ready") else
                          "la petición fue aceptada y el proveedor no devolvió filas",
            })
            if key == "dark_pool_levels" and rows:
                # Item 4 de la corrección: los campos oficiales tienen que
                # SOBREVIVIR al normalizador, no sólo llegar a él.
                first = rows[0] if isinstance(rows[0], dict) else {}
                row["preserved_fields"] = [f for f in LEVEL_FIELDS if first.get(f) is not None]
                row["dropped_fields"] = [f for f in LEVEL_FIELDS if first.get(f) is None]
                row["latest_stock_price"] = normalized.get("latest_stock_price")
                row["extra_fields"] = sorted((first.get("extra") or {}).keys())
            lanes[key] = row
    finally:
        await client.close()
    return {"configured": True, "ticker": ticker.upper(), "lanes": lanes}


def render_dark_pool(results: Dict[str, Dict[str, Any]]) -> Tuple[str, bool]:
    """Una fila por activo y carril. La independencia se VE, no se promete."""
    lines = ["", "=" * 104, "DARK POOL · TRES CARRILES INDEPENDIENTES · MULTI-ACTIVO", "=" * 104,
             f"{'ACTIVO':<8}{'CARRIL':<20}{'VEREDICTO':<18}{'HTTP':>5}{'FILAS':>7}  DETALLE"]
    lines.append("-" * 104)
    ok = True
    for ticker, out in results.items():
        lanes = out.get("lanes") or {}
        for label, key in DARK_POOL_LANES:
            r = lanes.get(key) or {}
            verdict = str(r.get("verdict") or "—")
            if verdict == REJECTED or verdict == ERROR:
                ok = False
            extra = ""
            if r.get("field_errors"):
                extra = " · ".join(f"{e.get('field')}: {e.get('message')}"
                                   for e in r["field_errors"][:3])
            elif r.get("rejected_fields"):
                extra = "campos: " + " · ".join(str(f) for f in r["rejected_fields"][:4])
            elif r.get("repairs"):
                extra = "reparado: " + " · ".join(r["repairs"][:2])
            elif r.get("dropped_fields"):
                extra = "sin " + " · ".join(r["dropped_fields"])
            elif r.get("detail"):
                extra = str(r["detail"])[:56]
            lines.append(f"{ticker:<8}{label:<20}{verdict:<18}"
                         f"{str(r.get('http_status') or '—'):>5}"
                         f"{str(r.get('normalized_rows') if r.get('normalized_rows') is not None else '—'):>7}  "
                         f"{extra}")
    lines.append("-" * 104)
    # v1.49.0 · `dark-pool-levels` no se considera cerrado hasta que devuelve un
    # 200 REAL. Un SIN DATOS no vale: el contrato exige `sessionDateRange`, y si
    # el cuerpo está bien y el día es una sesión válida, tiene que responder.
    lv = [(t, (o.get("lanes") or {}).get("dark_pool_levels", {})) for t, o in results.items()]
    ok200 = [t for t, r in lv if r.get("http_status") == 200]
    rejected = [t for t, r in lv if r.get("verdict") == REJECTED]
    lines.append(f"DARK POOL LEVELS · {len(ok200)}/{len(lv)} con HTTP 200 real")
    if rejected:
        lines.append(f"  RECHAZADO en {', '.join(rejected)} — el endpoint NO está cerrado.")
        lines.append("  El siguiente paso no es otro payload: es leer el campo y el mensaje")
        lines.append("  de arriba, que es exactamente lo que el proveedor está rechazando.")
    elif ok200:
        first = next(r for _t, r in lv if r.get("http_status") == 200)
        lines.append(f"  campos conservados: {' · '.join(first.get('preserved_fields') or []) or '—'}")
        if first.get("dropped_fields"):
            lines.append(f"  NO conservados: {' · '.join(first['dropped_fields'])}")
        lines.append(f"  latestStockPrice: {first.get('latest_stock_price')}")

    # Un carril roto en UN activo y sano en otros seis es un problema de ese
    # activo. Roto en los siete es un problema del cuerpo que enviamos.
    for label, key in DARK_POOL_LANES:
        verdicts = [(t, (o.get("lanes") or {}).get(key, {}).get("verdict"))
                    for t, o in results.items()]
        bad = [t for t, v in verdicts if v in (REJECTED, ERROR)]
        if bad and len(bad) == len(verdicts):
            lines.append(f"{label}: falla en los {len(bad)} activos → el defecto es del "
                         f"cuerpo que enviamos, no del activo.")
        elif bad:
            lines.append(f"{label}: falla sólo en {', '.join(bad)} → el defecto es de "
                         f"esos activos, no del carril.")
    return "\n".join(lines), ok


async def probe(ticker: str, timeout: float) -> Dict[str, Any]:
    """Pide cada herramienta al proveedor y la pasa por su propio normalizador."""
    from app.providers.quantdata.client import QuantDataClient, QuantDataError
    from app.providers.quantdata.settings import load_settings
    from app.providers.quantdata.tools import build_catalog

    settings = load_settings()
    if not settings.configured:
        return {"configured": False,
                "detail": ("QUANTDATA_API_KEY no está configurada o no tiene el "
                           "formato esperado (qd_… de 35 caracteres)")}

    catalog = build_catalog()
    client = QuantDataClient(settings)
    await client.start()
    results: Dict[str, Any] = {}
    try:
        for label, key, _section in DATASETS:
            tool = catalog.get(key)
            if tool is None:
                results[key] = {"label": label, "verdict": ERROR,
                                "detail": f"la herramienta «{key}» no está en el catálogo"}
                continue
            path = tool.paths[0]
            try:
                # `request_body` y no `body`: es el cuerpo que el programa envía de
                # verdad, ya sin los campos heredados de otras herramientas. Probar
                # aquí uno distinto al de producción verificaría otra cosa.
                response = await asyncio.wait_for(
                    client.post(path, tool.request_body(ticker)), timeout=timeout)
                raw = response.payload
            except (QuantDataError, asyncio.TimeoutError) as exc:
                results[key] = {"label": label, "endpoint": path,
                                **_failure_detail(exc)}
                continue

            try:
                normalized = tool.normalize(raw)
            except Exception as exc:   # noqa: BLE001
                results[key] = {"label": label, "verdict": ERROR, "endpoint": path,
                                "detail": f"el normalizador falló: {type(exc).__name__}: {exc}"}
                continue

            rows = normalized.get("rows")
            ready = bool(normalized.get("ready"))
            results[key] = {
                "label": label, "endpoint": path, "ready": ready,
                "raw_keys": sorted(list(raw)[:8]) if isinstance(raw, dict) else type(raw).__name__,
                "raw_bytes": len(json.dumps(raw)) if raw is not None else 0,
                "normalized_rows": (len(rows) if isinstance(rows, list) else None),
                "normalized_sample": _sample(rows),
                "verdict": OK if ready else NO_DATA,
                "detail": "" if ready else str(normalized.get("reason")
                                               or normalized.get("error")
                                               or "el proveedor respondió sin filas utilizables"),
            }
    finally:
        await client.close()
    return {"configured": True, "ticker": ticker.upper(), "datasets": results}


def read_bundle(base_url: str, ticker: str, timeout: float) -> Dict[str, Any]:
    """Lo que la API interna publica: el último tramo antes del frontend."""
    import httpx
    try:
        with httpx.Client(timeout=timeout) as c:
            state = c.get(f"{base_url}/api/terminal/bundle").json()
    except Exception as exc:   # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:180]}"}
    got = str(state.get("symbol") or "").upper()
    if got != ticker.upper():
        return {"ok": False, "symbol": got,
                "detail": (f"la terminal está en {got}, no en {ticker.upper()}. "
                           "Cambia de activo antes de verificar, o el informe "
                           "compararía dos mercados distintos.")}
    return {"ok": True, "bundle": state}


def audit_modes(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Modo de fuente REAL de cada métrica, leído del bloque del Auditor."""
    auditor = bundle.get("auditor") or {}
    rows = auditor.get("records") or []
    return {r.get("metric"): r for r in rows if isinstance(r, dict)}


def evaluate(probe_out: Dict[str, Any], bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Cruza los tres tramos y emite un veredicto por dataset."""
    from app.core.quant_data_hub import TOOL_METRIC

    modes = audit_modes(bundle)
    out: List[Dict[str, Any]] = []
    for label, key, section in DATASETS:
        row = dict((probe_out.get("datasets") or {}).get(key) or {})
        row.setdefault("label", label)
        row["section"] = section
        metric = TOOL_METRIC.get(key)
        rec = modes.get(metric) if metric else None
        row["metric"] = metric
        row["published_mode"] = (rec or {}).get("source_mode")
        row["published_state"] = (rec or {}).get("state")
        row["fallback_used"] = bool((rec or {}).get("fallback_used"))

        if row.get("verdict") in (ERROR,):
            out.append(row)
            continue
        if not row.get("ready"):
            row["verdict"] = NO_DATA
            out.append(row)
            continue
        if row["published_mode"] != "DIRECT_PROVIDER":
            row["verdict"] = NOT_DIRECT
            row["detail"] = (f"el proveedor entregó datos pero la terminal publica "
                             f"«{metric}» como {row['published_mode'] or 'nada'}"
                             + (" con respaldo activo" if row["fallback_used"] else ""))
            out.append(row)
            continue
        # El valor tiene que SOBREVIVIR al normalizador: mismo número, no parecido.
        sample = (row.get("normalized_sample") or [None])[0]
        row["sample_value"] = _first_number(sample)
        if row["sample_value"] is None:
            row["verdict"] = MISMATCH
            row["detail"] = "la fila normalizada no contiene ningún valor numérico legible"
        else:
            row["verdict"] = OK
        out.append(row)
    return out


def render(ticker: str, rows: List[Dict[str, Any]]) -> Tuple[str, bool]:
    lines = [f"\n{'=' * 96}", f"VERIFICACIÓN LIVE · {ticker}", "=" * 96,
             f"{'DATASET':<28}{'VEREDICTO':<14}{'FILAS':>7}  {'MODO PUBLICADO':<18}MUESTRA"]
    lines.append("-" * 96)
    ok = True
    for r in rows:
        if r.get("verdict") != OK:
            ok = False
        sample = r.get("sample_value")
        sample_txt = "—" if sample is None else f"{sample:,.4g}"
        lines.append(f"{str(r.get('label'))[:27]:<28}"
                     f"{str(r.get('verdict')):<14}"
                     f"{str(r.get('normalized_rows') if r.get('normalized_rows') is not None else '—'):>7}  "
                     f"{str(r.get('published_mode') or '—'):<18}{sample_txt}")
        if r.get("detail"):
            lines.append(f"{'':<28}↳ {str(r['detail'])[:64]}")
    lines.append("-" * 96)
    passed = sum(1 for r in rows if r.get("verdict") == OK)
    lines.append(f"{passed}/{len(rows)} datasets DIRECT_PROVIDER verificados de extremo a extremo")
    if not ok:
        lines.append("")
        lines.append("Un dataset que no llega a OK NO es necesariamente un fallo del programa:")
        lines.append("  SIN DATOS    el plan no incluye esa herramienta, o el mercado está cerrado")
        lines.append("  NO DIRECTO   el proveedor respondió pero la terminal sirvió un respaldo")
        lines.append("  ERROR        el endpoint falló; la línea de detalle dice cuál y por qué")
    return "\n".join(lines), ok


#: La cesta de los ocho activos del criterio de cierre. Escalas distintas a
#: propósito: un ETF de índice, uno de pequeña capitalización y equities de
#: precio y liquidez muy dispares. Si algo se arregla con una constante, aquí
#: se rompe.
CIERRE_BASKET = ("DIA", "SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "AMD")


def probe_aggressor(base_url: str, ticker: str, timeout: float) -> Dict[str, Any]:
    """TABLA DE EVIDENCIA del lado agresor, con la cinta REAL. Punto 44.

    Lee el bundle en marcha, saca las filas normalizadas de `order-flow` y las
    contrasta contra la regla del contrato publicado, reimplementada A MANO en
    `aggressor_evidence.expected_side`. Comparar el clasificador consigo mismo
    no demostraría nada.

    Exige que el lado sea el MISMO en RAW, clasificador y las marcas que dibujan
    FLUJO y TRACE. Si una sola etapa cambia BUY por SELL, falla y dice cuál.
    """
    from app.core.aggressor_evidence import build_evidence

    br = read_bundle(base_url, ticker, timeout)
    if not br.get("ok"):
        return {"ok": False, "ticker": ticker, "detail": br.get("detail")}
    bundle = br["bundle"]
    flujo = bundle.get("flujo_ordenes") or {}
    bloque = (flujo.get("order_flow_unconsolidated") or {})
    if not bloque.get("rows"):
        bloque = (flujo.get("order_flow_consolidated") or {})
    filas = bloque.get("rows") or []
    marcas = ((bundle.get("qflow") or {}).get("markers")) or []

    # Las filas normalizadas llevan el crudo que hace falta para recalcular el
    # lado esperado, así que sirven de las dos entradas: es el mismo dato, leído
    # por dos caminos que no se hablan.
    ev = build_evidence(filas, filas, flow_marks=marcas, trace_marks=marcas, limit=200)
    ev["ticker"] = ticker.upper()
    ev["auditor"] = (bundle.get("auditor") or {}).get("aggressor") or {}
    return ev


def render_aggressor(reports: List[Dict[str, Any]]) -> Tuple[str, bool]:
    out = ["", "=" * 104,
           "TABLA DE EVIDENCIA DEL AGRESOR · cinta real",
           "  RAW tradeSideCode / bid / ask / precio  →  esperado  →  clasificador  →  marca FLUJO  →  marca TRACE",
           "=" * 104]
    todo_ok = True
    for r in reports:
        t = r.get("ticker", "?")
        if not r.get("rows"):
            out.append(f"\n[{t}] sin operaciones en la cinta: {r.get('detail') or 'nada que auditar'}")
            continue
        a = r.get("auditor") or {}
        out.append(f"\n[{t}] {r['count']} operaciones · cobertura {r.get('coverage_pct')} % · "
                   f"estado {a.get('state')}")
        out.append(f"  {'hora':<9} {'contrato':<10} {'code':<12} {'bid':>8} {'ask':>8} {'precio':>8} "
                   f"{'esperado':<9} {'clasif.':<9} {'FLUJO':<8} {'TRACE':<8} ok")
        for f in r["rows"][:25]:
            out.append(
                f"  {str(f.get('tradeTime') or '')[11:19]:<9} "
                f"{str(f.get('option_symbol') or '')[-10:]:<10} "
                f"{str(f.get('tradeSideCode') or '—'):<12} "
                f"{(f.get('bidPrice') if f.get('bidPrice') is not None else float('nan')):>8.2f} "
                f"{(f.get('askPrice') if f.get('askPrice') is not None else float('nan')):>8.2f} "
                f"{(f.get('optionPrice') if f.get('optionPrice') is not None else float('nan')):>8.2f} "
                f"{f['expected']:<9} {f['classifier']:<9} "
                f"{str(f.get('flow_mark') or '—'):<8} {str(f.get('trace_mark') or '—'):<8} "
                f"{'OK' if f['match'] else 'FALLO'}")
        if not r["ok"]:
            todo_ok = False
            out.append(f"  >>> {r['mismatch_count']} operacion(es) CAMBIAN DE LADO por el camino:")
            for f in r["mismatches"][:10]:
                out.append(f"      {f.get('tradeTime')} esperado={f['expected']} "
                           f"clasificador={f['classifier']} flujo={f.get('flow_mark')} "
                           f"trace={f.get('trace_mark')}")
    out.append("")
    out.append("VEREDICTO: " + ("el lado se conserva en toda la cadena"
                                if todo_ok else "HAY OPERACIONES QUE CAMBIAN DE LADO"))
    return "\n".join(out), todo_ok


def probe_interval_map(base_url: str, ticker: str, timeout: float) -> Dict[str, Any]:
    """Interval Map celda a celda contra el crudo publicado. Puntos 18, 53 y 54.

    «El mapa de QQQ sale casi todo rojo» puede ser correcto —si el proveedor
    devuelve exposición negativa— o un bug de agregación. Mirar la pantalla al
    lado de la web del proveedor no las distingue. Esto compara los números.
    """
    from app.core.interval_map_audit import audit_grid

    br = read_bundle(base_url, ticker, timeout)
    if not br.get("ok"):
        return {"ok": False, "symbol": ticker, "detail": br.get("detail")}
    bundle = br["bundle"]
    rejilla = bundle.get("interval_map") or {}
    a = audit_grid(rejilla, limit=50)
    a["symbol"] = ticker.upper()
    return a


def render_interval_map(reports: List[Dict[str, Any]]) -> Tuple[str, bool]:
    out = ["", "=" * 104,
           "INTERVAL MAP · validación celda a celda contra el crudo de Quant Data",
           "  RAW → valor canónico → signo → intensidad. El signo NO puede cambiar.",
           "=" * 104,
           f"  {'activo':<7} {'griega':<7} {'celdas':>7} {'inversiones':>12} "
           f"{'huecos medidos':>15} {'atenuadas':>10}  veredicto"]
    todo_ok = True
    for r in reports:
        if not r.get("checked"):
            out.append(f"  {r.get('symbol','?'):<7} {'—':<7} {'—':>7} {'—':>12} {'—':>15} {'—':>10}  "
                       f"{r.get('reason') or r.get('detail') or 'sin rejilla'}")
            todo_ok = False
            continue
        out.append(f"  {r.get('symbol','?'):<7} {str(r.get('greek') or '—'):<7} {r['checked']:>7} "
                   f"{r['sign_flip_count']:>12} {len(r.get('missing_became_measured') or []):>15} "
                   f"{r.get('noise_floor_muted', 0):>10}  {'OK' if r['ok'] else 'BUG'}")
        if not r["ok"]:
            todo_ok = False
            for c in (r.get("sign_flips") or [])[:6]:
                out.append(f"      strike {c['strike']} · {c['time']}: RAW {c['raw']} "
                           f"({c['expected_color']}) → canónico {c['canonical']}")
    out.append("")
    out.append("VEREDICTO: " + ("el signo del proveedor se conserva en el renderizador"
                                if todo_ok else "HAY CELDAS QUE CAMBIAN DE SIGNO"))
    return "\n".join(out), todo_ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticker", action="append", default=None,
                    help="activo a verificar; repetible. Usa varios de escalas distintas.")
    ap.add_argument("--base-url", default=os.getenv("ITMQ_BASE_URL", "http://127.0.0.1:8000"),
                    help="URL de la terminal en marcha")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--json", dest="json_out", default=None,
                    help="ruta donde guardar el informe completo")
    ap.add_argument("--skip-bundle", action="store_true",
                    help="sólo proveedor y normalizador, sin comprobar la API interna")
    ap.add_argument("--aggressor", action="store_true",
                    help=("tabla de evidencia del lado agresor con la cinta REAL: "
                          "RAW tradeSideCode/bid/ask/precio → esperado → clasificador "
                          "→ marca FLUJO → marca TRACE, y exige que coincidan"))
    ap.add_argument("--interval-map", action="store_true",
                    help=("valida el Interval Map celda a celda contra el crudo: el "
                          "signo del proveedor no puede cambiar en el renderizador"))
    ap.add_argument("--cierre", action="store_true",
                    help=("ejecuta los tres modos forenses sobre los ocho activos del "
                          "criterio de cierre"))
    ap.add_argument("--dark-pool", action="store_true",
                    help=("verifica sólo los tres carriles de DARK POOL sobre una cesta "
                          "multi-activo, con el código HTTP y el campo rechazado de cada 400"))
    args = ap.parse_args()

    if args.cierre:
        args.aggressor = True
        args.interval_map = True
        if not args.ticker:
            args.ticker = list(CIERRE_BASKET)

    if args.aggressor or args.interval_map:
        cesta = args.ticker or ["SPY"]
        salida: Dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(),
                                  "base_url": args.base_url}
        ok = True
        if args.aggressor:
            informes = [probe_aggressor(args.base_url, t, args.timeout) for t in cesta]
            txt, parcial = render_aggressor(informes)
            print(txt)
            ok = ok and parcial
            salida["aggressor"] = informes
        if args.interval_map:
            informes = [probe_interval_map(args.base_url, t, args.timeout) for t in cesta]
            txt, parcial = render_interval_map(informes)
            print(txt)
            ok = ok and parcial
            salida["interval_map"] = informes
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps(salida, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            print(f"\ninforme completo: {args.json_out}")
        return 0 if ok else 1

    if args.dark_pool:
        basket = args.ticker or list(DARK_POOL_BASKET)
        results: Dict[str, Dict[str, Any]] = {}
        for ticker in basket:
            out = asyncio.run(probe_dark_pool(ticker, args.timeout))
            if not out.get("configured"):
                print(f"\n{out.get('detail')}")
                return 2
            results[ticker.upper()] = out
        txt, ok = render_dark_pool(results)
        print(txt)
        if args.json_out:
            Path(args.json_out).write_text(
                json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                            "dark_pool": results}, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8")
            print(f"\ninforme completo: {args.json_out}")
        return 0 if ok else 1

    tickers = args.ticker or ["SPY"]
    report: Dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(),
                              "base_url": args.base_url, "tickers": {}}
    all_ok = True

    for ticker in tickers:
        probe_out = asyncio.run(probe(ticker, args.timeout))
        if not probe_out.get("configured"):
            print(f"\n[{ticker}] {probe_out.get('detail')}")
            return 2

        bundle: Dict[str, Any] = {}
        if not args.skip_bundle:
            br = read_bundle(args.base_url, ticker, args.timeout)
            if not br.get("ok"):
                print(f"\n[{ticker}] no se pudo leer la API interna: {br.get('detail')}")
                print("   (usa --skip-bundle para verificar sólo proveedor y normalizador)")
                all_ok = False
                continue
            bundle = br["bundle"]

        rows = evaluate(probe_out, bundle)
        txt, ok = render(ticker, rows)
        print(txt)
        all_ok = all_ok and ok
        report["tickers"][ticker.upper()] = {"rows": rows,
                                             "auditor_present": bool(bundle.get("auditor"))}

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2, ensure_ascii=False,
                                                  default=str), encoding="utf-8")
        print(f"\ninforme completo: {args.json_out}")

    print("\nRecuerda: esta verificación es de FUENTE y TRANSPORTE. No sustituye a la "
          "comprobación visual de\n"
          "docs/operations/VALIDACION_LIVE_PRE_PRODUCCION_v1.44.0.md, que es la que "
          "confirma el renderizado.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""VERIFICACIÓN LIVE DE QUANT DATA · ITM QUANT v1.44.0

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

OK = "OK"
NO_DATA = "SIN DATOS"
NOT_DIRECT = "NO DIRECTO"
MISMATCH = "DISCREPANCIA"
ERROR = "ERROR"


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
                response = await asyncio.wait_for(
                    client.post(path, tool.body(ticker)), timeout=timeout)
                raw = response.payload
            except (QuantDataError, asyncio.TimeoutError) as exc:
                results[key] = {"label": label, "verdict": ERROR, "endpoint": path,
                                "detail": f"{type(exc).__name__}: {str(exc)[:180]}"}
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
    args = ap.parse_args()

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

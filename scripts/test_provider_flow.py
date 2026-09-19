from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _yn(v):
    return "SI" if bool(v) else "NO"


def main() -> int:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "DIA").upper().strip() or "DIA"
    url = f"http://127.0.0.1:8000/api/providers/flow-health?symbol={urllib.parse.quote(symbol)}"
    payload = None
    last_exc = None
    for attempt, timeout in enumerate((4.0, 12.0), start=1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                payload = json.loads(r.read().decode("utf-8"))
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 1:
                time.sleep(0.35)
    if payload is None:
        if isinstance(last_exc, urllib.error.URLError):
            print("ERROR: ITM QUANT no responde en http://127.0.0.1:8000")
            print(f"Detalle: {last_exc}")
            print("Inicia primero INICIAR_WEB.bat y vuelve a ejecutar esta prueba.")
        else:
            print(f"ERROR leyendo Provider Flow Fabric: {type(last_exc).__name__}: {last_exc}")
        return 3

    price = payload.get("price") or {}
    options = payload.get("options") or {}
    runtime = payload.get("runtime") or {}
    tasty = runtime.get("tastytrade") or {}
    qd = runtime.get("quantdata") or {}
    queue = runtime.get("data_lake_queue") or {}

    print("=" * 72)
    print(f"ITM QUANT v{payload.get('version','?')} - PROVIDER FLOW FABRIC - {symbol}")
    print("=" * 72)
    print(f"ESTADO GENERAL       : {payload.get('state','?')}")
    print(f"PROVEEDORES ACTIVOS  : {', '.join(payload.get('active_provider_scope') or []) or 'NINGUNO'}")
    print(f"REDUNDANCIA OPCIONES : {payload.get('redundancy_state','?')}")
    print()

    print("PRECIO")
    print(f"  Estado              : {price.get('state','?')}")
    print(f"  Fuente canonica     : {price.get('canonical_source') or 'NINGUNA'}")
    print(f"  Fuentes frescas     : {', '.join(price.get('providers_live') or []) or 'NINGUNA'}")
    print(f"  Bottleneck          : {price.get('bottleneck') or 'CLEAR'}")
    print()

    print("OPCIONES / FLOW")
    print(f"  Tape canonico       : {options.get('canonical_source') or 'NINGUNO'}")
    print(f"  Tapes LIVE          : {', '.join(options.get('live_trade_sources') or []) or 'NINGUNO'}")
    print(f"  Redundancia         : {options.get('redundancy',0)}")
    print(f"  Contratos/universo  : {options.get('universe_contracts') or options.get('contracts') or 0}")
    print(f"  Bottleneck          : {options.get('bottleneck') or 'CLEAR'}")
    print()

    print("ALPACA / TASTYTRADE")
    alp_sip = runtime.get("alpaca_sip") or {}
    alp_opra = runtime.get("alpaca_opra") or {}
    print(f"  Alpaca SIP connected: {_yn(alp_sip.get('connected'))}")
    print(f"  Alpaca OPRA connected: {_yn(alp_opra.get('connected'))}")
    print(f"  tastytrade configured: {_yn(tasty.get('configured'))}")
    print(f"  tastytrade DXLink    : {tasty.get('dxlink') or tasty.get('state') or '?'}")
    print()

    print("QUANT DATA")
    print(f"  Configurado          : {_yn(qd.get('configured'))}")
    print(f"  Runtime              : {_yn(qd.get('running'))}")
    print(f"  Último éxito         : {qd.get('last_success') or 'NINGUNO'}")
    print(f"  Último error         : {qd.get('last_error') or 'NINGUNO'}")
    print(f"  Rate remaining       : {qd.get('remaining') if qd.get('remaining') is not None else '?'}")
    print("  Autoridad            : CORROBORATION_ONLY · NATIVE ITM MATH")
    print()

    print("DATA LAKE")
    print(f"  Cola                 : {queue.get('depth',0)}/{queue.get('capacity',0)}")
    print(f"  Presion              : {queue.get('pressure_pct',0)}%")
    print(f"  Dropped              : {queue.get('dropped',0)}")
    issues = payload.get("issues") or []
    advisories = payload.get("analytics_advisories") or []
    print()
    print(f"ISSUES                : {', '.join(issues) if issues else 'NINGUNO'}")
    print(f"ANALYTICS ADVISORIES  : {', '.join(advisories) if advisories else 'NINGUNO'}")
    print("=" * 72)

    # A degraded result is useful diagnosis, not a script execution failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

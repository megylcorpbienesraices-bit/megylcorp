from __future__ import annotations

"""Headless ITM QUANT engine host.

Run this process continuously on a Windows host or VPS.  The browser is only a client;
closing the browser does not stop market collection/calculation.  To keep working while
the user's PC is physically off, run this exact process on an external 24/7 host/VPS.
"""

import os
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOCAL_ENV = BASE / ".env"
GLOBAL_ENV = Path.home() / ".itm_quant_gamma" / "alpaca.env"


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _load_env() -> list[Path]:
    used: list[Path] = []
    merged: dict[str, str] = {}
    for p in (GLOBAL_ENV, LOCAL_ENV):
        if p.exists():
            merged.update(_read_env(p)); used.append(p)
    for k, v in merged.items():
        os.environ[k] = v
    os.environ.setdefault("APP_ENV", "production" if os.getenv("ITM_PRODUCTION") == "1" else "always_on")
    os.environ.setdefault("ITM_ALWAYS_ON", "1")
    os.environ.setdefault("ITM_READY_PACKAGE_SECONDS", "20")
    os.environ.setdefault("ITM_HISTORY_PRECOMPUTE_SESSIONS", "370")
    return used



def _version() -> str:
    """Lee VERSION.txt en vez de repetir el número a mano en el banner."""
    try:
        return (Path(__file__).resolve().parent / "VERSION.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def main() -> int:
    used = _load_env()
    host = os.getenv("ITM_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("ITM_PORT", "8000"))
    print("=" * 72)
    print(f"ITM QUANT v{_version()} · ALWAYS-ON QUANT ENGINE")
    print("=" * 72)
    print("Motor headless 24/7. El navegador NO es el motor.")
    print("Bind:", f"{host}:{port}")
    print("Configuración:", ", ".join(str(x) for x in used) if used else "variables de entorno del host")
    print("Para seguir trabajando con tu PC apagada, ejecuta este proceso en un VPS/host externo 24/7.")
    # GUARDIA DE EXPOSICIÓN. La autenticación vive en net_guard; este preflight
    # aborta ANTES de abrir el puerto si una exposición externa/proxy no tiene
    # ITM_ACCESS_TOKEN fuerte. Fallar al arrancar es mejor que servir inseguro.
    from app.core.net_guard import assert_safe_bind, InsecureExposureError
    try:
        assert_safe_bind(host)
    except InsecureExposureError as exc:
        print()
        print("!" * 72)
        print("ARRANQUE ABORTADO POR SEGURIDAD")
        print("!" * 72)
        print(exc)
        return 2

    try:
        import uvicorn
        uvicorn.run("app.main:app", host=host, port=port, log_level=os.getenv("ITM_LOG_LEVEL", "info"), access_log=False)
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

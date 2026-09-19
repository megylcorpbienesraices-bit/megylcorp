from __future__ import annotations

import os
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOCAL_ENV = BASE / ".env"
GLOBAL_ENV = Path.home() / ".itm_quant_gamma" / "alpaca.env"
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "server.log"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_configuration() -> Path | None:
    # Merge the shared credential store first and let this version's local .env
    # override only the keys it actually contains.  A partial local .env must not
    # hide valid tastytrade/Quant Data credentials already saved globally.
    sources = [p for p in (GLOBAL_ENV, LOCAL_ENV) if p.exists()]
    if not sources:
        return None
    env: dict[str, str] = {}
    for source in sources:
        env.update(read_env(source))
    for k, v in env.items():
        os.environ[k] = v
    os.environ["APP_ENV"] = "local"
    return LOCAL_ENV if LOCAL_ENV.exists() else GLOBAL_ENV


def open_when_ready():
    url = "http://127.0.0.1:8000/health"
    for _ in range(80):
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                if r.status == 200:
                    webbrowser.open("http://127.0.0.1:8000")
                    return
        except Exception:
            time.sleep(0.25)


def main() -> int:
    src = load_configuration()
    if src is None:
        print("\nFALTA CONFIGURAR ALPACA.")
        print("Ejecuta primero CONFIGURAR_WEB.bat y guarda tus credenciales.\n")
        input("Presiona Enter para cerrar...")
        return 2

    print("ITM QUANT MULTI ASSET · INTERFAZ LOCAL")
    try:
        from app.config import PERSISTENCE_REPORT
        from app.persistence import category_dir
        global LOG_DIR, LOG_FILE
        LOG_DIR = category_dir("audit") / "system_logs"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE = LOG_DIR / "server.log"
        print("Memoria cuantitativa persistente:", PERSISTENCE_REPORT.persistent_root)
        if PERSISTENCE_REPORT.backup:
            print("Backup de migración:", PERSISTENCE_REPORT.backup)
        print("Política: NO RESET LIVE / AUDITOR / CALIBRATION / REPLAY")
    except Exception as exc:
        print("Aviso de persistencia:", exc)
    print("Configuracion cargada desde:", src)
    print("Abriendo: http://127.0.0.1:8000")
    print("Si ocurre un error, revisa:", LOG_FILE)
    print("\nPara uso 24/7 usa INICIAR_MOTOR_24_7.bat o despliega en VPS; el navegador puede cerrarse sin detener el motor.\n")

    threading.Thread(target=open_when_ready, daemon=True).start()

    try:
        import uvicorn
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=8000,
            log_level="info",
            access_log=True,
        )
        return 0
    except Exception:
        text = traceback.format_exc()
        LOG_FILE.write_text(text, encoding="utf-8")
        print("\nERROR AL INICIAR EL SERVIDOR:\n")
        print(text)
        input("Presiona Enter para cerrar...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

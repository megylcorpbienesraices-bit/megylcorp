"""Observabilidad estructurada para ITM QUANT (v1.27.1).

Motivación
----------
El motor tenía 611 `except Exception` y 191 que se tragaban el error en silencio.
En un sistema de trading eso significa que un fetch caído, una calibración corrupta
o un griego inválido degradan a un default silencioso: no puedes distinguir
"el mercado está tranquilo" de "el módulo se cayó".

Este módulo da tres cosas:

1. `log` / `get_logger`  -> logging con nombre por módulo, a consola + archivo rotativo.
2. `swallow(...)`        -> context manager que REEMPLAZA a `except Exception: pass`.
                            Degrada igual que antes (no rompe el flujo) pero deja rastro.
3. `degradations()`      -> contador vivo por sitio, para exponerlo en /health y ver
                            de un vistazo qué se está rompiendo en silencio.

Regla de uso:
    - Si el fallo es tolerable  -> `with swallow("dealer.hedge_pressure"): ...`
    - Si el fallo NO es tolerable -> deja que la excepción suba. No la envuelvas.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator
import logging
import logging.handlers
import os
import time

_LOCK = Lock()
_CONFIGURED = False
_COUNTS: dict[str, dict[str, Any]] = {}

SEVERITIES = ("OPTIONAL", "DEGRADED", "CRITICAL_DATA", "CRITICAL_MODEL")

LOG_LEVEL = os.getenv("ITM_LOG_LEVEL", "INFO").upper()
LOG_DIR = os.getenv("ITM_LOG_DIR", "").strip()
_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(message)s"


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    with _LOCK:
        if _CONFIGURED:
            return
        root = logging.getLogger("itm")
        root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
        root.propagate = False
        if not root.handlers:
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter(_FORMAT))
            root.addHandler(console)
            if LOG_DIR:
                try:
                    d = Path(LOG_DIR)
                    d.mkdir(parents=True, exist_ok=True)
                    fh = logging.handlers.RotatingFileHandler(
                        d / "itm_quant.log", maxBytes=8_000_000, backupCount=5, encoding="utf-8"
                    )
                    fh.setFormatter(logging.Formatter(_FORMAT))
                    root.addHandler(fh)
                except OSError as exc:
                    # No poder escribir el log NO debe tumbar el motor, pero sí avisar.
                    root.warning("no se pudo abrir el log de archivo en %s: %s", LOG_DIR, exc)
        _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Logger namespaced bajo `itm.` — usar `get_logger(__name__)`."""
    _configure()
    short = str(name or "app").replace("app.core.", "").replace("app.", "")
    return logging.getLogger(f"itm.{short}")


log = get_logger("core")


def _record(site: str, exc: BaseException, severity: str = "DEGRADED") -> None:
    sev = str(severity or "DEGRADED").upper()
    if sev not in SEVERITIES:
        sev = "DEGRADED"
    with _LOCK:
        row = _COUNTS.setdefault(site, {"count": 0, "last_error": "", "last_ts": 0.0, "type": "", "severity": sev})
        row["severity"] = sev
        row["count"] += 1
        row["last_error"] = str(exc)[:200]
        row["type"] = type(exc).__name__
        row["last_ts"] = time.time()


@contextmanager
def swallow(site: str, *, level: int = logging.WARNING, severity: str = "DEGRADED",
            expected: tuple[type[BaseException], ...] = (Exception,)) -> Iterator[None]:
    """Sustituto auditable de `except Exception: pass`.

    El flujo degrada exactamente igual que antes, pero el fallo queda contado y
    logueado con el nombre del sitio, así que es visible en /health y en el archivo.

        with swallow("calibration.report"):
            calib = calibration_report(...)
    """
    try:
        yield
    except expected as exc:  # noqa: BLE001 - degradación deliberada y registrada
        _record(site, exc, severity)
        get_logger(site.split(".")[0]).log(level, "degradado en %s [%s]: %s: %s",
                                           site, str(severity).upper(), type(exc).__name__, str(exc)[:200])


def guard(site: str, fn, default=None, *, level: int = logging.WARNING, severity: str = "DEGRADED"):
    """Versión expresión de `swallow`, para reemplazar `try: x=f() except: x=default`.

        calib = guard("calibration.report", lambda: calibration_report(...), default={})
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - degradación deliberada y registrada
        _record(site, exc, severity)
        get_logger(site.split(".")[0]).log(level, "degradado en %s [%s]: %s: %s",
                                           site, str(severity).upper(), type(exc).__name__, str(exc)[:200])
        return default


def note(site: str, exc: BaseException, *, level: int = logging.WARNING, severity: str = "DEGRADED") -> None:
    """Registro puntual de una degradación tolerada.

    Es lo que inyecta `scripts/codemod_silent_except.py` en lugar de `pass`.
    Mantiene el flujo idéntico al anterior; solo deja de ser invisible.
    """
    _record(site, exc, severity)
    get_logger(site.split(":")[0]).log(level, "degradado en %s [%s]: %s: %s",
                                       site, str(severity).upper(), type(exc).__name__, str(exc)[:200])


def expected(site: str) -> None:
    """Marca flujo de control esperado sin contaminar degradaciones ni consola INFO.

    Úsalo para timeouts que actúan como reloj, `queue.Empty` de fin de lote y
    cancelaciones ordenadas. Queda disponible a nivel DEBUG si se necesita
    investigar la cadencia, pero no cuenta como fallo de salud.
    """
    logger = get_logger("expected")
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("flujo esperado: %s", site)


def degradations(min_count: int = 1) -> dict[str, Any]:
    """Estado de degradación silenciosa, para exponer en /health."""
    with _LOCK:
        rows = [{"site": k, **v} for k, v in _COUNTS.items() if v["count"] >= min_count]
    rows.sort(key=lambda r: (-r["count"], r["site"]))
    total = sum(r["count"] for r in rows)
    by_severity = {name: sum(r["count"] for r in rows if r.get("severity", "DEGRADED") == name) for name in SEVERITIES}
    if by_severity["CRITICAL_MODEL"] or by_severity["CRITICAL_DATA"]:
        status = "CRITICAL"
    else:
        status = "OK" if total == 0 else ("WARN" if total < 50 else "DEGRADED")
    return {
        "total_degradations": total,
        "distinct_sites": len(rows),
        "status": status,
        "by_severity": by_severity,
        "sites": rows[:80],
    }


def reset_degradations() -> None:
    """Solo para tests y para reiniciar la ventana de observación de una sesión."""
    with _LOCK:
        _COUNTS.clear()

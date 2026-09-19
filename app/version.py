"""Single source of truth for the running ITM QUANT release."""
from __future__ import annotations
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

def _read_version() -> str:
    try:
        value=(PACKAGE_ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    except OSError:
        value="0.0.0"
    return value or "0.0.0"

APP_VERSION = _read_version()
CACHE_BUST = APP_VERSION.replace("+", "-")

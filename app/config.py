from __future__ import annotations
import os
from pathlib import Path

from .persistence import PERSISTENT_ROOT, bootstrap_persistence, category_dir
from .version import APP_VERSION

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"

# v1.23.0: quantitative memory lives OUTSIDE replaceable code; side-by-side migration is explicit-only.
PERSISTENCE_REPORT = bootstrap_persistence(BASE_DIR, APP_VERSION)
STORAGE_DIR = PERSISTENT_ROOT
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

APP_NAME = os.getenv("APP_NAME", "ITM QUANT MULTI ASSET ALWAYS-ON")
APP_ENV = os.getenv("APP_ENV", "development")
DATA_MODE = os.getenv("DATA_MODE", "auto").lower()  # auto | live | demo
REFRESH_SECONDS = max(5, int(os.getenv("REFRESH_SECONDS", "15")))
FLOW_REFRESH_SECONDS = max(10, int(os.getenv("FLOW_REFRESH_SECONDS", "30")))

# v1.25.3: expensive structural refreshes adapt to observed SIP/OPRA activity.
# Price and OPRA streams remain event-driven; these knobs do NOT create synthetic updates.
ADAPTIVE_REFRESH_ENABLED = os.getenv("ITM_ADAPTIVE_REFRESH_ENABLED", "1") == "1"
ADAPTIVE_STRUCT_ACTIVE_SECONDS = max(3.0, float(os.getenv("ITM_STRUCT_ACTIVE_SECONDS", "5")))
ADAPTIVE_STRUCT_NORMAL_SECONDS = max(5.0, float(os.getenv("ITM_STRUCT_NORMAL_SECONDS", "10")))
ADAPTIVE_STRUCT_QUIET_SECONDS = max(10.0, float(os.getenv("ITM_STRUCT_QUIET_SECONDS", "20")))
ADAPTIVE_FLOW_ACTIVE_SECONDS = max(5.0, float(os.getenv("ITM_FLOW_ACTIVE_SECONDS", "10")))
ADAPTIVE_FLOW_NORMAL_SECONDS = max(10.0, float(os.getenv("ITM_FLOW_NORMAL_SECONDS", "20")))
ADAPTIVE_FLOW_QUIET_SECONDS = max(20.0, float(os.getenv("ITM_FLOW_QUIET_SECONDS", "45")))
ADAPTIVE_WAKE_SECONDS = min(2.0, max(0.5, float(os.getenv("ITM_ADAPTIVE_WAKE_SECONDS", "1"))))
STRIKE_WINDOW = float(os.getenv("STRIKE_WINDOW", "12"))
EXPIRY_DAYS = int(os.getenv("EXPIRY_DAYS", "21"))
PREMARKET_FREEZE_MINUTES = int(os.getenv("PREMARKET_FREEZE_MINUTES", "5"))
PREMARKET_PATH = category_dir("sessions") / "premarket_maps.json"

HOT_SNAPSHOTS = max(80, int(os.getenv("HOT_SNAPSHOTS", "240")))  # ~60 min at 15 s structural cadence
CALIBRATION_MIN_SAMPLES = max(10, int(os.getenv("CALIBRATION_MIN_SAMPLES", "30")))

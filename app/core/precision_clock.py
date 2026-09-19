"""Precision clock observability with explicit PTP truth boundaries."""

from __future__ import annotations

from typing import Any, Dict
from datetime import datetime, timezone
import os, platform, shutil, subprocess, re


def clock_report() -> Dict[str, Any]:
    system = platform.system().upper()
    configured = bool(os.getenv("ITM_PTP_INTERFACE") or os.getenv("ITM_PTP_ENABLED", "").lower() in {"1","true","yes","on"})
    pmc = shutil.which("pmc")
    phc2sys = shutil.which("phc2sys")
    ptp4l = shutil.which("ptp4l")
    report: Dict[str, Any] = {
        "source": "SYSTEM/NTP",
        "ptp_status": "READY" if configured else "UNAVAILABLE",
        "hardware_timestamping": False,
        "offset_ns": None,
        "quality": "MEDIUM",
        "system": system,
        "exchange_timestamping": True,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "authority": "OBSERVABILITY_ONLY",
    }
    if configured and pmc:
        try:
            cmd = [pmc, "-u", "-b", "0", "GET TIME_STATUS_NP"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            text = (r.stdout or "") + "\n" + (r.stderr or "")
            m = re.search(r"master_offset\s+(-?\d+)", text)
            if r.returncode == 0 and m:
                off = int(m.group(1))
                report.update({"source":"PTP", "ptp_status":"ACTIVE", "offset_ns":off, "hardware_timestamping": bool(ptp4l or phc2sys), "quality":"HIGH" if abs(off) <= 500 else "DEGRADED"})
        except Exception as exc:
            report["detail"] = f"PTP probe failed: {type(exc).__name__}: {exc}"[:180]
            report["ptp_status"] = "READY"
    return report

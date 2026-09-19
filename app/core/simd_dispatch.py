"""CPU SIMD capability dispatch metadata for the quant engine."""

from __future__ import annotations

from typing import Any, Dict
import os, platform
from .obs import note as _obs_note


def _flags() -> set[str]:
    flags=set()
    if os.path.exists('/proc/cpuinfo'):
        try:
            text=open('/proc/cpuinfo','r',encoding='utf-8',errors='ignore').read().lower()
            for line in text.splitlines():
                if line.startswith('flags') or line.startswith('features'):
                    flags.update(line.split(':',1)[-1].split())
        except Exception as _e:
            _obs_note('simd_dispatch:17', _e)
    return flags


def status() -> Dict[str, Any]:
    f=_flags(); arch=platform.machine().lower()
    avx512=any(x.startswith('avx512') for x in f)
    avx2='avx2' in f
    backend='AVX512' if avx512 else 'AVX2' if avx2 else 'SCALAR/NUMPY'
    return {"architecture":arch,"avx512":avx512,"avx2":avx2,"backend":backend,"state":"ACTIVE" if (avx512 or avx2) else "READY","authority":"EXECUTION_SPEED_ONLY","note":"SIMD dispatch must preserve the same validated GEX/Greeks formulas."}

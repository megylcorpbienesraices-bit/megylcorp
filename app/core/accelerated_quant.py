"""ITM QUANT accelerated numerical kernels.

Execution policy
----------------
* Research/reference semantics remain NumPy-compatible and deterministic.
* CuPy/CUDA is preferred when a CUDA device is actually reachable.
* JAX is the second accelerated backend (GPU/TPU preferred, CPU allowed).
* NumPy is the hard fallback.
* Backend availability never changes Scanner authority or model meaning.

The host engine continues to receive NumPy arrays.  This keeps Calibration/Replay
portable while the expensive vectorized Greek/surface kernels can execute on an
accelerator when one is present.
"""

from __future__ import annotations

from typing import Any, Dict
import math
import os
import numpy as np
from .obs import note as _obs_note

try:  # CUDA path
    import cupy as cp
    from cupyx.scipy.special import ndtr as cp_ndtr
except Exception:  # pragma: no cover
    cp = None; cp_ndtr = None

try:  # accelerator / portable XLA path
    import jax
    import jax.numpy as jnp
    from jax.scipy.special import ndtr as jax_ndtr
    try:
        jax.config.update("jax_enable_x64", True)
    except Exception as _e:
        _obs_note('accelerated_quant:35', _e)
except Exception:  # pragma: no cover
    jax = None; jnp = None; jax_ndtr = None


def _enabled() -> bool:
    return str(os.getenv("ITM_ACCELERATED_QUANT", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _preference() -> str:
    return str(os.getenv("ITM_ACCELERATOR_BACKEND", "AUTO")).strip().upper()


def _cupy_status() -> Dict[str, Any]:
    if cp is None:
        return {"available": False, "gpu": False, "reason": "CuPy unavailable"}
    try:
        n = int(cp.cuda.runtime.getDeviceCount())
        if n <= 0:
            return {"available": True, "gpu": False, "reason": "No CUDA device"}
        devs=[]
        for i in range(n):
            props=cp.cuda.runtime.getDeviceProperties(i)
            name=props.get("name", b"CUDA")
            if isinstance(name,(bytes,bytearray)): name=name.decode(errors="replace")
            devs.append(f"cuda:{i}:{name}")
        return {"available": True, "gpu": True, "devices": devs}
    except Exception as exc:
        return {"available": True, "gpu": False, "reason": str(exc)[:160]}


def _jax_status() -> Dict[str, Any]:
    if jax is None:
        return {"available": False, "gpu": False, "reason": "JAX unavailable"}
    try:
        ds=list(jax.devices())
        gpu=any(getattr(d,"platform","") in {"gpu","tpu"} for d in ds)
        return {"available": True, "gpu": gpu,
                "devices": [f"{d.platform}:{getattr(d,'device_kind',str(d))}" for d in ds]}
    except Exception as exc:
        return {"available": True, "gpu": False, "reason": str(exc)[:160]}


def _selected_backend() -> str:
    if not _enabled():
        return "NUMPY"
    pref=_preference(); cs=_cupy_status(); js=_jax_status()
    if pref in {"CUPY","CUDA"}:
        return "CUPY" if cs.get("gpu") else "NUMPY"
    if pref=="JAX":
        return "JAX" if js.get("available") else "NUMPY"
    if pref=="NUMPY":
        return "NUMPY"
    if cs.get("gpu"):
        return "CUPY"
    if js.get("gpu"):
        return "JAX"
    # AUTO debe ser aceleración real, no simplemente «JAX está instalado».
    # En CPU, JAX no aporta una ventaja material para este kernel y puede variar
    # unos ULP frente al camino NumPy/SciPy. Eso viola el contrato de replay y
    # auditoría bit-a-bit. JAX CPU sigue disponible si el operador lo solicita
    # explícitamente con ITM_ACCELERATOR_BACKEND=JAX.
    return "NUMPY"


def backend_status() -> Dict[str, Any]:
    cs=_cupy_status(); js=_jax_status(); sel=_selected_backend()
    if not _enabled():
        state="DISABLED"
    elif sel=="CUPY":
        state="ACTIVE_GPU"
    elif sel=="JAX":
        state="ACTIVE_GPU" if js.get("gpu") else "ACTIVE_CPU"
    else:
        state="FALLBACK" if _preference() not in {"AUTO","NUMPY"} else "READY"
    return {
        "backend": sel, "state": state, "gpu": bool(sel=="CUPY" or (sel=="JAX" and js.get("gpu"))),
        "preference": _preference(), "cupy": cs, "jax": js,
        "note": "Acceleration changes throughput only; formulas/model authority are unchanged.",
    }


def _np_ndtr(x):
    from scipy.special import ndtr
    return ndtr(x)


def black_scholes_batch(S, K, T, sigma, is_call, r, q, *, prefer_accelerated: bool = True):
    """Vectorized Delta/Gamma/Vanna/Charm/Speed on CUDA/JAX/NumPy."""
    backend=_selected_backend() if prefer_accelerated else "NUMPY"
    if backend=="CUPY":
        xp=cp; cdf_fn=cp_ndtr
    elif backend=="JAX":
        xp=jnp; cdf_fn=jax_ndtr
    else:
        xp=np; cdf_fn=_np_ndtr

    S=xp.asarray(S,dtype=xp.float64);K=xp.asarray(K,dtype=xp.float64);T=xp.asarray(T,dtype=xp.float64)
    sig=xp.asarray(sigma,dtype=xp.float64);call=xp.asarray(is_call,dtype=bool);r=xp.asarray(r,dtype=xp.float64);q=xp.asarray(q,dtype=xp.float64)
    S=xp.maximum(S,1e-12);K=xp.maximum(K,1e-12);T=xp.maximum(T,1e-12);sig=xp.maximum(sig,1e-9)
    root=xp.sqrt(T);d1=(xp.log(S/K)+(r-q+0.5*sig*sig)*T)/(sig*root);d2=d1-sig*root
    phi=xp.exp(-0.5*d1*d1)/math.sqrt(2*math.pi);cdf=cdf_fn(d1);discq=xp.exp(-q*T)
    delta=discq*xp.where(call,cdf,cdf-1.0)
    gamma=discq*phi/(S*sig*root)
    vanna=-discq*phi*d2/sig
    charm=discq*(q*xp.where(call,cdf,cdf-1.0)-phi*(2*(r-q)*T-d2*sig*root)/(2*T*sig*root))
    speed=-gamma/S*(d1/(sig*root)+1.0)
    out={"delta":delta,"gamma":gamma,"vanna":vanna,"charm":charm,"speed":speed}
    if backend=="CUPY":
        return {k:cp.asnumpy(v) for k,v in out.items()}
    return {k:np.asarray(v,dtype=float) for k,v in out.items()}


def matrix_reprice(*, spot_grid, strikes, maturities, iv_matrix, call_mask=None, r=0.045, q=0.013):
    """Reprice strike×maturity Greek fields across spot scenarios."""
    spots=np.atleast_1d(np.asarray(spot_grid,float));ks=np.asarray(strikes,float);ts=np.asarray(maturities,float);iv=np.asarray(iv_matrix,float)
    if iv.shape!=(len(ks),len(ts)):
        raise ValueError("iv_matrix must be strike×maturity")
    calls=np.ones_like(iv,dtype=bool) if call_mask is None else np.asarray(call_mask,bool)
    rr=np.full_like(iv,float(r));qq=np.full_like(iv,float(q));K=np.repeat(ks[:,None],len(ts),axis=1);T=np.repeat(ts[None,:],len(ks),axis=0)
    result=[]
    for s in spots:
        S=np.full_like(iv,float(s));g=black_scholes_batch(S.ravel(),K.ravel(),T.ravel(),iv.ravel(),calls.ravel(),rr.ravel(),qq.ravel())
        result.append({k:v.reshape(iv.shape) for k,v in g.items()})
    return result

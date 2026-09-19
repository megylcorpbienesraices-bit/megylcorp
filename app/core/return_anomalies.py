"""Anomalías en rendimientos logarítmicos — sección observacional aislada.

QUÉ ES Y QUÉ NO ES
------------------
Es una sección **independiente** del motor. No importa nada de ``service.py`` ni
de ``engine.py``, no toca ``STATE``, no escribe en disco y no participa en
ninguna decisión del Scanner. Se puede borrar este fichero entero y la
plataforma sigue funcionando igual. Esa es la condición de diseño.

Corrige dos defectos del detector clásico de bandas ±2σ sobre log-returns:

1. **Frontera de sesión.** Mezclar barras intradía con el salto overnight trata
   dos poblaciones de volatilidad distinta como si fueran una. Medido sobre una
   serie sintética SIN ninguna anomalía real: las fronteras eran el 1 % de las
   barras y el 30 % de las detecciones. Aquí cada población se normaliza contra
   sí misma.

2. **Umbral fijo de 2σ.** Con colas gruesas y clustering de volatilidad, 2σ no
   marca el 2,3 % sino más, y en rachas. El umbral se calibra aquí con un
   bootstrap de bloques circular, que conserva el clustering de la propia serie.

LO QUE ESTE MÓDULO NO AFIRMA
----------------------------
No emite dirección. Medido sobre GARCH(1,1) con deriva cero y sin mecanismo de
reversión: tras un evento >+2σ, la barra siguiente es negativa el 50,4 % de las
veces —una moneda— mientras que su magnitud media sube ×1,19. La evidencia
sostiene afirmaciones sobre |r|, no sobre sign(r). Por eso la salida expone
``direccion: None`` de forma explícita y permanente, y lo que informa es el
régimen de magnitud esperable y la **frecuencia histórica observada en esta
misma serie** de continuación / reversión / sin recorrido.

Esa frecuencia es descriptiva, calculada dentro de la muestra. No es una
predicción y el campo ``base_rate_en_muestra`` lo dice.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional
import math

import numpy as np
import pandas as pd

AUTHORITY = "OBSERVATIONAL_ONLY_NEVER_DIRECTIONAL"
SEMANTICS = "MAGNITUDE_REGIME_NOT_DIRECTION"

INTRADIA = "INTRADIA"
OVERNIGHT = "OVERNIGHT"

CONTINUACION = "CONTINUACION"
REVERSION = "REVERSION"
SIN_RECORRIDO = "SIN_RECORRIDO"


@dataclass(frozen=True)
class PoliticaAnomalias:
    """Parámetros del detector. Todos explícitos: nada calibrado a ojo."""

    hueco_sesion_minutos: float = 45.0   # salto mayor => población OVERNIGHT
    bloque: int = 12                     # longitud de bloque del bootstrap
    iteraciones: int = 400
    percentil: float = 99.0              # umbral del nulo
    minimo_muestras: int = 40
    horizonte_barras: int = 3            # ventana para medir qué pasó después
    clip_z: float = 12.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


POLITICA_POR_DEFECTO = PoliticaAnomalias()


# --------------------------------------------------------------- utilidades


def _z_robusto(x: np.ndarray, clip: float) -> np.ndarray:
    """Mediana y MAD en lugar de media y desviación típica.

    Con colas gruesas la desviación típica la infla justo el dato extremo que se
    quiere detectar, de modo que el outlier se esconde a sí mismo.
    """
    finitos = x[np.isfinite(x)]
    if finitos.size < 3:
        return np.zeros_like(x, dtype=float)
    med = float(np.median(finitos))
    mad = float(np.median(np.abs(finitos - med)))
    escala = 1.4826 * mad
    if not math.isfinite(escala) or escala <= 1e-15:
        sd = float(np.std(finitos))
        escala = sd if math.isfinite(sd) and sd > 1e-15 else 1.0
    z = (x - med) / escala
    return np.clip(np.nan_to_num(z, nan=0.0, posinf=clip, neginf=-clip), -clip, clip)


def separar_poblaciones(frame: pd.DataFrame, politica: PoliticaAnomalias = POLITICA_POR_DEFECTO) -> pd.DataFrame:
    """Calcula log-returns y etiqueta cada uno como INTRADIA u OVERNIGHT.

    La frontera se detecta por el hueco temporal real entre observaciones, no por
    un calendario de festivos: así no hay que inventarse una fuente de días no
    hábiles antes de tener una autoritativa.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=["timestamp", "precio", "log_return", "poblacion", "hueco_min"])
    columna = next((c for c in ("price", "close", "underlying_price", "precio") if c in frame.columns), None)
    if columna is None or "timestamp" not in frame.columns:
        return pd.DataFrame(columns=["timestamp", "precio", "log_return", "poblacion", "hueco_min"])

    d = pd.DataFrame({
        "timestamp": pd.to_datetime(frame["timestamp"], errors="coerce", utc=True),
        "precio": pd.to_numeric(frame[columna], errors="coerce"),
    }).dropna().sort_values("timestamp").reset_index(drop=True)
    d = d[d["precio"] > 0]
    # La entrada puede ser una cadena de opciones con muchas filas por timestamp
    # (una por contrato). Se colapsa a un precio por instante: el subyacente es
    # el mismo para todas ellas, y duplicarlo fabricaría retornos nulos.
    if d["timestamp"].duplicated().any():
        d = d.groupby("timestamp", as_index=False)["precio"].median().sort_values("timestamp")
        d = d.reset_index(drop=True)
    if len(d) < 2:
        return pd.DataFrame(columns=["timestamp", "precio", "log_return", "poblacion", "hueco_min"])

    d["log_return"] = np.log(d["precio"]).diff()
    d["hueco_min"] = d["timestamp"].diff().dt.total_seconds() / 60.0
    d["poblacion"] = np.where(d["hueco_min"] > float(politica.hueco_sesion_minutos), OVERNIGHT, INTRADIA)
    return d.dropna(subset=["log_return"]).reset_index(drop=True)


def umbral_bootstrap(valores: np.ndarray, politica: PoliticaAnomalias = POLITICA_POR_DEFECTO,
                     semilla: int = 20260912) -> float:
    """Umbral de |z| calibrado con bootstrap de bloques circular.

    Remuestrear por bloques —y no punto a punto— conserva el clustering de
    volatilidad de la serie. El umbral resultante es el percentil de |z| que la
    propia serie produce sin que ocurra nada especial, así que la tasa de
    disparo bajo el nulo es ``100 - percentil`` por construcción, en lugar del
    2,3 % teórico que 2σ solo cumpliría con normalidad.
    """
    x = np.asarray(valores, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < max(8, int(politica.minimo_muestras) // 2):
        return float("nan")
    bloque = int(max(2, min(int(politica.bloque), n // 2)))
    rng = np.random.default_rng(semilla)
    n_bloques = int(np.ceil(n / bloque))
    acumulado = np.empty(int(politica.iteraciones) * n_bloques * bloque, dtype=float)
    cursor = 0
    for _ in range(int(politica.iteraciones)):
        inicios = rng.integers(0, n, size=n_bloques)
        idx = (inicios[:, None] + np.arange(bloque)[None, :]).ravel() % n   # circular
        muestra = x[idx]
        z = np.abs(_z_robusto(muestra, politica.clip_z))
        acumulado[cursor:cursor + z.size] = z
        cursor += z.size
    return float(np.percentile(acumulado[:cursor], float(politica.percentil)))


def _que_paso_despues(retornos: np.ndarray, posicion: int, horizonte: int) -> tuple[str, float]:
    """Clasifica el recorrido posterior. Descriptivo, nunca predictivo."""
    fin = min(len(retornos), posicion + 1 + int(horizonte))
    if fin <= posicion + 1:
        return SIN_RECORRIDO, 0.0
    acumulado = float(np.sum(retornos[posicion + 1:fin]))
    referencia = abs(float(retornos[posicion]))
    if referencia <= 0 or abs(acumulado) < 0.5 * referencia:
        return SIN_RECORRIDO, acumulado
    mismo_signo = math.copysign(1.0, acumulado) == math.copysign(1.0, float(retornos[posicion]))
    return (CONTINUACION if mismo_signo else REVERSION), acumulado


# --------------------------------------------------------------- detección


def detectar_anomalias(frame: pd.DataFrame, politica: PoliticaAnomalias = POLITICA_POR_DEFECTO,
                       *, simbolo: str = "UNKNOWN", max_eventos: int = 60) -> dict[str, Any]:
    """Sección observacional completa. Nunca lanza; devuelve su propio estado."""
    base: dict[str, Any] = {
        "seccion": "ANOMALIAS_RENDIMIENTOS",
        "simbolo": str(simbolo or "UNKNOWN").upper(),
        "autoridad": AUTHORITY,
        "semantica": SEMANTICS,
        "direccion": None,
        "aviso": ("La evidencia sostiene afirmaciones sobre la MAGNITUD esperable, no sobre el signo. "
                  "Este panel no propone dirección y no participa en el Scanner."),
        "politica": politica.as_dict(),
        "listo": False,
        "eventos": [],
    }
    try:
        d = separar_poblaciones(frame, politica)
    except Exception as exc:                                  # pragma: no cover - defensa
        return {**base, "estado": "ERROR_ENTRADA", "detalle": f"{type(exc).__name__}: {exc}"[:160]}
    if d.empty:
        return {**base, "estado": "SIN_DATOS", "detalle": "no hay precios utilizables"}
    if len(d) < int(politica.minimo_muestras):
        return {**base, "estado": "MUESTRA_INSUFICIENTE",
                "detalle": f"{len(d)} retornos; se exigen {politica.minimo_muestras}",
                "observaciones": int(len(d))}

    retornos = d["log_return"].to_numpy(dtype=float)
    z = np.zeros(len(d), dtype=float)
    umbrales: dict[str, float] = {}
    resumen_poblaciones: dict[str, Any] = {}

    # El punto clave: cada población se normaliza y se umbraliza CONTRA SÍ MISMA.
    for poblacion in (INTRADIA, OVERNIGHT):
        mascara = (d["poblacion"] == poblacion).to_numpy()
        if not mascara.any():
            continue
        valores = retornos[mascara]
        z[mascara] = _z_robusto(valores, politica.clip_z)
        umbral = umbral_bootstrap(valores, politica)
        umbrales[poblacion] = umbral
        resumen_poblaciones[poblacion] = {
            "observaciones": int(mascara.sum()),
            "umbral_z": None if not math.isfinite(umbral) else round(umbral, 3),
            "sigma_robusta": round(float(np.median(np.abs(valores - np.median(valores))) * 1.4826), 8),
            "umbral_calibrado": bool(math.isfinite(umbral)),
        }

    d["z"] = z
    d["umbral"] = d["poblacion"].map(umbrales)
    # Sin umbral calibrado no se inventa uno: la población queda sin marcar.
    d["anomalia"] = np.where(d["umbral"].notna(), np.abs(d["z"]) > d["umbral"], False)

    posiciones = np.flatnonzero(d["anomalia"].to_numpy())
    eventos = []
    conteo = {CONTINUACION: 0, REVERSION: 0, SIN_RECORRIDO: 0}
    for pos in posiciones:
        etiqueta, recorrido = _que_paso_despues(retornos, int(pos), politica.horizonte_barras)
        conteo[etiqueta] += 1
        eventos.append({
            "timestamp": pd.Timestamp(d["timestamp"].iloc[pos]).isoformat(),
            "poblacion": str(d["poblacion"].iloc[pos]),
            "log_return": round(float(retornos[pos]), 8),
            "z": round(float(d["z"].iloc[pos]), 3),
            "umbral": round(float(d["umbral"].iloc[pos]), 3),
            "hueco_min": None if not math.isfinite(float(d["hueco_min"].iloc[pos] or np.nan))
                         else round(float(d["hueco_min"].iloc[pos]), 1),
            "despues": etiqueta,
            "recorrido_posterior": round(recorrido, 8),
        })

    total_eventos = len(eventos)
    # Régimen de magnitud: cuánto crece |r| después de un evento, en esta serie.
    if total_eventos and len(retornos) > politica.horizonte_barras + 1:
        siguientes = [abs(float(retornos[p + 1])) for p in posiciones if p + 1 < len(retornos)]
        incondicional = float(np.mean(np.abs(retornos)))
        multiplo = (float(np.mean(siguientes)) / incondicional) if siguientes and incondicional > 0 else None
    else:
        multiplo = None

    intradia_marcados = sum(1 for e in eventos if e["poblacion"] == INTRADIA)
    return {
        **base,
        "listo": True,
        "estado": "OK",
        "observaciones": int(len(d)),
        "poblaciones": resumen_poblaciones,
        "eventos_totales": total_eventos,
        "tasa_disparo_pct": round(100.0 * total_eventos / len(d), 2) if len(d) else 0.0,
        "tasa_esperada_bajo_nulo_pct": round(100.0 - float(politica.percentil), 2),
        "eventos_intradia": intradia_marcados,
        "eventos_overnight": total_eventos - intradia_marcados,
        "magnitud_siguiente_x": None if multiplo is None else round(multiplo, 3),
        "base_rate_en_muestra": {
            "nota": "Frecuencias observadas DENTRO de esta misma serie. Es descriptivo, no una predicción.",
            "horizonte_barras": int(politica.horizonte_barras),
            **{k: {"n": v, "pct": round(100.0 * v / total_eventos, 1) if total_eventos else 0.0}
               for k, v in conteo.items()},
        },
        "eventos": eventos[-int(max_eventos):],
    }


def resumen_compacto(reporte: dict[str, Any]) -> dict[str, Any]:
    """Vista mínima para la cabecera de la sección."""
    if not isinstance(reporte, dict) or not reporte.get("listo"):
        return {"seccion": "ANOMALIAS_RENDIMIENTOS", "listo": False,
                "estado": (reporte or {}).get("estado", "SIN_DATOS"), "direccion": None}
    return {
        "seccion": "ANOMALIAS_RENDIMIENTOS",
        "listo": True,
        "simbolo": reporte.get("simbolo"),
        "eventos_totales": reporte.get("eventos_totales"),
        "tasa_disparo_pct": reporte.get("tasa_disparo_pct"),
        "tasa_esperada_bajo_nulo_pct": reporte.get("tasa_esperada_bajo_nulo_pct"),
        "magnitud_siguiente_x": reporte.get("magnitud_siguiente_x"),
        "direccion": None,
        "autoridad": AUTHORITY,
    }

# ===========================================================================
# v1.27.11 · laboratorio causal de anomalías + momentum (SHADOW)
#
# Este bloque NO reemplaza el detector v1.27.10. Lo conserva como CURRENT y
# añade un CANDIDATE causal/multiframe que solo observa. La separación es
# deliberada: cualquier promoción futura exige evidencia OOS.
# ===========================================================================

TIMEFRAMES_MIN = (1, 3, 5, 15)
MOMENTUM_AUTHORITY = "SHADOW_OBSERVATIONAL_NO_SCANNER_AUTHORITY"


@dataclass(frozen=True)
class PoliticaMomentum:
    """Política explícita del laboratorio causal.

    El detector LIVE usa solo observaciones anteriores a cada barra. La
    estacionalidad intradía se aproxima por franjas de reloj NY y, cuando hay
    muestra suficiente, por régimen de volatilidad previo. Cuando no existe
    suficiente historia contextual, degrada a un baseline causal global; nunca
    usa observaciones futuras para completar el umbral.
    """

    timeframes_min: tuple[int, ...] = TIMEFRAMES_MIN
    franja_minutos: int = 30
    hueco_sesion_minutos: float = 45.0
    minimo_muestras_causal: int = 120
    minimo_muestras_contexto: int = 100
    ventana_baseline: int = 900
    percentil_causal: float = 99.0
    clip_z: float = 12.0
    vol_window: int = 20
    precursor_short: int = 4
    precursor_long: int = 24
    episodio_gap_barras: int = 2
    max_series: int = 720

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


POLITICA_MOMENTUM_DEFECTO = PoliticaMomentum()


def _precio_unico(frame: pd.DataFrame) -> pd.DataFrame:
    """Normaliza un historial a ``timestamp`` + ``precio`` sin interpolar."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame(columns=["timestamp", "precio"])
    columna = next((c for c in ("price", "close", "underlying_price", "precio") if c in frame.columns), None)
    if columna is None or "timestamp" not in frame.columns:
        return pd.DataFrame(columns=["timestamp", "precio"])
    d = pd.DataFrame({
        "timestamp": pd.to_datetime(frame["timestamp"], errors="coerce", utc=True),
        "precio": pd.to_numeric(frame[columna], errors="coerce"),
    }).dropna(subset=["timestamp", "precio"])
    d = d[d["precio"] > 0].sort_values("timestamp")
    if d.empty:
        return pd.DataFrame(columns=["timestamp", "precio"])
    # Una cadena puede repetir el mismo underlying por contrato. Mediana evita
    # fabricar N retornos nulos por el mismo instante.
    d = d.groupby("timestamp", as_index=False)["precio"].median().sort_values("timestamp")
    return d.reset_index(drop=True)


def _resample_timeframe(frame: pd.DataFrame, minutos: int, politica: PoliticaMomentum) -> pd.DataFrame:
    """Construye barras de cierre causal para un timeframe concreto."""
    d = _precio_unico(frame)
    if len(d) < 2:
        return pd.DataFrame(columns=["timestamp", "precio", "log_return", "hueco_min", "poblacion"])
    minutos = int(max(1, minutos))
    serie = d.set_index("timestamp")["precio"].resample(
        f"{minutos}min", label="right", closed="right"
    ).last().dropna()
    if len(serie) < 2:
        return pd.DataFrame(columns=["timestamp", "precio", "log_return", "hueco_min", "poblacion"])
    out = serie.rename("precio").reset_index()
    out["log_return"] = np.log(out["precio"]).diff()
    out["hueco_min"] = out["timestamp"].diff().dt.total_seconds() / 60.0
    corte = max(float(politica.hueco_sesion_minutos), float(minutos) * 3.0)
    out["poblacion"] = np.where(out["hueco_min"] > corte, OVERNIGHT, INTRADIA)
    out = out.dropna(subset=["log_return"]).reset_index(drop=True)
    if out.empty:
        return out
    ny = out["timestamp"].dt.tz_convert("America/New_York")
    minuto_dia = ny.dt.hour * 60 + ny.dt.minute
    franja = int(max(5, politica.franja_minutos))
    out["franja_ny"] = (minuto_dia // franja).astype(int)
    out["hora_ny"] = ny.dt.strftime("%H:%M")
    return out


def _escala_robusta(valores: np.ndarray) -> tuple[float, float]:
    x = np.asarray(valores, dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 3:
        return 0.0, float("nan")
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med)))
    scale = 1.4826 * mad
    if not math.isfinite(scale) or scale <= 1e-15:
        sd = float(np.std(x))
        scale = sd if math.isfinite(sd) and sd > 1e-15 else float("nan")
    return med, scale


def _percentil_empirico(hist: np.ndarray, valor: float) -> float:
    """Percentil causal [0,100] de ``valor`` frente a historia previa."""
    h = np.asarray(hist, dtype=float)
    h = h[np.isfinite(h)]
    if h.size == 0 or not math.isfinite(float(valor)):
        return float("nan")
    return 100.0 * (float(np.sum(h <= float(valor))) + 1.0) / (float(h.size) + 1.0)


def _regimenes_volatilidad_causales(d: pd.DataFrame, politica: PoliticaMomentum) -> list[str]:
    """LOW/MID/HIGH usando solo realized-vol conocida antes de cada barra."""
    r = pd.to_numeric(d["log_return"], errors="coerce").astype(float)
    w = int(max(6, politica.vol_window))
    # shifted: la barra actual nunca participa en su propio régimen previo.
    vol_previa = r.shift(1).rolling(w, min_periods=max(5, w // 2)).std(ddof=0)
    estados: list[str] = []
    for i in range(len(d)):
        v = float(vol_previa.iloc[i]) if pd.notna(vol_previa.iloc[i]) else float("nan")
        hist = vol_previa.iloc[:i].dropna().to_numpy(dtype=float)
        if not math.isfinite(v) or hist.size < max(20, politica.minimo_muestras_causal // 2):
            estados.append("UNKNOWN")
            continue
        q1, q2 = np.quantile(hist, [0.33, 0.67])
        estados.append("LOW" if v <= q1 else "HIGH" if v >= q2 else "MID")
    return estados


def _baseline_indices_causal(d: pd.DataFrame, i: int, politica: PoliticaMomentum, minimo_causal: int) -> tuple[np.ndarray, str]:
    """Selecciona baseline previo con degradación contextual explícita."""
    if i <= 0:
        return np.array([], dtype=int), "INSUFFICIENT"
    prev = d.iloc[:i]
    pop = str(d["poblacion"].iloc[i])
    candidatos = prev[prev["poblacion"] == pop]
    if candidatos.empty:
        return np.array([], dtype=int), "INSUFFICIENT"

    if pop == INTRADIA:
        franja = int(d["franja_ny"].iloc[i])
        reg = str(d["regimen_vol"].iloc[i])
        mismo_t = candidatos[candidatos["franja_ny"] == franja]
        if reg != "UNKNOWN":
            mismo_tr = mismo_t[mismo_t["regimen_vol"] == reg]
            if len(mismo_tr) >= int(politica.minimo_muestras_contexto):
                idx = mismo_tr.index.to_numpy(dtype=int)
                return idx[-int(politica.ventana_baseline):], "TIME_OF_DAY_VOL_REGIME"
        if len(mismo_t) >= int(politica.minimo_muestras_contexto):
            idx = mismo_t.index.to_numpy(dtype=int)
            return idx[-int(politica.ventana_baseline):], "TIME_OF_DAY"

    if len(candidatos) >= int(minimo_causal):
        idx = candidatos.index.to_numpy(dtype=int)
        return idx[-int(politica.ventana_baseline):], "GLOBAL_CAUSAL"
    return np.array([], dtype=int), "INSUFFICIENT"


def _serie_causal(frame: pd.DataFrame, minutos: int, politica: PoliticaMomentum) -> pd.DataFrame:
    """Score causal por barra: cada fila se evalúa contra pasado estricto."""
    d = _resample_timeframe(frame, minutos, politica)
    if d.empty:
        return d
    d["regimen_vol"] = _regimenes_volatilidad_causales(d, politica)
    r = d["log_return"].to_numpy(dtype=float)
    z = np.full(len(d), np.nan, dtype=float)
    umbral = np.full(len(d), np.nan, dtype=float)
    p_cola = np.full(len(d), np.nan, dtype=float)
    baseline_med = np.full(len(d), np.nan, dtype=float)
    baseline_scale = np.full(len(d), np.nan, dtype=float)
    scope: list[str] = []
    muestras = np.zeros(len(d), dtype=int)

    minimo_causal = max(30, int(round(float(politica.minimo_muestras_causal) / math.sqrt(max(1.0, float(minutos))))))
    for i in range(len(d)):
        idx, sc = _baseline_indices_causal(d, i, politica, minimo_causal)
        scope.append(sc)
        if idx.size == 0:
            continue
        hist = r[idx]
        hist = hist[np.isfinite(hist)]
        minimo_requerido = int(politica.minimo_muestras_contexto) if sc in {"TIME_OF_DAY", "TIME_OF_DAY_VOL_REGIME"} else int(minimo_causal)
        if hist.size < minimo_requerido:
            continue
        med, scale = _escala_robusta(hist)
        if not math.isfinite(scale) or scale <= 0:
            continue
        zi = (float(r[i]) - med) / scale
        prior_z = np.abs((hist - med) / scale)
        thr = float(np.percentile(prior_z, float(politica.percentil_causal)))
        tail = (1.0 + float(np.sum(prior_z >= abs(zi)))) / (float(prior_z.size) + 1.0)
        z[i] = float(np.clip(zi, -politica.clip_z, politica.clip_z))
        umbral[i] = thr
        p_cola[i] = tail
        baseline_med[i] = med
        baseline_scale[i] = scale
        muestras[i] = int(hist.size)

    d["z_causal"] = z
    d["umbral_causal"] = umbral
    d["tail_p"] = p_cola
    d["baseline_med"] = baseline_med
    d["baseline_scale"] = baseline_scale
    d["banda_superior"] = baseline_med + umbral * baseline_scale
    d["banda_inferior"] = baseline_med - umbral * baseline_scale
    d["baseline_scope"] = scope
    d["baseline_n"] = muestras
    d["anomalia_causal"] = np.where(
        np.isfinite(d["z_causal"]) & np.isfinite(d["umbral_causal"]),
        np.abs(d["z_causal"]) > d["umbral_causal"],
        False,
    )
    return d


def _feature_percentiles_causales(d: pd.DataFrame, politica: PoliticaMomentum) -> pd.DataFrame:
    """Features de presión con percentiles estrictamente causales.

    No son probabilidades. El objetivo es producir un laboratorio comparable y
    auditable sin fijar escalas en dólares/puntos por activo.
    """
    if d.empty:
        return d
    out = d.copy()
    r = pd.to_numeric(out["log_return"], errors="coerce").astype(float)
    accel = r.diff()
    short = int(max(2, politica.precursor_short))
    long = int(max(short + 2, politica.precursor_long))
    rv_short = r.rolling(short, min_periods=short).std(ddof=0)
    rv_long = r.rolling(long, min_periods=max(short + 2, long // 2)).std(ddof=0)
    ratio = rv_short / rv_long.replace(0, np.nan)
    # Persistencia direccional observada; 1 = todas las barras recientes mismo signo.
    sign = np.sign(r.fillna(0.0).to_numpy(dtype=float))
    persist = np.full(len(out), np.nan)
    for i in range(len(out)):
        ini = max(0, i - short + 1)
        w = sign[ini:i + 1]
        nz = w[w != 0]
        if nz.size >= 2:
            persist[i] = abs(float(np.sum(nz))) / float(nz.size)

    accel_pct = np.full(len(out), np.nan)
    vol_pct = np.full(len(out), np.nan)
    ret_pct = np.full(len(out), np.nan)
    for i in range(len(out)):
        # Historia previa estricta de cada feature; ventana acotada.
        start = max(0, i - int(politica.ventana_baseline))
        ah = np.abs(accel.iloc[start:i].dropna().to_numpy(dtype=float))
        vh = ratio.iloc[start:i].dropna().to_numpy(dtype=float)
        zh = np.abs(pd.to_numeric(out["z_causal"].iloc[start:i], errors="coerce").dropna().to_numpy(dtype=float))
        if pd.notna(accel.iloc[i]) and ah.size >= 20:
            accel_pct[i] = _percentil_empirico(ah, abs(float(accel.iloc[i])))
        if pd.notna(ratio.iloc[i]) and vh.size >= 20:
            vol_pct[i] = _percentil_empirico(vh, float(ratio.iloc[i]))
        if pd.notna(out["z_causal"].iloc[i]) and zh.size >= 20:
            ret_pct[i] = _percentil_empirico(zh, abs(float(out["z_causal"].iloc[i])))

    out["accel_pct"] = accel_pct
    out["vol_expansion_pct"] = vol_pct
    out["return_pressure_pct"] = ret_pct
    out["persistence"] = persist
    scores = []
    for i in range(len(out)):
        comps = [
            out["accel_pct"].iloc[i],
            out["vol_expansion_pct"].iloc[i],
            out["return_pressure_pct"].iloc[i],
            None if pd.isna(out["persistence"].iloc[i]) else float(out["persistence"].iloc[i]) * 100.0,
        ]
        vals = [float(v) for v in comps if v is not None and math.isfinite(float(v))]
        scores.append(float(np.mean(vals)) if len(vals) >= 3 else float("nan"))
    out["precursor_score"] = scores

    estados = []
    presiones = []
    for i, row in out.iterrows():
        score = float(row["precursor_score"]) if pd.notna(row["precursor_score"]) else float("nan")
        if bool(row.get("anomalia_causal")):
            estado = "TRIGGERED_RETURN_ANOMALY"
        elif math.isfinite(score) and score >= 90.0:
            estado = "BUILDING"
        elif math.isfinite(score) and score >= 75.0:
            estado = "WATCH"
        elif math.isfinite(score):
            estado = "NORMAL"
        else:
            estado = "COLLECTING"
        estados.append(estado)
        ini = max(0, int(i) - short + 1)
        acum = float(r.iloc[ini:int(i) + 1].sum())
        if abs(acum) <= 1e-15:
            presiones.append("MIXTA")
        else:
            presiones.append("ALCISTA" if acum > 0 else "BAJISTA")
    out["momentum_state"] = estados
    out["presion_observada"] = presiones
    return out


def _episodios(d: pd.DataFrame, minutos: int, politica: PoliticaMomentum) -> list[dict[str, Any]]:
    """Agrupa anomalías cercanas; no une eventos entre sesiones ni signos opuestos."""
    if d.empty or "anomalia_causal" not in d:
        return []
    pos = np.flatnonzero(d["anomalia_causal"].to_numpy(dtype=bool))
    if pos.size == 0:
        return []
    grupos: list[list[int]] = []
    actual: list[int] = []
    max_gap = float(minutos) * max(1, int(politica.episodio_gap_barras) + 1)
    for p in pos:
        p = int(p)
        signo = int(np.sign(float(d["log_return"].iloc[p])))
        if not actual:
            actual = [p]
            continue
        q = actual[-1]
        signo_q = int(np.sign(float(d["log_return"].iloc[q])))
        gap = (pd.Timestamp(d["timestamp"].iloc[p]) - pd.Timestamp(d["timestamp"].iloc[q])).total_seconds() / 60.0
        misma_sesion = str(d["poblacion"].iloc[p]) == INTRADIA and str(d["poblacion"].iloc[q]) == INTRADIA
        if gap <= max_gap and signo == signo_q and misma_sesion:
            actual.append(p)
        else:
            grupos.append(actual)
            actual = [p]
    if actual:
        grupos.append(actual)

    ultimo_ts = pd.Timestamp(d["timestamp"].iloc[-1])
    out = []
    for numero, g in enumerate(grupos, start=1):
        zvals = pd.to_numeric(d.loc[g, "z_causal"], errors="coerce").to_numpy(dtype=float)
        ret = pd.to_numeric(d.loc[g, "log_return"], errors="coerce").to_numpy(dtype=float)
        fin = pd.Timestamp(d["timestamp"].iloc[g[-1]])
        activo = (ultimo_ts - fin).total_seconds() / 60.0 <= max_gap
        out.append({
            "id": numero,
            "inicio": pd.Timestamp(d["timestamp"].iloc[g[0]]).isoformat(),
            "fin": fin.isoformat(),
            "barras_anomalas": len(g),
            "peak_abs_z": round(float(np.nanmax(np.abs(zvals))), 3) if np.isfinite(zvals).any() else None,
            "peak_z": round(float(zvals[int(np.nanargmax(np.abs(zvals)))]), 3) if np.isfinite(zvals).any() else None,
            "retorno_acumulado": round(float(np.nansum(ret)), 8),
            "presion_observada": "ALCISTA" if np.nansum(ret) > 0 else "BAJISTA" if np.nansum(ret) < 0 else "MIXTA",
            "estado": "ACTIVO" if activo else "CERRADO",
        })
    return out


def _contexto_flujo(contexto: Optional[dict[str, Any]], presion: str) -> dict[str, Any]:
    """Interpreta un snapshot ya publicado; nunca importa el motor."""
    flow = (contexto or {}).get("flow_kinematics") if isinstance(contexto, dict) else None
    if not isinstance(flow, dict) or not flow.get("ready"):
        return {
            "estado": "SIN_EVIDENCIA_FLOW",
            "alineacion": "UNKNOWN",
            "significancia": None,
            "absorcion": None,
            "nota": "No hay snapshot de flow utilizable; no se infiere nada.",
        }
    latest = flow.get("latest") or {}
    cand = flow.get("candidate") or {}
    side_flow = str(cand.get("observed_side") or latest.get("direction") or "NEUTRAL").upper()
    significativo = cand.get("significant")
    absorcion = cand.get("absorption_score")
    if absorcion is None:
        absorcion = latest.get("absorption_score")
    if isinstance(absorcion, (int, float)) and math.isfinite(float(absorcion)) and float(absorcion) >= 65.0:
        estado = "ABSORPTION_HIGH"
    elif significativo is True:
        estado = "DIRECTIONAL_FLOW_PRESENT"
    elif significativo is False:
        estado = "FLOW_NOT_SIGNIFICANT"
    else:
        estado = "FLOW_UNCALIBRATED"
    esperado = "BUY" if presion == "ALCISTA" else "SELL" if presion == "BAJISTA" else "NEUTRAL"
    if esperado == "NEUTRAL" or side_flow == "NEUTRAL":
        alineacion = "MIXED"
    else:
        alineacion = "ALIGNED" if esperado == side_flow else "CONFLICT"
    return {
        "estado": estado,
        "alineacion": alineacion,
        "significancia": significativo,
        "p_value": cand.get("p_value"),
        "flow_side_observado": side_flow,
        "absorcion": None if absorcion is None else round(float(absorcion), 2),
        "price_coverage": cand.get("absorption_price_coverage"),
        "nota": "Contexto SHADOW. No modifica Scanner ni constituye recomendación direccional.",
    }


def analizar_anomalias_momentum(
    frame: pd.DataFrame,
    *,
    simbolo: str = "UNKNOWN",
    contexto: Optional[dict[str, Any]] = None,
    politica: PoliticaMomentum = POLITICA_MOMENTUM_DEFECTO,
) -> dict[str, Any]:
    """Paquete aislado CURRENT + CANDIDATE para la sección visual.

    ``CURRENT`` conserva v1.27.10. ``CANDIDATE`` es causal, multiframe y
    consciente de estacionalidad/volatilidad. Ninguno tiene autoridad de Scanner.
    """
    current = detectar_anomalias(frame, simbolo=simbolo)
    base: dict[str, Any] = {
        "seccion": "ANOMALIAS_MOMENTUM",
        "simbolo": str(simbolo or "UNKNOWN").upper(),
        "autoridad": MOMENTUM_AUTHORITY,
        "direccion": None,
        "current": resumen_compacto(current),
        "candidate": {
            "estado_modelo": "SHADOW",
            "promocion": "PROHIBIDA_SIN_OOS_PURGE_GAP_BASELINE_GUARDRAILS",
        },
        "politica": politica.as_dict(),
        "timeframes": {},
        "listo": False,
    }
    p = _precio_unico(frame)
    if len(p) < 3:
        return {**base, "estado": "SIN_DATOS", "detalle": "historial de precio insuficiente"}

    for tf in politica.timeframes_min:
        d = _serie_causal(p, int(tf), politica)
        d = _feature_percentiles_causales(d, politica)
        if d.empty:
            base["timeframes"][f"{tf}m"] = {"listo": False, "estado": "SIN_DATOS", "series": [], "episodios": []}
            continue
        ult = d.iloc[-1]
        episodios = _episodios(d, int(tf), politica)
        serie_out = []
        keep = d.tail(int(max(60, politica.max_series)))
        for _, row in keep.iterrows():
            def _n(v, nd=4):
                try:
                    x = float(v)
                    return round(x, nd) if math.isfinite(x) else None
                except Exception:
                    return None
            serie_out.append({
                "timestamp": pd.Timestamp(row["timestamp"]).isoformat(),
                "log_return": _n(row.get("log_return"), 8),
                "z": _n(row.get("z_causal"), 3),
                "umbral": _n(row.get("umbral_causal"), 3),
                "tail_p": _n(row.get("tail_p"), 4),
                "banda_superior": _n(row.get("banda_superior"), 8),
                "banda_inferior": _n(row.get("banda_inferior"), 8),
                "anomalia": bool(row.get("anomalia_causal")),
                "scope": str(row.get("baseline_scope") or "INSUFFICIENT"),
                "regimen_vol": str(row.get("regimen_vol") or "UNKNOWN"),
                "precursor_score": _n(row.get("precursor_score"), 1),
                "momentum_state": str(row.get("momentum_state") or "COLLECTING"),
                "presion_observada": str(row.get("presion_observada") or "MIXTA"),
            })
        ultimo_score = None if pd.isna(ult.get("precursor_score")) else round(float(ult.get("precursor_score")), 1)
        presion = str(ult.get("presion_observada") or "MIXTA")
        base["timeframes"][f"{tf}m"] = {
            "listo": True,
            "estado": str(ult.get("momentum_state") or "COLLECTING"),
            "precursor_score": ultimo_score,
            "presion_observada": presion,
            "baseline_scope": str(ult.get("baseline_scope") or "INSUFFICIENT"),
            "baseline_n": int(ult.get("baseline_n") or 0),
            "finite_sample_floor_pct": round(100.0 / (int(ult.get("baseline_n") or 0) + 1), 3) if int(ult.get("baseline_n") or 0) > 0 else None,
            "regimen_vol": str(ult.get("regimen_vol") or "UNKNOWN"),
            "ultimo_z": None if pd.isna(ult.get("z_causal")) else round(float(ult.get("z_causal")), 3),
            "ultimo_umbral": None if pd.isna(ult.get("umbral_causal")) else round(float(ult.get("umbral_causal")), 3),
            "episodios": episodios[-12:],
            "episodios_totales": len(episodios),
            "series": serie_out,
        }

    # Contexto de flow se aplica solo al timeframe táctico 1m si existe.
    tf1 = base["timeframes"].get("1m") or {}
    presion1 = str(tf1.get("presion_observada") or "MIXTA")
    contexto_flow = _contexto_flujo(contexto, presion1)
    base["candidate"]["contexto_flow"] = contexto_flow
    base["candidate"]["timeframe_tactico"] = "1m"
    base["candidate"]["estado"] = tf1.get("estado", "COLLECTING")
    base["candidate"]["precursor_score"] = tf1.get("precursor_score")
    base["candidate"]["presion_observada"] = presion1
    base["candidate"]["interpretacion"] = (
        "TRIGGERED identifica expansión ya observada. BUILDING/WATCH son hipótesis SHADOW de presión previa; "
        "deben demostrar valor OOS antes de influir en cualquier decisión."
    )
    base["listo"] = any(bool(v.get("listo")) for v in base["timeframes"].values())
    base["estado"] = "OK" if base["listo"] else "MUESTRA_INSUFICIENTE"
    return base

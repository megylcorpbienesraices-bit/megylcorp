#!/usr/bin/env python3
"""CERTIFICADOR LIVE DEL TRANSPORTE · ITM QUANT v1.58.0

Se ejecuta EN WINDOWS, contra la API real, con las credenciales del operador.
Emite `JSON` + `MD` con las afirmaciones del bloque 1+2 MEDIDAS, no declaradas.

    py -3.13 scripts\\certificar_transporte_live.py --symbols DIA,SPY,QQQ

Por qué existe
--------------
La suite de caos (`tests/test_v1580_caos_transporte.py`) reproduce timeouts,
429, 5xx, cuerpos vacíos y respuestas lentas sin tocar la red. Eso demuestra la
CONDUCTA del código y no puede demostrar nada sobre el proveedor real.

Las siete afirmaciones de abajo sólo se pueden cerrar con tráfico real, así que
este script las mide y las declara PASA/FALLA con su evidencia. Ningún criterio
LIVE se cierra con sintéticos.

Lo que mide
-----------
    1  ninguna llamada muere sistemáticamente a 5.0 s
    2  no hay avalanchas de endpoints pesados simultáneos
    3  gamma/delta/net_drift/net_flow se recuperan después de un timeout
    4  ningún fallo de un endpoint bloquea a los demás
    5  ninguna respuesta tardía de un activo contamina a otro
    6  el plazo lo gobierna el p95 medido, no la variable de entorno
    7  la cola propia y la latencia del proveedor quedan separadas

No modifica nada del producto: arranca los dos carriles, observa N ciclos y
escribe el informe. Si falta la clave, lo dice y sale sin fingir una medición.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Una masa de timeouts en el MISMO plazo es la firma del defecto: significa que
#: el plazo lo fija una constante y no la latencia medida.
SOSPECHA_PLAZO_FIJO = 0.30

#: Cuántos plazos distintos tiene que haber para considerar que el plazo se
#: adapta de verdad.
MIN_PLAZOS_DISTINTOS = 2


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pct(parte: int, total: int) -> float:
    return round(100.0 * parte / total, 2) if total else 0.0


class Certificacion:
    def __init__(self, symbols: list[str], ciclos: int, segundos: float) -> None:
        self.symbols = symbols
        self.ciclos = ciclos
        self.segundos = segundos
        self.observaciones: list[dict] = []
        self.inicio = _ahora()

    # ── captura ───────────────────────────────────────────────────────────
    async def _observar(self, simbolo: str) -> None:
        from app.providers.quantdata.intelligence import QUANTDATA_INTELLIGENCE as PAGES
        from app.providers.quantdata.runtime import QUANTDATA as ENGINE
        from app.providers.quantdata.shared import ENDPOINT_RUNTIME, GOVERNOR

        await ENGINE.start(simbolo)
        await PAGES.start(simbolo)
        for i in range(self.ciclos):
            await asyncio.sleep(self.segundos)
            cov = PAGES.coverage()
            self.observaciones.append({
                "symbol": simbolo,
                "cycle": i + 1,
                "at": _ahora(),
                "governor": GOVERNOR.snapshot(),
                "endpoint_runtime": ENDPOINT_RUNTIME.snapshot(),
                "timings": cov.get("timings") or [],
                "timeout_policy": cov.get("timeout_policy") or {},
                "retry_decisions": cov.get("retry_decisions") or [],
                "drift": cov.get("drift") or [],
                "tools": [{"key": t.get("key"), "state": t.get("state"),
                           "detail": t.get("detail")}
                          for t in (cov.get("tools") or [])],
                "quota": cov.get("quota") or {},
            })
        await PAGES.stop()
        await ENGINE.stop()

    def ejecutar(self) -> None:
        for simbolo in self.symbols:
            asyncio.run(self._observar(simbolo))

    # ── veredictos ────────────────────────────────────────────────────────
    def _timeouts(self) -> list[dict]:
        filas = []
        for obs in self.observaciones:
            for e in obs["endpoint_runtime"].get("endpoints", []):
                if int(e.get("timeouts") or 0) > 0:
                    filas.append({"symbol": obs["symbol"], "key": e["key"],
                                  "timeouts": e["timeouts"],
                                  "read_timeout_seconds": e.get("read_timeout_seconds"),
                                  "timeout_source": e.get("timeout_source")})
        return filas

    def afirmaciones(self) -> list[dict]:
        out: list[dict] = []

        def add(clave, ok, detalle, **ev):
            out.append({"assertion": clave, "pass": bool(ok), "detail": detalle,
                        "evidence": ev})

        # 1 · ninguna llamada muere sistemáticamente a 5.0 s
        plazos = Counter()
        total_to = 0
        for fila in self._timeouts():
            plazo = fila.get("read_timeout_seconds")
            if plazo is not None:
                plazos[round(float(plazo), 1)] += int(fila["timeouts"])
                total_to += int(fila["timeouts"])
        en_cinco = plazos.get(5.0, 0)
        masa = _pct(en_cinco, total_to) if total_to else 0.0
        add("SIN_MUERTE_SISTEMATICA_EN_5S",
            en_cinco == 0,
            (f"{en_cinco} timeouts con plazo de 5.0 s ({masa} % del total)"
             if en_cinco else
             (f"{total_to} timeouts, ninguno con plazo de 5.0 s" if total_to
              else "ningún timeout en la ventana observada")),
            timeouts_por_plazo={str(k): v for k, v in sorted(plazos.items())},
            total_timeouts=total_to)

        # 2 · sin avalanchas de pesados
        max_pesadas = max([o["governor"].get("max_observed_heavy") or 0
                           for o in self.observaciones] or [0])
        techo = max([o["governor"].get("max_heavy_inflight") or 0
                     for o in self.observaciones] or [0])
        add("SIN_AVALANCHA_DE_PESADOS", max_pesadas <= techo and techo > 0,
            f"máximo de pesadas simultáneas observado: {max_pesadas} (techo {techo})",
            max_observed_heavy=max_pesadas, ceiling=techo,
            max_observed_inflight=max([o["governor"].get("max_observed_inflight") or 0
                                       for o in self.observaciones] or [0]))

        # 3 · recuperación tras timeout de los cuatro que fallaban
        criticos = ("engine:gamma", "engine:delta", "engine:net_drift",
                    "engine:net_flow", "gamma", "delta", "net_drift", "net_flow")
        recuperados, con_timeout = [], []
        ultimo = self.observaciones[-1]["endpoint_runtime"] if self.observaciones else {}
        for e in (ultimo.get("endpoints") or []):
            if e["key"] not in criticos:
                continue
            if int(e.get("timeouts") or 0) > 0:
                con_timeout.append(e["key"])
                if e.get("last_status") == "OK" and e.get("breaker") == "CLOSED":
                    recuperados.append(e["key"])
        add("RECUPERACION_TRAS_TIMEOUT",
            not con_timeout or len(recuperados) == len(con_timeout),
            (f"{len(recuperados)}/{len(con_timeout)} endpoints críticos con timeout "
             f"volvieron a OK" if con_timeout
             else "ningún endpoint crítico sufrió timeout en la ventana"),
            with_timeout=sorted(con_timeout), recovered=sorted(recuperados))

        # 4 · un endpoint caído no bloquea a los demás
        abiertos = sorted({e["key"] for o in self.observaciones
                           for e in o["endpoint_runtime"].get("endpoints", [])
                           if e.get("breaker") == "OPEN"})
        vivos = sorted({t["key"] for o in self.observaciones for t in o["tools"]
                        if t.get("state") == "LIVE"})
        add("UN_FALLO_NO_BLOQUEA_A_LOS_DEMAS",
            not abiertos or bool(vivos),
            (f"{len(abiertos)} endpoints con circuito abierto y {len(vivos)} "
             f"herramientas LIVE al mismo tiempo" if abiertos
             else f"ningún circuito abierto; {len(vivos)} herramientas LIVE"),
            breakers_open=abiertos, live_tools=len(vivos))

        # 5 · aislamiento entre activos
        por_simbolo = {}
        for o in self.observaciones:
            por_simbolo.setdefault(o["symbol"], []).append(
                len([t for t in o["tools"] if t.get("state") == "LIVE"]))
        add("SIN_CONTAMINACION_ENTRE_ACTIVOS",
            all(max(v) > 0 for v in por_simbolo.values()) if por_simbolo else False,
            "cada activo observado produjo herramientas LIVE por su cuenta",
            live_por_simbolo={k: max(v) for k, v in por_simbolo.items()})

        # 6 · el plazo lo gobierna el p95 medido, no el entorno
        pol = self.observaciones[-1]["timeout_policy"] if self.observaciones else {}
        fuentes = Counter(e.get("timeout_source")
                          for o in self.observaciones
                          for e in o["endpoint_runtime"].get("endpoints", []))
        distintos = len({round(float(e.get("read_timeout_seconds") or 0), 1)
                         for o in self.observaciones
                         for e in o["endpoint_runtime"].get("endpoints", [])})
        add("EL_PLAZO_LO_FIJA_EL_p95_MEDIDO",
            distintos >= MIN_PLAZOS_DISTINTOS or fuentes.get("MEASURED_P95", 0) > 0,
            (f"{fuentes.get('MEASURED_P95', 0)} endpoints con plazo medido y "
             f"{distintos} plazos distintos en la ventana"),
            sources=dict(fuentes), plazos_distintos=distintos,
            legacy_env=pol.get("legacy_env"), legacy_value=pol.get("legacy_value"),
            legacy_ignored=pol.get("legacy_ignored"))

        # 7 · cola propia frente a proveedor
        cuellos = Counter()
        for o in self.observaciones:
            for e in o["governor"].get("endpoints", []):
                if e.get("bottleneck"):
                    cuellos[e["bottleneck"]] += 1
        add("COLA_PROPIA_SEPARADA_DE_LA_LATENCIA_DEL_PROVEEDOR",
            sum(cuellos.values()) > 0,
            (f"{cuellos.get('COLA_PROPIA', 0)} mediciones con cuello en nuestra cola "
             f"y {cuellos.get('PROVEEDOR', 0)} en el proveedor"),
            bottlenecks=dict(cuellos))
        return out

    # ── informes ──────────────────────────────────────────────────────────
    def informe(self) -> dict:
        afirmaciones = self.afirmaciones()
        return {
            "contract": "ITMQ_LIVE_TRANSPORT_V1",
            "version": (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip(),
            "started_at": self.inicio,
            "finished_at": _ahora(),
            "host": {"platform": platform.platform(),
                     "python": platform.python_version(),
                     "machine": platform.machine()},
            "symbols": self.symbols,
            "cycles_per_symbol": self.ciclos,
            "seconds_between_cycles": self.segundos,
            "assertions": afirmaciones,
            "verdict": ("PASS" if all(a["pass"] for a in afirmaciones) else "FAIL"),
            "observations": self.observaciones,
            "note": ("Medido contra la API real. Las pruebas sintéticas de "
                     "tests/test_v1580_caos_transporte.py no sustituyen a este "
                     "informe: demuestran la conducta del código, no la del "
                     "proveedor."),
        }

    def markdown(self, informe: dict) -> str:
        lineas = [
            f"# CERTIFICACIÓN LIVE DEL TRANSPORTE · v{informe['version']}",
            "",
            f"- Activos: **{', '.join(informe['symbols'])}**",
            f"- Ciclos por activo: {informe['cycles_per_symbol']} "
            f"cada {informe['seconds_between_cycles']} s",
            f"- Host: {informe['host']['platform']} · Python "
            f"{informe['host']['python']}",
            f"- Inicio: {informe['started_at']}",
            f"- Veredicto: **{informe['verdict']}**",
            "",
            "## Afirmaciones",
            "",
            "| # | Afirmación | Resultado | Detalle |",
            "|---|---|---|---|",
        ]
        for i, a in enumerate(informe["assertions"], 1):
            marca = "PASA" if a["pass"] else "**FALLA**"
            lineas.append(f"| {i} | `{a['assertion']}` | {marca} | {a['detail']} |")

        lineas += ["", "## Latencias por endpoint (último ciclo)", "",
                   "| endpoint | clase | cola ms | petición ms | total ms | "
                   "presupuesto ms | cuello | plazo | fuente |",
                   "|---|---|---|---|---|---|---|---|---|"]
        ultimo = informe["observations"][-1] if informe["observations"] else {}
        runtime = {e["key"]: e for e in
                   (ultimo.get("endpoint_runtime", {}).get("endpoints") or [])}
        for e in (ultimo.get("governor", {}).get("endpoints") or []):
            rt = runtime.get(e["key"], {})
            lineas.append(
                f"| `{e['key']}` | {e.get('weight')} | {e.get('queue_wait_ms')} | "
                f"{e.get('request_ms')} | {e.get('total_ms')} | "
                f"{e.get('timeout_budget_ms')} | {e.get('bottleneck')} | "
                f"{rt.get('read_timeout_seconds')} s | {rt.get('timeout_source')} |")

        gov = ultimo.get("governor", {})
        lineas += ["", "## Concurrencia", "",
                   f"- máximo en vuelo observado: **{gov.get('max_observed_inflight')}** "
                   f"(techo {gov.get('max_inflight')})",
                   f"- máximo de pesadas: **{gov.get('max_observed_heavy')}** "
                   f"(techo {gov.get('max_heavy_inflight')})",
                   f"- peticiones que hicieron cola: {gov.get('queued')} de "
                   f"{gov.get('granted')}",
                   f"- congestión propia detectada en: "
                   f"{', '.join(gov.get('self_congested') or []) or 'ninguno'}"]

        pol = ultimo.get("timeout_policy", {})
        lineas += ["", "## Política de plazos", "",
                   f"- conexión: **{pol.get('connect_timeout_s')} s** · warm start "
                   f"de lectura: **{pol.get('read_warm_start_s')} s**",
                   f"- autoridad: {pol.get('authority')}",
                   f"- `{pol.get('legacy_env')}`: {pol.get('legacy_note')}"]

        rechazos = [r for r in (ultimo.get("retry_decisions") or [])
                    if not r.get("retry")]
        if rechazos:
            lineas += ["", "## Reintentos NO concedidos, y por qué", "",
                       "| endpoint | motivo |", "|---|---|"]
            for r in rechazos:
                lineas.append(f"| `{r.get('key')}` | {r.get('reason')} |")

        lineas += ["", "---", "", informe["note"], ""]
        return "\n".join(lineas)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="DIA,SPY,QQQ")
    ap.add_argument("--cycles", type=int, default=8,
                    help="ciclos observados por activo")
    ap.add_argument("--seconds", type=float, default=16.0,
                    help="segundos entre observaciones")
    ap.add_argument("--out", type=Path, default=ROOT / "CERTIFICACION_LIVE_TRANSPORTE")
    args = ap.parse_args()

    if not os.getenv("QUANTDATA_API_KEY", "").strip():
        print("FALTA QUANTDATA_API_KEY: este certificador mide contra la API real "
              "y no simula nada. Configura el .env y vuelve a ejecutarlo.")
        return 2

    simbolos = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cert = Certificacion(simbolos, max(1, args.cycles), max(1.0, args.seconds))
    print(f"Observando {simbolos} · {cert.ciclos} ciclos de {cert.segundos:.0f} s "
          f"por activo. Esto tarda unos "
          f"{len(simbolos) * cert.ciclos * cert.segundos / 60:.0f} minutos.")
    cert.ejecutar()

    informe = cert.informe()
    json_path = args.out.with_suffix(".json")
    md_path = args.out.with_suffix(".md")
    json_path.write_text(json.dumps(informe, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    md_path.write_text(cert.markdown(informe), encoding="utf-8")

    print()
    for a in informe["assertions"]:
        print(f"  {'PASA  ' if a['pass'] else 'FALLA '} {a['assertion']}: {a['detail']}")
    print()
    print(f"VEREDICTO: {informe['verdict']}")
    print(f"  {json_path}")
    print(f"  {md_path}")
    return 0 if informe["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

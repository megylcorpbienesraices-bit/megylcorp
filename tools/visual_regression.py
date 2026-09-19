#!/usr/bin/env python3
"""REGRESIÓN VISUAL · ITM QUANT v1.47.0

PARA QUÉ SIRVE
--------------
La suite numérica estaba en verde mientras las barras se veían como rayitas. El
dato estaba, la escala era correcta, el suelo «existía»… y el resultado en
pantalla era ilegible. Ninguna prueba podía detectarlo porque ninguna miraba la
GEOMETRÍA que se acaba dibujando.

Esto la mira. Sirve `tools/visual_harness.html` —que alimenta los
renderizadores REALES, no una copia— y lee, panel a panel, el plan que el
componente calculó con el ancho MEDIDO del lienzo:

    grosor de barra · hueco entre barras · ocupación · agrupación aplicada

Cubre las combinaciones que rompen: cinco activos de 10⁹ a 10⁴, cinco formas de
distribución (normal, cola pesada, un dominante, muy disperso, valores
diminutos), densidades de 12 a 780 observaciones y cinco viewports.

CÓMO SE USA
-----------
    python tools/visual_regression.py              # informe legible
    python tools/visual_regression.py --json       # para la suite
    python tools/visual_regression.py --png out.png

Devuelve 1 si algún panel dibuja barras por debajo del mínimo legible, barras
solapadas o barras sin separación. NO MODIFICA NADA.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import http.server
import json
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM = "/opt/pw-browsers/chromium"

# Los mismos umbrales que declara el componente. Si allí cambian, aquí falla, y
# eso es lo correcto: son la definición de «legible».
MIN_BAR_PX = 5.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _serve(root: Path):
    """Sirve el arnés y `/static` sin tocar el proyecto ni pedir un servidor vivo."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            clean = path.split("?", 1)[0].split("#", 1)[0]
            if clean.startswith("/static/"):
                return str(root / "app" / "static" / clean[len("/static/"):])
            if clean in ("/", "/index.html"):
                return str(root / "tools" / "visual_harness.html")
            return str(root / clean.lstrip("/"))

        def log_message(self, *_args):
            pass

    port = _free_port()
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            httpd.shutdown()


async def _measure(url: str, png: str | None, wait_ms: int):
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        launch = {"executable_path": CHROMIUM} if Path(CHROMIUM).exists() else {}
        browser = await p.chromium.launch(**launch)
        page = await browser.new_page(viewport={"width": 1900, "height": 1200})
        # Un error de JAVASCRIPT invalida la medición: si el renderizador se
        # rompió, la geometría que informe no describe lo que vería nadie. Un
        # 404 de recurso —el favicon que el navegador pide siempre— no lo es, y
        # tratarlo como tal dejaba la comprobación sin ejecutarse nunca.
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(f"console: {m.text}")
                if (m.type == "error" and "Failed to load resource" not in m.text) else None)
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_function("() => window.__harnessReady === true", timeout=20_000)
        await page.wait_for_timeout(wait_ms)
        report = await page.evaluate("() => window.__harnessReport()")
        if png:
            await page.screenshot(path=png, full_page=True)

        # Item 17 · RESIZE. Los paneles miden su lienzo en cada fotograma, así
        # que el plan tiene que recalcularse solo. Se comprueba, no se supone:
        # una geometría correcta sólo al primer render no sirve de nada en una
        # terminal que se redimensiona.
        await page.set_viewport_size({"width": 1100, "height": 900})
        await page.wait_for_timeout(600)
        resized = await page.evaluate("() => window.__harnessReport()")

        await browser.close()
        for r in resized:
            r["phase"] = "resize"
        return report + resized, errors


def evaluate(report: list[dict]) -> list[dict]:
    """Qué paneles fallan, y por qué. Un fallo sin motivo no se puede corregir."""
    bad = []
    for r in report:
        why = []
        if r["thickness"] < MIN_BAR_PX:
            why.append(f"barra de {r['thickness']:.2f}px (mínimo {MIN_BAR_PX})")
        if r["occupancy"] > 1.0:
            why.append(f"barras solapadas: ocupación {r['occupancy']:.3f}")
        if r["bins"] > 1 and r["gap"] <= 0:
            why.append("sin separación entre barras")
        if why:
            bad.append({**r, "why": " · ".join(why)})
    return bad


def render(report: list[dict], bad: list[dict]) -> str:
    lines = ["", "=" * 96, "REGRESIÓN VISUAL · GEOMETRÍA REAL DE LOS RENDERIZADORES", "=" * 96,
             f"{'PANEL':<24}{'TIPO':<7}{'N':>6}{'BARRAS':>8}{'GROSOR':>9}{'HUECO':>8}{'OCUP':>7}"]
    lines.append("-" * 96)
    for r in report:
        lines.append(f"{str(r['name'])[:23]:<24}{r['kind']:<7}{r['n']:>6}{r['bins']:>8}"
                     f"{r['thickness']:>8.2f}px{r['gap']:>7.2f}{r['occupancy']:>7.2f}")
    lines.append("-" * 96)
    thick = [r["thickness"] for r in report]
    first = [r for r in report if r.get("phase") != "resize"]
    after = [r for r in report if r.get("phase") == "resize"]
    lines.append(f"{len(first)} paneles + {len(after)} tras redimensionar · "
                 f"grosor {min(thick):.2f}–{max(thick):.2f}px · "
                 f"ocupación máxima {max(r['occupancy'] for r in report):.3f}")
    if bad:
        lines.append("")
        lines.append(f"FALLAN {len(bad)}:")
        for r in bad:
            lines.append(f"  {r['name']} ({r['n']} obs en {r['extent_px']:.0f}px): {r['why']}")
    else:
        lines.append("Ningún panel dibuja rayitas, masa sólida ni barras sin separación.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="informe crudo para la suite")
    ap.add_argument("--png", default=None, help="guarda también la captura completa")
    ap.add_argument("--wait", type=int, default=1500, help="ms de espera tras cargar")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("playwright no está instalado", file=sys.stderr)
        return 2

    with _serve(ROOT) as url:
        report, errors = asyncio.run(_measure(url, args.png, args.wait))

    if errors:
        print("errores de página: " + " | ".join(errors[:5]), file=sys.stderr)
        return 2
    if not report:
        print("el arnés no devolvió ningún panel", file=sys.stderr)
        return 2

    bad = evaluate(report)
    if args.json:
        print(json.dumps(report))
    else:
        print(render(report, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

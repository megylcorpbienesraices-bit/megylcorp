from __future__ import annotations

from pathlib import Path
import os
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox
import sys

BASE = Path(__file__).resolve().parent
CURRENT_VERSION = (BASE / "VERSION.txt").read_text(encoding="utf-8").strip() if (BASE / "VERSION.txt").exists() else "unknown"
ENV_FILE = BASE / ".env"
GLOBAL_DIR = Path.home() / ".itm_quant_gamma"
GLOBAL_ENV = GLOBAL_DIR / "alpaca.env"  # backward-compatible shared local credential file


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception as exc:
        print(f"[ITM][WARN] operación auxiliar falló: {type(exc).__name__}: {exc}", file=sys.stderr)
    return out


def existing_settings() -> dict[str, str]:
    merged: dict[str, str] = {}
    for p in (GLOBAL_ENV, ENV_FILE):
        merged.update(read_env(p))
    return merged


def _merge_env(path: Path, updates: dict[str, str]) -> None:
    """Update only owned credential keys; preserve every other ITM QUANT setting."""
    old_lines = []
    if path.exists():
        try:
            old_lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            print(f"[ITM][WARN] no se pudo leer configuración existente: {type(exc).__name__}: {exc}", file=sys.stderr)
            old_lines = []
    seen: set[str] = set()
    out: list[str] = []
    for raw in old_lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                out.append(f"{k}={updates[k]}")
                seen.add(k)
                continue
        out.append(raw)
    missing = [k for k in updates if k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append(f"# === ITM QUANT · CENTRO DE PROVEEDORES v{CURRENT_VERSION} ===")
        out.extend(f"{k}={updates[k]}" for k in missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except Exception as exc:
        print(f"[ITM][WARN] operación auxiliar falló: {type(exc).__name__}: {exc}", file=sys.stderr)


def save_settings(values: dict[str, str]) -> None:
    api_key = values["ALPACA_API_KEY"].strip()
    secret_key = values["ALPACA_SECRET_KEY"].strip()
    quantdata_key = values.get("QUANTDATA_API_KEY", "").strip()
    tasty_id = values["TASTYTRADE_CLIENT_ID"].strip()
    tasty_secret = values["TASTYTRADE_CLIENT_SECRET"].strip()
    tasty_refresh = values["TASTYTRADE_REFRESH_TOKEN"].strip()

    if len(api_key) < 8:
        raise ValueError("La Alpaca API Key parece incompleta.")
    if len(secret_key) < 12:
        raise ValueError("La Alpaca Secret Key parece incompleta.")
    if quantdata_key and not (quantdata_key.startswith("qd_") and len(quantdata_key) == 35 and quantdata_key[3:].isalnum()):
        raise ValueError("La Quant Data API Key no tiene el formato esperado qd_ + 32 caracteres.")
    tasty_any = any((tasty_id, tasty_secret, tasty_refresh))
    tasty_all = all((tasty_secret, tasty_refresh))  # Client ID can be optional in refresh flow.
    if tasty_any and not tasty_all:
        raise ValueError("Tastytrade está incompleto: agrega Client Secret y Refresh Token. Client ID puede quedar vacío si tu OAuth no lo requiere.")
    # but do not validate/activate them until the user explicitly approves that provider.

    updates = {
        "APP_NAME": "ITM QUANT MULTI ASSET",
        "APP_ENV": "local",
        "DATA_MODE": "live",
        "ALPACA_API_KEY": api_key,
        "ALPACA_SECRET_KEY": secret_key,
        "ALPACA_STOCK_FEED": "sip",
        "ALPACA_OPTIONS_FEED": "opra",
        "QUANTDATA_ENABLED": "1" if quantdata_key else "0",
        "QUANTDATA_API_KEY": quantdata_key,
        "QUANTDATA_BASE_URL": values.get("QUANTDATA_BASE_URL", "https://api.quantdata.us").strip() or "https://api.quantdata.us",
        "QUANTDATA_REFRESH_SECONDS": "15",
        # Plazo inicial; cada endpoint calibra el suyo con la latencia medida.
        # 5 s cortaba los endpoints pesados en cada ciclo. Ver .env.example.
        "QUANTDATA_TIMEOUT_SECONDS": "12",
        # tastytrade is read-only market data in this build.
        "TASTYTRADE_ENABLED": "1" if tasty_all else "0",
        "TASTYTRADE_CLIENT_ID": tasty_id,
        "TASTYTRADE_CLIENT_SECRET": tasty_secret,
        "TASTYTRADE_REFRESH_TOKEN": tasty_refresh,
        "TASTYTRADE_BASE_URL": values.get("TASTYTRADE_BASE_URL", "https://api.tastyworks.com").strip() or "https://api.tastyworks.com",
        "TASTYTRADE_USER_AGENT": f"ITM-QUANT/{CURRENT_VERSION}",
        "TASTYTRADE_STREAM_DERIVATIVES": "1",
        # WARM/COLD provider library + loss-audited data lake.
        "PROVIDER_LIBRARY_ENABLED": "1",
        "PROVIDER_LIBRARY_ASSET_INTERVAL_SECONDS": "75",
        "PROVIDER_LIBRARY_ALPACA_CATALOG_DAYS": "365",
        "PROVIDER_LIBRARY_ALPACA_SNAPSHOT_DAYS": "30",
        "PROVIDER_DATA_LAKE_QUEUE": "250000",
        "TASTYTRADE_CATALOG_ENABLED": "1",
        "TASTYTRADE_CATALOG_REFRESH_MINUTES": "60",
        "TASTYTRADE_CATALOG_ASSET_DELAY_MS": "150",
    }
    _merge_env(ENV_FILE, updates)
    GLOBAL_DIR.mkdir(parents=True, exist_ok=True)
    _merge_env(GLOBAL_ENV, updates)


class SecretRow(ttk.Frame):
    def __init__(self, master, label: str, variable: tk.StringVar, *, secret: bool = True, hint: str = ""):
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        ttk.Label(self, text=label, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.entry = ttk.Entry(self, textvariable=variable, show="•" if secret else "", font=("Consolas", 10))
        self.entry.grid(row=1, column=0, sticky="ew")
        if secret:
            self.show_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(self, text="Mostrar", variable=self.show_var,
                            command=lambda: self.entry.configure(show="" if self.show_var.get() else "•")).grid(row=1, column=1, padx=(8, 0))
        if hint:
            ttk.Label(self, text=hint, foreground="#5b6470", font=("Segoe UI", 8)).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ITM QUANT — Centro de proveedores")
        self.geometry("940x920")
        self.minsize(840, 720)
        self.configure(padx=22, pady=18)

        old = existing_settings()
        self.vars = {
            "ALPACA_API_KEY": tk.StringVar(value=old.get("ALPACA_API_KEY", "")),
            "ALPACA_SECRET_KEY": tk.StringVar(value=old.get("ALPACA_SECRET_KEY", "")),
            "QUANTDATA_API_KEY": tk.StringVar(value=old.get("QUANTDATA_API_KEY", "")),
            "QUANTDATA_BASE_URL": tk.StringVar(value=old.get("QUANTDATA_BASE_URL", "https://api.quantdata.us")),
            "TASTYTRADE_CLIENT_ID": tk.StringVar(value=old.get("TASTYTRADE_CLIENT_ID", "")),
            "TASTYTRADE_CLIENT_SECRET": tk.StringVar(value=old.get("TASTYTRADE_CLIENT_SECRET", "")),
            "TASTYTRADE_REFRESH_TOKEN": tk.StringVar(value=old.get("TASTYTRADE_REFRESH_TOKEN", "")),
            "TASTYTRADE_BASE_URL": tk.StringVar(value=old.get("TASTYTRADE_BASE_URL", "https://api.tastyworks.com")),
        }
        self.status_var = tk.StringVar(value="Las credenciales se guardan localmente. No se imprimen tokens ni secrets en logs.")

        ttk.Label(self, text="ITM QUANT — CENTRO DE PROVEEDORES", font=("Segoe UI", 20, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(
            self,
            text=("Todos los proveedores configurados participan en la misma arquitectura de datos. No existe una jerarquía fija de principal/secundario/terciario: "
                  "ITM QUANT evalúa freshness, latencia, integridad y divergencia por observación antes de fusionar datos comparables."),
            wraplength=840, font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(0, 14))

        canvas = tk.Canvas(self, highlightthickness=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        alp = ttk.LabelFrame(body, text="ALPACA · SIP + OPRA · PROVEEDOR INTEGRADO", padding=14)
        alp.pack(fill="x", pady=(0, 12))
        SecretRow(alp, "Alpaca API Key", self.vars["ALPACA_API_KEY"], secret=False).pack(fill="x", pady=(0, 9))
        SecretRow(alp, "Alpaca Secret Key", self.vars["ALPACA_SECRET_KEY"]).pack(fill="x")


        qd = ttk.LabelFrame(body, text="QUANT DATA · INTELIGENCIA DE OPCIONES · INTERNO", padding=14)
        qd.pack(fill="x", pady=(0, 12))
        SecretRow(qd, "Quant Data API Key", self.vars["QUANTDATA_API_KEY"],
                  hint="Se usa internamente para exposición, flujo, migración Gamma, IV y Max Pain. No aparece en la vista del analista.").pack(fill="x", pady=(0, 9))
        SecretRow(qd, "Quant Data Base URL", self.vars["QUANTDATA_BASE_URL"], secret=False).pack(fill="x")

        tt = ttk.LabelFrame(body, text="TASTYTRADE · OAUTH2 READ ONLY + DXLINK · PROVEEDOR INTEGRADO", padding=14)
        tt.pack(fill="x", pady=(0, 12))
        SecretRow(tt, "Tastytrade Client ID", self.vars["TASTYTRADE_CLIENT_ID"], secret=False,
                  hint="Puede quedar vacío si el refresh de tu OAuth no lo requiere.").pack(fill="x", pady=(0, 9))
        SecretRow(tt, "Tastytrade Client Secret", self.vars["TASTYTRADE_CLIENT_SECRET"]).pack(fill="x", pady=(0, 9))
        SecretRow(tt, "Tastytrade Refresh Token", self.vars["TASTYTRADE_REFRESH_TOKEN"]).pack(fill="x", pady=(0, 9))
        SecretRow(tt, "Tastytrade Base URL", self.vars["TASTYTRADE_BASE_URL"], secret=False).pack(fill="x")
        ttk.Label(tt, text="Scope esperado: READ ONLY. ITM QUANT no envía órdenes desde este conector.", foreground="#5b6470", font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 0))

        info = ttk.LabelFrame(body, text="FUENTES SIN CREDENCIAL LOCAL", padding=14)
        info.pack(fill="x", pady=(0, 12))
        ttk.Label(info, text="FRED · BLS · Federal Reserve: contexto macro oficial. Se integran cuando el módulo correspondiente dispone de datos válidos.", wraplength=790).pack(anchor="w")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(4, 10))
        ttk.Button(buttons, text="GUARDAR CREDENCIALES", command=self.save).pack(side="left")
        ttk.Button(buttons, text="VALIDAR CAMPOS", command=self.validate_fields).pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="AUDITOR DE COBERTURA", command=lambda: webbrowser.open("http://127.0.0.1:8000/providers/coverage")).pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="ESTADO PROVEEDORES", command=lambda: webbrowser.open("http://127.0.0.1:8000/api/providers/status")).pack(side="left", padx=(10, 0))
        ttk.Button(buttons, text="CERRAR", command=self.destroy).pack(side="left", padx=(10, 0))
        ttk.Separator(body).pack(fill="x", pady=(4, 10))
        ttk.Label(body, textvariable=self.status_var, wraplength=820, font=("Segoe UI", 10)).pack(anchor="w")
        ttk.Label(body, text=("Regla vigente: un proveedor sin dato fresco queda temporalmente fuera de esa observación, pero no existe un rango fijo entre proveedores. "
                              "HOT usa solo lo necesario para LIVE; WARM/COLD recopila catálogos e histórico en segundo plano. El Auditor muestra cobertura real y cualquier pérdida de archivo. "
                              "Datos incompatibles (ETF, índice, futuro, opciones) se normalizan antes de fusionarse; no se suman ciegamente."),
                  wraplength=820, foreground="#5b6470", font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 16))

        self.after(200, lambda: self.focus_force())

    def _values(self) -> dict[str, str]:
        return {k: v.get() for k, v in self.vars.items()}

    def validate_fields(self):
        v = self._values()
        messages = []
        messages.append("Alpaca: LISTO" if len(v["ALPACA_API_KEY"].strip()) >= 8 and len(v["ALPACA_SECRET_KEY"].strip()) >= 12 else "Alpaca: INCOMPLETO")
        qd=v.get("QUANTDATA_API_KEY", "").strip(); messages.append("Quant Data: LISTO" if qd.startswith("qd_") and len(qd)==35 else "Quant Data: SIN API KEY")
        tasty = bool(v["TASTYTRADE_CLIENT_SECRET"].strip() and v["TASTYTRADE_REFRESH_TOKEN"].strip())
        messages.append("Tastytrade: LISTO PARA OAUTH" if tasty else "Tastytrade: INCOMPLETO")
        self.status_var.set(" · ".join(messages))

    def save(self):
        try:
            save_settings(self._values())
        except Exception as exc:
            messagebox.showerror("No se pudo guardar", str(exc), parent=self)
            return
        self.status_var.set("✅ Credenciales guardadas sin borrar el resto del .env. Reinicia ITM QUANT para recargar proveedores.")
        messagebox.showinfo("Configuración lista", "Fuentes guardadas localmente.\n\nReinicia ITM QUANT con INICIAR_WEB.bat para aplicar los cambios.", parent=self)


if __name__ == "__main__":
    App().mainloop()

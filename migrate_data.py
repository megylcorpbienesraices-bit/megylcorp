from __future__ import annotations
from pathlib import Path
import sys

from app.persistence import migrate_from_path, PERSISTENT_ROOT


def main() -> int:
    print("\nITM QUANT · MIGRACIÓN SEGURA DE MEMORIA CUANTITATIVA")
    print("No escanea carpetas vecinas. Tú eliges exactamente la instalación anterior.\n")
    raw = " ".join(sys.argv[1:]).strip().strip('"')
    if not raw:
        raw = input("Pega la ruta de la carpeta ITM QUANT anterior (ej. ...v1.16.8):\n> ").strip().strip('"')
    if not raw:
        print("Cancelado: no se indicó una fuente.")
        return 2
    src = Path(raw).expanduser()
    print(f"\nFuente seleccionada: {src}")
    print(f"Destino persistente: {PERSISTENT_ROOT}")
    confirm = input("Escribe MIGRAR para continuar: ").strip().upper()
    if confirm != "MIGRAR":
        print("Cancelado. No se modificó ningún dato.")
        return 3
    try:
        r = migrate_from_path(src)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        print("No se borró el origen.")
        return 1
    print("\nMigración terminada.")
    print("Estado:", r.status)
    print("Archivos migrados:", r.migrated_files)
    print("Idénticos omitidos:", r.skipped_identical)
    print("Conflictos preservados:", r.conflicts_preserved)
    print("Backup:", r.backup or "no requerido")
    print("\nRegla: NO RESET LIVE / AUDITOR / CALIBRATION / REPLAY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

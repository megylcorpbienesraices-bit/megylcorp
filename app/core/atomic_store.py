"""Persistencia atómica y de esquema estable (v1.42.1).

POR QUÉ APARECÍAN FILAS MALFORMADAS
-----------------------------------
En el arranque se leía, cada sesión:

    scanner_history_dia_2026-09-09.csv · 22 fila(s) malformada(s) aisladas
                                       · 400 fila(s) válidas preservadas

El lector hacía lo correcto: aislar lo dañado y conservar lo bueno. Pero un lector
excelente limpiando tras un escritor que sigue corrompiendo no es una solución, es
una tirita. Las 22 filas tenían dos causas, las dos en el escritor:

1. ESQUEMA VARIABLE EN MODO APPEND. El registro del Scanner añade columnas
   dinámicas —`feature_*` según el snapshot, `stop_shadow_k_*` según cuántas
   sombras haya—. La cabecera se escribe UNA vez, con las columnas del primer
   ciclo. Si un ciclo posterior trae una feature más, pandas escribe una fila con
   más campos que la cabecera. Eso es, literalmente, una fila malformada — y no da
   ningún error al escribirla.

2. ESCRITURA NO ATÓMICA. Un `mode="a"` sin `fsync` puede quedar a medias si el
   proceso muere o el host se apaga mientras el búfer está sin volcar. La fila
   queda cortada por la mitad.

CÓMO SE ARREGLA
---------------
- La fila se ALINEA con la cabecera existente antes de escribirse. Si trae columnas
  nuevas, el fichero se reescribe entero con el esquema ampliado (operación rara y
  atómica), en vez de añadir una fila incoherente.
- La escritura es `temp → fsync → rename`, que en POSIX y en NTFS es atómica: o
  está el fichero anterior, o está el nuevo. Nunca medio fichero.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .obs import note as _obs_note


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """temp → fsync → rename. Nunca deja un fichero a medias."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError as cleanup_exc:
            # El temporal quedó huérfano; el fichero real sigue intacto, que es lo
            # que importa. Se anota para que no desaparezca sin rastro.
            _obs_note("atomic_store:tmp_cleanup", cleanup_exc, severity="DEGRADED")
        raise
    # POSIX permite fsync del directorio para endurecer la durabilidad del rename.
    # Windows/NTFS no expone esa misma operación mediante os.open sobre carpetas;
    # intentarlo produce PermissionError [Errno 13] aun cuando la escritura y el
    # os.replace ya terminaron correctamente. No es degradación y no debe ensuciar
    # la consola del motor con warnings falsos.
    if os.name != "nt":
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError) as exc:
            _obs_note("atomic_store:dir_fsync", exc, severity="DEGRADED")
    return path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def read_header(path: Path) -> Optional[List[str]]:
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return None
    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            first = fh.readline()
        if not first.strip():
            return None
        return next(csv.reader(io.StringIO(first)))
    except (OSError, StopIteration, csv.Error) as exc:
        _obs_note("atomic_store:read_header", exc, severity="DEGRADED")
        return None


def _render_row(values: Iterable[Any]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(
        ["" if v is None else v for v in values])
    return buf.getvalue()


def append_row(path: Path, row: Dict[str, Any]) -> Dict[str, Any]:
    """Añade una fila manteniendo el esquema coherente con la cabecera.

    Tres caminos, y ninguno puede producir una fila con un número de campos
    distinto al de la cabecera —que era el origen de las filas malformadas—:

      fichero nuevo      se escribe cabecera + fila, atómicamente
      mismas columnas    se añade la fila en una sola escritura con fsync
      columnas nuevas    se reescribe el fichero entero con el esquema ampliado
    """
    p = Path(path)
    header = read_header(p)

    if header is None:
        cols = list(row.keys())
        payload = _render_row(cols) + _render_row(row.get(c) for c in cols)
        atomic_write_bytes(p, payload.encode("utf-8"))
        return {"ok": True, "mode": "CREATED", "columns": len(cols), "rewritten": False}

    new_cols = [c for c in row.keys() if c not in header]
    if new_cols:
        # El esquema creció. Reescribir es caro, pero ocurre pocas veces y es la
        # única forma de que las filas antiguas y las nuevas sigan siendo legibles
        # por el mismo parser. Añadir a ciegas es lo que rompía el fichero.
        try:
            with p.open("r", encoding="utf-8", newline="") as fh:
                existing = list(csv.DictReader(fh))
        except (OSError, csv.Error) as exc:
            _obs_note("atomic_store:reschema_read", exc, severity="DEGRADED")
            return {"ok": False, "mode": "RESCHEMA_FAILED", "error": str(exc)[:160]}
        cols = header + new_cols
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(cols)
        for old in existing:
            w.writerow([old.get(c, "") if old.get(c) is not None else "" for c in cols])
        w.writerow(["" if row.get(c) is None else row.get(c) for c in cols])
        atomic_write_bytes(p, out.getvalue().encode("utf-8"))
        return {"ok": True, "mode": "RESCHEMA", "columns": len(cols),
                "added_columns": new_cols, "rewritten": True,
                "rows_preserved": len(existing)}

    line = _render_row(row.get(c) for c in header).encode("utf-8")
    try:
        # O_APPEND garantiza que la escritura no se entrelaza con otro proceso; el
        # fsync garantiza que no queda a medias si el host se apaga.
        fd = os.open(str(p), os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        _obs_note("atomic_store:append", exc, severity="DEGRADED")
        return {"ok": False, "mode": "APPEND_FAILED", "error": str(exc)[:160]}
    return {"ok": True, "mode": "APPENDED", "columns": len(header), "rewritten": False}


def verify(path: Path) -> Dict[str, Any]:
    """Cuenta filas con un número de campos distinto al de la cabecera.

    Es la comprobación que convierte «el lector aisló 22 filas» en «el escritor
    produjo 22 filas incoherentes», que es lo que de verdad hay que arreglar.
    """
    p = Path(path)
    header = read_header(p)
    if header is None:
        return {"ok": False, "reason": "sin cabecera legible", "rows": 0, "malformed": 0}
    good = bad = 0
    try:
        with p.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for rec in reader:
                if len(rec) == len(header):
                    good += 1
                else:
                    bad += 1
    except (OSError, csv.Error) as exc:
        return {"ok": False, "reason": str(exc)[:160], "rows": good, "malformed": bad}
    return {"ok": bad == 0, "rows": good, "malformed": bad, "columns": len(header),
            "detail": ("esquema coherente" if bad == 0 else
                       f"{bad} filas con un número de campos distinto al de la cabecera")}

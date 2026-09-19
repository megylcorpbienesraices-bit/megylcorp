from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .settings import QuantDataSettings
from ...version import APP_VERSION


class QuantDataError(RuntimeError):
    """Fallo del proveedor, con lo que dijo el proveedor intacto.

    `validation_fields` lleva los campos concretos que rechazó, que es lo único
    con lo que se puede corregir un 400 sin adivinar.
    """

    status_code: int | None = None
    validation_fields: list[str] | None = None
    error_detail: dict | None = None
    body: Any = None


@dataclass
class QuantDataResponse:
    payload: dict[str, Any]
    status_code: int
    remaining: int | None = None
    limit: int | None = None
    reset_seconds: float | None = None


# Claves bajo las que los proveedores publican el detalle de una validación
# fallida. No hay una sola convención, y fijar una era garantizar perderlo.
_ERROR_KEYS = ("detail", "title", "message", "error", "description", "reason")
_FIELD_KEYS = ("errors", "validationErrors", "validation_errors", "fields",
               "invalidFields", "issues", "violations", "detail")


def _safe_body(response: httpx.Response, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text[:limit]


def _field_errors(node: Any, depth: int = 0) -> list[str]:
    """Errores por CAMPO, en la forma que los publique el proveedor.

    Pydantic/FastAPI usan `{"loc": [...], "msg": ...}`; otros usan
    `{"field": ..., "message": ...}`. Se reconocen las dos y se recorre en
    profundidad, porque el detalle suele venir anidado bajo `detail` o `errors`.
    """
    out: list[str] = []
    if depth > 4 or node is None:
        return out
    if isinstance(node, str):
        return [node] if node.strip() else []
    if isinstance(node, list):
        for item in node[:20]:
            out.extend(_field_errors(item, depth + 1))
        return out
    if not isinstance(node, dict):
        return out
    loc = node.get("loc") or node.get("field") or node.get("path") or node.get("name")
    msg = next((node[k] for k in ("msg", "message", "error", "detail", "reason")
                if isinstance(node.get(k), str)), None)
    if loc is not None and msg:
        where = ".".join(str(x) for x in loc) if isinstance(loc, (list, tuple)) else str(loc)
        out.append(f"{where}: {msg}")
        return out
    for key in _FIELD_KEYS:
        child = node.get(key)
        # Una cadena suelta bajo `detail` ya es el titular; repetirla como si fuera
        # un error de campo llenaría el diagnóstico de ruido y escondería el campo
        # real, que es lo único que sirve para corregir la petición.
        if isinstance(child, (list, dict)):
            out.extend(_field_errors(child, depth + 1))
    return out


def _describe_error(response: httpx.Response) -> tuple[str, list[str]]:
    """Mensaje accionable + lista de campos que el proveedor rechazó."""
    body = _safe_body(response)
    if isinstance(body, str):
        return body[:300], []
    if not isinstance(body, dict):
        return str(body)[:300], []
    headline = ""
    for key in _ERROR_KEYS:
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            headline = v.strip()
            break
    fields = _field_errors(body)
    # El titular sin los campos es lo que dejaba al operador sin nada que hacer.
    if fields:
        joined = " · ".join(fields[:6])
        return (f"{headline} [{joined}]" if headline else joined)[:400], fields
    return (headline or str(body)[:300])[:400], fields


def _structured_error(body: Any, status: int) -> dict:
    """El cuerpo de un error, desglosado en lo que hace falta para actuar.

    `type` y `detail` sitúan el fallo; `errors[]` dice qué campo concreto lo
    provocó. Un 400 sin esa lista no se puede corregir, y con ella la corrección
    es de una línea.
    """
    out = {"status": status, "type": "", "detail": "", "errors": [], "raw": body}
    if not isinstance(body, dict):
        out["detail"] = str(body)[:400]
        return out
    out["type"] = str(body.get("type") or body.get("title") or "")[:200]
    d = body.get("detail")
    out["detail"] = str(d)[:400] if isinstance(d, str) else ""
    rows = body.get("errors")
    if not isinstance(rows, list) and isinstance(d, list):
        rows = d                     # convención Pydantic: la lista va en `detail`
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        field = r.get("field")
        if field is None and isinstance(r.get("loc"), (list, tuple)):
            field = ".".join(str(x) for x in r["loc"])
        out["errors"].append({
            "field": str(field or "")[:120],
            "message": str(r.get("message") or r.get("msg") or "")[:240],
        })
    return out


class QuantDataClient:
    def __init__(self, settings: QuantDataSettings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.base_url,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": f"ITM-QUANT/{APP_VERSION}",
                },
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                follow_redirects=False,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _int_header(headers: httpx.Headers, name: str) -> int | None:
        try:
            return int(headers.get(name, ""))
        except Exception:
            return None

    @staticmethod
    def _float_header(headers: httpx.Headers, name: str) -> float | None:
        try:
            return float(headers.get(name, ""))
        except Exception:
            return None

    async def post(self, path: str, body: dict[str, Any]) -> QuantDataResponse:
        if not self.settings.configured:
            raise QuantDataError("Quant Data is not configured")
        await self.start()
        assert self._client is not None
        try:
            response = await self._client.post(path, json=body)
        except httpx.TimeoutException as exc:
            raise QuantDataError("Quant Data request timed out") from exc
        except httpx.HTTPError as exc:
            raise QuantDataError(f"Quant Data transport error: {type(exc).__name__}") from exc

        remaining = self._int_header(response.headers, "X-RateLimit-Remaining")
        limit = self._int_header(response.headers, "X-RateLimit-Limit")
        reset = self._float_header(response.headers, "X-RateLimit-Reset")

        if response.status_code == 429:
            retry_after = self._float_header(response.headers, "Retry-After")
            wait = retry_after if retry_after is not None else reset
            # El límite es de la cuenta: frenar sólo al carril que recibió el 429
            # dejaría al otro gastando peticiones que ya se sabe que van a fallar.
            from .shared import QUOTA
            QUOTA.note_rate_limited(wait)
            raise QuantDataError(f"Quant Data rate limited; retry_after={wait}")
        if response.status_code in {401, 403}:
            raise QuantDataError(f"Quant Data authorization failed ({response.status_code})")
        if response.status_code >= 400:
            # v1.45.0 · El error de validación DICE qué campo falla. Antes se leía
            # sólo `detail`/`title` y se recortaba a 180 caracteres, así que
            #
            #     {"detail": "Request validation failed",
            #      "errors": [{"loc": ["body","lookBackPeriod"], "msg": "field required"}]}
            #
            # llegaba al operador como «Quant Data HTTP 400: Request validation
            # failed» —cierto, inútil y sin el único dato accionable—. El campo que
            # falta estaba en la respuesta y lo tirábamos nosotros.
            detail, fields = _describe_error(response)
            error = QuantDataError(f"Quant Data HTTP {response.status_code}: {detail}")
            error.status_code = response.status_code
            error.validation_fields = fields
            error.body = _safe_body(response)
            # v1.49.0 · El cuerpo del 400 viaja además DESGLOSADO. El crudo vale
            # para el registro; para actuar hacen falta `type`, `detail` y cada
            # `errors[].field` con su `errors[].message` por separado, que es lo
            # que el Auditor tiene que poder enseñar en una tabla.
            error.error_detail = _structured_error(error.body, response.status_code)
            raise error
        try:
            payload = response.json()
        except Exception as exc:
            raise QuantDataError("Quant Data returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise QuantDataError("Quant Data returned a non-object payload")
        return QuantDataResponse(payload=payload, status_code=response.status_code, remaining=remaining, limit=limit, reset_seconds=reset)

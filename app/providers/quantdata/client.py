from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import asyncio

import httpx

from .settings import QuantDataSettings
from ...core.endpoint_runtime import Deadline
from ...core.transport_runtime import (TRANSPORT, TransportTrace, pool_limits,
                                       CONNECT, POOL, READ, WRITE,
                                       WRITE_TIMEOUT_S)
from .shared import QUOTA
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


class QuantDataTimeout(QuantDataError):
    """El plazo expiró antes de que el proveedor contestara.

    Tiene clase propia porque la decisión que sigue es DISTINTA a la de
    cualquier otro fallo: un timeout no dice que el endpoint esté roto, dice que
    el plazo con el que se le llamó era demasiado corto. Distinguirlo por el
    TEXTO del error obligaba a comparar cadenas —y a acertar con el idioma y el
    formato—; con un tipo, quien decide no se puede equivocar.

    `limit_seconds` lleva el plazo que expiró, que es el dato con el que el
    runtime del endpoint puede subirlo en el intento siguiente.
    """

    limit_seconds: float | None = None
    #: v1.60.0 · CONNECT_TIMEOUT, POOL_TIMEOUT, READ_TIMEOUT o WRITE_TIMEOUT.
    #: Cuatro causas y cuatro arreglos, y ninguno se parece a los otros:
    #:
    #:   CONNECT  no se alcanzó al host   → red/DNS/TLS. NO es el endpoint, y
    #:                                      afecta a TODAS las herramientas
    #:   POOL     nuestro pool lleno      → congestión PROPIA. Subir el plazo
    #:                                      del proveedor no la toca
    #:   READ     conectó y no contestó   → el endpoint tarda: su p95 manda
    #:   WRITE    no se pudo enviar       → enlace de subida
    #:
    #: Antes los cuatro llegaban como «timeout» y el remedio se adivinaba.
    phase: str | None = None
    #: Telemetría del transporte de ESTA petición, cuando se llegó a medir.
    transport: dict | None = None
    #: Por qué NO se reintentó la conexión, cuando no se reintentó. Un «no» sin
    #: causa no se puede diagnosticar.
    retry_decision: dict | None = None


@dataclass
class QuantDataResponse:
    payload: dict[str, Any]
    status_code: int
    remaining: int | None = None
    limit: int | None = None
    reset_seconds: float | None = None
    #: v1.60.0 · Dónde se fue el tiempo de ESTA petición, medido en el
    #: transporte: `pool_wait_ms`, `connect_ms`, `tls_ms`, `write_ms`,
    #: `read_ms`, `request_ms` y si la conexión se REUTILIZÓ.
    transport: dict[str, Any] | None = None


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
        self._owns_reference = False

    def _build_client(self) -> httpx.AsyncClient:
        """El cliente del host: UNO para todo el proceso, con keep-alive real.

        v1.60.0 · EL DEFECTO NO ERA EL PLAZO, ERA VOLVER A CONECTAR SIEMPRE.

        Cada carril construía el suyo: dos pools contra el mismo host. Y con el
        `keepalive_expiry` por defecto de httpx —CINCO segundos— frente a ciclos
        de quince, todas las conexiones estaban caducadas al empezar cada ciclo.
        El resultado era un handshake TCP+TLS por herramienta y por ciclo, todos
        a la vez: una estampida de conexión contra el mismo host, que es lo que
        se veía como `connect timed out after 4.0s` en cuatro endpoints que no
        comparten nada salvo el destino.

        El pool se dimensiona DESDE el techo de concurrencia: ni menos —la
        diferencia se convertiría en espera de pool, que se lee como lentitud
        del proveedor y no lo es— ni mucho más, que devolvería la estampida por
        el otro lado.
        """
        limites = pool_limits(self.settings.max_inflight)
        anfitrion = TRANSPORT.host(self.settings.base_url,
                                   self.settings.connect_timeout_seconds)
        return httpx.AsyncClient(
            base_url=self.settings.base_url,
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"ITM-QUANT/{APP_VERSION}",
            },
            limits=httpx.Limits(
                max_connections=limites["max_connections"],
                max_keepalive_connections=limites["max_keepalive_connections"],
                keepalive_expiry=limites["keepalive_expiry"]),
            # Plazo por DEFECTO, con las CUATRO fases separadas. Cada petición
            # trae el suyo (ver `post`); el del constructor ya no puede ser un
            # número plano aplicado a las cuatro.
            timeout=httpx.Timeout(
                self.settings.read_warm_start_seconds,
                connect=anfitrion.connect_timeout(),
                write=WRITE_TIMEOUT_S,
                pool=limites["pool_timeout_s"]),
            follow_redirects=False,
        )

    async def start(self) -> None:
        if self._client is None:
            # El pool es del HOST, no de este objeto: los dos carriles comparten
            # conexiones en vez de abrir cada uno las suyas.
            self._client = TRANSPORT.acquire(self.settings.base_url,
                                             self._build_client)
            self._owns_reference = True

    async def close(self) -> None:
        if self._client is not None:
            self._client = None
            if self._owns_reference:
                self._owns_reference = False
                # Sólo se cierra de verdad cuando lo suelta el ÚLTIMO carril:
                # que el primero en parar cerrara el pool dejaría al otro sin
                # transporte a mitad de ciclo.
                sobrante = TRANSPORT.release(self.settings.base_url)
                if sobrante is not None:
                    await sobrante.aclose()

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

    async def _enviar(self, path: str, body: dict[str, Any], plazo: "Deadline",
                      anfitrion, limites: dict) -> tuple[Any, "TransportTrace"]:
        """UN intento contra el host, con las cuatro fases separadas.

        Cada `except` nombra una fase distinta porque cada una tiene un arreglo
        distinto, y confundirlas fue lo que hizo que «timeout» significara
        cuatro cosas en el mismo registro.
        """
        assert self._client is not None
        traza = TransportTrace()
        try:
            response = await self._client.post(
                path, json=body,
                timeout=httpx.Timeout(plazo.read, connect=plazo.connect,
                                      write=plazo.write, pool=plazo.pool),
                # El único sitio donde se puede separar «esperar hueco en el
                # pool» de «abrir el socket» de «negociar TLS» de «esperar la
                # respuesta». Sin esto, los cuatro se leen igual desde fuera.
                extensions={"trace": traza})
        except httpx.PoolTimeout as exc:
            # NO es el proveedor: es NUESTRO pool. Subirle el plazo al proveedor
            # por esto sería arreglar la casa del vecino.
            anfitrion.note_pool_timeout()
            error = QuantDataTimeout(
                f"Quant Data pool timed out after {plazo.pool:.1f}s: "
                f"sin hueco en el pool propio")
            error.limit_seconds = plazo.pool
            error.phase = POOL
            error.transport = anfitrion.snapshot()
            raise error from exc
        except httpx.ConnectTimeout as exc:
            # Un plazo de CONEXIÓN agotado no dice nada sobre lo que tarda el
            # endpoint en calcular: dice que no se llegó al proveedor. Subir el
            # plazo de lectura por esto sería perseguir el síntoma equivocado.
            #
            # Y deja MUESTRA: el plazo del siguiente intento sube. Sin eso, un
            # plazo corto impide completar el handshake, la falta de handshake
            # impide medir y la falta de medidas mantiene el plazo corto. Para
            # siempre, que es exactamente lo que se veía en Windows.
            anfitrion.note_connect_timeout(plazo.connect)
            error = QuantDataTimeout(
                f"Quant Data connect timed out after {plazo.connect:.1f}s")
            error.limit_seconds = plazo.connect
            error.phase = CONNECT
            error.transport = anfitrion.snapshot()
            raise error from exc
        except httpx.WriteTimeout as exc:
            error = QuantDataTimeout(
                f"Quant Data write timed out after {float(plazo.write or 0):.1f}s")
            error.limit_seconds = plazo.write
            error.phase = WRITE
            raise error from exc
        except httpx.TimeoutException as exc:
            # El plazo que expiró viaja en el error. Sin él, aguas arriba no se
            # puede distinguir «este endpoint está muerto» de «se le dieron
            # cinco segundos y necesita doce», que son dos arreglos opuestos.
            error = QuantDataTimeout(
                f"Quant Data request timed out after {plazo.read:.1f}s")
            error.limit_seconds = plazo.read
            error.phase = READ
            error.transport = traza.finish().as_dict()
            raise error from exc
        except httpx.ConnectError as exc:
            # DNS, ruta, TLS rechazado: no se llegó al host. Cuenta para el
            # cortocircuitos DEL TRANSPORTE, que es el que protege a las treinta
            # y seis herramientas a la vez.
            anfitrion.note_host_failure(f"{type(exc).__name__}: {exc}")
            error = QuantDataError(f"Quant Data connect error: {type(exc).__name__}")
            error.phase = CONNECT
            error.transport = anfitrion.snapshot()
            raise error from exc
        except httpx.HTTPError as exc:
            raise QuantDataError(f"Quant Data transport error: {type(exc).__name__}") from exc

        traza.finish()
        return response, traza

    async def post(self, path: str, body: dict[str, Any], *,
                   timeout: float | "Deadline" | None = None,
                   burst: bool = False,
                   cycle_left_s: float | None = None) -> QuantDataResponse:
        """Una petición. `timeout` es el plazo DE ESTA petición, en segundos.

        v1.58.1 · EL PLAZO MEDIDO NO LLEGABA AL TRANSPORTE.

        `endpoint_runtime` calibra un plazo por endpoint —p95 medido, techo de
        veinte segundos— y el cliente se construía con UNO fijo para las treinta
        y seis herramientas y los dos carriles. El plazo calibrado sólo decidía
        cuánto esperaba el CICLO; la petición seguía viva con el plazo del
        constructor. Dos consecuencias, las dos visibles en el registro:

          · un endpoint que legítimamente necesita doce segundos no podía
            responder nunca, porque el transporte lo cortaba a los cinco por
            mucho que su plazo calibrado dijera otra cosa;
          · un endpoint rápido al que el ciclo abandonaba a los dos segundos
            seguía ocupando conexión y cuota hasta el plazo del constructor, y
            al morir soltaba un SEGUNDO aviso por el mismo hecho.

        Con el plazo por petición hay un solo plazo por endpoint y un solo sitio
        donde se decide.
        """
        if not self.settings.configured:
            raise QuantDataError("Quant Data is not configured")
        await self.start()
        assert self._client is not None
        # v1.57.3 · LA CONTABILIDAD VIVE AQUÍ, EN EL ÚNICO SITIO POR DONDE PASAN
        # LAS DOS CARRILES. Antes cada carril anotaba lo suyo: el del motor hacía
        # `note()` + `spend()` y el de páginas sólo `spend()`, así que la mitad de
        # las respuestas no actualizaba las cabeceras de cuota y el presupuesto se
        # calculaba con telemetría a medias. Se anota ANTES de pedir, porque una
        # petición en vuelo ya ocupa sitio en la ventana deslizante.
        QUOTA.spend(1)
        # v1.58.0 · `timeout` admite un `Deadline` —conexión y lectura por
        # separado— o un float por compatibilidad. Con el float se conserva el
        # plazo de conexión de las settings en vez de aplicar el mismo número a
        # los dos, que era la forma de cortar por lectura a un endpoint que
        # conectaba en 80 ms.
        if timeout is None:
            plazo = Deadline(connect=self.settings.connect_timeout_seconds,
                             read=self.settings.read_warm_start_seconds,
                             source="CLIENT_DEFAULT")
        elif isinstance(timeout, Deadline):
            plazo = timeout
        else:
            plazo = Deadline(connect=self.settings.connect_timeout_seconds,
                             read=max(0.1, float(timeout)), source="FLOAT_COMPAT")

        # ═══════════════════════════════════════════════════════════════════
        # v1.60.0 · EL PLAZO DE CONEXIÓN ES DEL HOST, NO DE LA HERRAMIENTA
        # ═══════════════════════════════════════════════════════════════════
        #
        # Quien decide cuánto se espera un handshake es el HOST: el handshake no
        # pertenece a `net_drift` ni a `gamma`, y medirlo por endpoint reparte
        # treinta y seis veces la misma muestra. La LECTURA sigue siendo del
        # endpoint —su p95—, que es lo que ya decidió `endpoint_runtime`.
        anfitrion = TRANSPORT.host(self.settings.base_url,
                                   self.settings.connect_timeout_seconds)
        limites = pool_limits(self.settings.max_inflight)
        permiso = anfitrion.allows()
        if not permiso["allowed"]:
            # Cortocircuito DE TRANSPORTE: el host no está respondiendo y
            # llamarle otra vez sólo alarga la cola. Se falla rápido para que
            # quien llama pueda servir su último valor bueno.
            error = QuantDataError(f"Quant Data transport open: {permiso['reason']}")
            error.phase = CONNECT
            error.transport = anfitrion.snapshot()
            raise error
        plazo = plazo.with_transport(connect=anfitrion.connect_timeout(),
                                     write=WRITE_TIMEOUT_S,
                                     pool=limites["pool_timeout_s"],
                                     source=plazo.source)

        try:
            response, traza = await self._enviar(path, body, plazo, anfitrion,
                                                 limites)
        except QuantDataTimeout as fallo:
            # ═══════════════════════════════════════════════════════════════
            # UN reintento de CONEXIÓN, y sólo con las cuatro condiciones
            # ═══════════════════════════════════════════════════════════════
            #
            # Un reintento por petición multiplicaría por dos la estampida de
            # conexión que causa el fallo, así que el presupuesto es del HOST y
            # por ciclo. Durante una ráfaga no se concede ninguno: la ráfaga ya
            # está usando el enlace entero.
            if fallo.phase != CONNECT:
                raise
            decision = anfitrion.retry_connect(burst=burst,
                                               deadline_left_s=cycle_left_s)
            if not decision.get("retry"):
                fallo.retry_decision = decision
                raise
            await asyncio.sleep(float(decision["sleep_seconds"]))
            QUOTA.spend(1)
            # El segundo intento va con el plazo YA escalado por el primero: es
            # lo que convierte un fallo en una medida en vez de en una repetición.
            plazo = plazo.with_transport(connect=anfitrion.connect_timeout(),
                                         write=WRITE_TIMEOUT_S,
                                         pool=limites["pool_timeout_s"],
                                         source="CONNECT_RETRY")
            try:
                response, traza = await self._enviar(path, body, plazo, anfitrion,
                                                     limites)
            except QuantDataTimeout as segundo:
                # El reintento ya se gastó: que el error lo DIGA, o aguas arriba
                # parecerá que nunca se intentó.
                segundo.retry_decision = {**decision, "spent": True,
                                          "outcome": "el reintento también agotó "
                                                     "el plazo de conexión"}
                raise
        # Se alcanzó el host: el transporte está sano, se midió lo que costó y
        # el cortocircuitos se cierra.
        anfitrion.note_trace(traza)
        anfitrion.note_success()

        remaining = self._int_header(response.headers, "X-RateLimit-Remaining")
        limit = self._int_header(response.headers, "X-RateLimit-Limit")
        reset = self._float_header(response.headers, "X-RateLimit-Reset")
        # Las cabeceras son de la CUENTA y llegan también en las respuestas de
        # error. Leerlas sólo en el camino feliz dejaba el presupuesto ciego justo
        # cuando más importa: cuando el proveedor está devolviendo errores.
        QUOTA.note(remaining=remaining, limit=limit, reset_seconds=reset)

        if response.status_code == 429:
            retry_after = self._float_header(response.headers, "Retry-After")
            # `Retry-After` manda; el guardián cae a `Reset` si no viene. El límite
            # es de la cuenta: frenar sólo al carril que recibió el 429 dejaría al
            # otro gastando peticiones que ya se sabe que van a fallar.
            QUOTA.note_rate_limited(retry_after)
            wait = retry_after if retry_after is not None else reset
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
        return QuantDataResponse(payload=payload, status_code=response.status_code,
                                 remaining=remaining, limit=limit, reset_seconds=reset,
                                 transport=traza.as_dict())

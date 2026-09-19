"""Fail-closed network exposure and HTTP/WebSocket authentication guard.

v1.27.6 corrige tres defectos de exposicion detectados en la auditoria de v1.27.4:

1. La proteccion dependia unicamente del bind. Detras de un reverse proxy que
   reenvia a 127.0.0.1 el middleware no se instalaba y el motor quedaba abierto.
   Ahora la proteccion se activa por token configurado, por bind externo o por
   declaracion explicita (``ITM_REQUIRE_TOKEN`` / ``ITM_TRUST_PROXY``).
2. La cookie transportaba el token maestro en claro. Ahora transporta una
   credencial de sesion opaca derivada por HMAC, con caducidad propia.
3. ``Secure`` iba forzado, lo que descartaba la cookie sobre HTTP plano y
   producia un bucle de redireccion. El modo por defecto es ``auto``.

Ademas se incorpora limitacion de intentos fallidos por origen, que antes vivia
en ``app/auth.py`` sin estar conectada a ninguna ruta.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from http.cookies import SimpleCookie
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from .obs import note as _obs_note

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}
OPEN_PATHS = {"/healthz", "/favicon.ico", "/login", "/auth/session"}
TOKEN_HEADER = b"x-itm-token"
COOKIE_NAME = "itm_access"
MIN_TOKEN_LENGTH = 32

SESSION_VERSION = "v2"
LEGACY_SESSION_VERSION = "v1"
DEFAULT_SESSION_TTL = 43200  # 12 h
SID_BYTES = 16
MAX_REVOKED = 4096

FAIL_WINDOW_SECONDS = 600
FAIL_MAX_ATTEMPTS = 12

_TRUTHY = {"1", "true", "yes", "on", "si"}
_FALSY = {"0", "false", "no", "off"}

MAX_TRACKED_IPS = max(128, int(os.getenv("ITM_RATE_LIMIT_MAX_IPS", "4096")))
_PURGE_EVERY = 256
_failed_attempts: "OrderedDict[str, deque]" = OrderedDict()
_global_failures: deque = deque(maxlen=max(4096, MAX_TRACKED_IPS * 4))
_ops_since_purge = 0


class InsecureExposureError(RuntimeError):
    """The engine was configured for unsafe external exposure."""


# --------------------------------------------------------------- entorno

def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return default


def is_loopback(host: str) -> bool:
    h = str(host or "").strip().lower()
    if h in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def access_token() -> str:
    return os.getenv("ITM_ACCESS_TOKEN", "").strip()


def trust_proxy() -> bool:
    """El motor esta detras de un reverse proxy de confianza."""
    return _flag("ITM_TRUST_PROXY")

def production_mode() -> bool:
    return _flag("ITM_PRODUCTION") or str(os.getenv("ITM_ENV","")).strip().lower()=="production"


def allow_query_token_bootstrap() -> bool:
    """Query-token bootstrap is opt-in in production to avoid proxy/history leakage."""
    default=not production_mode()
    return _flag("ITM_ALLOW_QUERY_TOKEN_BOOTSTRAP", default=default)


def allow_legacy_master_token_cookie() -> bool:
    """Temporary <=1.27.4 compatibility, impossible to enable in production."""
    if production_mode():
        return False
    return _flag("ITM_ALLOW_LEGACY_COOKIE", default=False)


def should_protect(host: str) -> bool:
    """Decide si exigir credencial, con independencia del bind.

    Este es el arreglo central: un bind loopback detras de un proxy ya no
    desactiva la autenticacion.
    """
    if _flag("ITM_REQUIRE_TOKEN"):
        return True
    if not is_loopback(host):
        return True
    if trust_proxy():
        return True
    # Token configurado en loopback: el operador quiso proteger, se respeta.
    return bool(access_token())


def assert_safe_bind(host: str) -> None:
    """Abort before serving when the exposure surface lacks a strong token."""
    exposed = (not is_loopback(host)) or trust_proxy() or _flag("ITM_REQUIRE_TOKEN")
    if not exposed:
        return
    if len(access_token()) < MIN_TOKEN_LENGTH:
        motivo = f"ITM_BIND_HOST={host!r}" if not is_loopback(host) else "la exposicion declarada (ITM_TRUST_PROXY/ITM_REQUIRE_TOKEN)"
        raise InsecureExposureError(
            f"{motivo} requiere ITM_ACCESS_TOKEN con al menos {MIN_TOKEN_LENGTH} caracteres. "
            'Genera uno con: python -c "import secrets;print(secrets.token_urlsafe(32))"'
        )


def token_matches(candidate: str | None) -> bool:
    expected = access_token()
    return bool(expected) and secrets.compare_digest(str(candidate or ""), expected)


# ------------------------------------------------- credencial de sesion

def session_ttl_seconds() -> int:
    try:
        ttl = int(os.getenv("ITM_SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL)).strip())
    except ValueError:
        return DEFAULT_SESSION_TTL
    return ttl if ttl > 0 else DEFAULT_SESSION_TTL


_REVOKED: "OrderedDict[str, int]" = OrderedDict()
_REVOKE_EPOCH = 0
_SESSION_STATE_LOCK = threading.Lock()


def _session_state_path() -> Path:
    raw = os.getenv("ITM_SESSION_STATE_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path(os.getenv("ITM_DATA_DIR", "app/storage")) / "session_state.json"


def _purge_revoked(now: float) -> None:
    for sid in [sid for sid, exp in _REVOKED.items() if int(exp) <= now]:
        _REVOKED.pop(sid, None)
    while len(_REVOKED) > MAX_REVOKED:
        _REVOKED.popitem(last=False)


def _load_session_state() -> None:
    global _REVOKE_EPOCH
    try:
        d = json.loads(_session_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as exc:
        # Fail closed: unreadable revocation state invalidates all pre-existing sessions.
        _REVOKE_EPOCH = max(int(_REVOKE_EPOCH) + 1, int(time.time()))
        _REVOKED.clear()
        _obs_note("net_guard:session_state_unreadable", exc, severity="CRITICAL_DATA")
        return
    if not isinstance(d, dict):
        _REVOKE_EPOCH = max(int(_REVOKE_EPOCH) + 1, int(time.time()))
        _REVOKED.clear()
        return
    _REVOKE_EPOCH = int(d.get("epoch") or 0)
    now = time.time()
    _REVOKED.clear()
    for sid, exp in (d.get("revoked") or {}).items():
        try:
            if int(exp) > now:
                _REVOKED[str(sid)] = int(exp)
        except Exception as exc:
            _obs_note("net_guard:revoked_entry_invalid", exc, severity="DEGRADED")
    _purge_revoked(now)


def _save_session_state() -> bool:
    p = _session_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"epoch": int(_REVOKE_EPOCH), "revoked": dict(_REVOKED)},
                                  separators=(",", ":"), sort_keys=True), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError as exc:
            _obs_note("net_guard:session_state_tmp_chmod", exc, severity="DEGRADED")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except OSError as exc:
            _obs_note("net_guard:session_state_chmod", exc, severity="DEGRADED")
        return True
    except Exception as exc:
        _obs_note("net_guard:session_state_unwritable", exc, severity="CRITICAL_DATA")
        return False


def _session_key() -> bytes:
    """Key derived from the master token and revocation epoch; never stores the token."""
    material = f"itm-quant-session|{access_token()}|{int(_REVOKE_EPOCH)}"
    return hashlib.sha256(material.encode("utf-8")).digest()


def _sign(payload: str) -> str:
    return hmac.new(_session_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(now: float | None = None) -> str:
    """Opaque revocable credential: ``v2.<expires>.<128-bit sid>.<hmac>``."""
    expires = int((time.time() if now is None else now)) + session_ttl_seconds()
    sid = secrets.token_hex(SID_BYTES)
    payload = f"{SESSION_VERSION}.{expires}.{sid}"
    return f"{payload}.{_sign(payload)}"


def session_sid(value: str | None) -> str | None:
    parts = str(value or "").split(".")
    return parts[2] if len(parts) == 4 and parts[0] == SESSION_VERSION else None


def revoke_session(value: str | None, now: float | None = None) -> bool:
    now_f = time.time() if now is None else float(now)
    parts = str(value or "").split(".")
    if len(parts) != 4 or parts[0] != SESSION_VERSION:
        return False
    try:
        expires = int(parts[1])
    except ValueError:
        return False
    if expires <= now_f:
        return False
    # Do not create revocation state for a forged credential.
    if not session_valid(value, now=now_f):
        return False
    with _SESSION_STATE_LOCK:
        _REVOKED[parts[2]] = expires
        _REVOKED.move_to_end(parts[2])
        _purge_revoked(now_f)
        return _save_session_state()


def revoke_all_sessions(now: float | None = None) -> int:
    global _REVOKE_EPOCH
    with _SESSION_STATE_LOCK:
        _REVOKE_EPOCH = max(int(_REVOKE_EPOCH) + 1, int(time.time() if now is None else now))
        _REVOKED.clear()
        _save_session_state()
        return int(_REVOKE_EPOCH)


def session_state() -> dict:
    p = _session_state_path()
    with _SESSION_STATE_LOCK:
        _purge_revoked(time.time())
        return {"version": SESSION_VERSION, "revoked_active": len(_REVOKED),
                "revoke_epoch": int(_REVOKE_EPOCH), "state_path": str(p),
                "persisted": p.exists()}


def session_valid(value: str | None, now: float | None = None) -> bool:
    if not access_token() or not value:
        return False
    parts = str(value).split(".")
    if len(parts) != 4:
        return False
    version, raw_expires, sid, mac = parts
    if version != SESSION_VERSION:
        return False
    try:
        expires = int(raw_expires)
    except ValueError:
        return False
    now_f = time.time() if now is None else float(now)
    if now_f >= expires:
        return False
    with _SESSION_STATE_LOCK:
        _purge_revoked(now_f)
        if sid in _REVOKED:
            return False
    return hmac.compare_digest(mac, _sign(f"{version}.{raw_expires}.{sid}"))


# ------------------------------------------------- limitacion de intentos

def _trim_failures(q: deque, now: float) -> None:
    while q and now - q[0] > FAIL_WINDOW_SECONDS:
        q.popleft()


def _purge_expired(now: float) -> None:
    for ip in [ip for ip, q in _failed_attempts.items()
               if not q or now - q[-1] > FAIL_WINDOW_SECONDS]:
        _failed_attempts.pop(ip, None)


def _maybe_purge(now: float) -> None:
    global _ops_since_purge
    _ops_since_purge += 1
    if _ops_since_purge >= _PURGE_EVERY:
        _ops_since_purge = 0
        _purge_expired(now)


def _peer_ip(scope) -> str:
    client = scope.get("client") or ()
    return str(client[0]) if client else "desconocido"


def _trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    raw = os.getenv("ITM_TRUSTED_PROXY_IPS", "127.0.0.1/32,::1/128")
    out=[]
    for item in raw.split(","):
        item=item.strip()
        if not item: continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            try:
                addr=ipaddress.ip_address(item)
                out.append(ipaddress.ip_network(f"{addr}/{addr.max_prefixlen}", strict=False))
            except ValueError as _e:
                _obs_note("net_guard:trusted_proxy_network", _e)
                continue
    return out


def proxy_peer_trusted(scope) -> bool:
    """Only the directly connected reverse proxy may supply forwarding headers."""
    if not trust_proxy():
        return False
    try:
        peer=ipaddress.ip_address(_peer_ip(scope))
    except ValueError:
        return False
    return any(peer in net for net in _trusted_proxy_networks())


def client_ip(scope) -> str:
    """Resolve client IP only through a verified proxy peer.

    For a trusted proxy chain, the right-most XFF hop is the one appended closest to
    ITM QUANT. ``ITM_PROXY_DEPTH`` can select a deeper trusted chain.  Untrusted TCP
    peers cannot influence the key used by the limiter by forging X-Forwarded-For.
    """
    if proxy_peer_trusted(scope):
        headers = _headers(scope)
        raw = headers.get(b"x-forwarded-for", b"").decode("utf-8", "replace")
        hops = [h.strip() for h in raw.split(",") if h.strip()]
        if hops:
            try: depth=max(1, int(os.getenv("ITM_PROXY_DEPTH", "1")))
            except ValueError: depth=1
            candidate=hops[-depth] if len(hops) >= depth else hops[0]
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError as _e:
                _obs_note("net_guard:xff_candidate", _e)
    return _peer_ip(scope)


def is_rate_limited(ip: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    _maybe_purge(now)
    q = _failed_attempts.get(str(ip))
    if q is None:
        return False
    _trim_failures(q, now)
    if not q:
        _failed_attempts.pop(str(ip), None)
        return False
    _failed_attempts.move_to_end(str(ip))
    return len(q) >= FAIL_MAX_ATTEMPTS


def record_failure(ip: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    _maybe_purge(now)
    key=str(ip)
    q=_failed_attempts.get(key)
    if q is None:
        while len(_failed_attempts) >= MAX_TRACKED_IPS:
            _failed_attempts.popitem(last=False)
        q=deque(maxlen=FAIL_MAX_ATTEMPTS * 4)
        _failed_attempts[key]=q
    _trim_failures(q, now)
    q.append(now); _failed_attempts.move_to_end(key)
    # Telemetry only. It intentionally does NOT globally lock out legitimate users;
    # network-wide throttling belongs at Caddy/nginx/firewall level.
    _global_failures.append(now)


def clear_failures(ip: str) -> None:
    _failed_attempts.pop(str(ip), None)


def reset_rate_limiter() -> None:
    global _ops_since_purge
    _failed_attempts.clear(); _global_failures.clear(); _ops_since_purge=0


def rate_limiter_stats(now: float | None = None) -> dict:
    now=time.time() if now is None else now
    _purge_expired(now); _trim_failures(_global_failures, now)
    return {
        "tracked_ips": len(_failed_attempts),
        "max_tracked_ips": MAX_TRACKED_IPS,
        "global_failures_window": len(_global_failures),
        "window_seconds": FAIL_WINDOW_SECONDS,
        "global_policy": "TELEMETRY_ONLY_NO_GLOBAL_LOCKOUT",
    }


# ------------------------------------------------------------ utilidades

def _headers(scope) -> dict[bytes, bytes]:
    return {key.lower(): value for key, value in scope.get("headers", [])}


def _cookie_value(raw_cookie: bytes) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie.decode("latin-1"))
    except Exception:
        return None
    morsel = cookie.get(COOKIE_NAME)
    return morsel.value if morsel else None


def _is_https(scope) -> bool:
    if str(scope.get("scheme") or "").lower() in {"https", "wss"}:
        return True
    if proxy_peer_trusted(scope):
        proto = _headers(scope).get(b"x-forwarded-proto", b"").decode("utf-8", "replace")
        return proto.split(",")[-1].strip().lower() == "https"
    return False


def session_cookie_secure(scope) -> bool:
    """Public wrapper used by the explicit login/session endpoint."""
    mode = os.getenv("ITM_SESSION_HTTPS_ONLY", "auto").strip().lower()
    return mode in _TRUTHY or (mode not in _FALSY and _is_https(scope))

def _session_cookie(scope) -> str:
    parts = [f"{COOKIE_NAME}={issue_session()}", "Path=/", "HttpOnly", "SameSite=Strict",
             f"Max-Age={session_ttl_seconds()}"]
    if session_cookie_secure(scope):
        parts.append("Secure")
    return "; ".join(parts)


class TokenASGIMiddleware:
    """Authenticate HTTP and WebSocket scopes without BaseHTTPMiddleware."""

    def __init__(self, app, host: str):
        self.app = app
        self.host = host

    @property
    def protected(self) -> bool:
        # Evaluado por peticion: el entorno puede resolverse despues del import.
        return should_protect(self.host)

    async def _deny(self, scope, send, status: int, body: bytes, reason: str):
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": reason})
            return
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")
        if not self.protected or scope_type not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path in OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        ip = client_ip(scope)
        if is_rate_limited(ip):
            await self._deny(scope, send, 429, b'{"error":"demasiados intentos"}', "demasiados intentos")
            return

        headers = _headers(scope)
        pairs = parse_qsl(scope.get("query_string", b"").decode("utf-8", "replace"), keep_blank_values=True)
        raw_query_token = next((value for key, value in pairs if key == "token"), None)
        query_token = raw_query_token if allow_query_token_bootstrap() else None
        header_token = headers.get(TOKEN_HEADER, b"").decode("utf-8", "replace")
        cookie_value = _cookie_value(headers.get(b"cookie", b""))

        # La cookie se valida como credencial de sesion; cabecera y query como token maestro.
        legacy_cookie_ok = allow_legacy_master_token_cookie() and token_matches(cookie_value)
        authorized = (
            token_matches(header_token)
            or token_matches(query_token)
            or session_valid(cookie_value)
            or legacy_cookie_ok
        )
        if raw_query_token and not allow_query_token_bootstrap() and not (token_matches(header_token) or session_valid(cookie_value) or legacy_cookie_ok):
            record_failure(ip)
            await self._deny(scope, send, 401, b'{"error":"query token bootstrap deshabilitado"}', "query token bootstrap deshabilitado")
            return
        if not authorized:
            record_failure(ip)
            await self._deny(scope, send, 401, b'{"error":"token requerido"}', "token requerido")
            return
        clear_failures(ip)

        if scope_type == "http" and query_token and str(scope.get("method", "GET")).upper() in {"GET", "HEAD"}:
            clean = [(key, value) for key, value in pairs if key != "token"]
            location = str(scope.get("path") or "/") + (("?" + urlencode(clean)) if clean else "")
            await send({"type": "http.response.start", "status": 303, "headers": [
                (b"location", location.encode("utf-8")),
                (b"set-cookie", _session_cookie(scope).encode("latin-1")),
                (b"cache-control", b"no-store"),
                (b"content-length", b"0"),
            ]})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


def install(app, host: str) -> None:
    # Always install. ``protected`` is evaluated per request, so a runtime/external
    # exposure change cannot leave the app without an authentication middleware.
    app.add_middleware(TokenASGIMiddleware, host=host)


# Load revocation state at import so process restarts do not resurrect stolen sessions.
try:
    _load_session_state()
except Exception as exc:  # pragma: no cover - defensive startup hardening
    _obs_note("net_guard:session_state_bootstrap", exc, severity="CRITICAL_DATA")

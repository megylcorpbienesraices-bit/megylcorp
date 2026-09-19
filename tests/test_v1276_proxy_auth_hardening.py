"""Endurecimiento de exposicion tras la auditoria de v1.27.4 (v1.27.6).

Cubre los tres defectos corregidos:
  1. loopback detras de reverse proxy dejaba el motor sin autenticacion,
  2. la cookie transportaba el token maestro en claro,
  3. ``Secure`` forzado descartaba la cookie sobre HTTP plano.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core import net_guard
from app.core.net_guard import TokenASGIMiddleware

TOKEN = "z" * 40


@pytest.fixture(autouse=True)
def _entorno_limpio(monkeypatch):
    for var in ("ITM_ACCESS_TOKEN", "ITM_REQUIRE_TOKEN", "ITM_TRUST_PROXY",
                "ITM_SESSION_HTTPS_ONLY", "ITM_SESSION_TTL_SECONDS", "ITM_TRUSTED_PROXY_IPS", "ITM_PROXY_DEPTH"):
        monkeypatch.delenv(var, raising=False)
    net_guard.reset_rate_limiter()
    yield
    net_guard.reset_rate_limiter()


def _app(scope, receive, send):
    async def inner():
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return inner()


def _run(guard, scope):
    eventos = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        eventos.append(message)

    asyncio.run(guard(scope, receive, send))
    return eventos


def _scope(path="/api/state", headers=None, query=b"", method="GET", scheme="http", client=("203.0.113.9", 51234)):
    return {"type": "http", "method": method, "path": path, "query_string": query,
            "headers": headers or [], "scheme": scheme, "client": client}


# ------------------------- 1. el bind ya no decide la seguridad


def test_loopback_detras_de_proxy_sigue_exigiendo_token(monkeypatch):
    """El defecto original: nginx -> 127.0.0.1 dejaba todo abierto."""
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("ITM_TRUST_PROXY", "1")
    guard = TokenASGIMiddleware(_app, "127.0.0.1")
    assert _run(guard, _scope())[0]["status"] == 401


def test_token_configurado_en_loopback_activa_la_proteccion(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    assert net_guard.should_protect("127.0.0.1") is True


def test_require_token_explicito_protege_aunque_no_haya_bind_externo(monkeypatch):
    monkeypatch.setenv("ITM_REQUIRE_TOKEN", "1")
    assert net_guard.should_protect("127.0.0.1") is True


def test_uso_local_puro_sigue_sin_friccion():
    """Sin token ni proxy declarado, el flujo local no cambia."""
    assert net_guard.should_protect("127.0.0.1") is False
    assert _run(TokenASGIMiddleware(_app, "127.0.0.1"), _scope())[0]["status"] == 200


def test_install_monta_el_middleware_en_loopback_protegido(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("ITM_TRUST_PROXY", "1")
    añadidos = []
    net_guard.install(type("FakeApp", (), {"add_middleware": lambda self, *a, **k: añadidos.append(a)})(), "127.0.0.1")
    assert añadidos, "el middleware debe instalarse detras de proxy"


def test_exposicion_declarada_sin_token_fuerte_aborta_el_arranque(monkeypatch):
    monkeypatch.setenv("ITM_TRUST_PROXY", "1")
    with pytest.raises(net_guard.InsecureExposureError):
        net_guard.assert_safe_bind("127.0.0.1")
    monkeypatch.setenv("ITM_ACCESS_TOKEN", "corto")
    with pytest.raises(net_guard.InsecureExposureError):
        net_guard.assert_safe_bind("127.0.0.1")
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    net_guard.assert_safe_bind("127.0.0.1")


# ------------------------- 2. la cookie ya no lleva el token


def test_la_cookie_emitida_no_contiene_el_token_maestro(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    eventos = _run(guard, _scope(path="/", query=f"token={TOKEN}".encode()))
    cookie = dict(eventos[0]["headers"])[b"set-cookie"].decode()
    assert eventos[0]["status"] == 303
    assert TOKEN not in cookie, "la cookie no puede transportar el secreto maestro"
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Max-Age=" in cookie


def test_la_cookie_de_sesion_autentica_peticiones_posteriores(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    cookie = dict(_run(guard, _scope(path="/", query=f"token={TOKEN}".encode()))[0]["headers"])[b"set-cookie"]
    valor = cookie.split(b";")[0]
    assert _run(guard, _scope(headers=[(b"cookie", valor)]))[0]["status"] == 200


def test_la_sesion_caduca(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    credencial = net_guard.issue_session(now=1_000_000.0)
    assert net_guard.session_valid(credencial, now=1_000_100.0)
    assert not net_guard.session_valid(credencial, now=1_000_000.0 + net_guard.session_ttl_seconds() + 1)


def test_una_sesion_falsificada_no_pasa(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    assert not net_guard.session_valid("v1.99999999999.deadbeef")
    assert not net_guard.session_valid("basura")
    assert not net_guard.session_valid(None)


def test_rotar_el_token_invalida_las_sesiones_vivas(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    credencial = net_guard.issue_session()
    assert net_guard.session_valid(credencial)
    monkeypatch.setenv("ITM_ACCESS_TOKEN", "w" * 40)
    assert not net_guard.session_valid(credencial), "rotar el token debe expulsar a todos"


# ------------------------- 3. Secure deja de romper HTTP plano


def test_sobre_http_plano_no_se_marca_secure(monkeypatch):
    """Con Secure forzado el navegador descartaba la cookie: bucle de redireccion."""
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    cookie = dict(_run(guard, _scope(path="/", query=f"token={TOKEN}".encode(), scheme="http"))[0]["headers"])[b"set-cookie"]
    assert b"Secure" not in cookie


def test_sobre_https_si_se_marca_secure(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    cookie = dict(_run(guard, _scope(path="/", query=f"token={TOKEN}".encode(), scheme="https"))[0]["headers"])[b"set-cookie"]
    assert b"Secure" in cookie


def test_https_terminado_en_el_proxy_se_detecta_por_cabecera(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("ITM_TRUST_PROXY", "1")
    guard = TokenASGIMiddleware(_app, "127.0.0.1")
    eventos = _run(guard, _scope(path="/", query=f"token={TOKEN}".encode(), scheme="http",
                                 headers=[(b"x-forwarded-proto", b"https")], client=("127.0.0.1", 51234)))
    assert b"Secure" in dict(eventos[0]["headers"])[b"set-cookie"]


# ------------------------- 4. limitacion de intentos (antes codigo muerto)


def test_los_intentos_fallidos_se_frenan(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    for _ in range(net_guard.FAIL_MAX_ATTEMPTS):
        assert _run(guard, _scope(headers=[(b"x-itm-token", b"malo")]))[0]["status"] == 401
    assert _run(guard, _scope(headers=[(b"x-itm-token", b"malo")]))[0]["status"] == 429


def test_el_freno_se_aplica_por_origen(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    for _ in range(net_guard.FAIL_MAX_ATTEMPTS + 1):
        _run(guard, _scope(headers=[(b"x-itm-token", b"malo")], client=("198.51.100.1", 1)))
    otro = _run(guard, _scope(headers=[(b"x-itm-token", TOKEN.encode())], client=("198.51.100.2", 1)))
    assert otro[0]["status"] == 200


def test_un_acierto_limpia_el_historial_de_fallos(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    for _ in range(net_guard.FAIL_MAX_ATTEMPTS - 1):
        _run(guard, _scope(headers=[(b"x-itm-token", b"malo")]))
    assert _run(guard, _scope(headers=[(b"x-itm-token", TOKEN.encode())]))[0]["status"] == 200
    assert not net_guard.is_rate_limited("203.0.113.9")


def test_el_websocket_tambien_se_frena(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    scope = {"type": "websocket", "path": "/ws/state", "query_string": b"", "headers": [],
             "client": ("203.0.113.77", 5)}
    for _ in range(net_guard.FAIL_MAX_ATTEMPTS):
        assert _run(guard, scope)[0]["code"] == 1008
    assert _run(guard, scope)[0]["reason"] == "demasiados intentos"


def test_healthz_sigue_siendo_sonda_publica(monkeypatch):
    monkeypatch.setenv("ITM_ACCESS_TOKEN", TOKEN)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    assert _run(guard, _scope(path="/healthz"))[0]["status"] == 200

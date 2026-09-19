from __future__ import annotations

import asyncio


def _run(scope, token_guard):
    events = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        events.append(message)

    asyncio.run(token_guard(scope, receive, send))
    return events


def _app(scope, receive, send):
    async def inner():
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return inner()


def test_external_guard_rejects_http_and_websocket_without_token(monkeypatch):
    from app.core.net_guard import TokenASGIMiddleware

    monkeypatch.setenv("ITM_ACCESS_TOKEN", "x" * 40)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    denied = _run({"type": "http", "method": "GET", "path": "/api/state", "query_string": b"", "headers": []}, guard)
    assert denied[0]["status"] == 401
    websocket = _run({"type": "websocket", "path": "/ws/state", "query_string": b"", "headers": []}, guard)
    assert websocket == [{"type": "websocket.close", "code": 1008, "reason": "token requerido"}]


def test_query_token_is_exchanged_for_cookie_and_cookie_authenticates(monkeypatch):
    from app.core.net_guard import TokenASGIMiddleware

    token = "t" * 40
    monkeypatch.setenv("ITM_ACCESS_TOKEN", token)
    monkeypatch.setenv("ITM_SESSION_HTTPS_ONLY", "0")
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    redirected = _run({"type": "http", "method": "GET", "path": "/", "query_string": f"token={token}&view=all".encode(), "headers": []}, guard)
    assert redirected[0]["status"] == 303
    cookie = dict(redirected[0]["headers"])[b"set-cookie"]
    assert b"HttpOnly" in cookie and b"token" not in dict(redirected[0]["headers"])[b"location"]
    allowed = _run({"type": "http", "method": "GET", "path": "/api/state", "query_string": b"", "headers": [(b"cookie", cookie)]}, guard)
    assert allowed[0]["status"] == 200


def test_healthz_is_minimal_public_probe(monkeypatch):
    from app.core.net_guard import TokenASGIMiddleware

    monkeypatch.setenv("ITM_ACCESS_TOKEN", "x" * 40)
    guard = TokenASGIMiddleware(_app, "0.0.0.0")
    events = _run({"type": "http", "method": "GET", "path": "/healthz", "query_string": b"", "headers": []}, guard)
    assert events[0]["status"] == 200

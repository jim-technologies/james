"""Unit tests for the web dashboard ASGI wrapper (Basic auth + static SPA).

Drives the ASGI interface directly with constructed scopes — no network, no
uvicorn. Verifies the fail-closed auth gate, static serving + traversal refusal,
and delegation of API paths to the wrapped (proto) app.
"""

from __future__ import annotations

import base64

from infra.web.app import WebApp


class _InnerApp:
    """A stand-in for the invariant asgi_app: records that it was reached."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"inner":true}'})


def _basic(user, pw):
    raw = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return [(b"authorization", f"Basic {raw}".encode())]


async def _call(app, *, method="GET", path="/", headers=None):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next(
        m["status"] for m in sent if m["type"] == "http.response.start"
    )
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    start = next(m for m in sent if m["type"] == "http.response.start")
    return status, body, dict(start["headers"])


def _app(tmp_path, *, password="s3cret", inner=None):  # noqa: S107 (test fixture)
    (tmp_path / "index.html").write_text("<!doctype html><title>james</title>")
    (tmp_path / "app.js").write_text("// js")
    return WebApp(
        inner or _InnerApp(),
        username="james",
        password=password,
        static_dir=str(tmp_path),
    )


async def test_denies_without_credentials(tmp_path):
    inner = _InnerApp()
    app = _app(tmp_path, inner=inner)
    status, _body, headers = await _call(app, path="/")
    assert status == 401
    assert headers.get(b"www-authenticate", b"").startswith(b"Basic")
    assert inner.called is False  # never reached the proto app


async def test_denies_wrong_password(tmp_path):
    inner = _InnerApp()
    app = _app(tmp_path, inner=inner)
    status, _b, _h = await _call(
        app, path="/", headers=_basic("james", "wrong")
    )
    assert status == 401
    assert inner.called is False


async def test_empty_password_denies_even_correct_form(tmp_path):
    # Fail-closed: no password configured -> serve nobody.
    inner = _InnerApp()
    app = _app(tmp_path, password="", inner=inner)
    status, _b, _h = await _call(app, path="/", headers=_basic("james", ""))
    assert status == 401
    assert inner.called is False


async def test_serves_static_index_with_auth(tmp_path):
    app = _app(tmp_path)
    status, body, headers = await _call(
        app, path="/", headers=_basic("james", "s3cret")
    )
    assert status == 200
    assert b"james" in body
    assert headers[b"content-type"].startswith(b"text/html")


async def test_serves_static_asset_under_ui(tmp_path):
    app = _app(tmp_path)
    status, body, headers = await _call(
        app, path="/ui/app.js", headers=_basic("james", "s3cret")
    )
    assert status == 200 and b"// js" in body
    assert headers[b"content-type"].startswith(b"text/javascript")


async def test_static_traversal_is_refused(tmp_path):
    app = _app(tmp_path)
    status, _b, _h = await _call(
        app,
        path="/ui/../../etc/passwd",
        headers=_basic("james", "s3cret"),
    )
    assert status == 404


async def test_api_path_delegates_to_inner(tmp_path):
    inner = _InnerApp()
    app = _app(tmp_path, inner=inner)
    status, body, _h = await _call(
        app,
        method="POST",
        path="/james.v1.DispatchService/ListSessions",
        headers=_basic("james", "s3cret"),
    )
    assert status == 200
    assert inner.called is True
    assert b'"inner":true' in body


async def test_non_http_scope_passes_through(tmp_path):
    # lifespan/websocket scopes go straight to the wrapped app (which handles
    # lifespan); the auth gate is HTTP-only.
    seen = {}

    async def inner(scope, receive, send):
        seen["type"] = scope["type"]

    app = WebApp(
        inner, username="james", password="x", static_dir=str(tmp_path)
    )
    await app({"type": "lifespan"}, None, None)
    assert seen["type"] == "lifespan"

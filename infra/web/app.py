"""ASGI wrapper for the optional web dashboard: HTTP Basic auth + static SPA.

Wraps the invariant ``Server.asgi_app()`` so the dashboard rides the SAME
DispatchService projection — a web prompt goes through the exact ``dispatch``
path and per-child secret-stripping as the chat channels, with no new
secret-exposure surface. Auth is HTTP Basic at the transport edge: the channels
call the server in-process (never through ASGI), so this gate never touches the
channel path.

Fail-closed: an unset/empty password denies every request (mirrors the
empty-allowlist-serves-nobody rule). Static SPA files are served from a fixed
directory (``/`` and ``/ui/...``); path traversal outside it is refused;
everything else (the Connect-JSON ``/{service}/{method}`` calls, healthz, the
catalog) is delegated to the wrapped app — behind the same auth.

This is a transport adapter (like the chat channels in infra/channels): it
exchanges only bytes/primitives and imports no domain types.
"""

from __future__ import annotations

import base64
import hmac
from collections.abc import Awaitable, Callable
from pathlib import Path

_ASGIApp = Callable[[dict, Callable, Callable], Awaitable[None]]

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class WebApp:
    """Basic-auth gate over static SPA files and the wrapped proto app."""

    def __init__(
        self,
        inner: _ASGIApp,
        *,
        username: str,
        password: str,
        static_dir: str,
    ) -> None:
        self._inner = inner
        self._username = username
        self._password = password
        self._static = Path(static_dir).resolve()

    async def __call__(self, scope: dict, receive, send) -> None:
        # Only HTTP is gated/served here; lifespan + anything else go straight
        # to the wrapped app (which handles lifespan and ignores the rest).
        if scope.get("type") != "http":
            await self._inner(scope, receive, send)
            return
        if not self._authorized(scope):
            await self._deny(send)
            return
        path = scope.get("path", "/")
        if scope.get("method") == "GET" and (
            path == "/" or path.startswith("/ui")
        ):
            await self._serve_static(path, send)
            return
        await self._inner(scope, receive, send)

    def _authorized(self, scope: dict) -> bool:
        """True iff the request carries valid HTTP Basic creds. Fail-closed."""
        if not self._password:
            return False  # no password configured -> serve nobody
        header = ""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                header = value.decode("latin-1")
                break
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        user, sep, pw = decoded.partition(":")
        if not sep:
            return False
        # Constant-time compare on both fields (avoid early-exit timing leaks).
        ok_user = hmac.compare_digest(user, self._username)
        ok_pw = hmac.compare_digest(pw, self._password)
        return ok_user and ok_pw

    async def _deny(self, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="james"'),
                    (b"content-type", b"text/plain; charset=utf-8"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"401 Unauthorized"})

    async def _serve_static(self, path: str, send) -> None:
        rel = (
            "index.html"
            if path in ("/", "/ui", "/ui/")
            else path[len("/ui/") :]
        )
        target = (self._static / rel).resolve()
        # Refuse anything outside the static dir (path traversal).
        if target != self._static and self._static not in target.parents:
            await self._not_found(send)
            return
        if not target.is_file():
            await self._not_found(send)
            return
        body = target.read_bytes()
        ctype = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", ctype.encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _not_found(self, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": b"404 Not Found"})

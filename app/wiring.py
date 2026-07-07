"""Composition root, importable: build a ready-to-invoke james Server.

This is the embedding seam. An application can load (or construct) a Config,
call ``build_server``, and drive the DispatchService in-process — no channels,
no HTTP, no assumption of this repo's checkout layout::

    from pathlib import Path

    from james.v1 import james_pb2

    from app.config import load_config
    from app.wiring import build_server, build_session_store

    config_path = Path("/etc/james/config.yaml")
    config = load_config(config_path)
    root = config_path.parent
    server = build_server(
        config, root=root, store=build_session_store(config, root)
    )
    response = await server.invoke(
        "DispatchService.Dispatch",
        james_pb2.DispatchRequest(backend="claude", prompt="…"),
    )

Relative paths in the config resolve against ``root`` (normally the config
file's directory), so the embedding application controls where all state
lives. With ``sessions.store_path: ""`` there is no store at all and dispatch
is stateless — every request is a self-contained (backend, prompt) -> Result
call. The same service is available out-of-process via the HTTP projection
(``server.serve(http=port)`` or ``server.asgi_app()``).
"""

from __future__ import annotations

from pathlib import Path

from invariant import Server

from apis.dispatch_server import DispatchServiceImpl
from app.config import Config
from biz.dispatch import SessionStore
from infra.backends.cli import SubprocessCliRunner
from infra.clients.a2a import A2ASdkCaller
from infra.clients.chat import ChatApiCaller
from infra.sessions.store import JsonSessionStore

# The committed proto descriptor, shipped alongside the code.
_DESCRIPTOR = (
    Path(__file__).resolve().parent.parent / "gen" / "descriptor.binpb"
)


def build_session_store(config: Config, root: Path) -> JsonSessionStore | None:
    """Build the per-conversation session store, or None when disabled.

    An empty ``sessions.store_path`` disables session memory entirely: no file
    is ever written and every dispatch is stateless (create-only, no resume).
    """
    if not config.session_store_path:
        return None
    return JsonSessionStore(str((root / config.session_store_path).resolve()))


def build_server(
    config: Config,
    *,
    root: Path,
    store: SessionStore | None = None,
    descriptor: str = "",
) -> Server:
    """Build the invariant Server with the DispatchService registered.

    Args:
        config: The validated configuration (see app.config.load_config).
        root: Base directory against which the config's relative paths
            (working_dir, browser.profiles_dir, mcp.config_path) resolve —
            normally the config file's directory.
        store: Session store port, or None for stateless dispatch. Embedders
            may inject their own SessionStore implementation.
        descriptor: Path to a compiled proto descriptor; defaults to the
            committed gen/descriptor.binpb next to this package.

    Returns:
        A Server ready for in-process ``invoke`` or the HTTP/ASGI projections.
    """
    descriptor = descriptor or str(_DESCRIPTOR)
    mcp_config = (
        str((root / config.mcp_config_path).resolve())
        if config.mcp_config_path
        else ""
    )
    servicer = DispatchServiceImpl(
        cli_runner=SubprocessCliRunner(),
        api_caller=ChatApiCaller(descriptor),
        a2a_caller=A2ASdkCaller(),
        default_backend=config.default_backend,
        cwd=str((root / config.working_dir).resolve()),
        profiles_dir=str((root / config.browser.profiles_dir).resolve()),
        default_profile=config.browser.default_profile,
        mcp_config=mcp_config,
        session_store=store,
    )
    server = Server.from_descriptor(descriptor)
    server.register(servicer)
    return server

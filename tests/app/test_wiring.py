"""Unit tests for the importable composition root (the embedding seam).

Builds the real Server from a config file in an arbitrary directory and
invokes it in-process — proving an application can embed james without the
channels, HTTP, or this repo's checkout layout. Unit-tier: only read-only RPCs
are invoked (no network, no keys, no subprocesses).
"""

from __future__ import annotations

from james.v1 import james_pb2

from app.config import load_config
from app.wiring import build_server, build_session_store


def _write_config(tmp_path, body="default_backend: claude\n"):
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


async def test_embedded_server_invokes_in_process(tmp_path):
    # The embedding seam end-to-end: config anywhere on disk -> build_server
    # -> in-process invoke, over the committed descriptor.
    path = _write_config(tmp_path)
    config = load_config(path)
    server = build_server(config, root=path.parent)
    try:
        resp = await server.invoke(
            "DispatchService.ListBackends", james_pb2.ListBackendsRequest()
        )
    finally:
        await server.stop()
    kinds = {b.name: b.kind for b in resp.backends}
    assert kinds["claude"] == "cli"
    assert kinds["gpt"] == "api"
    assert kinds["openclaw"] == "a2a"
    assert resp.default_backend == "claude"


def test_empty_store_path_disables_sessions(tmp_path):
    # Stateless mode: "" means no store at all — nothing is ever written and
    # every dispatch is a self-contained one-shot.
    path = _write_config(tmp_path, 'sessions:\n  store_path: ""\n')
    config = load_config(path)
    assert build_session_store(config, path.parent) is None


def test_default_store_path_builds_store(tmp_path):
    path = _write_config(tmp_path)
    config = load_config(path)
    assert build_session_store(config, path.parent) is not None

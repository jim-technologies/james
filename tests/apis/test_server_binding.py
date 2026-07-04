"""The invariant Server seam: reflection binding + in-process invoke.

``Server.from_descriptor`` + ``register()`` bind the servicer's methods to the
proto RPCs by method-name matching — the load-bearing glue between the
committed descriptor and DispatchServiceImpl. This exercises that binding
end-to-end in-process (the same invoke path the channels use). Unit-tier: no
network, no keys.
"""

from __future__ import annotations

from conftest import FakeApiCaller, FakeCliRunner, FakeSessionStore
from invariant import Server
from james.v1 import james_pb2

from apis.dispatch_server import DispatchServiceImpl

_DESCRIPTOR = "gen/descriptor.binpb"


async def test_server_binds_and_invokes_dispatch_and_list_sessions():
    store = FakeSessionStore()
    await store.record("claude", "42:7", "sid-1")
    server = Server.from_descriptor(_DESCRIPTOR)
    server.register(
        DispatchServiceImpl(
            cli_runner=FakeCliRunner(stdout="pong"),
            api_caller=FakeApiCaller(),
            default_backend="claude",
            cwd=".",
            session_store=store,
        )
    )
    try:
        resp = await server.invoke(
            "DispatchService.Dispatch",
            james_pb2.DispatchRequest(backend="claude", prompt="hi"),
        )
        assert resp.ok and resp.text == "pong"

        listed = await server.invoke(
            "DispatchService.ListSessions", james_pb2.ListSessionsRequest()
        )
        pairs = [(s.backend, s.conversation_id) for s in listed.sessions]
        assert pairs == [("claude", "42:7")]
    finally:
        await server.stop()

"""Unit tests for the DispatchService servicer seam (proto <-> biz)."""

from __future__ import annotations

import asyncio

from conftest import FakeApiCaller, FakeCliRunner, FakeSessionStore
from james.v1 import james_pb2

from apis.dispatch_server import DispatchServiceImpl


def _servicer(runner=None, api=None, session_store=None):
    return DispatchServiceImpl(
        cli_runner=runner or FakeCliRunner(),
        api_caller=api or FakeApiCaller(),
        default_backend="claude",
        cwd=".",
        session_store=session_store,
    )


class _ProbeCliRunner:
    """Records the peak number of overlapping run() calls."""

    def __init__(self):
        self.active = 0
        self.peak = 0

    async def run(self, argv, prompt, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.02)  # hold the slot so a racing call can overlap
        self.active -= 1
        return (0, "ok", "", b"", "")


async def test_servicer_maps_artifact_bytes_to_proto():
    runner = FakeCliRunner(returncode=0, stdout="", artifact_bytes=b"PNGDATA")
    resp = await _servicer(runner=runner).Dispatch(
        james_pb2.DispatchRequest(backend="shot", prompt="https://x"), None
    )
    assert resp.ok
    assert len(resp.artifacts) == 1
    assert (
        resp.artifacts[0].content == b"PNGDATA"
    )  # bytes inlined, no host path
    assert resp.artifacts[0].mime == "image/png"
    assert resp.artifacts[0].filename == "shot.png"


async def test_servicer_text_backend_has_no_artifacts():
    resp = await _servicer(runner=FakeCliRunner(stdout="hi")).Dispatch(
        james_pb2.DispatchRequest(backend="claude", prompt="hi"), None
    )
    assert resp.ok and resp.text == "hi"
    assert list(resp.artifacts) == []


async def test_servicer_unknown_backend_returns_help():
    resp = await _servicer().Dispatch(
        james_pb2.DispatchRequest(backend="nope", prompt="hi"), None
    )
    assert not resp.ok
    assert "Unknown backend" in resp.error


async def test_servicer_list_sessions_maps_store():
    store = FakeSessionStore()
    await store.resolve("claude", "42:7")
    await store.record("codex", "9:1", "thread-abc")
    resp = await _servicer(session_store=store).ListSessions(
        james_pb2.ListSessionsRequest(), None
    )
    pairs = sorted((s.backend, s.conversation_id) for s in resp.sessions)
    assert pairs == [("claude", "42:7"), ("codex", "9:1")]


async def test_servicer_list_sessions_empty_without_store():
    resp = await _servicer().ListSessions(james_pb2.ListSessionsRequest(), None)
    assert list(resp.sessions) == []


async def test_same_conversation_dispatches_serialize():
    # Two messages in one thread must not run concurrently (concurrent runs on
    # one agent session id collide); the second waits for the first.
    runner = _ProbeCliRunner()
    svc = _servicer(runner=runner, session_store=FakeSessionStore())

    def req():
        return james_pb2.DispatchRequest(
            backend="claude", prompt="hi", conversation_id="42:7"
        )

    await asyncio.gather(
        svc.Dispatch(req(), None),
        svc.Dispatch(req(), None),
        svc.Dispatch(req(), None),
    )
    assert runner.peak == 1


async def test_different_conversations_run_concurrently():
    # Distinct threads are independent and should not block each other.
    runner = _ProbeCliRunner()
    svc = _servicer(runner=runner, session_store=FakeSessionStore())
    await asyncio.gather(
        svc.Dispatch(
            james_pb2.DispatchRequest(
                backend="claude", prompt="hi", conversation_id="a"
            ),
            None,
        ),
        svc.Dispatch(
            james_pb2.DispatchRequest(
                backend="claude", prompt="hi", conversation_id="b"
            ),
            None,
        ),
    )
    assert runner.peak == 2

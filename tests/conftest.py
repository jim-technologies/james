"""Shared test fixtures and the two-tier (unit/live) harness.

Unit tests always run with no network or credentials, using injected fakes. Live
tests are marked ``@pytest.mark.live`` and skipped unless RUN_LIVE_TESTS is set;
``requires_env`` further skips a live test when a needed credential is absent.
Dependencies are injected (Protocols / fakes), never monkeypatched.
"""

from __future__ import annotations

import os

import pytest
from james.v1 import james_pb2


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: test that hits the network or real CLIs (gated)"
    )


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_LIVE_TESTS"):
        return
    skip = pytest.mark.skip(reason="live test; set RUN_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


def requires_env(*names: str):
    """Return a skipif marker for live tests missing a required credential."""
    missing = [n for n in names if not os.environ.get(n)]
    return pytest.mark.skipif(
        bool(missing), reason=f"missing env: {', '.join(missing)}"
    )


class FakeCliRunner:
    """A CliRunner that records its calls and returns a canned 5-tuple."""

    def __init__(
        self,
        returncode=0,
        stdout="ok",
        stderr="",
        artifact_bytes=b"",
        captured_id="",
    ):
        self.calls: list[dict] = []
        self._result = (returncode, stdout, stderr)
        self._artifact_bytes = artifact_bytes
        self._captured_id = captured_id

    async def run(
        self,
        argv,
        prompt,
        *,
        cwd,
        env_set,
        env_unset,
        secret_env,
        timeout_s,
        wants_artifact=False,
        artifact_suffix="",
        user_data_dir="",
        capture_stream="stdout",
        capture_format="",
        capture_field="",
        capture_event="",
        reply_format="",
        reply_field="",
        reply_event="",
        reply_match="",
    ):
        self.calls.append(
            {
                "argv": list(argv),
                "prompt": prompt,
                "cwd": cwd,
                "env_set": dict(env_set),
                "env_unset": list(env_unset),
                "secret_env": dict(secret_env),
                "wants_artifact": wants_artifact,
                "artifact_suffix": artifact_suffix,
                "user_data_dir": user_data_dir,
                "capture_format": capture_format,
                "reply_format": reply_format,
            }
        )
        rc, out, err = self._result
        return (
            rc,
            out,
            err,
            self._artifact_bytes if wants_artifact else b"",
            # Mirror the real runner: an id is only ever captured when a
            # capture rule is configured for the backend.
            self._captured_id if capture_format else "",
        )


class FakeApiCaller:
    """An ApiCaller that records its calls and returns a canned result."""

    def __init__(self, ok=True, text="api-ok", error=""):
        self.calls: list[dict] = []
        self._result = (ok, text, error)

    async def call(
        self,
        prompt,
        *,
        base_url,
        model,
        service_name,
        tool_name,
        api_key_env,
        timeout_s,
    ):
        self.calls.append(
            {"prompt": prompt, "model": model, "base_url": base_url}
        )
        return self._result


class FakeSessionStore:
    """An in-memory SessionStore: entry presence = resume; new id per create.

    Mirrors JsonSessionStore: the first resolve for a key creates (resume=False)
    and records the id; later resolves return that id with resume=True. ``forget``
    drops the entry so the next resolve creates a fresh id (suffixed so a
    post-forget id is distinguishable from the original in assertions).
    """

    def __init__(self):
        self.ids: dict[tuple[str, str], str] = {}
        self.gen: dict[tuple[str, str], int] = {}
        self.calls: list[tuple] = []

    async def resolve(self, backend, key, *, mint=True):
        k = (backend, key)
        self.calls.append(("resolve", backend, key))
        if k not in self.ids:
            if not mint:
                return ("", False)  # capture: id arrives via record() later
            n = self.gen.get(k, 0) + 1
            self.gen[k] = n
            base = f"sid-{backend}-{key}"
            self.ids[k] = base if n == 1 else f"{base}-{n}"
            return (self.ids[k], False)
        return (self.ids[k], True)

    async def record(self, backend, key, session_id):
        if not session_id:
            return  # mirror JsonSessionStore: empty ids are never persisted
        self.ids[(backend, key)] = session_id
        self.calls.append(("record", backend, key, session_id))

    async def forget(self, backend, key):
        self.ids.pop((backend, key), None)
        self.calls.append(("forget", backend, key))

    async def reset(self, key):
        stale = [k for k in self.ids if k[1] == key]
        for k in stale:
            del self.ids[k]
        self.calls.append(("reset", key))
        return len(stale)

    async def list_sessions(self):
        return list(self.ids.keys())


class FakeA2ACaller:
    """An A2ACaller that records its calls and returns a canned result."""

    def __init__(self, ok=True, text="a2a-ok", error="", artifacts=()):
        self.calls: list[dict] = []
        self._result = (ok, text, error, tuple(artifacts))

    async def call(
        self,
        prompt,
        *,
        base_url,
        agent_card_path,
        token_env,
        timeout_s,
    ):
        self.calls.append(
            {"prompt": prompt, "base_url": base_url, "token_env": token_env}
        )
        return self._result


class FakeChannel:
    """An in-memory channel: applies the fail-closed allowlist, records sends.

    Uses the real shared helpers (parse_command, chunk_text) so its behaviour
    mirrors the production channels without any network transport.
    """

    def __init__(
        self, *, allowed_ids, invoker, default_backend="claude", limit=4096
    ):
        from infra.channels.common import chunk_text, parse_command

        self._allowed = set(allowed_ids)
        self._invoke = invoker
        self._default = default_backend
        self._limit = limit
        self._chunk = chunk_text
        self._parse = parse_command
        self.sent: list[tuple[int, str]] = []

    async def deliver(self, chat_id: int, text: str) -> bool:
        """Simulate an inbound message; return False if not allowlisted."""
        if chat_id not in self._allowed:
            return False  # fail-closed
        backend, prompt = self._parse(text)
        if not prompt:
            return True
        shown = backend or f"{self._default} (default)"
        await self._send(chat_id, f"▶ running on {shown}…")
        response = await self._invoke(
            james_pb2.DispatchRequest(
                backend=backend,
                prompt=prompt,
                channel="fake",
                conversation_id=str(chat_id),
            )
        )
        body = response.text if response.ok else f"⚠️ {response.error}"
        await self._send(chat_id, f"[{response.backend}] {body}")
        return True

    async def _send(self, chat_id: int, text: str) -> None:
        for chunk in self._chunk(text, self._limit):
            self.sent.append((chat_id, chunk))

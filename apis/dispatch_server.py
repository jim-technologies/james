"""The DispatchService servicer — the thin seam between proto and biz.

invariant-protocol binds this class to the DispatchService RPCs by matching
method names, so the async ``Dispatch`` method below implements the ``Dispatch``
RPC. The method is purely translational — all validation and routing live in
``biz.dispatch.dispatch`` — and maps the Result back to a DispatchResponse. No
domain logic lives here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping

from james.v1 import james_pb2

from biz.backends import REGISTRY, Backend
from biz.dispatch import A2ACaller, ApiCaller, CliRunner, SessionStore, dispatch


class DispatchServiceImpl:
    """Implements the DispatchService.Dispatch RPC over the business core."""

    def __init__(
        self,
        *,
        cli_runner: CliRunner,
        api_caller: ApiCaller,
        default_backend: str,
        cwd: str,
        profiles_dir: str = "",
        default_profile: str = "default",
        mcp_config: str = "",
        session_store: SessionStore | None = None,
        a2a_caller: A2ACaller | None = None,
        registry: Mapping[str, Backend] = REGISTRY,
    ) -> None:
        self._cli_runner = cli_runner
        self._api_caller = api_caller
        self._a2a_caller = a2a_caller
        self._registry = registry
        self._default_backend = default_backend
        self._cwd = cwd
        self._profiles_dir = profiles_dir
        self._default_profile = default_profile
        self._mcp_config = mcp_config
        self._session_store = session_store
        # One lock per conversation so two messages in the same chat thread run
        # one-at-a-time: a follow-up sent while the previous run is still going
        # waits its turn instead of racing the agent session (two concurrent
        # runs on one session id collide). Stateless calls (no conversation id,
        # e.g. the one-shot CLI) are not serialized.
        self._convo_locks: dict[str, asyncio.Lock] = {}

    # Method name is PascalCase to match the proto RPC (invariant-protocol binds
    # by reflection). N802 is ignored for apis/ in pyproject.
    async def Dispatch(self, request, context):
        """Delegate to biz and translate the Result to a DispatchResponse."""
        # All validation and routing (empty prompt, unknown backend, defaults,
        # profile selection) lives in biz.dispatch; this seam is translational.
        convo = request.conversation_id
        lock = (
            self._convo_locks.setdefault(convo, asyncio.Lock())
            if convo
            else contextlib.nullcontext()
        )
        async with lock:
            result = await dispatch(
                request.backend,
                request.prompt,
                cwd=self._cwd,
                default_backend=self._default_backend,
                cli_runner=self._cli_runner,
                api_caller=self._api_caller,
                a2a_caller=self._a2a_caller,
                registry=self._registry,
                profiles_dir=self._profiles_dir,
                default_profile=self._default_profile,
                mcp_config=self._mcp_config,
                session_key=request.conversation_id,
                session_store=self._session_store,
            )
        return james_pb2.DispatchResponse(
            backend=result.backend,
            ok=result.ok,
            text=result.text,
            error=result.error,
            artifacts=[
                james_pb2.Artifact(
                    content=a.content, mime=a.mime, filename=a.filename
                )
                for a in result.artifacts
            ],
        )

    # PascalCase to match the proto RPC (see Dispatch). Read-only listing so a
    # client (the web dashboard, an embedding app) can offer the backend
    # choices without duplicating the registry.
    async def ListBackends(self, request, context):
        """Return the registry rows and the configured default backend."""
        return james_pb2.ListBackendsResponse(
            backends=[
                james_pb2.BackendInfo(name=b.name, kind=b.kind)
                for b in self._registry.values()
            ],
            default_backend=self._default_backend,
        )

    # PascalCase to match the proto RPC (see Dispatch). Read-only listing for a
    # client (the web dashboard) to show stored conversations.
    async def ListSessions(self, request, context):
        """Return the stored (backend, conversation) sessions."""
        store = self._session_store
        sessions = await store.list_sessions() if store is not None else []
        return james_pb2.ListSessionsResponse(
            sessions=[
                james_pb2.SessionInfo(backend=backend, conversation_id=key)
                for backend, key in sessions
            ]
        )

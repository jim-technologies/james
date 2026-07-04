"""A2A client for a2a backends — via the official a2a-sdk, gRPC-primary.

Talks the A2A protocol to a remote agent using the official ``a2a-sdk``. The
transport is negotiated from the peer's Agent Card with **gRPC preferred**,
falling back to JSON-RPC / HTTP+JSON; the SDK speaks A2A v1.0 with v0.3
compatibility, so this one client reaches both. Implements the biz A2ACaller
port: returns (ok, text, error, artifacts) where each artifact is a
(bytes, mime, filename) triple; a missing token or any error is returned, never
raised. Auth is a bearer token (the peer's shared secret) supplied per the
card's security scheme via an AuthInterceptor — james never forwards any other
secret to a peer.

The proto Part oneof (text / raw bytes / url / data) is mapped to text + file
artifacts; the SDK aggregates streaming updates and (with polling) resolves a
Task to a terminal state, so a single send_message yields the final result.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from collections.abc import Iterable

import grpc
import httpx
from a2a.client import (
    AuthInterceptor,
    ClientConfig,
    CredentialService,
    create_client,
)
from a2a.client.client_factory import TransportProtocol
from a2a.types import Message, Part, Role, SendMessageRequest, TaskState

_Artifacts = tuple[tuple[bytes, str, str], ...]
_Result = tuple[bool, str, str, _Artifacts]

# Prefer gRPC, then JSON-RPC, then HTTP+JSON (with use_client_preference).
_PREFERENCE = [
    TransportProtocol.GRPC,
    TransportProtocol.JSONRPC,
    TransportProtocol.HTTP_JSON,
]
_FAILED_STATES = {
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _grpc_channel(url: str):
    """Channel factory for the SDK: plaintext to loopback, TLS otherwise."""
    target = url
    for scheme in ("grpc://", "grpcs://", "https://", "http://", "dns:///"):
        if target.startswith(scheme):
            target = target[len(scheme) :]
            break
    target = target.rstrip("/")
    host = target.rsplit(":", 1)[0].strip("[]")
    if host in _LOOPBACK:
        return grpc.aio.insecure_channel(target)
    return grpc.aio.secure_channel(target, grpc.ssl_channel_credentials())


class _StaticBearer(CredentialService):
    """Supplies one bearer token for any security scheme the peer declares."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_credentials(self, security_scheme_name, context=None):
        return self._token


def _collect(
    parts: Iterable, texts: list[str], arts: list[tuple[bytes, str, str]]
) -> None:
    """Append a Part oneof's text / file content to the accumulators."""
    for part in parts:
        which = part.WhichOneof("content")
        if which == "text":
            if part.text:
                texts.append(part.text)
        elif which == "raw":
            mime = part.media_type or "application/octet-stream"
            arts.append((part.raw, mime, part.filename or "file"))
        elif which == "url":
            if part.url:
                texts.append(
                    f"[file: {part.url}]"
                )  # binary URI: surfaced, not fetched


def _events_to_result(events: Iterable) -> _Result:
    """Aggregate the SDK's StreamResponse events into a normalised tuple."""
    texts: list[str] = []
    arts: list[tuple[bytes, str, str]] = []
    failed = ""
    for event in events:
        payload = event.WhichOneof("payload")
        if payload == "message":
            _collect(event.message.parts, texts, arts)
        elif payload == "task":
            task = event.task
            _collect(task.status.message.parts, texts, arts)
            for artifact in task.artifacts:
                _collect(artifact.parts, texts, arts)
            if task.status.state in _FAILED_STATES:
                failed = TaskState.Name(task.status.state)
        # status_update / artifact_update only occur when streaming; with
        # streaming disabled the SDK yields the final message/task above.
    text = "\n".join(texts).strip()
    if failed and not arts and not text:
        return (False, "", f"a2a task {failed.lower()}", ())
    return (True, text, "", tuple(arts))


class A2ASdkCaller:
    """Calls a remote A2A peer via a2a-sdk (A2ACaller port; gRPC-primary)."""

    async def call(
        self,
        prompt: str,
        *,
        base_url: str,
        agent_card_path: str,
        token_env: str,
        timeout_s: float,
    ) -> _Result:
        """Negotiate transport, send the prompt, return a result tuple."""
        token = os.environ.get(token_env) if token_env else None
        if not token:
            name = token_env or "the configured env var"
            return (
                False,
                "",
                f"missing token: set {name} for this backend",
                (),
            )
        if not base_url:
            return (False, "", "no base_url configured for this backend", ())
        try:
            return await asyncio.wait_for(
                self._send(prompt, base_url, agent_card_path, token, timeout_s),
                timeout=timeout_s + 5,
            )
        except TimeoutError:
            return (False, "", f"a2a timed out after {timeout_s:g}s", ())
        except Exception as exc:  # never raise out of infra
            return (False, "", f"a2a call failed: {exc}", ())

    async def _send(
        self,
        prompt: str,
        base_url: str,
        agent_card_path: str,
        token: str,
        timeout_s: float,
    ) -> _Result:
        http = httpx.AsyncClient(timeout=timeout_s)
        try:
            config = ClientConfig(
                streaming=False,
                polling=True,
                httpx_client=http,
                grpc_channel_factory=_grpc_channel,
                supported_protocol_bindings=list(_PREFERENCE),
                use_client_preference=True,
                accepted_output_modes=["text/plain"],
            )
            client = await create_client(
                base_url,
                client_config=config,
                interceptors=[AuthInterceptor(_StaticBearer(token))],
                relative_card_path=(
                    agent_card_path or "/.well-known/agent-card.json"
                ),
            )
            try:
                request = SendMessageRequest(
                    message=Message(
                        role=Role.ROLE_USER,
                        parts=[Part(text=prompt)],
                        message_id=str(uuid.uuid4()),
                    )
                )
                events = [ev async for ev in client.send_message(request)]
                return _events_to_result(events)
            finally:
                closed = client.close()
                if inspect.isawaitable(closed):
                    await closed
        finally:
            await http.aclose()

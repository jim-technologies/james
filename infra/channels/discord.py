"""Discord channel adapter — gateway (websockets) + REST (httpx), self-built.

No discord.py: connect to the gateway, IDENTIFY, heartbeat, and on each
MESSAGE_CREATE enforce a fail-closed channel-id allowlist, build a
DispatchRequest and invoke the service, then reply over REST (chunked to
Discord's 2000-character limit). Acks immediately and dispatches in a bounded
background task so a long run never blocks the gateway. Reconnects on drop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable

import httpx
import websockets
from james.v1 import james_pb2
from websockets.exceptions import WebSocketException

from infra.channels.common import Invoker, chunk_text, parse_command

_DISCORD_LIMIT = 2000
_GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
_REST = "https://discord.com/api/v10"
# GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
_INTENTS = (1 << 9) | (1 << 12) | (1 << 15)
_TIMEOUT = httpx.Timeout(60.0)


class DiscordChannel:
    """Connects to the Discord gateway and dispatches incoming messages."""

    def __init__(
        self,
        *,
        token: str,
        allowed_channel_ids: set[int],
        invoker: Invoker,
        default_backend: str,
        max_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
        reset_session: Callable[[str], Awaitable[int]] | None = None,
    ) -> None:
        self._token = token
        self._allowed = allowed_channel_ids
        self._invoke = invoker
        self._default_backend = default_backend
        self._sem = asyncio.Semaphore(max_concurrency)
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT)
        self._headers = {"Authorization": f"Bot {token}"}
        self._self_id: str | None = None
        self._seq: int | None = None
        self._reset_session = reset_session

    async def run(self) -> None:
        """Maintain a gateway connection, reconnecting after a drop."""
        while True:
            try:
                await self._session()
            except (WebSocketException, OSError):
                await asyncio.sleep(5)  # reconnect after a brief backoff

    async def _session(self) -> None:
        """One gateway session: hello, identify, heartbeat, receive loop."""
        # Each session is a fresh IDENTIFY (not a RESUME), so the sequence
        # restarts: heartbeat with null until this session sees a frame.
        self._seq = None
        tasks: set[asyncio.Task] = set()
        async with websockets.connect(_GATEWAY, max_size=None) as ws:
            hello = json.loads(await ws.recv())
            interval = hello["d"]["heartbeat_interval"] / 1000
            await ws.send(json.dumps(self._identify()))
            heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                async for raw in ws:
                    await self._on_event(ws, json.loads(raw), tasks)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _on_event(self, ws, event: dict, tasks: set) -> None:
        """Handle one gateway frame: track sequence, heartbeat, dispatch."""
        if event.get("s") is not None:
            self._seq = event["s"]
        op = event.get("op")
        if op == 1:  # gateway requested an immediate heartbeat
            await ws.send(json.dumps({"op": 1, "d": self._seq}))
            return
        if op != 0:  # only DISPATCH frames carry events we care about
            return
        name = event.get("t")
        if name == "READY":
            self._self_id = event["d"]["user"]["id"]
        elif name == "MESSAGE_CREATE":
            task = asyncio.create_task(self._handle(event["d"]))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    def _identify(self) -> dict:
        """Build the IDENTIFY payload (op 2)."""
        return {
            "op": 2,
            "d": {
                "token": self._token,
                "intents": _INTENTS,
                "properties": {
                    "os": "linux",
                    "browser": "james",
                    "device": "james",
                },
            },
        }

    async def _heartbeat(self, ws, interval: float) -> None:
        """Send a heartbeat (op 1) every ``interval`` seconds."""
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": 1, "d": self._seq}))

    async def _handle(self, message: dict) -> None:
        """Allowlist-check one message, dispatch it, and reply."""
        author = message.get("author", {})
        if author.get("bot") or author.get("id") == self._self_id:
            return  # ignore other bots and our own messages
        try:
            channel_id = int(message.get("channel_id", 0))
        except (TypeError, ValueError):
            return
        if channel_id not in self._allowed:
            return  # fail-closed
        backend, prompt = parse_command(message.get("content") or "")
        # Each channel (a Discord thread is its own channel id) is its own
        # conversation/session; `/reset` forgets it — mirroring Telegram.
        if backend == "reset" and self._reset_session is not None:
            removed = await self._reset_session(str(channel_id))
            note = "started fresh" if removed else "nothing to reset"
            await self._send(channel_id, f"🆕 {note} for this channel.")
            return
        if not prompt:
            return
        shown = backend or f"{self._default_backend} (default)"
        await self._send(channel_id, f"▶ running on {shown}…")
        async with self._sem:
            try:
                response = await self._invoke(
                    james_pb2.DispatchRequest(
                        backend=backend,
                        prompt=prompt,
                        channel="discord",
                        conversation_id=str(channel_id),
                    )
                )
            except Exception as exc:
                # Having acked, we owe the user a terminal message even if the
                # dispatch raises unexpectedly (this task is fire-and-forget).
                await self._send(channel_id, f"⚠️ dispatch failed: {exc}")
                return
        if not response.ok:
            await self._send(
                channel_id, f"⚠️ [{response.backend}] {response.error}"
            )
        elif response.text:
            await self._send(
                channel_id, f"[{response.backend}] {response.text}"
            )
        for artifact in response.artifacts:
            if not await self._send_artifact(channel_id, artifact):
                # Acked already, so a failed upload still owes a reply.
                name = artifact.filename or "file"
                await self._send(channel_id, f"⚠️ couldn't deliver {name}")

    async def _send(self, channel_id: int, text: str) -> None:
        """Send a message over REST, chunked to Discord's limit."""
        for chunk in chunk_text(text, _DISCORD_LIMIT):
            with contextlib.suppress(httpx.HTTPError):
                await self._client.post(
                    f"{_REST}/channels/{channel_id}/messages",
                    headers=self._headers,
                    json={"content": chunk},
                    timeout=_TIMEOUT,
                )

    async def _send_artifact(
        self, channel_id: int, artifact: james_pb2.Artifact
    ) -> bool:
        """Upload a file artifact to the channel as a Discord attachment."""
        if not artifact.content:
            return False
        try:
            resp = await self._client.post(
                f"{_REST}/channels/{channel_id}/messages",
                headers=self._headers,
                files={
                    "files[0]": (
                        artifact.filename or "file",
                        artifact.content,
                        artifact.mime or "application/octet-stream",
                    )
                },
                timeout=_TIMEOUT,
            )
            return resp.is_success
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

"""Telegram channel adapter — long-poll getUpdates, dispatch, sendMessage.

Thin by design: receive a message, enforce a fail-closed chat-id allowlist,
build a DispatchRequest and invoke the service, and render the reply (chunked to
Telegram's 4096-character limit) in the originating topic. Each forum topic is a
separate conversation (its own resumable agent session); `/reset` forgets one.
Each task is acknowledged immediately ("running on <backend>…") and runs in a
bounded background task, so a long-running agent never blocks the poll loop or a
second incoming message.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import httpx
from james.v1 import james_pb2

from infra.channels.common import (
    Invoker,
    chunk_text,
    parse_command,
    reset_note,
)

_TELEGRAM_LIMIT = 4096
_API = "https://api.telegram.org"
_TIMEOUT = httpx.Timeout(60.0)


class TelegramChannel:
    """Polls Telegram for messages and dispatches them through the service."""

    def __init__(
        self,
        *,
        token: str,
        allowed_chat_ids: set[int],
        invoker: Invoker,
        default_backend: str,
        max_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
        reset_session: Callable[[str], Awaitable[int]] | None = None,
    ) -> None:
        self._allowed = allowed_chat_ids
        self._invoke = invoker
        self._default_backend = default_backend
        self._sem = asyncio.Semaphore(max_concurrency)
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT)
        self._base = f"{_API}/bot{token}"
        self._reset_session = reset_session

    async def run(self) -> None:
        """Long-poll for updates and spawn a handler task per message."""
        offset = 0
        tasks: set[asyncio.Task] = set()
        while True:
            updates = await self._get_updates(offset)
            for update in updates:
                offset = max(offset, update.get("update_id", 0) + 1)
                message = update.get("message") or update.get("channel_post")
                if not message:
                    continue
                task = asyncio.create_task(self._handle(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

    async def _get_updates(self, offset: int) -> list[dict]:
        """Fetch the next batch of updates (long poll); return [] on error."""
        try:
            resp = await self._client.get(
                f"{self._base}/getUpdates",
                params={"offset": offset, "timeout": 50},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json().get("result", [])
        except (httpx.HTTPError, ValueError):
            await asyncio.sleep(3)  # transient: back off and retry
            return []

    async def _handle(self, message: dict) -> None:
        """Allowlist-check one message, dispatch it, and reply in its topic."""
        chat_id = message.get("chat", {}).get("id")
        if chat_id not in self._allowed:
            return  # fail-closed: silently ignore non-allowlisted chats
        # Each forum topic is its own conversation/session; bare chats use the
        # chat id. Replies go back to the same topic.
        thread_id = message.get("message_thread_id")
        convo = f"{chat_id}:{thread_id}" if thread_id else str(chat_id)
        backend, prompt = parse_command(message.get("text") or "")
        if backend == "reset":
            note = await reset_note(self._reset_session, convo)
            await self._send(chat_id, f"🆕 {note} for this thread.", thread_id)
            return
        if not prompt:
            await self._send(
                chat_id, "Send a task, e.g. /claude summarise…", thread_id
            )
            return
        shown = backend or f"{self._default_backend} (default)"
        await self._send(chat_id, f"▶ running on {shown}…", thread_id)
        async with self._sem:
            try:
                response = await self._invoke(
                    james_pb2.DispatchRequest(
                        backend=backend,
                        prompt=prompt,
                        channel="telegram",
                        conversation_id=convo,
                    )
                )
            except Exception as exc:
                # Having acked, we owe the user a terminal message even if the
                # dispatch raises unexpectedly (this task is fire-and-forget).
                await self._send(
                    chat_id, f"⚠️ dispatch failed: {exc}", thread_id
                )
                return
        if not response.ok:
            await self._send(
                chat_id, f"⚠️ [{response.backend}] {response.error}", thread_id
            )
        elif response.text:
            await self._send(
                chat_id, f"[{response.backend}] {response.text}", thread_id
            )
        for artifact in response.artifacts:
            if not await self._send_artifact(chat_id, artifact, thread_id):
                # Acked already, so a failed upload still owes a reply.
                name = artifact.filename or "file"
                await self._send(
                    chat_id, f"⚠️ couldn't deliver {name}", thread_id
                )

    async def _send(
        self, chat_id: int, text: str, thread_id: int | None = None
    ) -> None:
        """Send a message (chunked), in the given topic if any."""
        for chunk in chunk_text(text, _TELEGRAM_LIMIT):
            payload: dict = {"chat_id": chat_id, "text": chunk}
            if thread_id is not None:
                payload["message_thread_id"] = thread_id
            with contextlib.suppress(httpx.HTTPError):
                await self._client.post(
                    f"{self._base}/sendMessage", json=payload, timeout=_TIMEOUT
                )

    async def _send_artifact(
        self,
        chat_id: int,
        artifact: james_pb2.Artifact,
        thread_id: int | None = None,
    ) -> bool:
        """Upload a file artifact (image as a photo, else a document)."""
        if not artifact.content:
            return False
        is_image = artifact.mime.startswith("image/")
        endpoint = "sendPhoto" if is_image else "sendDocument"
        field = "photo" if is_image else "document"
        data = {"chat_id": str(chat_id)}
        if thread_id is not None:
            data["message_thread_id"] = str(thread_id)
        try:
            resp = await self._client.post(
                f"{self._base}/{endpoint}",
                data=data,
                files={
                    field: (
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

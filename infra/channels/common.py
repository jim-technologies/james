"""Shared helpers for channel adapters: command parsing and message chunking.

These are the only pieces both channels genuinely share. Routing lives in biz;
channels only parse an inbound message into (backend, prompt), invoke the
service, and chunk the reply to the platform's per-message limit.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from james.v1 import james_pb2

# An injected entry point into the service: build a DispatchRequest, get a
# DispatchResponse back. Channels never touch backends or routing directly.
Invoker = Callable[
    [james_pb2.DispatchRequest], Awaitable[james_pb2.DispatchResponse]
]


def parse_command(text: str) -> tuple[str, str]:
    """Split an incoming message into (backend, prompt).

    A leading "/word" selects a backend ("/claude summarise" -> ("claude",
    "summarise")), and a "@botname" suffix on the command word is stripped. The
    command word ends at the FIRST whitespace of any kind — a newline right
    after the command (common when pasting a multi-line prompt) starts the
    prompt just like a space. Any other message routes to the default backend
    (an empty backend is returned).

    Args:
        text: The raw inbound message text.

    Returns:
        A (backend, prompt) pair; backend is "" to mean "use the default".
    """
    stripped = text.strip()
    if stripped.startswith("/"):
        head, *rest = re.split(r"\s+", stripped[1:], maxsplit=1)
        backend = head.split("@", 1)[0].strip().lower()
        return backend, (rest[0].strip() if rest else "")
    return "", stripped


async def reset_note(
    reset_session: Callable[[str], Awaitable[int]] | None, key: str
) -> str:
    """Forget a conversation's sessions and word the reply.

    ``reset_session`` is None in stateless mode (no session store) — nothing
    to forget, but ``/reset`` still deserves an answer rather than being
    dispatched to a backend named "reset".

    Args:
        reset_session: The store's reset callable, or None when stateless.
        key: The conversation key to forget.

    Returns:
        The user-facing note ("started fresh" / "nothing to reset").
    """
    removed = await reset_session(key) if reset_session is not None else 0
    return "started fresh" if removed else "nothing to reset"


def chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks no longer than ``limit`` characters.

    Prefers to break on a newline rather than mid-line; falls back to a hard cut
    when a single line exceeds the limit. Never returns an empty list.

    Args:
        text: The text to split.
        limit: Maximum characters per chunk.

    Returns:
        A non-empty list of chunks, each at most ``limit`` characters.
    """
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining[:limit].rfind("\n")
        if cut <= 0:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        else:
            chunks.append(remaining[:cut])
            remaining = remaining[cut + 1 :]  # drop the newline we split on
    if remaining:
        chunks.append(remaining)
    return chunks or [""]

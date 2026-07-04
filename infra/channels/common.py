"""Shared helpers for channel adapters: command parsing and message chunking.

These are the only pieces both channels genuinely share. Routing lives in biz;
channels only parse an inbound message into (backend, prompt), invoke the
service, and chunk the reply to the platform's per-message limit.
"""

from __future__ import annotations

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
    "summarise")), and a "@botname" suffix on the command word is stripped. Any
    other message routes to the default backend (an empty backend is returned).

    Args:
        text: The raw inbound message text.

    Returns:
        A (backend, prompt) pair; backend is "" to mean "use the default".
    """
    stripped = text.strip()
    if stripped.startswith("/"):
        head, _, rest = stripped[1:].partition(" ")
        backend = head.split("@", 1)[0].strip().lower()
        return backend, rest.strip()
    return "", stripped


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

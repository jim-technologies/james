"""Unit tests for channel behaviour: command parsing, chunking, allowlist.

Covers the shared helpers, the FakeChannel (DI), and the real TelegramChannel
driven through an httpx MockTransport (no network).
"""

from __future__ import annotations

import json

import httpx
from conftest import FakeChannel
from james.v1 import james_pb2

from infra.channels.common import chunk_text, parse_command
from infra.channels.discord import DiscordChannel
from infra.channels.telegram import TelegramChannel


def test_parse_command():
    assert parse_command("/claude summarise it") == ("claude", "summarise it")
    assert parse_command("/CLAUDE@mybot do x") == ("claude", "do x")
    assert parse_command("just a bare message") == ("", "just a bare message")
    assert parse_command("/codex") == ("codex", "")


def test_parse_command_splits_on_any_whitespace():
    # The command word ends at the FIRST whitespace of any kind: a newline
    # right after the command (common when pasting a multi-line prompt) must
    # not bleed into the backend name.
    assert parse_command("/claude\nsummarise the README") == (
        "claude",
        "summarise the README",
    )
    assert parse_command("/codex\tdo x") == ("codex", "do x")
    assert parse_command("/grok \n what changed?") == ("grok", "what changed?")


def test_chunk_text_respects_limit_and_preserves_content():
    body = "\n".join(f"line {i}" for i in range(500))
    chunks = chunk_text(body, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == body.replace("\n", "")
    assert chunk_text("", 10) == [""]


async def test_fake_channel_allowlist_fail_closed():
    async def invoker(_req):
        raise AssertionError("must not dispatch a non-allowlisted chat")

    channel = FakeChannel(allowed_ids={42}, invoker=invoker)
    allowed = await channel.deliver(99, "/claude hi")
    assert allowed is False
    assert channel.sent == []


async def test_fake_channel_dispatches_allowlisted():
    async def invoker(req):
        return james_pb2.DispatchResponse(
            backend=req.backend or "claude", ok=True, text="done"
        )

    channel = FakeChannel(allowed_ids={42}, invoker=invoker)
    allowed = await channel.deliver(42, "/claude go")
    assert allowed is True
    assert channel.sent[0][1].startswith("▶ running on claude")
    assert channel.sent[-1] == (42, "[claude] done")


def _mock_client(captured, status=200):
    def handler(request):
        captured.append(request)
        return httpx.Response(status, json={"ok": status < 400, "result": {}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _png_response(backend="shot"):
    async def invoker(_req):
        return james_pb2.DispatchResponse(
            backend=backend,
            ok=True,
            text="",
            artifacts=[
                james_pb2.Artifact(
                    content=b"\x89PNG\r\n\x1a\nDATA",
                    mime="image/png",
                    filename="shot.png",
                )
            ],
        )

    return invoker


async def test_telegram_allowlist_fail_closed():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)

    async def invoker(_req):
        raise AssertionError("must not dispatch")

    channel = TelegramChannel(
        token="t",
        allowed_chat_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
    )
    await channel._handle({"chat": {"id": 99}, "text": "/claude hi"})
    await channel.aclose()
    assert captured == []  # no send at all to a non-allowlisted chat


async def test_telegram_dispatch_acks_and_chunks_reply():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)
    long_text = "x" * 9000

    async def invoker(_req):
        return james_pb2.DispatchResponse(
            backend="claude", ok=True, text=long_text
        )

    channel = TelegramChannel(
        token="t",
        allowed_chat_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
    )
    await channel._handle({"chat": {"id": 42}, "text": "/claude do it"})
    await channel.aclose()

    texts = [json.loads(r.content)["text"] for r in captured]
    assert texts[0].startswith("▶ running on claude")  # immediate ack
    assert len(texts) >= 4  # ack + 3+ reply chunks for 9000 chars at 4096
    assert all(len(t) <= 4096 for t in texts)


async def test_telegram_dispatch_exception_still_replies():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)

    async def invoker(_req):
        raise RuntimeError("boom")

    channel = TelegramChannel(
        token="t",
        allowed_chat_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
    )
    await channel._handle({"chat": {"id": 42}, "text": "/claude go"})
    await channel.aclose()
    texts = [json.loads(r.content)["text"] for r in captured]
    assert texts[0].startswith("▶ running on claude")  # acked
    assert any("dispatch failed" in t for t in texts)  # terminal msg, not hung


async def test_telegram_uploads_artifact_bytes_as_photo():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)
    channel = TelegramChannel(
        token="t",
        allowed_chat_ids={42},
        invoker=_png_response(),
        default_backend="claude",
        client=client,
    )
    await channel._handle(
        {"chat": {"id": 42}, "text": "/shot https://example.com"}
    )
    await channel.aclose()
    paths = [r.url.path for r in captured]
    assert any(p.endswith("/sendPhoto") for p in paths)  # image uploaded
    # the PNG bytes are in the multipart body (no host path involved)
    photo = next(r for r in captured if r.url.path.endswith("/sendPhoto"))
    assert b"\x89PNG" in photo.content


async def test_discord_uploads_artifact_bytes():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)
    channel = DiscordChannel(
        token="t",
        allowed_channel_ids={42},
        invoker=_png_response(),
        default_backend="claude",
        client=client,
    )
    await channel._handle(
        {"author": {"id": "u1"}, "channel_id": "42", "content": "/shot x"}
    )
    await channel.aclose()
    uploads = [
        r
        for r in captured
        if "multipart/form-data" in r.headers.get("content-type", "")
    ]
    assert len(uploads) == 1  # the attachment upload
    assert b'name="files[0]"' in uploads[0].content
    assert b"\x89PNG" in uploads[0].content


async def test_discord_allowlist_fail_closed():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)

    async def invoker(_req):
        raise AssertionError("must not dispatch")

    channel = DiscordChannel(
        token="t",
        allowed_channel_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
    )
    await channel._handle(
        {"author": {"id": "u1"}, "channel_id": "99", "content": "/claude hi"}
    )
    await channel.aclose()
    assert captured == []  # no send at all to a non-allowlisted channel


async def test_discord_ignores_bot_authors():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)

    async def invoker(_req):
        raise AssertionError("must not dispatch")

    channel = DiscordChannel(
        token="t",
        allowed_channel_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
    )
    await channel._handle(
        {
            "author": {"id": "u1", "bot": True},
            "channel_id": "42",
            "content": "/claude hi",
        }
    )
    await channel.aclose()
    assert captured == []  # bot messages dropped before any send


async def test_discord_reset_forgets_channel_session():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)
    seen = {}

    async def reset(key):
        seen["key"] = key
        return 1

    async def invoker(_req):
        raise AssertionError("reset must not dispatch")

    channel = DiscordChannel(
        token="t",
        allowed_channel_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
        reset_session=reset,
    )
    await channel._handle(
        {"author": {"id": "u1"}, "channel_id": "42", "content": "/reset"}
    )
    await channel.aclose()
    assert seen["key"] == "42"  # the channel's conversation key was reset
    assert any(b"started fresh" in r.content for r in captured)


async def test_discord_malformed_channel_id_is_ignored():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)

    async def invoker(_req):
        raise AssertionError("must not dispatch")

    channel = DiscordChannel(
        token="t",
        allowed_channel_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
    )
    await channel._handle(
        {"author": {"id": "u1"}, "channel_id": "nope", "content": "/claude hi"}
    )
    await channel.aclose()
    assert captured == []  # unparseable id -> fail-closed, no send


async def test_telegram_topic_scopes_conversation_and_replies_in_topic():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)
    seen = {}

    async def invoker(req):
        seen["convo"] = req.conversation_id
        return james_pb2.DispatchResponse(backend="claude", ok=True, text="ok")

    channel = TelegramChannel(
        token="t",
        allowed_chat_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
    )
    await channel._handle(
        {"chat": {"id": 42}, "message_thread_id": 7, "text": "/claude hi"}
    )
    await channel.aclose()
    assert seen["convo"] == "42:7"  # topic-scoped session key
    # every reply carries the topic id so it lands in the same thread
    bodies = [json.loads(r.content) for r in captured]
    assert bodies and all(b.get("message_thread_id") == 7 for b in bodies)


async def test_telegram_reset_command_forgets_thread():
    captured: list[httpx.Request] = []
    client = _mock_client(captured)
    resets = []

    async def reset(key):
        resets.append(key)
        return 1

    async def invoker(_req):
        raise AssertionError("/reset must not dispatch")

    channel = TelegramChannel(
        token="t",
        allowed_chat_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
        reset_session=reset,
    )
    await channel._handle(
        {"chat": {"id": 42}, "message_thread_id": 7, "text": "/reset"}
    )
    await channel.aclose()
    assert resets == ["42:7"]
    texts = [json.loads(r.content)["text"] for r in captured]
    assert any("fresh" in t.lower() for t in texts)


async def test_telegram_reset_without_store_still_replies():
    # Stateless mode (no session store wired): /reset must still answer, not
    # fall through to dispatch as an unknown backend named "reset".
    captured: list[httpx.Request] = []
    client = _mock_client(captured)

    async def invoker(_req):
        raise AssertionError("/reset must not dispatch")

    channel = TelegramChannel(
        token="t",
        allowed_chat_ids={42},
        invoker=invoker,
        default_backend="claude",
        client=client,
    )
    await channel._handle({"chat": {"id": 42}, "text": "/reset"})
    await channel.aclose()
    texts = [json.loads(r.content)["text"] for r in captured]
    assert any("nothing to reset" in t for t in texts)


async def test_telegram_upload_failure_notifies_user():
    captured: list[httpx.Request] = []
    client = _mock_client(captured, status=413)  # e.g. photo too large
    channel = TelegramChannel(
        token="t",
        allowed_chat_ids={42},
        invoker=_png_response(),
        default_backend="claude",
        client=client,
    )
    await channel._handle({"chat": {"id": 42}, "text": "/shot x"})
    await channel.aclose()
    # after the ack + failed upload, the user still gets a terminal message
    msgs = [
        json.loads(r.content)["text"]
        for r in captured
        if r.url.path.endswith("/sendMessage")
    ]
    assert any("couldn't deliver" in m for m in msgs)

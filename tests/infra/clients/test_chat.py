"""Unit tests for the chat api client — error paths, no network.

These exercise the never-raises contract: a missing key and a misconfigured
backend (bad service name) must be returned as (False, ...), not raised. Neither
path makes a network call.
"""

from __future__ import annotations

import os

from infra.clients.chat import ChatApiCaller

_DESCRIPTOR = "gen/descriptor.binpb"


async def test_missing_key_returns_error():
    caller = ChatApiCaller(_DESCRIPTOR)
    ok, _text, err = await caller.call(
        "hi",
        base_url="https://example.com",
        model="m",
        service_name="james.chat.v1.ChatService",
        tool_name="ChatService.CreateChatCompletion",
        api_key_env="DEFINITELY_UNSET_KEY_XYZ",
        timeout_s=5,
    )
    assert not ok
    assert "missing API key" in err


async def test_bad_service_name_is_returned_not_raised():
    os.environ["TMP_CHAT_KEY"] = "x"
    try:
        caller = ChatApiCaller(_DESCRIPTOR)
        ok, _text, err = await caller.call(
            "hi",
            base_url="https://example.com",
            model="m",
            service_name="does.not.Exist",
            tool_name="Nope.Nope",
            api_key_env="TMP_CHAT_KEY",
            timeout_s=5,
        )
        assert not ok
        assert "api call failed" in err
    finally:
        del os.environ["TMP_CHAT_KEY"]

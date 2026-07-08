"""HTTP client for api backends — an OpenAI-compatible chat completion call.

A thin adapter over invariant-protocol's ``connect_http``: it builds a Server
from the committed descriptor, scopes it to the chat service, injects the API
key as a per-connection bearer header (``auth=``), and invokes the completion
tool. The provider's REST mapping (method, path, body) comes from the
google.api.http annotation in the proto, not from code here. Returns
(ok, text, error); a missing key or any transport error is returned rather
than raised.
"""

from __future__ import annotations

import os

from invariant import ChannelOptions, Server

# Importing the generated stub registers the chat messages in the default
# descriptor pool, which invariant-protocol's HTTP client resolves against.
from james.chat.v1 import chat_pb2


class ChatApiCaller:
    """Calls an OpenAI-compatible chat endpoint (the ApiCaller port)."""

    def __init__(self, descriptor_path: str) -> None:
        self._descriptor_path = descriptor_path

    async def call(
        self,
        prompt: str,
        *,
        base_url: str,
        model: str,
        service_name: str,
        tool_name: str,
        api_key_env: str,
        timeout_s: float,
    ) -> tuple[bool, str, str]:
        """Send a single-message completion and return (ok, text, error)."""
        api_key = os.environ.get(api_key_env) if api_key_env else None
        if not api_key:
            name = api_key_env or "the configured env var"
            return (False, "", f"missing API key: set {name} for this backend")

        # Setup can raise too (a bad base_url or service_name), so it lives in
        # the try — the docstring promises errors are returned, not raised.
        server = None
        try:
            server = Server.from_descriptor(self._descriptor_path)
            # Per-connection auth + timeouts (the server-global header hook was
            # removed in invariant-protocol 0.3): the bearer header rides only
            # this connection, and the generous read timeout covers a slow
            # completion.
            server.connect_http(
                base_url,
                service_name,
                auth=lambda _req: {"Authorization": f"Bearer {api_key}"},
                options=ChannelOptions(read_timeout=timeout_s),
            )
            request = chat_pb2.CreateChatCompletionRequest(
                model=model,
                messages=[chat_pb2.ChatMessage(role="user", content=prompt)],
            )
            response = await server.invoke(tool_name, request)
        except Exception as exc:
            return (False, "", f"api call failed: {exc}")
        finally:
            if server is not None:
                await server.stop()

        if not response.choices:
            return (False, "", "empty response from provider")
        return (True, response.choices[0].message.content, "")

"""Live tests — real network / real CLIs. Gated by RUN_LIVE_TESTS=1.

Each is additionally skipped when its credential or CLI is unavailable, so the
suite degrades gracefully on a partially configured host.
"""

from __future__ import annotations

import os
import shutil

import httpx
import pytest
from conftest import requires_env

from biz.backends import REGISTRY
from biz.dispatch import dispatch
from infra.backends.cli import SubprocessCliRunner
from infra.clients.chat import ChatApiCaller

pytestmark = pytest.mark.live

_DESCRIPTOR = "gen/descriptor.binpb"


@requires_env("OPENAI_API_KEY")
async def test_live_api_backend_completes():
    backend = REGISTRY["gpt"]
    caller = ChatApiCaller(_DESCRIPTOR)
    ok, text, err = await caller.call(
        "Reply with the single word: PONG.",
        base_url=backend.base_url,
        model=backend.model,
        service_name=backend.service_name,
        tool_name=backend.tool_name,
        api_key_env="OPENAI_API_KEY",
        timeout_s=60,
    )
    assert ok, err
    assert text.strip()


async def test_live_claude_cli_runs():
    if not shutil.which("claude"):
        pytest.skip("claude CLI not installed/logged in")
    result = await dispatch(
        "claude",
        "Reply with exactly: OK",
        cwd=".",
        default_backend="claude",
        cli_runner=SubprocessCliRunner(),
        api_caller=ChatApiCaller(_DESCRIPTOR),
    )
    assert result.ok, result.error


async def _session_roundtrip(name: str, tmp_path) -> None:
    """Create a session with a codeword, resume it, and verify recall."""
    from infra.sessions.store import JsonSessionStore

    store = JsonSessionStore(str(tmp_path / "sessions.json"))
    runner = SubprocessCliRunner()
    api = ChatApiCaller(_DESCRIPTOR)
    key = "live-roundtrip"
    first = await dispatch(
        name,
        "Remember this codeword: PINEAPPLE. Reply with just: OK",
        cwd=".",
        default_backend=name,
        cli_runner=runner,
        api_caller=api,
        session_key=key,
        session_store=store,
    )
    assert first.ok, first.error
    second = await dispatch(
        name,
        "What was the codeword I gave you? Reply with only that word.",
        cwd=".",
        default_backend=name,
        cli_runner=runner,
        api_caller=api,
        session_key=key,
        session_store=store,
    )
    assert second.ok, second.error
    assert "pineapple" in second.text.lower(), second.text  # resumed memory


async def test_live_codex_session_roundtrip(tmp_path):
    if not shutil.which("codex"):
        pytest.skip("codex CLI not installed/logged in")
    await _session_roundtrip("codex", tmp_path)


async def test_live_grok_session_roundtrip(tmp_path):
    if not shutil.which("grok"):
        pytest.skip("grok CLI not installed/logged in")
    await _session_roundtrip("grok", tmp_path)


async def test_live_opencode_session_roundtrip(tmp_path):
    # Needs a configured opencode z.ai login; validates capture + `-s` resume.
    if not shutil.which("opencode"):
        pytest.skip("opencode CLI not installed / z.ai not configured")
    await _session_roundtrip("opencode", tmp_path)


async def test_live_claude_session_roundtrip(tmp_path):
    # The caller_set model: james mints the id (--session-id) and resumes it.
    if not shutil.which("claude"):
        pytest.skip("claude CLI not installed/logged in")
    await _session_roundtrip("claude", tmp_path)


@requires_env("OPENCLAW_A2A_TOKEN")
async def test_live_openclaw_a2a():
    # Talk to a running openclaw-a2a-gateway over A2A (gRPC-preferred, HTTP
    # fallback). Skipped unless the token is set; also needs the gateway running.
    from infra.clients.a2a import A2ASdkCaller

    ok, text, err, _arts = await A2ASdkCaller().call(
        "Reply with the single word: PONG.",
        base_url="http://127.0.0.1:18800",
        agent_card_path="/.well-known/agent-card.json",
        token_env="OPENCLAW_A2A_TOKEN",
        timeout_s=60,
    )
    assert ok, err
    assert text.strip()


@requires_env("TELEGRAM_BOT_TOKEN")
async def test_live_telegram_token_valid():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        assert resp.json().get("ok") is True


@requires_env("DISCORD_BOT_TOKEN")
async def test_live_discord_token_valid():
    token = os.environ["DISCORD_BOT_TOKEN"]
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        assert resp.status_code == 200

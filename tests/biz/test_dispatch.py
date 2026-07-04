"""Unit tests for the dispatch core: routing, defaults, normalisation."""

from __future__ import annotations

from conftest import (
    FakeA2ACaller,
    FakeApiCaller,
    FakeCliRunner,
    FakeSessionStore,
)

from biz.backends import REGISTRY
from biz.dispatch import dispatch


async def test_cli_backend_routes_and_returns_text():
    runner = FakeCliRunner(returncode=0, stdout="  hello  ")
    result = await dispatch(
        "claude",
        "hi",
        cwd="/work",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
    )
    assert result.ok and result.text == "hello"
    assert result.backend == "claude"
    assert runner.calls[0]["argv"] == ["claude", "-p"]
    assert runner.calls[0]["prompt"] == "hi"
    assert runner.calls[0]["cwd"] == "/work"
    # james's own secrets are stripped from the agent's env (defense-in-depth):
    unset = runner.calls[0]["env_unset"]
    assert "ANTHROPIC_API_KEY" in unset  # subscription fallback
    assert "TELEGRAM_BOT_TOKEN" in unset and "OPENAI_API_KEY" in unset


async def test_empty_backend_uses_default():
    runner = FakeCliRunner(stdout="x")
    result = await dispatch(
        "",
        "hi",
        cwd=".",
        default_backend="codex",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
    )
    assert result.backend == "codex"
    assert runner.calls[0]["argv"] == ["codex", "exec"]


async def test_unknown_backend_returns_help():
    result = await dispatch(
        "nope",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(),
        api_caller=FakeApiCaller(),
    )
    assert not result.ok
    assert "Unknown backend 'nope'" in result.error
    assert "claude" in result.error  # lists the available backends


async def test_cli_failure_surfaces_stderr():
    runner = FakeCliRunner(returncode=1, stdout="", stderr="boom")
    result = await dispatch(
        "claude",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
    )
    assert not result.ok and result.error == "boom"


async def test_api_backend_routes():
    api = FakeApiCaller(ok=True, text="answer")
    result = await dispatch(
        "gpt",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(),
        api_caller=api,
    )
    assert result.ok and result.text == "answer"
    assert api.calls[0]["model"] == REGISTRY["gpt"].model
    assert api.calls[0]["base_url"] == REGISTRY["gpt"].base_url


async def test_api_failure_surfaces_error():
    api = FakeApiCaller(ok=False, text="", error="no key")
    result = await dispatch(
        "gpt",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(),
        api_caller=api,
    )
    assert not result.ok and result.error == "no key"


async def test_empty_prompt_rejected():
    result = await dispatch(
        "claude",
        "   ",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(),
        api_caller=FakeApiCaller(),
    )
    assert not result.ok and "Empty" in result.error


async def test_adding_a_backend_is_one_registry_row():
    # The whole routing contract is data: every registered backend dispatches
    # with no change to dispatch() — proving "adding a backend = one row".
    # artifact_bytes satisfies media backends (shot); text-only ones ignore it.
    for name, backend in REGISTRY.items():
        result = await dispatch(
            name,
            "hi",
            cwd=".",
            default_backend="claude",
            cli_runner=FakeCliRunner(stdout="out", artifact_bytes=b"PNG"),
            api_caller=FakeApiCaller(ok=True, text="out"),
            a2a_caller=FakeA2ACaller(ok=True, text="out"),
        )
        assert result.ok, (name, backend.kind, result.error)


def test_harness_secrets_stripped_from_every_cli_agent():
    # No agent backend should inherit james's own bot tokens / provider keys.
    harness = ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "OPENAI_API_KEY")
    for name, backend in REGISTRY.items():
        if backend.kind != "cli":
            continue
        for secret in harness:
            assert secret in backend.env_unset, (name, secret)


async def test_shot_backend_returns_artifact():
    runner = FakeCliRunner(returncode=0, stdout="", artifact_bytes=b"PNGDATA")
    result = await dispatch(
        "shot",
        "https://example.com",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
    )
    assert result.ok
    assert len(result.artifacts) == 1
    assert result.artifacts[0].content == b"PNGDATA"
    assert result.artifacts[0].mime == "image/png"
    assert result.artifacts[0].filename == "shot.png"
    # the runner was asked to produce a .png artifact
    assert runner.calls[0]["wants_artifact"] is True
    assert runner.calls[0]["artifact_suffix"] == ".png"


async def test_shot_backend_no_file_is_error():
    runner = FakeCliRunner(returncode=0, stdout="", artifact_bytes=b"")
    result = await dispatch(
        "shot",
        "not-a-url",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
    )
    assert not result.ok
    assert "no file" in result.error


async def test_shot_profile_resolves_user_data_dir():
    runner = FakeCliRunner(returncode=0, stdout="", artifact_bytes=b"PNG")
    result = await dispatch(
        "shot:work",
        "https://x",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        profiles_dir="/profiles",
        default_profile="default",
    )
    assert result.ok
    assert runner.calls[0]["user_data_dir"] == "/profiles/work"


async def test_shot_uses_default_profile_when_unnamed():
    runner = FakeCliRunner(returncode=0, stdout="", artifact_bytes=b"PNG")
    await dispatch(
        "shot",
        "https://x",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        profiles_dir="/profiles",
        default_profile="main",
    )
    assert runner.calls[0]["user_data_dir"] == "/profiles/main"


async def test_invalid_profile_name_is_rejected():
    result = await dispatch(
        "shot:../etc",
        "https://x",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(artifact_bytes=b"PNG"),
        api_caller=FakeApiCaller(),
        profiles_dir="/profiles",
    )
    assert not result.ok
    assert "Invalid profile" in result.error  # path traversal blocked


async def test_non_profile_backend_ignores_variant():
    runner = FakeCliRunner(stdout="hi")
    result = await dispatch(
        "claude:anything",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        profiles_dir="/profiles",
    )
    assert result.ok
    assert runner.calls[0]["user_data_dir"] == ""  # claude has no profile


async def test_claude_mcp_config_injected_when_configured():
    runner = FakeCliRunner(stdout="ok")
    await dispatch(
        "claude",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        mcp_config="/etc/james/servers.mcp.json",
    )
    assert runner.calls[0]["argv"] == [
        "claude",
        "--mcp-config",
        "/etc/james/servers.mcp.json",
        "-p",
    ]


async def test_mcp_config_not_injected_without_path():
    runner = FakeCliRunner(stdout="ok")
    await dispatch(
        "claude",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
    )
    assert runner.calls[0]["argv"] == ["claude", "-p"]


async def test_mcp_config_ignored_for_non_accepting_backend():
    runner = FakeCliRunner(stdout="ok")
    await dispatch(
        "codex",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        mcp_config="/etc/james/servers.mcp.json",
    )
    assert runner.calls[0]["argv"] == [
        "codex",
        "exec",
    ]  # codex uses its own MCP


async def test_session_create_then_resume():
    store = FakeSessionStore()
    runner1 = FakeCliRunner(stdout="a")
    await dispatch(
        "claude",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner1,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert runner1.calls[0]["argv"][:3] == [
        "claude",
        "--session-id",
        "sid-claude-42:7",
    ]

    runner2 = FakeCliRunner(stdout="b")
    await dispatch(
        "claude",
        "again",
        cwd=".",
        default_backend="claude",
        cli_runner=runner2,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert runner2.calls[0]["argv"][:3] == [
        "claude",
        "--resume",
        "sid-claude-42:7",
    ]


async def test_failed_first_run_still_resumes_next_time():
    # The id is recorded at resolve time, so even a failed first run resumes on
    # the next message (the agent registered the id regardless of exit code) —
    # no permanent wedge re-creating a duplicate "--session-id".
    store = FakeSessionStore()
    await dispatch(
        "claude",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(returncode=1, stderr="boom"),
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    runner2 = FakeCliRunner(stdout="b")
    await dispatch(
        "claude",
        "again",
        cwd=".",
        default_backend="claude",
        cli_runner=runner2,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert runner2.calls[0]["argv"][:3] == [
        "claude",
        "--resume",
        "sid-claude-42:7",
    ]


class _SequenceCliRunner:
    """A CliRunner returning queued results in order, recording argv.

    Each result is (rc, out, err) or (rc, out, err, captured_id).
    """

    def __init__(self, results):
        self.calls: list[dict] = []
        self._results = [tuple(r) for r in results]

    async def run(self, argv, prompt, **kwargs):
        self.calls.append({"argv": list(argv)})
        r = self._results.pop(0)
        captured = r[3] if len(r) > 3 else ""
        return (r[0], r[1], r[2], b"", captured)


async def test_dead_resume_recovers_with_fresh_session():
    # A thread whose claude session has vanished (pruned, or the store outlived
    # ~/.claude across a redeploy): the resume fails with the dead-session
    # signal, so dispatch forgets the dead id and retries once as a fresh
    # --session-id, healing the thread instead of wedging on a dead resume.
    store = FakeSessionStore()
    await store.resolve(
        "claude", "42:7"
    )  # pre-existing -> next resolve resumes
    runner = _SequenceCliRunner(
        [
            (1, "", "Error: No conversation found with session ID: x"),
            (0, "recovered", ""),
        ]
    )
    result = await dispatch(
        "claude",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert result.ok and result.text == "recovered"
    assert runner.calls[0]["argv"][:3] == [
        "claude",
        "--resume",
        "sid-claude-42:7",
    ]
    assert runner.calls[1]["argv"][:3] == [
        "claude",
        "--session-id",
        "sid-claude-42:7-2",
    ]
    assert ("forget", "claude", "42:7") in store.calls


async def test_transient_resume_failure_keeps_session():
    # A non-dead-session failure (rate limit, timeout) must NOT forget/retry —
    # that would silently drop the thread's memory.
    store = FakeSessionStore()
    await store.resolve("claude", "42:7")  # pre-existing -> resumes
    runner = _SequenceCliRunner([(1, "", "rate limited, try again")])
    result = await dispatch(
        "claude",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert not result.ok
    assert len(runner.calls) == 1  # no retry
    assert ("forget", "claude", "42:7") not in store.calls


async def test_no_session_without_key_or_store():
    runner = FakeCliRunner(stdout="a")
    await dispatch(
        "claude",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
    )
    assert runner.calls[0]["argv"] == ["claude", "-p"]


async def test_session_ignored_for_stateless_backend():
    # shot has no session_model, so session_key/store are ignored entirely.
    store = FakeSessionStore()
    runner = FakeCliRunner(stdout="", artifact_bytes=b"PNG")
    result = await dispatch(
        "shot",
        "https://x",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert result.ok
    assert store.calls == []  # no session handling for a stateless backend


# --- a2a backends (remote A2A peers, e.g. openclaw) ---


async def test_a2a_backend_routes_and_maps_artifacts():
    a2a = FakeA2ACaller(
        ok=True,
        text="hi from peer",
        artifacts=((b"PNG", "image/png", "x.png"),),
    )
    result = await dispatch(
        "openclaw",
        "hello",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(),
        api_caller=FakeApiCaller(),
        a2a_caller=a2a,
    )
    assert result.ok and result.text == "hi from peer"
    assert len(result.artifacts) == 1
    assert result.artifacts[0].content == b"PNG"
    assert result.artifacts[0].mime == "image/png"
    assert result.artifacts[0].filename == "x.png"
    # the peer's token env name (not value) is passed through from the row
    assert a2a.calls[0]["token_env"] == "OPENCLAW_A2A_TOKEN"


async def test_a2a_backend_error_surfaces():
    a2a = FakeA2ACaller(ok=False, error="peer unreachable")
    result = await dispatch(
        "openclaw",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(),
        api_caller=FakeApiCaller(),
        a2a_caller=a2a,
    )
    assert not result.ok and result.error == "peer unreachable"


async def test_a2a_backend_without_caller_is_clean_error():
    # An a2a backend dispatched without the a2a port returns an error, not crash.
    result = await dispatch(
        "openclaw",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(),
        api_caller=FakeApiCaller(),
    )
    assert not result.ok and "not configured" in result.error


# --- capture-model sessions (codex / grok / opencode) ---


async def test_capture_create_records_id_and_uses_create_argv():
    # First message: the CLI mints its own id (the runner returns it as the 5th
    # value); dispatch records it and uses create_argv (no flag injection).
    store = FakeSessionStore()
    runner = FakeCliRunner(stdout="hello", captured_id="thread-xyz")
    result = await dispatch(
        "codex",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert result.ok and result.text == "hello"
    assert runner.calls[0]["argv"] == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-",
    ]
    assert ("record", "codex", "42:7", "thread-xyz") in store.calls


async def test_capture_second_message_resumes_via_resume_argv():
    store = FakeSessionStore()
    await dispatch(
        "codex",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(stdout="a", captured_id="thread-xyz"),
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    runner2 = FakeCliRunner(stdout="b", captured_id="thread-xyz")
    await dispatch(
        "codex",
        "again",
        cwd=".",
        default_backend="claude",
        cli_runner=runner2,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    # resume_argv with {sid} substituted; "resume" is a subcommand, id positional
    assert runner2.calls[0]["argv"] == [
        "codex",
        "exec",
        "resume",
        "thread-xyz",
        "--json",
        "--skip-git-repo-check",
        "-",
    ]


async def test_capture_failed_create_still_records_id():
    # The CLI prints its id before doing the work, so a create that then fails
    # still yields a resumable id — record it (mirrors the caller_set invariant).
    store = FakeSessionStore()
    await dispatch(
        "codex",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(
            returncode=1, stderr="boom", captured_id="thread-xyz"
        ),
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert ("record", "codex", "42:7", "thread-xyz") in store.calls
    runner2 = FakeCliRunner(stdout="b", captured_id="thread-xyz")
    await dispatch(
        "codex",
        "again",
        cwd=".",
        default_backend="claude",
        cli_runner=runner2,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert runner2.calls[0]["argv"][:4] == [
        "codex",
        "exec",
        "resume",
        "thread-xyz",
    ]


async def test_capture_create_without_id_records_nothing():
    # A failed grok create emits an error object with no id -> captured "" -> the
    # store is left untouched (no spurious "null" entry).
    store = FakeSessionStore()
    await dispatch(
        "grok",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=FakeCliRunner(returncode=1, stderr="bad", captured_id=""),
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert not any(c[0] == "record" for c in store.calls)


async def test_capture_grok_and_opencode_create_argv():
    store = FakeSessionStore()
    grok = FakeCliRunner(stdout="a", captured_id="grok-sid")
    await dispatch(
        "grok",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=grok,
        api_caller=FakeApiCaller(),
        session_key="9",
        session_store=store,
    )
    # {prompt} stays literal here — the real runner substitutes it, not dispatch.
    assert grok.calls[0]["argv"] == [
        "grok",
        "--single={prompt}",
        "--output-format",
        "json",
    ]
    opencode = FakeCliRunner(stdout="a", captured_id="ses_abc")
    await dispatch(
        "opencode",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=opencode,
        api_caller=FakeApiCaller(),
        session_key="9",
        session_store=store,
    )
    assert opencode.calls[0]["argv"] == [
        "opencode",
        "run",
        "--format",
        "json",
        "-m",
        "zai-coding-plan/glm-4.7",
        "{prompt}",
    ]
    assert ("record", "opencode", "9", "ses_abc") in store.calls


async def test_capture_dead_resume_recovers_fresh():
    # A recorded id whose CLI session has vanished: the resume hits the
    # dead-session signal, so dispatch forgets it and retries once as a fresh
    # create, recording the newly minted id.
    store = FakeSessionStore()
    await store.record("codex", "42:7", "old-thread")  # pre-existing -> resumes
    runner = _SequenceCliRunner(
        [
            (1, "", "Error: no rollout found for session old-thread"),
            (0, "recovered", "", "new-thread"),
        ]
    )
    result = await dispatch(
        "codex",
        "hi",
        cwd=".",
        default_backend="claude",
        cli_runner=runner,
        api_caller=FakeApiCaller(),
        session_key="42:7",
        session_store=store,
    )
    assert result.ok and result.text == "recovered"
    assert runner.calls[0]["argv"][:4] == [
        "codex",
        "exec",
        "resume",
        "old-thread",
    ]
    assert runner.calls[1]["argv"] == [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-",
    ]
    assert ("forget", "codex", "42:7") in store.calls
    assert ("record", "codex", "42:7", "new-thread") in store.calls

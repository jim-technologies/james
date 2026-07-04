"""Backend domain model and registry.

A backend is the unit of routing: a typed row describing how to reach one AI
agent. Three kinds exist. A ``cli`` backend shells out to a vendor's own
command-line tool, using that tool's own login (so Claude runs through the
official ``claude`` CLI, never a third-party OAuth path). An ``api`` backend
calls an OpenAI-compatible HTTP endpoint via invariant-protocol with a key read
from the environment. An ``a2a`` backend talks to a remote agent peer over the
A2A protocol via the official a2a-sdk (gRPC preferred, HTTP fallback).

REGISTRY is the one place provider names live. Adding a backend is a single row;
dispatch code does not change. Secrets are referenced by environment-variable
*name* only and resolved lazily at dispatch, so a missing key disables just that
backend rather than crashing startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class Backend:
    """One AI agent backend: how to reach it and how to feed it a prompt."""

    name: str
    kind: Literal["cli", "api", "a2a"]

    # --- cli backends: shell out to a vendor CLI using its own login ---
    # The command to run. If any element contains the literal "{prompt}", the
    # prompt is substituted there (argv mode); otherwise the prompt is written
    # to the child's stdin.
    argv: tuple[str, ...] = ()
    # Extra environment variables to set on the child process.
    env_set: dict[str, str] = field(default_factory=dict)
    # Environment variables to remove from the child (e.g. drop a metered API
    # key so the CLI uses its subscription login instead).
    env_unset: tuple[str, ...] = ()
    # If set, an "--mcp-config <path>" flag is injected when an MCP config is
    # configured — for CLIs that take MCP servers as a flag (claude). codex/grok
    # read their own MCP config files instead, so they leave this False.
    accepts_mcp_config: bool = False
    # --- resumable per-conversation sessions ---
    # session_model selects how the session id is obtained:
    #   "caller_set" — james mints a uuid and injects it on create via
    #                  session_flag; later runs resume via resume_flag (claude).
    #   "capture"    — the CLI mints its OWN id and prints it; james parses it
    #                  from the first run's output (capture_* below), persists
    #                  it, and resumes via resume_argv (codex/grok/opencode);
    #                  caller-minted ids are ignored by these CLIs.
    #   ""           — stateless (no memory).
    session_model: Literal["", "caller_set", "capture"] = ""
    # caller_set: the create / resume flags (the id is injected as the value).
    session_flag: str = ""
    resume_flag: str = ""
    # capture: full argv templates ("{sid}" -> captured id on resume; "{prompt}"
    # handled by the runner). create_argv has no id yet; resume_argv resumes a
    # known id — this expresses codex's resume being a SUBCOMMAND + POSITIONAL,
    # which the flag+value model can't produce.
    create_argv: tuple[str, ...] = ()
    resume_argv: tuple[str, ...] = ()
    # capture: how to find the CLI-minted id in the create run's output. Applied
    # by the CliRunner (it holds the raw streams); passed as primitives so biz
    # never parses vendor output. capture_format: "jsonl" (parse each line) or
    # "json" (parse the WHOLE stream as one object — grok pretty-prints across
    # lines, so never line-split). capture_event: for jsonl, only consider
    # objects whose top-level "type" equals it ("" = first object with the
    # field).
    capture_stream: str = "stdout"
    capture_format: str = ""
    capture_field: str = ""
    capture_event: str = ""
    # capture: how to pull the human reply out of the same machine-readable
    # stream (raw stdout is JSON, not the answer). reply_event: top-level "type"
    # to match (jsonl). reply_match: an extra "dotted.path=value" filter (codex:
    # only item.type==agent_message, never reasoning/tool items). reply_field:
    # dotted path to the text. Parts are concatenated.
    reply_format: str = ""
    reply_field: str = ""
    reply_event: str = ""
    reply_match: str = ""
    # Lowercase substring the CLI prints to stderr when a resume targets a
    # session it no longer has (pruned, or the store outlived the CLI's own
    # state across a redeploy / HOME change). On a resume hitting this, dispatch
    # forgets the dead id and retries once as a fresh session, so the thread
    # self-heals instead of wedging on a permanent dead resume. Empty = no
    # detection (a dead resume just surfaces as an error).
    session_dead_signal: str = ""

    # --- api backends: call an OpenAI-compatible chat endpoint ---
    base_url: str = ""
    model: str = ""
    # Full proto service name + tool name the invariant-protocol client drives.
    service_name: str = ""
    tool_name: str = ""

    # --- a2a backends: talk to a remote agent over the A2A protocol via the
    # official a2a-sdk (transport negotiated from the peer's Agent Card — gRPC
    # preferred, JSON-RPC/HTTP+JSON fallback; A2A v1.0 with v0.3 compat).
    # base_url is the peer's origin; secret_env's "token" names the env var
    # holding the peer's bearer token (NOT a harness secret). agent_card_path is
    # where the client fetches the card (the negotiated endpoint comes from the
    # card itself); the SDK resolves any non-terminal Task by polling.
    agent_card_path: str = "/.well-known/agent-card.json"

    # --- shared ---
    # Secrets required by this backend, as {child_env_var: SOURCE_ENV_NAME}.
    # Resolved lazily at dispatch; a missing source disables only this backend.
    # For api backends the "api_key" entry's source env holds the API key.
    secret_env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 120.0

    # --- media: cli backends that produce a file (e.g. a screenshot) ---
    # A non-empty artifact_mime marks this cli backend as producing a file: the
    # runner allocates a temp path, substitutes it for "{outfile}" in argv, and
    # returns it as an artifact the channel uploads. artifact_suffix is the temp
    # file's extension (some tools, e.g. chromium, pick the format from it).
    artifact_mime: str = ""
    artifact_suffix: str = ""

    # --- browser profiles: cli backends that run against a persistent,
    # logged-in Chrome profile. The chosen profile dir is substituted for
    # "{profile_dir}" in argv; select per message via "/<backend>:<profile>".
    # Same-profile runs serialize (Chrome locks a user-data-dir to one process).
    uses_profile: bool = False


# james's OWN operational secrets, stripped from every cli agent's environment:
# no agent backend needs them, so a prompt-injected agent can't exfiltrate the
# bot tokens (= bot takeover) or the metered provider keys. Stripping the keys
# also reinforces the subscriptions stance (claude/codex fall back to their
# own login, not a billed key). Secrets the agent is *meant* to use (e.g.
# DATABASE_URL) are NOT listed here, so they still flow through — see the README
# "Secrets the agents can use" section.
_HARNESS_SECRETS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
)

# The single source of truth for provider names. One row per backend.
#
# CLI flags below were verified against each tool's --help: `claude -p` and
# `codex exec` read the prompt from stdin; `grok --single=` takes it as an
# attached value (safe for prompts starting with "-"), hence the placeholder.
# Agentic CLI backends get a generous timeout — real work (reading the repo,
# calling MCP tools) easily exceeds the 120s default.
REGISTRY: dict[str, Backend] = {
    # Anthropic Claude via the official CLI (Max subscription login; the metered
    # key is stripped via _HARNESS_SECRETS).
    "claude": Backend(
        name="claude",
        kind="cli",
        argv=("claude", "-p"),
        env_unset=_HARNESS_SECRETS,
        accepts_mcp_config=True,
        session_model="caller_set",
        session_flag="--session-id",
        resume_flag="--resume",
        # claude prints "No conversation found with session ID: <id>" on a
        # resume of a session it no longer has (verified against the CLI).
        session_dead_signal="no conversation found",
        timeout_s=600.0,
    ),
    # OpenAI Codex via the `codex` CLI (ChatGPT login). Capture model: codex
    # mints its own thread id and prints it as the first JSONL event; resume is
    # the `exec resume <id>` subcommand. The prompt is piped on stdin via the
    # trailing "-" positional (the "--" guard panics in 0.142.0).
    # --skip-git-repo-check is needed when the working dir isn't a trusted git
    # repo. (Verified against codex-cli 0.142.0.)
    "codex": Backend(
        name="codex",
        kind="cli",
        argv=("codex", "exec"),  # non-session one-shot (plain output)
        env_unset=_HARNESS_SECRETS,
        session_model="capture",
        create_argv=("codex", "exec", "--json", "--skip-git-repo-check", "-"),
        resume_argv=(
            "codex",
            "exec",
            "resume",
            "{sid}",
            "--json",
            "--skip-git-repo-check",
            "-",
        ),
        capture_format="jsonl",
        capture_event="thread.started",
        capture_field="thread_id",
        # The assistant message is item.completed with item.type==agent_message
        # (reasoning/tool items share item.completed but a different item.type).
        reply_format="jsonl",
        reply_event="item.completed",
        reply_match="item.type=agent_message",
        reply_field="item.text",
        session_dead_signal="no rollout found",
        timeout_s=600.0,
    ),
    # xAI Grok via the `grok` CLI (SuperGrok login). Capture model: -s is
    # silently ignored, grok mints a UUIDv7 printed as `sessionId`. Resume by
    # the exact captured id ONLY (never -c / bare -r: those are cwd-scoped and
    # cross threads). --output-format json prints ONE pretty-printed object
    # across multiple lines, so the runner parses the whole stdout blob.
    # (Verified against grok 0.2.59.)
    "grok": Backend(
        name="grok",
        kind="cli",
        argv=("grok", "--single={prompt}"),  # non-session one-shot
        env_unset=_HARNESS_SECRETS,
        session_model="capture",
        create_argv=("grok", "--single={prompt}", "--output-format", "json"),
        resume_argv=(
            "grok",
            "--single={prompt}",
            "--resume",
            "{sid}",
            "--output-format",
            "json",
        ),
        capture_format="json",
        capture_field="sessionId",
        reply_format="json",
        reply_field="text",
        session_dead_signal="session does not exist",
        timeout_s=600.0,
    ),
    # opencode against z.ai (zai-coding-plan/glm-4.7). Capture model:
    # caller-minted ids are rejected; opencode mints a `ses_…` id printed on
    # every JSONL line. Resume with -s <id>. Reply is the concatenation of
    # type=="text" events' part.text. Needs opencode's z.ai auth (see deploy
    # docs). (Flags verified against opencode 1.17.8; live resume recall pending
    # a z.ai round-trip.)
    "opencode": Backend(
        name="opencode",
        kind="cli",
        argv=("opencode", "run"),  # non-session one-shot
        env_unset=_HARNESS_SECRETS,
        session_model="capture",
        create_argv=(
            "opencode",
            "run",
            "--format",
            "json",
            "-m",
            "zai-coding-plan/glm-4.7",
            "{prompt}",
        ),
        resume_argv=(
            "opencode",
            "run",
            "--format",
            "json",
            "-s",
            "{sid}",
            "-m",
            "zai-coding-plan/glm-4.7",
            "{prompt}",
        ),
        capture_format="jsonl",
        capture_field="sessionID",  # camel-cap-D; id is `ses_`+base62 (any len)
        reply_format="jsonl",
        reply_event="text",
        reply_field="part.text",
        session_dead_signal="session not found",
        timeout_s=600.0,
    ),
    # The api-seam example: any OpenAI-compatible chat endpoint. Point base_url
    # at your provider and set OPENAI_API_KEY; swapping providers is config, not
    # code. Modeled by api/proto/james/chat/v1/chat.proto.
    "gpt": Backend(
        name="gpt",
        kind="api",
        base_url="https://api.openai.com",
        model="gpt-4o-mini",
        service_name="james.chat.v1.ChatService",
        tool_name="ChatService.CreateChatCompletion",
        secret_env={"api_key": "OPENAI_API_KEY"},
        timeout_s=60.0,
    ),
    # Screenshot a web page to PNG via headless Chromium; prompt is the URL.
    # Just another cli row: it shells out and writes a file, which the media
    # return path delivers to the chat. {prompt} is the URL, {outfile} the PNG
    # path the runner allocates. (chromium is in the flox manifest, Linux-only.)
    "shot": Backend(
        name="shot",
        kind="cli",
        argv=(
            "chromium",
            "--headless=new",
            "--no-sandbox",
            # Use /tmp not /dev/shm: containers default to a tiny 64MB /dev/shm,
            # which crashes the renderer on larger pages.
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--hide-scrollbars",
            "--window-size=1280,2000",
            # Persistent, logged-in profile (seed it with `james login <name>`).
            "--user-data-dir={profile_dir}",
            "--screenshot={outfile}",
            "{prompt}",
        ),
        artifact_mime="image/png",
        artifact_suffix=".png",
        uses_profile=True,
        env_unset=_HARNESS_SECRETS,
        timeout_s=60.0,
    ),
    # A2A peer: OpenClaw via the `openclaw-a2a-gateway` plugin. On the OpenClaw
    # host: `openclaw plugins install openclaw-a2a-gateway` (gateway port 18800;
    # gRPC on port+1 = 18801). base_url is the origin; the a2a-sdk client
    # fetches the agent card and negotiates transport — gRPC preferred, JSON-RPC
    # fallback (the gateway advertises both). Auth: gateway inbound bearer — put
    # the token in $OPENCLAW_A2A_TOKEN (an unset token disables this backend).
    # base_url defaults to loopback; edit this row for a remote gateway.
    "openclaw": Backend(
        name="openclaw",
        kind="a2a",
        base_url="http://127.0.0.1:18800",
        secret_env={"token": "OPENCLAW_A2A_TOKEN"},
        timeout_s=120.0,
    ),
    # A2A peer: Hermes — PLACEHOLDER, NOT REACHABLE TODAY. NousResearch/
    # hermes-agent does NOT ship A2A (proposal-only: issues #514/#4454; the
    # native adapter PR is unmerged; the old PyPI package is archived). Released
    # hermes exposes MCP + ACP, not A2A. Left with an empty base_url + unset
    # token so it stays disabled; set base_url + $HERMES_A2A_TOKEN only once a
    # real A2A endpoint fronts hermes.
    "hermes": Backend(
        name="hermes",
        kind="a2a",
        base_url="",
        secret_env={"token": "HERMES_A2A_TOKEN"},
        timeout_s=120.0,
    ),
}

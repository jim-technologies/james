"""The heart of james: resolve a backend and run a prompt against it.

``dispatch`` is the one function that turns a (backend, prompt) into a
normalised Result. It reads top-to-bottom: resolve the backend from the
registry, branch once on its kind, run it through an injected port, and
normalise the outcome.

Infrastructure (the subprocess runner, the HTTP client) is injected as a
Protocol so this module never imports infra and stays unit-testable with no
network or keys. The ports exchange only primitives, which keeps infra free of
domain types and the dependency direction one-way (biz never imports infra).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from biz.backends import REGISTRY, Backend


@dataclass(frozen=True, slots=True)
class Artifact:
    """A file produced by a backend (e.g. a screenshot), delivered inline."""

    content: bytes
    mime: str
    filename: str


@dataclass(frozen=True, slots=True)
class Result:
    """A normalised dispatch outcome, mapped one-to-one to DispatchResponse."""

    backend: str
    ok: bool
    text: str = ""
    error: str = ""
    artifacts: tuple[Artifact, ...] = ()


class CliRunner(Protocol):
    """Port: a cli backend → (returncode, stdout, stderr, artifact, session_id).

    A return code of 0 is success. A negative code signals a problem before or
    around exec (missing secret, timeout, command not found). When
    wants_artifact is set, the runner allocates a temp file (extension
    artifact_suffix), substitutes it for "{outfile}" in argv, reads the file the
    backend wrote, removes it, and returns its bytes (or b"" if none). No host
    path ever leaves infra.

    The capture_*/reply_* parameters drive a generic, table-driven JSON walk
    over the child's output (the runner is the only component holding the raw
    streams). When capture_format is set, the runner returns the CLI-minted
    session id as the 5th element (or "" if not found). When reply_format is
    set, the runner extracts the human reply from the structured stream and
    returns it IN PLACE OF stdout, so callers keep reading stdout as the
    answer. Both are best-effort and never raise (parse failure → "" / raw
    stdout). The runner takes only primitives, so infrastructure never imports
    the domain.
    """

    async def run(
        self,
        argv: Sequence[str],
        prompt: str,
        *,
        cwd: str,
        env_set: Mapping[str, str],
        env_unset: Sequence[str],
        secret_env: Mapping[str, str],
        timeout_s: float,
        wants_artifact: bool = False,
        artifact_suffix: str = "",
        user_data_dir: str = "",
        capture_stream: str = "stdout",
        capture_format: str = "",
        capture_field: str = "",
        capture_event: str = "",
        reply_format: str = "",
        reply_field: str = "",
        reply_event: str = "",
        reply_match: str = "",
    ) -> tuple[int, str, str, bytes, str]: ...


class ApiCaller(Protocol):
    """Port for calling an api backend; returns (ok, text, error)."""

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
    ) -> tuple[bool, str, str]: ...


class A2ACaller(Protocol):
    """Port: call a remote A2A peer (official a2a-sdk under the hood).

    The transport is negotiated from the peer's Agent Card — gRPC preferred,
    JSON-RPC / HTTP+JSON fallback; A2A v1.0 with v0.3 compat. Returns
    (ok, text, error, artifacts), where each artifact is a primitive
    (bytes, mime, filename) triple — biz constructs the Artifact, same as the
    cli path. token_env names the env var holding the peer's bearer token (read
    by infra; a missing token disables the backend). The caller fetches the
    agent card at agent_card_path, sends the prompt, and resolves any
    non-terminal Task by polling to the timeout_s deadline.
    """

    async def call(
        self,
        prompt: str,
        *,
        base_url: str,
        agent_card_path: str,
        token_env: str,
        timeout_s: float,
    ) -> tuple[bool, str, str, tuple[tuple[bytes, str, str], ...]]: ...


class SessionStore(Protocol):
    """Port: per-conversation agent session ids for resumable backends.

    ``resolve`` returns (session_id, resume): a stable id for the
    (backend, conversation) pair and whether the agent should resume it. With
    ``mint=True`` (caller_set backends) the id is chosen and persisted here, so
    every call after the first resumes — even if the creating run failed. With
    ``mint=False`` (capture backends) the CLI mints its own id, so a fresh entry
    returns ("", False) and the caller persists the captured id via ``record``.
    ``forget`` drops one entry so the next run creates a fresh session; it is
    the recovery path when a resume finds the agent's session gone (pruned, or
    store outlived the agent's own state).
    """

    async def resolve(
        self, backend: str, key: str, *, mint: bool = True
    ) -> tuple[str, bool]: ...

    async def record(self, backend: str, key: str, session_id: str) -> None: ...

    async def forget(self, backend: str, key: str) -> None: ...

    async def list_sessions(self) -> list[tuple[str, str]]: ...


async def dispatch(
    backend_name: str,
    prompt: str,
    *,
    cwd: str,
    default_backend: str,
    cli_runner: CliRunner,
    api_caller: ApiCaller,
    registry: Mapping[str, Backend] = REGISTRY,
    a2a_caller: A2ACaller | None = None,
    profiles_dir: str = "",
    default_profile: str = "default",
    mcp_config: str = "",
    session_key: str = "",
    session_store: SessionStore | None = None,
) -> Result:
    """Route a prompt to a backend and return a normalised Result.

    The backend may carry a ``:profile`` suffix ("shot:work") selecting a
    browser profile for backends that use one. An empty backend selects the
    default; an unknown name returns a help Result. Otherwise the backend's kind
    chooses the port — exactly one branch — and its outcome is normalised.

    Args:
        backend_name: Requested backend, optionally "name:profile"; empty
            selects the default.
        prompt: The task text to run.
        cwd: Working directory for cli backends.
        default_backend: Backend used when none is requested.
        cli_runner: Injected port for cli backends.
        api_caller: Injected port for api backends.
        a2a_caller: Injected port for a2a backends (remote A2A peers).
        registry: Backend registry (injectable for tests).
        profiles_dir: Base directory holding browser profile sub-dirs.
        default_profile: Profile used when none is named.
        mcp_config: Path to an MCP config, injected as "--mcp-config" for
            backends that accept it (claude).
        session_key: Conversation key (e.g. a chat thread); when set with a
            session-capable backend, the agent resumes that conversation.
        session_store: Injected port that maps a conversation to an agent
            session id (None disables session memory).

    Returns:
        A Result carrying the resolved backend, an ok flag, and text or error.
    """
    requested = backend_name.strip() or default_backend
    name, _, variant = requested.partition(":")
    name = name.strip()
    variant = variant.strip().lower()

    backend = registry.get(name)
    if backend is None:
        available = ", ".join(sorted(registry)) or "(none configured)"
        return Result(
            backend=name,
            ok=False,
            error=(
                f"Unknown backend '{name}'. Available: {available}. "
                f"Default: {default_backend}."
            ),
        )

    if not prompt.strip():
        return Result(backend=name, ok=False, error="Empty prompt.")

    # Reject anything that could escape profiles_dir (path traversal); the
    # variant becomes a directory name.
    if variant and not all(c.isalnum() or c in "-_" for c in variant):
        return Result(
            backend=name, ok=False, error=f"Invalid profile '{variant}'."
        )

    if backend.kind == "cli":
        user_data_dir = ""
        if backend.uses_profile:
            profile = variant or default_profile
            base = profiles_dir.rstrip("/")
            user_data_dir = f"{base}/{profile}" if base else profile

        # Flags injected on every attempt: an MCP config for CLIs that take one
        # as a flag (claude); codex/grok read their own config files instead.
        mcp = (
            ["--mcp-config", mcp_config]
            if backend.accepts_mcp_config and mcp_config
            else []
        )
        # Per-conversation session, two models (see biz/backends.py). caller_set
        # (claude): james mints the id and injects it via session_flag on
        # create, resume_flag later. capture (codex/grok/opencode): the CLI
        # mints its own id, so the create run uses create_argv and the runner
        # parses the id out of its output; james records it and resumes via
        # resume_argv. Either way a follow-up keeps the thread's memory.
        capture = backend.session_model == "capture"
        use_session = bool(
            backend.session_model and session_key and session_store is not None
        )
        sid, resume = "", False
        if use_session:
            sid, resume = await session_store.resolve(
                name, session_key, mint=not capture
            )

        # Run it. If a resume fails because the agent no longer holds that
        # session (it was pruned, or this store outlived the agent's own state
        # across a redeploy / HOME change), forget the dead id and retry once as
        # a fresh create, so the thread self-heals instead of wedging forever on
        # a dead resume. A genuine create is never retried this way, and a
        # transient resume failure (timeout, rate limit) keeps the memory.
        attempt = 0
        while True:
            if use_session and capture:
                template = (
                    backend.resume_argv if resume else backend.create_argv
                )
                argv = [
                    template[0],
                    *mcp,
                    *(a.replace("{sid}", sid) for a in template[1:]),
                ]
            elif use_session:
                flag = backend.resume_flag if resume else backend.session_flag
                argv = [backend.argv[0], *mcp, flag, sid, *backend.argv[1:]]
            else:
                argv = [backend.argv[0], *mcp, *backend.argv[1:]]
            code, out, err, artifact_bytes, captured = await cli_runner.run(
                argv,
                prompt,
                cwd=cwd,
                env_set=backend.env_set,
                env_unset=backend.env_unset,
                secret_env=backend.secret_env,
                timeout_s=backend.timeout_s,
                wants_artifact=bool(backend.artifact_mime),
                artifact_suffix=backend.artifact_suffix,
                user_data_dir=user_data_dir,
                capture_stream=backend.capture_stream,
                capture_format=backend.capture_format,
                capture_field=backend.capture_field,
                capture_event=backend.capture_event,
                reply_format=backend.reply_format,
                reply_field=backend.reply_field,
                reply_event=backend.reply_event,
                reply_match=backend.reply_match,
            )
            # A capture backend prints its minted id before doing the work, so
            # record it even if the create run then fails (mirrors caller_set:
            # the id is live regardless of exit code). Only on a create, only
            # when the runner actually parsed one — a failed grok create emits
            # an error object with no id, so captured is "" and nothing stored.
            if use_session and capture and not resume and captured:
                await session_store.record(name, session_key, captured)
            dead_resume = (
                code != 0
                and resume
                and attempt == 0
                and bool(backend.session_dead_signal)
                and backend.session_dead_signal in err.lower()
            )
            if use_session and dead_resume:
                await session_store.forget(name, session_key)
                sid, resume = await session_store.resolve(
                    name, session_key, mint=not capture
                )
                attempt += 1
                continue
            break

        if code != 0:
            detail = err.strip() or out.strip() or f"exited with code {code}"
            return Result(backend=name, ok=False, error=detail)
        if backend.artifact_mime and not artifact_bytes:
            return Result(backend=name, ok=False, error="no file was produced")
        if artifact_bytes:
            artifact = Artifact(
                content=artifact_bytes,
                mime=backend.artifact_mime,
                filename=f"{name}{backend.artifact_suffix}",
            )
            return Result(
                backend=name, ok=True, text=out.strip(), artifacts=(artifact,)
            )
        return Result(backend=name, ok=True, text=out.strip() or "(no output)")

    if backend.kind == "a2a":
        if a2a_caller is None:
            return Result(
                backend=name, ok=False, error="a2a backend not configured"
            )
        ok, text, err, arts = await a2a_caller.call(
            prompt,
            base_url=backend.base_url,
            agent_card_path=backend.agent_card_path,
            token_env=backend.secret_env.get("token", ""),
            timeout_s=backend.timeout_s,
        )
        if not ok:
            return Result(
                backend=name, ok=False, error=err or "a2a call failed"
            )
        # Map primitive triples to Artifact here (biz owns the domain type), the
        # same way the cli branch turns runner bytes into an Artifact.
        artifacts = tuple(
            Artifact(content=c, mime=m, filename=f) for (c, m, f) in arts
        )
        return Result(
            backend=name,
            ok=True,
            text=text.strip() or "(no output)",
            artifacts=artifacts,
        )

    # backend.kind == "api"
    ok, text, err = await api_caller.call(
        prompt,
        base_url=backend.base_url,
        model=backend.model,
        service_name=backend.service_name,
        tool_name=backend.tool_name,
        api_key_env=backend.secret_env.get("api_key", ""),
        timeout_s=backend.timeout_s,
    )
    if ok:
        return Result(backend=name, ok=True, text=text.strip() or "(no output)")
    return Result(backend=name, ok=False, error=err or "api call failed")

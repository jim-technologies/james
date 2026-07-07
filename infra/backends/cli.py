"""Subprocess runner for cli backends — thin, primitive-only, never raises.

Builds the child environment (copy os.environ, apply env_set, drop env_unset,
resolve secret_env lazily), delivers the prompt via stdin or an argv "{prompt}"
placeholder, runs the command, and returns (returncode, stdout, stderr,
artifact_bytes, session_id). When wants_artifact is set it allocates a temp file
(extension artifact_suffix), substitutes it for "{outfile}" in argv, reads back
the file the backend wrote, removes it, and returns its bytes (b"" if none) — so
the temp file never outlives the call and no host path leaves infra. A
user_data_dir is substituted for "{profile_dir}"; runs sharing a profile are
serialized (Chrome locks a profile to one process).

For "capture" session backends (codex/grok/opencode) the CLI mints its own
session id and emits it as structured (JSON/JSONL) output. The capture_*/reply_*
parameters drive a GENERIC, table-driven JSON walk over that output — there are
no per-vendor branches, only the data rule (which stream, jsonl-vs-json, which
field/event). The walk returns the minted id as the 5th tuple element and, when
reply_* is set, replaces stdout with the extracted human reply so callers keep
reading stdout as the answer. Both are best-effort.

Every failure (missing secret, missing command, timeout, a malformed JSON
stream) is returned — a negative return code with a message on stderr, or an
empty id / raw stdout — rather than raised, so a broken backend never takes down
the dispatcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import tempfile
from collections.abc import Iterator, Mapping, Sequence


def _dig(obj: object, path: str) -> object:
    """Resolve a dotted path (e.g. "item.text") to a value, or None."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _json_objects(text: str, fmt: str) -> Iterator[dict]:
    """Yield JSON objects: per non-empty line for "jsonl", the whole for "json".

    Non-JSON / non-object content is skipped (best-effort, never raises) — so
    pretty-printed "json" (one object across many lines) must NOT be line-split.
    """
    if fmt == "json":
        try:
            obj = json.loads(text)
        except ValueError:
            return
        if isinstance(obj, dict):
            yield obj
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _capture_session_id(text: str, fmt: str, field: str, event: str) -> str:
    """Return the first non-empty string ``field`` in the output, or ""."""
    if not fmt or not field:
        return ""
    for obj in _json_objects(text, fmt):
        if event and obj.get("type") != event:
            continue
        val = obj.get(field)
        if isinstance(val, str) and val:
            return val
    return ""


def _extract_reply(
    text: str, fmt: str, field: str, event: str, match: str
) -> str | None:
    """Concatenate the reply text from matching objects, or None if none match.

    ``event`` filters on the top-level "type"; ``match`` is an optional extra
    "dotted.path=value" filter (e.g. codex's item.type=agent_message); ``field``
    is the dotted path to the text.
    """
    if not fmt or not field:
        return None
    mpath, _, mval = match.partition("=")
    parts: list[str] = []
    for obj in _json_objects(text, fmt):
        if event and obj.get("type") != event:
            continue
        if match and str(_dig(obj, mpath)) != mval:
            continue
        val = _dig(obj, field)
        if isinstance(val, str):
            parts.append(val)
    if not parts:
        return None
    return "".join(parts)


class SubprocessCliRunner:
    """Runs a cli backend as a child process; implements the CliRunner port."""

    def __init__(self) -> None:
        # One lock per profile dir: Chrome refuses a second process on the same
        # user-data-dir, so same-profile runs must serialize (different profiles
        # still run in parallel).
        self._profile_locks: dict[str, asyncio.Lock] = {}

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
    ) -> tuple[int, str, str, bytes, str]:
        """Run; return (returncode, stdout, stderr, artifact_bytes, session_id).

        With capture_*/reply_* set, the CLI-minted session id is parsed out (5th
        element) and the human reply replaces stdout (see module docstring).
        """
        env = dict(os.environ)
        env.update(env_set)
        for key in env_unset:
            env.pop(key, None)
        for child_var, source in secret_env.items():
            value = os.environ.get(source)
            if not value:
                msg = f"missing secret: set {source} for this backend"
                return (-1, "", msg, b"", "")
            env[child_var] = value

        outfile = ""
        if wants_artifact:
            fd, outfile = tempfile.mkstemp(suffix=artifact_suffix or "")
            os.close(fd)
        if user_data_dir:
            # The profile dir holds live logins: create it 0700 and tighten it
            # even when it pre-existed looser (makedirs' mode applies only to
            # dirs it creates). The parent is created 0700 too, but a
            # pre-existing parent's mode is the operator's choice — left alone.
            # Each step gets its own suppress so one failure (e.g. EPERM on an
            # unowned parent) can't skip tightening the profile dir itself.
            parent = os.path.dirname(user_data_dir)
            if parent:
                with contextlib.suppress(OSError):
                    os.makedirs(parent, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.makedirs(user_data_dir, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(user_data_dir, 0o700)

        # Substitute {outfile}/{profile_dir} first (template), then {prompt} —
        # so a prompt value containing a placeholder literal is not re-scanned.
        # Prompt delivery: a "{prompt}" placeholder passes the prompt as an
        # argument, else it goes on stdin (the default — avoids flag-injection
        # from a leading "-" and argv length limits).
        had_placeholder = any("{prompt}" in arg for arg in argv)
        resolved = [
            arg.replace("{outfile}", outfile)
            .replace("{profile_dir}", user_data_dir)
            .replace("{prompt}", prompt)
            for arg in argv
        ]
        stdin_data = None if had_placeholder else prompt.encode()
        first = argv[0] if argv else ""

        rc, out, err, artifact = -1, "", "", b""
        try:
            if not resolved:
                return (-1, "", "backend has no command configured", b"", "")
            if user_data_dir:
                # Serialize same-profile runs; other profiles run in parallel.
                lock = self._profile_locks.setdefault(
                    user_data_dir, asyncio.Lock()
                )
                async with lock:
                    rc, out, err, artifact = await self._spawn(
                        resolved,
                        first,
                        stdin_data,
                        cwd,
                        env,
                        timeout_s,
                        outfile,
                    )
            else:
                rc, out, err, artifact = await self._spawn(
                    resolved, first, stdin_data, cwd, env, timeout_s, outfile
                )
        finally:
            # The bytes are in memory now; the temp file never outlives the run.
            if outfile:
                with contextlib.suppress(OSError):
                    os.unlink(outfile)

        # Capture model (codex/grok/opencode): pull the CLI-minted id from the
        # configured stream and, if the output is structured, the human reply in
        # place of raw stdout. Best-effort, never raises — a parse miss leaves
        # the id "" and stdout untouched.
        captured = _capture_session_id(
            err if capture_stream == "stderr" else out,
            capture_format,
            capture_field,
            capture_event,
        )
        reply = _extract_reply(
            out, reply_format, reply_field, reply_event, reply_match
        )
        return (rc, out if reply is None else reply, err, artifact, captured)

    @staticmethod
    def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """SIGKILL the child's whole process group (agents spawn helpers).

        The child is a session leader (start_new_session), so its group id is
        its pid and exists as long as it does; suppress covers the
        already-reaped race.
        """
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)

    async def _spawn(
        self,
        resolved: list[str],
        first: str,
        stdin_data: bytes | None,
        cwd: str,
        env: dict[str, str],
        timeout_s: float,
        outfile: str,
    ) -> tuple[int, str, str, bytes]:
        """Spawn the child, run to completion/timeout, collect any artifact."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *resolved,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # Own process group, so a timeout can kill the whole tree:
                # agent CLIs spawn tool children and chromium spawns helpers,
                # which would otherwise outlive the kill (a leaked chromium
                # keeps its profile locked, wedging later runs on it).
                start_new_session=True,
            )
        except FileNotFoundError:
            return (-1, "", f"command not found: {first}", b"")
        except (OSError, ValueError) as exc:
            # ValueError covers an embedded NUL in argv (e.g. a prompt with \x00
            # via the {prompt} placeholder); keep the never-raises guarantee.
            return (-1, "", f"failed to start {first}: {exc}", b"")

        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=stdin_data), timeout=timeout_s
            )
        except TimeoutError:
            self._kill_tree(proc)
            await proc.wait()
            return (-1, "", f"timed out after {timeout_s:g}s", b"")
        except asyncio.CancelledError:
            # james itself is stopping (Ctrl+C / task cancelled). The child is
            # in its own session (start_new_session), so the terminal's SIGINT
            # never reaches it — kill the tree here or it outlives james (a
            # leaked chromium would keep its profile locked).
            self._kill_tree(proc)
            await proc.wait()
            raise

        rc = proc.returncode or 0
        artifact = b""
        if outfile and rc == 0:
            with contextlib.suppress(OSError), open(outfile, "rb") as handle:
                artifact = handle.read()
        return (
            rc,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
            artifact,
        )

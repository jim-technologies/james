"""A small JSON-file store mapping a conversation to an agent session id.

Each (backend, conversation key) maps to the agent's session id, so a follow-up
message in the same chat thread resumes the same agent session (memory). The
presence of an entry is the create-vs-resume signal: the first message in a
thread mints a fresh id and the run *creates* a session against it; every later
message *resumes* that id. The entry is persisted on that first call, so even a
failed first run resumes next time — the agent registers the id on first use
regardless of exit code, and resuming a registered (even empty) session
succeeds. Implements the biz SessionStore port (primitives only); writes are
atomic and a lock guards the read-modify-write.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path

# Separator between backend and conversation key in a store entry id. The ASCII
# unit separator never appears in a backend name or conversation key.
_SEP = "\x1f"


class JsonSessionStore:
    """Conversation -> agent session id, persisted to a JSON file."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, self._path)

    async def resolve(
        self, backend: str, key: str, *, mint: bool = True
    ) -> tuple[str, bool]:
        """Return (session_id, resume); mint and persist a fresh id if absent.

        ``resume`` is False only on the very first call for a (backend, key) —
        that run then creates the session.

        ``mint`` distinguishes the two session models. For caller_set backends
        (mint=True, the default) james chooses the id, so a fresh entry is
        minted and persisted here and every later call resumes — even if the
        creating run failed. For capture backends (mint=False) the CLI mints its
        OWN id, unknown until after the run, so a fresh entry returns ("",
        False) and the caller persists the captured id later via ``record``.

        A missing, non-dict, or id-less entry is treated as absent, so a corrupt
        or hand-edited file self-heals rather than raising out of infra.
        """
        ident = f"{backend}{_SEP}{key}"
        async with self._lock:
            data = self._load()
            entry = data.get(ident)
            sid = entry.get("id") if isinstance(entry, dict) else None
            if not sid:
                if not mint:
                    return (
                        "",
                        False,
                    )  # capture: id comes from the run's output
                sid = str(uuid.uuid4())
                data[ident] = {"id": sid}
                self._save(data)
                return (sid, False)
            return (str(sid), True)

    async def record(self, backend: str, key: str, session_id: str) -> None:
        """Persist a CLI-minted id captured after a create run (capture)."""
        if not session_id:
            return
        ident = f"{backend}{_SEP}{key}"
        async with self._lock:
            data = self._load()
            data[ident] = {"id": session_id}
            with contextlib.suppress(OSError):
                self._save(data)

    async def forget(self, backend: str, key: str) -> None:
        """Drop one backend's session for a conversation (e.g. a dead resume).

        Unlike ``reset`` (which forgets every backend in the conversation), this
        removes a single (backend, key) entry so the next run for it creates a
        fresh session — the recovery path when a resume finds the agent's
        session gone.
        """
        ident = f"{backend}{_SEP}{key}"
        async with self._lock:
            data = self._load()
            if ident in data:
                del data[ident]
                with contextlib.suppress(OSError):
                    self._save(data)

    async def list_sessions(self) -> list[tuple[str, str]]:
        """Return (backend, conversation_key) for every stored session.

        Read-only; primitives only. Malformed / id-less entries are skipped so a
        hand-edited file can't break the listing.
        """
        async with self._lock:
            data = self._load()
        out: list[tuple[str, str]] = []
        for ident, entry in data.items():
            if not (isinstance(entry, dict) and entry.get("id")):
                continue
            backend, sep, key = ident.partition(_SEP)
            if sep:
                out.append((backend, key))
        return out

    async def reset(self, key: str) -> int:
        """Forget all of a conversation's sessions; return how many removed."""
        suffix = f"{_SEP}{key}"
        async with self._lock:
            data = self._load()
            stale = [k for k in data if k.endswith(suffix)]
            for k in stale:
                del data[k]
            if stale:
                with contextlib.suppress(OSError):
                    self._save(data)
            return len(stale)

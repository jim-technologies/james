"""Unit tests for the JSON session store."""

from __future__ import annotations

import json

from infra.sessions.store import JsonSessionStore

_SEP = "\x1f"


async def test_resolve_creates_then_resumes(tmp_path):
    store = JsonSessionStore(str(tmp_path / "s.json"))
    sid, resume = await store.resolve("claude", "k1")
    assert sid and resume is False
    # The id is recorded on the first call, so the same key now resumes — even
    # without a prior successful run (the agent registers the id on first use).
    sid2, resume2 = await store.resolve("claude", "k1")
    assert sid2 == sid and resume2 is True


async def test_resume_persists_across_instances(tmp_path):
    path = str(tmp_path / "s.json")
    sid, _ = await JsonSessionStore(path).resolve("claude", "k1")
    # A fresh instance reading the same file resumes the same id.
    sid2, resume = await JsonSessionStore(path).resolve("claude", "k1")
    assert sid2 == sid and resume is True


async def test_forget_drops_one_backend_only(tmp_path):
    store = JsonSessionStore(str(tmp_path / "s.json"))
    a, _ = await store.resolve("claude", "k1")
    b, _ = await store.resolve("codex", "k1")
    await store.forget("claude", "k1")
    # claude re-creates with a new id; codex still resumes its original.
    a2, a_resume = await store.resolve("claude", "k1")
    b2, b_resume = await store.resolve("codex", "k1")
    assert a2 != a and a_resume is False
    assert b2 == b and b_resume is True


async def test_reset_forgets_the_conversation(tmp_path):
    store = JsonSessionStore(str(tmp_path / "s.json"))
    sid, _ = await store.resolve("claude", "k1")
    await store.resolve("claude", "k1")  # now resuming
    removed = await store.reset("k1")
    assert removed == 1
    # After reset, a fresh session id that creates again.
    sid2, resume = await store.resolve("claude", "k1")
    assert sid2 != sid and resume is False


async def test_keys_are_isolated_by_backend_and_conversation(tmp_path):
    store = JsonSessionStore(str(tmp_path / "s.json"))
    a, _ = await store.resolve("claude", "k1")
    b, _ = await store.resolve("claude", "k2")
    c, _ = await store.resolve("codex", "k1")
    assert len({a, b, c}) == 3  # distinct sessions


async def test_list_sessions_returns_backend_key_pairs(tmp_path):
    store = JsonSessionStore(str(tmp_path / "s.json"))
    await store.resolve("claude", "k1")  # caller_set: minted
    await store.record("codex", "k2", "thread-1")  # capture: recorded
    await store.resolve("codex", "k3", mint=False)  # no id yet -> not listed
    listed = sorted(await store.list_sessions())
    assert listed == [("claude", "k1"), ("codex", "k2")]


async def test_list_sessions_skips_malformed(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                f"claude{_SEP}good": {"id": "x"},
                f"claude{_SEP}bad": {"note": "no id"},
                "noseparator": {"id": "y"},
            }
        )
    )
    listed = await JsonSessionStore(str(path)).list_sessions()
    assert listed == [("claude", "good")]


async def test_corrupt_file_self_heals(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{ not valid json")
    store = JsonSessionStore(str(path))
    sid, resume = await store.resolve("claude", "k1")
    assert sid and resume is False


async def test_capture_resolve_does_not_mint(tmp_path):
    # Capture backends (mint=False): a fresh key returns no id (the CLI mints it),
    # and only after record() does the key resume.
    path = str(tmp_path / "s.json")
    store = JsonSessionStore(path)
    sid, resume = await store.resolve("codex", "k1", mint=False)
    assert sid == "" and resume is False
    # nothing persisted yet
    sid2, resume2 = await store.resolve("codex", "k1", mint=False)
    assert sid2 == "" and resume2 is False
    await store.record("codex", "k1", "thread-abc")
    # a fresh instance reading the same file now resumes the captured id
    sid3, resume3 = await JsonSessionStore(path).resolve(
        "codex", "k1", mint=False
    )
    assert sid3 == "thread-abc" and resume3 is True


async def test_record_overwrites_after_forget(tmp_path):
    store = JsonSessionStore(str(tmp_path / "s.json"))
    await store.record("grok", "k1", "id-1")
    await store.forget("grok", "k1")
    sid, resume = await store.resolve("grok", "k1", mint=False)
    assert sid == "" and resume is False  # forgotten -> creates fresh next run
    await store.record("grok", "k1", "id-2")
    sid2, resume2 = await store.resolve("grok", "k1", mint=False)
    assert sid2 == "id-2" and resume2 is True


async def test_record_empty_id_is_noop(tmp_path):
    store = JsonSessionStore(str(tmp_path / "s.json"))
    await store.record("codex", "k1", "")
    sid, resume = await store.resolve("codex", "k1", mint=False)
    assert sid == "" and resume is False  # empty id never persisted


async def test_malformed_entries_treated_as_absent(tmp_path):
    # Entries a hand-edit or schema drift could produce must mint a fresh
    # session, not raise KeyError/TypeError out of infra into biz.
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                f"claude{_SEP}k1": {"note": "no id"},  # id-less dict
                f"claude{_SEP}k2": "raw-string",  # non-dict
                f"claude{_SEP}k3": {"id": None},  # null id
            }
        )
    )
    store = JsonSessionStore(str(path))
    for key in ("k1", "k2", "k3"):
        sid, resume = await store.resolve("claude", key)
        assert sid and resume is False

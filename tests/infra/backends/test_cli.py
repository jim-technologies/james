"""Unit tests for the subprocess CLI runner — real child processes, no network.

Each test runs the test interpreter as a tiny program to observe how the runner
builds the child environment, delivers the prompt, and (for capture backends)
walks structured output for the session id and reply. No keys, no network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from infra.backends.cli import SubprocessCliRunner

PY = sys.executable


async def test_stdin_delivery_and_env_set():
    runner = SubprocessCliRunner()
    code, out, _err, _art, _sid = await runner.run(
        (
            PY,
            "-c",
            "import os,sys;sys.stdout.write(sys.stdin.read()+os.environ['MARK'])",
        ),
        "PROMPT",
        cwd=".",
        env_set={"MARK": "/M"},
        env_unset=(),
        secret_env={},
        timeout_s=30,
    )
    assert code == 0
    assert out == "PROMPT/M"  # prompt arrived on stdin; env_set applied


async def test_env_unset_removes_parent_variable():
    runner = SubprocessCliRunner()
    _code, out, _err, _art, _sid = await runner.run(
        (
            PY,
            "-c",
            "import os,sys;sys.stdout.write(os.environ.get('HOME','GONE'))",
        ),
        "x",
        cwd=".",
        env_set={},
        env_unset=("HOME",),
        secret_env={},
        timeout_s=30,
    )
    assert out == "GONE"  # HOME existed in the parent but was dropped


async def test_env_unset_overrides_env_set():
    runner = SubprocessCliRunner()
    _code, out, _err, _art, _sid = await runner.run(
        (
            PY,
            "-c",
            "import os,sys;sys.stdout.write(os.environ.get('Z','GONE'))",
        ),
        "x",
        cwd=".",
        env_set={"Z": "present"},
        env_unset=("Z",),
        secret_env={},
        timeout_s=30,
    )
    assert out == "GONE"


async def test_lazy_secret_env_present():
    os.environ["SRC_KEY"] = "sekret"
    try:
        runner = SubprocessCliRunner()
        code, out, _err, _art, _sid = await runner.run(
            (
                PY,
                "-c",
                "import os,sys;sys.stdout.write(os.environ['CHILD_KEY'])",
            ),
            "x",
            cwd=".",
            env_set={},
            env_unset=(),
            secret_env={"CHILD_KEY": "SRC_KEY"},
            timeout_s=30,
        )
        assert code == 0 and out == "sekret"
    finally:
        del os.environ["SRC_KEY"]


async def test_lazy_secret_env_missing_disables_backend():
    runner = SubprocessCliRunner()
    code, _out, err, _art, _sid = await runner.run(
        (PY, "-c", "print('should not run')"),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={"CHILD_KEY": "DEFINITELY_UNSET_VAR_XYZ"},
        timeout_s=30,
    )
    assert code == -1
    assert "DEFINITELY_UNSET_VAR_XYZ" in err


async def test_argv_placeholder_substitution():
    runner = SubprocessCliRunner()
    _code, out, _err, _art, _sid = await runner.run(
        (PY, "-c", "import sys;sys.stdout.write(sys.argv[1])", "{prompt}"),
        "INLINE",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
    )
    assert out == "INLINE"  # delivered via the {prompt} argv placeholder


async def test_command_not_found_is_clean_error():
    runner = SubprocessCliRunner()
    code, _out, err, _art, _sid = await runner.run(
        ("this-command-does-not-exist-xyz",),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
    )
    assert code == -1 and "not found" in err


async def test_timeout_is_clean_error():
    runner = SubprocessCliRunner()
    code, _out, err, _art, _sid = await runner.run(
        (PY, "-c", "import time;time.sleep(5)"),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=0.5,
    )
    assert code == -1 and "timed out" in err


async def test_timeout_kills_the_whole_process_group(tmp_path):
    # Agents spawn their own children (tool processes, chromium helpers); on a
    # timeout the runner must kill the whole group, or a leaked chromium keeps
    # its profile locked. The child writes its grandchild's pid to a file and
    # then outsleeps the timeout; the grandchild must be gone afterwards.
    pid_file = tmp_path / "grandchild.pid"
    prog = (
        "import subprocess,time;"
        f"p=subprocess.Popen([{PY!r},'-c','import time;time.sleep(60)']);"
        f"open({str(pid_file)!r},'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    runner = SubprocessCliRunner()
    code, _out, err, _art, _sid = await runner.run(
        (PY, "-c", prog),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=2.0,
    )
    assert code == -1 and "timed out" in err
    pid = int(pid_file.read_text())
    for _ in range(40):  # allow a moment for init to reap the orphan
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(pid, 9)  # don't leak it out of the test suite
        raise AssertionError("grandchild survived the timeout kill")


async def test_cancellation_kills_the_whole_process_group(tmp_path):
    # The child runs in its own session, so james's own Ctrl+C/SIGTERM never
    # reaches it via the terminal; on cancellation the runner must kill the
    # tree itself or the agent (and e.g. its chromium) outlives james.
    pid_file = tmp_path / "grandchild.pid"
    prog = (
        "import subprocess,time;"
        f"p=subprocess.Popen([{PY!r},'-c','import time;time.sleep(60)']);"
        f"open({str(pid_file)!r},'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    runner = SubprocessCliRunner()
    task = asyncio.create_task(
        runner.run(
            (PY, "-c", prog),
            "x",
            cwd=".",
            env_set={},
            env_unset=(),
            secret_env={},
            timeout_s=60,
        )
    )
    for _ in range(100):  # wait for the grandchild pid to land on disk
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    else:
        task.cancel()
        raise AssertionError("child never started")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    pid = int(pid_file.read_text())
    for _ in range(40):  # allow a moment for init to reap the orphan
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(pid, 9)  # don't leak it out of the test suite
        raise AssertionError("grandchild survived the cancellation kill")


async def test_existing_loose_profile_dir_is_tightened(tmp_path):
    # makedirs' mode applies only to dirs it creates; a pre-existing profile
    # dir at a looser mode must still be chmodded private (it holds logins).
    profile = tmp_path / "work"
    profile.mkdir(mode=0o755)
    runner = SubprocessCliRunner()
    code, _out, _err, _art, _sid = await runner.run(
        (PY, "-c", "pass", "--user-data-dir={profile_dir}"),
        "",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
        user_data_dir=str(profile),
    )
    assert code == 0
    assert (os.stat(profile).st_mode & 0o777) == 0o700


async def test_nul_byte_in_placeholder_prompt_is_clean_error():
    # A NUL in the prompt would raise ValueError from create_subprocess_exec
    # when delivered via a {prompt} placeholder; it must be returned, not raised.
    runner = SubprocessCliRunner()
    code, _out, err, _art, _sid = await runner.run(
        (PY, "-c", "import sys;sys.stdout.write(sys.argv[1])", "{prompt}"),
        "bad\x00prompt",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
    )
    assert code == -1 and err  # clean error, no exception escaped


async def test_empty_argv_is_clean_error():
    runner = SubprocessCliRunner()
    code, _out, err, _art, _sid = await runner.run(
        (),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
    )
    assert code == -1 and "no command" in err


async def test_artifact_bytes_are_collected():
    # A tool that writes to {outfile} yields the file's bytes as the artifact.
    runner = SubprocessCliRunner()
    code, _out, _err, art, _sid = await runner.run(
        (
            PY,
            "-c",
            "import sys;open(sys.argv[1],'wb').write(b'PNGDATA')",
            "{outfile}",
        ),
        "",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
        wants_artifact=True,
        artifact_suffix=".png",
    )
    assert code == 0
    assert art == b"PNGDATA"  # bytes returned; temp file already removed


async def test_profile_dir_substituted_and_created(tmp_path):
    # {profile_dir} is substituted into argv and the dir is created.
    runner = SubprocessCliRunner()
    profile = str(tmp_path / "work")
    code, _out, _err, art, _sid = await runner.run(
        (
            PY,
            "-c",
            "import sys;open(sys.argv[2],'w').write(sys.argv[1])",
            "--user-data-dir={profile_dir}",
            "{outfile}",
        ),
        "",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
        wants_artifact=True,
        artifact_suffix=".txt",
        user_data_dir=profile,
    )
    assert code == 0
    assert art.decode() == f"--user-data-dir={profile}"
    assert os.path.isdir(profile)  # runner created the profile dir
    assert (os.stat(profile).st_mode & 0o777) == 0o700  # private (credential)


async def test_same_profile_runs_are_serialized(tmp_path):
    # Chrome locks a profile to one process; the runner must serialize same-dir
    # runs. Two concurrent runs on one profile must not interleave.
    runner = SubprocessCliRunner()
    marker = tmp_path / "m.txt"
    profile = str(tmp_path / "p")
    tool = (
        PY,
        "-c",
        "import sys,time;f=open(sys.argv[1],'a');f.write('s');f.flush();"
        "time.sleep(0.3);f.write('e');f.flush()",
        str(marker),
    )

    async def one():
        await runner.run(
            tool,
            "",
            cwd=".",
            env_set={},
            env_unset=(),
            secret_env={},
            timeout_s=30,
            user_data_dir=profile,
        )

    await asyncio.gather(one(), one())
    assert marker.read_text() == "sese"  # serialized, not interleaved "ssee"


async def test_artifact_empty_when_tool_writes_nothing():
    # The tool ignores {outfile}; the empty temp file is cleaned, no artifact.
    runner = SubprocessCliRunner()
    code, _out, _err, art, _sid = await runner.run(
        (PY, "-c", "print('did nothing')", "{outfile}"),
        "",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
        wants_artifact=True,
        artifact_suffix=".png",
    )
    assert code == 0
    assert art == b""  # empty file -> no artifact bytes


# --- capture model: the generic JSON walker for session id + reply ---


async def test_capture_jsonl_id_and_nested_reply_filter():
    # codex-shape: thread.started carries the id; the reply is item.completed
    # with item.type==agent_message — reasoning/tool items must NOT be included.
    prog = (
        "import json;"
        "print(json.dumps({'type':'thread.started','thread_id':'tid-123'}));"
        "print(json.dumps({'type':'item.completed',"
        "'item':{'type':'reasoning','text':'thinking'}}));"
        "print(json.dumps({'type':'item.completed',"
        "'item':{'type':'agent_message','text':'hello'}}))"
    )
    runner = SubprocessCliRunner()
    code, out, _err, _art, sid = await runner.run(
        (PY, "-c", prog),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
        capture_format="jsonl",
        capture_event="thread.started",
        capture_field="thread_id",
        reply_format="jsonl",
        reply_event="item.completed",
        reply_match="item.type=agent_message",
        reply_field="item.text",
    )
    assert code == 0
    assert sid == "tid-123"
    assert out == "hello"  # reasoning 'thinking' filtered out by reply_match


async def test_capture_whole_blob_json_pretty_printed():
    # grok-shape: --output-format json is ONE object pretty-printed across many
    # lines — the walker must parse the whole blob, not line-split.
    blob = json.dumps(
        {"text": "the answer", "sessionId": "sid-7", "stopReason": "end"},
        indent=2,
    )
    runner = SubprocessCliRunner()
    code, out, _err, _art, sid = await runner.run(
        (PY, "-c", f"print({blob!r})"),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
        capture_format="json",
        capture_field="sessionId",
        reply_format="json",
        reply_field="text",
    )
    assert code == 0
    assert sid == "sid-7"
    assert out == "the answer"


async def test_capture_jsonl_multipart_reply_concatenated():
    # opencode-shape: id on every line; reply = concat of type==text part.text.
    prog = (
        "import json;"
        "print(json.dumps({'type':'start','sessionID':'ses_abc'}));"
        "print(json.dumps({'type':'text','sessionID':'ses_abc',"
        "'part':{'text':'part1 '}}));"
        "print(json.dumps({'type':'text','sessionID':'ses_abc',"
        "'part':{'text':'part2'}}))"
    )
    runner = SubprocessCliRunner()
    code, out, _err, _art, sid = await runner.run(
        (PY, "-c", prog),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
        capture_format="jsonl",
        capture_field="sessionID",
        reply_format="jsonl",
        reply_event="text",
        reply_field="part.text",
    )
    assert code == 0
    assert sid == "ses_abc"
    assert out == "part1 part2"


async def test_capture_on_garbage_output_never_raises():
    # Non-JSON output with capture configured: no id, stdout passed through raw.
    runner = SubprocessCliRunner()
    code, out, _err, _art, sid = await runner.run(
        (PY, "-c", "print('not json at all');print('{ broken')"),
        "x",
        cwd=".",
        env_set={},
        env_unset=(),
        secret_env={},
        timeout_s=30,
        capture_format="jsonl",
        capture_field="thread_id",
        reply_format="jsonl",
        reply_event="item.completed",
        reply_field="item.text",
    )
    assert code == 0
    assert sid == ""  # nothing parseable
    assert "not json at all" in out  # raw stdout preserved (reply was None)

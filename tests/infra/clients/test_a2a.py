"""Unit tests for the a2a-sdk client — the response-mapping logic.

The transport/negotiation is the SDK's job (and is exercised by a gated live
test against a real peer); here we test james's part: turning the SDK's
StreamResponse events into the (ok, text, error, artifacts) tuple, plus the
fail-closed missing-token path. Protos are constructed directly — no network.
"""

from __future__ import annotations

from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    StreamResponse,
    Task,
    TaskState,
    TaskStatus,
)

from infra.clients.a2a import A2ASdkCaller, _events_to_result


def _msg(text):
    return Message(
        role=Role.ROLE_AGENT, parts=[Part(text=text)], message_id="m1"
    )


def test_message_reply_maps_to_text():
    events = [StreamResponse(message=_msg("hello back"))]
    ok, text, err, arts = _events_to_result(events)
    assert ok and text == "hello back" and err == "" and arts == ()


def test_task_completed_with_text_and_file_artifact():
    task = Task(
        id="t1",
        status=TaskStatus(
            state=TaskState.TASK_STATE_COMPLETED, message=_msg("done")
        ),
        artifacts=[
            Artifact(
                artifact_id="a1",
                parts=[
                    Part(
                        raw=b"PNGDATA", media_type="image/png", filename="o.png"
                    )
                ],
            )
        ],
    )
    ok, text, err, arts = _events_to_result([StreamResponse(task=task)])
    assert ok and "done" in text and err == ""
    assert arts == ((b"PNGDATA", "image/png", "o.png"),)


def test_task_failed_surfaces_error():
    task = Task(id="t1", status=TaskStatus(state=TaskState.TASK_STATE_FAILED))
    ok, _text, err, arts = _events_to_result([StreamResponse(task=task)])
    assert not ok and "failed" in err.lower() and arts == ()


def test_file_uri_part_is_surfaced_as_text():
    msg = Message(
        role=Role.ROLE_AGENT,
        parts=[Part(url="https://x/y.pdf", media_type="application/pdf")],
        message_id="m1",
    )
    ok, text, _err, arts = _events_to_result([StreamResponse(message=msg)])
    assert ok and "https://x/y.pdf" in text and arts == ()


async def test_missing_token_is_clean_error():
    ok, text, err, arts = await A2ASdkCaller().call(
        "hi",
        base_url="http://127.0.0.1:18800",
        agent_card_path="/.well-known/agent-card.json",
        token_env="DEFINITELY_UNSET_A2A_XYZ",
        timeout_s=5,
    )
    assert not ok and "missing token" in err and arts == ()

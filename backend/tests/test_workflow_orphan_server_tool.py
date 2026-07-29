"""Regression tests for the orphan `server_tool_use` strip in workflow execution.

Root cause of run d1ba4ced (workflow "DD Efficacy Tracker - Daily Update",
2026-07-09): a `bash_code_execution` server_tool_use, appended VERBATIM as an
Anthropic SDK object (`response.raw.content`) and cut off by `max_tokens` before
its result block arrived, survived `_strip_orphan_server_tools` because that
strip only inspected plain-dict blocks. SDK-object blocks were skipped in the
id-collection loop AND kept unconditionally in the drop loop, so the orphan
reached the Anthropic request and 400'd:

    messages.1: `bash_code_execution` tool use with id `srvtoolu_...` was found
    without a corresponding `bash_code_execution_tool_result` block

The fix routes block-type/id inspection through `_block_fields`, which reads both
dicts and SDK `BaseModel` blocks, so the orphan is now dropped everywhere the
strip runs (per-round sanitize, resume seed, escalation conversion, drop-oldest).
"""
from anthropic.types.beta import BetaServerToolUseBlock, BetaThinkingBlock

from services.workflow_execution_service import (
    _block_fields,
    _make_resume_safe,
    _strip_orphan_server_tools,
)

ORPHAN_ID = "srvtoolu_01Cr7tvrxWRTw1MxUA1cnYLi"


def _server_use_sdk(block_id: str, name: str = "bash_code_execution") -> BetaServerToolUseBlock:
    """A real Anthropic SDK server_tool_use block (pydantic BaseModel), exactly
    what `response.raw.content` yields on the Anthropic path."""
    return BetaServerToolUseBlock(
        id=block_id, name=name, input={"command": "echo hi"}, type="server_tool_use"
    )


def _thinking_sdk() -> BetaThinkingBlock:
    return BetaThinkingBlock(thinking="reasoning...", signature="sig", type="thinking")


def _orphan_server_use_ids(messages: list) -> set:
    """server_tool_use ids with NO matching *_tool_result anywhere — precisely the
    shape Anthropic 400s on. Uses the same `_block_fields` view as the strip."""
    use_ids: set = set()
    result_ids: set = set()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            btype, bid, btuid = _block_fields(b)
            if btype == "server_tool_use" and bid:
                use_ids.add(bid)
            elif isinstance(btype, str) and btype.endswith("_tool_result") and btype != "tool_result" and btuid:
                result_ids.add(btuid)
    return use_ids - result_ids


def _failing_run_messages() -> list:
    """In-memory state at the top of run d1ba4ced's failing round: the orphan
    bash server_tool_use is an SDK object in a NON-trailing assistant turn, with
    the `max_tokens` synthesis nudge (a user turn) appended right behind it."""
    return [
        {"role": "user", "content": "## Workflow Instructions ..."},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "get-grafana-dashboard-0", "name": "get_grafana_dashboard", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "get-grafana-dashboard-0", "content": "{}"},
        ]},
        # The offending Anthropic turn — appended verbatim as SDK block objects.
        {"role": "assistant", "content": [_thinking_sdk(), _server_use_sdk(ORPHAN_ID)]},
        {"role": "user", "content": (
            "Your previous response was truncated before producing any output. "
            "Please produce the final markdown summary now."
        )},
    ]


def test_block_fields_reads_sdk_objects():
    """The linchpin: `_block_fields` must extract type/id from an SDK block, not
    just a dict (the dict-only assumption was the bug)."""
    assert _block_fields(_server_use_sdk(ORPHAN_ID)) == (
        "server_tool_use", ORPHAN_ID, None,
    )
    assert _block_fields(_thinking_sdk()) == ("thinking", None, None)
    assert _block_fields({"type": "tool_result", "tool_use_id": "x"}) == (
        "tool_result", None, "x",
    )


def test_orphan_sdk_server_tool_use_is_stripped():
    """The exact per-round sanitize the failing round runs (preserve_trailing=True)
    must drop the SDK-object orphan so the request is protocol-valid."""
    msgs = _failing_run_messages()
    # Precondition: orphan present, as an SDK object, in a non-trailing turn.
    assert ORPHAN_ID in _orphan_server_use_ids(msgs)
    assert isinstance(msgs[3]["content"][1], BetaServerToolUseBlock)

    out = _strip_orphan_server_tools(msgs, preserve_trailing_server_use=True)

    assert _orphan_server_use_ids(out) == set(), "orphan server_tool_use survived the strip"
    # The truncated turn is collapsed to a valid placeholder — no dangling
    # thinking-only assistant turn left behind.
    offending = out[3]
    assert offending["role"] == "assistant"
    assert [_block_fields(b)[0] for b in offending["content"]] == ["text"]
    assert offending["content"][0]["text"] == "(continuing)"


def test_resume_from_serialized_replay_messages():
    """Resume rebuilds from `replay_messages` (SDK objects serialized to dicts).
    `_make_resume_safe` must also leave no orphan — this is the checkpoint the
    reaper would have replayed."""
    msgs = [
        {"role": "user", "content": "## Workflow Instructions ..."},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "reasoning...", "signature": "sig"},
            {"type": "server_tool_use", "id": ORPHAN_ID, "name": "bash_code_execution", "input": {"command": "x"}},
        ]},
        {"role": "user", "content": "nudge"},
    ]
    assert ORPHAN_ID in _orphan_server_use_ids(msgs)
    out = _make_resume_safe(msgs)
    assert _orphan_server_use_ids(out) == set()


def test_paired_sdk_server_tool_is_preserved():
    """A server_tool_use WITH its matching result (SDK use + dict result) is a
    valid pair and must survive the strip untouched."""
    pair_id = "srvtoolu-paired"
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [
            _thinking_sdk(),
            _server_use_sdk(pair_id),
            {"type": "bash_code_execution_tool_result", "tool_use_id": pair_id,
             "content": [{"type": "bash_code_execution_result", "stdout": "hi"}]},
            {"type": "text", "text": "done"},
        ]},
        {"role": "user", "content": [{"type": "text", "text": "next"}]},
    ]
    out = _strip_orphan_server_tools(msgs, preserve_trailing_server_use=True)
    kept_ids = [_block_fields(b)[1] for b in out[1]["content"]]
    assert pair_id in kept_ids, "paired server_tool_use was wrongly dropped"
    assert _orphan_server_use_ids(out) == set()


def test_trailing_pause_turn_sdk_use_is_exempt_only_when_requested():
    """A trailing (pause_turn) SDK server_tool_use with no result yet is exempt
    ONLY under preserve_trailing_server_use=True (server mid-execution); without
    it — the resume/escalation seed path — it is dropped."""
    pause_id = "srvtoolu-pause"
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [_thinking_sdk(), _server_use_sdk(pause_id)]},
    ]
    preserved = _strip_orphan_server_tools(msgs, preserve_trailing_server_use=True)
    assert pause_id in [_block_fields(b)[1] for b in preserved[1]["content"]]

    dropped = _strip_orphan_server_tools(msgs, preserve_trailing_server_use=False)
    assert _orphan_server_use_ids(dropped) == set()

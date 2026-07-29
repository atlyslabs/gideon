"""Cross-provider code execution via Anthropic's hosted sandbox.

When a non-Anthropic provider (Kimi, or OpenAI Chat Completions when not
using the Responses API) needs code execution, we expose `execute_python`
as a regular function tool. This module is the actual implementation: it
RPCs into Anthropic's hosted ``code_execution_20250825`` beta, runs the
code in a real sandboxed container, and returns the result.

Why this works:
  - Anthropic's container has a 20-minute idle lifetime. Caller is responsible
    for persisting ``container_id`` between calls within the same chat
    session (chat_service already stores ``conv.code_exec_container_id``).
  - File artifacts come back as Anthropic ``file_id``s — the caller can
    fetch them via ``client.beta.files.*`` to surface as images / Drive
    uploads in the chat UI.
  - The bridge uses Haiku as the carrier model — we only need the carrier
    to invoke the sandbox tool, not to reason. Haiku is the cheapest model
    that supports ``code_execution_20250825``.

This is intentionally a thin layer. It's not provider-detection — call sites
explicitly decide to use it (typically by including ``execute_python`` in the
tools list when the spec is Kimi-routed).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from config import CODE_EXECUTION_BETA, CODE_EXECUTION_TOOL, Models, get_anthropic_client

_logger = logging.getLogger(__name__)


EXECUTE_PYTHON_TOOL_SCHEMA = {
    "name": "execute_python",
    "description": (
        "Run Python code in a hosted sandbox and return its stdout / stderr / "
        "return_code. The sandbox has Python with pre-installed scientific "
        "libraries (numpy, pandas, matplotlib, scipy, sklearn). Each call runs "
        "as a fresh Python process — VARIABLES DO NOT PERSIST across calls. "
        "However FILES persist (the container's filesystem is preserved between "
        "calls in the same conversation), and pip-installed packages persist. "
        "If you need to share state across calls, write it to a file in /tmp "
        "and read it back. The container is isolated — no internet access, no "
        "access to the user's local files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python source code to execute. Use print() to surface output. The last expression's value is NOT auto-printed.",
            }
        },
        "required": ["code"],
        "additionalProperties": False,
    },
}


def _serialize_content_value(value: Any) -> Any:
    """Walk an Anthropic content object and produce a JSON-safe dict."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_serialize_content_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_content_value(v) for k, v in value.items()}
    # Pydantic model (Anthropic SDK)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            pass
    # Fallback — extract known fields off the object
    out: dict[str, Any] = {}
    for k in ("type", "stdout", "stderr", "return_code", "content", "file_id"):
        if hasattr(value, k):
            out[k] = _serialize_content_value(getattr(value, k))
    return out


_RESULT_BLOCK_TYPES = {
    # The 2025-08-25 server-side code_execution tool family. The model uses
    # text_editor to write/edit a file in the container, then bash to run it
    # (e.g. `python script.py`). Some plans return the simpler Python-only
    # `code_execution_tool_result` instead. We aggregate across all of them.
    "code_execution_tool_result",
    "bash_code_execution_tool_result",
    "text_editor_code_execution_tool_result",
}


def _extract_result_from_blocks(blocks: list[Any]) -> dict[str, Any]:
    """Walk the assistant content blocks for code_execution_tool_result-family
    blocks. Multiple may appear in one response (text_editor write, then bash
    run). We aggregate stdout/stderr across all bash results and pull file_ids
    from any block that has them."""
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    file_ids: list[str] = []
    return_code = 0
    error: Optional[str] = None
    saw_result_block = False

    for block in blocks:
        btype = getattr(block, "type", "")
        if btype not in _RESULT_BLOCK_TYPES:
            continue
        saw_result_block = True
        content = getattr(block, "content", None)
        if content is None:
            continue
        ctype = getattr(content, "type", "")
        if ctype.endswith("_error"):
            error = getattr(content, "error_code", None) or "unknown_error"
            continue

        stdout = getattr(content, "stdout", None)
        if stdout:
            stdout_parts.append(stdout)
        stderr = getattr(content, "stderr", None)
        if stderr:
            stderr_parts.append(stderr)
        rc = getattr(content, "return_code", None)
        if rc is not None and rc != 0:
            return_code = rc

        # Files emitted by the tool — usually under content.content[].file_id.
        # Some result types put them directly on content.
        for source in (getattr(content, "content", None) or [], [content]):
            if not isinstance(source, list):
                continue
            for item in source:
                fid = getattr(item, "file_id", None)
                if fid and fid not in file_ids:
                    file_ids.append(fid)

    return {
        "stdout": "".join(stdout_parts),
        "stderr": "".join(stderr_parts),
        "return_code": return_code,
        "file_ids": file_ids,
        "error": error,
        "_saw_result_block": saw_result_block,
    }


async def execute_python(
    code: str,
    *,
    container_id: Optional[str] = None,
    timeout: float = 150.0,
    carrier_model: str = Models.HAIKU,
) -> dict[str, Any]:
    """Run ``code`` in Anthropic's hosted Python sandbox.

    Args:
        code: Python source to execute.
        container_id: Reuse this Anthropic container so variables/files persist.
            None means a fresh container.
        timeout: HTTP timeout in seconds.
        carrier_model: Anthropic model that carries the tool invocation. Haiku
            is cheapest and sufficient — it only needs to relay the code.

    Returns:
        {
            "stdout": str,
            "stderr": str,
            "return_code": int,
            "container_id": str,  # save and pass back on next call for state continuity
            "container_expires_at": Optional[str],  # ISO timestamp
            "file_ids": list[str],  # Anthropic file_ids for any generated files
            "error": Optional[str],
        }
    """
    client = get_anthropic_client(timeout=timeout)

    # Force the carrier to call code_execution by giving an extremely terse
    # instruction. Haiku doesn't try to chat — it sees the code, calls the
    # tool. We delimit the code with unambiguous sentinel markers rather than
    # a markdown ``` fence: valid Python frequently contains ``` inside string
    # literals / docstrings, and a fenced prompt would let that inner fence
    # truncate the code the carrier sees. The sentinels below are effectively
    # never present in real source.
    user_content = (
        "Execute the Python code delimited by the markers below, verbatim, by "
        "calling the `code_execution` tool exactly once. The markers themselves "
        "are NOT part of the code — do not include them. Do not modify, "
        "explain, or add to the code.\n\n"
        "<<<GIDEON_CODE_START>>>\n"
        f"{code}\n"
        "<<<GIDEON_CODE_END>>>"
    )

    # Server-side code execution returns stop_reason="pause_turn" while the
    # sandbox is still running (heavy charts / PDF gen / long compute). A single
    # call then returns NO code_execution_tool_result block, which we used to
    # mis-report as "no_code_execution_result" — the exact intermittent failure
    # seen in prod where re-running the same code worked. We must replay the
    # paused assistant turn and continue until the server finishes. Bounded so a
    # pathological pause loop can't run forever.
    _MAX_PAUSE_CONTINUATIONS = 6

    async def _run_once(start_container: Optional[str]):
        """Drive one full carrier turn, following pause_turn continuations until
        the server-side tool completes. Returns (collected_blocks, container_id,
        container_expires_at). Each request applies `timeout` via with_options
        because get_anthropic_client caches the client (constructor timeout is
        honored only on the first-ever call)."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        container = start_container
        collected: list[Any] = []
        last_container_id = start_container or ""
        last_expires: Optional[str] = None
        for _ in range(_MAX_PAUSE_CONTINUATIONS + 1):
            create_kwargs: dict[str, Any] = {
                "model": carrier_model,
                "max_tokens": 8192,
                "tools": [CODE_EXECUTION_TOOL],
                "messages": messages,
            }
            if container:
                # The container param is a bare string id, not a dict — Anthropic
                # 400s on `{"id": "..."}` with "Input should be a valid string".
                create_kwargs["container"] = container
            resp = await client.with_options(timeout=timeout).beta.messages.create(
                betas=[CODE_EXECUTION_BETA], **create_kwargs
            )
            blocks = list(resp.content or [])
            collected.extend(blocks)
            container_obj = getattr(resp, "container", None)
            if container_obj is not None:
                last_container_id = getattr(container_obj, "id", "") or last_container_id
                exp = getattr(container_obj, "expires_at", None)
                last_expires = str(exp) if exp else last_expires
                container = last_container_id or container
            if getattr(resp, "stop_reason", None) != "pause_turn":
                break
            # Still running server-side: replay the paused assistant turn so the
            # API resumes and the result block lands on a subsequent response.
            messages.append({"role": "assistant", "content": blocks})
        return collected, last_container_id, last_expires

    def _is_stale_container_error(exc: Exception) -> bool:
        """True ONLY for errors that specifically mean the referenced container
        is gone (idle-expired / not found). We intentionally do NOT treat a
        generic 400, or the word "invalid", as stale: those can come from an
        unrelated request problem (bad params, quota), and retrying WITHOUT the
        container id would silently abandon a still-live sandbox's state. Narrow
        signals only."""
        msg = str(exc).lower()
        if "container" not in msg:
            return False
        return (
            "expired" in msg
            or "not found" in msg
            or "no longer exists" in msg
            or "does not exist" in msg
        )

    try:
        blocks, new_container_id, new_container_expires = await _run_once(container_id)
    except Exception as exc:  # noqa: BLE001
        # A resumed conversation can carry a container_id whose sandbox already
        # idle-timed-out (Anthropic containers expire after ~20 min). Retry once
        # WITHOUT the stale id so the call starts a fresh container instead of
        # hard-failing the whole tool call.
        if container_id and _is_stale_container_error(exc):
            _logger.warning(
                "code_execution bridge: stale container %s, retrying fresh (%s)",
                container_id, str(exc)[:160],
            )
            try:
                blocks, new_container_id, new_container_expires = await _run_once(None)
                container_id = None
            except Exception as exc2:  # noqa: BLE001
                _logger.exception("code_execution bridge: retry without container failed")
                return _bridge_error(f"bridge_call_failed_after_retry: {exc2}", container_id="")
        else:
            _logger.exception("code_execution bridge: anthropic call failed")
            return _bridge_error(f"bridge_call_failed: {exc}", container_id=container_id or "")

    extracted = _extract_result_from_blocks(blocks)
    saw_result = extracted.pop("_saw_result_block", False)

    # No result block despite finishing the turn: the carrier replied with plain
    # text instead of running the tool (intermittent, non-deterministic). Retry on
    # the SAME container so any state from earlier in the run persists. These
    # no-result replies come back fast (the carrier paraphrased rather than running
    # code), so a small bounded retry is cheap and recovers most of them — a single
    # retry was leaving too many turns with no_code_execution_result under load.
    _NO_RESULT_RETRIES = 2
    attempt = 0
    while not saw_result and not extracted.get("error") and attempt < _NO_RESULT_RETRIES:
        attempt += 1
        _logger.warning(
            "code_execution bridge: no result block, retry %d/%d", attempt, _NO_RESULT_RETRIES
        )
        try:
            blocks2, nc2, ne2 = await _run_once(new_container_id or container_id)
            extracted2 = _extract_result_from_blocks(blocks2)
            if extracted2.pop("_saw_result_block", False):
                extracted = extracted2
                saw_result = True
                new_container_id = nc2 or new_container_id
                new_container_expires = ne2 or new_container_expires
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "code_execution bridge: no-result retry %d failed: %s", attempt, str(exc)[:160]
            )
            break

    # Still nothing after the retry — surface a real failure so the caller/model
    # knows the code did not run (rather than a misleading return_code=0).
    if not saw_result and not extracted.get("error"):
        extracted["error"] = "no_code_execution_result"
        extracted["return_code"] = -1

    extracted["container_id"] = new_container_id or (container_id or "")
    extracted["container_expires_at"] = new_container_expires
    return extracted


def _bridge_error(message: str, *, container_id: str) -> dict[str, Any]:
    return {
        "stdout": "",
        "stderr": "",
        "return_code": -1,
        "container_id": container_id,
        "container_expires_at": None,
        "file_ids": [],
        "error": message,
    }

"""Read Cursor CLI's final response from its native sessionEnd transcript.

The 2026.09.08 CLI --print path emits sessionEnd, not afterAgentResponse.
Only the explicit native transcript's bounded tail is read; no transcript is
searched for and no tool output, earlier turn or reasoning is summarized.
"""
from __future__ import annotations

import json
from pathlib import Path

MAX_TAIL_BYTES = 1_048_576


def summary_result(payload: dict) -> tuple[dict, dict]:
    facts = {"status": "no_summary", "reason": "native-final-text-unavailable"}
    if isinstance(payload.get("transcript_path"), str):
        facts["transcript_path"] = payload["transcript_path"]
    if payload.get("hook_event_name") == "afterAgentResponse" and payload.get("text"):
        return payload, {**facts, "status": "ready", "reason": "native-response-event"}
    if ("hookEventName" in payload or payload.get("hook_event_name") != "sessionEnd"
            or payload.get("final_status") != "completed"
            or not isinstance(payload.get("conversation_id"), str) or not payload["conversation_id"]
            or not isinstance(payload.get("transcript_path"), str) or not payload["transcript_path"]):
        return payload, facts
    try:
        with Path(payload["transcript_path"]).open("rb") as stream:
            offset = max(0, stream.seek(0, 2) - MAX_TAIL_BYTES)
            stream.seek(offset)
            lines = stream.read(MAX_TAIL_BYTES).splitlines()
        if offset:
            lines = lines[1:]  # The first row may begin before the bounded tail.
        rows = iter(line for line in reversed(lines) if line.strip())
        last = next(rows, None)
        if last is None:
            return payload, facts
        row = json.loads(last)
        # Headless writes this terminal marker after its final assistant row,
        # before awaiting sessionEnd. Do not walk past another turn boundary.
        if row.get("type") == "turn_ended":
            if row.get("status") != "success":
                return payload, facts
            previous = next(rows, None)
            if previous is None:
                return payload, facts
            row = json.loads(previous)
        if row.get("role") != "assistant":
            return payload, facts
        content = row.get("message", {}).get("content", [])
        if not isinstance(content, list) or any(item.get("type") == "tool_use" for item in content):
            return payload, facts
        text = "\n".join(item["text"] for item in content
                         if item.get("type") == "text" and isinstance(item.get("text"), str)).strip()
        if text:
            return ({**payload, "hook_event_name": "afterAgentResponse", "text": text},
                    {**facts, "status": "ready", "reason": "native-final-text"})
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        # Optional capture never interrupts exit or falls back to an older turn.
        facts["error"] = f"{type(exc).__name__}: {exc}"
    return payload, facts


def summary_payload(payload: dict) -> dict:
    return summary_result(payload)[0]

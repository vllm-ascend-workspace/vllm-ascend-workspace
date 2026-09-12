"""Read one official Kimi 0.42 main-agent final step, never a session export.

Kimi's Stop payload has cwd/session_id but no response text. Its wire 1.5
schema records step.end before invoking Stop and flushes queued records in a
microtask. Read only this session's bounded tail; unknown schemas are a miss.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

TAIL_BYTES = 1_048_576


class FinalUnavailable(ValueError):
    pass


def journal_path(payload: dict, *, home: Path | None = None) -> Path | None:
    session = payload.get("session_id")
    cwd = payload.get("cwd")
    if (payload.get("hook_event_name") != "Stop" or payload.get("agent_id", "main") != "main"
            or not isinstance(session, str) or not re.fullmatch(r"[\w-]+", session)
            or not isinstance(cwd, str) or not Path(cwd).is_absolute()):
        return None
    # Official encodeWorkDirKey; do not search other workspaces or sessions.
    normalized = cwd.replace("\\", "/").rstrip("/")
    slug = re.sub(r"[^a-z0-9._-]+", "-", normalized.rsplit("/", 1)[-1].lower()).strip("-")[:40].strip("-")
    if slug in {"", ".", ".."}:
        slug = "workspace"
    key = "wd_" + slug + "_" + hashlib.sha256(normalized.encode()).hexdigest()[:12]
    home = home or Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))).expanduser()
    return home / "sessions" / key / session / "agents/main/wire.jsonl"


def _final_step(path: Path) -> tuple[str, str] | None:
    with path.open("rb") as stream:
        header = json.loads(stream.readline(4096))
        if header.get("type") != "metadata" or header.get("protocol_version") != "1.5":
            raise FinalUnavailable("unsupported-wire-version")
        size = stream.seek(0, 2)
        offset = max(0, size - TAIL_BYTES)
        stream.seek(offset)
        data = stream.read(TAIL_BYTES)
    if not data.endswith(b"\n"):
        return None  # the writer has not finished its current record
    lines = data.splitlines()
    if offset:
        lines = lines[1:]  # the leading line may be truncated
    step = None
    texts = []
    turn = ""
    for raw in reversed(lines):
        record = json.loads(raw)
        if record.get("type") == "context.append_message" and step is None:
            raise FinalUnavailable("new-prompt-after-final")
        if record.get("type") != "context.append_loop_event":
            continue
        event = record.get("event", {})
        kind = event.get("type")
        if step is None:
            if kind != "step.end" or event.get("finishReason") != "end_turn":
                return None
            step, turn = event.get("uuid"), event.get("turnId", "")
            if not isinstance(step, str):
                raise FinalUnavailable("unsupported-step-shape")
        elif kind == "step.begin":
            if event.get("uuid") != step:
                raise FinalUnavailable("final-step-boundary-mismatch")
            return "".join(reversed(texts)), str(turn)
        elif kind == "content.part" and event.get("stepUuid") == step:
            part = event.get("part", {})
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
        else:
            raise FinalUnavailable("non-final-step-content")
    raise FinalUnavailable("final-step-exceeds-tail-limit")


def summary_payload(payload: dict, *, home: Path | None = None) -> dict:
    return summary_result(payload, home=home)[0]


def summary_result(payload: dict, *, home: Path | None = None) -> tuple[dict, dict]:
    path = journal_path(payload, home=home)
    if path is None:
        return payload, {"status": "no_summary", "reason": "missing-native-main-session-facts"}
    for attempt in range(3):
        try:
            final = _final_step(path)
        except FinalUnavailable as exc:
            return payload, {"status": "no_summary", "reason": str(exc)}
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            return payload, {"status": "no_summary", "reason": "native-journal-unavailable",
                             "error_type": type(exc).__name__}
        if final is not None:
            text, turn = final
            return ({**payload, "last_assistant_message": text, "turn_id": turn},
                    {"status": "read", "turn_id": turn, "text_length": len(text)})
        if attempt < 2:
            time.sleep(0.025)  # allow only the already-queued native flush
    return payload, {"status": "no_summary", "reason": "native-final-step-not-complete"}

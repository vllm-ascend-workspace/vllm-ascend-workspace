"""Cursor --print supplies a final transcript path when sessionEnd completes."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
import vaws_cursor_summary as cursor


def assistant(text, *more):
    return {"role": "assistant", "message": {"content": [{"type": "text", "text": text}, *more]}}


@pytest.fixture
def native(tmp_path):
    transcript = tmp_path / "native transcript.jsonl"
    payload = {"hook_event_name": "sessionEnd", "conversation_id": "actual-native-id",
               "generation_id": "actual-native-id", "session_id": "actual-native-id",
               "cursor_version": "2026.09.08-6caf4ff", "workspace_roots": [str(tmp_path)],
               "reason": "completed", "final_status": "completed",
               "transcript_path": str(transcript)}
    return transcript, payload


def write_rows(path, *rows):
    rows = (*rows, {"type": "turn_ended", "status": "success"})
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_official_completed_payload_keeps_native_identity_and_only_final_text(native):
    transcript, payload = native
    final = "任务完成：仅在选定 worktree 写入说明，知识查询成功，保留固定环境。"
    write_rows(transcript, assistant("earlier commentary"),
               {"role": "user", "message": {"content": [{"type": "tool_result", "content": "raw evidence"}]}},
               assistant(final, {"type": "thinking", "thinking": "not a summary"}))
    normalized = cursor.summary_payload(payload)
    assert normalized == {**payload, "hook_event_name": "afterAgentResponse", "text": final}
    assert payload["hook_event_name"] == "sessionEnd"


def test_large_history_reads_only_complete_tail_row(native, monkeypatch):
    transcript, payload = native
    monkeypatch.setattr(cursor, "MAX_TAIL_BYTES", 256)
    final = "Current task final response exceeds the minimum capture length."
    write_rows(transcript, assistant("old history " * 1000), assistant(final))
    assert cursor.summary_payload(payload)["text"] == final


@pytest.mark.parametrize("last", [
    {"role": "user", "message": {"content": [{"type": "text", "text": "new prompt"}]}},
    assistant("still running", {"type": "tool_use", "name": "Shell", "input": {"command": "pwd"}}),
    assistant("", {"type": "thinking", "thinking": "not final"}),
])
def test_no_final_response_does_not_capture_previous_turn(native, last):
    transcript, payload = native
    write_rows(transcript, assistant("old completed turn must never become this task's final summary"), last)
    assert cursor.summary_payload(payload) is payload


@pytest.mark.parametrize("changes", [
    {"final_status": "aborted"}, {"final_status": "error"}, {"final_status": None},
    {"hook_event_name": "afterAgentResponse"}, {"hookEventName": "stop"},
    {"conversation_id": ""}, {"transcript_path": None},
])
def test_other_events_and_missing_native_facts_remain_untouched(native, changes):
    transcript, payload = native
    write_rows(transcript, assistant("valid final response that belongs to a different event"))
    payload.update(changes)
    assert cursor.summary_payload(payload) is payload


def test_incomplete_tail_and_missing_file_are_noops(native):
    transcript, payload = native
    assert cursor.summary_payload(payload) is payload
    _, facts = cursor.summary_result(payload)
    assert facts["reason"] == "native-final-text-unavailable"
    assert facts["transcript_path"] == str(transcript)
    assert "FileNotFoundError" in facts["error"]
    write_rows(transcript, assistant("previous completed answer should not mask the partial newest row"))
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write('{"role":"assistant","message":')
    assert cursor.summary_payload(payload) is payload


def test_failed_terminal_marker_does_not_reuse_an_older_success(native):
    transcript, payload = native
    write_rows(transcript, assistant("previous complete answer is not evidence of a successful current turn"))
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "turn_ended", "status": "aborted"}) + "\n")
    assert cursor.summary_payload(payload) is payload


def test_observable_result_reports_source_without_logging_response(native):
    transcript, payload = native
    text = "Keep this full response in the capture owner, not in diagnostics."
    write_rows(transcript, assistant(text))
    normalized, facts = cursor.summary_result(payload)
    assert normalized["text"] == text
    assert facts == {"status": "ready", "reason": "native-final-text", "transcript_path": str(transcript)}

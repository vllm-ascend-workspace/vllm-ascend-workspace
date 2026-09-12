"""Official Kimi final-step adaptation is bounded to the supplied session."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import vaws_kimi_summary as summary
from test_knowledge_summary_hook import hook, invoke
from test_kimi_native_setup import client_setup, git, repo
from client_setup_fixtures import selected_runtime
import vaws_kimi_config


def event(kind, **values):
    return {"type": "context.append_loop_event", "event": {"type": kind, **values}}


def final_step(text="The ordinary completed reply is reused without another summary."):
    return [event("step.begin", uuid="step", turnId="3"),
            event("content.part", stepUuid="step", part={"type": "think", "text": "private thought"}),
            event("content.part", stepUuid="step", part={"type": "text", "text": text}),
            event("content.part", stepUuid="step", part={"type": "text", "text": " Done."}),
            event("step.end", uuid="step", turnId="3", finishReason="end_turn")]


def write_wire(home, payload, records, version="1.5"):
    path = summary.journal_path(payload, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(value) for value in [
        {"type": "metadata", "protocol_version": version}, *records]) + "\n")
    return path


@pytest.fixture
def payload(tmp_path):
    return {"hook_event_name": "Stop", "cwd": str(tmp_path / "project 用户"), "session_id": "known-session"}


def test_only_final_step_text_from_exact_session_is_captured(tmp_path, payload):
    records = [{"type": "unrelated", "padding": "x" * (summary.TAIL_BYTES + 100)}, *final_step()]
    path = write_wire(tmp_path, payload, records)
    # Unrelated content is not opened, even if its session has a later mtime.
    other = write_wire(tmp_path, {**payload, "session_id": "other"}, final_step("Unrelated response"))
    with patch.object(Path, "glob", side_effect=AssertionError("session discovery")), \
         patch.object(Path, "rglob", side_effect=AssertionError("session discovery")):
        result = summary.summary_payload(payload, home=tmp_path)
    assert result["last_assistant_message"] == final_step()[2]["event"]["part"]["text"] + " Done."
    assert result["turn_id"] == "3"
    assert "private thought" not in result["last_assistant_message"]
    assert path != other


@pytest.mark.parametrize("suffix", [
    [event("step.begin", uuid="new")],
    [{"type": "context.append_message", "message": {"role": "user", "content": []}}],
    [event("step.end", uuid="new", finishReason="tool_use")],
    [event("step.end", uuid="new", finishReason="error")],
])
def test_incomplete_tool_or_new_prompt_does_not_reuse_old_final(tmp_path, payload, suffix):
    write_wire(tmp_path, payload, final_step() + suffix)
    assert summary.summary_payload(payload, home=tmp_path) == payload


def test_unknown_schema_and_oversized_final_are_skipped(tmp_path, payload):
    write_wire(tmp_path, payload, final_step(), version="2.0")
    assert summary.summary_result(payload, home=tmp_path) == (payload, {
        "status": "no_summary", "reason": "unsupported-wire-version"})
    write_wire(tmp_path, payload, final_step("x" * summary.TAIL_BYTES))
    assert summary.summary_result(payload, home=tmp_path) == (payload, {
        "status": "no_summary", "reason": "final-step-exceeds-tail-limit"})


def test_flush_retry_is_short_and_bounded(tmp_path, payload):
    path = write_wire(tmp_path, payload, final_step()[:-1])
    sleeps = []
    def flush(delay):
        sleeps.append(delay)
        with path.open("a") as stream:
            stream.write(json.dumps(final_step()[-1]) + "\n")
    with patch.object(summary.time, "sleep", side_effect=flush):
        assert "last_assistant_message" in summary.summary_payload(payload, home=tmp_path)
    assert sleeps == [0.025]


def test_subagent_and_unknown_session_never_fall_back_to_another_file(tmp_path, payload):
    write_wire(tmp_path, payload, final_step())
    for changes in ({"agent_id": "child"}, {"session_id": "../known-session"},
                    {"session_id": "missing"}, {"hook_event_name": "UserPromptSubmit"}):
        value = {**payload, **changes}
        assert summary.summary_payload(value, home=tmp_path) == value


def test_hook_uses_adapter_and_same_family_shared_configuration(tmp_path, monkeypatch):
    project = repo(tmp_path / "project")
    linked = tmp_path / "linked"
    git(project, "worktree", "add", "--detach", str(linked), "HEAD")
    payload = {"hook_event_name": "Stop", "cwd": str(linked), "session_id": "known"}
    write_wire(tmp_path, payload, final_step())
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path))
    with patch.object(hook, "ensure_workspace_interpreter"), \
         patch("vaws_knowledge_service.service_config", return_value="shared") as config, \
         patch("vaws_knowledge.summary_hook.capture_summary") as capture:
        invoke(payload, client="kimi", project=project)
    config.assert_called_once_with(project)
    assert capture.call_args.kwargs == {"config": "shared", "client": "kimi"}
    assert capture.call_args.args[0]["last_assistant_message"].endswith(" Done.")


def test_setup_installs_stop_as_summary_command_and_is_idempotent(tmp_path, monkeypatch):
    import tomllib
    receipt = selected_runtime(monkeypatch, client_setup, tmp_path)
    monkeypatch.setattr(vaws_kimi_config, "managed_receipt", lambda _: receipt)
    project = repo(tmp_path / "project")
    config = tmp_path / "config.toml"
    config.write_text('[[hooks]]\nevent = "Stop"\ncommand = "user-stop"\n')
    plan = client_setup.build_plan("kimi", project, kimi_config=config)
    providers = json.loads(plan["files"][config.parent / "mcp.json"])["mcpServers"]
    providers.update(json.loads(plan["files"][project / ".kimi-code/mcp.json"])["mcpServers"])
    assert len(providers) == 3
    assert all(server["env"]["VAWS_MCP_CLIENT"] == "kimi" for server in providers.values())
    hooks = tomllib.loads(plan["files"][config])["hooks"]
    stops = [entry for entry in hooks if entry["event"] == "Stop"]
    assert len(stops) == 2
    assert stops[0]["command"] == "user-stop"
    arguments = client_setup.hook_argv(stops[1]["command"])
    assert any(Path(argument).name == "knowledge_summary.py" for argument in arguments)
    assert all(Path(argument).name != "vaws_session.py" for argument in arguments)
    assert all(entry["event"] != "SessionSetup" for entry in hooks)
    config.write_text(plan["files"][config])
    repeated = client_setup.build_plan("kimi", project, kimi_config=config)
    assert repeated["files"][config] == plan["files"][config]

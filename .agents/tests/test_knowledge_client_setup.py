"""Knowledge access and available native summary events across the five clients."""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from client_setup_fixtures import selected_runtime

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("knowledge_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
setup = importlib.util.module_from_spec(spec)
with patch("vaws_venv.ensure_workspace_interpreter"):
    spec.loader.exec_module(setup)


@pytest.fixture(autouse=True)
def selected_environment(monkeypatch, tmp_path):
    return selected_runtime(monkeypatch, setup, tmp_path)

HOOK_FILES = {
    "codex": ".codex/hooks.json",
    "claude": ".claude/settings.local.json",
    "cursor": ".cursor/hooks.json",
    "grok": ".grok/hooks/vaws-session.json",
}


@pytest.mark.parametrize("client", ["codex", "claude", "cursor", "grok", "kimi"])
def test_all_clients_receive_knowledge_access_and_only_supported_summary_events(client, tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", tmp_path / ".agents/hooks/vaws_session.py")
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "kimi-home"))
    plan = setup.build_plan(client, tmp_path, kimi_config=tmp_path / "kimi-config.toml")
    assert plan["mcp_servers"]["vaws-knowledge"] == [str(tmp_path / ".agents/scripts/vaws_native_mcp.py"), "knowledge"]
    if client == "kimi":
        hooks = setup.tomllib.loads(plan["files"][tmp_path / "kimi-config.toml"])["hooks"]
        assert "SessionStart" in {entry["event"] for entry in hooks}
        stop = next(entry for entry in hooks if entry["event"] == "Stop")
        assert any(Path(argument).name == "knowledge_summary.py"
                   for argument in setup.hook_argv(stop["command"]))
        return
    payload = json.loads(plan["files"][tmp_path / HOOK_FILES[client]])
    event = "afterAgentResponse" if client == "cursor" else "Stop"
    group = payload["hooks"][event][0]
    command = group["command"] if client == "cursor" else group["hooks"][0]["command"]
    if client == "cursor":
        ended = payload["hooks"]["sessionEnd"]
        assert len(ended) == 2
        assert any(Path(argument).name == "vaws_session.py"
                   for entry in ended for argument in setup.hook_argv(entry["command"]))
        assert sum(entry["command"] == command for entry in ended) == 1
    arguments = setup.hook_argv(command)
    if client == "claude":
        assert arguments[1:3] == [str(tmp_path / ".agents/scripts/vaws_claude_entry.py"), "summary"]
        return  # The exec entry supplies the actual native cwd and saved environment.
    assert Path(arguments[1]).name == "knowledge_summary.py"
    assert arguments[arguments.index("--client") + 1] == client
    assert arguments[arguments.index("--project") + 1] == str(tmp_path.resolve())


def test_grok_summary_preserves_foreign_stop_and_existing_session_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", tmp_path / ".agents/hooks/vaws_session.py")
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    path = tmp_path / HOOK_FILES["grok"]
    path.parent.mkdir(parents=True)
    first = setup.configuration("grok", tmp_path)[path]
    payload = json.loads(first)
    session = payload["hooks"]["SessionStart"]
    payload["hooks"]["Stop"].insert(0, {"hooks": [{"type": "command", "command": "foreign-stop"}]})
    path.write_text(json.dumps(payload), encoding="utf-8")
    second = setup.configuration("grok", tmp_path)[path]
    path.write_text(second, encoding="utf-8")
    assert second == setup.configuration("grok", tmp_path)[path]
    updated = json.loads(second)["hooks"]
    assert updated["SessionStart"] == session
    commands = [entry["command"] for group in updated["Stop"] for entry in group["hooks"]]
    assert commands[0] == "foreign-stop"
    assert len(commands) == 2


@pytest.mark.parametrize("existing_summary", [False, True])
def test_cursor_existing_session_end_adds_or_updates_summary(tmp_path, monkeypatch, existing_summary):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", tmp_path / ".agents/hooks/vaws_session.py")
    path = tmp_path / HOOK_FILES["cursor"]
    desired = json.loads(setup.configuration("cursor", tmp_path)[path])
    session, summary = desired["hooks"]["sessionEnd"]
    foreign = {"command": "foreign-session-end", "timeout": 23}
    existing = [{**session, "timeout": 19}, foreign]
    if existing_summary:
        arguments = setup.hook_argv(summary["command"])
        arguments[0] = str(tmp_path / ".vaws-local/env-links" / ("a" * 64) / "bin/python")
        existing.append({"command": setup.local_hook_command(arguments), "timeout": 31})
    desired["hooks"]["sessionEnd"] = existing
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(desired), encoding="utf-8")

    updated = setup.configuration("cursor", tmp_path)[path]
    ended = json.loads(updated)["hooks"]["sessionEnd"]
    assert ended[:2] == [{**session, "timeout": 19}, foreign]
    assert len(ended) == 3
    assert ended[2] == ({**summary, "timeout": 31} if existing_summary else summary)
    path.write_text(updated, encoding="utf-8")
    assert setup.configuration("cursor", tmp_path)[path] == updated


@pytest.mark.parametrize("client", ["claude", "cursor", "codex", "grok"])
def test_generated_knowledge_owner_and_paths_migrate_without_changing_custom_values(client, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_relative = ".vaws-local/env-links/" + "a" * 64 + "/bin/python"
    old_python = workspace / old_relative
    old_python.parent.mkdir(parents=True)
    old_python.touch()
    monkeypatch.setattr(setup, "ROOT", workspace)
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    desired = {
        "command": str(workspace / ".vaws-local/env-links" / ("b" * 64) / "Scripts/python.exe"),
        "args": setup.knowledge_server_args(),
        "env": {"VAWS_KNOWLEDGE_PROJECT_ROOTS": str(workspace / ".agents/knowledge"),
                "VAWS_KNOWLEDGE_CANDIDATE_ROOT": str(workspace / ".vaws-local/knowledge/candidate")},
    }
    monkeypatch.setattr(setup, "desired_mcp_servers", lambda **kwargs: {"vaws-knowledge": desired})
    existing = {"command": "./" + old_relative, "args": setup.knowledge_server_args(),
                "env": {"VAWS_KNOWLEDGE_PROJECT_ROOTS": ".agents/knowledge", "CUSTOM": "keep"},
                "enabled_tools": ["knowledge_query"]}
    if client in {"claude", "cursor"}:
        path = workspace / (".mcp.json" if client == "claude" else ".cursor/mcp.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": {"vaws_knowledge": existing, "other": {"command": "untouched"}}}), encoding="utf-8")
    else:
        path = workspace / ("." + client) / "config.toml"
        path.parent.mkdir()
        path.write_text("# existing settings\n" + setup.toml_server_body("vaws_knowledge", existing).replace(
            "\n[mcp_servers.vaws_knowledge.env]", '\nenabled_tools = ["knowledge_query"]\n[mcp_servers.vaws_knowledge.env]')
            + "\n[mcp_servers.other]\ncommand = 'untouched'\n", encoding="utf-8")
    plan = setup.build_plan(client, workspace)
    text = plan["files"][path]
    servers = json.loads(text)["mcpServers"] if client in {"claude", "cursor"} else setup.tomllib.loads(text)["mcp_servers"]
    assert servers["vaws_knowledge"]["command"] == desired["command"]
    assert servers["vaws_knowledge"]["env"] == {**desired["env"], "CUSTOM": "keep"}
    assert servers["vaws_knowledge"]["enabled_tools"] == ["knowledge_query"]
    assert servers["other"] == {"command": "untouched"}
    for output, content in plan["files"].items():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    repeated = setup.build_plan(client, workspace)
    assert repeated["files"].get(path, text) == text


def test_custom_knowledge_provider_and_storage_are_preserved(tmp_path):
    existing = {"command": "custom-provider", "args": setup.knowledge_server_args(),
                "env": {"VAWS_KNOWLEDGE_CANDIDATE_ROOT": "/custom/candidate"}}
    desired = {"command": str(tmp_path / ".vaws-local/env-links" / ("b" * 64) / "Scripts/python.exe"),
               "args": setup.knowledge_server_args(), "env": {"VAWS_KNOWLEDGE_CANDIDATE_ROOT": str(tmp_path / "candidate")}}
    merged, action = setup.merge_server_entry(existing, desired, checkout=tmp_path)
    assert action == "preserved"
    assert merged == existing


@pytest.mark.parametrize("format", ["json", "toml"])
@pytest.mark.parametrize("custom_candidate", [False, True])
def test_service_config_replaces_only_generated_default_root_environment(format, custom_candidate, tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    existing = {
        "command": str(tmp_path / ".vaws-local/env-links" / ("a" * 64) / "bin/python"),
        "args": setup.knowledge_server_args(),
        "env": {
            "VAWS_KNOWLEDGE_PROJECT_ROOTS": ".agents/knowledge",
            "VAWS_KNOWLEDGE_CANDIDATE_ROOT": "/custom/candidate" if custom_candidate else str(tmp_path / ".vaws-local/knowledge/candidate"),
            "VAWS_KNOWLEDGE_STATE": str(tmp_path / ".vaws-local/knowledge/instance"),
            "CUSTOM": "keep",
        },
    }
    desired = {
        "command": str(tmp_path / ".vaws-local/env-links" / ("b" * 64) / "Scripts/python.exe"),
        "args": setup.knowledge_server_args(),
        "env": {"VAWS_KNOWLEDGE_CONFIG": str(tmp_path / ".vaws-local/knowledge/service.json")},
    }
    if format == "json":
        updated = setup.merge_server_entry(existing, desired, checkout=tmp_path)[0]
    else:
        text = setup.toml_server_body("vaws_knowledge", existing)
        updated = setup.tomllib.loads(setup.fill_toml_server_env(text, "vaws_knowledge", existing, desired, checkout=tmp_path))["mcp_servers"]["vaws_knowledge"]
    environment = updated["env"]
    assert environment["VAWS_KNOWLEDGE_CONFIG"] == desired["env"]["VAWS_KNOWLEDGE_CONFIG"]
    assert "VAWS_KNOWLEDGE_PROJECT_ROOTS" not in environment
    assert "VAWS_KNOWLEDGE_STATE" not in environment
    assert ("VAWS_KNOWLEDGE_CANDIDATE_ROOT" in environment) is custom_candidate
    if custom_candidate:
        assert environment["VAWS_KNOWLEDGE_CANDIDATE_ROOT"] == "/custom/candidate"
    assert environment["CUSTOM"] == "keep"

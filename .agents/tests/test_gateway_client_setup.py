"""Stable provider launchers and their project guidance across stock clients."""
import json
import re
from pathlib import Path
import sys
import tomllib

import pytest

from test_knowledge_client_setup import setup
from client_setup_fixtures import selected_runtime


@pytest.fixture
def configured_project(tmp_path, monkeypatch):
    project = tmp_path / "project 用户"
    project.mkdir()
    user_dir = tmp_path / "user"
    user_dir.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_dir))
    monkeypatch.setenv("KIMI_CODE_HOME", str(user_dir / ".kimi-code"))
    monkeypatch.setattr(setup, "ROOT", project)
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", project / ".agents/hooks/vaws_session.py")
    receipt = selected_runtime(monkeypatch, setup, tmp_path)
    monkeypatch.setattr(setup, "read_receipt", lambda _: receipt)
    return project, user_dir, receipt


@pytest.mark.parametrize("client", ["codex", "cursor", "claude", "grok", "kimi"])
def test_every_provider_uses_stable_gateway_and_stock_startup(client, configured_project):
    project, user_dir, _ = configured_project
    plan = setup.build_plan(client, project, cursor_global_mcp=client == "cursor")
    if client in {"grok", "codex"}:
        providers = tomllib.loads(plan["files"][project / ("." + client) / "config.toml"])["mcp_servers"]
    else:
        path = {"claude": project / ".mcp.json", "cursor": user_dir / ".cursor/mcp.json",
                "kimi": user_dir / ".kimi-code/mcp.json"}[client]
        providers = json.loads(plan["files"][path])["mcpServers"]
    assert len(providers) == 3
    for server in providers.values():
        assert Path(server["args"][0]).name in {"vaws_native_mcp.py", "vaws_claude_entry.py"}
        assert server["args"][1] in {"task", "remote", "knowledge"}
    assert "vaws_start.py --client CLIENT" in plan["files"][project / "AGENTS.md"]
    assert "Resume keeps" in plan["files"][project / "AGENTS.md"]
    if client == "kimi":
        hooks = tomllib.loads(plan["files"][user_dir / ".kimi-code/config.toml"])["hooks"]
        assert all(hook["event"] != "SessionSetup" for hook in hooks)


@pytest.mark.parametrize("client", ["codex", "grok", "cursor"])
def test_owned_old_provider_moves_to_gateway_but_keeps_custom_fields(client, configured_project):
    project, _, receipt = configured_project
    old = {"command": sys.executable, "args": setup.task_server_args(),
           "env": {setup.PIN_ENV: receipt["receipt"], "CUSTOM": "retained"}}
    if client == "cursor":
        path = project / ".cursor/mcp.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"mcpServers": {"vaws-task": {**old, "custom": 7},
                                                   "other": {"command": "user-provider"}}}))
    else:
        path = project / ("." + client) / "config.toml"
        path.parent.mkdir()
        text = setup.toml_server_body("vaws_task", old).replace("\n[mcp_servers.vaws_task.env]",
                    '\nenabled_tools = ["vaws_run"]\n[mcp_servers.vaws_task.env]')
        path.write_text("# user comment\n" + text + '\n[mcp_servers.other]\ncommand = "user-provider"\n')
    plan = setup.build_plan(client, project, task_only=True)
    text = plan["files"][path]
    providers = json.loads(text)["mcpServers"] if client == "cursor" else tomllib.loads(text)["mcp_servers"]
    server = providers["vaws-task" if client == "cursor" else "vaws_task"]
    assert server["args"] == [str(project / ".agents/scripts/vaws_native_mcp.py"), "task"]
    assert server["env"]["CUSTOM"] == "retained"
    assert providers["other"] == {"command": "user-provider"}
    if client == "cursor":
        assert server["custom"] == 7
    else:
        assert server["enabled_tools"] == ["vaws_run"]
        assert text.startswith("# user comment\n")
    for output, content in plan["files"].items():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content.encode())
    repeated = setup.build_plan(client, project, task_only=True)
    assert repeated["files"].get(path, text) == text


def test_custom_same_named_launcher_is_not_replaced(configured_project):
    project, _, _ = configured_project
    custom = {"command": "user-python", "args": setup.task_server_args(), "custom": True}
    desired = setup.desired_mcp_servers(task_only=True)["vaws-task"]
    assert setup.merge_server_entry(custom, desired, checkout=project) == (custom, "preserved")


@pytest.mark.parametrize("client,name", [
    ("codex", "mcp__vaws_knowledge__knowledge_query"),
    ("grok", "remote_dev__remote_read"),
    ("claude", "mcp__vaws-knowledge__knowledge_capture"),
    ("cursor", "MCP:knowledge_explain"),
    ("cursor", "MCP:remote_read"),
])
def test_context_hook_covers_each_native_companion_name(client, name, configured_project):
    project, _, _ = configured_project
    groups = setup.hook_groups(client, project)
    matcher = groups["preToolUse" if client == "cursor" else "PreToolUse"][0]["matcher"]
    assert re.search(matcher, name)
    assert not re.search(matcher, "user_provider__query")
    if client != "cursor":
        assert not re.search(matcher, "MCP:knowledge_query")


LEGACY_TASK_MATCHER = r"(?:^|:|__)vaws_(session|run|execution|finish|message)$"


def old_context_hook(client, project, *, wrapped=False):
    event = "preToolUse" if client == "cursor" else "PreToolUse"
    group = setup.hook_groups(client, project)[event][0]
    group["matcher"] = LEGACY_TASK_MATCHER
    if wrapped:
        group["hooks"][0]["command"] = setup.local_hook_command([
            sys.executable, str(project / ".agents/scripts/vaws_claude_entry.py"),
            "session", "--agent-sessions-dir", str(project / ".vaws-local/agent-sessions")])
    return event, group


@pytest.mark.parametrize("client", ["claude", "cursor", "grok", "codex"])
def test_existing_generated_task_only_matcher_upgrades_companion_context(client, configured_project):
    project, _, _ = configured_project
    event, old = old_context_hook(client, project, wrapped=client == "claude")
    relative = {"claude": ".claude/settings.local.json", "cursor": ".cursor/hooks.json",
                "grok": ".grok/hooks/vaws-session.json", "codex": ".codex/hooks.json"}[client]
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {event: [old]}}))
    plan = setup.build_plan(client, project)
    groups = json.loads(plan["files"][path])["hooks"][event]
    assert len(groups) == 1
    assert re.search(groups[0]["matcher"], "mcp__vaws-knowledge__knowledge_query")
    assert re.search(groups[0]["matcher"], "remote_dev__remote_read")
    if client == "cursor":
        assert re.search(groups[0]["matcher"], "MCP:knowledge_query")
    for output, content in plan["files"].items():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content.encode())
    assert setup.build_plan(client, project)["files"][path] == plan["files"][path]


def test_claude_mixed_legacy_group_keeps_user_scope_when_owned_context_expands(configured_project):
    project, _, _ = configured_project
    event, old = old_context_hook("claude", project, wrapped=True)
    user = {"type": "command", "command": "user-audit-hook", "timeout": 37}
    old["hooks"].append(user)
    old["custom_metadata"] = "retained"
    path = project / ".claude/settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {event: [old]}}))
    plan = setup.build_plan("claude", project)
    groups = json.loads(plan["files"][path])["hooks"][event]
    assert groups[0] == {**old, "hooks": [user]}
    assert len(groups) == 2
    assert re.search(groups[1]["matcher"], "mcp__vaws-knowledge__knowledge_capture")
    assert len(groups[1]["hooks"]) == 1
    path.write_text(plan["files"][path])
    assert setup.build_plan("claude", project)["files"][path] == plan["files"][path]


def test_claude_custom_wrapper_scope_is_preserved(configured_project):
    project, _, _ = configured_project
    event, old = old_context_hook("claude", project, wrapped=True)
    old["matcher"] = "Bash"
    path = project / ".claude/settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {event: [old]}}))
    groups = json.loads(setup.build_plan("claude", project)["files"][path])["hooks"][event]
    assert len(groups) == 1
    assert groups[0]["matcher"] == "Bash"

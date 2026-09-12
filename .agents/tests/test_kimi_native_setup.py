"""Kimi's native SessionSetup adapter creates directories only for its project."""
import importlib.util
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
from unittest.mock import patch

import pytest

from client_setup_fixtures import selected_runtime
import vaws_kimi_config

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("kimi_setup", ROOT / ".agents/scripts/vaws_kimi_session_setup.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

config_spec = importlib.util.spec_from_file_location("kimi_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
client_setup = importlib.util.module_from_spec(config_spec)
with patch("vaws_venv.ensure_workspace_interpreter"):
    config_spec.loader.exec_module(client_setup)

provider_spec = importlib.util.spec_from_file_location("native_mcp", ROOT / ".agents/scripts/vaws_native_mcp.py")
provider = importlib.util.module_from_spec(provider_spec)
provider_spec.loader.exec_module(provider)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(path):
    path.mkdir()
    git(path, "init", "-b", "main")
    (path / ".gitignore").write_text(".vaws-local/\n")
    (path / "README.md").write_text("native session fixture\n")
    git(path, "add", ".")
    git(path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    return path


def test_creates_linked_worktrees_for_native_ids_and_keeps_existing_edits(tmp_path, monkeypatch):
    project = repo(tmp_path / "project 用户")
    monkeypatch.setattr(adapter, "prepare_worktree", lambda *_: {"status": "ready"})
    first = Path(adapter.setup(project, project, {"session_id": "session-first"})["hookSpecificOutput"]["cwd"])
    second = Path(adapter.setup(project, project, {"session_id": "session-second"})["hookSpecificOutput"]["cwd"])
    assert first != second != project
    assert git(first, "rev-parse", "HEAD") == git(project, "rev-parse", "HEAD")
    assert adapter.scoped_source(project, first) == first
    (first / "task.txt").write_text("unfinished work")
    repeated = adapter.setup(project, project, {"session_id": "session-first"})
    assert Path(repeated["hookSpecificOutput"]["cwd"]) == first
    assert (first / "task.txt").read_text() == "unfinished work"
    assert git(project, "status", "--porcelain") == ""


def test_unrelated_nested_repository_does_not_inherit_setup(tmp_path):
    project = repo(tmp_path / "project")
    nested = repo(project / "unrelated")
    assert adapter.scoped_source(project, nested) is None
    assert adapter.scoped_source(project, project) == project


def test_resume_routes_existing_receipt_without_preparing(tmp_path, monkeypatch):
    project = repo(tmp_path / "project")
    calls = []
    monkeypatch.setattr(adapter, "saved_ready", lambda _: {"python": sys.executable, "receipt": "old-receipt"})
    monkeypatch.setattr(adapter, "task_env", lambda *_: {})
    monkeypatch.setattr(adapter.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or type("Result", (), {"returncode": 0})())
    payload = {"hook_event_name": "SessionStart", "source": "resume", "session_id": "known-session", "cwd": str(project)}
    assert adapter.forward(project, payload) == 0
    assert calls[0][1]["env"][adapter.PIN_ENV] == "old-receipt"
    assert json.loads(calls[0][1]["input"]) == payload
    assert "--environment-receipt" in calls[0][0]


def test_native_extension_requires_explicit_opt_in_on_each_configuration_run(tmp_path, monkeypatch):
    receipt = selected_runtime(monkeypatch, client_setup, tmp_path)
    monkeypatch.setattr(vaws_kimi_config, "managed_receipt", lambda root: receipt)
    project = repo(tmp_path / "extended")
    other = repo(tmp_path / "official")
    config = tmp_path / "config.toml"
    config.write_text('[provider]\nname = "kept"\n')
    ordinary = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)
    assert 'event = "SessionSetup"' not in ordinary["files"][config]
    extended = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True,
                                       kimi_session_setup=True)
    text = extended["files"][config]
    hooks = client_setup.tomllib.loads(text)["hooks"]
    assert hooks[0]["event"] == "SessionSetup"
    assert hooks[0]["timeout"] <= 600
    expected = ["uv", "run", "--no-project", "python",
                str(ROOT / ".agents/scripts/vaws_kimi_session_setup.py"), "--project", str(project)]
    assert all(client_setup.hook_argv(hook["command"]) == expected for hook in hooks)
    config.write_text(text)
    repaired = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)
    repaired_hooks = client_setup.tomllib.loads(repaired["files"][config])["hooks"]
    assert all(hook["event"] != "SessionSetup" for hook in repaired_hooks)
    assert {hook["event"] for hook in repaired_hooks} == set(client_setup.EVENTS) - {"PreToolUse"}
    for hook in repaired_hooks:
        argv = client_setup.hook_argv(hook["command"])
        assert Path(argv[1]) == ROOT / ".agents/hooks/vaws_session.py"
        assert argv[argv.index("--client") + 1] == "kimi"
        assert argv[argv.index("--project") + 1] == str(project)
    assert client_setup.tomllib.loads(repaired["files"][config])["provider"] == {"name": "kept"}
    # Configuring another repository must preserve this project's explicitly
    # installed extension; only the selected project's owned hooks are replaced.
    separate = client_setup.build_plan("kimi", other, kimi_config=config, task_only=True)
    separate_hooks = client_setup.tomllib.loads(separate["files"][config])["hooks"]
    assert sum(hook["event"] == "SessionSetup" for hook in separate_hooks) == 1
    separate_argv = client_setup.hook_argv(separate_hooks[-1]["command"])
    assert Path(separate_argv[1]) == ROOT / ".agents/hooks/vaws_session.py"
    assert separate_argv[separate_argv.index("--project") + 1] == str(other)
    assert client_setup.tomllib.loads(separate["files"][config])["provider"] == {"name": "kept"}
    config.write_text(repaired["files"][config])
    assert client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)["files"][config] == repaired["files"][config]
    explicit_again = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True,
                                              kimi_session_setup=True)
    assert sum(hook["event"] == "SessionSetup"
               for hook in client_setup.tomllib.loads(explicit_again["files"][config])["hooks"]) == 1


@pytest.mark.parametrize("extended", [False, True])
def test_user_mcp_moves_only_managed_providers_and_keeps_user_configuration(tmp_path, monkeypatch, extended):
    receipt = selected_runtime(monkeypatch, client_setup, tmp_path)
    monkeypatch.setattr(vaws_kimi_config, "managed_receipt", lambda root: receipt)
    managed_task = client_setup.desired_mcp_servers(task_only=True)["vaws-task"]
    monkeypatch.setattr(client_setup, "owned_environment_server",
                        lambda entry, project: entry.get("args") == managed_task["args"])
    project = repo(tmp_path / "project")
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    original = {"custom": "keep", "mcpServers": {"another": {"command": "custom-provider"}}}
    (home / "mcp.json").write_text(json.dumps(original))
    plan = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True, kimi_session_setup=extended)
    value = json.loads(plan["files"][home / "mcp.json"])
    assert value["custom"] == "keep"
    assert value["mcpServers"]["another"] == original["mcpServers"]["another"]
    assert value["mcpServers"]["vaws-task"]["args"] == [str(ROOT / ".agents/scripts/vaws_native_mcp.py"), "task"]
    assert provider.PIN_ENV not in value["mcpServers"]["vaws-task"]["env"]
    assert "vaws-task" not in json.loads(plan["files"][project / ".kimi-code/mcp.json"])["mcpServers"]
    custom = {"mcpServers": {"vaws-task": {"command": "my-own-server", "custom": True}}}
    (home / "mcp.json").write_text(json.dumps(custom))
    plan = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True, kimi_session_setup=extended)
    assert json.loads(plan["files"][home / "mcp.json"])["mcpServers"]["vaws-task"] == custom["mcpServers"]["vaws-task"]


def test_native_provider_uses_each_worktree_saved_receipt_and_client_cwd(tmp_path, monkeypatch):
    import vaws_claude_entry

    project = repo(tmp_path / "project")
    (project / ".agents/lib").mkdir(parents=True)
    (project / ".agents/lib/vaws_environment.py").write_text("# fixture marker\n")
    git(project, "add", ".agents")
    git(project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "marker")
    target = tmp_path / "new worktree"
    git(project, "worktree", "add", "--detach", str(target), "HEAD")
    monkeypatch.setattr(provider, "ROOT", project)
    receipts = {project: {"python": "/old/python", "receipt": "/old/ready.json"},
                target: {"python": "/new/python", "receipt": "/new/ready.json"}}
    monkeypatch.setattr(vaws_claude_entry, "saved_ready", lambda cwd: receipts[cwd])
    native, command, env = provider.provider_plan("task", project, {"VAWS_MCP_WORKSPACE": str(target),
                                                                   provider.PIN_ENV: "/stale.json"})
    assert native == target
    assert command[:1] == ["/new/python"]
    assert env[provider.PIN_ENV] == "/new/ready.json"
    assert env["VAWS_AGENT_SESSIONS_DIR"] == str(project / ".vaws-local/agent-sessions")
    assert "VAWS_CONTEXT_FILE" not in env
    resumed, old_command, old_env = provider.provider_plan("remote", project, {})
    assert resumed == project and old_command[0] == "/old/python"
    assert old_env[provider.PIN_ENV] == "/old/ready.json"
    assert old_env["REMOTE_DEV_STATE_DIR"] == str(project / ".vaws-local/remote-dev-state")


def test_kimi_global_windows_owner_does_not_depend_on_the_source_cwd(tmp_path, monkeypatch):
    root = PurePosixPath("/mnt/d/work")
    project = tmp_path / "project"
    home = tmp_path / "home"
    files = {project / ".kimi-code/mcp.json": json.dumps({"mcpServers": {"vaws-task": {
        "command": "./.vaws-local/env-links/old/Scripts/python.exe",
        "args": ["-m", "vaws_coordinator", "task-server"], "env": {}}}})}
    monkeypatch.setattr(vaws_kimi_config, "windows_mounted_workspace", lambda _: True)
    monkeypatch.setattr(vaws_kimi_config, "managed_receipt", lambda _: {"python": r"C:\Users\owner\vaws\new\Scripts\python.exe"})
    vaws_kimi_config.add_kimi_user_mcp(files, [], project, root, home, owned_server=lambda *_: True)
    server = json.loads(files[home / "mcp.json"])["mcpServers"]["vaws-task"]
    assert server["command"] == r"C:\Users\owner\vaws\new\Scripts\python.exe"
    assert server["args"] == [r"D:\work\.agents\scripts\vaws_native_mcp.py", "task"]

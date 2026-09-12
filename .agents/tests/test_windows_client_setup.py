"""Execute generated hooks under native Windows shells with literal input."""
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch
from types import SimpleNamespace

import pytest
from client_setup_fixtures import selected_runtime

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("windows_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
setup = importlib.util.module_from_spec(spec)
with patch("vaws_venv.ensure_workspace_interpreter"):
    spec.loader.exec_module(setup)


@pytest.fixture(autouse=True)
def selected_environment(monkeypatch, tmp_path):
    return selected_runtime(monkeypatch, setup, tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="native Windows shell execution")
@pytest.mark.parametrize("shell", ["cmd", "powershell", "pwsh"])
def test_hook_roundtrip_literal_arguments_stdin_and_exit(shell):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    with tempfile.TemporaryDirectory(prefix="hook 中文 '") as temporary:
        script = Path(temporary) / "echo args.py"
        script.write_text(
            "import json, sys\nprint(json.dumps([sys.argv[1:], sys.stdin.read()], ensure_ascii=False))\nsys.exit(7)\n",
            encoding="utf-8",
        )
        arguments = [sys.executable, str(script), "中文 空格", "x&y", "$value", "single'quote", "%PATH%"]
        command = setup.local_hook_command(arguments)
        assert setup.hook_argv(command) == arguments
        invocation = ([executable, "/d", "/s", "/c", command] if shell == "cmd" else
                      [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command + "; exit $LASTEXITCODE"])
        result = subprocess.run(invocation, input='{"message":"输入文本"}', text=True,
                                encoding="utf-8", capture_output=True, timeout=20)
        assert result.returncode == 7, result.stderr
        assert json.loads(result.stdout) == [arguments[2:], '{"message":"输入文本"}']


def test_foreign_encoded_command_is_not_owned():
    assert not setup.owned_hook_command("powershell.exe -EncodedCommand Zg==", "codex", ROOT)


def test_wsl_hooks_share_windows_task_owner_and_keep_literal_paths(monkeypatch, selected_environment):
    monkeypatch.setattr(setup, "managed_python", lambda: "/mnt/d/work/.vaws-local/env-links/" + "a" * 64 + "/Scripts/python.exe")
    monkeypatch.setattr(setup, "ROOT", PurePosixPath("/mnt/d/work"))
    monkeypatch.setattr(setup, "windows_mounted_workspace", lambda root: True)
    monkeypatch.setattr(setup, "local_hook_command", lambda argv: __import__("shlex").join(argv))
    registry = r"D:\work\.vaws-local\agent-sessions"
    command = setup.hook_command("grok", PurePosixPath("/mnt/d/work/client 中文"), {"VAWS_AGENT_SESSIONS_DIR": registry})
    argv = setup.hook_argv(command)
    assert argv[0].startswith("/mnt/d/")
    assert argv[1] == r"D:\work\.agents\hooks\vaws_session.py"
    assert argv[argv.index("--project") + 1] == "D:\\work\\client 中文"
    assert argv[argv.index("--agent-sessions-dir") + 1] == registry
    assert argv[argv.index("--environment-receipt") + 1] == selected_environment["receipt"]
    assert setup.managed_path(registry) == registry
    assert setup.hook_path_identity("/mnt/d/work/client 中文") == setup.hook_path_identity("D:\\work\\client 中文")
    with pytest.raises(ValueError, match="mounted Windows drive"):
        setup.managed_path("/opt/private")


def test_kimi_code_uses_native_home_and_discovers_scoped_project_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "kimi"))
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path / "legacy"))
    setup.kimi_home().mkdir()
    (setup.kimi_home() / "mcp.json").write_text('{"mcpServers":{"existing":{}}}')
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(setup, "ROOT", project)
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", project / ".agents/hooks/vaws_session.py")
    plan = setup.build_plan("kimi", project, task_only=True)
    assert setup.kimi_home() / "config.toml" in plan["files"]
    assert plan["launch_argv"] == ["kimi"]
    assert plan["launch_cwd"] == str(project)
    assert json.loads(plan["files"][setup.kimi_home() / "mcp.json"])["mcpServers"]["existing"] == {}
    project_servers = json.loads(plan["files"][project / ".kimi-code/mcp.json"])["mcpServers"]
    user_servers = json.loads(plan["files"][setup.kimi_home() / "mcp.json"])["mcpServers"]
    # An owned native launch moves to the user provider; a custom interpreter
    # stays project-scoped. Both native discoveries must expose exactly one.
    entries = [servers["vaws-task"] for servers in (project_servers, user_servers) if "vaws-task" in servers]
    assert len(entries) == 1
    assert entries[0]["toolTimeoutMs"] == 600000
    assert "type" not in entries[0]


def test_generated_task_owner_migrates_without_rewriting_user_provider_or_policy(monkeypatch):
    monkeypatch.setattr(setup,"ROOT",PurePosixPath("/mnt/d/work"))
    existing={"command":"/mnt/d/work/.vaws-local/env-links/" + "a" * 64 + "/bin/python", "args":setup.task_server_args(),
              "env":{"CUSTOM":"kept"}, "enabled_tools":["vaws_execution"]}
    desired={"command":"/mnt/d/work/.vaws-local/env-links/" + "b" * 64 + "/Scripts/python.exe", "args":setup.task_server_args(),"env":{}}
    merged,action=setup.merge_server_entry(existing,desired)
    assert action == "updated-managed"
    assert merged["command"] == desired["command"]
    assert merged["enabled_tools"] == existing["enabled_tools"]
    assert merged["env"]["CUSTOM"] == "kept"
    custom={**existing,"command":"/usr/bin/python3"}
    assert setup.merge_server_entry(custom, desired)[0]["command"] == custom["command"]
    text='[mcp_servers.vaws_task]\ncommand="old"\nargs=["-m","vaws_coordinator","task-server"]\nenabled_tools=["vaws_execution"]\n[mcp_servers.other]\ncommand="untouched"\n'
    value=setup.tomllib.loads(setup.update_toml_server_command(text,"vaws_task",desired["command"]))
    assert value["mcp_servers"]["vaws_task"]["enabled_tools"] == ["vaws_execution"]
    assert value["mcp_servers"]["other"]["command"] == "untouched"


def test_wsl_setup_replaces_native_spelling_and_keeps_one_owned_hook(monkeypatch):
    import shlex
    monkeypatch.setattr(setup, "ROOT", PurePosixPath("/mnt/d/work"))
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", PurePosixPath("/mnt/d/work/.agents/hooks/vaws_session.py"))
    native = {"command": "D:\\work\\.vaws-local\\env-links\\" + "a" * 64 + "\\Scripts\\python.exe", "args": setup.task_server_args()}
    desired = {"command": "/mnt/d/work/.vaws-local/env-links/" + "a" * 64 + "/Scripts/python.exe", "args": setup.task_server_args()}
    assert setup.merge_server_entry(native, desired)[0]["command"] == desired["command"]
    old_command = shlex.join([native["command"], r"D:\work\.agents\hooks\vaws_session.py", "--client", "codex", "--project", r"D:\work\project"])
    new_command = shlex.join([desired["command"], r"D:\work\.agents\hooks\vaws_session.py", "--client", "codex", "--project", r"D:\work\project"])
    old = [{"hooks": [{"command": old_command, "custom": "keep"}]}]
    wanted = [{"hooks": [{"command": new_command, "timeout": 12}]}]
    merged = setup.merge_hook_event(old, wanted, "codex", PurePosixPath("/mnt/d/work/project"))
    assert merged == [{"hooks": [{"command": new_command, "custom": "keep", "type": "command", "timeout": 12}]}]
    assert setup.merge_hook_event(merged, wanted, "codex", PurePosixPath("/mnt/d/work/project")) == merged


def test_shared_kimi_migrates_generated_servers_and_preserves_custom_values(monkeypatch):
    monkeypatch.setattr(setup, "ROOT", PurePosixPath("/mnt/d/work"))
    existing = {"command": "/mnt/d/work/.vaws-local/env-links/" + "a" * 64 + "/bin/python",
                "args": setup.remote_dev_server_args(),
                "env": {"REMOTE_DEV_STATE_DIR": "/mnt/d/work/.vaws-local/remote-dev-state", "CUSTOM": "keep"},
                "disabledTools": ["remote_run"]}
    desired = {"command": "./.vaws-local/env-links/" + "b" * 64 + "/Scripts/python.exe",
               "args": setup.remote_dev_server_args(),
               "env": {"REMOTE_DEV_STATE_DIR": r"D:\work\.vaws-local\remote-dev-state"}}
    merged, action = setup.merge_server_entry(existing, desired, checkout=PurePosixPath("/mnt/d/work"))
    assert action == "updated-managed"
    assert merged["command"] == desired["command"]
    assert merged["env"] == {**desired["env"], "CUSTOM": "keep"}
    assert merged["disabledTools"] == existing["disabledTools"]
    custom = {**existing, "command": "/usr/local/bin/my-provider", "env": {"REMOTE_DEV_STATE_DIR": "/custom/state", "CUSTOM": "keep"}}
    preserved, action = setup.merge_server_entry(custom, desired, checkout=PurePosixPath("/mnt/d/work"))
    assert preserved["command"] == custom["command"] and preserved["env"] == custom["env"]
    assert action == "preserved"


def test_windows_mcp_env_crosses_wsl_and_preserves_custom_flags():
    result = setup.windows_interop_env({"REMOTE_DEV_STATE_DIR": r"D:\work\state", "CUSTOM": "value", "WSLENV": "EXISTING/p:CUSTOM/w"})
    assert result["CUSTOM"] == "value"
    assert result["WSLENV"] == "EXISTING/p:CUSTOM/w:REMOTE_DEV_STATE_DIR/w"
    text = '[mcp_servers.vaws_task]\ncommand="managed-python"\n[mcp_servers.vaws_task.env]\nCUSTOM="value"\nWSLENV="EXISTING/p"\n'
    existing = setup.tomllib.loads(text)["mcp_servers"]["vaws_task"]
    desired = {"env": setup.windows_interop_env({"VAWS_AGENT_SESSIONS_DIR": r"D:\work\sessions"})}
    updated = setup.fill_toml_server_env(text, "vaws_task", existing, desired)
    env = setup.tomllib.loads(updated)["mcp_servers"]["vaws_task"]["env"]
    assert env["CUSTOM"] == "value" and env["VAWS_AGENT_SESSIONS_DIR"] == r"D:\work\sessions"
    assert env["WSLENV"] == "EXISTING/p:CUSTOM/w:VAWS_AGENT_SESSIONS_DIR/w"
    assert setup.fill_toml_server_env(updated, "vaws_task", {"env": env}, desired) == updated


def test_existing_json_task_alias_keeps_its_custom_registry(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"vaws_task": {
        "command": "custom", "env": {"VAWS_AGENT_SESSIONS_DIR": "/custom/registry"}}}}))
    assert setup.existing_task_env("claude", tmp_path) == {"VAWS_AGENT_SESSIONS_DIR": "/custom/registry"}


@pytest.mark.parametrize("command", [
    "/mnt/d/work/.venv/bin/python", r"D:\work\.venv\Scripts\python.exe",
    "./.venv/bin/python", r".\.venv\Scripts\python.exe",
    "./.vaws-local/venvs/linux/bin/python",
    "./.vaws-local/venvs/win32/Scripts/python.exe",
    "../work/.venv/bin/python",
])
def test_old_layout_alone_does_not_establish_provider_ownership(command, tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", PurePosixPath("/mnt/d/work"))
    monkeypatch.chdir(tmp_path)
    desired = {"command": "/mnt/d/work/.vaws-local/env-links/" + "b" * 64 + "/Scripts/python.exe",
               "args": setup.task_server_args()}
    existing = {"command": command, "args": setup.task_server_args()}
    merged, action = setup.merge_server_entry(existing, desired, checkout=PurePosixPath("/mnt/d/work"))
    assert action == "preserved" and merged == existing


@pytest.mark.parametrize("customization", ["different-checkout", "custom-provider", "extra-args", "wrapper", "another-known-provider"])
def test_live_custom_task_launcher_is_preserved(customization, tmp_path, monkeypatch):
    workspace, other = tmp_path / "workspace", tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    interpreter = workspace / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(setup, "ROOT", workspace)
    desired = {"command": str(workspace / ".vaws-local/env-links" / ("b" * 64) / "Scripts/python.exe"),
               "args": setup.task_server_args()}
    existing = {"command": str(interpreter), "args": setup.task_server_args(), "enabled_tools": ["vaws_execution"]}
    checkout = workspace
    if customization == "different-checkout":
        checkout = other
        relative = other / ".venv/bin/python"
        relative.parent.mkdir(parents=True)
        relative.touch()
        existing["command"] = "./.venv/bin/python"
    elif customization == "custom-provider":
        existing["command"] = str(other / "my-provider")
        (other / "my-provider").touch()
    elif customization == "extra-args":
        existing["args"] = [*setup.task_server_args(), "--custom-option"]
    elif customization == "another-known-provider":
        existing["command"] = str(workspace / ".vaws-local/env-links" / ("a" * 64) / "bin/python")
        existing["args"] = setup.knowledge_server_args()
    else:
        existing["args"] = ["-c", "import runpy; runpy.run_module('vaws_coordinator')"]
    merged, action = setup.merge_server_entry(existing, desired, checkout=checkout)
    assert action == "preserved"
    assert merged == existing


@pytest.mark.parametrize("client", ["claude", "cursor", "codex", "grok"])
@pytest.mark.parametrize("relative", [False, True])
def test_setup_updates_owned_task_entry_and_preserves_configuration(client, relative, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_relative = ".vaws-local/env-links/" + "a" * 64 + "/bin/python"
    interpreter = workspace / old_relative
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    unrelated_cwd = tmp_path / "setup-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setattr(setup, "ROOT", workspace)
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    desired = {"command": str(workspace / ".vaws-local/env-links" / ("b" * 64) / "Scripts/python.exe"),
               "args": setup.task_server_args(), "env": {"DEFAULT": "added", "WSLENV": "DEFAULT/w"}}
    Path(desired["command"]).parent.mkdir(parents=True)
    Path(desired["command"]).touch()
    monkeypatch.setattr(setup, "desired_mcp_servers", lambda **kwargs: {"vaws-task": desired})
    existing = {"command": "./" + old_relative if relative else str(interpreter),
                "args": setup.task_server_args(), "enabled_tools": ["vaws_execution"],
                "env": {"CUSTOM": "keep", "VAWS_AGENT_SESSIONS_DIR": str(tmp_path / "custom-registry")}}
    if client in {"claude", "cursor"}:
        path = workspace / (".mcp.json" if client == "claude" else ".cursor/mcp.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": {"vaws_task": existing, "other": {"command": "untouched"}}}))
    else:
        path = workspace / ("." + client) / "config.toml"
        path.parent.mkdir()
        path.write_text("# user settings\napproval_policy = 'on-request'\n"
                        + setup.toml_server_body("vaws_task", existing).replace("\n[mcp_servers.vaws_task.env]",
                            '\nenabled_tools = ["vaws_execution"]\n[mcp_servers.vaws_task.env]')
                        + "\n[mcp_servers.other]\ncommand = 'untouched'\n")
    plan = setup.build_plan(client, workspace, task_only=True)
    rendered = plan["files"][path]
    parsed = (json.loads(rendered)["mcpServers"] if client in {"claude", "cursor"}
              else setup.tomllib.loads(rendered)["mcp_servers"])
    migrated = parsed["vaws_task"]
    assert migrated["command"] == desired["command"]
    assert migrated["args"] == ([str(workspace / ".agents/scripts/vaws_claude_entry.py"), "task"]
                                if client == "claude" else existing["args"])
    assert migrated["enabled_tools"] == existing["enabled_tools"]
    assert {key: migrated["env"][key] for key in existing["env"]} == existing["env"]
    assert migrated["env"]["DEFAULT"] == "added"
    assert "CUSTOM/w" in migrated["env"]["WSLENV"]
    assert parsed["other"] == {"command": "untouched"}
    if client in {"codex", "grok"}:
        assert rendered.startswith("# user settings\napproval_policy = 'on-request'\n")
    for output, content in plan["files"].items():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    repeated = setup.build_plan(client, workspace, task_only=True)
    assert repeated["files"].get(path, rendered) == rendered


@pytest.mark.parametrize("customization", ["inline-env", "another-known-provider", "quoted-header",
                                          "commented-header", "quoted-command", "quoted-pin", "multiline-command"])
def test_toml_custom_entry_preserves_interpreter_and_pin_together(customization, tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    old_python = str(tmp_path / ".vaws-local/env-links" / ("a" * 64) / "Scripts/python.exe")
    new_python = str(tmp_path / ".vaws-local/env-links" / ("b" * 64) / "Scripts/python.exe")
    existing = {"command": old_python,
                "args": setup.knowledge_server_args() if customization == "another-known-provider" else setup.task_server_args(),
                "env": {setup.PIN_ENV: "old-pin", "CUSTOM": "keep"}}
    desired = {"command": new_python, "args": setup.task_server_args(),
               "env": {setup.PIN_ENV: "new-pin", "DEFAULT": "added"}}
    monkeypatch.setattr(setup, "desired_mcp_servers", lambda **kwargs: {"vaws-task": desired})
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir()
    if customization == "inline-env":
        content = ("[mcp_servers.vaws_task]\ncommand = " + json.dumps(old_python)
                   + "\nargs = " + json.dumps(existing["args"])
                   + '\nenv = { VAWS_ENV_RECEIPT = "old-pin", CUSTOM = "keep" }\n')
    else:
        content = setup.toml_server_body("vaws_task", existing)
        if customization == "quoted-header":
            content = content.replace("[mcp_servers.vaws_task]", "[mcp_servers.'vaws_task']")
        elif customization == "commented-header":
            content = content.replace("[mcp_servers.vaws_task]", "[mcp_servers.vaws_task] # user comment")
        elif customization == "quoted-command":
            content = content.replace("command =", "'command' =")
        elif customization == "quoted-pin":
            content = content.replace("VAWS_ENV_RECEIPT =", "'VAWS_ENV_RECEIPT' =")
        elif customization == "multiline-command":
            content = content.replace("command = " + json.dumps(old_python), "command = '''\n" + old_python + "'''")
    path.write_text(content, encoding="utf-8")
    plan = setup.build_plan("codex", tmp_path, task_only=True)
    rendered = plan["files"].get(path, content)
    assert rendered == content
    assert setup.tomllib.loads(rendered)["mcp_servers"]["vaws_task"] == existing


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("provider,args", [
    ("vaws-task", ["-m", "vaws_coordinator", "task-server"]),
    ("remote-dev", ["-m", "remote_dev.mcp.server"]),
    ("vaws-knowledge", ["-m", "vaws_knowledge.server.mcp_server"]),
])
def test_generated_provider_and_hooks_move_to_new_pin_preserving_user_fields(client, provider, args, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(setup, "ROOT", workspace)
    hook_script = workspace / ".agents/hooks/vaws_session.py"
    monkeypatch.setattr(setup, "OWNED_HOOK_SCRIPT", hook_script)
    old_command = str(workspace / ".vaws-local/env-links" / ("a" * 64) / "Scripts/python.exe")
    new_command = str(workspace / ".vaws-local/env-links" / ("b" * 64) / "Scripts/python.exe")
    old_pin = str(tmp_path / "environments" / ("a" * 64) / ".vaws-ready.json")
    new_pin = str(tmp_path / "environments" / ("b" * 64) / ".vaws-ready.json")
    monkeypatch.setattr(setup, "managed_receipt", lambda root: {"receipt": new_pin})
    monkeypatch.setattr(setup, "managed_python", lambda: new_command)
    desired = {"command": new_command, "args": args, "env": {setup.PIN_ENV: new_pin}}
    monkeypatch.setattr(setup, "desired_mcp_servers", lambda **kwargs: {provider: desired})
    existing = {"command": old_command, "args": args, "enabled_tools": ["chosen_tool"],
                "custom_policy": "keep", "env": {setup.PIN_ENV: old_pin, "CUSTOM": "keep"}}
    key = provider.replace("-", "_")
    path = workspace / (".mcp.json" if client == "claude" else ".codex/config.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    if client == "claude":
        path.write_text(json.dumps({"mcpServers": {key: existing, "foreign": {"command": "unchanged"}}}), encoding="utf-8")
    else:
        text = setup.toml_server_body(key, existing).replace(
            f"\n[mcp_servers.{key}.env]", '\nenabled_tools = ["chosen_tool"]\ncustom_policy = "keep"\n' + f"[mcp_servers.{key}.env]")
        path.write_text("# user comment\n" + text + "\n[mcp_servers.foreign]\ncommand = 'unchanged'\n", encoding="utf-8")
    hook_path = workspace / (".claude/settings.local.json" if client == "claude" else ".codex/hooks.json")
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    old_hook = setup.local_hook_command([old_command, str(hook_script), "--client", client,
                                        "--project", str(workspace), "--environment-receipt", old_pin])
    hook_path.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"command": "foreign literal %PATH% $value", "custom": "untouched"}]},
        {"hooks": [{"command": old_hook, "custom": "kept"}]},
    ]}}), encoding="utf-8")
    plan = setup.build_plan(client, workspace)
    rendered = plan["files"][path]
    servers = json.loads(rendered)["mcpServers"] if client == "claude" else setup.tomllib.loads(rendered)["mcp_servers"]
    expected = {**existing, "command": new_command, "env": {setup.PIN_ENV: new_pin, "CUSTOM": "keep"}}
    if client == "claude":
        kind = {"vaws-task": "task", "remote-dev": "remote", "vaws-knowledge": "knowledge"}[provider]
        expected.update(args=[str(workspace / ".agents/scripts/vaws_claude_entry.py"), kind], env={"CUSTOM": "keep"})
    assert servers[key] == expected
    assert servers["foreign"] == {"command": "unchanged"}
    hooks = json.loads(plan["files"][hook_path])["hooks"]["SessionStart"]
    assert hooks[0] == {"hooks": [{"command": "foreign literal %PATH% $value", "custom": "untouched"}]}
    assert len(hooks) == 2 and hooks[1]["hooks"][0]["custom"] == "kept"
    arguments = setup.hook_argv(hooks[1]["hooks"][0]["command"])
    if client == "claude":
        assert arguments[1:3] == [str(workspace / ".agents/scripts/vaws_claude_entry.py"), "session"]
        assert "--environment-receipt" not in arguments  # selected from actual native cwd at launch
    else:
        assert arguments[arguments.index("--environment-receipt") + 1] == new_pin
    for output, content in plan["files"].items():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8")
    repeated = setup.build_plan(client, workspace)
    assert all(content == path.read_text(encoding="utf-8") for path, content in repeated["files"].items())


@pytest.mark.parametrize("args", [
    ["-m", "vaws_coordinator", "task-server"], ["-m", "remote_dev.mcp.server"],
    ["-m", "vaws_knowledge.server.mcp_server"],
])
def test_foreign_provider_keeps_its_own_pin_and_policy(args, tmp_path):
    existing = {"command": "user-provider", "args": args, "env": {setup.PIN_ENV: "user-pin", "CUSTOM": "keep"},
                "enabled_tools": ["chosen"]}
    desired = {"command": "new-managed-provider", "args": args, "env": {setup.PIN_ENV: "new-managed-pin"}}
    result, action = setup.merge_server_entry(existing, desired, checkout=tmp_path)
    assert action == "preserved" and result == existing


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction and mounted-drive rendering")
def test_shared_kimi_windows_and_wsl_use_identical_per_key_relative_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "ROOT", tmp_path)
    servers = {"vaws-task": {"command": sys.executable, "args": setup.task_server_args(),
                            "env": {"VAWS_AGENT_SESSIONS_DIR": str(tmp_path / "sessions")}}}
    windows = setup.shared_kimi_servers(servers, tmp_path)
    expected = "./.vaws-local/env-links/" + "d" * 64 + "/Scripts/python.exe"
    assert windows["vaws-task"]["command"] == expected

    class MountedPath(PurePosixPath):
        def is_file(self):
            return Path(self.parts[2] + ":/", *self.parts[3:]).is_file()

    mounted = MountedPath("/mnt", tmp_path.drive[0].lower(), *tmp_path.parts[1:])
    monkeypatch.setattr(setup, "ROOT", mounted)
    monkeypatch.setattr(setup, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(setup, "windows_mounted_workspace", lambda root: True)
    wsl = setup.shared_kimi_servers(servers, mounted)
    assert wsl == windows

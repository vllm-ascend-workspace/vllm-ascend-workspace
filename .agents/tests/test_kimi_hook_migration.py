"""Only generated callbacks from the same Git family are superseded."""
import hashlib
import json
import shlex
import tomllib

from test_kimi_native_setup import client_setup, git, repo
from client_setup_fixtures import selected_runtime
import vaws_kimi_config
from vaws_kimi_config import remove_owned_kimi_hooks


def hook(command, event="SessionStart"):
    return f'[[hooks]]\nevent = "{event}"\ncommand = {json.dumps(command)}\ntimeout = 12\n'


def block(project, body):
    key = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    return f"# BEGIN VAWS session-{key}\n{body}# END VAWS session-{key}\n"


def test_removes_all_same_family_generated_hooks_and_preserves_custom_text(tmp_path):
    project = repo(tmp_path / "project 用户")
    linked = tmp_path / "old tree"
    git(project, "worktree", "add", "--detach", str(linked), "HEAD")
    other = repo(tmp_path / "independent")
    nested = project / "old-config"
    nested.mkdir()
    direct = lambda target: shlex.join(["python", str(project / ".agents/hooks/vaws_session.py"),
                                       "--client", "kimi", "--project", str(target)])
    adapter = lambda target: shlex.join(["uv", "run", "--no-project", "python",
                                        str(project / ".agents/scripts/vaws_kimi_session_setup.py"), "--project", str(target)])
    current = block(project, hook(adapter(project), "SessionSetup"))
    unrelated = block(other, hook(direct(other)))
    custom = block(nested, hook("my-custom-hook") + hook(direct(nested)))
    original = ("# keep native settings\n[thinking]\nenabled = true\n\n"
                + hook(direct(nested)) + block(linked, hook(adapter(linked)))
                + unrelated + custom + hook("another-custom-hook") + current)
    migrated = remove_owned_kimi_hooks(original, project, project, parse_command=shlex.split)
    assert block(linked, hook(adapter(linked))) not in migrated
    assert current not in migrated
    assert unrelated in migrated
    assert hook("my-custom-hook") in migrated
    assert hook("another-custom-hook") in migrated
    commands = [entry["command"] for entry in tomllib.loads(migrated)["hooks"]]
    assert commands == [direct(other), "my-custom-hook", "another-custom-hook"]
    assert tomllib.loads(migrated)["thinking"] == {"enabled": True}
    assert remove_owned_kimi_hooks(migrated, project, project, parse_command=shlex.split) == migrated


def test_preserves_custom_legacy_arguments_or_matcher(tmp_path):
    project = repo(tmp_path / "project")
    command = shlex.join(["python", str(project / ".agents/hooks/vaws_session.py"),
                          "--client", "kimi", "--project", str(project)])
    original = hook(command + " --custom preserved") + hook(command) + 'matcher = "Bash"\n'
    assert remove_owned_kimi_hooks(original, project, project, parse_command=shlex.split) == original


def test_repair_after_native_writer_removed_markers_keeps_one_callback(tmp_path, monkeypatch):
    receipt = selected_runtime(monkeypatch, client_setup, tmp_path)
    monkeypatch.setattr(vaws_kimi_config, "managed_receipt", lambda _: receipt)
    project = repo(tmp_path / "project")
    config = tmp_path / "config.toml"
    initial = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True, kimi_session_setup=True)
    original = initial["files"][config]
    unmarked = "\n".join(line for line in original.splitlines() if not line.startswith("#")) + "\n"
    old = shlex.join(["python", str(client_setup.ROOT / ".agents/hooks/vaws_session.py"),
                      "--client", "kimi", "--project", str(project), "--environment-receipt", "/old/receipt.json"])
    config.write_text(unmarked + hook(old))
    repaired = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)["files"][config]
    hooks = tomllib.loads(repaired)["hooks"]
    events = [entry["event"] for entry in hooks]
    assert "SessionSetup" not in events
    assert len(events) == len(set(events))
    assert set(events) == set(client_setup.EVENTS) - {"PreToolUse"}
    for entry in hooks:
        argv = client_setup.hook_argv(entry["command"])
        assert argv[1] == str(client_setup.ROOT / ".agents/hooks/vaws_session.py")
        assert argv[argv.index("--client") + 1] == "kimi"
        assert argv[argv.index("--project") + 1] == str(project)
    config.write_text(repaired)
    assert client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)["files"][config] == repaired


def test_official_switch_preserves_custom_and_stop_hooks_inside_managed_block(tmp_path, monkeypatch):
    receipt = selected_runtime(monkeypatch, client_setup, tmp_path)
    monkeypatch.setattr(vaws_kimi_config, "managed_receipt", lambda _: receipt)
    project = repo(tmp_path / "project 用户")
    config = tmp_path / "config.toml"
    adapter = shlex.join(["uv", "run", "--no-project", "python",
                          str(client_setup.ROOT / ".agents/scripts/vaws_kimi_session_setup.py"),
                          "--project", str(project)])
    knowledge = shlex.join(["python", str(client_setup.ROOT / ".agents/hooks/knowledge_summary.py"),
                            "--client", "kimi", "--project", str(project)])
    custom_start = hook("user-start-hook", "SessionStart")
    custom_stop = hook("user-stop-hook", "Stop")
    knowledge_stop = hook(knowledge, "Stop")
    custom_adapter = hook(adapter + " --user-option preserved", "UserPromptSubmit")
    config.write_text('[provider]\nname = "kept"\n\n' + block(project,
        hook(adapter, "SessionSetup") + hook(adapter, "UserPromptSubmit")
        + custom_start + custom_stop + knowledge_stop + custom_adapter))

    repaired = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)["files"][config]
    hooks = tomllib.loads(repaired)["hooks"]
    assert all(entry["event"] != "SessionSetup" for entry in hooks)
    assert all(entry["command"] != adapter for entry in hooks)
    for preserved in (custom_start, custom_stop, knowledge_stop, custom_adapter):
        assert preserved in repaired
    assert tomllib.loads(repaired)["provider"] == {"name": "kept"}
    assert [entry["command"] for entry in hooks].count("user-start-hook") == 1
    assert [entry["command"] for entry in hooks].count("user-stop-hook") == 1
    config.write_text(repaired)
    repeated = client_setup.build_plan("kimi", project, kimi_config=config, task_only=True)["files"][config]
    assert repeated == repaired


def test_removal_requires_exact_owned_command_and_keeps_unrelated_projects(tmp_path):
    project = repo(tmp_path / "project")
    other = repo(tmp_path / "other")
    adapter = lambda target: shlex.join(["uv", "run", "--no-project", "python",
        str(project / ".agents/scripts/vaws_kimi_session_setup.py"), "--project", str(target)])
    owned = hook(adapter(project), "SessionSetup")
    unrelated = block(other, hook(adapter(other), "SessionSetup"))
    customized = hook(adapter(project), "UserPromptSubmit") + 'matcher = "custom"\n'
    original = block(project, owned + hook("keep-stop", "Stop")) + unrelated + customized
    cleaned = remove_owned_kimi_hooks(original, project, project, parse_command=shlex.split)
    assert owned not in cleaned
    assert hook("keep-stop", "Stop") in cleaned
    assert unrelated in cleaned
    assert customized in cleaned
    assert remove_owned_kimi_hooks(cleaned, project, project, parse_command=shlex.split) == cleaned

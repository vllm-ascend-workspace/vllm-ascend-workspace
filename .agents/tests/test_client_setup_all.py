"""One-time setup discovers installed clients and preserves independent results."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import plistlib
import sys
import tomllib
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
import vaws_client_inventory as inventory
import vaws_native_mode_config as modes

spec = importlib.util.spec_from_file_location("all_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
setup = importlib.util.module_from_spec(spec)
with patch("vaws_venv.ensure_workspace_interpreter"):
    spec.loader.exec_module(setup)


def installations(*names):
    return {name: {"installed": name in names, "executable": "/tools/" + name if name in names else None,
                   "evidence": []} for name in inventory.CLIENT_COMMANDS}


@pytest.fixture
def local_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / ".kimi-code"))
    monkeypatch.setenv("GROK_HOME", str(tmp_path / ".grok"))
    monkeypatch.setattr(setup, "BACKUP_DIR", tmp_path / "backups")
    project = tmp_path / "project"
    project.mkdir()
    calls = []

    def plan(client, directory, **kwargs):
        calls.append((client, kwargs))
        return {"files": {directory / (client + ".json"): '{"configured":true}\n'},
                "notes": [], "mcp_servers": {}, "launch_argv": None, "launch_cwd": str(directory)}

    monkeypatch.setattr(setup, "build_plan", plan)
    return project, calls


def test_configuration_directories_are_not_installation_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(inventory.shutil, "which", lambda *args, **kwargs: None)
    for name in inventory.CLIENT_COMMANDS:
        path = tmp_path / ("." + name) / "config.json"
        path.parent.mkdir()
        path.write_text('{}')
    found = inventory.installed_clients(home=tmp_path, environment={"PATH": ""}, platform="win32")
    assert not any(row["installed"] for row in found.values())
    kimi = tmp_path / ".kimi-code/bin/kimi.exe"
    kimi.parent.mkdir(parents=True)
    kimi.write_text("installed executable fixture")
    found = inventory.installed_clients(home=tmp_path, environment={"PATH": ""}, platform="win32")
    assert found["kimi"]["installed"] and found["kimi"]["executable"] == str(kimi)
    assert not found["codex"]["installed"]


def test_path_and_identified_desktop_app_are_installation_evidence(tmp_path, monkeypatch):
    executable = tmp_path / "claude"
    executable.write_text("binary fixture")
    executable.chmod(0o700)
    monkeypatch.setattr(inventory.shutil, "which", lambda name, **kwargs: str(executable) if name == "claude" else None)
    monkeypatch.setattr(inventory.os, "access", lambda path, mode: str(path).startswith(str(tmp_path)))
    monkeypatch.setattr(inventory, "MAC_APPS", {"codex": (("Fixture.app", "com.openai.codex"),)})
    application = tmp_path / "Applications/Fixture.app/Contents"
    (application / "MacOS").mkdir(parents=True)
    (application / "MacOS/Fixture").write_text("desktop fixture")
    metadata = {"CFBundleIdentifier": "com.openai.chat", "CFBundleExecutable": "Fixture"}
    (application / "Info.plist").write_bytes(plistlib.dumps(metadata))
    assert not inventory.installed_clients(home=tmp_path, environment={}, platform="darwin")["codex"]["installed"]
    metadata["CFBundleIdentifier"] = "com.openai.codex"
    (application / "Info.plist").write_bytes(plistlib.dumps(metadata))
    found = inventory.installed_clients(home=tmp_path, environment={}, platform="darwin")
    assert found["codex"]["installed"] and found["codex"]["executable"] is None
    assert found["claude"]["executable"] == str(executable)


def test_grok_follows_path_while_kimi_follows_its_configured_home(tmp_path, monkeypatch):
    paths = {}
    known = {}
    for client, folder in (("grok", ".grok"), ("kimi", ".kimi-code")):
        paths[client] = tmp_path / "path-bin" / (client + ".exe")
        known[client] = tmp_path / folder / "bin" / (client + ".exe")
        for path in (paths[client], known[client]):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("installed executable fixture")
    monkeypatch.setattr(inventory.shutil, "which", lambda name, **kwargs: str(paths[name]) if name in paths else None)
    found = inventory.installed_clients(home=tmp_path, environment={}, platform="win32")
    assert found["grok"]["executable"] == str(paths["grok"])
    assert found["kimi"]["executable"] == str(known["kimi"])


def test_all_preview_configures_stock_clients_without_native_patch(local_setup, monkeypatch, capsys):
    project, calls = local_setup
    monkeypatch.setattr(inventory, "installed_clients", lambda: installations("codex", "cursor", "kimi", "grok", "claude"))
    assert setup.main(["--client", "all", "--project", str(project)]) == 0
    result = json.loads(capsys.readouterr().out)
    options = dict(calls)
    assert options["codex"]["codex_global_hooks"]
    assert options["cursor"]["cursor_global_mcp"]
    assert options["kimi"]["kimi_session_setup"] is False
    for row in result["clients"].values():
        assert row["workspace_start"]["trigger"] == "new-session-project-guidance"
        assert row["workspace_start"]["native_patch_required"] is False
        assert row["native_worktree"]["missing_action"] is None
    assert not list(project.iterdir())
    assert not (project.parent / ".grok/config.toml").exists()


def test_all_skips_missing_clients_and_does_not_require_kimi_extension(local_setup, monkeypatch, capsys):
    project, calls = local_setup
    monkeypatch.setattr(inventory, "installed_clients", lambda: installations("kimi", "cursor"))
    config = project.parent / ".kimi-code/config.toml"
    config.parent.mkdir()
    original = '[[hooks]]\nevent="SessionStart"\ncommand="custom-setup"\n'
    config.write_text(original)
    assert setup.main(["--client", "all", "--project", str(project), "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert {name for name, _ in calls} == {"cursor", "kimi"}
    assert result["clients"]["kimi"]["state"] == "configured"
    assert result["clients"]["codex"]["state"] == "skipped"
    assert result["clients"]["cursor"]["state"] == "configured"
    assert config.read_text() == original
    record = json.loads(Path(result["record"]).read_text())
    assert record["state"] == "wiring_configured"
    assert record["clients"]["kimi"]["workspace_start"]["resume"] == "reuse-existing-workspace"


@pytest.mark.parametrize("extended", [False, True])
def test_all_preserves_native_update_preferences_and_requires_explicit_extension(local_setup, monkeypatch, capsys, extended):
    project, calls = local_setup
    monkeypatch.setattr(inventory, "installed_clients", lambda: installations("grok", "kimi"))
    grok = project.parent / ".grok/config.toml"
    kimi_home = project.parent / "native-kimi-home"
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    kimi = kimi_home / "tui.toml"
    for path, text in ((grok, "[cli]\nauto_update = true\n"), (kimi, "[upgrade]\nauto_install = true\n")):
        path.parent.mkdir(parents=True)
        path.write_text(text)
    args = ["--client", "all", "--project", str(project), "--apply"]
    if extended:
        args.append("--kimi-session-setup")
    assert setup.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert dict(calls)["kimi"]["kimi_session_setup"] is extended
    assert tomllib.loads(grok.read_text())["cli"]["auto_update"] is True
    assert tomllib.loads(kimi.read_text())["upgrade"]["auto_install"] is True
    assert result["clients"]["kimi"]["workspace_start"]["native_patch_required"] is False


def test_native_gui_choice_is_optional_for_project_startup(local_setup, monkeypatch, capsys):
    project, _ = local_setup
    monkeypatch.setattr(inventory, "installed_clients", lambda: installations("codex", "cursor"))
    assert setup.main(["--client", "all", "--project", str(project), "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "wiring_configured"
    for client in ("codex", "cursor"):
        native = result["clients"][client]["native_worktree"]
        assert native["default_mode"] == "client_choice"
        assert native["missing_action"] is None


def test_all_keeps_completed_files_backups_and_continues_after_a_write_failure(local_setup, monkeypatch, capsys):
    project, _ = local_setup
    monkeypatch.setattr(inventory, "installed_clients", lambda: installations("codex", "cursor"))
    first, broken, later = (project / name for name in ("first.json", "broken.json", "later.json"))
    first.write_text("original content")

    def plan(client, directory, **kwargs):
        return {"files": {first: "new content", broken: "blocked"} if client == "codex" else {later: "complete"},
                "notes": [], "mcp_servers": {}, "launch_argv": None, "launch_cwd": str(directory)}

    monkeypatch.setattr(setup, "build_plan", plan)
    apply = setup.apply_plan

    def write(plan):
        if broken in plan["files"]:
            raise PermissionError("fixture write failure")
        return apply(plan)

    monkeypatch.setattr(setup, "apply_plan", write)
    assert setup.main(["--client", "all", "--project", str(project), "--apply"]) == 1
    result = json.loads(capsys.readouterr().out)
    codex = result["clients"]["codex"]
    assert codex["state"] == "failed" and codex["error"]["path"] == str(broken)
    assert [item["path"] for item in codex["files"]] == [str(first)]
    assert Path(codex["files"][0]["backup"]).read_text() == "original content"
    assert later.read_text() == "complete"
    assert result["clients"]["cursor"]["state"] == "configured"


def test_single_client_entry_does_not_discover_or_set_global_native_preferences(local_setup, monkeypatch, capsys):
    project, calls = local_setup
    monkeypatch.setattr(inventory, "installed_clients", lambda: pytest.fail("per-worktree setup scanned clients"))
    monkeypatch.setattr(modes, "add_native_mode", lambda *args, **kwargs: pytest.fail("per-worktree setup changed user preferences"))
    assert setup.main(["--client", "codex", "--project", str(project), "--apply"]) == 0
    capsys.readouterr()
    assert not calls[0][1]["codex_global_hooks"]
    assert not calls[0][1]["cursor_global_mcp"]
    assert not calls[0][1]["kimi_session_setup"]
    assert not (project / ".vaws-local/client-initialization.json").exists()

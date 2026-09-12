"""Grok Git callback scope and coexistence use real local repositories."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
sys.path.insert(0, str(ROOT / ".agents/scripts"))
from vaws_grok_setup_config import MARKER, plan_grok_setup
import vaws_grok_worktree as callback

spec = importlib.util.spec_from_file_location("grok_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
client_setup = importlib.util.module_from_spec(spec)
with patch("vaws_venv.ensure_workspace_interpreter"):
    spec.loader.exec_module(client_setup)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path):
    source = tmp_path / "source 用户's repo $shell"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README").write_text("fixture\n")
    git(source, "add", ".")
    git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    home = tmp_path / "grok home"
    target = home / "worktrees/project/new"
    target.parent.mkdir(parents=True)
    git(source, "worktree", "add", "--detach", str(target), "HEAD")
    return source, target, home, git(source, "rev-parse", "HEAD")


def test_only_new_native_git_worktree_qualifies(repository):
    source, target, home, revision = repository
    assert callback.creation_target(source, target, "0" * 40, revision, "1", home)
    assert not callback.creation_target(source, source, "0" * 40, revision, "1", home)
    assert not callback.creation_target(source, target, revision, revision, "1", home)
    assert not callback.creation_target(source, target, "0" * 40, revision, "0", home)
    assert not callback.creation_target(source, target, "0" * 40, revision, "1", home / "other")
    assert not callback.creation_target(source, target, "0" * 40, "bad-head", "1", home)


def test_unrelated_repository_is_not_adopted(repository, tmp_path):
    source, target, home, revision = repository
    other = home / "worktrees/unrelated"
    subprocess.run(["git", "clone", str(source), str(other)], check=True, capture_output=True)
    assert not callback.creation_target(source, other, "0" * 40, revision, "1", home)


def test_callback_prepares_before_return_and_clears_parent_pins(repository, monkeypatch, capsys):
    source, target, home, revision = repository
    monkeypatch.chdir(target)
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.setenv(callback.PIN_ENV, "parent-receipt")
    monkeypatch.setenv(callback.MANAGED_PIN_ENV, "parent-managed-receipt")
    calls = []
    def prepare(client, actual_source, actual_target):
        assert callback.PIN_ENV not in os.environ
        assert callback.MANAGED_PIN_ENV not in os.environ
        calls.append((client, actual_source, actual_target))
        return {"status": "ready", "environment": "fixed"}
    monkeypatch.setattr(callback, "prepare_worktree", prepare)
    assert callback.main(["--source", str(source), "0" * 40, revision, "1"]) == 0
    assert calls == [("grok", source, target)]
    assert '"status": "ready"' in capsys.readouterr().err
    assert git(target, "rev-parse", "HEAD") == revision
    assert git(source, "symbolic-ref", "--short", "HEAD") == "main"


def test_failure_returns_native_creation_error(repository, monkeypatch, capsys):
    source, target, home, revision = repository
    monkeypatch.chdir(target)
    monkeypatch.setenv("GROK_HOME", str(home))
    def fail(*args):
        raise RuntimeError("dependency preparation failed: fixture evidence")
    monkeypatch.setattr(callback, "prepare_worktree", fail)
    assert callback.main(["--source", str(source), "0" * 40, revision, "1"]) == 1
    assert "fixture evidence" in capsys.readouterr().err


def test_config_preserves_global_preferences_and_quotes_paths(repository, monkeypatch):
    source, target, home, revision = repository
    files, notes = {}, []
    executable = plan_grok_setup(files, notes, source, ROOT)
    hook = source / ".git/hooks/post-checkout"
    assert executable == [hook] and list(files) == [hook]
    assert MARKER in files[hook]
    assert all("config.toml" not in str(path) for path in files)
    assert notes[0]["reason"] == "grok-native-worktree-preferences"
    monkeypatch.setattr(client_setup, "BACKUP_DIR", home / "backups")
    plan = {"files": files, "executable_files": executable}
    applied = client_setup.apply_plan(plan)
    expected = files[hook].encode("utf-8")
    assert hook.read_bytes() == expected and b"\r\n" not in expected
    assert applied[0]["sha256"] == hashlib.sha256(expected).hexdigest()
    # A previous Windows text write must be repaired despite read_text's
    # newline normalization; the backup retains the exact original bytes.
    crlf = expected.replace(b"\n", b"\r\n")
    hook.write_bytes(crlf)
    repaired = client_setup.apply_plan(plan)
    assert hook.read_bytes() == expected
    assert Path(repaired[0]["backup"]).read_bytes() == crlf
    assert repaired[0]["sha256"] == hashlib.sha256(hook.read_bytes()).hexdigest()
    assert client_setup.apply_plan(plan) == []
    # Let Git select its native hook shell before removing the hook's tool PATH.
    # Ordinary checkout must return without starting Python or uv.
    client_setup.apply_plan({"files": {hook: files[hook].replace(
        "#!/bin/sh\n", "#!/bin/sh\nPATH=; export PATH\n", 1)}, "executable_files": executable})
    result = subprocess.run(["git", "-C", str(source), "checkout", "--detach", "--quiet", "HEAD"],
                            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    once = dict(files)
    assert plan_grok_setup(files, [], source, ROOT) == [hook]
    assert files == once


def test_all_preview_reports_owned_crlf_hook_repair(repository, monkeypatch, capsys):
    import vaws_client_inventory
    import vaws_native_mode_config

    source, target, home, revision = repository
    files, notes = {}, []
    executable = plan_grok_setup(files, notes, source, ROOT)
    hook = executable[0]
    crlf = files[hook].replace("\n", "\r\n").encode("utf-8")
    hook.write_bytes(crlf)
    monkeypatch.setattr(client_setup, "build_plan", lambda *args, **kwargs: {
        "files": files, "executable_files": executable, "notes": [], "mcp_servers": {},
        "launch_argv": None, "launch_cwd": str(source)})
    monkeypatch.setattr(vaws_client_inventory, "installed_clients",
                        lambda: {"grok": {"installed": True, "executable": None, "evidence": []}})
    monkeypatch.setattr(vaws_native_mode_config, "add_native_mode", lambda *args, **kwargs: None)
    monkeypatch.setattr(vaws_native_mode_config, "add_grok_import_dedup", lambda *args, **kwargs: None)
    assert client_setup.main(["--client", "all", "--project", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["clients"]["grok"]["files"] == [
        {"path": str(hook), "sha256": hashlib.sha256(files[hook].encode("utf-8")).hexdigest()}]
    assert hook.read_bytes() == crlf


@pytest.mark.parametrize("kind", ["hook", "hooks-path", "symlink"])
def test_existing_hook_owner_is_preserved(repository, tmp_path, kind):
    source, target, home, revision = repository
    hook = source / ".git/hooks/post-checkout"
    if kind == "hooks-path":
        git(source, "config", "core.hooksPath", str(tmp_path / "shared-hooks"))
    elif kind == "symlink":
        hook.symlink_to(tmp_path / "foreign-hook")
    else:
        hook.write_text("#!/bin/sh\necho user hook\n")
    files, notes = {}, []
    assert plan_grok_setup(files, notes, source, ROOT) == []
    assert files == {} and notes[0]["action"] == "preserved"


def test_target_configuration_does_not_retarget_shared_git_hook(repository):
    source, target, home, revision = repository
    files, notes = {}, []
    assert plan_grok_setup(files, notes, target, ROOT) == []
    assert files == {} and notes == []

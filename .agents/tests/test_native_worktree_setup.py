"""Native creation advances only a fresh linked worktree; no network or agents."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
spec = importlib.util.spec_from_file_location("native_worktree_setup", ROOT / ".agents/scripts/vaws_worktree_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(root, message):
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(root).parts}


def make_repository(tmp_path, monkeypatch, *, submodule=False, detached=False, mock_update=True):
    source, target, stage = [tmp_path / name for name in ("source 用户", "native worktree", "prepared")]
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / ".gitignore").write_text(".vaws-local/\n")
    (source / "README").write_text("old code\n")
    (source / "pyproject.toml").write_text("[tool.uv]\npackage = false\n")
    (source / "uv.lock").write_text("version = 1\n")
    module = None
    module_old = module_new = None
    if submodule:
        module = tmp_path / "module"
        module.mkdir()
        git(module, "init", "-b", "main")
        (module / "operator.py").write_text("old operator\n")
        module_old = commit(module, "old module")
        git(source, "-c", "protocol.file.allow=always", "submodule", "add", str(module), "vllm")
        git(source / "vllm", "checkout", "--detach", module_old)
    old = commit(source, "old workspace")
    git(source, "worktree", "add", *( ["--detach"] if detached else ["-b", "cursor/new-task"]), str(target), old)
    if submodule:
        # Initialize before the new component commit exists, so adoption must
        # import that commit from preparation's local object cache.
        git(target, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "vllm")
    git(source, "worktree", "add", "--detach", str(stage), old)
    (stage / "README").write_text("new code\n")
    (stage / "uv.lock").write_text("version = 1\n# new pinned components\n")
    if submodule:
        (module / "operator.py").write_text("new operator\n")
        module_new = commit(module, "new module")
        git(stage, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "vllm")
        git(stage / "vllm", "checkout", "--detach", module_new)
    new = commit(stage, "upstream workspace update")
    receipt_old = {"key": "old-environment", "input_id": setup._inputs(source)[3],
                   "python": sys.executable, "receipt": str(tmp_path / "old-receipt.json")}
    receipt_new = {"key": "new-environment", "input_id": setup._inputs(stage)[3],
                   "python": sys.executable, "receipt": str(tmp_path / "new-receipt.json")}
    calls = {"prepare": [], "configure": [], "select": [], "knowledge": []}

    def prepare(path, baseline=None):
        calls["prepare"].append(path)
        return {"status": "ready", "target": new}, stage

    def native(path):
        return receipt_new if setup._inputs(path)[3] == receipt_new["input_id"] else receipt_old

    def configure(client, path, receipt, environment):
        calls["configure"].append({"client": client, "path": path, "receipt": receipt,
                                   "head": git(path, "rev-parse", "HEAD"), "env": environment})

    def select(path, receipt):
        calls["select"].append((path, receipt))
        selection = path / ".vaws-local/environment-selection" / f"{sys.platform}.json"
        selection.parent.mkdir(parents=True, exist_ok=True)
        selection.write_text(json.dumps(receipt))

    if mock_update:
        monkeypatch.setattr(setup, "prepare_canonical", prepare)
    monkeypatch.setattr(setup, "native_ready", native)
    monkeypatch.setattr(setup, "saved_ready", lambda path: json.loads(
        (path / ".vaws-local/environment-selection" / f"{sys.platform}.json").read_text()))
    monkeypatch.setattr(setup, "configure_target", configure)
    monkeypatch.setattr(setup, "select_environment", select)
    monkeypatch.setattr(setup, "prepare_knowledge", lambda path, **kwargs:
                        calls["knowledge"].append((path, kwargs["receipt"])) or {"status": "ready", "ready": True})
    monkeypatch.setattr("vaws_local_owner.windows_mounted_workspace", lambda _: False)
    return SimpleNamespace(source=source, target=target, stage=stage, old=old, new=new,
                           module_old=module_old, module_new=module_new,
                           receipt_old=receipt_old, receipt_new=receipt_new, calls=calls)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    return make_repository(tmp_path, monkeypatch)


@pytest.mark.parametrize("client,detached", [("codex", True), ("cursor", False)])
def test_native_new_worktree_updates_code_and_environment_only(tmp_path, monkeypatch, client, detached):
    f = make_repository(tmp_path, monkeypatch, detached=detached)
    before, index = snapshot(f.source), (f.source / ".git/index").read_bytes()
    result = setup.prepare_worktree(client, f.source, f.target)
    assert result["head"] == f.new and result["environment"] == "new-environment"
    assert git(f.source, "rev-parse", "HEAD") == f.old
    assert snapshot(f.source) == before
    assert (f.source / ".git/index").read_bytes() == index
    assert f.calls["prepare"] == [f.source]
    assert f.calls["configure"][0]["head"] == f.new
    assert f.calls["configure"][0]["receipt"] == f.receipt_new
    assert f.calls["select"] == [(f.target, f.receipt_new)]
    assert f.calls["knowledge"] == [(f.target, f.receipt_new)]
    assert result["knowledge"] == {"status": "ready", "ready": True}


def test_explicit_old_revision_is_not_updated(fixture):
    f = fixture
    # The creation source is now ahead of the explicitly selected target.
    second = f.target.parent / "explicit old"
    git(f.source, "worktree", "add", "--detach", str(second), f.old)
    result = setup.prepare_worktree("codex", f.stage, second)
    assert result["head"] == f.old
    assert result["update"] == {"status": "kept", "reason": "explicit_source"}
    assert not f.calls["prepare"]


def default_snapshot_fixture(tmp_path, monkeypatch):
    f = make_repository(tmp_path, monkeypatch, detached=True, mock_update=False)
    git(f.source, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(f.source, "checkout", "-b", "feature/current-task")
    (f.source / "README").write_text("current business work\n")
    commit(f.source, "business source ahead of main")
    calls = []
    class Updater:
        def __init__(self, source):
            assert source == f.source
            self.state = {"phase": "ready", "target": f.new, "prepared": {"stage": str(f.stage)}}
        def step(self, **options):
            calls.append(options)
            # Preparation may cache upstream while the editing source is on
            # a business branch; neither activate nor for_session may be true.
            assert options == {"apply": True, "activate": False}
            return {"status": "ready", "branch": "main", "target": f.new}
        def validate_prepared(self, state, prepared):
            assert state is self.state and prepared == self.state["prepared"]
            return f.stage
    monkeypatch.setattr(setup, "workspace_entry", lambda _: {"state": "configured"})
    monkeypatch.setattr(setup, "WorkspaceUpdater", Updater)
    return f, calls


@pytest.mark.parametrize("source_at_default_tip", [False, True])
def test_codex_default_main_updates_while_source_has_unfinished_business_work(tmp_path, monkeypatch, source_at_default_tip):
    f, calls = default_snapshot_fixture(tmp_path, monkeypatch)
    if source_at_default_tip:
        git(f.source, "update-ref", "refs/heads/feature/current-task", f.old)
    (f.source / "README").write_text("staged work\n")
    git(f.source, "add", "README")
    (f.source / "README").write_text("unstaged work\n")
    before, index = snapshot(f.source), (f.source / ".git/index").read_bytes()
    source_head = git(f.source, "rev-parse", "HEAD")
    result = setup.prepare_worktree("codex", f.source, f.target)
    assert result["head"] == f.new and result["environment"] == "new-environment"
    assert result["update"]["baseline"] == {
        "kind": "local_default_branch_snapshot", "ref": "refs/heads/main", "head": f.old,
        "native_ref_selection": "unavailable"}
    assert calls == [{"apply": True, "activate": False}]
    assert not f.calls["prepare"]
    assert git(f.source, "rev-parse", "HEAD") == source_head
    assert snapshot(f.source) == before and (f.source / ".git/index").read_bytes() == index


@pytest.mark.parametrize("choice", ["old-commit", "business-commit", "named-branch", "unknown-default",
                                    "conflicting-defaults", "dirty", "selected", "fork"])
def test_default_snapshot_does_not_expand_existing_preservation_boundaries(tmp_path, monkeypatch, choice):
    f, calls = default_snapshot_fixture(tmp_path, monkeypatch)
    if choice == "old-commit":
        git(f.source, "update-ref", "refs/heads/main", f.new)
    elif choice == "business-commit":
        (f.target / "README").write_text("independent business commit\n")
        commit(f.target, "explicit business revision")
    elif choice == "named-branch":
        git(f.target, "checkout", "-b", "feature/explicit")
    elif choice == "unknown-default":
        git(f.source, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    elif choice == "conflicting-defaults":
        git(f.source, "symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/develop")
    elif choice == "dirty":
        (f.target / "README").write_text("unfinished target edit\n")
    elif choice in {"selected", "fork"}:
        path = (f.source if choice == "fork" else f.target) / ".vaws-local/environment-selection" / f"{sys.platform}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(f.receipt_old))
    head, content = git(f.target, "rev-parse", "HEAD"), (f.target / "README").read_bytes()
    result = setup.prepare_worktree("codex", f.source, f.target, preserve_source=choice == "fork")
    assert git(f.target, "rev-parse", "HEAD") == head
    assert (f.target / "README").read_bytes() == content
    assert result["environment"] == "old-environment"
    assert not calls and not f.calls["prepare"]


@pytest.mark.parametrize("change", ["edit-during-prepare", "diverged-upstream", "disabled"])
def test_default_snapshot_adoption_still_requires_clean_fast_forward(tmp_path, monkeypatch, change):
    f, calls = default_snapshot_fixture(tmp_path, monkeypatch)
    if change == "edit-during-prepare":
        original_prepare = setup.prepare_canonical
        def intervening_edit(*args):
            result = original_prepare(*args)
            (f.target / "README").write_text("edit during native setup\n")
            return result
        monkeypatch.setattr(setup, "prepare_canonical", intervening_edit)
    elif change == "diverged-upstream":
        # The local default tip has a commit absent from canonical history.
        (f.target / "README").write_text("local default commit\n")
        original = commit(f.target, "local default diverged")
        git(f.source, "update-ref", "refs/heads/main", original)
    else:
        monkeypatch.setattr(setup, "workspace_entry", lambda _: {"state": "disabled"})
    head = git(f.target, "rev-parse", "HEAD")
    result = setup.prepare_worktree("codex", f.source, f.target)
    assert result["head"] == head and result["environment"] == "old-environment"
    if change == "edit-during-prepare":
        assert result["update"]["reason"] == "dirty_checkout"
        assert (f.target / "README").read_text() == "edit during native setup\n"
    elif change == "diverged-upstream":
        assert result["update"]["reason"] == "command_failed"
    else:
        assert result["update"]["state"] == "disabled" and not calls


@pytest.mark.parametrize("client", ["codex", "cursor", "claude", "grok", "kimi"])
def test_new_session_uses_canonical_when_mother_has_business_commits_and_edits(tmp_path, monkeypatch, client):
    f, updates = default_snapshot_fixture(tmp_path, monkeypatch)
    business = git(f.source, "rev-parse", "HEAD")
    # The native client has just created its new directory from mother HEAD.
    git(f.target, "reset", "--hard", business)
    (f.source / "README").write_text("staged business work\n")
    git(f.source, "add", "README")
    (f.source / "README").write_text("unstaged business work\n")
    (f.source / "untracked.txt").write_text("untracked business work\n")
    before, index = snapshot(f.source), (f.source / ".git/index").read_bytes()
    result = setup.prepare_worktree(client, f.source, f.target)
    assert result["head"] == f.new and result["environment"] == "new-environment"
    assert result["update"]["baseline"] == {
        "kind": "source_head_snapshot", "head": business, "native_ref_selection": "unavailable"}
    assert updates == [{"apply": True, "activate": False}]
    assert git(f.source, "rev-parse", "HEAD") == business
    assert git(f.source, "branch", "--show-current") == "feature/current-task"
    assert snapshot(f.source) == before and (f.source / ".git/index").read_bytes() == index


def test_dirty_native_worktree_keeps_edits_without_checking_updates(fixture):
    f = fixture
    (f.target / "README").write_text("user draft\n")
    result = setup.prepare_worktree("cursor", f.source, f.target)
    assert result["head"] == f.old and result["update"]["reason"] == "dirty_checkout"
    assert (f.target / "README").read_text() == "user draft\n"
    assert not f.calls["prepare"]


def test_edit_during_preparation_stays_untouched(fixture, monkeypatch):
    f = fixture
    def intervening_edit(_):
        (f.target / "README").write_text("edit while preparing\n")
        return {"status": "ready"}, f.stage
    monkeypatch.setattr(setup, "prepare_canonical", intervening_edit)
    result = setup.prepare_worktree("cursor", f.source, f.target)
    assert result["head"] == f.old and result["update"]["reason"] == "dirty_checkout"
    assert result["environment"] == "old-environment"


def test_repeat_setup_uses_saved_environment_and_repairs_wiring(fixture, monkeypatch):
    f = fixture
    setup.prepare_worktree("cursor", f.source, f.target)
    selected = (f.target / ".vaws-local/environment-selection" / f"{sys.platform}.json").read_bytes()
    (f.target / "uv.lock").write_text("a user draft that is not valid TOML\n")
    monkeypatch.setattr(setup, "prepare_canonical", lambda *_: pytest.fail("repeat must not check upstream"))
    monkeypatch.setattr(setup, "native_ready", lambda _: pytest.fail("repeat must not resolve changed dependency inputs"))
    result = setup.prepare_worktree("cursor", f.source, f.target)
    assert result["status"] == "reused" and result["environment"] == "new-environment"
    assert len(f.calls["configure"]) == 2
    assert len(f.calls["select"]) == 1
    assert f.calls["knowledge"] == [(f.target, f.receipt_new)]
    assert (f.target / ".vaws-local/environment-selection" / f"{sys.platform}.json").read_bytes() == selected


def test_failure_does_not_publish_selection(fixture, monkeypatch):
    f = fixture
    monkeypatch.setattr(setup, "configure_target", lambda *args: (_ for _ in ()).throw(RuntimeError("wiring failed")))
    with pytest.raises(RuntimeError, match="wiring failed"):
        setup.prepare_worktree("cursor", f.source, f.target)
    assert not f.calls["select"]


def test_selection_written_by_sync_still_repairs_interrupted_wiring(fixture):
    f = fixture
    selection = f.target / ".vaws-local/environment-selection" / f"{sys.platform}.json"
    selection.parent.mkdir(parents=True)
    selection.write_text(json.dumps(f.receipt_old))
    result = setup.prepare_worktree("cursor", f.source, f.target)
    assert result["status"] == "reused" and result["environment"] == "old-environment"
    assert not f.calls["prepare"] and not f.calls["select"]
    assert f.calls["configure"][0]["head"] == f.old
    assert f.calls["knowledge"] == [(f.target, f.receipt_old)]


def test_inherited_environment_prepares_knowledge_once_for_the_new_worktree(fixture):
    f = fixture
    source_selection = f.source / ".vaws-local/environment-selection" / f"{sys.platform}.json"
    source_selection.parent.mkdir(parents=True)
    source_selection.write_text(json.dumps(f.receipt_old))
    first = setup.prepare_worktree("kimi", f.source, f.target, preserve_source=True)
    assert first["update"]["reason"] == "fork_source"
    assert first["knowledge"]["ready"] is True
    second = setup.prepare_worktree("kimi", f.source, f.target, preserve_source=True)
    assert second["knowledge"] == first["knowledge"]
    assert f.calls["knowledge"] == [(f.target, f.receipt_old)]
    assert not f.calls["prepare"]


def test_pending_knowledge_is_reported_without_retrying_native_setup(fixture, monkeypatch):
    f = fixture
    pending = {"status": "pending", "ready": False, "reason": "offline corpus"}
    calls = []
    monkeypatch.setattr(setup, "prepare_knowledge", lambda *args, **kwargs: calls.append(args) or pending)
    first = setup.prepare_worktree("codex", f.source, f.target)
    assert first["status"] == "ready" and first["knowledge"] == pending
    second = setup.prepare_worktree("codex", f.source, f.target)
    assert second["status"] == "reused" and second["knowledge"] == pending
    assert calls == [(f.target,)]


def test_kimi_worktree_wiring_does_not_enable_an_unconfirmed_extension(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    receipt = {"python": sys.executable, "receipt": "/selected/receipt.json"}
    setup.configure_target("kimi", tmp_path, receipt, {})
    argv, options = calls[0]
    assert "--kimi-session-setup" not in argv
    assert argv[-2:] == ["--kimi-config", str(tmp_path / ".vaws-local/kimi-hooks.toml")]
    assert options["env"][setup.PIN_ENV] == receipt["receipt"]


def test_malformed_optional_identity_does_not_block_local_setup(fixture):
    f = fixture
    identity = f.source / ".vaws-local/github.json"
    identity.parent.mkdir()
    identity.write_text("{invalid JSON")
    result = setup.prepare_worktree("cursor", f.source, f.target)
    assert result["status"] == "ready" and result["identity"]["status"] == "unavailable"
    assert f.calls["configure"]


def test_initialized_components_follow_exact_gitlinks_without_changing_source(tmp_path, monkeypatch):
    f = make_repository(tmp_path, monkeypatch, submodule=True)
    before = snapshot(f.source)
    transfers = []
    actual_git = setup.git
    def local_git(path, *args, **kwargs):
        if "fetch" in args:
            transfers.append(args)
            assert args == ("fetch", "--no-tags", str(f.stage / "vllm"), f.module_new)
        return actual_git(path, *args, **kwargs)
    monkeypatch.setattr(setup, "git", local_git)
    result = setup.prepare_worktree("cursor", f.source, f.target)
    assert result["submodules"] == {"vllm": f.module_new}
    assert git(f.target / "vllm", "rev-parse", "HEAD") == f.module_new
    assert git(f.source / "vllm", "rev-parse", "HEAD") == f.module_old
    assert snapshot(f.source) == before
    assert transfers


def test_uninitialized_components_are_not_downloaded(tmp_path, monkeypatch):
    f = make_repository(tmp_path, monkeypatch, submodule=True)
    other = tmp_path / "not initialized"
    git(f.source, "worktree", "add", "--detach", str(other), f.old)
    result = setup.prepare_worktree("codex", f.source, other)
    assert result["head"] == f.new and result["submodules"] == {}
    assert not (other / "vllm/.git").exists()


def test_dirty_component_prevents_parent_advance(tmp_path, monkeypatch):
    f = make_repository(tmp_path, monkeypatch, submodule=True)
    (f.target / "vllm/operator.py").write_text("user operator draft\n")
    result = setup.prepare_worktree("cursor", f.source, f.target)
    assert result["head"] == f.old and result["update"]["reason"] == "dirty_checkout"
    assert not f.calls["prepare"]


@pytest.mark.parametrize("client", ["codex", "cursor"])
def test_native_paths_accept_actual_linked_worktree(fixture, client):
    f = fixture
    env = ({"CODEX_SOURCE_TREE_PATH": str(f.source), "CODEX_WORKTREE_PATH": str(f.target)}
           if client == "codex" else {"ROOT_WORKTREE_PATH": str(f.source)})
    assert setup.native_paths(client, env, f.target) == (f.source, f.target)


@pytest.mark.parametrize("client,env", [("codex", {}), ("cursor", {}),
                                        ("codex", {"CODEX_SOURCE_TREE_PATH": "source"})])
def test_native_paths_require_native_environment(fixture, client, env):
    with pytest.raises(ValueError, match="missing"):
        setup.native_paths(client, env, fixture.target)


def test_main_checkout_cannot_be_native_target(fixture):
    f = fixture
    with pytest.raises(ValueError, match="main checkout"):
        setup.native_paths("cursor", {"ROOT_WORKTREE_PATH": str(f.target)}, f.source)
    with pytest.raises(ValueError, match="separate"):
        setup.native_paths("cursor", {"ROOT_WORKTREE_PATH": str(f.source)}, f.source)


def test_native_paths_reject_different_repository(fixture, tmp_path):
    other = tmp_path / "unrelated"
    other.mkdir()
    git(other, "init", "-b", "main")
    with pytest.raises(ValueError, match="same Git"):
        setup.native_paths("cursor", {"ROOT_WORKTREE_PATH": str(other)}, fixture.target)


def test_mounted_windows_worktree_requires_native_owner(fixture, monkeypatch):
    monkeypatch.setattr("vaws_local_owner.windows_mounted_workspace", lambda _: True)
    with pytest.raises(ValueError, match="Windows owner"):
        setup.prepare_worktree("cursor", fixture.source, fixture.target)
    assert not fixture.calls["prepare"]


def test_ready_fallback_prepares_packages_only_without_parent_pins(fixture, monkeypatch):
    f = fixture
    monkeypatch.setenv(setup.PIN_ENV, "parent-environment")
    monkeypatch.setenv("VAWS_CONTEXT_FILE", "parent-task")
    monkeypatch.setattr(setup, "native_ready", lambda _: (_ for _ in ()).throw(setup.EnvironmentError("missing")))
    calls = []
    monkeypatch.setattr(setup, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or
                        SimpleNamespace(stdout=json.dumps({"receipt": f.receipt_old})))
    assert setup.ready_for_target(f.source, f.target, setup.unpinned_environment()) == f.receipt_old
    argv, options = calls[0]
    assert argv[-3:] == ["sync", "--packages-only", "--locked"]
    assert options["cwd"] == f.target
    assert setup.PIN_ENV not in options["env"] and "VAWS_CONTEXT_FILE" not in options["env"]

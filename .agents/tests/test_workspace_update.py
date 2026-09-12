"""Real Git branch safety with fixture remotes; no GitHub writes or NPU work."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import vaws_workspace_update as updates

REAL_PREPARE = updates.WorkspaceUpdater.prepare


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            encoding="utf-8", env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(root, content):
    (root / "README").write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", content)
    return git(root, "rev-parse", "HEAD")


class API:
    def __init__(self):
        self.calls = []
        self.owner_type = "User"

    def api(self, endpoint):
        self.calls.append(endpoint)
        if endpoint == "user":
            return {"type": "User", "login": "alice", "id": 123}
        if endpoint == f"repos/{updates.CANONICAL}":
            return {"full_name": updates.CANONICAL, "default_branch": "stable"}
        if endpoint.endswith("/releases/latest"):
            pytest.fail("updates must not require a GitHub Release")
        return {"full_name": "alice/vllm-ascend-workspace", "fork": True,
                "owner": {"login": "alice", "type": self.owner_type},
                "parent": {"full_name": updates.CANONICAL}}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "stable")
    (upstream / ".gitignore").write_text(".vaws-local/\n", encoding="utf-8")
    old = commit(upstream, "before")
    fork = tmp_path / "fork.git"
    git(tmp_path, "clone", "--bare", str(upstream), str(fork))
    root = tmp_path / "workspace 中文"
    git(tmp_path, "clone", str(fork), str(root))
    url = "https://github.com/alice/vllm-ascend-workspace.git"
    git(root, "remote", "set-url", "origin", url)
    released = commit(upstream, "released")
    git(upstream, "tag", "v1.0.0")
    new = commit(upstream, "unreleased main development")
    updates.write_json(root / ".vaws-local/github.json", {"schema": "vaws.github.v1", "login": "alice"})
    api = API()
    real_run = updates.run
    calls = []

    def local_run(argv, **kwargs):
        calls.append(list(argv))
        # Exercise the real fetch and push protocols against local Git repositories.
        # Stored remotes retain their GitHub identities for the policy checks.
        actual = [str(upstream) if arg == updates.UPSTREAM else str(fork) if arg == url else arg for arg in argv]
        return real_run(actual, **kwargs)

    monkeypatch.setattr(updates, "run", local_run)
    monkeypatch.setattr("vaws_github.GitHubClient", lambda: api)
    prepares = []

    def prepare(self, release, active):
        prepares.append(release["target"])
        stage = self.base / "releases" / release["target"]
        if not stage.exists():
            stage.parent.mkdir(parents=True, exist_ok=True)
            git(root, "worktree", "add", "--detach", str(stage), release["target"])
        receipt = self.base / "fixture-receipt.json"
        updates.write_json(receipt, {})
        return {"stage": str(stage), "receipt": {"receipt": str(receipt)},
                "knowledge": {"ready": False, "status": "pending"}}

    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", prepare)
    activated = []
    monkeypatch.setattr(updates.WorkspaceUpdater, "activate_environment", lambda self, prepared: activated.append(prepared))
    return {"root": root, "upstream": upstream, "fork": fork, "old": old, "new": new, "released": released,
            "api": api, "calls": calls, "prepares": prepares, "activated": activated, "url": url}


def updater(fixture):
    return updates.WorkspaceUpdater(fixture["root"], client=fixture["api"])


def test_apply_uses_default_branch_head_even_with_older_release_tag(fixture):
    result = updater(fixture).step(apply=True)
    assert result["status"] == "applied"
    assert git(fixture["root"], "rev-parse", "HEAD") == fixture["new"]
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["new"]
    assert fixture["new"] != fixture["released"]
    assert result["branch"] == "stable"
    assert not any("releases" in endpoint for endpoint in fixture["api"].calls)
    assert fixture["activated"]
    assert all("--force" not in call and "reset" not in call and "stash" not in call for call in fixture["calls"])


def test_prepare_command_syncs_fork_once_without_mutating_live_checkout(fixture, capsys):
    script = Path(__file__).resolve().parents[1] / "scripts/workspace_update.py"
    spec = importlib.util.spec_from_file_location("workspace_update_command", script)
    command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(command)
    assert command.main(["--root", str(fixture["root"]), "prepare"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert git(fixture["root"], "rev-parse", "HEAD") == fixture["old"]
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["new"]
    assert not fixture["activated"]
    assert command.main(["--root", str(fixture["root"]), "prepare"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"
    assert len(fixture["prepares"]) == 1


def test_prepare_follows_new_default_branch_commits_without_tags(fixture):
    root = fixture["root"]
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    next_head = commit(fixture["upstream"], "next untagged change")
    result = updater(fixture).step(apply=True, activate=False)
    assert result["status"] == "ready"
    assert result["target"] == next_head
    assert result["channel"] == "default_branch"
    assert result["url"].endswith(f"/commit/{next_head}")
    assert fixture["prepares"] == [fixture["new"], next_head]
    assert git(fixture["fork"], "rev-parse", "stable") == next_head
    assert git(root, "rev-parse", "HEAD") == fixture["old"]
    assert not fixture["activated"]


@pytest.mark.parametrize("change, reason", [("dirty", "dirty_checkout"), ("branch", "working_branch")])
def test_session_skips_preparation_when_it_would_keep_business_source(fixture, change, reason):
    root = fixture["root"]
    if change == "dirty":
        (root / "README").write_text("unfinished session edit", encoding="utf-8")
    else:
        git(root, "checkout", "-b", "business")
    result = updater(fixture).step(apply=True, activate=False, for_session=True)
    assert result["status"] == "deferred"
    assert result["reason"] == reason
    assert not fixture["prepares"]
    assert not fixture["activated"]
    assert git(root, "rev-parse", "HEAD") == fixture["old"]
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["old"]
    assert fixture["api"].calls.count(f"repos/{updates.CANONICAL}") == 1


def test_successful_preparation_and_reuse_clear_stale_failure_but_keep_logs(fixture, monkeypatch):
    base = fixture["root"] / ".vaws-local/updates"
    log = base / "logs/previous-failure.json"
    evidence = {"stderr": "previous download failed"}
    updates.write_json(log, evidence)
    updates.write_json(base / "state.json", {
        "status": "pending", "reason": "no_stable_release", "error_log": str(log)})
    original = updates.WorkspaceUpdater.prepare

    def prepare(self, release, active):
        state = updates.read_json(base / "state.json")
        assert state["status"] == state["phase"] == "preparing"
        assert "reason" not in state and "error_log" not in state
        return original(self, release, active)

    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", prepare)
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    state = updates.read_json(base / "state.json")
    assert state["status"] == state["phase"] == "ready"
    assert "reason" not in state and "error_log" not in state
    # A transient check failure after readiness must also clear on reuse.
    updates.write_json(base / "state.json", {
        **state, "status": "deferred", "reason": "network_unavailable", "error_log": str(log)})
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    state = updates.read_json(base / "state.json")
    assert state["status"] == "ready"
    assert "reason" not in state and "error_log" not in state
    assert len(fixture["prepares"]) == 1
    assert updates.read_json(log) == evidence


def test_updates_follow_the_saved_github_assigned_fork_name(fixture, monkeypatch):
    name = "alice/vllm-ascend-workspace-1"
    url = f"https://github.com/{name}.git"
    git(fixture["root"], "remote", "set-url", "origin", url)
    updates.write_json(fixture["root"] / ".vaws-local/github.json",
                      {"schema": "vaws.github.v1", "login": "alice", "forks": {"workspace": name}})
    original_api = fixture["api"].api
    def api(endpoint):
        response = original_api(endpoint)
        return {**response, "full_name": name} if endpoint == f"repos/{name}" else response
    fixture["api"].api = api
    original_run = updates.run
    monkeypatch.setattr(updates, "run", lambda argv, **kwargs: original_run(
        [fixture["url"] if arg == url else arg for arg in argv], **kwargs))
    assert updater(fixture).step(apply=True)["status"] == "applied"
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["new"]
    assert f"repos/{name}" in fixture["api"].calls


def test_prepare_preserves_dirty_business_checkout(fixture):
    root = fixture["root"]
    git(root, "checkout", "-b", "business")
    business_head = commit(root, "local business commit")
    (root / "README").write_text("staged business edit", encoding="utf-8")
    git(root, "add", "README")
    (root / "README").write_text("unfinished business edit", encoding="utf-8")
    (root / "notes.txt").write_text("untracked notes", encoding="utf-8")
    staged = git(root, "diff", "--cached")
    working = git(root, "diff")
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["new"]
    assert git(root, "rev-parse", "HEAD") == business_head
    assert git(root, "symbolic-ref", "--short", "HEAD") == "business"
    assert git(root, "diff", "--cached") == staged
    assert git(root, "diff") == working
    assert (root / "notes.txt").read_text(encoding="utf-8") == "untracked notes"
    assert updates.prepared_source(root) is None
    checked = updater(fixture).step(apply=False)
    assert checked["status"] == "available"
    assert checked["local_apply_deferred"] == "working_branch"


def test_prepared_activation_is_offline(fixture):
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    fixture["calls"].clear()
    fixture["api"].api = lambda *_: pytest.fail("activation made a GitHub request")
    assert updates.activate_prepared(fixture["root"])["status"] == "applied"
    assert not any(call[1] in ("fetch", "push", "clone") for call in fixture["calls"])


@pytest.mark.parametrize("condition,reason", [("dirty", "dirty_checkout"), ("branch", "working_branch"),
                                             ("diverge", "local_not_fast_forward"), ("merge", "git_operation_in_progress")])
def test_preserves_local_work(fixture, condition, reason):
    root = fixture["root"]
    if condition == "dirty":
        (root / "README").write_text("unfinished", encoding="utf-8")
    elif condition == "branch":
        git(root, "checkout", "-b", "business")
    elif condition == "diverge":
        commit(root, "personal commit")
    else:
        (root / ".git/MERGE_HEAD").write_text(fixture["old"], encoding="utf-8")
    before = git(root, "rev-parse", "HEAD")
    result = updater(fixture).step(apply=True)
    assert result["reason"] == reason
    assert git(root, "rev-parse", "HEAD") == before
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["old"]
    assert not fixture["prepares"]


def test_fork_divergence_defers_without_force(fixture):
    # A personal remote commit must never be overwritten, even with a clean clone.
    root = fixture["root"]
    personal = commit(root, "remote personal commit")
    git(root, "push", str(fixture["fork"]), f"{personal}:refs/heads/stable")
    result = updater(fixture).step(apply=True)
    assert result["reason"] == "fork_not_fast_forward"
    assert git(fixture["fork"], "rev-parse", "stable") == personal


def test_check_works_without_any_tags_or_releases_and_does_not_prepare(fixture):
    git(fixture["upstream"], "tag", "-d", "v1.0.0")
    result = updater(fixture).step()
    assert result["status"] == "available"
    assert result["target"] == fixture["new"]
    assert not fixture["prepares"]
    assert git(fixture["root"], "rev-parse", "HEAD") == fixture["old"]
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["old"]


def test_unconfigured_is_normal_pending_without_network(fixture):
    (fixture["root"] / ".vaws-local/github.json").unlink()
    fixture["api"].calls.clear()
    result = updater(fixture).step(apply=True)
    assert result["reason"] == "github_identity_required"
    assert not fixture["api"].calls
    assert not (fixture["root"] / ".vaws-local/updates/logs").exists()


def test_non_personal_fork_never_pushes(fixture):
    fixture["api"].owner_type = "Organization"
    assert updater(fixture).step(apply=True)["status"] == "deferred"
    assert not any(call[1] == "push" for call in fixture["calls"])


def test_multiple_push_targets_are_rejected(fixture):
    git(fixture["root"], "config", "--add", "remote.origin.pushurl", fixture["url"])
    git(fixture["root"], "config", "--add", "remote.origin.pushurl", "https://github.com/other/repo.git")
    assert updater(fixture).step(apply=True)["reason"] == "personal_origin_required"


def test_expanded_origin_url_cannot_be_rewritten_to_another_fork(fixture):
    root = fixture["root"]
    git(root, "remote", "set-url", "origin", "git@github.com:alice/vllm-ascend-workspace.git")
    git(root, "config", "url.https://github.com/alice/.insteadOf", "git@github.com:alice/")
    git(root, "config", "url.https://github.com/other/.pushInsteadOf", "https://github.com/alice/")
    # Discovery sees a legitimate personal URL, but Git expands that explicit
    # URL again when it is subsequently used as the destination of a push.
    assert git(root, "remote", "get-url", "--push", "origin") == fixture["url"]
    git(root, "remote", "add", "explicit-url-probe", fixture["url"])
    assert git(root, "remote", "get-url", "--push", "explicit-url-probe").startswith("https://github.com/other/")
    result = updater(fixture).step(apply=True)
    assert result["reason"] == "git_url_rewrite"
    assert not any(call[1] == "push" for call in fixture["calls"])


def test_push_url_rewrites_are_rechecked_after_preparation(fixture, monkeypatch):
    original = updates.WorkspaceUpdater.prepare
    def prepare(self, release, active):
        value = original(self, release, active)
        git(self.root, "config", "url.https://github.com/other/.pushInsteadOf", "https://github.com/alice/")
        return value
    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", prepare)
    result = updater(fixture).step(apply=True)
    assert result["reason"] == "git_url_rewrite"
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["old"]
    assert not any(call[1] == "push" for call in fixture["calls"])


def test_prepare_failure_preserves_local_and_remote_and_records_evidence(fixture, monkeypatch):
    def failing(*_):
        raise updates.Deferred("package_download_failed", "fixture download unavailable")
    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", failing)
    result = updater(fixture).step(apply=True)
    assert result["reason"] == "package_download_failed"
    assert Path(result["log"]).is_file()
    assert git(fixture["root"], "rev-parse", "HEAD") == fixture["old"]
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["old"]


def test_push_retry_reuses_prepared_dependencies(fixture, monkeypatch):
    original = updates.run
    failed = False
    def run(argv, **kwargs):
        nonlocal failed
        if argv[:2] == ["git", "push"] and not failed:
            failed = True
            raise updates.Deferred("temporary_push_failure")
        return original(argv, **kwargs)
    monkeypatch.setattr(updates, "run", run)
    assert updater(fixture).step(apply=True, activate=False)["reason"] == "temporary_push_failure"
    assert len(fixture["prepares"]) == 1
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    assert len(fixture["prepares"]) == 1
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["new"]
    assert git(fixture["root"], "rev-parse", "HEAD") == fixture["old"]


def test_edit_during_preparation_defers_activation_and_push(fixture, monkeypatch):
    original = updates.WorkspaceUpdater.prepare
    def prepare(self, release, active):
        value = original(self, release, active)
        (self.root / "README").write_text("user edited while preparing", encoding="utf-8")
        return value
    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", prepare)
    assert updater(fixture).step(apply=True)["reason"] == "dirty_checkout"
    assert git(fixture["root"], "rev-parse", "HEAD") == fixture["old"]
    assert git(fixture["fork"], "rev-parse", "stable") == fixture["old"]


def test_activation_waits_if_user_edited_after_prepare(fixture):
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    (fixture["root"] / "README").write_text("user editing", encoding="utf-8")
    assert updates.activate_prepared(fixture["root"])["reason"] == "dirty_checkout"
    assert not fixture["activated"]


def test_update_lock_is_shared_across_linked_worktrees(fixture):
    root = fixture["root"]
    linked = root.parent / "linked"
    git(root, "worktree", "add", "--detach", str(linked), "HEAD")
    with updates.update_lock(root):
        with pytest.raises(updates.Deferred, match="updater_running"):
            with updates.update_lock(linked):
                pytest.fail("both publishers obtained the same repository lock")


def test_new_session_waits_for_another_preparation_then_continues(fixture, monkeypatch, capsys):
    waiting, acquired = threading.Event(), threading.Event()
    errors = []
    sleep = time.sleep

    def observe_wait(seconds):
        waiting.set()
        sleep(seconds)

    def next_session():
        try:
            with updates.update_lock(fixture["root"], wait_seconds=5):
                acquired.set()
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(updates.time, "sleep", observe_wait)
    with updates.update_lock(fixture["root"]):
        worker = threading.Thread(target=next_session)
        worker.start()
        assert waiting.wait(5), "the second session did not reach the shared preparation lock"
        assert not acquired.is_set()
    worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert acquired.is_set()
    assert capsys.readouterr().err.count("waiting for another session") == 1


def test_preparation_wait_timeout_keeps_lock_and_elapsed_evidence(fixture):
    with updates.update_lock(fixture["root"]):
        with pytest.raises(updates.Deferred, match="preparation is still running") as failure:
            with updates.update_lock(fixture["root"], wait_seconds=0.02):
                pytest.fail("a timed-out waiter acquired another session's lock")
    assert failure.value.reason == "updater_running"
    assert Path(failure.value.evidence["lock"]).name == "vaws-update.lock"
    assert failure.value.evidence["waited_seconds"] >= 0.02


def test_git_failure_log_retains_stderr_without_token(fixture):
    subject = updater(fixture)
    failure = updates.Deferred("command_failed", "authentication failed ghp_secret123",
                               evidence={"stderr": "already redacted"})
    result = subject.failure(failure)
    assert "ghp_" not in result["detail"]
    assert json.loads(Path(result["log"]).read_text())["stderr"] == "already redacted"


def test_command_timeout_keeps_captured_diagnostics(fixture, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output=b"download progress", stderr=b"retry ghp_secret123")
    monkeypatch.setattr(updates.subprocess, "run", timeout)
    with pytest.raises(updates.Deferred) as failure:
        updates.run([sys.executable, "fixture-download.py"], cwd=fixture["root"], timeout=1)
    result = updater(fixture).failure(failure.value)
    evidence = json.loads(Path(result["log"]).read_text(encoding="utf-8"))
    assert result["reason"] == "command_timeout"
    assert evidence["stdout"] == "download progress"
    assert evidence["stderr"] == "retry [redacted]"
    assert evidence["timeout"] == 1


@pytest.fixture
def subfixture(fixture):
    root, upstream = fixture["root"], fixture["upstream"]
    source = root.parent / "module"
    source.mkdir()
    git(source, "init", "-b", "main")
    module_old = commit(source, "module before")
    module_new = commit(source, "module released")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "vllm")
    git(root / "vllm", "checkout", "--detach", module_old)
    base = commit(root, "workspace with old module")
    git(upstream, "fetch", str(root), base)
    git(upstream, "checkout", "-B", "stable", base)
    git(upstream, "update-index", "--cacheinfo", f"160000,{module_new},vllm")
    git(upstream, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "released module pin")
    target = git(upstream, "rev-parse", "HEAD")
    return {**fixture, "old": base, "new": target, "module_old": module_old, "module_new": module_new}


def test_submodule_follows_exact_gitlink(subfixture):
    result = updater(subfixture).step(apply=True)
    assert result["status"] == "applied"
    root = subfixture["root"]
    assert git(root / "vllm", "rev-parse", "HEAD") == subfixture["module_new"]
    assert not git(root, "status", "--porcelain")


@pytest.mark.parametrize("kind,reason", [("dirty", "dirty_checkout"), ("branch", "working_branch"),
                                       ("local_commit", "submodule_local_commit")])
def test_submodule_user_work_is_preserved(subfixture, kind, reason):
    child = subfixture["root"] / "vllm"
    if kind == "dirty":
        (child / "README").write_text("unfinished", encoding="utf-8")
    elif kind == "branch":
        git(child, "checkout", "-b", "business")
    else:
        commit(child, "local detached commit")
    before = git(child, "rev-parse", "HEAD")
    assert updater(subfixture).step(apply=True)["reason"] == reason
    assert git(child, "rev-parse", "HEAD") == before


def test_uninitialized_submodule_stays_uninitialized(subfixture):
    root = subfixture["root"]
    git(root, "submodule", "deinit", "vllm")
    assert updater(subfixture).step(apply=True)["status"] == "applied"
    assert not updates.initialized(root, "vllm")
    assert not any("--remote" in call for call in subfixture["calls"])


def test_interrupted_submodule_activation_can_resume(subfixture, monkeypatch):
    root = subfixture["root"]
    assert updater(subfixture).step(apply=True, activate=False)["status"] == "ready"
    # Simulate interruption after the superproject FF, before its child checkout.
    git(root, "merge", "--ff-only", subfixture["new"])
    state_path = root / ".vaws-local/updates/state.json"
    state = updates.read_json(state_path)
    state["phase"] = "local_updated"
    updates.write_json(state_path, state)
    subfixture["api"].api = lambda *_: pytest.fail("offline resume queried GitHub")
    assert updates.activate_prepared(root)["status"] == "applied"
    assert git(root / "vllm", "rev-parse", "HEAD") == subfixture["module_new"]


def test_wsl_windows_mount_requires_single_native_owner(monkeypatch):
    monkeypatch.setattr("vaws_local_owner.windows_mounted_workspace", lambda _: True)
    with pytest.raises(updates.Deferred, match="native Windows owner"):
        with updates.update_lock(Path("/mnt/c/workspace")):
            pytest.fail("WSL acquired a separate lock on a Windows checkout")


def test_prepared_source_is_read_only_and_preserves_business_sources(fixture):
    root = fixture["root"]
    assert updates.prepared_source(root) is None
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    stage = updates.prepared_source(root)
    assert stage and git(stage, "rev-parse", "HEAD") == fixture["new"]
    assert git(root, "rev-parse", "HEAD") == fixture["old"]
    git(root, "checkout", "-b", "business")
    assert updates.prepared_source(root) is None


def test_prepare_uses_target_scripts_locked_pins_and_deploy_only(fixture, monkeypatch):
    root = fixture["root"]
    monkeypatch.setenv("VAWS_ENV_RECEIPT", "old-client-pin")
    monkeypatch.setenv("VAWS_TOP_FROM", "local-custom-monitor")
    commands = []
    original = updates.run
    receipt = root / ".vaws-local/prepared-env.json"
    updates.write_json(receipt, {})

    def run(argv, **kwargs):
        if len(argv) > 1 and argv[1].endswith(("vaws_deps.py", "manage_monitor.py")):
            commands.append((argv, {**kwargs, "env": dict(kwargs["env"])}))
            if argv[1].endswith("vaws_deps.py"):
                payload = {"ok": True, "receipt": {"receipt": str(receipt)}, "knowledge": {"ready": False}}
            else:
                payload = {"ok": True}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        return original(argv, **kwargs)

    monkeypatch.setattr(updates, "run", run)
    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", REAL_PREPARE)
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    assert commands[0][0][-2:] == ["sync", "--locked"]
    assert commands[1][0][-1] == "deploy"
    assert "VAWS_ENV_RECEIPT" not in commands[0][1]["env"]
    assert commands[1][1]["env"]["VAWS_ENV_RECEIPT"] == str(receipt)
    assert "VAWS_TOP_FROM" not in commands[1][1]["env"]
    assert fixture["new"] in commands[0][0][1]
    assert git(root, "rev-parse", "HEAD") == fixture["old"]


def test_prepared_source_keeps_initialized_submodule_and_personal_remotes(subfixture, monkeypatch):
    root = subfixture["root"]
    child = root / "vllm"
    git(child, "remote", "set-url", "origin", "https://github.com/alice/vllm.git")
    git(child, "config", "remote.origin.pushurl", "git@github.com:alice/vllm.git")
    git(child, "remote", "add", "upstream", "https://github.com/vllm-project/vllm.git")
    git(child, "remote", "add", "extra", "https://example.invalid/mirror.git")
    receipt = root / ".vaws-local/prepared-env.json"
    updates.write_json(receipt, {})
    original = updates.run
    def run(argv, **kwargs):
        if len(argv) > 1 and argv[1].endswith("vaws_deps.py"):
            data = {"ok": True, "receipt": {"receipt": str(receipt)}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        if len(argv) > 1 and argv[1].endswith("manage_monitor.py"):
            return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")
        mapped = [str(root.parent / "module") if arg == "https://github.com/vllm-project/vllm.git" else arg for arg in argv]
        return original(mapped, **kwargs)
    monkeypatch.setattr(updates, "run", run)
    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", REAL_PREPARE)
    assert updater(subfixture).step(apply=True, activate=False)["status"] == "ready"
    stage = updates.prepared_source(root)
    assert stage is not None
    assert updates.initialized(stage, "vllm")
    assert git(stage / "vllm", "rev-parse", "HEAD") == subfixture["module_new"]
    assert git(child, "rev-parse", "HEAD") == subfixture["module_old"]
    assert git(stage / "vllm", "remote", "get-url", "origin") == "https://github.com/alice/vllm.git"
    assert git(stage / "vllm", "remote", "get-url", "--push", "origin") == "git@github.com:alice/vllm.git"
    assert git(stage / "vllm", "remote", "get-url", "extra") == "https://example.invalid/mirror.git"
    # A staging child edit must never become the source of a new editing copy.
    (stage / "vllm/README").write_text("modified staging child", encoding="utf-8")
    assert updates.prepared_source(root) is None

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
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import vaws_workspace_update as updates

REAL_PREPARE = updates.WorkspaceUpdater.prepare


def git(root, *args):
    result = subprocess.run(["git", "-c", "core.longpaths=true", "-C", str(root), *args], stdin=subprocess.DEVNULL, timeout=30, capture_output=True, text=True,
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
    (upstream / ".gitignore").write_text(".vaws-local/\nvllm/\nvllm-ascend/\n", encoding="utf-8")
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
        normalized = [arg for i, arg in enumerate(argv) if not (arg == "core.longpaths=true" or (arg == "-c" and i + 1 < len(argv) and argv[i + 1] == "core.longpaths=true"))]
        calls.append(normalized)
        # Exercise the real fetch and push protocols against local Git repositories.
        # Stored remotes retain their GitHub identities for the policy checks.
        actual = [str(upstream) if arg == updates.UPSTREAM else str(fork) if arg == url else arg for arg in argv]
        return real_run(actual, **kwargs)

    monkeypatch.setattr(updates, "run", local_run)
    monkeypatch.setattr("vaws_github.GitHubClient", lambda: api)
    prepares = []

    def prepare(self, release, active):
        prepares.append(release["target"])
        stage = self.stage_path(release, active)
        if not stage.exists():
            stage.parent.mkdir(parents=True, exist_ok=True)
            git(root, "clone", "--local", "--no-checkout", str(root), str(stage))
            git(stage, "checkout", "--detach", release["target"])
        receipt = self.base / "fixture-receipt.json"
        updates.write_json(receipt, {})
        return {"stage": str(stage), "receipt": {"receipt": str(receipt)},
                "sources": {}, "revisions": {"workspace": release["target"]}, "source_channel": self.source_channel}

    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", prepare)
    activated = []
    monkeypatch.setattr(updates.WorkspaceUpdater, "activate_environment", lambda self, prepared: activated.append(prepared))
    return {"root": root, "upstream": upstream, "fork": fork, "old": old, "new": new, "released": released,
            "api": api, "calls": calls, "prepares": prepares, "activated": activated, "url": url}


def updater(fixture):
    return updates.WorkspaceUpdater(fixture["root"], client=fixture["api"])


def test_repository_root_finds_nested_directory_and_linked_gitfile(fixture):
    root = fixture["root"]
    target = root.parent / "linked"
    git(root, "worktree", "add", "--detach", str(target), "HEAD")
    for checkout in (root, target):
        nested = checkout / "nested/source"
        nested.mkdir(parents=True)
        assert updates.repository_root(nested) == checkout.resolve()


@pytest.mark.parametrize("kind", ["directory", "gitfile", "dangling_link"])
def test_repository_root_never_skips_broken_nearest_git_boundary(fixture, kind):
    root = fixture["root"]
    before = ((root / ".git/HEAD").read_bytes(), (root / ".git/index").read_bytes(),
              (root / ".git/config").read_bytes())
    boundary = root / "nested checkout"
    nested = boundary / "source"
    nested.mkdir(parents=True)
    if kind == "directory":
        (boundary / ".git").mkdir()
    elif kind == "gitfile":
        (boundary / ".git").write_text("gitdir: missing-git-storage\n", encoding="utf-8")
    else:
        try:
            (boundary / ".git").symlink_to("missing-git-storage")
        except OSError:
            pytest.skip("symlink creation is unavailable")
    with pytest.raises(updates.Deferred):
        updates.repository_root(nested)
    after = ((root / ".git/HEAD").read_bytes(), (root / ".git/index").read_bytes(),
             (root / ".git/config").read_bytes())
    assert after == before


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
    checked = updater(fixture).step(apply=False)
    assert checked["status"] == "available"
    assert checked["local_apply_deferred"] == "working_branch"


def test_prepared_activation_is_offline(fixture):
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    fixture["calls"].clear()
    fixture["api"].api = lambda *_: pytest.fail("activation made a GitHub request")
    assert updater(fixture).activate()["status"] == "applied"
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
        if argv[0] == "git" and "push" in argv and not failed:
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
    with pytest.raises(updates.Deferred) as error:
        updater(fixture).activate()
    assert error.value.reason == "dirty_checkout"
    assert (fixture["root"] / "README").read_text(encoding="utf-8") == "user editing"
    assert git(fixture["root"], "rev-parse", "HEAD") == fixture["old"]
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

    # Git subprocess waits also use time.sleep; only observe the lock's waits.
    local_time = Mock(wraps=time)
    local_time.sleep = observe_wait
    monkeypatch.setattr(updates, "time", local_time)
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


def test_prepare_uses_only_target_locked_packages(fixture, monkeypatch):
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
                payload = {"ok": True, "receipt": {"receipt": str(receipt)}}
            else:
                payload = {"ok": True}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        return original(argv, **kwargs)

    monkeypatch.setattr(updates, "run", run)
    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", REAL_PREPARE)
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    assert len(commands) == 1
    assert commands[0][0][-2:] == ["sync", "--locked"]
    assert "VAWS_ENV_RECEIPT" not in commands[0][1]["env"]
    assert "VAWS_TOP_FROM" not in commands[0][1]["env"]
    assert git(Path(commands[0][0][1]).parents[2], "rev-parse", "HEAD") == fixture["new"]
    assert git(root, "rev-parse", "HEAD") == fixture["old"]


def business_fixture(fixture, monkeypatch):
    root, upstream = fixture["root"], fixture["upstream"]
    child = root / "vllm"
    child.mkdir()
    git(child, "init", "-b", "operator-work")
    before = commit(child, "operator before")
    after = commit(child, "operator after")
    git(child, "checkout", "operator-work")
    git(child, "reset", "--hard", before)
    git(child, "remote", "add", "origin", "https://github.com/alice/vllm.git")
    git(child, "config", "remote.origin.pushurl", "git@github.com:alice/vllm.git")
    git(child, "config", "--add", "remote.origin.fetch", "+refs/custom/*:refs/custom/*")
    lock = {"schema_version": 1,
            "vllm-ascend": {"repository": "vllm-project/vllm-ascend", "revision": after},
            "vllm": {"repository": "vllm-project/vllm", "development": {"revision": after},
                     "release": {"tag": "v0.28.0", "revision": before}}}
    (upstream / "sources.lock.json").write_text(json.dumps(lock))
    fixture["new"] = commit(upstream, "locked business sources")
    receipt = root / ".vaws-local/prepared-env.json"
    updates.write_json(receipt, {})
    original = updates.run
    packages = []
    def run(argv, **kwargs):
        if len(argv) > 1 and argv[1].endswith("vaws_deps.py"):
            packages.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "receipt": {"receipt": str(receipt)}}), "")
        return original(argv, **kwargs)
    monkeypatch.setattr(updates, "run", run)
    monkeypatch.setattr(updates.WorkspaceUpdater, "prepare", REAL_PREPARE)
    return child, before, after, packages


def test_preparation_uses_lock_in_independent_sources_and_preserves_actual_work(fixture, monkeypatch):
    child, before, after, packages = business_fixture(fixture, monkeypatch)
    (child / "README").write_text("staged operator")
    git(child, "add", "README")
    (child / "README").write_text("working operator")
    staged, working = git(child, "diff", "--cached"), git(child, "diff")
    subject = updater(fixture)
    assert subject.step(apply=True, activate=False)["status"] == "ready"
    stage = subject.validate_prepared(subject.state, subject.state["prepared"])
    prepared_child = stage / "vllm"
    assert git(prepared_child, "rev-parse", "HEAD") == after
    assert git(child, "rev-parse", "HEAD") == before
    assert git(child, "diff", "--cached") == staged and git(child, "diff") == working
    assert (stage / ".git").is_dir() and (prepared_child / ".git").is_dir()
    assert not (prepared_child / ".git/objects/info/alternates").exists()
    assert git(prepared_child, "config", "--get-all", "remote.origin.fetch") == git(child, "config", "--get-all", "remote.origin.fetch")
    assert git(prepared_child, "remote", "get-url", "--push", "origin") == "git@github.com:alice/vllm.git"
    assert len(packages) == 1 and packages[0][-2:] == ["sync", "--locked"]
    assert subject.activate()["status"] == "applied"
    assert git(child, "rev-parse", "HEAD") == before and git(child, "diff") == working


def test_prepared_child_tamper_never_reuses_ready(fixture, monkeypatch):
    child, before, after, packages = business_fixture(fixture, monkeypatch)
    subject = updater(fixture)
    assert subject.step(apply=True, activate=False)["status"] == "ready"
    stage = Path(subject.state["prepared"]["stage"])
    (stage / "vllm/README").write_text("edited cache")
    directories = set(stage.parent.iterdir())
    assert updater(fixture).step(apply=True, activate=False)["reason"] == "dirty_checkout"
    assert updater(fixture).step(apply=True, activate=False)["reason"] == "dirty_checkout"
    assert set(stage.parent.iterdir()) == directories
    assert (stage / "vllm/README").read_text() == "edited cache"
    assert not fixture["activated"]


def test_explicit_release_and_development_have_distinct_complete_caches(fixture, monkeypatch):
    child, before, after, packages = business_fixture(fixture, monkeypatch)
    development = updater(fixture)
    assert development.step(apply=True, activate=False)["status"] == "ready"
    release = updates.WorkspaceUpdater(fixture["root"], client=fixture["api"], source_channel="release")
    assert release.step(apply=True, activate=False)["status"] == "ready"
    dev_stage = Path(development.state["prepared"]["stage"])
    release_stage = Path(release.state["prepared"]["stage"])
    assert dev_stage != release_stage
    assert git(dev_stage / "vllm", "rev-parse", "HEAD") == after
    assert git(release_stage / "vllm", "rev-parse", "HEAD") == before


def test_failed_child_clone_retries_in_new_stage_without_altering_failed_files(fixture, monkeypatch):
    import vaws_native_workspace as copying
    child, before, after, packages = business_fixture(fixture, monkeypatch)
    original = copying.prepare_source
    def fail(destination, **kwargs):
        if destination.name == "vllm":
            destination.mkdir()
            (destination / "partial.txt").write_text("injected incomplete clone")
            raise RuntimeError("injected source failure")
        return original(destination, **kwargs)
    monkeypatch.setattr(copying, "prepare_source", fail)
    subject = updater(fixture)
    assert subject.step(apply=True, activate=False)["status"] == "deferred"
    assert not packages and not fixture["activated"]
    assert subject.state["phase"] == "preparing"
    previous = subject.stage_path(subject.state, subject.preparation_inputs(subject.state))
    partial = previous / "vllm/partial.txt"
    assert partial.read_text() == "injected incomplete clone"
    (previous / "diagnosis.txt").write_text("keep user diagnosis in failed staging directory")
    monkeypatch.setattr(copying, "prepare_source", original)
    retry = updater(fixture)
    retried = retry.step(apply=True, activate=False)
    assert retried["status"] == "ready", retried
    prepared = retry.state["prepared"]
    assert Path(prepared["stage"]) != previous
    assert len(Path(prepared["stage"]).name) == len(previous.name) == 32
    assert retry.state["retained_failed_stage"] == str(previous)
    assert partial.read_text() == "injected incomplete clone"
    assert (previous / "diagnosis.txt").read_text() == "keep user diagnosis in failed staging directory"
    assert len(packages) == 1
    assert git(Path(prepared["stage"]) / "vllm", "rev-parse", "HEAD") == after
    reused = updater(fixture)
    assert reused.step(apply=True, activate=False)["status"] == "ready"
    assert reused.state["prepared"]["stage"] == prepared["stage"] and len(packages) == 1
    from vaws_native_workspace import create_workspace
    from vaws_workspace_entry import write_preparation
    root_only = fixture["root"].parent / "root-only-donor"
    copied = create_workspace(fixture["root"], root_only, sources={})
    write_preparation(root_only, project_root=fixture["root"], native_workspace=root_only,
                      workspace=root_only, sources=copied["sources"])
    other = updates.WorkspaceUpdater(fixture["root"], source_root=root_only, client=fixture["api"])
    assert other.step(apply=True, activate=False)["status"] == "ready"
    returned = updater(fixture)
    assert returned.step(apply=True, activate=False)["status"] == "ready"
    assert returned.state["prepared"]["stage"] == prepared["stage"]
    assert partial.read_text() == "injected incomplete clone"
    assert git(child, "rev-parse", "HEAD") == before


def test_recovery_never_replaces_a_published_stage_reference(fixture):
    subject = updater(fixture)
    assert subject.step(apply=True, activate=False)["status"] == "ready"
    stage = Path(subject.state["prepared"]["stage"])
    subject.save(phase="preparing")
    subject.failure(updates.Deferred("injected_preparation_failure"))
    retry = updater(fixture)
    assert retry.step(apply=True, activate=False)["reason"] == "prepared_stage_referenced"
    assert retry.state["prepared"]["stage"] == str(stage)
    assert len(list(stage.parent.iterdir())) == 1


def test_root_only_prepare_never_reads_source_lock(fixture, monkeypatch):
    monkeypatch.setattr("vaws_source_lock.selected_sources", lambda *args: pytest.fail("root-only read source lock"))
    assert updater(fixture).step(apply=True, activate=False)["status"] == "ready"
    assert updater(fixture).state["prepared"]["sources"] == {}


def test_path_lock_works_without_git_and_other_tasks_are_independent(tmp_path):
    one, two = tmp_path / "one/start.lock", tmp_path / "two/start.lock"
    with updates.path_lock(one):
        with updates.path_lock(two):
            pass
        with pytest.raises(updates.Deferred):
            with updates.path_lock(one, wait_seconds=0):
                pytest.fail("same task acquired its lock twice")


def test_same_owner_donors_reuse_canonical_cache_and_keep_explicit_root_only(fixture, monkeypatch):
    from vaws_native_workspace import create_workspace
    from vaws_workspace_entry import write_preparation
    child, before, after, packages = business_fixture(fixture, monkeypatch)
    owner = fixture["root"]
    sibling = owner / "vllm-ascend"
    git(child, "clone", "--local", str(child), str(sibling))
    donors = []
    for index in range(3):
        donor = owner.parent / f"donor-{index}"
        copied = create_workspace(owner, donor, sources={"vllm": child, "vllm-ascend": sibling})
        selected = copied["sources"] if index != 2 else {"workspace": str(donor)}
        write_preparation(donor, project_root=owner, native_workspace=donor, workspace=donor, sources=selected)
        donors.append(donor)
    one = updates.WorkspaceUpdater(owner, source_root=donors[0], client=fixture["api"])
    assert one.step(apply=True, activate=False)["status"] == "ready"
    two = updates.WorkspaceUpdater(owner, source_root=donors[1], client=fixture["api"])
    assert two.step(apply=True, activate=False)["status"] == "ready"
    assert one.base == two.base == owner / ".vaws-local/updates"
    assert one.state["prepared"]["stage"] == two.state["prepared"]["stage"]
    assert len(packages) == 1
    root_only = updates.WorkspaceUpdater(owner, source_root=donors[2], client=fixture["api"])
    assert root_only.step(apply=True, activate=False)["status"] == "ready"
    prepared = root_only.state["prepared"]
    assert prepared["sources"] == {} and set(prepared["revisions"]) == {"workspace"}
    assert not (Path(prepared["stage"]) / "vllm").exists()
    assert not (Path(prepared["stage"]) / "vllm-ascend").exists()
    assert len(packages) == 2
    assert all(not (donor / ".vaws-local/updates").exists() for donor in donors)

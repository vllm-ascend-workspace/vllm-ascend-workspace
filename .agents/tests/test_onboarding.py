"""First-use decisions, interrupted setup and revocation use real local state."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

import vaws_community as community
import vaws_onboarding as onboarding
import vaws_workspace_entry as entry


class GitHub:
    def __init__(self):
        self.calls = []

    def api(self, endpoint):
        self.calls.append(endpoint)
        assert endpoint == "user"
        return {"login": "alice", "id": 12, "type": "User"}

    def ensure_star(self, repository):
        self.calls.append("star")
        return {"state": "starred"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import vaws_local_state
    monkeypatch.setattr(vaws_local_state, "shared_workspace_root", lambda root: Path(root))
    monkeypatch.setattr(onboarding, "prepare_network", lambda root: {"status": "ready", "network_checked": True})
    calls = []
    account = GitHub()
    def runner(command, root, environment):
        calls.append(command)
        assert environment["VAWS_COMMUNITY_POLICY"] == str(root / ".vaws-local/community.json")
        if "sync" in command:
            return {"ok": True, "receipt": {"python": "fixed-python", "key": "fixed"}}
        return {"state": "configured"}
    def knowledge(root, decision, login, **kwargs):
        calls.append(["knowledge", decision, login])
        assert community.read_choice(root)["decision"] == decision
        return {"configured": True}
    monkeypatch.setattr(onboarding, "configure_knowledge", knowledge)
    monkeypatch.setattr(onboarding, "prepare_knowledge_runtime", lambda *args: {"state": "ready"})
    monkeypatch.setattr(onboarding, "prepare_knowledge_reference", lambda *args: {"ready": True})
    monkeypatch.setattr(onboarding, "configure_reporting", lambda *args: calls.append(["worker"]) or {"state": "running"})
    return tmp_path, account, calls, runner


def initialize(setup, **kwargs):
    root, account, calls, runner = setup
    return onboarding.initialize(root, github=account, runner=runner, **kwargs)


def test_unanswered_choices_do_not_create_fork_star_or_enable_contribution(setup):
    root, account, calls, _ = setup
    result = initialize(setup, github_user="alice")
    assert result["state"] == "needs_choices"
    assert account.calls == calls == []
    assert not (root / ".vaws-local/community.json").exists()
    assert not (root / ".vaws-local/github.json").exists()


def test_status_identity_only_preserves_username_but_still_needs_choices(setup):
    root, account, calls, _ = setup
    snapshot = root / ".vaws-local/github.json"
    snapshot.parent.mkdir()
    snapshot.write_text('{"schema":"vaws.github.v1","login":"alice","forks":{}}')
    original = snapshot.read_bytes()
    result = onboarding.status(root)
    assert result["state"] == "needs_choices" and result["github_user"] == "alice"
    assert result["choices"] == {} and result["network_checked"] is False
    assert snapshot.read_bytes() == original
    assert account.calls == calls == []


def test_declined_features_keep_identity_and_no_upload_worker(setup):
    root, account, calls, _ = setup
    result = initialize(setup, github_user="alice", fork=False, star=False, community="disabled")
    assert result["state"] == "ready"
    assert account.calls == []
    assert ["worker"] not in calls
    assert json.loads((root / ".vaws-local/github.json").read_text())["forks"] == {}
    assert entry.workspace_entry(root)["state"] == "configured"


def test_ready_reuses_answers_without_auth_install_write_or_star(setup):
    root, account, calls, _ = setup
    first = initialize(setup, github_user="alice", fork=False, star=True, community="enabled")
    assert first["state"] == "ready"
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in (root / ".vaws-local/onboarding.json", root / ".vaws-local/community.json")}
    counts = len(account.calls), len(calls)
    second = initialize(setup)
    assert second["reused"] is True
    assert (len(account.calls), len(calls)) == counts
    assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}


def test_resume_preserves_selected_client(setup):
    root, account, calls, _ = setup
    assert initialize(setup, github_user="alice", fork=False, star=False, community="disabled", client="codex")["state"] == "ready"
    previous_calls = list(calls)
    assert initialize(setup)["reused"] is True
    assert calls == previous_calls


def test_optional_service_failure_does_not_block_task_setup_and_retries_only_that_stage(setup, monkeypatch):
    root, account, calls, _ = setup
    def unavailable(*args):
        raise RuntimeError("service unavailable")
    monkeypatch.setattr(onboarding, "configure_reporting", unavailable)
    first = initialize(setup, github_user="alice", fork=False, star=False, community="enabled")
    assert first["state"] == "ready" and first["pending_optional"] == ["reporting"]
    assert first["collaboration_state"] == "pending"
    assert entry.workspace_entry(root)["state"] == "configured"
    old_calls = list(calls)
    monkeypatch.setattr(onboarding, "configure_reporting", lambda *args: {"state": "running"})
    second = initialize(setup)
    assert second["pending_optional"] == [] and second["collaboration_state"] == "configured"
    assert calls == old_calls and account.calls == []


@pytest.mark.parametrize("decision", ["enabled", "disabled"])
def test_init_prepares_local_knowledge_independently_of_contribution(setup, monkeypatch, decision):
    prepared = []
    monkeypatch.setattr(onboarding, "prepare_knowledge_runtime", lambda root, receipt:
                        prepared.append((root, receipt["key"])) or {"state": "ready"})
    first = initialize(setup, github_user="alice", fork=False, star=False, community=decision)
    assert first["steps"]["knowledge_runtime"]["state"] == "ready"
    assert prepared == [(setup[0], "fixed")]
    assert initialize(setup)["reused"] is True
    assert prepared == [(setup[0], "fixed")]


def test_knowledge_install_and_reporter_overlap_with_separate_durations(setup, monkeypatch):
    rendezvous = threading.Barrier(2, timeout=5)
    def prepare(*args):
        rendezvous.wait()
        return {"state": "ready"}
    def report(*args):
        rendezvous.wait()
        return {"state": "running"}
    monkeypatch.setattr(onboarding, "prepare_knowledge_runtime", prepare)
    monkeypatch.setattr(onboarding, "configure_reporting", report)
    result = initialize(setup, github_user="alice", fork=False, star=False, community="enabled")
    assert result["pending_optional"] == []
    for name in ("knowledge_runtime", "reporting"):
        assert result["steps"][name]["seconds"] >= 0


def test_missing_optional_owner_does_not_install_again_or_block_local_task(setup, monkeypatch):
    def fail(*args):
        raise RuntimeError("offline package registry")
    monkeypatch.setattr(onboarding, "prepare_knowledge_runtime", fail)
    first = initialize(setup, github_user="alice", fork=False, star=False, community="enabled")
    assert first["state"] == "ready"
    assert first["pending_optional"] == ["knowledge_runtime", "knowledge_reference", "knowledge"]
    assert entry.workspace_entry(setup[0])["state"] == "configured"
    assert not any(command[0] == "knowledge" for command in setup[2])
    before = list(setup[2])
    monkeypatch.setattr(onboarding, "prepare_knowledge_runtime", lambda *args: {"state": "ready"})
    resumed = initialize(setup)
    assert resumed["pending_optional"] == []
    assert setup[2] == before + [["knowledge", "enabled", "alice"]]


def test_reference_preparation_failure_preserves_ready_runtime_and_local_task(setup, monkeypatch):
    attempts = []
    def unavailable(*args):
        attempts.append("reference")
        raise RuntimeError("model download unavailable")
    monkeypatch.setattr(onboarding, "prepare_knowledge_reference", unavailable)
    first = initialize(setup, github_user="alice", fork=False, star=False, community="disabled")
    assert first["pending_optional"] == ["knowledge_reference"]
    assert first["steps"]["knowledge_runtime"]["state"] == "ready"
    assert entry.workspace_entry(setup[0])["state"] == "configured"
    assert attempts == ["reference"]
    monkeypatch.setattr(onboarding, "prepare_knowledge_runtime", lambda *args: pytest.fail("runtime prepared twice"))
    monkeypatch.setattr(onboarding, "prepare_knowledge_reference", lambda *args: {"ready": True})
    assert initialize(setup)["pending_optional"] == []


def test_failure_after_identity_is_incomplete_and_resumes_only_failed_stages(setup, monkeypatch):
    root, account, calls, runner = setup
    def failing(command, root, environment):
        raise RuntimeError("package network failure")
    failed = onboarding.initialize(root, github=account, runner=failing, github_user="alice",
                                    fork=False, star=False, community="disabled")
    assert failed["state"] == "pending" and failed["phase"] == "dependencies"
    assert (root / ".vaws-local/github.json").exists()
    assert entry.workspace_entry(root)["state"] == "setup_pending"
    result = initialize(setup)
    assert result["state"] == "ready"
    assert account.calls == []


def test_revocation_happens_before_failing_reconfiguration_and_keeps_other_workspace(setup, monkeypatch):
    root, account, calls, runner = setup
    assert initialize(setup, github_user="alice", fork=False, star=False, community="enabled")["state"] == "ready"
    original = community.read_choice(root)
    other = root / "unrelated"
    other_choice = community.write_choice(other, "enabled")
    def failing(root, decision, login, **kwargs):
        assert community.read_choice(root)["decision"] == "disabled"
        raise RuntimeError("unavailable owner")
    monkeypatch.setattr(onboarding, "configure_knowledge", failing)
    result = initialize(setup, community="disabled")
    assert result["state"] == "ready" and result["collaboration_state"] == "pending"
    assert community.read_choice(root)["workspace_id"] == original["workspace_id"]
    assert community.read_choice(root)["revision"] != original["revision"]
    assert community.read_choice(other) == other_choice


def test_choice_is_not_implicitly_authorized_and_revision_is_idempotent(setup):
    root, *_ = setup
    assert community.read_choice(root) is None
    enabled = community.write_choice(root, "enabled")
    assert community.write_choice(root, "enabled") == enabled
    disabled = community.write_choice(root, "disabled")
    reenabled = community.write_choice(root, "enabled")
    assert len({enabled["revision"], disabled["revision"], reenabled["revision"]}) == 3
    assert enabled["workspace_id"] == disabled["workspace_id"] == reenabled["workspace_id"]


def test_malformed_choice_fails_closed_without_replacing_prior_record(setup):
    root, *_ = setup
    path = root / ".vaws-local/community.json"
    path.parent.mkdir()
    path.write_text('{"schema":"vaws.community.v1","decision":true}')
    original = path.read_bytes()
    with pytest.raises(ValueError, match="invalid"):
        community.write_choice(root, "enabled")
    assert path.read_bytes() == original


def test_task_policy_comes_from_explicit_owner_not_inherited_environment(setup, monkeypatch):
    root, *_ = setup
    from vaws_community import community_environment, local_policy_path
    env = community_environment(root, {"VAWS_COMMUNITY_POLICY": "foreign", "CUSTOM": "keep"})
    assert env == {"VAWS_COMMUNITY_POLICY": str(root / ".vaws-local/community.json"), "CUSTOM": "keep"}
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("bootstrap spawned a process"))
    assert local_policy_path(root) == root / ".vaws-local/community.json"


def test_opt_out_before_setup_does_not_require_fork_star_answers_or_auth(setup):
    root, account, calls, _ = setup
    state = root / ".vaws-local"
    state.mkdir()
    (state / "github.json").write_text('{"schema":"vaws.github.v1","login":"alice"}')
    config = state / "knowledge/service.json"
    config.parent.mkdir()
    config.write_text('{"publishing":{"enabled":true,"fork":"alice/references"},"custom":"preserve"}')
    result = initialize(setup, community="disabled")
    assert result["state"] == "choice_updated"
    assert community.read_choice(root)["decision"] == "disabled"
    assert json.loads(config.read_text())["publishing"]["enabled"] is False
    assert json.loads(config.read_text())["custom"] == "preserve"
    assert account.calls == []
    assert calls == []


def test_resume_never_reenables_a_revoked_choice_from_old_onboarding_record(setup):
    root, account, calls, _ = setup
    assert initialize(setup, github_user="alice", fork=False, star=False, community="enabled")["state"] == "ready"
    revoked = community.write_choice(root, "disabled")
    count = len(account.calls)
    resumed = initialize(setup)
    assert resumed["state"] == "ready"
    assert resumed["choices"]["community"] == "disabled"
    assert community.read_choice(root) == revoked
    assert len(account.calls) == count


def test_dependency_change_reconfigures_consumers_without_repeating_github_choices(setup):
    root, account, calls, _ = setup
    assert initialize(setup, github_user="alice", fork=False, star=True, community="enabled")["state"] == "ready"
    calls.clear()
    (root / "uv.lock").write_text("updated exact dependency closure")
    result = initialize(setup)
    assert result["state"] == "ready" and not result["reused"]
    assert account.calls == ["user", "star"]
    assert len(calls) == 4  # dependency, client, knowledge, reporter
    calls.clear()
    assert initialize(setup)["reused"] is True
    assert calls == []


def test_disabled_knowledge_does_not_prepare_or_launch_an_owner(tmp_path, monkeypatch):
    import vaws_local_state
    import vaws_knowledge_service
    monkeypatch.setattr(vaws_local_state, "shared_workspace_root", lambda root: Path(root))
    monkeypatch.setattr(vaws_knowledge_service, "_run_knowledge", lambda *a, **k: pytest.fail("unneeded owner launch"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("unneeded owner preparation"))
    result = onboarding.configure_knowledge(tmp_path, "disabled", "alice")
    assert result["owner_prepared"] is False
    config = tmp_path / ".vaws-local/knowledge/service.json"
    assert not config.exists()
    config.parent.mkdir(parents=True)
    config.write_text('{"publishing":{"enabled":true}}')
    onboarding.configure_knowledge(tmp_path, "disabled", "alice")
    assert json.loads(config.read_text())["publishing"]["enabled"] is False


@pytest.mark.parametrize("section", ["shared_sync", "publishing"])
def test_reenabling_community_keeps_selected_knowledge_corpus(tmp_path, monkeypatch, section):
    import vaws_knowledge_service
    config = tmp_path / ".vaws-local/knowledge/service.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({section: {"repository": "example/selected-corpus"}}))
    calls = []
    monkeypatch.setattr(vaws_knowledge_service, "run_knowledge_cli", lambda root, command: calls.append(command) or (0, {"ready": True}))
    onboarding.configure_knowledge(tmp_path, "enabled", "alice")
    assert calls[0][calls[0].index("--repository") + 1] == "example/selected-corpus"


@pytest.mark.parametrize("progress", ["{", "[]", '{"schema":"invalid","steps":{}}'])
def test_opt_out_precedes_invalid_progress_and_preserves_its_bytes(setup, progress):
    root, account, calls, _ = setup
    enabled = community.write_choice(root, "enabled")
    record = root / ".vaws-local/onboarding.json"
    record.write_text(progress)
    original = record.read_bytes()
    config = root / ".vaws-local/knowledge/service.json"
    config.parent.mkdir()
    config.write_text('{"publishing":{"enabled":true},"custom":"keep"}')
    result = initialize(setup, community="disabled")
    assert result["state"] == "choice_updated" and result["setup_state"] == "repair_required"
    assert result["phase"] == "progress"
    assert record.read_bytes() == original
    revoked = community.read_choice(root)
    assert revoked["decision"] == "disabled" and revoked["revision"] != enabled["revision"]
    assert revoked["workspace_id"] == enabled["workspace_id"]
    assert json.loads(config.read_text())["publishing"]["enabled"] is False
    assert json.loads(config.read_text())["custom"] == "keep"
    assert account.calls == calls == []


@pytest.mark.parametrize("forked", [False, True])
def test_ready_missing_identity_recovers_only_saved_label_without_network(setup, forked):
    root, account, calls, _ = setup
    first = initialize(setup, github_user="alice", fork=False, star=False, community="disabled")
    record = root / ".vaws-local/onboarding.json"
    if forked:
        # A prior success receipt is not a freshly verified fork or numeric ID.
        first["choices"]["fork"] = True
        first["steps"]["fork"]["result"] = {"status": "configured", "repositories": [{"personal": "alice/corpus"}]}
        record.write_text(json.dumps(first))
    snapshot = root / ".vaws-local/github.json"
    snapshot.unlink()
    before = record.read_bytes()
    counts = len(account.calls), len(calls)
    assert onboarding.status(root)["state"] == "pending"
    result = initialize(setup)
    assert result["state"] == "ready" and result["reused"] is True
    assert json.loads(snapshot.read_text()) == {"schema": "vaws.github.v1", "login": "alice", "forks": {}}
    assert record.read_bytes() == before
    assert onboarding.status(root)["state"] == "ready"
    assert (len(account.calls), len(calls)) == counts


@pytest.mark.parametrize("identity", [
    "{", '{"schema":"vaws.github.v1","login":""}',
    '{"schema":"vaws.github.v1","login":"alice","forks":[]}',
    '{"schema":"vaws.github.v1","login":"alice","github_user_id":"12"}',
    '{"schema":"vaws.github.v1","login":"bob"}',
])
def test_ready_invalid_identity_is_preserved_for_repair_without_false_ready(setup, identity):
    root, account, calls, _ = setup
    initialize(setup, github_user="alice", fork=False, star=False, community="enabled")
    snapshot = root / ".vaws-local/github.json"
    snapshot.write_text(identity)
    record = root / ".vaws-local/onboarding.json"
    before = snapshot.read_bytes(), record.read_bytes()
    counts = len(account.calls), len(calls)
    result = initialize(setup)
    assert result["state"] == "pending" and result["phase"] == "identity"
    assert result["setup_state"] == "repair_required"
    assert (snapshot.read_bytes(), record.read_bytes()) == before
    revoked = initialize(setup, community="disabled")
    assert revoked["state"] == "choice_updated" and revoked["setup_state"] == "repair_required"
    assert community.read_choice(root)["decision"] == "disabled"
    assert (snapshot.read_bytes(), record.read_bytes()) == before
    assert (len(account.calls), len(calls)) == counts


@pytest.mark.parametrize("state,steps", [("unknown", {}), ("ready", {}), ("pending", {"fork": "invalid"})])
def test_progress_reader_rejects_false_ready_and_uses_explicit_path_without_discovery(tmp_path, monkeypatch, state, steps):
    record = tmp_path / "progress.json"
    record.write_text(json.dumps({"schema": onboarding.SCHEMA, "state": state, "steps": steps}))
    monkeypatch.setattr(onboarding, "record_path", lambda *args: pytest.fail("unneeded owner discovery"))
    with pytest.raises(ValueError, match="invalid"):
        onboarding.read_record(tmp_path, path=record)


def test_missing_policy_never_reauthorizes_from_saved_enabled_choice(setup):
    root, account, calls, _ = setup
    initialize(setup, github_user="alice", fork=False, star=False, community="enabled")
    record = root / ".vaws-local/onboarding.json"
    before = record.read_bytes()
    community.policy_path(root).unlink()
    counts = len(account.calls), len(calls)
    result = initialize(setup)
    assert result["state"] == "needs_choices" and result["choices"]["community"] is None
    assert onboarding.status(root)["state"] == "needs_choices"
    assert onboarding.status(root)["choices"]["community"] is None
    assert community.read_choice(root) is None
    assert record.read_bytes() == before
    assert (len(account.calls), len(calls)) == counts


def test_opt_out_during_blocked_setup_is_immediate_and_old_initializer_cannot_reenable(setup):
    root, account, calls, runner = setup
    entered, release = threading.Event(), threading.Event()
    def blocked_runner(command, target, env):
        if "sync" in command:
            entered.set()
            assert release.wait(5), "test never released setup"
        return runner(command, target, env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(onboarding.initialize, root, github_user="alice", fork=False,
                             star=False, community="enabled", github=account, runner=blocked_runner)
        try:
            assert entered.wait(3), "setup did not reach dependency step"
            enabled = community.read_choice(root)
            off = pool.submit(initialize, setup, community="disabled")
            result = off.result(timeout=2)
            assert result["state"] == "choice_updated" and result["setup_state"] == "pending"
            assert not active.done()
            revoked = community.read_choice(root)
            assert revoked["decision"] == "disabled"
            assert revoked["workspace_id"] == enabled["workspace_id"]
            assert revoked["revision"] != enabled["revision"]
        finally:
            release.set()
        finished = active.result(timeout=5)
    assert finished["state"] == "ready" and finished["choices"]["community"] == "disabled"
    assert finished["community_revision"] == revoked["revision"]
    assert community.read_choice(root) == revoked
    assert ["worker"] not in calls
    assert ["knowledge", "enabled", "alice"] not in calls


def test_concurrent_first_policy_writes_share_one_workspace_identity(setup, monkeypatch):
    import vaws_session_state
    root, *_ = setup
    writing, release = threading.Event(), threading.Event()
    original = vaws_session_state.write_json
    def blocked_write(path, value):
        if value["decision"] == "enabled":
            writing.set()
            assert release.wait(5)
        return original(path, value)
    monkeypatch.setattr(vaws_session_state, "write_json", blocked_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(community.write_choice, root, "enabled")
        try:
            assert writing.wait(3)
            second = pool.submit(community.write_choice, root, "disabled")
            time.sleep(.1)
            assert not second.done(), "second writer bypassed the local policy lock"
        finally:
            release.set()
        enabled, disabled = first.result(timeout=3), second.result(timeout=3)
    assert enabled["workspace_id"] == disabled["workspace_id"]
    assert enabled["revision"] != disabled["revision"]
    assert community.read_choice(root) == disabled


def test_delayed_disable_skips_a_newer_enabled_choice(setup):
    root, *_ = setup
    community.write_choice(root, "disabled")
    community.write_choice(root, "enabled")
    config = root / ".vaws-local/knowledge/service.json"
    config.parent.mkdir()
    config.write_text('{"publishing":{"enabled":true},"custom":"keep"}')
    before = config.read_bytes()
    assert community.disable_knowledge(root) == {"state": "unchanged", "reason": "community_enabled"}
    assert config.read_bytes() == before


def test_disable_config_write_cannot_complete_after_new_enabled_configuration(setup, monkeypatch):
    import vaws_session_state
    root, *_ = setup
    community.write_choice(root, "disabled")
    config = root / ".vaws-local/knowledge/service.json"
    config.parent.mkdir()
    config.write_text('{"publishing":{"enabled":true},"custom":"keep"}')
    writing, release, enable_started = threading.Event(), threading.Event(), threading.Event()
    original = vaws_session_state.write_json
    def blocked_write(path, value):
        if Path(path) == config and value["publishing"]["enabled"] is False:
            writing.set()
            assert release.wait(5)
        return original(path, value)
    def enable():
        enable_started.set()
        community.write_choice(root, "enabled")
        original(config, {"publishing": {"enabled": True}, "custom": "keep"})
    monkeypatch.setattr(vaws_session_state, "write_json", blocked_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        off = pool.submit(community.disable_knowledge, root)
        try:
            assert writing.wait(3)
            on = pool.submit(enable)
            assert enable_started.wait(3)
            time.sleep(.1)
            assert community.read_choice(root)["decision"] == "disabled"
            assert not on.done(), "new enable bypassed the in-flight local config write"
        finally:
            release.set()
        assert off.result(timeout=3)["state"] == "disabled"
        on.result(timeout=3)
    assert community.read_choice(root)["decision"] == "enabled"
    assert json.loads(config.read_text()) == {"publishing": {"enabled": True}, "custom": "keep"}

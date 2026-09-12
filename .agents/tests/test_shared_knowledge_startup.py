"""Native and fallback worktrees use one knowledge owner and candidate store."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import vaws_knowledge_service as knowledge
from vaws_knowledge.local.instance import instance_for_config
from vaws_knowledge.local.reconcile import reconcile_markdown
from vaws_knowledge.server.capture import capture
from vaws_knowledge.server.layers import load_config
from vaws_knowledge.server.query import explain, query


def git(root, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def family(tmp_path):
    root = tmp_path / "母仓 with spaces"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / ".gitignore").write_text(".vaws-local/\n")
    notes = root / ".agents/knowledge"
    notes.mkdir(parents=True)
    (notes / "fact.md").write_text("# Recorded snapshot\n\nsharedsnapshot old text.\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    a, b = tmp_path / "client A", tmp_path / "client B"
    git(root, "worktree", "add", "--detach", str(a), "HEAD")
    git(root, "worktree", "add", "--detach", str(b), "HEAD")
    path = root / ".vaws-local/knowledge/service.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"backend": "memory", "layers": {"shared": {"enabled": False}},
                               "shared_sync": {"enabled": False}, "publishing": {"enabled": False}}))
    return root, a, b, path


def test_capture_in_a_is_queryable_and_explainable_in_b(family):
    root, a, b, path = family
    knowledge.shared_project_config(a)
    config_a = knowledge.service_config(a)
    captured = capture(title="sharedcandidate canary", content="The reusable result stays across native clients.",
                       config=config_a, index=False)
    assert Path(captured["path"]).is_relative_to(root / knowledge.CANDIDATE_ROOT_RELATIVE)
    knowledge.shared_project_config(b)
    config_b = load_config(env=knowledge.knowledge_server_env(b))
    assert config_b.config_path == path == knowledge.service_config(a).config_path
    owner_a, owner_b = instance_for_config(config_a), instance_for_config(config_b)
    assert owner_a.state_root == owner_b.state_root
    assert owner_a.cache_dir == owner_b.cache_dir
    # The package memory backend is deliberately process-local; reconstruct its
    # index from the shared authoritative files, as package maintenance does.
    assert not reconcile_markdown(config_b, verify=True).degraded
    assert query(config_b, text="sharedcandidate").results[0]["title"] == "sharedcandidate canary"
    assert explain(config_b, captured["ref"])["found"] is True
    assert not (a / ".vaws-local/knowledge").exists()
    assert not (b / ".vaws-local/knowledge").exists()


def test_latest_snapshot_updates_in_place_without_mutating_other_checkout(family):
    root, a, b, _ = family
    knowledge.shared_project_config(a)
    snapshot = root / ".vaws-local/knowledge/project"
    original_time = (snapshot / "fact.md").stat().st_mtime_ns
    knowledge.shared_project_config(a)
    assert (snapshot / "fact.md").stat().st_mtime_ns == original_time
    (b / ".agents/knowledge/fact.md").unlink()
    (b / ".agents/knowledge/new.md").write_text("# New snapshot\n\nnew revision knowledge.\n")
    knowledge.shared_project_config(b)
    assert not (snapshot / "fact.md").exists()
    assert (snapshot / "new.md").read_text() == (b / ".agents/knowledge/new.md").read_text()
    assert (a / ".agents/knowledge/fact.md").is_file()
    assert knowledge.service_config(a).mount("project").roots == (snapshot,)


def test_custom_sources_and_publishing_choices_are_retained(family):
    root, a, _, path = family
    custom = {"backend": "memory", "state_root": "../custom-state", "layers": {
        "project": {"roots": ["../custom-project"]}, "candidate": {"root": "../custom-candidate"},
        "shared": {"enabled": False}}, "shared_sync": {"enabled": False, "repository": "chosen/corpus"},
        "publishing": {"enabled": True, "repository": "chosen/corpus", "fork": "person/corpus"}}
    path.write_text(json.dumps(custom))
    knowledge.shared_project_config(a)
    assert json.loads(path.read_text()) == custom
    config = knowledge.service_config(a)
    assert config.publishing == custom["publishing"]
    mcp = load_config(env=knowledge.knowledge_server_env(a))
    assert config.mount("candidate").roots == mcp.mount("candidate").roots
    assert config.mount("candidate").roots[0].resolve() == (path.parent / "../custom-candidate").resolve()


def test_cli_drops_inherited_location_and_uses_selected_runtime(family, monkeypatch):
    _, a, _, path = family
    for key in knowledge.LOCATION_ENV:
        monkeypatch.setenv(key, "/obsolete-parent-location")
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root: sys.executable)
    monkeypatch.setattr(knowledge, "managed_receipt", lambda root: {"receipt": "/old-environment"})
    calls = []
    original_run = knowledge.subprocess.run
    def run(command, **kwargs):
        if command[0] == "git":
            return original_run(command, **kwargs)
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"ready": true, "status": "ready"}')
    monkeypatch.setattr(knowledge.subprocess, "run", run)
    receipt = {"python": sys.executable, "receipt": "/selected-environment"}
    assert knowledge.prepare_knowledge(a, receipt=receipt)["ready"]
    command, kwargs = calls[0]
    assert command == [sys.executable, "-c", knowledge.PREPARE_CODE]
    assert kwargs["env"]["VAWS_ENV_RECEIPT"] == "/selected-environment"
    assert kwargs["env"]["VAWS_KNOWLEDGE_CONFIG"] == str(path)
    assert all(key not in kwargs["env"] for key in knowledge.LOCATION_ENV if key != "VAWS_KNOWLEDGE_CONFIG")


def test_startup_uses_package_incremental_maintenance_without_forced_model_audit(family, monkeypatch):
    _, a, _, path = family
    knowledge.shared_project_config(a)
    calls = []
    monkeypatch.setenv("VAWS_KNOWLEDGE_CONFIG", str(path))
    monkeypatch.setattr("vaws_knowledge.publishing.run_once", lambda config, **kwargs:
                        calls.append(("shared", kwargs)) or {"status": "disabled"})
    monkeypatch.setattr("vaws_knowledge.maintenance.maintain", lambda config, **kwargs:
                        calls.append(("maintain", kwargs)) or {"ready": True})
    exec(knowledge.PREPARE_CODE, {})
    assert calls == [("shared", {"force": True, "verify": False}),
                     ("maintain", {"force": True, "verify": False})]

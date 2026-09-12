"""Real package hooks project task selection without doing startup work."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
import vaws_environment as environments
import vaws_start_context as hints
from vaws_coordinator.agent_session import AgentSessions


def git(root, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "母仓 project"
    root.mkdir()
    git(root, "init", "-b", "main")
    (root / "README").write_text("fixture\n")
    git(root, "add", "README")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    target = tmp_path / "selected task W"
    git(root, "worktree", "add", "--detach", str(target), "HEAD")
    state = root / ".vaws-local/agent-sessions"
    identity = root / ".vaws-local/github.json"
    identity.parent.mkdir(parents=True)
    identity.write_text(json.dumps({"schema": "vaws.github.v1", "login": "fixture-user", "github_user_id": 42}))
    for key in ("VAWS_CONTEXT_FILE", "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT", "CLAUDE_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VAWS_SKIP_VENV_REEXEC", "1")
    monkeypatch.setenv("VAWS_AGENT_SESSIONS_DIR", str(state))
    monkeypatch.setenv("VAWS_GITHUB_IDENTITY_FILE", str(identity))

    # A ready receipt exercises the actual selection reader. It need not run
    # a provider: the selected Python is only a reported startup fact here.
    python_identity = environments._identity()
    selection = {"groups": [], "extras": [], "project": True}
    key = environments._key(python_identity, "fixture-input", selection)
    store = tmp_path / "environments"
    directory = store / key
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture")
    receipt = {"schema_version": 1, "recipe_version": environments.RECIPE_VERSION,
               "key": key, "root": str(directory), "python": str(python),
               "base_python": str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
               "python_identity": python_identity, "input_id": "fixture-input", "lock_sha256": "fixture-lock",
               "selection": selection, "store": str(store), "receipt": str(directory / environments.READY_NAME),
               **{name: python_identity[name] for name in ("platform", "arch", "abi", "python_version")}}
    Path(receipt["receipt"]).write_text(json.dumps(receipt))
    # A mother cached environment must never imply that a task is prepared.
    environments.select_environment(root, receipt)
    return root, target, AgentSessions(state), receipt


def payload(client, event, native, cwd, **extra):
    result = ({"hookEventName": event, "sessionId": native, "workspaceRoot": str(cwd)}
              if client == "grok" else {"hook_event_name": event, "session_id": native, "cwd": str(cwd)})
    if client == "cursor":
        result.update(conversation_id=native, cursor_version="fixture", workspace_roots=[str(cwd)])
    return {**result, **extra}


def run_hook(root, client, event_payload, *, package=False):
    entry = (["-m", "vaws_coordinator.hooks.vaws_session"] if package else
             [str(ROOT / ".agents/hooks/vaws_session.py")])
    return subprocess.run([sys.executable, *entry, "--client", client, "--project", str(root)],
                          input=json.dumps(event_payload), capture_output=True, text=True,
                          encoding="utf-8", cwd=root, timeout=15, check=True)


def hint(client, event, raw):
    if client == "kimi" and event == "UserPromptSubmit":
        return raw
    output = json.loads(raw)
    return output["additional_context"] if client == "cursor" else output["hookSpecificOutput"]["additionalContext"]


def task_record(root, context, target, receipt):
    path = root / ".vaws-local/tasks" / context["session"]["id"] / "start.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"workspace": str(target), "environment": receipt}))
    return path


@pytest.mark.parametrize("client", ["claude", "codex", "cursor", "grok", "kimi"])
def test_real_hook_unprepared_then_repeated_prepared_resume(project, client):
    root, target, store, receipt = project
    native = "native-exact-" + client
    first = run_hook(root, client, payload(client, "SessionStart", native, root))
    context = store.native_context(client, native)
    message = hint(client, "SessionStart", first.stdout)
    assert "NOT prepared" in message
    assert "First repository action, before file reads" in message
    assert "--client " + client in message and context["context_file"] in message
    assert "Client startup owns" not in message
    assert not (root / ".vaws-local/tasks").exists()

    store.bind_sources(context, {root.name: str(target)})
    record = task_record(root, context, target, receipt)
    original = record.read_bytes()
    for event in ("UserPromptSubmit", "SessionStart", "UserPromptSubmit"):
        result = run_hook(root, client, payload(client, event, native, root, source="resume"))
        message = hint(client, event, result.stdout)
        assert "workspace is prepared: W=" + str(target) in message
        assert receipt["key"] in message and receipt["python"] in message
        assert "First repository action" not in message
        assert record.read_bytes() == original
        if client == "cursor":
            assert "hookSpecificOutput" not in json.loads(result.stdout)
    current = store.native_context(client, native)
    assert current["session"]["id"] == context["session"]["id"]
    assert current["attachment"]["cwd"] == str(root)
    assert current["source_defaults"]["sources"][root.name]["path"] == str(target)


def test_prepared_native_worktree_and_broken_selection(project):
    root, target, store, receipt = project
    environments.select_environment(target, receipt)
    nested = target / "src"
    nested.mkdir()
    native = "native-worktree"
    result = run_hook(root, "claude", payload("claude", "SessionStart", native, nested))
    assert "workspace is prepared: W=" + str(target) in hint("claude", "SessionStart", result.stdout)
    assert not (root / ".vaws-local/tasks").exists()
    selected = target / ".vaws-local/environment-selection" / f"{sys.platform}.json"
    selected.write_text("{broken")
    result = run_hook(root, "claude", payload("claude", "UserPromptSubmit", native, nested))
    assert "workspace selection could not be read" in result.stdout
    assert "invalid environment selection" in result.stdout
    assert "First repository action" not in result.stdout
    assert selected.read_text() == "{broken"


def test_broken_task_receipt_is_not_a_new_session(project):
    root, target, store, receipt = project
    native = "broken-task"
    run_hook(root, "claude", payload("claude", "SessionStart", native, root))
    context = store.native_context("claude", native)
    record = task_record(root, context, target, receipt)
    Path(receipt["receipt"]).write_text("{}")
    result = run_hook(root, "claude", payload("claude", "UserPromptSubmit", native, root))
    assert "workspace selection could not be read" in result.stdout
    assert "incomplete ready receipt" in result.stdout
    assert "First repository action" not in result.stdout
    assert record.is_file()


def test_missing_identity_does_not_claim_prepared(project, monkeypatch):
    root, target, store, receipt = project
    (root / ".vaws-local/github.json").unlink()
    monkeypatch.delenv("VAWS_GITHUB_IDENTITY_FILE")
    # Override the real wrapper's ROOT identity discovery with a missing
    # package identity environment after the native package call below.
    context = store.attach("claude", "first-use", str(root))
    task_record(root, context, target, receipt)
    result = hints.project_output("claude", payload("claude", "SessionStart", "first-use", root),
                                  '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"native context"}}', root=root)
    assert "first-use setup is incomplete" in result
    assert "ask once" in result and "already supplied" in result
    assert "workspace is prepared" not in result and "First repository action" not in result


@pytest.mark.parametrize("case", ["foreign", "outside", "missing-id", "pretool", "kimi-extension"])
def test_package_noop_error_and_tool_output_remain_exact(project, case):
    root, _, _, _ = project
    client = "kimi" if case == "kimi-extension" else "claude"
    run_hook(root, client, payload(client, "SessionStart", "existing", root))
    event_payload = payload(client, "UserPromptSubmit", "existing", root)
    if case == "foreign":
        event_payload = payload("grok", "SessionStart", "foreign-id", root)
    elif case == "outside":
        event_payload["cwd"] = str(root.parent)
    elif case == "missing-id":
        event_payload.pop("session_id")
    elif case == "pretool":
        event_payload = payload(client, "PreToolUse", "existing", root,
                                tool_name="mcp__vaws-knowledge__knowledge_query", tool_input={"query": "principle"})
    else:
        event_payload["agent_id"] = "main"
    expected = run_hook(root, client, event_payload, package=True)
    actual = run_hook(root, client, event_payload)
    assert actual.stdout == expected.stdout
    assert actual.stderr == expected.stderr
    if case == "pretool":
        assert "context_file" in json.loads(actual.stdout)["hookSpecificOutput"]["updatedInput"]


def test_projection_does_not_guess_context_from_cwd_or_environment(project, monkeypatch):
    root, _, store, _ = project
    context = store.attach("claude", "actual", str(root))
    monkeypatch.setenv("VAWS_CONTEXT_FILE", context["context_file"])
    output = '{"hookSpecificOutput":{"additionalContext":"native context"}}'
    result = hints.project_output("claude", payload("claude", "SessionStart", "unknown", root), output, root=root)
    assert "association is missing or ambiguous" in result
    assert "First repository action" not in result
    with pytest.raises(ValueError):
        store.native_context("claude", "unknown")

"""Automatic capture reuses final text and never prevents normal client work."""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from test_immutable_environment import workspace

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("workspace_knowledge_summary", ROOT / ".agents/hooks/knowledge_summary.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def invoke(payload, *, client="codex", project=ROOT, facts_expected=None):
    output, errors = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["knowledge_summary.py", "--client", client, "--project", str(project)]), \
         mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
         contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = hook.main()
    assert code == 0
    assert output.getvalue() == "{}\n"
    facts = [json.loads(line) for line in errors.getvalue().splitlines()]
    if facts_expected is None:
        facts_expected = hook.in_project(payload, client=client, project=project)
    if facts_expected:
        assert len(facts) == 1
        assert facts[0]["event"] == "knowledge_summary"
        assert facts[0]["client"] == client
        assert "status" in facts[0]
    else:
        assert facts == []
    return facts


def test_capture_receives_existing_summary_without_an_extra_format():
    payload = {"hook_event_name": "Stop", "cwd": str(ROOT),
               "last_assistant_message": "The run completed. A retry fixed the transient timeout."}
    config = object()
    with mock.patch.object(hook, "ensure_workspace_interpreter"), \
         mock.patch("vaws_knowledge_service.service_config", return_value=config), \
         mock.patch("vaws_knowledge.summary_hook.capture_summary") as capture:
        invoke(payload)
    capture.assert_called_once_with(payload, config=config, client="codex")


def test_capture_error_does_not_interrupt_client():
    with mock.patch.object(hook, "ensure_workspace_interpreter"), \
         mock.patch("vaws_knowledge_service.service_config", side_effect=OSError("config unavailable ghp_exampletoken")):
        facts = invoke({"hook_event_name": "Stop", "cwd": str(ROOT), "last_assistant_message": "Existing final response."})
    assert facts[0]["status"] == "failed"
    assert facts[0]["error_type"] == "OSError"
    assert facts[0]["error"] == "config unavailable [redacted]"


def test_missing_environment_does_not_interrupt_client():
    def missing(**kwargs):
        print("workspace environment unavailable", file=sys.stderr)
        raise SystemExit(2)

    with mock.patch.object(hook, "ensure_workspace_interpreter", side_effect=missing):
        facts = invoke({"hook_event_name": "Stop", "cwd": str(ROOT),
                        "last_assistant_message": "This valid final response cannot prepare its unavailable owner."})
    assert facts[0]["status"] == "pending"
    assert facts[0]["reason"] == "knowledge_environment_not_prepared"
    assert "knowledge_setup.py" in facts[0]["remedy"]


def test_prepared_owner_handoff_has_real_stderr_for_child_processes():
    import subprocess
    def prepare(**kwargs):
        result = subprocess.run([sys.executable, "-c", "import sys;print('owner unavailable',file=sys.stderr)"],
                                stderr=sys.stderr, stdout=subprocess.DEVNULL, check=False)
        assert result.returncode == 0
        raise SystemExit(2)
    with mock.patch.object(hook, "ensure_workspace_interpreter", side_effect=prepare):
        facts = invoke({"hook_event_name": "Stop", "cwd": str(ROOT),
                        "last_assistant_message": "This valid final response needs actual child process stderr."})
    assert facts[0]["status"] == "pending"


def test_valid_cold_summary_never_installs_or_opens_a_socket(workspace):
    """Exercise the actual hook and fixed split receipt with owner I/O forbidden."""
    import vaws_environment as environments
    from test_capability_environment import configure

    configure(workspace)
    selected = environments.prepare_environment(workspace)
    assert not Path(selected["components"]["knowledge"]).exists()
    environment = dict(os.environ)
    for key in ("VAWS_SKIP_VENV_REEXEC", "VAWS_VENV_REEXEC", "VAWS_ENV_RECEIPT", "PYTHONHOME"):
        environment.pop(key, None)
    environment["VAWS_DIAGNOSTICS_ROOT"] = str(workspace / "isolated-logs")
    environment["VAWS_COMMUNITY_POLICY"] = str(workspace / "absent-community.json")
    attempts = workspace / "unexpected-owner-io.txt"
    bootstrap = f"import runpy,sys\nfrom pathlib import Path\nattempts=Path({str(attempts)!r})\n" + """
def no_owner_io(event, args):
    if event in {'socket.connect', 'subprocess.Popen', 'os.system', 'os.exec'}:
        attempts.write_text(event, encoding='utf-8')
        raise SystemExit(91)
sys.addaudithook(no_owner_io)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", bootstrap,
                             str(ROOT / ".agents/hooks/knowledge_summary.py"),
                             "--client", "codex", "--project", str(workspace),
                             "--environment-receipt", selected["receipt"]],
                            input=json.dumps({"cwd": str(workspace), "hook_event_name": "Stop",
                                              "last_assistant_message": "A useful final response must not install an optional owner."}),
                            capture_output=True, encoding="utf-8", env=environment, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "{}\n"
    assert not attempts.exists(), attempts.read_text(encoding="utf-8")
    facts = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(facts) == 1 and facts[0]["status"] == "pending", facts
    assert facts[0]["reason"] == "knowledge_environment_not_prepared"
    assert "knowledge_setup.py" in facts[0]["remedy"]
    assert not Path(selected["components"]["knowledge"]).exists()
    assert not (workspace / ".vaws-local/knowledge/service.json").exists()


def test_foreign_or_unknown_project_does_not_capture(tmp_path):
    for payload in ({"cwd": str(tmp_path)}, {}, {"cwd": "."}):
        with mock.patch.object(hook, "ensure_workspace_interpreter") as prepare, \
             mock.patch("vaws_knowledge_service.service_config") as config, \
             mock.patch("vaws_knowledge.summary_hook.capture_summary") as capture:
            invoke(payload)
        prepare.assert_not_called()
        config.assert_not_called()
        capture.assert_not_called()


def test_project_subdirectory_is_in_scope(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    assert hook.in_project({"cwd": str(child)}, client="grok", project=tmp_path)
    assert not hook.in_project({"cwd": str(tmp_path.parent)}, client="grok", project=tmp_path)


def test_cursor_workspace_roots_must_stay_in_scope(tmp_path):
    assert hook.in_project({"workspace_roots": [str(tmp_path)]}, client="cursor", project=tmp_path)
    assert not hook.in_project({"workspace_roots": [str(tmp_path), str(tmp_path.parent)]}, client="cursor", project=tmp_path)
    assert not hook.in_project({"workspace_roots": [str(tmp_path)]}, client="codex", project=tmp_path)
    assert not hook.in_project({"cwd": str(tmp_path.parent), "workspace_roots": [str(tmp_path)]},
                               client="cursor", project=tmp_path)


def test_cursor_completed_session_end_captures_existing_final_response(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    text = "The completed final response already contains useful task evidence."
    transcript.write_text(json.dumps({"role": "assistant", "message": {
        "content": [{"type": "text", "text": text}]}}) + "\n")
    payload = {"hook_event_name": "sessionEnd", "final_status": "completed",
               "conversation_id": "actual-session", "workspace_roots": [str(tmp_path)],
               "transcript_path": str(transcript)}
    with mock.patch.object(hook, "ensure_workspace_interpreter"), \
         mock.patch("vaws_knowledge_service.service_config", return_value="shared"), \
         mock.patch("vaws_knowledge.summary_hook.capture_summary") as capture:
        invoke(payload, client="cursor", project=tmp_path)
    assert capture.call_args.args[0] == {**payload, "hook_event_name": "afterAgentResponse", "text": text}
    assert capture.call_args.kwargs == {"config": "shared", "client": "cursor"}


@pytest.mark.skipif(os.name != "nt", reason="native Windows path interpretation")
def test_windows_summary_owner_accepts_only_its_mounted_wsl_project():
    project = Path(r"D:\work")
    assert hook.in_project({"cwd": "/mnt/d/work/subdir"}, client="grok", project=project)
    assert not hook.in_project({"cwd": "/mnt/d/other"}, client="grok", project=project)
    assert not hook.in_project({"cwd": "/mnt/d/work/../other"}, client="grok", project=project)
    assert not hook.in_project({"workspace_roots": ["/mnt/d/work", "/mnt/d/other"]}, client="cursor", project=project)


@pytest.mark.parametrize("raw", ["", "{", "[]", '"text"', "x" * 1_048_577],
                         ids=["empty", "invalid-json", "array", "string", "oversized"])
def test_invalid_input_does_not_prepare_an_owner(raw):
    with mock.patch.object(sys, "argv", ["knowledge_summary.py", "--client", "codex", "--project", str(ROOT)]), \
         mock.patch.object(sys, "stdin", io.StringIO(raw)), \
         mock.patch.object(hook, "ensure_workspace_interpreter") as prepare, \
         contextlib.redirect_stdout(io.StringIO()) as output:
        assert hook.main() == 0
    assert output.getvalue() == "{}\n"
    prepare.assert_not_called()


@pytest.mark.parametrize("client,fields", [
    ("codex", {"hook_event_name": "Stop"}),
    ("codex", {"hook_event_name": "Stop", "last_assistant_message": "   "}),
    ("codex", {"hook_event_name": "Stop", "last_assistant_message": "short"}),
    ("codex", {"hook_event_name": "Stop", "last_assistant_message": {"text": "not a native string"}}),
    ("codex", {"hook_event_name": "Stop", "last_assistant_message": "<oai-mem-citation>citation only</oai-mem-citation>"}),
    ("codex", {"hook_event_name": "PreToolUse", "last_assistant_message": "Long text from an event that is not a final response."}),
    ("codex", {"hookEventName": "stop", "lastAssistantMessage": "A Grok event must not be consumed by Codex's imported hook."}),
    ("grok", {"hook_event_name": "Stop", "last_assistant_message": "A non-native event must not trigger another Grok capture."}),
    ("cursor", {"hook_event_name": "sessionEnd", "final_status": "aborted"}),
    ("kimi", {"hook_event_name": "Stop", "session_id": "../not-a-session"}),
])
def test_events_without_a_usable_native_final_do_not_prepare(client, fields):
    with mock.patch.object(hook, "ensure_workspace_interpreter") as prepare, \
         mock.patch("vaws_knowledge_service.service_config") as config:
        facts = invoke({"cwd": str(ROOT), **fields}, client=client)
    prepare.assert_not_called()
    config.assert_not_called()
    assert facts[0]["status"] == "no_summary"


@pytest.mark.parametrize("kind", ["invalid-json", "foreign", "no-final"])
def test_public_hook_skips_without_site_packages_or_a_ready_receipt(tmp_path, kind):
    project = tmp_path / "project"
    project.mkdir()
    raw = "{" if kind == "invalid-json" else json.dumps({
        "cwd": str(tmp_path if kind == "foreign" else project), "hook_event_name": "Stop",
        "last_assistant_message": "A valid body belonging to a different project." if kind == "foreign" else ""})
    environment = dict(os.environ)
    for key in ("VAWS_SKIP_VENV_REEXEC", "VAWS_VENV_REEXEC", "VAWS_ENV_RECEIPT"):
        environment.pop(key, None)
    bootstrap = """import runpy,sys
def no_owner_io(event, args):
    if event in {'socket.connect', 'subprocess.Popen', 'os.system', 'sqlite3.connect'}:
        raise AssertionError('skipped hook attempted owner I/O: ' + event)
sys.addaudithook(no_owner_io)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", bootstrap,
                             str(ROOT / ".agents/hooks/knowledge_summary.py"),
                             "--client", "codex", "--project", str(project),
                             "--environment-receipt", str(tmp_path / "missing-receipt.json")],
                            input=raw, capture_output=True, encoding="utf-8", env=environment, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "{}\n"
    lines = result.stderr.splitlines()
    # -S intentionally removes the diagnostics distribution too. Its bounded,
    # static fallback does not prepare the knowledge owner or inspect the
    # nonexistent receipt. Free diagnostic text remains unavailable without
    # the redactor, including the otherwise static no-summary status.
    assert json.loads(lines.pop(0)) == {
        "level": "WARNING", "event": "diagnostics.unavailable",
        "component": "workspace", "category": "package_unavailable",
    }
    if kind == "no-final":
        assert lines == ["[diagnostic text unavailable]"]
    else:
        assert lines == []
    assert list(project.iterdir()) == []


def test_same_interpreter_needs_no_input_replay_file():
    import vaws_venv as bootstrap
    from types import SimpleNamespace

    receipt = {"python": sys.executable, "root": sys.prefix, "key": "current", "receipt": "current.json"}
    # Lazy imports still need the real interpreter's other startup flags.
    flags = SimpleNamespace(**{name: getattr(sys.flags, name)
                               for name in dir(sys.flags) if not name.startswith("_")})
    flags.utf8_mode = 1
    with mock.patch.object(bootstrap, "native_ready", return_value=receipt), \
         mock.patch.object(bootstrap, "capability_receipt", return_value=receipt), \
         mock.patch.object(bootstrap.sys, "flags", flags), \
         mock.patch.object(bootstrap.tempfile, "TemporaryFile") as temporary, \
         mock.patch.dict(os.environ):
        os.environ.pop(bootstrap.SKIP_ENV, None)
        bootstrap.ensure_workspace_interpreter(repo_root=ROOT, stdin=b"already inspected")
    temporary.assert_not_called()


@pytest.mark.parametrize("client", ["codex", "cursor"])
def test_real_interpreter_hop_replays_original_input_and_captures_once(workspace, client):
    """Use an actual second venv and the installed capture implementation."""
    import vaws_environment as environments

    selected = environments.prepare_environment(workspace)
    config = workspace / ".vaws-local/knowledge/service.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    candidate = workspace / "candidate"
    config.write_text(json.dumps({"backend": "memory", "layers": {
        "candidate": {"roots": [str(candidate)]}, "project": {"roots": []}},
        "state_root": str(workspace / "state"), "publishing": {"enabled": False},
        "shared_sync": {"enabled": False}}), encoding="utf-8")
    text = "复用已完成的模型验证总结。The original final response crosses the interpreter boundary intact."
    payload = {"hook_event_name": "Stop", "cwd": str(workspace),
               "session_id": "source-session", "last_assistant_message": text}
    if client == "cursor":
        transcript = workspace / "native-transcript.jsonl"
        transcript.write_text(json.dumps({"role": "assistant", "message": {
            "content": [{"type": "text", "text": text}]}}, ensure_ascii=False) + "\n", encoding="utf-8")
        payload = {"hook_event_name": "sessionEnd", "final_status": "completed",
                   "conversation_id": "source-conversation", "workspace_roots": [str(workspace)],
                   "transcript_path": str(transcript)}
    raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    log = workspace / "handoff.jsonl"
    runner = workspace / "summary_runner.py"
    runner.write_text(
        "import json,runpy,sys\nfrom pathlib import Path\n"
        f"log=Path({str(log)!r})\n"
        "def record(value):\n"
        "    with log.open('a',encoding='utf-8') as stream: stream.write(json.dumps(value)+'\\n')\n"
        "class Input:\n"
        "    def __init__(self,stream): self.stream=stream\n"
        "    def __getattr__(self,name): return getattr(self.stream,name)\n"
        "    def read(self,size):\n"
        "        value=self.stream.read(size)\n"
        "        record({'kind':'input','prefix':sys.prefix,'raw':value})\n"
        "        return value\n"
        "sys.stdin=Input(sys.stdin)\n"
        "import vaws_knowledge.summary_hook as owner\n"
        "capture=owner.capture_summary\n"
        "def observed(payload,**kwargs):\n"
        "    record({'kind':'capture','prefix':sys.prefix,'payload':payload})\n"
        "    return capture(payload,**kwargs)\n"
        "owner.capture_summary=observed\n"
        f"runpy.run_path({str(ROOT / '.agents/hooks/knowledge_summary.py')!r},run_name='__main__')\n",
        encoding="utf-8")
    environment = dict(os.environ)
    for key in ("VAWS_SKIP_VENV_REEXEC", "VAWS_VENV_REEXEC", "VAWS_ENV_RECEIPT", "PYTHONHOME"):
        environment.pop(key, None)
    # Reuse installed package bytes without another download; selection and
    # sys.prefix still come from the genuinely distinct ready interpreter.
    environment["PYTHONPATH"] = os.pathsep.join(str(Path(path).resolve()) for path in sys.path if path)
    if client == "codex":
        # Native hooks may start in a legacy locale before the UTF-8 owner hop.
        environment["PYTHONIOENCODING"] = "cp936"
    result = subprocess.run([sys.executable, *([] if client == "codex" else ["-X", "utf8"]), str(runner), "--client", client,
                             "--project", str(workspace), "--environment-receipt", selected["receipt"]],
                            input=raw, capture_output=True, encoding="utf-8", env=environment, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "{}\n", result.stdout + result.stderr
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    inputs = [record for record in records if record["kind"] == "input"]
    captures = [record for record in records if record["kind"] == "capture"]
    assert len(inputs) == 2, records
    assert inputs[0]["raw"] == inputs[1]["raw"] == raw
    assert Path(inputs[0]["prefix"]).resolve() != Path(inputs[1]["prefix"]).resolve()
    assert Path(inputs[1]["prefix"]).resolve() == Path(selected["root"]).resolve()
    assert len(captures) == 1, records
    assert captures[0]["prefix"] == inputs[1]["prefix"]
    assert captures[0]["payload"] == (payload if client == "codex" else {
        **payload, "hook_event_name": "afterAgentResponse", "text": text})
    notes = list(candidate.glob("*.md"))
    assert len(notes) == 1
    assert text in notes[0].read_text(encoding="utf-8")

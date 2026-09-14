"""Real entry/stdio failure boundaries, without remote or business execution."""
from __future__ import annotations

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents/lib"
sys.path.insert(0, str(LIB))
import vaws_diagnostics_adapter as diagnostics


@pytest.fixture
def observed(tmp_path, monkeypatch):
    pytest.importorskip("vaws_diagnostics")
    monkeypatch.setenv("VAWS_DIAGNOSTICS_ROOT", str(tmp_path / "diagnostics"))
    monkeypatch.setenv("VAWS_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(diagnostics, "_entry", None)
    monkeypatch.setattr(diagnostics, "_loaded", False)
    return tmp_path / "diagnostics"


def events(root):
    return [json.loads(line) for path in root.glob("events/**/*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()]


def test_failed_phase_has_actual_duration_and_original_exception(observed):
    failure = ValueError("password=private-test-credential")
    with pytest.raises(ValueError) as caught:
        with diagnostics.operation("fixture") as operation:
            with diagnostics.phase("parse"):
                raise failure
    assert caught.value is failure
    rows = events(observed)
    ended = [row for row in rows if row["event"] == "phase.end"]
    assert ended and ended[-1]["duration_ms"] >= 0
    assert ended[-1]["status"] == "error"
    assert operation.summary()["started_at"]
    assert operation.summary()["finished_at"]
    assert "private-test-credential" not in json.dumps(rows)


def test_unwritable_observer_preserves_success(tmp_path, monkeypatch):
    root = tmp_path / "blocked"
    root.write_text("not a directory")
    monkeypatch.setenv("VAWS_DIAGNOSTICS_ROOT", str(root))
    with diagnostics.operation("fixture") as operation:
        value = {"business": "complete"}
    assert value == {"business": "complete"}
    assert operation.summary()["status"] == "success"
    assert operation.summary()["logging_failed"] is True


def test_parallel_children_keep_the_same_trace_and_distinct_phases(observed):
    with diagnostics.operation("parent") as parent:
        def child(number):
            with diagnostics.phase("copy", repository=number) as current:
                return current.summary()
        with ThreadPoolExecutor(max_workers=2) as pool:
            children = list(pool.map(diagnostics.wrap_context(child), (1, 2)))
    assert {item["trace_id"] for item in children} == {parent.summary()["trace_id"]}
    assert len({item["phase_id"] for item in children}) == 2


def test_request_metadata_carries_only_diagnostic_context_and_keeps_native_metadata(observed):
    with diagnostics.operation("request") as operation:
        metadata = diagnostics.context_metadata({"native": {"turn": "unchanged"}})
        environment = diagnostics.context_environment({"KEEP": "yes"})
    assert metadata["native"] == {"turn": "unchanged"}
    context = metadata["vaws_diagnostics"]
    assert context["operation_id"] == operation.summary()["operation_id"]
    assert set(context) <= {"trace_id", "operation_id", "parent_operation_id", "phase_id"}
    assert json.loads(environment["VAWS_DIAGNOSTICS_CONTEXT"]) == context
    assert environment["KEEP"] == "yes"


def test_child_stderr_is_drained_redacted_and_drops_oversized_lines(observed):
    secret = "ghp_" + "a" * 40
    with diagnostics.operation("child") as operation:
        with diagnostics.captured_stderr(operation) as sink:
            result = subprocess.run([sys.executable, "-c",
                "import sys;sys.stderr.write('x'*10000+'\\n');sys.stderr.write('failure '+sys.argv[1]+'\\n');print('result')", secret],
                stdout=subprocess.PIPE, stderr=sink, text=True, check=False)
    assert result.returncode == 0 and result.stdout == "result\n"
    rows = events(observed)
    assert any(row["event"] == "process.stderr_line_omitted" for row in rows)
    assert any(row["event"] == "process.stderr" for row in rows)
    assert secret not in json.dumps(rows)
    assert "x" * 10000 not in json.dumps(rows)


def test_cancelled_async_phase_propagates_cancellation(observed):
    @diagnostics.measured("request")
    async def blocked():
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(blocked())
    assert events(observed)[-1]["status"] == "cancelled"


def test_result_envelope_uses_real_start_and_null_when_not_observed(observed):
    from vaws_result_envelope import make_attempt, make_command
    command = make_command(argv=["python", "--password", "private-value"])
    assert command["argv"][-1] == "[redacted]"
    assert make_attempt(command=command, reproduce="fixture")["started_at"] is None
    with diagnostics.operation("fixture") as operation:
        attempt = make_attempt(command=command, reproduce="fixture")
    assert attempt["started_at"] == operation.started_at or attempt["started_at"] == operation.summary()["started_at"]
    assert attempt["duration_ms"] >= 0


@pytest.mark.parametrize("consent", ["enabled", "disabled"])
def test_bootstrap_before_dependency_import_keeps_one_safe_failure_record(tmp_path, consent):
    from vaws_community import write_choice
    write_choice(tmp_path, consent)
    script = tmp_path / "broken.py"
    script.write_text("import sys,importlib.abc\n"
                      "class Missing(importlib.abc.MetaPathFinder):\n"
                      " def find_spec(self, name, *args):\n"
                      "  if name == 'vaws_diagnostics' or name.startswith('vaws_diagnostics.'):\n"
                      "   raise ModuleNotFoundError(name)\n"
                      "sys.meta_path.insert(0, Missing())\n"
                      "sys.path.insert(0," + repr(str(LIB)) + ")\n"
                      "from vaws_diagnostics_adapter import bootstrap\nbootstrap(__file__)\n"
                      "raise ImportError('password=never-include-this-body')\n")
    root = tmp_path / "diagnostics"
    result = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True,
                            env={**os.environ, "VAWS_DIAGNOSTICS_ROOT": str(root),
                                 "VAWS_COMMUNITY_POLICY": str(tmp_path / ".vaws-local/community.json")}, check=False)
    assert result.returncode != 0
    assert result.stdout == ""
    rows = events(root)
    assert len(rows) == 1 and rows[0]["attributes"]["error_type"] == "ImportError"
    assert rows[0]["schema"] == 1 and rows[0]["started_at"]
    assert rows[0]["duration_ms"] >= 0
    assert "never-include-this-body" not in json.dumps(rows) + result.stderr
    from vaws_diagnostics import collect_bundle
    from vaws_diagnostics.reporter import ingest
    from vaws_diagnostics.outbox import Outbox
    bundle = collect_bundle(root)
    assert len(bundle["events"]) == 1 and bundle["events"][0]["status"] == "error"
    queue = Outbox(tmp_path / "private-outbox.sqlite")
    assert "community" not in bundle["events"][0]
    assert ingest(root, queue)["enqueued"] == (1 if consent == "enabled" else 0)
    assert ingest(root, queue)["enqueued"] == 0


@pytest.mark.parametrize(("classification", "error_code"), [("caller", -32602), ("unknown", 503)])
def test_public_bundle_cli_preserves_failure_classification_and_numeric_code(observed, classification, error_code):
    with diagnostics.operation("fixture.public_bundle") as operation:
        operation.fail("tool_result", classification=classification, error_code=error_code,
                       preview="private-business-detail")
    output = observed.parent / "support.json"
    result = subprocess.run([
        sys.executable, "-I", str(ROOT / ".agents/scripts/vaws_diagnose.py"),
        "bundle", "--root", str(observed), "--operation-id", operation.summary()["operation_id"],
        "--output", str(output),
    ], capture_output=True, encoding="utf-8", env=dict(os.environ), timeout=10)
    assert result.returncode == 0, result.stderr
    returned = json.loads(result.stdout)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert returned == written
    failures = [row for row in returned["events"]
                if row["event"] == "operation.end" and row["status"] == "error"]
    assert len(failures) == 1
    assert failures[0]["attributes"]["classification"] == classification
    assert failures[0]["attributes"]["error_code"] == error_code
    assert "private-business-detail" not in result.stdout + output.read_text(encoding="utf-8")


def test_leak_help_needs_no_knowledge_or_diagnostics_install(tmp_path):
    root = tmp_path / "diagnostics"
    result = subprocess.run([sys.executable, "-I", str(ROOT / ".agents/scripts/tracked_leak_scan.py"), "--help"],
                            env={**os.environ, "VAWS_DIAGNOSTICS_ROOT": str(root)}, capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0 and "usage:" in result.stdout
    assert "knowledge" not in result.stderr and not root.exists()


def test_updater_error_log_failure_does_not_mask_original(observed, tmp_path):
    from vaws_workspace_update import WorkspaceUpdater, Deferred
    updater = object.__new__(WorkspaceUpdater)
    updater.base = tmp_path
    updater.state = {"phase": "fetch"}
    error = Deferred("command_timeout", "original timeout")
    with patch("vaws_workspace_update.write_json", side_effect=OSError("disk full")):
        result = updater.failure(error)
    assert result["reason"] == "command_timeout"
    assert result["detail"] == "original timeout"
    assert result["log"] is None


def test_cli_inventory_actual_main_calls_use_one_bootstrap():
    # Detect future entry omissions using source structure, without executing tools.
    for path in ROOT.joinpath(".agents").rglob("*.py"):
        relative = path.relative_to(ROOT)
        if "tests" in relative.parts or ".vaws-local" in relative.parts:
            continue
        if relative.as_posix() == ".agents/lib/vaws_git_credential.py":
            # Its only output is Git's private credential pipe. Auth tests prove
            # it does not record or print credentials into diagnostic channels.
            continue
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        guards = [node for node in tree.body if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test)]
        calls_main = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "main"
                         for guard in guards for node in ast.walk(guard))
        if calls_main:
            assert "bootstrap" in source and ("_vaws_entry.run" in source or "bootstrap(__file__).run" in source), path


@pytest.mark.parametrize("code", [0, 2])
def test_pre_main_interpreter_exit_has_actual_exit_status(tmp_path, code):
    script = tmp_path / "entry.py"
    script.write_text("import sys\nsys.path.insert(0," + repr(str(LIB)) + ")\n"
        "from vaws_diagnostics_adapter import bootstrap,measured\nentry=bootstrap(__file__)\n"
        "@measured('interpreter')\ndef select(): raise SystemExit(" + str(code) + ")\nselect()\n")
    root = tmp_path / "diagnostics"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                            env={**os.environ, "VAWS_DIAGNOSTICS_ROOT": str(root)}, check=False)
    assert result.returncode == code and result.stdout == ""
    ends = [row for row in events(root) if row["event"] == "operation.end"]
    assert len(ends) == 1
    assert ends[0]["status"] == ("success" if code == 0 else "error")
    assert ends[0].get("attributes", {}).get("category") != "caller"


def test_early_parser_failure_is_caller_without_secret(tmp_path):
    script = tmp_path / "entry.py"
    script.write_text("import sys\nsys.path.insert(0," + repr(str(LIB)) + ")\n"
        "from vaws_diagnostics_adapter import bootstrap\nentry=bootstrap(__file__)\n"
        "import argparse\ndef main(): argparse.ArgumentParser().parse_args()\nentry.run(main)\n")
    root = tmp_path / "diagnostics"
    result = subprocess.run([sys.executable, str(script), "--token=ghp_" + "a" * 40],
                            capture_output=True, text=True,
                            env={**os.environ, "VAWS_DIAGNOSTICS_ROOT": str(root)}, check=False)
    assert result.returncode == 2 and result.stdout == ""
    ends = [row for row in events(root) if row["event"] == "operation.end"]
    assert ends[-1]["attributes"]["category"] == "caller"
    assert "a" * 40 not in result.stderr + json.dumps(events(root))


def test_custom_exception_hook_is_preserved(observed, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: calls.append(args))
    with patch.object(diagnostics.atexit, "register"), patch.object(diagnostics.argparse.ArgumentParser, "error"):
        entry = diagnostics.Entry("fixture")
        error = ValueError("original")
        entry.excepthook(ValueError, error, None)
    assert calls == [(ValueError, error, None)]


def test_after_fork_replaces_inherited_adapter_lock(observed, monkeypatch):
    import threading
    lock = threading.Lock()
    lock.acquire()
    monkeypatch.setattr(diagnostics, "_recorder_lock", lock)
    diagnostics._after_fork_child()
    with diagnostics.operation("child") as operation:
        assert operation.summary()["operation_id"]
    lock.release()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork boundary")
def test_actual_fork_while_other_thread_holds_adapter_lock(observed):
    import select
    import signal
    import threading
    locked, release = threading.Event(), threading.Event()
    with diagnostics.operation("before_fork"):
        pass
    def holder():
        with diagnostics._recorder_lock:
            locked.set()
            release.wait(10)
    thread = threading.Thread(target=holder)
    thread.start()
    assert locked.wait(2)
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(reader)
        try:
            with diagnostics.operation("after_fork"):
                pass
            os.write(writer, b"success")
            os._exit(0)
        except BaseException:
            os._exit(2)
    os.close(writer)
    try:
        ready, _, _ = select.select([reader], [], [], 5)
        if not ready:
            os.kill(pid, signal.SIGKILL)
        assert ready and os.read(reader, 100) == b"success"
    finally:
        release.set()
        thread.join(2)
        os.close(reader)
        os.waitpid(pid, 0)


@pytest.mark.parametrize("details,expected,certainty", [
    ({"category": "validation", "submission_state": "not_sent"}, "validation", "not_sent"),
    ({"category": "command_exit", "submission_state": "acknowledged"}, "command_exit", "acknowledged"),
    ({}, "owner_result", "unknown"),
])
def test_mcp_preserves_explicit_owner_error_classification(observed, details, expected, certainty):
    from mcp.types import CallToolResult
    from unittest.mock import AsyncMock
    from vaws_mcp_runtime import Provider
    provider = object.__new__(Provider)
    provider.kind = "fixture"
    provider.root = observed.parent
    provider._call_tool = AsyncMock(return_value=CallToolResult(isError=True,
        structuredContent={"error_details": details}, content=[]))
    result = asyncio.run(provider.call_tool("fixture", {}))
    assert result.isError
    ends = [row for row in events(observed) if row["event"] == "operation.end"]
    assert ends[-1]["attributes"]["category"] == expected
    assert ends[-1]["attributes"]["submission_state"] == certainty


def test_persistent_mcp_calls_have_independent_traces_and_accept_upstream_ids(observed):
    from mcp.types import CallToolResult
    from unittest.mock import AsyncMock
    from vaws_mcp_runtime import Provider
    provider = object.__new__(Provider)
    provider.kind = "fixture"
    provider.root = observed.parent
    provider._call_tool = AsyncMock(side_effect=lambda *args: CallToolResult(content=[]))
    async def run():
        with diagnostics.operation("server"):
            first = await provider.call_tool("fixture", {})
            second = await provider.call_tool("fixture", {})
            linked = await provider.call_tool("fixture", {}, {"vaws_diagnostics": {
                "trace_id": "a" * 32, "operation_id": "b" * 32, "identity": "not authority"}})
        return [item.meta["vaws_diagnostics"] for item in (first, second, linked)]
    first, second, linked = asyncio.run(run())
    assert first["trace_id"] != second["trace_id"]
    assert linked["trace_id"] == "a" * 32 and linked["parent_operation_id"] == "b" * 32


def test_cli_fd_capture_redacts_and_restores_stdout(tmp_path):
    script = tmp_path / "output.py"
    script.write_text("import sys,os\nsys.path.insert(0," + repr(str(LIB)) + ")\n"
        "from vaws_diagnostics_adapter import captured_process_output\n"
        "with captured_process_output('fixture'):\n"
        " print('progress ghp_'+'a'*40)\n os.write(2,b'child stderr\\n')\n"
        "print('business-json')\n")
    root = tmp_path / "diagnostics"
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                            env={**os.environ, "VAWS_DIAGNOSTICS_ROOT": str(root)}, check=False)
    assert result.returncode == 0 and result.stdout == "business-json\n"
    assert result.stderr == ""
    rows = events(root)
    assert any(row["event"] == "process.output" for row in rows)
    assert "a" * 40 not in json.dumps(rows)


def test_cli_fd_capture_unwritable_logger_preserves_business_and_bounded_warning(tmp_path):
    script = tmp_path / "output-failure.py"
    script.write_text("import sys,os\nsys.path.insert(0," + repr(str(LIB)) + ")\n"
        "from vaws_diagnostics_adapter import captured_process_output\n"
        "with captured_process_output('fixture'):\n"
        " os.write(2,b'ERROR: fixture child stderr\\n')\n"
        " for _ in range(16): os.write(1,b'x'*65536)\n"
        " os.write(1,b'\\n')\n"
        "print('business-json')\n", encoding="utf-8")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    # Suppress operation.start so the first real storage error occurs while
    # stderr is redirected, exercising the shared drain's fallback branch.
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                            env={**os.environ, "VAWS_DIAGNOSTICS_ROOT": str(blocked),
                                 "VAWS_LOG_LEVEL": "WARNING"}, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "business-json\n"
    assert "diagnostic storage unavailable" in result.stderr
    assert len(result.stderr) < 300
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows emergency bootstrap")
def test_powershell_no_python_failure_can_be_collected(tmp_path):
    installer = ROOT / ".agents/bootstrap/repo-init/scripts/install-gh-user.ps1"
    root = tmp_path / "diagnostics"
    # A shell-local replacement proves logging without network or installation.
    command = ("function Start-Job { throw 'fixture-no-network' }; "
               "try { & '" + str(installer).replace("'", "''") + "' } catch { exit 7 }")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True,
                            env={**os.environ, "VAWS_DIAGNOSTICS_ROOT": str(root)}, check=False)
    assert result.returncode == 7
    from vaws_diagnostics import collect_bundle
    rows = collect_bundle(root)["events"]
    assert rows[-1]["event"] == "operation.end" and rows[-1]["status"] == "error"
    assert "fixture-no-network" not in json.dumps(rows)


def test_incomplete_stderr_line_is_not_saved_as_credential_fragment(observed):
    with diagnostics.operation("partial") as operation:
        with diagnostics.captured_stderr(operation) as sink:
            sink.write("ghp_incomplete_credential_fragment")
    rows = events(observed)
    assert "credential_fragment" not in json.dumps(rows)
    assert any(row["event"] == "process.stderr_line_omitted" for row in rows)


def test_unprintable_caught_error_does_not_break_business(observed):
    class BadError(Exception):
        def __str__(self):
            raise RuntimeError("broken error formatter")
    diagnostics.report_failure("optional.failed", BadError())
    assert events(observed)[-1]["status"] == "success"


def test_progress_is_bounded_and_log_write_failure_is_optional(observed):
    import io
    from vaws_result_envelope import progress
    stream = io.StringIO()
    progress("fixture", "message", stream=stream, **{f"k{i}": "x" * 10000 for i in range(100)})
    assert len(stream.getvalue().encode("utf-8")) < 16384
    stream.close()
    progress("fixture", "complete", stream=stream)

import json
import subprocess
import sys
import time

import pytest

from vaws_process_wait import owned, run_captured, wait_with_progress


def test_wait_emits_progress_and_times_out(capsys):
    with pytest.raises(subprocess.TimeoutExpired):
        with owned([sys.executable, "-c", "import time; time.sleep(20)"]) as process:
            wait_with_progress(process, stage="test", timeout=.4, interval=.1)
    assert '"stage": "test"' in capsys.readouterr().err
    assert process.poll() is not None


def test_timeout_stops_child_heartbeat_too(tmp_path):
    heartbeat = tmp_path / "heartbeat"
    child = "import pathlib,time; p=pathlib.Path(" + repr(str(heartbeat)) + ");\nwhile True:\n p.write_text(str(time.time())); time.sleep(.05)"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(30)"
    with pytest.raises(subprocess.TimeoutExpired):
        run_captured([sys.executable, "-c", parent], stage="test_tree", timeout=2)
    before = heartbeat.read_text()
    time.sleep(.3)
    assert heartbeat.read_text() == before


def test_large_output_does_not_deadlock_and_evidence_is_bounded():
    result = run_captured([sys.executable, "-c", "print('x'*1000000)"], stage="test_output", timeout=10)
    assert result.returncode == 0 and len(result.stdout) == 65536


def test_binary_git_input_and_complete_output_are_preserved():
    content = bytes(range(256)) * 1000
    result = run_captured([sys.executable, "-c", "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())"],
                          stage="binary_input", timeout=10, encoding=None, limit=None, input_data=content)
    assert result.returncode == 0 and result.stdout == content

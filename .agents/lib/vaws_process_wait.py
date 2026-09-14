"""Visible, bounded waits; callers retain ownership of child-tree cleanup."""
import json
import subprocess
import sys
import time
import os
import signal
import tempfile
from contextlib import contextmanager


def wait_with_progress(process, *, stage: str, timeout: float = 900, interval: float = 15):
    if not 0 < timeout <= 86400 or interval <= 0:
        raise ValueError("invalid process deadline or progress interval")
    started = time.monotonic()
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(stage, timeout)
        try:
            return process.wait(timeout=min(interval, remaining))
        except subprocess.TimeoutExpired:
            print(json.dumps({"stage": stage, "status": "running",
                              "elapsed_seconds": round(time.monotonic() - started, 1),
                              "deadline_seconds": timeout}), file=sys.stderr, flush=True)


@contextmanager
def owned(command, **kwargs):
    if os.name == "nt":
        from vaws_windows import owned_process
        with owned_process(command, **kwargs) as process:
            yield process
    else:
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        try:
            yield process
        finally:
            # Kill only our new process group, including orphaned helpers.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def run_captured(command, *, stage, timeout, env=None, cwd=None, encoding="utf-8", limit=65536, input_data=None):
    """No pipe deadlocks, periodic progress and a deadline for the owned tree."""
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        if input_data is not None:
            source.write(input_data)
            source.seek(0)
        try:
            with owned(command, env=env, cwd=cwd, stdin=source if input_data is not None else subprocess.DEVNULL,
                       stdout=stdout, stderr=stderr) as process:
                code = wait_with_progress(process, stage=stage, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            stdout.seek(0)
            stderr.seek(0)
            exc.output, exc.stderr = stdout.read(65536), stderr.read(65536)
            raise
        stdout.seek(0)
        stderr.seek(0)
        out, err = stdout.read(-1 if limit is None else limit), stderr.read(-1 if limit is None else limit)
        if encoding:
            out, err = out.decode(encoding, "replace"), err.decode(encoding, "replace")
        return subprocess.CompletedProcess(command, code, out, err)

"""Select the immutable environment pinned by this client or worktree session.

The bootstrap command is ``uv run --no-project python .agents/scripts/vaws_deps.py sync``. Windows
and WSL environments coexist in the per-user content-addressed store. Interpreter
flags and module entry points survive the hop. Native Windows owns the child
process tree and emits UTF-8 JSON independently of the terminal code page.
"""
from __future__ import annotations

from vaws_diagnostics_adapter import measured as _diagnostic_measured

import os
import sys
import tempfile
from pathlib import Path

from vaws_environment import EnvironmentError, PIN_ENV, native_ready, capability_receipt

REEXEC_ENV = "VAWS_VENV_REEXEC"
SKIP_ENV = "VAWS_SKIP_VENV_REEXEC"
SENTINEL_PACKAGES = ("remote_dev", "vaws_coordinator")
REMEDY = "uv run --no-project python .agents/scripts/vaws_deps.py sync"


def configure_windows_stdio() -> None:
    """Keep native CLI output stable across Windows display languages."""
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


@_diagnostic_measured('entry.interpreter')
def ensure_workspace_interpreter(
    *, repo_root: Path, packages: tuple[str, ...] = SENTINEL_PACKAGES, use_saved: bool = True,
    stdin: bytes | None = None,
) -> None:
    """Enter a prepared runtime; consuming an owner never installs packages."""
    configure_windows_stdio()
    if os.environ.get(SKIP_ENV) == "1":
        return
    from vaws_windows_runtime import configure_windows_runtime
    configure_windows_runtime(repo_root)
    try:
        # Native GUI shells need not inherit the hook/MCP process's pin. Their
        # worktree selection remains valid while the Agent edits dependencies.
        selected = native_ready(repo_root, use_saved=use_saved)
        knowledge = packages == ("vaws_knowledge",)
        receipt = capability_receipt(selected, "knowledge" if knowledge else "runtime", prepare_missing=False)
    except EnvironmentError as exc:
        remedy = ("uv run --no-project python .agents/scripts/knowledge_setup.py"
                  if packages == ("vaws_knowledge",) else REMEDY)
        sys.stderr.write(f"{exc}; run `{remedy}` explicitly in {repo_root}.\n")
        raise SystemExit(2) from exc
    venv_python = Path(receipt["python"])
    needs_utf8 = os.name == "nt" and not sys.flags.utf8_mode
    # POSIX bin/python is a symlink to the base executable. Comparing resolved
    # executables alone would falsely accept an unrelated base environment.
    same_environment = Path(sys.prefix).resolve() == Path(receipt["root"]).resolve()
    if same_environment and not needs_utf8:
        os.environ[PIN_ENV] = selected["receipt"]
        os.environ.pop(REEXEC_ENV, None)
        return
    if not same_environment and os.environ.get(REEXEC_ENV) == receipt["key"]:
        sys.stderr.write("the selected interpreter did not enter its ready environment\n")
        raise SystemExit(2)
    if venv_python.is_file():
        from vaws_diagnostics_adapter import context_environment, event
        env = context_environment(os.environ)
        env[REEXEC_ENV] = receipt["key"]
        env[PIN_ENV] = selected["receipt"]
        env.pop("PYTHONHOME", None)
        env.pop("VIRTUAL_ENV", None)
        executable = os.fsdecode(venv_python)
        original = getattr(sys, "orig_argv", None)
        if original is None:
            # Bootstrap launchers can predate the workspace's Python minimum.
            main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
            arguments = ["-m", main_spec.name, *sys.argv[1:]] if main_spec else list(sys.argv)
        else:
            arguments = original[1:]
        argv = [executable, *(["-X", "utf8"] if needs_utf8 else []), *arguments]
        event("INFO", "interpreter.handoff", runtime=receipt["key"], platform=sys.platform)
        if stdin is not None:
            # A caller may inspect a bounded event before choosing its owner.
            # Replay that input through a real descriptor, not argv or env.
            with tempfile.TemporaryFile() as replay:
                replay.write(stdin)
                replay.seek(0)
                if os.name == "nt":
                    original_stdin = sys.stdin
                    try:
                        sys.stdin = replay
                        from vaws_windows import run_owned

                        raise SystemExit(run_owned(argv, env=env))
                    finally:
                        sys.stdin = original_stdin
                original_fd = os.dup(0)
                try:
                    os.dup2(replay.fileno(), 0)
                    os.execve(executable, argv, env)
                finally:  # Only reached if exec fails; preserve the caller.
                    os.dup2(original_fd, 0)
                    os.close(original_fd)
        elif os.name == "nt":
            from vaws_windows import run_owned

            raise SystemExit(run_owned(argv, env=env))
        else:
            os.execve(executable, argv, env)
    sys.stderr.write(
        "the selected ready interpreter is missing; "
        f"install them with `{REMEDY}` "
        "before running the entry again.\n"
    )
    raise SystemExit(2)

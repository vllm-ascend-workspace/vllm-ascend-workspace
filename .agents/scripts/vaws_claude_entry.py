#!/usr/bin/env python3
"""Launch one Claude provider using its actual native worktree's saved environment.

Claude 2.1.143 reads MCP configuration before WorktreeCreate, but starts the
provider in the returned directory. This small exec boundary resolves that
already-prepared directory; it never updates code or constructs a task.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))

from vaws_environment import PIN_ENV, MANAGED_PIN_ENV, saved_ready
from vaws_workspace_update import common_dir

PROVIDERS = {
    "task": ["-m", "vaws_coordinator", "task-server"],
    "remote": ["-m", "remote_dev.mcp.server"],
    "knowledge": ["-m", "vaws_knowledge.server.mcp_server"],
}


def workspace(cwd: Path, project: str | None = None, *, source: Path = ROOT) -> Path:
    shared = common_dir(source).resolve()
    for start in (cwd, Path(project) if project else cwd):
        for candidate in (start.resolve(), *start.resolve().parents):
            if not (candidate / ".agents/lib/vaws_environment.py").is_file():
                continue
            if common_dir(candidate).resolve() == shared:
                return candidate
    raise ValueError("Claude did not launch this provider in its configured workspace or worktree")


def launch_plan(kind: str, target: Path, options: list[str], environment: dict) -> tuple[list[str], dict]:
    receipt = saved_ready(target)
    environment = dict(environment)
    for key in (MANAGED_PIN_ENV, "PYTHONHOME", "VIRTUAL_ENV", "VAWS_VENV_REEXEC"):
        environment.pop(key, None)
    environment[PIN_ENV] = receipt["receipt"]
    identity = target / ".vaws-local/github.json"
    if identity.is_file():
        environment["VAWS_GITHUB_IDENTITY_FILE"] = str(identity)
    if kind in PROVIDERS:
        if options:
            raise ValueError("provider entries do not accept extra arguments")
        arguments = PROVIDERS[kind]
    else:
        # Hook implementations belong to the selected revision, with the
        # installed source as a bootstrap fallback for an older upstream.
        name = "vaws_session.py" if kind == "session" else "knowledge_summary.py"
        path = target / ".agents/hooks" / name
        if not path.is_file():
            path = ROOT / ".agents/hooks" / name
        arguments = [str(path), "--client", "claude", "--project", str(target),
                     "--environment-receipt", receipt["receipt"], *options]
    return [receipt["python"], *arguments], environment


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=(*PROVIDERS, "session", "summary"))
    args, options = parser.parse_known_args(argv)
    try:
        target = workspace(Path.cwd(), os.environ.get("CLAUDE_PROJECT_DIR"))
        if args.kind in PROVIDERS:
            from vaws_venv import ensure_workspace_interpreter
            ensure_workspace_interpreter(repo_root=ROOT)
            import asyncio
            from vaws_mcp_runtime import serve
            asyncio.run(serve(args.kind, target))
            return 0
        command, environment = launch_plan(args.kind, target, options, os.environ)
        # Claude exposes this file only to SessionStart. Use the native
        # channel for subsequent shell tools, alongside the coordinator's
        # task-context export, without expecting the Agent to run a launcher.
        if args.kind == "session" and os.environ.get("CLAUDE_ENV_FILE"):
            with Path(os.environ["CLAUDE_ENV_FILE"]).open("a", encoding="utf-8") as stream:
                stream.write("\nexport " + PIN_ENV + "=" + shlex.quote(environment[PIN_ENV]) + "\n")
        print(json.dumps({"vaws_claude_provider": args.kind, "workspace": str(target),
                          "python": command[0], "receipt": environment[PIN_ENV]}), file=sys.stderr, flush=True)
        if os.name == "nt":
            from vaws_windows import run_owned
            return run_owned(command, env=environment)
        os.execve(command[0], command, environment)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"VAWS Claude {args.kind} launch unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

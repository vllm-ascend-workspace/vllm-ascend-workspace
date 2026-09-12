#!/usr/bin/env python3
"""Serve one MCP provider from each task's selected immutable environment.

Kimi supplies the stdio cwd. Cursor supplies VAWS_MCP_WORKSPACE through its
native workspace variable. This selects only a saved package environment;
task identity remains the native attachment and is never inferred here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/scripts"))
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_claude_entry import PROVIDERS, launch_plan, workspace
from vaws_coordinator_launch import coordinator_environment
from vaws_environment import PIN_ENV
from vaws_knowledge_service import knowledge_server_env


def provider_plan(kind: str, cwd: Path, environment: dict) -> tuple[Path, list[str], dict]:
    native = Path(environment.get("VAWS_MCP_WORKSPACE") or cwd).expanduser().resolve(strict=True)
    try:
        target = workspace(native, source=ROOT)
    except ValueError:
        # A user-level provider may be visible in unrelated projects. Its
        # installed environment can list tools; task calls still need a real
        # native attachment, independently checked by the coordinator.
        target = ROOT
    command, env = launch_plan(kind, target, [], environment)
    env.pop("VAWS_MCP_WORKSPACE", None)
    if kind == "task":
        env = coordinator_environment(env, repo_root=target)
    elif kind == "remote":
        env.setdefault("REMOTE_DEV_DEFAULT_USER", "root")
        env.setdefault("REMOTE_DEV_STATE_DIR", str(target / ".vaws-local/remote-dev-state"))
    else:
        env = {**knowledge_server_env(target), **env}
    return native, command, env


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=PROVIDERS)
    args = parser.parse_args(argv)
    try:
        from vaws_venv import ensure_workspace_interpreter
        ensure_workspace_interpreter(repo_root=ROOT)
        from vaws_mcp_runtime import serve
        native = Path(os.environ.get("VAWS_MCP_WORKSPACE") or Path.cwd()).resolve()
        try:
            target = workspace(native, source=ROOT)
        except ValueError:
            target = ROOT
        asyncio.run(serve(args.kind, target))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"VAWS {args.kind} provider unavailable: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

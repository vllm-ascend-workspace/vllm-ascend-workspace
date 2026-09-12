#!/usr/bin/env python3
"""Prepare one new native task's workspace; repeat calls reuse its selection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))

from vaws_environment import read_receipt, saved_ready, select_environment
from vaws_knowledge_service import knowledge_config_path
from vaws_local_state import shared_workspace_root
from vaws_native_workspace import create_workspace
from vaws_session_state import task_dir, write_json
from vaws_task_target import resolve_context_file
from vaws_venv import configure_windows_stdio, ensure_workspace_interpreter
from vaws_workspace_entry import copy_workspace_identity, workspace_entry
from vaws_workspace_update import WorkspaceUpdater, common_dir, git, redact, update_lock
from vaws_worktree_setup import configure_target, prepare_selected_knowledge, unpinned_environment

CLIENTS = ("codex", "cursor", "claude", "grok", "kimi")


def native_prepared(context: dict, project: Path) -> tuple[Path, dict] | None:
    """Reuse a prepared native linked worktree, including an explicitly old ref."""
    cwd = Path(context["attachment"]["cwd"])
    try:
        workspace = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
        shared = common_dir(project)
        if common_dir(workspace) != shared:
            return None
        if Path(git(workspace, "rev-parse", "--absolute-git-dir")).resolve() == shared:
            return None
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None
    selection = workspace / ".vaws-local/environment-selection" / f"{sys.platform}.json"
    return (workspace, saved_ready(workspace)) if selection.is_file() else None


def prepare_latest(project: Path) -> tuple[Path, dict, dict]:
    """Prepare canonical HEAD without activating or inspecting the editing branch."""
    updater = WorkspaceUpdater(project)
    update = updater.step(apply=True, activate=False)
    if update.get("status") not in {"ready", "current"}:
        error = RuntimeError(update.get("detail") or update.get("reason") or "upstream preparation failed")
        error.evidence = update
        raise error
    prepared = updater.state.get("prepared")
    if not prepared:
        # An already-current main checkout can lack a preparation cache. Use
        # the updater's clean committed stage, never copy its working edits.
        prepared = updater.prepare(update, updater.preparation_inputs(update))
        updater.save(**{key: value for key, value in update.items() if key != "status"},
                     prepared=prepared, phase="ready", status="ready")
    stage = updater.validate_prepared(update, prepared)
    return stage, prepared, update


def selected_result(workspace: Path, receipt: dict, context: dict, *, status: str, evidence: Path,
                    **facts) -> dict:
    config = knowledge_config_path(workspace)
    return {"status": status, "workspace": str(workspace), "head": git(workspace, "rev-parse", "HEAD"),
            "environment": {key: receipt[key] for key in ("key", "python", "receipt")},
            "context_file": context["context_file"], "native_cwd": context["attachment"]["cwd"],
            "knowledge_config": str(config) if config.is_file() else None,
            "evidence": str(evidence), **facts}


def start(client: str, project: Path = ROOT, context_file: str | None = None) -> dict:
    from vaws_coordinator.agent_session import AgentSessions, load_context

    phase, context, record, workspace = "context", None, None, None
    try:
        context = load_context(resolve_context_file(context_file))
        if context["attachment"]["client"] != client:
            raise ValueError("--client differs from the existing native attachment")
        project = shared_workspace_root(project.resolve())
        record = task_dir(context["session"]["id"], project) / "start.json"
        phase = "preparation_lock"
        # The repository lock also serializes two callers preparing one task.
        # Nothing in the completed path checks upstream or changes task sources.
        with update_lock(project, wait_seconds=180):
            phase = "reuse"
            if record.is_file():
                previous = json.loads(record.read_text(encoding="utf-8"))
                workspace = Path(previous["workspace"])
                receipt = read_receipt(previous["environment"]["receipt"])
                return selected_result(workspace, receipt, context, status="reused", evidence=record,
                                       preparation=previous["preparation"], knowledge=previous.get("knowledge", {}))
            prepared_native = native_prepared(context, project)
            update, knowledge = {}, {}
            if prepared_native:
                workspace, receipt = prepared_native
                preparation = "native"
            else:
                phase = "upstream"
                print("VAWS: preparing the canonical workspace and its locked components", file=sys.stderr, flush=True)
                stage, prepared, update = prepare_latest(project)
                knowledge = prepared.get("knowledge", {})
                workspace = project.parent / (project.name + "-" + context["session"]["id"])
                phase = "worktree"
                print(f"VAWS: creating {workspace}", file=sys.stderr, flush=True)
                create_workspace(stage, workspace, linked=True)
                copy_workspace_identity(project, workspace)
                phase = "environment"
                environment = unpinned_environment()
                # The updater prepared this exact committed stage. Do not let
                # the caller's old VAWS_ENV_RECEIPT select the new task runtime.
                receipt = prepared["receipt"]
                configure_target(client, workspace, receipt, environment)
                select_environment(workspace, receipt)
                preparation = "created"
            phase = "knowledge"
            knowledge = prepare_selected_knowledge(workspace, receipt)
            phase = "sources"
            store = AgentSessions(Path(context["state_dir"]))
            # This is a task source selection, not a native cwd handoff.
            context = store.bind_sources(context, {project.name: str(workspace)})
            result = selected_result(workspace, receipt, context, status="ready", evidence=record,
                                     preparation=preparation, update=update, knowledge=knowledge)
            write_json(record, result)
            if preparation == "created":
                write_json(project / ".vaws-local/latest-runtime.json",
                           {key: result[key] for key in ("workspace", "environment", "head")})
            return result
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        result = {"status": "failed", "phase": phase, "error": redact(str(exc)),
                  "error_type": type(exc).__name__}
        if context:
            result["context_file"] = context["context_file"]
        if workspace:
            result["workspace"] = str(workspace)
        if record:
            path = record.with_name("start-error.json")
            write_json(path, {**result, "details": getattr(exc, "evidence", {})})
            result["evidence"] = str(path)
        return result


def main(argv=None) -> int:
    configure_windows_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=CLIENTS, required=True)
    parser.add_argument("--context-file", help="existing native context, when the shell cannot provide it")
    args = parser.parse_args(argv)
    setup = workspace_entry(shared_workspace_root(ROOT), announce=False)
    if setup["state"] != "configured":
        first_use = setup["state"] in {"identity_pending", "needs_github_user"}
        next_step = ("Follow AGENTS.md First use, forks and updates. Reuse an already supplied personal "
                     "GitHub username, or ask once; workspace_forks.py prepares the personal forks. "
                     "Then prepare dependencies and client wiring as described there." if first_use else
                     "Inspect the reported local initialization state; no identity, update preference or task was changed.")
        print(json.dumps({"status": "needs_setup" if first_use else "failed", "phase": "initialization",
                          "setup": setup, "next": next_step}, ensure_ascii=False))
        return 1
    ensure_workspace_interpreter(repo_root=ROOT)
    result = start(args.client, ROOT, args.context_file)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

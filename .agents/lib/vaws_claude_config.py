"""Wire Claude's native worktree callback and late environment selection.

Claude snapshots provider settings before its creation callback. Only generated
VAWS providers/hooks get a late exec entry; unrelated user configuration stays
with its owner. No default mode, trust, permissions or global file is changed.
"""
from __future__ import annotations

import json
from pathlib import Path

from vaws_environment import PIN_ENV
from vaws_workspace_update import common_dir

KINDS = {
    ("-m", "vaws_coordinator", "task-server"): "task",
    ("-m", "remote_dev.mcp.server"): "remote",
    ("-m", "vaws_knowledge.server.mcp_server"): "knowledge",
}


def same_repository(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return common_dir(left).resolve() == common_dir(right).resolve()
    except (OSError, RuntimeError):
        return False


def owned_entry(path: str, root: Path, name: str) -> bool:
    candidate = Path(path)
    if candidate.name != name or candidate.parent.name != "scripts" or candidate.parent.parent.name != ".agents":
        return False
    return same_repository(candidate.parents[2], root)


def provider_kind(arguments, root: Path) -> str | None:
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        return None
    kind = KINDS.get(tuple(arguments))
    if kind is not None:
        return kind
    if len(arguments) == 2 and arguments[1] in KINDS.values() and any(
        owned_entry(arguments[0], root, name) for name in ("vaws_native_mcp.py", "vaws_claude_entry.py")
    ):
        return arguments[1]
    return None


def wrapped_hook_kind(argv: list[str], root: Path) -> str | None:
    """Recognize only the command forms emitted by Claude setup."""
    if (len(argv) not in {3, 5} or not owned_entry(argv[1], root, "vaws_claude_entry.py")
            or argv[2] not in {"session", "summary"}):
        return None
    if len(argv) == 5 and (argv[3] != "--agent-sessions-dir" or not argv[4]):
        return None
    return argv[2]


def add_claude_setup(files: dict, notes: list, project: Path, root: Path, *,
                     shell_command, parse_command, owned_server) -> None:
    """Adapt the already-merged plan, including old generated configurations."""
    from vaws_local_owner import windows_mounted_workspace
    if windows_mounted_workspace(root):
        notes.append({"path": str(project), "action": "preserved", "reason": "native-claude-setup-requires-windows-owner"})
        return
    hooks_path, mcp_path = project / ".claude/settings.local.json", project / ".mcp.json"
    settings = json.loads(files.get(hooks_path, "{}"))
    servers = json.loads(files.get(mcp_path, "{}"))
    entry = str(root / ".agents/scripts/vaws_claude_entry.py")
    python = None
    for name, server in servers.get("mcpServers", {}).items():
        arguments = server.get("args", [])
        kind = provider_kind(arguments, root)
        already_wrapped = len(arguments) == 2 and owned_entry(arguments[0], root, "vaws_claude_entry.py")
        if already_wrapped and arguments[1] in KINDS.values():
            kind = arguments[1]
        elif kind is None or not owned_server(server, project):
            continue
        python = python or server["command"]
        server["args"] = [entry, kind]
        # A static pin belongs to the source configuration loaded before
        # creation. The exec entry selects the new directory's saved receipt.
        server.get("env", {}).pop(PIN_ENV, None)
    hooks = settings.setdefault("hooks", {})
    for event, groups in hooks.items():
        seen = set()
        for group in groups:
            kept = []
            for item in group.get("hooks", []):
                try:
                    argv = parse_command(item.get("command", ""))
                except (ValueError, OSError):
                    kept.append(item)
                    continue
                kind = None
                options = []
                if len(argv) >= 3 and owned_entry(argv[1], root, "vaws_claude_entry.py"):
                    kind, options = argv[2], argv[3:]
                elif len(argv) >= 2:
                    path = Path(argv[1])
                    if (path.parent.name == "hooks" and path.parent.parent.name == ".agents"
                            and path.name in {"vaws_session.py", "knowledge_summary.py"}
                            and "--client" in argv
                            and argv[argv.index("--client") + 1:argv.index("--client") + 2] == ["claude"]
                            and same_repository(path.parents[2], root)):
                        kind = "session" if path.name == "vaws_session.py" else "summary"
                        if "--agent-sessions-dir" in argv:
                            index = argv.index("--agent-sessions-dir")
                            options = argv[index:index + 2]
                if kind not in {"session", "summary"}:
                    kept.append(item)
                    continue
                python = python or argv[0]
                command = shell_command([argv[0], entry, kind, *options])
                # The general hook merger preserves its unfamiliar previous
                # wrapper and adds the current direct hook. Keep one callback.
                key = (kind, tuple(options))
                if key not in seen:
                    kept.append({**item, "command": command})
                    seen.add(key)
            group["hooks"] = kept
        hooks[event] = [group for group in groups if group.get("hooks")]
    create = hooks.get("WorktreeCreate", [])
    custom = []
    for group in create:
        for item in group.get("hooks", []):
            try:
                argv = parse_command(item.get("command", ""))
            except (ValueError, OSError):
                argv = []
            if len(argv) != 2 or not owned_entry(argv[1], root, "vaws_claude_worktree.py"):
                custom.append(item)
    if custom:
        notes.append({"path": str(hooks_path), "action": "preserved", "reason": "custom-claude-worktree-create"})
    elif python:
        hooks["WorktreeCreate"] = [{"hooks": [{"type": "command", "timeout": 1800,
            "command": shell_command([python, str(root / ".agents/scripts/vaws_claude_worktree.py")])}]}]
    files[hooks_path] = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    files[mcp_path] = json.dumps(servers, ensure_ascii=False, indent=2) + "\n"

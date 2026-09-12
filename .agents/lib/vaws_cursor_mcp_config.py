"""Select Cursor's ordinary user-level providers once for native worktrees.

Project MCP approval includes the worktree's generated folder name. User-level
providers have stable identifiers; a resolved workspace argument separates
their native processes and selects each directory's prepared environment.
"""
from __future__ import annotations

import json
from pathlib import Path

from vaws_environment import PIN_ENV, MANAGED_PIN_ENV
from vaws_claude_config import provider_kind
from vaws_workspace_update import common_dir

KINDS = {
    ("-m", "vaws_coordinator", "task-server"): "task",
    ("-m", "remote_dev.mcp.server"): "remote",
    ("-m", "vaws_knowledge.server.mcp_server"): "knowledge",
}
NAMES = {"vaws-task", "vaws_task", "remote-dev", "remote_dev", "vaws-knowledge", "vaws_knowledge"}
WORKSPACE_ENV = "VAWS_MCP_WORKSPACE"
WORKSPACE_DEFAULTS = {PIN_ENV, MANAGED_PIN_ENV, "REMOTE_DEV_STATE_DIR", "VAWS_KNOWLEDGE_CONFIG",
                      "VAWS_KNOWLEDGE_PROJECT_ROOTS", "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE"}


def same_repository(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return common_dir(left).resolve() == common_dir(right).resolve()
    except (OSError, RuntimeError):
        return False


def owned_entry(server: dict, root: Path) -> bool:
    args = server.get("args", [])
    if (not isinstance(args, list) or len(args) != 2 or not isinstance(args[0], str)
            or args[1] not in KINDS.values()):
        return False
    path = Path(args[0])
    return (path.name == "vaws_native_mcp.py" and path.parent.name == "scripts"
            and path.parent.parent.name == ".agents" and same_repository(path.parents[2], root))


def document(path: Path, files: dict) -> dict:
    text = files.get(path)
    if text is None and path.exists():
        text = path.read_text(encoding="utf-8")
    value = json.loads(text) if text else {}
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers", {}), dict):
        raise ValueError(f"MCP configuration must contain an object of servers: {path}")
    return value


def add_cursor_global_mcp(files: dict, notes: list, project: Path, root: Path, *,
                          owned_server, enable: bool = False, global_path: Path | None = None) -> None:
    """Plan normal configuration files; do not read or write approval state."""
    from vaws_local_owner import windows_mounted_workspace
    if windows_mounted_workspace(root):
        if enable:
            notes.append({"action": "preserved", "reason": "native-cursor-global-mcp-requires-windows-owner"})
        return
    global_path = global_path or Path.home() / ".cursor/mcp.json"
    shared = document(global_path, files)
    global_servers = shared.get("mcpServers", {})
    configured = any(isinstance(server, dict) and owned_entry(server, root)
                     for server in global_servers.values())
    if not enable and not configured:
        return
    project_path = project / ".cursor/mcp.json"
    local = document(project_path, files)
    local_servers = local.get("mcpServers", {})
    global_changed = False
    local_changed = False
    for name, server in list(local_servers.items()):
        if name not in NAMES or not isinstance(server, dict):
            continue
        args = server.get("args", [])
        kind = provider_kind(args, root)
        if kind is None or not owned_server(server, project):
            continue
        existing = global_servers.get(name)
        if existing is not None and (not isinstance(existing, dict) or not owned_entry(existing, root)):
            notes.append({"path": str(global_path), "server": name, "action": "preserved",
                          "reason": "custom-global-mcp-server"})
            continue
        if existing is None:
            if not enable:
                continue
            env = dict(server.get("env", {}))
            for key in WORKSPACE_DEFAULTS:
                env.pop(key, None)
            # Cursor resolves this variable before computing the process key.
            # Plain global stdio starts in HOME, so the entry explicitly chdirs.
            env[WORKSPACE_ENV] = "${workspaceFolder}"
            global_servers[name] = {**server, "args": [str(root / ".agents/scripts/vaws_native_mcp.py"), kind],
                                    "env": env}
            global_changed = True
        del local_servers[name]
        local_changed = True
        notes.append({"path": str(global_path), "server": name, "action": "configured-user-mcp",
                      "reason": "stable-cursor-worktree-provider",
                      "project_entry_removed": str(project_path)})
    if global_changed:
        shared["mcpServers"] = global_servers
        files[global_path] = json.dumps(shared, ensure_ascii=False, indent=2) + "\n"
    if local_changed:
        local["mcpServers"] = local_servers
        files[project_path] = json.dumps(local, ensure_ascii=False, indent=2) + "\n"

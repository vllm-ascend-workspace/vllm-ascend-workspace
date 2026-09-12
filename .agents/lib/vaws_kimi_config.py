"""Install generated Kimi providers once in native user MCP configuration."""
from __future__ import annotations

import json
import hashlib
import re
import tomllib
from pathlib import Path

from vaws_claude_config import KINDS, owned_entry, provider_kind, same_repository
from vaws_environment import PIN_ENV
from vaws_local_owner import managed_path, managed_receipt, windows_mounted_workspace

DERIVED_ENV = {PIN_ENV, "REMOTE_DEV_STATE_DIR", "VAWS_KNOWLEDGE_CONFIG",
               "VAWS_KNOWLEDGE_PROJECT_ROOTS", "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE"}


def _hook_kind(hook, project, root, parse_command, include_summary=False):
    events = {"SessionSetup", "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
              "PreToolUse", "UserPromptSubmit"}
    if include_summary:
        events.add("Stop")
    if not isinstance(hook, dict) or set(hook) - {"event", "command", "timeout"} or hook.get("event") not in events:
        return None
    try:
        argv = parse_command(hook.get("command", ""))
        if (len(argv) == 7 and argv[:4] == ["uv", "run", "--no-project", "python"]
                and owned_entry(argv[4], root, "vaws_kimi_session_setup.py") and argv[5] == "--project"
                and same_repository(Path(argv[6]), project)):
            return "adapter"
        if len(argv) < 6:
            return None
        entry = Path(argv[1])
        names = {"vaws_session.py"}
        if include_summary and hook.get("event") == "Stop":
            names.add("knowledge_summary.py")
        if not (entry.name in names and entry.parent.name == "hooks"
                and entry.parent.parent.name == ".agents" and same_repository(entry.parents[2], root)):
            return None
        options = argv[2:]
        allowed = {"--client", "--project", "--agent-sessions-dir", "--github-identity-file",
                   "--coordinator-state-dir", "--environment-receipt"}
        if len(options) % 2:
            return None
        values = {}
        for index in range(0, len(options), 2):
            key = options[index]
            if key not in allowed or key in values:
                return None
            values[key] = options[index + 1]
        if values.get("--client") == "kimi" and values.get("--project") and same_repository(Path(values["--project"]), project):
            return "direct"
    except (ValueError, OSError, TypeError):
        pass
    return None


def remove_owned_kimi_hooks(text: str, project: Path, root: Path, *, parse_command,
                            include_summary=False) -> str:
    """Remove this family's generated callbacks, retaining custom hooks verbatim.

    The replacement uses official events unless initialization explicitly asks
    for an extension. Old generated markers confer no ownership on user entries
    that were subsequently added inside them.
    """
    table = re.compile(r"^\[\[hooks\]\][ \t]*\n.*?(?=^[ \t]*\[|^# BEGIN VAWS|^# END VAWS|\Z)", re.M | re.S)

    def strip_owned(match):
        try:
            parsed = tomllib.loads(match[0])
        except tomllib.TOMLDecodeError:
            return match[0]
        entries = parsed.get("hooks", [])
        if (set(parsed) == {"hooks"} and len(entries) == 1
                and _hook_kind(entries[0], project, root, parse_command, include_summary)):
            return ""
        return match[0]

    current = hashlib.sha256(str(project).encode()).hexdigest()[:16]
    marker = re.compile(r"^# BEGIN VAWS session-([0-9a-f]{16})\n(.*?)^# END VAWS session-\1(?:\n|$)", re.M | re.S)

    def unmark_replaced(match):
        cleaned = table.sub(strip_owned, match[2])
        return cleaned if cleaned != match[2] or match[1] == current else match[0]

    return table.sub(strip_owned, marker.sub(unmark_replaced, text))


def add_kimi_user_mcp(files: dict, notes: list, project: Path, root: Path, home: Path, *, owned_server) -> None:
    project_path, user_path = project / ".kimi-code/mcp.json", home / "mcp.json"
    local = json.loads(files.get(project_path, "{}"))
    existing = files.get(user_path)
    if existing is None:
        existing = user_path.read_text(encoding="utf-8") if user_path.exists() else "{}"
    user = json.loads(existing)
    user_servers = user.setdefault("mcpServers", {})
    entry = managed_path(root / ".agents/scripts/vaws_native_mcp.py", windows=windows_mounted_workspace(root))
    bootstrap = managed_receipt(root)["python"]
    for name, server in list(local.get("mcpServers", {}).items()):
        kind = provider_kind(server.get("args", []), root)
        if kind is None or not owned_server(server, project):
            continue
        prior = user_servers.get(name)
        if prior is not None:
            args = prior.get("args", [])
            wrapped = (len(args) == 2 and args[1] == kind
                       and owned_entry(args[0], root, "vaws_native_mcp.py"))
            if not wrapped and not owned_server(prior, project):
                notes.append({"path": str(user_path), "server": name, "action": "preserved",
                              "reason": "custom-user-provider"})
                continue
        wanted = {**server, **(prior or {}), "command": bootstrap, "args": [entry, kind]}
        wanted["env"] = {key: value for key, value in {**server.get("env", {}), **(prior or {}).get("env", {})}.items()
                         if key not in DERIVED_ENV}
        user_servers[name] = wanted
        del local["mcpServers"][name]
        notes.append({"path": str(user_path), "server": name, "action": "updated-managed",
                      "reason": "native-user-mcp-saved-workspace-environment"})
    files[user_path] = json.dumps(user, ensure_ascii=False, indent=2) + "\n"
    files[project_path] = json.dumps(local, ensure_ascii=False, indent=2) + "\n"

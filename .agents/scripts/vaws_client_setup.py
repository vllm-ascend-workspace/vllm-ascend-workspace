#!/usr/bin/env python3
"""Install scoped native session hooks and the stdio MCP entries.

This configures files only. It does not grant client trust, change approval
policies, authenticate clients, run hooks, or contact a remote machine. It
never writes a bearer token and must not be applied to the operator's live
client configuration from tests.

Three logical providers use the stable native MCP entry, which dispatches to
the component environment selected by the native task:

* `vaws-task` -> `python -m vaws_coordinator task-server`, which serves
  `vaws_session` / `vaws_run` / `vaws_execution` / `vaws_finish` and optional
  `vaws_message`. Local
  attach/finish need no manager.
* `remote-dev` -> `python -m remote_dev.mcp.server`, which serves `remote_*`.
* `vaws-knowledge` -> `python -m vaws_knowledge.server.mcp_server`, which
  serves `knowledge_query` / `knowledge_explain` / `knowledge_capture`.

`--task-only` writes only the vaws-task entry; it skips remote-dev and
vaws-knowledge.

Preservation: existing user-managed servers and unknown fields are kept.
Generated task servers in this checkout move to the shared native owner when
needed; user-managed launchers remain unchanged. A missing executable or script
does not establish VAWS ownership. JSON and TOML keep unrelated aliases and
native permission settings unchanged.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import ntpath
import os
import posixpath
import shlex
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_venv import ensure_workspace_interpreter

ensure_workspace_interpreter(repo_root=ROOT, use_saved=False)

import tomllib  # noqa: E402

from vaws_coordinator_launch import coordinator_environment
from vaws_knowledge_service import knowledge_owner_env, knowledge_owner_path, knowledge_owner_python
from vaws_local_owner import managed_path as _managed_path, managed_python as _managed_python, managed_receipt, windows_interop_env, windows_mounted_workspace, accessible_windows_path
from vaws_environment import PIN_ENV, native_ready, windows_ready, read_receipt
from vaws_local_state import agent_sessions_root
from vaws_native_task_env import user_task_env
from vaws_claude_config import provider_kind, wrapped_hook_kind
from vaws_remote_dev import state_dir

CLIENTS = {"claude", "grok", "kimi", "codex", "cursor"}
EVENTS = ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop", "PreToolUse", "UserPromptSubmit")
BACKUP_DIR = ROOT / ".vaws-local/client-setup-backups"
TASK_SERVER_NAME = "vaws-task"
REMOTE_DEV_SERVER_NAME = "remote-dev"
KNOWLEDGE_SERVER_NAME = "vaws-knowledge"
HOOK_TIMEOUT_SECONDS = 12
TASK_TOOL_MATCHER = r"(?:^|:|__)vaws_(?:session|run|execution|finish|message)$"
LEGACY_CONTEXT_MATCHERS = frozenset({TASK_TOOL_MATCHER, r"(?:^|:|__)vaws_(session|run|execution|finish|message)$"})
COMPANION_TOOL_MATCHER = r"^(?:MCP:)?(?:mcp__)?(?:vaws[-_]knowledge__knowledge_(?:query|explain|capture)|remote[-_]dev__remote_[a-z_]+)$"
CONTEXT_TOOL_MATCHER = "(?:" + TASK_TOOL_MATCHER + "|" + COMPANION_TOOL_MATCHER + ")"
CURSOR_CONTEXT_TOOL_MATCHER = "(?:" + CONTEXT_TOOL_MATCHER + r"|^MCP:(?:knowledge_(?:query|explain|capture)|remote_[a-z_]+)$)"


def remote_dev_server_args():
    return ["-m", "remote_dev.mcp.server"]


def task_server_args():
    return ["-m", "vaws_coordinator", "task-server"]


def knowledge_server_args():
    return ["-m", "vaws_knowledge.server.mcp_server"]


def kimi_home():
    return Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code"))).expanduser()


def kimi_launch_arguments(project, *, config=None):
    """Use Kimi Code; legacy kimi-cli has different configuration contracts."""
    executable = kimi_home() / "bin" / ("kimi.exe" if os.name == "nt" else "kimi")
    return [str(executable) if executable.is_file() else "kimi"]


def remote_dev_env():
    """Environment the remote-dev MCP server needs; the launcher fills the same defaults."""
    return {
        "REMOTE_DEV_DEFAULT_USER": "root",
        "REMOTE_DEV_STATE_DIR": str(state_dir()),
        PIN_ENV: native_ready(ROOT)["receipt"],
    }


def _resolved_path(value):
    return str(Path(value).expanduser().resolve())


def managed_python():
    """A shared Windows worktree has one Windows coordinator, including from WSL."""
    return _managed_python(ROOT)


def managed_path(value):
    """Arguments to a Windows Python process use its native mounted-drive paths."""
    return _managed_path(value, windows=windows_mounted_workspace(ROOT))


def task_server_env():
    """Environment the task server needs so it shares this workspace's registry."""
    env = coordinator_environment()
    keys = (
        "VAWS_AGENT_SESSIONS_DIR",
        "VAWS_COORDINATOR_STATE_DIR",
        "VAWS_GITHUB_IDENTITY_FILE",
    )
    result = {key: managed_path(env[key]) for key in keys if key in env}
    result[PIN_ENV] = managed_receipt(ROOT)["receipt"]
    return windows_interop_env(result) if windows_mounted_workspace(ROOT) else result


def existing_task_env(client, project, *, kimi_config=None):
    """User-managed vaws-task env already on disk, if any."""
    if client in {"claude", "cursor", "kimi"}:
        path = project / {
            "claude": ".mcp.json",
            "cursor": ".cursor/mcp.json",
            "kimi": ".kimi-code/mcp.json",
        }[client]
        if path.is_file():
            try:
                servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
            except json.JSONDecodeError:
                return {}
            entry = servers.get(TASK_SERVER_NAME) or servers.get("vaws_task")
            if entry is not None:
                return dict(entry.get("env") or {})
        return user_task_env(client, project, kimi_config=kimi_config)
    if client in {"codex", "grok"}:
        path = project / ("." + client) / "config.toml"
        if path.is_file():
            try:
                servers = (tomllib.loads(path.read_text(encoding="utf-8")) or {}).get("mcp_servers") or {}
            except tomllib.TOMLDecodeError:
                return {}
            entry = servers.get("vaws_task") or servers.get("vaws-task") or {}
            return dict(entry.get("env") or {})
    return {}


def launch_env(client, project, *, kimi_config=None):
    """Setup defaults with existing provider env taking precedence."""
    return {**task_server_env(), **existing_task_env(client, project, kimi_config=kimi_config)}


def desired_mcp_servers(*, task_only=False):
    """Ordered `{server name: entry}` for every stdio entry this helper owns."""
    servers = {}
    if not task_only:
        servers[REMOTE_DEV_SERVER_NAME] = {
            "command": native_ready(ROOT)["python"],
            "args": [str(ROOT / ".agents/scripts/vaws_native_mcp.py"), "remote"],
            "type": "stdio",
            "timeout": 600000,
            "env": remote_dev_env(),
        }
    servers[TASK_SERVER_NAME] = {
        "command": managed_python(),
        "args": [managed_path(ROOT / ".agents/scripts/vaws_native_mcp.py"), "task"],
        "type": "stdio",
        "timeout": 600000,
        "env": task_server_env(),
    }
    if not task_only:
        servers[KNOWLEDGE_SERVER_NAME] = {
            "command": knowledge_owner_python(ROOT),
            "args": [knowledge_owner_path(ROOT, ROOT / ".agents/scripts/vaws_native_mcp.py"), "knowledge"],
            "type": "stdio",
            "timeout": 600000,
            "env": knowledge_owner_env(ROOT),
        }
    return servers


def shared_kimi_servers(servers, project):
    """One mounted-drive Kimi config can launch from native Windows and WSL.

    Kimi starts project MCP servers in the project cwd. WSL can execute the
    Windows interpreter directly; a project-relative command works in both.
    Other clients keep their platform-native remote-dev interpreter. Knowledge
    uses the same native Windows owner for a mounted Windows workspace.
    """
    if os.name != "nt" and not windows_mounted_workspace(ROOT):
        return servers
    receipt = windows_ready(ROOT) if windows_mounted_workspace(ROOT) else native_ready(ROOT)
    from vaws_environment_link import link_environment
    if os.name == "nt":
        alias = link_environment(ROOT, key=receipt["key"], environment_root=Path(receipt["root"]))
    else:
        alias = ROOT / ".vaws-local/env-links" / receipt["key"]
    candidate = alias / "Scripts/python.exe"
    if not candidate.is_file():
        raise ValueError("the shared Windows launch alias is missing; run dependency sync in Windows")

    def native(value):
        value = str(value)
        mounted = re.fullmatch(r"/mnt/([a-zA-Z])(?:/(.*))?", value)
        if mounted:
            return mounted[1].upper() + ":\\" + (mounted[2] or "").replace("/", "\\")
        return value if re.fullmatch(r"[a-zA-Z]:[\\/].*", value) else None

    executable, cwd = native(candidate), native(project)
    if not executable or not cwd or ntpath.splitdrive(executable)[0].casefold() != ntpath.splitdrive(cwd)[0].casefold():
        return servers
    command = "./" + ntpath.relpath(executable, cwd).replace("\\", "/")
    path_keys = {"VAWS_AGENT_SESSIONS_DIR", "VAWS_COORDINATOR_STATE_DIR", "VAWS_GITHUB_IDENTITY_FILE", "REMOTE_DEV_STATE_DIR",
                 "VAWS_KNOWLEDGE_CONFIG", "VAWS_KNOWLEDGE_PROJECT_ROOTS", "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE", PIN_ENV}
    return {name: {**entry, "command": command,
                   "env": windows_interop_env({**{key: (native(value) or value) if key in path_keys else value
                           for key, value in entry.get("env", {}).items()}, PIN_ENV: receipt["receipt"]})}
            for name, entry in servers.items()}


def hook_command(client, project, env=None):
    """Self-contained hook command; a GUI client must not inherit setup's shell."""
    env = task_server_env() if env is None else env
    argv = [
        managed_python(),
        managed_path(ROOT / ".agents/hooks/vaws_session.py"),
        "--client", client,
        "--project", managed_path(project),
        "--agent-sessions-dir", env["VAWS_AGENT_SESSIONS_DIR"],
        "--environment-receipt", managed_receipt(ROOT)["receipt"],
    ]
    if env.get("VAWS_GITHUB_IDENTITY_FILE"):
        argv += ["--github-identity-file", env["VAWS_GITHUB_IDENTITY_FILE"]]
    if env.get("VAWS_COORDINATOR_STATE_DIR"):
        argv += ["--coordinator-state-dir", managed_path(env["VAWS_COORDINATOR_STATE_DIR"])]
    return local_hook_command(argv)


def local_hook_command(argv):
    if os.name != "nt":
        return shlex.join(argv)
    # One portable launcher for clients which execute command strings through
    # cmd, PowerShell or a shell configured by the user. Paths stay literal,
    # including spaces, apostrophes, dollar signs and non-ASCII characters.
    literals = " ".join("'" + str(arg).replace("'", "''") + "'" for arg in argv)
    script = ("[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
              "$OutputEncoding = [Console]::OutputEncoding; $env:PYTHONIOENCODING='utf-8'; & ") + literals + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand " + encoded


def hook_argv(command):
    prefix = "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand "
    if not command.startswith(prefix):
        return shlex.split(command)
    try:
        script = base64.b64decode(command[len(prefix):], validate=True).decode("utf-16-le")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid generated hook command") from exc
    begin = ("[Console]::InputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
             "$OutputEncoding = [Console]::OutputEncoding; $env:PYTHONIOENCODING='utf-8'; & ")
    end = "; exit $LASTEXITCODE"
    if not script.startswith(begin) or not script.endswith(end):
        raise ValueError("not an owned hook launcher")
    body = script[len(begin):-len(end)]
    tokens = re.findall(r"'(?:[^']|'')*'", body)
    if " ".join(tokens) != body:
        raise ValueError("invalid literal hook arguments")
    return [token[1:-1].replace("''", "'") for token in tokens]


def hook_groups(client, project, env=None):
    command = hook_command(client, project, env)
    if client == "cursor":
        groups = {
            event[0].lower() + event[1:]: [{"command": command}]
            for event in EVENTS if event not in {"PreToolUse", "UserPromptSubmit"}
        }
        groups["preToolUse"] = [{"command": command, "timeout": HOOK_TIMEOUT_SECONDS,
                                 "matcher": CURSOR_CONTEXT_TOOL_MATCHER}]
        return groups
    groups = {
        event: [{"hooks": [{"type": "command", "command": command, "timeout": HOOK_TIMEOUT_SECONDS}]}]
        for event in EVENTS if not (client == "kimi" and event == "PreToolUse")
    }
    if "PreToolUse" in groups:
        groups["PreToolUse"][0]["matcher"] = CONTEXT_TOOL_MATCHER
    return groups


OWNED_HOOK_SCRIPT = ROOT / ".agents/hooks/vaws_session.py"


def _is_python_interpreter(program):
    name = str(program).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name == "python" or name.startswith("python3")


def executed_hook_script(argv):
    """Return the script file a command would run, or None.

    A basename `vaws_session.py` anywhere in argv is not proof of ownership.
    Wrappers that pass our hook path as data are not owned.
    """
    index = 0
    while index < len(argv):
        item = argv[index]
        if item.startswith("-"):
            break
        if "=" in item:
            key = item.split("=", 1)[0]
            if key.isidentifier() and Path(item).suffix != ".py":
                index += 1
                continue
        break
    if index >= len(argv):
        return None
    program = argv[index]
    if _is_python_interpreter(program):
        index += 1
        while index < len(argv):
            item = argv[index]
            if item == "--":
                return argv[index + 1] if index + 1 < len(argv) else None
            if item in {"-c", "-m"}:
                return None
            if item.startswith("-"):
                if item in {"-W", "-X", "--check-hash-based-pycs"}:
                    index += 2
                    continue
                index += 1
                continue
            return item
        return None
    return program


def owned_hook_script(path, expected=None):
    try:
        wanted = expected if expected else OWNED_HOOK_SCRIPT
        allowed = {hook_path_identity(OWNED_HOOK_SCRIPT), hook_path_identity(ROOT / ".agents/hooks/knowledge_summary.py")}
        if hook_path_identity(wanted) not in allowed:
            return False
        return hook_path_identity(path) == hook_path_identity(wanted)
    except OSError:
        return False


def hook_path_identity(value):
    """Compare an owned hook's native and WSL mounted-drive spellings."""
    value = str(value)
    mounted = re.fullmatch(r"/mnt/([a-zA-Z])(?:/(.*))?", value)
    if mounted:
        value = mounted[1] + ":/" + (mounted[2] or "")
    if re.fullmatch(r"[a-zA-Z]:[\\/].*", value):
        return value.replace("\\", "/").rstrip("/").casefold()
    return str(Path(value).expanduser().resolve())


def owned_hook_command(command, client, project, expected=None):
    """True when `command` execs this checkout's hook for this client and project.

    Old generated commands (client/project only) and new ones (explicit
    root/registry flags) both match. A same-basename script in another path
    or worktree does not.
    """
    try:
        argv = hook_argv(command)
    except ValueError:
        return False
    script = executed_hook_script(argv)
    if client == "claude" and len(argv) >= 2 and script == argv[1] and _is_python_interpreter(argv[0]):
        kind = wrapped_hook_kind(argv, ROOT)
        if kind is not None:
            direct = ROOT / ".agents/hooks" / ("vaws_session.py" if kind == "session" else "knowledge_summary.py")
            return owned_hook_script(direct, expected=expected)
    if not script or not owned_hook_script(script, expected=expected):
        return False
    parsed_client = None
    parsed_project = None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--client" and index + 1 < len(argv):
            parsed_client = argv[index + 1]
            index += 2
            continue
        if item.startswith("--client="):
            parsed_client = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--project" and index + 1 < len(argv):
            parsed_project = argv[index + 1]
            index += 2
            continue
        if item.startswith("--project="):
            parsed_project = item.split("=", 1)[1]
            index += 1
            continue
        index += 1
    if parsed_client != client or not parsed_project:
        return False
    try:
        return hook_path_identity(parsed_project) == hook_path_identity(project)
    except OSError:
        return parsed_project == str(project)


def _desired_hook_command(groups):
    if not groups:
        return ""
    group = groups[0]
    if "hooks" in group:
        entries = group.get("hooks") or []
        return entries[0].get("command", "") if entries else ""
    return group.get("command", "")


def merge_hook_event(existing, desired, client, project):
    """Replace one owned hook entry; keep siblings and group metadata."""
    desired_command = _desired_hook_command(desired)
    expected = executed_hook_script(hook_argv(desired_command)) if desired_command else None
    replaced = False
    result = []
    for group in existing:
        if not isinstance(group, dict):
            result.append(group)
            continue
        if "hooks" in group:
            entries = []
            for entry in group.get("hooks") or []:
                if owned_hook_command(entry.get("command", ""), client, project, expected=expected):
                    if replaced:
                        continue
                    updated = dict(entry)
                    updated["command"] = desired_command
                    updated.setdefault("type", "command")
                    updated["timeout"] = desired[0]["hooks"][0].get("timeout", HOOK_TIMEOUT_SECONDS)
                    entries.append(updated)
                    replaced = True
                else:
                    entries.append(entry)
            if entries:
                updated_group = dict(group)
                updated_group["hooks"] = entries
                matcher = desired[0].get("matcher")
                owned = [entry for entry in entries if owned_hook_command(
                    entry.get("command", ""), client, project, expected=expected)]
                if matcher and owned and group.get("matcher") in LEGACY_CONTEXT_MATCHERS:
                    custom = [entry for entry in entries if entry not in owned]
                    if custom:
                        # Expanding a VAWS matcher must not expand a sibling's
                        # user-selected scope. Keep that group and move ours.
                        result.append({**updated_group, "hooks": custom})
                        result.append({"matcher": matcher, "hooks": owned})
                        continue
                    updated_group["matcher"] = matcher
                elif "matcher" not in group and matcher and len(owned) == len(entries):
                    updated_group["matcher"] = matcher
                result.append(updated_group)
            continue
        command = group.get("command", "")
        if owned_hook_command(command, client, project, expected=expected):
            if replaced:
                continue
            updated = dict(group)
            updated["command"] = desired_command
            if desired[0].get("matcher"):
                if "matcher" not in updated or updated["matcher"] in LEGACY_CONTEXT_MATCHERS:
                    updated["matcher"] = desired[0]["matcher"]
            result.append(updated)
            replaced = True
        else:
            result.append(group)
    if not replaced:
        result.extend(desired)
    return result





def server_command_identity(command, checkout):
    """Resolve generated commands against their configuration project, not cwd."""
    command = str(command).replace("\\", "/")
    if not re.match(r"(?:[a-zA-Z]:/|/)", command):
        command = hook_path_identity(checkout) + "/" + command
    return hook_path_identity(posixpath.normpath(command))


def owned_workspace_interpreter(command, checkout):
    value = server_command_identity(command, checkout)
    prefix = hook_path_identity(ROOT).rstrip("/") + "/.vaws-local/env-links/"
    return value.startswith(prefix) and bool(re.fullmatch(r"[0-9a-f]{64}/(?:[Ss]cripts/python\.exe|bin/python)", value[len(prefix):]))


def owned_environment_server(existing, checkout):
    if provider_kind(existing.get("args"), ROOT) is None:
        return False
    if owned_workspace_interpreter(existing.get("command", ""), checkout):
        return True
    pin = existing.get("env", {}).get(PIN_ENV)
    if not pin:
        return False
    try:
        receipt = read_receipt(pin)
    except (OSError, ValueError, RuntimeError):
        return False
    return server_command_identity(existing.get("command", ""), checkout) == hook_path_identity(receipt["python"])


def legacy_generated_toml_server(text, name, key, existing, checkout):
    """Recognize the old generated block, not an arbitrary local Python server."""
    if existing.get("args") not in (task_server_args(), knowledge_server_args(), remote_dev_server_args()):
        return False
    command = server_command_identity(existing.get("command", ""), checkout)
    if command not in {server_command_identity(ROOT / relative, checkout)
                       for relative in (".venv/bin/python", ".venv/Scripts/python.exe")}:
        return False
    blocks = re.findall(r"^# BEGIN VAWS " + re.escape(name) + r"\n(.*?)^# END VAWS "
                        + re.escape(name) + r"(?:\n|\Z)", text, re.M | re.S)
    if len(blocks) != 1:
        return False
    try:
        return tomllib.loads(blocks[0]) == {"mcp_servers": {key: existing}}
    except tomllib.TOMLDecodeError:
        return False


def managed_environment_change(existing, desired, *, checkout):
    return (provider_kind(existing.get("args"), ROOT) == provider_kind(desired.get("args"), ROOT)
            and owned_environment_server(existing, checkout)
            and (existing.get("args") != desired.get("args") or existing.get("command") != desired.get("command")
                 or existing.get("env", {}).get(PIN_ENV) != desired.get("env", {}).get(PIN_ENV)))


def knowledge_owner_defaults(existing, desired, checkout, *, legacy=False):
    """Normalize generated knowledge paths without replacing custom locations."""
    environment = dict(existing.get("env") or {})
    if (provider_kind(existing.get("args"), ROOT) == provider_kind(desired.get("args"), ROOT) == "knowledge"
            and (legacy or owned_environment_server(existing, checkout))):
        if desired.get("env", {}).get("VAWS_KNOWLEDGE_CONFIG"):
            defaults = {"VAWS_KNOWLEDGE_PROJECT_ROOTS": ".agents/knowledge",
                        "VAWS_KNOWLEDGE_CANDIDATE_ROOT": ".vaws-local/knowledge/candidate",
                        "VAWS_KNOWLEDGE_STATE": ".vaws-local/knowledge/instance"}
            for key, relative in defaults.items():
                if key in environment and key not in desired.get("env", {}) and (
                    server_command_identity(environment[key], ROOT) == server_command_identity(ROOT / relative, ROOT)
                ):
                    environment.pop(key)
        for key in ("VAWS_KNOWLEDGE_CONFIG", "VAWS_KNOWLEDGE_PROJECT_ROOTS",
                    "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE"):
            value = desired.get("env", {}).get(key)
            if key in environment and value is not None and (
                server_command_identity(environment[key], checkout) == server_command_identity(value, checkout)
            ):
                environment[key] = value
    return environment


def update_toml_server_command(text, key, command):
    headers = {f"[mcp_servers.{key}]", f"[mcp_servers.{json.dumps(key)}]"}
    lines = text.splitlines(keepends=True)
    inside = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped in headers
        elif inside and re.match(r"^command\s*=", stripped):
            lines[index] = "command = " + json.dumps(command) + "\n"
            updated = "".join(lines)
            tomllib.loads(updated)
            return updated
    return text


def update_toml_server_args(text, key, arguments):
    headers = {f"[mcp_servers.{key}]", f"[mcp_servers.{json.dumps(key)}]"}
    lines = text.splitlines(keepends=True)
    inside = False
    for index, line in enumerate(lines):
        if line.strip().startswith("["):
            inside = line.strip() in headers
        elif inside and re.match(r"^args\s*=", line.strip()):
            # Generated launch arrays occupy one line; custom multiline arrays
            # remain unchanged and the complete TOML parse below rejects drift.
            lines[index] = "args = " + json.dumps(arguments) + "\n"
            candidate = "".join(lines)
            tomllib.loads(candidate)
            return candidate
    return text


def merge_server_entry(existing, desired, *, checkout=None):
    """Update generated environment entries; preserve user-managed entries."""
    checkout = ROOT if checkout is None else checkout
    if (provider_kind(existing.get("args"), ROOT) != provider_kind(desired.get("args"), ROOT)
            or not owned_environment_server(existing, checkout)):
        return dict(existing), "preserved"
    existing = {**existing, "env": knowledge_owner_defaults(existing, desired, checkout)} if "env" in existing else existing
    if managed_environment_change(existing, desired, checkout=checkout):
        environment = {**desired.get("env", {}), **existing.get("env", {})}
        if PIN_ENV in desired.get("env", {}):
            environment[PIN_ENV] = desired["env"][PIN_ENV]
        for key, value in desired.get("env", {}).items():
            if key != PIN_ENV and key in environment and hook_path_identity(environment[key]) == hook_path_identity(value):
                environment[key] = value
        if "WSLENV" in desired.get("env", {}):
            environment = windows_interop_env(environment)
        return {**desired, **existing, "command": desired["command"], "args": desired["args"], "env": environment}, "updated-managed"
    merged = {**desired, **existing}
    desired_env = dict(desired.get("env") or {})
    existing_env = dict(existing.get("env") or {})
    if desired_env or existing_env:
        merged["env"] = {**desired_env, **existing_env}
        if "WSLENV" in desired_env:
            merged["env"] = windows_interop_env(merged["env"])
    preserved = any(
        key in existing and existing.get(key) != desired.get(key)
        for key in ("command", "args", "type")
    )
    return merged, "preserved" if preserved else None



def mcp_server_aliases(name):
    """Hyphen name plus the underscore form TOML/JSON may already use."""
    aliases = []
    for alias in (name, name.replace("-", "_")):
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def merge_json(path, *, hooks=None, mcp=None, notes=None, client=None, project=None):
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    notes = [] if notes is None else notes
    if hooks:
        target = value.setdefault("hooks", {})
        for event, groups in hooks.items():
            merged = target.get(event) or []
            # A native event can own both session and summary hook commands.
            for group in groups:
                merged = merge_hook_event(merged, [group], client, project)
            target[event] = merged
        if path.parent.name == ".cursor":
            value.setdefault("version", 1)
    if mcp:
        servers = value.setdefault("mcpServers", {})
        checkout = project or ROOT
        for name, desired in mcp.items():
            aliases = mcp_server_aliases(name)
            found_keys = [alias for alias in aliases if alias in servers]
            if not found_keys:
                servers[name] = dict(desired)
                continue
            for source_key in found_keys:
                merged, action = merge_server_entry(
                    servers[source_key], desired, checkout=checkout
                )
                servers[source_key] = merged
                if action in {"preserved", "updated-managed"}:
                    notes.append({
                        "path": str(path), "server": name, "alias": source_key,
                        "action": action,
                        "reason": "shared-native-owner" if action == "updated-managed" else "existing-named-server",
                    })
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    return text


def toml_server_body(key, entry):
    body = f"[mcp_servers.{key}]\ncommand = " + json.dumps(entry["command"]) + "\n"
    body += "args = " + json.dumps(entry["args"]) + "\n"
    env = entry.get("env") or {}
    if env:
        body += f"\n[mcp_servers.{key}.env]\n"
        body += "".join(item + " = " + json.dumps(value) + "\n" for item, value in env.items())
    return body


def fill_toml_server_env(text, key, existing, desired, *, checkout=None, legacy=False):
    """Add missing defaults to an ordinary env table; preserve user values/text."""
    desired_env = dict(desired.get("env") or {})
    checkout = ROOT if checkout is None else checkout
    normalized = knowledge_owner_defaults(existing, desired, checkout, legacy=legacy)
    if legacy and existing.get("args") == remote_dev_server_args():
        defaults = {"REMOTE_DEV_DEFAULT_ROOT": "/vllm-workspace", "REMOTE_DEV_DEFAULT_CWD": "/vllm-workspace",
                    "REMOTE_DEV_RUNTIME_ENV_FILE": "/etc/profile.d/vaws-ascend-env.sh"}
        for name, value in defaults.items():
            if normalized.get(name) == value and name not in desired_env:
                normalized.pop(name)
        resolver = normalized.get("REMOTE_DEV_RESOLVERS", "")
        if (isinstance(resolver, str) and resolver.endswith(":setup") and server_command_identity(resolver[:-6], checkout)
                == server_command_identity(ROOT / ".agents/lib/vaws_remote_dev_plugin.py", checkout)):
            normalized.pop("REMOTE_DEV_RESOLVERS")
    removals = set(existing.get("env", {})) - set(normalized)
    replacements = {name: value for name, value in normalized.items()
                    if value != existing.get("env", {}).get(name)}
    if (legacy or owned_environment_server(existing, checkout)) and PIN_ENV in desired_env:
        replacements[PIN_ENV] = desired_env[PIN_ENV]
    if "WSLENV" in desired_env:
        desired_env["WSLENV"] = windows_interop_env({**desired_env, **existing.get("env", {})})["WSLENV"]
        replacements["WSLENV"] = desired_env["WSLENV"]
    if replacements or removals:
        headers = {f"[mcp_servers.{key}.env]", f"[mcp_servers.{json.dumps(key)}.env]"}
        lines = text.splitlines(keepends=True)
        inside = False
        for index, line in enumerate(lines):
            if line.strip().startswith("["):
                inside = line.strip() in headers
            elif inside:
                assignment = re.match(r'^(\s*([A-Za-z_][A-Za-z0-9_]*|"[A-Za-z_][A-Za-z0-9_]*")\s*=\s*)', line)
                if assignment:
                    name = assignment[2].strip('"')
                    if name in removals:
                        lines[index] = ""
                    elif name in replacements:
                        lines[index] = assignment[1] + json.dumps(replacements[name]) + ("\n" if line.endswith("\n") else "")
        text = "".join(lines)
    missing = {name: value for name, value in desired_env.items()
               if name not in (existing.get("env") or {})}
    if not missing:
        return text
    additions = "".join(json.dumps(name) + " = " + json.dumps(value) + "\n"
                        for name, value in missing.items())
    headers = {f"[mcp_servers.{key}.env]", f"[mcp_servers.{json.dumps(key)}.env]"}
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.strip() in headers:
            lines.insert(index + 1, additions)
            result = "".join(lines)
            tomllib.loads(result)
            return result
    if "env" not in existing:
        result = text.rstrip() + f"\n\n[mcp_servers.{json.dumps(key)}.env]\n" + additions
        tomllib.loads(result)
        return result
    return text  # a hand-written inline env remains user-owned


def editable_toml_server_env(text, key, existing):
    """Only edit supported env tables, so interpreter and pin move together."""
    if "env" not in existing:
        return True
    headers = {f"[mcp_servers.{key}.env]", f"[mcp_servers.{json.dumps(key)}.env]"}
    return any(line.strip() in headers for line in text.splitlines())


def configuration(client, project, *, kimi_config=None, task_only=False):
    return build_plan(client, project, kimi_config=kimi_config, task_only=task_only)["files"]


def build_plan(client, project, *, kimi_config=None, task_only=False, kimi_session_setup=False,
               cursor_global_mcp=False, codex_global_hooks=False):
    project = project.expanduser().resolve(strict=True)
    env = launch_env(client, project, kimi_config=kimi_config)
    groups = hook_groups(client, project, env)
    # Kimi's adapter reads only the known session's final completed wire step.
    if not task_only:
        summary_command = local_hook_command([
            knowledge_owner_python(ROOT), knowledge_owner_path(ROOT, ROOT / ".agents/hooks/knowledge_summary.py"),
            "--client", client, "--project", knowledge_owner_path(ROOT, project),
            "--environment-receipt", managed_receipt(ROOT)["receipt"],
        ])
        if client == "cursor":
            groups["afterAgentResponse"] = [{"command": summary_command}]
            groups.setdefault("sessionEnd", []).append({"command": summary_command})
        else:
            groups["Stop"] = [{"hooks": [{"type": "command", "command": summary_command, "timeout": 5}]}]
    servers = desired_mcp_servers(task_only=task_only)
    if client == "kimi":
        servers = shared_kimi_servers(servers, project)
        servers = {name: {**{key: value for key, value in entry.items() if key not in {"type", "timeout"}},
                          "env": {**entry.get("env", {}), "VAWS_MCP_CLIENT": "kimi"},
                          "toolTimeoutMs": 600000}
                   for name, entry in servers.items()}
    files = {}
    notes = []
    if client in {"claude", "cursor", "codex", "grok"}:
        relative = {
            "claude": ".claude/settings.local.json",
            "cursor": ".cursor/hooks.json",
            "codex": ".codex/hooks.json",
            "grok": ".grok/hooks/vaws-session.json",
        }[client]
        path = project / relative
        files[path] = merge_json(path, hooks=groups, notes=notes, client=client, project=project)
    if client in {"claude", "cursor", "kimi"}:
        path = project / {
            "claude": ".mcp.json",
            "cursor": ".cursor/mcp.json",
            "kimi": ".kimi-code/mcp.json",
        }[client]
        files[path] = merge_json(path, mcp=servers, notes=notes, project=project)
    if client in {"codex", "grok"}:
        path = project / ("." + client) / "config.toml"
        original = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        existing = original.get("mcp_servers", {})
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        changed = False
        for name, entry in servers.items():
            key = name.replace("-", "_")
            aliases = mcp_server_aliases(name)
            matching = [alias for alias in aliases if alias in existing]
            if matching:
                for alias in matching:
                    before = text
                    legacy = legacy_generated_toml_server(text, name, alias, existing[alias], project)
                    if (provider_kind(existing[alias].get("args"), ROOT) == provider_kind(entry.get("args"), ROOT)
                            and (legacy or owned_environment_server(existing[alias], project))
                            and editable_toml_server_env(text, alias, existing[alias])):
                        try:
                            candidate = update_toml_server_command(text, alias, entry["command"])
                            candidate = update_toml_server_args(candidate, alias, entry["args"])
                            candidate = fill_toml_server_env(candidate, alias, existing[alias], entry,
                                                             checkout=project, legacy=legacy)
                            rendered = tomllib.loads(candidate)["mcp_servers"][alias]
                        except tomllib.TOMLDecodeError:
                            candidate = before
                            rendered = existing[alias]
                        desired_pin = entry.get("env", {}).get(PIN_ENV)
                        if (rendered.get("command") == entry["command"] and rendered.get("args") == entry["args"]
                                and (desired_pin is None or rendered.get("env", {}).get(PIN_ENV) == desired_pin)):
                            text = candidate
                    updated = text != before
                    changed = changed or updated
                    notes.append({
                        "path": str(path), "server": name, "alias": alias,
                        "action": "updated-managed" if updated else "preserved",
                        "reason": "shared-native-owner" if updated else "existing-named-server",
                    })
                continue
            text = managed_toml_text(text, name, toml_server_body(key, entry))
            changed = True
        if changed:
            files[path] = text
    if client == "kimi":
        from vaws_kimi_config import add_kimi_user_mcp, remove_owned_kimi_hooks
        path = kimi_config or kimi_home() / "config.toml"
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        project_key = hashlib.sha256(str(project).encode()).hexdigest()[:16]
        # Official events are the default. A previous personal binary must not
        # make SessionSetup a permanent requirement for later initialization.
        extended = kimi_session_setup
        command = local_hook_command(["uv", "run", "--no-project", "python",
                                      str(ROOT / ".agents/scripts/vaws_kimi_session_setup.py"),
                                      "--project", str(project)]) if extended else groups["SessionStart"][0]["hooks"][0]["command"]
        events = [*( ["SessionSetup"] if extended else []), *groups]
        body = "\n".join(
            "[[hooks]]\nevent = " + json.dumps(event) + "\ncommand = " + json.dumps(
                groups[event][0]["hooks"][0]["command"] if event == "Stop" else command)
            + "\ntimeout = " + str(600 if event == "SessionSetup" else HOOK_TIMEOUT_SECONDS) + "\n"
            for event in events
        )
        original = remove_owned_kimi_hooks(original, project, ROOT, parse_command=hook_argv,
                                           include_summary=not task_only)
        files[path] = managed_toml_text(original, "session-" + project_key, body)
        add_kimi_user_mcp(files, notes, project, ROOT, path.parent,
                          owned_server=owned_environment_server)
    from vaws_native_setup_config import add_native_setup
    add_native_setup(files, notes, client, project, ROOT)
    if client == "codex":
        from vaws_codex_config import add_codex_setup
        add_codex_setup(files, notes, project, ROOT, shell_command=local_hook_command,
                        parse_command=hook_argv, enable=codex_global_hooks)
    if client == "cursor":
        from vaws_cursor_mcp_config import add_cursor_global_mcp
        add_cursor_global_mcp(files, notes, project, ROOT, owned_server=owned_environment_server,
                              enable=cursor_global_mcp)
    if client == "claude":
        from vaws_claude_config import add_claude_setup
        add_claude_setup(files, notes, project, ROOT, shell_command=local_hook_command,
                         parse_command=hook_argv, owned_server=owned_environment_server)
    executable_files = []
    if client == "grok":
        from vaws_grok_setup_config import plan_grok_setup
        executable_files = plan_grok_setup(files, notes, project, ROOT)
    from vaws_start_guidance import add_start_guidance
    add_start_guidance(files, notes, client, project)
    return {
        "files": files,
        "executable_files": executable_files,
        "mcp_servers": {name: entry["args"] for name, entry in servers.items()},
        "notes": notes,
        "task_registry": str(agent_sessions_root()),
        "launch_argv": kimi_launch_arguments(project, config=kimi_config) if client == "kimi" else None,
        "launch_cwd": str(project),
    }



def managed_toml_text(original, name, text):
    begin, end = f"# BEGIN VAWS {name}\n", f"# END VAWS {name}\n"
    if begin in original:
        before, rest = original.split(begin, 1)
        _, after = rest.split(end, 1)
        original = before + after
    result = original.rstrip() + "\n\n" + begin + text.rstrip() + "\n" + end
    tomllib.loads(result)
    return result


def apply_plan(plan):
    """Write a reviewed/generated native configuration, retaining private backups."""
    changed = []
    for path, content in plan["files"].items():
        mode = 0o700 if path in plan.get("executable_files", []) else 0o600
        payload = content.encode("utf-8")
        if path.exists() and path.read_bytes() == payload:
            if os.name != "nt" and mode == 0o700 and path.stat().st_mode & 0o777 != mode:
                path.chmod(mode)
                changed.append({"path": str(path), "action": "executable-mode-repaired"})
            continue
        item = {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup = BACKUP_DIR / (hashlib.sha256(str(path).encode()).hexdigest()[:16] + "-" + str(time.time_ns()))
            backup.write_bytes(path.read_bytes())
            backup.chmod(0o600)
            item["backup"] = str(backup)
        temporary = path.with_name(path.name + ".vaws-" + str(time.time_ns()))
        # Match the planned bytes on every OS. CRLF translation breaks Git's
        # shell hooks and makes the reported content hash differ on Windows.
        temporary.write_bytes(payload)
        temporary.chmod(mode)
        os.replace(temporary, path)
        changed.append(item)
    return changed


def setup_installed_clients(args):
    """One-time initialization; each installed client keeps its existing builder."""
    from vaws_client_inventory import installed_clients
    from vaws_native_mode_config import add_grok_import_dedup, add_native_mode

    clients = {}
    for client, installation in installed_clients().items():
        row = {"installation": installation, "files": [], "notes": [],
               "native_worktree": {"status": "not_configured", "missing_action": None}}
        clients[client] = row
        if not installation["installed"]:
            row.update(state="skipped", reason="not_installed")
            continue
        phase, current_path = "plan", None
        try:
            plan = build_plan(client, args.project, kimi_config=args.kimi_config, task_only=args.task_only,
                              kimi_session_setup=bool(client == "kimi" and args.kimi_session_setup),
                              codex_global_hooks=client == "codex", cursor_global_mcp=client == "cursor")
            row["notes"] = plan["notes"]
            add_native_mode(plan["files"], plan["notes"], client, args.project)
            if client == "grok":
                add_grok_import_dedup(plan["files"], plan["notes"], args.project, ROOT,
                                      owned_server=owned_environment_server)
            row["mcp_servers"] = plan["mcp_servers"]
            row["launch_argv"], row["launch_cwd"] = plan["launch_argv"], plan["launch_cwd"]
            phase = "apply" if args.apply else "preview"
            for path, content in plan["files"].items():
                current_path = str(path)
                if args.apply:
                    row["files"].extend(apply_plan({**plan, "files": {path: content}}))
                elif not path.exists() or path.read_bytes() != content.encode("utf-8"):
                    row["files"].append({"path": str(path), "sha256": hashlib.sha256(content.encode()).hexdigest()})
            row["state"] = "configured" if args.apply else "preview"
            row["workspace_start"] = {"status": "configured" if args.apply else "planned",
                                      "entry": ".agents/scripts/vaws_start.py", "trigger": "new-session-project-guidance",
                                      "native_patch_required": False, "resume": "reuse-existing-workspace"}
            native = {"status": "optional_native_wiring", "missing_action": None}
            if client in {"codex", "cursor"}:
                native["default_mode"] = "client_choice"
            elif client == "grok":
                native.update(scope="native --worktree, /new and /fork preferences", initial_cli_start="project-guidance")
            elif client == "claude":
                native.update(default_mode="project-guidance")
            else:
                native.update(default_mode="explicit_extension" if args.kimi_session_setup else "project-guidance")
            row["native_worktree"] = native
        except Exception as exc:
            row.update(state="failed", error={"phase": phase, "path": current_path,
                                               "type": type(exc).__name__, "message": str(exc)})
            row["native_worktree"] = {"status": "not_configured",
                                      "missing_action": "Repair this client's reported configuration error and rerun initialization."}
    result = {"state": "partial" if any(row["state"] in {"failed", "blocked"} for row in clients.values()) else
                       "wiring_configured" if args.apply else "preview",
              "clients": clients, "trust_granted": False, "connected": False}
    if args.apply:
        from vaws_local_state import shared_workspace_root, utc_now_iso
        from vaws_session_state import write_json
        path = shared_workspace_root(args.project) / ".vaws-local/client-initialization.json"
        record = {"state": result["state"], "attempted_at": utc_now_iso(), "project": str(args.project.resolve()),
                  "clients": {name: {key: value for key, value in row.items()
                                     if key in {"state", "reason", "files", "native_worktree", "workspace_start", "error"}}
                              for name, row in clients.items()}}
        try:
            write_json(path, record)
            result["record"] = str(path)
        except OSError as exc:
            result.update(state="partial", record_error={"path": str(path), "message": str(exc)})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=sorted(CLIENTS | {"all"}), required=True,
                        help="One client for scoped wiring, or all for one-time initialization of installed clients")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--kimi-config", type=Path, help="Explicit Kimi Code configuration file to edit for scoped session hooks")
    parser.add_argument("--kimi-session-setup", action="store_true", help="Enable the Kimi native SessionSetup extension after installing the patched client")
    parser.add_argument("--cursor-global-mcp", action="store_true", help="Install generated Cursor providers once in its native user MCP configuration")
    parser.add_argument("--codex-global-hooks", action="store_true", help="Install fixed Codex user hooks for this Git worktree family; native review remains separate")
    parser.add_argument(
        "--task-only",
        action="store_true",
        help="Write only the vaws-task entry; skip remote-dev and vaws-knowledge",
    )
    parser.add_argument("--apply", action="store_true", help="Write with private backups; default is preview")
    args = parser.parse_args(argv)
    if args.client == "all":
        result = setup_installed_clients(args)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result["state"] == "partial" else 0
    plan = build_plan(args.client, args.project, kimi_config=args.kimi_config, task_only=args.task_only,
                      kimi_session_setup=args.kimi_session_setup, cursor_global_mcp=args.cursor_global_mcp,
                      codex_global_hooks=args.codex_global_hooks)
    changed = apply_plan(plan) if args.apply else [
        {"path": str(path), "sha256": hashlib.sha256(content.encode()).hexdigest()}
        for path, content in plan["files"].items()
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]
    print(json.dumps({
        "state": "configured" if args.apply else "preview",
        "files": changed,
        "mcp_servers": plan["mcp_servers"],
        "notes": plan["notes"],
        "launch_argv": plan["launch_argv"],
        "launch_cwd": plan["launch_cwd"],
        "preserved_servers": [
            note for note in plan["notes"] if note.get("reason") == "existing-named-server"
        ],
        "trust_granted": False,
        "connected": False,
        "next": (
            "Review native client trust/approval prompts for each listed server, "
            "restart or resume the client, then verify actual calls. "
            "Do not treat this helper as a rewrite of hand-managed providers."
        ),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Workspace roots and optional Markdown lookup over the installed knowledge owner."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from vaws_local_owner import accessible_windows_path, managed_path, managed_python, managed_receipt, windows_interop_env, windows_mounted_workspace
from vaws_local_state import shared_workspace_root
from vaws_session_state import write_json

if TYPE_CHECKING:
    from vaws_knowledge.server.layers import ServiceConfig

PROJECT_ROOT_RELATIVE = ".agents/knowledge"
CANDIDATE_ROOT_RELATIVE = ".vaws-local/knowledge/candidate"
LOCATION_ENV = ("VAWS_KNOWLEDGE_CONFIG", "VAWS_KNOWLEDGE_PROJECT_ROOTS",
                "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE")


def knowledge_config_path(repo_root: Path) -> Path:
    return shared_workspace_root(repo_root) / ".vaws-local/knowledge/service.json"


def shared_project_config(repo_root: Path) -> Path:
    """Update only the owned Markdown snapshot; retain user-selected mounts."""
    primary = shared_workspace_root(repo_root)
    path = primary / ".vaws-local/knowledge/service.json"
    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(payload, dict):
        raise ValueError("existing knowledge configuration must be a JSON object")
    snapshot = path.parent / "project"
    source = repo_root / PROJECT_ROOT_RELATIVE
    files = {file.relative_to(source): file for file in source.rglob("*") if file.is_file()}
    snapshot.mkdir(parents=True, exist_ok=True)
    for relative, file in files.items():
        target = snapshot / relative
        if not target.is_file() or file.read_bytes() != target.read_bytes():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
    for file in snapshot.rglob("*"):
        if file.is_file() and file.relative_to(snapshot) not in files:
            file.unlink()
    payload.setdefault("state_root", knowledge_owner_path(repo_root, path.parent / "instance"))
    layers = payload.setdefault("layers", {})
    project = layers.get("project")
    defaults = [str(primary / PROJECT_ROOT_RELATIVE), knowledge_owner_path(repo_root, primary / PROJECT_ROOT_RELATIVE)]
    if project is None or (isinstance(project, dict) and project.get("roots") in [[value] for value in defaults]):
        layers["project"] = {**(project or {}), "roots": [knowledge_owner_path(repo_root, snapshot)]}
    layers.setdefault("candidate", {"roots": [knowledge_owner_path(repo_root, path.parent / "candidate")]})
    payload.setdefault("shared_sync", {"enabled": True})
    payload.setdefault("publishing", {"enabled": False})
    if not path.is_file() or json.loads(path.read_text(encoding="utf-8")) != payload:
        write_json(path, payload)
    return path


def origin_repo_from_url(url: str) -> str:
    text = url.strip()
    text = re.sub(r"\.git$", "", text)
    if text.startswith("git@") and ":" in text:
        return text.split(":", 1)[1]
    parts = [part for part in re.split(r"[/:]", text) if part]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return text or "local/unpublished"


def origin_repo_from_git(repo_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return "local/unpublished"
    if proc.returncode != 0:
        return "local/unpublished"
    return origin_repo_from_url(proc.stdout.strip())


def knowledge_identity(repo_root: Path) -> dict[str, str]:
    return {
        "contributor": "anonymous",
        "origin_repo": origin_repo_from_git(repo_root),
    }


def knowledge_server_env(repo_root: Path) -> dict[str, str]:
    """Use the same roots from any client cwd or service-config directory."""

    repo_root = shared_workspace_root(repo_root)
    identity = knowledge_identity(repo_root)
    result = {"VAWS_KNOWLEDGE_ORIGIN_REPO": identity["origin_repo"]}
    config_path = repo_root / ".vaws-local/knowledge/service.json"
    if config_path.is_file():
        result["VAWS_KNOWLEDGE_CONFIG"] = str(config_path)
    else:
        result["VAWS_KNOWLEDGE_PROJECT_ROOTS"] = str(repo_root / PROJECT_ROOT_RELATIVE)
        result["VAWS_KNOWLEDGE_CANDIDATE_ROOT"] = str(repo_root / CANDIDATE_ROOT_RELATIVE)
        result["VAWS_KNOWLEDGE_STATE"] = str(repo_root / ".vaws-local/knowledge/instance")
    return result


def knowledge_owner_python(repo_root: Path) -> str:
    """A Windows-mounted workspace has one Windows knowledge database owner."""
    return managed_python(repo_root)


def knowledge_owner_path(repo_root: Path, value: object) -> str:
    return managed_path(value, windows=windows_mounted_workspace(repo_root))


def knowledge_owner_env(repo_root: Path) -> dict[str, str]:
    environment = knowledge_server_env(repo_root)
    environment["VAWS_ENV_RECEIPT"] = managed_receipt(repo_root)["receipt"]
    if windows_mounted_workspace(repo_root):
        for key in ("VAWS_KNOWLEDGE_CONFIG", "VAWS_KNOWLEDGE_PROJECT_ROOTS",
                    "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE"):
            if key in environment:
                environment[key] = knowledge_owner_path(repo_root, environment[key])
        if os.environ.get("WSLENV"):
            environment["WSLENV"] = os.environ["WSLENV"]
        environment = windows_interop_env(environment)
    return environment


def _run_knowledge(repo_root: Path, arguments: Sequence[str], *, receipt: dict | None = None,
                   prepare: bool = False) -> tuple[int, dict[str, Any]]:
    try:
        executable = accessible_windows_path(receipt["python"]) if receipt else knowledge_owner_python(repo_root)
    except (OSError, ValueError, RuntimeError) as exc:
        return 1, {"status": "pending", "ready": False, "reason": str(exc)}
    if not Path(executable).is_file():
        return 1, {"status": "pending", "ready": False,
                   "reason": "knowledge owner interpreter is not installed", "interpreter": executable}
    try:
        if prepare:
            shared_project_config(repo_root)
        environment = {key: value for key, value in os.environ.items() if key not in LOCATION_ENV}
        environment.update(knowledge_owner_env(repo_root))
        if receipt:
            environment["VAWS_ENV_RECEIPT"] = receipt["receipt"]
        command = [executable, *arguments]
        completed = subprocess.run(
            command, cwd=str(repo_root), env=environment,
            stdout=subprocess.PIPE, stderr=sys.stderr, text=True, encoding="utf-8", check=False,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("knowledge command returned no JSON object")
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return 1, {"status": "pending", "ready": False, "reason": str(exc)}
    return completed.returncode, payload


def run_knowledge_cli(repo_root: Path, arguments: Sequence[str]) -> tuple[int, dict[str, Any]]:
    """Run the installed owner without importing its runtime into the caller."""
    return _run_knowledge(repo_root, ["-m", "vaws_knowledge", *arguments])


PREPARE_CODE = """import json
from vaws_knowledge.server.layers import load_config
from vaws_knowledge.publishing import run_once
from vaws_knowledge.maintenance import maintain
config = load_config()
shared = run_once(config, force=True, verify=False)
result = maintain(config, force=True, verify=False)
result['config'] = str(config.config_path)
result['shared'] = shared
print(json.dumps(result, ensure_ascii=False))
"""


def prepare_knowledge(repo_root: Path, *, receipt: dict | None = None) -> dict[str, Any]:
    code, payload = _run_knowledge(repo_root, ["-c", PREPARE_CODE], receipt=receipt, prepare=True)
    if code != 0 or payload.get("ready") is not True:
        return {**payload, "status": "pending", "ready": False}
    return payload


def service_config(
    repo_root: Path,
    *,
    project_root: Path | None = None,
    candidate_root: Path | None = None,
) -> ServiceConfig:
    from vaws_knowledge.server.layers import load_config

    repo_root = shared_workspace_root(repo_root)
    config_path = repo_root / ".vaws-local/knowledge/service.json"
    mapping: dict[str, Any] = {}
    if not config_path.is_file():
        mapping = {
            "state_root": str(repo_root / ".vaws-local/knowledge/instance"),
            "layers": {
                "project": {"roots": [str(repo_root / PROJECT_ROOT_RELATIVE)]},
                "candidate": {"root": str(repo_root / CANDIDATE_ROOT_RELATIVE)},
            },
            "identity": knowledge_identity(repo_root),
        }
    if os.environ.get("VAWS_KNOWLEDGE_BACKEND"):
        mapping["backend"] = os.environ["VAWS_KNOWLEDGE_BACKEND"]
    if project_root is not None:
        mapping.setdefault("layers", {})["project"] = {"roots": [str(project_root)]}
    if candidate_root is not None:
        mapping.setdefault("layers", {})["candidate"] = {"root": str(candidate_root)}
    return load_config(
        mapping,
        env={},
        path=config_path if config_path.is_file() else None,
        base_dir=config_path.parent if config_path.is_file() else repo_root,
    )


def query_knowledge(*, knowledge_dir: Path, query: str, limit: int = 3) -> dict[str, Any]:
    """Keep index availability distinct from a successful query with zero results."""
    repo = knowledge_dir.parent.parent if knowledge_dir.parent.name == ".agents" else knowledge_dir.parent
    try:
        from vaws_knowledge.server.query import query as package_query
        override = None if knowledge_dir == repo / PROJECT_ROOT_RELATIVE else knowledge_dir
        result = package_query(service_config(repo, project_root=override), text=query, limit=limit)
        return result.to_dict()
    except Exception as exc:
        return {"results": [], "unavailable": True, "degraded": True, "index_detail": str(exc)}

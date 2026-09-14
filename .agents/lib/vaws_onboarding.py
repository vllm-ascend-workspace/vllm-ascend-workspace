"""Resumable first-use setup with explicit choices and no per-task network probe."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time

from vaws_community import POLICY_URL, community_environment, read_choice, write_choice, policy_path, disable_knowledge
from vaws_github import atomic_json, load_github_identity
from vaws_network import prepare as prepare_network

SCHEMA = "vaws.onboarding.v1"
REFERENCE = ".agents/bootstrap/repo-init/SKILL.md"
OPTIONAL_STAGES = ("star", "knowledge_runtime", "knowledge_reference", "knowledge", "reporting")


def setup_inputs(root: Path) -> str:
    """Invalidate explicit setup on dependency changes, without a network probe."""
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock"):
        path = root / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.exists() else b"missing")
    return digest.hexdigest()


def record_path(root: Path) -> Path:
    from vaws_local_state import shared_workspace_root
    return shared_workspace_root(root) / ".vaws-local/onboarding.json"


def read_record(root: Path, *, path: Path | None = None) -> dict | None:
    """Read current progress; an explicit path avoids owner discovery in hooks."""
    path = record_path(root) if path is None else path
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema") != SCHEMA
            or value.get("state") not in {"pending", "ready"}
            or not isinstance(value.get("steps"), dict)
            or any(not isinstance(step, dict) for step in value["steps"].values())
            or (value["state"] == "ready" and any(value["steps"].get(name, {}).get("state") != "ready"
                for name in ("fork", "dependencies", "clients")))):
        raise ValueError("Saved onboarding record is invalid; repair it without resetting existing work")
    choices = value.get("choices", {})
    if (not isinstance(choices, dict) or (value["state"] == "ready" and (
            not isinstance(choices.get("github_user"), str)
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", choices["github_user"])
            or any(type(choices.get(name)) is not bool for name in ("fork", "star"))
            or choices.get("community") not in {"enabled", "disabled"}))):
        raise ValueError("Saved onboarding choices are invalid; repair them without resetting existing work")
    return value


def read_identity(root: Path) -> dict | None:
    """Validate a local label without treating it as authentication."""
    from vaws_github import ForkPolicyError
    identity = load_github_identity(root)
    if identity and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", identity["login"]):
        raise ForkPolicyError("Saved GitHub identity has no valid personal login")
    user_id = (identity or {}).get("github_user_id")
    if user_id is not None and (type(user_id) is not int or user_id <= 0):
        raise ForkPolicyError("Saved GitHub identity has an invalid numeric user ID")
    return identity


def status(root: Path, *, detect_auth: bool = False) -> dict:
    record = read_record(root)
    identity = read_identity(root)
    choice = read_choice(root)
    state = record.get("state", "pending") if record else "needs_choices"
    identity_missing = bool(record and state == "ready" and (
        identity is None or identity["login"].casefold() != record["choices"]["github_user"].casefold()))
    if identity_missing:
        state = "pending"
    if record and choice is None:
        state = "needs_choices"
    result = {"state": state, "reference": REFERENCE, "policy_url": POLICY_URL,
              "github_user": (identity or {}).get("login"), "community": choice,
              "choices": (record or {}).get("choices", {}),
              "steps": (record or {}).get("steps", {}), "record": str(record_path(root)),
              "network_checked": False}
    if identity_missing:
        result.update(setup_state="repair_required", phase="identity")
    if record:
        result["choices"] = {**record.get("choices", {}), "community": (choice or {}).get("decision")}
    if detect_auth and state != "ready":
        from vaws_github import detect_github_auth
        result["authentication"] = detect_github_auth()
        result["network_checked"] = True
    return result


def run_json(command: list[str], root: Path, environment: dict) -> dict:
    completed = subprocess.run(command, cwd=root, env=environment, text=True, encoding="utf-8",
                               stdout=subprocess.PIPE, stderr=sys.stderr, check=False, timeout=900)
    try:
        value = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"Setup command returned no JSON object (exit {completed.returncode})") from exc
    if completed.returncode or not isinstance(value, dict):
        raise RuntimeError(f"Setup command failed (exit {completed.returncode}); inspect its local diagnostic log")
    return value


def configure_knowledge(root: Path, decision: str, login: str, *, receipt: dict | None = None) -> dict:
    if decision == "disabled":
        disable_knowledge(root)
        return {"state": "disabled", "local_reference": True, "owner_prepared": False}
    from vaws_knowledge_service import knowledge_config_path, knowledge_owner_path, run_knowledge_cli, _run_knowledge
    command = ["publishing", "configure", "--config", knowledge_owner_path(root, knowledge_config_path(root)),
               "--consent-file", knowledge_owner_path(root, policy_path(root)), "--github-user", login]
    config = knowledge_config_path(root)
    saved = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    repository = (saved.get("shared_sync") or {}).get("repository") or (saved.get("publishing") or {}).get("repository")
    if repository:
        command.extend(["--repository", repository])
    code, result = (_run_knowledge(root, ["-m", "vaws_knowledge", *command], receipt=receipt) if receipt else
                    run_knowledge_cli(root, command))
    if code:
        raise RuntimeError("Knowledge contribution configuration failed; the recorded choice is retained")
    return result


def configure_reporting(root: Path, receipt: dict, environment: dict) -> dict:
    """The installed diagnostics owner provides platform supervision and reuse."""
    base = (Path(environment.get("LOCALAPPDATA") or Path.home() / "AppData/Local") if os.name == "nt" else
            Path(environment.get("XDG_STATE_HOME") or Path.home() / ".local/state"))
    log_root = Path(environment.get("VAWS_DIAGNOSTICS_ROOT") or base / "vaws/diagnostics")
    state = log_root.parent / "diagnostics-worker"
    save_token = ["--save-token"] if environment.get("GH_TOKEN") or environment.get("GITHUB_TOKEN") else []
    return run_json([receipt["python"], "-m", "vaws_diagnostics.cli", "service", "ensure",
                     "--root", str(log_root), "--state", str(state), "--python", receipt["python"], *save_token],
                    root, environment)


def prepare_knowledge_runtime(root: Path, receipt: dict) -> dict:
    """Install local reference support during setup, independently of uploads."""
    from vaws_environment import capability_receipt
    from vaws_knowledge_service import shared_project_config
    from vaws_workspace_update import path_lock

    timings = {}
    owner = capability_receipt(receipt, "knowledge", prepare_missing=True, timings=timings)
    started = time.monotonic()
    # Do not overwrite a concurrent revocation with a stale config snapshot.
    # Installation above never holds this short local policy/config lock.
    with path_lock(policy_path(root).with_suffix(".lock"), wait_seconds=5):
        config = shared_project_config(root)
    timings["configuration_seconds"] = time.monotonic() - started
    return {"state": "ready", "owner": owner["key"], "config": str(config),
            "timings": timings, "local_reference": True}


REFERENCE_PREPARE_CODE = """import json
from dataclasses import replace
from vaws_knowledge.server.layers import load_config
from vaws_knowledge.maintenance import maintain
config = load_config()
# Setup prepares references, never drains an existing contribution queue.
# No config_path prevents the owner from reloading publishing=True from disk.
reference = replace(config, publishing={**config.publishing, 'enabled': False}, config_path=None)
print(json.dumps(maintain(reference, force=True), ensure_ascii=False))
"""


def prepare_knowledge_reference(root: Path, receipt: dict) -> dict:
    """Prepare the selected model, local services and reference index once."""
    from vaws_knowledge_service import _run_knowledge
    from vaws_diagnostics_adapter import redact

    code, result = _run_knowledge(root, ["-c", REFERENCE_PREPARE_CODE], receipt=receipt)
    if code or result.get("ready") is not True:
        raise RuntimeError("Knowledge reference preparation is pending: " + redact(json.dumps(result))[:2000])
    return result


def initialize(root: Path, *, github_user: str | None = None, fork: bool | None = None,
               star: bool | None = None, community: str | None = None, client: str | None = None,
               github=None, runner=run_json) -> dict:
    from vaws_github import ForkPolicyError, GitHubClient, setup, validate_github_user
    from vaws_local_state import shared_workspace_root
    from vaws_workspace_update import Deferred, path_lock
    from vaws_diagnostics_adapter import phase, redact, wrap_context

    root = shared_workspace_root(root.resolve())
    begun = time.monotonic()
    if community is not None:
        # Only an explicit answer writes authorization, before the long setup
        # lock. A running initializer must never replay its older answer.
        write_choice(root, community)
        if community == "disabled":
            disable_knowledge(root)
    with ExitStack() as locks:
        try:
            locks.enter_context(path_lock(root / ".vaws-local/onboarding.lock",
                                          wait_seconds=0 if community is not None else 180))
        except Deferred as exc:
            if community is None or exc.reason != "updater_running":
                raise
            return {"state": "choice_updated", "setup_state": "pending", "community": read_choice(root),
                    "record": str(record_path(root)), "seconds": time.monotonic()-begun,
                    "message": "Community choice is effective. Another initialization is running; its setup progress is retained."}
        current_choice = read_choice(root)

        def repair_required(name, exc):
            return {"state": "choice_updated" if community == "disabled" else "pending",
                    "setup_state": "repair_required", "phase": name, "community": current_choice,
                    "record": str(record_path(root)),
                    "error": {"type": type(exc).__name__, "message": redact(str(exc))},
                    "next": "Repair the reported local state and rerun vaws_init.py apply. Existing files were preserved."}

        try:
            previous = read_record(root)
        except (OSError, ValueError) as exc:
            return repair_required("progress", exc)
        client = client or (previous or {}).get("client") or "all"
        saved = (previous or {}).get("choices", {})
        try:
            identity = read_identity(root)
        except (OSError, ValueError, ForkPolicyError) as exc:
            return repair_required("identity", exc)
        choices = {"github_user": github_user or saved.get("github_user") or (identity or {}).get("login"),
                   "fork": fork if fork is not None else saved.get("fork"),
                   "star": star if star is not None else saved.get("star"),
                   "community": (current_choice or {}).get("decision")}
        if not choices["github_user"] or any(choices[key] is None for key in ("fork", "star", "community")):
            return {**status(root), "state": "choice_updated" if community == "disabled" else "needs_choices", "choices": choices,
                    "setup_state": "needs_choices",
                    "message": "Ask once for missing choices; reuse explicit answers. No fork, star or upload was attempted."}
        if type(choices["fork"]) is not bool or type(choices["star"]) is not bool:
            raise ValueError("fork and star choices must be boolean")
        if choices["community"] not in {"enabled", "disabled"}:
            raise ValueError("community must be enabled or disabled")
        if not isinstance(choices["github_user"], str) or not re.fullmatch(
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", choices["github_user"]):
            raise ValueError("GitHub ID must be a personal account login")
        if (identity and previous and choices["github_user"] == saved.get("github_user")
                and identity["login"].casefold() != choices["github_user"].casefold()):
            return repair_required("identity", ForkPolicyError("Saved identity differs from the confirmed workspace owner"))
        if (identity is None and previous
                and choices["github_user"] == saved.get("github_user")):
            # Recover only the explicitly saved local label. A completed setup
            # step is not fresh proof of a numeric identity or personal fork.
            identity = {"schema": "vaws.github.v1", "login": choices["github_user"], "forks": {}}
            atomic_json(root / ".vaws-local/github.json", identity)
        inputs = setup_inputs(root)
        if (previous and previous.get("state") == "ready" and choices == saved
                and previous.get("inputs") == inputs
                and current_choice and current_choice["decision"] == choices["community"]
                and previous.get("community_revision") == current_choice["revision"]
                and all(previous["steps"].get(name, {}).get("state") == "ready" for name in OPTIONAL_STAGES)
                and all(item.get("state") == "ready" for item in previous["steps"].values())
                and previous.get("client") == client):
            return {**previous, "record": str(record_path(root)), "seconds": time.monotonic()-begun,
                    "reused": True, "native_client_loaded": False}
        steps = dict((previous or {}).get("steps", {}))
        if previous and previous.get("inputs") != inputs:
            for name in ("dependencies", "clients", "knowledge_runtime", "knowledge_reference", "knowledge", "reporting"):
                steps.pop(name, None)
        if previous and current_choice and previous.get("community_revision") != current_choice["revision"]:
            for name in ("knowledge", "reporting"):
                steps.pop(name, None)
        if previous and previous.get("client") != client:
            steps.pop("clients", None)
        if previous and choices != saved:
            for field, affected in {"github_user": ("fork", "star", "knowledge"), "fork": ("fork",),
                                    "star": ("star",), "community": ("knowledge", "reporting")}.items():
                if choices[field] != saved.get(field):
                    for name in affected:
                        steps.pop(name, None)
        record = {"schema": SCHEMA, "state": "pending", "choices": choices, "steps": steps,
                  "policy_url": POLICY_URL, "client": client, "inputs": inputs}
        choice = current_choice
        record["community_revision"] = choice["revision"]
        environment = community_environment(root)
        environment["PYTHONUTF8"] = "1"
        environment.update(GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1")
        atomic_json(record_path(root), record)
        github = github or GitHubClient()

        def step(name, action):
            if steps.get(name, {}).get("state") == "ready":
                return steps[name]["result"]
            started = time.monotonic()
            record["phase"] = name
            atomic_json(record_path(root), record)
            print(f"VAWS initialization: {name}", file=sys.stderr, flush=True)
            with phase(f"initialization.{name}"):
                result = action()
            steps[name] = {"state": "ready", "seconds": time.monotonic()-started, "result": result}
            atomic_json(record_path(root), record)
            return result

        def identity_only():
            # A confirmed label is sufficient for local task ownership. It is
            # never an API credential; each external write authenticates itself.
            login = choices["github_user"]
            saved_identity = load_github_identity(root) or {}
            if saved_identity and saved_identity.get("login", "").casefold() != login.casefold():
                raise ValueError("Confirmed account differs from the saved workspace owner")
            identity = {**saved_identity, "schema": "vaws.github.v1", "login": login,
                        "forks": saved_identity.get("forks", {})}
            atomic_json(root / ".vaws-local/github.json", identity)
            return {"state": "configured", "fork": "declined", "github_user": login}

        try:
            # Only incomplete setup enters discovery; a ready workspace's early
            # return above remains entirely local and does not add a new gate.
            if any(steps.get(name, {}).get("state") != "ready" for name in ("fork", "dependencies")):
                step("network", lambda: prepare_network(root))
            environment = {**community_environment(root), "PYTHONUTF8": "1",
                           "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
            step("fork", lambda: setup(root, choices["github_user"], apply=True, roles=["workspace"], client=github)
                 if choices["fork"] else identity_only())
            dependency = step("dependencies", lambda: runner(
                [sys.executable, str(root / ".agents/scripts/vaws_deps.py"), "sync", "--locked"], root, environment))
            receipt = dependency["receipt"]
            step("clients", lambda: runner([receipt["python"], str(root / ".agents/scripts/vaws_client_setup.py"),
                                             "--client", client, "--project", str(root), "--apply"], root, environment))
        except Exception as exc:
            if getattr(exc, "status", None) in {401, 403}:
                from vaws_diagnostics_adapter import failure
                failure("caller", submission_state="not_submitted")
            record.update(state="pending", error={"type": type(exc).__name__, "message": redact(str(exc))})
            atomic_json(record_path(root), record)
            return {**record, "record": str(record_path(root)), "seconds": time.monotonic()-begun,
                    "next": "Fix the reported stage and rerun vaws_init.py apply; saved choices and completed steps are reused."}
        # Optional community capabilities never turn reference availability into
        # a task gate. Keep failed stages explicit and resumable independently.
        def configure_star():
            if not choices["star"]:
                return {"state": "declined"}
            validate_github_user(github.api("user"), choices["github_user"])
            return github.ensure_star("vllm-ascend-workspace/vllm-ascend-workspace")

        def refresh_choice():
            nonlocal choice
            latest = read_choice(root)
            if latest != choice:
                choice = latest
                choices["community"] = (latest or {}).get("decision")
                record["community_revision"] = (latest or {}).get("revision")
                for name in ("knowledge", "reporting"):
                    steps[name] = {"state": "pending", "reason": "community_choice_changed"}

        def configure_reference_contribution():
            if choice and choice["decision"] == "enabled" and steps.get("knowledge_runtime", {}).get("state") != "ready":
                raise RuntimeError("Local knowledge runtime is pending; retry repo-init to finish its preparation")
            return configure_knowledge(root, (choice or {}).get("decision", "disabled"), choices["github_user"], receipt=receipt)

        optional = {
            "star": configure_star,
            "knowledge": configure_reference_contribution,
            "reporting": lambda: configure_reporting(root, receipt, environment)
                         if choice and choice["decision"] == "enabled" else {"state": "disabled", "local_logs": True},
        }

        def optional_step(name, action):
            started = time.monotonic()
            try:
                refresh_choice()
                step(name, action)
            except Exception as exc:
                steps[name] = {"state": "pending", "seconds": time.monotonic()-started,
                               "error": {"type": type(exc).__name__, "message": redact(str(exc))}}
                atomic_json(record_path(root), record)

        # Knowledge packages and platform reporting setup have no dependency on
        # each other. Keep record writes on this thread, and contribution config
        # after its owner is ready. The pool is bounded by this explicit init.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = None
            if steps.get("knowledge_runtime", {}).get("state") != "ready":
                def install_reference_runtime():
                    started = time.monotonic()
                    try:
                        with phase("initialization.knowledge_runtime"):
                            result = prepare_knowledge_runtime(root, receipt)
                    except Exception as exc:
                        return {"state": "pending", "seconds": time.monotonic()-started,
                                "error": {"type": type(exc).__name__, "message": redact(str(exc))}}
                    return {"state": "ready", "result": result, "seconds": time.monotonic()-started}
                steps["knowledge_runtime"] = {"state": "running"}
                atomic_json(record_path(root), record)
                print("VAWS initialization: knowledge_runtime", file=sys.stderr, flush=True)
                future = pool.submit(wrap_context(install_reference_runtime))
            for name in ("star", "reporting"):
                optional_step(name, optional[name])
            if future is not None:
                record["phase"] = "knowledge_runtime"
                atomic_json(record_path(root), record)
                steps["knowledge_runtime"] = future.result()
                atomic_json(record_path(root), record)
            if steps.get("knowledge_runtime", {}).get("state") == "ready":
                optional_step("knowledge_reference", lambda: prepare_knowledge_reference(root, receipt))
            else:
                steps["knowledge_reference"] = {"state": "pending", "reason": "knowledge_runtime_pending"}
            optional_step("knowledge", optional["knowledge"])
        refresh_choice()
        record["pending_optional"] = [name for name in OPTIONAL_STAGES if steps[name]["state"] != "ready"]
        record["collaboration_state"] = "pending" if any(name in record["pending_optional"] for name in ("knowledge", "reporting")) else "configured"
        record.update(state="ready" if choice else "pending", phase="complete", completed_at=datetime.now(timezone.utc).isoformat())
        if choice is None:
            record.update(state="pending", setup_state="needs_choices", phase="community",
                          next="Community choice is missing. Confirm it explicitly; saved setup choices do not authorize contributions.")
        atomic_json(record_path(root), record)
        return {**record, "record": str(record_path(root)), "seconds": time.monotonic()-begun,
                "reused": False,
                "native_client_loaded": False}

"""Track the canonical default branch; existing processes keep their pins.

Git's fast-forward checks protect both branches. Preparation is reusable, and a
small receipt allows an interrupted checkout/push to be finished on the next run.
No stash, reset, rebase, force push, service restart or MCP rewrite is performed.
"""
from __future__ import annotations

from vaws_diagnostics_adapter import measured as _diagnostic_measured, wrap_context, phase, context_environment

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

CANONICAL = "vllm-ascend-workspace/vllm-ascend-workspace"
UPSTREAM = f"https://github.com/{CANONICAL}.git"


class Deferred(RuntimeError):
    def __init__(self, reason: str, detail: str = "", *, status: str = "deferred", evidence=None):
        super().__init__(detail or reason)
        self.reason, self.status = reason, status
        self.evidence = evidence


def redact(value: str) -> str:
    from vaws_diagnostics_adapter import redact as redact_text
    # Historical CLI errors also hid shortened GitHub token-shaped fixtures.
    value = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[redacted]", value)
    return redact_text(value)


def run(argv: list[str], *, cwd: Path, timeout: int = 120, env=None, check=True):
    try:
        environment = context_environment(os.environ if env is None else env)
        if argv[0] == "git":
            from vaws_process_wait import run_captured
            result = run_captured(argv, cwd=cwd, env=environment, stage="workspace_git", timeout=timeout, limit=None)
        else:
            result = subprocess.run(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        def output(value):
            return redact(value.decode("utf-8", "replace") if isinstance(value, bytes) else value or "")
        raise Deferred("command_timeout", f"{argv[0]} exceeded {timeout} seconds",
                       evidence={"argv": [redact(item) for item in argv], "cwd": str(cwd),
                                 "timeout": timeout, "stdout": output(exc.stdout), "stderr": output(exc.stderr)}) from exc
    if check and result.returncode:
        detail = redact(result.stderr or result.stdout).strip()
        raise Deferred("command_failed", detail[-1500:] or f"{argv[0]} exited {result.returncode}",
                       evidence={"argv": [redact(item) for item in argv], "cwd": str(cwd),
                                 "returncode": result.returncode, "stdout": redact(result.stdout),
                                 "stderr": redact(result.stderr)})
    return result


def git(root: Path, *args: str, check=True) -> str:
    environment = {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(root).resolve().parent)}
    action = args[0] if args else "unknown"
    if action in {"fetch", "clone", "push", "ls-remote"}:
        from vaws_network import environment_for
        environment = environment_for(root, environment)
    measured = action in {"fetch", "clone", "checkout", "reset", "push", "ls-remote"}
    with phase("source.git", action=action, level="INFO" if measured else "DEBUG"):
        return run(["git", *(["-c", "core.longpaths=true"] if os.name == "nt" else []), *args],
                   cwd=root, env=environment, check=check).stdout.strip()


def repository_root(path: Path) -> Path:
    """Find a checkout from a subdirectory without skipping a broken boundary."""
    path = Path(path).resolve(strict=True)
    if not path.is_dir():
        raise Deferred("repository_directory_required", str(path))
    for candidate in (path, *path.parents):
        if os.path.lexists(candidate / ".git"):
            # gitfiles and dangling .git links are boundaries too. A failed
            # discovery here must not select a different ancestor checkout.
            found = Path(git(candidate, "rev-parse", "--show-toplevel")).resolve()
            if found != candidate:
                raise Deferred("repository_root_changed", str(candidate))
            return found
    raise Deferred("repository_not_found", str(path))


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Deferred("invalid_update_state", path.name)
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def common_dir(root: Path) -> Path:
    value = Path(git(root, "rev-parse", "--git-common-dir"))
    return (root / value).resolve() if not value.is_absolute() else value.resolve()


@contextmanager
def update_lock(root: Path, *, wait_seconds: float = 0):
    """Serialize updates; new sessions can wait briefly for shared preparation."""
    from vaws_local_owner import windows_mounted_workspace
    if windows_mounted_workspace(root):
        raise Deferred("windows_owner_required", "run workspace_update.py with the native Windows owner")
    with path_lock(common_dir(root) / "vaws-update.lock", wait_seconds=wait_seconds):
        yield


@contextmanager
def path_lock(path: Path, *, wait_seconds: float = 180):
    """Serialize one operation by its explicit file without requiring Git."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                # Windows byte locks also block reads through another handle.
                # Inspect size without touching the byte the first updater owns.
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
            started = time.monotonic()
            announced = False
            with phase("source.lock_wait"):
                while not acquired:
                    try:
                        if os.name == "nt":
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError as exc:
                        if os.name != "nt" and not isinstance(exc, BlockingIOError):
                            raise
                        elapsed = time.monotonic() - started
                        if elapsed >= wait_seconds:
                            detail = (f"workspace preparation is still running after {elapsed:.1f}s; "
                                      f"lock: {path}") if wait_seconds else ""
                            raise Deferred("updater_running", detail,
                                           evidence={"lock": str(path), "waited_seconds": round(elapsed, 3)}) from exc
                        if not announced:
                            print("VAWS: waiting for another session's workspace preparation", file=sys.stderr, flush=True)
                            announced = True
                        time.sleep(min(0.2, wait_seconds - elapsed))
                    else:
                        acquired = True
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def github_repository(url: str) -> str | None:
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", url)
    return match.group(1) if match else None


def verified_git_url(root: Path, url: str, repository: str) -> None:
    """A resolved remote URL is rewritten again when passed directly to Git."""
    from vaws_github import ForkPolicyError, check_url_rewrites
    if (github_repository(url) or "").casefold() != repository.casefold():
        raise Deferred("git_target_changed")
    try:
        check_url_rewrites(root, url, repository)
    except ForkPolicyError as exc:
        raise Deferred("git_url_rewrite", str(exc)) from exc


@_diagnostic_measured('source.checkout_verify')
def clean_checkout(root: Path, *, branch: str | None, expected: set[str] | None = None) -> dict:
    directory = Path(git(root, "rev-parse", "--absolute-git-dir"))
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        if (directory / marker).exists():
            raise Deferred("git_operation_in_progress", marker)
    rows = git(root, "--no-optional-locks", "status", "--porcelain=v2", "--branch",
               "--untracked-files=all", "--ignore-submodules=all").splitlines()
    headers = dict(row[2:].split(" ", 1) for row in rows if row.startswith("# "))
    current_branch = headers.get("branch.head", "")
    if current_branch == "(detached)":
        current_branch = ""
    if current_branch != (branch or ""):
        raise Deferred("working_branch", f"automatic updates require {branch or 'a detached checkout'}")
    if any(not row.startswith("# ") for row in rows):
        raise Deferred("dirty_checkout")
    head = headers.get("branch.oid")
    if expected is not None and head not in expected:
        raise Deferred("source_local_commit")
    return {"head": head, "branch": current_branch, "git_dir": str(directory)}


def available_sources(root: Path) -> dict[str, str]:
    """Use the explicit selection or the two already populated business roots."""
    from vaws_workspace_entry import prepared_sources
    selected = prepared_sources(root)
    if selected is not None:
        return {name: path for name, path in selected.items() if name != "workspace"}
    result = {}
    for name in ("vllm", "vllm-ascend"):
        path = root / name
        if (path / ".git").exists():
            if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
                raise Deferred("invalid_source_root", str(path))
            result[name] = str(path.resolve())
    return result


def ancestor(root: Path, old: str, new: str) -> bool:
    result = run(["git", "merge-base", "--is-ancestor", old, new], cwd=root, check=False)
    if result.returncode not in (0, 1):
        raise Deferred("ancestry_unavailable")
    return result.returncode == 0


class WorkspaceUpdater:
    def __init__(self, root: Path, *, source_root: Path | None = None,
                 source_channel: str = "development", source_names: tuple[str, ...] | None = None, client=None):
        if source_channel not in {"development", "release"}:
            raise ValueError("source_channel must be development or release")
        self.root = root.resolve()
        self.source_root = (source_root or root).resolve()
        self.source_channel = source_channel
        if source_names is not None and set(source_names) - {"vllm", "vllm-ascend"}:
            raise ValueError("unsupported business source selection")
        self.source_names = source_names
        self.base = self.root / ".vaws-local/updates"
        self.client = client
        self.state = read_json(self.base / "state.json")

    def save(self, **fields):
        if fields.get("status") in ("preparing", "prepared", "ready", "current"):
            # Successful progress supersedes the last failure; raw logs remain.
            self.state.pop("reason", None)
            self.state.pop("error_log", None)
        self.state.update(fields)
        self.state["checked_at"] = datetime.now(timezone.utc).isoformat()
        write_json(self.base / "state.json", self.state)

    def failure(self, exc: Exception) -> dict:
        status = exc.status if isinstance(exc, Deferred) else "deferred"
        reason = exc.reason if isinstance(exc, Deferred) else "operation_pending"
        evidence = getattr(exc, "evidence", None) or {"error_type": type(exc).__name__, "error": redact(str(exc))}
        log = self.base / "logs" / f"{time.time_ns()}.json"
        try:
            write_json(log, evidence)
            self.save(status=status, reason=reason, error_log=str(log))
        except OSError as diagnostic_error:
            from vaws_diagnostics_adapter import report_failure
            report_failure("updater.evidence_write_failed", diagnostic_error, original_error_type=type(exc).__name__)
            log = None
        return {"status": status, "reason": reason, "detail": redact(str(exc))[-1500:],
                "log": str(log) if log else None, "phase": self.state.get("phase")}

    @_diagnostic_measured('source.upstream')
    def discover(self) -> dict:
        from vaws_github import (GitHubAPIError, GitHubClient, load_github_identity,
                                 validate_github_user, validate_personal_fork)
        identity = load_github_identity(self.root)
        if not identity:
            raise Deferred("github_identity_required", status="pending")
        client = self.client or GitHubClient()
        account = client.api("user")
        login = validate_github_user(account, identity["login"])
        if identity.get("github_user_id") is not None and identity["github_user_id"] != account.get("id"):
            raise Deferred("github_identity_changed", status="pending")
        canonical = client.api(f"repos/{CANONICAL}")
        if str(canonical.get("full_name", "")).casefold() != CANONICAL.casefold():
            raise Deferred("canonical_repository_changed")
        branch = canonical.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise Deferred("canonical_default_branch_unavailable")
        git(self.root, "check-ref-format", f"refs/heads/{branch}")
        fork_name = (identity.get("forks") or {}).get("workspace") or f"{login}/{CANONICAL.rsplit('/', 1)[1]}"
        try:
            fork = client.api(f"repos/{fork_name}")
        except GitHubAPIError as exc:
            if exc.status == 404:
                raise Deferred("personal_fork_required", status="pending") from exc
            raise
        validate_personal_fork(fork, login, CANONICAL, requested_full_name=fork_name)
        urls = git(self.root, "remote", "get-url", "--push", "--all", "origin").splitlines()
        fetch_urls = git(self.root, "remote", "get-url", "--all", "origin").splitlines()
        if len(urls) != 1 or len(fetch_urls) != 1 or any(
            (github_repository(url) or "").casefold() != fork_name.casefold() for url in urls + fetch_urls
        ):
            raise Deferred("personal_origin_required")
        # Use a private ref, not FETCH_HEAD (which an unrelated fetch can change).
        reference = "refs/vaws/upstream/" + hashlib.sha256(branch.encode()).hexdigest()
        verified_git_url(self.root, UPSTREAM, CANONICAL)
        git(self.root, "fetch", "--no-tags", UPSTREAM, f"refs/heads/{branch}:{reference}")
        sha = git(self.root, "rev-parse", f"{reference}^{{commit}}")
        verified_git_url(self.root, urls[0], fork_name)
        git(self.root, "fetch", "--no-tags", urls[0], f"refs/heads/{branch}:refs/vaws/fork-default")
        remote_sha = git(self.root, "rev-parse", "refs/vaws/fork-default")
        if not ancestor(self.root, remote_sha, sha):
            raise Deferred("fork_not_fast_forward")
        return {"target": sha, "branch": branch, "channel": "default_branch",
                "url": f"https://github.com/{CANONICAL}/commit/{sha}",
                "fork": fork_name, "push_url": urls[0]}

    def safe_inputs(self, release: dict) -> dict[str, str]:
        head = clean_checkout(self.root, branch=release["branch"])["head"]
        if not ancestor(self.root, head, release["target"]):
            raise Deferred("local_not_fast_forward")
        # Root gitlinks belong to the retired layout. Do not let an in-place
        # merge delete uninitialized source directories containing user files.
        if any(row.startswith("160000 ") for row in git(self.root, "ls-files", "--stage").splitlines()):
            raise Deferred("independent_workspace_required")
        return available_sources(self.source_root)

    def preparation_inputs(self, release: dict) -> dict[str, str]:
        if self.source_names is None:
            return available_sources(self.source_root)
        return {name: str(self.source_root / name) for name in self.source_names}

    @_diagnostic_measured('source.local_selection')
    def local_prepare(self) -> tuple[dict, dict]:
        """Choose already accepted preparation or one local commit; no upstream work."""
        started = time.monotonic()
        target = git(self.source_root, "rev-parse", "HEAD")
        active = self.preparation_inputs({})
        names = tuple(active)
        previous = self.state.get("prepared")
        rejected = None
        if (previous and self.state.get("target") == target
                and self.state.get("phase") in {"ready", "active", "local_updated"}
                and previous.get("source_channel") == self.source_channel
                and set(names) <= set(previous.get("sources", {}))):
            try:
                self.validate_prepared(self.state, previous, source_names=names)
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                rejected = redact(str(exc))
            else:
                selected = {**previous, "sources": {name: previous["sources"][name] for name in names},
                            "revisions": {name: previous["revisions"][name] for name in ("workspace", *names)}}
                return {"status": "cached", "target": selected["revisions"]["workspace"],
                        "upstream_checked": False, "selection_seconds": time.monotonic()-started}, selected
        release = {"target": target, "branch": None, "channel": "local"}
        attempt = self.preparation_attempt(release, active)
        if rejected is not None and self.stage_path(release, active).exists():
            # A damaged cache is optional. Retain its files and references;
            # prepare the local immutable commit in another private directory.
            locations = dict(self.state.get("stage_ids", {}))
            locations[self.stage_key(release, active)] = uuid.uuid4().hex
            attempt["stage_ids"] = locations
        self.save(**release, phase="preparing", status="preparing", source_channel=self.source_channel,
                  selected_sources=sorted(active), **attempt)
        try:
            prepared = self.prepare(release, active)
        except (OSError, ValueError, RuntimeError) as exc:
            self.failure(exc)
            raise
        self.save(**release, prepared=prepared, phase="ready", status="ready")
        return {"status": "local", "target": release["target"], "upstream_checked": False,
                "selection_seconds": time.monotonic()-started,
                **({"cache_rejected": rejected} if rejected is not None else {})}, prepared

    def stage_key(self, release: dict, active: dict) -> str:
        selection = hashlib.sha256("\0".join(sorted(active)).encode()).hexdigest()[:12]
        return f"{release['target']}-{self.source_channel}-{selection}"

    def stage_path(self, release: dict, active: dict) -> Path:
        key = self.stage_key(release, active)
        locations = self.state.get("stage_ids", {})
        if not isinstance(locations, dict):
            raise Deferred("invalid_update_state", "invalid private stage cache")
        stage_id = locations.get(key, "")
        if not isinstance(stage_id, str) or (stage_id and not re.fullmatch(r"[0-9a-f]{32}", stage_id)):
            raise Deferred("invalid_update_state", "invalid private stage id")
        # Keep retry paths equally short: Windows Git repository discovery
        # has a stricter path limit than tracked-file checkout.
        directory = hashlib.sha256((key + "\0" + stage_id).encode()).hexdigest()[:32]
        return self.base / "releases" / directory

    def same_selection(self, release: dict, active: dict) -> bool:
        return (self.state.get("target") == release["target"]
                and self.state.get("source_channel") == self.source_channel
                and self.state.get("selected_sources") == sorted(active))

    def preparation_attempt(self, release: dict, active: dict) -> dict:
        """Keep a failed private stage intact and choose another on a retry."""
        same = self.same_selection(release, active)
        if (same and self.state.get("phase") == "preparing" and self.state.get("status") == "deferred"
                and self.state.get("error_log")):
            previous = self.stage_path(release, active)
            referenced = (self.state.get("prepared") or {}).get("stage")
            if referenced and Path(referenced).resolve() == previous.resolve():
                raise Deferred("prepared_stage_referenced", "retaining the previously published staging checkout")
            if previous.exists():
                # No move, reset or deletion: even edits made while diagnosing
                # this failed preparation remain at their original path.
                # Remember recovered cache locations by code/channel/source
                # selection, so alternating root-only and full preparations
                # cannot return to an old incomplete directory.
                locations = dict(self.state.get("stage_ids", {}))
                locations[self.stage_key(release, active)] = uuid.uuid4().hex
                return {"stage_ids": locations, "retained_failed_stage": str(previous)}
        return {}

    @_diagnostic_measured('source.cache_verify')
    def validate_prepared(self, release: dict, prepared: dict, *, source_names=None) -> Path:
        stage = Path(prepared["stage"])
        sources = prepared["sources"]
        revisions = prepared["revisions"]
        if set(revisions) != {"workspace", *sources} or revisions["workspace"] != release["target"]:
            raise Deferred("prepared_revisions_changed")
        if prepared.get("source_channel") != self.source_channel or stage.resolve() != self.stage_path(release, sources).resolve():
            raise Deferred("prepared_checkout_changed")
        if not (stage / ".git").is_dir() or (stage / ".git/objects/info/alternates").exists():
            raise Deferred("prepared_checkout_not_independent")
        clean_checkout(stage, branch=None, expected={release["target"]})
        if sources:
            from vaws_source_lock import selected_sources
            locked = selected_sources(stage, self.source_channel)
            for name, value in sources.items():
                if source_names is not None and name not in source_names:
                    continue
                path = Path(value)
                if name not in locked or path.resolve() != (stage / name).resolve():
                    raise Deferred("prepared_source_changed", name)
                if revisions[name] != locked[name]["revision"]:
                    raise Deferred("prepared_revisions_changed", name)
                if not (path / ".git").is_dir() or (path / ".git/objects/info/alternates").exists():
                    raise Deferred("prepared_source_not_independent", name)
                clean_checkout(path, branch=None, expected={locked[name]["revision"]})
        if not Path(prepared["receipt"]["receipt"]).is_file():
            raise Deferred("prepared_environment_unavailable")
        return stage

    @_diagnostic_measured('source.prepare')
    def prepare(self, release: dict, active: dict[str, str]) -> dict:
        from vaws_native_workspace import prepare_source
        target = release["target"]
        stage = self.stage_path(release, active)
        if not stage.exists():
            donor = self.source_root if release.get("channel") == "local" else self.root
            prepare_source(stage, repository=CANONICAL, revision=target, local_source=donor)
        if not (stage / ".git").is_dir() or (stage / ".git/objects/info/alternates").exists():
            raise Deferred("prepared_checkout_not_independent")
        clean_checkout(stage, branch=None, expected={target})
        sources = {}
        revisions = {"workspace": target}
        if active:
            from vaws_source_lock import selected_sources
            locked = selected_sources(stage, self.source_channel)
            def prepare_child(item):
                name, source = item
                if name not in locked:
                    raise Deferred("invalid_source_name", name)
                destination = stage / name
                spec = locked[name]
                if not destination.exists():
                    local = Path(source)
                    prepare_source(destination, **spec, local_source=local if (local / ".git").exists() else None)
                else:
                    # Only a fully completed owned checkout is reusable. An
                    # interrupted clone never becomes ready by file existence.
                    if not (destination / ".git").is_dir() or (destination / ".git/objects/info/alternates").exists():
                        raise Deferred("prepared_source_incomplete", name)
                    clean_checkout(destination, branch=None, expected={spec["revision"]})
                return name, str(destination), spec["revision"]
            with ThreadPoolExecutor(max_workers=min(2, len(active))) as pool:
                for name, destination, revision in pool.map(wrap_context(prepare_child), active.items()):
                    sources[name] = destination
                    revisions[name] = revision
        environment = dict(os.environ)
        for name in ("VAWS_ENV_RECEIPT", "VAWS_MANAGED_ENV_RECEIPT", "VAWS_TOP_FROM", "VIRTUAL_ENV",
                     "VAWS_SKIP_VENV_REEXEC", "VAWS_VENV_REEXEC", "PYTHONPATH"):
            environment.pop(name, None)
        from vaws_environment import EnvironmentError, _lookup
        try:
            # Preparation resolves this revision's inputs, independently of a
            # parent task pin. Lookup is read-only and supports bundle receipts.
            receipt = _lookup(stage, sys.platform)
        except EnvironmentError:
            interpreter = getattr(sys, "_base_executable", sys.executable)
            print(f"preparing locked packages at {target[:12]}", file=sys.stderr, flush=True)
            installed = run([interpreter, str(stage / ".agents/scripts/vaws_deps.py"),
                             "sync", "--locked"], cwd=stage, timeout=1800, env=environment)
            if installed.stderr:
                print(installed.stderr, file=sys.stderr, end="", flush=True)
            payload = json.loads(installed.stdout)
            if not payload.get("ok") or not payload.get("receipt"):
                raise Deferred("dependencies_pending")
            receipt = payload["receipt"]
        result = {"receipt": receipt, "stage": str(stage), "sources": sources, "revisions": revisions,
                  "source_channel": self.source_channel}
        self.validate_prepared(release, result)
        return result

    @_diagnostic_measured('environment.activate')
    def activate_environment(self, prepared: dict) -> None:
        # Use the prepared revision's implementation, and publish only the next
        # client selection. Running native clients retain their explicit pin.
        stage = Path(prepared["stage"])
        code = ("import sys;sys.path.insert(0,sys.argv[1]);"
                "from pathlib import Path;from vaws_environment import read_receipt,select_environment;"
                "select_environment(Path(sys.argv[2]),read_receipt(sys.argv[3]))")
        environment = dict(os.environ)
        for name in ("VAWS_ENV_RECEIPT", "VAWS_MANAGED_ENV_RECEIPT", "PYTHONPATH"):
            environment.pop(name, None)
        run([getattr(sys, "_base_executable", sys.executable), "-c", code,
             str(stage / ".agents/lib"), str(self.root), prepared["receipt"]["receipt"]],
            cwd=self.root, env=environment)

    @_diagnostic_measured('source.activate')
    def activate(self) -> dict:
        if self.state.get("phase") not in ("ready", "local_updated"):
            return {"status": "pending", "reason": "no_prepared_update"}
        release = self.state
        self.safe_inputs(release)
        prepared = self.state["prepared"]
        self.validate_prepared(release, prepared)
        git(self.root, "merge", "--ff-only", release["target"])
        self.save(phase="local_updated")
        # Existing business roots keep their branches and working edits. The
        # locked inputs live in the separately validated prepared bundle.
        self.safe_inputs(release)
        self.activate_environment(prepared)
        self.save(phase="active", active=release["target"], status="current")
        return {"status": "applied", "branch": release["branch"], "target": release["target"]}

    def step(self, *, apply: bool = False, activate: bool = True) -> dict:
        try:
            release = self.discover()
            public = {key: value for key, value in release.items() if key != "push_url"}
            if not apply:
                status = "current" if self.state.get("active") == release["target"] else "available"
                result = {"status": status, **public}
                try:
                    self.safe_inputs(release)
                except Deferred as exc:
                    result["local_apply_deferred"] = exc.reason
                return result
            # Canonical preparation does not gate on the editing branch.
            active = self.safe_inputs(release) if activate else self.preparation_inputs(release)
            if activate and self.state.get("active") == release["target"] and git(self.root, "rev-parse", "HEAD") == release["target"]:
                self.save(status="current")
                return {"status": "current", **public}
            local_ready = self.state.get("channel") == "local" and self.state.get("phase") == "ready"
            if self.same_selection(release, active) and self.state.get("phase") in (
                "ready", "local_updated", "active") and not local_ready:
                self.validate_prepared(release, self.state["prepared"])
                self.save(status="ready", phase="ready")
                return self.activate() if activate else {"status": "ready", **public}
            if self.same_selection(release, active) and (self.state.get("phase") == "prepared" or local_ready):
                self.validate_prepared(release, self.state["prepared"])
                # A local stage is ready to copy, but did not update a fork.
                # Explicit maintenance reuses its work and completes that one
                # remaining operation with the freshly discovered branch facts.
                self.save(**public, phase="prepared", status="prepared")
            else:
                attempt = self.preparation_attempt(release, active)
                self.save(**public, phase="preparing", status="preparing", selected_sources=sorted(active),
                          source_channel=self.source_channel, **attempt)
                prepared = self.prepare(release, active)
                self.save(phase="prepared", status="prepared", prepared=prepared)
                # Dependencies may take minutes; explicit activation must check
                # again, while preparation alone never edits this tree.
                if activate:
                    self.safe_inputs(release)
            # Explicit target prevents branch.<name>.pushRemote/pushDefault from
            # redirecting the operation. There is deliberately no force option.
            verified_git_url(self.root, release["push_url"], release["fork"])
            git(self.root, "push", "--porcelain", release["push_url"],
                f"{release['target']}:refs/heads/{release['branch']}")
            self.save(phase="ready", status="ready")
            return self.activate() if activate else {"status": "ready", **public}
        except Deferred as exc:
            if exc.status == "pending":
                self.save(status=exc.status, reason=exc.reason)
                return {"status": exc.status, "reason": exc.reason}
            return self.failure(exc)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            # Missing gh/auth/network are recoverable next time; no busy loop.
            return self.failure(exc)

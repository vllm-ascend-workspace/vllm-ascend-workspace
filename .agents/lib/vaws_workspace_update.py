"""Track the canonical default branch; existing processes keep their pins.

Git's fast-forward checks protect both branches. Preparation is reusable, and a
small receipt allows an interrupted checkout/push to be finished on the next run.
No stash, reset, rebase, force push, service restart or MCP rewrite is performed.
"""
from __future__ import annotations

from contextlib import contextmanager
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

CANONICAL = "vllm-ascend-workspace/vllm-ascend-workspace"
UPSTREAM = f"https://github.com/{CANONICAL}.git"
SUBMODULES = {"vllm": "vllm-project/vllm", "vllm-ascend": "vllm-project/vllm-ascend"}


class Deferred(RuntimeError):
    def __init__(self, reason: str, detail: str = "", *, status: str = "deferred", evidence=None):
        super().__init__(detail or reason)
        self.reason, self.status = reason, status
        self.evidence = evidence


def redact(value: str) -> str:
    value = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", value)
    return re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)", "[redacted]", value)


def run(argv: list[str], *, cwd: Path, timeout: int = 120, env=None, check=True):
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
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
    return run(["git", *args], cwd=root, check=check).stdout.strip()


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
    path = common_dir(root) / "vaws-update.lock"
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


def clean_checkout(root: Path, *, branch: str | None, expected: set[str] | None = None) -> None:
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        location = Path(git(root, "rev-parse", "--git-path", marker))
        if not location.is_absolute():
            location = root / location
        if location.exists():
            raise Deferred("git_operation_in_progress", marker)
    current_branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current_branch != (branch or ""):
        raise Deferred("working_branch", f"automatic updates require {branch or 'a detached submodule checkout'}")
    if git(root, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"):
        raise Deferred("dirty_checkout")
    if expected is not None and git(root, "rev-parse", "HEAD") not in expected:
        raise Deferred("submodule_local_commit")


def gitlinks(root: Path, revision: str) -> dict[str, str]:
    data = run(["git", "ls-tree", "-rz", revision], cwd=root).stdout
    links = {}
    for entry in data.split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            mode, _, sha = metadata.split(" ")
            if mode == "160000":
                links[path] = sha
    return links


def initialized(root: Path, path: str) -> bool:
    return (root / path / ".git").exists()


def ancestor(root: Path, old: str, new: str) -> bool:
    result = run(["git", "merge-base", "--is-ancestor", old, new], cwd=root, check=False)
    if result.returncode not in (0, 1):
        raise Deferred("ancestry_unavailable")
    return result.returncode == 0


class WorkspaceUpdater:
    def __init__(self, root: Path, *, client=None):
        self.root = root.resolve()
        self.base = self.root / ".vaws-local/updates"
        self.client = client
        self.state = read_json(self.base / "state.json")

    def save(self, **fields):
        if fields.get("status") in ("preparing", "prepared", "ready", "current"):
            # Successful progress supersedes the last failure; raw logs remain.
            self.state.pop("reason", None)
            self.state.pop("error_log", None)
        if "target" in fields:
            # Older receipts described Releases; the target is now a branch SHA.
            self.state.pop("tag", None)
            self.state.pop("release_id", None)
        self.state.update(fields)
        self.state["checked_at"] = datetime.now(timezone.utc).isoformat()
        write_json(self.base / "state.json", self.state)

    def failure(self, exc: Exception) -> dict:
        status = exc.status if isinstance(exc, Deferred) else "deferred"
        reason = exc.reason if isinstance(exc, Deferred) else "operation_pending"
        evidence = getattr(exc, "evidence", None) or {"error_type": type(exc).__name__, "error": redact(str(exc))}
        log = self.base / "logs" / f"{time.time_ns()}.json"
        write_json(log, evidence)
        self.save(status=status, reason=reason, error_log=str(log))
        return {"status": status, "reason": reason, "detail": redact(str(exc))[-1500:],
                "log": str(log), "phase": self.state.get("phase")}

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
        clean_checkout(self.root, branch=release["branch"])
        head = git(self.root, "rev-parse", "HEAD")
        if not ancestor(self.root, head, release["target"]):
            raise Deferred("local_not_fast_forward")
        old_links = gitlinks(self.root, "HEAD")
        target_links = gitlinks(self.root, release["target"])
        resuming = self.state.get("target") == release["target"] and self.state.get("phase") in (
            "ready", "local_updated", "submodules_updated")
        original = self.state.get("original_submodules", {}) if resuming else {}
        active = {path: sha for path, sha in old_links.items() if initialized(self.root, path)}
        for path, sha in active.items():
            if path not in SUBMODULES or path not in target_links:
                raise Deferred("submodule_layout_changed", path)
            accepted = {sha}
            if resuming and head == release["target"] and path in original:
                accepted.add(original[path])
            clean_checkout(self.root / path, branch=None, expected=accepted)
            # Nested modules are outside this workspace's managed source contract.
            if any(initialized(self.root / path, child) for child in gitlinks(self.root / path, "HEAD")):
                raise Deferred("nested_submodule_initialized", path)
        return active

    def preparation_inputs(self, release: dict) -> dict[str, str]:
        """Preparation copies committed objects without requiring an idle editing tree."""
        target_links = gitlinks(self.root, release["target"])
        return {path: sha for path, sha in gitlinks(self.root, "HEAD").items()
                if path in SUBMODULES and path in target_links and initialized(self.root, path)}

    def validate_prepared(self, release: dict, prepared: dict) -> Path:
        stage = Path(prepared["stage"])
        if stage.resolve() != (self.base / "releases" / release["target"]).resolve():
            raise Deferred("prepared_checkout_changed")
        if git(stage, "rev-parse", "HEAD") != release["target"]:
            raise Deferred("prepared_checkout_changed")
        clean_checkout(stage, branch=None)
        if not Path(prepared["receipt"]["receipt"]).is_file():
            raise Deferred("prepared_environment_unavailable")
        return stage

    def prepare(self, release: dict, active: dict[str, str]) -> dict:
        target = release["target"]
        stage = self.base / "releases" / target
        if not stage.exists():
            stage.parent.mkdir(parents=True, exist_ok=True)
            git(self.root, "worktree", "add", "--detach", str(stage), target)
        if git(stage, "rev-parse", "HEAD") != target:
            raise Deferred("prepared_checkout_changed")
        clean_checkout(stage, branch=None)
        environment = dict(os.environ)
        for name in ("VAWS_ENV_RECEIPT", "VAWS_MANAGED_ENV_RECEIPT", "VAWS_TOP_FROM", "VIRTUAL_ENV",
                     "VAWS_SKIP_VENV_REEXEC", "VAWS_VENV_REEXEC", "PYTHONPATH"):
            environment.pop(name, None)
        interpreter = getattr(sys, "_base_executable", sys.executable)
        print(f"preparing locked packages for {release['branch']} at {target[:12]}", file=sys.stderr, flush=True)
        command = [interpreter, str(stage / ".agents/scripts/vaws_deps.py"), "sync", "--locked"]
        prepared = run(command, cwd=stage, timeout=1800, env=environment)
        if prepared.stderr:
            print(prepared.stderr, file=sys.stderr, end="", flush=True)
        payload = json.loads(prepared.stdout)
        if not payload.get("ok") or not payload.get("receipt"):
            raise Deferred("dependencies_pending")
        environment["VAWS_ENV_RECEIPT"] = payload["receipt"]["receipt"]
        print("caching the pinned monitor package; existing services keep running", file=sys.stderr, flush=True)
        monitor = run([interpreter, str(stage / ".agents/skills/npu-fleet-monitor/scripts/manage_monitor.py"), "deploy"],
                      cwd=stage, timeout=600, env=environment)
        if monitor.stderr:
            print(monitor.stderr, file=sys.stderr, end="", flush=True)
        if not json.loads(monitor.stdout).get("ok"):
            raise Deferred("monitor_package_pending")
        target_links = gitlinks(self.root, target)
        for path in active:
            # Never follow a submodule branch; cache precisely the workspace gitlink.
            url = f"https://github.com/{SUBMODULES[path]}.git"
            verified_git_url(self.root / path, url, SUBMODULES[path])
            git(self.root / path, "fetch", "--no-tags", url, target_links[path])
            self.prepare_submodule(stage, path, target_links[path])
        return {"receipt": payload["receipt"], "knowledge": payload.get("knowledge", {}), "stage": str(stage)}

    def prepare_submodule(self, stage: Path, path: str, target: str) -> None:
        """Give new editing copies the existing initialized modules, from local objects."""
        source, destination = self.root / path, stage / path
        if not initialized(stage, path):
            git(self.root, "clone", "--local", "--no-checkout", "--", str(source), str(destination))
        index = Path(git(destination, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = destination / index
        # A no-checkout clone interrupted before its first checkout has no index
        # and no files outside .git. Only that exact unfinished state is resumed.
        unfinished = not index.exists() and all(item.name == ".git" for item in destination.iterdir())
        if not unfinished:
            clean_checkout(destination, branch=None, expected={target})
        git(destination, "checkout", "--detach", target)
        for remote in git(destination, "remote").splitlines():
            git(destination, "remote", "remove", remote)
        # Preserve all remote settings, including multiple URLs and refspecs;
        # never leave the local staging/source path as the development origin.
        values = run(["git", "config", "--null", "--get-regexp", r"^remote\."], cwd=source, check=False)
        if values.returncode not in (0, 1):
            raise Deferred("submodule_remotes_unavailable", path)
        for entry in values.stdout.split("\0"):
            if entry:
                key, value = entry.split("\n", 1)
                git(destination, "config", "--local", "--add", key, value)
        clean_checkout(destination, branch=None, expected={target})

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

    def activate(self) -> dict:
        if self.state.get("phase") not in ("ready", "local_updated", "submodules_updated"):
            return {"status": "pending", "reason": "no_prepared_update"}
        release = self.state
        active = self.safe_inputs(release)
        prepared = self.state["prepared"]
        self.validate_prepared(release, prepared)
        target_links = gitlinks(self.root, release["target"])
        for path in active:
            git(self.root / path, "cat-file", "-e", f"{target_links[path]}^{{commit}}")
        if git(self.root, "rev-parse", "HEAD") != release["target"]:
            # Preparation can have happened on another business branch. Record
            # only the clean sources just proven safe for this activation.
            self.save(original_submodules=active)
        git(self.root, "merge", "--ff-only", release["target"])
        self.save(phase="local_updated")
        for path in active:
            # No network on client startup; prepare has already cached objects.
            git(self.root / path, "checkout", "--detach", target_links[path])
        self.save(phase="submodules_updated")
        self.safe_inputs(release)
        self.activate_environment(prepared)
        self.save(phase="active", active=release["target"], status="current")
        return {"status": "applied", "branch": release["branch"], "target": release["target"],
                "knowledge": prepared.get("knowledge", {})}

    def step(self, *, apply: bool = False, activate: bool = True, for_session: bool = False) -> dict:
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
            # A new session must be able to adopt this revision before spending
            # time preparing it. Explicit preparation can still cache updates
            # while the editing source is dirty or on a business branch.
            active = self.safe_inputs(release) if activate or for_session else self.preparation_inputs(release)
            if self.state.get("active") == release["target"] and git(self.root, "rev-parse", "HEAD") == release["target"]:
                self.save(status="current")
                return {"status": "current", **public}
            if self.state.get("target") == release["target"] and self.state.get("phase") in (
                "ready", "local_updated", "submodules_updated"):
                self.save(status="ready")
                return self.activate() if activate else {"status": "ready", **public}
            if self.state.get("target") == release["target"] and self.state.get("phase") == "prepared":
                self.validate_prepared(release, self.state["prepared"])
            else:
                self.save(**public, phase="preparing", status="preparing", original_submodules=active)
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


def activate_prepared(root: Path) -> dict:
    """Bounded offline activation before a new native client starts."""
    updater = None
    try:
        with update_lock(root):
            updater = WorkspaceUpdater(root)
            return updater.activate()
    except Deferred as exc:
        return updater.failure(exc) if updater else {"status": exc.status, "reason": exc.reason}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        return updater.failure(exc) if updater else {"status": "deferred", "reason": "activation_pending", "error_type": type(exc).__name__}


def prepared_source(root: Path) -> Path | None:
    """Read a prepared revision for a new editing copy; never modify this root.

    A personal branch, local commit or unfinished edit continues from its own
    source instead of silently being replaced by the upstream tree.
    """
    try:
        updater = WorkspaceUpdater(root)
        state = updater.state
        if state.get("phase") not in ("ready", "active"):
            return None
        active = updater.safe_inputs(state)
        sha = state["target"]
        stage = Path(state["prepared"]["stage"])
        expected = updater.base / "releases" / sha
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha) or stage.resolve() != expected.resolve():
            return None
        if git(stage, "rev-parse", "HEAD") != sha:
            return None
        clean_checkout(stage, branch=None)
        for path, target in gitlinks(root, sha).items():
            if initialized(stage, path) != (path in active):
                return None
            if path in active:
                clean_checkout(stage / path, branch=None, expected={target})
        return stage
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError):
        return None

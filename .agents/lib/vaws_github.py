#!/usr/bin/env python3
"""Plan or establish verified personal GitHub forks using only the standard library.

No branch is switched, pushed or reset. Missing submodules are initialized at
the recorded gitlink; initialized business checkouts retain their current HEAD.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
REPOSITORIES = {
    "workspace": {"path": ".", "upstream": "vllm-ascend-workspace/vllm-ascend-workspace"},
    "vllm": {"path": "vllm", "upstream": "vllm-project/vllm"},
    "vllm-ascend": {"path": "vllm-ascend", "upstream": "vllm-project/vllm-ascend"},
}
IDENTITY_SCHEMA = "vaws.github.v1"


class ForkPolicyError(RuntimeError):
    """A requested operation does not satisfy the personal-fork policy."""


class GitHubAPIError(ForkPolicyError):
    def __init__(self, message: str, status: int | None = None, *, evidence: dict | None = None):
        super().__init__(message)
        self.status = status
        self.evidence = evidence


class GitHubClient:
    def api(self, endpoint: str, method: str = "GET", fields: dict | None = None) -> dict:
        from vaws_workspace_update import redact

        command = ["gh", "api", "--hostname", "github.com", endpoint, "--method", method]
        if fields is not None:
            command += ["--input", "-"]
        # Native terminals can force gh to colorize even captured JSON. Select
        # machine output for this subprocess without changing the user's shell.
        environment = os.environ.copy()
        environment.update(CLICOLOR_FORCE="0", FORCE_COLOR="0", NO_COLOR="1")
        environment.pop("GH_FORCE_TTY", None)

        def evidence(result=None, **extra):
            def output(value):
                text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""
                text = redact(text)
                return re.sub(r"(?im)^([ \t]*[<>*]?[ \t]*(?:authorization|proxy-authorization):)[^\r\n]*",
                              r"\1 [redacted]", text)
            return {"command": [redact(item) for item in command], "endpoint": redact(endpoint),
                    "method": method, "returncode": getattr(result, "returncode", None),
                    "stdout": output(getattr(result, "stdout", None)),
                    "stderr": output(getattr(result, "stderr", None)), **extra}

        try:
            result = subprocess.run(command, input=json.dumps(fields) if fields is not None else None,
                                    capture_output=True, text=True, encoding="utf-8", timeout=60,
                                    env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubAPIError(f"GitHub request could not complete: {redact(str(exc))}",
                                 evidence=evidence(exc, error_type=type(exc).__name__)) from exc
        if result.returncode:
            status = re.search(r"HTTP (\d{3})", result.stderr)
            facts = evidence(result)
            raise GitHubAPIError(facts["stderr"].strip() or "GitHub request failed",
                                 int(status[1]) if status else None, evidence=facts)
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError("GitHub returned invalid JSON", evidence=evidence(result)) from exc
        if not isinstance(value, dict):
            raise GitHubAPIError("GitHub returned an unexpected response", evidence=evidence(result))
        return value


def validate_github_user(payload: dict, requested_login: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", requested_login):
        raise ForkPolicyError("GitHub ID must be a personal account login")
    login = payload.get("login")
    if payload.get("type") != "User" or not isinstance(login, str):
        raise ForkPolicyError("GitHub authentication must belong to a personal User account")
    user_id = payload.get("id")
    if type(user_id) is not int or user_id <= 0:
        raise ForkPolicyError("GitHub did not return a valid numeric personal user ID")
    if login.casefold() != requested_login.casefold():
        raise ForkPolicyError(f"Authenticated GitHub account {login!r} differs from the confirmed ID")
    return login


def validate_personal_fork(payload: dict, github_user: str, upstream_full_name: str,
                           requested_full_name: str | None = None) -> dict:
    expected = requested_full_name or f"{github_user}/{upstream_full_name.split('/')[-1]}"
    if expected.split("/", 1)[0].casefold() != github_user.casefold():
        raise ForkPolicyError("Requested fork owner differs from the confirmed personal ID")
    full_name = payload.get("full_name")
    if not isinstance(full_name, str) or full_name.casefold() != expected.casefold():
        raise ForkPolicyError(f"Repository {expected} redirects or resolves to another identity")
    owner = payload.get("owner") or {}
    if owner.get("type") != "User" or str(owner.get("login", "")).casefold() != github_user.casefold():
        raise ForkPolicyError(f"Repository {expected} is not owned by the confirmed personal User")
    if payload.get("fork") is not True:
        raise ForkPolicyError(f"Repository {expected} exists but is not a GitHub fork")
    network = {str((payload.get(key) or {}).get("full_name") or "").casefold() for key in ("parent", "source")}
    if upstream_full_name.casefold() not in network:
        raise ForkPolicyError(f"Repository {expected} is outside the {upstream_full_name} fork network")
    return payload


def load_github_identity(repo_root: Path) -> dict | None:
    path = repo_root / ".vaws-local/github.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ForkPolicyError("The saved GitHub identity could not be read") from exc
    if not isinstance(value, dict) or value.get("schema") != IDENTITY_SCHEMA or not isinstance(value.get("login"), str):
        raise ForkPolicyError("The saved GitHub identity has an unsupported format")
    saved_forks = value.get("forks", {})
    if not isinstance(saved_forks, dict) or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name)
        for name in saved_forks.values()
    ):
        raise ForkPolicyError("Saved forks must map repository roles to owner/name strings")
    return value


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if check and result.returncode:
        raise ForkPolicyError(result.stderr.strip() or "Git command failed")
    return result


def repository_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = git(path, "rev-parse", "--show-toplevel", check=False)
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == path.resolve()


def parse_github_url(value: str) -> str | None:
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?", value)
    return match[1] if match else None


def github_url(full_name: str, previous: str | None = None) -> str:
    if previous and previous.startswith("git@github.com:"):
        return f"git@github.com:{full_name}.git"
    if previous and previous.startswith("ssh://git@github.com/"):
        return f"ssh://git@github.com/{full_name}.git"
    return f"https://github.com/{full_name}.git"


def config_values(repo: Path, key: str, *, local: bool = False) -> list[str]:
    result = git(repo, "config", *(["--local"] if local else []), "--get-all", key, check=False)
    if result.returncode not in (0, 1):
        raise ForkPolicyError(result.stderr.strip() or f"Cannot read {key}")
    return result.stdout.splitlines()


def remote_plan(repo: Path, origin: str, upstream: str, replace: bool = False) -> dict:
    plan = {}
    for name, target in (("origin", origin), ("upstream", upstream)):
        current = {kind: config_values(repo, f"remote.{name}.{kind}") for kind in ("url", "pushurl")}
        local = {kind: config_values(repo, f"remote.{name}.{kind}", local=True) for kind in ("url", "pushurl")}
        if current != local:
            raise ForkPolicyError(f"{repo.name}/{name} inherits remote URLs; configure them locally before setup")
        urls, pushes = current["url"], current["pushurl"]
        complex_config = (len(urls) > 1 or len(pushes) > 1 or
                          any(parse_github_url(value) is None for value in urls + pushes) or
                          any((parse_github_url(value) or "").casefold() != target.casefold() for value in pushes))
        if complex_config and not replace:
            raise ForkPolicyError(f"{repo.name}/{name} has multiple or custom fetch/push URLs; "
                                  "review them and use --replace-primary-remotes to replace these URLs with a backup")
        desired = {"url": [github_url(target, urls[0] if urls else None)],
                   "pushurl": [github_url(target, pushes[0])] if pushes else []}
        # Preserve an already correct URL exactly, including protocol and .git spelling.
        for kind, values in current.items():
            if len(values) == 1 and (parse_github_url(values[0]) or "").casefold() == target.casefold():
                desired[kind] = values
        for value in desired["url"] + desired["pushurl"]:
            check_url_rewrites(repo, value, target)
        plan[name] = {"before": current, "after": desired, "target": target}
    return plan


def check_url_rewrites(repo: Path, value: str, target: str) -> None:
    rules = git(repo, "config", "--null", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$", check=False)
    if rules.returncode not in (0, 1):
        raise ForkPolicyError("Cannot inspect Git URL rewrites")
    for entry in rules.stdout.split("\0"):
        if not entry:
            continue
        key, prefix = entry.split("\n", 1)
        replacement = key[len("url."):].rsplit(".", 1)[0]
        if value.startswith(prefix):
            rewritten = replacement + value[len(prefix):]
            if (parse_github_url(rewritten) or "").casefold() != target.casefold():
                raise ForkPolicyError("Git URL rewriting changes the intended personal/upstream repository")


def replace_values(repo: Path, key: str, values: list[str]) -> None:
    result = git(repo, "config", "--local", "--unset-all", key, check=False)
    if result.returncode not in (0, 5):
        raise ForkPolicyError(result.stderr.strip() or f"Cannot update {key}")
    for value in values:
        git(repo, "config", "--local", "--add", key, value)


def configure_remotes(repo: Path, plan: dict, state_root: Path) -> str | None:
    if all(item["before"] == item["after"] for item in plan.values()):
        verify_remotes(repo, plan)
        return None
    backup = state_root / "fork-setup-backups" / f"{time.time_ns()}-{repo.name}.json"
    atomic_json(backup, {"repository": str(repo), "remotes": plan})
    for name, item in plan.items():
        for kind, values in item["after"].items():
            replace_values(repo, f"remote.{name}.{kind}", values)
        if not config_values(repo, f"remote.{name}.fetch"):
            git(repo, "config", "--local", "--add", f"remote.{name}.fetch",
                f"+refs/heads/*:refs/remotes/{name}/*")
    verify_remotes(repo, plan)
    return str(backup)


def verify_remotes(repo: Path, plan: dict) -> None:
    for name, item in plan.items():
        for push in (False, True):
            values = git(repo, "remote", "get-url", "--all", *(["--push"] if push else []), name).stdout.splitlines()
            if not values or any((parse_github_url(value) or "").casefold() != item["target"].casefold() for value in values):
                raise ForkPolicyError(f"Effective {name} URL does not match the verified repository; inspect Git URL rewrites")


def missing_submodule(root: Path, role: str) -> dict:
    path = root / REPOSITORIES[role]["path"]
    if path.exists() and any(path.iterdir()):
        raise ForkPolicyError(f"{role} is not an initialized repository and its directory is not empty")
    rows = git(root, "ls-files", "--stage", "--", role).stdout.splitlines()
    if len(rows) != 1 or not rows[0].startswith("160000 ") or rows[0].split()[2] != "0":
        raise ForkPolicyError(f"{role} is not one unconflicted recorded submodule gitlink")
    module_url = git(root, "config", "-f", ".gitmodules", "--get", f"submodule.{role}.url").stdout.strip()
    if (parse_github_url(module_url) or "").casefold() != REPOSITORIES[role]["upstream"].casefold():
        raise ForkPolicyError(f".gitmodules must retain the official URL for {role}")
    return {"role": role, "gitlink": rows[0].split()[1]}


def canonical_redirect(payload: dict, requested: str, upstream: str) -> bool:
    """A transferred personal name can still redirect to its canonical source.

    This identifies a missing personal fork; it never validates that redirect as
    a fork. Any other resolved repository continues through the strict policy.
    """
    resolved = payload.get("full_name")
    return (isinstance(resolved, str) and resolved.casefold() == upstream.casefold()
            and resolved.casefold() != requested.casefold())


def setup(repo_root: Path, github_user: str | None = None, *, apply: bool = False,
          roles: list[str] | None = None, replace_primary_remotes: bool = False,
          client: GitHubClient | None = None) -> dict:
    root = repo_root.expanduser().resolve()
    if not repository_root(root):
        raise ForkPolicyError("--repo-root must name the workspace Git root")
    client = client or GitHubClient()
    account = client.api("user")
    saved = load_github_identity(root)
    requested = github_user or (saved or {}).get("login")
    saved_id = (saved or {}).get("github_user_id")
    if saved_id is not None:
        if type(saved_id) is not int or saved_id <= 0 or saved_id != account.get("id"):
            raise ForkPolicyError("Authenticated GitHub user ID differs from this workspace's saved identity")
        if not github_user:
            requested = account.get("login")  # Same stable ID, possibly a renamed account.
    if not requested:
        return {"status": "needs_github_user", "authenticated_login": account.get("login"),
                "message": "Enter your personal GitHub ID with --github-user; the detected login is only a suggestion"}
    login = validate_github_user(account, requested)
    if saved and saved_id is None and saved["login"].casefold() != login.casefold():
        raise ForkPolicyError("The confirmed GitHub ID differs from this workspace's saved identity")
    selected = list(dict.fromkeys(roles or REPOSITORIES))
    if any(role not in REPOSITORIES for role in selected):
        raise ForkPolicyError("Unknown repository role")
    planned, missing = [], []
    known_forks = dict((saved or {}).get("forks") or {})
    for role in selected:
        spec = REPOSITORIES[role]
        path = root / spec["path"]
        initialized = repository_root(path)
        if not initialized:
            missing.append(missing_submodule(root, role))
        personal = known_forks.get(role) or f"{login}/{spec['upstream'].split('/')[-1]}"
        try:
            existing = client.api(f"repos/{personal}")
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise
            existing = None
        legacy_redirect = existing is not None and canonical_redirect(existing, personal, spec["upstream"])
        if existing is not None and not legacy_redirect:
            validate_personal_fork(existing, login, spec["upstream"], personal)
        remotes = remote_plan(path, personal, spec["upstream"], replace_primary_remotes) if initialized else None
        planned.append({"role": role, "upstream": spec["upstream"], "personal": personal,
                        "create_fork": existing is None or legacy_redirect,
                        "legacy_redirect": legacy_redirect,
                        "resolved_full_name": existing.get("full_name") if existing else None,
                        "personal_fork": existing is not None and not legacy_redirect,
                        "initialize_submodule": not initialized,
                        "remotes": remotes})
    result = {"status": "planned", "github_user": login, "repositories": planned}
    if not apply:
        return result
    state_root = root / ".vaws-local"
    if git(root, "ls-files", "--", ".vaws-local").stdout.strip():
        raise ForkPolicyError(".vaws-local must remain untracked before saving GitHub setup state")
    # This is a client snapshot, never a server principal or authorization proof.
    snapshot = {"schema": IDENTITY_SCHEMA, "login": login,
                "github_user_id": account["id"], "forks": known_forks}
    atomic_json(state_root / "github.json", snapshot)
    for item in planned:
        if item["create_fork"]:
            # Omitting organization makes GitHub create the authenticated user's fork.
            created = client.api(f"repos/{item['upstream']}/forks", method="POST", fields={})
            # GitHub can allocate a suffix when the old name is a transfer
            # redirect. Follow the returned real fork, never the redirect.
            actual = created.get("full_name")
            validate_personal_fork(created, login, item["upstream"], actual)
            item["personal"] = actual
        for attempt in range(6):
            try:
                payload = client.api(f"repos/{item['personal']}")
                if item["create_fork"] and canonical_redirect(payload, item["personal"], item["upstream"]):
                    if attempt == 5:
                        raise ForkPolicyError(f"Repository {item['personal']} still redirects to {item['upstream']}; "
                                              "a personal fork was not verified")
                    time.sleep(2)
                    continue
                validate_personal_fork(payload, login, item["upstream"], item["personal"])
                item["personal_fork"] = True
                item["verified_full_name"] = payload["full_name"]
                known_forks[item["role"]] = payload["full_name"]
                atomic_json(state_root / "github.json", snapshot)
                break
            except GitHubAPIError as exc:
                if exc.status != 404 or not item["create_fork"] or attempt == 5:
                    raise
                time.sleep(2)
    for item in missing:
        role = item["role"]
        path = root / role
        if repository_root(path):
            # Another initializer may have completed during the GitHub requests.
            # Its business checkout is now initialized and must remain untouched.
            continue
        if missing_submodule(root, role) != item:
            raise ForkPolicyError(f"{role} gitlink changed during setup; inspect the updated plan")
        # Override only this invocation, never tracked .gitmodules or global settings.
        git(root, "-c", f"submodule.{role}.url=https://github.com/{REPOSITORIES[role]['upstream']}.git",
            "submodule", "update", "--init", "--recursive", "--checkout", "--", role)
        if not repository_root(path) or git(path, "rev-parse", "HEAD").stdout.strip() != item["gitlink"]:
            raise ForkPolicyError(f"{role} did not initialize at its recorded gitlink")
    for item in planned:
        path = root / REPOSITORIES[item["role"]]["path"]
        # Recheck after network calls and initialization before any remote edit.
        item["remotes"] = remote_plan(path, item["personal"], item["upstream"], replace_primary_remotes)
        item["backup"] = configure_remotes(path, item["remotes"], state_root)
    result["status"] = "configured"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--github-user", help="explicit personal GitHub login, required on first setup")
    parser.add_argument("--repo", action="append", choices=REPOSITORIES, help="limit setup; default is all three repositories")
    parser.add_argument("--apply", action="store_true", help="create missing personal forks and configure local remotes")
    parser.add_argument("--replace-primary-remotes", action="store_true",
                        help="replace custom or multiple origin/upstream URLs, recording their previous values locally")
    args = parser.parse_args()
    try:
        result = setup(args.repo_root, args.github_user, apply=args.apply, roles=args.repo,
                       replace_primary_remotes=args.replace_primary_remotes)
        code = 2 if result["status"] == "needs_github_user" else 0
    except (ForkPolicyError, OSError) as exc:
        result, code = {"status": "blocked", "message": str(exc)}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

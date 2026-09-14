#!/usr/bin/env python3
"""Plan or establish verified personal GitHub forks using only the standard library.

Existing business branches and edits stay in place. Missing source repositories
are created as independent clones at the versions in sources.lock.json.
"""
from __future__ import annotations

from vaws_diagnostics_adapter import measured as _diagnostic_measured

# Observe the real CLI before optional runtime imports; copied remote helpers stay standalone.
if __name__ == "__main__":
    import sys as _vaws_sys
    from pathlib import Path as _VawsPath
    _vaws_parents = _VawsPath(__file__).absolute().parents
    _vaws_lib = _vaws_parents[1] / "lib" if len(_vaws_parents) > 1 else None
    _vaws_entry = None
    if _vaws_lib is not None and (_vaws_lib / "vaws_diagnostics_adapter.py").is_file():
        _vaws_sys.path.insert(0, str(_vaws_lib))
        from vaws_diagnostics_adapter import bootstrap as _vaws_bootstrap
        _vaws_entry = _vaws_bootstrap(__file__)

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from urllib import error as urlerror, parse as urlparse, request as urlrequest
import uuid

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


def _safe_text(value: object) -> str:
    """Redact known credentials even when their shape is not a GitHub PAT."""
    from vaws_workspace_update import redact
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "[redacted]")
    text = redact(text)
    return re.sub(r"(?im)^([ \t]*[<>*]?[ \t]*(?:authorization|proxy-authorization):)[^\r\n]*",
                  r"\1 [redacted]", text)


class _GitHubRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards Authorization to a different origin.
        target = urlparse.urlsplit(newurl)
        if target.scheme != "https" or target.netloc != "api.github.com":
            raise GitHubAPIError("GitHub API redirected outside its HTTPS origin", code)
        if req.get_method() not in {"GET", "HEAD"}:
            raise GitHubAPIError("GitHub write request redirected; inspect its result before retrying", code)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class GitHubClient:
    """Use an environment PAT directly, or the CLI's existing secure login."""

    @property
    def provider(self) -> str:
        return next((name for name in ("GH_TOKEN", "GITHUB_TOKEN") if os.environ.get(name)), "gh")

    @_diagnostic_measured('source.github_request')
    def api(self, endpoint: str, method: str = "GET", fields: dict | None = None) -> dict:
        if not isinstance(endpoint, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", endpoint) or endpoint.startswith("/"):
            raise GitHubAPIError("GitHub API endpoint must be a relative repository or user path")
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise GitHubAPIError("Unsupported GitHub API method")
        from vaws_network import network_scope
        with network_scope(ROOT):
            if self.provider != "gh":
                return self._token_api(endpoint, method, fields)
            return self._gh_api(endpoint, method, fields)

    @staticmethod
    def _decode(body: str, endpoint: str, evidence: dict) -> dict:
        if not body.strip() and endpoint.startswith("user/starred/"):
            return {}  # The star endpoints use HTTP 204 with no response body.
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError("GitHub returned invalid JSON", evidence=evidence) from exc
        if not isinstance(value, dict):
            raise GitHubAPIError("GitHub returned an unexpected response", evidence=evidence)
        return value

    def _token_api(self, endpoint: str, method: str, fields: dict | None) -> dict:
        token = os.environ[self.provider]
        if not token.strip() or any(character.isspace() or ord(character) < 32 for character in token):
            raise GitHubAPIError("GitHub token environment variable contains invalid whitespace")
        headers = {"Accept": "application/vnd.github+json", "Authorization": "Bearer " + token,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vaws-workspace"}
        data = None
        if fields is not None:
            data = json.dumps(fields).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urlrequest.Request("https://api.github.com/" + endpoint, data=data, headers=headers, method=method)
        evidence = {"provider": self.provider, "endpoint": endpoint, "method": method, "status": None}
        try:
            with urlrequest.build_opener(_GitHubRedirect()).open(request, timeout=60) as response:
                evidence["status"] = response.status
                body = response.read(1024 * 1024 + 1)
        except urlerror.HTTPError as exc:
            # Error bodies may echo submitted values. Keep only the HTTP fact;
            # no headers, token, response body or proxy URL enters evidence.
            evidence["status"] = exc.code
            exc.close()
            raise GitHubAPIError(f"GitHub request failed (HTTP {exc.code})", exc.code, evidence=evidence) from None
        except (OSError, ValueError) as exc:
            evidence["error_type"] = type(exc).__name__
            raise GitHubAPIError("GitHub request could not complete: " + _safe_text(exc), evidence=evidence) from None
        if len(body) > 1024 * 1024:
            raise GitHubAPIError("GitHub response exceeded 1 MiB", evidence=evidence)
        return self._decode(body.decode("utf-8", "replace"), endpoint, evidence)

    def _gh_api(self, endpoint: str, method: str, fields: dict | None) -> dict:
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
            return {"command": [redact(item) for item in command], "endpoint": redact(endpoint),
                    "method": method, "returncode": getattr(result, "returncode", None),
                    "stdout": _safe_text(getattr(result, "stdout", None)),
                    "stderr": _safe_text(getattr(result, "stderr", None)), **extra}

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
        return self._decode(result.stdout, endpoint, evidence(result))

    def ensure_star(self, repository: str) -> dict:
        """Apply an explicitly accepted star, leaving an existing star intact."""
        return ensure_star(repository, client=self)


def detect_github_auth(*, client: GitHubClient | None = None) -> dict:
    """Read one authenticated account as a candidate, never as user consent."""
    client = client or GitHubClient()
    result = {"provider": getattr(client, "provider", "external"), "authenticated": False}
    try:
        account = client.api("user")
        login = validate_github_user(account, account.get("login", ""))
        return {**result, "authenticated": True, "login": login, "github_user_id": account["id"], "type": "User"}
    except (ForkPolicyError, OSError, TypeError) as exc:
        return {**result, "error": _safe_text(exc), "status": getattr(exc, "status", None)}


def ensure_star(repository: str, *, client: GitHubClient | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ForkPolicyError("Star repository must be an owner/name pair")
    client = client or GitHubClient()
    endpoint = "user/starred/" + repository
    try:
        client.api(endpoint)
        return {"status": "already_starred", "repository": repository}
    except GitHubAPIError as exc:
        if exc.status != 404:
            raise
    client.api(endpoint, method="PUT")
    return {"status": "starred", "repository": repository}


def _credential_command() -> str:
    helper = Path(__file__).with_name("vaws_git_credential.py").resolve()
    # Git executes ! helpers with its POSIX shell, also on Windows. The command
    # contains interpreter/script paths only; credentials remain in the process.
    return "!" + " ".join(shlex.quote(str(value).replace("\\", "/")) for value in (sys.executable, helper))


def github_git_environment(environment: dict | None = None) -> dict:
    """Return a single Git process's token authentication, including first clone."""
    environment = dict(os.environ if environment is None else environment)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if not any(environment.get(name) for name in ("GH_TOKEN", "GITHUB_TOKEN")):
        return environment
    # Git tracing can dump arbitrary environment variables and bypass the
    # product redactor. Explicit credentials must not enter those trace sinks.
    for key in tuple(environment):
        if key.upper().startswith("GIT_TRACE") or key.upper() == "GIT_CURL_VERBOSE":
            environment.pop(key)
    try:
        count = int(environment.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        raise ForkPolicyError("GIT_CONFIG_COUNT must be an integer") from None
    if not 0 <= count <= 100:
        raise ForkPolicyError("GIT_CONFIG_COUNT is outside the supported range")
    # An empty helper resets previous helpers for this host. Do not allow an
    # unrelated cached account to silently replace the explicitly provided PAT.
    for value in ("", _credential_command()):
        environment[f"GIT_CONFIG_KEY_{count}"] = "credential.https://github.com.helper"
        environment[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    environment["GIT_CONFIG_COUNT"] = str(count)
    return environment


def configure_token_git(repo: Path) -> str:
    """Keep token overrides command-scoped so later keyring auth still works."""
    key, helper = "credential.https://github.com.helper", _credential_command()
    values = config_values(repo, key, local=True)
    if values == ["", helper]:
        replace_values(repo, key, [])
        return "legacy_override_removed"
    if values:
        return "existing_helper_preserved"
    return "command_scoped" if GitHubClient().provider != "gh" else "unchanged"


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
    environment = github_git_environment()
    if args and args[0] in {"fetch", "clone", "push", "ls-remote"}:
        from vaws_network import environment_for
        environment = environment_for(ROOT, environment)
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=environment, timeout=120)
    if check and result.returncode:
        raise ForkPolicyError(_safe_text(result.stderr).strip() or "Git command failed")
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
        if any("@" in urlparse.urlsplit(value).netloc for values in current.values() for value in values
               if value.startswith(("https://", "http://"))):
            raise ForkPolicyError("Remote URL contains embedded credentials; move authentication to a Git credential "
                                  "helper or environment-backed askpass and save a credential-free URL before setup")
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


def independent_source_path(root: Path, role: str) -> None:
    if role == "workspace":
        return
    if git(root, "ls-files", "--", role).stdout.strip():
        raise ForkPolicyError(f"{role} must be an independent source repository, not tracked by the workspace")
    modules = root / ".gitmodules"
    if modules.is_file():
        entries = git(root, "config", "--file", str(modules), "--get-regexp", r"^submodule\..*\.path$", check=False)
        if any(line.split(None, 1)[-1].strip("/") == role for line in entries.stdout.splitlines()):
            raise ForkPolicyError(f"{role} still has a .gitmodules entry; remove the old submodule topology first")


def missing_source(root: Path, role: str) -> dict:
    from vaws_source_lock import selected_sources

    path = root / REPOSITORIES[role]["path"]
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise ForkPolicyError(f"{role} is not an initialized repository and its directory is not empty")
    independent_source_path(root, role)
    locked = selected_sources(root)[role]
    return {"role": role, **locked}


def canonical_redirect(payload: dict, requested: str, upstream: str) -> bool:
    """A transferred personal name can still redirect to its canonical source.

    This identifies a missing personal fork; it never validates that redirect as
    a fork. Any other resolved repository continues through the strict policy.
    """
    resolved = payload.get("full_name")
    return (isinstance(resolved, str) and resolved.casefold() == upstream.casefold()
            and resolved.casefold() != requested.casefold())


@_diagnostic_measured('source.fork_setup')
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
    for role in selected:
        independent_source_path(root, role)
    planned, missing = [], []
    known_forks = dict((saved or {}).get("forks") or {})
    for role in selected:
        spec = REPOSITORIES[role]
        path = root / spec["path"]
        initialized = repository_root(path)
        if not initialized:
            missing.append(missing_source(root, role))
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
                        "initialize_source": not initialized,
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
        if missing_source(root, role) != item:
            raise ForkPolicyError(f"{role} source selection changed during setup")
        from vaws_native_workspace import prepare_source
        from vaws_workspace_update import path_lock

        # The actual user path is published only after the complete fixed source
        # has checked out. An incomplete private clone remains available to inspect.
        with path_lock(state_root / "source-initialization.lock"):
            if repository_root(path):
                continue
            staging = state_root / "source-initialization" / (role + "-" + uuid.uuid4().hex)
            staging.parent.mkdir(parents=True, exist_ok=True)
            prepare_source(staging, repository=item["repository"], revision=item["revision"])
            if missing_source(root, role) != item:
                raise ForkPolicyError(f"{role} source selection changed while preparing; copy retained at {staging}")
            if path.exists():
                path.rmdir()  # Only the empty path just checked above is eligible.
            staging.rename(path)
        if not repository_root(path) or git(path, "rev-parse", "HEAD").stdout.strip() != item["revision"]:
            raise ForkPolicyError(f"{role} did not initialize at its locked source revision")
    for item in planned:
        path = root / REPOSITORIES[item["role"]]["path"]
        # Recheck after network calls and initialization before any remote edit.
        item["remotes"] = remote_plan(path, item["personal"], item["upstream"], replace_primary_remotes)
        item["backup"] = configure_remotes(path, item["remotes"], state_root)
        item["git_authentication"] = configure_token_git(path)
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
    raise SystemExit((_vaws_entry.run(main) if _vaws_entry else main()))

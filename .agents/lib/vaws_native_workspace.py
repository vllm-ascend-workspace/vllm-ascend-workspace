"""Copy native editing state before client startup.

Git owns objects and indexes. This module owns only a bounded copy operation;
it does not allocate tasks, infer session identity, or manage workspace leases.
"""
from __future__ import annotations

from vaws_diagnostics_adapter import measured as _diagnostic_measured, wrap_context, phase, context_environment

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

REPOSITORIES = {"workspace": "vllm-ascend-workspace/vllm-ascend-workspace",
                "vllm": "vllm-project/vllm", "vllm-ascend": "vllm-project/vllm-ascend"}


class WorkspaceCopyError(RuntimeError):
    pass


def git(root: Path, *args: str, data: bytes | None = None, env=None, timeout: int = 120) -> bytes:
    # Calls target an explicit repository root (or a parent for clone/init).
    # Windows Git can miss a deeply nested .git even with core.longpaths;
    # never let discovery silently redirect an operation to an ancestor.
    environment = context_environment(os.environ if env is None else env)
    environment["GIT_CEILING_DIRECTORIES"] = str(Path(root).resolve().parent)
    action = args[0] if args else "unknown"
    measured = action in {"fetch", "clone", "checkout", "reset", "push", "ls-remote"}
    with phase("source.git", action=action, level="INFO" if measured else "DEBUG"):
        command = ["git", *(["-c", "core.longpaths=true"] if os.name == "nt" else []),
                   "--no-optional-locks", "-C", str(root), *args]
        from vaws_process_wait import run_captured
        if action in {"fetch", "clone", "push", "ls-remote"}:
            from vaws_network import environment_for
            environment = environment_for(root, environment)
        result = run_captured(command, stage="source_" + action, timeout=timeout,
                              env=environment, encoding=None, limit=None, input_data=data)
        if result.returncode:
            raise WorkspaceCopyError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _paths(raw: bytes) -> list[str]:
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def _private(name: str) -> bool:
    # These copies are portable to case-insensitive Windows/macOS filesystems.
    return name.split("/", 1)[0].rstrip(" .").casefold() == ".vaws-local"


def _file_state(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        # Windows directory links have a distinct reparse-point type, including
        # dangling links. Do not infer it by following the target. POSIX has no
        # file/directory distinction in a symbolic link itself.
        directory = bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY) if os.name == "nt" else False
        return ["link", os.readlink(path), directory]
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceCopyError(f"unsupported file type: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return ["file", stat.S_IMODE(info.st_mode), info.st_size, digest.hexdigest()]


def _configuration_entries(source: Path, *, local=False) -> list[tuple[str, str]]:
    return [tuple(entry.split("\n", 1)) for entry in git(source, "config", *(["--local"] if local else []), "--null", "--list").decode("utf-8").split("\0")
            if "\n" in entry]


def _checkout_configuration(entries: list[tuple[str, str]]) -> dict[str, str]:
    policy = {"core.autocrlf": "false", "core.eol": "native", "core.safecrlf": "false",
              "core.filemode": "true", "core.ignorecase": "false", "core.symlinks": "true"}
    for key, value in entries:
        if key in policy:
            policy[key] = value
    if policy["core.eol"] == "native":
        policy["core.eol"] = "crlf" if os.name == "nt" else "lf"
    return policy


def _branch_configuration(source: Path) -> list[tuple[str, str]]:
    fields = iter(git(source, "config", "--show-scope", "--null", "--list").decode("utf-8").split("\0"))
    return [tuple(entry.split("\n", 1)) for scope, entry in zip(fields, fields)
            if scope in {"local", "worktree"} and entry.startswith("branch.") and "\n" in entry]


@_diagnostic_measured('source.capture')
def _capture(source: Path) -> dict:
    head = git(source, "rev-parse", "HEAD").decode().strip()
    flags = git(source, "ls-files", "-v", "-z").split(b"\0")
    if any(row and (row[:1].islower() or row[:1] == b"S") for row in flags):
        raise WorkspaceCopyError("sparse/assume-unchanged index entries require an ordinary checkout before copying")
    staged = git(source, "ls-files", "--stage", "-z")
    if any(row and row.split(b"\t", 1)[0].split()[-1] != b"0" for row in staged.split(b"\0")):
        raise WorkspaceCopyError("resolve the unmerged Git index before creating a workspace")
    modules = []
    for row in staged.split(b"\0"):
        if row.startswith(b"160000 "):
            modules.append(os.fsdecode(row.split(b"\t", 1)[1]))
    tracked = _paths(git(source, "ls-files", "-z"))
    if any(_private(name) for name in tracked):
        raise WorkspaceCopyError("tracked .vaws-local state must be removed from the index before creating a workspace")
    untracked = _paths(git(source, "ls-files", "--others", "--exclude-standard", "-z"))
    names = sorted(name for name in set(tracked + untracked) - set(modules) if not _private(name))
    files = {name: _file_state(source / name) for name in names}
    index_path = Path(os.fsdecode(git(source, "rev-parse", "--git-path", "index").strip()))
    if not index_path.is_absolute():
        index_path = source / index_path
    # write-tree may update the index's cache extension. Keep the real index
    # byte-for-byte unchanged and let Git interpret split indexes in its own dir.
    temporary = index_path.with_name("vaws-copy-index-" + uuid.uuid4().hex)
    try:
        shutil.copyfile(index_path, temporary)
        environment = {**os.environ, "GIT_INDEX_FILE": str(temporary)}
        tree = git(source, "write-tree", env=environment).decode().strip()
    finally:
        temporary.unlink(missing_ok=True)
    children = {}
    for name in modules:
        path = source / name
        if not (path / ".git").exists():
            # A normal non-recursive clone has empty, uninitialized gitlinks.
            # Keep that state without fetching source the task does not need.
            # Also distinguish a locally deleted directory from an empty one.
            if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
                raise WorkspaceCopyError(f"uninitialized submodule {name!r} contains non-Git content that cannot be copied as an empty gitlink")
            children[name] = {"uninitialized": True, "directory": path.is_dir()}
        else:
            children[name] = _capture(path)
    entries = _configuration_entries(source)
    configuration = _checkout_configuration(entries)
    refs = git(source, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)").decode("utf-8").splitlines()
    symbolic_head = git(source, "rev-parse", "--symbolic-full-name", "HEAD").decode("utf-8").strip()
    stash_path = Path(os.fsdecode(git(source, "rev-parse", "--git-path", "logs/refs/stash").strip()))
    if not stash_path.is_absolute():
        stash_path = source / stash_path
    status = git(source, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    status = b"\0".join(row for row in status.split(b"\0")
                         if not (row.startswith(b"?? ") and _private(os.fsdecode(row[3:]))))
    return {"head": head, "tree": tree, "tracked": tracked, "files": files, "modules": children,
            "configuration": configuration, "status": status.hex(), "refs": refs,
            "symbolic_head": "" if symbolic_head == "HEAD" else symbolic_head,
            "branch_configuration": _branch_configuration(source),
            "stash_log": stash_path.read_bytes().hex() if stash_path.is_file() else None,
            "staged_diff": git(source, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--cached").hex(),
            "working_diff": git(source, "diff", "--no-ext-diff", "--no-textconv", "--binary").hex()}


def _copy_remotes(source: Path, destination: Path, entries: list[tuple[str, str]] | None = None) -> None:
    for remote in git(destination, "remote").decode().splitlines():
        git(destination, "remote", "remove", remote)
    for key, value in _configuration_entries(source) if entries is None else entries:
        if key.startswith("remote.") or key == "push.autosetupremote":
            git(destination, "config", "--local", "--add", key, value)


@_diagnostic_measured('source.clone_checkout')
def prepare_source(destination: Path, *, repository: str, revision: str,
                   local_source: Path | None = None) -> None:
    """Create a self-contained checkout at one locked canonical commit.

    Only the new destination is written. Existing destinations are rejected,
    including failed earlier attempts, so user work is never reset implicitly.
    """
    if repository not in REPOSITORIES.values() or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise WorkspaceCopyError("source requires a supported canonical repository and full commit SHA")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise WorkspaceCopyError(f"source destination already exists: {destination}")
    url = f"https://github.com/{repository}.git"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if local_source is not None:
        local_source = Path(local_source).resolve(strict=True)
        storage = Path(os.fsdecode(git(local_source, "rev-parse", "--git-path", "objects/info/alternates").strip()))
        if not storage.is_absolute():
            storage = local_source / storage
        if storage.exists():
            raise WorkspaceCopyError("source uses object alternates; a self-contained source is required")
        entries = _configuration_entries(local_source)
        configuration = [f"--config={key}={value}" for key, value in _checkout_configuration(entries).items()]
        git(local_source, "clone", "--local", "--no-checkout", *configuration, "--", str(local_source), str(destination))
        _copy_remotes(local_source, destination, entries)
    else:
        from vaws_github import check_url_rewrites
        check_url_rewrites(destination.parent, url, repository)
        git(destination.parent, "clone", "--no-checkout", "--", url, str(destination), timeout=1800)
        for key, value in _checkout_configuration(_configuration_entries(destination)).items():
            git(destination, "config", "--local", key, value)
    if (destination / ".git/objects/info/alternates").exists():
        raise WorkspaceCopyError("prepared source unexpectedly depends on object alternates")
    if os.name == "nt":
        git(destination, "config", "core.longpaths", "true")
    try:
        git(destination, "cat-file", "-e", revision + "^{commit}")
    except WorkspaceCopyError:
        from vaws_github import check_url_rewrites
        check_url_rewrites(destination, url, repository)
        git(destination, "fetch", "--no-tags", url, revision, timeout=600)
    git(destination, "checkout", "--detach", revision)
    if git(destination, "rev-parse", "HEAD").decode().strip() != revision:
        raise WorkspaceCopyError("prepared source did not select the requested commit")
    if not git(destination, "remote").strip():
        git(destination, "remote", "add", "origin", url)


@_diagnostic_measured('source.copy')
def create_prepared_workspace(prepared: dict, destination: Path) -> dict:
    """Clone the updater's fixed revisions without scanning mutable workfiles.

    Canonical preparation has already validated this plan. Revisions remain
    explicit so even a later staging workfile edit cannot change copied code.
    Dirty editing copies and conversation forks use create_workspace instead.
    """
    stage = Path(prepared["stage"]).resolve(strict=True)
    destination = destination.absolute()
    from vaws_local_owner import windows_mounted_workspace
    if windows_mounted_workspace(stage):
        raise WorkspaceCopyError("prepared workspace cloning requires the native Windows owner")
    if destination.exists() or destination.is_symlink():
        raise WorkspaceCopyError(f"workspace destination already exists: {destination}")
    destination = destination.resolve()
    sources = prepared["sources"]
    revisions = prepared["revisions"]
    if set(revisions) != {"workspace", *sources} or any(name not in REPOSITORIES or name == "workspace" for name in sources):
        raise WorkspaceCopyError("prepared source names and revisions do not match")
    roots = {"workspace": stage, **{name: Path(path).resolve(strict=True) for name, path in sources.items()}}
    for name, source in roots.items():
        if not re.fullmatch(r"[0-9a-f]{40}", revisions[name]):
            raise WorkspaceCopyError("prepared sources require full commit SHAs")
        if name != "workspace" and source != stage / name:
            raise WorkspaceCopyError("prepared business source is outside its stage")
        if source == destination or source in destination.parents or destination in source.parents:
            raise WorkspaceCopyError("prepared source overlaps the destination")
        if not (source / ".git").is_dir() or (source / ".git/objects/info/alternates").exists():
            raise WorkspaceCopyError("prepared source must have independent Git storage")
    result_sources, copy_seconds = {}, {}
    task_branch = "codex/task-" + uuid.uuid4().hex[:12]
    def copy(item):
        name, source = item
        started = time.monotonic()
        target = destination if name == "workspace" else destination / name
        prepare_source(target, repository=REPOSITORIES[name], revision=revisions[name], local_source=source)
        git(target, "switch", "-c", task_branch)
        if not any(key == "push.autosetupremote" for key, _ in _configuration_entries(target)):
            git(target, "config", "--local", "push.autoSetupRemote", "true")
        if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
            from vaws_github import configure_token_git
            configure_token_git(target)
        return name, str(target), time.monotonic()-started
    name, target, seconds = copy(("workspace", roots.pop("workspace")))
    result_sources[name], copy_seconds[name] = target, seconds
    if roots:
        with ThreadPoolExecutor(max_workers=min(2, len(roots))) as pool:
            for name, target, seconds in pool.map(wrap_context(copy), roots.items()):
                result_sources[name], copy_seconds[name] = target, seconds
    return {"schema": "vaws.workspace-copy.v1", "state": "ready", "source": str(stage),
            "workspace": str(destination), "sources": result_sources, "copy_seconds": copy_seconds,
            "head": revisions["workspace"], "revisions": dict(revisions)}


@_diagnostic_measured('source.copy_repository')
def _copy_repository(source: Path, destination: Path, snapshot: dict) -> None:
    # Local clone copies/hardlinks objects, with independent refs and .git dirs.
    # Unlike linked-worktree absolute gitdir pointers, these are readable by
    # native Windows Git and WSL Git on the same mounted filesystem.
    git(source, "clone", "--local", "--no-checkout", "--", str(source), str(destination))
    if (destination / ".git/objects/info/alternates").exists():
        raise WorkspaceCopyError("source uses object alternates; create a self-contained source checkout first")
    if os.name == "nt":
        git(destination, "config", "core.longpaths", "true")
    for key, value in snapshot["configuration"].items():
        git(destination, "config", key, value)
    # clone advertises only part of the source refs. Preserve local branches,
    # tags, private refs and stashes without altering the source or using alternates.
    _copy_remotes(source, destination)
    source_refs = {row.split("\0", 1)[0] for row in snapshot["refs"]}
    commands = [f"delete {ref}\n" for ref in git(destination, "for-each-ref", "--format=%(refname)").decode().splitlines()
                if ref not in source_refs]
    symbolic = []
    for row in snapshot["refs"]:
        ref, oid, symref = row.split("\0")
        if symref:
            symbolic.append((ref, symref))
        else:
            commands.append(f"update {ref} {oid}\n")
    if commands:
        git(destination, "update-ref", "--stdin", data=("option no-deref\n"+"".join(commands)).encode())
    for ref, target in symbolic:
        git(destination, "symbolic-ref", ref, target)
    if snapshot["symbolic_head"]:
        git(destination, "symbolic-ref", "HEAD", snapshot["symbolic_head"])
    else:
        git(destination, "update-ref", "--no-deref", "HEAD", snapshot["head"])
    for key in {key for key, _ in _configuration_entries(destination, local=True) if key.startswith("branch.")}:
        git(destination, "config", "--local", "--unset-all", key)
    for key, value in snapshot["branch_configuration"]:
        git(destination, "config", "--local", "--add", key, value)
    if snapshot["stash_log"] is not None:
        stash = destination / ".git/logs/refs/stash"
        stash.parent.mkdir(parents=True, exist_ok=True)
        stash.write_bytes(bytes.fromhex(snapshot["stash_log"]))
    git(destination, "read-tree", snapshot["tree"])
    exclude = destination / ".git/info/exclude"
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n.vaws-local/\n")
    _copy_contents(source, destination, snapshot)


def _copy_contents(source: Path, destination: Path, snapshot: dict) -> None:
    for name, state in snapshot["files"].items():
        if state is None or state[0] == "link":
            continue
        src, dst = source / name, destination / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for name, child in snapshot["modules"].items():
        if child.get("uninitialized"):
            if child["directory"]:
                (destination / name).mkdir(parents=True)
        else:
            _copy_repository(source / name, destination / name, child)
    # Targets, including submodules, exist before links are created. Explicit
    # Windows link types also preserve directory and dangling-directory links.
    for name, state in snapshot["files"].items():
        if state is not None and state[0] == "link":
            dst = destination / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(state[1], target_is_directory=state[2])
    actual_tracked = set(_paths(git(destination, "ls-files", "-z")))
    intent = sorted(set(snapshot["tracked"]) - actual_tracked)
    if intent:
        missing = [destination / name for name in intent if snapshot["files"][name] is None]
        for path in missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        try:
            git(destination, "add", "--intent-to-add", "--", *intent)
        finally:
            for path in missing:
                path.unlink(missing_ok=True)


_WINDOWS_COPY = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from vaws_native_workspace import create_workspace
selection = json.loads(sys.argv[4])
print(json.dumps(create_workspace(Path(sys.argv[2]), Path(sys.argv[3]), sources=selection), ensure_ascii=True))
"""

_WINDOWS_LAUNCH = """
import sys
sys.path.insert(0, sys.argv[1])
from vaws_windows import owned_process
command = [sys.executable, '-I', '-X', 'utf8', '-c', sys.argv[5], *sys.argv[1:5]]
with owned_process(command, stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer) as process:
    raise SystemExit(process.wait())
"""


@_diagnostic_measured('source.windows_owner')
def _copy_with_windows(source: Path, destination: Path, *, python: str, sources: dict | None = None) -> dict:
    """One bounded Windows-owned copy, callable directly by real bridge tests.

    Only mounted-drive paths are mapped. No path guessing, shell, RPC service,
    task identity or automatic replay is involved. The Windows launcher owns
    its copying child and Git descendants through the existing Job Object.
    """
    from vaws_local_owner import accessible_windows_path, managed_path

    try:
        arguments = [managed_path(value, windows=True)
                     for value in (Path(__file__).resolve().parent, source, destination)]
        selection = None if sources is None else {name: managed_path(Path(path), windows=True)
                                                  for name, path in sources.items()}
    except ValueError as exc:
        raise WorkspaceCopyError("Windows-owned workspace copying requires mounted-drive source, destination and helper paths") from exc
    command = [accessible_windows_path(python), "-I", "-X", "utf8", "-c", _WINDOWS_LAUNCH,
               *arguments, json.dumps(selection), _WINDOWS_COPY]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=600, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceCopyError("Windows copy timed out; destination outcome is unknown and the copy was not replayed") from exc
    except OSError as exc:
        raise WorkspaceCopyError(f"cannot start the selected Windows copy owner: {exc}") from exc
    if result.returncode:
        raise WorkspaceCopyError(result.stderr.decode("utf-8", "replace").strip() or "Windows copy owner failed")
    try:
        receipt = json.loads(result.stdout)
        if not isinstance(receipt, dict) or receipt.get("schema") != "vaws.workspace-copy.v1" or receipt.get("state") != "ready":
            raise ValueError("Windows copy did not return a ready workspace")
        return {**receipt, "source": accessible_windows_path(receipt["source"]),
                "workspace": accessible_windows_path(receipt["workspace"]),
                "sources": {name: accessible_windows_path(path) for name, path in receipt["sources"].items()}}
    except (ValueError, KeyError, TypeError) as exc:
        raise WorkspaceCopyError(f"invalid Windows copy response: {exc}") from exc


@_diagnostic_measured('source.copy')
def create_workspace(source: Path, destination: Path, *, sources: dict[str, Path] | None = None) -> dict:
    """Copy one independent root and explicitly selected source repositories.

    None reuses a valid preparation's source roots; an empty map copies only
    the root. Business repositories' own submodules retain their Git semantics.
    All Git storage is independent. Ignored files are excluded, so source roots
    are copied explicitly and never discovered by recursively scanning .git.
    Existing destinations are never replaced. A failed copy stays
    visible for diagnosis and is never published as ready.
    """
    source, destination = source.resolve(), destination.absolute()
    from vaws_local_owner import windows_mounted_workspace

    if windows_mounted_workspace(source):
        # WSL-created NTFS symlinks can use Linux-only reparse points. Native
        # Windows owns the whole operation, including Git pointer interpretation.
        # Lookup is read-only; an unavailable environment is never synthesized.
        from vaws_environment import EnvironmentError, windows_ready

        try:
            interpreter = windows_ready(Path(__file__).resolve().parents[2])["python"]
        except EnvironmentError as exc:
            raise WorkspaceCopyError(f"Windows workspace copy owner is not ready: {exc}") from exc
        return _copy_with_windows(source, destination, python=interpreter, sources=sources)
    if destination.exists():
        raise WorkspaceCopyError(f"workspace destination already exists: {destination}")
    top = Path(os.fsdecode(git(source, "rev-parse", "--show-toplevel").strip())).resolve()
    if top != source:
        raise WorkspaceCopyError("source must be the repository root")
    before = _capture(source)
    if sources is None:
        from vaws_workspace_entry import prepared_sources
        sources = {name: path for name, path in (prepared_sources(source) or {}).items() if name != "workspace"}
    selected = {}
    for name, path in sources.items():
        if name not in {"vllm", "vllm-ascend"}:
            raise WorkspaceCopyError(f"unsupported business source name: {name!r}")
        path = Path(path).resolve(strict=True)
        if not (path / ".git").exists() or Path(os.fsdecode(git(path, "rev-parse", "--show-toplevel").strip())).resolve() != path:
            raise WorkspaceCopyError(f"selected source is not a Git repository root: {path}")
        if path == source or destination.resolve() == path or destination.resolve() in path.parents:
            raise WorkspaceCopyError(f"selected source overlaps the destination or root: {path}")
        selected[name] = path
    for name in selected:
        if name in before["modules"] or any(path == name or path.startswith(name + "/") for path in before["files"]):
            raise WorkspaceCopyError(f"selected source path is also tracked or untracked root content: {name}")
    source_snapshots = {name: _capture(path) for name, path in selected.items()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_repository(source, destination, before)
    with (destination / ".git/info/exclude").open("a", encoding="utf-8") as stream:
        stream.write("".join(f"\n/{name}/\n" for name in selected))
    for name, path in selected.items():
        _copy_repository(path, destination / name, source_snapshots[name])
    if _capture(source) != before:
        raise WorkspaceCopyError(f"source changed while copying; incomplete workspace kept at {destination}")
    if _capture(destination) != before:
        raise WorkspaceCopyError(f"copied Git or file state differs; incomplete workspace kept at {destination}")
    for name, path in selected.items():
        if _capture(path) != source_snapshots[name] or _capture(destination / name) != source_snapshots[name]:
            raise WorkspaceCopyError(f"selected source changed or copied state differs: {name}; incomplete workspace kept at {destination}")
    receipt = {"schema": "vaws.workspace-copy.v1", "source": str(source),
               "workspace": str(destination), "head": before["head"],
               "staged_tree": before["tree"], "submodules": list(before["modules"]),
               "sources": {"workspace": str(destination), **{name: str(destination / name) for name in selected}},
               "source_snapshots": {name: {"head": value["head"], "staged_tree": value["tree"]}
                                    for name, value in source_snapshots.items()},
               "state": "ready", "ignored_files": "excluded"}
    # Preparation owners publish the native receipt only after environment and
    # wiring succeed. A completed file copy alone does not bind a task.
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        from vaws_github import configure_token_git
        for value in receipt["sources"].values():
            configure_token_git(Path(value))
    return receipt

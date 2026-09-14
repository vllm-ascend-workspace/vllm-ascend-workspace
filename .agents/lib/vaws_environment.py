"""Publish immutable control-plane environments and look up their ready receipts.

Dependency sync constructs core environments; actual optional capability use
can prepare its fixed child from saved lock inputs. Each install happens at
its permanent content address while holding an OS lock; the receipt is published
last. Core launchers only read receipts. A project selection is setup configuration,
not a process lease, and an explicit receipt pins an already running client.
"""
from __future__ import annotations

from vaws_diagnostics_adapter import measured as _diagnostic_measured, phase, event, context_environment

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib

PIN_ENV = "VAWS_ENV_RECEIPT"
MANAGED_PIN_ENV = "VAWS_MANAGED_ENV_RECEIPT"
READY_NAME = ".vaws-ready.json"
RECIPE_VERSION = 3
# These settings change transport, not the locked environment contents.
UV_TRANSPORT_ENV = frozenset({"UV_NATIVE_TLS", "UV_SYSTEM_CERTS", "UV_HTTP_TIMEOUT", "UV_CONCURRENT_DOWNLOADS"})


class EnvironmentError(RuntimeError):
    """An environment is unavailable or cannot be constructed safely."""


def _json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(value) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _identity() -> dict:
    return {"platform": sys.platform, "arch": platform.machine().lower(),
            "abi": sysconfig.get_config_var("SOABI") or sys.implementation.cache_tag,
            "python_version": platform.python_version(),
            "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag,
            "sysconfig_platform": sysconfig.get_platform(), "build": sys.version}


@_diagnostic_measured('environment.interpreter_identity')
def _python_identity(python: str | None) -> tuple[str, dict]:
    candidate = python or getattr(sys, "_base_executable", sys.executable)
    executable = shutil.which(candidate) or (str(Path(candidate).absolute()) if Path(candidate).is_file() else None)
    if executable is None:
        result = subprocess.run(["uv", "python", "find", candidate], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, check=False, timeout=30)
        if result.returncode:
            raise EnvironmentError(result.stderr.strip() or f"cannot find Python {candidate!r}")
        executable = result.stdout.strip()
    # uv's minor-version Python path can be a mutable junction/symlink. A venv
    # embeds this base path, so resolve it before both inspection and install.
    executable = str(Path(executable).resolve(strict=True))
    code = ("import sys,json,platform,sysconfig;print(json.dumps({"
            "'platform':sys.platform,'arch':platform.machine().lower(),"
            "'abi':sysconfig.get_config_var('SOABI') or sys.implementation.cache_tag,"
            "'python_version':platform.python_version(),'implementation':sys.implementation.name,"
            "'cache_tag':sys.implementation.cache_tag,'sysconfig_platform':sysconfig.get_platform(),"
            "'build':sys.version}))")
    result = subprocess.run([executable, "-I", "-c", code], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, check=False, timeout=15)
    if result.returncode:
        raise EnvironmentError(result.stderr.strip() or "cannot inspect the selected Python")
    identity = json.loads(result.stdout)
    if identity["platform"] != sys.platform:
        raise EnvironmentError("dependency sync must run with a Python native to this platform")
    return executable, identity


def _store() -> Path:
    if value := os.environ.get("VAWS_ENV_HOME"):
        return Path(value).expanduser().absolute()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "vaws/environments"


def _inputs(repo_root: Path) -> tuple[bytes, bytes, dict, str, str]:
    try:
        project = (repo_root / "pyproject.toml").read_bytes()
        lock = (repo_root / "uv.lock").read_bytes()
        document = tomllib.loads(project.decode("utf-8-sig"))
        lock_document = tomllib.loads(lock.decode("utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise EnvironmentError(f"cannot read locked dependency inputs: {exc}") from exc
    uv = document.get("tool", {}).get("uv", {})
    if uv.get("package", True) is not False:
        raise EnvironmentError("immutable workspace environments require tool.uv.package = false")
    if uv.get("workspace"):
        raise EnvironmentError("mutable workspace dependencies cannot be cached as immutable environments")
    for name, source in uv.get("sources", {}).items():
        for item in source if isinstance(source, list) else [source]:
            if isinstance(item, dict) and (item.get("path") or item.get("workspace") or item.get("editable")):
                raise EnvironmentError(f"mutable source for {name!r} is unsupported; use a locked wheel or immutable Git revision")
            if isinstance(item, dict) and item.get("git") and not re.fullmatch(r"[a-fA-F0-9]{40}", item.get("rev", "")):
                raise EnvironmentError(f"Git source {name!r} must use a complete immutable commit")
    for package in lock_document.get("package", []):
        source = package.get("source", {})
        # uv represents this package=false project as virtual='.'. It is not installed.
        if source.get("editable") or source.get("directory") or source.get("path") or (source.get("virtual") and source["virtual"] != "."):
            raise EnvironmentError("uv.lock contains a mutable local dependency")
    project_fields = ("name", "version", "requires-python", "dependencies", "optional-dependencies", "dynamic")
    projection = {"project": {key: value for key, value in document.get("project", {}).items() if key in project_fields}}
    projection.update({key: document[key] for key in ("dependency-groups", "build-system") if key in document})
    projection["uv"] = uv
    if environment := document.get("tool", {}).get("vaws", {}).get("environment"):
        projection["environment"] = environment
    lock_sha = hashlib.sha256(lock).hexdigest()
    return project, lock, document, _digest({"project": projection, "lock_sha256": lock_sha}), lock_sha


def _selection(document: dict, groups=None, extras=(), options=()) -> tuple[dict, str | None, list[str]]:
    uv = document.get("tool", {}).get("uv", {})
    available_groups = set(document.get("dependency-groups", {}))
    defaults = uv.get("default-groups", ["dev"] if "dev" in available_groups else [])
    selected = set(available_groups if defaults == "all" else defaults) if groups is None else set(groups)
    available_extras = set(document.get("project", {}).get("optional-dependencies", {}))
    extra_set = set(extras)
    python = None
    transport = []
    project = True
    options = list(options)
    index = 0
    while index < len(options):
        token = options[index]
        flag, equal, value = token.partition("=")
        if flag in ("--group", "--only-group", "--no-group", "--extra", "--no-extra", "--python", "--cache-dir", "--link-mode"):
            if not equal:
                index += 1
                if index == len(options):
                    raise EnvironmentError(f"{flag} requires a value")
                value = options[index]
            if flag == "--group": selected.add(value)
            elif flag == "--only-group":
                if project: selected.clear()
                project = False
                selected.add(value)
            elif flag == "--no-group": selected.discard(value)
            elif flag == "--extra": extra_set.add(value)
            elif flag == "--no-extra": extra_set.discard(value)
            elif flag == "--python": python = value
            else: transport.extend((flag, value))
        elif equal:
            raise EnvironmentError(f"unsupported immutable sync option: {token}")
        elif flag in ("--locked", "--no-editable", "--no-install-project"):
            pass
        elif flag in ("--no-default-groups", "--no-dev"):
            selected = set() if flag == "--no-default-groups" else selected - {"dev"}
        elif flag == "--dev": selected.add("dev")
        elif flag == "--only-dev": selected, project = {"dev"}, False
        elif flag == "--all-groups": selected = available_groups.copy()
        elif flag == "--all-extras": extra_set = available_extras.copy()
        elif flag in ("--offline", "--no-cache", "--refresh", "--quiet", "--verbose", "--native-tls", "--system-certs"):
            transport.append(flag)
        else:
            raise EnvironmentError(f"unsupported immutable sync option: {token}; dependency-changing options must be represented in the environment key")
        index += 1
    if selected - available_groups or extra_set - available_extras:
        raise EnvironmentError(f"unknown dependency groups or extras: {sorted((selected - available_groups) | (extra_set - available_extras))}")
    return {"groups": sorted(selected), "extras": sorted(extra_set), "project": project}, python, transport


def _key(identity: dict, input_id: str, selection: dict) -> str:
    return _digest({"recipe_version": RECIPE_VERSION, "python_identity": identity, "input_id": input_id, "selection": selection})


def _selection_path(repo_root: Path, target_platform: str) -> Path:
    return repo_root / ".vaws-local/environment-selection" / f"{target_platform}.json"


def _access_path(value: str | Path) -> Path:
    text = str(value)
    if os.name != "nt" and re.match(r"^[a-zA-Z]:[\\/]", text):
        windows = PureWindowsPath(text)
        return Path("/mnt") / windows.drive[0].lower() / Path(*windows.parts[1:])
    return Path(value)


def read_receipt(path: str | Path, *, expected_platform: str | None = None, _allow_bundle=True) -> dict:
    actual = _access_path(path)
    try:
        value = json.loads(actual.read_text(encoding="utf-8"))
        required = ("key", "root", "python", "base_python", "platform", "arch", "abi", "python_version", "python_identity", "input_id", "lock_sha256", "selection", "store", "receipt")
        if value.get("schema_version") not in (1, 2) or value.get("recipe_version") != RECIPE_VERSION or any(name not in value for name in required):
            raise ValueError("incomplete ready receipt")
        if value["schema_version"] == 2 and not _allow_bundle:
            raise ValueError("nested capability selection")
        if expected_platform and value["platform"] != expected_platform:
            raise ValueError(f"ready receipt belongs to {value['platform']}, expected {expected_platform}")
        if value["key"] != _key(value["python_identity"], value["input_id"], value["selection"]):
            raise ValueError("ready receipt content does not match its key")
        root = _access_path(value["root"])
        manifest_root = _access_path(value["store"]) / value["key"] if value["schema_version"] == 2 else root
        if manifest_root.name != value["key"] or actual.resolve() != (manifest_root / READY_NAME).resolve():
            raise ValueError("ready receipt does not belong to its content-addressed root")
        if _access_path(value["receipt"]).resolve() != actual.resolve() or manifest_root.parent.resolve() != _access_path(value["store"]).resolve():
            raise ValueError("ready receipt paths do not agree")
        if value["schema_version"] == 2:
            component_keys = value["selection"]["components"]
            if set(component_keys) != {"runtime", "knowledge"} or set(value["components"]) != set(component_keys):
                raise ValueError("incomplete capability selection")
            for name, path in value["components"].items():
                key = component_keys[name]
                if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                    raise ValueError("invalid fixed capability key")
                expected = _access_path(value["store"]) / key / READY_NAME
                if _access_path(path).absolute() != expected.absolute():
                    raise ValueError("capability receipt path differs from its fixed selection")
            child = _read_capability(value, "runtime")
            if any(child[key] != value[key] for key in ("root", "python", "base_python")):
                raise ValueError("runtime interpreter differs from the selection")
            # Optional knowledge is fixed, but not required for core readiness.
            # Its complete input bytes are checked only if preparation is needed.
            if "frozen_inputs" in value:
                frozen = value["frozen_inputs"]
                if (not isinstance(frozen, dict) or set(frozen) != {"pyproject.toml", "uv.lock"}
                        or any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) for sha in frozen.values())):
                    raise ValueError("invalid frozen capability input descriptor")
            if "knowledge_catalog_sha256" in value:
                if ("frozen_inputs" not in value or not isinstance(value["knowledge_catalog_sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", value["knowledge_catalog_sha256"])):
                    raise ValueError("invalid frozen knowledge catalog descriptor")
        python = _access_path(value["python"])
        expected = root / ("Scripts/python.exe" if value["platform"] == "win32" else "bin/python")
        if python != expected or not python.is_file():
            raise ValueError("ready interpreter is missing or outside its environment")
        base_python = _access_path(value["base_python"])
        if not base_python.is_file() or base_python.resolve() != base_python:
            raise ValueError("ready base interpreter is missing or is a mutable alias")
        if any(value[field] != value["python_identity"][field] for field in ("platform", "arch", "abi", "python_version")):
            raise ValueError("ready Python facts are inconsistent")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EnvironmentError(f"environment is not ready: {actual}: {exc}") from exc


def _configured(repo_root: Path, target_platform: str) -> dict | None:
    path = _selection_path(repo_root, target_platform)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value["platform"] != target_platform:
            raise ValueError("selection platform does not match its filename")
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise EnvironmentError(f"invalid environment selection: {path}: {exc}") from exc


def _lookup(repo_root: Path, target_platform: str, *, require_configuration=False) -> dict:
    configuration = _configured(repo_root, target_platform)
    _, lock, document, input_id, _ = _inputs(repo_root)
    if configuration:
        identity, selection = configuration["python_identity"], configuration["selection"]
        store = _access_path(configuration["store"])
    else:
        if require_configuration:
            raise EnvironmentError("no Windows environment selection exists; run dependency sync in native Windows")
        identity = _identity()
        selection = _selection(document)[0]
        store = _store()
    from vaws_environment_capabilities import bundle_selection, enabled
    if enabled(document, selection):
        selection = bundle_selection(document, lock, selection, identity)
    return read_receipt(store / _key(identity, input_id, selection) / READY_NAME, expected_platform=target_platform)


@_diagnostic_measured('environment.select')
def native_ready(repo_root: Path, *, pin: str | Path | None = None, use_saved: bool = False) -> dict:
    """Read readiness; business entries reuse the saved session selection.

    Setup and maintenance leave use_saved false to inspect current inputs.
    Explicit process pins always take precedence.
    """
    selected = pin or os.environ.get(PIN_ENV)
    if selected:
        return read_receipt(selected, expected_platform=sys.platform)
    if use_saved:
        return saved_ready(repo_root)
    return _lookup(Path(repo_root), sys.platform)


def saved_ready(repo_root: Path, *, target_platform: str | None = None) -> dict:
    """Resume the checkout's saved environment, independent of parent pins or edits."""
    repo_root = Path(repo_root)
    target_platform = target_platform or sys.platform
    configuration = _configured(repo_root, target_platform)
    if configuration is not None:
        if "receipt" not in configuration:
            raise EnvironmentError(f"saved environment selection has no receipt: {_selection_path(repo_root, target_platform)}")
        return read_receipt(configuration["receipt"], expected_platform=target_platform)
    return _lookup(repo_root, target_platform, require_configuration=target_platform == "win32")


def windows_ready(repo_root: Path, *, use_saved: bool = False) -> dict:
    """Read native Windows facts from WSL; never synthesize Windows ABI facts."""
    if pin := os.environ.get(MANAGED_PIN_ENV):
        return read_receipt(pin, expected_platform="win32")
    if pin := os.environ.get(PIN_ENV):
        value = read_receipt(pin)
        if value["platform"] == "win32":
            return value
    if use_saved:
        return saved_ready(repo_root, target_platform="win32")
    return _lookup(Path(repo_root), "win32", require_configuration=True)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(_json(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def select_environment(repo_root: Path, receipt: dict) -> None:
    value = read_receipt(receipt["receipt"])
    _atomic_json(_selection_path(Path(repo_root), value["platform"]), value)


def _read_capability(receipt: dict, name: str) -> dict:
    child = read_receipt(receipt["components"][name], expected_platform=receipt["platform"], _allow_bundle=False)
    if child["key"] != receipt["selection"]["components"][name]:
        raise EnvironmentError("capability receipt differs from its fixed selection")
    if child["python_identity"] != receipt["python_identity"]:
        raise EnvironmentError("capability interpreter identity differs")
    return child


@_diagnostic_measured('environment.capability')
def capability_receipt(receipt: dict, capability: str = "runtime", *, prepare_missing=False, timings=None) -> dict:
    """Read a fixed owner; only explicit setup may prepare its missing child."""
    if receipt.get("schema_version") != 2:
        return receipt
    name = "knowledge" if capability == "knowledge" else "runtime"
    path = _access_path(receipt["components"][name])
    if path.exists() or name == "runtime":
        return _read_capability(receipt, name)
    if not prepare_missing:
        raise EnvironmentError("knowledge is not prepared for this fixed selection; run repo-init or "
                               "`knowledge_setup.py` explicitly, or use `vaws_deps.py sync --capability knowledge`")
    from vaws_environment_capabilities import frozen_bundle_inputs
    directory, frozen = frozen_bundle_inputs(receipt)
    from vaws_knowledge_catalog import frozen_catalog
    frozen_catalog(receipt)
    if receipt["platform"] != sys.platform:
        _prepare_windows_capability(receipt)
        return _read_capability(receipt, name)
    prepare_environment(directory, groups=receipt["selection"]["groups"], extras=receipt["selection"]["extras"],
                        python=receipt["base_python"], _component="knowledge", _frozen=frozen,
                        _store_path=_access_path(receipt["store"]),
                        _expected_key=receipt["selection"]["components"]["knowledge"], timings=timings)
    return _read_capability(receipt, name)


@_diagnostic_measured('environment.windows_capability')
def _prepare_windows_capability(receipt: dict) -> None:
    """WSL hands off only installation to this bundle's native runtime owner."""
    from vaws_local_owner import accessible_windows_path, managed_path, windows_interop_env
    if receipt["platform"] != "win32" or not os.environ.get("WSL_DISTRO_NAME"):
        raise EnvironmentError("prepare this fixed knowledge selection through its native runtime owner")
    runtime = _read_capability(receipt, "runtime")
    code = ("import sys;sys.path.insert(0,sys.argv[1]);"
            "from vaws_environment import read_receipt,capability_receipt;"
            "capability_receipt(read_receipt(sys.argv[2]),'knowledge',prepare_missing=True)")
    command = [accessible_windows_path(runtime["python"]), "-I", "-X", "utf8", "-c", code,
               managed_path(Path(__file__).resolve().parent, windows=True), receipt["receipt"]]
    environment = dict(os.environ)
    environment[PIN_ENV] = receipt["receipt"]
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", MANAGED_PIN_ENV):
        environment.pop(name, None)
    # Windows retains its own PATH; only the fixed pin crosses the OS boundary.
    forwarding = windows_interop_env({PIN_ENV: receipt["receipt"], "WSLENV": environment.get("WSLENV", "")})
    environment["WSLENV"] = forwarding["WSLENV"]
    result = subprocess.run(command, env=environment, stdout=sys.stderr, stderr=sys.stderr, check=False)
    if result.returncode:
        raise EnvironmentError(f"fixed Windows knowledge preparation failed with exit code {result.returncode}")


@contextmanager
def _key_lock(store: Path, key: str):
    locks = store / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    with (locks / (key + ".lock")).open("a+b") as handle:
        with phase("environment.lock_wait"):
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                while True:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield handle.fileno()
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


@_diagnostic_measured('environment.install')
def _install(command: list[str], environment: dict, lock_fd: int) -> None:
    from vaws_process_wait import wait_with_progress
    timeout = float(environment.get("VAWS_INSTALL_TIMEOUT", "900"))
    if not 1 <= timeout <= 86400:
        raise EnvironmentError("VAWS_INSTALL_TIMEOUT must be between 1 and 86400 seconds")
    if os.name == "nt":
        from vaws_windows import owned_process
        with owned_process(command, env=environment, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr) as process:
            code = wait_with_progress(process, stage="locked_install", timeout=timeout)
    else:
        # The child retains the lock if this process is killed. A second builder
        # cannot clean its unfinished root until that installer has exited.
        process = subprocess.Popen(command, env=environment, stdout=sys.stderr, stderr=sys.stderr,
                                   stdin=subprocess.DEVNULL, start_new_session=True, pass_fds=(lock_fd,))
        try:
            code = wait_with_progress(process, stage="locked_install", timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if code:
        raise EnvironmentError(f"locked dependency installation failed with exit code {code}")


@_diagnostic_measured('environment.prepare')
def prepare_environment(repo_root: Path, *, groups=None, extras=(), python=None, install_options=(),
                        timings: dict | None = None, _component: str | None = None, _frozen=None,
                        _store_path=None, _expected_key=None) -> dict:
    """Construct once at the final address, publish ready last, and select it."""
    repo_root = Path(repo_root)
    timings = timings if timings is not None else {}
    started = time.monotonic()
    frozen_inputs = _frozen or _inputs(repo_root)
    project, lock, document, input_id, lock_sha = frozen_inputs
    selection, selected_python, transport = _selection(document, groups, extras, install_options)
    executable, identity = _python_identity(python or selected_python)
    from vaws_environment_capabilities import bundle_selection, enabled, plans
    split = enabled(document, selection)
    exclude = []
    if _component:
        plan = plans(document, lock, selection)[_component]
        input_id, lock_sha, selection, exclude = (plan[key] for key in ("input_id", "lock_sha256", "selection", "exclude"))
    elif split:
        selection = bundle_selection(document, lock, selection, identity)
    timings["selection_seconds"] = time.monotonic() - started
    store = Path(_store_path) if _store_path is not None else _store()
    # Packaged desktop apps can virtualize LocalAppData writes. Resolve only
    # after explicit creation, so every receipt records the physical directory
    # seen by external Windows processes and WSL, not this app's virtual view.
    store.mkdir(parents=True, exist_ok=True)
    store = store.resolve(strict=True)
    key = _key(identity, input_id, selection)
    if _expected_key is not None and key != _expected_key:
        raise EnvironmentError("capability inputs or interpreter differ from the task's fixed selection")
    root = store / key
    receipt_path = root / READY_NAME
    if receipt_path.exists():
        receipt = read_receipt(receipt_path, expected_platform=sys.platform)
        if not _component:
            select_environment(repo_root, receipt)
        event("INFO", "environment.cache_hit", runtime=key)
        timings.update(reused=True, total_seconds=time.monotonic() - started)
        return receipt
    waiting = time.monotonic()
    with _key_lock(store, key) as lock_fd:
        timings["lock_wait_seconds"] = time.monotonic() - waiting
        if receipt_path.exists():
            receipt = read_receipt(receipt_path, expected_platform=sys.platform)
        elif split and not _component:
            from vaws_knowledge_catalog import RELATIVE_PATH, validate_catalog
            try:
                catalog = (repo_root / RELATIVE_PATH).read_bytes()
                validate_catalog(catalog, lock)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise EnvironmentError("cannot freeze locked knowledge catalog: " + str(exc)) from exc
            timings["components"] = {"runtime": {}}
            runtime = prepare_environment(repo_root, groups=groups, extras=extras,
                          python=executable, install_options=install_options, _component="runtime",
                          _frozen=frozen_inputs, _store_path=store,
                          _expected_key=selection["components"]["runtime"], timings=timings["components"]["runtime"])
            from vaws_environment_capabilities import save_bundle_inputs
            frozen_descriptor = save_bundle_inputs(root, project, lock, catalog)
            catalog_sha = frozen_descriptor.pop("knowledge-catalog.json")
            receipt = {**runtime, "schema_version": 2, "key": key, "input_id": input_id,
                       "lock_sha256": lock_sha, "selection": selection, "receipt": str(receipt_path),
                       "components": {name: str(store / child_key / READY_NAME)
                                      for name, child_key in selection["components"].items()},
                       "frozen_inputs": frozen_descriptor, "knowledge_catalog_sha256": catalog_sha}
            _atomic_json(receipt_path, receipt)
            receipt = read_receipt(receipt_path, expected_platform=sys.platform)
        else:
            # An unpublished failed attempt may be retried. Published roots are
            # never synced, moved, replaced or garbage-collected here.
            if root.exists():
                if root.is_symlink() or root.resolve().parent != store.resolve() or root.name != key:
                    raise EnvironmentError("unfinished environment path escapes its store")
                shutil.rmtree(root)
            with tempfile.TemporaryDirectory(prefix="vaws-locked-inputs-") as temporary:
                frozen = Path(temporary)
                from vaws_download_source import select_pypi_transport
                from vaws_network import network_scope, environment_for
                with network_scope(repo_root, target="pypi"):
                    install_lock, source_options, source_evidence = select_pypi_transport(lock, offline="--offline" in transport)
                timings["download_source"] = source_evidence
                if source_evidence.get("probe") not in {"not_configured", "no_pypi_artifacts"}:
                    print("VAWS download source: " + json.dumps(source_evidence), file=sys.stderr, flush=True)
                (frozen / "pyproject.toml").write_bytes(project)
                (frozen / "uv.lock").write_bytes(install_lock)
                command = ["uv", "sync", "--project", str(frozen), "--locked", "--no-editable", "--no-install-project",
                           "--python", executable, "--no-default-groups"]
                for group in selection["groups"]:
                    command.extend(("--group" if selection["project"] else "--only-group", group))
                for extra in selection["extras"]:
                    command.extend(("--extra", extra))
                for name in exclude:
                    command.extend(("--no-install-package", name))
                command.extend(transport)
                command.extend(source_options)
                environment = {name: value for name, value in environment_for(repo_root, target="pypi").items()
                               if (not name.startswith("UV_") or name in UV_TRANSPORT_ENV)
                               and name not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", PIN_ENV)}
                environment["UV_PROJECT_ENVIRONMENT"] = str(root)
                installing = time.monotonic()
                _install(command, context_environment(environment), lock_fd)
                timings["install_seconds"] = time.monotonic() - installing
            environment_python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            verifying = time.monotonic()
            with phase("environment.verify_interpreter"):
                verification = subprocess.run([str(environment_python), "-I", "-X", "utf8", "-c",
                                              "import importlib.metadata,sys,json;list(importlib.metadata.distributions());print(json.dumps({'prefix':sys.prefix,'base':sys._base_executable}))"],
                                             stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", check=False, timeout=30)
                facts = json.loads(verification.stdout) if verification.returncode == 0 else {}
                if (verification.returncode or Path(facts["prefix"]).resolve() != root.resolve()
                        or Path(facts["base"]).resolve() != Path(executable)):
                    raise EnvironmentError("installed interpreter failed its permanent-path verification")
            timings["verification_seconds"] = time.monotonic() - verifying
            receipt = {"schema_version": 1, "recipe_version": RECIPE_VERSION, "key": key, "root": str(root), "python": str(environment_python),
                       "platform": identity["platform"], "arch": identity["arch"], "abi": identity["abi"],
                       "python_version": identity["python_version"], "python_identity": identity, "input_id": input_id,
                       "lock_sha256": lock_sha, "selection": selection, "store": str(store), "receipt": str(receipt_path),
                       "base_python": executable}
            _atomic_json(receipt_path, receipt)
            receipt = read_receipt(receipt_path, expected_platform=sys.platform)
    if not _component:
        select_environment(repo_root, receipt)
    timings.update(reused="install_seconds" not in timings and "components" not in timings,
                   total_seconds=time.monotonic() - started)
    return receipt

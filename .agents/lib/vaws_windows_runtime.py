"""Apply an explicit, workspace-owned Windows C++ runtime selection."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

_loaded: dict[str, tuple[object, object]] = {}


def configure_windows_runtime(repo_root: Path) -> None:
    """Keep a selected MSVC runtime available to this process and its children.

    No machine-wide DLLs or immutable Python environments are modified. An
    administrator-installed runtime directory is selected explicitly during
    repair, never discovered from cwd or an untrusted DLL search path.
    """
    if os.name != "nt":
        return
    # Ordinary checkouts without a selection need no Git subprocess on startup.
    owner = repo_root
    prepared = (repo_root / ".vaws-local/native-workspace.json").is_file()
    if prepared or (repo_root / ".git").is_file():
        from vaws_local_state import shared_workspace_root
        owner = shared_workspace_root(repo_root)
    config = owner / ".vaws-local/windows-runtime.json"
    if not config.is_file():
        return
    payload = json.loads(config.read_text(encoding="utf-8-sig"))
    value = payload.get("msvc_directory") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError("windows-runtime.json must specify an absolute msvc_directory")
    path = Path(value)
    if not path.is_absolute() or not (path / "msvcp140.dll").is_file():
        raise ValueError("windows-runtime.json must select an installed MSVC directory containing msvcp140.dll")
    path = path.resolve(strict=True)
    key = str(path)
    if key in _loaded:
        return
    # Python extension imports use restricted DLL lookup. Preload this exact
    # library and retain the directory handle for its sibling dependencies.
    directory = os.add_dll_directory(key)
    try:
        library = ctypes.WinDLL(str(path / "msvcp140.dll"))
        # Unlike AddDllDirectory, this setting is inherited by child processes,
        # including venv redirectors and the package's detached local daemons.
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
        kernel.SetDllDirectoryW.restype = wintypes.BOOL
        if not kernel.SetDllDirectoryW(key):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        directory.close()
        raise
    _loaded[key] = directory, library

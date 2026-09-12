"""Selected-runtime boundaries for configuration planning tests.

These tests exercise generated configuration, not environment construction.
Real construction, pinning and process execution have dedicated tests.
"""
from pathlib import Path
import shutil
import sys

import vaws_knowledge_service
import vaws_kimi_config


def selected_runtime(monkeypatch, setup, tmp_path):
    receipt = {"key": "d" * 64, "platform": sys.platform,
               "python": sys.executable, "root": sys.prefix,
               "receipt": str(tmp_path / "selected-ready.json")}
    monkeypatch.setattr(setup, "native_ready", lambda root: receipt)
    monkeypatch.setattr(setup, "windows_ready", lambda root: receipt)
    monkeypatch.setattr(setup, "managed_receipt", lambda root: receipt)
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    monkeypatch.setattr(setup, "windows_mounted_workspace", lambda root: False)
    monkeypatch.setattr(vaws_knowledge_service, "managed_receipt", lambda root: receipt)
    monkeypatch.setattr(vaws_knowledge_service, "managed_python", lambda root: sys.executable)
    monkeypatch.setattr(vaws_knowledge_service, "windows_mounted_workspace", lambda root: False)
    monkeypatch.setattr(vaws_kimi_config, "managed_receipt", lambda root: receipt)
    return receipt


def native_task_entry(source: Path, root: Path, *, prepared: bool) -> Path:
    """Copy the real task CLI into the test's native temporary filesystem.

    A test of native local task behavior must not accidentally enter the Windows
    owner just because its repository was checked out on a WSL mounted drive.
    The production path detection and process boundary remain unmodified.
    """
    root.mkdir()
    shutil.copytree(source / '.agents/lib', root / '.agents/lib',
                    ignore=shutil.ignore_patterns('__pycache__'))
    entry = root / '.agents/scripts/vaws.py'
    entry.parent.mkdir()
    shutil.copyfile(source / '.agents/scripts/vaws.py', entry)
    for name in ('pyproject.toml', 'uv.lock'):
        shutil.copyfile(source / name, root / name)
    if prepared:
        from vaws_environment import native_ready, select_environment

        select_environment(root, native_ready(source))
    return entry

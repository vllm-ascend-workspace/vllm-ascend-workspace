"""Explicit runtime selection reaches real Windows child processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import vaws_windows_runtime as runtime

LIB = Path(__file__).resolve().parents[1] / "lib"


def test_missing_selection_does_not_load_dlls(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt"))
    runtime.configure_windows_runtime(tmp_path)


@pytest.mark.parametrize("value", ["relative", "/nonexistent-vaws-msvc-directory"])
def test_invalid_selection_has_actionable_error(tmp_path, monkeypatch, value):
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt"))
    config = tmp_path / ".vaws-local/windows-runtime.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"msvc_directory": value}), encoding="utf-8")
    with pytest.raises(ValueError, match="installed MSVC directory"):
        runtime.configure_windows_runtime(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows loader inheritance")
def test_selected_directory_is_used_by_fresh_child(tmp_path):
    system = Path(os.environ["SystemRoot"]) / "System32"
    if not (system / "msvcp140.dll").is_file():
        pytest.skip("Microsoft C++ runtime is not installed")
    selected = tmp_path / "selected runtime"
    selected.mkdir()
    for source in system.glob("msvcp140*.dll"):
        shutil.copy2(source, selected / source.name)
    config = tmp_path / ".vaws-local/windows-runtime.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"msvc_directory": str(selected)}), encoding="utf-8")
    child = """import ctypes
from ctypes import wintypes
library = ctypes.WinDLL('msvcp140.dll')
kernel = ctypes.WinDLL('kernel32')
kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
name = ctypes.create_unicode_buffer(32768)
assert kernel.GetModuleFileNameW(library._handle, name, len(name))
print(name.value)
"""
    parent = f"""import sys, subprocess
from pathlib import Path
sys.path.insert(0, {str(LIB)!r})
from vaws_windows_runtime import configure_windows_runtime
configure_windows_runtime(Path({str(tmp_path)!r}))
subprocess.run([sys.executable, '-I', '-c', {child!r}], check=True)
"""
    result = subprocess.run([sys.executable, "-I", "-c", parent], capture_output=True,
                            text=True, encoding="utf-8", timeout=30, check=True)
    assert Path(result.stdout.strip()).resolve() == (selected / "msvcp140.dll").resolve()

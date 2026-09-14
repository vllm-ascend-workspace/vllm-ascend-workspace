#!/usr/bin/env python3
"""Install GitHub CLI into a user-writable prefix without sudo/admin.

Supported platforms:
  - macOS
  - Linux
  - Windows with an existing Python; the PowerShell fallback needs no Python.
"""

from __future__ import annotations

# Observe the real CLI before optional runtime imports; copied remote helpers stay standalone.
if __name__ == "__main__":
    import sys as _vaws_sys
    from pathlib import Path as _VawsPath
    _vaws_parents = _VawsPath(__file__).absolute().parents
    _vaws_lib = _vaws_parents[3] / "lib" if len(_vaws_parents) > 3 else None
    _vaws_entry = None
    if _vaws_lib is not None and (_vaws_lib / "vaws_diagnostics_adapter.py").is_file():
        _vaws_sys.path.insert(0, str(_vaws_lib))
        from vaws_diagnostics_adapter import bootstrap as _vaws_bootstrap
        _vaws_entry = _vaws_bootstrap(__file__)

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".agents/lib"))

import platform
import re
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile
from typing import NoReturn


API_URL = "https://api.github.com/repos/cli/cli/releases/latest"


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def detect_target() -> tuple[str, str, str]:
    system = platform.system()
    machine = platform.machine().lower()

    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    arch = arch_map.get(machine)
    if not arch:
        fail(f"unsupported architecture: {machine}")

    if system == "Darwin":
        return "macOS", arch, "zip"
    if system == "Linux":
        return "linux", arch, "tar.gz"
    if system == "Windows":
        return "windows", arch, "zip"

    fail("this fallback installer supports only macOS and Linux")


def latest_release() -> dict:
    from vaws_network import fetch_bytes
    return json.loads(fetch_bytes(API_URL, deadline=30))


def select_asset(release: dict, os_token: str, arch: str, ext: str) -> dict:
    pattern = re.compile(rf"^gh_(?P<version>[^_]+)_{re.escape(os_token)}_{re.escape(arch)}\.{re.escape(ext)}$")
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if pattern.match(name):
            return asset
    fail(f"could not find a matching release asset for {os_token}/{arch}.{ext}")


def ensure_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def install_zip(archive_path: pathlib.Path, target_bin: pathlib.Path) -> None:
    with zipfile.ZipFile(archive_path) as zf:
        members = [name for name in zf.namelist() if name.endswith(("/bin/gh", "/bin/gh.exe")) or name == "bin/gh.exe"]
        if not members:
            fail("zip archive does not contain bin/gh")
        member = members[0]
        with zf.open(member) as src, open(target_bin, "wb") as dst:
            shutil.copyfileobj(src, dst)


def install_tar(archive_path: pathlib.Path, target_bin: pathlib.Path) -> None:
    with tarfile.open(archive_path, "r:gz") as tf:
        members = [m for m in tf.getmembers() if m.name.endswith("/bin/gh")]
        if not members:
            fail("tar archive does not contain bin/gh")
        member = members[0]
        src = tf.extractfile(member)
        if src is None:
            fail("failed to read gh binary from tar archive")
        with src, open(target_bin, "wb") as dst:
            shutil.copyfileobj(src, dst)


def install() -> None:
    os_token, arch, ext = detect_target()
    release = latest_release()
    asset = select_asset(release, os_token, arch, ext)
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", release["tag_name"]):
        fail("unexpected release tag")
    from vaws_network import fetch_bytes, owner
    from vaws_public_download import download
    checksum_asset = next((item for item in release["assets"] if item["name"].endswith("_checksums.txt")), None)
    if not checksum_asset:
        fail("release has no published checksums")
    checksums = fetch_bytes(checksum_asset["browser_download_url"], deadline=30).decode("ascii")
    expected = next((parts[0] for line in checksums.splitlines() if len(parts := line.split()) == 2 and parts[1].lstrip("*") == asset["name"]), None)
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        fail("release checksum does not cover the selected asset")

    home = pathlib.Path.home()
    install_root = home / ".local" / "gh" / release["tag_name"]
    install_bin_dir = install_root / "bin"
    link_bin_dir = home / ".local" / "bin"
    if os_token == "windows":
        install_root = pathlib.Path(os.environ["LOCALAPPDATA"]) / "Programs/GitHubCLI" / release["tag_name"]
        install_bin_dir = install_root / "bin"
        link_bin_dir = install_root.parent / "current"
    ensure_dir(install_bin_dir)
    ensure_dir(link_bin_dir)

    binary = "gh.exe" if os_token == "windows" else "gh"
    target_bin = install_bin_dir / binary
    link_bin = link_bin_dir / binary

    archive_path = owner(ROOT) / ".vaws-local/downloads" / expected / asset["name"]
    print(f"Downloading {asset['name']} ...")
    print(json.dumps(download(asset["browser_download_url"], archive_path, expected)))

    if ext == "zip":
        install_zip(archive_path, target_bin)
    else:
        install_tar(archive_path, target_bin)

    mode = target_bin.stat().st_mode
    target_bin.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if link_bin.exists() or link_bin.is_symlink():
        link_bin.unlink()
    try:
        if os_token == "windows":
            shutil.copy2(target_bin, link_bin)
        else:
            link_bin.symlink_to(target_bin)
    except OSError:
        shutil.copy2(target_bin, link_bin)

    print(f"Installed gh to {target_bin}")
    print(f"User-facing command path: {link_bin}")

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if str(link_bin_dir) not in path_entries:
        print("")
        print("Add this directory to PATH if needed:")
        if os_token == "windows":
            print(f"  Add {link_bin_dir} to your user PATH in Windows settings.")
        else:
            print(f"  export PATH=\"{link_bin_dir}:$PATH\"")

    print("")
    print("Verify with:")
    print("  gh --version")
    print("  gh auth status --hostname github.com")


def main() -> None:
    from vaws_network import network_scope
    with network_scope(ROOT):
        install()


if __name__ == "__main__":
    (_vaws_entry.run(main) if _vaws_entry else main())

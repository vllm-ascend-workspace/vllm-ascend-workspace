#!/usr/bin/env python3
"""uv.lock is the only pin: pyproject / lock / installed interpreter."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_dependency as deps  # noqa: E402


class SpecLockTests(unittest.TestCase):
    def test_pyproject_requires_the_three_packages(self) -> None:
        versions = deps.required_versions()
        self.assertEqual(set(versions), {"vaws-remote-dev", "vaws-coordinator", "vaws-knowledge", "pillow", "mcp"})
        self.assertNotIn(deps.VAWS_TOP_NAME, versions)

    def test_status_tracks_only_the_three_packages(self) -> None:
        self.assertEqual(deps.KNOWN_NAMES, deps.PACKAGE_NAMES)
        self.assertNotIn(deps.VAWS_TOP_NAME, deps.all_packages())

    def test_lock_records_the_pinned_commits(self) -> None:
        locked = deps.locked_packages()
        sources = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]["sources"]
        for name in deps.PACKAGE_NAMES:
            self.assertEqual(locked[name]["commit"], sources[name]["rev"], name)
        expected_versions = deps.required_versions()
        repositories = {
            "vaws-remote-dev": {"https://github.com/vllm-ascend-workspace/remote-dev"},
            "vaws-coordinator": {
                "https://github.com/vllm-ascend-workspace/vaws-coordinator",
                "https://github.com/maoxx241/vaws-coordinator",
            },
            "vaws-knowledge": {"https://github.com/vllm-ascend-workspace/vaws-knowledge"},
        }
        for name in deps.PACKAGE_NAMES:
            self.assertEqual(locked[name]["version"], expected_versions[name], name)
            self.assertIn(sources[name]["git"], repositories[name], name)
            self.assertEqual((locked[name]["url"] or "").split("?")[0], sources[name]["git"], name)

    def test_inspect_ready_when_installed_matches_lock(self) -> None:
        installed = {name: deps.installed_spec(name) for name in deps.PACKAGE_NAMES}
        if any(item is None for item in installed.values()):
            self.skipTest("workspace packages are not installed in this interpreter")
        for name in deps.PACKAGE_NAMES:
            info = deps.inspect(name)
            self.assertEqual(info["name"], name)
            self.assertIn(info["state"], deps.STATES)
            if info["state"] == "ready":
                self.assertEqual(info["installed_version"], deps.locked_packages()[name]["version"])
                self.assertEqual(info["installed_commit"], deps.locked_packages()[name]["commit"])
                self.assertEqual(info["remedy"], "uv run --no-project python .agents/scripts/vaws_deps.py sync")

    def test_missing_pyproject_is_missing_not_an_execution_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info = deps.inspect("vaws-coordinator", repo_root=Path(tmp))
            self.assertEqual(info["state"], "missing")
            self.assertTrue(info["problems"])
            self.assertEqual(info["remedy"], "uv run --no-project python .agents/scripts/vaws_deps.py sync")

    def test_status_exit_code_is_nonzero_unless_ready(self) -> None:
        self.assertEqual(deps.status_exit_code({"vaws-coordinator": "ready"}), 0)
        self.assertEqual(deps.status_exit_code({"vaws-coordinator": "off_spec"}), 1)
        self.assertEqual(deps.status_exit_code({"vaws-coordinator": "missing"}), 1)

    def test_status_cli_prints_one_json_object(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws_deps.py"), "status", "vaws-coordinator"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["name"], "vaws-coordinator")
        self.assertIn(payload["state"], deps.STATES)
        self.assertIn("inspecting", proc.stderr)

    def test_unknown_name_is_exit_2(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws_deps.py"), "status", "not-a-dep"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout)
        self.assertIn("unknown", payload["error"])


class BuildInputOwnershipTests(unittest.TestCase):
    def test_build_inputs_live_in_the_coordinator_package(self) -> None:
        self.assertFalse((ROOT / ".agents/lib/vaws_build_inputs.py").is_file())
        try:
            import vaws_coordinator.build_inputs as packaged
        except ImportError:
            self.skipTest("vaws-coordinator is not installed")
        self.assertTrue(hasattr(packaged, "build_input_fingerprints"))
        self.assertTrue(hasattr(packaged, "runtime_build_inputs"))


if __name__ == "__main__":
    unittest.main()

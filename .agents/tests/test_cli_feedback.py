"""Parser feedback may read public API docs but must not access execution state."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/scripts/vaws.py"


class CliFeedbackTests(unittest.TestCase):
    def test_help_and_root_argument_errors_need_no_state_or_subprocess(self):
        # The package help imports TaskClient to reuse its public docstring.
        # Importing that facade is allowed; backend imports and actual state or
        # remote operations are not. sitecustomize survives both launcher paths.
        guard = '''
import importlib.abc, sys
class NoExecutionImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"vaws_local_state", "vaws_coordinator.service", "vaws_coordinator.backend", "remote_dev.core.ssh_transport"}:
            raise AssertionError("parser imported execution state: " + fullname)
sys.meta_path.insert(0, NoExecutionImports())
def audit(event, args):
    if event in {"socket.connect", "subprocess.Popen", "os.system", "sqlite3.connect"}:
        raise AssertionError("parser attempted side effect: " + event)
sys.addaudithook(audit)
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sitecustomize.py").write_text(guard, encoding="utf-8")
            home = root / "home"
            home.mkdir()
            env = os.environ.copy()
            env.update(PYTHONPATH=str(root), HOME=str(home), USERPROFILE=str(home))
            cases = [
                (["--help"], 0),
                (["status", "--help"], 0),
                (["env", "--help"], 0),
                (["attach", "--help"], 0),
                (["session", "--help"], 0),
                (["run", "--help"], 0),
                (["execution", "--help"], 0),
                (["finish", "--help"], 0),
                (["--unknown-option"], 2),
            ]
            for argv, expected in cases:
                with self.subTest(argv=argv):
                    result = subprocess.run(
                        [sys.executable, "-X", "utf8", str(SCRIPT), *argv], cwd=ROOT, env=env,
                        capture_output=True, encoding="utf-8", timeout=15,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertIn("usage:", result.stdout + result.stderr)
                    self.assertNotIn("AssertionError", result.stdout + result.stderr)
            self.assertEqual(list(home.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

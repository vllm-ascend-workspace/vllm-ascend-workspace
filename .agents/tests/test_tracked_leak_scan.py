#!/usr/bin/env python3
"""Regression tests for the tracked-file leak guard.

Everything here runs on a laptop: temporary Git repositories, text fixtures,
and the repository's own tracked tree. No SSH, no NPU, no torch.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import vaws_leak_guard as guard  # noqa: E402

FIXTURE_DIR = ROOT / ".agents" / "tests" / "fixtures" / "tracked_leak_guard"
POLICY_PATH = ROOT / ".agents" / "leak-guard" / "allowlist.yaml"
SCANNER = ROOT / ".agents" / "scripts" / "tracked_leak_scan.py"
HOOK = ROOT / ".agents" / "hooks" / "tracked_leak_precommit.py"
MINIMAL_POLICY = "schema_version: 1\n"
SYNTHETIC_IPV4 = "192.168.240.7"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def scan_fixture(name: str, policy: guard.Policy | None = None) -> list[guard.Finding]:
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    return guard.scan_document(text, path=f"fixture/{name}", policy=policy or guard.default_policy())


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def init_repo(repo: Path) -> None:
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")


def write_minimal_policy(repo: Path, body: str = MINIMAL_POLICY) -> Path:
    path = repo / ".agents" / "leak-guard" / "allowlist.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def invoke_cli(script: Path, *args: str, cwd: Path | None = None) -> tuple[int, dict, str]:
    env = os.environ.copy()
    if os.name == "nt":
        # English Windows must also report paths outside its ANSI code page.
        env["PYTHONIOENCODING"] = "cp1252"
    result = subprocess.run(
        [sys.executable, "-B", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(cwd or ROOT),
    )
    payload = json.loads(result.stdout.strip())
    return result.returncode, payload, result.stderr


class DetectionTests(unittest.TestCase):
    """The dirty fixture must produce at least one finding per category."""

    def test_every_category_is_detected(self) -> None:
        findings = scan_fixture("dirty.txt")
        found = {finding.category for finding in findings}
        for category in guard.CATEGORIES:
            with self.subTest(category=category):
                self.assertIn(category, found)

    def test_missing_knowledge_package_refuses_to_scan(self) -> None:
        saved = guard._knowledge_redact
        guard._knowledge_redact = None
        self.addCleanup(setattr, guard, "_knowledge_redact", saved)
        with mock.patch.dict(
            sys.modules, {"vaws_knowledge": None, "vaws_knowledge.redact": None}
        ):
            with self.assertRaises(guard.LeakGuardError) as caught:
                guard.require_knowledge_redact()
        message = str(caught.exception)
        self.assertIn("vaws_knowledge", message)
        self.assertIn("python .agents/scripts/vaws_deps.py sync", message)
        with mock.patch.dict(
            sys.modules, {"vaws_knowledge": None, "vaws_knowledge.redact": None}
        ):
            with self.assertRaises(guard.LeakGuardError):
                guard.scan_line("token = hunter2", guard.default_policy())
            with self.assertRaises(guard.LeakGuardError):
                guard.scan_diff("", guard.default_policy())

    def test_clean_fixture_produces_no_findings(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        # Reuse the repository policy's allowed prefixes without its
        # fixture exclusion, so allowed values are proven allowed by policy.
        policy.scoped_exclusions = ()
        findings = scan_fixture("clean.txt", policy)
        self.assertEqual(
            [guard.format_finding_line(item, show_matches=True) for item in findings], []
        )

    def test_documentation_and_loopback_ranges_are_allowed(self) -> None:
        policy = guard.default_policy()
        for value in ("127.0.0.1", "192.0.2.10", "198.51.100.4", "203.0.113.9", "0.0.0.0"):
            with self.subTest(value=value):
                self.assertEqual(guard.scan_line(f"host = {value}", policy), [])
        for value in ("::1", "2001:db8::1"):
            with self.subTest(value=value):
                self.assertEqual(guard.scan_line(f"host = {value}", policy), [])

    def test_private_and_public_addresses_are_reported(self) -> None:
        policy = guard.default_policy()
        for value in ("192.168.240.7", "10.11.12.13", "100.64.0.1", "fd00:beef::12"):
            with self.subTest(value=value):
                spans = guard.scan_line(f"host = {value}", policy)
                self.assertEqual([span[2] for span in spans], ["ipv4" if "." in value else "ipv6"])

    def test_named_paths_are_reported_and_shared_mounts_are_not(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        for value in ("/Users/janedoe/x", "/home/jsmith/x", "/root/jsmith/x"):
            with self.subTest(value=value):
                spans = guard.scan_line(value, policy)
                self.assertEqual([span[2] for span in spans], ["absolute-user-path"])
        for value in (
            "/vllm-workspace/.vaws-runtime",
            "/home/weights/Qwen3-32B",
            "/root/Qwen3-32B",
            "/root/.cache/hub",
            "/Users/Shared/x",
        ):
            with self.subTest(value=value):
                self.assertEqual(guard.scan_line(value, policy), [])

    def test_scanned_roots_are_policy_not_code(self) -> None:
        policy = guard.default_policy()
        self.assertEqual(guard.scan_line("/vllm-workspace/model", policy), [])
        policy.user_path_re = guard.build_user_path_re(("Users", "home", "root", "vllm-workspace"))
        spans = guard.scan_line("/vllm-workspace/model", policy)
        self.assertEqual([span[2] for span in spans], ["absolute-user-path"])

    def test_credential_shaped_values_extend_knowledge_patterns(self) -> None:
        policy = guard.default_policy()
        for line in (
            'api_key = "Zt7Qw2Lm9Rk4Xy1B"',
            "password: 8Hj2kLm9Qw4Z",
            "AWS key AKIAIOSFODNN7EXAMPLE",
            "Authorization: Bearer aaaabbbbccccdddd1234",
        ):
            with self.subTest(line=line):
                spans = guard.scan_line(line, policy)
                self.assertTrue(
                    any(span[2] in {"secret-key", "secret-value"} for span in spans), spans
                )

    def test_code_shaped_assignments_are_not_credentials(self) -> None:
        policy = guard.default_policy()
        for line in (
            "token = invocation_id",
            "password=password",
            'token_env = "VAWS_COORDINATOR_TOKEN"',
            'token_file = "/private/path/to/token"',
            'bootstrap = "password-once"',
            'password = "<your-password>"',
            "secret = os.environ.get(name)",
        ):
            with self.subTest(line=line):
                self.assertEqual(guard.scan_line(line, policy), [])

    def test_hostname_rule_ignores_python_attribute_access(self) -> None:
        policy = guard.default_policy()
        self.assertEqual(guard.scan_line("value = spec.local", policy), [])
        self.assertEqual(guard.scan_line("if args.local_path:", policy), [])
        for line in ('host = "registry.local"', "Host node.internal:22", "http://npu-01.corp/"):
            with self.subTest(line=line):
                spans = guard.scan_line(line, policy)
                self.assertEqual([span[2] for span in spans], ["internal-hostname"])
        # An ssh target reads as a mailbox; either category blocks the commit.
        spans = guard.scan_line("ssh root@node.internal", policy)
        self.assertEqual([span[2] for span in spans], ["email"])

    def test_identity_token_rule_ignores_short_git_shas(self) -> None:
        policy = guard.default_policy()
        self.assertEqual(guard.scan_line('"commit:c015161"', policy), [])
        spans = guard.scan_line("owner q12345678 wrote this", policy)
        self.assertEqual([span[2] for span in spans], ["internal-identifier"])

    def test_overlapping_rules_yield_one_finding(self) -> None:
        policy = guard.default_policy()
        spans = guard.scan_line("/home/q12345678/work", policy)
        self.assertEqual([span[2] for span in spans], ["absolute-user-path"])

    def test_preview_redacts_the_matched_value(self) -> None:
        finding = guard.Finding("a.md", 1, 1, "ipv4", "ipv4-address", "192.168.240.7")
        self.assertNotIn("240", finding.preview)
        self.assertIn("192", finding.preview)


class PolicyTests(unittest.TestCase):
    def test_repository_policy_loads_and_requires_justifications(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        self.assertTrue(policy.entries)
        self.assertTrue(policy.scoped_exclusions)
        for entry in policy.entries:
            with self.subTest(entry=entry.id):
                self.assertGreaterEqual(
                    len(entry.justification), guard.MIN_JUSTIFICATION_CHARS
                )
        for exclusion in policy.scoped_exclusions:
            with self.subTest(exclusion=exclusion.id):
                self.assertGreaterEqual(
                    len(exclusion.justification), guard.MIN_JUSTIFICATION_CHARS
                )

    def test_fixtures_are_excluded_by_scope_not_by_weakened_patterns(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        relative = (FIXTURE_DIR / "dirty.txt").relative_to(ROOT).as_posix()
        self.assertTrue(any(item.covers(relative) for item in policy.scoped_exclusions))
        # The same content outside the fixture directory is still reported.
        text = (FIXTURE_DIR / "dirty.txt").read_text(encoding="utf-8")
        self.assertTrue(guard.scan_document(text, path="docs/elsewhere.md", policy=policy))
        self.assertEqual(guard.scan_document(text, path=relative, policy=policy), [])

    def _write_policy(self, directory: Path, body: str) -> Path:
        path = directory / "allowlist.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_entry_without_justification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: no-reason\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv4]\n",
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "justification"):
                guard.load_policy(path)

    def test_short_justification_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: terse\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv4]\n"
                "    justification: fine\n",
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "at least"):
                guard.load_policy(path)

    def test_unknown_category_and_duplicate_id_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: bad-category\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv7]\n"
                "    justification: this justification is long enough to pass\n",
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "unknown category"):
                guard.load_policy(path)
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: twice\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv4]\n"
                "    justification: this justification is long enough to pass\n"
                "  - id: twice\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv6]\n"
                "    justification: this justification is long enough to pass\n",
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "duplicate"):
                guard.load_policy(path)

    def test_unknown_keys_and_schema_version_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(Path(tmp), "schema_version: 2\n")
            with self.assertRaisesRegex(guard.LeakGuardError, "schema_version"):
                guard.load_policy(path)
            path = self._write_policy(
                Path(tmp), "schema_version: 1\nallow_everything: true\n"
            )
            with self.assertRaisesRegex(guard.LeakGuardError, "unsupported keys"):
                guard.load_policy(path)

    def test_missing_policy_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(guard.LeakGuardError, "not found"):
                guard.load_policy(Path(tmp) / "absent.yaml")

    def test_schema_version_requires_an_actual_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for body in (
                "schema_version: true\n",
                "schema_version: false\n",
                "schema_version: 0\n",
                "schema_version: '1'\n",
                "schema_version: 1.0\n",
            ):
                path = self._write_policy(directory, body)
                with self.subTest(body=body.strip()):
                    with self.assertRaisesRegex(guard.LeakGuardError, "schema_version"):
                        guard.load_policy(path)
            policy = guard.load_policy(self._write_policy(directory, "schema_version: 1\n"))
            self.assertEqual(policy.source, directory / "allowlist.yaml")

    def test_max_file_bytes_requires_a_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for setting in ("true", "false", "0", "-1", "'64'", "1.5"):
                body = "schema_version: 1\nsettings:\n  max_file_bytes: " + setting + "\n"
                path = self._write_policy(directory, body)
                with self.subTest(setting=setting):
                    with self.assertRaisesRegex(guard.LeakGuardError, "max_file_bytes"):
                        guard.load_policy(path)
            policy = guard.load_policy(
                self._write_policy(
                    directory,
                    "schema_version: 1\nsettings:\n  max_file_bytes: 64\n",
                )
            )
            self.assertEqual(policy.max_file_bytes, 64)

    def test_allowlist_entry_scopes_to_path_category_and_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_policy(
                Path(tmp),
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: docs-only\n"
                "    path_glob: docs/**\n"
                "    categories: [ipv4]\n"
                "    match: 192.168.240.7\n"
                "    justification: reserved private example used by the guard test suite\n",
            )
            policy = guard.load_policy(path)
            line = "host 192.168.240.7"
            allowed = guard.scan_document(line, path="docs/a.md", policy=policy)
            self.assertEqual([item.allowlisted_by for item in allowed], ["docs-only"])
            elsewhere = guard.scan_document(line, path="src/a.py", policy=policy)
            self.assertEqual([item.allowlisted_by for item in elsewhere], [None])
            other_value = guard.scan_document("host 192.168.240.8", path="docs/a.md", policy=policy)
            self.assertEqual([item.allowlisted_by for item in other_value], [None])

    def test_malformed_yaml_is_a_policy_error(self) -> None:
        with self.assertRaisesRegex(guard.LeakGuardError, "invalid YAML policy"):
            guard.load_yaml_mapping("settings: [unfinished")


class DiffModeTests(unittest.TestCase):
    def test_staged_diff_reports_added_lines_with_post_image_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            target = repo / "notes.md"
            target.write_text("line one\nline two\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "base")
            target.write_text("line one\nline two\nhost 192.168.240.7\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            result = guard.scan_diff(guard.staged_diff(repo), guard.default_policy())
            self.assertEqual(len(result.findings), 1)
            finding = result.findings[0]
            self.assertEqual((finding.path, finding.line, finding.category), ("notes.md", 3, "ipv4"))

    def test_removing_a_leak_is_not_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            target = repo / "notes.md"
            target.write_text("host 192.168.240.7\nkeep\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "base")
            target.write_text("keep\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            result = guard.scan_diff(guard.staged_diff(repo), guard.default_policy())
            self.assertEqual(result.findings, [])

    def test_commit_range_mode_scans_added_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            target = repo / "notes.md"
            target.write_text("clean\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD").strip()
            target.write_text("clean\nmac 02:1a:2b:3c:4d:5e\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "leak")
            result = guard.scan_diff(guard.range_diff(repo, f"{base}..HEAD"), guard.default_policy())
            self.assertEqual([item.category for item in result.findings], ["mac-address"])

    def test_unsafe_commit_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(guard.LeakGuardError, "unsafe commit range"):
            guard.range_diff(ROOT, "main; rm -rf /")

    def test_added_line_starting_with_two_pluses_is_scanned(self) -> None:
        diff = (
            "diff --git a/notes.md b/notes.md\n"
            "--- a/notes.md\n"
            "+++ b/notes.md\n"
            "@@ -0,0 +1,2 @@\n"
            "+safe\n"
            "+++ host " + SYNTHETIC_IPV4 + "\n"
        )
        result = guard.scan_diff(diff, guard.default_policy())
        self.assertEqual(result.scanned, 1)
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual((finding.path, finding.line, finding.category), ("notes.md", 2, "ipv4"))
        self.assertEqual(finding.column, 9)
        self.assertNotIn("240", finding.preview)

    def test_quoted_diff_path_preserves_tab_and_utf8(self) -> None:
        diff = (
            'diff --git "a/notes\\tfile.md" "b/notes\\tfile.md"\n'
            "new file mode 100644\n"
            "--- /dev/null\n"
            '+++ "b/notes\\tfile.md"\n'
            "@@ -0,0 +1 @@\n"
            "+host " + SYNTHETIC_IPV4 + "\n"
        )
        result = guard.scan_diff(diff, guard.default_policy())
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.findings[0].path, "notes\tfile.md")
        utf_diff = (
            'diff --git "a/\\346\\226\\207\\344\\273\\266.md" '
            '"b/\\346\\226\\207\\344\\273\\266.md"\n'
            "--- /dev/null\n"
            '+++ "b/\\346\\226\\207\\344\\273\\266.md"\n'
            "@@ -0,0 +1 @@\n"
            "+host " + SYNTHETIC_IPV4 + "\n"
        )
        utf_result = guard.scan_diff(utf_diff, guard.default_policy())
        self.assertEqual(utf_result.findings[0].path, "文件.md")

    def test_malformed_plus_header_does_not_inherit_previous_path(self) -> None:
        diff = (
            "diff --git a/one.md b/one.md\n"
            "--- a/one.md\n"
            "+++ b/one.md\n"
            "@@ -0,0 +1 @@\n"
            "+safe\n"
            "diff --git a/two.md b/two.md\n"
            "--- a/two.md\n"
            "+++ not-a-git-prefix\n"
            "@@ -0,0 +1 @@\n"
            "+host " + SYNTHETIC_IPV4 + "\n"
        )
        with self.assertRaisesRegex(guard.LeakGuardError, "diff path header"):
            guard.scan_diff(diff, guard.default_policy())

    def test_hunk_without_path_header_fails_closed(self) -> None:
        diff = (
            "diff --git a/two.md b/two.md\n"
            "@@ -0,0 +1 @@\n"
            "+host " + SYNTHETIC_IPV4 + "\n"
        )
        with self.assertRaisesRegex(guard.LeakGuardError, "path header"):
            guard.scan_diff(diff, guard.default_policy())

    def test_empty_deleted_rename_copy_and_multiple_files(self) -> None:
        policy = guard.default_policy()
        empty = guard.scan_diff("", policy)
        self.assertEqual((empty.findings, empty.scanned), ([], 0))
        deleted = guard.scan_diff(
            "diff --git a/gone.md b/gone.md\n"
            "deleted file mode 100644\n"
            "--- a/gone.md\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-host " + SYNTHETIC_IPV4 + "\n",
            policy,
        )
        self.assertEqual((deleted.findings, deleted.scanned), ([], 0))
        renamed = guard.scan_diff(
            "diff --git a/old.md b/new.md\n"
            "rename from old.md\n"
            "rename to new.md\n"
            "--- a/old.md\n"
            "+++ b/new.md\n"
            "@@ -1 +1,2 @@\n"
            " keep\n"
            "+host " + SYNTHETIC_IPV4 + "\n",
            policy,
        )
        self.assertEqual(
            [(item.path, item.line, item.category) for item in renamed.findings],
            [("new.md", 2, "ipv4")],
        )
        copied = guard.scan_diff(
            "diff --git a/src.md b/copy.md\n"
            "copy from src.md\n"
            "copy to copy.md\n"
            "--- a/src.md\n"
            "+++ b/copy.md\n"
            "@@ -1 +1,2 @@\n"
            " keep\n"
            "+host " + SYNTHETIC_IPV4 + "\n",
            policy,
        )
        self.assertEqual(copied.findings[0].path, "copy.md")
        multiple = guard.scan_diff(
            "diff --git a/one.md b/one.md\n"
            "--- a/one.md\n"
            "+++ b/one.md\n"
            "@@ -0,0 +1 @@\n"
            "+host " + SYNTHETIC_IPV4 + "\n"
            "diff --git a/two.md b/two.md\n"
            "--- a/two.md\n"
            "+++ b/two.md\n"
            "@@ -0,0 +1 @@\n"
            "+mac 02:1a:2b:3c:4d:5e\n",
            policy,
        )
        self.assertEqual(
            [(item.path, item.line, item.category) for item in multiple.findings],
            [("one.md", 1, "ipv4"), ("two.md", 1, "mac-address")],
        )
        self.assertEqual(multiple.scanned, 2)


class TrackedTreeTests(unittest.TestCase):
    def test_private_state_force_added_as_empty_or_binary_never_reaches_content_scan(self) -> None:
        for private_root in guard.PRIVATE_STATE_ROOTS:
            with self.subTest(root=private_root), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                init_repo(repo)
                (repo / "README.md").write_text("public\n", encoding="utf-8")
                (repo / ".gitignore").write_text(private_root + "/\n", encoding="utf-8")
                git(repo, "add", "README.md", ".gitignore")
                git(repo, "commit", "-qm", "base")
                for name, data in (("empty.json", b""), ("private-name.bin", b"\0private-content-marker")):
                    path = repo / private_root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                git(repo, "add", "-f", private_root)
                for read in (lambda: guard.tracked_files(repo), lambda: guard.staged_diff(repo)):
                    with self.assertRaisesRegex(guard.LeakGuardError, "private runtime state") as raised:
                        read()
                    self.assertNotIn("private-name", str(raised.exception))
                    self.assertNotIn("private-content-marker", str(raised.exception))
                git(repo, "commit", "-qm", "fixture private state")
                with self.assertRaisesRegex(guard.LeakGuardError, "private runtime state"):
                    guard.range_diff(repo, "HEAD~1..HEAD")
                git(repo, "rm", "-r", private_root)
                self.assertEqual(guard.staged_diff(repo), "")

    def test_private_paths_are_rejected_before_exclusions_or_file_reads(self) -> None:
        policy = guard.default_policy()
        policy.excluded_path_globs += ("**",)
        for path in (".vaws-local/report.json", ".VAWS-LOCAL/report.json", "docs/../.vaws-local/report.json", ".vaws-runtime/report.json"):
            with self.subTest(path=path), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("private content read")):
                with self.assertRaisesRegex(guard.LeakGuardError, "private runtime state"):
                    guard.scan_files(ROOT, [path], policy)

    def test_submodule_gitlinks_are_not_listed(self) -> None:
        paths = guard.tracked_files(ROOT)
        self.assertTrue(paths)
        self.assertNotIn("vllm", paths)
        self.assertNotIn("vllm-ascend", paths)
        self.assertFalse([path for path in paths if path.startswith(("vllm/", "vllm-ascend/"))])

    def test_binary_and_oversized_files_are_skipped_not_silently_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "blob.bin").write_bytes(b"\x00\x01host 192.168.240.7")
            (repo / "big.txt").write_text("host 192.168.240.7\n" * 100, encoding="utf-8")
            policy = guard.default_policy()
            policy.max_file_bytes = 64
            result = guard.scan_files(repo, ["blob.bin", "big.txt"], policy)
            self.assertEqual(result.findings, [])
            self.assertEqual(
                sorted(item["reason"] for item in result.skipped),
                ["binary", "larger-than-64-bytes"],
            )


class ScannerCliTests(unittest.TestCase):
    def _run(self, *args: str) -> tuple[int, dict, str]:
        result = subprocess.run(
            [sys.executable, "-B", str(SCANNER), *args],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        text = result.stdout.strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = json.loads(text.splitlines()[-1])
        return result.returncode, payload, result.stderr

    def test_progress_on_stderr_and_single_json_on_stdout(self) -> None:
        code, payload, stderr = self._run("--paths", ".agents/tests/fixtures/tracked_leak_guard/clean.txt")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertIn("[tracked-leak-scan]", stderr)

    def test_scoped_exclusion_keeps_the_guards_own_fixtures_clean(self) -> None:
        code, payload, _ = self._run(
            "--paths", ".agents/tests/fixtures/tracked_leak_guard/dirty.txt"
        )
        self.assertEqual((code, payload["finding_count"]), (0, 0))

    def test_findings_fail_with_exit_code_one(self) -> None:
        code, payload, _ = self._run(
            "--paths", ".agents/tests/fixtures/tracked_leak_guard/dirty.txt", "--no-allowlist"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertGreaterEqual(payload["finding_count"], len(guard.CATEGORIES))

    def test_tracked_tree_scan_passes_and_reports_suppressions(self) -> None:
        code, payload, _ = self._run("--format", "json")
        self.assertEqual((code, payload["status"]), (0, "passed"))
        self.assertEqual(payload["mode"], "tracked-tree")
        self.assertGreater(payload["suppressed_count"], 0)
        self.assertEqual(payload["unused_allowlist_entries"], [])


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hook = load_script_module("_vaws_leak_hook_test", HOOK)

    def _repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        init_repo(repo)
        policy_dir = repo / ".agents" / "leak-guard"
        policy_dir.mkdir(parents=True)
        (policy_dir / "allowlist.yaml").write_text(
            "schema_version: 1\n"
            "allowlist:\n"
            "  - id: docs-example\n"
            "    path_glob: allowed.md\n"
            "    categories: [ipv4]\n"
            "    match: 192.168.240.7\n"
            "    justification: reserved private example kept for the guard test suite\n",
            encoding="utf-8",
        )
        return repo

    def test_install_status_and_uninstall_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            payload = self.hook.install(repo, force=False)
            self.assertEqual((payload["status"], payload["action"]), ("passed", "installed"))
            hook_path = Path(payload["hook_path"])
            self.assertTrue(hook_path.exists())
            self.assertTrue(os.name == "nt" or hook_path.stat().st_mode & 0o111)
            self.assertEqual(self.hook.status(repo)["installed"], True)
            self.assertEqual(self.hook.install(repo, force=False)["action"], "reinstalled")
            self.assertEqual(self.hook.uninstall(repo)["action"], "removed")
            self.assertEqual(self.hook.status(repo)["installed"], False)

    def test_foreign_hook_is_preserved_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            hook_path = self.hook.hooks_dir(repo)
            hook_path.mkdir(parents=True, exist_ok=True)
            (hook_path / "pre-commit").write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
            blocked = self.hook.install(repo, force=False)
            self.assertEqual(blocked["status"], "blocked")
            self.assertIn("mine", (hook_path / "pre-commit").read_text(encoding="utf-8"))
            forced = self.hook.install(repo, force=True)
            self.assertEqual(forced["action"], "replaced")
            self.assertIn("mine", Path(forced["backup_path"]).read_text(encoding="utf-8"))
            self.assertEqual(self.hook.uninstall(repo)["action"], "removed")

    def test_hook_blocks_a_real_commit_and_names_file_line_category(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            self.hook.install(repo, force=False)
            (repo / "leak.md").write_text("intro\nhost 192.168.240.7\n", encoding="utf-8")
            git(repo, "add", "leak.md")
            attempt = subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "add leak"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(attempt.returncode, 0)
            self.assertIn("leak.md:2:", attempt.stderr)
            self.assertIn("ipv4", attempt.stderr)
            self.assertIn("allowlist.yaml", attempt.stderr)
            self.assertIn("justification", attempt.stderr)
            # The blocked value itself is not echoed back in full.
            self.assertNotIn("192.168.240.7", attempt.stderr)

    def test_hook_allows_a_commit_covered_by_the_repository_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            self.hook.install(repo, force=False)
            (repo / "allowed.md").write_text("host 192.168.240.7\n", encoding="utf-8")
            git(repo, "add", "allowed.md")
            done = subprocess.run(
                ["git", "-C", str(repo), "commit", "-m", "allowed example"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_hook_fails_closed_when_the_policy_is_broken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            (repo / ".agents" / "leak-guard" / "allowlist.yaml").write_text(
                "schema_version: 1\nallow_everything: true\n", encoding="utf-8"
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = self.hook.main(["--check", "--repo-root", str(repo)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "error")

    def test_hook_resolves_the_policy_of_the_committed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            self.assertEqual(
                self.hook.resolve_policy_path(repo, None),
                repo / ".agents" / "leak-guard" / "allowlist.yaml",
            )
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / ".agents" / "leak-guard" / "allowlist.yaml"
            self.assertEqual(self.hook.resolve_policy_path(Path(tmp), None), missing)
            self.assertFalse(missing.is_file())
            explicit = Path(tmp) / "custom.yaml"
            self.assertEqual(self.hook.resolve_policy_path(Path(tmp), explicit), explicit)


class G1BoundaryTests(unittest.TestCase):
    """Synthetic regressions for the six demonstrated guard false passes."""

    def _policy_repo(self, repo: Path, body: str = MINIMAL_POLICY) -> Path:
        init_repo(repo)
        policy = write_minimal_policy(repo, body)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "base policy")
        return policy

    def test_unquote_git_path_covers_tab_quote_backslash_newline_and_utf8(self) -> None:
        self.assertEqual(guard.unquote_git_path("notes.md"), "notes.md")
        self.assertEqual(guard.unquote_git_path(r'"notes\tfile.md"'), "notes\tfile.md")
        self.assertEqual(guard.unquote_git_path(r'"notes\nfile.md"'), "notes\nfile.md")
        self.assertEqual(guard.unquote_git_path('"quote\\"file.md"'), 'quote"file.md')
        self.assertEqual(guard.unquote_git_path(r'"back\\slash.md"'), "back\\slash.md")
        self.assertEqual(
            guard.unquote_git_path(r'"\346\226\207\344\273\266.md"'),
            "文件.md",
        )

    def test_long_line_is_scanned_past_4096_and_does_not_split_tokens(self) -> None:
        policy = guard.default_policy()
        long_line = ("x" * 4097) + " host " + SYNTHETIC_IPV4
        findings = guard.scan_document(long_line + "\n", path="notes.md", policy=policy)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].category, "ipv4")
        self.assertEqual(findings[0].line, 1)
        self.assertEqual(findings[0].column, 4104)
        self.assertNotIn("240", findings[0].preview)
        spanning = ("x" * 4090) + SYNTHETIC_IPV4
        spanning_findings = guard.scan_document(spanning + "\n", path="notes.md", policy=policy)
        self.assertEqual(len(spanning_findings), 1)
        self.assertEqual(spanning_findings[0].column, 4091)

    def test_tree_staged_range_and_hook_agree_on_long_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            policy_path = self._policy_repo(repo)
            target = repo / "notes.md"
            target.write_text(("x" * 4097) + " host " + SYNTHETIC_IPV4 + "\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            policy = guard.load_policy(policy_path)
            tree = guard.scan_files(repo, ["notes.md"], policy)
            staged = guard.scan_diff(guard.staged_diff(repo), policy)
            self.assertEqual(len(tree.findings), 1)
            self.assertEqual(len(staged.findings), 1)
            self.assertEqual(tree.findings[0].line, staged.findings[0].line)
            self.assertEqual(tree.findings[0].column, staged.findings[0].column)
            self.assertEqual(tree.findings[0].path, "notes.md")
            code, payload, stderr = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--format",
                "json",
            )
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["finding_count"], 1)
            self.assertEqual(payload["scanned_file_count"], 2)
            self.assertEqual(payload["findings"][0]["path"], "notes.md")
            self.assertEqual(payload["findings"][0]["line"], 1)
            self.assertEqual(payload["findings"][0]["column"], 4104)
            self.assertNotIn("240", payload["findings"][0]["preview"])
            self.assertNotIn("Traceback", stderr)
            staged_code, staged_payload, _ = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--staged",
                "--format",
                "json",
            )
            self.assertEqual(staged_code, 1)
            self.assertEqual(staged_payload["finding_count"], 1)
            self.assertEqual(staged_payload["findings"][0]["path"], "notes.md")
            self.assertEqual(staged_payload["scanned_file_count"], 1)
            hook_code, hook_payload, hook_err = invoke_cli(
                HOOK,
                "--check",
                "--repo-root",
                str(repo),
            )
            self.assertEqual(hook_code, 1)
            self.assertEqual(hook_payload["status"], "failed")
            self.assertEqual(hook_payload["finding_count"], 1)
            self.assertEqual(hook_payload["findings"][0]["line"], 1)
            self.assertNotIn("240", hook_payload["findings"][0]["preview"])
            self.assertNotIn("Traceback", hook_err)
            git(repo, "commit", "-qm", "long line")
            head = git(repo, "rev-parse", "HEAD").strip()
            base = git(repo, "rev-parse", "HEAD~1").strip()
            range_code, range_payload, _ = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--commit-range",
                base + ".." + head,
                "--format",
                "json",
            )
            self.assertEqual(range_code, 1)
            self.assertEqual(range_payload["finding_count"], 1)
            self.assertEqual(range_payload["findings"][0]["path"], "notes.md")
            self.assertEqual(range_payload["findings"][0]["column"], 4104)

    def test_quoted_git_filenames_scan_with_both_quotepath_settings(self) -> None:
        names = [
            "notes\tfile.md",
            'quote"file.md',
            "back\\slash.md",
            "new\nline.md",
            "utf8文件.md",
        ]
        if os.name == "nt":
            names = ["space name.md", "quote’file.md", "utf8文件.md"]
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            policy_path = self._policy_repo(repo)
            git(repo, "config", "core.quotePath", "true")
            for name in names:
                (repo / name).write_text("host " + SYNTHETIC_IPV4 + "\n", encoding="utf-8")
            git(repo, "add", "-A")
            policy = guard.load_policy(policy_path)
            for quote_path in ("true", "false"):
                git(repo, "config", "core.quotePath", quote_path)
                result = guard.scan_diff(guard.staged_diff(repo), policy)
                found = {item.path for item in result.findings}
                with self.subTest(quotePath=quote_path):
                    self.assertEqual(found, set(names))
                    self.assertEqual(result.scanned, len(names))
                    self.assertEqual({item.line for item in result.findings}, {1})
            tree = guard.scan_files(repo, names, policy)
            self.assertEqual({item.path for item in tree.findings}, set(names))
            code, payload, _ = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--staged",
                "--format",
                "json",
            )
            self.assertEqual(code, 1)
            self.assertEqual(payload["finding_count"], len(names))
            self.assertEqual({item["path"] for item in payload["findings"]}, set(names))
            hook_code, hook_payload, hook_stderr = invoke_cli(
                HOOK, "--repo-root", str(repo), "--check"
            )
            self.assertEqual(hook_code, 1)
            self.assertEqual({item["path"] for item in hook_payload["findings"]}, set(names))
            self.assertNotIn("Traceback", hook_stderr)
            for name in names:
                self.assertIn(name, hook_stderr)
            git(repo, "commit", "-qm", "special names")
            tree_code, tree_payload, _ = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--format",
                "json",
            )
            self.assertEqual(tree_code, 1)
            self.assertEqual({item["path"] for item in tree_payload["findings"]}, set(names))
            head = git(repo, "rev-parse", "HEAD").strip()
            base = git(repo, "rev-parse", "HEAD~1").strip()
            range_code, range_payload, _ = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--commit-range",
                base + ".." + head,
                "--format",
                "json",
            )
            self.assertEqual(range_code, 1)
            self.assertEqual({item["path"] for item in range_payload["findings"]}, set(names))

    def test_boolean_policy_fields_fail_closed_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._policy_repo(repo)
            (repo / "notes.md").write_text("ordinary safe documentation\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            schema_path = write_minimal_policy(repo, "schema_version: true\n")
            code, payload, stderr = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--allowlist",
                str(schema_path),
                "--format",
                "json",
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertIn("schema_version", payload["error"])
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn(SYNTHETIC_IPV4, payload["error"])
            bytes_path = write_minimal_policy(
                repo,
                "schema_version: 1\nsettings:\n  max_file_bytes: true\n",
            )
            code, payload, stderr = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--allowlist",
                str(bytes_path),
                "--format",
                "json",
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertIn("max_file_bytes", payload["error"])
            self.assertNotIn("Traceback", stderr)
            hook_code, hook_payload, hook_err = invoke_cli(
                HOOK,
                "--check",
                "--repo-root",
                str(repo),
                "--allowlist",
                str(bytes_path),
            )
            self.assertEqual(hook_code, 1)
            self.assertEqual(hook_payload["status"], "error")
            self.assertNotIn("Traceback", hook_err)

    def test_target_repo_policy_is_used_and_missing_policy_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            allowing = (
                "schema_version: 1\n"
                "allowlist:\n"
                "  - id: notes-example\n"
                "    path_glob: notes.md\n"
                "    categories: [ipv4]\n"
                "    match: " + SYNTHETIC_IPV4 + "\n"
                "    justification: reserved private example used by the guard test suite\n"
            )
            self._policy_repo(repo, allowing)
            (repo / "notes.md").write_text("host " + SYNTHETIC_IPV4 + "\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            code, payload, _ = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--format",
                "json",
            )
            expected_policy = str(
                (repo / ".agents" / "leak-guard" / "allowlist.yaml").resolve()
            )
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["status"], "passed")
            self.assertEqual(payload["finding_count"], 0)
            self.assertGreaterEqual(payload["suppressed_count"], 1)
            self.assertTrue(any(item["path"] == "notes.md" for item in payload["suppressed"]))
            self.assertEqual(payload["policy_file"], expected_policy)
            self.assertNotEqual(payload["policy_file"], str(guard.DEFAULT_POLICY_PATH))
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_repo(repo)
            (repo / "notes.md").write_text("ordinary safe documentation\n", encoding="utf-8")
            git(repo, "add", "notes.md")
            git(repo, "commit", "-qm", "no policy")
            code, payload, stderr = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--format",
                "json",
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertIn("not found", payload["error"])
            self.assertIn(
                str((repo / ".agents" / "leak-guard" / "allowlist.yaml").resolve()),
                payload["error"],
            )
            self.assertNotIn("Traceback", stderr)
            hook_code, hook_payload, hook_err = invoke_cli(
                HOOK,
                "--check",
                "--repo-root",
                str(repo),
            )
            self.assertEqual(hook_code, 1)
            self.assertEqual(hook_payload["status"], "error")
            self.assertIn("not found", hook_payload["error"])
            self.assertIn(
                str((repo / ".agents" / "leak-guard" / "allowlist.yaml").resolve()),
                hook_payload["error"],
            )
            self.assertNotEqual(
                hook_payload.get("policy_file"),
                str(guard.DEFAULT_POLICY_PATH),
            )
            self.assertNotIn("Traceback", hook_err)
            explicit = write_minimal_policy(repo)
            allow_code, allow_payload, _ = invoke_cli(
                SCANNER,
                "--repo-root",
                str(repo),
                "--allowlist",
                str(explicit),
                "--format",
                "json",
            )
            self.assertEqual(allow_code, 0)
            self.assertEqual(allow_payload["status"], "passed")

    def test_linked_worktree_without_policy_does_not_use_installing_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "main"
            main.mkdir()
            self._policy_repo(main)
            (main / "notes.md").write_text("ordinary safe documentation\n", encoding="utf-8")
            git(main, "add", "notes.md")
            git(main, "commit", "-qm", "with policy")
            with_policy = git(main, "rev-parse", "HEAD").strip()
            (main / ".agents" / "leak-guard" / "allowlist.yaml").unlink()
            git(main, "add", "-A")
            git(main, "commit", "-qm", "drop policy")
            without_policy = git(main, "rev-parse", "HEAD").strip()
            linked = root / "linked"
            missing = root / "missing"
            git(main, "worktree", "add", str(linked), with_policy)
            git(main, "worktree", "add", str(missing), without_policy)
            linked_code, linked_payload, _ = invoke_cli(
                HOOK,
                "--check",
                "--repo-root",
                str(linked),
            )
            self.assertEqual(linked_code, 0)
            self.assertEqual(linked_payload["status"], "passed")
            self.assertEqual(
                linked_payload["policy_file"],
                str((linked / ".agents" / "leak-guard" / "allowlist.yaml").resolve()),
            )
            missing_code, missing_payload, missing_err = invoke_cli(
                HOOK,
                "--check",
                "--repo-root",
                str(missing),
            )
            self.assertEqual(missing_code, 1)
            self.assertEqual(missing_payload["status"], "error")
            self.assertIn("not found", missing_payload["error"])
            self.assertIn(
                str((missing / ".agents" / "leak-guard" / "allowlist.yaml").resolve()),
                missing_payload["error"],
            )
            self.assertNotIn("Traceback", missing_err)
            scan_code, scan_payload, _ = invoke_cli(
                SCANNER,
                "--repo-root",
                str(missing),
                "--format",
                "json",
            )
            self.assertEqual(scan_code, 2)
            self.assertEqual(scan_payload["status"], "error")


class CurrentMainFindingScopeTests(unittest.TestCase):
    """The five current-main allowances suppress only their path/category/value."""

    SHARED_ROOTS = "/home/models\n/home/data\n/home/cache\n/home/shared\n"
    ENVELOPE_SRC = ".agents/lib/vaws_result_envelope.py"
    FEEDBACK_DOC = "docs/agent-feedback-contract.md"
    ENVELOPE_TEST = ".agents/tests/test_result_envelope.py"
    OTHER_SRC = ".agents/lib/vaws_local_state.py"
    VERSION_LINE = 'torch_npu="2.7.1.dev20260801"'
    HOME_LINE = "/home/example-user/work/run.py"
    TOKEN_LINE = 'extensions={"env": {"HF_TOKEN": "hf_realsecretvaluegoeshere"}}'
    OTHER_IPV4 = "host " + SYNTHETIC_IPV4

    def setUp(self) -> None:
        self.policy = guard.load_policy(POLICY_PATH)

    def _scan(self, text: str, path: str) -> list[guard.Finding]:
        return guard.scan_document(text, path=path, policy=self.policy)

    def _unallowlisted(self, findings: list[guard.Finding]) -> list[str]:
        return [
            f"{item.category}:{item.match}"
            for item in findings
            if item.allowlisted_by is None
        ]

    def test_global_prefixes_and_scoped_exclusions_are_unchanged(self) -> None:
        self.assertEqual(
            self.policy.allowed_path_prefixes,
            (
                "/home/weights",
                "/home/vaws",
                "/root/namespace",
                "/root/cwd",
                "/Users/Shared",
            ),
        )
        self.assertEqual(
            [(item.id, item.path_glob, item.categories) for item in self.policy.scoped_exclusions],
            [
                ("leak-guard-test-module", ".agents/tests/test_tracked_leak_scan.py", ("*",)),
                (
                    "leak-guard-test-fixtures",
                    ".agents/tests/fixtures/tracked_leak_guard/**",
                    ("*",),
                ),
            ],
        )
        self.assertNotIn("/home/models", self.policy.allowed_path_prefixes)
        self.assertNotIn("/home/data", self.policy.allowed_path_prefixes)
        self.assertNotIn("/home/cache", self.policy.allowed_path_prefixes)
        self.assertNotIn("/home/shared", self.policy.allowed_path_prefixes)


    def test_different_or_longer_homes_remain_findings_in_allowed_source_and_doc(self) -> None:
        longer = "/home/models-extra /home/shared-extra /home/q12345678\n"
        for path in (self.ENVELOPE_SRC, self.FEEDBACK_DOC):
            with self.subTest(path=path):
                findings = self._scan(longer, path)
                self.assertEqual(
                    self._unallowlisted(findings),
                    [
                        "absolute-user-path:/home/models-extra",
                        "absolute-user-path:/home/shared-extra",
                        "absolute-user-path:/home/q12345678",
                    ],
                )


    def test_envelope_version_fixture_allows_only_the_exact_value_and_path(self) -> None:
        findings = self._scan(self.VERSION_LINE, self.ENVELOPE_TEST)
        self.assertEqual([(item.category, item.allowlisted_by) for item in findings],
                         [("internal-identifier", "result-envelope-test-torch-npu-dev-date")])
        for text, path in ((self.VERSION_LINE, self.OTHER_SRC),
                           ('torch_npu="2.7.1.dev20260802"', self.ENVELOPE_TEST),
                           (self.HOME_LINE, self.ENVELOPE_TEST),
                           (self.TOKEN_LINE, self.ENVELOPE_TEST)):
            with self.subTest(path=path, text=text):
                self.assertTrue(self._unallowlisted(self._scan(text, path)))


class LockfileVersionAllowanceTests(unittest.TestCase):
    def test_performance_version_allowance_is_exact_and_document_scoped(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        path = "docs/six-scenario-performance-2026-09-13.md"
        value = "torch_npu 2.10.0.post4.dev20260715"
        findings = guard.scan_document(value, path=path, policy=policy)
        self.assertEqual([item.allowlisted_by for item in findings],
                         ["six-scenario-report-torch-npu-dev-date"])
        for text, candidate_path in (
            (value, "report.md"),
            ("torch_npu 2.10.0.post4.dev20260716", path),
            ("owner q12345678", path),
            ("host " + SYNTHETIC_IPV4, path),
        ):
            with self.subTest(text=text, path=candidate_path):
                findings = guard.scan_document(text, path=candidate_path, policy=policy)
                self.assertTrue(findings)
                self.assertTrue(all(item.allowlisted_by is None for item in findings))

    def test_package_version_allowance_cannot_hide_an_address_elsewhere(self) -> None:
        policy = guard.load_policy(POLICY_PATH)
        findings = guard.scan_document("1.2.0.2", path="uv.lock", policy=policy)
        self.assertEqual([item.allowlisted_by for item in findings], ["lockfile-brotlicffi-version"])
        for value, path in (("1.2.0.3", "uv.lock"), ("1.2.0.2", "report.md")):
            findings = guard.scan_document(value, path=path, policy=policy)
            self.assertTrue(findings)
            self.assertTrue(all(item.allowlisted_by is None for item in findings))


if __name__ == "__main__":
    unittest.main()

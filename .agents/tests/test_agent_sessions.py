"""Local task identity, actual worktree binding and client hook contracts.

Run with the local workspace control-plane tests.
These tests do not stand in for native-client acceptance.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))

PACKAGE_PRESENT = importlib.util.find_spec("vaws_coordinator") is not None
requires_coordinator = unittest.skipUnless(
    PACKAGE_PRESENT, "vaws-coordinator is not installed; run `uv sync`"
)


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if PACKAGE_PRESENT:
    from vaws_coordinator.agent_session import AgentSessions, load_context
    from vaws_coordinator.task_client import TaskClient
    from vaws_coordinator.hooks import vaws_session as hooks
else:
    AgentSessions = load_context = TaskClient = hooks = None  # type: ignore[misc, assignment]
setup = module_at("native_session_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
from client_setup_fixtures import selected_runtime


@requires_coordinator
class AgentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var is a /private/var symlink; resolve once so path equality
        # checks against resolved hook/setup outputs hold on the dev machine.
        self.root = Path(self.temp.name).resolve()
        self.store = AgentSessions(self.root / "state")
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def attach(self, native="root-a", client="codex", **args):
        return self.store.attach(client, native, str(self.root), **args)

    def test_new_native_sessions_same_cwd_are_distinct_but_resume_keeps_task(self):
        first, second = self.attach(), self.attach("root-b")
        self.assertNotEqual(first["session"]["id"], second["session"]["id"])
        self.store.detach(first)
        resumed = self.attach()
        self.assertEqual(resumed["session"]["id"], first["session"]["id"])
        self.assertEqual(resumed["attachment"]["id"], first["attachment"]["id"])
        self.assertEqual(load_context(first["context_file"])["attachment"]["state"], "attached")

    def test_repeated_concurrent_start_hooks_cannot_duplicate_a_task(self):
        def attach(_):
            return AgentSessions(self.root / "state").attach("codex", "native", str(self.root))
        with ThreadPoolExecutor(3) as workers:
            contexts = list(workers.map(attach, range(9)))
        self.assertEqual(len({item["session"]["id"] for item in contexts}), 1)

    def test_child_cross_tool_and_explicit_association_share_task_only_when_requested(self):
        parent = self.attach()
        child = self.attach("child", client="claude", parent_context=parent["context_file"])
        explicit = self.attach("other", client="kimi", association=parent["context_file"])
        independent = self.attach("other", client="grok")
        self.assertEqual(child["session"]["id"], parent["session"]["id"])
        self.assertEqual(child["attachment"]["parent_id"], parent["attachment"]["id"])
        self.assertEqual(explicit["session"]["id"], parent["session"]["id"])
        self.assertIsNone(explicit["attachment"]["parent_id"])
        self.assertNotEqual(independent["session"]["id"], parent["session"]["id"])
        with self.assertRaisesRegex(ValueError, "another task"):
            self.attach("other", client="grok", association=parent["context_file"])
        other_registry = AgentSessions(self.root / "clone-b")
        with self.assertRaisesRegex(ValueError, "local registry"):
            other_registry.attach("codex", "from-clone-b", str(self.root), parent_context=parent["context_file"])

    def test_hook_contracts_use_native_ids_and_resume_the_same_task(self):
        payloads = {
            "claude": {"hook_event_name": "SessionStart", "session_id": "native-claude"},
            "codex": {"hook_event_name": "SessionStart", "session_id": "native-codex"},
            "grok": {"hookEventName": "session_start", "sessionId": "native-grok"},
            "kimi": {"hook_event_name": "SessionStart", "session_id": "native-kimi"},
            "cursor": {"hook_event_name": "sessionStart", "conversation_id": "native-cursor", "cursor_version": "test"},
        }
        for client, payload in payloads.items():
            with self.subTest(client=client):
                payload["cwd"] = str(self.root)
                hooks.handle(client, payload, self.store)
                before = self.store.native_context(client, "native-" + client)
                hooks.handle(client, {**payload, "source": "resume"}, self.store)
                self.assertEqual(before["session"]["id"], self.store.native_context(client, "native-" + client)["session"]["id"])
        self.assertEqual(hooks.handle("claude", payloads["grok"], self.store), {})
        self.assertEqual(hooks.handle("cursor", payloads["grok"], self.store), {})
        with self.store.transaction() as db:
            self.assertEqual(len(self.store.rows(db, "session")), 5)

    def test_cursor_session_id_only_payload_attaches_and_grok_import_still_noops(self):
        # A genuine Cursor payload is discriminated by cursor_version, not by
        # the presence of conversation_id; Grok's Cursor-hook import carries
        # Grok's camelCase hookEventName and no cursor_version.
        payload = {"hook_event_name": "sessionStart", "sessionId": "cursor-session-id-only",
                   "cursor_version": "test", "cwd": str(self.root)}
        output = hooks.handle("cursor", payload, self.store)
        context = self.store.native_context("cursor", "cursor-session-id-only")
        self.assertIn("context_file", context)
        self.assertTrue(output)
        grok_import = {"hookEventName": "session_start", "sessionId": "grok-via-cursor", "cwd": str(self.root)}
        self.assertEqual(hooks.handle("cursor", grok_import, self.store), {})
        with self.assertRaises(ValueError):
            self.store.native_context("cursor", "grok-via-cursor")

    def test_pretool_hook_injects_context_and_subagent_detach_does_not_end_root(self):
        parent = self.attach()
        child = {"hook_event_name": "SubagentStart", "session_id": "root-a", "agent_id": "child-1", "cwd": str(self.root)}
        hooks.handle("codex", child, self.store)
        context = self.store.native_context("codex", "root-a", "child-1")
        self.assertEqual(context["session"]["id"], parent["session"]["id"])
        output = hooks.handle("codex", {**child, "hook_event_name": "PreToolUse", "tool_name": "mcp__vaws-task__vaws_session", "tool_input": {}}, self.store)
        self.assertEqual(output["hookSpecificOutput"]["updatedInput"]["context_file"], context["context_file"])
        stale = hooks.handle("codex", {**child, "hook_event_name": "PreToolUse", "tool_name": "mcp__remote_dev__vaws_session", "tool_input": {}}, self.store)
        self.assertEqual(stale["hookSpecificOutput"]["updatedInput"]["context_file"], context["context_file"])
        hooks.handle("codex", {**child, "hook_event_name": "SubagentStop"}, self.store)
        self.assertEqual(self.store.context(parent["attachment"]["id"])["attachment"]["state"], "attached")

    def test_grok_use_tool_dispatcher_injects_nested_context(self):
        context = self.attach(client="grok", native="grok-native")
        payload = {
            "hookEventName": "pre_tool_use",
            "sessionId": "grok-native",
            "cwd": str(self.root),
            "toolName": "use_tool",
            "toolInput": {
                "tool_name": "remote-dev__vaws_session",
                "tool_input": {"sources": {"workspace": str(self.root)}},
            },
        }
        output = hooks.handle("grok", payload, self.store)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["tool_name"], "remote-dev__vaws_session")
        self.assertEqual(updated["tool_input"]["context_file"], context["context_file"])
        self.assertNotIn("context_file", updated)

    def test_grok_qualified_hook_name_preserves_use_tool_envelope(self):
        context = self.attach(client="grok", native="grok-qualified")
        payload = {
            "hookEventName": "pre_tool_use",
            "sessionId": "grok-qualified",
            "cwd": str(self.root),
            "toolName": "remote-dev__vaws_session",
            "toolInput": {
                "tool_name": "remote-dev__vaws_session",
                "tool_input": {"sources": {"workspace": str(self.root)}},
            },
        }
        output = hooks.handle("grok", payload, self.store)
        updated = output["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["tool_name"], "remote-dev__vaws_session")
        self.assertEqual(updated["tool_input"]["context_file"], context["context_file"])
        self.assertNotIn("context_file", updated)

    def test_attach_rejects_child_inheritance_and_association_together(self):
        parent = self.attach()
        with self.assertRaisesRegex(ValueError, "choose child inheritance or an explicit task association"):
            self.attach("other", parent_context=parent["context_file"], association=parent["context_file"])

    def test_execution_request_id_reuse_must_keep_the_same_spec(self):
        context = self.attach()
        row = self.store.execution(context, "req", {"command": "one"})
        self.assertEqual(self.store.execution(context, "req", {"command": "one"})["id"], row["id"])
        with self.assertRaisesRegex(ValueError, "reused with different arguments"):
            self.store.execution(context, "req", {"command": "two"})

    def test_finishing_task_cannot_be_reopened_by_native_resume(self):
        context = self.attach()
        with self.store.transaction() as db:
            session = self.store.get(db, "session", context["session"]["id"])
            session["state"] = "finishing"
            self.store.put(db, "session", session)
        with self.assertRaisesRegex(ValueError, "finishing"):
            self.attach()

    def test_session_end_detaches_root_and_compact_resume_guides_explicit_context(self):
        context = self.attach()
        payload = {"session_id": "root-a", "cwd": str(self.root)}
        output = hooks.handle("codex", {**payload, "hook_event_name": "SessionEnd"}, self.store)
        self.assertEqual(output, {})
        self.assertEqual(self.store.context(context["attachment"]["id"])["attachment"]["state"], "detached")
        with self.assertRaisesRegex(ValueError, "VAWS_CONTEXT_FILE"):
            hooks.handle("codex", {**payload, "hook_event_name": "SessionStart", "source": "compact"}, self.store)

    def test_subagent_stop_for_unknown_child_records_and_detaches_it(self):
        parent = self.attach()
        payload = {"hook_event_name": "SubagentStop", "session_id": "root-a", "agent_id": "ghost", "cwd": str(self.root)}
        self.assertEqual(hooks.handle("codex", payload, self.store), {})
        with self.store.transaction() as db:
            ghosts = [row for row in self.store.rows(db, "attachment") if row.get("agent_id") == "ghost"]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0]["state"], "detached")
        self.assertEqual(ghosts[0]["session_id"], parent["session"]["id"])
        self.assertEqual(ghosts[0]["parent_id"], parent["attachment"]["id"])
        self.assertEqual(self.store.context(parent["attachment"]["id"])["attachment"]["state"], "attached")

    def test_hint_prints_context_path_on_its_own_line(self):
        context = self.attach("hint-path")
        output = hooks.handle("codex", {"hook_event_name": "UserPromptSubmit", "session_id": "hint-path",
                                        "cwd": str(self.root)}, self.store)
        hint = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn(context["context_file"], hint.splitlines())


class ScaffoldSetupTests(unittest.TestCase):
    """Hook wrapper and client-setup contracts that stay in this repository."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        runtime = pytest.MonkeyPatch()
        self.addCleanup(runtime.undo)
        selected_runtime(runtime, setup, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_kimi_hook_stays_silent_outside_project_and_on_errors(self):
        script = ROOT / ".agents/hooks/vaws_session.py"
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "kimi-outside",
                   "cwd": str(self.root), "prompt": "hello"}
        env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
        env["VAWS_SKIP_VENV_REEXEC"] = "1"
        for args, stdin in (
            (["--client", "kimi", "--project", str(self.root / "elsewhere")], payload),
            (["--client", "kimi"], {**payload, "session_id": ""}),
        ):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(script), *args], input=json.dumps(stdin),
                                        capture_output=True, text=True, env=env, check=False)
                self.assertEqual(result.returncode, 0)
                self.assertNotIn("{}", result.stdout)
                self.assertEqual(result.stdout.strip(), "")

    def test_client_setup_preserves_user_policy_and_does_not_grant_trust(self):
        settings = self.root / ".claude/settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"deny": ["Bash(ssh *)"]}, "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "my-hook"}]}]}}))
        files = setup.configuration("claude", self.root)
        value = json.loads(files[settings])
        self.assertEqual(value["permissions"], {"deny": ["Bash(ssh *)"]})
        self.assertEqual(len(value["hooks"]["SessionStart"]), 2)
        settings.write_text(files[settings])
        self.assertEqual(setup.configuration("claude", self.root)[settings], files[settings])
        kimi = self.root / "kimi-private.toml"
        kimi.write_text('default_model = "existing"\n')
        import tomllib
        result = tomllib.loads(setup.configuration("kimi", self.root, kimi_config=kimi)[kimi])
        self.assertEqual(result["default_model"], "existing")
        self.assertTrue(all("--project" in setup.hook_argv(item["command"]) for item in result["hooks"]))

    def test_hook_timeout_covers_the_hook_git_calls(self):
        groups = setup.hook_groups("claude", self.root)
        self.assertTrue(all(entry["timeout"] >= 12 for group in groups.values() for entry in group[0]["hooks"]))
        import tomllib
        kimi = self.root / "kimi-private.toml"
        result = tomllib.loads(setup.configuration("kimi", self.root, kimi_config=kimi)[kimi])
        self.assertTrue(all(item["timeout"] >= 12 for item in result["hooks"]))

    def test_cursor_native_setup_supplies_context_to_task_and_companion_tools(self):
        import re
        files = setup.configuration("cursor", self.root)
        hooks = json.loads(files[self.root / ".cursor/hooks.json"])["hooks"]
        hook = hooks["preToolUse"][0]
        for name in ("vaws_run", "MCP:vaws_execution", "mcp__vaws_task__vaws_message",
                     "MCP:knowledge_query", "MCP:remote_read"):
            self.assertIsNotNone(re.search(hook["matcher"], name))
        for name in ("read_file", "MCP:user_query", "vaws_run_unrelated"):
            self.assertIsNone(re.search(hook["matcher"], name))
        self.assertGreaterEqual(hook["timeout"], 12)
        native = json.loads(files[self.root / ".cursor/worktrees.json"])
        self.assertIn("ROOT_WORKTREE_PATH", native["setup-worktree"][0])
        self.assertNotIn("vaws_worktree_setup", hooks["sessionStart"][0]["command"])

    def test_task_pretool_matchers_migrate_generated_hooks_and_preserve_user_scope(self):
        import re
        paths = {"codex": ".codex/hooks.json", "claude": ".claude/settings.local.json",
                 "grok": ".grok/hooks/vaws-session.json", "cursor": ".cursor/hooks.json"}
        for client, relative in paths.items():
            with self.subTest(client=client):
                event = "preToolUse" if client == "cursor" else "PreToolUse"
                wanted = setup.hook_groups(client, self.root)[event]
                for name in ("vaws_session", "MCP:vaws_run", "vaws_task__vaws_message"):
                    self.assertIsNotNone(re.search(wanted[0]["matcher"], name))
                for name in ("Bash", "read_file", "MCP:user_query", "vaws_run_other"):
                    self.assertIsNone(re.search(wanted[0]["matcher"], name))
                self.assertEqual(bool(re.search(wanted[0]["matcher"], "MCP:knowledge_query")), client == "cursor")
                old = {key: value for key, value in wanted[0].items() if key != "matcher"}
                settings = self.root / relative
                settings.parent.mkdir(parents=True, exist_ok=True)
                user = {"matcher": "Bash", "hooks": [{"command": "user-hook"}]}
                settings.write_text(json.dumps({"hooks": {event: [user, old]}}))
                files = setup.configuration(client, self.root)
                groups = json.loads(files[settings])["hooks"][event]
                self.assertEqual(groups[0], user)
                self.assertEqual(groups[1]["matcher"], wanted[0]["matcher"])
                settings.write_text(files[settings])
                self.assertEqual(setup.configuration(client, self.root)[settings], files[settings])

                custom = {**wanted[0], "matcher": "custom-user-scope"}
                self.assertEqual(setup.merge_hook_event([custom], wanted, client, self.root)[0]["matcher"],
                                 "custom-user-scope")
                if client != "cursor":
                    mixed = {"hooks": [{"command": "user-hook"}, *old["hooks"]]}
                    merged = setup.merge_hook_event([mixed], wanted, client, self.root)
                    self.assertNotIn("matcher", merged[0])
                    self.assertEqual(merged[0]["hooks"][0], {"command": "user-hook"})

    def test_kimi_removes_ineffective_pretool_hook_and_preserves_user_hooks(self):
        import hashlib
        import tomllib
        settings = self.root / "kimi-private.toml"
        project_key = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        command = setup.hook_command("kimi", self.root)
        user = '[[hooks]]\nevent = "PreToolUse"\ncommand = "user-hook"\n'
        old = '[[hooks]]\nevent = "PreToolUse"\ncommand = ' + json.dumps(command) + '\n'
        settings.write_text(setup.managed_toml_text(user, "session-" + project_key, old))
        files = setup.configuration("kimi", self.root, kimi_config=settings)
        hooks = tomllib.loads(files[settings])["hooks"]
        self.assertEqual([item for item in hooks if item["event"] == "PreToolUse"],
                         [{"event": "PreToolUse", "command": "user-hook"}])
        self.assertIn("SessionStart", {item["event"] for item in hooks})
        settings.write_text(files[settings])
        self.assertEqual(setup.configuration("kimi", self.root, kimi_config=settings)[settings], files[settings])


if __name__ == "__main__":
    unittest.main()

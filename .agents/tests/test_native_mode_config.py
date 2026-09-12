"""Native preference plans preserve user configuration and unsupported clients."""
from __future__ import annotations

import os
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from vaws_native_mode_config import add_grok_import_dedup, add_native_mode


class NativeModeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.user_dir = Path(self.temporary.name)
        self.project = self.user_dir / "project"
        self.path = self.user_dir / ".grok/config.toml"

    def plan(self, text=None):
        files, notes = {}, []
        if text is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
        add_native_mode(files, notes, "grok", self.project, user_home=self.user_dir)
        return files, notes

    def test_missing_configuration_is_planned_without_writes(self):
        files, notes = self.plan()
        parsed = tomllib.loads(files[self.path])
        self.assertEqual(parsed["cli"]["worktree_type"], "git")
        self.assertEqual(parsed["hints"], {"new_session_worktree_mode": "always", "fork_worktree_mode": "always"})
        self.assertFalse(self.path.exists())
        self.assertFalse((self.project / ".grok/config.toml").exists())
        self.assertEqual(notes[0]["scope"], "user")

    def test_custom_settings_comments_and_hooks_survive(self):
        before = ('# personal setup\n[cli]\nworktree_type = "standalone" # old choice\n'
                  'custom = "keep"\n[hints]\nnew_session_worktree_mode = "never"\n'
                  'fork_worktree_mode = "ask"\nmemory_modal_fullscreen = true\n'
                  '[[hooks.SessionStart]]\nmatcher = "startup"\n')
        files, _ = self.plan(before)
        parsed = tomllib.loads(files[self.path])
        self.assertEqual(parsed["cli"]["custom"], "keep")
        self.assertTrue(parsed["hints"]["memory_modal_fullscreen"])
        self.assertEqual(parsed["hooks"], tomllib.loads(before)["hooks"])
        self.assertTrue(files[self.path].startswith("# personal setup\n"))
        self.assertEqual(self.path.read_text(), before)

    def test_existing_plan_is_used_and_repeated_planning_is_idempotent(self):
        files, notes = self.plan('[cli]\ncustom = "from-file"\n')
        files[self.path] += '\n[models]\ndefault = "custom-model"\n'
        before = files.copy()
        add_native_mode(files, notes, "grok", self.project, user_home=self.user_dir)
        self.assertEqual(files, before)
        self.assertEqual(notes[-1]["action"], "configured")

    def test_multiline_text_containing_table_headers_is_preserved(self):
        before = 'banner = """\n[cli]\npretend = true\n"""\n[hints]\ncustom = 42\n'
        files, _ = self.plan(before)
        parsed = tomllib.loads(files[self.path])
        self.assertEqual(parsed["banner"], tomllib.loads(before)["banner"])
        self.assertEqual(parsed["hints"]["custom"], 42)

    def test_valid_inline_table_is_left_for_its_owner(self):
        before = 'cli = {worktree_type = "standalone", custom = 1}\n'
        files, notes = self.plan(before)
        self.assertEqual(files, {})
        self.assertEqual(notes[0]["action"], "preserved")
        self.assertEqual(self.path.read_text(), before)

    def test_malformed_configuration_is_not_overwritten(self):
        files, notes = self.plan('[cli\nworktree_type = "git"\n')
        self.assertEqual(files, {})
        self.assertEqual(notes[0]["reason"], "native-mode-config-needs-integration")

    def test_config_directory_override_is_honored(self):
        custom = self.user_dir / "profile"
        files, notes = {}, []
        with patch.dict(os.environ, {"GROK_HOME": str(custom)}):
            add_native_mode(files, notes, "grok", self.project)
        self.assertIn(custom / "config.toml", files)

    def test_claude_reports_native_cli_gap_without_unknown_keys(self):
        files, notes = {}, []
        add_native_mode(files, notes, "claude", self.project, user_home=self.user_dir)
        self.assertFalse(files)
        self.assertEqual(notes[0]["reason"], "no-native-default-worktree-setting")
        self.assertEqual(notes[0]["scope"], "native-cli")

    def test_kimi_does_not_gain_an_unproven_hook(self):
        files, notes = {}, []
        add_native_mode(files, notes, "kimi", self.project, user_home=self.user_dir)
        self.assertFalse(files)
        self.assertFalse(notes)


    def test_unverified_clients_preserve_automatic_update_settings(self):
        for client, table, key in (("grok", "cli", "auto_update"), ("kimi", "upgrade", "auto_install")):
            for enabled in (False, True):
                with self.subTest(client=client, enabled=enabled):
                    path = self.user_dir / (".grok/config.toml" if client == "grok" else "custom-kimi/tui.toml")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    before = "[" + table + "]\n" + key + " = " + str(enabled).lower() + "\n"
                    path.write_text(before, encoding="utf-8")
                    files, notes = {}, []
                    add_native_mode(files, notes, client, self.project, user_home=self.user_dir)
                    self.assertIs(tomllib.loads(files.get(path, before))[table][key], enabled)
                    self.assertEqual(path.read_text(), before)


class GrokImportDedupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.user_dir = Path(self.temporary.name)
        self.project = self.user_dir / "project"
        self.path = self.user_dir / ".grok/config.toml"
        self.cursor_path = self.user_dir / ".cursor/mcp.json"
        self.native_path = self.project / ".grok/config.toml"
        self.args = {"remote-dev": ["-m", "remote_dev.mcp.server"],
                     "vaws-task": ["-m", "vaws_coordinator", "task-server"],
                     "vaws-knowledge": ["-m", "vaws_knowledge.server.mcp_server"]}
        self.imports = {name: {"command": "python", "args": [
            str(self.project / ".agents/scripts/vaws_native_mcp.py"), kind],
            "env": {"VAWS_MCP_WORKSPACE": "${workspaceFolder}"}}
            for name, kind in (("remote-dev", "remote"), ("vaws-task", "task"), ("vaws-knowledge", "knowledge"))}
        self.native = {name.replace("-", "_"): {"command": "/prepared/python", "args": args}
                       for name, args in self.args.items()}

    def plan(self, text="", *, imports=None, native=None, project_imports=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text)
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(json.dumps({"mcpServers": self.imports if imports is None else imports}))
        native = self.native if native is None else native
        native_text = "\n".join("[mcp_servers." + json.dumps(name) + "]\n" +
                              "".join(key + " = " + json.dumps(value) + "\n" for key, value in entry.items())
                              for name, entry in native.items())
        files, notes = {self.native_path: native_text}, []
        if project_imports is not None:
            files[self.project / ".cursor/mcp.json"] = json.dumps({"mcpServers": project_imports})
        self.run_plan(files, notes)
        return files, notes

    def run_plan(self, files, notes):
        # The environment owner independently validates receipt/interpreter
        # ownership. These fixtures exercise how its decision affects the plan.
        with patch("vaws_native_mode_config.Path.home", return_value=self.user_dir), \
                patch.dict(os.environ, {"GROK_HOME": str(self.path.parent)}):
            add_grok_import_dedup(files, notes, self.project, self.project,
                                  owned_server=lambda entry, project: entry.get("command") == "/prepared/python")

    def test_keeps_user_config_and_other_imports_without_writing(self):
        before = '# personal\nbanner = """\ndisabled_mcp_servers = ["text"]\n"""\ndisabled_mcp_servers = [\n"custom-disabled",\n]\n[compat.cursor]\nmcps = true\n'
        imports = {**self.imports, "custom-server": {"command": "my-server"}}
        files, notes = self.plan(before, imports=imports)
        after = tomllib.loads(files[self.path])
        self.assertEqual(after["disabled_mcp_servers"], ["custom-disabled", *self.args])
        self.assertEqual({key: value for key, value in after.items() if key != "disabled_mcp_servers"},
                         {key: value for key, value in tomllib.loads(before).items() if key != "disabled_mcp_servers"})
        self.assertIn("# personal\n", files[self.path])
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(json.loads(self.cursor_path.read_text())["mcpServers"], imports)
        self.assertEqual(notes[-1]["servers"], list(self.args))

    def test_second_plan_is_idempotent_and_single_client_mode_does_not_dedup(self):
        files, notes = self.plan('disabled_mcp_servers = ["remote-dev"]\n')
        before = files.copy()
        self.run_plan(files, notes)
        self.assertEqual(files, before)
        self.assertEqual(notes[-1]["action"], "configured")
        mode_files, mode_notes = {}, []
        add_native_mode(mode_files, mode_notes, "grok", self.project, user_home=self.user_dir)
        self.assertEqual(tomllib.loads(mode_files[self.path])["disabled_mcp_servers"], ["remote-dev"])

    def test_custom_cursor_entries_and_project_overrides_are_preserved(self):
        custom = {"remote-dev": {"command": "custom", "args": ["other"]}}
        custom_workspace = {**self.imports["remote-dev"], "env": {"VAWS_MCP_WORKSPACE": "/custom/project"}}
        for imports, project_imports in (({**self.imports, **custom}, None), (self.imports, custom),
                                        ({**self.imports, "remote-dev": custom_workspace}, None)):
            with self.subTest(project_override=project_imports is not None):
                files, _ = self.plan(imports=imports, project_imports=project_imports)
                self.assertEqual(tomllib.loads(files[self.path])["disabled_mcp_servers"],
                                 ["vaws-task", "vaws-knowledge"])

    def test_only_an_enabled_owned_matching_replacement_is_sufficient(self):
        for replacement in (None, {"command": "custom", "args": self.args["remote-dev"]},
                            {**self.native["remote_dev"], "enabled": False},
                            {**self.native["remote_dev"], "args": self.args["vaws-task"]}):
            with self.subTest(replacement=replacement):
                native = {key: value for key, value in self.native.items() if key != "remote_dev"}
                if replacement is not None:
                    native["remote_dev"] = replacement
                files, _ = self.plan(native=native)
                self.assertNotIn("remote-dev", tomllib.loads(files[self.path])["disabled_mcp_servers"])
        files, _ = self.plan('disabled_mcp_servers = ["remote_dev"]\n')
        self.assertEqual(tomllib.loads(files[self.path])["disabled_mcp_servers"],
                         ["remote_dev", "vaws-task", "vaws-knowledge"])

    def test_existing_same_name_grok_servers_are_preserved(self):
        custom = {"command": "custom", "args": []}
        for text, native in (('[mcp_servers."remote-dev"]\ncommand = "custom"\n', self.native),
                             ("", {**self.native, "remote-dev": custom})):
            with self.subTest(user_definition=bool(text)):
                files, _ = self.plan(text, native=native)
                self.assertNotIn("remote-dev", tomllib.loads(files[self.path])["disabled_mcp_servers"])

    def test_unknown_or_wrong_kind_cursor_provider_is_not_disabled(self):
        imports = dict(self.imports)
        imports["remote-dev"] = {**imports["remote-dev"], "args": [
            str(self.project / ".agents/scripts/vaws_native_mcp.py"), "task"]}
        imports["vaws-task"] = {**imports["vaws-task"], "args": [
            str(self.user_dir / "other/.agents/scripts/vaws_native_mcp.py"), "task"]}
        files, _ = self.plan(imports=imports)
        self.assertEqual(tomllib.loads(files[self.path])["disabled_mcp_servers"], ["vaws-knowledge"])

    def test_invalid_disabled_list_is_preserved(self):
        files, notes = self.plan('disabled_mcp_servers = "custom"\n')
        self.assertNotIn(self.path, files)
        self.assertEqual(notes[-1]["reason"], "grok-compat-mcp-needs-integration")


if __name__ == "__main__":
    unittest.main()

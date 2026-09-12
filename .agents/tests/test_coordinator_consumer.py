"""Scaffold-side contract with the installed vaws-coordinator package.

Launcher, client-setup preservation and the build-input byte match run
against the package. Official MCP SDK coverage lives in
``test_coordinator_official_stdio.py``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "scripts"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_coordinator_launch as coordinator  # noqa: E402
from client_setup_fixtures import native_task_entry

_GONE_COORDINATOR_ROOT = "VAWS_" + "COORDINATOR_ROOT"

requires_package = unittest.skipUnless(
    importlib.util.find_spec("vaws_coordinator") is not None,
    "vaws-coordinator is not installed; run `uv sync`",
)


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def isolated_python():
    """Return a 3.11+ interpreter that does not see the workspace ``.venv``."""
    base = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
    if base.is_file() and base.resolve() != Path(sys.executable).resolve():
        return str(base)
    for candidate in ("/opt/homebrew/bin/python3", "/usr/bin/python3"):
        path = Path(candidate)
        if not path.is_file():
            continue
        proc = subprocess.run(
            [str(path), "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"],
            check=False,
        )
        if proc.returncode == 0:
            return str(path)
    raise unittest.SkipTest("no Python 3.11+ interpreter outside .venv")


def isolated_env():
    drop = {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in drop and not key.startswith("UV_")
    }
    env["VAWS_SKIP_VENV_REEXEC"] = "1"
    return env


class EnvironmentTests(unittest.TestCase):
    def test_resource_requests_preserve_explicit_zero_and_device_choices(self) -> None:
        from vaws_task_target import run_command, service_resources

        client = mock.Mock()
        for inputs, expected in (
            ({}, {"npu_count": 1, "service_port": 0}),
            ({"npu_count": 0}, {"npu_count": 0, "service_port": 0}),
            ({"devices": []}, {"devices": [], "service_port": 0}),
            ({"devices": [], "npu_count": 0, "service_port": None}, {"devices": [], "npu_count": 0}),
            ({"devices": [2, 3], "npu_count": 1}, {"devices": [2, 3], "npu_count": 1, "service_port": 0}),
        ):
            with self.subTest(inputs=inputs):
                run_command(client, "true", resources=service_resources(**inputs))
                client.run.assert_called_with("true", resources=expected)

    def test_environment_fills_the_single_registry(self) -> None:
        env = coordinator.coordinator_environment({})
        self.assertTrue(env["VAWS_AGENT_SESSIONS_DIR"].endswith("agent-sessions"))
        self.assertNotIn("VAWS_HOST_QUEUE_MODULE", env)
        self.assertNotIn("VAWS_COORDINATOR_STATE_DIR", env)
        self.assertNotIn(_GONE_COORDINATOR_ROOT, env)

    def test_environment_keeps_caller_values(self) -> None:
        env = coordinator.coordinator_environment({
            "VAWS_AGENT_SESSIONS_DIR": "/tmp/explicit-registry",
            "VAWS_HOST_QUEUE_MODULE": "/tmp/host.py",
        })
        self.assertEqual(Path(env["VAWS_AGENT_SESSIONS_DIR"]).resolve(), Path("/tmp/explicit-registry").resolve())
        self.assertNotIn("VAWS_HOST_QUEUE_MODULE", env)

    def test_relative_registry_path_uses_the_shared_workspace(self) -> None:
        from vaws_local_state import shared_workspace_root
        env = coordinator.coordinator_environment({"VAWS_AGENT_SESSIONS_DIR": ".vaws-local/agent-sessions"})
        self.assertEqual(
            env["VAWS_AGENT_SESSIONS_DIR"],
            str(shared_workspace_root(ROOT) / ".vaws-local" / "agent-sessions"),
        )

    def test_identity_snapshot_is_forwarded_without_reading_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertNotIn("VAWS_GITHUB_IDENTITY_FILE", coordinator.coordinator_environment({}, repo_root=root))
            snapshot = root / ".vaws-local/github.json"
            snapshot.parent.mkdir()
            snapshot.write_text('{"schema":"vaws.github.v1","login":"alice","github_user_id":1}')
            env = coordinator.coordinator_environment({}, repo_root=root)
            self.assertEqual(env["VAWS_GITHUB_IDENTITY_FILE"], str(snapshot.resolve()))
            explicit = coordinator.coordinator_environment(
                {"VAWS_GITHUB_IDENTITY_FILE": "another-user.json"}, repo_root=root)
            self.assertEqual(explicit["VAWS_GITHUB_IDENTITY_FILE"], str(root / "another-user.json"))


class BuildInputOwnershipTests(unittest.TestCase):
    def test_scaffold_does_not_keep_a_copy(self) -> None:
        self.assertFalse((ROOT / ".agents/lib/vaws_build_inputs.py").is_file())
        try:
            import vaws_coordinator.build_inputs as packaged
        except ImportError:
            self.skipTest("vaws-coordinator is not installed")
        self.assertTrue(Path(packaged.__file__).is_file())


class LauncherTests(unittest.TestCase):
    def test_available_package_launch_does_not_read_the_lock(self) -> None:
        with mock.patch.object(coordinator, "find_spec", return_value=object()), mock.patch.object(
            coordinator, "inspect", side_effect=AssertionError("startup must not scan dependency pins")
        ):
            coordinator.require_package()

    def test_unavailable_package_explains_the_setup_remedy(self) -> None:
        with mock.patch.object(coordinator, "find_spec", return_value=None):
            with self.assertRaisesRegex(coordinator.CoordinatorUnavailable, "vaws_deps.py sync"):
                coordinator.require_package()

    def test_status_reports_the_installed_package(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws.py"), "status"],
            capture_output=True, text=True, check=False,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["name"], "vaws-coordinator")
        self.assertIn(payload["state"], {"missing", "off_spec", "ready"})
        self.assertEqual(payload["remedy"], "uv run --no-project python .agents/scripts/vaws_deps.py sync")

    def test_status_without_package_reports_missing(self) -> None:
        proc = subprocess.run(
            [isolated_python(), str(SCRIPTS / "vaws.py"), "status"],
            capture_output=True, text=True, env=isolated_env(), check=False,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"], "missing")
        self.assertEqual(payload["remedy"], "uv run --no-project python .agents/scripts/vaws_deps.py sync")

    def test_unprepared_native_task_entries_require_setup_before_creating_state(self) -> None:
        # Normal startup now resolves an immutable environment before package
        # invocation. Exercise that reachable boundary without the old bypass.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            entry = native_task_entry(ROOT, root / 'unprepared-checkout', prepared=False)
            env = {key: value for key, value in isolated_env().items() if not key.startswith('VAWS_')}
            registry = root / 'registry'
            env['VAWS_AGENT_SESSIONS_DIR'] = str(registry)
            env['VAWS_ENV_HOME'] = str(root / 'empty-ready-store')
            python = isolated_python()
            for operation in ('task-server', 'attach', 'session', 'run', 'execution', 'finish'):
                with self.subTest(operation=operation):
                    child = subprocess.run([python, str(entry), operation], input='',
                                           capture_output=True, text=True, env=env, check=False)
                    self.assertEqual(child.returncode, 2, (operation, child.stderr))
                    self.assertNotIn('Traceback', child.stderr)
                    self.assertIn('uv run --no-project python .agents/scripts/vaws_deps.py sync', child.stderr)
            self.assertFalse(registry.exists())

    def test_hook_without_package_does_not_write_a_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = isolated_env()
            proc = subprocess.run(
                [isolated_python(), str(ROOT / ".agents/hooks/vaws_session.py"), "--client", "codex"],
                input='{"hook_event_name":"SessionStart","session_id":"n1"}',
                capture_output=True, text=True, env=env, check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("uv run --no-project python .agents/scripts/vaws_deps.py sync", proc.stderr)
        self.assertFalse(list(Path(tmp).rglob("sessions.sqlite3")))

    def test_env_json_lists_owned_keys(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws.py"), "env", "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("VAWS_AGENT_SESSIONS_DIR", payload)
        self.assertNotIn("VAWS_HOST_QUEUE_MODULE", payload)
        self.assertNotIn(_GONE_COORDINATOR_ROOT, payload)
        self.assertTrue(all(key.startswith("VAWS_") for key in payload))

    def test_exec_module_preserves_existing_coordinator_machines(self) -> None:
        self.assertFalse(hasattr(coordinator, "seed_machine_directory"))
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            local = repo / ".vaws-local"
            local.mkdir()
            inventory = local / "machine-inventory.json"
            inventory.write_text(
                '{"schema_version": 1, "hosts": [{"host": "old.example"}]}\n',
                encoding="utf-8",
            )
            state_dir = local / "coordinator"
            state_dir.mkdir()
            machines = state_dir / "machines.json"
            original = '{"schema_version": 1, "hosts": [{"host": "provisioned.example"}]}\n'
            machines.write_text(original, encoding="utf-8")
            sessions = local / "agent-sessions"
            sessions.mkdir()
            captured: dict[str, object] = {}

            def fake_execve(executable, command, env):
                captured["env"] = dict(env)
                captured["command"] = list(command)
                raise SystemExit(0)

            def fake_run_module(module, **kwargs):
                return fake_execve(sys.executable, [sys.executable, "-m", *sys.argv], os.environ)

            isolated = {
                key: value
                for key, value in os.environ.items()
                if key != "VAWS_COORDINATOR_STATE_DIR"
            }
            isolated["VAWS_COORDINATOR_STATE_DIR"] = str(state_dir)
            isolated["VAWS_AGENT_SESSIONS_DIR"] = str(sessions)
            with mock.patch.object(coordinator, "require_package", return_value={"state": "ready"}), mock.patch.object(
                coordinator.os, "execve", side_effect=fake_execve
            ), mock.patch("runpy.run_module", side_effect=fake_run_module), mock.patch.object(
                sys, "argv", list(sys.argv)
            ), mock.patch.dict(os.environ, isolated, clear=True):
                with self.assertRaises(SystemExit):
                    coordinator.exec_module("vaws_coordinator", ["status"], repo_root=repo)
            self.assertEqual(machines.read_text(encoding="utf-8"), original)
            self.assertEqual(
                inventory.read_text(encoding="utf-8"),
                '{"schema_version": 1, "hosts": [{"host": "old.example"}]}\n',
            )
            env = captured["env"]
            assert isinstance(env, dict)
            self.assertEqual(
                Path(env["VAWS_COORDINATOR_STATE_DIR"]).resolve(),
                state_dir.resolve(),
            )

    def test_help_matrix(self) -> None:
        for args in (["--help"], ["status", "--help"]):
            with self.subTest(args=args):
                proc = subprocess.run(
                    [sys.executable, str(SCRIPTS / "vaws.py"), *args],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws.py"), "bootstrap", "--help"],
            capture_output=True, text=True, check=False,
        )
        self.assertNotEqual(proc.returncode, 0)


class ClientSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.setup = load_script("vaws_client_setup")
        # These preservation fixtures use the current platform's temporary
        # directory. The mounted-drive WSL bridge has its own integration test.
        owner = mock.patch.object(self.setup, "managed_python", return_value=sys.executable)
        owner.start()
        self.addCleanup(owner.stop)
        native = mock.patch("vaws_local_owner.windows_mounted_workspace", return_value=False)
        native.start()
        self.addCleanup(native.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve() / "project"
        self.project.mkdir()
        patcher = mock.patch.dict(os.environ, {_GONE_COORDINATOR_ROOT: "", "WSL_DISTRO_NAME": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def is_claude_session_command(self, command):
        return self.setup.hook_argv(command)[:3] == [
            sys.executable, str(ROOT / ".agents/scripts/vaws_claude_entry.py"), "session",
        ]

    def test_fresh_json_emits_both_launchers(self) -> None:
        files = self.setup.configuration("claude", self.project)
        servers = json.loads(files[self.project / ".mcp.json"])["mcpServers"]
        self.assertEqual(set(servers), {"remote-dev", "vaws-task", "vaws-knowledge"})
        entry = str(ROOT / ".agents/scripts/vaws_claude_entry.py")
        self.assertEqual(servers["remote-dev"]["args"], [entry, "remote"])
        self.assertEqual(servers["vaws-task"]["args"], [entry, "task"])
        self.assertNotIn("VAWS_ENV_RECEIPT", servers["vaws-task"]["env"])
        self.assertEqual(servers["vaws-task"]["type"], "stdio")
        self.assertIn("VAWS_AGENT_SESSIONS_DIR", servers["vaws-task"]["env"])
        self.assertNotIn("VAWS_HOST_QUEUE_MODULE", servers["vaws-task"]["env"])
        self.assertNotIn(_GONE_COORDINATOR_ROOT, servers["vaws-task"]["env"])
        hook = json.loads(files[self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn("--agent-sessions-dir", self.setup.hook_argv(hook))
        self.assertNotIn("--coordinator-root", self.setup.hook_argv(hook))

    def test_task_only_skips_remote_dev(self) -> None:
        servers = json.loads(
            self.setup.configuration("claude", self.project, task_only=True)[self.project / ".mcp.json"]
        )["mcpServers"]
        self.assertEqual(set(servers), {"vaws-task"})
        grok = tomllib.loads(
            self.setup.configuration("grok", self.project, task_only=True)[self.project / ".grok/config.toml"]
        )
        self.assertEqual(set(grok["mcp_servers"]), {"vaws_task"})

    def test_summary_hook_setup_preserves_foreign_stop_and_is_idempotent(self) -> None:
        path = self.project / ".codex/hooks.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "foreign-hook"}]}]}}))
        first = self.setup.configuration("codex", self.project)[path]
        path.write_text(first)
        second = self.setup.configuration("codex", self.project)[path]
        self.assertEqual(first, second)
        handlers = [entry for group in json.loads(second)["hooks"]["Stop"] for entry in group["hooks"]]
        self.assertEqual(len(handlers), 2)
        self.assertEqual(handlers[0]["command"], "foreign-hook")
        self.assertTrue(any(Path(arg).name == "knowledge_summary.py" for arg in self.setup.hook_argv(handlers[1]["command"])))

    def test_toml_knowledge_environment_adds_config_without_replacing_user_fields(self) -> None:
        source = '[mcp_servers.vaws_knowledge]\ncommand = "custom-python"\nargs = []\nenabled = true\n[mcp_servers.vaws_knowledge.env]\nCUSTOM = "keep"\n'
        entry = tomllib.loads(source)["mcp_servers"]["vaws_knowledge"]
        desired = {"env": {"VAWS_KNOWLEDGE_CONFIG": "workspace-config.json", "CUSTOM": "replace"}}
        text = self.setup.fill_toml_server_env(source, "vaws_knowledge", entry, desired)
        result = tomllib.loads(text)["mcp_servers"]["vaws_knowledge"]
        self.assertEqual(result["command"], "custom-python")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["env"], {"CUSTOM": "keep", "VAWS_KNOWLEDGE_CONFIG": "workspace-config.json"})

    def test_json_preserves_hand_managed_remote_dev_command_args_type(self) -> None:
        path = self.project / ".mcp.json"
        old = {
            "user_top": "preserve",
            "mcpServers": {
                "remote-dev": {
                    "command": "user-command",
                    "args": ["user-argument"],
                    "type": "stdio",
                    "env": {"USER_SETTING": "fixture-value"},
                    "user_field": 17,
                },
                "other": {"command": "other-command"},
            },
        }
        path.write_text(json.dumps(old))
        plan = self.setup.build_plan("claude", self.project)
        data = json.loads(plan["files"][path])
        entry = data["mcpServers"]["remote-dev"]
        self.assertEqual(entry["command"], "user-command")
        self.assertEqual(entry["args"], ["user-argument"])
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(entry["user_field"], 17)
        self.assertEqual(entry["env"]["USER_SETTING"], "fixture-value")
        self.assertEqual(data["user_top"], "preserve")
        self.assertEqual(data["mcpServers"]["other"], {"command": "other-command"})
        self.assertIn("vaws-task", data["mcpServers"])
        self.assertTrue(any(note.get("reason") == "existing-named-server" for note in plan["notes"]))

    def _apply_client(self, client, project):
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "vaws_client_setup.py"),
                "--client", client, "--project", str(project), "--apply",
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_json_preserves_missing_custom_command_and_both_aliases(self) -> None:
        path = self.project / ".mcp.json"
        servers = {
            "remote-dev": {"command": "python", "args": [str(self.project / "not-installed.py")], "env": {"CUSTOM": "keep"}},
            "remote_dev": {"command": "custom-server", "args": ["--live"], "enabled": True},
        }
        path.write_text(json.dumps({"mcpServers": servers}))
        self._apply_client("claude", self.project)
        actual = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        for alias, expected in servers.items():
            self.assertEqual(actual[alias], expected)
        self.assertIn("vaws-task", actual)

    def test_toml_preserves_missing_custom_command_and_both_aliases(self) -> None:
        for client in ("codex", "grok"):
            with self.subTest(client=client):
                project = self.project / client
                project.mkdir()
                config = project / f".{client}" / "config.toml"
                config.parent.mkdir()
                original = (
                    "# Keep the custom provider until its script is installed.\n"
                    '[mcp_servers."remote-dev"]\ncommand = "python"\n'
                    f'args = {json.dumps([str(project / "not-installed.py")])}\n'
                    '[mcp_servers.remote_dev]\ncommand = "custom-server"\nargs = ["--live"]\n'
                )
                config.write_text(original)
                expected = tomllib.loads(original)["mcp_servers"]
                self._apply_client(client, project)
                text = config.read_text(encoding="utf-8")
                actual = tomllib.loads(text)["mcp_servers"]
                for alias, entry in expected.items():
                    self.assertEqual(actual[alias], entry)
                self.assertIn("# Keep the custom provider", text)
                self.assertIn("vaws_task", actual)

    def test_environment_link_owner_accepts_windows_and_wsl_spelling(self) -> None:
        key = "a" * 64
        with mock.patch.object(self.setup, "ROOT", Path("C:/workspace")):
            for command in (
                f"C:/workspace/.vaws-local/env-links/{key}/Scripts/python.exe",
                f"/mnt/c/workspace/.vaws-local/env-links/{key}/Scripts/python.exe",
                f"../.vaws-local/env-links/{key}/Scripts/python.exe",
            ):
                self.assertTrue(self.setup.owned_workspace_interpreter(command, "C:/workspace/child"), command)
            self.assertFalse(self.setup.owned_workspace_interpreter(
                f"C:/workspace-other/.vaws-local/env-links/{key}/Scripts/python.exe", "C:/workspace"))
            self.assertFalse(self.setup.owned_workspace_interpreter("C:/workspace/.venv/Scripts/python.exe", "C:/workspace"))
            self.assertFalse(self.setup.owned_workspace_interpreter(
                f"C:/workspace/.vaws-local/env-links/{key}/Scripts/pythonXexe", "C:/workspace"))

    def test_json_setup_is_idempotent_on_fixtures(self) -> None:
        first = self.setup.configuration("claude", self.project)
        path = self.project / ".mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(first[path])
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(first[settings])
        second = self.setup.configuration("claude", self.project)
        self.assertEqual(second[path], first[path])
        self.assertEqual(second[settings], first[settings])

    def test_toml_preserves_existing_remote_dev_and_adds_task_server(self) -> None:
        config = self.project / ".codex/config.toml"
        config.parent.mkdir()
        config.write_text(
            'user_top = "preserve"\n[mcp_servers.remote_dev]\ncommand = "user-command"\n'
            'args = ["user-argument"]\nuser_field = 17\n[mcp_servers.other]\ncommand = "other-command"\n'
        )
        files = self.setup.configuration("codex", self.project)
        data = tomllib.loads(files[config])
        self.assertEqual(data["user_top"], "preserve")
        self.assertEqual(data["mcp_servers"]["remote_dev"]["command"], "user-command")
        self.assertEqual(data["mcp_servers"]["remote_dev"]["args"], ["user-argument"])
        self.assertEqual(data["mcp_servers"]["other"], {"command": "other-command"})
        self.assertEqual(data["mcp_servers"]["vaws_task"]["args"],
                         [str(self.setup.ROOT / ".agents/scripts/vaws_native_mcp.py"), "task"])
        config.write_text(files[config])
        self.assertNotIn(config, self.setup.configuration("codex", self.project))

    def test_permission_settings_remain_unchanged(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({
            "permissions": {"allow": ["mcp__remote-dev__vaws_session", "Bash(python3 *)"]},
            "hooks": {},
        }))
        plan = self.setup.build_plan("claude", self.project)
        text = plan["files"][settings]
        self.assertIn("mcp__remote-dev__vaws_session", text)
        self.assertIn("mcp__remote-dev__vaws_session", json.loads(text)["permissions"]["allow"])

    def test_preview_does_not_write(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "vaws_client_setup.py"), "--client", "claude", "--project", str(self.project)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"], "preview")
        self.assertFalse(payload["trust_granted"])
        self.assertFalse((self.project / ".mcp.json").exists())

    def test_apply_stays_inside_the_fixture_project(self) -> None:
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPTS / "vaws_client_setup.py"),
                "--client", "claude", "--project", str(self.project), "--apply",
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["state"], "configured")
        self.assertTrue((self.project / ".mcp.json").is_file())
        for item in payload["files"]:
            self.assertTrue(item["path"].startswith(str(self.project)))

    def test_generated_provider_and_hook_keep_explicit_registry(self) -> None:
        registry = (self.project / "reg dir").resolve()
        with mock.patch.dict(os.environ, {
            "VAWS_AGENT_SESSIONS_DIR": str(registry),
        }):
            plan = self.setup.build_plan("claude", self.project, task_only=True)
        server = json.loads(plan["files"][self.project / ".mcp.json"])["mcpServers"]["vaws-task"]
        self.assertNotIn(_GONE_COORDINATOR_ROOT, server["env"])
        self.assertEqual(server["env"]["VAWS_AGENT_SESSIONS_DIR"], str(registry))
        hook = json.loads(plan["files"][self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        argv = self.setup.hook_argv(hook)
        self.assertNotIn("--coordinator-root", argv)
        self.assertEqual(argv[argv.index("--agent-sessions-dir") + 1], str(registry))

    def test_existing_user_env_wins_over_setup_registry(self) -> None:
        path = self.project / ".mcp.json"
        path.write_text(json.dumps({"mcpServers": {"vaws-task": {
            "command": "user-command",
            "args": ["user-argument"],
            "env": {"VAWS_AGENT_SESSIONS_DIR": "/user/managed/registry"},
            "user_field": 1,
        }}}))
        with mock.patch.dict(os.environ, {
            "VAWS_AGENT_SESSIONS_DIR": str(self.project / "setup-registry"),
        }):
            plan = self.setup.build_plan("claude", self.project, task_only=True)
        server = json.loads(plan["files"][path])["mcpServers"]["vaws-task"]
        self.assertEqual(server["command"], "user-command")
        self.assertEqual(server["env"]["VAWS_AGENT_SESSIONS_DIR"], "/user/managed/registry")
        self.assertEqual(server["user_field"], 1)
        hook = json.loads(plan["files"][self.project / ".claude/settings.local.json"])["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        argv = self.setup.hook_argv(hook)
        self.assertNotIn("--coordinator-root", argv)
        self.assertEqual(argv[argv.index("--agent-sessions-dir") + 1], "/user/managed/registry")

    def test_previously_generated_hook_is_replaced_once(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        old = shlex.join([
            sys.executable,
            str(ROOT / ".agents/hooks/vaws_session.py"),
            "--client", "claude",
            "--project", str(self.project),
        ])
        settings.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "my-hook"}]},
            {"hooks": [{"type": "command", "command": old, "timeout": 12}]},
        ]}}))
        with mock.patch.dict(os.environ, {}):
            first = self.setup.build_plan("claude", self.project)
            groups = json.loads(first["files"][settings])["hooks"]["SessionStart"]
            commands = [entry.get("command", "") for group in groups for entry in group.get("hooks", [group])]
            self.assertEqual(commands.count("my-hook"), 1)
            owned = [item for item in commands if self.is_claude_session_command(item)]
            self.assertEqual(len(owned), 1)
            self.assertNotIn("--coordinator-root", self.setup.hook_argv(owned[0]))
            self.assertIn("--agent-sessions-dir", self.setup.hook_argv(owned[0]))
            self.assertNotEqual(owned[0], old)
            settings.write_text(first["files"][settings])
            second = self.setup.build_plan("claude", self.project)
            self.assertEqual(second["files"][settings], first["files"][settings])

    def test_foreign_same_basename_hook_is_preserved(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        foreign = shlex.join([
            sys.executable, "/user/custom/vaws_session.py",
            "--client", "claude", "--project", str(self.project),
        ])
        user = {"type": "command", "command": foreign, "timeout": 37, "user_metadata": "retain"}
        original = {
            "user_top_metadata": "retain",
            "hooks": {"SessionStart": [{
                "matcher": "*",
                "user_group_metadata": "retain",
                "hooks": [user],
            }]},
        }
        settings.write_text(json.dumps(original))
        plan = self.setup.build_plan("claude", self.project, task_only=True)
        after = json.loads(plan["files"][settings])
        self.assertEqual(after["user_top_metadata"], "retain")
        group = after["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "*")
        self.assertEqual(group["user_group_metadata"], "retain")
        self.assertIn(user, group["hooks"])
        owned = [
            entry for item in after["hooks"]["SessionStart"] for entry in item["hooks"]
            if self.is_claude_session_command(entry.get("command", ""))
        ]
        self.assertEqual(len(owned), 1)
        self.assertNotEqual(owned[0]["command"], foreign)

    def test_mixed_group_keeps_user_sibling_and_metadata(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        old = shlex.join([
            sys.executable, str(ROOT / ".agents/hooks/vaws_session.py"),
            "--client", "claude", "--project", str(self.project),
        ])
        user = {"type": "command", "command": "my-existing-audit-hook --record", "timeout": 37, "user_metadata": "retain"}
        settings.write_text(json.dumps({
            "user_top_metadata": "retain",
            "hooks": {"SessionStart": [{
                "matcher": "*",
                "user_group_metadata": "retain",
                "hooks": [{"type": "command", "command": old, "timeout": 12}, user],
            }]},
        }))
        with mock.patch.dict(os.environ, {}):
            plan = self.setup.build_plan("claude", self.project, task_only=True)
            after = json.loads(plan["files"][settings])
            self.assertEqual(after["user_top_metadata"], "retain")
            groups = after["hooks"]["SessionStart"]
            self.assertEqual(len(groups), 1)
            group = groups[0]
            self.assertEqual(group["matcher"], "*")
            self.assertEqual(group["user_group_metadata"], "retain")
            self.assertIn(user, group["hooks"])
            owned = [
                entry for entry in group["hooks"]
                if self.is_claude_session_command(entry.get("command", ""))
            ]
            self.assertEqual(len(owned), 1)
            self.assertNotIn("--coordinator-root", owned[0]["command"])
            self.assertNotEqual(owned[0]["command"], old)
            settings.write_text(plan["files"][settings])
            second = self.setup.build_plan("claude", self.project, task_only=True)
            self.assertEqual(second["files"][settings], plan["files"][settings])

    def test_wrapper_data_argument_is_not_owned(self) -> None:
        settings = self.project / ".claude/settings.local.json"
        settings.parent.mkdir()
        wrapper = shlex.join([
            sys.executable, str(self.project / "audit-wrapper.py"),
            "--hook", str(ROOT / ".agents/hooks/vaws_session.py"),
            "--client", "claude", "--project", str(self.project),
        ])
        user = {"type": "command", "command": wrapper, "timeout": 9, "user_metadata": "retain"}
        settings.write_text(json.dumps({"hooks": {"SessionStart": [{"matcher": "UserPromptSubmit", "hooks": [user]}]}}))
        plan = self.setup.build_plan("claude", self.project, task_only=True)
        after = json.loads(plan["files"][settings])
        group = after["hooks"]["SessionStart"][0]
        self.assertEqual(group["matcher"], "UserPromptSubmit")
        self.assertIn(user, group["hooks"])
        owned = [
            entry for item in after["hooks"]["SessionStart"] for entry in item.get("hooks", [item])
            if self.is_claude_session_command(entry.get("command", ""))
        ]
        self.assertEqual(len(owned), 1)

    def test_cursor_flat_list_replaces_only_owned_command(self) -> None:
        hooks = self.project / ".cursor/hooks.json"
        hooks.parent.mkdir()
        old = shlex.join([
            sys.executable, str(ROOT / ".agents/hooks/vaws_session.py"),
            "--client", "cursor", "--project", str(self.project),
        ])
        hooks.write_text(json.dumps({
            "version": 1,
            "hooks": {"sessionStart": [
                {"command": old, "user_field": "owned-meta"},
                {"command": "user-cursor-hook", "user_field": "retain"},
            ]},
        }))
        plan = self.setup.build_plan("cursor", self.project, task_only=True)
        after = json.loads(plan["files"][hooks])
        groups = after["hooks"]["sessionStart"]
        user = [item for item in groups if item.get("command") == "user-cursor-hook"]
        self.assertEqual(user, [{"command": "user-cursor-hook", "user_field": "retain"}])
        owned = [item for item in groups if self.setup.owned_hook_command(item.get("command", ""), "cursor", self.project)]
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0].get("user_field"), "owned-meta")
        self.assertNotEqual(owned[0]["command"], old)

    def test_all_clients_embed_explicit_registry_in_owned_hooks(self) -> None:
        registry = str((self.project / "explicit-registry").resolve())
        with mock.patch.dict(os.environ, {
            "VAWS_AGENT_SESSIONS_DIR": registry,
        }):
            for client in ("claude", "cursor", "codex", "grok", "kimi"):
                with self.subTest(client=client):
                    plan = self.setup.build_plan(client, self.project, kimi_config=self.project / "kimi.toml")
                    blob = "\n".join(plan["files"].values())
                    self.assertNotIn(_GONE_COORDINATOR_ROOT, blob)
                    self.assertIn(registry, self.setup.hook_argv(self.setup.hook_command(client, self.project)))


class HookAdapterTests(unittest.TestCase):
    def test_explicit_registry_flag_attaches_native_event_in_selected_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "registry with spaces"
            registry.mkdir()
            env = {key: value for key, value in os.environ.items()}
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / ".agents/hooks/vaws_session.py"),
                    "--client", "claude",
                    "--project", tmp,
                    "--agent-sessions-dir", str(registry),
                ],
                input=json.dumps({"hook_event_name":"SessionStart","session_id":"n1","cwd":tmp}),
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            contexts = list((registry / "contexts").glob("*.json"))
            self.assertEqual(len(contexts), 1, proc.stderr)
            hint = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
            context_file = contexts[0].resolve()
            self.assertIn(str(context_file), hint)
            self.assertTrue(context_file.is_file())
            self.assertEqual(context_file.parent.parent.resolve(), registry.resolve())


class NoInTreeTaskWriterTests(unittest.TestCase):
    MOVED = (
        ".agents/coordinator",
        ".agents/lib/vaws_agent_session.py",
        ".agents/lib/vaws_task_client.py",
        ".agents/lib/vaws_ready_runtime.py",
        ".agents/lib/vaws_managed_execution.py",
        ".agents/lib/vaws_runtime_profile.py",
    )

    def test_moved_sources_are_not_tracked(self) -> None:
        tracked = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--", *self.MOVED],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(tracked.stdout.strip(), "", tracked.stdout)

    def test_build_inputs_live_in_the_coordinator_package(self) -> None:
        """Run Manifest and build-inputs are imported from the package; scaffold copies are gone."""
        self.assertFalse((ROOT / ".agents/lib/vaws_build_inputs.py").is_file())
        self.assertFalse((ROOT / ".agents/lib/vaws_host_queue_module.py").is_file())
        self.assertFalse((ROOT / ".agents/lib/vaws_run_manifest.py").is_file())
        import vaws_coordinator.build_inputs  # noqa: F401
        import vaws_coordinator.run_manifest  # noqa: F401




if __name__ == "__main__":
    unittest.main()

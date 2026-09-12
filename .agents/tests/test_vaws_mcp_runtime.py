"""Exercise real stdio backends selected by two independent native tasks."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
import vaws_mcp_runtime as runtime


FAKE_SERVER = '''import json,os,sys,threading,time
from pathlib import Path
key,evidence,init_gate=sys.argv[1:]
lock=threading.Lock()
waiting={}
cancelled=set()
def respond(message):
 method=message.get("method")
 params=message.get("params",{})
 if method=="initialize":
  while init_gate!="-" and not Path(init_gate).exists(): time.sleep(.01)
  result={"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":key}}
 elif method=="tools/list":
  result={"tools":[{"name":"knowledge_query","description":"fixture","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"additionalProperties":False}}]}
 elif method=="tools/call":
  if params["name"]=="exit": os._exit(7)
  if params["name"]=="fail":
   with lock: print(json.dumps({"jsonrpc":"2.0","id":message["id"],"error":{"code":-32000,"message":"fixture remote failure"}}),flush=True)
   return
  if message["id"] in waiting:
   waiting[message["id"]].wait()
   if message["id"] in cancelled: return
  payload={"runtime":key,"arguments":params["arguments"],"meta":params.get("_meta")}
  result={"content":[{"type":"text","text":json.dumps(payload)}],"structuredContent":payload,"isError":False}
 else: result={}
 with lock: print(json.dumps({"jsonrpc":"2.0","id":message["id"],"result":result}),flush=True)
for line in sys.stdin:
 message=json.loads(line)
 with open(evidence,"a",encoding="utf-8") as log: log.write(json.dumps(message)+"\\n")
 if message.get("method")=="notifications/cancelled":
  identifier=message["params"]["requestId"]
  cancelled.add(identifier)
  if identifier in waiting: waiting[identifier].set()
  continue
 if "id" not in message: continue
 if message.get("params",{}).get("arguments",{}).get("wait"):
  waiting[message["id"]]=threading.Event()
 threading.Thread(target=respond,args=(message,),daemon=True).start()
'''


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "provider.py"
        self.script.write_text(FAKE_SERVER)
        self.workspaces = []
        self.selections = {}
        for key in ("old", "new"):
            workspace = self.root / key
            workspace.mkdir()
            self.workspaces.append(workspace)
            self.selections[key] = runtime.Selection(workspace, str(workspace / "ready.json"), sys.executable, key)
        self.init_gate = None
        self.command_patch = patch.object(runtime, "provider_command", side_effect=lambda kind, selected, env:
                                          ([sys.executable, str(self.script), selected.key,
                                            str(self.root / (selected.key + ".jsonl")), str(self.init_gate or "-")], dict(env)))
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)
        from mcp.client import stdio
        launch = stdio._create_platform_compatible_process
        self.processes = []
        async def observe_launch(*args, **kwargs):
            process = await launch(*args, **kwargs)
            self.processes.append(process)
            return process
        process_patch = patch.object(stdio, "_create_platform_compatible_process", side_effect=observe_launch)
        process_patch.start()
        self.addCleanup(process_patch.stop)

    async def received(self, predicate, *, key="new"):
        async def wait():
            path = self.root / (key + ".jsonl")
            while True:
                for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # The child may still be appending this line.
                    if predicate(message):
                        return message
                await asyncio.sleep(.01)
        return await asyncio.wait_for(wait(), timeout=5)

    async def test_persistent_provider_uses_each_tasks_fixed_runtime(self):
        provider = runtime.Provider("knowledge", self.root)
        contexts = {"first": {"id": "old"}, "second": {"id": "new"}}
        def choose(root, context=None, *, catalog=False):
            return self.selections[context["id"] if context else "new"]
        try:
            with patch.object(runtime, "caller_context", side_effect=lambda args, meta, **kw: contexts[args["context_file"]]), \
                 patch.object(runtime, "selection", side_effect=choose):
                tools = await provider.list_tools()
                self.assertIn("context_file", tools[0].inputSchema["properties"])
                old = await provider.call_tool("knowledge_query", {"text": "first", "context_file": "first"})
                new = await provider.call_tool("knowledge_query", {"text": "second", "context_file": "second"})
                again = await provider.call_tool("knowledge_query", {"text": "resume", "context_file": "first"})
            self.assertEqual([old.structuredContent["runtime"], new.structuredContent["runtime"], again.structuredContent["runtime"]],
                             ["old", "new", "old"])
            self.assertNotIn("context_file", new.structuredContent["arguments"])
            self.assertEqual(new.meta["vaws_provider"]["environment"], "new")
            self.assertTrue(Path(new.meta["vaws_provider"]["stderr"]).is_file())
            self.assertEqual(len(provider.backends), 2)
        finally:
            await provider.close()
        self.assertTrue(all(item.worker.done() for item in provider.backends.values()))

    async def test_task_context_and_native_metadata_survive_forwarding(self):
        provider = runtime.Provider("task", self.root)
        metadata = {"x-codex-turn-metadata": {"thread_id": "test-thread"}}
        try:
            with patch.object(runtime, "caller_context", return_value={"context_file": "known-context"}), \
                 patch.object(runtime, "selection", return_value=self.selections["new"]):
                result = await provider.call_tool("vaws_session", {}, metadata)
            self.assertEqual(result.structuredContent["arguments"]["context_file"], "known-context")
            self.assertEqual(result.structuredContent["meta"]["x-codex-turn-metadata"], metadata["x-codex-turn-metadata"])
        finally:
            await provider.close()

    async def test_kimi_catalog_requires_the_existing_native_context(self):
        for kind in ("task", "remote", "knowledge"):
            provider = runtime.Provider(kind, self.root, {"VAWS_MCP_CLIENT": "kimi"})
            try:
                with patch.object(runtime, "selection", return_value=self.selections["new"]):
                    tools = await provider.list_tools()
                self.assertIn("context_file", tools[0].inputSchema["required"])
                self.assertIn("native hook", tools[0].inputSchema["properties"]["context_file"]["description"])
            finally:
                await provider.close()

    async def test_missing_backend_returns_failure_without_hidden_fallback(self):
        provider = runtime.Provider("task", self.root)
        try:
            with patch.object(runtime, "provider_command", return_value=([sys.executable, str(self.root / "missing.py")], {})):
                backend = provider.backend(self.selections["new"])
                with self.assertRaises(Exception):
                    await asyncio.wait_for(backend.request("list_tools"), timeout=10)
                self.assertTrue(backend.stderr.is_file())
                self.assertIn("missing.py", backend.stderr.read_text())
        finally:
            await provider.close()

    async def test_companion_without_context_does_not_start_mother_runtime(self):
        for kind in ("knowledge", "remote", "task"):
            provider = runtime.Provider(kind, self.root)
            with patch.object(runtime, "caller_context", return_value=None):
                with self.assertRaisesRegex(ValueError, "No native task context"):
                    await provider.call_tool("unused", {})
            self.assertEqual(provider.backends, {})

    async def test_long_call_keeps_status_and_stop_concurrent_and_cancellation_reaches_exact_request(self):
        provider = runtime.Provider("remote", self.root)
        backend = provider.backend(self.selections["new"])
        try:
            slow = asyncio.create_task(backend.request("call_tool", name="remote_bash", arguments={"wait": True}))
            request = await self.received(lambda row: row.get("params", {}).get("name") == "remote_bash")
            replies = await asyncio.wait_for(asyncio.gather(
                backend.request("call_tool", name="remote_job_status", arguments={"job_id": "owned"}),
                backend.request("call_tool", name="remote_job_stop", arguments={"job_id": "owned"}),
            ), timeout=3)
            self.assertFalse(slow.done())
            self.assertEqual([row.structuredContent["arguments"] for row in replies], [{"job_id": "owned"}] * 2)
            slow.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await slow
            cancelled = await self.received(lambda row: row.get("method") == "notifications/cancelled")
            self.assertEqual(cancelled["params"]["requestId"], request["id"])
            self.assertEqual((await backend.request("list_tools")).tools[0].name, "knowledge_query")
        finally:
            await provider.close()
        self.assertTrue(backend.worker.done())
        self.assertTrue(self.processes)
        self.assertTrue(all(process.returncode is not None for process in self.processes))

    async def test_cancel_during_initialization_does_not_cancel_other_callers_startup(self):
        self.init_gate = self.root / "initialize-release"
        provider = runtime.Provider("remote", self.root)
        backend = provider.backend(self.selections["new"])
        try:
            first = asyncio.create_task(backend.request("list_tools"))
            await self.received(lambda row: row.get("method") == "initialize")
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(backend.ready.cancelled())
            second = asyncio.create_task(backend.request("list_tools"))
            self.init_gate.touch()
            self.assertEqual((await asyncio.wait_for(second, timeout=3)).tools[0].name, "knowledge_query")
            self.assertFalse(backend.worker.done())
        finally:
            await provider.close()
        self.assertTrue(all(process.returncode is not None for process in self.processes))

    async def test_remote_error_keeps_selected_runtime_and_raw_failure_visible(self):
        provider = runtime.Provider("remote", self.root)
        backend = provider.backend(self.selections["new"])
        try:
            with self.assertRaisesRegex(RuntimeError, "fixture remote failure") as error:
                await backend.request("call_tool", name="fail", arguments={})
            self.assertIn("remote provider call_tool failed in new", str(error.exception))
            self.assertIn(str(backend.stderr), str(error.exception))
            self.assertIsNotNone(error.exception.__cause__)
            self.assertTrue(backend.stderr.is_file())
            self.assertEqual((await backend.request("list_tools")).tools[0].name, "knowledge_query")
        finally:
            await provider.close()

    async def test_child_exit_returns_evidence_and_close_reaps_the_process(self):
        provider = runtime.Provider("remote", self.root)
        backend = provider.backend(self.selections["new"])
        try:
            with self.assertRaisesRegex(RuntimeError, "evidence:") as error:
                await asyncio.wait_for(backend.request("call_tool", name="exit", arguments={}), timeout=3)
            self.assertIn(str(backend.stderr), str(error.exception))
        finally:
            await provider.close()
        self.assertEqual([process.returncode for process in self.processes], [7])

    async def test_evidence_directory_failure_finishes_startup_with_a_locatable_error(self):
        provider = runtime.Provider("remote", self.root)
        backend = provider.backend(self.selections["new"])
        backend.stderr.parent.parent.mkdir(parents=True, exist_ok=True)
        backend.stderr.parent.write_text("not a directory")
        try:
            with self.assertRaisesRegex(RuntimeError, "evidence:"):
                await asyncio.wait_for(backend.request("list_tools"), timeout=3)
        finally:
            await provider.close()
        self.assertEqual(self.processes, [])


class SelectionTests(unittest.TestCase):
    def test_metadata_cannot_override_an_explicit_different_native_context(self):
        with patch("vaws_coordinator.agent_session.AgentSessions") as store:
            store.return_value.native_context.return_value = {"context_file": "native-context"}
            with self.assertRaisesRegex(ValueError, "differs"):
                runtime.caller_context({"context_file": "another-task"},
                                       {"x-codex-turn-metadata": {"thread_id": "native"}})

    def test_persistent_process_environment_is_not_a_caller_identity(self):
        with patch.dict("os.environ", {"VAWS_CONTEXT_FILE": "parent-task"}), \
             patch("vaws_coordinator.agent_session.load_context") as load:
            self.assertIsNone(runtime.caller_context({}, {}))
            load.assert_not_called()

    def test_task_receipt_wins_over_latest_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, new = root / "old", root / "new"
            old.mkdir()
            new.mkdir()
            record = runtime.task_dir("vaws-" + "a" * 32, root) / "start.json"
            record.parent.mkdir(parents=True)
            record.write_text(json.dumps({"workspace": str(old), "environment": {"receipt": "old"}}))
            latest = root / ".vaws-local/latest-runtime.json"
            latest.write_text(json.dumps({"workspace": str(new), "environment": {"receipt": "new"}}))
            context = {"session": {"id": "vaws-" + "a" * 32}, "context_file": "known", "attachment": {"cwd": str(root)}}
            with patch.object(runtime, "read_receipt", side_effect=lambda key: {"key": key, "python": sys.executable, "receipt": key}):
                self.assertEqual(runtime.selection(root, context).workspace, old.resolve())
                self.assertEqual(runtime.selection(root, catalog=True).workspace, new.resolve())

    def test_only_prepared_linked_worktree_can_supply_a_missing_task_receipt(self):
        from vaws_workspace_update import git
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mother"
            root.mkdir()
            git(root, "init")
            git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture")
            worktree = Path(tmp) / "prepared worktree"
            git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
            context = {"session": {"id": "vaws-" + "a" * 32}, "context_file": "known", "attachment": {"cwd": str(root)}}
            saved = {"key": "prepared", "python": sys.executable, "receipt": "fixed-receipt"}
            mother_selection = root / ".vaws-local/environment-selection" / f"{sys.platform}.json"
            mother_selection.parent.mkdir(parents=True)
            mother_selection.write_text(json.dumps(saved))
            with patch.object(runtime, "saved_ready", return_value=saved) as ready:
                with self.assertRaisesRegex(ValueError, "no prepared workspace"):
                    runtime.selection(root, context)
                ready.assert_not_called()
                context["attachment"]["cwd"] = str(worktree)
                with self.assertRaisesRegex(ValueError, "no prepared workspace"):
                    runtime.selection(root, context)
                ready.assert_not_called()
                native_selection = worktree / ".vaws-local/environment-selection" / f"{sys.platform}.json"
                native_selection.parent.mkdir(parents=True)
                native_selection.write_text(json.dumps(saved))
                self.assertEqual(runtime.selection(root, context).workspace, worktree.resolve())
                child = worktree / "src"
                child.mkdir()
                context["attachment"]["cwd"] = str(child)
                self.assertEqual(runtime.selection(root, context).workspace, worktree.resolve())
                self.assertEqual(ready.call_args.args, (worktree.resolve(),))


if __name__ == "__main__":
    unittest.main()

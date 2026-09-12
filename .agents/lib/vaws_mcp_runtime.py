"""Route a provider call to the immutable environment selected by its task.

This adapter owns stdio connections, not task execution or knowledge behavior.
Native identity selects a prepared receipt; existing task receipts never follow
the latest catalog. Backend stderr and selected inputs remain inspectable.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys

from vaws_environment import read_receipt, saved_ready, PIN_ENV
from vaws_local_state import shared_workspace_root
from vaws_session_state import task_dir


@dataclass(frozen=True)
class Selection:
    workspace: Path
    receipt: str
    python: str
    key: str
    context_file: str = ""


def caller_context(arguments: dict, metadata: dict | None, *, state_dir: str = "") -> dict | None:
    from vaws_coordinator.agent_session import AgentSessions, load_context

    supplied = arguments.get("context_file")
    context = None
    store = AgentSessions(Path(state_dir) if state_dir else None)
    metadata = metadata or {}
    if "x-codex-turn-metadata" in metadata:
        turn = metadata["x-codex-turn-metadata"]
        native = turn.get("thread_id") if isinstance(turn, dict) else None
        if not isinstance(native, str) or not native:
            raise ValueError("native Codex request has no thread_id")
        context = store.native_context("codex", native)
    elif "kimi_code/session_id" in metadata:
        native = metadata["kimi_code/session_id"]
        agent = metadata.get("kimi_code/agent_id", "")
        if not isinstance(native, str) or not native or not isinstance(agent, str):
            raise ValueError("native Kimi request has no session identity")
        context = store.native_context("kimi", native, "" if agent == "main" else agent)
    if context is not None:
        if supplied and str(supplied) != context["context_file"]:
            raise ValueError("context_file differs from the native caller")
        return context
    return load_context(supplied, allow_native_context=False) if supplied else None


def selection(root: Path, context: dict | None = None, *, catalog: bool = False) -> Selection:
    shared = shared_workspace_root(root)
    path = (task_dir(context["session"]["id"], shared) / "start.json" if context else
            shared / ".vaws-local/latest-runtime.json")
    if (context or catalog) and path.is_file():
        result = json.loads(path.read_text(encoding="utf-8"))
        target = Path(result["workspace"]).resolve(strict=True)
        receipt = read_receipt(result["environment"]["receipt"])
    else:
        # No prepared task record is needed for native worktrees. Its actual
        # attachment already names the directory prepared before the Agent.
        target = Path(context["attachment"]["cwd"]) if context else root
        target = target.resolve(strict=True)
        if context:
            from vaws_workspace_update import common_dir, git
            shared_git = common_dir(root)
            target = Path(git(target, "rev-parse", "--show-toplevel")).resolve()
            actual_git = Path(git(target, "rev-parse", "--absolute-git-dir")).resolve()
            selected_file = target / ".vaws-local/environment-selection" / f"{sys.platform}.json"
            if common_dir(target) != shared_git or actual_git == shared_git or not selected_file.is_file():
                raise ValueError("This new task has no prepared workspace. Run the project vaws_start.py entry once, then reuse its context.")
        receipt = saved_ready(target)
    return Selection(target, receipt["receipt"], receipt["python"], receipt["key"],
                     context["context_file"] if context else "")


def provider_command(kind: str, selected: Selection, environment: dict) -> tuple[list[str], dict]:
    from vaws_coordinator_launch import coordinator_environment
    from vaws_knowledge_service import knowledge_server_env

    modules = {"task": ["-m", "vaws_coordinator", "task-server"],
               "remote": ["-m", "remote_dev.mcp.server"],
               "knowledge": ["-m", "vaws_knowledge.server.mcp_server"]}
    env = dict(environment)
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "VAWS_MANAGED_ENV_RECEIPT",
                "VAWS_CONTEXT_FILE", "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT"):
        env.pop(key, None)
    env[PIN_ENV] = selected.receipt
    env["VIRTUAL_ENV"] = str(Path(selected.python).parent.parent)
    env["PATH"] = str(Path(selected.python).parent) + os.pathsep + env.get("PATH", "")
    if kind == "task":
        env = coordinator_environment(env, repo_root=selected.workspace)
    elif kind == "remote":
        env.setdefault("REMOTE_DEV_DEFAULT_USER", "root")
        env["REMOTE_DEV_STATE_DIR"] = str(selected.workspace / ".vaws-local/remote-dev-state")
    elif kind == "knowledge":
        # A user MCP entry may have inherited its mother checkout's config.
        # Target settings must win; model credentials/explicit backend settings
        # are unrelated and remain in the environment.
        for key in ("VAWS_KNOWLEDGE_CONFIG", "VAWS_KNOWLEDGE_PROJECT_ROOTS",
                    "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE"):
            env.pop(key, None)
        env.update(knowledge_server_env(selected.workspace))
    else:
        raise ValueError(f"unknown provider: {kind}")
    return [selected.python, *modules[kind]], env


class Backend:
    """One task environment connection, owned and closed by one async worker."""

    def __init__(self, kind: str, selected: Selection, root: Path, environment: dict):
        self.kind, self.selected = kind, selected
        self.command, self.environment = provider_command(kind, selected, environment)
        digest = hashlib.sha256(str(selected.workspace).encode()).hexdigest()[:12]
        self.stderr = shared_workspace_root(root) / ".vaws-local/mcp/providers" / f"{kind}-{selected.key[:12]}-{digest}.log"
        self.closed = asyncio.Event()
        self.requests = set()
        self.ready = asyncio.get_running_loop().create_future()
        self.worker = asyncio.create_task(self.run())

    async def run(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp import types
        import anyio

        class CancellableSession(ClientSession):
            async def send_request(self, request, result_type, *args, **kwargs):
                # MCP SDK 1.30 assigns this ID before its first await, but does
                # not send notifications/cancelled when its caller cancels.
                request_id = self._request_id
                try:
                    return await super().send_request(request, result_type, *args, **kwargs)
                except asyncio.CancelledError:
                    with anyio.move_on_after(1, shield=True):
                        with suppress(Exception):
                            await self.send_notification(types.ClientNotification(types.CancelledNotification(
                                method="notifications/cancelled", params=types.CancelledNotificationParams(
                                    requestId=request_id, reason="native caller cancelled"))))
                    raise

        try:
            self.stderr.parent.mkdir(parents=True, exist_ok=True)
            with self.stderr.open("a", encoding="utf-8") as log:
                facts = {"provider": self.kind, "workspace": str(self.selected.workspace),
                         "environment": self.selected.key, "python": self.selected.python,
                         "receipt": self.selected.receipt, "stderr": str(self.stderr)}
                log.write(json.dumps({"vaws_provider_start": facts}) + "\n")
                log.flush()
                print(json.dumps({"vaws_provider_start": facts}), file=sys.stderr, flush=True)
                parameters = StdioServerParameters(command=self.command[0], args=self.command[1:],
                                                   cwd=self.selected.workspace, env=self.environment)
                async with stdio_client(parameters, errlog=log) as (reader, writer):
                    async with CancellableSession(reader, writer, read_timeout_seconds=timedelta(seconds=1800)) as session:
                        await session.initialize()
                        self.ready.set_result(session)
                        await self.closed.wait()
        except BaseException as exc:
            if not self.ready.done():
                if isinstance(exc, asyncio.CancelledError):
                    self.ready.cancel()
                else:
                    self.ready.set_exception(exc)
            if not isinstance(exc, asyncio.CancelledError):
                failure = json.dumps({"vaws_provider_failed": type(exc).__name__, "error": str(exc),
                                      "evidence": str(self.stderr)})
                with suppress(OSError):
                    with self.stderr.open("a", encoding="utf-8") as log:
                        log.write(failure + "\n")
                print(failure, file=sys.stderr, flush=True)
            for request in self.requests:
                request.cancel()
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def request(self, method: str, **arguments):
        try:
            session = await asyncio.shield(self.ready)
        except Exception as exc:
            raise RuntimeError(f"{self.kind} provider could not start in {self.selected.key}; evidence: {self.stderr}: {exc}") from exc
        if self.worker.done():
            raise RuntimeError(f"provider stopped; evidence: {self.stderr}")
        request = asyncio.create_task(getattr(session, method)(**arguments))
        self.requests.add(request)
        try:
            return await request
        except Exception as exc:
            raise RuntimeError(f"{self.kind} provider {method} failed in {self.selected.key}; evidence: {self.stderr}: {exc}") from exc
        finally:
            self.requests.discard(request)

    async def close(self):
        pending = list(self.requests)
        for request in pending:
            request.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if not self.worker.done():
            self.worker.cancel()
        with suppress(asyncio.CancelledError):
            await self.worker


class Provider:
    def __init__(self, kind: str, root: Path, environment: dict | None = None):
        self.kind, self.root = kind, root
        self.environment = dict(os.environ if environment is None else environment)
        self.backends = {}

    def backend(self, selected: Selection) -> Backend:
        key = (str(selected.workspace), selected.receipt)
        if key not in self.backends:
            self.backends[key] = Backend(self.kind, selected, self.root, self.environment)
        return self.backends[key]

    async def list_tools(self):
        result = await self.backend(selection(self.root, catalog=True)).request("list_tools")
        tools = []
        for tool in result.tools:
            item = tool.model_copy(deep=True)
            schema = copy.deepcopy(item.inputSchema)
            schema.setdefault("properties", {}).setdefault("context_file", {
                "type": "string", "description": "Existing VAWS context; native hooks normally supply it."})
            if self.environment.get("VAWS_MCP_CLIENT") == "kimi":
                schema["properties"]["context_file"] = {
                    "type": "string", "description": "Copy context_file supplied by this session's native hook or vaws_start result."}
                required = schema.setdefault("required", [])
                if "context_file" not in required:
                    required.append("context_file")
            item.inputSchema = schema
            tools.append(item)
        return tools

    async def call_tool(self, name: str, arguments: dict, metadata: dict | None = None):
        context = caller_context(arguments, metadata, state_dir=self.environment.get("VAWS_AGENT_SESSIONS_DIR", ""))
        if context is None:
            raise ValueError("No native task context was supplied. Pass the context_file from session startup; no workspace or runtime was guessed.")
        selected = selection(self.root, context)
        values = dict(arguments)
        if self.kind == "task" and context:
            values["context_file"] = context["context_file"]
        elif self.kind != "task":
            values.pop("context_file", None)
        backend = self.backend(selected)
        result = await backend.request("call_tool", name=name, arguments=values, meta=metadata)
        result.meta = {**(result.meta or {}), "vaws_provider": {
            "environment": selected.key, "workspace": str(selected.workspace),
            "python": selected.python, "stderr": str(backend.stderr)}}
        return result

    async def close(self):
        await asyncio.gather(*(backend.close() for backend in self.backends.values()), return_exceptions=True)


async def serve(kind: str, root: Path):
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    provider = Provider(kind, root)
    server = Server("vaws-" + kind + "-environment", version="1")

    @server.list_tools()
    async def list_tools():
        return await provider.list_tools()

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        meta = server.request_context.meta
        return await provider.call_tool(name, arguments, meta.model_dump(exclude_none=True) if meta else None)

    try:
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
    finally:
        await provider.close()

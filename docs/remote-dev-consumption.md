# Consuming remote-dev

Status: current

Generic remote operations live in
[`vaws-remote-dev`](https://github.com/vllm-ascend-workspace/remote-dev).
This workspace installs the package and wires its MCP server with ordinary
configuration. remote-dev stays independently usable with explicit
`host` + `port`. See [target-state.md](target-state.md).

## 1. Installing

```bash
uv run --no-project python .agents/scripts/vaws_deps.py sync
uv run --no-project python .agents/scripts/vaws_deps.py status vaws-remote-dev
```

Client setup installs a stable MCP gateway. A new task selects a prepared
immutable environment; the gateway starts `-m remote_dev.mcp.server` there.
A mutable workspace venv is not required.

## 2. What this workspace may inject

| Variable | Value | Why |
|---|---|---|
| `REMOTE_DEV_STATE_DIR` | `<repo>/.vaws-local/remote-dev-state` | Package-owned job records and logs |
| `REMOTE_DEV_SSH_MUX_DIR` | `~/.ssh/vaws-mux` | Shared multiplexed SSH directory |
| `REMOTE_DEV_DEFAULT_USER` | `root` | Ordinary default user |

Do **not** inject `REMOTE_DEV_RESOLVERS` or a global
`REMOTE_DEV_RUNTIME_ENV_FILE`. Coordinator supplies a complete launch
environment on managed executions. Ad-hoc remote-dev calls use the explicit
endpoint the caller already has.

## 3. Skill transport

Skills do not construct SSH options. For ordinary remote I/O they call
`.agents/lib/vaws_remote_dev.py` helpers that wrap `remote_dev.core.ssh_transport`
with an explicit endpoint mapping returned by coordinator or typed by the
user. They do not resolve `session_id` / `machine` through a consumer plugin.

## 4. Client wiring

`uv run --no-project python .agents/scripts/vaws_client_setup.py --client all --apply`
configures the remote-dev provider through `vaws_native_mcp.py`. The gateway
selects the task's fixed environment from explicit context or actual native
metadata, then forwards ordinary remote calls to the package. It strips its
routing-only `context_file` before calling the remote-dev backend. It does not
resolve endpoints, allocate resources or implement a remote-dev resolver.

Supported client hooks provide context; official Kimi passes the existing
`context_file` explicitly. New tasks can use newly prepared components without
manually reconnecting the client, while resumed tasks keep their earlier
selection. The package remains independently usable with explicit endpoints.

Generated configuration preserves user server fields and follows the existing
native interpreter/Windows owner rules, including required WSL environment
forwarding. This does not expand mixed Windows/WSL worktree support. See the
[platform contract](platform-contract.md) and
[native client contract](native-workspace-isolation.md) for those boundaries.

# Consuming vaws-coordinator

Status: current

The local task registry, environment/runtime pool, NPU and port leases, and
the `vaws_*` tools live in
[`vaws-coordinator`](https://github.com/vllm-ascend-workspace/vaws-coordinator).
This workspace imports that package. It does not clone a checkout, does not
own request association, and does not tick the pool.

See [target-state.md](target-state.md) and [dependency-plane.md](dependency-plane.md).

## 1. Public actions

Configure installed clients once with
`uv run --no-project python .agents/scripts/vaws_client_setup.py --client all --apply`.
Native hooks attach the session. Shared project guidance prepares a new task's
editing workspace and fixed component environment through `vaws_start.py`, unless
native setup already supplied them. Preparation binds the selected source roots;
`vaws_session` is not a prerequisite. Resume reuses the original selection.

The stable MCP gateway routes task tools to that task's selected coordinator
package using explicit context or actual native metadata. Codex, Claude, Cursor
and Grok hooks can supply `context_file`. Official Kimi exposes context through
its prompt hook and carries it explicitly in startup and all three VAWS providers'
calls. Never guess identity from cwd or history. See
[MCP and shell context](native-workspace-isolation.md#context-in-mcp-and-shell).

Kimi Code reads hooks from `~/.kimi-code/config.toml` (or `KIMI_CODE_HOME`).
Setup configures project and user MCP providers using the supported official
contract. A personal SessionSetup extension is optional; stock clients do not
receive unknown hook events. The legacy Python `kimi-cli` is a different client.

| Tool | Meaning |
|---|---|
| `vaws_session` | Optional inspection or explicit source-default override; startup already binds the selected editing roots |
| `vaws_run` | Submit `command` plus optional `sources` / `env` / `environment` / `resources` / `topology` / `timeout_seconds` / `service` / `restart`. Skills do not pass `request_id` / `profile_key` / `runtime_id` / a Python path |
| `vaws_execution` | Status, tail, stop, or read the ordinary endpoint of one owned execution |
| `vaws_finish` | Close admission; stop owned executions; keep container, roots, evidence |
| `vaws_message` | Send text to a returned coordination reference or reply_reference; sender and delivery bookkeeping are automatic |

Task MCP/CLI status may reuse a snapshot for two seconds. Use `refresh: true`
or `python -m vaws_coordinator.vaws execution --refresh` for a new observation. Compact results retain
`observation_freshness` (snapshot completion time, age, source and deferred
refresh) plus per-role sampling times. Busy executions return immediately
with the cached observation and mark refresh as deferred. Python
`TaskClient.observe()` remains fresh by default; `refresh=False` permits caching.
These observations do not grant resource access. Tail, target and stop keep
their existing behavior.

Package CLI: `python -m vaws_coordinator.vaws session|run|execution|finish`.
The gateway starts MCP backend `python -m vaws_coordinator task-server` in the
task's selected environment.

`sources` omitted uses the effective task source defaults; `sources={}` runs
without project sources. An explicit map selects sources for that execution.
Names are not restricted to vLLM repositories. Admission captures fixed Git
commits including dirty edits without modifying HEAD or the user's index.
Later local edits and changes to session defaults affect future runs only.
The returned source snapshot identifies the accepted inputs.

Source defaults have explicit provenance: per-run sources override explicit
task-wide defaults, which override this attachment's native-cwd sources.
Resuming or moving a native attachment refreshes its cwd and its automatic
sources without changing other attachments or accepted executions. A saved
mapping without provenance requires explicit rebinding or per-run sources.
The hook recognizes owner-accessible linked worktrees of the configured Git
repository; it does not create worktrees or move the client's cwd. See
[native workspace isolation](native-workspace-isolation.md) for current client
and Windows/WSL boundaries.

Device count defaults to zero. Ordinary commands and CPU compilation do not
reserve NPUs or wait behind an NPU request. NPU workloads explicitly request
`resources.npu_count` or devices. Source-free commands reuse an existing image
interpreter; they do not install vLLM or create a task virtual environment.

For user-authorized sharing, `resources={"devices": [id],
"allow_external_busy": True}` permits external occupancy on one explicit card.
Other managed leases and holds still block admission; device visibility, owned
process completion and service-port release remain required. `topology.host`
selects the host. The serving entry exposes these as `--host`, `--devices` and
`--allow-external-busy` before the vLLM argument separator.

A long-running service uses `timeout_seconds=None`, `resources.service_port=0`
(or an explicit port), and a task-scoped business name (`service`). The same
spec reconnects; a changed spec without `restart=True` is an error. `--relaunch`
is `restart=True`. Status reads package facts. Health/first-token are skill
business checks against the returned endpoint and port once the execution is
actually running.
The serving entry follows preparation and readiness within one
`--health-timeout`; `--no-wait` returns the execution receipt immediately.

Container hostname `/etc/hosts` repair is coordinator environment preparation,
not a per-model launch snippet.

## 2. Environment this workspace injects

| Variable | Value | Why |
|---|---|---|
| `VAWS_AGENT_SESSIONS_DIR` | `<shared workspace>/.vaws-local/agent-sessions` | One local task registry directory for the package |
| `VAWS_COORDINATOR_STATE_DIR` | unset; package default under `.vaws-local/coordinator` | Coordinator-owned pool and machine directory |
| `VAWS_GITHUB_IDENTITY_FILE` | confirmed `.vaws-local/github.json` when present | Bind the initialized user without per-call identity arguments |

There is no workspace `leases.json` and no `session.json` resource authority.

For a Windows checkout shared with WSL, task tools and hooks retain the existing
Windows owner and its selected interpreter. Mounted-drive source and context
paths are normalized by coordinator. Native gateway generation preserves custom
server fields and the required environment forwarding; Linux refuses a state
directory already owned through Windows IPC. This does not imply support for
native worktree setup from a WSL `/mnt` path. See the
[platform contract](platform-contract.md) for interpreter and linked-worktree
boundaries; configuration tests do not replace a real client launch.

## 3. User container

Each host has one persistent container per user, named `vaws-<github-login>`
by default. Initialization supplies the user automatically; SSH still uses root.
Bootstrap, recipe execution, and runtime registration belong to the
coordinator (`python -m vaws_coordinator provision --host ... --image ...
--user ...`). Container-user configuration belongs to coordinator provisioning;
business skills do not create or delete that container, and the
launcher does not copy project `machine-inventory.json` over coordinator
`machines.json`.

## 4. Public TaskClient

Skills import the installed package. They do not keep a workspace request
ledger or paper over unfinished package behavior.

```python
from vaws_coordinator.task_client import TaskClient

client = TaskClient()  # native context/environment; never cwd/history
reply = client.run(
    command='"$VAWS_PYTHON" -m vllm.entrypoints.cli.main serve ... --port "$VAWS_SERVICE_PORT"',
    env=None,
    environment={"recipe": "rc", "python_abi": "cp311", "soc": "ascend910b"},
    resources={"npu_count": 2, "service_port": 0},
    timeout_seconds=None,
    service="vllm",
    restart=False,
)
# reply["state"] may be queued | preparing | waiting | waiting_for_runtime | running | ...
observation = client.observe(reply["execution_id"], "status")
target = client.target(reply["execution_id"])  # live only while running
client.observe(reply["execution_id"], "tail")
client.observe(reply["execution_id"], "tail", role="prefill")
client.observe(reply["execution_id"], "target", role="decode")
client.observe(reply["execution_id"], "stop")
client.finish()
```

Native context supplies the attachment and its source defaults. An explicit
`TaskClient(context_file)` is for an intentional association or a client without
a native context channel. Use a run's `sources` only when that operation needs
different inputs; routine skills do not fill identity or source records.

`preparing` is observable pending state during long environment setup. Skills
retain that phase and the same `execution_id`. Workflows that need a running
service wait through the owner API instead of resubmitting.

A multi-role PD launch uses `topology={"roles": [{"name", "command",
"npu_count"|"devices", "service_port", "host"?, "env"?}, ...]}`. Role `env`
is literal data. The package reserves the full group before any role starts
and returns per-role `target` / `tail` on `observe(..., role=...)` and on
`roles[]`. Unmatched environment constraints stay `waiting_for_runtime`. CLI:
`python -m vaws_coordinator.vaws session|run|execution|finish`. MCP:
`python -m vaws_coordinator task-server`.

## 5. Source publication to an explicit endpoint

Managed `run` prepares its bound sources. It needs neither a session-management
skill nor a separate parity invocation. Use native Git to inspect local worktrees.

For a prepared direct endpoint outside a managed execution, use the installed
package's `python -m vaws_coordinator.parity sync` API in `source-only` mode.
Package help owns its arguments. This operation publishes source to an explicit
endpoint; it does not allocate, install or repair a managed runtime.

## 6. Owned references and observations

`client.resolve_execution(service="vllm")` resolves within the attached task;
`client.observe(service="vllm")` reads the same authoritative execution. A missing
service returns `not_found`, and multiple live matches require an explicit ID.
Neither lookup allocates devices or starts a service. `client.wait(execution_id,
until="running")` ends at running or a terminal failure; `until="released"`
requires terminal state and confirmed resource release. Timeouts retain the last
observed facts.

Changing selected business worktrees updates the session defaults while live
executions retain their accepted inputs and private source views. Each execution
has its own source root. Valid dependency environments and native artifacts can
be reused by content and environment compatibility; Python-only source edits do
not force native recompilation. Native edits invalidate native reuse. A changed
service source snapshot requires `restart=True`, even if its command is unchanged.
Containers and unrelated worktrees are preserved.

After successful preflight, coordinator records an immutable `launch_observation`
with source commits, environment profile and environment digest, native build key,
machine, devices and launch command. The target API retains that receipt after
stop; it does not reconstruct it from a subsequently changed binding. Managed
payloads receive it in reserved `VAWS_EXECUTION_OBSERVATION`. It describes the
attested launch and does not claim to detect later runtime mutations.

The package also writes a `vaws.managed-run.v2` execution record with accepted
source snapshots, environment, process facts and resource release state. Business
workflows may add Run Manifest v1 measurements through the existing package
library. Polling does not recapture mutable worktrees or create fictitious
two-repository evidence for a source-free command.

PD starts directly with `pd_serving.py start --config topology.json`. Its status
and stop operations consume a service or execution reference. Local smoke and
report artifacts record business evidence; coordinator owns topology lifecycle.

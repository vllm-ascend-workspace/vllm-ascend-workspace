# Runtime ownership and contracts

Status: current

This workspace contains vLLM-Ascend project materials, client wiring and business
skills. Installed packages own remote I/O, managed execution, knowledge and fleet
observation. This document describes their current relationship; package help and
the locked source identify detailed arguments and shipped behavior.

## 1. Design decisions

The [nine design principles](design-principles.md) govern tradeoffs. Total cost of
completing the user's goal includes understanding, execution, waiting, diagnosis
and rework. Replace unnecessary interfaces and update their callers directly;
the number of packages or commands is not a design target.

Tools automate bounded operations and their bookkeeping. Agents choose research
methods, interpret observations and decide which existing evidence applies.
Ordinary work does not require a plan, knowledge lookup, manual manifest or
additional summary. These principles are design criteria, not task gates.

## 2. Ownership

| Concern | Owner | Workspace responsibility |
|---|---|---|
| Explicit endpoint files, shell, jobs, artifacts and connection diagnostics | remote-dev | Pass ordinary endpoint and operation arguments; no custom SSH transport or job supervisor |
| Persistent environments, fixed source inputs, devices, ports, execution and recovery | vaws-coordinator | Provide project recipes and business inputs; no allocator, execution state machine or recovery ledger |
| Markdown lookup/capture, indexing, redaction and configured distribution | vaws-knowledge | Project Markdown and native-client wiring; no second knowledge engine |
| Fleet observation | vaws-top | Optional uvx launcher; observation does not allocate devices |
| Local dependency preparation and native-client setup | workspace | Immutable local environments, independent editing copies, generated hooks/MCP and platform boundaries |
| vLLM-Ascend business operations | workspace | Model/workload/topology arguments, readiness checks, measurements and reports |

No runtime package imports this workspace. Coordinator uses remote-dev; knowledge
uses OpenViking and local embedding. An operation stays with its owner; placing
package state under `.vaws-local/` does not transfer ownership. Business reports
describe observations and never authorize access or recover executions.

## 3. Runtime components

### 3.1 Explicit remote operations

An explicit remote endpoint uses remote-dev directly. Its inputs are ordinary
`host`, `port`, `user`, `root` and `cwd` plus the requested operation. It does not
interpret VAWS task names or allocate NPUs. Local native files, Git and shell do
not depend on coordinator or knowledge availability.

See [remote-dev consumption](remote-dev-consumption.md).

### 3.2 Managed executions

Coordinator owns persistent user containers, environment preparation, resource
admission and execution supervision. The host is infrastructure; device code
runs inside the selected Ascend container. Existing user containers and unrelated
worktrees remain intact.

| Action | Caller supplies | Owner does |
|---|---|---|
| session | Native context and optional source defaults | Associate the native task and retain defaults |
| run | Command and needed sources/environment/resources/topology | Fix inputs, prepare or reuse sources and environment, allocate requested devices/ports, launch and supervise |
| execution | Execution reference and status/tail/stop/target operation | Observe or control that owned execution |
| finish | This task | Stop its owned executions while retaining persistent containers, source roots and evidence |

Task identity comes from the native attachment's `context_file` or `VAWS_CONTEXT_FILE`.
A new native session creates a new task; native resume keeps its identity.
Joining another task requires explicit association. Cwd, recent chats and local
reports are not identity or resource-access evidence.

Session sources are defaults for future runs. Admission captures each run's
fixed inputs, including dirty edits; later worktree edits cannot change accepted
inputs. Runs may supply their own source map, including an empty map for generic
commands. Device allocation defaults to zero. Source views are execution-local;
compatible dependency environments and native build artifacts can be reused.

Source and build checks follow their actual input scopes. A business Python edit
does not invalidate unrelated native artifacts. An incompatible ABI, build input
or environment does. Status and tail do not recapture sources or rebuild them.

Named services are task-scoped. Reconnecting to or benchmarking an existing
service uses its execution reference and does not acquire the same NPUs again.
Queued, waiting, starting and uncertain states are not running; a service endpoint
may not exist yet. Business readiness is separate from process state. A terminal
execution is not itself proof that its resources have been released.

The [coordinator contract](coordinator-consumption.md) describes consumer calls.

### 3.3 Knowledge and fleet observation

Knowledge is optional reference, with the contract in section 5.4. Its package
owns local instance lifecycle and index/distribution maintenance. OpenViking is
an internal dependency, not an additional runtime owner.

Fleet monitoring uses the separately distributed vaws-top application. It
reports observations and does not grant resource ownership. See the
[fleet monitor entry](npu-fleet-monitor.md).

## 4. Local workspace and clients

This is a `package = false` uv project. Dependency preparation uses
`uv run --no-project python .agents/scripts/vaws_deps.py sync`; status and doctor
inspect it. Prepared environments have permanent content addresses; an update
prepares a new environment without modifying one used by a running client or daemon.

Initialization wires the official Codex, Cursor, Claude, Grok and Kimi clients
once. All five receive the same short startup guidance in `AGENTS.md`; Claude
references it from `CLAUDE.md` and Cursor receives an always-applied rule.
This entry does not require a Skill. Native hooks attach the session identity
and actual cwd; Cursor preToolUse handles context injection and hook ordering.

A new session reuses an independent native worktree when startup has already
prepared its selected environment. Codex local-environment setup and Cursor
worktree setup can prepare that directory before the Agent starts. Otherwise,
the first repository operation is
`uv run --no-project python .agents/scripts/vaws_start.py --client CLIENT`, with
`--context-file PATH` when the native context is not available to the shell.
It prepares the canonical default branch and its locked components, creates an
independent worktree, binds explicit task sources and saves the selection in the
shared primary worktree's `.vaws-local/tasks/<task-id>/start.json`. Later calls
and resume reuse that selection without checking upstream or preparing again.

The returned workspace is the editing directory: set shell cwd to it, or prefix
commands with `cd`, and use absolute paths for file/search/patch tools. The
client UI and its default cwd can remain at the original project. Official Kimi
uses this path without a personal SessionSetup extension. Native UI selection,
trust and real-client acceptance remain separate from generated wiring; dated
evidence and client boundaries are recorded in the editing-isolation contract.

`vaws_native_mcp.py` and `vaws_mcp_runtime.py` route task, remote-dev and knowledge
MCP calls through the task's selected environment. They resolve an existing
`context_file` or supported native request metadata, never infer identity from
cwd or recent tasks. Official Kimi must carry the returned `context_file` in all
three providers' calls. A long-lived gateway can retain separate backends for
different workspace/environment selections; a newer tool catalog does not
replace an existing task's runtime. These adapters own local connections, while
package owners retain execution and knowledge behavior.

`vaws_client.py CLIENT` is an optional installed-CLI convenience. Shared
Windows-mounted WSL workspaces retain one Windows coordinator/knowledge owner while explicit remote
I/O can use the native Linux provider. Native and managed environments are pinned
independently. User arguments, cwd, stdin and exit codes survive these boundaries.
Native new-worktree setup on a Windows mounted drive requires the Windows owner;
invocation from WSL `/mnt` is not supported by this callback.

Generated configuration contains local paths and remains untracked. Client trust
and approval settings belong to the client and the user's authorization; setup
does not silently grant them. Full-access acceptance is configured explicitly
for the authorized invocation or test directory.

See [platform behavior](platform-contract.md), [editing isolation](native-workspace-isolation.md)
and [dependency preparation](dependency-plane.md). Shared workspace libraries
cover these local boundaries and business reporting; remote lifecycle behavior
stays in its installed owner.

Skills offer business methods on demand. A script may call TaskClient,
explicit remote-dev operations or knowledge APIs. It does not choose remote
Python/CANN by guesswork, allocate leases, replay uncertain admission or infer
identity. Existing useful outputs may support a report when their source,
environment and observations establish the requested claim; the absence of a
planning parent is not itself grounds for rejection.

## 5. Shared contracts

Workspace install/client wiring owns the bounded personal-fork and default-branch
consumption operations in [forks and updates](forks-and-updates.md). GitHub
configuration is distinct from native task identity and shared root login. A new
native directory can adopt the prepared revision during setup; the shared
startup entry prepares a new editing worktree when native setup has not already
provided one with a selected environment. An explicitly selected, prepared
native revision is reused. Component pins are reused and resumed tasks and
running processes retain their selections. There is no periodic updater;
session hooks record the application's actual cwd, while explicit task sources
can point at the separate editing worktree.
The [identity and coordination implementation](identity-and-agent-coordination.md)
uses shared root access and fixed per-user container names. Packages consume the
initialized user and handle container binding, notifications and routine reuse
internally, with no per-task Agent identity or bookkeeping steps. Attribution
and existing task ownership prevent accidental misuse through managed calls;
they do not isolate arbitrary root commands. Reuse on shared
development servers covers existing operator builds, weights and compatible
environments regardless of creator. Tools check conditions relevant to each
artifact; no personal/public classification, sharing ACL or publication flow
is required for this server-local reuse.

### 5.1 Dependencies and runtime identity

`pyproject.toml` declares dependencies and `uv.lock` fixes their source.
Doctor reports installed and loaded runtime identities separately. Pin drift
is reported without blocking observation or cleanup of existing executions.
No consumer handshake or additional compatibility manifest is required.

### 5.2 Run Manifest v1

The implementation is `vaws_coordinator.run_manifest`. Tools record actual
execution and business-measurement facts under untracked local state; Agents
do not fill management fields manually. Git identifies code. Artifact hashes
provide integrity information, without replacing source identity.

### 5.3 Results and observations

`remote-dev.result.v1` describes a remote tool operation. Workspace Result
Envelope v1 describes a business operation. Its compact view points to the full
retained result; it does not create another execution state machine. Missing
facts remain unknown and failures retain their original evidence.

See [agent feedback](agent-feedback-contract.md) and
[runtime observations](runtime-feedback-design.md). Paired measurement tools
retain intended differences, confounders and unknowns in the
[comparability record](comparability-certificate.md); Agent judgment remains
responsible for the scope of a conclusion.

### 5.4 Knowledge

Use `knowledge_query(text)`, `knowledge_explain(ref)` and `knowledge_capture(title, content)`
when they help. A title and non-empty Markdown body suffice; no frontmatter,
fixed headings, labels, evidence form or task association is required by the
knowledge API. The workspace MCP gateway uses the existing native context only
to select the task's package environment. Preserve known conditions, evidence
and uncertainty. Neither lookup nor capture is a
prerequisite or completion step. A search miss does not prove that relevant
experience is absent.

Local and shared results are references, not instructions, approvals or current
environment facts. Review or release does not confer authority. Agents assess
relevance and reuse existing evidence with checks proportional to change.

Shared releases are read-only. Project Markdown lives in `.agents/knowledge/`.
Clients and linked worktrees share the primary worktree's
`.vaws-local/knowledge/service.json`, candidate content and configured model/index
state. Preparation refreshes an owned project Markdown snapshot from the selected
workspace; explicit custom mounts and storage choices are preserved. This shared
reference content is not a task's pinned source snapshot. The package owns
indexing and model lifecycle. Supported hooks reuse the normal task summary,
and manual capture can reuse useful existing text without an extra summary or
publishing follow-up.

Dependency sync and new-workspace preparation ask the selected package to prepare
the shared model and index. MCP maintains readiness and configured shared updates
while alive. Pending knowledge is
reported separately and leaves ordinary tools usable. Windows/WSL clients of
one mounted workspace retain its Windows knowledge owner. Sharing configuration
and state does not imply that every task uses one backend process, or establish
compatibility between concurrently running package versions.

Public sharing follows existing authorization/configuration and uses only a
package-prepared redacted copy. Public review and merge remain human; failed
redaction blocks that export, not local work. Ordinary development needs no fork
or publishing wait. Explicit maintenance may use the optional installed package
skill through `python -m vaws_knowledge skill`.

### 5.5 Endpoints and local state

Coordinator returns ordinary endpoint coordinates for its owned execution.
Remote-dev never receives VAWS object names as endpoint selectors. Runtime state,
private configuration and credentials remain untracked. Source cleanup does not
authorize deleting live resources, unrelated worktrees or user data.

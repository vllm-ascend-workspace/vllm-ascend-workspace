Status: current

# Dependency plane

This scaffold consumes three extracted repositories as **installed packages**,
not git checkouts, not submodules, and not vendored copies. `uv.lock` is the
only pin. A fourth repository, `vaws-top`, is a uvx service and is not an
import.

## Packages

`pyproject.toml` declares the three packages. `[tool.uv.sources]`
names public git+https sources because `vaws-coordinator` depends on
`vaws-remote-dev`, which is not on PyPI.

| Package | Module | Required version | Role |
|---|---|---|---|
| `vaws-remote-dev` | `remote_dev` | from `pyproject.toml` | process-in import + MCP server |
| `vaws-coordinator` | `vaws_coordinator` | from `pyproject.toml` | process-in import + stdio MCP |
| `vaws-knowledge` | `vaws_knowledge` | from `pyproject.toml` | separate knowledge interpreter and MCP |
| `vaws-top` | — | uvx only | fleet dashboard; not imported |

`uv run --no-project python .agents/scripts/vaws_deps.py sync` prepares a locked,
immutable selection in the operating system's user data directory. Normal
clients use a small runtime owner and a separate knowledge owner. Each key covers
its exact dependency closure from `uv.lock`, Python identity, platform,
architecture and selected extras. Updating knowledge alone reuses the runtime
owner. A small immutable receipt fixes both owners for a task and is published
when the runtime is ready. It also saves the verified `pyproject.toml`, `uv.lock`
and generated knowledge tool catalog, each with a SHA256. Knowledge is optional:
its first actual use prepares only its fixed child from those saved inputs,
even if the checkout's dependencies have since changed. This does not replace
the task or project selection. Core commands and status reads never install
knowledge. An explicit
`--group dev` keeps one complete environment for tests that import multiple
components. There is no additional client configuration choice.
CI validates the lock with `vaws_deps.py sync --locked --group dev`. Do not copy those
SHAs into workflows.

Sources may select release tags or validated commit revisions; `uv.lock`
records their resolved commits. Read exact installed/locked identities through
`vaws_deps.py status` instead of maintaining a second SHA table. Acceptance
uses installed packages, including their public APIs and packaged data.

The [workspace updater](forks-and-updates.md) consumes this exact combination.
New tasks select the current local committed revision and reuse a matching
validated preparation. They do not check GitHub identity or sync a Fork. Explicit
`--latest` requests upstream maintenance. A missing locked environment alone
invokes that revision's sync entry; monitor deployment is separate.
Configured Codex/Cursor native worktree setup prepares once after the client
creates a directory and before the Agent starts. It fixes the chosen environment
and that directory's MCP/hook wiring. All five official clients also receive the
same short `AGENTS.md` startup guidance. A prepared independent native worktree
is reused. When independent local editing or managed preparation is needed,
the task calls
`uv run --no-project python .agents/scripts/vaws_start.py --client CLIENT`
with the existing `--context-file PATH` when needed. This bounded entry prepares
the selected fixed revision, creates an independent editing directory, binds
explicit sources and saves the task selection in
`.vaws-local/tasks/<task-id>/start.json` under the shared primary worktree.
Ordinary review and direct remote endpoints do not call this preparation entry.
It requires no Skill or client fork. Resume and repeated calls reuse that
selection; running services keep their loaded environments. There is no periodic
updater or unrelated per-component upgrade. The optional CLI launcher remains
separate from this native-session entry. Dated native acceptance records do not
establish acceptance of a subsequently changed startup or provider path.

## Loader

`.agents/lib/vaws_dependency.py` answers three questions. It does not run
git.

| Call | Meaning |
|---|---|
| `required_versions()` | exact versions from `pyproject.toml` |
| `locked_packages()` | version + commit from `uv.lock` |
| `installed_spec(name)` | version + commit from `importlib.metadata` / `direct_url.json` |
| `inspect(name)` | never raises; `state` is one of the three values below |

`inspect()` assigns exactly one state:

| state | Meaning |
|---|---|
| `missing` | not installed in its selected capability owner, or pyproject/lock cannot describe it |
| `off_spec` | installed, but version or commit does not match pyproject / lock |
| `ready` | installed version and commit match the lock |

`off_spec` warns but does not block execution. `missing` makes capabilities
that depend on the package unavailable. Core gaps use
`uv run --no-project python .agents/scripts/vaws_deps.py sync`. Missing optional
knowledge is prepared by actual use; `sync --capability knowledge` can prewarm
it explicitly. Its absence does not block core readiness.

## Commands

```bash
uv run --no-project python .agents/scripts/vaws_deps.py status
uv run --no-project python .agents/scripts/vaws_deps.py doctor
uv run --no-project python .agents/scripts/vaws_deps.py sync
```

`status` inspects only the three `pyproject.toml` packages. `vaws-top` is
not a package and is not part of `status` or its exit code.

`status` and `doctor` print one JSON object on stdout. Progress goes to
stderr. Doctor defaults to a compact projection with a reference to its complete
Result Envelope v1; `--full` returns the full record. A missing `uvx` degrades
`fleet_observation`; the remedy is
`uv run --no-project python .agents/scripts/manage_monitor.py deploy`.

`sync` is the bootstrap and works before packages are installed. It accepts
`--locked`, groups/extras, `--python`, cache placement, link mode and offline
options; unsupported flags and mutable local dependencies fail explicitly. It
installs at the final path under a per-key OS lock and publishes the ready receipt
last. The selected base Python and store paths are resolved to physical paths so
an interpreter alias change cannot replace a running client's dependencies.
An ordinary core command reads the ready receipt and never installs packages.
`--capability knowledge` additionally prewarms the optional owner. If that
installation fails, the already-ready runtime remains usable; a retry uses the
same child key and lock. No model or index work is part of package sync.

For a proxy whose CA is installed in the operating system trust store, `sync`
also forwards `--native-tls` / `--system-certs` and preserves `UV_NATIVE_TLS` /
`UV_SYSTEM_CERTS` for its uv installer. It also preserves `UV_HTTP_TIMEOUT` and
`UV_CONCURRENT_DOWNLOADS` to tune slow or connection-limited proxies. These
transport settings leave the locked selection and cache key unchanged.
Other `UV_*` overrides remain filtered so they
cannot redirect the environment or change its dependency selection. See the
[Windows proxy recipe](windows-installation.md#corporate-proxies-and-system-certificates).

`VAWS_PYPI_MIRROR` optionally selects an HTTPS PyPI mirror with compatible
`packages/` paths. Cold installs compare small download samples before selecting
the faster source; warm reuse and offline mode perform no probes. Registry and
artifact URLs change only in the temporary installation copy, with the original
versions, hashes and dependency graph retained and `uv sync --locked` enforced.
Canonical receipt inputs and environment keys do not change. Mirror selection and
measured rates are recorded in installation timings without credential-bearing URLs.

`sync` installs or reuses packages and reports only their environment receipt.
It does not import the knowledge service, inspect its readiness, prepare a model
or index, or start maintenance. Knowledge MCP activates those capabilities on
actual use. Explicit knowledge configuration or repair uses
`.agents/scripts/knowledge_setup.py`; its result remains separate from package
installation.

Entry scripts select a prepared platform environment. Interpreter flags and `-m`
module calls survive re-execution; native Windows launches use UTF-8 and retain
child-process ownership. A missing installation returns the bootstrap command
as its remedy. Hooks and the native MCP gateway start in a prepared environment.
Schema v1 single-environment receipts remain readable. Schema v2 receipts fix
runtime and knowledge owners separately; `root` and `python` describe the runtime
owner, while `key` and `receipt` identify the immutable combined selection.
Knowledge commands and summary hooks select and, when needed, prepare the fixed
knowledge interpreter internally. Configuring hooks does not prepare knowledge.
Legacy schema v2 receipts without saved inputs continue using already-ready
children. A missing legacy child reports that its original locked inputs or
ready environment must be restored; it never adopts the current checkout's lock.
The gateway (`vaws_native_mcp.py` / `vaws_mcp_runtime.py`) resolves calls from an
existing `context_file` or supported native metadata and reads the task's fixed
workspace/receipt. It launches task, remote-dev and knowledge package backends
with that receipt's Python and workspace cwd. A native worktree already prepared
at startup can supply its saved selection directly. Managed task calls require
context and preparation. Direct remote-dev and optional knowledge calls without
context use the configured workspace's saved environment, without task-registry,
Git or latest-catalog discovery. Supplied context retains its fixed task selection;
an ordinary checkout's saved environment suffices for these companion calls.

Backends are retained by workspace and receipt. A definite child exit or failed
startup permits a fresh connection on the next new request; no in-flight command
is replayed. A native-scoped tool listing uses that task's fixed environment.
Knowledge listings read its fixed generated catalog without installing packages
or starting a worker, including when the knowledge child is already ready.
The projection is generated from the locked package's literal official `TOOLS`;
it is not a separately authored schema. When updating the knowledge pin, run
`python .agents/scripts/sync_knowledge_catalog.py` in the matching complete
`--group dev` environment. CI checks the file against that installed package
with the same generator (`--check` is also available). Bundle preparation
verifies the projection's package identity and Git revision against `uv.lock`.
Invalid calls are rejected against the fixed schema before optional installation.
When a client retains a newer catalog, calls to an older environment are checked
against its cached supported schema before submission. Unsupported calls return
the actual schema and `submitted: false`, without changing the task's version.
A long-lived gateway can serve
tasks with different fixed environments; later syncs or a newer catalog selection
do not replace their imported dependencies. Tool results expose the selected
environment, workspace, Python and backend stderr path. Official Kimi passes the
returned `context_file` to task tools; companion tools accept it optionally.
Clients without supported native metadata can supply their existing context
through hooks or tool arguments when using a task's selected environment.

In a checkout shared by Windows and WSL, managed tasks and knowledge use the
prepared Windows owner. A managed CLI switches owner before reading stdin or
performing work; local analysis and explicit endpoint I/O stay native. Existing
per-environment Windows launch aliases remain immutable. The new gateway does
not extend the supported mixed-OS worktree or owner boundaries.
If its knowledge child is missing, WSL hands installation to that bundle's
Windows runtime and original saved lock. It neither constructs a Linux child
for the Windows selection nor changes the checkout's environment selection.
Run `uv run --no-project python
.agents/scripts/vaws_client_setup.py --client CLIENT --project PATH --apply` to
generate configuration. Codex setup replaces each selected VAWS MCP entry with
the current interpreter, gateway and environment. Its old launcher, environment
overrides and server-specific options are discarded; other server names and
top-level client settings are preserved. Applying the plan saves private backups.
`--task-only` limits replacement to the task server. Other clients continue to
preserve custom fields and foreign launchers. The [platform contract](platform-contract.md)
describes the common native client entry. The [Windows installation guide](windows-installation.md)
covers caching and offline transfer.

## Capabilities

`.agents/lib/vaws_capability.py` keeps these capabilities. A
`missing` package makes the capabilities that list it unavailable.
`fleet_observation` is not a package: it needs `uvx` plus the `vaws-top`
release wheel.

| Capability | Depends on |
|---|---|
| `remote_endpoints` | `vaws-remote-dev` |
| `task_pool` | `vaws-coordinator` |
| `host_npu_authority` | `vaws-coordinator` |
| `fleet_observation` | `uvx`, `vaws-top` |
| `shared_knowledge` | `vaws-knowledge` in its selected owner, with packaged corpus |

## Shared knowledge corpus

The installed `vaws-knowledge` package provides the engine and a bootstrap
corpus. Actual knowledge use prepares its local model and index on demand. For an
explicit retry or configuration change, `uv run --no-project python .agents/scripts/knowledge_setup.py`
uses the same package preparation entry. New setup enables local knowledge and
shared downloads; it does not create a fork or enable public contribution.
Existing publishing configuration is preserved. Community participation is chosen
only with `vaws_init.py apply --community enabled` or `disabled`. The knowledge
setup entry has no separate contribution switch. `--repository OWNER/NAME` changes
the corpus and retains publishing only when the current community decision,
confirmed identity and existing publishing configuration authorize it; otherwise
the corpus is configured for reference reads. The owner rechecks live consent
before creating a contribution fork or performing an external write.
Then refresh selected clients with `vaws_client_setup.py --apply` to install the
MCP wiring and supported final-response hooks.

Clients and linked worktrees use the shared primary worktree's
`.vaws-local/knowledge/service.json`. Preparation refreshes its owned project
Markdown snapshot from the selected workspace and retains custom mounts,
candidate storage, backend settings and contribution choices. Default model and
index state also live under this shared knowledge directory; they are not copied
per editing worktree. Gateway routing fixes the package interpreter for each task,
while the service configuration and reference content are shared. Package owners
retain index maintenance, locking and model lifecycle; shared paths alone do not
prove compatibility across concurrently running package versions.

Knowledge MCP activates internal model/index maintenance only on a valid query
or successful capture when needed, independent of public contribution.
Initialize, tools/list, ping, invalid requests and unused EOF do not start a
backend or maintenance/network work. Explain reads Markdown, and automatic
summary capture remains a local non-indexing write. An unused provider therefore
does not prepare knowledge for a plain PR review or explicit remote operation.
Maintenance respects existing `next_check` and `next_verify` receipts; the
verification interval is 3,600 seconds while maintenance is active and usable.
A stopped/unused provider resumes overdue work on its next actual use; backend
failures may defer repair. Explicit prepare retains its verification behavior.
See the [knowledge contract](target-state.md#54-knowledge) for vector-loss bounds.

Shared synchronization is enabled by default, runs with use-driven maintenance,
and consumes
GitHub Releases from `vllm-ascend-workspace/vaws-knowledge-corpus`. Shared updates
verify the exact Git identity, model files and dense OVPack before switching;
project and candidate knowledge stay local. Knowledge PRs currently require
human review and merge.
Only configured, authorized public contribution submits redacted public copies.
Use `vaws-knowledge publishing status --config PATH` to inspect retries and the
active sync result. Native hook trust remains managed by each client.

The [knowledge contract](target-state.md#54-knowledge) keeps lookup and capture
optional and uses ordinary Markdown. This setup is not a prerequisite or a
maintenance sequence for ordinary tasks; a knowledge outage does not block
independent development.

For one Windows-mounted workspace, knowledge MCP, preparation and summary hooks
use its Windows interpreter and native paths from both Windows and WSL. A missing
Windows interpreter leaves preparation pending instead of starting another
Linux database process in the shared state directory. An independent Linux
workspace uses its Linux environment. Existing generated knowledge launchers
migrate to this owner; custom launchers and storage choices are preserved.

All five clients use the knowledge MCP tools. Configured Codex, Claude Code,
Cursor and Grok adapters reuse their native final-response text. Kimi Code
currently supplies no final text in `Stop`, so it receives MCP/session wiring
without automatic summary capture. No client needs a second summary or transcript
scan to complete a task; the package's publishing documentation records the
event fields and native sources.

## What was removed

- hand-written pin JSON files and the dependency-v1 schema
- git checkout states and locator helpers
- former checkout-root environment variables and the off-pin override
- the `bootstrap` subcommand
- the local launcher that shadowed the `remote_dev` package name

## Optional package skill

`python -m vaws_knowledge skill` through the configured interpreter reads the optional maintenance skill
without starting OpenViking. `--install-dir <client-skill-directory>` installs
that same packaged resource for native discovery. Workspace does not keep a
second canonical copy or require curation for ordinary capture.

Doctor also reads the running coordinator identity without launching a daemon.
Its loaded version/commit can differ from the installed package after sync; use
`vaws-coordinator daemon --action restart-if-idle` after owned executions and
leases finish. Task MCP responses carry their backend's startup identity. A newer
installed or catalog version does not hot-upgrade a task bound to an older
receipt. Missing loaded identity remains unknown.

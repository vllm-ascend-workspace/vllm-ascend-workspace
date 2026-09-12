# Workspace skills and client wiring

The workspace keeps project materials, installation/client wiring and business
skills. Runtime behavior belongs to the four installed components; see
[AGENTS.md](../AGENTS.md) and [target-state.md](../docs/target-state.md).

- `skills/repo-init/` initializes or repairs workspace configuration and clients.
- `scripts/workspace_forks.py` configures verified personal GitHub forks without
  a Skill or installed runtime.
- `scripts/vaws_client_setup.py` configures installed clients and shared project
  guidance once. Official Codex, Cursor, Claude, Grok and Kimi use
  `scripts/vaws_start.py` for one new-task preparation; existing native worktree
  callbacks can supply an already selected workspace and environment.
  Startup binds sources and returns the editing root. Resume retains the task,
  directory and environment. See [client boundaries](../docs/native-workspace-isolation.md)
  and [current acceptance progress](../docs/unified-session-validation-2026-09-13.md).
- `scripts/vaws_native_mcp.py` routes the three VAWS providers by exact task
  context to the selected component environment. It does not pick a task from
  cwd or recent activity. The original client connection can serve new tasks
  while earlier tasks retain their component selection.
- `scripts/vaws_client.py` is an optional convenience for launching installed
  CLIs; ordinary native sessions do not require the Agent to call it.
  `scripts/workspace_update.py` provides explicit maintenance using the same
  updater; see [forks and updates](../docs/forks-and-updates.md).
- `skills/npu-fleet-monitor/` starts, checks or stops the local uvx monitor.
- Other `skills/` directories add vLLM-Ascend business methods such as serving,
  measurements, profiling and debugging. Their `SKILL.md` files are the source
  of truth; the client skill catalog provides discovery.
- `scripts/vaws.py` optionally forwards session/run/execution/finish to coordinator.
  Startup binds the selected editing root; explicit source overrides remain available.
  Use native Git for source inspection.
- Provisioning and native task identity remain in coordinator.
- Direct remote I/O and optional source publication use their installed owner
  APIs. Managed runs prepare their bound sources internally.
- Knowledge lookup and capture use the package tools. `scripts/knowledge_setup.py`
  retries package preparation or changes the requested sharing configuration.
  Dependency sync prepares knowledge; MCP maintains it while alive. Linked
  worktrees share configuration, content and reusable model/index state.
  New setup keeps public contribution disabled, and preserves existing choices.

Knowledge is optional reference, using ordinary Markdown with a title and body.
Keep known conditions, evidence and uncertainty in the text. Lookup, capture and
public review add no required steps to ordinary tasks; configured hooks reuse
the existing summary. The [knowledge contract](../docs/target-state.md#54-knowledge)
describes the shared conventions.

For explicit knowledge maintenance, the package skill is available through its
configured interpreter with `python -m vaws_knowledge skill`.
The optional `curate-knowledge` skill is shipped by that package, not maintained
in this workspace. Native clients can install it into a chosen skill directory
with the package command's `--install-dir` option. Ordinary lookup, capture and
task completion require no curation skill.

## Maintaining skills

Keep names/descriptions specific enough for discovery. Put task decisions and
common entry points in SKILL.md; put detailed formats and conditional procedures
in linked references. Do not duplicate package APIs or add a compulsory
management workflow before business work.

`.claude/skills/` contains generated routing shims. ModelScope's Trae package is
also generated; remaining Trae stubs link to their canonical skill. Regenerate
with `uv run --no-project python .agents/scripts/sync_claude_skills.py` and verify with `--check`.
`uv run --no-project python .agents/scripts/skill_catalog.py --help` lists catalog checks.

Local control-plane tests belong in `.agents/tests/` or the owning business
skill. Preserve caller coverage when moving code out of a retired skill; device
execution and model tests still require remote Ascend hardware.

---
name: repo-init
description: Initialize this workspace or repair its dependency, fork and native-client configuration. Use for workspace setup requests; a standalone Git or package command does not require the full workflow.
---

# Initialize the workspace

Complete the requested setup using existing configuration. Broad initialization
usually needs submodules, dependencies and installed clients; a narrow repair
uses only the relevant operation. Ordinary new sessions receive project startup
guidance without invoking this skill.

Use `uv run --no-project python` before local script paths on Windows, macOS,
Linux and WSL. Reuse ready dependency environments and conclusive probe results.

## Inspect only what is missing

`scripts/repo_init_probe.py --compact` reports platform, GitHub authentication,
submodules and remotes without creating identity or setup choices. Add
`--include-forks` when personal fork discovery helps the requested topology.
Known state does not require another complete probe.

Preserve extra remotes, dirty sources and user choices. Keep `.gitmodules` on
community URLs. Task identity and resources belong to native attachments and
coordinator; do not ask for machine usernames or task aliases.

## Complete the relevant setup

- Establish GitHub authentication when the requested operation needs it.
  Initialize submodules before configuring their remotes: Git in an empty
  submodule directory can otherwise resolve to its parent repository.
- For requested CI-pinned vLLM alignment, use `resolve_vllm_ci_pin.py` and report
  the source of the ref. Preserve dirty submodules and intentional pins.
- On first setup ask once for the personal GitHub ID; the authenticated login
  is a suggestion, not consent. Reuse a saved confirmation. Run
  `.agents/scripts/workspace_forks.py --github-user USER --apply` to create or
  reuse verified personal forks with personal `origin` and official `upstream`.
  Omitting `--apply` returns a plan. Conflicting primary URLs require an explicit
  replacement with a backup; existing branches, commits and dirty files remain.
- Prepare missing dependencies with `.agents/scripts/vaws_deps.py sync`.
  `doctor` helps unresolved capability or pin questions; it is not an extra step
  after an already conclusive result.
- Run `.agents/scripts/vaws_client_setup.py --client all --apply` once to detect
  installed Codex, Cursor, Claude, Grok and Kimi clients and configure providers,
  hooks and short project guidance. It preserves unrelated configuration and
  records changes under `.vaws-local/client-initialization.json`. A targeted
  repair uses `--client CLIENT --apply`. Native trust stays with each client.
  Configuration alone is not live acceptance; use one small real task when
  verifying the requested integration.

The shared guidance makes a new task prepare its workspace once through
`vaws_start.py`; an independent native worktree whose setup already selected an
environment is reused directly. The returned directory becomes the editing root,
with shell cwd and absolute file paths. Native UI Worktree modes are optional
optimizations, and official Grok/Kimi need no personal binary. Resume keeps the
original task, directory and environment with no preparation or update. See
[client boundaries](../../../docs/native-workspace-isolation.md).

New tasks use the mainline workspace's locked component combination. The stable
MCP gateway selects each task's fixed environment without a manual reconnect.
Official Kimi uses the hook's `context_file` for startup and all three VAWS MCP
providers; other clients inject it through their supported hooks.

Dependency preparation also prepares knowledge. Linked worktrees share its
configuration, project snapshot, candidate store and reusable model/index state.
Pending knowledge work leaves ordinary tools usable. `knowledge_setup.py` is for
an explicit preparation retry or configuration change; existing custom roots and
publishing choices are preserved, and new setup disables public contribution.
Ordinary development needs no maintenance sequence or additional summary.

Report actual changes, relevant verification and unresolved limitations. Reuse
existing authorization and ask only for missing information affecting the result.
Read [command recipes](references/command-recipes.md) for individual operations
and [the platform contract](../../../docs/platform-contract.md) for Windows/WSL owners.

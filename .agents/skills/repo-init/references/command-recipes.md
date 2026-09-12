# Repo-init command recipes

Prefer the helper scripts in `scripts/` and `.agents/scripts/` when possible.

## Initialize installed clients together

```text
uv run --no-project python .agents/scripts/vaws_deps.py sync
uv run --no-project python .agents/scripts/vaws_client_setup.py --client all --apply
```

The second command detects installed clients and configures providers, hooks and
short project guidance. Native Worktree preferences are optional optimizations;
o personal Grok/Kimi build is required. The primary worktree keeps
`.vaws-local/client-initialization.json` for inspection. Trust remains native,
and live acceptance requires an actual client task. Ordinary tasks do not rerun
initialization or inspect its record as a prerequisite.

## New native task

Project guidance supplies this entry when native setup has not already prepared
an independent worktree and selected environment:

```text
uv run --no-project python .agents/scripts/vaws_start.py --client CLIENT
```

Use the actual client name: `codex`, `cursor`, `claude`, `grok` or `kimi`.
Official Kimi adds `--context-file PATH` from its existing hook. The returned
workspace is the shell cwd and root for absolute file/search/patch paths;
sources and environment are already bound. Kimi also passes that context to all
three VAWS MCP providers. Resume uses the earlier directory and environment
without running preparation. See [client boundaries](../../../../docs/native-workspace-isolation.md).

## Optional Codex native hooks

```text
uv run --no-project python .agents/scripts/vaws_client_setup.py --client codex --codex-global-hooks --apply
```

This installs stable user hooks scoped to the current Git worktree family and
migrates its generated project hooks. Review native hook definitions once;
setup does not grant trust. If native Worktree mode and the VAWS local environment
are selected, the client can perform preparation before the first Agent action.
The shared new-task entry also supports ordinary project sessions.

## Probe

Windows, macOS, Linux and WSL use the same entry:

```text
uv run --no-project python .agents/skills/repo-init/scripts/repo_init_probe.py --compact
```

Add `--include-forks` when personal fork discovery is needed. The probe does not
create workspace state or ask setup questions.

## Submodules

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

## Resolve CI-pinned vLLM ref

Use this after `vllm-ascend/` is populated and the user chose CI-pinned
alignment:

```bash
uv run --no-project python .agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py --vllm-ascend-dir vllm-ascend
```

Then check out `vllm/` at the returned `vllm_ref`. The resolver prefers
`vllm-ascend/.github/vllm-main-verified.commit`; older checkouts may fall back to a
workflow `vllm_version` or docs `main_vllm_commit` value.

## External dependency plane

The three in-process packages are not submodules. Install them with `uv run --no-project python .agents/scripts/vaws_deps.py sync`.
Use `doctor` when a capability or dependency pin needs diagnosis.

```bash
uv run --no-project python .agents/scripts/vaws_deps.py sync
```

`uv.lock` is the only pin. The packages are public git+https. `uvx vaws-top`
is a separate service. Successful `sync` also prepares the knowledge model and
index through the installed package. Its JSON reports `knowledge.ready` separately
from package installation; pending knowledge does not block ordinary tools. See
[docs/dependency-plane.md](../../../../docs/dependency-plane.md).

The default per-user uv cache and environment store can be reused by independent
workspaces. To prepare dev dependencies:

```text
uv run --no-project python .agents/scripts/vaws_deps.py sync --locked --group dev
```

For offline preparation, exact tool/lock checks and restoring a transferred
cache, use [Windows installation](../../../../docs/windows-installation.md).
Keep the default knowledge package installed; cache placement does not require
changing forks or native client settings.

## Quiet main comparison

```bash
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo .
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py compare-main --repo vllm-ascend
```

## Remote configuration

Personal fork setup has a general entry that does not depend on this skill:

```bash
uv run --no-project python .agents/scripts/workspace_forks.py --github-user USER --apply
```

Without `--apply` this prints the plan. A missing first-use ID returns
`needs_github_user` and the authenticated login as a suggestion. Input must match
the authenticated personal User account; confirmed login and GitHub's numeric
user ID are stored only in `.vaws-local/github.json` and reused. The stable ID
keeps a renamed account associated with its prior confirmation. This file is
client configuration, not server authentication.

Default scope is workspace, `vllm` and `vllm-ascend`; `--repo workspace` limits
it to the scaffold. Missing forks are created under the authenticated user,
then checked again for exact identity, personal ownership, `fork=true` and the
official parent/source network. Organization forks, URL redirects and unrelated
same-name repositories are rejected. Missing submodules initialize at the
recorded gitlinks. Initialized checkouts retain their branches, commits and files.

The command sets personal `origin` and official `upstream`, preserving other
remotes and already correct fetch/push protocol splits. Multiple URLs and
conflicting explicit push URLs stop before mutation; after reviewing the plan,
`--replace-primary-remotes` explicitly replaces those primary URLs and records
their prior values under `.vaws-local/fork-setup-backups/`. `.gitmodules` stays
on community URLs. The low-level `repo_topology.py configure` is reserved for
official upstream URLs; personal-fork configuration uses the verified entry.

## Branch tracking

```bash
uv run --no-project python .agents/skills/repo-init/scripts/repo_topology.py ensure-main   --repo vllm-ascend   --remote origin
```
## Knowledge setup and background updates

For an explicit knowledge preparation retry or configuration change, run
`uv run --no-project python .agents/scripts/knowledge_setup.py`. Default setup prepares local knowledge
and shared downloads, preserving existing publishing choices. Add `--contribute`
only to enable authorized public contribution; `--read-only` disables contribution
while keeping shared downloads. `--repository OWNER/REPO` changes the shared
corpus without enabling contribution. Only contribution setup needs a GitHub
login and fork; no token belongs in tracked files.
Refresh the selected clients with `vaws_client_setup.py --apply` afterward.
Related worktrees reuse the shared configuration, project snapshot, candidate
store and model/index state. Task-specific MCP backends maintain knowledge while
alive; they do not require one new corpus or model download per task. Windows and WSL use the Windows knowledge owner for the same mounted
workspace; a missing Windows interpreter is reported as pending. Independent
Linux workspaces use their own environment. Knowledge PR review and merge remain
manual. Ordinary development requires no maintenance commands.

## Optional isolated CLI entry

This launcher remains an optional terminal convenience. Ordinary native sessions
use the project startup guidance and do not require it.

```text
uv run --no-project python .agents/scripts/vaws_client.py codex
uv run --no-project python .agents/scripts/vaws_client.py kimi --workspace PATH
```

The default creates an independent Git copy before the first native tool runs.
An explicit existing workspace is reused. Setup pins prepared environments and
preserves the native client's session identity. See the
[platform contract](../../../../docs/platform-contract.md).

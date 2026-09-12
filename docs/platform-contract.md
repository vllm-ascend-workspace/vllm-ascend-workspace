# Native platform contract

Status: current

Windows, macOS and Linux use the same task concepts, argument names and result
schemas. Platform differences belong to the implementation of paths, process
ownership and dependency preparation. They do not require separate Agent
workflows for PowerShell, WSL, bash or zsh.

## Entry points

The portable bootstrap prefix is `uv run --no-project python`. It avoids relying
on whether a machine names its Python command `python` or `python3`, and does not
ask the user's shell to activate a virtual environment.

```text
uv run --no-project python .agents/scripts/vaws_deps.py sync --group dev
uv run --no-project python .agents/scripts/vaws_deps.py status
```

Ordinary use starts in the native client. One-time initialization uses
`vaws_client_setup.py --client all --apply` to detect installed clients and
configure providers, hooks and project guidance together. Supported native
worktree preferences are optional optimizations. Writing configuration does not
grant native trust or prove a real client task passed.
Official Codex, Cursor, Claude, Grok and Kimi receive the same short startup
guidance in `AGENTS.md`, with a Claude reference and an always-applied Cursor rule.
No Skill or personal client extension is required for this entry.
Codex local-environment setup and Cursor worktree setup run after
the client creates the new directory and before the Agent operates in it. The
callback checks upstream once, advances an eligible new checkout and fixes its
dependency environment and MCP/hook wiring. SessionStart attaches the native
identity and actual cwd to VAWS automatically. Cursor preToolUse injects context
internally and can establish that same attachment if it runs first.
Codex initialization uses `--codex-global-hooks` and native review of the fixed
user definitions. These hooks only handle the configured Git worktree family,
read the actual directory's saved environment and retain their source and command
across new worktrees. Setup preserves native trust and unrelated custom hooks.

A new session reuses an independent native worktree with a selected environment.
Otherwise its first repository operation is
`uv run --no-project python .agents/scripts/vaws_start.py --client CLIENT`
(`codex`, `cursor`, `claude`, `grok` or `kimi`), adding `--context-file PATH` when
the native context is unavailable to the shell. This entry prepares the canonical
default branch and its locked components, creates an independent worktree, binds
explicit sources and saves the task's selection in the shared primary worktree's
`.vaws-local/tasks/<task-id>/start.json`.

Native Local chats can retain their UI/default directory; session hooks cannot
move the parent application. Agent shell operations use the returned workspace
as cwd, or an explicit `cd` prefix, and file/search/patch tools use absolute paths
under it. Resume and repeat calls reuse the original task, workspace and selected
environment without another preparation or update. There is no periodic watcher.
Historical setup tests and real-client evidence are recorded in
[native client acceptance](native-client-validation-2026-09-12.md);
they do not certify later startup or gateway changes. Native client environment
selection and MCP enablement are one-time initialization choices.
Other client capabilities and sources are listed in
[native client boundaries](native-workspace-isolation.md). Explicit maintenance
can use apply; see [forks and updates](forks-and-updates.md).

`vaws_client.py CLIENT` remains an optional launcher for installed CLIs. It
creates an independent editing copy or reuses an explicit `--workspace PATH`;
native arguments follow `--`. Resuming through this convenience launcher needs
the original path, because the launcher does not guess it from a resume ID.
It sets its child process cwd, PATH and VIRTUAL_ENV before launch. Those child
environment guarantees do not imply that a GUI setup subprocess can change
its parent's environment. Ordinary native sessions need no Agent launcher call.

## Commands and files

Internal commands pass an argument vector, explicit cwd and environment to the
process API. Spaces, Unicode, quotes and shell punctuation remain data. File
copying, directory creation and local state use filesystem APIs. Internal process
launches need no user shell activation, `cd && ...`, or PowerShell-to-bash string
translation. An Agent whose native shell tool lacks a cwd argument may use the
shell's own directory-change syntax for the returned editing workspace.

An explicitly supplied shell script still has a declared interpreter. Remote
Linux shell commands use the remote bash contract on every client platform,
including the same runtime initialization and command scope. Arbitrary shell
programs cannot be translated between PowerShell and bash without changing
their meaning.

Owned attached local processes use Windows Job Objects or POSIX process groups.
Timeout and close include their still-owned descendants even when the original
parent has already exited. Unrelated processes remain outside this scope.
Cleaning up a local SSH process does not by itself prove a remote execution has
stopped; managed execution status remains with its execution owner.

## Paths and environments

Native paths are converted at the boundary to the process that will read them.
A shared Windows-mounted WSL workspace has one Windows coordinator and knowledge
owner. It must use a verified Windows environment, not a path constructed using
Linux's Python ABI. Native new-worktree setup on a mounted Windows workspace
must run with the Windows owner. Invoking this callback from a WSL `/mnt` path
is currently unsupported; this change does not claim mixed-OS linked-worktree
support. The optional CLI's independent editing copies have ordinary `.git`
directories, avoiding absolute linked-worktree pointers from the other OS.
Empty uninitialized submodules remain uninitialized; source is not fetched merely
to start a workspace editing task. Initialized submodules keep independent state.

Prepared dependency environments have content identities including lock inputs,
effective dependency selection and the actual Python platform/ABI. Published
environments are immutable. Updating dependencies prepares another environment;
it does not upgrade the interpreter of an already running client or daemon.
Generated hooks and the native MCP gateway start in a prepared environment.
The gateway selects each task, remote-dev or knowledge backend from the task's
fixed workspace receipt, using an existing `context_file` or supported native
request metadata. It never selects identity from cwd or a recent task. Official
Kimi carries `context_file` explicitly in all three providers' calls. Different
tasks can use different retained backend processes through one gateway; a newer
catalog does not replace an existing task's loaded packages. The native client
also pins its managed owner environment independently, so a later lock edit in a
WSL shell cannot switch the owner used by an already running task. Project-relative
launch aliases are also per identity and never redirected to another version.

Setup and ordinary task work have different costs: explicit sync prepares missing
dependencies. Native new-worktree setup may prepare an upstream update before
the Agent starts; the shared startup entry performs that bounded preparation
when no prepared native worktree is available. Resume resolves the existing
task selection. Knowledge preparation uses the shared primary worktree's service
configuration and model/index state. It remains optional after sync and does not
invalidate a completed package environment when unavailable. These gateway and
storage rules do not add mixed-OS linked-worktree support or change the verified
Windows owner requirement above.

## Verification boundaries

Native Windows and macOS CI exercise the same behavior tests on Python 3.13.
Linux CI covers the declared minimum Python 3.11. WSL is supplementary evidence
for Windows/Linux interoperability; it is not a substitute for macOS execution.
Behavior cases cover literal arguments, UTF-8, cwd, process lifetime, independent
indexes and fixed environment selection. Duplicate help-string checks and
repeated whole-repository scans are not substitutes for those behaviors.

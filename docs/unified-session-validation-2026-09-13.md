# Unified native-session startup acceptance

Status: dated validation evidence — 2026-09-13, official CLI acceptance completed

## Tested implementation

[Workspace PR #154](https://github.com/vllm-ascend-workspace/vllm-ascend-workspace/pull/154)
merged as `16b6dc9907a5d6268a8e6adabc17d0783e2552b0`. Its final implementation
head `767829ad13e956d7e68b693f9161f5d65cf2b583` passed complete consumer CI on
Ubuntu/Python 3.11, Windows/Python 3.13 and macOS/Python 3.13
([run](https://github.com/vllm-ascend-workspace/vllm-ascend-workspace/actions/runs/34713221298)).

The selected immutable component environment was `794caf986c1d…`:

| Component | Selected revision |
|---|---|
| vaws-coordinator | `831a0f568024e982b88bfba0d72f3debf542f723` |
| vaws-knowledge | `f928dc37c6f34688d0cfa9a9b4f6d3e882958959` |
| remote-dev | `2de5cc32c5f3dd517e698cadfb1f9ed23589b1aa` |

Coordinator PR #26 and knowledge PRs #26/#27 passed their own CI before the
consumer pins were adopted. Earlier sessions selected workspace `4a1f28e…`
and environment `91a33bf6008a…`; new sessions selected the merged canonical
main and its new environment automatically.

## Ordinary new-session tasks

Each official CLI launched from the configured primary project with an ordinary
business prompt: read the first design principle, write two sentences to one
`notes-CLIENT.txt`, query project knowledge, and report directory/source/runtime.
The prompt did not name the startup command, request a worktree, or supply a
replacement system prompt. Existing authentication and model preferences were
reused. The clients independently followed generated project guidance and
executed the startup entry before repository work.

| Client | Official version | Selected worktree and latest environment | Automatic final-summary capture |
|---|---|---|---|
| Codex CLI | 0.153.4 | Passed | Passed |
| Cursor CLI | 2026.09.08-6caf4ff | Passed | Passed after configuration repair |
| Claude Code | 2.1.269 | Passed | Passed |
| Grok | 1.0.30 (`04b7ffed98c6`) | Passed | Passed |
| Kimi Code | 0.42.0 | Passed | Passed |

Grok and Kimi ran through their restored default official executables. SHA-256
was checked against the downloaded official files; earlier personal builds were
retained only as backups. The invocation records retain executable identities
and hashes, rather than relying on a version string alone.

All test notes were written only inside their selected worktrees; the primary
checkout retained its tracked contents and contained none of the test notes.
Real MCP provider output identified the selected workspace, Python and component
revision. Claude's raw `vaws_session {}` and knowledge calls with only business
arguments received the correct native context through PreToolUse. Kimi's
schema required its existing context, and its first knowledge call supplied it
without a missing-argument retry. A separate ordinary Kimi inspection task called
`vaws_session` successfully and observed the selected source and new coordinator
runtime, without file changes or remote execution.

Cursor also succeeded with raw `vaws_session {}` and knowledge arguments limited
to text and limit. After configuration repair, the real CLI's completed native
answer was captured once, with matching session identity and identical body.

The clients ran concurrently to exercise the shared preparation lock. Waiting
remained inside the startup process rather than returning a lock conflict to the
model. End-to-end times include model work and this deliberate contention;
they are not isolated startup benchmarks.

## Runtime and knowledge evidence

A single live knowledge gateway handled old-task, new-task, then old-task calls
through two real component processes. The old task retained environment
`91a33bf6008a…` and its original package revision; the new task used
`794caf986c1d…` and `f928dc3…`. Repeating the old call reused its original
backend, and both workers closed cleanly. This tests selected-runtime routing
without treating a connected MCP panel as execution evidence.

The worktrees shared the primary knowledge configuration, index and compatible
model services. Existing service processes remained alive throughout acceptance;
there was no per-worktree model download or knowledge store. Grok's automatically
captured final answer was retrieved through both `knowledge_query` and
`knowledge_explain` from the separate Codex task, with the original Grok source
identity preserved. No manual capture or index repair was used for this check.

Kimi briefly reported pending background index maintenance while still returning
eight results. Its next ordinary query was no longer degraded. The raw status
was retained rather than interpreted as a complete first search.

## Observed boundaries and repairs

Cursor's first final run exposed an upgrade bug: a pre-existing `sessionEnd`
identity hook caused configuration merging to omit the newly added summary hook.
Fresh-configuration tests had not exercised that migration. The repair merges
each desired hook separately and adds upgrade/idempotence coverage. The official
CLI's `sessionEnd` transcript path supplies the completed final answer; the
interactive `afterAgentResponse` path remains supported.
The two migration cases failed before the repair and passed afterward; affected
configuration/consumer coverage passed 83 tests and 14 subtests, with three
existing interpreter/platform skips. Applying the repaired setup changed only
the existing Cursor hook configuration, and repeating all-client setup changed
no files. The subsequent ordinary Cursor session passed all checks in 81.5
seconds. Its workspace remained canonical `16b6dc9…`; the repair changes setup
merging rather than component pins or execution behavior. The earlier failed
run is retained as failed evidence.

The fallback prepares the editing workspace and binds VAWS sources; it does not
change the native application's default cwd or force all native tools into that
directory. Grok made two read-only shell calls without changing directory,
including one failed relative file read, then corrected itself. Its writes were
isolated throughout. This is a measured instruction-following cost, not enforced
native shell isolation. No personal Grok or Kimi binary patch is required.

Resume was outside this round's requested live scope. It retains its existing
task, worktree and component selection by contract; there is no periodic code
watcher or update during work. This round exercised official macOS CLIs, not
Windows GUI or NPU execution. The Mac was already locked when continuous
anti-sleep protection was established, so desktop UI acceptance was not repeated.

Local raw evidence is retained under untracked
`.vaws-local/implementation/20260913-unified-startup/acceptance/`: client
invocations and transcripts, native identities, start receipts, provider logs,
candidate metadata, runtime routing and cross-client knowledge results.
Private paths, native IDs and server addresses are intentionally absent here.

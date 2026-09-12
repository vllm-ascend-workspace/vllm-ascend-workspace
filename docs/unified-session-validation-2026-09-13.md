# Unified native-session startup acceptance

Status: dated validation evidence — 2026-09-13, acceptance in progress

This record covers the unified project-guidance startup and task-selected MCP
runtime. It does not promote the earlier personal Grok/Kimi builds or the
2026-09-12 results into acceptance of this new path.

## Evidence recorded so far

Configuration/guidance regression tests exercised the five stock-client plans,
stable gateway entries, exact generated-provider replacement, custom settings
preservation, Kimi official-event migration, context matchers and resume guidance.
The affected ten-file run passed 144 tests, with four platform-specific skips
and 13 subtests. These are configuration tests, not real model sessions.

The implementation is still being integrated. Coordinator PR #26 and knowledge
PR #26 are merged; final consumer pins, CI results and runtime evidence will be
recorded with acceptance.
No live client result is inferred from a generated configuration or connected
MCP panel.

## Real-client acceptance

| Client | Version / build | New session edits selected directory | Latest locked components and knowledge query/capture |
|---|---|---|---|
| Codex | Pending | Pending | Pending |
| Cursor | Pending | Pending | Pending |
| Claude Code | Pending | Pending | Pending |
| Official Grok | Pending | Pending | Pending |
| Official Kimi Code | Pending | Pending | Pending |

For each executed row, retain the ordinary launch/new action, native ID,
selected workspace and HEAD, task/environment receipt, provider startup facts
and knowledge result. Local paths and native IDs stay in untracked evidence;
this public record should summarize their relationships.
A stock-client result must identify the stock executable, not merely a version
number shared by a personal build.

## Remaining checks

A long-lived gateway must route a new task to its new selected component
environment while retaining an earlier task's fixed selection. Resume adds no
preparation or update under the existing contract; this round's requested live
acceptance scope is new sessions. Knowledge acceptance must
show shared configuration/index/model reuse across different worktrees, without
creating per-worktree knowledge stores. Update/preparation failures must expose
their phase and preserve existing work. Platform-specific results and skips are
reported separately; no remote NPU or Windows GUI acceptance is claimed here.

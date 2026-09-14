# Documentation index

Status: current, 2026-09-14

Every document under `docs/` is listed here or in a linked directory index. `Status: current` is a
contract. Dated validation evidence records the tested state; it is not a current
command recipe. Superseded designs and duplicate inventories are retained in Git.

## Architecture and features

- [Architecture atlas](architecture/README.md) — eight diagrams covering the platform, key features and six components, with implementation sources fixed to the 2026-09-14 snapshot. This is an introduction; current runtime contracts remain below. The atlas indexes all SVG and PNG assets.
- [Offline atlas page](architecture/index.html) — open it from a local checkout to browse the diagrams and download individual images.
- [Atlas generator](architecture/build_atlas.py), [renderer](architecture/render_atlas.cjs) and [metadata](architecture/atlas.json) — editable content, image export and local layout checks; usage is in the atlas index. Local render evidence is ignored by [the atlas ignore file](architecture/.gitignore).

## Current contracts

- [community-collaboration.md](community-collaboration.md) — first-use choices, contribution consent and revocation, GitHub authentication and token boundaries.
- [source-workspace.md](source-workspace.md) — exact development/release source pairs, independent multi-repository directories, automatic native source defaults and client lifecycle boundaries.
- [forks-and-updates.md](forks-and-updates.md) — requested setup and managed identity, personal forks and upstream preparation for independent local editing or managed work; ordinary review and explicit endpoints need no first-use setup.
- [identity-and-agent-coordination.md](identity-and-agent-coordination.md) — shared-root user attribution, opportunistic message delivery and compiled-output reuse; first-version boundaries are explicit.

- [platform-contract.md](platform-contract.md) — common Windows/macOS/Linux entry points, literal process arguments, native owners and immutable environments.
- [native-workspace-isolation.md](native-workspace-isolation.md) — official five-client guidance, editing paths, automatic attachments and fixed component selection; preparation is used on demand and native setup is optional.

- [design-principles.md](design-principles.md) — nine governing principles; total Agent task cost takes priority, tools stay bounded, knowledge is advisory, and valid work is reused.
- [windows-installation.md](windows-installation.md) — PowerShell setup, same-filesystem uv cache and verified offline transfer.
- [enterprise-network.md](enterprise-network.md) — enterprise proxy and CA discovery, trust recovery, bounded downloads, native checks and setup recovery.
- [local-tests.md](local-tests.md) — local test progress, subprocess lifetime, retained evidence and validated retries.
- [runtime-feedback-design.md](runtime-feedback-design.md) — real-machine progress, loaded runtime identity, state projection, compact output and connection diagnostics.
- [diagnostics-system.md](diagnostics-system.md) — shared severity, phase timing, bounded local evidence, automatic redacted issues and supervised Grok diagnosis.
- [README.md](README.md) — this index.
- [agent-feedback-contract.md](agent-feedback-contract.md) — Result Envelope v1: the JSON stdout contract for agent-facing commands.
- [comparability-certificate.md](comparability-certificate.md) — observational comparability certificate for paired measurements.
- [coordinator-consumption.md](coordinator-consumption.md) — how this scaffold consumes the installed vaws-coordinator package.
- [dependency-plane.md](dependency-plane.md) — `uv.lock` is the only pin; `vaws_deps.py status|doctor|sync`. Covers immutable preparation and capability reporting.
- [npu-fleet-monitor.md](npu-fleet-monitor.md) — local deploy and lifecycle of the standalone vaws-top fleet monitor.
- [knowledge-maintenance.md](knowledge-maintenance.md) — VA/NPU/AI/infra reference capabilities, independent Grok curation and decoupled feed transport; ordinary tasks keep three optional knowledge tools.
- [property-testing.md](property-testing.md) — property-based tests for the deterministic cores.
- [remote-dev-consumption.md](remote-dev-consumption.md) — direct existing-container/code/script work, full-ID endpoint reuse, unsupported boundaries and installed remote-dev wiring.
- [target-state.md](target-state.md) — current component ownership and runtime contracts, including the single minimal knowledge contract: optional reference, plain Markdown and no per-task bookkeeping.
- [tracked-leak-guard.md](tracked-leak-guard.md) — tracked-file leak scanner, hook, and CI.
- [tracked-path-guard.md](tracked-path-guard.md) — anti-rot guard against dead in-tree paths in tracked docs.

## Dated design and validation evidence

- [first-use-validation-2026-09-14.md](first-use-validation-2026-09-14.md) — fresh clone and actual Agent initialization, token-only Git, contribution choices, detached resume and supervised diagnostic workers; timing boundaries and earlier failed candidates are explicit.

- [diagnostics-validation-2026-09-13.md](diagnostics-validation-2026-09-13.md) — logging and timing coverage, local failure injection, protocol and process tests, measured instrumentation costs and explicit deployment/device limits.
- [legacy-retirement-2026-09-13.md](legacy-retirement-2026-09-13.md) — bounded consumer/Skill retirement, historical domain-note migration, retained live mechanisms and regression scope.

- [task-cost-reduction-validation-2026-09-13.md](task-cost-reduction-validation-2026-09-13.md) — local startup, optional dependency loading, managed preparation and completion costs, preserved failures and exact acceptance boundaries.

- [knowledge-platform-validation-2026-09-13.md](knowledge-platform-validation-2026-09-13.md) — final knowledge component selection, exact CI, installed-reference acceptance and independent maintenance deployment; engineering evidence, not domain knowledge.

- [validation/README.md](validation/README.md) — separate VAWS engineering archive: versioned evidence, all runtime/tool families and 18 business skills, measured limits and explicit gaps; not development-domain knowledge.
- [validation/knowledge-component-2026-09-13.md](validation/knowledge-component-2026-09-13.md) — knowledge PR30–32 exact CI provenance, retained local results and actual Grok export/Windows scheduled feed return; later final installation remains separate.
- [validation/coverage.json](validation/coverage.json) — bounded capability-to-source/test/evidence index used only by archive maintenance.
- [validation/check_archive.py](validation/check_archive.py) and [validation/test_check_archive.py](validation/test_check_archive.py) — read-only archive and retained-JUnit integrity checks; no runtime startup or test replay.
- [source-workspace-validation-2026-09-13.md](source-workspace-validation-2026-09-13.md) — final installed four-machine source validation, three-platform CI, Kimi review, actual migration and task reuse, measured costs, and automatic source-lock PR validation and merge.
- [managed-performance-redesign-2026-09-13.md](managed-performance-redesign-2026-09-13.md) — implemented preparation, wait, evidence and serving changes; real native and warm measurements with explicit limitations.

- [knowledge-adoption-2026-09-13.md](knowledge-adoption-2026-09-13.md) — source-level TeamAI/WeKnora comparison, selected knowledge mechanisms, nine-principle assessment and package/consumer validation boundaries.

- [mechanism-performance-review-2026-09-13.md](mechanism-performance-review-2026-09-13.md) — critical-path and reuse review, local source-packet and lock probes, and ranked mechanism proposals with explicit measurement limits.

- [existing-container-design.md](existing-container-design.md) — reviewed design for existing-container I/O, use-driven knowledge maintenance and lightweight native sessions; acceptance requirements are separate from results.
- [existing-container-review-2026-09-13.md](existing-container-review-2026-09-13.md) — redacted Kimi K3 max/Never Ask review, its sole required correction and approved design scope; no runtime acceptance claim.
- [existing-container-validation-2026-09-13.md](existing-container-validation-2026-09-13.md) — anonymized installed-candidate container, knowledge and native-review evidence, measured read costs and explicit pending final acceptance.
- [fresh-xhigh-six-comparison-2026-09-13.md](fresh-xhigh-six-comparison-2026-09-13.md) — independent fresh xhigh scenario-6 Agent comparison, common business input, explicit timing boundaries and retained failures.

- [native-kernel-recipe-performance-2026-09-13.md](native-kernel-recipe-performance-2026-09-13.md) — controlled incremental recipe compilation timings, retained fallback and failed attempts, and actual operator validation.

- [six-scenario-performance-2026-09-13.md](six-scenario-performance-2026-09-13.md) — fixed-component managed preparation, cross-container native reuse, incremental compilation and independent Agent timing comparisons.

- [unified-session-validation-2026-09-13.md](unified-session-validation-2026-09-13.md) — unified official-client preparation and MCP routing; acceptance progress and pending native cases are explicit.

- [native-client-validation-2026-09-12.md](native-client-validation-2026-09-12.md) — native worktree preparation, real client tasks and resume behavior, plus supported client extension boundaries.

- [shared-root-host-validation-2026-09-12.md](shared-root-host-validation-2026-09-12.md) — four-host managed CPU work, shared weights, busy NPU queue, real SSH messages, native cache reuse and default-branch update preparation.
- [shared-root-first-version-validation-2026-09-12.md](shared-root-first-version-validation-2026-09-12.md) — personal Fork setup, installed shared-root coordinator, messages, compiled-output reuse and idle daemon upgrade; local evidence and hardware limits.
- [core-workflow-validation-2026-09-12.md](core-workflow-validation-2026-09-12.md) — fixed execution inputs, native rebuild/reuse/switchback, Windows/WSL clients and real-host validation, with explicit scope limits.
- [workflow-usability-validation-2026-09-11.md](workflow-usability-validation-2026-09-11.md) — skill boundary audit, bounded service startup, source reuse and real four-host fleet lifecycle observations.
- [agent-only-validation-2026-09-11.md](agent-only-validation-2026-09-11.md) — Agent-only entry consolidation and Windows PowerShell/WSL acceptance, including corrections and hardware limits.
- [installation-feedback-2026-09-11.md](installation-feedback-2026-09-11.md) — fresh/cached Windows installation timings, cache relocation and dependency extras decision.
- [observation-feedback-2026-09-11.md](observation-feedback-2026-09-11.md) — SSH batching/timing evidence and status freshness behavior.
- [cli-feedback-2026-09-11.md](cli-feedback-2026-09-11.md) — paired Windows CLI startup measurements and parser side-effect checks.
- [windows-validation-2026-09-11.md](windows-validation-2026-09-11.md) — completed Windows non-NPU validation, repairs and limits at the recorded commits.

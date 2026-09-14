---
name: repo-init
description: "First entry to a fresh clone: discover authentication and confirm personal Fork, optional Star and community collaboration. Existing tasks reuse saved choices."
---

# First repository setup

Read this reference on first entry to a fresh clone, explicit initialization, or
reported incomplete setup. It stays outside automatic business skill discovery.
Later tasks, updates and resumes reuse recorded choices and completed stages.
Independent local work can continue while answers or authentication are pending.

For enterprise proxy or certificate failures, use the standard-library
[network deployment entry](../../../docs/enterprise-network.md). Incomplete
Fork/dependency setup discovers existing routes and reuses system trust once;
completed setup and ordinary tasks do not probe again. Recover existing trusted
CA sources before asking for new material. A local CA bundle does not prove that
Windows gh or a remote container uses that trust store.

Discover the available GitHub identity once:

```text
uv run --no-project python .agents/scripts/vaws_init.py status --detect-auth
```

An authenticated `gh`, `GH_TOKEN` / `GITHUB_TOKEN`, or the Agent's GitHub connector
can suggest a personal username. Reuse an explicit username already supplied by
the user; authentication does not replace that choice. Connector credentials are
not automatically available to local Git or the unattended worker. Never request
a token in conversation or write one into a URL.

Ask once for missing choices together:

1. Use the confirmed account and create/reuse its personal development Fork?
   Recommended. Declining preserves upstream remotes and records the identity.
2. Star the VAWS main repository? Optional and independent of other choices.
3. Enable **community collaboration**? Recommended. Link the
   [complete rules on GitHub](https://github.com/vllm-ascend-workspace/vllm-ascend-workspace/blob/main/docs/community-collaboration.md).
   Explain that it contributes redacted development knowledge and failure evidence,
   helps maintain central knowledge, and enables automatic issue reporting and
   configured Grok diagnosis. Central reference downloads and local diagnostics
   remain available when declined. Automatic repair is not currently implemented;
   do not promise automatic code changes, merges or deployments.

An unanswered question is not consent. Do not couple Star to features or silently
enable contributions because authentication exists. Apply only the actual answers;
the following is an example, not authorization:

```text
uv run --no-project python .agents/scripts/vaws_init.py apply --github-user USER --fork yes --star no --community enabled
```

Setup prepares the workspace Fork, locked runtime and knowledge dependencies,
installed-client wiring, local knowledge model/services/index and selected
contribution services. Knowledge package installation overlaps reporting-service
setup. Local reference preparation runs whether community collaboration is
enabled or disabled; it never uploads an existing contribution queue. These
one-time costs belong to this initialization, not the first task or its Stop hook.
Business repositories are prepared on demand by the first real task.
Business Forks use `workspace_forks.py --repo vllm` or
`--repo vllm-ascend` when contributing to those repositories.

Failure remains incomplete even if identity was saved. Resume with
`vaws_init.py apply`; completed stages and answers are reused. `status` without
`--detect-auth` is a local read. Change only community choice with
`apply --community disabled` or `enabled`; revocation precedes further setup.
Optional knowledge and contribution-service failures are reported separately as pending and
can be retried with `apply`; they do not block an otherwise ready local task.
Report these pending stages accurately: local-task readiness does not prove
that the model, index or every background service is ready. Task hooks and MCP
calls consume installed dependencies and never install missing knowledge packages.
Declining Star does not unstar an existing repository; declining Fork does not
delete existing forks or rewrite their remotes.

Complete the running client's native trust prompts. If the client has not loaded
the new configuration, reopen the project or start a new native session once.
Configuration files do not prove that native tools or hooks loaded. Record the
actual client, directory, and source/environment selection during acceptance.
Prepare a task with `vaws_start.py`, reusing an existing native context and any
already prepared W. Resumes retain the actual code and environment.

For authentication and prerequisites, read only the relevant part of
[bootstrap prerequisites](references/command-recipes.md). Later targeted repairs
use [forks and updates](../../../docs/forks-and-updates.md).

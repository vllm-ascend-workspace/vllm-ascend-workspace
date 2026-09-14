# Bootstrap prerequisites

Read only the operation needed by a first-use failure. The normal sequence is in
[the initialization reference](../SKILL.md); no whole-repository probe is needed.

## GitHub authentication

The setup command uses `GH_TOKEN` / `GITHUB_TOKEN` when present, including without
`gh`; otherwise it reuses `gh` authentication. HTTPS Git uses a host-scoped
credential helper that reads the token from the process, not from a remote URL.
The helper is command-scoped; it does not replace later gh/keyring authentication.
Do not print tokens, put them in command arguments, or request them in chat.
An Agent connector can report a candidate identity, but it does not establish
local Git or unattended-worker authentication. Restricted tokens may allow clone
while denying Fork, Star, push or issues; report the actual failed operation.

If choosing GitHub CLI, authenticate through `gh auth login`. If `gh` is not
installed and a normal package install is unavailable, the user-local fallback
installers are:

```text
python .agents/bootstrap/repo-init/scripts/install_gh_user.py
powershell -ExecutionPolicy Bypass -File .agents/bootstrap/repo-init/scripts/install-gh-user.ps1
```

Use the installer for the actual platform. It does not choose or authenticate a
GitHub user. Windows/WSL owner and offline dependency preparation details are in
[Windows installation](../../../../docs/windows-installation.md).
The Python installer supports all three platforms, verifies published SHA256
checksums, and reuses partial downloads. The no-Python PowerShell fallback bounds
each download with a child job and verifies checksums before installation.
For proxy, CA recovery and native-tool checks, use
[enterprise network deployment](../../../../docs/enterprise-network.md).

## Fork conflicts

`workspace_forks.py` without `--apply` reports its current plan. Review only the
reported conflict; preserve intentional refs, extra remotes and dirty work.
When replacing conflicting primary remote URLs is intended,
`--replace-primary-remotes` records their prior values before replacing them.
See [personal forks](../../../../docs/forks-and-updates.md#个人-fork).

Initialization preserves existing source revisions. To inspect the locked
development pair without network access, use
`uv run --no-project python .agents/scripts/workspace_sources.py show`.
The same command with `show --channel release` selects the declared vLLM release
baseline for the same Ascend commit. An explicit
`uv run --no-project python .agents/scripts/workspace_sources.py refresh`
updates only the committed source lock from upstream declarations; it does not
switch existing code. Later source selection, dependency repair and client changes use the
[maintenance commands](../../../../docs/forks-and-updates.md#显式维护与证据).

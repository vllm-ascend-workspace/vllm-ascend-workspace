Status: current

# Windows installation and offline transfer

These are Agent execution recipes for PowerShell in the workspace root. Prepare Windows x64
Python, uv and Git first. The validated combination is Python 3.13.12 and uv
0.10.7; the project supports other Python versions, but a prepared cache must
be validated again for another platform, interpreter or uv version. See the
[installation measurements](installation-feedback-2026-09-11.md).

## Online installation with a reusable user cache

```powershell
$workspaceRoot = (Get-Location).Path
$cachePath = Join-Path $env:LOCALAPPDATA 'vaws\offline-preparation-cache'
uv run --no-project python .agents/scripts/vaws_deps.py sync --locked --group dev --python 3.13 --cache-dir $cachePath --link-mode hardlink
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
uv run --no-project python .agents/scripts/vaws_deps.py doctor
if ($LASTEXITCODE -ne 0) { throw 'Dependency inspection failed' }
```

`--cache-dir` applies to this command. Keep using the same cache path on later
syncs. The default environment store is under local application data, outside the
checkout. A cache on the same filesystem permits hardlinks; cross-filesystem
installations fall back to copying. The ready receipt records the physical store
path, including MSIX application-data redirection where present. A junction or
mounted directory can still cross filesystems despite sharing a drive letter.
Use `--link-mode copy` when the destination filesystem cannot hardlink.
No global uv settings need to change. See
[uv cache location](https://docs.astral.sh/uv/concepts/cache/#cache-directory)
and [uv sync options](https://docs.astral.sh/uv/reference/cli/#uv-sync).

`--group dev` includes the local test dependencies. Omitting it does not remove
the three required runtime packages or the default knowledge capability.
The environment builder resolves the selected Python to its physical versioned
path. Install that Python before preparing an offline bundle. It accepts only
options represented in the immutable build contract; unknown options fail.

## Corporate proxies and system certificates

If uv reports `invalid peer certificate: UnknownIssuer` behind a corporate proxy
and the proxy CA is already trusted by Windows, enable the system trust store for
the current PowerShell session before downloading Python or syncing dependencies:

```powershell
$env:UV_NATIVE_TLS = 'true'
uv python install 3.13
uv run --no-project --python 3.13 python .agents/scripts/vaws_deps.py sync --locked --python 3.13
```

The sync entry preserves this setting for its installer. Current uv versions also
support `UV_SYSTEM_CERTS=true`; `--native-tls` and `--system-certs` can instead be
passed explicitly to `vaws_deps.py sync` when the bootstrap interpreter is already
available. Certificate verification stays enabled and the dependency lock is
unchanged.

If downloads time out under load while individual requests succeed, set
`$env:UV_CONCURRENT_DOWNLOADS = '4'` and `$env:UV_HTTP_TIMEOUT = '120'` before
retrying. The sync installer preserves these transport limits too. Resume
initialization with `vaws_init.py apply` to reuse saved choices and finished stages.

When an internal package mirror is reachable directly but times out through the
proxy, add that mirror's hostname to the session's `NO_PROXY`, preserving existing
entries. Keep external GitHub traffic on the configured proxy. Do not put proxy
credentials in tracked configuration or diagnostic output.

For an approved PyPI mirror, set `VAWS_PYPI_MIRROR` to its HTTPS simple-index URL
before resuming setup. For example, replace the placeholder below with the actual
mirror available on the machine:

```powershell
$env:VAWS_PYPI_MIRROR = 'https://mirror.example/simple'
uv run --no-project python .agents/scripts/vaws_init.py apply
```

On a cold install, VAWS compares a bounded sample (at most 256 KiB per source,
four-second connection/read timeouts) from the locked source and configured mirror.
It selects the mirror when the measured rate is at least 25% higher, or the default
source fails. Warm environment reuse and explicit `--offline` runs do not probe.
The selected source and measurements appear on stderr. A missing artifact,
incompatible mirror layout, or slower mirror retains the default source.

The mirror must retain PyPI's `packages/` artifact paths. Only the temporary
installation copy maps registry/artifact URLs; the checked-in lock, exact versions,
Git revisions, artifact SHA256 values and environment key stay unchanged. uv still
enforces `--locked` and rejects modified artifacts. This setting covers locked
PyPI packages; GitHub and knowledge model downloads use their own transports.

## Knowledge startup and the Microsoft C++ runtime

An installed knowledge environment can still fail at `import onnxruntime` with
Windows status `0xC0000005` when an old system `msvcp140.dll` is loaded. Inspect
the first native error and the installed Microsoft Visual C++ runtime version
before reinstalling Python packages. Updating the official Microsoft Visual C++
Redistributable is the normal system repair.

When a compatible, Microsoft-signed runtime is already installed in a protected
directory and a system update is unavailable, an explicit workspace selection is
also supported. Write `.vaws-local/windows-runtime.json` in the shared workspace
owner with `msvc_directory` set to that absolute directory. Verify its architecture
matches Python and verify the Microsoft signature before selecting it. Do not use
a download directory or an untrusted DLL location.

VAWS keeps the selected library loaded and makes its directory available to
Python extension imports and inherited child processes, including knowledge
daemons. This changes only the VAWS process tree; it does not replace system DLLs,
edit immutable environments, or change locked package versions. Resume
`vaws_init.py apply`, then reopen the native client so its MCP processes use the
selection. Recheck the path if Windows updates remove that runtime package.

Local knowledge also defaults `LITELLM_LOCAL_MODEL_COST_MAP=true` so importing its
backend uses bundled provider metadata without a pricing download. An explicit
environment value takes precedence; the local embedding model is unchanged.

## Prepare an offline bundle while online

First complete the online sync above for the exact checkout and target Python.
Wait for all uv operations using this cache to finish before copying it.
Create a new bundle directory; copy the entire cache without changing its
internal files. The manifest records the lock and tool combination.

```powershell
$bundlePath = Join-Path $workspaceRoot ('.vaws-local\offline-bundle-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $bundlePath) { throw 'Choose a new bundle directory' }
New-Item -ItemType Directory -Path $bundlePath -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath $cachePath -Destination (Join-Path $bundlePath 'uv-cache') -Recurse -Force -ErrorAction Stop
$receipt = uv run --no-project python -c 'import sys,json; from pathlib import Path; sys.path.insert(0,".agents/lib"); from vaws_environment import native_ready; print(json.dumps(native_ready(Path.cwd())))' | ConvertFrom-Json
$pythonIdentity = & $receipt.python -c 'import platform, sysconfig; print(platform.python_version(), sysconfig.get_platform())'
if ($LASTEXITCODE -ne 0) { throw 'Cannot read Python identity' }
$manifest = [ordered]@{
    uv = (uv --version)
    python = $pythonIdentity
    pyproject_sha256 = (Get-FileHash -LiteralPath 'pyproject.toml' -Algorithm SHA256).Hash
    lock_sha256 = (Get-FileHash -LiteralPath 'uv.lock' -Algorithm SHA256).Hash
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bundlePath 'manifest.json') -Encoding utf8 -ErrorAction Stop
$bundlePath
```

Transfer that bundle and the matching workspace checkout separately. Prepare
the Python/uv/Git installers while online if the destination lacks them. Do not
copy a prepared virtual environment as the installation: recreate it from the lock and cache. The
bundle does not include model weights, remote containers, shared knowledge
Release downloads or the separate vaws-top service. Those have their own
preparation and storage requirements. Native client configuration belongs to
the destination machine; see [repo-init](../.agents/bootstrap/repo-init/SKILL.md).

## Recreate the environment offline

Run from the matching destination checkout with the bundle path resolved from
the transfer operation. The following checks prevent accidentally
using a bundle prepared for a different lock or interpreter. Use a new local
cache directory instead of merging files into an active uv cache.

```powershell
$workspaceRoot = (Get-Location).Path
# $bundlePath is the actual transferred bundle directory selected by the Agent.
$manifest = Get-Content -LiteralPath (Join-Path $bundlePath 'manifest.json') -Raw -ErrorAction Stop | ConvertFrom-Json
if ((uv --version) -ne $manifest.uv) { throw 'Install the uv version recorded in manifest.json' }
$preparedPython = uv python find --offline 3.13
if ($LASTEXITCODE -ne 0) { throw 'Install the prepared Python interpreter first' }
$pythonIdentity = & $preparedPython -c 'import platform, sysconfig; print(platform.python_version(), sysconfig.get_platform())'
if ($LASTEXITCODE -ne 0) { throw 'Install the prepared Python interpreter first' }
if ($pythonIdentity -ne $manifest.python) { throw 'Python version or platform differs from the prepared cache' }
if ((Get-FileHash -LiteralPath 'pyproject.toml' -Algorithm SHA256).Hash -ne $manifest.pyproject_sha256) { throw 'pyproject.toml differs from the bundle' }
if ((Get-FileHash -LiteralPath 'uv.lock' -Algorithm SHA256).Hash -ne $manifest.lock_sha256) { throw 'uv.lock differs from the bundle' }
$cachePath = Join-Path $env:LOCALAPPDATA ('vaws\offline-cache-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $cachePath) { throw 'Choose a new cache directory' }
New-Item -ItemType Directory -Path (Split-Path -Parent $cachePath) -Force -ErrorAction Stop | Out-Null
Copy-Item -LiteralPath (Join-Path $bundlePath 'uv-cache') -Destination $cachePath -Recurse -Force -ErrorAction Stop
uv run --offline --no-project --python $preparedPython python .agents/scripts/vaws_deps.py sync --locked --group dev --python $preparedPython --offline --cache-dir $cachePath --link-mode hardlink
if ($LASTEXITCODE -ne 0) { throw 'Offline sync failed; retain the output and prepare the missing cache entries online' }
uv run --no-project python .agents/scripts/vaws_deps.py doctor
if ($LASTEXITCODE -ne 0) { throw 'Dependency inspection failed' }
```

The destination must have the same Python build available to uv; an explicit
path to that interpreter can replace `uv python find`. `--offline` limits uv to local and
cached data. A failure means the bundle, interpreter or selected dependencies
are incomplete; prepare those on a connected machine with the same lock and
retry. A changed lock needs a new prepared cache. Preserve the failed output.

Inspect the doctor's JSON `outcome` and individual capabilities. Successful
package installation does not prove remote access or NPU execution. A missing
optional fleet monitor may appear separately from the installed packages.
Use `uv cache clean --cache-dir $cachePath` only when intentionally discarding
that cache; do not manually alter uv's internal cache layout.
